"""Fingerprint anonimo tollerante ai micro-cambi del system prompt: si hasha
solo la testa (primi N char), cosi' timestamp/contesto accodato ad ogni turno
non invalida sticky e prompt-cache."""
from app import main as M

_SYS_HEAD = "Istruzioni strutturali stabili e ripetute. " * 60  # > 768 char


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


def _pay(sys_txt):
    return {"messages": [
        {"role": "system", "content": sys_txt},
        {"role": "user", "content": "domanda iniziale sempre identica"}]}


def test_tail_change_keeps_same_fingerprint():
    a = M._session_id(_Req(), _pay(_SYS_HEAD))
    b = M._session_id(_Req(), _pay(_SYS_HEAD + " timestamp=2026-09-13T18:00"))
    assert a and a.startswith("fq_")
    assert a == b


def test_head_change_changes_fingerprint():
    a = M._session_id(_Req(), _pay("AAA " * 400))
    b = M._session_id(_Req(), _pay("BBB " * 400))
    assert a != b


def test_knob_zero_uses_whole_system(monkeypatch):
    monkeypatch.setattr(M.policy, "anon_session_fp_system_chars", 0)
    a = M._session_id(_Req(), _pay(_SYS_HEAD))
    b = M._session_id(_Req(), _pay(_SYS_HEAD + " tail"))
    assert a != b


def test_knob_parsing():
    from app.policy import Policy
    assert Policy.from_dict({}).anon_session_fp_system_chars == 768
    assert Policy.from_dict(
        {"anon_session_fp_system_chars": 128}).anon_session_fp_system_chars == 128
