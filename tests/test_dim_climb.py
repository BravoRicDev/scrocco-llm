"""Salita di dim sull'overflow (m: 'deve SALIRE DI DIM non forzare') e il
crash del coalescer con profile=None passato come 500 al client."""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m-a,groq,https://api.groq.com/openai/v1,free,64,64000,0,K-A,text
t@x,m-b,groq,https://api.groq.com/openai/v1,free,200,200000,0,K-B,text
t@x,m-c,groq,https://api.groq.com/openai/v1,free,512,512000,0,K-C,text
t@x,m-g,groq,https://api.groq.com/openai/v1,,64,64000,0,K-G,text
t@x,m-f,groq,https://api.groq.com/openai/v1,fallback,64,64000,0,K-F,text
"""


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({}))
    yield r
    os.unlink(path)


def test_sale_al_dim_piu_piccolo_che_contiene(router):
    r = router
    assert r.climb_dim_group("scrocco-llm-test-64k", 150000) == \
        "scrocco-llm-test-200k"
    assert r.climb_dim_group("scrocco-llm-test-64k", 300000) == \
        "scrocco-llm-test-512k"
    # nessun dim basta -> None (poi tocca a compattazione/400)
    assert r.climb_dim_group("scrocco-llm-test-64k", 900000) is None


def test_non_scende_mai_e_non_tocca_pagati(router):
    r = router
    # entra gia': None (nessun movimento)
    assert r.climb_dim_group("scrocco-llm-test-512k", 1000) is None
    # bucket pagati e non-dim non si spostano
    assert r.climb_dim_group("scrocco-llm-test-go", 150000) is None
    assert r.climb_dim_group("scrocco-llm-test-fallback", 150000) is None
    assert r.climb_dim_group(None, 150000) is None
    assert r.climb_dim_group("scrocco-llm-test-64k", 0) is None


def test_coalesce_key_con_profile_none():
    """Il 500 'NoneType + str' passato trasparente al client (incidente in produzione)."""
    from app.main import _coalesce_key
    k1 = _coalesce_key({"model": "m", "messages": []}, None)
    k2 = _coalesce_key({"model": "m", "messages": []}, "")
    assert isinstance(k1, str) and k1 == k2
