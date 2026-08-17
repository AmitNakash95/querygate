"""Unit tests for query templates (querygate/templates/) — model, store, and
parameter binding (TODO.md item 48)."""

from __future__ import annotations

import pytest

from querygate.core.config import AppConfig
from querygate.core.exceptions import QueryValidationError
from querygate.query_ast.models import StructuredQuery
from querygate.templates.binding import (
    bind_template,
    dummy_bound_query,
    validate_template_structure,
)
from querygate.templates.loader import TemplateStore
from querygate.templates.models import (
    PublicQueryTemplate,
    QueryTemplate,
    QueryTemplateFile,
    TemplateParameter,
)


def _template(**overrides) -> QueryTemplate:
    defaults = dict(
        id="orders_for_customer",
        connection="demo",
        description="Orders for a customer.",
        parameters=[
            {"name": "customer_id", "type": "integer", "required": True},
            {
                "name": "limit",
                "type": "integer",
                "required": False,
                "default": 10,
                "min": 1,
                "max": 100,
            },
        ],
        query={
            "from": "orders",
            "select": ["orders.id", "orders.status"],
            "where": {"col": "orders.customer_id", "op": "eq", "value": {"param": "customer_id"}},
            "limit": {"param": "limit"},
        },
    )
    defaults.update(overrides)
    return QueryTemplate.model_validate(defaults)


# ── model validation ─────────────────────────────────────────────────────────


def test_template_id_must_be_identifier():
    with pytest.raises(ValueError):
        _template(id="not a valid id!")


def test_duplicate_parameter_names_rejected():
    with pytest.raises(ValueError, match="duplicate parameter"):
        _template(
            parameters=[
                {"name": "x", "type": "integer"},
                {"name": "x", "type": "string"},
            ]
        )


def test_min_max_only_valid_for_numeric():
    with pytest.raises(ValueError, match="min/max only valid"):
        QueryTemplate.model_validate(
            {
                "id": "t",
                "connection": "demo",
                "parameters": [{"name": "s", "type": "string", "min": 1}],
                "query": {"from": "orders", "select": ["orders.id"]},
            }
        )


# ── slot self-consistency (allowed_values / default must match the type) ──────
def test_allowed_values_must_match_declared_type():
    # type integer but the enum is strings — the classic contradiction: the
    # template would deploy but no integer a caller supplies could ever match.
    with pytest.raises(ValueError, match="allowed value 'pending' must be an integer"):
        TemplateParameter(name="status", type="integer", allowed_values=["pending", "completed"])


def test_allowed_value_out_of_numeric_bounds_rejected():
    with pytest.raises(ValueError, match="allowed value 99 is above max 10"):
        TemplateParameter(name="n", type="integer", max=10, allowed_values=[1, 5, 99])


def test_default_must_satisfy_its_own_slot_type():
    with pytest.raises(ValueError, match="default 'hello' must be an integer"):
        TemplateParameter(name="n", type="integer", required=False, default="hello")


def test_default_must_be_in_allowed_values_when_set():
    with pytest.raises(ValueError, match="default 3 is not in allowed_values"):
        TemplateParameter(
            name="k", type="integer", required=False, default=3, allowed_values=[1, 2]
        )


def test_string_default_over_max_length_rejected():
    with pytest.raises(ValueError, match="default 'toolong' exceeds max length 2"):
        TemplateParameter(name="x", type="string", max_length=2, required=False, default="toolong")


def test_list_default_elements_validated_against_slot():
    with pytest.raises(ValueError, match="default 'x' must be an integer"):
        TemplateParameter(
            name="ids", type="integer", is_list=True, required=False, default=[1, "x"]
        )


def test_self_consistent_slot_is_accepted():
    p = TemplateParameter(
        name="ok", type="integer", required=False, default=2, allowed_values=[1, 2, 3]
    )
    assert p.default == 2


