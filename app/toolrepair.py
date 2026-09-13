"""Riparazione argomenti tool-call (`tool_repair`).

[IT] COSA: normalizza la FORMA degli argomenti dei tool-call emessi
dai modelli, prima che il client li validi ed esegua. Non cambia mai
la semantica nei nomi dei tool. Riduce turni morti, rotazioni inutili
della catena e errori "invalid arguments".

[EN] WHAT: normalizes the shape of tool-call arguments emitted by models,
before the client validates and executes them. Never changes tool
semantics or names. Reduces dead turns, unnecessary chain rotations,
and "invalid arguments" errors.

Decisioni (docs/TOOL_REPAIR_SPEC.md):
  - Default aggressive su tutti i deployment.
  - Colonna CSV tool_repair: vuoto=aggressive, safe, off.
  - Google/Gemini: off di default (is_gemini_deployment).
  - Nessuna canonicalizzazione dei nomi tool (solo validita sintattica).
  - Streaming supportato da subito.
  - Logging sempre attivo.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .thought_sig import is_gemini_deployment

log = logging.getLogger("nx.toolrepair")

# ------------------------------------------------------------------- costanti
TOOL_REPAIR_HEADER = "tool_repair"
"""Nome della colonna CSV per il flag tool_repair."""

VALID_TOOL_REPAIR_VALUES = frozenset({"", "off", "safe", "aggressive"})

# ------------------------------------------------------------------- tipi
@dataclass
class ToolRepairConfig:
    """Configurazione per tool_repair (da policy + CSV)."""
    enabled: bool = True
    default_level: str = "aggressive"  # aggressive | safe
    disable_for_google: bool = True
    max_args_size: int = 100000        # limite dimensione args da tentare
    annotate_reasoning: bool = False

    # Mosse attive per livello (set di mosse)
    safe_moves: frozenset[str] = field(default_factory=lambda: frozenset({
        "reescape_control_chars",
        "extract_markdown_fence",
        "parse_stringified_objects",
        "coerce_stringified_scalars",
        "null_on_optional_field",
        "empty_string_to_object",
        "remove_trailing_comma",
        "python_style_bools",
    }))
    aggressive_moves: frozenset[str] = field(default_factory=lambda: frozenset({
        "reescape_control_chars",
        "extract_markdown_fence",
        "parse_stringified_objects",
        "coerce_stringified_scalars",
        "null_on_optional_field",
        "empty_string_to_object",
        "remove_trailing_comma",
        "python_style_bools",
        "close_truncated_json",
        "remove_extra_fields",
        "null_on_required_with_default",
        "mixed_quoting",
        "collapse_double_serialization",
    }))


# ------------------------------------------------------------------- parsing
def parse_tool_repair_value(raw: str | None) -> str:
    """Parsa il valore della colonna CSV tool_repair.

    Vuoto -> 'aggressive'. Valori validi: 'off', 'safe', 'aggressive'.
    """
    if raw is None:
        return "aggressive"
    v = raw.strip().lower()
    if v == "":
        return "aggressive"
    if v in ("off", "safe", "aggressive"):
        return v
    return "aggressive"  # fallback silenzioso


def resolve_level(deployment: dict, policy: ToolRepairConfig) -> str:
    """Risolve il livello effettivo di tool_repair per un deployment.

    Precedenza (spec sezione 4):
    1. colonna CSV tool_repair del deployment (off/safe/aggressive)
    2. se vuoto -> aggressive, TRANNE Google -> off
    3. policy.enabled=false -> tutto spento
    """
    if not policy.enabled:
        return "off"
    csv_val = deployment.get("tool_repair", "")
    level = parse_tool_repair_value(csv_val)
    if level == "off":
        return "off"
    if level == "aggressive" and csv_val == "" and policy.disable_for_google:
        if is_gemini_deployment(deployment):
            return "off"
    return level


# ------------------------------------------------------------------- riparazioni
def _reescape_control_chars(args_str: str) -> tuple[str, bool]:
    """Re-escape newline/tab/caratteri di controllo non escapati dentro stringhe JSON.

    Se gli argomenti contengono caratteri di controllo letterali (es. newline
    non escapati dentro una stringa JSON), tenta di parsare e re-escaperli.
    """
    try:
        parsed = json.loads(args_str)
        # JSON valido: nulla da fare
        return args_str, False
    except (ValueError, TypeError):
        # JSON non valido per caratteri di controllo: tenta di ripararlo
        # Rimuovi caratteri di controllo letterali dalle stringhe
        cleaned = re.sub(r'(?<!["\\\\])[\x00-\x1f]', lambda m: {
            '\n': '\\n', '\r': '\\r', '\t': '\\t'
        }.get(m.group(0), f'\\u{ord(m.group(0)):04x}'), args_str)
        try:
            json.loads(cleaned)
            return cleaned, True
        except (ValueError, TypeError):
            # Se la pulizia non risolve, restituisci invariato
            return args_str, False


def _extract_markdown_fence(args_str: str) -> tuple[str, bool]:
    """Estrae JSON da fence markdown (```json ... ```)."""
    pattern = r"```(?:json)?\s*\n(.*?)```"
    m = re.search(pattern, args_str, re.DOTALL)
    if m:
        extracted = m.group(1).strip()
        return extracted, True
    return args_str, False


def _parse_stringified_objects(args_str: str) -> tuple[str, bool]:
    """Parsa oggetti/array 'stringificati' (una o più volte)."""
    # Se e' gia' un oggetto/array valido, restituiscilo
    try:
        parsed = json.loads(args_str)
        if isinstance(parsed, (dict, list)):
            return json.dumps(parsed), False  # gia' valido
    except (ValueError, TypeError):
        pass
    # Prova a parsare come stringa che contiene un oggetto/array
    # Caso: "\"{\\\"a\\\": 1}\"" -> deve diventare "{\"a\": 1}"
    # Caso: "'{\"a\": 1}'" -> deve diventare '{"a": 1}'
    stripped = args_str.strip()
    if stripped.startswith('"') and stripped.endswith('"'):
        inner = stripped[1:-1]
        # Rimuovi escape aggiuntivi
        inner = inner.replace('\\\\', '\\').replace('\\"', '"')
        try:
            parsed = json.loads(inner)
            if isinstance(parsed, (dict, list)):
                return json.dumps(parsed), True
        except (ValueError, TypeError):
            pass
    return args_str, False


def _coerce_stringified_scalars(args_str: str) -> tuple[str, bool]:
    """Coerisce scalari stringificati dove lo schema vuole number/integer/boolean."""
    try:
        parsed = json.loads(args_str)
    except (ValueError, TypeError):
        return args_str, False
    if not isinstance(parsed, dict):
        return args_str, False
    changed = False
    for key, val in parsed.items():
        if isinstance(val, str):
            v = val.strip()
            if v.lower() == "true":
                parsed[key] = True
                changed = True
            elif v.lower() == "false":
                parsed[key] = False
                changed = True
            elif v.lower() == "null":
                parsed[key] = None
                changed = True
            elif v == "":
                continue  # lasciamo vuoto per il campo opzionale
            else:
                # Prova a convertire in numero
                try:
                    if "." in v:
                        parsed[key] = float(v)
                    else:
                        parsed[key] = int(v)
                    changed = True
                except ValueError:
                    pass
    if changed:
        return json.dumps(parsed), True
    return args_str, False


def _null_on_optional_field(args_str: str) -> tuple[str, bool]:
    """Rimuove campi null su campi opzionali."""
    try:
        parsed = json.loads(args_str)
    except (ValueError, TypeError):
        return args_str, False
    if not isinstance(parsed, dict):
        return args_str, False
    changed = False
    keys_to_remove = [k for k, v in parsed.items() if v is None]
    if keys_to_remove:
        for k in keys_to_remove:
            del parsed[k]
        changed = True
    if changed:
        return json.dumps(parsed), True
    return args_str, False


def _empty_string_to_object(args_str: str) -> tuple[str, bool]:
    """Sostituisce stringa vuota dove serve oggetto su tool zero-arg -> {}."""
    stripped = args_str.strip()
    if stripped == "" or stripped == '""':
        return "{}", True
    return args_str, False


def _remove_trailing_comma(args_str: str) -> tuple[str, bool]:
    """Rimuove trailing comma prima di } o ]."""
    # Usa regex per rimuovere virgole prima di } o ]
    cleaned = re.sub(r',\s*([}\]])', r'\1', args_str)
    changed = cleaned != args_str
    return cleaned, changed


def _python_style_bools(args_str: str) -> tuple[str, bool]:
    """Converte True/False/None stile Python in true/false/null JSON."""
    cleaned = args_str
    # Sostituisci True -> true, False -> false, None -> null
    # Ma solo come parole intere, non dentro stringhe
    cleaned = re.sub(r'\bTrue\b', 'true', cleaned)
    cleaned = re.sub(r'\bFalse\b', 'false', cleaned)
    cleaned = re.sub(r'\bNone\b', 'null', cleaned)
    changed = cleaned != args_str
    return cleaned, changed


def _close_truncated_json(args_str: str) -> tuple[str, bool]:
    """Chiude JSON troncato (bilanciamento graffe/quadre/stringhe) se ricostruibile."""
    original = args_str
    # Conta graffe e quadre aperte
    open_braces = args_str.count('{') - args_str.count('}')
    open_brackets = args_str.count('[') - args_str.count(']')
    # Conta virgolette non chiuse (semplice approssimazione)
    # Rimuovi virgolette per contare
    in_string = False
    quote_count = 0
    for ch in args_str:
        if ch == '"' and not in_string:
            in_string = True
            quote_count += 1
        elif ch == '"' and in_string:
            in_string = False
            quote_count += 1
    # Se quote_count e' dispari, il JSON e' troncato dentro una stringa
    if quote_count % 2 != 0:
        # Aggiungi una virgoletta finale
        args_str = args_str + '"'
        open_braces = args_str.count('{') - args_str.count('}')
        open_brackets = args_str.count('[') - args_str.count(']')
    # Chiudi quadre PRIMA delle graffe (ordine corretto: [ ] { })
    if open_brackets > 0:
        args_str += ']' * open_brackets
    if open_braces > 0:
        args_str += '}' * open_braces
    # Verifica che sia valido
    try:
        json.loads(args_str)
        # Restituisce True se abbiamo aggiunto caratteri di chiusura
        changed = args_str != original
        return args_str, changed
    except (ValueError, TypeError):
        return args_str, False


def _remove_extra_fields(args_str: str) -> tuple[str, bool]:
    """Rimuove campi extra non previsti dallo schema (placeholder - richiede schema strict)."""
    # Questa funzione richiede il JSON Schema della request per sapere quali
    # campi sono extra. Per ora e' un placeholder che non fa nulla.
    # Il campo e' incluso nel catalogo ma richiede schema strict / additionalProperties:false
    # per essere attivo.
    return args_str, False


def _null_on_required_with_default(args_str: str) -> tuple[str, bool]:
    """Sostituisce null su campo obbligatorio con default esplicito (placeholder)."""
    # Richiede conoscenza dei defaults dello schema. Placeholder.
    return args_str, False


def _mixed_quoting(args_str: str) -> tuple[str, bool]:
    """Converte quoting misto (single -> double) con parser tollerante come ultimo tentativo."""
    # Prova a parsare con single quotes come se fossero double quotes
    # Sostituisci ' con " solo se non dentro una stringa
    # Questo e' un tentativo aggressivo
    try:
        json.loads(args_str)
        return args_str, False  # gia' valido
    except (ValueError, TypeError):
        pass
    # Prova a sostituire single quotes con double quotes
    # Semplice approccio: sostituisci ' con " e prova
    # ATTENZIONE: questo puo' rompere stringhe che contengono apostrofi
    # Per ora, tentiamo solo se il parsing standard fallisce
    # e non ci sono virgolette doppie
    if '"' not in args_str and "'" in args_str:
        cleaned = args_str.replace("'", '"')
        try:
            json.loads(cleaned)
            return cleaned, True
        except (ValueError, TypeError):
            pass
    return args_str, False


def _collapse_double_serialization(args_str: str) -> tuple[str, bool]:
    """Collassa doppie serializzazioni annidate e array-wrapping spurio se univoco."""
    # Caso: "\"{\\\"a\\\": 1}\"" -> deve diventare "{\"a\": 1}"
    # Caso: "[\"{\\\"a\\\": 1}\"]" -> deve diventare "{\"a\": 1}" se e' l'unico elemento
    try:
        parsed = json.loads(args_str)
    except (ValueError, TypeError):
        return args_str, False
    # Se e' una lista con un solo elemento che e' una stringa JSON valida
    if isinstance(parsed, list) and len(parsed) == 1:
        inner = parsed[0]
        if isinstance(inner, str):
            try:
                inner_parsed = json.loads(inner)
                # Se l'inner e' un oggetto/array, collassa
                if isinstance(inner_parsed, (dict, list)):
                    return json.dumps(inner_parsed), True
            except (ValueError, TypeError):
                pass
    # Se e' una stringa che contiene un oggetto/array serializzato
    if isinstance(parsed, str):
        try:
            inner_parsed = json.loads(parsed)
            if isinstance(inner_parsed, (dict, list)):
                return json.dumps(inner_parsed), True
        except (ValueError, TypeError):
            pass
    return args_str, False


# ------------------------------------------------------------------- riparazione principale
def repair_arguments(args_str: str, level: str, config: ToolRepairConfig) -> tuple[str, bool, list[str]]:
    """Tenta di riparare gli argomenti di un tool-call.

    Args:
        args_str: La stringa JSON degli argomenti (potrebbe essere invalida).
        level: Il livello di riparazione ('safe', 'aggressive', 'off').
        config: La configurazione di tool_repair.

    Returns:
        (args_riparati, modificato, lista_mosse_applicate)
    """
    if level == "off":
        return args_str, False, []

    moves = config.aggressive_moves if level == "aggressive" else config.safe_moves
    current = args_str
    applied_moves: list[str] = []
    changed = False

    # Applica le mosse in ordine
    move_order = [
        "extract_markdown_fence",
        "remove_trailing_comma",
        "python_style_bools",
        "empty_string_to_object",
        "reescape_control_chars",
        "parse_stringified_objects",
        "coerce_stringified_scalars",
        "null_on_optional_field",
        "collapse_double_serialization",
        "close_truncated_json",
        "mixed_quoting",
        "remove_extra_fields",
        "null_on_required_with_default",
    ]

    for move_name in move_order:
        if move_name not in moves:
            continue
        if move_name == "extract_markdown_fence":
            result, did_change = _extract_markdown_fence(current)
        elif move_name == "remove_trailing_comma":
            result, did_change = _remove_trailing_comma(current)
        elif move_name == "python_style_bools":
            result, did_change = _python_style_bools(current)
        elif move_name == "empty_string_to_object":
            result, did_change = _empty_string_to_object(current)
        elif move_name == "reescape_control_chars":
            result, did_change = _reescape_control_chars(current)
        elif move_name == "parse_stringified_objects":
            result, did_change = _parse_stringified_objects(current)
        elif move_name == "coerce_stringified_scalars":
            result, did_change = _coerce_stringified_scalars(current)
        elif move_name == "null_on_optional_field":
            result, did_change = _null_on_optional_field(current)
        elif move_name == "collapse_double_serialization":
            result, did_change = _collapse_double_serialization(current)
        elif move_name == "close_truncated_json":
            result, did_change = _close_truncated_json(current)
        elif move_name == "mixed_quoting":
            result, did_change = _mixed_quoting(current)
        elif move_name == "remove_extra_fields":
            result, did_change = _remove_extra_fields(current)
        elif move_name == "null_on_required_with_default":
            result, did_change = _null_on_required_with_default(current)
        else:
            continue

        if did_change:
            current = result
            applied_moves.append(move_name)
            changed = True

    # Verifica che il risultato sia JSON valido
    if changed:
        try:
            json.loads(current)
        except (ValueError, TypeError):
            # Non valido nemmeno dopo le riparazioni
            pass

    return current, changed, applied_moves


# ------------------------------------------------------------------- entry point
def repair_tool_calls(data: dict, payload: dict, dep: dict,
                       config: ToolRepairConfig) -> dict:
    """Ripara gli argomenti dei tool-call nella risposta upstream.

    Modifica data in-place. Ritorna un dict con le informazioni di riparazione.

    Args:
        data: La risposta JSON upstream (modificata in-place).
        payload: La richiesta originale (contiene tools[] se presente).
        dep: Il deployment che ha risposto.
        config: La configurazione di tool_repair.

    Returns:
        Dict con chiavi: repaired (bool), moves (list[str]), level (str),
        tool (str|None), deployment (str|None)
    """
    result = {
        "repaired": False,
        "moves": [],
        "level": "off",
        "tool": None,
        "deployment": dep.get("unique", ""),
    }

    if not payload.get("tools"):
        return result

    level = resolve_level(dep, config)
    result["level"] = level
    if level == "off":
        return result

    msg = data.get("choices", [{}])[0].get("message", {})
    tool_calls = msg.get("tool_calls")
    if not tool_calls:
        return result

    total_moves: list[str] = []
    any_repaired = False

    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function", {})
        args_str = fn.get("arguments")
        if not isinstance(args_str, str):
            continue
        if args_str.strip() == "":
            continue

        # Controlla dimensione
        if len(args_str) > config.max_args_size:
            continue

        # Verifica se e' gia' valido JSON
        try:
            json.loads(args_str)
            continue  # gia' valido, nessuna riparazione necessaria
        except (ValueError, TypeError):
            pass

        # Tenta riparazione
        repaired_args, did_change, moves = repair_arguments(
            args_str, level, config)

        if did_change:
            # Verifica che il risultato sia valido JSON
            try:
                json.loads(repaired_args)
                fn["arguments"] = repaired_args
                total_moves.extend(moves)
                any_repaired = True
                result["tool"] = fn.get("name", "")
            except (ValueError, TypeError):
                # Non riparabile, lascia come e' (QC lo catturera)
                pass

    if any_repaired:
        result["repaired"] = True
        result["moves"] = total_moves
        log.info("[repair] riparati %d tool-call su %s | moves=%s | level=%s",
                 len(tool_calls), dep.get("unique", "?"),
                 total_moves, level)

    return result


# ------------------------------------------------------------------- streaming
class ToolRepairSSEFilter:
    """Trasformatore SSE per la riparazione di tool-call in streaming.

    Bufferizza gli argomenti dei tool-call nei delta, li ripara quando il
    tool-call e' completo (o al finish_reason), e emette i chunk riparati
    nel formato OpenAI compatibile. I byte in arrivo possono essere chunk
    parziali: si mantiene un buffer finche' non si incontra la blank line
    che separa gli eventi SSE.
    """

    def __init__(self, config: ToolRepairConfig, dep: dict):
        self.config = config
        self.dep = dep
        self.level = resolve_level(dep, config)
        self._buffers: dict[int, dict] = {}   # index -> {id, name, arguments}
        self._raw = b""                       # byte in attesa di blank line
        self._done = False
        self._repaired_count = 0
        self._total_moves: list[str] = []
        self._choice_index: int = 0           # choice index dall'upstream

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _find_sep(buf: bytes):
        """Ritorna (indice, lunghezza) del primo separatore di eventi."""
        i = buf.find(b"\n\n")
        j = buf.find(b"\r\n\r\n")
        if i == -1 and j == -1:
            return None
        if i == -1:
            return j, 4
        if j == -1:
            return i, 2
        return (i, 2) if i <= j else (j, 4)

    @staticmethod
    def _data_of(event: bytes) -> str:
        """Estrae il payload `data:` (piu' righe unite da newline)."""
        parts: list[str] = []
        for raw_line in event.split(b"\n"):
            line = raw_line.rstrip(b"\r")
            if line.startswith(b"data:"):
                val = line[5:]
                if val.startswith(b" "):
                    val = val[1:]
                parts.append(val.decode("utf-8", errors="replace"))
        return "\n".join(parts)

    def feed(self, chunk_bytes: bytes) -> list[bytes]:
        """Processa bytes SSE e ritorna i chunk da inoltrare (riparati/originali)."""
        if self.level == "off" or self._done:
            return [chunk_bytes]

        self._raw += chunk_bytes
        output: list[bytes] = []
        while True:
            found = self._find_sep(self._raw)
            if found is None:
                break
            idx, seplen = found
            event = self._raw[:idx]
            self._raw = self._raw[idx + seplen:]
            output.extend(self._process_event(event, b"\n\n"))
        return output

    def finalize(self) -> list[bytes]:
        """Da chiamare a fine stream: processa l'evento residuo e fa flush."""
        output: list[bytes] = []
        if self._raw.strip():
            output.extend(self._process_event(self._raw, b"\n"))
            self._raw = b""
        output.extend(self._flush_buffers())
        # Se il modello non ha emesso [DONE], lo aggiungiamo noi
        if not self._done:
            output.append(b"data: [DONE]\n\n")
            self._done = True
        return output

    def _process_event(self, event: bytes, sep: bytes) -> list[bytes]:
        passthrough = [event + sep]
        data = self._data_of(event)
        if not data:
            return passthrough
        if data.strip() == "[DONE]":
            out = self._flush_buffers()
            self._done = True
            out.extend(passthrough)
            return out
        try:
            obj = json.loads(data)
        except (ValueError, TypeError):
            return passthrough
        choices = obj.get("choices") if isinstance(obj, dict) else None
        if not choices:
            return passthrough
        choice = choices[0] or {}
        # Salva il choice index per usarlo nei chunk emessi
        self._choice_index = choice.get("index", 0)
        delta = choice.get("delta") or {}
        finish_reason = choice.get("finish_reason")

        if "tool_calls" in delta:
            for tc_delta in delta.get("tool_calls") or []:
                idx = tc_delta.get("index", 0)
                tc_obj = self._buffers.setdefault(
                    idx, {"id": "", "name": "", "arguments": ""})
                if tc_delta.get("id"):
                    tc_obj["id"] = tc_delta["id"]
                # In formato OpenAI `name`/`arguments` stanno dentro `function`.
                fn = tc_delta.get("function") or {}
                if fn.get("name"):
                    tc_obj["name"] = fn["name"]
                if fn.get("arguments"):
                    tc_obj["arguments"] += fn["arguments"]
            if finish_reason:
                out = self._flush_buffers()
                finish_event = {
                    "choices": [{
                        "index": self._choice_index,
                        "delta": {},
                        "finish_reason": finish_reason,
                    }],
                }
                out.append(b"data: " + json.dumps(finish_event).encode() + b"\n\n")
                return out
            return []                       # bufferizzato: non emettere ora

        if finish_reason or delta.get("content") or delta.get("reasoning_content"):
            out = self._flush_buffers()
            out.extend(passthrough)
            return out

        return passthrough

    def _flush_buffers(self) -> list[bytes]:
        """Ripara e emette i tool-call bufferizzati (formato OpenAI)."""
        output: list[bytes] = []
        for idx in sorted(self._buffers.keys()):
            tc_obj = self._buffers[idx]
            args = tc_obj.get("arguments") or ""
            name = tc_obj.get("name") or ""
            call_id = tc_obj.get("id") or f"toolcall_{idx}"

            repaired_args = args
            moves: list[str] = []
            try:
                json.loads(args)
            except (ValueError, TypeError):
                candidate, did_change, moves = repair_arguments(
                    args, self.level, self.config)
                if did_change:
                    repaired_args = candidate

            chunk = {
                "id": "chatcmpl-toolrepair",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": self.dep.get("model", ""),
                "choices": [{
                    "index": self._choice_index,
                    "delta": {"tool_calls": [{
                        "index": idx,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": repaired_args},
                    }]},
                    "finish_reason": None,
                }],
            }
            output.append(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self._repaired_count += 1
            self._total_moves.extend(moves)

        self._buffers.clear()
        return output

    @property
    def stats(self) -> dict:
        """Ritorna le statistiche di riparazione."""
        return {
            "repaired_count": self._repaired_count,
            "total_moves": self._total_moves,
            "level": self.level,
        }


# ------------------------------------------------------------------- factory
def create_tool_repair_config(policy_dict: dict | None = None) -> ToolRepairConfig:
    """Crea un ToolRepairConfig dalla policy YAML."""
    if policy_dict is None:
        return ToolRepairConfig()

    tr = policy_dict.get("tool_repair", {})
    if not isinstance(tr, dict):
        return ToolRepairConfig()

    enabled = tr.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.lower() in ("true", "on", "yes", "1")

    default_level = tr.get("default_level", "aggressive")
    if default_level not in ("off", "safe", "aggressive"):
        default_level = "aggressive"

    disable_for_google = tr.get("disable_for_google", True)
    if isinstance(disable_for_google, str):
        disable_for_google = disable_for_google.lower() in ("true", "on", "yes", "1")

    max_args_size = tr.get("max_args_size", 100000)
    try:
        max_args_size = int(max_args_size)
    except (ValueError, TypeError):
        max_args_size = 100000

    annotate_reasoning = tr.get("annotate_reasoning", False)
    if isinstance(annotate_reasoning, str):
        annotate_reasoning = annotate_reasoning.lower() in ("true", "on", "yes", "1")

    return ToolRepairConfig(
        enabled=bool(enabled),
        default_level=default_level,
        disable_for_google=bool(disable_for_google),
        max_args_size=max_args_size,
        annotate_reasoning=bool(annotate_reasoning),
    )
