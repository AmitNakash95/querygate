#!/usr/bin/env python3
"""Seed the live audit stream with usage that trips the anomaly panel (item 59).

Sanity-test the *live* admin UI on demand: this appends real, now-relative
`query.execution` audit events to your configured `AUDIT_JSONL_PATH` so that,
next time the running app serves `GET /api/v1/admin/observability/anomalies`
(the "Behavioral anomalies" panel in the Observability view), it surfaces one
demo principal per signal kind — a volume spike, a rejection-rate spike, and a
new-connection access.

It reads the same `AppConfig` the app does (so it honors your `.env`: the audit
path and the `ANOMALY_*` thresholds), sizes the demo traffic to comfortably
clear those thresholds, and then re-runs the real detector to confirm the
signals it wrote will actually fire — printing the result — before you even
open the UI. The demo principals are prefixed (default `demo-`) so they never
collide with real callers, and are additive: your real audit history is left
untouched unless you pass `--reset`.

Usage:
    python scripts/seed_anomaly_demo.py            # append demo spike traffic
    python scripts/seed_anomaly_demo.py --reset    # truncate the file first
    make seed-anomaly-demo

Then reload /admin -> Observability (the endpoint re-reads the file every
request). This writes redaction-safe events only — no SQL, values, or rows —
exactly like the real audit sink.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from querygate.admin.anomaly import (  # noqa: E402
    AnomalyThresholds,
    JsonlAuditEventSource,
    build_anomaly_report,
)
from querygate.audit.events import AuditEvent  # noqa: E402
from querygate.core.config import AppConfig, AuditSinkBackend  # noqa: E402


def _event(at, principal, connection, outcome):
    return AuditEvent(
        occurred_at=at,
        principal_id=principal,
        connection_id=connection,
        policy_decision="allowed" if outcome == "success" else "denied",
        outcome=outcome,
        # Redaction-safe shape, same as the real sink — no literals ever.
        query_shape={"from": "orders", "select": [{"kind": "column", "column": "id"}]},
        duration_ms=3,
    )


def _baseline_times(now, th, count):
    # Evenly spread across the baseline window, which sits immediately *before*
    # the recent window: (now - recent - baseline, now - recent].
    start = now - timedelta(seconds=th.recent_window_seconds + th.baseline_window_seconds * 0.9)
    end = now - timedelta(seconds=th.recent_window_seconds * 1.1)
    span = (end - start).total_seconds()
    step = span / max(count, 1)
    return [start + timedelta(seconds=step * i + 1) for i in range(count)]


def _recent_times(now, th, count):
    # Spread across the recent window (now - recent, now], kept off the exact edges.
    start = now - timedelta(seconds=th.recent_window_seconds * 0.85)
    end = now - timedelta(seconds=5)
    span = (end - start).total_seconds()
    step = span / max(count, 1)
    return [start + timedelta(seconds=step * i + 1) for i in range(count)]


def build_demo_events(now, th, prefix):
    """Three demo principals, each isolating one signal, sized to clear `th`."""
    baseline_n = max(th.min_baseline_events + 10, 20)

    # Volume spike: recent per-second rate must be >= ratio * baseline rate.
    # ratio = (recent/recent_win) / (baseline/baseline_win)  →  solve for recent.
    required = (
        th.volume_spike_ratio * baseline_n * th.recent_window_seconds / th.baseline_window_seconds
    )
    volume_recent = max(th.min_recent_events + 10, int(math.ceil(required * 2)) + 5)

    # Rejection spike: baseline clean, recent ~90% denied → delta ~0.9 >> threshold.
    rej_recent = max(th.min_recent_events + 10, 12)
    rej_denied = int(round(rej_recent * 0.9))

    # New connection: modest recent volume, a few on a never-before-seen conn.
    nc_recent_demo = max(th.min_recent_events + 2, 6)
    nc_recent_new = 3

    events = []

    p_vol = f"{prefix}volume-spike"
    for at in _baseline_times(now, th, baseline_n):
        events.append(_event(at, p_vol, "demo", "success"))
    for at in _recent_times(now, th, volume_recent):
        events.append(_event(at, p_vol, "demo", "success"))

    p_rej = f"{prefix}rejection-spike"
    for at in _baseline_times(now, th, baseline_n):
        events.append(_event(at, p_rej, "demo", "success"))
    recent_rej_times = _recent_times(now, th, rej_recent)
    for i, at in enumerate(recent_rej_times):
        events.append(_event(at, p_rej, "demo", "rejected" if i < rej_denied else "success"))

    p_nc = f"{prefix}new-connection"
    for at in _baseline_times(now, th, baseline_n):
        events.append(_event(at, p_nc, "demo", "success"))
    for at in _recent_times(now, th, nc_recent_demo):
        events.append(_event(at, p_nc, "demo", "success"))
    for at in _recent_times(now, th, nc_recent_new)[-nc_recent_new:]:
        events.append(_event(at, p_nc, "payroll-prod", "success"))

    return events, {
        p_vol: "volume_spike",
        p_rej: "rejection_rate_spike",
        p_nc: "new_connection_access",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed demo anomaly traffic into the audit stream.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="truncate the audit file before writing (default: append)",
    )
    parser.add_argument(
        "--prefix",
        default="demo-",
        help="principal id prefix for the demo callers (default: demo-)",
    )
    parser.add_argument(
        "--path",
        default=None,
        help="override the audit JSONL path (default: AUDIT_JSONL_PATH from .env)",
    )
    args = parser.parse_args()

    cfg = AppConfig()
    path = Path(args.path or cfg.audit_jsonl_path)
    th = AnomalyThresholds(
        recent_window_seconds=cfg.anomaly_recent_window_seconds,
        baseline_window_seconds=cfg.anomaly_baseline_window_seconds,
        min_baseline_events=cfg.anomaly_min_baseline_events,
        min_recent_events=cfg.anomaly_min_recent_events,
        volume_spike_ratio=cfg.anomaly_volume_spike_ratio,
        rejection_rate_delta=cfg.anomaly_rejection_rate_delta,
        max_events_scanned=cfg.anomaly_max_events_scanned,
        max_principals_reported=cfg.anomaly_max_principals_reported,
    )

    if cfg.audit_sink_backend != AuditSinkBackend.JSONL:
        print("WARNING: AUDIT_SINK_BACKEND is not 'jsonl' — the live app will report the anomaly")
        print("         panel as 'disabled' until you set AUDIT_SINK_BACKEND=jsonl. Writing the")
        print(f"         file anyway so it's ready: {path}\n")

    now = datetime.now(timezone.utc)
    events, expected = build_demo_events(now, th, args.prefix)

    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if args.reset else "a"
    with path.open(mode, encoding="utf-8") as handle:
        for event in events:
            handle.write(event.model_dump_json(exclude_none=True) + "\n")
    print(f"{'Reset and wrote' if args.reset else 'Appended'} {len(events)} demo events -> {path}")

    # Confirm the detector actually flags them, so you know before opening the UI.
    report = build_anomaly_report(JsonlAuditEventSource(str(path)), now=now, thresholds=th)
    found = {p.principal_id: {s.kind for s in p.signals} for p in report.principals}
    print(f"\nDetector re-run over the file: {len(report.principals)} principal(s) flagged.")
    ok = True
    for principal, kind in expected.items():
        got = found.get(principal, set())
        mark = "OK " if kind in got else "!! "
        if kind not in got:
            ok = False
        print(f"  {mark}{principal}: expected {kind}, got {sorted(got) or 'nothing'}")

    print("\nNow reload /admin -> Observability (connect with an admin:observability:read key).")
    if not ok:
        print("\nNOTE: some demo signals did not fire under your current ANOMALY_* thresholds —")
        print("      lower ANOMALY_MIN_BASELINE_EVENTS / ANOMALY_MIN_RECENT_EVENTS in .env, or")
        print("      re-run; the sizing is derived from your configured thresholds.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
