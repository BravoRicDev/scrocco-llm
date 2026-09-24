"""Regressione: il QC sotto HOLD deve validare gli ARGOMENTI di una tool-call
ASSEMBLATI (frammenti uniti per `index`), non i singoli delta.

Bug reale (cubotto, session fq_bf0dc7ed1b1fa70b, rid 7392a614ea40): gli
argomenti di `mcp__bravoric_ssh` arrivavano in ~80 delta; il blocco QC li
accodava come 80 tool-call diverse, quindi validava solo il primo frammento
`{"` -> "Unterminated string starting at: line 1 column 2 (char 1)" su OGNI
risposta con tools (falso positivo). Gli argomenti VERI (uniti) erano JSON
valido e completi.

I dati usati sono risposte REALI dei deployment salvate dallo sniffer.
"""
from __future__ import annotations

import json
import os

import pytest

import app.main as M
from app.protocols import sse_to_chat_obj
from app.qc import check_response

_SAMPLES = os.path.join(os.path.dirname(__file__), "data",
                        "sniff_samples.json")


def _load():
    with open(_SAMPLES, encoding="utf-8") as f:
        return json.load(f)


def _buggy_qc_tcs(sse: str):
    """Emula il blocco QC attuale: accoda i tool_calls dei delta SENZA unire
    per index."""
    qc_tcs = None
    for o in M._sse_data_objs(sse.encode()):
        for ch in (o.get("choices") or []):
            d = ch.get("delta") if isinstance(ch, dict) else None
            tc = d.get("tool_calls") if isinstance(d, dict) else None
            if tc:
                qc_tcs = (qc_tcs or []) + list(tc)
    return qc_tcs


class _QC:
    enabled = True
    strip_fences = True
    max_attempts = 2


class _Pay:
    def __init__(self, tools):
        self._tools = tools

    def get(self, k, d=None):
        return self._tools if k == "tools" else d


def _tool_samples():
    data = _load()
    return [(r, v) for r, v in data.items() if v.get("had_tool_calls")]


def test_real_samples_are_multi_fragment():
    """Documenta la realta': le risposte con tool-call hanno MOLTI frammenti."""
    samples = _tool_samples()
    assert samples, "nessun campione con tool_calls nei dati reali"
    for rid, v in samples:
        tcs = _buggy_qc_tcs(v["sse"])
        assert len(tcs) > 5, f"{rid}: attesi molti frammenti, trovati {len(tcs)}"


def test_buggy_assembly_is_false_positive():
    """Con l'assemblaggio attuale il QC vede frammenti e segnala un errore
    JSON che NON esiste negli argomenti reali."""
    rid, v = _tool_samples()[0]
    tcs = _buggy_qc_tcs(v["sse"])
    obj = {"choices": [{"message": {"content": "", "tool_calls": tcs}}]}
    reason = check_response(obj, _Pay(tools=[{"type": "function"}]), _QC())
    assert reason is not None
    assert reason.startswith("tool_calls.")


def test_merged_assembly_is_valid():
    """Gli argomenti VERI (uniti da `sse_to_chat_obj`) sono JSON valido: il QC
    non deve segnalare nulla."""
    for rid, v in _tool_samples():
        obj = sse_to_chat_obj([v["sse"].encode()])
        msg = obj["choices"][0]["message"]
        for tc in msg.get("tool_calls") or []:
            args = (tc.get("function") or {}).get("arguments")
            if isinstance(args, str) and args.strip():
                json.loads(args)        # non deve sollevare
        assert not check_response(obj, _Pay(tools=[{"type": "function"}]), _QC())


def test_helper_used_by_qc_merges_by_index():
    """L'helper che il fix introduce deve unire i frammenti per index."""
    rid, v = _tool_samples()[0]
    tcs = M._merge_qc_tool_calls(v["sse"].encode())
    # un solo tool-call (index 0), argomenti interi e validi
    assert len(tcs) == 1
    args = (tcs[0].get("function") or {}).get("arguments")
    json.loads(args)
    assert args == (sse_to_chat_obj([v["sse"].encode()])["choices"][0]
                    ["message"]["tool_calls"][0]["function"]["arguments"])
