---
id: configuration.catalog
title: Add a semantic schema catalog
summary: A versioned catalog adds business metadata, durable provenance, schema fingerprints, and policy-first compact retrieval.
tags: [configuration, catalog.yaml, metadata, provenance, fingerprints, retrieval, sensitivity, pii, descriptions, relationships]
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
schema structure and hashes rather than stores raw database comments. Automatic
refresh/invalidation and manual-only generated drafts are not part of 32A-1.

The catalog is validated against the configured connection IDs and reloads
with connections and policy. Treat descriptions as administrator-curated
guidance, not as database-enforced foreign keys or authorization rules.
