"""The development provider's own mechanics, tested with the real verifier.

The end-to-end flow test cannot supply the correct PKCE verifier — QueryGate
keeps it server-side — so over HTTP a rejected replay could be either
single-use *or* PKCE, and a mutation that removed single-use went uncaught.
These call the provider directly, where the verifier is in hand and each check
can be isolated.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from querygate.identity.config_store import IdentityConfigStore
from querygate.identity.dev_idp import (
    UNMAPPED_PERSONA_SUB,
    DevIdentityProvider,
    DevIdpError,
    DevPersona,
    derive_personas,
    nest_claim,
)

pytestmark = pytest.mark.unit

ISSUER = "http://127.0.0.1:8000/dev-idp"
REDIRECT = "http://127.0.0.1:8000/api/v1/auth/sso/callback"
VERIFIER = "a-verifier-long-enough-to-be-realistic-0123456789"
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
)


def _provider(**overrides) -> DevIdentityProvider:
    personas = overrides.pop(
        "personas",
        [
            DevPersona(
                sub="dev-1",
                name="Dev One",
                email="dev-1@localhost",
                claims={"groups": ["platform"]},
            )
        ],
    )
    return DevIdentityProvider(issuer=ISSUER, personas=personas)


def _issue(provider: DevIdentityProvider, nonce: str = "the-nonce") -> str:
    return provider.issue_code(
        persona=provider.personas[0],
        code_challenge=CHALLENGE,
        nonce=nonce,
        redirect_uri=REDIRECT,
    )


class TestCodeExchange:
    def test_a_correct_exchange_returns_an_id_token(self):
        provider = _provider()
        token = provider.redeem_code(
            code=_issue(provider), code_verifier=VERIFIER, redirect_uri=REDIRECT
        )
        assert token.count(".") == 2

    def test_a_code_can_be_redeemed_exactly_once(self):
        """With the *correct* verifier, so only single-use can reject the replay.

        This is the check the HTTP-level test cannot isolate: there, a second
        exchange fails PKCE regardless, which masked a mutation that turned the
        consuming `pop` into a `get`.
        """
        provider = _provider()
        code = _issue(provider)
        assert provider.redeem_code(code=code, code_verifier=VERIFIER, redirect_uri=REDIRECT)
        with pytest.raises(DevIdpError) as exc:
            provider.redeem_code(code=code, code_verifier=VERIFIER, redirect_uri=REDIRECT)
        assert exc.value.error == "invalid_grant"

    def test_a_wrong_verifier_is_refused(self):
        provider = _provider()
        with pytest.raises(DevIdpError, match="PKCE verification failed"):
            provider.redeem_code(
                code=_issue(provider), code_verifier="not-the-verifier", redirect_uri=REDIRECT
            )

    def test_a_mismatched_redirect_uri_is_refused(self):
        provider = _provider()
        with pytest.raises(DevIdpError, match="redirect_uri"):
            provider.redeem_code(
                code=_issue(provider),
                code_verifier=VERIFIER,
                redirect_uri="http://127.0.0.1:8000/somewhere/else",
            )

    def test_an_unknown_code_is_refused(self):
        with pytest.raises(DevIdpError, match="Unknown or expired"):
            _provider().redeem_code(
                code="never-issued", code_verifier=VERIFIER, redirect_uri=REDIRECT
            )


class TestIdTokenContents:
    def test_the_token_carries_the_nonce_and_the_personas_claims(self):
        import jwt

        provider = _provider()
        token = provider.redeem_code(
            code=_issue(provider, nonce="round-trip-nonce"),
            code_verifier=VERIFIER,
            redirect_uri=REDIRECT,
        )
        claims = jwt.decode(
            token, options={"verify_signature": False}, audience="querygate-dev-idp"
        )
        assert claims["nonce"] == "round-trip-nonce"
        assert claims["iss"] == ISSUER
        assert claims["sub"] == "dev-1"
        assert claims["groups"] == ["platform"]
        # Marked, so an audit reader can tell a development sign-in from a real one.
        assert claims["querygate_dev_identity"] is True


class TestPersonaDerivation:
    def _store(self) -> IdentityConfigStore:
        return IdentityConfigStore.from_dict(
            {
                "sso": {"base_url": "http://127.0.0.1:8000"},
                "providers": [{"id": "dev", "kind": "dev"}],
                "mapping": {
                    "rules": [
                        {
                            "provider": "dev",
                            "claim": "groups",
                            "equals": "platform",
                            "grant_roles": ["Operator"],
                            "description": "Platform on-call",
                        },
                        {
                            "provider": "*",
                            "claim": "realm_access.roles",
                            "any_of": ["qg-approver", "qg-admin"],
                            "grant_scopes": ["query:approve"],
                        },
                    ]
                },
            }
        )

    def test_one_persona_per_rule_value_plus_an_unmapped_one(self):
        personas = derive_personas(self._store(), "dev")
        assert [p.sub for p in personas] == ["dev-1", "dev-2", "dev-3", UNMAPPED_PERSONA_SUB]

    def test_a_dotted_claim_path_becomes_a_nested_object(self):
        """A flat key named "realm_access.roles" would match no rule at all.

        `identity/claims.py` walks nesting; the persona has to be shaped the way
        a real IdP would send it, or the rule that inspired the persona would not
        fire on it. Found by the end-to-end flow test, pinned here.
        """
        personas = derive_personas(self._store(), "dev")
        nested = next(p for p in personas if p.sub == "dev-2")
        assert nested.claims == {"realm_access": {"roles": ["qg-approver"]}}
        assert "realm_access.roles" not in nested.claims

    def test_the_unmapped_persona_matches_no_rule(self):
        store = self._store()
        unmapped = next(p for p in derive_personas(store, "dev") if p.sub == UNMAPPED_PERSONA_SUB)
        assert store.mapping.scopes_for("dev", unmapped.claims) == frozenset()

    def test_every_derived_persona_actually_earns_its_rule(self):
        """The point of the derivation: each button demonstrates a real grant."""
        store = self._store()
        for persona in derive_personas(store, "dev"):
            if persona.sub == UNMAPPED_PERSONA_SUB:
                continue
            assert store.mapping.scopes_for("dev", persona.claims), persona.sub

    def test_a_rule_for_another_provider_contributes_no_persona(self):
        store = IdentityConfigStore.from_dict(
            {
                "sso": {"base_url": "http://127.0.0.1:8000"},
                "providers": [
                    {"id": "dev", "kind": "dev", "subject_prefix": "dev:"},
                    {
                        "id": "entra",
                        "preset": "entra_id",
                        "tenant": "t",
                        "client_id": "c",
                        "subject_prefix": "entra:",
                    },
                ],
                "mapping": {
                    "rules": [
                        {
                            "provider": "entra",
                            "claim": "groups",
                            "equals": "only-entra",
                            "grant_scopes": ["query:approve"],
                        }
                    ]
                },
            }
        )
        assert [p.sub for p in derive_personas(store, "dev")] == [UNMAPPED_PERSONA_SUB]


class TestNestClaim:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("groups", {"groups": ["v"]}),
            ("realm_access.roles", {"realm_access": {"roles": ["v"]}}),
            ("a.b.c", {"a": {"b": {"c": ["v"]}}}),
            ("", {}),
        ],
    )
    def test_paths_expand_as_an_idp_would_send_them(self, path, expected):
        assert nest_claim(path, ["v"]) == expected

    def test_a_nested_path_is_readable_by_the_claim_walker(self):
        from querygate.identity.claims import claim_values

        built = nest_claim("realm_access.roles", ["qg-admin"])
        assert claim_values(built, "realm_access.roles") == ["qg-admin"]


class TestDiscoveryDocument:
    def test_it_describes_only_what_it_actually_supports(self):
        document = _provider().discovery_document()
        assert document["code_challenge_methods_supported"] == ["S256"]
        assert document["id_token_signing_alg_values_supported"] == ["RS256"]
        assert document["response_types_supported"] == ["code"]
        assert document["issuer"] == ISSUER

    def test_it_issues_no_access_token(self):
        """QueryGate reads identity from the ID token and never calls userinfo,
        so minting an access token would be a credential with no purpose."""
        import inspect

        source = inspect.getsource(DevIdentityProvider)
        assert "access_token" not in source
