# LICENSE drafting notes — for counsel

> **DRAFT — FOR LAWYER REVIEW. NOT LEGAL ADVICE.**
> This file, and the `LICENSE` it accompanies, are a working draft prepared
> in-house. Nothing here is a legal opinion and nothing here is settled. It
> exists so that the one-off licence review does not start from a blank page.

**What is being reviewed.** `LICENSE` in the repository root: the Business
Source License 1.1 template with QueryGate's four parameters filled in. The
template body from `Terms` to the end of `Covenants of Licensor` is reproduced
**byte-for-byte** from SPDX's canonical `BUSL-1.1` text. Two deviations sit
*outside* that body and are deliberate: the MariaDB licence-text copyright lines
are placed above `Parameters` (adopter convention — CockroachDB and MaxScale
both do this), and the alternative-licensing line reads "please contact" rather
than the template's "please visit:", as HashiCorp's does. The review is of the
*parameters* and of the four blanks, not of the template body.

## The parameters as drafted

| Parameter | Value |
|---|---|
| Licensor | **BLANK** — `<LICENSOR — legal entity, to be supplied>`. The entity does not exist yet. |
| Licensed Work | QueryGate *at the release being licensed* (e.g. `QueryGate 0.1.0`) — see "Licensed Work is per release" below. |
| Additional Use Grant | "Shape A" — unlimited internal production use, no hosted/managed/embedded resale, with an MSP carve-out. Full text in `LICENSE`. |
| Change Date | **BLANK** — `<CHANGE_DATE — stamped per release>`. The policy is four years per release; each release is to stamp its own concrete date. |
| Change License | Apache License, Version 2.0. |

## Questions for counsel, in priority order

### 1. Do not restate the template

BSL 1.1 already carries the non-production core grant and the buy-or-refrain
clause. The Additional Use Grant is *additional* only; the draft deliberately
adds nothing that the template already says. Please flag any clause that
duplicates or contradicts the template body rather than supplementing it.

### 2. The MSP carve-out is deliberate

The second half of the grant — "except that a third party may install, operate,
or manage the Licensed Work solely on behalf of, and for the internal use of, a
single licensee" — is intentional, not a drafting accident. Without it, a
consultancy or managed service provider operating QueryGate for one client's own
internal use would be prohibited. That is a deployment shape the regulated
mid-market uses constantly, and one HashiCorp's grant permits. The question for
counsel is whether the wording achieves that carve-out without accidentally
permitting a multi-tenant hosted offering dressed up as an MSP arrangement.

### 3. "Production" is undefined in BSL 1.1 itself

The template grants non-production use and lets the Licensor grant production
use, but never defines "production". Shape A makes this far less load-bearing
than a scale-capped grant would — there is no threshold to argue about, and the
grant is affirmative rather than conditional — but it is still an undefined term
doing work. `docs/LICENSING_FAQ.md` answers it in plain language for users;
counsel should say whether the licence itself needs to.

### 4. Change License GPL-compatibility

