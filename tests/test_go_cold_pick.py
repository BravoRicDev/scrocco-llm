"""Scelta nel bucket -go a FREDDO: cache-holder vince sempre, altrimenti si
evita cio' che altre sessioni stanno gia' usando e si prende il MENO USATO (24h).

Regole utente:
  - il detentore cache della sessione NON va mai scartato;
  - a freddo (nessun holder) fra i modelli dello stesso tier si sceglie quello
    usato MENO da tutte le sessioni: le sessioni si spartiscono le chiavi
    (4-2, 3-3, ...) invece di martellare sempre la stessa;
  - la `data` (sort_key) resta il criterio PRIMARIO: la scelta avviene solo
    dentro il tier minimo (sort_key + model_preference).
"""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router
from app.session_ctx import set_current_session

BASE = "scrocco-llm-test"
HDR = ("commento,modello,provider,endpoint,data,context,max_input,priority,"
       f"{BASE},caps,intelligence_score,model_preference,order\n")
# due modelli nello STESSO giorno di rinnovo (stesso sort_key) e stessa pref:
# finiscono entrambi nel tier minimo; chiavi gemelle per modello.
CSV = HDR + (
    f"a@x.com,m/luna,opencode-go,https://x/v1,20,1000,250000,0,K-L1,,8,100,20\n"
    f"a@x.com,m/deep,opencode-go,https://x/v1,20,1000,250000,0,K-D1,,8,100,20\n"
    f"a@x.com,m/deep,opencode-go,https://x/v1,20,1000,250000,0,K-D2,,8,100,20\n"
)


@pytest.fixture()
def r():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    yield Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
                 Policy.from_dict({"go_preferred_models": "m/luna, m/deep"}))
    os.unlink(path)


def _go(r):
    return r.config.groups[f"{BASE}-go"]


def _by_key(r, key):
    return next(d for d in _go(r) if d.get("api_key") == key)


def test_cold_pick_sceglie_il_meno_usato(r):
    """m/luna usato molto, m/deep mai -> sceglie m/deep (meno usato)."""
    luna = _by_key(r, "K-L1")
    for _ in range(30):
        r.note_usage(luna["unique"], ctx_est=8000)
    for _ in range(10):
        d = r.pick_deployment(f"{BASE}-go", need=None, ctx=100)
        assert d["model"] == "m/deep", d["model"]


def test_cold_pick_distribuisce_col_tempo(r):
    """Senza storia la scelta e' uniforme; dopo un uso la bilancia si sposta."""
    seen = {}
    for _ in range(200):
        d = r.pick_deployment(f"{BASE}-go", need=None, ctx=100)
        seen[d["model"]] = seen.get(d["model"], 0) + 1
        r.note_usage(d["unique"], ctx_est=8000)
    # entrambe le famiglie vengono usate, nessuna esclusa
    assert set(seen) == {"m/luna", "m/deep"}, seen


def test_cache_holder_non_viene_mai_scartato(r):
    """Se la sessione ha un holder nel tier, vince anche se piu' usato."""
    set_current_session("sess-A")
    luna = _by_key(r, "K-L1")
    for _ in range(50):
        r.note_usage(luna["unique"], ctx_est=8000)
    r.note_session_success("sess-A", luna["unique"], latency_ms=100, ctx_est=100)
    for _ in range(10):
        d = r.pick_deployment(f"{BASE}-go", need=None, ctx=100)
        assert d["unique"] == luna["unique"]


def test_last_go_vince_su_meno_usato(r):
    """L'ultimo -go servito alla sessione vince anche se piu' usato degli altri."""
    set_current_session("sess-LG")
    deep = _by_key(r, "K-D2")
    # rendi deep molto usato da tutti
    for _ in range(80):
        r.note_usage(deep["unique"], ctx_est=8000)
    # ma e' l'ultimo -go con successo di QUESTA sessione
    r.note_session_success("sess-LG", deep["unique"], latency_ms=100, ctx_est=100)
    for _ in range(10):
        d = r.pick_deployment(f"{BASE}-go", need=None, ctx=100)
        assert d["unique"] == deep["unique"], "last_go deve vincere"


def test_senza_last_go_si_usa_il_meno_usato(r):
    """Sessione senza storia -go -> meno usato (non salta a caso)."""
    set_current_session("sess-new")
    luna = _by_key(r, "K-L1")
    for _ in range(50):
        r.note_usage(luna["unique"], ctx_est=8000)
    for _ in range(10):
        d = r.pick_deployment(f"{BASE}-go", need=None, ctx=100)
        assert d["unique"] != luna["unique"]


def test_cold_pick_usa_i_dep_meno_usati(r):
    """Tra chiavi gemelle dello stesso modello, si usa quella con meno uso 24h.

    (Nel -go non esiste ownership per-sessione: la distribuzione si basa sulla
    finestra di uso 24h, pesata su token di prefill.)"""
    luna_keys = [d for d in _go(r) if d["model"] == "m/luna"]
    heavy = luna_keys[0]
    light = luna_keys[1] if len(luna_keys) > 1 else luna_keys[0]
    # rendi heavy molto usato; light resta a zero
    for _ in range(50):
        r.note_usage(heavy["unique"], ctx_est=8000)
    for _ in range(20):
        d = r.pick_deployment(f"{BASE}-go", need=None, ctx=100)
        assert d["unique"] != heavy["unique"], "dep piu' usato non scelto"


def test_sort_key_primario_su_uso(r):
    """Il tier minimo (data) vince: un dep con sort_key piu' alto MAI scelto
    anche se meno usato."""
    # aggiungi un modello che si rinnova in un giorno diverso (sort_key > 0)
    import app.config as cfgmod
    from datetime import date, timedelta
    other_day = (date.today() + timedelta(days=2)).day
    # ricostruisci con quel giorno
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(HDR +
                f"a@x.com,m/luna,opencode-go,https://x/v1,20,1000,250000,0,"
                f"K-L1,,8,100,20\n"
                f"a@x.com,m/later,opencode-go,https://x/v1,{other_day},1000,"
                f"250000,0,K-LT,,8,100,20\n")
    rr = Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
                Policy.from_dict({}))
    try:
        go = rr.config.groups[f"{BASE}-go"]
        first = go[0]["model"]
        for _ in range(20):
            d = rr.pick_deployment(f"{BASE}-go", need=None, ctx=100)
            assert d["model"] == first
    finally:
        os.unlink(path)
