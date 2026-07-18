"""Wire QueryGate's MCP server into the Claude Agent SDK.

This is the fastest path from "I have a database" to "an agent can query it
safely" — `examples/mcp_calls.md`/`examples/rest_calls.md` show the raw
JSON-RPC/curl shape, correct but not the quickest way to get an agent
framework talking to QueryGate. This registers QueryGate as an HTTP MCP
server and lets Claude discover + call its tools itself.

Setup:
  pip install claude-agent-sdk
  export ANTHROPIC_API_KEY=...          # skip if already set (e.g. inside Claude Code)

  # In another terminal, start QueryGate with MCP enabled:
  MCP_ENABLED=true poetry run uvicorn querygate.api.app:app
  # (defaults to examples/connections.example.yaml + examples/policy.example.yaml —
  # see README.md for QUERYGATE_DEMO_DB_URL / docker compose up)

Run:
  python examples/claude_agent_sdk_integration.py "your question about the demo data"

The MCP transport this relies on (tool discovery + tool invocation over
Streamable HTTP) is exercised by tests/integration/test_mcp_server.py and
was independently verified live during development using the `mcp` client
SDK directly, in addition to this script.
"""

from __future__ import annotations

import asyncio
import sys

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    ToolUseBlock,
)

QUERYGATE_MCP_URL = "http://localhost:8000/mcp"
# Once MCP_API_KEYS is configured, add: "headers": {"Authorization": f"Bearer {API_KEY}"}

# The SDK namespaces every tool from an MCP server as mcp__<server_name>__<tool_name> —
# "querygate" here is this dict's key, not QueryGate's own naming.
QUERYGATE_TOOLS = [
    "mcp__querygate__list_connections",
    "mcp__querygate__list_tables",
    "mcp__querygate__describe_table",
    "mcp__querygate__execute_structured_query",
    "mcp__querygate__execute_structured_queries",
    "mcp__querygate__explain_structured_query",
]


async def main() -> None:
    question = " ".join(sys.argv[1:]) or (
        "Using the 'demo' connection, list the 3 highest-value completed orders."
    )

    options = ClaudeAgentOptions(
        mcp_servers={"querygate": {"type": "http", "url": QUERYGATE_MCP_URL}},
        allowed_tools=QUERYGATE_TOOLS,
        system_prompt=(
            "You can query databases through QueryGate's MCP tools. QueryGate has no "
            "raw-SQL tool — every query is a validated StructuredQuery JSON AST. Discover "
            "schemas with list_tables/describe_table before writing a query; never guess "
            "table or column names."
        ),
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query(question)
        async for message in client.receive_response():
            if not isinstance(message, AssistantMessage):
                continue
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text)
                elif isinstance(block, ToolUseBlock):
                    print(f"[calling {block.name} with {block.input}]", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
