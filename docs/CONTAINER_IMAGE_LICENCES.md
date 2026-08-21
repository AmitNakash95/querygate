# Container image — non-Python licence assessment

*TODO.md item 196. Measured 2026-08-21 against a locally built production image.*

`docs/THIRD_PARTY_LICENSES.md` covers every Python package in `poetry.lock` and
says so. It does not cover the rest of the image, and the artifact QueryGate
actually distributes is the image. This document is that assessment.

**Scope caveat, stated first.** The image was built and inspected on
**`aarch64`** (Apple Silicon). The image published by `release.yml` is built on
`ubuntu-latest`, i.e. **`amd64`**. The package *set* is the same Debian
`bookworm` selection and the licences below do not vary by architecture, but the
exact package list should be re-taken on an amd64 build before this is quoted to
a counterparty. Nothing here rests on the architecture.

## What the image contains

| Layer | Contents | Assessed |
|---|---|---|
| Python packages (`main` group) | 56 that install on Linux | [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) |
| Debian `bookworm` userland | **124 OS packages** | This document |
| Microsoft ODBC Driver 18 | `msodbcsql18` 18.6.2.1-1 | This document — **the open question** |
| QueryGate itself | `/app/.venv`, `/app/LICENSE` | `LICENSE` |

## The Debian userland — routine, and not a problem

124 packages. The licence families present, counted from each package's
`/usr/share/doc/*/copyright`: **GPL** (168 declarations), **BSD** (95),
**LGPL** (86), public-domain (28), MIT/Expat/X11 (46 combined), ISC (12),
MPL (10), Apache (9), and a long tail of permissive one-offs.

**GPL in this layer is not a QueryGate licensing problem, and the reason is
worth writing down rather than assuming.** `bash`, `coreutils`, `dpkg`, `grep`,
`gzip` and the rest are *separate programs* that QueryGate neither links against
nor derives from. Shipping them in a container image is aggregation on a storage
medium, not the creation of a combined work — the same position every Debian-based
image on Docker Hub relies on. The GPL's copyleft reaches works derived from the
covered program; QueryGate is not one.

The obligation this *does* create is the source-offer: distributing GPL binaries
obliges you to offer corresponding source. In practice this is discharged the way
every Debian-derived image discharges it — the packages are unmodified and
available from Debian's own archives. **This is the one part of this section a
lawyer should confirm rather than take from me**, because "unmodified upstream,
available from the distro" is a customary answer, not a contractual one.

Three of the 124 packages ship no copyright file in the image (`dirmngr`,
`gpg-agent`, and — see below — `msodbcsql18`), because the slim base image's
dpkg configuration path-excludes `/usr/share/doc/*`.

## `msodbcsql18` — permitted, but conditionally, and one condition is not met

This is the finding.

**It was shipping without its licence.** The package declares
`/usr/share/doc/msodbcsql18/LICENSE.txt`, but the slim base image's
`path-exclude` meant the file never landed in the image: QueryGate was
distributing Microsoft's proprietary driver with no licence text alongside it.
Fixed — `Dockerfile` now writes a `path-include` for that package *before*
installing it, and the EULA is present at
`/usr/share/doc/msodbcsql18/LICENSE.txt`.

**Redistribution is expressly permitted.** §2 of the EULA is headed
"DISTRIBUTABLE CODE" and grants the right to distribute the object code in
applications you develop, and to let your own distributors do the same. So the
worst case — that the driver could not ship in a commercial image at all — does
not apply.

**But §2(b) attaches three requirements, and QueryGate currently satisfies one
and a half of them:**

| Requirement | Status |
|---|---|
| (i) "add significant primary functionality to it in your applications" | **Met.** QueryGate is not a repackaged driver. |
| (ii) "require distributors and external end users to agree to terms that protect it and Microsoft at least as much as this agreement" | **NOT met.** See below. |
| (iii) "indemnify, defend, and hold harmless Microsoft from any claims … related to the distribution or use of your applications" | **Not addressed.** Nothing in QueryGate's licence or terms does this. |

Requirement (ii) is the load-bearing one. A user who pulls the image receives
QueryGate under BSL 1.1 and agrees to nothing on Microsoft's behalf. BSL 1.1 has
no pass-through clause, no third-party-components section, and no mechanism for
binding a downstream user to another vendor's terms. As it stands, the image
distributes Microsoft's driver without imposing the terms Microsoft requires be
imposed.

There is also a restriction worth flagging even though it appears satisfied:
§2(c)(ii) forbids distributing the code "so that any part of it becomes subject
to any license that requires that the distributable code … be disclosed or
distributed in source code form". BSL 1.1 makes *QueryGate's* source available;
it does not reach a separately-installed binary driver. That reading is almost
certainly right and is also exactly the kind of sentence a licensing reviewer
will stop on.

## What should happen, in order of preference

1. **Add a third-party-notices file and a pass-through clause.** A
   `THIRD_PARTY_NOTICES` in the image plus a short clause in QueryGate's terms
   requiring end users to accept third-party component terms would address
   §2(b)(ii) directly. This is the ordinary way commercial products discharge
   this obligation and it changes nothing about the product.
2. **Decide the indemnity question (§2(b)(iii)) with counsel.** It is a
   commitment to Microsoft, and per `GTM_EXECUTION_PLAN.md` §3 Layer 3 the
   owner is already weighing indemnification exposure and insurance. This
   belongs in that conversation, not in a separate one.
3. **If either proves awkward, make the driver an opt-in layer.** MSSQL support
   is one of three dialects. A base image without `msodbcsql18` and a documented
   operator-installed step removes the obligation entirely, at the cost of a
   worse first-run experience for MSSQL users. `Dockerfile` already isolates the
   driver in a single `RUN` block, so this is a small change if it is wanted.

**Do not treat this as blocking the flip.** It is a distribution-terms question
about a third-party component, not a question about QueryGate's own licence, and
option 1 resolves the substance of it. It does need an answer before the first
paid pilot's security review, which is the north-star metric.

## One interaction with a claim we make elsewhere

`docs/LICENSING_FAQ.md` states that QueryGate "contains no telemetry of any kind
… makes no outbound calls to us, ever", and
`tests/security/test_no_phone_home.py` enforces that against QueryGate's own
source. §3 of the ODBC driver's EULA is headed **DATA COLLECTION** and
contemplates the driver enabling collection of data from users of applications
that use it.

Our claim is about **QueryGate**, and it remains true: nothing in
`src/querygate/` calls a QueryGate-controlled host. But a careful reviewer who
reads both documents will ask about the driver, and the honest answer is that
the driver is a third-party component with its own terms, whose telemetry
behaviour we have not audited. Worth a sentence in the FAQ rather than being
discovered.

## How to reproduce

```bash
docker build -t querygate:licence-audit .
docker run --rm querygate:licence-audit bash -c 'dpkg-query -W | wc -l'
docker run --rm querygate:licence-audit bash -c 'cat /usr/share/doc/msodbcsql18/LICENSE.txt'
docker run --rm querygate:licence-audit bash -c 'ls /app/LICENSE'
```

The third command is the one that used to fail.
