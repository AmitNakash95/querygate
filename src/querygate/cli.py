"""`querygate-validate-config` — validate connections.yaml/policy.yaml before deploy.

A typo in either file currently only surfaces as a runtime error the first
time something touches the affected connection. This loads both files
through the same Pydantic validation the app uses at runtime, plus a
cross-check that every connection id referenced in policy.yaml's
`connections:` map corresponds to a real connection id, and reports all
problems it finds rather than stopping at the first one.
"""

from __future__ import annotations

import argparse
import sys

from querygate.connections.registry import ConnectionRegistry
from querygate.policy.loader import PolicyStore


def validate_config(connections_file: str, policy_file: str) -> list[str]:
    """Return a list of human-readable problems; empty means both files are valid."""
    errors: list[str] = []

    registry: ConnectionRegistry | None = None
    try:
        registry = ConnectionRegistry.from_file(connections_file)
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
    args = parser.parse_args()

    from querygate.core.config import config

    connections_file = args.connections_file or config.connections_file
    policy_file = args.policy_file or config.policy_file

    errors = validate_config(connections_file, policy_file)
    if errors:
        print(f"Config validation FAILED ({len(errors)} problem(s)):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(1)

    print(f"Config validation OK: {connections_file}, {policy_file}")


if __name__ == "__main__":
    main()
