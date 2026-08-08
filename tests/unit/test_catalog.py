"""Unit tests for the curated schema-catalog overlay (querygate/catalog/)."""

from __future__ import annotations

import pytest

from querygate.catalog.loader import CatalogStore
from querygate.catalog.models import (
    CatalogEntryProvenance,
    CatalogEntryStatus,
    KnowledgeSourceClass,
    SensitivityClass,
    replacement_decision,
    visible_relationships,
)
from querygate.policy.models import Policy


def _catalog_dict() -> dict:
    return {
        "version": 1,
        "connections": {
            "demo": {
                "tables": {
                    "Customers": {
                        "description": "One row per customer.",
                        "aliases": ["clients"],
                        "sensitivity": "internal",
                        "default_aggregation": "count(customers.id)",
                        "allow_samples": False,
                        "relationships": [
                            {
                                "to_table": "orders",
                                "column": "id",
                                "to_column": "customer_id",
                                "description": "A customer's orders.",
                            }
                        ],
                        "columns": {
                            "Email": {
                                "description": "Customer email address.",
                                "sensitivity": "pii",
                                "allow_samples": False,
                            }
                        },
                    }
                }
            }
        },
    }


def test_from_dict_parses_nested_structure():
    store = CatalogStore.from_dict(_catalog_dict())
    entry = store.get_table("demo", "Customers")
    assert entry is not None
    assert entry.description == "One row per customer."
    assert entry.sensitivity == SensitivityClass.INTERNAL
    assert entry.column("Email").sensitivity == SensitivityClass.PII
    assert entry.provenance.entry_id.startswith("urn:querygate:catalog:")
    assert entry.provenance.catalog_version == 1
    assert entry.provenance.source_class == KnowledgeSourceClass.VERIFIED
    assert entry.provenance.status == CatalogEntryStatus.VERIFIED
    assert entry.provenance.precedence == 400


def test_legacy_entry_ids_are_stable_across_loads_and_relationship_reordering():
    original = _catalog_dict()
    first = CatalogStore.from_dict(original).get_table("demo", "customers")
    second = CatalogStore.from_dict(original).get_table("demo", "customers")

    assert first.provenance.entry_id == second.provenance.entry_id
    assert first.column("email").provenance.entry_id == second.column("email").provenance.entry_id
    assert first.relationships[0].provenance.entry_id == second.relationships[0].provenance.entry_id


def test_inferred_content_cannot_load_as_verified_or_omit_confidence():
    raw = _catalog_dict()
    provenance = {
        "source_class": "inferred",
        "status": "verified",
        "confidence": 0.8,
        "source_evidence": [{"kind": "model", "reference": "job:123"}],
    }
    raw["connections"]["demo"]["tables"]["Customers"]["provenance"] = provenance
    with pytest.raises(Exception, match="cannot be verified"):
        CatalogStore.from_dict(raw)

    provenance["status"] = "draft"
    provenance["confidence"] = None
    with pytest.raises(Exception, match="requires confidence"):
        CatalogStore.from_dict(raw)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sensitivity", "pii", "only verified knowledge may set"),
        ("allow_samples", True, "only verified knowledge may enable"),
    ],
)
def test_inferred_drafts_cannot_change_sensitivity_or_enable_samples(field, value, message):
    raw = _catalog_dict()
    email = raw["connections"]["demo"]["tables"]["Customers"]["columns"]["Email"]
    if field == "allow_samples":
        email["sensitivity"] = "none"
    email[field] = value
    email["provenance"] = {
        "source_class": "inferred",
        "status": "draft",
        "confidence": 0.8,
        "source_evidence": [{"kind": "model", "reference": "job:123"}],
        "model_id": "manual-only",
    }

    with pytest.raises(Exception, match=message):
        CatalogStore.from_dict(raw)


def test_precedence_never_allows_inferred_overwrite_of_verified_or_sensitivity():
    verified = CatalogEntryProvenance()
    inferred = CatalogEntryProvenance(
        source_class="inferred",
        status="draft",
        confidence=0.7,
        source_evidence=[{"kind": "model", "reference": "job:123"}],
        model_id="manual-only-test-provider",
    )

    description = replacement_decision(verified, inferred, field_name="description")
    sensitivity = replacement_decision(verified, verified, field_name="sensitivity")

    assert not description.allowed
    assert "human-verified" in description.reason
    assert not sensitivity.allowed
    assert "never changed" in sensitivity.reason

    draft_over_draft = replacement_decision(inferred, inferred, field_name="description")
    assert not draft_over_draft.allowed
    assert "not publishable" in draft_over_draft.reason


def test_table_lookup_is_case_insensitive():
    store = CatalogStore.from_dict(_catalog_dict())
    assert store.get_table("demo", "customers") is not None
    assert store.get_table("demo", "CUSTOMERS") is not None
    assert store.get_table("demo", "orders") is None


