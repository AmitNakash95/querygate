"""First-boot self-configuration: seed once, never overwrite, never regenerate.

The risk auto-configuration creates is a deployment that configures *itself* into
an insecure or unstable state, so each test here targets one specific way that
happens rather than testing "it works".
"""

from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

from querygate.bootstrap import BootstrapError, first_boot, read_admin_key
from querygate.policy.models import Policy

pytestmark = [pytest.mark.security, pytest.mark.unit]


def test_first_boot_seeds_config_and_generates_a_key(tmp_path):
    result = first_boot(tmp_path)
    assert set(result.seeded) == {"connections.yaml", "policy.yaml", "catalog.yaml"}
    assert result.generated_admin_key
    assert result.is_first_boot is True


def test_a_restart_regenerates_nothing(tmp_path):
    """Silently rotating a key on a rebuilt image turns a routine upgrade into
    an unplanned re-activation, and breaks item 213's re-activation guarantee.
    A restart must be a no-op."""
    first = first_boot(tmp_path)
    second = first_boot(tmp_path)
    assert second.seeded == ()
    assert second.generated_admin_key is None
    assert second.is_first_boot is False
    assert read_admin_key(tmp_path) == first.generated_admin_key


def test_first_boot_never_overwrites_an_existing_config(tmp_path):
    """An operator's edited policy must survive a restart. Seeding is
    create-if-absent, never a write."""
    (tmp_path / "policy.yaml").write_text("default: {enabled: false}\n", encoding="utf-8")
    first_boot(tmp_path)
    assert (tmp_path / "policy.yaml").read_text(encoding="utf-8") == "default: {enabled: false}\n"


def test_concurrent_first_boots_agree_on_one_key(tmp_path):
    """The race `O_EXCL` exists for.

    Concurrent workers and replicas start at the same instant. A
    read-then-write would let two of them each generate a key and one silently
    win — which presents as "the key in the log does not work", intermittently,
    on exactly the multi-replica deployments hardest to debug. Exactly one
    caller may report having generated it, and the file must match that one.
    """
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: first_boot(tmp_path), range(8)))

    generated = [r.generated_admin_key for r in results if r.generated_admin_key]
    assert len(generated) == 1, f"{len(generated)} workers each generated a key"
    assert read_admin_key(tmp_path) == generated[0]


def test_the_admin_key_file_is_owner_only(tmp_path):
    """A credential file that is group- or world-readable on a shared volume is
    a credential. Set explicitly, because `os.open`'s mode is masked by umask."""
    first_boot(tmp_path)
    mode = stat.S_IMODE(os.stat(tmp_path / "admin-api-key").st_mode)
    assert mode == 0o600, f"admin key is {oct(mode)}"


def test_the_key_is_not_derived_from_anything_guessable(tmp_path):
    """Two independent installs must not produce the same key. A predictable
    admin credential on a deployment that auto-configures is worse than no
    auto-configuration at all."""
    one = first_boot(tmp_path / "one").generated_admin_key
    two = first_boot(tmp_path / "two").generated_admin_key
    assert one != two
    assert len(one) >= 40


def test_the_starter_policy_denies_by_default(tmp_path):
    """The whole point of depending on item 220. A fresh install reaches nothing
    until an operator names a table deliberately."""
    first_boot(tmp_path)
    parsed = yaml.safe_load((tmp_path / "policy.yaml").read_text(encoding="utf-8"))
    assert parsed["default"]["enabled"] is True
    assert parsed["default"]["require_explicit_allowlist"] is True

    # And it means what it says once loaded into the real model.
    policy = Policy(**{k: v for k, v in parsed["default"].items()})
    assert policy.table_allowed("customers") is False


