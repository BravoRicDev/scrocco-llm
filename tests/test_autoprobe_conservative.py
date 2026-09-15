"""Autoprobe conservativo (free-tier): budget probe/giorno per CHIAVE, gap
orario, skip quando la chiave ha prova di vita dal traffico reale, ritiro che
NON scatta mai per sola quota, e giro giornaliero sui ritirati."""
import time
from collections import deque

import pytest

from app import autoprobe as AP
from app.ctxcompact import CtxCompactConfig  # noqa: F401  (import sanity)
from app.policy import Policy
from app.router import Router, _is_quota_evidence


# --------------------------------------------------------------- _is_quota
def test_is_quota_evidence():
    assert _is_quota_evidence("http_429") is True
    assert _is_quota_evidence(None, 429) is True
    assert _is_quota_evidence("quota_exhausted") is True
    assert _is_quota_evidence("rate_limit") is True
    assert _is_quota_evidence("http_503", 503) is False
    assert _is_quota_evidence("timeout") is False


# ------------------------------------------------------------- day budget
def test_key_day_budget_provider_aware():
    AP._key_probe_day.clear()
    dep = {"provider": "openrouter", "api_key": "sk-abc"}
    now = time.time()
    assert AP._key_day_max(dep, 4) == 1          # openrouter: 1 probe/giorno
    assert AP._key_day_ok(dep, now, 4) is True
    AP._note_key_probe_day(dep, now)
    assert AP._key_day_ok(dep, now, 4) is False  # budget esaurito
    # provider senza override: vale il cap base
    dep2 = {"provider": "mistral", "api_key": "sk-xyz"}
    assert AP._key_day_max(dep2, 4) == 4


def test_key_day_budget_prunes_24h():
    AP._key_probe_day.clear()
    dep = {"provider": "mistral", "api_key": "sk-1"}
    old = time.time() - 90000
    AP._key_probe_day["sk-1"] = deque([old] * 10)
    assert AP._key_day_ok(dep, time.time(), 4) is True


# -------------------------------------------------------------- ok-fresh
def test_key_ok_fresh_skips_proven_key():
    now = time.time()
    dep = {"api_key": "sk-live"}
    okmap = {"sk-live": now - 100}
    assert AP._key_ok_fresh(okmap, dep, now, 43200) is True
    okmap_old = {"sk-live": now - 99999}
    assert AP._key_ok_fresh(okmap_old, dep, now, 43200) is False
    assert AP._key_ok_fresh({}, dep, now, 43200) is False


# --------------------------------------------------- ritiro mai per quota
class _KhFake:
    def __init__(self):
        self.state = {}

    def is_retired(self, u):
        return self.state.get(u) == "retired"

    def set_state(self, u, s, reason=None):
        self.state[u] = s

    def save(self):
        pass


def _policy():
    return Policy.from_dict({})


def test_retire_non_scatta_per_quota(monkeypatch):
    import app.main as M
    kh = _KhFake()
    monkeypatch.setattr(M, "KEYHEALTH", kh, raising=False)
    r = Router.__new__(Router)
    r.policy = _policy()
    r.policy.probe_retire_after = 5

    class S:
        probe_fail_streak = 5
        last_reason = "http_429"

    assert r._maybe_retire_on_probe_fail("u1", S()) is False
    assert kh.state == {}


def test_retire_scatta_su_guasto_vero(monkeypatch):
    import app.main as M
    kh = _KhFake()
    monkeypatch.setattr(M, "KEYHEALTH", kh, raising=False)
    r = Router.__new__(Router)
    r.policy = _policy()
    r.policy.probe_retire_after = 5

    class S:
        probe_fail_streak = 5
        last_reason = "http_403"

    assert r._maybe_retire_on_probe_fail("u2", S()) is True
    assert kh.state["u2"] == "retired"


# ------------------------------------------------------ giro sui ritirati
@pytest.mark.anyio
async def test_retired_pass_riabilita(tmp_path, monkeypatch):
    pass


def test_retired_pass_riabilita_sync(monkeypatch):
    import asyncio
    calls = {}

    class Kh:
        data = {"u1": {"state": "retired"}}

        def clear(self, u):
            calls["cleared"] = u

    class Cfg:
        def deployment_by_unique(self, u):
            return {"unique": u, "api_key": "sk-1", "provider": "mistral"}

    class R:
        policy = _policy()
        config = Cfg()

        class _S:
            probe_fail_streak = 3

        def stats_for(self, u):
            return self._S()

        def clear_cooldown(self, u):
            calls["cooldown"] = u

    async def fake_probe(forwarder, dep, timeout):
        return True, 12.0, 200, ""

    monkeypatch.setattr(AP, "_keyhealth", lambda: Kh())
    monkeypatch.setattr(AP, "_probe_one", fake_probe)
    monkeypatch.setattr(AP, "_key_gap_ok", lambda *a, **k: True)
    AP._key_probe_day.clear()
    asyncio.run(AP._retired_pass(R(), None))
    assert calls.get("cleared") == "u1"
    assert calls.get("cooldown") == "u1"


# ------------------------------------------------------------- defaults
def test_default_conservativi():
    p = _policy()
    assert p.cooldown_autoprobe_per_dim == 1
    assert p.cooldown_autoprobe_max_total == 3
    assert p.cooldown_autoprobe_key_gap_sec == 3600.0
    assert p.cooldown_autoprobe_key_day_max == 2
    assert p.cooldown_autoprobe_key_ok_fresh_sec == 43200.0
    assert p.cooldown_autoprobe_retired_enabled is True


# --------------------------------------------- blocco chiave dopo 429
def test_quota_code_e_blocco_giornaliero():
    AP._key_quota_day.clear()
    dep = {"provider": "mistral", "api_key": "sk-q"}
    now = time.time()
    assert AP._quota_code(429) is True
    assert AP._quota_code("http_429") is True
    assert AP._quota_code(503) is False
    assert AP._key_saturated(None, dep, now) is False
    AP._block_key_for_day(dep, now)
    assert AP._key_saturated(None, dep, now) is True
    # dopo 24h il blocco decade
    assert AP._key_saturated(None, dep, now + 86401) is False


def test_key_saturated_da_traffico_reale():
    import hashlib
    class R:
        _key_soft = {}
    key = "sk-real"
    tag = hashlib.sha256(key.encode()).hexdigest()[:12]
    R._key_soft[tag] = time.time() + 60
    dep = {"provider": "openrouter", "api_key": key}
    assert AP._key_saturated(R(), dep, time.time()) is True


# ------------------------------- ritirati: permanenti vs quota
def test_retired_permanent_vs_usable(monkeypatch):
    import app.main as M
    kh = _KhFake()

    class Kh2(_KhFake):
        def is_retired(self, u):
            return u in self.state

        def is_permanently_retired(self, u):
            return str(self.state.get(u, "")).startswith("permanent")

    kh = Kh2()
    monkeypatch.setattr(M, "KEYHEALTH", kh, raising=False)
    r = Router.__new__(Router)
    r.policy = _policy()

    class Cfg:
        def deployment_by_unique(self, u):
            return {"unique": u}

    r.config = Cfg()

    def _is_retired(u):
        return u in kh.state

    monkeypatch.setattr(r, "is_retired", _is_retired)
    kh.state["u-perm"] = "permanent_dead"
    kh.state["u-quota"] = "probe_escalation_cap"
    assert r._retired_permanent("u-perm") is True
    assert r._retired_usable("u-perm") is False
    assert r._retired_permanent("u-quota") is False
    assert r._retired_usable("u-quota") is True
