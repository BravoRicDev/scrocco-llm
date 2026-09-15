"""Rotazione "-dim successiva" prima di -go.

Regola utente: "se la -dim non ne ha abbastanza prosegue semplicemente la
sua ricerca nella -dim successiva prima di andare a -go".

Il difetto coperto: la scala testo e' ordinata per (order, dim); la
camminata dopo un fallimento riprende DOPO il fallito e non torna mai
indietro. Se il dep fallito e' in coda alla propria -dim (order alto), i dep
delle -dim superiori con order piu' basso restano DIETRO e non vengono mai
raggiunti -> si salta a -go (o 503) anche se la -dim successiva ha
deployment vivi.
"""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

# small: context 32, order 9999 -> ultimo gradino della scala corrente.
# mid:   context 200, order 10 -> dim superiore, MA ordinata PRIMA.
# go:    bucket a pagamento.
CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,intelligence_score,order
t@x.com,m/nx-small,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-S,,5,9999
t@x.com,m/nx-mid,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-MID,,6,10
t@x.com,m/nx-go,groq,https://api.groq.com/openai/v1,20,0,0,5,K-G,,5,0
"""


@pytest.fixture()
def mk(request):
    def _mk(**pol):
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w") as f:
            f.write(CSV)
        request.addfinalizer(lambda: os.path.exists(path) and os.unlink(path))
        return Router(GatewayConfig(path, proxy_prefix="scrocco-llm-",
                                    seed=1),
                      Policy.from_dict(pol))
    return _mk


def _dep(r, key):
    for lst in r.config.groups.values():
        for d in lst:
            if d.get("api_key") == key:
                return d
    raise AssertionError(key)


def test_scala_mette_la_dim_superiore_prima_del_fallito(mk):
    """Precondizione del test: mid (200k, order 10) sta PRIMA di small."""
    r = mk()
    small, mid = _dep(r, "K-S"), _dep(r, "K-MID")
    lad = r._ladder_for_group(small["group"])
    assert lad.index(mid["unique"]) < lad.index(small["unique"])


def test_dims_above_solo_dim_superiori(mk):
    r = mk()
    small, mid, go = (_dep(r, "K-S"), _dep(r, "K-MID"), _dep(r, "K-G"))
    dims = [mid["unique"], small["unique"]]
    assert r._dims_above(dims, small["unique"]) == [mid["unique"]]
    # il bucket -go non e' una -dim: nessun dep "sopra"
    assert r._dims_above(dims + [go["unique"]], go["unique"]) == []
    # fallito sconosciuto/None -> nessun effetto
    assert r._dims_above(dims, None) == []
    assert r._dims_above(dims, "non-esiste") == []


def test_rotazione_prosegue_nella_dim_successiva_non_a_go(mk):
    """Da un fallito in coda alla -32k si prosegue su -200k, NON su -go."""
    r = mk()
    small, mid, go = (_dep(r, "K-S"), _dep(r, "K-MID"), _dep(r, "K-G"))
    nxt = r.fallback_next("test", small, need=None, scope="group", ctx=100,
                          tried={small["unique"]},
                          requested_group=f"{BASE}-32k")
    assert nxt is not None
    assert nxt["unique"] == mid["unique"], nxt["unique"]
    assert nxt["group"] == f"{BASE}-200k"
    assert nxt["unique"] != go["unique"]


def test_se_la_dim_successiva_e_morta_si_va_a_go(mk):
    """Con la -dim successiva in cooldown il comportamento resta: -go."""
    r = mk()
    small, mid, go = (_dep(r, "K-S"), _dep(r, "K-MID"), _dep(r, "K-G"))
    r.mark_failed(mid["unique"], seconds=600, reason="http_429")
    nxt = r.fallback_next("test", small, need=None, scope="group", ctx=100,
                          tried={small["unique"]},
                          requested_group=f"{BASE}-32k")
    assert nxt is not None
    assert nxt["unique"] == go["unique"], nxt["unique"]


def test_dim_successiva_gia_tentata_non_si_ripete(mk):
    """Se anche la -dim successiva e' in `tried` si passa a -go."""
    r = mk()
    small, mid, go = (_dep(r, "K-S"), _dep(r, "K-MID"), _dep(r, "K-G"))
    nxt = r.fallback_next("test", small, need=None, scope="group", ctx=100,
                          tried={small["unique"], mid["unique"]},
                          requested_group=f"{BASE}-32k")
    assert nxt is not None
    assert nxt["unique"] == go["unique"], nxt["unique"]


def test_scope_chain_usa_la_stessa_regola(mk):
    """Anche in scope='chain' (rotazione automatica) vale la -dim successiva."""
    r = mk()
    small, mid = _dep(r, "K-S"), _dep(r, "K-MID")
    nxt = r.fallback_next("test", small, need=None, scope="chain", ctx=100,
                          tried={small["unique"]})
    assert nxt is not None
    assert nxt["unique"] == mid["unique"], nxt["unique"]
