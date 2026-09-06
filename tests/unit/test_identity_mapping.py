"""Claim→scope mapping: deny-by-default, grant-only, and validated at load."""

from __future__ import annotations

import pytest

from querygate.core.scopes import ALL_SCOPES
from querygate.identity.claims import MAX_CLAIM_PATH_DEPTH, claim_scalar, claim_values
from querygate.identity.config_store import IdentityConfigStore
from querygate.identity.mapping import MAX_RULES, IdentityMappingStore
from querygate.identity.models import ClaimRule

pytestmark = pytest.mark.unit


class TestClaimReading:
    def test_flat_list_claim(self):
        assert claim_values({"groups": ["a", "b"]}, "groups") == ["a", "b"]

    def test_dotted_path_reaches_keycloak_realm_roles(self):
        claims = {"realm_access": {"roles": ["admin", "user"]}}
        assert claim_values(claims, "realm_access.roles") == ["admin", "user"]

    def test_scalar_claim_becomes_one_element(self):
        assert claim_values({"groups": "solo"}, "groups") == ["solo"]

    def test_unexpected_shape_yields_nothing_rather_than_matching_loosely(self):
        # An object where a list was expected must contribute NO values: a rule
        # against it then fails closed instead of matching something incidental.
        assert claim_values({"groups": {"nested": "value"}}, "groups") == []

    def test_booleans_never_stringify_into_a_match_target(self):
        assert claim_values({"groups": True}, "groups") == []
        assert claim_values({"groups": [True, "real"]}, "groups") == ["real"]

    def test_path_deeper_than_the_bound_is_refused(self):
        path = ".".join(["a"] * (MAX_CLAIM_PATH_DEPTH + 1))
        nested: dict = {"leaf": "x"}
        for _ in range(MAX_CLAIM_PATH_DEPTH + 1):
            nested = {"a": nested}
        assert claim_values(nested, path) == []

    def test_scalar_helper_refuses_to_pick_from_a_list(self):
        # Which human this is must never depend on the IdP's array ordering.
        assert claim_scalar({"sub": ["one", "two"]}, "sub") is None
        assert claim_scalar({"sub": "one"}, "sub") == "one"


class TestMapping:
    def test_no_matching_rule_grants_nothing(self):
        store = IdentityMappingStore.from_rules(
            [
                {
                    "provider": "idp",
                    "claim": "groups",
                    "equals": "admins",
                    "grant_roles": ["Operator"],
                }
            ]
        )
        assert store.scopes_for("idp", {"groups": ["everyone-else"]}) == frozenset()

    def test_empty_mapping_grants_nothing_to_anyone(self):
        assert IdentityMappingStore.empty().scopes_for("idp", {"groups": ["admins"]}) == frozenset()

    def test_rules_union_and_order_is_irrelevant(self):
        rules = [
            {"claim": "groups", "equals": "a", "grant_scopes": ["admin:config:read"]},
            {"claim": "groups", "equals": "b", "grant_scopes": ["admin:metrics:read"]},
        ]
        forward = IdentityMappingStore.from_rules(rules)
        reverse = IdentityMappingStore.from_rules(list(reversed(rules)))
        claims = {"groups": ["a", "b"]}
        assert forward.scopes_for("x", claims) == reverse.scopes_for("x", claims)
        assert forward.scopes_for("x", claims) == frozenset(
            {"admin:config:read", "admin:metrics:read"}
        )

    def test_a_rule_for_another_provider_never_applies(self):
        store = IdentityMappingStore.from_rules(
            [
                {
                    "provider": "okta",
                    "claim": "groups",
                    "equals": "a",
                    "grant_scopes": ["query:approve"],
                }
            ]
        )
        assert store.scopes_for("entra", {"groups": ["a"]}) == frozenset()
        assert store.scopes_for("okta", {"groups": ["a"]}) == frozenset({"query:approve"})

    def test_wildcard_provider_applies_everywhere(self):
        store = IdentityMappingStore.from_rules(
            [{"provider": "*", "claim": "groups", "equals": "a", "grant_scopes": ["query:approve"]}]
        )
        assert store.scopes_for("anything", {"groups": ["a"]}) == frozenset({"query:approve"})

    def test_any_of_matches_any_single_value(self):
        store = IdentityMappingStore.from_rules(
            [{"claim": "roles", "any_of": ["x", "y"], "grant_scopes": ["admin:metrics:read"]}]
        )
        assert store.scopes_for("p", {"roles": ["y"]}) == frozenset({"admin:metrics:read"})
        assert store.scopes_for("p", {"roles": ["z"]}) == frozenset()

    def test_explain_reports_only_rule_identity_not_the_caller_claims(self):
        store = IdentityMappingStore.from_rules(
            [{"claim": "groups", "equals": "a", "grant_scopes": ["admin:metrics:read"]}]
        )
        explained = store.explain("p", {"groups": ["a"], "ssn": "secret-attribute"})
        assert explained == [
            {
                "claim": "groups",
                "matched_value": "a",
                "provider": "*",
                "granted_scopes": ["admin:metrics:read"],
                "description": "",
            }
        ]
        assert "secret-attribute" not in str(explained)

    def test_rule_count_is_bounded(self):
        rules = [
            {"claim": "g", "equals": str(i), "grant_scopes": ["admin:metrics:read"]}
            for i in range(MAX_RULES + 1)
        ]
        with pytest.raises(ValueError, match="maximum"):
            IdentityMappingStore.from_rules(rules)


class TestRuleValidation:
    def test_unknown_scope_is_refused_at_load(self):
        with pytest.raises(ValueError, match="Unknown QueryGate scope"):
            ClaimRule.model_validate(
                {"claim": "groups", "equals": "a", "grant_scopes": ["admin:not-a-real-scope"]}
            )

    def test_unknown_role_bundle_is_refused_at_load(self):
        with pytest.raises(ValueError, match="Unknown role bundle"):
            ClaimRule.model_validate(
                {"claim": "groups", "equals": "a", "grant_roles": ["Supreme Leader"]}
            )

    def test_a_rule_must_grant_something(self):
        with pytest.raises(ValueError, match="grants nothing"):
            ClaimRule.model_validate({"claim": "groups", "equals": "a"})

    def test_exactly_one_of_equals_or_any_of(self):
        with pytest.raises(ValueError, match="exactly one"):
            ClaimRule.model_validate(
                {"claim": "g", "equals": "a", "any_of": ["b"], "grant_scopes": ["query:approve"]}
            )
        with pytest.raises(ValueError, match="exactly one"):
            ClaimRule.model_validate({"claim": "g", "grant_scopes": ["query:approve"]})

    def test_every_granted_role_resolves_to_real_scopes(self):
        rule = ClaimRule.model_validate(
            {"claim": "g", "equals": "a", "grant_roles": ["Identity Administrator"]}
        )
        assert rule.granted_scopes <= set(ALL_SCOPES)
        assert rule.granted_scopes == {"admin:identity:read", "admin:identity:write"}

    def test_mapping_referencing_an_unconfigured_provider_is_refused(self):
        with pytest.raises(ValueError, match="unconfigured provider"):
            IdentityConfigStore.from_dict(
                {
                    "providers": [
                        {"id": "entra", "preset": "entra_id", "tenant": "t", "client_id": "c"}
                    ],
                    "mapping": {
                        "rules": [
                            {
                                "provider": "typo",
                                "claim": "groups",
                                "equals": "a",
                                "grant_scopes": ["query:approve"],
                            }
                        ]
                    },
                }
            )
