# QueryGate licensing FAQ

> **DRAFT — FOR LAWYER REVIEW. NOT LEGAL ADVICE.**
> This FAQ explains QueryGate's licensing in plain language. The licence of
> record is [`legal/EULA.en.md`](legal/EULA.en.md) (Hebrew:
> [`legal/EULA.he.md`](legal/EULA.he.md)), which has not yet been settled by a
> lawyer and still carries unfilled placeholders. Where this page and the EULA
> ever disagree, **the EULA governs**.

> **⚠️ What is shipping, and what is not.** QueryGate the gateway ships today,
> and so do the subscription mechanics this page describes: the entitlement and
> its offline verification, the renewal countdown, the refusal after lapse, and
> the four-field licence call are all implemented and tested (TODO.md items 211,
> 212, 215 and 216). **What is not built is self-service activation** — enrolling
> a deployment through a signed-in browser flow (item 213) — **and the customer
> portal** (item 217): signup, checkout and downloads. Until those exist, an
> account is opened and a deployment enrolled by hand. This page is otherwise a
> description of code that exists; the earlier version of this banner said the
> reverse and had gone stale.

## Is QueryGate open source?

**No.** QueryGate is **proprietary, closed-source, commercial software**, sold as
a paid subscription. The source is not published, there is no free tier for
production use, and no version of it converts to an open-source licence on any
date.

If you have read an older document in this repository promising a Business
Source Licence flip, unlimited free production use, or a four-year conversion to
Apache-2.0: **that plan was cancelled on 2026-08-23** and those documents are
banner-marked as superseded. See [`business/GTM_SAAS.md`](business/GTM_SAAS.md)
for the decision.

## What am I buying?

A subscription licence to install and run QueryGate **in your own environment**,
for the term you have paid for, at the scope set out in your order.

- **Monthly, billed one month in advance.** An annual term is available at a
  discount, billed annually in advance.
- **Auto-renews** unless you turn that off.
- **Priced on governed database connections and human seats** — the two things
  that track the value you get. Your tier's limits are in your order and are
  enforced in software.
- **Self-hosted, always.** We do not host your gateway, we do not run queries,
  and there is no QueryGate cloud that your data passes through. See "Does my
  data reach you?" below.

## What happens if I cancel?

**You keep full access until the end of the period you have already paid for**,
and the subscription then expires. Nothing is cut off mid-term.

**Fees already paid are not refunded and are not pro-rated** (EULA §14.3). If you
cancel an hour after a monthly charge, that month is yours to use and the money
is not coming back. We would rather you read that here than discover it.

## What happens when a subscription lapses?

This is the part to read carefully, because the honest answer is "the product
stops doing its main job".

| State | What happens |
|---|---|
| Paid and renewing | Nothing. |
| Auto-renew off (or a failed payment), under 30 days left | A persistent countdown banner in the admin and access UIs, an email from the licence service, a `querygate-license status` line, and `"subscription": "renewal_due"` on `/health` plus the matching metric. The banner, the email and the CLI name the exact date; **the health field and the metric deliberately do not** — see below. |
| Term expired, inside the grace window | **Everything keeps working.** The banner escalates to critical — `expired` is the more urgent level and is reserved for a term that is actually being refused; `/health` still reports `renewal_due`. |
| Past the term *and* the grace window | **Every governed query, every governed write, and every live schema reflection is refused** with HTTP 402 over REST, and the `SUBSCRIPTION_EXPIRED` error code over MCP (JSON-RPC has no HTTP status), each naming the renewal route. `/health` reports `"subscription": "expired"` — and still returns 200, because the process is healthy and refusing on a billing decision; a 503 would make an orchestrator restart it in a loop. |

**Why the health endpoint and the metric say less than the banner.** `/health`
is unauthenticated by design, for orchestrator readiness probes, and `/metrics`
is authenticated only by default — `metrics_require_auth=false` is a supported
configuration. Both are therefore treated as public, and both carry a
three-value subscription state (`ok` / `renewal_due` / `expired`) and, on the
subscription, nothing else: no org, no plan, no dates, no counts. An expiry date there would tell anyone who can
reach the port the day this customer's gateway stops serving. The day countdown
lives on the authenticated banner, the renewal email, and
`querygate-license status`. Grace is not published as its own state for the same
reason — the operator action is identical to any other renewal warning, and the
distinction is a commercial fact about the customer rather than an operational
one.

