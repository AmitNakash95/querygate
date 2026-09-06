"""Pure-logic regression tests for the demo control backend's honesty-guard
helpers (demo/SPEC.md F1-F12 fix session).

Not wired into the QueryGate product test suite (pyproject.toml's
`testpaths = ["tests"]` never discovers this file) — demo/ is excluded from
every release artifact and this file only needs the interpreter, never a
live Postgres/MCP server. Run it explicitly:

    poetry run pytest demo/control/test_scenarios_logic.py -v

These functions were, prior to this session, the site of five independently
confirmed "fabricated success" bugs (this repo's working agreement requires
mutation-verifying every new enforcement point). None of them need a live
server or database — they operate on plain dicts/strings — so there is no
excuse for leaving them with zero regression coverage. This file exists
specifically so a future one-line "simplification" of one of these branches
(e.g. swapping `and`/`or`, or moving a check outside its guard) fails loudly
instead of silently reintroducing exactly the bug this session fixed.
"""

from __future__ import annotations

import pytest

from demo.control.mcp_client import MCPCallResult, MCPTransportError
from demo.control.policy_lines import find_key_block_lines, masked_columns_for
from demo.control.scenarios import (
    _all_calls_failed,
    _blocked_by_from_error,
    _reject_if_call_failed,
)


def _call(*, is_protocol_error: bool = False, is_tool_error: bool = False) -> MCPCallResult:
    return MCPCallResult(
        request_body={},
        rpc_response={},
        elapsed_ms=1.0,
        is_protocol_error=is_protocol_error,
        is_tool_error=is_tool_error,
        payload={"ok": "whatever"},
    )


# ---------------------------------------------------------------------------
# _reject_if_call_failed
# ---------------------------------------------------------------------------


class TestRejectIfCallFailed:
    def test_passes_through_a_normal_call(self):
        call = _call()
        assert _reject_if_call_failed(call, "server", "tool") is call

    def test_raises_on_protocol_error(self):
        with pytest.raises(MCPTransportError):
            _reject_if_call_failed(_call(is_protocol_error=True), "server", "tool")

    def test_raises_on_tool_error(self):
        with pytest.raises(MCPTransportError):
            _reject_if_call_failed(_call(is_tool_error=True), "server", "tool")

    def test_a_normal_policy_rejection_is_not_a_call_failure(self):
        # QueryGate reports a policy rejection ('table not accessible', a
        # cross-join refusal, etc.) as a real `results[0].error` string
        # inside an otherwise-successful (isError=false) tool response —
        # NOT as is_tool_error/is_protocol_error. If this guard ever
        # started treating that shape as a call failure, every rejected
        # scenario (pii_table/on, pii_column/on, overload/on, ...) would
        # start 502ing instead of reporting a real 'blocked' RunResult.
        call = MCPCallResult(
            request_body={},
            rpc_response={},
            elapsed_ms=1.0,
            is_protocol_error=False,
            is_tool_error=False,
            payload={"results": [{"error": "Table 'employees' is not accessible"}]},
        )
        assert _reject_if_call_failed(call, "server", "tool") is call


# ---------------------------------------------------------------------------
# _all_calls_failed (the overload scenario's F4 guard)
# ---------------------------------------------------------------------------


class TestAllCallsFailed:
    def test_empty_is_not_all_failed(self):
        # Every one of the 20 concurrent calls is still pending at the 15s
        # wall-clock bound (real load, not failure) — `completed_results`
        # is [] in that case. A guard that reads `all([]) == True` (Python's
        # actual behavior for an empty iterable) without the `bool(...)`
        # short-circuit would wrongly treat "nothing has finished yet" the
        # same as "everything failed".
        assert _all_calls_failed([]) is False

    def test_all_failed(self):
        assert _all_calls_failed([{"ok": False}, {"ok": False}, {"ok": False}]) is True

    def test_one_success_is_not_all_failed(self):
        assert _all_calls_failed([{"ok": False}, {"ok": True}, {"ok": False}]) is False

    def test_all_succeeded(self):
        assert _all_calls_failed([{"ok": True}, {"ok": True}]) is False

    def test_missing_ok_key_treated_as_failed(self):
        # one_call()'s dicts always carry "ok", but the guard should not
        # crash or silently pass on a malformed entry.
        assert _all_calls_failed([{}]) is True


# ---------------------------------------------------------------------------
# _blocked_by_from_error (F11)
# ---------------------------------------------------------------------------


