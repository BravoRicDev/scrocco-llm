"""Estrazione del cluster Circuit Breaker in app/routing/circuit_breaker.py.

[IT] Verifica che il refactor sia PURAMENTE STRUTTURALE: il mixin esiste, il
Router lo compone, i metodi CB sono raggiungibili sull'istanza e si comportano
come prima (stessi valori, stessi short-circuit, nessuna eccezione).
[EN] Guards the structural extraction of the circuit-breaker cluster: the mixin
is importable, the Router composes it and the moved methods still behave.
"""
from unittest.mock import MagicMock

from app.policy import Policy
from app.router import Router
from app.routing.circuit_breaker import CircuitBreakerMixin

CB_METHODS = (
    # sezione 1: ibrido per-deployment + per-API-key
    "_cb_scope", "_dep_cb_store", "_get_cb_entry", "_dep_of",
    "_get_circuit_breaker", "_get_dep_circuit_breaker",
    "_cb_register_failure", "_cb_register_success",
    "_cb_block_or_transition", "_is_key_level_failure",
    "_update_circuit_breaker_on_failure",
    "_update_circuit_breaker_on_success", "_is_circuit_open",
    # sezione 2: F25 proattivo per provider|modello
    "_model_cb_key", "_key_tag_of", "_note_model_failure", "_model_blocked",
)


def _dep(unique, api_key="k1", model="m-a", group="test-128k"):
    return {"unique": unique, "group": group, "model": model,
            "api_base": "https://api.groq.com/openai/v1", "api_key": api_key,
            "tier": 0, "max_input_tokens": 128000,
            "needs_openai_provider": True, "priority": 0,
            "caps": frozenset({"text"}), "provider": "groq",
            "effort_capable": False, "intelligence": 5, "media_defer": True,
            "tool_repair": "", "model_preference": 0}


def _mk_router(deps):
    """Router minimale: pattern `Router.__new__` usato anche dai test CB."""
    r = Router.__new__(Router)
    r.policy = Policy()
    r.config = MagicMock()
    r.config.groups = {"test-128k": list(deps)}
    r.config.deployment_by_unique = lambda u: next(
        (d for d in r.config.groups["test-128k"] if d["unique"] == u), None)
    for a in ("_base_scores", "_provider_scores", "_key_scores",
              "_avg_latencies", "_cooldown", "_cooldown_since", "_stats",
              "_sticky_dep", "_sticky", "_session_group", "_defer_active",
              "_cap_strikes", "_circuit_breakers", "_dep_circuit_breakers",
              "_model_cb"):
        setattr(r, a, {})
    return r


# --- struttura ------------------------------------------------------------

def test_mixin_importable_and_composed():
    assert issubclass(Router, CircuitBreakerMixin)
    assert isinstance(_mk_router([_dep("dA")]), CircuitBreakerMixin)


def test_router_exposes_all_cb_methods():
    r = _mk_router([_dep("dA")])
    for name in CB_METHODS:
        assert callable(getattr(r, name)), name


def test_cb_methods_live_in_the_mixin_not_in_router_module():
    # i metodi sono estratti: nessuno resta definito nel corpo di Router
    for name in CB_METHODS:
        assert name not in vars(Router), name


def test_composition_order_keeps_single_definition():
    # nessun metodo omonimo fra i mixin: la MRO non puo' mascherare nulla
    from app.router import CanaryMixin, SessionMixin, WarmMixin
    mixins = (WarmMixin, CanaryMixin, SessionMixin, CircuitBreakerMixin)
    for name in CB_METHODS:
        owners = [m for m in mixins if name in vars(m)]
        assert len(owners) == 1, (name, owners)


# --- comportamento basico -------------------------------------------------

def test_cb_scope_falls_back_to_valid_values():
    r = _mk_router([_dep("dA")])
    for raw, expected in (("hybrid", "hybrid"), ("dep", "dep"),
                          ("key", "key"), ("DEP", "dep"), (None, "hybrid"),
                          ("", "hybrid"), ("bogus", "hybrid")):
        r.policy.circuit_breaker_scope = raw
        assert r._cb_scope() == expected, raw


def test_is_circuit_open_false_for_unknown_deployment():
    r = _mk_router([_dep("dA")])
    assert r._is_circuit_open("dA") is False          # nessun breaker -> no raise
    assert r._is_circuit_open("dA") is False          # e non crea entry spuria
    assert r._dep_circuit_breakers == {}
    assert r._circuit_breakers == {}


def test_register_failure_and_success_run_clean():
    r = _mk_router([_dep("dA")])
    store = r._dep_cb_store()
    r._cb_register_failure(store, "dA", "dep", "dA")
    entry = r._get_cb_entry(store, "dA")
    assert entry["failures"] == 1 and entry["state"] == "closed"
    r._cb_register_success(store, "dA", "dep", "dA")
    assert entry["failures"] == 0
    # successo su chiave assente: no-op, nessuna eccezione
    r._cb_register_success(store, "inesistente", "dep", "inesistente")


def test_model_cb_key_and_key_tag_are_coherent():
    r = _mk_router([_dep("dA", "k1", "m-a")])
    dep = r._dep_of("dA")
    assert r._model_cb_key(dep) == "groq|m-a"
    tag = r._key_tag_of(dep)
    assert tag == r._key_tag_of(dep)                  # stabile
    assert len(tag) == 12
    assert r._key_tag_of({**dep, "api_key": "k2"}) != tag
    assert r._key_tag_of({**dep, "api_key": ""}) == ""


def test_model_blocked_and_note_failure_soft_paths():
    r = _mk_router([_dep("dA")])
    dep = r._dep_of("dA")
    assert r._model_blocked(dep) is False             # store vuoto -> chiuso
    r._note_model_failure(None, 500)                 # dep None: no-op
    r._note_model_failure(dep, 400)                  # sotto il 500: ignorato
    assert r._model_cb == {}
