"""F10: breadcrumb 503 x cache-audit.

Quando la catena si esaurisce (503 retryable), il verdetto dell'audit del
prefisso (identity/prefix) etichetta il contatore: parte dei 503 su catena
fredda sono cache-miss percepiti come "provider morto".
"""
import inspect

from app import main
from app import metrics


def test_exhausted_labels_prefix_mutation():
    metrics.reset()
    r = main._exhausted(3, "boom", prefix_reason="prefix")
    assert r.status_code == 503
    snap = metrics.snapshot(("nx_chain_503_total",))
    assert snap["nx_chain_503_total"][("prefix",)] == 1.0


def test_exhausted_clean_by_default():
    metrics.reset()
    main._exhausted(1, None)
    snap = metrics.snapshot(("nx_chain_503_total",))
    assert snap["nx_chain_503_total"][("clean",)] == 1.0


def test_exhausted_identity_label():
    metrics.reset()
    main._exhausted(2, "x", prefix_reason="identity")
    snap = metrics.snapshot(("nx_chain_503_total",))
    assert snap["nx_chain_503_total"][("identity",)] == 1.0


def test_exhausted_ignores_non_mutated_reason():
    metrics.reset()
    main._exhausted(1, None, prefix_reason="ok")     # 'ok' non e' mutato
    snap = metrics.snapshot(("nx_chain_503_total",))
    assert snap["nx_chain_503_total"][("clean",)] == 1.0


def test_exhausted_return_shape_unchanged():
    r = main._exhausted(1, "detail")
    assert r.status_code == 503
    assert r.headers.get("retry-after") == "2"


def test_stream_path_threads_prefix_reason():
    """Il kwarg deve esistere su _stream_with_fallback (threading dal
    chiamante chat_completions)."""
    params = inspect.signature(main._stream_with_fallback).parameters
    assert "prefix_reason" in params
    assert params["prefix_reason"].default is None