class TestBlockedByFromError:
    @pytest.mark.parametrize(
        "error,expected",
        [
            (
                "Table 'employees' is not accessible under the active policy",
                "policy.demo.yaml: allowed_tables",
            ),
            (
                "Column 'customers.email' is not accessible under the active policy",
                "policy.demo.yaml: denied_columns",
            ),
            (
                "cross join to 'orders' is not allowed under the active policy",
                "policy.demo.yaml: allow_cross_join",
            ),
        ],
    )
    def test_known_patterns(self, error, expected):
        assert _blocked_by_from_error(error) == expected

    def test_none_error_is_none(self):
        assert _blocked_by_from_error(None) is None

    def test_empty_string_is_none(self):
        assert _blocked_by_from_error("") is None

    def test_unrecognized_error_leaves_it_null_rather_than_guessing(self):
        # F11's whole point: an unmatched error must not be mislabeled as
        # some other rule (the config panel would highlight the wrong
        # lines). max_limit itself never appears here because it clamps
        # silently and can't raise this way in the normal path.
        assert _blocked_by_from_error("a completely unrelated database error") is None


# ---------------------------------------------------------------------------
# find_key_block_lines (F10 — union every match, not just the first)
# ---------------------------------------------------------------------------


class TestFindKeyBlockLines:
    def test_single_line_scalar(self):
        text = "default:\n  allowed_tables: [a, b]\n  max_limit: 100\n"
        assert find_key_block_lines(text, "allowed_tables") == [2]

    def test_nested_block(self):
        text = (
            "default:\n  column_masks:\n    customers:\n      - column: phone\n  max_limit: 100\n"
        )
        assert find_key_block_lines(text, "column_masks") == [2, 3, 4]

    def test_key_not_found(self):
        assert find_key_block_lines("default:\n  max_limit: 100\n", "nonexistent_key") == []

    def test_unions_every_occurrence_not_just_the_first(self):
        # F10's regression case: a repeated key (e.g. a future
        # per-connection override section) must highlight every block, not
        # silently stop at the first match.
        text = (
            "default:\n"
            "  allowed_tables: [a, b]\n"
            "per_connection:\n"
            "  conn2:\n"
            "    allowed_tables: [c, d]\n"
        )
        assert find_key_block_lines(text, "allowed_tables") == [2, 5]

    def test_adjacent_matches_are_not_skipped(self):
        text = "a:\n  x: 1\nb:\n  y: 2\n"
        assert find_key_block_lines(text, "a") == [1, 2]
        assert find_key_block_lines(text, "b") == [3, 4]

    def test_block_terminated_by_eof_does_not_infinite_loop(self):
        text = "default:\n  allowed_tables:\n    - a\n    - b"
        assert find_key_block_lines(text, "allowed_tables") == [2, 3, 4]

    def test_blank_lines_inside_a_block_do_not_terminate_it(self):
        text = "default:\n  allowed_tables:\n    - a\n\n    - b\n  max_limit: 100\n"
        assert find_key_block_lines(text, "allowed_tables") == [2, 3, 5]


# ---------------------------------------------------------------------------
# masked_columns_for (F7 + robustness fix found in review)
# ---------------------------------------------------------------------------


class TestMaskedColumnsFor:
    def _write(self, tmp_path, content):
        p = tmp_path / "policy.yaml"
        p.write_text(content)
        return p

    def test_reads_real_shape(self, tmp_path):
        path = self._write(
            tmp_path,
            "default:\n"
            "  column_masks:\n"
            "    customers:\n"
            "      - column: phone\n"
            "        kind: last\n"
            "      - column: national_id\n"
            "        kind: hash\n",
        )
        assert masked_columns_for(path, "customers") == ["phone", "national_id"]

    def test_table_with_no_masks_is_empty(self, tmp_path):
        path = self._write(tmp_path, "default:\n  column_masks:\n    customers: []\n")
        assert masked_columns_for(path, "customers") == []

    def test_missing_column_masks_key_is_empty(self, tmp_path):
        path = self._write(tmp_path, "default:\n  max_limit: 100\n")
        assert masked_columns_for(path, "customers") == []

    def test_null_default_does_not_raise(self, tmp_path):
        # doc.get("default") returning None (a present-but-null key) used to
        # crash with AttributeError on the next .get() call — the same
        # "present key, null value" trap _row_count_or_len's own docstring
        # documents having been bitten by once already, just in a different
        # module.
        path = self._write(tmp_path, "default:\n")
        assert masked_columns_for(path, "customers") == []

    def test_list_default_does_not_raise(self, tmp_path):
        path = self._write(tmp_path, "default: [a, b]\n")
        assert masked_columns_for(path, "customers") == []

    def test_unparseable_yaml_does_not_raise(self, tmp_path):
        path = self._write(tmp_path, "default: [unterminated\n")
        assert masked_columns_for(path, "customers") == []

    def test_missing_file_does_not_raise(self, tmp_path):
        assert masked_columns_for(tmp_path / "does_not_exist.yaml", "customers") == []
