"""Cache delle thought_signature di Google Gemini 3 (sidecar).

Gemini 3 allega un blob opaco `thought_signature` a ogni tool_call; i client
OpenAI-compatibili (opencode & co.) lo scartano, quindi al replay Google
risponde 400 INVALID_ARGUMENT. Il gateway fa da sidecar: cattura le firme
dalle risposte Google e le re-inietta nelle richieste di replay dirette a
Google, mappandole per tool_call id.
"""
import json
import logging
import os
import threading
import time

log = logging.getLogger("nx.thotsig")


THOUGHT_SIG_FILE = os.environ.get("THOUGHT_SIG_FILE", "var/thought_sigs.json")


def is_google_base(api_base: str) -> bool:
    """True se l'api_base e' un endpoint Google Generative Language."""
    return "generativelanguage.googleapis.com" in (api_base or "")


def is_openrouter_base(api_base: str) -> bool:
    """True se l'api_base e' OpenRouter (proxy che serve modelli Gemini)."""
    return "openrouter.ai" in (api_base or "")


def is_gemini_model(model: str) -> bool:
    """True se il nome del modello indica un modello Google Gemini."""
    if not model:
        return False
    low = model.lower()
    return "gemini" in low


def is_gemini_deployment(dep: dict) -> bool:
    """True se il deployment richiede thought_signature (Gemini diretto o via proxy OpenRouter)."""
    api_base = dep.get("api_base", "")
    # Google diretto
    if is_google_base(api_base):
        return True
    # OpenRouter che serve modelli Gemini
    if is_openrouter_base(api_base) and is_gemini_model(dep.get("model", "")):
        return True
    return False


def extract_signatures(msg: dict) -> dict[str, str]:
    """Estrae {tool_call_id: thought_signature} dai tool_calls di un messaggio
    (assistant/delta). Ignora tool_calls senza id o senza firma."""
    out: dict[str, str] = {}
    if not isinstance(msg, dict):
        return out
    for tc in (msg.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        tcid = tc.get("id")
        if not tcid:
            continue
        g = ((tc.get("extra_content") or {}).get("google") or {})
        sig = g.get("thought_signature") if isinstance(g, dict) else None
        if sig:
            out[str(tcid)] = str(sig)
    return out


def has_unsigned_tool_calls(messages) -> bool:
    """True se la history contiene assistant tool_calls che NON hanno firma
    (ne' inline, ne' risolvibile dalla cache).

    Gemini 3 pretende il blob `thought_signature` su OGNI functionCall
    rigiocato: se la conversazione e' passata per altri modelli (rotazione),
    i loro tool_call non hanno firma -> il replay su Gemini riceve un 400
    INVALID_ARGUMENT. In quel caso Gemini va ESCLUSO dal routing per questa
    richiesta (nessun modo di sintetizzare la firma: non e' nostra).
    """
    for m in (messages or []):
        if not (isinstance(m, dict) and m.get("role") == "assistant"
                and m.get("tool_calls")):
            continue
        for tc in m["tool_calls"]:
            if not isinstance(tc, dict):
                continue
            tcid = tc.get("id")
            g = ((tc.get("extra_content") or {}).get("google") or {})
            if isinstance(g, dict) and g.get("thought_signature"):
                continue                      # gia' firmata inline (client ok)
            if tcid and THOUGHT_SIGS.get(str(tcid)):
                continue                      # risolvibile dalla cache
            return True                       # non firmata e irresolvibile
    return False


class ThoughtSignatureCache:
    """Mappa tool_call_id -> thought_signature con TTL e cap di dimensione."""

    def __init__(self, max_size: int = 10000, ttl_sec: float = 86400.0):
        self._map: dict[str, tuple[str, float]] = {}
        self._max = max(1, int(max_size))
        self._ttl = float(ttl_sec)
        self._lock = threading.Lock()

    def store(self, tool_call_id: str, signature: str) -> None:
        if not tool_call_id or not signature:
            return
        now = time.time()
        with self._lock:
            self._map[str(tool_call_id)] = (str(signature), now)
            if len(self._map) > self._max:
                self._evict_locked(now)

    def store_many(self, pairs) -> None:
        for k, v in (pairs or {}).items():
            self.store(k, v)

    def get(self, tool_call_id: str) -> str | None:
        if not tool_call_id:
            return None
        now = time.time()
        with self._lock:
            ent = self._map.get(str(tool_call_id))
            if ent is None:
                return None
            sig, ts = ent
            if now - ts > self._ttl:
                self._map.pop(str(tool_call_id), None)
                return None
            return sig

    def _evict_locked(self, now: float) -> None:
        # 1) scarta scaduti; 2) se ancora sopra il cap, scarta i piu' vecchi
        dead = [k for k, (_s, ts) in self._map.items() if now - ts > self._ttl]
        for k in dead:
            self._map.pop(k, None)
        overflow = len(self._map) - self._max
        if overflow > 0:
            oldest = sorted(self._map.items(), key=lambda kv: kv[1][1])[:overflow]
            for k, _ in oldest:
                self._map.pop(k, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._map)

    # --- Persistenza ----------------------------------------------------------
    def dump(self) -> dict:
        """Snapshot serializzabile per salvataggio su disco."""
        now = time.time()
        with self._lock:
            # salva solo entry non scadute
            return {
                k: {"sig": v[0], "ts": v[1]}
                for k, v in self._map.items()
                if now - v[1] <= self._ttl
            }

    def load(self, data: dict) -> None:
        """Carica snapshot da disco, scartando entry scadute."""
        if not isinstance(data, dict):
            return
        now = time.time()
        with self._lock:
            self._map = {
                k: (v["sig"], v["ts"])
                for k, v in data.items()
                if isinstance(v, dict) and "sig" in v and "ts" in v
                and now - v["ts"] <= self._ttl
            }
            # cap size
            if len(self._map) > self._max:
                oldest = sorted(self._map.items(), key=lambda kv: kv[1][1])
                self._map = dict(oldest[-self._max:])


# Singleton di processo
THOUGHT_SIGS = ThoughtSignatureCache()
