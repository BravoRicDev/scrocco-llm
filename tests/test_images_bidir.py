"""Adattamento bidirezionale immagini: ogni deployment image_gen/image_edit
deve poter essere servito SIA da una chat (modalities:["image"]) SIA dagli
endpoint nativi /v1/images/generations|/v1/images/edits, indipendentemente da
COME il provider espone il modello.

Copre:
  - la colonna CSV `image_via` (chat | images | both) e il suo effetto
    sull'ordine dei tentativi (nativo vs chat);
  - i segnali di direzione negli errori upstream
    (native_images_only_error / chat_only_image_error): il caso reale #56
    ("is not supported on /v1/images/..." NON era riconosciuto);
  - chat -> /images/*: il body chat viene tradotto in body OpenAI images e la
    risposta riconvertita in chat.completion con `message.images[]`;
  - /images/* -> chat: la macchina esistente, guidata dalla dichiarazione;
  - `mask` inoltrato al provider nativo;
  - riferimenti multipli in entrambe le direzioni.
"""
import base64

import pytest
from fastapi.testclient import TestClient

from app.capabilities import wants_image_output
from app.config import parse_image_via
from app.forwarder import (UpstreamError, chat_only_image_error,
                           chat_prompt_and_refs, dep_image_via,
                           image_chat_fallback_signature,
                           images_payload_from_chat, images_response_to_chat,
                           native_images_only_error)

PNG_1x1 = b"\x89PNG\r\n\x1a\n" + b"x" * 8
_B64 = base64.b64encode(b"ABC").decode()
_PNG_URI = "data:image/png;base64," + base64.b64encode(PNG_1x1).decode()


# ------------------------------------------------------------------ unit: csv
def test_parse_image_via_valori():
    assert parse_image_via("chat") == "chat"
    assert parse_image_via("IMAGES") == "images"
    assert parse_image_via("native") == "images"
    assert parse_image_via("chat_completions") == "chat"


def test_parse_image_via_default_e_refusi():
    # vuoto/ignoto -> "both" (comportamento storico: prova e poi adatta).
    assert parse_image_via("") == "both"
    assert parse_image_via(None) == "both"
    assert parse_image_via("magico") == "both"


def test_dep_image_via_default_su_dep_legacy():
    # un deployment senza la chiave (CSV vecchio) resta "both".
    assert dep_image_via({}) == "both"
    assert dep_image_via({"image_via": ""}) == "both"
    assert dep_image_via({"image_via": "chat"}) == "chat"


# ------------------------------------------------- unit: firma errori (#56)
_GEMINI_ERR = ("Model gemini-3.1-flash-image is not supported on "
               "/v1/images/generations or /v1/images/edits")
_GPT_ERR = ("model gpt-image-2.5 is only supported on /v1/images/generations "
            "and /v1/images/edits")


def test_firma_chat_only_non_tocca_more_strike():
    # PRIMA (bug #56) questa stringa NON era riconosciuta -> il deployment
    # veniva messo in cooldown con strike invece di essere servito via chat.
    assert image_chat_fallback_signature(400, _GEMINI_ERR) is True
    assert chat_only_image_error(_GEMINI_ERR) is True
    assert native_images_only_error(_GEMINI_ERR) is False


def test_firma_native_only():
    assert image_chat_fallback_signature(400, _GPT_ERR) is True
    assert native_images_only_error(_GPT_ERR) is True
    assert chat_only_image_error(_GPT_ERR) is False


def test_firma_direzioni_esclusive():
    # "only" e' il discriminante: le due firme non devono mai essere vere insieme.
    for txt in (_GEMINI_ERR, _GPT_ERR, "x" * 3, "", "rate limit"):
        assert not (native_images_only_error(txt) and chat_only_image_error(txt))


def test_firma_non_scambia_rifiuti_di_politica():
    # un rifiuto di contenuto non e' un problema di endpoint: niente adattamento
    blocked = "Your prompt was blocked by content policy"
    assert image_chat_fallback_signature(400, blocked) is False
    assert native_images_only_error(blocked) is False
    assert chat_only_image_error(blocked) is False


