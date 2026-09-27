"""Helper puri della chat: parsing del contenuto, fingerprint di sessione,
usage/token, riepilogo per-request e auto-learn.

Estratti da `app/main.py` (C3, Round 4 Clean Code).

Lo stato runtime condiviso (router, config, policy, forwarder, ...) si legge
da `app.state` (`gw_state.<nome>`), popolato da `app/main.py` all'avvio;
il logger e' quello di main (`nx.main`), cosi' i record restano identici.
"""

import hashlib
import json
import logging
import os
import re

from starlette.requests import Request

from . import journal, metrics
from . import state as gw_state
from .suppressed import report_suppressed
from .forwarder import _client_attribution
from .opencode_gate import (
    client_can_use_opencode_zen,
    client_is_opencode,
    set_allow_opencode_zen,
    set_spoofing_request,
    set_zen_first,
    spoof_enabled,
)

# Stesso logger di app.main: i record (nome "nx.main") restano identici.
log = logging.getLogger("nx.main")


def _text_of(content) -> str:
    """Testo da un `content` OpenAI: stringa o lista di parti multimodali."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                t = part.get("text")
                if isinstance(t, str):
                    out.append(t)
        return "\n".join(out)
    return ""


_ANON_FP_MIN_CHARS = 24


def _anon_session_fingerprint(request: Request, payload: dict) -> str | None:
    """Id di sessione deterministico per client ANONIMI.

    Base = primo messaggio `system` + primo messaggio `user` + `user-agent`.
    Stabile tra i turni della stessa conversazione, distinto tra conversazioni
    diverse; del contenuto viene salvato solo l'hash (nessun testo in chiaro).
    Gated da `policy.anon_session_fingerprint`.
    """

    if not getattr(gw_state.policy, "anon_session_fingerprint", True):
        return None
    if not isinstance(payload, dict):
        return None
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return None
    sys_txt = usr_txt = ""
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system" and not sys_txt:
            sys_txt = _text_of(m.get("content"))
        elif role == "user" and not usr_txt:
            usr_txt = _text_of(m.get("content"))
        if sys_txt and usr_txt:
            break
    # Tronchiamo il system prompt ai primi N char: molti agenti accodano
    # timestamp/contesto variabile che cambierebbe l'hash ad ogni turno.
    _sys_cap = int(getattr(gw_state.policy, "anon_session_fp_system_chars", 768) or 0)
    sys_part = sys_txt.strip()
    if _sys_cap > 0:
        sys_part = sys_part[:_sys_cap]
    ua = (request.headers.get("user-agent") or "").strip()
    basis = "\x1f".join((sys_part, usr_txt.strip()[:4096], ua))
    if len(basis.replace("\x1f", "").strip()) < _ANON_FP_MIN_CHARS:
        return None
    digest = hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:16]
    return "fq_" + digest


def _session_id(request: Request, payload: dict) -> str | None:
    # PRIMA gli header di sessione del client: opencode invia
    # `x-session-affinity` (vedi _opencode_session) con lo stesso valore della
    # sua sessione. Ignorarlo faceva ricadere sticky/cache-holder/SESSION-DEP
    # GUARD sulla fingerprint anonima `fq_...`, instabile (es. dopo una
    # compattazione o un cambio di system prompt): la stessa conversazione
    # finiva per risultare "un'altra sessione" e auto-escludersi i deployment.
    sid = _opencode_session(request)
    if sid:
        return sid
    md = payload.get("metadata") or {}
    sid = payload.get("user") or md.get("session_id")
    if sid:
        return str(sid)
    return _anon_session_fingerprint(request, payload)


def _client_ip(request: Request) -> str:
    """IP del client (rispetta X-Forwarded-For se dietro proxy)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else ""


