"""`querygate-login` — get a QueryGate token as yourself, from a terminal.

The client half of the RFC 8628 device grant (`identity/device.py`). It prints
a short code, sends you to a browser you are already signed in to, and — once a
human approves — receives a short-lived token carrying **that person's**
identity, limited to the scopes they actually hold.

This is the alternative to the thing every CLI-plus-API story degrades into: a
long-lived shared key pasted into a shell profile, attributable to nobody. A
token obtained here expires on its own, is revocable from the admin UI, and
names a real person in every audit event it produces.

Two deliberate choices about handling the token:

* **It is not saved anywhere by default.** The command prints an `export` line
  and stops. `--save` writes it to `~/.querygate/token` at mode 0600, and says
  so; nothing writes a credential to disk without being asked.
* **It is never echoed into a shell history or a log by this tool.** The token
  goes to stdout exactly once, so `querygate-login --quiet` can be captured by
  a command substitution without the surrounding prose.

Exit codes: 0 success, 1 refused or timed out, 2 usage/connection error.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

import httpx

DEFAULT_CLIENT_NAME = "querygate-cli"
DEFAULT_TIMEOUT_SECONDS = 15.0
# A floor under whatever the server asks for, so a misconfigured `interval`
# cannot turn this into a hot loop against someone's gateway.
MIN_POLL_INTERVAL_SECONDS = 1.0
TOKEN_FILE = Path.home() / ".querygate" / "token"


class LoginError(Exception):
    """A failure worth printing plainly. Never carries a token or a code."""


def _api_base(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LoginError(f"--url must be an absolute http(s) URL; got {url!r}")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        # A device code and the token it becomes are bearer credentials. Sending
        # them over cleartext to anything but loopback is a mistake worth
        # refusing rather than warning about.
        raise LoginError(
            "Refusing to send a credential over plain http to a non-loopback host. "
            "Use https, or --insecure if this is a local test server on another port."
        )
    return url.rstrip("/") + "/api/v1"


def _post(client: httpx.Client, path: str, payload: Dict[str, Any]) -> Tuple[int, Any]:
    try:
        response = client.post(path, json=payload)
    except httpx.HTTPError as exc:
        raise LoginError(f"Could not reach QueryGate ({type(exc).__name__}).") from exc
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


def start(client: httpx.Client, *, client_name: str, scopes: list[str]) -> Dict[str, Any]:
    status, body = _post(
        client, "/auth/device/code", {"client_name": client_name, "scopes": scopes}
    )
    if status == 404:
        raise LoginError(
            "This deployment has not enabled the device grant "
            "(SSO_ENABLED and SSO_DEVICE_GRANT_ENABLED)."
        )
    if status != 200 or not isinstance(body, dict):
        raise LoginError(f"QueryGate refused the sign-in request (HTTP {status}).")
    return body


def poll(
    client: httpx.Client, *, device_code: str, interval: float, expires_in: float
) -> Dict[str, Any]:
    """Poll until approved, denied, or expired. Honours `slow_down`."""
    wait = max(MIN_POLL_INTERVAL_SECONDS, interval)
    deadline = time.monotonic() + expires_in
    while time.monotonic() < deadline:
        time.sleep(wait)
        status, body = _post(client, "/auth/device/token", {"device_code": device_code})
        if status == 200 and isinstance(body, dict):
            return body
        error = ""
        if isinstance(body, dict):
            detail = body.get("detail")
            if isinstance(detail, dict):
                error = str(detail.get("error", ""))
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            # The server is telling us we are asking too often. Back off rather
            # than keep the same cadence and be told again.
            wait += 5
            continue
        if error == "access_denied":
            raise LoginError("The sign-in request was denied.")
        if error == "expired_token":
            raise LoginError("The sign-in request expired before it was approved.")
        raise LoginError(f"QueryGate refused the token request (HTTP {status}).")
    raise LoginError("Timed out waiting for approval.")


def save_token(token: str, path: Path = TOKEN_FILE) -> Path:
    """Write the token 0600, creating its directory with the same restriction."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)
    # Create with the right mode from the start: writing then chmod-ing leaves a
    # window in which the file is world-readable.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    os.chmod(path, 0o600)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="querygate-login",
        description=(
            "Sign in to QueryGate as yourself and receive a short-lived token. "
            "Approval happens in a browser; nothing is stored unless you ask."
        ),
    )
    parser.add_argument("--url", required=True, help="QueryGate base URL, e.g. https://qg.internal")
    parser.add_argument(
        "--client-name",
        default=DEFAULT_CLIENT_NAME,
        help="How this tool identifies itself on the approval screen.",
    )
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        dest="scopes",
        metavar="SCOPE",
        help=(
            "Request a specific scope (repeatable). Omit to inherit the approver's "
            "scopes. You can never receive more than they hold."
        ),
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help=f"Write the token to {TOKEN_FILE} (mode 0600) instead of only printing it.",
    )
    parser.add_argument("--no-browser", action="store_true", help="Do not try to open a browser.")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only the token on success, for command substitution.",
    )
    parser.add_argument("--json", action="store_true", help="Print the token response as JSON.")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Permit a plain-http URL to a non-loopback host. For local testing only.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    note = (lambda *m: None) if args.quiet else (lambda *m: print(*m, file=sys.stderr))

    try:
        base = args.url.rstrip("/") + "/api/v1" if args.insecure else _api_base(args.url)
        with httpx.Client(base_url=base, timeout=DEFAULT_TIMEOUT_SECONDS) as client:
            started = start(client, client_name=args.client_name, scopes=args.scopes)
            verification = started.get("verification_uri_complete") or started.get(
                "verification_uri", ""
            )
            note("")
            note(f"  Your code:  {started['user_code']}")
            note(f"  Approve at: {verification}")
            note("")
            note("  Waiting for approval… (Ctrl-C to cancel)")
            if not args.no_browser:
                try:
                    webbrowser.open(verification)
                except Exception:  # pragma: no cover - platform dependent
                    # Headless box, no DISPLAY, no default handler — none of
                    # which is a failure: the code and the URL are already on
                    # screen. Say so rather than swallowing it silently.
                    note("  (could not open a browser — open the URL above yourself)")
            issued = poll(
                client,
                device_code=started["device_code"],
                interval=float(started.get("interval", 5)),
                expires_in=float(started.get("expires_in", 600)),
            )
    except LoginError as exc:
        print(f"querygate-login: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\nquerygate-login: cancelled.", file=sys.stderr)
        return 1

    token = issued["access_token"]
    scope = issued.get("scope", "")
    if args.json:
        print(json.dumps(issued))
        return 0
    if args.quiet:
        print(token)
        return 0

    note("")
    note(f"  Signed in. Scopes: {scope or '(none — governed by policy only)'}")
    note(f"  Expires in {issued.get('expires_in', '?')} seconds.")
    if args.save:
        path = save_token(token)
        note(f"  Saved to {path} (mode 0600).")
        return 0
    note("")
    print(f"export QUERYGATE_TOKEN={token}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
