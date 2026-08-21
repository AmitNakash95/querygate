# QueryGate licensing FAQ

> **DRAFT — FOR LAWYER REVIEW. NOT LEGAL ADVICE.**
> This FAQ explains the intent of QueryGate's `LICENSE` in plain language. The
> licence itself has not yet been settled by a lawyer, and where this page and
> `LICENSE` ever disagree, `LICENSE` governs. Drafting notes for counsel are in
> [`LICENSE_NOTES.md`](LICENSE_NOTES.md).

## Internal production use is free, forever, for every version released under this
licence, with no limit.

That is the headline and it has no asterisk. Run QueryGate in production, inside
your organisation, against as many databases, with as many users, at whatever
scale you like, for as long as you like. Pay nothing. There is no user cap, no
database cap, no core cap, no seat count, no trial period, and no licence key to
obtain.

QueryGate **will be** licensed under the **Business Source License 1.1** (BSL 1.1)
— `LICENSE` is a draft not yet in force. BSL
is a source-available licence, not an open-source licence — the difference is
that one specific commercial use is restricted, and that after a fixed date each
version becomes fully open source under Apache-2.0.

---

## What exactly is prohibited?

One thing: **providing QueryGate itself to third parties** — hosted, managed,
or embedded, whether or not you charge for it.

The grant permits production use "provided that you do not provide the Licensed
Work to third parties on a hosted, managed, or embedded basis, whether or not
for a fee."

So this is not allowed:

- Running QueryGate as a multi-tenant hosted service that other organisations
  sign up for.
- Offering "managed QueryGate" as a product to a general customer base.
- Embedding QueryGate inside a product you sell, such that your customers get
  QueryGate's functionality as part of what they bought.

And this **is** allowed, without asking anyone:

- Running it in production for your own organisation, at any scale.
- Running it for your subsidiaries, your parent, and your sister companies. The
  licence's definition of "You" covers everything under common control with you;
  note that clause is drafted to stop a group structure being used to *fragment*
  the grant, and affiliate use follows from it as a consequence.
- Running it for your internal customers: other teams, other departments, other
  business units.
- Reading, modifying, forking, and rebuilding the source for your own use.
- Evaluating it, benchmarking it, and publishing what you find.
- Running it behind a product you sell, as part of your own internal
  infrastructure, where your customers are not being given access to QueryGate.
  **⚠️ This last one is our reading, not something the grant text settles.** The
  grant prohibits providing the Licensed Work on an "embedded" basis and does not
  define that word; HashiCorp's equivalent grant needs roughly twenty lines of
  definitions to draw the same line. If your product's value depends on
  QueryGate being in it, **ask us in writing before relying on this.**

The line is not scale. The line is whether QueryGate itself is the thing being
supplied to a third party.

## Can our MSP / consultancy / systems integrator run it for us?

**Yes — that is what the carve-out is for**, with one caveat we would rather
state than have you discover.

The grant says: "except that a third party may install, operate, or manage the
Licensed Work solely on behalf of, and for the internal use of, a single
licensee."

So a managed service provider, a consultancy, or a contractor can deploy,
operate, upgrade, and administer QueryGate for you. They are working on your
behalf, for your internal use, and that is a permitted deployment.

**⚠️ The caveat:** the open question counsel is being asked (see
[`LICENSE_NOTES.md`](LICENSE_NOTES.md)) is whether this wording achieves the
carve-out *without* also permitting a multi-tenant hosted offering dressed up as
an MSP arrangement. A single MSP running a single instance for a single licensee
is squarely what it is for. If your arrangement is less clear-cut than that, ask
us in writing rather than relying on this page.

What that same MSP may **not** do is stand up one QueryGate estate and resell
access to it across their client base. Per client, on that client's behalf: fine.
As a shared service they sell: not fine.

## What counts as "production"?

Under this licence, mostly it does not matter — which is deliberate.

BSL 1.1 itself grants non-production use and lets the licensor grant production
use on top. Many BSL projects then attach conditions to production use, so the
boundary gets argued about. QueryGate's grant does not: **all internal use is
permitted, production or not.** There is no threshold to be on the wrong side
of, so there is nothing to measure, self-assess, or report.

The only place the word does any work is the one restriction above, and that
restriction does not turn on production-ness either — offering QueryGate to
third parties as a hosted, managed, or embedded service is restricted whether or
not you would call it production, and whether or not you charge for it.

If you are unsure whether something you want to do is permitted, the useful
question is not "is this production?" — it is "am I giving other organisations
access to QueryGate itself?"

## What happens at the Change Date?

**Each version of QueryGate becomes Apache-2.0 four years after it is
published.** Permanently, automatically, with no action required by anyone.

