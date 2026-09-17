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


def test_riserva_cedevole_non_affama_output():
    """REGRESSION 2026-09-15: ctx ~80% della finestra su modello
    thinking. La riserva del 30% NON deve piu' clampare a 512 (risposta
    monca: il reasoning si mangiava tutto il budget): se input + richiesta
    entrano nella finestra niente clamp."""
    msgs = [{"role": "user", "content": "x" * 2_800_000}]      # ctx = 700k
    ctx = _ctx(msgs)
    assert ctx == 700_000
    mi = 1_049_000
    room0 = mi - ctx - int(mi * 0.05)
    assert room0 > 32000                                        # c'e' spazio
    body = {"messages": msgs, "max_tokens": 32000}
    clamp_max_tokens(body, _dep_eff(mi))
    assert body["max_tokens"] == 32000                          # nessun clamp


def test_riserva_mangia_solo_il_surplus():
    """La riserva puo' ridursi fino ad azzerarsi, ma mai toccare il
    max_tokens chiesto quando c'e' spazio; quando lo spazio manca il clamp
    va a room0 (senza riserva), come per i modelli non-thinking."""
    msgs = [{"role": "user", "content": "x" * 4000}]            # ctx = 1000
    ctx = _ctx(msgs)
    mi = 100_000
    body = {"messages": msgs, "max_tokens": 95_000}             # > room0
    clamp_max_tokens(body, _dep_eff(mi))
    room0 = mi - ctx - int(mi * 0.05)
    assert body["max_tokens"] == room0          # riserva ceduta, clamp a room0
    assert body["max_tokens"] > mi - ctx - int(mi * 0.30) - int(mi * 0.05)


def test_effort_capable_clamp_uguale_quando_non_c_e_spazio():
    msgs = [{"role": "user", "content": "x" * 4000}]
    b1 = {"messages": msgs, "max_tokens": 32000}
    clamp_max_tokens(b1, _dep())
    b2 = {"messages": msgs, "max_tokens": 32000}
    clamp_max_tokens(b2, _dep_eff())
    assert b1["max_tokens"] == b2["max_tokens"] == 32768 - _ctx(msgs) - _safety()


def test_floor_4096_sopra_la_riserva():
    """Se la riserva affamava tutto (vecchio caso 512), oggi il floor e'
    4096: una risposta utilizabile, non un moncone."""
    msgs = [{"role": "user", "content": "x" * 380_000}]         # ctx = 95k
    body = {"messages": msgs, "max_tokens": 32000}
    clamp_max_tokens(body, _dep_eff(100_000))
    assert body["max_tokens"] == 4096


def test_floor_512_when_room_is_tiny():
    """Con la finestra quasi piena il clamp non scende sotto 512 se lo spazio
    reale lo consente (mai output inutilizzabile)."""
    msgs = [{"role": "user", "content": "x" * 127000}]     # ctx ~31750
    body = {"messages": msgs, "max_tokens": 50000}
    clamp_max_tokens(body, _dep(32768))
    assert body["max_tokens"] >= 512
    assert body["max_tokens"] <= max(1, 32768 - _ctx(msgs))


def test_clamp_non_alza_mai_sopra_la_richiesta():
    # room negativissimo ma cap>mt: il floor 4096 non deve MAI alzare il
    # max_tokens chiesto dal client (vecchio bug: min(cap, max(512, room))
    # su richieste minuscole lo riportava sopra).
    body = {"messages": [{"role": "user", "content": "x" * 384_000}],
            "max_tokens": 100}
    clamp_max_tokens(body, _dep(100_000))       # cap=4000 > 100
    assert body["max_tokens"] == 100


def test_reasoning_reserve_not_applied_when_no_clamp_needed():
    msgs = [{"role": "user", "content": "ciao"}]
    body = {"messages": msgs, "max_tokens": 100}
    clamp_max_tokens(body, _dep_eff())
    assert body["max_tokens"] == 100


def test_hook_chiamata_sul_clamp():
    seen = []
    msgs = [{"role": "user", "content": "x" * 4000}]
    clamp_max_tokens({"messages": msgs, "max_tokens": 32000}, _dep_eff(),
                     hook=lambda old, new: seen.append((old, new)))
    assert seen == [(32000, 32768 - _ctx(msgs) - _safety())]
    seen.clear()
    clamp_max_tokens({"messages": [{"role": "user", "content": "ciao"}],
                      "max_tokens": 100}, _dep_eff(),
                     hook=lambda old, new: seen.append((old, new)))
    assert seen == []


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
