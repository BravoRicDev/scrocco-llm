"""Regressione image2image: /v1/images/edits + reference su /images/generations.

Contesto: l'endpoint nativo /images/generations non accetta immagini di
riferimento. Per l'image-to-image i modelli (Gemini/nano-banana) vanno serviti
via chat multimodale (`messages` + `modalities:["image"]`), con la reference
come parte `image_url` PRIMA del testo.

Qui si blinda:
  - la conversione reference -> parti chat (image_chat_payload con refs);
  - l'estrazione dei campi reference (image/images/reference_images);
  - il troncamento per i modelli single-ref (image_multi_ref);
  - i nuovi token caps image_edit/image_multi_ref;
  - l'endpoint /v1/images/edits (multipart e JSON);
  - il path chat diretto di /v1/images/generations con reference;
  - l'errore 400 senza reference o senza deployment image_edit.
"""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.capabilities import normalize_caps, refs_max_for
from app.config import parse_caps
from app.forwarder import (Forwarder, UpstreamError, extract_chat_images,
                           image_chat_payload, image_refs_from_payload,
                           truncate_refs)


# ------------------------------------------------------------------ unit: refs
def test_image_refs_from_payload_forme():
    assert image_refs_from_payload({"image": "u1"}) == ["u1"]
    assert image_refs_from_payload({"images": ["u1", "u2"]}) == ["u1", "u2"]
    assert image_refs_from_payload(
        {"reference_images": [{"url": "u1"},
                              {"image_url": {"url": "u2"}}]}) == ["u1", "u2"]
    assert image_refs_from_payload({"image": None, "images": []}) == []


def test_image_refs_from_payload_b64_json():
    assert image_refs_from_payload({"images": [{"b64_json": "QUJD"}]}) == \
        ["data:image/png;base64,QUJD"]


def test_truncate_refs():
    assert truncate_refs(["a", "b", "c"], 1) == ["a"]
    assert truncate_refs(["a", "b"], 5) == ["a", "b"]
    assert truncate_refs(["a", "b"], 0) == ["a"]


def test_refs_max_for():
    assert refs_max_for(frozenset({"image_gen"}), 16) == 1
    assert refs_max_for(frozenset({"image_gen", "image_multi_ref"}), 16) == 16
    assert refs_max_for(frozenset({"image_multi_ref"}), 3) == 3


# ------------------------------------------------------- unit: chat payload
def test_image_chat_payload_include_refs():
    body = image_chat_payload(
        {"model": "x", "prompt": "rendilo blu",
         "images": ["data:image/png;base64,AAA", "data:image/png;base64,BBB"],
         "size": "1024x1024", "seed": 7},
        "models/gemini-2.5-flash-image")
    assert body["modalities"] == ["image"]
    assert body["seed"] == 7
    assert "images" not in body and "size" not in body
    content = body["messages"][0]["content"]
    assert content[0] == {"type": "image_url",
                          "image_url": {"url": "data:image/png;base64,AAA"}}
    assert content[1] == {"type": "image_url",
                          "image_url": {"url": "data:image/png;base64,BBB"}}
    assert content[-1] == {"type": "text", "text": "rendilo blu"}


def test_image_chat_payload_senza_refs_resta_stringa():
    body = image_chat_payload({"prompt": "un gatto"}, "m")
    assert body["messages"] == [{"role": "user", "content": "un gatto"}]


# ------------------------------------------------------------------ unit: caps
def test_parse_caps_nuovi_token():
    got = parse_caps("image_gen, image_edit, image_multi_ref")
    assert got == frozenset({"image_gen", "image_edit", "image_multi_ref"})


def test_normalize_caps_nuovi_token():
    got = normalize_caps(["image_edit", "image_multi_ref"], "ctx")
    assert got == frozenset({"image_edit", "image_multi_ref"})


# ------------------------------------------------------------------ e2e setup
class _FakeForwarder:
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
_IMG_ONLY = lambda dep, p: (200, {"choices": [{"message": {"images": [
    {"type": "image_url",
     "image_url": {"url": "data:image/png;base64,ZZZ"}}]}}]})


def _make_client(monkeypatch, tmp_path, csv_text):
    csv = tmp_path / "k.csv"
    csv.write_text(csv_text)
    import app.main as m
    orig = (m.authn.master_key, m.config.csv_path,
            m.router.policy.cap_groups_enabled)
    m.authn.master_key = "test-master-img"
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
    m.router.policy.cap_groups_enabled = orig[2]
    m.router._cooldown.clear()
    m.authn.master_key = orig[0]
    m.config.csv_path = orig[1]
    m.config.reload()


def _row(caps: str, model: str = "models/gemini-2.5-flash-image",
         key: str = "sk-test-key") -> str:
    if "," in caps:
        caps = f'"{caps}"'
    return (f"seed,{model},google,{_GOOGLE},free,8,8000,1,{key},{caps}\n")


@pytest.fixture()
def client_single(monkeypatch, tmp_path):
    c, m, orig = _make_client(monkeypatch, tmp_path,
                              _CSV_HEADER + _row("image_gen,image_edit"))
    yield c, m
    _teardown(m, orig)


