"""Phase 2: le chiavi deterministiche sk-<profilo> valgono solo in dev; in
produzione (GATEWAY_ENV=production) servono client_keys esplicite e lo startup
e' fail-fast. [EN] Deterministic sk-<profile> keys are dev-only; production
disables them and fails fast without explicit client_keys + a real master key.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from app.auth import AuthManager, generate_client_key, gateway_env, is_production
from app.config import GatewayConfig

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,m-a,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-A,text
"""


@pytest.fixture()
def cfg():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    yield GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    os.unlink(path)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("GATEWAY_ENV", raising=False)
    monkeypatch.delenv("GATEWAY_MASTER_KEY", raising=False)


def test_default_env_is_development():
    assert gateway_env() == "development"
    assert is_production() is False


def test_dev_accepts_deterministic_key(cfg):
    a = AuthManager(cfg, master_key="mk-real", client_keys_provider=lambda: {})
    r = a.authenticate("Bearer sk-test")
    assert r.ok and r.profile == "test" and r.mode == "local"


def test_production_rejects_deterministic_key(cfg, monkeypatch):
    monkeypatch.setenv("GATEWAY_ENV", "production")
    monkeypatch.setenv("GATEWAY_MASTER_KEY", "mk-real-secret-value")
    a = AuthManager(cfg, client_keys_provider=lambda: {})
    assert a.production is True
    r = a.authenticate("Bearer sk-test")
    assert not r.ok and r.profile is None


def test_production_accepts_explicit_client_key(cfg, monkeypatch):
    monkeypatch.setenv("GATEWAY_ENV", "production")
    monkeypatch.setenv("GATEWAY_MASTER_KEY", "mk-real-secret-value")
    key = "sk-abc123custom456"
    a = AuthManager(cfg, client_keys_provider=lambda: {"test": key})
    r = a.authenticate(f"Bearer {key}")
    assert r.ok and r.profile == "test" and r.mode == "local"
    # la deterministica per lo stesso profilo e' disattivata
    assert not a.authenticate("Bearer sk-test").ok


def test_production_fail_fast_without_secrets(cfg, monkeypatch):
    monkeypatch.setenv("GATEWAY_ENV", "production")
    a = AuthManager(cfg, client_keys_provider=lambda: {})
    with pytest.raises(RuntimeError, match="GATEWAY_ENV=production"):
        a.enforce_startup()


def test_production_fail_fast_on_placeholder_master(cfg, monkeypatch):
    monkeypatch.setenv("GATEWAY_ENV", "production")
    monkeypatch.setenv("GATEWAY_MASTER_KEY", "sk-master-CHANGE-ME-xxxxxxxxxxxx")
    a = AuthManager(cfg, client_keys_provider=lambda: {"test": "sk-real-key-1"})
    assert a.startup_issues()                    # master placeholder
    with pytest.raises(RuntimeError):
        a.enforce_startup()


def test_production_ok_with_secret_and_keys(cfg, monkeypatch):
    monkeypatch.setenv("GATEWAY_ENV", "production")
    monkeypatch.setenv("GATEWAY_MASTER_KEY", "mk-real-secret-value")
    a = AuthManager(cfg, client_keys_provider=lambda: {"test": "sk-real-key-1"})
    assert a.startup_issues() == []
    a.enforce_startup()                          # non solleva


def test_dev_never_fails_startup(cfg):
    a = AuthManager(cfg, client_keys_provider=lambda: {})
    assert a.startup_issues() == []
    a.enforce_startup()


def test_generate_client_key_is_random_and_prefixed():
    a, b = generate_client_key(), generate_client_key()
    assert a.startswith("sk-") and len(a) > 20
    assert a != b
