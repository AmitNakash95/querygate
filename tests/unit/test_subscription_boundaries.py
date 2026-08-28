"""The forbidden-edge guard: which packages may know the subscription exists.

This is a **bidirectional** import rule, and both directions matter for a
different reason (TODO.md item 211):

* ``validation/``, ``compiler/``, ``connections/``, ``policy/``, ``catalog/``,
  ``schema/``, ``query_ast/``, ``write_ast/`` must not import ``subscription/``.
  Compilation and session guardrails must not vary by entitlement — a query that
  compiles differently when the subscription lapses is a query whose *semantics*
  depend on billing. And ``policy/`` importing ``subscription/`` would put
  observe/enforce inside the customer-reloadable ``Policy``, which is precisely
  the customer-settable bypass this design exists to prevent.
* ``subscription/`` must not import ``execution/``, ``validation/`` or
  ``audit/``. The gate reads an in-memory verdict; the moment the subscription
  package can reach the request path or the audit sink, "fail-open per
  iteration" stops being something a reader can verify locally.

A source-level guard rather than a behavioural one, deliberately: the property
is about imports that must *not exist*, and no runtime test can prove the absence
of an edge it never happens to traverse.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "querygate"

#: Packages whose behaviour must not vary by entitlement, in either direction.
_ISOLATED_FROM_SUBSCRIPTION = (
    "validation",
    "compiler",
    "connections",
    "policy",
    "catalog",
    "schema",
    "query_ast",
    "write_ast",
)

#: What `subscription/` itself may reach. `core/` for config and exceptions,
#: `metrics` for the observe counter, `connections`/`identity` for the two
#: reported counts — read-only, and only from `bootstrap`.
_SUBSCRIPTION_MAY_IMPORT = {"subscription", "core", "metrics", "connections", "identity"}

#: What may reach into `subscription/`, and the single module each may reach.
#: `execution/` gets the gate; `api/` gets the lifespan bootstrap and the
#: read-only status accessor item 216 renders.
_ALLOWED_INBOUND = {
    "execution": {"gate"},
    # `observability` is the operator-facing read side (the notice, the coarse
    # signal, the gauge); `models` is value types only. Both are listed
    # explicitly rather than widened to the package, so a future edge still has
    # to be argued for here.
    "api": {"bootstrap", "gate", "manager", "models", "observability"},
    "mcp": {"gate"},
}


def _modules() -> list[tuple[str, Path]]:
    return [
        (str(path.relative_to(SOURCE_ROOT)), path) for path in sorted(SOURCE_ROOT.rglob("*.py"))
    ]


def _imported_querygate_packages(path: Path) -> set[tuple[str, str]]:
    """(package, submodule) pairs this module imports from within querygate."""
    found: set[tuple[str, str]] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    try:
        own_parts = list(path.relative_to(SOURCE_ROOT).parts)
    except ValueError:  # a synthetic file, used by the negative control below
        own_parts = ["planted.py"]
    package_depth = len(own_parts) - 1

    for node in ast.walk(tree):
        parts: list[str] = []
        if isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import — resolve it against this package
                own = own_parts[:package_depth]
                base = own[: len(own) - (node.level - 1)] if node.level > 1 else own
                parts = base + ((node.module or "").split(".") if node.module else [])
            elif node.module and node.module.startswith("querygate"):
                parts = node.module.split(".")[1:]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("querygate"):
                    chunks = alias.name.split(".")[1:]
                    if chunks:
                        found.add((chunks[0], chunks[1] if len(chunks) > 1 else ""))
            continue
        if parts:
            found.add((parts[0], parts[1] if len(parts) > 1 else ""))
    return found


def test_no_isolated_package_imports_the_subscription_layer():
    """Compilation, validation and policy must not know the subscription exists."""
    offenders = []
    for rel, path in _modules():
        package = rel.split("/")[0]
        if package not in _ISOLATED_FROM_SUBSCRIPTION:
            continue
        if any(pkg == "subscription" for pkg, _ in _imported_querygate_packages(path)):
            offenders.append(rel)
    assert not offenders, (
        "these modules import `subscription/`, so behaviour they own would vary by "
        "entitlement — and `policy/` doing it would put observe/enforce inside the "
        f"customer-reloadable Policy: {offenders}"
    )


def test_the_subscription_layer_imports_nothing_it_should_not():
    """The reverse edge: the gate must not be able to reach the request path."""
    offenders = []
    for rel, path in _modules():
        if not rel.startswith("subscription/"):
            continue
        for package, _ in _imported_querygate_packages(path):
            if package and package not in _SUBSCRIPTION_MAY_IMPORT:
                offenders.append(f"{rel} -> {package}")
    assert not offenders, (
        "`subscription/` may reach only "
        f"{sorted(_SUBSCRIPTION_MAY_IMPORT)}; found: {sorted(offenders)}"
    )


def test_inbound_edges_reach_only_their_permitted_module():
    """`execution/` gets `gate`; nothing gets `state`, `verify` or `cache`.

    Reaching `state` directly would let a caller publish a verdict; reaching
    `verify` would let one construct an `Entitlement` without the manager's
    serial floor. Both are the shape of a bypass rather than a use.
    """
    offenders = []
    for rel, path in _modules():
        package = rel.split("/")[0]
        if rel.startswith("subscription/"):
            continue
        allowed = _ALLOWED_INBOUND.get(package)
        for imported_package, submodule in _imported_querygate_packages(path):
            if imported_package != "subscription":
                continue
            if allowed is None or (submodule and submodule not in allowed):
                offenders.append(f"{rel} -> subscription.{submodule or '<package>'}")
    assert not offenders, (
        "unexpected edge into `subscription/`; the permitted set is "
        f"{_ALLOWED_INBOUND}: {sorted(offenders)}"
    )


def test_the_guard_would_catch_a_planted_edge(tmp_path):
    """Negative control: the import extractor must actually see both forms."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "from querygate.subscription.state import subscription_state\n"
        "import querygate.subscription.verify\n"
        "from ..subscription import gate\n",
        encoding="utf-8",
    )
    # `_imported_querygate_packages` resolves relative imports against SOURCE_ROOT,
    # so exercise the absolute forms here and the relative form in the live tree.
    found = _imported_querygate_packages(planted)
    assert ("subscription", "state") in found
    assert ("subscription", "verify") in found
