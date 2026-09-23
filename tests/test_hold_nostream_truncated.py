"""Parita' hold-until-finish nel percorso NON-STREAMING.

Con hold attivo (`policy.qc_json.stream_hold_until_finish`, default True, OR
colonna CSV `hold_until_finish`) la risposta non-streaming — gia' interamente
bufferizzata — non deve MAI consegnare un moncone troncato dal modello
(`finish_reason=length` con contenuto): si ruota PRE-CONSEGNA su un candidato
piu' capace, con soft cooldown del troncatore. Stessa garanzia del path
streaming (`tests/test_hold_never_truncated.py`).
"""
import asyncio
import os
import tempfile

import httpx
import pytest

from app.config import GatewayConfig
from app.forwarder import Forwarder, UpstreamError
from app.policy import Policy
from app.router import Router

CSV = (
    "commento,modello,provider,endpoint,data,context,max_input,priority,"
    "scrocco-llm-test,caps\n"
    "a,trunc,groq,https://trunc.test/v1,paid,128,8000,5,K1,\n"
    "a,good,groq,https://good.test/v1,paid,128,8000,5,K2,text\n"
)
GRP = "scrocco-llm-test-fallback"


def _mk_router(hold=True):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    pol = Policy.from_dict({})
    pol.cooldown_jitter_ratio = 0        # secondi di cooldown esatti
    pol.qc_json.stream_hold_until_finish = hold
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    router = Router(cfg, pol)
    os.unlink(path)
    return router


def _deps(router):
    g = router.config.groups[GRP]
    return (next(d for d in g if d["api_key"] == "K1"),
            next(d for d in g if d["api_key"] == "K2"))


def _fwd(handler):
    return Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))


def _run(router, fwd, dep, payload):
    return asyncio.run(fwd.call_with_fallback(
        router, "test", dep, payload, collect_qc_failures=True))


PAYLOAD = {"model": "x", "messages": [{"role": "user", "content": "ciao"}]}


def _no_rotate(*a, **k):
    raise AssertionError("NON deve ruotare")


def test_hold_length_con_contenuto_ruota_su_capace():
    router = _mk_router(hold=True)
    trunc, good = _deps(router)
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "trunc.test":
            return httpx.Response(200, json={"choices": [{
                "message": {"content": "risposta parziale"},
                "finish_reason": "length"}]})
        return httpx.Response(200, json={"choices": [{
            "message": {"content": "risposta completa"},
            "finish_reason": "stop"}]})

    router.fallback_next = lambda *a, **k: good
    res = _run(router, _fwd(handler), trunc, dict(PAYLOAD))
    data, used = res[0], res[1]
    assert used["api_key"] == "K2"
    assert data["choices"][0]["message"]["content"] == "risposta completa"
    assert seen == ["trunc.test", "good.test"]
    assert router.is_cooled_down(trunc["unique"])       # soft cooldown
    assert not router.is_cooled_down(good["unique"])


def test_hold_length_cap_del_client_consegna():
    """Il modello si e' fermato ESATTAMENTE sul max_tokens del client: e' il
    cap voluto -> nessuna rotazione, si consegna."""
    router = _mk_router(hold=True)
    trunc, _good = _deps(router)

    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "risposta parziale"},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 400}})

    router.fallback_next = _no_rotate
    p = dict(PAYLOAD)
    p["max_tokens"] = 400
    res = _run(router, _fwd(handler), trunc, p)
    data, used = res[0], res[1]
    assert used["api_key"] == "K1"
    assert data["choices"][0]["message"]["content"] == "risposta parziale"


def test_senza_hold_il_troncato_passa():
    router = _mk_router(hold=False)
    trunc, _good = _deps(router)

    def handler(request):
        return httpx.Response(200, json={"choices": [{
            "message": {"content": "risposta parziale"},
            "finish_reason": "length"}]})

    router.fallback_next = _no_rotate
    res = _run(router, _fwd(handler), trunc, dict(PAYLOAD))
    data, used = res[0], res[1]
    assert used["api_key"] == "K1"
    assert data["choices"][0]["message"]["content"] == "risposta parziale"


def test_hold_zero_answer_length_ruota_senza_penale():
    """finish_reason=length con 0 caratteri: budget esaurito, non colpa del
    deployment -> ruota SENZA penale verso il piu' capace (parita' stream)."""
    router = _mk_router(hold=True)
    trunc, good = _deps(router)

    def handler(request):
        if request.url.host == "trunc.test":
            return httpx.Response(200, json={"choices": [{
                "message": {"content": ""}, "finish_reason": "length"}]})
        return httpx.Response(200, json={"choices": [{
            "message": {"content": "ok"}, "finish_reason": "stop"}]})

    router.fallback_next = lambda *a, **k: good
    res = _run(router, _fwd(handler), trunc, dict(PAYLOAD))
    data, used = res[0], res[1]
    assert used["api_key"] == "K2"
    assert data["choices"][0]["message"]["content"] == "ok"
    assert not router.is_cooled_down(trunc["unique"])   # nessuna penale


def test_hold_toolcall_args_non_validi_lasciati_al_repair():
    """Args tool-call non riparabili: competenza del tool-repair -> il QC non
    ruota e non azzera: si consegna il turno con la tool-call cosi' com'e'."""
    router = _mk_router(hold=True)
    trunc, _good = _deps(router)
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json={"choices": [{
            "message": {"content": "",
                        "tool_calls": [{"id": "c1", "type": "function",
                                        "function": {
                                            "name": "run",
                                            "arguments": '{"a":1,,"b":2}'}}]},
            "finish_reason": "tool_calls"}]})

    router.fallback_next = _no_rotate
    p = dict(PAYLOAD)
    p["tools"] = [{"type": "function",
                   "function": {"name": "run", "parameters": {}}}]
    data, used = _run(router, _fwd(handler), trunc, p)[:2]
    assert used["api_key"] == "K1"               # nessuna rotazione
    assert calls == ["trunc.test"]              # nessun retry correttivo
    msg = data["choices"][0]["message"]
    assert msg.get("tool_calls")                # tool-call mantenuta


def test_hold_catena_esaurita_503():
    """Tutti i candidati troncano: mai il moncone -> 503 RETRYABLE."""
    router = _mk_router(hold=True)
    trunc, _good = _deps(router)

    def handler(request):
        return httpx.Response(200, json={"choices": [{
            "message": {"content": "risposta parziale"},
            "finish_reason": "length"}]})

    router.fallback_next = lambda *a, **k: None
    with pytest.raises(UpstreamError) as ei:
        _run(router, _fwd(handler), trunc, dict(PAYLOAD))
    assert ei.value.status == 503
