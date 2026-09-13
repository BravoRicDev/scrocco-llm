"""Test per la colonna CSV `media_defer`.

media_defer=false esenta un deployment (anche multimodale) dal MEDIA DEFER
(multimodal_last_resort) per le richieste di testo puro, senza toccare i caps.
"""
from __future__ import annotations

from datetime import date

from app.config import _classify
from app.csv_store import apply_payload
from app.policy import Policy
from app.router import Router


# ------------------------------------------------------------------ helpers

def _row(**over):
    row = {"modello": "m", "provider": "p", "endpoint": "e", "data": "free"}
    row.update(over)
    return row


class _Cfg:
    """Stub config: un gruppo dims senza cap."""
    group_caps: dict = {"g": None}


def _router() -> Router:
    r = Router.__new__(Router)
    r.policy = Policy()
    r.config = _Cfg()
    r.media_deferred = {}
    r._defer_active = {}
    return r


def _dep(unique: str, caps: set[str], media_defer: bool) -> dict:
    return {"unique": unique, "model": unique,
            "caps": frozenset(caps), "media_defer": media_defer}


# ------------------------------------------------------------- config parse

def test_classify_media_defer_default_true():
    assert _classify(_row(), date.today())["media_defer"] is True


def test_classify_media_defer_false_values():
    for v in ("false", "FALSE", "0", "no", "n", "off"):
        assert _classify(_row(media_defer=v), date.today())["media_defer"] is False, v


def test_classify_media_defer_true_values():
    for v in ("", "true", "1", "yes", "y", "on"):
        assert _classify(_row(media_defer=v), date.today())["media_defer"] is True, v


# ------------------------------------------------------------- router logic

def test_is_deferrable_media_with_flag():
    r = _router()
    assert r._is_deferrable(_dep("m", {"vision"}, True)) is True


def test_is_deferrable_exempt_false():
    r = _router()
    assert r._is_deferrable(_dep("g", {"vision"}, False)) is False


def test_is_deferrable_non_media_false():
    r = _router()
    assert r._is_deferrable(_dep("t", {"text"}, True)) is False


def test_defer_media_exempt_media_stays_in_pool():
    r = _router()
    text = _dep("t", {"text"}, True)       # text-only
    media = _dep("m", {"vision"}, True)    # multimodale deferito
    exempt = _dep("g", {"vision"}, False)  # multimodale esente
    pool, deferred = r._defer_media("g", frozenset({"text"}),
                                    [text, media, exempt])
    assert deferred is True
    assert {d["unique"] for d in pool} == {"t", "g"}


def test_defer_media_all_exempt_no_deferral():
    r = _router()
    text = _dep("t", {"text"}, True)
    exempt = _dep("g", {"vision"}, False)
    pool, deferred = r._defer_media("g", frozenset({"text"}), [text, exempt])
    assert deferred is False
    assert {d["unique"] for d in pool} == {"t", "g"}


def test_defer_media_disabled_by_policy():
    r = _router()
    r.policy.multimodal_last_resort = False
    text = _dep("t", {"text"}, True)
    media = _dep("m", {"vision"}, True)
    pool, deferred = r._defer_media("g", frozenset({"text"}), [text, media])
    assert deferred is False
    assert {d["unique"] for d in pool} == {"t", "m"}


def test_defer_media_skipped_for_media_request():
    r = _router()
    text = _dep("t", {"text"}, True)
    media = _dep("m", {"vision"}, True)
    pool, deferred = r._defer_media("g", frozenset({"vision"}), [text, media])
    assert deferred is False
    assert len(pool) == 2


# ------------------------------------------------------------- admin API

def test_apply_payload_media_defer_normalizes_bool():
    row: dict = {}
    apply_payload(row, {"media_defer": False, "profile": "p"}, "scrocco-llm-")
    assert row["media_defer"] == "false"
    apply_payload(row, {"media_defer": True, "profile": "p"}, "scrocco-llm-")
    assert row["media_defer"] == "true"


def test_apply_payload_media_defer_string_values():
    row: dict = {}
    apply_payload(row, {"media_defer": "0", "profile": "p"}, "scrocco-llm-")
    assert row["media_defer"] == "false"
    apply_payload(row, {"media_defer": "", "profile": "p"}, "scrocco-llm-")
    assert row["media_defer"] == ""
