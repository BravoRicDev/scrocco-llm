"""AUTO-ADAPTIVE estimator: i contatori shadow sopravvivono al restart e la
stima adattiva si accende da sola quando l'evidenza e' sufficiente."""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import (Router, _maybe_auto_adaptive, _estimate_shadow_stats,
                        configure_estimate, estimate_shadow_stats,
                        estimate_tokens, load_estimate_shadow)

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m-a,groq,https://api.groq.com/openai/v1,free,64,8000,0,K-A,text
"""

MSGS = [{"role": "user", "content": "x" * 400}]


@pytest.fixture(autouse=True)
def _clean():
    def _reset():
        configure_estimate(adaptive=False, shadow=True, auto_enable=False,
                           auto_min_n=200, auto_max_delta_pct=5.0)
        load_estimate_shadow({"n": 0, "legacy": 0, "adaptive": 0})
    _reset()
    yield
    _reset()


def test_shadow_accumula_i_contatori():
    estimate_tokens(MSGS)
    estimate_tokens(MSGS)
    st = estimate_shadow_stats()
    assert st["n"] == 2
    assert st["legacy"] > 0 and st["adaptive"] > 0
    assert "delta_pct" in st


def test_auto_si_accende_con_delta_basso():
    configure_estimate(adaptive=False, shadow=True, auto_enable=True,
                       auto_min_n=200, auto_max_delta_pct=5.0)
    load_estimate_shadow({"n": 300, "legacy": 1000, "adaptive": 1010})
    _maybe_auto_adaptive()
    assert estimate_auto_state_on() is True
    # da qui la stima restituita e' quella adattiva
    assert estimate_tokens(MSGS) > 0


def estimate_auto_state_on() -> bool:
    return bool(estimate_shadow_stats().get("auto", {}).get("on"))


def test_auto_resta_spento_con_delta_alto():
    configure_estimate(adaptive=False, shadow=True, auto_enable=True,
                       auto_min_n=200, auto_max_delta_pct=5.0)
    load_estimate_shadow({"n": 300, "legacy": 1000, "adaptive": 1300})
    _maybe_auto_adaptive()
    assert estimate_auto_state_on() is False


def test_auto_richiede_campioni_minimi():
    configure_estimate(adaptive=False, shadow=True, auto_enable=True,
                       auto_min_n=500, auto_max_delta_pct=5.0)
    load_estimate_shadow({"n": 100, "legacy": 1000, "adaptive": 1000})
    _maybe_auto_adaptive()
    assert estimate_auto_state_on() is False


def test_auto_disabilitato_da_policy():
    configure_estimate(adaptive=False, shadow=True, auto_enable=False,
                       auto_min_n=10, auto_max_delta_pct=50.0)
    load_estimate_shadow({"n": 1000, "legacy": 1000, "adaptive": 1000})
    _maybe_auto_adaptive()
    assert estimate_auto_state_on() is False


def test_master_switch_esplicito_spegne_l_auto():
    configure_estimate(adaptive=True, shadow=False, auto_enable=True)
    assert estimate_auto_state_on() is False


def test_shadow_stats_espone_lo_stato_auto():
    configure_estimate(adaptive=False, shadow=True, auto_enable=True,
                       auto_min_n=10, auto_max_delta_pct=5.0)
    auto = estimate_shadow_stats()["auto"]
    assert auto == {"allowed": True, "on": False, "min_n": 10,
                    "max_delta_pct": 5.0}


def test_persistenza_shadow_roundtrip():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({}))
    estimate_tokens(MSGS)
    estimate_tokens(MSGS)
    snap = r.dump_stats()
    assert snap["estimate_shadow"]["n"] == 2
    r2 = Router(cfg, Policy.from_dict({}))
    load_estimate_shadow({"n": 0, "legacy": 0, "adaptive": 0})
    r2.load_stats(snap)
    assert estimate_shadow_stats()["n"] == 2
    os.unlink(path)


def test_load_shadow_ignora_payload_sporco():
    load_estimate_shadow({"n": 50, "legacy": 100, "adaptive": 300})
    assert estimate_shadow_stats()["n"] == 50
    load_estimate_shadow({"n": -3, "legacy": "x"})
    assert estimate_shadow_stats()["n"] == 50      # invariato
    load_estimate_shadow(None)                     # nessun crash
    load_estimate_shadow({"n": 5, "legacy": 0, "adaptive": 9})
    assert estimate_shadow_stats()["n"] == 50      # legacy<=0 -> rifiutato
