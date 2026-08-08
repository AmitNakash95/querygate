#!/usr/bin/env python3
"""Compose a single, always-current procurement evidence page (TODO.md item
147) from artifacts that already exist: `docs/SECURITY_POSTURE.md`,
`docs/COMPLIANCE_MAPPING.md`, `docs/business/SECURITY_BENCHMARK.md`,
`SECURITY.md`'s disclosure program, and the current dependency-audit
allowlist status (`security/dependency-audit-allowlist.json`).

No new evidence is generated here — this only turns docs a prospect's
security reviewer would otherwise be handed one at a time (or as a
hand-rebuilt, easily-stale packet, the `trust-evidence` skill's ad hoc
process) into one generated, git-committed artifact. Regenerate after editing
any source doc or the allowlist:

    poetry run python scripts/generate_trust_page.py   # or: make trust-page
"""

from __future__ import annotations

import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "TRUST_EVIDENCE.md"
ALLOWLIST_FILE = ROOT / "security" / "dependency-audit-allowlist.json"

# (section title, source file) — each read in full and embedded verbatim, in
# this order. Composing the real docs rather than summarizing them avoids a
# lossy, opinion-laden rewrite of already-reviewed evidence.
_SOURCES: list[tuple[str, Path]] = [
    ("Security & reliability posture", ROOT / "docs" / "SECURITY_POSTURE.md"),
    ("Compliance control mapping", ROOT / "docs" / "COMPLIANCE_MAPPING.md"),
    (
        "Adversarial benchmark report",
        ROOT / "docs" / "business" / "SECURITY_BENCHMARK.md",
    ),
    ("Responsible disclosure program", ROOT / "SECURITY.md"),
]


def _load_allowlist() -> list[dict]:
    """The reviewed vulnerability allowlist (see `scripts/generate_sbom.py`'s
    own `load_allowlist` — duplicated rather than imported: scripts in this
    directory are standalone by convention, and the shape needed here is
    just "read the JSON list", not the id-keyed dict with duplicate/missing-
    field validation the SBOM generator's own gate needs)."""
    if not ALLOWLIST_FILE.is_file():
        return []
    return json.loads(ALLOWLIST_FILE.read_text(encoding="utf-8"))


def _slug(title: str) -> str:
    """GitHub-flavored-markdown heading anchor: lowercase, drop `&` (leaving
    the double space it sat in, which becomes a double hyphen below —
    matching GitHub's own algorithm), then replace every space with a
    hyphen."""
    return title.lower().replace("&", "").replace(" ", "-")


def _dependency_audit_summary() -> str:
    entries = _load_allowlist()
    if not entries:
        return (
            "**0 allowlisted known vulnerabilities.** Every disclosed CVE against "
            "the exact shipped dependency set (`poetry.lock`'s `main` group) is "
            "fixed by version, not waived. See `make sbom` in "
            f"[{_SOURCES[0][0]}](#{_slug(_SOURCES[0][0])})."
        )
    lines = [
        f"**{len(entries)} reviewed allowlist entries** (deny-by-default — every other known finding fails the release gate):",
        "",
    ]
    for entry in entries:
        lines.append(
            f"- `{entry['id']}` ({entry['package']}) — {entry['reason']} "
            f"(tracking: {entry['tracking']})"
        )
    return "\n".join(lines)


def build_document(*, version: str, generated_at: str) -> str:
    parts = [
        "<!-- GENERATED FILE — do not hand-edit. Regenerate with `make trust-page` "
        "(scripts/generate_trust_page.py). -->",
        "# QueryGate — Trust & Evidence Packet",
        "",
        f"*Generated {generated_at} for QueryGate {version}. Composed, read-only, "
        "from the checked-in docs and dependency-audit results below (TODO.md "
        "item 147) — this file asserts no claim of its own; every statement here "
        "is backed by the cited source doc and, where named, a reproducible "
        "`make` command. It does not imply any control, certification, or "
        "third-party attestation that isn't explicitly stated in a source doc.*",
        "",
        "## Current dependency audit status",
        "",
        _dependency_audit_summary(),
        "",
    ]
    for title, path in _SOURCES:
        if not path.is_file():
            raise SystemExit(f"trust page source is missing: {path}")
        parts.append(f"## {title}")
        parts.append("")
        parts.append(f"*Source: [`{path.relative_to(ROOT)}`]({path.relative_to(ROOT)}).*")
        parts.append("")
        parts.append(path.read_text(encoding="utf-8").strip())
        parts.append("")
    return "\n".join(parts) + "\n"


def main() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    version = metadata["project"]["version"]
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    OUTPUT.write_text(build_document(version=version, generated_at=generated_at), encoding="utf-8")
    print(f"Trust evidence page -> {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
