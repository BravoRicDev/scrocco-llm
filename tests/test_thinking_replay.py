"""`thinking_replay`: replay del reasoning PROATTIVO, non piu' solo reattivo.

Il provider in modalita' thinking (opencode zen "Console Go") pretende il campo
`reasoning_content` sui turni assistant con tool_calls; il client lo droppa.
Prima: 400 -> riparazione reattiva + retry. Ora, se il deployment ha il flag
`thinking_replay` (colonna CSV, scritta in automatico quando un deployment lo
"impara"), il gateway rimette il reasoning (Vero se ha la history originale,
altrimenti un segnaposto) PRIMA del primo invio.

E la scoperta viene PERSISTITA: flag su tutte le righe dello stesso modello
(gemelli con chiavi diverse) nel CSV.
"""
import asyncio
import json
import os
import tempfile
from datetime import date

import httpx
import pytest

from app import csvlearn
from app.config import GatewayConfig, _classify
from app.forwarder import (Forwarder, UpstreamError, apply_thinking_replay,
                           repair_reasoning_replay, restore_reasoning)
from app.policy import Policy
from app.router import Router

RC_400 = ('{"error":{"message":"Error from provider (Console Go): Upstream '
          'request failed: [invalid_request_error] The `reasoning_content` in '
          'the thinking mode must be passed back to the API.","type":'
          '"invalid_request_error","code":"invalid_request_error"}}')

_HDR = ("commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,caps,thinking_replay\n")


def _csv(flag: str) -> str:
    out = _HDR
    for key in ("K1", "K2", "K3"):
        out += (f"a,m1,groq,https://api.groq.com/openai/v1,free,200,200000,5,"
                f"{key},text,{flag}\n")
    out += ("a,m2,groq,https://api.groq.com/openai/v1,free,200,200000,5,"
            "K9,text,\n")
    return out


def _mk(flag: str = ""):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(_csv(flag))
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    router = Router(cfg, pol)
    grp = next(g for g, deps in cfg.groups.items()
               if any(d["api_key"] == "K1" for d in deps))
    dep = next(d for d in cfg.groups[grp] if d["api_key"] == "K1")
    return cfg, router, dep


def _payload():
    return {"model": "x", "messages": [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "42"},
    ]}


def _asst(body):
    return next(m for m in body["messages"] if m.get("role") == "assistant")


# -------------------------------------------------------------- parsing colonna
def test_classify_thinking_replay():
    row = {"commento": "a", "modello": "m1", "provider": "p",
           "endpoint": "https://x/v1", "data": "free", "context": "200",
           "max_input": "0", "priority": "0", "caps": "text"}
    assert _classify(row, date.today())["thinking_replay"] is False
    for v in ("1", "true", "TRUE", "yes", "on"):
        assert _classify({**row, "thinking_replay": v},
                         date.today())["thinking_replay"] is True
    assert _classify({**row, "thinking_replay": "false"},
                     date.today())["thinking_replay"] is False


def test_colonna_finisca_nel_dep_dict():
    cfg, _router, dep = _mk(flag="true")
    assert dep["thinking_replay"] is True
    dep2 = next(d for deps in cfg.groups.values() for d in deps
                if d["api_key"] == "K9")
    assert dep2["thinking_replay"] is False


# ------------------------------------------------------------------- helper
def test_apply_thinking_replay_off_e_on():
    dep_off = {"model": "m1"}
    dep_on = {"model": "m1", "thinking_replay": True}
    body = _payload()
    assert apply_thinking_replay(body, dep_off) == 0
    assert _asst(body).get("reasoning_content") is None
    # senza orig -> segnaposto
    assert apply_thinking_replay(body, dep_on) == 1
    assert _asst(body)["reasoning_content"]
    # con orig -> reasoning VERO
    body2 = _payload()
    orig = [{"role": "assistant", "content": None,
             "tool_calls": [{"id": "t1", "type": "function",
                             "function": {"name": "f", "arguments": "{}"}}],
             "reasoning_content": "ragionamento vero"}]
    assert apply_thinking_replay(body2, dep_on, orig) == 1
    assert _asst(body2)["reasoning_content"] == "ragionamento vero"
    # idempotente
    assert apply_thinking_replay(body2, dep_on, orig) == 0


