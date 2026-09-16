"""PRESTITO DEI WARM fra sessioni (`warm_borrow_*`).

Regola utente: quando piu' sessioni (es. i subagent di Hermes, ognuno con
prompt separato) si accumulano, ognuna "sequestra" i propri warm e i canary
delle altre non trovano piu' nulla. Un warm di un'ALTRA sessione il cui
DEPLOYMENT e' fermo da >= warm_borrow_idle_sec (idle del deployment, NON della
sessione) e senza richieste in volo diventa "prestabile":
  - conta nei 3 ready  -> il canary non spreca una chiamata;
  - entra nel pool IN CODA (priorita' di consumo: propri >> condivisi);
  - al primo successo su un prestato la proprieta' si trasferisce da sola.
"""
import os
import tempfile
import time

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router, set_current_session

BASE = "scrocco-llm-test"
HEADER = ("commento,modello,provider,endpoint,data,context,max_input,"
          "priority,scrocco-llm-test,caps,intelligence_score,order\n")


def _row(model, key, ctx=200, mxi=200000, order=100):
    return (f"t@x.com,{model},groq,https://api.groq.com/openai/v1,free,"
            f"{ctx},{mxi},5,{key},,5,{order}\n")


CSV = HEADER + (
    _row("wb-a", "K-A1") + _row("wb-a", "K-A2") + _row("wb-a", "K-A3")
    + _row("wb-b", "K-B1") + _row("wb-b", "K-B2")
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


def _own(r, sid, dep):
    r.note_session_success(sid, dep["unique"])


def _age(r, dep, secs):
    """Simula che QUEL deployment non viene usato da `secs` secondi."""
    st = r.stats_for(dep["unique"])
    st.last_used = time.time() - secs


def _leave(sess):
    set_current_session(sess)


def _reset():
    set_current_session(None)


def test_knob_default_e_parse():
    p = Policy.from_dict({})
    assert p.warm_borrow_enabled is True
    assert p.warm_borrow_idle_sec == 240.0
    assert p.warm_borrow_selectable is True
    p2 = Policy.from_dict({"warm_pool": {"borrow_enabled": False,
                                         "borrow_idle_sec": 90,
                                         "borrow_selectable": False}})
    assert p2.warm_borrow_enabled is False
    assert p2.warm_borrow_idle_sec == 90.0
    assert p2.warm_borrow_selectable is False
    # flat (per il PATCH /admin/policy senza redeploy)
    p3 = Policy.from_dict({"warm_borrow_enabled": False,
                           "warm_borrow_idle_sec": 30})
    assert p3.warm_borrow_enabled is False
    assert p3.warm_borrow_idle_sec == 30.0


def test_prestito_idle_sul_deployment_non_sulla_sessione():
    """Caso chiave: la sessione B e' ATTIVA (ha appena servito un altro dep),
    ma il dep X di B e' fermo da 5 minuti -> X e' prestabile."""
    r = _mk()
    try:
        a1 = _dep(r, "K-A1")
        b1, b2 = _dep(r, "K-B1"), _dep(r, "K-B2")
        _leave("sesB")
        _own(r, "sesB", b1)
        _own(r, "sesB", b2)
        _age(r, b1, 300)                      # fermo da 5 min
        r.note_start(b2["unique"])            # b2 usato adesso (sessione attiva)
        _leave("sesA")
        assert r._borrowable(b1["unique"]) is True
        assert r._borrowable(b2["unique"]) is False, \
            "il dep usato adesso non si presta (l'idle e' del deployment)"
    finally:
        _reset()


def test_non_prestabile_se_in_volo_o_usato_da_poco():
    r = _mk()
    try:
        b1 = _dep(r, "K-B1")
        _leave("sesB")
        _own(r, "sesB", b1)
        _age(r, b1, 600)
        _leave("sesA")
        assert r._borrowable(b1["unique"]) is True
        # in volo: una generazione lunga NON e' "fermo"
        r.stats_for(b1["unique"]).inflight = 1
        assert r._borrowable(b1["unique"]) is False
        r.stats_for(b1["unique"]).inflight = 0
        # usato 1 minuto fa: troppo presto
        _age(r, b1, 60)
        assert r._borrowable(b1["unique"]) is False
    finally:
        _reset()


def test_proprio_warm_non_e_mai_prestabile():
    r = _mk()
    try:
        a1 = _dep(r, "K-A1")
        _leave("sesA")
        _own(r, "sesA", a1)
        _age(r, a1, 9999)
        assert r._borrowable(a1["unique"]) is False
    finally:
        _reset()


def test_nessun_requisito_sul_numero_di_warm_del_proprietario():
    """Anche con UN solo warm il proprietario "presta" se quel dep e' fermo:
    sara' lui, alla prossima chiamata, a decidere se gli serve un canary."""
    r = _mk()
    try:
        b1 = _dep(r, "K-B1")
        _leave("sesB")
        _own(r, "sesB", b1)
        _age(r, b1, 300)
        _leave("sesA")
        assert len(r._warm_pool("sesB", None)) == 1
        assert r._borrowable(b1["unique"]) is True
    finally:
        _reset()


def test_ready_conta_i_prestati_e_propri_prima_dei_prestati():
    r = _mk()
    try:
        a1 = _dep(r, "K-A1")
        b1 = _dep(r, "K-B1")
        _leave("sesA")
        _own(r, "sesA", a1)
        _leave("sesB")
        _own(r, "sesB", b1)
        _age(r, b1, 300)
        _leave("sesA")
        own_only = r.warm_valid_for("sesA", "test", f"{BASE}-200k",
                                    frozenset({"text"}), 100, 4096)
        assert [d["unique"] for d in own_only] == [a1["unique"]]
        both = r.warm_valid_for("sesA", "test", f"{BASE}-200k",
                                frozenset({"text"}), 100, 4096,
                                include_borrowed=True)
        assert [d["unique"] for d in both] == [a1["unique"], b1["unique"]], \
            "i propri restano PRIMA dei prestati"
    finally:
        _reset()


def test_selectable_false_conta_solo_per_i_ready():
    r = _mk(warm_pool={"borrow_selectable": False})
    try:
        a1 = _dep(r, "K-A1")
        b1 = _dep(r, "K-B1")
        _leave("sesA")
        _own(r, "sesA", a1)
        _leave("sesB")
        _own(r, "sesB", b1)
        _age(r, b1, 300)
        _leave("sesA")
        n = len(r.warm_valid_for("sesA", "test", f"{BASE}-200k",
                                frozenset({"text"}), 100, 4096,
                                include_borrowed=True))
        assert n == 2, "il conteggio ready include i prestati"
        # i picker usano _borrow_selectable(): con selectable=False i
        # prestati NON entrano nel pool di selezione
        assert r._borrow_selectable() is False
        pool = r._warm_pool("sesA", None,
                            include_borrowed=r._borrow_selectable())
        assert [d["unique"] for d in pool] == [a1["unique"]], \
            "ma non sono selezionabili dal pool dei picker"
    finally:
        _reset()


def test_borrow_disabled_e_come_oggi():
    r = _mk(warm_pool={"borrow_enabled": False})
    try:
        b1 = _dep(r, "K-B1")
        _leave("sesB")
        _own(r, "sesB", b1)
        _age(r, b1, 9999)
        _leave("sesA")
        assert r._borrowable(b1["unique"]) is False
        assert r.warm_valid_for("sesA", "test", f"{BASE}-200k",
                                frozenset({"text"}), 100, 4096,
                                include_borrowed=True) == []
    finally:
        _reset()


def test_proprieta_trasferita_al_primo_successo_su_un_prestato():
    r = _mk()
    try:
        b1 = _dep(r, "K-B1")
        _leave("sesB")
        _own(r, "sesB", b1)
        _age(r, b1, 300)
        _leave("sesA")
        assert r._borrowable(b1["unique"]) is True
        _own(r, "sesA", b1)                    # servito dalla sessione A
        assert r._borrowable(b1["unique"]) is False
        assert r._dep_sess()[b1["unique"]][0] == "sesA"
        assert b1["unique"] in (r._sess_deps().get("sesA") or set())
        # il vecchio owner perde il warm (la sua mappa viene ripulita in modo
        # lazy: basta che il pool non lo veda piu')
        assert r._warm_pool("sesB", None) == []
        r._refresh_session("sesB")
        assert b1["unique"] not in (r._sess_deps().get("sesB") or set())
    finally:
        _reset()


def test_owner_decaduto_non_e_prestabile():
    r = _mk()
    try:
        b1 = _dep(r, "K-B1")
        _leave("sesB")
        _own(r, "sesB", b1)
        _age(r, b1, 300)
        # owner oltre la guardia: il dep semplicemente non e' piu' "di
        # qualcuno", quindi non serve il prestito (lo prendono tutti).
        r._dep_sess()[b1["unique"]] = ("sesB", time.time() - 99999)
        _leave("sesA")
        assert r._borrowable(b1["unique"]) is False
        assert r._owned_by_any_session(b1["unique"]) is False
    finally:
        _reset()
