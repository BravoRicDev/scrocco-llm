"""Colonna `alias`: nomi richiamabili che riuniscono piu' righe.

Un alias (lista separata da virgola, stile `caps`) rende le righe
richiamabili con `model=<alias>`. Il gateway costruisce un gruppo-alias
primario/-go/-fallback usando SOLO le righe con quell'alias, cosi' valgono
invariati cooldown, fair-share, sticky e la catena di fallback gia'
collaudata. Un alias che unisce modelli upstream DIVERSI (es. `gemini` su
zen e antigravity) e' il caso principale.
"""
from __future__ import annotations

import os
import tempfile

from app.config import GatewayConfig, parse_alias

_HEADER = ("commento,modello,provider,endpoint,data,context,max_input,"
           "priority,scrocco-llm-t,caps,alias\n")


def _cfg(rows: list[str]) -> GatewayConfig:
    d = tempfile.mkdtemp()
    p = os.path.join(d, "k.csv")
    with open(p, "w", encoding="utf-8") as f:
        f.write(_HEADER + "".join(rows))
    return GatewayConfig(p)


# ---------------------------------------------------------- parsing/validaz.
def test_parse_alias_normalizza_e_dedup():
    assert parse_alias(" Gemini , gemini ,WHISPER-GPU ") == ("gemini",
                                                              "whisper-gpu")
    assert parse_alias("a_b c") == ("a-b-c",)
    assert parse_alias("") == ()
    assert parse_alias(None) == ()
    assert parse_alias(",,,") == ()


def test_colonna_alias_assente_non_crea_gruppi():
    c = _cfg(['a@x,m,opencode-zen,https://opencode.ai/zen/v1,free,200,,1,'
              'sk1,"text",\n'])
    assert c.alias_groups == {}
    assert c.alias_names() == []


# ------------------------------------------------------ costruzione gruppi
def test_alias_unifica_modelli_diversi_con_catena_free_go():
    c = _cfg([
        'a@x,gemini-3-flash,opencode-zen,https://opencode.ai/zen/v1,free,'
        '200,,1,sk1,"text",gemini\n',
        'b@x,gemini-3-pro,opencode-zen,https://opencode.ai/zen/v1,free,'
        '200,,1,sk2,"text",gemini\n',
        'c@x,gemini-3-pro,antigravity,https://ag/v1,15,1000,,1,sk3,"text",'
        'gemini\n',
    ])
    gs = c.alias_groups["gemini"]
    assert "scrocco-llm-t-gemini" in gs
    assert "scrocco-llm-t-gemini-go" in gs
    prim = c.groups["scrocco-llm-t-gemini"]
    go = c.groups["scrocco-llm-t-gemini-go"]
    assert len(prim) == 2 and len(go) == 1
    # i dep del gruppo-alias sono le STESSE istanze dei gruppi normali
    assert all(d["_category"] in ("priority", "zen") for d in prim)
    assert go[0]["_category"] == "go"


def test_alias_singolo_isola_la_riga():
    c = _cfg([
        'd@x,whisper-large-v3-turbo,speaches,http://speaches-gpu:8000/v1,'
        'free,128,,5,sk4,"stt",whisper-gpu\n',
        'e@x,whisper-large-v3-turbo,groq,https://api.groq.com/openai/v1,'
        'free,128,,5,sk5,"stt",\n',
    ])
    g = c.alias_target("whisper-gpu", "stt")
    assert g == "scrocco-llm-t-whisper-gpu-stt"
    deps = c.groups[g]
    assert len(deps) == 1
    assert deps[0]["api_base"] == "http://speaches-gpu:8000/v1"
    # la riga Groq (senza alias) NON partecipa
    assert all(d["api_base"] != "https://api.groq.com/openai/v1" for d in deps)


def test_alias_gruppo_ha_la_cap_corretta():
    c = _cfg([
        'd@x,whisper-large-v3-turbo,speaches,http://speaches-gpu:8000/v1,'
        'free,128,,5,sk4,"stt",whisper-gpu\n',
    ])
    g = "scrocco-llm-t-whisper-gpu-stt"
    assert c.group_caps[g] == "stt"
    # nessun gruppo testo per un alias solo-stt
    assert c.alias_target("whisper-gpu", None) is None


def test_alias_multiplo_su_una_riga():
    c = _cfg([
        'a@x,m1,opencode-zen,https://opencode.ai/zen/v1,free,200,,1,sk1,'
        '"text","uno,due"\n',
    ])
    assert c.alias_target("uno") == "scrocco-llm-t-uno"
    assert c.alias_target("due") == "scrocco-llm-t-due"


def test_alias_non_entra_nelle_catene_di_default():
    c = _cfg([
        'a@x,gemini-3-flash,opencode-zen,https://opencode.ai/zen/v1,free,'
        '200,,1,sk1,"text",gemini\n',
    ])
    # la catena testo contiene l'unique naturale, non i gruppi-alias
    assert "scrocco-llm-t-gemini" not in c.chains["t"]
    assert "scrocco-llm-t-gemini" in c.groups
    # whitelist: i gruppi-alias sono richiamabili
    assert "scrocco-llm-t-gemini" in c.whitelist_for("t")


# --------------------------------------------------------------- router
def _router(c):
    from app.router import Router
    from app.policy import Policy
    r = Router.__new__(Router)
    r.config = c
    r.policy = Policy.default()
    return r