**The renewal email comes from the licence service, not from your deployment.**
QueryGate has no customer email address and no SMTP credentials, and the licence
refresh is its only outbound call **to us**. (It does make outbound calls you
configure — your IdP for JWKS and OIDC, your secret store, your S3 audit
archive; those are yours, and `docs/SECURITY_POSTURE.md` lists them.) Giving the gateway a mail
client so it could warn its own operator would add an egress channel inside your
network to deliver a message we can already send from ours.

**What keeps working even then**, because you may need it precisely when you are
in a billing dispute (EULA §15.2): the health and metrics endpoints, the licence
status view, and **retrieval and integrity verification of your own audit
records**. **Reading live database schema through the gateway** is suspended along with
queries and writes — `list_tables`, `describe_table` and catalog search all
reflect against your database, and that is the funnel that stops. Configuration
reads and writes, connection tests, and audit retrieval are **not** gated: a
customer in a billing dispute must be able to retrieve their records and roll
back a policy, and the code reflects that rather than the stricter sentence this
paragraph used to carry. Your audit records are yours — the EULA grants a
**perpetual, irrevocable licence** to export and verify them that survives
termination even for breach (§18). A product that held your compliance records
hostage would be indefensible in the regulated sectors that need this most.

**Nothing is deleted.** Lapsing suspends governed operation; it does not
uninstall anything, touch your databases, or drop your configuration. Paying
resumes full operation without reinstalling (EULA §7.4).

## So there *is* a kill switch?

There is a **term**, and the software enforces it. We are not going to euphemise
that. What we will do is bound it precisely:

- **A failed licence check never blocks anything.** Only a confirmed,
  server-side non-renewal does, and only after the full paid term *plus* a grace
  window has elapsed. If our licence service is down for a week, nothing happens
  to you.
- **It cannot be a surprise.** Roughly 45 days of escalating, visible warnings
  precede any refusal.
- **A first boot with no network fails open**, in grace. A fresh install, a DR
  failover into an isolated network, or a scaled-out replica has nothing to be
  "valid" from, and our outage must never become your outage on day one.
- **If our service is unavailable long enough that your entitlement would
  otherwise lapse, we owe you a replacement entitlement covering that period**
  and the lapse is not treated as expiry (EULA §17.3). Your operation is not
  supposed to depend on our uptime, and the contract says so.
- **Air-gapped and offline deployments** are supported on the Enterprise tier
  with an entitlement issued out of band — no network involved at all.

Disabling or patching the check is a material breach of the EULA (§15.5), and
that clause — not the code — is the layer that actually holds. Compilation raises
the bar; it does not make the gate unbypassable, and we are not going to pretend
otherwise.

## Does it phone home? Does my data reach you?

**A licence call: yes. Your data: no, and there is no code path for it.**

**What is transmitted**, on start-up and roughly once a day (EULA §16.1) — four
fields, and that is the whole list:

```
{ org_id, deployment_id, connection_count, seat_count }
```

Two of those are operational facts about your estate. They are transmitted so
your tier's connection and seat limits can be applied, and so an upgrade happens
automatically instead of by invoice surprise. We are naming them rather than
describing this as "just an org id".

**What is never transmitted** (EULA §16.2): database credentials or connection
strings, query text, query results, row values, schema or catalog content, audit
records, policy files, personal data of your users, or anything else derived from
your databases. **There is no transmission path for any of it in the software.**

**You do not have to take that on trust.**
[`tests/security/test_no_phone_home.py`](../tests/security/test_no_phone_home.py)
fails the build if a QueryGate-controlled hostname or a licence-server,
activation, or telemetry endpoint appears anywhere in shipped source **outside
the single `src/querygate/subscription/` package** — and it separately enforces a
four-rule contract on that package: the field list must be declared once as a
literal and match this page; no mapping reaching a request body may spread
another mapping into itself; none may be written out in place as the body; and
every key in one must be a disclosed field or an HTTP header. Both spellings
count — `{**payload, "hostname": h}` and `dict(**payload, hostname=h)` are the
same thing to the wire and to this contract.

