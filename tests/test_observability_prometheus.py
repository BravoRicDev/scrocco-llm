r"""B1/B2 - /metrics di app.observability: tetto agli istogrammi e sintassi
Prometheus VALIDA.

B2 (root cause): `generate_prometheus` costruiva
`prefix = f"{base_name}{{{label_str}}}"` e poi emetteva `f"{prefix}_count"`
-> `nome{label}_count`. Prometheus vuole `nome_suffix{label...}`: il
`nome{label}_count` e' sintassi INVALIDA e il parser di riferimento
rifiuta l'INTERO body ("ValueError: Invalid value: '_count'"), quindi si
perdevono TUTTE le metriche nx_* di app/metrics piu' quelle HTTP dal primo
scrape successivo a una richiesta HTTP. Inoltre i bucket perdevono le label
reali: `le` resta da solo invece di stare insieme a method/path/status.

B1: `observe_histogram` accodava senza tetto -> memoria illimitata per
( path, status ) distinti (i path URL-encoded non hanno cardinalita' finita).

SCELTA DELLE ASSEZIONI (documentata come richiesto): `prometheus_client`
NON e' una dipendenza del venv e NON viene aggiunto a requirements-dev.txt
(il progetto non ha alcuna dipendenza Prometheus: le metriche sono
implementate a mano in app/metrics.py e app/observability.py, e portare
il parser ufficiale come dipendenza di test cambierebbe il profilo del
progetto solo per i test). Le asserzioni sono quindi fatte con regex
sulla forma attesa dal text exposition format:
  - `nome` = `[a-zA-Z_:][a-zA-Z0-9_:]*`
  - ogni riga di sample = `nome {etichette} valore`, con le etichette dentro
    `{...}` e `le` INSIEME alle altre (non in banda);
  - i valori label sono escaped: `\`, `"` -> `\"`, newline -> spazio.
Una riga con `nome{label}_count` VIOLA la forma (il suffisso deve stare
prima di `{`) ed e' quindi esattamente cio' che fallisce prima del fix.
"""
from __future__ import annotations

import re

import pytest

import app.metrics as nx_metrics
import app.observability as obs

# --- forma del text exposition format -----------------------------------
# Nome metrica: lo zio `_count`/`_sum`/`_bucket` sta DENTRO il nome (dopo
# il nome base, PRIMA delle eventuali label).
_NAME = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
_SAMPLE = re.compile(
    rf"^{_NAME}(?:\{{.*\}})? -?[0-9.eE+-]+$", re.MULTILINE)
_SUFFIX = re.compile(
    rf'^({_NAME})_(count|sum|bucket)\{{', re.MULTILINE)
_TYPE = re.compile(rf"^# TYPE ({_NAME}) (counter|gauge|histogram)$",
                   re.MULTILINE)

# Percorso "cattivo": contiene i caratteri che il parser odierno considera
# sintassi dell'esposizione, non letterali da escaping.
EVIL_PATH = '/v1/models?a=1&b="x"\nc'


@pytest.fixture(autouse=True)
def _clean():
    """Ogni test parte da un collettore vuoto (istanza globale condivisa)."""
    c = obs.metrics_collector
    c._counters.clear()
    c._gauges.clear()
    c._histograms.clear()
    c._labels.clear()
    yield
    c._counters.clear()
    c._gauges.clear()
    c._histograms.clear()
    c._labels.clear()


def _malformed(body: str) -> list[str]:
    """Righe che NON sono una riga #TYPE valida ne' un sample valido."""
    bad = []
    for line in body.splitlines():
        if not line or line.startswith("#"):
            if line and line.startswith("#") and not _TYPE.match(line):
                bad.append(line)
            continue
        if not _SAMPLE.match(line):
            bad.append(line)
    return bad


