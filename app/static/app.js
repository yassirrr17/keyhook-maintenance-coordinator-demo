"use strict";

const state = {
  selectedId: null,
  filters: { status: "", urgency: "", category: "", q: "" },
  meta: null,
  counts: {},
  total: 0,
  busy: false,
};

const STATUS_LABELS = {
  received: "Received",
  triaged: "Triaged",
  needs_info: "Needs info",
  escalated: "Escalated",
  awaiting_approval: "Awaiting approval",
  approved: "Approved",
  rejected: "Rejected",
  work_order_created: "Work order created",
  contractor_notified: "Contractor drafted",
  verified: "Verified",
  verification_failed: "Failed verification",
};
const FILTER_ORDER = [
  "escalated", "needs_info", "awaiting_approval", "verification_failed", "verified", "rejected",
  "received", "triaged", "approved", "work_order_created", "contractor_notified",
];
const FAULT_LABELS = {
  wrong_trade: "Assign a contractor with the wrong trade",
  missing_contractor: "Leave the work order without a contractor",
  wrong_address: "Draft the contractor message with the wrong address",
  missing_sla: "Drop the urgency SLA from the work order",
};
const STEP_INDEX = {
  received: 0, triaged: 1, needs_info: 2, escalated: 2, awaiting_approval: 2,
  approved: 3, rejected: 3, work_order_created: 4, contractor_notified: 5, verified: 6, verification_failed: 6,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
const label = (s) => STATUS_LABELS[s] || s;
const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : "");
const tradeLabel = (t) => (t || "").replace(/_/g, " ");

function fmtTime(ts, withSeconds = false) {
  if (!ts) return "-";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleString("en-NZ", {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
    ...(withSeconds ? { second: "2-digit" } : {}),
  });
}

function statusChip(status) {
  return `<span class="chip dot s-${esc(status)}">${esc(label(status))}</span>`;
}
function urgencyChip(u) {
  return u ? `<span class="chip u-${esc(u)}">${esc(cap(u))}</span>` : "";
}
function categoryChip(c) {
  return c ? `<span class="chip cat">${esc(c)}</span>` : "";
}

// ------------------------------------------------------------------ API

async function api(path, { method = "GET", body } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    const err = new Error("Network error - is the server running?");
    err.errors = [];
    throw err;
  }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON body */ }
  if (!res.ok) {
    const err = new Error((data && typeof data.detail === "string" && data.detail) || `Request failed (${res.status})`);
    err.status = res.status;
    err.errors = (data && data.errors) || [];
    throw err;
  }
  return data;
}

function errorText(err) {
  if (err.errors && err.errors.length) {
    return err.errors.map((e) => `${e.field.replace(/_/g, " ")}: ${e.message}`).join(" · ");
  }
  return err.message;
}

let toastTimer;
function toast(message, kind = "") {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 4200);
}

async function withBusy(button, fn) {
  if (state.busy) return;
  state.busy = true;
  const original = button ? button.innerHTML : "";
  if (button) { button.disabled = true; button.innerHTML = `<span class="spinner"></span> Working…`; }
  try { await fn(); }
  finally {
    state.busy = false;
    if (button && document.body.contains(button)) { button.disabled = false; button.innerHTML = original; }
  }
}

// ------------------------------------------------------------------ status + list

async function loadStatus() {
  const badge = $("#mode-badge");
  try {
    const s = await api("/api/status");
    badge.textContent = s.label;
    badge.className = `mode-badge ${s.mode}`;
  } catch {
    badge.textContent = "Server unreachable";
    badge.className = "mode-badge";
  }
}

async function loadList() {
  const params = new URLSearchParams();
  Object.entries(state.filters).forEach(([k, v]) => { if (v) params.set(k, v); });
  const list = $("#request-list");
  try {
    const data = await api(`/api/requests?${params}`);
    state.counts = data.counts;
    state.total = data.total;
    renderFilters();
    renderList(data.items);
  } catch (err) {
    list.innerHTML = `<li class="list-empty">Could not load requests: ${esc(errorText(err))}</li>`;
  }
}

