---
id: configuration.catalog
title: Add a semantic schema catalog
summary: An optional catalog adds business descriptions, aliases, sensitivity labels, aggregation hints, and policy-filtered relationships.
tags: [configuration, catalog.yaml, metadata, sensitivity, pii, descriptions, relationships]
next_actions:
  - Add catalog entries only after connection and policy configuration validate successfully.
---
`catalog.yaml` is an optional descriptive overlay. It can explain tables and
columns, add aliases and sensitivity labels, control whether samples are
appropriate, suggest a default aggregation, and document relationships.

Catalog content never grants access and does not alter query validation. A
denied column is removed before its catalog metadata is attached, and a
relationship hint to a denied table is omitted. This keeps curated business
metadata within the same visibility boundary as ordinary schema discovery.

The catalog is validated against the configured connection IDs and reloads
with connections and policy. Treat descriptions as administrator-curated
guidance, not as database-enforced foreign keys or authorization rules.