def test_the_starter_connections_file_is_empty_and_says_why(tmp_path):
    """Seeded empty rather than with an example: a commented-out example DSN is
    the thing an operator uncomments, and a literal connection string must never
    be persisted here."""
    first_boot(tmp_path)
    text = (tmp_path / "connections.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(text)["connections"] == {}
    assert "environment-variable reference" in text


def test_an_unwritable_volume_fails_loudly(tmp_path):
    """A silent failure here means a deployment that looks configured and is
    not. The message has to say what to do, since it appears once, at boot."""
    target = tmp_path / "readonly"
    target.mkdir()
    os.chmod(target, stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(BootstrapError, match="writable volume"):
            first_boot(target)
    finally:
        os.chmod(target, stat.S_IRWXU)


# --- the single-writer rule ---------------------------------------------------


def test_only_the_admin_service_and_bootstrap_write_connections_yaml():
    """Item 215: "do not add a second connections writer".

    Two writers means two sources of truth for which databases a gateway may
    reach, and the loser's changes vanish without an error. `bootstrap.py` seeds
    the file once when absent and never mutates it; `admin/service.py`'s
    stage/apply path owns every write after that. `admin/store.py` snapshots
    versions of it, which is a copy of the file, not a second writer of it.
    """
    import ast
    from pathlib import Path

    source_root = Path(__file__).resolve().parents[2] / "src" / "querygate"
    permitted = {
        # Seeds once when absent, never mutates. Item 215.
        "bootstrap.py",
        # The stage/apply path. THE writer of the live file.
        "admin/service.py",
        # Snapshots versions of the file — a copy, not a second writer of it.
        "admin/store.py",
        # Writes a connections.yaml into a THROWAWAY directory it creates for a
        # benchmark run, never the live config. Permitted deliberately rather
        # than by weakening the rule: a static check cannot tell "writes the
        # live file" from "writes a file of the same name in a temp dir", so the
        # exception is recorded here where a reviewer sees it.
        "load_benchmark.py",
    }

    writers = []
    for path in sorted(source_root.rglob("*.py")):
        rel = str(path.relative_to(source_root))
        if rel in permitted:
            continue
        text = path.read_text(encoding="utf-8")
        if "connections.yaml" not in text and "connections_yaml" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"write_text", "write_bytes"}:
                    writers.append(f"{rel}:{node.lineno}")
    assert not writers, (
        "a second connections.yaml writer appeared; every write after first boot "
        f"must go through admin/service.py's stage/apply path: {writers}"
    )


def test_the_generated_admin_key_actually_authenticates(tmp_path, monkeypatch):
    """The key first boot generates must be accepted by the running app.

    **This shipped broken.** Item 215 correctly closed the anonymous bypass for
    `is_hardened_image`, and first boot correctly generated a key and told the
    operator to copy it — but nothing ever put that key into `cfg.api_keys`.
    `read_admin_key` existed, documented itself as "used to satisfy production
    auth", and had unit tests, yet had no production caller. So
    `docker run <registry>/querygate:latest` produced a deployment whose
    `ApiKeyAuthenticator` held an EMPTY key list: every authenticated request
    401'd forever, while the startup log told the operator to copy a key that
    could never work.

    Nothing caught it because the release smoke sent no credential at all, so it
    never exercised auth — two defects that concealed each other. CI found it
    only once the smoke was fixed to authenticate.

    `API_KEYS` is forced empty here deliberately: the repository's own `.env`
    sets `API_KEYS='["admin"]'` for local development, and `AppConfig` is a
    `BaseSettings` that reads it. Under that value the operator-supplied key
    wins, `first_boot`'s key is never consulted, and this test passes while
    testing nothing — which is exactly what happened on the first attempt to
    verify the fix by hand.
    """
    from fastapi.testclient import TestClient

    from querygate.api.app import create_app
    from querygate.core.config import AppConfig

    monkeypatch.setenv("HARDENED_IMAGE", "1")
    monkeypatch.setenv("API_KEYS", "[]")
    monkeypatch.setenv("VAR_DIR", str(tmp_path))

    app = create_app(AppConfig())
    generated = (tmp_path / "admin-api-key").read_text(encoding="utf-8").strip()
    assert generated, "first boot generated no admin key in a hardened image"

    with TestClient(app) as client:
        anonymous = client.get("/api/v1/connections")
        assert anonymous.status_code == 401, (
            "the hardened image must not serve an unauthenticated caller; "
            f"got {anonymous.status_code}"
        )
        authenticated = client.get(
            "/api/v1/connections", headers={"Authorization": f"Bearer {generated}"}
        )
        assert authenticated.status_code == 200, (
            "the key first boot generated was refused by the app it configures — "
            f"got {authenticated.status_code}. The one-command install is unusable "
            "when this fails."
        )
