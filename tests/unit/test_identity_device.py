"""RFC 8628 device grant: narrowing, single use, and revocation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from querygate.identity.device import (
    USER_CODE_ALPHABET,
    USER_CODE_LENGTH,
    DeviceGrantError,
    PublicDeviceGrant,
    PublicIssuedToken,
    approve_device_grant,
    deny_device_grant,
    get_device_grant_store,
    get_issued_token_store,
    new_user_code,
    normalize_user_code,
    redeem_device_code,
    start_device_authorization,
)
from querygate.identity.sessions import DEVICE_TOKEN_PREFIX, token_digest

pytestmark = pytest.mark.unit

_ALL = frozenset({"admin:config:read", "admin:config:write", "admin:metrics:read"})


class TestUserCode:
    def test_code_uses_only_unambiguous_characters(self):
        code = new_user_code().replace("-", "")
        assert len(code) == USER_CODE_LENGTH
        assert set(code) <= set(USER_CODE_ALPHABET)
        # Characters people misread aloud must not be in the alphabet at all.
        assert not (set("O0I1L5S2Z8B") & set(USER_CODE_ALPHABET))

    def test_normalization_accepts_what_a_person_actually_types(self):
        code = new_user_code()
        assert normalize_user_code(code.lower()) == code
        assert normalize_user_code(code.replace("-", "")) == code
        assert normalize_user_code(f" {code.lower()} ".replace("-", " ")) == code

    def test_codes_are_fresh(self):
        assert new_user_code() != new_user_code()


class TestGrantLifecycle:
    async def test_polling_before_approval_reports_pending(self):
        code, _ = await start_device_authorization(client_name="cli", requested_scopes=[])
        with pytest.raises(DeviceGrantError) as exc:
            await redeem_device_code(device_code=code, token_ttl_seconds=60)
        assert exc.value.error == "authorization_pending"

    async def test_polling_faster_than_the_interval_is_told_to_slow_down(self):
        code, _ = await start_device_authorization(client_name="cli", requested_scopes=[])
        with pytest.raises(DeviceGrantError):
            await redeem_device_code(device_code=code, token_ttl_seconds=60)
        with pytest.raises(DeviceGrantError) as exc:
            await redeem_device_code(device_code=code, token_ttl_seconds=60)
        assert exc.value.error == "slow_down"

    async def test_an_approved_grant_is_served_without_a_back_off_delay(self):
        code, grant = await start_device_authorization(client_name="cli", requested_scopes=[])
        with pytest.raises(DeviceGrantError):
            await redeem_device_code(device_code=code, token_ttl_seconds=60)
        await approve_device_grant(
            user_code=grant.user_code,
            approver_subject="alice",
            approver_scopes=_ALL,
            provider_id="entra",
        )
        # Immediately after approval — no slow_down, the human already decided.
        token, issued = await redeem_device_code(device_code=code, token_ttl_seconds=60)
        assert token.startswith(DEVICE_TOKEN_PREFIX)
        assert issued.subject == "alice"

    async def test_a_device_code_can_only_be_redeemed_once(self):
        code, grant = await start_device_authorization(client_name="cli", requested_scopes=[])
        await approve_device_grant(
            user_code=grant.user_code,
            approver_subject="alice",
            approver_scopes=_ALL,
            provider_id="entra",
        )
        await redeem_device_code(device_code=code, token_ttl_seconds=60)
        with pytest.raises(DeviceGrantError) as exc:
            await redeem_device_code(device_code=code, token_ttl_seconds=60)
        assert exc.value.error == "expired_token"

    async def test_a_denied_grant_yields_access_denied_and_then_nothing(self):
        code, grant = await start_device_authorization(client_name="cli", requested_scopes=[])
        await deny_device_grant(user_code=grant.user_code)
        with pytest.raises(DeviceGrantError) as exc:
            await redeem_device_code(device_code=code, token_ttl_seconds=60)
        assert exc.value.error == "access_denied"
        with pytest.raises(DeviceGrantError) as second:
            await redeem_device_code(device_code=code, token_ttl_seconds=60)
        assert second.value.error == "expired_token"

    async def test_an_unknown_user_code_cannot_be_approved(self):
        with pytest.raises(DeviceGrantError) as exc:
            await approve_device_grant(
                user_code="XXXX-XXXX",
                approver_subject="alice",
                approver_scopes=_ALL,
                provider_id="entra",
            )
        assert exc.value.error == "invalid_user_code"

    async def test_a_grant_cannot_be_approved_twice(self):
        _, grant = await start_device_authorization(client_name="cli", requested_scopes=[])
        await approve_device_grant(
            user_code=grant.user_code,
            approver_subject="alice",
            approver_scopes=_ALL,
            provider_id="entra",
        )
        with pytest.raises(DeviceGrantError) as exc:
            await approve_device_grant(
                user_code=grant.user_code,
                approver_subject="mallory",
                approver_scopes=_ALL,
                provider_id="entra",
            )
        assert exc.value.error == "already_resolved"

    async def test_an_expired_grant_is_gone(self):
        code, grant = await start_device_authorization(
            client_name="cli", requested_scopes=[], ttl_seconds=0.001
        )
        store = get_device_grant_store()
        await store.put(
            grant.model_copy(
                update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
            )
        )
        with pytest.raises(DeviceGrantError) as exc:
            await redeem_device_code(device_code=code, token_ttl_seconds=60)
        assert exc.value.error == "expired_token"


class TestNarrowing:
    async def test_a_grant_can_never_exceed_the_approvers_own_scopes(self):
        code, grant = await start_device_authorization(
            client_name="cli", requested_scopes=["admin:config:write", "admin:metrics:read"]
        )
        approved = await approve_device_grant(
            user_code=grant.user_code,
            approver_subject="alice",
            approver_scopes=frozenset({"admin:metrics:read"}),
            provider_id="entra",
        )
        assert approved.granted_scopes == ["admin:metrics:read"]
        _, issued = await redeem_device_code(device_code=code, token_ttl_seconds=60)
        assert issued.scopes == ["admin:metrics:read"]

    async def test_asking_for_nothing_inherits_the_approvers_scopes(self):
        code, grant = await start_device_authorization(client_name="cli", requested_scopes=[])
        approved = await approve_device_grant(
            user_code=grant.user_code,
            approver_subject="alice",
            approver_scopes=frozenset({"admin:metrics:read"}),
            provider_id="entra",
        )
        assert approved.granted_scopes == ["admin:metrics:read"]

    async def test_an_approver_with_no_scopes_grants_no_scopes(self):
        _, grant = await start_device_authorization(
            client_name="cli", requested_scopes=["admin:config:write"]
        )
        approved = await approve_device_grant(
            user_code=grant.user_code,
            approver_subject="alice",
            approver_scopes=frozenset(),
            provider_id="entra",
        )
        assert approved.granted_scopes == []


class TestIssuedTokens:
    async def test_the_store_holds_only_a_digest_of_the_token(self):
        code, grant = await start_device_authorization(client_name="cli", requested_scopes=[])
        await approve_device_grant(
            user_code=grant.user_code,
            approver_subject="alice",
            approver_scopes=_ALL,
            provider_id="entra",
        )
        token, issued = await redeem_device_code(device_code=code, token_ttl_seconds=60)
        assert issued.token_digest == token_digest(token)
        assert token not in issued.model_dump_json()

    async def test_revoking_a_subject_removes_every_token_they_hold(self):
        for _ in range(2):
            code, grant = await start_device_authorization(client_name="cli", requested_scopes=[])
            await approve_device_grant(
                user_code=grant.user_code,
                approver_subject="alice",
                approver_scopes=_ALL,
                provider_id="entra",
            )
            await redeem_device_code(device_code=code, token_ttl_seconds=60)
        store = get_issued_token_store()
        assert len(await store.list_for_subject("alice")) == 2
        assert await store.delete_for_subject("alice") == 2
        assert await store.list_for_subject("alice") == []

    async def test_an_expired_token_stops_resolving(self):
        code, grant = await start_device_authorization(client_name="cli", requested_scopes=[])
        await approve_device_grant(
            user_code=grant.user_code,
            approver_subject="alice",
            approver_scopes=_ALL,
            provider_id="entra",
        )
        token, _ = await redeem_device_code(device_code=code, token_ttl_seconds=0)
        assert await get_issued_token_store().get(token_digest(token)) is None

    def test_public_models_carry_no_credential_field(self):
        assert "device_code_digest" not in PublicDeviceGrant.model_fields
        assert "approver_claims" not in PublicDeviceGrant.model_fields
        assert "approver_claims" not in PublicIssuedToken.model_fields
