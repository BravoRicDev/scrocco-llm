"""GARA LENTA (45s), gate warm<6 e ordine della WARM.

- soglia lenta di default a 45s (stream e non-stream);
- il canary lento si apre SOLO se la sessione ha < `slow_race_max_warm` warm
  NON LENTI validi (prestati inclusi), altrimenti niente canario (metric
  `warm_full`); un pool di soli lenti NON blocca il canario;
- al trigger il dep che sta ancora generando viene FLAGGATO "lento per la
  sessione" subito (anche se poi vince la gara);
- i flaggati restano in WARM ma vanno nella terza fascia: l'ordine e' tre
  blocchi (propri non lenti > prestati non lenti > lenti comuni), decisi dal
  SOLO flag del timer >45s, e dentro ogni blocco vale l'EMA di latenza.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time

import pytest

import app.main as M
from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

_HDR = ("commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,caps\n")
_GOOD = "a,good,groq,https://ok.test/v1,free,128,200000,5,K-G,text\n"
_BROKEN = "a,broken,cloudflare,https://cf.test/v1,free,128,200000,9,K-B,text\n"

CSV_CAP = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,intelligence_score
t@x.com,m/rf-small,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-S,,5
t@x.com,m/rf-mid,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-M,,4
t@x.com,m/rf-big,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-B,,10
"""


# ------------------------------------------------------------------ policy
def test_policy_defaults_slow_race():
    p = Policy.from_dict({})
    assert p.stream_slow_race_after_ms == 45000
    assert p.nonstream_slow_race_after_ms == 45000
    assert p.slow_race_max_warm == 6
    assert p.warm_pick_fastest is True


def test_policy_parsing_slow_race():
    p = Policy.from_dict({"warm_pool": {
        "slow_race_after_ms": 30000,
        "nonstream_slow_race_after_ms": 31000,
        "slow_race_max_warm": 4,
        "warm_pick_fastest": False}})
    assert p.stream_slow_race_after_ms == 30000
    assert p.nonstream_slow_race_after_ms == 31000
    assert p.slow_race_max_warm == 4
    assert p.warm_pick_fastest is False


# --------------------------------------------------------------- _warm_pool
@pytest.fixture()
def wr():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_CAP)
    pol = Policy.from_dict({})
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, pol)
    yield r
    os.unlink(path)


def _dep(r, gname, key):
    return next(d for d in r.config.groups[gname] if d.get("api_key") == key)


def _warm3(r):
    s = _dep(r, f"{BASE}-32k", "K-S")
    m = _dep(r, f"{BASE}-200k", "K-M")
    b = _dep(r, f"{BASE}-1000k", "K-B")
    r.note_session_success("s1", s["unique"], 100, ctx_est=100)   # holder
    r.note_warm_owner("s1", m["unique"])
    r.note_warm_owner("s1", b["unique"])
    r._avg_latencies[s["unique"]] = 5000.0    # holder ma lento
    r._avg_latencies[b["unique"]] = 100.0     # il piu' veloce
    r._avg_latencies[m["unique"]] = 900.0
    return s, m, b


def test_warm_pool_holder_primo_poi_piu_veloce(wr):
    s, m, b = _warm3(wr)
    pool = wr._warm_pool("s1", None, None, 100)
    # holder (s) primo NONOSTANTE sia il piu' lento; poi per latenza asc
    assert [d["unique"] for d in pool] == [s["unique"], b["unique"],
                                           m["unique"]]


def test_warm_pool_flaggato_in_fondo_ma_non_rimosso(wr):
    s, m, b = _warm3(wr)
    wr.mark_session_slow("s1", s["unique"])
    pool = wr._warm_pool("s1", None, None, 100)
    uniqs = [d["unique"] for d in pool]
    assert s["unique"] in uniqs, "il flaggato RESTA in warm"
    assert uniqs[-1] == s["unique"], "il flaggato va in fondo"
    assert uniqs[0] == b["unique"], "serve il piu' veloce fra i capaci"
    assert wr.is_slow_for_session(s["unique"], "s1", 100) is True


