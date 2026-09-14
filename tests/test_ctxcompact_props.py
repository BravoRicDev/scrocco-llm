"""Property-based (hypothesis) sull'invariante CACHE-CORRECT del ctxcompact:

1) IDEMPOTENZA: compact(compact(m)) == compact(m) (nessuna ri-compressione).
2) MONOTONIA DELLA FRONTIERA: con boundary_floor crescente i byte dei messaggi
   gia' compressi NON cambiano (nessuna regressione della prompt-cache).
3) PUREZZA: la stessa lista, stesse config -> stessi byte, sempre.

Se hypothesis non e' installata questi test sono saltati: l'invariante resta
documentata qui e replicata dai test deterministici in test_ctxcompact.py.
"""
import pytest

hyps = pytest.importorskip("hypothesis", reason="hypothesis non installata")
from hypothesis import given, settings, strategies as st  # noqa: E402

from app.ctxcompact import (CtxCompactConfig, compact_tool_outputs)  # noqa: E402


def _cfg(**kw):
    k = dict(enabled=True, keep_turns=2, max_tool_output_chars=64,
             min_saved_tokens=0, head_chars=32, tail_chars=32,
             keep_tail_pct=0.0, tool_args_max_chars=64)
    k.update(kw)
    return CtxCompactConfig(**k)


# ---------------------------------------------------------------- strategies
_word = st.text(alphabet="abcdefghij \n", min_size=0, max_size=400)


@st.composite
def conversation(draw):
    """Lista chat plausibile: user, assistant (a volte con tool_calls di args
    JSON), tool (output anche duplicati/grandi), chiusura user."""
    msgs = []
    n_turns = draw(st.integers(min_value=1, max_value=5))
    cids = []
    for t in range(n_turns):
        if draw(st.booleans()):
            msgs.append({"role": "user", "content": draw(_word)})
        calls = []
        if draw(st.booleans()):
            n = draw(st.integers(min_value=1, max_value=2))
            for c in range(n):
                cid = f"c{t}-{c}"
                cids.append(cid)
                big = draw(st.integers(min_value=0, max_value=3))
                argval = ("Z" * 200) if big else "x"
                calls.append({"id": cid, "type": "function",
                              "function": {
                                  "name": draw(st.sampled_from(
                                      ["bash", "write_file", "read"])),
                                  "arguments": '{"content": "%s"}' % argval}})
            msgs.append({"role": "assistant", "content": "ok",
                         "tool_calls": calls})
        else:
            msgs.append({"role": "assistant", "content": draw(_word)})
        if calls:
            for call in calls:
                body = draw(_word)
                msgs.append({"role": "tool",
                             "tool_call_id": call["id"],
                             "content": body})
    msgs.append({"role": "user", "content": "finale"})
    return msgs


# ------------------------------------------------------------------ purity
@settings(max_examples=80, deadline=None)
@given(msgs=conversation())
def test_pure_same_input_same_output(msgs):
    cfg = _cfg()
    a, ra = compact_tool_outputs([dict(m) for m in msgs], cfg)
    b, rb = compact_tool_outputs([dict(m) for m in msgs], cfg)
    assert a == b
    assert ra["boundary"] == rb["boundary"]
    assert ra["changed"] == rb["changed"]


# ------------------------------------------------------------- idempotenza
@settings(max_examples=80, deadline=None)
@given(msgs=conversation())
def test_idempotent_second_run_unchanged(msgs):
    cfg = _cfg()
    once, r1 = compact_tool_outputs([dict(m) for m in msgs], cfg)
    twice, r2 = compact_tool_outputs([dict(m) for m in once], cfg)
    # ri-applicare non deve cambiare un byte (gia' stub/rimando/intatto)
    assert twice == once


# ----------------------------------------------- monotonia della frontiera
@settings(max_examples=60, deadline=None)
@given(msgs=conversation(), extra_turns=st.integers(min_value=0, max_value=3))
def test_frontier_never_regresses_bytes_stable(msgs, extra_turns):
    cfg = _cfg(keep_turns=1)
    base, _r0 = compact_tool_outputs([dict(m) for m in msgs], cfg)
    # appendere turni NON deve toccare i messaggi gia' scritti: il floor e'
    # la frontiera maxima precedente (watermark di sessione).
    extended = [dict(m) for m in msgs]
    for k in range(extra_turns):
        extended.append({"role": "user", "content": f"ancora {k}"})
        big = "W" * 300
        extended.append({"role": "assistant", "content": "a", "tool_calls": [
            {"id": f"x{k}", "type": "function",
             "function": {"name": "bash",
                          "arguments": '{"content": "%s"}' % big}}]})
        extended.append({"role": "tool", "tool_call_id": f"x{k}",
                         "content": big})
    prev_boundary = _r0["boundary"] or 0
    got, rg = compact_tool_outputs(
        extended, cfg, boundary_floor=prev_boundary)
    assert rg["boundary"] >= prev_boundary
    # tutto il prefisso < prev_boundary resta byte-identico a `base`
    assert got[:prev_boundary] == base[:prev_boundary]
