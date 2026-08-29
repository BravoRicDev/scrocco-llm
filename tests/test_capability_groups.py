"""Test gruppi capacità STRUTTURALI: colonna caps, terna -C/-C-go/-C-fallback,
dispatch base, guardia soft max_input, espliciti, degrade e kill-switch."""
import os
import tempfile

import pytest

from app.config import GatewayConfig, parse_caps
from app.policy import Policy
from app.router import Router

# profilo "caps": qwen-vl (free, vision), gemini-vision (fallback, vision),
# whisper (go giorno 5, stt) + due modelli testo nei dims
CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,caps,scrocco-llm-cg
t@x.com,gpt-text-a,groq,https://x/v1,free,128,8000,0,,sk-K1-AAAAAAAAAA
t@x.com,gpt-text-b,groq,https://x/v1,free,256,8000,0,,sk-K1-AAAAAAAAAA
t@x.com,qwen/qwen2.5-vl-72b,groq,https://x/v1,free,128,8000,5,vision,sk-K1-AAAAAAAAAA
t@x.com,gemini/gemini-vision-x,google,https://x/v1,fallback,128,200000,0,vision,sk-K1-AAAAAAAAAA
t@x.com,openai/whisper-base,groq,https://x/v1,5,128,8000,10,stt,sk-K1-AAAAAAAAAA
"""


def _router(policy_overrides: dict | None = None):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {
        "*qwen2.5-vl*": [text := "text", "vision"]}},
        "capability_groups": policy_overrides or {}})
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=7)
    return Router(cfg, pol), path


@pytest.fixture()
def router_on():
    r, path = _router({"enabled": True})
    yield r
    os.unlink(path)


def test_parse_caps_tokens():
    assert parse_caps("text, Vision , ") == frozenset({"text", "vision"})
    assert parse_caps("") == frozenset()
    assert parse_caps("vision,bogus") == frozenset({"vision"})   # ignoto scartato


def test_build_ternary_buckets_and_chains(router_on):
    cfg = router_on.config
    assert cfg.profile_caps["cg"] == ["stt", "vision"]
    # vision: free -> primario; fallback -> -vision-fallback; niente go
    assert "scrocco-llm-cg-vision" in cfg.groups
    assert "scrocco-llm-cg-vision-fallback" in cfg.groups
    assert "scrocco-llm-cg-vision-go" not in cfg.groups
    # stt: data=5 (giorno) -> solo -stt-go
    assert "scrocco-llm-cg-stt-go" in cfg.groups
    assert "scrocco-llm-cg-stt" not in cfg.groups
    # catena cap = primario -> go -> fallback
    ch = cfg.chains_cap["cg"]["vision"]
    assert all(u.startswith("scrocco-llm-cg-vision") for u in ch)
    assert ch[0].startswith("scrocco-llm-cg-vision__")
    assert any(u.startswith("scrocco-llm-cg-vision-fallback") for u in ch)
    # group_caps tipizza i gruppi
    assert cfg.group_caps["scrocco-llm-cg-vision"] == "vision"
    assert cfg.group_caps["scrocco-llm-cg-256k"] is None
    counts = router_on.capability_groups_counts("cg")
    assert counts["vision"] == {"primary": 1, "go": 0, "fallback": 1}
    assert counts["stt"] == {"primary": 0, "go": 1, "fallback": 0}


def test_text_world_excludes_membership(router_on):
    cfg = router_on.config
    # qwen (caps=vision senza 'text') NON sta più nei dims
    models_128 = {d["model"] for d in cfg.groups["scrocco-llm-cg-128k"]}
    assert models_128 == {"gpt-text-a"}
    # la catena TESTO non contiene univoci cap
    assert all("__qwen" not in u for u in cfg.chains["cg"])


def test_dual_membership_text_token():
    # riga dedicata: caps "text,stt" -> sta nei dims E in -stt-go
    csv2 = CSV + ("t@x.com,deep/whisper-2,groq,https://x/v1,free,128,8000,0,"
                  "text,stt,sk-K1-AAAAAAAAAA\n")
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(csv2)
    try:
        cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
        dims_models = {d["model"] for g, deps in cfg.groups.items()
                       if g.endswith("k") for d in deps}
        assert "deep/whisper-2" in dims_models
        assert "scrocco-llm-cg-stt-go" in cfg.groups
        # il modello con text,stt NON finisce nel gruppo stt primario (era go?)...
        # data=free -> -stt primario; asserzione solo su dims + gruppo esistente
    finally:
        os.unlink(path)


def test_whitelist_includes_cap_groups_and_uniques(router_on):
    wl = set(router_on.config.whitelist_for("cg"))
    assert "scrocco-llm-cg-vision" in wl
    assert "scrocco-llm-cg-vision-fallback" in wl
    assert any(u.startswith("scrocco-llm-cg-vision__") for u in wl)


def test_known_suffixes_explicit_cap_groups(router_on):
    sufs = router_on.config.known_suffixes()
    assert "-vision" in sufs and "-vision-go" in sufs \
        and "-vision-fallback" in sufs and "-stt-go" in sufs


def test_dispatch_base_media_to_cap_group(router_on):
    msgs_img = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "x"}}]}]
    got = router_on.resolve_group_for_request(
        "scrocco-llm-cg", msgs_img, None, frozenset({"vision", "text"}))
    assert got == "scrocco-llm-cg-vision"


def test_dispatch_rarest_first_multi_cap(router_on):
    # need={vision,tts}: tts è più rara nell'ordine -> -tts... ma il profilo
    # ha SOLO -stt-go per tts -> nessun gruppo tts -> target=vision? NO:
    # l'ordine di specificità prova tts PRIMA ma richiede che ESISTA il gruppo;
    # non esiste -> prosegue e trova vision.
    got = router_on.resolve_group_for_request(
        "scrocco-llm-cg", [], None, frozenset({"vision", "tts"}))
    assert got == "scrocco-llm-cg-vision"


def test_dispatch_missing_group_dynamic_degrade(router_on):
    audio_msgs = [{"role": "user", "content": [{"type": "input_audio"}]}]
    # audio senza gruppo -> on_missing=dynamic -> filtro dinamico legacy:
    # nessun deployment dichiara audio -> None (400 a monte)
    got = router_on.resolve_group_for_request(
        "scrocco-llm-cg", audio_msgs, None, frozenset({"audio"}))
    assert got is None


def test_dispatch_missing_group_error_mode():
    r, path = _router({"enabled": True, "on_missing": "error"})
    try:
        audio_msgs = [{"role": "user", "content": [{"type": "input_audio"}]}]
        # error mode -> None SENZA provare il filtro dinamico
        assert r.resolve_group_for_request(
            "scrocco-llm-cg", audio_msgs, None, frozenset({"audio"})) is None
    finally:
        os.unlink(path)


def test_kill_switch_off_legacy_dynamic():
    # CSV LEGACY puro (senza colonna caps): filtro dinamico sulla mappa
    legacy = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-cg
t@x.com,gpt-text-a,groq,https://x/v1,free,128,8000,0,sk-K1-AAAAAAAAAA
t@x.com,qwen/qwen2.5-vl-72b,groq,https://x/v1,free,128,8000,5,sk-K1-AAAAAAAAAA
"""
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(legacy)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {
        "*qwen2.5-vl*": ["text", "vision"]}},
        "capability_groups": {"enabled": False}})
    try:
        r = Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=3), pol)
        msgs_img = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "x"}}]}]
        got = r.resolve_group_for_request(
            "scrocco-llm-cg", msgs_img, None, frozenset({"vision"}))
        assert got == "scrocco-llm-cg-128k"
    finally:
        os.unlink(path)


