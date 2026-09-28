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

from app import chat_completions, chat_stream, main, stream_verdicts  # noqa: F401
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
        if os.path.exists(path):
            os.unlink(path)


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
    r = stream_verdicts._exhausted(2, "boom", trail=TRAIL, retry_at_ms=1234567890)
    body = json.loads(bytes(r.body).decode())
    assert r.status_code == 503
    assert body["error"]["attempts"] == TRAIL
    assert body["error"]["retry_at_ms"] == 1234567890
    assert r.headers.get("x-scrocco-attempts") == "2"
    assert r.headers.get("x-scrocco-trail") == "u1:reasoning,u2:rate_limited"
    assert r.headers.get("retry-after") == "2"


def test_exhausted_trail_cappata_a_dieci():
    trail = [dict(TRAIL[0], ord=i, dep=f"u{i}") for i in range(1, 15)]
    r = stream_verdicts._exhausted(14, "boom", trail=trail)
    body = json.loads(bytes(r.body).decode())
    assert len(body["error"]["attempts"]) == 10
    assert r.headers.get("x-scrocco-attempts") == "10"


# ------------------- P0-1b: il trail registra ANCHE i hop per VERDETTO
# Prima del fix un 503 con catena esaurita per verdetti (empty_eof,
# length_truncated, timeout, fake_tool_call, struct_invalid) restituiva
# attempts=[] e nessun X-Scrocco-Trail: il client non sapeva quante/quali
# deployment erano stati scartati.

# una entry per ogni verdetto, con la classe e lo status che il trail deve
# riportare (stessa mappa usata dal ciclo di fallback)
_VERDICT_TRAIL = [
    {"ord": 1, "dep": "v1", "group": BASE, "model": "m/a",
     "cls": "empty_eof", "status": 502, "ms": 30},
    {"ord": 2, "dep": "v2", "group": BASE, "model": "m/b",
     "cls": "length_truncated", "status": 502, "ms": 40},
    {"ord": 3, "dep": "v3", "group": BASE, "model": "m/c",
     "cls": "timeout", "status": 504, "ms": 5000},
    {"ord": 4, "dep": "v4", "group": BASE, "model": "m/d",
     "cls": "fake_tool_call", "status": 502, "ms": 60},
    {"ord": 5, "dep": "v5", "group": BASE, "model": "m/e",
     "cls": "struct_invalid", "status": 422, "ms": 70},
]


def test_exhausted_trail_di_verdetti_e_valido():
    r = stream_verdicts._exhausted(5, "empty_eof", trail=_VERDICT_TRAIL)
    body = json.loads(bytes(r.body).decode())
    assert r.status_code == 503
    # attempts NON vuoto: e' la lista dei deployment scartati
    assert body["error"]["attempts"] == _VERDICT_TRAIL
    assert r.headers.get("x-scrocco-attempts") == "5"
    # l'header elenca ogni hop con la sua classe di verdetto
    assert r.headers.get("x-scrocco-trail") == (
        "v1:empty_eof,v2:length_truncated,v3:timeout,"
        "v4:fake_tool_call,v5:struct_invalid")


def test_exhausted_attempts_non_mai_zero():
    """Con trail vuoto ma n_tries>0 l'header non puo' dire "0" tentativi."""
    r = stream_verdicts._exhausted(3, "boom", trail=[])
    assert r.headers.get("x-scrocco-attempts") == "3"
    assert "x-scrocco-trail" not in r.headers        # niente trail da mostrare
    # nemmeno con n_tries=0 (o None): resta almeno 1, mai 0
    assert stream_verdicts._exhausted(0, "b").headers.get(  # type: ignore[arg-type]
        "x-scrocco-attempts") == "1"
    assert stream_verdicts._exhausted(None, "b").headers.get(  # type: ignore[arg-type]
        "x-scrocco-attempts") == "1"


def test_trail_campione_per_verdetto_generato():
    """Il ciclo di fallback registra davvero l'hop di verdetto.

    Non basta il test su _exhausted (riceverebbe qualunque trail): qui si
    verifica che il codice del ciclo mappi ogni verdetto sulla classe e sullo
    status attesi, e che appenda al trail.
    """
    import inspect

    src = inspect.getsource(chat_stream._StreamFallback)
    # append al trail con classe e status, non un recordon generico
    assert '"cls": self._v_cls' in src and '"status": self._v_st' in src
    assert "self.trail.append(" in src
    # e la mappa deve coprire i verdetti citati nel contratto
    for v in ("timeout", "struct_invalid", "empty_eof", "length_truncated",
              "fake_tool_call"):
        assert f'"{v}"' in src, f"verdetto {v} non mappato nel trail"
    # timeout e' un 504, la struttura non conforme un 422, il resto 502
    assert "504" in src and 'verdict == "timeout"' in src
    assert '"struct_corrective",' in src and '"struct_invalid"' in src
    assert "else 502" in src


def test_ret_popola_il_trail_nel_result_box():
    """Criterio 3 (il percorso dati): _ret consegna il trail al chiamante."""
    import inspect

    src = inspect.getsource(chat_stream._StreamFallback)
    ret_src = src[src.index("def _ret(self, resp):"):]
    ret_src = ret_src[:ret_src.index("\n    def ")]
    assert 'self.result_box["trail"] = list(self.trail)' in ret_src
    # insieme a dep/attempts, che il redirect non-stream gia' usava
    assert 'self.result_box["dep"]' in ret_src
    assert 'self.result_box["attempts"]' in ret_src


def test_redirect_nonstream_trasporta_il_trail():
    """Criterio 3: il redirect non-stream passa il trail da result_box
    all'UpstreamError, cosi' il 503 finale non e' piu' anonimo."""
    from app.forwarder import UpstreamError

    err = UpstreamError(503, "chain esaurita")
    assert getattr(err, "trail", None) is None
    # il tratto di codice che collega i due: la firma deve accettere il trail
    import inspect as _i
    src = _i.getsource(chat_completions.chat_completions)
    assert '_err.trail = _meta.get("trail")' in src
    assert "_ret(" in src


def test_retry_at_ms_dal_cooldown_residuo(r):
    d = _dep(r, "K-B")
    r.mark_failed(d["unique"], seconds=1800, reason="http_429", status=429)
    t = stream_verdicts._retry_at_ms(r, [{"dep": d["unique"], "cls": "rate_limited"}])
    assert t is not None and abs(t / 1000.0 - (time.time() + 1800)) < 30
    assert stream_verdicts._retry_at_ms(r, [{"dep": "sconosciuto"}]) is None


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
    assert getattr(err, "trail", None) and err.trail[0]["dep"] == broken["unique"]  # type: ignore[attr-defined]
    assert err.trail[0]["cls"] in ("upstream_error", "provider_transient")  # type: ignore[attr-defined]


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
