"""Selezione Gemini: esclusione A MONTE quando la richiesta ha tool_call non firmate.

Regola (richiesta utente): se la request porta una history con tool_call prive
di thought_signature, Gemini NON deve comparire nella pila di selezione (come
una capability mancante), non essere "saltato" con tentativi finti. Se la
richiesta e' buona (nessuna tool_call non firmata) Gemini resta eleggibile e
usa normalmente il suo model_preference (es. +1000).

Il flag vive in una ContextVar per-request (`set_avoid_gemini`), letta dal
router tramite `_gemini_blocked` in pick_deployment / _walk_chain / sticky /
cooldown-wakeup / esc-pin.
"""
import contextlib
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router
from app.thought_sig import (reset_avoid_gemini, set_avoid_gemini,
                             should_avoid_gemini)

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,models/gemini-3.7-flash,google,https://generativelanguage.googleapis.com/v1beta/openai,free,1000,8000,5,sk-GEM,text
t@x.com,plain-model,test,https://example.test/v1,free,128,8000,5,sk-PLAIN,text
"""

POLICY_MAP = {"capability_routing": {"model_capabilities": {}}}

GEM_GROUP = "scrocco-llm-test-1000k"
PLAIN_GROUP = "scrocco-llm-test-128k"


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    pol = Policy.from_dict(POLICY_MAP)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, pol)
    yield r
    os.unlink(path)


@contextlib.contextmanager
def avoid_ctx(flag: bool):
    tok = set_avoid_gemini(flag)
    try:
        yield
    finally:
        reset_avoid_gemini(tok)


def _gem(router: Router) -> dict:
    return router.config.groups[GEM_GROUP][0]


# ----------------------------------------------------- flag / ContextVar
def test_flag_default_false_and_scoped():
    assert should_avoid_gemini() is False
    with avoid_ctx(True):
        assert should_avoid_gemini() is True
        with avoid_ctx(False):
            assert should_avoid_gemini() is False
        assert should_avoid_gemini() is True
    assert should_avoid_gemini() is False


# ----------------------------------------------------- _gemini_blocked
def test_gemini_blocked_only_when_flag_set(router):
    gem = _gem(router)
    with avoid_ctx(False):
        assert router._gemini_blocked(gem) is False
    with avoid_ctx(True):
        assert router._gemini_blocked(gem) is True


# ----------------------------------------------------- pick_deployment
def test_pick_allows_gemini_when_request_good(router):
    """Richiesta buona: Gemini eleggibile (prima era escluso sempre)."""
    with avoid_ctx(False):
        d = router.pick_deployment(GEM_GROUP)
    assert d is not None
    assert "gemini" in d["model"].lower()


def test_pick_excludes_gemini_when_unsigned_tools(router):
    """History con tool_call non firmate: Gemini NON compare tra i candidati."""
    with avoid_ctx(True):
        assert router.pick_deployment(GEM_GROUP) is None


def test_pick_still_works_for_non_gemini_when_avoid(router):
    with avoid_ctx(True):
        d = router.pick_deployment(PLAIN_GROUP)
    assert d is not None and d["model"] == "plain-model"


# ----------------------------------------------------- _walk_chain / fallback
def test_walk_chain_respects_avoid(router):
    gem = _gem(router)
    with avoid_ctx(True):
        assert router._walk_chain([gem["unique"]], None) is None
    with avoid_ctx(False):
        got = router._walk_chain([gem["unique"]], None)
    assert got is not None and got["unique"] == gem["unique"]


def test_initial_pick_falls_back_away_from_gemini_group(router):
    """initial_pick di un gruppo solo-Gemini, con avoid attivo, non lo usa."""
    with avoid_ctx(True):
        d = router.initial_pick("test", GEM_GROUP)
    assert d is None or "gemini" not in d["model"].lower()
