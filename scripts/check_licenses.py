#!/usr/bin/env python3
"""Generate and gate ``docs/THIRD_PARTY_LICENSES.md`` — the licence of every
Python package in ``poetry.lock``, split by whether QueryGate redistributes it.

Why this exists: QueryGate is being prepared for a source-available (BSL 1.1)
licence flip, and a copyleft licence in the *redistributed* set would be a
blocking legal problem for that. A spot-check of the direct dependencies is not
a pass — this walks all locked packages and fails closed on anything that is not
a reviewed, permissive licence.

**What "redistributed" means here, precisely.** A Python wheel and sdist
contain none of their dependencies, and QueryGate's wheel *declares* only its
**direct** requirements — ``certifi``, for one, is not among them; ``pip``
resolves it transitively through ``httpx``/``httpcore``/``requests``. The
artifact that actually carries third-party bytes is the published container
image, which ``Dockerfile`` builds with ``poetry install --no-root --only main``.
So the ``main`` group is the set of packages QueryGate ships **in the image**,
and the set a ``pip install querygate`` ends up fetching, directly or
transitively. Getting this backwards matters in both directions: it is the
premise of the MPL-2.0 question this pass sends to counsel.

**What this inventory does NOT cover**, stated so nobody mistakes its scope: the
container image also layers a Debian ``bookworm`` userland and Microsoft's
``msodbcsql18`` ODBC driver, installed under ``ACCEPT_EULA=Y``. Those are not
Python packages, are not in ``poetry.lock``, and are out of scope for this tool.
Clearing the image's OS layer is a separate piece of work.

Three deliberate design choices, each of which has a wrong-looking alternative:

1. **``poetry.lock`` is the authority for *which* packages exist, not the
   ambient virtualenv.** This is the same rule ``scripts/generate_sbom.py``
   states for the SBOM: the locked set is what QueryGate ships, not whatever a
   working environment happens to contain. Measured 2026-08-21: this repo's own
   ``.venv`` carried a stale ``sniffio`` left behind by an older ``anyio``, which
   an environment-scanning tool (``pip-licenses`` and friends all scan the
   environment) would have reported as a QueryGate dependency. It is not one.
   The lock is the authority for the *version* too — a licence fact is only
   attributed to a package whose installed version matches the pin.

2. **Licence *facts* come from installed package metadata**, resolved in this
   order: ``License-Expression`` (authoritative under PEP 639), then the Trove
   classifiers, then the free-text ``License`` field. Only the first of those is
   PEP 639's ranking — the standard *deprecates* the other two without ordering
   them, so classifier-before-free-text is this tool's own choice, made because
   ``pytokens`` puts its entire licence *text* in the free-text field and
   ``isoduration`` puts the literal string ``UNKNOWN`` there, while both carry a
   correct classifier. That choice costs precision when a package's classifier is
   a *generic* family label and its free text names the exact variant (``httpx``
   declares ``BSD-3-Clause`` beside a bare ``License :: OSI Approved :: BSD
   License``), so a generic classifier defers to a more specific recognised free-
   text value. See ``_GENERIC_IDENTIFIERS``.

3. **Deny-by-default, three times over.** A licence string this script does not
   recognise is a failure, not a guess — so a future ``GPL-3.0-only`` cannot be
   waved through by a fuzzy match. A **strong**-copyleft licence (GPL, AGPL) is
   an unconditional failure in either group and cannot be waived at all. A
   **weak**-copyleft licence (MPL, LGPL) fails unless a reviewed entry naming
   that exact package exists in ``security/copyleft-license-allowlist.json``,
   the same posture ``security/dependency-audit-allowlist.json`` takes for CVEs.

A handful of locked packages cannot have their licence read on the machine
running this (Windows-only wheels; an ``async-timeout`` that only applies below
Python 3.11.3; one package whose metadata declares no licence at all). Those are
recorded, with evidence, in ``security/third-party-license-overrides.json``.
Two things together make the generated report byte-identical wherever it is
produced: those records replace metadata that cannot be read locally, and every
"does this ship" question is evaluated against ``IMAGE_ENVIRONMENT`` rather than
the generating machine. (On Windows three of the four marker-excluded packages
install locally, and below CPython 3.11.3 the fourth does; their real metadata is
then read and cross-checked against the record, which changes nothing in the
output.) An override never silently outranks
reality, on two independent paths: when the package is installed and says
anything at all about its own licence, a disagreement is a failure; and
``--verify-overrides`` re-reads every record from PyPI in the nightly workflow,
so a record cannot quietly outlive the release it describes. That mode needs
network, which is why it is not part of ``--check`` — the per-commit gate stays
hermetic.

**Facts are gated; the legal reading is not.** A reviewed copyleft record is
part evidence and part argument, and only the first part can be machine-checked.
Each record therefore carries a ``facts`` block — which packages require it,
whether QueryGate declares it directly, and whether any module under
``src/querygate/`` imports it — and the gate verifies
every one of those against ``poetry.lock`` and the source tree on each run. What
remains in ``reason`` is the legal reading, which is explicitly a draft until a
human confirms it. A record whose facts have quietly gone stale can therefore no
longer keep passing on the strength of its prose.

Usage::

    python3 scripts/check_licenses.py --write   # regenerate the report
    python3 scripts/check_licenses.py --check   # gate: drift + licence policy
    python3 scripts/check_licenses.py --verify-overrides   # re-read PyPI (network)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = ROOT / "poetry.lock"
REPORT_FILE = ROOT / "docs" / "THIRD_PARTY_LICENSES.md"
OVERRIDES_FILE = ROOT / "security" / "third-party-license-overrides.json"
COPYLEFT_ALLOWLIST_FILE = ROOT / "security" / "copyleft-license-allowlist.json"

# The Poetry group whose packages QueryGate redistributes. `Dockerfile` builds
# the production image with `poetry install --no-root --only main`, so this is
# exactly the set of third-party bytes in the published image — and the set a
# `pip install querygate` is obliged to fetch. `dev` packages are build/test/CI
# tooling: never shipped, never fetched by a consumer, so their licences
# constrain how QueryGate is *developed*, not how it may be licensed.
SHIPPED_GROUP = "main"

PERMISSIVE = "permissive"
WEAK_COPYLEFT = "weak-copyleft"
STRONG_COPYLEFT = "strong-copyleft"
UNKNOWN = "unknown"

# The only two states a reviewed copyleft record may be in. `draft` means the
# analysis is written but no human has confirmed it; `approved` means the owner
# has. Nothing here may invent a third value silently.
REVIEW_STATUSES = frozenset({"draft", "approved"})

# pip/setuptools/wheel are bootstrapped into a virtualenv by ensurepip and are
# managed by the environment, not by the lock — `scripts/generate_sbom.py` keeps
# the same set for the same reason. Their *licences* still count (they are read
# and reported like any other package); what is exempt is the installed-version-
# must-equal-the-pin rule, which would otherwise fail the unit suite whenever a
# venv's bootstrap pip differs from the locked one.
VERSION_EXEMPT_PACKAGES = frozenset({"pip", "setuptools", "wheel"})

# Trove classifiers that name a licence *family* rather than a specific licence.
# When one of these is all a package's classifiers give us, a recognised and more
# specific free-text `License` value is preferred instead.
_GENERIC_IDENTIFIERS = frozenset({"BSD"})

# Why a package's licence could not be read from a local install. Constrained so
# the overrides file cannot quietly grow into a general-purpose licence-assertion
# escape hatch.
UNREADABLE_REASONS = frozenset({"marker-excluded", "no-licence-metadata"})

# Every licence identifier this project has actually seen, mapped to its tier.
# Adding a package whose licence is not listed here fails the gate — that is the
# point. Extend it deliberately, with the licence read, never to make a red gate
# green.
LICENSE_TIERS: dict[str, str] = {
    "0BSD": PERMISSIVE,
    "Apache-2.0": PERMISSIVE,
    "Apache-2.0 AND BSD-2-Clause": PERMISSIVE,
    "Apache-2.0 OR BSD": PERMISSIVE,
    "Apache-2.0 OR BSD-3-Clause": PERMISSIVE,
    "BSD": PERMISSIVE,
    "BSD-2-Clause": PERMISSIVE,
    "BSD-3-Clause": PERMISSIVE,
    "ISC": PERMISSIVE,
    "MIT": PERMISSIVE,
    "MIT-0": PERMISSIVE,
    "PSF-2.0": PERMISSIVE,
    "Python-2.0": PERMISSIVE,
    "Unlicense": PERMISSIVE,
    "LGPL-2.0-or-later": WEAK_COPYLEFT,
    "LGPL-2.1-or-later": WEAK_COPYLEFT,
    "LGPL-3.0-or-later": WEAK_COPYLEFT,
    "MPL-2.0": WEAK_COPYLEFT,
    "AGPL-3.0-only": STRONG_COPYLEFT,
    "AGPL-3.0-or-later": STRONG_COPYLEFT,
    "GPL-2.0-only": STRONG_COPYLEFT,
    "GPL-2.0-or-later": STRONG_COPYLEFT,
    "GPL-3.0-only": STRONG_COPYLEFT,
    "GPL-3.0-or-later": STRONG_COPYLEFT,
}

# Raw metadata strings -> the identifier above. Keys are matched after
# case-folding and whitespace collapsing; there is no fuzzy or substring
# matching, so an unseen string surfaces as `unknown` rather than being guessed.
_RAW_LICENSE_ALIASES: dict[str, str] = {
    # --- Trove classifiers (the trailing segment, e.g. "MIT License") ---------
    "mit license": "MIT",
    "mit no attribution license (mit-0)": "MIT-0",
    "apache software license": "Apache-2.0",
    "bsd license": "BSD",
    "isc license (iscl)": "ISC",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "python software foundation license": "PSF-2.0",
    "gnu lesser general public license v2 or later (lgplv2+)": "LGPL-2.0-or-later",
    "gnu lesser general public license v3 or later (lgplv3+)": "LGPL-3.0-or-later",
    "gnu general public license v2 or later (gplv2+)": "GPL-2.0-or-later",
    "gnu general public license v3 or later (gplv3+)": "GPL-3.0-or-later",
    "gnu affero general public license v3 or later (agpl3+)": "AGPL-3.0-or-later",
    # Multi-classifier dual licensing, joined by "; " in classifier order.
    # Note that multiple classifiers do not *formally* imply a disjunction; for
    # `python-dateutil` and `packaging` (the only two packages that hit this) the
    # dual grant is real. Any unseen combination falls through to `unknown` and
    # fails closed, which is the behaviour that makes this shortcut safe.
    "apache software license; bsd license": "Apache-2.0 OR BSD",
    "bsd license; apache software license": "Apache-2.0 OR BSD",
    # --- free-text `License:` values seen in this dependency set --------------
    # Reached only when a package declares no expression and no classifier.
    "apache 2": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "3-clause bsd license": "BSD-3-Clause",
    "modified bsd license": "BSD-3-Clause",
    "bsd": "BSD",
    "lgpl": "LGPL-2.1-or-later",
    "mit style": "MIT",
    "mit-0 license": "MIT-0",
    "mpl 2.0": "MPL-2.0",
}

# Every identifier in LICENSE_TIERS is also a valid raw value in its own right —
# a PEP 639 `License-Expression` field carries the SPDX id verbatim. Deriving
# these rather than hand-listing them is what stops a licence from being *known
# to the tier table* but *unrecognised by the classifier*, which would downgrade
# a "this is GPL" failure into a vaguer "unrecognised licence" one.
for _identifier in LICENSE_TIERS:
    _RAW_LICENSE_ALIASES.setdefault(_identifier.lower(), _identifier)

# The alias table may only ever point at identifiers the tier table knows. A
# near-miss entry (say "gplv3" -> "GPL-3.0", which is not a key above) would make
# `classify` raise KeyError instead of denying — a deny-by-default gate that
# crashes rather than denies is the one failure mode it must not have.
_unknown_targets = sorted(set(_RAW_LICENSE_ALIASES.values()) - set(LICENSE_TIERS))
if _unknown_targets:  # pragma: no cover - guards a developer edit, not runtime input
    raise RuntimeError(
        f"_RAW_LICENSE_ALIASES points at identifiers missing from LICENSE_TIERS: "
        f"{_unknown_targets}"
    )


def _display(path: Path) -> str:
    """Repo-relative path for messages, tolerating a path outside the repo."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def normalize_name(name: str) -> str:
    """PEP 503 name normalization. `boolean.py` and `boolean-py` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    shipped: bool
    license_id: str
    tier: str
    source: str  # "metadata" or "override"
    raw: str
    # Whether this package's `main`-group marker keeps it out of the published
    # container image. Recorded so the report can reconcile its row count with
    # the SBOM's component count instead of silently disagreeing with it.
    marker_excluded_from_image: bool = False


def locked_packages(lock_path: Path = LOCK_FILE) -> list[dict]:
    """Every package in poetry.lock, all groups, normalized and sorted.

    Unlike `generate_sbom.py`'s `locked_main_packages`, this deliberately does
    NOT drop marker-excluded packages: a Windows-only wheel in the `main` group
    is still installed for a Windows operator and its licence still counts.
    """
    data = tomllib.loads(lock_path.read_text())
    packages = [
        {
            "name": normalize_name(pkg["name"]),
            "version": pkg["version"],
            "groups": pkg.get("groups", []),
            "markers": pkg.get("markers"),
        }
        for pkg in data["package"]
    ]
    seen: dict[str, str] = {}
    for pkg in packages:
        if pkg["name"] in seen:
            # Poetry can lock two versions of one package under disjoint markers.
            # Only one can ever be installed, so the other would trip the
            # version-drift rule forever with advice (`poetry install`) that can
            # never work. Fail naming the real cause instead.
            raise ValueError(
                f"poetry.lock contains {pkg['name']!r} twice ({seen[pkg['name']]} and "
                f"{pkg['version']}). This gate cannot attribute one licence to two "
                f"locked versions of the same package — resolve the duplication first."
            )
        seen[pkg["name"]] = pkg["version"]
    groups = {g for pkg in packages for g in pkg["groups"]}
    unknown = sorted(groups - {SHIPPED_GROUP, "dev"})
    if unknown:
        # The report labels everything outside `main` as "development, test, and
        # CI only". A third group would be filed there correctly (it is not
        # redistributed) but described wrongly, in a document written for counsel.
        raise ValueError(
            f"poetry.lock has group(s) {unknown} that this report has no wording for — "
            f"teach `render` about them before adding the group."
        )
    return sorted(packages, key=lambda p: p["name"])


def reverse_dependencies(lock_path: Path = LOCK_FILE) -> dict[str, set[str]]:
    """package -> the set of locked packages that declare it as a dependency."""
    data = tomllib.loads(lock_path.read_text())
    reverse: dict[str, set[str]] = {}
    for pkg in data["package"]:
        for dependency in pkg.get("dependencies") or {}:
            reverse.setdefault(normalize_name(dependency), set()).add(normalize_name(pkg["name"]))
    return reverse


def top_level_modules(package: str, distributions=None) -> set[str] | None:
    """The import names a distribution installs, or None if it cannot be resolved.

    A distribution's project name is NOT its import name often enough to matter:
    measured against this repo's own lock, 24 of the 138 locked packages diverge —
    6 of the 60 redistributed ones (`pyyaml` -> `yaml`, `pyjwt` -> `jwt`,
    `python-dateutil` -> `dateutil`, `rpds-py` -> `rpds`, ...). Deriving the
    module by string-munging the project name therefore fails to find real
    imports — and it fails in the direction that *confirms* a record claiming
    QueryGate does not import the package, which is a premise of the copyleft
    argument. This reads the mapping from installed metadata instead, and returns
    None rather than guessing when the package is not installed.
    """
    if distributions is None:
        import importlib.metadata as md

        distributions = md.packages_distributions()
    modules = {
        module
        for module, dists in distributions.items()
        for dist in dists
        if normalize_name(dist) == normalize_name(package)
    }
    return modules or None


def optional_dependency_edges(lock_path: Path = LOCK_FILE) -> set[tuple[str, str]]:
    """(requirer, dependency) pairs the lockfile marks optional.

    "Required by `jsonschema`" reads as a mandatory edge; for `fqdn` it is an
    `extra == "format-nongpl"` edge that only `cyclonedx-python-lib` selects.
    The published facts line is the part a reviewer quotes, so it says so.
    """
    data = tomllib.loads(lock_path.read_text())
    edges: set[tuple[str, str]] = set()
    for pkg in data["package"]:
        for dependency, spec in (pkg.get("dependencies") or {}).items():
            if isinstance(spec, dict) and spec.get("optional"):
                edges.add((normalize_name(pkg["name"]), normalize_name(dependency)))
    return edges


def imported_by_source(
    package: str, source_root: Path | None = None, distributions=None
) -> bool | None:
    """Whether any module under `src/querygate/` imports this package.

    Returns None when the package's import names cannot be resolved, so the
    caller can report that rather than silently reading it as "not imported" —
    a record claiming "no QueryGate source imports it" is doing real work in the
    copyleft argument, so ignorance must not corroborate it.

    Known bound, stated because the legal reading is entitled to know it: this
    matches `import x` / `from x ...` statements and a literal
    `importlib.import_module("x")` / `__import__("x")`. A module name *computed*
    at runtime is beyond any static check, so a False result is strong evidence
    rather than proof — which is why it is one input to a reviewed record and not
    the record itself.
    """
    names = top_level_modules(package, distributions)
    if not names:
        return None
    root = source_root if source_root is not None else ROOT / "src" / "querygate"
    alternation = "|".join(re.escape(n) for n in sorted(names))
    pattern = re.compile(
        rf"^\s*(?:import\s+(?:{alternation})\b|from\s+(?:{alternation})[.\s])"
        rf"|(?:import_module|__import__)\(\s*[\"'](?:{alternation})[.\"']",
        re.M,
    )
    return any(pattern.search(path.read_text(encoding="utf-8")) for path in root.rglob("*.py"))


# The environment of the published container image, which is what the report's
# "does this actually install" claim is about. Evaluating markers against the
# *local* machine instead would make a document that claims to be identical
# everywhere depend on who generated it — and on an Apple Silicon laptop
# (`platform_machine == "arm64"`) it silently produced a different answer than
# CI. `Dockerfile` builds `python:3.11-slim-bookworm` on linux/amd64.
#
# `python_full_version` is a representative 3.11 patch: every marker in this
# lockfile that mentions it compares against 3.11.3, so any supported patch at or
# above that yields the same answer. If a future lock adds a finer-grained
# constraint, pin this to the image's actual interpreter.
IMAGE_ENVIRONMENT = {
    "os_name": "posix",
    "sys_platform": "linux",
    "platform_system": "Linux",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "implementation_name": "cpython",
    "python_version": "3.11",
    "python_full_version": "3.11.13",
    "extra": "",
}


def marker_for_group(marker, group: str) -> str | None:
    """The marker that governs a package's membership of one Poetry group.

    Poetry records either a bare string (applies to every group) or a per-group
    mapping. Reading an arbitrary entry out of that mapping is the bug this
    function exists to prevent: `greenlet` is in both `main` and `dev` and
    carries a marker for `dev` only, so asking "is greenlet excluded?" with the
    dev marker answers a question nobody asked — and answered it wrongly, since
    greenlet is a direct, unmarked `main` dependency of QueryGate itself
    (`pyproject.toml`), required by SQLAlchemy's async layer at runtime. Note
    SQLAlchemy's *own* edge to greenlet IS marker-conditional; QueryGate's is not,
    which is exactly why the group matters.
    """
    if isinstance(marker, dict):
        return marker.get(group)
    return marker or None


def marker_excludes(marker, group: str, environment: dict | None = None) -> bool:
    """Whether `group`'s marker keeps this package out of `environment`.

    `environment=None` means "this machine" — the right question for "is it
    installed here right now". Pass `IMAGE_ENVIRONMENT` for "does it ship".
    """
    expression = marker_for_group(marker, group)
    if not expression:
        return False
    from packaging.markers import Marker

    parsed = Marker(expression)
    return not (parsed.evaluate(environment) if environment else parsed.evaluate())


def load_overrides(path: Path = OVERRIDES_FILE) -> dict[str, dict]:
    """Reviewed licence facts for packages whose licence cannot be read locally."""
    entries = json.loads(path.read_text())
    required = {"package", "version", "license", "unreadable_reason", "reason", "evidence", "added"}
    by_name: dict[str, dict] = {}
    for entry in entries:
        missing = required - entry.keys()
        if missing:
            raise ValueError(f"license override entry missing fields {missing}: {entry}")
        if entry["unreadable_reason"] not in UNREADABLE_REASONS:
            raise ValueError(
                f"license override for {entry['package']!r} has unreadable_reason "
                f"{entry['unreadable_reason']!r}; expected one of {sorted(UNREADABLE_REASONS)}"
            )
        key = normalize_name(entry["package"])
        if key in by_name:
            raise ValueError(f"duplicate license override for {entry['package']!r}")
        by_name[key] = entry
    return by_name


def load_copyleft_allowlist(path: Path = COPYLEFT_ALLOWLIST_FILE) -> dict[str, dict]:
    """Reviewed exceptions permitting a specific weak-copyleft dependency."""
    entries = json.loads(path.read_text())
    required = {
        "package",
        "license",
        "redistributed",
        "facts",
        "reason",
        "added",
        "review_status",
    }
    fact_keys = {"required_by", "imported_by_querygate_source", "declared_direct_dependency"}
    by_name: dict[str, dict] = {}
    for entry in entries:
        missing = required - entry.keys()
        if missing:
            raise ValueError(f"copyleft allowlist entry missing fields {missing}: {entry}")
        facts = entry["facts"]
        if not isinstance(facts, dict) or facts.keys() != fact_keys:
            raise ValueError(
                f"copyleft allowlist entry for {entry['package']!r} must record exactly "
                f"{sorted(fact_keys)} under `facts`, got "
                f"{sorted(facts) if isinstance(facts, dict) else type(facts).__name__}"
            )
        if not isinstance(facts["required_by"], list) or not all(
            isinstance(name, str) for name in facts["required_by"]
        ):
            raise ValueError(
                f"copyleft allowlist entry for {entry['package']!r} must record "
                f"`facts.required_by` as a list of package names"
            )
        for flag in ("imported_by_querygate_source", "declared_direct_dependency"):
            if not isinstance(facts[flag], bool):
                raise ValueError(
                    f"copyleft allowlist entry for {entry['package']!r} must record "
                    f"`facts.{flag}` as a boolean"
                )
        if entry["review_status"] not in REVIEW_STATUSES:
            raise ValueError(
                f"copyleft allowlist entry for {entry['package']!r} has review_status "
                f"{entry['review_status']!r}; expected one of {sorted(REVIEW_STATUSES)}"
            )
        if not isinstance(entry["redistributed"], bool):
            raise ValueError(
                f"copyleft allowlist entry for {entry['package']!r} must record "
                f"`redistributed` as a boolean, not {entry['redistributed']!r}"
            )
        key = normalize_name(entry["package"])
        if key in by_name:
            raise ValueError(f"duplicate copyleft allowlist entry for {entry['package']!r}")
        by_name[key] = entry
    return by_name


def _installed_metadata() -> dict[str, object]:
    import importlib.metadata as md

    found: dict[str, object] = {}
    for dist in md.distributions():
        name = dist.metadata["Name"]
        if name:
            found.setdefault(normalize_name(name), dist.metadata)
    return found


def raw_license_from_metadata(metadata) -> str:
    """PEP 639 resolution order: expression, then classifiers, then free text.

    Free text is last because it is the least trustworthy field in practice —
    `pytokens` stores its whole licence body there and `isoduration` stores the
    string "UNKNOWN", while both carry a usable classifier.
    """
    expression = metadata.get("License-Expression")
    if expression and expression.strip():
        return expression.strip()

    # Some projects paste the entire licence body into this field. Keep the
    # first line so the alias table matches on something bounded, and so an
    # unrecognised value is reported readably rather than as 20 lines.
    raw_free_text = (metadata.get("License") or "").strip()
    free_text = raw_free_text.splitlines()[0].strip() if raw_free_text else ""

    classifiers = [
        c.split("::")[-1].strip()
        for c in (metadata.get_all("Classifier") or [])
        if c.startswith("License ::")
    ]
    if classifiers:
        joined = "; ".join(classifiers)
        identifier = _RAW_LICENSE_ALIASES.get(re.sub(r"\s+", " ", joined).strip().lower())
        if identifier in _GENERIC_IDENTIFIERS and free_text:
            # The classifier only names a family (e.g. "BSD License"). If the
            # free-text field names a specific licence this gate recognises,
            # that is strictly more information — prefer it. Anything the gate
            # does not recognise is ignored here and the family label stands,
            # so this cannot turn a known licence into an unknown one.
            specific, tier = classify(free_text)
            if tier != UNKNOWN and specific not in _GENERIC_IDENTIFIERS:
                return free_text
        return joined

    return free_text


def classify(raw: str) -> tuple[str, str]:
    """Map a raw licence string to (identifier, tier). Unrecognised -> unknown."""
    key = re.sub(r"\s+", " ", raw).strip().lower()
    identifier = _RAW_LICENSE_ALIASES.get(key)
    if identifier is None:
        return (raw or "(no licence metadata)", UNKNOWN)
    return (identifier, LICENSE_TIERS[identifier])


def resolve(
    locked_set: list[dict], installed: dict[str, object], overrides: dict[str, dict]
) -> tuple[list[Package], list[str]]:
    """Resolve every locked package's licence. Returns (packages, problems).

    Pure in its inputs so each failure rule below is directly testable; `collect`
    is the thin wrapper that reads them off disk and out of the environment.
    """
    problems: list[str] = []
    packages: list[Package] = []

    for locked in locked_set:
        name, version = locked["name"], locked["version"]
        shipped = SHIPPED_GROUP in locked["groups"]
        metadata = installed.get(name)
        override = overrides.get(name)
        installed_version = (metadata.get("Version") or "").strip() if metadata else ""
        group = SHIPPED_GROUP if shipped else "dev"
        # Two different questions, deliberately kept apart: does this package
        # ship in the image (for the report), and is it installed on THIS
        # machine (for contradicting an override that says it cannot be).
        excluded_from_image = shipped and marker_excludes(
            locked.get("markers"), SHIPPED_GROUP, IMAGE_ENVIRONMENT
        )
        excluded_locally = marker_excludes(locked.get("markers"), group)

        if override is not None:
            if (
                override["unreadable_reason"] == "marker-excluded"
                and metadata is not None
                and excluded_locally
            ):
                # Only a contradiction where the marker actually excludes the
                # package. On Windows (or a supported CPython below 3.11.3) the
                # marker DOES apply, the package IS installed, and the record is
                # still correct — telling the operator to delete it there would
                # break every other platform.
                problems.append(
                    f"{name}: license override claims the package is marker-excluded here, "
                    f"but it IS installed and its marker applies on this platform — read "
                    f"its real metadata and remove the override"
                )
            if override["version"] != version:
                problems.append(
                    f"{name}: license override records version {override['version']}, "
                    f"but poetry.lock pins {version} — re-verify the licence and update "
                    f"{OVERRIDES_FILE.relative_to(ROOT)}"
                )
            license_id, tier = classify(override["license"])
            # Cross-check the reviewed fact against reality wherever reality is
            # available, so an override cannot quietly outlive the truth. The
            # comparison is on the raw string, not the classified tier: if the
            # package now declares something this gate does not recognise, that
            # is exactly the case deny-by-default exists for, and letting the
            # override stand would make this file a silent bypass of it.
            # Only cross-check against metadata for the release the lock names;
            # otherwise a mismatch would blame the reviewed record for what is
            # actually an out-of-date environment.
            observed_raw = ""
            if metadata is not None:
                if installed_version and installed_version != version:
                    problems.append(
                        f"{name}: poetry.lock pins {version} but {installed_version} is "
                        f"installed — run `poetry install` so the override can be checked "
                        f"against the locked release"
                    )
                else:
                    observed_raw = raw_license_from_metadata(metadata)
            if observed_raw and override["unreadable_reason"] == "no-licence-metadata":
                problems.append(
                    f"{name}: license override claims the package declares no licence "
                    f"metadata, but it declares {observed_raw!r} — remove the override"
                )
            if observed_raw:
                observed_id, observed_tier = classify(observed_raw)
                if observed_tier == UNKNOWN:
                    problems.append(
                        f"{name}: license override says {license_id}, but the installed "
                        f"package declares a licence this gate does not recognise: "
                        f"{observed_raw!r} — read it and either extend the alias table or "
                        f"correct the override"
                    )
                elif observed_id != license_id:
                    problems.append(
                        f"{name}: license override says {license_id}, but the installed "
                        f"package's own metadata says {observed_id}"
                    )
            packages.append(
                Package(
                    name,
                    version,
                    shipped,
                    license_id,
                    tier,
                    "override",
                    override["license"],
                    excluded_from_image,
                )
            )
            continue

        if metadata is None:
            problems.append(
                f"{name} {version}: not installed and not in "
                f"{OVERRIDES_FILE.relative_to(ROOT)} — run `poetry install`, or record "
                f"its licence with evidence if it cannot be installed on this platform"
            )
            continue

        if (
            installed_version
            and installed_version != version
            and name not in VERSION_EXEMPT_PACKAGES
        ):
            # The licence would be read from a different release than the one
            # the report attributes it to. A relicensing that happened in
            # exactly that bump would be invisible.
            problems.append(
                f"{name}: poetry.lock pins {version} but {installed_version} is installed — "
                f"run `poetry install` so the licence is read from the locked release"
            )
            continue

        raw = raw_license_from_metadata(metadata)
        license_id, tier = classify(raw)
        packages.append(
            Package(name, version, shipped, license_id, tier, "metadata", raw, excluded_from_image)
        )

    return packages, problems


def collect() -> tuple[list[Package], list[str]]:
    """`resolve` over this repository's lockfile and this interpreter's packages."""
    return resolve(locked_packages(), _installed_metadata(), load_overrides())


def enforce_policy(
    packages: list[Package],
    allowlist: dict[str, dict] | None = None,
    overrides: dict[str, dict] | None = None,
    locked_names: set[str] | None = None,
    reverse: dict[str, set[str]] | None = None,
    source_root: Path | None = None,
    direct: set[str] | None = None,
    distributions=None,
) -> list[str]:
    """Deny-by-default: strong copyleft never passes, weak copyleft needs review.

    `locked_names` is what a reviewed record is checked for staleness against —
    the *lockfile*, not `packages`. Those differ whenever a package fails to
    resolve (not installed, version drift), and conflating them told an operator
    to delete a reviewed copyleft record merely because the dev group was not
    installed.
    """
    if allowlist is None:
        allowlist = load_copyleft_allowlist()
    if overrides is None:
        overrides = load_overrides()
    if locked_names is None:
        locked_names = {p["name"] for p in locked_packages()}

    problems: list[str] = []
    by_name = {pkg.name: pkg for pkg in packages}

    for pkg in packages:
        where = "REDISTRIBUTED" if pkg.shipped else "development/CI only"

        if pkg.tier == UNKNOWN:
            # Never waivable: an unrecognised licence string cannot be reviewed
            # into permissiveness, only identified. Extend LICENSE_TIERS and
            # _RAW_LICENSE_ALIASES once the actual licence has been read.
            problems.append(
                f"{pkg.name} {pkg.version} ({where}) declares a licence this gate does not "
                f"recognise: {pkg.raw!r}. Read the licence, then add it to LICENSE_TIERS and "
                f"_RAW_LICENSE_ALIASES in {Path(__file__).name}"
            )
            continue

        if pkg.tier == STRONG_COPYLEFT:
            # Never waivable either, in EITHER group. A GPL/AGPL dependency is
            # the finding this whole pass exists to catch; there is no reviewed
            # entry that makes one acceptable, so none is consulted.
            problems.append(
                f"{pkg.name} {pkg.version} is {pkg.license_id} ({where}) — strong copyleft is "
                f"blocking in every group and cannot be waived by a reviewed entry"
            )
            continue

        if pkg.tier == PERMISSIVE:
            continue

        entry = allowlist.get(pkg.name)
        if entry is None:
            problems.append(
                f"{pkg.name} {pkg.version} is {pkg.license_id} ({pkg.tier}, {where}) with no "
                f"reviewed entry in {COPYLEFT_ALLOWLIST_FILE.relative_to(ROOT)}"
            )
            continue
        if classify(entry["license"])[0] != pkg.license_id:
            problems.append(
                f"{pkg.name}: reviewed entry covers {entry['license']}, but the package is "
                f"{pkg.license_id} — re-review it"
            )
        if entry["redistributed"] != pkg.shipped:
            problems.append(
                f"{pkg.name}: reviewed entry records redistributed={entry['redistributed']}, "
                f"but poetry.lock now makes it redistributed={pkg.shipped} — re-review it"
            )

    for name in sorted(allowlist):
        if name not in locked_names:
            problems.append(
                f"{name}: reviewed non-permissive entry exists but the package is no longer "
                f"in poetry.lock — delete the entry"
            )
            continue
        pkg = by_name.get(name)
        if pkg is not None and pkg.tier == PERMISSIVE:
            # Relicensing in the permissive direction is the happy case, but the
            # record must not survive it: the report would keep publishing a
            # "non-permissive, reviewed" claim about a package that is now MIT.
            problems.append(
                f"{name}: reviewed non-permissive entry exists but the package is now "
                f"{pkg.license_id} (permissive) — delete the entry"
            )

    problems += verify_recorded_facts(
        {n: e for n, e in allowlist.items() if n in locked_names},
        reverse=reverse,
        source_root=source_root,
        direct=direct,
        distributions=distributions,
    )

    for name in sorted(overrides):
        if name not in locked_names:
            problems.append(
                f"{name}: license override exists but the package is no longer in "
                f"poetry.lock — delete the entry"
            )

    return problems


def direct_dependencies(pyproject_path: Path | None = None) -> set[str]:
    """Packages QueryGate itself declares, across both Poetry groups.

    `required_by` alone cannot distinguish "nothing depends on it" from
    "QueryGate depends on it directly" — both render as an empty set — and
    directness is exactly what a copyleft argument turns on.
    """
    path = pyproject_path if pyproject_path is not None else ROOT / "pyproject.toml"
    data = tomllib.loads(path.read_text())
    poetry = data.get("tool", {}).get("poetry", {})
    names: set[str] = set()
    for table in (
        poetry.get("dependencies") or {},
        (poetry.get("group") or {}).get("dev", {}).get("dependencies") or {},
    ):
        names |= {normalize_name(name) for name in table if name != "python"}
    return names


def verify_recorded_facts(
    allowlist: dict[str, dict],
    reverse: dict[str, set[str]] | None = None,
    source_root: Path | None = None,
    direct: set[str] | None = None,
    distributions=None,
) -> list[str]:
    """Check every machine-checkable claim a reviewed record makes.

    This is what keeps a record honest between reviews. The legal reading in
    `reason` cannot be verified here; its factual premises can, and a premise
    that has silently changed is exactly how a stale waiver survives.
    """
    if reverse is None:
        reverse = reverse_dependencies()
    if direct is None:
        direct = direct_dependencies()
    problems: list[str] = []
    for name, entry in sorted(allowlist.items()):
        facts = entry["facts"]

        recorded = {normalize_name(n) for n in facts["required_by"]}
        actual = reverse.get(name, set())
        if recorded != actual:
            problems.append(
                f"{name}: reviewed record says it is required by "
                f"{sorted(recorded) or '[]'}, but poetry.lock says {sorted(actual) or '[]'} "
                f"— the dependency path changed, so re-review it"
            )

        claimed_direct = facts["declared_direct_dependency"]
        actual_direct = name in direct
        if claimed_direct != actual_direct:
            problems.append(
                f"{name}: reviewed record says declared_direct_dependency="
                f"{claimed_direct}, but pyproject.toml says {actual_direct} — a direct "
                f"dependency is a different copyleft question, so re-review it"
            )

        claimed_import = facts["imported_by_querygate_source"]
        actual_import = imported_by_source(name, source_root, distributions)
        if actual_import is None:
            # Fail closed: an unresolvable import name must never be read as
            # "not imported", because that is the answer the record wants.
            problems.append(
                f"{name}: cannot resolve which module(s) this package installs, so its "
                f"imported_by_querygate_source={claimed_import} claim cannot be checked "
                f"— run `poetry install`, or re-review the record by hand"
            )
        elif claimed_import != actual_import:
            problems.append(
                f"{name}: reviewed record says imported_by_querygate_source="
                f"{claimed_import}, but the source tree says {actual_import} — this is a "
                f"premise of the copyleft argument, so re-review it"
            )
    return problems


PYPI_METADATA_URL = "https://pypi.org/pypi/{name}/{version}/json"


def pypi_declared_license(name: str, version: str, opener=None) -> str:
    """The licence a release declares on PyPI, in the same field order.

    Kept separate from `raw_license_from_metadata` because the JSON API exposes
    the same three fields under different names and without an email.Message.
    One deliberate difference: this does NOT apply the generic-classifier-defers-
    to-specific-free-text step, because the records it checks are for packages
    whose classifier is what was recorded in the first place. A package that
    declares a generic family classifier beside a specific free-text licence
    would compare unequal here; none of the override records has that shape, and
    a spurious mismatch is a nightly failure to investigate, not a silent pass.
    """
    import json as _json
    import ssl
    import urllib.request

    url = PYPI_METADATA_URL.format(name=name, version=version)
    if opener is not None:
        response_cm = opener(url, timeout=30)
    else:
        # Python's default trust store is the platform's, which on a stock macOS
        # install has no usable CA path — the same reason `pip` bundles certifi.
        # certifi is a `main`-group dependency, so it is present wherever this
        # script can run at all; fall back to the platform store if it somehow
        # is not.
        try:
            import certifi

            context = ssl.create_default_context(cafile=certifi.where())
        except ImportError:  # pragma: no cover - certifi is a main dependency
            context = ssl.create_default_context()
        # Fixed https PyPI host, built from PYPI_METADATA_URL — never caller input.
        response_cm = urllib.request.urlopen(url, timeout=30, context=context)
    with response_cm as response:
        info = _json.loads(response.read().decode())["info"]

    expression = (info.get("license_expression") or "").strip()
    if expression:
        return expression
    classifiers = [
        c.split("::")[-1].strip()
        for c in info.get("classifiers") or []
        if c.startswith("License ::")
    ]
    if classifiers:
        return "; ".join(classifiers)
    free_text = (info.get("license") or "").strip()
    return free_text.splitlines()[0].strip() if free_text else ""


def verify_overrides_against_pypi(
    overrides: dict[str, dict] | None = None, opener=None
) -> list[str]:
    """Re-read every override record's licence from PyPI and compare.

    Four of the redistributed packages are Windows-only or marker-excluded, so
    no ordinary run can ever read their metadata locally, and a fifth declares
    no licence metadata at all. Without this they would rest permanently on a
    hand-typed snapshot. This needs network, so it is deliberately NOT part of
    `--check`: it runs in the nightly workflow, where a failure is actionable
    and a flaky network is not blocking anybody's commit.
    """
    if overrides is None:
        overrides = load_overrides()
    problems: list[str] = []
    for name, entry in sorted(overrides.items()):
        try:
            observed = pypi_declared_license(entry["package"], entry["version"], opener)
        except Exception as exc:  # network, 404, malformed payload
            problems.append(
                f"{name}: could not read {entry['package']} {entry['version']} from PyPI "
                f"({type(exc).__name__}: {exc})"
            )
            continue
        recorded_id, _ = classify(entry["license"])
        observed_id, observed_tier = classify(observed)
        if not observed:
            # PyPI declaring nothing either is not a mismatch — for a record
            # whose whole premise is "this package declares no licence
            # metadata", it is independent corroboration. For any other record
            # it means the recorded licence has no upstream basis at all.
            if entry["unreadable_reason"] != "no-licence-metadata":
                problems.append(
                    f"{name}: record says {recorded_id}, but PyPI declares no licence "
                    f"metadata for {entry['package']} {entry['version']} at all — "
                    f"re-verify the record against the release's own LICENSE file"
                )
            continue
        if observed_tier == UNKNOWN:
            problems.append(
                f"{name}: PyPI declares {observed!r}, which this gate does not recognise "
                f"— read the licence and update the alias table or the record"
            )
        elif observed_id != recorded_id:
            problems.append(
                f"{name}: record says {recorded_id}, but PyPI says {observed_id} for "
                f"{entry['package']} {entry['version']} — re-verify and update the record"
            )
    return problems


def unconfirmed_reviews(
    allowlist: dict[str, dict] | None = None, packages: list[Package] | None = None
) -> list[tuple[dict, bool]]:
    """Unconfirmed copyleft records as (record, redistributed), redistributed first.

    `redistributed` is taken from the lockfile-derived `Package` whenever one is
    available, never from the record describing itself — the same rule the
    report's Scope line follows.
    """
    if allowlist is None:
        allowlist = load_copyleft_allowlist()
    shipped_by_name = {p.name: p.shipped for p in packages or []}
    drafts = [
        (entry, shipped_by_name.get(normalize_name(entry["package"]), entry["redistributed"]))
        for entry in allowlist.values()
        if entry["review_status"] != "approved"
    ]
    return sorted(drafts, key=lambda pair: (not pair[1], pair[0]["package"]))


def _table(rows: list[Package]) -> list[str]:
    lines = ["| Package | Version | Licence |", "| --- | --- | --- |"]
    for pkg in rows:
        marker = " ᵈ" if pkg.source == "override" else ""
        lines.append(f"| `{pkg.name}` | {pkg.version} | {pkg.license_id}{marker} |")
    return lines


def render(
    packages: list[Package],
    allowlist: dict[str, dict] | None = None,
    overrides: dict[str, dict] | None = None,
) -> str:
    shipped = [p for p in packages if p.shipped]
    dev_only = [p for p in packages if not p.shipped]
    if allowlist is None:
        allowlist = load_copyleft_allowlist()
    if overrides is None:
        overrides = load_overrides()
    overridden = [p for p in packages if p.source == "override"]
    by_name = {p.name: p for p in packages}
    optional_edges = optional_dependency_edges()
    drafts = unconfirmed_reviews(allowlist, packages)
    drafts_shipped = [pair for pair in drafts if pair[1]]

    counts: dict[str, int] = {}
    for pkg in packages:
        counts[pkg.license_id] = counts.get(pkg.license_id, 0) + 1

    out: list[str] = []
    add = out.append

    add("# Third-party licences")
    add("")
    add(
        "**Generated file — do not edit by hand.** Regenerate with `make license-report`; "
        "`make license-check` fails if it drifts from `poetry.lock` or if any dependency "
        "carries a licence that is not permissive and not individually recorded. The check "
        "runs in the unit suite (`tests/unit/test_third_party_licenses.py`) and in "
        "`make release-check`."
    )
    add("")
    if drafts:
        add(
            f"> ⚠️ **{len(drafts)} non-permissive dependencies are recorded but not yet "
            f"confirmed by a human"
            + (
                f", {len(drafts_shipped)} of them in the redistributed set"
                if drafts_shipped
                else ""
            )
            + ".** The gate passes on those records; that is not the same as the question "
            "being settled. See the review section below."
        )
        add("")
    marker_excluded = sorted(p.name for p in shipped if p.marker_excluded_from_image)
    add(
        f"Source of truth: `poetry.lock` — **{len(packages)} Python packages**, of which "
        f"**{len(shipped)}** are in the `main` group and **{len(dev_only)}** are not "
        "redistributed — development, test, and CI tooling."
    )
    if marker_excluded:
        add("")
        add(
            f"Of those {len(shipped)}, **{len(shipped) - len(marker_excluded)}** install "
            f"into the published Linux image; {len(marker_excluded)} carry an environment "
            f"marker that excludes them there ("
            + ", ".join(f"`{n}`" for n in marker_excluded)
            + "). That is why the CycloneDX SBOM in `dist/`, which lists only what installs, "
            f"has {len(shipped) - len(marker_excluded)} third-party components where this "
            f"report has {len(shipped)} rows. This report deliberately keeps them: a "
            "Windows-only wheel in the `main` group is still installed for a Windows "
            "operator, and its licence still counts."
        )
    add("")
    add(
        "**What “redistributed” means here.** The `main` group is what the published "
        "container image contains: `Dockerfile` builds it with "
        "`poetry install --no-root --only main`, minus any package whose environment marker "
        "excludes it on the image's platform. The wheel and source distribution contain none "
        "of these packages: QueryGate's wheel declares only its **direct** requirements, and "
        "`pip` resolves the rest transitively, so a `pip install querygate` fetches them from "
        "PyPI — subject to the same environment markers, and to pip's own resolution against "
        "the declared version ranges rather than to this lockfile's pins. `certifi` is one of "
        "the transitive ones — it is not named in QueryGate's own metadata at all. The `dev` "
        "group is neither shipped nor fetched by a consumer."
    )
    add("")
    add(
        "**Scope limit.** This inventory covers Python packages in `poetry.lock` only. The "
        "container image additionally layers a Debian `bookworm` userland and Microsoft's "
        "`msodbcsql18` ODBC driver (installed under `ACCEPT_EULA=Y`, its own proprietary "
        "terms). Those are not Python packages and are not in `poetry.lock`, so this gate "
        "does not see them — they are assessed separately in "
        "`docs/CONTAINER_IMAGE_LICENCES.md`."
    )
    add("")
    add(
        "Licence identifiers are read from each package's own metadata in this order: "
        "`License-Expression` (authoritative under PEP 639), then Trove classifiers, then "
        "the free-text `License` field — except that a classifier naming only a licence "
        "*family* defers to a more specific recognised free-text value. PEP 639 ranks only "
        "the first of those; it deprecates the other two without ordering them, so the rest "
        "is this tool's own choice, made because some packages put their entire licence body "
        "or the string `UNKNOWN` in the free-text field."
    )
    add("")
    add(
        "This report and the CycloneDX SBOM in `dist/` can name the same licence differently, "
        "because they map the same declared metadata through different tables. Neither is "
        "authoritative on its own — the package's own bundled licence text is. Where they "
        "differ, this report's identifier and the evidence behind it are stated here."
    )
    add("")
    add("## Summary")
    add("")
    add("| Licence | Packages |")
    add("| --- | --- |")
    for license_id in sorted(counts):
        add(f"| {license_id} | {counts[license_id]} |")
    add("")
    strong = sorted({p.license_id for p in packages if p.tier == STRONG_COPYLEFT})
    if strong:
        add(
            f"> 🛑 **BLOCKING: strong-copyleft package(s) present ({', '.join(strong)}).** "
            "Strong copyleft cannot be waived by a reviewed entry, and this report must not "
            "be published in this state."
        )
    else:
        add(
            "No GPL or AGPL licence is a locked package, in either group — both GPL MySQL "
            "drivers appear in `poetry.lock` only inside SQLAlchemy's unselected `extras`, "
            "which Poetry never resolves. Strong copyleft is blocking in every group and "
            "cannot be waived by a reviewed entry."
        )
    add("")

    if allowlist:
        add("## Non-permissive licences — recorded, review pending")
        add("")
        add(
            "Every entry below is a weak-copyleft licence that the deny-by-default gate "
            "refuses unless a record exists in `security/copyleft-license-allowlist.json`. "
            "Each record names the package, the licence, whether QueryGate redistributes it, "
            "and why it is acceptable. **A record marked `draft` is drafted analysis awaiting "
            "owner and counsel confirmation. It is not legal advice, it is not settled, and "
            "the gate passing over it is not evidence that the question is closed.**"
        )
        add("")
        for name in sorted(allowlist):
            entry = allowlist[name]
            pkg = by_name.get(name)
            # Derive the legally load-bearing sentence from the lockfile, never
            # from the record being described by it.
            redistributed = pkg.shipped if pkg is not None else entry["redistributed"]
            scope = (
                "redistributed with QueryGate"
                if redistributed
                else "development/CI only — never redistributed"
            )
            facts = entry["facts"]
            requirers = ", ".join(
                f"`{n}`" + (" (optional extra)" if (n, name) in optional_edges else "")
                for n in sorted(facts["required_by"])
            )
            add(f"### `{name}` — {entry['license']} ({entry['review_status']})")
            add("")
            add(f"- **Scope:** {scope}.")
            add(
                "- **Required by:** "
                + (requirers if requirers else "nothing else in the lockfile")
                + (
                    " — and declared directly by QueryGate."
                    if facts["declared_direct_dependency"]
                    else "; QueryGate does not declare it directly."
                )
            )
            add(
                "- **Imported by QueryGate source:** "
                + ("yes" if facts["imported_by_querygate_source"] else "no")
                + "."
            )
            add(
                "- **Reason** (drafted legal reading — the three facts above are "
                f"machine-checked on every run, this is not): {entry['reason']}"
            )
            add(f"- **Recorded:** {entry['added']}")
            add("")

    add("## MySQL driver note")
    add("")
    asyncmy = by_name.get("asyncmy")
    add(
        "MySQL client libraries are a well-known copyleft trap: `mysqlclient` and "
        "`mysql-connector-python` are both GPL-licensed, and neither is a locked package "
        "here (both appear only inside SQLAlchemy's unselected `extras`). QueryGate's MySQL "
        "support uses **`asyncmy`**, "
        + (
            f"which this pass resolves to **{asyncmy.license_id}** from its own declared "
            f"`{asyncmy.raw}`."
            if asyncmy is not None
            else "which is not present in this lockfile."
        )
    )
    add("")

    add("## Redistributed with QueryGate (`main` group)")
    add("")
    out.extend(_table(shipped))
    add("")
    add("## Not redistributed (development, test, and CI tooling)")
    add("")
    out.extend(_table(dev_only))
    add("")

    if overridden:
        add("## ᵈ Licences not readable from a local install")
        add("")
        add(
            "These packages' licences are recorded from the evidence below rather than read "
            "from local metadata — either because the package cannot be installed here "
            "(a Windows-only wheel, or an environment marker that does not apply), or "
            "because it declares no licence metadata at all. Two things keep these records "
            "from becoming stale snapshots: when such a package *is* installed and does "
            "declare a licence, `make license-check` cross-checks the record against the "
            "real metadata and fails on a mismatch; and the nightly workflow re-reads every "
            "record straight from PyPI "
            "(`scripts/check_licenses.py --verify-overrides`) and fails if what upstream "
            "declares no longer matches."
        )
        add("")
        add("| Package | Version | Licence | Why not readable locally | Evidence |")
        add("| --- | --- | --- | --- | --- |")
        for pkg in overridden:
            entry = overrides[pkg.name]
            add(
                f"| `{pkg.name}` | {pkg.version} | {pkg.license_id} | "
                f"{entry['reason']} | {entry['evidence']} |"
            )
        add("")

    add("---")
    add("")
    add(
        "*This report states the licences the dependencies themselves declare. It is "
        "evidence for a licence review, not a legal opinion, and it says nothing about "
        "QueryGate's own licence.*"
    )
    add("")
    return "\n".join(out)


def run(write: bool) -> int:
    packages, problems = collect()
    problems += enforce_policy(packages)

    if problems:
        print("Dependency licence gate FAILED:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            f"\n{len(problems)} problem(s). A copyleft or unrecognised licence is a blocking "
            "finding — report it, do not work around it.",
            file=sys.stderr,
        )
        return 1

    rendered = render(packages)
    if write:
        REPORT_FILE.write_text(rendered)
        print(f"wrote {_display(REPORT_FILE)} ({len(packages)} packages)")
    else:
        current = REPORT_FILE.read_text() if REPORT_FILE.exists() else ""
        if current != rendered:
            print(
                f"{_display(REPORT_FILE)} is out of date with poetry.lock.\n"
                "Regenerate it with `make license-report`.",
                file=sys.stderr,
            )
            return 1
        shipped = sum(1 for p in packages if p.shipped)
        print(
            f"dependency licence gate OK: {len(packages)} locked packages "
            f"({shipped} redistributed), report up to date"
        )

    # Passing the gate is not the same as the licence questions being settled.
    # Say so on every run, loudly enough that a green release-check cannot be
    # read as "the copyleft findings are resolved".
    for entry, redistributed in unconfirmed_reviews(packages=packages):
        scope = "REDISTRIBUTED" if redistributed else "dev-only"
        print(
            f"NOTICE: {entry['package']} ({entry['license']}, {scope}) is recorded but its "
            f"review is still {entry['review_status']} — not confirmed by the owner"
        )
    return 0


def run_override_verification() -> int:
    problems = verify_overrides_against_pypi()
    if problems:
        print("Override verification FAILED against PyPI:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    overrides = load_overrides()
    print(
        f"override verification OK: {len(overrides)} recorded licence(s) still match what "
        f"PyPI declares"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write", action="store_true", help="regenerate the report file")
    group.add_argument(
        "--check",
        action="store_true",
        help="verify the report is current and the licence policy holds (this is the "
        "default when no flag is given)",
    )
    group.add_argument(
        "--verify-overrides",
        action="store_true",
        help="re-read every security/third-party-license-overrides.json record from PyPI "
        "and fail on a mismatch (needs network; run nightly, not per-commit)",
    )
    args = parser.parse_args()
    if args.verify_overrides:
        return run_override_verification()
    return run(write=args.write)


if __name__ == "__main__":
    raise SystemExit(main())
