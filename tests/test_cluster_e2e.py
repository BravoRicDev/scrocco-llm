"""Multi-worker END-TO-END: supervisore reale con 2 worker (processi veri).

Il pacchetto `app` viene copiato in una directory temporanea (var/ isolata),
davanti a un upstream finto OpenAI-compatibile con tre chiavi, di cui una
risponde sempre 429. Si verifica che:
- il servizio risponda come col processo singolo;
- la chiave in 429 venga provata UNA sola volta in tutto il cluster (il
  cooldown deciso da un worker vale subito anche per l'altro);
- le sessioni vivano sul worker proprietario;
- la morte di un worker non causi errori (e il worker venga riavviato);
- /metrics esponga le serie di entrambi i worker;
- SIGTERM chiuda tutto in modo pulito.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("uvicorn")

REPO = Path(__file__).resolve().parent.parent
KEY = "sk-master-e2e-test-0123456789abcdef"

UPSTREAM = '''
import asyncio, json
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

HITS = {}

async def chat(request):
    body = await request.json()
    auth = request.headers.get("authorization", "")
    HITS[auth] = HITS.get(auth, 0) + 1
    if "K-BAD" in auth:
        return JSONResponse({"error": {"message": "rate limited"}}, status_code=429,
                            headers={"retry-after": "600"})
    usage = {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}
    if body.get("stream"):
        async def gen():
            for i in range(3):
                c = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": body["model"],
                     "choices": [{"index": 0, "delta": {"content": f"p{i} "}, "finish_reason": None}]}
                yield f"data: {json.dumps(c)}\\n\\n"
                await asyncio.sleep(0.02)
            c = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": body["model"],
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}
            yield f"data: {json.dumps(c)}\\n\\n"
            yield "data: [DONE]\\n\\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    return JSONResponse({"id": "x", "object": "chat.completion", "created": 1, "model": body["model"],
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": "ciao a te"},
                                      "finish_reason": "stop"}], "usage": usage})

async def hits(request):
    return JSONResponse(HITS)

app = Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"]), Route("/hits", hits)])
'''


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, timeout: float = 45.0, **kw) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2.0, **kw).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"{url} non pronto")


@pytest.fixture()
def cluster2(tmp_path):
    up_port, gw_port = _free_port(), _free_port()
    work = tmp_path / "gw"
    shutil.copytree(REPO / "app", work / "app", ignore=shutil.ignore_patterns("__pycache__"))
    (work / "var").mkdir()
    (work / "var" / "keys_rotation.csv").write_text(
        "commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps\n"
        + "".join(f"{n},model-a,groq,http://127.0.0.1:{up_port}/v1,free,32000,8000,5,{k},\n"
                  for n, k in (("a", "K-GOOD1"), ("b", "K-BAD"), ("c", "K-GOOD2"))))
    (tmp_path / "upstream.py").write_text(UPSTREAM)
    run_root = tempfile.mkdtemp(prefix="sc-")        # path corto: limite AF_UNIX
    env = {**os.environ, "GATEWAY_WORKERS": "2", "GATEWAY_MASTER_KEY": KEY, "GATEWAY_PORT": str(gw_port),
           "GATEWAY_HEARTBEAT_FILE": str(tmp_path / "hb"), "GATEWAY_RUN_DIR": run_root,
           "BACKGROUND_CAUTIOUS": "1", "PYTHONUNBUFFERED": "1",
           # watcher lento: una modifica di config vista SUBITO dall'altro
           # worker puo' venire solo dall'avviso sul bus
           "GATEWAY_WATCH_SECONDS": "30"}
    env.pop("PYTEST_CURRENT_TEST", None)
    logs = open(tmp_path / "gw.log", "wb")
    up = subprocess.Popen([sys.executable, "-m", "uvicorn", "upstream:app", "--port", str(up_port),
                           "--log-level", "warning"], cwd=tmp_path, stdout=logs, stderr=subprocess.STDOUT)
    gw = subprocess.Popen([sys.executable, "-m", "app.serve"], cwd=work, env=env,
                          stdout=logs, stderr=subprocess.STDOUT)
    try:
        _wait(f"http://127.0.0.1:{up_port}/hits")
        _wait(f"http://127.0.0.1:{gw_port}/healthz")
        run_dir = next(Path(run_root).iterdir())
        for i in (0, 1):                                # entrambi i worker pronti
            deadline = time.time() + 45
            while not (run_dir / f"w{i}.sock").exists() and time.time() < deadline:
                time.sleep(0.2)
        yield {"gw": gw, "up_port": up_port, "base": f"http://127.0.0.1:{gw_port}",
               "run_dir": run_dir, "log": tmp_path / "gw.log", "work": work}
    finally:
        for p in (gw, up):
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        for p in (gw, up):
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()
        logs.close()
        shutil.rmtree(run_root, ignore_errors=True)


def _chat(base: str, session: str, stream: bool = False) -> httpx.Response:
    return httpx.post(f"{base}/v1/chat/completions", timeout=30.0,
                      headers={"authorization": f"Bearer {KEY}", "x-session-id": session},
                      json={"model": "scrocco-llm-test", "stream": stream,
                            "messages": [{"role": "user", "content": "ciao"}]})


def _worker_state(run_dir: Path, i: int) -> dict:
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(run_dir / f"w{i}.sock"))) as c:
        return c.get("http://w/admin/state", headers={"authorization": f"Bearer {KEY}"}).json()


def test_two_workers_behave_like_one_process(cluster2):
    base, run_dir = cluster2["base"], cluster2["run_dir"]
    for i in range(16):
        r = _chat(base, f"ses_{i}", stream=bool(i % 2))
        assert r.status_code == 200, r.text
        if i % 2:
            assert "data: [DONE]" in r.text
        else:
            assert r.json()["choices"][0]["message"]["content"].strip()

    # la chiave in 429 e' stata provata UNA volta in tutto il cluster
    hits = httpx.get(f"http://127.0.0.1:{cluster2['up_port']}/hits").json()
    assert hits.get("Bearer K-BAD") == 1

    s0, s1 = _worker_state(run_dir, 0), _worker_state(run_dir, 1)
    # stato globale identico su entrambi i worker
    assert s0["cooldowns_active"] and \
        [c["unique"] for c in s0["cooldowns_active"]] == [c["unique"] for c in s1["cooldowns_active"]]
    # stato per-sessione: ogni sessione su UN solo worker, entrambi usati
    ses0 = {e["session_id"] for e in s0["sticky_sessions"]}
    ses1 = {e["session_id"] for e in s1["sticky_sessions"]}
    assert ses0 and ses1 and not ses0 & ses1
    assert ses0 | ses1 == {f"ses_{i}" for i in range(16)}

    text = httpx.get(f"{base}/metrics").text
    assert 'worker="0"' in text and 'worker="1"' in text


def test_worker_crash_is_invisible_and_restarted(cluster2):
    base, run_dir = cluster2["base"], cluster2["run_dir"]
    for i in range(16):                              # porta la chiave in 429 in cooldown
        assert _chat(base, f"warm_{i}").status_code == 200
    before = [c["unique"] for c in _worker_state(run_dir, 0)["cooldowns_active"]]
    assert before
    # individua il worker 1 dal log del supervisore
    log_text = cluster2["log"].read_text(errors="replace")
    pid = int(log_text.split("worker 1 avviato (pid ")[1].split(")")[0])
    os.kill(pid, signal.SIGKILL)
    for i in range(10):                              # nessun errore visibile
        assert _chat(base, f"crash_{i}").status_code == 200
    deadline = time.time() + 45
    while time.time() < deadline:
        if cluster2["log"].read_text(errors="replace").count("worker 1 avviato") >= 2 \
                and (run_dir / "w1.sock").exists():
            break
        time.sleep(0.3)
    else:
        raise AssertionError("worker 1 non riavviato")
    time.sleep(1.0)
    # il worker riavviato e' entrato allineato allo stato vivo dell'altro
    assert "allineato" in cluster2["log"].read_text(errors="replace")
    assert [c["unique"] for c in _worker_state(run_dir, 1)["cooldowns_active"]] == before
    hits_before = httpx.get(f"http://127.0.0.1:{cluster2['up_port']}/hits").json().get("Bearer K-BAD")
    for i in range(6):
        assert _chat(base, f"after_{i}").status_code == 200
    assert httpx.get(f"http://127.0.0.1:{cluster2['up_port']}/hits").json().get("Bearer K-BAD") == hits_before


def test_admin_config_change_reaches_the_other_worker_immediately(cluster2):
    run_dir = cluster2["run_dir"]
    auth = {"authorization": f"Bearer {KEY}"}
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(run_dir / "w0.sock"))) as w0:
        r = w0.patch("http://w/admin/policy", headers=auth, json={"step_up_pct": 55})
        assert r.status_code == 200, r.text
    deadline = time.time() + 3.0                   # il watcher da solo impiegherebbe 30s
    while time.time() < deadline:
        if _worker_state(run_dir, 1)["policy"]["step_up_pct"] == 55:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("il worker 1 non ha ricaricato la policy")


def test_sigterm_shuts_down_cleanly(cluster2):
    gw = cluster2["gw"]
    assert _chat(cluster2["base"], "ses_bye").status_code == 200
    gw.send_signal(signal.SIGTERM)
    assert gw.wait(timeout=30) == 0
    assert not cluster2["run_dir"].exists()
    text = cluster2["log"].read_text(errors="replace")
    assert "supervisore terminato" in text and "Traceback" not in text
