"""Unit test del gate per-client opencode.ai (app/opencode_gate.py) e della
delega del forwarder sullo stesso modulo.

Il gate decide se un upstream opencode.ai (zen / zen/go) e' utilizzabile:
  - client opencode reale (user-agent `opencode/...` o header `x-opencode-*`),
    oppure env OPENCODE_SPOOF_HEADERS truthy;
  - contesti interni (probe/background) senza decisione per-request ->
    ricadono sulla sola env.
"""
import pytest

from app.opencode_gate import (allow_opencode, client_can_use_opencode,
                               client_is_opencode, dep_usable, is_opencode_dep,
                               set_allow_opencode, spoof_enabled)


@pytest.fixture(autouse=True)
def _reset_gate(monkeypatch):
    """Isola lo stato ContextVar tra un test e l'altro."""
    monkeypatch.delenv("OPENCODE_SPOOF_HEADERS", raising=False)
    set_allow_opencode(None)
    yield
    set_allow_opencode(None)


# ------------------------------------------------------------------- dep
def test_is_opencode_dep_zen_and_go():
    assert is_opencode_dep({"api_base": "https://opencode.ai/zen/v1"})
    assert is_opencode_dep({"api_base": "https://opencode.ai/zen/go/v1"})
    assert is_opencode_dep({"api_base": "HTTPS://OPENCODE.AI/zen/v1"})


def test_is_opencode_dep_other_and_empty():
    assert not is_opencode_dep({"api_base": "https://api.groq.com/openai/v1"})
    assert not is_opencode_dep({"api_base": "https://openrouter.ai/api/v1"})
    assert not is_opencode_dep({})
    assert not is_opencode_dep(None)


# ----------------------------------------------------------------- client
def test_client_is_opencode_by_ua():
    assert client_is_opencode({"user-agent": "opencode/1.18.31 ai-sdk/x"})
    assert client_is_opencode({"user-agent": "Opencode/2.0.0"})       # case-ins
    assert not client_is_opencode({"user-agent": "OpenAI/Python 2.26.0"})
    assert not client_is_opencode({"user-agent": "curl/8.5.0"})
    assert not client_is_opencode({})


def test_client_is_opencode_by_xopencode_header():
    assert client_is_opencode({"x-opencode-client": "cli"})
    assert client_is_opencode({"x-opencode-request": "req-1"})
    assert client_is_opencode({"x-opencode-session": "ses_abc"})


def test_client_can_use_opencode_without_spoof():
    assert client_can_use_opencode({"user-agent": "opencode/1.18.31"})
    assert not client_can_use_opencode({"user-agent": "OpenAI/Python 2.26.0"})


def test_client_can_use_opencode_with_spoof(monkeypatch):
    monkeypatch.setenv("OPENCODE_SPOOF_HEADERS", "1")
    assert spoof_enabled()
    assert client_can_use_opencode({"user-agent": "curl/8.5.0"})


# ------------------------------------------------------------- ContextVar
def test_allow_opencode_default_none_falls_back_to_spoof(monkeypatch):
    set_allow_opencode(None)
    assert allow_opencode() is False
    monkeypatch.setenv("OPENCODE_SPOOF_HEADERS", "1")
    assert allow_opencode() is True


def test_set_allow_opencode_wins_over_env(monkeypatch):
    monkeypatch.setenv("OPENCODE_SPOOF_HEADERS", "1")
    set_allow_opencode(False)
    assert allow_opencode() is False
    monkeypatch.delenv("OPENCODE_SPOOF_HEADERS", raising=False)
    set_allow_opencode(True)
    assert allow_opencode() is True


def test_dep_usable_gate(monkeypatch):
    zen = {"api_base": "https://opencode.ai/zen/v1"}
    other = {"api_base": "https://api.groq.com/openai/v1"}
    set_allow_opencode(False)
    assert not dep_usable(zen)
    assert dep_usable(other)                     # gli altri upstream passano
    set_allow_opencode(True)
    assert dep_usable(zen)


def test_dep_usable_internal_context_uses_spoof(monkeypatch):
    zen = {"api_base": "https://opencode.ai/zen/v1"}
    set_allow_opencode(None)                     # nessuna decisione per-request
    assert not dep_usable(zen)                   # spoof off -> probe escluse
    monkeypatch.setenv("OPENCODE_SPOOF_HEADERS", "1")
    assert dep_usable(zen)


# ------------------------------------------------- forwarder delegation
def test_forwarder_helpers_delegate_to_gate():
    from app.forwarder import _client_is_opencode, _is_opencode_upstream
    assert _is_opencode_upstream({"api_base": "https://opencode.ai/zen/v1"})
    assert not _is_opencode_upstream({"api_base": "https://api.openai.com/v1"})
    assert _client_is_opencode({"user-agent": "opencode/1.18.31"})
    assert not _client_is_opencode({"user-agent": "curl/8.5.0"})


# --------------------------------------------------- main.py set point
def test_main_set_opencode_gate_wiring(monkeypatch):
    monkeypatch.delenv("OPENCODE_SPOOF_HEADERS", raising=False)
    from app import main as m

    class _Req:
        def __init__(self, headers):
            self.headers = headers

    m._set_opencode_gate(_Req({"user-agent": "opencode/1.18.31"}))
    assert allow_opencode() is True
    m._set_opencode_gate(_Req({"user-agent": "OpenAI/Python 2.26.0"}))
    assert allow_opencode() is False
