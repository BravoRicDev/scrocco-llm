"""402 "insufficient balance": il DEPLOYMENT va RITIRATO al primo errore
(sblocco solo manuale), non messo in cooldown. La firma ha priorita' sulla
quota perche' i body la accompagnano con "type":"insufficient_quota"."""
from __future__ import annotations

import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.forwarder import is_insufficient_balance, _QUOTA_EXHAUSTED_RE
from app.policy import Policy
from app.router import ErrorKind, Router

BASE = "scrocco-llm-test"
BODY = ('{"error":{"message":"Insufficient balance.",'
        '"type":"insufficient_quota","param":null,'
        '"code":"insufficient_balance"}}')
CSV = ("commento,modello,provider,endpoint,data,context,max_input,priority,"
       f"{BASE},caps,intelligence_score,model_preference,order\n"
       f"t@x.com,m/llm7,llm7,https://api.llm7.io/v1,free,200,200000,5,K-L7,,5,0,0\n")


@pytest.fixture()
def r():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    yield Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
                 Policy.from_dict({}))
    os.unlink(path)


def _dep(r):
    return next(d for lst in r.config.groups.values() for d in lst)


def test_firma_insufficient_balance():
    assert is_insufficient_balance(BODY)
    assert is_insufficient_balance("Insufficient Balance")
    # il body "quota" senza balance NON attiva la firma
    assert not is_insufficient_balance('{"code":429,"message":"Rate limit"}')
    # ...ma per il body llm7 _QUOTA_EXHAUSTED_RE matcherebbe: la firma bilancia
    # deve avere priorita' nel codice (testata sotto via mark_failed)
    assert _QUOTA_EXHAUSTED_RE.search(BODY)


def test_mark_failed_insufficient_balance_ritira_subito(r):
    u = _dep(r)["unique"]
    secs = r.mark_failed(u, reason="insufficient_balance", status=402,
                         kind=ErrorKind.PERMANENT_DEAD)
    assert secs == 0.0, "niente cooldown: si ritira"
    assert r.is_retired(u) is True
    assert r._retired_permanent(u) is True, "mai riusato, nemmeno ultima spiaggia"


def test_ritiro_solo_sblocco_manuale():
    from app import main as M
    kh = M.KEYHEALTH
    kh.set_state("X__m__0", "retired", reason="insufficient_balance")
    assert kh.is_permanently_retired("X__m__0") is True
    # un successo NON lo riabilita (serve unretire manuale)
    kh.observe("X__m__0", fail_streak=0, success_ema=1.0, is_cooled=False)
    assert kh.is_retired("X__m__0") is True
    kh.clear("X__m__0")
    assert kh.is_retired("X__m__0") is False
