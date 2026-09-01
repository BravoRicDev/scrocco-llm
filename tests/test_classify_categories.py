"""Regressione: classificazione categorie in _classify.

Blocca i casi chiave della colonna 'data' + provider/modello:
  - data=free  -> priority (il bucket gratuito primario)
  - data=paolo -> fallback
  - giorno del mese (rinnovo) -> go, TRANNE provider esplicito "zen" -> zen
  - modello con ":free"        -> free
  - NVIDIA NIM NON e' "zen": con un giorno di rinnovo va in "go" come un
    normale provider (l'endpoint integrate.api.nvidia.com non ha alcun
    trattamento speciale).
"""
from datetime import date

from app.config import _classify

TODAY = date(2026, 8, 30)


def _row(*, modello="m", provider="p", endpoint="e", data=""):
    return {
        "modello": modello,
        "provider": provider,
        "endpoint": endpoint,
        "data": data,
    }


# ------------------------------------------------------------ data 'free' ----

def test_data_free_is_priority_bucket():
    # "free" nella colonna data -> bucket priority (il free primario),
    # indipendentemente dal provider: NVIDIA NIM incluso.
    for provider, endpoint in (
        ("groq", "https://api.groq.com/openai/v1"),
        ("nvidia", "https://integrate.api.nvidia.com/v1"),
        ("opencode-zen", "https://opencode-zen.example/v1"),
    ):
        got = _classify(_row(provider=provider, endpoint=endpoint, data="free"),
                        TODAY)
        assert got["category"] == "priority"
        assert got["sort_key"] == 0


# ------------------------------------------------------------ data 'paid' ----

def test_data_paid_is_fallback():
    got = _classify(_row(endpoint="https://api.mistral.ai/v1", data="paid"),
                    TODAY)
    assert got["category"] == "fallback"


# -------------------------------------------------- giorno di rinnovo -------

def test_renewal_day_defaults_to_go():
    # Giorno di rinnovo (colonna data numerica) -> bucket "go" per un
    # provider qualsiasi, NVIDIA NIM compreso.
    for provider, endpoint in (
        ("groq", "https://api.groq.com/openai/v1"),
        ("nvidia", "https://integrate.api.nvidia.com/v1"),
    ):
        got = _classify(_row(provider=provider, endpoint=endpoint, data="15"),
                        TODAY)
        assert got["category"] == "go"
        assert got["sort_key"] == 16    # giorni al prossimo rinnovo (15 >= 30? no -> mese prossimo)


def test_renewal_day_zen_only_for_explicit_provider():
    # "zen" scatta SOLO se la colonna provider contiene "zen" (opencode):
    # l'endpoint opencode-zen DA SOLO non basta.
    got = _classify(_row(provider="opencode-zen",
                         endpoint="https://opencode-zen.example/v1", data="15"),
                    TODAY)
    assert got["category"] == "zen"

    got = _classify(_row(provider="opencode",
                         endpoint="https://opencode-zen.example/v1", data="15"),
                    TODAY)
    assert got["category"] == "go"


# ----------------------------------------------------- modello ':free' ------

def test_model_with_free_suffix_is_free():
    # Modello con ":free" -> bucket "free" (il secondo blocco).
    got = _classify(_row(modello="nvidia/nemotron-3.5-lightning:free",
                         provider="nvidia",
                         endpoint="https://integrate.api.nvidia.com/v1",
                         data="15"), TODAY)
    assert got["category"] == "free"


def test_nvidia_endpoint_never_zen():
    # Regressione del codice morto: l'endpoint NVIDIA NIM non deve MAI
    # classificare la riga come "zen" solo per l'endpoint.
    for data, expected in (
        ("free", "priority"),
        ("15", "go"),
        ("", "go"),
        ("paid", "fallback"),
    ):
        got = _classify(_row(provider="nvidia",
                             endpoint="https://integrate.api.nvidia.com/v1",
                             data=data), TODAY)
        assert got["category"] == expected, (data, got["category"])


# ------------------------------------------------------------ misc --------

def test_default_go_when_no_data():
    # Colonna data vuota: default "go".
    got = _classify(_row(), TODAY)
    assert got["category"] == "go"