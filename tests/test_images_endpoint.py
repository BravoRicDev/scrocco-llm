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
  - il percorso end-to-end: nativo 404/400 -> chat 200 -> 200 al client.
"""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.forwarder import (Forwarder, UpstreamError, extract_chat_images,
                           image_chat_fallback_signature, image_chat_payload)

GEMINI_400 = ('{"error":{"code":400,"message":"Invalid JSON payload '
              'received. Unknown name \\"prompt\\": Cannot find field.",'
              '"status":"INVALID_ARGUMENT"}}')

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
    """Sostituto di Forwarder: registra le chiamate e simula l'upstream."""

    def __init__(self, native, chat):
        self.native = native
        self.chat = chat
        self.calls: list[str] = []
        self.chat_payloads: list[dict] = []

    async def call_images(self, dep, payload, **kw):
        self.calls.append("images")
        status, body = self.native
        if status >= 400:
            raise UpstreamError(-status if status != 429 else status,
                                str(body)[:500])
        return body

    async def call(self, dep, payload, **kw):
        self.calls.append("chat")
        self.chat_payloads.append(payload)
        status, body = self.chat
        if status >= 400:
            raise UpstreamError(-status if status != 429 else status,
                                str(body)[:500])
        return body


@pytest.fixture()
def client(monkeypatch, tmp_path):
    csv = tmp_path / "k.csv"
    csv.write_text(
        "commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,caps\n"
        "seed,models/gemini-2.5-flash-image,google,"
        "https://generativelanguage.googleapis.com/v1beta/openai,"
        "free,8,8000,1,sk-test-key,image_gen\n"
    )
    import app.main as m
    orig_mk = m.authn.master_key
    m.authn.master_key = "test-master-img"
    orig_csv = m.config.csv_path
    m.LEDGER.flush()
    monkeypatch.setattr(m, "VAR_DIR", str(tmp_path))
    monkeypatch.setattr(m, "CSV_PATH", str(csv))
    monkeypatch.setattr(m.config, "csv_path", csv)
    m.config.reload()
    from app.ledger import Ledger
    led = Ledger(tmp_path)
    monkeypatch.setattr(m, "LEDGER", led)
    orig_cap_groups = m.router.policy.cap_groups_enabled
    m.router.policy.cap_groups_enabled = True
    yield TestClient(m.app), m
    m.router.policy.cap_groups_enabled = orig_cap_groups
    m.router._cooldown.clear()
    m.authn.master_key = orig_mk
    m.config.csv_path = orig_csv
    m.config.reload()


MK = {"Authorization": "Bearer test-master-img"}


def _fun(monkeypatch, m, native, chat) -> _FakeForwarder:
    fwd = _FakeForwarder(native, chat)
    monkeypatch.setattr(m, "forwarder", fwd)
    return fwd


def test_e2e_nativo_404_chat_fallback_ok(client, monkeypatch):
    c, m = client
    fwd = _fun(monkeypatch, m, native=(404, "not found"),
               chat=(200, {"choices": [{"message": {"images": [
                   {"type": "image_url",
                    "image_url": {"url": "data:image/png;base64,ZZZ"}}]}}]}))
    r = c.post("/v1/images/generations", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "un gatto"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"] == [{"url": "data:image/png;base64,ZZZ"}]
    assert body["via"] == "chat"
    assert fwd.calls == ["images", "chat"]
    assert fwd.chat_payloads[0]["messages"] == [
        {"role": "user", "content": "un gatto"}]
    assert fwd.chat_payloads[0]["modalities"] == ["image"]


def test_e2e_google_400_prompt_trigghera_chat(client, monkeypatch):
    c, m = client
    fwd = _fun(monkeypatch, m, native=(400, GEMINI_400),
               chat=(200, {"choices": [{"message": {"images": [
                   {"image_url": {"url": "https://img/ok.png"}}]}}]}))
    r = c.post("/v1/images/generations", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "un cane"})
    assert r.status_code == 200, r.text
    assert r.json()["data"] == [{"url": "https://img/ok.png"}]
    assert fwd.calls == ["images", "chat"]


def test_e2e_rifiuto_contenuto_non_ritenta(client, monkeypatch):
    c, m = client
    fwd = _fun(monkeypatch, m,
               native=(400, "Your request was blocked by our safety filters"),
               chat=(200, {"choices": [{"message": {"images": [
                   {"image_url": {"url": "https://img/never.png"}}]}}]}))
    r = c.post("/v1/images/generations", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "x"})
    assert r.status_code == 400
    assert fwd.calls == ["images"]


def test_e2e_nativo_ok_passthrough(client, monkeypatch):
    c, m = client
    fwd = _fun(monkeypatch, m,
               native=(200, {"created": 9,
                             "data": [{"url": "https://img/native.png"}]}),
               chat=(200, {"choices": []}))
    r = c.post("/v1/images/generations", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "x"})
    assert r.status_code == 200
    assert r.json()["data"][0]["url"] == "https://img/native.png"
    assert fwd.calls == ["images"]
