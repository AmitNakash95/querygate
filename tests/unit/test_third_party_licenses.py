"""Dependency-licence gate — `scripts/check_licenses.py` / `docs/THIRD_PARTY_LICENSES.md`.

QueryGate ships **proprietary and closed-source** (item 210,
`docs/business/GTM_SAAS.md`), and a copyleft licence in the *redistributed*
dependency set is a blocking legal problem — a reciprocal licence whose
conditions attach to the distributed larger work is exactly what a closed-source
product cannot satisfy. The premise changed with the licence decision (it was
originally written for a source-available flip) and the gate got *more* load
bearing, not less. A one-off spot-check rots the moment `poetry.lock` changes,
so the pass runs here,
in the default unit suite, exactly the way `test_worklist_consistency.py` keeps
the worklist rules enforceable.

Three halves, the third added after review:
  * The *live* guard: the real lockfile passes the real policy, the generated
    report is not stale, its tables actually say what the lockfile says, and
    nothing strongly copyleft appears in either group.
  * *Detector* tests: each rule is fed planted drift and must fire. A licence
    gate that silently passes everything is worse than no gate at all.
  * *Process* tests: `run()`'s exit code, which is what `make license-check`
    and therefore `make release-check` actually depend on.

Note the live tests read the ambient interpreter's installed packages, so they
require a full, current `poetry install` (main **and** dev). Two failures are
environment problems rather than licence regressions, and both say so in their
message: "not installed" (run `poetry install`), "poetry.lock pins X but Y is
installed" (the venv is behind the lock), and "cannot resolve which module(s)
this package installs" (a reviewed record's package is not installed, so its
import claim cannot be checked — which fails closed rather than being assumed).
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile

import pytest

from scripts import check_licenses

pytestmark = pytest.mark.unit


class _FakeMetadata:
    """Minimal stand-in for `importlib.metadata`'s message-like metadata object.

    `get_all` returns `None` rather than `[]` when there are no classifiers,
    because that is what `email.message.Message` does and it is the case the
    production `or []` guard exists for.
    """

    def __init__(self, *, expression=None, classifiers=(), license_field=None, version="1.0.0"):
        self._fields = {
            "License-Expression": expression,
            "License": license_field,
            "Version": version,
        }
        self._classifiers = list(classifiers)

    def get(self, key, default=None):
        return self._fields.get(key) or default

    def get_all(self, key):
        if key != "Classifier":
            return None
        return list(self._classifiers) if self._classifiers else None


# An empty directory, so `imported_by_source` finds nothing to scan in the
# detector tests. Created once; nothing writes to it.
_EMPTY_TREE = pathlib.Path(tempfile.mkdtemp(prefix="querygate-licence-tests-"))


def _locked(name, version="1.0.0", groups=("main",), markers=None):
    return {"name": name, "version": version, "groups": list(groups), "markers": markers}


def _override(name, version="1.0.0", license_="MIT", unreadable_reason="marker-excluded"):
    return {
        name: {
            "package": name,
            "version": version,
            "license": license_,
            "unreadable_reason": unreadable_reason,
            "reason": "planted",
            "evidence": "planted",
            "added": "2026-08-21",
        }
    }


def _allow(
    name,
    license_="MPL-2.0",
    redistributed=False,
    review_status="draft",
    required_by=(),
    imported=False,
    direct=False,
):
    return {
        name: {
            "package": name,
            "license": license_,
            "redistributed": redistributed,
            "facts": {
                "required_by": list(required_by),
                "declared_direct_dependency": direct,
                "imported_by_querygate_source": imported,
            },
            "reason": "planted",
            "added": "2026-08-21",
            "review_status": review_status,
        }
    }


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        import json as _json

        return _json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _pypi(payload=None, error=None):
    """A stand-in for `urllib.request.urlopen` returning one canned payload."""

    def opener(url, timeout=None):
        if error is not None:
            raise error
        return _FakeResponse({"info": payload})

    return opener


def _policy(packages, allowlist=None, overrides=None, locked=None, source_root=None):
    """`enforce_policy` with every input pinned, so no test reads the real repo.

    Fact inputs are pinned too (empty reverse-deps, empty direct set, an empty
    source tree), so planted records verify cleanly instead of being measured
    against this repository. `verify_recorded_facts` is exercised directly by
    its own tests below, and its wiring into the gate by
    `test_the_gate_itself_runs_fact_verification`.
    """
    if locked is None:
        locked = {p.name for p in packages}
    allowlist = allowlist or {}
    return check_licenses.enforce_policy(
        packages,
        allowlist,
        overrides or {},
        locked_names=locked,
        reverse={},
        source_root=source_root if source_root is not None else _EMPTY_TREE,
        direct=set(),
        # Planted packages are not installed, so their import names cannot be
        # resolved from real metadata; supply a stub so the fail-closed rule
        # (exercised by its own test) does not fire in every detector.
        distributions={name: [name] for name in allowlist},  # module -> [dist]
    )


# --- live guard ---------------------------------------------------------------


def test_live_dependency_licences_pass_the_policy():
    packages, problems = check_licenses.collect()
    problems += check_licenses.enforce_policy(packages)
    assert problems == [], (
        "dependency licence gate failed (if these say 'not installed' or 'is "
        "installed', the venv is stale or missing the dev group — run "
        "`poetry install`):\n" + "\n".join(f"  - {p}" for p in problems)
    )
    assert len(packages) == len(check_licenses.locked_packages())


def test_live_report_is_not_stale():
    packages, problems = check_licenses.collect()
    assert problems == []
    current = check_licenses.REPORT_FILE.read_text()
    assert current == check_licenses.render(packages), (
        f"{check_licenses.REPORT_FILE.name} has drifted from poetry.lock — "
        "regenerate it with `make license-report`"
    )


def test_live_report_tables_match_the_lockfile():
    """Staleness alone would not catch a renderer that swapped the two tables.

    Without this, transposing the "redistributed" and "dev only" headings and
    regenerating leaves every other test green while the counsel-facing document
    says certifi is dev-only.
    """
    packages, problems = check_licenses.collect()
    assert problems == []
    rendered = check_licenses.render(packages)
    shipped_heading = "## Redistributed with QueryGate (`main` group)"
    dev_heading = "## Not redistributed (development, test, and CI tooling)"
    override_heading = "## ᵈ Licences not readable from a local install"
    _, _, rest = rendered.partition(shipped_heading)
    shipped_block, _, after = rest.partition(dev_heading)
    # The override-evidence table repeats some rows, so stop at its heading.
    dev_block, _, _ = after.partition(override_heading)

    def names(block):
        return {line.split("`")[1] for line in block.splitlines() if line.startswith("| `")}

    # Both sides come from the LOCKFILE. Comparing the render against `packages`
    # would shrink together with any bug that silently drops packages.
    locked = check_licenses.locked_packages()
    assert names(shipped_block) == {p["name"] for p in locked if "main" in p["groups"]}
    assert names(dev_block) == {p["name"] for p in locked if "main" not in p["groups"]}
    assert names(shipped_block) | names(dev_block) == {p["name"] for p in locked}
    # The single most consequential row in the document.
    assert "certifi" in names(shipped_block)


def test_live_report_discloses_its_scope_limits():
    """The image's OS layer is out of scope; saying so is load-bearing, not prose."""
    packages, _ = check_licenses.collect()
    rendered = check_licenses.render(packages)
    assert "msodbcsql18" in rendered
    assert "The wheel and source distribution contain none of these packages" in rendered
    assert "declares only its **direct** requirements" in rendered
    # Every override-sourced package must be disclosed as such.
    assert "## ᵈ Licences not readable from a local install" in rendered
    for pkg in packages:
        if pkg.source == "override":
            assert f"| `{pkg.name}` |" in rendered.split("not readable from a local install")[1]


