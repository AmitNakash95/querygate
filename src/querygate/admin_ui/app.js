(() => {
  "use strict";

  const API = "/api/v1";
  const viewMeta = {
    overview: ["Control plane / Overview", "Access policy at a glance"],
    connections: ["Control plane / Schema review", "Policy-filtered schema"],
    policy: ["Control plane / Policy designer", "Design and simulate access"],
    changes: ["Control plane / Change set", "Validate before activation"],
    history: ["Control plane / Versions", "Immutable configuration history"],
    audit: ["Control plane / Audit trail", "Decisions without sensitive payloads"],
  };
  const documentKeys = ["policy", "connections", "catalog"];
  const guardrailFields = {
    "guard-max-limit": "max_limit",
    "guard-max-limit-aggregate": "max_limit_aggregate",
    "guard-max-joins": "max_joins",
    "guard-max-select-columns": "max_select_columns",
    "guard-timeout-seconds": "timeout_seconds",
    "guard-max-concurrency": "max_concurrency",
    "guard-max-queue-depth": "max_queue_depth",
    "guard-max-response-bytes": "max_response_bytes",
  };
  const policyDefaults = {
    enabled: true,
    max_limit: 100,
    max_limit_aggregate: 1000,
    max_joins: 5,
    max_select_columns: 30,
    timeout_seconds: 30,
    max_concurrency: 8,
    max_queue_depth: null,
    max_response_bytes: 10000000,
  };

  const state = {
    token: "",
    access: null,
    current: null,
    versions: [],
    connections: [],
    selectedConnection: null,
    tables: [],
    selectedTable: null,
    policyDocument: null,
    activeDocuments: { policy: "", connections: "", catalog: "" },
    draftDocuments: { policy: "", connections: "", catalog: "" },
    selectedDocument: "policy",
    validatedFingerprint: null,
    auditCursor: 0,
    auditNextCursor: null,
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

  function hasScope(scope) {
    return Boolean(state.access?.scopes?.includes(scope));
  }

  function canRead() { return hasScope("admin:config:read"); }
  function canWrite() { return hasScope("admin:config:write"); }

  function showView(name) {
    if (!viewMeta[name]) return;
    $$(".view").forEach((view) => {
      const active = view.dataset.view === name;
      view.classList.toggle("active", active);
      view.hidden = !active;
    });
    $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.viewTarget === name));
    $("#view-kicker").textContent = viewMeta[name][0];
    $("#view-title").textContent = viewMeta[name][1];
    document.body.classList.remove("menu-open");
    $("#menu-button").setAttribute("aria-expanded", "false");
    window.history.replaceState({}, "", name === "overview" ? "/admin/" : `/admin/#${name}`);
    if (name === "connections" && state.connections.length && !state.selectedConnection) selectConnection(state.connections[0].id);
    if (name === "audit" && state.access && !$("#audit-list .audit-event")) loadAudit(false);
  }

  function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
  }

  function relativeDate(value) {
    if (!value) return "Never activated";
    const delta = new Date(value).getTime() - Date.now();
    const minutes = Math.round(delta / 60000);
    const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
    if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
    const hours = Math.round(minutes / 60);
    if (Math.abs(hours) < 24) return formatter.format(hours, "hour");
    return formatter.format(Math.round(hours / 24), "day");
  }

  function fingerprintDraft() {
    return documentKeys.map((key) => state.draftDocuments[key]).join("\u0000");
  }

  function documentChanged(key) {
    return state.draftDocuments[key] !== state.activeDocuments[key];
  }

  function anyDocumentChanged() {
    return documentKeys.some(documentChanged);
  }

  function syncDirtyState() {
    const dirty = anyDocumentChanged();
    $("#draft-dot").hidden = !dirty;
    documentKeys.forEach((key) => {
      $(`#${key}-change-mark`).textContent = documentChanged(key) ? "•" : "";
    });
    if (state.validatedFingerprint !== fingerprintDraft()) {
      state.validatedFingerprint = null;
      $("#validation-status").className = "status-chip neutral";
      $("#validation-status").textContent = "Not validated";
      $("#stage-draft").disabled = true;
    }
  }

  function renderCapabilities() {
    if (!state.access) return;
    const entries = [
      ["Discover visible connections", state.access.capabilities?.query_visible_connections],
      ["Inspect config versions", state.access.capabilities?.read_configuration],
      ["Change configuration", state.access.capabilities?.change_configuration],
      ["Reload IaC files", state.access.capabilities?.reload_configuration],
    ];
    $("#capability-list").innerHTML = entries.map(([label, enabled]) =>
      `<div class="capability ${enabled ? "on" : ""}"><span>${escapeHtml(label)}</span><b aria-label="${enabled ? "Available" : "Unavailable"}"></b></div>`
    ).join("");
  }

  function renderOverview(configuration = null) {
    $("#session-state").className = "status-chip good";
    $("#session-state").textContent = state.access?.auth_method || "Connected";
    $("#session-principal").textContent = state.access?.principal || "Unknown principal";
    $("#stat-connections").textContent = state.connections.length;
    $("#stat-staged").textContent = state.versions.filter((version) => version.status === "staged").length;
    $("#overview-version").textContent = state.current ? `v${state.current.id}` : "—";
    $("#overview-version-time").textContent = state.current ? `${state.current.applied_by || "system"} · ${relativeDate(state.current.applied_at)}` : "Read scope required";
    if (configuration) {
      $("#stat-overrides").textContent = Object.keys(configuration.policy?.connection_overrides || {}).length;
      $("#stat-principals").textContent = configuration.policy?.principal_override_count ?? 0;
    }
    renderCapabilities();
  }

  function populateConnectionSelects() {
    const items = state.connections.map((connection) => `<option value="${escapeHtml(connection.id)}">${escapeHtml(connection.id)}</option>`).join("");
    $("#test-connection").innerHTML = items || '<option value="">No visible connections</option>';
    const knownIds = state.current ? connectionIdsFromPolicy() : state.connections.map((item) => item.id);
    const policyOptions = knownIds.map((id) => `<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`).join("");
    $("#designer-connection").innerHTML = policyOptions || '<option value="">No connections</option>';
    $("#designer-principal-connection").innerHTML = `<option value="*">* (every connection)</option>${policyOptions}`;
  }

  function connectionIdsFromPolicy() {
    const ids = new Set(state.connections.map((item) => item.id));
    Object.keys(state.policyDocument?.connections || {}).forEach((id) => ids.add(id));
    return Array.from(ids).sort();
  }

  function renderConnectionList(filter = "") {
    const query = filter.trim().toLowerCase();
    const matches = state.connections.filter((connection) =>
      [connection.id, connection.dialect, connection.description].filter(Boolean).some((value) => String(value).toLowerCase().includes(query))
    );
    $("#connection-list").innerHTML = matches.length ? matches.map((connection) => `
      <button class="connection-item ${state.selectedConnection === connection.id ? "active" : ""}" type="button" data-connection-id="${escapeHtml(connection.id)}">
        <span><strong>${escapeHtml(connection.id)}</strong><small>${escapeHtml(connection.dialect)}</small></span><i aria-hidden="true"></i>
      </button>`).join("") : '<p class="empty-state">No matching visible connections.</p>';
  }

  async function selectConnection(connectionId) {
    const connection = state.connections.find((item) => item.id === connectionId);
    if (!connection) return;
    state.selectedConnection = connectionId;
    state.selectedTable = null;
    renderConnectionList($("#connection-search").value);
    $("#schema-empty").hidden = true;
    $("#schema-content").hidden = false;
    $("#schema-connection-title").textContent = connection.id;
    $("#schema-dialect").textContent = connection.dialect;
    $("#schema-description").textContent = connection.description || "No connection description configured.";
    $("#table-list").innerHTML = '<p class="empty-state">Loading policy-filtered tables…</p>';
    $("#column-list").innerHTML = '<p class="empty-state">Select a table to inspect its columns.</p>';
    try {
      const body = await api(`/${encodeURIComponent(connectionId)}/tables`);
      state.tables = body.tables || [];
      $("#schema-table-count").textContent = `${state.tables.length} table${state.tables.length === 1 ? "" : "s"}`;
      renderTableList();
    } catch (error) {
      state.tables = [];
      $("#schema-table-count").textContent = "Unavailable";
      $("#table-list").innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    }
  }

  function renderTableList() {
    const query = $("#table-search").value.trim().toLowerCase();
    const tables = state.tables.filter((table) => table.toLowerCase().includes(query));
    $("#table-list").innerHTML = tables.length ? tables.map((table) => `
      <button class="table-item ${state.selectedTable === table ? "active" : ""}" type="button" data-table-name="${escapeHtml(table)}">
        <span>${escapeHtml(table)}</span><small>Review →</small>
      </button>`).join("") : '<p class="empty-state">No matching policy-visible tables.</p>';
  }

  async function selectTable(table) {
    state.selectedTable = table;
    renderTableList();
    $("#table-detail-title").textContent = table;
    $("#column-list").innerHTML = '<p class="empty-state">Loading columns…</p>';
    $("#table-sensitivity").hidden = true;
    try {
      const description = await api(`/${encodeURIComponent(state.selectedConnection)}/tables/${encodeURIComponent(table)}`);
      const sensitivity = description.catalog?.sensitivity;
      if (sensitivity && sensitivity !== "none") {
        $("#table-sensitivity").hidden = false;
        $("#table-sensitivity").textContent = sensitivity;
        $("#table-sensitivity").className = sensitivity === "pii" ? "status-chip bad" : "status-chip warning";
      }
      const columns = description.columns || [];
      $("#column-list").innerHTML = columns.length ? columns.map((column) => `
        <div class="column-row">
          <strong title="${escapeHtml(column.name)}">${escapeHtml(column.name)}</strong>
          <span title="${escapeHtml(column.type)}">${escapeHtml(column.type)}</span>
          <small>${column.nullable ? "nullable" : "required"}</small>
        </div>`).join("") : '<p class="empty-state">No policy-visible columns.</p>';
    } catch (error) {
      $("#column-list").innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    }
  }

  async function parsePolicyDocument() {
    if (!state.draftDocuments.policy || (!canRead() && !canWrite())) return;
    const result = await api("/admin/ui/policy/parse", {
      method: "POST",
      body: JSON.stringify({ policy_yaml: state.draftDocuments.policy }),
    });
    state.policyDocument = result.document;
    populateConnectionSelects();
    populateDesigner();
  }

  function selectedLayer(create = false) {
    if (!state.policyDocument) return null;
    const type = $("input[name='policy-layer']:checked").value;
    if (type === "default") {
      if (create && !state.policyDocument.default) state.policyDocument.default = {};
      return state.policyDocument.default || {};
    }
    if (type === "connection") {
      const id = $("#designer-connection").value;
      if (!state.policyDocument.connections && create) state.policyDocument.connections = {};
      if (create && !state.policyDocument.connections[id]) state.policyDocument.connections[id] = {};
      return state.policyDocument.connections?.[id] || {};
    }
    const principal = $("#designer-principal").value.trim();
    const connection = $("#designer-principal-connection").value || "*";
    if (!principal) return null;
    if (!state.policyDocument.principals && create) state.policyDocument.principals = {};
    if (!state.policyDocument.principals[principal] && create) state.policyDocument.principals[principal] = {};
    if (!state.policyDocument.principals[principal][connection] && create) state.policyDocument.principals[principal][connection] = {};
    return state.policyDocument.principals?.[principal]?.[connection] || {};
  }

  function isDefaultLayer() {
    return $("input[name='policy-layer']:checked").value === "default";
  }

  function csv(values) { return Array.isArray(values) ? values.join(", ") : ""; }
  function csvValues(value) { return value.split(",").map((item) => item.trim()).filter(Boolean); }

  function populateDesigner() {
    const type = $("input[name='policy-layer']:checked").value;
    $("#designer-connection-wrap").hidden = type !== "connection";
    $("#designer-principal-wrap").hidden = type !== "principal";
    $("#designer-principal-connection-wrap").hidden = type !== "principal";
    $("#remove-policy-layer").hidden = type === "default";
    const titles = {
      default: ["Default policy", "Baseline for every connection before connection and principal overrides are merged."],
      connection: ["Connection override", "Changes only the selected connection, merged on top of the default policy."],
      principal: ["Principal override", "The most specific layer, merged for this subject and connection at request time."],
    };
    $("#layer-title").textContent = titles[type][0];
    $("#layer-description").textContent = titles[type][1];
    const layer = selectedLayer(false) || {};
    const defaultLayer = isDefaultLayer();
    const enabled = Object.prototype.hasOwnProperty.call(layer, "enabled") ? String(layer.enabled) : (defaultLayer ? "true" : "");
    $("#policy-enabled").value = enabled;
    $("#policy-allowed-tables").value = csv(layer.allowed_tables);
    $("#policy-denied-tables").value = csv(layer.denied_tables);
    const columnTables = Array.from(new Set([
      ...Object.keys(layer.allowed_columns || {}), ...Object.keys(layer.denied_columns || {}),
    ]));
    const table = columnTables[0] || "";
    $("#policy-column-table").value = table;
    $("#policy-allowed-columns").value = csv(layer.allowed_columns?.[table]);
    $("#policy-denied-columns").value = csv(layer.denied_columns?.[table]);
    Object.entries(guardrailFields).forEach(([id, field]) => {
      const value = Object.prototype.hasOwnProperty.call(layer, field) ? layer[field] : (defaultLayer ? policyDefaults[field] : "");
      $(`#${id}`).value = value ?? "";
    });
  }

  function updateDesignerLayer() {
    const layer = selectedLayer(true);
    if (!layer) throw new Error("Enter a principal subject before applying this layer.");
    const enabled = $("#policy-enabled").value;
    if (enabled === "") delete layer.enabled;
    else layer.enabled = enabled === "true";

    [["allowed_tables", "#policy-allowed-tables"], ["denied_tables", "#policy-denied-tables"]].forEach(([field, selector]) => {
      const values = csvValues($(selector).value);
      if (values.length || isDefaultLayer()) layer[field] = values;
      else delete layer[field];
    });

    const table = $("#policy-column-table").value.trim();
    if (table) {
      [["allowed_columns", "#policy-allowed-columns"], ["denied_columns", "#policy-denied-columns"]].forEach(([field, selector]) => {
        const values = csvValues($(selector).value);
        if (!layer[field]) layer[field] = {};
        if (values.length) layer[field][table] = values;
        else delete layer[field][table];
        if (!Object.keys(layer[field]).length) delete layer[field];
      });
    }

    Object.entries(guardrailFields).forEach(([id, field]) => {
      const raw = $(`#${id}`).value.trim();
      if (!raw) delete layer[field];
      else layer[field] = Number(raw);
    });
  }

  async function renderPolicyDraft(message = "Policy builder changes added to the local draft.") {
    const result = await api("/admin/ui/policy/render", {
      method: "POST",
      body: JSON.stringify({ document: state.policyDocument }),
    });
    state.draftDocuments.policy = result.policy_yaml;
    if (state.selectedDocument === "policy") {
      $("#document-editor").value = result.policy_yaml;
      renderEditor();
    }
    syncDirtyState();
    toast(message);
  }

  async function applyDesigner() {
    const button = $("#apply-designer");
    setBusy(button, true, "Applying…");
    try {
      updateDesignerLayer();
      await renderPolicyDraft();
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      setBusy(button, false);
    }
  }

  async function removePolicyLayer() {
    const type = $("input[name='policy-layer']:checked").value;
    if (type === "connection") {
      delete state.policyDocument.connections?.[$("#designer-connection").value];
    } else if (type === "principal") {
      const subject = $("#designer-principal").value.trim();
      const connection = $("#designer-principal-connection").value || "*";
      if (state.policyDocument.principals?.[subject]) {
        delete state.policyDocument.principals[subject][connection];
        if (!Object.keys(state.policyDocument.principals[subject]).length) delete state.policyDocument.principals[subject];
      }
    }
    await renderPolicyDraft("Policy override removed from the local draft.");
    populateDesigner();
  }

  async function runPolicyTest(event) {
    event.preventDefault();
    const formButton = $("#policy-test-form button[type='submit']");
    setBusy(formButton, true, "Checking…");
    try {
      let claims;
      try { claims = JSON.parse($("#test-claims").value || "{}"); }
      catch { throw new Error("Claims must be valid JSON."); }
      const body = await api("/admin/ui/policy/test", {
        method: "POST",
        body: JSON.stringify({
          principal: $("#test-principal").value,
          connection: $("#test-connection").value,
          table: $("#test-table").value || null,
          columns: csvValues($("#test-columns").value),
          claims,
        }),
      });
      const result = $("#policy-test-result");
      result.hidden = false;
      result.className = `decision-result ${body.allowed ? "allowed" : "denied"}`;
      const guardrails = ["max_limit", "timeout_seconds", "max_concurrency", "max_joins"]
        .map((key) => `<span>${escapeHtml(key)}: ${escapeHtml(body.guardrails[key])}</span>`).join("");
      result.innerHTML = `
        <div class="decision-head"><div><p class="eyebrow">Effective active policy</p><h3>${body.allowed ? "Access permitted" : "Access rejected"}</h3></div><span class="status-chip ${body.allowed ? "good" : "bad"}">${body.allowed ? "Allow" : "Deny"}</span></div>
        <ul>${body.reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>
        <div class="decision-guardrails">${guardrails}</div>`;
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      setBusy(formButton, false);
    }
  }

  function simpleLineDiff(before, after) {
    const oldLines = before.split("\n");
    const newLines = after.split("\n");
    if (oldLines.length * newLines.length > 180000) {
      return newLines.map((line, index) => ({ type: oldLines[index] === line ? "same" : "add", line, number: index + 1 }));
    }
    const table = Array.from({ length: oldLines.length + 1 }, () => new Uint16Array(newLines.length + 1));
    for (let i = oldLines.length - 1; i >= 0; i -= 1) {
      for (let j = newLines.length - 1; j >= 0; j -= 1) {
        table[i][j] = oldLines[i] === newLines[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
      }
    }
    const diff = [];
    let i = 0; let j = 0;
    while (i < oldLines.length && j < newLines.length) {
      if (oldLines[i] === newLines[j]) { diff.push({ type: "same", line: oldLines[i], number: j + 1 }); i += 1; j += 1; }
      else if (table[i + 1][j] >= table[i][j + 1]) { diff.push({ type: "remove", line: oldLines[i], number: i + 1 }); i += 1; }
      else { diff.push({ type: "add", line: newLines[j], number: j + 1 }); j += 1; }
    }
    while (i < oldLines.length) { diff.push({ type: "remove", line: oldLines[i], number: i + 1 }); i += 1; }
    while (j < newLines.length) { diff.push({ type: "add", line: newLines[j], number: j + 1 }); j += 1; }
    return diff;
  }

  function renderEditor() {
    const key = state.selectedDocument;
    const draft = state.draftDocuments[key];
    const before = state.activeDocuments[key];
    $("#editor-file-name").textContent = `${key}.yaml`;
    $("#editor-line-count").textContent = `${draft.split("\n").length} lines`;
    if ($("#document-editor").value !== draft) $("#document-editor").value = draft;
    const diff = simpleLineDiff(before, draft);
    const changed = diff.filter((item) => item.type !== "same");
    $("#diff-summary").textContent = changed.length ? `${changed.filter((item) => item.type === "add").length} added · ${changed.filter((item) => item.type === "remove").length} removed` : "Unchanged";
    $("#diff-summary").className = `status-chip ${changed.length ? "warning" : "neutral"}`;
    const visibleDiff = changed.length ? diff : [];
    $("#document-diff").innerHTML = visibleDiff.length ? visibleDiff.map((item) => `
      <div class="diff-line ${item.type}"><span class="line-no">${item.number}</span><span class="sign">${item.type === "add" ? "+" : item.type === "remove" ? "−" : ""}</span><code>${escapeHtml(item.line || " ")}</code></div>`).join("") : '<p class="empty-state">Edit this document to see a line-level preview.</p>';
  }

  function selectDocument(key) {
    state.selectedDocument = key;
    $$(".document-tab").forEach((tab) => {
      const active = tab.dataset.document === key;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    renderEditor();
  }

  function draftPayload(includeUnchanged = false) {
    const payload = {};
    documentKeys.forEach((key) => {
      if (includeUnchanged || documentChanged(key)) payload[`${key}_yaml`] = state.draftDocuments[key];
    });
    return payload;
  }

  async function validateDraft() {
    const button = $("#validate-draft");
    setBusy(button, true, "Validating…");
    try {
      const payload = draftPayload();
      const [validation, preview] = await Promise.all([
        api("/admin/config/validate", { method: "POST", body: JSON.stringify(payload) }),
        api("/admin/config/preview", { method: "POST", body: JSON.stringify(payload) }),
      ]);
      const valid = validation.valid && preview.ready_to_stage;
      $("#validation-status").className = `status-chip ${valid ? "good" : "bad"}`;
      $("#validation-status").textContent = valid ? "Valid" : "Needs changes";
      if (valid) {
        const documentSummary = preview.documents.map((item) => `${item.document}: ${item.change}`).join(" · ");
        $("#validation-result").innerHTML = `<div class="validation-ok"><span class="status-chip good">Passed</span><p>${escapeHtml(documentSummary)}</p></div>`;
        state.validatedFingerprint = fingerprintDraft();
        $("#stage-draft").disabled = !anyDocumentChanged();
      } else {
        const errors = [...(validation.errors || []), ...(preview.errors || [])];
        $("#validation-result").innerHTML = `<ul>${Array.from(new Set(errors)).map((error) => `<li>${escapeHtml(error)}</li>`).join("")}</ul>`;
        state.validatedFingerprint = null;
        $("#stage-draft").disabled = true;
      }
    } catch (error) {
      $("#validation-status").className = "status-chip bad";
      $("#validation-status").textContent = "Request failed";
      $("#validation-result").innerHTML = `<ul><li>${escapeHtml(error.message)}</li></ul>`;
    } finally {
      setBusy(button, false);
    }
  }

  async function stageDraft() {
    if (state.validatedFingerprint !== fingerprintDraft()) {
      toast("Validate the current draft before staging.", "bad");
      return;
    }
    const button = $("#stage-draft");
    setBusy(button, true, "Staging…");
    try {
      const payload = draftPayload();
      payload.description = $("#stage-description").value.trim() || null;
      const version = await api("/admin/config/versions", { method: "POST", body: JSON.stringify(payload) });
      state.versions.push(version);
      renderHistory();
      renderOverview();
      toast(`Version v${version.id} staged. It is not active yet.`);
      showView("history");
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      setBusy(button, false);
    }
  }

  function discardDraft() {
    state.draftDocuments = { ...state.activeDocuments };
    state.validatedFingerprint = null;
    $("#stage-description").value = "";
    syncDirtyState();
    renderEditor();
    parsePolicyDocument().catch((error) => toast(error.message, "bad"));
    toast("Local draft reset to the active version.");
  }

  function renderHistory() {
    const current = state.versions.find((version) => version.status === "active") || state.current;
    $("#history-current").textContent = current ? `v${current.id}` : "—";
    $("#history-total").textContent = state.versions.length;
    $("#history-staged").textContent = state.versions.filter((version) => version.status === "staged").length;
    $("#history-body").innerHTML = state.versions.length ? [...state.versions].reverse().map((version) => {
      const statusClass = version.status === "active" ? "good" : version.status === "staged" ? "warning" : "neutral";
      const actionLabel = version.status === "staged" ? "Activate" : version.status === "inactive" ? "Roll back" : "Active";
      return `<tr>
        <td>v${escapeHtml(version.id)}</td>
        <td><span class="status-chip ${statusClass}">${escapeHtml(version.status)}</span></td>
        <td>${escapeHtml(version.description || "No description")}</td>
        <td title="${escapeHtml(formatDate(version.created_at))}">${escapeHtml(relativeDate(version.created_at))}<br><small>${escapeHtml(version.created_by)}</small></td>
        <td title="${escapeHtml(formatDate(version.applied_at))}">${escapeHtml(relativeDate(version.applied_at))}<br><small>${escapeHtml(version.applied_by || "—")}</small></td>
        <td><button class="button secondary" type="button" data-review-version="${escapeHtml(version.id)}">Review</button> <button class="button ${version.status === "active" ? "secondary" : "primary"}" type="button" data-apply-version="${escapeHtml(version.id)}" ${version.status === "active" || !canWrite() ? "disabled" : ""}>${actionLabel}</button></td>
      </tr>`;
    }).join("") : '<tr><td colspan="6" class="empty-cell">No versions available.</td></tr>';
  }

  function confirmAction({ title, message, phrase }) {
    const dialog = $("#confirm-dialog");
    $("#confirm-title").textContent = title;
    $("#confirm-message").textContent = message;
    $("#confirm-phrase-wrap").hidden = !phrase;
    $("#confirm-phrase-input").value = "";
    $("#confirm-phrase-input").placeholder = phrase || "";
    dialog.showModal();
    return new Promise((resolve) => {
      const onClose = () => {
        dialog.removeEventListener("close", onClose);
        resolve(dialog.returnValue === "confirm" && (!phrase || $("#confirm-phrase-input").value.trim() === phrase));
      };
      dialog.addEventListener("close", onClose);
    });
  }

  async function applyVersion(versionId) {
    const version = state.versions.find((item) => item.id === versionId);
    if (!version || version.status === "active") return;
    const rollback = version.status === "inactive";
    const phrase = `${rollback ? "ROLLBACK" : "ACTIVATE"} v${version.id}`;
    const confirmed = await confirmAction({
      title: rollback ? `Roll back to v${version.id}?` : `Activate v${version.id}?`,
      message: `${rollback ? "This restores an earlier complete configuration snapshot." : "This reloads connections, policy, and catalog from the staged snapshot."}${anyDocumentChanged() ? " Your current local draft will be reset to the newly active version." : ""} Type ${phrase} to continue.`,
      phrase,
    });
    if (!confirmed) {
      toast(`Confirmation cancelled. Enter “${phrase}” exactly to proceed.`);
      return;
    }
    try {
      await api(`/admin/config/versions/${encodeURIComponent(versionId)}/apply`, { method: "POST" });
      toast(rollback ? `Rolled back to v${versionId}.` : `Version v${versionId} is active.`);
      await loadGovernance(false);
    } catch (error) {
      toast(error.message, "bad");
    }
  }

  function reviewVersion(versionId) {
    const version = state.versions.find((item) => item.id === versionId);
    if (!version) return;
    state.draftDocuments = {
      connections: version.connections_yaml || "",
      policy: version.policy_yaml || "",
      catalog: version.catalog_yaml || "",
    };
    state.validatedFingerprint = null;
    syncDirtyState();
    selectDocument("policy");
    parsePolicyDocument().catch((error) => toast(error.message, "bad"));
    showView("changes");
    toast(`Loaded v${versionId} into the local change set for review.`);
  }

  function auditQuery(cursor) {
    const params = new URLSearchParams({ cursor: String(cursor), limit: "50" });
    const fields = {
      event_type: $("#audit-event-type").value,
      outcome: $("#audit-outcome").value,
      principal_id: $("#audit-principal").value.trim(),
      connection_id: $("#audit-connection").value.trim(),
    };
    Object.entries(fields).forEach(([key, value]) => { if (value) params.set(key, value); });
    return `/admin/ui/audit/events?${params}`;
  }

  function renderAuditEvent(event) {
    const kind = event.event_type === "query.execution" ? "Query execution" : event.event_type === "config.governance" ? `Config ${event.action}` : `Catalog ${event.action}`;
    const subject = event.principal_id || "system";
    const context = event.connection_id || event.version_id || event.operation || "—";
    const details = event.error_category ? `Error: ${event.error_category}` : event.query_shape?.from ? `Table: ${event.query_shape.from}` : `Surface: ${event.surface || "internal"}`;
    return `<article class="audit-event">
      <div><time>${escapeHtml(formatDate(event.occurred_at))}</time><span class="event-id" title="${escapeHtml(event.event_id)}">${escapeHtml(event.event_id)}</span></div>
      <div><strong>${escapeHtml(kind)}</strong><p>${escapeHtml(subject)}</p></div>
      <div><strong>${escapeHtml(context)}</strong><p>${escapeHtml(details)}</p></div>
      <span class="status-chip ${event.outcome === "success" ? "good" : "bad"}">${escapeHtml(event.outcome)}</span>
    </article>`;
  }

  async function loadAudit(append = false) {
    const button = append ? $("#audit-more") : $("#refresh-audit");
    setBusy(button, true, "Loading…");
    try {
      const cursor = append ? state.auditNextCursor || 0 : 0;
      const page = await api(auditQuery(cursor));
      state.auditCursor = cursor;
      state.auditNextCursor = page.next_cursor;
      const list = $("#audit-list");
      if (!append) list.innerHTML = "";
      if (page.events.length) list.insertAdjacentHTML("beforeend", page.events.map(renderAuditEvent).join(""));
      if (!list.children.length) list.innerHTML = `<p class="empty-state">${page.source === "disabled" ? "The persisted JSONL audit sink is disabled for this deployment." : "No matching events."}</p>`;
      $("#audit-count").textContent = `${page.total} matching event${page.total === 1 ? "" : "s"}`;
      $("#audit-source").textContent = page.malformed ? `${page.malformed} malformed lines skipped` : `Source: ${page.source}`;
      $("#audit-more").hidden = page.next_cursor === null;
    } catch (error) {
      $("#audit-list").innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    } finally {
      setBusy(button, false);
    }
  }

  async function loadGovernance(preserveDraft = true) {
    if (!canRead()) return;
    const [current, versions, configuration] = await Promise.all([
      api("/admin/config/current"),
      api("/admin/config/versions"),
      api("/help/configuration"),
    ]);
    state.current = current;
    state.versions = versions;
    state.activeDocuments = {
      connections: current.connections_yaml || "",
      policy: current.policy_yaml || "",
      catalog: current.catalog_yaml || "",
    };
    if (!preserveDraft || !anyDocumentChanged()) state.draftDocuments = { ...state.activeDocuments };
    renderOverview(configuration);
    renderHistory();
    syncDirtyState();
    renderEditor();
    if (canWrite() || canRead()) await parsePolicyDocument();
  }

  async function connect(token, remember = false) {
    state.token = token.trim();
    if (remember && state.token) sessionStorage.setItem("querygate_admin_token", state.token);
    else sessionStorage.removeItem("querygate_admin_token");
    state.access = await api("/help/my-access");
    state.connections = state.access.visible_connections || [];
    setBanner("");
    renderConnectionList();
    populateConnectionSelects();
    renderOverview();
    if (canRead()) {
      await loadGovernance(false);
    } else {
      setBanner("This principal can use QueryGate, but the admin UI needs admin:config:read to inspect governance state. Add admin:config:write for editing and activation.");
      disableWriteActions();
    }
    if (!canWrite()) disableWriteActions();
  }

  function disableWriteActions() {
    ["#apply-designer", "#remove-policy-layer", "#validate-draft", "#stage-draft", "#discard-draft"].forEach((selector) => {
      const element = $(selector);
      if (element) element.disabled = true;
    });
    $("#document-editor").readOnly = true;
  }

  function disconnect() {
    sessionStorage.removeItem("querygate_admin_token");
    state.token = "";
    state.access = null;
    state.current = null;
    $("#session-state").className = "status-chip neutral";
    $("#session-state").textContent = "Disconnected";
    $("#session-principal").textContent = "No principal";
    $("#auth-token").value = "";
    $("#auth-dialog").showModal();
  }

  function bindEvents() {
    $$(".nav-item").forEach((item) => item.addEventListener("click", () => showView(item.dataset.viewTarget)));
    $$('[data-go-view]').forEach((button) => button.addEventListener("click", () => showView(button.dataset.goView)));
    $("#menu-button").addEventListener("click", () => {
      const open = document.body.classList.toggle("menu-open");
      $("#menu-button").setAttribute("aria-expanded", String(open));
    });
    $("#disconnect-button").addEventListener("click", disconnect);
    $("#connection-search").addEventListener("input", (event) => renderConnectionList(event.target.value));
    $("#connection-list").addEventListener("click", (event) => {
      const button = event.target.closest("[data-connection-id]");
      if (button) selectConnection(button.dataset.connectionId);
    });
    $("#table-search").addEventListener("input", renderTableList);
    $("#table-list").addEventListener("click", (event) => {
      const button = event.target.closest("[data-table-name]");
      if (button) selectTable(button.dataset.tableName);
    });
    $("#refresh-connections").addEventListener("click", async () => {
      try {
        state.connections = await api("/connections");
        renderConnectionList();
        populateConnectionSelects();
        if (state.selectedConnection) await selectConnection(state.selectedConnection);
        toast("Visible connection schema refreshed.");
      } catch (error) { toast(error.message, "bad"); }
    });
    $$("input[name='policy-layer']").forEach((input) => input.addEventListener("change", populateDesigner));
    ["#designer-connection", "#designer-principal-connection"].forEach((selector) => $(selector).addEventListener("change", populateDesigner));
    $("#designer-principal").addEventListener("change", populateDesigner);
    $("#policy-column-table").addEventListener("change", () => {
      const layer = selectedLayer(false) || {};
      const table = $("#policy-column-table").value.trim();
      $("#policy-allowed-columns").value = csv(layer.allowed_columns?.[table]);
      $("#policy-denied-columns").value = csv(layer.denied_columns?.[table]);
    });
    $("#apply-designer").addEventListener("click", applyDesigner);
    $("#reset-designer").addEventListener("click", populateDesigner);
    $("#remove-policy-layer").addEventListener("click", () => removePolicyLayer().catch((error) => toast(error.message, "bad")));
    $("#policy-test-form").addEventListener("submit", runPolicyTest);
    $$(".document-tab").forEach((tab) => tab.addEventListener("click", () => selectDocument(tab.dataset.document)));
    $("#document-editor").addEventListener("input", (event) => {
      state.draftDocuments[state.selectedDocument] = event.target.value;
      syncDirtyState();
      renderEditor();
    });
    $("#document-editor").addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      event.preventDefault();
      const editor = event.target;
      const start = editor.selectionStart;
      editor.setRangeText("  ", start, editor.selectionEnd, "end");
      editor.dispatchEvent(new Event("input", { bubbles: true }));
    });
    $("#copy-document").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(state.draftDocuments[state.selectedDocument]); toast("Document copied."); }
      catch { toast("Clipboard access is unavailable in this browser.", "bad"); }
    });
    $("#validate-draft").addEventListener("click", validateDraft);
    $("#stage-draft").addEventListener("click", stageDraft);
    $("#discard-draft").addEventListener("click", discardDraft);
    $("#refresh-history").addEventListener("click", () => loadGovernance(true).then(() => toast("Version history refreshed.")).catch((error) => toast(error.message, "bad")));
    $("#history-body").addEventListener("click", (event) => {
      const review = event.target.closest("[data-review-version]");
      const apply = event.target.closest("[data-apply-version]");
      if (review) reviewVersion(review.dataset.reviewVersion);
      if (apply) applyVersion(apply.dataset.applyVersion);
    });
    $("#audit-filters").addEventListener("submit", (event) => { event.preventDefault(); loadAudit(false); });
    $("#refresh-audit").addEventListener("click", () => loadAudit(false));
    $("#audit-more").addEventListener("click", () => loadAudit(true));
    $("#confirm-cancel").addEventListener("click", () => { $("#confirm-dialog").returnValue = "cancel"; });
    $("#confirm-accept").addEventListener("click", (event) => {
      const phrase = $("#confirm-phrase-input").placeholder;
      if (phrase && $("#confirm-phrase-input").value.trim() !== phrase) {
        event.preventDefault();
        toast(`Type “${phrase}” exactly to continue.`, "bad");
      }
    });
    $("#auth-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("#auth-form .button");
      const errorNode = $("#auth-error");
      errorNode.hidden = true;
      setBusy(button, true, "Connecting…");
      try {
        await connect($("#auth-token").value, $("#remember-token").checked);
        $("#auth-dialog").close();
        toast(`Connected as ${state.access.principal}.`);
      } catch (error) {
        errorNode.textContent = error.status === 401 ? "Token rejected by QueryGate." : error.message;
        errorNode.hidden = false;
      } finally { setBusy(button, false); }
    });
    window.addEventListener("beforeunload", (event) => {
      if (!anyDocumentChanged()) return;
      event.preventDefault();
      event.returnValue = "";
    });
  }

  async function initialize() {
    bindEvents();
    const initialView = window.location.hash.slice(1);
    showView(viewMeta[initialView] ? initialView : "overview");
    selectDocument("policy");
    const savedToken = sessionStorage.getItem("querygate_admin_token") || "";
    if (savedToken) {
      $("#auth-token").value = savedToken;
      $("#remember-token").checked = true;
      try {
        await connect(savedToken, true);
        toast(`Restored session for ${state.access.principal}.`);
        return;
      } catch {
        sessionStorage.removeItem("querygate_admin_token");
        state.token = "";
        $("#auth-error").textContent = "The saved token is no longer valid.";
        $("#auth-error").hidden = false;
      }
    }
    $("#auth-dialog").showModal();
  }

  initialize();
})();
