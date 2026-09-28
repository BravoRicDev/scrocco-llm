"""Media nelle chat: tetto alle immagini inviate e ponte STT per l'audio.

[IT] `_trim_chat_images` limita le immagini inviate all'upstream; il ponte
STT trascrive le parti audio per i modelli che non le accettano. Estratto da
app/main.py senza modifiche di logica (logger "nx.main").

[EN] Chat media helpers: image cap and STT bridge (moved out of app/main.py).
"""
from __future__ import annotations

import logging
import time

from fastapi import Request

from . import metrics, sttchat, sttscrub
from . import state as gw_state
from .auth import AuthResult
from .capabilities import _is_image_part, count_audio_parts
from .chat_helpers import _client_ip, _emit_summary, _opencode_session, _strike_hook
from .forwarder import UpstreamError, _client_attribution, media_reject_signature
from .image_helpers import _profile_of_request
from .suppressed import report_suppressed

log = logging.getLogger("nx.main")


def _trim_chat_images(payload: dict, max_images: int) -> tuple[dict, int]:
    """Tetto alle immagini INVIATE all'upstream, in una copia del payload.

    Tiene le `max_images` piu' RECENTI, dando priorita' alle immagini del turno
    corrente (l'utente): se il turno corrente ne ha piu' del tetto, si tiene la
    parte piu' recente di quelle. Le piu' vecchi restano nella history del
    CLIENT (che non perde nulla) ma non vengono reinviate.

    Restituisce (payload_out, n_rimosse). Con `max_images <= 0` o zero
    immagini non tocca nulla e restituisce l'ORIGINALE (nessuna copia inutile).
    La stima del contesto continua a contarle tutte: restare conservativi
    sull'overflow vale piu' di ottimizzare i token."""
    if not max_images or max_images <= 0:
        return payload, 0
    messages = payload.get("messages") or []
    if not messages:
        return payload, 0
    # indice (msg_index, part_index) di ogni immagine, in ordine di arrivo
    spots: list[tuple[int, int]] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for j, part in enumerate(content):
            if isinstance(part, dict) and _is_image_part(part):
                spots.append((i, j))
    if len(spots) <= max_images:
        return payload, 0
    # da tenere: le ultime N in ordine, con le immagini dell'ULTIMO messaggio
    # (turno corrente) davanti a tutto il resto a parita' di recency.
    last_msg = max((i for i, _j in spots), default=-1)
    current = [s for s in spots if s[0] == last_msg]
    older = [s for s in spots if s[0] != last_msg]
    keep = set()
    for s in reversed(current):  # turno corrente, piu' recente
        if len(keep) >= max_images:
            break
        keep.add(s)
    for s in reversed(older):  # poi il passato, dal piu' recente
        if len(keep) >= max_images:
            break
        keep.add(s)
    drop = set(spots) - keep

    out_msgs: list = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            out_msgs.append(msg)
            continue
        parts = [p for j, p in enumerate(msg["content"]) if (i, j) not in drop]
        if len(parts) == len(msg["content"]):
            out_msgs.append(msg)
            continue
        out_msgs.append({**msg, "content": parts})
    out = dict(payload)
    out["messages"] = out_msgs
    return out, len(drop)


