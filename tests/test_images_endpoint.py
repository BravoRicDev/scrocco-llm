"""Regressione endpoint /v1/images/generations.

Contesto: `Forwarder.call_images` era stato cambiato (commit b493acf) per
postare il body OpenAI-immagini `{"prompt": ...}` su `/chat/completions`
invece che sull'endpoint NATIVO `/images/generations`. Google/Gemini risponde
400 'Invalid JSON payload received. Unknown name "prompt": Cannot find field.'
e il gateway rilanciava il 400 al client, senza MAI usare il percorso di
fallback via chat (messages + modalities:["image"]) che pure esisteva.

Qui si blinda:
  - l'URL nativo di call_images;
  - la conversione image -> chat (image_chat_payload);
  - la firma che decide il fallback (image_chat_fallback_signature);
  - l'estrazione delle immagini dalla risposta chat (extract_chat_images);
  - il percorso end-to-end: nativo 404/400 -> chat 200 -> 200 al client;
  - la ROTAZIONE su errori deployment-side (403 progetto negato/402 crediti/
    401 chiave rifiutata): la catena raggiunge il gruppo -image_gen-fallback
    (chiavi a pagamento).
"""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import imagestore
from app.forwarder import (Forwarder, UpstreamError, extract_chat_images,
                           image_chat_fallback_signature, image_chat_payload)
import app.state as gw_state

GEMINI_400 = ('{"error":{"code":400,"message":"Invalid JSON payload '
              'received. Unknown name \\"prompt\\": Cannot find field.",'
              '"status":"INVALID_ARGUMENT"}}')
GOOGLE_403 = ('{"code":403,"message":"Your project has been denied access. '
              'Please contact support.","status":"PERMISSION_DENIED"}')
# 401 realmente osservato (Bynara/TokenHarbor): envelope OpenAI.
UPSTREAM_401 = ('{"error":{"type":"unauthorized","message":'
                '"A valid API key is required.","request_id":"x"}}')

_DEP = {
    "unique": "g__models-gemini-2.5-flash-image__0",
    "group": "scrocco-llm-test-image_gen",
    "model": "models/gemini-2.5-flash-image",
    "api_base": "https://generativelanguage.googleapis.com/v1beta/openai",
    "api_key": "sk-test-key",
    "provider": "google",
}


# ---------------------------------------------------------------- unit: URL
def test_call_images_usa_endpoint_nativo():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"created": 1,
                                         "data": [{"url": "https://img/1.png"}]})

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        return await fwd.call_images(_DEP, {"model": "x", "prompt": "gatto"})

    out = asyncio.run(_run())
    assert seen["url"].endswith("/images/generations")
    assert "/chat/completions" not in seen["url"]
    assert seen["body"]["model"] == "models/gemini-2.5-flash-image"
    assert seen["body"]["prompt"] == "gatto"
    assert out["data"][0]["url"] == "https://img/1.png"


def test_call_images_propaga_404_come_negativo():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        with pytest.raises(UpstreamError) as ei:
            await fwd.call_images(_DEP, {"model": "x", "prompt": "gatto"})
        return ei.value

    err = asyncio.run(_run())
    assert err.status == -404


# ------------------------------------------------------ unit: conversione chat
def test_image_chat_payload_converte_prompt_in_messaggi():
    body = image_chat_payload(
        {"model": "x", "prompt": "un gatto", "size": "1024x1024", "n": 3,
         "quality": "hd", "response_format": "b64_json", "seed": 7},
        "models/gemini-2.5-flash-image")
    assert body["model"] == "models/gemini-2.5-flash-image"
    assert body["messages"] == [{"role": "user", "content": "un gatto"}]
    assert body["modalities"] == ["image"]
    assert body["n"] == 3
    assert body["seed"] == 7
    for dropped in ("prompt", "size", "quality", "response_format"):
        assert dropped not in body


def test_image_chat_payload_prompt_vuoto():
    body = image_chat_payload({"prompt": None}, "m")
    assert body["messages"][0]["content"] == ""


# ------------------------------------------------------------- unit: firma
def test_signature_google_unknown_prompt():
    assert image_chat_fallback_signature(-400, GEMINI_400) is True


