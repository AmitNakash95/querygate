"""Unit tests for safe explanations of a caller's own recent denials
(TODO.md item 45, phase 2).

`select_recent_denials` is a pure function over a list of `AuditEvent`s, so
these tests drive it directly with hand-built events — no app, no clock, no
global state. `build_recent_denials_report` is tested against a temp file via
the reused `JsonlAuditEventSource` (item 59), mirroring
`tests/unit/test_anomaly.py`'s and `tests/unit/test_config_trends.py`'s shape.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pydantic as pyd
import pytest

from querygate.admin.anomaly import JsonlAuditEventSource
from querygate.audit.events import AuditEvent
from querygate.help.personal_denials import (
    PersonalDenialsCooldown,
    RecentDenialsReport,
    build_recent_denials_report,
    select_recent_denials,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


def _event(
    *,
    at: datetime,
    principal: str = "user-a",
    connection: str = "demo",
    outcome: str = "rejected",
    error_category: str | None = "policy",
    surface: str = "rest",
    operation: str = "execute_structured_query",
) -> AuditEvent:
    return AuditEvent(
        occurred_at=at,
        principal_id=principal,
        connection_id=connection,
        policy_decision="denied" if outcome == "rejected" else "allowed",
        outcome=outcome,
        surface=surface,
        query_shape={"from": "customers", "select": [{"kind": "column", "column": "id"}]},
        error_category=error_category,
        duration_ms=3,
        operation=operation,
    )


# --- select_recent_denials ---------------------------------------------------


def test_no_events_yields_no_denials():
    assert select_recent_denials([], principal_id="user-a", limit=10) == []


def test_only_the_callers_own_events_are_returned():
    events = [
        _event(at=_NOW, principal="user-a"),
        _event(at=_NOW, principal="user-b"),
    ]
    denials = select_recent_denials(events, principal_id="user-a", limit=10)
    assert len(denials) == 1


def test_success_events_are_excluded_even_for_the_caller():
    events = [_event(at=_NOW, principal="user-a", outcome="success", error_category=None)]
    assert select_recent_denials(events, principal_id="user-a", limit=10) == []


def test_denials_are_sorted_most_recent_first():
    events = [
        _event(at=_NOW - timedelta(seconds=300), principal="user-a", connection="c-old"),
        _event(at=_NOW, principal="user-a", connection="c-new"),
        _event(at=_NOW - timedelta(seconds=100), principal="user-a", connection="c-mid"),
    ]
    denials = select_recent_denials(events, principal_id="user-a", limit=10)
    assert [d.connection for d in denials] == ["c-new", "c-mid", "c-old"]


def test_denials_are_capped_at_limit():
    events = [_event(at=_NOW - timedelta(seconds=i)) for i in range(5)]
    denials = select_recent_denials(events, principal_id="user-a", limit=2)
    assert len(denials) == 2


@pytest.mark.parametrize(
    "category,expected_snippet",
    [
        ("policy", "policy"),
        ("schema", "table or column"),
        ("quota", "quota"),
        ("cost_estimate", "estimated cost"),
        ("concurrency", "concurrency limit"),
        ("queue_full", "queue"),
        ("approval_required", "approval"),
        ("not_found", "does not exist"),
        ("db_error", "database itself"),
    ],
)
def test_known_categories_get_a_specific_explanation(category, expected_snippet):
    denials = select_recent_denials(
        [_event(at=_NOW, error_category=category)], principal_id="user-a", limit=10
    )
    assert denials[0].reason == category
    assert expected_snippet in denials[0].explanation


def test_query_verdict_denials_are_excluded_entirely():
    """Item 133's audit: `StructuredQueryService.verdict` collapses every
    denial into one generic response specifically so a caller can't learn
    whether policy or schema rejected their query. Before this fix, the same
    caller could immediately read the real `error_category` back through
    this endpoint for their own just-submitted verdict probe — completely
    reconstructing the distinction `verdict()`'s response deliberately
    withheld. A `query_verdict` event must not appear in the caller's own
    denial list at all, even though it is that caller's own rejected event."""
    events = [
        _event(at=_NOW, principal="user-a", operation="query_verdict", error_category="policy"),
        _event(at=_NOW, principal="user-a", operation="execute_structured_query"),
    ]
    denials = select_recent_denials(events, principal_id="user-a", limit=10)
    assert len(denials) == 1
    assert all(d.connection == "demo" for d in denials)


