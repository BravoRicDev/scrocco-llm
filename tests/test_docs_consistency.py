"""FASE 5 — coerenza docs <-> route <-> tool MCP.

Verifica:
1. ogni endpoint `(METHOD /admin/...)` citato in AGENT.md/OPERATIONS.md esiste
   come route col metodo combaciante (cattura es. `GET /admin/pressure/inspect`
   mentre la route e' POST);
2. un solo handler per `(method, path)` su `/admin/*` e su `/metrics`
   (cattura il doppio `POST /admin/reload` e il doppio `/metrics`);
3. il conteggio tool citato nei doc == `len(_mcp_tool_specs())`;
4. i nomi canonici elencati in AGENT.md sono un sottoinsieme di
   `_mcp_known_names()` (e sono esattamente quelli delle spec).
"""
import re
from collections import Counter
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from app.admin import _mcp_known_names, _mcp_tool_specs

REPO = Path(__file__).resolve().parents[1]
DOC_ENDPOINTS = ("docs/AGENT.md", "docs/OPERATIONS.md")
METHODS = "GET|POST|PUT|PATCH|DELETE"
ENDPOINT_RE = re.compile(
    r"\b((?:" + METHODS + r")(?:/(?:" + METHODS + r"))*)\s+"
    r"(`?/admin[/A-Za-z0-9_\-{}\[\]]*)")


def _iter_api_routes(app):
    out = []

    def walk(routes):
        for r in routes:
            if isinstance(r, APIRoute):
                out.append(r)
            else:
                orig = getattr(r, "original_router", None)
                if orig is not None:
                    walk(orig.routes)
                elif hasattr(r, "routes"):
                    walk(r.routes)

    walk(app.routes)
    return out


def _norm(path):
    path = path.strip("`").split("?")[0].split("[")[0]
    path = re.sub(r"\{[^}]*\}", "{}", path)
    return path.rstrip("/") or "/"


@pytest.fixture(scope="module")
def api():
    import app.main as m
    return m.app


def test_doc_endpoints_esistono(api):
    by_method = {}
    for r in _iter_api_routes(api):
        for meth in r.methods:
            by_method.setdefault(meth, []).append(_norm(r.path))
    for doc in DOC_ENDPOINTS:
        text = (REPO / doc).read_text()
        for mo in ENDPOINT_RE.finditer(text):
            docpath = _norm(mo.group(2))
            for meth in mo.group(1).split("/"):
                cands = by_method.get(meth, [])
                ok = any(c == docpath or c.startswith(docpath + "/")
                         for c in cands)
                assert ok, f"{doc}: {meth} {docpath} non esiste come route"


def test_single_handler_per_method_path(api):
    counts = Counter((meth, r.path)
                     for r in _iter_api_routes(api)
                     for meth in r.methods)
    dups = {k: v for k, v in counts.items()
            if v > 1 and (k[1].startswith("/admin/") or k[1] == "/metrics")}
    assert dups == {}, f"handler duplicati: {dups}"


def test_conteggio_tool_nei_doc():
    expected = len(_mcp_tool_specs())
    for doc in ("docs/AGENT.md", "README.md"):
        text = (REPO / doc).read_text()
        nums = re.findall(r"(\d+)\s+tools\b", text)
        nums += re.findall(r"Canonical tool names \((\d+)\)", text)
        assert nums, f"{doc}: nessun conteggio tool citato"
        assert all(int(x) == expected for x in nums), (doc, nums, expected)


def test_nomi_canonici_agent_su_known():
    text = (REPO / "docs/AGENT.md").read_text()
    block = text.split("Canonical tool names", 1)[1].split("Legacy aliases",
                                                          1)[0]
    names = re.findall(r"`([a-z][a-z0-9_]*)`", block)
    known = _mcp_known_names()
    missing = [n for n in names if n not in known]
    assert not missing, f"nomi non noti: {missing}"
    assert len(names) == len(_mcp_tool_specs())
