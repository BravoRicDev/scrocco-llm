"""Fair-share delle chiavi nei gruppi capacità PRIMARY (cap_fair_share).

Regola: con `cap_fair_share.enabled` e la cap nell'elenco, i gruppi primary
(-C) scelgono la chiave col MINOR numero di richieste nella finestra rolling
(60s), invece del winner-take-all della reputation (_key_scores: ATTEMPT +1,
SUCCESS -2 -> la chiave riuscita viene sempre ripescata fino al 429). Tie-break:
richieste in volo, model_preference, latenza EMA.

Solo primary: -C-go / -C-fallback e le altre cap non sono toccati. Con il knob
OFF (default) il comportamento resta invariato.
"""
import copy
import os
import tempfile
from collections import Counter

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,groq/whisper-large-v3-turbo,groq,https://api.groq.com/openai/v1,free,0,0,5,sk-W1,stt
t@x.com,groq/whisper-large-v3-turbo,groq,https://api.groq.com/openai/v1,free,0,0,5,sk-W2,stt
t@x.com,groq/whisper-large-v3-turbo,groq,https://api.groq.com/openai/v1,free,0,0,5,sk-W3,stt
t@x.com,groq/whisper-large-v3-turbo,groq,https://api.groq.com/openai/v1,free,0,0,5,sk-W4,stt
t@x.com,groq/whisper-large-v3-turbo,groq,https://api.groq.com/openai/v1,free,0,0,5,sk-W5,stt
t@x.com,openai/tts-1,groq,https://api.groq.com/openai/v1,free,0,0,5,sk-T1,tts
t@x.com,openai/tts-1,groq,https://api.groq.com/openai/v1,free,0,0,5,sk-T2,tts
t@x.com,openai/tts-1,groq,https://api.groq.com/openai/v1,free,0,0,5,sk-T3,tts
"""

POLICY_MAP = {"capability_routing": {"model_capabilities": {
    "*whisper*": ["stt"],
    "*tts*": ["tts"],
}}}

STT = "scrocco-llm-test-stt"
NEED_STT = frozenset({"stt"})
WKEYS = {"sk-W1", "sk-W2", "sk-W3", "sk-W4", "sk-W5"}


def _make(policy_map):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    return Router(cfg, Policy.from_dict(policy_map)), path


@pytest.fixture()
def router():
    pm = copy.deepcopy(POLICY_MAP)
    pm["cap_fair_share"] = {"enabled": True, "caps": ["stt"], "window_sec": 60}
    r, path = _make(pm)
    yield r
    os.unlink(path)


def _picks(router, n, note=True):
    out = []
    for _ in range(n):
        dep = router.pick_deployment(STT, need=NEED_STT)
        assert dep is not None
        out.append(dep["api_key"])
        if note:
            router.note_start(dep["unique"])
    return out


def test_even_distribution(router):
    """20 richieste su 5 chiavi gemelle -> 4 a testa (RPM pari)."""
    c = Counter(_picks(router, 20))
    assert set(c) == WKEYS
    assert set(c.values()) == {4}


def test_respects_cooldown(router):
    """Una chiave in cooldown e' esclusa; le altre 4 si spartiscono pari."""
    w1 = next(d for d in router.config.groups[STT] if d["api_key"] == "sk-W1")
    router.mark_failed(w1["unique"], seconds=600)
    c = Counter(_picks(router, 16))
    assert "sk-W1" not in c
    assert set(c) == WKEYS - {"sk-W1"}
    assert set(c.values()) == {4}


def test_inflight_tiebreak(router):
    """A pari conteggio finestra, vince la chiave con meno richieste in volo."""
    deps = {d["api_key"]: d for d in router.config.groups[STT]}
    for k in WKEYS - {"sk-W2"}:
        router.stats_for(deps[k]["unique"]).inflight = 2
    for _ in range(5):
        dep = router.pick_deployment(STT, need=NEED_STT)
        assert dep["api_key"] == "sk-W2"


def _hammered(policy_map):
    """Con la reputation attiva: dopo un successo la stessa chiave resta la
    scelta (winner-take-all) -> insieme di unique di cardinalita' 1."""
    r, path = _make(policy_map)
    try:
        dep0 = r.pick_deployment(STT, need=NEED_STT)
        assert dep0 is not None
        r.note_start(dep0["unique"])
        r.record_success(dep0["unique"], 100.0)
        picked = {r.pick_deployment(STT, need=NEED_STT)["unique"]
                  for _ in range(10)}
        return picked
    finally:
        os.unlink(path)


def test_disabled_is_winner_take_all():
    """Knob OFF (default): comportamento invariato (martellamento)."""
    assert len(_hammered(copy.deepcopy(POLICY_MAP))) == 1


def test_scope_caps_list():
    """caps=[tts]: l'STT NON e' fair-shared -> winner-take-all invariato."""
    pm = copy.deepcopy(POLICY_MAP)
    pm["cap_fair_share"] = {"enabled": True, "caps": ["tts"]}
    assert len(_hammered(pm)) == 1


def test_policy_parse_validation():
    p = Policy.from_dict({"cap_fair_share": {"enabled": True,
                                             "caps": ["stt", "tts"],
                                             "window_sec": 30}})
    assert p.cap_fair_share_enabled is True
    assert p.cap_fair_share_caps == ["stt", "tts"]
    assert p.cap_fair_share_window_sec == 30
    # default: off, caps [stt], 60s
    d = Policy.from_dict({})
    assert d.cap_fair_share_enabled is False
    assert d.cap_fair_share_caps == ["stt"]
    assert d.cap_fair_share_window_sec == 60
    with pytest.raises(ValueError):
        Policy.from_dict({"cap_fair_share": {"caps": ["bogus"]}})
    with pytest.raises(ValueError):
        Policy.from_dict({"cap_fair_share": {"window_sec": 0}})
