"""Tests asserting connection credentials can never leak through a schema,
response, or error — REST OpenAPI schema, MCP tool schemas, and Pydantic
validation errors are all checked.
"""

from __future__ import annotations

import json

import pytest

from querygate.api.app import create_app
from querygate.connections.models import ConnectionProfile, PublicConnectionInfo
from querygate.core.config import AppConfig

_SECRET = "postgresql+asyncpg://user:s3cr3t-password@host/db"


def test_public_connection_info_has_no_connection_string_field():
    assert "connection_string" not in PublicConnectionInfo.model_fields


def test_connection_profile_validation_error_does_not_leak_secret():
    """A malformed profile's Pydantic ValidationError must not echo back a
    connection string that was otherwise valid.
    """
    with pytest.raises(Exception) as exc_info:
        ConnectionProfile.model_validate(
            {"id": "bad id with spaces", "dialect": "postgresql", "connection_string": _SECRET}
        )
    assert _SECRET not in str(exc_info.value)


def test_openapi_schema_never_mentions_connection_string():
    settings = AppConfig(environment="localhost", api_v1_prefix="/api/v1")
    app = create_app(settings)
    schema = app.openapi()
    serialized = json.dumps(schema)
    assert "connection_string" not in serialized
    assert _SECRET not in serialized


def test_mcp_tool_schemas_never_mention_connection_string():
    from querygate.mcp.server import create_mcp_server

    server = create_mcp_server()
    for tool in server._tool_manager._tools.values():
        serialized = json.dumps(tool.parameters) + json.dumps(
            getattr(tool, "output_schema", None) or {}
        )
        assert "connection_string" not in serialized
