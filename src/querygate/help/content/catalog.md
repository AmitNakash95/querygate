---
id: configuration.catalog
title: Add a semantic schema catalog
summary: A versioned catalog adds business metadata, provenance, safe retrieval, selective refresh, and quarantined manual drafts.
tags: [configuration, catalog.yaml, metadata, provenance, fingerprints, retrieval, refresh, drafts, benchmark, sensitivity, pii, descriptions, relationships]
next_actions:
  - Add catalog entries only after connection and policy configuration validate successfully.
---
`catalog.yaml` is an optional descriptive overlay. It can explain tables and
columns, add aliases and sensitivity labels, control whether samples are
appropriate, suggest a default aggregation, and document relationships.

Catalog content never grants access and does not alter query validation. A
denied column is removed before its catalog metadata is attached, and a
relationship hint to a denied table—or using a denied join column—is omitted.
`search_catalog` applies policy before tokenizing, ranking, counting, or
relationship traversal and returns only bounded metadata, never database rows.

Catalog version 2 gives every table, column, and relationship a deterministic
stable id plus source evidence/class, status, confidence, catalog/schema
version, freshness, actors/timestamps, and server-derived precedence. Verified
knowledge cannot be overwritten by inferred/learned proposals, and semantic
merging cannot change a sensitivity label. Version-1 files load unchanged and
receive deterministic manually verified provenance in memory.

Agent-facing citations hash evidence references and omit creator, approver,
and model identities. Those fields remain in the privileged catalog record for
future governed review without becoming another principal-disclosure surface.

Version 2 may persist a row-free schema snapshot. The fingerprint/diff covers
schema structure and hashes rather than stores raw database comments. An
opt-in background refresh atomically updates snapshots, marks only affected
entries and dependent relationships/proposals stale, and rebinds unaffected
active entries. It remains off the query path and disabled by default.

The semantic-memory provider defaults to `disabled`; the only other shipped
mode is offline `manual`. Strict structured imports tied to the current schema
fingerprint create separate inferred draft proposals with deterministic IDs.
They cannot contain policy/sensitivity/sampling fields, never overwrite
verified entries, and are not agent-searchable. Use
`querygate-semantic-memory refresh`, `generate-drafts`, and `evaluate`; the
packaged deterministic benchmark has fixed release thresholds and no network
dependency. Approval/publication is not part of phase 32A.

The catalog is validated against the configured connection IDs and reloads
with connections and policy. Treat descriptions as administrator-curated
guidance, not as database-enforced foreign keys or authorization rules.
