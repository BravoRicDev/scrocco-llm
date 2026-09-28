"""Motore STREAM con fallback: `_StreamFallback` + relay SSE `_ClientRelay`.

[IT] Dall'apertura del primo stream upstream alla consegna al client:
peek del primo contenuto, verdetti, riparazioni, fallback a catena, relay
SSE con watchdog. Estratto da app/main.py senza modifiche di logica
(logger "nx.main").

[EN] Streaming fallback engine and client SSE relay (moved out of app/main.py).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import forwarder as fwd
from . import metrics, repairlog
from . import state as gw_state
from .capabilities import count_image_parts
from .chat_hedge import _hedge_peek
from .chat_helpers import _cached_tokens_of, _emit_summary, _note_fb_refund
from .chat_media import _trim_chat_images
from .csvlearn import learn_content_string, learn_no_thinking, learn_strip_reasoning, learn_thinking_replay
from .fakecall import TemplateTokenStripper, fake_config_from_policy, is_escalation_group, looks_like_fake_tool_call
from .forwarder import (
    _CONTENT_ARRAY_RE,
    _MODEL_MISSING_RE,
    _PAYLOAD_SCHEMA_RE,
    _PROVIDER_TRANSIENT_RE,
    _QUOTA_EXHAUSTED_RE,
    _QUOTA_RESET_RE,
    _THOUGHT_SIG_RE,
    _UNKNOWN_FIELD_RE,
    StreamLoopDetected,
    UpstreamError,
    _corrective_kind,
    _corrective_note,
    _length_truncated_should_fail,
    _looks_context_limit,
    classify_error_class,
    dep_host,
    extract_requested_tokens,
    is_provider_error_body,
    is_provider_fault_body,
    is_provider_level,
    is_unclear_error,
    maybe_account_quota_cooldown,
    maybe_host_transient_cooldown,
    maybe_quarantine_ban,
    media_input_needed,
    media_modality_signature,
    media_reject_signature,
    note_context_limit,
    parse_quota_reset_seconds,
    reasoning_err_kind,
    repair_reasoning_error,
    restore_reasoning,
    tool_combo_signature,
)
from .histnorm import flatten_text_content
from .opencode_gate import is_opencode_zen_dep, opencode_cautious_request
from .policy import refill_out_budget
from .probes import _spawn_wake_sweep
from .protocols import sse_to_chat_obj as _sse2obj
from .qc import check_response, check_sanity
from .router import _prompt_chars, inject_identity
from .sampling import sampling_config_from_policy
from .schemaout import enforce_response, schemaout_config_from_policy
from .sse_utils import (
    _answer_chars,
    _buffered_answer_text,
    _collapse_sse_content,
    _collapse_sse_field,
    _merge_qc_tool_calls,
    _peek_stream,
    _rewrite_sse_tool_calls,
    _sse_data_objs,
    _strip_sse_content,
    _tool_calls_sse,
)
from .stream_verdicts import (
    _actionable_upstream_error,
    _discard_stream,
    _exhausted,
    _parachute_verdict,
    _payload_text_empty,
    _retry_at_ms,
    _soft_cd,
)
from .suppressed import report_suppressed
from .texttoolparse import (
    parse_text_toolcalls,
    strip_toolid_markup,
    text_config_from_policy,
    truncation_config_from_policy,
)
from .toolrepair import create_tool_repair_config
from .toolrepair import repair_tool_calls as _rep_tc
from .toolrepair import sanitize_response as _san_resp

log = logging.getLogger("nx.main")


class _ClientRelay:
    """Relay SSE verso il client di UNA richiesta streaming (method object di
    `_StreamFallback.sse`): emette il prebuffer e il resto dello stream upstream,
    sorveglia la disconnessione del client e, a fine stream, registra summary,
    usage ed eventuali penalita'. `self.fb` e' la pipeline di fallback."""

    def __init__(self, fb):
        self.fb = fb

    async def run(self):
        if self.fb._synth:
            for self._b in self.fb._synth:
                yield self._b
            _emit_summary(
                ses=self.fb.ses or "-",
                req=self.fb.req or "-",
                grp=self.fb.dep["group"],
                dep=self.fb.dep["unique"],
                tries=len(self.fb.attempts),
                fb=max(0, len(self.fb.attempts) - 1),
                dur_ms=int((time.monotonic() - self.fb.t_req) * 1000),
                stream=self.fb.client_stream,
                qc=False,
                wd="text-toolcall",
                ttfb_ms=self.fb.ttfb_ms,
                usage=None,
            )
            _note_fb_refund(gw_state.router, self.fb.ses, max(0, len(self.fb.attempts) - 1))
            return
        self._init_state()
        # il task che sta eseguendo QUESTO generator (sse): e' lui che va
        # cancellato per interrompere SUBITO l'attesa upstream. asyncio
        # current_task() qui restituisce proprio il task della StreamingResponse.
        self._sse_task = asyncio.current_task()


        try:
            self.monitor = asyncio.create_task(self._watch_disconnect())
            # STRIP dei marker di template (Nemotron/Ling): nessun marker di
            # tool-call testuale deve arrivare al client (anche sul bucket di
            # escalation, dove non ruotiamo).
            self._stripper = TemplateTokenStripper()
            # (D2/B) ordine: prima il prebuffer gia' letto da _peek_stream, poi
            # l'eventuale lettura rimasta in volo (`pending`), poi il resto.
            # OUTPUT STRUTTURATO (HOLD): se il content e' stato pulito/riparato
            # prima di inviare i byte, si emette il testo sanificato al posto
            # dell'originale (finish_reason/usage/[DONE] preservati).
            self._emit_chunks = _collapse_sse_content(self.fb.prebuf, self.fb._so_text) if self.fb._so_rewrite else self.fb.prebuf
            for self.chunk in self._emit_chunks:
                yield _strip_sse_content(self._ingest(self.chunk), self._stripper)
            if self.fb.pending is not None:
                try:
                    yield _strip_sse_content(self._ingest(await self.fb.pending), self._stripper)
                except StopAsyncIteration:
                    self.finished = True
                except Exception:
                    self.gen_broken = True  # upstream rotto a meta' frame
            if not self.finished and not self.gen_broken:
                async for self.chunk in self.fb.gen:
                    yield _strip_sse_content(self._ingest(self.chunk), self._stripper)
                self.finished = True  # StopAsyncIteration: stream chiuso
            if self._stripper.tail:
                log.debug("[strip-tokens] coda residua scartata a fine stream (len=%d)", len(self._stripper.tail))
        except (GeneratorExit, asyncio.CancelledError):
            # disconnessione client o aborted dal monitor: chiudi l'upstream e
            # non punire il deployment (e' il client che e' andato via).
            if not self.aborted:
                await _discard_stream(self.fb.gen, self.fb.pending)
            raise  # disconnessione client: non punire
        except Exception as _exc_exc:
            self.exc = _exc_exc
            self.gen_broken = True
            # anti-stall: StreamStallError e' un asyncio.TimeoutError -> danno
            # reale (upstream appeso), cooldown lungo invece del soft.
            self.gen_stall = isinstance(self.exc, asyncio.TimeoutError)
            self.gen_loop = isinstance(self.exc, StreamLoopDetected)
        finally:
            self._on_stream_end()

    def _init_state(self):
        """Contatori e flag del watchdog passivo (chunk, [DONE], finish_reason, usage, esito)."""
        self.sent_first = False
        self.chunks = 0
        self.seen_done = False
        self.seen_error = False
        self.finished = False
        self.wd: str | None = None
        self.usage_final: dict | None = None
        self.sum_sent = False
        self.answer_total = 0  # solo testo risposta (D2/C)
        self.req_has_input = not _payload_text_empty(self.fb.payload)  # D2/C
        self.finish_len = False  # finish_reason == "length" (D2/C)
        self.saw_finish_reason = False  # QUALSIASI finish_reason non nullo
        self.last_finish_reason: str | None = None  # ultimo finish_reason visto
        self.had_tool_calls = False  # tool_calls visti (D2/C)
        self.req_max_tokens = self.fb.payload.get("max_tokens") or self.fb.payload.get("max_completion_tokens")



        self.gen_broken = False
        self.gen_stall = False  # stall mid-stream rilevato (anti-stall)
        self.gen_loop = False  # loop degenere rilevato in streaming
        self.aborted = False  # client disconnesso durante lo stream
        self.monitor: asyncio.Task | None = None

    async def _watch_disconnect(self) -> None:
        """Se il client chiude la connessione, interrompe SUBITO il task di
        sse() (CancelledError) invece di lasciare l'upstream generare fino a
        fine stream: niente token sprecati sul provider e niente raffiche di
        'socket.send() raised exception' verso una socket morta.

        NB: cancellare il task di sse() chiude anche `gen` (il generator
        upstream esegue il suo finally -> resp.aclose()); aclose() diretto
        da un altro task NON interrompe un generator in pausa, quindi e'
        il task a dover essere cancellato."""
        pass
        try:
            while True:
                await asyncio.sleep(0.5)
                disconnected = False
                if self.fb.request is not None:
                    try:
                        disconnected = await self.fb.request.is_disconnected()
                    except Exception:
                        disconnected = False
                if disconnected:
                    self.aborted = True
                    if self._sse_task is not None:
                        self._sse_task.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            report_suppressed("main._ClientRelay._watch_disconnect")

    # corpo del loop fattorizzato: aggiorna lo stato watchdog ed emette
    # il chunk invariato. Condiviso da prebuffer e dal flusso residuo.
    def _ingest(self, chunk: bytes) -> bytes:
        pass
        pass
        pass
        if self.fb.sniffer is not None:
            self.fb.sniffer.feed(chunk)
        self.chunks += 1
        if b"[DONE]" in chunk:
            self.seen_done = True
        if b'data: {"error"' in chunk:
            self.seen_error = True
        if self.usage_final is None and b'"usage"' in chunk and self.chunks > 1:  # parse best-effort del chunk usage
            try:
                line = next((ln for ln in chunk.split(b"\n") if ln.startswith(b"data:") and b'"usage"' in ln), None)
                if line:
                    obj = json.loads(line[5:].strip())
                    u = obj.get("usage")
                    if isinstance(u, dict):
                        self.usage_final = {
                            k: u[k]
                            for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                            if u.get(k) is not None
                        }
                        _cached = _cached_tokens_of(u)
                        if _cached is not None:
                            self.usage_final["cached_tokens"] = _cached
                            if _cached > 0:
                                metrics.inc("nx_cache_hit_requests_total", ())
                        c = u.get("cost")
                        if isinstance(c, dict):
                            self.usage_final["cost"] = c.get("total_cost")
                        elif c is not None:
                            self.usage_final["cost"] = c
            except Exception:
                pass
        # F14: calibrazione closed-loop dell'estimator col prompt_tokens
        # reale del provider (stream: arriva nel chunk finale di usage).
        try:
            if isinstance(self.usage_final, dict) and self.usage_final.get("prompt_tokens"):
                gw_state.router.note_estimate_error(self.fb.dep["unique"], self.fb.ctx, self.usage_final["prompt_tokens"])
                # Stima per-sessione (stream): char REALI inviati a monte.
                gw_state.router.note_session_estimate(
                    self.fb.ses,
                    self.fb.est_chars,
                    _prompt_chars(self.fb.payload.get("messages"), self.fb.payload.get("tools")),
                    self.usage_final["prompt_tokens"],
                )
                metrics.inc("nx_sess_est_samples_total")
        except Exception:
            report_suppressed("main._ClientRelay._ingest")
        for o in _sse_data_objs(chunk):
            self.answer_total += _answer_chars(o)
            for ch in o.get("choices") or []:
                if not isinstance(ch, dict):
                    continue
                fr = ch.get("finish_reason")
                if fr:
                    self.saw_finish_reason = True
                    self.last_finish_reason = fr
                    if fr == "length":
                        self.finish_len = True
                d = ch.get("delta") or ch.get("message") or {}
                if isinstance(d, dict) and d.get("tool_calls"):
                    self.had_tool_calls = True
        if not self.sent_first:
            self.sent_first = True  # TTFB gia' presa agli header upstream
        return chunk

    def _on_stream_end(self):
        """Chiusura dello stream (sempre, dal finally): monitor, inflight, esito, summary e sniff."""
        if self.monitor is not None:
            self.monitor.cancel()
        self.dur_ms = int((time.monotonic() - self.fb.t_req) * 1000)
        gw_state.router.note_end(self.fb.dep["unique"], self.fb.ctx)
        if not self.aborted:
            # F1: durata TOTALE del tentativo vincente nel bucket di
            # contesto (il commit ha gia' registrato il TTFT).
            gw_state.router.note_stream_end(
                self.fb.dep["unique"],
                (time.monotonic() - self.fb.t_att) * 1000,
                self.fb.ctx,
                completion_tokens=(self.usage_final or {}).get("completion_tokens"),
            )
        self._judge_stream_outcome()
        self._summary(self.dur_ms)
        if self.fb.sniffer is not None:
            self.fb.sniffer.finish_stream(
                {
                    "status": "success" if (self.finished and not self.gen_broken) else ("aborted" if self.aborted else "broken"),
                    "wd": self.wd,
                    "chunks": self.chunks,
                    "answer_chars": self.answer_total,
                    "had_tool_calls": self.had_tool_calls,
                    "finish_reason_len": self.finish_len,
                    "saw_finish_reason": self.saw_finish_reason,
                    "seen_done": self.seen_done,
                    "usage": self.usage_final,
                    "dep_final": self.fb.dep.get("unique"),
                    "tries": len(self.fb.attempts),
                }
            )

    def _judge_stream_outcome(self):
        """Esito a fine stream: disconnessione del client, stream rotto/stallato/in loop o completo
        -> penalita' e successo del deployment (mai byte aggiuntivi al client)."""
        # NB (fix): il watchdog NON inietta mai nulla nello stream verso il
        # client (un `data:` non-conforme viene renderizzato come testo da
        # opencode & simili). L'unica reazione automatica e' il cooldown del
        # deployment, cosi' i retry del client / le richieste successive
        # evitano la chiave che ha scazzato.
        if self.aborted or self.finished or self.gen_broken:
            if self.aborted:
                # client disconnesso a meta' stream: NON e' colpa del
                # deployment -> nessun cooldown, solo log diagnostico.
                self.wd = "client-aborted"
                log.info("[watchdog] client disconnesso durante lo stream da %s (chunks=%d)", self.fb.dep["unique"], self.chunks)
            elif self.chunks == 0:
                self.wd = "tier1-empty"
                metrics.inc("nx_qc_watchdog_total", (self.fb.dep["unique"], "empty"))
                log.warning("[watchdog] tier1 stream VUOTO da %s (chunks=0): cooldown", self.fb.dep["unique"])
                self.fb._fail(self.fb.dep["unique"], seconds=_soft_cd(gw_state.router.stats_for(self.fb.dep["unique"]).fail_count_24h))
            elif self.seen_error:
                self.wd = "tier1-error"
                metrics.inc("nx_qc_watchdog_total", (self.fb.dep["unique"], "error"))
                log.warning("[watchdog] tier1 evento error esplicito da %s (chunks=%d)", self.fb.dep["unique"], self.chunks)
                self.fb._fail(self.fb.dep["unique"], seconds=_soft_cd(gw_state.router.stats_for(self.fb.dep["unique"]).fail_count_24h))
            elif self.gen_loop:
                # loop degenere: il modello streammava output ripetitivo,
                # il detector l'ha killato -> cooldown medio e riparti.
                self.wd = "loop-detected"
                metrics.inc("nx_qc_watchdog_total", (self.fb.dep["unique"], "loop"))
                log.warning(
                    "[watchdog] stream in LOOP da %s (chunks=%d): kill precoce, cooldown %ds",
                    self.fb.dep["unique"],
                    self.chunks,
                    fwd.STREAM_LOOP_COOLDOWN_S,
                )
                self.fb._fail(self.fb.dep["unique"], seconds=fwd.STREAM_LOOP_COOLDOWN_S, reason="loop_detected")
            elif self.gen_broken or (not self.seen_done and not self.saw_finish_reason):
                # troncamento GENUINO: stream rotto a meta' oppure niente
                # [DONE] E niente finish_reason -> il modello ha scazzato.
                if self.gen_stall:
                    # upstream "congelato" a meta' stream (nessun byte per
                    # stream_stall_sec): danno REALE -> cooldown lungo.
                    self.wd = "tier2-stall"
                    metrics.inc("nx_qc_watchdog_total", (self.fb.dep["unique"], "stall"))
                    log.warning(
                        "[watchdog] tier2 stream in STALLO da %s (chunk=%d, stall=%.0fs): cooldown",
                        self.fb.dep["unique"],
                        self.chunks,
                        float(getattr(gw_state.router.policy, "stream_stall_sec", 0) or 0),
                    )
                    self.fb._fail(self.fb.dep["unique"], reason="timeout")
                else:
                    self.wd = "tier2-truncated"
                    metrics.inc("nx_qc_watchdog_total", (self.fb.dep["unique"], "truncated"))
                    log.warning(
                        "[watchdog] tier2 stream TRONCATO da %s (chunk=%d, finish_reason=%s): cooldown",
                        self.fb.dep["unique"],
                        self.chunks,
                        self.saw_finish_reason,
                    )
                    self.fb._fail(self.fb.dep["unique"], seconds=_soft_cd(gw_state.router.stats_for(self.fb.dep["unique"]).fail_count_24h))
            elif not self.seen_done:
                # c'e' un finish_reason ma manca [DONE]: risposta di fatto
                # completa, il provider omette solo il sentinel. Solo log.
                self.wd = "tier2-no-done"
                log.info(
                    "[watchdog] tier2 %s: finish_reason presente, nessun [DONE] (provider senza sentinel)",
                    self.fb.dep["unique"],
                )
            elif (
                self.finish_len
                and self.fb._maxtok.get("cap")
                and (self.usage_final or {}).get("completion_tokens") is not None
                and int((self.usage_final or {}).get("completion_tokens")) >= int(self.fb._maxtok["cap"]) - 2
            ):
                # Troncatura AUTO-INFLITTA: il gateway ha clampato
                # max_tokens e il modello ha esaurito ESATTAMENTE quel
                # budget (finish_reason=length). Non e' colpa del
                # deployment: nessuna penale, solo log (i byte sono gia'
                # partiti). Serve a non far scattare il cooldown
                # zero-answer/length-truncated su risposte monche nostre.
                self.wd = "clamp-truncated"
                log.info(
                    "[watchdog] %s: risposta troncata dal clamp "
                    "gateway (max_tokens %s->%s, completion=%s): "
                    "nessuna penale",
                    self.fb.dep["unique"],
                    self.fb._maxtok.get("old"),
                    self.fb._maxtok["cap"],
                    (self.usage_final or {}).get("completion_tokens"),
                )
            elif _length_truncated_should_fail(
                self.finish_len,
                self.answer_total,
                self.req_max_tokens,
                (self.usage_final or {}).get("completion_tokens"),
                gw_state.router.policy.qc_sanity.rotate_on_length_truncated,
            ):
                # risposta TRONCATA dal modello (finish_reason=length) ma
                # con contenuto: come un errore -> cooldown del dep, cosi'
                # le prossime richieste ruotano su un altro modello.
                # (La risposta corrente e' gia' partita: non e' ritraibile.)
                self.wd = "length-truncated"
                metrics.inc("nx_qc_watchdog_total", (self.fb.dep["unique"], "length_truncated"))
                log.warning(
                    "[watchdog] risposta TRONCATA (finish_reason="
                    "length) da %s (chunk=%d, answer=%d, "
                    "completion=%s, req_max=%s): cooldown + "
                    "rotazione",
                    self.fb.dep["unique"],
                    self.chunks,
                    self.answer_total,
                    (self.usage_final or {}).get("completion_tokens"),
                    self.req_max_tokens,
                )
                self.fb._fail(self.fb.dep["unique"], seconds=_soft_cd(gw_state.router.stats_for(self.fb.dep["unique"]).fail_count_24h))
            elif (
                self.answer_total == 0
                and self.req_has_input
                and not self.had_tool_calls
                and not (self.finish_len and not gw_state.router.policy.qc_sanity.rotate_on_length_empty)
            ):
                # stream "completo" ma 0 testo di risposta con input reale:
                # fallimento silenzioso -> cooldown (nessun artefatto verso
                # il client: i byte, per quanto vuoti, sono gia' partiti).
                self.wd = "zero-answer"
                metrics.inc("nx_qc_watchdog_total", (self.fb.dep["unique"], "zero_answer"))
                log.warning(
                    "[watchdog] stream 0-answer da %s (input non vuoto, finish_len=%s): cooldown",
                    self.fb.dep["unique"],
                    self.finish_len,
                )
                self.fb._fail(self.fb.dep["unique"], seconds=_soft_cd(gw_state.router.stats_for(self.fb.dep["unique"]).fail_count_24h))

    def _summary(self, dur_ms: int) -> None:
        pass
        if self.sum_sent:
            return
        self.sum_sent = True
        _emit_summary(
            ses=self.fb.ses or "-",
            req=self.fb.req or "-",
            grp=self.fb.dep["group"],
            dep=self.fb.dep["unique"],
            tries=len(self.fb.attempts),
            fb=max(0, len(self.fb.attempts) - 1),
            dur_ms=dur_ms,
            stream=self.fb.client_stream,
            qc=False,
            wd=self.wd,
            ttfb_ms=self.fb.ttfb_ms,
            fr=self.last_finish_reason,
            usage=self.usage_final,
        )
        _note_fb_refund(gw_state.router, self.fb.ses, max(0, len(self.fb.attempts) - 1))


