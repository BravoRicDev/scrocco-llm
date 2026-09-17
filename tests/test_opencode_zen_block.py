"""Blocco ZEN separato nella scala (initial_pick / _walk_ladder_resilient).

Decisioni utente (piano A):
  - sotto cautela opencode (traffico spoofato) gli zen NON vengono interleavati
    nella camminata sequenziale della chain: formano un blocco separato provato
    DOPO tutti i free non-zen (vivi, prelast, wakeup, stantii) e PRIMA dei
    bucket -go/-fallback;
  - in particolare dopo un fallimento del primo provider NON si deve saltare a
    zen: si prova prima il secondo deployment dello stesso provider;
  - fuori cautela (client opencode reale, o OPENCODE_CAUTIOUS=0) l'ordine
    nativo (zen order 0 = primo) resta invariato.
"""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.opencode_gate import (set_allow_opencode_zen, set_spoofing_request)
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

# -100k: 2 deployment dello stesso provider non-zen (groq), 1 nvidia, 1 zen.
# -200k: 1 groq. -go: 1 opencode-go (data=giorno -> future -> go).
# -fallback: 1 groq (data=fallback).
CSV_SEQ = """commento,modello,provider,endpoint,data,context,max_input,priority,order,scrocco-llm-test
t@x.com,m/p1,groq,https://api.groq.com/openai/v1,free,100,100000,5,10,K-P1
t@x.com,m/p2,groq,https://api.groq.com/openai/v1,free,100,100000,5,10,K-P2
t@x.com,m/n1,nvidia,https://integrate.api.nvidia.com/v1,free,100,100000,5,20,K-N1
t@x.com,m/z1,opencode-zen,https://opencode.ai/zen/v1,free,100,100000,5,0,K-Z1
t@x.com,m/p3,groq,https://api.groq.com/openai/v1,free,200,200000,5,10,K-P3
t@x.com,m/g1,opencode-go,https://opencode.ai/zen/go/v1,15,200,200000,5,20,K-G1
t@x.com,m/f1,groq,https://api.groq.com/openai/v1,fallback,100,100000,5,5,K-F1
"""

POLICY = {
    "capability_routing": {"model_capabilities": {
        "m/p1": ["text"], "m/p2": ["text"], "m/n1": ["text"], "m/z1": ["text"],
        "m/p3": ["text"], "m/g1": ["text"], "m/f1": ["text"]}},
    "ladder_skip_after": 20,
    "ladder_stale_max": 10,
    "dims_ladder_floor": True,
    "deployment_sticky": False,
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("OPENCODE_SPOOF_HEADERS", raising=False)
    monkeypatch.delenv("OPENCODE_CAUTIOUS", raising=False)
    monkeypatch.delenv("OPENCODE_GO", raising=False)
    monkeypatch.delenv("BACKGROUND_CAUTIOUS", raising=False)
    set_allow_opencode_zen(None)
    set_spoofing_request(False)
    yield
    set_allow_opencode_zen(None)
    set_spoofing_request(False)


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_SEQ)
    pol = Policy.from_dict(POLICY)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, pol)
    os.unlink(path)


def _cautious(monkeypatch, spoof: bool = True):
    """Attiva cautela opencode per traffico spoofato."""
    monkeypatch.setenv("OPENCODE_CAUTIOUS", "1")
    set_allow_opencode_zen(True)
    set_spoofing_request(spoof)


def _seq_ladder(r, spoof):
    """Sequenza completa di `_walk_ladder_resilient` sulla chain del profilo,
    simulando il failover (failed_unique = ultimo scelto, tried crescente)."""
    chain = r.config.chains["test"]
    seen: list[str] = []
    tried: set[str] = set()
    cur = None
    for _ in range(20):
        d = r._walk_ladder_resilient(
            chain, cur["unique"] if cur else None,
            frozenset({"text"}), None, tried=tried or None)
        if d is None:
            break
        seen.append(d["model"])
        cur = d
        tried.add(d["unique"])
    return seen


def _model(r, u):
    d = r.config.deployment_by_unique(u)
    return d["model"] if d else None


def test_zen_split_helpers(router, monkeypatch):
    chain = router.config.chains["test"]
    # fuori cautela: nessuno split
    assert router._zen_split(chain)[1] == []
    _, _, gofb = router._zen_split3(chain)
    assert gofb == []
    # in cautela: zen e -go/-fallback isolati
    _cautious(monkeypatch)
    nz, z = router._zen_split(chain)
    assert {_model(router, u) for u in z} == {"m/z1"}
    assert "m/z1" not in {_model(router, u) for u in nz}
    free3, zen3, gofb3 = router._zen_split3(chain)
    assert {_model(router, u) for u in free3} == {
        "m/p1", "m/p2", "m/p3", "m/n1"}
    assert {_model(router, u) for u in zen3} == {"m/z1"}
    assert {_model(router, u) for u in gofb3} == {"m/g1", "m/f1"}


def test_spoofed_ladder_zen_block_after_all_free(router, monkeypatch):
    _cautious(monkeypatch)
    seq = _seq_ladder(router, spoof=True)
    # i primi due tentativi sono i 2 deployment dello stesso provider free
    assert seq[:2] == ["m/p1", "m/p2"]
    # zen NON e' interleavato nella sequenza: arriva dopo i primi free e
    # PRIMA di -go e -fallback.
    # (n1 può restare "dietro": escalation dim monotona pre-esistente — una
    # volta provato p3 in -200k la camminata non torna a -100k finché il
    # fallito non è un dep non-dim. Non è interleaving di zen.)
    zi = seq.index("m/z1")
    gi = seq.index("m/g1")
    fi = seq.index("m/f1")
    assert zi < gi < fi
    assert "m/p1" in seq[:zi] and "m/p2" in seq[:zi]


def test_spoofed_second_failure_does_not_jump_to_zen(router, monkeypatch):
    """Dopo il primo fallimento si prova il 2° deployment del 1° provider,
    NON zen (regressione della rotazione della chain)."""
    _cautious(monkeypatch)
    seq = _seq_ladder(router, spoof=True)
    assert seq[0] == "m/p1"
    assert seq[1] == "m/p2"                 # stesso provider, non zen


def test_real_opencode_client_native_order(router):
    set_allow_opencode_zen(True)
    set_spoofing_request(False)             # client opencode reale
    seq = _seq_ladder(router, spoof=False)
    # zen (order 0) resta PRIMO nel percorso nativo
    assert seq[0] == "m/z1"


def test_caution_off_native_order(router, monkeypatch):
    monkeypatch.setenv("OPENCODE_CAUTIOUS", "0")
    set_allow_opencode_zen(True)
    set_spoofing_request(True)              # spoof ON ma cautela OFF
    seq = _seq_ladder(router, spoof=True)
    assert seq[0] == "m/z1"                 # ordine nativo (zen primo)


def test_initial_pick_nonzen_first_under_caution(router, monkeypatch):
    _cautious(monkeypatch)
    d = router.initial_pick("test", f"{BASE}-100k", need=frozenset({"text"}),
                            session_id=None)
    assert d is not None and d["model"] in {"m/p1", "m/p2", "m/n1"}


def test_initial_pick_zen_first_for_real_client(router):
    set_allow_opencode_zen(True)
    set_spoofing_request(False)
    d = router.initial_pick("test", f"{BASE}-100k", need=frozenset({"text"}),
                            session_id=None)
    assert d is not None and d["model"] == "m/z1"