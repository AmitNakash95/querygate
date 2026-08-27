#!/usr/bin/env python3
"""Machine-derive the file:line site list for a claim-drift class.

Five consecutive `claim-reviewer` rounds on 2026-08-12 found the same failure:
a claim-drift fix was scoped to the site list in its TODO item, and every
hand-assembled list was shorter than reality. The lists were not careless — they
were built by grepping the surfaces the author was already thinking about (the
marketing HTML, the customer docs) and missed the ones nobody sweeps: `src/`
comments and docstrings, `examples/` (which ships to customers), the root-level
review docs, and `docs/TODO_ARCHIVE.md`.

The fix is to stop hand-assembling. Run this, paste the output into the TODO
item, fix every line, and re-run to confirm empty (or to confirm only the
deliberate lookalikes remain).

    python3 scripts/claim_drift_sites.py cost-estimation
    python3 scripts/claim_drift_sites.py --all

Adding a class is three lines in `_CLASSES`. This is a *finding aid*, not a
gate: it over-reports by design (a historical release note and a live claim look
identical to a regex), so every hit needs a human read. `KNOWN_OK` records the
lookalikes a previous sweep already judged correct, with the reason, so the next
person does not re-litigate them or "fix" a true statement.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

REPO = Path(__file__).resolve().parent.parent

# Directories that are never a live claim surface: vendored/archived history,
# other agents' worktrees, build output, and the venv.
_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "dist",
    "archive",
    ".claude",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}

_TEXT_SUFFIXES = {".md", ".html", ".py", ".yaml", ".yml", ".txt", ".json", ".cfg", ".toml"}


class ClaimClass:
    def __init__(self, name: str, why: str, patterns: Sequence[str]) -> None:
        self.name = name
        self.why = why
        self.regexes = [re.compile(p, re.IGNORECASE) for p in patterns]

    def matches(self, line: str) -> bool:
        return any(r.search(line) for r in self.regexes)


# Each class is the *denial or understatement* of something that has shipped.
# Patterns intentionally target the claim's phrasing, not the feature's name, so
# an accurate mention of the feature does not hit.
_CLASSES: Dict[str, ClaimClass] = {
    "licensing": ClaimClass(
        "licensing",
        "QueryGate ships proprietary/closed-source on a paid subscription (item 210, "
        "docs/business/GTM_SAAS.md). Every BSL-flip, free-forever, source-available and "
        "no-outbound-calls claim in the repo is now false. This class is the inverse of "
        "the others: it hunts claims about a future that was CANCELLED, not a shipped "
        "feature that is understated.",
        [
            r"\bBSL\b|\bBUSL\b|business source licen[cs]e",
            r"additional use grant|change date|change licen[cs]e",
            r"licen[cs]e flip|the flip\b|pre-bsl|post-bsl",
            r"source[- ]available",
            r"free (forever|internal production)|no (user|database|seat) cap",
            r"no licen[cs]e key|no activation|no trial period",
            r"no outbound calls|no telemetry of any kind|never phones? home",
            r"no kill switch|no time bomb",
            r"apache[- ]2\.0 (after|in|on)|converts? to (apache|open source)",
            r"the (entire )?source is public|readable, buildable, forkable",
        ],
    ),
    "cost-estimation": ClaimClass(
        "cost-estimation",
        "MSSQL cost estimation shipped as item 26 phase 2; docs/code comments still say Postgres-only.",
        [
            r"postgres[- ]only.{0,80}(explain|cost|estimat)",
            r"(cost|estimat|explain).{0,80}postgres[- ]only",
            r"mssql.{0,60}(no equivalent|not (yet )?(built|supported|implemented))",
            r"no effect on an mssql",
            r"item 26 (phase|ph) ?2",
            r"pre-execution cost estimation \(postgres\)",
        ],
    ),
    "worm-resumable": ClaimClass(
        "worm-resumable",
        "Item 184: a day exceeding max_objects_scanned returns a NON-advancing cursor, so "
        "'every bound degrades to a resumable page' is not unconditionally true.",
        [
            r"truncated,? (but )?resumable",
            r"resumable (cursor|page)",
            r"degrades to a truncated",
        ],
    ),
    "four-eyes": ClaimClass(
        "four-eyes",
        "Item 42 (four-eyes config approval) shipped; some surfaces still deny it.",
        [
            r"(no|not|without|lacks?).{0,60}(four[- ]eyes|second[- ]approver|approval workflow)",
            r"(four[- ]eyes|second[- ]approver).{0,60}(not (yet )?(shipped|provided|available|implemented)|roadmap)",
        ],
    ),
    "admin-ui": ClaimClass(
        "admin-ui",
        "Item 31 (admin UI) shipped; some surfaces still deny it.",
        [
            r"(no|not|without).{0,40}admin(istration)? (ui|user interface|console)",
            r"admin(istration)? (ui|user interface).{0,40}not (currently )?(provided|shipped|available)",
            r"rest[- ]only for now",
        ],
    ),
    "disclosure-budget": ClaimClass(
        "disclosure-budget",
        "Item 179 (cumulative disclosure budget) shipped and BOUNDS multi-query differencing; "
        "also item 186 — the per-shape cap is evadable, so any surface calling it "
        "'the targeted probe cap' without that caveat understates the gap.",
        [
            r"multi-query differencing.{0,60}(out of scope|not (closed|bounded|addressed))",
            r"(does not|doesn't|no).{0,40}(privacy budget|sequence-level|query-history)",
            r"closes the evasion at the shape layer",
            r"the targeted probe cap",
        ],
    ),
}

# Hits a previous sweep read and judged CORRECT. Keyed by "path:line-substring"
# so a line moving does not silently drop its exemption.
#
# **Each entry names the ONE claim class it is exempt from.** `_is_known_ok` used
# to key on path and needle alone, so a whole-file exemption granted for the
# licensing sweep silently blinded that file to `cost-estimation`,
# `worm-resumable`, `four-eyes`, `admin-ui` and `disclosure-budget` too — item
# 210 added eight such entries, one of them over `docs/CONTROL_PLANE_PLAN.md`, a
# live design document. A reviewer caught it. Use `"*"` only where the reason
# genuinely holds for every class: this scanner's own pattern table, and the
# worklist files, which describe drift rather than claiming anything.
KNOWN_OK: List[Tuple[str, str, str, str]] = [
    (
        "docs/PRODUCT_GUIDE.md",
        "INTERSECT ALL",
        "A genuine Postgres-only claim about set-operation ALL variants, not cost estimation.",
        "cost-estimation",
    ),
    (
        "src/querygate/core/config.py",
        "request_timeout_seconds",
        "The timeout bound genuinely DOES resume — only the max_objects_scanned bound (item 184) does not.",
        "worm-resumable",
    ),
    (
        "src/querygate/audit/worm_search.py",
        "rather than hanging",
        "The request_timeout bullet: that bound's truncation encodes a forward position.",
        "worm-resumable",
    ),
    (
        "src/querygate/audit/worm_search.py",
        "honestly with a resumable cursor rather than silently",
        "The per-object line cap encodes `last_line`, so this cursor genuinely advances.",
        "worm-resumable",
    ),
    (
        "src/querygate/core/config.py",
        "rather than hanging past this bound",
        "The timeout bound genuinely DOES resume — only max_objects_scanned (item 184) does not.",
        "worm-resumable",
    ),
    (
        "docs/ENGINE_EXPRESSIVENESS_PLAN.md",
        "EXCEPT ALL",
        "A set-operation dialect claim, not cost estimation.",
        "cost-estimation",
    ),
    (
        "src/querygate/policy/models.py",
        "Both dialects are supported since item 26 phase 2",
        "States the CORRECT post-phase-2 position; matches only because it names the item.",
        "cost-estimation",
    ),
    (
        "tests/integration/test_mssql_cost_estimation.py",
        "",
        "The MSSQL estimation test itself — naming item 26 phase 2 is correct here.",
        "cost-estimation",
    ),
    (
        "scripts/claim_drift_sites.py",
        "",
        "This scanner's own pattern table matches itself; not a claim surface.",
        "*",
    ),
    (
        "TODO.md",
        "",
        "The worklist DESCRIBES these drifts; it is the tracker, not a claim surface.",
        "*",
    ),
    (
        "ROADMAP.md",
        "",
        "As TODO.md: describes the drift items rather than making the claim.",
        "*",
    ),
    # --- the cancelled BSL plan, banner-marked as whole documents (item 210) ---
    #
    # These five are PLANS FOR THE FLIP THAT WAS CANCELLED, not live claims about
    # the product. Line-editing them would be wasted work and would destroy the
    # record of why the decision changed, so each carries a whole-document
    # SUPERSEDED/RETIRED banner instead. The exemption is only honest while that
    # banner is present, which is why
    # `test_every_banner_exempt_document_still_carries_its_banner` asserts it.
    (
        "docs/business/GTM_EXECUTION_PLAN.md",
        "",
        "Superseded-by-banner (2026-08-23): the cancelled BSL/Shape A execution plan.",
        "licensing",
    ),
    (
        "docs/business/PRE_BSL_CLEANUP_PLAN.md",
        "",
        "Superseded-by-banner (2026-08-23): cleanup sequenced for a flip that will not happen.",
        "licensing",
    ),
    (
        "docs/business/BSL_EXECUTION_PROMPT.md",
        "",
        "Superseded-by-banner (2026-08-23): an execution prompt for the cancelled flip.",
        "licensing",
    ),
    (
        "docs/business/GTM_EXECUTION_PROMPT.md",
        "",
        "Superseded-by-banner (2026-08-23): an execution prompt for the cancelled flip.",
        "licensing",
    ),
    (
        "docs/LICENSE_NOTES.md",
        "",
        "Superseded-by-banner (2026-08-23): counsel notes for the BSL text that was never adopted.",
        "licensing",
    ),
    (
        ".github/cla/CLA.md",
        "",
        "Retired-by-banner (2026-08-23): a CLA for outside contributors to a public repo, of which there are none.",
        "licensing",
    ),
    (
        ".github/cla/README.md",
        "",
        "Retired-by-banner (2026-08-23): configuration for the CLA bot that will never be enabled.",
        "licensing",
    ),
    (
        "demo/_dsn_guard.py",
        "password-free forever",
        "A false positive: 'password-free forever' is about the demo DSN, not a licence grant.",
        "licensing",
    ),
    (
        "docs/TODO_ARCHIVE.md",
        "",
        "As TODO.md: the archive RECORDS what each item fixed, quoting the retired claims "
        "in order to say they were retired. Not a claim surface.",
        "*",
    ),
    (
        "tests/unit/test_claim_drift_exemptions.py",
        "",
        "This guard's own negative-control fixtures are deliberately-false claim strings; "
        "they exist so a broken detector fails loudly.",
        "licensing",
    ),
    (
        "tests/unit/test_async_execution.py",
        "The flip side",
        "A false positive: 'the flip side' is English, not a licence flip.",
        "licensing",
    ),
    # --- the licensing guards themselves, and the plan that tracks them -------
    #
    # Same reasoning as `scripts/claim_drift_sites.py`, TODO.md and ROADMAP.md
    # above: these DESCRIBE the drift — a gate's docstring saying what it
    # replaced, a design document naming the claims item 210 had to retire — and
    # a guard that cannot explain its own history is a guard the next person
    # deletes. None is a surface a customer reads.
    (
        "docs/CONTROL_PLANE_PLAN.md",
        "",
        "The transition's own design document: it enumerates the false claims to fix, "
        "in the same way TODO.md does. Not a claim surface.",
        "licensing",
    ),
    (
        "tests/unit/test_eula.py",
        "",
        "The EULA gate's tests; its comments explain the BSL Change-Date gate they replaced.",
        "licensing",
    ),
    (
        "scripts/check_eula.py",
        "",
        "The EULA gate itself; same reasoning as its tests.",
        "licensing",
    ),
    (
        "tests/unit/test_release_metadata.py",
        "",
        "Rescued the changelog binding out of the deleted BSL Change-Date test and says so.",
        "licensing",
    ),
    (
        "tests/security/test_no_phone_home.py",
        "",
        "Item 210 narrowed this guard; its docstring quotes the retired absolute in order to "
        "explain what replaced it. Quoting a false claim to retire it is not making it.",
        "licensing",
    ),
    (
        "tests/unit/test_third_party_licenses.py",
        "",
        "Dependency-licence gate; its docstring records that the premise changed from a "
        "source-available flip to closed-source distribution.",
        "licensing",
    ),
    (
        "scripts/check_licenses.py",
        "",
        "As its tests: the rationale names the cancelled plan to explain why the gate outlived it.",
        "licensing",
    ),
    (
        "scripts/check_release_artifacts.py",
        "",
        "Same: the LICENSE-in-every-artifact rationale names the requirement it used to rest on.",
        "licensing",
    ),
]

#: The documents given a WHOLE-FILE exemption above on the strength of a
#: superseded/retired banner, and the banner text each must carry. Kept next to
#: `KNOWN_OK` so the two cannot drift apart.
BANNER_EXEMPT: Dict[str, str] = {
    "docs/business/GTM_EXECUTION_PLAN.md": "SUPERSEDED",
    "docs/business/PRE_BSL_CLEANUP_PLAN.md": "SUPERSEDED",
    "docs/business/BSL_EXECUTION_PROMPT.md": "SUPERSEDED",
    "docs/business/GTM_EXECUTION_PROMPT.md": "SUPERSEDED",
    "docs/LICENSE_NOTES.md": "SUPERSEDED",
    ".github/cla/CLA.md": "RETIRED",
    ".github/cla/README.md": "RETIRED",
}


def _is_known_ok(rel: str, line: str, claim_name: str) -> str | None:
    for path, needle, reason, classes in KNOWN_OK:
        if rel != path or needle not in line:
            continue
        if classes == "*" or claim_name in {c.strip() for c in classes.split(",")}:
            return reason
    return None


def _tracked_files() -> List[Path]:
    """Only git-tracked files: an untracked scratch file is not a claim surface."""
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    files = []
    for rel in out:
        path = REPO / rel
        if any(part in _EXCLUDED_DIRS for part in Path(rel).parts):
            continue
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        if path.is_file():
            files.append(path)
    return files


def scan(claim: ClaimClass) -> Tuple[List[str], List[str]]:
    hits: List[str] = []
    exempt: List[str] = []
    for path in _tracked_files():
        rel = path.relative_to(REPO).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if not claim.matches(line):
                continue
            stripped = line.strip()
            if len(stripped) > 160:
                stripped = stripped[:157] + "..."
            reason = _is_known_ok(rel, line, claim.name)
            if reason:
                exempt.append(f"  {rel}:{number}  [known-ok: {reason}]")
            else:
                hits.append(f"  {rel}:{number}  {stripped}")
    return hits, exempt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "claim_class", nargs="?", choices=sorted(_CLASSES), help="which class to scan"
    )
    parser.add_argument("--all", action="store_true", help="scan every class")
    parser.add_argument("--quiet-exempt", action="store_true", help="hide known-ok lookalikes")
    args = parser.parse_args(argv)

    if not args.all and not args.claim_class:
        parser.error("give a claim class or --all")

    names = sorted(_CLASSES) if args.all else [args.claim_class]
    total = 0
    for name in names:
        claim = _CLASSES[name]
        hits, exempt = scan(claim)
        total += len(hits)
        print(f"\n=== {name} — {len(hits)} site(s) ===")
        print(f"    {claim.why}")
        for hit in hits:
            print(hit)
        if exempt and not args.quiet_exempt:
            print(f"    ({len(exempt)} known-correct lookalike(s), not to be 'fixed':)")
            for line in exempt:
                print(line)
    print(
        f"\n{total} candidate site(s) across {len(names)} class(es). Every hit needs a human read."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