def test_firma_schema_400_restano_adattabili():
    assert image_chat_fallback_signature(
        400, 'Invalid JSON payload received. Unknown name "prompt"') is True


# --------------------------------------------------------- unit: chat -> images
def test_wants_image_output_forme():
    assert wants_image_output({"modalities": ["image"]}) is True
    assert wants_image_output({"modalities": ["image", "text"]}) is True
    assert wants_image_output({"output_modalities": ["image"]}) is True
    assert wants_image_output({"image_output": True}) is True
    assert wants_image_output({"modalities": ["text"]}) is False
    assert wants_image_output({"messages": [{"role": "user", "content": "hi"}]}) \
        is False


def test_chat_prompt_and_refs_da_content_misto():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "rendilo blu"},
        {"type": "image_url", "image_url": {"url": _PNG_URI}},
        {"type": "text", "text": "sempre"}]}]
    prompt, refs = chat_prompt_and_refs(msgs)
    assert prompt == "rendilo blu\nsempre"
    assert refs == [_PNG_URI]


def test_chat_prompt_usa_ultimo_turno_umano():
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "primo"},
            {"role": "assistant", "content": "risposta"},
            {"role": "user", "content": "secondo"}]
    assert chat_prompt_and_refs(msgs) == ("secondo", [])


def test_images_payload_from_chat_scarta_campi_chat():
    body = images_payload_from_chat({
        "model": "gpt-image-1", "modalities": ["image"], "stream": True,
        "messages": [{"role": "user", "content": "un gatto"}],
        "n": 2, "size": "1024x1024", "seed": 5, "max_tokens": 10})
    assert body["prompt"] == "un gatto"
    assert body["n"] == 2 and body["size"] == "1024x1024" and body["seed"] == 5
    # campi solo-chat: non hanno senso su /images/generations
    for k in ("model", "messages", "modalities", "stream", "max_tokens"):
        assert k not in body


def test_images_payload_con_refs_diveno_edit():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "nel tramonto"},
        {"type": "image_url", "image_url": {"url": _PNG_URI}}]}]
    body = images_payload_from_chat({"messages": msgs})
    assert body["prompt"] == "nel tramonto"
    assert body["image"] == _PNG_URI


def test_images_response_to_chat_forma_chat_completion():
    out = images_response_to_chat({"data": [{"b64_json": _B64}]}, "gpt-image-1")
    assert out["object"] == "chat.completion"
    assert out["choices"][0]["message"]["role"] == "assistant"
    assert out["choices"][0]["message"]["images"][0]["image_url"]["url"].\
        startswith("data:image/png;base64,")
    # la spec OpenAI images resta disponibile per i client che la leggono
    assert out["data"][0]["b64_json"] == _B64


def test_images_response_to_chat_senza_immagini():
    assert images_response_to_chat({"data": []}, "m") is None
    assert images_response_to_chat({}, "m") is None
    assert images_response_to_chat(None, "m") is None


# ------------------------------------------------------------------ e2e setup
_CSV_HEADER = ("commento,modello,provider,endpoint,data,context,max_input,"
               "priority,scrocco-llm-test,image_via,caps\n")
_UPSTREAM = "https://upstream.test/v1"


def _row(model, provider, image_via, caps):
    caps = f'"{caps}"' if "," in caps else caps
    return (f"seed,{model},{provider},{_UPSTREAM},free,8,8000,1,sk-key-1234,"
            f"{image_via},{caps}\n")


class _FakeForwarder:
    """Forwarder finto che registra le chiamate e risponde come il provider."""

    def __init__(self, native, chat):
        self.native_fn = native
        self.chat_fn = chat
        self.images_calls: list[str] = []
        self.chat_calls: list[str] = []
        self.image_payloads: list[dict] = []
        self.chat_payloads: list[dict] = []
        self.image_kwargs: list[dict] = []

    async def call_images(self, dep, payload, **kw):
        tag = dep.get("provider") or dep.get("unique")
        self.images_calls.append(tag)
        self.image_payloads.append(payload)
        self.image_kwargs.append(kw)
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


