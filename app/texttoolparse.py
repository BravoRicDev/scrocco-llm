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

DEFAULT_FORMATS: tuple[str, ...] = ("tool_call_json", "nemotron",
                                    "function_xml", "antml", "argkv",
                                    "toolid", "bare_json")


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
_TOOLCALL_TAG = re.compile(
    r"(?:<tool_call>|<\|tool_call>)(.*?)(?:</tool_call>|<tool_call\|>)", re.S)
# Formato NATIVO Nemotron/Ling: `<|tool_call>call:NAME{key:<|"|>v<|"|>}<tool_call|>`
_QUOTE_TOK = re.escape('<|"|>')
_NEMOTRON_CALL = re.compile(
    r"<\|tool_call>\s*call:\s*([A-Za-z0-9_.:\-]+)\s*\{(.*?)\}\s*"
    r"(?:<tool_call\|>|</tool_call>|<\|tool_call>|$)", re.S)
_NEMOTRON_PARAM = re.compile(
    r"([A-Za-z0-9_\-]+)\s*:\s*(?:" + _QUOTE_TOK + r"(.*?)" + _QUOTE_TOK
    + r"|([^,}]+))", re.S)
_ARGKV = re.compile(
    r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>", re.S)
_NAME_ATTR = re.compile(r"name\s*=\s*[\"']([^\"']+)[\"']")
_JSON_NAME = re.compile(r"[\"']name[\"']\s*:\s*[\"']([^\"']+)[\"']")
# Formato Anthropic-like reso come TESTO da alcuni modelli deboli (es.
# ling-3.0): `<tool_call_id>ID</tool_call_id>` (opzionale) +
# `<tool_call_type>NAME</tool_call_type>` + `<tool_input>{json}</tool_input>`.
_TOOLID_BLOCK = re.compile(
    r"(?:<tool_call_id>\s*(?P<id>.*?)\s*</tool_call_id>\s*)?"
    r"<tool_call_type>\s*(?P<name>.*?)\s*</tool_call_type>\s*"
    r"<tool_input>\s*(?P<input>.*?)\s*</tool_input>", re.S)


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
    # Envelope compatto {"tool": NAME, "args"/"arguments": {...}} (usato da
    # alcuni client/agenti, es. pi): NON e' la forma OpenAI, quindi va
    # riconosciuto esplicitamente altrimenti la tool-call resa come testo
    # resta tale e l'agente perde il meccanismo di tool-call.
    if isinstance(obj, dict) and "function" not in obj \
            and isinstance(obj.get("tool"), str):
        name = obj.get("tool")
        args = obj.get("args")
        if args is None:
            args = obj.get("arguments")
        if args is None:
            args = obj.get("input")
        c = _call(name, args)
        if c:
            return [c]
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


def _nemotron_calls(content: str) -> list[dict]:
    """Formato nativo Nemotron/Ling: `<|tool_call>call:NAME{...}<tool_call|>`.

    Normalizza anche il nome: opencode/Nemotron a volte lo espongono come
    `tool_<id>_<nome>` (id interno del client) -> si recupera `<nome>`.
    """
    calls: list[dict] = []
    for m in _NEMOTRON_CALL.finditer(content):
        name = re.sub(r"^tool_[A-Za-z0-9]+_", "", m.group(1).strip())
        args = {}
        for k, qv, bv in _NEMOTRON_PARAM.findall(m.group(2)):
            args[k] = _coerce_value(qv if qv is not None else bv)
        c = _call(name, args)
        if c:
            calls.append(c)
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


def _toolid_calls(content: str) -> list[dict]:
    """Formato Anthropic-like reso come testo (es. ling-3.0):
    `<tool_call_id>ID</tool_call_id>` + `<tool_call_type>NAME</tool_call_type>`
    + `<tool_input>{json}</tool_input>`. Il tool e' VALIDO: va riparato in un
    vero `tool_calls`, non eliminato."""
    calls: list[dict] = []
    for m in _TOOLID_BLOCK.finditer(content or ""):
        name = (m.group("name") or "").strip()
        raw = (m.group("input") or "").strip()
        if not name or not raw:
            continue
        try:
            args = json.loads(_strip_code_fence(raw))
        except Exception:
            continue
        if not isinstance(args, dict):
            continue
        c = _call(name, args)
        if not c:
            continue
        _id = (m.group("id") or "").strip()
        if _id:
            c["id"] = _id
        calls.append(c)
    return calls


def strip_toolid_markup(content: str) -> str:
    """Rimuove SOLO i blocchi `<tool_call_id>/<tool_call_type>/<tool_input>`
    (opzione A: il testo residuo, es. `<goal .../>`, resta al client)."""
    if not isinstance(content, str) or not content:
        return content
    return _TOOLID_BLOCK.sub("", content)


_PARSERS = {
    "function_xml": _xml_calls,
    "antml": _antml_calls,
    "tool_call_json": _toolcall_json_calls,
    "nemotron": _nemotron_calls,
    "argkv": _argkv_calls,
    "toolid": _toolid_calls,
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


def apply_to_message(message: dict, tools, cfg: TextToolcallConfig,
                     preserve_residual: bool = False):
    """Riscrive un messaggio assistant con tool-call testuali. Ritorna info o None.

    `preserve_residual=True` (opzione A): NON azzera il content, ma rimuove
    SOLO il markup del tool-call riparato, lasciando il testo residuo (es. i
    tag `<goal .../>`/`<goal_status .../>` del goal plugin)."""
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
    if preserve_residual and isinstance(content, str):
        message["content"] = strip_toolid_markup(content).strip()
    else:
        message["content"] = ""
    info = [{"name": c["function"]["name"],
             "n_args": len(json.loads(c["function"]["arguments"]))} for c in calls]
    return info


# ------------------------------------------------ truncated tool-call salvage
# Tag tool-call come TESTTO che un modello debole puo' emettere (e troncare):
# coppie (apertura, chiusura). La chiusura mancante = risposta troncata/finta.
_TOOLCALL_PAIRS: tuple[tuple[str, str], ...] = (
    ("<tool_call>", "</tool_call>"),
    ("<|tool_call>", "<tool_call|>"),
    ("<function_call>", "</function_call>"),
    ("<tool_calls>", "</tool_calls>"),
    ("<function_calls>", "</function_calls>"),
    ("<function=", "</function>"),
    ("antml:invoke", "</antml:invoke>"),
    ("<invoke", "</invoke>"),
)

# Prefissi parziali: se il testo TERMINA con uno di questi (>=3 char) il tag
# potrebbe ancora formarsi nel chunk successivo -> si trattiene la coda.
_PARTIAL_OPENERS: tuple[str, ...] = (
    "<tool_call>", "<function_call>", "<tool_calls>", "<function_calls>",
    "<function=", "antml:invoke", "</function>", "<invoke", "<|tool_call>",
)


@dataclass
class TruncationConfig:
    """Config per la gestione dei tool-call troncati (policy toolcall_truncation)."""
    enabled: bool = True
    cooldown_sec: int = 30
    holdback: bool = True


def truncation_config_from_policy(policy) -> TruncationConfig:
    if policy is None:
        return TruncationConfig()
    return TruncationConfig(
        enabled=bool(getattr(policy, "toolcall_truncation_enabled", True)),
        cooldown_sec=int(getattr(policy, "toolcall_truncation_cooldown_sec", 30) or 30),
        holdback=bool(getattr(policy, "toolcall_truncation_holdback", True)),
    )


def unclosed_toolcall_index(text) -> int:
    """Indice dell'ULTIMO tag tool-call aperto e mai chiuso, o -1 se nessuno."""
    if not isinstance(text, str) or not text:
        return -1
    best = -1
    for op, cl in _TOOLCALL_PAIRS:
        oi = text.rfind(op)
        if oi != -1 and text.find(cl, oi) == -1 and oi > best:
            best = oi
    return best


def has_unclosed_toolcall(text) -> bool:
    """True se `text` contiene un tag tool-call APERTO senza chiusura dopo.

    Solo l'ULTIMA apertura per coppia conta: se un tag precedente e' chiuso ma
    l'ultimo e' aperto, la risposta e' troncata.
    """
    return unclosed_toolcall_index(text) != -1


def partial_opener_at_end(text) -> bool:
    """True se il testo termina con un prefisso (>=3 char) di un tag di apertura.

    Serve a trattenere l'ultimo frammento quando un tag puo' essere spezzato
    tra due chunk (es. "...<tool" + "_call>...").
    """
    if not isinstance(text, str) or not text:
        return False
    t = text.lower()
    for op in _PARTIAL_OPENERS:
        low = op.lower()
        lo = min(len(low), len(t))
        for k in range(3, lo + 1):
            if t.endswith(low[:k]):
                return True
    return False


def _fuzzy_declared(low: str, declared: dict[str, str]) -> str | None:
    """Match approssimato di un nome (anche parziale/troncato) sui tool dichiarati."""
    if not low:
        return None
    for dlow, dorig in declared.items():
        if low == dlow or dlow.startswith(low) or low.startswith(dlow):
            return dorig
    return None


def _salvage_json_body(body: str, tools, cfg: TextToolcallConfig):
    body = _strip_code_fence(body).strip()
    if not body:
        return None
    obj = None
    try:
        obj = json.loads(body)
    except Exception:
        # JSON troncato: chiudilo con il repair aggressive (close_truncated_json)
        try:
            from .toolrepair import ToolRepairConfig, repair_arguments
            repaired, _changed, _moves = repair_arguments(
                body, "aggressive", ToolRepairConfig())
            obj = json.loads(repaired)
        except Exception:
            return None
    calls = _obj_to_calls(obj)
    if not calls:
        return None
    declared = _declared(tools)
    for c in calls:
        name = c["function"]["name"]
        if cfg.require_declared_name and declared and name.strip().lower() not in declared:
            match = _fuzzy_declared(name.strip().lower(), declared)
            if not match:
                log.info("[truncation] nome %r non dichiarato: no salvage", name)
                return None
            c["function"]["name"] = match
        try:
            v = json.loads(c["function"]["arguments"])
        except Exception:
            return None
        if not isinstance(v, dict):
            return None
    return calls


def salvage_truncated_toolcall(text, tools, cfg: TextToolcallConfig | None = None):
    """Estrae una tool-call da un tag APERTO non chiuso. None se non confidente.

    Solo per corpi JSON (`<tool_call>{...}`, `<function_call>{...}`): completa
    il JSON troncato e valida il nome contro i tool dichiarati (fuzzy match).
    I formati XML/antml troncati non sono salvabili -> None (il chiamante ruota).
    """
    cfg = cfg or TextToolcallConfig()
    if not cfg.enabled or not tools:
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    best_pos = unclosed_toolcall_index(text)
    if best_pos < 0:
        return None
    op = next((o for o, _c in _TOOLCALL_PAIRS if text.startswith(o, best_pos)),
              None)
    if op not in ("<tool_call>", "<function_call>",
                  "<tool_calls>", "<function_calls>"):
        return None
    body = text[best_pos + len(op):]
    return _salvage_json_body(body, tools, cfg)
