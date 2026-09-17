"""Sanitizer dei campi client-only NON standard (es. `fallback_models`).

[IT] Alcuni client (es. le opzioni degli agenti opencode) aggiungono al body di
/chat/completions campi non-OpenAI che i provider severi (Google via
/v1beta/openai) rifiutano con 400 "Unknown name". Il gateway li rimuove prima
dell'invio a monte (`forwarder.strip_client_fields`).
[EN] Client-only non-OpenAI fields are stripped from the upstream body.
"""
from app.forwarder import set_strip_client_fields, strip_client_fields


def test_strip_removes_fallback_models():
    body = {"model": "m", "messages": [], "fallback_models": ["x"]}
    assert strip_client_fields(body) == 1
    assert "fallback_models" not in body
    assert body["model"] == "m"          # il resto resta intatto


def test_strip_noop_when_absent():
    body = {"model": "m", "messages": []}
    assert strip_client_fields(body) == 0


def test_strip_custom_list():
    body = {"a": 1, "b": 2}
    assert strip_client_fields(body, ["a", "zzz"]) == 1
    assert body == {"b": 2}


def test_strip_non_dict_safe():
    assert strip_client_fields(None) == 0
    assert strip_client_fields(["x"]) == 0


def test_set_strip_client_fields_updates_default():
    try:
        set_strip_client_fields(["custom_field"])
        body = {"custom_field": 1, "fallback_models": 2}
        assert strip_client_fields(body) == 1
        assert "custom_field" not in body
        assert "fallback_models" in body   # non piu' nella denylist
    finally:
        set_strip_client_fields(["fallback_models"])


def test_policy_default_and_parse():
    from app.policy import Policy
    p = Policy()
    assert p.strip_client_fields == ["fallback_models"]
    p2 = Policy.from_dict({"strip_client_fields": ["foo", "bar"]})
    assert p2.strip_client_fields == ["foo", "bar"]