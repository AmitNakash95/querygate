"""Unit tests for config/catalog-change trend surfacing (TODO.md item 44,
phase 2 slice).

`build_change_trend` is a pure function over a list of
`ConfigChangeEvent`/`CatalogGovernanceEvent`s plus a fixed `now`, so these
tests drive it directly with hand-built events — no app, no clock, no global
state. `JsonlChangeEventSource` and `build_change_trend_report` are tested
against a temp file, mirroring `tests/unit/test_anomaly.py`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pydantic as pyd
import pytest

from querygate.admin.config_trends import (
    ChangeTrendThresholds,
    ConfigCatalogChangeTrend,
    JsonlChangeEventSource,
    build_change_trend,
    build_change_trend_report,
)
from querygate.audit.events import CatalogGovernanceEvent, ConfigChangeEvent

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


def _config_event(
    *, at: datetime, action: str = "apply", outcome: str = "success"
) -> ConfigChangeEvent:
    return ConfigChangeEvent(occurred_at=at, action=action, outcome=outcome)


def _catalog_event(
    *, at: datetime, action: str = "publish", outcome: str = "success"
) -> CatalogGovernanceEvent:
    return CatalogGovernanceEvent(occurred_at=at, action=action, outcome=outcome)


def _thresholds(**overrides) -> ChangeTrendThresholds:
    base = dict(recent_window_seconds=3600.0, baseline_window_seconds=3600.0)
    base.update(overrides)
    return ChangeTrendThresholds(**base)


def _spread(n, *, start, span_seconds, kind="config", **kw):
    if n == 0:
        return []
    step = span_seconds / n
    maker = _config_event if kind == "config" else _catalog_event
    return [maker(at=start + timedelta(seconds=step * i + 1), **kw) for i in range(n)]


# --- build_change_trend -----------------------------------------------------


def test_no_events_yields_all_zero_windows():
    config_recent, config_baseline, catalog_recent, catalog_baseline = build_change_trend(
        [], now=_NOW, thresholds=_thresholds()
    )
    assert config_recent.total == 0
    assert config_baseline.total == 0
    assert catalog_recent.total == 0
    assert catalog_baseline.total == 0


def test_config_and_catalog_events_are_bucketed_separately():
    events = _spread(3, start=_NOW - timedelta(seconds=1800), span_seconds=1800, kind="config")
    events += _spread(2, start=_NOW - timedelta(seconds=1800), span_seconds=1800, kind="catalog")
    config_recent, _, catalog_recent, _ = build_change_trend(
        events, now=_NOW, thresholds=_thresholds()
    )
    assert config_recent.total == 3
    assert catalog_recent.total == 2


def test_recent_vs_baseline_split_by_window():
    baseline_start = _NOW - timedelta(seconds=7200)
    recent_start = _NOW - timedelta(seconds=3600)
    events = _spread(10, start=baseline_start, span_seconds=3600, kind="config")
    events += _spread(40, start=recent_start, span_seconds=3600, kind="config")
    config_recent, config_baseline, _, _ = build_change_trend(
        events, now=_NOW, thresholds=_thresholds()
    )
    assert config_recent.total == 40
    assert config_baseline.total == 10


def test_outcome_is_split_into_success_and_rejected():
    events = _spread(3, start=_NOW - timedelta(seconds=1800), span_seconds=1800, outcome="success")
    events += _spread(
        2, start=_NOW - timedelta(seconds=1800), span_seconds=1800, outcome="rejected"
    )
    config_recent, _, _, _ = build_change_trend(events, now=_NOW, thresholds=_thresholds())
    assert config_recent.total == 5
    assert config_recent.rejected == 2


def test_by_action_breakdown():
    events = _spread(3, start=_NOW - timedelta(seconds=1800), span_seconds=1800, action="stage")
    events += _spread(2, start=_NOW - timedelta(seconds=1800), span_seconds=1800, action="apply")
    config_recent, _, _, _ = build_change_trend(events, now=_NOW, thresholds=_thresholds())
    by_action = {a.action: a for a in config_recent.by_action}
    assert by_action["stage"].success == 3
    assert by_action["apply"].success == 2


def test_action_success_and_rejected_counted_independently():
    events = [
        _config_event(at=_NOW - timedelta(seconds=100), action="apply", outcome="success"),
        _config_event(at=_NOW - timedelta(seconds=200), action="apply", outcome="rejected"),
        _config_event(at=_NOW - timedelta(seconds=300), action="apply", outcome="rejected"),
    ]
    config_recent, _, _, _ = build_change_trend(events, now=_NOW, thresholds=_thresholds())
    apply_bucket = next(a for a in config_recent.by_action if a.action == "apply")
    assert apply_bucket.success == 1
    assert apply_bucket.rejected == 2


def test_rate_per_min_is_computed_from_window_size():
    events = _spread(60, start=_NOW - timedelta(seconds=1800), span_seconds=1800, kind="config")
    config_recent, _, _, _ = build_change_trend(events, now=_NOW, thresholds=_thresholds())
    # 60 events over a 3600s recent window -> 1/min.
    assert config_recent.rate_per_min == pytest.approx(1.0)


def test_naive_timestamps_are_treated_as_utc():
    naive_now = _NOW.replace(tzinfo=None)
    events = [_config_event(at=naive_now - timedelta(seconds=100))]
    config_recent, _, _, _ = build_change_trend(events, now=_NOW, thresholds=_thresholds())
    assert config_recent.total == 1


def test_window_boundary_classification():
    recent_start = _NOW - timedelta(seconds=3600)
    events = [
        _config_event(at=recent_start),  # exactly at boundary -> baseline
        _config_event(at=_NOW),  # exactly at now -> recent
    ]
    config_recent, config_baseline, _, _ = build_change_trend(
        events, now=_NOW, thresholds=_thresholds()
    )
    assert config_recent.total == 1
    assert config_baseline.total == 1


# --- JSONL source ------------------------------------------------------------


def _write_jsonl(path, events, *, extra_lines=()):
    with open(path, "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(event.model_dump_json(exclude_none=True) + "\n")
        for line in extra_lines:
            handle.write(line + "\n")


def test_jsonl_source_reads_config_and_catalog_events_and_ignores_others(tmp_path):
    th = _thresholds()
    path = tmp_path / "audit.jsonl"
    events = _spread(3, start=_NOW - timedelta(seconds=1800), span_seconds=1800, kind="config")
    events += _spread(2, start=_NOW - timedelta(seconds=1800), span_seconds=1800, kind="catalog")
    _write_jsonl(
        path,
        events,
        extra_lines=(
            json.dumps(
                {
                    "event_type": "query.execution",
                    "occurred_at": _NOW.isoformat(),
                    "outcome": "success",
                    "policy_decision": "allowed",
                    "query_shape": {"from": "t", "select": []},
                }
            ),
            "",  # blank line skipped, not malformed
        ),
    )
    source = JsonlChangeEventSource(str(path))
    loaded, malformed, truncated, _ = source.load_change_events(now=_NOW, thresholds=th)
    assert len(loaded) == 5
    assert malformed == 0
    assert truncated is False


def test_jsonl_source_counts_malformed_lines(tmp_path):
    th = _thresholds()
    path = tmp_path / "audit.jsonl"
    _write_jsonl(
        path,
        _spread(3, start=_NOW - timedelta(seconds=600), span_seconds=600, kind="config"),
        extra_lines=(
            "{not json",  # invalid JSON
            json.dumps({"event_type": "config.governance"}),  # missing required fields
        ),
    )
    source = JsonlChangeEventSource(str(path))
    loaded, malformed, _, _ = source.load_change_events(now=_NOW, thresholds=th)
    assert len(loaded) == 3
    assert malformed == 2


def test_jsonl_source_filters_out_of_window_events(tmp_path):
    th = _thresholds()  # combined window = 7200s
    path = tmp_path / "audit.jsonl"
    in_window = _spread(4, start=_NOW - timedelta(seconds=1000), span_seconds=1000, kind="config")
    stale = _spread(6, start=_NOW - timedelta(seconds=50_000), span_seconds=1000, kind="config")
    future = [_config_event(at=_NOW + timedelta(seconds=60))]
    _write_jsonl(path, in_window + stale + future)
    source = JsonlChangeEventSource(str(path))
    loaded, _, _, _ = source.load_change_events(now=_NOW, thresholds=th)
    assert len(loaded) == 4


def test_jsonl_source_early_exit_stops_before_reading_older_lines(tmp_path):
    # TODO.md item 141: mirrors test_anomaly.py's identical scenario for this
    # reader's own threshold/counter. A malformed sentinel at the physical
    # start of the file (oldest, read last tail-first) proves the scan
    # stopped without reaching it once the consecutive out-of-window run
    # crossed the threshold.
    th = _thresholds(max_consecutive_out_of_window=3)
    path = tmp_path / "audit.jsonl"
    out_of_window = _spread(
        5, start=_NOW - timedelta(seconds=50_000), span_seconds=1000, kind="config"
    )
    in_window = _spread(2, start=_NOW - timedelta(seconds=500), span_seconds=100, kind="config")
    lines = ["{not json"] + [
        e.model_dump_json(exclude_none=True) for e in out_of_window + in_window
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    source = JsonlChangeEventSource(str(path))
    loaded, malformed, truncated, ended_on_run = source.load_change_events(now=_NOW, thresholds=th)
    assert len(loaded) == 2
    assert malformed == 0  # the sentinel was never reached
    assert truncated is False
    assert ended_on_run is True


def test_jsonl_source_early_exit_fires_at_exactly_the_threshold(tmp_path):
    # Pins the `>=` boundary specifically — see test_anomaly.py's identical
    # scenario for the full rationale. EXACTLY `max_consecutive_out_of_window`
    # out-of-window events, no slack, sentinel immediately behind them.
    th = _thresholds(max_consecutive_out_of_window=3)
    path = tmp_path / "audit.jsonl"
    out_of_window = _spread(
        3, start=_NOW - timedelta(seconds=50_000), span_seconds=100, kind="config"
    )
    lines = ["{not json"] + [e.model_dump_json(exclude_none=True) for e in out_of_window]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    source = JsonlChangeEventSource(str(path))
    loaded, malformed, truncated, ended_on_run = source.load_change_events(now=_NOW, thresholds=th)
    assert loaded == []
    assert malformed == 0  # the sentinel was never reached
    assert truncated is False
    assert ended_on_run is True


def test_jsonl_source_early_exit_counter_shared_across_both_matching_types(tmp_path):
    # TODO.md item 141: config.governance and catalog.governance are counted
    # toward ONE shared counter (this reader treats them as one aggregation).
    # Threshold 3 with 2 config + 2 catalog out-of-window events (4 total,
    # alternating types) must still cross the threshold and fire the early
    # exit — a bug that scoped the counter per-type instead of sharing it
    # would never reach 3 of either type alone, so the scan would continue
    # to the sentinel and malformed would be 1, not 0.
    th = _thresholds(max_consecutive_out_of_window=3)
    path = tmp_path / "audit.jsonl"
    mixed = [
        _config_event(at=_NOW - timedelta(seconds=50_000)),
        _catalog_event(at=_NOW - timedelta(seconds=49_000)),
        _config_event(at=_NOW - timedelta(seconds=48_000)),
        _catalog_event(at=_NOW - timedelta(seconds=47_000)),
    ]
    lines = ["{not json"] + [e.model_dump_json(exclude_none=True) for e in mixed]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    source = JsonlChangeEventSource(str(path))
    loaded, malformed, truncated, ended_on_run = source.load_change_events(now=_NOW, thresholds=th)
    assert loaded == []
    assert malformed == 0  # the sentinel was never reached
    assert truncated is False
    assert ended_on_run is True


def test_jsonl_source_early_exit_counter_resets_on_an_in_window_event(tmp_path):
    th = _thresholds(max_consecutive_out_of_window=3)
    path = tmp_path / "audit.jsonl"
    old_run = _spread(2, start=_NOW - timedelta(seconds=50_000), span_seconds=100, kind="config")
    interrupter = _spread(1, start=_NOW - timedelta(seconds=400), span_seconds=1, kind="config")
    more_old = _spread(2, start=_NOW - timedelta(seconds=40_000), span_seconds=100, kind="config")
    lines = (
        ["{not json"]
        + [e.model_dump_json(exclude_none=True) for e in old_run]
        + [e.model_dump_json(exclude_none=True) for e in interrupter]
        + [e.model_dump_json(exclude_none=True) for e in more_old]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    source = JsonlChangeEventSource(str(path))
    loaded, malformed, truncated, ended_on_run = source.load_change_events(now=_NOW, thresholds=th)
    assert len(loaded) == 1  # only the interrupter is in-window
    assert malformed == 1  # the sentinel WAS reached: no premature exit
    assert truncated is False
    assert ended_on_run is False


def test_jsonl_source_early_exit_counter_ignores_other_event_types(tmp_path):
    # A non-matching-type line (query.execution) inside an out-of-window run
    # must neither advance nor reset the counter. Threshold 3, with 2
    # matching out-of-window events either side of an unrelated event type:
    # the consecutive *matching-type* count reaches 4 without ever being
    # broken, so the early exit must still fire before the sentinel.
    th = _thresholds(max_consecutive_out_of_window=3)
    path = tmp_path / "audit.jsonl"
    old_a = _spread(2, start=_NOW - timedelta(seconds=50_000), span_seconds=100, kind="config")
    old_b = _spread(2, start=_NOW - timedelta(seconds=40_000), span_seconds=100, kind="config")
    other_type = json.dumps({"event_type": "query.execution", "outcome": "success"})
    lines = (
        ["{not json"]
        + [e.model_dump_json(exclude_none=True) for e in old_a]
        + [other_type]
        + [e.model_dump_json(exclude_none=True) for e in old_b]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    source = JsonlChangeEventSource(str(path))
    loaded, malformed, truncated, ended_on_run = source.load_change_events(now=_NOW, thresholds=th)
    assert len(loaded) == 0
    assert malformed == 0  # the sentinel was never reached
    assert truncated is False
    assert ended_on_run is True


def test_jsonl_source_bounds_and_reports_truncation(tmp_path):
    th = _thresholds(max_events_scanned=3)
    path = tmp_path / "audit.jsonl"
    _write_jsonl(
        path, _spread(10, start=_NOW - timedelta(seconds=1800), span_seconds=1800, kind="config")
    )
    source = JsonlChangeEventSource(str(path))
    loaded, _, truncated, ended_on_run = source.load_change_events(now=_NOW, thresholds=th)
    assert len(loaded) == 3
    assert truncated is True
    # TODO.md item 171: a resource-bound stop is NOT the heuristic exit —
    # independent from the early-exit tests' truncated=False/ended_on_run=True.
    assert ended_on_run is False


def test_jsonl_source_at_exact_cap_is_not_truncated(tmp_path):
    # matched == max_events_scanned exactly: every in-window event fit, so
    # nothing was actually dropped and truncated must be False, not True.
    # Proves the cap check runs BEFORE appending (not after) — the file is
    # exhausted right after the Nth match, so this alone can't distinguish a
    # `>` vs `>=` comparison at the cap boundary; that variant is covered by
    # the over-cap scenario in test_jsonl_source_bounds_and_reports_truncation.
    th = _thresholds(max_events_scanned=10)
    path = tmp_path / "audit.jsonl"
    _write_jsonl(
        path, _spread(10, start=_NOW - timedelta(seconds=1800), span_seconds=1800, kind="config")
    )
    source = JsonlChangeEventSource(str(path))
    loaded, _, truncated, _ = source.load_change_events(now=_NOW, thresholds=th)
    assert len(loaded) == 10
    assert truncated is False


def test_jsonl_source_finds_recent_window_past_a_huge_prefix_of_old_lines(tmp_path):
    # TODO.md item 138: mirrors the identical test in test_anomaly.py. Reading
    # tail-first means old data physically ahead of the window is never even
    # touched, so a bound on lines read can never miss the recent window.
    th = _thresholds(max_events_scanned=10, max_lines_read=50)
    path = tmp_path / "audit.jsonl"
    ancient = _spread(10_000, start=_NOW - timedelta(days=365), span_seconds=3600, kind="config")
    recent = _spread(4, start=_NOW - timedelta(seconds=1800), span_seconds=1800, kind="config")
    _write_jsonl(path, ancient + recent)
    source = JsonlChangeEventSource(str(path))
    loaded, _, truncated, _ = source.load_change_events(now=_NOW, thresholds=th)
    assert len(loaded) == 4
    assert truncated is True  # can't prove completeness without reading the whole file


def test_jsonl_source_bounds_lines_read_not_just_events_retained(tmp_path):
    th = _thresholds(max_events_scanned=1000, max_lines_read=25)
    path = tmp_path / "audit.jsonl"
    noise = _spread(10_000, start=_NOW - timedelta(days=30), span_seconds=3600, kind="config")
    _write_jsonl(path, noise)
    source = JsonlChangeEventSource(str(path))
    loaded, _, truncated, _ = source.load_change_events(now=_NOW, thresholds=th)
    assert len(loaded) == 0
    assert truncated is True


def test_jsonl_source_reports_truncated_when_the_underlying_reader_bails(tmp_path):
    # audit.file_reader.iter_lines_reverse raises AuditFileReadBounded on an
    # internal safety bound (an oversized undelimited line, here) rather than
    # silently stopping — this reader must catch it and report truncated=True.
    th = _thresholds()
    path = tmp_path / "audit.jsonl"
    path.write_bytes(b"x" * 2_000_000)  # one giant line, no newline anywhere
    source = JsonlChangeEventSource(str(path))
    loaded, malformed, truncated, _ = source.load_change_events(now=_NOW, thresholds=th)
    assert loaded == []
    assert truncated is True


def test_jsonl_source_missing_file_is_empty_not_error(tmp_path):
    source = JsonlChangeEventSource(str(tmp_path / "does-not-exist.jsonl"))
    loaded, malformed, truncated, _ = source.load_change_events(now=_NOW, thresholds=_thresholds())
    assert loaded == [] and malformed == 0 and truncated is False


def test_jsonl_source_reads_hash_chained_ledger(tmp_path):
    from querygate.audit.sinks import HashChainedAuditSink

    th = _thresholds()
    path = tmp_path / "ledger.jsonl"
    sink = HashChainedAuditSink(str(path), key=b"k")
    for event in _spread(5, start=_NOW - timedelta(seconds=1800), span_seconds=1800, kind="config"):
        sink.emit(event)
    source = JsonlChangeEventSource(str(path), ledger_key=b"k")
    loaded, malformed, _, _ = source.load_change_events(now=_NOW, thresholds=th)
    assert len(loaded) == 5
    assert malformed == 0


def test_jsonl_source_rejects_a_forged_chain_record(tmp_path):
    # TODO.md item 137: see the identical regression in test_anomaly.py — same
    # reader shape, same self-consistency check.
    from querygate.audit.ledger import LedgerRecord
    from querygate.audit.sinks import HashChainedAuditSink

    th = _thresholds()
    path = tmp_path / "ledger.jsonl"
    sink = HashChainedAuditSink(str(path), key=b"k")
    genuine = _spread(1, start=_NOW - timedelta(seconds=900), span_seconds=1, kind="config")[0]
    sink.emit(genuine)

    genuine_record = LedgerRecord.model_validate_json(path.read_text().splitlines()[0])
    forged_event = _spread(1, start=_NOW - timedelta(seconds=600), span_seconds=1, kind="config")[0]
    forged = LedgerRecord(
        seq=1,
        prev_hash=genuine_record.hash,
        event=forged_event.model_dump(mode="json", exclude_none=True),
        hash="anything",
    )
    with path.open("a") as f:
        f.write(forged.model_dump_json() + "\n")

    source = JsonlChangeEventSource(str(path), ledger_key=b"k")
    loaded, malformed, _, _ = source.load_change_events(now=_NOW, thresholds=th)
    assert len(loaded) == 1
    assert malformed == 1


def test_jsonl_source_rejects_a_bare_envelope_less_line_when_backend_is_chained(tmp_path):
    """TODO.md item 137 regression (found by `security-invariant-reviewer`,
    2026-08-05): see the identical regression in test_anomaly.py — same
    reader shape."""
    from querygate.audit.sinks import HashChainedAuditSink

    th = _thresholds()
    path = tmp_path / "ledger.jsonl"
    sink = HashChainedAuditSink(str(path), key=b"k")
    genuine = _spread(1, start=_NOW - timedelta(seconds=900), span_seconds=1, kind="config")[0]
    sink.emit(genuine)

    bare_event = _spread(1, start=_NOW - timedelta(seconds=600), span_seconds=1, kind="config")[0]
    with path.open("a") as f:
        f.write(bare_event.model_dump_json(exclude_none=True) + "\n")

    source = JsonlChangeEventSource(str(path), ledger_key=b"k", require_envelope=True)
    loaded, malformed, _, _ = source.load_change_events(now=_NOW, thresholds=th)
    assert len(loaded) == 1
    assert malformed == 1


# --- build_change_trend_report ----------------------------------------------


def test_report_with_no_source_is_disabled_not_error():
    report = build_change_trend_report(None, now=_NOW, thresholds=_thresholds())
    assert report.source == "disabled"
    assert report.config_recent.total == 0
    assert report.generated_at == _NOW.isoformat()


def test_report_end_to_end_computes_volume_ratio(tmp_path):
    th = _thresholds()
    path = tmp_path / "audit.jsonl"
    baseline_start = _NOW - timedelta(seconds=7200)
    recent_start = _NOW - timedelta(seconds=3600)
    events = _spread(10, start=baseline_start, span_seconds=3600, kind="config")
    events += _spread(40, start=recent_start, span_seconds=3600, kind="config")
    _write_jsonl(path, events)

    report = build_change_trend_report(JsonlChangeEventSource(str(path)), now=_NOW, thresholds=th)
    assert report.source == "jsonl"
    assert report.events_scanned == 50
    assert report.config_recent.total == 40
    assert report.config_baseline.total == 10
    assert report.config_volume_ratio == pytest.approx(4.0)
    assert report.scan_ended_on_out_of_window_run is False


def test_report_discloses_the_heuristic_early_exit(tmp_path):
    # TODO.md item 171: the reader's 4th return value must actually reach
    # the caller-facing report, not just exist on the reader.
    path = tmp_path / "audit.jsonl"
    out_of_window = _spread(
        5, start=_NOW - timedelta(seconds=50_000), span_seconds=1000, kind="config"
    )
    _write_jsonl(path, out_of_window)
    th = _thresholds(max_consecutive_out_of_window=3)
    report = build_change_trend_report(JsonlChangeEventSource(str(path)), now=_NOW, thresholds=th)
    assert report.events_scanned == 0
    assert report.scan_ended_on_out_of_window_run is True


def test_volume_ratio_is_none_when_baseline_is_empty(tmp_path):
    path = tmp_path / "audit.jsonl"
    events = _spread(5, start=_NOW - timedelta(seconds=1800), span_seconds=1800, kind="config")
    _write_jsonl(path, events)
    report = build_change_trend_report(
        JsonlChangeEventSource(str(path)), now=_NOW, thresholds=_thresholds()
    )
    assert report.config_volume_ratio is None


def test_report_is_redaction_safe():
    """The serialized report must never carry version content, proposal text,
    or raw YAML — only action names, outcomes, and counts. Proven against the
    live schema, not by eye."""
    events = [
        _config_event(at=_NOW - timedelta(seconds=100), action="apply"),
        _catalog_event(at=_NOW - timedelta(seconds=100), action="publish"),
    ]

    class _Source:
        def load_change_events(self, *, now, thresholds):
            return events, 0, False, False

    report = build_change_trend_report(_Source(), now=_NOW, thresholds=_thresholds())
    blob = report.model_dump_json()
    for forbidden in ("version_id", "description", "proposal_id", "entry_id"):
        assert forbidden not in blob

    with pytest.raises(pyd.ValidationError):
        ConfigCatalogChangeTrend(
            generated_at=_NOW.isoformat(),
            recent_window_seconds=1,
            baseline_window_seconds=1,
            leaked_field=1,  # type: ignore[call-arg]
        )


def test_jsonl_source_with_no_in_window_events_is_empty_but_still_jsonl(tmp_path):
    """A configured-but-quiet stream is source="jsonl" with zero events — not
    "disabled", which specifically means no sink is configured at all."""
    path = tmp_path / "audit.jsonl"
    _write_jsonl(
        path,
        _spread(5, start=_NOW - timedelta(seconds=50_000), span_seconds=1000, kind="config"),
    )
    report = build_change_trend_report(
        JsonlChangeEventSource(str(path)), now=_NOW, thresholds=_thresholds()
    )
    assert report.source == "jsonl"
    assert report.events_scanned == 0
    assert report.config_recent.total == 0


# --- thresholds validation ----------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"recent_window_seconds": 0},
        {"baseline_window_seconds": -1},
        {"max_events_scanned": 0},
        {"max_consecutive_out_of_window": 0},
    ],
)
def test_thresholds_reject_out_of_range_values(overrides):
    with pytest.raises(pyd.ValidationError):
        ChangeTrendThresholds(**overrides)


# --- route helpers (config -> thresholds/source wiring) -----------------------


def test_route_helpers_map_config_to_thresholds_and_source(tmp_path):
    from querygate.admin.config_trends import JsonlChangeEventSource as _JsonlSource
    from querygate.api.admin_observability_routes import (
        _change_trend_source,
        _change_trend_thresholds,
    )
    from querygate.core.config import AppConfig

    audit_path = str(tmp_path / "a.jsonl")
    cfg = AppConfig(
        environment="localhost",
        audit_sink_backend="jsonl",
        audit_jsonl_path=audit_path,
        change_trend_recent_window_seconds=1200.0,
        change_trend_baseline_window_seconds=48000.0,
        change_trend_max_events_scanned=33,
        change_trend_max_lines_read=77,
        change_trend_max_consecutive_out_of_window=42,
    )
    th = _change_trend_thresholds(cfg)
    assert th.recent_window_seconds == 1200.0
    assert th.baseline_window_seconds == 48000.0
    assert th.max_events_scanned == 33
    # TODO.md item 138 (security-review follow-up): max_lines_read must be
    # independently operator-configurable, not silently stuck at the
    # pydantic-model class default.
    assert th.max_lines_read == 77
    # TODO.md item 141 (security-review follow-up, docs/THREAT_MODEL.md
    # QG-43): same requirement for the early-exit tolerance.
    assert th.max_consecutive_out_of_window == 42

    source = _change_trend_source(cfg)
    assert isinstance(source, _JsonlSource)
    assert str(source.path) == audit_path

    # The tamper-evident backend shares the same on-disk format (item 91's
    # envelope is transparently unwrapped by the reader) and must be equally
    # readable here (item 136 -- was silently gated out before the fix).
    cfg_chained = AppConfig(
        environment="localhost",
        audit_sink_backend="jsonl_chained",
        audit_jsonl_path=audit_path,
    )
    source_chained = _change_trend_source(cfg_chained)
    assert isinstance(source_chained, _JsonlSource)
    assert str(source_chained.path) == audit_path

    # No persisted sink -> no source (endpoint reports "disabled").
    cfg_none = AppConfig(environment="localhost", audit_sink_backend="none")
    assert _change_trend_source(cfg_none) is None