Some detail worth having:

- The Change Date is **per version**, not per project. Every release stamps its
  own concrete Change Date into its own `LICENSE` file. Version 1.0 converts
  four years after version 1.0 shipped; version 1.4 converts four years after
  version 1.4 shipped.
- The conversion is written into the licence you already have. It is not a
  promise to relicense later — it is a grant that takes effect on a date. Nobody
  can withdraw it, including us.
- Apache-2.0 is a permissive open-source licence. Once a version converts, every
  restriction on this page is gone for that version, including the hosted-service
  restriction.
- BSL 1.1 also has a backstop: the conversion happens on the Change Date **or**
  the fourth anniversary of that version's first public distribution, whichever
  comes first.

This is the part of BSL that a legal team usually cares about most: the
restriction has a fixed, non-negotiable expiry.

## Is QueryGate open source?

No, and we do not call it that. BSL 1.1 is **source-available**: the entire
source is public, readable, buildable, forkable, and modifiable, but one
commercial use is restricted, so it does not meet the Open Source Definition.
Each version becomes genuinely open source (Apache-2.0) at its Change Date.

We chose this deliberately over the alternative — an "open core" split where the
interesting parts are held back. QueryGate's differentiators are its policy
enforcement, its per-human attribution, and its tamper-evident audit ledger.
Holding any of those back would make the free product not worth evaluating. BSL
restricts *resale*, never *capability*: everything is in the box.

## Do we need to tell you we are using it? Does it phone home?

No, and no.

There is nothing to register, no licence key to install, no activation, and no
usage report. QueryGate contains **no telemetry of any kind** — not for
licensing, not for analytics. It makes no outbound calls to us, ever. That is a
product commitment as much as a licensing one: QueryGate sits in the request path
between an agent and your operational database, and a beacon originating inside
a customer's network would contradict the entire premise.

You do not have to take that on trust, and you should not: `tests/security/test_no_phone_home.py` fails the build if a QueryGate-controlled hostname, a licence-server or activation endpoint, or a telemetry URL ever appears in shipped source. One honest boundary on that: the claim is about **QueryGate**. The container image also carries Microsoft's ODBC driver for SQL Server, whose own licence terms contemplate data collection; we have not audited that third-party component's behaviour and do not speak for it. See `docs/CONTAINER_IMAGE_LICENCES.md`. Run `grep -rniE 'querygate\.(com|io|dev|net)' src/` yourself — it is a one-line check and it is meant to be run.

There is also no kill switch, no time bomb, and no check that can refuse to start
or block a query. If a licensing feature is ever added for the paid tier, it will
be a locally-verified entitlement token that at most logs a warning — it will
never interrupt data access.

## What does the paid tier add, if the software is free?

Not software features. Everything QueryGate does technically is in the free,
source-available product. What the paid tier adds is a counterparty who is
contractually on the hook:

- **Indemnification** — contractual liability the free user does not get.
- **A support SLA** — scoped honestly to what we can actually sign, which today
  means business-hours response and best-effort severity-1.
- **The security packet** — penetration test report, completed vendor security
  questionnaires, SBOM, third-party licence inventory, and the composed trust
  evidence page.
- **Private CVE pre-notification**, ahead of public disclosure.
- **Upgrade assistance and policy-design consulting.**
- **QueryGate Notary** — third-party anchoring of the audit ledger's chain head
  hashes, which turns "tamper-evident if you trust the operator" into
  "tamper-evident *against* the operator".

**Two of those are honest forward statements, not things you can buy today.**
QueryGate Notary is a planned service that does not exist yet, and no external
penetration test has been performed yet. They are listed because they are the
plan, not because they are shipping. Ask us where each one actually stands
before you rely on it.

## We are a cloud provider / database vendor / platform. Can we offer QueryGate?

Not under this licence — that is precisely the restriction. Talk to us about a
commercial licence instead; BSL 1.1 explicitly contemplates one ("you must
purchase a commercial license from the Licensor... or you must refrain from
using the Licensed Work"), and the alternative-arrangements contact is in
`LICENSE`.

## Can we contribute?

Yes — see [`../CONTRIBUTING.md`](../CONTRIBUTING.md). Contributions **will require** a
lightweight click-through Contributor Licence Agreement — nothing enforces it yet
(see `.github/cla/`) — for the ordinary reason:
QueryGate is sold under commercial licences alongside BSL, and that is not
possible over code the project does not hold the rights to relicense.

## Where do I ask a question this page does not answer?

Open a discussion or an issue in the repository. If the answer turns out to be
interesting to more than one person, it belongs on this page and we will add it.