def test_column_lookup_is_case_insensitive():
    store = CatalogStore.from_dict(_catalog_dict())
    entry = store.get_table("demo", "customers")
    assert entry.column("email") is not None
    assert entry.column("EMAIL") is not None
    assert entry.column("phone") is None


def test_table_and_column_lookup_use_casefold_not_lower():
    # `.lower()` and `.casefold()` disagree on a handful of real Unicode
    # identifiers: "STRASSE".lower() == "strasse" but "straße".lower() ==
    # "straße" (unchanged) -- while .casefold() unifies both to "strasse".
    # `ConnectionCatalog.table`/`TableCatalogEntry.column` used to use
    # `.lower()`, the odd one out against this same class's own uniqueness
    # validators (`_table_names_are_unique`/the column-uniqueness check),
    # which already used `.casefold()` -- so a table/column an operator's own
    # catalog.yaml treats as a single unique entry could fail to resolve here,
    # silently dropping its sensitivity label from the human-approval gate
    # (TODO.md item 149; found by `security-invariant-reviewer` while
    # reviewing the `policy/models.py` sibling fix for the same bug class).
    catalog = {
        "version": 1,
        "connections": {
            "demo": {
                "tables": {
                    "STRASSE": {
                        "sensitivity": "pii",
                        "columns": {"HAUSNUMMER": {"sensitivity": "pii"}},
                    }
                }
            }
        },
    }
    store = CatalogStore.from_dict(catalog)
    entry = store.get_table("demo", "straße")
    assert entry is not None
    assert entry.column("hausnummer") is not None


def test_unknown_connection_returns_none():
    store = CatalogStore.from_dict(_catalog_dict())
    assert store.get_table("other", "customers") is None


def test_empty_store_has_no_entries():
    store = CatalogStore.empty()
    assert store.get_table("demo", "customers") is None
    assert store.connection_ids() == []


def test_connection_ids_lists_every_referenced_connection():
    store = CatalogStore.from_dict(
        {
            "connections": {
                "demo": {"tables": {}},
                "reporting": {"tables": {}},
            }
        }
    )
    assert store.connection_ids() == ["demo", "reporting"]


def test_from_file_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        CatalogStore.from_file(str(tmp_path / "does_not_exist.yaml"))


def test_extra_field_is_rejected():
    with pytest.raises(Exception):
        CatalogStore.from_dict(
            {
                "connections": {
                    "demo": {"tables": {"customers": {"not_a_real_field": 1}}},
                }
            }
        )


def test_unknown_future_catalog_version_is_rejected_instead_of_silently_loaded():
    with pytest.raises(Exception):
        CatalogStore.from_dict({"version": 3, "connections": {}})


def test_schema_snapshots_require_catalog_version_2():
    with pytest.raises(Exception, match="require catalog version 2"):
        CatalogStore.from_dict(
            {
                "version": 1,
                "connections": {},
                "schema_snapshots": {"demo": {"connection_id": "demo"}},
            }
        )


def test_duplicate_explicit_entry_ids_are_rejected():
    raw = _catalog_dict()
    duplicate = "urn:querygate:catalog:duplicate"
    table = raw["connections"]["demo"]["tables"]["Customers"]
    table["provenance"] = {"entry_id": duplicate}
    table["columns"]["Email"]["provenance"] = {"entry_id": duplicate}

    with pytest.raises(Exception, match="entry ids must be unique"):
        CatalogStore.from_dict(raw)


def test_evidence_reference_rejects_query_or_credential_shaped_payloads():
    raw = _catalog_dict()
    raw["connections"]["demo"]["tables"]["Customers"]["provenance"] = {
        "source_evidence": [
            {"kind": "manual", "reference": "postgres://user:password@example/db?token=x"}
        ]
    }

    with pytest.raises(Exception, match="evidence references"):
        CatalogStore.from_dict(raw)


def test_visible_relationships_drops_hints_toward_denied_tables():
    store = CatalogStore.from_dict(_catalog_dict())
    entry = store.get_table("demo", "customers")
    assert [relationship.to_table for relationship in visible_relationships(entry, Policy())] == [
        "orders"
    ]
    assert visible_relationships(entry, Policy(denied_tables=["orders"])) == []
    assert (
        visible_relationships(entry, Policy(allowed_tables=["customers"])) == []
    )  # orders not in the allow-list either


def test_visible_relationships_filters_both_join_columns_before_disclosure():
    entry = CatalogStore.from_dict(_catalog_dict()).get_table("demo", "customers")
    assert (
        visible_relationships(
            entry,
            Policy(denied_columns={"customers": ["id"]}),
            from_table="customers",
        )
        == []
    )
    assert (
        visible_relationships(
            entry,
            Policy(denied_columns={"orders": ["customer_id"]}),
            from_table="customers",
        )
        == []
    )
