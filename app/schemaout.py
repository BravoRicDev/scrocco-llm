"""Enforcement dell'output strutturato (#5 del piano L2).

[IT] Mosse:
 A) pulizia del content consegnato: se il client chiede JSON (o il content
    SEMBRA un documento JSON) si estrae il JSON puro da fence/prosa e si
    riscrive il content;
 B) validazione JSON Schema con un subset pragmatico (type, required,
    properties, enum, items, additionalProperties, minimum/maximum,
    minLength/maxLength); se fallisce il chiamante ruota/riprova;
 C) iniezione condizionale di `response_format` SOLO per provider in
    allow-list e SOLO se il client non l'ha inviato (default: disattivata,
    allow-list vuota finche' non configurata);
 D) riparazione schema-driven del content riusando coercizioni elementari.

Non tocca mai la history (prompt cache preservata).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("nx.schemaout")

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


@dataclass
class SchemaOutConfig:
    enabled: bool = True
    rewrite_content: bool = True
    strict_schema: bool = False
    repair_content: bool = True
    inject_response_format: bool = False
    allow_providers: tuple = ()


def create_schemaout_config(policy_dict: dict | None = None) -> SchemaOutConfig:
    cfg = SchemaOutConfig()
    if not isinstance(policy_dict, dict):
        return cfg
    blk = policy_dict.get("qc_json") or {}
    if not isinstance(blk, dict):
        return cfg
    for key, attr in (("struct_out_enabled", "enabled"),
                      ("rewrite_content", "rewrite_content"),
                      ("strict_schema", "strict_schema"),
                      ("repair_content", "repair_content"),
                      ("inject_response_format", "inject_response_format")):
        if key in blk:
            setattr(cfg, attr, bool(blk[key]))
    if blk.get("inject_allow_providers") is not None:
        cfg.allow_providers = tuple(
            str(x).lower() for x in blk["inject_allow_providers"])
    return cfg


def schemaout_config_from_policy(policy) -> SchemaOutConfig:
    if policy is None:
        return SchemaOutConfig()
    qc = getattr(policy, "qc_json", None)
    return SchemaOutConfig(
        enabled=bool(getattr(qc, "struct_out_enabled", True)),
        rewrite_content=bool(getattr(qc, "rewrite_content", True)),
        strict_schema=bool(getattr(qc, "strict_schema", False)),
        repair_content=bool(getattr(qc, "repair_content", True)),
        inject_response_format=bool(
            getattr(qc, "inject_response_format", False)),
        allow_providers=tuple(
            str(x).lower() for x in
            (getattr(qc, "inject_allow_providers", ()) or ())),
    )


# ------------------------------------------------------------------ helpers
def _strip_fence(text: str) -> str:
    m = _FENCE.search(text or "")
    return m.group(1).strip() if m else (text or "").strip()


def _looks_json(text: str) -> bool:
    if not isinstance(text, str):
        return False
    s = _strip_fence(text)
    if not s:
        return False
    if s[0] == "{" and s[-1] == "}":
        return True
    if s[0] == "[" and s[-1] == "]":
        return True
    return bool(_FENCE.search(text)) and (s.startswith("{") or s.startswith("["))


def clean_json_content(content):
    """Estrae un documento JSON puro da content (fence/prosa). None se fallisce."""
    if not isinstance(content, str) or not content.strip():
        return None
    s = _strip_fence(content)
    if not (s.startswith("{") or s.startswith("[")):
        # cerca il primo blocco bilanciato
        for i, ch in enumerate(s):
            if ch in "{[":
                s = s[i:]
                break
        else:
            return None
    try:
        obj = json.loads(s)
    except Exception:
        # prova a ritagliare all'ultima graffa/quadra
        for close in ("}", "]"):
            j = s.rfind(close)
            if j != -1:
                try:
                    obj = json.loads(s[:j + 1])
                    break
                except Exception:
                    continue
        else:
            return None
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return None


def _type_ok(value, t: str) -> bool:
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    return True


def validate_schema(obj, schema, path: str = "$") -> str | None:
    """Valida un subset di JSON Schema. Ritorna il motivo o None."""
    if not isinstance(schema, dict):
        return None
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(obj, x) for x in types):
            return f"{path}: tipo atteso {t}, trovato {type(obj).__name__}"
    if "enum" in schema and obj not in schema["enum"]:
        return f"{path}: valore non in enum"
    if isinstance(obj, dict):
        for req in schema.get("required") or []:
            if req not in obj:
                return f"{path}: campo obbligatorio mancante {req!r}"
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key in obj:
                r = validate_schema(obj[key], sub, f"{path}.{key}")
                if r:
                    return r
        if schema.get("additionalProperties") is False:
            extra = [k for k in obj if k not in props]
            if extra:
                return f"{path}: campi extra non ammessi {extra}"
    if isinstance(obj, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(obj):
            r = validate_schema(item, schema["items"], f"{path}[{i}]")
            if r:
                return r
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        if "minimum" in schema and obj < schema["minimum"]:
            return f"{path}: sotto il minimo"
        if "maximum" in schema and obj > schema["maximum"]:
            return f"{path}: sopra il massimo"
    if isinstance(obj, str):
        if "minLength" in schema and len(obj) < schema["minLength"]:
            return f"{path}: stringa troppo corta"
        if "maxLength" in schema and len(obj) > schema["maxLength"]:
            return f"{path}: stringa troppo lunga"
    return None


def _coerce_for_schema(value, schema):
    if not isinstance(schema, dict):
        return value, None
    t = schema.get("type")
    if t == "integer" and isinstance(value, str):
        try:
            return int(value.strip()), "coerce_int"
        except Exception:
            return value, None
    if t == "number" and isinstance(value, str):
        try:
            return float(value.strip()), "coerce_number"
        except Exception:
            return value, None
    if t == "boolean" and isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "false"):
            return low == "true", "coerce_bool"
    if t == "object" and isinstance(value, str):
        try:
            v = json.loads(value)
            if isinstance(v, dict):
                return v, "parse_object"
        except Exception:
            pass
    if t == "array" and isinstance(value, str):
        try:
            v = json.loads(value)
            if isinstance(v, list):
                return v, "parse_array"
        except Exception:
            pass
    if t == "array" and not isinstance(value, list):
        return [value], "wrap_array"
    return value, None


def repair_by_schema(obj, schema):
    """Riparazione elementare schema-driven. Ritorna (nuovo_obj, mosse)."""
    moves: list[str] = []
    if not isinstance(schema, dict):
        return obj, moves
    t = schema.get("type")
    if t == "object" and isinstance(obj, dict):
        props = schema.get("properties") or {}
        req = set(schema.get("required") or [])
        out = {}
        for key, value in obj.items():
            if key in props:
                nv, mv = repair_by_schema(value, props[key])
                if mv:
                    moves.extend(mv)
                if nv is None and key not in req:
                    moves.append("drop_null")
                    continue
                out[key] = nv
            else:
                if schema.get("additionalProperties") is False:
                    moves.append("drop_extra")
                    continue
                out[key] = value
        return out, moves
    if t == "array" and isinstance(obj, list) and isinstance(schema.get("items"), dict):
        items = []
        for item in obj:
            nv, mv = repair_by_schema(item, schema["items"])
            if mv:
                moves.extend(mv)
            items.append(nv)
        return items, moves
    nv, mv = _coerce_for_schema(obj, schema)
    if mv:
        moves.append(mv)
    return nv, moves


def _wants_json(response_format):
    if not isinstance(response_format, dict):
        return None, None
    kind = response_format.get("type")
    if kind == "json_schema":
        js = response_format.get("json_schema") or {}
        return "json_schema", (js.get("schema") if isinstance(js, dict) else None)
    if kind == "json_object":
        return "json_object", None
    return None, None


def _set_content(data, text):
    try:
        data["choices"][0]["message"]["content"] = text
        return True
    except (KeyError, IndexError, TypeError):
        return False


def enforce_response(data, payload, cfg: SchemaOutConfig | None = None) -> dict:
    """Applica A/B/D a una risposta non-streaming (in place). Ritorna un report."""
    cfg = cfg or SchemaOutConfig()
    if not cfg.enabled or not isinstance(data, dict):
        return {"status": "notjson"}
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return {"status": "notjson"}
    if not isinstance(message, dict) or message.get("tool_calls"):
        return {"status": "notjson"}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content
                          if isinstance(p, dict))
    kind, schema = _wants_json((payload or {}).get("response_format"))
    if kind is None and not (cfg.rewrite_content and _looks_json(content)):
        return {"status": "notjson"}
    cleaned = clean_json_content(content)
    if cleaned is None:
        return {"status": "invalid", "reason": "json non estraibile"}
    obj = json.loads(cleaned)
    if kind == "json_schema" and schema and cfg.strict_schema:
        reason = validate_schema(obj, schema)
        if reason:
            if cfg.repair_content:
                repaired, moves = repair_by_schema(obj, schema)
                if not validate_schema(repaired, schema):
                    _set_content(data, json.dumps(repaired, ensure_ascii=False))
                    return {"status": "repaired", "moves": moves}
            return {"status": "invalid", "reason": reason}
    if cleaned != (content if isinstance(content, str) else None):
        if cfg.rewrite_content:
            _set_content(data, cleaned)
            return {"status": "cleaned"}
    return {"status": "ok"}


def _provider_key(dep: dict) -> str:
    prov = (dep or {}).get("provider") or ""
    if prov:
        return str(prov).lower()
    base = (dep or {}).get("api_base") or ""
    try:
        from urllib.parse import urlparse
        return str(urlparse(base).hostname or base).lower()
    except Exception:
        return str(base).lower()


def maybe_inject_response_format(body: dict, dep: dict,
                                 cfg: SchemaOutConfig | None = None) -> bool:
    """C: inietta response_format solo se abilitato, provider in allow-list,
    e il client non l'ha gia' inviato."""
    cfg = cfg or SchemaOutConfig()
    if not cfg.inject_response_format or not isinstance(body, dict):
        return False
    if "response_format" in body:
        return False
    allow = cfg.allow_providers or ()
    if "*" not in allow and _provider_key(dep) not in allow:
        return False
    body["response_format"] = {"type": "json_object"}
    return True
