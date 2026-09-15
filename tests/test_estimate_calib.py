"""F14 — calibrazione closed-loop dell'estimator sull'usage REALE.

`prompt_tokens` dell'upstream e' la verita': se la stima e' troppo bassa
(pt/ctx > 1) il divisor appreso DEVE scendere (stima = chars/divisor), fino a
convergere al divisor vero base/r. Qui si verifica la convergenza, i guard
(solo campioni affidabili), il clamp e la persistenza in adaptive_stats.
"""
import os
import tempfile

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m-a,groq,https://api.groq.com/openai/v1,free,64,8000,0,K-A,text
"""


def _router(**pk):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    pol = Policy.from_dict({})
    for k, v in pk.items():
        setattr(pol, k, v)
    r = Router(cfg, pol)
    return r, path


def _u(r, key="K-A"):
    return next(d for deps in r.config.groups.values() for d in deps
                if d.get("api_key") == key)["unique"]


def test_no_correction_without_samples():
    r, path = _router()
    try:
        assert r.estimate_correction(_u(r)) == 1.0
    finally:
        os.unlink(path)


def test_underestimate_lowers_divisor_and_raises_estimate():
    """pt = 2x ctx: il divisor deve scendere verso base/2 e la correzione
    tendere a ~2 (stima raddoppiata)."""
    r, path = _router(estimate_divisor=4, estimate_calib_alpha=0.05)
    try:
        u = _u(r)
        for _ in range(400):
            r.note_estimate_error(u, 10000, 20000)      # r = 2
        cur = r._est_div[u]
        assert cur < 4.0
        assert r.estimate_correction(u) > 1.5           # stima gonfiata
        assert abs(cur - 2.0) < 0.05                    # ~ base/r = 2
    finally:
        os.unlink(path)


def test_overestimate_raises_divisor():
    r, path = _router(estimate_divisor=4, estimate_calib_alpha=0.05)
    try:
        u = _u(r)
        for _ in range(400):
            r.note_estimate_error(u, 10000, 5000)       # r = 0.5
        cur = r._est_div[u]
        assert cur > 4.0
        assert r.estimate_correction(u) < 1.0
        assert abs(cur - 4.5) < 0.05                    # target 8 -> clamp 4.5
    finally:
        os.unlink(path)


def test_clamp_bounds():
    r, path = _router(estimate_divisor=4, estimate_calib_alpha=1.0)
    try:
        u = _u(r)
        for _ in range(100):
            r.note_estimate_error(u, 10000, 100000)     # r = 10 (estremo)
        assert 1.5 <= r._est_div[u] <= 4.5
    finally:
        os.unlink(path)


def test_guards_ignore_unreliable_samples():
    r, path = _router()
    try:
        u = _u(r)
        r.note_estimate_error(u, 4000, 8000)            # ctx < 8000
        r.note_estimate_error(u, 10000, 500)            # pt <= 1000
        r.note_estimate_error(u, 10000, 0)
        assert u not in r._est_div
        assert r.estimate_correction(u) == 1.0
    finally:
        os.unlink(path)


def test_dump_load_roundtrip_and_purge():
    r, path = _router()
    try:
        u = _u(r)
        r.note_estimate_error(u, 10000, 20000)
        snap = r.dump_stats()
        assert "est_div" in snap and u in snap["est_div"]
        r2, path2 = _router()
        try:
            r2.load_stats(snap)
            assert r2.estimate_correction(u) == r.estimate_correction(u)
        finally:
            os.unlink(path2)
        # valori fuori range vengono scartati al load
        bad = {"est_div": {u: 99.0}}
        r3, path3 = _router()
        try:
            r3.load_stats(bad)
            assert u not in (r3._est_div or {})
        finally:
            os.unlink(path3)
    finally:
        os.unlink(path)
