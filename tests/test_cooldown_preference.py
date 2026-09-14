"""Cooldown residuo scalato dalla preferenza del deployment.

Storage GREZZO (`_cooldown`/`_cooldown_full`/`cooldown_state.json`); il fattore
(pref/2, max +/-50%) si applica SOLO al calcolo del residuo, e solo sopra i 60s
di dato grezzo."""
import os
import tempfile
import time

import pytest

from app import autoprobe
from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,model_preference
t@x.com,d-pref,prov,https://x/v1,free,200,0,0,K1,text,100
t@x.com,d-neg,prov,https://x/v1,free,200,0,0,K2,text,-100
t@x.com,d-zero,prov,https://x/v1,free,200,0,0,K3,text,0
"""


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({"cooldown_jitter_ratio": 0}))
    autoprobe._last_probe.clear()
    autoprobe._probe_times.clear()
    yield r
    os.unlink(path)


def _u(router, model):
    for deps in router.config.groups.values():
        for d in deps:
            if d.get("model") == model:
                return d["unique"]
    raise KeyError(model)


def _seed(router, unique, full, elapsed=0.0):
    """Cooldown grezzo di `full` secondi, iniziato `elapsed` secondi fa."""
    now = time.time()
    router._cooldown[unique] = now + full - elapsed
    router._cooldown_since[unique] = now - elapsed
    router._cooldown_full_map()[unique] = float(full)


@pytest.mark.parametrize("raw,pref,exp", [
    (60.0, 100, 1.0),      # soglia: 60s = grezzo, nessuna modifica
    (61.0, 100, 0.5),
    (3000.0, 100, 0.5),
    (3000.0, 200, 0.5),    # cap +50%
    (3000.0, 0, 1.0),
    (3000.0, -30, 1.15),
    (3000.0, -100, 1.5),
    (3000.0, -300, 1.5),   # cap -50%
])
def test_pref_factor(router, raw, pref, exp):
    assert router._pref_cooldown_factor(raw, pref) == pytest.approx(exp)


def test_storage_resta_grezzo(router):
    u = _u(router, "d-pref")
    router.mark_failed(u, seconds=3000)
    assert router._cooldown_full_map()[u] == pytest.approx(3000, rel=0.1)
    # il residuo efficace e' ~meta' (pref 100 -> x0.5)
    assert router.cooldown_residual(u) < 1800


def test_pref_accorcia_residuo(router):
    dp, dz = _u(router, "d-pref"), _u(router, "d-zero")
    router.mark_failed(dp, seconds=3000)
    router.mark_failed(dz, seconds=3000)
    assert router.cooldown_residual(dp) == pytest.approx(
        router.cooldown_residual(dz) * 0.5, rel=0.05)


def test_pref_negativa_allunga_residuo(router):
    dn, dz = _u(router, "d-neg"), _u(router, "d-zero")
    router.mark_failed(dn, seconds=3000)
    router.mark_failed(dz, seconds=3000)
    assert router.cooldown_residual(dn) == pytest.approx(
        router.cooldown_residual(dz) * 1.5, rel=0.05)


def test_sotto_60s_nessuna_modifica(router):
    for m in ("d-pref", "d-neg"):
        u = _u(router, m)
        router.mark_failed(u, seconds=30)
        assert router.cooldown_residual(u) == pytest.approx(30, abs=3)


def test_is_cooled_down_usa_residuo_scalato(router):
    dp, dz = _u(router, "d-pref"), _u(router, "d-zero")
    _seed(router, dp, full=100.0, elapsed=60.0)   # efficace: -60+50 < 0 -> no
    _seed(router, dz, full=100.0, elapsed=60.0)   # residuo 40 -> si
    assert router.is_cooled_down(dp) is False
    assert router.cooldown_residual(dp) == 0.0
    assert router.is_cooled_down(dz) is True
    assert router.cooldown_residual(dz) == pytest.approx(40, abs=2)


def test_double_residual_non_ri_applica_fattore(router):
    dp = _u(router, "d-pref")
    _seed(router, dp, full=1000.0)
    before = router.cooldown_residual(dp)          # ~500
    router.mark_failed_double_residual(dp)
    after = router.cooldown_residual(dp)           # grezzo x2 -> efficace x2
    assert after == pytest.approx(before * 2, rel=0.05)


def test_autoprobe_ordina_per_residuo_scalato(router):
    dp, dz = _u(router, "d-pref"), _u(router, "d-zero")
    _seed(router, dp, full=1000.0, elapsed=10.0)   # efficace ~490
    _seed(router, dz, full=1000.0, elapsed=10.0)   # efficace ~990
    targets = autoprobe._select_targets(
        router, "test", per_dim=1, min_age=1.0, min_gap=0.0, max_total=10)
    assert targets and targets[0][1] == dp          # preferito = residuo minore