_NATIVE_OK = lambda dep, p: (200, {"data": [{"b64_json": _B64}]})
_CHAT_IMG_OK = lambda dep, p: (200, {"choices": [{"message": {"images": [
    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _B64}}]}}]})


def _make_client(monkeypatch, tmp_path, csv_text):
    csv = tmp_path / "k.csv"
    csv.write_text(csv_text)
    import app.main as m
    orig = (m.authn.master_key, m.config.csv_path,
            m.router.policy.cap_groups_enabled)
    m.authn.master_key = "test-master-bidir"
    m.LEDGER.flush()
    monkeypatch.setattr(m, "VAR_DIR", str(tmp_path))
    monkeypatch.setattr(m, "CSV_PATH", str(csv))
    monkeypatch.setattr(m.config, "csv_path", csv)
    m.config.reload()
    from app.ledger import Ledger
    monkeypatch.setattr(m, "LEDGER", Ledger(tmp_path))
    m.router.policy.cap_groups_enabled = True
    return TestClient(m.app), m, orig


def _teardown(m, orig):
    m.imagestore.clear()
    m.router.policy.cap_groups_enabled = orig[2]
    m.router._cooldown.clear()
    m.authn.master_key = orig[0]
    m.config.csv_path = orig[1]
    m.config.reload()


def _client(monkeypatch, tmp_path, model, provider, image_via, caps,
            native=_NATIVE_OK, chat=_CHAT_IMG_OK):
    c, m, orig = _make_client(
        monkeypatch, tmp_path,
        _CSV_HEADER + _row(model, provider, image_via, caps))
    fwd = _FakeForwarder(native, chat)
    monkeypatch.setattr(m, "forwarder", fwd)
    return c, m, fwd, orig


MK = {"Authorization": "Bearer test-master-bidir"}


@pytest.fixture()
def native_dep(monkeypatch, tmp_path):
    """Deployment image-NATIVE (gpt-image): esposto solo su /images/*."""
    c, m, fwd, orig = _client(monkeypatch, tmp_path, "gpt-image-1", "openai",
                              "images", "image_gen,image_edit")
    yield c, m, fwd
    _teardown(m, orig)


@pytest.fixture()
def chat_dep(monkeypatch, tmp_path):
    """Deployment CHAT-ONLY (gemini nano-banana): /images/* risponde 400."""
    c, m, fwd, orig = _client(
        monkeypatch, tmp_path, "gemini-3.1-flash-image", "antigravity",
        "chat", "image_gen,image_edit", native=lambda d, p: (400, _GEMINI_ERR))
    yield c, m, fwd
    _teardown(m, orig)


# ------------------------------------------- e2e: chat servita da image-native
def test_chat_modalities_image_serve_da_image_native(native_dep):
    """Il cuore del gap: una CHAT con modalities:["image"] resta uguale per
    il client, ma il provider image-native riceve /images/generations."""
    c, m, fwd = native_dep
    r = c.post("/v1/chat/completions", headers=MK, json={
        "model": "scrocco-llm-test", "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": "un gatto su un tetto"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    # risposta in forma CHAT
    assert body["object"] == "chat.completion"
    imgs = body["choices"][0]["message"]["images"]
    # localizzazione: b64 specchiato nello store + url del gateway
    assert imgs and imgs[0]["type"] == "image_url"
    iu = imgs[0]["image_url"]
    assert iu["url"].startswith("http://testserver/v1/images/files/")
    assert iu["b64_json"] == "QUJD"
    got = c.get(iu["url"])
    assert got.status_code == 200 and got.content == b"ABC"
    # il provider ha ricevuto una chiamata /images/generations, non una chat
    assert fwd.images_calls == ["openai"]
    assert fwd.chat_calls == []
    assert fwd.image_payloads[0]["prompt"] == "un gatto su un tetto"
    assert fwd.image_kwargs[0].get("endpoint") == "generations"


def test_chat_modalities_image_usa_edits_se_ci_sono_refs(native_dep):
    c, m, fwd = native_dep
    r = c.post("/v1/chat/completions", headers=MK, json={
        "model": "scrocco-llm-test", "modalities": ["image"],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "nel tramonto"},
            {"type": "image_url", "image_url": {"url": _PNG_URI}}]}]})
    assert r.status_code == 200, r.text
    assert fwd.image_kwargs[0].get("endpoint") == "edits"
    assert fwd.image_kwargs[0].get("refs") == [_PNG_URI]


