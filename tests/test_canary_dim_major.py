"""Canary/sveglia: ordine DIM-MAJOR.

Regola utente (m1973): "prima voglio finire la -dim richiesta poi si passa
alla successiva". Prima l'ordine dei gruppi era quello del ladder
(tier-major: `order` prima, dim dopo), quindi una -dim piu' profonda con
`order` basso veniva sondata PRIMA di esaurire quella richiesta.
Qui il -1000k ha `order` 0 e il -200k ne ha 100: il canary deve comunque
finire il -200k (tutte le sue chiavi) e solo dopo salire.
"""
import os
import tempfile
import time

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

HEADER = ("commento,modello,provider,endpoint,data,context,max_input,"
          "priority,scrocco-llm-test,caps,intelligence_score,order\n")


def _row(model, ctx, mxi, key, order):
    return (f"t@x.com,{model},groq,https://api.groq.com/openai/v1,free,"
            f"{ctx},{mxi},5,{key},,5,{order}\n")


CSV = HEADER + (
    # 3 chiavi nel -200k: la holder e' sempre esclusa -> 2 probe disponibili
    _row("dm-mid", 200, 200000, "K-M1", 100)
    + _row("dm-mid", 200, 200000, "K-M2", 100)
    + _row("dm-mid", 200, 200000, "K-M3", 100)
    + _row("dm-big", 1000, 1000000, "K-B1", 0)      # order PIU' BASSO
    + _row("dm-big", 1000, 1000000, "K-B2", 0)
)


def _mk(**pol):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    r = Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
               Policy.from_dict(pol))
    os.unlink(path)
    return r


def _dep(r, key):
    for lst in r.config.groups.values():
        for d in lst:
            if d.get("api_key") == key:
                return d
    raise AssertionError(key)


def test_canary_finisce_la_dim_richiesta_prima_di_salire():
    r = _mk()
    mid = _dep(r, "K-M1")
    big = _dep(r, "K-B1")
    assert mid["group"] == f"{BASE}-200k"
    assert big["group"] == f"{BASE}-1000k"
    assert int(big["order"]) < int(mid["order"])         # trappola tier-major

    tried: set[str] = set()
    picks = []
    for _ in range(3):
        c = r.warm_fill_canary("test", mid, None, 100, 4096, tried=set(tried),
                               requested_group=f"{BASE}-200k")
        assert c is not None
        picks.append(c["group"])
        tried.add(c["unique"])

    # le prime DUE scelte sono le due chiavi del -200k (order 100), solo la
    # terza sale al -1000k (order 0)
    assert picks == [f"{BASE}-200k", f"{BASE}-200k", f"{BASE}-1000k"]


def test_sveglia_finisce_la_dim_richiesta_prima_di_salire():
    r = _mk()
    mid = _dep(r, "K-M1")
    for k in ("K-M2", "K-M3", "K-B1"):
        u = _dep(r, k)["unique"]
        r.mark_failed(u, reason="http_429")
        assert r.cooldown_probeable(u)

    tried: set[str] = set()
    picks = []
    for _ in range(3):
        c = r.warm_wake_canary("test", mid, None, 100, 4096, tried=set(tried),
                               requested_group=f"{BASE}-200k",
                               min_age_sec=0.0)
        assert c is not None
        picks.append(c["group"])
        tried.add(c["unique"])

    # 2 dormienti nel -200k, poi si sale al -1000k dove c'e' K-B1
    assert picks == [f"{BASE}-200k", f"{BASE}-200k", f"{BASE}-1000k"]
