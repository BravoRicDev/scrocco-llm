"""Test degli helper introdotti dal round di pulizia/prestazioni:
indice `deployment_by_unique`, potatura mappe (`routing/evict`), rotazione
JSONL condivisa (`jsonl_store`)."""
from __future__ import annotations

import json
import random

from app.config import GatewayConfig
from app.jsonl_store import append_jsonl, rotate_segments
from app.routing.evict import drop_expired, entry_ts, evict_oldest
import app.state as gw_state


def _cfg(groups: dict[str, list[dict]]) -> GatewayConfig:
    cfg = GatewayConfig.__new__(GatewayConfig)
    cfg.groups = groups
    return cfg


def _dep(unique: str, group: str) -> dict:
    return {"unique": unique, "group": group}


def _linear(cfg: GatewayConfig, unique: str):
    for lst in cfg.groups.values():
        for d in lst:
            if d["unique"] == unique:
                return d
    return None


def test_deployment_by_unique_matches_linear_scan():
    a, b = _dep("a", "g1"), _dep("b", "g2")
    cfg = _cfg({"g1": [a], "g2": [b], "alias": [b, a]})
    assert cfg.deployment_by_unique("a") is a
    assert cfg.deployment_by_unique("b") is b
    assert cfg.deployment_by_unique("zzz") is None


def test_deployment_by_unique_sees_in_place_mutations():
    a = _dep("a", "g1")
    cfg = _cfg({"g1": [a]})
    assert cfg.deployment_by_unique("a") is a
    # aggiunta in place (draining / test): nessun reload
    c = _dep("c", "g1")
    cfg.groups["g1"].append(c)
    assert cfg.deployment_by_unique("c") is c
    # rimozione con nuova lista (stop_draining)
    cfg.groups["g1"] = [x for x in cfg.groups["g1"] if x["unique"] != "a"]
    assert cfg.deployment_by_unique("a") is None
    # sostituzione dell'intero dict (reload)
    a2 = _dep("a", "g9")
    cfg.groups = {"g9": [a2]}
    assert cfg.deployment_by_unique("a") is a2
    assert cfg.deployment_by_unique("c") is None


def test_deployment_by_unique_without_group_key():
    d = {"unique": "x"}
    cfg = _cfg({"g": [d]})
    assert cfg.deployment_by_unique("x") is d
    assert cfg.deployment_by_unique("x") is d


def test_evict_oldest_equals_sorted_slice():
    rng = random.Random(7)
    for _ in range(50):
        d = {f"s{i}": (i, float(rng.randint(0, 20))) for i in range(rng.randint(0, 40))}
        cap = rng.randint(0, 30)
        expected = dict(d)
        if len(expected) > cap:
            for k in sorted(expected, key=lambda k: expected[k][1])[: len(expected) - cap]:
                expected.pop(k)
        evict_oldest(d, entry_ts, cap)
        assert d == expected


def test_drop_expired():
    d = {"old": (1, 0.0), "new": (1, 95.0), "edge": (1, 90.0)}
    drop_expired(d, entry_ts, now=100.0, ttl=10.0)
    assert set(d) == {"new", "edge"}


def test_rotate_segments_and_append(tmp_path):
    p = tmp_path / "x.jsonl"
    assert rotate_segments(p, 10, 2) is False          # file assente
    append_jsonl(p, [{"a": 1}, {"b": "è"}])
    lines = p.read_text(encoding="utf-8").splitlines()
    assert [json.loads(x) for x in lines] == [{"a": 1}, {"b": "è"}]
    assert rotate_segments(p, 10_000, 2) is False       # sotto soglia
    assert rotate_segments(p, 1, 2) is True
    assert not p.exists() and (tmp_path / "x.jsonl.1").exists()
    append_jsonl(p, [{"c": 3}])
    assert rotate_segments(p, 1, 2) is True
    assert (tmp_path / "x.jsonl.2").exists() and (tmp_path / "x.jsonl.1").exists()


# ------------------------------------------------ atomic_store (snapshot)
def test_encode_json_matches_json_dump():
    import io

    from app.atomic_store import encode_json
    obj = {"a": [1, 2.5, None, True], "é": {"n": float("inf")}, 3: "x"}
    for indent in (None, 1):
        buf = io.StringIO()
        json.dump(obj, buf, indent=indent)
        assert encode_json(obj, indent=indent) == buf.getvalue()


def test_stale_snapshot_never_overwrites_newer(tmp_path):
    from app.atomic_store import freeze_json, load_json, save_json_text
    p = tmp_path / "s.json"
    old = freeze_json({"v": 1})           # fotografia presa PRIMA
    new = freeze_json({"v": 2})
    assert save_json_text(p, new)
    assert save_json_text(p, old)         # arriva tardi: ignorato
    assert load_json(p) == {"v": 2}


