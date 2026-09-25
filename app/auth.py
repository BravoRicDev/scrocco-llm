"""Auth a 3 livelli: master key (admin), sk-<profilo> deterministiche,
override client_keys custom.

[IT] HOW: Bearer == GATEWAY_MASTER_KEY -> admin totale; Bearer ==
sk-<profilo> esistente -> client di quel profilo; se la policy definisce
client_keys[profilo], la deterministica DEL profilo e DISATTIVATA (legge
override: una chiave custom SOSTITUISCE, non affianca). WHY i motivi nel
log NEGATA (vuota/formato/profilo inesistente/disattivata): diagnosticare
un 401 senza indovinare. Con GATEWAY_ENV=production le sk-<profilo>
deterministiche NON valgono e lo startup e' fail-fast se manca una
configurazione sicura (master key reale + client_keys esplicite).

[EN] WHAT: three-tier bearer auth. WHY: deterministic profile keys give
zero-config tenants (DEVELOPMENT only: GATEWAY_ENV=production disables
them and fails fast without explicit client_keys + a real master key);
custom overrides REPLACE deterministic ones (single source of truth);
denial reasons are logged so agents can self-debug 401s.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass

from .config import GatewayConfig

log = logging.getLogger("nx.auth")

# Ambienti: "production" (o "prod") disattiva le chiavi deterministiche
# sk-<profilo> e pretende chiavi client esplicite + master key reale.
_PRODUCTION_ENVS = {"production", "prod"}
# Placeholder noti: master key mai accettata in produzione.
_WEAK_MASTER_KEYS = {"sk-master", "sk-master-change-me"}


def gateway_env() -> str:
    """Ambiente dichiarato via GATEWAY_ENV (default: development)."""
    return (os.environ.get("GATEWAY_ENV") or "development").strip().lower()


def is_production() -> bool:
    return gateway_env() in _PRODUCTION_ENVS


def _weak_master_key(key: str | None) -> bool:
    if not key or not key.strip():
        return True
    k = key.strip().lower()
    return k in _WEAK_MASTER_KEYS or "change-me" in k


def generate_client_key(prefix: str = "sk") -> str:
    """Chiave client casuale e non prevedibile per un profilo."""
    return f"{prefix}-{secrets.token_urlsafe(24)}"


def _mask(key: str) -> str:
    return f"{key[:6]}..." if len(key) > 10 else "***"


@dataclass
class AuthResult:
    ok: bool
    profile: str | None          # profilo dietro la chiave
    mode: str | None             # "master" | "local"
    error: str | None = None


class AuthManager:
    def __init__(self, config: GatewayConfig, master_key: str | None = None,
                 master_key_name: str = "sk-master",
                 client_keys_provider=None):
        self.config = config
        # FIX: Require explicit master key; no hardcoded default
        if master_key is None:
            env_key = os.environ.get("GATEWAY_MASTER_KEY")
            if env_key is None:
                # Generate a secure random key for this startup session
                # This avoids the well-known "sk-master" default while allowing
                # the service to start without environment configuration
                env_key = secrets.token_urlsafe(32)
                log.warning(
                    "GATEWAY_MASTER_KEY not set in environment; "
                    "using generated key for this session. "
                    "Set GATEWAY_MASTER_KEY in production for proper auth."
                )
            self.master_key = env_key
        else:
            self.master_key = master_key
        self.master_key_name = master_key_name
        # callable -> dict profilo->chiave custom (policy.client_keys).
        # Callable (e non dict) così l'hot-reload della policy è sempre vivo.
        self._client_keys = client_keys_provider
        # In produzione le chiavi deterministiche sk-<profilo> non valgono:
        # servono client_keys esplicite e una master key non di default.
        self.production = is_production()

    def parse_bearer(self, authorization: str | None) -> str | None:
        if not authorization:
            return None
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        return parts[1].strip()

    def authenticate(self, authorization: str | None) -> AuthResult:
        key = self.parse_bearer(authorization)
        if not key:
            self._log_auth(key, False, None, None, reason="vuota/assente")
            return AuthResult(False, None, None,
                              "Authentication Error, No api key passed in.")

        # master key -> admin
        if key == self.master_key:
            res = AuthResult(True, None, "master")
        else:
            # override chiavi CUSTOM per profilo (policy.client_keys):
            # match esatto -> autenticato; se un profilo HA l'override, la
            # chiave deterministica sk-<profilo> viene DISATTIVATA.
            ck = self._client_keys() if self._client_keys is not None else {}
            custom_profile = next((p for p, v in ck.items() if key == v),
                                  None)
            pname = key[3:] if key.startswith("sk-") else None
            if custom_profile:
                res = AuthResult(True, custom_profile, "local")
            elif pname and pname in self.config.profile_dims \
                    and pname not in ck and not self.production:
                res = AuthResult(True, pname, "local")
            elif pname and pname in self.config.profile_dims \
                    and pname not in ck and self.production:
                res = AuthResult(False, None, None,
                                 "Authentication Error, Invalid api key.")
                self._log_auth(key, False, None, None,
                               reason="chiavi deterministiche disattivate "
                                      "(GATEWAY_ENV=production): usa una "
                                      "client_keys esplicita")
                return res
            elif not key.startswith("sk-"):
                res = AuthResult(False, None, None,
                                 "Authentication Error, Invalid api key.")
                self._log_auth(key, False, None, None, reason="formato")
                return res
            elif pname and pname in ck:
                res = AuthResult(False, None, None,
                                 "Authentication Error, Invalid api key.")
                self._log_auth(key, False, None, None,
                               reason="chiave determinativa disattivata "
                                      "(override client_keys attivo)")
                return res
            elif pname and pname not in self.config.profile_dims:
                res = AuthResult(False, None, None,
                                 "Authentication Error, Invalid api key.")
                self._log_auth(key, False, None, None,
                               reason=f"profilo '{pname}' inesistente")
                return res
            else:
                res = AuthResult(False, None, None,
                                 "Authentication Error, Invalid api key.")
                self._log_auth(key, False, None, None,
                               reason="chiave non riconosciuta")
        self._log_auth(key, res.ok, res.mode, res.profile)
        return res

    def _log_auth(self, key: str | None, ok: bool, mode: str | None,
                  profile: str | None, reason: str | None = None) -> None:
        if ok:
            log.info("[auth] ok mode=%s profile=%s key=%s",
                     mode, profile or "-", _mask(key or ""))
        else:
            log.warning("[auth] NEGATA key=%s motivo=%s",
                        _mask(key or ""), reason or "non riconosciuta")

    # --------------------------------------------------- production fail-fast
    def startup_issues(self) -> list[str]:
        """Problemi bloccanti di configurazione in produzione (vuoto in dev)."""
        if not self.production:
            return []
        problems: list[str] = []
        if _weak_master_key(os.environ.get("GATEWAY_MASTER_KEY")):
            problems.append(
                "GATEWAY_MASTER_KEY assente o ancora al placeholder")
        ck = self._client_keys() if self._client_keys is not None else {}
        if not ck:
            problems.append(
                "nessuna client_keys definita (le sk-<profilo> deterministiche "
                "sono disattivate in produzione)")
        return problems

    def enforce_startup(self) -> None:
        """Fail-fast: in produzione rifiuta di partire con config debole."""
        problems = self.startup_issues()
        if problems:
            raise RuntimeError(
                "GATEWAY_ENV=production ma configurazione non sicura: "
                + "; ".join(problems)
                + ". Genera le chiavi con scripts/gen_client_keys.py e "
                  "imposta GATEWAY_MASTER_KEY.")

    # --------------------------------------------------------- authorization
    def model_allowed(self, profile: str, requested_model: str) -> bool:
        """Whitelist a tre livelli.

        L'auth avviene PRIMA dell'hook di routing: il client chiede il NOME BASE
        (o un gruppo/deployment esplicito). Tutti devono essere in whitelist.
        Gli ALIAS (colonna `alias`) sono nomi richiamabili a tutti gli effetti:
        il gruppo-alias e' in whitelist ma il token alias no, quindi lo
        accettiamo qui (la risoluzione avviene poi nel router).
        """
        if requested_model in self.config.alias_groups:
            return True
        whitelist = set(self.config.whitelist_for(profile))
        return requested_model in whitelist

    def authorize_model(self, auth: AuthResult, model: str) -> bool:
        """True se la chiave può usare 'model'. Master key -> tutto."""
        if auth.mode == "master":
            return True
        if auth.profile is None:
            return False
        return self.model_allowed(auth.profile, model)