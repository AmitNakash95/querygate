"""Unit tests for read-only audit-stream anomaly surfacing (TODO.md item 59).

`detect_anomalies` is a pure function over a list of `AuditEvent`s plus a fixed
`now`, so these tests drive it directly with hand-built events — no app, no
clock, no global state. `JsonlAuditEventSource` and `build_anomaly_report` are
tested against a temp file. The 32C boundary (this only ever *reads*) is
structural: there is no write path to assert against.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pydantic as pyd
import pytest

from querygate.admin.anomaly import (
    AnomalyReport,
    AnomalyThresholds,
    JsonlAuditEventSource,
    build_anomaly_report,
    detect_anomalies,
)
from querygate.audit.events import AuditEvent

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def _event(
    *,
    at: datetime,
    principal: str = "svc-a",
    connection: str = "demo",
    outcome: str = "success",
) -> AuditEvent:
    return AuditEvent(
        occurred_at=at,
        principal_id=principal,
        connection_id=connection,
        policy_decision="allowed" if outcome == "success" else "denied",
        outcome=outcome,
        query_shape={"from": "customers", "select": [{"kind": "column", "column": "id"}]},
        duration_ms=3,
    )


def _thresholds(**overrides) -> AnomalyThresholds:
    base = dict(
        recent_window_seconds=3600.0,
        baseline_window_seconds=3600.0,  # equal windows keep the arithmetic obvious
        min_baseline_events=10,
        min_recent_events=3,
        volume_spike_ratio=3.0,
        rejection_rate_delta=0.3,
    )
    base.update(overrides)
    return AnomalyThresholds(**base)


def _spread(n: int, *, start: datetime, span_seconds: float, **kw) -> list[AuditEvent]:
    """n events evenly spread across [start, start+span)."""
    if n == 0:
        return []
    step = span_seconds / n
    return [_event(at=start + timedelta(seconds=step * i + 1), **kw) for i in range(n)]


# --- detection -------------------------------------------------------------


def test_no_events_flags_nothing():
    flagged, truncated = detect_anomalies([], now=_NOW, thresholds=_thresholds())
    assert flagged == []
    assert truncated is False


def test_volume_spike_is_flagged():
    th = _thresholds()
    baseline_start = _NOW - timedelta(seconds=7200)
    recent_start = _NOW - timedelta(seconds=3600)
    # 10 baseline events, 40 recent events over equal windows → ratio 4.0 ≥ 3.0.
    events = _spread(10, start=baseline_start, span_seconds=3600)
    events += _spread(40, start=recent_start, span_seconds=3600)

    flagged, _ = detect_anomalies(events, now=_NOW, thresholds=th)

    assert len(flagged) == 1
    assert flagged[0].principal_id == "svc-a"
    kinds = {s.kind for s in flagged[0].signals}
    assert "volume_spike" in kinds
    spike = next(s for s in flagged[0].signals if s.kind == "volume_spike")
    assert spike.recent_count == 40
    assert spike.baseline_count == 10
    assert spike.ratio == pytest.approx(4.0)


def test_volume_below_ratio_not_flagged():
    th = _thresholds()
    events = _spread(10, start=_NOW - timedelta(seconds=7200), span_seconds=3600)
    events += _spread(20, start=_NOW - timedelta(seconds=3600), span_seconds=3600)  # ratio 2.0
    flagged, _ = detect_anomalies(events, now=_NOW, thresholds=th)
    assert flagged == []


def test_new_principal_without_baseline_is_not_flagged():
    th = _thresholds()
    # 50 recent events but zero baseline → min_baseline_events guard suppresses it.
    events = _spread(50, start=_NOW - timedelta(seconds=3600), span_seconds=3600)
    flagged, _ = detect_anomalies(events, now=_NOW, thresholds=th)
    assert flagged == []


def test_rejection_rate_spike_is_flagged():
    th = _thresholds()
    baseline_start = _NOW - timedelta(seconds=7200)
    recent_start = _NOW - timedelta(seconds=3600)
    # baseline: 20 events, all success (0% rejection).
    events = _spread(20, start=baseline_start, span_seconds=3600, outcome="success")
    # recent: 10 events, 8 rejected (80% rejection) → delta 0.8 ≥ 0.3.
    events += _spread(2, start=recent_start, span_seconds=1800, outcome="success")
    events += _spread(
        8, start=recent_start + timedelta(seconds=1800), span_seconds=1800, outcome="rejected"
    )

    flagged, _ = detect_anomalies(events, now=_NOW, thresholds=th)

    assert len(flagged) == 1
    rej = next(s for s in flagged[0].signals if s.kind == "rejection_rate_spike")
    assert rej.recent_rejection_rate == pytest.approx(0.8)
    assert rej.baseline_rejection_rate == pytest.approx(0.0)


def test_new_connection_access_is_flagged():
    th = _thresholds()
    baseline_start = _NOW - timedelta(seconds=7200)
    recent_start = _NOW - timedelta(seconds=3600)
    events = _spread(15, start=baseline_start, span_seconds=3600, connection="demo")
    # recent traffic still mostly on "demo" but reaches "payroll" it never touched.
    events += _spread(8, start=recent_start, span_seconds=3000, connection="demo")
    events += _spread(
        2, start=recent_start + timedelta(seconds=3000), span_seconds=500, connection="payroll"
    )

    flagged, _ = detect_anomalies(events, now=_NOW, thresholds=th)

    assert len(flagged) == 1
    new_conn = [s for s in flagged[0].signals if s.kind == "new_connection_access"]
    assert len(new_conn) == 1
    assert new_conn[0].connection == "payroll"


def test_events_outside_the_baseline_window_are_ignored():
    th = _thresholds()
    # An old burst well before the baseline window must not inflate the baseline.
    old = _spread(500, start=_NOW - timedelta(seconds=100_000), span_seconds=1000)
    events = old + _spread(5, start=_NOW - timedelta(seconds=1800), span_seconds=1800)
    flagged, _ = detect_anomalies(events, now=_NOW, thresholds=th)
    # No baseline (the 500 are out of window) → the recent 5 can't be judged.
    assert flagged == []


def test_unauthenticated_events_group_under_a_sentinel():
    th = _thresholds()
    events = [_event(at=_NOW - timedelta(seconds=1000), principal=None) for _ in range(5)]
    events += _spread(10, start=_NOW - timedelta(seconds=7200), span_seconds=3600, principal=None)
    events += _spread(40, start=_NOW - timedelta(seconds=3600), span_seconds=3600, principal=None)
    flagged, _ = detect_anomalies(events, now=_NOW, thresholds=th)
    assert flagged
    assert flagged[0].principal_id == "<unauthenticated>"


def test_principal_cap_truncates_and_ranks_by_severity():
    th = _thresholds(max_principals_reported=2)
    events: list[AuditEvent] = []
    # Three spiking principals with increasing ratios (3x, 5x, 9x).
    for principal, recent in (("p-lo", 30), ("p-mid", 50), ("p-hi", 90)):
        events += _spread(
            10, start=_NOW - timedelta(seconds=7200), span_seconds=3600, principal=principal
        )
        events += _spread(
            recent, start=_NOW - timedelta(seconds=3600), span_seconds=3600, principal=principal
        )

    flagged, truncated = detect_anomalies(events, now=_NOW, thresholds=th)

    assert truncated is True
    assert [p.principal_id for p in flagged] == ["p-hi", "p-mid"]  # weakest dropped


def test_multiple_signals_for_one_principal():
    th = _thresholds()
    baseline_start = _NOW - timedelta(seconds=7200)
    recent_start = _NOW - timedelta(seconds=3600)
    events = _spread(
        10, start=baseline_start, span_seconds=3600, connection="demo", outcome="success"
    )
    # Recent: big volume, high rejection, and a brand-new connection.
    events += _spread(
        35, start=recent_start, span_seconds=2000, connection="demo", outcome="rejected"
    )
    events += _spread(
        5,
        start=recent_start + timedelta(seconds=2000),
        span_seconds=1000,
        connection="secret-db",
        outcome="rejected",
    )

    flagged, _ = detect_anomalies(events, now=_NOW, thresholds=th)
    kinds = {s.kind for s in flagged[0].signals}
    assert kinds == {"volume_spike", "rejection_rate_spike", "new_connection_access"}


# --- JSONL source ----------------------------------------------------------


def _write_jsonl(path, events, *, extra_lines=()):
    with open(path, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(event.model_dump_json(exclude_none=True) + "\n")
        for line in extra_lines:
            handle.write(line + "\n")


def test_jsonl_source_reads_query_events_and_ignores_others(tmp_path):
    th = _thresholds()
    path = tmp_path / "audit.jsonl"
    events = _spread(5, start=_NOW - timedelta(seconds=1800), span_seconds=1800)
    _write_jsonl(
        path,
        events,
        extra_lines=(
            json.dumps({"event_type": "config.governance", "action": "apply"}),
            json.dumps({"event_type": "catalog.governance", "action": "publish"}),
            "",  # blank line skipped, not malformed
        ),
    )
    source = JsonlAuditEventSource(str(path))
    loaded, malformed, truncated = source.load_query_events(now=_NOW, thresholds=th)
    assert len(loaded) == 5
    assert malformed == 0
    assert truncated is False


def test_jsonl_source_counts_malformed_lines(tmp_path):
    th = _thresholds()
    path = tmp_path / "audit.jsonl"
    _write_jsonl(
        path,
        _spread(3, start=_NOW - timedelta(seconds=600), span_seconds=600),
        extra_lines=(
            "{not json",  # invalid JSON
            json.dumps({"event_type": "query.execution", "outcome": "success"}),  # missing fields
        ),
    )
    source = JsonlAuditEventSource(str(path))
    loaded, malformed, _ = source.load_query_events(now=_NOW, thresholds=th)
    assert len(loaded) == 3
    assert malformed == 2


def test_jsonl_source_filters_out_of_window_events(tmp_path):
    th = _thresholds()  # combined window = 7200s
    path = tmp_path / "audit.jsonl"
    in_window = _spread(4, start=_NOW - timedelta(seconds=1000), span_seconds=1000)
    stale = _spread(6, start=_NOW - timedelta(seconds=50_000), span_seconds=1000)
    future = [_event(at=_NOW + timedelta(seconds=60))]
    _write_jsonl(path, in_window + stale + future)
    source = JsonlAuditEventSource(str(path))
    loaded, _, _ = source.load_query_events(now=_NOW, thresholds=th)
    assert len(loaded) == 4


def test_jsonl_source_bounds_and_reports_truncation(tmp_path):
    th = _thresholds(max_events_scanned=3)
    path = tmp_path / "audit.jsonl"
    _write_jsonl(path, _spread(10, start=_NOW - timedelta(seconds=1800), span_seconds=1800))
    source = JsonlAuditEventSource(str(path))
    loaded, _, truncated = source.load_query_events(now=_NOW, thresholds=th)
    assert len(loaded) == 3  # deque maxlen
    assert truncated is True


def test_jsonl_source_missing_file_is_empty_not_error(tmp_path):
    source = JsonlAuditEventSource(str(tmp_path / "does-not-exist.jsonl"))
    loaded, malformed, truncated = source.load_query_events(now=_NOW, thresholds=_thresholds())
    assert loaded == [] and malformed == 0 and truncated is False


# --- build_anomaly_report --------------------------------------------------


def test_report_with_no_source_is_disabled_not_error():
    report = build_anomaly_report(None, now=_NOW, thresholds=_thresholds())
    assert report.source == "disabled"
    assert report.principals == []
    assert report.generated_at == _NOW.isoformat()


def test_report_end_to_end_flags_a_spike(tmp_path):
    th = _thresholds()
    path = tmp_path / "audit.jsonl"
    events = _spread(10, start=_NOW - timedelta(seconds=7200), span_seconds=3600)
    events += _spread(40, start=_NOW - timedelta(seconds=3600), span_seconds=3600)
    _write_jsonl(path, events)

    report = build_anomaly_report(JsonlAuditEventSource(str(path)), now=_NOW, thresholds=th)
    assert report.source == "jsonl"
    assert report.events_scanned == 50
    assert len(report.principals) == 1
    assert any(s.kind == "volume_spike" for s in report.principals[0].signals)


def test_report_is_redaction_safe():
    """The serialized report must never carry query shape, SQL, or values —
    only ids, counts, and rates. Proven against the live schema, not by eye."""
    th = _thresholds()
    events = _spread(10, start=_NOW - timedelta(seconds=7200), span_seconds=3600)
    events += _spread(40, start=_NOW - timedelta(seconds=3600), span_seconds=3600)

    class _Source:
        def load_query_events(self, *, now, thresholds):
            return events, 0, False

    report = build_anomaly_report(_Source(), now=_NOW, thresholds=th)
    blob = report.model_dump_json()
    for forbidden in ("query_shape", "customers", '"select"', "sql", "duration_ms"):
        assert forbidden not in blob

    # And the schema forbids adding such a field silently.
    with pytest.raises(pyd.ValidationError):
        AnomalyReport(
            generated_at=_NOW.isoformat(),
            recent_window_seconds=1,
            baseline_window_seconds=1,
            query_shape={"leak": 1},  # type: ignore[call-arg]
        )


# --- thresholds / config validation ---------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"recent_window_seconds": 0},
        {"baseline_window_seconds": -1},
        {"volume_spike_ratio": 1.0},  # must be strictly > 1
        {"rejection_rate_delta": 0},  # must be > 0
        {"rejection_rate_delta": 1.5},  # must be <= 1
        {"min_baseline_events": 0},
        {"min_recent_events": 0},
        {"max_events_scanned": 0},
        {"max_principals_reported": 0},
        {"max_new_connections_per_principal": 0},
    ],
)
def test_thresholds_reject_out_of_range_values(overrides):
    with pytest.raises(pyd.ValidationError):
        AnomalyThresholds(**overrides)


# --- source / report edge cases -------------------------------------------


def test_jsonl_source_with_no_in_window_events_is_empty_but_still_jsonl(tmp_path):
    """A configured-but-quiet stream is source="jsonl" with zero events — not
    "disabled", which specifically means no sink is configured at all."""
    path = tmp_path / "audit.jsonl"
    _write_jsonl(path, _spread(5, start=_NOW - timedelta(seconds=50_000), span_seconds=1000))
    report = build_anomaly_report(
        JsonlAuditEventSource(str(path)), now=_NOW, thresholds=_thresholds()
    )
    assert report.source == "jsonl"
    assert report.events_scanned == 0
    assert report.principals == []


# --- new-connection cap and baseline guard --------------------------------


def test_new_connection_signals_are_capped_per_principal():
    th = _thresholds(max_new_connections_per_principal=2, volume_spike_ratio=99)
    baseline_start = _NOW - timedelta(seconds=7200)
    recent_start = _NOW - timedelta(seconds=3600)
    events = _spread(15, start=baseline_start, span_seconds=3600, connection="demo")
    # Reaches five brand-new connections; only the first two (sorted) are kept.
    for name in ("c1", "c2", "c3", "c4", "c5"):
        events += _spread(
            1, start=recent_start + timedelta(seconds=100), span_seconds=50, connection=name
        )
    events += _spread(3, start=recent_start, span_seconds=50, connection="demo")

    flagged, _ = detect_anomalies(events, now=_NOW, thresholds=th)
    new_conns = [s for s in flagged[0].signals if s.kind == "new_connection_access"]
    assert [s.connection for s in new_conns] == ["c1", "c2"]


def test_new_connection_needs_sufficient_baseline_volume():
    th = _thresholds(min_baseline_events=10)
    recent_start = _NOW - timedelta(seconds=3600)
    # Only 5 baseline events (< min) — even reaching a new connection is not judged.
    events = _spread(5, start=_NOW - timedelta(seconds=7200), span_seconds=3600, connection="demo")
    events += _spread(4, start=recent_start, span_seconds=1000, connection="payroll")
    flagged, _ = detect_anomalies(events, now=_NOW, thresholds=th)
    assert flagged == []


# --- window-boundary classification ----------------------------------------


def test_window_boundaries_are_classified_deterministically():
    th = _thresholds(min_baseline_events=10, min_recent_events=3, volume_spike_ratio=1.5)
    recent_start = _NOW - timedelta(seconds=3600)
    baseline_start = _NOW - timedelta(seconds=7200)
    events = _spread(12, start=baseline_start + timedelta(seconds=10), span_seconds=3000)
    events += _spread(40, start=recent_start + timedelta(seconds=10), span_seconds=3000)
    # Exactly at recent_start → baseline (occurred > recent_start is False).
    events.append(_event(at=recent_start))
    # Exactly at now → recent (occurred > now is False, so it is included).
    events.append(_event(at=_NOW))
    # Exactly at baseline_start → excluded (occurred <= baseline_start).
    events.append(_event(at=baseline_start))

    flagged, _ = detect_anomalies(events, now=_NOW, thresholds=th)
    assert flagged[0].recent_events == 41  # 40 + the now event
    assert flagged[0].baseline_events == 13  # 12 + the recent_start event; baseline_start dropped


def test_naive_timestamps_are_treated_as_utc():
    """Persisted events could carry a naive occurred_at; detection must not
    crash comparing them and must place them by the same window logic."""
    th = _thresholds()
    naive_now = _NOW.replace(tzinfo=None)
    events = [_event(at=naive_now - timedelta(seconds=3600 + 60 * (i + 1))) for i in range(10)]
    events += [_event(at=naive_now - timedelta(seconds=60 * (i + 1))) for i in range(40)]
    flagged, _ = detect_anomalies(events, now=_NOW, thresholds=th)
    assert len(flagged) == 1
    assert any(s.kind == "volume_spike" for s in flagged[0].signals)


# --- route helpers (config → thresholds/source wiring) ---------------------


def test_route_helpers_map_config_to_thresholds_and_source(tmp_path):
    from querygate.api.admin_observability_routes import _anomaly_source, _anomaly_thresholds
    from querygate.core.config import AppConfig

    audit_path = str(tmp_path / "a.jsonl")
    cfg = AppConfig(
        environment="localhost",
        audit_sink_backend="jsonl",
        audit_jsonl_path=audit_path,
        anomaly_recent_window_seconds=1200.0,
        anomaly_baseline_window_seconds=48000.0,
        anomaly_min_baseline_events=7,
        anomaly_volume_spike_ratio=4.5,
        anomaly_max_principals_reported=33,
    )
    th = _anomaly_thresholds(cfg)
    assert th.recent_window_seconds == 1200.0
    assert th.baseline_window_seconds == 48000.0
    assert th.min_baseline_events == 7
    assert th.volume_spike_ratio == 4.5
    assert th.max_principals_reported == 33

    source = _anomaly_source(cfg)
    assert isinstance(source, JsonlAuditEventSource)
    assert str(source.path) == audit_path

    # No persisted sink → no source (endpoint reports "disabled").
    cfg_none = AppConfig(environment="localhost", audit_sink_backend="none")
    assert _anomaly_source(cfg_none) is None
