(() => {
  "use strict";

  const API = "/api/v1";
  const viewMeta = {
    overview: ["Control plane / Overview", "Access policy at a glance"],
    connections: ["Control plane / Connections / Schema review", "Policy-filtered schema"],
    health: ["Control plane / Connections / Connection health", "Reachability, credential-free"],
    policy: ["Control plane / Policy / Designer", "Design and simulate access"],
    curate: ["Control plane / Catalog / Curate", "Author curated entries through the governance queue"],
    catalog: ["Control plane / Catalog / Review proposals", "Review, approve, and publish schema-catalog proposals"],
    "catalog-versions": ["Control plane / Catalog / Versions & rollback", "Every publish and rollback, connection-scoped"],
    templates: ["Control plane / Templates / Query templates", "Named, parameterized queries"],
    changes: ["Control plane / Releases / Change set", "Validate before activation"],
    history: ["Control plane / Releases / Versions", "Immutable configuration history"],
    observability: ["Control plane / Observability", "Rejection and capacity trends"],
    audit: ["Control plane / Audit trail", "Decisions without sensitive payloads"],
  };

  // Two-level navigation: six domains, each grouping one or more inner tabs.
  // A tab's `section` is the `.view` it activates (defaults to its own `view`
  // key); `panel` optionally selects a sub-panel within that section. `release`
  // records how a domain's edits reach activation — `self` = catalog's own
  // governance (self-contained), `shared` = feeds the bundled ConfigVersionStore
  // release, `bundle` = the shared atomic-version surface itself.
  const navModel = [
    { id: "overview", label: "Overview", glyph: "01", release: null, tabs: [
      { view: "overview", label: "Overview" },
    ] },
    { id: "connections", label: "Connections", glyph: "02", release: "shared", tabs: [
      { view: "connections", label: "Schema review" },
      { view: "health", label: "Connection health" },
    ] },
    { id: "policy", label: "Policy", glyph: "03", release: "shared", tabs: [
      { view: "policy", label: "Designer" },
    ] },
    { id: "catalog", label: "Catalog", glyph: "04", release: "self", tabs: [
      { view: "curate", label: "Curate", scope: "catalog:author" },
      { view: "catalog", label: "Review proposals", section: "catalog", panel: "review" },
      { view: "catalog-versions", label: "Versions & rollback", section: "catalog", panel: "versions" },
    ] },
    { id: "templates", label: "Templates", glyph: "05", release: "shared", tabs: [
      { view: "templates", label: "Query templates" },
    ] },
    { id: "releases", label: "Releases", glyph: "06", release: "bundle", tabs: [
      { view: "changes", label: "Change set" },
      { view: "history", label: "Versions" },
    ] },
    { id: "observability", label: "Observability", glyph: "07", release: null, tabs: [
      { view: "observability", label: "Observability", scope: "admin:observability:read" },
    ] },
    { id: "audit", label: "Audit", glyph: "08", release: null, tabs: [
      { view: "audit", label: "Audit trail" },
    ] },
  ];

  const releaseNotes = {
    self: "Self-contained governance — proposals publish independently, not through the shared release.",
    shared: "Edits here stage into the shared release — activate them in Releases.",
    bundle: "Shared atomic version — policy, connections, catalog.yaml and templates apply together.",
  };

  // Reverse lookups from a tab's view key to its tab descriptor and owning domain.
  const viewToTab = {};
  const viewToDomain = {};
  for (const domain of navModel) {
    for (const tab of domain.tabs) {
      if (!tab.section) tab.section = tab.view;
      viewToTab[tab.view] = tab;
      viewToDomain[tab.view] = domain;
    }
  }
  const defaultDomainView = (domain) =>
    (domain.tabs.find((tab) => !tab.scope || hasScope(tab.scope)) || domain.tabs[0]).view;
  const documentKeys = ["policy", "connections", "catalog", "templates"];
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
    activeDocuments: { policy: "", connections: "", catalog: "", templates: "" },
    draftDocuments: { policy: "", connections: "", catalog: "", templates: "" },
    selectedDocument: "policy",
    validatedFingerprint: null,
    auditCursor: 0,
    auditNextCursor: null,
    catalogProposals: [],
    catalogVersions: [],
    selectedProposalId: null,
    currentProposal: null,
    publishedComparison: null,
    connectionHealth: [],
    templates: [],
    templatePreview: null,
    templateList: [],
    observability: null,
    anomalies: null,
    templateSources: {},
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

  function renderDomainTabs(domain, activeView) {
    const bar = $("#domain-tabs");
    const visibleTabs = domain.tabs.filter((tab) => !tab.scope || hasScope(tab.scope));
    if (visibleTabs.length <= 1) {
      bar.hidden = true;
      bar.innerHTML = "";
    } else {
      bar.hidden = false;
      bar.innerHTML = visibleTabs.map((tab) =>
        `<button class="domain-tab${tab.view === activeView ? " active" : ""}" type="button" role="tab" aria-selected="${tab.view === activeView}" data-view-target="${escapeHtml(tab.view)}">${escapeHtml(tab.label)}</button>`
      ).join("");
    }
    const note = $("#domain-release-note");
    const message = domain.release ? releaseNotes[domain.release] : "";
    note.textContent = message;
    note.hidden = !message;
    note.className = `domain-release ${domain.release || ""}`.trim();
  }

  function showView(name) {
    const tab = viewToTab[name];
    if (!tab) return;
    const domain = viewToDomain[name];
    $$(".view").forEach((view) => {
      const active = view.dataset.view === tab.section;
      view.classList.toggle("active", active);
      view.hidden = !active;
    });
    if (tab.panel) {
      $$(`#view-${tab.section} [data-catalog-panel]`).forEach((panel) => {
        panel.hidden = panel.dataset.catalogPanel !== tab.panel;
      });
    }
    $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.domain === domain.id));
    renderDomainTabs(domain, name);
    $("#view-kicker").textContent = viewMeta[name][0];
    $("#view-title").textContent = viewMeta[name][1];
    document.body.classList.remove("menu-open");
    $("#menu-button").setAttribute("aria-expanded", "false");
    window.history.replaceState({}, "", name === "overview" ? "/admin/" : `/admin/#${name}`);
    if (name === "connections" && state.connections.length && !state.selectedConnection) selectConnection(state.connections[0].id);
    if (name === "audit" && state.access && !$("#audit-list .audit-event")) loadAudit(false);
    if (name === "catalog" && state.access && $("#catalog-connection").value && !state.catalogProposals.length && !state.selectedProposalId) {
      loadCatalogProposals();
      loadCatalogVersions();
    }
    if (name === "catalog-versions" && state.access && $("#catalog-connection").value && !state.catalogVersions.length) loadCatalogVersions();
    if (name === "curate" && state.access) initCurateView();
    if (name === "health" && state.access && !$("#health-body [data-health-row]")) loadConnectionHealth();
    if (name === "templates" && state.access) {
      initTemplateAuthorView();
      if (!state.templateList.length) loadTemplates();
    }
    if (name === "observability" && state.access && !state.observability) loadObservability();
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
    savePolicyDraftLocally();
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
      const candidate = canRead() && canWrite();
      const target = {
        principal: $("#test-principal").value,
        connection: $("#test-connection").value,
        table: $("#test-table").value || null,
        columns: csvValues($("#test-columns").value),
        claims,
      };
      const body = await api(candidate ? "/admin/config/simulate" : "/admin/ui/policy/test", {
        method: "POST",
        body: JSON.stringify(candidate ? { ...draftPayload(), ...target } : target),
      });
      const result = $("#policy-test-result");
      const allowed = candidate ? body.decision === "allow" : body.allowed;
      const reasons = body.reasons.map((reason) => typeof reason === "string" ? reason : reason.message);
      result.hidden = false;
      result.className = `decision-result ${allowed ? "allowed" : "denied"}`;
      const effectiveGuardrails = body.guardrails || {};
      const guardrails = ["max_limit", "timeout_seconds", "max_concurrency", "max_joins"]
        .filter((key) => effectiveGuardrails[key] !== undefined)
        .map((key) => `<span>${escapeHtml(key)}: ${escapeHtml(effectiveGuardrails[key])}</span>`).join("");
      result.innerHTML = `
        <div class="decision-head"><div><p class="eyebrow">Effective ${candidate ? "candidate" : "active"} policy</p><h3>${allowed ? "Access permitted" : "Access rejected"}</h3></div><span class="status-chip ${allowed ? "good" : "bad"}">${allowed ? "Allow" : "Deny"}</span></div>
        <ul>${reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>
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
      const errors = Array.from(
        new Set([...(validation.errors || []), ...(preview.errors || [])])
      );
      const passed = validation.valid && (preview.errors || []).length === 0;
      const valid = passed && preview.ready_to_stage;
      const scopeNote = "Structural, slot, and policy checks. Column/table existence is verified against the live database separately.";
      if (valid) {
        $("#validation-status").className = "status-chip good";
        $("#validation-status").textContent = "Valid";
        const documentSummary = preview.documents.map((item) => `${item.document}: ${item.change}`).join(" · ");
        $("#validation-result").innerHTML = `<div class="validation-ok"><span class="status-chip good">Passed</span><p>${escapeHtml(documentSummary)}</p><p class="validation-note">${escapeHtml(scopeNote)}</p></div>`;
        state.validatedFingerprint = fingerprintDraft();
        $("#stage-draft").disabled = !anyDocumentChanged();
      } else if (passed && errors.length === 0) {
        // Checks passed but the draft is identical to the active configuration,
        // so there is nothing to stage — a distinct outcome from failed checks.
        $("#validation-status").className = "status-chip warning";
        $("#validation-status").textContent = "No changes";
        $("#validation-result").innerHTML = `<div class="validation-ok"><span class="status-chip warning">Nothing to stage</span><p>The draft passed all checks but is identical to the active configuration. Edit a document before staging.</p><p class="validation-note">${escapeHtml(scopeNote)}</p></div>`;
        state.validatedFingerprint = null;
        $("#stage-draft").disabled = true;
      } else {
        $("#validation-status").className = "status-chip bad";
        $("#validation-status").textContent = "Needs changes";
        const items = errors.length
          ? errors.map((error) => `<li>${escapeHtml(error)}</li>`).join("")
          : "<li>Validation failed but no detail was returned. Check the connection, policy, catalog, and template documents, then retry.</li>";
        $("#validation-result").innerHTML = `<ul>${items}</ul><p class="validation-note">${escapeHtml(scopeNote)}</p>`;
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

  const SCHEMA_STATUS_CLASS = {
    ok: "good",
    issues: "bad",
    connection_unavailable: "warning",
    unreachable: "warning",
    structural_error: "bad",
  };

  function renderSchemaCheck(result) {
    const el = $("#schema-check-result");
    if (!result.checked) {
      el.innerHTML = `<p class="empty-state">${escapeHtml(result.note || "Nothing to check.")}</p>`;
      return;
    }
    if (!result.results.length) {
      el.innerHTML = `<p class="empty-state">No query templates to check.</p>`;
      return;
    }
    el.innerHTML = result.results
      .map((row) => {
        const cls = SCHEMA_STATUS_CLASS[row.status] || "neutral";
        const messages = row.messages.length
          ? `<ul>${row.messages.map((m) => `<li>${escapeHtml(m)}</li>`).join("")}</ul>`
          : "";
        return `<div class="schema-check-row"><span class="status-chip ${cls}">${escapeHtml(row.status.replace(/_/g, " "))}</span> <code>${escapeHtml(row.template_id)}</code> <small>${escapeHtml(row.connection)}</small>${messages}</div>`;
      })
      .join("");
  }

  async function checkSchema() {
    const button = $("#check-schema");
    setBusy(button, true, "Checking…");
    try {
      const payload = { templates_yaml: state.draftDocuments.templates || null };
      const result = await api("/admin/config/check-template-schema", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      renderSchemaCheck(result);
    } catch (error) {
      $("#schema-check-result").innerHTML = `<ul><li>${escapeHtml(error.message)}</li></ul>`;
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
      clearLocalPolicyDraft();
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
    clearLocalPolicyDraft();
    syncDirtyState();
    renderEditor();
    parsePolicyDocument().catch((error) => toast(error.message, "bad"));
    toast("Local draft reset to the active version.");
  }

  // --- Portable change-set bundles (item 47) --------------------------------

  function downloadJson(filename, data) {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function renderChangeSetResult(html) {
    $("#change-set-result").innerHTML = html;
  }

  async function exportChangeSet() {
    if (!canWrite()) { toast("Config-write scope is required to export a change set.", "bad"); return; }
    const payload = draftPayload();
    if (!Object.keys(payload).length) {
      toast("Edit a document before exporting a change set.", "bad");
      return;
    }
    const button = $("#export-change-set");
    setBusy(button, true, "Exporting…");
    try {
      payload.description = $("#stage-description").value.trim() || null;
      const bundle = await api("/admin/config/export", { method: "POST", body: JSON.stringify(payload) });
      const stamp = new Date().toISOString().replace(/[:.]/g, "-");
      downloadJson(`querygate-change-set-v${bundle.base_version_id || "0"}-${stamp}.json`, bundle);
      const docs = Object.keys(bundle.documents || {}).join(", ") || "none";
      const warning = bundle.documents && bundle.documents.connections
        ? '<p class="validation-note">This bundle includes connections.yaml, which may contain a literal credential. Store the downloaded file securely.</p>'
        : "";
      renderChangeSetResult(`<div class="validation-ok"><span class="status-chip good">Exported</span><p>Documents: ${escapeHtml(docs)} · base v${escapeHtml(String(bundle.base_version_id || "?"))}</p>${warning}</div>`);
      toast("Change set downloaded.");
    } catch (error) {
      renderChangeSetResult(`<ul><li>${escapeHtml(error.message)}</li></ul>`);
      toast(error.message, "bad");
    } finally {
      setBusy(button, false);
    }
  }

  async function importChangeSet(file) {
    if (!canWrite()) { toast("Config-write scope is required to import a change set.", "bad"); return; }
    let bundle;
    try {
      bundle = JSON.parse(await file.text());
    } catch {
      renderChangeSetResult('<ul><li>The selected file is not valid JSON.</li></ul>');
      return;
    }
    if (!bundle || bundle.bundle_format !== "querygate.config-change-set/1" || typeof bundle.documents !== "object") {
      renderChangeSetResult('<ul><li>The selected file is not a QueryGate change set.</li></ul>');
      return;
    }
    const button = $("#import-change-set");
    setBusy(button, true, "Importing…");
    try {
      const check = await api("/admin/config/import", { method: "POST", body: JSON.stringify(bundle) });
      if (!check.valid) {
        const items = (check.errors || []).map((error) => `<li>${escapeHtml(error)}</li>`).join("") || "<li>The change set failed validation.</li>";
        renderChangeSetResult(`<ul>${items}</ul>`);
        toast("Change set rejected — see details.", "bad");
        return;
      }
      // Populate the editors from the bundle the browser already holds (the
      // server response is content-free). Only documents present in the bundle
      // are applied; everything else stays at the active version.
      documentKeys.forEach((key) => {
        if (bundle.documents[key] !== undefined) state.draftDocuments[key] = bundle.documents[key];
      });
      state.validatedFingerprint = null;
      savePolicyDraftLocally();
      syncDirtyState();
      renderEditor();
      await parsePolicyDocument().catch((error) => toast(error.message, "bad"));
      const signal = (check.documents || []).map((d) => `${d.document}: ${d.change}`).join(" · ");
      const warnings = (check.warnings || []).map((w) => `<li>${escapeHtml(w)}</li>`).join("");
      const staleChip = check.stale_base
        ? '<span class="status-chip warning">Base changed</span>'
        : '<span class="status-chip good">Base current</span>';
      renderChangeSetResult(`<div class="validation-ok">${staleChip}<p>${escapeHtml(signal)}</p>${warnings ? `<ul>${warnings}</ul>` : ""}<p class="validation-note">The documents were loaded into the draft editors. Validate, then stage as usual.</p></div>`);
      toast(check.stale_base ? "Imported — the active version has changed; review before staging." : "Change set imported into the draft.");
    } catch (error) {
      renderChangeSetResult(`<ul><li>${escapeHtml(error.message)}</li></ul>`);
      toast(error.message, "bad");
    } finally {
      setBusy(button, false);
    }
  }

  // --- Tab-scoped local recovery — POLICY DRAFT ONLY (item 47) ---------------
  // Only the policy document is ever written to browser storage. connections.yaml
  // (which can hold a literal credential), secret references, and bearer tokens
  // are never persisted here — full-config recovery uses the exported file above.
  const LOCAL_POLICY_KEY = "querygate.admin.policyDraft";

  function savePolicyDraftLocally() {
    try {
      if (state.draftDocuments.policy && documentChanged("policy")) {
        window.localStorage.setItem(
          LOCAL_POLICY_KEY,
          JSON.stringify({ policy: state.draftDocuments.policy, savedAt: new Date().toISOString() })
        );
      } else {
        window.localStorage.removeItem(LOCAL_POLICY_KEY);
      }
    } catch { /* storage unavailable/full — recovery is best-effort */ }
  }

  function clearLocalPolicyDraft() {
    try { window.localStorage.removeItem(LOCAL_POLICY_KEY); } catch { /* ignore */ }
  }

  function recoverPolicyDraftLocally() {
    let saved;
    try {
      const raw = window.localStorage.getItem(LOCAL_POLICY_KEY);
      if (!raw) return;
      saved = JSON.parse(raw);
    } catch { return; }
    if (!saved || typeof saved.policy !== "string") { clearLocalPolicyDraft(); return; }
    // Only restore when the saved draft is a real, un-staged change and the
    // in-memory draft isn't already dirty (don't clobber active edits).
    if (saved.policy === state.activeDocuments.policy || anyDocumentChanged()) return;
    state.draftDocuments.policy = saved.policy;
    state.validatedFingerprint = null;
    syncDirtyState();
    renderEditor();
    parsePolicyDocument().catch(() => {});
    toast("Recovered an unsaved policy draft from this browser.");
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
      message: `${rollback ? "This restores an earlier complete configuration snapshot." : "This reloads connections, policy, catalog, and query templates from the staged snapshot."}${anyDocumentChanged() ? " Your current local draft will be reset to the newly active version." : ""} Type ${phrase} to continue.`,
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
      templates: version.templates_yaml || "",
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
    const kind = event.event_type === "query.execution" ? "Query execution"
      : event.event_type === "config.governance" ? `Config ${event.action}`
      : event.event_type === "catalog.governance" ? `Catalog ${event.action}`
      : event.event_type === "connection.probe" ? "Connection probe"
      : event.event_type;
    const subject = event.principal_id || "system";
    const context = event.connection_id || event.version_id || event.operation || "—";
    const details = event.error_category ? `Error: ${event.error_category}`
      : event.query_shape?.from ? `Table: ${event.query_shape.from}`
      : event.event_type === "connection.probe" && typeof event.probe_healthy === "boolean" ? `Probe: ${event.probe_healthy ? "healthy" : "degraded"}`
      : `Surface: ${event.surface || "internal"}`;
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

  function selectedProposal() {
    return state.currentProposal;
  }

  function proposalTargetLabel(target) {
    if (target.object_type === "table") return target.table;
    if (target.object_type === "column") return `${target.table}.${target.column}`;
    return `${target.table}.${target.column} → ${target.to_table}.${target.to_column}`;
  }

  function populateCatalogConnectionOptions() {
    const select = $("#catalog-connection");
    const previous = select.value;
    const items = state.connections.map((c) => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.id)}</option>`).join("");
    select.innerHTML = items || '<option value="">No visible connections</option>';
    if (previous && state.connections.some((c) => c.id === previous)) select.value = previous;
  }

  function applyCatalogScopeGating() {
    $("#save-proposal-edit").disabled = !hasScope("catalog:edit");
    $("#approve-proposal").disabled = !hasScope("catalog:approve");
    $("#reject-proposal").disabled = !hasScope("catalog:reject");
    $("#publish-proposal").disabled = !hasScope("catalog:publish");
  }

  function resetProposalSelection() {
    state.selectedProposalId = null;
    state.currentProposal = null;
    state.publishedComparison = null;
    $("#proposal-empty").hidden = false;
    $("#proposal-content").hidden = true;
  }

  async function loadCatalogProposals() {
    // Deliberately does not reset the open detail panel — the currently
    // selected proposal may no longer match the status filter after a
    // mutation (e.g. it moves from pending to approved) without that being
    // a reason to close the panel out from under the reviewer mid-review.
    // Callers that want a reset (switching connection/filter) call
    // resetProposalSelection() themselves first.
    const connection = $("#catalog-connection").value;
    if (!connection) {
      state.catalogProposals = [];
      $("#proposal-list").innerHTML = '<p class="empty-state">Select a connection to load proposals.</p>';
      return;
    }
    if (!hasScope("catalog:review")) {
      state.catalogProposals = [];
      $("#proposal-list").innerHTML = '<p class="empty-state">Connect with catalog:review to load proposals.</p>';
      return;
    }
    const status = $("#catalog-status-filter").value;
    const params = new URLSearchParams();
    if (status) params.set("review_status", status);
    try {
      state.catalogProposals = await api(`/admin/catalog/${encodeURIComponent(connection)}/proposals?${params}`);
      renderProposalList();
    } catch (error) {
      state.catalogProposals = [];
      $("#proposal-list").innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    }
  }

  function renderProposalList() {
    const source = $("#catalog-source-filter").value;
    const objectType = $("#catalog-object-filter").value;
    const items = state.catalogProposals.filter((proposal) =>
      (!source || proposal.source_class === source) &&
      (!objectType || proposal.target.object_type === objectType)
    );
    $("#catalog-pending-dot").hidden = !state.catalogProposals.some((p) => p.review_status === "pending");
    $("#proposal-list").innerHTML = items.length ? items.map((proposal) => `
      <button class="proposal-item ${state.selectedProposalId === proposal.proposal_id ? "active" : ""}" type="button" data-proposal-id="${escapeHtml(proposal.proposal_id)}">
        <div class="proposal-item-head"><strong title="${escapeHtml(proposalTargetLabel(proposal.target))}">${escapeHtml(proposalTargetLabel(proposal.target))}</strong><span class="status-chip ${proposal.review_status === "published" ? "good" : proposal.review_status === "rejected" ? "bad" : "neutral"}">${escapeHtml(proposal.review_status)}</span></div>
        <div class="proposal-item-meta"><span>${escapeHtml(proposal.target.object_type)}</span><span>${escapeHtml(proposal.source_class)}</span><span>${escapeHtml(proposal.schema_status)}</span></div>
      </button>`).join("") : '<p class="empty-state">No matching proposals.</p>';
  }

  function fieldsMarkup(content) {
    const rows = [
      ["Description", content?.description],
      ["Aliases", content?.aliases && content.aliases.length ? content.aliases.join(", ") : null],
      ["Default aggregation", content?.default_aggregation],
    ];
    return rows.map(([label, value]) =>
      `<dt>${escapeHtml(label)}</dt><dd class="${value ? "" : "empty"}">${value ? escapeHtml(value) : "Not set"}</dd>`
    ).join("");
  }

  async function loadPublishedComparison(proposal) {
    const dl = $("#published-fields");
    dl.innerHTML = '<p class="empty-state">Loading published catalog content…</p>';
    try {
      const description = await api(
        `/${encodeURIComponent(proposal.target.connection_id)}/tables/${encodeURIComponent(proposal.target.table)}`
      );
      let published = null;
      if (proposal.target.object_type === "table") {
        published = description.catalog;
      } else if (proposal.target.object_type === "column") {
        const column = (description.columns || []).find((c) => c.name.toLowerCase() === proposal.target.column.toLowerCase());
        published = column?.catalog || null;
      } else {
        const relationship = (description.catalog?.relationships || []).find((r) =>
          r.column.toLowerCase() === proposal.target.column.toLowerCase() &&
          r.to_table.toLowerCase() === proposal.target.to_table.toLowerCase() &&
          r.to_column.toLowerCase() === proposal.target.to_column.toLowerCase()
        );
        published = relationship ? { description: relationship.description, aliases: [], default_aggregation: null } : null;
      }
      state.publishedComparison = published;
      dl.innerHTML = published ? fieldsMarkup(published) : fieldsMarkup(null);
      if (!published) dl.insertAdjacentHTML("afterbegin", '<p class="empty-state">Nothing published for this target yet — publishing will create a new entry.</p>');
    } catch (error) {
      dl.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    }
  }

  function renderProposalDetail(proposal) {
    // Renders from a proposal object directly rather than looking it up in
    // state.catalogProposals — a proposal legitimately stops matching the
    // current status/source/object-type filter the moment its own review
    // action changes it (e.g. approve moves it out of a "pending" filter),
    // and that must not close the panel the reviewer is actively using.
    state.currentProposal = proposal;
    state.selectedProposalId = proposal.proposal_id;
    $("#proposal-empty").hidden = true;
    $("#proposal-content").hidden = false;
    $("#proposal-kicker").textContent = `${proposal.target.connection_id} / ${proposal.target.object_type}`;
    $("#proposal-title").textContent = proposalTargetLabel(proposal.target);
    $("#proposal-source-chip").textContent = proposal.source_class;
    $("#proposal-freshness-chip").textContent = proposal.schema_status;
    $("#proposal-freshness-chip").className = `status-chip ${proposal.schema_status === "stale" ? "warning" : "neutral"}`;
    if (typeof proposal.confidence === "number") {
      $("#proposal-confidence-chip").hidden = false;
      $("#proposal-confidence-chip").textContent = `confidence ${proposal.confidence.toFixed(2)}`;
    } else {
      $("#proposal-confidence-chip").hidden = true;
    }
    $("#proposal-fields").innerHTML = fieldsMarkup(proposal.content);
    $("#edit-description").value = proposal.content?.description || "";
    $("#edit-aliases").value = (proposal.content?.aliases || []).join(", ");
    $("#edit-default-aggregation").value = proposal.content?.default_aggregation || "";
    $("#preview-result").hidden = true;
    hideRejectRow();
    applyCatalogScopeGating();
    // Mirrors catalog/governance.py's state machine exactly: edit/approve
    // only from pending, reject from pending or approved, publish only from
    // approved — showing a button the backend would reject just trades one
    // error toast for a worse one, so the UI hides what can't legally run.
    const status = proposal.review_status;
    $("#edit-panel").hidden = status !== "pending";
    $("#approve-proposal").hidden = status !== "pending";
    $("#reject-proposal").hidden = !(status === "pending" || status === "approved");
    $("#publish-proposal").hidden = status !== "approved";
    $("#publish-preview-panel").hidden = status !== "approved";
    $("#proposal-action-buttons").hidden = !(status === "pending" || status === "approved");
    loadPublishedComparison(proposal);
  }

  function selectProposal(proposalId) {
    const proposal = state.catalogProposals.find((item) => item.proposal_id === proposalId);
    if (!proposal) return;
    renderProposalDetail(proposal);
    renderProposalList();
  }

  async function refreshSelectedProposalAfterMutation() {
    const connection = $("#catalog-connection").value;
    const previouslySelected = state.selectedProposalId;
    await loadCatalogProposals();
    if (!previouslySelected) return;
    try {
      const proposal = await api(
        `/admin/catalog/${encodeURIComponent(connection)}/proposals/${encodeURIComponent(previouslySelected)}`
      );
      renderProposalDetail(proposal);
      renderProposalList();
    } catch (error) {
      // The proposal was deleted, or the connection changed underneath the
      // panel — fall back to closing it rather than showing stale content.
      resetProposalSelection();
      toast(error.message, "bad");
    }
  }

  async function saveProposalEdit() {
    const proposal = selectedProposal();
    if (!proposal) return;
    const button = $("#save-proposal-edit");
    setBusy(button, true, "Saving…");
    try {
      const content = {};
      const description = $("#edit-description").value.trim();
      const aliases = csvValues($("#edit-aliases").value);
      const defaultAggregation = $("#edit-default-aggregation").value.trim();
      if (description) content.description = description;
      if (aliases.length) content.aliases = aliases;
      if (defaultAggregation) content.default_aggregation = defaultAggregation;
      if (!Object.keys(content).length) throw new Error("Provide at least one field before saving.");
      await api(
        `/admin/catalog/${encodeURIComponent(proposal.target.connection_id)}/proposals/${encodeURIComponent(proposal.proposal_id)}`,
        { method: "PATCH", body: JSON.stringify({ content }) }
      );
      toast("Proposal content updated.");
      await refreshSelectedProposalAfterMutation();
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      setBusy(button, false);
    }
  }

  async function runPublishPreview() {
    const proposal = selectedProposal();
    if (!proposal) return;
    const principal = $("#preview-principal").value.trim();
    if (!principal) { toast("Enter a principal subject to preview against.", "bad"); return; }
    const button = $("#run-preview");
    setBusy(button, true, "Checking…");
    try {
      const params = new URLSearchParams({ principal_subject: principal });
      const result = await api(
        `/admin/catalog/${encodeURIComponent(proposal.target.connection_id)}/proposals/${encodeURIComponent(proposal.proposal_id)}/preview?${params}`
      );
      const el = $("#preview-result");
      el.hidden = false;
      const bad = !result.visible_to_principal || result.would_conflict;
      el.className = `decision-result ${bad ? "denied" : "allowed"}`;
      const lines = [
        `Visible to ${escapeHtml(principal)}: ${result.visible_to_principal ? "yes" : "no"}`,
        `Would ${escapeHtml(result.change_kind || "change")} this entry`,
      ];
      if (result.would_conflict) lines.push(`Conflicts on: ${result.conflicting_fields.join(", ")}`);
      el.innerHTML = `
        <div class="decision-head"><div><p class="eyebrow">Publish preview</p><h3>${result.would_conflict ? "Would be a reviewable conflict" : "Safe to publish"}</h3></div><span class="status-chip ${bad ? "bad" : "good"}">${result.would_conflict ? "Conflict" : "Clear"}</span></div>
        <ul>${lines.map((line) => `<li>${line}</li>`).join("")}</ul>`;
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      setBusy(button, false);
    }
  }

  async function approveSelectedProposal() {
    const proposal = selectedProposal();
    if (!proposal) return;
    const button = $("#approve-proposal");
    setBusy(button, true, "Approving…");
    try {
      await api(
        `/admin/catalog/${encodeURIComponent(proposal.target.connection_id)}/proposals/${encodeURIComponent(proposal.proposal_id)}/approve`,
        { method: "POST" }
      );
      toast("Proposal approved.");
      await refreshSelectedProposalAfterMutation();
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      setBusy(button, false);
    }
  }

  function showRejectRow() {
    $("#reject-row").hidden = false;
    $("#proposal-action-buttons").hidden = true;
    $("#proposal-actions").classList.add("reasoning");
    $("#reject-reason").value = "";
    $("#reject-reason").focus();
  }

  function hideRejectRow() {
    $("#reject-row").hidden = true;
    $("#proposal-action-buttons").hidden = false;
    $("#proposal-actions").classList.remove("reasoning");
  }

  async function confirmRejectProposal() {
    const proposal = selectedProposal();
    if (!proposal) return;
    const reason = $("#reject-reason").value.trim();
    if (!reason) { toast("A rejection reason is required.", "bad"); return; }
    const button = $("#confirm-reject");
    setBusy(button, true, "Rejecting…");
    try {
      await api(
        `/admin/catalog/${encodeURIComponent(proposal.target.connection_id)}/proposals/${encodeURIComponent(proposal.proposal_id)}/reject`,
        { method: "POST", body: JSON.stringify({ reason }) }
      );
      toast("Proposal rejected.");
      hideRejectRow();
      await refreshSelectedProposalAfterMutation();
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      setBusy(button, false);
    }
  }

  async function publishSelectedProposal() {
    const proposal = selectedProposal();
    if (!proposal) return;
    const confirmed = await confirmAction({
      title: "Publish this proposal?",
      message: "This merges the proposed content into the live catalog agents read from. Type PUBLISH to continue.",
      phrase: "PUBLISH",
    });
    if (!confirmed) { toast('Confirmation cancelled. Enter "PUBLISH" exactly to continue.'); return; }
    try {
      const result = await api(
        `/admin/catalog/${encodeURIComponent(proposal.target.connection_id)}/proposals/${encodeURIComponent(proposal.proposal_id)}/publish`,
        { method: "POST" }
      );
      toast(`Published as catalog version v${result.version_id}.`);
      await refreshSelectedProposalAfterMutation();
      await loadCatalogVersions();
    } catch (error) {
      toast(error.message, "bad");
    }
  }

  async function loadCatalogVersions() {
    const connection = $("#catalog-connection").value;
    if (!connection || !hasScope("catalog:review")) {
      state.catalogVersions = [];
      $("#catalog-history-body").innerHTML = '<tr><td colspan="7" class="empty-cell">Select a connection to load catalog version history.</td></tr>';
      return;
    }
    try {
      state.catalogVersions = await api(`/admin/catalog/${encodeURIComponent(connection)}/versions`);
      renderCatalogHistory();
    } catch (error) {
      state.catalogVersions = [];
      $("#catalog-history-body").innerHTML = `<tr><td colspan="7" class="empty-cell">${escapeHtml(error.message)}</td></tr>`;
    }
  }

  function catalogVersionTargetLabel(version) {
    const label = version.column ? `${version.table}.${version.column}` : version.table;
    return version.to_table ? `${label} → ${version.to_table}.${version.to_column}` : label;
  }

  function renderCatalogHistory() {
    $("#catalog-history-body").innerHTML = state.catalogVersions.length ? [...state.catalogVersions].reverse().map((version) => `
      <tr>
        <td>v${escapeHtml(version.version_id)}</td>
        <td><span class="status-chip ${version.action === "rollback" ? "warning" : "good"}">${escapeHtml(version.action)}</span></td>
        <td>${escapeHtml(catalogVersionTargetLabel(version))}</td>
        <td>${escapeHtml(version.fields_changed.join(", ") || "—")}</td>
        <td>${escapeHtml(version.actor)}</td>
        <td title="${escapeHtml(formatDate(version.occurred_at))}">${escapeHtml(relativeDate(version.occurred_at))}</td>
        <td><button class="button secondary" type="button" data-rollback-version="${escapeHtml(version.version_id)}" ${hasScope("catalog:rollback") ? "" : "disabled"}>Roll back</button></td>
      </tr>`).join("") : '<tr><td colspan="7" class="empty-cell">No catalog versions yet for this connection.</td></tr>';
  }

  async function rollbackCatalogVersion(versionId) {
    const connection = $("#catalog-connection").value;
    const phrase = `ROLLBACK v${versionId}`;
    const confirmed = await confirmAction({
      title: `Roll back catalog to v${versionId}?`,
      message: `This restores the field values this version replaced (or removes the entry it created). Type ${phrase} to continue.`,
      phrase,
    });
    if (!confirmed) { toast(`Confirmation cancelled. Enter “${phrase}” exactly to proceed.`); return; }
    try {
      await api(
        `/admin/catalog/${encodeURIComponent(connection)}/versions/${encodeURIComponent(versionId)}/rollback`,
        { method: "POST" }
      );
      toast(`Catalog rolled back to v${versionId}.`);
      await loadCatalogVersions();
      await refreshSelectedProposalAfterMutation();
    } catch (error) {
      toast(error.message, "bad");
    }
  }

  // --- Curate (human-authored catalog entries, TODO item 84) -------------

  const curateState = { tables: [], columns: [], toColumns: [] };

  function curateObjectType() {
    return $("#curate-object-type").value;
  }

  function applyCurateFieldVisibility() {
    const type = curateObjectType();
    $("#curate-column-wrap").hidden = type === "table";
    $("#curate-to-table-wrap").hidden = type !== "relationship";
    $("#curate-to-column-wrap").hidden = type !== "relationship";
    $("#curate-default-aggregation-wrap").hidden = type !== "table";
    // A relationship carries no aliases (mirrors the backend validator).
    $("#curate-aliases").closest("label").hidden = type === "relationship";
  }

  function optionMarkup(values, placeholder) {
    if (!values.length) return `<option value="">${escapeHtml(placeholder)}</option>`;
    return values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
  }

  async function loadCurateTables() {
    const connection = $("#curate-connection").value;
    curateState.tables = [];
    if (!connection) {
      $("#curate-table").innerHTML = optionMarkup([], "No connection");
      return;
    }
    try {
      const body = await api(`/${encodeURIComponent(connection)}/tables`);
      curateState.tables = (body.tables || []).map((table) => table.name || table);
    } catch (error) {
      curateState.tables = [];
      toast(error.message, "bad");
    }
    $("#curate-table").innerHTML = optionMarkup(curateState.tables, "No visible tables");
    $("#curate-to-table").innerHTML = optionMarkup(curateState.tables, "No visible tables");
    await loadCurateColumns();
  }

  async function fetchTableColumns(table) {
    const connection = $("#curate-connection").value;
    if (!connection || !table) return [];
    try {
      const description = await api(
        `/${encodeURIComponent(connection)}/tables/${encodeURIComponent(table)}`
      );
      return (description.columns || []).map((column) => column.name);
    } catch (error) {
      toast(error.message, "bad");
      return [];
    }
  }

  async function loadCurateColumns() {
    curateState.columns = await fetchTableColumns($("#curate-table").value);
    $("#curate-column").innerHTML = optionMarkup(curateState.columns, "No visible columns");
    await loadCurateToColumns();
    await loadCuratePublished();
  }

  async function loadCurateToColumns() {
    if (curateObjectType() !== "relationship") return;
    curateState.toColumns = await fetchTableColumns($("#curate-to-table").value);
    $("#curate-to-column").innerHTML = optionMarkup(curateState.toColumns, "No visible columns");
  }

  function curateTarget() {
    const type = curateObjectType();
    const target = { object_type: type, table: $("#curate-table").value };
    if (type === "column" || type === "relationship") target.column = $("#curate-column").value;
    if (type === "relationship") {
      target.to_table = $("#curate-to-table").value;
      target.to_column = $("#curate-to-column").value;
    }
    return target;
  }

  async function loadCuratePublished() {
    const dl = $("#curate-published-fields");
    const target = curateTarget();
    if (!target.table) {
      dl.innerHTML = '<p class="empty-state">Pick a target to compare against the live catalog.</p>';
      return;
    }
    // Reuses the exact compare shape the review workbench uses.
    await loadPublishedComparison({ target: { ...target, connection_id: $("#curate-connection").value } });
    const rendered = state.publishedComparison
      ? fieldsMarkup(state.publishedComparison)
      : `${fieldsMarkup(null)}`;
    dl.innerHTML = rendered;
    if (!state.publishedComparison) {
      dl.insertAdjacentHTML("afterbegin", '<p class="empty-state">Nothing published for this target yet — submitting will propose a new entry.</p>');
    }
  }

  function populateCurateConnectionOptions() {
    const select = $("#curate-connection");
    const previous = select.value;
    const items = state.connections.map((c) => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.id)}</option>`).join("");
    select.innerHTML = items || '<option value="">No visible connections</option>';
    if (previous && state.connections.some((c) => c.id === previous)) select.value = previous;
  }

  let curateInitialized = false;
  function initCurateView() {
    const authorized = hasScope("catalog:author");
    $("#curate-gate").hidden = authorized;
    $("#curate-body").hidden = !authorized;
    if (!authorized || curateInitialized) return;
    curateInitialized = true;
    populateCurateConnectionOptions();
    applyCurateFieldVisibility();
    loadCurateTables();
  }

  async function submitManualProposal(event) {
    event.preventDefault();
    const connection = $("#curate-connection").value;
    if (!connection) { toast("Select a connection first.", "bad"); return; }
    const type = curateObjectType();
    const target = curateTarget();
    if (!target.table) { toast("Select a table.", "bad"); return; }
    if ((type === "column" || type === "relationship") && !target.column) { toast("Select a column.", "bad"); return; }
    if (type === "relationship" && (!target.to_table || !target.to_column)) { toast("Select the related table and column.", "bad"); return; }

    const payload = { object_type: type, table: target.table };
    if (target.column) payload.column = target.column;
    if (target.to_table) payload.to_table = target.to_table;
    if (target.to_column) payload.to_column = target.to_column;
    const description = $("#curate-description").value.trim();
    const aliases = type === "relationship" ? [] : csvValues($("#curate-aliases").value);
    const defaultAggregation = type === "table" ? $("#curate-default-aggregation").value.trim() : "";
    if (description) payload.description = description;
    if (aliases.length) payload.aliases = aliases;
    if (defaultAggregation) payload.default_aggregation = defaultAggregation;
    if (!description && !aliases.length && !defaultAggregation) {
      toast("Provide at least one of description, aliases, or default aggregation.", "bad");
      return;
    }

    const button = $("#submit-manual-proposal");
    setBusy(button, true, "Submitting…");
    try {
      await api(`/admin/catalog/${encodeURIComponent(connection)}/proposals`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      toast("Manual proposal created — pending review.");
      $("#curate-description").value = "";
      $("#curate-aliases").value = "";
      $("#curate-default-aggregation").value = "";
      // Surface it in the review queue so the reviewer picks it straight up.
      if (hasScope("catalog:review")) {
        $("#catalog-connection").value = connection;
        $("#catalog-status-filter").value = "pending";
        $("#catalog-source-filter").value = "manual";
        resetProposalSelection();
        showView("catalog");
        loadCatalogProposals();
        loadCatalogVersions();
      }
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      setBusy(button, false);
    }
  }

  function healthStatusChipClass(status) {
    if (status === "healthy") return "good";
    if (status === "degraded") return "bad";
    if (status === "disabled") return "neutral";
    return "warning"; // unknown — never checked yet
  }

  function renderConnectionHealth() {
    const canTest = hasScope("admin:connections:test");
    $("#health-body").innerHTML = state.connectionHealth.length ? state.connectionHealth.map((item) => {
      const testTitle = !canTest
        ? "Requires the admin:connections:test scope"
        : !item.enabled
          ? "Connection is disabled"
          : "Trigger an immediate re-check";
      return `<tr data-health-row="${escapeHtml(item.connection_id)}">
        <td>${escapeHtml(item.connection_id)}</td>
        <td>${escapeHtml(item.dialect)}</td>
        <td><span class="status-chip ${healthStatusChipClass(item.status)}">${escapeHtml(item.status)}</span>${item.failure_category ? `<br><small>${escapeHtml(item.failure_category)}</small>` : ""}</td>
        <td title="${escapeHtml(formatDate(item.last_checked ? item.last_checked * 1000 : null))}">${item.last_checked ? escapeHtml(relativeDate(item.last_checked * 1000)) : "—"}</td>
        <td title="${escapeHtml(formatDate(item.last_success ? item.last_success * 1000 : null))}">${item.last_success ? escapeHtml(relativeDate(item.last_success * 1000)) : "—"}</td>
        <td>${item.latency_ms != null ? `${item.latency_ms.toFixed(1)} ms` : "—"}</td>
        <td>${item.schema_reflected ? "cached" : "—"}</td>
        <td><button class="button secondary" type="button" data-test-connection="${escapeHtml(item.connection_id)}" title="${escapeHtml(testTitle)}" ${canTest && item.enabled ? "" : "disabled"}>Test now</button></td>
      </tr>`;
    }).join("") : '<tr><td colspan="8" class="empty-cell">No connections configured.</td></tr>';
  }

  async function loadConnectionHealth() {
    if (!hasScope("admin:connections:read")) {
      state.connectionHealth = [];
      $("#health-body").innerHTML = '<tr><td colspan="8" class="empty-cell">Connect with admin:connections:read to load connection health.</td></tr>';
      return;
    }
    const button = $("#refresh-health");
    setBusy(button, true, "Loading…");
    try {
      state.connectionHealth = await api("/admin/connections");
      renderConnectionHealth();
    } catch (error) {
      state.connectionHealth = [];
      $("#health-body").innerHTML = `<tr><td colspan="8" class="empty-cell">${escapeHtml(error.message)}</td></tr>`;
    } finally {
      setBusy(button, false);
    }
  }

  function paramSignature(param) {
    const bits = [escapeHtml(param.type)];
    if (param.is_list) bits.push("list");
    bits.push(param.required ? "required" : "optional");
    if (param.allowed_values) bits.push(`one of ${escapeHtml(param.allowed_values.join(", "))}`);
    return `<span class="template-param" title="${escapeHtml(param.description || "")}"><code>${escapeHtml(param.name)}</code> ${bits.join(" · ")}</span>`;
  }

  function renderTemplates() {
    $("#template-cards").innerHTML = state.templateList.length
      ? state.templateList
          .map(
            (tpl) => `
      <div class="template-card" data-template-id="${escapeHtml(tpl.id)}">
        <button class="template-card-head" type="button" data-template-toggle="${escapeHtml(tpl.id)}" aria-expanded="false">
          <div><strong>${escapeHtml(tpl.id)}</strong><p>${escapeHtml(tpl.description || "No description")}</p>
            <div class="template-params">${tpl.parameters.length ? tpl.parameters.map(paramSignature).join("") : "<small>No parameters</small>"}</div>
          </div>
          <span class="template-card-meta"><span class="status-chip neutral">${escapeHtml(tpl.connection)}</span><span class="template-card-caret" aria-hidden="true">▸</span></span>
        </button>
        <div class="template-query" data-template-query="${escapeHtml(tpl.id)}" hidden></div>
      </div>`
          )
          .join("")
      : '<p class="empty-state">No query templates visible to this principal.</p>';
  }

  function toggleTemplateQuery(head) {
    const card = head.closest(".template-card");
    if (!card) return;
    const id = head.dataset.templateToggle;
    const region = $(".template-query", card);
    const opening = region.hidden;
    if (opening && !region.dataset.rendered) {
      const template = state.templateSources[id];
      if (template) {
        region.innerHTML =
          `<div class="template-query-label">Query skeleton <small>read-only — edit via the templates.yaml draft</small></div>` +
          `<pre class="code-view">${escapeHtml(JSON.stringify(template.query, null, 2))}</pre>`;
      } else {
        region.innerHTML = canRead() || canWrite()
          ? '<p class="empty-state">Query skeleton not found in the current templates.yaml draft.</p>'
          : '<p class="empty-state">Connect with <code>admin:config:read</code> to view the query skeleton.</p>';
      }
      region.dataset.rendered = "1";
    }
    region.hidden = !opening;
    head.setAttribute("aria-expanded", String(opening));
    card.classList.toggle("open", opening);
  }

  async function loadTemplateSources() {
    // The agent-facing /query-templates projection omits the query skeleton by
    // design; the admin reads it from the templates.yaml it already governs.
    state.templateSources = {};
    if (!canRead() && !canWrite()) return;
    // The browse list is the live template set, so read its queries from the
    // active templates.yaml (fall back to the draft when no active copy loaded).
    const yaml = state.activeDocuments.templates || state.draftDocuments.templates || "";
    if (!yaml.trim()) return;
    try {
      const parsed = await api("/admin/ui/templates/parse", {
        method: "POST",
        body: JSON.stringify({ templates_yaml: yaml }),
      });
      // Defense in depth: only retain query skeletons for templates that are in
      // this principal's visibility-filtered browse list, so the in-memory
      // footprint never exceeds what a card could actually reveal.
      const visibleIds = new Set(state.templateList.map((template) => template.id));
      for (const template of parsed.document?.templates || []) {
        if (visibleIds.has(template.id)) state.templateSources[template.id] = template;
      }
    } catch {
      state.templateSources = {};
    }
  }

  async function loadTemplates() {
    const button = $("#refresh-templates");
    setBusy(button, true, "Loading…");
    try {
      state.templateList = await api("/query-templates");
      await loadTemplateSources();
      renderTemplates();
    } catch (error) {
      state.templateList = [];
      $("#template-cards").innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    } finally {
      setBusy(button, false);
    }
  }

  const obsPercent = (value) => (value == null ? "—" : `${(value * 100).toFixed(1)}%`);
  const obsSeconds = (stat) =>
    stat && stat.avg_seconds != null ? `${(stat.avg_seconds * 1000).toFixed(0)} ms` : "—";

  function obsTopReasons(byReason) {
    const entries = Object.entries(byReason || {}).sort((a, b) => b[1] - a[1]);
    if (!entries.length) return "—";
    return entries
      .slice(0, 2)
      .map(([reason, count]) => `${escapeHtml(reason)} ${count}`)
      .join(", ");
  }

  function obsCard(label, value, sub) {
    return `<div class="observability-card"><span class="observability-card-label">${escapeHtml(label)}</span><strong class="observability-card-value">${escapeHtml(value)}</strong>${sub ? `<small>${escapeHtml(sub)}</small>` : ""}</div>`;
  }

  function renderObservability() {
    const overview = state.observability;
    const banner = $("#observability-snapshot");
    if (!overview) {
      banner.hidden = true;
      $("#observability-table-wrap").hidden = true;
      return;
    }
    banner.hidden = false;
    banner.textContent = `${overview.note} (since ${overview.since})`;

    const cost = overview.cost_estimation || {};
    $("#observability-cards").innerHTML = [
      obsCard("Queries", String(overview.queries_total), `${overview.queries_success} ok · ${overview.queries_rejected} rejected`),
      obsCard("Top reject reason", obsTopReasons(overview.rejections_by_reason), "since process start"),
      obsCard("Avg duration", obsSeconds(overview.duration), `${overview.duration ? overview.duration.count : 0} sampled`),
      obsCard("Concurrency", obsPercent(overview.concurrency_utilization), `${overview.concurrency_in_use_total}/${overview.concurrency_max_total} slots`),
      obsCard("Queue depth", String(overview.queue_depth_total), "waiting now"),
      obsCard("Cost-estimate fail-open", obsPercent(cost.fail_open_rate), `${cost.unavailable || 0}/${cost.attempts || 0} unavailable`),
    ].join("");

    const rows = overview.by_connection || [];
    $("#observability-table-wrap").hidden = rows.length === 0;
    $("#observability-body").innerHTML = rows
      .map(
        (conn) => `<tr>
        <td>${escapeHtml(conn.connection)}</td>
        <td>${conn.queries_total}</td>
        <td>${conn.queries_rejected}</td>
        <td>${obsTopReasons(conn.rejections_by_reason)}</td>
        <td>${obsSeconds(conn.duration)}</td>
        <td>${obsPercent(conn.concurrency_utilization)}</td>
        <td>${obsPercent(conn.cost_estimation ? conn.cost_estimation.fail_open_rate : null)}</td>
      </tr>`
      )
      .join("");
  }

  const ANOMALY_LABELS = {
    volume_spike: "Volume spike",
    rejection_rate_spike: "Rejection spike",
    new_connection_access: "New connection",
  };

  function obsDuration(seconds) {
    if (seconds == null) return "—";
    if (seconds % 86400 === 0) return `${seconds / 86400}d`;
    if (seconds % 3600 === 0) return `${seconds / 3600}h`;
    if (seconds % 60 === 0) return `${seconds / 60}m`;
    return `${seconds}s`;
  }

  const anomalyRatePerMin = (value) => (value == null ? "—" : `${value.toFixed(1)}/min`);

  // Returns plain text (escaped at insert time), never HTML.
  function anomalySignalDetail(signal) {
    if (signal.kind === "volume_spike") {
      return signal.ratio != null ? `${signal.ratio.toFixed(1)}× its baseline rate` : "elevated volume";
    }
    if (signal.kind === "rejection_rate_spike") {
      return `${obsPercent(signal.recent_rejection_rate)} rejected vs ${obsPercent(signal.baseline_rejection_rate)} baseline`;
    }
    if (signal.kind === "new_connection_access") {
      return `reached ${signal.connection || "?"} — untouched in baseline`;
    }
    return "—";
  }

  function renderAnomalies() {
    const report = state.anomalies;
    const note = $("#anomaly-note");
    const wrap = $("#anomaly-table-wrap");
    const empty = $("#anomaly-empty");
    if (!report) {
      note.hidden = true;
      wrap.hidden = true;
      empty.hidden = true;
      return;
    }
    if (report.source === "disabled") {
      note.hidden = true;
      wrap.hidden = true;
      empty.hidden = false;
      empty.textContent =
        "Anomaly surfacing is disabled — enable the JSONL audit sink (AUDIT_SINK_BACKEND=jsonl) to compute per-principal baselines.";
      return;
    }
    const windowLabel = `recent ${obsDuration(report.recent_window_seconds)} vs baseline ${obsDuration(report.baseline_window_seconds)}`;
    note.hidden = false;
    note.textContent = `${report.note} (${windowLabel}; ${report.events_scanned} events scanned${report.truncated ? ", truncated" : ""}${report.malformed ? `, ${report.malformed} malformed` : ""})`;

    const rows = [];
    (report.principals || []).forEach((principal) => {
      principal.signals.forEach((signal, index) => {
        rows.push(`<tr>
        <td>${index === 0 ? escapeHtml(principal.principal_id) : ""}</td>
        <td><span class="anomaly-badge anomaly-${escapeHtml(signal.kind)}">${escapeHtml(ANOMALY_LABELS[signal.kind] || signal.kind)}</span></td>
        <td>${escapeHtml(anomalySignalDetail(signal))}</td>
        <td>${signal.recent_count} · ${anomalyRatePerMin(signal.recent_rate_per_min)}</td>
        <td>${signal.baseline_count} · ${anomalyRatePerMin(signal.baseline_rate_per_min)}</td>
      </tr>`);
      });
    });

    wrap.hidden = rows.length === 0;
    empty.hidden = rows.length !== 0;
    if (!rows.length) {
      empty.textContent = "No anomalies — every caller's recent activity is within its own baseline.";
    }
    $("#anomaly-body").innerHTML = rows.join("");
  }

  async function loadObservability() {
    if (!hasScope("admin:observability:read")) {
      state.observability = null;
      state.anomalies = null;
      $("#observability-snapshot").hidden = true;
      $("#observability-table-wrap").hidden = true;
      $("#observability-cards").innerHTML =
        '<p class="empty-state">Connect with admin:observability:read to load observability.</p>';
      renderAnomalies();
      return;
    }
    const button = $("#refresh-observability");
    setBusy(button, true, "Loading…");
    try {
      state.observability = await api("/admin/observability/overview");
      renderObservability();
    } catch (error) {
      state.observability = null;
      $("#observability-snapshot").hidden = true;
      $("#observability-table-wrap").hidden = true;
      $("#observability-cards").innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
    }
    try {
      state.anomalies = await api("/admin/observability/anomalies");
      renderAnomalies();
    } catch (error) {
      state.anomalies = null;
      $("#anomaly-note").hidden = true;
      $("#anomaly-table-wrap").hidden = true;
      $("#anomaly-empty").hidden = false;
      $("#anomaly-empty").textContent = error.message;
    }
    setBusy(button, false);
  }

  // --- Query-template authoring (TODO.md item 87) ------------------------
  // A guided form composes a validated QueryTemplate and merges it into the
  // draft templates.yaml change-set document, which then flows through the
  // shared validate → stage → apply → rollback (Releases). Raw-YAML editing of
  // templates.yaml stays available in the change-set editor as the escape hatch.
  function templateParamRowMarkup() {
    return `<div class="param-row">
      <input type="text" data-p="name" placeholder="name" aria-label="Parameter name">
      <select data-p="type" aria-label="Parameter type"><option value="string">string</option><option value="integer">integer</option><option value="number">number</option><option value="boolean">boolean</option></select>
      <label class="inline-check"><input type="checkbox" data-p="required" checked> req</label>
      <label class="inline-check"><input type="checkbox" data-p="is_list"> list</label>
      <input type="text" data-p="default" placeholder="default" aria-label="Default value">
      <input type="text" data-p="min" placeholder="min" aria-label="Min">
      <input type="text" data-p="max" placeholder="max" aria-label="Max">
      <input type="text" data-p="max_length" placeholder="max len" aria-label="Max length">
      <input type="text" data-p="allowed_values" placeholder="allowed (csv)" aria-label="Allowed values">
      <button type="button" class="text-button danger" data-remove-param aria-label="Remove parameter">✕</button>
    </div>`;
  }

  const templateParamsEmpty = '<p class="empty-state">No parameters — a template can be a fixed query with no slots.</p>';

  function addTemplateParamRow() {
    const rows = $("#template-param-rows");
    if ($(".empty-state", rows)) rows.innerHTML = "";
    rows.insertAdjacentHTML("beforeend", templateParamRowMarkup());
  }

  function coerceTemplateScalar(type, raw) {
    // Best-effort coercion from a text field to the slot's declared type; if
    // it can't coerce, the raw string is kept and the model validation rejects
    // it with a clear message the caller sees as a toast.
    if (type === "integer" || type === "number") { const n = Number(raw); return raw !== "" && !Number.isNaN(n) ? n : raw; }
    if (type === "boolean") { if (raw === "true") return true; if (raw === "false") return false; return raw; }
    return raw;
  }

  function readTemplateParams() {
    return $$("#template-param-rows .param-row").map((row) => {
      const get = (p) => $(`[data-p="${p}"]`, row);
      const type = get("type").value;
      const param = { name: get("name").value.trim(), type, required: get("required").checked };
      if (get("is_list").checked) param.is_list = true;
      const def = get("default").value.trim();
      if (def !== "") param.default = param.is_list ? csvValues(def).map((v) => coerceTemplateScalar(type, v)) : coerceTemplateScalar(type, def);
      const min = get("min").value.trim(); if (min !== "") param.min = Number(min);
      const max = get("max").value.trim(); if (max !== "") param.max = Number(max);
      const maxLen = get("max_length").value.trim(); if (maxLen !== "") param.max_length = Number(maxLen);
      const allowed = get("allowed_values").value.trim();
      if (allowed !== "") param.allowed_values = csvValues(allowed).map((v) => coerceTemplateScalar(type, v));
      return param;
    });
  }

  async function addTemplateToDraft(event) {
    event.preventDefault();
    if (!canWrite()) { toast("Authoring a template needs admin:config:write.", "bad"); return; }
    const button = $("#add-template-to-draft");
    setBusy(button, true, "Adding…");
    try {
      const id = $("#template-author-id").value.trim();
      const connection = $("#template-author-connection").value;
      if (!id) throw new Error("Template id is required.");
      if (!connection) throw new Error("Select a connection.");
      let query;
      try { query = JSON.parse($("#template-author-query").value || ""); }
      catch { throw new Error("Query skeleton must be valid JSON."); }
      const template = { id, connection, parameters: readTemplateParams(), query };
      const description = $("#template-author-description").value.trim();
      if (description) template.description = description;

      // Round-trip the current draft through the shared model, merge by id, and
      // re-render — a validation failure (bad slot, duplicate id) comes back as
      // a 422 and surfaces below without corrupting the draft.
      const parsed = await api("/admin/ui/templates/parse", {
        method: "POST",
        body: JSON.stringify({ templates_yaml: state.draftDocuments.templates || "" }),
      });
      const document = parsed.document && typeof parsed.document === "object" ? parsed.document : {};
      const templates = Array.isArray(document.templates) ? document.templates : [];
      const index = templates.findIndex((entry) => entry.id === id);
      if (index >= 0) templates[index] = template; else templates.push(template);
      document.templates = templates;

      const rendered = await api("/admin/ui/templates/render", {
        method: "POST",
        body: JSON.stringify({ document }),
      });
      state.draftDocuments.templates = rendered.templates_yaml;
      if (state.selectedDocument === "templates") { $("#document-editor").value = rendered.templates_yaml; renderEditor(); }
      syncDirtyState();
      resetTemplateAuthorForm();
      closeTemplateAuthorForm();
      toast(index >= 0 ? `Template “${id}” updated in the templates.yaml draft.` : `Template “${id}” added to the templates.yaml draft.`);
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      setBusy(button, false);
    }
  }

  let templateAuthorInitialized = false;
  function openTemplateAuthorForm() {
    $("#template-author-form").hidden = false;
    $("#toggle-template-author").hidden = true;
    $("#template-author-id").focus();
  }
  function closeTemplateAuthorForm() {
    $("#template-author-form").hidden = true;
    $("#toggle-template-author").hidden = !canWrite();
  }
  function resetTemplateAuthorForm() {
    ["#template-author-id", "#template-author-description", "#template-author-query"].forEach((sel) => { $(sel).value = ""; });
    $("#template-param-rows").innerHTML = templateParamsEmpty;
  }
  function initTemplateAuthorView() {
    const authorized = canWrite();
    $("#template-author-gate").hidden = authorized;
    // The form is opened on demand via the toggle button, not always-on.
    $("#toggle-template-author").hidden = !authorized || !$("#template-author-form").hidden;
    if (!authorized) $("#template-author-form").hidden = true;
    if (!authorized || templateAuthorInitialized) return;
    templateAuthorInitialized = true;
    $("#template-author-connection").innerHTML =
      state.connections.map((c) => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.id)}</option>`).join("")
      || '<option value="">No visible connections</option>';
  }

  async function testConnectionNow(connectionId) {
    const button = $(`[data-test-connection="${CSS.escape(connectionId)}"]`);
    setBusy(button, true, "Testing…");
    try {
      const result = await api(`/admin/connections/${encodeURIComponent(connectionId)}/test`, { method: "POST" });
      const index = state.connectionHealth.findIndex((item) => item.connection_id === connectionId);
      if (index >= 0) state.connectionHealth[index] = result;
      renderConnectionHealth();
      toast(
        `${connectionId}: ${result.status}${result.failure_category ? ` — ${result.failure_category}` : ""}.`,
        result.status === "degraded" ? "bad" : ""
      );
    } catch (error) {
      // Covers unknown (404), disabled (409), no monitor running (503), and
      // rate-limited (429) — the backend's detail message already explains
      // which one and, for 429, how long to wait before trying again.
      toast(error.message, "bad");
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
      templates: current.templates_yaml || "",
    };
    if (!preserveDraft || !anyDocumentChanged()) state.draftDocuments = { ...state.activeDocuments };
    if (canWrite()) recoverPolicyDraftLocally();
    renderOverview(configuration);
    renderHistory();
    syncDirtyState();
    renderEditor();
    if (canWrite() || canRead()) await parsePolicyDocument();
    if (!state.templates.length) {
      try {
        state.templates = await api("/admin/config/templates");
        renderTemplateSelect();
      } catch (error) { toast(error.message, "bad"); }
    }
  }

  function renderTemplateSelect() {
    $("#template-select").innerHTML = state.templates
      .map((template) => `<option value="${escapeHtml(template.id)}">${escapeHtml(template.name)}</option>`)
      .join("");
    renderTemplateParams();
  }

  function renderTemplateParams() {
    const template = state.templates.find((item) => item.id === $("#template-select").value) || state.templates[0];
    state.templatePreview = null;
    $("#apply-template").disabled = true;
    $("#template-result").hidden = true;
    if (!template) { $("#template-params").innerHTML = ""; return; }
    $("#template-params").innerHTML = template.parameters.map((param) => {
      const inputType = param.type === "integer" ? "number" : "text";
      const placeholder = param.type === "string_list" ? "comma-separated" : "";
      const defaultValue = param.default != null ? param.default : "";
      return `
        <label ${param.type === "string_list" ? 'class="span-2"' : ""}>
          <span>${escapeHtml(param.label)}${param.required ? "" : " (optional)"}</span>
          <input data-param-name="${escapeHtml(param.name)}" data-param-type="${escapeHtml(param.type)}"
                 type="${inputType}" placeholder="${escapeHtml(placeholder)}" value="${escapeHtml(defaultValue)}">
          <small>${escapeHtml(param.description)}</small>
        </label>`;
    }).join("");
  }

  function collectTemplateParams() {
    const params = {};
    $$("#template-params [data-param-name]").forEach((input) => {
      const name = input.dataset.paramName;
      const type = input.dataset.paramType;
      const raw = input.value.trim();
      if (!raw) return;
      if (type === "integer") params[name] = Number(raw);
      else if (type === "string_list") params[name] = raw.split(",").map((item) => item.trim()).filter(Boolean);
      else params[name] = raw;
    });
    return params;
  }

  async function previewTemplate() {
    const templateId = $("#template-select").value;
    if (!templateId) return;
    const button = $("#preview-template");
    setBusy(button, true, "Rendering…");
    try {
      const result = await api("/admin/config/templates/render", {
        method: "POST",
        body: JSON.stringify({
          template_id: templateId,
          params: collectTemplateParams(),
          policy_yaml: state.draftDocuments.policy,
        }),
      });
      state.templatePreview = result;
      $("#template-result").hidden = false;
      $("#template-result").innerHTML = result.rules
        .map((rule) => `<li class="capability"><span>${escapeHtml(rule)}</span></li>`)
        .join("");
      $("#apply-template").disabled = false;
    } catch (error) {
      toast(error.message, "bad");
    } finally { setBusy(button, false); }
  }

  function applyTemplateToDraft() {
    if (!state.templatePreview) return;
    state.draftDocuments.policy = state.templatePreview.policy_yaml;
    syncDirtyState();
    selectDocument("policy");
    showView("changes");
    toast("Template applied to the local policy draft. Validate before staging.");
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
    renderConnectionList();
    populateConnectionSelects();
    populateCatalogConnectionOptions();
    curateInitialized = false;
    templateAuthorInitialized = false;
    // Set the template-authoring entry point (button vs. gate) as soon as scopes
    // are known, so it is correct even when the Templates view is deep-linked
    // and shown before connect() runs.
    initTemplateAuthorView();
    // Re-render the active domain's inner tabs now that scopes are known — this
    // is what surfaces/hides the catalog:author-gated Curate tab.
    const activeView = viewToTab[window.location.hash.slice(1)] ? window.location.hash.slice(1) : "overview";
    renderDomainTabs(viewToDomain[activeView], activeView);
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
    ["#apply-designer", "#remove-policy-layer", "#validate-draft", "#stage-draft", "#discard-draft", "#export-change-set", "#import-change-set"].forEach((selector) => {
      const element = $(selector);
      if (element) element.disabled = true;
    });
    $("#document-editor").readOnly = true;
  }

  function disconnect() {
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
    $$(".nav-item").forEach((item) => item.addEventListener("click", () => {
      const domain = navModel.find((entry) => entry.id === item.dataset.domain);
      if (domain) showView(defaultDomainView(domain));
    }));
    $("#domain-tabs").addEventListener("click", (event) => {
      const button = event.target.closest("[data-view-target]");
      if (button) showView(button.dataset.viewTarget);
    });
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
    $("#template-select").addEventListener("change", renderTemplateParams);
    $("#preview-template").addEventListener("click", previewTemplate);
    $("#apply-template").addEventListener("click", applyTemplateToDraft);
    $("#apply-designer").addEventListener("click", applyDesigner);
    $("#reset-designer").addEventListener("click", populateDesigner);
    $("#remove-policy-layer").addEventListener("click", () => removePolicyLayer().catch((error) => toast(error.message, "bad")));
    $("#policy-test-form").addEventListener("submit", runPolicyTest);
    $$(".document-tab").forEach((tab) => tab.addEventListener("click", () => selectDocument(tab.dataset.document)));
    $("#document-editor").addEventListener("input", (event) => {
      state.draftDocuments[state.selectedDocument] = event.target.value;
      syncDirtyState();
      if (state.selectedDocument === "policy") savePolicyDraftLocally();
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
    $("#check-schema").addEventListener("click", checkSchema);
    $("#stage-draft").addEventListener("click", stageDraft);
    $("#discard-draft").addEventListener("click", discardDraft);
    $("#export-change-set").addEventListener("click", () => exportChangeSet());
    $("#import-change-set").addEventListener("click", () => $("#import-change-set-file").click());
    $("#import-change-set-file").addEventListener("change", (event) => {
      const file = event.target.files && event.target.files[0];
      if (file) importChangeSet(file).catch((error) => toast(error.message, "bad"));
      event.target.value = "";
    });
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
    $("#catalog-connection").addEventListener("change", () => { resetProposalSelection(); loadCatalogProposals(); loadCatalogVersions(); });
    $("#catalog-status-filter").addEventListener("change", () => { resetProposalSelection(); loadCatalogProposals(); });
    $("#catalog-source-filter").addEventListener("change", renderProposalList);
    $("#catalog-object-filter").addEventListener("change", renderProposalList);
    $("#catalog-filters").addEventListener("submit", (event) => event.preventDefault());
    $("#refresh-catalog").addEventListener("click", () => loadCatalogProposals().then(() => toast("Proposal queue refreshed.")).catch((error) => toast(error.message, "bad")));
    $("#refresh-catalog-versions").addEventListener("click", () => loadCatalogVersions().then(() => toast("Catalog version history refreshed.")).catch((error) => toast(error.message, "bad")));
    $("#proposal-list").addEventListener("click", (event) => {
      const button = event.target.closest("[data-proposal-id]");
      if (button) selectProposal(button.dataset.proposalId);
    });
    $("#save-proposal-edit").addEventListener("click", saveProposalEdit);
    $("#run-preview").addEventListener("click", runPublishPreview);
    $("#approve-proposal").addEventListener("click", approveSelectedProposal);
    $("#reject-proposal").addEventListener("click", showRejectRow);
    $("#cancel-reject").addEventListener("click", hideRejectRow);
    $("#confirm-reject").addEventListener("click", confirmRejectProposal);
    $("#publish-proposal").addEventListener("click", publishSelectedProposal);
    $("#catalog-history-body").addEventListener("click", (event) => {
      const button = event.target.closest("[data-rollback-version]");
      if (button) rollbackCatalogVersion(button.dataset.rollbackVersion);
    });
    $("#curate-connection").addEventListener("change", loadCurateTables);
    $("#curate-object-type").addEventListener("change", () => { applyCurateFieldVisibility(); loadCurateToColumns(); loadCuratePublished(); });
    $("#curate-table").addEventListener("change", loadCurateColumns);
    $("#curate-column").addEventListener("change", loadCuratePublished);
    $("#curate-to-table").addEventListener("change", () => { loadCurateToColumns().then(loadCuratePublished); });
    $("#curate-to-column").addEventListener("change", loadCuratePublished);
    $("#curate-form").addEventListener("submit", submitManualProposal);
    $("#refresh-health").addEventListener("click", () => loadConnectionHealth());
    $("#refresh-templates").addEventListener("click", () => loadTemplates());
    $("#refresh-observability").addEventListener("click", () => loadObservability());
    $("#template-cards").addEventListener("click", (event) => {
      const toggle = event.target.closest("[data-template-toggle]");
      if (toggle) toggleTemplateQuery(toggle);
    });
    $("#toggle-template-author").addEventListener("click", openTemplateAuthorForm);
    $("#cancel-template-author").addEventListener("click", closeTemplateAuthorForm);
    $("#add-template-param").addEventListener("click", addTemplateParamRow);
    $("#template-param-rows").addEventListener("click", (event) => {
      const remove = event.target.closest("[data-remove-param]");
      if (!remove) return;
      remove.closest(".param-row").remove();
      if (!$("#template-param-rows .param-row")) $("#template-param-rows").innerHTML = templateParamsEmpty;
    });
    $("#template-author-form").addEventListener("submit", addTemplateToDraft);
    $("#health-body").addEventListener("click", (event) => {
      const button = event.target.closest("[data-test-connection]");
      if (button) testConnectionNow(button.dataset.testConnection);
    });
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
        await connect($("#auth-token").value);
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
    // No token is ever persisted, so there is nothing to restore — every load
    // starts with an in-memory-only authentication prompt.
    $("#auth-dialog").showModal();
  }

  initialize();
})();
