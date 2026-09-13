"""Anti-stall streaming, canonicalizzazione famiglie, graceful shutdown del
ledger e drain inflight."""
import asyncio
import os
import tempfile

from app import forwarder
from app import ledger as ledger_mod
from app.capabilities import canonical_family
from app.config import GatewayConfig
from app.ctxcompact import CtxCompactConfig, should_compact
from app.ledger import Ledger
from app.policy import Policy
from app.router import Router


# --------------------------------------------------------------- anti-stall
def test_stall_guard_passes_fast_chunks():
    async def agen():
        yield b"a"
        yield b"b"

    async def run():
        got = [c async for c in forwarder._stall_guard(agen(), 1.0, "m")]
        assert got == [b"a", b"b"]

    asyncio.run(run())


def test_stall_guard_raises_on_gap():
    async def agen():
        yield b"x"
        await asyncio.sleep(5)
        yield b"y"

    async def run():
        it = forwarder._stall_guard(agen(), 0.05, "m")
        assert await it.__anext__() == b"x"
        try:
            await it.__anext__()
        except forwarder.StreamStallError as exc:
            assert exc.seconds == 0.05
            assert exc.model == "m"
        else:
            raise AssertionError("StreamStallError non sollevata")

    asyncio.run(run())


def test_stream_stall_error_is_timeout():
    assert issubclass(forwarder.StreamStallError, asyncio.TimeoutError)


def test_set_stream_stall_sec_clamps():
    old = forwarder.STREAM_STALL_SEC
    try:
        forwarder.set_stream_stall_sec(3)
        assert forwarder.STREAM_STALL_SEC == 3.0
        forwarder.set_stream_stall_sec(-1)
        assert forwarder.STREAM_STALL_SEC == 0.0
        forwarder.set_stream_stall_sec("bad")
        assert forwarder.STREAM_STALL_SEC == 0.0
    finally:
        forwarder.STREAM_STALL_SEC = old


# ---------------------------------------------------------------- famiglie
def test_canonical_family_aliases():
    assert canonical_family("meta-llama/llama-3-8b-instruct") == "llama-3-8b"
    assert canonical_family("llama3-8b") == "llama-3-8b"
    assert canonical_family("nvidia/gpt-oss-120b:free") == "gpt-oss-120b"
    assert canonical_family("models/gemini-1.5-flash") == "gemini-1-5-flash"
    assert canonical_family("") == ""


def test_same_family_suppresses_switch():
    cfg = CtxCompactConfig(min_ctx_tokens=50000, switch_min_tokens=8000)
    d = should_compact(cfg, 10000, 0, "A", "B", False)
    assert "switch" in d["reason"]
    d2 = should_compact(cfg, 10000, 0, "A", "B", False, same_family=True)
    assert d2["reason"] == ""


def test_same_family_keeps_overflow():
    cfg = CtxCompactConfig(min_ctx_tokens=0, switch_min_tokens=8000)
    d = should_compact(cfg, 10000, 5000, "A", "B", False, same_family=True)
    assert d["overflow"] is True
    assert "overflow" in d["reason"]


# --------------------------------------------------------------- ledger/sd
def test_flush_sync_writes_and_counts(tmp_path):
    led = Ledger(tmp_path)
    led.record({"kind": "chat", "n": 1})
    led.record({"kind": "chat", "n": 2})
    assert led.flush_sync() == 2
    assert (tmp_path / "usage_ledger.jsonl").exists()


def test_flush_sync_empty(tmp_path):
    assert Ledger(tmp_path).flush_sync() == 0


CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,m-a,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-A,text
t@x.com,m-b,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-B,text
"""
GRP = "scrocco-llm-test-32k"


def test_inflight_total_sums():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    try:
        r = Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
                   Policy.from_dict({}))
        assert r.inflight_total() == 0
        u1 = r.config.groups[GRP][0]["unique"]
        u2 = r.config.groups[GRP][1]["unique"]
        r.stats_for(u1).inflight = 2
        r.stats_for(u2).inflight = 3
        assert r.inflight_total() == 5
    finally:
        os.unlink(path)


# ------------------------------------------------------------------- policy
def test_policy_parses_stream_and_shutdown():
    p = Policy.from_dict({"stream_stall_sec": 5, "shutdown_drain_sec": 3})
    assert p.stream_stall_sec == 5.0
    assert p.shutdown_drain_sec == 3.0
