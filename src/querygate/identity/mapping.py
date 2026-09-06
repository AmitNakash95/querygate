"""Claim→scope mapping: how an IdP's groups become QueryGate authority.

An identity provider knows *who* someone is. It does not know what QueryGate
scopes mean, and QueryGate does not know what a customer's group GUIDs mean.
This module is the one place those two vocabularies meet, and it is
deliberately a **file**, not code: an operator writes the mapping in
identity.yaml, it is validated against `core/scopes.ALL_SCOPES` and
`ROLE_BUNDLES` at load, it hot-reloads with the rest of the config, and it is
diffable and reviewable like any other governance artifact.

Two properties are non-negotiable here:

* **Deny by default.** Matching no rule yields the empty scope set, not a
  default role. Signing in successfully is authentication; it grants nothing on
  its own.
* **Grant-only, union semantics.** A rule can add scopes; no rule can remove
  them, and rule order is irrelevant. That makes the outcome of a mapping a
  pure function of (provider, claims) — the property `explain()` relies on to
  answer "why does this person have this scope?" without replaying a flow.

The mapper is applied to browser SSO logins *and* to bearer JWTs verified by
`core/jwt_auth.JwtAuthenticator`, so one human gets the same authority whether
they arrive through the admin UI or an agent presents their token.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml

from querygate.identity.claims import claim_values
from querygate.identity.models import ClaimRule

# Bound on rules considered for one decision. A mapping file this large is a
# misconfiguration; evaluating it per request is not something a caller should
# be able to make expensive.
MAX_RULES = 2048


class IdentityMappingStore:
    """Resolves a caller's claims to the QueryGate scopes they hold."""

    def __init__(self, rules: List[ClaimRule]) -> None:
        if len(rules) > MAX_RULES:
            raise ValueError(
                f"identity mapping has {len(rules)} rules; the maximum is {MAX_RULES}."
            )
        self._rules = list(rules)
        # provider id (or "*") -> rules, so a decision only walks the rules that
        # could possibly apply to the provider the caller signed in through.
        self._by_provider: Dict[str, List[ClaimRule]] = {}
        for rule in self._rules:
            self._by_provider.setdefault(rule.provider, []).append(rule)

    @classmethod
    def from_rules(cls, raw_rules: List[dict]) -> "IdentityMappingStore":
        return cls([ClaimRule.model_validate(entry or {}) for entry in raw_rules])

    @classmethod
    def empty(cls) -> "IdentityMappingStore":
        return cls([])

    @property
    def rules(self) -> Tuple[ClaimRule, ...]:
        return tuple(self._rules)

    def _candidate_rules(self, provider_id: str) -> List[ClaimRule]:
        return self._by_provider.get(provider_id, []) + self._by_provider.get("*", [])

    def scopes_for(self, provider_id: str, claims: Mapping[str, Any]) -> frozenset[str]:
        """The union of every matching rule's grant. Empty when nothing matches."""
        granted: set[str] = set()
        for rule in self._candidate_rules(provider_id):
            if self._matched_value(rule, claims) is not None:
                granted.update(rule.granted_scopes)
        return frozenset(granted)

    def explain(self, provider_id: str, claims: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """Which rules matched and what each contributed.

        Powers the admin "why does this person hold this scope?" answer.
        Returns rule *identity* (claim name, matched value, granted scopes) —
        never the caller's full claim set, so the explanation is safe to show an
        operator who is not entitled to see every attribute the IdP asserted
        about that human.
        """
        explained: List[Dict[str, Any]] = []
        for rule in self._candidate_rules(provider_id):
            matched = self._matched_value(rule, claims)
            if matched is not None:
                explained.append(
                    {
                        "claim": rule.claim,
                        "matched_value": matched,
                        "provider": rule.provider,
                        "granted_scopes": sorted(rule.granted_scopes),
                        "description": rule.description,
                    }
                )
        return explained

    @staticmethod
    def _matched_value(rule: ClaimRule, claims: Mapping[str, Any]) -> Optional[str]:
        present = claim_values(claims, rule.claim)
        if not present:
            return None
        held = set(present)
        for candidate in rule.match_values:
            if candidate in held:
                return candidate
        return None


def load_mapping_rules(path: str) -> List[dict]:
    """Read just the `mapping.rules` list out of an identity file."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Identity file not found: {path}")
    raw = yaml.safe_load(file_path.read_text()) or {}
    return list((raw.get("mapping") or {}).get("rules") or [])
