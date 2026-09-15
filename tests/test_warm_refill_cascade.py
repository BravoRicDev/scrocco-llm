"""WARM-REFILL a cascata: finche' la sessione ha meno di `warm_ready_min`
caldi che possono EFFETTIVAMENTE consegnare (need + ctx + output assicurato),
ogni richiesta reale lancia gare 2-alla-volta (A + 1 canary) percorrendo il
ladder -dim ASCENDENTE (free only, MAI -go/-fallback). Il perdente non viene
mai cancellato: finisce come PROBE REALE -> se serve, entra in warm; se
sbaglia, cooldown con le solite logiche. Candidati esclusi: api_key gia' in
warm, dep owner di qualsiasi sessione, dep gia' testati/sondati.

`app.main` va importato SOLO dentro funzioni/fixture (convenzione del repo).
"""
import asyncio
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.forwarder import Forwarder, UpstreamError
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"
CONTENT = b'data: {"choices":[{"delta":{"content":"ciao mondo"}}]}\n\n'
FAST = b'data: {"choices":[{"delta":{"content":"VELOCE"}}]}\n\n'
SLOW = b'data: {"choices":[{"delta":{"content":"LENTO"}}]}\n\n'
STOP = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
DONE = b"data: [DONE]\n\n"

CSV_CAP = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,intelligence_score
t@x.com,m/rf-small,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-S,,5
t@x.com,m/rf-mid,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-M,,4
t@x.com,m/rf-big,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-B,,10
t@x.com,m/rf-g1,groq-go,https://api.groq.com/openai/v1,,0,0,5,K-GO,,
t@x.com,m/rf-f1,groq,https://api.groq.com/openai/v1,paid,0,0,5,K-FB,,
"""


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_CAP)
    pol = Policy.from_dict({})
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, pol)
    os.unlink(path)


def _dep(r, gname, key):
    return next(d for d in r.config.groups[gname] if d.get("api_key") == key)


# ------------------------------------------------------- dep_deliverable
def test_dep_deliverable_bordi(router):
    big = _dep(router, f"{BASE}-1000k", "K-B")
    assert router.dep_deliverable(big, None, 0, 4096)
    assert not router.dep_deliverable(big, None, 995_000, 4096)
    # output grande: la finestra non basta + safety 5%
    assert not router.dep_deliverable(big, None, 500_000, 460_000)
    # effort_capable: la riserva di reasoning (30%) rende non-valido
    effort = dict(big)
    effort["effort_capable"] = True
    assert router.dep_deliverable(effort, None, 0, 4096)
    assert not router.dep_deliverable(effort, None, 0, 650_001)
    # max_input 0 = nessun guardo (come _cap_fits)
    assert router.dep_deliverable({"max_input_tokens": 0}, None, 9_999_999,
                                  999_999)


def test_warm_valid_for_counta_solo_deliverabili(router):
    big = _dep(router, f"{BASE}-1000k", "K-B")
    mid = _dep(router, f"{BASE}-200k", "K-M")
    router.note_session_success("rf-sess", big["unique"], 100, ctx_est=100)
    router.note_session_success("rf-sess", mid["unique"], 100, ctx_est=100)
    pool = router.warm_valid_for("rf-sess", "test", f"{BASE}-32k",
                                 frozenset(), 100, 4096)
    assert {d["unique"] for d in pool} == {big["unique"], mid["unique"]}
    # con un budget di output che SOLO il big regge, il mid non conta
    pool2 = router.warm_valid_for("rf-sess", "test", f"{BASE}-32k",
                                  frozenset(), 100, 190_000)
    assert [d["unique"] for d in pool2] == [big["unique"]]


# ------------------------------------------------------- warm_fill_canary
def test_canary_dim_ascendente_e_free_only(router):
    small = _dep(router, f"{BASE}-32k", "K-S")
    mid = _dep(router, f"{BASE}-200k", "K-M")
    c = router.warm_fill_canary("test", small, frozenset(), 100, 4096,
                                tried=set(), requested_group=None,
                                exclude_keys=set(), exclude_uniq=set())
    assert c and c["unique"] == mid["unique"]      # il -dim subito sopra
    # escluso il mid -> il big... ma se lo scartiamo restano solo i PAGATI:
    # la cascata deve fermarsi (None), mai salire su -go/-fallback
    c2 = router.warm_fill_canary("test", small, frozenset(), 100, 4096,
                                 tried=set(), requested_group=None,
                                 exclude_keys=set(),
                                 exclude_uniq={mid["unique"]})
    assert c2 and c2["unique"] == _dep(router, f"{BASE}-1000k",
                                       "K-B")["unique"]
    c3 = router.warm_fill_canary("test", small, frozenset(), 100, 4096,
                                 tried=set(), requested_group=None,
                                 exclude_keys=set(),
                                 exclude_uniq={mid["unique"], c2["unique"]})
    assert c3 is None


def test_canary_esclude_owner_api_key_e_non_deliverabili(router):
    small = _dep(router, f"{BASE}-32k", "K-S")
    mid = _dep(router, f"{BASE}-200k", "K-M")
    big = _dep(router, f"{BASE}-1000k", "K-B")
    # owner vivo DI QUALSIASI SESSIONE: non si tocca
    router.note_session_success("ALTRO", mid["unique"], 100, ctx_est=100)
    c = router.warm_fill_canary("test", small, frozenset(), 100, 4096,
                                tried=set(), requested_group=None,
                                exclude_keys=set(), exclude_uniq=set())
    assert c and c["unique"] == big["unique"]
    # api_key gia' rappresentata in warm (stessa chiave di un caldo)
    router.note_session_success("rf-sess", big["unique"], 100, ctx_est=100)
    wk = router.warm_api_keys("rf-sess", "test", f"{BASE}-32k")
    assert "K-B" in wk
    c2 = router.warm_fill_canary("test", small, frozenset(), 100, 4096,
                                 tried=set(), requested_group=None,
                                 exclude_keys=wk, exclude_uniq=set())
    assert c2 is None                              # mid owner + big key in
    # budget di output fuori scala: niente di consegnabile
    c3 = router.warm_fill_canary("test", small, frozenset(), 100, 250_000,
                                 tried=set(), requested_group=None,
                                 exclude_keys=set(), exclude_uniq=set())
    assert c3 is None                              # solo big potrebbe, ma e'
    # ... owner-altre-sessioni? no: big e' owner sX -> ancora occupato


# ---------------------------------------------------- probe (main, stream)
@pytest.fixture()
def M():
    import app.main as _M
    return _M


def _fake_peel_router(notes):
    from types import SimpleNamespace
    r = SimpleNamespace(
        config=SimpleNamespace(go_suffix="-go",
                               fallback_suffix="-fallback"),
        policy=SimpleNamespace(qc_json=SimpleNamespace(
            watchdog_cooldown_sec=90, stream_total_deadline_ms=180000)),
        stats_for=lambda u: SimpleNamespace(fail_count_24h=0),
        escalate_cooldown=lambda base, f: base,
        note_start=lambda u, ctx=None: notes["start"].append(u),
        note_end=lambda u, ctx=None: notes["end"].append(u),
        is_cooled_down=lambda u: False,
        clear_cooldown=lambda u: None,
        first_content_deadline_ms=lambda u, ctx=None: 5000,
        mark_failed=lambda u, **kw: notes["fail"].append(u),
        note_rate_limit=lambda u, rl: None,
        hedge_canaries=lambda *a, **k: [],
        warm_api_keys=lambda *a, **k: {"K-W"},
        _sess_deps=lambda: {},
        note_warm_owner=lambda sid, u: notes["warm"].append((sid, u)),
    )
    return r


async def _join_probes(M):
    while M._PROBE_TASKS:
        await asyncio.gather(*list(M._PROBE_TASKS), return_exceptions=True)


def test_hedge_refill_gara_a_coppie_e_probe_in_warm(M, monkeypatch):
    """refill=True: anche se A STA gia' fluendo (hold), parte UN solo canary
    (2 in flight); il canary che chiude prima vince e A, finita comunque,
    entra in warm come probe reale."""
    from types import SimpleNamespace
    DEP_A = {"unique": "A__m1__0", "group": "g-32k", "model": "m1",
             "api_key": "K-A"}
    DEP_C = {"unique": "C__m9__9", "group": "g-200k", "model": "m9",
             "api_key": "K-C"}
    notes = {"start": [], "fail": [], "end": [], "warm": []}
    r = _fake_peel_router(notes)
    picked = {}

    def warm_fill(profile, cur, need, ctx, out, **kw):
        picked["exclude_keys"] = set(kw.get("exclude_keys") or ())
        picked["exclude_uniq"] = set(kw.get("exclude_uniq") or ())
        return DEP_C
    r.warm_fill_canary = warm_fill

    async def genA():
        yield SLOW
        await asyncio.sleep(0.3)
        yield STOP

    async def sr(dep, payload, **kw):
        async def gen():
            yield FAST
            yield STOP
        return gen()
    monkeypatch.setattr(M.forwarder, "stream_response", sr)
    monkeypatch.setattr(M, "inject_identity",
                        lambda p, d, router=None: None)

    async def go():
        old = (M.router, M.inject_identity)
        M.router = r
        try:
            raced = {"uniq": {"A__m1__0"}, "keys": {"K-A"}}
            out = await M._hedge_peek(
                dict(DEP_A), genA(), 0.0, 5000, False, 40, 60000, 2048,
                payload={}, profile="test", need=frozenset(), scope="chain",
                ctx=100, tried_set=set(), attempts=[], requested_group=None,
                session="rf-sess", client_ip="", attribution=None,
                hedge_ms=30, _tr_cfg=None, _tct_cfg=None,
                hold=True, refill=True, out_tokens=4096, raced=raced)
            await _join_probes(M)
            return out, raced
        finally:
            M.router, M.inject_identity = old
    (dep, gen, t_att, verdict, prebuf, pending, meta), raced = asyncio.run(go())
    assert verdict == "content" and dep["unique"] == "C__m9__9"
    # il picker ha visto escluse: chiave di A + chiavi warm +uniq corsi
    assert "K-A" in picked["exclude_keys"] and "K-W" in picked["exclude_keys"]
    assert "A__m1__0" in picked["exclude_uniq"]
    assert "C__m9__9" in raced["uniq"] and "K-C" in raced["keys"]
    # A NON cancellata: probe completato con contenuto -> warm
    assert ("rf-sess", "A__m1__0") in notes["warm"]
    assert notes["fail"] == []


# --------------------------------------- loop streaming: refill end-to-end
CSV_LOOP = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,intelligence_score
t@x.com,m/rf-small,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-S,,5
t@x.com,m/rf-mid,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-M,,4
t@x.com,m/rf-big,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-B,,10
t@x.com,m/rf-g1,groq-go,https://api.groq.com/openai/v1,,0,0,5,K-GO,,
t@x.com,m/rf-f1,groq,https://api.groq.com/openai/v1,paid,0,0,5,K-FB,,
"""