# ------------------------------------------------- proattivo: non-stream
def test_proattivo_nonstream_primo_invio_gia_riparato():
    cfg, router, dep = _mk(flag="true")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        return await fwd.call_with_fallback(router, "test", dep, _payload())

    data, used = asyncio.run(_run())
    assert data["choices"][0]["message"]["content"] == "ok"
    assert len(seen) == 1                       # nessun 400, nessun retry
    assert _asst(seen[0])["reasoning_content"]


def test_proattivo_nonstream_usa_il_reasoning_vero():
    cfg, router, dep = _mk(flag="true")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    orig = _payload()["messages"]
    orig[1]["reasoning_content"] = "pensiero originale VERO"
    payload = _payload()                        # quello inviato NON lo ha

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        return await fwd.call_with_fallback(router, "test", dep, payload,
                                            orig_messages=orig)

    asyncio.run(_run())
    assert _asst(seen[0])["reasoning_content"] == "pensiero originale VERO"


def test_senza_flag_niente_riparazione():
    cfg, router, dep = _mk(flag="")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        return await fwd.call_with_fallback(router, "test", dep, _payload())

    asyncio.run(_run())
    assert _asst(seen[0]).get("reasoning_content") is None


# ------------------------------------------------- reattivo: impara e persiste
def test_reattivo_impara_scrive_sui_gemelli():
    cfg, router, dep = _mk(flag="")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(400, content=RC_400.encode())
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        data, used = await fwd.call_with_fallback(router, "test", dep,
                                                  _payload())
        await asyncio.gather(*list(csvlearn._TASKS))
        return data, used

    data, used = asyncio.run(_run())
    assert data["choices"][0]["message"]["content"] == "ok"
    assert len(calls) == 2                      # 400 -> repair -> stesso dep
    assert _asst(calls[0]).get("reasoning_content") is None
    assert _asst(calls[1])["reasoning_content"]  # riparato al 2o colpo
    # tutti i gemelli (stesso modello m1, chiavi K1/K2/K3) flaggati in memoria
    twins = [d for deps in router.config.groups.values() for d in deps
             if d["model"] == "m1"]
    assert len(twins) == 3 and all(d["thinking_replay"] for d in twins)
    # e PERSISTITI nel CSV (solo le righe del modello m1)
    with open(cfg.csv_path, newline="", encoding="utf-8") as f:
        txt = f.read()
    lines = [l for l in txt.strip().splitlines()
             if l and not l.startswith("commento")]
    flg = [l for l in lines if l.split(",")[-1] == "true"]
    assert len(flg) == 3
    assert all(l.split(",")[1] == "m1" for l in flg)


def test_persistenza_non_riscrive_due_volte():
    cfg, router, dep = _mk(flag="")
    n1 = csvlearn.learn_thinking_replay(router, "m1")
    assert n1 == 3
    # secondo giro: in memoria nulla da fare, nessuna nuova scrittura
    n2 = csvlearn.learn_thinking_replay(router, "m1")
    assert n2 == 0


# -------------------------------------------- famiglia: rejects / history ---
RC_UNSUPPORTED = ('{"error":{"message":"property \'reasoning_content\' is '
                  'unsupported","type":"invalid_request_error","code":'
                  '"invalid_request_error"}}')
THINK_400 = ('{"error":{"message":"thinking blocks are not allowed in the '
             'current history","type":"invalid_request_error","code":'
             '"invalid_request_error"}}')


def test_repair_reasoning_error_kinds():
    from app.forwarder import (repair_reasoning_error, reasoning_err_kind,
                               strip_reasoning_fields, downgrade_thinking)
    assert reasoning_err_kind(RC_400) == "needs"
    assert reasoning_err_kind(RC_UNSUPPORTED) == "rejects"
    assert reasoning_err_kind(THINK_400) == "history"
    assert reasoning_err_kind("Rate limit exceeded") is None
    # strip
    body = _payload()
    _asst(body)["reasoning_content"] = "rc"
    steps = set()
    assert strip_reasoning_fields(body) == 1
    assert _asst(body).get("reasoning_content") is None
    # downgrade
    body2 = _payload()
    body2["reasoning_effort"] = "medium"
    assert downgrade_thinking(body2) == 1
    assert "reasoning_effort" not in body2
    # helper: un rimedio per dep, mai due volte
    body3 = _payload()
    body3["reasoning_effort"] = "medium"
    assert repair_reasoning_error(body3, THINK_400, {}, steps) == "downgraded"
    assert repair_reasoning_error(body3, THINK_400, {}, steps) is None