BSL 1.1's Covenants of Licensor, clause 1, require the Change License to be
"the GPL Version 2.0 or any later version, or a license that is compatible with
GPL Version 2.0 or a later version". Apache-2.0 is GPLv3-compatible but not
GPLv2-compatible, which reads as satisfying the clause as written ("or a later
version"). **Confirm rather than assume.**

Precedent, checked directly rather than recalled — and stated with its limits:

- **CockroachDB 22.2** (`cockroachdb/cockroach`, tag `v22.2.0`,
  `licenses/BSL.txt`) shipped BSL 1.1 with `Change License: Apache License,
  Version 2.0`. That is a real, direct precedent for the exact pairing drafted
  here. **However**, CockroachDB has since moved off BSL entirely — the current
  `LICENSE` in that repository is the proprietary "CockroachDB Software
  License", not BSL — so this is a *historical* precedent, not a live one.
- **HashiCorp is not a precedent for this pairing.** Terraform's and Vault's
  current `LICENSE` files specify `Change License: MPL 2.0`, not Apache-2.0.
  They are cited elsewhere in the strategy material for their *grant shape*, not
  for their Change License.

### 5. The "You" definition

The grant closes with: *"You" includes all entities that control, are controlled
by, or are under common control with you.* This is drafted to stop a group
company structure from being used to fragment the grant. Please confirm it does
not collide with any use of "you" in the template body, which uses the word
un-defined throughout.

### 6. The DRAFT banner and Covenant 4

Covenant 4 of the template requires the Licensor "not to modify this License in
any other way". `LICENSE` currently carries a prepended DRAFT banner. Our
reading is that a banner sitting outside the License text modifies none of it,
but the banner **must be deleted before the licence goes into force** regardless
— it says so itself. Flag if the banner needs to go before any distribution, not
merely before the flip.

## Dependency-licence questions that travel with the BSL text

These came out of the dependency-licence pass run on 2026-08-21
(`scripts/check_licenses.py`, gated by `make license-check`; the full inventory
is `docs/THIRD_PARTY_LICENSES.md`, and each non-permissive package has a written
draft review record in `security/copyleft-license-allowlist.json`). **Every one
of those records is still marked `review_status: "draft"` — analysis written
in-house, confirmed by nobody.**

### 7. `certifi` is MPL-2.0 and IS redistributed — the one open dependency question

This is the only non-permissive licence in the redistributed set. Stated
precisely, because the precision is the premise of the question:

- `certifi` is the Mozilla CA root bundle. It reaches QueryGate's `main`
  dependency group transitively, via `httpx` / `httpcore` / `requests`.
- **The published container image contains it.** `Dockerfile` runs
  `poetry install --no-root --only main`.
- The wheel and sdist contain **no** dependencies at all, and QueryGate's own
  wheel metadata does not even declare `certifi` — it is not among the built
  wheel's `Requires-Dist` entries. `pip` resolves it transitively, so a
  `pip install querygate` still fetches it from PyPI.
- MPL-2.0 is file-level (weak) copyleft. The in-house reading is that §3.3
  permits distributing a Larger Work under other terms provided the MPL-covered
  files stay under MPL-2.0 with their source available; QueryGate ships
  `certifi` verbatim as its own installed package, unmodified and not derived
  from, so no QueryGate source file becomes MPL-covered.

**That reading has not been confirmed by counsel. It is the question.** Does
shipping an unmodified MPL-2.0 package inside a BSL-1.1 container image create
any obligation beyond preserving the package's own licence and source
availability?

### 8. Four further copyleft packages, all dev-only, all believed out of scope

Recorded for completeness so counsel can confirm the *routing* — that these
genuinely need no opinion — as well as the substance. None is in the `main`
group, so none is in the container image or in the wheel's declared
requirements:

| Package | Licence | Pulled in by | Note |
|---|---|---|---|
| `chardet` | LGPL-2.0-or-later | `cyclonedx-bom` (the SBOM tool) | Recorded from its declared Trove classifier (`LGPLv2+`); its bundled LICENSE text is in fact 2.1. Same tier either way. |
| `fqdn` | MPL-2.0 | `jsonschema[format-nongpl]`, via `cyclonedx-python-lib` | Plain `jsonschema` *is* in `main` (via `mcp`), but without that extra. |
| `hypothesis` | MPL-2.0 | declared directly, dev group | Used by `tests/unit/test_compiler_properties.py` only. |
| `pathspec` | MPL-2.0 | `black` | Formatter dependency. |

No GPL or AGPL licence appears as a locked package in either group. Both GPL
MySQL drivers (`mysqlclient`, `mysql-connector-python`) appear in `poetry.lock`
only inside SQLAlchemy's unselected `extras`, which Poetry never resolves, so
neither is installed or shipped. `asyncmy`, the MySQL driver QueryGate actually
uses, declares `License-Expression: Apache-2.0`.

### 9. The container image's non-Python layers have never been assessed

The inventory above covers **Python packages only**. The artifact QueryGate
actually distributes is a container image, and `Dockerfile` layers two things
the inventory never sees:

- a full **Debian `bookworm`** userland from `python:3.11-slim-bookworm` (glibc
  under LGPL-2.1, plus the usual GPL-licensed coreutils and shell), and
- **`unixodbc`** plus Microsoft's **`msodbcsql18`**, installed with
  `ACCEPT_EULA=Y` — proprietary terms accepted at build time, in the image
  handed to a customer.

Nothing here is presumed to be a problem — redistributing a Debian base image is
routine, and the GPL components are separate programs rather than anything
linked into QueryGate. The defect is that **it has not been looked at**, while a
customer-facing document (`docs/THIRD_PARTY_LICENSES.md`) now states a licence
position for everything else. This is tracked as **TODO.md item 196** and is
needed before the first paid pilot's security review, not necessarily before the
licence flip — but counsel should say whether they agree with that sequencing.

## What is out of scope for this review

- A commercial licence agreement template for the paid tier. That is a separate
  deliverable and is being commissioned alongside this one, not inside it.
- The BSL template body itself, which is reproduced unmodified.

## Licensed Work is per release, not "all versions" (resolved 2026-08-21)

An earlier planning document specified `Licensed Work: QueryGate, all versions`
alongside a Change Date of "4 years per release". Those are mutually exclusive — a
single Change Date cannot govern releases stamped four years apart — so the parameter
now names the release (`QueryGate <version>`) and each released artifact carries its own
`LICENSE`. This matches how BSL adopters that stamp per release do it.

**For counsel:** confirm this is the intended construction, and in particular what
governs a version a customer is still running after its Change Date has passed but
before they upgrade. The intended reading is that each release converts on its own date
and conversion is irreversible for that release, but the licence text does not say so
explicitly and it is the question a licensee's lawyer will ask first.