def test_signature_endpoint_assente():
    assert image_chat_fallback_signature(-404, "") is True
    assert image_chat_fallback_signature(-405, "") is True
    assert image_chat_fallback_signature(-415, "") is True


def test_signature_non_triggera_su_altri_status():
    assert image_chat_fallback_signature(429, "rate limited") is False
    assert image_chat_fallback_signature(500, "boom") is False
    # 403 permessi/crediti: non e' un problema di endpoint -> niente chat
    assert image_chat_fallback_signature(-403, GOOGLE_403) is False


def test_signature_esclude_rifiuti_di_contenuto():
    assert image_chat_fallback_signature(
        -400, "Your request was blocked by our safety filters") is False
    assert image_chat_fallback_signature(
        -400, "This prompt violates the content policy") is False


# ------------------------------------------------------------ unit: estrazione
def test_extract_chat_images_gemini_shim():
    data = {"choices": [{"message": {"images": [
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64,AAA"}}]}}]}
    assert extract_chat_images(data) == [{"url": "data:image/png;base64,AAA"}]


def test_extract_chat_images_content_multimodale():
    data = {"choices": [{"message": {"content": [
        {"type": "text", "text": "ecco"},
        {"type": "image_url", "image_url": {"url": "https://img/x.png"}}]}}]}
    assert extract_chat_images(data) == [{"url": "https://img/x.png"}]


def test_extract_chat_images_b64():
    data = {"choices": [{"message": {"images": [{"b64_json": "QUJD"}]}}]}
    assert extract_chat_images(data) == [{"b64_json": "QUJD"}]


def test_extract_chat_images_nessuna():
    assert extract_chat_images({"choices": [{"message": {"content": "no"}}]}) == []


# ------------------------------------------------------------- end-to-end
class _FakeForwarder:
    """Sostituto di Forwarder: registra le chiamate e simula l'upstream.

    `native`/`chat` sono callable (dep, payload) -> (status, body)."""

    def __init__(self, native, chat):
        self.native_fn = native
        self.chat_fn = chat
        self.images_calls: list[str] = []
        self.chat_calls: list[str] = []
        self.chat_payloads: list[dict] = []

    async def call_images(self, dep, payload, **kw):
        tag = dep.get("provider") or dep.get("unique")
        self.images_calls.append(tag)
        status, body = self.native_fn(dep, payload)
        if status >= 400:
            raise UpstreamError(-status if status != 429 else status,
                                str(body)[:500])
        return body

    async def call(self, dep, payload, **kw):
        tag = dep.get("provider") or dep.get("unique")
        self.chat_calls.append(tag)
        self.chat_payloads.append(payload)
        status, body = self.chat_fn(dep, payload)
        if status >= 400:
            raise UpstreamError(-status if status != 429 else status,
                                str(body)[:500])
        return body


_CSV_HEADER = ("commento,modello,provider,endpoint,data,context,max_input,"
               "priority,scrocco-llm-test,caps\n")
_GOOGLE = "https://generativelanguage.googleapis.com/v1beta/openai"
_FALLBACK_ROW = ("orfall,google/gemini-2.5-flash-image,openrouter,"
                 "https://openrouter.ai/api/v1,fallback,128,0,0,"
                 "sk-or-test-key,image_gen\n")


def _make_client(monkeypatch, tmp_path, csv_text):
    csv = tmp_path / "k.csv"
    csv.write_text(csv_text)
    import app.main as m
    orig = (gw_state.authn.master_key, gw_state.config.csv_path,
            gw_state.router.policy.cap_groups_enabled)
    gw_state.authn.master_key = "test-master-img"
    gw_state.LEDGER.flush()
    monkeypatch.setattr(gw_state, "VAR_DIR", str(tmp_path))
    monkeypatch.setattr(gw_state, "CSV_PATH", str(csv))
    monkeypatch.setattr(gw_state.config, "csv_path", csv)
    gw_state.config.reload()
    from app.ledger import Ledger
    monkeypatch.setattr(gw_state, "LEDGER", Ledger(tmp_path))
    gw_state.router.policy.cap_groups_enabled = True
    return TestClient(m.app), m, orig


def _teardown(m, orig):
    imagestore.clear()
    gw_state.router.policy.cap_groups_enabled = orig[2]
    gw_state.router._cooldown.clear()
    gw_state.authn.master_key = orig[0]
    gw_state.config.csv_path = orig[1]
    gw_state.config.reload()


@pytest.fixture()
def client(monkeypatch, tmp_path):
    csv_text = (
        _CSV_HEADER
        + f"seed,models/gemini-2.5-flash-image,google,{_GOOGLE},"
          "free,8,8000,1,sk-test-key,image_gen\n"
        # gruppo -image_gen-fallback (categoria 'fallback'): chiave a pagamento
        + _FALLBACK_ROW
    )
    c, m, orig = _make_client(monkeypatch, tmp_path, csv_text)
    yield c, m
    _teardown(m, orig)


MK = {"Authorization": "Bearer test-master-img"}


def _fun(monkeypatch, m, native, chat) -> _FakeForwarder:
    fwd = _FakeForwarder(native, chat)
    monkeypatch.setattr(gw_state, "forwarder", fwd)
    return fwd


_IMG_ONLY = lambda dep, p: (200, {"choices": [{"message": {"images": [
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]}}]})
_NATIVE_URL = lambda dep, p: (200, {"created": 1,
                                    "data": [{"url": "https://img/native.png"}]})


def test_e2e_nativo_404_chat_fallback_ok(client, monkeypatch):
    c, m = client
    fwd = _fun(monkeypatch, m, native=lambda d, p: (404, "not found"),
               chat=_IMG_ONLY)
    r = c.post("/v1/images/generations", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "un gatto"})
    assert r.status_code == 200, r.text
    body = r.json()
    item = body["data"][0]
    assert item["url"].startswith("http://testserver/v1/images/files/")
    assert item["b64_json"] == "QUJD"
    got = c.get(item["url"])
    assert got.status_code == 200 and got.content == b"ABC"
    assert body["via"] == "chat"
    assert fwd.images_calls == ["google"]
    assert fwd.chat_calls == ["google"]
    assert fwd.chat_payloads[0]["messages"] == [
        {"role": "user", "content": "un gatto"}]
    assert fwd.chat_payloads[0]["modalities"] == ["image"]


def test_e2e_google_400_prompt_trigghera_chat(client, monkeypatch):
    c, m = client
    fwd = _fun(monkeypatch, m, native=lambda d, p: (400, GEMINI_400),
               chat=_IMG_ONLY)
    r = c.post("/v1/images/generations", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "un cane"})
    assert r.status_code == 200, r.text
    assert fwd.images_calls == ["google"]
    assert fwd.chat_calls == ["google"]


def test_e2e_rifiuto_contenuto_non_ritenta(client, monkeypatch):
    c, m = client
    fwd = _fun(monkeypatch, m,
               native=lambda d, p: (400, "Your request was blocked by our safety filters"),
               chat=_IMG_ONLY)
    r = c.post("/v1/images/generations", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "x"})
    assert r.status_code == 400
    assert fwd.images_calls == ["google"]
    assert fwd.chat_calls == []


def test_e2e_403_progetto_negato_ruota_senza_chat(client, monkeypatch):
    """403 'project denied' e' deployment-side: ruota (anche verso il fallback)
    SENZA sprecare una chiamata chat."""
    c, m = client
    fwd = _fun(monkeypatch, m, native=lambda d, p: (403, GOOGLE_403),
               chat=_IMG_ONLY)
    r = c.post("/v1/images/generations", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "x"})
    assert r.status_code == 403
    assert fwd.chat_calls == []
    # primario google + fallback openrouter (entrambi 403) -> 2 tentativi
    assert fwd.images_calls == ["google", "openrouter"]


def test_e2e_401_chiave_rifiutata_ruota(client, monkeypatch):
    """401 upstream = la NOSTRA chiave e' rifiutata -> SEMPRE deployment-side
    (il client si e' gia' autenticato verso il gateway): si RUOTA come il 403.
    Prima il path media non ruotava e il client riceveva 401 dopo UNA sola
    chiamata; ora la catena prova 2 deployment (primario + fallback)."""
    c, m = client
    fwd = _fun(monkeypatch, m, native=lambda d, p: (401, UPSTREAM_401),
               chat=_IMG_ONLY)
    r = c.post("/v1/images/generations", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "x"})
    assert r.status_code == 401
    assert fwd.chat_calls == []          # niente spreco di una chiamata chat
    # primario google + fallback openrouter (entrambi 401) -> 2 tentativi
    assert fwd.images_calls == ["google", "openrouter"]