@pytest.fixture()
def client_multi(monkeypatch, tmp_path):
    c, m, orig = _make_client(
        monkeypatch, tmp_path,
        _CSV_HEADER + _row("image_gen,image_edit,image_multi_ref"))
    yield c, m
    _teardown(m, orig)


@pytest.fixture()
def client_no_edit(monkeypatch, tmp_path):
    c, m, orig = _make_client(monkeypatch, tmp_path,
                              _CSV_HEADER + _row("image_gen"))
    yield c, m
    _teardown(m, orig)


MK = {"Authorization": "Bearer test-master-img"}


def _fun(monkeypatch, m, native=lambda d, p: (404, "not found"), chat=_IMG_ONLY):
    fwd = _FakeForwarder(native, chat)
    monkeypatch.setattr(m, "forwarder", fwd)
    return fwd


# --------------------------------------------------------------- e2e: edits
def test_edits_multipart_single_ref(client_single, monkeypatch):
    c, m = client_single
    fwd = _fun(monkeypatch, m)
    r = c.post("/v1/images/edits", headers=MK,
               data={"model": "scrocco-llm-test", "prompt": "rendilo blu"},
               files={"image": ("ref.png", b"\x89PNG\x00ref", "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["via"] == "chat"
    assert r.json()["data"] == [{"url": "data:image/png;base64,ZZZ"}]
    assert fwd.chat_calls == ["google"]
    content = fwd.chat_payloads[0]["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[-1] == {"type": "text", "text": "rendilo blu"}


def test_edits_json_multi_ref(client_multi, monkeypatch):
    c, m = client_multi
    fwd = _fun(monkeypatch, m)
    r = c.post("/v1/images/edits", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "fondi",
                     "images": ["data:image/png;base64,AAA",
                                "data:image/png;base64,BBB"]})
    assert r.status_code == 200, r.text
    content = fwd.chat_payloads[0]["messages"][0]["content"]
    imgs = [p for p in content if p["type"] == "image_url"]
    assert len(imgs) == 2


def test_edits_single_ref_truncates_extra(client_single, monkeypatch):
    c, m = client_single
    fwd = _fun(monkeypatch, m)
    r = c.post("/v1/images/edits", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "fondi",
                     "images": ["data:image/png;base64,AAA",
                                "data:image/png;base64,BBB"]})
    assert r.status_code == 200, r.text
    content = fwd.chat_payloads[0]["messages"][0]["content"]
    imgs = [p for p in content if p["type"] == "image_url"]
    assert len(imgs) == 1
    assert imgs[0]["image_url"]["url"].endswith("AAA")


def test_edits_mask_ignorato(client_single, monkeypatch):
    c, m = client_single
    fwd = _fun(monkeypatch, m)
    r = c.post("/v1/images/edits", headers=MK,
               data={"model": "scrocco-llm-test", "prompt": "cambia sfondo"},
               files={"image": ("ref.png", b"\x89PNGref", "image/png"),
                      "mask": ("mask.png", b"\x89PNGmask", "image/png")})
    assert r.status_code == 200, r.text


def test_edits_senza_ref_400(client_single, monkeypatch):
    c, m = client_single
    _fun(monkeypatch, m)
    r = c.post("/v1/images/edits", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "x"})
    assert r.status_code == 400
    assert "image" in r.json()["error"]["message"]


def test_edits_senza_cap_image_edit_400(client_no_edit, monkeypatch):
    c, m = client_no_edit
    _fun(monkeypatch, m)
    r = c.post("/v1/images/edits", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "x",
                     "image": "data:image/png;base64,AAA"})
    assert r.status_code == 400
    assert "image_edit" in r.json()["error"]["message"]


# ------------------------------------------------- e2e: generations con refs
def test_generations_con_refs_usa_chat(client_single, monkeypatch):
    c, m = client_single
    fwd = _fun(monkeypatch, m)
    r = c.post("/v1/images/generations", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "rendilo blu",
                     "image": "data:image/png;base64,AAA"})
    assert r.status_code == 200, r.text
    assert r.json()["via"] == "chat"
    assert fwd.images_calls == []          # nativo MAI tentato con reference
    assert fwd.chat_calls == ["google"]
    content = fwd.chat_payloads[0]["messages"][0]["content"]
    assert content[0]["type"] == "image_url"


def test_generations_senza_refs_usa_nativo(client_single, monkeypatch):
    c, m = client_single
    fwd = _fun(monkeypatch, m,
               native=lambda d, p: (200, {"created": 1,
                                          "data": [{"url": "https://img/n.png"}]}))
    r = c.post("/v1/images/generations", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "un gatto"})
    assert r.status_code == 200, r.text
    assert r.json()["data"][0]["url"] == "https://img/n.png"
    assert fwd.images_calls == ["google"]
    assert fwd.chat_calls == []


def test_generations_con_refs_senza_image_edit_400(client_no_edit,
                                                   monkeypatch):
    c, m = client_no_edit
    _fun(monkeypatch, m)
    r = c.post("/v1/images/generations", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "x",
                     "images": ["data:image/png;base64,AAA"]})
    assert r.status_code == 400
    assert "image_edit" in r.json()["error"]["message"]