def test_no_thinking_blocca_il_reinserimento_dell_effort():
    from app.forwarder import apply_effort_policy
    body = {"reasoning_effort": "medium"}
    apply_effort_policy(body, {"model": "m1", "_no_thinking": True})
    assert "reasoning_effort" not in body


def test_e2e_rejects_strip_e_ritenta_stesso_dep():
    """Cloudflare & co. RIFIUTANO `reasoning_content` nella history: si toglie
    il campo e si ritenta lo STESSO dep (prima si ruotava a vuoto)."""
    cfg, router, dep = _mk(flag="")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(400, content=RC_UNSUPPORTED.encode())
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    payload = _payload()
    _asst(payload)["reasoning_content"] = "vecchio reasoning"

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        return await fwd.call_with_fallback(router, "test", dep, payload,
                                            need=frozenset({"text"}))

    data, used = asyncio.run(_run())
    assert data["choices"][0]["message"]["content"] == "ok"
    assert used["unique"] == dep["unique"]
    assert len(seen) == 2
    assert _asst(seen[0]).get("reasoning_content")
    assert _asst(seen[1]).get("reasoning_content") is None
    assert dep["unique"] not in router._cooldown


def test_e2e_esenzione_esaurita_poi_ko_normale():
    """P0: il rimedio e' esentato solo per `repair_exempt_streak_limit`
    volte; oltre, lo stesso dep prende il KO normale (cooldown) invece di
    rimediare all'infinito."""
    cfg, router, dep = _mk(flag="")
    router.policy.repair_exempt_streak_limit = 1
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return httpx.Response(400, content=RC_UNSUPPORTED.encode())

    payload = _payload()
    _asst(payload)["reasoning_content"] = "vecchio reasoning"

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        return await fwd.call_with_fallback(router, "test", dep, payload,
                                            need=frozenset({"text"}))

    # 1a passata: il primo KO sul dep viene rimediato ('stripped') e
    # ritentato sullo STESSO dep; esaurita l'esenzione (1) -> KO normale.
    with pytest.raises(UpstreamError):
        asyncio.run(_run())
    assert len(seen) <= 3                       # 1 rimedio, mai 2 in un giro
    assert _asst(seen[0]).get("reasoning_content")
    assert _asst(seen[1]).get("reasoning_content") is None    # strip avvenuto
    assert dep["unique"] in router._cooldown    # KO normale applicato
    assert router.repair_exempt_blocked(dep["unique"], 1) is True


def test_e2e_history_downgrade_thinking_stesso_dep():
    cfg, router, dep = _mk(flag="")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(400, content=THINK_400.encode())
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    payload = _payload()
    payload["reasoning_effort"] = "medium"

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        return await fwd.call_with_fallback(router, "test", dep, payload,
                                            need=frozenset({"text"}))

    data, used = asyncio.run(_run())
    assert data["choices"][0]["message"]["content"] == "ok"
    assert used["unique"] == dep["unique"]
    assert len(seen) == 2
    assert seen[0].get("reasoning_effort") == "medium"
    assert "reasoning_effort" not in seen[1]      # thinking disabilitato
    assert dep["unique"] not in router._cooldown
    # il CSV non e' stato sporcato (solo copia locale marcata)
    assert not dep.get("_no_thinking")


def test_probe_impara_il_flag_senza_penale():
    """Il probe (sweep non-stream) scopre che il provider vuole il reasoning:
    nessuna penale sulla chiave e flag `thinking_replay` appreso per il modello."""
    from app import forwarder as F
    cfg, router, dep = _mk(flag="")
    assert dep["thinking_replay"] is False

    async def _boom():
        raise F.UpstreamError(-400, RC_400)

    async def _run():
        fut = asyncio.ensure_future(_boom())
        F._spawn_ns_probe(router, dep, fut, 0.0, 100, "sess")
        await asyncio.gather(*list(F._NS_PROBES), return_exceptions=True)

    asyncio.run(_run())
    twins = [d for deps in router.config.groups.values() for d in deps
             if d["model"] == "m1"]
    assert all(d["thinking_replay"] for d in twins)
    assert dep["unique"] not in router._cooldown


