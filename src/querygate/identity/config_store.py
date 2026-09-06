"""Loads identity.yaml — providers, SSO settings, and the claim→scope mapping.

Same shape as `connections/registry.py` and `policy/loader.py`: one YAML file
read into a process-wide store on first access, swappable atomically by
`config_reload.reload_config()`. Provider client secrets are interpolated
through the same `SecretResolver` registry connection strings use, so
identity.yaml holds a `${ENV_VAR}` / `vault://…` reference and never a literal
credential — and a secret backend added for connections works here for free.

The file is intentionally split from `users.yaml` (`identity/local_store.py`).
This one is *deployment configuration* an operator edits and reviews; that one
holds password verifiers and mutates at runtime through a single locked writer.
Keeping them apart means a config diff never contains a password hash.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

import yaml

from querygate.identity.mapping import IdentityMappingStore
from querygate.identity.models import (
    IdentityProviderProfile,
    PublicIdentityProvider,
    SsoSettings,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from querygate.core.config import AppConfig
    from querygate.secrets.resolvers import SecretResolverRegistry


def _default_resolver_registry() -> "SecretResolverRegistry":
    from querygate.secrets.resolvers import EnvSecretResolver, SecretResolverRegistry

    return SecretResolverRegistry({"env": EnvSecretResolver()})


class IdentityConfigStore:
    """Every configured way a human can sign in, plus what that earns them."""

    def __init__(
        self,
        providers: Dict[str, IdentityProviderProfile],
        settings: SsoSettings,
        mapping: IdentityMappingStore,
    ) -> None:
        self._providers = providers
        self._settings = settings
        self._mapping = mapping

    @classmethod
    def empty(cls) -> "IdentityConfigStore":
        """An SSO-disabled deployment: no providers, no grants."""
        return cls({}, SsoSettings(), IdentityMappingStore.empty())

    @classmethod
    def from_file(
        cls, path: str, resolver_registry: Optional["SecretResolverRegistry"] = None
    ) -> "IdentityConfigStore":
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Identity file not found: {path}")
        raw = yaml.safe_load(file_path.read_text()) or {}
        return cls.from_dict(raw, resolver_registry=resolver_registry)

    @classmethod
    def from_dict(
        cls, raw: dict, resolver_registry: Optional["SecretResolverRegistry"] = None
    ) -> "IdentityConfigStore":
        registry = resolver_registry or _default_resolver_registry()
        settings = SsoSettings.model_validate(raw.get("sso") or {})

        providers: Dict[str, IdentityProviderProfile] = {}
        for raw_entry in raw.get("providers") or []:
            entry = dict(raw_entry or {})
            if entry.get("client_secret"):
                entry["client_secret"] = registry.interpolate(str(entry["client_secret"]))
            if entry.get("kind") == "dev":
                # QueryGate serves this provider itself, so its issuer and client
                # id are ours to fill in — an operator supplying either would be
                # describing a different provider than the one that will answer.
                from querygate.identity.dev_idp import DEV_CLIENT_ID, MOUNT_PATH

                if not settings.base_url:
                    raise ValueError(
                        "A kind=dev identity provider requires `sso.base_url` to be set: "
                        "QueryGate serves the provider itself, so it must know the origin "
                        "a browser reaches it on."
                    )
                entry["issuer"] = settings.base_url.rstrip("/") + MOUNT_PATH
                entry["client_id"] = DEV_CLIENT_ID
            profile = IdentityProviderProfile.model_validate(entry)
            if profile.id in providers:
                raise ValueError(f"Duplicate identity provider id: {profile.id!r}")
            providers[profile.id] = profile

        # Two IdPs can independently assert the same opaque `sub`. Rather than
        # silently prefix (which would decouple a person's browser identity from
        # their agent's bearer-token identity, splitting their policy entry and
        # their audit trail in two), force the operator to decide — but only at
        # the moment a collision actually becomes possible.
        # `dev` participates in the same subject namespace as a real OIDC
        # provider, so it counts here too — otherwise adding the dev provider
        # alongside a real one could silently collide two `sub` values.
        oidc_providers = [p for p in providers.values() if p.kind in ("oidc", "dev") and p.enabled]
        if len(oidc_providers) > 1:
            prefixes = [p.subject_prefix for p in oidc_providers]
            if not all(prefixes) or len(set(prefixes)) != len(prefixes):
                raise ValueError(
                    "More than one OIDC identity provider is enabled, so each needs a "
                    "distinct non-empty `subject_prefix` to keep their subject "
                    "namespaces apart: " + ", ".join(sorted(p.id for p in oidc_providers))
                )

        local_providers = [p.id for p in providers.values() if p.kind == "local"]
        if len(local_providers) > 1:
            raise ValueError(
                "At most one kind=local identity provider may be configured; found "
                f"{', '.join(sorted(local_providers))}."
            )

        mapping = IdentityMappingStore.from_rules(
            list((raw.get("mapping") or {}).get("rules") or [])
        )
        unknown = sorted(
            {rule.provider for rule in mapping.rules if rule.provider != "*"} - set(providers)
        )
        if unknown:
            raise ValueError(
                "identity mapping references unconfigured provider(s): " + ", ".join(unknown)
            )
        return cls(providers, settings, mapping)

    # --- accessors ----------------------------------------------------------

    @property
    def settings(self) -> SsoSettings:
        return self._settings

    @property
    def mapping(self) -> IdentityMappingStore:
        return self._mapping

    def get(self, provider_id: str) -> IdentityProviderProfile:
        profile = self._providers.get(provider_id)
        if profile is None:
            raise KeyError(f"Unknown identity provider: {provider_id!r}")
        return profile

    def get_enabled(self, provider_id: str) -> IdentityProviderProfile:
        """Like `get`, but a disabled provider is indistinguishable from an
        absent one — an operator who turns a provider off must not leave a
        probe that still confirms it exists."""
        profile = self._providers.get(provider_id)
        if profile is None or not profile.enabled:
            raise KeyError(f"Unknown identity provider: {provider_id!r}")
        return profile

    def dev_provider(self) -> Optional[IdentityProviderProfile]:
        for profile in self._providers.values():
            if profile.kind == "dev" and profile.enabled:
                return profile
        return None

    def local_provider(self) -> Optional[IdentityProviderProfile]:
        for profile in self._providers.values():
            if profile.kind == "local" and profile.enabled:
                return profile
        return None

    def list_public(self) -> List[PublicIdentityProvider]:
        return sorted(
            (p.to_public() for p in self._providers.values() if p.enabled),
            key=lambda info: info.id,
        )

    def all_ids(self) -> List[str]:
        return sorted(self._providers)

    @property
    def has_providers(self) -> bool:
        return any(p.enabled for p in self._providers.values())


_store: Optional[IdentityConfigStore] = None


def get_identity_store() -> IdentityConfigStore:
    global _store
    if _store is None:
        from querygate.core.config import config

        if not config.sso_enabled:
            _store = IdentityConfigStore.empty()
        else:
            from querygate.secrets.resolvers import build_secret_resolver_registry

            _store = IdentityConfigStore.from_file(
                config.identity_file,
                resolver_registry=build_secret_resolver_registry(config),
            )
    return _store


def ensure_identity_store(cfg: "AppConfig") -> IdentityConfigStore:
    """Load the store from **this** config, if nothing has loaded one yet.

    `get_identity_store()` reads the process-wide `config` singleton, matching
    how the connections and policy stores behave on the request path. But
    `create_app(cfg)` may be handed a *different* `AppConfig` — that is the
    whole point of the factory — and a startup-time validation that consulted
    the global singleton instead would quietly validate the wrong deployment's
    files, or none at all. So `create_app` calls this: it honours the config it
    was actually given, and it leaves an already-installed store alone, so a
    test (or a hot reload) that set one is never clobbered by a file read.
    """
    global _store
    if _store is not None:
        return _store
    if not cfg.sso_enabled:
        _store = IdentityConfigStore.empty()
        return _store
    from querygate.secrets.resolvers import build_secret_resolver_registry

    _store = IdentityConfigStore.from_file(
        cfg.identity_file, resolver_registry=build_secret_resolver_registry(cfg)
    )
    return _store


def set_identity_store(store: IdentityConfigStore) -> None:
    """Swap the process-wide store (hot reload, and tests)."""
    global _store
    _store = store


def clear_identity_store() -> None:
    global _store
    _store = None
