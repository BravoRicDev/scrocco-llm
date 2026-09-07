"""Escalation cooldown dolce (Leva A) + esclusione cronici (Leva B).

Leva A: un fallimento SOFT/transitorio ripetuto (finestra 24h) NON resta a
cooldown fisso breve: dopo il primo il cooldown sale col 10% di quello
"potente" lineare, cosi' una chiave che fallisce 18 volte/24h viene esclusa
per minuti/ore invece di essere riesumata ogni 2 minuti.

Leva B: un deployment CRONICO (fail_count_24h >= cooldown_retry_max_fail_24h)
non viene riesumato dagli step stale/ULTIMA SPIAGGIA della scala; resta solo
come ultimo paracadute assoluto. clear_cooldown su successo azzera i contatori
-> un deployment "svegliato" dalla catena che risponde torna pienamente vivo.
"""
import os
import tempfile
import time

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m/a1000,groq,https://api.groq.com/openai/v1,free,1000,8000,5,K-A,
t@x,m/b1000,groq,https://api.groq.com/openai/v1,free,1000,8000,5,K-B,
t@x,m/g1,groq-go,https://api.groq.com/openai/v1,,0,0,5,K-GO,
t@x,m/f1,groq,https://api.groq.com/openai/v1,paid,0,0,5,K-FB,
"""
BASE = "scrocco-llm-test"


def _make_router(max_fail=10):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    pol.stale_cooldown_retry_sec = 300
    pol.cooldown_retry_max_fail_24h = max_fail
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    return Router(cfg, pol), path


def _dep(r, group, key):
    return next(d for d in r.config.groups[group] if d.get("api_key") == key)


def _uniques(r, group):
    return [d["unique"] for d in r.config.groups[group]]


# ------------------------------------------------------------ Leva A (escalation)
def test_escalate_cooldown_first_failure_is_base():
    r, p = _make_router()
    assert r.escalate_cooldown(90, 0) == 90      # nessun fail -> base
    assert r.escalate_cooldown(90, 1) == 90      # primo -> base
    assert r.escalate_cooldown(120, 1) == 120
    os.unlink(p)


def test_escalate_cooldown_grows_after_repeated_failures():
    r, p = _make_router()
    # base=90 (soft streaming), cooldown_base_min=30, mult=30
    # potent(2) = (30+30*1)*60 = 3600 -> 10% = 360 -> 90+360 = 450
    assert r.escalate_cooldown(90, 2) == 450
    # potent(5) = (30+30*4)*60 = 9000 -> 10% = 900 -> 90+900 = 990
    assert r.escalate_cooldown(90, 5) == 990
    # base=120 (transient 429)
    assert r.escalate_cooldown(120, 2) == 480
    os.unlink(p)


def test_escalate_cooldown_capped_at_max():
    r, p = _make_router()
    # potent(1000) capato a max_cooldown_sec (18000) -> 10% = 1800 -> 90+1800
    v = r.escalate_cooldown(90, 1000)
    assert v <= r.policy.max_cooldown_sec
    assert v > 1800
    os.unlink(p)


def test_soft_cd_uses_escalation():
    # _soft_cd(fail_24h) deve riflettere l'escalation (via router)
    import app.main as m
    r = m.router
    assert m._soft_cd(1) == m.router.policy.qc_json.watchdog_cooldown_sec
    assert m._soft_cd(5) > m._soft_cd(1)
    assert m._soft_cd(18) > m._soft_cd(5)


# ------------------------------------------------------------ Leva B (cronici)
def test_chronic_dep_excluded_from_stale_and_last_resort(router=None):
    r, p = _make_router(max_fail=10)
    # A e B sono CRONICI (fail_24h alti), in cooldown PIU' che stantio
    for key in ("K-A", "K-B"):
        d = _dep(r, f"{BASE}-1000k", key)
        r.mark_failed(d["unique"], seconds=600)
        r._cooldown_since[d["unique"]] = time.time() - 999
        r.stats_for(d["unique"]).fail_count_24h = 25
    # go e fallback: in cooldown FRESCO (non stantii), non cronici
    go = _dep(r, f"{BASE}-go", "K-GO")
    fb = _dep(r, f"{BASE}-fallback", "K-FB")
    r.mark_failed(go["unique"], seconds=600)
    r.mark_failed(fb["unique"], seconds=600)
    a = _dep(r, f"{BASE}-1000k", "K-A")
    # Nessuna alternativa viva; i cronici saltati da stale e ultima spiaggia
    # -> l'unico candidato resta il -fallback (step 5, paracadute).
    nxt = r.fallback_next("test", a, None, "group", ctx=1000)
    assert nxt is not None
    assert nxt["unique"] == fb["unique"]
    os.unlink(p)


def test_non_chronic_stale_still_retried_first():
    r, p = _make_router(max_fail=10)
    # B stantio MA non cronico -> ri-provato PRIMA del paid (-fallback)
    b = _dep(r, f"{BASE}-1000k", "K-B")
    go = _dep(r, f"{BASE}-go", "K-GO")
    fb = _dep(r, f"{BASE}-fallback", "K-FB")
    r.mark_failed(b["unique"], seconds=600)
    r._cooldown_since[b["unique"]] = time.time() - 600
    r.mark_failed(go["unique"], seconds=600)
    r.mark_failed(fb["unique"], seconds=600)
    a = _dep(r, f"{BASE}-1000k", "K-A")
    nxt = r.fallback_next("test", a, None, "group", ctx=1000)
    assert nxt["api_key"] == "K-B"
    os.unlink(p)


def test_clear_cooldown_resets_chronic_counter():
    r, p = _make_router(max_fail=10)
    d = _dep(r, f"{BASE}-1000k", "K-A")
    r.stats_for(d["unique"]).fail_count_24h = 25
    r._cooldown[d["unique"]] = time.time() + 600
    r.clear_cooldown(d["unique"])
    # dopo successo da dormiente la chiave torna VIVA: niente cooldown,
    # contatori azzerati (non piu' cronica)
    assert r.stats_for(d["unique"]).fail_count_24h == 0
    assert not r.is_cooled_down(d["unique"])
    os.unlink(p)