def test_no_strongly_copyleft_licence_appears_in_the_lock_at_all():
    """The claim the closed-source distribution rests on, asserted against the real lock.

    Not filtered to the redistributed set: a GPL/AGPL dependency in *either*
    group is the finding this pass exists to catch.
    """
    packages, problems = check_licenses.collect()
    assert problems == []
    assert len(packages) == len(check_licenses.locked_packages())
    offenders = [
        (p.name, p.license_id) for p in packages if p.tier == check_licenses.STRONG_COPYLEFT
    ]
    assert offenders == [], f"GPL/AGPL licence in poetry.lock: {offenders}"


def test_live_reviewed_entries_name_real_locked_packages():
    """Checked against the lockfile, not against what resolved on this machine.

    Asserting against `collect()` would fail with "names no locked package" after
    `poetry install --only main` — untrue, and the same conflation the production
    staleness rule was corrected to avoid.
    """
    allowlist = check_licenses.load_copyleft_allowlist()
    locked = {p["name"]: p for p in check_licenses.locked_packages()}
    for name, entry in allowlist.items():
        assert name in locked, f"reviewed entry for {name!r} names no locked package"
        assert entry["redistributed"] == ("main" in locked[name]["groups"])


def test_live_reviewed_entries_carry_the_not_legal_advice_disclaimer():
    """These records get quoted into the GTM plan without the report's caveats."""
    for name, entry in check_licenses.load_copyleft_allowlist().items():
        assert "NOT A LEGAL OPINION" in entry["reason"], (
            f"{name}'s reason states a legal conclusion without the disclaimer that "
            "travels with it when the record is quoted elsewhere"
        )


def test_live_repo_paths_quoted_in_reviewed_reasons_exist():
    """A grep artifact once got promoted into a record labelled 'reviewed'."""
    import re

    for name, entry in check_licenses.load_copyleft_allowlist().items():
        for path in re.findall(r"`((?:src|tests|scripts)/[\w./-]+)`", entry["reason"]):
            assert (check_licenses.ROOT / path).exists(), f"{name} cites missing path {path}"


def test_live_lockfile_normalizers_agree_with_the_sbom_generator():
    """A silent SBOM component drop would look like a licence-report problem."""
    from scripts import generate_sbom

    for pkg in check_licenses.locked_packages():
        assert generate_sbom._normalize(pkg["name"]) == check_licenses.normalize_name(pkg["name"])


# --- licence resolution -------------------------------------------------------


def test_pep639_expression_wins_over_classifier_and_free_text():
    metadata = _FakeMetadata(
        expression="MIT",
        classifiers=["License :: OSI Approved :: Apache Software License"],
        license_field="BSD-3-Clause",
    )
    assert check_licenses.raw_license_from_metadata(metadata) == "MIT"


def test_classifier_wins_over_free_text():
    """`isoduration` declares `License: UNKNOWN` next to a correct ISC classifier."""
    metadata = _FakeMetadata(
        classifiers=["License :: OSI Approved :: ISC License (ISCL)"],
        license_field="UNKNOWN",
    )
    assert check_licenses.classify(check_licenses.raw_license_from_metadata(metadata)) == (
        "ISC",
        check_licenses.PERMISSIVE,
    )


def test_free_text_licence_body_is_truncated_to_its_first_line():
    """`pytokens` pastes its entire licence text into the `License` field."""
    metadata = _FakeMetadata(
        license_field="MIT License\n\nCopyright (c) 2024 Someone\n\nPermission"
    )
    assert check_licenses.classify(check_licenses.raw_license_from_metadata(metadata))[0] == "MIT"


def test_metadata_with_no_classifier_header_at_all_is_handled():
    """`get_all` returns None, not []; production must not iterate None."""
    metadata = _FakeMetadata(license_field="Apache-2.0")
    assert metadata.get_all("Classifier") is None
    assert check_licenses.raw_license_from_metadata(metadata) == "Apache-2.0"


def test_metadata_declaring_nothing_resolves_to_no_licence_metadata():
    identifier, tier = check_licenses.classify(
        check_licenses.raw_license_from_metadata(_FakeMetadata())
    )
    assert tier == check_licenses.UNKNOWN
    assert identifier == "(no licence metadata)"


def test_unrecognised_licence_string_is_not_guessed():
    identifier, tier = check_licenses.classify("Weird Corporate Licence v3")
    assert tier == check_licenses.UNKNOWN
    assert identifier == "Weird Corporate Licence v3"


def test_gpl_is_recognised_as_strong_copyleft():
    assert check_licenses.classify("GPL-3.0-only")[1] == check_licenses.STRONG_COPYLEFT
    assert (
        check_licenses.classify("GNU General Public License v2 or later (GPLv2+)")[1]
        == check_licenses.STRONG_COPYLEFT
    )


def test_package_names_are_pep503_normalized():
    assert check_licenses.normalize_name("boolean.py") == check_licenses.normalize_name(
        "boolean-py"
    )
    assert check_licenses.normalize_name("Typing_Extensions") == "typing-extensions"


# --- detector tests: each rule must fire on planted drift ---------------------


def test_permissive_package_produces_no_problem():
    """Negative control: without it, a gate that fails everything would pass above."""
    packages, problems = check_licenses.resolve(
        [_locked("widget")], {"widget": _FakeMetadata(expression="MIT")}, {}
    )
    assert problems == []
    assert _policy(packages) == []


def test_locked_package_that_is_neither_installed_nor_overridden_fails():
    _, problems = check_licenses.resolve([_locked("ghost")], {}, {})
    assert len(problems) == 1
    assert "ghost" in problems[0] and "not installed" in problems[0]


def test_installed_version_that_drifts_from_the_lock_fails():
    """Otherwise the report prints the locked version beside another release's licence."""
    packages, problems = check_licenses.resolve(
        [_locked("widget", version="2.0.0")],
        {"widget": _FakeMetadata(expression="MIT", version="1.0.0")},
        {},
    )
    assert packages == []
    assert len(problems) == 1
    assert "pins 2.0.0" in problems[0] and "1.0.0 is installed" in problems[0]


def test_override_version_that_drifts_from_the_lock_fails():
    packages, problems = check_licenses.resolve(
        [_locked("winonly", version="2.0.0")], {}, _override("winonly", version="1.0.0")
    )
    assert any("records version 1.0.0" in p and "pins 2.0.0" in p for p in problems)
    # Still reported, so the report can name it — but `run()` refuses to render
    # while any problem stands, so it can never reach the file unverified.
    assert [p.name for p in packages] == ["winonly"]


