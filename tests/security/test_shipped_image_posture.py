"""What `docker run <registry>/querygate:latest` actually gets you.

TODO.md item 215 calls this the single highest-severity item in the phase, and
the reason is worth stating plainly rather than filing under hardening:

Before this, the exact one-command install the product promises produced a
deployment where **every request resolved to an anonymous principal**. The image
set no `ENVIRONMENT`; the default is `localhost`; `api/auth.py` appends
`AnonymousAuthenticator` when nothing real is configured; and
`_validate_production_auth` only fires on `production`. So the first connection
an operator added was queryable by anyone who could reach the port — with
FastAPI's debug traceback page on and the OpenAPI schema public.

Every test here is written against **the image's own defaults**, not against a
hand-built config, because the defect was precisely that the defaults were wrong
while every configured path was fine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from querygate.api.auth import build_authenticator
from querygate.core.auth import AnonymousAuthenticator, CompositeAuthenticator
from querygate.core.config import AppConfig

pytestmark = [pytest.mark.security, pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[2]

#: The image's posture, with **nothing else configured** — the state a fresh
#: `docker run` with no flags produces.
_IMAGE_DEFAULTS = dict(hardened_image=True, api_keys=[], jwt_enabled=False, sso_enabled=False)


def _authenticators(cfg: AppConfig) -> list[str]:
    built = build_authenticator(cfg)
    parts = built._authenticators if isinstance(built, CompositeAuthenticator) else [built]
    return [type(part).__name__ for part in parts]


def test_the_shipped_image_has_no_anonymous_authenticator():
    """The one that matters. Nothing configured, and still no bypass."""
    assert AnonymousAuthenticator.__name__ not in _authenticators(AppConfig(**_IMAGE_DEFAULTS))


@pytest.mark.parametrize("environment", ["localhost", "development", "staging"])
def test_no_environment_setting_re_opens_the_bypass(environment):
    """The flag is separate from `ENVIRONMENT` for exactly this reason.

    An operator debugging a containerised deployment may legitimately set
    `ENVIRONMENT=development` for readable logs. That must not also hand them an
    anonymous-auth bypass — which folding this into `is_local` would have done.
    """
    cfg = AppConfig(**{**_IMAGE_DEFAULTS, "environment": environment})
    assert AnonymousAuthenticator.__name__ not in _authenticators(cfg)


def test_local_development_keeps_its_bypass():
    """The negative control, and it is load-bearing.

    A fix that also broke `poetry run querygate` on a laptop would be reverted
    within a day, and the bypass would come back with it. Outside the image,
    with nothing configured, the dev convenience stays.
    """
    cfg = AppConfig(environment="localhost", api_keys=[], jwt_enabled=False, sso_enabled=False)
    assert AnonymousAuthenticator.__name__ in _authenticators(cfg)


def test_configuring_a_real_credential_still_removes_the_bypass_outside_the_image():
    """Pre-existing behaviour, re-pinned: configuring auth in dev must actually
    require it, or configuring auth would silently do nothing."""
    cfg = AppConfig(environment="localhost", api_keys=["k"], jwt_enabled=False)
    assert AnonymousAuthenticator.__name__ not in _authenticators(cfg)


def test_the_dockerfile_actually_sets_the_flag():
    """The code above is only true of the image if the image sets the flag.

    A source-level assertion because nothing else in the suite builds the image:
    without this, deleting the ENV line would leave every test above green while
    the shipped artifact reverted to the anonymous default.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "HARDENED_IMAGE=1" in dockerfile, (
        "the Dockerfile no longer sets HARDENED_IMAGE, so the shipped image is "
        "back to resolving every request to an anonymous principal"
    )
    assert "VAR_DIR=/app/var" in dockerfile


def test_the_image_serves_no_openapi_schema_or_docs():
    """A public schema plus a debug traceback page is a reconnaissance surface
    an operator did not ask for by choosing a log level."""
    cfg = AppConfig(**{**_IMAGE_DEFAULTS, "environment": "development"})
    assert cfg.is_local is True  # the condition that used to be sufficient
    assert cfg.is_hardened_image is True
    # Mirrors the expressions in `api/app.py`; asserted here so a change to
    # either side has to be deliberate.
    assert not (cfg.is_local and not cfg.is_hardened_image)


def test_the_flag_defaults_off_outside_the_image():
    """It is the image's property, not a global default — a developer running
    from a checkout must not silently inherit the container's posture."""
    assert AppConfig().is_hardened_image is False


def test_production_with_no_credentials_refuses_to_start_at_all():
    """Stronger than "no bypass", and worth pinning separately.

    `_validate_production_auth` makes `ENVIRONMENT=production` with no API key
    and no JWT a hard configuration error rather than a running-but-open
    deployment. That guard predates this item; what it could not do was cover the
    image's *default* environment, which is why `HARDENED_IMAGE` exists
    alongside it rather than instead of it.
    """
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="API_KEYS or JWT_ENABLED"):
        AppConfig(**{**_IMAGE_DEFAULTS, "environment": "production"})
