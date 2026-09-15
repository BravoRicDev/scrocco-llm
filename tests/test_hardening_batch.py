"""Batch F23-F28 + G1-G4 (un solo commit).

F23 recovery tool-call troncati in stream; F24 dedup per contenuto; F25
circuit breaker per provider|modello; F26 flush unificato; F27 warm pool
deterministico; F28 cold-spread pesato a token; G1 histnorm tail_floor;
G2 keyhealth quota; G3 fail-fast ctx overflow; G4 autoprobe per-chiave.
"""
import asyncio
import inspect
import json
import os
import tempfile

import pytest

import app.autoprobe as AP
import app.forwarder as F
from app import main as M
from app import metrics
from app.config import GatewayConfig
from app.ctxcompact import (compact_tool_outputs, create_ctxcompact_config)
from app.histnorm import create_hist_config, normalize_messages
from app.keyhealth import KeyHealth
from app.policy import Policy
from app.router import Router
from app.toolrepair import ToolRepairSSEFilter, create_tool_repair_config

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m,groq,https://api.groq.com/openai/v1,free,64,32000,0,K1,text
t@x,m,groq,https://api.groq.com/openai/v1,free,64,32000,0,K2,text
t@x,m,groq,https://api.groq.com/openai/v1,free,64,32000,0,K3,text
t@x,m2,groq,https://api.groq.com/openai/v1,free,64,32000,0,K4,text
"""


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    r = Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
               Policy.from_dict({}))
    yield r
    os.unlink(path)


def _deps(r, model):
    return [d for deps in r.config.groups.values() for d in deps
            if d.get("model") == model]


def _u(r, model):
    return _deps(r, model)[0]["unique"]


def _group_of(r, model):
    return next(g for g, deps in r.config.groups.items()
                if any(d.get("model") == model for d in deps))


# ------------------------------------------------------------------- F23
def _delta(idx, cid, name, args):
    obj = {"id": "c1", "object": "chat.completion.chunk", "created": 0,
           "model": "m",
           "choices": [{"index": 0, "finish_reason": None,
                        "delta": {"tool_calls": [{"index": idx, "id": cid,
                                                  "type": "function",
                                                  "function": {
                                                      "name": name,
                                                      "arguments": args}}]}}]}
    return ("data: " + json.dumps(obj) + "\n\n").encode()


def _chunks(blob):
    out = []
    for line in blob.splitlines():
        if line.startswith("data: "):
            out.append(line[6:])
    return out


def test_f23_abort_finalize_closes_truncated_tool_call():
    dep = {"model": "m", "provider": "groq", "api_key": "K",
           "tool_repair": "aggressive"}
    flt = ToolRepairSSEFilter(create_tool_repair_config(), dep)
    out = list(flt.feed(_delta(0, "call_1", "read_file", '{"path": "a')))
    out += flt.abort_finalize()
    blob = b"".join(out).decode()
    args = None
    saw_finish = saw_done = False
    for payload in _chunks(blob):
        if payload == "[DONE]":
            saw_done = True
            continue
        ch = json.loads(payload)["choices"][0]
        for tc in (ch["delta"].get("tool_calls") or []):
            a = tc.get("function", {}).get("arguments")
            if a:
                args = a
        if ch.get("finish_reason") == "tool_calls":
            saw_finish = True
    assert args is not None
    assert json.loads(args) == {"path": "a"}      # JSON chiuso e valido
    assert saw_finish and saw_done
    # idempotente: un secondo abort non rimette un finish
    assert flt.abort_finalize() == []


# ------------------------------------------------------------------- F24
def test_f24_dedup_identico_per_contenuto_non_per_id():
    from app.ctxcompact import CtxCompactConfig
    cfg = CtxCompactConfig(max_tool_output_chars=50, min_saved_tokens=0,
                           keep_turns=2)
    body = "ROWS\n" + ("x" * 400)

    def _tc(i):
        return {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"c{i}", "type": "function",
                                "function": {"name": "read_file",
                                             "arguments": "{}"}}]}

    msgs = [
        {"role": "user", "content": "start"},
        _tc(1),
        {"role": "tool", "tool_call_id": "c1", "content": body},
        _tc(2),
        {"role": "tool", "tool_call_id": "c2", "content": body},
        {"role": "user", "content": "avanti"},
        {"role": "user", "content": "fine"},
    ]
    out, rep = compact_tool_outputs(msgs, cfg, max_in=0)
    assert rep["changed"] and rep["deduped"] == 1
    assert out[2]["content"].startswith("[rimando:")
    assert "c2" in out[2]["content"]          # rimanda alla copia recente
    assert not out[4]["content"].startswith("[rimando:")


# ------------------------------------------------------------------- F25
def test_f25_model_circuit_apre_su_chiavi_distinte(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    r = Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
               Policy.from_dict({}))
    try:
        for d in _deps(r, "m"):
            r.mark_failed(d["unique"], reason="http_503", status=503)
        r._cooldown.clear()                    # il cooldown per-unique non c'entra
        assert r._model_blocked(_deps(r, "m")[0]) is True
        g = _group_of(r, "m")
        pick = r.pick_deployment(g)
        assert pick is not None and pick["model"] == "m2"
        # disattivabile
        r.policy.model_circuit_enabled = False
        assert r._model_blocked(_deps(r, "m")[0]) is False
    finally:
        os.unlink(path)


def test_f25_model_circuit_scade_col_tempo(router):
    for d in _deps(router, "m"):
        router.mark_failed(d["unique"], reason="http_503", status=503)
    dep = _deps(router, "m")[0]
    assert router._model_blocked(dep) is True
    key = router._model_cb_key(dep)
    router._model_cb[key]["opened"] -= 10_000
    assert router._model_blocked(dep) is False


# ------------------------------------------------------------------- F26
def test_f26_flush_unificato_esiste():
    assert callable(getattr(M, "_maybe_save_all", None))
    src = inspect.getsource(M._maybe_save_all)
    assert "_last_stats_save" in src and "_last_routing_save" in src


# ------------------------------------------------------------------- F27
def test_f27_warm_deterministico_senza_regex():
    src = inspect.getsource(M.chat_completions)
    assert "_grp_is_dim" in src
    assert "_warm = (not explicit_req) or _grp_is_dim" in src
    assert "re.search" not in src.split("_warm")[1].split("\n")[0]


# ------------------------------------------------------------------- F28
def test_f28_usage_pesato_a_token(router):
    u = _u(router, "m")
    router.note_usage(u, ctx_est=80000)
    router.note_usage(u, ctx_est=8000)
    router.note_usage(u)
    assert router.usage_count_24h(u) == 3
    assert router.usage_weight_24h(u) == pytest.approx(10.0 + 1.0 + 1.0)


# ------------------------------------------------------------------- G1
def test_g1_histnorm_tail_floor_protegge_il_prefisso():
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "orfano", "content": "x"},
        {"role": "assistant", "content": "fatto"},
    ]
    cfg = create_hist_config({"history_normalize": {
        "tail_only": True, "drop_orphan_tool": True}})
    _o1, r1 = normalize_messages(msgs, cfg)
    assert r1.get("changed")                    # senza floor tocca il vecchio
    _o2, r2 = normalize_messages(msgs, cfg, tail_floor=3)
    assert not r2.get("changed")                # col floor la coda e' intatta


# ------------------------------------------------------------------- G2
def test_g2_keyhealth_429_non_avanza(tmp_path):
    kh = KeyHealth(str(tmp_path))
    st = kh.observe("u1", fail_streak=5, success_ema=0.0, is_cooled=True,
                    reason="http_429", status=429)
    assert st is None and "u1" not in kh.data
    st2 = kh.observe("u1", fail_streak=5, success_ema=0.0, is_cooled=True,
                     status=500)
    assert st2 == "dead_suspect"


# ------------------------------------------------------------------- G3
def test_g3_fail_fast_overflow(router, monkeypatch):
    calls = []
    monkeypatch.setattr(metrics, "inc", lambda n, l=None: calls.append(n))
    g = _group_of(router, "m")
    # alza il ctx oltre OGNI max_input del gruppo
    for d in router.config.groups[g]:
        d["max_input_tokens"] = 32000
    assert router.pick_deployment(g, ctx=999999) is None
    assert "nx_ctx_overflow_total" in calls


def test_g3_main_ha_il_400_context_length_exceeded():
    src = inspect.getsource(M.chat_completions)
    assert "context_length_exceeded" in src
    assert "nx_ctx_compacted_forced" in src


# ------------------------------------------------------------------- G4
def test_g4_autoprobe_gap_per_chiave():
    AP._key_last_probe.clear()
    dep = {"api_key": "K1"}
    assert AP._key_gap_ok(dep, 1000.0, 300.0) is True
    AP._note_key_probe(dep, 1000.0)
    assert AP._key_gap_ok(dep, 1100.0, 300.0) is False
    assert AP._key_gap_ok(dep, 1300.0, 300.0) is True
    assert AP._key_gap_ok({"api_key": ""}, 1100.0, 300.0) is True
    AP._key_last_probe.clear()
