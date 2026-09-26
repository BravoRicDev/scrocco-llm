"""`capmeta`: le capability del gateway proiettate nei formati che i client
leggono (Ollama, OpenRouter, Anthropic-style, LiteLLM, llama.cpp).

Non esiste uno standard (l'oggetto Model di OpenAI non ha capability): qui
blocchiamo le proiezioni cosi' non divergono dagli endpoint reali.
"""
from app import capmeta


def test_ollama_enum_values_first():
    """I valori dell'enum Ollama vengono per primi, cosi' un client Ollama
    trova subito i nomi che conosce. Dietro restano i token nostri."""
    caps = {"text", "decision"}
    out = capmeta.ollama_capabilities(caps)
    assert out[0] == "completion"          # valore Ollama per `text`
    assert "decision" in out               # token nostro
    assert out.index("completion") < out.index("decision")


def test_ollama_is_superset_of_our_tokens():
    caps = {"text", "vision", "image_gen", "tts", "stt", "decision",
            "video_gen"}
    out = capmeta.ollama_capabilities(caps)
    for c in caps:                      # nessun token nostro perso
        assert c in out
    assert "completion" in out and "vision" in out
    assert "image" in out and "audio" in out


def test_modalities_openrouter_shape():
    mods = capmeta.modalities({"text", "vision", "decision"})
    assert mods["input_modalities"] == ["text", "image"]
    # `decisions` e' un valore dell'enum OpenRouter OutputModality
    assert "decisions" in mods["output_modalities"]
    assert "text" in mods["output_modalities"]
    assert "image" not in mods["output_modalities"]


def test_stt_and_tts_modalities():
    assert "speech" in capmeta.modalities({"tts"})["output_modalities"]
    assert "transcription" in capmeta.modalities({"stt"})["output_modalities"]
    assert "audio" in capmeta.modalities({"stt"})["input_modalities"]


def test_supported_endpoints_tell_how_to_call():
    assert capmeta.supported_endpoints({"decision"}) == ["/v1/systemone"]
    assert capmeta.supported_endpoints({"image_gen"}) == ["/v1/images/generations"]
    assert "/v1/audio/speech" in capmeta.supported_endpoints({"tts"})
    assert "/v1/chat/completions" in capmeta.supported_endpoints({"text"})


def test_supported_parameters_gated_on_chat():
    assert "tools" in capmeta.supported_parameters({"text"})
    # sola decision: niente tool calling via chat
    assert "tools" not in capmeta.supported_parameters({"decision"})
    # i parametri base ci sono sempre
    assert "temperature" in capmeta.supported_parameters({"decision"})


def test_structured_style_anthropic_shape():
    sx = capmeta.structured_style({"vision", "decision"})
    assert sx == {"vision": {"supported": True}, "decision": {"supported": True}}


def test_litellm_style_flags():
    info = capmeta.litellm_style({"text", "tts", "decision"})
    assert info["supports_function_calling"] is True
    assert info["supports_audio_output"] is True
    assert info["supports_decisions"] is True
    assert info["supports_vision"] is False
    assert "/v1/systemone" in info["supported_endpoints"]


def test_llama_cpp_modalities():
    m = capmeta.llama_cpp_modalities({"vision", "video_gen"})
    assert m["vision"] is True and m["video"] is True
    assert m["image"] is False and m["audio"] is False


def test_context_lengths_max_and_min():
    deps = [{"max_input_tokens": 200000}, {"max_input_tokens": 8000}]
    hi, lo = capmeta.context_lengths(deps)
    assert (hi, lo) == (200000, 8000)
    assert capmeta.context_lengths([]) == (0, 0)


def test_usage_hint_for_decision():
    hint = capmeta.usage_hint({"decision"})
    assert hint["decision"]["endpoint"] == "/v1/systemone"
    assert "questions" in hint["decision"]["body"]


def test_no_capability_invented_for_all():
    """Ogni capability canonica deve essere mappata da qualche parte: se ne
    aggiunge una nuova, qui fallisce finche' non si aggiorna la mappa."""
    from app.capabilities import CANONICAL_CAPS
    for c in CANONICAL_CAPS:
        assert (c in capmeta.OLLAMA
                or c in capmeta.IN_MODALITIES
                or c in capmeta.OUT_MODALITIES
                or c in capmeta.ENDPOINTS), c
