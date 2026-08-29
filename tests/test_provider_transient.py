"""Errore TRANSITORIO del provider/router a monte (4xx col body d'errore ma
NON un problema della richiesta): si RUOTA con cooldown corto, non si consegna
un 4xx al client che poi ritenta a vuoto."""
import pytest

from app.forwarder import (_PROVIDER_TRANSIENT_RE, is_provider_error_body,
                           PROVIDER_TRANSIENT_COOLDOWN_S)


@pytest.mark.parametrize("body", [
    '{"error":{"type":"server_error","message":"Error from provider (Console): '
    'Upstream request failed: [404] Provider returned error"}}',
    '{"error":{"message":"No endpoints found that support tool use","code":404}}',
    '{"error":{"message":"Provider returned error"}}',
    "upstream error: 502 Bad Gateway",
    '{"error":{"message":"temporarily unavailable, try again"}}',
    '{"error":{"message":"Service Unavailable"}}',
])
def test_matches_transient(body):
    assert _PROVIDER_TRANSIENT_RE.search(body)


@pytest.mark.parametrize("body", [
    '{"choices":[{"message":{"content":"ecco la risposta"}}]}',
    '{"error":{"type":"invalid_request_error","message":"messages required"}}',
    "you must provide an api key",
    '{"choices":[{"message":{"content":"def handle(): raise Error(\\"nope\\")"}}]}',
])
def test_does_not_match_legit(body):
    assert not _PROVIDER_TRANSIENT_RE.search(body)


def test_not_confused_with_provider_error_envelope():
    # {"error":{...}} annidato NON e' l'envelope {"type":"error",...} che il
    # fast-path is_provider_error_body salta: quello resta gestito a parte.
    body = ('{"error":{"type":"server_error","message":"Error from provider: '
            'Upstream request failed"}}')
    assert not is_provider_error_body(body)     # non e' l'envelope stretto
    assert _PROVIDER_TRANSIENT_RE.search(body)  # ma e' transitorio -> ruota


def test_cooldown_is_short():
    assert 0 < PROVIDER_TRANSIENT_COOLDOWN_S <= 300


# --- retryDelay dal BODY 429 (stile Google generativelanguage) ---

def test_retry_after_from_body_google_style():
    import httpx
    from app.forwarder import _retry_after_from

    def _resp(headers=None):
        return httpx.Response(429, headers=headers or {}, request=httpx.Request("POST", "http://x"))

    # header vince sempre
    assert _retry_after_from(_resp({"retry-after": "12"}), '{"retryDelay":"999s"}') == 12.0
    # body: RetryInfo "58s"
    assert _retry_after_from(_resp(), 'blah {"retryDelay": "58s"} blah') == 58.0
    # body: "Please retry in 58.93s"
    assert _retry_after_from(_resp(), 'Quota exceeded. Please retry in 58.930249303s.') == pytest.approx(58.93, abs=0.01)
    # body: {"seconds": 42}
    assert _retry_after_from(_resp(), '{"retryDelay": {"seconds": 42}}') == 42.0
    # cap a 300s
    assert _retry_after_from(_resp(), '{"retryDelay":"3600s"}') == 300.0
    # niente header, niente pattern -> None (escalation standard)
    assert _retry_after_from(_resp(), 'rate limit exceeded: free-models-per-day') is None
    assert _retry_after_from(_resp(), None) is None