def test_e2e_chain_raggiunge_fallback_openrouter(client, monkeypatch):
    """Google free morto (403) -> la catena arriva al gruppo -image_gen-fallback
    (OpenRouter a pagamento) che consegna via chat."""
    c, m = client

    def native(dep, p):
        if dep.get("provider") == "openrouter":
            return (404, "no images endpoint")
        return (403, GOOGLE_403)

    fwd = _fun(monkeypatch, m, native=native, chat=_IMG_ONLY)
    r = c.post("/v1/images/generations", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "una mela"})
    assert r.status_code == 200, r.text
    assert fwd.images_calls == ["google", "openrouter"]
    assert fwd.chat_calls == ["openrouter"]
    assert r.json()["data"][0]["url"].startswith(
        "http://testserver/v1/images/files/")
    assert r.json()["data"][0]["b64_json"] == "QUJD"


def test_e2e_nativo_ok_passthrough(client, monkeypatch):
    c, m = client
    monkeypatch.setattr(gw_state.router.policy, "images_mirror_remote",
                        False)   # no rete: url provider intatto
    fwd = _fun(monkeypatch, m, native=_NATIVE_URL, chat=_IMG_ONLY)
    r = c.post("/v1/images/generations", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "x"})
    assert r.status_code == 200
    assert r.json()["data"][0]["url"] == "https://img/native.png"
    assert fwd.images_calls == ["google"]
    assert fwd.chat_calls == []


