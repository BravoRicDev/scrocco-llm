"""Rilevamento e gestione dei tool-call resi COME TESTO (fake tool-call).

[IT] COSA: quando una richiesta dichiara `tools` ma il modello emette il
tool-call come testo nel `content` (es. `<arg_key>`, `<bash>`, `<function=`,
`antml:`...) invece che nel campo strutturato `tool_calls`, il client
(opencode) vede solo testo e chiude il turno: l'agente si blocca.

Questo modulo rileva quel caso e il chiamante (forwarder/main) reagisce
trattando il deployment come FALLITO e scalando DIRETTAMENTE a
`-go`/`-fallback` (dove i modelli gestiscono meglio i tool). Sui gruppi di
escalation il rilevamento e' DISATTIVATO per evitare loop. A catena esaurita
si restituisce un 503 retryable. Nessuna chiamata LLM di riparazione.

[EN] WHAT: detects tool-calls emitted as free text (no structured
`tool_calls`) and lets the caller fail the deployment and escalate straight
to the paid `-go`/`-fallback` buckets. Detection is disabled on the
escalation buckets themselves; on exhaustion a retryable 503 is returned.
"""

from __future__ import annotations

import logging

log = logging.getLogger("nx.fakecall")

# Pattern XML/antml molto specifici: il modello "scrive" la chiamata.
DEFAULT_PATTERNS: tuple[str, ...] = (
    "<arg_key>",
    "<arg_value>",
    "<tool_calls>",
    "</tool_calls>",
    "<tool_call>",
    "</tool_call>",
    "<function=",
    "</function>",
    "<invoke",
    "</invoke>",
    "<parameter=",
    "</parameter>",
    "<bash",
    "</bash",
    "<edit>",
    "</edit>",
    "<write",
    "</write>",
    "antml:",
)

FAKE_CALL_REASON = "tool_call reso come testo"


class FakeCallConfig:
    """Configurazione del rilevamento fake tool-call (da policy + default)."""

    def __init__(self, enabled: bool = True,
                 patterns: tuple[str, ...] | list[str] | None = None,
                 max_escalations: int = 2,
                 stream_hold_max_bytes: int = 4096,
                 stream_hold_timeout_ms: int = 4000):
        self.enabled = bool(enabled)
        self.patterns = tuple(patterns) if patterns else DEFAULT_PATTERNS
        self.max_escalations = int(max_escalations)
        self.stream_hold_max_bytes = int(stream_hold_max_bytes)
        self.stream_hold_timeout_ms = int(stream_hold_timeout_ms)


def create_fake_call_config(policy_dict: dict | None = None) -> FakeCallConfig:
    """Costruisce la config dal blocco `tool_repair.fake_call` di gateway.yaml."""
    cfg = FakeCallConfig()
    if not isinstance(policy_dict, dict):
        return cfg
    tr = policy_dict.get("tool_repair") or {}
    if not isinstance(tr, dict):
        return cfg
    fc = tr.get("fake_call") or {}
    if not isinstance(fc, dict):
        return cfg
    if "enabled" in fc:
        cfg.enabled = bool(fc["enabled"])
    if fc.get("patterns"):
        cfg.patterns = tuple(str(p) for p in fc["patterns"])
    for src, attr in (("max_escalations", "max_escalations"),
                      ("stream_hold_max_bytes", "stream_hold_max_bytes"),
                      ("stream_hold_timeout_ms", "stream_hold_timeout_ms")):
        if fc.get(src) is not None:
            setattr(cfg, attr, int(fc[src]))
    return cfg


def fake_config_from_policy(policy) -> FakeCallConfig:
    """Costruisce la config dai campi gia' parsati in `Policy`."""
    if policy is None:
        return FakeCallConfig()
    return FakeCallConfig(
        enabled=bool(getattr(policy, "tool_repair_fake_call_enabled", True)),
        patterns=tuple(getattr(policy, "tool_repair_fake_call_patterns", ()) or ())
        or DEFAULT_PATTERNS,
        max_escalations=int(
            getattr(policy, "tool_repair_fake_call_max_escalations", 2) or 2),
        stream_hold_max_bytes=int(
            getattr(policy, "tool_repair_fake_call_hold_max_bytes", 4096) or 4096),
        stream_hold_timeout_ms=int(
            getattr(policy, "tool_repair_fake_call_hold_timeout_ms", 4000) or 4000),
    )


def looks_like_fake_tool_call(text, cfg: FakeCallConfig | None = None) -> str | None:
    """Ritorna il pattern matchato se `text` sembra un tool-call testuale."""
    if cfg is not None and not cfg.enabled:
        return None
    if not isinstance(text, str) or not text:
        return None
    patterns = cfg.patterns if cfg is not None else DEFAULT_PATTERNS
    for pat in patterns:
        if pat and pat in text:
            return pat
    return None


def _content_text(message: dict):
    """Estrae il testo da `content` (stringa o lista di parti)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                t = part.get("text")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return None


def message_fake_pattern(data: dict, payload: dict,
                         cfg: FakeCallConfig) -> str | None:
    """Pattern fake rilevato in una risposta NON-streaming, o None.

    Si attiva solo se la richiesta dichiara `tools`, la risposta NON ha
    `tool_calls` strutturati e il content contiene un pattern di tool-call
    testuale.
    """
    if not cfg.enabled or not payload.get("tools"):
        return None
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(message, dict):
        return None
    if message.get("tool_calls"):
        return None
    return looks_like_fake_tool_call(_content_text(message), cfg)


def is_escalation_group(group: str | None, go_suffix: str | None,
                        fallback_suffix: str | None) -> bool:
    """True se il gruppo e' un bucket di escalation `-go`/`-fallback`."""
    g = group or ""
    return bool((go_suffix and g.endswith(go_suffix))
                or (fallback_suffix and g.endswith(fallback_suffix)))
