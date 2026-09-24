"""Ordinamento DETERMINISTICO nei gruppi -go e -fallback: SOLO `data`
(giorno rinnovo -> sort_key) e `model_preference`. Le metriche (reputation,
latenza, recency, _score) NON devono influenzare la scelta: un deployment
preferito resta davanti anche se la sua reputazione e' peggiore."""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,model_preference
t@x.com,ds-a,prov,https://x/v1,20,32,8000,0,K1,text,100
t@x.com,ds-b,prov,https://x/v1,20,32,8000,0,K2,text,100
t@x.com,mimo-x,prov,https://x/v1,20,32,8000,0,K3,text,50
t@x.com,muse,prov,https://x/v1,20,32,8000,0,K4,text,-50
t@x.com,fb-deep,prov,https://x/v1,fallback,32,8000,0,K6,text,100
t@x.com,fb-other,prov,https://x/v1,fallback,32,8000,0,K7,text,50
"""
CSV_EARLY = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,model_preference
t@x.com,ds-a,prov,https://x/v1,20,32,8000,0,K1,text,100
t@x.com,early-x,prov,https://x/v1,5,32,8000,0,K5,text,50
t@x.com,mimo-x,prov,https://x/v1,20,32,8000,0,K3,text,50
"""
GO = "scrocco-llm-test-go"
FB = "scrocco-llm-test-fallback"


def _mkrouter(csv_text):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(csv_text)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({}))
    r.policy.adaptive_pick = True
    return r, path


@pytest.fixture()
def router():
    r, path = _mkrouter(CSV_ROWS)
    yield r
    os.unlink(path)


def _deps(router, grp):
    return {d["model"]: d for d in router.config.groups[grp]}


def _worsen_reputation(router, model, grp):
    """Rende la reputazione del deployment peggiore (score piu' alto =
    peggio) rispetto ai rivali, per provare che le metriche NON contano."""
    d = _deps(router, grp)[model]
    router._base_scores[d["unique"]] = 5000.0
    router._avg_latencies[d["unique"]] = 9000.0


def test_go_preference_wins_over_reputation(router):
    """deepseek (pref 100) davanti a mimo (50) anche con rep peggiore."""
    _worsen_reputation(router, "ds-a", GO)
    _worsen_reputation(router, "ds-b", GO)
    picks = {router.pick_deployment(GO, need=frozenset({"text"}))["model"]
             for _ in range(50)}
    assert picks <= {"ds-a", "ds-b"}
    assert picks                     # almeno uno dei due


def test_go_tie_stays_in_best_tier(router):
    """Entro il tier (data+pref uguali) si resta nelle chiavi del tier."""
    picks = {router.pick_deployment(GO, need=frozenset({"text"}))["model"]
             for _ in range(80)}
    assert picks <= {"ds-a", "ds-b"}
    assert "mimo-x" not in picks


def test_data_renewal_dominates_preference():
    """sort_key (rinnovo) e' il criterio PRIMARIO con `go_balance.flat_pool`
    DISATTIVATO: early-x rinnova prima di ds-a anche se ds-a ha pref maggiore.
    I sort_key sono forzati per non dipendere dalla data corrente."""
    r, path = _mkrouter(CSV_EARLY)
    try:
        r.policy.go_balance_flat_pool = False
        ds = _deps(r, GO)["ds-a"]
        early = _deps(r, GO)["early-x"]
        mimo = _deps(r, GO)["mimo-x"]
        ds["sort_key"], mimo["sort_key"], early["sort_key"] = 20.0, 20.0, 5.0
        picks = {r.pick_deployment(GO, need=frozenset({"text"}))["model"]
                 for _ in range(50)}
        assert picks <= {"early-x"}
    finally:
        os.unlink(path)


def test_fallback_preference_wins_over_reputation(router):
    _worsen_reputation(router, "fb-deep", FB)
    picks = {router.pick_deployment(FB, need=frozenset({"text"}))["model"]
             for _ in range(50)}
    assert picks <= {"fb-deep"}


def test_cooldown_still_respected(router):
    """Con tutto il tier migliore in cooldown si scende al tier successivo."""
    for m in ("ds-a", "ds-b"):
        u = _deps(router, GO)[m]["unique"]
        router.mark_failed(u, seconds=3600)
    picks = {router.pick_deployment(GO, need=frozenset({"text"}))["model"]
             for _ in range(50)}
    assert picks <= {"mimo-x"}