def test_e2e_catena_non_troncata_dai_marker_chat(monkeypatch, tmp_path):
    """Con molti deployment primari che falliscono (ognuno tentando la chat),
    i marker `::chat` NON devono consumare il budget tentativi: la catena deve
    arrivare fino all'ULTIMO deployment (il fallback a pagamento)."""
    rows = _CSV_HEADER
    for i in range(20):
        rows += (f"g{i},models/gemini-2.5-flash-image,google,{_GOOGLE},"
                 f"free,8,8000,{i + 1},sk-test-key-{i},image_gen\n")
    rows += _FALLBACK_ROW
    c, m, orig = _make_client(monkeypatch, tmp_path, rows)
    try:
        def native(dep, p):
            return (404, "not found")

        def chat(dep, p):
            # i google falliscono anche via chat; solo il fallback consegna
            if dep.get("provider") == "openrouter":
                return _IMG_ONLY(dep, p)
            return (429, "rate limited")

        fwd = _fun(monkeypatch, m, native=native, chat=chat)
        r = c.post("/v1/images/generations", headers=MK,
                   json={"model": "scrocco-llm-test", "prompt": "x"})
        assert r.status_code == 200, r.text
        assert fwd.images_calls[-1] == "openrouter"
        assert fwd.images_calls.count("openrouter") == 1
        assert len(fwd.images_calls) == 21
    finally:
        _teardown(m, orig)


# 401 anche sul path image-refs, che passa da `_images_chat_loop`
# (chat multimodale: il 401 arriva da forwarder.call, NON da
# call_with_fallback -> era l'unico dei due loop media senza la rotazione).
def test_e2e_401_chiave_rifiutata_ruota_su_image_refs(monkeypatch, tmp_path):
    """Stessa rotazione sul path image-refs, che passa da `_images_chat_loop`
    (chat multimodale: il 401 arriva da forwarder.call, NON da
    call_with_fallback -> era l'unico dei due loop media senza la rotazione)."""
    rows = ("commento,modello,provider,endpoint,data,context,max_input,"
            "priority,scrocco-llm-test,caps,alias\n"
            "seed,models/gemini-3.1-flash-image,antigravity,https://p.test/v1,"
            "free,128,0,5,sk-test-key,\"image_gen,image_edit\","
            "gemini31-image\n"
            "orfall,models/gemini-3.1-flash-image,openrouter,"
            "https://o.test/v1,fallback,128,0,0,sk-or-test-key,"
            "\"image_gen,image_edit\",gemini31-image\n")
    c, m, orig = _make_client(monkeypatch, tmp_path, rows)
    try:
        # nativo 404: nessun endpoint images (deployment chat-only) -> la
        # richiesta con reference va per forza via _images_chat_loop.
        fwd = _fun(monkeypatch, m, native=lambda d, p: (404, "no endpoint"),
                   chat=lambda d, p: (401, UPSTREAM_401))
        r = c.post("/v1/images/generations", headers=MK,
                   json={"model": "scrocco-llm-test", "prompt": "x",
                         "image": ["data:image/png;base64,QUJD"]})
        assert r.status_code == 401
        assert fwd.chat_calls == ["antigravity", "openrouter"]
    finally:
        _teardown(m, orig)