def test_warm_pool_log_conta_propri_prestabili(wr, caplog):
    """Il log distingue i 'propri' (ownership, ts rinfrescato) dai 'propri
    gia' prestabili' (deployment fermo da >= borrow_idle_sec)."""
    s, m, b = _warm3(wr)
    # s e' un PROPRIO ma il deployment e' fermo da molto: prestabile
    wr.stats_for(s["unique"]).last_used = time.time() - 100000
    with caplog.at_level(logging.INFO, logger="nx.router"):
        wr._warm_pool("s1", None, None, 100)
    lines = [r.getMessage() for r in caplog.records
             if "[warm] pool" in r.getMessage()]
    assert lines, "deve loggare la composizione del pool"
    assert "propri 3" in lines[-1]
    assert "di cui prestabili 1" in lines[-1]


def test_marchio_pulito_da_successo_rapido(wr):
    s, _m, _b = _warm3(wr)
    wr.mark_session_slow("s1", s["unique"])
    assert wr.is_slow_for_session(s["unique"], "s1", 100)
    wr.note_session_success("s1", s["unique"], 50, ctx_est=100)
    assert not wr.is_slow_for_session(s["unique"], "s1", 100)


def test_slow_flag_loggato(wr, caplog):
    s = _dep(wr, f"{BASE}-32k", "K-S")
    with caplog.at_level(logging.INFO, logger="nx.router"):
        wr.mark_session_slow("s1", s["unique"])
    assert any("[slow-flag]" in r.getMessage()
               and "NESSUN cooldown" in r.getMessage()
               for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="nx.router"):
        wr.mark_session_slow("s1", s["unique"])      # gia' flaggato
    assert not any("[slow-flag]" in r.getMessage() for r in caplog.records)


def test_slow_flag_clear_loggato(wr, caplog):
    s = _dep(wr, f"{BASE}-32k", "K-S")
    wr.mark_session_slow("s1", s["unique"])
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="nx.router"):
        wr.note_session_success("s1", s["unique"], 50, ctx_est=100)
    assert any("NON piu' lento" in r.getMessage() for r in caplog.records)


def test_slow_race_ms(wr):
    assert wr._slow_race_ms() == 45000
    wr.policy.stream_slow_race_after_ms = 30000
    wr.policy.nonstream_slow_race_after_ms = 50000
    assert wr._slow_race_ms() == 30000          # min dei valori positivi
    wr.policy.stream_slow_race_after_ms = 0
    wr.policy.nonstream_slow_race_after_ms = 0
    assert wr._slow_race_ms() == 45000          # fallback


def test_marchio_non_pulito_da_successo_lento(wr, caplog):
    """Un dep marcato lento dal timer resta marcato anche se il successivo
    successo e' "normale per la sua baseline" (107s): la regola relativa F1
    da sola non deve riabilitarlo."""
    s = _dep(wr, f"{BASE}-32k", "K-S")
    wr.mark_session_slow("s1", s["unique"])
    with caplog.at_level(logging.INFO, logger="nx.router"):
        wr.note_session_success("s1", s["unique"], 107000, ctx_est=100)
    assert wr.is_slow_for_session(s["unique"], "s1", 100) is True
    assert not any("NON piu' lento" in r.getMessage() for r in caplog.records)


def test_warm_pool_lenti_comuni_piu_veloce_prima(wr):
    s, m, b = _warm3(wr)
    wr.mark_session_slow("s1", s["unique"])     # lento (EMA 5000ms)
    wr.mark_session_slow("s1", b["unique"])     # lento (EMA 100ms)
    pool = wr._warm_pool("s1", None, None, 100)
    # blocco 3 = lenti comuni, dal piu' veloce: b prima di s; m (non lento) primo
    assert [d["unique"] for d in pool] == [m["unique"], b["unique"],
                                           s["unique"]]


def test_warm_pool_knob_off_ordine_legacy(wr):
    s, m, b = _warm3(wr)
    wr.policy.warm_pick_fastest = False
    wr.stats_for(m["unique"]).last_used = 1.0
    wr.stats_for(b["unique"]).last_used = 9.0
    pool = wr._warm_pool("s1", None, None, 100)
    # holder, poi MRU (ignora la latenza): b prima di m
    assert [d["unique"] for d in pool] == [s["unique"], b["unique"],
                                           m["unique"]]


