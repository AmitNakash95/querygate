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
from dataclasses import dataclass
from typing import Optional

import pydantic as pyd
import yaml

from querygate.catalog.loader import CatalogStore
from querygate.connections.registry import ConnectionRegistry
from querygate.core.exceptions import safe_pydantic_error_lines, safe_yaml_error_detail
from querygate.policy.loader import PolicyStore
from querygate.secrets.resolvers import SecretResolverRegistry
from querygate.templates.binding import validate_template_structure
from querygate.templates.loader import TemplateStore


def _describe_load_error(file_label: str, exc: Exception) -> str:
    """Build a safe `"<file>: <detail>"` error line for any exception one of
    `load_config_context`'s file loads can raise.

    `ConnectionRegistry.from_file` validates each entry with
    `ConnectionProfile.model_validate()` *after* `${...}` interpolation, so a
    `pydantic.ValidationError` it raises is validating an object that may
    already carry a live, resolved credential -- `str(exc)` (and
    `error["input"]`/`error["input_value"]`) can embed that whole object for
    some error kinds (e.g. a `missing`-type error). YAML parsing itself can
    also fail on a `connections.yaml` whose `connection_string` is a literal
    (non-`${...}`) credential -- config-governance drafts explicitly permit
    that -- in which case PyYAML's own `Mark.__str__()` embeds the offending
    SOURCE LINE verbatim, before pydantic ever runs. Route both through the
    same structural-safety helpers item 165 built for the REST
    `/admin/reload-config` handler (`safe_pydantic_error_lines`,
    `safe_yaml_error_detail`) instead of ever stringifying the raw exception
    for these two types -- this is item 168's fix, closing the same gap at
    the `load_config_context` source used by both the `querygate-validate-
    config` CLI (`main()`) and the config-governance dry-run endpoints
    (`admin/service.py`'s `validate_candidate_content`). `policy_file`/
    `catalog_file`/`template_file` loads never see a connection string (their
    loaders don't resolve or touch one), so this same, uniform handling for
    them is defense-in-depth/consistency, not a proven leak fix. Every other
    exception type (missing file, duplicate id, unresolved secret reference,
    ...) keeps the plain `f"{file}: {exc}"` shape unchanged -- none of those
    messages are built from an already-interpolated object."""
    if isinstance(exc, pyd.ValidationError):
        return f"{file_label}: " + "; ".join(safe_pydantic_error_lines(exc))
    if isinstance(exc, yaml.YAMLError):
        return f"{file_label}: {safe_yaml_error_detail(exc)}"
    return f"{file_label}: {exc}"


@dataclass(frozen=True)
class LoadedConfigContext:
    """Validated config objects built without installing process globals."""

    registry: ConnectionRegistry
    policy_store: PolicyStore
    catalog_store: CatalogStore


def _templates_only_layers(policy_store) -> list[tuple[str, str]]:
    """Every (human label, connection id) pair whose resolved policy layer sets
    `templates_only`. Walks the connection and principal layers rather than
    calling `PolicyStore.get()` per pair, so it reports the *authored* rule an
    operator has to fix, not a merged view.
    """
    found: list[tuple[str, str]] = []
    for connection_id, policy in getattr(policy_store, "_overrides", {}).items():
        if getattr(policy, "templates_only", False):
            found.append((f"connections.{connection_id}", connection_id))
    for principal, per_connection in getattr(policy_store, "_principal_overrides", {}).items():
        for connection_id, policy in (per_connection or {}).items():
            if connection_id == "*":
                continue
            # A principal override is stored as a RAW DICT (it is merged onto
            # the connection layer at resolve time), unlike the connection
            # layer's parsed `Policy` — so read it both ways rather than
            # assuming, or the common per-principal narrowing goes unchecked.
            narrowed = (
                policy.get("templates_only", False)
                if isinstance(policy, dict)
                else getattr(policy, "templates_only", False)
            )
            if narrowed:
                found.append((f"principals.{principal}.{connection_id}", connection_id))
    return found


