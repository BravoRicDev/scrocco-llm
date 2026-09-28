"""Isolamento dello stato PERSISTENTE per la suite.

Senza isolamento i test girano contro `<repo>/var/` (VAR_DIR di app.main): i
percorsi REALI di retirement (keyhealth) e di warm-start (routing/cooldown)
scrivono su var/key_health.json ecc. Un test che provoca un retirement di un
dep di test lo PERSISTE, e i test successivi (nella stessa run e in quelle
seguenti) lo vedono come "retired": failure spurie e run lentissimi
(probe/timeout su dep fantasma).

Questa fixture autouse, per OGNI test, installa un KeyHealth nuovo su una dir
temporanea e redirige i file di stato persistente, cosi' nessun test puo'
inquinare gli altri ne' il repository.
"""
from __future__ import annotations

import pytest
import app.state as gw_state


@pytest.fixture(autouse=True)
def _isolate_gateway_state(tmp_path, monkeypatch):
    try:
        from app import main as M
    except Exception:                                  # pragma: no cover
        return

    try:
        from app.keyhealth import KeyHealth
        monkeypatch.setattr(gw_state, "KEYHEALTH", KeyHealth(str(tmp_path)),
                            raising=False)
    except Exception:                                  # pragma: no cover
        pass

    for attr, name in (("_stats_file", "adaptive_stats.json"),
                       ("_cooldown_file", "cooldown_state.json"),
                       ("_routing_file", "routing_state.json"),
                       ("_thought_sigs_file", "thought_sigs.json")):
        if hasattr(gw_state, attr):
            monkeypatch.setattr(gw_state, attr, tmp_path / name, raising=False)

    groups_keys = set(gw_state.config.groups)
    yield
    for k in list(gw_state.config.groups):
        if k not in groups_keys:
            gw_state.config.groups.pop(k, None)


def register_fake_deployment(name="scrocco-llm-test-fake", idx=0):
    """Registra un deployment finto in `gw_state.config.groups` per i test
    che devono attraversare il percorso reale di scelta del deployment.

    Auto-pulente: la fixture autouse `_isolate_gateway_state` rimuove in
    teardown ogni gruppo comparso durante il test.
    """
    dep = {"unique": "%s__fake__%d" % (name, idx), "group": name,
           "model": "fake-model", "api_key": "sk-fake-%d" % idx,
           "api_base": "https://fake.test/v1"}
    gw_state.config.groups.setdefault(name, []).append(dep)
    return dep


def stub_fallback_chain(monkeypatch, router, deps):
    """Sostituisce `router.fallback_next` con una rotazione lineare su `deps`.

    Il gruppo finto di `register_fake_deployment` non appartiene a nessun
    profilo/capacita' noto (niente `chains_cap`/`profile_dims`), quindi la
    catena di routing reale (dims/cap-group) lo tratterebbe come esaurito
    dopo un solo tentativo. Questo stub isola le regole di verdict/rotazione
    di `chat_stream` (l'oggetto sotto test) dalla catena di routing reale,
    che e' gia' coperta altrove (es. test_capability_groups.py).
    """
    remaining = list(deps)

    def _next(*_a, **_k):
        return remaining.pop(0) if remaining else None

    monkeypatch.setattr(router, "fallback_next", _next)
