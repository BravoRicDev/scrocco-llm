"""Go preferred models + ancoraggio del canary alla dim RICHIESTA.

Regole utente:
  - "-go: vorrei SEMPRE deepseek-v4.1-flash se disponibile nel gruppo -go"
    (knob `go_preferred_models`, match per sottostringa sul modello, vale
    sia nel pick deterministico del bucket sia nel walk della catena);
  - il canary/sveglia "scava il -dim esplicito": parte dalla dim RICHIESTA,
    non da quella della holder (una sessione ancorata a -1000k non deve
    ignorare le free -200k vive).
"""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,intelligence_score
t@x.com,m/gp-mimo,groq,https://api.groq.com/openai/v1,20,0,0,5,K-M1,,5
t@x.com,m/gp-mimo,groq,https://api.groq.com/openai/v1,20,0,0,5,K-M2,,5
t@x.com,m/gp-deepseek,groq,https://api.groq.com/openai/v1,20,0,0,5,K-D1,,10
t@x.com,m/gp-deepseek,groq,https://api.groq.com/openai/v1,20,0,0,5,K-D2,,10
t@x.com,m/gp-small,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-S,,5
t@x.com,m/gp-mid,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-MID,,6
t@x.com,m/gp-big,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-B1,,9
t@x.com,m/gp-big,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-B2,,9
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


# ------------------------------------------------------------------ knob
def test_knob_parsing_lista_e_stringa():
    assert Policy.from_dict({}).go_preferred_models == ""
    p = Policy.from_dict({"go_preferred_models": ["DeepSeek-V4.1-Flash"]})
    assert p.go_preferred_models == "deepseek-v4.1-flash"
    q = Policy.from_dict({"go_preferred_models": "A, B-c"})
    assert q.go_preferred_models == "a, b-c"


# ------------------------------------------------------- pick del bucket -go
def test_pick_go_preferito_sempre_vivo(mk):
    r = mk(go_preferred_models="gp-deepseek")
    g = f"{BASE}-go"
    for _ in range(8):                        # random.choice interno
        d = r.pick_deployment(g, need=None, ctx=None)
        assert "gp-deepseek" in d["model"]


def test_pick_go_senza_preferiti_vivi_ripiega(mk):
    r = mk(go_preferred_models="gp-deepseek")
    g = f"{BASE}-go"
    for d in r.config.groups[g]:
        if "gp-deepseek" in d["model"]:
            r.mark_failed(d["unique"], seconds=3600, reason="http_429")
    for _ in range(5):
        d = r.pick_deployment(g, need=None, ctx=None)
        assert "gp-mimo" in d["model"]


def test_pick_go_senza_knob_comportamento_normale(mk):
    r = mk()
    g = f"{BASE}-go"
    mods = {r.pick_deployment(g, need=None, ctx=None)["model"]
            for _ in range(20)}
    assert len(mods) > 1                      # nessun vincolo: rotazione


# ------------------------------------------------------------ walk catena
def test_walk_chain_go_usa_preferito(mk):
    """Nella catena piatta il tail -go e' in ordine CSV (mimo PRIMA di
    deepseek): senza knob il walk prende mimo, con il knob prende deepseek."""
    r_no = mk()
    r_pref = mk(go_preferred_models="gp-deepseek")
    chain = [u for u in r_no.config.chains["test"] if f"{BASE}-go__" in u]
    d_no = r_no._walk_chain(chain, None, need=None, ctx=10)
    d_pref = r_pref._walk_chain(chain, None, need=None, ctx=10)
    assert d_no is not None and "gp-mimo" in d_no["model"]
    assert d_pref is not None and "gp-deepseek" in d_pref["model"]


# ----------------------------------------------- canary ancorato alla richiesta
def test_canary_scava_dalla_dim_richiesta_non_dalla_holder(mk):
    r = mk()
    big = _dep(r, "K-B1")
    assert big["group"] == f"{BASE}-1000k"
    # senza richiesta: ladder dalla dim della holder -> gemello in -1000k
    c = r.warm_fill_canary("test", big, None, 100, 4096, tried=set(),
                           requested_group=None)
    assert c is not None and c["group"] == f"{BASE}-1000k"
    # con richiesta esplicita -200k: scava il -200k ANCHE se la holder e'
    # ancorata piu' in alto (il bug che portava dritto a -go)
    c2 = r.warm_fill_canary("test", big, None, 100, 4096, tried=set(),
                            requested_group=f"{BASE}-200k")
    assert c2 is not None and c2["group"] == f"{BASE}-200k"


def test_canary_salire_se_dim_richiesta_esaurita(mk):
    r = mk()
    big = _dep(r, "K-B1")
    mid = _dep(r, "K-MID")
    c = r.warm_fill_canary("test", big, None, 100, 4096,
                           tried={mid["unique"]},
                           requested_group=f"{BASE}-200k")
    assert c is not None and c["group"] == f"{BASE}-1000k"
