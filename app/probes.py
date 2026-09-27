"""Helper probe speculativi e wake-sweep (canari e risvegli 429).

Estratti verbatim da `app/main.py` (cluster C5, Round 4 Clean Code).
Gli oggetti creati a RUNTIME dentro main.py (`log`, `router`, `forwarder` al
riga 285, e la funzione `_soft_cd`) sono raggiunti con `import app.main as M`
DENTRO il corpo delle funzioni che li usano: a livello di modulo non esisterebbero
ancora, e main.py importa questo modulo.

`policy` NON e' importata: i suoi due usi nello span sono attributi di `router`
(`router.policy.qc_json`), non il global `policy` di main.py.
"""

import asyncio
import contextlib
import time

from . import autoprobe
from .suppressed import report_suppressed
from . import metrics


async def _drain_probe_tasks() -> int:
    """Cancella e attende i task SPECULATIVI in volo (canari, probe, sveglie).

    Va chiamata PRIMA di forwarder.aclose(): un probe che continua mentre il
    client httpx e' chiuso solleva un'eccezione non gestita, sporca i log
    dello shutdown e puo' far saltare il salvataggio atomico finale.
    Ritorna quanti task ha cancellato."""

    import app.main as M

    _pt = [t for t in list(M._PROBE_TASKS) if not t.done()]
    for t in _pt:
        t.cancel()
    if _pt:
        await asyncio.gather(*_pt, return_exceptions=True)
    return len(_pt)


def _probe_drain_cap_sec() -> float:
    import app.main as M

    try:
        dd = int(getattr(M.router.policy.qc_json, "stream_total_deadline_ms", 180000) or 180000)
    except Exception:
        dd = 180000
    return max(120.0, dd / 1000.0 + 60.0)


async def _consume_probe_stream(gen) -> tuple[bool, bool]:
    """Consuma fino in fondo lo stream di un perdente SENZA interrompere la
    generazione. Ritorna `(delivered, clean)`:

    - `delivered`: ha erogato contenuto reale (nessun errore). Regola utente:
      QUALSIASI canary che consegna qualcosa va in warm, anche se troncato.
    - `clean`: contenuto e chiusura pulita (niente `finish_reason=length`).
    """
    saw_content = saw_err = saw_len = False
    async for ch in gen:
        if isinstance(ch, str):
            ch = ch.encode("utf-8", "ignore")
        if b'"content"' in ch and b'"delta"' in ch:
            saw_content = True
        if b'"error"' in ch:
            saw_err = True
        if b'"finish_reason":"length"' in ch:
            saw_len = True
    delivered = saw_content and not saw_err
    return delivered, (delivered and not saw_len)


