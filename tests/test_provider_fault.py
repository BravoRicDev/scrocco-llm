"""Fault del provider nel leggere/parsare la richiesta (envelope OpenAI).

Guardia: NON va mai scambiato per errore il codice/testo scritto dal modello
o presente nella richiesta (es. codice di gestione errori di un'app).
"""

from app.forwarder import (is_embedded_provider_error, is_provider_error_body,
                           is_provider_fault_body)

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


def test_fault_phrase_outside_message_ignored():
    # La frase compare nella RICHIESTA rimandata indietro dal provider, non
    # nel message dell'errore: NON e' un fault del provider.
    detail = ('{"error":{"message":"context length exceeded"},'
              '"request":{"messages":[{"role":"user","content":'
              '"Could not read the request body."}]}}')
    assert is_provider_fault_body(detail) is False


def test_embedded_pure_envelope_true():
    assert is_embedded_provider_error(BYNARA) is True
    assert is_embedded_provider_error(
        '{"type":"error","error":{"type":"ModelError"},"metadata":{}}') is True


def test_embedded_model_code_false():
    # codice scritto dal modello / in una app: NON e' un envelope provider
    assert is_embedded_provider_error(
        'function handle() { return {"error":{"type":"bad_request"}}; }') is False
    assert is_embedded_provider_error(
        'if (e) { console.error("Could not read the request body."); }') is False


def test_embedded_thin_json_false():
    # JSON del modello senza le chiavi tipiche dell'envelope provider
    assert is_embedded_provider_error('{"error":{"type":"bad_request"}}') is False
    assert is_embedded_provider_error('{"type":"error"}') is False
    assert is_embedded_provider_error('{"message":"hello"}') is False
    assert is_embedded_provider_error('Could not read the request body.') is False
