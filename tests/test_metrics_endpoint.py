"""Fase 1: /metrics unificato.

Verifica il fix dello shadowing della route /metrics (era registrata due
volte: observability vinceva e gli nx_* non erano mai esposti), i bucket
istogramma in millisecondi e la copertura declare() dei contatori.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.metrics as nx_metrics
import app.observability as obs


@pytest.fixture()
def client(monkeypatch, tmp_path):
    csv = tmp_path / "k.csv"
    csv.write_text(
        "commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,caps\n"
        "seed,openai/gpt-4o-mini,openai,https://api.openai.com/v1,"
        "free,8,8000,1,sk-test-key,\n"
    )
    import app.main as m
    orig_mk = m.authn.master_key
    m.authn.master_key = "test-master-metrics"
    orig_csv = m.config.csv_path
    monkeypatch.setattr(m.config, "csv_path", csv)
    m.config.reload()
    yield TestClient(m.app), m
    m.router._cooldown.clear()
    m.authn.master_key = orig_mk
    m.config.csv_path = orig_csv
    m.config.reload()


@pytest.fixture(autouse=True)
def _clean_metrics():
    nx_metrics.reset()
    obs.metrics_collector._counters.clear()
    obs.metrics_collector._gauges.clear()
    obs.metrics_collector._histograms.clear()
    obs.metrics_collector._labels.clear()
    yield


def test_single_metrics_route(client):
    _c, m = client
    routes = [r for r in m.app.routes if getattr(r, "path", "") == "/metrics"]
    assert len(routes) == 1, f"attese 1 route /metrics, trovate {len(routes)}"


def test_metrics_endpoint_exposes_nx_and_http(client):
    c, _m = client
    # una richiesta prima: il middleware registra http_* DOPO aver servito la
    # risposta, quindi il primo scrape non contiene ancora se stesso.
    c.get("/healthz")
    r = c.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "nx_uptime_seconds" in body
    assert "nx_cooldown_active" in body
    assert "http_requests_total" in body


def test_metrics_router_gauges_emitted(client):
    c, _m = client
    body = c.get("/metrics").text
    assert "nx_cooldown_active" in body
    assert "nx_sticky_active" in body


def test_declared_labels_rendered(client):
    _c, _m = client
    nx_metrics.inc("nx_struct_out_total", ("d", "cleaned"))
    body = nx_metrics.render()
    assert 'nx_struct_out_total{unique="d",status="cleaned"} 1' in body


def test_undeclared_metric_autoreg(client):
    _c, _m = client
    nx_metrics.inc("nx_probe_x", ("a", "b"))
    body = nx_metrics.render()
    assert 'nx_probe_x{label0="a",label1="b"} 1' in body
    assert "nx_probe_x{}" not in body


def test_no_duplicate_type_lines(client):
    _c, _m = client
    nx_metrics.inc("nx_requests_total", ("m1", "false"))
    nx_metrics.inc("nx_requests_total", ("m2", "true"))
    body = nx_metrics.render()
    assert body.count("# TYPE nx_requests_total ") == 1


def test_histogram_buckets_in_ms(client):
    _c, _m = client
    obs.metrics_collector.observe_histogram("http_request_duration_ms", 2.0)
    obs.metrics_collector.observe_histogram("http_request_duration_ms", 2000.0)
    body = obs.metrics_collector.generate_prometheus()
    assert 'le="5"' in body
    assert 'le="1000"' in body
    assert 'le="2500"' in body
    assert 'le="0.05"' not in body
    # 2.0 -> <=5 ; 2000.0 -> <=2500 ma non <=1000
    assert re.search(r'_bucket\{le="5"\} 1', body)
    assert re.search(r'_bucket\{le="1000"\} 1', body)
    assert re.search(r'_bucket\{le="2500"\} 2', body)
    assert re.search(r'_bucket\{le="\+Inf"\} 2', body)


def test_label_values_escaped(client):
    _c, _m = client
    nx_metrics.inc("nx_qc_discarded_total", ('uniq"x', "reason"))
    body = nx_metrics.render()
    assert '\\"' in body


def test_all_inc_names_declared():
    root = Path(nx_metrics.__file__).resolve().parent
    pat = re.compile(r'metrics\.inc\(\s*"([a-z_][a-z0-9_]*)"')
    missing: set[str] = set()
    for src in root.rglob("*.py"):
        for name in pat.findall(src.read_text(encoding="utf-8")):
            if name not in nx_metrics._metric_labels:
                missing.add(name)
    assert not missing, f"metriche inc() senza declare(): {sorted(missing)}"


def test_dead_router_metrics_removed():
    assert not hasattr(obs, "record_router_metrics")
    assert not hasattr(obs, "record_circuit_breaker_metrics")
    body = obs.metrics_collector.generate_prometheus()
    assert "router_selections_total" not in body
    assert "circuit_breaker_state_changes_total" not in body
