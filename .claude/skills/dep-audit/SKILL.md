---
name: dep-audit
description: >-
  Audit locked dependencies for known vulnerabilities and lockfile drift, and
  govern the deny-by-default allowlist. Use for a periodic supply-chain check,
  before a release, when a CVE advisory lands, or when asked to "audit
  dependencies", "check for CVEs", or "update the SBOM". Mirrors the CI security
  gate (TODO.md item 30).
---

# dep-audit — supply-chain vulnerability gate

QueryGate is a security product; a known CVE in a shipped dependency undermines
the whole pitch. This is the same gate CI runs.

## Run it

```bash
poetry run python scripts/generate_sbom.py
```

This generates the SBOM and audits `poetry.lock`'s `main` group (the exact set
that ships, not a fresh unpinned resolve) with `pip-audit`. It is
**deny-by-default**: any known vulnerability without a reviewed entry in
`security/dependency-audit-allowlist.json` fails the gate. Needs network access
to reach the vulnerability database.

## When a vulnerability is reported

Do **not** reflexively allowlist it. In priority order:

1. **Upgrade** the dependency to a fixed version if one exists and it's
   compatible — this is the real fix. Update `poetry.lock`, re-run the gate.
2. If no fix is available yet, **assess reachability**: does QueryGate actually
   exercise the vulnerable code path? (See existing allowlist entries — e.g.
   `click.edit()` is never called; `idna` only parses operator-configured hosts,
   never caller input.)
3. Only if genuinely unreachable *and* unfixable now, add an allowlist entry with:
   `id`, `package`, a concrete `reason` explaining why it's not exploitable in
   QueryGate's usage (name the compensating control if any), `added` (today's
   date), and `tracking` (the TODO item for remediation). A reason of
   "transitive dep" alone is not sufficient — say why the path is unreachable.
4. Open/append a TODO item under item 30 phase 2 (dependency remediation) so the
   allowlist entry has a removal path, not a permanent parking spot.

## Also check

- **Lockfile drift**: `poetry lock --check` (or `poetry check`) — `pyproject.toml`
  and `poetry.lock` must agree.
- **Stale allowlist**: for each existing entry, verify the CVE isn't now fixable
  (upgrade available) — allowlist entries are temporary by design.

## Report

Vulnerabilities found (id / package / severity / fixable?), what you did
(upgraded / assessed unreachable / allowlisted with justification), any new
allowlist entries, and lockfile-drift status. Flag any allowlist entry that
should now be removed because a fix shipped.
