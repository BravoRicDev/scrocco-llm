"""Verdetti di streaming: errori azionabili, cooldown soft, scarico stream
abbandonati, esaurimento catena, retry-at, payload vuoto, verdetto paracadute.
Estratti verbatim da `app/main.py` (cluster C4, Round 4 Clean Code).
Gli oggetti creati a RUNTIME dentro main.py (`log`, `router`) sono raggiunti
con `import app.main as M` DENTRO il corpo delle funzioni che li usano: a
livello di modulo non esisterebbero ancora, e main.py importa questo modulo.
"""
import time
from fastapi.responses import JSONResponse
from . import metrics
from .suppressed import report_suppressed
from .forwarder import (
    _MODEL_MISSING_RE,
    _PAYLOAD_SCHEMA_RE,
    _THOUGHT_SIG_RE,
)


def _actionable_upstream_error(err) -> bool:
    """True se l'errore e' AZIONABILE dall'agente/utente (auth, credito,
    modello inesistente, thought_signature) e va consegnato col suo status
    vero. Il resto (rete, 5xx, 404 transitori, body d'errore provider)
    -> risposta 'notice' non vuota, cosi' il loop dell'agente non si pianta.

    Il 403 NON e' mai azionabile dal client: un upstream che risponde 403
    sta rifiutando la CHIAVE/deployment (project banned, key disabled,
    permission denied...), non la richiesta. Il client non puo' farci nulla
    -> si ruota; a catena esaurita si consegna un 503 retryable."""
    detail = getattr(err, "detail", "") or ""
    if _THOUGHT_SIG_RE.search(detail) or _MODEL_MISSING_RE.search(detail) or _PAYLOAD_SCHEMA_RE.search(detail):
        return True
    st = getattr(err, "status", None)
    return st in (-401, -402, 401, 402)


def _soft_cd(fail_24h: int = 0) -> int:
    """Secondi di cooldown per un fallimento SOFT dello streaming (vuoto/
    troncato/zero-answer).

    Escalation dolce sui fallimenti RECENTI (finestra 24h): il primo resta il
    watchdog_cooldown_sec fisso (90s); i successivi aggiungono il 10% del
    cooldown "potente" lineare (vedi Router.escalate_cooldown), cosi' un
    deployment che fallisce in continuazione (es. 18 volte/24h) viene escluso
    per minuti/ore invece di essere riesumato ogni 2 minuti. La finestra 24h
    si azzera al cambio giorno (fail_day_key), quindi l'indomani la chiave
    riparte dal cooldown base; e su successo (clear_cooldown) torna subito
    disponibile.
    """
    import app.main as M

    base = int(getattr(M.router.policy.qc_json, "watchdog_cooldown_sec", 90) or 90)
    return int(M.router.escalate_cooldown(base, fail_24h))


async def _discard_stream(gen, pending=None) -> None:
    """Chiude in sicurezza uno stream upstream abbandonato (rotazione pre-byte):
    la lettura in volo NON viene cancellata a meta' frame ma consumata, poi si
    chiude la connessione httpx sottostante."""
    if pending is not None:
        pending.cancel()
        try:
            await pending
        except BaseException:
            pass
    try:
        aclose = getattr(gen, "aclose", None)
        if aclose is not None:
            await aclose()
    except Exception:
        pass


