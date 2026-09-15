"""P1-4: finestra di fallimento del MODELLO (cross-chiave).

3 KO (non esenti) entro `model_fail_window_sec` sullo stesso modello — anche
su chiavi DIVERSE — mettono il modello in pausa su tutte le sue chiavi per
`model_fail_cooldown_sec`. Un successo azzera la finestra. Mai accorciare un
bench piu' lungo (es. ban 24h).
"""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,intelligence_score
t@x.com,m/p1-big,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-B1,,5
t@x.com,m/p1-big,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-B2,,5
t@x.com,m/p1-mid,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-M1,,5
t@x.com,m/p1-mid,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-M2,,5
t@x.com,m/p1-other,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-O1,,5
"""


@pytest.fixture()
def r():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    try:
        yield Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
                     Policy.from_dict({}))
    finally:
        os.path.exists(path) and os.unlink(path)


def _u(r, key):
    for lst in r.config.groups.values():
        for d in lst:
            if d.get("api_key") == key:
                return d["unique"]
    raise AssertionError(key)


def test_knob_parse():
    p = Policy.from_dict({"model_fail_window_sec": 60,
                          "model_fail_threshold": 5,
                          "model_fail_cooldown_sec": 120})
    assert p.model_fail_window_sec == 60
    assert p.model_fail_threshold == 5
    assert p.model_fail_cooldown_sec == 120
    d = Policy.from_dict({})
    assert (d.model_fail_window_sec, d.model_fail_threshold,
            d.model_fail_cooldown_sec) == (900, 3, 600)


def test_sotto_soglia_non_punisce_i_gemelli(r):
    u1, u2 = _u(r, "K-M1"), _u(r, "K-M2")
    r.note_model_failure(u1)
    r.note_model_failure(u1)          # 2 KO < 3
    assert r.cooldown_residual(u1) == 0
    assert r.cooldown_residual(u2) == 0


def test_soglia_bencha_tutte_le_chiavi_del_modello(r):
    u1, u2 = _u(r, "K-M1"), _u(r, "K-M2")
    other = _u(r, "K-O1")
    r.note_model_failure(u1)          # K-M1
    r.note_model_failure(u2)          # K-M2 (chiave diversa)
    r.note_model_failure(u1)          # terza -> bench del MODELLO
    assert r.cooldown_residual(u1) > 0
    assert r.cooldown_residual(u2) > 0
    assert r.cooldown_residual(other) == 0        # altri modelli intatti
    assert r.stats_for(u2).last_reason == "model_unhealthy"


def test_successo_azzera_la_finestra(r):
    u1, u2 = _u(r, "K-M1"), _u(r, "K-M2")
    r.note_model_failure(u1)
    r.note_model_failure(u2)
    r.note_model_success(u1)          # azzera la finestra del modello
    r.note_model_failure(u1)
    r.note_model_failure(u2)
    assert r.cooldown_residual(u1) == 0       # mai arrivati a 3 di fila
    assert r.cooldown_residual(u2) == 0


def test_non_accorcia_un_bench_piu_lungo(r):
    u1, u2 = _u(r, "K-M1"), _u(r, "K-M2")
    r.mark_failed(u2, seconds=86400, reason="ban_tos", status=403)
    assert r.cooldown_residual(u2) > 600
    r.note_model_failure(u1)
    r.note_model_failure(u1)
    r.note_model_failure(u1)
    assert r.cooldown_residual(u2) > 600     # 24h non ridotte a 10min


def test_threshold_zero_disattiva(r):
    r.policy.model_fail_threshold = 0
    u1, u2 = _u(r, "K-M1"), _u(r, "K-M2")
    for _ in range(4):
        r.note_model_failure(u1)
        r.note_model_failure(u2)
    assert r.cooldown_residual(u1) == 0