def test_documented_TEMPLATES_FILE_env_var_populates_template_file(monkeypatch):
    """Regression: the documented env var is the plural TEMPLATES_FILE
    (.env.example, examples/templates.example.yaml, CLI help). Without the
    field's AliasChoices it only bound the singular TEMPLATE_FILE, so the
    documented var was silently ignored and no templates ever loaded."""
    monkeypatch.setenv("TEMPLATES_FILE", "examples/templates.example.yaml")
    monkeypatch.delenv("TEMPLATE_FILE", raising=False)
    assert AppConfig().template_file == "examples/templates.example.yaml"


def test_template_file_rejects_duplicate_ids():
    with pytest.raises(ValueError, match="duplicate template id"):
        QueryTemplateFile.model_validate(
            {
                "templates": [
                    {"id": "t", "connection": "demo", "query": {"from": "o", "select": ["o.a"]}},
                    {"id": "T", "connection": "demo", "query": {"from": "o", "select": ["o.a"]}},
                ]
            }
        )


# ── binding: happy path ──────────────────────────────────────────────────────


def test_bind_substitutes_values_and_defaults():
    query = bind_template(_template(), {"customer_id": 42})
    assert isinstance(query, StructuredQuery)
    assert query.limit == 10  # from default
    # The bound value landed in the predicate.
    assert query.where.value == 42


def test_bind_list_parameter():
    template = _template(
        parameters=[{"name": "statuses", "type": "string", "is_list": True, "required": True}],
        query={
            "from": "orders",
            "select": ["orders.id"],
            "where": {"col": "orders.status", "op": "in", "value": {"param": "statuses"}},
        },
    )
    query = bind_template(template, {"statuses": ["a", "b"]})
    assert query.where.value == ["a", "b"]


# ── binding: rejections ──────────────────────────────────────────────────────


def test_unknown_parameter_rejected():
    with pytest.raises(QueryValidationError, match="unknown parameter"):
        bind_template(_template(), {"customer_id": 1, "bogus": 2})


def test_missing_required_parameter_rejected():
    with pytest.raises(QueryValidationError, match="missing required"):
        bind_template(_template(), {})


def test_wrong_type_rejected():
    with pytest.raises(QueryValidationError, match="must be an integer"):
        bind_template(_template(), {"customer_id": "not-an-int"})


def test_bool_is_not_an_integer():
    with pytest.raises(QueryValidationError):
        bind_template(_template(), {"customer_id": True})


def test_numeric_bounds_enforced():
    with pytest.raises(QueryValidationError, match="above max"):
        bind_template(_template(), {"customer_id": 1, "limit": 9999})


def test_allowed_values_enforced():
    template = _template(
        parameters=[{"name": "s", "type": "string", "allowed_values": ["ok"]}],
        query={
            "from": "orders",
            "select": ["orders.id"],
            "where": {"col": "orders.status", "op": "eq", "value": {"param": "s"}},
        },
    )
    with pytest.raises(QueryValidationError, match="not an allowed value"):
        bind_template(template, {"s": "nope"})


def test_max_length_enforced():
    template = _template(
        parameters=[{"name": "s", "type": "string", "max_length": 3}],
        query={
            "from": "orders",
            "select": ["orders.id"],
            "where": {"col": "orders.status", "op": "eq", "value": {"param": "s"}},
        },
    )
    with pytest.raises(QueryValidationError, match="max length"):
        bind_template(template, {"s": "toolong"})


def test_empty_list_rejected():
    template = _template(
        parameters=[{"name": "statuses", "type": "string", "is_list": True}],
        query={
            "from": "orders",
            "select": ["orders.id"],
            "where": {"col": "orders.status", "op": "in", "value": {"param": "statuses"}},
        },
    )
    with pytest.raises(QueryValidationError, match="non-empty list"):
        bind_template(template, {"statuses": []})


def test_binding_that_produces_invalid_query_is_rejected():
    # A limit of 0 violates StructuredQuery's ge=1 — caught after substitution.
    template = _template(
        parameters=[{"name": "n", "type": "integer"}],
        query={"from": "orders", "select": ["orders.id"], "limit": {"param": "n"}},
    )
    with pytest.raises(QueryValidationError, match="invalid query"):
        bind_template(template, {"n": 0})


