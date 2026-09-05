"""Regressione: header x-opencode-session calcolato al volo (niente colonna CSV).

La sessione NON viene letta da una colonna del CSV: e' derivata al volo
nel forwarder (hash di api_key + client_ip + profilo) oppure passata dritta
se il client la invia gia' (passthrough). Stessa chiave+client+profilo ->
stessa sessione; chiavi diverse o profili diversi o IP diversi -> sessioni
diverse; mai la chiave in chiaro. Formato: 8 hex come le sessioni opencode.
"""
import hashlib

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


def test_is_sha256_prefix_8_hex():
    got = _session_headers(_dep("sk-X"), profile="p", client_ip="9.9.9.9")
    full = hashlib.sha256(b"sk-X|9.9.9.9|p").hexdigest()
    assert got["x-opencode-session"] == full[:8]
    # formato 8 hex come le sessioni native opencode (es. "5f634ae1")
    v = got["x-opencode-session"]
    assert len(v) == 8 and all(c in "0123456789abcdef" for c in v)
    # la chiave non appare MAI in chiaro nell'header
    assert "sk-X" not in got["x-opencode-session"]


def test_passthrough_session_wins():
    # se il client invia l'header, quel valore prevale su ogni hash
    got = _session_headers(_dep("sk-Z"), profile="pz", client_ip="1.1.1.1",
                           session="client-session-123")
    assert got == {"x-opencode-session": "client-session-123"}