def load_config_context(
    connections_file: str,
    policy_file: str,
    catalog_file: Optional[str] = None,
    resolver_registry: Optional[SecretResolverRegistry] = None,
    template_file: Optional[str] = None,
) -> tuple[Optional[LoadedConfigContext], list[str]]:
    """Load and cross-validate config, returning isolated objects on success.

    This is the common implementation behind the validation CLI and the
    draft-policy simulator. It never calls any singleton setter, so callers
    can safely inspect a candidate alongside live requests.
    """
    errors: list[str] = []

    registry: ConnectionRegistry | None = None
    try:
        registry = ConnectionRegistry.from_file(
            connections_file, resolver_registry=resolver_registry
        )
    except Exception as exc:
        errors.append(_describe_load_error(connections_file, exc))

    policy_store: PolicyStore | None = None
    try:
        policy_store = PolicyStore.from_file(policy_file)
    except Exception as exc:
        errors.append(_describe_load_error(policy_file, exc))

    if registry is not None and policy_store is not None:
        known_ids = set(registry.all_ids())
        for connection_id in policy_store.override_connection_ids():
            if connection_id not in known_ids:
                errors.append(
                    f"{policy_file}: 'connections.{connection_id}' does not match any "
                    f"connection id in {connections_file} (known ids: {sorted(known_ids)})"
                )

    catalog_store: CatalogStore | None = CatalogStore.empty()
    if catalog_file:
        try:
            catalog_store = CatalogStore.from_file(catalog_file)
        except Exception as exc:
            catalog_store = None
            errors.append(_describe_load_error(catalog_file, exc))

    if registry is not None and catalog_store is not None:
        known_ids = set(registry.all_ids())
        for connection_id in catalog_store.connection_ids():
            if connection_id not in known_ids:
                errors.append(
                    f"{catalog_file}: 'connections.{connection_id}' does not match any "
                    f"connection id in {connections_file} (known ids: {sorted(known_ids)})"
                )

    if template_file:
        template_store: Optional[TemplateStore] = None
        try:
            template_store = TemplateStore.from_file(template_file)
        except Exception as exc:
            errors.append(_describe_load_error(template_file, exc))
        if template_store is not None:
            for template in template_store.list():
                structural_error = validate_template_structure(template)
                if structural_error is not None:
                    errors.append(f"{template_file}: {structural_error}")
        if registry is not None and template_store is not None:
            known_ids = set(registry.all_ids())
            for connection_id in template_store.connection_ids():
                if connection_id not in known_ids:
                    errors.append(
                        f"{template_file}: template targets connection {connection_id!r} which "
                        f"does not match any connection id in {connections_file} "
                        f"(known ids: {sorted(known_ids)})"
                    )

    # TODO.md item 195: a connection narrowed to `templates_only` with no
    # template that targets it is unusable — every read is refused and there is
    # nothing the caller may invoke instead. It validates clean otherwise, so an
    # operator can flip the switch before publishing the templates and brick the
    # connection with no signal. This is the one misconfiguration the whole
    # promote-then-narrow workflow can end in, so it is caught at validate time.
    # Deliberately an ERROR rather than a warning: there is no deployment for
    # which "this principal may read nothing at all" is the intent — that is
    # what `enabled: false` is for.
    if policy_store is not None:
        narrowed = _templates_only_layers(policy_store)
        if narrowed:
            reachable = (
                set(template_store.connection_ids())
                if template_file and template_store is not None
                else set()
            )
            for label, connection_id in sorted(narrowed):
                if connection_id not in reachable:
                    errors.append(
                        f"{policy_file}: {label} sets templates_only for connection "
                        f"{connection_id!r}, but no query template targets that connection"
                        + (
                            f" in {template_file}"
                            if template_file
                            else " (no templates file is configured)"
                        )
                        + " — every read would be refused with nothing to invoke instead."
                    )

    if errors or registry is None or policy_store is None or catalog_store is None:
        return None, errors
    return LoadedConfigContext(registry, policy_store, catalog_store), []


def validate_config(
    connections_file: str,
    policy_file: str,
    catalog_file: Optional[str] = None,
    resolver_registry: Optional[SecretResolverRegistry] = None,
    template_file: Optional[str] = None,
) -> list[str]:
    """Return a list of human-readable problems; empty means every given file is valid.

    `resolver_registry` resolves any `${...}` reference in the connections
    file — env-only when omitted (matches `ConnectionRegistry.from_file`'s
    own default). Pass a registry built from the real `AppConfig` (see
    `main()` below) to also catch a broken `${vault:...}` reference before
    deploy, the same way a missing environment variable is already caught.
    """
    _context, errors = load_config_context(
        connections_file,
        policy_file,
        catalog_file,
        resolver_registry=resolver_registry,
        template_file=template_file,
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
    parser.add_argument(
        "--template-file",
        default=None,
        help=(
            "Path to an optional query-templates.yaml (defaults to TEMPLATES_FILE / "
            "AppConfig default; skipped entirely when neither is set)"
        ),
    )
    args = parser.parse_args()

    from querygate.core.config import config
    from querygate.secrets.resolvers import build_secret_resolver_registry

    connections_file = args.connections_file or config.connections_file
    policy_file = args.policy_file or config.policy_file
    catalog_file = args.catalog_file or config.catalog_file
    template_file = args.template_file or config.template_file

    errors = validate_config(
        connections_file,
        policy_file,
        catalog_file,
        resolver_registry=build_secret_resolver_registry(config),
        template_file=template_file,
    )
    if errors:
        print(f"Config validation FAILED ({len(errors)} problem(s)):", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(1)

    extra = "".join(f", {f}" for f in (catalog_file, template_file) if f)
    print(f"Config validation OK: {connections_file}, {policy_file}{extra}")


if __name__ == "__main__":
    main()
