"""Unit tests for the curated schema-catalog overlay (querygate/catalog/)."""

from __future__ import annotations

import pytest

from querygate.catalog.loader import CatalogStore
from querygate.catalog.models import RelationshipHint, SensitivityClass, visible_relationships
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


def test_visible_relationships_drops_hints_toward_denied_tables():
    store = CatalogStore.from_dict(_catalog_dict())
    entry = store.get_table("demo", "customers")
    assert visible_relationships(entry, Policy()) == [
        RelationshipHint(
            to_table="orders",
            column="id",
            to_column="customer_id",
            description="A customer's orders.",
        )
    ]
    assert visible_relationships(entry, Policy(denied_tables=["orders"])) == []
    assert (
        visible_relationships(entry, Policy(allowed_tables=["customers"])) == []
    )  # orders not in the allow-list either
