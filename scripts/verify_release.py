"""Verify a built release's artifact integrity against its `SHA256SUMS` manifest.

TODO.md item 30 / 89 phase 2. This is the *verification* counterpart to
`scripts/generate_sbom.py`'s checksum generation: given a `dist/` directory that
contains a `SHA256SUMS` manifest (wheel, sdist, SBOM) plus the artifacts it
names, recompute each file's SHA-256 and confirm it matches — the exact
tamper/corruption check a customer runs after downloading a release bundle,
made a first-class, gating, cross-platform step instead of a doc snippet
(`shasum -a 256 -c` differs from `sha256sum -c` across platforms).

This checks *integrity* (the bytes are the ones this repo produced), not
*authenticity* — authenticity of the published container image comes from the
cosign keyless signature and the SLSA build-provenance attestation the release
workflow (`.github/workflows/release.yml`) attaches to the pushed image; a
consumer verifies those with `cosign verify` / `gh attestation verify` (see
`docs/RELEASING.md`).

Exit code is 0 iff every manifest entry is present and matches, so it can gate a
release or a periodic integrity job. It fails closed on a missing manifest, a
missing artifact, a digest mismatch, or a malformed manifest line.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIST = ROOT / "dist"
MANIFEST_NAME = "SHA256SUMS"


class VerificationError(Exception):
    """A release-integrity check failed (fail-closed)."""


def _parse_manifest(manifest: Path) -> List[Tuple[str, str]]:
    """Parse `<hexdigest>  <filename>` lines into (digest, filename) pairs."""
    entries: List[Tuple[str, str]] = []
    for lineno, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        # Standard coreutils format: digest, two spaces, filename. Split on the
        # first run of whitespace so filenames with single spaces still parse.
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise VerificationError(f"{manifest.name}:{lineno}: malformed line: {raw!r}")
        digest, name = parts[0].lower(), parts[1].strip()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise VerificationError(
                f"{manifest.name}:{lineno}: not a SHA-256 hex digest: {parts[0]!r}"
            )
        entries.append((digest, name))
    if not entries:
        raise VerificationError(f"{manifest.name} is empty — nothing to verify")
    return entries


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_release(dist_dir: Path) -> List[str]:
    """Verify every artifact named in `dist_dir/SHA256SUMS`.

    Returns the list of verified filenames. Raises `VerificationError` on the
    first integrity problem (missing manifest/artifact, digest mismatch, or a
    malformed manifest).
    """
    manifest = dist_dir / MANIFEST_NAME
    if not manifest.is_file():
        raise VerificationError(
            f"no {MANIFEST_NAME} in {dist_dir} — run `make sbom` (after `poetry build`) first"
        )

    verified: List[str] = []
    for expected_digest, name in _parse_manifest(manifest):
        artifact = dist_dir / name
        if not artifact.is_file():
            raise VerificationError(f"missing artifact named in manifest: {name}")
        actual_digest = _sha256(artifact)
        if actual_digest != expected_digest:
            raise VerificationError(
                f"digest mismatch for {name}:\n  expected {expected_digest}\n  actual   {actual_digest}"
            )
        verified.append(name)
    return verified


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_release",
        description="Verify release artifact integrity against dist/SHA256SUMS.",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=DEFAULT_DIST,
        help=f"Directory holding {MANIFEST_NAME} and the artifacts (default: {DEFAULT_DIST}).",
    )
    args = parser.parse_args(argv)

    try:
        verified = verify_release(args.dist_dir)
    except VerificationError as exc:
        print(f"Release integrity check FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"Release integrity OK: {len(verified)} artifact(s) verified against {MANIFEST_NAME}")
    for name in verified:
        print(f"  ✓ {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
