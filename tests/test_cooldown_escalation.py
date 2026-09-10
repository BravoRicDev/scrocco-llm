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
    # Nessuna alternativa viva; i cronici NON vengono riesumati da stale o
    # ULTIMA SPIAGGIA ma dal PARACADUTE 4bis (PRIMA del -fallback a pagamento).
    nxt = r.fallback_next("test", a, None, "group", ctx=1000)
    assert nxt is not None
    # primo cronico (B: meno fallimenti? entrambi 25 -> ordine per cooldown,
    # entrambi stantii; il 4bis li prova) -> NON il fallback a pagamento
    assert nxt["group"] != f"{BASE}-fallback"
    assert nxt["group"] == f"{BASE}-1000k"
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


# ------------------------------------------------- paracadute 4bis (cronici)
def _make_4bis_router(max_fail=10, chronic_max=3):
    r, p = _make_router(max_fail=max_fail)
    r.policy.ladder_chronic_max = chronic_max
    return r, p


def test_4bis_retries_chronics_before_paid_fallback():
    """Cronico (fail_24h>=soglia): provato nel paracadute 4bis PRIMA di
    scomodare il -fallback a pagamento, anche se ancora in cooldown."""
    r, p = _make_4bis_router(max_fail=10, chronic_max=3)
    a = _dep(r, f"{BASE}-1000k", "K-A")
    b = _dep(r, f"{BASE}-1000k", "K-B")
    go = _dep(r, f"{BASE}-go", "K-GO")
    fb = _dep(r, f"{BASE}-fallback", "K-FB")
    today = time.strftime("%Y-%m-%d", time.gmtime())
    # A cronico in cooldown attivo; B non-cronico in cooldown; go/fb freschi
    r.stats_for(a["unique"]).fail_count_24h = 18
    r.stats_for(a["unique"]).fail_day_key = today
    r.mark_failed(a["unique"], seconds=600)        # A in cooldown fresco
    r.stats_for(b["unique"]).fail_count_24h = 2
    r.mark_failed(b["unique"], seconds=600)
    r.mark_failed(go["unique"], seconds=600)
    r.mark_failed(fb["unique"], seconds=600)
    # parte da B (ladder dims 1000k, contiene A) — B fallito punto di partenza
    nxt = r.fallback_next("test", b, None, "group", ctx=1000)
    # A (cronico, svegliato) viene provato PRIMA di fallback a pagamento
    assert nxt is not None
    assert nxt["unique"] == a["unique"]
    os.unlink(p)


def test_4bis_orders_less_failures_first():
    """A parità di disponibilità: il cronico con MENO fallimenti/24h viene
    prima di quello con più fallimenti (ordinamento primario). Test diretto
    su _walk_ladder_resilient (failed_unique=None per non escludere A/B)."""
    r, p = _make_4bis_router(max_fail=10, chronic_max=3)
    a = _dep(r, f"{BASE}-1000k", "K-A")
    b = _dep(r, f"{BASE}-1000k", "K-B")
    go = _dep(r, f"{BASE}-go", "K-GO")
    fb = _dep(r, f"{BASE}-fallback", "K-FB")
    today = time.strftime("%Y-%m-%d", time.gmtime())
    r.stats_for(a["unique"]).fail_count_24h = 12     # meno fallimenti
    r.stats_for(a["unique"]).fail_day_key = today
    r.stats_for(b["unique"]).fail_count_24h = 25     # più fallimenti
    r.stats_for(b["unique"]).fail_day_key = today
    # A e B in cooldown; go e fallback pure (per non farli scegliere prima)
    r.mark_failed(a["unique"], seconds=600)
    r.mark_failed(b["unique"], seconds=600)
    r.mark_failed(go["unique"], seconds=600)
    r.mark_failed(fb["unique"], seconds=600)
    ladder = r._text_ladder("test", start_dim=1000)  # A,B,go,fallback
    dep = r._walk_ladder_resilient(ladder, None, None, 1000)
    assert dep is not None
    assert dep["unique"] == a["unique"]              # 12 < 25 -> A prima
    os.unlink(p)


