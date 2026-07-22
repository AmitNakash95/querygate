"""Wire QueryGate's MCP server into a LlamaIndex agent.

Same shape as `examples/claude_agent_sdk_integration.py` (TODO.md item 20),
for a third framework (item 52): register QueryGate as a Streamable-HTTP MCP
server, turn its tools into LlamaIndex FunctionTools, and let a FunctionAgent
discover + call them. No raw SQL is ever constructed — every tool call is a
validated `StructuredQuery` handled by QueryGate.

Setup:
  pip install llama-index-tools-mcp llama-index-llms-anthropic llama-index
  export ANTHROPIC_API_KEY=...          # or use any other LlamaIndex LLM

  # In another terminal, start QueryGate with MCP enabled:
  MCP_ENABLED=true poetry run uvicorn querygate.api.app:app
  # (defaults to examples/connections.example.yaml + examples/policy.example.yaml —
  # see README.md for QUERYGATE_DEMO_DB_URL / docker compose up)

Run:
  python examples/llamaindex_integration.py "your question about the demo data"

What is / isn't verified: the QueryGate side of this — that the MCP server
lists exactly these tools over Streamable HTTP and that they execute validated
structured queries — is covered by tests/integration/test_mcp_server.py, and
`QUERYGATE_TOOLS` below is asserted against the live tool list by
tests/integration/test_integration_examples.py. The LlamaIndex glue and the
live model call are *not* exercised in CI (no model key, and we add no
maintained framework SDK layer of our own — see item 20's warning); this file
is a runnable, framework-idiomatic reference, not a tested code path. The
framework calls below are written against the `llama-index-tools-mcp` API as of
this file's authoring; if a newer version has moved a name, consult that
package's own MCP docs — the QueryGate side is unchanged.
"""

from __future__ import annotations

import asyncio
import sys

QUERYGATE_MCP_URL = "http://localhost:8000/mcp"
# Once MCP_API_KEYS is configured, pass an Authorization header to BasicMCPClient
# below: BasicMCPClient(QUERYGATE_MCP_URL, headers={"Authorization": f"Bearer {API_KEY}"}).

# The QueryGate MCP tools this agent is allowed to call — least privilege, the
# same idea as item 20's allowed_tools. LlamaIndex's McpToolSpec names each tool
# by its bare server name, so these are the raw names QueryGate registers. Kept
# at module scope (framework imported lazily in main()) so the drift test can
# import this list without installing LlamaIndex.
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
    # the drift test without LlamaIndex installed (see module docstring).
    from llama_index.core.agent.workflow import FunctionAgent
    from llama_index.llms.anthropic import Anthropic
    from llama_index.tools.mcp import BasicMCPClient, McpToolSpec

    mcp_client = BasicMCPClient(QUERYGATE_MCP_URL)
    # Restrict the tool spec to the allow-listed QueryGate tools (least privilege).
    tool_spec = McpToolSpec(client=mcp_client, allowed_tools=QUERYGATE_TOOLS)
    tools = await tool_spec.to_tool_list_async()

    agent = FunctionAgent(
        tools=tools,
        llm=Anthropic(model="claude-sonnet-5"),
        system_prompt=SYSTEM_PROMPT,
    )

    response = await agent.run(question)
    print(str(response))


if __name__ == "__main__":
    asyncio.run(main())