**One honest limit.** This is a static check
on mappings: a body built from a typed model with a fifth attribute would pass
it, and catching that is the job of a runtime assertion on the real request body.
The contract now has a real subject — `src/querygate/subscription/` exists and
`test_the_live_subscription_package_satisfies_the_contract` runs every rule
against it, not only against planted source. Run
`grep -rniE '\bquerygate\.(com|io|dev|net|org|ai|sh|app|cloud)\b' src/` yourself — it is a
one-line check and it is meant to be run. (The word boundaries matter: without
them the pattern also matches `querygate.compiler`, and you would get eight
false hits.)

**One honest boundary:** that claim is about **QueryGate**. The container image
also carries Microsoft's ODBC driver for SQL Server, whose own licence terms
contemplate data collection. We have not audited that third-party component's
behaviour and do not speak for it — see
[`CONTAINER_IMAGE_LICENCES.md`](CONTAINER_IMAGE_LICENCES.md).

## Can our MSP, consultancy, or systems integrator run it for us?

Yes, if your order says so. A third party operating the software **solely on your
behalf and for your internal use** is an ordinary arrangement and we will write it
into the Order. What is not permitted under any order is standing up one QueryGate
estate and reselling access to it across a client base — that is offering
QueryGate as a service, which is a different product and a different conversation.

## Can we run it for our subsidiaries and affiliates?

That is a scope question, so it is answered by your Order (EULA §13), not by a
blanket grant. Tell us the group structure and we will price and scope it. There
is no longer a licence clause defining "You" to include entities under common
control — that construct belonged to the cancelled BSL grant.

## Can we read or modify the source?

No. The EULA prohibits reverse engineering, decompiling, and creating derivative
works (§3(c), §3(d)), except where applicable law forbids that restriction.
Enterprise customers can negotiate **source escrow** in the Order — the mechanism
that answers "what if you disappear" without publishing the source. ⚠️ **Not
available yet:** no escrow agent is engaged, and no auditor-access-under-NDA
process exists. Both are intended; ask us where each stands rather than planning
around them.

## Can we contribute?

There is no external contribution process. QueryGate is closed-source and the
repository is private, so the Contributor Licence Agreement that existed for a
public source-available repository has been retired along with the BSL plan.

## What does a paid subscription include beyond the software?

- **A support SLA**, scoped honestly to what we can sign: business-hours response
  and best-effort severity-1.
- **The security packet** — completed vendor security questionnaires, SBOM,
  third-party licence inventory, and the composed
  [trust evidence page](TRUST_EVIDENCE.md).
- **Private CVE pre-notification**, ahead of public disclosure.
- **Upgrade assistance and policy-design consulting.**
- **Enterprise tier only:** indemnification, air-gapped/offline entitlement,
  source escrow, unlimited connections.

**Five things named on this page are forward statements, not things you can buy
today:** no external penetration test has been performed; **QueryGate Notary** —
third-party anchoring of the audit ledger's chain-head hashes — is a planned
service that does not exist; **source escrow** has no engaged agent; **auditor
access under NDA** has no defined process; and the signed, provenance-attested
image pipeline is wired and CI-exercised but **has never run on a published
tag**, so there is no signature to verify yet. All five are named because they
are the plan. Ask us where each one stands before relying on it.

## What licence documents govern, exactly?

1. **[`legal/EULA.en.md`](legal/EULA.en.md)** — the licence of record. English
   governs; [`legal/EULA.he.md`](legal/EULA.he.md) is the Hebrew version (EULA
   §12).
2. **Your Order** — the order form, quote, or signed agreement setting your
   scope, seats, connections, term, and fees. Where the Order and the EULA
   conflict, the **Order** controls (EULA §13).
3. **[`../LICENSE`](../LICENSE)** — a notice, not a grant. It reserves all rights
   and points at the EULA. It exists in the repository, the wheel, the sdist, and
   the container image because packaging requires a licence file to be present.

Third-party open-source components we redistribute keep their own licences,
unaffected by any of the above:
[`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md) and
[`CONTAINER_IMAGE_LICENCES.md`](CONTAINER_IMAGE_LICENCES.md).

## Where do I ask a question this page does not answer?

Ask us directly. If the answer turns out to be interesting to more than one
customer, it belongs on this page and we will add it.