def _assert_valid_prometheus(body: str) -> None:
    """Il body deve essere un'esposizione Prometheus valida, riga per riga."""
    assert body, "corpo vuoto"
    bad = _malformed(body)
    assert not bad, f"righe non valide nel formato Prometheus: {bad}"
    # Nessun suffisso DOPO le label: `nome{...}_count` e' la forma rotta.
    assert not re.search(rf"{_NAME}\{{[^}}]*\}}_(count|sum|bucket)\b", body), (
        "suffisso emesso DOPO le label: nome{label}_count e' Prometheus "
        "invalido")
    # Ogni riga _count/_sum/_bucket ha il suffisso prima di '{'.
    assert _SUFFIX.search(body), "nessuna riga _count/_sum/_bucket emessa"


# --------------------------------------------------------------- B2 (forma)
def test_labelled_histogram_is_valid_prometheus():
    """Con label l'output parsea: il suffisso sta PRIMA delle label e i
    bucket portano `le` INSIEME alle altre label."""
    obs.metrics_collector.observe_histogram(
        "http_request_duration_ms", 12.0,
        {"method": "GET", "path": "/v1/chat/completions", "status": "200"})
    body = obs.metrics_collector.generate_prometheus()

    _assert_valid_prometheus(body)
    # forma esatta attesa
    assert ('http_request_duration_ms_count{method="GET",'
            'path="/v1/chat/completions",status="200"} 1') in body
    assert ('http_request_duration_ms_sum{method="GET",'
            'path="/v1/chat/completions",status="200"} 12.0') in body
    # i bucket NON perdono piu' method/path/status
    assert re.search(
        r'http_request_duration_ms_bucket\{le="25",method="GET",'
        r'path="/v1/chat/completions",status="200"\} 1$', body, re.M)
    assert re.search(
        r'http_request_duration_ms_bucket\{le="\+Inf",method="GET",'
        r'path="/v1/chat/completions",status="200"\} 1$', body, re.M)


def test_production_path_full_scrape_parses():
    """Percorso REALE: record_request_metrics -> scrape intero valido.

    E' il trigger del bug in produzione: una richiesta HTTP passa dal
    middleware, quindi l'istogramma ACQUISCE label e da quel momento
    l'intero /metrics (nx_* compresi) diventa non parsabile."""
    for status in (200, 401, 500):
        obs.record_request_metrics("POST", "/v1/chat/completions",
                                   status, float(status) / 10)
    obs.metrics_collector.inc_counter("nx_requests_total", 1.0,
                                      {"model": "m", "stream": "false"})
    obs.metrics_collector.set_gauge("nx_cooldown_active", 3)

    body = (nx_metrics.render() + obs.render_prometheus())
    _assert_valid_prometheus(body)
    # le metriche nx_* sopravvivono: non vengono piu' perse insieme alle HTTP
    assert "nx_requests_total" in body
    assert "http_request_duration_ms_count" in body
    assert 'status="500"' in body


def test_label_escaping_does_not_corrupt_scrape():
    """Un path con `"` o newline non deve rompere l'esposizione."""
    obs.record_request_metrics("GET", EVIL_PATH, 400, 3.0)
    body = obs.metrics_collector.generate_prometheus()

    _assert_valid_prometheus(body)
    # il valore e' escaped: virgoletta scappata, newline -> spazio: il path
    # resta su UNA riga e ogni riga ha un numero pari di virgolette
    assert '\\"' in body
    assert 'path="/v1/models?a=1&b=\\"x\\" c"' in body
    for line in body.splitlines():
        assert line.count('"') % 2 == 0, line


def test_istogramma_senza_label_invariato():
    """Il caso senza label (prefix == base_name) resta identico: e' il
    contratto di tests/test_metrics_endpoint.py::test_histogram_buckets_in_ms."""
    obs.metrics_collector.observe_histogram("h", 2.0)
    obs.metrics_collector.observe_histogram("h", 2000.0)
    body = obs.metrics_collector.generate_prometheus()
    _assert_valid_prometheus(body)
    assert "h_count 2" in body and "h_sum 2002.0" in body
    assert re.search(r'_bucket\{le="5"\} 1', body)
    assert re.search(r'_bucket\{le="1000"\} 1', body)
    assert re.search(r'_bucket\{le="2500"\} 2', body)
    assert re.search(r'_bucket\{le="\+Inf"\} 2', body)


