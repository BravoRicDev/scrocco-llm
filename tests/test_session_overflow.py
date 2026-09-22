"""Soglia minima per-sessione (overflow context) + blend per-deployment.

Quando un provider risponde context_length_exceeded:
- la sessione NON riceve il 400: si alza la soglia minima (`_sess_floor`) e si
  ruota sul deployment successivo (la ladder sale di dim);
- le richieste successive stimano `>= floor * margine`, quindi partono da una
  dim che contiene il payload.
In piu', la stima per-sessione puo' essere "blendata" con il divisore
per-deployment appreso (F14, `effective_divisor`): si prende il MASSIMO
(conservativo, anti-overflow).
Qui: monotonia, TTL, purge, cap, blend, extractor dei token richiesti.
"""
import os
import tempfile
import time

from app.config import GatewayConfig
from app.forwarder import (_looks_context_limit, extract_requested_tokens)
from app.policy import Policy
from app.router import Router, estimate_tokens

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m-a,groq,https://api.groq.com/openai/v1,free,64,8000,0,K-A,text
"""


def _router(**pk):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    pol = Policy.from_dict({})
    for k, v in pk.items():
        setattr(pol, k, v)
    return Router(cfg, pol), path


def _msgs(nchars):
    return [{"role": "user", "content": "x" * nchars}]


def test_floor_monotonic_max():
    r, path = _router()
    try:
        r.note_session_overflow("s1", 50000)
        assert r.session_floor_tokens("s1") == 50000
        # un overflow minore NON abbassa la soglia
        r.note_session_overflow("s1", 30000)
        assert r.session_floor_tokens("s1") == 50000
        # un overflow maggiore la alza
        r.note_session_overflow("s1", 70000)
        assert r.session_floor_tokens("s1") == 70000
        # valori invalidi/non numerici ignorati
        r.note_session_overflow("s1", 0)
        r.note_session_overflow("s1", None)
        r.note_session_overflow("s1", "abc")
        r.note_session_overflow("", 90000)
        assert r.session_floor_tokens("s1") == 70000
    finally:
        os.unlink(path)


def test_estimate_respects_floor():
    """Senza rapporto appreso, il floor alza la stima a >= floor*margine."""
    r, path = _router(estimate_divisor=4, session_estimate_margin=1.05)
    try:
        r.note_session_overflow("s1", 60000)
        tokens, used = r.estimate_for_session("s1", _msgs(20000))
        # euristica = 20000/4 = 5000; il floor impone >= 60000*1.05 = 63000
        assert used is False
        assert tokens == int(60000 * 1.05)
        # anche la variante pre-compressione rispetta il floor
        tokens_pre, _ = r.estimate_for_session("s1", _msgs(20000), pre=True)
        assert tokens_pre == int(60000 * 1.05)
    finally:
        os.unlink(path)


def test_floor_ttl_expired():
    r, path = _router(session_estimate_ttl_sec=1)
    try:
        r.note_session_overflow("s1", 60000)
        assert r.session_floor_tokens("s1") == 60000
        time.sleep(1.2)
        assert r.session_floor_tokens("s1") is None
    finally:
        os.unlink(path)


def test_purge_sweeps_expired_and_caps():
    r, path = _router(session_estimate_ttl_sec=1)
    try:
        r.note_session_overflow("old", 10000)
        time.sleep(1.2)
        r.note_session_overflow("fresh", 20000)
        r.purge_expired()
        assert r.session_floor_tokens("old") is None
        assert r.session_floor_tokens("fresh") == 20000
    finally:
        os.unlink(path)


def test_floor_cap_4096():
    r, path = _router()
    try:
        for i in range(4100):
            r.note_session_overflow("s%d" % i, 10000 + i)
        m = r._sess_floor_map()
        assert len(m) <= 4096
        # qualcuna e' stata sfrattata (4100 inserite, cap 4096)
        assert len(m) < 4100
    finally:
        os.unlink(path)


def test_floor_disabled_by_policy():
    r, path = _router(session_estimate_enabled=False)
    try:
        r.note_session_overflow("s1", 60000)
        assert r.session_floor_tokens("s1") is None
    finally:
        os.unlink(path)


def test_blend_with_effective_divisor():
    """Il divisore per-deployment (F14) alza la stima quando dice 'piu' token'.

    note_estimate_error(u1, 10000, 20000): r=2.0 -> target=2.0 -> cur=2.0
    (divisore appreso 2.0 => 20000 char => 10000 token, non 5000)."""
    r, path = _router(estimate_divisor=4, session_estimate_margin=1.05)
    try:
        r.note_estimate_error("u1", 10000, 20000)
        assert r.effective_divisor("u1") < 4.0     # appreso piu' basso di 4
        tokens, used = r.estimate_for_session("s1", _msgs(20000),
                                              unique="u1")
        # euristica 5000; blend F14: 20000/eff_div*1.05 > 5000 -> vince il max
        assert used is False
        assert tokens == int(20000 / r.effective_divisor("u1") * 1.05)
        assert tokens > estimate_tokens(_msgs(20000), 4)
    finally:
        os.unlink(path)


def test_blend_ignored_without_unique():
    r, path = _router(estimate_divisor=4, session_estimate_margin=1.05)
    try:
        r.note_estimate_error("u1", 10000, 20000)
        tokens, _ = r.estimate_for_session("s1", _msgs(20000))
        assert tokens == estimate_tokens(_msgs(20000), 4)
    finally:
        os.unlink(path)


BODY = ("Requested token count exceeds the model's maximum context length "
        "of 256000 tokens. You requested a total of 275614 tokens: "
        "175556 tokens from the input messages and 100058 tokens for "
        "the completion")


def test_extract_requested_tokens_real_body():
    # prima la regex "input messages" (175556), piu' precisa del totale
    assert extract_requested_tokens(BODY) == 175556


def test_extract_requested_tokens_fallback_total():
    body = "Input is too long. You asked for a total of 98765 tokens."
    assert extract_requested_tokens(body) == 98765


def test_extract_requested_tokens_none():
    assert extract_requested_tokens("rate limited, retry later") is None
    assert extract_requested_tokens("") is None
    assert extract_requested_tokens("abc 12 tokens") is None  # sotto il range


def test_looks_context_limit():
    assert _looks_context_limit(-400, BODY)
    assert _looks_context_limit(-413, "whatever")
    assert not _looks_context_limit(-400, "rate limited, retry later")
    assert not _looks_context_limit(-404, "not found")