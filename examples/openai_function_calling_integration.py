"""Wire QueryGate's MCP tools into OpenAI Chat Completions function-calling.

Same shape as `examples/claude_agent_sdk_integration.py` (TODO.md item 20),
for a fourth framework (item 52). OpenAI's classic function-calling doesn't
speak MCP natively, so this example bridges the two directly: it discovers
QueryGate's tools over Streamable HTTP with the MCP client SDK (already a
QueryGate dependency), converts each into an OpenAI function schema, runs the
chat loop, and dispatches any tool call the model makes back to QueryGate. No
raw SQL is ever constructed — every dispatched call is a validated
`StructuredQuery` handled by QueryGate.

The MCP -> OpenAI conversion (`mcp_tool_to_openai_function`) is a pure function,
kept at module scope and unit-tested against QueryGate's real tool schemas in
tests/integration/test_integration_examples.py.

Setup:
  pip install openai                    # the MCP client SDK ships with QueryGate
  export OPENAI_API_KEY=...

  # In another terminal, start QueryGate with MCP enabled:
  MCP_ENABLED=true poetry run uvicorn querygate.api.app:app
  # (defaults to examples/connections.example.yaml + examples/policy.example.yaml —
  # see README.md for QUERYGATE_DEMO_DB_URL / docker compose up)

Run:
  python examples/openai_function_calling_integration.py "your question about the demo data"

What is / isn't verified: the QueryGate side — tool discovery over Streamable
HTTP and validated structured execution — is covered by
tests/integration/test_mcp_server.py, `QUERYGATE_TOOLS` is asserted against the
live tool list, and `mcp_tool_to_openai_function` is unit-tested against the
real tool schemas (all in tests/integration/test_integration_examples.py). The
OpenAI SDK glue and the live model call are *not* exercised in CI (no model
key); this file is a runnable, framework-idiomatic reference.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Dict, List, Optional

QUERYGATE_MCP_URL = "http://localhost:8000/mcp"
# Once MCP_API_KEYS is configured, pass an Authorization header to
# streamablehttp_client below: headers={"Authorization": f"Bearer {API_KEY}"}.

# The QueryGate MCP tools this agent is allowed to call — least privilege, the
# same idea as item 20's allowed_tools. These are the bare names QueryGate
# registers over MCP.
QUERYGATE_TOOLS = [
    "list_connections",
    "list_tables",
    "describe_table",
    "run_structured_queries",
]

SYSTEM_PROMPT = (
    "You can query databases through QueryGate's MCP tools. QueryGate has no "
    "raw-SQL tool — every query is a validated StructuredQuery JSON AST. Discover "
    "schemas with list_tables/describe_table before writing a query; never guess "
    "table or column names."
)


def mcp_tool_to_openai_function(
    name: str,
    description: Optional[str],
    input_schema: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Convert one MCP tool definition into an OpenAI function-calling tool schema.

    Pure and framework-free: an MCP tool's ``inputSchema`` is already a JSON
    Schema object, which is exactly what OpenAI's ``function.parameters`` field
    expects, so the mapping is a direct lift. A tool with no input schema becomes
    an empty-object parameter schema (a valid no-argument function).
    """
    parameters = input_schema or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or "",
            "parameters": parameters,
        },
    }


def _text_from_tool_result(result: Any) -> str:
    """Join the text content blocks of an MCP `call_tool` result."""
    return "".join(block.text for block in result.content if getattr(block, "type", None) == "text")


async def discover_openai_tools(
    session: Any, allowed: Optional[set] = None
) -> List[Dict[str, Any]]:
    """List the MCP server's tools through `session` and return the allow-listed
    ones as OpenAI function-calling tool schemas.

    `session` is any initialized MCP `ClientSession` (real or a test double);
    keeping the transport out of this function is what lets it be exercised
    against a live server without a model — see
    tests/integration/test_integration_examples.py.
    """
    allow = set(QUERYGATE_TOOLS) if allowed is None else allowed
    listed = await session.list_tools()
    return [
        mcp_tool_to_openai_function(tool.name, tool.description, tool.inputSchema)
        for tool in listed.tools
        if tool.name in allow
    ]


async def run_tool_calling_loop(
    openai_client: Any,
    session: Any,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    *,
    model: str = "gpt-4o",
) -> str:
    """Drive the chat loop until the model returns a final answer, dispatching
    every tool call it makes back to QueryGate over `session`.

    Returns the model's final text. `openai_client` is duck-typed
    (`chat.completions.create(...)`) so the loop's message-accumulation and
    tool-dispatch mechanics can be tested with a scripted client and no API key.
    """
    while True:
        completion = openai_client.chat.completions.create(
            model=model, messages=messages, tools=tools
        )
        choice = completion.choices[0].message
        if not choice.tool_calls:
            return choice.content or ""

        messages.append(choice.model_dump(exclude_none=True))
        for call in choice.tool_calls:
            args = json.loads(call.function.arguments or "{}")
            print(f"[calling {call.function.name} with {args}]", file=sys.stderr)
            result = await session.call_tool(call.function.name, args)
            # Hand the tool's text content back to the model as the result.
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": _text_from_tool_result(result),
                }
            )


async def main() -> None:
    question = " ".join(sys.argv[1:]) or (
        "Using the 'demo' connection, list the 3 highest-value completed orders."
    )

    # Lazy imports: openai is user-installed; the MCP client SDK ships with
    # QueryGate but stays out of the module top so this file mirrors the others.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from openai import OpenAI

    async with streamablehttp_client(QUERYGATE_MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            openai_tools = await discover_openai_tools(session)

            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ]
            answer = await run_tool_calling_loop(OpenAI(), session, messages, openai_tools)
            print(answer)


if __name__ == "__main__":
    asyncio.run(main())