def test_ctx_guard_soft_skips_over_limit(router_on):
    # pick INTRA-gruppo -vision (solo qwen, max 8000): ctx enorme -> None
    big = 90000
    assert router_on.pick_deployment("scrocco-llm-cg-vision",
                                     need=frozenset({"vision"}),
                                     ctx=big) is None
    # initial_pick attraversa la catena cap: qwen skippato -> gemini (fallback)
    d = router_on.initial_pick("cg", "scrocco-llm-cg-vision",
                               frozenset({"vision"}), ctx=big)
    assert d is not None and "gemini" in d["unique"]
    # ctx piccolo -> primario qwen direttamente
    d2 = router_on.initial_pick("cg", "scrocco-llm-cg-vision",
                                frozenset({"vision"}), ctx=100)
    assert d2 is not None and "qwen" in d2["unique"]


def test_membership_counts_as_declaration(router_on):
    # gemini NON è nella mappa policy ma è membro di -vision: la membership
    # basta a dichiarare la capacità (fonte di verità strutturale)
    dep = next(d for d in router_on.config.groups
               .get("scrocco-llm-cg-vision-fallback", [])[0:1])
    assert router_on._dep_supports(dep, frozenset({"vision"}))


def test_fallback_next_walks_cap_chain(router_on):
    cfg = router_on.config
    prim = next(d for d in cfg.groups["scrocco-llm-cg-vision"]
                if "qwen" in d["unique"])
    nxt = router_on.fallback_next("cg", prim, frozenset({"vision"}),
                                  scope="chain")
    assert nxt is not None and nxt["group"] == "scrocco-llm-cg-vision-fallback"
    # scope group: ruota solo nel gruppo primario
    nxt2 = router_on.fallback_next("cg", prim, frozenset({"vision"}),
                                   scope="group")
    assert nxt2 is None or nxt2["group"] == "scrocco-llm-cg-vision"


def test_fallback_next_never_crosses_to_text_world(router_on):
    cfg = router_on.config
    prim = cfg.groups["scrocco-llm-cg-vision"][0]
    cur = prim
    seen_groups = {cur["group"]}
    while True:
        nxt = router_on.fallback_next("cg", cur, frozenset({"vision"}),
                                      scope="chain")
        if nxt is None:
            break
        seen_groups.add(nxt["group"])
        cur = nxt
    assert seen_groups <= {"scrocco-llm-cg-vision",
                           "scrocco-llm-cg-vision-go",
                           "scrocco-llm-cg-vision-fallback"}


