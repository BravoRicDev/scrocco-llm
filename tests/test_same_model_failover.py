"""Failover SAME-MODEL nei gruppi gen/stt (gen_same_model_failover).

Regola: al fallimento di un deployment in image_gen/video_gen/tts/stt il
fallback riprova PRIMA le altre chiavi dello STESSO modello upstream;
attraversare verso un modello diverso è ammesso solo a esaurimento, con
log + contatore (gen_cross_model). Chat/dims/vision intatti.

Ordine CSV VOLUTO: seedance-A, veo, seedance-B -> dal fallimento di
seedance-A: flag ON -> seedance-B (salta veo); flag OFF -> veo."""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,openai/gpt-oss-120b,groq,https://api.groq.com/openai/v1,free,128,8000,5,sk-OSS,text
t@x.com,qwen/qwen3-32b,groq,https://api.groq.com/openai/v1,free,128,8000,5,sk-QWEN-T,text
t@x.com,bytedance/seedance-2.0-mini,openrouter,https://openrouter.ai/api/v1,paid,0,0,5,sk-SEED-A,video_gen
t@x.com,google/veo-3.1-lite,openrouter,https://openrouter.ai/api/v1,paid,0,0,5,sk-VEO-A,video_gen
t@x.com,bytedance/seedance-2.0-mini,openrouter,https://openrouter.ai/api/v1,paid,0,0,5,sk-SEED-B,video_gen
t@x.com,groq/whisper-large-v3-turbo,groq,https://api.groq.com/openai/v1,free,0,0,5,sk-WHISP-G1,stt
t@x.com,speaches/piper-it,it-local,http://127.0.0.1:8009/v1,free,0,0,5,sk-LOCAL,stt
t@x.com,groq/whisper-large-v3-turbo,groq,https://api.groq.com/openai/v1,free,0,0,5,sk-WHISP-G2,stt
t@x.com,qwen/qwen2.5-vl-72b,groq,https://api.groq.com/openai/v1,free,128,8000,5,sk-QWEN-V,vision
t@x.com,google/gemini-2.5-flash,google,https://generativelanguage.googleapis.com/v1beta/openai,free,128,8000,5,sk-GEM-V,vision
"""

POLICY_MAP = {"capability_routing": {"model_capabilities": {
    "*seedance*": ["video_gen"],
    "*veo*": ["video_gen"],
    "*whisper*": ["stt"],
    "speaches/piper-it": ["stt"],
    "*qwen2.5-vl*": ["vision", "text"],
    "google/gemini-2.5-flash": ["vision", "text"],
}}}


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    pol = Policy.from_dict(POLICY_MAP)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, pol)
    os.unlink(path)


def _dep(router, cap_group: str, key_hint: str) -> dict:
    return next(d for d in router.config.groups[cap_group]
                if key_hint in d["unique"] or key_hint in d["model"]
                or key_hint == d.get("api_key"))


PROF = "test"
VID = "scrocco-llm-test-video_gen-fallback"   # righe paid -> bucket fallback
STT = "scrocco-llm-test-stt"
VIS = "scrocco-llm-test-vision"
NEED_VID = frozenset({"video_gen"})
NEED_STT = frozenset({"stt"})


def test_same_model_preferred_over_chain_order(router):
    """ON: da seedance-A salta VEO (primo in catena) e va su seedance-B."""
    seed_a = _dep(router, VID, "sk-SEED-A")
    nxt = router.fallback_next(PROF, seed_a,
                               need=NEED_VID, scope="chain")
    assert nxt is not None and "seedance" in nxt["model"]
    assert router.gen_cross_model == {}          # mai attraversato


def test_cross_model_only_at_exhaustion(router):
    """ON: esaurite le chiavi seedance -> ammesso veo, con log+counter."""
    seed_a = _dep(router, VID, "sk-SEED-A")
    seed_b = _dep(router, VID, "sk-SEED-B")
    router.mark_failed(seed_b["unique"], seconds=600)
    nxt = router.fallback_next(PROF, seed_a,
                               need=NEED_VID, scope="chain")
    assert nxt is not None and "veo" in nxt["model"]      # cross-model
    assert router.gen_cross_model.get(VID) == 1


def test_explicit_scope_group_same_priority(router):
    """scope='group' (richieste esplicite): stessa priorità same-model."""
    seed_a = _dep(router, VID, "sk-SEED-A")
    nxt = router.fallback_next(PROF, seed_a,
                               need=NEED_VID, scope="group")
    assert nxt is not None and "seedance" in nxt["model"]


def test_stt_prefers_twin_keys(router):
    """STT su catena ESPLICITA: dal fallito whisper si salta speaches e si
    arriva al gemello whisper (prefer_model gestito dentro _walk_chain)."""
    w1 = _dep(router, STT, "sk-WHISP-G1")
    g2 = _dep(router, STT, "sk-WHISP-G2")
    pip = _dep(router, STT, "sk-LOCAL")
    chain = [w1["unique"], pip["unique"], g2["unique"]]
    nxt = router._walk_chain(chain, failed_unique=w1["unique"],
                             need=NEED_STT, prefer_model=w1["model"])
    assert nxt is not None and nxt["model"] == w1["model"]
    assert router.gen_cross_model == {}


def test_stt_cross_at_exhaustion(router):
    """Esaurito il gemello whisper -> ammesso speaches con counter."""
    w1 = _dep(router, STT, "sk-WHISP-G1")
    g2 = _dep(router, STT, "sk-WHISP-G2")
    pip = _dep(router, STT, "sk-LOCAL")
    router.mark_failed(g2["unique"], seconds=600)
    chain = [w1["unique"], pip["unique"], g2["unique"]]
    nxt = router._walk_chain(chain, failed_unique=w1["unique"],
                             need=NEED_STT, prefer_model=w1["model"])
    assert nxt is not None and "piper" in nxt["model"]
    assert router.gen_cross_model.get(STT) == 1


def test_vision_unaffected(router):
    """Vision NON è nel set: attraversa liberamente secondo l'ordine catena."""
    qwen = _dep(router, VIS, "sk-QWEN-V")
    nxt = router.fallback_next(PROF, qwen,
                               need=frozenset({"vision"}), scope="chain")
    assert nxt is not None and "gemini" in nxt["model"]   # primo in catena
    assert router.gen_cross_model == {}