# 401 anche sul path TTS: `call_speech` non ruota da solo (a differenza di
# call_with_fallback), serve la classificazione deployment_side del loop.
def test_e2e_401_chiave_rifiutata_ruota_su_tts(monkeypatch, tmp_path):
    rows = (
        _CSV_HEADER
        + "tts1,openai/tts-1,groq,https://api.groq.com/openai/v1,"
          "free,0,0,1,sk-tts-key,tts\n"
        + "tts2,openai/tts-1,openrouter,https://openrouter.ai/api/v1,"
          "fallback,0,0,0,sk-or-tts-key,tts\n"
    )
    c, m, orig = _make_client(monkeypatch, tmp_path, rows)
    try:
        calls: list[str] = []

        class _F:
            async def call_speech(self, dep, payload, **kw):
                calls.append(dep.get("provider") or dep["unique"])
                raise UpstreamError(-401, UPSTREAM_401)

        monkeypatch.setattr(gw_state, "forwarder", _F())
        # chiave CLIENTE sk-<profile>: con la master la rotazione dei path
        # audio non parte (profile assente -> nessun fallback_next).
        r = c.post("/v1/audio/speech", headers={"Authorization": "Bearer sk-test"},
                   json={"model": "scrocco-llm-test", "input": "ciao",
                         "voice": "alloy"})
        assert r.status_code == 401
        assert calls == ["groq", "openrouter"]        # la catena prova 2 dep
    finally:
        _teardown(m, orig)


# 401 anche sul path STT (`forwarder.transcribe` non ruota da solo): stessa
# condizione di codice dei path media, bloccata qui perche' la riga e' stata
# modificata (STT e' servito dallo stesso template dei path audio).
def test_e2e_401_chiave_rifiutata_ruota_su_stt(monkeypatch, tmp_path):
    rows = (
        _CSV_HEADER
        + "stt1,openai/whisper-large-v3,groq,https://api.groq.com/openai/v1,"
          "free,0,0,1,sk-stt-key,stt\n"
        + "stt2,openai/whisper-large-v3,openrouter,https://openrouter.ai/api/v1,"
          "fallback,0,0,0,sk-or-stt-key,stt\n"
    )
    c, m, orig = _make_client(monkeypatch, tmp_path, rows)
    try:
        calls: list[str] = []

        class _F:
            async def transcribe(self, dep, *a, **kw):
                calls.append(dep.get("provider") or dep["unique"])
                raise UpstreamError(-401, UPSTREAM_401)

        monkeypatch.setattr(gw_state, "forwarder", _F())
        r = c.post("/v1/audio/transcriptions",
                   headers={"Authorization": "Bearer sk-test"},
                   files={"file": ("a.wav", b"RIFF0000WAVE", "audio/wav")},
                   data={"model": "scrocco-llm-test"})
        assert r.status_code == 401
        assert calls == ["groq", "openrouter"]
    finally:
        _teardown(m, orig)


# ----------------------------------------------- fix roadmap backend (Fase 0)
# Finding A-E della web/docs/BACKEND-BUG-ROADMAP-OPERATIONAL.md, tutti su
# app/audio_api.py (i tre path media). Righe al checkout bf02a8c: guardia D
# ~L122, chiamate B ~L135/311/534, guardie C ~L149 (TTS) e ~L353 (STT),
# predicati E ~L208 (TTS) e ~L620 (STT).

_TTS_ROWS = ("tts1,openai/tts-1,groq,https://api.groq.com/openai/v1,"
             "free,0,0,1,sk-tts-key,tts\n"
             "tts2,openai/tts-1,openrouter,https://openrouter.ai/api/v1,"
             "fallback,0,0,0,sk-or-tts-key,tts\n")
