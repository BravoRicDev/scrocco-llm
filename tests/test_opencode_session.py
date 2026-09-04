"""Regressione: header x-opencode-session calcolato al volo (niente colonna CSV).

La sessione NON viene piu' letta da una colonna del CSV: e' derivata al volo
nel forwarder (hash di api_key + client_ip) oppure passata dritta se il
client la invia gia' (passthrough). Stessa chiave+client -> stessa sessione;
chiavi diverse -> sessioni diverse; mai la chiave in chiaro.
"""
import hashlib

from app.forwarder import _session_headers


def _dep(key="sk-secret-key"):
    return {"api_key": key}


def test_same_key_same_ip_same_session():
    a = _session_headers(_dep("sk-A"), client_ip="1.2.3.4")
    b = _session_headers(_dep("sk-A"), client_ip="1.2.3.4")
    assert a == b == {"x-opencode-session": a["x-opencode-session"]}


def test_different_key_different_session():
    a = _session_headers(_dep("sk-A"), client_ip="1.2.3.4")
    b = _session_headers(_dep("sk-B"), client_ip="1.2.3.4")
    assert a != b


def test_different_ip_different_session():
    a = _session_headers(_dep("sk-A"), client_ip="1.2.3.4")
    b = _session_headers(_dep("sk-A"), client_ip="5.6.7.8")
    assert a != b


def test_is_sha256_hash_of_key_ip():
    got = _session_headers(_dep("sk-X"), client_ip="9.9.9.9")
    expected = hashlib.sha256(b"sk-X|9.9.9.9").hexdigest()
    assert got["x-opencode-session"] == expected
    # la chiave non appare MAI in chiaro nell'header
    assert "sk-X" not in got["x-opencode-session"]


def test_no_client_ip_hashes_key_alone():
    got = _session_headers(_dep("sk-Y"))
    assert got["x-opencode-session"] == \
        hashlib.sha256(b"sk-Y").hexdigest()


def test_passthrough_session_wins():
    # se il client invia l'header, quel valore prevale su ogni hash
    got = _session_headers(_dep("sk-Z"), client_ip="1.1.1.1",
                           session="client-session-123")
    assert got == {"x-opencode-session": "client-session-123"}