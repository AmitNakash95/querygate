"""Bounded, tail-first reading of the persisted audit JSONL file (TODO.md item 138).

Every read surface over the audit stream (`admin/anomaly.py`,
`admin/config_trends.py`, `api/admin_ui_routes.py`'s `_audit_page`, and
`/help/my-recent-denials` via its reuse of `admin/anomaly.py`'s reader) cares
about the *newest* events: a recent-vs-baseline window, or a newest-first
page. Before this module, every one of them read the file forward from the
beginning and relied on a bounded in-memory deque to keep only what mattered —
correct, but unbounded in the amount of *work* done: a deployment with a large
audit history pays for parsing and validating every line on every request,
regardless of how much of it is actually needed.

``iter_lines_reverse`` reads the file from the end backward instead. Because
the audit sink only ever appends, this means the data every caller actually
wants — the physically most recent lines — is examined first, and a hard cap
on lines read (rather than lines *retained*) can bound worst-case work without
the correctness trap a forward-and-cap scan would have: capping a forward scan
from the beginning of a large file would silently stop before ever reaching
the recent activity a caller asked about, which is worse than doing no
bounding at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

# Read in fixed-size chunks from the end of the file rather than loading it
# whole, so memory stays bounded regardless of file size.
_CHUNK_SIZE = 65_536

# A single JSONL audit line longer than this is not a legitimate event body —
# the normal sink always writes one bounded, redaction-safe JSON object per
# line. Without a cap here, a file region containing no newline at all (a
# corrupted line, or an adversarial one — see TODO.md item 139) makes the
# undelimited byte run grow by `chunk_size` every iteration and get fully
# re-copied each time (`chunk + carry`), an accidental O(run_length^2) cost
# instead of the intended O(run_length). Capping the run length bounds that
# cost to a small, fixed amount regardless of how much larger the file is.
_DEFAULT_MAX_LINE_BYTES = 1 << 20  # 1 MiB

# Hard ceiling on total bytes read from disk, independent of how many lines
# (blank or otherwise) that spans. `max_line_bytes` alone doesn't bound a file
# padded with an enormous number of *blank* lines — each is cheap individually
# but still costs a byte-range read, and blank lines are skipped before a
# caller's own lines-yielded counter ever sees them.
_DEFAULT_MAX_TOTAL_BYTES = 256 * (1 << 20)  # 256 MiB


class AuditFileReadBounded(Exception):
    """Raised when `iter_lines_reverse` stops before reaching the start of the
    file, because an internal safety bound fired rather than because there
    was nothing left to read. Callers must treat this exactly like their own
    lines-read/retention cap firing — catch it and mark their own result as
    truncated — since silently letting it look like natural exhaustion would
    let a bounded scan claim completeness it doesn't have."""


def iter_lines_reverse(
    path: Path,
    *,
    chunk_size: int = _CHUNK_SIZE,
    max_line_bytes: int = _DEFAULT_MAX_LINE_BYTES,
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
) -> Iterator[str]:
    """Yield each line of a UTF-8 text file from the end backward.

    Splitting is done on the raw newline byte (``b"\\n"``) before any UTF-8
    decoding, which is always safe regardless of where a chunk boundary falls:
    ``0x0A`` never appears inside a multi-byte UTF-8 sequence (continuation
    and lead bytes for multi-byte characters are always ``>= 0x80``), so a
    byte-level split can never cut a character in half — it can only ever cut
    between two complete lines, one of which may still need bytes from an
    earlier (physically preceding) chunk before it's decoded. Each yielded
    line has no trailing newline. A blank line (including the file's own
    trailing newline, if any) is skipped, mirroring `str.strip()` + a
    truthiness check on a forward `for line in handle` scan.

    Raises `AuditFileReadBounded` — never silently stops — if `max_line_bytes`
    or `max_total_bytes` is exceeded, or if the file changes size while being
    read (e.g. in-place truncation by `logrotate copytruncate`, as opposed to
    rename-and-recreate rotation, which this generator's open file handle
    already tolerates): a short read at a given offset means the byte range
    this generator is reconstructing is no longer contiguous, so splicing it
    together would silently fabricate a line that never existed in that form.
    """
    with path.open("rb") as handle:
        handle.seek(0, 2)  # SEEK_END
        position = handle.tell()
        if position == 0:
            return
        carry = b""
        total_read = 0
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            if len(chunk) != read_size:
                raise AuditFileReadBounded("file size changed while reading")
            total_read += len(chunk)
            if total_read > max_total_bytes:
                raise AuditFileReadBounded("max_total_bytes exceeded")
            buffer = chunk + carry
            segments = buffer.split(b"\n")
            # The earliest (first) segment may continue into the still-earlier
            # chunk read next iteration; carry it forward rather than yielding
            # it as if it were already a complete line. It can only still be
            # growing (rather than freshly bounded to this chunk) when no
            # newline was found anywhere in `buffer` — `carry` itself is
            # newline-free by this same invariant from the prior iteration,
            # so any newline in `buffer` must lie within the freshly read
            # `chunk`, which already bounds the new carry to `chunk_size`.
            carry = segments[0]
            if len(segments) == 1 and len(carry) > max_line_bytes:
                raise AuditFileReadBounded("max_line_bytes exceeded")
            for segment in reversed(segments[1:]):
                if segment:
                    yield segment.decode("utf-8", errors="replace")
        if carry:
            yield carry.decode("utf-8", errors="replace")
