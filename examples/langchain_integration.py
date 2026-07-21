"""Wire QueryGate's MCP server into a LangChain / LangGraph agent.

Same shape as `examples/claude_agent_sdk_integration.py` (TODO.md item 20),
for a second framework (item 52): register QueryGate as a Streamable-HTTP MCP
server, load its tools as LangChain tools, and let a LangGraph ReAct agent
discover + call them itself. No raw SQL is ever constructed — every tool call
is a validated `StructuredQuery` handled by QueryGate.

Setup:
  pip install langchain-mcp-adapters langgraph "langchain[anthropic]"
  export ANTHROPIC_API_KEY=...          # or use any other LangChain chat model

  # In another terminal, start QueryGate with MCP enabled:
  MCP_ENABLED=true poetry run uvicorn querygate.api.app:app
  # (defaults to examples/connections.example.yaml + examples/policy.example.yaml —
  # see README.md for QUERYGATE_DEMO_DB_URL / docker compose up)

Run:
  python examples/langchain_integration.py "your question about the demo data"

What is / isn't verified: the QueryGate side of this — that the MCP server
lists exactly these tools over Streamable HTTP and that they execute validated
structured queries — is covered by tests/integration/test_mcp_server.py, and
`QUERYGATE_TOOLS` below is asserted against the live tool list by
tests/integration/test_integration_examples.py. The LangChain/LangGraph glue
and the live model call are *not* exercised in CI (no model key, and we add no
maintained framework SDK layer of our own — see item 20's warning); this file
is a runnable, framework-idiomatic reference, not a tested code path. The
framework calls below are written against the `langchain-mcp-adapters` /
`langgraph` API as of this file's authoring; if a newer version has moved a
name, consult that package's own MCP docs — the QueryGate side is unchanged.
"""

from __future__ import annotations

import asyncio
import sys

QUERYGATE_MCP_URL = "http://localhost:8000/mcp"
# Once MCP_API_KEYS is configured, add an Authorization header to the server
# entry below: "headers": {"Authorization": f"Bearer {API_KEY}"}.

# The QueryGate MCP tools this agent is allowed to call — least privilege, the
# same idea as item 20's allowed_tools. langchain-mcp-adapters names each tool
# by its bare server name (no mcp__server__ prefix), so these are the raw names
# QueryGate registers. Kept at module scope (and the framework imported lazily
# in main()) so the drift test can import this list without installing LangChain.
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


async def main() -> None:
    question = " ".join(sys.argv[1:]) or (
        "Using the 'demo' connection, list the 3 highest-value completed orders."
    )

    # Imported here, not at module top, so QUERYGATE_TOOLS stays importable for
    # the drift test without LangChain installed (see module docstring).
    from langchain.chat_models import init_chat_model
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langgraph.prebuilt import create_react_agent

    client = MultiServerMCPClient(
        {
            "querygate": {
                "transport": "streamable_http",
                "url": QUERYGATE_MCP_URL,
            }
        }
    )
    all_tools = await client.get_tools()
    # Least privilege: only expose the allow-listed QueryGate tools to the agent.
    allowed = set(QUERYGATE_TOOLS)
    tools = [tool for tool in all_tools if tool.name in allowed]

    model = init_chat_model("anthropic:claude-sonnet-5")
    agent = create_react_agent(model, tools, prompt=SYSTEM_PROMPT)

    result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})
    # The final assistant message is the last item in the returned message list.
    print(result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())
