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
