"""Scala unica di rotazione dims (dims_ladder_floor).

Semantica:
  SCALA = [primari dims >= start asc] + [-go] + [-fallback], ceiling max-dim
  - suffisso esplicito -Nk = SOGLIA MINIMA: mai dim < N (neanche ruotando)
  - i bucket -go/-fallback esistono UNA volta sola in coda (della max dim)
  - alias senza suffisso = "0k" completamente automatico
  - richieste -go partono dal tier go (mai primari); -fallback solo fallback
  - gruppi CAP e unique __: fuori dalla scala, intoccati"""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,m/z32,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-Z32,
t@x.com,m/a200,groq,https://api.groq.com/openai/v1,free,200,8000,5,K-A200,
t@x.com,m/b200,groq,https://api.groq.com/openai/v1,free,200,8000,5,K-B200,
t@x.com,m/a400,groq,https://api.groq.com/openai/v1,free,400,8000,5,K-A400,
t@x.com,m/a1000,groq,https://api.groq.com/openai/v1,free,1000,8000,5,K-A1000,
t@x.com,m/g1,groq-go,https://api.groq.com/openai/v1,,0,0,5,K-GO,
t@x.com,m/f1,groq,https://api.groq.com/openai/v1,paid,0,0,5,K-FB,
t@x.com,m/v1,groq,https://api.groq.com/openai/v1,free,128,8000,5,K-VIS,vision
t@x.com,m/v2,groq,https://api.groq.com/openai/v1,free,128,8000,5,K-VIS2,vision
"""

POLICY_MAP = {"capability_routing": {"model_capabilities": {}}}

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


def _dep(router, gname: str, key_hint: str) -> dict:
    return next(d for d in router.config.groups[gname]
                if d.get("api_key") == key_hint)


def _walk_all(router, start_dep):
    """Segue la scala fino a esaurimento; ritorna lista di (gruppo, unique).
    Guardia anti-oscillazione (il ramo cap può alternare i membri)."""
    seq, cur, tried = [], start_dep, {start_dep["unique"]}
    while True:
        nxt = router.fallback_next(PROF, cur, None, scope="group")
        if nxt is None or nxt["unique"] in tried:
            return seq
        seq.append((nxt["group"], nxt["unique"]))
        tried.add(nxt["unique"])
        cur = nxt


def _compress_groups(seq):
    out = []
    for g, _u in seq:
        if not out or out[-1] != g:
            out.append(g)
    return out


def test_group_inventory(router):
    assert sorted(router.config.groups.keys()) == [
        f"{BASE}-1000k", f"{BASE}-200k", f"{BASE}-32k", f"{BASE}-400k",
        f"{BASE}-fallback", f"{BASE}-go", f"{BASE}-vision"]


def test_ladder_full_order_from_200k(router):
    a200 = _dep(router, f"{BASE}-200k", "K-A200")
    seq = _walk_all(router, a200)
    groups = _compress_groups(seq)
    assert groups == [f"{BASE}-200k", f"{BASE}-400k", f"{BASE}-1000k",
                      f"{BASE}-go", f"{BASE}-fallback"]
    # mai la dim sotto soglia, neanche viva
    assert all("z32" not in u for _g, u in seq)
    assert seq[-1][0] == f"{BASE}-fallback"


def test_never_below_floor_even_when_upper_dead(router):
    for key in ("K-A400", "K-A1000", "K-GO", "K-FB"):
        d = next(dd for g in router.config.groups.values() for dd in g
                 if dd.get("api_key") == key)
        router.mark_failed(d["unique"], seconds=600)
    a200 = _dep(router, f"{BASE}-200k", "K-A200")
    seq = _walk_all(router, a200)
    groups = _compress_groups(seq)
    assert all("z32" not in u for _g, u in seq)      # MAI sotto la soglia
    # prima si esauriscono i 200k non in cooldown, poi — ULTIMA SPIAGGIA —
    # la scala ri-prova i tier superiori in cooldown (>= soglia) invece di
    # arrendersi: mai una notice se esiste QUALCOSA da provare.
    assert groups[0] == f"{BASE}-200k"
    assert f"{BASE}-fallback" in groups              # arriva fino in fondo
    assert all(g in (f"{BASE}-200k", f"{BASE}-400k", f"{BASE}-1000k",
                     f"{BASE}-go", f"{BASE}-fallback") for g in groups)


def test_alias_zero_is_fully_automatic(router):
    got_small = router.resolve_group_for_request(BASE, [], None, ctx=10_000)
    got_mid = router.resolve_group_for_request(BASE, [], None, ctx=250_000)
    got_huge = router.resolve_group_for_request(BASE, [], None, ctx=1_500_000)
    assert got_small == f"{BASE}-32k"
    assert got_mid == f"{BASE}-400k"
    assert got_huge == f"{BASE}-1000k"               # ceiling = dim massima


def test_explicit_floor_selects_min_covering(router):
    got = router.resolve_group_for_request(f"{BASE}-200k", [], None, ctx=10_000)
    assert got == f"{BASE}-200k"                     # il minimo della soglia
    got = router.resolve_group_for_request(f"{BASE}-200k", [], None,
                                           ctx=250_000)
    assert got == f"{BASE}-400k"                     # salita per contesto
    got = router.resolve_group_for_request(f"{BASE}-200k", [], None,
                                           ctx=1_500_000)
    assert got == f"{BASE}-1000k"


def test_ctx_growth_logs_transition(router, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="nx.router")
    sid = "ses_L"
    router.resolve_group_for_request(BASE, [], sid, ctx=10_000)
    router.resolve_group_for_request(BASE, [], sid, ctx=250_000)
    msgs = [r for r in caplog.records if "[session]" in r.message]
    assert any(f"{BASE}-32k -> {BASE}-400k" in r.message for r in msgs)


def test_session_map_purged(router):
    sid = "ses_OLD"
    router._session_group[sid] = (f"{BASE}-32k", 0.0)   # epoca = scaduto
    router.purge_expired()
    assert sid not in router._session_group


def test_explicit_go_never_primary(router):
    got = router.resolve_group_for_request(f"{BASE}-go", [], None)
    assert got == f"{BASE}-go"
    g1 = _dep(router, f"{BASE}-go", "K-GO")
    seq = _walk_all(router, g1)
    assert [g for g, _u in seq] == [f"{BASE}-fallback"]
    assert all("m-g1" not in u for _g, u in seq)


def test_explicit_fallback_alone(router):
    got = router.resolve_group_for_request(f"{BASE}-fallback", [], None)
    assert got == f"{BASE}-fallback"
    f1 = _dep(router, f"{BASE}-fallback", "K-FB")
    assert _walk_all(router, f1) == []


def test_cap_explicit_stays_in_bucket(router):
    got = router.resolve_group_for_request(f"{BASE}-vision", [], None)
    assert got == f"{BASE}-vision"
    v1 = _dep(router, f"{BASE}-vision", "K-VIS")
    seq = _walk_all(router, v1)
    assert [g for g, _u in seq] == [f"{BASE}-vision"]    # solo l'altra chiave
    assert all("v2" in u for _g, u in seq)


def test_toggle_off_restores_legacy():
    import copy
    pol = copy.deepcopy(POLICY_MAP)
    pol["capability_routing"]["dims_ladder_floor"] = False
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict(pol))
    try:
        # esplicito = puntamento esatto anche con ctx enorme
        got = r.resolve_group_for_request(f"{BASE}-200k", [], None,
                                          ctx=1_500_000)
        assert got == f"{BASE}-200k"
        # rotazione blindata al gruppo: legacy = last-resort che ignora il
        # cooldown MAI sconfinare (risponde b200 cooled, non sale di dim)
        a200 = _dep(r, f"{BASE}-200k", "K-A200")
        b200 = _dep(r, f"{BASE}-200k", "K-B200")
        r.mark_failed(b200["unique"], seconds=600)
        nxt = r.fallback_next(PROF, a200, None, scope="group")
        assert nxt is not None and nxt["unique"] == b200["unique"]
    finally:
        os.unlink(path)


def test_unknown_dim_suffix_passthrough(router):
    # suffisso -Nk non esistente tra i gruppi: non è esplicito noto ->
    # il percorso legacy risponde None (nessuna invenzione di candidati)
    assert router.resolve_group_for_request(f"{BASE}-5000k", [], None) is None


def test_unique_passthrough(router):
    u = f"{BASE}-200k__m-a200__0"
    assert router.resolve_group_for_request(u, [], None) == u


def test_ghost_group_yields_no_candidates(router):
    ghost = {"unique": "zz", "group": f"{BASE}-999k"}
    assert router.fallback_next(PROF, ghost, None, scope="group") is None