def _set_opencode_gate(request: Request) -> None:
    """Imposta il gate per-client degli upstream opencode.ai (zen/go).

    Va chiamato PRIMA di qualunque selezione (initial_pick/pick_deployment/
    warm): il router esclude gli upstream opencode.ai quando il client non e'
    opencode e lo spoof (env OPENCODE_SPOOF_HEADERS) e' off. I contesti
    interni (probe/autoprobe/admin/background) non lo impostano e ricadono
    sulla sola env (vedi app/opencode_gate.py).

    Imposta anche il flag "stiamo spoofando" (client non-opencode con spoof
    attivo): in cautela opencode il router tratta gli upstream zen come ultima
    scelta. Un client opencode reale NON e' mai in cautela. La cautela generica
    (probe/background) e' separata: vedi app/caution.py."""
    _attr = _client_attribution(request)
    _oc = client_is_opencode(_attr)
    set_allow_opencode_zen(client_can_use_opencode_zen(_attr))
    set_spoofing_request(spoof_enabled() and not _oc)
    # Client opencode NATIVO: gli zen sono il PRIMO tier (pool propria), cosi'
    # non consuma i deployment condivisi. I non-opencode hanno allow=False
    # (zen esclusi); gli interni non impostano nulla (possono solo sondare).
    set_zen_first(_oc)


def _opencode_session(request: Request) -> str | None:
    """Header di sessione in arrivo dal client (passthrough upstream).

    Priorità:
      1. `x-opencode-session`  — header legacy/esplicito (prevale su tutto);
      2. `x-session-affinity`  — header NATIVO inviato da opencode client
         (verificato via sniffing: opencode 1.18.x NON invia x-opencode-session,
         ma invia x-session-affinity con lo stesso valore del session_id body);
      3. `x-session-id`        — alias alternativo inviato da opencode.

    Il valore è lo stesso che opencode usa per la propria sessione
    (formato `ses_...`): propagandolo in upstream si replica il comportamento
    nativo invece di generare un hash inventato non riconosciuto.
    """
    for name in ("x-opencode-session", "x-session-affinity", "x-session-id"):
        v = (request.headers.get(name) or "").strip()
        if v:
            return v
    return None


# Header x-opencode-* di interesse per replicare il comportamento del client
# opencode verso l'upstream. Utilizzati solo per lo sniffing diagnostico.
_SNIFF_OPENCODE_HEADERS = (
    "x-opencode-session",
    "x-opencode-request",
    "x-opencode-project",
    "x-opencode-client",
    "x-opencode-agent-name",
    "x-opencode-agent-mode",
    "x-session-affinity",
    "x-session-id",
    "user-agent",
)


def _sniff_headers(request: Request, *, logger, body_size: int = 0, session_id: str | None = None) -> None:
    """Log diagnostico degli header in ingresso a /v1/chat/completions.

    Attivo SOLO se l'env SNIFF_HEADERS e' truthy. Scopo: verificare se e con
    quale formato il client opencode invia davvero l'header x-opencode-session
    (e i correlati x-opencode-*), per replicarne il comportamento in
    passthrough/fallback. L'Authorization e' sempre mascherata.
    """
    if not os.environ.get("SNIFF_HEADERS"):
        return
    try:
        picked = {}
        for name in _SNIFF_OPENCODE_HEADERS:
            if name in request.headers:
                picked[name] = request.headers[name]
        auth = request.headers.get("authorization") or ""
        picked["authorization"] = (auth[:8] + "***" + auth[-4:]) if len(auth) > 12 else "***"
        # Tutti gli altri header (mascherando authorization), per non perdere
        # header utili non ancora contemplati nella lista sopra.
        picked["all_headers"] = {
            k: (v[:8] + "***" + v[-4:] if k.lower() == "authorization" else v) for k, v in request.headers.items()
        }
        logger.warning(
            "[sniff] path=%s body_size=%s session_id=%s headers=%s",
            request.url.path,
            body_size,
            session_id,
            json.dumps(picked, ensure_ascii=False, default=str),
        )
    except Exception:
        report_suppressed("chat_helpers._sniff_headers")


