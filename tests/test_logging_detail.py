"""Logging super dettagliato: transizioni complete + decisioni router visibili.

Copre i punti A1-A4 (transizioni hot-word/caps/degrade/ladder-escape),
B5 [defer], B8 escalation, D14 identity+ema, F18 auth reasons.
Il tag [summary] è integration-covered (emesso nei path risposta)."""
import logging
import os
import tempfile

import pytest

from app.auth import AuthManager
from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router, inject_identity

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,m/t32,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-T32,
t@x.com,m/t200a,groq,https://api.groq.com/openai/v1,free,200,8000,0,K-TA,
t@x.com,m/t200b,groq,https://api.groq.com/openai/v1,free,200,8000,0,K-TB,
t@x.com,m/v400,groq,https://api.groq.com/openai/v1,free,400,8000,0,K-V4,"text,vision"
t@x.com,m/fb,groq,https://api.groq.com/openai/v1,paid,128,8000,0,K-FB,
"""

POLICY_MAP = {
    "hotwords": ["ragiona profondamente"],
    "capability_groups": {"enabled": True},
    "capability_routing": {"model_capabilities": {}},
}

BASE = "scrocco-llm-test"
PROF = "test"


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    pol = Policy.from_dict(POLICY_MAP)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, pol)
    os.unlink(path)


def _dep(router, gname, key_hint):
    return next(d for d in router.config.groups[gname]
                if d.get("api_key") == key_hint)


def test_hot_word_transition_logged(router, caplog):
    caplog.set_level(logging.INFO, logger="nx.router")
    sid = "ses_hw"
    router.resolve_group_for_request(BASE, [{"role": "user", "content": "ciao"}],
                                     sid, ctx=10_000)
    router.resolve_group_for_request(
        BASE, [{"role": "user", "content": "ragiona profondamente"}],
        sid, ctx=10_000)
    msgs = [r.message for r in caplog.records if "[session]" in r.message]
    assert any(f"{BASE}-32k -> {BASE}-400k" in m for m in msgs)


def test_caps_dispatch_transition_logged(router, caplog):
    caplog.set_level(logging.INFO, logger="nx.router")
    sid = "ses_cap"
    g_vision = router.resolve_group_for_request(
        BASE, [{"role": "user", "content": [
            {"type": "text", "text": "cosa vedi"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]}],
        sid, need=frozenset({"vision", "text"}))
    assert g_vision == f"{BASE}-vision"
    router.resolve_group_for_request(BASE, [{"role": "user", "content": "ciao"}],
                                     sid, ctx=10_000)
    msgs = [r.message for r in caplog.records if "[session]" in r.message]
    assert any(f"{BASE}-vision -> {BASE}-32k" in m for m in msgs)


def test_defer_info_first_then_debug(router, caplog):
    caplog.set_level(logging.DEBUG, logger="nx.router")
    # pool misto nel -32k? no: serve vision NELLO STESSO gruppo dims ->
    # il fixture ha v400 in -400k: usiamo -400k come pool misto? anche lì
    # v400 è solo vision... simuliamo direttamente _defer_media:
    pool = [_dep(router, f"{BASE}-32k", "K-T32"),
            {"unique": "fake-v", "model": "m/vfake", "group": f"{BASE}-32k",
             "caps": frozenset({"vision"})}]
    infos = lambda: [r for r in caplog.records
                     if "[defer]" in r.message and r.levelno == logging.INFO]
    p1, d1 = router._defer_media(f"{BASE}-32k", frozenset({"text"}), list(pool))
    assert d1 and len(infos()) == 1                    # primo: INFO
    p2, d2 = router._defer_media(f"{BASE}-32k", frozenset({"text"}), list(pool))
    assert d2 and len(infos()) == 1                    # burst: niente INFO nuovo
    # pool torna tutto-text-only -> reset con INFO di riapertura
    router._defer_media(f"{BASE}-32k", frozenset({"text"}), [pool[0]])
    assert len(infos()) == 2


def test_cooldown_escalation_visible(router, caplog):
    caplog.set_level(logging.WARNING, logger="nx.router")
    # Forzarizza il mode escalation esponenziale per questo test
    from app.policy import Policy
    pol = Policy()
    pol.cooldown_mode = "exponential"
    router.policy = pol
    u = f"{BASE}-32k__m-t32__0"
    router.mark_failed(u)                              # streak=1 -> 10s
    router.mark_failed(u)                              # streak=2 -> escalation (esponenziale)
    warns = [r.message for r in caplog.records if "[cooldown]" in r.message]
    assert len(warns) == 2
    assert "escalation" not in warns[0]
    assert "escalation" in warns[1]


def test_identity_includes_ema(router, caplog):
    caplog.set_level(logging.INFO, logger="nx.router")
    dep = dict(_dep(router, f"{BASE}-32k", "K-T32"))
    router.note_result(dep["unique"], 250.0)
    data = {"messages": [{"role": "user", "content": "ciao"}]}
    inject_identity(data, dep, router=router)
    assert any("ema=250ms" in r.message for r in caplog.records
               if "[identity]" in r.message)


def test_initial_pick_ladder_escape_logged(router, caplog):
    caplog.set_level(logging.INFO, logger="nx.router")
    # -200k non ha nessuno capace vision; la scala trova v400 in -400k
    dep = router.initial_pick(PROF, f"{BASE}-200k",
                              frozenset({"vision", "text"}))
    assert dep is not None and dep["group"] == f"{BASE}-400k"
    assert any("[ladder] initial_pick" in r.message for r in caplog.records)


# ------------------------------------------------------------------ auth F18

def test_auth_reasons():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    am = AuthManager(cfg, master_key="sk-master-test")
    try:
        # branch diretti: verifichiamo i MOTIVI via messaggio
        res_bad = am.authenticate("Bearer sk-profiloinesistente")
        assert not res_bad.ok and "Invalid api key" in res_bad.error
        res_fmt = am.authenticate("Bearer chiave-senza-sk")
        assert not res_fmt.ok
        res_none = am.authenticate(None)
        assert not res_none.ok and "No api key" in res_none.error
        # override attivo -> la deterministica del profilo viene disattivata
        am._client_keys = lambda: {"test": "sk-custom-key"}
        res_ov = am.authenticate("Bearer sk-test")
        assert not res_ov.ok
        res_ok = am.authenticate("Bearer sk-custom-key")
        assert res_ok.ok and res_ok.profile == "test"
    finally:
        os.unlink(path)


def test_auth_negata_log_has_reason(router, caplog):
    caplog.set_level(logging.WARNING)
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    am = AuthManager(cfg, master_key="sk-master-test")
    try:
        am.authenticate("Bearer sk-nonesiste")
        negs = [r.message for r in caplog.records
                if "[auth] NEGATA" in r.message]
        assert negs and "motivo=" in negs[-1]
    finally:
        os.unlink(path)
