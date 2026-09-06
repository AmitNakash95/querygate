# Support

Read this before you ask — it will save you time, and it is honest about what
you are and are not getting.

## There is no SLA, and no commercial support

QueryGate has **no support contract, no response-time commitment, and no uptime
guarantee**. There is no paid tier that changes this today. It is maintained by
one person alongside other work.

The one exception is security. [`SECURITY.md`](SECURITY.md) states target times
for acknowledging and triaging a vulnerability report, and those are the only
timing commitments this project makes. They are a best effort by a single
maintainer, not a contractual SLA.

If you are evaluating QueryGate for something that needs a support agreement,
read [the project-maturity statement in the README](README.md#project-maturity--read-this-before-you-evaluate)
first. The honest answer is that this project is not there yet.

## Answer it yourself, first — the docs are unusually complete

QueryGate ships a lot of documentation, and it is kept reconciled with the code
by CI gates rather than by good intentions. Before opening an issue:

| Question | Where the answer is |
|---|---|
| What can it do? What does a request look like? | [`docs/FEATURE_REFERENCE.md`](docs/FEATURE_REFERENCE.md) |
| Why is it built this way? What was traded off? | [`docs/PRODUCT_GUIDE.md`](docs/PRODUCT_GUIDE.md), including its Decision Log |
| Is *this* thing missing on purpose? | [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md) |
| What does this config field do? | `GET /api/v1/help/config-fields/{app\|connection\|policy\|catalog}/{field}` |
| What does this error mean? | `GET /api/v1/help/errors/{error_code}` |
| Why can't *my* principal see this table? | `GET /api/v1/help/my-access` |
| What scope do I need? | [`docs/SCOPE_CATALOG.md`](docs/SCOPE_CATALOG.md) |
| How do I deploy it properly? | [`deploy/README.md`](deploy/README.md), [`deploy/runbook.md`](deploy/runbook.md) |

The built-in guide (`GET /api/v1/help/search?q=...`, and the equivalent MCP
tools) works offline against the version you actually have installed, which
makes it more reliable than any web search.

## Opening an issue

Use the [issue templates](https://github.com/AGitmit/QueryGate/issues/new/choose).
In short:

- **Bug** — include the QueryGate version or image digest, the dialect, the
  `StructuredQuery` AST or request that triggers it, and the relevant policy.
  Redact real data; a minimal reproduction against the demo Postgres
  (`docker compose up -d`) is worth ten paragraphs of description.
- **Feature request** — say what you are trying to accomplish, not just the
  feature you have in mind. Check [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md)
  and the README's "What it deliberately does not do" first: several obvious
  requests (a raw-SQL escape hatch, stored-procedure pass-through) are
  **permanent non-goals**, not backlog items, and a request for one will be
  declined on principle.
- **Question** — fine, but check the table above first.

## Do not report a vulnerability in a public issue

Read [`SECURITY.md`](SECURITY.md). A public issue describing a bypass is a
public disclosure of that bypass.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for local setup, the test tiers, and
what a good pull request looks like, and [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
for the behavioural expectations.

## Commercial enquiries

There is no company, no pricing page, and no sales contact yet — see the
project-maturity statement linked above. If you are interested in a design
partnership, open a GitHub issue saying so and the maintainer will follow up.
