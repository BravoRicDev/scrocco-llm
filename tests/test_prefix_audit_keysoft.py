"""F4 AUDIT PREFISSO: impronta deterministica del prefisso [1:frontier] per
dire PERCHE' la cache upstream si spegne (identity = colpa nostra, prefix =
ctxcompact/histnorm/client). F6/F7/F8 SOFT-PER-CHIAVE: skip senza cooldown,
429 blocca la chiave (tutte le twin) per il Retry-After, header presenti ->
niente cap appresi."""
import os
import tempfile
import time

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m-a,groq,https://api.groq.com/openai/v1,free,64,8000,0,K-ONE,text
t@x,m-b,groq,https://api.groq.com/openai/v1,free,64,8000,0,K-ONE,text
t@x,m-c,nvidia,https://api.nvidia.com/v1,free,64,8000,0,K-TWO,text
t@x,m-go,groq,https://api.groq.com/openai/v1,,64,8000,0,K-ONE,text
"""


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    r = Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
               Policy.from_dict({}))
    yield r
    os.unlink(path)


def _u(r, model):
    return next(d["unique"] for deps in r.config.groups.values()
                for d in deps if d.get("model") == model)


def _msgs(sys="SYS", n_turns=4):
    out = [{"role": "system", "content": sys}]
    for i in range(n_turns):
        out.append({"role": "user", "content": f"u{i}"})
        out.append({"role": "assistant", "content": f"a{i}"})
    return out


# ------------------------------------------------------------------ F4 audit
def test_audit_states(router):
    m = _msgs()
    assert router.audit_prefix("S", m, 5) == "new"
    assert router.audit_prefix("S", m, 5) == "ok"
    m2 = _msgs(sys="SYS-DIVERSO")
    assert router.audit_prefix("S", m2, 5) == "identity"
    m3 = _msgs()
    m3[2]["content"] = "a0-stubbato"
    assert router.audit_prefix("S", m3, 5) == "prefix"
    m4 = [dict(x) for x in m3]
    m4[0] = {"role": "system", "content": "SYS-DIVERSO"}
    assert router.audit_prefix("S", m4, 5) == "identity"   # solo il system
    assert router.audit_prefix("S", m3, 5) == "identity"   # e di nuovo solo il system


def test_audit_skips_and_ttl(router):
    assert router.audit_prefix(None, _msgs(), 5) == "skip"
    assert router.audit_prefix("S", _msgs(), None) == "skip"
    assert router.audit_prefix("S", _msgs(), 1) == "skip"
    m = _msgs()
    router.audit_prefix("S", m, 5)
    rec = router._prefix_fp["S"]
    router._prefix_fp["S"] = (rec[0], rec[1], time.time() - 99999)
    assert router.audit_prefix("S", m, 5) == "new"      # dopo la guard: riparte


def test_audit_key_order_irrelevant(router):
    m = _msgs()
    router.audit_prefix("S", m, 5)
    m2 = [dict(msg) for msg in m]
    m2[1] = {"content": "u0", "role": "user"}            # chiavi riordinate
    assert router.audit_prefix("S", m2, 5) == "ok"


# --------------------------------------------------------------- F6 hint skip
def test_hint_blocks_all_twins_free_not_paid(router):
    ua, ub = _u(router, "m-a"), _u(router, "m-b")     # stessa api_key
    uc = _u(router, "m-c")
    go_unique = _u(router, "m-go")
    router.note_rate_limit(ua, {"requests_remaining": 1})
    assert router._key_fault_blocked(ua) and router._key_fault_blocked(ub)
    assert not router._key_fault_blocked(uc)             # altra chiave pulita
    assert not router._key_fault_blocked(go_unique)      # bucket pagato: mai
    # e' anche demotion visibile dalle selezioni (via is_slow_for_session):
    assert router.is_slow_for_session(ua, "qualsiasi", None)
    # ma un successo non ne risente: nessun cooldown reale, nessun strike
    assert not router.is_cooled_down(ua)
    assert router.stats_for(ua).fail_count == 0


def test_hint_expires(router):
    ua = _u(router, "m-a")
    router.note_rate_limit(ua, {"requests_remaining": 0})
    tag = router._key_tag(ua)
    ts, rem = router._key_hints[tag]
    router._key_hints[tag] = (ts - 3600.0, rem)         # vecchio
    assert not router._key_fault_blocked(ua)


def test_hint_only_snapshot_when_enabled(router):
    router.policy.rate_hint_skip_enabled = False
    ua = _u(router, "m-a")
    router.note_rate_limit(ua, {"requests_remaining": 0})
    assert not router._key_fault_blocked(ua)


# ------------------------------------------------------------------ F7 429
def test_429_soft_blocks_key_for_retry_after(router):
    ua, ub = _u(router, "m-a"), _u(router, "m-b")
    uc = _u(router, "m-c")
    router.mark_failed(ua, seconds=30, reason="http_429")
    assert router._key_fault_blocked(ua) and router._key_fault_blocked(ub)
    assert not router._key_fault_blocked(uc)
    assert router._key_soft[router._key_tag(ua)] > time.time()
    # nessuna punizione reputazionale sulle twin: niente cooldown, niente strike
    assert not router.is_cooled_down(ub)
    assert router.stats_for(ub).fail_count == 0
    # scadenza: dopo il Retry-After la chiave torna pulita
    tag = router._key_tag(ua)
    router._key_soft[tag] = time.time() - 1
    assert not router._key_fault_blocked(ub)


def test_429_soft_capped(router):
    ua = _u(router, "m-a")
    router.policy.key_soft_max_sec = 60
    router.mark_failed(ua, seconds=9999, reason="http_429")
    until = router._key_soft[router._key_tag(ua)]
    d = until - time.time()
    # tetto rispettato + spread DETERMINISTICO anti-herd (F19, 0..2s)
    assert 60 <= d <= 62.5


def test_soft_disabled_flag(router):
    ua, ub = _u(router, "m-a"), _u(router, "m-b")
    router.policy.key_soft_429_enabled = False
    router.mark_failed(ua, seconds=30, reason="http_429")
    # il dep colpito ha il SUO cooldown (comportamento storico), la twin no
    assert router.is_cooled_down(ua)
    assert not router._key_fault_blocked(ub)


# ------------------------------------------------------------ F8 headers win
def test_fresh_headers_suppress_learned_caps(router):
    ua = _u(router, "m-c")
    s = router.stats_for(ua)
    s.min_cap_learned = 5.0
    s.minute_calls = 5
    bg = router.policy.budget_guard
    dep = router.config.deployment_by_unique(ua)
    assert router._virtually_saturated(dep, float(bg.get("safety_ratio", 0.8)),
                                       False) is True
    # header fresco -> i cap appresi sono spazzatura: non saturare
    router.note_rate_limit(ua, {"requests_remaining": 50})
    assert router._virtually_saturated(dep, 0.8, False) is False
    # soppressione disattivata -> torna il comportamento storico
    router.policy.budget_guard = dict(bg)
    router.policy.budget_guard["suppress_with_headers"] = False
    # NB: il budget_guard in Policy e' un dict: lo riscriviamo per il test
    assert router._virtually_saturated(dep, 0.8, False) is True


def test_purge_cleans_key_maps(router):
    ua = _u(router, "m-a")
    router.note_rate_limit(ua, {"requests_remaining": 0})
    router._key_hints[router._key_tag(ua)] = (time.time() - 999999, 0.0)
    router._key_soft["deadbeefcafe00"] = time.time() - 10
    router.purge_expired()
    assert router._key_hints.get(router._key_tag(ua)) is None
    assert "deadbeefcafe00" not in router._key_soft
