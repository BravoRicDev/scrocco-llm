"""Fault del provider nel leggere/parsare la richiesta (envelope OpenAI)."""

from app.forwarder import is_provider_error_body, is_provider_fault_body

BYNARA = ('{"error":{"type":"bad_request","message":"Could not read the '
          'request body.","request_id":"c9eac8ae-76be-4719"}}')


def test_bynara_fault_true():
    assert is_provider_fault_body(BYNARA) is True


def test_openai_envelope_without_fault_phrase_false():
    assert is_provider_fault_body(
        '{"error":{"message":"model not found"}}') is False


def test_plain_text_false():
    assert is_provider_fault_body("Could not read the request body.") is False


def test_empty_false():
    assert is_provider_fault_body("") is False


def test_provider_error_body_unchanged():
    # l'envelope OpenAI {"error":{...}} NON e' l'envelope {"type":"error",...}
    assert is_provider_error_body(BYNARA) is False


def test_malformed_request_variant_true():
    assert is_provider_fault_body(
        '{"error":{"type":"bad_request","message":"unable to parse the request"}}'
    ) is True
