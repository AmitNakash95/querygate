"""The renewal countdown, and what each surface is allowed to say (item 216).

Two properties, and the second is the security one:

* **The threshold is decided once.** Every surface derives from
  `renewal_notice`, so a banner cannot warn at a different point than the CLI.
* **The unauthenticated surfaces say strictly less.** `/health` is
  unauthenticated by design and `/metrics` is authenticated only by default
  (`metrics_require_auth=false` is supported), so both are treated as public:
  a day count or an expiry date on either tells any scanner when this
  customer's gateway stops serving. Those tests are written as *absence*
  assertions over the whole rendered body, not as "the field I added is
  coarse", because the failure mode is a field somebody adds later.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from querygate.api.app import create_app
from querygate.core.config import AppConfig
from querygate.core.exceptions import SubscriptionExpiredError
from querygate.core.scopes import (
    ADMIN_AUDIT_WORM_SEARCH_SCOPE,
    ADMIN_CONFIG_READ_SCOPE,
    ADMIN_METRICS_READ_SCOPE,
    ADMIN_OBSERVABILITY_READ_SCOPE,
)
from querygate.metrics import render_latest, reset_subscription_metrics
from querygate.subscription.cli import render as render_cli
from querygate.subscription.observability import current_notice, current_signal, publish_signal
from querygate.subscription.models import (
    LAPSE_CONSEQUENCE,
    RENEWAL_ESCALATION_DAYS,
    RENEWAL_NOTICE_DAYS,
    SCHEMA_VERSION,
    EnforcementMode,
    Entitlement,
    NoticeSeverity,
    RenewalState,
    SubscriptionSignal,
    evaluate,
    renewal_notice,
)
from querygate.subscription.state import subscription_state

pytestmark = [pytest.mark.security, pytest.mark.unit]

#: The real clock, because three of these surfaces read it and none of them has
#: an injectable one in production — `/api/v1/subscription` computes the notice
#: per request, deliberately, since the day count changes with the clock and not
#: with any event. Adding a test-only clock seam to a route is how the route
#: stops being the thing under test.
NOW = datetime.now(timezone.utc).replace(microsecond=0)

#: Every expiry is placed at midday rather than exactly N days out. `days_until`
#: floors, so an expiry exactly N days from `NOW` reads as N-1 the moment a
#: millisecond of test runtime elapses — a real off-by-one that would make this
#: file flaky rather than wrong.
_MIDDAY = timedelta(hours=12)
API_KEY = "renewal-notice-test-key"
ORG = "org_secret_identifier"
DEPLOYMENT = "dep_secret_identifier"


def _entitlement(
    *,
    expires_in_days: int,
    grace_days: int = 14,
    renewal_state: RenewalState = RenewalState.CANCELLING,
    enforce: bool = True,
) -> Entitlement:
    expires = NOW + timedelta(days=expires_in_days) + _MIDDAY
    return Entitlement(
        schema_version=SCHEMA_VERSION,
        org_id=ORG,
        deployment_id=DEPLOYMENT,
        serial=7,
        issued_at=NOW - timedelta(days=1),
        expires_at=expires,
        grace_expires_at=expires + timedelta(days=grace_days),
        enforcement=EnforcementMode.ENFORCE if enforce else EnforcementMode.OBSERVE,
        renewal_state=renewal_state,
        plan="enterprise-secret-plan-name",
        max_connections=None,
        max_seats=None,
        renewal_url="https://billing.example.invalid/renew",
    )


def _status(**kwargs):
    return evaluate(_entitlement(**kwargs), now=NOW)


def _publish(**kwargs):
    status = _status(**kwargs)
    subscription_state().publish(status)
    return status


# --- the threshold, decided once ---------------------------------------------


def test_an_auto_renewing_subscription_never_notices_however_close_it_is():
    """The window alone is not the trigger, and this is why it cannot be.

    The control plane's entitlement term is 30 days, so a healthy monthly
    subscription is *always* inside the notice window. A day-count trigger on
    its own would banner every deployment, forever, and a banner that is always
    up is a banner nobody reads on the day it matters.
    """
    for days in (0, 1, 5, 29):
        status = _status(expires_in_days=days, renewal_state=RenewalState.AUTO_RENEWING)
        assert renewal_notice(status, now=NOW) is None, days


def test_a_non_renewing_subscription_notices_only_inside_the_window():
    assert renewal_notice(_status(expires_in_days=RENEWAL_NOTICE_DAYS), now=NOW) is None
    assert renewal_notice(_status(expires_in_days=RENEWAL_NOTICE_DAYS + 30), now=NOW) is None
    notice = renewal_notice(_status(expires_in_days=RENEWAL_NOTICE_DAYS - 1), now=NOW)
    assert notice is not None
    assert notice.severity is NoticeSeverity.WARNING
    assert notice.days_remaining == RENEWAL_NOTICE_DAYS - 1


def test_the_notice_escalates_under_the_escalation_threshold():
    above = renewal_notice(_status(expires_in_days=RENEWAL_ESCALATION_DAYS + 1), now=NOW)
    at = renewal_notice(_status(expires_in_days=RENEWAL_ESCALATION_DAYS), now=NOW)
    assert above.severity is NoticeSeverity.WARNING
    assert at.severity is NoticeSeverity.CRITICAL


def test_grace_always_notices_at_critical_whatever_renewal_state_claims():
    """`auto_renewing` that expired anyway did not renew — the field was
    describing an intention, and the outcome overrules it."""
    notice = renewal_notice(
        _status(expires_in_days=-2, renewal_state=RenewalState.AUTO_RENEWING), now=NOW
    )
    assert notice is not None
    assert notice.severity is NoticeSeverity.CRITICAL
    assert "grace" in notice.headline.lower()


def test_a_fully_elapsed_term_notices_as_expired():
    notice = renewal_notice(
        _status(expires_in_days=-30, renewal_state=RenewalState.AUTO_RENEWING), now=NOW
    )
    assert notice.severity is NoticeSeverity.EXPIRED
    assert notice.days_remaining == 0


def test_a_deployment_with_no_entitlement_yet_is_never_bannered():
    """A fresh install, or a replica that has not refreshed, has no date to
    count down to. Warning it that it is about to stop is how the first five
    minutes of an evaluation go wrong."""
    assert renewal_notice(evaluate(None, now=NOW), now=NOW) is None


def test_a_payment_failure_says_so_rather_than_blaming_auto_renew():
    notice = renewal_notice(
        _status(expires_in_days=5, renewal_state=RenewalState.PAYMENT_FAILING), now=NOW
    )
    assert "payment" in notice.headline.lower()
    assert "auto-renew" not in notice.headline.lower()


def test_the_day_count_floors_rather_than_rounds():
    """Telling an operator "1 day" when eleven hours remain is a promise the
    clock breaks."""
    subscription = _status(expires_in_days=5)
    # 13 hours, not 11: `round(11/24)` is also 0, so the original interval was
    # satisfied by a rounding implementation and proved nothing. 13 hours floors
    # to 0 and rounds to 1, so only flooring passes.
    assert (
        renewal_notice(
            subscription, now=subscription.expires_at - timedelta(hours=13)
        ).days_remaining
        == 0
    )
    # And the other side of the boundary, so "always 0" does not pass either.
    assert (
        renewal_notice(
            subscription, now=subscription.expires_at - timedelta(hours=25)
        ).days_remaining
        == 1
    )


# --- what the public surfaces may say ----------------------------------------


_SECRETS = (
    ORG,
    DEPLOYMENT,
    "enterprise-secret-plan-name",
    NOW.date().isoformat(),
    "billing.example.invalid",
)


def test_the_health_endpoint_publishes_a_coarse_state_and_nothing_else():
    """An **allow-list** of keys, which is the only shape that catches a future field.

    The first version was a five-item denylist plus a dead disjunct: Starlette
    renders JSON with `separators=(",", ":")`, so the literal
    `'"subscription": "expired"'` — with a space — never matched, and the whole
    assertion collapsed to `"expired" in raw`. Adding `"subscription_ends":
    "2026-09-27"` to the body passed every line of it, while publishing exactly
    what the item forbids: today's date is not a future expiry, and the key was
    neither `expires_at` nor `days_remaining`.

    Naming the permitted keys inverts that. A field added "just for ops" now has
    to be argued for here first.
    """
    _publish(expires_in_days=-30)
    with TestClient(create_app(AppConfig(api_keys=[API_KEY]))) as client:
        response = client.get("/health")
    body = response.json()

    assert set(body) == {"status", "service", "version", "connections", "subscription"}
    assert body["subscription"] in {signal.value for signal in SubscriptionSignal}
    assert body["subscription"] == "expired"

    raw = response.text
    for secret in _SECRETS:
        assert secret not in raw, secret
    # No date in any format, anywhere in the body — the disclosure that would
    # tell a scanner when this customer's gateway stops serving.
    assert not re.search(r"\d{4}-\d{2}-\d{2}", raw), raw
    assert "days" not in raw


def test_the_health_endpoint_reports_grace_as_renewal_due_not_as_its_own_state():
    """Grace collapses on purpose: the operator action is identical, and the
    distinction is a commercial fact about the customer.

    Asserted **through the endpoint**, not by calling `current_signal` — this is
    the disclosure decision the whole `SubscriptionSignal` docstring defends, so
    it must be pinned where a caller can actually read it.
    """
    _publish(expires_in_days=-2)
    assert current_signal(now=NOW) is SubscriptionSignal.RENEWAL_DUE
    with TestClient(create_app(AppConfig(api_keys=[API_KEY]))) as client:
        assert client.get("/health").json()["subscription"] == "renewal_due"


def test_the_subscription_state_never_moves_the_health_status_code():
    """A 503 would make an orchestrator kill and restart the pod in a loop,
    turning a renewal conversation into something that looks like a crash.

    Asserted as a **comparison** rather than `== 200`: the status code is a
    function of database reachability, and the test fixtures point at databases
    that are not there, so a bare `== 200` fails for a reason that has nothing
    to do with billing. Driving both subscription states through the same app
    isolates the property that actually matters — billing does not move it.

    The first version of this test read only `.json()` and never touched
    `.status_code` at all; `TestClient` does not raise on 5xx, so returning 503
    on `expired` would have left it green.
    """
    with TestClient(create_app(AppConfig(api_keys=[API_KEY]))) as client:
        _publish(expires_in_days=3, renewal_state=RenewalState.AUTO_RENEWING)
        healthy = client.get("/health")
        _publish(expires_in_days=-30)
        expired = client.get("/health")

    assert expired.status_code == healthy.status_code
    assert healthy.json()["subscription"] == "ok"
    assert expired.json()["subscription"] == "expired"
    # And the health verdict itself is untouched by the subscription.
    assert expired.json()["status"] == healthy.json()["status"]


def test_the_metrics_gauge_carries_no_day_count_or_date():
    """The gauge is the unguarded sibling: `metrics_require_auth=false` is a
    supported configuration, so it may disclose no more than `/health`."""
    reset_subscription_metrics()
    _publish(expires_in_days=3)
    publish_signal(now=NOW)
    rendered = render_latest().decode()
    lines = [line for line in rendered.splitlines() if "querygate_subscription_signal{" in line]
    assert lines
    assert 'state="renewal_due"} 1.0' in "\n".join(lines)
    # Over the exposed *samples*, not the HELP prose — the HELP text explains
    # why there is no day count and would trip an assertion on the word itself.
    samples = "\n".join(line for line in rendered.splitlines() if line and not line.startswith("#"))
    for secret in _SECRETS:
        assert secret not in samples, secret
    assert "days_remaining" not in samples
    assert "expires" not in samples


def test_the_gauge_zeroes_the_states_that_are_no_longer_current():
    """Otherwise a deployment that recovers leaves a stale 1 alerting forever."""
    reset_subscription_metrics()
    _publish(expires_in_days=-30)
    publish_signal(now=NOW)
    _publish(expires_in_days=3, renewal_state=RenewalState.AUTO_RENEWING)
    publish_signal(now=NOW)
    rendered = render_latest().decode()
    assert 'querygate_subscription_signal{state="expired"} 0.0' in rendered
    assert 'querygate_subscription_signal{state="ok"} 1.0' in rendered


def test_the_signal_is_computed_at_scrape_time_not_at_publish_time():
    """The clock alone moves a deployment into the notice window.

    A subscription 40 days out is quiet; 15 days later, with **no refresh and no
    republish**, it is inside the 30-day window. A gauge written only when the
    manager publishes a verdict would sit on `ok` for up to a full refresh
    interval after the threshold was crossed — stale in the direction that says
    everything is fine.

    Note what this deliberately does *not* claim: `expired` still requires a
    re-evaluation, because `SubscriptionStatus.entitlement` is a snapshot of the
    last one. That is the same property the gate itself has (item 211), and it is
    why grace is measured in days rather than hours.
    """
    reset_subscription_metrics()
    _publish(expires_in_days=40)
    publish_signal(now=NOW)
    assert 'querygate_subscription_signal{state="ok"} 1.0' in render_latest().decode()

    # Nothing republished; only the clock moved.
    publish_signal(now=NOW + timedelta(days=15))
    rendered = render_latest().decode()
    assert 'querygate_subscription_signal{state="renewal_due"} 1.0' in rendered
    assert 'querygate_subscription_signal{state="ok"} 0.0' in rendered


# --- what the authenticated surfaces may say ---------------------------------


def test_the_notice_endpoint_requires_a_credential():
    _publish(expires_in_days=3)
    with TestClient(create_app(AppConfig(api_keys=[API_KEY]))) as client:
        assert client.get("/api/v1/subscription").status_code == 401


def test_the_notice_endpoint_carries_the_countdown_and_the_exact_date():
    _publish(expires_in_days=3)
    with TestClient(create_app(AppConfig(api_keys=[API_KEY]))) as client:
        body = client.get(
            "/api/v1/subscription", headers={"Authorization": f"Bearer {API_KEY}"}
        ).json()
    notice = body["notice"]
    assert notice["days_remaining"] == 3
    assert notice["severity"] == "critical"
    assert notice["expires_at"].startswith((NOW + timedelta(days=3) + _MIDDAY).date().isoformat())
    assert notice["renewal_url"] == "https://billing.example.invalid/renew"


def test_the_notice_endpoint_names_no_org_deployment_serial_or_plan():
    """Every principal with any credential reads this, including an analyst with
    no admin scope. None of those identifiers helps them act."""
    _publish(expires_in_days=3)
    with TestClient(create_app(AppConfig(api_keys=[API_KEY]))) as client:
        raw = client.get(
            "/api/v1/subscription", headers={"Authorization": f"Bearer {API_KEY}"}
        ).text
    for secret in (ORG, DEPLOYMENT, "enterprise-secret-plan-name", '"serial"'):
        assert secret not in raw, secret


def test_a_healthy_subscription_returns_no_notice_so_the_banner_stays_hidden():
    _publish(expires_in_days=3, renewal_state=RenewalState.AUTO_RENEWING)
    with TestClient(create_app(AppConfig(api_keys=[API_KEY]))) as client:
        body = client.get(
            "/api/v1/subscription", headers={"Authorization": f"Bearer {API_KEY}"}
        ).json()
    assert body["notice"] is None


# --- the 402, and what it says -----------------------------------------------


def test_the_refusal_names_the_renewal_url_and_no_identifier():
    message = str(SubscriptionExpiredError(renewal_url="https://billing.example.invalid/renew"))
    assert "https://billing.example.invalid/renew" in message
    for secret in (ORG, DEPLOYMENT, "enterprise-secret-plan-name", NOW.date().isoformat()):
        assert secret not in message, secret


def test_the_refusal_without_a_url_still_tells_the_caller_what_to_do():
    message = str(SubscriptionExpiredError())
    assert "administrator" in message


def test_every_surface_describes_the_consequence_the_same_way():
    """A banner, an email and a CLI that describe expiry differently are three
    chances to be wrong about it. One sentence, quoted everywhere."""
    notice = renewal_notice(_status(expires_in_days=-30), now=NOW)
    assert LAPSE_CONSEQUENCE in notice.detail
    assert "402" in LAPSE_CONSEQUENCE
    # Both transports, because JSON-RPC has no HTTP status.
    assert "SUBSCRIPTION_EXPIRED" in LAPSE_CONSEQUENCE
    # And what survives, stated as what the gate actually covers. The earlier
    # wording said "configuration access keeps working" while LICENSING_FAQ.md
    # said configuration was suspended; the code matches neither exactly —
    # live schema reflection is gated, configuration and audit retrieval are not.
    assert "Audit retrieval and configuration changes are not gated" in LAPSE_CONSEQUENCE


# --- the CLI ------------------------------------------------------------------


def test_the_cli_renders_the_countdown_and_the_renewal_state():
    _publish(expires_in_days=3)
    rendered = render_cli(subscription_state().status, now=NOW)
    assert "auto-renew is off" in rendered
    assert "[CRITICAL]" in rendered
    assert "https://billing.example.invalid/renew" in rendered


def test_the_cli_says_nothing_extra_when_the_subscription_is_healthy():
    _publish(expires_in_days=3, renewal_state=RenewalState.AUTO_RENEWING)
    rendered = render_cli(subscription_state().status, now=NOW)
    assert "CRITICAL" not in rendered
    assert "renews automatically" in rendered


def test_the_cli_and_the_endpoint_agree_because_both_call_the_same_function():
    _publish(expires_in_days=5)
    assert current_notice(now=NOW).headline in render_cli(subscription_state().status, now=NOW)


# --- the claim the banner makes about what survives expiry --------------------


def test_audit_and_configuration_survive_expiry_but_queries_do_not():
    """`LAPSE_CONSEQUENCE` is published in the banner, the email and the CLI.

    It promises two things: queries stop, and audit export does not. The second
    half is the one that could quietly become false — someone adds the gate to a
    shared dependency and a customer who has lapsed can no longer retrieve the
    records EULA §18 says they may. So it is asserted through real routes, on a
    deployment whose term and grace have both elapsed under enforcement.
    """
    _publish(expires_in_days=-30)
    cfg = AppConfig(
        api_keys=[API_KEY],
        api_key_scopes=[ADMIN_OBSERVABILITY_READ_SCOPE, "query:read"],
    )
    headers = {"Authorization": f"Bearer {API_KEY}"}
    with TestClient(create_app(cfg)) as client:
        audit = client.get("/api/v1/admin/observability/overview", headers=headers)
        query = client.post(
            "/api/v1/demo/query",
            headers=headers,
            json={"from_table": "users", "select": ["users.id"], "limit": 1},
        )
    assert audit.status_code == status.HTTP_200_OK
    assert query.status_code == status.HTTP_402_PAYMENT_REQUIRED
    assert "billing.example.invalid/renew" in query.text


def test_the_gate_is_wired_only_into_the_funnels_the_consequence_names():
    """A source-level companion to the behavioural test above.

    The promise is about *categories* of surface, and no behavioural test can
    prove the absence of a call site it never happens to traverse. If the gate
    appears in `audit/`, `admin/` or a shared dependency, the sentence every
    surface publishes has become false.

    Walks the tree rather than shelling out to `git grep`, which was the first
    version and had three problems: it is blind to untracked files, so a new
    `audit/export_routes.py` importing the gate would be invisible until someone
    ran `git add`; it matched comments and docstrings, so a documentation edit
    failed the suite; and it printed paths relative to the invocation directory,
    so running pytest from `tests/` failed for no reason at all. Import
    statements only, and a subset assertion, so adding a permitted module is not
    a suite break.
    """
    import ast
    from pathlib import Path

    source_root = Path(__file__).resolve().parents[2] / "src" / "querygate"
    permitted = {
        "execution/service.py",
        "execution/subscription_gate.py",
        "execution/write_execution.py",
        "execution/write_preview.py",
    }

    importers = set()
    for path in source_root.rglob("*.py"):
        relative = str(path.relative_to(source_root))
        if relative.startswith("subscription/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [alias.name for alias in node.names]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any(
                "subscription_gate" in name or "require_active_subscription" in name
                for name in names
            ):
                importers.add(relative)

    assert importers <= permitted, sorted(importers - permitted)
    # And the gate is genuinely reached from the funnels, not merely absent
    # everywhere — an empty set would satisfy the assertion above.
    assert "execution/service.py" in importers


def test_the_metrics_route_publishes_the_signal_on_every_scrape():
    """The **wiring**, not the helper.

    `publish_signal()` could be deleted from both `/metrics` handlers with the
    rest of this file green: the earlier tests call it directly and read
    `render_latest()` directly, proving only that it honours its `now` argument.
    A real scrape would then return no `querygate_subscription_signal` series at
    all — a total loss of the surface, invisible to the suite.
    """
    reset_subscription_metrics()
    _publish(expires_in_days=3)
    cfg = AppConfig(api_keys=[API_KEY], api_key_scopes=[ADMIN_METRICS_READ_SCOPE])
    with TestClient(create_app(cfg)) as client:
        body = client.get("/metrics", headers={"Authorization": f"Bearer {API_KEY}"}).text
    assert 'querygate_subscription_signal{state="renewal_due"} 1.0' in body


def test_the_unauthenticated_metrics_route_publishes_it_too_and_says_no_more():
    """`metrics_require_auth=false` is the branch whose disclosure risk the whole
    `SubscriptionSignal` docstring is written to justify, and it had no test. It
    must carry the signal, and nothing beyond it."""
    reset_subscription_metrics()
    _publish(expires_in_days=3)
    with TestClient(create_app(AppConfig(metrics_require_auth=False))) as client:
        response = client.get("/metrics")
    assert response.status_code == status.HTTP_200_OK
    samples = "\n".join(
        line for line in response.text.splitlines() if line and not line.startswith("#")
    )
    assert 'querygate_subscription_signal{state="renewal_due"} 1.0' in samples
    for secret in _SECRETS:
        assert secret not in samples, secret
    assert not re.search(r"\d{4}-\d{2}-\d{2}", samples)


def test_audit_retrieval_survives_expiry_through_the_route_the_consequence_names():
    """`LAPSE_CONSEQUENCE` says "audit export keeps working", and the earlier
    test proved it of the *observability overview*. The literal audit-record
    surface — the one EULA §18 grants a perpetual licence to — is
    `/admin/audit/events`, and it was never requested on an expired deployment.
    """
    _publish(expires_in_days=-30)
    cfg = AppConfig(
        api_keys=[API_KEY],
        api_key_scopes=[
            ADMIN_CONFIG_READ_SCOPE,
            ADMIN_AUDIT_WORM_SEARCH_SCOPE,
            ADMIN_OBSERVABILITY_READ_SCOPE,
        ],
    )
    headers = {"Authorization": f"Bearer {API_KEY}"}
    with TestClient(create_app(cfg)) as client:
        events = client.get("/api/v1/admin/audit/events", headers=headers)
        worm = client.get("/api/v1/admin/observability/worm-search", headers=headers)
    assert events.status_code != status.HTTP_402_PAYMENT_REQUIRED
    assert worm.status_code != status.HTTP_402_PAYMENT_REQUIRED


def test_the_mcp_transport_reports_a_code_because_it_has_no_http_status():
    """`LAPSE_CONSEQUENCE` says "HTTP 402", which is true of REST and meaningless
    over JSON-RPC. An MCP-only customer told to look for a status code their
    transport does not have has been given a wrong instruction, so the code they
    *will* see is pinned here and named in LICENSING_FAQ.md."""
    from querygate.mcp.exceptions import _error_code_from_exception

    result = _error_code_from_exception(
        SubscriptionExpiredError(renewal_url="https://billing.example.invalid/renew")
    )
    code = result[0] if isinstance(result, tuple) else result
    assert code == "SUBSCRIPTION_EXPIRED"


# --- observe mode must not claim a refusal it is not making --------------------


def test_observe_mode_never_reports_expired_on_any_surface():
    """The state **every new deployment starts in**, so this false alarm would
    land on the first customers first.

    `_enforcement_for` defaults to `observe`, and item 211's whole design is that
    the enforce path runs in production long before it can refuse anything. A
    deployment whose term and grace have both elapsed under observe serves every
    request normally — reporting `expired` would page an operator about a healthy
    gateway, and would make "expired and refusing" indistinguishable from
    "expired and observing" on all three of the new surfaces, which is exactly
    the question a cutover needs answered.
    """
    reset_subscription_metrics()
    _publish(expires_in_days=-30, enforce=False)

    notice = current_notice(now=NOW)
    assert notice is not None
    assert notice.severity is NoticeSeverity.CRITICAL
    assert notice.severity is not NoticeSeverity.EXPIRED
    assert "Nothing is being refused yet" in notice.detail
    assert "still being served" in notice.headline

    assert current_signal(now=NOW) is not SubscriptionSignal.EXPIRED
    publish_signal(now=NOW)
    assert 'querygate_subscription_signal{state="expired"} 0.0' in render_latest().decode()


def test_observe_mode_serves_the_query_the_banner_says_is_not_refused():
    """The behavioural half. The banner claims nothing is being refused; a real
    request through the real service is what makes that a fact rather than a
    sentence."""
    _publish(expires_in_days=-30, enforce=False)
    cfg = AppConfig(api_keys=[API_KEY], api_key_scopes=["query:read"])
    with TestClient(create_app(cfg)) as client:
        response = client.post(
            "/api/v1/demo/query",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"from_table": "users", "select": ["users.id"], "limit": 1},
        )
    assert response.status_code != status.HTTP_402_PAYMENT_REQUIRED


def test_the_notice_detail_never_promises_a_402_observe_mode_is_not_making():
    """`LAPSE_CONSEQUENCE` says queries "are refused". Quoting it in observe mode
    would be plainly false to the one person reading the banner."""
    observing = current_notice_for(_status(expires_in_days=-30, enforce=False))
    enforcing = current_notice_for(_status(expires_in_days=-30, enforce=True))
    assert LAPSE_CONSEQUENCE not in observing.detail
    assert LAPSE_CONSEQUENCE in enforcing.detail


def current_notice_for(subscription_status):
    """Local helper: the notice for a status that is not the published one."""
    return renewal_notice(subscription_status, now=NOW)
