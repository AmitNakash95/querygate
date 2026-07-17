"""Application-level settings.

No database connection strings live here — those belong to individual
connection profiles (see querygate/connections/), loaded from a separate
file so secrets never mix with app config or get serialized into a schema.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import pydantic as pyd
from pydantic_settings import BaseSettings


def _parse_str_list(value: Any) -> Any:
    """Accept a JSON array, a comma-separated string, or a list as-is."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        if value.startswith("["):
            return json.loads(value)
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class AppConfig(BaseSettings):
    environment: Literal["production", "staging", "development", "localhost"] = pyd.Field(
        default="localhost"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = pyd.Field(default="INFO")
    host_address: str = pyd.Field(default="0.0.0.0")
    port: int = pyd.Field(default=8000)
    app_version: str = pyd.Field(default="0.1.0")
    num_of_workers: int = pyd.Field(default=1)
    api_v1_prefix: str = pyd.Field(default="/api/v1")

    # Connection profiles and policy are file-configured, not code-configured —
    # see examples/connections.example.yaml and examples/policy.example.yaml.
    # Connection strings are resolved via ${ENV_VAR} interpolation inside the
    # connections file, so secrets live only in the environment.
    connections_file: str = pyd.Field(default="examples/connections.example.yaml")
    policy_file: str = pyd.Field(default="examples/policy.example.yaml")

    # REST and MCP share one API-key authenticator (see core/auth.py). Both
    # allow an anonymous dev-bypass outside production when no keys are set.
    api_keys: list[str] = pyd.Field(
        default_factory=list, validation_alias=pyd.AliasChoices("API_KEYS", "api_keys")
    )
    api_key_subject: str = pyd.Field(default="api-key-client")

    mcp_enabled: bool = pyd.Field(default=False)
    mcp_mount_path: str = pyd.Field(default="/mcp")
    mcp_api_keys: list[str] = pyd.Field(
        default_factory=list, validation_alias=pyd.AliasChoices("MCP_API_KEYS", "mcp_api_keys")
    )
    mcp_api_key_subject: str = pyd.Field(default="mcp-service-account")

    # Engine pool defaults, shared across connections (per-connection timeout /
    # concurrency guardrails live in policy, not here).
    pool_size: int = pyd.Field(default=20)
    conn_max_overflow: int = pyd.Field(default=10)
    pool_timeout: int = pyd.Field(default=30)
    pool_recycle: int = pyd.Field(default=3600)
    odbc_driver: str = pyd.Field(default="ODBC+Driver+17+for+SQL+Server")
    db_trust_server_certificate: bool = pyd.Field(default=False)

    @pyd.field_validator("api_keys", "mcp_api_keys", mode="before")
    @classmethod
    def _parse_key_lists(cls, value: Any) -> Any:
        return _parse_str_list(value)

    @pyd.model_validator(mode="after")
    def _validate_production_auth(self) -> "AppConfig":
        if self.environment == "production" and not self.api_keys:
            raise ValueError("API_KEYS must be set when ENVIRONMENT=production")
        if self.environment == "production" and self.mcp_enabled and not self.mcp_api_keys:
            raise ValueError(
                "MCP_API_KEYS must be set when ENVIRONMENT=production and MCP_ENABLED=true"
            )
        return self

    @property
    def is_local(self) -> bool:
        return self.environment in ("localhost", "development")

    class Config:
        case_sensitive = False
        env_file = ".env"
        extra = "allow"


config = AppConfig()
