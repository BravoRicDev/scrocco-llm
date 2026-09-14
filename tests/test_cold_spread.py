"""Cold usage-hiding (spread): finestra 24h, taglio del top%, esenzione
dei deployment della sessione, propagazione sulla catena, bootstrap dai log."""
import os
import tempfile
import time

import pytest

from app.config import GatewayConfig
from app.logboot import scan_log
from app.policy import Policy
from app.router import Router, set_current_session

PROF = "test"

_CSV = ["commento,modello,provider,endpoint,data,context,max_input,priority,"
        "scrocco-llm-test,caps"]
for _i in range(12):
    _CSV.append(f"t@x.com,m{_i:02d}-free,groq,https://api.groq.com/openai/v1,"
                f"free,32,8000,0,D{_i:02d},text")
_CSV.append("t@x.com,mgo,groq,https://api.groq.com/openai/v1,,32,8000,0,DGO,text")
CSV = "\n".join(_CSV) + "\n"


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({}))
    yield r
    os.unlink(path)


def _dims(cfg):
    out = []
    for u in cfg.chains[PROF]:
        d = cfg.deployment_by_unique(u)
        g = str((d or {}).get("group", ""))
        if not g.endswith("-go") and not g.endswith("-fallback"):
            out.append(d)
    return out


def _seed_counts(router, deps):
    for i, d in enumerate(deps):
        for _ in range(i):                    # dep i -> i tentativi
            router.note_usage(d["unique"])


# ------------------------------------------------------------------ window
def test_usage_window_24h(router):
    now = time.time()
    router.note_usage("u1", now - 90000.0)    # fuori finestra
    assert router.usage_count_24h("u1", now) == 0
    router.note_usage("u1", now - 100.0)
    assert router.usage_count_24h("u1", now) == 1


# ------------------------------------------------------------------ spread
def test_spread_hides_top20(router):
    deps = _dims(router.config)
    assert len(deps) == 12
    _seed_counts(router, deps)                # 0,1,...,11
    kept = router._spread_hide(list(deps))
    kept_u = {d["unique"] for d in kept}
    top2 = {d["unique"] for d in deps[-2:]}   # i 2 piu' usati
    assert len(kept) == 10
    assert top2.isdisjoint(kept_u)


def test_spread_below_min_pool(router):
    router.policy.ladder_skip_after = 10      # min_pool riusa questo knob
    deps = _dims(router.config)[:5]
    _seed_counts(router, deps)
    assert router._spread_hide(list(deps)) == deps


def test_spread_all_zero_no_cut(router):
    deps = _dims(router.config)
    assert router._spread_hide(list(deps)) == deps


def test_spread_pct_zero_disables(router):
    router.policy.cold_spread_pct = 0.0
    deps = _dims(router.config)
    _seed_counts(router, deps)
    assert router._spread_hide(list(deps)) == deps


def test_spread_exempts_attached(router):
    deps = _dims(router.config)
    _seed_counts(router, deps)
    top = deps[-1]                            # il piu' usato
    router._dep_last_session[top["unique"]] = ("S", time.time())
    set_current_session("S")
    kept = router._spread_hide(list(deps))
    assert top["unique"] in {d["unique"] for d in kept}
    set_current_session(None)


def test_spread_hidden_chain(router):
    hidden = router._spread_hidden_chain(router.config.chains[PROF])
    _seed_counts(router, _dims(router.config))
    hidden = router._spread_hidden_chain(router.config.chains[PROF])
    assert len(hidden) == 2
    dims_u = {d["unique"] for d in _dims(router.config)}
    assert hidden <= dims_u                  # -go/-fallback non toccati


# ------------------------------------------------------------------ logboot
def _line(epoch, body):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch)) + " " + body


def test_logboot_scan(tmp_path):
    now = time.time()
    p = tmp_path / "gateway.log"
    p.write_text(
        _line(now - 10, "INFO [fallback] stream depA 429 motivo=rate -> depB\n")
        + _line(now - 20, 'INFO [summary] {"req":"r1","dep":"depB"}\n')
        + _line(now - 30, "INFO [autoprobe] depC: probe OK -> promosso\n")
        + _line(now - 90000, "INFO [fallback] stream depOLD 500 motivo=x\n"),
        encoding="utf-8")
    usage, probes = scan_log(p, now - 86400.0)
    usage_u = {u for u, _ in usage}
    assert usage_u == {"depA", "depB"}       # depOLD escluso (fuori 24h)
    assert [u for u, _ in probes] == ["depC"]


def test_logboot_missing_file(tmp_path):
    usage, probes = scan_log(tmp_path / "nope.log", time.time())
    assert usage == [] and probes == []
