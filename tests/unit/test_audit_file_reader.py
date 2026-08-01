"""Unit tests for the tail-first audit file reader (TODO.md item 138)."""

from __future__ import annotations

import pytest

from querygate.audit.file_reader import AuditFileReadBounded, iter_lines_reverse

pytestmark = pytest.mark.unit


def _write(tmp_path, content: str, *, name: str = "audit.jsonl"):
    path = tmp_path / name
    path.write_bytes(content.encode("utf-8"))
    return path


def test_empty_file_yields_nothing(tmp_path):
    path = _write(tmp_path, "")
    assert list(iter_lines_reverse(path)) == []


def test_single_line_no_trailing_newline(tmp_path):
    path = _write(tmp_path, "only line")
    assert list(iter_lines_reverse(path)) == ["only line"]


def test_single_line_with_trailing_newline(tmp_path):
    path = _write(tmp_path, "only line\n")
    assert list(iter_lines_reverse(path)) == ["only line"]


def test_multiple_lines_yielded_newest_first(tmp_path):
    path = _write(tmp_path, "line-1\nline-2\nline-3\n")
    assert list(iter_lines_reverse(path)) == ["line-3", "line-2", "line-1"]


def test_multiple_lines_no_trailing_newline(tmp_path):
    path = _write(tmp_path, "line-1\nline-2\nline-3")
    assert list(iter_lines_reverse(path)) == ["line-3", "line-2", "line-1"]


def test_blank_lines_are_skipped(tmp_path):
    path = _write(tmp_path, "line-1\n\nline-2\n\n\nline-3\n")
    assert list(iter_lines_reverse(path)) == ["line-3", "line-2", "line-1"]


def test_file_of_only_blank_lines_yields_nothing(tmp_path):
    path = _write(tmp_path, "\n\n\n")
    assert list(iter_lines_reverse(path)) == []


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 4, 7, 65_536])
def test_stable_across_chunk_sizes_including_mid_line_boundaries(tmp_path, chunk_size):
    lines = [f"line-{i}" for i in range(20)]
    path = _write(tmp_path, "\n".join(lines) + "\n")
    assert list(iter_lines_reverse(path, chunk_size=chunk_size)) == list(reversed(lines))


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 4, 5, 65_536])
def test_multibyte_utf8_characters_survive_arbitrary_chunk_boundaries(tmp_path, chunk_size):
    # "日本語" and an emoji are each multi-byte UTF-8 sequences; a byte-level
    # split at an arbitrary chunk boundary must never corrupt one, since the
    # algorithm only ever splits on the raw newline byte (0x0A), which cannot
    # appear inside a multi-byte sequence.
    lines = ["café", "日本語のログ", "emoji: 🎉🔥", "plain-ascii"]
    path = _write(tmp_path, "\n".join(lines) + "\n")
    assert list(iter_lines_reverse(path, chunk_size=chunk_size)) == list(reversed(lines))


def test_large_file_spanning_many_chunks_round_trips(tmp_path):
    lines = [f"event-{i:06d}" for i in range(5000)]
    path = _write(tmp_path, "\n".join(lines) + "\n")
    assert list(iter_lines_reverse(path, chunk_size=512)) == list(reversed(lines))


def test_can_be_stopped_early_without_reading_the_whole_file(tmp_path):
    # The whole point of reading in reverse: a caller that only needs the
    # newest few lines can stop consuming the generator immediately, without
    # ever touching the (potentially huge) rest of the file.
    lines = [f"line-{i}" for i in range(100_000)]
    path = _write(tmp_path, "\n".join(lines) + "\n")
    gen = iter_lines_reverse(path, chunk_size=64)
    first_three = [next(gen) for _ in range(3)]
    assert first_three == ["line-99999", "line-99998", "line-99997"]


# --- safety bounds (TODO.md item 138, security-review follow-up) -----------


def test_oversized_undelimited_run_raises_instead_of_growing_without_bound(tmp_path):
    # A run with no newline at all longer than max_line_bytes is not a
    # legitimate audit line (the sink always writes one bounded JSON object
    # per line) — it must raise, not silently keep accumulating it forever.
    path = _write(tmp_path, "x" * 10_000)
    with pytest.raises(AuditFileReadBounded):
        list(iter_lines_reverse(path, chunk_size=64, max_line_bytes=1_000))


def test_oversized_line_does_not_prevent_finding_shorter_lines_first(tmp_path):
    # Real, short lines physically after (newer than) an oversized one are
    # still found before the bound fires — the cap only stops the scan once
    # it actually reaches the oversized run, exactly like the line/event caps.
    path = _write(tmp_path, "x" * 10_000 + "\n" + "\n".join(["recent-1", "recent-2"]))
    gen = iter_lines_reverse(path, chunk_size=64, max_line_bytes=1_000)
    assert next(gen) == "recent-2"
    assert next(gen) == "recent-1"
    with pytest.raises(AuditFileReadBounded):
        next(gen)


def test_max_total_bytes_bounds_a_file_padded_with_many_blank_lines(tmp_path):
    # Blank lines are skipped before ever reaching a caller's own
    # lines-yielded counter, so max_line_bytes alone can't bound a file
    # padded with an enormous number of them — max_total_bytes closes that
    # gap by bounding total bytes read regardless of how they're split.
    path = _write(tmp_path, "\n" * 1_000_000 + "real-line\n")
    with pytest.raises(AuditFileReadBounded):
        list(iter_lines_reverse(path, chunk_size=256, max_total_bytes=10_000))


def test_max_total_bytes_does_not_fire_under_the_bound(tmp_path):
    path = _write(tmp_path, "line-1\nline-2\nline-3\n")
    assert list(iter_lines_reverse(path, max_total_bytes=1_000)) == [
        "line-3",
        "line-2",
        "line-1",
    ]


def test_file_changing_size_mid_scan_raises_instead_of_splicing(tmp_path):
    lines = [f"line-{i}" for i in range(200)]
    path = _write(tmp_path, "\n".join(lines) + "\n")
    gen = iter_lines_reverse(path, chunk_size=64)
    seen = [next(gen), next(gen)]
    path.write_bytes(b"short\n")  # simulate in-place truncation (not rotation)
    with pytest.raises(AuditFileReadBounded):
        list(gen)
    # What was already yielded before the truncation must still be genuine,
    # unmodified lines from the original file — never a spliced fabrication.
    assert seen == ["line-199", "line-198"]