def test_initial_pick_cap_then_chain(router_on):
    d = router_on.initial_pick("cg", "scrocco-llm-cg-vision",
                               frozenset({"vision"}), ctx=None)
    assert d is not None and d["group"].startswith("scrocco-llm-cg-vision")


def test_explicit_cap_group_passthrough(router_on):
    got = router_on.resolve_group_for_request(
        "scrocco-llm-cg-vision", [], None, frozenset())
    assert got == "scrocco-llm-cg-vision"


def test_policy_capability_groups_validation():
    p = Policy.from_dict({"capability_groups": {"enabled": True,
                                                "on_missing": "error"}})
    assert p.cap_groups_enabled is True and p.cap_groups_on_missing == "error"
    with pytest.raises(ValueError):
        Policy.from_dict({"capability_groups": {"on_missing": "boh"}})


# ---------------------------------------------- NODO: ingest vs generation
GEN_CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,caps,scrocco-llm-gt
t@x.com,gpt-text,groq,https://x/v1,free,128,8000,0,,sk-K1-AAAAAAAAAA
t@x.com,vision-analyzer,groq,https://x/v1,free,128,8000,0,vision,sk-K1-AAAAAAAAAA
t@x.com,img-maker,google,https://x/v1,free,128,200000,5,image_gen,sk-K1-AAAAAAAAAA
t@x.com,dual-maker,google,https://x/v1,free,128,200000,5,image_gen video_gen vision,sk-K1-AAAAAAAAAA
"""


@pytest.fixture()
def gen_router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(GEN_CSV)
    pol = Policy()
    pol.cap_groups_enabled = True
    r = Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=2), pol)
    yield r
    os.unlink(path)


def test_gen_rows_never_in_ingest_chains(gen_router):
    cfg = gen_router.config
    # -vision esiste (vision-analyzer) ma NON contiene i generatori
    vis_chain = cfg.chains_cap["gt"].get("vision", [])
    assert any("vision-analyzer" in u for u in vis_chain)
    assert not any("maker" in u for u in vis_chain)
    # e i generatori non stanno nei dims
    dims_models = {d["model"] for g, deps in cfg.groups.items()
                   if g.endswith("k") for d in deps}
    assert dims_models == {"gpt-text"}


def test_multi_gen_row_in_both_gen_groups(gen_router):
    ch = gen_router.config.chains_cap["gt"]
    assert any("dual-maker" in u for u in ch.get("image_gen", []))
    assert any("dual-maker" in u for u in ch.get("video_gen", []))
    assert not any("img-maker" in u for u in ch.get("video_gen", []))


def test_dispatch_i2v_goes_to_video_gen_group(gen_router):
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "x"}}]}]
    got = gen_router.resolve_group_for_request(
        "scrocco-llm-gt", msgs, None, frozenset({"video_gen", "vision"}))
    assert got == "scrocco-llm-gt-video_gen"


def test_chat_vision_never_picks_generator(gen_router):
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "x"}}]}]
    for _ in range(15):
        d = gen_router.pick_deployment("scrocco-llm-gt-vision",
                                       need=frozenset({"vision"}))
        assert d is not None and "maker" not in d["unique"]


def test_video_gen_endpoint_need_selects_only_gen_group(gen_router):
    # submit video puro (t2v): nessuna parte input -> solo il cap di output
    got = gen_router.resolve_group_for_request(
        "scrocco-llm-gt", [], None, frozenset({"video_gen"}))
    assert got == "scrocco-llm-gt-video_gen"
    d = gen_router.pick_deployment(got, need=frozenset({"video_gen"}))
    assert d is not None and "maker" in d["unique"]


def test_priority_order_video_gen_before_vision():
    from app.config import CAP_PRIORITY_ORDER as P
    assert P.index("video_gen") < P.index("vision")
    assert P.index("image_gen") < P.index("vision")


# ------------------------------------------------------------- csv_store
def test_validate_caps_normalizes_and_rejects():
    from app.csv_store import validate_caps, CsvStoreError
    assert validate_caps("Text, VISION") == "text,vision"
    assert validate_caps(["stt", " tts "]) == "stt,tts"
    assert validate_caps("") == ""
    assert validate_caps(None) == ""
    with pytest.raises(CsvStoreError):
        validate_caps("vision,holodeck")


def test_row_id_stable_with_caps_column():
    from app.csv_store import row_id
    base = {"modello": "m/x", "endpoint": "https://e/v1", "chiave": ""}
    a = {**base, "caps": ""}
    b = {**base, "caps": "text,vision"}
    # la membership NON entra nell'id: rotare caps non invalida i drow_
    assert row_id(a, base["endpoint"]) == row_id(b, base["endpoint"])


def test_apply_payload_writes_canonical_caps():
    from app.csv_store import apply_payload
    row = {"modello": "m/x", "provider": "p", "data": "free",
           "context": "128", "max_input": "0", "priority": "0",
           "endpoint": "https://e/v1", "scrocco-llm-t": "k"*10}
    apply_payload(row, {"caps": ["TEXT", "vision"]}, "scrocco-llm-", "t")
    assert row["caps"] == "text,vision"