_STT_ROWS = ("stt1,openai/whisper-large-v3,groq,https://api.groq.com/openai/v1,"
             "free,0,0,1,sk-stt-key,stt\n"
             "stt2,openai/whisper-large-v3,openrouter,https://openrouter.ai/api/v1,"
             "fallback,0,0,0,sk-or-stt-key,stt\n")
_TTS_BODY = {"model": "scrocco-llm-test", "input": "ciao", "voice": "alloy"}
_SYSTEMONE_BODY = {"model": "scrocco-llm-test", "state": "x",
                   "questions": {"q": {"type": "noul", "instructions": "si?"}}}
_STT_FILE = {"file": ("a.wav", b"RIFF0000WAVE", "audio/wav")}
_STT_DATA = {"model": "scrocco-llm-test"}
# Chiave di PROFILO: con la master il profilo e' assente e i path audio non
# ruotano (fallback_next richiede `profile`).
_SK_TEST = {"Authorization": "Bearer sk-test"}
# 403 di CREDENZIALE upstream: nessun marker di rifiuto di modalita', quindi
# non passa da `media_reject_signature`.
_UPSTREAM_403_DENIED = ('{"error":{"code":403,"message":"Forbidden: generic '
                        'provider denial"}}')


class _FwdVietato:
    """Forwarder che registra e fa fallire OGNI metodo: serve ai path che non
    devono nemmeno arrivarci (400 di schema, 401, 503 difensivo)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        async def _rec(*a, **kw):
            self.calls.append(name)
            raise AssertionError(f"forwarder.{name} non doveva essere chiamato")

        return _rec


def _fwd_vietato(monkeypatch) -> _FwdVietato:
    f = _FwdVietato()
    monkeypatch.setattr(gw_state, "forwarder", f)
    return f


def _cooldown_pulito() -> None:
    gw_state.router._cooldown.clear()


def test_finding_d_body_json_non_oggetto_400(monkeypatch, tmp_path):
    """Finding D (roadmap §7): un JSON sintatticamente valido ma non-oggetto
    (`null`, `[]`, 42, `"x"`) faceva esplodere `payload.get` -> 500.
    E' un body fuori schema: 400 invalid_request_error, mai 500."""
    c, m, orig = _make_client(monkeypatch, tmp_path, _CSV_HEADER + _TTS_ROWS)
    f = _fwd_vietato(monkeypatch)
    try:
        for raw in (b"null", b"[]", b"42", b'"x"'):
            r = c.post("/v1/audio/speech",
                       headers={"Content-Type": "application/json"},
                       content=raw)
            assert r.status_code == 400, (raw, r.status_code, r.text)
            err = r.json()["error"]
            assert err["type"] == "invalid_request_error", (raw, err)
            assert err["message"] == "il body deve essere un oggetto JSON", (raw, err)
        # Contro-prova: body oggetto valido ma senza `input` -> 400 preesistente
        r = c.post("/v1/audio/speech",
                   json={"model": "scrocco-llm-test", "voice": "alloy"})
        assert r.status_code == 400, r.text
        assert r.json()["error"]["message"] == "'input' è obbligatorio", r.text
        assert f.calls == [], f.calls
    finally:
        _teardown(m, orig)


def test_finding_b_401_senza_dettaglio_messaggio_non_vuoto(monkeypatch, tmp_path):
    """Finding B (roadmap §5): `AuthResult.error` e' `str | None` mentre
    `_unauthorized(detail: str)`. Con `error=None` il body 401 aveva
    `message: null`. Contratto: 401 auth_error con messaggio leggibile su
    tutti e tre i path audio, senza toccare il forwarder."""
    from app.auth import AuthResult

    c, m, orig = _make_client(monkeypatch, tmp_path, _CSV_HEADER + _TTS_ROWS)
    f = _fwd_vietato(monkeypatch)
    try:
        monkeypatch.setattr(
            gw_state.authn, "authenticate",
            lambda _h: AuthResult(ok=False, profile=None, mode=None, error=None))
        got = (
            ("/v1/audio/speech",
             c.post("/v1/audio/speech", headers=MK, json=_TTS_BODY)),
            ("/v1/systemone",
             c.post("/v1/systemone", headers=MK, json=_SYSTEMONE_BODY)),
            ("/v1/audio/transcriptions",
             c.post("/v1/audio/transcriptions", headers=MK,
                    files=_STT_FILE, data=_STT_DATA)),
        )
        for path, r in got:
            assert r.status_code == 401, (path, r.status_code, r.text)
            err = r.json()["error"]
            assert err["type"] == "auth_error", (path, err)
            assert err["message"] == "Authentication failed", (path, err)
        assert f.calls == [], f.calls
    finally:
        _teardown(m, orig)


