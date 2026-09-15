"""Clamp di max_tokens: input stimato + output richiesto non deve superare il
context window del deployment (altrimenti l'upstream risponde -400/-413)."""
from app.forwarder import clamp_max_tokens
from app.router import estimate_tokens


def _dep(mi=32768):
    return {"unique": "d1", "max_input_tokens": mi}


def _ctx(msgs):
    return estimate_tokens(msgs)


def _safety(mi=32768):
    """Margine di sicurezza 5% della finestra (F16, clamp contestuale)."""
    return int(mi * 0.05)


def test_clamp_reduces_when_room_is_tighter():
    msgs = [{"role": "user", "content": "x" * 4000}]
    body = {"messages": msgs, "max_tokens": 32000}
    clamp_max_tokens(body, _dep())
    assert body["max_tokens"] == 32768 - _ctx(msgs) - _safety()
    assert body["max_tokens"] < 32000


def test_clamp_noop_when_fits():
    body = {"messages": [{"role": "user", "content": "ciao"}],
            "max_tokens": 100}
    clamp_max_tokens(body, _dep())
    assert body["max_tokens"] == 100


def test_clamp_uses_max_completion_tokens():
    msgs = [{"role": "user", "content": "x" * 4000}]
    body = {"messages": msgs, "max_completion_tokens": 50000}
    clamp_max_tokens(body, _dep())
    assert "max_tokens" not in body
    assert body["max_completion_tokens"] == 32768 - _ctx(msgs) - _safety()


def test_clamp_floor_is_one():
    body = {"messages": [{"role": "user", "content": "x" * 200000}],
            "max_tokens": 50000}
    clamp_max_tokens(body, _dep())
    assert body["max_tokens"] == 1


def test_clamp_skips_unknown_max_input():
    body = {"messages": [{"role": "user", "content": "x" * 4000}],
            "max_tokens": 50000}
    clamp_max_tokens(body, {"unique": "d1", "max_input_tokens": 0})
    assert body["max_tokens"] == 50000


def test_clamp_skips_when_no_limit_field():
    body = {"messages": [{"role": "user", "content": "x" * 4000}]}
    clamp_max_tokens(body, _dep())
    assert "max_tokens" not in body


# ------------------------------------------------- F16: riserva reasoning + metric
def _dep_eff(mi=32768):
    return {"unique": "d1", "max_input_tokens": mi, "effort_capable": True}


def test_reasoning_reserve_only_for_effort_capable():
    """Un modello che pensa brucia output: si lascia libero ~30% di finestra."""
    msgs = [{"role": "user", "content": "x" * 4000}]
    ctx = _ctx(msgs)
    b1 = {"messages": msgs, "max_tokens": 32000}
    clamp_max_tokens(b1, _dep())                    # no reasoning
    b2 = {"messages": msgs, "max_tokens": 32000}
    clamp_max_tokens(b2, _dep_eff())                # reasoning: riserva 30%
    assert b2["max_tokens"] < b1["max_tokens"]
    reserve = int(32768 * 0.30)
    assert b1["max_tokens"] == 32768 - ctx - _safety()
    assert b2["max_tokens"] == 32768 - ctx - reserve - _safety()


def test_floor_512_when_room_is_tiny():
    """Con la finestra quasi piena il clamp non scende sotto 512 se lo spazio
    reale lo consente (mai output inutilizzabile)."""
    msgs = [{"role": "user", "content": "x" * 127000}]     # ctx ~31750
    body = {"messages": msgs, "max_tokens": 50000}
    clamp_max_tokens(body, _dep(32768))
    assert body["max_tokens"] >= 512
    assert body["max_tokens"] <= max(1, 32768 - _ctx(msgs))


def test_reasoning_reserve_not_applied_when_no_clamp_needed():
    msgs = [{"role": "user", "content": "ciao"}]
    body = {"messages": msgs, "max_tokens": 100}
    clamp_max_tokens(body, _dep_eff())
    assert body["max_tokens"] == 100


def test_metric_incremented():
    from app import metrics

    seen = []
    old = metrics.inc
    metrics.inc = lambda name, labels=(): seen.append((name, labels))
    try:
        msgs = [{"role": "user", "content": "x" * 4000}]
        clamp_max_tokens({"messages": msgs, "max_tokens": 32000}, _dep())
    finally:
        metrics.inc = old
    assert any(n == "nx_max_tokens_clamped" for n, _ in seen)