def test_4bis_respects_cap_context_need():
    """Il paracadute cronico NON propone un deployment senza la capacità o
    senza contesto per la richiesta (need + _cap_fits)."""
    r, p = _make_4bis_router(max_fail=10, chronic_max=3)
    a = _dep(r, f"{BASE}-1000k", "K-A")
    go = _dep(r, f"{BASE}-go", "K-GO")
    fb = _dep(r, f"{BASE}-fallback", "K-FB")
    today = time.strftime("%Y-%m-%d", time.gmtime())
    r.stats_for(a["unique"]).fail_count_24h = 18
    r.stats_for(a["unique"]).fail_day_key = today
    r.mark_failed(go["unique"], seconds=600)
    r.mark_failed(fb["unique"], seconds=600)
    # need VISION: A è solo text (mappa vuota) -> scartato, nessun cronico ok
    nxt = r.fallback_next("test", go, frozenset({"vision"}), "group", ctx=1000)
    # non deve MAI scegliere A (non supporta vision)
    assert nxt is None or nxt["unique"] != a["unique"]
    os.unlink(p)


def test_4bis_chronic_max_limits_attempts():
    """Con ladder_chronic_max=1, il secondo cronico NON è provato: si
    esaurisce il paracadute gratis e si va al fallback (a parità cooldown
    vince il meno fallimentare, quindi il primo è A)."""
    r, p = _make_4bis_router(max_fail=10, chronic_max=1)
    a = _dep(r, f"{BASE}-1000k", "K-A")
    b = _dep(r, f"{BASE}-1000k", "K-B")
    go = _dep(r, f"{BASE}-go", "K-GO")
    fb = _dep(r, f"{BASE}-fallback", "K-FB")
    today = time.strftime("%Y-%m-%d", time.gmtime())
    r.stats_for(a["unique"]).fail_count_24h = 12
    r.stats_for(a["unique"]).fail_day_key = today
    r.stats_for(b["unique"]).fail_count_24h = 14
    r.stats_for(b["unique"]).fail_day_key = today
    # A e B in cooldown; go e fallback pure (per non farli scegliere prima)
    r.mark_failed(a["unique"], seconds=600)
    r.mark_failed(b["unique"], seconds=600)
    r.mark_failed(go["unique"], seconds=600)
    r.mark_failed(fb["unique"], seconds=600)
    ladder = r._text_ladder("test", start_dim=1000)
    # primo giro: A provato (12 < 14, max=1)
    dep1 = r._walk_ladder_resilient(ladder, None, None, 1000)
    assert dep1 is not None and dep1["unique"] == a["unique"]
    # A in tried -> il pool cronico si riduce; con max=1 il primo candidato
    # diventa B (entro il max), quindi B viene provato.
    dep2 = r._walk_ladder_resilient(ladder, None, None, 1000,
                                    tried={a["unique"]})
    assert dep2 is not None and dep2["unique"] == b["unique"]
    os.unlink(p)


# ------------------------------------------------ floor 2h per cronici
def test_chronic_mark_failed_floor_2h():
    r, p = _make_router(max_fail=10)
    d = _dep(r, f"{BASE}-1000k", "K-A")
    today = time.strftime("%Y-%m-%d", time.gmtime())
    r.stats_for(d["unique"]).fail_count_24h = 25     # cronico
    r.stats_for(d["unique"]).fail_day_key = today
    # un fallimento "soft" da 90s -> il floor cronico lo porta a >=2h
    r.mark_failed(d["unique"], seconds=90)
    cd = r._cooldown[d["unique"]] - time.time()
    assert cd >= 7200 - 1
    os.unlink(p)


def test_chronic_mark_failed_double_residual_floor_2h():
    r, p = _make_router(max_fail=10)
    d = _dep(r, f"{BASE}-1000k", "K-A")
    today = time.strftime("%Y-%m-%d", time.gmtime())
    r.stats_for(d["unique"]).fail_count_24h = 25
    r.stats_for(d["unique"]).fail_day_key = today
    r._cooldown[d["unique"]] = time.time() + 120
    return_cd = r.mark_failed_double_residual(d["unique"])
    assert return_cd >= 7200
    assert r._cooldown[d["unique"]] - time.time() >= 7200 - 1
    os.unlink(p)


def test_non_chronic_not_affected_by_floor():
    r, p = _make_router(max_fail=10)
    d = _dep(r, f"{BASE}-1000k", "K-A")
    today = time.strftime("%Y-%m-%d", time.gmtime())
    r.stats_for(d["unique"]).fail_count_24h = 3      # non cronico
    r.stats_for(d["unique"]).fail_day_key = today
    r.mark_failed(d["unique"], seconds=90)           # resta ~90s (escalation soft)
    cd = r._cooldown[d["unique"]] - time.time()
    assert cd < 7200
    os.unlink(p)


# -------------------------------- media defer nel paracadute cronico (Opzione A)
def _make_media_router(chronic_max=3):
    """Stesso CSV base, ma rende K-B 'vision' via model_capabilities della
    policy (resta nel gruppo 1000k: è m/b1000, solo dichiarato vision)."""
    r, p = _make_router(max_fail=10)
    r.policy.ladder_chronic_max = chronic_max
    b = _dep(r, f"{BASE}-1000k", "K-B")
    r.policy.model_capabilities = {
        b["model"]: ["text", "vision"],
    }
    return r, p, b


