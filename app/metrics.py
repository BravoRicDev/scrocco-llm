"""Contatori runtime in formato testo Prometheus (GET /metrics).

Tutto in memoria, reset a restart (i valori storici vivono nei log/Grafana).
Thread-safety sufficiente: GIL sulle letture/scritture atomiche di dict.

[EN] WHAT: in-memory Prometheus text counters (GET /metrics). WHY no
persistence: history lives in logs/Grafana, not in the gateway.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any

_lock = threading.Lock()
_started = time.time()

_counters: dict[str, dict[tuple[str, ...], float]] = defaultdict(
    lambda: defaultdict(float))
_gauges: dict[str, Any] = {}
# latenze per unique: somme e conteggi (media calcolata all'export)
_latency_sum: dict[str, float] = defaultdict(float)
_latency_count: dict[str, float] = defaultdict(float)


def inc(name: str, labels: tuple[str, ...] = (), value: float = 1.0) -> None:
    with _lock:
        _counters[name][labels] += value


def set_gauge(name: str, value: float) -> None:
    with _lock:
        _gauges[name] = value


def observe_latency_ms(unique: str, ms: float) -> None:
    with _lock:
        _latency_sum[unique] += ms
        _latency_count[unique] += 1


def render() -> str:
    """Formato testo exposition Prometheus."""
    lines = [
        "# TYPE nx_uptime_seconds gauge",
        f"nx_uptime_seconds {time.time() - _started:.0f}",
    ]
    with _lock:
        for name, series in sorted(_counters.items()):
            lines.append(f"# TYPE {name} counter")
            for labels, v in sorted(series.items()):
                lbl = ""
                if labels:
                    parts = ",".join(
                        f'{k}="{v}"' for k, v in zip(_label_names(name),
                                                     labels))
                    lbl = "{" + parts + "}"
                lines.append(f"{name}{lbl} {v}")
        for name, v in sorted(_gauges.items()):
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {v}")
        if _latency_sum:
            lines.append("# TYPE nx_upstream_latency_ms gauge  # media")
            for u, s in sorted(_latency_sum.items()):
                n = _latency_count.get(u) or 1
                safe = _safe_label(u)
                lines.append(f'nx_upstream_latency_ms{{unique="{safe}"}} '
                             f"{s / n:.0f}")
    return "\n".join(lines) + "\n"


def reset() -> None:
    """Solo per i test."""
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
        _latency_count.clear()


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
