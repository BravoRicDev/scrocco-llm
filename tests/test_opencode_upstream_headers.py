"""Gate opencode.ai (zen / zen/go): identita' client opencode in upstream.

opencode.ai/zen risponde 403 FreeTierError "can only be used from within
OpenCode" alle richieste che non provengono da un client opencode reale.
Il gateway replica l'identita' del client (passthrough fedele) e sintetizza
i campi mancanti (x-opencode-client / x-opencode-request / x-opencode-project)
SOLO per upstream opencode.ai:

- client opencode reale (user-agent "opencode/..." o header x-opencode-*)
  -> header sempre presenti;
- client NON-opencode -> niente header (POC "trucco" OFF di default); con
  l'env OPENCODE_SPOOF_HEADERS attivo si sintetizza l'identita' (esperimento);
- upstream NON opencode.ai -> mai header opencode (gli altri provider li
  rifiutano o li ignorano, ma non vanno iniettati).
"""
import os

from app.forwarder import (_client_is_opencode, _is_native_session,
                           _is_opencode_upstream, _native_session_of,
                           _opencode_upstream_headers, _session_headers)


def _zen_dep(key="sk-zen-key"):
    return {"api_key": key, "api_base": "https://opencode.ai/zen/v1"}


def _go_dep(key="sk-go-key"):
    return {"api_key": key, "api_base": "https://opencode.ai/zen/go/v1"}


def _other_dep(key="sk-other"):
    return {"api_key": key, "api_base": "https://api.openai.com/v1"}


def _oc_headers(ua="opencode/1.18.31 ai-sdk/provider-utils/4.0.23 "
                  "runtime/bun/1.3.14"):
    return {"user-agent": ua, "x-session-affinity": "ses_f5204e4a7ffeBxQjwqn3m1wM0X"}


# ---------------------------------------------------------------- upstream
def test_is_opencode_upstream_only_opencode_ai():
    assert _is_opencode_upstream(_zen_dep())
    assert _is_opencode_upstream(_go_dep())
    assert not _is_opencode_upstream(_other_dep())


def test_client_is_opencode_by_ua():
    assert _client_is_opencode({"user-agent": "opencode/1.18.31 ..."})
    assert _client_is_opencode({"user-agent": "Opencode/2.0.0"})  # case-ins


def test_client_is_opencode_by_xopencode_header():
    assert _client_is_opencode({"x-opencode-client": "cli"})
    assert _client_is_opencode({"x-opencode-request": "req-123"})
    assert _client_is_opencode({"x-opencode-session": "ses_abc"})


def test_client_is_not_opencode():
    assert not _client_is_opencode({"user-agent": "curl/8.0"})
    assert not _client_is_opencode({"user-agent": "python-httpx/0.27"})
    assert not _client_is_opencode({})


# --------------------------------------------------------- client opencode
def test_opencode_client_gets_full_identity_zen():
    out = _opencode_upstream_headers(_zen_dep(),
                                     client_headers=_oc_headers())
    assert out["x-opencode-client"] == "cli"
    assert out["x-opencode-project"] == "default"
    assert "x-opencode-request" in out
    assert out["User-Agent"].startswith("opencode/1.18.31")


def test_opencode_client_gets_full_identity_go():
    out = _opencode_upstream_headers(_go_dep(),
                                     client_headers=_oc_headers())
    assert out["x-opencode-client"] == "cli"
    assert out["x-opencode-project"] == "default"


def test_request_id_passthrough_wins():
    out = _opencode_upstream_headers(
        _zen_dep(), client_headers={"user-agent": "opencode/1.18.31",
                                    "x-opencode-request": "req-client-1",
                                    "x-opencode-client": "tui",
                                    "x-opencode-project": "proj-x"})
    assert out["x-opencode-request"] == "req-client-1"
    assert out["x-opencode-client"] == "tui"
    assert out["x-opencode-project"] == "proj-x"
    assert out["User-Agent"].startswith("opencode/1.18.31")


# ----------------------------------------------------- client NON opencode
def test_non_opencode_client_no_headers_by_default(monkeypatch):
    monkeypatch.delenv("OPENCODE_SPOOF_HEADERS", raising=False)
    out = _opencode_upstream_headers(
        _zen_dep(), client_headers={"user-agent": "curl/8.0"})
    assert out == {}


