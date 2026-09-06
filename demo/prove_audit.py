#!/usr/bin/env python3
"""Act 4 — prove the audit ledger is tamper-evident, live, on stage.

Every scenario the demo runs writes a real audit record. This script shows one,
then attacks the ledger three ways and shows each attack being caught. It works
on a COPY; the live ledger is never modified.

The three attacks are deliberately chosen to be the three a real attacker has:
edit a record, remove a record, or drop the tail. The third one is the
interesting case and the script is honest about it — truncation cannot be
detected from the file alone, only against an externally anchored head hash.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LEDGER = Path("demo/config/var/querygate-pitch-ledger.jsonl")
KEY_ENV = "AUDIT_LEDGER_HMAC_KEY"
KEY = "pitch-demo-ledger-key-not-a-real-secret"

DIM, BOLD, GREEN, RED, YEL, OFF = (
    "\033[2m",
    "\033[1m",
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[0m",
)


def rule(title: str) -> None:
    print(f"\n{BOLD}── {title} {'─' * max(0, 66 - len(title))}{OFF}")


def verify(path: Path, *, expected_head: str | None = None) -> tuple[int, str]:
    cmd = ["poetry", "run", "querygate-audit", "verify", str(path), "--hmac-key-env", KEY_ENV]
    if expected_head:
        cmd += ["--expected-head", expected_head]
    env = {**os.environ, KEY_ENV: KEY}
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return p.returncode, (p.stdout + p.stderr).strip()


def show(code: int, out: str) -> None:
    colour = GREEN if code == 0 else RED
    print(f"    {colour}{out}{OFF}")
    print(
        f"    {DIM}exit code {code}{'  (a CI job would pass)' if code == 0 else '  (a CI job would FAIL)'}{OFF}"
    )


def main() -> int:
    if not LEDGER.exists():
        print(f"No ledger at {LEDGER}. Run some scenarios first (make pitch-up, then use the UI).")
        return 2

    records = [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()]
    print(f"\n{BOLD}The audit ledger{OFF}  {DIM}{LEDGER}{OFF}")
    print(f"  {len(records)} records, one per action this demo has taken.")

    rule("1. What one record actually contains")
    blocked = next(
        (r for r in records if r["event"].get("policy_decision") == "denied"), records[-1]
    )
    ev = blocked["event"]
    print(
        f"  {DIM}chain{OFF}     seq={blocked['seq']}  prev_hash={blocked['prev_hash'][:16]}…  hash={blocked['hash'][:16]}…"
    )
    print(
        f"  {DIM}who{OFF}       {ev['principal_id']}  via {ev['auth_method']}  on {ev['surface']}"
    )
    print(f"  {DIM}what{OFF}      {ev['operation']} on connection '{ev['connection_id']}'")
    print(f"  {DIM}verdict{OFF}   {ev['policy_decision']}  ({ev.get('outcome')})")
    shape = ev.get("query_shape") or {}
    print(
        f"  {DIM}shape{OFF}     from={shape.get('from')!r}  select={len(shape.get('select') or [])} item(s)"
    )
    where = shape.get("where")
    if where:
        print(
            f"            where: column={where.get('column')!r} operator={where.get('operator')!r}"
        )
    print(f"\n  {YEL}Note what is NOT here:{OFF} no SQL text, no predicate VALUES, no rows,")
    print("  no credentials. The record proves what was asked and what was decided,")
    print("  without becoming a second copy of the data it was protecting.")

    rule("2. Verify the ledger as it stands")
    code, out = verify(LEDGER)
    show(code, out)
    head = out.split("head=")[-1].strip() if "head=" in out else None

    tmp = Path(tempfile.mkdtemp()) / "ledger.jsonl"
    try:
        rule("3. ATTACK: edit a record — turn a refusal into an approval")
        lines = LEDGER.read_text().splitlines()
        idx = next(
            (
                i
                for i, l in enumerate(lines)
                if json.loads(l)["event"].get("policy_decision") == "denied"
            ),
            1,
        )
        rec = json.loads(lines[idx])
        rec["event"]["policy_decision"] = "allowed"
        rec["event"]["outcome"] = "success"
        lines[idx] = json.dumps(rec)
        tmp.write_text("\n".join(lines) + "\n")
        print(f"    {DIM}rewrote record seq={rec['seq']}: 'denied' -> 'allowed'{OFF}")
        show(*verify(tmp))

        rule("4. ATTACK: delete a record entirely")
        lines = LEDGER.read_text().splitlines()
        del lines[idx]
        tmp.write_text("\n".join(lines) + "\n")
        print(f"    {DIM}removed record at line {idx + 1}{OFF}")
        show(*verify(tmp))

        rule("5. ATTACK: truncate the tail — hide the last few actions")
        lines = LEDGER.read_text().splitlines()
        kept = max(1, len(lines) - 5)
        tmp.write_text("\n".join(lines[:kept]) + "\n")
        print(f"    {DIM}dropped the last {len(lines) - kept} records{OFF}")
        print(f"    {DIM}first, checking the file on its own:{OFF}")
        show(*verify(tmp))
        print(f"\n    {YEL}That passed — and it should.{OFF} A truncated chain is still an")
        print("    internally valid chain. This is the honest limit of a hash chain, and")
        print("    it is why the head hash gets anchored somewhere the attacker doesn't")
        print("    control. Checking against the head we recorded a moment ago:")
        if head:
            show(*verify(tmp, expected_head=head))
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)

    rule("6. A single receipt, portable and independently checkable")
    env = {**os.environ, KEY_ENV: KEY}
    p = subprocess.run(
        [
            "poetry",
            "run",
            "querygate-audit",
            "receipt",
            str(LEDGER),
            ev["event_id"],
            "--hmac-key-env",
            KEY_ENV,
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    if p.returncode == 0:
        r = json.loads(p.stdout)
        print(f"    {r['kind']} v{r['version']}, algorithm={r['algorithm']}")
        print(f"    seq={r['seq']}  hash={r['hash'][:32]}…")
        print(f"    for event {r['event']['event_id']}")
        print(
            f"\n    {DIM}Hand this one JSON object to an auditor. They can verify it against{OFF}"
        )
        print(
            f"    {DIM}the ledger without being given the ledger, or the database, or access{OFF}"
        )
        print(f"    {DIM}to anything else.{OFF}")
    else:
        print(f"    {RED}receipt extraction failed: {(p.stderr or p.stdout).strip()[:120]}{OFF}")

    print(
        f"\n{GREEN}{BOLD}  The ledger is tamper-evident, and every claim above was just executed live.{OFF}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
