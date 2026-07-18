"""`querygate-validate-config` — validate connections.yaml/policy.yaml (and an
optional catalog.yaml) before deploy.

A typo in any of these files currently only surfaces as a runtime error the
first time something touches the affected connection. This loads every file
through the same Pydantic validation the app uses at runtime, plus a
cross-check that every connection id referenced in policy.yaml's
`connections:` map (and catalog.yaml's `connections:` map, when a catalog
file is given) corresponds to a real connection id, and reports all problems
it finds rather than stopping at the first one.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from querygate.catalog.loader import CatalogStore
from querygate.connections.registry import ConnectionRegistry
from querygate.policy.loader import PolicyStore
from querygate.secrets.resolvers import SecretResolverRegistry


def validate_config(
    connections_file: str,
    policy_file: str,
    catalog_file: Optional[str] = None,
    resolver_registry: Optional[SecretResolverRegistry] = None,
) -> list[str]:
    """Return a list of human-readable problems; empty means every given file is valid.

    `resolver_registry` resolves any `${...}` reference in the connections
    file — env-only when omitted (matches `ConnectionRegistry.from_file`'s
    own default). Pass a registry built from the real `AppConfig` (see
    `main()` below) to also catch a broken `${vault:...}` reference before
    deploy, the same way a missing environment variable is already caught.
    """
    errors: list[str] = []

    registry: ConnectionRegistry | None = None
    try:
        registry = ConnectionRegistry.from_file(
            connections_file, resolver_registry=resolver_registry
        )
    except Exception as exc:
        errors.append(f"{connections_file}: {exc}")

    policy_store: PolicyStore | None = None
    try:
        policy_store = PolicyStore.from_file(policy_file)
    except Exception as exc:
        errors.append(f"{policy_file}: {exc}")

    if registry is not None and policy_store is not None:
        known_ids = set(registry.all_ids())
        for connection_id in policy_store.override_connection_ids():
            if connection_id not in known_ids:
                errors.append(
                    f"{policy_file}: 'connections.{connection_id}' does not match any "
                    f"connection id in {connections_file} (known ids: {sorted(known_ids)})"
                )

    catalog_store: CatalogStore | None = None
    if catalog_file:
        try:
            catalog_store = CatalogStore.from_file(catalog_file)
        except Exception as exc:
            errors.append(f"{catalog_file}: {exc}")

    if registry is not None and catalog_store is not None:
        known_ids = set(registry.all_ids())
        for connection_id in catalog_store.connection_ids():
            if connection_id not in known_ids:
                errors.append(
                    f"{catalog_file}: 'connections.{connection_id}' does not match any "
                    f"connection id in {connections_file} (known ids: {sorted(known_ids)})"
                )

    return errors


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="querygate-validate-config",
        description="Validate QueryGate's connections and policy YAML files.",
    )
    parser.add_argument(
        "--connections-file",
        default=None,
        help="Path to connections.yaml (defaults to CONNECTIONS_FILE / AppConfig default)",
    )
    parser.add_argument(
        "--policy-file",
        default=None,
        help="Path to policy.yaml (defaults to POLICY_FILE / AppConfig default)",
    )
    parser.add_argument(
        "--catalog-file",
        default=None,
        help=(
            "Path to an optional curated catalog.yaml (defaults to CATALOG_FILE / "
            "AppConfig default; skipped entirely when neither is set)"
        ),
    )
    args = parser.parse_args()

    from querygate.core.config import config
    from querygate.secrets.resolvers import build_secret_resolver_registry

    connections_file = args.connections_file or config.connections_file
    policy_file = args.policy_file or config.policy_file
    catalog_file = args.catalog_file or config.catalog_file

    errors = validate_config(
        connections_file,
        policy_file,
        catalog_file,
        resolver_registry=build_secret_resolver_registry(config),
    )
    if errors:
        print(f"Config validation FAILED ({len(errors)} problem(s)):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(1)

    catalog_note = f", {catalog_file}" if catalog_file else ""
    print(f"Config validation OK: {connections_file}, {policy_file}{catalog_note}")


if __name__ == "__main__":
    main()
