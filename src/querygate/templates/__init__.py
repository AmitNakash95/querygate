"""Admin-defined, named, parameterized query templates (TODO.md item 48).

A query template is a stored, reviewed `StructuredQuery` skeleton with typed
parameter slots. Agents invoke a template *by name* with typed parameters
instead of composing an arbitrary query — shrinking the effective surface to a
finite, admin-reviewed set of query shapes ("these are the 12 things this agent
can ask").

This is additive to QueryGate's core guarantee, never a new one: a template is
just a stored `StructuredQuery` AST — there is still no raw-SQL field anywhere.
At invocation the caller's parameters are bound into the skeleton and the
*resulting* `StructuredQuery` runs through the unchanged `StructuredQueryService`
pipeline, so a bound template inherits every policy cap, allow/deny list,
mandatory row filter, and guardrail that an ad-hoc query does — parameters get
no special exemption.

Phase 1 (this module): templates are file-configured (`TEMPLATES_FILE`),
hot-reloadable, and invocable over REST and MCP, filtered per-principal by
target-connection visibility. The governed create/edit/approve/publish/rollback
workflow (reusing item 32B's state machine) is phase 2.
"""
