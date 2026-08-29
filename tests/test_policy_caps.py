"""Test policy: validazione capability_routing e risoluzione caps_for."""
import pytest

from app.capabilities import CapabilitiesError
from app.policy import Policy


def test_defaults():
    p = Policy()
    assert p.routing_active() is True
    assert p.model_capabilities == {}
    assert p.capabilities_default == frozenset({"text"})
    assert p.image_token_estimate == 800
    assert p.images_chat_fallback is True


def test_caps_for_exact_beats_glob():
    p = Policy.from_dict({"capability_routing": {"model_capabilities": {
        "*gpt-oss*": [text := "text", "vision"],
        "openai/gpt-oss-120b": [text],
    }}})
    # exact vince anche se il glob è più lungo? NO: exact ha priorità assoluta
    assert p.caps_for("openai/gpt-oss-120b") == frozenset({"text"})


def test_caps_for_longest_glob_wins():
    p = Policy.from_dict({"capability_routing": {"model_capabilities": {
        "*vl*": ["text", "vision"],
        "qwen/qwen2.5-vl-72b-instruct": ["text", "vision", "video"],
    }}})
    # il pattern più lungo tra i GLOB che matchano vince; l'exact è un caso a sé
    got = p.caps_for("qwen/qwen2.5-vl-72b-instruct")
    assert got == frozenset({"text", "vision", "video"})


def test_caps_for_default_when_no_match():
    p = Policy.from_dict({"capability_routing": {
        "model_capabilities": {"a/b": ["text"]},
        "capabilities_default": [text := "text", "tools"],
    }})
    assert p.caps_for("sconosciuto/x") == frozenset({"text", "tools"})


def test_from_dict_full_block():
    p = Policy.from_dict({
        "capability_routing": {
            "enabled": False,
            "model_capabilities": {"x/y": [c for c in ("text",)]},
            "capabilities_default": ["text", "audio"],
            "image_token_estimate": 500,
            "images_chat_fallback": False,
        },
    })
    assert p.routing_active() is False
    assert p.model_capabilities == {"x/y": ["text"]}
    assert p.capabilities_default == frozenset({"text", "audio"})
    assert p.image_token_estimate == 500
    assert p.images_chat_fallback is False


def test_from_dict_rejects_unknown_cap():
    with pytest.raises(ValueError, match="sconosciute"):
        Policy.from_dict({"capability_routing": {
            "model_capabilities": {"x/y": ["holodeck"]}}})


def test_from_dict_rejects_bad_map():
    with pytest.raises(ValueError):
        Policy.from_dict({"capability_routing": {"model_capabilities": ["nope"]}})


def test_from_dict_rejects_negative_image_tokens():
    with pytest.raises(ValueError):
        Policy.from_dict({"capability_routing": {"image_token_estimate": -1}})


def test_bool_coercion():
    p = Policy.from_dict({"capability_routing": {
        "enabled": "off", "images_chat_fallback": "yes"}})
    assert p.routing_active() is False
    assert p.images_chat_fallback is True


def test_normalize_via_policy_error_is_capabilitieserror():
    # normalize_caps solleva CapabilitiesError (sottoclasse ValueError)
    from app.capabilities import normalize_caps
    with pytest.raises(CapabilitiesError):
        normalize_caps(["nope"], "t")
