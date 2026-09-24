"""CACHE PAGATA su richiesta esplicita: nel gruppo TESTO -go/-fallback, se la
richiesta e' esplicita (prefer_holder=True) il detentore cache della sessione
vince sul tier di rinnovo (stessa chiave = KV-cache calda; al 429 il holder si
esclude da solo e la rotazione prosegue nell'ordine normale, cosi' i crediti si
sommano un account alla volta). Auto-routing ed escalation interne NON usano
prefer_holder: l'ordine resta data+pref con random nel tier migliore.

NB: questi test usano `go_balance.flat_pool=False` per isolare la semantica
"tier stretto + prefer_holder" (con il default flat_pool il pool dei rinnovi
futuri e' unico e la scelta a freddo e' bilanciata per token di output)."""
import os
import tempfile
from datetime import date

import pytest

from app.config import GatewayConfig, parse_renewal
from app.policy import Policy
from app.router import Router, set_current_session

BASE = "scrocco-llm-test"
G64 = f"{BASE}-64k"
GGO = f"{BASE}-go"
GFB = f"{BASE}-fallback"


def _renewal_days():
    """Due giorni del mese con giorni-mancanti diversi e non-nulli:
    (miglior tier, peggiore). Escludiamo 0 perche' il config loader mappa
    sort_key 0 -> inf (`meta.get(...) or inf`)."""
    today = date.today()
    sk = {d: int(parse_renewal(str(d), today)["sort_key"])
          for d in range(1, 32)
          if 0 < parse_renewal(str(d), today)["sort_key"] < float("inf")}
    best = min(sk, key=lambda d: (sk[d], d))
    worse = max(sk, key=lambda d: (sk[d], -d))
    assert sk[worse] > sk[best]
    return str(best), str(worse)


BEST_DAY, WORSE_DAY = _renewal_days()

CSV = f"""commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m-f,groq,https://api.groq.com/openai/v1,free,64,8000,0,K-FREE,text
t@x,m-gb,groq,https://api.groq.com/openai/v1,{BEST_DAY},64,8000,0,K-GB,text
t@x,m-gw,groq,https://api.groq.com/openai/v1,{WORSE_DAY},64,8000,0,K-GW,text
t@x,m-fb,groq,https://api.groq.com/openai/v1,fallback,64,8000,0,K-FB,text
t@x,m-fb2,groq,https://api.groq.com/openai/v1,fallback,64,8000,0,K-FB2,text
"""


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({"go_balance": {"flat_pool": False}}))
    yield r
    os.unlink(path)


def _dep(r, group, key):
    return next(d for d in r.config.groups[group] if d.get("api_key") == key)


def _u(r, group, key):
    return _dep(r, group, key)["unique"]


# ---------------------------------------------------------------- -go
def test_go_tier_order_unchanged_without_flag(router):
    """Holder su tier peggiore, niente flag: vince comunque il tier migliore."""
    gw = _u(router, GGO, "K-GW")
    gb = _u(router, GGO, "K-GB")
    set_current_session("S")
    router.note_session_success("S", gw)
    assert router.session_holder("S") == gw
    assert router.pick_deployment(GGO)["unique"] == gb


def test_go_prefer_holder_beats_worse_tier(router):
    """Con prefer_holder la sessione resta sull'account gia' caldo."""
    gw = _u(router, GGO, "K-GW")
    set_current_session("S")
    router.note_session_success("S", gw)
    assert router.pick_deployment(GGO, prefer_holder=True)["unique"] == gw


def test_go_holder_other_group_not_used(router):
    """Holder su free-dim: non appartiene al bucket -> ordine normale."""
    gb = _u(router, GGO, "K-GB")
    free = _u(router, G64, "K-FREE")
    set_current_session("S")
    router.note_session_success("S", free)
    assert router.pick_deployment(GGO, prefer_holder=True)["unique"] == gb


def test_go_holder_cooled_falls_back_to_tier(router):
    """Holder in cooldown (es. 429 crediti): si passa alla chiave successiva."""
    gw = _u(router, GGO, "K-GW")
    gb = _u(router, GGO, "K-GB")
    set_current_session("S")
    router.note_session_success("S", gw)
    router.mark_failed(gw, seconds=600)
    assert router.pick_deployment(GGO, prefer_holder=True)["unique"] == gb


def test_go_no_session_holder_normal_path(router):
    """Sessione nuova (nessun holder sul bucket): comportamento invariato."""
    gb = _u(router, GGO, "K-GB")
    set_current_session("S-new")
    assert router.pick_deployment(GGO, prefer_holder=True)["unique"] == gb


# ------------------------------------------------------------- -fallback
def test_fallback_prefer_holder_same_bucket(router):
    fb2 = _u(router, GFB, "K-FB2")
    set_current_session("S")
    router.note_session_success("S", fb2)
    assert router.pick_deployment(GFB, prefer_holder=True)["unique"] == fb2


# --------------------------------------------------------------- initial_pick
def test_initial_pick_propagates_flag(router):
    """Il wrapper del main: warm=False + prefer_holder=True (esplicito -go)."""
    gw = _u(router, GGO, "K-GW")
    gb = _u(router, GGO, "K-GB")
    set_current_session("S")
    router.note_session_success("S", gw)
    got = router.initial_pick("test", GGO, None, None,
                              session_id="S", warm=False, prefer_holder=True)
    assert got["unique"] == gw
    got2 = router.initial_pick("test", GGO, None, None,
                               session_id="S", warm=False)
    assert got2["unique"] == gb


# ------------------------------------------------------------ sessione-multipla
def test_holder_follows_last_success_across_accounts(router):
    """Effetto 'somma gli account': A in cache; A 429 -> B; dopo il successo
    di B la sessione si riattacca a B, non torna ad A."""
    ga = _u(router, GGO, "K-GB")   # tier migliore
    gb = _u(router, GGO, "K-GW")
    set_current_session("S")
    router.note_session_success("S", ga)
    assert router.pick_deployment(GGO, prefer_holder=True)["unique"] == ga
    router.mark_failed(ga, seconds=600)                 # crediti finiti
    nxt = router.pick_deployment(GGO, prefer_holder=True)
    assert nxt["unique"] == gb
    router.note_session_success("S", gb)                # B risponde
    assert router.pick_deployment(GGO, prefer_holder=True)["unique"] == gb
