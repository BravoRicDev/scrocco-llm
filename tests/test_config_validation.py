"""Reload CSV transazionale (due fasi): lint con riga/colonna + swap atomico.

Se il CSV e' invalido, lo stato precedente resta intatto.
"""
import os
import tempfile
from types import SimpleNamespace

import pytest

from app.config import (GatewayConfig, ConfigValidationError, validate_csv,
                        self_check)

HEADER = ("commento,modello,provider,endpoint,data,context,max_input,priority,"
          "scrocco-llm-test,caps,intelligence_score,order\n")


def _row(model, key, score="5", order="10"):
    return (f"a@x.com,{model},groq,https://api.groq.com/openai/v1,free,32,"
            f"8000,0,{key},text,{score},{order}\n")


def _write(path, body):
    with open(path, "w") as f:
        f.write(HEADER + body)


@pytest.fixture()
def csv_path():
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    _write(path, _row("m-a", "K-A"))
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


def test_validate_csv_clean(csv_path):
    assert validate_csv(csv_path) == []


def test_validate_csv_reports_row_and_column(csv_path):
    _write(csv_path, _row("m-a", "K-A", score="abc"))
    issues = validate_csv(csv_path)
    assert len(issues) == 1
    assert "riga 2" in issues[0]
    assert "intelligence_score" in issues[0]
    assert "abc" in issues[0]


def test_reload_valid_swaps(csv_path):
    cfg = GatewayConfig(csv_path, proxy_prefix="scrocco-llm-")
    n1 = sum(len(v) for v in cfg.groups.values())
    assert n1 == 1
    _write(csv_path, _row("m-a", "K-A") + _row("m-b", "K-B"))
    cfg.reload()
    assert sum(len(v) for v in cfg.groups.values()) == 2


def test_reload_invalid_keeps_previous(csv_path):
    cfg = GatewayConfig(csv_path, proxy_prefix="scrocco-llm-")
    _write(csv_path, _row("m-a", "K-A", score="NON_NUMERO"))
    with pytest.raises(ConfigValidationError):
        cfg.reload()
    # stato precedente intatto
    assert sum(len(v) for v in cfg.groups.values()) == 1
    assert cfg.profiles == ["test"]


def test_self_check_detects_duplicate_unique():
    fake = SimpleNamespace(groups={"g": [{"unique": "u"},
                                         {"unique": "u"}]})
    problems = self_check(fake)
    assert any("duplicato" in p for p in problems)


def test_self_check_ok():
    fake = SimpleNamespace(groups={"g": [{"unique": "u1"},
                                         {"unique": "u2"}]})
    assert self_check(fake) == []