function renderFilters() {
  const chips = [`<button type="button" class="filter-chip ${state.filters.status ? "" : "active"}" data-status="">All<span class="count">${state.total}</span></button>`];
  FILTER_ORDER.forEach((s) => {
    const n = state.counts[s] || 0;
    if (!n && state.filters.status !== s) return;
    chips.push(`<button type="button" class="filter-chip ${state.filters.status === s ? "active" : ""}" data-status="${s}">${esc(label(s))}<span class="count">${n}</span></button>`);
  });
  const row = $("#status-filters");
  row.innerHTML = chips.join("");
  $$(".filter-chip", row).forEach((b) => b.addEventListener("click", () => {
    state.filters.status = b.dataset.status;
    loadList();
  }));
}

function renderList(items) {
  const list = $("#request-list");
  $("#list-count").textContent = `${items.length} shown`;
  if (!items.length) {
    list.innerHTML = `<li class="list-empty">No requests match these filters.</li>`;
    return;
  }
  list.innerHTML = items.map((r) => `
    <li class="request-item ${r.id === state.selectedId ? "selected" : ""}" data-id="${r.id}" tabindex="0" role="button" aria-label="Open request ${r.id}">
      <div class="row">
        <span class="title">${esc(r.description.split("\n")[0])}</span>
        <span class="faint small">#${r.id}</span>
      </div>
      <div class="meta">${esc(r.tenant_name)} · ${esc(r.property_address)}</div>
      <div class="chips">${statusChip(r.status)}${urgencyChip(r.urgency)}${categoryChip(r.category)}</div>
    </li>`).join("");
  $$(".request-item", list).forEach((li) => {
    const open = () => selectRequest(Number(li.dataset.id));
    li.addEventListener("click", open);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  });
}

async function selectRequest(id) {
  state.selectedId = id;
  $$(".request-item").forEach((li) => li.classList.toggle("selected", Number(li.dataset.id) === id));
  try {
    const detail = await api(`/api/requests/${id}`);
    renderDetail(detail);
    if (window.matchMedia("(max-width: 820px)").matches) $("#detail").scrollIntoView({ behavior: "smooth" });
  } catch (err) {
    $("#detail").innerHTML = `<div class="empty-state"><h2>Could not load request</h2><p>${esc(errorText(err))}</p></div>`;
  }
}

async function refreshAfterAction(detail) {
  renderDetail(detail);
  await loadList();
}

// ------------------------------------------------------------------ detail view

function renderStepper(d) {
  const status = d.request.status;
  const visited = new Set(d.events.map((e) => e.to_state).filter(Boolean));
  const current = STEP_INDEX[status] ?? 0;
  let gateLabel = "Approval gate";
  if (["needs_info", "escalated", "awaiting_approval"].includes(status)) gateLabel = label(status);
  else if (visited.has("escalated")) gateLabel = "Escalated → approved";
  const steps = [
    "Received", "Triaged", gateLabel,
    status === "rejected" ? "Rejected" : "Approved",
    "Work order", "Contractor draft",
    status === "verification_failed" ? "Failed verification" : "Verified",
  ];
  const tone = {
    escalated: "bad", needs_info: "warn", rejected: "bad",
    verified: "good", verification_failed: "bad",
  }[status] || "current";
  return `<div class="stepper" aria-label="Workflow progress">${steps.map((s, i) => {
    let cls = "";
    if (i < current) cls = "done";
    else if (i === current) cls = tone;
    const mark = i < current ? "✓" : (cls === "bad" ? "!" : (cls === "good" ? "✓" : i + 1));
    return `<div class="step ${cls}"><div class="step-node"><div class="step-dot">${mark}</div><div class="step-label">${esc(s)}</div></div>${i < steps.length - 1 ? '<div class="step-line"></div>' : ""}</div>`;
  }).join("")}</div>`;
}