# ------------------------------------------------------- gate warm < cap
def test_slow_race_allowed_gate(wr):
    wr.policy.slow_race_max_warm = 2
    assert wr.slow_race_allowed("s1", "test", f"{BASE}-32k",
                                frozenset(), 100) is True
    wr.note_warm_owner("s1", _dep(wr, f"{BASE}-200k", "K-M")["unique"])
    wr.note_warm_owner("s1", _dep(wr, f"{BASE}-1000k", "K-B")["unique"])
    assert wr.slow_race_allowed("s1", "test", f"{BASE}-32k",
                                frozenset(), 100) is False
    wr.policy.slow_race_max_warm = 0          # 0 = nessun gate
    assert wr.slow_race_allowed("s1", "test", f"{BASE}-32k",
                                frozenset(), 100) is True


def test_slow_race_allowed_rispetta_il_budget_output(wr):
    """Il gate conta solo i CAPACI: un caldo che non puo' consegnare
    l'output richiesto NON e' 'pronto' (prima lo era: out_tokens=None)."""
    m = _dep(wr, f"{BASE}-200k", "K-M")          # max_input 200000
    wr.note_warm_owner("s1", m["unique"])
    wr.policy.slow_race_max_warm = 1
    # ctx 190k + out 32k -> room = 200000-190000-10000 = 0 -> NON capace
    assert wr.slow_race_allowed("s1", "test", f"{BASE}-200k", frozenset(),
                                190000, 32000) is True
    # ctx basso: ci stanno ctx e output -> capace -> gate chiuso
    assert wr.slow_race_allowed("s1", "test", f"{BASE}-200k", frozenset(),
                                100000, 32000) is False


def test_slow_race_allowed_non_conta_prestiti_non_selezionabili(wr):
    """I prestati si contano solo se sono anche SELEZIONABILI (come nel pick)."""
    a = _dep(wr, f"{BASE}-200k", "K-M")
    b = _dep(wr, f"{BASE}-1000k", "K-B")
    wr.note_warm_owner("s2", a["unique"])        # di un'ALTRA sessione
    wr.note_warm_owner("s2", b["unique"])
    for u in (a["unique"], b["unique"]):
        wr.stats_for(u).last_used = time.time() - 100000
    wr.policy.slow_race_max_warm = 2
    wr.policy.warm_borrow_enabled = True
    wr.policy.warm_borrow_selectable = True
    assert wr.slow_race_allowed("s1", "test", f"{BASE}-200k",
                                frozenset(), 100, 1000) is False
    wr.policy.warm_borrow_selectable = False     # solo conteggio, non si usa
    assert wr.slow_race_allowed("s1", "test", f"{BASE}-200k",
                                frozenset(), 100, 1000) is True


def test_slow_race_allowed_conta_solo_i_non_lenti(wr):
    """Un pool fatto di soli LENTI non chiude il gate: il canario parte e i
    lenti si lasciano esaurire (nessuna penalita')."""
    m = _dep(wr, f"{BASE}-200k", "K-M")
    wr.note_warm_owner("s1", m["unique"])
    wr.policy.slow_race_max_warm = 1
    assert wr.slow_race_allowed("s1", "test", f"{BASE}-200k",
                                frozenset(), 100) is False
    wr.mark_session_slow("s1", m["unique"])     # ora l'unico warm e' lento
    assert wr.slow_race_allowed("s1", "test", f"{BASE}-200k",
                                frozenset(), 100) is True


# ----------------------------------------------------- streaming (caller)
def _mk(csv_text, *, slow_ms=100, hedge_base=50):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(csv_text)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    pol.cooldown_jitter_ratio = 0
    pol.warm_refill_enabled = False
    pol.qc_json.stream_hold_until_finish = True
    pol.qc_json.stream_commit_min_chars = 4
    pol.qc_json.stream_hedge_delay_ms = hedge_base
    pol.stream_slow_race_after_ms = slow_ms
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    router = Router(cfg, pol)
    os.unlink(path)
    by_key = {}
    for deps in cfg.groups.values():
        for d in deps:
            by_key[d["api_key"]] = d
    return cfg, router, by_key


