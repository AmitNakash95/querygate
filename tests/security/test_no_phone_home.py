"""QueryGate makes no outbound call to its vendor, ever.

`docs/LICENSING_FAQ.md` states this as an absolute — "no telemetry of any kind …
no outbound calls to us, ever … no kill switch, no time bomb, and no check that
can refuse to start or block a query" — and it is the first thing a security
reviewer greps for, because a beacon originating inside a customer network
contradicts the product's central claim that credentials and data never leave.

It was true by architecture and asserted by nothing. That is the same shape as
the alias-quoting gap: correct today, unguarded against tomorrow. It matters
more here because TODO item 197 (the offline entitlement token) will deliberately
add licence-checking code, and the property that must survive it — verified
locally, zero network calls, soft-warn only — is exactly what this pins.

Source-level rather than behavioural, deliberately: the claim is about code that
must *not exist*, and no runtime test can prove the absence of a call site it
never happens to execute. This is the posture
`test_every_key_the_script_touches_shares_one_hash_slot` takes for the same
reason.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.security, pytest.mark.unit]

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "querygate"

# Hosts that would represent a call home. Any QueryGate-controlled domain is a
# phone-home by definition, whatever it is called in the code.
VENDOR_HOST_RE = re.compile(
    r"\b(?:[\w-]+\.)*(?:querygate|query-gate)\.(?:com|io|dev|net|org|ai|sh|app|cloud)\b",
    re.I,
)

# Vocabulary that unambiguously means calling home. Deliberately NOT bare
# "telemetry" or "beacon": both have benign local uses here — a catalog fixture
# uses `telemetry` as a table alias, and `admin/redis_observed_shapes.py`
# describes itself as "best-effort operator telemetry", which is local and is
# the operator's own data. A guard that fires on those would be turned off.
# No trailing \b: the point is to catch `LICENSE_SERVER_URL`, where the next
# character is a word character.
PHONE_HOME_TOKENS = re.compile(
    r"(?:phone_?home|call_?home|licen[cs]e_server|entitlement_(?:url|endpoint|server)|"
    r"activation_(?:url|endpoint|server)|usage_report|heartbeat_url)",
    re.I,
)

# "telemetry" only counts when it is pointed at something over the wire.
TELEMETRY_ENDPOINT_RE = re.compile(r"telemetry[\w_]*\s*[:=]\s*[\"']https?://", re.I)


def _python_sources() -> list[Path]:
    return sorted(SOURCE_ROOT.rglob("*.py"))


def test_no_vendor_hostname_appears_anywhere_in_the_shipped_source():
    """The grep a reviewer will run, run for them and pinned."""
    offenders = []
    for path in _python_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if VENDOR_HOST_RE.search(line):
                offenders.append(
                    f"{path.relative_to(SOURCE_ROOT.parent.parent)}:{number}: {line.strip()}"
                )
    assert not offenders, (
        "a QueryGate-controlled hostname appears in shipped source, which would make "
        "`docs/LICENSING_FAQ.md`'s 'no outbound calls to us, ever' false:\n" + "\n".join(offenders)
    )


def test_no_telemetry_or_licence_server_vocabulary_in_the_shipped_source():
    """Catches the intent before the endpoint exists.

    A constant named `LICENSE_SERVER_URL` is a phone-home whether or not it is
    populated yet, and this is the guard that has to survive item 197.
    """
    offenders = []
    for path in _python_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PHONE_HOME_TOKENS.search(line) or TELEMETRY_ENDPOINT_RE.search(line):
                offenders.append(f"{path.relative_to(SOURCE_ROOT.parent.parent)}:{number}")
    assert not offenders, "telemetry/licence-server vocabulary in shipped source: " + ", ".join(
        offenders
    )


def test_the_shipped_source_imports_no_outbound_http_client_at_module_scope():
    """`httpx` is a dependency — of the MCP SDK and of JWKS verification, both of
    which call hosts the *operator* configures. What must not appear is an
    unconditional client bound to a QueryGate endpoint. This asserts the weaker,
    checkable half: every `httpx`/`urllib`/`requests` import in shipped source is
    accounted for by a module the operator points at something.
    """
    allowed = {
        "core/auth.py",  # JWKS fetch, from an operator-configured issuer URL
        "core/jwks.py",
        "secrets/resolvers.py",  # Vault / cloud secret backends the operator names
        "health.py",
    }
    unexpected = []
    for path in _python_sources():
        rel = str(path.relative_to(SOURCE_ROOT))
        if rel in allowed:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - shipped source always parses
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in {"requests", "urllib3"}:
                    unexpected.append(f"{rel} imports {name}")
    assert not unexpected, (
        "an unexpected HTTP client is imported in shipped source; if this is legitimate, "
        "add the module to `allowed` with the operator-configured endpoint it talks to:\n"
        + "\n".join(unexpected)
    )


def test_the_guard_would_catch_a_planted_beacon(tmp_path):
    """Negative control: the patterns must actually match a real phone-home."""
    planted = 'LICENSE_SERVER_URL = "https://api.querygate.com/v1/entitlement"'
    assert VENDOR_HOST_RE.search(planted)
    assert PHONE_HOME_TOKENS.search(planted)
    assert TELEMETRY_ENDPOINT_RE.search('TELEMETRY_URL = "https://api.querygate.com/t"')
    # ...and must not fire on the legitimate operator-configured shapes.
    for benign in (
        'issuer = "https://login.microsoftonline.com/tenant/v2.0"',
        'VAULT_ADDR = "https://vault.internal:8200"',
        'gateway = "https://gateway.internal"',
        # The two real in-tree uses of the word, both local to the operator.
        "aliases: [telemetry]",
        "which is the closer analogue: best-effort operator telemetry.",
    ):
        assert not VENDOR_HOST_RE.search(benign), benign
        assert not PHONE_HOME_TOKENS.search(benign), benign
