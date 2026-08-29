"""multimodal_last_resort: protezione quota free-tier.

Regola: nei gruppi DIMS le richieste di TESTO PURO non devono mai cadere
su modelli con input media (vision/video/audio) finché esiste un text-only
vivo. Hard rule per tutti i tier. Gruppi cap e richieste media intoccati."""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test
t@x.com,qwen/qwen2.5-vl-72b,groq,https://api.groq.com/openai/v1,free,128,8000,5,sk-KEY-QWEN
t@x.com,openai/gpt-oss-120b,groq,https://api.groq.com/openai/v1,free,128,8000,5,sk-KEY-OSS
t@x.com,meta-llama/llama-4-scout,nvidia,https://integrate.api.nvidia.com/v1,free,128,8000,5,sk-KEY-SCOUT
"""

POLICY_MAP = {"capability_routing": {"model_capabilities": {
    "*qwen2.5-vl-*": ["text", "vision"],
    "openai/gpt-oss-120b": ["text"],
    "*llama-4-scout*": ["text"],
}}}


def _make(policy_map: dict) -> tuple[Router, GatewayConfig, str]:
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    pol = Policy.from_dict(policy_map)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    return Router(cfg, pol), cfg, path


@pytest.fixture()
def router():
    r, cfg, path = _make(POLICY_MAP)
    yield r
    os.unlink(path)


GROUP = "scrocco-llm-test-128k"

def _is_vision(u: str) -> bool:
    return "-vl-" in u


def test_text_never_uses_multimodal(router):
    for _ in range(40):
        dep = router.pick_deployment(GROUP, need=frozenset({"text"}))
        assert dep is not None and not _is_vision(dep["unique"]), \
            f"text pick caduto su multimodale: {dep['unique']}"


def test_text_uses_multimodal_only_at_exhaustion(router):
    oss = next(d for d in router.config.groups[GROUP] if "gpt-oss" in d["model"])
    scout = next(d for d in router.config.groups[GROUP] if "llama-4" in d["model"])
    qwen = next(d for d in router.config.groups[GROUP] if "-vl-" in d["model"])
    # escludendo UN text-only il defer salta all'altro text-only
    dep = router.pick_deployment(GROUP, need=frozenset({"text"}),
                                 exclude=oss["unique"])
    assert dep["unique"] == scout["unique"]
    # TUTTI i text-only in cooldown -> ammesso il multimodale
    router.mark_failed(oss["unique"], seconds=600)
    router.mark_failed(scout["unique"], seconds=600)
    dep = router.pick_deployment(GROUP, need=frozenset({"text"}))
    assert dep is not None and dep["unique"] == qwen["unique"]


def test_media_request_unaffected(router):
    qwen = next(d for d in router.config.groups[GROUP] if "-vl-" in d["model"])
    for need in (frozenset({"vision"}), frozenset({"text", "vision"})):
        dep = router.pick_deployment(GROUP, need=need)
        assert dep is not None and dep["unique"] == qwen["unique"]


def test_toggle_off_restores_old_behavior():
    import copy
    pol = copy.deepcopy(POLICY_MAP)
    pol["capability_routing"]["multimodal_last_resort"] = False
    pol["adaptive_pick"] = False      # pesi piatti: la scelta varia davvero
    r, cfg, path = _make(pol)
    try:
        seen = {r.pick_deployment(GROUP, need=frozenset({"text"}))["unique"]
                for _ in range(60)}
        assert any(_is_vision(u) for u in seen)   # qwen torna eleggibile
    finally:
        os.unlink(path)


def test_cap_group_never_defers():
    """Un gruppo CAP (-vision) è lo scopo della richiesta: mai filtrato."""
    import copy
    pol = copy.deepcopy(POLICY_MAP)
    # una riga caps=vision + una text nella stessa CSV -> gruppo cap presente
    csv = CSV_ROWS + (
        "t@x.com,qwen/qwen2.5-vl-72b,groq,https://api.groq.com/openai/v1,"
        "free,128,8000,5,sk-KEY-QWEN,vision\n")
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(csv)
    p = Policy.from_dict(pol)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, p)
    try:
        # secondo CSV: la riga caps=vision aggiunge un GRUPPO CAP duplicate?
        # semplifichiamo: _defer_media su gruppo cap -> mai filtro
        pool = [{"unique": "a", "model": "m", "caps": frozenset({"vision"}),
                 "group": "scrocco-llm-test-vision"}]
        got, deferred = r._defer_media("scrocco-llm-test-vision",
                                       frozenset({"text"}), pool)
        assert not deferred and got == pool
    finally:
        os.unlink(path)


def test_walk_chain_skips_media_ahead(router):
    """Multimodale IN TESTA alla catena: il walk salta comunque al text-only."""
    u_vision = next(d["unique"] for d in router.config.groups[GROUP]
                    if "-vl-" in d["model"])
    u_oss = next(d["unique"] for d in router.config.groups[GROUP]
                 if "gpt-oss" in d["model"])
    dep = router._walk_chain([u_vision, u_oss],
                             failed_unique=None,
                             need=frozenset({"text"}))
    assert dep is not None and dep["unique"] == u_oss


def test_walk_chain_exhaustion_uses_media(router):
    u_vision = next(d["unique"] for d in router.config.groups[GROUP]
                    if "-vl-" in d["model"])
    u_oss = next(d["unique"] for d in router.config.groups[GROUP]
                 if "gpt-oss" in d["model"])
    # il text-only è fallito (failed_unique) -> resta solo il multimodale
    dep = router._walk_chain([u_oss, u_vision],
                             failed_unique=u_oss,
                             need=frozenset({"text"}))
    assert dep is not None and dep["unique"] == u_vision


def test_media_deferred_counter_counts(router):
    for _ in range(10):
        router.pick_deployment(GROUP, need=frozenset({"text"}))
    assert router.media_deferred.get(GROUP, 0) >= 10