def test_override_is_cross_checked_against_the_locked_release_not_whatever_is_installed():
    """Otherwise the operator is told the reviewed record is wrong when the
    real fault is a stale environment."""
    packages, problems = check_licenses.resolve(
        [_locked("crc", version="2.0.0")],
        {"crc": _FakeMetadata(expression="Apache-2.0", version="1.0.0")},
        _override(
            "crc",
            version="2.0.0",
            license_="Apache-2.0",
            unreadable_reason="no-licence-metadata",
        ),
    )
    drift = [p for p in problems if "1.0.0 is installed" in p]
    assert len(drift) == 1
    assert "pins 2.0.0" in drift[0]
    # The cross-check was skipped rather than run against the wrong release, so
    # the record is not blamed for a stale environment.
    assert not any("declares no licence metadata" in p for p in problems)
    assert packages[0].license_id == "Apache-2.0"


def test_marker_excluded_override_for_an_installed_package_fails():
    """`unreadable_reason` is a claim about reality, so reality must be able to
    contradict it — otherwise the field is documentation, not a constraint."""
    _, problems = check_licenses.resolve(
        [_locked("winonly", markers='sys_platform == "definitely-not-this-one"')],
        {"winonly": _FakeMetadata(expression="MIT")},
        _override("winonly", license_="MIT", unreadable_reason="marker-excluded"),
    )
    assert len(problems) == 1
    assert "claims the package is marker-excluded here, but it IS installed" in problems[0]


def test_marker_excluded_override_is_accepted_where_the_marker_does_apply():
    """On Windows, `colorama` and friends DO install and their records are still
    correct. Failing there would make the gate unpassable on a supported platform
    and the remediation ("remove the override") would break every other one."""
    packages, problems = check_licenses.resolve(
        [_locked("winonly", markers='sys_platform == "%s"' % sys.platform)],
        {"winonly": _FakeMetadata(expression="MIT")},
        _override("winonly", license_="MIT", unreadable_reason="marker-excluded"),
    )
    assert problems == []
    assert packages[0].license_id == "MIT"


def test_no_licence_metadata_override_for_a_declaring_package_fails():
    _, problems = check_licenses.resolve(
        [_locked("crc")],
        {"crc": _FakeMetadata(expression="Apache-2.0")},
        _override("crc", license_="Apache-2.0", unreadable_reason="no-licence-metadata"),
    )
    assert len(problems) == 1
    assert "declares no licence metadata, but it declares" in problems[0]


def test_bootstrap_packages_are_exempt_from_the_version_drift_rule():
    """`pip` is locked but bootstrapped by ensurepip, so its installed version is
    environment-managed — the same set `generate_sbom.py` excludes, and the one
    that would otherwise redden the unit suite on a fresh venv."""
    packages, problems = check_licenses.resolve(
        [_locked("pip", version="26.1.2")],
        {"pip": _FakeMetadata(expression="MIT", version="24.0")},
        {},
    )
    assert problems == []
    assert packages[0].license_id == "MIT"


def test_override_contradicted_by_real_installed_metadata_fails():
    _, problems = check_licenses.resolve(
        [_locked("winonly")],
        {"winonly": _FakeMetadata(expression="Apache-2.0")},
        _override("winonly", license_="MIT"),
    )
    assert any("override says MIT" in p and "metadata says Apache-2.0" in p for p in problems)


def test_override_does_not_bypass_the_unrecognised_licence_rule():
    """The overrides file must not become a silent waiver for an unknown licence.

    The package here carries no marker, so the marker-excluded contradiction does
    not fire; the licence it declares is one the gate refuses to guess at, and the
    override must not silence that.
    """
    _, problems = check_licenses.resolve(
        [_locked("winonly")],
        {"winonly": _FakeMetadata(license_field="Business Source License 1.1")},
        _override("winonly", license_="MIT"),
    )
    assert len(problems) == 1
    assert "does not recognise" in problems[0]
    assert "Business Source License 1.1" in problems[0]


def test_override_is_not_contradicted_by_a_package_declaring_no_licence_at_all():
    """`google-crc32c` ships only a `License-File` pointer — nothing to contradict."""
    packages, problems = check_licenses.resolve(
        [_locked("crc")],
        {"crc": _FakeMetadata()},
        _override("crc", license_="Apache-2.0", unreadable_reason="no-licence-metadata"),
    )
    assert problems == []
    assert packages[0].license_id == "Apache-2.0"


def test_unreviewed_weak_copyleft_dependency_fails():
    packages, _ = check_licenses.resolve(
        [_locked("copyleft-thing")], {"copyleft-thing": _FakeMetadata(expression="MPL-2.0")}, {}
    )
    problems = _policy(packages)
    assert len(problems) == 1
    assert "no reviewed entry" in problems[0]
    # The label is what tells a reviewer whether the finding blocks distribution.
    assert "REDISTRIBUTED" in problems[0]


def test_strong_copyleft_in_the_redistributed_set_cannot_be_waived():
    """The finding this entire work package exists to catch. Its sibling covers
    the dev group; without this one, a GPL package in the container image with a
    matching record would pass the gate green."""
    packages, _ = check_licenses.resolve(
        [_locked("gpl-thing", groups=["main"])],
        {"gpl-thing": _FakeMetadata(expression="GPL-3.0-only")},
        {},
    )
    problems = _policy(packages, _allow("gpl-thing", license_="GPL-3.0-only", redistributed=True))
    assert len(problems) == 1
    assert "cannot be waived" in problems[0]
    assert "REDISTRIBUTED" in problems[0]


def test_unreviewed_weak_copyleft_dev_dependency_also_fails():
    """Deny-by-default is stated without a group qualifier, so it must hold in
    the dev group too — otherwise chardet/fqdn/hypothesis/pathspec could lose
    their records silently."""
    packages, _ = check_licenses.resolve(
        [_locked("copyleft-thing", groups=["dev"])],
        {"copyleft-thing": _FakeMetadata(expression="MPL-2.0")},
        {},
    )
    problems = _policy(packages)
    assert len(problems) == 1
    assert "no reviewed entry" in problems[0]
    assert "development/CI only" in problems[0]


def test_reviewed_weak_copyleft_dependency_passes():
    packages, _ = check_licenses.resolve(
        [_locked("copyleft-thing", groups=["dev"])],
        {"copyleft-thing": _FakeMetadata(expression="MPL-2.0")},
        {},
    )
    assert _policy(packages, _allow("copyleft-thing")) == []


def test_strong_copyleft_cannot_be_waived_even_by_a_reviewed_entry():
    """A GPL dependency is the finding; no record makes one acceptable."""
    packages, _ = check_licenses.resolve(
        [_locked("gpl-thing", groups=["dev"])],
        {"gpl-thing": _FakeMetadata(expression="GPL-3.0-only")},
        {},
    )
    problems = _policy(packages, _allow("gpl-thing", license_="GPL-3.0-only"))
    assert len(problems) == 1
    assert "cannot be waived" in problems[0]
    assert "development/CI only" in problems[0]


def test_reviewed_entry_for_a_different_licence_fails():
    packages, _ = check_licenses.resolve(
        [_locked("copyleft-thing", groups=["dev"])],
        {"copyleft-thing": _FakeMetadata(expression="LGPL-2.1-or-later")},
        {},
    )
    problems = _policy(packages, _allow("copyleft-thing", license_="MPL-2.0"))
    assert len(problems) == 1
    assert "covers MPL-2.0" in problems[0] and "is LGPL-2.1-or-later" in problems[0]