# ------------------------------------------------------------------- B1
def test_histogram_observations_are_capped(monkeypatch):
    """Tetto per-key: N osservazioni non ne lasciano piu' di _hist_max."""
    monkeypatch.setattr(obs.metrics_collector, "_hist_max", 8)
    labels = {"method": "GET", "path": "/v1/x", "status": "200"}
    for i in range(500):
        obs.metrics_collector.observe_histogram("h", float(i), labels)
    key = obs.metrics_collector._make_key("h", labels)
    assert len(obs.metrics_collector._histograms[key]) == 8

    # il tetto e' PER-KEY, non globale: due path distinti non si rubano slot
    other = {"method": "GET", "path": "/v1/y", "status": "200"}
    for i in range(3):
        obs.metrics_collector.observe_histogram("h", 1.0, other)
    assert len(obs.metrics_collector._histograms[
        obs.metrics_collector._make_key("h", other)]) == 3


def test_histogram_cap_env_configurable(monkeypatch):
    """OBSERVABILITY_HIST_MAX e' letto dalla env all'istanziazione."""
    monkeypatch.setenv("OBSERVABILITY_HIST_MAX", "3")
    assert obs.MetricsCollector()._hist_max == 3
    monkeypatch.setenv("OBSERVABILITY_HIST_MAX", "0")
    c0 = obs.MetricsCollector()
    assert c0._hist_max == 0
    # 0 = istogrammi disattivati del tutto (niente accumulo)
    c0.observe_histogram("h", 1.0, {"a": "b"})
    assert c0._histograms == {}
    # default quando la env e' assente/vuota
    monkeypatch.delenv("OBSERVABILITY_HIST_MAX", raising=False)
    assert obs.MetricsCollector()._hist_max == 512
    monkeypatch.setenv("OBSERVABILITY_HIST_MAX", "")
    assert obs.MetricsCollector()._hist_max == 512


def test_histogram_cap_default_is_512():
    """Il default e' lo stesso tetto di app/metrics.py::_LATENCY_MAX."""
    c = obs.MetricsCollector()
    assert c._hist_max == 512 == nx_metrics._LATENCY_MAX


def test_cap_preserves_count_sum_coherent(monkeypatch):
    """Dopo il taglio count/sum/bucket restano COERENTI col buffer
    troncato: il tetto perde le osservazioni piu' vecchie, mai la
    consistenza del testo esposto."""
    monkeypatch.setattr(obs.metrics_collector, "_hist_max", 5)
    labels = {"method": "GET", "path": "/v1/z", "status": "200"}
    for i in range(50):
        obs.metrics_collector.observe_histogram("h", float(i), labels)
    key = obs.metrics_collector._make_key("h", labels)
    kept = obs.metrics_collector._histograms[key]
    assert kept == [45.0, 46.0, 47.0, 48.0, 49.0]   # LRU: piu' recenti

    body = obs.metrics_collector.generate_prometheus()
    _assert_valid_prometheus(body)
    m = re.search(r'h_count\{[^}]*\} (\d+)', body)
    s = re.search(r'h_sum\{[^}]*\} ([0-9.]+)', body)
    inf = re.search(r'h_bucket\{le="\+Inf"[^}]*\} (\d+)', body)
    b50 = re.search(r'h_bucket\{le="50"[^}]*\} (\d+)', body)
    b5 = re.search(r'h_bucket\{le="5"[^}]*\} (\d+)', body)
    assert m and s and inf and b50 and b5
    assert int(m.group(1)) == len(kept) == 5
    assert float(s.group(1)) == sum(kept) == 235.0
    assert int(inf.group(1)) == len(kept)          # +Inf == count
    assert int(b5.group(1)) == sum(1 for v in kept if v <= 5) == 0
    assert int(b50.group(1)) == sum(1 for v in kept if v <= 50) == 5
