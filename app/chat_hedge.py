"""Gara di hedge sul primo contenuto: A + canarini, vince chi parla prima.

[IT] Estratto da app/main.py senza modifiche di logica (logger "nx.main").

[EN] First-content hedge race (moved out of app/main.py unchanged).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from . import metrics, repairlog
from . import state as gw_state
from .csvlearn import learn_no_thinking, learn_strip_reasoning, learn_thinking_replay
from .effort import max_inflight_effective, slow_race_max_warm_effective, spinta
from .forwarder import maybe_account_quota_cooldown, reasoning_err_kind
from .probes import _probe_late_open, _spawn_probe
from .router import inject_identity
from .sse_utils import _peek_stream
from .stream_verdicts import _discard_stream, _soft_cd
from .suppressed import report_suppressed

log = logging.getLogger("nx.main")


async def _hedge_peek(
    dep,
    gen,
    t_att,
    fc_ms,
    incl_reason,
    min_ch,
    hold_idle,
    hold_maxb,
    *,
    payload,
    profile,
    need,
    scope,
    ctx,
    tried_set,
    attempts,
    requested_group,
    session,
    client_ip,
    attribution,
    hedge_ms,
    _tr_cfg,
    _tct_cfg,
    k: int = 1,
    slow_race_ms: int = 0,
    slow_canary_ms: int = 0,
    fresh_only: bool = False,
    hold: bool = False,
    refill: bool = False,
    zen_only: bool = False,
    out_tokens: int | None = None,
    raced: dict | None = None,
):
    """HEDGE sul primo contenuto (stream, pre-commit).

    A e' gia' aperto; se dopo `hedge_ms` non ha ancora un verdetto si aprono
    fino a `k` canary **nuovi** (tier crescenti, mai bucket pagati, mai sotto
    il floor di dim richiesto; con `fresh_only=True` si escludono i caldi
    della sessione e si preferiscono i meno usati) e corrono tutti. Vince chi
    IMPEGNA contenuto.

    I perdenti NON vengono MAI cancellati (regola del warm-refill): restano
    in volo come PROBE REALI fino alla fine della generazione. Se consegnano
    una risposta piena e pulita entrano nel warm della sessione
    (`note_warm_owner`); altrimenti si scartano senza nessuna punizione. Il
    double-cost e' accettato per costruzione: solo free-dims.

    `refill=True` (warm-refill a cascata): invece dei canary cross-tier si
    lancia UN solo candidato nuovo (libero da qualsiasi sessione, api_key
    diversa, stesso tier prioritario, output assicurato) e la gara parte
    anche se A sta gia' streammando: con il hold il verdetto 'content' di A
    arriva solo a chiusura, quindi un canary che chiude prima e' davvero la
    risposta piu' veloce da consegnare.

    Ritorna i valori di (dep, gen, t_att, verdict, prebuf, pending, meta) del
    vincente."""

    def _peek(g, fcm, fb=None):
        return _peek_stream(
            g,
            fcm,
            incl_reason,
            min_ch,
            hold_until_finish=hold,
            hold_idle_ms=hold_idle,
            hold_max_bytes=hold_maxb,
            first_byte=fb,
        )

    firstA = asyncio.Event() if hold else None
    futA = asyncio.ensure_future(_peek(gen, fc_ms, firstA))
    waiter = asyncio.ensure_future(firstA.wait()) if hold else None
    done, _pending = await asyncio.wait(
        ({futA, waiter} if waiter is not None else {futA}), timeout=max(0.05, hedge_ms / 1000.0)
    )
    if futA in done:
        if waiter is not None:
            waiter.cancel()
        return dep, gen, t_att, *await futA
    # ---- DUE TRIGGER INDIPENDENTI --------------------------------------
    # (1) HEDGE CLASSICO (invariato): se A non ha ancora emesso NIENTE si
    #     aprono i canary classici/di refill; se A sta GIA' streammando lo si
    #     lascia finire (in hold e' la norma: risposta lunga, nessuna gara
    #     inutile). In refill si gareggia comunque (winner solo pre-byte).
    # (2) GARA LENTA: timer indipendente a `slow_race_ms` dall'inizio del
    #     TENTATIVO di A. Se A non ha ancora CONSEGNATO (hold: verdetto solo
    #     a chiusura) apre UN canario (fuori dal tetto per-sessione) e lo
    #     mette in gara. Nessuna penalita' per il "lento": A e i perdenti
    #     restano probe reali.
    _slow_dl = (t_att + slow_race_ms / 1000.0) if slow_race_ms > 0 else None
    # TIMING DEL CANARY separato dal FLAG lento: `slow_canary_ms` apre il
    # canario, `slow_race_ms` marca il dep "lento per la sessione". Con
    # `slow_canary_ms <= 0` il canario resta appeso alla soglia del flag
    # (storico: i due scattano insieme).
    _canary_dl = (t_att + slow_canary_ms / 1000.0) if slow_canary_ms > 0 else _slow_dl
    _a_streaming = bool(waiter is not None and waiter.done())
    # con A gia' in streaming (e fuori refill) NON si aprono canary classici:
    # si arma solo il timer lento.
    _skip_classic = bool(_a_streaming and not refill)
    if _a_streaming and not refill and _slow_dl is None and _canary_dl is None:
        # A ha emesso byte ma non ha chiuso e la gara lenta e' spenta: si
        # aspetta A (comportamento storico).
        return dep, gen, t_att, *await futA
    if waiter is not None:
        waiter.cancel()
    # ---- candidati NUOVI per la gara -----------------------------------
    _W = None
    try:
        if refill:
            _wk = gw_state.router.warm_api_keys(session, profile, requested_group or dep.get("group"))
            _xk = set((raced or {}).get("keys") or ()) | _wk
            _xk.add(str(dep.get("api_key") or ""))
            _ex_uniq = (raced or {}).get("uniq")
            _B = gw_state.router.warm_fill_canary(
                profile,
                dep,
                need,
                ctx,
                out_tokens,
                tried=tried_set,
                requested_group=requested_group,
                exclude_keys=_xk,
                exclude_uniq=_ex_uniq,
                only_zen=False,
            )
            if _B is not None:
                log.info(
                    "[refill] canario %s per %s (order=%s, chiavi warm+in-volo escluse=%d, out=%s)",
                    _B["unique"],
                    dep.get("unique"),
                    _B.get("order"),
                    len(_xk),
                    out_tokens,
                )
            else:
                log.info(
                    "[refill] %s: nessun canario free consegnabile (chiavi escluse=%d)", dep.get("unique"), len(_xk)
                )
            # TERZO canario: la SVEglia. Cerca un dep dormiente da un 429 da
            # ALMENO 1h (regola utente) e prova a rimetterlo caldo.
            if int(k) > 1:
                _exu2 = set(_ex_uniq or ())
                _xk2 = set(_xk)
                if _B is not None:
                    _exu2.add(_B["unique"])
                    _xk2.add(str(_B.get("api_key") or ""))
                try:
                    _age = float(getattr(gw_state.router.policy, "warm_refill_wake_min_cooldown_age_sec", 3600.0) or 3600.0)
                except Exception:
                    _age = 3600.0
                _W = gw_state.router.warm_wake_canary(
                    profile,
                    dep,
                    need,
                    ctx,
                    out_tokens,
                    tried=tried_set,
                    requested_group=requested_group,
                    exclude_keys=_xk2,
                    exclude_uniq=_exu2,
                    only_zen=False,
                    min_age_sec=_age,
                )
                if _W is not None:
                    log.info("[refill] sveglia %s (429 in cooldown da almeno %.0fs)", _W["unique"], _age)
                else:
                    log.info("[refill] nessuna sveglia 429 matura")
            # CANARY ZEN DEDICATO (client opencode nativo senza zen in warm):
            # affianca il canary normale e cerca gli zen in TUTTE le dim del
            # profilo (non solo in quella richiesta, che puo' essere senza zen).
            _Z = None
            if zen_only:
                _exu3 = set(_ex_uniq or ())
                _xk3 = set(_xk)
                for _c in (_B, _W):
                    if _c is not None:
                        _exu3.add(_c["unique"])
                        _xk3.add(str(_c.get("api_key") or ""))
                try:
                    _Z = gw_state.router.warm_fill_canary(
                        profile,
                        dep,
                        need,
                        ctx,
                        out_tokens,
                        tried=tried_set,
                        requested_group=requested_group,
                        exclude_keys=_xk3,
                        exclude_uniq=_exu3,
                        only_zen=True,
                    )
                except Exception:  # noqa: BLE001
                    _Z = None
                if _Z is None:
                    try:
                        _age_z = float(
                            getattr(gw_state.router.policy, "warm_refill_wake_min_cooldown_age_sec", 3600.0) or 3600.0
                        )
                    except Exception:  # noqa: BLE001
                        _age_z = 3600.0
                    try:
                        _Z = gw_state.router.warm_wake_canary(
                            profile,
                            dep,
                            need,
                            ctx,
                            out_tokens,
                            tried=tried_set,
                            requested_group=requested_group,
                            exclude_keys=_xk3,
                            exclude_uniq=_exu3,
                            only_zen=True,
                            min_age_sec=_age_z,
                        )
                    except Exception:  # noqa: BLE001
                        _Z = None
                if _Z is not None:
                    log.info("[refill] zen-wake %s (dim=%s, order=%s)", _Z["unique"], _Z.get("group"), _Z.get("order"))
                else:
                    log.info("[refill] %s: nessun canary zen consegnabile (ctx=%s)", dep.get("unique"), ctx)
            cands = [c for c in (_Z, _B, _W) if c is not None]
        else:
            _excl = set(gw_state.router._sess_deps().get(session, ())) if fresh_only else None
            cands = gw_state.router.hedge_canaries(
                profile,
                dep,
                need,
                ctx,
                tried_set,
                requested_group,
                k=max(1, int(k)),
                exclude=_excl,
                fresh_only=bool(fresh_only),
                out_tokens=out_tokens,
            )
    except Exception:
        cands = []
    if _skip_classic:
        # A gia' in streaming: nessun canario classico, solo il timer lento.
        cands = []
    # TETTO per-sessione: apri solo i canari che stanno nel tetto (gli
    # in-volo contano tutti: refill, legacy, A/loser staccati come probe).
    if refill and cands:
        try:
            # Lettore UNICO: scalato dal ratio superscrocco e tappato dal tetto
            # assoluto. Stessa manopola di chat_stream/forwarder.
            _mx = max_inflight_effective(gw_state.router.policy)
        except Exception:
            _mx = 6
        try:
            _free = max(0, _mx - int(gw_state.router.probes_in_flight(session)))
        except Exception:
            _free = _mx
        cands = cands[:_free] if _free > 0 else []
    if not cands:
        metrics.inc("nx_hedge_total", ("no_canary",))
        log.debug(
            "[hedge] %s: nessun candidato nuovo (%s)", dep.get("unique"), "warm-lento" if fresh_only else "cross-tier"
        )
        if _slow_dl is None and _canary_dl is None:
            return dep, gen, t_att, *await futA

    async def _open_canary(B, wake=False):
        """Apre un canary e ne ritorna il record (o None se non disponibile:
        in tal caso la chiave va in cooldown con le regole di sempre)."""
        _bu = B["unique"]
        tB = time.monotonic()
        p2 = dict(payload)
        inject_identity(p2, B, router=gw_state.router)

        def _hookB(_salvaged, _u=_bu, _m=B.get("model", "")):
            metrics.inc("nx_truncated_toolcall_total", (_u, "salvaged" if _salvaged else "dropped"))
            repairlog.note(
                "salvage_truncated",
                source="hedge",
                outcome="ok" if _salvaged else "fail",
                dep=_u,
                model=_m,
                detail="tag tool-call rotto (canary)",
            )
            gw_state.router.mark_failed(_u, seconds=_tct_cfg.cooldown_sec, reason="truncated_toolcall")

        gw_state.router.note_start(_bu, ctx)
        if refill and raced is not None:
            raced.setdefault("uniq", set()).add(_bu)
            raced.setdefault("keys", set()).add(str(B.get("api_key") or ""))
        genB = None
        try:
            genB = await gw_state.forwarder.stream_response(
                B,
                p2,
                profile=profile or "",
                ctx_est=ctx,
                client_ip=client_ip,
                session=session,
                attribution=attribution,
                tool_repair_config=_tr_cfg,
                truncation_config=_tct_cfg,
                truncation_hook=_hookB,
                rate_hook=lambda u, rl: gw_state.router.note_rate_limit(u, rl),
            )
            if gw_state.router.is_cooled_down(_bu):
                gw_state.router.clear_cooldown(_bu)
            futB = asyncio.ensure_future(_peek(genB, gw_state.router.first_content_deadline_ms(_bu, ctx)))
            with contextlib.suppress(Exception):
                gw_state.router.note_probe_started(session, _bu)
            return {"dep": B, "gen": genB, "t0": tB, "fut": futB, "wake": bool(wake)}
        except asyncio.CancelledError:
            if genB is not None:
                await _discard_stream(genB, None)
            with contextlib.suppress(Exception):
                gw_state.router.note_end(_bu, ctx)
            raise
        except BaseException as exc:
            if genB is not None:
                await _discard_stream(genB, None)
            with contextlib.suppress(Exception):
                gw_state.router.note_end(_bu, ctx)
            # REGE (utente): un errore durante il canary va in cooldown
            # COME AL SOLITO: 429/5xx transitori -> soft cooldown calibrato
            # sui fallimenti 24h (retry_after numerico ha priorita').
            # ECCEZIONE: il rifiuto "replay del reasoning" e' un problema del
            # PAYLOAD (tutte le chiavi del provider lo rifiutano): la chiave
            # e' sana -> nessuna penale (la richiesta principale ripara).
            _rk = reasoning_err_kind(str(exc))
            _qacct = 0
            with contextlib.suppress(Exception):
                _qacct = maybe_account_quota_cooldown(gw_state.router, B, getattr(exc, "status", None), str(exc))
            if _qacct:
                log.info(
                    "[hedge] canary %s: quota dell'account esaurita -> %d chiavi dell'account in pausa fino al reset",
                    _bu,
                    _qacct,
                )
            elif _rk is not None:
                log.info(
                    "[hedge] canary %s: payload della famiglia reasoning (%s) (chiave sana, nessuna penale)", _bu, _rk
                )
                with contextlib.suppress(Exception):
                    if _rk == "needs":
                        learn_thinking_replay(gw_state.router, B.get("model"))
                    elif _rk == "rejects":
                        learn_strip_reasoning(gw_state.router, B.get("model"))
                    elif _rk == "history":
                        learn_no_thinking(gw_state.router, B.get("model"))
            else:
                try:
                    _sec = getattr(exc, "retry_after", None)
                    if not (isinstance(_sec, (int, float)) and _sec > 0):
                        try:
                            _f24 = gw_state.router.stats_for(_bu).fail_count_24h
                        except Exception:
                            _f24 = 0
                        _sec = _soft_cd(_f24)
                    gw_state.router.mark_failed(_bu, seconds=_sec, reason="canary_error")
                except Exception:
                    report_suppressed("main._hedge_peek._open_canary")
            log.info("[hedge] canary %s non disponibile (%s) -> cooldown", _bu, type(exc).__name__)
            return None

    # Apertura dei canari in PARALLELO e NON bloccante: `_open_canary` fa
    # `await forwarder.stream_response` (attende le HEADERS dell'upstream) e
    # con l'apertura sequenziale un provider lento bloccava il loop della
    # gara, rimandando/saltando il timer della gara lenta (e il `break` sul
    # vincitore scavalcava il check del timer). Ora ogni apertura e' un TASK:
    # le risoluzioni vengono raccolte DENTRO il loop, cosi' il timer scatta
    # SEMPRE a `slow_race_ms`.
    canaries: list[dict] = []
    _tasks: dict = {}
    for B in cands:
        _tasks[asyncio.ensure_future(_open_canary(B, wake=bool(_W is not None and B is _W)))] = None
    if not cands:
        if _slow_dl is None and _canary_dl is None:
            metrics.inc("nx_hedge_total", ("no_canary",))
            return dep, gen, t_att, *await futA
    else:
        log.info(
            "[hedge] %s: nessun contenuto dopo %dms -> gara con %s",
            dep.get("unique"),
            hedge_ms,
            ",".join(B.get("unique", "") for B in cands),
        )
    futs: dict = {futA: None}
    for c in canaries:
        futs[c["fut"]] = c
    results: dict = {}
    winner = None
    running = set(futs) | set(_tasks)
    _canary_opened = _canary_dl is None
    _slow_marked = _slow_dl is None
    while running:
        _pending_dls = []
        if not _canary_opened and _canary_dl is not None:
            _pending_dls.append(_canary_dl)
        if not _slow_marked and _slow_dl is not None:
            _pending_dls.append(_slow_dl)
        _to = max(0.0, min(_pending_dls) - time.monotonic()) if _pending_dls else None
        completed, pending = await asyncio.wait(running, timeout=_to, return_when=asyncio.FIRST_COMPLETED)
        # NB: esaminare TUTTI i completati del tick (non solo uno): se piu'
        # canari finiscono insieme, gli altri resterebbero con l'eccezione
        # non recuperata e il loro verdetto andrebbe perso.
        running = set(pending)
        _add: set = set()
        for f in completed:
            if f in _tasks:
                # apertura di un canary conclusa: raccogli il record (None =
                # non disponibile) e metti in gara la sua attesa.
                _tasks.pop(f, None)
                with contextlib.suppress(BaseException):
                    _c = f.result()
                    if _c is not None:
                        canaries.append(_c)
                        futs[_c["fut"]] = _c
                        _add.add(_c["fut"])
                continue
            if f in results:
                continue
            try:
                results[f] = f.result()
            except BaseException:
                results[f] = ("error", [], None, {})
            if results[f][0] == "content":
                winner = f
                break
        running |= _add
        if winner is not None:
            break
        _now_sr = time.monotonic()
        # ---- FLAG LENTO: alla soglia `slow_race_ms` il dep e' lento per la
        # sessione (indipendente dall'esito della gara e dal canary). Per il
        # canary c'e' un timer PROPRIO (`slow_canary_ms`).
        if not _slow_marked and _slow_dl is not None and _now_sr >= _slow_dl:
            _slow_marked = True
            # R3: il dep che ha fatto scattare il timer e' lento per la
            # sessione, indipendentemente dall'esito della gara (anche se poi
            # vince). Auto-pulito al primo successo rapido.
            with contextlib.suppress(Exception):
                gw_state.router.mark_session_slow(session, dep.get("unique"))
        # ---- CANARY LENTO: timer proprio scaduto -> UN canario in piu' ----
        # INDIPENDENTE dal tetto per-sessione e dall'hedge classico: il
        # "lento" non prende nessuna penale (resta probe reale).
        if not _canary_opened and _canary_dl is not None and _now_sr >= _canary_dl:
            _canary_opened = True
            _shown_ms = slow_canary_ms if slow_canary_ms > 0 else slow_race_ms
            log.info(
                "[slow-race] %s in generazione da %.0fs (> %.0fs) -> canario in gara",
                dep.get("unique"),
                _now_sr - t_att,
                _shown_ms / 1000.0,
            )
            # R2: gate — il canary si apre solo se la sessione ha pochi warm
            # (need+ctx+output, prestati inclusi); a warm pieno riempirebbe
            # la lista di altri lenti.
            _allow = True
            try:
                _allow = bool(
                    gw_state.router.slow_race_allowed(session, profile, requested_group, need, ctx, out_tokens, tried_set)
                )
            except Exception:  # noqa: BLE001
                _allow = True
            if not _allow:
                metrics.inc("nx_slow_race_total", ("warm_full",))
                log.info(
                    "[slow-race] %s: warm gia' pieno (>=%s), niente canario",
                    dep.get("unique"),
                    slow_race_max_warm_effective(gw_state.router.policy),
                )
            else:
                metrics.inc("nx_slow_race_total", ("open",))
                # N canari (default 1). `stream_slow_race_canaries` era MORTA:
                # il commento in policy.py prometteva N canari, il codice ne
                # apriva sempre UNO (`k=1` cablato e poi `_lc[0]`). Questa e'
                # la gara STREAM (`_hedge_peek` e' chiamata solo da
                # chat_stream._run_peek), quindi e' qui che la manopola vive.
                # Il canario lento NON concorre al tetto per-sessione, quindi
                # N non e' limitato da `warm_refill_max_inflight`.
                # `hedge_canaries` ritorna fino a k candidati NUOVI (mai A,
                # mai i gia' provati); il filtro sotto ripulisce comunque.
                _ns_k = max(1, spinta(
                    gw_state.router.policy, "stream_slow_race_canaries",
                    1, lo=1))
                _lc: list[dict] = []
                with contextlib.suppress(Exception):
                    _lc = gw_state.router.hedge_canaries(
                        profile,
                        dep,
                        need,
                        ctx,
                        tried_set,
                        requested_group,
                        k=_ns_k,
                        exclude=None,
                        fresh_only=False,
                        out_tokens=out_tokens,
                    )
                _xu = set((raced or {}).get("uniq") or ())
                _xk2 = set((raced or {}).get("keys") or ())
                for _cc in canaries:
                    _xu.add(_cc["dep"]["unique"])
                    _xk2.add(str(_cc["dep"].get("api_key") or ""))
                _lc = [B for B in _lc if B["unique"] not in _xu and str(B.get("api_key") or "") not in _xk2]
                if not _lc:
                    metrics.inc("nx_slow_race_total", ("no_canary",))
                    log.info("[slow-race] %s: nessun canario libero (chiavi/uniq in gara escluse)", dep.get("unique"))
                else:
                    # Apertura NON bloccante anche qui: ogni canario lento
                    # entra in gara appena arrivano le headers (task in coda).
                    for _cc2 in _lc:
                        _t2 = asyncio.ensure_future(_open_canary(_cc2))
                        _tasks[_t2] = None
                        running.add(_t2)
                        if raced is not None:
                            raced.setdefault("uniq", set()).add(_cc2["unique"])
                            raced.setdefault("keys", set()).add(str(_cc2.get("api_key") or ""))
                        log.info("[hedge] slow-race: %s in gara con A (fuori dal tetto)", _cc2["unique"])
    if winner is None:
        # fallback: risolvi prima le aperture ancora in corso, poi attendi
        # tutti i verdetti (A compreso).
        for f in list(running):
            if f in _tasks:
                _tasks.pop(f, None)
                running.discard(f)
                with contextlib.suppress(BaseException):
                    _c = f.result() if f.done() else await f
                    if _c is not None:
                        canaries.append(_c)
                        futs[_c["fut"]] = _c
                        running.add(_c["fut"])
        for f in list(running):
            if f in results:
                continue
            try:
                results[f] = await f
            except BaseException:
                results[f] = ("error", [], None, {})
        winner = futA

    # REGOLA UTENTE: mai bloccare in volo e mai buttare via un canary — le
    # aperture ancora in corso NON vengono annullate: restano in background e,
    # appena arrivano le headers, la loro attesa entra in gara come probe reale
    # (chi consegna va in warm, anche se lento).
    def _handover_late(race):
        for _t in list(_tasks):
            _tasks.pop(_t, None)
            _pt = asyncio.ensure_future(_probe_late_open(_t, session, ctx, hold, race))
            gw_state._PROBE_TASKS.add(_pt)
            _pt.add_done_callback(gw_state._PROBE_TASKS.discard)

    # ---------------------------------------------------------- A vince ----
    if winner is futA:
        if not canaries:
            metrics.inc("nx_hedge_total", ("no_canary",))
        metrics.inc("nx_hedge_total", ("won_a",))
        _race = (dep["unique"], max(0.0, (time.monotonic() - t_att) * 1000.0))
        _handover_late(_race)
        for c in canaries:
            _spawn_probe(
                c["dep"],
                c["gen"],
                c["fut"],
                results.get(c["fut"]),
                session,
                ctx,
                hold,
                wake=bool(c.get("wake")),
                t0=c.get("t0"),
                race=_race,
            )
        _rA = results.get(futA)
        if _rA is None:
            try:
                _rA = await futA
            except BaseException:
                _rA = ("timeout", [], None, {})
        return dep, gen, t_att, *_rA
    # ------------------------------------------------- canary vince ---------
    metrics.inc("nx_hedge_total", ("won_b",))
    w = futs[winner]
    with contextlib.suppress(Exception):
        gw_state.router.note_probe_done(session, w["dep"]["unique"])
    if w.get("wake"):
        with contextlib.suppress(Exception):
            gw_state.router.clear_cooldown(w["dep"]["unique"])  # sveglia riuscita
        log.info("[refill] sveglia riuscita: %s torna caldo (consegna la risposta)", w["dep"]["unique"])
    # A NON viene annullata: finisce la sua risposta in background come probe
    # reale (se consegna pulita entra in warm, altrimenti si scarta).
    _race = (w["dep"]["unique"], max(0.0, (time.monotonic() - w["t0"]) * 1000.0))
    _handover_late(_race)
    _spawn_probe(dep, gen, futA, results.get(futA), session, ctx, hold, t0=t_att, race=_race)
    for c in canaries:
        if c is w:
            continue
        _spawn_probe(
            c["dep"],
            c["gen"],
            c["fut"],
            results.get(c["fut"]),
            session,
            ctx,
            hold,
            wake=bool(c.get("wake")),
            t0=c.get("t0"),
            race=_race,
        )
    attempts.append(w["dep"]["unique"])
    tried_set.add(w["dep"]["unique"])
    log.info(
        "[hedge] vince %s (A=%s e %d altri in volo come probe, non puniti)",
        w["dep"]["unique"],
        dep.get("unique"),
        len(canaries) - 1,
    )
    return w["dep"], w["gen"], w["t0"], *results[winner]
