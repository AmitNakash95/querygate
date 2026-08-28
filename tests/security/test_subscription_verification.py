"""Every verification rule, planted individually.

`verify.py` is the only thing standing between a signed entitlement and a forged
one, so each rule gets its own test with its own typed failure reason. A single
"a bad document is rejected" test would pass with nine of the ten rules deleted.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from querygate.subscription.verify import (
    ENTITLEMENT_DOMAIN,
    MANIFEST_DOMAIN,
    EntitlementVerificationError,
    TrustedKey,
    VerificationFailure,
    load_manifest,
    verify_entitlement,
)

pytestmark = [pytest.mark.security, pytest.mark.unit]

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
DEPLOYMENT = "dep_test"


def _payload(**overrides) -> dict:
    body = {
        "schema_version": 1,
        "org_id": "org_test",
        "deployment_id": DEPLOYMENT,
        "serial": 5,
        "issued_at": (NOW - timedelta(days=1)).isoformat(),
        "expires_at": (NOW + timedelta(days=30)).isoformat(),
        "grace_expires_at": (NOW + timedelta(days=44)).isoformat(),
        "enforcement": "enforce",
        "plan": "team",
        "max_connections": 10,
        "max_seats": 25,
        "renewal_url": "https://example.invalid/renew",
    }
    body.update(overrides)
    return body


def _envelope(key: Ed25519PrivateKey, payload: dict, *, key_id: str = "k1", domain=None) -> bytes:
    raw = json.dumps(payload).encode("utf-8")
    signature = key.sign((domain or ENTITLEMENT_DOMAIN) + b"." + raw)
    return json.dumps(
        {
            "key_id": key_id,
            "payload": base64.b64encode(raw).decode(),
            "signature": base64.b64encode(signature).decode(),
        }
    ).encode("utf-8")


@pytest.fixture
def signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def trusted(signing_key) -> dict[str, TrustedKey]:
    return {"k1": TrustedKey(key_id="k1", public_key=signing_key.public_key())}


def _verify(raw: bytes, trusted, **kwargs):
    return verify_entitlement(
        raw, trusted_keys=trusted, deployment_id=DEPLOYMENT, now=NOW, **kwargs
    )


def _reason(raw: bytes, trusted, **kwargs) -> VerificationFailure:
    with pytest.raises(EntitlementVerificationError) as caught:
        _verify(raw, trusted, **kwargs)
    return caught.value.reason


# --- the happy path, so every rejection below means something -----------------


def test_a_well_formed_entitlement_verifies(signing_key, trusted):
    entitlement = _verify(_envelope(signing_key, _payload()), trusted)
    assert entitlement.org_id == "org_test"
    assert entitlement.serial == 5
    assert entitlement.enforcement.value == "enforce"


# --- one test per rule --------------------------------------------------------


def test_a_signature_from_an_untrusted_key_is_refused(trusted):
    other = Ed25519PrivateKey.generate()
    assert _reason(_envelope(other, _payload()), trusted) is VerificationFailure.BAD_SIGNATURE


def test_an_unknown_key_id_is_refused(signing_key, trusted):
    raw = _envelope(signing_key, _payload(), key_id="rotated-away")
    assert _reason(raw, trusted) is VerificationFailure.UNKNOWN_KEY


def test_a_tampered_payload_is_refused(signing_key, trusted):
    """The signature covers the exact decoded bytes, so editing the payload
    after signing invalidates it — which is the whole point of verifying before
    parsing rather than after."""
    envelope = json.loads(_envelope(signing_key, _payload()))
    tampered = _payload(expires_at=(NOW + timedelta(days=3650)).isoformat())
    envelope["payload"] = base64.b64encode(json.dumps(tampered).encode()).decode()
    raw = json.dumps(envelope).encode()
    assert _reason(raw, trusted) is VerificationFailure.BAD_SIGNATURE


def test_a_signature_minted_for_another_domain_is_refused(signing_key, trusted):
    """Domain separation: a manifest signature must not verify as an entitlement."""
    raw = _envelope(signing_key, _payload(), domain=MANIFEST_DOMAIN)
    assert _reason(raw, trusted) is VerificationFailure.BAD_SIGNATURE


def test_an_entitlement_for_another_deployment_is_refused(signing_key, trusted):
    raw = _envelope(signing_key, _payload(deployment_id="someone-else"))
    assert _reason(raw, trusted) is VerificationFailure.WRONG_DEPLOYMENT


def test_a_replayed_serial_is_refused(signing_key, trusted):
    """The primary anti-replay control: no clock required, no false positives."""
    raw = _envelope(signing_key, _payload(serial=4))
    assert _reason(raw, trusted, min_serial=5) is VerificationFailure.REPLAYED_SERIAL


def test_the_serial_floor_admits_the_same_serial_again(signing_key, trusted):
    """Equal is fine — a refresh that returns the current entitlement unchanged
    is the normal case, and refusing it would expire every stable deployment."""
    assert _verify(_envelope(signing_key, _payload(serial=5)), trusted, min_serial=5).serial == 5


def test_an_unknown_schema_version_is_refused(signing_key, trusted):
    raw = _envelope(signing_key, _payload(schema_version=99))
    assert _reason(raw, trusted) is VerificationFailure.UNKNOWN_SCHEMA_VERSION


def test_an_unexpected_field_at_a_known_version_is_refused(signing_key, trusted):
    """Strict parse: an unrecognised field means the issuer and this build
    disagree about what the document says, and guessing is how a capability
    field gets silently ignored."""
    payload = _payload()
    payload["unlimited"] = True
    assert _reason(_envelope(signing_key, payload), trusted) is VerificationFailure.UNEXPECTED_FIELD


def test_a_duplicate_json_key_is_refused(signing_key, trusted):
    """JSON permits duplicates and parsers keep the last, so a document could
    verify under one reading and be interpreted under another."""
    body = json.dumps(_payload())[:-1] + ', "expires_at": "2099-01-01T00:00:00+00:00"}'
    raw_bytes = body.encode()
    signature = signing_key.sign(ENTITLEMENT_DOMAIN + b"." + raw_bytes)
    raw = json.dumps(
        {
            "key_id": "k1",
            "payload": base64.b64encode(raw_bytes).decode(),
            "signature": base64.b64encode(signature).decode(),
        }
    ).encode()
    assert _reason(raw, trusted) is VerificationFailure.DUPLICATE_KEY


def test_a_term_longer_than_the_compiled_bound_is_refused(signing_key, trusted):
    raw = _envelope(
        signing_key,
        _payload(
            expires_at=(NOW + timedelta(days=3650)).isoformat(),
            grace_expires_at=(NOW + timedelta(days=3660)).isoformat(),
        ),
    )
    assert _reason(raw, trusted) is VerificationFailure.TERM_TOO_LONG


def test_a_grace_longer_than_the_compiled_bound_is_refused(signing_key, trusted):
    raw = _envelope(signing_key, _payload(grace_expires_at=(NOW + timedelta(days=200)).isoformat()))
    assert _reason(raw, trusted) is VerificationFailure.TERM_TOO_LONG


def test_an_entitlement_issued_in_the_future_is_refused(signing_key, trusted):
    raw = _envelope(signing_key, _payload(issued_at=(NOW + timedelta(days=7)).isoformat()))
    assert _reason(raw, trusted) is VerificationFailure.ISSUED_IN_FUTURE


def test_a_naive_timestamp_is_refused(signing_key, trusted):
    raw = _envelope(signing_key, _payload(expires_at="2026-07-01T00:00:00"))
    assert _reason(raw, trusted) is VerificationFailure.MALFORMED_PAYLOAD


def test_a_key_past_its_manifest_expiry_is_refused(signing_key):
    expired = {
        "k1": TrustedKey(
            key_id="k1",
            public_key=signing_key.public_key(),
            not_after=NOW - timedelta(days=1),
        )
    }
    raw = _envelope(signing_key, _payload())
    assert _reason(raw, expired) is VerificationFailure.MANIFEST_UNTRUSTED


def test_non_base64_payload_is_refused(trusted):
    raw = json.dumps({"key_id": "k1", "payload": "!!!not base64!!!", "signature": "AA=="}).encode()
    assert _reason(raw, trusted) is VerificationFailure.MALFORMED_ENVELOPE


def test_a_negative_or_boolean_serial_is_refused(signing_key, trusted):
    assert _reason(_envelope(signing_key, _payload(serial=-1)), trusted) is (
        VerificationFailure.MALFORMED_PAYLOAD
    )
    # `True` is an `int` in Python; without the explicit bool check it would be
    # accepted as serial 1.
    assert _reason(_envelope(signing_key, _payload(serial=True)), trusted) is (
        VerificationFailure.MALFORMED_PAYLOAD
    )


# --- the manifest -------------------------------------------------------------


def test_a_manifest_signed_by_the_root_yields_its_issuing_keys():
    root = Ed25519PrivateKey.generate()
    issuing = Ed25519PrivateKey.generate()
    body = json.dumps(
        {
            "keys": [
                {
                    "key_id": "k1",
                    "public_key": base64.b64encode(
                        issuing.public_key().public_bytes_raw()
                    ).decode(),
                }
            ]
        }
    ).encode()
    raw = json.dumps(
        {
            "payload": base64.b64encode(body).decode(),
            "signature": base64.b64encode(root.sign(MANIFEST_DOMAIN + b"." + body)).decode(),
        }
    ).encode()
    keys = load_manifest(raw, root_public_key=root.public_key(), now=NOW)
    assert set(keys) == {"k1"}


def test_a_manifest_not_signed_by_the_root_is_refused():
    root = Ed25519PrivateKey.generate()
    impostor = Ed25519PrivateKey.generate()
    body = json.dumps({"keys": []}).encode()
    raw = json.dumps(
        {
            "payload": base64.b64encode(body).decode(),
            "signature": base64.b64encode(impostor.sign(MANIFEST_DOMAIN + b"." + body)).decode(),
        }
    ).encode()
    with pytest.raises(EntitlementVerificationError) as caught:
        load_manifest(raw, root_public_key=root.public_key(), now=NOW)
    assert caught.value.reason is VerificationFailure.MANIFEST_UNTRUSTED