def test_non_opencode_client_spoof_when_env(monkeypatch):
    monkeypatch.setenv("OPENCODE_SPOOF_HEADERS", "1")
    out = _opencode_upstream_headers(
        _zen_dep(), client_headers={"user-agent": "curl/8.0"})
    assert out["x-opencode-client"] == "cli"
    assert out["x-opencode-project"] == "default"
    assert out["User-Agent"].startswith("opencode/")
    assert "x-opencode-request" in out


def test_spoof_uses_default_ua_when_absent(monkeypatch):
    monkeypatch.setenv("OPENCODE_SPOOF_HEADERS", "1")
    out = _opencode_upstream_headers(_zen_dep(), client_headers={})
    assert out["User-Agent"].startswith("opencode/")
    assert out["x-opencode-client"] == "cli"


# ------------------------------------------------------- upstream non-zen
def test_other_upstream_never_gets_opencode_headers(monkeypatch):
    monkeypatch.setenv("OPENCODE_SPOOF_HEADERS", "1")
    out = _opencode_upstream_headers(_other_dep(),
                                     client_headers=_oc_headers())
    assert out == {}


# ----------------------------------------------------- session_headers glue
def test_session_headers_includes_opencode_identity():
    out = _session_headers(_zen_dep(), profile="p", client_ip="1.2.3.4",
                           attribution=_oc_headers())
    assert out["x-opencode-session"]  # sessione (passthrough o nativa)
    assert out["x-opencode-client"] == "cli"
    assert out["x-opencode-project"] == "default"
    assert "x-opencode-request" in out
    assert out["User-Agent"].startswith("opencode/")


def test_session_headers_non_opencode_client():
    out = _session_headers(_zen_dep(), profile="p", client_ip="1.2.3.4",
                           attribution={"user-agent": "curl/8.0"})
    # solo la sessione, nessun header opencode sintetizzato
    assert set(out) == {"x-opencode-session"}


def test_session_headers_other_upstream_ignores_identity():
    out = _session_headers(_other_dep(), profile="p", client_ip="1.2.3.4",
                           attribution=_oc_headers())
    assert set(out) == {"x-opencode-session"}


# -------------------------------- session id nativo (gate opencode.ai/zen)
def test_is_native_session_accepts_native_format():
    assert _is_native_session("ses_f5204e4a7ffeBxQjwqn3m1wM0X")
    assert _is_native_session(_native_session_of("qualunque-seed"))


def test_is_native_session_rejects_foreign_values():
    # il fingerprint interno, stringhe corte o prefisso sbagliato non passano
    assert not _is_native_session("fq_7c7a170864154b1d")
    assert not _is_native_session("abc")
    assert not _is_native_session("ses_x")
    assert not _is_native_session("")
    assert not _is_native_session(None)


def test_session_headers_passthrough_native_session():
    nat = _native_session_of("client")
    out = _session_headers(_zen_dep(), session=nat)
    assert out["x-opencode-session"] == nat


def test_session_headers_rewrites_foreign_session_to_native():
    # una sessione NON nativa (fingerprint anonimo fq_...) NON viene propagata
    # cosi' com'e': opencode.ai la rifiuterebbe con 403, quindi si rigenera
    # un id nativo verosimile.
    out = _session_headers(_zen_dep(), profile="p", client_ip="1.2.3.4",
                           session="fq_7c7a170864154b1d")
    sid = out["x-opencode-session"]
    assert sid != "fq_7c7a170864154b1d"
    assert _is_native_session(sid)


def test_session_headers_foreign_session_deterministic_and_distinct():
    def sid(seed):
        return _session_headers(_zen_dep(), profile="p", client_ip="1.2.3.4",
                                session=seed)["x-opencode-session"]
    assert sid("fq_aaa") == sid("fq_aaa")      # deterministico
    assert sid("fq_aaa") != sid("fq_bbb")      # distinto per sessione
    # distinto anche dalla sessione derivata senza sessione interna
    assert sid("fq_aaa") != _session_headers(
        _zen_dep(), profile="p", client_ip="1.2.3.4")["x-opencode-session"]