"""Pluggable secret resolution for `${...}` references in connections.yaml.

`SecretResolver` is a narrow protocol — one method — so a new backend (AWS
Secrets Manager, GCP Secret Manager, ...) is a new class plus one more
registration line in `build_secret_resolver_registry`, never a change to
`connections/registry.py`'s interpolation call site or to this protocol.
Mirrors the `Authenticator` (core/auth.py) and `AuditSink` (audit/sinks.py)
pluggable-backend shape already used elsewhere in this codebase.

Reference syntax: a bare identifier (`${QUERYGATE_DEMO_DB_URL}`) always means
an environment variable — the original, unchanged behavior. Anything else
must be `${scheme:reference}` (e.g.
`${vault:querygate/demo-db#connection_string}`), dispatched by `scheme` to
whichever resolver is registered for it.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any, Mapping, Optional, Protocol

from dotenv import dotenv_values

if TYPE_CHECKING:
    from querygate.core.config import AppConfig

_ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REFERENCE_PATTERN = re.compile(r"\$\{([^}]+)\}")


class SecretResolver(Protocol):
    def resolve(self, reference: str) -> str:
        """Return the resolved secret, or raise ValueError with a message
        that is safe to surface to an operator — never echo the secret
        itself, only what was being looked up.
        """
        ...


class EnvSecretResolver:
    """The original, always-registered backend: `${VAR}` from the process
    environment or a local `.env` file. Real process environment values win
    over `.env`, matching Pydantic Settings' own precedence and normal
    deployment expectations.
    """

    def __init__(self, environment: Optional[Mapping[str, Optional[str]]] = None) -> None:
        self._environment = environment

    def _runtime_environment(self) -> Mapping[str, Optional[str]]:
        if self._environment is not None:
            return self._environment
        return {**dotenv_values(".env"), **os.environ}

    def resolve(self, reference: str) -> str:
        value = self._runtime_environment().get(reference)
        if value is None:
            raise ValueError(
                f"Environment variable {reference!r} referenced in the connections file is not set"
            )
        return value


class VaultSecretResolver:
    """Reads a KV v2 secret from HashiCorp Vault.

    Reference syntax: `<path>#<field>` (e.g.
    `querygate/demo-db#connection_string`) — `path` is the KV v2 secret path
    under the configured mount point, `field` is the key within that
    secret's data. Token auth only for this first backend
    (`AppConfig.vault_token`) — AppRole/Kubernetes auth can be added as
    another constructor path later without changing `resolve()`'s contract
    or any caller of this class.
    """

    def __init__(
        self,
        *,
        url: str,
        token: str,
        mount_point: str = "secret",
        namespace: Optional[str] = None,
        client: Optional[Any] = None,
    ) -> None:
        import hvac
        import requests

        self._mount_point = mount_point
        self._client = client or hvac.Client(url=url, token=token, namespace=namespace or None)
        # requests.RequestException covers a Vault server that's unreachable
        # or times out — a connectivity failure hvac itself doesn't wrap in
        # VaultError, since it never gets far enough to parse a Vault
        # response at all.
        self._catch = (hvac.exceptions.VaultError, requests.exceptions.RequestException)

    def resolve(self, reference: str) -> str:
        if "#" not in reference:
            raise ValueError(
                f"Malformed vault secret reference {reference!r} — expected 'path#field'"
            )
        path, field = reference.rsplit("#", 1)
        try:
            response = self._client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=self._mount_point, raise_on_deleted_version=True
            )
        except self._catch as exc:
            # Only the exception type name is included — Vault/requests error
            # text is never surfaced, since it could echo request details a
            # deployment doesn't want in a log an operator might paste
            # somewhere (a URL with an embedded credential, etc.).
            raise ValueError(
                f"Vault secret {path!r} (mount {self._mount_point!r}) could not be read: "
                f"{type(exc).__name__}"
            ) from exc
        data = response.get("data", {}).get("data", {})
        if field not in data:
            raise ValueError(f"Vault secret {path!r} has no field {field!r}")
        return str(data[field])


class SecretResolverRegistry:
    """Dispatches a `${...}` reference to the resolver registered for its scheme."""

    def __init__(self, resolvers: Mapping[str, SecretResolver]) -> None:
        self._resolvers = dict(resolvers)

    def interpolate(self, value: str) -> str:
        def _replace(match: "re.Match[str]") -> str:
            body = match.group(1)
            if _ENV_VAR_NAME.match(body):
                scheme, reference = "env", body
            elif ":" in body:
                scheme, reference = body.split(":", 1)
            else:
                raise ValueError(
                    f"Malformed secret reference '{{{body}}}': expected a bare environment "
                    "variable name or 'scheme:reference' (e.g. 'vault:path#field')"
                )
            resolver = self._resolvers.get(scheme)
            if resolver is None:
                raise ValueError(
                    f"No secret resolver registered for scheme {scheme!r} "
                    f"(reference: '{{{body}}}')"
                )
            return resolver.resolve(reference)

        return _REFERENCE_PATTERN.sub(_replace, value)


def build_secret_resolver_registry(cfg: "AppConfig") -> SecretResolverRegistry:
    """Always registers the env resolver; adds Vault when configured.

    Shared by the process-wide connection registry, config reload, and the
    validate-config CLI — the one place that decides which backends exist
    for a given deployment.
    """
    resolvers: dict[str, SecretResolver] = {"env": EnvSecretResolver()}
    if cfg.vault_enabled:
        resolvers["vault"] = VaultSecretResolver(
            url=cfg.vault_addr,
            token=cfg.vault_token,
            mount_point=cfg.vault_kv_mount,
            namespace=cfg.vault_namespace or None,
        )
    return SecretResolverRegistry(resolvers)
