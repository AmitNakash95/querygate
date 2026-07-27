"""Drift guards for the reproduce-it-yourself security claims (TODO.md item 89).

`docs/SECURITY_POSTURE.md` promises that "a security reviewer under NDA can run
every command in a checkout and see the same result CI does". Nothing mechanically
held that promise: a doc could name a `make` target that had been renamed or never
existed, and a local gate's flags could drift from the CI job it claims to
reproduce. Both failure modes are invisible to every other test in the suite —
which is exactly how the pre-2026-07-27 "Reproduce: `make sast`" line survived
while `make sast` ran only half of the CI SAST job.

These tests make the promise enforceable:

* every `make <target>` cited in a customer/auditor-facing security doc resolves
  to a real `.PHONY` target, and
* the Semgrep gate's flags and rulesets are identical in the Makefile and in
  `.github/workflows/ci.yml`, which deliberately keeps its own copy (see the
  note above `SEMGREP_ARGS`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = _ROOT / "Makefile"
_CI_WORKFLOW = _ROOT / ".github" / "workflows" / "ci.yml"

# The docs a security reviewer / compliance auditor is handed, each of which
# cites `make` commands as reproduction evidence.
_CLAIM_DOCS = (
    _ROOT / "docs" / "SECURITY_POSTURE.md",
    _ROOT / "docs" / "COMPLIANCE_MAPPING.md",
    _ROOT / "docs" / "business" / "openssf-best-practices-answers.md",
)

_MAKE_REF = re.compile(r"\bmake ([a-z][a-z0-9-]*)\b")
_PHONY = re.compile(r"^\.PHONY: (.+)$", re.MULTILINE)


def _phony_targets() -> set[str]:
    """Every target declared `.PHONY` in the Makefile."""
    return {
        target
        for line in _PHONY.findall(_MAKEFILE.read_text(encoding="utf-8"))
        for target in line.split()
    }


def _unwrap_continuations(text: str) -> str:
    """Collapse backslash-newline continuations into single logical lines."""
    return re.sub(r"\\\s*\n\s*", " ", text)


@pytest.mark.parametrize("doc", _CLAIM_DOCS, ids=lambda p: p.name)
def test_every_make_command_cited_in_a_security_doc_exists(doc: Path):
    targets = _phony_targets()
    cited = set(_MAKE_REF.findall(doc.read_text(encoding="utf-8")))
    assert cited, f"{doc.name} cites no `make` command — did the extraction regex break?"
    missing = sorted(cited - targets)
    assert not missing, (
        f"{doc.name} tells a reader to run {missing}, which is not a .PHONY target "
        "in the Makefile. A security/compliance doc must never point at a command "
        "that does not exist — rename the reference or restore the target."
    )


def test_semgrep_flags_match_between_makefile_and_ci():
    """The local Semgrep gate must invoke exactly what the CI SAST job invokes.

    `SEMGREP_ARGS` exists so the installed-binary and container paths agree, but
    ci.yml holds an independent copy of the same list. If they diverge, `make
    semgrep` stops reproducing the gate that actually blocks a push.
    """
    makefile = _unwrap_continuations(_MAKEFILE.read_text(encoding="utf-8"))
    match = re.search(r"^SEMGREP_ARGS \?= (.+)$", makefile, re.MULTILINE)
    assert match, "SEMGREP_ARGS assignment not found in the Makefile"
    make_args = match.group(1).split()

    workflow = _unwrap_continuations(_CI_WORKFLOW.read_text(encoding="utf-8"))
    ci_invocations = [
        line.strip()[len("semgrep ") :]
        for line in workflow.splitlines()
        if line.strip().startswith("semgrep --")
    ]
    assert len(ci_invocations) == 1, (
        f"expected exactly one `semgrep --...` invocation in ci.yml, found "
        f"{len(ci_invocations)} — update this guard if the SAST job was restructured"
    )
    ci_args = ci_invocations[0].split()

    assert sorted(make_args) == sorted(ci_args), (
        "Makefile SEMGREP_ARGS and the ci.yml Semgrep step have drifted.\n"
        f"  Makefile: {make_args}\n"
        f"  ci.yml:   {ci_args}\n"
        "Mirror the change in both, or make the CI step `run: make semgrep` so "
        "there is only one copy (the pattern the `dast` job already uses)."
    )
