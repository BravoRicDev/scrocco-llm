"""Test routing consapevole delle capacità con GatewayConfig su CSV temporaneo."""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test
t@x.com,openai/gpt-oss-120b,groq,https://api.groq.com/openai/v1,free,128,8000,0,sk-FAKE-KEY-12345678
t@x.com,qwen/qwen2.5-vl-72b,groq,https://api.groq.com/openai/v1,free,128,8000,5,sk-FAKE-KEY-12345678
t@x.com,meta-llama/llama-4-scout,nvidia,https://integrate.api.nvidia.com/v1,fallback,128,8000,0,sk-FAKE-KEY-12345678
"""

POLICY_MAP = {"capability_routing": {"model_capabilities": {
    "openai/gpt-oss-120b": [text := "text", "tools"],
    "*qwen2.5-vl-*": [text, "vision"],
    "*llama-4-scout*": [text, "vision", "video"],
}}}

MSG_TEXT = [{"role": "user", "content": "ciao"}]
MSG_VISION = [{"role": "user", "content": [
    {"type": "text", "text": "descrivi"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]}]


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    pol = Policy.from_dict(POLICY_MAP)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, pol)
    os.unlink(path)


def test_dep_supports(router):
    deps = router.config.groups["scrocco-llm-test-128k"]
    oss = next(d for d in deps if "gpt-oss" in d["model"])
    vl = next(d for d in deps if "-vl-" in d["model"])
    assert router._dep_supports(oss, frozenset({"text"}))
    assert not router._dep_supports(oss, frozenset({"vision"}))
    assert router._dep_supports(vl, frozenset({"vision"}))
    assert router._dep_supports(oss, frozenset())          # need vuoto = ok


def test_capable_dims_filters(router):
    dims_all = router.config.profile_dims["test"]
    dims_vis = router._capable_dims("test", frozenset({"vision"}))
    assert dims_vis == dims_all            # entrambi i dep 128k hanno vision-capabili? no:
    # gpt-oss non è vision; qwen sì -> il gruppo 128k resta capace
    dims_vid = router._capable_dims("test", frozenset({"video"}))
    assert dims_vid == []                  # nessun free-group ha video


def test_route_text_unchanged(router):
    got = router.resolve_group_for_request("scrocco-llm-test", MSG_TEXT,
                                           None, frozenset())
    assert got == "scrocco-llm-test-128k"


def test_route_vision_picks_capable_group(router):
    got = router.resolve_group_for_request("scrocco-llm-test", MSG_VISION,
                                           None, frozenset({"vision"}))
    assert got == "scrocco-llm-test-128k"


def test_route_video_falls_back_to_fallback_group(router):
    got = router.resolve_group_for_request("scrocco-llm-test", MSG_VISION,
                                           None, frozenset({"video"}))
    assert got == "scrocco-llm-test-fallback"


def test_route_impossible_returns_none(router):
    msgs = [{"role": "user", "content": [{"type": "input_audio"}]}]
    got = router.resolve_group_for_request("scrocco-llm-test", msgs, None,
                                           frozenset({"audio"}))
    assert got is None


def test_pick_deployment_never_returns_incapable(router):
    for _ in range(30):
        d = router.pick_deployment("scrocco-llm-test-128k",
                                   frozenset({"vision"}))
        assert "-vl-" in d["model"]


def test_pick_deployment_no_candidate_returns_none(router):
    assert router.pick_deployment("scrocco-llm-test-fallback",
                                  frozenset({"audio"})) is None


def test_fallback_after_skips_incapable(router):
    nxt = router.fallback_after("test", None, frozenset({"vision"}))
    assert nxt is not None and "gpt-oss" not in nxt["unique"]
    # partendo dal deployment incapace, il successivo deve essere capace
    chain = router.config.chains["test"]
    oss = next(u for u in chain if "gpt-oss" in u)
    nxt2 = router.fallback_after("test", oss, frozenset({"vision"}))
    assert nxt2 is None or "gpt-oss" not in nxt2["unique"]


def test_explicit_incapable_passthrough_with_warning(router, caplog):
    got = router.resolve_group_for_request("scrocco-llm-test-128k",
                                           MSG_VISION, None,
                                           frozenset({"audio"}))
    assert got == "scrocco-llm-test-128k"          # rispettato alla lettera
    # semantica scala: il warning di incapabilità arriva dal percorso ladder
    assert any(("[caps]" in r.message or "[ladder]" in r.message)
               and "pass-through" in r.message.lower()
               for r in caplog.records)


def test_sticky_bypassed_when_unfit(router):
    sid = "sess-1"
    router.sticky_set(sid, "scrocco-llm-test-128k")
    # sticky group NON ha audio -> bypass: cerca altrove (fallback group no audio
    # nemmeno lui) => None; ma se il gruppo sticky fosse capace verrebbe usato.
    msgs = [{"role": "user", "content": [{"type": "input_audio"}]}]
    got = router.resolve_group_for_request("scrocco-llm-test", msgs, sid,
                                           frozenset({"audio"}))
    assert got is None


def test_sticky_used_when_fit(router):
    sid = "sess-2"
    router.sticky_set(sid, "scrocco-llm-test-128k")
    got = router.resolve_group_for_request("scrocco-llm-test", MSG_VISION,
                                           sid, frozenset({"vision"}))
    assert got == "scrocco-llm-test-128k"


def test_estimate_tokens_counts_images():
    from app.router import estimate_tokens
    base = estimate_tokens(MSG_TEXT, 4)
    with_img = estimate_tokens(MSG_VISION, 4, image_token_estimate=800)
    assert with_img >= base + 800 - 10      # margine per i caratteri del testo


def test_routing_disabled_legacy_behavior(router):
    router.policy.capability_routing_enabled = False
    msgs = [{"role": "user", "content": [{"type": "input_audio"}]}]
    got = router.resolve_group_for_request("scrocco-llm-test", msgs, None,
                                           frozenset())
    # senza filtro si torna al gruppo minimo sufficiente standard
    assert got == "scrocco-llm-test-128k"


def test_route_tts_falls_back_to_capable_group(router):
    # nessun modello tts nei gruppi dim -> catena -fallback -> None (nessuno dichiara tts)
    got = router.resolve_group_for_request("scrocco-llm-test", [], None,
                                           frozenset({"tts"}))
    assert got is None


def test_chat_implicit_text_excludes_stt_only(router):
    # un modello dichiarato SOLO [stt] non deve mai servire chat testuali
    p = router.policy
    p.model_capabilities["*whisper*"] = ["stt"]
    assert p.caps_for("openai/whisper-base") == frozenset({"stt"})
    assert not router._dep_supports({"model": "openai/whisper-base"},
                                    frozenset({"text"}))


# --------------------------------------------------- fallback di SCOPO (non-text)
def test_fallback_after_never_lands_on_incapable_in_chain(router):
    # con need=vision, la catena totale NON deve mai restituire gpt-oss (no vision)
    seen = set()
    failed = None
    for _ in range(10):
        nxt = router.fallback_after("test", failed, frozenset({"vision"}))
        if nxt is None:
            break
        assert "-vl-" in nxt["unique"] or "llama-4" in nxt["unique"]
        seen.add(nxt["unique"])
        failed = nxt["unique"]
    assert seen      # almeno un candidato capace trovato


def test_fallback_next_group_scope_stays_in_group_and_excludes(router):
    # semantica SCALA: da un dims la rotazione sale mai scende; nel fixture
    # 128k è l'unica dim -> prosegue nei bucket di coda (-go assente, -fallback)
    first = router.pick_deployment("scrocco-llm-test-128k", None)
    nxt = router.fallback_next("test", first, frozenset({"vision"}),
                               scope="group")
    if nxt is not None:
        assert nxt["group"] in ("scrocco-llm-test-128k",
                                "scrocco-llm-test-fallback")
        assert nxt["unique"] != first["unique"]
    # fallback_next su gruppo inesistente -> None
    ghost = router.fallback_next("test",
                                 {"unique": "zz", "group": "scrocco-llm-test-999k"},
                                 frozenset(), scope="group")
    assert ghost is None
    # fallback_next su gruppo inesistente -> None
    ghost = router.fallback_next("test",
                                 {"unique": "zz", "group": "scrocco-llm-test-999k"},
                                 frozenset(), scope="group")
    assert ghost is None


def test_capability_chains_structural_empty_without_caps(router):
    # NUOVA semantica: chains_cap deriva dalla colonna CSV "caps";
    # il fixture non ha colonna caps -> nessun gruppo capacità
    assert router.capability_chains("test") == {}
    assert router.capability_groups_counts("test") == {}


def test_pick_deployment_exclude(router):
    g = "scrocco-llm-test-128k"
    first = router.pick_deployment(g, None)
    assert first is not None
    for _ in range(20):
        nxt = router.pick_deployment(g, None, exclude=first["unique"])
        if nxt is not None:
            assert nxt["unique"] != first["unique"]