def test_dev_only_review_does_not_silently_cover_a_package_that_became_shipped():
    """The rule that makes `chardet`'s dev-only waiver safe to write down."""
    packages, _ = check_licenses.resolve(
        [_locked("copyleft-thing", groups=["main", "dev"])],
        {"copyleft-thing": _FakeMetadata(expression="MPL-2.0")},
        {},
    )
    problems = _policy(packages, _allow("copyleft-thing", redistributed=False))
    assert len(problems) == 1
    assert "records redistributed=False" in problems[0]
    assert "redistributed=True" in problems[0]


def test_reviewed_entry_for_a_now_permissive_package_fails():
    """Otherwise the report keeps publishing 'MPL-2.0, reviewed' about an MIT package."""
    packages, _ = check_licenses.resolve(
        [_locked("relicensed")], {"relicensed": _FakeMetadata(expression="MIT")}, {}
    )
    problems = _policy(packages, _allow("relicensed"))
    assert len(problems) == 1
    assert "now MIT (permissive) — delete the entry" in problems[0]


def test_unconfirmed_reviews_put_redistributed_records_first():
    """`certifi` is alphabetically first too, so the live pin cannot show this."""
    allowlist = {**_allow("aardvark", redistributed=False), **_allow("zebra", redistributed=True)}
    packages, _ = check_licenses.resolve(
        [_locked("aardvark", groups=["dev"]), _locked("zebra", groups=["main"])],
        {
            "aardvark": _FakeMetadata(expression="MPL-2.0"),
            "zebra": _FakeMetadata(expression="MPL-2.0"),
        },
        {},
    )
    ordered = check_licenses.unconfirmed_reviews(allowlist, packages)
    assert [entry["package"] for entry, _ in ordered] == ["zebra", "aardvark"]
    assert [redistributed for _, redistributed in ordered] == [True, False]


def test_unconfirmed_reviews_take_redistribution_from_the_lock_not_the_record():
    packages, _ = check_licenses.resolve(
        [_locked("thing", groups=["main"])], {"thing": _FakeMetadata(expression="MPL-2.0")}, {}
    )
    ordered = check_licenses.unconfirmed_reviews(_allow("thing", redistributed=False), packages)
    assert ordered[0][1] is True


def test_approved_reviews_are_not_reported_as_unconfirmed():
    allowlist = _allow("thing", review_status="approved")
    assert check_licenses.unconfirmed_reviews(allowlist, []) == []


def test_stale_reviewed_entry_for_a_removed_package_fails():
    problems = _policy([], _allow("gone-from-the-lock"), locked={"something-else"})
    assert len(problems) == 1
    assert "gone-from-the-lock" in problems[0]
    assert "reviewed non-permissive entry" in problems[0] and "no longer" in problems[0]


def test_stale_licence_override_for_a_removed_package_fails():
    problems = _policy([], overrides=_override("gone-from-the-lock"), locked={"something-else"})
    assert len(problems) == 1
    assert "license override exists" in problems[0]


def test_unresolved_package_is_not_reported_as_removed_from_the_lock():
    """`poetry install --only main` must not advise deleting the LGPL review record.

    Staleness is measured against the lockfile, not against what resolved. When
    the dev group is simply not installed, the operator gets 'not installed' —
    never 'delete the entry'.
    """
    packages, problems = check_licenses.resolve([_locked("copyleft-thing", groups=["dev"])], {}, {})
    assert packages == []
    problems += _policy(packages, _allow("copyleft-thing"), locked={"copyleft-thing"})
    assert len(problems) == 1
    assert "not installed" in problems[0]
    assert "delete the entry" not in problems[0]


def test_unknown_licence_cannot_be_waived_by_a_reviewed_entry():
    packages, _ = check_licenses.resolve(
        [_locked("mystery")], {"mystery": _FakeMetadata(license_field="Ask Us Nicely v1")}, {}
    )
    problems = _policy(packages, _allow("mystery", license_="Ask Us Nicely v1"))
    assert len(problems) == 1
    assert "does not recognise" in problems[0]


def test_report_scope_line_follows_the_lock_not_the_reviewed_entry():
    """The one legally load-bearing sentence must not be sourced from the record.

    `enforce_policy` also rejects this disagreement, but `render` is a public
    function with its own callers, so it must not be able to publish
    "never redistributed" about a package the lockfile ships.
    """
    packages, _ = check_licenses.resolve(
        [_locked("copyleft-thing", groups=["main"])],
        {"copyleft-thing": _FakeMetadata(expression="MPL-2.0")},
        {},
    )
    rendered = check_licenses.render(
        packages, allowlist=_allow("copyleft-thing", redistributed=False), overrides={}
    )
    section = rendered.split("### `copyleft-thing`")[1]
    assert "**Scope:** redistributed with QueryGate." in section


def test_a_bad_alias_target_is_refused_at_import_rather_than_crashing_later():
    """A near-miss alias would make `classify` raise KeyError — a gate that
    crashes instead of denying. The module-level guard turns that into a loud
    edit-time error; this pins the guard itself, not just today's clean tables."""
    import importlib

    module = importlib.import_module("scripts.check_licenses")
    source = (check_licenses.ROOT / "scripts" / "check_licenses.py").read_text()
    planted = source.replace(
        '    "mit style": "MIT",',
        '    "mit style": "MIT",\n    "gplv3": "GPL-3.0",',
        1,
    )
    assert planted != source, "alias table anchor moved — update this test"
    namespace: dict = {"__name__": "planted_check_licenses", "__file__": module.__file__}
    with pytest.raises(RuntimeError, match="missing from LICENSE_TIERS"):
        exec(compile(planted, module.__file__, "exec"), namespace)


def test_report_refuses_to_claim_no_gpl_when_a_gpl_package_is_present():
    """The sentence must be derived. `render` is a public function with its own
    callers, and a document whose prose contradicts its own summary table two
    lines above it is worse than no document."""
    packages, _ = check_licenses.resolve(
        [_locked("evil-gpl-lib")], {"evil-gpl-lib": _FakeMetadata(expression="GPL-3.0-only")}, {}
    )
    rendered = check_licenses.render(packages, allowlist={}, overrides={})
    assert "No GPL or AGPL licence is a locked package" not in rendered
    assert "BLOCKING: strong-copyleft package(s) present (GPL-3.0-only)" in rendered


def test_report_states_the_no_gpl_conclusion_when_there_is_no_gpl():
    """Negative control for the rule above."""
    packages, _ = check_licenses.resolve(
        [_locked("fine")], {"fine": _FakeMetadata(expression="MIT")}, {}
    )
    rendered = check_licenses.render(packages, allowlist={}, overrides={})
    assert "No GPL or AGPL licence is a locked package" in rendered
    assert "BLOCKING" not in rendered


def _lock(tmp_path, *entries):
    path = tmp_path / "poetry.lock"
    path.write_text(
        "\n\n".join(
            f'[[package]]\nname = "{name}"\nversion = "{version}"\ngroups = {groups!r}'
            for name, version, groups in entries
        )
        + "\n"
    )
    return path