def test_finding_b_401_dettaglio_presente_preservato(monkeypatch, tmp_path):
    """Contro-prova del finding B: quando il dettaglio C'E' non si sostituisce
    (il fallback copre solo il caso mancante)."""
    from app.auth import AuthResult

    c, m, orig = _make_client(monkeypatch, tmp_path, _CSV_HEADER + _TTS_ROWS)
    _fwd_vietato(monkeypatch)
    try:
        monkeypatch.setattr(
            gw_state.authn, "authenticate",
            lambda _h: AuthResult(ok=False, profile=None, mode=None,
                                  error="chiave scaduta"))
        r = c.post("/v1/audio/speech", headers=MK, json=_TTS_BODY)
        assert r.status_code == 401
        err = r.json()["error"]
        assert err["type"] == "auth_error"
        assert err["message"] == "chiave scaduta"
    finally:
        _teardown(m, orig)


def test_finding_c_routing_fallito_normale_invariato(monkeypatch, tmp_path):
    """Finding C (roadmap §6): quando `_audio_route` produce un errore proprio,
    TTS e STT consegnano QUELLO (status e body invariati), non un 503."""
    import app.audio_api as audio_api
    from fastapi.responses import JSONResponse

    c, m, orig = _make_client(monkeypatch, tmp_path, _CSV_HEADER + _TTS_ROWS)
    f = _fwd_vietato(monkeypatch)
    corpo = {"error": {"message": "modello sconosciuto",
                       "type": "invalid_request_error"}}

    def _route_404(*_a, **_kw):
        return (None, None, JSONResponse(status_code=404, content=corpo))

    try:
        monkeypatch.setattr(audio_api, "_audio_route", _route_404)
        for r in (c.post("/v1/audio/speech", headers=MK, json=_TTS_BODY),
                  c.post("/v1/audio/transcriptions", headers=MK,
                         files=_STT_FILE, data=_STT_DATA)):
            assert r.status_code == 404, r.text
            assert r.json() == corpo, r.text
        assert f.calls == [], f.calls
    finally:
        _teardown(m, orig)


def test_finding_c_audio_route_dep_none_503(monkeypatch, tmp_path):
    """Finding C (roadmap §6): se `_audio_route` torna `(None, profile, None)`
    l'endpoint risponde 503 server_error invece di esplodere con un subscript
    su None (500), e non prova il forwarder."""
    import app.audio_api as audio_api

    c, m, orig = _make_client(monkeypatch, tmp_path, _CSV_HEADER + _TTS_ROWS)
    f = _fwd_vietato(monkeypatch)
    try:
        monkeypatch.setattr(audio_api, "_audio_route",
                            lambda *a, **kw: (None, None, None))
        got = (
            ("/v1/audio/speech",
             c.post("/v1/audio/speech", headers=MK, json=_TTS_BODY), "tts"),
            ("/v1/audio/transcriptions",
             c.post("/v1/audio/transcriptions", headers=MK,
                    files=_STT_FILE, data=_STT_DATA), "stt"),
        )
        for path, r, cap in got:
            assert r.status_code == 503, (path, r.status_code, r.text)
            err = r.json()["error"]
            assert err["type"] == "server_error", (path, err)
            assert err["message"] == (
                f"nessun deployment disponibile per ['{cap}']"), (path, err)
        assert f.calls == [], f.calls
    finally:
        _teardown(m, orig)


