"""Regressione: header x-opencode-session calcolato al volo (niente colonna CSV).

La sessione NON viene letta da una colonna del CSV: e' derivata al volo
nel forwarder (hash di api_key + client_ip + profilo) oppure passata dritta
se il client la invia gia' (passthrough). Stessa chiave+client+profilo ->
stessa sessione; chiavi diverse o profili diversi o IP diversi -> sessioni
diverse; mai la chiave in chiaro. Formato: nativo opencode (ses_ + 26 char),
verificato sui log reali.
"""
import re

from app.forwarder import _session_headers


def _dep(key="sk-secret-key"):
    return {"api_key": key}


def test_same_key_same_ip_same_profile_same_session():
    a = _session_headers(_dep("sk-A"), profile="p1", client_ip="1.2.3.4")
    b = _session_headers(_dep("sk-A"), profile="p1", client_ip="1.2.3.4")
    assert a == b == {"x-opencode-session": a["x-opencode-session"]}


def test_different_key_different_session():
    a = _session_headers(_dep("sk-A"), profile="p1", client_ip="1.2.3.4")
    b = _session_headers(_dep("sk-B"), profile="p1", client_ip="1.2.3.4")
    assert a != b


def test_different_ip_different_session():
    a = _session_headers(_dep("sk-A"), profile="p1", client_ip="1.2.3.4")
    b = _session_headers(_dep("sk-A"), profile="p1", client_ip="5.6.7.8")
    assert a != b


def test_different_profile_same_key_same_ip_different_session():
    # 2 profili condividono la stessa chiave dallo stesso IP -> sessioni diverse
    a = _session_headers(_dep("sk-A"), profile="profilo-uno",
                         client_ip="1.2.3.4")
    b = _session_headers(_dep("sk-A"), profile="profilo-due",
                         client_ip="1.2.3.4")
    assert a != b


def test_fallback_matches_native_format():
    got = _session_headers(_dep("sk-X"), profile="p", client_ip="9.9.9.9")
    # formato NATIVO opencode: ses_ + 12 hex lowercase + 14 base62 (26 char)
    # verificato sui log reali (es. ses_fb5856a92ffekeJf366z10YP19)
    v = got["x-opencode-session"]
    assert re.fullmatch(r"ses_[0-9a-f]{12}[A-Za-z0-9]{14}", v) is not None
    assert len(v) == 30  # "ses_" + 26
    # la chiave non appare MAI in chiaro nell'header
    assert "sk-X" not in got["x-opencode-session"]


def test_fallback_deterministic_and_differentiated():
    # deterministico: stesso input -> stesso valore
    a = _session_headers(_dep("sk-A"), profile="p1", client_ip="1.2.3.4")
    b = _session_headers(_dep("sk-A"), profile="p1", client_ip="1.2.3.4")
    assert a == b
    # diverso se cambia anche solo il profilo (stessa chiave/IP)
    c = _session_headers(_dep("sk-A"), profile="p2", client_ip="1.2.3.4")
    assert a != c


def test_passthrough_session_wins():
    # se il client invia l'header, quel valore prevale su ogni hash
    got = _session_headers(_dep("sk-Z"), profile="pz", client_ip="1.1.1.1",
                           session="client-session-123")
    assert got == {"x-opencode-session": "client-session-123"}


# ---------------------------------------------------------------------------
# _opencode_session (app/main.py): header di sessione in arrivo dal client.
# Verificato via sniffing: opencode 1.18.x NON invia x-opencode-session ma
# invia x-session-affinity / x-session-id con lo stesso valore del body.
# ---------------------------------------------------------------------------
class _FakeHeaders(dict):
    """Minimo stub: dict con .get() per simulare request.headers."""

    def get(self, key, default=None):
        return dict.get(self, key, default)


class _FakeRequest:
    def __init__(self, headers):
        self.headers = _FakeHeaders(headers)


from app.main import _opencode_session


def test_opencode_session_x_opencode_session_wins():
    req = _FakeRequest({
        "x-opencode-session": "alpha",
        "x-session-affinity": "beta",
        "x-session-id": "gamma",
    })
    assert _opencode_session(req) == "alpha"


def test_opencode_session_uses_x_session_affinity():
    # opencode reale NON invia x-opencode-session -> si usa x-session-affinity
    req = _FakeRequest({
        "x-session-affinity": "ses_fake0affinit0000000000000",
        "x-session-id": "ses_fake0affinit0000000000000",
    })
    assert _opencode_session(req) == "ses_fake0affinit0000000000000"


def test_opencode_session_falls_back_to_x_session_id():
    req = _FakeRequest({"x-session-id": "ses_solo-questo"})
    assert _opencode_session(req) == "ses_solo-questo"


def test_opencode_session_none_when_no_headers():
    assert _opencode_session(_FakeRequest({})) is None
    # header vuoti non contano come sessione
    req = _FakeRequest({"x-session-affinity": "  "})
    assert _opencode_session(req) is None