# ===================== flag strip_reasoning / no_thinking (persistiti) ======
_HDR2 = ("commento,modello,provider,endpoint,data,context,max_input,"
         "priority,scrocco-llm-test,caps,thinking_replay,strip_reasoning,"
         "no_thinking\n")


def _csv2(strip: str = "", nothink: str = "", model: str = "m9") -> str:
    out = _HDR2
    for key in ("K1", "K2", "K3"):
        out += (f"a,{model},groq,https://api.groq.com/openai/v1,free,200,200000,5,"
                f"{key},text,,{strip},{nothink}\n")
    return out


def _mk2(strip: str = "", nothink: str = ""):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(_csv2(strip, nothink))
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    router = Router(cfg, pol)
    grp = next(g for g, deps in cfg.groups.items()
               if any(d["api_key"] == "K1" for d in deps))
    dep = next(d for d in cfg.groups[grp] if d["api_key"] == "K1")
    return cfg, router, dep


def test_parsing_colonne_strip_e_no_thinking():
    cfg, router, dep = _mk2(strip="true", nothink="1")
    assert dep["strip_reasoning"] is True
    assert dep["no_thinking"] is True
    cfg0, _, dep0 = _mk2()
    assert dep0["strip_reasoning"] is False
    assert dep0["no_thinking"] is False


def test_proattivo_strip_reasoning_toglie_i_campi():
    """Con `strip_reasoning` nel CSV la richiesta parte GIA' senza i campi
    reasoning (il provider li rifiuta): nessun 400 al primo invio."""
    _, _, dep = _mk2(strip="true")
    body = {"messages": [
        {"role": "assistant", "content": "x", "reasoning_content": "penso"},
        {"role": "user", "content": "y"}]}
    n = apply_thinking_replay(body, dep)
    assert n == 1
    assert "reasoning_content" not in body["messages"][0]


def test_no_thinking_dal_csv_blocca_effort():
    from app.forwarder import apply_effort_policy
    _, _, dep = _mk2(nothink="true")
    body = {"reasoning_effort": "medium", "thinking": {"type": "enabled"}}
    apply_effort_policy(body, dep)
    assert "reasoning_effort" not in body
    assert "thinking" not in body


def test_learn_strip_e_no_thinking_scrive_il_csv():
    csvlearn._PERSISTED.clear()          # dedup globale: non ereditare test
    cfg, router, dep = _mk2()

    async def _go():
        n = csvlearn.learn_strip_reasoning(router, "m9")
        await asyncio.gather(*list(csvlearn._TASKS))
        return n

    assert asyncio.run(_go()) == 3
    twins = [d for deps in router.config.groups.values() for d in deps
             if d["model"] == "m9"]
    assert all(d["strip_reasoning"] for d in twins)
    import csv as _csvmod
    with open(cfg.csv_path, newline="", encoding="utf-8") as f:
        rows = list(_csvmod.DictReader(f))
    assert len(rows) == 3
    assert all(r["strip_reasoning"] == "true" for r in rows)
    # idempotente: secondo giro non riscrive
    assert csvlearn.learn_strip_reasoning(router, "m9") == 0

    cfg2, router2, _ = _mk2()

    async def _go2():
        n = csvlearn.learn_no_thinking(router2, "m9")
        await asyncio.gather(*list(csvlearn._TASKS))
        return n

    assert asyncio.run(_go2()) == 3
    twins2 = [d for deps in router2.config.groups.values() for d in deps
              if d["model"] == "m9"]
    assert all(d["no_thinking"] for d in twins2)
    with open(cfg2.csv_path, newline="", encoding="utf-8") as f:
        rows2 = list(_csvmod.DictReader(f))
    assert all(r["no_thinking"] == "true" for r in rows2)
