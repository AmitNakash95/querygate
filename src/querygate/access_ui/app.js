(() => {
  "use strict";

  const API = "/api/v1";

  const state = {
    token: "",
    access: null,
    connections: [],
    selectedConnection: null,
    tables: [],
    selectedTable: null,
    denials: null,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  }[character]));

  class ApiError extends Error {
    constructor(status, detail) {
      super(detail || `Request failed with status ${status}`);
      this.status = status;
    }
  }

  function detailText(body, fallback) {
    if (!body) return fallback;
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) return body.detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
    return fallback;
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
    if (options.body !== undefined) headers.set("Content-Type", "application/json");
    const response = await fetch(`${API}${path}`, { ...options, headers });
    const isJson = (response.headers.get("content-type") || "").includes("json");
    const body = isJson ? await response.json() : null;
    if (!response.ok) throw new ApiError(response.status, detailText(body, response.statusText));
    return body;
  }

  function toast(message, kind = "") {
    const item = document.createElement("div");
    item.className = `toast ${kind}`.trim();
    item.textContent = message;
    $("#toast-region").append(item);
    window.setTimeout(() => item.remove(), 4200);
  }

  function setBanner(message = "") {
    const banner = $("#global-banner");
    banner.textContent = message;
    banner.hidden = !message;
  }

  function setBusy(button, busy, busyLabel = "Working…") {
    if (!button) return;
    if (busy) {
      button.dataset.label = button.textContent;
      button.textContent = busyLabel;
      button.disabled = true;
    } else {
      button.textContent = button.dataset.label || button.textContent;
      button.disabled = false;
    }
  }

  const GUARDRAIL_LABELS = {
    max_limit: "Max rows (row select)",
    max_limit_aggregate: "Max rows (aggregate)",
    max_joins: "Max joins",
    max_select_columns: "Max select columns",
    max_where_depth: "Max where depth",
    max_group_by: "Max group-by columns",
    max_top_n: "Max top-N",
    max_batch_size: "Max batch size",
    timeout_seconds: "Query timeout (s)",
    max_concurrency: "Max concurrent queries",
    max_response_bytes: "Max response bytes",
  };

  function renderIdentity() {
    const access = state.access;
    $("#identity-subject").textContent = access.principal;
    $("#identity-method").textContent = `Authentication method: ${access.auth_method}`;
    $("#scope-list").innerHTML = access.scopes.length
      ? access.scopes.map((scope) => `<span class="capability"><span>${escapeHtml(scope)}</span></span>`).join("")
      : '<span class="capability"><span>No scopes granted</span></span>';
    const capabilities = [
      ["Query visible connections", access.capabilities.query_visible_connections],
      ["Read configuration", access.capabilities.read_configuration],
      ["Change configuration", access.capabilities.change_configuration],
      ["Reload configuration", access.capabilities.reload_configuration],
    ];
    $("#capability-list").innerHTML = capabilities.map(([label, on]) => `
      <li class="capability"><span>${escapeHtml(label)}</span><span class="status-chip ${on ? "good" : "neutral"}">${on ? "Yes" : "No"}</span></li>`).join("");
    $("#session-state").className = "status-chip good";
    $("#session-state").textContent = "Connected";
    $("#session-principal").textContent = access.principal;
  }

  function renderConnectionCards() {
    const details = state.access.connection_access || [];
    const byId = new Map(details.map((item) => [item.connection, item]));
    $("#connection-cards").innerHTML = state.connections.length ? state.connections.map((connection) => {
      const detail = byId.get(connection.id);
      const rows = detail
        ? Object.entries(GUARDRAIL_LABELS).map(([field, label]) => `
            <div class="column-row"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(detail.guardrails[field])}</span></div>`).join("")
        : '<p class="empty">Guardrails unavailable.</p>';
      return `
        <article class="connection-item" data-static="true">
          <span><strong>${escapeHtml(connection.id)}</strong><small>${escapeHtml(connection.dialect)}${connection.description ? ` — ${escapeHtml(connection.description)}` : ""}</small></span>
          <div class="guardrail-grid">${rows}</div>
        </article>`;
    }).join("") : '<p class="empty">No connections are visible to this principal.</p>';
  }

  function renderFilterList() {
    const details = state.access.connection_access || [];
    const rows = [];
    details.forEach((detail) => {
      detail.mandatory_filters.forEach((filter) => {
        rows.push(`
          <div class="column-row">
            <strong>${escapeHtml(detail.connection)}.${escapeHtml(filter.table)}.${escapeHtml(filter.column)}</strong>
            <span>${filter.source === "claim" ? `from claim ${escapeHtml(filter.claim)}` : "fixed by policy"}</span>
            <span class="status-chip ${filter.ready ? "good" : "bad"}">${filter.ready ? "Ready" : "Missing claim"}</span>
          </div>`);
      });
    });
    $("#filter-list").innerHTML = rows.length ? rows.join("") : '<p class="empty">No mandatory row filters apply to your visible connections.</p>';
  }

  function renderDenials() {
    const report = state.denials;
    const note = $("#denials-note");
    if (!report) {
      note.hidden = true;
      $("#denial-list").innerHTML = '<p class="empty">Connect to load your recent denials.</p>';
      return;
    }
    if (report.source === "disabled") {
      note.hidden = true;
      $("#denial-list").innerHTML =
        '<p class="empty">Denial history is disabled — this deployment has no persisted audit sink enabled.</p>';
      return;
    }
    note.hidden = false;
    note.textContent = `${report.note} (lookback ${Math.round(report.lookback_seconds / 3600)}h; ${report.own_denials_found} of your requests were rejected${report.truncated ? ", list may be incomplete" : ""})`;
    const denials = report.denials || [];
    $("#denial-list").innerHTML = denials.length
      ? denials.map((denial) => `
          <div class="column-row">
            <strong>${escapeHtml(denial.connection)}</strong>
            <span>${escapeHtml(denial.explanation)} <small>(${escapeHtml(denial.occurred_at)} · ${escapeHtml(denial.surface)})</small></span>
            <span class="status-chip bad">${escapeHtml(denial.reason)}</span>
          </div>`).join("")
      : '<p class="empty">No recent denials.</p>';
  }

  function populateConnectionSelect() {
    const options = state.connections.map((connection) => `<option value="${escapeHtml(connection.id)}">${escapeHtml(connection.id)}</option>`).join("");
    $("#connection-select").innerHTML = `<option value="">Select a connection</option>${options}`;
  }

  function renderTableList() {
    $("#table-list").innerHTML = state.tables.length ? state.tables.map((table) => `
      <button class="connection-item ${state.selectedTable === table ? "active" : ""}" type="button" data-table-name="${escapeHtml(table)}">
        <span><strong>${escapeHtml(table)}</strong></span>
      </button>`).join("") : '<p class="empty">No policy-visible tables.</p>';
  }

  async function selectSchemaConnection(connectionId) {
    state.selectedConnection = connectionId || null;
    state.selectedTable = null;
    state.tables = [];
    $("#table-title").textContent = "Select a table";
    $("#column-list").innerHTML = "";
    if (!connectionId) { renderTableList(); return; }
    try {
      const body = await api(`/${encodeURIComponent(connectionId)}/tables`);
      state.tables = body.tables || [];
      renderTableList();
    } catch (error) {
      toast(error.message, "bad");
    }
  }

  async function selectTable(table) {
    state.selectedTable = table;
    renderTableList();
    $("#table-title").textContent = table;
    $("#column-list").innerHTML = '<p class="empty">Loading columns…</p>';
    try {
      const description = await api(`/${encodeURIComponent(state.selectedConnection)}/tables/${encodeURIComponent(table)}`);
      const columns = description.columns || [];
      $("#column-list").innerHTML = columns.length ? columns.map((column) => `
        <div class="column-row">
          <strong title="${escapeHtml(column.name)}">${escapeHtml(column.name)}</strong>
          <span title="${escapeHtml(column.type)}">${escapeHtml(column.type)}</span>
          <small>${column.nullable ? "nullable" : "required"}</small>
        </div>`).join("") : '<p class="empty">No policy-visible columns.</p>';
    } catch (error) {
      $("#column-list").innerHTML = "";
      toast(error.message, "bad");
    }
  }

  async function connect(token) {
    // The bearer token lives only in memory (state.token) for the lifetime of
    // the tab — never in any web storage — so it cannot be exfiltrated from
    // browser storage by an XSS. Closing or reloading the tab clears it and
    // requires re-authentication.
    state.token = token.trim();
    state.access = await api("/help/my-access");
    state.connections = state.access.visible_connections || [];
    setBanner("");
    renderIdentity();
    renderConnectionCards();
    renderFilterList();
    populateConnectionSelect();
    try {
      state.denials = await api("/help/my-recent-denials");
    } catch (error) {
      state.denials = null;
      toast(error.message, "bad");
    }
    renderDenials();
  }

  function disconnect() {
    state.token = "";
    state.access = null;
    state.connections = [];
    $("#session-state").className = "status-chip neutral";
    $("#session-state").textContent = "Disconnected";
    $("#session-principal").textContent = "No principal";
    $("#identity-subject").textContent = "Not connected";
    $("#identity-method").textContent = "Authentication method: —";
    $("#scope-list").innerHTML = "";
    $("#capability-list").innerHTML = "";
    $("#connection-cards").innerHTML = '<p class="empty">Connect to load your access.</p>';
    $("#filter-list").innerHTML = '<p class="empty">Connect to load your access.</p>';
    $("#connection-select").innerHTML = '<option value="">Select a connection</option>';
    $("#table-list").innerHTML = "";
    $("#column-list").innerHTML = "";
    $("#table-title").textContent = "Select a table";
    state.denials = null;
    renderDenials();
    $("#auth-token").value = "";
    $("#auth-dialog").showModal();
  }

  function bindEvents() {
    $("#disconnect-button").addEventListener("click", disconnect);
    $("#connection-select").addEventListener("change", (event) => selectSchemaConnection(event.target.value));
    $("#table-list").addEventListener("click", (event) => {
      const button = event.target.closest("[data-table-name]");
      if (button) selectTable(button.dataset.tableName);
    });
    $("#auth-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("#auth-form .button");
      const errorNode = $("#auth-error");
      errorNode.hidden = true;
      setBusy(button, true, "Connecting…");
      try {
        await connect($("#auth-token").value);
        $("#auth-dialog").close();
        toast(`Connected as ${state.access.principal}.`);
      } catch (error) {
        errorNode.textContent = error.status === 401 ? "Token rejected by QueryGate." : error.message;
        errorNode.hidden = false;
      } finally { setBusy(button, false); }
    });
  }

  async function initialize() {
    bindEvents();
    // No token is ever persisted, so there is nothing to restore — every load
    // starts with an in-memory-only authentication prompt.
    $("#auth-dialog").showModal();
  }

  initialize();
})();