def _emit_summary(**f) -> None:
    """Riga [summary] JSON a fine richiesta (osservabilità per-request).

    Campi tipici: ses, req, grp, dep, tries, fb, dur_ms, stream, qc, wd,
    ttfb_ms, usage{prompt_tokens,completion_tokens,total_tokens,cost}.

    STESSA riga alimenta il LEDGER persistente (var/usage_ledger.jsonl):
    una sola fonte di verita' per log e analytics (/admin/insights).
    Il profilo e' deducibile dal gruppo (<prefix><profilo>-...); se manca
    (gruppi cap senza dims) si prova il campo esplicito "profile".
    """

    try:
        if "via" not in f and f.get("dep"):
            d = gw_state.router.config.deployment_by_unique(f["dep"])
            if d:
                base = d.get("api_base", "")
                if "://" in base:
                    base = base.split("://", 1)[1].split("/", 1)[0]
                f["via"] = base
        log.info("[summary] %s", json.dumps(f, ensure_ascii=False, separators=(",", ":"), default=str))
    except Exception:  # mai bloccare la risposta per un log
        report_suppressed("chat_helpers._emit_summary.log")
    try:
        grp = str(f.get("grp") or "")
        prof = f.get("profile")
        if not prof:
            for p in gw_state.config.profiles:  # match esatto sul segmento
                if grp.startswith(gw_state.config.proxy_prefix + p + "-"):
                    prof = p
                    break
        dep_unique = str(f.get("dep") or "")
        model = ""
        d = gw_state.config.deployment_by_unique(dep_unique)
        if d:
            model = d["model"]
        gw_state.LEDGER.record(
            {
                "ses": f.get("ses"),
                "profile": prof,
                "req": f.get("req"),
                "grp": grp or None,
                "dep": dep_unique or None,
                "model": model,
                "tries": f.get("tries"),
                "fb": f.get("fb"),
                "dur_ms": f.get("dur_ms"),
                "stream": f.get("stream", False),
                "qc": f.get("qc", False),
                "wd": f.get("wd"),
                "fr": f.get("fr"),
                "ttfb_ms": f.get("ttfb_ms"),
                "kind": f.get("kind", "chat"),
                "status": f.get("status"),
                "usage": f.get("usage") if isinstance(f.get("usage"), dict) else None,
            },
            pricing=gw_state.policy.pricing,
            upstream_model=model,
        )
    except Exception:  # analytics non deve mai mordere
        report_suppressed("chat_helpers._emit_summary.ledger")


def _cached_tokens_of(u: dict) -> int | None:
    """Estrae i token di prompt serviti dalla cache (formati provider)."""
    if not isinstance(u, dict):
        return None
    ptd = u.get("prompt_tokens_details")
    if isinstance(ptd, dict) and ptd.get("cached_tokens") is not None:
        return int(ptd["cached_tokens"])
    for k in ("cached_tokens", "prompt_cache_hit_tokens"):
        if u.get(k) is not None:
            return int(u[k])
    return None