def test_a_package_locked_twice_is_refused_by_name_rather_than_as_an_install_problem(tmp_path):
    """Poetry can lock two versions under disjoint markers. Only one can ever be
    installed, so the other would trip the version-drift rule forever with advice
    (`poetry install`) that cannot succeed."""
    path = _lock(tmp_path, ("twice", "1.0", ["main"]), ("Twice", "2.0", ["main"]))
    with pytest.raises(ValueError, match="contains 'twice' twice"):
        check_licenses.locked_packages(path)


def test_a_poetry_group_the_report_has_no_wording_for_is_refused(tmp_path):
    """A third group would be filed under "development, test, and CI tooling" —
    correctly not-redistributed, wrongly described, in a doc written for counsel."""
    path = _lock(tmp_path, ("thing", "1.0", ["main", "docs"]))
    with pytest.raises(ValueError, match=r"group\(s\) \['docs'\]"):
        check_licenses.locked_packages(path)


def test_the_two_known_poetry_groups_are_accepted(tmp_path):
    """Negative control, so the guard above cannot pass by rejecting everything."""
    path = _lock(tmp_path, ("a", "1.0", ["main"]), ("b", "2.0", ["dev"]))
    assert [p["name"] for p in check_licenses.locked_packages(path)] == ["a", "b"]


# --- recorded facts are machine-checked, the legal reading is not ------------


def test_reverse_dependencies_reads_the_lock(tmp_path):
    path = tmp_path / "poetry.lock"
    path.write_text(
        '[[package]]\nname = "app"\nversion = "1.0"\ngroups = ["main"]\n'
        '[package.dependencies]\nlib = "*"\n\n'
        '[[package]]\nname = "lib"\nversion = "2.0"\ngroups = ["main"]\n'
    )
    assert check_licenses.reverse_dependencies(path) == {"lib": {"app"}}


def test_imported_by_source_detects_both_import_forms(tmp_path):
    (tmp_path / "a.py").write_text("import chardet\n")
    assert check_licenses.imported_by_source("chardet", tmp_path) is True
    (tmp_path / "a.py").write_text("from chardet.universaldetector import X\n")
    assert check_licenses.imported_by_source("chardet", tmp_path) is True


def test_imported_by_source_detects_a_literal_dynamic_import(tmp_path):
    """`importlib.import_module("x")` is still an import; only a *computed*
    module name is beyond a static check."""
    dists = {"chardet": ["chardet"]}
    (tmp_path / "a.py").write_text('importlib.import_module("chardet")\n')
    assert check_licenses.imported_by_source("chardet", tmp_path, dists) is True
    (tmp_path / "a.py").write_text('__import__("chardet")\n')
    assert check_licenses.imported_by_source("chardet", tmp_path, dists) is True
    (tmp_path / "a.py").write_text("importlib.import_module(name_from_config)\n")
    assert check_licenses.imported_by_source("chardet", tmp_path, dists) is False


def test_imported_by_source_is_not_fooled_by_a_mention_in_prose(tmp_path):
    """A record's claim must not be satisfied by the package name in a comment."""
    (tmp_path / "a.py").write_text("# chardet is deliberately not imported\nx = 'chardet'\n")
    assert check_licenses.imported_by_source("chardet", tmp_path) is False


# A real `packages_distributions()`-shaped map: module name -> distributions.
_STUB_DISTS = {"thing": ["thing"], "unrelated_module": ["some-other-package"]}


def test_recorded_required_by_that_no_longer_matches_the_lock_fails():
    """The dependency path is a premise of every one of these arguments."""
    problems = check_licenses.verify_recorded_facts(
        _allow("thing", required_by=["old-parent"]),
        reverse={"thing": {"new-parent"}},
        source_root=_EMPTY_TREE,
        direct=set(),
        distributions=_STUB_DISTS,
    )
    assert len(problems) == 1
    assert "['old-parent']" in problems[0] and "['new-parent']" in problems[0]


def test_recorded_direct_dependency_claim_that_no_longer_matches_pyproject_fails():
    """`required_by: []` alone cannot tell "nothing needs it" from "we declare it"."""
    problems = check_licenses.verify_recorded_facts(
        _allow("thing", direct=False),
        reverse={},
        source_root=_EMPTY_TREE,
        direct={"thing"},
        distributions=_STUB_DISTS,
    )
    assert len(problems) == 1
    assert "declared_direct_dependency=False" in problems[0]
    assert "pyproject.toml says True" in problems[0]


def test_an_unresolvable_import_name_fails_rather_than_confirming_the_record():
    """Fail closed. `False` is the answer the record wants, so ignorance must
    not be allowed to supply it."""
    problems = check_licenses.verify_recorded_facts(
        _allow("thing", imported=False),
        reverse={},
        source_root=_EMPTY_TREE,
        direct=set(),
        distributions={},
    )
    assert len(problems) == 1
    assert "cannot resolve which module(s)" in problems[0]


def test_top_level_modules_resolves_a_divergent_import_name():
    """`pyyaml` installs `yaml`; string-munging the project name would miss it."""
    assert check_licenses.top_level_modules("pyyaml") == {"_yaml", "yaml"}
    assert check_licenses.top_level_modules("not-a-real-distribution-xyz") is None


def test_imported_by_source_finds_a_divergent_import_name(tmp_path):
    """The regression that motivated resolving modules from real metadata:
    `src/querygate/` imports `yaml`, and `pyyaml` must not read as unimported."""
    (tmp_path / "m.py").write_text("import yaml\n")
    assert check_licenses.imported_by_source("pyyaml", tmp_path) is True


def test_imported_by_source_on_the_real_source_tree_sees_pyyaml():
    """Same rule, against the real tree rather than a fixture."""
    assert check_licenses.imported_by_source("pyyaml") is True


def test_recorded_import_claim_that_no_longer_matches_the_source_fails(tmp_path):
    (tmp_path / "m.py").write_text("import thing\n")
    problems = check_licenses.verify_recorded_facts(
        _allow("thing", imported=False),
        reverse={"thing": set()},
        source_root=tmp_path,
        direct=set(),
        distributions=_STUB_DISTS,
    )
    assert len(problems) == 1
    assert "imported_by_querygate_source=False" in problems[0]
    assert "source tree says True" in problems[0]


def test_accurate_recorded_facts_produce_no_problem(tmp_path):
    """Negative control for both fact rules."""
    (tmp_path / "m.py").write_text("x = 1\n")
    assert (
        check_licenses.verify_recorded_facts(
            _allow("thing", required_by=["parent"], imported=False),
            reverse={"thing": {"parent"}},
            source_root=tmp_path,
            direct=set(),
            distributions=_STUB_DISTS,
        )
        == []
    )


def test_live_recorded_facts_hold_against_the_real_lock_and_source():
    """Every factual premise in the five reviewed records, checked for real."""
    problems = check_licenses.verify_recorded_facts(check_licenses.load_copyleft_allowlist())
    assert problems == [], "\n".join(problems)


