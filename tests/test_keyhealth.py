"""Lifecycle chiavi morte: dead_suspect -> retired -> sblocco. MAI delete."""
import time

import pytest

from app.keyhealth import KeyHealth


@pytest.fixture()
def kh(tmp_path):
    return KeyHealth(tmp_path)


def test_healthy_when_success(kh):
    assert kh.observe("u1", fail_streak=0, success_ema=0.9,
                      is_cooled=False) == "healthy"
    # anche una chiave gia' marcata morta torna healthy al successo
    kh.set_state("u2", "retired")
    assert kh.observe("u2", fail_streak=0, success_ema=None,
                      is_cooled=False) == "healthy"
    assert "u2" not in kh.data


def test_dead_suspect_requires_all_conditions(kh):
    # streak basso: non basta
    assert kh.observe("a", fail_streak=2, success_ema=0.05,
                      is_cooled=True) != "dead_suspect"
    # streak alto ma non cooled: no
    assert kh.observe("b", fail_streak=9, success_ema=0.05,
                      is_cooled=False) is None
    # condizioni tutte: SI'
    assert kh.observe("c", fail_streak=6, success_ema=0.02,
                      is_cooled=True) == "dead_suspect" or \
        kh.data["c"].get("state") == "dead_suspect"


def test_retirement_after_days_and_persistence(kh, tmp_path):
    old = time.time() - 8 * 86400
    rec = {"first_dead_ts": int(old), "state": "dead_suspect",
           "last_reason": "no_credits", "streak_max": 12}
    kh.data["zombi"] = rec
    fresh = {"first_dead_ts": int(time.time()), "state": "dead_suspect",
             "streak_max": 6}
    kh.data["fresco"] = fresh
    promoted = kh.apply_retirement(retire_after_days=7)
    assert "zombi" in promoted and "fresco" not in promoted
    assert kh.is_retired("zombi") and not kh.is_retired("fresco")
    # persistenza su disco e ricarica
    kh.save()
    kh2 = KeyHealth(tmp_path)
    assert kh2.is_retired("zombi")


def test_clear_unblocks(kh):
    kh.set_state("u", "retired")
    assert kh.is_retired("u")
    kh.clear("u")
    assert not kh.is_retired("u")


# --------------------------------- integrazione router (esclusione) ------

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,m-a,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-A,text
t@x.com,m-b,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-B,text
"""


def test_router_excludes_retired(tmp_path, monkeypatch):
    import os, tempfile
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    from app.config import GatewayConfig
    from app.policy import Policy
    from app.router import Router
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict(
        {"capability_routing": {"model_capabilities": {}}}))
    os.unlink(path)
    # KEYHEALTH isolata nella tmp del test
    import app.main as m
    from app.keyhealth import KeyHealth as KH
    kh = KH(tmp_path)
    monkeypatch.setattr(m, "KEYHEALTH", kh)
    dep_a = next(d for d in cfg.groups["scrocco-llm-test-32k"]
                 if d["api_key"] == "K-A")
    kh.set_state(dep_a["unique"], "retired")
    picked = r.pick_deployment("scrocco-llm-test-32k",
                               exclude=dep_a["unique"])
    # con A retired e B... pick(exclude=A) deve dare comunque B, mai A
    assert picked["unique"] != dep_a["unique"]
    picks = {r.pick_deployment("scrocco-llm-test-32k")["unique"]
             for _ in range(30)}
    assert dep_a["unique"] not in picks     # MAI il retired


def test_probe_success_clears_state(tmp_path, monkeypatch):
    """Il probe riuscito e' la prova suprema: pulisce dead/retired."""
    import asyncio
    import httpx
    import app.admin as adm
    import app.main as m
    from app.keyhealth import KeyHealth as KH
    kh = KH(tmp_path)
    monkeypatch.setattr(m, "KEYHEALTH", kh)
    monkeypatch.setattr(adm, "_probe_path", lambda: tmp_path / "probe.json")
    dep = {"unique": "g__m__0", "group": "g", "model": "m",
           "api_base": "https://ok.test/v1", "api_key": "K"}
    kh.set_state(dep["unique"], "retired")
    real_cls = httpx.AsyncClient
    transport = httpx.MockTransport(lambda req: httpx.Response(
        200, json={"choices": [{"message": {"content": "A"}}]}))
    monkeypatch.setattr(adm.httpx, "AsyncClient",
                        lambda *a, **k: real_cls(transport=transport))
    out = asyncio.run(adm._probe_one(real_cls(transport=transport), dep,
                                     force=False))
    assert out["ok"] is True
    assert not kh.is_retired(dep["unique"])
