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
