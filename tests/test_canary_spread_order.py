"""Ordine del canary refill dentro la dim (regola utente verificata):

  "il canario prova il -dim esplicito scavandolo; i PIU' USATI vanno per
   ULTIMI, prima di arrendersi e salire al -dim superiore".

Fissa il comportamento di `_canary_cold_pick`/`_spread_hide`:
  - i piu' usati (weight 24h = ctx/8000) sono NASCOSTI ai primi round;
  - quando nella dim restano solo loro, il canario LI RIPROVA (non li
    esclude per sempre: k = min(int(n*pct), n-1) non nasconde mai tutti);
  - solo a dim esaurita si sale al -dim superiore.
"""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,intelligence_score
t@x.com,m/rf2-mid,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-1,,5
t@x.com,m/rf2-mid,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-2,,5
t@x.com,m/rf2-mid,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-3,,5
t@x.com,m/rf2-mid,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-4,,5
t@x.com,m/rf2-mid,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-5,,5
t@x.com,m/rf2-big,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-BIG,,5
t@x.com,m/rf2-big,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-BIG2,,5
"""

HEAVY = ("K-1", "K-2")          # i due piu' usati della dim


@pytest.fixture()
def r():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    try:
        yield Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
                     Policy.from_dict({}))
    finally:
        os.path.exists(path) and os.unlink(path)


def _uniq_of(r, key):
    for lst in r.config.groups.values():
        for d in lst:
            if d.get("api_key") == key:
                return d["unique"]
    raise AssertionError(key)


def test_canary_scava_la_dim_usati_per_ultimi_poi_sale(r):
    heavy_u = [_uniq_of(r, k) for k in HEAVY]
    fresh_u = [_uniq_of(r, k) for k in ("K-3", "K-4", "K-5")]
    for u in heavy_u:
        for _ in range(50):
            r.note_usage(u, ctx_est=64000)      # peso 8/call -> top 20%
    cur = _dep_by_key(r, "K-BIG")               # holder -1000k: il canario
    #   pero' scava la dim RICHIESTA (-200k)
    tried = set()
    picks = []
    for _ in range(6):
        d = r.warm_fill_canary("test", cur, None, 100, 4096,
                               tried=tried, requested_group=f"{BASE}-200k")
        assert d is not None
        picks.append(d["unique"])
        tried.add(d["unique"])
    assert all(u not in heavy_u for u in picks[:3])
    assert set(picks[:3]) == set(fresh_u)       # freschi PRIMA
    assert sorted(picks[3:5]) == sorted(heavy_u)      # usati per ULTIMI
    assert picks[5] == _uniq_of(r, "K-BIG2")    # poi la dim superiore


def test_spread_non_nasconde_mai_tutti(r):
    uniqs = [_uniq_of(r, k) for k in ("K-1", "K-2", "K-3", "K-4", "K-5")]
    for u in uniqs:
        for _ in range(50):
            r.note_usage(u, ctx_est=64000)
    deps = [_dep_by_key(r, k) for k in ("K-1", "K-2", "K-3", "K-4", "K-5")]
    assert r._spread_hide(deps)                 # k=n-1: ne tiene sempre 1


def _dep_by_key(r, key):
    for lst in r.config.groups.values():
        for d in lst:
            if d.get("api_key") == key:
                return d
    raise AssertionError(key)