function approverName() {
  return localStorage.getItem("approverName") || "";
}

function renderActionCard(d) {
  const r = d.request;
  const t = d.triage || {};
  const name = esc(approverName());
  switch (r.status) {
    case "escalated": {
      const flags = t.safety_flags || [];
      return `
      <div class="card danger" id="action-card">
        <div class="card-head"><h3>⚠ Safety escalation - human must handle</h3><span class="hint">Agent halted</span></div>
        <div class="callout red">Safety rules flagged <strong>${esc(flags.map((f) => f.label).join(", "))}</strong>. The agent will not
          proceed automatically and no work order can be approved until a person confirms the emergency has been handled.
          Review and send the drafted safety instructions below yourself.</div>
        <form class="form-grid" id="escalation-form">
          <div class="two">
            <label><span>Handled by *</span><input name="handler" value="${name}" maxlength="100" placeholder="Your name"></label>
            <label><span>What was done? *</span><input name="note" maxlength="1000" placeholder="e.g. Called tenant, they evacuated; gas supplier attending"></label>
          </div>
          <label class="checkbox"><input type="checkbox" name="safety_confirmed">
            <span>I confirm a human has handled the emergency and the occupants are safe. Repairs can now go to approval.</span></label>
          <div class="inline-error" id="action-error"></div>
          <div class="actions">
            <button type="button" class="btn" disabled title="Blocked until the safety escalation is handled">Approve work order (blocked)</button>
            <button type="submit" class="btn danger">Mark safety handled</button>
          </div>
        </form>
      </div>`;
    }
    case "needs_info":
      return `
      <div class="card warn" id="action-card">
        <div class="card-head"><h3>More information needed</h3><span class="hint">Clarifying question drafted - not sent</span></div>
        <div class="callout amber">The request is too vague to dispatch a tradesperson. Send the drafted question below to the tenant, then record their reply here to re-run triage.</div>
        <form class="form-grid" id="info-form">
          <label><span>Tenant's reply *</span><textarea name="additional_info" rows="3" maxlength="2000" placeholder="e.g. The oven in the kitchen won't turn on at all since yesterday."></textarea></label>
          <div class="inline-error" id="action-error"></div>
          <div class="actions"><button type="submit" class="btn primary">Add info &amp; re-triage</button></div>
        </form>
      </div>`;
    case "awaiting_approval":
      return `
      <div class="card accent" id="action-card">
        <div class="card-head"><h3>Approval required</h3><span class="hint">Nothing runs until you decide</span></div>
        <p class="muted" style="margin:0 0 12px">Approving lets the agent find a ${esc(tradeLabel(t.trade))}, estimate the cost, create the work order,
          draft the contractor message (never sent) and verify the result.</p>
        <form class="form-grid" id="approval-form">
          <div class="two">
            <label><span>Property manager *</span><input name="approver" value="${name}" maxlength="100" placeholder="Your name"></label>
            <label><span>Note <em class="faint">(required to reject)</em></span><input name="note" maxlength="1000" placeholder="Optional for approval"></label>
          </div>
          <div class="fault-box">
            <div class="title">Demo: fault injection</div>
            <label><span class="small muted">Deliberately corrupt the work order to show verification catching it.</span>
              <select name="inject_fault">
                <option value="">No fault - normal run</option>
                ${Object.entries(FAULT_LABELS).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}
              </select>
            </label>
          </div>
          <div class="inline-error" id="action-error"></div>
          <div class="actions">
            <button type="button" class="btn danger" id="reject-btn">Reject</button>
            <button type="submit" class="btn success" id="approve-btn">Approve &amp; create work order</button>
          </div>
        </form>
      </div>`;
    case "verification_failed":
      return `
      <div class="card danger" id="action-card">
        <div class="card-head"><h3>Work order failed verification</h3><span class="hint">Not dispatched</span></div>
        <p class="muted" style="margin:0 0 12px">The verifier blocked this work order. Void it and let the agent rebuild it under the existing approval.</p>
        <form class="form-grid" id="retry-form">
          <label><span>Your name *</span><input name="actor" value="${name}" maxlength="100"></label>
          <div class="inline-error" id="action-error"></div>
          <div class="actions"><button type="submit" class="btn primary">Void &amp; retry</button></div>
        </form>
      </div>`;
    case "rejected": {
      const rej = [...d.approvals].reverse().find((a) => a.decision === "rejected");
      return `<div class="card"><div class="card-head"><h3>Rejected</h3><span class="hint">${esc(fmtTime(rej?.created_at))}</span></div>
        <p class="muted" style="margin:0">Rejected by <strong>${esc(rej?.actor)}</strong>: ${esc(rej?.note)}. No work order was created.</p></div>`;
    }
    default:
      return "";
  }
}