def test_the_gate_itself_runs_fact_verification():
    """`verify_recorded_facts` existing is not the same as it being wired in.

    Calls the real `enforce_policy` (not the `_policy` helper, which neutralises
    fact-checking) so that unwiring the call is caught rather than only the rule.
    """
    problems = check_licenses.enforce_policy(
        [],
        _allow("thing", required_by=["a-parent-that-does-not-exist"]),
        {},
        locked_names={"thing"},
        reverse={},
        source_root=_EMPTY_TREE,
        direct=set(),
        distributions=_STUB_DISTS,
    )
    assert len(problems) == 1
    assert "['a-parent-that-does-not-exist']" in problems[0]


def test_fact_verification_skips_records_whose_package_left_the_lock():
    """A removed package gets one 'delete the entry' problem, not that plus two
    bogus fact mismatches about a package that is gone."""
    problems = check_licenses.enforce_policy(
        [],
        _allow("gone", required_by=["whatever"]),
        {},
        locked_names=set(),
        reverse={},
        source_root=_EMPTY_TREE,
        direct=set(),
        distributions={},
    )
    assert len(problems) == 1
    assert "gone" in problems[0] and "no longer" in problems[0]


def test_a_marker_recorded_for_one_group_does_not_answer_for_another():
    """The `greenlet` defect: it is in `main` and `dev`, carries a marker for
    `dev` only, and reading an arbitrary entry out of that mapping reported an
    unconditional runtime dependency as excluded from the image."""
    dev_only = {"dev": 'platform_machine == "definitely-not-this-one"'}
    assert check_licenses.marker_for_group(dev_only, "main") is None
    assert check_licenses.marker_for_group(dev_only, "dev") is not None
    assert check_licenses.marker_excludes(dev_only, "main") is False
    assert check_licenses.marker_excludes(dev_only, "dev") is True
    # A bare string applies to every group.
    assert check_licenses.marker_excludes('sys_platform == "nope"', "main") is True


def test_image_exclusion_is_evaluated_against_the_image_not_this_machine():
    """Otherwise the report depends on who generated it — on Apple Silicon
    (`platform_machine == "arm64"`) it disagreed with CI."""
    arm_only = 'platform_machine == "arm64"'
    assert (
        check_licenses.marker_excludes(arm_only, "main", check_licenses.IMAGE_ENVIRONMENT) is True
    )
    x86_only = 'platform_machine == "x86_64"'
    assert (
        check_licenses.marker_excludes(x86_only, "main", check_licenses.IMAGE_ENVIRONMENT) is False
    )


def test_resolve_asks_the_image_environment_not_the_local_one(monkeypatch):
    """Deterministic on any platform: the marker is written so that the patched
    image environment and the real machine must disagree, and the recorded answer
    has to follow the image. Without this, the difference only shows on hardware
    the developer does not have."""
    monkeypatch.setattr(
        check_licenses,
        "IMAGE_ENVIRONMENT",
        {**check_licenses.IMAGE_ENVIRONMENT, "platform_machine": "s390x"},
    )
    marker = 'platform_machine == "s390x"'
    # Sanity: no machine running this test is an s390x mainframe, so the local
    # answer is "excluded" while the image answer is "installs".
    assert check_licenses.marker_excludes(marker, "main") is True
    packages, problems = check_licenses.resolve(
        [_locked("thing", markers=marker)], {"thing": _FakeMetadata(expression="MIT")}, {}
    )
    assert problems == []
    assert packages[0].marker_excluded_from_image is False


def test_marker_excludes_honours_an_explicit_environment():
    """The `environment` argument must actually reach `Marker.evaluate`."""
    marker = 'platform_machine == "s390x"'
    assert check_licenses.marker_excludes(marker, "main") is True
    assert (
        check_licenses.marker_excludes(
            marker, "main", {**check_licenses.IMAGE_ENVIRONMENT, "platform_machine": "s390x"}
        )
        is False
    )


def test_live_image_install_count_matches_the_sbom_generator():
    """The two shipped artifacts must not disagree about what the image contains.

    Both sides are asked about the IMAGE, not about this machine — otherwise the
    test would pass here and fail on a platform where the two diverge, reported
    as a licence regression. That is structurally the same trap as the `greenlet`
    defect this pass fixed.
    """
    from scripts import generate_sbom

    packages, problems = check_licenses.collect()
    assert problems == []
    installs = {p.name for p in packages if p.shipped and not p.marker_excluded_from_image}
    sbom_side = {
        check_licenses.normalize_name(name)
        for name, _ in generate_sbom.locked_main_packages(
            environment=check_licenses.IMAGE_ENVIRONMENT
        )
    }
    assert installs == sbom_side


def test_live_greenlet_is_not_reported_as_excluded_from_the_image():
    """Regression pin: greenlet is a direct, unmarked `main` dependency of
    QueryGate itself, so no `main`-group marker can exclude it — the marker in its
    lock entry belongs to `dev`."""
    packages, _ = check_licenses.collect()
    greenlet = next(p for p in packages if p.name == "greenlet")
    assert greenlet.shipped is True
    assert greenlet.marker_excluded_from_image is False


def test_direct_dependencies_reads_both_poetry_tables(tmp_path):
    """Only `hypothesis` claims directness live, and it is dev-group — so the
    `main` table, the normalization and the `python` filter are all unpinned
    without this."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.poetry.dependencies]\n"
        'python = "^3.11"\n'
        'PyYAML = "^6.0"\n'
        "[tool.poetry.group.dev.dependencies]\n"
        'hypothesis = "^6.0"\n'
    )
    assert check_licenses.direct_dependencies(pyproject) == {"pyyaml", "hypothesis"}


def test_a_generic_classifier_defers_to_a_specific_free_text_licence():
    """`httpx` declares BSD-3-Clause in free text beside a bare `BSD License`."""
    metadata = _FakeMetadata(
        classifiers=["License :: OSI Approved :: BSD License"],
        license_field="BSD-3-Clause",
    )
    assert check_licenses.raw_license_from_metadata(metadata) == "BSD-3-Clause"


def test_a_generic_classifier_keeps_the_family_label_when_free_text_is_no_better():
    """The inner guard: deferring to another generic value would be a no-op at
    best and a loop at worst."""
    metadata = _FakeMetadata(
        classifiers=["License :: OSI Approved :: BSD License"], license_field="BSD"
    )
    assert check_licenses.raw_license_from_metadata(metadata) == "BSD License"


def test_an_unrecognised_free_text_value_never_displaces_a_known_classifier():
    metadata = _FakeMetadata(
        classifiers=["License :: OSI Approved :: BSD License"],
        license_field="Weird Corporate Licence v3",
    )
    assert check_licenses.raw_license_from_metadata(metadata) == "BSD License"


def test_imported_by_source_resolves_through_the_injected_distribution_map(tmp_path):
    """The seam must resolve module names, not be mistaken for them: a map that
    contains OTHER packages' modules must not make this package read as imported."""
    (tmp_path / "m.py").write_text("import yaml\n")
    dists = {"yaml": ["pyyaml"], "_yaml": ["pyyaml"], "certifi": ["certifi"]}
    assert check_licenses.imported_by_source("pyyaml", tmp_path, dists) is True
    assert check_licenses.imported_by_source("certifi", tmp_path, dists) is False
    assert check_licenses.imported_by_source("absent-package", tmp_path, dists) is None


# --- override records are re-verifiable against PyPI (network mode) ----------


