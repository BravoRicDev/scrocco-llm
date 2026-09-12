from app.sampling import (LoopConfig, SamplingConfig, apply_sampling_defaults,
                          detect_loop, response_loop_reason)


def _dep(provider="groq"):
    return {"provider": provider, "api_base": "https://api.groq.com/openai/v1"}


def test_apply_default_top_p():
    body = {}
    applied = apply_sampling_defaults(body, _dep())
    assert "top_p" in applied and body["top_p"] == 0.95


def test_client_wins():
    body = {"top_p": 0.5}
    applied = apply_sampling_defaults(body, _dep())
    assert "top_p" not in applied and body["top_p"] == 0.5


def test_allow_providers_restrict():
    cfg = SamplingConfig(provider_params={"*": {"top_p": 0.9}},
                         allow_providers=("openai",))
    body = {}
    assert apply_sampling_defaults(body, _dep("groq"), cfg) == []
    assert "top_p" not in body


def test_detect_repeated_ngram():
    text = ("alpha beta gamma delta epsilon zeta eta theta " * 4)
    assert detect_loop(text, None, SamplingConfig()) == "repeated_ngram"


def test_no_loop_short_text():
    assert detect_loop("ciao come stai", None, SamplingConfig()) is None


def test_repeated_toolcall():
    tcs = [{"function": {"name": "bash", "arguments": "{}"}},
           {"function": {"name": "bash", "arguments": "{}"}}]
    assert detect_loop("", tcs, SamplingConfig()) == "repeated_toolcall"


def test_response_loop_reason():
    data = {"choices": [{"message": {
        "content": "one two three four five six seven eight " * 4}}]}
    assert response_loop_reason(data, SamplingConfig()) == "repeated_ngram"