@pytest.fixture()
def ML(tmp_path, monkeypatch):
    import app.main as _M
    csv = tmp_path / "k.csv"
    csv.write_text(CSV_LOOP)
    orig_csv = _M.config.csv_path
    qj = _M.router.policy.qc_json
    pol = _M.router.policy
    snap = (qj.stream_hedge_delay_ms, qj.stream_first_content_ms,
            qj.stream_hold_until_finish, pol.warm_refill_enabled,
            pol.warm_ready_min)
    cooled = set(_M.router._cooldown)
    owned = dict(_M.router._dep_last_session)
    sdeps = {k: set(v) for k, v in _M.router._session_deps.items()}
    slok = dict(_M.router._session_last_ok)
    _M.config.csv_path = csv
    _M.config.reload()
    qj.stream_hedge_delay_ms = 50
    qj.stream_first_content_ms = 5000
    qj.stream_hold_until_finish = True
    pol.warm_refill_enabled = True
    pol.warm_ready_min = 3
    yield _M
    (qj.stream_hedge_delay_ms, qj.stream_first_content_ms,
     qj.stream_hold_until_finish, pol.warm_refill_enabled,
     pol.warm_ready_min) = snap
    _M.config.csv_path = orig_csv
    _M.config.reload()
    for k in list(_M.router._cooldown):
        if k not in cooled:
            _M.router._cooldown.pop(k, None)
    _M.router._dep_last_session.clear()
    _M.router._dep_last_session.update(owned)
    _M.router._session_deps.clear()
    _M.router._session_deps.update(sdeps)
    _M.router._session_last_ok.clear()
    _M.router._session_last_ok.update(slok)