def test_own_denials_found_also_excludes_query_verdict_events(tmp_path):
    """`own_denials_found` is a separate count from `select_recent_denials`'s
    output (see the fleet-wide-volume regression above) — it must exclude
    `query_verdict` events too, not just omit them from the visible list."""
    path = tmp_path / "audit.jsonl"
    _write_jsonl(
        path,
        [
            _event(at=_NOW, principal="user-a", operation="query_verdict"),
            _event(at=_NOW, principal="user-a", operation="execute_structured_query"),
        ],
    )
    report = build_recent_denials_report(
        JsonlAuditEventSource(str(path)), principal_id="user-a", now=_NOW
    )
    assert report.own_denials_found == 1
    assert len(report.denials) == 1


def test_unknown_category_falls_back_to_generic_explanation():
    denials = select_recent_denials(
        [_event(at=_NOW, error_category=None)], principal_id="user-a", limit=10
    )
    assert denials[0].reason == "unknown"
    assert "wasn't recorded" in denials[0].explanation


def test_denial_never_carries_the_query_shape():
    denials = select_recent_denials([_event(at=_NOW)], principal_id="user-a", limit=10)
    blob = denials[0].model_dump_json()
    assert "query_shape" not in blob
    assert "customers" not in blob


# --- build_recent_denials_report ---------------------------------------------


