"""Dedup /models: una sola GET per endpoint (prima chiave valida, fallback
sulle successive) + cache TTL. Client httpx FAKE, nessuna rete reale."""
import asyncio

from app import provider_models
from app.provider_models import fetch_provider_models


class _Resp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data if data is not None else {"data": []}

    def json(self):
        return self._data


class _FakeHttp:
    """Risponde in base alla chiave del header Authorization."""

    def __init__(self, script):
        self.script = script          # {key: _Resp}
        self.calls: list[str] = []

    async def get(self, url, headers=None):
        key = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        self.calls.append(key)
        return self.script.get(key, _Resp(500))


def _run(coro):
    return asyncio.run(coro)


def setup_function(_fn):
    provider_models.clear_cache()


def test_fallback_to_second_key():
    http = _FakeHttp({"k1": _Resp(401), "k2": _Resp(200, {"data": [{"id": "m1"}]})})
    res = _run(fetch_provider_models(http, "https://p.test/v1", ["k1", "k2"]))
    assert res.ok is True
    assert res.ids == {"m1"}
    assert res.tried == 2
    assert http.calls == ["k1", "k2"]


def test_first_key_wins_siblings_skipped():
    http = _FakeHttp({"k1": _Resp(200, {"data": [{"id": "m1"}]})})
    res = _run(fetch_provider_models(http, "https://p.test/v1",
                                     ["k1", "k2", "k3"]))
    assert res.ok is True
    assert res.tried == 1
    assert res.skipped == 2
    assert http.calls == ["k1"]           # una sola chiamata per endpoint


def test_duplicate_keys_are_collapsed():
    http = _FakeHttp({"k1": _Resp(200)})
    _run(fetch_provider_models(http, "https://p.test/v1", ["k1", "k1", "k1"]))
    assert http.calls == ["k1"]


def test_cache_ttl_and_force():
    http = _FakeHttp({"k1": _Resp(200, {"data": [{"id": "m1"}]})})
    r1 = _run(fetch_provider_models(http, "https://p.test/v1", ["k1"]))
    assert r1.cached is False
    r2 = _run(fetch_provider_models(http, "https://p.test/v1", ["k1"]))
    assert r2.cached is True
    assert http.calls == ["k1"]           # seconda call servita dalla cache
    r3 = _run(fetch_provider_models(http, "https://p.test/v1", ["k1"],
                                    force=True))
    assert r3.cached is False
    assert http.calls == ["k1", "k1"]


def test_ttl_zero_disables_cache():
    http = _FakeHttp({"k1": _Resp(200)})
    _run(fetch_provider_models(http, "https://p.test/v1", ["k1"], ttl_sec=0))
    _run(fetch_provider_models(http, "https://p.test/v1", ["k1"], ttl_sec=0))
    assert http.calls == ["k1", "k1"]


def test_405_stops_trying_remaining_keys():
    http = _FakeHttp({"k1": _Resp(405)})
    res = _run(fetch_provider_models(http, "https://p.test/v1", ["k1", "k2"]))
    assert res.ok is False
    assert res.tried == 1
    assert http.calls == ["k1"]           # 405 = /models non supportato


def test_all_keys_fail_reports_error():
    http = _FakeHttp({"k1": _Resp(401), "k2": _Resp(403)})
    res = _run(fetch_provider_models(http, "https://p.test/v1", ["k1", "k2"]))
    assert res.ok is False
    assert res.tried == 2
    assert "403" in (res.error or "")