def test_streaming_refill_riscalda_il_warm_a_3(ML, monkeypatch):
    """Sessione con UN solo caldo valido (il big/holder): la richiesta parte
    sul small, il refill lancia UN canary (il mid), il piu' veloce consegna,
    e A finisce come probe -> warm a 3. Nessun -go/-fallback chiamato."""
    from app.router import set_current_session
    small = ML.config.groups[f"{BASE}-32k"][0]
    mid = ML.config.groups[f"{BASE}-200k"][0]
    big = ML.config.groups[f"{BASE}-1000k"][0]
    ML.router.note_session_success("rf-sess", big["unique"], 100, ctx_est=100)
    calls = []

    async def sr(dep, payload, **kw):
        calls.append(dep["unique"])
        u = dep["unique"]

        async def gen():
            if u == small["unique"]:
                yield SLOW
                await asyncio.sleep(0.3)
                yield STOP
            else:
                yield FAST
                yield STOP
        return gen()
    monkeypatch.setattr(ML.forwarder, "stream_response", sr)

    async def go():
        set_current_session("rf-sess")
        try:
            payload = {"model": small["model"],
                       "messages": [{"role": "user", "content": "ciao"}]}
            resp = await ML._stream_with_fallback(
                "test", small, payload, scope="chain", session="rf-sess",
                ses="rf-sess", ctx=100)
            body = b""
            if hasattr(resp, "body_iterator"):
                async for c in resp.body_iterator:
                    body += c
            await _join_probes(ML)
            return resp, body
        finally:
            set_current_session(None)
    resp, body = asyncio.run(go())
    assert isinstance(resp, ML.StreamingResponse)
    assert b"VELOCE" in body and b"LENTO" not in body
    # solo A + UN canary: mai i pagati, mai il big (chiave gia' in warm)
    assert sorted(calls) == sorted([small["unique"], mid["unique"]])
    # warm completo: holder + winner + probe
    pool = ML.router.warm_valid_for("rf-sess", "test", f"{BASE}-32k",
                                    frozenset(), 100, 4096)
    assert {d["unique"] for d in pool} == {small["unique"], mid["unique"],
                                           big["unique"]}
    assert small["unique"] not in ML.router._cooldown


