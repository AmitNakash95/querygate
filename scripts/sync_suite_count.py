#!/usr/bin/env python3
"""Rewrite the published adversarial-suite count to what `pytest -m security` collects.

`tests/unit/test_security_suite_count_claims.py` is the *gate* — it fails when a
published figure disagrees with the real collection, and it names the new number
and the two generated artifacts to regenerate. This is the corresponding
**sync**, the same relationship `make worklist-sync` has to `make worklist-check`.

It exists because the gate fires on every change that adds or removes a
security-marked test, and hand-editing nine files each time is how one of them
gets missed — which is a claim defect, not a typo. The gate stays authoritative;
this only saves the typing.

Derived, never invented: the count comes from a real `--collect-only` run, and
the file list comes from the gate's own `CLAIM_FILES`, so the two cannot drift.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess  # nosec B404 - runs pytest from this repo, no external input
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.unit.test_security_suite_count_claims import (  # noqa: E402
    CLAIM_FILES,
    collected_security_tests,
)

#: The two files that are generated, not edited. Rewriting them directly would be
#: overwritten by the next `make` run, so they are regenerated instead.
_GENERATED = {
    "docs/TRUST_EVIDENCE.md": "trust-page",
    "docs/product-guide.html": "product-guide-html",
}


def _current_counts(path: Path) -> set[int]:
    """Every suite-size figure the gate would read out of this file."""
    from tests.unit.test_security_suite_count_claims import _quoted_counts

    return _quoted_counts(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="report drift without rewriting anything"
    )
    args = parser.parse_args(argv)

    actual = collected_security_tests()
    print(f"`pytest -m security` collects {actual} tests")

    changed: list[str] = []
    for name in CLAIM_FILES:
        path = ROOT / name
        stale = {count for count in _current_counts(path) if count != actual}
        if not stale:
            continue
        if name in _GENERATED:
            changed.append(f"{name} (generated — run `make {_GENERATED[name]}`)")
            continue
        if args.check:
            changed.append(f"{name} quotes {sorted(stale)}")
            continue
        text = path.read_text(encoding="utf-8")
        for old in stale:
            # Word-bounded so a port, a year or a byte size is never rewritten.
            text = re.sub(rf"\b{old}\b", str(actual), text)
        path.write_text(text, encoding="utf-8")
        changed.append(f"{name}: {sorted(stale)} -> {actual}")

    if not changed:
        print("suite-count claims: OK — every surface already quotes the real figure")
        return 0
    for line in changed:
        print(("  drift: " if args.check else "  updated: ") + line)
    if args.check:
        return 1
    generated = [name for name in CLAIM_FILES if name in _GENERATED]
    if generated:
        print("\nNow regenerate the derived artifacts:")
        for name in generated:
            print(f"  make {_GENERATED[name]}")
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    raise SystemExit(main())
