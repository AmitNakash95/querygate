"""Security regression: TODO.md item 134 phase 2's managed WORM search must
never surface SQL text, a predicate literal value, a row, or a credential —
mirroring phase 1's own redaction-safety technique (`test_audit_worm_sink.py`'s
byte-identical-to-the-local-sink assertion): assert against the actual
returned payload, not just review the code.

A search result can only ever be as redaction-safe as the archived event body
it reads back — this module (`audit/worm_search.py`) never constructs its own
event content, it validates each line against the same `extra="forbid"`
`AuditEvent`/`ConfigChangeEvent`/`CatalogGovernanceEvent`/`ConnectionProbeEvent`
schemas the local sinks already write. The second test below proves that
guarantee holds even when an object in the bucket carries a forbidden field
(simulating a compromised writer or a bug that leaked something into the
archive that should never have been there) — it must be rejected as
malformed, never smuggled through the search response.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from querygate.audit.events import AuditEvent, normalize_query_shape
from querygate.audit.ledger import GENESIS_PREV_HASH, make_record
from querygate.audit.worm_search import WormSearchBounds, search_worm_archive
from querygate.query_ast.models import Predicate, StructuredQuery, WhereGroup

pytestmark = pytest.mark.security

_BUCKET = "querygate-worm-search-security-test"
_PREFIX = "querygate-audit/"
_SECRET_LITERAL = "super-secret-ssn-shaped-literal-861204321"


def _bounds() -> WormSearchBounds:
    return WormSearchBounds(
        max_window_seconds=86400 * 30,
        max_objects_scanned=100,
        default_limit=50,
        max_limit=500,
        request_timeout_seconds=20.0,
    )


def _put(client, key: str, lines: list) -> None:
    body = ("\n".join(lines) + "\n").encode("utf-8")
    client.put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=body,
        ObjectLockMode="COMPLIANCE",
        ObjectLockRetainUntilDate=datetime.now(timezone.utc) + timedelta(days=1),
    )


def _enveloped(event_body: dict) -> str:
    # TODO.md item 154: a single-record, validly-signed segment (genesis
    # seq=0) wrapping `event_body` as-is — including when `event_body` is
    # deliberately tampered/forged content. This is the realistic threat
    # model post-item-154: a writer able to produce a self-consistent
    # envelope (e.g. a compromised process with the same signing key, or an
    # unkeyed chain anyone with bucket write access can sign) that still
    # tries to smuggle a forbidden field inside the embedded event — the
    # envelope alone is not the only defense, extra="forbid" schema
    # validation on the UNWRAPPED body is the second, independent gate these
    # tests exist to prove.
    return make_record(0, GENESIS_PREV_HASH, event_body).model_dump_json()


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=_BUCKET, ObjectLockEnabledForBucket=True)
        yield client


async def _search(**overrides):
    kwargs = dict(
        bucket=_BUCKET,
        prefix=_PREFIX,
        region="us-east-1",
        flush_interval_seconds=60,
        start_time=datetime(2026, 3, 15, tzinfo=timezone.utc),
        end_time=datetime(2026, 3, 16, tzinfo=timezone.utc),
        bounds=_bounds(),
    )
    kwargs.update(overrides)
    return await search_worm_archive(**kwargs)


class TestLegitimateEventCarriesNoLiteralValue:
    async def test_a_predicate_referencing_a_sensitive_value_never_surfaces_that_value(self, s3):
        query = StructuredQuery(
            from_table="customers",
            select=["id"],
            where=WhereGroup(
                and_terms=[
                    Predicate(op="eq", col="customers.ssn", value=_SECRET_LITERAL),
                ]
            ),
        )
        event = AuditEvent(
            connection_id="demo",
            policy_decision="allowed",
            outcome="success",
            query_shape=normalize_query_shape(query),
            duration_ms=2,
            occurred_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        )
        _put(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_enveloped(event.model_dump(mode="json", exclude_none=True))],
        )

        result = await _search()

        assert len(result.events) == 1
        raw = result.model_dump_json()
        assert _SECRET_LITERAL not in raw
        # The predicate's structural shape (operator/column) is legitimately
        # present — only the VALUE is excluded.
        assert '"operator":"eq"' in raw.replace(" ", "") or "eq" in raw
        assert "ssn" in raw


class TestTamperedSegmentIsRejectedNotLeaked:
    async def test_a_forbidden_sql_field_on_an_otherwise_valid_event_is_never_returned(self, s3):
        """extra='forbid' on every persistable-event schema is the actual
        enforcement point: an object in the bucket that somehow carries a
        'sql'/'row_data' field alongside otherwise-valid fields must fail
        validation and be counted as malformed, not silently pass through
        with the forbidden field just dropped (which would be a different,
        also-bad failure mode — the point is the whole line is rejected)."""
        legit = AuditEvent(
            connection_id="demo",
            policy_decision="allowed",
            outcome="success",
            query_shape={"from": "customers"},
            duration_ms=1,
            occurred_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        )
        tampered = json.loads(legit.model_dump_json(exclude_none=True))
        tampered["sql"] = f"SELECT * FROM customers WHERE ssn = '{_SECRET_LITERAL}'"
        tampered["row_data"] = {"ssn": _SECRET_LITERAL}

        _put(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_enveloped(tampered)],
        )

        result = await _search()

        assert result.events == []
        assert result.malformed == 1
        raw = result.model_dump_json()
        assert _SECRET_LITERAL not in raw
        assert "SELECT" not in raw
        assert '"sql"' not in raw
        assert '"row_data"' not in raw

    async def test_a_forbidden_field_nested_inside_query_shape_is_rejected_too(self, s3):
        """`AuditEvent.query_shape` is a plain `Dict[str, Any]` — the one
        field on any PersistableEvent member whose INTERIOR `extra="forbid"`
        cannot constrain. A forged line that puts the top-level fields
        through validation cleanly but smuggles a forbidden key INSIDE
        query_shape must be rejected the same way a top-level forgery is
        (security-invariant-reviewer, 2026-08-06, WS-2) — not silently
        returned with the secret content nested one level down."""
        legit = AuditEvent(
            connection_id="demo",
            policy_decision="allowed",
            outcome="success",
            query_shape={"from": "customers"},
            duration_ms=1,
            occurred_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        )
        tampered = json.loads(legit.model_dump_json(exclude_none=True))
        # Nested two levels deep, inside an otherwise-legitimate-looking
        # structural shape — not at the top level, which the other tests
        # already cover.
        tampered["query_shape"] = {
            "from": "customers",
            "select": [{"kind": "column", "column": "id"}],
            "where": {"sql": f"ssn = '{_SECRET_LITERAL}'"},
        }
        _put(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_enveloped(tampered)],
        )

        result = await _search()

        assert result.events == []
        assert result.malformed == 1
        assert _SECRET_LITERAL not in result.model_dump_json()

    async def test_an_unknown_event_type_discriminator_is_rejected_not_coerced(self, s3):
        forged = {
            "schema_version": "1",
            "event_id": "x",
            "occurred_at": "2026-03-15T12:00:00+00:00",
            "event_type": "row.dump",  # not a real discriminator value
            "connection_id": "demo",
            "policy_decision": "allowed",
            "outcome": "success",
            "query_shape": {},
            "duration_ms": 1,
        }
        _put(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_enveloped(forged)],
        )

        result = await _search()

        assert result.events == []
        assert result.malformed == 1

    async def test_a_credential_shaped_field_smuggled_onto_an_event_is_rejected(self, s3):
        legit = AuditEvent(
            connection_id="demo",
            policy_decision="allowed",
            outcome="success",
            query_shape={},
            duration_ms=1,
            occurred_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        )
        tampered = json.loads(legit.model_dump_json(exclude_none=True))
        tampered["connection_string"] = "postgresql://user:hunter2@db.internal/prod"

        _put(
            s3,
            f"{_PREFIX}2026/03/15/20260315T120000-000001.jsonl",
            [_enveloped(tampered)],
        )

        result = await _search()

        assert result.events == []
        assert result.malformed == 1
        assert "hunter2" not in result.model_dump_json()
