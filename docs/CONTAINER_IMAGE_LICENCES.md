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
| (ii) "require distributors and external end users to agree to terms that protect it and Microsoft at least as much as this agreement" | **Partly met, and this is the change item 210 made.** See below. |
| (iii) "indemnify, defend, and hold harmless Microsoft from any claims … related to the distribution or use of your applications" | **Not addressed.** Nothing in QueryGate's licence or terms does this. |

Requirement (ii) is the load-bearing one, and **the licence change improved it
rather than leaving it where it was.** Under the cancelled BSL plan a user who
pulled the image received QueryGate under BSL 1.1 and agreed to nothing on
Microsoft's behalf: BSL 1.1 has no pass-through clause, no third-party-components
section, and no mechanism for binding a downstream user to another vendor's
terms.

Under the proprietary EULA it does. `docs/legal/EULA.en.md` §2 now carries a
**Third-party components** paragraph: the customer agrees to comply with each
third-party component's own terms, "including any terms that protect their
respective vendors", and those terms control over the EULA for that component.
That is the ordinary mechanism by which a commercial product discharges a
pass-through obligation, and it is exactly what option 1 below called for.

**And one assumption, stated rather than left implicit:** EULA §2 binds a party
who has *entered* the EULA, and `LICENSE` says in terms that someone without an
Order "has no licence to use this software." So the pass-through reaches every
lawful user of the image and nobody else. Whether that satisfies §2(b)(ii)'s
"distributors and external end users" depends on the image never being
distributed outside an Order — the current model (`docs/business/GTM_SAAS.md` §6,
signed image only), but a premise, not a clause.

**Two reasons this is "partly", not "met".** First, the EULA is still a draft
with unfilled placeholders and has not been reviewed by counsel, so no clause in
it can be called settled. Second, requirement (iii) — the indemnity running to
Microsoft — is still not addressed anywhere, and that is a commitment nobody can
draft their way into without the owner deciding to make it.

There is also a restriction worth flagging even though it now appears comfortably
satisfied: §2(c)(ii) forbids distributing the code "so that any part of it
becomes subject to any license that requires that the distributable code … be
disclosed or distributed in source code form". Under BSL 1.1 this needed an
argument — the source was published, and the reading turned on the driver being a
separately-installed binary. Proprietary, closed-source distribution removes the
question rather than answering it: no QueryGate licence requires anything to be
disclosed in source form. This is one of the few places where the licence change
made a legal reading *simpler*.

## What should happen, in order of preference

1. ✅ **Add a pass-through clause — done (item 210).** `docs/legal/EULA.en.md`
   §2 "Third-party components" (and its Hebrew mirror) requires the customer to
   comply with third-party component terms, including terms protecting their
   vendors. **Still outstanding from this option:** a `THIRD_PARTY_NOTICES` file
   *inside the image itself*, so a user who pulls the image and never sees the
   repository still receives the notices. `Dockerfile` already `COPY`s `LICENSE`;
   this is the same shape and is not done.
2. **Decide the indemnity question (§2(b)(iii)) with counsel.** It is a
   commitment to Microsoft. The owner is already weighing indemnification
   exposure and insurance for the Enterprise tier
   (`docs/business/GTM_SAAS.md`), so this belongs in that conversation, not in a
   separate one. **This is the requirement that is still not met at all.**
3. **If either proves awkward, make the driver an opt-in layer.** MSSQL support
   is one of three dialects. A base image without `msodbcsql18` and a documented
   operator-installed step removes the obligation entirely, at the cost of a
   worse first-run experience for MSSQL users. `Dockerfile` already isolates the
   driver in a single `RUN` block, so this is a small change if it is wanted.

**Do not treat this as blocking a release.** It is a distribution-terms question
about a third-party component, not a question about QueryGate's own licence, and
option 1's clause now goes to the substance of §2(b)(ii). What remains — the
in-image notices file and the §2(b)(iii) indemnity — needs an answer before the
first paid pilot's security review, which is the north-star metric.

## One interaction with a claim we make elsewhere

`docs/LICENSING_FAQ.md` states that QueryGate transmits exactly four licence
fields and that no path exists for database credentials, query text, results,
rows, schema, catalog, audit records or policy files — and
`tests/security/test_no_phone_home.py` enforces that against QueryGate's own
source, both as a hostname/vocabulary ban outside `src/querygate/subscription/`
and as a positive assertion on that package's payload. §3 of the ODBC driver's
EULA is headed **DATA COLLECTION** and contemplates the driver enabling
collection of data from users of applications that use it.

The scope of our claim narrowed in item 210 — QueryGate now makes a licence call
of its own — but the boundary described here did not move: that call is ours and
is disclosed; the driver's behaviour is Microsoft's and is not ours to
characterise.

Our claim is about **QueryGate**, and it remains true in its narrowed form:
nothing in `src/querygate/` calls a QueryGate-controlled host except the
subscription client of items 211-213, whose four-field payload is disclosed in
EULA §16.1; `tests/security/test_no_phone_home.py` pins that package's declared
payload constant to the same four fields. But a careful reviewer who reads both
documents will ask about the driver, and the honest answer is that the driver is
a third-party component with its own terms, whose telemetry behaviour we have not
audited. → **Done (item 210):** stated in `docs/LICENSING_FAQ.md` under "One
honest boundary", rather than left to be discovered.

## How to reproduce

```bash
docker build -t querygate:licence-audit .
docker run --rm querygate:licence-audit bash -c 'dpkg-query -W | wc -l'
docker run --rm querygate:licence-audit bash -c 'cat /usr/share/doc/msodbcsql18/LICENSE.txt'
docker run --rm querygate:licence-audit bash -c 'ls /app/LICENSE'
```

The third command is the one that used to fail.
