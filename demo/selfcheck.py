#!/usr/bin/env python3
"""Pre-flight check for the partner demo — run it right before the meeting.

Asserts the demo's actual thesis against the live stack, rather than merely
checking that services respond:

  * the gate-OFF path really does leak SSNs (if it doesn't, Act 1 has no point)
  * the gate-ON path really does refuse, citing a rule
  * a refusal really does leave the database untouched (pg_stat_statements
    delta of exactly 0) -- the headline claim
  * a legitimate query really does still succeed through the gate
  * no agent backend is left running
  * the tamper-evident audit ledger still verifies (Act 4 depends on it)

These are the failure modes that a green service-health check cannot see and
that are expensive to discover on a projector. Exit code 0 means present it.
"""

from __future__ import annotations

import sys

import os
import subprocess
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8900"
LEDGER = Path("demo/config/var/querygate-pitch-ledger.jsonl")
LEDGER_KEY_ENV = "AUDIT_LEDGER_HMAC_KEY"
LEDGER_KEY = "pitch-demo-ledger-key-not-a-real-secret"
OK, BAD = "  \033[32mPASS\033[0m", "  \033[31mFAIL\033[0m"


def main() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        print(f"{OK if condition else BAD}  {label}{('  — ' + detail) if detail else ''}")
        if not condition:
            failures.append(label)

    try:
        with httpx.Client(base_url=BASE, timeout=180) as c:
            health = c.get("/api/health").json()
            check("all three services up", all(health.values()), str(health))

            # Act 1 must actually leak, or there is nothing to contrast against.
            c.post("/api/gate", json={"gate": "off"})
            off = c.post("/api/run", json={"scenario_id": "pii_table", "gate": "off"}).json()
            leaked = (off.get("row_count") or 0) > 0 and off.get("outcome") == "allowed"
            check("gate OFF leaks employee rows", leaked, f"{off.get('row_count')} rows")

            # The headline claim, measured rather than asserted.
            c.post("/api/gate", json={"gate": "on"})
            on = c.post("/api/run", json={"scenario_id": "pii_table", "gate": "on"}).json()
            db = on.get("db") or {}
            check(
                "gate ON blocks the HR table",
                on.get("outcome") == "blocked",
                str(on.get("blocked_by")),
            )
            check("the refusal names a rule", bool(on.get("blocked_by")))
            check(
                "database never contacted (delta 0)",
                db.get("touched") is False and db.get("calls_before") == db.get("calls_after"),
                f"{db.get('calls_before')} -> {db.get('calls_after')}",
            )

            # A gate that blocks everything would also pass the checks above.
            legit = c.post("/api/run", json={"scenario_id": "legit", "gate": "on"}).json()
            check(
                "legitimate query still succeeds",
                legit.get("outcome") == "allowed" and (legit.get("row_count") or 0) > 0,
                f"{legit.get('row_count')} rows, overhead {legit.get('overhead_ms')} ms",
            )

            # Act 4 is worthless if the ledger does not verify. Check it here
            # rather than discovering it in front of a security reviewer.
            if LEDGER.exists():
                proc = subprocess.run(
                    [
                        "poetry",
                        "run",
                        "querygate-audit",
                        "verify",
                        str(LEDGER),
                        "--hmac-key-env",
                        LEDGER_KEY_ENV,
                    ],
                    capture_output=True,
                    text=True,
                    env={**os.environ, LEDGER_KEY_ENV: LEDGER_KEY},
                )
                summary = (proc.stdout + proc.stderr).strip().splitlines()
                check(
                    "audit ledger chain verifies",
                    proc.returncode == 0,
                    summary[-1][:90] if summary else "no output",
                )
            else:
                check("audit ledger chain verifies", False, f"no ledger at {LEDGER}")

            state = c.get("/api/state").json()
            stray = (state.get("db_calls") or {}).get("agent_ro_active", 0)
            check("no agent backend left running", not stray, f"{stray} active")
    except Exception as exc:  # noqa: BLE001 - a pre-flight check reports, never raises
        print(f"{BAD}  could not reach the demo stack — {type(exc).__name__}: {exc}")
        print("\n  Run 'make pitch-up' first, then 'make pitch-status'.")
        return 2

    print()
    if failures:
        print(f"  \033[31m{len(failures)} check(s) failed — do NOT present until resolved.\033[0m")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("  \033[32mAll checks passed. The demo is telling the truth. Go present.\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