def _exhausted(
    n_tries: int,
    detail: str | None = None,
    prefix_reason: str | None = None,
    trail: list | None = None,
    retry_at_ms: int | None = None,
):
    """Risposta di errore RETRYABLE quando nessun deployment ha prodotto un
    output utile: HTTP 503 + Retry-After. MAI un turno finto verso il client —
    l'agente ritenta (e col routing resiliente/transient il retry di solito
    trova una chiave viva).

    F10: se il prefisso della sessione era MUTATO in questa richiesta
    (identity/prefix), il 503 viene etichettato come breadcrumb: parte dei
    503 su catena fredda sono cache-miss percepiti come "provider morto".

    ATTEMPT TRAIL (P0): ogni hop tenta di dire PERCHE' e' stato scartato
    (classe d'errore onesta, mai testo del provider) cosi' l'operatore non
    deve leggere i log. `retry_at_ms` = prima scadenza utile fra i cooldown
    dei deployment provati (quando ritentare ha senso)."""
    import app.main as M

    lab = prefix_reason if prefix_reason in ("identity", "prefix") else "clean"
    try:
        metrics.inc("nx_chain_503_total", (lab,))
    except Exception:  # noqa: BLE001
        report_suppressed("stream_verdicts._exhausted")
    if lab != "clean":
        M.log.warning(
            "[cache-audit] 503 catena esaurita dopo prefisso MUTATO "
            "(%s): possibile cache-miss percepito come provider morto",
            lab,
        )
    msg = "nessun deployment upstream ha prodotto una risposta dopo %d tentativi" % max(1, int(n_tries or 1))
    if detail:
        msg += " (ultimo: %s)" % str(detail)[:160]
    _tr = list(trail or [])[:10]
    body = {"error": {"message": msg, "type": "upstream_unavailable", "code": "no_healthy_deployment", "attempts": _tr}}
    if retry_at_ms:
        body["error"]["retry_at_ms"] = int(retry_at_ms)
    # Gli header devono raccontare i tentativi REALI. Se il trail e' vuoto ma
    # la catena ha comunque provato n>0 deployment (es. il chiamante non ha
    # propagato il trail), "0" e' una bugia: il client leggeva 0 tentativi.
    _n_att = len(_tr) if _tr else max(1, int(n_tries or 1))
    headers = {"Retry-After": "2", "X-Scrocco-Attempts": str(_n_att)}
    if _tr:
        headers["X-Scrocco-Trail"] = ",".join("%s:%s" % (t.get("dep", "?"), t.get("cls", "?")) for t in _tr)
    return JSONResponse(status_code=503, headers=headers, content=body)


def _retry_at_ms(router, trail: list | None) -> int | None:
    """Prima scadenza UTILE fra i cooldown dei deployment provati, in ms epoch:
    dice al client (e all'operatore) quando ritentare ha senso invece di
    ritentare alla cieca. None se nessun cooldown residuo."""
    try:
        best = None
        for t in trail or []:
            r = router.cooldown_residual(t.get("dep") or "")
            if r and r > 0 and (best is None or r < best):
                best = r
        if best:
            return int((time.time() + best) * 1000)
    except Exception:  # noqa: BLE001
        return None
    return None


def _payload_text_empty(payload: dict) -> bool:
    """True se il payload non porta alcun testo di input utile."""
    try:
        for m in payload.get("messages") or []:
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                return False
            if isinstance(c, list):
                for p in c:
                    if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"].strip():
                        return False
        for k in ("input", "prompt"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return False
        return True
    except Exception:
        return False


def _parachute_verdict(verdict: str, qcp, dep: dict, policy, *, hold: bool = False, has_buffer: bool = False) -> str:
    """Sulla catena PARACADUTE (-go/-fallback, ultimo scaglione del ladder) il
    timeout sul primo contenuto non deve produrre rotazione/503: li' non c'e'
    piu' nessun deployment dietro, quindi si consegna comunque quello che
    arriva. Attivo con qc_json.stream_parachute_no_timeout (default True);
    False ripristina il comportamento legacy (timeout -> rotazione/503).

    Con HOLD attivo la consegna e' SEMPRE bufferizzata (mai byte live, cosi'
    il tool repair hold si applica): si torna 'content' solo se c'e' un buffer
    parziale da consegnare, altrimenti si resta 'timeout' (rotazione/503).
    Senza hold resta il comportamento storico (trasmissione live)."""
    if verdict == "timeout" and getattr(qcp, "stream_parachute_no_timeout", True):
        grp = dep.get("group") or ""
        if grp.endswith(policy.go_suffix) or grp.endswith(policy.fallback_suffix):
            if hold and not has_buffer:
                return "timeout"
            return "content"
    return verdict
