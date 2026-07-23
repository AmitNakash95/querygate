"""Four-eyes config approval — store + service invariants (TODO.md item 42 phase 1).

The security-critical properties: an author can never approve their own change,
a version can only be reviewed while staged, N approvals means N *distinct*
reviewers (a reviewer's latest decision supersedes their own), and a staged
version cannot be applied until it has the required number of approvals — while
single-administrator mode (require_config_approvals == 0) behaves exactly as
before (backward compatible).
"""

from __future__ import annotations

import pytest

from querygate.admin import service as governance
from querygate.admin.models import ConfigApprovalDecision, ConfigVersionStatus
from querygate.admin.store import ConfigVersionStore, set_config_version_store
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import PolicyViolationError

pytestmark = pytest.mark.unit


def _store(tmp_path) -> ConfigVersionStore:
    store = ConfigVersionStore(str(tmp_path / "governance"))
    store.bootstrap_if_empty(
        connections_yaml="connections: []\n", policy_yaml="default: {}\n", catalog_yaml=None
    )
    set_config_version_store(store)
    return store


def _stage(store, *, actor="author-a") -> str:
    # Valid config content — `apply` validates before the four-eyes gate, so the
    # staged version must pass validation for the gate itself to be exercised.
    v = store.create_staged_version(
        connections_yaml="connections: []\n",
        policy_yaml="default:\n  enabled: true\n",
        catalog_yaml=None,
        description="change",
        actor=actor,
    )
    return v.id


def _fp(store, version_id) -> str:
    v = store.get_version(version_id)
    return governance._version_fingerprint(
        v.connections_yaml, v.policy_yaml, v.catalog_yaml, v.templates_yaml
    )


def test_author_cannot_approve_their_own_version(tmp_path):
    store = _store(tmp_path)
    vid = _stage(store, actor="author-a")
    with pytest.raises(PolicyViolationError):
        store.add_approval(
            vid,
            approver="author-a",
            decision=ConfigApprovalDecision.APPROVE,
            content_fingerprint=_fp(store, vid),
        )


def test_only_staged_versions_can_be_reviewed(tmp_path):
    store = _store(tmp_path)
    active_id = store.get_active_version_id()
    with pytest.raises(PolicyViolationError):
        store.add_approval(
            active_id,
            approver="reviewer-b",
            decision=ConfigApprovalDecision.APPROVE,
            content_fingerprint=_fp(store, active_id),
        )


def test_distinct_reviewers_accumulate_but_same_reviewer_supersedes(tmp_path):
    store = _store(tmp_path)
    vid = _stage(store)
    fp = _fp(store, vid)
    store.add_approval(
        vid, approver="b", decision=ConfigApprovalDecision.APPROVE, content_fingerprint=fp
    )
    store.add_approval(
        vid, approver="c", decision=ConfigApprovalDecision.APPROVE, content_fingerprint=fp
    )
    # b changes their mind: reject supersedes their earlier approve (no stacking).
    store.add_approval(
        vid, approver="b", decision=ConfigApprovalDecision.REJECT, content_fingerprint=fp
    )
    version = store.get_version(vid)
    assert len(version.approvals) == 2  # one per reviewer
    assert governance._count_valid_approvals(version) == 1  # only c approves now


@pytest.mark.asyncio
async def test_apply_blocked_until_required_approvals_present(tmp_path, monkeypatch):
    store = _store(tmp_path)
    vid = _stage(store, actor="author-a")
    cfg = AppConfig(environment="localhost", require_config_approvals=2)
    author = Principal(subject="author-a", scopes={"admin:config:write"}, auth_method="api_key")

    # Zero approvals -> blocked.
    with pytest.raises(PolicyViolationError):
        await governance.apply(cfg, author, vid)

    fp = _fp(store, vid)
    store.add_approval(
        vid, approver="b", decision=ConfigApprovalDecision.APPROVE, content_fingerprint=fp
    )
    # One of two -> still blocked.
    with pytest.raises(PolicyViolationError):
        await governance.apply(cfg, author, vid)

    store.add_approval(
        vid, approver="c", decision=ConfigApprovalDecision.APPROVE, content_fingerprint=fp
    )
    # Two distinct approvals -> allowed.
    version, _reload = await governance.apply(cfg, author, vid)
    assert version.status == ConfigVersionStatus.ACTIVE


@pytest.mark.asyncio
async def test_single_admin_mode_applies_without_approvals(tmp_path):
    store = _store(tmp_path)
    vid = _stage(store, actor="author-a")
    cfg = AppConfig(environment="localhost")  # require_config_approvals defaults to 0
    author = Principal(subject="author-a", scopes={"admin:config:write"}, auth_method="api_key")
    version, _reload = await governance.apply(cfg, author, vid)
    assert version.status == ConfigVersionStatus.ACTIVE


def test_service_approve_enforces_author_not_approver(tmp_path):
    store = _store(tmp_path)
    vid = _stage(store, actor="author-a")
    cfg = AppConfig(environment="localhost", require_config_approvals=1)
    author = Principal(subject="author-a", scopes={"admin:config:approve"}, auth_method="api_key")
    with pytest.raises(PolicyViolationError):
        governance.approve(cfg, author, vid, decision=ConfigApprovalDecision.APPROVE)


def test_service_approve_records_a_distinct_reviewer(tmp_path):
    store = _store(tmp_path)
    vid = _stage(store, actor="author-a")
    cfg = AppConfig(environment="localhost", require_config_approvals=1)
    reviewer = Principal(
        subject="reviewer-b", scopes={"admin:config:approve"}, auth_method="api_key"
    )
    updated = governance.approve(
        cfg, reviewer, vid, decision=ConfigApprovalDecision.APPROVE, note="lgtm"
    )
    assert governance._count_valid_approvals(updated) == 1
    assert updated.approvals[0].note == "lgtm"


# --------------------------------------------------------------------------- #
# REST endpoint wiring                                                          #
# --------------------------------------------------------------------------- #

_KEY = "reviewer-key"


def _client_app(tmp_path, scopes):
    from querygate.api.app import create_app

    _store(tmp_path)  # installs a fresh governance store with a bootstrapped active version
    cfg = AppConfig(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend="none",
        api_keys=[_KEY],
        api_key_scopes=list(scopes),
    )
    return create_app(cfg)


@pytest.mark.asyncio
async def test_approve_endpoint_requires_approve_scope(tmp_path):
    from httpx import ASGITransport, AsyncClient

    from querygate.admin.store import get_config_version_store

    app = _client_app(tmp_path, scopes=("admin:config:write",))  # write, but NOT approve
    vid = _stage(get_config_version_store(), actor="author-a")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        resp = await client.post(
            f"/api/v1/admin/config/versions/{vid}/approve",
            headers={"Authorization": f"Bearer {_KEY}"},
            json={},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_approve_endpoint_author_conflict_is_409(tmp_path):
    from httpx import ASGITransport, AsyncClient

    from querygate.admin.store import get_config_version_store

    app = _client_app(tmp_path, scopes=("admin:config:approve",))
    # The api-key principal's subject is AppConfig.api_key_subject ("api-key-client");
    # stage as that same subject so the endpoint sees author == approver.
    vid = _stage(get_config_version_store(), actor="api-key-client")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        resp = await client.post(
            f"/api/v1/admin/config/versions/{vid}/approve",
            headers={"Authorization": f"Bearer {_KEY}"},
            json={"note": "self-approve attempt"},
        )
    assert resp.status_code == 409
