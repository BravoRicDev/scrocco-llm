"""Miglioramenti F/G/H (+ C/D/E): persistenza con backup, floor Retry-After,
isteresi ctxcompact, stima token adattiva in shadow, unwrap ricorsivo
tool_repair, indice+rollup dello sniff.

Tutti unit test offline: nessuna rete, nessun container."""
import json
import logging

import httpx
import pytest

from app import forwarder, sniff
from app.atomic_store import load_json, save_json
from app.ctxcompact import CtxCompactConfig, should_compact
from app.router import (_tokens_for_text, configure_estimate, estimate_tokens,
                        estimate_shadow_stats)
from app.toolrepair import _collapse_double_serialization


# --------------------------------------------------------------------- F) atomic
def test_atomic_roundtrip_and_backup(tmp_path):
    p = tmp_path / "state.json"
    assert save_json(p, {"a": 1}, indent=1)
    assert p.exists() and (tmp_path / "state.json.bak").exists()
    assert load_json(p, dict) == {"a": 1}


def test_atomic_recovers_from_bak(tmp_path):
    p = tmp_path / "state.json"
    save_json(p, {"keep": True}, indent=1)
    # il file principale si corrompe (scrittura interrotta)
    p.write_text("{corrotto", encoding="utf-8")
    assert load_json(p, dict) == {"keep": True}


def test_atomic_missing_returns_default(tmp_path):
    assert load_json(tmp_path / "nope.json", dict) == {}


# ----------------------------------------------------------------- G) retry floor
@pytest.fixture()
def retry_floor():
    old = forwarder.RETRY_AFTER_MIN_SEC
    yield
    forwarder.RETRY_AFTER_MIN_SEC = old


def test_retry_floor_raises_small_header(retry_floor):
    forwarder.set_retry_after_floor(10)
    r = httpx.Response(429, headers={"retry-after": "1"})
    assert forwarder._retry_after_from(r, "") == 10.0


def test_retry_floor_keeps_larger_header(retry_floor):
    forwarder.set_retry_after_floor(10)
    r = httpx.Response(429, headers={"retry-after": "60"})
    assert forwarder._retry_after_from(r, "") == 60.0


def test_retry_floor_body_and_disable(retry_floor):
    forwarder.set_retry_after_floor(10)
    r = httpx.Response(429)
    assert forwarder._retry_after_from(r, "Please retry in 2.5s") == 10.0
    forwarder.set_retry_after_floor(0)          # floor disabilitato
    assert forwarder._retry_after_from(r, "Please retry in 2.5s") == 2.5


# --------------------------------------------------------------- D) tool unwrap
def test_unwrap_triple_serialization():
    inner = {"a": 1}
    s = json.dumps(inner)
    s = json.dumps(s)              # stringa dentro stringa
    s = json.dumps(s)
    out, changed = _collapse_double_serialization(s)
    assert changed and json.loads(out) == inner


def test_unwrap_array_then_string():
    s = json.dumps([json.dumps({"b": 2})])
    out, changed = _collapse_double_serialization(s)
    assert changed and json.loads(out) == {"b": 2}


def test_unwrap_noop_on_plain():
    out, changed = _collapse_double_serialization("not json")
    assert not changed and out == "not json"


# ------------------------------------------------------- H) ctxcompact hysteresis
def _warm(**kw):
    """compatta solo per soglia assoluta (cache calda, nessuno switch)."""
    cfg = CtxCompactConfig(min_ctx_tokens=1000, switch_min_tokens=1000, **kw)
    return cfg


def test_ctxcompact_defers_when_headroom():
    dec = should_compact(_warm(), ctx_est=5000, max_in=100000,
                         holder="d1", dep_unique="d1")
    assert dec["compact"] is False


def test_ctxcompact_fires_near_saturation():
    dec = should_compact(_warm(), ctx_est=90000, max_in=100000,
                         holder="d1", dep_unique="d1")
    assert dec["compact"] is True and dec["reason"] == "abs"


def test_ctxcompact_overflow_always():
    dec = should_compact(_warm(), ctx_est=5000, max_in=1000,
                         holder="d1", dep_unique="d1")
    assert dec["compact"] is True and dec["overflow"] is True


def test_ctxcompact_ratio_zero_is_historical():
    dec = should_compact(_warm(abs_headroom_ratio=0.0), ctx_est=5000,
                         max_in=100000, holder="d1", dep_unique="d1")
    assert dec["compact"] is True


# ------------------------------------------------------- C) estimate adaptive
@pytest.fixture()
def est_mode():
    old_stats = dict(estimate_shadow_stats())
    yield
    configure_estimate(adaptive=False, shadow=True)
    from app import router as _r
    _r._estimate_shadow_stats.update({"n": 0, "legacy": 0, "adaptive": 0})


def test_estimate_shadow_returns_legacy(est_mode):
    configure_estimate(adaptive=False, shadow=True)
    msgs = [{"role": "user", "content": "x" * 400}]
    assert estimate_tokens(msgs, 4) == 100
    assert estimate_shadow_stats()["n"] >= 1


def test_estimate_adaptive_denser_for_code(est_mode):
    code = json.dumps({"k": "v" * 200, "n": [1, 2, 3, 4, 5]})
    assert _tokens_for_text(code) > _tokens_for_text("a b c d " * 20)


def test_estimate_adaptive_mode_returns_adaptive(est_mode):
    configure_estimate(adaptive=True, shadow=False)
    msgs = [{"role": "user", "content": "{}[]()<>;:," * 50}]
    assert estimate_tokens(msgs, 4) > (len("{}[]()<>;:," * 50) // 4)


# --------------------------------------------------------------- E) sniff index
def _reset_sniff():
    for name in ("nx.sniff", "nx.sniff.index"):
        logging.getLogger(name).handlers.clear()
    sniff._logger = None
    sniff._index_logger = None
    sniff._rollup = {}
    sniff._rollup_hour = None


def test_sniff_writes_index(tmp_path):
    _reset_sniff()
    try:
        full = tmp_path / "sniff.log"
        idx = tmp_path / "sniff-index.log"
        sniff.configure(str(full), 24, str(idx))
        assert sniff._index_logger is not None
        sn = sniff.begin("rid1", {"model": "m"}, {"messages": []})
        sn.finish_json({"ok": True}, {"status": 200, "answer_chars": 42,
                                      "tries": 1})
        text = idx.read_text(encoding="utf-8")
        assert '"dir": "idx"' in text and '"rid": "rid1"' in text
        assert '"status": 200' in text
    finally:
        _reset_sniff()
