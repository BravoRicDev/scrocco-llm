"""(A) Priorita' zen nel warm pool + (B) tetto di dim per i nativi zen-first.

Utente: le sessioni opencode native devono usare i modelli designati (zen,
order=0) e NON devono pescare warm in dim SUPERIORI a quella risolta (es.
`200k -> 1000k` con ctx piccolo).

  - A: per un nativo, un warm ZEN batte un warm non-zen (blocco zen prima).
  - B: `_warm_allowed(pname, "-200k")` per un nativo esclude le dim > 200k
       (nessun prestito da `-1000k`); per i non-nativi resta il solo floor.
"""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.opencode_gate import set_allow_opencode_zen, set_spoofing_request, \
    set_zen_first
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"
HEADER = ("commento,modello,provider,endpoint,data,context,max_input,"
          "priority,order,scrocco-llm-test,caps\n")


def _row(model, provider, endpoint, dim, mxi, key, order=100):
    return (f"t@x.com,{model},{provider},{endpoint},free,"
            f"{dim},{mxi},5,{order},{key},text\n")


# dims: 64k, 200k, 1000k. Zen in 200k con order ALTO (10) cosi' il solo `order`
# NON lo metterebbe davanti: serve il blocco zen (A) per vederlo primo.
CSV = HEADER + (
    _row("m-z", "opencode-zen", "https://opencode.ai/zen/v1", 200, 200000,
         "K-Z", order=10)
    + _row("m-n", "groq", "https://api.groq.com/openai/v1", 200, 200000,
           "K-N", order=5)
    + _row("m-big", "groq", "https://api.groq.com/openai/v1", 1000, 1000000,
           "K-BIG")
    + _row("m-small", "groq", "https://api.groq.com/openai/v1", 64, 64000,
           "K-SMALL")
)
DIM200 = f"{BASE}-200k"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("OPENCODE_SPOOF_HEADERS", raising=False)
    monkeypatch.delenv("OPENCODE_CAUTIOUS", raising=False)
    monkeypatch.delenv("BACKGROUND_CAUTIOUS", raising=False)
    set_allow_opencode_zen(None)
    set_spoofing_request(False)
    set_zen_first(False)
    yield
    set_allow_opencode_zen(None)
    set_spoofing_request(False)
    set_zen_first(False)


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({}))
    yield r
    os.unlink(path)


def _u(r, key):
    for deps in r.config.groups.values():
        for d in deps:
            if d.get("api_key") == key:
                return d["unique"]
    raise AssertionError(key)


def test_allowed_cap_for_native(router):
    """(B) nativo: il warm ammesso per -200k non include la dim -1000k."""
    set_zen_first(True)
    allowed = router._warm_allowed("test", DIM200)
    assert _u(router, "K-N") in allowed
    assert _u(router, "K-Z") in allowed
    assert _u(router, "K-BIG") not in allowed      # dim superiore: esclusa
    assert _u(router, "K-SMALL") not in allowed    # dim inferiore: floor


def test_allowed_no_cap_non_native(router):
    """(B) non-nativo: resta il solo floor, la dim alta e' ammessa."""
    set_zen_first(False)
    allowed = router._warm_allowed("test", DIM200)
    assert _u(router, "K-BIG") in allowed
    assert _u(router, "K-SMALL") not in allowed


def test_warm_pool_zen_first_priority(router):
    """(A) nativo: lo zen caldo viene prima del non-zen caldo.

    NB: K-Z registrato PRIMA di K-N, cosi' l'MRU favorirebbe K-N: se lo zen
    vince comunque, e' merito del blocco zen (A)."""
    sid = "S-NAT"
    set_zen_first(True)
    router.note_session_success(sid, _u(router, "K-Z"))
    router.note_session_success(sid, _u(router, "K-N"))
    pool = router._warm_pool(sid, router._warm_allowed("test", DIM200))
    assert pool, "pool vuoto"
    assert pool[0]["unique"] == _u(router, "K-Z")


def test_warm_pool_non_native_order(router):
    """(A) non-nativo: nessun blocco zen; vince l'MRU (K-N, registrato dopo)."""
    sid = "S-PLAIN"
    set_zen_first(False)
    router.note_session_success(sid, _u(router, "K-Z"))
    router.note_session_success(sid, _u(router, "K-N"))
    pool = router._warm_pool(sid, router._warm_allowed("test", DIM200))
    assert pool and pool[0]["unique"] == _u(router, "K-N")
