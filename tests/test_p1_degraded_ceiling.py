"""P1-6 degraded mode + P1-7 tetto cooldown stimati."""
import asyncio
import os
import tempfile

import httpx
import pytest

from app.config import GatewayConfig
from app.forwarder import Forwarder
from app.policy import Policy
from app.router import Router, set_current_session

_CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,p1d-a,openrouter,https://openrouter.ai/api/v1,free,128,8000,5,K-A,
t@x.com,p1d-b,groq,https://api.groq.com/openai/v1,free,128,8000,5,K-B,
t@x.com,p1d-c,cerebras,https://api.cerebras.ai/v1,free,128,8000,5,K-C,
"""


def _mk(pol=None, csv_text=_CSV):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(csv_text)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, pol or Policy.from_dict({}))
    by_key = {}
    for lst in cfg.groups.values():
        for d in lst:
            by_key[d.get("api_key")] = d
    return cfg, r, by_key, path


# ------------------------------------------------------------ degraded ----
def test_knob_defaults_e_parse():
    p = Policy.from_dict({})
    assert p.degraded_mode_enabled is True
    assert p.degraded_healthy_ratio == 0.5
    assert p.degraded_min_providers == 3
    assert p.degraded_entry_grace_sec == 60
    assert p.degraded_exit_grace_sec == 120
    assert p.cooldown_estimate_ceiling_sec == 0
    q = Policy.from_dict({"degraded_mode_enabled": False,
                          "degraded_healthy_ratio": 0.25,
                          "degraded_min_providers": 2,
                          "cooldown_estimate_ceiling_sec": 300})
    assert q.degraded_mode_enabled is False
    assert q.degraded_healthy_ratio == 0.25
    assert q.degraded_min_providers == 2
    assert q.cooldown_estimate_ceiling_sec == 300
    with pytest.raises(ValueError):
        Policy.from_dict({"degraded_healthy_ratio": 2.0})


class TestDegradedModeEnabledCoercion:
    """FIX 1: `degraded_mode_enabled` non era piu' `bool(...)` nudo.

    Con `bool()` una stringa YAML come "false" diventava True (bool di una
    stringa non vuota) e "0" idem: il knob si disattivava da solo. Ora passa
    da `_coerce_bool`, che accetta true/on/sì/1 e false/off/no/0 e solleva
    ValueError (=> 400 admin) su qualsiasi altro input.
    """

    def test_string_false_is_not_truthy(self):
        p = Policy.from_dict({"degraded_mode_enabled": "false"})
        assert p.degraded_mode_enabled is False

    @pytest.mark.parametrize("raw_false", ["false", "False", "FALSE", " off ",
                                          "no", "0", " false  "])
    def test_falsy_strings(self, raw_false):
        p = Policy.from_dict({"degraded_mode_enabled": raw_false})
        assert p.degraded_mode_enabled is False

    @pytest.mark.parametrize("raw_true", ["true", "True", " on ", "sì", "si",
                                         "1", "yes"])
    def test_truthy_strings(self, raw_true):
        p = Policy.from_dict({"degraded_mode_enabled": raw_true})
        assert p.degraded_mode_enabled is True

    def test_native_bool_unchanged(self):
        assert Policy.from_dict(
            {"degraded_mode_enabled": True}).degraded_mode_enabled is True
        assert Policy.from_dict(
            {"degraded_mode_enabled": False}).degraded_mode_enabled is False

    @pytest.mark.parametrize("bad", ["", "  ", "forse", "2", "disabled",
                                     "maybe"])
    def test_non_bool_string_raises(self, bad):
        with pytest.raises(ValueError, match="degraded_mode_enabled"):
            Policy.from_dict({"degraded_mode_enabled": bad})

    def test_non_scalar_raises(self):
        with pytest.raises(ValueError, match="degraded_mode_enabled"):
            Policy.from_dict({"degraded_mode_enabled": ["true"]})
        with pytest.raises(ValueError, match="degraded_mode_enabled"):
            Policy.from_dict({"degraded_mode_enabled": 1.5})

    def test_absent_keeps_default(self):
        assert Policy.from_dict({}).degraded_mode_enabled is True
        assert Policy.from_dict(
            {"degraded_mode_enabled": None}).degraded_mode_enabled is True


class TestKeyConcurrencyEnabledCoercion:
    """FIX 1, secondo knob: stessa difetta su `key_concurrency_enabled`."""

    def test_string_false_is_not_truthy(self):
        p = Policy.from_dict({"key_concurrency_enabled": "false"})
        assert p.key_concurrency_enabled is False

    def test_string_true(self):
        p = Policy.from_dict({"key_concurrency_enabled": "on"})
        assert p.key_concurrency_enabled is True

    def test_default_is_false(self):
        assert Policy.from_dict({}).key_concurrency_enabled is False

    def test_non_bool_string_raises(self):
        with pytest.raises(ValueError, match="key_concurrency_enabled"):
            Policy.from_dict({"key_concurrency_enabled": "forse"})


def test_hosts_health_e_degraded_con_grace_zero():
    pol = Policy.from_dict({"degraded_entry_grace_sec": 0,
                            "degraded_exit_grace_sec": 0})
    cfg, r, by_key, path = _mk(pol)
    try:
        assert r.hosts_health() == (3, 3)
        assert r.degraded_active() is False        # tutto sano
        # 2 host su 3 morti -> ratio 1/3 < 0.5, entry grace 0 -> degradato
        r.mark_failed(by_key["K-A"]["unique"], seconds=600, reason="http_429")
        r.mark_failed(by_key["K-B"]["unique"], seconds=600, reason="http_429")
        h, t = r.hosts_health()
        assert (h, t) == (1, 3)
        assert r.degraded_active() is True
        v = r.degraded_view()
        assert v["active"] is True and v["hosts_healthy"] == 1 \
            and v["hosts_total"] == 3 and v["ratio"] <= 0.34
        # tornano sani -> esce subito (exit grace 0)
        r.clear_cooldown(by_key["K-A"]["unique"])
        r.clear_cooldown(by_key["K-B"]["unique"])
        assert r.degraded_active() is False
    finally:
        os.unlink(path)


def test_degraded_non_scatta_sotto_min_providers():
    pol = Policy.from_dict({"degraded_entry_grace_sec": 0,
                            "degraded_min_providers": 5})
    csv2 = _CSV.replace("p1d-c,cerebras,https://api.cerebras.ai/v1,free,128,"
                        "8000,5,K-C,\n", "")
    cfg, r, by_key, path = _mk(pol, csv2)
    try:
        r.mark_failed(by_key["K-A"]["unique"], seconds=600, reason="http_429")
        assert r.hosts_health() == (1, 2)
        assert r.degraded_active() is False       # min_providers=5
    finally:
        os.unlink(path)


def test_host_quarantenato_non_conta_sano():
    cfg, r, by_key, path = _mk()
    try:
        r.quarantine_endpoint("api.groq.com", 600)
        h, t = r.hosts_health()
        assert (h, t) == (2, 3)
    finally:
        os.unlink(path)


def test_degraded_blocca_il_refill_ns():
    cfg, r, by_key, path = _mk()
    a = by_key["K-A"]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        return await fwd.call_with_fallback(
            r, "test", a,
            {"model": a["model"], "messages": [{"role": "user",
                                                "content": "x"}]},
            need=frozenset({"text"}), session="s1", ses="s1")
    try:
        r.degraded_active = lambda: True          # forza il degraded
        set_current_session("s1")
        try:
            asyncio.run(_run())
        finally:
            set_current_session(None)
        # nessun canario in piu': una sola chiamata
        assert seen == ["openrouter.ai"]
    finally:
        os.unlink(path)


# ------------------------------------------------- P1-7 tetto cooldown ----
def test_tetto_solo_su_heuristic():
    pol = Policy.from_dict({"cooldown_estimate_ceiling_sec": 120})
    cfg, r, by_key, path = _mk(pol)
    try:
        # DERIVATO (nostra stima) -> cappato
        u1 = by_key["K-A"]["unique"]
        r.mark_failed(u1, reason="http_429")
        assert 0 < r.cooldown_residual(u1) <= 122
        # seconds ESPLICITO con 429 = Retry-After dichiarato -> mai cappato
        u2 = by_key["K-B"]["unique"]
        r.mark_failed(u2, seconds=3600, reason="http_429")
        assert r.cooldown_residual(u2) > 121
        assert r.cooldown_provenance(u2) == "authoritative"
        # credit/tier -> mai cappati
        u3 = by_key["K-C"]["unique"]
        r.mark_failed(u3, seconds=3600, reason="http_402",
                      provenance="credit")
        assert r.cooldown_residual(u3) > 121
    finally:
        os.unlink(path)


def test_tetto_zero_disattiva():
    cfg, r, by_key, path = _mk()
    try:
        u = by_key["K-A"]["unique"]
        r.mark_failed(u, reason="http_429")
        assert r.cooldown_residual(u) > 1000
    finally:
        os.unlink(path)
