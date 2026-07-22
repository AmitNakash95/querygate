#!/usr/bin/env python3
"""UI-visualization smoke test for the audit-stream anomaly panel (TODO.md item 59).

This drives the *real* admin UI assets — `admin_ui/index.html`, `app.js`, and
`app.css`, unmodified except for rewritten `/admin/` asset paths — through a
headless Chromium, and asserts that the actual `renderAnomalies()` code path
draws the expected panel. The anomaly data it renders is produced by the *real*
backend (`build_anomaly_report` over a seeded JSONL audit file), so both the
data path and the render path are genuine; only the network layer is stubbed
(there is no live server, and the admin UI's bearer token is entered through a
modal that can't be pre-seeded, so the fixture self-drives auth).

It writes a screenshot to `dist/anomaly-ui-smoke.png` for human inspection and
fails (non-zero exit) if the rendered DOM is missing the expected principals,
signal badges, or details. When no headless browser is available it prints SKIP
and exits 0, so it is safe to wire into a make target without becoming a hard
browser dependency.

Run: `python scripts/anomaly_ui_smoke.py` (or `make anomaly-ui-smoke`).
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import tempfile
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

ADMIN_UI = REPO / "src" / "querygate" / "admin_ui"
OUT_PNG = REPO / "dist" / "anomaly-ui-smoke.png"

_NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
_THRESHOLDS = AnomalyThresholds(
    recent_window_seconds=3600.0,
    baseline_window_seconds=3600.0,
    min_baseline_events=10,
    min_recent_events=3,
    volume_spike_ratio=3.0,
    rejection_rate_delta=0.3,
)


def _event(at, principal, connection, outcome="success"):
    return AuditEvent(
        occurred_at=at,
        principal_id=principal,
        connection_id=connection,
        policy_decision="allowed" if outcome == "success" else "denied",
        outcome=outcome,
        query_shape={"from": "customers", "select": [{"kind": "column", "column": "ssn"}]},
        duration_ms=2,
    )


def _spread(n, start, span, principal, connection, outcome="success"):
    if n == 0:
        return []
    step = span / n
    return [
        _event(start + timedelta(seconds=step * i + 1), principal, connection, outcome)
        for i in range(n)
    ]


def seed_report(audit_path: Path):
    """Write a JSONL audit stream that yields all three signal kinds across two
    principals, then compute the real report the UI will render."""
    recent = _NOW - timedelta(seconds=3600)
    baseline = _NOW - timedelta(seconds=7200)
    events = []
    # svc-reporting: big volume spike on demo + reaches payroll-prod (new conn).
    events += _spread(15, baseline, 3600, "svc-reporting", "demo")
    events += _spread(80, recent, 3000, "svc-reporting", "demo")
    events += _spread(4, recent + timedelta(seconds=3000), 400, "svc-reporting", "payroll-prod")
    # agent-42: rejection-rate spike (baseline clean, recent mostly denied).
    events += _spread(30, baseline, 3600, "agent-42", "demo", outcome="success")
    events += _spread(4, recent, 1800, "agent-42", "demo", outcome="success")
    events += _spread(
        16, recent + timedelta(seconds=1800), 1600, "agent-42", "demo", outcome="rejected"
    )
    with audit_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(event.model_dump_json(exclude_none=True) + "\n")

    report = build_anomaly_report(
        JsonlAuditEventSource(str(audit_path)), now=_NOW, thresholds=_THRESHOLDS
    )
    return report


def find_chromium():
    candidates = []
    home = Path.home()
    # Playwright-managed browsers (present in this environment's cache).
    candidates += glob.glob(
        str(home / "Library/Caches/ms-playwright/chromium*/chrome-mac*/*.app" "/Contents/MacOS/*")
    )
    candidates += glob.glob(str(home / ".cache/ms-playwright/chromium*/chrome-linux/chrome"))
    # Common system locations.
    candidates += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


_BOOTSTRAP = """
(function () {
  // Under file:// these history calls throw; app.js calls them on navigation.
  try { history.replaceState = function () {}; history.pushState = function () {}; } catch (e) {}
  const ACCESS = __ACCESS__;
  const OVERVIEW = __OVERVIEW__;
  const ANOMALIES = __ANOMALIES__;
  const json = (obj) => Promise.resolve(new Response(JSON.stringify(obj), {
    status: 200, headers: { "Content-Type": "application/json" },
  }));
  window.fetch = function (input) {
    const url = typeof input === "string" ? input : input.url;
    if (url.includes("/help/my-access")) return json(ACCESS);
    if (url.includes("/observability/anomalies")) return json(ANOMALIES);
    if (url.includes("/observability/overview")) return json(OVERVIEW);
    return json({});
  };
  window.addEventListener("load", function () {
    // app.js (defer) has run and shown the auth modal; self-drive the login and
    // then navigate to the Observability domain so renderAnomalies() fires.
    setTimeout(function () {
      const token = document.getElementById("auth-token");
      if (token) token.value = "smoke-token";
      const form = document.getElementById("auth-form");
      if (form) {
        if (form.requestSubmit) form.requestSubmit();
        else form.dispatchEvent(new Event("submit", { cancelable: true }));
      }
      setTimeout(function () {
        const nav = document.querySelector('.nav-item[data-domain="observability"]');
        if (nav) nav.click();
      }, 300);
    }, 100);
  });
})();
"""

_OVERVIEW = {
    "source": "process_snapshot",
    "durable": False,
    "since": _NOW.isoformat(),
    "note": "Current-process snapshot: counters are cumulative since this process started.",
    "queries_total": 4213,
    "queries_success": 4090,
    "queries_rejected": 123,
    "rejections_by_reason": {"policy": 88, "quota": 35},
    "duration": {"count": 4213, "total_seconds": 210.6, "avg_seconds": 0.05},
    "queue_depth_total": 0,
    "queue_wait_by_outcome": {},
    "concurrency_in_use_total": 2,
    "concurrency_max_total": 16,
    "concurrency_utilization": 0.125,
    "quota_rejections_by_kind": {"requests": 35},
    "cost_estimation": {
        "attempts": 4213,
        "unavailable": 12,
        "unavailable_by_reason": {"explain_failed": 12},
        "would_reject": 0,
        "fail_open_rate": 0.0028,
    },
    "by_connection": [],
}


def build_fixture(tmp: Path, report) -> Path:
    for name in ("app.js", "app.css", "favicon.svg", "logo-wordmark.svg"):
        src = ADMIN_UI / name
        if src.exists():
            (tmp / name).write_bytes(src.read_bytes())

    access = {
        "principal": "svc-smoke",
        "scopes": ["admin:observability:read"],
        "visible_connections": [],
    }
    bootstrap = (
        _BOOTSTRAP.replace("__ACCESS__", json.dumps(access))
        .replace("__OVERVIEW__", json.dumps(_OVERVIEW))
        .replace("__ANOMALIES__", report.model_dump_json())
    )
    html = (ADMIN_UI / "index.html").read_text(encoding="utf-8").replace("/admin/", "")
    html = html.replace(
        '<script src="app.js" defer></script>',
        f'<script>{bootstrap}</script>\n  <script src="app.js" defer></script>',
    )
    fixture = tmp / "index.html"
    fixture.write_text(html, encoding="utf-8")
    return fixture


def run_chromium(chrome: str, url: str, *, screenshot: Path | None = None, dump: bool = False):
    args = [
        chrome,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--force-color-profile=srgb",
        "--virtual-time-budget=5000",
        "--run-all-compositor-stages-before-draw",
    ]
    if screenshot:
        # Tall viewport so the anomaly panel (below the overview cards) is
        # captured in the artifact, not just the top of the page.
        args += [f"--screenshot={screenshot}", "--window-size=1160,1820"]
    if dump:
        args += ["--dump-dom"]
    args.append(url)
    result = subprocess.run(args, capture_output=True, text=True, timeout=90)
    return result.stdout


def main() -> int:
    report = None
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        report = seed_report(tmp / "audit.jsonl")

        # Behavioral pre-check: the real backend produced the signals we expect.
        by_principal = {p.principal_id: {s.kind for s in p.signals} for p in report.principals}
        assert "volume_spike" in by_principal.get("svc-reporting", set()), by_principal
        assert "new_connection_access" in by_principal.get("svc-reporting", set()), by_principal
        assert "rejection_rate_spike" in by_principal.get("agent-42", set()), by_principal
        print(f"backend report OK: {len(report.principals)} principals, " f"signals={by_principal}")

        chrome = find_chromium()
        if not chrome:
            print("SKIP: no headless Chromium found (set one up to render the UI smoke).")
            return 0

        fixture = build_fixture(tmp, report)
        url = fixture.as_uri()

        OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
        run_chromium(chrome, url, screenshot=OUT_PNG)
        dom = run_chromium(chrome, url, dump=True)

    # Assert the real render path drew the expected panel content.
    required = [
        "Behavioral anomalies",
        'id="anomaly-table-wrap"',
        "svc-reporting",
        "agent-42",
        "Volume spike",
        "Rejection spike",
        "New connection",
        "baseline rate",
        "payroll-prod",
    ]
    missing = [needle for needle in required if needle not in dom]
    if missing:
        snippet = (
            dom[dom.find("anomaly-body") : dom.find("anomaly-body") + 1200]
            if "anomaly-body" in dom
            else dom[:1200]
        )
        print("FAIL: rendered DOM is missing expected content:", missing, file=sys.stderr)
        print("--- DOM snippet ---\n", snippet, file=sys.stderr)
        return 1

    # The anomaly panel must not be hidden (it is `hidden` until rendered).
    if 'id="anomaly-table-wrap" hidden' in dom.replace("\n", " "):
        print("FAIL: anomaly table remained hidden — renderAnomalies did not run.", file=sys.stderr)
        return 1

    if not OUT_PNG.exists() or OUT_PNG.stat().st_size < 5000:
        print(f"FAIL: screenshot not written or too small: {OUT_PNG}", file=sys.stderr)
        return 1

    print(f"PASS: anomaly panel rendered by the real admin UI; screenshot -> {OUT_PNG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
