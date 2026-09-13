"""Ordinamento esplicito per deployment (colonna CSV `order`).

`order` piu' basso = prima; i deployment con lo stesso valore formano un
"tier" (aggregabile anche tra provider diversi); vuoto = in coda. Nei gruppi
TESTO dims la scala ordina per (tier, dim) e la PRIMA scelta si restringe al
tier minimo tra i vivi; -go/-fallback restano in coda. Senza la colonna il
comportamento e' invariato.
"""
import os
import re
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,order
,fast-a,FastProv,https://fast.example/v1,free,128,8000,0,sk-FA,text,0
,fast-b,FastProv,https://fast.example/v1,free,128,8000,0,sk-FB,text,0
,fast-c,FastProv,https://fast.example/v1,free,256,8000,0,sk-FC,text,0
,slow-a,SlowProv,https://slow.example/v1,free,128,8000,0,sk-SA,text,5
,slow-b,SlowProv,https://slow.example/v1,free,256,8000,0,sk-SB,text,5
,last-a,LastProv,https://last.example/v1,free,128,8000,0,sk-LA,text,
,paid-a,PaidProv,https://paid.example/v1,paid,128,8000,0,sk-PA,text,
"""

CSV_NO_ORDER = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
,aaa,ProvA,https://a.example/v1,free,128,8000,0,sk-A,text
,bbb,ProvB,https://b.example/v1,free,256,8000,0,sk-B,text
"""


def _make_router(rows: str) -> tuple[Router, str]:
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(rows)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    return Router(cfg, Policy.from_dict({})), path


@pytest.fixture()
def router():
    r, path = _make_router(CSV_ROWS)
    yield r
    os.unlink(path)


def _dims_providers(r: Router) -> list[str]:
    out = []
    for u in r.config.chains["test"]:
        d = r.config.deployment_by_unique(u)
        if not re.search(r"-\d+k$", d["group"]):
            continue
        out.append(d["provider"])
    return out


def test_chains_tier_then_dim(router):
    assert _dims_providers(router) == [
        "fastprov", "fastprov", "fastprov",
        "slowprov", "slowprov", "lastprov",
    ]


def test_go_fallback_in_coda(router):
    chain = router.config.chains["test"]
    last = router.config.deployment_by_unique(chain[-1])
    assert last["group"].endswith("-fallback")


def test_text_ladder_tier_order(router):
    ladder = router._text_ladder("test", start_dim=128)
    provs = [router.config.deployment_by_unique(u)["provider"]
             for u in ladder
             if re.search(r"-\d+k$", router.config.deployment_by_unique(u)["group"])]
    assert provs[:3] == ["fastprov", "fastprov", "fastprov"]
    assert provs[3:5] == ["slowprov", "slowprov"]


def test_first_pick_is_min_tier(router):
    dep = router.pick_deployment("scrocco-llm-test-128k")
    assert dep["provider"] == "fastprov"


def test_falls_back_to_next_tier_when_cooled(router):
    fast = [d["unique"] for d in router.config.groups["scrocco-llm-test-128k"]
            if d["provider"] == "fastprov"]
    assert fast
    for u in fast:
        router.mark_failed(u, seconds=3600)
    dep = router.pick_deployment("scrocco-llm-test-128k")
    assert dep["provider"] == "slowprov"


def test_backward_compatible_without_column():
    r, path = _make_router(CSV_NO_ORDER)
    try:
        provs = [r.config.deployment_by_unique(u)["provider"]
                 for u in r.config.chains["test"]]
        assert provs == ["prova", "provb"]
        dep = r.pick_deployment("scrocco-llm-test-128k")
        assert dep["provider"] == "prova"
    finally:
        os.unlink(path)
