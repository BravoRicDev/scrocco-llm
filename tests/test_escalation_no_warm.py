"""Bucket di escalation (-go/-fallback): il warm va skippato SOLO quando il
client richiede esplicitamente quel bucket; se ci si arriva via fallback dal
dim, la speculativa (refill/canary/slow-race) resta attiva per tornare al
caldo appena possibile.

Fix (decisione utente):
  - `initial_pick`: il blocco warm NON viene consultato quando il gruppo
    RICHIESTO e' -go/-fallback (`not _is_renewal_bucket(group_name)`) —
    centralizzato, copre anche video/admin che passano warm=True;
  - streaming/non-streaming: refill/canary/slow-race/hedge saltati quando il
    gruppo RICHIESTO esplicitamente e' di escalation
    (`is_escalation_group(requested_group)`), NON in base al gruppo del dep
    corrente, cosi' l'escalation via fallback continua a funzionare come prima;
  - canary/slow_race: NON gated sul gruppo del dep corrente (i canary sono
    gia' FREE-only; servono proprio a tornare al caldo dopo l'escalation).
"""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.opencode_gate import set_allow_opencode_zen, set_spoofing_request
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,order,scrocco-llm-test
t@x.com,m/p1,groq,https://api.groq.com/openai/v1,free,100,100000,5,10,K-P1
t@x.com,m/z1,opencode-zen,https://opencode.ai/zen/v1,free,100,100000,5,0,K-Z1
t@x.com,m/g1,opencode-go,https://opencode.ai/zen/go/v1,15,200,200000,5,20,K-G1
t@x.com,m/f1,groq,https://api.groq.com/openai/v1,fallback,100,100000,5,5,K-F1
"""

POLICY = {"capability_routing": {"model_capabilities": {
    "m/p1": ["text"], "m/z1": ["text"], "m/g1": ["text"], "m/f1": ["text"]}},
    "ladder_skip_after": 20, "ladder_stale_max": 10,
    "dims_ladder_floor": True, "deployment_sticky": False}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("OPENCODE_SPOOF_HEADERS", raising=False)
    monkeypatch.delenv("OPENCODE_CAUTIOUS", raising=False)
    monkeypatch.delenv("OPENCODE_GO", raising=False)
    monkeypatch.delenv("BACKGROUND_CAUTIOUS", raising=False)
    set_allow_opencode_zen(None)
    set_spoofing_request(False)
    yield
    set_allow_opencode_zen(None)
    set_spoofing_request(False)


def test_dep_attachment_only_go_when_spoofed(router, monkeypatch):
    """Per una richiesta spoofata in cautela l'unico aggancio ammesso e' lo
    stesso dep -go (cache); nessun'altra casistica. Client reali invariati."""
    monkeypatch.setenv("OPENCODE_CAUTIOUS", "1")
    go = router.config.groups[f"{BASE}-go"][0]
    dim = router.config.groups[f"{BASE}-100k"][0]
    sid = "sess-go-only"
    set_spoofing_request(True)
    router.dep_sticky_set(sid, dim["unique"])
    assert router.dep_sticky_get(sid) is None           # spoofato: dim no
    router.dep_sticky_set(sid, go["unique"])
    assert router.dep_sticky_get(sid) == go["unique"]   # -go si (cache)
    router.note_session_success(sid, go["unique"], 100, ctx_est=100)
    assert router.session_holder(sid) == go["unique"]
    router.note_session_success(sid, dim["unique"], 100, ctx_est=100)
    assert router.session_holder(sid) is None
    # client opencode reale: comportamento invariato (dim consentito)
    set_spoofing_request(False)
    router.note_session_success(sid, dim["unique"], 100, ctx_est=100)
    assert router.session_holder(sid) == dim["unique"]


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    pol = Policy.from_dict(POLICY)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, pol)
    os.unlink(path)


def _group_of(r, suffix):
    return next(g for g in r.config.groups if g.endswith(suffix))


def _count_warm_pool(r, monkeypatch):
    calls = {"n": 0}
    orig = r._warm_pool

    def counting(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(r, "_warm_pool", counting)
    return calls


NEED = frozenset({"text"})


def test_initial_pick_go_skips_warm(router, monkeypatch):
    go_group = _group_of(router, "-go")
    calls = _count_warm_pool(router, monkeypatch)
    d = router.initial_pick("test", go_group, need=NEED, session_id="ses-x")
    assert calls["n"] == 0
    assert d is not None
    assert router.config.deployment_by_unique(d["unique"])["group"] == go_group
    assert d["model"] == "m/g1"


def test_initial_pick_fallback_skips_warm(router, monkeypatch):
    fb_group = _group_of(router, "-fallback")
    calls = _count_warm_pool(router, monkeypatch)
    d = router.initial_pick("test", fb_group, need=NEED, session_id="ses-x")
    assert calls["n"] == 0
    assert d is not None
    assert router.config.deployment_by_unique(d["unique"])["group"] == fb_group
    assert d["model"] == "m/f1"


def test_initial_pick_free_still_uses_warm(router, monkeypatch):
    free_group = _group_of(router, "-100k")
    calls = _count_warm_pool(router, monkeypatch)
    d = router.initial_pick("test", free_group, need=NEED, session_id="ses-x")
    assert calls["n"] == 1
    assert d is not None


def test_slow_race_allowed_not_gated_on_escalation(router):
    go_group = _group_of(router, "-go")
    free_group = _group_of(router, "-100k")
    assert router.slow_race_allowed(
        "ses-x", "test", go_group, NEED, None, 4096, set()) is True
    assert router.slow_race_allowed(
        "ses-x", "test", free_group, NEED, None, 4096, set()) is True


def test_warm_fill_canary_explicit_go_no_candidate(router):
    go_group = _group_of(router, "-go")
    gdep = router.config.groups[go_group][0]
    assert router.warm_fill_canary(
        "test", gdep, NEED, None, 100, set(), go_group) is None


def test_warm_fill_canary_via_fallback_from_dim_active(router):
    go_group = _group_of(router, "-go")
    free_group = _group_of(router, "-100k")
    gdep = router.config.groups[go_group][0]
    cand = router.warm_fill_canary(
        "test", gdep, NEED, None, 100, set(), free_group)
    assert cand is not None
    assert router.config.deployment_by_unique(cand["unique"])["group"] == free_group