def test_toggle_off_restores_free_rotation():
    import copy
    pol = copy.deepcopy(POLICY_MAP)
    pol["capability_routing"]["gen_same_model_failover"] = False
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict(pol))
    try:
        seed_a = _dep(r, VID, "sk-SEED-A")
        nxt = r.fallback_next(PROF, seed_a,
                              need=NEED_VID, scope="chain")
        # ordine catena nudo: dopo seed-A c'è veo (nessuna preferenza)
        assert nxt is not None and "veo" in nxt["model"]
    finally:
        os.unlink(path)


def test_chat_dims_rotation_still_free(router):
    """Il mondo testo non è toccato: dims diversi modelli ruotano come oggi."""
    oss = _dep(router, "scrocco-llm-test-128k", "gpt-oss")
    nxt = router.fallback_next(PROF, oss,
                               need=frozenset({"text"}), scope="chain")
    assert nxt is not None            # prosegue nella catena testo
    assert set(router.gen_cross_model) <= {VID, STT}


# ------------------------------------------------------- stickiness modello

def test_consecutive_picks_stick_to_model(router):
    """2 generazioni di fila: la seconda NON perde priorità per il fatto di
    aver usato il modello (recency ruota solo le chiavi gemelle)."""
    seed_a = _dep(router, VID, "sk-SEED-A")
    router.note_start(seed_a["unique"])           # prima generazione parte
    models = set()
    for _ in range(15):
        dep = router.pick_deployment(VID, need=NEED_VID)
        assert dep is not None
        models.add(dep["model"])
        router.note_start(dep["unique"])          # catena di richieste consecutive
    assert models == {"bytedance/seedance-2.0-mini"}   # mai veo a sorpresa


def test_stickiness_moves_after_cross(router):
    """Tutte le chiavi del modello esaurite -> si attacca al nuovo modello."""
    for d in router.config.groups[VID]:
        if "seedance" in d["model"]:
            router.mark_failed(d["unique"], seconds=600)
    veo = _dep(router, VID, "sk-VEO-A")
    router.note_start(veo["unique"])
    for _ in range(8):
        dep = router.pick_deployment(VID, need=NEED_VID)
        assert dep is not None and "veo" in dep["model"]


def test_text_not_sticky(router):
    """Il testo conserva il comportamento anti rate-limit: dopo l'uso il
    deployment perde priorità (recency) — nessuna stickiness nei dims."""
    oss = _dep(router, "scrocco-llm-test-128k", "gpt-oss")
    router.note_start(oss["unique"])
    picked = {router.pick_deployment("scrocco-llm-test-128k",
                                     need=frozenset({"text"}))["unique"]
              for _ in range(20)}
    assert any(u != oss["unique"] for u in picked)


def test_stickiness_toggle_off():
    import copy
    pol = copy.deepcopy(POLICY_MAP)
    pol["capability_routing"]["gen_same_model_failover"] = False
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict(pol))
    try:
        seed_a = _dep(r, VID, "sk-SEED-A")
        r.note_start(seed_a["unique"])
        models = set()
        for _ in range(30):
            dep = r.pick_deployment(VID, need=NEED_VID)
            models.add(dep["model"])
        # senza flag la recency-penalty su seed-A domina: la scelta esce
        # dal modello appena usato (veo o seed-B), come per il testo
        assert "google/veo-3.1-lite" in models
    finally:
        os.unlink(path)
