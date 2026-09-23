"""sort_key del bucket -go: il rinnovo OGGI (sort_key=0) e' un valore VALIDO.

Bug storico: `float(meta.get("sort_key") or float("inf"))` scambiava 0 per
assente -> inf, mandando in FONDO proprio i deployment che si rinnovano oggi.
Regola utente: "oggi = urgentissimo" (si consuma per primo, mai sprecato).
"""
import os
import tempfile

import pytest

from app.config import GatewayConfig, parse_renewal
from datetime import date

BASE = "scrocco-llm-test"
HDR = ("commento,modello,provider,endpoint,data,context,max_input,priority,"
       f"{BASE},caps,intelligence_score,order\n")


def _cfg(body: str):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(HDR + body)
    return path, GatewayConfig(path, proxy_prefix="scrocco-llm-")


def _dep(cfg, model):
    for lst in cfg.groups.values():
        for d in lst:
            if d["model"] == model:
                return d
    raise AssertionError(model)


@pytest.fixture()
def today():
    return date.today()


def test_parse_renewal_oggi_zero(today):
    assert parse_renewal(str(today.day), today)["sort_key"] == 0


def test_sort_key_zero_non_diventa_inf():
    """data=giorno-odierno -> sort_key 0 (non inf)."""
    day = date.today().day
    path, cfg = _cfg(
        f"a@x.com,m/today,opencode-go,https://x/v1,{day},1000,250000,0,"
        f"K-T,,8,20\n")
    try:
        d = _dep(cfg, "m/today")
        assert d["group"] == f"{BASE}-go"
        assert d["sort_key"] == 0.0
    finally:
        os.unlink(path)


def test_sort_key_zero_vince_su_giorni_futuri():
    """Oggi (0) e' il tier minimo: batte rinnovi piu' lontani."""
    day = date.today().day
    # un secondo giorno di rinnovo != oggi (giorno 1..31, mai = oggi)
    other = 1 if day != 1 else 2
    path, cfg = _cfg(
        f"a@x.com,m/today,opencode-go,https://x/v1,{day},1000,250000,0,"
        f"K-T,,8,20\n"
        f"a@x.com,m/later,opencode-go,https://x/v1,{other},1000,250000,0,"
        f"K-L,,8,20\n")
    try:
        go = cfg.groups[f"{BASE}-go"]
        keys = {d["model"]: d["sort_key"] for d in go}
        assert keys["m/today"] == 0.0
        assert keys["m/today"] < keys["m/later"]
        # ordine di costruzione: il bucket -go e' ordinato per sort_key
        assert go[0]["model"] == "m/today"
    finally:
        os.unlink(path)
