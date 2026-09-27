"""Contatori runtime in formato testo Prometheus (GET /metrics).

Tutto in memoria, reset a restart (i valori storici vivono nei log/Grafana).
Thread-safety sufficiente: GIL sulle letture/scritture atomiche di dict.

[EN] WHAT: in-memory Prometheus text counters (GET /metrics). WHY no
persistence: history lives in logs/Grafana, not in the gateway.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import threading
import time
from collections import OrderedDict, defaultdict
from typing import Any

log = logging.getLogger("nx.metrics")

_lock = threading.Lock()
_started = time.time()

_counters: dict[str, dict[tuple[str, ...], float]] = defaultdict(
    lambda: defaultdict(float))
_gauges: dict[str, Any] = {}
# latenze per unique: somme e conteggi (media calcolata all'export).
# I5: LRU limitata — senza tetto la cardinalita' di
# nx_upstream_latency_ms{unique=...} cresceva per sempre (scrape pesanti,
# Grafana che esplode). 512 unique in volo sono piu' che sufficienti.
# Configurabile via env METRICS_LATENCY_MAX.
_LATENCY_MAX = int(os.environ.get("METRICS_LATENCY_MAX", "512") or "512")
_latency_sum: "OrderedDict[str, float]" = OrderedDict()
_latency_count: "OrderedDict[str, float]" = OrderedDict()


# Replica multi-worker (app/cluster.py): un worker che RIESEGUE l'osservazione
# di un altro non deve contarla due volte. Dentro `muted()` le scritture sono
# ignorate (i contatori restano quelli del worker che ha servito davvero).
_muted: contextvars.ContextVar[bool] = contextvars.ContextVar("nx_metrics_muted", default=False)


@contextlib.contextmanager
def muted():
    token = _muted.set(True)
    try:
        yield
    finally:
        _muted.reset(token)


def inc(name: str, labels: tuple[str, ...] = (), value: float = 1.0) -> None:
    if _muted.get():
        return
    with _lock:
        _counters[name][labels] += value
        # Auto-registrazione: una metrica CON label non dichiarata otterrebbe
        # nomi placeholder in render() (o serie duplicate). Registriamo qui
        # nomi deterministici e logghiamo: un declare() esplicito e' meglio.
        if labels and name not in _metric_labels:
            _metric_labels[name] = [f"label{i}" for i in range(len(labels))]
            log.warning("[metrics] metrica %r non dichiarata: label "
                        "auto-registrate %s (aggiungere declare())",
                        name, _metric_labels[name])


def set_gauge(name: str, value: float) -> None:
    if _muted.get():
        return
    with _lock:
        _gauges[name] = value


def observe_latency_ms(unique: str, ms: float) -> None:
    if _muted.get():
        return
    with _lock:
        _latency_sum[unique] = _latency_sum.get(unique, 0.0) + ms
        _latency_count[unique] = _latency_count.get(unique, 0.0) + 1
        _latency_sum.move_to_end(unique)        # I5: LRU (il piu' recente in coda)
        _latency_count.move_to_end(unique)
        while len(_latency_sum) > _LATENCY_MAX:
            old, _ = _latency_sum.popitem(last=False)
            _latency_count.pop(old, None)


def render() -> str:
    """Formato testo exposition Prometheus.

    Sotto il lock si COPIANO solo i valori (veloce); la formattazione, che
    con centinaia di serie e' la parte costosa, avviene fuori: le inc() dei
    thread (offload, flush) non aspettano lo scrape."""
    with _lock:
        counters = [(name, sorted(series.items())) for name, series in _counters.items()]
        gauges = list(_gauges.items())
        latencies = [(u, s, _latency_count.get(u) or 1) for u, s in _latency_sum.items()]
    lines = [
        "# TYPE nx_uptime_seconds gauge",
        f"nx_uptime_seconds {time.time() - _started:.0f}",
    ]
    for name, series in sorted(counters, key=lambda kv: kv[0]):
        lines.append(f"# TYPE {name} counter")
        for labels, v in series:
            lbl = ""
            if labels:
                names = _label_names(name)
                if len(names) != len(labels):
                    # declare() assente o arity incoerente: non perdere
                    # mai la serie (nomi placeholder deterministici).
                    names = [f"label{i}" for i in range(len(labels))]
                parts = ",".join(
                    f'{k}="{_safe_label(str(val))}"'
                    for k, val in zip(names, labels))
                lbl = "{" + parts + "}"
            lines.append(f"{name}{lbl} {v}")
    for name, v in sorted(gauges):
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {v}")
    if latencies:
        lines.append("# TYPE nx_upstream_latency_ms gauge")
        for u, s, n in sorted(latencies):
            lines.append(f'nx_upstream_latency_ms{{unique="{_safe_label(u)}"}} '
                         f"{s / n:.0f}")
    return "\n".join(lines) + "\n"


def reset() -> None:
    """Reset per test e azione admin /admin/metrics/reset."""
    with _lock:
        _counters.clear()
        _gauges.clear()
        _latency_sum.clear()
        _latency_count.clear()


def snapshot(names: tuple[str, ...] = ()) -> dict[str, dict[tuple[str, ...], float]]:
    """Copia leggibile dei contatori richiesti (per /admin/state e TUI)."""
    with _lock:
        if names:
            return {n: {k: v for k, v in _counters.get(n, {}).items()}
                    for n in names}
        return {n: {k: v for k, v in series.items()}
                for n, series in _counters.items()}


def _label_names(metric: str) -> list[str]:
    """Nomi label dichiarati alla prima inc() via convenzione metric{a,b}."""
    return _metric_labels.get(metric, [])


_metric_labels: dict[str, list[str]] = {}


def declare(metric: str, *labels: str) -> None:
    _metric_labels[metric] = list(labels)


def _safe_label(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


# dichiarazione nomi label (ORDINE = ordine tuple nelle inc() corrispondenti)
declare("nx_requests_total", "model", "stream")
declare("nx_group_total", "group")
declare("nx_upstream_calls_total", "unique", "result")
declare("nx_qc_discarded_total", "unique", "reason")
declare("nx_qc_watchdog_total", "unique", "tier")
declare("nx_caps_requests_total", "capability")
declare("nx_caps_unroutable_total", "capability")
declare("nx_images_total", "group", "result")
declare("nx_videos_total", "group", "result")
declare("nx_tts_total", "group", "result")
declare("nx_stt_total", "group", "result")
declare("nx_systemone_total", "group", "result")
declare("nx_stt_scrubbed_total", "group")
declare("nx_hedge_total", "tier")
declare("nx_wake_sweep_total", "result")
declare("nx_coalesce_total", "result")
declare("nx_reasoning_replay_total", "result")
declare("nx_thinking_replay_total", "result")
declare("nx_content_string_total", "result")
declare("nx_client_fields_stripped_total", "field")
declare("nx_opencode_headers_total", "result")
declare("nx_learn_flag_total", "flag", "result")
declare("nx_json_sse_total", "provider")
# --- Contatori incrementati senza declare(): senza nomi di label render()
# emetteva "{}" per ogni serie -> serie identiche duplicate e label perse.
declare("nx_cache_audit_total", "audit")
declare("nx_cache_hit_requests_total")
declare("nx_chain_503_total", "reason")
declare("nx_content_sanitized_total", "unique")
declare("nx_corrective_retry_total", "unique", "kind")
declare("nx_ctx_compacted_forced")
declare("nx_ctxcompact_tool_total", "tool")
declare("nx_ctxcompact_total", "status")
declare("nx_ctx_overflow_total", "group")
declare("nx_fake_toolcall_total", "unique", "result")
declare("nx_go_refund_total", "kind")
declare("nx_histnorm_total", "result")
declare("nx_loop_detected_total", "unique", "reason")
declare("nx_max_tokens_clamped")
declare("nx_repair_events_total", "family", "kind", "outcome")
declare("nx_resp_format_injected_total", "unique")
declare("nx_sess_est_fallback_total")
declare("nx_sess_est_samples_total")
declare("nx_sess_est_used_total")
declare("nx_slow_flag_total", "action")
declare("nx_slow_race_total", "reason")
declare("nx_struct_out_total", "unique", "status")
declare("nx_template_tokens_stripped_total", "unique")
declare("nx_text_toolcall_total", "unique", "result")
declare("nx_tool_repair_total", "unique", "result")
declare("nx_toolrepair_truncated_total", "tool")
declare("nx_truncated_toolcall_total", "unique", "result")
declare("nx_zen_dim_stay")
