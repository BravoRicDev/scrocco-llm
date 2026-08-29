"""Unit tests for app.logview (pure log parsing, no TestClient needed)."""

from __future__ import annotations

import os
import tempfile

from app import logview


# Righe di esempio realistiche (formato di var/gateway.log / error-audit.log).
def _sample_calls_lines() -> list[str]:
    return [
        '2026-08-27 01:09:20,100 INFO nx.main [route] routed req=scrocco-llm-x to dep-1',
        '2026-08-27 01:09:21,200 INFO nx.main [summary] '
        '{"ses":"-","req":"r1","grp":"g1","dep":"dep-A__modelX__1",'
        '"model":"modelX","tries":1,"fb":0,"dur_ms":4471,"stream":true,'
        '"qc":false,"wd":null,"ttfb_ms":839,"usage":null}',
        '2026-08-27 01:09:22,300 INFO nx.main [summary] '
        '{"ses":"-","req":"r2","grp":"g2","dep":"dep-B__modelY__2",'
        '"model":"modelY","tries":2,"fb":1,"dur_ms":1200,"stream":false,'
        '"qc":true,"wd":null,"ttfb_ms":100,"usage":null,"status":200}',
        '2026-08-27 01:09:23,400 INFO nx.main [identity] session abc opened',
        # riga rumore: non matcha il regex (troppo corta)
        'rumore qualunque',
        # riga summary con JSON NON valido: deve essere saltata
        '2026-08-27 01:09:24,500 INFO nx.main [summary] questo non e json{',
    ]


def _sample_error_lines() -> list[str]:
    return [
        '2026-08-27 01:09:09,863 WARNING nx.erroraudit status=-401 '
        ':: {"type":"error","error":{"type":"AuthError",'
        '"message":"Invalid API key."}}',
        '2026-08-27 01:09:10,384 WARNING nx.erroraudit status=-400 '
        ':: {"error":{"type":"server_error",'
        '"message":"Model is unavailable."}}',
        '2026-08-27 01:09:11,031 WARNING nx.erroraudit status=-401 '
        ':: {"type":"error","error":{"type":"AuthKeyError",'
        '"message":"Auth key revoked."}}',
        '2026-08-27 01:09:12,308 WARNING nx.erroraudit status=-500 '
        ':: {"error":{"type":"upstream_error",'
        '"message":"Upstream timeout."}}',
        # riga non-JSON: error_message cade sul testo grezzo troncato
        '2026-08-27 01:09:13,000 WARNING nx.erroraudit status=-418 '
        ':: not-json-audit-line',
    ]


def test_parse_summary_extracts_and_skips_nonjson():
    lines = _sample_calls_lines()
    events = logview.parse_summary_lines(
        lines, {"summary", "route", "identity", "fallback"}, since=None, tail=100)
    # 2 summary valide + 1 route + 1 identity = 4; la riga non-json e il rumore spariscono
    assert len(events) == 4
    summaries = [e for e in events if e["tag"] == "summary"]
    assert len(summaries) == 2
    by_dep = {e["dep"]: e for e in summaries}
    a = by_dep["dep-A__modelX__1"]
    assert a["model"] == "modelX"
    assert a["dur_ms"] == 4471
    assert a["tries"] == 1
    assert a["fb"] == 0
    b = by_dep["dep-B__modelY__2"]
    assert b["model"] == "modelY"
    assert b["dur_ms"] == 1200
    assert b["fb"] == 1
    assert b["status"] == 200
    # la riga non-json non deve essere presente
    assert not any(e["tag"] == "summary" and e["dep"] is None for e in summaries)
    # i tag non-summary tengono il testo grezzo, campi strutturati None
    route_ev = [e for e in events if e["tag"] == "route"][0]
    assert route_ev["model"] is None
    assert route_ev["raw"]["text"].startswith("routed req=")


def test_parse_summary_since_filter():
    lines = _sample_calls_lines()
    # cutoff dopo la prima summary (01:09:21,200) -> tiene solo la seconda
    since = logview._parse_ts("2026-08-27 01:09:21,500")
    events = logview.parse_summary_lines(
        lines, {"summary"}, since=since, tail=100)
    assert len(events) == 1
    assert events[0]["dep"] == "dep-B__modelY__2"


def test_parse_error_numeric_filter():
    lines = _sample_error_lines()
    events = logview.parse_error_lines(lines, filt="-401", since=None, tail=100)
    assert len(events) == 2
    assert all(e["status"] == -401 for e in events)
    assert {e["error_type"] for e in events} == {"AuthError", "AuthKeyError"}


