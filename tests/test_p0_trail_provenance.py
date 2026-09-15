"""P0: attempt trail nel 503, esenzione riparazione con streak, provenienza
cooldown (heuristic/authoritative/credit/tier).

Regole utente rispettate: nessuna riscrittura del prompt (cache intatta),
nessun concetto provider-specifico; la sveglia continua a RADDOPPIARE il
cooldown sul KO del probe (qui si testa solo CHI puo' essere svegliato).
"""
import asyncio
import json
import os
import tempfile
import time

import pytest

from app import main
from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,intelligence_score
t@x.com,m/p0-a,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-A,,5
t@x.com,m/p0-a,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-B,,5
t@x.com,m/p0-b,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-C,,5
"""


@pytest.fixture()
def r():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    try:
        yield Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
                     Policy.from_dict({}))
    finally:
        os.path.exists(path) and os.unlink(path)


def _dep(r, key):
    for lst in r.config.groups.values():
        for d in lst:
            if d.get("api_key") == key:
                return d
    raise AssertionError(key)


# ----------------------------------------------------- P0-1 attempt trail
TRAIL = [
    {"ord": 1, "dep": "u1", "group": f"{BASE}-32k", "model": "m/a",
     "cls": "reasoning", "status": 400, "ms": 120},
    {"ord": 2, "dep": "u2", "group": f"{BASE}-32k", "model": "m/b",
     "cls": "rate_limited", "status": 429, "ms": 80},
]


def test_exhausted_espone_trail_e_header():
    r = main._exhausted(2, "boom", trail=TRAIL, retry_at_ms=1234567890)
    body = json.loads(bytes(r.body).decode())
    assert r.status_code == 503
    assert body["error"]["attempts"] == TRAIL
    assert body["error"]["retry_at_ms"] == 1234567890
    assert r.headers.get("x-scrocco-attempts") == "2"
    assert r.headers.get("x-scrocco-trail") == "u1:reasoning,u2:rate_limited"
    assert r.headers.get("retry-after") == "2"


def test_exhausted_trail_cappata_a_dieci():
    trail = [dict(TRAIL[0], ord=i, dep=f"u{i}") for i in range(1, 15)]
    r = main._exhausted(14, "boom", trail=trail)
    body = json.loads(bytes(r.body).decode())
    assert len(body["error"]["attempts"]) == 10
    assert r.headers.get("x-scrocco-attempts") == "10"


def test_retry_at_ms_dal_cooldown_residuo(r):
    d = _dep(r, "K-B")
    r.mark_failed(d["unique"], seconds=1800, reason="http_429", status=429)
    t = main._retry_at_ms(r, [{"dep": d["unique"], "cls": "rate_limited"}])
    assert t is not None and abs(t / 1000.0 - (time.time() + 1800)) < 30
    assert main._retry_at_ms(r, [{"dep": "sconosciuto"}]) is None


def test_nonstream_mette_il_trail_sull_errore_finale():
    """call_with_fallback annota l'UpstreamError finale con la trail
    (il 503 non-stream la legge da li')."""
    import httpx

    import app.forwarder as F
    from tests.test_error_passthrough_fixes import _mk

    cfg, router, broken, good = _mk()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b'{"error":{"message":"boom"}}')

    router.fallback_next = lambda *a, **k: None      # catena subito esausta

    async def go():
        fwd = F.Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        with pytest.raises(F.UpstreamError) as ei:
            await fwd.call_with_fallback(
                router, "test", broken,
                {"model": "x", "messages": [{"role": "user", "content": "x"}]},
                need=frozenset({"text"}))
        return ei.value
    err = asyncio.run(go())
    assert err.trail and err.trail[0]["dep"] == broken["unique"]
    assert err.trail[0]["cls"] in ("upstream_error", "provider_transient")


# ------------------------------------------- P0-2 esenzione con streak
def test_repair_exempt_streak_e_reset(r):
    u = _dep(r, "K-A")["unique"]
    assert r.repair_exempt_blocked(u, 3) is False
    assert r.note_repair_exempt(u) == 1
    assert r.note_repair_exempt(u) == 2
    assert r.repair_exempt_blocked(u, 3) is False
    assert r.note_repair_exempt(u) == 3
    assert r.repair_exempt_blocked(u, 3) is True      # budget esaurito
    r.reset_repair_exempt(u)
    assert r.repair_exempt_blocked(u, 3) is False


def test_repair_exempt_si_azzera_sul_successo(r):
    u = _dep(r, "K-A")["unique"]
    r.note_repair_exempt(u)
    r.note_repair_exempt(u)
    r.note_result(u, 42.0, quality=1.0, ctx_est=100)   # successo reale
    assert r.repair_exempt_blocked(u, 3) is False


def test_repair_exempt_limit_zero_disabilita(r):
    u = _dep(r, "K-A")["unique"]
    for _ in range(5):
        r.note_repair_exempt(u)
    assert r.repair_exempt_blocked(u, 0) is False      # 0 = nessun limite


# ------------------------------------------- P0-3 provenienza cooldown
def test_infer_provenance_e_probeable(r):
    inf = r._infer_provenance
    assert inf("credit", 402, False) == "credit"
    assert inf("forbidden", 403, False) == "tier"
    assert inf("http_429", 429, True) == "authoritative"
    assert inf("http_429", 429, False) == "heuristic"
    assert inf("watchdog", None, False) == "heuristic"


def test_cooldown_provenance_persistita_e_probeable(r):
    a, b, c = (_dep(r, k) for k in ("K-A", "K-B", "K-C"))
    r.mark_failed(a["unique"], seconds=60, reason="watchdog")
    r.mark_failed(b["unique"], seconds=1800, reason="http_429", status=429)
    r.mark_failed(c["unique"], seconds=None, reason="credit", status=402)
    assert r.cooldown_provenance(a["unique"]) == "heuristic"
    assert r.cooldown_provenance(b["unique"]) == "authoritative"
    assert r.cooldown_provenance(c["unique"]) == "credit"
    assert r.cooldown_probeable(a["unique"]) is True
    assert r.cooldown_probeable(b["unique"]) is False
    assert r.cooldown_probeable(c["unique"]) is False
    r.clear_cooldown(a["unique"])
    assert r.cooldown_provenance(a["unique"]) is None


def test_sveglia_salta_cooldown_authoritative(r):
    """Con retry dichiarato dal provider (authoritative) la sveglia non
    prova: solo i cooldown 'heuristic' sono svegliabili."""
    a, b = _dep(r, "K-A"), _dep(r, "K-B")
    now = time.time()
    r._cooldown[b["unique"]] = now + 600
    r._cooldown_since[b["unique"]] = now - 7200
    r.stats_for(b["unique"]).last_reason = "http_429"
    r._cooldown_prov()[b["unique"]] = "authoritative"     # retry dichiarato
    got = r.warm_wake_canary("test", a, None, 1000, 4096,
                             exclude_keys={"K-A"}, exclude_uniq={a["unique"]})
    assert got is None
    # stesso scenario ma cooldown euristico (nessun retry dichiarato)
    r._cooldown_prov()[b["unique"]] = "heuristic"
    got = r.warm_wake_canary("test", a, None, 1000, 4096,
                             exclude_keys={"K-A"}, exclude_uniq={a["unique"]})
    assert got is not None and got["unique"] == b["unique"]
