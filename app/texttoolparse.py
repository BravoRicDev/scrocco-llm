"""Parsing dei tool-call scritti come TESTO (#6 del piano L2).

[IT] Inverso di `fakecall`: se un modello debole scrive la chiamata come testo
nel `content` invece che in `tool_calls`, qui proviamo un parse CONFIDENTE e,
se riesce, il gateway consegna una `tool_calls` strutturata (evitando
l'escalation a -go/-fallback). Se non siamo confidenti si lascia a `fakecall`.

[EN] Inverse of `fakecall`: parse tool-calls emitted as free text and, only
when every confidence check passes, return structured OpenAI tool_calls.
On any ambiguity returns None so the caller can fall back to escalation.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field

log = logging.getLogger("nx.texttoolparse")

DEFAULT_FORMATS: tuple[str, ...] = ("tool_call_json", "function_xml",
                                    "antml", "argkv", "bare_json")


class TextToolcallConfig:
    def __init__(self, enabled: bool = True,
                 require_declared_name: bool = True,
                 allow_formats: tuple[str, ...] | list[str] | None = None,
                 max_bytes: int = 200000,
                 hold_until_close: bool = True,
                 fallback_to_escalation: bool = True):
        self.enabled = bool(enabled)
        self.require_declared_name = bool(require_declared_name)
        self.allow_formats = tuple(allow_formats) if allow_formats else DEFAULT_FORMATS
        self.max_bytes = int(max_bytes)
        self.hold_until_close = bool(hold_until_close)
        self.fallback_to_escalation = bool(fallback_to_escalation)


def create_text_toolcall_config(policy_dict: dict | None = None) -> TextToolcallConfig:
    cfg = TextToolcallConfig()
    if not isinstance(policy_dict, dict):
        return cfg
    blk = policy_dict.get("text_toolcall") or {}
    if not isinstance(blk, dict):
        return cfg
    if "enabled" in blk:
        cfg.enabled = bool(blk["enabled"])
    if "require_declared_name" in blk:
        cfg.require_declared_name = bool(blk["require_declared_name"])
    if blk.get("allow_formats"):
        cfg.allow_formats = tuple(str(x) for x in blk["allow_formats"])
    if "max_bytes" in blk:
        cfg.max_bytes = int(blk["max_bytes"])
    if "hold_until_close" in blk:
        cfg.hold_until_close = bool(blk["hold_until_close"])
    if "fallback_to_escalation" in blk:
        cfg.fallback_to_escalation = bool(blk["fallback_to_escalation"])
    return cfg


def text_config_from_policy(policy) -> TextToolcallConfig:
    if policy is None:
        return TextToolcallConfig()
    return TextToolcallConfig(
        enabled=bool(getattr(policy, "text_toolcall_enabled", True)),
        require_declared_name=bool(
            getattr(policy, "text_toolcall_require_declared_name", True)),
        allow_formats=tuple(
            getattr(policy, "text_toolcall_allow_formats", ()) or ()) or None,
        max_bytes=int(getattr(policy, "text_toolcall_max_bytes", 200000) or 200000),
        hold_until_close=bool(
            getattr(policy, "text_toolcall_hold_until_close", True)),
        fallback_to_escalation=bool(
            getattr(policy, "text_toolcall_fallback_to_escalation", True)),
    )


# --------------------------------------------------------------- helpers
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_FN_XML = re.compile(r"<function\s*=\s*([^>\s]+)\s*>(.*?)</function>", re.S)
_PARAM_XML = re.compile(r"<parameter\s*=\s*([^>\s]+)\s*>(.*?)</parameter>", re.S)
_ANTML_FN = re.compile(
    r"antml:invoke\s+name\s*=\s*\"([^\"]+)\"(.*?)(?:</antml:invoke>|$)", re.S)
_ANTML_PARAM = re.compile(
    r"<parameter\s+name\s*=\s*\"([^\"]+)\"[^>]*>(.*?)</parameter>", re.S)
_TOOLCALL_TAG = re.compile(r"<tool_call>(.*?)</tool_call>", re.S)
_ARGKV = re.compile(
    r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>", re.S)
_NAME_ATTR = re.compile(r"name\s*=\s*[\"']([^\"']+)[\"']")
_JSON_NAME = re.compile(r"[\"']name[\"']\s*:\s*[\"']([^\"']+)[\"']")


def _strip_code_fence(text: str) -> str:
    m = _FENCE.search(text or "")
    return m.group(1).strip() if m else (text or "").strip()


def _declared(tools) -> dict[str, str]:
    names: dict[str, str] = {}
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        n = (fn or {}).get("name")
        if isinstance(n, str) and n.strip():
            names[n.strip().lower()] = n.strip()
    return names


def _coerce_value(raw: str):
    s = (raw or "").strip()
    if s == "":
        return ""
    try:
        return json.loads(s)
    except Exception:
        return raw


def _args(obj):
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, str):
        s = _strip_code_fence(obj)
        try:
            v = json.loads(s)
            return v if isinstance(v, dict) else {"value": v}
        except Exception:
            return {"value": obj}
    return {"value": obj}


def _call(name, arguments):
    if not isinstance(name, str) or not name.strip():
        return None
    args = _args(arguments)
    return {"id": "call_" + uuid.uuid4().hex,
            "type": "function",
            "function": {"name": name.strip(),
                         "arguments": json.dumps(args, ensure_ascii=False)}}


def _obj_to_calls(obj) -> list[dict]:
    out: list[dict] = []
    items = obj
    if isinstance(obj, dict) and isinstance(obj.get("tool_calls"), list):
        items = obj["tool_calls"]
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        fn = it.get("function") if isinstance(it.get("function"), dict) else it
        name = (fn or {}).get("name")
        arguments = (fn or {}).get("arguments")
        if arguments is None:
            arguments = (fn or {}).get("parameters")
        if arguments is None and isinstance((fn or {}).get("input"), dict):
            arguments = fn.get("input")
        c = _call(name, arguments)
        if c:
            out.append(c)
    return out


def _xml_calls(content: str) -> list[dict]:
    calls: list[dict] = []
    for m in _FN_XML.finditer(content):
        name = m.group(1)
        args = {k: _coerce_value(v)
                for k, v in _PARAM_XML.findall(m.group(2))}
        c = _call(name, args)
        if c:
            calls.append(c)
    return calls


def _antml_calls(content: str) -> list[dict]:
    calls: list[dict] = []
    for m in _ANTML_FN.finditer(content):
        name = m.group(1)
        args = {k: _coerce_value(v)
                for k, v in _ANTML_PARAM.findall(m.group(2))}
        c = _call(name, args)
        if c:
            calls.append(c)
    return calls


def _toolcall_json_calls(content: str) -> list[dict]:
    calls: list[dict] = []
    for m in _TOOLCALL_TAG.finditer(content):
        try:
            obj = json.loads(_strip_code_fence(m.group(1)))
        except Exception:
            continue
        calls.extend(_obj_to_calls(obj))
    return calls


def _argkv_calls(content: str) -> list[dict]:
    kvs = _ARGKV.findall(content)
    if not kvs:
        return []
    name = None
    for rx in (_FN_XML,):
        m = rx.search(content)
        if m:
            name = m.group(1)
            break
    if name is None:
        m = _NAME_ATTR.search(content) or _JSON_NAME.search(content)
        if m:
            name = m.group(1)
    if not name:
        return []
    args = {k: _coerce_value(v) for k, v in kvs}
    c = _call(name, args)
    return [c] if c else []


def _bare_json_calls(content: str) -> list[dict]:
    text = _strip_code_fence(content)
    # prova l'intero contenuto, poi la prima graffa/quadra bilanciata grezza
    candidates = [text]
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i = text.find(open_ch)
        j = text.rfind(close_ch)
        if i != -1 and j > i:
            candidates.append(text[i:j + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        calls = _obj_to_calls(obj)
        if calls:
            return calls
    return []


_PARSERS = {
    "function_xml": _xml_calls,
    "antml": _antml_calls,
    "tool_call_json": _toolcall_json_calls,
    "argkv": _argkv_calls,
    "bare_json": _bare_json_calls,
}


def parse_text_toolcalls(content, tools, cfg: TextToolcallConfig | None = None):
    """Ritorna una lista di tool_calls OpenAI se il parse e' confidente, else None."""
    cfg = cfg or TextToolcallConfig()
    if not cfg.enabled or not tools:
        return None
    if not isinstance(content, str) or not content.strip():
        return None
    if len(content) > cfg.max_bytes:
        return None
    declared = _declared(tools)

    found: list[dict] = []
    seen_ids: set[str] = set()
    for fmt in cfg.allow_formats:
        fn = _PARSERS.get(fmt)
        if not fn:
            continue
        try:
            calls = fn(content)
        except Exception:
            calls = []
        for c in calls:
            key = (c["function"]["name"].lower(), c["function"]["arguments"])
            if key in seen_ids:
                continue
            seen_ids.add(key)
            found.append(c)
    if not found:
        return None
    # confidenza: nome non vuoto (gia' garantito) e, se richiesto, dichiarato
    if cfg.require_declared_name and declared:
        for c in found:
            if c["function"]["name"].lower() not in declared:
                log.info("[text-toolcall] nome %r non dichiarato: no parse",
                         c["function"]["name"])
                return None
    # arguments deve essere JSON valido (oggetto)
    for c in found:
        try:
            v = json.loads(c["function"]["arguments"])
        except Exception:
            return None
        if not isinstance(v, dict):
            return None
    return found


def apply_to_message(message: dict, tools, cfg: TextToolcallConfig):
    """Riscrive un messaggio assistant con tool-call testuali. Ritorna info o None."""
    if not isinstance(message, dict) or message.get("tool_calls"):
        return None
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content
                          if isinstance(p, dict) and isinstance(p.get("text"), str))
    calls = parse_text_toolcalls(content, tools, cfg)
    if not calls:
        return None
    message["tool_calls"] = calls
    message["content"] = ""
    info = [{"name": c["function"]["name"],
             "n_args": len(json.loads(c["function"]["arguments"]))} for c in calls]
    return info
