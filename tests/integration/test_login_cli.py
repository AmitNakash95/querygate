"""`querygate-login` driven against a real server, approval and all.

The CLI is the half of the device grant a human actually touches, and the half
whose failure modes are all about *waiting*: a poll that never backs off, an
approval that arrives after the code expired, a denial that reads like an
outage. So this drives the real binary path against a real uvicorn server, with
a background thread playing the human who clicks approve.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest
import uvicorn

from querygate.api.app import create_app
from querygate.core.config import AppConfig
from querygate.identity.config_store import IdentityConfigStore, set_identity_store
from querygate.login_cli import LoginError, _api_base, main, poll, save_token, start

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


PORT = _free_port()
BASE_URL = f"http://127.0.0.1:{PORT}"
API = f"{BASE_URL}/api/v1"

_CONFIG = {
    "sso": {"base_url": BASE_URL, "default_landing_path": "/admin/"},
    "providers": [{"id": "dev", "kind": "dev"}],
    "mapping": {
        "rules": [
            {
                "provider": "dev",
                "claim": "groups",
                "equals": "platform",
                "grant_roles": ["Operator"],
                "description": "Platform on-call",
            }
        ]
    },
}


@pytest.fixture(scope="module")
def server() -> Iterator[None]:
    set_identity_store(IdentityConfigStore.from_dict(_CONFIG))
    app = create_app(
        AppConfig(
            environment="localhost",
            api_v1_prefix="/api/v1",
            sso_enabled=True,
            dev_idp_enabled=True,
            sso_device_grant_enabled=True,
            sso_session_cookie_secure=False,
            mcp_enabled=False,
            health_check_interval_seconds=3600,
        )
    )
    running = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not running.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert running.started, "the test server did not start"
    try:
        yield
    finally:
        running.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _identity(server: None) -> None:
    # The conftest reset empties the identity store before each test, and the
    # server thread reads it per request.
    set_identity_store(IdentityConfigStore.from_dict(_CONFIG))


def _approve(user_code: str, *, decision: str = "approve", persona: str = "dev-1") -> None:
    """Play the human: sign in through the dev provider, then approve or deny.

    Runs on a timer thread while the CLI blocks in `poll`, which is the real
    shape of the flow — the tool waits, a person acts elsewhere.
    """
    human = httpx.Client(base_url=BASE_URL, follow_redirects=False, timeout=15)
    try:
        login = human.get(
            "/api/v1/auth/sso/login", params={"provider": "dev", "return_to": "/admin/"}
        )
        picked = human.get(f"{login.headers['location']}&persona={persona}")
        human.get(picked.headers["location"])
        csrf = human.get("/api/v1/auth/session").json()["csrf_token"]
        response = human.post(
            f"/api/v1/auth/device/{decision}",
            json={"user_code": user_code},
            headers={"X-QueryGate-CSRF": csrf},
        )
        assert response.status_code == 200, response.text
    finally:
        human.close()


class TestDeviceLogin:
    def test_the_cli_receives_a_token_carrying_the_approvers_identity(self):
        with httpx.Client(base_url=API, timeout=15) as client:
            started = start(client, client_name="querygate-cli", scopes=[])
            assert "-" in started["user_code"]

            approver = threading.Timer(0.3, _approve, args=(started["user_code"],))
            approver.start()
            try:
                issued = poll(
                    client,
                    device_code=started["device_code"],
                    interval=1,
                    expires_in=20,
                )
            finally:
                approver.cancel()

        assert issued["token_type"] == "Bearer"
        assert "admin:reload-config" in issued["scope"]

        # The token must work as a plain bearer credential with no cookie.
        with httpx.Client(base_url=API, timeout=15) as bare:
            response = bare.get(
                "/help/my-access",
                headers={"Authorization": f"Bearer {issued['access_token']}"},
            )
            assert response.status_code == 200
            assert response.json()["principal"] == "dev-1"

    def test_a_request_for_more_than_the_approver_holds_is_narrowed(self):
        with httpx.Client(base_url=API, timeout=15) as client:
            started = start(client, client_name="cli", scopes=["admin:config:write"])
            approver = threading.Timer(0.3, _approve, args=(started["user_code"],))
            approver.start()
            try:
                issued = poll(client, device_code=started["device_code"], interval=1, expires_in=20)
            finally:
                approver.cancel()
        # The Operator bundle does not include admin:config:write.
        assert issued["scope"] == ""

    def test_a_denial_is_reported_as_a_denial_not_an_outage(self):
        with httpx.Client(base_url=API, timeout=15) as client:
            started = start(client, client_name="cli", scopes=[])
            denier = threading.Timer(
                0.3, _approve, args=(started["user_code"],), kwargs={"decision": "deny"}
            )
            denier.start()
            try:
                with pytest.raises(LoginError, match="denied"):
                    poll(client, device_code=started["device_code"], interval=1, expires_in=20)
            finally:
                denier.cancel()

    def test_waiting_past_the_deadline_times_out_cleanly(self):
        with httpx.Client(base_url=API, timeout=15) as client:
            started = start(client, client_name="cli", scopes=[])
            with pytest.raises(LoginError, match="Timed out"):
                poll(client, device_code=started["device_code"], interval=1, expires_in=1.5)

    def test_main_prints_an_export_line_and_never_the_bare_token(self, capsys):
        code_holder: dict = {}

        def _approve_when_ready() -> None:
            for _ in range(100):
                if code_holder.get("user_code"):
                    _approve(code_holder["user_code"])
                    return
                time.sleep(0.05)

        original_start = start

        import querygate.login_cli as cli

        def _capturing_start(client, **kwargs):
            started = original_start(client, **kwargs)
            code_holder["user_code"] = started["user_code"]
            return started

        cli.start = _capturing_start
        watcher = threading.Thread(target=_approve_when_ready, daemon=True)
        watcher.start()
        try:
            exit_code = main(["--url", BASE_URL, "--no-browser"])
        finally:
            cli.start = original_start
            watcher.join(timeout=5)

        assert exit_code == 0
        out = capsys.readouterr()
        # The in-process test server shares this stdout, so look at the CLI's own
        # last line rather than the whole stream.
        lines = [line for line in out.out.splitlines() if line.strip()]
        assert lines[-1].startswith("export QUERYGATE_TOKEN=qgd_")
        # Exactly one line carries the token, and it is that one — everything
        # else the CLI prints goes to stderr, so `$(querygate-login --quiet)`
        # yields a bare token.
        assert sum("qgd_" in line for line in lines) == 1
        assert "qgd_" not in out.err


class TestTokenHandling:
    def test_save_writes_the_token_owner_only(self, tmp_path):
        import stat as stat_module

        target = tmp_path / "nested" / "token"
        save_token("qgd_example-token", target)
        assert target.read_text().strip() == "qgd_example-token"
        assert stat_module.S_IMODE(target.stat().st_mode) == 0o600
        assert stat_module.S_IMODE(target.parent.stat().st_mode) == 0o700

    def test_an_existing_world_readable_token_file_is_tightened(self, tmp_path):
        """Covers the overwrite case, which `os.open`'s mode does not.

        `O_CREAT` only applies its mode when it creates the file, so replacing a
        token that was previously written 0644 needs the explicit chmod.
        """
        import stat as stat_module

        target = tmp_path / "token"
        target.write_text("stale")
        target.chmod(0o644)
        save_token("qgd_fresh-token", target)
        assert stat_module.S_IMODE(target.stat().st_mode) == 0o600

    def test_the_file_is_created_restricted_rather_than_tightened_afterwards(self):
        """The no-window property, which cannot be observed after the fact.

        Writing then chmod-ing leaves an interval in which the token is
        world-readable. Nothing can detect that interval from outside, so the
        guard is a source-level assertion that `os.open` is given the mode —
        the same posture as the Lua guard on the Redis stores.
        """
        import ast
        import inspect

        import querygate.login_cli as cli

        tree = ast.parse(Path(inspect.getfile(cli)).read_text())
        opens = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "open"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ]
        assert opens, "save_token no longer uses os.open — re-check the mode argument"
        for call in opens:
            assert len(call.args) == 3, "os.open must be given an explicit mode"
            assert isinstance(call.args[2], ast.Constant)
            assert call.args[2].value == 0o600, (
                f"os.open mode is {oct(call.args[2].value)}; the token file must be "
                "created owner-only, not widened and then tightened."
            )

    def test_nothing_is_written_unless_save_was_asked_for(self, tmp_path, monkeypatch, capsys):
        """The default must leave no credential on disk."""
        import querygate.login_cli as cli

        monkeypatch.setattr(cli, "TOKEN_FILE", tmp_path / "token")
        code_holder: dict = {}
        original_start = cli.start

        def _capturing_start(client, **kwargs):
            started = original_start(client, **kwargs)
            code_holder["user_code"] = started["user_code"]
            return started

        def _approve_when_ready() -> None:
            for _ in range(100):
                if code_holder.get("user_code"):
                    _approve(code_holder["user_code"])
                    return
                time.sleep(0.05)

        cli.start = _capturing_start
        watcher = threading.Thread(target=_approve_when_ready, daemon=True)
        watcher.start()
        try:
            assert main(["--url", BASE_URL, "--no-browser", "--quiet"]) == 0
        finally:
            cli.start = original_start
            watcher.join(timeout=5)
        capsys.readouterr()
        assert not (tmp_path / "token").exists()


class TestUrlHandling:
    def test_a_cleartext_non_loopback_url_is_refused_before_any_request(self):
        """Asserted on `_api_base`, not on `main`'s exit code.

        Going through `main` would exit 1 either way — with the guard removed it
        simply fails to connect to the host instead — so the exit code proves
        nothing. The message is what distinguishes "refused to send a credential
        in cleartext" from "that host is down".
        """
        with pytest.raises(LoginError, match="Refusing to send a credential"):
            _api_base("http://gateway.internal")
        with pytest.raises(LoginError, match="Refusing to send a credential"):
            _api_base("http://10.0.0.5:8000")
        # Loopback is the one exception, for a local server.
        assert _api_base("http://127.0.0.1:8000") == "http://127.0.0.1:8000/api/v1"
        assert _api_base("https://qg.example.com") == "https://qg.example.com/api/v1"

    def test_main_surfaces_the_cleartext_refusal_as_a_failure(self):
        assert main(["--url", "http://gateway.internal", "--no-browser"]) == 1

    def test_a_deployment_without_the_device_grant_says_so(self):
        """A 404 here means "not enabled", not "wrong URL" — and the message
        has to say which, or an operator debugs the wrong thing."""
        no_grant_port = _free_port()
        set_identity_store(IdentityConfigStore.from_dict(_CONFIG))
        app = create_app(
            AppConfig(
                environment="localhost",
                sso_enabled=True,
                sso_device_grant_enabled=False,
                mcp_enabled=False,
                health_check_interval_seconds=3600,
            )
        )
        running = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=no_grant_port, log_level="error")
        )
        thread = threading.Thread(target=running.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while not running.started and time.monotonic() < deadline:
            time.sleep(0.05)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{no_grant_port}/api/v1", timeout=15) as c:
                with pytest.raises(LoginError, match="SSO_DEVICE_GRANT_ENABLED"):
                    start(c, client_name="cli", scopes=[])
        finally:
            running.should_exit = True
            thread.join(timeout=10)