def test_4bis_text_prefers_cronic_text_only_over_media():
    """Opzione A: richiesta testo puro -> il paracadute cronico sceglie il
    cronico text-only (K-A) PRIMA di quello vision (K-B), a parità cooldown."""
    r, path, b = _make_media_router(chronic_max=3)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    a = _dep(r, f"{BASE}-1000k", "K-A")          # text-only
    go = _dep(r, f"{BASE}-go", "K-GO")
    fb = _dep(r, f"{BASE}-fallback", "K-FB")
    for u in (a["unique"], b["unique"]):
        r.stats_for(u).fail_count_24h = 15
        r.stats_for(u).fail_day_key = today
        r.mark_failed(u, seconds=600)            # in cooldown attivo
    r.mark_failed(go["unique"], seconds=600)     # go e fallback freschi
    r.mark_failed(fb["unique"], seconds=600)
    ladder = r._text_ladder("test", start_dim=1000)
    dep = r._walk_ladder_resilient(ladder, None, frozenset({"text"}), 1000)
    assert dep is not None
    assert dep["unique"] == a["unique"]           # text-only prima di vision
    os.unlink(path)


def test_4bis_vision_uses_media_capable():
    """Richiesta VISION: il paracadute cronico deve poter usare il cronico
    vision (media defer non si applica alle richieste media)."""
    r, path, b = _make_media_router(chronic_max=3)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    a = _dep(r, f"{BASE}-1000k", "K-A")
    go = _dep(r, f"{BASE}-go", "K-GO")
    fb = _dep(r, f"{BASE}-fallback", "K-FB")
    for u in (a["unique"], b["unique"]):
        r.stats_for(u).fail_count_24h = 15
        r.stats_for(u).fail_day_key = today
        r.mark_failed(u, seconds=600)
    r.mark_failed(go["unique"], seconds=600)
    r.mark_failed(fb["unique"], seconds=600)
    ladder = r._text_ladder("test", start_dim=1000)
    # richiesta vision: text-only K-A non la supporta -> resta solo K-B vision
    dep = r._walk_ladder_resilient(ladder, None, frozenset({"text", "vision"}),
                                   1000)
    assert dep is not None
    assert dep["unique"] == b["unique"]
    os.unlink(path)


# ------------------------------------------------ TIMEOUT = danno reale (10x)
def test_timeout_cooldown_multiplied_vs_classic():
    r, p = _make_router(max_fail=10)
    # stesso deployment: fallimento classico (errore con codice) vs timeout
    d = _dep(r, f"{BASE}-1000k", "K-A")
    classic = r.mark_failed(d["unique"], reason="http_500")
    # reset e rifai come timeout
    r._cooldown.pop(d["unique"], None)
    r.stats_for(d["unique"]).fail_count_24h = 0
    r.stats_for(d["unique"]).fail_day_key = time.strftime("%Y-%m-%d",
                                                          time.gmtime())
    to = r.mark_failed(d["unique"], reason="timeout")
    assert classic == 1800                              # linear primo fail
    assert to == min(classic * 10, r.policy.max_cooldown_sec)
    assert to >= 17990
    os.unlink(p)


def test_timeout_cooldown_mult_knob():
    r, p = _make_router(max_fail=10)
    r.policy.timeout_cooldown_mult = 1                  # nessuna penalità extra
    d = _dep(r, f"{BASE}-1000k", "K-B")
    classic = 1800
    to = r.mark_failed(d["unique"], reason="timeout")
    assert to == min(classic * 1, r.policy.max_cooldown_sec)
    os.unlink(p)


def test_timeout_double_residual_multiplied():
    r, p = _make_router(max_fail=10)
    d = _dep(r, f"{BASE}-1000k", "K-A")
    r._cooldown[d["unique"]] = time.time() + 100     # residuo ~100s
    cd = r.mark_failed_double_residual(d["unique"], reason="timeout")
    # residuo x2 = ~200 -> x10 = ~2000 (sotto il cap 18000)
    assert 1800 <= cd <= 2200
    os.unlink(p)


def test_non_timeout_reason_unaffected():
    r, p = _make_router(max_fail=10)
    d = _dep(r, f"{BASE}-1000k", "K-A")
    classic = r.mark_failed(d["unique"], reason="http_429")
    assert classic == 1800                              # nessun x10
    os.unlink(p)