def test_unencodable_snapshot_fails_like_save_json(tmp_path):
    from app.atomic_store import freeze_json, save_json, save_json_text
    p = tmp_path / "bad.json"
    assert save_json_text(p, freeze_json({"s": {1}})) is False
    assert save_json(p, {"s": {1}}) is False
    assert not p.exists() and not (tmp_path / "bad.json.tmp").exists()


# ------------------------------------------------------------- bgtasks
def test_spawn_keeps_reference_until_done():
    import asyncio

    from app import bgtasks

    async def main():
        reg: set = set()
        done = asyncio.Event()

        async def job():
            await done.wait()
            return 42

        t = bgtasks.spawn(asyncio.get_running_loop(), job(), registry=reg)
        assert t in reg
        done.set()
        assert await t == 42
        await asyncio.sleep(0)            # callback di completamento
        assert t not in reg

    asyncio.run(main())


# ------------------------------------------------ rolling usage windows
def test_rolling_usage_matches_full_resum():
    """Somma incrementale == somma completa della vecchia implementazione
    (conteggi e token esatti, pesi a meno dell'arrotondamento float)."""
    from collections import deque

    import pytest

    from app.routing.usage import UsageMixin

    class _Pol:
        go_balance_window_sec = 300

    class _R(UsageMixin):
        policy = _Pol()

    rng = random.Random(11)
    r = _R()
    ref_w: deque = deque()
    ref_o: deque = deque()
    now = 1_000_000.0
    for _ in range(5000):
        now += rng.uniform(0, 60)
        ctx = rng.choice([None, 0, -5, 100, 7999, 8000, 8001, 80000, 123456.7, "x"])
        r.note_usage("u", ts=now, ctx_est=ctx)
        try:
            w = max(1.0, float(int(ctx)) / 8000.0) if ctx else 1.0
        except (TypeError, ValueError):
            w = 1.0
        ref_w.append((now, w))
        tok = rng.choice([0, 1, 50, 4096])
        r.note_output_tokens("u", tok, ts=now)
        if tok > 0:
            ref_o.append((now, tok))
        probe = now + rng.uniform(0, 90000)
        while ref_w and ref_w[0][0] < probe - 86400.0:
            ref_w.popleft()
        assert r.usage_weight_24h("u", now=probe) == pytest.approx(sum(x for _t, x in ref_w), rel=1e-12)
        assert r.usage_count_24h("u", now=probe) == len(ref_w)
        while ref_o and ref_o[0][0] < probe - 300:
            ref_o.popleft()
        assert r.output_tokens_window("u", now=probe) == sum(n for _t, n in ref_o)
        if rng.random() < 0.01:          # finestra svuotata: si riparte
            now = probe


# ------------------------------------------------------------ suppressed
def test_report_suppressed_is_rate_limited(caplog, monkeypatch):
    import logging

    from app import suppressed

    monkeypatch.setattr(suppressed, "_counts", {})
    monkeypatch.setattr(suppressed, "_last_report", {})
    caplog.set_level(logging.WARNING, logger="nx.suppressed")
    for _ in range(5):
        try:
            raise RuntimeError("boom")
        except Exception:
            suppressed.report_suppressed("test.site")
    recs = [r for r in caplog.records if r.name == "nx.suppressed"]
    assert len(recs) == 1 and recs[0].exc_info is not None
    assert suppressed.suppressed_counts() == {"test.site": 5}


# --------------------------------------------------------- policy readers
def test_policy_float_int_match_getattr_or_expression():
    from app.policy import policy_float, policy_int

    class P:
        zero = 0
        none = None
        val = "7"
        f = 2.5

    p = P()
    for name in ("zero", "none", "val", "f", "missing"):
        for d, fz in ((3, 3), (3, 0), (1.5, 0.0)):
            assert policy_float(p, name, d, falsy=fz) == float(getattr(p, name, d) or fz)
            assert policy_int(p, name, d, falsy=fz) == int(getattr(p, name, d) or fz)
        assert policy_float(p, name, 9) == float(getattr(p, name, 9) or 9)


# ------------------------------------------- thought signatures throttle
def test_watcher_tick_persists_thought_sigs(monkeypatch, tmp_path):
    """Il tick del watcher salva stats/routing (F26) E le firme Gemini: prima
    il throttle condiviso con le stats le rimandava sempre allo shutdown."""
    import app.main as M
    from app import runtime_persistence as rp

    monkeypatch.setattr(gw_state, "PERSIST_STATS", True)
    monkeypatch.setattr(gw_state, "_last_stats_save", 0.0)
    monkeypatch.setattr(gw_state, "_last_routing_save", 0.0)
    monkeypatch.setattr(gw_state, "_last_thought_sigs_save", 0.0)
    monkeypatch.setattr(gw_state, "_thought_sigs_file", tmp_path / "thought_sigs.json")
    writes = rp._maybe_save_all(defer=True)            # stesso ordine del watcher
    writes += rp._maybe_save_thought_sigs(defer=True)
    assert tmp_path / "thought_sigs.json" in [w.path for w in writes]
    assert rp._maybe_save_thought_sigs(defer=True) == []   # throttle 60s
