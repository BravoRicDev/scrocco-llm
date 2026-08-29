"""Test del modulo capacità: normalize_caps, required_caps, count_image_parts."""
import pytest

from app.capabilities import (CapabilitiesError, CANONICAL_CAPS,
                              count_image_parts, normalize_caps, required_caps)


# ---------------------------------------------------------------- normalize
def test_normalize_list_ok():
    assert normalize_caps(["text", "vision"], "t") == frozenset({"text", "vision"})


def test_normalize_string_comma():
    assert normalize_caps("vision, audio", "t") == frozenset({"vision", "audio"})


def test_normalize_none_empty():
    assert normalize_caps(None, "t") == frozenset()
    assert normalize_caps([], "t") == frozenset()
    assert normalize_caps("", "t") == frozenset()


def test_normalize_unknown_raises():
    with pytest.raises(CapabilitiesError):
        normalize_caps(["vision", "telepatia"], "t")


def test_normalize_wrong_type_raises():
    with pytest.raises(CapabilitiesError):
        normalize_caps(42, "t")


def test_canonical_set_complete():
    assert CANONICAL_CAPS == {"text", "vision", "video", "audio",
                              "image_gen", "tools", "tts", "stt", "video_gen"}


def test_normalize_accepts_audio_caps():
    assert normalize_caps(["tts", "stt"], "t") == frozenset({"tts", "stt"})


# ------------------------------------------------------------- required_caps
def _msg(parts_or_text):
    return {"role": "user", "content": parts_or_text}


def test_required_text_only_empty():
    payload = {"messages": [_msg("ciao")]}
    assert required_caps(payload) == frozenset()


def test_required_image_url_vision():
    payload = {"messages": [_msg([
        {"type": "text", "text": "cos'è?"},
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
    ])]}
    assert required_caps(payload) == frozenset({"vision"})


def test_required_input_image_vision():
    payload = {"messages": [_msg([{"type": "input_image"}])]}
    assert required_caps(payload) == frozenset({"vision"})


def test_required_input_audio_audio():
    payload = {"messages": [_msg([
        {"type": "input_audio", "input_audio": {"data": "x", "format": "wav"}},
    ])]}
    assert required_caps(payload) == frozenset({"audio"})


def test_required_video_url_video():
    payload = {"messages": [_msg([{"type": "video_url"}])]}
    assert required_caps(payload) == frozenset({"video"})


def test_required_file_video_mime():
    payload = {"messages": [_msg([
        {"type": "file", "file": {"mime_type": "video/mp4"}},
    ])]}
    assert required_caps(payload) == frozenset({"video"})


def test_required_inline_data_video():
    payload = {"messages": [_msg([
        {"type": "inline_data", "mime_type": "video/webm"},
    ])]}
    assert required_caps(payload) == frozenset({"video"})


def test_required_mixed_union():
    payload = {"messages": [
        _msg([{"type": "image_url"}, {"type": "input_audio"}]),
        _msg([{"type": "video_url"}]),
    ]}
    assert required_caps(payload) == frozenset({"vision", "audio", "video"})


def test_required_ignores_unknown_parts_and_garbage():
    payload = {"messages": [
        _msg([{"type": "quantum_entanglement"}]),
        "stringa non dict",
        {"role": "assistant", "content": None},
        None,
    ]}
    assert required_caps(payload) == frozenset()


def test_required_no_messages():
    assert required_caps({}) == frozenset()
    assert required_caps({"messages": []}) == frozenset()


# --------------------------------------------------------- count_image_parts
def test_count_images_zero_on_text():
    assert count_image_parts([_msg("ciao")]) == 0


def test_count_images_counts_both_types():
    msgs = [_msg([{"type": "image_url"}, {"type": "input_image"},
                  {"type": "text", "text": "x"}])]
    assert count_image_parts(msgs) == 2


def test_count_images_none():
    assert count_image_parts(None) == 0