# ---------------------------------------------- non-streaming: race 2/2
def _ns_payload():
    return {"model": "m", "messages": [{"role": "user", "content": "ciao"}]}


def _ns_resp(txt):
    return {"choices": [{"message": {"role": "assistant",
                                     "content": txt},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2}}


@pytest.fixture()
def FW():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_LOOP)
    pol = Policy.from_dict({})
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, pol)
    yield r
    os.unlink(path)


async def _join_ns():
    import app.forwarder as _F
    while _F._NS_PROBES:
        await asyncio.gather(*list(_F._NS_PROBES), return_exceptions=True)


def test_nonstream_refill_consegna_la_piu_veloce(FW, monkeypatch):
    import app.forwarder as F
    small = _dep(FW, f"{BASE}-32k", "K-S")
    mid = _dep(FW, f"{BASE}-200k", "K-M")
    calls = []

    async def fake_call(self, dep, payload, **kw):
        calls.append(dep["unique"])
        if dep["unique"] == small["unique"]:
            await asyncio.sleep(0.05)
            return _ns_resp("LENTO")
        return _ns_resp("VELOCE")
    monkeypatch.setattr(F.Forwarder, "call", fake_call)
    fwd = F.Forwarder()

    async def go():
        data, used = await fwd.call_with_fallback(
            FW, "test", small, _ns_payload(), need=frozenset(),
            scope="chain", ctx=100, attempts_box=[], session="rf-sess",
            ses="rf-sess", client_ip="", attribution=None,
            requested_group=None)
        await _join_ns()
        return data, used
    data, used = asyncio.run(go())
    assert used["unique"] == mid["unique"]
    assert data["choices"][0]["message"]["content"] == "VELOCE"
    assert sorted(calls) == sorted([small["unique"], mid["unique"]])
    # A (lenta ma sana) e' finita come probe: in warm, nessuna penale
    ent = FW._dep_last_session.get(small["unique"])
    assert ent and ent[0] == "rf-sess"
    assert small["unique"] not in FW._cooldown


