"""(A) Priorita' zen nel warm pool + (B) tetto di dim per i nativi zen-first.

Utente: le sessioni opencode native devono usare i modelli designati (zen,
order=0). Se NON c'e' uno zen caldo la prima chiamata puo' andare su un altro
provider (latenza minima) e i canary scaldano lo zen in background.

  - A: per un nativo, un warm ZEN batte un warm non-zen (blocco zen prima).
  - B: il tetto di dim vale SOLO se tra i warm c'e' almeno uno zen: si
       tengono gli zen (qualsiasi dim) + i non-zen entro la dim richiesta,
       scartando i non-zen in dim superiore. Senza zen caldo: nessun tetto.
       `_warm_allowed` impone solo il floor; per i non-nativi nessun tetto.
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
    + _row("m-zbig", "opencode-zen", "https://opencode.ai/zen/v1", 1000,
           1000000, "K-ZBIG")
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


def test_allowed_floor_only(router):
    """`_warm_allowed` impone SOLO il floor: la dim alta e' ammessa (il tetto
    vive in `_warm_pool`, e solo se c'e' uno zen caldo)."""
    for native in (True, False):
        set_zen_first(native)
        allowed = router._warm_allowed("test", DIM200)
        assert _u(router, "K-N") in allowed
        assert _u(router, "K-Z") in allowed
        assert _u(router, "K-BIG") in allowed        # dim superiore: ammessa
        assert _u(router, "K-SMALL") not in allowed  # dim inferiore: floor


def test_pool_cap_when_zen_warm(router):
    """(B) c'e' uno zen caldo -> si scartano i non-zen in dim superiore."""
    sid = "S-CAP"
    set_zen_first(True)
    router.note_session_success(sid, _u(router, "K-Z"))      # zen 200k
    router.note_session_success(sid, _u(router, "K-BIG"))    # non-zen 1000k
    pool = router._warm_pool(sid, router._warm_allowed("test", DIM200),
                             ctx=1000, cap_dim=200)
    u = {d["unique"] for d in pool}
    assert _u(router, "K-Z") in u
    assert _u(router, "K-BIG") not in u          # scartato: non-zen oltre dim


def test_pool_no_cap_when_no_zen_warm(router):
    """(B) NESSUNO zen caldo -> nessun tetto: si usa il migliore disponibile."""
    sid = "S-NOCAP"
    set_zen_first(True)
    router.note_session_success(sid, _u(router, "K-N"))      # non-zen 200k
    router.note_session_success(sid, _u(router, "K-BIG"))    # non-zen 1000k
    pool = router._warm_pool(sid, router._warm_allowed("test", DIM200),
                             ctx=1000, cap_dim=200)
    u = {d["unique"] for d in pool}
    assert _u(router, "K-N") in u
    assert _u(router, "K-BIG") in u              # mantenuto: niente zen caldo


def test_pool_cap_keeps_zen_any_dim(router):
    """(B) lo zen caldo resta anche in dim superiore; il non-zen oltre dim no."""
    sid = "S-ZCAP"
    set_zen_first(True)
    router.note_session_success(sid, _u(router, "K-ZBIG"))   # zen 1000k
    router.note_session_success(sid, _u(router, "K-BIG"))    # non-zen 1000k
    pool = router._warm_pool(sid, router._warm_allowed("test", DIM200),
                             ctx=1000, cap_dim=200)
    u = {d["unique"] for d in pool}
    assert _u(router, "K-ZBIG") in u             # zen: tenuto a qualsiasi dim
    assert _u(router, "K-BIG") not in u          # non-zen oltre dim: scartato
    assert pool[0]["unique"] == _u(router, "K-ZBIG")   # e vince (A)


def test_pool_no_cap_non_native(router):
    """(B) non-nativo: il tetto non si applica, la dim alta resta."""
    sid = "S-PLAIN2"
    set_zen_first(False)
    router.note_session_success(sid, _u(router, "K-Z"))
    router.note_session_success(sid, _u(router, "K-BIG"))
    pool = router._warm_pool(sid, router._warm_allowed("test", DIM200),
                             ctx=1000, cap_dim=200)
    u = {d["unique"] for d in pool}
    assert _u(router, "K-BIG") in u


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
