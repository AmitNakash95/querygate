"""Tests for the release-integrity verifier (TODO.md item 30/89 phase 2).

The verifier is a fail-closed supply-chain control, so these lock in that it
accepts a good bundle and rejects every way a bundle can be wrong: a missing
manifest, a missing artifact, a tampered byte, and a malformed/empty manifest.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.verify_release import VerificationError, main, verify_release

pytestmark = pytest.mark.unit


def _write_artifact(dist: Path, name: str, content: bytes) -> str:
    (dist / name).write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def _write_manifest(dist: Path, lines: list[str]) -> None:
    (dist / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_valid_bundle_verifies(tmp_path: Path):
    d1 = _write_artifact(tmp_path, "querygate-0.1.0-py3-none-any.whl", b"wheel-bytes")
    d2 = _write_artifact(tmp_path, "querygate-0.1.0.tar.gz", b"sdist-bytes")
    _write_manifest(
        tmp_path, [f"{d1}  querygate-0.1.0-py3-none-any.whl", f"{d2}  querygate-0.1.0.tar.gz"]
    )

    verified = verify_release(tmp_path)
    assert set(verified) == {"querygate-0.1.0-py3-none-any.whl", "querygate-0.1.0.tar.gz"}


def test_missing_manifest_fails_closed(tmp_path: Path):
    with pytest.raises(VerificationError, match="no SHA256SUMS"):
        verify_release(tmp_path)


def test_tampered_artifact_is_detected(tmp_path: Path):
    digest = _write_artifact(tmp_path, "querygate-0.1.0.tar.gz", b"original")
    _write_manifest(tmp_path, [f"{digest}  querygate-0.1.0.tar.gz"])
    # Tamper with the bytes after the manifest was written.
    (tmp_path / "querygate-0.1.0.tar.gz").write_bytes(b"tampered")

    with pytest.raises(VerificationError, match="digest mismatch"):
        verify_release(tmp_path)


def test_missing_artifact_is_detected(tmp_path: Path):
    _write_manifest(tmp_path, [f"{'0' * 64}  ghost.tar.gz"])
    with pytest.raises(VerificationError, match="missing artifact"):
        verify_release(tmp_path)


def test_malformed_line_is_rejected(tmp_path: Path):
    _write_manifest(tmp_path, ["not-a-valid-manifest-line"])
    with pytest.raises(VerificationError, match="malformed line"):
        verify_release(tmp_path)


def test_non_hex_digest_is_rejected(tmp_path: Path):
    _write_manifest(tmp_path, ["zz" + "0" * 62 + "  querygate-0.1.0.tar.gz"])
    with pytest.raises(VerificationError, match="not a SHA-256 hex digest"):
        verify_release(tmp_path)


def test_empty_manifest_is_rejected(tmp_path: Path):
    (tmp_path / "SHA256SUMS").write_text("\n\n", encoding="utf-8")
    with pytest.raises(VerificationError, match="empty"):
        verify_release(tmp_path)


def test_filename_with_space_parses(tmp_path: Path):
    digest = _write_artifact(tmp_path, "my artifact.tar.gz", b"x")
    _write_manifest(tmp_path, [f"{digest}  my artifact.tar.gz"])
    assert verify_release(tmp_path) == ["my artifact.tar.gz"]


def test_main_returns_zero_on_success(tmp_path: Path, capsys):
    digest = _write_artifact(tmp_path, "querygate-0.1.0.tar.gz", b"bytes")
    _write_manifest(tmp_path, [f"{digest}  querygate-0.1.0.tar.gz"])
    rc = main(["--dist-dir", str(tmp_path)])
    assert rc == 0
    assert "Release integrity OK" in capsys.readouterr().out


def test_main_returns_one_on_failure(tmp_path: Path, capsys):
    rc = main(["--dist-dir", str(tmp_path)])
    assert rc == 1
    assert "FAILED" in capsys.readouterr().err