class _StreamFallback:
    """Method object di `_stream_with_fallback` (Replace Method with Method Object).

    Lo stato di UNA richiesta streaming (deployment corrente, tentativi, trail,
    rimedi gia' provati, buffer del peek...) vive negli attributi invece che
    in ~200 variabili locali: la pipeline e' divisa in metodi che condividono
    `self`. Il comportamento e' quello della funzione originale."""

    def __init__(self, profile, first_dep, payload, need, hook, scope, ctx, ses, est_chars, req, session, client_ip, request, attribution, requested_group, cold, prefix_reason, orig_messages, sniffer, result_box, client_stream):
        self.profile = profile
        self.first_dep = first_dep
        self.payload = payload
        self.need = need
        self.hook = hook
        self.scope = scope
        self.ctx = ctx
        self.ses = ses
        self.est_chars = est_chars
        self.req = req
        self.session = session
        self.client_ip = client_ip
        self.request = request
        self.attribution = attribution
        self.requested_group = requested_group
        self.cold = cold
        self.prefix_reason = prefix_reason
        self.orig_messages = orig_messages
        self.sniffer = sniffer
        self.result_box = result_box
        self.client_stream = client_stream

    async def run(self):
        self._init_request_state()
        # Gemini 3 tool replay: una history con tool_call prive di firma rende Gemini
        # inutilizzabile. L'esclusione avviene A MONTE nel router (set_avoid_gemini in
        # chat_completions -> _gemini_blocked in pick_deployment/_walk_chain), quindi
        # qui non serve più alcun salto o tentativo finto.
        while True:
            self._begin_attempt()
            try:
                await self._open_upstream_stream()
                self._load_attempt_settings()
                await self._peek_first_content()
                await self._resolve_verdict()
                self._apply_content_remedies()
                if self._commit_content():
                    break
                await self._close_rejected_attempt()
                self._penalize_verdict()
                _resp = self._last_resort_after_verdict()
                if _resp is not None:
                    return _resp
                self.dep = self.nxt
                inject_identity(self.payload, self.dep, router=gw_state.router)
                continue  # ri-entra nel while col nuovo dep
            except UpstreamError as _err_exc:
                self.err = _err_exc
                gw_state.router.note_end(self.dep["unique"], self.ctx)  # tentativo chiuso senza stream
                gw_state.router.key_lease_release(self._lease)  # P2-8
                self._lease = None
                self.detail = self.err.detail or ""
                if self._on_upstream_error():
                    continue
                _resp = self._last_resort_after_upstream_error()
                if _resp is not None:
                    return _resp
                if self.ses:
                    gw_state.router.sticky_handoff(self.ses, self.nxt)
                self.dep = self.nxt
                inject_identity(self.payload, self.dep, router=gw_state.router)
            except (GeneratorExit, asyncio.CancelledError):
                raise
            except Exception as _exc_exc:
                # qualsiasi errore IMPREVISTO nell'ottenere lo stream da questo
                # deployment (es. httpx che cade leggendo il body d'errore) -> NON
                # deve 500-are la richiesta: cooldown corto + rotazione, 503 solo
                # se non resta nulla.
                self.exc = _exc_exc
                self._on_unexpected_exception()
                _resp = self._last_resort_after_exception()
                if _resp is not None:
                    return _resp
                if self.ses:
                    gw_state.router.sticky_handoff(self.ses, self.nxt)
                self.dep = self.nxt
                inject_identity(self.payload, self.dep, router=gw_state.router)


        return self._ret(StreamingResponse(self.sse(), media_type="text/event-stream"))

    def _init_request_state(self):
        """Stato iniziale della richiesta: tetto immagini, contatori, configurazioni di rimedio da policy."""
        self.dep = self.first_dep
        # Tetto immagini: una volta sola, prima di qualsiasi tentativo, cosi' vale
        # per TUTTA la catena di fallback (niente righe per-deployment e nessuna
        # copia per tentativo). L'originale resta intatto: i rimedi che
        # ripristinano la history (reasoning replay) continuano a vedere tutto.
        try:
            self._imax = int(getattr(gw_state.router.policy, "chat_images_max", 0) or 0)
        except Exception:  # noqa: BLE001
            self._imax = 0
        if self._imax > 0 and count_image_parts(self.payload.get("messages") or []) > self._imax:
            self.payload, self._dropped = _trim_chat_images(self.payload, self._imax)
            if self._dropped:
                metrics.inc("nx_images_total", ((self.dep or {}).get("group", "-"), "chat_images_trimmed"))
                log.info(
                    "[images] tetto chat_images_max=%d: %d immagini non "
                    "inviate all'upstream (restano nella history del client)",
                    self._imax,
                    self._dropped,
                )
        # Gruppo ORIGINARIO della richiesta (es. -200k): serve al pin
        # escalation-winner per valere anche dopo la salita su altre dim.
        self.requested_group = self.requested_group or (self.first_dep or {}).get("group")
        self.tried = 0
        self.tried_set: set[str] = set()
        self._rsn_steps: dict[str, set] = {}  # rimedi reasoning per dep
        self._cstr_steps: dict[str, set] = {}  # rimedi content-string per dep
        self._rsn_restored = False  # history originale gia' riprovata
        self._max_tries = int(
            getattr(gw_state.router.policy, "max_fallback_tries", os.environ.get("GATEWAY_MAX_FALLBACK_TRIES", "128")) or 128
        )
        # Tool repair config per streaming

        self._tr_cfg = create_tool_repair_config(
            {
                "tool_repair": {
                    "enabled": gw_state.router.policy.tool_repair_enabled,
                    "default_level": gw_state.router.policy.tool_repair_default_level,
                    "disable_for_google": gw_state.router.policy.tool_repair_disable_for_google,
                    "max_args_size": gw_state.router.policy.tool_repair_max_args_size,
                },
            }
        )
        self._fc = fake_config_from_policy(gw_state.router.policy)

        self._tt = text_config_from_policy(gw_state.router.policy)
        self._tct_cfg = truncation_config_from_policy(gw_state.router.policy)

        self._sm = sampling_config_from_policy(gw_state.router.policy)

        self._so = schemaout_config_from_policy(gw_state.router.policy)
        # QC di contenuto (parita' col non-stream): attivi anche in hold.
        self.qc = gw_state.router.policy.qc_json
        self.san = gw_state.router.policy.qc_sanity
        self._synth: list[bytes] = []
        # OUTPUT STRUTTURATO in HOLD: la risposta bufferizzata viene trattata come
        # non-streaming -> pulizia/riparazione JSON prima di inviare i byte.
        self._so_corrected: set[str] = set()  # retry correttivo gia' provato per dep
        self._so_rewrite = False  # il content va riscritto in emissione
        self._so_text = ""  # content sanificato da inviare
        self.t_req = time.monotonic()
        try:
            self._hedge_ms = int(getattr(gw_state.router.policy.qc_json, "stream_hedge_delay_ms", 0) or 0)
        except Exception:
            self._hedge_ms = 0
        self.attempts: list[str] = []
        # ATTEMPT TRAIL (P0): per ogni hop fallito, PERCHE' e' stato scartato
        # (classe d'errore onesta). Finisce nel body/header del 503 finale.
        self.trail: list = []
        self.skip_hosts: set[str] = set()  # P1-5: host saltati (errore provider)
        self._lease = None  # P2-8: lease per chiave (opt-in)



        self._races_done = 0
        # WARM-REFILL a cascata: candidati gia' sonciati in QUESTA richiesta
        # (uniq + api_key) e round gia' consumati (budget per-richiesta =
        # warm_refill_max_inflight; il tetto GLOBALE e' il registro in volo per
        # sessione nel router).
        self._raced: dict = {}
        self._refill_rounds = 0
        self._wake_spawned = False  # la SVEglia parte una volta per richiesta
        self.ttfb_ms: int | None = None  # letta da sse()/_summary via closure
        # Clamp max_tokens GATEWAY-side dell'attempt corrente (via maxtok_hook):
        # se il modello esaurisce il NOSTRO budget ridotto, la troncatura e'
        # auto-inflitta -> il watchdog non deve punire il deployment.
        self._maxtok: dict = {}

    def _begin_attempt(self):
        """Apre il tentativo sul deployment corrente: contatori, stato di rimedio per-tentativo, note_start."""
        self.tried += 1
        self._maxtok.clear()
        self._so_rewrite = False
        self._so_text = ""
        self.attempts.append(self.dep["unique"])
        self.tried_set.add(self.dep["unique"])
        self._was_dormant = gw_state.router.is_cooled_down(self.dep["unique"])


        gw_state.router.note_start(self.dep["unique"], self.ctx)
        # qcp PRIMA del try: lo usano anche gli handler `except` (es.
        # stream_total_deadline_ms), quindi deve essere sempre definito anche se
        # `stream_response` solleva UpstreamError al primo invio (429/402 subito).
        self.qcp = gw_state.router.policy.qc_json

    async def _open_upstream_stream(self):
        """Apre lo stream upstream del tentativo (hook di troncatura, thinking replay, lease della chiave)."""
        self.t_att = time.monotonic()
        # hook: a fine stream, se il guard ha trovato un tag tool-call
        # rotto, declassa il deployment (cooldown breve). `salvaged` dice
        # se la chiamata e' stata recuperata o scartata.
        self._trunc_unique = self.dep["unique"]
        self._trunc_was_dormant = self._was_dormant

        def _trunc_hook(_salvaged, _u=self._trunc_unique, _was=self._trunc_was_dormant):
            metrics.inc("nx_truncated_toolcall_total", (_u, "salvaged" if _salvaged else "dropped"))
            repairlog.note(
                "salvage_truncated",
                source="stream",
                outcome="ok" if _salvaged else "fail",
                dep=_u,
                model=self.dep.get("model", ""),
                detail="tag tool-call rotto",
            )
            log.warning(
                "[truncation] stream %s: tag tool-call rotto (%s) -> declasso %ds",
                _u,
                "salvato" if _salvaged else "scartato",
                self._tct_cfg.cooldown_sec,
            )
            if _was:
                gw_state.router.mark_failed_double_residual(_u, reason="truncated_toolcall")
            else:
                gw_state.router.mark_failed(_u, seconds=self._tct_cfg.cooldown_sec, reason="truncated_toolcall")
        self._trunc_hook = _trunc_hook

        if self.dep.get("thinking_replay") and self.orig_messages:
            self._tpr = restore_reasoning(self.payload, self.orig_messages)
            if self._tpr:
                metrics.inc("nx_thinking_replay_total", ("proactive",))
                log.info(
                    "[thinking-replay] %s: %d campi reasoning rimessi PRIMA dell'invio (proattivo)",
                    self.dep["unique"],
                    self._tpr,
                )
        self._lease = gw_state.router.key_lease_acquire(self.dep)  # P2-8 (opt-in)
        # HOLD (parita' col non-stream): se la risposta sara' interamente
        # bufferizzata, la riparazione tool-call NON si fa nel filtro SSE
        # incrementale ma ALLA FINE sull'output GREZZO totale (stessa
        # riparazione del percorso non-streaming). Vedi blocco HOLD sotto.
        self._defer_tr = bool(self.dep.get("hold_until_finish")) or bool(
            getattr(gw_state.router.policy.qc_json, "stream_hold_until_finish", False)
        )
        self.gen = await gw_state.forwarder.stream_response(
            self.dep,
            self.payload,
            profile=self.profile or "",
            ctx_est=self.ctx,
            client_ip=self.client_ip,
            session=self.session,
            attribution=self.attribution,
            tool_repair_config=self._tr_cfg,
            truncation_config=self._tct_cfg,
            truncation_hook=_trunc_hook,
            maxtok_hook=lambda old, new: self._maxtok.update(cap=new, old=old),
            rate_hook=lambda u, rl: gw_state.router.note_rate_limit(u, rl),
            defer_tool_repair=self._defer_tr,
        )

    def _load_attempt_settings(self):
        """TTFB, qualita' e parametri di commit/hold/race letti per questo tentativo."""
        # la TTFB vera e' il tempo fino agli HEADER upstream
        # (send(stream=True) ritorna gia' col primo chunk bufferizzato:
        # misurarla sul primo yield darebbe sempre ~0ms e avvelenerebbe
        # l'EMA della rotazione adattiva con latenze nulle).
        self.ttfb_ms = int((time.monotonic() - self.t_att) * 1000)
        self._quality = 1.0
        if self._was_dormant:
            gw_state.router.clear_cooldown(self.dep["unique"])
        # ANTI-STALLO (1): lo stream verso il client NON parte finche' non
        # arriva CONTENUTO DI RISPOSTA reale. Entro stream_first_content_ms
        # un upstream vuoto/errore/lento viene ruotato in modo TRASPARENTE
        # (nessun byte inviato). Esaurita la catena -> risposta "notice".
        self.qcp = gw_state.router.policy.qc_json
        # ADATTIVO: deadline proporzionale alla latenza storica (EMA) del
        # dep scelto, con pavimento e tetto. Un dep normalmente veloce che
        # stalla non trattiene la richiesta per il cap; un dep lento ha un
        # margine proporzionato (mai oltre il cap). EMA ignota -> cap.
        self.fc_ms = gw_state.router.first_content_deadline_ms(self.dep["unique"], self.ctx)
        self.incl_reason = bool(getattr(self.qcp, "stream_commit_include_reasoning", False))
        self.min_ch = int(getattr(self.qcp, "stream_commit_min_chars", 40) or 0)
        # HOLD-UNTIL-FINISH: attesa della chiusura PULITA dello stream
        # prima di inviare byte (per-deployment dal CSV, o globale da
        # policy). Cosi' una risposta troncata non arriva MAI al client:
        # si ruota pre-byte come per gli altri errori.
        self.hold = bool(self.dep.get("hold_until_finish")) or bool(getattr(self.qcp, "stream_hold_until_finish", False))
        self.hold_idle = int(getattr(self.qcp, "stream_hold_idle_ms", 120000) or 120000)
        self.hold_maxb = int(getattr(self.qcp, "stream_hold_max_buffer_bytes", 52428800) or 52428800)
        # --- peek + HEDGE (F3) + WARM-REFILL a cascata ------------------
        # La gara parte quando: (legacy) il warm non puo' aiutare —
        # catena fredda, holder lento o gia' provato; oppure (REFILL) la
        # sessione ha MENO di warm_ready_min caldi che possono
        # EFFETTIVAMENTE consegnare questa richiesta (need + ctx + output
        # assicurato). Il refill ignora lentezza e warm utile: 2 alla
        # volta (A + 1 canary nuovo, free-only), a cascata a ogni
        # rotazione. I perdenti restano in volo come probe reali.
        self._h_ms = 0
        self._fresh_only = False
        self._legacy = False
        self._refill = False
        self._zen_hunt = False  # caccia canary zen-only (nativo)
        self._pol = gw_state.router.policy
        # Budget di output della richiesta: serve SEMPRE (non solo in
        # refill) — e' il criterio di "capace" per il gruppo warm (gate
        # della gara lenta e conteggi di prontezza). Senza questo il gate
        # conteggiava come caldi dep che non possono consegnare l'output.
        self._need_out = refill_out_budget(self.payload, self._pol)

    async def _peek_first_content(self):
        """Attende il primo contenuto utile (con gare warm/hedge/slow-race): produce `verdict`."""
        self._plan_warm_refill()
        self._plan_races()
        await self._run_peek()

    def _plan_warm_refill(self):
        """Modalita' degradata, bucket di escalation e piano di warm-refill a cascata."""
        # DEGRADED (P1-6): in un blackout upstream l'esplorazione
        # (cascata refill, hedge canary, sveglia) si sospende: spreca
        # rate-limit e chiavi. Resta la rotazione della ladder.
        try:
            self._degraded = gw_state.router.degraded_active()
        except Exception:
            self._degraded = False
        if self._degraded and not self._wake_spawned:
            self._wake_spawned = True  # evita ripetizioni nel loop
            log.info("[degraded] esplorazione sospesa per questa richiesta (%s)", self.dep.get("unique"))
        # Bucket di escalation (-go/-fallback): niente esplorazione
        # Solo se il gruppo RICHIESTO esplicitamente e' un bucket di
        # escalation (-go/-fallback): niente refill/canary/gara lenta/hedge
        # (i bucket a pagamento non usano il caldo, sonde sprecate). Se ci
        # si arriva via FALLBACK dal dim, la speculativa resta attiva per
        # tornare al caldo appena possibile.
        self._esc_grp = is_escalation_group(
            str(self.requested_group or ""), gw_state.router.config.go_suffix, gw_state.router.config.fallback_suffix
        )
        if (
            self.session
            and self.profile
            and not self._degraded
            and not self._esc_grp
            and not opencode_cautious_request()
            and bool(getattr(self._pol, "warm_refill_enabled", True))
            and bool(getattr(self._pol, "warm_pool_enabled", True))
        ):
            self._ready = gw_state.router.warm_ready_effective(self.session, self._pol)
            self._maxif = max(0, int(getattr(self._pol, "warm_refill_max_inflight", 6) or 0))
            try:
                self._fly = gw_state.router.probes_in_flight(self.session)
            except Exception:
                self._fly = 0
            if self._ready and self._refill_rounds < self._maxif and self._fly < self._maxif:
                try:
                    self._pool = gw_state.router.warm_valid_for(
                        self.session,
                        self.profile,
                        self.requested_group or self.dep.get("group"),
                        self.need,
                        self.ctx,
                        self._need_out,
                        tried=self.tried_set,
                        include_borrowed=True,
                    )
                    self._nv = len(self._pool)
                except Exception:
                    self._pool, self._nv = [], self._ready
                # Nativo opencode SENZA zen nel warm: caccia un canary
                # zen-only anche se il conteggio MISTO basta (basta 1 zen).
                self._zen_hunt = (
                    gw_state.router._zen_first_active()
                    and not any(is_opencode_zen_dep(d) for d in self._pool)
                    and gw_state.router.hunt_allowed(self.session, self.ctx)
                )
                self._refill = (self._nv < self._ready) or self._zen_hunt
                if self._zen_hunt:
                    gw_state.router.note_hunt(self.session, self.ctx, gained=False)
                    log.info(
                        "[refill] %s: 0 zen nel warm per client nativo -> caccia canary zen-only", self.dep.get("unique")
                    )
                if self._refill:
                    self._rpm = gw_state.router.session_rpm(self.session)
                    log.info(
                        "[refill] %s: warm validi %d/%d, in volo "
                        "%d/%d (ctx=%s, out=%s, rpm=%.1f) -> "
                        "canario extra in gara",
                        self.dep.get("unique"),
                        self._nv,
                        self._ready,
                        self._fly,
                        self._maxif,
                        self.ctx,
                        self._need_out,
                        self._rpm,
                    )
                    if not self._wake_spawned:
                        self._wake_spawned = True
                        _spawn_wake_sweep(
                            self.payload, self.profile, self.dep, self.need, self.ctx, self._need_out, self.requested_group, self.session, self._raced
                        )

    def _plan_races(self):
        """Parametri di gara: slow race, slow canary e hedge sul cache holder."""
        # GARA LENTA: se A non ha ancora CONSEGNATO dopo N ms si apre 1
        # canario SENZA buttare via la risposta (regola utente): vince
        # chi consegna prima, ma per il giro successivo e' eletto chi ha
        # impiegato meno nel proprio tentativo. E' INDIPENDENTE
        # dall'hedge classico (che resta attivo) e vale anche in refill;
        # il canario lento NON concorre al tetto per-sessione.
        # NB: il campo vive su Policy (non su qc_json): leggerlo da qcp
        # lo lasciava sempre a 0 (bug: la gara lenta non partiva mai).
        self._slow_ms = 0
        self._slow_canary_ms = 0
        if not self._degraded and not self._esc_grp:
            try:
                self._slow_ms = int(getattr(gw_state.router.policy, "stream_slow_race_after_ms", 0) or 0)
            except Exception:
                self._slow_ms = 0
            try:
                self._slow_canary_ms = int(getattr(gw_state.router.policy, "slow_canary_after_ms", 0) or 0)
            except Exception:
                self._slow_canary_ms = 0
        self._slow_only = bool((self._slow_ms > 0 or self._slow_canary_ms > 0) and not self._refill)
        if not self._degraded and not self._esc_grp and (self._hedge_ms > 0 or self._refill or self._slow_only):
            try:
                self._h_dep = gw_state.router.cache_holder(need=self.need, ctx=self.ctx)
                self._h_u = self._h_dep["unique"] if self._h_dep else None
            except Exception:
                self._h_u = None
            self._warm_useful = bool(self._h_u and self._h_u not in self.tried_set and self._h_u != self.dep["unique"])
            self._races_max = int(getattr(self.qcp, "stream_hedge_max_races", 0) or 0)
            self._legacy = (
                not self._warm_useful
                and (self._races_max == 0 or self._races_done < self._races_max)
                and gw_state.router.hunt_allowed(self.session, self.ctx)
            )
            if self._legacy or self._refill or self._slow_only:
                if self._refill:
                    # la cascata parte SUBITO e con il proprio picker:
                    # indipendente dalla lentezza di A (regola utente).
                    self._h_ms = 1
                else:
                    # HEDGE CLASSICO invariato (F13: ritardo calibrato sul
                    # bucket, TTFT fisiologico). La gara lenta NON lo
                    # sostituisce: e' un timer separato dentro _hedge_peek.
                    try:
                        self._h_ms = gw_state.router.hedge_delay_ms(self.dep["unique"], self.ctx)
                    except Exception:
                        self._h_ms = self._hedge_ms
                if self._h_ms <= 0 and self._slow_only:
                    # hedge classico spento ma la gara lenta va armata:
                    # _hedge_peek deve essere chiamato comunque.
                    self._h_ms = 1
                self._fresh_only = bool(self._h_u and self._h_u == self.dep["unique"])

    async def _run_peek(self):
        """Esegue il peek: con gara (hedge/refill/slow) oppure sul solo deployment corrente."""
        if not self._esc_grp and self._h_ms > 0:
            self._races_done += 1
            if self._refill:
                self._refill_rounds += 1
            self._dep_before = self.dep["unique"]
            if self._refill:
                self._hh_k = 2
            else:
                self._hh_k = (
                    max(1, int(getattr(self.qcp, "stream_hedge_tiers", 1) or 1))
                    if bool(getattr(self.qcp, "stream_hedge_cross_tier", True))
                    else 1
                )
            self._raced.setdefault("uniq", set()).add(self.dep["unique"])
            self._raced.setdefault("keys", set()).add(str(self.dep.get("api_key") or ""))
            (self.dep, self.gen, self.t_att, self.verdict, self.prebuf, self.pending, self.meta) = await _hedge_peek(
                self.dep,
                self.gen,
                self.t_att,
                self.fc_ms,
                self.incl_reason,
                self.min_ch,
                self.hold_idle,
                self.hold_maxb,
                payload=self.payload,
                profile=self.profile,
                need=self.need,
                scope=self.scope,
                ctx=self.ctx,
                tried_set=self.tried_set,
                attempts=self.attempts,
                requested_group=self.requested_group,
                session=self.session,
                client_ip=self.client_ip,
                attribution=self.attribution,
                hedge_ms=self._h_ms,
                _tr_cfg=self._tr_cfg,
                _tct_cfg=self._tct_cfg,
                k=self._hh_k,
                fresh_only=self._fresh_only,
                hold=self.hold,
                refill=self._refill,
                zen_only=self._zen_hunt,
                slow_race_ms=self._slow_ms,
                slow_canary_ms=self._slow_canary_ms,
                out_tokens=self._need_out or None,
                raced=self._raced,
            )
            if self._legacy:
                # backoff "il buono non esiste": solo la gara legacy
                # consuma il budget caccia; il refill ha il suo (round).
                gw_state.router.note_hunt(self.session, self.ctx, gained=(self.dep["unique"] != self._dep_before))
        else:
            self.verdict, self.prebuf, self.pending, self.meta = await _peek_stream(
                self.gen,
                self.fc_ms,
                self.incl_reason,
                self.min_ch,
                hold_until_finish=self.hold,
                hold_idle_ms=self.hold_idle,
                hold_max_bytes=self.hold_maxb,
            )

    async def _resolve_verdict(self):
        """Verdetto finale del peek: paracadute sotto hold e troncature da length."""
        # FIX paracadute: sulla catena -go/-fallback (ULTIMO scaglione del
        # ladder) il timeout sul primo contenuto NON deve produrre un 503:
        # li' non c'e' piu' nessuno dietro a cui ruotare, quindi si
        # consegna comunque quello che arriva (parametro opzionale
        # stream_parachute_no_timeout, default True). Sotto HOLD la
        # consegna e' SEMPRE bufferizzata (mai byte live): si scarta la
        # coda in volo cosi' il tool repair hold gira sul buffer parziale.
        self._pv = _parachute_verdict(self.verdict, self.qcp, self.dep, gw_state.router.policy, hold=self.hold, has_buffer=bool(self.prebuf))
        if self.hold and self.verdict == "timeout" and self._pv == "content":
            await _discard_stream(self.gen, self.pending)
            self.pending = None
        self.verdict = self._pv
        # HOLD: finish_reason=length -> risposta TRONCATA dal modello (non
        # dal cap del client): si ruota pre-byte, non si consegna il
        # parziale. Se invece il client ha chiesto max_tokens ed e' stato
        # raggiunto (stima answer_chars/4) la risposta e' voluta -> content.
        if self.verdict == "length_truncated":
            self._req_max = self.payload.get("max_tokens") or self.payload.get("max_completion_tokens")
            self._ans_chars = len(_buffered_answer_text(self.prebuf))
            self._capped = False
            try:
                if self._req_max and self._ans_chars > 0:
                    self._capped = (self._ans_chars / 4.0) >= float(self._req_max) - 2
            except (TypeError, ValueError):
                self._capped = False
            if self._capped:
                self.verdict = "content"

    def _apply_content_remedies(self):
        """Rimedi sul contenuto bufferizzato: tool call testuali/finte, tool repair, output strutturato e QC (hold)."""
        self._parse_text_tool_calls()
        self._reject_fake_tool_call()
        self._repair_tool_calls_on_hold()
        self._enforce_structured_output_on_hold()
        self._quality_check_on_hold()

    def _parse_text_tool_calls(self):
        """Tool call scritte come testo (formato non nativo): convertite in tool_calls vere."""
        if self.verdict == "content" and self._tt.enabled and self.payload.get("tools"):
            self._parsed = parse_text_toolcalls(_buffered_answer_text(self.prebuf), self.payload.get("tools"), self._tt)
            if self._parsed:
                self._synth.extend(_tool_calls_sse(self._parsed, self.dep.get("model")))
                metrics.inc("nx_text_toolcall_total", (self.dep["unique"], "parsed"))
                self._quality = 0.6
                repairlog.note(
                    "salvage_text",
                    source="stream",
                    outcome="ok",
                    dep=self.dep["unique"],
                    model=self.dep.get("model", ""),
                    detail="tool-call resi come testo",
                    count=len(self._parsed),
                )
                # OPZIONE A: il tool-call va RICOSTRUITO ma il testo
                # residuo (es. i marker <goal .../> del plugin) resta al
                # client: si rimuove SOLO il markup del tool-call.
                self._tt_txt = _buffered_answer_text(self.prebuf)
                self._tt_res = strip_toolid_markup(self._tt_txt)
                if self._tt_res != self._tt_txt:
                    self._so_text = self._tt_res
                    self._so_rewrite = True

    def _reject_fake_tool_call(self):
        """Tool call finte (JSON imitato nel testo) -> il verdetto diventa `fake_tool_call`."""
        if self.verdict == "content" and self._fc.enabled:
            self._pat = looks_like_fake_tool_call(_buffered_answer_text(self.prebuf), self._fc)
            if self._pat:
                metrics.inc("nx_fake_toolcall_total", (self.dep["unique"], "detected"))
                self._esc = is_escalation_group(self.dep.get("group"), gw_state.router.config.go_suffix, gw_state.router.config.fallback_suffix)
                if self._esc:
                    # sul bucket di escalation non c'e' dove ruotare senza
                    # loop: si logga e si lascia al sanitizzatore (strip dei
                    # marker), cosi' il client non li vede mai.
                    log.warning(
                        "[fake-tool-call] stream %s: tool-call reso "
                        "come testo (pattern=%s) su bucket di "
                        "escalation -> strip",
                        self.dep["unique"],
                        self._pat,
                    )
                else:
                    log.warning(
                        "[fake-tool-call] stream %s: tool-call reso come testo (pattern=%s), escalation",
                        self.dep["unique"],
                        self._pat,
                    )
                    self._quality = 0.3
                    self.verdict = "fake_tool_call"

    def _repair_tool_calls_on_hold(self):
        """Tool repair sull'output bufferizzato (hold): stessa riparazione del non-streaming."""
        # TOOL REPAIR (HOLD): la risposta e' INTERAMENTE bufferizzata ->
        # STESSA riparazione del percorso non-streaming, applicata
        # ALL'OUTPUT GREZZO totale. Sotto hold il filtro SSE incrementale
        # NON gira (defer_tool_repair), quindi l'intenzione del modello e'
        # intatta; qui si assembla, si ripara e si riscrive lo stream
        # bufferizzato (content + tool_calls) prima di inviare i byte.
        if self.verdict == "content" and self.hold and not self._synth:

            try:
                self._tr_obj = _sse2obj(self.prebuf)
            except ValueError:
                self._tr_obj = None
            if self._tr_obj is not None:
                self._tr_rep = _rep_tc(self._tr_obj, self.payload, self.dep, self._tr_cfg)
                self._tr_san = _san_resp(self._tr_obj)
                if self._tr_rep.get("repaired") or self._tr_san:
                    self._tr_msg = self._tr_obj["choices"][0]["message"]
                    if self._tr_san:
                        self._tr_c = self._tr_msg.get("content")
                        if isinstance(self._tr_c, str):
                            self.prebuf = _collapse_sse_field(self.prebuf, "content", self._tr_c)
                        self._tr_rc = self._tr_msg.get("reasoning_content")
                        if isinstance(self._tr_rc, str):
                            self.prebuf = _collapse_sse_field(self.prebuf, "reasoning_content", self._tr_rc)
                        metrics.inc("nx_content_sanitized_total", (self.dep["unique"],))
                    if self._tr_rep.get("repaired"):
                        self._tr_tcs = self._tr_msg.get("tool_calls")
                        if isinstance(self._tr_tcs, list):
                            self.prebuf = _rewrite_sse_tool_calls(self.prebuf, self._tr_tcs)
                        metrics.inc("nx_tool_repair_total", (self.dep["unique"], "ok"))
                        repairlog.note(
                            "repair_args",
                            source="stream",
                            outcome="ok",
                            dep=self.dep["unique"],
                            model=self.dep.get("model", ""),
                            detail="hold whole-output: moves=%s" % self._tr_rep.get("moves"),
                        )

    def _enforce_structured_output_on_hold(self):
        """Output strutturato (response_format) sull'output bufferizzato: pulizia/riparazione o retry correttivo."""
        # OUTPUT STRUTTURATO (HOLD): la risposta e' INTERAMENTE bufferizzata
        # -> la trattiamo come non-streaming. Pulizia (A) / riparazione
        # schema-driven (D) PRIMA di inviare qualunque byte: il client non
        # vede mai il JSON sporco, e rotazione/corrective restano
        # trasparenti. Solo con HOLD attivo (senza buffer completo non e'
        # possibile) e senza tool-call sintetizzate.
        if self.verdict == "content" and self.hold and self._so.enabled and not self._synth:
            self._so_txt = _buffered_answer_text(self.prebuf)
            self._so_tcs: list | None = None
            for self._o in _sse_data_objs(b"".join(self.prebuf)):
                for self._ch in (self._o.get("choices") or []) if isinstance(self._o, dict) else []:
                    self._d = self._ch.get("delta") if isinstance(self._ch, dict) else None
                    self._tc = self._d.get("tool_calls") if isinstance(self._d, dict) else None
                    if self._tc:
                        self._so_tcs = (self._so_tcs or []) + list(self._tc)
            self._so_data = {"choices": [{"message": {"content": self._so_txt, "tool_calls": self._so_tcs}}]}
            self._so_rep = enforce_response(self._so_data, self.payload, self._so)
            self._so_st = self._so_rep.get("status")
            if self._so_st in ("cleaned", "repaired"):
                self._so_new = self._so_data["choices"][0]["message"].get("content")
                if isinstance(self._so_new, str) and self._so_new != self._so_txt:
                    self._so_text = self._so_new
                    self._so_rewrite = True
                metrics.inc("nx_struct_out_total", (self.dep["unique"], self._so_st))
                log.info("[struct-out] stream %s: %s", self.dep["unique"], self._so_st)
                repairlog.note(
                    "struct_cleaned" if self._so_st == "cleaned" else "struct_repaired",
                    source="stream",
                    outcome="ok",
                    dep=self.dep["unique"],
                    model=self.dep.get("model", ""),
                    detail=",".join(self._so_rep.get("moves") or []) or self._so_st,
                )
            elif self._so_st == "invalid":
                self._r5 = self._so_rep.get("reason") or "schema"
                if getattr(gw_state.router.policy, "corrective_retry_enabled", True) and self.dep["unique"] not in self._so_corrected:
                    self._so_corrected.add(self.dep["unique"])
                    self.payload.setdefault("messages", []).append(
                        {"role": "system", "content": _corrective_note("schema")}
                    )
                    metrics.inc("nx_corrective_retry_total", (self.dep["unique"], "schema"))
                    log.warning(
                        "[retry] stream %s contenuto non conforme (%s): retry correttivo", self.dep["unique"], self._r5
                    )
                    repairlog.note(
                        "struct_corrective",
                        source="stream",
                        outcome="ok",
                        dep=self.dep["unique"],
                        model=self.dep.get("model", ""),
                        detail="schema",
                    )
                    self.verdict = "struct_corrective"
                else:
                    metrics.inc("nx_struct_out_total", (self.dep["unique"], "invalid"))
                    log.warning(
                        "[struct-out] stream %s non conforme (%s): ruoto senza cooldown", self.dep["unique"], self._r5
                    )
                    repairlog.note(
                        "struct_invalid",
                        source="stream",
                        outcome="fail",
                        dep=self.dep["unique"],
                        model=self.dep.get("model", ""),
                        detail=self._r5,
                    )
                    self.verdict = "struct_invalid"

    def _quality_check_on_hold(self):
        """QC di contenuto (JSON e anti-vuoto) sull'output bufferizzato, come nel non-streaming."""
        # QC DI CONTENUTO (HOLD): parita' col percorso non-streaming. La
        # risposta e' INTERAMENTE bufferizzata -> si applicano check_response
        # (JSON quando richiesto) e check_sanity (anti-vuoto) come nel
        # non-stream, con corrective JSON sullo stesso dep e rotazione senza
        # penale se non conforme. (D3 'meno peggio' non si applica: in hold
        # non si consegna mai un body rotto, si ruota fino al 503.)
        if self.verdict == "content" and self.hold and not self._synth and (self.qc.enabled or self.san.enabled):

            self._qc_txt = _buffered_answer_text(self.prebuf)
            self._qc_tcs = _merge_qc_tool_calls(b"".join(self.prebuf)) or None
            self._qc_obj = {"choices": [{"message": {"content": self._qc_txt, "tool_calls": self._qc_tcs}}]}
            # QC solo su contenuto REALE: i casi vuoti/zero-answer (e il
            # paracadute -go che trasmette senza contenuto) restano gestiti
            # dalla macchina a verdict, non dalla sanity.
            self._qc_reason = None
            if self._qc_txt.strip() or self._qc_tcs:
                self._qc_reason = check_response(self._qc_obj, self.payload, self.qc) if self.qc.enabled else None
                if not self._qc_reason and self.san.enabled:
                    self._qc_reason = check_sanity(self._qc_obj, self.payload, self.san)
            if self._qc_reason:
                self._ck = _corrective_kind(self._qc_reason)
                if getattr(gw_state.router.policy, "corrective_retry_enabled", True) and self.dep["unique"] not in self._so_corrected:
                    self._so_corrected.add(self.dep["unique"])
                    self.payload.setdefault("messages", []).append({"role": "system", "content": _corrective_note(self._ck)})
                    metrics.inc("nx_corrective_retry_total", (self.dep["unique"], self._ck))
                    log.warning(
                        "[retry] stream %s contenuto non conforme (%s): retry correttivo %s",
                        self.dep["unique"],
                        self._qc_reason,
                        self._ck,
                    )
                    repairlog.note(
                        "struct_corrective",
                        source="stream",
                        outcome="ok",
                        dep=self.dep["unique"],
                        model=self.dep.get("model", ""),
                        detail=self._ck,
                    )
                    self.verdict = "struct_corrective"
                else:
                    metrics.inc("nx_qc_discarded_total", (self.dep["unique"], str(self._qc_reason).split(" ")[0]))
                    log.warning(
                        "[qc] stream %s contenuto non conforme (%s): ruoto senza cooldown",
                        self.dep["unique"],
                        self._qc_reason,
                    )
                    repairlog.note(
                        "struct_invalid",
                        source="stream",
                        outcome="fail",
                        dep=self.dep["unique"],
                        model=self.dep.get("model", ""),
                        detail=str(self._qc_reason)[:60],
                    )
                    self.verdict = "struct_invalid"

    def _commit_content(self):
        """Contenuto valido: registra il successo e termina il ciclo (True)."""
        if self.verdict == "content":
            # risposta reale in arrivo: se questo deployment ha SERVITO in
            # salita (gruppo != richiesto), ricorda il winner come
            # scorciatoia per le prossime richieste di QUEL bucket.
            gw_state.router.note_result(
                self.dep["unique"], (time.monotonic() - self.t_att) * 1000, quality=self._quality, ctx_est=self.ctx, kind="ttft"
            )
            gw_state.router.record_escalation_win(self.requested_group, self.dep)
            gw_state.router.note_session_success(
                self.ses, self.dep["unique"], (time.monotonic() - self.t_att) * 1000, ctx_est=self.ctx, kind="ttft"
            )
            # P2-8: la gara e' decisa; la lease si libera qui (il cap
            # serve a non FAR PARTIRE nuovi tentativi su chiave satura).
            gw_state.router.key_lease_release(self._lease)
            self._lease = None
            return True  # risposta reale in arrivo: si parte

    async def _close_rejected_attempt(self):
        """Chiude il tentativo scartato: scarica lo stream e rilascia inflight e lease."""
        # --- nessun contenuto: rotazione PRE-BYTE ---
        await _discard_stream(self.gen, self.pending)
        gw_state.router.note_end(self.dep["unique"], self.ctx)
        gw_state.router.key_lease_release(self._lease)  # P2-8
        self._lease = None

    def _penalize_verdict(self):
        """Trail, cooldown e rimedi per un verdetto non consegnabile (vuoto, troncato, timeout, struttura)."""
        self._record_verdict_trail()
        self.fr = self.meta.get("finish_reason")
        self.rot_len = getattr(gw_state.router.policy.qc_sanity, "rotate_on_length_empty", False)
        # NON ruotare (e non punire) se il modello HA prodotto reasoning o
        # ha esaurito max_tokens: non e' rotto, ruotare non cambia nulla
        # (tutto il gruppo si comporterebbe uguale) -> 503 retryable diretto.
        self.no_rotate = self.verdict == "empty_eof" and self.meta.get("no_rotate") and not self.rot_len
        # Clamp GATEWAY-side: se il modello ha esaurito il max_tokens che
        # GLI ABBIAMO TAGLIATO NOI (fr=length + cap nostro), la troncatura
        # e' auto-inflitta: ruotare va bene (il dim dopo ha piu' spazio) ma
        # NON declassare il deployment (non e' colpa sua).
        self._gw_clamp_trunc = bool(self._maxtok.get("cap") and self.fr == "length")
        # 0 caratteri in hold (stop/[DONE] puliti senza risposta, o length
        # bruciato tutto in reasoning): il modello NON e' rotto, ha solo
        # finito il budget o risposto vuoto -> si ruota (ladder, poi
        # -go/-fallback) SENZA penale; la penale resta per gli stream
        # VAMENTE rotti (timeout, EOF sporco, length con mezzo answer).
        self._zero_empty = (self.verdict == "empty_eof" and bool(self.meta.get("empty_clean"))) or (
            self.verdict == "length_truncated" and not _buffered_answer_text(self.prebuf)
        )
        self._penalize_rejected_verdict()
        self.over_deadline = (time.monotonic() - self.t_req) * 1000 > int(
            getattr(self.qcp, "stream_total_deadline_ms", 90000) or 90000
        )
        self._on_structure_verdict()
        log.warning(
            "[fallback] stream %s pre-contenuto verdict=%s fr=%s no_rotate=%s -> %s",
            self.dep["unique"],
            self.verdict,
            self.fr,
            bool(self.no_rotate),
            self.nxt["unique"] if self.nxt else "503",
        )

    def _record_verdict_trail(self):
        """Aggiunge al trail l'hop del verdetto (classe e status) per il 503 finale."""
        # ATTEMPT TRAIL anche per i VERDETTI: senza questo hop un 503 con
        # catena esaurita per verdetti (empty_eof/length_truncated/
        # timeout/fake_tool_call/struct_invalid) riportava attempts=[] e
        # nessun X-Scrocco-Trail: il client non capiva QUANTI e QUALI
        # deployment erano stati scartati, e perche'.
        try:
            self._v_cls = (
                "timeout"
                if self.verdict == "timeout"
                else (
                    "struct_invalid"
                    if self.verdict in ("struct_corrective", "struct_invalid")
                    else (
                        self.verdict
                        if self.verdict in ("empty_eof", "length_truncated", "fake_tool_call")
                        else classify_error_class(502, self.verdict)
                    )
                )
            )
            self._v_st = (
                504
                if self.verdict == "timeout"
                else (422 if self.verdict in ("struct_corrective", "struct_invalid") else 502)
            )
            self.trail.append(
                {
                    "ord": len(self.trail) + 1,
                    "dep": self.dep.get("unique"),
                    "group": self.dep.get("group"),
                    "model": self.dep.get("model"),
                    "cls": self._v_cls,
                    "status": self._v_st,
                    "ms": int((time.monotonic() - self.t_att) * 1000),
                }
            )
        except Exception:  # noqa: BLE001
            report_suppressed("main._StreamFallback._record_verdict_trail")

    def _penalize_rejected_verdict(self):
        """Penale del deployment per il verdetto scartato: nessuna per chiusure pulite,
        clamp gateway, tool call finte e output strutturato; timeout lungo; altro soft."""
        if self._zero_empty:
            log.info(
                "[hold] %s: chiusura '%s' senza risposta (fr=%s): nessuna penale, ruoto su candidato piu' capace",
                self.dep["unique"],
                self.verdict,
                self.fr,
            )
        elif self._gw_clamp_trunc:
            log.info(
                "[maxtok] %s: stream vuoto perche' ha esaurito il clamp gateway (%s->%s): nessuna penale, ruoto",
                self.dep["unique"],
                self._maxtok.get("old"),
                self._maxtok.get("cap"),
            )
        elif not self.no_rotate:
            # TIMEOUT (upstream che appende): danno REALE (tempo perso) ->
            # cooldown lungo (timeout_cooldown_mult x classico). Vuoto/
            # troncato: fallimento SOFT -> cooldown corto con escalation
            # dolce sui fallimenti recenti (24h).
            if self.verdict == "timeout":
                self._fail(self.dep["unique"], reason="timeout")
            elif self.verdict == "fake_tool_call":
                # ROTAZIONE SENZA PENALITA' (richiesta esplicita): il modello
                # non e' rotto, ha solo reso la chiamata come testo ->
                # nessun cooldown/streak, si ruota e basta.
                log.info("[fake-tool-call] %s: rotazione senza cooldown", self.dep["unique"])
            elif self.verdict in ("struct_corrective", "struct_invalid"):
                # OUTPUT STRUTTURATO (HOLD): risposta gia' completa e non
                # conforme -> nessuna penale (no cooldown/streak): si
                # ritenta lo stesso dep (corrective) o si ruota.
                log.info("[struct-out] %s: %s senza cooldown", self.dep["unique"], self.verdict)
            else:
                self._fail(self.dep["unique"], seconds=_soft_cd(gw_state.router.stats_for(self.dep["unique"]).fail_count_24h))

    def _on_structure_verdict(self):
        """Output strutturato non conforme: retry correttivo sullo stesso dep o cooldown."""
        if self.verdict == "struct_corrective":
            # retry correttivo: STESSO deployment (la nota di sistema e'
            # gia' stata appesa al payload).
            self.nxt = self.dep
        elif self.verdict == "fake_tool_call":
            self.nxt = (
                gw_state.router.force_escalation(
                    self.dep, self.need, self.ctx, tried=self.tried_set, out_tokens=refill_out_budget(self.payload, gw_state.router.policy)
                )
                if self.profile
                else None
            )
            if self.nxt is None and self.profile and gw_state.router._is_renewal_bucket(str(self.dep.get("group") or "")):
                self.nxt = gw_state.router._free_last_resort(
                    self.dep, self.need, self.ctx, self.tried_set, refill_out_budget(self.payload, gw_state.router.policy), self.requested_group
                )
        else:
            # Su troncatura/risposta-vuota preferiamo un candidato PIU'
            # CAPACE (finestra > corrente, poi intelligence), perche' il
            # problema e'fisicamente lo spazio di output: la scala normale
            # (dim ascendente) resta il fallback se il picker non trova di
            # meglio.
            self._cap_pref = self.verdict == "length_truncated" or self._zero_empty
            self.nxt = (
                None
                if (self.no_rotate or self.over_deadline)
                else (
                    gw_state.router.fallback_next(
                        self.profile,
                        self.dep,
                        self.need,
                        self.scope,
                        ctx=self.ctx,
                        tried=self.tried_set,
                        requested_group=self.requested_group,
                        out_tokens=refill_out_budget(self.payload, gw_state.router.policy),
                        prefer_capable=self._cap_pref,
                    )
                    if self.profile
                    else None
                )
            )

    def _last_resort_after_verdict(self):
        """Nessun prossimo deployment dopo un verdetto scartato: ultima risorsa free, poi 503 retryable.

        Ritorna la risposta finale, oppure None se la catena prosegue."""
        if self.nxt is None or self.tried > self._max_tries:
            # ULTIMA RISORSA: bucket -go/-fallback esaurito -> scendi ai
            # free-dims (warm di chiunque cap-ok, poi canary, poi cooled)
            # pur di non consegnare un 503.
            self._flr = None
            if (
                self.nxt is None
                and self.profile
                and not self.over_deadline
                and gw_state.router._is_renewal_bucket(str(self.dep.get("group") or ""))
            ):
                self._flr = gw_state.router._free_last_resort(
                    self.dep, self.need, self.ctx, self.tried_set, refill_out_budget(self.payload, gw_state.router.policy), self.requested_group
                )
            if self._flr is not None:
                self.nxt = self._flr
            elif self.nxt is None or self.tried > self._max_tries:
                # nessun byte inviato al client -> errore RETRYABLE pulito
                _emit_summary(
                    ses=self.ses or "-",
                    req=self.req or "-",
                    grp=self.dep.get("group"),
                    dep=self.dep.get("unique"),
                    tries=len(self.attempts),
                    fb=max(0, len(self.attempts) - 1),
                    dur_ms=int((time.monotonic() - self.t_req) * 1000),
                    stream=self.client_stream,
                    qc=True,
                    wd="chain-exhausted",
                    ttfb_ms=self.ttfb_ms,
                    usage=None,
                )
                return self._ret(
                    _exhausted(
                        len(self.attempts),
                        "%s (%s)" % (self.verdict, self.fr) if self.fr else self.verdict,
                        prefix_reason=self.prefix_reason,
                        trail=self.trail,
                        retry_at_ms=_retry_at_ms(gw_state.router, self.trail),
                    )
                )

    def _on_upstream_error(self):
        """Dopo un UpstreamError: rimedi sul payload (True = ritenta lo stesso dep),
        classificazione dell'errore, cooldown e scelta del prossimo deployment."""
        if self._try_payload_repairs():
            return True
        self._classify_upstream_error()
        self._penalize_failed_dep()
        self._pick_next_after_error()

    def _try_payload_repairs(self):
        """Rimedi sul payload che ritentano lo STESSO deployment (reasoning replay, content
        array, history originale). True = ritenta."""
        if self._try_reasoning_repairs():
            return True
        if self._try_content_array_repair():
            return True
        if self._try_original_history():
            return True

    def _try_reasoning_repairs(self):
        """Firme media/reasoning e rimedi reasoning-replay sullo stesso deployment. True = ritenta."""
        # ATTEMPT TRAIL: registra l'hop fallito con la sua classe onesta
        # (anche quando il rimedio reasoning piu' sotto lo ritenta).
        try:
            self.trail.append(
                {
                    "ord": len(self.trail) + 1,
                    "dep": self.dep.get("unique"),
                    "group": self.dep.get("group"),
                    "model": self.dep.get("model"),
                    "cls": classify_error_class(self.err.status, self.detail),
                    "status": abs(int(self.err.status)) if self.err.status else None,
                    "ms": int((time.monotonic() - self.t_att) * 1000),
                }
            )
        except Exception:  # noqa: BLE001
            report_suppressed("main._StreamFallback._try_reasoning_repairs#1")
        # P1-5 skipPlatforms: errore PROVIDER-level (5xx/timeout/transport)
        # -> salta TUTTO l'host per questa richiesta.
        try:
            if is_provider_level(classify_error_class(self.err.status, self.detail)):
                self._h = dep_host(self.dep)
                if self._h and self._h not in self.skip_hosts:
                    self.skip_hosts.add(self._h)
                    log.info(
                        "[skip-host] %s: errore provider-level -> host %s saltato per questa richiesta",
                        self.dep["unique"],
                        self._h,
                    )
        except Exception:  # noqa: BLE001
            report_suppressed("main._StreamFallback._try_reasoning_repairs#2")
        # "does not support vision input" (llm7/Cloudflare) su richieste
        # di PURO TESTO: il proxy maschera spesso lo stesso problema del
        # reasoning mancante (i payload reali hanno decine di assistant
        # con tool_calls e zero reasoning_content). Quindi lo trattiamo
        # come candidato replay: prima si ripara e si ritenta LO STESSO
        # dep; se fallisce di nuovo -> cooldown (reason=model_feature).
        self._media_raw = bool(media_reject_signature(self.detail))
        self.media_sig = self._media_raw and media_input_needed(self.need)
        self._rsn_media = media_modality_signature(self.detail) and not self.media_sig and reasoning_err_kind(self.detail) is None
        # FAMIGLIA REASONING (needs/rejects/history): un rimedio per dep,
        # poi si ritenta LO STESSO deployment. Copre il replay del campo
        # `reasoning_content` (opencode zen / deepseek thinking), il
        # provider che lo RIFIUTA (Cloudflare "reasoning_content is
        # unsupported") e la history incoerente col thinking nativo
        # (Anthropic/Gemini). Ruotare non aiuta: tutte le chiavi dello
        # stesso provider rifiutano lo stesso payload.
        self._steps = self._rsn_steps.setdefault(self.dep["unique"], set())
        self._replim = int(getattr(gw_state.router.policy, "repair_exempt_streak_limit", 3) or 0)
        self._rexb = gw_state.router.repair_exempt_blocked(self.dep["unique"], self._replim)
        self._rr = (
            None
            if self._rexb
            else repair_reasoning_error(
                self.payload, self.detail, self.dep, self._steps, self.orig_messages, force_kind=("needs" if self._rsn_media else None)
            )
        )
        if self._rexb:
            log.warning(
                "[reasoning-exempt] %s: budget esenzione esaurito (%d) -> KO normale", self.dep["unique"], self._replim
            )
            # Booking NORMALE: le classi payload/schema da sole non
            # prevedono cooldown, quindi lo applichiamo qui (altrimenti
            # il dep verrebbe ritentato all'infinito su ogni richiesta).
            with contextlib.suppress(Exception):
                self._f24 = gw_state.router.stats_for(self.dep["unique"]).fail_count_24h
                gw_state.router.mark_failed(
                    self.dep["unique"],
                    seconds=_soft_cd(self._f24),
                    reason="repair_exempt_exhausted",
                    status=abs(int(self.err.status)) if self.err.status else None,
                )
        if self._rr == "downgraded":
            self.dep = dict(self.dep)
            self.dep["_no_thinking"] = True  # copia locale, non il CSV
        if self._rr:
            with contextlib.suppress(Exception):
                gw_state.router.note_repair_exempt(self.dep["unique"])
            metrics.inc("nx_reasoning_replay_total", (self._rr,))
            log.warning("[reasoning-%s] %s: rimedio applicato -> ritento lo stesso deployment", self._rr, self.dep["unique"])
            # IMPARA il flag corrispondente: d'ora in poi il CSV lo porta
            # per questo modello (tutti i gemelli) e parte corretto.
            with contextlib.suppress(Exception):
                if self._rr == "repaired":
                    learn_thinking_replay(gw_state.router, self.dep.get("model"))
                elif self._rr == "stripped":
                    learn_strip_reasoning(gw_state.router, self.dep.get("model"))
                elif self._rr == "downgraded":
                    learn_no_thinking(gw_state.router, self.dep.get("model"))
            return True

    def _try_content_array_repair(self):
        """Provider con schema stretto (content array -> stringa): ripara il payload. True = ritenta."""
        # CONTENT ARRAY -> STRING (provider schema stretto, es.
        # Cloudflare Workers AI): 400 "'array' not in 'string'" /
        # "required properties ... 'role,content'". Payload RIPARABILE:
        # impariamo `content_string` (gemelli del modello) e ritentiamo LO
        # STESSO deployment col payload appiattito (media-safe). Se la
        # bonifica non basta (array con media) o il flag c'e' gia', si
        # ricade sulla rotazione di _PAYLOAD_SCHEMA_RE piu' sotto.
        if _CONTENT_ARRAY_RE.search(self.detail):
            self._csteps = self._cstr_steps.setdefault(self.dep["unique"], set())
            # Solo se c'e' DAVVERO qualcosa da appiattire (altrimenti il
            # retry non aiuta: si ricade sulla rotazione piu' sotto).
            self._flat, self._fn = flatten_text_content((self.payload or {}).get("messages"))
            if self._fn and "flatten" not in self._csteps and not self.dep.get("content_string"):
                self._csteps.add("flatten")
                metrics.inc("nx_content_string_total", ("learned",))
                log.warning(
                    "[content-string] %s: 400 schema content-array "
                    "-> imparo content_string e ritento lo stesso "
                    "deployment (%d messaggi)",
                    self.dep["unique"],
                    self._fn,
                )
                with contextlib.suppress(Exception):
                    learn_content_string(gw_state.router, self.dep.get("model"))
                self.dep = dict(self.dep)
                self.dep["content_string"] = True  # copia locale (retry)
                return True

    def _try_original_history(self):
        """Errore oscuro dopo il taglio della history: riprova con la history originale. True = ritenta."""
        # ERRORE "OSCURO" su richiesta reasoning: il taglio del reasoning
        # (histnorm) e' un'ottimizzazione di token; se il provider non ci
        # da' una firma chiara, si ritenta UNA volta lo STESSO deployment
        # con la history ORIGINALE (reasoning intatto). Se l'errore e'
        # chiaro (quota/auth/ban/schema/...) il tentativo non serve.
        if self.orig_messages is not None and not self._rsn_restored and is_unclear_error(self.err.status, self.detail):
            self._nres = restore_reasoning(self.payload, self.orig_messages)
            if self._nres:
                self._rsn_restored = True
                metrics.inc("nx_reasoning_replay_total", ("restored",))
                log.warning(
                    "[reasoning-restore] %s: errore non chiaro (%s) "
                    "-> reasoning ripristinato (%d campi), ritento "
                    "lo stesso deployment",
                    self.dep["unique"],
                    (self.detail or "")[:90],
                    self._nres,
                )
                return True

    def _classify_upstream_error(self):
        """Classifica l'errore: quarantene/cooldown di host e contesto, firme (thought, schema, quota, 401/403...)."""
        # BAN/ToS dell'endpoint (ip_banned / policy_review / Terms of
        # Service): quarantena dell'HOST 24h, cosi' la rotazione non
        # brucia una chiave dietro l'altra dello stesso provider.
        maybe_quarantine_ban(gw_state.router, self.dep, self.err.status, self.detail)
        # 502/503 mid-stream di un aggregatore: e' l'HOST a essere
        # malato -> pausa BREVE dell'host invece di bruciare le chiavi
        # sorelle (elasticita' per un problema transitorio).
        maybe_host_transient_cooldown(gw_state.router, self.dep, self.err.status, self.detail)
        # 413/400 "context length": il provider ha rivelato il VERO
        # limite di input -> ridimensiona il deployment (regola utente).
        note_context_limit(gw_state.router, self.dep, self.err.status, self.detail, self.ctx)
        # QUOTA DI ACCOUNT (Cloudflare & co.): la quota e' dell'account,
        # non della chiave -> metti in pausa TUTTE le chiavi sorelle fino
        # al reset invece di ruotarle a vuoto una per una.
        with contextlib.suppress(Exception):
            maybe_account_quota_cooldown(gw_state.router, self.dep, self.err.status, self.detail)
        # D5 anche in STREAMING: 4xx deployment-side (firma provider-side,
        # modello inesistente oppure 404) -> fallback pre-byte invece di
        # pass-through. Gli altri 4xx restano errori del client.
        self.thought_sig = bool(_THOUGHT_SIG_RE.search(self.detail))
        # CF Workers AI & co.: rifiuto di SCHEMA (content array vs string,
        # messaggio senza content) -> stesso trattamento del
        # thought_signature: ruota SENZA cooldown, mai pass-through finche'
        # c'e' un'alternativa (un provider OpenAI-compatibile lo accetta).
        # Include anche i rifiuti "campo sconosciuto" dei provider severi
        # (Google: "Unknown name \"store\" ... Invalid JSON payload"):
        # incompatibilita' col provider, NON colpa della richiesta ->
        # ruota senza cooldown, mai pass-through del 400 al client (parita'
        # col path non-stream, forwarder._UNKNOWN_FIELD_RE).
        self.schema_sig = bool(_PAYLOAD_SCHEMA_RE.search(self.detail) or _UNKNOWN_FIELD_RE.search(self.detail))
        # Google/Gemini 3 (anche via proxy OpenAI-compat): rifiuto della
        # COMBINAZIONE built-in tools + function calling (il flag
        # tool_config non e' passabile). Stesso trattamento dello schema:
        # ruota SENZA cooldown, mai pass-through; a catena esaurita NON e'
        # "actionable" -> 503 RETRYABLE (il client non puo' farci nulla).
        self.tool_combo_sig = tool_combo_signature(self.detail)
        if self.schema_sig or self.tool_combo_sig:
            self.thought_sig = True  # riusa tutta la logica no-cooldown
        # Rifiuto di MODALITA' (vision/image/audio/…): il modello non e'
        # rotto, semplicemente non accetta quel tipo di input -> ruota
        # SENZA cooldown (un altro deployment multimodale lo accetta),
        # mai pass-through del 400 al client.
        # MA solo se la richiesta HA davvero media: alcuni proxy (llm7/
        # Cloudflare) rispondono "does not support vision input" a
        # richieste di puro testo -> in quel caso il dep e' rotto per
        # QUESTA richiesta e va in cooldown come un KO normale.
        # NB: `_media_raw`/`media_sig` sono gia' calcolati sopra (servono
        # anche al tentativo di replay reasoning).
        self.prov_err = is_provider_error_body(self.detail)  # body {"error":...} & co.
        self.prov_fault = is_provider_fault_body(self.detail)
        # QUOTA: la firma basta da sola. Alcuni provider (Cloudflare
        # Workers AI) usano un envelope {"errors":[{...}]} che NON passa
        # `prov_err`, ma il messaggio di quota e' inequivocabile.
        self.quota_exhausted = (
            bool(_QUOTA_EXHAUSTED_RE.search(self.detail)) if (self.prov_err or abs(int(self.err.status or 0)) == 429) else False
        )
        self.transient = bool(_PROVIDER_TRANSIENT_RE.search(self.detail))
        # 403 di qualsiasi tipo: chiave/progetto rifiutato dal provider ->
        # deployment-side (mai colpa della richiesta), ruota (mai al client).
        self.upstream403 = self.err.status == -403
        # 401 upstream: la NOSTRA chiave e' rifiutata dal provider
        # (assente/invalidata/revocata). E' SEMPRE deployment-side: il
        # client si e' gia' autenticato da noi, quindi non e' colpa sua.
        # Ruota come il 403, mai pass-through.
        self.upstream401 = self.err.status == -401
        self.openai_sig = "bad_response_status_code" in self.detail or "openai_error" in self.detail
        # 4xx con body d'errore ASSENTE/illeggibile (stream appeso ->
        # _safe_aread scaduto): non c'e' alcun messaggio azionabile per il
        # client -> NON e' un errore del client, e' infrastruttura ->
        # ruota + cooldown corto, mai pass-through (503 se catena esaurita).
        self.empty_body = not self.detail.strip() or "body non leggibile" in self.detail.lower() or len(self.detail.strip()) < 12

    def _penalize_failed_dep(self):
        """Cooldown/strike del deployment che ha fallito, secondo la classe dell'errore."""
        self._classify_failure_reason()
        self._classify_negative_status()
        self._cool_down_failed_dep()

    def _classify_failure_reason(self):
        """Motivo deployment-side del fallimento (loop, quota, schema, media...), per log e cooldown."""
        # motivo della classificazione deployment-side (per il log)
        if isinstance(self.err, StreamLoopDetected):
            self.reason = "loop_detected"
        elif self.quota_exhausted:
            # QUOTA prima dello schema: la quota va in cooldown (fino al
            # reset), non ruotata a vuoto senza cooldown.
            self.reason = "quota_exhausted"
        elif self.tool_combo_sig:
            self.reason = "tool_combo"
        elif self.schema_sig:
            self.reason = "payload_schema"
        elif self.media_sig:
            self.reason = "media_reject"
        elif self._media_raw:
            # falso rifiuto di modalita': niente media nella richiesta ->
            # modello rotto per questa richiesta, cooldown normale.
            self.reason = "model_feature"
        elif self.thought_sig:
            self.reason = "thought_signature"
        elif self.prov_err:
            self.reason = "provider_error_body"
        elif self.transient:
            self.reason = "provider_transient"
        elif _MODEL_MISSING_RE.search(self.detail):
            self.reason = "model_missing"
        elif gw_state.router.policy.qc_json.retry_provider_4xx and self.openai_sig:
            self.reason = "openai_error"
        elif self.err.status == -402:
            self.reason = "http_402"
        elif self.empty_body:
            self.reason = "empty_error_body"
        elif self.upstream403:
            self.reason = "upstream_403"
        elif self.upstream401:
            self.reason = "upstream_401"
        elif self.prov_fault:
            self.reason = "provider_fault"
        elif self.err.status == -429:
            # 429 esplicito: quota/chiave satura -> soft per-chiave (F7)
            # e NESSUNA penale reputazionale (record_failure class-aware).
            self.reason = "http_429"
        elif self.err.status is not None and self.err.status < 0:
            self.reason = "other_4xx"
        elif self.err.status is None and "upstream timeout" in self.detail.lower():
            # Upstream che APPENDE (read/connect timeout): danno reale ->
            # cooldown lungo via reason=timeout (timeout_cooldown_mult x).
            self.reason = "timeout"
        else:
            self.reason = "http_%s" % self.err.status if self.err.status else "network"

    def _classify_negative_status(self):
        """Status negativo (4xx del provider): colpa del deployment (ruota, mai pass-through)
        oppure della richiesta; il context length alza la soglia della sessione."""
        if self.err.status is not None and self.err.status < 0:
            self.provider_side = (
                (gw_state.router.policy.qc_json.retry_provider_4xx and self.openai_sig)
                or _MODEL_MISSING_RE.search(self.detail)
                or self.thought_sig
                or self.prov_err
                or self.transient
                or self.upstream403
                or self.upstream401
                or self.empty_body
                or self.prov_fault
                or self._media_raw
                or self.quota_exhausted
                # 429 (anche a status negativo, es. body non-standard di
                # un aggregatore): chiave/quota satura = deployment-side,
                # MAI un errore della richiesta -> ruota, mai pass-through.
                or self.err.status == -429
                or self.err.status == -402
            )
            # né thought_signature né il body d'errore provider né
            # il 403 sono rifiuti di modalita': non alimentano l'auto-
            # learn (hook).
            if (
                self.provider_side
                and self.hook
                and not self.thought_sig
                and not self.prov_err
                and not self.upstream403
                and not self.upstream401
                and not self.prov_fault
                and self.media_sig
            ):
                try:
                    self.hook(self.dep["model"], self.detail)
                except Exception:
                    report_suppressed("main._StreamFallback._classify_negative_status#1")
            if _looks_context_limit(-self.err.status, self.detail):
                # CONTEXT LENGTH: NON passiamo il 400 al client. Alziamo la
                # soglia minima della sessione (le richieste successive
                # partiranno da una dim che contiene il payload) e lasciamo
                # cadere nel flusso di fallback: `_fail` + `_next_filtered`
                # ruotano (e per i dim espliciti la ladder sale di dim).
                self._actual = extract_requested_tokens(self.detail)
                try:
                    gw_state.router.note_session_overflow(self.ses, self._actual or 0)
                except Exception:  # noqa: BLE001
                    report_suppressed("main._StreamFallback._classify_negative_status#2")
                log.warning(
                    "[fallback] stream %s context_length_exceeded (%.90s): alzo la soglia sessione (%s) e ruoto",
                    self.dep["unique"],
                    self.detail,
                    (">=%d tok" % self._actual) if self._actual else "ctx-sconosciuto",
                )

    def _cool_down_failed_dep(self):
        """Applica il cooldown al deployment secondo il motivo (quota fino al reset,
        transiente, 401/403 lunghi, modello assente...) e rilascia lo sticky."""
            # QUALSIASI altro non-200: ruota, mai pass-through al client.
            # (La rotazione termina solo a catena esaurita: a quel punto
            # _actionable_upstream_error consegna lo status vero oppure 503.)
        # Gemini 3 tool replay: ruota SENZA cooldown (vedi _THOUGHT_SIG_RE
        # nel forwarder) — la key Gemini resta sana per il traffico non-tool.
        # model_missing (inesistente/non servito/giu'): 24h fissi.
        if not self.thought_sig and not self.media_sig:
            self._prov_q = None
            if self.reason == "quota_exhausted":
                # Abbonamento flat esaurito: cooldown = tempo al reset
                # (es. "Resets in 9 days" -> ~9gg), non escalation.
                self._cd = parse_quota_reset_seconds(self.detail)
                # Provenienza: 'authoritative' SOLO se il provider ha
                # dichiarato il reset ("Resets in ..."); la nostra stima
                # (mezzanotte UTC) resta 'heuristic' -> la SVEglia puo'
                # comunque tentare il risveglio (regola utente).
                self._prov_q = "authoritative" if _QUOTA_RESET_RE.search(self.detail or "") else "heuristic"
                # Rilascia dep-sticky: questa key NON tornerà prima del
                # reset; la sessione deve ripartire su un'altra chiave.
                if self.ses:
                    self.cur = gw_state.router.dep_sticky_get(self.ses)
                    if self.cur and self.cur == self.dep["unique"]:
                        gw_state.router.dep_sticky_release(self.ses)
            elif self.reason in ("provider_transient", "empty_error_body"):
                self._cd = gw_state.router.escalate_cooldown(
                    fwd.PROVIDER_TRANSIENT_COOLDOWN_S, gw_state.router.stats_for(self.dep["unique"]).fail_count_24h
                )
            elif self.reason == "model_missing":
                self._cd = fwd.MODEL_MISSING_COOLDOWN_S
            elif self.reason == "upstream_403":
                # Key/progetto rifiutato dal provider: cooldown lungo +
                # rilascia lo sticky, la sessione riparte su un'altra key.
                self._cd = fwd.PERMISSION_DENIED_COOLDOWN_S
                if self.ses:
                    self.cur = gw_state.router.dep_sticky_get(self.ses)
                    if self.cur and self.cur == self.dep["unique"]:
                        gw_state.router.dep_sticky_release(self.ses)
            elif self.reason == "upstream_401":
                # Chiave assente/invalidata/revocata: stessa gestione del
                # 403 (cooldown lungo + rilascio sticky).
                self._cd = fwd.PERMISSION_DENIED_COOLDOWN_S
                if self.ses:
                    self.cur = gw_state.router.dep_sticky_get(self.ses)
                    if self.cur and self.cur == self.dep["unique"]:
                        gw_state.router.dep_sticky_release(self.ses)
            elif self.reason == "loop_detected":
                # Loop degenere in streaming: cooldown medio, si ruota
                # subito (un'altra chiave/modello puo' rispondere).
                self._cd = fwd.STREAM_LOOP_COOLDOWN_S
            else:
                self._cd = self.err.retry_after
            # reason propagato solo per il TIMEOUT (mark_failed applica il
            # moltiplicatore dedicato); per gli altri resta il comportamento
            # storico (seconds esplicito / default).
            # reason/status propagati SEMPRE: senza, sul path streaming
            # restavano morti key-soft 429, budget-guard learning,
            # _punish_concurrency e le classi di cooldown (F18).
            if fwd.is_insufficient_balance(self.detail):
                # BILANCIO ESAURITO: ritira il DEPLOYMENT (sblocco manuale).
                if self.ses:
                    self._cur = gw_state.router.dep_sticky_get(self.ses)
                    if self._cur and self._cur == self.dep["unique"]:
                        gw_state.router.dep_sticky_release(self.ses)
                self._fail(self.dep["unique"], reason="insufficient_balance", status=402, kind=fwd.ErrorKind.PERMANENT_DEAD)
                log.warning(
                    "[fallback] stream %s 402 'insufficient balance': DEPLOYMENT RITIRATO (sblocco manuale)",
                    self.dep["unique"],
                )
            else:
                self._fail(
                    self.dep["unique"],
                    seconds=self._cd,
                    reason=self.reason,
                    status=abs(self.err.status) if self.err.status else None,
                    provenance=self._prov_q,
                )

    def _pick_next_after_error(self):
        """Sceglie il prossimo deployment dopo l'errore (mai lo stesso per le firme thought)."""
        self.nxt = (
            self._next_filtered(self.profile, self.dep, self.need, self.scope, ctx=self.ctx, tried=self.tried_set, requested_group=self.requested_group)
            if self.profile
            else None
        )
        if self.nxt is not None and self.thought_sig and self.nxt["unique"] in self.attempts:
            self.nxt = None  # gruppo/catena tutto Gemini 3
        log.warning(
            "[fallback] stream %s %s motivo=%s -> %s :: %.120s",
            self.dep["unique"],
            self.err.status or "conn",
            self.reason,
            self.nxt["unique"] if self.nxt else "nessun alternativo",
            self.detail,
        )

    def _last_resort_after_upstream_error(self):
        """Nessun prossimo deployment dopo un UpstreamError: ultima risorsa free, errore
        azionabile col suo status oppure 503. Ritorna la risposta o None."""
        if self.nxt is None or self.tried > self._max_tries:
            # ULTIMA RISORSA free (solo se -go/-fallback esaurito).
            self._over_dl = (time.monotonic() - self.t_req) * 1000 > int(
                getattr(self.qcp, "stream_total_deadline_ms", 90000) or 90000
            )
            self._flr = None
            if self.nxt is None and self.profile and not self._over_dl and gw_state.router._is_renewal_bucket(str(self.dep.get("group") or "")):
                self._flr = gw_state.router._free_last_resort(
                    self.dep, self.need, self.ctx, self.tried_set, refill_out_budget(self.payload, gw_state.router.policy), self.requested_group
                )
            if self._flr is not None:
                self.nxt = self._flr
            else:
                # errori AZIONABILI (auth/credito/permessi/modello assente/
                # thought_signature) -> status vero. Il resto -> 503.
                if _actionable_upstream_error(self.err) and self.err.status:
                    return self._ret(
                        JSONResponse(
                            status_code=abs(self.err.status),
                            content={"error": {"message": self.err.detail, "type": "upstream_error"}},
                        )
                    )
                _emit_summary(
                    ses=self.ses or "-",
                    req=self.req or "-",
                    grp=self.dep.get("group"),
                    dep=self.dep.get("unique"),
                    tries=len(self.attempts),
                    fb=max(0, len(self.attempts) - 1),
                    dur_ms=int((time.monotonic() - self.t_req) * 1000),
                    stream=self.client_stream,
                    qc=True,
                    wd="chain-exhausted",
                    ttfb_ms=self.ttfb_ms,
                    usage=None,
                )
                return self._ret(
                    _exhausted(
                        len(self.attempts),
                        self.err.detail,
                        prefix_reason=self.prefix_reason,
                        trail=self.trail,
                        retry_at_ms=_retry_at_ms(gw_state.router, self.trail),
                    )
                )

    def _on_unexpected_exception(self):
        """Eccezione inattesa nel tentativo: cooldown soft del deployment e scelta del prossimo."""
        try:
            gw_state.router.note_end(self.dep["unique"], self.ctx)
        except Exception:
            report_suppressed("main._StreamFallback._on_unexpected_exception")
        self._fail(self.dep["unique"], seconds=_soft_cd(gw_state.router.stats_for(self.dep["unique"]).fail_count_24h))
        self.nxt = (
            self._next_filtered(self.profile, self.dep, self.need, self.scope, ctx=self.ctx, tried=self.tried_set, requested_group=self.requested_group)
            if self.profile
            else None
        )
        log.warning(
            "[fallback] stream %s errore imprevisto %r -> %s", self.dep["unique"], self.exc, self.nxt["unique"] if self.nxt else "503"
        )

    def _last_resort_after_exception(self):
        """Nessun prossimo deployment dopo un'eccezione inattesa: ultima risorsa free
        oppure 503. Ritorna la risposta o None."""
        if self.nxt is None or self.tried > self._max_tries:
            self._over_dl = (time.monotonic() - self.t_req) * 1000 > int(
                getattr(self.qcp, "stream_total_deadline_ms", 90000) or 90000
            )
            self._flr = None
            if self.nxt is None and self.profile and not self._over_dl and gw_state.router._is_renewal_bucket(str(self.dep.get("group") or "")):
                self._flr = gw_state.router._free_last_resort(
                    self.dep, self.need, self.ctx, self.tried_set, refill_out_budget(self.payload, gw_state.router.policy), self.requested_group
                )
            if self._flr is not None:
                self.nxt = self._flr
            else:
                _emit_summary(
                    ses=self.ses or "-",
                    req=self.req or "-",
                    grp=self.dep.get("group"),
                    dep=self.dep.get("unique"),
                    tries=len(self.attempts),
                    fb=max(0, len(self.attempts) - 1),
                    dur_ms=int((time.monotonic() - self.t_req) * 1000),
                    stream=self.client_stream,
                    qc=True,
                    wd="chain-exhausted",
                    ttfb_ms=self.ttfb_ms,
                    usage=None,
                )
                return self._ret(
                    _exhausted(
                        len(self.attempts),
                        repr(self.exc)[:160],
                        prefix_reason=self.prefix_reason,
                        trail=self.trail,
                        retry_at_ms=_retry_at_ms(gw_state.router, self.trail),
                    )
                )

    def _fail(self, u, *, seconds=None, reason=None, status=None, provenance=None, kind=None):
        if kind is not None:
            # errore che IMPONE una strategia (es. PERMANENT_DEAD ->
            # retirement): nessun cooldown, la decisione e' del lifecycle.
            return gw_state.router.mark_failed(u, reason=reason, status=status, kind=kind)
        if self._was_dormant:
            _r = gw_state.router.mark_failed_double_residual(u, reason=reason, status=status)
        else:
            _r = gw_state.router.mark_failed(u, seconds=seconds, reason=reason, status=status, provenance=provenance)
        # P1-4: 3 KO dello stesso MODELLO (anche su chiavi diverse) entro
        # la finestra -> bench del modello su tutte le sue chiavi.
        with contextlib.suppress(Exception):
            gw_state.router.note_model_failure(self.dep)
        return _r

    def _next_filtered(self, *a, **k):
        """fallback_next + P1-5: salta gli host che hanno gia' fallito a
        livello provider in QUESTA richiesta; se non ne restano, torna al
        candidato saltato (mai lasciare la richiesta senza risposta)."""
        k.setdefault("out_tokens", refill_out_budget(self.payload, gw_state.router.policy))
        _n = gw_state.router.fallback_next(*a, **k)
        if _n is None or dep_host(_n) not in self.skip_hosts:
            return _n
        _saved = _n
        for _ in range(8):
            self.tried_set.add(_saved["unique"])
            _c = gw_state.router.fallback_next(*a, **k)
            if _c is None:
                return _saved
            if dep_host(_c) not in self.skip_hosts:
                return _c
            _saved = _c
        return _saved

    def _ret(self, resp):
        """Registra dep/attempts/trail finali per il chiamante (redirect
        non-stream) e ritorna la risposta invariata."""
        if self.result_box is not None:
            try:
                self.result_box["dep"] = self.dep
                self.result_box["attempts"] = list(self.attempts)
                # il trail DEVE passare da qui: il redirect non-stream non
                # vede la closure e senza questo finiva con _tr=None -> 503
                # con attempts vuoti e nessun X-Scrocco-Trail.
                self.result_box["trail"] = list(self.trail)
            except Exception:  # noqa: BLE001
                report_suppressed("main._StreamFallback._ret")
        return resp

    def sse(self):
        # Watchdog PASSIVO (D4): conta chunk, rileva [DONE] ed eventi error.
        # NON modifica mai i byte verso il client. Due livelli:
        #   tier1 stream vuoto / evento "error" esplicito -> cooldown subito
        #   tier2 chiuso senza [DONE] -> solo log, cooldown se policy lo vuole
        # + (D2) ri-emissione del prebuffer e coda d'errore SSE finale (verdict C).
        return _ClientRelay(self).run()


