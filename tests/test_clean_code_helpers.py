"""Test degli helper introdotti dal round di pulizia/prestazioni:
indice `deployment_by_unique`, potatura mappe (`routing/evict`), rotazione
JSONL condivisa (`jsonl_store`)."""
from __future__ import annotations

import json
import random

from app.config import GatewayConfig
from app.jsonl_store import append_jsonl, rotate_segments
from app.routing.evict import drop_expired, entry_ts, evict_oldest


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
