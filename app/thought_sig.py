"""Cache delle thought_signature di Google Gemini 3 (sidecar).

Gemini 3 allega un blob opaco `thought_signature` a ogni tool_call; i client
OpenAI-compatibili (opencode & co.) lo scartano, quindi al replay Google
risponde 400 INVALID_ARGUMENT. Il gateway fa da sidecar: cattura le firme
dalle risposte Google e le re-inietta nelle richieste di replay dirette a
Google, mappandole per tool_call id.
"""
import logging
import threading
import time

log = logging.getLogger("nx.thotsig")


def is_google_base(api_base: str) -> bool:
    """True se l'api_base e' un endpoint Google Generative Language."""
    return "generativelanguage.googleapis.com" in (api_base or "")


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


# Singleton di processo
THOUGHT_SIGS = ThoughtSignatureCache()