def test_parse_error_substring_filter_case_insensitive():
    lines = _sample_error_lines()
    events = logview.parse_error_lines(lines, filt="authkey", since=None, tail=100)
    # match case-insensitive su error_type "AuthKeyError" (riga -401)
    assert len(events) == 1
    assert events[0]["status"] == -401
    assert events[0]["error_type"] == "AuthKeyError"

    # substring su message
    events2 = logview.parse_error_lines(lines, filt="unavailable", since=None, tail=100)
    assert len(events2) == 1
    assert events2[0]["status"] == -400


def test_parse_error_fallback_to_raw_text():
    lines = _sample_error_lines()
    events = logview.parse_error_lines(lines, filt="-418", since=None, tail=100)
    assert len(events) == 1
    assert events[0]["error_type"] is None
    assert events[0]["error_message"] == "not-json-audit-line"[:300]


def test_parse_error_tail_limits():
    lines = _sample_error_lines()
    events = logview.parse_error_lines(lines, filt=None, since=None, tail=2)
    assert len(events) == 2
    # gli ultimi 2 per ts crescente: -500 e -418
    assert events[-1]["status"] == -418
    assert events[0]["status"] == -500


def test_read_tail_lines_ordering_and_limit(tmp_path):
    main = tmp_path / "gateway.log"
    old = tmp_path / "gateway.log.1"
    old.write_text("\n".join(f"OLD {i}" for i in range(5)) + "\n")
    main.write_text("\n".join(f"NEW {i}" for i in range(20)) + "\n")
    # solo il principale esiste come path principale -> prendi ultime 6
    paths = [str(old), str(main)]
    lines = logview._read_tail_lines(paths, max_lines=6)
    assert len(lines) == 6
    assert lines[-1] == "NEW 19"
    # ordine cronologico crescente
    assert lines == sorted(lines)
    # quando il principale basta, non servono righe dal .1
    assert not any(l.startswith("OLD") for l in lines)


def test_read_tail_lines_fills_from_rotated(tmp_path):
    main = tmp_path / "gateway.log"
    old = tmp_path / "gateway.log.1"
    old.write_text("\n".join(f"OLD {i}" for i in range(10)) + "\n")
    main.write_text("\n".join(f"NEW {i}" for i in range(3)) + "\n")
    paths = [str(old), str(main)]
    # servono 8 righe totali: 3 dal principale + 5 dal .1 (piu' recenti)
    lines = logview._read_tail_lines(paths, max_lines=8)
    assert len(lines) == 8
    assert lines[-1] == "NEW 2"
    assert lines[0] == "OLD 5"  # le 5 piu' recenti del .1
    # ordine cronologico: prima le 5 piu' recenti del .1 (OLD 5..9), poi NEW 0..2
    assert lines[:5] == [f"OLD {i}" for i in range(5, 10)]
    assert lines[5:] == [f"NEW {i}" for i in range(3)]


def test_parse_summary_extra_fields():
    lines = [
        '2026-08-27 01:10:00,100 INFO nx.main [summary] '
        '{"ses":"-","req":"r9","grp":"g1","dep":"dep-A__modelX__1",'
        '"dur_ms":900,"tries":2,"fb":1,"qc":true,"status":null,'
        '"via":"api.llm7.io","ttfb_ms":350,"wd":"quota","stream":true,'
        '"kind":"text"}',
        '2026-08-27 01:10:02,200 INFO nx.main [route] routed req=x to y',
    ]
    events = logview.parse_summary_lines(
        lines, {"summary", "route"}, since=None, tail=100)
    sums = [e for e in events if e["tag"] == "summary"]
    assert len(sums) == 1
    a = sums[0]
    assert a["via"] == "api.llm7.io"
    assert a["ttfb_ms"] == 350
    assert a["wd"] == "quota"
    assert a["stream"] is True
    assert a["qc"] is True
    assert a["kind"] == "text"


def test_parse_summary_extra_fields_default_none():
    # riga summary senza campi extra -> i nuovi campi sono None, non KeyError
    lines = [
        '2026-08-27 01:11:00,100 INFO nx.main [summary] '
        '{"ses":"-","req":"r","grp":"g","dep":"d__m__0",'
        '"dur_ms":10,"tries":1,"fb":0}',
    ]
    events = logview.parse_summary_lines(
        lines, {"summary"}, since=None, tail=100)
    assert len(events) == 1
    for key in ("via", "ttfb_ms", "wd", "stream", "kind", "extra"):
        assert key in events[0]
        assert events[0][key] is None
