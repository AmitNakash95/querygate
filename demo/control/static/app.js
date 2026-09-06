/*
 * app.js — QueryGate partner demo control UI.
 *
 * Talks to the control backend (demo/control/app.py, port 8900) per the
 * contract in demo/SPEC.md. Vanilla JS, no build step, no framework.
 *
 * Mock mode (?mock=1) routes every call through window.QG_MOCK
 * (mock-data.js) instead of the network, so this file can be built and
 * verified before the backend exists. Mock mode is loudly labelled on
 * screen (see #mock-banner) and never runs unless the query param is
 * present — it must be impossible to present with it on by accident.
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------------
  // DOM helpers
  // ---------------------------------------------------------------------
  function $(sel, root) {
    return (root || document).querySelector(sel);
  }
  function $$(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === "class") node.className = attrs[k];
        else if (k === "text") node.textContent = attrs[k];
        else node.setAttribute(k, attrs[k]);
      });
    }
    (children || []).forEach(function (c) {
      if (c != null) node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }
  // Wraps an externally-sourced string (an error message, a server-reported
  // endpoint/tool name) in Unicode isolate marks (U+2066 FSI / U+2069 PDI) so
  // it cannot reorder the surrounding literal chrome if it happens to contain
  // bidi control characters or RTL script, without altering how the text
  // itself reads.
  function isolate(s) {
    return "⁦" + String(s) + "⁩";
  }
  function formatInt(n) {
    if (n == null || Number.isNaN(n)) return "—";
    return Number(n).toLocaleString("en-US");
  }
  // Short "git-style" hash preview: enough characters to be visually
  // distinct chain-link-to-chain-link without wrapping the narrow chain
  // strip on a projector. The full value is always still present verbatim
  // in the raw-record drawer (syntaxHighlightJSON), never only here.
  function truncHash(h, n) {
    if (!h) return "—";
    var s = String(h);
    return s.length > n ? s.slice(0, n) + "…" : s;
  }
  function formatMs(ms) {
    if (ms == null || Number.isNaN(ms)) return "—";
    if (ms >= 1000) return (ms / 1000).toLocaleString("en-US", { maximumFractionDigits: 1 }) + " s";
    return Number(ms).toLocaleString("en-US", { maximumFractionDigits: 1 }) + " ms";
  }
  // Fallback only — used when a RunResult carries no masked_columns array at
  // all (older fixtures, or a scenario with nothing masked). Real masked
  // values from the live engine don't reliably match any fixed shape: a
  // last-4 phone mask renders as a bare "0182", a hashed national_id as a
  // bare 32-char hex digest, neither of which looks any different from
  // ordinary data by pattern alone.
  function isMaskedValue(v) {
    if (typeof v !== "string") return false;
    return v.indexOf("•") !== -1 /* • */ || /…$/.test(v) || /^[0-9a-f]{4,}…$/.test(v);
  }
  // Keys the masked styling off column membership in RunResult.masked_columns
  // (an array of column names, possibly qualified like "customers.phone")
  // rather than sniffing the value's shape — the only way "0182" (a real
  // last-4 phone mask) and a bare hex digest (a real hashed national_id) get
  // any visual treatment at all.
  function isMaskedColumn(columnName, maskedColumns) {
    if (!columnName) return false;
    var bare = String(columnName).split(".").pop().toLowerCase();
    return maskedColumns.some(function (m) {
      return String(m).split(".").pop().toLowerCase() === bare;
    });
  }
  // Tokenizes the RAW (un-escaped) JSON.stringify output and returns a
  // DocumentFragment of <span class="json-*"> nodes (via textContent, never
  // innerHTML) interleaved with plain text nodes for the punctuation/
  // whitespace between tokens. This must never HTML-escape-then-regex —
  // QueryGate's real validation errors and SQL both quote identifiers
  // ("Table 'employees' is not accessible..."), and escaping first turns
  // every apostrophe into "&#39;", whose embedded "39" the old number regex
  // then matched and wrapped in a span, splitting the entity so the browser
  // rendered the literal text "&#39;" in the one panel that exists to prove
  // nothing here is faked. Building DOM nodes directly with textContent
  // sidesteps both the fidelity bug and any HTML-injection sink at once.
  var JSON_TOKEN_RE = /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g;
  function syntaxHighlightJSON(value) {
    var json = JSON.stringify(value, null, 2);
    var frag = document.createDocumentFragment();
    var lastIndex = 0;
    var match;
    JSON_TOKEN_RE.lastIndex = 0;
    while ((match = JSON_TOKEN_RE.exec(json)) !== null) {
      if (match.index > lastIndex) {
        frag.appendChild(document.createTextNode(json.slice(lastIndex, match.index)));
      }
      var token = match[0];
      var cls = "json-number";
      if (/^"/.test(token)) cls = /:$/.test(token) ? "json-key" : "json-string";
      else if (/^(true|false)$/.test(token)) cls = "json-boolean";
      else if (token === "null") cls = "json-null";
      frag.appendChild(el("span", { class: cls, text: token }));
      lastIndex = JSON_TOKEN_RE.lastIndex;
    }
    if (lastIndex < json.length) {
      frag.appendChild(document.createTextNode(json.slice(lastIndex)));
    }
    return frag;
  }

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------
  var state = {
    mock: false,
    gate: "off",
    scenarios: [],
    scenarioById: {},
    running: false,
    lastResult: null,
    activeConfigDoc: "policy",
    config: null,
    probeBuffer: [],
    probeMax: 140,
    mockCounter: 812,
    mockOverloadUntil: 0,
  };

  var params = new URLSearchParams(window.location.search);
  state.mock = params.get("mock") === "1";

  // ---------------------------------------------------------------------
  // Transport — real fetch, or mock fixtures. Both resolve to the exact
  // shapes in demo/SPEC.md so render code never branches on mode.
  // ---------------------------------------------------------------------
  function realRequest(path, opts) {
    return fetch(path, opts).then(function (res) {
      return res
        .json()
        .catch(function () {
          return null;
        })
        .then(function (body) {
          if (!res.ok) {
            var msg = (body && (body.detail || body.error || body.message)) || res.status + " " + res.statusText;
            throw new Error(path + " → " + msg);
          }
          return body;
        });
    });
  }

  function mockDelay(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms);
    });
  }

  function mockRequest(path, opts) {
    var method = (opts && opts.method) || "GET";
    var M = window.QG_MOCK;
    if (!M) return Promise.reject(new Error("mock-data.js did not load"));

    if (path === "/api/health" && method === "GET") {
      return mockDelay(120).then(function () {
        return Object.assign({}, M.health);
      });
    }
    if (path === "/api/state" && method === "GET") {
      return mockDelay(80).then(function () {
        return {
          gate: state.gate,
          db_calls: { agent_ro: state.mockCounter },
          probe_ms: currentMockProbeMs(),
        };
      });
    }
    if (path === "/api/scenarios" && method === "GET") {
      return mockDelay(150).then(function () {
        return M.scenarios.slice();
      });
    }
    if (path === "/api/config" && method === "GET") {
      return mockDelay(150).then(function () {
        return Object.assign({}, M.config);
      });
    }
    if (path === "/api/gate" && method === "POST") {
      var body = JSON.parse(opts.body);
      return mockDelay(180).then(function () {
        state.gate = body.gate;
        return { gate: state.gate };
      });
    }
    if (path === "/api/reset" && method === "POST") {
      return mockDelay(220).then(function () {
        state.mockCounter = 812;
        state.mockOverloadUntil = 0;
        return { ok: true };
      });
    }
    if (path === "/api/run" && method === "POST") {
      var reqBody = JSON.parse(opts.body);
      return mockRunScenario(reqBody.scenario_id, reqBody.gate);
    }
    return Promise.reject(new Error("mock transport has no handler for " + method + " " + path));
  }

  function api(path, opts) {
    return state.mock ? mockRequest(path, opts) : realRequest(path, opts);
  }

  function mockRunScenario(scenarioId, gate) {
    var M = window.QG_MOCK;
    var fixture = M.results[gate] && M.results[gate][scenarioId];
    if (!fixture) return Promise.reject(new Error("no mock fixture for " + scenarioId + "/" + gate));

    // Pace the mock so it *feels* like a network round trip; the overload
    // scenario (gate off) also drives the sparkline spike for its duration.
    var pacingMs = 260;
    if (scenarioId === "overload" && gate === "off") {
      pacingMs = 3400;
      state.mockOverloadUntil = Date.now() + pacingMs;
    }

    return mockDelay(pacingMs).then(function () {
      var before = state.mockCounter;
      var after = fixture.touched ? before + fixture.callDelta : before;
      state.mockCounter = after;
      return {
        scenario_id: scenarioId,
        gate: gate,
        mcp: fixture.mcp,
        outcome: fixture.outcome,
        blocked_by: fixture.blocked_by,
        policy_lines: fixture.policy_lines,
        latency_ms: fixture.latency_ms,
        db: { calls_before: before, calls_after: after, touched: fixture.touched },
        rows: fixture.rows,
        columns: fixture.columns,
        row_count: fixture.row_count,
        masked_columns: fixture.masked_columns,
        audit: fixture.audit,
        note: fixture.note,
      };
    });
  }

  function currentMockProbeMs() {
    var last = state.probeBuffer[state.probeBuffer.length - 1];
    return last ? last.ms : window.QG_MOCK.probe.baseline[state.gate];
  }

  // ---------------------------------------------------------------------
  // Mood — the single biggest signal on the page. Flipping the gate swaps
  // the whole surface's register between "alarmed" (off) and "calm" (on).
  // ---------------------------------------------------------------------
  function applyMood(gate) {
    document.body.dataset.gate = gate;
    var toggle = $("#gate-toggle");
    var isOn = gate === "on";
    toggle.setAttribute("aria-checked", String(isOn));
    toggle.setAttribute("aria-label", "QueryGate protection is " + (isOn ? "on" : "off") + ". Activate to turn it " + (isOn ? "off" : "on") + ".");
    $("#mood-eyebrow").textContent = isOn ? "ACT 3 — GATE ON" : "ACT 1 — GATE OFF";
    $("#gate-headline").textContent = isOn
      ? "Same agent, same request, same database credential. Now it's governed."
      : "Anything the agent asks for, the database hands over.";
    $("#gate-sub").textContent = isOn
      ? "Refused before your database is touched — or bounded before it answers."
      : "Identical database privileges. Opposite outcomes. Flip it.";
  }

  // ---------------------------------------------------------------------
  // Scenario list
  // ---------------------------------------------------------------------
  function renderScenarios() {
    var list = $("#scenario-list");
    list.innerHTML = "";
    state.scenarios.forEach(function (sc, i) {
      var num = i + 1;
      var row = el("li", { class: "scenario-row", id: "scenario-" + sc.id });
      var btn = el("button", {
        class: "scenario-button",
        type: "button",
        "data-scenario-id": sc.id,
      });
      btn.appendChild(el("span", { class: "scenario-num", "aria-hidden": "true", text: String(num) }));
      var body = el("div", { class: "scenario-body" });
      body.appendChild(el("p", { class: "scenario-ask", text: "“" + sc.ask + "”" }));
      // No aria-describedby back to this <p>: it already sits inside the
      // button as visible content, so it is already part of the button's
      // accessible name computation. Pointing aria-describedby at it too
      // made a screen reader announce the same sentence twice per button.
      body.appendChild(el("p", { class: "scenario-point", text: sc.point }));
      btn.appendChild(body);
      btn.appendChild(el("span", { class: "scenario-run", "aria-hidden": "true", text: "Run" }));
      btn.addEventListener("click", function () {
        runScenario(sc.id);
      });
      row.appendChild(btn);
      list.appendChild(row);
    });
  }

  // Uses aria-disabled, not the disabled attribute: a natively-disabled
  // button drops out of the tab order, so a keyboard user whose focus was on
  // one of these buttons when a scenario started got bumped all the way back
  // to the top of the document once it finished. aria-disabled keeps the
  // button focusable and in tab order; runScenario()'s own `state.running`
  // guard (below) is what actually makes a click on it a no-op meanwhile.
  function setScenarioBusy(scenarioId, busy) {
    $$(".scenario-button").forEach(function (b) {
      var isInert = busy && b.getAttribute("data-scenario-id") !== scenarioId;
      b.setAttribute("aria-disabled", String(isInert));
    });
    var active = $('.scenario-button[data-scenario-id="' + scenarioId + '"]');
    if (active) {
      active.classList.toggle("is-running", busy);
      active.querySelector(".scenario-run").textContent = busy ? "Running…" : "Run";
    }
  }

  // ---------------------------------------------------------------------
  // Result rendering
  // ---------------------------------------------------------------------
  function renderResult(scenario, result) {
    state.lastResult = result;
    $("#result-empty").hidden = true;
    var panel = $("#result-panel");
    panel.hidden = false;

    var actLabel = { recon: "RECON", confidentiality: "CONFIDENTIALITY", availability: "AVAILABILITY", overhead: "OVERHEAD" }[scenario.act] || scenario.act.toUpperCase();
    $("#result-act").textContent = actLabel;
    $("#result-title").textContent = scenario.title;

    var badge = $("#outcome-badge");
    badge.className = "outcome-badge outcome-" + result.outcome;
    badge.textContent = result.outcome.toUpperCase();

    $("#result-note").textContent = result.note || "";

    var db = result.db;
    var untouched = !db.touched && db.calls_after === db.calls_before;
    $("#db-before").textContent = formatInt(db.calls_before);
    $("#db-after").textContent = formatInt(db.calls_after);
    var indicator = $("#db-indicator");
    indicator.className = "db-indicator " + (untouched ? "db-untouched" : "db-touched");
    $("#db-verdict-tag").textContent = untouched ? "NEVER CONTACTED" : "CONTACTED";
    $("#db-verdict-text").textContent = untouched
      ? "Queries executed: 0. Your database never saw this request."
      : (db.calls_after - db.calls_before) + " quer" + (db.calls_after - db.calls_before === 1 ? "y" : "ies") + " reached your database.";

    $("#metric-latency").textContent = formatMs(result.latency_ms);
    $("#metric-rows").textContent = formatInt(result.row_count);

    var blockedMetric = $("#blocked-by-metric");
    if (result.blocked_by) {
      blockedMetric.hidden = false;
      $("#metric-blocked-by").textContent = result.blocked_by;
    } else {
      blockedMetric.hidden = true;
    }

    renderRowsTable(scenario, result);
    renderPayload(result);
    renderAudit(result);

    state.activeConfigDoc = "policy";
    setActiveConfigTab("policy");
    renderConfigDoc();

    announce(scenario, result);
  }

  function renderRowsTable(scenario, result) {
    var wrap = $("#rows-preview-wrap");
    var table = $("#rows-table");
    table.innerHTML = "";
    var rows = result.rows || [];
    var columns = result.columns || [];
    if (!rows.length || !columns.length) {
      wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    $("#rows-preview-count").textContent = "(" + formatInt(rows.length) + " of " + formatInt(result.row_count) + ")";

    var isRawPii = scenario.act === "confidentiality" && result.gate === "off" && result.outcome === "allowed";
    table.classList.toggle("rows-table-leak", isRawPii);

    var maskedColumns = Array.isArray(result.masked_columns) ? result.masked_columns : null;

    var thead = el("thead", {}, [el("tr", {}, columns.map(function (c) { return el("th", { scope: "col", text: c }); }))]);
    table.appendChild(thead);

    var tbody = el("tbody");
    rows.forEach(function (row) {
      var tr = el("tr");
      row.forEach(function (cell, colIdx) {
        var text = cell == null ? "" : String(cell);
        var td = el("td");
        // masked_columns (when the backend supplies it) is authoritative;
        // isMaskedValue's shape-sniffing is a fallback for results that
        // don't carry it, not the primary signal — a real mask can render as
        // plain-looking text ("0182", a bare hex digest) with nothing about
        // its shape to distinguish it from ordinary data.
        var masked = maskedColumns ? isMaskedColumn(columns[colIdx], maskedColumns) : isMaskedValue(text);
        if (masked) {
          // sr-only text, not a title attribute: title reaches neither
          // keyboard-only nor touch users, so it was a sighted-mouse-only
          // affordance for the one beat that most needs to be unambiguous.
          var span = el("span", { class: "masked-value" }, [text]);
          span.appendChild(el("span", { class: "sr-only", text: " (masked by policy)" }));
          td.appendChild(span);
        } else {
          td.textContent = text;
          if (isRawPii) td.classList.add("sensitive-value");
        }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
  }

  function renderPayload(result) {
    var mcp = result.mcp || {};
    var reqEl = $("#payload-request");
    var resEl = $("#payload-response");
    reqEl.textContent = "";
    reqEl.appendChild(syntaxHighlightJSON(mcp.request || {}));
    resEl.textContent = "";
    resEl.appendChild(syntaxHighlightJSON(mcp.response || {}));
    var mockTag = state.mock ? " — MOCK FIXTURE, not a live call" : " — a real MCP tools/call over HTTP";
    $("#payload-endpoint").textContent =
      isolate(mcp.server || "?") + " · " + isolate(mcp.tool || "?") + " · " + isolate(mcp.endpoint || "") + mockTag;
    $("#payload-drawer").open = false;
  }

  // ---------------------------------------------------------------------
  // Audit ledger panel — the real hash-chained record(s) this run appended
  // (or, for a gate-OFF run against the baseline server, the real absence
  // of any). result.audit is the RunResult["audit"] block app.py/audit.py
  // build from an actual tail-read of the real ledger file; nothing here
  // is computed or guessed client-side except which of the (already real)
  // captured records to headline when a run appended more than one.
  // ---------------------------------------------------------------------
  // pickHeadlineAuditRecord (A4): the backend now identifies, at the exact
  // moment the deciding real MCP call is made, which ledger record actually
  // determined this RunResult's `outcome` — threaded through as
  // audit.headline_event_id (demo/control/audit.py's audit_block_for_run,
  // demo/control/scenarios.py's _qg_call_with_headline). Selecting by that
  // id — rather than re-guessing from the (capped, earliest-5) preview list
  // by matching policy_decision to outcome — is what makes it impossible
  // for the badge and the headline record to disagree: multi-call
  // scenarios (legit's 15 calls, overload's 20) derive `outcome` from a
  // call that is NOT guaranteed to land in the first 5 records shown here.
  //
  // Returns {record, notInPreview}. When the backend named a headline id
  // that isn't among the (capped) records actually sent to the browser,
  // notInPreview is true — the caller must show that honestly rather than
  // fall back to a different, possibly wrong-verdict record. When the
  // backend didn't name one at all (headlineEventId is null/undefined —
  // e.g. overload's outcome is an aggregate over 20 calls, decided by no
  // single record), fall back to the old decision-matching heuristic; that
  // fallback is only safe when every candidate record agrees, which is the
  // one case it's used for.
  function pickHeadlineAuditRecord(records, outcome, headlineEventId) {
    if (!records || !records.length) return { record: null, notInPreview: false };
    if (headlineEventId) {
      var byId = records.find(function (r) {
        return r.event && r.event.event_id === headlineEventId;
      });
      if (byId) return { record: byId, notInPreview: false };
      return { record: null, notInPreview: true };
    }
    var wantDecision = outcome === "blocked" ? "denied" : "allowed";
    var match = records.find(function (r) {
      return r.event && r.event.policy_decision === wantDecision;
    });
    return { record: match || records[records.length - 1], notInPreview: false };
  }

  // renderAuditChainBadge (A1 + the follow-up review that extended it):
  // chain_status is the machine-readable tri-state the backend now returns
  // ("ok" | "not_yet_initialized" | "broken" — demo/control/audit.py's
  // ChainStatus) instead of overloading one boolean. A run against a fresh
  // ledger (no file written yet — the very first scenario click of the
  // day) or a genuinely empty-but-existing ledger must never render the
  // same alarmed "CHAIN BROKEN" treatment a real tampering finding gets:
  // only status "broken" does. The green "CHAIN VERIFIED" state additionally
  // requires records_checked > 0 — an "ok" status over zero records is
  // vacuously true, not something to present as verified PROOF on a
  // projector.
  function renderAuditChainBadge(audit) {
    var badge = $("#audit-chain-badge");
    var text = $("#audit-chain-badge-text");
    var broken = $("#audit-chain-broken");
    badge.classList.remove("audit-chain-ok", "audit-chain-bad", "audit-chain-unknown");

    var status = audit ? audit.chain_status : null;
    if (!status && audit) {
      // Backward-compat for a payload shape without chain_status (an older
      // mock fixture, or a server that hasn't been redeployed yet): never
      // infer the alarmed state from chain_verified alone.
      status = audit.chain_verified === false ? "broken" : "ok";
    }
    var recordsChecked = audit && typeof audit.records_checked === "number" ? audit.records_checked : 0;

    if (status === "broken") {
      badge.classList.add("audit-chain-bad");
      text.textContent = "CHAIN BROKEN";
      broken.hidden = false;
      broken.textContent =
        "The audit ledger did not verify after this run" + (audit && audit.chain_error ? ": " + isolate(audit.chain_error) : ".") + " Treat this as a real finding, not a display glitch.";
      return;
    }

    broken.hidden = true;
    broken.textContent = "";

    if (status === "ok" && recordsChecked > 0) {
      badge.classList.add("audit-chain-ok");
      text.textContent = "CHAIN VERIFIED";
      return;
    }

    badge.classList.add("audit-chain-unknown");
    text.textContent = status ? "NO RECORDS YET" : "CHAIN STATUS UNKNOWN";
  }

  // Why a real run legitimately shows 0 audit records, chosen from fields
  // already present on the RunResult — never a guess. Two genuinely
  // different reasons produce the same number: the baseline server has no
  // audit sink at all (gate off, every scenario), and a metadata read like
  // list_tables isn't a query execution QueryGate's ledger records today
  // (gate on, scenario `recon` specifically) — conflating the two would
  // misrepresent the second as a product gap it isn't.
  // Shared by renderAudit's alarmed-empty-state styling and announce's live
  // region (A5): true exactly for the "the gate is off and this left no
  // trace anywhere" case — never for a gate-ON metadata read (list_tables),
  // which also legitimately shows 0 audit records for an unrelated, benign
  // reason.
  function auditLeftNoTraceAtAll(result) {
    var count = result.audit ? result.audit.count : 0;
    return count === 0 && !!(result.mcp && result.mcp.server === "baseline-postgres-mcp");
  }

  function auditEmptyReason(result) {
    var mcp = result.mcp || {};
    if (mcp.server === "baseline-postgres-mcp") {
      return "The baseline server has no audit sink at all — this action left no trace anywhere, on any log, on this machine.";
    }
    if (mcp.tool && mcp.tool !== "run_structured_queries") {
      return isolate(mcp.tool) + " is a metadata read, not a query execution — QueryGate's audit ledger records query executions. (The employees table it can't even name is still the point of scenario 2.)";
    }
    return "No new ledger entries were appended for this run.";
  }

  function renderAudit(result) {
    var audit = result.audit || null;
    renderAuditChainBadge(audit);

    var empty = $("#audit-empty");
    var content = $("#audit-content");
    var records = (audit && audit.records) || [];
    var count = audit ? audit.count : 0;

    if (!audit || count === 0) {
      content.hidden = true;
      empty.hidden = false;
      // Alarmed styling only for the "left no trace anywhere" case (the
      // baseline server, no audit sink at all) — a gate-ON metadata read
      // (list_tables) also legitimately shows 0 for an unrelated, benign
      // reason and must not borrow the same visual alarm.
      empty.classList.toggle("audit-empty-alarm", auditLeftNoTraceAtAll(result));
      $("#audit-empty-headline").textContent = "0 audit records.";
      $("#audit-empty-reason").textContent = audit ? auditEmptyReason(result) : "No audit information was returned for this run.";
      return;
    }
    empty.classList.remove("audit-empty-alarm");

    empty.hidden = true;
    content.hidden = false;

    var picked = pickHeadlineAuditRecord(records, result.outcome, audit.headline_event_id);
    if (picked.notInPreview) {
      // A4: the backend named the exact record that decided this run's
      // outcome, but it isn't among the (capped) records sent to the
      // browser. Showing a DIFFERENT record here — even one that matches
      // on decision — could disagree with the outcome badge next to it, so
      // this is an explicit, honest state rather than a silent guess.
      content.hidden = true;
      empty.hidden = false;
      $("#audit-empty-headline").textContent = formatInt(count) + " audit record(s) recorded.";
      $("#audit-empty-reason").textContent =
        "The record that decided this run's outcome (id " + isolate(audit.headline_event_id) + ") wasn't included in this response's preview — open the raw ledger to find it.";
      return;
    }
    var headline = picked.record;
    if (!headline) {
      // count > 0 but the (capped) preview list is empty — shouldn't
      // happen, but don't render a chain strip from nothing.
      content.hidden = true;
      empty.hidden = false;
      $("#audit-empty-headline").textContent = formatInt(count) + " audit record(s) recorded.";
      $("#audit-empty-reason").textContent = "None were included in this response's preview to display in detail.";
      return;
    }
    var ev = headline.event || {};

    $("#audit-seq").textContent = headline.seq != null ? String(headline.seq) : "—";
    $("#audit-prev-hash").textContent = truncHash(headline.prev_hash, 16);
    $("#audit-hash").textContent = truncHash(headline.hash, 16);

    $("#audit-who").textContent = isolate(ev.principal_id || "?") + " via " + isolate(ev.auth_method || "?") + " on " + isolate(ev.surface || "?");
    $("#audit-what").textContent = isolate(ev.operation || "?") + " on connection " + isolate(ev.connection_id || "?");

    var verdictEl = $("#audit-verdict");
    var decision = ev.policy_decision || "unknown";
    verdictEl.textContent = decision.toUpperCase();
    verdictEl.className = "audit-verdict audit-verdict-" + decision;

    var countNote = $("#audit-count-note");
    // Only claim a record "decided the outcome" when the BACKEND said so. On a
    // gate-OFF `legit` run the outcome comes from the baseline server (which
    // has no audit sink), while QueryGate's own measurement loop still appends
    // real records — so the backend deliberately leaves headline_event_id
    // unset. Captioning one of those records as authoritative would be two
    // false statements at once, on the panel whose whole point is that nothing
    // here is fabricated.
    if (!audit.headline_event_id) {
      countNote.textContent =
        formatInt(count) + " ledger record(s) were appended while this run executed. None of them decided " +
        "the outcome above — that came from a server with no audit trail at all.";
    } else if (count > records.length) {
      countNote.textContent = "Showing " + formatInt(records.length) + " of " + formatInt(count) + " ledger records this run appended, including the one that decided the outcome above.";
    } else if (count > 1) {
      countNote.textContent = formatInt(count) + " ledger records appended this run — showing the one that decided the outcome above.";
    } else {
      countNote.textContent = "1 ledger record appended this run.";
    }

    var rawEl = $("#audit-raw");
    rawEl.textContent = "";
    rawEl.appendChild(syntaxHighlightJSON(headline));
    $("#audit-raw-drawer").open = false;
  }

  function announce(scenario, result) {
    var msg = scenario.title + ": " + result.outcome.toUpperCase() + ". ";
    if (result.blocked_by) msg += "Blocked by " + result.blocked_by + ". ";
    var db = result.db;
    msg += db.touched ? (db.calls_after - db.calls_before) + " database call(s) made. " : "Database not touched. ";
    msg += formatMs(result.latency_ms) + ", " + formatInt(result.row_count) + " rows. ";
    var auditCount = result.audit ? result.audit.count : 0;
    msg += auditCount > 0 ? formatInt(auditCount) + " audit record(s) written." : "0 audit records.";
    // A5: the demo's central argument for this exact state — gate off, zero
    // audit records — is written into the DOM (#audit-empty-reason) but was
    // never spoken by the one live region on the page. Fold it into the
    // same announcement rather than opening a second live region (a
    // reviewer confirmed the sparkline is correctly outside every live
    // region, and that must stay true).
    if (auditLeftNoTraceAtAll(result)) {
      msg += " This action left no trace anywhere, on any log, on this machine.";
    }
    $("#live-announcer").textContent = msg;
  }

  // ---------------------------------------------------------------------
  // Config panel (Act 2)
  // ---------------------------------------------------------------------
  function setActiveConfigTab(doc) {
    state.activeConfigDoc = doc;
    $$(".document-tab").forEach(function (tab) {
      var active = tab.dataset.doc === doc;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    $("#config-code").setAttribute("aria-labelledby", "tab-" + doc);
    renderConfigDoc();
  }

  function renderConfigDoc() {
    if (!state.config) return;
    var doc = state.activeConfigDoc;
    var text = doc === "policy" ? state.config.policy_yaml : state.config.connections_yaml;
    var highlight = {};
    if (doc === "policy" && state.lastResult && state.lastResult.policy_lines) {
      state.lastResult.policy_lines.forEach(function (n) {
        highlight[n] = true;
      });
    }
    var container = $("#config-code");
    container.innerHTML = "";
    var firstHit = null;
    text.split("\n").forEach(function (line, idx) {
      var n = idx + 1;
      var isHit = !!highlight[n];
      var row = el("div", { class: "config-line" + (isHit ? " config-line-hit" : ""), "data-line": String(n) });
      row.appendChild(el("span", { class: "config-line-no", "aria-hidden": "true", text: String(n) }));
      var codeEl = el("code", { class: "config-line-text" });
      // The highlight itself is a background-color wash — invisible to a
      // screen reader and, per WCAG 1.4.1, not allowed to be the *only* way
      // the "this is the rule that fired" signal is conveyed. The left
      // border (CSS) covers low-vision/color-blind sighted users; this
      // sr-only prefix covers screen readers.
      if (isHit) codeEl.appendChild(el("span", { class: "sr-only", text: "Cited rule, line " + n + ": " }));
      codeEl.appendChild(document.createTextNode(line.length ? line : " "));
      row.appendChild(codeEl);
      container.appendChild(row);
      if (isHit && !firstHit) firstHit = row;
    });
    // Scroll just the inner code panel to the highlighted rule — never the
    // outer page — so the rule that blocked a run is visible without the
    // presenter hunting for it inside this box's own scroll region.
    if (firstHit) {
      // getBoundingClientRect (not offsetTop) so this is correct regardless
      // of which ancestor ends up as the offsetParent.
      var rowRect = firstHit.getBoundingClientRect();
      var containerRect = container.getBoundingClientRect();
      var delta = rowRect.top - containerRect.top;
      var target = container.scrollTop + delta - container.clientHeight / 2 + firstHit.clientHeight / 2;
      container.scrollTop = Math.max(0, target);
    } else {
      container.scrollTop = 0;
    }
  }

  // ---------------------------------------------------------------------
  // Health strip
  // ---------------------------------------------------------------------
  function renderHealth(health, ok) {
    ["db", "baseline_mcp", "querygate"].forEach(function (svc) {
      var chip = $('.health-chip[data-service="' + svc + '"]');
      // Base the accessible name on the chip's own visible text ("Database",
      // "Baseline MCP", "QueryGate"), not the raw data-service key: an
      // aria-label of "db is up" doesn't contain the visible label "Database"
      // at all, which breaks WCAG 2.5.3 Label in Name — a voice-control user
      // saying "click Database" wouldn't match it.
      var label = chip.querySelector("span").textContent;
      chip.classList.remove("health-up", "health-down", "health-unknown");
      if (!ok) {
        chip.classList.add("health-unknown");
        chip.setAttribute("aria-label", label + ": health unknown — could not reach the control backend");
        return;
      }
      var up = !!health[svc];
      chip.classList.add(up ? "health-up" : "health-down");
      chip.setAttribute("aria-label", label + ": " + (up ? "up" : "down"));
    });
  }

  function pollHealth() {
    api("/api/health", { method: "GET" })
      .then(function (health) {
        renderHealth(health, true);
        clearError("health");
      })
      .catch(function (err) {
        renderHealth({}, false);
        showError("Health check failed: " + isolate(err.message), "health");
      });
  }

  // ---------------------------------------------------------------------
  // Probe sparkline
  // ---------------------------------------------------------------------
  function pushProbePoint(point) {
    state.probeBuffer.push(point);
    if (state.probeBuffer.length > state.probeMax) state.probeBuffer.shift();
    renderProbe();
  }

  // Real SSE points arrive every ~400ms (config.PROBE_INTERVAL_SECONDS) and
  // mock points every 220ms (see startMockProbe below); redrawing the SVG
  // path on every one of them is a continuous animation that ignores the
  // viewer's OS-level motion preference entirely — the CSS reduced-motion
  // block only ever covered CSS animations/transitions, never this JS-driven
  // redraw. Follow landing/index.html's pattern: check the media query once
  // and gate the *visual* update on it, while the numeric readout and live/
  // mock status — which are information, not motion — still update on every
  // point regardless.
  var prefersReducedMotion = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  var PATH_REDRAW_THROTTLE_MS = 1000;
  var lastPathRenderAt = 0;

  function renderProbe() {
    var buf = state.probeBuffer;
    var path = $("#probe-path");
    var valueEl = $("#probe-value");
    var statusEl = $("#probe-status");
    if (!buf.length) {
      path.setAttribute("d", "");
      path.classList.remove("probe-line-hot");
      valueEl.textContent = "— ms";
      valueEl.classList.remove("probe-value-hot");
      statusEl.textContent = state.mock ? "mock feed" : "connecting…";
      lastPathRenderAt = 0;
      return;
    }

    var last = buf[buf.length - 1];
    valueEl.textContent = formatMs(last.ms);
    valueEl.classList.toggle("probe-value-hot", !!last.hot);
    statusEl.textContent = state.mock ? "mock feed" : "live";
    statusEl.classList.remove("probe-status-error");

    var now = Date.now();
    if (prefersReducedMotion && lastPathRenderAt && now - lastPathRenderAt < PATH_REDRAW_THROTTLE_MS) {
      return; // data + readout are current; the waveform itself redraws statically
    }
    lastPathRenderAt = now;

    var w = 600,
      h = 64,
      padTop = 6,
      padBottom = 6;
    var usableH = h - padTop - padBottom;
    var scaleRef = 3000; // ms that maps to the top of the strip (sqrt scale)
    var pts = buf.map(function (p, i) {
      var x = buf.length === 1 ? 0 : (i / (buf.length - 1)) * w;
      var norm = Math.min(1, Math.sqrt(Math.max(0, p.ms) / scaleRef));
      var y = padTop + (1 - norm) * usableH;
      return x.toFixed(1) + "," + y.toFixed(1);
    });
    path.setAttribute("d", "M" + pts.join(" L"));
    path.classList.toggle("probe-line-hot", !!last.hot);
  }

  function seedProbeBaseline() {
    // Only mock mode gets a pre-seeded flat line — it's fixture data,
    // already loudly labelled. Real mode must never paint a plausible-looking
    // number before a genuine SSE sample has arrived; it stays empty ("— ms",
    // "connecting…") until startRealProbe() gets a real point.
    if (!state.mock) {
      renderProbe();
      return;
    }
    var baseMs = window.QG_MOCK.probe.baseline[state.gate];
    for (var i = 0; i < 40; i++) {
      state.probeBuffer.push({ t: new Date().toISOString(), ms: baseMs, hot: false });
    }
    renderProbe();
  }

  var mockProbeTimer = null;
  function startMockProbe() {
    if (mockProbeTimer) return;
    mockProbeTimer = setInterval(function () {
      var M = window.QG_MOCK;
      var base = M.probe.baseline[state.gate];
      var jitter = (Math.random() - 0.5) * 2 * M.probe.baseline.jitter;
      var ms = Math.max(0.4, base + jitter);
      var hot = false;

      if (state.mockOverloadUntil && Date.now() < state.mockOverloadUntil && state.gate === "off") {
        var remain = state.mockOverloadUntil - Date.now();
        var total = 3400;
        var elapsed = total - remain;
        var spike = M.probe.spike;
        if (elapsed < 400) {
          ms = base + (spike.peakMs - base) * (elapsed / 400);
        } else if (elapsed < 2600) {
          ms = spike.peakMs + (Math.random() - 0.5) * 200;
        } else {
          ms = spike.peakMs * (1 - (elapsed - 2600) / 800);
        }
        hot = ms > 150;
      } else if (state.mockOverloadUntil && Date.now() >= state.mockOverloadUntil) {
        state.mockOverloadUntil = 0;
      }

      pushProbePoint({ t: new Date().toISOString(), ms: Math.max(0.4, ms), hot: hot });
    }, 220);
  }

  var realEventSource = null;
  function startRealProbe() {
    try {
      realEventSource = new EventSource("/api/probe");
    } catch (e) {
      $("#probe-status").textContent = "unavailable";
      $("#probe-status").classList.add("probe-status-error");
      return;
    }
    $("#probe-status").textContent = "connecting…";
    realEventSource.onopen = function () {
      $(".probe-strip").classList.remove("probe-strip-disconnected");
      $("#probe-status").textContent = "live";
      $("#probe-status").classList.remove("probe-status-error");
      clearError("probe");
    };
    realEventSource.onmessage = function (evt) {
      try {
        var point = JSON.parse(evt.data);
        pushProbePoint(point);
      } catch (e) {
        /* malformed event — skip it, do not fabricate a point */
      }
    };
    realEventSource.onerror = function () {
      // The presenter is instructed to read this number aloud — on a real
      // disconnect (e.g. the control backend itself dying) the strip must
      // never keep showing its last sampled value as though it were still
      // live. Blank the value, drop the drawn path, and grey the whole
      // strip so "disconnected" is unmistakable at a glance, not just in the
      // small status word.
      state.probeBuffer = [];
      renderProbe();
      var statusEl = $("#probe-status");
      statusEl.textContent = "disconnected";
      statusEl.classList.add("probe-status-error");
      $(".probe-strip").classList.add("probe-strip-disconnected");
      showError("Live database probe stream disconnected.", "probe");
    };
  }

  // ---------------------------------------------------------------------
  // Error banner
  //
  // Scoped by category so an unrelated success (e.g. a routine 4s health
  // poll succeeding) can never silently wipe a *different* category's error
  // off the screen — in particular, a failed scenario run must stay visible
  // even after the next health poll comes back green. showError()/clearError()
  // called with no category are the "explicit user action" form (dismiss
  // button, reset, initial load) and always act unconditionally.
  // ---------------------------------------------------------------------
  var currentErrorCategory = null;
  function showError(message, category) {
    currentErrorCategory = category || null;
    var banner = $("#error-banner");
    // Unhide before setting content: role="alert" is only reliably announced
    // by assistive tech when the DOM mutation that populates it happens while
    // it is already part of the accessibility tree. Setting text on a still-
    // hidden node and unhiding afterward is the unreliable order.
    banner.hidden = false;
    banner.textContent = message + " ";
    var dismiss = el("button", { class: "error-dismiss", type: "button", "aria-label": "Dismiss error", text: "✕" });
    dismiss.addEventListener("click", function () {
      clearError();
    });
    banner.appendChild(dismiss);
    banner.scrollIntoView({ block: "nearest" });
  }
  function clearError(category) {
    if (category && currentErrorCategory !== category) return;
    currentErrorCategory = null;
    var banner = $("#error-banner");
    banner.hidden = true;
    banner.textContent = "";
  }

  // ---------------------------------------------------------------------
  // Actions
  // ---------------------------------------------------------------------
  function toggleGate() {
    var toggle = $("#gate-toggle");
    if (toggle.getAttribute("aria-disabled") === "true") return; // see setScenarioBusy's comment: aria-disabled, not disabled, keeps focus
    var next = state.gate === "on" ? "off" : "on";
    toggle.setAttribute("aria-disabled", "true");
    api("/api/gate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ gate: next }) })
      .then(function (res) {
        state.gate = res.gate;
        applyMood(state.gate);
        clearError("gate");
        $("#live-announcer").textContent = "QueryGate is now " + state.gate.toUpperCase() + ".";
      })
      .catch(function (err) {
        showError("Could not flip the gate: " + isolate(err.message), "gate");
      })
      .then(function () {
        toggle.setAttribute("aria-disabled", "false");
      });
  }

  function runScenario(scenarioId) {
    if (state.running) return;
    var scenario = state.scenarioById[scenarioId];
    if (!scenario) return;
    state.running = true;
    setScenarioBusy(scenarioId, true);
    var panel = $("#result-panel");
    // Reaches assistive tech even though the only other "running" signal
    // (the button's spinner-ish "Running…" label swap) lives inside an
    // aria-hidden span — otherwise the 15s overload scenario runs in total
    // silence for anyone not looking at the screen.
    panel.setAttribute("aria-busy", "true");
    $("#live-announcer").textContent = "Running " + scenario.title + "…";
    api("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario_id: scenarioId, gate: state.gate }),
    })
      .then(function (result) {
        renderResult(scenario, result);
        clearError("run");
      })
      .catch(function (err) {
        // A failed run must never leave the *previous* scenario's outcome
        // badge, latency, rows, and DB counters on screen — four seconds
        // later a routine health-poll success (scoped to its own "health"
        // category, so it can't reach into this one) would otherwise be the
        // only other thing that touches the error banner, wiping the error
        // while the stale result panel stays up and looks like a live
        // success for the wrong scenario. Blank it back to the empty state.
        state.lastResult = null;
        panel.hidden = true;
        $("#result-empty").hidden = false;
        showError('Scenario "' + isolate(scenario.title) + '" failed: ' + isolate(err.message), "run");
      })
      .then(function () {
        state.running = false;
        setScenarioBusy(scenarioId, false);
        panel.setAttribute("aria-busy", "false");
      });
  }

  function resetDemo() {
    var btn = $("#reset-btn");
    if (btn.getAttribute("aria-disabled") === "true") return;
    btn.setAttribute("aria-disabled", "true");
    api("/api/reset", { method: "POST" })
      .then(function () {
        state.lastResult = null;
        $("#result-panel").hidden = true;
        $("#result-empty").hidden = false;
        state.probeBuffer = [];
        seedProbeBaseline();
        renderConfigDoc();
        clearError();
        $("#live-announcer").textContent = "Demo reset.";
      })
      .catch(function (err) {
        showError("Reset failed: " + isolate(err.message), "reset");
      })
      .then(function () {
        btn.setAttribute("aria-disabled", "false");
      });
  }

  // ---------------------------------------------------------------------
  // Keyboard shortcuts
  // ---------------------------------------------------------------------
  function isTypingTarget(t) {
    return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
  }

  function onKeydown(evt) {
    var dialog = $("#help-dialog");
    if (dialog.open) return; // let the dialog's own Esc/close handling work
    if (isTypingTarget(evt.target)) return;
    if (evt.metaKey || evt.ctrlKey || evt.altKey) return;

    if (evt.key >= "1" && evt.key <= "6") {
      var idx = Number(evt.key) - 1;
      var sc = state.scenarios[idx];
      if (sc) {
        evt.preventDefault();
        runScenario(sc.id);
      }
    } else if (evt.key === "g" || evt.key === "G") {
      evt.preventDefault();
      toggleGate();
    } else if (evt.key === "r" || evt.key === "R") {
      evt.preventDefault();
      resetDemo();
    } else if (evt.key === "?") {
      evt.preventDefault();
      dialog.showModal();
    }
  }

  // ---------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------
  function init() {
    document.body.dataset.mock = state.mock ? "1" : "0";
    $("#mock-banner").hidden = !state.mock;

    $("#gate-toggle").addEventListener("click", toggleGate);
    $("#reset-btn").addEventListener("click", resetDemo);
    $("#help-btn").addEventListener("click", function () {
      $("#help-dialog").showModal();
    });
    $$(".document-tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        setActiveConfigTab(tab.dataset.doc);
      });
    });
    $$(".health-chip").forEach(function (chip) {
      chip.addEventListener("click", pollHealth);
    });
    document.addEventListener("keydown", onKeydown);

    seedProbeBaseline();
    if (state.mock) startMockProbe();
    else startRealProbe();

    pollHealth();
    setInterval(pollHealth, 4000);

    Promise.all([api("/api/scenarios", { method: "GET" }), api("/api/config", { method: "GET" }), api("/api/state", { method: "GET" })])
      .then(function (results) {
        state.scenarios = results[0];
        state.scenarioById = {};
        state.scenarios.forEach(function (sc) {
          state.scenarioById[sc.id] = sc;
        });
        renderScenarios();

        state.config = results[1];

        var initState = results[2];
        state.gate = initState.gate;
        applyMood(state.gate);
        renderConfigDoc();
        // No unscoped clearError() here: this success branch races
        // pollHealth()'s own "health"-categorized error (both fire from
        // init()), and an unscoped clear wipes ANY category — including a
        // genuine, still-true health-down banner — the moment scenarios/
        // config/state happen to load successfully, even though the health
        // strip itself keeps showing the down chip. There is nothing
        // load-specific to clear here that the "health" category and the
        // catch branch below don't already own.
      })
      .catch(function (err) {
        showError("Could not load the demo: " + isolate(err.message));
        applyMood(state.gate);
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
