# Example MCP usage

QueryGate exposes an MCP server (Streamable HTTP transport) at `/mcp` when
`MCP_ENABLED=true`. Any MCP-compatible agent client can connect to it
directly; the raw JSON-RPC shape for `tools/call` looks like this.

Send each payload as a POST to `http://localhost:8000/mcp` with
`Content-Type: application/json` and `Accept: application/json,
text/event-stream`, plus `Authorization: Bearer <key>` once
`MCP_API_KEYS` is configured.

## List connections

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": { "name": "list_connections", "arguments": {} }
}
```

## List tables

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "list_tables",
    "arguments": { "connection": "demo" }
  }
}
```

## Describe a table

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "describe_table",
    "arguments": { "connection": "demo", "table_name": "orders" }
  }
}
```

## Search the semantic catalog

This searches only policy-visible metadata, never database row values:

```json
{
  "jsonrpc": "2.0",
  "id": 31,
  "method": "tools/call",
  "params": {
    "name": "search_catalog",
    "arguments": {
      "connection": "demo",
      "query": "customer revenue",
      "limit": 5
    }
  }
}
```

## Execute a structured query

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "tools/call",
  "params": {
    "name": "execute_structured_query",
    "arguments": {
      "connection": "demo",
      "query": {
        "from": "orders",
        "select": ["orders.id", "orders.status", "orders.total_amount"],
        "where": { "col": "orders.status", "op": "eq", "value": "completed" },
        "order_by": [{ "col": "orders.total_amount", "dir": "desc" }],
        "limit": 10,
        "intent": "recent completed orders, highest value first"
      }
    }
  }
}
```

## Capacity waiting (queue_mode / wait_timeout_seconds)

Optional tool arguments, separate from `query` — see the README's
"Agent-visible capacity waiting" section for the full contract. Reject
immediately instead of waiting for a concurrency slot:

```json
{
  "jsonrpc": "2.0",
  "id": 41,
  "method": "tools/call",
  "params": {
    "name": "execute_structured_query",
    "arguments": {
      "connection": "demo",
      "query": { "from": "orders", "select": ["orders.id"], "limit": 10 },
      "queue_mode": "fail_fast"
    }
  }
}
```

A capacity rejection returns `success: false`, `error_code: "VALIDATION"`,
and `admission_state: "capacity_timeout"` plus `admission_id`/`queue_wait_ms`
on the `MCPErrorResult`. A successful call returns those same
`admission_id`/`queue_wait_ms` fields on the tool result instead.

## Top-N per group: top 2 orders by value per customer

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "tools/call",
  "params": {
    "name": "execute_structured_query",
    "arguments": {
      "connection": "demo",
      "query": {
        "from": "orders",
        "select": ["orders.customer_id", "orders.id", "orders.total_amount"],
        "top_n": {
          "partition_by": ["orders.customer_id"],
          "order_by": [{ "col": "orders.total_amount", "dir": "desc" }],
          "n": 2
        },
        "limit": 50,
        "intent": "top 2 highest-value orders per customer"
      }
    }
  }
}
```
