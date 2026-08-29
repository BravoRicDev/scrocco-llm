"""Auth a 3 livelli: master key (admin), sk-<profilo> deterministiche,
override client_keys custom.

[IT] HOW: Bearer == GATEWAY_MASTER_KEY -> admin totale; Bearer ==
sk-<profilo> esistente -> client di quel profilo; se la policy definisce
client_keys[profilo], la deterministica DEL profilo e DISATTIVATA (legge
override: una chiave custom SOSTITUISCE, non affianca). WHY i motivi nel
log NEGATA (vuota/formato/profilo inesistente/disattivata): diagnosticare
un 401 senza indovinare.

[EN] WHAT: three-tier bearer auth. WHY: deterministic profile keys give
zero-config tenants; custom overrides REPLACE deterministic ones (single
source of truth); denial reasons are logged so agents can self-debug 401s.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass

from .config import GatewayConfig

log = logging.getLogger("nx.auth")

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
                    and pname not in ck:
                res = AuthResult(True, pname, "local")
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

    # --------------------------------------------------------- authorization
    def model_allowed(self, profile: str, requested_model: str) -> bool:
        """Whitelist a tre livelli.

        L'auth avviene PRIMA dell'hook di routing: il client chiede il NOME BASE
        (o un gruppo/deployment esplicito). Tutti devono essere in whitelist.
        """
        whitelist = set(self.config.whitelist_for(profile))
        return requested_model in whitelist

    def authorize_model(self, auth: AuthResult, model: str) -> bool:
        """True se la chiave può usare 'model'. Master key -> tutto."""
        if auth.mode == "master":
            return True
        if auth.profile is None:
            return False
        return self.model_allowed(auth.profile, model)