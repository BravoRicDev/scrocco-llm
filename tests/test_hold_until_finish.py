"""hold-until-finish: il gateway NON consegna mai una risposta a meta'.

Con hold attivo, `_peek_stream` continua a bufferizzare finche' lo stream
upstream non si chiude in modo PULITO (finish_reason presente oppure
`[DONE]`). Solo allora la risposta viene consegnata. Se lo stream finisce
troncato (EOF senza terminatore) o con `finish_reason=length` (non dovuto
al cap del client) il verdict e' rispettivamente 'truncated' /
'length_truncated': il chiamante scarta e ruota (o 503), mai parziale.

`app.main` va importato SOLO dentro funzioni/fixture (mai a livello modulo).
"""
import asyncio
from datetime import date

import pytest

from app.config import _classify
from app.policy import Policy

CONTENT = b'data: {"choices":[{"delta":{"content":"ciao mondo"}}]}\n\n'
STOP = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
LENGTH = b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
DONE = b"data: [DONE]\n\n"


@pytest.fixture()
def M():
    import app.main as _M
    return _M


def _row(**over):
    row = {
        "commento": "t@x.com", "modello": "m1", "provider": "p",
        "endpoint": "https://x/v1", "data": "free", "context": "200",
        "max_input": "0", "priority": "0", "caps": "text",
    }
    row.update(over)
    return row


def _run(M, chunks, delay=0.0, min_chars=40, **kw):
    async def gen():
        for i, c in enumerate(chunks):
            if delay and i:
                await asyncio.sleep(delay)
            yield c
    return asyncio.run(M._peek_stream(gen(), 500, False, min_chars, **kw))


# ------------------------------------------------------------ CSV / policy
def test_classify_default_false():
    assert _classify(_row(), date.today())["hold_until_finish"] is False


@pytest.mark.parametrize("v", ["1", "true", "TRUE", "yes", "on"])
def test_classify_true(v):
    assert _classify(_row(hold_until_finish=v), date.today())[
        "hold_until_finish"] is True


def test_policy_defaults():
    qj = Policy.from_dict({}).qc_json
    # hold ON by default (post-incidente 2026-09-15)
    assert qj.stream_hold_until_finish is True
    assert qj.stream_hold_idle_ms == 120000
    assert qj.stream_hold_max_buffer_bytes == 50 * 1024 * 1024


def test_policy_parse():
    qj = Policy.from_dict({"qc_json": {
        "stream_hold_until_finish": True,
        "stream_hold_idle_ms": 30000,
        "stream_hold_max_buffer_bytes": 1048576,
    }}).qc_json
    assert qj.stream_hold_until_finish is True
    assert qj.stream_hold_idle_ms == 30000
    assert qj.stream_hold_max_buffer_bytes == 1048576


# ------------------------------------------------------------ hold mode
def test_hold_clean_stop_is_content(M):
    v, buf, pend, meta = _run(M, [CONTENT, STOP], hold_until_finish=True)
    assert v == "content" and buf == [CONTENT, STOP] and pend is None
    assert meta.get("finish_reason") == "stop"


def test_hold_done_is_content(M):
    v, buf, _p, _m = _run(M, [CONTENT, bytes(DONE)], hold_until_finish=True)
    assert v == "content"


def test_hold_length_is_length_truncated(M):
    v, buf, _p, meta = _run(M, [CONTENT, LENGTH], hold_until_finish=True)
    assert v == "length_truncated"
    assert meta.get("finish_reason") == "length"


def test_hold_eof_without_terminator_is_truncated(M):
    v, _buf, _p, _m = _run(M, [CONTENT], hold_until_finish=True)
    assert v == "truncated"


def test_hold_idle_timeout(M):
    # primo chunk subito, poi un buco > idle -> timeout (nessun byte al client)
    v, _buf, _p, _m = _run(
        M, [CONTENT, STOP], delay=0.4, hold_until_finish=True,
        hold_idle_ms=100)
    assert v == "timeout"


def test_hold_buffer_cap_delivers_content(M):
    big = (b'data: {"choices":[{"delta":{"content":"' + b"x" * 3000
           + b'"}}]}\n\n')
    v, _buf, _p, _m = _run(M, [big], hold_until_finish=True,
                           hold_max_bytes=1024)
    assert v == "content"


def test_non_hold_early_commit_unchanged(M):
    v, _buf, _p, _m = _run(M, [CONTENT, STOP], min_chars=5,
                           hold_until_finish=False)
    assert v == "content"