function renderRequestCard(r) {
  return `
  <div class="card">
    <div class="card-head"><h3>Tenant request</h3><span class="hint">Received ${esc(fmtTime(r.created_at))}</span></div>
    <p style="margin:0 0 12px; white-space:pre-wrap">${esc(r.description)}</p>
    <div class="kv">
      <div><div class="k">Tenant</div><div class="v">${esc(r.tenant_name)}</div></div>
      <div><div class="k">Contact</div><div class="v cap">${esc(r.contact_preference)}</div></div>
      <div style="grid-column: span 2"><div class="k">Address</div><div class="v">${esc(r.property_address)}</div></div>
      ${r.photo_note ? `<div style="grid-column: 1 / -1"><div class="k">Photo note</div><div class="v">${esc(r.photo_note)}</div></div>` : ""}
    </div>
  </div>`;
}

function renderTriageCard(t) {
  if (!t) return "";
  const pct = Math.round(t.confidence * 100);
  let engine;
  if (t.engine === "llm") engine = `<span class="engine-tag llm">AI model: ${esc(t.model)}</span>`;
  else if (t.fallback_reason) engine = `<span class="engine-tag rules">Rule-based fallback</span>`;
  else engine = `<span class="engine-tag rules">Rule-based (demo mode, no AI)</span>`;
  const flags = t.safety_flags || [];
  return `
  <div class="card">
    <div class="card-head"><h3>Triage</h3>${engine}</div>
    ${t.fallback_reason ? `<div class="callout amber">${esc(t.fallback_reason)}</div>` : ""}
    ${flags.length ? `<div class="callout red"><strong>Safety override:</strong> ${esc(flags.map((f) => f.label).join(", "))} - forced to emergency escalation.</div>` : ""}
    <div class="kv">
      <div><div class="k">Category</div><div class="v cap">${esc(t.category)}</div></div>
      <div><div class="k">Urgency</div><div class="v">${urgencyChip(t.urgency)}</div></div>
      <div><div class="k">Trade</div><div class="v cap">${esc(tradeLabel(t.trade))}</div></div>
      <div><div class="k">Confidence</div><div class="v">${pct}%</div>
        <div class="conf-bar ${t.confidence < 0.5 ? "low" : ""}"><span style="width:${pct}%"></span></div></div>
    </div>
    ${t.missing_info.length ? `<div class="section-label">Missing information</div>
      <ul class="list-plain">${t.missing_info.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>` : ""}
    <div class="reasoning">${esc(t.reasoning)}</div>
  </div>`;
}

function renderVerificationCard(v) {
  if (!v) return "";
  const ok = v.passed;
  return `
  <div class="card ${ok ? "good" : "danger"}">
    <div class="card-head">
      <div class="verify-banner"><span class="big">${ok ? "✓" : "✕"}</span>${ok ? "Verified" : "Failed verification"}</div>
      <span class="hint">${esc(fmtTime(v.created_at, true))}</span>
    </div>
    <ul class="checks">${v.checks.map((c) => `
      <li class="${c.passed ? "pass" : "fail"}"><span class="icon">${c.passed ? "✓" : "✕"}</span>
        <div><div class="label">${esc(c.label)}</div><div class="detail-text">${esc(c.detail)}</div></div></li>`).join("")}
    </ul>
  </div>`;
}

function renderWorkOrderCard(d) {
  const wo = d.work_order;
  if (!wo) return "";
  const approval = [...d.approvals].reverse().find((a) => a.decision === "approved");
  const contractor = wo.contractor_name
    ? `${esc(wo.contractor_name)}<div class="faint small">${esc(tradeLabel(wo.contractor_trade))} · ${esc(wo.contractor_region)} · ★ ${esc(wo.contractor_rating)}</div>`
    : `<span style="color:var(--red)">None assigned</span>`;
  const earlier = d.work_orders.length > 1 ? `<span class="hint">${d.work_orders.length - 1} earlier work order(s) voided</span>` : "";
  return `
  <div class="card">
    <div class="card-head"><h3>Work order #${wo.id} <span class="chip">${esc(wo.status.replace(/_/g, " "))}</span></h3>${earlier}</div>
    <div class="kv">
      <div><div class="k">Contractor</div><div class="v">${contractor}</div></div>
      <div><div class="k">Trade</div><div class="v cap">${esc(tradeLabel(wo.trade))}</div></div>
      <div><div class="k">SLA</div><div class="v">${wo.sla_hours ? `${wo.sla_hours}h` : '<span style="color:var(--red)">Not set</span>'}
        ${wo.sla_due_at ? `<div class="faint small">due ${esc(fmtTime(wo.sla_due_at))}</div>` : ""}</div></div>
      <div><div class="k">Estimate</div><div class="v">NZD $${esc(wo.cost_low)}-$${esc(wo.cost_high)}</div></div>
      <div><div class="k">Approved by</div><div class="v">${esc(approval?.actor || "-")}</div></div>
      <div><div class="k">Created</div><div class="v">${esc(fmtTime(wo.created_at))}</div></div>
    </div>
  </div>`;
}

function renderMessages(messages) {
  if (!messages.length) return "";
  const kindLabel = {
    contractor_work_order: "Contractor work order",
    tenant_clarification: "Clarifying question to tenant",
    tenant_safety_instructions: "Safety instructions to tenant",
  };
  return `
  <div class="card">
    <div class="card-head"><h3>Drafted messages</h3><span class="hint">Nothing is ever sent from this prototype</span></div>
    ${messages.map((m) => `
      <div class="draft">
        <div class="draft-head">
          <span><strong>${esc(kindLabel[m.kind] || m.kind)}</strong> → ${esc(m.recipient)} (${esc(m.channel)})</span>
          <span class="draft-stamp">${esc(m.status)}</span>
        </div>
        <pre>${esc(m.body)}</pre>
      </div>`).join("")}
  </div>`;
}

function renderTimeline(events) {
  return `
  <div class="card">
    <div class="card-head"><h3>Audit trail</h3><span class="hint">${events.length} events · persisted in SQLite</span></div>
    <ol class="timeline">${events.map((e) => {
      const failed = e.kind === "verification" && e.data && e.data.passed === false;
      const data = e.data ? `<details><summary>${e.kind === "tool_call" ? "Tool input / output" : "Details"}</summary><pre>${esc(JSON.stringify(e.data, null, 2))}</pre></details>` : "";
      return `<li class="tl-item k-${esc(e.kind)} ${failed ? "failed" : ""}">
        <span class="tl-dot"></span>
        <div class="tl-meta"><span>${esc(fmtTime(e.ts, true))}</span><span class="tl-kind">${esc(e.kind)}</span><span>${esc(e.actor)}</span></div>
        <div class="tl-summary">${esc(e.summary)}</div>${data}
      </li>`;
    }).join("")}</ol>
  </div>`;
}

function renderDetail(d) {
  const r = d.request;
  const t = d.triage || {};
  $("#detail").innerHTML = `
    <div class="detail-head">
      <div>
        <h1>#${r.id} · ${esc(cap(t.category || "request"))} - ${esc(r.tenant_name)}</h1>
        <div class="sub"><span>${esc(r.property_address)}</span><span>Updated ${esc(fmtTime(r.updated_at))}</span></div>
      </div>
      <div class="chips">${statusChip(r.status)}${urgencyChip(t.urgency)}</div>
    </div>
    ${renderStepper(d)}
    <div class="detail-grid">
      <div class="col">
        ${renderActionCard(d)}
        ${renderVerificationCard(d.verification)}
        ${renderWorkOrderCard(d)}
        ${renderTriageCard(d.triage)}
        ${renderRequestCard(r)}
        ${renderMessages(d.messages)}
      </div>
      <div class="col">${renderTimeline(d.events)}</div>
    </div>`;
  bindActions(d);
}

// ------------------------------------------------------------------ actions

function showActionError(msg) {
  const el = $("#action-error");
  if (el) el.textContent = msg;
}

function rememberName(name) {
  if (name && name.trim()) localStorage.setItem("approverName", name.trim());
}

function bindActions(d) {
  const id = d.request.id;

  const approval = $("#approval-form");
  if (approval) {
    approval.addEventListener("submit", (e) => {
      e.preventDefault();
      const f = new FormData(approval);
      const body = { approver: f.get("approver"), note: f.get("note") || null, inject_fault: f.get("inject_fault") || null };
      rememberName(body.approver);
      withBusy($("#approve-btn"), async () => {
        try {
          const res = await api(`/api/requests/${id}/approve`, { method: "POST", body });
          await refreshAfterAction(res);
          const ok = res.request.status === "verified";
          toast(ok ? "Approved - work order created and verified" : "Approved - but the work order FAILED verification", ok ? "success" : "error");
        } catch (err) { showActionError(errorText(err)); }
      });
    });
    $("#reject-btn").addEventListener("click", () => {
      const f = new FormData(approval);
      const body = { approver: f.get("approver"), note: f.get("note") };
      if (!String(body.note || "").trim()) { showActionError("Add a note explaining why the request is rejected."); return; }
      rememberName(body.approver);
      withBusy($("#reject-btn"), async () => {
        try {
          const res = await api(`/api/requests/${id}/reject`, { method: "POST", body });
          await refreshAfterAction(res);
          toast("Request rejected - no work order created");
        } catch (err) { showActionError(errorText(err)); }
      });
    });
  }

  const escalation = $("#escalation-form");
  if (escalation) {
    escalation.addEventListener("submit", (e) => {
      e.preventDefault();
      const f = new FormData(escalation);
      const body = { handler: f.get("handler"), note: f.get("note"), safety_confirmed: f.get("safety_confirmed") === "on" };
      rememberName(body.handler);
      withBusy(escalation.querySelector("button[type=submit]"), async () => {
        try {
          const res = await api(`/api/requests/${id}/escalation/handle`, { method: "POST", body });
          await refreshAfterAction(res);
          toast("Safety handling recorded - request moved to approval", "success");
        } catch (err) { showActionError(errorText(err)); }
      });
    });
  }

  const info = $("#info-form");
  if (info) {
    info.addEventListener("submit", (e) => {
      e.preventDefault();
      const body = { additional_info: new FormData(info).get("additional_info") };
      withBusy(info.querySelector("button[type=submit]"), async () => {
        try {
          const res = await api(`/api/requests/${id}/info`, { method: "POST", body });
          await refreshAfterAction(res);
          toast(`Re-triaged: ${label(res.request.status)}`, "success");
        } catch (err) { showActionError(errorText(err)); }
      });
    });
  }

  const retry = $("#retry-form");
  if (retry) {
    retry.addEventListener("submit", (e) => {
      e.preventDefault();
      const body = { actor: new FormData(retry).get("actor") };
      rememberName(body.actor);
      withBusy(retry.querySelector("button[type=submit]"), async () => {
        try {
          const res = await api(`/api/requests/${id}/retry`, { method: "POST", body });
          await refreshAfterAction(res);
          const ok = res.request.status === "verified";
          toast(ok ? "Rebuilt work order passed verification" : "Retry failed verification again", ok ? "success" : "error");
        } catch (err) { showActionError(errorText(err)); }
      });
    });
  }
}

// ------------------------------------------------------------------ new request dialog

function setupDialog() {
  const dialog = $("#new-request-dialog");
  const form = $("#new-request-form");
  const sample = $("#sample-select");

  $("#new-request-btn").addEventListener("click", () => {
    clearFormErrors(form);
    dialog.showModal();
    form.elements.tenant_name.focus();
  });
  $$("[data-close]", dialog).forEach((b) => b.addEventListener("click", () => dialog.close()));

  sample.addEventListener("change", () => {
    const s = (state.meta?.samples || []).find((x) => x.key === sample.value);
    if (!s) { form.reset(); return; }
    ["tenant_name", "property_address", "description", "photo_note", "contact_preference"].forEach((k) => {
      form.elements[k].value = s[k] || (k === "contact_preference" ? "email" : "");
    });
    clearFormErrors(form);
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    clearFormErrors(form);
    const f = new FormData(form);
    const body = {
      tenant_name: f.get("tenant_name"),
      property_address: f.get("property_address"),
      description: f.get("description"),
      photo_note: String(f.get("photo_note") || "").trim() || null,
      contact_preference: f.get("contact_preference"),
    };
    withBusy($("#submit-request"), async () => {
      try {
        const res = await api("/api/requests", { method: "POST", body });
        dialog.close();
        form.reset();
        sample.value = "";
        state.filters.status = "";
        await loadList();
        await selectRequest(res.request.id);
        toast(`Request #${res.request.id} triaged → ${label(res.request.status)}`, "success");
      } catch (err) {
        if (err.errors && err.errors.length) {
          err.errors.forEach((x) => {
            const slot = form.querySelector(`[data-error-for="${x.field}"]`);
            const input = form.elements[x.field];
            if (slot) slot.textContent = x.message;
            if (input) input.classList.add("invalid");
          });
          $("#form-error").textContent = "Please fix the highlighted fields.";
        } else {
          $("#form-error").textContent = errorText(err);
        }
      }
    });
  });
}

function clearFormErrors(form) {
  $$(".field-error", form).forEach((el) => { el.textContent = ""; });
  $$(".invalid", form).forEach((el) => el.classList.remove("invalid"));
  $("#form-error").textContent = "";
}

// ------------------------------------------------------------------ init

function setupFilters() {
  let timer;
  $("#search").addEventListener("input", (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.filters.q = e.target.value.trim(); loadList(); }, 250);
  });
  $("#urgency-filter").addEventListener("change", (e) => { state.filters.urgency = e.target.value; loadList(); });
  $("#category-filter").addEventListener("change", (e) => { state.filters.category = e.target.value; loadList(); });
  $("#reset-demo").addEventListener("click", async () => {
    if (!confirm("Delete all requests and audit logs, then re-create the 5 sample requests?")) return;
    try {
      await api("/api/demo/reset", { method: "POST" });
      state.selectedId = null;
      $("#detail").innerHTML = `<div class="empty-state"><h2>Demo data reset</h2><p>Five sample requests were re-created and triaged.</p></div>`;
      await loadList();
      toast("Demo data reset", "success");
    } catch (err) { toast(errorText(err), "error"); }
  });
}

async function init() {
  $("#request-list").innerHTML = `<li class="skeleton"></li><li class="skeleton"></li><li class="skeleton"></li>`;
  setupDialog();
  setupFilters();
  loadStatus();
  try {
    state.meta = await api("/api/meta");
    $("#sample-select").insertAdjacentHTML("beforeend", state.meta.samples.map(
      (s) => `<option value="${esc(s.key)}">${esc(s.key.replace(/_/g, " "))} - ${esc(s.description.slice(0, 40))}…</option>`
    ).join(""));
  } catch { /* samples are optional */ }
  await loadList();
}

init();
