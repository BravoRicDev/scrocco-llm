"""STT: campi extra (allowlist del gateway + inoltro selettivo per provider).

`/v1/audio/transcriptions` legge dal multipart una allowlist e la passa a
`forwarder.transcribe`. Qui si blinda che:
  - `prompt`/`language`/`hotwords`/`vad_filter` arrivino al forwarder;
  - i campi sconosciuti siano scartati;
  - `hotwords`/`vad_filter` siano inoltrati SOLO ai server whisper self-hosted
    (Speaches/faster-whisper) e RIMOSSI sui cloud che li rifiutano (Groq: 400
    "unknown param" — verificato e2e).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.forwarder import Forwarder, _stt_extras_supported

_CSV_HEADER = ("commento,modello,provider,endpoint,data,context,max_input,"
               "priority,scrocco-llm-test,caps\n")
_ROW = ("a@x.com,whisper-x,speaches,http://speaches:8000/v1,free,8,8000,1,"
        "sk-stt,stt\n")

MK = {"Authorization": "Bearer test-master-stt"}


class _FakeFwd:
    def __init__(self):
        self.calls: list[dict] = []

    async def transcribe(self, dep, data_fields, file_bytes, filename,
                         content_type, path="transcriptions", **kw):
        self.calls.append({"dep": dep, "data": dict(data_fields), "path": path,
                           "file": file_bytes})
        return {"text": "ciao", "nx_deployment": dep["unique"]}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    csv = tmp_path / "k.csv"
    csv.write_text(_CSV_HEADER + _ROW)
    import app.main as m
    orig = (m.authn.master_key, m.config.csv_path)
    m.authn.master_key = "test-master-stt"
    m.LEDGER.flush()
    monkeypatch.setattr(m, "VAR_DIR", str(tmp_path))
    monkeypatch.setattr(m, "CSV_PATH", str(csv))
    monkeypatch.setattr(m.config, "csv_path", csv)
    m.config.reload()
    m.router.policy.cap_groups_enabled = True
    yield TestClient(m.app), m
    m.router._cooldown.clear()
    m.router.policy.cap_groups_enabled = False
    m.authn.master_key = orig[0]
    m.config.csv_path = orig[1]
    m.config.reload()


def _post(c, monkeypatch, m, **fields):
    fwd = _FakeFwd()
    monkeypatch.setattr(m, "forwarder", fwd)
    data = {"model": "scrocco-llm-test"}
    data.update(fields)
    r = c.post("/v1/audio/transcriptions", headers=MK, data=data,
               files={"file": ("a.wav", b"RIFF0000", "audio/wav")})
    return r, fwd


def test_stt_allowlist_inoltra_prompt_language_hotwords_vad(client,
                                                            monkeypatch):
    c, m = client
    r, fwd = _post(c, monkeypatch, m, language="it",
                   prompt="Dettatura tecnica: scrocco-llm",
                   hotwords="scrocco-llm deployment", vad_filter="true",
                   response_format="json", temperature="0")
    assert r.status_code == 200, r.text
    assert len(fwd.calls) == 1
    data = fwd.calls[0]["data"]
    assert data["language"] == "it"
    assert data["prompt"] == "Dettatura tecnica: scrocco-llm"
    assert data["hotwords"] == "scrocco-llm deployment"
    assert data["vad_filter"] == "true"
    assert data["response_format"] == "json"
    assert data["temperature"] == "0"
    assert r.json()["text"] == "ciao"


def test_stt_scarta_campi_sconosciuti(client, monkeypatch):
    c, m = client
    r, fwd = _post(c, monkeypatch, m, language="it",
                   hotwords="x", unknown_field="ignored", stream="true")
    assert r.status_code == 200, r.text
    data = fwd.calls[0]["data"]
    assert data["hotwords"] == "x"
    assert "unknown_field" not in data
    assert "stream" not in data


def test_stt_senza_campi_extra_non_ne_inventa(client, monkeypatch):
    c, m = client
    r, fwd = _post(c, monkeypatch, m)
    assert r.status_code == 200, r.text
    data = fwd.calls[0]["data"]
    for k in ("prompt", "hotwords", "vad_filter", "language"):
        assert k not in data


# ------------------------------------------------- forwarder: gating provider
def test_stt_extras_supported_provider():
    assert _stt_extras_supported({"provider": "speaches"})
    assert _stt_extras_supported({"api_base": "http://speaches:8000/v1"})
    assert not _stt_extras_supported(
        {"provider": "groq", "api_base": "https://api.groq.com/openai/v1"})
    assert not _stt_extras_supported({"provider": "openai",
                                      "api_base": "https://api.openai.com/v1"})


class _Resp:
    status_code = 200
    headers = {"content-type": "application/json"}
    text = ""

    def json(self):
        return {"text": "ok"}


class _Client:
    def __init__(self, sink):
        self.sink = sink

    async def post(self, url, **kw):
        self.sink.update(kw)
        return _Resp()


def _transcribe_seen(dep):
    fwd = Forwarder()
    seen: dict = {}
    fwd._client_for = lambda url, key="": _Client(seen)
    fields = {"language": "it", "prompt": "p", "hotwords": "h",
              "vad_filter": "true"}
    asyncio.run(fwd.transcribe(dep, fields, b"RIFF", "a.wav", "audio/wav"))
    return seen["data"]


def test_transcribe_inoltra_extras_a_speaches():
    data = _transcribe_seen({"unique": "u", "model": "m", "api_key": "k",
                             "api_base": "http://speaches:8000/v1",
                             "provider": "speaches"})
    assert data["hotwords"] == "h" and data["vad_filter"] == "true"
    assert data["prompt"] == "p"


def test_transcribe_rimuove_extras_su_groq():
    data = _transcribe_seen({"unique": "u", "model": "m", "api_key": "k",
                             "api_base": "https://api.groq.com/openai/v1",
                             "provider": "groq"})
    assert "hotwords" not in data and "vad_filter" not in data
    assert data["prompt"] == "p"        # gli altri campi passano comunque