async def _stream_with_fallback(
    profile: str | None,
    first_dep: dict,
    payload: dict,
    need: frozenset[str] = frozenset(),
    hook=None,
    scope: str = "chain",
    ctx: int | None = None,
    ses: str | None = None,
    est_chars: int = 0,
    req: str | None = None,
    session: str | None = None,
    client_ip: str = "",
    request: "Request | None" = None,
    attribution: dict | None = None,
    requested_group: str | None = None,
    cold: bool = False,
    prefix_reason: str | None = None,
    orig_messages: list | None = None,
    sniffer=None,
    result_box: dict | None = None,
    client_stream: bool = True,
):
    """Streaming SSE con fallback PRIMA del primo byte inviato al client.

    `result_box`, se fornito, riceve ('dep'/'attempts'/'trail') il deployment
    finale, i tentativi e l'attempt trail (quali hop e con quale classe):
    serve al redirect non-stream->stream sotto hold per la post-elaborazione
    non-stream e per propagare la provenienza al suo 503.
    `client_stream=False` etichetta summary/sniff come non-stream (il client
    reale ha chiesto non-stream)."""
    return await _StreamFallback(profile=profile, first_dep=first_dep, payload=payload, need=need, hook=hook, scope=scope, ctx=ctx, ses=ses, est_chars=est_chars, req=req, session=session, client_ip=client_ip, request=request, attribution=attribution, requested_group=requested_group, cold=cold, prefix_reason=prefix_reason, orig_messages=orig_messages, sniffer=sniffer, result_box=result_box, client_stream=client_stream).run()