def test_chat_senza_modalities_usa_il_motore_testo(native_dep):
    """Una chat di testo normale NON viene deviata sulla macchina immagini."""
    c, m, fwd = native_dep
    r = c.post("/v1/chat/completions", headers=MK, json={
        "model": "scrocco-llm-test",
        "messages": [{"role": "user", "content": "ciao"}]})
    # l'immagine non e' richiesta: il gateway non la genera da solo
    assert fwd.images_calls == []


# --------------------------------------------- e2e: /images servita da chat-only
def test_images_generations_usa_chat_se_dichiarato_chat(chat_dep):
    c, m, fwd = chat_dep
    r = c.post("/v1/images/generations", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "un gatto"})
    assert r.status_code == 200, r.text
    # dichiarando image_via=chat il gateway NON spreca una chiamata al nativo
    assert fwd.images_calls == []
    assert fwd.chat_calls == ["antigravity"]
    assert fwd.chat_payloads[0]["modalities"] == ["image"]
    assert r.json()["data"][0]["b64_json"] == _B64


def test_images_generations_fallback_chat_senza_dichiarazione(
        monkeypatch, tmp_path):
    """CSV che non dichiara nulla: nativa prima, poi chat sullo stesso dep."""
    c, m, fwd, orig = _client(
        monkeypatch, tmp_path, "gemini-3.1-flash-image", "antigravity",
        "both", "image_gen", native=lambda d, p: (400, _GEMINI_ERR))
    try:
        r = c.post("/v1/images/generations", headers=MK,
                   json={"model": "scrocco-llm-test", "prompt": "un gatto"})
        assert r.status_code == 200, r.text
        assert fwd.images_calls == ["antigravity"]     # provato il nativo
        assert fwd.chat_calls == ["antigravity"]      # poi adattato in chat
        assert r.json()["via"] == "chat"
    finally:
        _teardown(m, orig)


def test_images_edits_via_chat_su_modello_chat_only(chat_dep):
    c, m, fwd = chat_dep
    r = c.post("/v1/images/edits", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "nel tramonto",
                     "image": _PNG_URI})
    assert r.status_code == 200, r.text
    assert fwd.chat_calls == ["antigravity"]
    content = fwd.chat_payloads[0]["messages"][0]["content"]
    assert content[0]["type"] == "image_url"


# ----------------------------------------------------- e2e: mask al provider
def test_edits_mask_inviato_al_provider_nativo(native_dep):
    from app.forwarder import _multipart_image_edit
    payload = {"prompt": "p", "mask": _PNG_URI}
    files, data = _multipart_image_edit(payload, "gpt-image-1", [_PNG_URI])
    names = [f[0] for f in files]
    assert "image" in names and "mask" in names
    # `mask` non deve finire anche come campo di form
    assert "mask" not in data


def test_edits_mask_parsing_da_multipart(monkeypatch, tmp_path):
    c, m, fwd, orig = _client(monkeypatch, tmp_path, "gpt-image-1", "openai",
                              "images", "image_gen,image_edit")
    try:
        r = c.post("/v1/images/edits", headers=MK,
                   data={"model": "scrocco-llm-test", "prompt": "p"},
                   files=[("image", ("ref.png", PNG_1x1, "image/png")),
                          ("mask", ("m.png", PNG_1x1, "image/png"))])
        assert r.status_code == 200, r.text
        assert fwd.image_kwargs[0].get("endpoint") == "edits"
        assert fwd.image_payloads[0]["mask"].startswith("data:image/png;base64,")
    finally:
        _teardown(m, orig)
