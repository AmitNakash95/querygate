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
| (ii) "require distributors and external end users to agree to terms that protect it and Microsoft at least as much as this agreement" | **Not met.** The Apache-2.0 transition removed the pass-through that partly addressed it. See below. |
| (iii) "indemnify, defend, and hold harmless Microsoft from any claims … related to the distribution or use of your applications" | **Not addressed.** Nothing in QueryGate's licence or terms does this. |

Requirement (ii) is the load-bearing one, and **the move to Apache-2.0 made it
materially harder, not easier. This section is the honest record of that, and it
is the one part of this repository that most needs a lawyer's eye before the
image is published.**

Under the cancelled proprietary plan, `docs/legal/EULA.en.md` §2 carried a
Third-party components pass-through, and — critically — the image only ever
reached a party who had *entered* an Order. `LICENSE` said in terms that someone
without an Order had no licence to use the software, so the pass-through reached
every lawful user of the image and nobody else.

**Open source removes both halves of that.** Apache-2.0 is a licence over
QueryGate's own code; it has no pass-through clause, no third-party-components
section, and no mechanism for binding a downstream recipient to another vendor's
terms — the same gap BSL 1.1 had. And there is no longer an Order gating who may
pull the image: once it is public, anyone can, including people who never agreed
to anything.

⚠️ **The specific open question.** `msodbcsql18` is Microsoft's proprietary ODBC
driver, installed under `ACCEPT_EULA=Y` at build time and shipped inside the
image. Its licence text is present in the image (the `dpkg path-include` that
`scripts/check_release_artifacts.py` asserts), which satisfies the
"licence text accompanies the binary" half. What is **not** established is
whether publicly redistributing that driver inside an Apache-2.0 image is
permitted at all, and whether the redistribution and indemnity requirements can
be discharged without an Order in place. That question did not need answering
while distribution was gated; it does now.

**Do not treat this as resolved by the licence change.** Three options, none of
them free:

1. Get the redistribution position reviewed by counsel before publishing an
   image that contains the driver.
2. Ship the driver in a separate, clearly-labelled image variant and make the
   default image MSSQL-free, so the default artifact carries no third-party
   proprietary binary at all.
3. Do not ship the driver; document the `apt-get install msodbcsql18` step as an
   operator action, so the person who accepts `ACCEPT_EULA=Y` is the person who
   installs it.

Option 2 or 3 removes the question rather than answering it, and either is
cheaper than a legal opinion. Requirement (iii) — the indemnity running to
Microsoft — remains unaddressed under any of them, and is an owner decision, not
a drafting one.

One restriction *is* now comfortably satisfied: §2(c)(ii) forbids distributing
the code so that any part of it becomes subject to a licence requiring source
disclosure. Apache-2.0 is permissive and imposes no such requirement on anything
it does not itself cover, so the driver is unaffected.

## What should happen, in order of preference

1. ❌ **The pass-through clause is gone.** It lived in the EULA, which the
   open-source transition deleted along with the proprietary model, and
   Apache-2.0 has no equivalent. Nothing currently binds a downstream recipient
   of the image to Microsoft's terms. **Still outstanding, and now more
   important:** a `THIRD_PARTY_NOTICES` file *inside the image itself*, so
   someone who pulls it and never sees this repository still receives the
   notices. `Dockerfile` already `COPY`s `LICENSE`; this is the same shape and is
   not done. That file is necessary but almost certainly not sufficient — see
   the open question above.
2. **Decide the indemnity question (§2(b)(iii)) with counsel.** It is a
   commitment to Microsoft. The owner is already weighing indemnification
   exposure and insurance for the Enterprise tier
   so this belongs in an owner conversation, not in a
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

Earlier licensing documentation stated that QueryGate transmits exactly four licence
fields and that no path exists for database credentials, query text, results,
rows, schema, catalog, audit records or policy files — and
`tests/security/test_no_phone_home.py` enforces that against QueryGate's own
source, both as a hostname/vocabulary ban outside `src/querygate/subscription/`
and as a positive assertion on that package's payload. §3 of the ODBC driver's
EULA is headed **DATA COLLECTION** and contemplates the driver enabling
collection of data from users of applications that use it.

The scope of our claim narrowed once, then widened back — QueryGate briefly made a licence call
of its own — but the boundary described here did not move: that call is ours and
is disclosed; the driver's behaviour is Microsoft's and is not ours to
characterise.

Our claim is about **QueryGate**, and it is now absolute: nothing in
`src/querygate/` calls a QueryGate-controlled host at all, which
`tests/security/test_no_phone_home.py` asserts positively. But a careful reviewer
will ask about the driver, and the honest answer is that it is a third-party
component with its own terms, whose telemetry behaviour we have not audited. Say
that rather than let it be discovered — and note that it is an argument for
options 2 and 3 above, since a QueryGate image that ships no Microsoft driver
carries no unaudited third-party telemetry surface either.

## How to reproduce

```bash
docker build -t querygate:licence-audit .
docker run --rm querygate:licence-audit bash -c 'dpkg-query -W | wc -l'
docker run --rm querygate:licence-audit bash -c 'cat /usr/share/doc/msodbcsql18/LICENSE.txt'
docker run --rm querygate:licence-audit bash -c 'ls /app/LICENSE'
```

The third command is the one that used to fail.
