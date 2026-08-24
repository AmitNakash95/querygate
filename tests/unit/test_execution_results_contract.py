"""The response models are a published API contract, so their move must be pinned.

`execution/results.py` was extracted from `execution/service.py` so the read
enforcement funnel could be Cython-compiled (TODO.md item 214). The refactor was
verified only by "3324 tests before, 3324 after" — which proves no existing test
broke, **not** that the move was faithful. Ten of the twelve are published in
the OpenAPI component schemas; a dropped field or a renamed component is a
silent breaking change for every client, and nothing in the suite asserted the
component set at all.

Two properties are pinned:

1. **Identity through the re-export.** `service.X is results.X`, so the eight
   files still importing from `service` get the same class, not a copy.
2. **The published schema is unchanged.** Each model still appears in
   `components.schemas` under its own name, with the field set the class
   declares — the assertion that would have caught a field lost in transit.

Deliberately not a full schema snapshot: that would fail on every unrelated
model change and get regenerated without being read, which is worse than no
test.
"""

from __future__ import annotations

import pytest

from querygate.execution import results, service

pytestmark = pytest.mark.unit

#: Every model moved out of `service.py` by the item-214 extraction.
MOVED_MODELS = (
    "BatchExplainItemResult",
    "BatchQueryItemResult",
    "BatchVerdictItemResult",
    "ColumnCatalogInfo",
    "ColumnInfo",
    "ExplainResult",
    "RelationshipCatalogInfo",
    "StructuredQueryResult",
    "TableCatalogInfo",
    "TableDescription",
    "VerdictPlan",
    "VerdictResult",
)


def test_every_moved_model_lives_in_results():
    missing = [n for n in MOVED_MODELS if not hasattr(results, n)]
    assert not missing, f"not found in execution.results: {missing}"


def test_service_reexports_the_same_objects_not_copies():
    """`X is X`, not merely `X == X`.

    A re-export that rebuilt the classes would give every `isinstance` check in
    the eight importing files a different class object, and pydantic would emit
    two schema components for one concept.
    """
    for name in MOVED_MODELS:
        assert hasattr(service, name), f"service no longer re-exports {name}"
        assert getattr(service, name) is getattr(results, name), (
            f"{name} re-exported from service is a different object than "
            f"execution.results.{name}"
        )


def test_service_defines_no_pydantic_models():
    """The property the extraction existed to create.

    A module defining `BaseModel` subclasses cannot be Cython-compiled, and
    `service.py` holds `_validate_and_compile` — the single read enforcement
    funnel and the future home of item 211's subscription gate. If a model is
    ever added back here, compilation breaks and this says so.
    """
    import inspect

    import pydantic as pyd

    defined_here = [
        name
        for name, obj in vars(service).items()
        if inspect.isclass(obj)
        and issubclass(obj, pyd.BaseModel)
        and obj.__module__ == service.__name__
    ]
    assert not defined_here, (
        "execution/service.py defines pydantic models again, which makes it "
        f"un-compilable: {defined_here}. Put them in execution/results.py."
    )


def _openapi_schemas() -> dict:
    from querygate.api.app import create_app

    return create_app().openapi().get("components", {}).get("schemas", {})


#: Ten of the twelve reach a client through the REST OpenAPI schema. The two
#: batch *item* result types do not appear in the OpenAPI components OR the MCP
#: tool schemas -- they are internal shapes. Measured, not assumed: an earlier
#: draft of this file (and of `results.py`'s docstring) claimed all twelve were
#: published, and this test is what disproved it.
INTERNAL_ONLY = ("BatchExplainItemResult", "BatchVerdictItemResult")
PUBLISHED = tuple(n for n in MOVED_MODELS if n not in INTERNAL_ONLY)


def test_the_published_models_are_still_published_under_their_own_names():
    schemas = _openapi_schemas()
    missing = [n for n in PUBLISHED if n not in schemas]
    assert not missing, (
        f"these models vanished from the published OpenAPI components: {missing}. "
        "They are a client-facing contract."
    )


def test_the_internal_models_are_still_internal():
    """Pins the split rather than leaving it as folklore.

    If one of these starts being published, that is a new public contract and
    should be a deliberate decision, not a side effect of wiring a route.
    """
    schemas = _openapi_schemas()
    leaked = [n for n in INTERNAL_ONLY if n in schemas]
    assert not leaked, (
        f"{leaked} became part of the published OpenAPI schema. If deliberate, move "
        "them out of INTERNAL_ONLY; if not, they are a contract nobody signed up to."
    )


def test_the_published_field_sets_match_the_classes():
    """Catches a field lost or renamed in transit, which a name check would miss."""
    schemas = _openapi_schemas()
    for name in PUBLISHED:
        model = getattr(results, name)
        declared = set(model.model_fields)
        published = set(schemas[name].get("properties", {}))
        assert published == declared, (
            f"{name}: OpenAPI properties {sorted(published)} do not match declared "
            f"fields {sorted(declared)}"
        )