def test_pypi_declared_license_prefers_the_expression():
    opener = _pypi(
        {
            "license_expression": "Apache-2.0",
            "classifiers": ["License :: OSI Approved :: MIT License"],
            "license": "BSD",
        }
    )
    assert check_licenses.pypi_declared_license("x", "1.0", opener) == "Apache-2.0"


def test_pypi_declared_license_falls_back_to_classifier_then_free_text():
    classifier_only = _pypi(
        {"classifiers": ["License :: OSI Approved :: MIT License"], "license": "UNKNOWN"}
    )
    assert check_licenses.pypi_declared_license("x", "1.0", classifier_only) == "MIT License"
    free_text_only = _pypi({"classifiers": [], "license": "Apache 2"})
    assert check_licenses.pypi_declared_license("x", "1.0", free_text_only) == "Apache 2"


def test_pypi_verification_flags_a_record_that_no_longer_matches_upstream():
    opener = _pypi({"license_expression": "GPL-3.0-only"})
    problems = check_licenses.verify_overrides_against_pypi(
        _override("winonly", license_="MIT"), opener
    )
    assert len(problems) == 1
    assert "record says MIT, but PyPI says GPL-3.0-only" in problems[0]


def test_pypi_verification_flags_an_unrecognised_upstream_licence():
    opener = _pypi({"license_expression": "Weird Corporate Licence v3"})
    problems = check_licenses.verify_overrides_against_pypi(
        _override("winonly", license_="MIT"), opener
    )
    assert len(problems) == 1
    assert "does not recognise" in problems[0]


def test_pypi_verification_treats_silence_as_corroboration_for_a_no_metadata_record():
    """`google-crc32c` declares nothing on PyPI either — which is the claim."""
    opener = _pypi({"classifiers": [], "license": ""})
    assert (
        check_licenses.verify_overrides_against_pypi(
            _override("crc", license_="Apache-2.0", unreadable_reason="no-licence-metadata"),
            opener,
        )
        == []
    )


def test_pypi_verification_flags_silence_for_a_marker_excluded_record():
    """For those, the recorded licence is supposed to have an upstream basis."""
    opener = _pypi({"classifiers": [], "license": ""})
    problems = check_licenses.verify_overrides_against_pypi(
        _override("winonly", license_="MIT", unreadable_reason="marker-excluded"), opener
    )
    assert len(problems) == 1
    assert "declares no licence metadata" in problems[0]


def test_pypi_verification_reports_a_network_failure_rather_than_passing():
    opener = _pypi(error=OSError("no route to host"))
    problems = check_licenses.verify_overrides_against_pypi(_override("winonly"), opener)
    assert len(problems) == 1
    assert "could not read" in problems[0]


def test_matching_pypi_metadata_produces_no_problem():
    opener = _pypi({"license_expression": "MIT"})
    assert (
        check_licenses.verify_overrides_against_pypi(_override("winonly", license_="MIT"), opener)
        == []
    )


def test_the_verify_overrides_flag_reaches_the_network_mode(monkeypatch):
    """Without this, deleting the dispatch branch makes the nightly job fall
    through to the hermetic drift check and pass having verified nothing."""
    monkeypatch.setattr(check_licenses, "run_override_verification", lambda: 7)
    monkeypatch.setattr(check_licenses, "run", lambda write: 0)
    monkeypatch.setattr(sys, "argv", ["check_licenses.py", "--verify-overrides"])
    assert check_licenses.main() == 7
    monkeypatch.setattr(sys, "argv", ["check_licenses.py", "--check"])
    assert check_licenses.main() == 0


def test_the_nightly_workflow_still_runs_the_network_verification():
    """docs/RELEASING.md promises this step; nothing else would notice its removal.

    Parsed rather than substring-matched, so commenting the step out — or
    dropping the `if: always()` that keeps a CVE failure from also suppressing
    licence verification — is caught.
    """
    import yaml

    workflow = yaml.safe_load(
        (check_licenses.ROOT / ".github" / "workflows" / "scheduled.yml").read_text()
    )
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "check_licenses.py --verify-overrides" in str(step.get("run", ""))
    ]
    assert len(steps) == 1, "the nightly PyPI re-verification step is missing"
    assert steps[0].get("if") == "always()"


def test_pypi_verification_requests_the_locked_version_not_the_latest_release():
    """Version-pinning is the entire premise of an override record."""
    seen = []

    def opener(url, timeout=None):
        seen.append(url)
        return _FakeResponse({"info": {"license_expression": "MIT"}})

    check_licenses.verify_overrides_against_pypi(
        _override("winonly", version="1.2.3", license_="MIT"), opener
    )
    assert seen == ["https://pypi.org/pypi/winonly/1.2.3/json"]


