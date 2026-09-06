"""The control plane must never leak into the product (TODO.md item 212).

Stripe, KMS and session dependencies must not enter the product's lock, its
SBOM, or `dep-audit`'s scope, and the signing plane must never ship inside a
customer artifact. Both are one careless edit away at all times, and each has a
*helpful-looking* edit that would undo it:

* "Why don't my control-plane tests run under `make test`?" → widening
  `testpaths` drags Stripe and KMS imports into the product's test environment.
* "Let's scan everything" → widening `bandit -r src/` puts control-plane findings
  in the product's SAST job, where a CVE in a payment SDK the customer never
  receives would fail the customer's gate.
* "One lockfile is simpler" → a security reviewer reading the product's
  dependency inventory should never have to ask why a payment SDK is in it.

So the separation is asserted, not conventional.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE = ROOT / "control-plane"

#: Packages that belong to the vendor plane and nowhere near the product.
_VENDOR_ONLY = ("stripe", "google-cloud-kms", "alembic", "psycopg")


def test_the_control_plane_is_its_own_project_with_its_own_lockfile():
    if not CONTROL_PLANE.is_dir():
        pytest.skip(
            "control-plane/ is not part of this distribution — the publication "
            "filter strips it from the open-source repository. The isolation this "
            "test guards is enforced where the service actually lives."
        )
    assert (CONTROL_PLANE / "pyproject.toml").is_file()
    assert (CONTROL_PLANE / "poetry.lock").is_file(), (
        "the control plane must pin its own dependencies; sharing the product's "
        "lock is what puts Stripe in the customer's SBOM"
    )


def test_no_vendor_dependency_reaches_the_products_lockfile():
    lock = (ROOT / "poetry.lock").read_text(encoding="utf-8")
    found = [name for name in _VENDOR_ONLY if f'name = "{name}"' in lock]
    assert not found, (
        f"{found} entered the product's lock. A customer's security reviewer reads "
        "this inventory, and a CVE in a payment SDK they never receive would fail "
        "their gate on software they do not run."
    )


def test_no_vendor_dependency_is_declared_by_the_product():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = str(metadata).lower()
    assert "stripe" not in declared
    assert "google-cloud-kms" not in declared


def test_the_products_test_run_excludes_the_control_plane():
    """`testpaths = ["tests"]` is load-bearing, not incidental."""
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


def test_the_control_plane_can_never_ship_inside_a_customer_artifact():
    from scripts.check_release_artifacts import FORBIDDEN_PARTS

    assert "control-plane" in FORBIDDEN_PARTS
    assert "control-plane" in (ROOT / ".dockerignore").read_text(encoding="utf-8").split()


def test_no_product_module_imports_the_control_plane():
    """The edge that would make the whole separation cosmetic."""
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "src" / "querygate").rglob("*.py")
        if "control_plane" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"product source references the control plane: {offenders}"


def test_the_control_plane_does_not_import_the_product_at_runtime():
    """One direction is fine and deliberate: the control-plane *tests* import the
    product's verifier, which is what keeps the two codebases' four shared
    constants honest. Runtime source must not — the vendor plane has to be
    deployable without the product installed at all."""
    offenders = []
    for path in (CONTROL_PLANE / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "import querygate" in text or "from querygate" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"control-plane runtime imports the product: {offenders}"
