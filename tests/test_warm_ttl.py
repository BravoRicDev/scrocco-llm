"""Durata dei WARM (session_dep_guard) 15 -> 60 minuti.

Regola utente: allungare la finestra del PRESTITO fra sessioni. Scenario
"cronjob": un giro parte senza warm, produce 3+ caldi, finisce; mezz'ora dopo
parte un altro giro che deve trovare "tutto pronto" (warm prestabili) invece
di riscoprirli con i canary.

La finestra e' `session_dep_guard_sec` (default 3600 = 60 min): e' l'UNICA
leva efficace, perche' `warm_pool.ttl_sec` (0 = segue la guardia) e
`_refresh_session`/`_borrowable`/`_lendable_set` usano tutti `_guard_sec()`.
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


CSV = HEADER + (_row("wt-a", "K-W1") + _row("wt-a", "K-W2"))


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


def _backdate(r, dep, secs):
    """Sposta indietro il ts di PROPRIETA' (come se la sessione fosse stata
    zitta per `secs`)."""
    sid = r._dep_sess()[dep["unique"]][0]
    r._dep_sess()[dep["unique"]] = (sid, time.time() - secs)
    r._lendable_cache = None


def test_default_e_parse():
    p = Policy.from_dict({})
    assert p.session_dep_guard_enabled is True
    assert p.session_dep_guard_sec == 3600, "default 60 minuti"
    assert p.warm_pool_ttl_sec == 0, "il warm segue la guardia"
    p2 = Policy.from_dict({"session_dep_guard": {"enabled": True, "sec": 7200}})
    assert p2.session_dep_guard_sec == 7200
    p3 = Policy.from_dict({"session_dep_guard": {"enabled": False}})
    assert p3.session_dep_guard_enabled is False


def test_warm_sopravvive_20_min_con_guard_3600_ma_non_con_900():
    """20 minuti di silenzio: dentro i 60 (eredita il parco) / fuori dai 15."""
    for guard, atteso in ((900, []), (3600, ["K-W1"])):
        r = _mk(session_dep_guard={"sec": guard})
        try:
            d = _dep(r, "K-W1")
            set_current_session("sesA")
            r.note_session_success("sesA", d["unique"])
            _backdate(r, d, 1200)                      # 20 minuti fa
            pool = r._warm_pool("sesA", None)
            assert [x["api_key"] for x in pool] == atteso, guard
        finally:
            set_current_session(None)


def test_refresh_session_scarta_dopo_20_min_solo_con_guard_900():
    for guard, resta in ((900, False), (3600, True)):
        r = _mk(session_dep_guard={"sec": guard})
        try:
            d = _dep(r, "K-W1")
            set_current_session("sesA")
            r.note_session_success("sesA", d["unique"])
            _backdate(r, d, 1200)
            r._refresh_session("sesA")                 # attivita' della sessione
            in_set = d["unique"] in (r._sess_deps().get("sesA") or set())
            assert in_set is resta, guard
            if resta:                                  # rinnovata: di nuovo fresh
                assert r._dep_sess()[d["unique"]][1] > time.time() - 5
        finally:
            set_current_session(None)


def test_prestito_possibile_dopo_30_min_con_guard_3600():
    """Lo scenario del cronjob: il giro precedente e' finito 30 min fa."""
    for guard, prestabile in ((900, False), (3600, True)):
        r = _mk(session_dep_guard={"sec": guard})
        try:
            d = _dep(r, "K-W1")
            set_current_session("cron1")
            r.note_session_success("cron1", d["unique"])
            r.stats_for(d["unique"]).last_used = time.time() - 1800
            _backdate(r, d, 1800)                      # 30 minuti fa
            set_current_session("cron2")               # giro nuovo
            assert r._borrowable(d["unique"]) is prestabile, guard
            assert (d["unique"] in r._lendable_set()) is prestabile, guard
            pool = r._warm_pool("cron2", None, include_borrowed=True)
            assert bool(pool) is prestabile, guard
        finally:
            set_current_session(None)
