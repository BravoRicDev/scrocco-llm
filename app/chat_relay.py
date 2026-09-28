"""Relay SSE verso il client di una richiesta streaming (`_ClientRelay`).

[IT] Riceve dal motore stream (app/chat_stream.py) il deployment vincente e
consegna i byte al client: prebuffer, watchdog passivo (tier1 vuoto/errore,
tier2 senza [DONE]), disconnessione del client, verdetti di fine stream,
coda d'errore SSE e riga [summary]. Estratto da app/chat_stream.py senza
modifiche di logica (logger "nx.main").

[EN] Client-facing SSE relay of one streaming request.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from . import forwarder as fwd
from . import metrics
from . import state as gw_state
from .chat_helpers import _cached_tokens_of, _emit_summary, _note_fb_refund
from .fakecall import TemplateTokenStripper
from .forwarder import StreamLoopDetected, _length_truncated_should_fail
from .router import _prompt_chars
from .sse_utils import _answer_chars, _collapse_sse_content, _sse_data_objs, _strip_sse_content
from .stream_verdicts import _discard_stream, _payload_text_empty, _soft_cd
from .suppressed import report_suppressed

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
