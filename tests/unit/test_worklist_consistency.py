"""Worklist consistency guard — TODO.md / ROADMAP.md / docs/TODO_ARCHIVE.md.

CLAUDE.md's worklist rules (archive a fully-shipped item and leave a stub, keep
the Quick-scan ✅ column and ROADMAP checkboxes reconciled, keep item numbers
unique and the archive in order) used to live only as prose and drifted
silently. This test makes them enforceable in the default unit suite — the same
move `test_scope_catalog.py` makes for the generated scope doc — so the drift
class this test was written after cannot silently reopen.

Two halves:
  * The *live* guard: `scripts/check_worklist.py` finds zero violations against
    the real repo docs. This is what fails in CI / the pre-commit gate when an
    item is marked done but not archived, or a mirror falls out of sync.
  * *Detector* tests: feeding planted drift makes each rule fire (and, for the
    prose-cross-reference case, makes sure it does NOT fire) — so a silently
    broken checker, which would be worse than none, is itself caught.
"""

from __future__ import annotations

import pytest

from scripts import check_worklist

pytestmark = pytest.mark.unit


def test_live_worklist_is_consistent():
    violations = check_worklist.find_violations()
    assert (
        violations == []
    ), "worklist drift — run `make worklist-sync`, then archive/stub as needed:\n" + "\n".join(
        f"  - {m}" for m in violations
    )


# --- detector tests: planted drift must be caught -----------------------------

_GOOD_TODO = (
    "| 1 | ✅ Alpha | S | — |\n"
    "| 2 | Beta | S | — |\n"
    "\n"
    "### 1. Alpha ✅ DONE\n"
    "\nStub. **Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 1).\n"
    "\n### 2. Beta\n\nOpen work.\n"
)
_GOOD_ROAD = "- [x] **1** — Alpha.\n- [ ] **2** — Beta.\n"
_GOOD_ARCH = "# archive\n\n### 1. Alpha ✅ DONE\n\nFull write-up.\n"


def test_clean_synthetic_docs_pass():
    assert check_worklist.find_violations(_GOOD_TODO, _GOOD_ROAD, _GOOD_ARCH) == []


def test_detects_done_item_not_archived():
    todo = _GOOD_TODO.replace("| 2 | Beta", "| 2 | ✅ Beta").replace(
        "### 2. Beta\n", "### 2. Beta ✅ DONE\n"
    )
    v = check_worklist.find_violations(todo, "- [x] **1**\n- [x] **2**\n", _GOOD_ARCH)
    assert any(m.startswith("C:") and "item 2" in m for m in v), v


def test_detects_unstubbed_body_over_line_cap():
    big = "### 1. Alpha ✅ DONE\n\n" + "\n".join(f"line {i}" for i in range(60)) + "\n(item 1)\n"
    todo = "| 1 | ✅ Alpha | S | — |\n\n" + big
    v = check_worklist.find_violations(todo, "- [x] **1**\n", _GOOD_ARCH)
    assert any(m.startswith("C:") and "lines" in m for m in v), v


def test_detects_table_mirror_drift():
    todo = _GOOD_TODO.replace("| 1 | ✅ Alpha", "| 1 | Alpha")  # heading done, table lacks ✅
    v = check_worklist.find_violations(todo, _GOOD_ROAD, _GOOD_ARCH)
    assert any(m.startswith("E:") and "item 1" in m for m in v), v


def test_detects_roadmap_checkbox_drift():
    road = "- [ ] **1** — Alpha.\n- [ ] **2** — Beta.\n"  # item 1 is done but unchecked
    v = check_worklist.find_violations(_GOOD_TODO, road, _GOOD_ARCH)
    assert any(m.startswith("F:") and "item 1" in m for m in v), v


def test_detects_archive_out_of_order():
    arch = "# archive\n\n### 2. Beta ✅ DONE\n\nx\n\n### 1. Alpha ✅ DONE\n\ny\n"
    todo = (
        _GOOD_TODO.replace("### 2. Beta\n", "### 2. Beta ✅ DONE\n").replace(
            "| 2 | Beta", "| 2 | ✅ Beta"
        )
        + "**Full write-up:** (item 2).\n"
    )
    v = check_worklist.find_violations(todo, "- [x] **1**\n- [x] **2**\n", arch)
    assert any(m.startswith("B:") and "ascending" in m for m in v), v


def test_prose_cross_reference_is_not_a_dangling_pointer():
    """A bare `(item 19)` in prose is a cross-reference, not a write-up pointer.

    Regression lock for the false positive that flagged item 57's
    'Adding a dialect (item 19) = …' as a dangling archive pointer.
    """
    todo = (
        "| 1 | ✅ Alpha | S | — |\n\n"
        "### 1. Alpha ✅ DONE\n\n"
        "See the sibling work (item 19) for context. "
        "**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 1).\n"
    )
    v = check_worklist.find_violations(todo, "- [x] **1**\n", _GOOD_ARCH)
    assert not any(m.startswith("G:") for m in v), v


def test_detects_genuine_dangling_pointer():
    todo = (
        "| 1 | ✅ Alpha | S | — |\n\n"
        "### 1. Alpha ✅ DONE\n\n"
        "**Full write-up:** [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 1). "
        "Also [docs/TODO_ARCHIVE.md](docs/TODO_ARCHIVE.md) (item 404).\n"
    )
    v = check_worklist.find_violations(todo, "- [x] **1**\n", _GOOD_ARCH)
    assert any(m.startswith("G:") and "404" in m for m in v), v