def test_finding_e_403_upstream_ruota_su_tts(monkeypatch, tmp_path):
    """Finding E (roadmap §8): un 403 upstream non e' azionabile dal client
    (contratto di tests/test_permission_denied.py): si marca fallito e si
    RUOTA. Prima il 403 usciva subito col primo deployment."""
    c, m, orig = _make_client(monkeypatch, tmp_path, _CSV_HEADER + _TTS_ROWS)
    _cooldown_pulito()
    provati: list[str] = []
    uniques: list[str] = []

    class _F:
        async def call_speech(self, dep, payload, **kw):
            provati.append(dep["provider"])
            uniques.append(dep["unique"])
            if len(provati) == 1:
                raise UpstreamError(-403, _UPSTREAM_403_DENIED)
            return b"audio", "audio/mpeg"

    monkeypatch.setattr(gw_state, "forwarder", _F())
    try:
        r = c.post("/v1/audio/speech", headers=_SK_TEST, json=_TTS_BODY)
        assert r.status_code == 200, r.text
        assert provati == ["groq", "openrouter"], provati
        assert r.content == b"audio", r.content
        assert r.headers["content-type"].startswith("audio/mpeg")
        assert gw_state.router._cooldown.get(uniques[0], 0) > 0, "403 senza cooldown"
    finally:
        _cooldown_pulito()
        _teardown(m, orig)


def test_finding_e_403_upstream_ruota_su_stt(monkeypatch, tmp_path):
    """Stessa rotazione sul path STT (predicato duplicato)."""
    c, m, orig = _make_client(monkeypatch, tmp_path, _CSV_HEADER + _STT_ROWS)
    _cooldown_pulito()
    provati: list[str] = []
    uniques: list[str] = []

    class _F:
        async def transcribe(self, dep, *a, **kw):
            provati.append(dep["provider"])
            uniques.append(dep["unique"])
            if len(provati) == 1:
                raise UpstreamError(-403, _UPSTREAM_403_DENIED)
            return {"text": "ok", "nx_deployment": dep["unique"]}

    monkeypatch.setattr(gw_state, "forwarder", _F())
    try:
        r = c.post("/v1/audio/transcriptions", headers=_SK_TEST,
                   files=_STT_FILE, data=_STT_DATA)
        assert r.status_code == 200, r.text
        assert r.json()["text"] == "ok", r.text
        assert provati == ["groq", "openrouter"], provati
        assert gw_state.router._cooldown.get(uniques[0], 0) > 0, "403 senza cooldown"
    finally:
        _cooldown_pulito()
        _teardown(m, orig)


def test_finding_e_403_esaurimento_consegna_403(monkeypatch, tmp_path):
    """Finding E, coda della catena: la route audio consegna l'ULTIMO status
    upstream (403), non un 503: e' il contratto fissato dai test e2e sui 401
    e da test_e2e_403_progetto_negato_ruota_senza_chat (roadmap §8 punto 3)."""
    c, m, orig = _make_client(monkeypatch, tmp_path, _CSV_HEADER + _TTS_ROWS)
    _cooldown_pulito()
    provati: list[str] = []

    class _F:
        async def call_speech(self, dep, payload, **kw):
            provati.append(dep["provider"])
            raise UpstreamError(-403, _UPSTREAM_403_DENIED)

    monkeypatch.setattr(gw_state, "forwarder", _F())
    try:
        r = c.post("/v1/audio/speech", headers=_SK_TEST, json=_TTS_BODY)
        assert provati == ["groq", "openrouter"], provati
        assert r.status_code == 403, (r.status_code, r.text)
    finally:
        _cooldown_pulito()
        _teardown(m, orig)


def test_finding_e_400_senza_firma_non_ruota(monkeypatch, tmp_path):
    """Contro-prova del finding E: un -400 SENZA le due firme body-specifiche
    resta un errore del CLIENT (nessuna rotazione, nessun cooldown)."""
    c, m, orig = _make_client(monkeypatch, tmp_path, _CSV_HEADER + _TTS_ROWS)
    _cooldown_pulito()
    provati: list[str] = []
    uniques: list[str] = []

    class _F:
        async def call_speech(self, dep, payload, **kw):
            provati.append(dep["provider"])
            uniques.append(dep["unique"])
            raise UpstreamError(-400, '{"error":{"message":"invalid payload"}}')

    monkeypatch.setattr(gw_state, "forwarder", _F())
    try:
        r = c.post("/v1/audio/speech", headers=_SK_TEST, json=_TTS_BODY)
        assert r.status_code == 400, (r.status_code, r.text)
        assert provati == ["groq"], provati
        assert gw_state.router._cooldown.get(uniques[0], 0) == 0, "400 in cooldown"
    finally:
        _cooldown_pulito()
        _teardown(m, orig)
