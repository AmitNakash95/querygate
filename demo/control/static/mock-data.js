/*
 * mock-data.js — fixture data for QueryGate demo control UI mock mode.
 *
 * Activated by ?mock=1 (see app.js). Every shape here conforms exactly to
 * demo/SPEC.md's Backend HTTP contract (Scenario, RunResult, /api/probe
 * events) so the UI can be built and screenshotted before demo/control/app.py
 * exists. Nothing here is loaded or used unless mock mode is explicitly on.
 */
(function (global) {
  "use strict";

  // These two documents are the LITERAL, byte-for-byte current text of
  // demo/config/policy.demo.yaml and demo/config/connections.demo.yaml — not
  // a hand-authored approximation. The real product's models are
  // extra="forbid", so any shape drift here (a mapping where the real file
  // has a list, a value the real file doesn't have) would render a config
  // the product could never actually load. Keep this block byte-identical
  // to those files; if either file changes, re-paste it here and re-derive
  // every policy_lines array below against the new line numbers.
  var POLICY_YAML = [
    "# QueryGate policy for the partner demo.",
    "#",
    "# This is the whole rulebook. Every rule below is a hard wall, checked",
    "# before the database is ever touched — not a suggestion the agent can",
    "# talk its way around.",
    "",
    "default:",
    "  enabled: true",
    "",
    "  # Only these four tables exist as far as the agent is concerned. The",
    "  # employee table (names, SSNs, salaries) is not on this list. It still",
    "  # exists in the database — the agent just never gets to know that.",
    "  allowed_tables: [customers, orders, order_items, products]",
    "",
    "  # Customer email addresses can never be read. Not filtered, not sorted,",
    "  # not returned. Full stop.",
    "  denied_columns:",
    "    customers: [email]",
    "",
    "  # These two columns are visible, but never in the raw. The agent gets a",
    "  # masked value; the real one never leaves the database.",
    "  column_masks:",
    "    customers:",
    "      - column: phone",
    '        kind: last          # only the last 4 digits are shown, e.g. "0142"',
    "        length: 4",
    "      - column: national_id",
    "        kind: hash          # a one-way hash, never the real ID",
    "",
    "  # The most rows the agent can ever pull back in one query. Asking for",
    '  # "the entire customer table" still only returns this many.',
    "  max_limit: 100",
    "",
    "  # No cartesian products. Joining every customer to every order to every",
    "  # order line with no matching condition is how a database gets brought",
    "  # to its knees — this makes that request impossible to even build.",
    "  allow_cross_join: false",
    "",
    "  # Sane limits on how complicated a single query can be, so nothing can",
    "  # be built that quietly turns into a massive, slow scan.",
    "  max_joins: 4",
    "  max_select_columns: 15",
    "  max_where_depth: 4",
    "  max_group_by: 6",
    "  max_where_predicates: 50",
    "  max_in_list_size: 500",
    "",
    "  # Every query gets a few seconds to finish, and only a handful can run",
    "  # at the same time. Nothing gets to hog the database.",
    "  timeout_seconds: 5",
    "  max_concurrency: 4",
  ].join("\n");

  var CONNECTIONS_YAML = [
    "# Partner-demo connection profile (demo/SPEC.md).",
    "#",
    "# One connection, `demo`, over the pitch database (querygate_demo_pitch on",
    "# 127.0.0.1:5544) via the agent_ro role — the SAME role and the SAME database",
    "# the deliberately-naive baseline MCP server (demo/baseline_mcp/) connects as.",
    "# Identical database privileges; the only difference is this file plus",
    "# policy.demo.yaml sitting in front of the connection.",
    "#",
    "# The connection string is never inlined — ${PITCH_AGENT_RO_DSN} is resolved",
    "# from the environment at load time (see env.demo), so this file is safe to",
    "# commit.",
    "#",
    "# `known_tables` intentionally includes `employees`: the connection can see",
    "# it (agent_ro has SELECT on every table, employees included) exactly the",
    "# same as the baseline server can. What withholds it from an agent talking to",
    "# QueryGate is policy.demo.yaml's `allowed_tables`, not a lack of visibility",
    "# here. That distinction is the whole demo, so don't \"fix\" it by dropping",
    "# employees from this list.",
    "",
    "connections:",
    "  - id: demo",
    "    dialect: postgresql",
    "    connection_string: ${PITCH_AGENT_RO_DSN}",
    '    description: "Partner-demo pitch database — customers/orders/order_items/products/employees"',
    "    enabled: true",
    "    known_tables: [customers, orders, order_items, products, employees]",
  ].join("\n");

  var SCENARIOS = [
    {
      id: "recon",
      title: "Recon the schema",
      ask: "What tables are in this database?",
      point: "Off the gate, the agent's reality includes employees. On the gate, it never existed.",
      act: "recon",
    },
    {
      id: "pii_table",
      title: "Pull the HR table",
      ask: "List every employee with their SSN and salary.",
      point: "A whole table walled off — rejected before the database is even opened.",
      act: "confidentiality",
    },
    {
      id: "pii_column",
      title: "Export customer contact data",
      ask: "Export customer emails and phone numbers for a campaign.",
      point: "Table allowed, columns are not — deny and mask are two different rules.",
      act: "confidentiality",
    },
    {
      id: "bulk_export",
      title: "Bulk export",
      ask: "Give me the entire customer table.",
      point: "The query still runs — just capped, not refused.",
      act: "confidentiality",
    },
    {
      id: "overload",
      title: "Cross-join overload",
      ask: "How many customer / order / item combinations exist?",
      point: "Read-only didn't stop the CPU from getting pegged. The gate does, in ~2ms.",
      act: "availability",
    },
    {
      id: "legit",
      title: "Legitimate report",
      ask: "Revenue by country for completed orders, top 10.",
      point: "Same answer, both paths — the overhead is measured, not asserted.",
      act: "overhead",
    },
  ];

  function cols(names) {
    return names;
  }

  // -------------------------------------------------------------------
  // Audit-ledger fixtures — shaped exactly like RunResult["audit"]
  // (demo/control/audit.py's audit_block_for_run): {records, count,
  // headline_event_id, chain_verified, chain_status, records_checked,
  // chain_head, chain_error, ledger_path}, each "records" entry a full
  // LedgerRecord envelope ({seq, prev_hash, event, hash}).
  // Verified against a real captured /api/run response on 2026-08-23 —
  // field names, nesting, and (critically) the fact that `list_tables`
  // appends ZERO ledger records even with the gate ON (it's a metadata
  // read, not a query.execution — QueryGate's audit ledger only records
  // the latter today) are copied from that real response, not invented.
  // Hashes/event_ids/correlation_ids below are deterministic synthetic hex
  // — never real SHA-256/HMAC output — since mock mode never verifies them
  // client-side; only the real backend's chain_status/chain_verified are
  // ever trusted, live or mocked. chain_status/records_checked/
  // headline_event_id were added post-launch (items A1/A3/A4, partner-demo
  // audit-panel fix session) — every fixture below sets them explicitly
  // rather than relying on app.js's backward-compat fallback, so mock mode
  // continues to exercise the same code path a live server does.
  //
  // The chain below is built by walking the six scenarios in RUNBOOK.md's
  // presented order TWICE (gate off, then gate on) — the same order a
  // presenter actually clicks through — so seq/prev_hash/hash progress
  // exactly the way one continuous ledger would across a real run-through.
  // `legit` is the one scenario where gate OFF *also* appends real records:
  // run_legit always exercises both the baseline AND the QueryGate path
  // (to measure overhead), regardless of which one is headlined — so its
  // gate-OFF fixture is not the "0 records" case the other five are.
  // -------------------------------------------------------------------
  var LEDGER_PATH = "demo/config/var/querygate-pitch-ledger.jsonl";
  function fakeHex(seed) {
    var s = "";
    var x = seed >>> 0 || 1;
    while (s.length < 64) {
      x = (x * 1103515245 + 12345) >>> 0;
      s += ("00000000" + x.toString(16)).slice(-8);
    }
    return s.slice(0, 64);
  }
  function fakeUuid(seed) {
    var h = fakeHex(seed);
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-4" + h.slice(13, 16) + "-a" + h.slice(17, 20) + "-" + h.slice(20, 32);
  }
  // Mid-day-looking starting point: a real demo ledger accumulates across
  // the whole day, so the first fixture below is not a suspiciously fresh
  // seq=0/genesis chain.
  var _chainSeq = 118;
  var _chainHead = fakeHex(4200);
  function _appendRecord(eventOverrides) {
    _chainSeq += 1;
    var seed = _chainSeq;
    var event = Object.assign(
      {
        schema_version: "1",
        event_id: fakeUuid(seed),
        occurred_at: new Date().toISOString(),
        event_type: "query.execution",
        correlation_id: fakeUuid(seed + 5000),
        surface: "mcp",
        operation: "execute_structured_query",
        principal_id: "mcp-service-account",
        auth_method: "api_key",
        principal_scopes: [],
        delegation_chain: [],
        connection_id: "demo",
        duration_ms: 12,
        masked_columns: [],
      },
      eventOverrides
    );
    var prevHash = _chainHead;
    var hash = fakeHex(seed * 97 + 13);
    _chainHead = hash;
    return { seq: seed, prev_hash: prevHash, event: event, hash: hash };
  }
  // _auditBlock's second argument is the EXPLICIT headline event_id (or
  // null) — never inferred from "the last record" or "the record whose
  // decision matches outcome" inside this function. That mirrors the real
  // backend post-A4 (demo/control/audit.py's audit_block_for_run,
  // demo/control/scenarios.py's _qg_call_with_headline): the caller who
  // knows which real call decided the outcome is the one who names the
  // record, not a heuristic re-deriving it from the (capped) preview list.
  // `records_checked` mirrors verify_chain() re-walking the WHOLE ledger
  // (not just this run's new records) — _chainSeq is this fixture set's
  // running total of records that exist by this point in the mocked day.
  function _auditBlock(records, headlineEventId) {
    return {
      records: records.slice(0, 5),
      count: records.length,
      headline_event_id: headlineEventId || null,
      chain_verified: true,
      chain_status: "ok",
      records_checked: _chainSeq,
      chain_head: _chainHead,
      chain_error: null,
      ledger_path: LEDGER_PATH,
    };
  }
  function _noAuditBlock() {
    return {
      records: [],
      count: 0,
      headline_event_id: null,
      chain_verified: true,
      chain_status: "ok",
      records_checked: _chainSeq,
      chain_head: _chainHead,
      chain_error: null,
      ledger_path: LEDGER_PATH,
    };
  }

  var AUDIT = { off: {}, on: {} };
  // Gate off — every scenario runs against the baseline server, which has
  // no audit sink at all. Except `legit`: see the note above.
  AUDIT.off.recon = _noAuditBlock();
  AUDIT.off.pii_table = _noAuditBlock();
  AUDIT.off.pii_column = _noAuditBlock();
  AUDIT.off.bulk_export = _noAuditBlock();
  AUDIT.off.overload = _noAuditBlock();
  // legit's gate-OFF path still appends 15 real QG-side ledger records
  // (both loops always run, to measure overhead) — but the DISPLAYED mcp
  // block/outcome for gate=="off" is the baseline call, which has no audit
  // sink at all. So headline_event_id is explicitly null here, same as the
  // real backend (run_legit only sets `_headline_event_id` in the
  // gate=="on" branch).
  var legitRecordsOff = Array.from({ length: 15 }, function () {
    return _appendRecord({
      policy_decision: "allowed",
      outcome: "success",
      query_shape: {
        from: "orders",
        select: [
          { kind: "column", column: "customers.country" },
          { kind: "aggregate", function: "sum", column: "orders.total_amount", alias: "revenue" },
        ],
        joins: [{ table: "customers", type: "inner", on: ["orders.customer_id", "customers.id"] }],
        group_by: ["customers.country"],
        order_by: [{ col: "revenue", dir: "desc", nulls: null }],
        offset: 0,
        where: { operator: "eq", column: "orders.status" },
        requested_limit: 10,
      },
      duration_ms: 61,
      row_count: 10,
      response_bytes: 505,
      truncated: true,
      admission_id: fakeUuid(_chainSeq + 9000),
      queue_wait_ms: 1,
      admission_state: "completed",
    });
  });
  AUDIT.off.legit = _auditBlock(legitRecordsOff, null);

  // Gate on.
  AUDIT.on.recon = _noAuditBlock(); // list_tables is a metadata read — not audited today
  var piiTableDenied = _appendRecord({
    policy_decision: "denied",
    outcome: "rejected",
    query_shape: {
      from: "employees",
      select: [
        { kind: "column", column: "employees.name" },
        { kind: "column", column: "employees.ssn" },
        { kind: "column", column: "employees.salary" },
      ],
      joins: [],
      group_by: [],
      order_by: [],
      offset: 0,
      requested_limit: 20,
    },
    duration_ms: 0,
    error_category: "policy",
    admission_id: fakeUuid(_chainSeq + 9000),
    queue_wait_ms: 0,
  });
  AUDIT.on.pii_table = _auditBlock([piiTableDenied], piiTableDenied.event.event_id);

  // pii_column: the DENIED call is always the headline (r["outcome"] is
  // derived from it, never from the masked follow-up — see run_pii_column's
  // own comments in scenarios.py) even though the masked call's record also
  // gets appended and shown in the preview.
  var piiColumnDenied = _appendRecord({
    policy_decision: "denied",
    outcome: "rejected",
    query_shape: {
      from: "customers",
      select: [
        { kind: "column", column: "customers.name" },
        { kind: "column", column: "customers.email" },
        { kind: "column", column: "customers.phone" },
      ],
      joins: [],
      group_by: [],
      order_by: [],
      offset: 0,
      requested_limit: 5,
    },
    duration_ms: 0,
    error_category: "policy",
    admission_id: fakeUuid(_chainSeq + 9000),
    queue_wait_ms: 0,
  });
  var piiColumnMasked = _appendRecord({
    policy_decision: "allowed",
    outcome: "success",
    query_shape: {
      from: "customers",
      select: [
        { kind: "column", column: "customers.name" },
        { kind: "column", column: "customers.phone" },
        { kind: "column", column: "customers.national_id" },
      ],
      joins: [],
      group_by: [],
      order_by: [],
      offset: 0,
      requested_limit: 5,
    },
    duration_ms: 9,
    row_count: 5,
    response_bytes: 320,
    truncated: false,
    admission_id: fakeUuid(_chainSeq + 9001),
    queue_wait_ms: 0,
    admission_state: "completed",
    masked_columns: ["phone", "national_id"],
  });
  AUDIT.on.pii_column = _auditBlock(
    [piiColumnDenied, piiColumnMasked],
    piiColumnDenied.event.event_id
  );

  var bulkExportRecord = _appendRecord({
    policy_decision: "allowed",
    outcome: "success",
    query_shape: {
      from: "customers",
      select: [
        { kind: "column", column: "customers.id" },
        { kind: "column", column: "customers.name" },
        { kind: "column", column: "customers.phone" },
        { kind: "column", column: "customers.national_id" },
        { kind: "column", column: "customers.country" },
        { kind: "column", column: "customers.is_active" },
        { kind: "column", column: "customers.created_at" },
      ],
      joins: [],
      group_by: [],
      order_by: [],
      offset: 0,
      requested_limit: 250000,
    },
    duration_ms: 38,
    row_count: 100,
    response_bytes: 9412,
    truncated: true,
    admission_id: fakeUuid(_chainSeq + 9000),
    queue_wait_ms: 0,
    admission_state: "completed",
    masked_columns: ["phone", "national_id"],
  });
  AUDIT.on.bulk_export = _auditBlock([bulkExportRecord], bulkExportRecord.event.event_id);

  // overload: `outcome` is an AGGREGATE over all 20 concurrent calls
  // (all_rejected), decided by no single one of them — headline_event_id is
  // explicitly null, same as the real backend (see scenarios.py's comment
  // in run_overload's gate=="on" branch).
  var overloadRecordsOn = Array.from({ length: 20 }, function () {
    return _appendRecord({
      policy_decision: "denied",
      outcome: "rejected",
      query_shape: {
        from: "customers",
        select: [{ kind: "aggregate", function: "count", column: "*", alias: "n" }],
        joins: [
          { table: "orders", type: "cross" },
          { table: "order_items", type: "cross" },
        ],
        group_by: [],
        order_by: [],
        offset: 0,
      },
      duration_ms: 0,
      error_category: "policy",
      admission_id: fakeUuid(_chainSeq + 9000),
      queue_wait_ms: 0,
    });
  });
  AUDIT.on.overload = _auditBlock(overloadRecordsOn, null);

  // legit gate on: the LAST of the 15 real QG calls decides `outcome`
  // (qg_last in scenarios.py) — headline is that last record's event_id.
  var legitRecordsOn = Array.from({ length: 15 }, function () {
    return _appendRecord({
      policy_decision: "allowed",
      outcome: "success",
      query_shape: {
        from: "orders",
        select: [
          { kind: "column", column: "customers.country" },
          { kind: "aggregate", function: "sum", column: "orders.total_amount", alias: "revenue" },
        ],
        joins: [{ table: "customers", type: "inner", on: ["orders.customer_id", "customers.id"] }],
        group_by: ["customers.country"],
        order_by: [{ col: "revenue", dir: "desc", nulls: null }],
        offset: 0,
        where: { operator: "eq", column: "orders.status" },
        requested_limit: 10,
      },
      duration_ms: 55,
      row_count: 10,
      response_bytes: 505,
      truncated: true,
      admission_id: fakeUuid(_chainSeq + 9000),
      queue_wait_ms: 1,
      admission_state: "completed",
    });
  });
  AUDIT.on.legit = _auditBlock(
    legitRecordsOn,
    legitRecordsOn[legitRecordsOn.length - 1].event.event_id
  );

  // touched / callDelta drive the mock "your database" call counter in app.js.
  var RESULTS = {
    off: {
      recon: {
        mcp: {
          server: "baseline-postgres-mcp",
          endpoint: "http://127.0.0.1:8811/mcp",
          tool: "execute_sql",
          request: {
            sql: "select table_name from information_schema.tables where table_schema = 'public' order by table_name",
          },
          response: {
            columns: ["table_name"],
            rows: [["customers"], ["employees"], ["order_items"], ["orders"], ["products"]],
            row_count: 5,
          },
        },
        outcome: "allowed",
        blocked_by: null,
        policy_lines: [],
        latency_ms: 6.1,
        touched: true,
        callDelta: 1,
        columns: cols(["table_name"]),
        rows: [["customers"], ["employees"], ["order_items"], ["orders"], ["products"]],
        row_count: 5,
        audit: AUDIT.off.recon,
        note: "employees is right there. Nothing in a read-only grant hides a table's existence.",
      },
      pii_table: {
        mcp: {
          server: "baseline-postgres-mcp",
          endpoint: "http://127.0.0.1:8811/mcp",
          tool: "execute_sql",
          request: { sql: "select name, ssn, department, salary from employees" },
          response: {
            columns: ["name", "ssn", "department", "salary"],
            rows: [
              ["Dana Whitfield", "912-34-5017", "Finance", "156200"],
              ["Marcus Ibe", "927-88-1043", "Engineering", "171500"],
              ["Priya Chandra", "934-02-9981", "Engineering", "164800"],
              ["Owen Falkner", "941-77-3320", "Sales", "128900"],
              ["Renee Achebe", "918-45-2266", "Finance", "148300"],
              ["Sam Okonkwo", "955-19-8871", "Operations", "121400"],
            ],
            row_count: 12,
          },
        },
        outcome: "allowed",
        blocked_by: null,
        policy_lines: [],
        latency_ms: 8.4,
        touched: true,
        callDelta: 1,
        columns: cols(["name", "ssn", "department", "salary"]),
        rows: [
          ["Dana Whitfield", "912-34-5017", "Finance", "156200"],
          ["Marcus Ibe", "927-88-1043", "Engineering", "171500"],
          ["Priya Chandra", "934-02-9981", "Engineering", "164800"],
          ["Owen Falkner", "941-77-3320", "Sales", "128900"],
          ["Renee Achebe", "918-45-2266", "Finance", "148300"],
          ["Sam Okonkwo", "955-19-8871", "Operations", "121400"],
        ],
        row_count: 12,
        audit: AUDIT.off.pii_table,
        note: "Every SSN and salary in the company, in plain text, over a connection that's genuinely read-only.",
      },
      pii_column: {
        mcp: {
          server: "baseline-postgres-mcp",
          endpoint: "http://127.0.0.1:8811/mcp",
          tool: "execute_sql",
          request: { sql: "select name, email, phone from customers limit 8" },
          response: {
            columns: ["name", "email", "phone"],
            rows: [
              ["Ines Torbjornsen", "ines.torbjornsen@example.invalid", "555-0114"],
              ["Gareth Nkomo", "gareth.nkomo@example.invalid", "555-0182"],
              ["Yuki Sasaki", "yuki.sasaki@example.invalid", "555-0147"],
              ["Bianca Duarte", "bianca.duarte@example.invalid", "555-0199"],
              ["Femi Adeyemi", "femi.adeyemi@example.invalid", "555-0123"],
              ["Lars Bergqvist", "lars.bergqvist@example.invalid", "555-0166"],
              ["Amara Chukwu", "amara.chukwu@example.invalid", "555-0138"],
              ["Petra Novak", "petra.novak@example.invalid", "555-0155"],
            ],
            row_count: 249812,
          },
        },
        outcome: "allowed",
        blocked_by: null,
        policy_lines: [],
        latency_ms: 11.2,
        touched: true,
        callDelta: 1,
        columns: cols(["name", "email", "phone"]),
        rows: [
          ["Ines Torbjornsen", "ines.torbjornsen@example.invalid", "555-0114"],
          ["Gareth Nkomo", "gareth.nkomo@example.invalid", "555-0182"],
          ["Yuki Sasaki", "yuki.sasaki@example.invalid", "555-0147"],
          ["Bianca Duarte", "bianca.duarte@example.invalid", "555-0199"],
          ["Femi Adeyemi", "femi.adeyemi@example.invalid", "555-0123"],
          ["Lars Bergqvist", "lars.bergqvist@example.invalid", "555-0166"],
          ["Amara Chukwu", "amara.chukwu@example.invalid", "555-0138"],
          ["Petra Novak", "petra.novak@example.invalid", "555-0155"],
        ],
        row_count: 249812,
        audit: AUDIT.off.pii_column,
        note: "Raw emails and phone numbers, unmasked, for the full customer table — not just this preview.",
      },
      bulk_export: {
        mcp: {
          server: "baseline-postgres-mcp",
          endpoint: "http://127.0.0.1:8811/mcp",
          tool: "execute_sql",
          request: { sql: "select * from customers" },
          response: { columns: ["id", "name", "email", "phone", "national_id", "country", "is_active", "created_at"], row_count: 249812 },
        },
        outcome: "allowed",
        blocked_by: null,
        policy_lines: [],
        latency_ms: 2140.6,
        touched: true,
        callDelta: 1,
        columns: cols(["id", "name", "email", "phone", "national_id", "country", "is_active", "created_at"]),
        rows: [
          [1, "Ines Torbjornsen", "ines.torbjornsen@example.invalid", "555-0114", "912-77-3301", "NO", "true", "2021-03-11"],
          [2, "Gareth Nkomo", "gareth.nkomo@example.invalid", "555-0182", "955-12-8842", "ZA", "true", "2021-04-02"],
          [3, "Yuki Sasaki", "yuki.sasaki@example.invalid", "555-0147", "928-64-1190", "JP", "true", "2021-04-19"],
          [4, "Bianca Duarte", "bianca.duarte@example.invalid", "555-0199", "941-30-5527", "BR", "false", "2021-05-06"],
          [5, "Femi Adeyemi", "femi.adeyemi@example.invalid", "555-0123", "916-88-2214", "NG", "true", "2021-05-30"],
          [6, "Lars Bergqvist", "lars.bergqvist@example.invalid", "555-0166", "962-05-9931", "SE", "true", "2021-06-14"],
          [7, "Amara Chukwu", "amara.chukwu@example.invalid", "555-0138", "934-71-4408", "NG", "true", "2021-07-01"],
          [8, "Petra Novak", "petra.novak@example.invalid", "555-0155", "907-22-6675", "CZ", "true", "2021-07-22"],
        ],
        row_count: 249812,
        audit: AUDIT.off.bulk_export,
        note: "All 249,812 customer rows left the database in a single unbounded SELECT * — no limit was ever asked for.",
      },
      overload: {
        mcp: {
          server: "baseline-postgres-mcp",
          endpoint: "http://127.0.0.1:8811/mcp",
          tool: "execute_sql",
          request: {
            sql: "select count(*) from customers, orders, order_items",
          },
          response: { error: "terminated by administrator command", pg_terminate_backend: true },
        },
        outcome: "killed",
        blocked_by: null,
        policy_lines: [],
        latency_ms: 15000,
        touched: true,
        callDelta: 1,
        columns: [],
        rows: [],
        row_count: 0,
        audit: AUDIT.off.overload,
        note: "An implicit cross join over 250k × 600k × 1.2M rows pegged the CPU. QueryGate's control server killed the backend at the 15s bound — your database took the hit first.",
      },
      legit: {
        mcp: {
          server: "baseline-postgres-mcp",
          endpoint: "http://127.0.0.1:8811/mcp",
          tool: "execute_sql",
          request: {
            sql: "select country, sum(total_amount) as total_revenue from orders join customers on customers.id = orders.customer_id where orders.status = 'completed' group by country order by total_revenue desc limit 10",
          },
          response: {
            columns: ["country", "total_revenue"],
            rows: [
              ["US", "4128340.55"],
              ["DE", "2015772.10"],
              ["GB", "1897440.25"],
              ["BR", "1650228.90"],
              ["JP", "1522015.40"],
              ["FR", "1401987.65"],
              ["NG", "1210554.30"],
              ["ZA", "1188402.75"],
              ["SE", "980215.60"],
              ["CZ", "845100.05"],
            ],
            row_count: 10,
          },
        },
        outcome: "allowed",
        blocked_by: null,
        policy_lines: [],
        latency_ms: 41.2,
        touched: true,
        callDelta: 1,
        columns: cols(["country", "total_revenue"]),
        rows: [
          ["US", "4128340.55"],
          ["DE", "2015772.10"],
          ["GB", "1897440.25"],
          ["BR", "1650228.90"],
          ["JP", "1522015.40"],
          ["FR", "1401987.65"],
          ["NG", "1210554.30"],
          ["ZA", "1188402.75"],
          ["SE", "980215.60"],
          ["CZ", "845100.05"],
        ],
        row_count: 10,
        audit: AUDIT.off.legit,
        note: "Median of 15 runs on the baseline path: 41.2ms.",
      },
    },
    on: {
      recon: {
        mcp: {
          server: "querygate",
          endpoint: "http://127.0.0.1:8010/mcp",
          tool: "list_tables",
          request: { connection: "demo" },
          response: { tables: ["customers", "orders", "order_items", "products"] },
        },
        outcome: "allowed",
        blocked_by: null,
        policy_lines: [13],
        latency_ms: 1.8,
        touched: true,
        callDelta: 1,
        columns: cols(["table_name"]),
        rows: [["customers"], ["order_items"], ["orders"], ["products"]],
        row_count: 4,
        audit: AUDIT.on.recon,
        note: "list_tables only returns what policy.demo.yaml's allowed_tables names. employees isn't hidden — it isn't part of the agent's reality.",
      },
      pii_table: {
        mcp: {
          server: "querygate",
          endpoint: "http://127.0.0.1:8010/mcp",
          tool: "run_structured_queries",
          request: {
            connection: "demo",
            queries: [{ table: "employees", select: [{ column: "name" }, { column: "ssn" }, { column: "salary" }] }],
          },
          response: {
            error: "QueryValidationError",
            message: "table 'employees' is not in allowed_tables for connection 'demo'",
          },
        },
        outcome: "blocked",
        blocked_by: "policy.demo.yaml: allowed_tables",
        policy_lines: [13],
        latency_ms: 1.4,
        touched: false,
        callDelta: 0,
        columns: [],
        rows: [],
        row_count: 0,
        audit: AUDIT.on.pii_table,
        note: "employees was never in allowed_tables. Rejected in validation — the database was never opened.",
      },
      pii_column: {
        mcp: {
          server: "querygate",
          endpoint: "http://127.0.0.1:8010/mcp",
          tool: "run_structured_queries",
          request: {
            connection: "demo",
            queries: [{ table: "customers", select: [{ column: "email" }, { column: "phone" }] }],
          },
          response: {
            error: "QueryValidationError",
            message: "column 'customers.email' is denied by policy for connection 'demo'",
          },
        },
        outcome: "blocked",
        blocked_by: "policy.demo.yaml: denied_columns",
        policy_lines: [17, 18, 24, 25, 26],
        latency_ms: 1.6,
        // AMENDMENT 2: RunResult.outcome reports the FIRST beat (the
        // email+phone request that denied_columns rejects outright) — the
        // mcp.request/response above are that first beat's, matching the
        // real backend. But the rows/columns/masked_columns below are the
        // SECOND beat's: a follow-up request for phone + national_id alone,
        // which succeeds and is what the row preview shows. Verified live
        // against the real backend on 2026-08-23: outcome "blocked" with a
        // populated, masked row preview is the actual shape, not a mock-only
        // simplification — the first call never touches the database, the
        // second one does, hence touched: true here.
        touched: true,
        callDelta: 1,
        columns: cols(["name", "phone", "national_id"]),
        rows: [
          ["Ines Torbjornsen", "0114", "8f2c1a4de7b9903f5e1a26c4d8907fbb"],
          ["Gareth Nkomo", "0182", "b71e04f3a825c619d0e4f8b2a6710c9d"],
          ["Yuki Sasaki", "0147", "1d9a638c2f716e0ab4c359d871ae2f6b"],
          ["Bianca Duarte", "0199", "e440cf9b1a736d0f8c25e19764a3b0d2"],
          ["Femi Adeyemi", "0123", "5c8b7103edc946a1f0b287d651c94a3e"],
        ],
        row_count: 5,
        masked_columns: ["phone", "national_id"],
        audit: AUDIT.on.pii_column,
        note: "Two beats. Asking for email + phone together was rejected outright (denied_columns). A follow-up request for phone + national_id alone succeeded — masked (last 4 digits; national_id one-way hashed), never in the clear.",
      },
      bulk_export: {
        mcp: {
          server: "querygate",
          endpoint: "http://127.0.0.1:8010/mcp",
          tool: "run_structured_queries",
          request: {
            connection: "demo",
            queries: [{ table: "customers", select: [{ column: "*" }] }],
          },
          response: {
            columns: ["id", "name", "phone", "national_id", "country", "is_active", "created_at"],
            rows: [
              [1, "Ines Torbjornsen", "•••• 0114", "8f2c1a…", "NO", "true", "2021-03-11"],
              [2, "Gareth Nkomo", "•••• 0182", "b71e04…", "ZA", "true", "2021-04-02"],
              [3, "Yuki Sasaki", "•••• 0147", "1d9a63…", "JP", "true", "2021-04-19"],
              [4, "Bianca Duarte", "•••• 0199", "e440cf…", "BR", "false", "2021-05-06"],
              [5, "Femi Adeyemi", "•••• 0123", "5c8b71…", "NG", "true", "2021-05-30"],
            ],
            row_count: 100,
            note: "capped at max_limit",
          },
        },
        outcome: "allowed",
        blocked_by: null,
        policy_lines: [24, 25, 26, 27, 28, 32],
        latency_ms: 34.9,
        touched: true,
        callDelta: 1,
        columns: cols(["id", "name", "phone", "national_id", "country", "is_active", "created_at"]),
        rows: [
          [1, "Ines Torbjornsen", "•••• 0114", "8f2c1a…", "NO", "true", "2021-03-11"],
          [2, "Gareth Nkomo", "•••• 0182", "b71e04…", "ZA", "true", "2021-04-02"],
          [3, "Yuki Sasaki", "•••• 0147", "1d9a63…", "JP", "true", "2021-04-19"],
          [4, "Bianca Duarte", "•••• 0199", "e440cf…", "BR", "false", "2021-05-06"],
          [5, "Femi Adeyemi", "•••• 0123", "5c8b71…", "NG", "true", "2021-05-30"],
        ],
        row_count: 100,
        masked_columns: ["phone", "national_id"],
        audit: AUDIT.on.bulk_export,
        note: "email isn't in the result at all; note is also missing entirely, not just blank — and phone/national_id are masked. The query still runs, capped at max_limit: 100 instead of refused.",
      },
      overload: {
        mcp: {
          server: "querygate",
          endpoint: "http://127.0.0.1:8010/mcp",
          tool: "run_structured_queries",
          request: {
            connection: "demo",
            queries: [{ tables: ["customers", "orders", "order_items"], select: [{ aggregate: "count" }] }],
          },
          response: {
            error: "QueryValidationError",
            message: "implicit cross join requires allow_cross_join, which is false for connection 'demo'",
          },
        },
        outcome: "blocked",
        blocked_by: "policy.demo.yaml: allow_cross_join",
        policy_lines: [37],
        latency_ms: 2.1,
        touched: false,
        callDelta: 0,
        columns: [],
        rows: [],
        row_count: 0,
        audit: AUDIT.on.overload,
        note: "allow_cross_join: false. Rejected in ~2ms — the database never felt this one at all.",
      },
      legit: {
        mcp: {
          server: "querygate",
          endpoint: "http://127.0.0.1:8010/mcp",
          tool: "run_structured_queries",
          request: {
            connection: "demo",
            queries: [
              {
                table: "orders",
                joins: [{ table: "customers", on: { left: "orders.customer_id", right: "customers.id" } }],
                where: { column: "orders.status", op: "eq", value: "completed" },
                group_by: ["customers.country"],
                select: [{ column: "customers.country" }, { aggregate: "sum", column: "orders.total_amount", alias: "total_revenue" }],
                order_by: [{ column: "total_revenue", direction: "desc" }],
                limit: 10,
              },
            ],
          },
          response: {
            columns: ["country", "total_revenue"],
            rows: [
              ["US", "4128340.55"],
              ["DE", "2015772.10"],
              ["GB", "1897440.25"],
              ["BR", "1650228.90"],
              ["JP", "1522015.40"],
              ["FR", "1401987.65"],
              ["NG", "1210554.30"],
              ["ZA", "1188402.75"],
              ["SE", "980215.60"],
              ["CZ", "845100.05"],
            ],
            row_count: 10,
          },
        },
        outcome: "allowed",
        blocked_by: null,
        policy_lines: [],
        latency_ms: 46.8,
        touched: true,
        callDelta: 1,
        columns: cols(["country", "total_revenue"]),
        rows: [
          ["US", "4128340.55"],
          ["DE", "2015772.10"],
          ["GB", "1897440.25"],
          ["BR", "1650228.90"],
          ["JP", "1522015.40"],
          ["FR", "1401987.65"],
          ["NG", "1210554.30"],
          ["ZA", "1188402.75"],
          ["SE", "980215.60"],
          ["CZ", "845100.05"],
        ],
        row_count: 10,
        audit: AUDIT.on.legit,
        note: "Median of 15 runs through QueryGate: 46.8ms — +5.6ms over baseline for full validation, policy, and audit.",
      },
    },
  };

  global.QG_MOCK = {
    scenarios: SCENARIOS,
    config: { policy_yaml: POLICY_YAML, connections_yaml: CONNECTIONS_YAML },
    results: RESULTS,
    health: { db: true, baseline_mcp: true, querygate: true },
    probe: {
      // Baseline steady-state jitter, ms.
      baseline: { off: 3.6, on: 3.1, jitter: 1.4 },
      // Shape of the overload spike (baseline path only — the gate-on path
      // never leaves this scenario's shape because the query is rejected).
      spike: { rampTicks: 4, peakMs: 2900, holdTicks: 5, decayTicks: 6 },
    },
  };
})(window);
