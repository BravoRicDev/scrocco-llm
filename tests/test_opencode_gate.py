"""Unit test del gate per-client opencode.ai (app/opencode_gate.py) e della
delega del forwarder sullo stesso modulo.

Il gate distingue:
  - upstream **zen** (free): usabili solo da client opencode reale o con
    env OPENCODE_SPOOF_HEADERS truthy; in cautela opencode sono ultima scelta;
  - upstream **go** (a pagamento): indipendenti da spoof/client/cautela,
    regolati solo dall'interruttore OPENCODE_GO (default ON).
  - contesti interni (probe/background) senza decisione per-request ->
    ricadono sulla sola env.
"""
import pytest

from app.opencode_gate import (allow_opencode_zen, client_can_use_opencode_zen,
                               client_is_opencode, dep_usable, is_opencode_dep,
                               is_opencode_go_dep, is_opencode_zen_dep,
                               opencode_go_enabled, set_allow_opencode_zen,
                               set_spoofing_request, spoof_enabled)

_ZEN = {"api_base": "https://opencode.ai/zen/v1", "provider": "opencode-zen"}
_GO = {"api_base": "https://opencode.ai/zen/go/v1", "provider": "opencode-go"}


@pytest.fixture(autouse=True)
def _reset_gate(monkeypatch):
    """Isola lo stato ContextVar e le env tra un test e l'altro."""
    monkeypatch.delenv("OPENCODE_SPOOF_HEADERS", raising=False)
    monkeypatch.delenv("OPENCODE_GO", raising=False)
    monkeypatch.delenv("OPENCODE_CAUTIOUS", raising=False)
    set_allow_opencode_zen(None)
    set_spoofing_request(False)
    yield
    set_allow_opencode_zen(None)
    set_spoofing_request(False)


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


def test_zen_and_go_classification():
    assert is_opencode_zen_dep(_ZEN) and not is_opencode_go_dep(_ZEN)
    assert is_opencode_go_dep(_GO) and not is_opencode_zen_dep(_GO)
    assert not is_opencode_zen_dep({"provider": "groq"})
    assert not is_opencode_go_dep(None)


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


def test_client_can_use_opencode_zen_without_spoof():
    assert client_can_use_opencode_zen({"user-agent": "opencode/1.18.31"})
    assert not client_can_use_opencode_zen({"user-agent": "OpenAI/Python 2.26.0"})


def test_client_can_use_opencode_zen_with_spoof(monkeypatch):
    monkeypatch.setenv("OPENCODE_SPOOF_HEADERS", "1")
    assert spoof_enabled()
    assert client_can_use_opencode_zen({"user-agent": "curl/8.5.0"})


# ------------------------------------------------------------- ContextVar
def test_allow_opencode_zen_default_none_falls_back_to_internal(monkeypatch):
    """Contesti interni (nessuna decisione per-request): gli zen restano
    sondabili di default (`OPENCODE_ZEN_INTERNAL` default ON), cosi' i nativi
    li trovano caldi; interruttore dedicato per disattivarli."""
    monkeypatch.delenv("OPENCODE_ZEN_INTERNAL", raising=False)
    set_allow_opencode_zen(None)
    assert allow_opencode_zen() is True
    monkeypatch.setenv("OPENCODE_ZEN_INTERNAL", "0")
    assert allow_opencode_zen() is False


def test_set_allow_opencode_zen_wins_over_env(monkeypatch):
    monkeypatch.setenv("OPENCODE_SPOOF_HEADERS", "1")
    set_allow_opencode_zen(False)
    assert allow_opencode_zen() is False
    monkeypatch.delenv("OPENCODE_SPOOF_HEADERS", raising=False)
    set_allow_opencode_zen(True)
    assert allow_opencode_zen() is True


def test_dep_usable_zen_gate():
    other = {"api_base": "https://api.groq.com/openai/v1"}
    set_allow_opencode_zen(False)
    assert not dep_usable(_ZEN)
    assert dep_usable(other)                     # gli altri upstream passano
    set_allow_opencode_zen(True)
    assert dep_usable(_ZEN)


def test_dep_usable_zen_internal_context_uses_internal_knob(monkeypatch):
    monkeypatch.delenv("OPENCODE_ZEN_INTERNAL", raising=False)
    set_allow_opencode_zen(None)                 # nessuna decisione per-request
    assert dep_usable(_ZEN)                      # interno: probe/warm zen ON
    monkeypatch.setenv("OPENCODE_ZEN_INTERNAL", "0")
    assert not dep_usable(_ZEN)                  # interruttore dedicato OFF


def test_dep_usable_go_independent_of_spoof_and_zen_gate(monkeypatch):
    """go (a pagamento): usabile anche con spoof OFF e gate zen chiuso."""
    set_allow_opencode_zen(False)
    assert dep_usable(_GO)                       # default OPENCODE_GO=ON
    monkeypatch.setenv("OPENCODE_GO", "0")
    assert not dep_usable(_GO)                   # interruttore dedicato
    assert opencode_go_enabled() is False


def test_opencode_go_enabled_default_on(monkeypatch):
    assert opencode_go_enabled() is True
    monkeypatch.setenv("OPENCODE_GO", "false")
    assert opencode_go_enabled() is False
    monkeypatch.setenv("OPENCODE_GO", "1")
    assert opencode_go_enabled() is True


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
    assert allow_opencode_zen() is True
    m._set_opencode_gate(_Req({"user-agent": "OpenAI/Python 2.26.0"}))
    assert allow_opencode_zen() is False
