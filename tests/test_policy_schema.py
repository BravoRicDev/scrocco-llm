"""Fase 2 — policy schema derivato + validazione chiavi YAML (unit).

Verifica che `policy_schema`/`unknown_yaml_paths`/`valid_yaml_keys` derivino
dai dataclass, che le eccezioni di percorso siano corrette e che i knob morti
(`effort_intel_weight`, `retire_after_days`) siano parsati.
"""
from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from app.policy import (
    Policy,
    policy_effective_all,
    policy_schema,
    unknown_yaml_paths,
    valid_yaml_keys,
)

REPO = Path(__file__).resolve().parents[1]
EXPECTED_SCHEMA_COUNT = 389


def _by_path() -> dict:
    return {f["yaml_path"]: f for f in
            policy_schema(Policy.from_dict({}))["fields"]}


def test_schema_count_and_unique_yaml_path():
    schema = policy_schema(Policy.from_dict({}))
    assert schema["count"] == EXPECTED_SCHEMA_COUNT
    assert len(schema["fields"]) == EXPECTED_SCHEMA_COUNT
    # NB: `name` NON e' unico (enabled compare in QcJson e QcSanity);
    # l'identita' stabile e' `yaml_path`.
    paths = [f["yaml_path"] for f in schema["fields"]]
    assert len(paths) == len(set(paths))


def test_schema_aliases_map_to_yaml_paths():
    fields = _by_path()
    assert fields["warm_pool.refill_enabled"]["name"] == "warm_refill_enabled"
    assert "qc_json.stream_hedge_delay_ms" in fields
    assert fields["qc_sanity.min_chars"]["block"] == "qc_sanity"
    assert fields["qc_sanity.min_chars"]["name"] == "min_chars"


def test_secret_fields_are_masked_in_schema():
    fields = _by_path()
    assert fields["alias_keys.*"]["secret"] is True
    assert fields["alias_keys.*"]["effective"] is None
    assert fields["client_keys.*"]["secret"] is True
    assert fields["client_keys.*"]["effective"] is None


def test_free_form_fields_show_star_suffix():
    fields = _by_path()
    for key in ("pricing.*", "budget_guard.*", "scoring_weights.*",
                "effort_temperature_overrides.*", "aliases.*",
                "retry_after_floor_by_provider.*", "quirks.*"):
        assert key in fields, key


def test_unknown_yaml_paths():
    assert unknown_yaml_paths({"warm_pool": {"bogus": 1}}) == ["warm_pool.bogus"]
    assert unknown_yaml_paths({"qc_json": {"enabled": True}}) == []
    assert unknown_yaml_paths({"totally_bogus": 1}) == ["totally_bogus"]
    assert unknown_yaml_paths({}) == []


def test_example_yaml_has_no_unknown_paths():
    raw = yaml.safe_load((REPO / "var" / "gateway.yaml.example").read_text())
    assert unknown_yaml_paths(raw) == []


@pytest.mark.parametrize("flat,nested", [
    ("warm_borrow_enabled", "borrow_enabled"),
    ("warm_borrow_idle_sec", "borrow_idle_sec"),
    ("warm_borrow_selectable", "borrow_selectable"),
    ("nonstream_canary_allowed", "nonstream_canary_allowed"),
])
def test_dual_flat_and_nested_keys_known(flat, nested):
    # from_dict legge queste 4 manopole sia flat sia dentro warm_pool.
    assert flat in valid_yaml_keys()
    assert unknown_yaml_paths({flat: False}) == []
    assert unknown_yaml_paths({"warm_pool": {nested: False}}) == []


def test_effort_intel_weight_parsed():
    assert Policy.from_dict({"effort_intel_weight": 5}).effort_intel_weight == 5.0
    assert (Policy.from_dict({"effort_intel_weight": 2.5})
            .effort_intel_weight == 2.5)


@pytest.mark.parametrize("bad", [-1, "x", True, [5]])
def test_effort_intel_weight_invalid(bad):
    with pytest.raises(ValueError):
        Policy.from_dict({"effort_intel_weight": bad})


def test_retire_after_days_parsed():
    assert Policy.from_dict({"retire_after_days": 30}).retire_after_days == 30


@pytest.mark.parametrize("bad", [0, -1, True, "x"])
def test_retire_after_days_invalid(bad):
    with pytest.raises(ValueError):
        Policy.from_dict({"retire_after_days": bad})


def test_roundtrip_from_dict_asdict():
    base = asdict(Policy())
    assert asdict(Policy.from_dict(base)) == base


def test_effective_all_strips_secrets():
    pol = Policy.from_dict({"aliases": {"a": "groq/x"},
                            "alias_keys": {"a": "secret-key-1"},
                            "client_keys": {"b": "secret-key-2"}})
    eff = policy_effective_all(pol)
    assert "alias_keys" not in eff
    assert "client_keys" not in eff
    assert eff["warm_refill_enabled"] == pol.warm_refill_enabled
    assert eff["hunt_backoff_sec"] == pol.hunt_backoff_sec