def _usage_of(data) -> dict | None:
    """Estrae usage/costo da una risposta upstream OpenAI-style (se presente)."""
    if not isinstance(data, dict):
        return None
    u = data.get("usage")
    if not isinstance(u, dict):
        return None
    out = {k: u.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens") if u.get(k) is not None}
    if not out:
        # SystemOne/Jev usa `input_tokens`/`output_tokens` (forma nativa).
        _pt, _ct = u.get("input_tokens"), u.get("output_tokens")
        if _pt is not None or _ct is not None:
            if _pt is not None:
                out["prompt_tokens"] = _pt
            if _ct is not None:
                out["completion_tokens"] = _ct
            out["total_tokens"] = int(_pt or 0) + int(_ct or 0)
    _cached = _cached_tokens_of(u)
    if _cached is not None:
        out["cached_tokens"] = _cached
        if _cached > 0:
            metrics.inc("nx_cache_hit_requests_total", ())
    cost = u.get("cost")
    if isinstance(cost, dict):
        out["cost"] = cost.get("total_cost")
    elif cost is not None:
        out["cost"] = cost
    return out or None


# --------------------------------------------------- auto-learn capacità
def _auto_learn_apply(model: str, cap: str, evidence: str, count: int) -> None:
    """Registra il SUGGERIMENTO (mode=suggest, default) o applica la rimozione
    della membership (mode=auto) per il (modello, capà) colpito."""

    mode = gw_state.router.policy.cap_auto_learn
    if mode == "off":
        return
    if mode == "suggest":
        # righe candidate alla rimozione del token cap (membership CSV)
        try:
            from .admin import membership_removal_candidates as _mrc

            candidates = _mrc(model, cap)
        except Exception:
            candidates = []
        journal.record(
            gw_state.VAR_DIR,
            "cap_learn_suggest",
            {
                "model": model,
                "cap": cap,
                "count": count,
                "evidence": evidence[:200],
                "candidates": candidates,
                "suggested_ops": [
                    {"action": "update", "id": c["id"], "caps": ",".join([t for t in c["caps"] if t != cap])}
                    for c in candidates
                ],
            },
        )
        log.warning(
            "[caps][suggest] %s rifiuta '%s' (%d strike): %d righe "
            "candidate alla rimozione del token (GET /admin/history)",
            model,
            cap,
            count,
            len(candidates),
        )
        return
    try:
        from .admin import remove_cap_for_model

        remove_cap_for_model(model=model, cap=cap, evidence=evidence, count=count)
    except Exception as exc:  # noqa: BLE001
        log.error("[caps][auto-learn] applicazione fallita %s/%s: %s", model, cap, exc)


def _strike_hook(explicit: bool, need=frozenset()):
    """Hook per il forwarder/loop endpoint: attribuisce i rifiuti modalità al
    modello upstream. MAI su richieste esplicite (il client le ha volute), mai
    con routing disattivato; conta solo cap dichiarate dal modello colpito."""


    def hook(model: str, detail: str) -> None:
        pol = gw_state.router.policy
        if explicit or not pol.routing_active() or pol.cap_auto_learn == "off":
            return
        from .forwarder import media_reject_signature

        if not media_reject_signature(detail):
            return
        declared = pol.caps_for(model)
        active = {c for c in need if c != "text" and c != "tools" and c in declared}
        if not active:
            return
        hits = gw_state.router.note_cap_strike(model, active, detail)
        for cap in hits:
            cnt = next((s["count"] for s in gw_state.router.cap_strikes_view() if s["model"] == model and s["cap"] == cap), 0)
            _auto_learn_apply(model, cap, detail, cnt)

    return hook


def _note_fb_refund(router, session_id: str | None, fb: int) -> None:
    """Regalo -go per i fallback attraversati (#50), a risposta consegnata.

    Non tocca la risposta: accredita solo turni `-go` alla sessione (vedi
    `Router.note_request_fallbacks`). Mai sollevare: un log/regalo non deve
    mordere la richiesta."""
    try:
        router.note_request_fallbacks(session_id, fb)
    except Exception:  # noqa: BLE001
        report_suppressed("chat_helpers._note_fb_refund")


def _apply_go_refund(
    router, group: str | None, profile: str | None, turn_go: bool, session_id: str | None = None
) -> tuple[str | None, bool]:
    """Rimborso latenza all'atterraggio: se il turno corrente e' coperto dal
    regalo (`turn_go`) e la richiesta atterra su un dim testo (-Nk), sposta il
    gruppo sul bucket -go del profilo. Ritorna (gruppo, rediretto?).

    Invariante: media/cap, -go/-fallback e i unique espliciti (`__`) NON sono
    toccati (il regex `-\\d+k$` seleziona solo i dim testo)."""
    if not turn_go or not group:
        return group, False
    if not re.search(r"-\d+k$", group):
        return group, False
    go_group = f"{router.config.proxy_prefix}{profile}{router.config.go_suffix}"
    if not router.config.groups.get(go_group):
        return group, False
    try:
        metrics.inc("nx_go_refund_total", ("redirect",))
    except Exception:  # noqa: BLE001
        report_suppressed("chat_helpers._apply_go_refund")
    _st = {}
    if hasattr(router, "go_refund_status"):
        try:
            _st = router.go_refund_status(session_id) or {}
        except Exception:  # noqa: BLE001
            _st = {}
    logging.getLogger("nx.api").info(
        "🎁 [go-refund] atterraggio %s -> %s (turno %d, restano %d)",
        group,
        go_group,
        _st.get("turns", 0),
        _st.get("refund_left", 0),
    )
    return go_group, True
