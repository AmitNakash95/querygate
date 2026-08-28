"""First-boot self-configuration: seed once, never overwrite, never regenerate.

The promise item 215 makes is "one command and it's set up". The danger it
creates is a deployment that configures *itself* into an insecure state, so every
mechanic here is chosen against a specific failure:

* **Secrets come from `secrets.token_urlsafe`**, never `random`, never derived
  from a hostname or a container id. A predictable admin key on a deployment that
  auto-configures is worse than no auto-configuration.
* **Written create-if-absent with `O_EXCL`, atomically, once.** Concurrent
  workers and replicas start simultaneously; a read-then-write would let two of
  them each generate a key and one silently win. `O_EXCL` makes the loser lose
  loudly and adopt what is already there.
* **Never regenerated when present.** Silently rotating on a rebuilt image turns
  a routine upgrade into an unplanned re-activation, and breaks item 213's
  re-activation guarantee. A restart must be a no-op.
* **Seeding happens before any store is constructed**, and only for files that do
  not exist. It never mutates an existing file — every subsequent write goes
  through `admin/service.py`'s stage/apply path, which is the single
  connections writer (asserted by `test_single_connections_writer`).
* **The starter policy denies.** It ships `require_explicit_allowlist: true`
  (item 220), so a fresh install reaches nothing until an operator names a table
  deliberately. Before item 220 the only deny-all lever was `enabled: false`,
  which is all-or-nothing and would have made the first query require turning
  the whole connection on.
"""

from __future__ import annotations

import logging
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: 0600 — readable only by the `querygate` user the image runs as. A credential
#: file that is group- or world-readable on a shared volume is a credential.
_SECRET_MODE = stat.S_IRUSR | stat.S_IWUSR
_CONFIG_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP

#: 32 bytes of `secrets` entropy, URL-safe so it survives an env var, a YAML
#: file and a copy-paste out of a container log without escaping.
_KEY_BYTES = 32

STARTER_CONNECTIONS = """\
# Seeded by QueryGate on first boot. Safe to edit, but prefer the admin UI —
# `admin/service.py`'s stage/apply path is the single writer for this file, and
# hand-edits are not version-tracked the way staged changes are.
#
# A connection string MUST be an environment-variable reference. A literal DSN
# is rejected at the boundary and never persisted: the config-version store
# returns this file verbatim under a *read* scope, so a literal here would
# launder a credential through an untyped string that
# `test_credential_redaction.py` cannot see.
connections: {}
"""

STARTER_POLICY = """\
# Seeded by QueryGate on first boot. DENY-BY-DEFAULT.
#
# `require_explicit_allowlist: true` (TODO.md item 220) means an empty
# `allowed_tables` denies EVERY table, and a table with no `allowed_columns`
# entry denies EVERY column. A fresh install therefore reaches nothing until you
# name what an agent may see — which is the opposite of the historical default,
# where enabling a connection made every table on it readable.
#
# To grant access, add the connection under `connections:` and name its tables:
#
#   connections:
#     my_db:
#       require_explicit_allowlist: true
#       allowed_tables: [customers, orders]
#       allowed_columns:
#         customers: [id, name, created_at]   # NOT email, NOT password_hash
default:
  enabled: true
  require_explicit_allowlist: true
  max_joins: 3
  max_select_columns: 50
  max_where_depth: 3
  default_limit: 100
  max_limit: 1000
  timeout_seconds: 30

connections: {}
"""

STARTER_CATALOG = """\
# Seeded empty by QueryGate on first boot. The catalog is a descriptive semantic
# overlay and never a query path. Every write goes through
# `CatalogFileRepository`'s lock — never through the config-version store, which
# keeps its own snapshot and would silently diverge from this file.
version: 1
connections: {}
"""


@dataclass(frozen=True)
class BootstrapResult:
    """What first boot did. Every field is false on a restart."""

    generated_admin_key: str | None
    seeded: tuple[str, ...]

    @property
    def is_first_boot(self) -> bool:
        return self.generated_admin_key is not None or bool(self.seeded)


def _write_once(path: Path, content: str, *, mode: int) -> bool:
    """Create `path` with `content`, or return False if it already exists.

    `O_CREAT | O_EXCL` rather than `if not path.exists(): write()`. Two workers
    starting at the same instant both pass the check in the second form, and one
    silently overwrites the other's freshly-generated secret — a race that shows
    up as "the key in the log does not work", intermittently, on exactly the
    multi-replica deployments hardest to debug.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    except FileExistsError:
        return False
    except OSError as exc:
        raise BootstrapError(
            f"cannot write {path} ({type(exc).__name__}). First boot needs a writable "
            "volume — mount one at /app/var and ensure the `querygate` user owns it."
        ) from exc
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        fh.write(content)
    # `os.open`'s mode is masked by umask; set it explicitly so a permissive
    # container umask cannot widen a secret file.
    os.chmod(path, mode)
    return True


class BootstrapError(Exception):
    """First boot cannot proceed. Fatal, and says what to do about it."""


def first_boot(var_dir: Path, *, config_dir: Path | None = None) -> BootstrapResult:
    """Seed configuration and an admin credential, exactly once.

    Idempotent by construction: every write is create-if-absent, so a restart,
    a rebuild, a scaled-out replica and a crash-loop all reach the same state
    without regenerating anything.
    """
    config_dir = config_dir or var_dir
    seeded: list[str] = []

    for name, content, mode in (
        ("connections.yaml", STARTER_CONNECTIONS, _CONFIG_MODE),
        ("policy.yaml", STARTER_POLICY, _CONFIG_MODE),
        ("catalog.yaml", STARTER_CATALOG, _CONFIG_MODE),
    ):
        if _write_once(config_dir / name, content, mode=mode):
            seeded.append(name)

    key_path = var_dir / "admin-api-key"
    generated: str | None = None
    candidate = secrets.token_urlsafe(_KEY_BYTES)
    if _write_once(key_path, candidate + "\n", mode=_SECRET_MODE):
        generated = candidate
        logger.warning(
            "QueryGate generated an admin API key on first boot and wrote it to %s. "
            "Copy it now and store it in your secret manager; it is not shown again.",
            key_path,
        )

    return BootstrapResult(generated_admin_key=generated, seeded=tuple(seeded))


def read_admin_key(var_dir: Path) -> str | None:
    """The key first boot generated, if any. Used to satisfy production auth."""
    try:
        return (var_dir / "admin-api-key").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
