"""Feature D1: fallback automatico su risposta a 0 token (completion_tokens=0)
nel percorso NON-STREAMING + 3 knob di policy (rotate_on_length_empty,
stream_buffer_ms, stream_emit_error_tail).

Testa le funzioni pure: check_sanity (app/qc.py) e il parsing policy
(app/policy.py). Niente rete: i dati upstream sono costruiti a mano.
"""
import pytest

from app.qc import check_sanity, _request_is_empty
from app.policy import Policy, QcSanity


def _san(**kw):
    s = QcSanity()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _resp(content=None, finish_reason=None, tool_calls=None, usage=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    data = {"choices": [{"message": msg, "finish_reason": finish_reason}]}
    if usage is not None:
        data["usage"] = usage
    return data


# ---------- check_sanity: usage.completion_tokens == 0 ----------

def test_sanity_0token_with_real_prompt_fails():
    # input reale (prompt_tokens=280), output 0 token, content vuoto
    data = _resp(content="", usage={"prompt_tokens": 280,
                                    "completion_tokens": 0})
    payload = {"messages": [{"role": "user", "content": "domanda?"}]}
    assert check_sanity(data, payload, _san()) == \
        "output 0 token (completion_tokens=0)"


def test_sanity_0token_but_real_text_passes():
    # provider mente sul conteggio: c'e' testo reale -> NON scartare
    data = _resp(content="ciao", usage={"prompt_tokens": 10,
                                        "completion_tokens": 0})
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    assert check_sanity(data, payload, _san()) is None


def test_sanity_0token_empty_request_passes():
    # completion=0, prompt=0 e messaggi tutti vuoti -> input vuoto, legittimo
    data = _resp(content="", usage={"prompt_tokens": 0,
                                    "completion_tokens": 0})
    payload = {"messages": [{"role": "user", "content": "   "},
                            {"role": "system", "content": ""}]}
    assert check_sanity(data, payload, _san()) is None


def test_sanity_no_usage_real_text_passes():
    # usage assente, content presente -> niente da segnalare
    data = _resp(content="risposta")
    payload = {"messages": [{"role": "user", "content": "ciao"}]}
    assert check_sanity(data, payload, _san()) is None


def test_sanity_tool_calls_0token_passes():
    # tool_calls presenti: eccezione legittima anche con content=None e ct=0
    data = _resp(content=None, tool_calls=[{"id": "x", "type": "function"}],
                 usage={"prompt_tokens": 50, "completion_tokens": 0})
    payload = {"messages": [{"role": "user", "content": "chiama?"}]}
    assert check_sanity(data, payload, _san()) is None


def test_sanity_0token_with_images_passes():
    # immagini allegate: eccezione legittima
    data = _resp(content="", usage={"prompt_tokens": 50,
                                    "completion_tokens": 0})
    data["choices"][0]["message"]["images"] = ["data:image/png;base64,xx"]
    payload = {"messages": [{"role": "user", "content": "descrivi"}]}
    assert check_sanity(data, payload, _san()) is None


# ---------- _request_is_empty helper ----------

def test_request_is_empty_variants():
    assert _request_is_empty({"messages": [{"role": "user",
                                            "content": "  "}]}) is True
    assert _request_is_empty({"messages": [{"role": "user",
                                            "content": "x"}]}) is False
    # content come lista di parti (multimodale)
    assert _request_is_empty({"messages": [{"role": "user", "content": [
        {"type": "text", "text": ""}, {"type": "image_url", "image_url": {}}]}]}) \
        is True
    assert _request_is_empty({"messages": [{"role": "user", "content": [
        {"type": "text", "text": "y"}]}]}) is False
    # campo input/prompt di comodo
    assert _request_is_empty({"input": "abc"}) is False
    assert _request_is_empty({"prompt": ""}) is True


# ---------- parsing policy: knob anti-stallo streaming ----------

def test_policy_parse_stream_knobs():
    p = Policy.from_dict({
        "qc_sanity": {"rotate_on_length_empty": True},
        "qc_json": {"stream_first_content_ms": 8000,
                    "stream_total_deadline_ms": 60000,
                    "stream_commit_include_reasoning": True},
    })
    assert p.qc_sanity.rotate_on_length_empty is True
    assert p.qc_json.stream_first_content_ms == 8000
    assert p.qc_json.stream_total_deadline_ms == 60000
    assert p.qc_json.stream_commit_include_reasoning is True
    # chiavi rimosse (stream_buffer_ms / stream_emit_error_tail /
    # on_empty_response): ignorate senza errore
    p2 = Policy.from_dict({"qc_json": {"stream_buffer_ms": 800,
                                       "on_empty_response": "boh"}})
    assert not hasattr(p2.qc_json, "stream_buffer_ms")
    assert not hasattr(p2.qc_json, "on_empty_response")


def test_policy_parse_stream_first_content_out_of_range():
    with pytest.raises(ValueError):
        Policy.from_dict({"qc_json": {"stream_first_content_ms": 500}})
    with pytest.raises(ValueError):
        Policy.from_dict({"qc_json": {"stream_first_content_ms": 999999}})


def test_policy_parse_cooldown_knobs():
    p = Policy.from_dict({
        "qc_json": {"watchdog_cooldown_sec": 45},
        "stale_cooldown_retry_sec": 120,
        "max_fallback_tries": 200,
    })
    assert p.qc_json.watchdog_cooldown_sec == 45
    assert p.stale_cooldown_retry_sec == 120
    assert p.max_fallback_tries == 200
    # default
    d = Policy.from_dict({})
    assert d.qc_json.watchdog_cooldown_sec == 90
    assert d.stale_cooldown_retry_sec == 300
    assert d.max_fallback_tries == 128
    with pytest.raises(ValueError):
        Policy.from_dict({"qc_json": {"watchdog_cooldown_sec": 99999}})