def test_the_default_opener_uses_https_with_a_certifi_backed_trust_store(monkeypatch):
    """The one branch the injected-opener tests never touch."""
    import urllib.request

    captured = {}

    def fake_urlopen(url, timeout=None, context=None):
        captured["url"] = url
        captured["context"] = context
        return _FakeResponse({"info": {"license_expression": "MIT"}})

    import ssl

    import certifi

    contexts = []
    real_create = ssl.create_default_context

    def recording_create(*args, **kwargs):
        contexts.append(kwargs.get("cafile"))
        return real_create(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", recording_create)
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert check_licenses.pypi_declared_license("widget", "2.0") == "MIT"
    assert captured["url"].startswith("https://pypi.org/pypi/widget/2.0/")
    assert captured["context"] is not None
    # The branch that exists because a stock macOS trust store has no usable CA
    # path — asserting only "some context" would not notice it being removed.
    assert contexts == [certifi.where()]


def test_run_override_verification_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(check_licenses, "verify_overrides_against_pypi", lambda: ["planted"])
    assert check_licenses.run_override_verification() == 1
    assert "planted" in capsys.readouterr().err
    monkeypatch.setattr(check_licenses, "verify_overrides_against_pypi", lambda: [])
    assert check_licenses.run_override_verification() == 0


# --- loader validation --------------------------------------------------------


def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


@pytest.mark.parametrize(
    "field",
    ["package", "license", "redistributed", "facts", "reason", "added", "review_status"],
)
def test_copyleft_allowlist_rejects_each_missing_field(tmp_path, field):
    """Parametrised so that *shrinking* the required set is caught too — dropping
    `reason` would let an unexplained copyleft waiver into a counsel-facing doc."""
    entry = {k: v for k, v in _allow("x")["x"].items() if k != field}
    path = _write(tmp_path, "a.json", [entry])
    with pytest.raises(ValueError, match="missing fields"):
        check_licenses.load_copyleft_allowlist(path)


def test_copyleft_allowlist_rejects_a_duplicate_entry(tmp_path):
    entry = _allow("x")["x"]
    path = _write(tmp_path, "a.json", [entry, dict(entry, license="LGPL-2.1-or-later")])
    with pytest.raises(ValueError, match="duplicate"):
        check_licenses.load_copyleft_allowlist(path)


def test_copyleft_allowlist_rejects_an_invented_review_status(tmp_path):
    """`approved` must mean something; a typo must not read as one."""
    path = _write(tmp_path, "a.json", [_allow("x", review_status="darft")["x"]])
    with pytest.raises(ValueError, match="review_status"):
        check_licenses.load_copyleft_allowlist(path)


def test_copyleft_allowlist_rejects_a_non_boolean_redistributed(tmp_path):
    entry = dict(_allow("x")["x"], redistributed="main")
    path = _write(tmp_path, "a.json", [entry])
    with pytest.raises(ValueError, match="boolean"):
        check_licenses.load_copyleft_allowlist(path)


@pytest.mark.parametrize(
    "field",
    ["package", "version", "license", "unreadable_reason", "reason", "evidence", "added"],
)
def test_overrides_reject_each_missing_field(tmp_path, field):
    """`evidence` in particular: without it the file becomes an unbacked
    licence-assertion escape hatch, which is what its docstring forbids."""
    entry = {k: v for k, v in _override("x")["x"].items() if k != field}
    path = _write(tmp_path, "o.json", [entry])
    with pytest.raises(ValueError, match="missing fields"):
        check_licenses.load_overrides(path)


def test_copyleft_allowlist_rejects_a_facts_block_with_the_wrong_keys(tmp_path):
    entry = dict(_allow("x")["x"], facts={"required_by": []})
    path = _write(tmp_path, "a.json", [entry])
    with pytest.raises(ValueError, match="under `facts`"):
        check_licenses.load_copyleft_allowlist(path)


def test_copyleft_allowlist_rejects_a_non_dict_facts_value(tmp_path):
    entry = dict(_allow("x")["x"], facts=["required_by"])
    path = _write(tmp_path, "a.json", [entry])
    with pytest.raises(ValueError, match="under `facts`"):
        check_licenses.load_copyleft_allowlist(path)


def test_copyleft_allowlist_rejects_a_non_list_required_by(tmp_path):
    entry = dict(_allow("x")["x"], facts=dict(_allow("x")["x"]["facts"], required_by="black"))
    path = _write(tmp_path, "a.json", [entry])
    with pytest.raises(ValueError, match="list of package names"):
        check_licenses.load_copyleft_allowlist(path)


@pytest.mark.parametrize("flag", ["imported_by_querygate_source", "declared_direct_dependency"])
def test_copyleft_allowlist_rejects_a_non_boolean_fact_flag(tmp_path, flag):
    facts = dict(_allow("x")["x"]["facts"], **{flag: "no"})
    entry = dict(_allow("x")["x"], facts=facts)
    path = _write(tmp_path, "a.json", [entry])
    with pytest.raises(ValueError, match=flag):
        check_licenses.load_copyleft_allowlist(path)


def test_overrides_reject_a_duplicate_entry(tmp_path):
    entry = _override("x")["x"]
    path = _write(tmp_path, "o.json", [entry, entry])
    with pytest.raises(ValueError, match="duplicate"):
        check_licenses.load_overrides(path)


def test_overrides_reject_an_invented_unreadable_reason(tmp_path):
    """Keeps the file from growing into a general licence-assertion escape hatch."""
    path = _write(tmp_path, "o.json", [_override("x", unreadable_reason="because")["x"]])
    with pytest.raises(ValueError, match="unreadable_reason"):
        check_licenses.load_overrides(path)


# --- the gate process contract ------------------------------------------------


def test_run_exits_nonzero_when_a_problem_is_found(monkeypatch, capsys):
    """`make license-check` -> `make release-check` depend on this exit code."""
    monkeypatch.setattr(check_licenses, "collect", lambda: ([], ["planted problem"]))
    monkeypatch.setattr(check_licenses, "enforce_policy", lambda *a, **k: [])
    assert check_licenses.run(write=False) == 1
    assert "planted problem" in capsys.readouterr().err


def test_run_exits_nonzero_when_the_report_has_drifted(monkeypatch, tmp_path, capsys):
    report = tmp_path / "THIRD_PARTY_LICENSES.md"
    report.write_text("stale contents")
    monkeypatch.setattr(check_licenses, "REPORT_FILE", report)
    monkeypatch.setattr(check_licenses, "collect", lambda: ([], []))
    monkeypatch.setattr(check_licenses, "enforce_policy", lambda *a, **k: [])
    monkeypatch.setattr(check_licenses, "render", lambda packages: "fresh contents")
    assert check_licenses.run(write=False) == 1
    assert "out of date" in capsys.readouterr().err


def test_run_does_not_write_the_report_when_the_policy_fails(monkeypatch, tmp_path):
    """--write must not paper over a blocking finding."""
    report = tmp_path / "THIRD_PARTY_LICENSES.md"
    monkeypatch.setattr(check_licenses, "REPORT_FILE", report)
    monkeypatch.setattr(check_licenses, "collect", lambda: ([], []))
    monkeypatch.setattr(check_licenses, "enforce_policy", lambda *a, **k: ["planted problem"])
    assert check_licenses.run(write=True) == 1
    assert not report.exists()


def test_run_writes_the_report_when_the_policy_passes(monkeypatch, tmp_path, capsys):
    """Without this, deleting the write leaves `make license-report` a silent no-op."""
    report = tmp_path / "THIRD_PARTY_LICENSES.md"
    monkeypatch.setattr(check_licenses, "REPORT_FILE", report)
    monkeypatch.setattr(check_licenses, "collect", lambda: ([], []))
    monkeypatch.setattr(check_licenses, "enforce_policy", lambda *a, **k: [])
    monkeypatch.setattr(check_licenses, "render", lambda *a, **k: "fresh contents")
    monkeypatch.setattr(check_licenses, "unconfirmed_reviews", lambda **k: [])
    assert check_licenses.run(write=True) == 0
    assert report.read_text() == "fresh contents"
    assert "wrote" in capsys.readouterr().out


def test_run_surfaces_unconfirmed_reviews_even_when_the_gate_passes(monkeypatch, tmp_path, capsys):
    """A green gate must not read as 'the copyleft findings are resolved'."""
    report_text = "fresh contents"
    report = tmp_path / "THIRD_PARTY_LICENSES.md"
    report.write_text(report_text)
    monkeypatch.setattr(check_licenses, "REPORT_FILE", report)
    monkeypatch.setattr(check_licenses, "collect", lambda: ([], []))
    monkeypatch.setattr(check_licenses, "enforce_policy", lambda *a, **k: [])
    monkeypatch.setattr(check_licenses, "render", lambda packages: report_text)
    monkeypatch.setattr(
        check_licenses,
        "unconfirmed_reviews",
        lambda **kwargs: [
            (
                {"package": "certifi", "license": "MPL-2.0", "review_status": "draft"},
                True,
            )
        ],
    )
    assert check_licenses.run(write=False) == 0
    out = capsys.readouterr().out
    assert "NOTICE" in out and "certifi" in out and "REDISTRIBUTED" in out


def test_live_gate_reports_the_certifi_review_as_unconfirmed():
    """Pins the current, honest state: the one blocking finding is not settled."""
    packages, _ = check_licenses.collect()
    drafts = check_licenses.unconfirmed_reviews(packages=packages)
    assert [entry["package"] for entry, _ in drafts][:1] == ["certifi"], (
        "certifi's review is either approved now (update this test and the docs "
        "that call it pending) or no longer first among the unconfirmed records"
    )
    assert drafts[0][1] is True
