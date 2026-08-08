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

`LeasedSecretResolver` (TODO.md item 135) is a *separate*, optional protocol
— never a widening of `SecretResolver` above — for backends that can report
when a resolved secret's lease actually expires (e.g. a Vault dynamic
secret). `SecretResolverRegistry.soonest_lease_expiry` probes for it via
`isinstance` rather than every resolver being forced to implement it, the
same "compose, don't widen" shape `core/auth.py`'s `CompositeAuthenticator`
uses. `config_reload.CredentialLeaseMonitor` is the one caller: it polls
this to decide whether to proactively call `reload_config()` before a lease
expires, instead of waiting for an operator to hit `/admin/reload-config`.

`LeasedSecretResolver.lease_expiry` deliberately never resolves/returns the
secret value itself (a post-ship review of item 135 caught an earlier draft
that did, via a `resolve_with_lease` method): for a backend where "reading
is issuing" (a Vault *dynamic* secrets engine — database, AWS, PKI — as
opposed to the static KV v2 secrets this module implements today), fetching
the value on every poll would mint and immediately orphan a fresh privileged
credential purely to check a timestamp. `lease_expiry` must be implemented
as a side-effect-free/non-minting probe (Vault's own `sys/leases/lookup` is
the non-minting way to introspect an *already-issued* lease); a future
dynamic-secrets resolver must not implement it by calling its own
credential-generating read.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import (
    TYPE_CHECKING,
    Any,
    Iterator,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from dotenv import dotenv_values

if TYPE_CHECKING:
    from querygate.core.config import AppConfig

_ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REFERENCE_PATTERN = re.compile(r"\$\{([^}]+)\}")

# Upper bound on a lease duration this module will act on. Vault's own
# lease_duration is attacker/operator-configured server-side data reaching
# this process over the network; `datetime + timedelta(seconds=huge_value)`
# can raise OverflowError for a large enough value, which would otherwise
# escape `soonest_lease_expiry`'s deliberately narrow `except ValueError`
# (see its docstring). Capping here keeps every code path after a
# `LeasedSecretResolver.lease_expiry` call provably unable to raise anything
# but ValueError, and a lease longer than a year is nonsensical for a
# "proactive refresh before it expires" feature regardless.
_MAX_LEASE_DURATION_SECONDS = 86400 * 400


class SecretResolver(Protocol):
    def resolve(self, reference: str) -> str:
        """Return the resolved secret, or raise ValueError with a message
        that is safe to surface to an operator — never echo the secret
        itself, only what was being looked up.
        """
        ...


@runtime_checkable
class LeasedSecretResolver(Protocol):
    """Optional capability for a `SecretResolver` backend that can report a
    lease expiry for a reference (e.g. Vault dynamic secrets). Deliberately
    not part of `SecretResolver` itself — most backends (env, a static Vault
    KV v2 secret) have no lease at all, and forcing every implementation to
    answer "when does this expire" would push lifecycle state onto backends
    that don't have any. A resolver implements this only when it can
    genuinely answer the question; `SecretResolverRegistry` probes for it
    with `isinstance` rather than assuming every registered resolver has it.
    """

    def lease_expiry(self, reference: str) -> Optional[datetime]:
        """Return the current lease expiry for `reference` as a
        timezone-aware UTC datetime, or `None` when this specific reference
        has no active lease (e.g. a static secret read through a backend
        that *can* report leases for other references) — that is a
        legitimate answer, not an error. Must be side-effect-free: this is
        called on every monitor poll and must never resolve/mint a fresh
        value as a side effect of checking its expiry (see module
        docstring). Raise `ValueError` for the same failures `resolve()`
        would raise for this reference (a transient backend error, a
        reference that no longer exists) — never a different exception
        type, since `SecretResolverRegistry.soonest_lease_expiry` only
        swallows `ValueError` per reference.
        """
        ...


def iter_secret_references(text: str) -> Iterator[Tuple[str, str]]:
    """Every `${...}` reference in `text`, as `(scheme, reference)` pairs —
    the same scheme/reference split `SecretResolverRegistry.interpolate`
    uses internally, exposed so a caller (the lease monitor) can enumerate
    references without resolving/mutating the text. A reference this can't
    split into scheme/body (the same shape `interpolate` would reject) is
    silently skipped here — `interpolate` itself is what surfaces a
    malformed reference as an error at actual resolve time; this helper is
    a best-effort scan, not a validator.
    """
    for match in _REFERENCE_PATTERN.finditer(text):
        body = match.group(1)
        if _ENV_VAR_NAME.match(body):
            yield "env", body
        elif ":" in body:
            scheme, reference = body.split(":", 1)
            yield scheme, reference


class EnvSecretResolver:
    """The original, always-registered backend: `${VAR}` from the process
    environment or a local `.env` file. Real process environment values win
    over `.env`, matching Pydantic Settings' own precedence and normal
    deployment expectations.

    Deliberately does *not* implement `LeasedSecretResolver`: `resolve()`
    already re-reads the environment/`.env` on every call (see
    `_runtime_environment`), so a changed value is picked up on the next
    resolve — it is leaseless, not un-refreshable, and has no TTL to report.
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

    `hvac.Client` is constructed with an explicit, tighter request timeout
    (default 10s vs. hvac's own 30s default) — a background poller
    (`config_reload.CredentialLeaseMonitor`) calls into this class on an
    unattended schedule, and an unreachable/partitioned Vault shouldn't be
    able to stall a poll for the full 30s; the call is also offloaded via
    `asyncio.to_thread` so a stall blocks a thread-pool worker, not the
    event loop itself.

    Also implements `LeasedSecretResolver`: Vault's KV v2 read response
    always carries a `lease_duration` field alongside the secret data, even
    though a *static* KV v2 secret reports it as `0` (no lease) — this
    honestly surfaces whatever Vault actually reports rather than
    synthesizing a TTL. `lease_expiry` does not currently reach a dynamic
    secrets engine (only KV v2 is implemented, same as `resolve()`), so in
    practice today it will usually report `None`; the moment this class (or
    a future dynamic-secrets resolver registered under a different scheme)
    reads a path that genuinely carries a lease, the background monitor
    picks it up with no further change. A KV v2 read is a plain GET with no
    server-side side effect, so probing it on every poll never mints
    anything — see the module docstring for why that property matters.
    """

    def __init__(
        self,
        *,
        url: str,
        token: str,
        mount_point: str = "secret",
        namespace: Optional[str] = None,
        client: Optional[Any] = None,
        timeout: float = 10.0,
    ) -> None:
        import hvac
        import requests

        self._mount_point = mount_point
        self._client = client or hvac.Client(
            url=url, token=token, namespace=namespace or None, timeout=timeout
        )
        # requests.RequestException covers a Vault server that's unreachable
        # or times out — a connectivity failure hvac itself doesn't wrap in
        # VaultError, since it never gets far enough to parse a Vault
        # response at all.
        self._catch = (hvac.exceptions.VaultError, requests.exceptions.RequestException)

    def _read(self, reference: str) -> Tuple[str, str, dict]:
        """Shared read path for `resolve`/`lease_expiry`: split the
        reference, fetch the raw Vault response, and return `(path, field,
        response)` so each public method only owns its own return shape.
        """
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
        return path, field, response

    def resolve(self, reference: str) -> str:
        path, field, response = self._read(reference)
        data = response.get("data", {}).get("data", {})
        if field not in data:
            raise ValueError(f"Vault secret {path!r} has no field {field!r}")
        return str(data[field])

    def lease_expiry(self, reference: str) -> Optional[datetime]:
        # Deliberately discards the resolved value -- probing an expiry
        # must never be the thing that hands out (or even touches) the
        # secret itself; see the module docstring and this class's own.
        path, field, response = self._read(reference)
        data = response.get("data", {}).get("data", {})
        if field not in data:
            raise ValueError(f"Vault secret {path!r} has no field {field!r}")
        lease_duration = response.get("lease_duration") or 0
        if not isinstance(lease_duration, (int, float)) or lease_duration <= 0:
            return None
        lease_duration = min(lease_duration, _MAX_LEASE_DURATION_SECONDS)
        return datetime.now(timezone.utc) + timedelta(seconds=lease_duration)


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

    def soonest_lease_expiry(self, text: str) -> Optional[datetime]:
        """The earliest reported lease expiry among every reference in
        `text` whose resolver implements `LeasedSecretResolver`, or `None`
        if no such reference exists (or none currently reports an expiry).
        Used by `config_reload.CredentialLeaseMonitor` to decide whether a
        proactive reload is due.

        A single reference's probe failure (a transient Vault error, a
        reference that no longer exists) is swallowed and treated as "no
        signal" from that reference, so it can never block detecting
        another reference's real expiry — the monitor's own iteration-level
        catch-all is what handles a total failure (e.g. the connections file
        itself being unreadable).
        """
        soonest: Optional[datetime] = None
        for scheme, reference in iter_secret_references(text):
            resolver = self._resolvers.get(scheme)
            if not isinstance(resolver, LeasedSecretResolver):
                continue
            try:
                expires_at = resolver.lease_expiry(reference)
            except ValueError:
                # ValueError is lease_expiry's documented failure mode
                # (matching resolve()'s own contract) -- a transient Vault
                # error or a reference that no longer exists. Anything else
                # is a bug, not an expected per-reference failure, so it
                # propagates to the monitor's own iteration-level catch-all
                # instead of being silently swallowed here.
                continue
            if expires_at is not None and (soonest is None or expires_at < soonest):
                soonest = expires_at
        return soonest


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
