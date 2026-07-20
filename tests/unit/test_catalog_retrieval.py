"""Policy-first compact catalog retrieval tests."""

from __future__ import annotations

import json

import pytest

from querygate.catalog.loader import CatalogStore
from querygate.catalog.retrieval import CatalogFreshness, search_catalog
from querygate.catalog.schema_memory import ObservedSchemaSnapshot
from querygate.policy.models import Policy


def _store() -> CatalogStore:
    snapshot = ObservedSchemaSnapshot(connection_id="demo")
    return CatalogStore.from_dict(
        {
            "version": 2,
            "schema_snapshots": {"demo": snapshot.model_dump(mode="json")},
            "connections": {
                "demo": {
                    "tables": {
                        "orders": {
                            "description": "Purchases and revenue.",
                            "aliases": ["sales"],
                            "relationships": [
                                {
                                    "to_table": "customers",
                                    "column": "customer_id",
                                    "to_column": "id",
                                    "description": "Customer who placed the purchase.",
                                }
                            ],
                            "columns": {
                                "customer_id": {"description": "Purchasing customer key."},
                                "internal_margin": {
                                    "description": "Secret finance profitability metric."
                                },
                            },
                        },
                        "customers": {
                            "description": "Registered customer accounts.",
                            "columns": {"id": {"description": "Customer key."}},
                        },
                        "discarded": {
                            "description": "Should never be searchable.",
                            "provenance": {
                                "source_class": "inferred",
                                "status": "rejected",
                                "confidence": 0.2,
                                "source_evidence": [{"kind": "model", "reference": "job:rejected"}],
                            },
                        },
                    }
                }
            },
        }
    )


def test_search_citation_is_compact_by_default():
    response = search_catalog(
        _store(), connection_id="demo", policy=Policy(), query="sales revenue", max_results=3
    )

    assert response.results
    assert response.results[0].table == "orders"
    citation = response.results[0].citation
    assert citation.model_dump() == {"status": "verified", "precedence": citation.precedence}
    assert response.result_count == len(response.results)


def test_search_returns_full_versioned_provenance_and_current_freshness_when_verbose():
    response = search_catalog(
        _store(),
        connection_id="demo",
        policy=Policy(),
        query="sales revenue",
        max_results=3,
        verbose_provenance=True,
    )

    assert response.results
    assert response.results[0].table == "orders"
    assert response.results[0].citation.entry_id.startswith("urn:querygate:catalog:")
    assert response.results[0].citation.status == "verified"
    assert response.results[0].citation.catalog_version == 2
    assert response.results[0].citation.freshness == CatalogFreshness.CURRENT
    assert response.result_count == len(response.results)


def test_policy_filters_hidden_objects_before_scoring_counts_and_relationships():
    restricted = Policy(denied_tables=["customers"], denied_columns={"orders": ["internal_margin"]})

    hidden_table = search_catalog(
        _store(), connection_id="demo", policy=restricted, query="customer"
    )
    hidden_column = search_catalog(
        _store(), connection_id="demo", policy=restricted, query="profitability"
    )

    # orders.customer_id is itself policy-visible and may legitimately match;
    # the denied customers table and relationship candidate must not.
    assert hidden_table.result_count == 1
    assert hidden_table.results[0].object_type == "column"
    assert hidden_table.results[0].table == "orders"
    assert hidden_table.results[0].column == "customer_id"
    assert hidden_column.result_count == 0
    serialized = json.dumps(
        [hidden_table.model_dump(mode="json"), hidden_column.model_dump(mode="json")]
    )
    assert '"to_table": "customers"' not in serialized
    assert '"table": "customers"' not in serialized
    assert "internal_margin" not in serialized
    assert "Secret finance" not in serialized


def test_rejected_and_archived_entries_are_not_searchable():
    response = search_catalog(
        _store(), connection_id="demo", policy=Policy(), query="never searchable"
    )
    assert response.results == []
    assert response.result_count == 0


def test_search_enforces_byte_budget_without_returning_oversized_first_hit():
    # verbose_provenance=True: the full citation is what's large enough to
    # exceed a 512-byte budget on its own — the default compact citation
    # (TODO.md item 64) now fits comfortably, which is the intended win.
    response = search_catalog(
        _store(),
        connection_id="demo",
        policy=Policy(),
        query="sales",
        max_response_bytes=512,
        verbose_provenance=True,
    )
    assert response.results == []
    assert response.result_count == 0
    assert response.truncated
    assert len(json.dumps(response.model_dump(mode="json")).encode("utf-8")) <= 512


def test_search_rejects_budget_smaller_than_response_envelope():
    with pytest.raises(ValueError, match="too small for its envelope"):
        search_catalog(
            _store(),
            connection_id="demo",
            policy=Policy(),
            query="a" + "é" * 255,
            max_response_bytes=512,
        )