def test_router_risolve_alias_hidden_name():
    c = _cfg([
        'a@x,gemini-3-flash,opencode-zen,https://opencode.ai/zen/v1,free,'
        '200,,1,sk1,"text",gemini\n',
        'd@x,whisper-large-v3-turbo,speaches,http://speaches-gpu:8000/v1,'
        'free,128,,5,sk4,"stt",whisper-gpu\n',
    ])
    r = _router(c)
    # alias col nome nudo: risolto col profilo passato dal chiamante
    assert r.resolve_group_for_request(
        "gemini", [], None, frozenset({"text"}),
        profile="t") == "scrocco-llm-t-gemini"
    assert r.resolve_group_for_request(
        "whisper-gpu", [], None, frozenset({"stt"}),
        profile="t") == "scrocco-llm-t-whisper-gpu-stt"


def test_router_risolve_alias_prefissato():
    c = _cfg([
        'a@x,gemini-3-flash,opencode-zen,https://opencode.ai/zen/v1,free,'
        '200,,1,sk1,"text",gemini\n',
    ])
    r = _router(c)
    assert r.resolve_group_for_request(
        "scrocco-llm-t-gemini", [], None, frozenset({"text"})) == \
        "scrocco-llm-t-gemini"


def test_router_alias_senza_profilo_ricade_su_tutti():
    c = _cfg([
        'a@x,gemini-3-flash,opencode-zen,https://opencode.ai/zen/v1,free,'
        '200,,1,sk1,"text",gemini\n',
    ])
    r = _router(c)
    # senza profilo: si cerca tra i profili noti (qui il solo 't')
    assert r.resolve_group_for_request(
        "gemini", [], None, frozenset({"text"})) == "scrocco-llm-t-gemini"


def test_router_alias_non_e_esplicito():
    c = _cfg([
        'a@x,gemini-3-flash,opencode-zen,https://opencode.ai/zen/v1,free,'
        '200,,1,sk1,"text",gemini\n',
    ])
    r = _router(c)
    # un alias NON e' un suffisso esplicito: la richiesta deve poter salire
    # la catena -free -> -go -> fallback del profilo.
    assert r.is_explicit("gemini") is False


def test_router_alias_cap_richiesta_non_disponibile_ricade_normale():
    c = _cfg([
        'a@x,gemini-3-flash,opencode-zen,https://opencode.ai/zen/v1,free,'
        '200,,1,sk1,"text",gemini\n',
    ])
    r = _router(c)
    # alias solo-testo richiesto per stt: nessun gruppo -> routing normale
    # (il nome non e' un gruppo noto -> None, pass-through)
    assert r.resolve_group_for_request(
        "gemini", [], None, frozenset({"stt"})) is None


def test_alias_condivide_unique_senza_errore_self_check():
    """Regressione #53: i gruppi-alias riusano le STESSE istanze-dep dei
    gruppi normali. `self_check` non deve segnalare unique duplicati, altrimenti
    ogni reload con un alias attivo fallisce e viene scartato."""
    from app.config import self_check
    c = _cfg([
        'a@x,whisper-large-v3-turbo,speaches,http://speaches-gpu:8000/v1,'
        'free,128,0,5,k1,"stt",whisper-gpu\n',
        'a@x,whisper-large-v3-turbo,groq,https://api.groq.com/openai/v1,'
        'free,128,0,5,k2,"stt",\n',
    ])
    problems = self_check(c)
    assert not [p for p in problems if "duplicato" in p], problems
    assert "scrocco-llm-t-whisper-gpu-stt" in c.groups
    # stessa istanza condivisa tra gruppo-alias e gruppo normale
    alias_dep = c.groups["scrocco-llm-t-whisper-gpu-stt"][0]
    normal = [d for d in c.groups["scrocco-llm-t-stt"]
              if d["unique"] == alias_dep["unique"]]
    assert normal and normal[0] is alias_dep



def test_alias_dep_multicap_crea_gruppi_per_ogni_cap():
    """Un dep con caps multiple (es. image_gen+image_edit) deve produrre un
    gruppo-alias per OGNI cap richiedibile, non solo per quella del gruppo di
    provenienza (#55: gemini-3.1-flash-image chat-only con image_edit)."""
    c = _cfg([
        'a@x,gemini-3.1-flash-image,antigravity,http://proxy/v1,free,128,0,5,'
        'k1,"image_gen,image_edit",gemini31-image\n',
    ])
    assert "scrocco-llm-t-gemini31-image-image_gen" in c.groups
    assert "scrocco-llm-t-gemini31-image-image_edit" in c.groups
    assert c.group_caps["scrocco-llm-t-gemini31-image-image_gen"] == "image_gen"
    assert c.group_caps["scrocco-llm-t-gemini31-image-image_edit"] == "image_edit"
    # i due gruppi condividono la STESSA istanza-dep
    g1 = c.groups["scrocco-llm-t-gemini31-image-image_gen"][0]
    g2 = c.groups["scrocco-llm-t-gemini31-image-image_edit"][0]
    assert g1 is g2 or g1["unique"] == g2["unique"]


def test_alias_target_sceglie_la_cap_image_edit():
    c = _cfg([
        'a@x,gemini-3.1-flash-image,antigravity,http://proxy/v1,free,128,0,5,'
        'k1,"image_gen,image_edit",gemini31-image\n',
    ])
    assert c.alias_target("gemini31-image", "image_edit") == \
        "scrocco-llm-t-gemini31-image-image_edit"
    assert c.alias_target("gemini31-image", "image_gen") == \
        "scrocco-llm-t-gemini31-image-image_gen"
