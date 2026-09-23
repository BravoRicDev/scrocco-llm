"""Store immagini + endpoint di download: `url` NOSTRO accanto a `b64_json`.

Gli endpoint /v1/images/* restituiscono un `url` del gateway
(`GET /v1/images/files/{id}`) cosi' il client puo' scaricare al volo anche
quando l'upstream risponde in base64. Gli URL http(s) del provider vengono
scaricati e ri-ospitati (mirror). Qui si blinda:
  - lo store in-memory (roundtrip, TTL, eviction, validazione immagine);
  - l'endpoint pubblico di download (200 + bytes, 404, nessuna auth);
  - la localizzazione degli item (data-URI/b64 -> url nostro + b64);
  - il mirror degli URL provider (download remoto);
  - la base URL (policy `images.url_base` e X-Forwarded-*).
"""
import base64

import httpx
import pytest
from fastapi.testclient import TestClient

from app.forwarder import UpstreamError
from app import imagestore


_PNG = b"\x89PNG\r\n\x1a\n0123456789"
_B64 = base64.b64encode(_PNG).decode()


# --------------------------------------------------------------- unit: store
def _reset_store(**kw):
    imagestore.clear()
    imagestore.configure(ttl_sec=3600, max_items=500, max_bytes=536870912)
    if kw:
        imagestore.configure(**kw)


def test_store_roundtrip():
    _reset_store()
    fid = imagestore.put(_PNG, "image/png")
    assert fid
    assert imagestore.get(fid) == (_PNG, "image/png")


def test_store_rifiuta_non_immagine():
    _reset_store()
    assert imagestore.put(b"not an image", "text/plain") is None
    assert imagestore.put(b"", "image/png") is None


def test_store_ttl_scadenza():
    _reset_store(ttl_sec=60)
    fid = imagestore.put(_PNG, "image/png")
    imagestore._ITEMS[fid]["ts"] -= 10_000
    assert imagestore.get(fid) is None


def test_store_eviction_max_items():
    _reset_store(max_items=2)
    ids = [imagestore.put(_PNG, "image/png") for _ in range(3)]
    assert imagestore.stats()["items"] == 2
    assert imagestore.get(ids[0]) is None          # il piu' vecchio evitto
    assert imagestore.get(ids[-1]) is not None


def test_store_eviction_max_bytes():
    _reset_store(max_bytes=len(_PNG) + 1)
    imagestore.put(_PNG, "image/png")
    imagestore.put(_PNG, "image/png")
    assert imagestore.stats()["bytes"] <= len(_PNG) + 1


def test_ext_for_mime():
    assert imagestore.ext_for_mime("image/png") == "png"
    assert imagestore.ext_for_mime("image/jpeg") == "jpg"
    assert imagestore.ext_for_mime("image/webp; charset=x") == "webp"
    assert imagestore.ext_for_mime("application/octet-stream") == "bin"


# ------------------------------------------------------------- e2e harness
class _FakeForwarder:
    def __init__(self, native, chat):
        self.native_fn = native
        self.chat_fn = chat
        self.images_calls: list[str] = []
        self.chat_calls: list[str] = []

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
        status, body = self.chat_fn(dep, payload)
        if status >= 400:
            raise UpstreamError(-status if status != 429 else status,
                                str(body)[:500])
        return body


_GOOGLE = "https://generativelanguage.googleapis.com/v1beta/openai"
_CSV = ("commento,modello,provider,endpoint,data,context,max_input,priority,"
        "scrocco-llm-test,caps\n"
        "seed,models/gemini-2.5-flash-image,google,"
        f"{_GOOGLE},free,8,8000,1,sk-test-key,\"image_gen,image_edit\"\n"
        "orfall,google/gemini-2.5-flash-image,openrouter,"
        "https://openrouter.ai/api/v1,fallback,128,0,0,sk-or-test-key,image_gen\n")

_CHAT_DATA_URI = lambda dep, p: (200, {"choices": [{"message": {"images": [
    {"type": "image_url",
     "image_url": {"url": "data:image/png;base64," + _B64}}]}}]})
