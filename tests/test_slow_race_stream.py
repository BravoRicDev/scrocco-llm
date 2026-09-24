"""GARA LENTA nel percorso STREAMING (caller `_stream_with_fallback`).

Regressione del bug: `stream_slow_race_after_ms` vive su **Policy**, non su
`qc_json`. Il caller lo leggeva da `qcp = router.policy.qc_json` -> restava
sempre 0 e il canario lento non partiva MAI (`[slow-race]` assente dai log).

Qui si verifica end-to-end: con HOLD e A che emette contenuto ma NON chiude,
il timer lento apre 1 canario, vince chi consegna, e A non prende nessuna
penale (`is_cooled_down` False). L'hedge classico resta un trigger separato.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time

import app.main as M
from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

_HDR = ("commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,caps\n")
_GOOD = "a,good,groq,https://ok.test/v1,free,128,200000,5,K-G,text\n"
_BROKEN = "a,broken,cloudflare,https://cf.test/v1,free,128,200000,9,K-B,text\n"


def _mk(csv_text, *, slow_ms=100, hedge_base=50):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(csv_text)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    pol.cooldown_jitter_ratio = 0
    pol.warm_refill_enabled = False          # isola il trigger "lento"
    pol.qc_json.stream_hold_until_finish = True
    pol.qc_json.stream_commit_min_chars = 4
    pol.qc_json.stream_hedge_delay_ms = hedge_base
    pol.stream_slow_race_after_ms = slow_ms  # campo su Policy (il bug)
    pol.slow_canary_after_ms = slow_ms       # canary e flag insieme (come prima)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    router = Router(cfg, pol)
    os.unlink(path)
    by_key = {}
    for deps in cfg.groups.values():
        for d in deps:
            by_key[d["api_key"]] = d
    return cfg, router, by_key


def _chunk(obj):
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _sse(text):
    return (_chunk({"choices": [{"index": 0, "delta": {"content": text},
                                 "finish_reason": None}]})
            + _chunk({"choices": [{"index": 0, "delta": {},
                                   "finish_reason": "stop"}]})
            + b"data: [DONE]\n\n")


def _content_of(raw) -> str:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    txt = ""
    for line in raw.split("\n"):
        s = line.strip()
        if not s.startswith("data:"):
            continue
        body = s[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            obj = json.loads(body)
        except Exception:
            continue
        for ch in (obj.get("choices") or []):
            c = (ch.get("delta") or {}).get("content")
            if isinstance(c, str):
                txt += c
    return txt


class _Fwd:
    """Il dep `slow_unique` streamma e NON chiude; gli altri chiudono subito."""

    def __init__(self, slow_unique, slow_sleep=0.3):
        self.slow = slow_unique
        self.slow_sleep = slow_sleep
        self.calls = []

    async def stream_response(self, d, payload, **kw):
        self.calls.append(d["unique"])
        if d["unique"] == self.slow:
            async def _g():
                yield _chunk({"choices": [
                    {"index": 0, "delta": {"content": "sto arrivando"},
                     "finish_reason": None}]})
                await asyncio.sleep(self.slow_sleep)   # generazione lunga...
                yield _chunk({"choices": [              # ...ma chiusura pulita
                    {"index": 0, "delta": {}, "finish_reason": "stop"}]})
                yield b"data: [DONE]\n\n"
            return _g()

        async def _g2():
            yield _sse("ok")
        return _g2()


def _stream(monkeypatch, cfg, router, fwd, first, payload):
    monkeypatch.setattr(M, "router", router)
    monkeypatch.setattr(M, "config", cfg)
    monkeypatch.setattr(M, "forwarder", fwd)

    async def _run():
        resp = await M._stream_with_fallback(
            "test", first, payload, need=frozenset({"text"}), scope="chain")
        out = b""
        async for chunk in resp.body_iterator:
            out += chunk if isinstance(chunk, bytes) else chunk.encode()
        return out

    return asyncio.run(_run())


_PAYLOAD = {"model": "x", "messages": [{"role": "user", "content": "ciao"}]}


def test_stream_slow_race_parte_e_vince(monkeypatch, caplog):
    cfg, router, by_key = _mk(_HDR + _BROKEN + _GOOD, slow_ms=100)
    broken, good = by_key["K-B"], by_key["K-G"]
    fwd = _Fwd(broken["unique"])
    with caplog.at_level(logging.INFO, logger="nx.main"):
        out = _stream(monkeypatch, cfg, router, fwd, broken, _PAYLOAD)
    assert good["unique"] in fwd.calls, "il canario lento deve partire"
    assert _content_of(out) == "ok", "vince chi consegna prima"
    assert any("[slow-race]" in r.getMessage() for r in caplog.records), \
        "il campo deve essere letto da policy (regressione)"
    assert router.is_cooled_down(broken["unique"]) is False, \
        "nessuna penale per il lento"


def test_stream_slow_race_spenta_non_parte(monkeypatch, caplog):
    cfg, router, by_key = _mk(_HDR + _BROKEN + _GOOD, slow_ms=0)
    good = by_key["K-G"]
    fwd = _Fwd(None)                    # nessun dep lento
    with caplog.at_level(logging.INFO, logger="nx.main"):
        out = _stream(monkeypatch, cfg, router, fwd, good, _PAYLOAD)
    assert _content_of(out) == "ok"
    assert not any("[slow-race]" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# Regressione: l'APERTURA di un canary (refill) non deve bloccare il loop.
# `_open_canary` fa `await forwarder.stream_response` (attende le headers):
# con l'apertura sequenziale un provider lento bloccava il loop e il `break`
# sul vincitore scavalcava il check del timer -> nessun `[slow-race]`.
_ROW_A = "a,dep-a,groq,https://a.test/v1,free,128,200000,5,K-A,text\n"
_ROW_R = "a,dep-r,groq,https://r.test/v1,free,128,200000,5,K-R,text\n"
_ROW_G = "a,dep-g,groq,https://g.test/v1,free,128,200000,5,K-G,text\n"


class _FwdSlowOpen:
    """`open_delay_dep` impiega `open_delay` s PRIMA di dare le headers."""

    def __init__(self, slow_dep, open_delay_dep, open_delay=0.5,
                 slow_sleep=0.3):
        self.slow = slow_dep
        self.delay = open_delay_dep
        self.open_delay = open_delay
        self.slow_sleep = slow_sleep
        self.calls = []

    async def stream_response(self, d, payload, **kw):
        self.calls.append(d["unique"])
        if d["unique"] == self.delay:
            await asyncio.sleep(self.open_delay)      # headers LENTE
        if d["unique"] == self.slow:
            async def _g():
                yield _chunk({"choices": [
                    {"index": 0, "delta": {"content": "sto arrivando"},
                     "finish_reason": None}]})
                await asyncio.sleep(self.slow_sleep)
                yield _chunk({"choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}]})
                yield b"data: [DONE]\n\n"
            return _g()

        async def _g2():
            yield _sse("ok")
        return _g2()


def test_stream_slow_race_parte_con_apertura_refill_lenta(monkeypatch,
                                                          caplog):
    cfg, router, by_key = _mk(_HDR + _ROW_A + _ROW_R + _ROW_G, slow_ms=100)
    a, r, g = by_key["K-A"], by_key["K-R"], by_key["K-G"]
    router.policy.warm_refill_enabled = True
    monkeypatch.setattr(router, "warm_fill_canary", lambda *a_, **k: [r])
    monkeypatch.setattr(router, "warm_wake_canary", lambda *a_, **k: None)
    monkeypatch.setattr(router, "hedge_canaries", lambda *a_, **k: [g])
    fwd = _FwdSlowOpen(a["unique"], r["unique"], open_delay=0.5,
                       slow_sleep=0.3)
    with caplog.at_level(logging.INFO, logger="nx.main"):
        out = _stream(monkeypatch, cfg, router, fwd, a, _PAYLOAD)
    assert g["unique"] in fwd.calls, \
        "il canario lento deve partire anche se l'apertura del refill e' lenta"
    assert _content_of(out) == "ok", "vince chi consegna prima"
    assert any("[slow-race]" in rec.getMessage() for rec in caplog.records), \
        "il timer deve scattare comunque"
    assert router.is_cooled_down(a["unique"]) is False


class _FwdDelay:
    """A ritarda il primo byte; `open_delay_dep` consegna le headers dopo
    `open_delay` (apertura lenta, come un provider che tarda a rispondere)."""

    def __init__(self, slow_dep, open_delay_dep, first_content=0.3,
                 close_after=0.1, open_delay=0.5):
        self.slow = slow_dep
        self.delay = open_delay_dep
        self.first_content = first_content
        self.close_after = close_after
        self.open_delay = open_delay
        self.calls = []

    async def stream_response(self, d, payload, **kw):
        self.calls.append(d["unique"])
        if d["unique"] == self.delay:
            await asyncio.sleep(self.open_delay)      # headers LENTE
        if d["unique"] == self.slow:
            async def _g():
                await asyncio.sleep(self.first_content)
                yield _chunk({"choices": [
                    {"index": 0, "delta": {"content": "sto arrivando"},
                     "finish_reason": None}]})
                await asyncio.sleep(self.close_after)
                yield _chunk({"choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}]})
                yield b"data: [DONE]\n\n"
            return _g()

        async def _g2():
            yield _sse("ok")
        return _g2()


def test_stream_canary_in_volo_non_viene_cancellato(monkeypatch, caplog):
    """REGRESSIONE: apertura PRE-looop bloccante + canary mai cancellato.

    A ritarda il primo byte (0.3s) e chiude (0.4s) DURANTE l'apertura lenta
    (0.5s) del canary classico: col codice VECCHIO il primo `asyncio.wait`
    trovava futA gia' completo, il `break` sul vincitore scavalcava il timer
    e `[slow-race]` non compariva mai. Inoltre il canary rimasto in volo NON
    deve essere cancellato: se consegna, entra in warm (regola utente).
    """
    cfg, router, by_key = _mk(_HDR + _ROW_A + _ROW_R + _ROW_G, slow_ms=100)
    a, r, g = by_key["K-A"], by_key["K-R"], by_key["K-G"]
    _n = {"h": 0}

    def _hedge(*a_, **k):
        _n["h"] += 1
        return [r] if _n["h"] == 1 else [g]
    monkeypatch.setattr(router, "hedge_canaries", _hedge)
    monkeypatch.setattr(router, "warm_fill_canary", lambda *a_, **k: [])
    monkeypatch.setattr(router, "warm_wake_canary", lambda *a_, **k: None)
    warmed: list[str] = []
    monkeypatch.setattr(router, "note_warm_owner",
                        lambda sid, u: warmed.append(u))
    fwd = _FwdDelay(a["unique"], r["unique"], first_content=0.3,
                    close_after=0.1, open_delay=0.5)
    monkeypatch.setattr(M, "router", router)
    monkeypatch.setattr(M, "config", cfg)
    monkeypatch.setattr(M, "forwarder", fwd)

    async def _run():
        with caplog.at_level(logging.INFO, logger="nx.main"):
            resp = await M._stream_with_fallback(
                "test", a, _PAYLOAD, need=frozenset({"text"}), scope="chain")
            out = b""
            async for chunk in resp.body_iterator:
                out += chunk if isinstance(chunk, bytes) else chunk.encode()
        t0 = time.monotonic()          # attende l'handover dei canary in volo
        while M._PROBE_TASKS and time.monotonic() - t0 < 3.0:
            await asyncio.sleep(0.02)
        return out

    out = asyncio.run(_run())
    assert any("[slow-race]" in rec.getMessage() for rec in caplog.records), \
        "il timer deve scattare anche con l'apertura di un canary bloccata"
    assert g["unique"] in fwd.calls, "il canario del timer deve partire"
    assert r["unique"] in fwd.calls, "il canary classico era stato aperto"
    assert r["unique"] in warmed, \
        "il canary in volo NON va cancellato: se consegna entra in warm"


def test_stream_gate_riceve_il_budget_output(monkeypatch):
    """Il gate della gara lenta deve ricevere il VERO budget di output del
    client (prima era None -> contava come 'capaci' caldi che non potevano
    consegnare l'output richiesto)."""
    cfg, router, by_key = _mk(_HDR + _ROW_A + _ROW_R + _ROW_G, slow_ms=50)
    a = by_key["K-A"]
    seen: list = []

    def _gate(sid, profile, group, need, ctx, out=None, tried=None):
        seen.append(out)
        return False                      # gate chiuso: nessun canario

    monkeypatch.setattr(router, "slow_race_allowed", _gate)
    monkeypatch.setattr(router, "hedge_canaries", lambda *a_, **k: [])
    monkeypatch.setattr(router, "warm_fill_canary", lambda *a_, **k: [])
    monkeypatch.setattr(router, "warm_wake_canary", lambda *a_, **k: None)
    fwd = _Fwd(a["unique"])               # A: contenuto, pausa, chiusura
    out = _stream(monkeypatch, cfg, router, fwd, a,
                  dict(_PAYLOAD, max_tokens=1234))
    assert seen, "il gate deve essere chiamato"
    assert seen[0] == 1234, f"budget di output propagato al gate: {seen[0]}"
    assert _content_of(out) == "sto arrivando"

