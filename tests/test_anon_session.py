"""Fingerprint di sessione per client ANONIMI (es. Hermes): id deterministico
`fq_<hash>` dal prefisso conversazione, cosi' sticky/cache funzionano anche
senza header di sessione."""
from app import main as M
from app.policy import Policy


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


_SYS = "Sei un agente. " * 6
_U1 = "Apri il file e riassumi cosa fa, per favore."
_U2 = "Ora aggiungi un test."


def _payload(user_text=None, extra=None):
    msgs = [{"role": "system", "content": _SYS},
            {"role": "user", "content": user_text or _U1}]
    if extra:
        msgs.extend(extra)
    return {"messages": msgs}


def test_fingerprint_stable_across_turns():
    p1 = _payload()
    p2 = _payload(extra=[{"role": "assistant", "content": "ok"},
                         {"role": "user", "content": _U2}])
    a = M._session_id(_Req(), p1)
    b = M._session_id(_Req(), p2)
    assert a and a.startswith("fq_")
    assert a == b


def test_fingerprint_differs_by_first_user():
    a = M._session_id(_Req(), _payload("prima domanda completamente diversa"))
    b = M._session_id(_Req(), _payload("altra domanda del tutto differente"))
    assert a != b


def test_fingerprint_includes_user_agent():
    a = M._session_id(_Req({"user-agent": "agent/1.0"}), _payload())
    b = M._session_id(_Req({"user-agent": "other/9.9"}), _payload())
    assert a != b


def test_explicit_header_wins():
    req = _Req({"x-session-id": "ses_explicit"})
    assert M._session_id(req, _payload()) == "ses_explicit"


def test_payload_user_wins():
    p = _payload()
    p["user"] = "utente-42"
    assert M._session_id(_Req(), p) == "utente-42"


def test_metadata_session_wins():
    p = _payload()
    p["metadata"] = {"session_id": "meta-7"}
    assert M._session_id(_Req(), p) == "meta-7"


def test_disabled_via_policy(monkeypatch):
    monkeypatch.setattr(M.policy, "anon_session_fingerprint", False)
    assert M._session_id(_Req(), _payload()) is None


def test_too_short_basis_is_anonymous():
    p = {"messages": [{"role": "user", "content": "hi"}]}
    assert M._session_id(_Req(), p) is None


def test_multimodal_content_text_extracted():
    p = {"messages": [
        {"role": "system", "content": _SYS},
        {"role": "user", "content": [
            {"type": "text", "text": _U1},
            {"type": "image_url", "image_url": {"url": "data:..."}}]}]}
    sid = M._session_id(_Req(), p)
    assert sid and sid.startswith("fq_")


def test_no_messages_is_anonymous():
    assert M._session_id(_Req(), {}) is None


def test_policy_parsing_flag():
    assert Policy.from_dict({}).anon_session_fingerprint is True
    assert Policy.from_dict(
        {"anon_session_fingerprint": False}).anon_session_fingerprint is False