def _spawn_probe(
    dep: dict,
    gen,
    fut,
    res,
    session,
    ctx,
    hold: bool = False,
    wake: bool = False,
    t0: float | None = None,
    race: tuple[str, float] | None = None,
) -> None:
    """Stacca un perdente di gara: aspetta il verdetto pendente, consuma lo
    stream fino alla fine (probe reale, MAI cancellato) e, se ha servito, lo
    registra warm. Nota: `hold` e' solo contestuale al log/diagnosi.
    `wake=True`: era la SVEglia di un cooldown 429 -> se risponde lo riporta
    caldo (clear_cooldown), cosi' la capacita' dormiente torna disponibile.

    `t0` = inizio del tentativo di questo probe; `race=(unique_vincente,
    durata_ms_vincente)`: al suo atterraggio confrontiamo il TEMPO DI
    TENTATIVO (regola utente) e, se il probe e' stato piu' veloce, diventa
    lui l'holder della sessione (l'altro viene marcato 'lento per la
    sessione'); se e' piu' lento, viene marcato lento il probe."""

    import app.main as M

    u = dep.get("unique", "?")
    cap = _probe_drain_cap_sec()
    with contextlib.suppress(Exception):
        M.router.note_probe_started(session, u)

    async def _run():
        ok = False
        v = None
        died = False
        r = res
        try:
            if fut is not None and not fut.done():
                try:
                    r = await asyncio.wait_for(fut, timeout=cap)
                except asyncio.CancelledError:
                    raise  # shutdown: non e' colpa upstream
                except BaseException:
                    r = None
                    died = True
            v = r[0] if r else None
            pend = r[2] if r and len(r) > 2 else None
            if pend is not None:
                with contextlib.suppress(BaseException):
                    await pend
            try:
                delivered, _clean = await asyncio.wait_for(_consume_probe_stream(gen), timeout=cap)
            except asyncio.CancelledError:
                raise  # shutdown: nessuna penale
            except BaseException:
                delivered = False
                died = True
            ok = (v == "content") or delivered
            if ok:
                if wake:
                    with contextlib.suppress(Exception):
                        M.router.clear_cooldown(u)  # sveglia riuscita
                M.router.note_warm_owner(session, u)
                metrics.inc("nx_hedge_total", ("probe_ok",))
                # ELEZIONE per TEMPO DI TENTATIVO (regola utente): il piu'
                # veloce diventa holder, indipendentemente da chi ha
                # consegnato prima. Guardia: solo se l'holder attuale e'
                # ancora il vincente della gara (niente esiti piu' recenti).
                if t0 is not None and race is not None:
                    try:
                        _d = (time.monotonic() - t0) * 1000.0
                        _wu, _wd = race
                        if _wd and _d > 0 and u != _wu:
                            if _d < _wd:
                                _cur = (M.router._cache_ok().get(session) or (None, 0.0))[0]
                                if _cur in (None, _wu):
                                    with contextlib.suppress(Exception):
                                        M.router.note_session_success(session, u, latency_ms=_d, ctx_est=ctx)
                                        M.router._note_session_slow(session, _wu, latency_ms=_wd, ctx_est=ctx)
                                    M.log.info(
                                        "[slow-race] holder -> %s (tentativo %.0fs vs %s %.0fs)",
                                        u,
                                        _d / 1000.0,
                                        _wu,
                                        _wd / 1000.0,
                                    )
                            else:
                                with contextlib.suppress(Exception):
                                    M.router._note_session_slow(session, u, latency_ms=_d, ctx_est=ctx)
                    except Exception:
                        report_suppressed("probes._run@160")
            elif v == "timeout" or (v is None and died):
                # muto/fallito come il tentativo servito: cooldown lungo.
                M.router.mark_failed(u, reason="timeout")
                metrics.inc("nx_hedge_total", ("probe_fail",))
            elif v in ("error", "truncated") or died:
                # errore di trasporto/stream rotto: cooldown corto solito.
                try:
                    _f24 = M.router.stats_for(u).fail_count_24h
                except Exception:
                    _f24 = 0
                M.router.mark_failed(u, seconds=M._soft_cd(_f24), reason="probe_error")
                metrics.inc("nx_hedge_total", ("probe_fail",))
            else:
                # empty_eof/length_truncated: comportamento del modello, non
                # colpa della chiave — SOLO qui nessuna penale, identico alla
                # regola del tentativo servito.
                metrics.inc("nx_hedge_total", ("probe_drop",))
            M.log.info("[probe] %s: %s (verdetto=%s)", u, "in warm" if ok else "gestito di solito", v)
        finally:
            with contextlib.suppress(Exception):
                M.router.note_probe_done(session, u)
            with contextlib.suppress(Exception):
                M.router.note_end(u, ctx)

    t = asyncio.ensure_future(_run())
    M._PROBE_TASKS.add(t)
    t.add_done_callback(M._PROBE_TASKS.discard)


async def _probe_late_open(open_task, session, ctx, hold: bool, race: tuple[str, float] | None) -> None:
    """Apertura di un canary conclusa DOPO la fine della gara.

    L'apertura era rimasta in volo (headers lente): NON la si annulla — si
    aspetta il record e lo si stacca come probe reale, cosi' chi consegna
    entra comunque nel warm della sessione (regola utente: qualsiasi canary
    deve poter portare a segno, anche se lento; mai bloccare la risposta).
    """
    rec = None
    with contextlib.suppress(BaseException):
        rec = await open_task
    if not rec:
        return
    _spawn_probe(
        rec["dep"],
        rec["gen"],
        rec["fut"],
        None,
        session,
        ctx,
        hold,
        wake=bool(rec.get("wake")),
        t0=rec.get("t0"),
        race=race,
    )