def test_nonstream_refill_errori_vanno_in_cooldown(FW, monkeypatch):
    import app.forwarder as F
    small = _dep(FW, f"{BASE}-32k", "K-S")
    mid = _dep(FW, f"{BASE}-200k", "K-M")
    from app.forwarder import UpstreamError

    async def fake_call(self, dep, payload, **kw):
        raise UpstreamError(503, "boom")
    monkeypatch.setattr(F.Forwarder, "call", fake_call)
    fwd = F.Forwarder()

    async def go():
        try:
            await fwd.call_with_fallback(
                FW, "test", small, _ns_payload(), need=frozenset(),
                scope="chain", ctx=100, attempts_box=[], session="rf-sess",
                ses="rf-sess", client_ip="", attribution=None,
                requested_group=None)
        finally:
            await _join_ns()
    with pytest.raises(UpstreamError):
        asyncio.run(go())
    # il probe fallito va in cooldown COME IL SOLITO tentativo servito
    assert small["unique"] in FW._cooldown
    assert mid["unique"] in FW._cooldown


# ------------------------------------------------ histnorm: testa sporca
def test_scrub_assistant_vuote_anche_in_testa():
    from app.histnorm import normalize_messages
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": None},          # in HEAD
        {"role": "user", "content": "b"},
        {"role": "assistant", "tool_calls": [{"id": "t1",
                                              "type": "function",
                                              "function": {"name": "f",
                                                           "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
        {"role": "assistant", "content": "   "},          # coda vuota
        {"role": "user", "content": "c"},
    ]
    out, rep = normalize_messages(msgs, tail_floor=3)     # tail=[3..]
    assert rep["empty_assistant"] == 2                    # testa + coda
    roles = [(m.get("role"), m.get("tool_calls") is not None) for m in out]
    assert ("assistant", True) in roles                   # intatta
    assert all(not (m.get("role") == "assistant" and not m.get("tool_calls")
                    and not str(m.get("content") or "").strip())
               for m in out)


def test_policy_knob_warm_refill():
    p = Policy.from_dict({"warm_pool": {"refill_enabled": False,
                                        "ready_min": 5,
                                        "refill_default_out_tokens": 1000}})
    assert p.warm_refill_enabled is False
    assert p.warm_ready_min == 5
    assert p.warm_refill_default_out_tokens == 1000
    d = Policy.from_dict({})
    assert d.warm_refill_enabled is True and d.warm_ready_min == 3
