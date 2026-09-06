"""Locate the line numbers of a named rule inside policy.demo.yaml, live.

demo/SPEC.md's RunResult carries `policy_lines` so the config panel can
highlight the exact rule that governed a scenario. Per the task's rule 6,
these must be *found by searching the file for the key*, not hardcoded line
numbers that rot the moment someone edits a comment above the rule — so this
module re-reads and re-parses the real file on every call.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def find_key_block_lines(text: str, key: str) -> list[int]:
    """Return the 1-indexed line numbers of every occurrence of `key:` in
    the file, plus, for each occurrence, every contiguous line indented
    further than it (its YAML block) — stopping at the first line back at or
    above its own indentation. Works for both a single-line scalar/flow
    value (`allowed_tables: [...]`) and a nested block (`column_masks:`
    followed by indented sub-mapping/list content).

    F10: this used to `break` at the first match and highlight only that
    one block. policy.demo.yaml has a single top-level `default:` policy
    today so that was harmless, but a future per-connection override would
    repeat a rule key once per connection — breaking at the first match
    would then silently highlight the wrong connection's block with no
    error. Every match is now collected and unioned, so this stays correct
    the day a second block exists, not just today.

    Returns [] if the key is not found at all — callers should treat that as
    "nothing to highlight", not raise, since a scenario whose citation key
    briefly stops matching the file (e.g. mid-edit) shouldn't crash a demo.
    """
    lines = text.split("\n")
    key_prefix = f"{key}:"
    result: set[int] = set()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not (
            stripped == key_prefix
            or stripped.startswith(key_prefix + " ")
            or stripped.startswith(key_prefix + "[")
        ):
            i += 1
            continue

        base_indent = _indent(line)
        result.add(i + 1)  # 1-indexed
        j = i + 1
        while j < len(lines):
            block_line = lines[j]
            if block_line.strip() == "":
                j += 1
                continue
            if _indent(block_line) > base_indent:
                result.add(j + 1)
                j += 1
            else:
                break
        i = j
    return sorted(result)


def load_policy_text(path: Path) -> str:
    return path.read_text()


def lines_for(path: Path, *keys: str) -> list[int]:
    """Union of find_key_block_lines() for each key, sorted and deduped.
    Returns [] (nothing to highlight) rather than raising if the file can't
    be read right now — same reasoning as masked_columns_for below: an
    operator may be mid-save on policy.demo.yaml between scenes."""
    try:
        text = load_policy_text(path)
    except (OSError, UnicodeDecodeError):
        return []
    out: set[int] = set()
    for key in keys:
        out.update(find_key_block_lines(text, key))
    return sorted(out)


def masked_columns_for(path: Path, table: str) -> list[str]:
    """Column names `default.column_masks.<table>` masks, read live from the
    real policy.demo.yaml (never hardcoded) — SPEC.md F7. The frontend keys
    off `RunResult.masked_columns` to recognize a real masked value (a live
    hash, or "last 4 digits") instead of only the mock fixtures' literal
    "••••" placeholder.

    Returns [] if the file can't be parsed, can't even be read, or has any
    unexpected shape along the way (e.g. `default:` present but null, or a
    list instead of a mapping) — every step below checks `isinstance`
    before calling `.get` on it, the same "present key, wrong/null value"
    trap `scenarios.py`'s `_row_count_or_len` docstring already documents
    having been bitten by once. Callers should treat any of this as
    "nothing masked", not raise — this module exists to support a live demo
    where an operator may be hand-editing policy.demo.yaml between scenes.
    """
    try:
        doc = yaml.safe_load(load_policy_text(path))
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return []
    if not isinstance(doc, dict):
        return []
    default = doc.get("default")
    if not isinstance(default, dict):
        return []
    masks = default.get("column_masks")
    if not isinstance(masks, dict):
        return []
    table_masks = masks.get(table)
    if not isinstance(table_masks, list):
        return []
    return [m["column"] for m in table_masks if isinstance(m, dict) and "column" in m]