def _spawn_wake_sweep(
    payload: dict,
    profile: str | None,
    cur_dep: dict,
    need,
    ctx,
    out_tokens,
    requested_group,
    session,
    raced: dict | None,
) -> None:
    """SVEglia in background (regola utente): fino a
    `warm_refill_wake_max_attempts` tentativi di risveglio su dep dormienti
    da 429 MATURO (>=1h), ognuno su una api_key DIVERSA da tutte le sessioni
    e diversa dagli altri tentativi. Chi risponde torna CALDO; chi fallisce
    vede RADDOPPIATO il proprio cooldown residuo. Gira staccata, mai nel
    percorso della risposta servita."""

    import app.main as M

    t = asyncio.ensure_future(
        _wake_sweep(payload, profile, cur_dep, need, ctx, out_tokens, requested_group, session, raced)
    )
    M._PROBE_TASKS.add(t)
    t.add_done_callback(M._PROBE_TASKS.discard)


async def _wake_sweep(
    payload: dict,
    profile: str | None,
    cur_dep: dict,
    need,
    ctx,
    out_tokens,
    requested_group,
    session,
    raced: dict | None,
) -> None:
    import app.main as M

    try:
        _pol = M.router.policy
        _n = int(getattr(_pol, "warm_refill_wake_max_attempts", 10) or 0)
        _age = float(getattr(_pol, "warm_refill_wake_min_cooldown_age_sec", 3600.0) or 3600.0)
    except Exception:  # noqa: BLE001
        return
    if _n <= 0:
        return
    used_uniq = set((raced or {}).get("uniq") or ())
    used_keys = set((raced or {}).get("keys") or ())
    done = 0
    tried: list[str] = []
    for i in range(_n):
        try:
            W = M.router.warm_wake_canary(
                profile,
                cur_dep,
                need,
                ctx,
                out_tokens,
                tried=used_uniq,
                requested_group=requested_group,
                exclude_keys=used_keys,
                exclude_uniq=used_uniq,
                min_age_sec=_age,
            )
        except Exception:  # noqa: BLE001
            return
        if W is None:
            break
        u = W["unique"]
        used_uniq.add(u)
        used_keys.add(str(W.get("api_key") or ""))
        tried.append(u)
        done += 1
        with contextlib.suppress(Exception):
            M.router.note_probe_started(session, u)
        try:
            ok, lat, code, _body = await autoprobe._probe_one(M.forwarder, W, 30.0)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                M.router.note_probe_done(session, u)
            raise
        except Exception:  # noqa: BLE001
            ok, code = False, 0
        with contextlib.suppress(Exception):
            M.router.note_probe_done(session, u)
        if ok:
            with contextlib.suppress(Exception):
                M.router.clear_cooldown(u)
                M.router.note_warm_owner(session, u)
            metrics.inc("nx_wake_sweep_total", ("ok",))
            M.log.info(
                "[sveglia] %s risponde (%.0fms, order=%s) -> torna caldo (tentativo %d/%d)",
                u,
                lat or 0.0,
                W.get("order"),
                done,
                _n,
            )
            return
        # KO: il dormiente ri-fallisce -> cooldown RADDOPPIATO (residuo).
        with contextlib.suppress(Exception):
            M.router.mark_failed_double_residual(u, reason="wake_probe", status=code or None)
        metrics.inc("nx_wake_sweep_total", ("ko",))
        M.log.info(
            "[sveglia] %s KO (code=%s, order=%s) -> cooldown raddoppiato (tentativo %d/%d)",
            u,
            code,
            W.get("order"),
            done,
            _n,
        )
    metrics.inc("nx_wake_sweep_total", ("exhausted",))
    if done:
        M.log.info("[sveglia] giro concluso: %d tentativi su [%s], nessun risveglio", done, ", ".join(tried))
    else:
        M.log.info("[sveglia] nessun dormiente maturo (429>=min_age) da svegliare")
