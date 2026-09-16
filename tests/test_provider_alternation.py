"""Anti-raffica provider (regola utente): mai due richieste consecutive allo
stesso provider nel mondo testo/dims; a parita' si preferisce lo STESSO modello
su un altro provider. Non tocca cache/holder/warm, -go/-fallback e capacita'."""
from __future__ import annotations

import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"
HDR = ("commento,modello,provider,endpoint,data,context,max_input,priority,"
       f"{BASE},caps,intelligence_score,model_preference,order\n")
CSV = HDR + (
    f"t@x.com,m/shared,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-G1,,5,0,0\n"
    f"t@x.com,m/shared,openrouter,https://openrouter.ai/api/v1,free,200,200000,5,K-O1,,5,0,0\n"
    f"t@x.com,m/other,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-G2,,5,0,0\n"
)


@pytest.fixture()
def r():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    yield Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
                 Policy.from_dict({}))
    os.unlink(path)


def _dep(r, key):
    return next(d for lst in r.config.groups.values() for d in lst
                if d.get("api_key") == key)


def test_cold_pick_evita_provider_consecutivo(r):
    g = _dep(r, "K-G1")
    o = _dep(r, "K-O1")
    r._last_attempt = ("groq", "m/shared", 200)
    dep = r.pick_deployment(f"{BASE}-200k", ctx=100)
    assert dep["provider"] == "openrouter", "dopo groq non si ripete groq"
    r._last_attempt = ("openrouter", "m/shared", 200)
    dep2 = r.pick_deployment(f"{BASE}-200k", ctx=100)
    assert dep2["provider"] == "groq"
    # entrambi eleggibili e nella stessa dim
    assert {g["unique"], o["unique"]} <= {
        d["unique"] for d in r.config.groups[f"{BASE}-200k"]}


def test_preferisce_stesso_modello_su_altro_provider(r):
    # ultimo = groq/m-shared: l'alternativa same-model su openrouter batte
    # il modello diverso pur restando su groq.
    r._last_attempt = ("groq", "m/shared", 200)
    avoid = r._prov_avoid_key(_dep(r, "K-O1"))
    avoid_other = r._prov_avoid_key(_dep(r, "K-G2"))
    assert avoid[0] == 0 and avoid[2] == 0   # altro provider + stesso modello
    assert avoid_other[0] == 1               # stesso provider
    dep = r.pick_deployment(f"{BASE}-200k", ctx=100)
    assert dep["unique"] == _dep(r, "K-O1")["unique"]


def test_chain_round_robin_provider(r):
    """Catena: giro1 = miglior candidato di OGNI provider, giro2 = il migliore
    rimasto; provider gia' in warm SEMPRE in fondo."""
    g1, o1, g2 = _dep(r, "K-G1"), _dep(r, "K-O1"), _dep(r, "K-G2")
    chain = r._provider_chain([g1, o1, g2], 200)
    seq = [d["unique"] for d in chain]
    provs = [d["provider"] for d in chain]
    assert provs[0] != provs[1], "giro1: provider diversi consecutivi"
    assert set(seq[:2]) == {g1["unique"], o1["unique"]}
    assert seq[2] == g2["unique"]           # giro2: groq, il rimasto
    r.note_session_success("altra", g1["unique"], 100, ctx_est=100)
    chain2 = r._provider_chain([g1, o1, g2], 200)
    assert chain2[0]["provider"] == "openrouter", "provider fresco primo"
    assert chain2[-1]["provider"] == "groq", "provider in warm in fondo"


def test_knob_off_disattiva_alternanza(r):
    r.policy.provider_alternation_enabled = False
    r._last_attempt = ("groq", "m/shared", 200)
    assert r._prov_avoid_key(_dep(r, "K-G1")) == (0, 0, 0)
    assert r._text_alternation_ok(f"{BASE}-200k") is False


def test_ultimo_tentativo_registrato_su_note_start(r):
    g = _dep(r, "K-G1")
    r.note_start(g["unique"], ctx_est=100)
    assert r._last_attempt == ("groq", "m/shared", 200)