async def _stt_bridge_transcribe(
    request: Request, chunk: bytes, profile: str | None, raw_model: str, session_id: str | None, used: set[str]
) -> str:
    """Trascrive un chunk di audio. Ritorna "" SOLO dopo aver provato TUTTI i
    deployment con la capacita' `stt` del profilo.

    Ordine di tentativi (e' la regola della flotta, non una scelta locale):
      1) free vivi          2) free in cooldown
      3) -go vivi           4) -go in cooldown
      5) -fallback
    Il fallimento si dichiara quando la lista e' esaurita, mai prima: un chunk
    senza trascrizione e' peggio di un tentativo in piu'.

    `used` raccoglie i deployment gia' occupati da altri chunk PARALLELI della
    stessa richiesta: dentro lo stesso tier si preferiscono quelli liberi, ma
    nessuno viene scartato. Il parallelismo e' un'ottimizzazione, la
    trascrizione e' un requisito.
    """
    need = frozenset({"stt"})

    def _tier(d: dict) -> int:
        """0=free, 1=-go, 2=-fallback (dal nome del gruppo)."""
        g = str(d.get("group") or "")
        if g.endswith(gw_state.config.fallback_suffix or "-fallback"):
            return 2
        if g.endswith(gw_state.router.policy.go_suffix or "-go"):
            return 1
        return 0

    # --- candidati: la CATENA CAPABILITY del profilo, che e' la lista
    #     completa dei deployment con cap `stt` (free + -go + -fallback).
    chains = getattr(gw_state.router.config, "chains_cap", {}).get(profile or "") or {}
    uniques = list(chains.get("stt") or ())
    cands: list[dict] = []
    seen_u: set[str] = set()
    for u in uniques:
        if u in seen_u:
            continue
        seen_u.add(u)
        d = gw_state.router.config.deployment_by_unique(u)
        if d is not None:
            cands.append(d)
    if not cands:
        # nessuna catena (routing spento o profilo senza stt): si prova il
        # gruppo -stt direttamente, se esiste.
        grp = None
        if profile:
            for cand in (f"{gw_state.config.proxy_prefix}{profile}-stt", f"{gw_state.config.proxy_prefix}{profile}-stt-fallback"):
                if cand in gw_state.router.config.groups:
                    grp = cand
                    break
        if grp is None:
            return ""
        d = gw_state.router.config.deployment_by_unique(grp) or gw_state.router.pick_deployment(grp, need)
        if d is None:
            return ""
        cands = [d]

    # --- ordine: tier, poi i VIVI prima dei raffreddati, poi i liberi prima
    #     di quelli gia' presi da un altro chunk.
    def _key(d: dict):
        return (_tier(d), 1 if gw_state.router.is_cooled_down(d["unique"]) else 0, 1 if d["unique"] in used else 0)

    cands.sort(key=_key)

    last_err: UpstreamError | None = None
    _sess = _opencode_session(request) or session_id
    _cip = _client_ip(request)
    _attr = _client_attribution(request)
    t_req = time.monotonic()
    tried: set[str] = set()
    for dep in cands:
        cur = dep["unique"]
        if cur in tried:
            continue
        tried.add(cur)
        used.add(cur)
        _was_dormant = gw_state.router.is_cooled_down(cur)
        gw_state.router.note_start(cur)
        t0 = time.monotonic()
        try:
            res = await gw_state.forwarder.transcribe(
                dep,
                {},
                chunk,
                "chunk.ogg",
                "audio/ogg",
                path="transcriptions",
                profile=profile or "",
                client_ip=_cip,
                session=_sess,
                attribution=_attr,
            )
            gw_state.router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                gw_state.router.clear_cooldown(cur)
            metrics.inc("nx_stt_total", (dep["group"], "ok"))
            res, _scrubbed = sttscrub.scrub_payload(res)
            if _scrubbed:
                log.info("[stt-bridge] %s: rimosse %d allucinazioni credit", cur, _scrubbed)
            _emit_summary(
                ses=session_id or "-",
                req=raw_model,
                grp=dep["group"],
                dep=cur,
                tries=len(tried),
                fb=len(tried) - 1,
                dur_ms=int((time.monotonic() - t_req) * 1000),
                stream=False,
                qc=False,
                wd=None,
                usage=None,
                kind="stt",
                path="transcriptions",
            )
            if isinstance(res, dict):
                return str(res.get("text") or "")
            return str(res or "")
        except UpstreamError as err:
            last_err = err
            detail = str(err.detail or "")
            gw_state.router.note_end(cur)
            st = abs(err.status) if err.status else 0
            if -err.status in (400, 403) and media_reject_signature(detail):
                try:
                    _strike_hook(False, need)(dep["model"], detail)
                except Exception:  # noqa: BLE001
                    report_suppressed("main._stt_bridge_transcribe")
            if _was_dormant:
                gw_state.router.mark_failed_double_residual(cur, reason=detail[:80], status=st or None)
            else:
                gw_state.router.mark_failed(cur, seconds=err.retry_after, status=st or None)
            metrics.inc("nx_stt_total", (dep["group"], "retry"))
            # NON si esce: si prosegue col candidato successivo. Solo a lista
            # esaurita si dichiara il fallimento (ritorno "").
    if last_err is not None:
        log.info(
            "[stt-bridge] esauriti %d/%d deployment stt: %s", len(tried), len(cands), (last_err.detail or "")[:100]
        )
    return ""


# --- STT-BRIDGE: audio in chat -> testo, prima di ogni altra logica ---
async def _stt_bridge(
    request: Request, payload: dict, auth: AuthResult, session_id: str | None, model: str, raw_model: str
) -> dict:
    """Trascrive l'audio nella history e lo sostituisce con testo.

    Va eseguita PRIMA di qualunque cosa che guardi la history: l'intercettore
    immagini, il tetto immagini, la stima del contesto e la compattazione. Se
    l'audio diventasse testo dopo, la stima conterrebbe byte che poi non
    esistono piu' e la compattazione taglierebbe a caso.

    Ritorna il payload (eventualmente identico): non solleva mai, perche' un
    audio non trascrivibile non deve portare via la richiesta.
    """
    if not count_audio_parts(payload.get("messages") or []):
        return payload
    # Il confine e' quello della COMPATTAZIONE: una sola nozione di "turno
    # protetto", cosi' trascrizione e compattazione non possono divergere.
    boundary = None
    try:
        from .ctxcompact import ctxcompact_config_from_policy, frontier_boundary  # noqa: PLC0415

        _cc = ctxcompact_config_from_policy(gw_state.router.policy)
        _b = frontier_boundary(payload.get("messages") or [], _cc, max_in=0, boundary_floor=0)
        boundary = _b
    except Exception:  # noqa: BLE001
        boundary = None
    used: set[str] = set()
    _prof = auth.profile or _profile_of_request(model, auth.profile)

    async def _one(chunk: bytes, idx: int) -> str:
        return await _stt_bridge_transcribe(request, chunk, _prof, raw_model, session_id, used)

    try:
        out = await sttchat.resolve_audio_in_payload(
            payload, boundary=boundary, transcript_one=_one, policy=gw_state.router.policy
        )
    except Exception as e:  # noqa: BLE001
        log.warning("[stt-bridge] errore inatteso: %s", e)
        return payload
    if out is not payload:
        _n = count_audio_parts(payload.get("messages") or []) - count_audio_parts(out.get("messages") or [])
        if _n:
            log.info("[stt-bridge] %d parti audio sostituite dal testo", _n)
            metrics.inc("nx_stt_total", (_prof or "-", "chat_bridge"))
    return out
