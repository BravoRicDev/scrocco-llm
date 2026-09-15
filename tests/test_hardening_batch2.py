"""Test I-batch: cite_retention senza regex (I1), dedup normalizzato + rimando
con head (I2), JSON fenced/dominante (I3), ledger requeue (I4), metrics LRU
(I5), reset dei flag per-request (I6)."""
import json

import pytest

from app.ctxcompact import (CtxCompactConfig, _json_struct_cut,
                            compact_tool_outputs)


def _tc(i):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": f"c{i}", "type": "function",
                            "function": {"name": "read_file",
                                         "arguments": "{}"}}]}


def _msgs(old_body, tail_text):
    return [
        {"role": "user", "content": "start"},
        _tc(1),
        {"role": "tool", "tool_call_id": "c1", "content": old_body},
        _tc(2),
        {"role": "tool", "tool_call_id": "c2", "content": tail_text},
        {"role": "user", "content": "avanti"},
        {"role": "user", "content": "fine"},
    ]


def _msgs_cited(old_body, tail_user):
    """Come `_msgs` ma la citazione sta nell'ULTIMA user message: la coda
    protetta e' `messages[boundary:]` (boundary = user_idx[-keep_turns]), non
    i messaggi tool."""
    m = _msgs(old_body, "nessun riferimento qui")
    m[-1] = {"role": "user", "content": tail_user}
    return m


def _cfg_head(**kw):
    """head/tail piccoli cosi' lo stub e' DAVVERO piu' corto del contenuto
    (col default 600+600 un body da 400 char verrebbe "gonfiato" e la guardia
    `len(stub) >= n` lo salterebbe)."""
    base = dict(head_chars=50, tail_chars=50, max_tool_output_chars=50,
                min_saved_tokens=0, keep_turns=2, cite_min_freq=3)
    base.update(kw)
    return CtxCompactConfig(**base)


# --------------------------------------------------------------------- I1
def test_i1_token_generico_non_protegge():
    """Un token ripetuto ma senza '/' o '.' non e' una citazione: si stubba."""
    old = "abcdef src/main.rs\n" + ("y" * 400)
    tail = "abcdef e ancora abcdef, poi abcdef"
    out, rep = compact_tool_outputs(_msgs_cited(old, tail), _cfg_head(),
                                    max_in=0)
    assert rep["cite_kept"] == 0
    assert out[2]["content"].startswith("[tool output omesso")


def test_i1_path_ripetuto_protegge():
    """Un path-like ripetuto >= min_freq nella coda protegge l'output."""
    old = "abcdef src/main.rs\n" + ("y" * 400)
    tail = "vedi src/main.rs, poi src/main.rs, ancora src/main.rs"
    out, rep = compact_tool_outputs(_msgs_cited(old, tail), _cfg_head(),
                                    max_in=0)
    assert rep["cite_kept"] == 1
    assert out[2]["content"] == old       # intatto


# --------------------------------------------------------------------- I2
def test_i2_dedup_su_contenuto_normalizzato():
    """Output che differiscono solo per date/ms/hex collassano in un rimando."""
    cfg = CtxCompactConfig(max_tool_output_chars=50, min_saved_tokens=0,
                           keep_turns=2)
    b1 = ("ROWS\n2026-09-15 12:00:00\n0xdeadbeef\n" + "x" * 400 + "\n72 ms")
    b2 = ("ROWS\n2026-09-16 13:11:22\n0xfeedface\n" + "x" * 400 + "\n91 ms")
    msgs = _msgs(b1, b2)
    out, rep = compact_tool_outputs(msgs, cfg, max_in=0)
    assert rep["deduped"] == 1
    assert out[2]["content"].startswith("[rimando:")
    assert "head: " in out[2]["content"]


def test_i2_rimando_porta_un_head_del_contenuto():
    cfg = CtxCompactConfig(max_tool_output_chars=50, min_saved_tokens=0,
                           keep_turns=2)
    body = "PRIMA RIGA UTILE\n" + ("z" * 400)
    msgs = _msgs(body, body)
    out, rep = compact_tool_outputs(msgs, cfg, max_in=0)
    assert rep["deduped"] == 1
    assert "PRIMA RIGA UTILE" in out[2]["content"]


# --------------------------------------------------------------------- I3
def test_i3_json_fenced_tagliato_strutturalmente():
    cfg = CtxCompactConfig(json_struct_max_items=40, json_struct_head=20,
                           json_struct_tail=5)
    items = [{"n": i, "v": "x" * 20} for i in range(200)]
    fenced = "```json\n" + json.dumps(items) + "\n```"
    out = _json_struct_cut(fenced, cfg)
    assert out is not None
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert parsed[0] == items[0]                    # primi intatti
    assert parsed[-1] == items[-1]                  # ultimi intatti
    assert len(parsed) == 20 + 5 + 1                # head + tail + marker
    assert parsed[20].get("totale") == 200


def test_i3_dict_con_valore_dominante():
    cfg = CtxCompactConfig(json_struct_max_items=40, json_struct_head=20,
                           json_struct_tail=5)
    obj = {"count": 200, "files": [{"i": i} for i in range(200)]}
    out = _json_struct_cut(json.dumps(obj), cfg)
    assert out is not None
    parsed = json.loads(out)
    assert parsed["count"] == 200                   # wrapper conservato
    assert len(parsed["files"]) < 200
    assert parsed["files"][0] == {"i": 0}


def test_i3_json_piccolo_non_tagliato():
    cfg = CtxCompactConfig(json_struct_max_items=40)
    small = json.dumps([{"i": i} for i in range(3)])
    assert _json_struct_cut(small, cfg) is None


# --------------------------------------------------------------------- I4
def test_i4_ledger_requeue_su_errore(monkeypatch):
    import tempfile
    from app.ledger import Ledger

    with tempfile.TemporaryDirectory() as d:
        led = Ledger(d)
        led.record({"a": 1})
        led.record({"a": 2})

        def _boom(_n):
            raise OSError("disco pieno")

        monkeypatch.setattr(led, "_rotate_if_needed", _boom)
        assert led.flush() == 0
        assert [r["a"] for r in led._buf] == [1, 2]   # ordine preservato
        monkeypatch.setattr(led, "_rotate_if_needed", lambda _n: None)
        assert led.flush() == 2
        assert led._buf == []


# --------------------------------------------------------------------- I5
def test_i5_metrics_lru_cap(monkeypatch):
    from app import metrics

    metrics.reset()
    for i in range(600):
        metrics.observe_latency_ms(f"u{i}", 10.0)
    assert len(metrics._latency_sum) == 512
    assert len(metrics._latency_count) == 512
    assert "u0" not in metrics._latency_sum          # i piu' vecchi evicti
    assert "u599" in metrics._latency_sum
    assert "nx_upstream_latency_ms" in metrics.render()


# --------------------------------------------------------------------- I6
def test_i6_reset_request_flags():
    from app.thought_sig import (get_dummy_fill, reset_request_flags,
                                 set_avoid_gemini, set_dummy_fill,
                                 should_avoid_gemini)

    set_avoid_gemini(True)
    set_dummy_fill(True, "SIG")
    assert should_avoid_gemini() is True
    reset_request_flags()
    assert should_avoid_gemini() is False
    assert get_dummy_fill() == (False, "")