def _chunk(obj):
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _content_of(raw) -> str:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    txt = ""
    for line in raw.split("\n"):
        s = line.strip()
        if not s.startswith("data:"):
            continue
        body = s[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            obj = json.loads(body)
        except Exception:
            continue
        for ch in (obj.get("choices") or []):
            c = (ch.get("delta") or {}).get("content")
            if isinstance(c, str):
                txt += c
    return txt


class _Fwd:
    def __init__(self, slow_unique, slow_sleep=0.3):
        self.slow = slow_unique
        self.slow_sleep = slow_sleep
        self.calls = []

    async def stream_response(self, d, payload, **kw):
        self.calls.append(d["unique"])
        if d["unique"] == self.slow:
            async def _g():
                yield _chunk({"choices": [
                    {"index": 0, "delta": {"content": "sto arrivando"},
                     "finish_reason": None}]})
                await asyncio.sleep(self.slow_sleep)
                yield _chunk({"choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}]})
                yield b"data: [DONE]\n\n"
            return _g()

        async def _g2():
            yield _chunk({"choices": [
                {"index": 0, "delta": {"content": "ok"},
                 "finish_reason": None}]})
            yield _chunk({"choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}]})
            yield b"data: [DONE]\n\n"
        return _g2()


_PAYLOAD = {"model": "x", "messages": [{"role": "user", "content": "ciao"}]}


def _stream(monkeypatch, cfg, router, fwd, first, payload):
    monkeypatch.setattr(M, "router", router)
    monkeypatch.setattr(M, "config", cfg)
    monkeypatch.setattr(M, "forwarder", fwd)

    async def _run():
        resp = await M._stream_with_fallback(
            "test", first, payload, need=frozenset({"text"}), scope="chain")
        out = b""
        async for chunk in resp.body_iterator:
            out += chunk if isinstance(chunk, bytes) else chunk.encode()
        return out

    return asyncio.run(_run())


def test_stream_slow_race_gate_warm_pieno(monkeypatch, caplog):
    """Con il gate chiuso: nessun canario, ma il dep lento viene flaggato."""
    cfg, router, by_key = _mk(_HDR + _BROKEN + _GOOD, slow_ms=100)
    broken, good = by_key["K-B"], by_key["K-G"]
    monkeypatch.setattr(router, "slow_race_allowed", lambda *a, **k: False)
    marked = []
    monkeypatch.setattr(router, "mark_session_slow",
                        lambda sid, u: marked.append(u))
    fwd = _Fwd(broken["unique"])
    with caplog.at_level(logging.INFO, logger="nx.main"):
        out = _stream(monkeypatch, cfg, router, fwd, broken, _PAYLOAD)
    assert good["unique"] not in fwd.calls, "gate chiuso: niente canario"
    assert marked and marked[0] == broken["unique"], "flag immediato"
    assert any("warm gia' pieno" in r.getMessage() for r in caplog.records)
    assert _content_of(out) == "sto arrivando"
    assert router.is_cooled_down(broken["unique"]) is False


# ---------------------------------------------------- non-streaming (caller)
def _ns_payload():
    return {"model": "m", "messages": [{"role": "user", "content": "ciao"}]}


def _ns_resp(txt):
    return {"choices": [{"message": {"role": "assistant", "content": txt},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2}}


async def _join_ns():
    import app.forwarder as _F
    while _F._NS_PROBES:
        await asyncio.gather(*list(_F._NS_PROBES), return_exceptions=True)


def test_nonstream_gate_warm_pieno(wr, monkeypatch):
    import app.forwarder as F
    small = _dep(wr, f"{BASE}-32k", "K-S")
    wr.policy.warm_refill_enabled = False
    wr.policy.nonstream_slow_race_after_ms = 100
    monkeypatch.setattr(wr, "slow_race_allowed", lambda *a, **k: False)
    marked = []
    monkeypatch.setattr(wr, "mark_session_slow",
                        lambda sid, u: marked.append(u))
    calls = []

    async def fake_call(self, dep, payload, **kw):
        calls.append(dep["unique"])
        await asyncio.sleep(0.6)
        return _ns_resp("LENTO")
    monkeypatch.setattr(F.Forwarder, "call", fake_call)
    fwd = F.Forwarder()

    async def go():
        data, used = await fwd.call_with_fallback(
            wr, "test", small, _ns_payload(), need=frozenset(),
            scope="chain", ctx=100, attempts_box=[], session="rf-sess",
            ses="rf-sess", client_ip="", attribution=None,
            requested_group=None)
        await _join_ns()
        return data, used
    data, _used = asyncio.run(go())
    assert calls == [small["unique"]], "gate chiuso: niente canario"
    assert marked == [small["unique"]], "flag immediato del lento"
    assert data["choices"][0]["message"]["content"] == "LENTO"
    assert wr.is_cooled_down(small["unique"]) is False
