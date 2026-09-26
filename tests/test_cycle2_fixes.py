"""Regressioni ciclo 2: K1 router ctx, K2 video status, K3 gemini thinking,
K4 schemaout fence, K5 imagestore cap.

Ogni test fallisce sul codice pre-fix e passa dopo. File self-contained:
costruisce le proprie fixture (CSV temporaneo, MockTransport).
"""
import asyncio
import json

import httpx
import pytest

from app.config import GatewayConfig
from app.forwarder import Forwarder, RETRYABLE_STATUS, UpstreamError
from app.imagestore import configure as images_configure
from app.imagestore import get as images_get
from app.imagestore import put as images_put
from app.imagestore import stats as images_stats
from app.policy import Policy
from app.protocols import chat_to_gemini
from app.router import Router
from app.schemaout import clean_json_content

_CSV = (
    "commento,modello,provider,endpoint,data,context,max_input,priority,"
    "scrocco-llm-test,caps\n"
    "t@x.com,m/solo,pA,https://a.example/v1,free,0,200000,0,K1,image_gen\n"
    "t@x.com,m/solo,pB,https://b.example/v1,free,0,1000000,0,K2,image_gen\n"
)


def _router(tmp_path):
    p = tmp_path / "k.csv"
    p.write_text(_CSV)
    return Router(GatewayConfig(str(p), proxy_prefix="scrocco-llm-", seed=1),
                  Policy.from_dict({}))


# ------------------------------------------------------------------ K1
def test_k1_fallback_group_rispetta_ctx(tmp_path):
    """La rotazione di fallback su un gruppo capacita' non deve scegliere un
    deployment con max_input < contesto richiesto (altrimenti 413)."""
    r = _router(tmp_path)
    grp, ctx = "scrocco-llm-test-image_gen", 900000
    first = r.initial_pick("test", grp, None, ctx)
    assert first["max_input_tokens"] == 1000000
    nxt = r.fallback_next("test", first, frozenset({"image_gen"}), "group",
                          ctx=ctx, tried={first["unique"]})
    assert nxt is None, "scelto un dep con max_input < ctx (413 garantito)"


def test_k1_ctx_none_invariato(tmp_path):
    """Senza stima di contesto il comportamento resta quello di prima."""
    r = _router(tmp_path)
    grp = "scrocco-llm-test-image_gen"
    first = r.initial_pick("test", grp, None, 900000)
    nxt = r.fallback_next("test", first, frozenset({"image_gen"}), "group",
                          ctx=None, tried={first["unique"]})
    assert nxt is not None, "ctx=None deve continuare a ruotare"


# ------------------------------------------------------------------ K2
def _fwd(handler):
    return Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))


_DEP = {"api_base": "https://x.example/v1", "api_key": "k", "unique": "u",
        "provider": "p", "model": "m"}


@pytest.mark.parametrize("code,retryable", [(404, False), (429, True),
                                            (500, True), (503, True)])
def test_k2_poll_video_status_follows_taxonomy(code, retryable):
    """status>0 = ritriabile come negli altri 5 metodi (prima era sempre <0,
    quindi un 503 diventava 502 al client e la tassonomia era incoerente)."""
    f = _fwd(lambda r: httpx.Response(code, json={"error": "x"}))
    with pytest.raises(UpstreamError) as ei:
        asyncio.run(f.poll_video(_DEP, "J"))
    st = ei.value.status
    assert st == (code if retryable else -code)
    assert (st is not None and st > 0) == (code in RETRYABLE_STATUS)


def test_k2_download_video_status_follows_taxonomy():
    f = _fwd(lambda r: httpx.Response(503, text="down"))
    with pytest.raises(UpstreamError) as ei:
        asyncio.run(f.download_video(_DEP, "J"))
    assert ei.value.status == 503


def test_k2_poll_any_salta_404_e_ritrova_il_job():
    """Il 404 (job su un altro account) resta il caso che fa proseguire."""
    seen = []

    def h(r):
        seen.append(r.headers.get("authorization"))
        return httpx.Response(200, json={"id": "J", "status": "completed"}) \
            if len(seen) > 1 else httpx.Response(404, json={"error": "no"})

    f = _fwd(h)
    deps = [dict(_DEP, api_key="A", unique="uA"),
            dict(_DEP, api_key="B", unique="uB")]
    out = asyncio.run(f.poll_video_any(deps, "J"))
    assert out["status"] == "completed" and len(seen) == 2


# ------------------------------------------------------------------ K3
def test_k3_gemini_thinking_budget_sotto_max_output():
    """Google rifiuta l'INTERA chiamata se thinkingBudget >= maxOutputTokens."""
    for mt in (5600, 2048, 64):
        out = chat_to_gemini({"messages": [{"role": "user", "content": "x"}],
                              "max_tokens": mt, "reasoning_effort": "high"}, {})
        g = out["generationConfig"]
        assert g["thinkingConfig"]["thinkingBudget"] < g["maxOutputTokens"]


def test_k3_gemini_budget_invariato_senza_max_tokens():
    out = chat_to_gemini({"messages": [{"role": "user", "content": "x"}],
                          "reasoning_effort": "high"}, {})
    g = out["generationConfig"]
    assert "maxOutputTokens" not in g
    assert g["thinkingConfig"]["thinkingBudget"] == 8192


def test_k3_gemini_senza_effort_nessun_thinking_config():
    out = chat_to_gemini({"messages": [{"role": "user", "content": "x"}],
                          "max_tokens": 100}, {})
    assert "thinkingConfig" not in out["generationConfig"]


# ------------------------------------------------------------------ K4
def test_k4_fence_dentro_valore_stringa_non_distrugge_il_json():
    v = '{"summary": "esempio: ```json\\n{\\"ok\\": true}\\n``` ecco", "n": 1}'
    out = clean_json_content(v)
    assert out is not None, "JSON valido scartato per una fence in un valore"
    assert json.loads(out) == json.loads(v)


def test_k4_comportamento_invariato_su_fence_esterna_e_prosa():
    assert clean_json_content('```json\n{"a": 1}\n```') is not None
    assert clean_json_content('ecco il risultato: {"a": 1}') == '{"a": 1}'
    assert clean_json_content("42") is None


# ------------------------------------------------------------------ K5
def test_k5_put_rifiuta_item_oltre_budget():
    """put() deve tornare un id SOLO se l'item e' davvero in store: prima
    restituiva un id per un item appena evictato -> url 404 silenzioso."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4000
    images_configure(max_bytes=100)
    try:
        assert images_put(png, "image/png") is None
        assert images_stats()["items"] == 0
    finally:
        images_configure(max_bytes=536870912)


def test_k5_put_sotto_budget_resta_scaricabile():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4000
    images_configure(max_bytes=1_000_000)
    try:
        fid = images_put(png, "image/png")
        assert fid and images_get(fid) is not None
    finally:
        images_configure(max_bytes=536870912)