def _write_jsonl(path, events):
    with open(path, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(event.model_dump_json(exclude_none=True) + "\n")


def test_report_with_no_source_is_disabled_not_error():
    report = build_recent_denials_report(None, principal_id="user-a", now=_NOW)
    assert report.source == "disabled"
    assert report.denials == []
    assert report.generated_at == _NOW.isoformat()


def test_report_end_to_end_filters_to_the_caller(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_jsonl(
        path,
        [
            _event(at=_NOW - timedelta(seconds=100), principal="user-a", connection="c1"),
            _event(at=_NOW - timedelta(seconds=50), principal="user-b", connection="c2"),
        ],
    )
    report = build_recent_denials_report(
        JsonlAuditEventSource(str(path)),
        principal_id="user-a",
        now=_NOW,
        lookback_seconds=3600.0,
    )
    assert report.source == "jsonl"
    # Two events exist in the file (one per principal), but own_denials_found
    # must reflect only the caller's own — never the fleet-wide scan total.
    assert report.own_denials_found == 1
    assert len(report.denials) == 1
    assert report.denials[0].connection == "c1"


def test_own_denials_found_never_reflects_fleet_wide_volume(tmp_path):
    """Regression: own_denials_found must count only the caller's own
    denials, never the total scanned across every principal — this endpoint
    requires no admin scope, so a fleet-wide count would leak cross-principal
    audit volume to any authenticated caller."""
    path = tmp_path / "audit.jsonl"
    _write_jsonl(
        path,
        [_event(at=_NOW - timedelta(seconds=i), principal="someone-else") for i in range(50)]
        + [_event(at=_NOW, principal="user-a")],
    )
    report = build_recent_denials_report(
        JsonlAuditEventSource(str(path)), principal_id="user-a", now=_NOW
    )
    assert report.own_denials_found == 1


def test_report_is_empty_but_still_jsonl_when_stream_is_quiet(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_jsonl(path, [_event(at=_NOW - timedelta(seconds=50_000))])  # outside lookback
    report = build_recent_denials_report(
        JsonlAuditEventSource(str(path)),
        principal_id="user-a",
        now=_NOW,
        lookback_seconds=3600.0,
    )
    assert report.source == "jsonl"
    assert report.denials == []


def test_report_respects_configured_max_consecutive_out_of_window(tmp_path):
    # TODO.md item 141 (security-review follow-up, docs/THREAT_MODEL.md
    # QG-43): this reader reuses `JsonlAuditEventSource.load_query_events`
    # (item 59), which gained a windowed early-exit — this surface must not
    # silently inherit `AnomalyThresholds`' own class default, the same
    # requirement item 138 already established for `max_lines_read`
    # (`_write_jsonl` above mirrors `tests/unit/test_anomaly.py`'s file-order
    # convention: physically-first = written first). The caller's own
    # in-window denial sits physically BEFORE a run of out-of-window events
    # from another principal — an inverted/merged-file shape, deliberately
    # simulating the QG-43 threat, not a normal single-writer stream — so a
    # low tolerance stops the scan before ever reaching it, while the
    # (much larger) class default does not.
    path = tmp_path / "audit.jsonl"
    _write_jsonl(
        path,
        [_event(at=_NOW - timedelta(seconds=100), principal="user-a")]
        + [_event(at=_NOW - timedelta(seconds=50_000), principal="someone-else") for _ in range(5)],
    )
    default_report = build_recent_denials_report(
        JsonlAuditEventSource(str(path)),
        principal_id="user-a",
        now=_NOW,
        lookback_seconds=3600.0,
    )
    assert default_report.own_denials_found == 1
    assert default_report.scan_ended_on_out_of_window_run is False

    tight_report = build_recent_denials_report(
        JsonlAuditEventSource(str(path)),
        principal_id="user-a",
        now=_NOW,
        lookback_seconds=3600.0,
        max_consecutive_out_of_window=3,
    )
    assert tight_report.own_denials_found == 0
    # TODO.md item 171: this is precisely the disclosure this item adds — a
    # caller-facing consumer can now tell `own_denials_found == 0` here means
    # "the scan heuristically stopped early on a merged/multi-writer-shaped
    # file", not "this caller genuinely has no denials in the window". Before
    # this field existed, `tight_report` and a genuinely-empty result were
    # indistinguishable over the wire.
    assert tight_report.scan_ended_on_out_of_window_run is True


def test_report_never_leaks_another_principals_denial(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_jsonl(path, [_event(at=_NOW, principal="someone-else", connection="secret-conn")])
    report = build_recent_denials_report(
        JsonlAuditEventSource(str(path)), principal_id="user-a", now=_NOW
    )
    blob = report.model_dump_json()
    assert "secret-conn" not in blob
    assert "someone-else" not in blob


def test_report_respects_configured_limit(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_jsonl(path, [_event(at=_NOW - timedelta(seconds=i)) for i in range(10)])
    report = build_recent_denials_report(
        JsonlAuditEventSource(str(path)), principal_id="user-a", now=_NOW, limit=3
    )
    assert len(report.denials) == 3


def test_report_is_redaction_safe():
    """The serialized report must never carry a query shape, SQL, or another
    principal's identity — only occurred-at, connection, surface, reason, and
    a fixed explanation. Proven against the live schema, not by eye."""

    class _Source:
        def load_query_events(self, *, now, thresholds):
            return [_event(at=_NOW, principal="user-a")], 0, False, False

    report = build_recent_denials_report(_Source(), principal_id="user-a", now=_NOW)
    blob = report.model_dump_json()
    for forbidden in ("query_shape", "customers", '"select"'):
        assert forbidden not in blob

    with pytest.raises(pyd.ValidationError):
        RecentDenialsReport(
            generated_at=_NOW.isoformat(),
            lookback_seconds=1,
            leaked_field=1,  # type: ignore[call-arg]
        )


class TestPersonalDenialsCooldown:
    """TODO.md item 126. Direct, white-box tests of the cooldown class itself
    (mirroring `test_health.py`'s coverage of `HealthMonitor`'s identical
    per-key cooldown shape), plus the pruning fix a same-day
    `security-invariant-reviewer` finding on this item's own completion gate
    added."""

    def test_first_call_is_always_allowed(self):
        cooldown = PersonalDenialsCooldown()
        assert cooldown.seconds_until_allowed("p1", 10.0) == 0.0

    def test_second_call_within_the_window_is_blocked(self):
        cooldown = PersonalDenialsCooldown()
        cooldown.record_request("p1", 10.0)
        remaining = cooldown.seconds_until_allowed("p1", 10.0)
        assert 0 < remaining <= 10

    def test_cooldown_is_scoped_per_principal(self):
        cooldown = PersonalDenialsCooldown()
        cooldown.record_request("p1", 10.0)
        assert cooldown.seconds_until_allowed("p2", 10.0) == 0.0

    def test_zero_cooldown_disables_the_limit(self):
        cooldown = PersonalDenialsCooldown()
        cooldown.record_request("p1", 0.0)
        assert cooldown.seconds_until_allowed("p1", 0.0) == 0.0

    def test_disabled_cooldown_records_nothing(self):
        # If cooldown_seconds<=0 recorded anyway, the map would grow forever
        # on every request even though seconds_until_allowed never consults
        # it in that case — pure waste with no behavioral effect.
        cooldown = PersonalDenialsCooldown()
        cooldown.record_request("p1", 0.0)
        assert cooldown._last_request == {}

    def test_record_prunes_entries_whose_own_cooldown_has_elapsed(self, monkeypatch):
        # TODO.md item 126 follow-up (2026-08-10 security-invariant-reviewer):
        # an unpruned map would grow one permanent entry per distinct
        # principal ever seen. Simulates 1,000 distinct principals recorded
        # at t=1000, then one more recorded after their 0.01s cooldowns have
        # all elapsed — only the newest entry should survive.
        import querygate.help.personal_denials as personal_denials_module

        fake_now = [1000.0]
        monkeypatch.setattr(personal_denials_module.time, "monotonic", lambda: fake_now[0])
        cooldown = PersonalDenialsCooldown()
        for i in range(1000):
            cooldown.record_request(f"p{i}", 0.01)
        assert len(cooldown._last_request) == 1000

        fake_now[0] += 1.0
        cooldown.record_request("new-principal", 0.01)
        assert len(cooldown._last_request) == 1
        assert set(cooldown._last_request) == {"new-principal"}