_CHAT_HTTP = lambda dep, p: (200, {"choices": [{"message": {"images": [
    {"type": "image_url",
     "image_url": {"url": "https://provider.example/x.png"}}]}}]})


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
    m.imagestore.clear()
    m.router._cooldown.clear()
    m.router.policy.cap_groups_enabled = orig[2]
    m.authn.master_key = orig[0]
    m.config.csv_path = orig[1]
    m.config.reload()


@pytest.fixture()
def client(monkeypatch, tmp_path):
    c, m, orig = _make_client(monkeypatch, tmp_path, _CSV)
    yield c, m
    _teardown(m, orig)


MK = {"Authorization": "Bearer test-master-img"}


def _fun(monkeypatch, m, native=lambda d, p: (404, "not found"),
         chat=_CHAT_DATA_URI):
    fwd = _FakeForwarder(native, chat)
    monkeypatch.setattr(m, "forwarder", fwd)
    return fwd


# ------------------------------------------------------------------ e2e
def test_edits_url_nostro_e_download_senza_auth(client, monkeypatch):
    c, m = client
    _fun(monkeypatch, m)
    r = c.post("/v1/images/edits", headers=MK,
               data={"model": "scrocco-llm-test", "prompt": "blu"},
               files={"image": ("ref.png", _PNG, "image/png")})
    assert r.status_code == 200, r.text
    item = r.json()["data"][0]
    assert item["url"].startswith("http://testserver/v1/images/files/")
    assert item["b64_json"] == _B64
    # download pubblico: nessun header di auth
    got = c.get(item["url"])
    assert got.status_code == 200, got.text
    assert got.content == _PNG
    assert got.headers["content-type"].startswith("image/png")
    assert "max-age=" in got.headers.get("cache-control", "")


def test_files_id_sconosciuto_404(client):
    c, _ = client
    assert c.get("/v1/images/files/nope-nope").status_code == 404


def test_mirror_url_provider(client, monkeypatch):
    c, m = client

    async def fake_download(url, *, timeout, max_bytes):
        assert url == "https://provider.example/x.png"
        return _PNG, "image/png"

    monkeypatch.setattr(m, "_download_remote_image", fake_download)
    monkeypatch.setattr(m.router.policy, "images_mirror_remote", True)
    _fun(monkeypatch, m, native=lambda d, p: (200, {
        "created": 1, "data": [{"url": "https://provider.example/x.png"}]}),
        chat=_CHAT_DATA_URI)
    r = c.post("/v1/images/generations", headers=MK,
               json={"model": "scrocco-llm-test", "prompt": "x"})
    assert r.status_code == 200, r.text
    item = r.json()["data"][0]
    assert item["url"].startswith("http://testserver/v1/images/files/")
    assert item["b64_json"] == _B64
    assert c.get(item["url"]).content == _PNG


def test_url_base_policy_override(client, monkeypatch):
    c, m = client
    monkeypatch.setattr(m.router.policy, "images_url_base",
                        "https://cdn.example.com/base")
    _fun(monkeypatch, m)
    r = c.post("/v1/images/edits", headers=MK,
               data={"model": "scrocco-llm-test", "prompt": "x"},
               files={"image": ("ref.png", _PNG, "image/png")})
    assert r.json()["data"][0]["url"].startswith(
        "https://cdn.example.com/base/v1/images/files/")


def test_url_base_da_forwarded_headers(client, monkeypatch):
    c, m = client
    _fun(monkeypatch, m)
    r = c.post("/v1/images/edits", headers={
        **MK, "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "gw.example.net"},
        data={"model": "scrocco-llm-test", "prompt": "x"},
        files={"image": ("ref.png", _PNG, "image/png")})
    assert r.json()["data"][0]["url"].startswith(
        "https://gw.example.net/v1/images/files/")


def test_store_disabilitato_url_provider_intatto(client, monkeypatch):
    c, m = client
    monkeypatch.setattr(m.router.policy, "images_store_enabled", False)
    _fun(monkeypatch, m, chat=_CHAT_HTTP)
    r = c.post("/v1/images/edits", headers=MK,
               data={"model": "scrocco-llm-test", "prompt": "x"},
               files={"image": ("ref.png", _PNG, "image/png")})
    item = r.json()["data"][0]
    assert item["url"] == "https://provider.example/x.png"