def test_bound_value_never_appears_in_query_shape_identifiers():
    # A predicate value is bound as data; the AST structure references only
    # identifiers/operators, so the pipeline's shape normalization won't leak it.
    query = bind_template(_template(), {"customer_id": 987654321})
    # value lives in .value (a bind param downstream), never in an identifier.
    assert query.from_table == "orders"
    assert query.where.col == "orders.customer_id"


# ── store ────────────────────────────────────────────────────────────────────


def test_store_lookup_is_case_insensitive_and_lists_sorted():
    store = TemplateStore([_template(id="b_tmpl"), _template(id="a_tmpl")])
    assert store.get("A_TMPL") is not None
    assert store.template_ids() == ["a_tmpl", "b_tmpl"]
    assert store.connection_ids() == ["demo"]
    assert store.get("missing") is None


def test_validate_template_structure_accepts_a_valid_skeleton():
    assert validate_template_structure(_template()) is None


def test_validate_template_structure_rejects_malformed_skeleton():
    bad = QueryTemplate.model_validate(
        {
            "id": "bad",
            "connection": "demo",
            "query": {"from": "orders", "select": []},  # empty select is invalid
        }
    )
    assert validate_template_structure(bad) is not None


def test_validate_template_structure_rejects_type_position_mismatch():
    # A string parameter placed where an integer is required (limit) — caught
    # at deploy time, not only at first invocation.
    bad = QueryTemplate.model_validate(
        {
            "id": "bad2",
            "connection": "demo",
            "parameters": [{"name": "n", "type": "string"}],
            "query": {"from": "orders", "select": ["orders.id"], "limit": {"param": "n"}},
        }
    )
    assert validate_template_structure(bad) is not None


def test_public_projection_excludes_query_skeleton():
    public = PublicQueryTemplate.from_template(_template())
    dumped = public.model_dump()
    assert "query" not in dumped  # the AST skeleton is not exposed to agents
    assert dumped["id"] == "orders_for_customer"
    assert [p["name"] for p in dumped["parameters"]] == ["customer_id", "limit"]


def test_a_between_template_passes_the_dry_run_structure_check():
    """`dummy_bound_query` bound a ONE-element list for every list slot, which
    satisfies `in`/`not_in` but fails `between`'s exact `[low, high]` rule — so a
    perfectly valid BETWEEN template was rejected by `querygate config check`,
    and the failure came from this dry-run binder rather than from the template.

    It surfaced through item 195's promote-then-check workflow (draft a template
    from an observed shape, check it, then install it), where the drafted
    template is checked before an operator commits it. Two elements satisfy every
    list-taking operator the AST has, so the arity does not have to be inferred
    from whichever operator the slot happens to sit under.
    """
    template = QueryTemplate(
        id="orders_in_range",
        connection="demo",
        parameters=[TemplateParameter(name="span", type="integer", is_list=True)],
        query={
            "from": "orders",
            "select": ["orders.id"],
            "where": {"col": "orders.total", "op": "between", "value": {"param": "span"}},
            "limit": 5,
        },
    )
    assert validate_template_structure(template) is None
    bound = dummy_bound_query(template)
    assert isinstance(bound.where.value, list) and len(bound.where.value) == 2


def test_an_in_list_template_still_passes_the_dry_run_check():
    """The same two-element dummy must not break the operator it was already
    working for — `in` takes any non-empty list, so two is fine.
    """
    template = QueryTemplate(
        id="orders_in_statuses",
        connection="demo",
        parameters=[TemplateParameter(name="statuses", type="string", is_list=True)],
        query={
            "from": "orders",
            "select": ["orders.id"],
            "where": {"col": "orders.status", "op": "in", "value": {"param": "statuses"}},
            "limit": 5,
        },
    )
    assert validate_template_structure(template) is None
