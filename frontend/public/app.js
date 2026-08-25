/* GST Invoice Automation - frontend.
 *
 * Four screens over the backend API: Inbox, Review, Registers, Tax position.
 *
 * The backend is a separate server on its own origin, so every call here is
 * cross-origin and absolute. Its address comes from /config.js, which the
 * frontend server generates at request time - pointing this at a different
 * backend is a restart of that server, not an edit to this file.
 */

const API = (window.GST_API_BASE || "http://127.0.0.1:8000").replace(/\/$/, "");
const url = (path) => `${API}${path}`;

const state = {
  info: null,
  documents: [],
  screen: "inbox",
  period: null,
  reviewId: null,
  register: "sales",
  filter: "",
  selected: new Set(),
  editing: false,
  busy: false,
};

// --------------------------------------------------------------------------- //
// Tiny DOM helpers
// --------------------------------------------------------------------------- //

const $ = (sel) => document.querySelector(sel);

function el(tag, props = {}, children = []) {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of [].concat(children)) {
    if (child == null || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

/* Inline icons. Status is never carried by colour alone - every pill and alert
   pairs its colour with one of these and a word. */
const ICON = {
  check: "M2.5 8.5l3.5 3.5 7.5-8",
  alert: "M8 5v4M8 11.5v.01M8 1.5 15 14H1z",
  info: "M8 7.5v4M8 4.5v.01M14.5 8a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0z",
  clock: "M8 4.5V8l2.5 1.5M14.5 8a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0z",
  dot: "M8 5.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z",
};

function icon(name, size = 14) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.6");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICON[name] || ICON.dot);
  svg.append(path);
  return svg;
}

// --------------------------------------------------------------------------- //
// Formatting
// --------------------------------------------------------------------------- //

const money = (value) =>
  "₹" + Number(value || 0).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const moneyRound = (value) => "₹" + Math.round(Number(value || 0)).toLocaleString("en-IN");

/* GST is filed in whole rupees, so the headline figure drops the paise. */

const STATUS = {
  new:          { label: "New",            icon: "clock" },
  needs_review: { label: "Needs a look",   icon: "alert" },
  ready:        { label: "Ready to post",  icon: "check" },
  posted:       { label: "Posted",         icon: "check" },
  failed:       { label: "Failed",         icon: "alert" },
};

const TYPE_LABEL = {
  sales: "Sale", purchase: "Purchase", credit_note: "Credit note", rcm: "Reverse charge",
};
const REGISTER_SHEET = {
  sales: "GSTR-1", purchase: "GSTR-2B", credit_note: "Credit Note", rcm: "RCM",
};
const REGISTERS = [
  ["sales", "Sales", "GSTR-1"],
  ["purchase", "Purchases", "GSTR-2B"],
  ["credit_note", "Credit notes", "Credit Note"],
  ["rcm", "Reverse charge", "RCM"],
];

function statusPill(status) {
  const meta = STATUS[status] || { label: status, icon: "dot" };
  return el("span", { className: `pill ${status}` }, [icon(meta.icon, 12), meta.label]);
}

function alertBox(kind, children) {
  const map = { error: "alert", warning: "alert", good: "check", info: "info" };
  return el("div", { className: `alert ${kind}` }, [
    icon(map[kind] || "info", 16),
    el("div", {}, children),
  ]);
}

// --------------------------------------------------------------------------- //
// API
// --------------------------------------------------------------------------- //

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(url(path), options);
  } catch (_) {
    // A separate backend can be down while this page is perfectly alive.
    throw new Error(`Cannot reach the backend at ${API}. Is it running?`);
  }
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

let toastTimer;
function toast(message, kind = "") {
  const node = $("#toast");
  node.textContent = message;
  node.className = `show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.className = ""; }, kind === "error" ? 7000 : 3200);
}

// --------------------------------------------------------------------------- //
// Loading
// --------------------------------------------------------------------------- //

async function loadInfo() {
  state.info = await api("/api/info");
  $("#brand-name").textContent = state.info.company;
  $("#brand-gstin").textContent = state.info.gstin;

  const offline = state.info.reader !== "claude";
  $("#reader-dot").className = `dot${offline ? " warn" : ""}`;
  $("#reader-label").textContent = offline ? "Offline reader" : "Claude reader";
  $("#reader-label").title = offline
    ? "No API key configured. Text-layer PDFs are read in full; scans cannot be read. "
      + "Add ANTHROPIC_API_KEY to backend/.env — no restart needed."
    : `Reading with ${state.info.model}.`;

  renderPeriods();
}

function renderPeriods() {
  const picker = $("#period-picker");
  const known = state.info.periods || [];
  if (!state.period || !known.includes(state.period)) {
    state.period = known[0] || state.info.master_period;
  }
  picker.textContent = "";
  for (const period of known) {
    picker.append(el("option", { value: period, selected: period === state.period, textContent: period }));
  }
  if (!known.length) picker.append(el("option", { textContent: state.period || "—" }));
}

async function loadDocuments() {
  state.documents = await api("/api/documents");
  for (const id of [...state.selected]) {
    if (!state.documents.some((d) => d.id === id && d.status === "ready")) state.selected.delete(id);
  }
  const counts = tally();
  setPip("#pip-inbox", counts.open, counts.needs_review ? "attention" : "ready");
  setPip("#pip-review", counts.needs_review + counts.ready, counts.needs_review ? "attention" : "ready");
}

function tally() {
  const c = { new: 0, needs_review: 0, ready: 0, posted: 0, failed: 0 };
  for (const doc of state.documents) c[doc.status] = (c[doc.status] || 0) + 1;
  c.open = c.new + c.needs_review + c.ready + c.failed;
  return c;
}

function setPip(sel, value, kind) {
  const node = $(sel);
  node.textContent = value || "";
  node.dataset.zero = String(!value);
  node.className = `pip ${value ? kind : ""}`;
}

// --------------------------------------------------------------------------- //
// Screen 1: Inbox
// --------------------------------------------------------------------------- //

function renderSummary() {
  const counts = tally();
  const box = $("#summary");
  box.textContent = "";

  const cards = [
    ["needs_review", "Needs a look", counts.needs_review, "alert"],
    ["ready", "Ready to post", counts.ready, "check"],
    ["posted", "Posted", counts.posted, "check"],
    ["", "All documents", state.documents.length, "info"],
  ];
  for (const [filter, label, value, ic] of cards) {
    box.append(el("button", {
      "aria-pressed": String(state.filter === filter),
      onclick: () => { state.filter = state.filter === filter ? "" : filter; renderInbox(); },
    }, [
      el("span", { className: "k" }, [icon(ic, 13), label]),
      el("span", { className: "v", textContent: String(value) }),
    ]));
  }
}

/* Documents that arrived inside one print run stay together, so 18 invoices
   from one upload read as one delivery rather than 18 unrelated rows. */
function groupDocuments(docs) {
  const groups = new Map();
  for (const doc of docs) {
    const key = doc.source_document || doc.filename;
    if (!groups.has(key)) groups.set(key, { key, solo: !doc.source_document, docs: [] });
    groups.get(key).docs.push(doc);
  }
  for (const group of groups.values()) {
    group.docs.sort((a, b) => (a.position || 0) - (b.position || 0));
    group.solo = group.docs.length === 1 && !group.docs[0].source_document;
  }
  return [...groups.values()];
}

function documentRow(doc) {
  const t = doc.treatment || {};
  const e = doc.extracted || {};
  const party = t.counterparty_name || "Unread";
  const errors = (doc.issues || []).filter((i) => i.severity === "error").length;

  const sub = [];
  if (e.invoice_number) sub.push(e.invoice_number);
  if (doc.status === "posted" && doc.sheet) sub.push(`${doc.sheet} row ${doc.row}`);
  else if (errors) sub.push(`${errors} thing${errors > 1 ? "s" : ""} to check`);
  else if (doc.page_label) sub.push(doc.page_label);

  const acts = el("div", { className: "acts" });
  if (doc.status !== "posted") {
    acts.append(el("button", { className: "btn sm ghost", textContent: "Open", onclick: () => openReview(doc.id) }));
  }
  if (doc.status === "ready") {
    acts.append(el("button", { className: "btn sm primary", textContent: "Post", onclick: () => postOne(doc.id) }));
  }
  if (doc.status === "posted") {
    acts.append(el("button", { className: "btn sm ghost", textContent: "Un-post", onclick: () => unpostOne(doc.id) }));
  } else {
    acts.append(el("button", { className: "btn sm danger", textContent: "Remove", onclick: () => removeOne(doc.id) }));
  }

  const box = el("input", {
    type: "checkbox",
    checked: state.selected.has(doc.id),
    disabled: doc.status !== "ready",
    title: doc.status === "ready" ? "Select for posting" : "Only documents ready to post can be selected",
    onchange: (ev) => {
      if (ev.target.checked) state.selected.add(doc.id); else state.selected.delete(doc.id);
      renderBulkbar();
    },
  });

  return el("div", { className: "row" }, [
    box,
    el("div", { className: "who" }, [
      el("div", { className: "party", textContent: party }),
      el("div", { className: "sub", textContent: sub.join(" · ") || doc.filename }),
    ]),
    el("div", { className: "when", style: "font-size:.8rem;color:var(--ink-3)" },
      e.invoice_date || (doc.period ? `files against ${doc.period}` : "")),
    el("span", { className: "kind tag", textContent: TYPE_LABEL[t.document_type] || "Unread" }),
    el("div", { className: "amount" }, [
      t.invoice_total != null ? money(t.invoice_total) : "—",
      doc.period ? el("span", { className: "sub", textContent: doc.period }) : null,
    ]),
    statusPill(doc.status),
    acts,
  ]);
}

function renderInbox() {
  renderSummary();
  const list = $("#inbox-list");
  list.textContent = "";

  const docs = state.documents.filter((d) => !state.filter || d.status === state.filter);
  $("#dropzone").classList.toggle("compact", state.documents.length > 0);

  if (!docs.length) {
    list.append(el("div", { className: "card" }, el("div", { className: "empty" }, [
      el("div", { className: "big" },
        state.documents.length ? "Nothing here right now" : "No invoices yet"),
      state.documents.length
        ? "No documents match this filter."
        : "Drop a PDF above — a single invoice or a whole month's print run.",
    ])));
    renderBulkbar();
    return;
  }

  for (const group of groupDocuments(docs)) {
    const readyIds = group.docs.filter((d) => d.status === "ready").map((d) => d.id);
    const head = el("div", { className: "head" }, [
      el("span", { className: "name", textContent: group.key }),
      el("span", { textContent: `${group.docs.length} invoices` }),
      el("span", { className: "grow" }),
      readyIds.length
        ? el("button", {
            className: "btn sm ghost",
            textContent: `Select ${readyIds.length} ready`,
            onclick: () => { readyIds.forEach((id) => state.selected.add(id)); renderInbox(); renderBulkbar(); },
          })
        : null,
    ]);
    const rows = el("div", { className: "rows" }, group.docs.map(documentRow));
    list.append(el("div", { className: `group${group.solo ? " solo" : ""}` }, [head, rows]));
  }
  renderBulkbar();
}

function renderBulkbar() {
  const bar = $("#bulkbar");
  bar.textContent = "";
  if (!state.selected.size) return;
  bar.append(el("div", { className: "bulkbar" }, [
    icon("check", 16),
    el("strong", { textContent: `${state.selected.size} selected` }),
    el("span", { className: "grow" }),
    el("button", { className: "btn sm", textContent: "Clear", onclick: () => { state.selected.clear(); renderInbox(); } }),
    el("button", {
      className: "btn sm primary", disabled: state.busy,
      textContent: `Post ${state.selected.size} to the workbook`,
      onclick: postSelected,
    }),
  ]));
}

// --------------------------------------------------------------------------- //
// Screen 2: Review
// --------------------------------------------------------------------------- //

/* Fields a reviewer may correct. The tax treatment is never among them - it is
   always re-derived by the backend from these. */
const FIELDS = [
  ["invoice_number", "Invoice no.", "text"],
  ["invoice_date", "Invoice date", "text"],
  ["supplier_name", "Supplier", "text"],
  ["supplier_gstin", "Supplier GSTIN", "text"],
  ["recipient_name", "Customer", "text"],
  ["recipient_gstin", "Customer GSTIN", "text"],
  ["place_of_supply", "Place of supply", "text"],
  ["hsn_sac", "HSN / SAC", "text"],
  ["quantity", "Quantity", "number"],
  ["taxable_value", "Taxable value", "number"],
  ["gst_rate_percent", "Rate %", "number"],
  ["cgst_amount", "CGST on document", "number"],
  ["sgst_amount", "SGST on document", "number"],
  ["igst_amount", "IGST on document", "number"],
  ["total_amount", "Total on document", "number"],
  ["reverse_charge", "Reverse charge", "bool"],
  ["is_credit_note", "Credit note", "bool"],
];

function queue() {
  return state.documents.filter((d) => d.status !== "posted");
}

function openReview(docId) {
  state.reviewId = docId;
  state.editing = false;
  show("review");
}

function renderReview() {
  const body = $("#review-body");
  body.textContent = "";

  const pending = queue();
  const doc = pending.find((d) => d.id === state.reviewId) || pending[0];
  if (!doc) {
    body.append(el("div", { className: "card" }, el("div", { className: "empty" }, [
      el("div", { className: "big" }, "Nothing waiting"),
      "Every invoice captured has been posted. Check the Registers to see them.",
    ])));
    return;
  }
  state.reviewId = doc.id;

  const index = pending.indexOf(doc);
  const t = doc.treatment || {};
  const e = doc.extracted || {};
  const issues = doc.issues || [];
  const blocking = issues.filter((i) => i.severity === "error");

  // ---- Position within the queue -----------------------------------------
  body.append(el("div", { className: "review-head" }, [
    el("button", {
      className: "btn sm ghost", textContent: "← Previous", disabled: index === 0,
      onclick: () => { state.reviewId = pending[index - 1].id; state.editing = false; renderReview(); },
    }),
    el("span", { className: "pos", textContent: `${index + 1} of ${pending.length}` }),
    el("div", { className: "progress" }, el("i", { style: `width:${((index + 1) / pending.length) * 100}%` })),
    el("button", {
      className: "btn sm ghost", textContent: "Next →", disabled: index >= pending.length - 1,
      onclick: () => { state.reviewId = pending[index + 1].id; state.editing = false; renderReview(); },
    }),
  ]));

  // ---- What needs attention ----------------------------------------------
  if (blocking.length) {
    for (const issue of blocking) body.append(alertBox("error", issue.message));
  } else if (issues.length) {
    for (const issue of issues) body.append(alertBox("warning", issue.message));
  } else {
    body.append(alertBox("good", [
      el("strong", { textContent: "Every check passed. " }),
      "GSTIN verified, tax arithmetic reconciled against the document, no duplicate in this return.",
    ]));
  }

  // ---- Document ------------------------------------------------------------
  const isPdf = /\.pdf$/i.test(doc.filename);
  const isImage = /\.(png|jpe?g|webp|gif)$/i.test(doc.filename);
  let viewer;
  if (isPdf) {
    viewer = el("iframe", { className: "doc-frame", src: url(`/api/documents/${doc.id}/file#view=FitH`), title: doc.filename });
  } else if (isImage) {
    viewer = el("img", { className: "doc-frame", style: "object-fit:contain", src: url(`/api/documents/${doc.id}/file`), alt: doc.filename });
  } else {
    viewer = el("div", { className: "doc-missing" }, [
      icon("info", 22), doc.filename,
      el("a", { className: "btn sm", href: url(`/api/documents/${doc.id}/file`), target: "_blank", textContent: "Open file" }),
    ]);
  }

  const provenance = doc.source_document
    ? `${doc.source_document} · ${doc.page_label} · ${doc.position} of ${doc.of}`
    : doc.filename;

  const left = el("div", { className: "card doc-pane" }, [
    el("header", {}, [
      el("h2", { textContent: "The invoice" }),
      el("span", { className: "grow" }),
      // Some browsers refuse to render a PDF inside an iframe; the tab always works.
      el("a", {
        className: "btn sm ghost", href: url(`/api/documents/${doc.id}/file`),
        target: "_blank", rel: "noopener", textContent: "Open in a tab",
      }),
    ]),
    viewer,
    el("div", { style: "padding:.5rem .9rem;font-size:.78rem;color:var(--ink-3);border-top:1px solid var(--line)" },
      provenance),
  ]);

  // ---- The decision --------------------------------------------------------
  const interState = t.supply_type === "inter_state";
  const right = el("div", { className: "card" }, [
    el("header", {}, [
      el("h2", { textContent: "What the system made of it" }),
      el("span", { className: "grow" }),
      el("span", { className: "sub", textContent: doc.reader === "claude" ? "read by Claude" : "read offline" }),
    ]),

    el("div", { className: "verdict" }, [
      el("div", { className: "route" }, [
        icon("check", 16),
        `${TYPE_LABEL[t.document_type] || "—"} → ${REGISTER_SHEET[t.document_type] || "—"}`,
        doc.period ? el("span", { className: "tag", style: "margin-left:.3rem", textContent: doc.period }) : null,
      ]),
      el("div", { className: "why" }, [
        interState
          ? `IGST — supplier in ${t.supplier_state_name || "?"}, supplied to ${t.place_of_supply_name || "?"}`
          : `CGST + SGST — both sides in ${t.place_of_supply_name || "?"}`,
        t.supply_category ? ` · ${t.supply_category}` : "",
      ]),
    ]),

    el("table", { className: "amounts" }, el("tbody", {}, [
      row2("Taxable value", money(t.taxable_value)),
      row2("Rate", `${(Number(t.rate || 0) * 100).toFixed(2)}%`),
      ...(interState
        ? [row2("IGST", money(t.igst))]
        : [row2("CGST", money(t.cgst)), row2("SGST", money(t.sgst))]),
      ...(Number(t.cess) ? [row2("Cess", money(t.cess))] : []),
      el("tr", { className: "total" }, [
        el("td", { textContent: "Invoice total" }),
        el("td", { textContent: money(t.invoice_total) }),
      ]),
    ])),

    fieldsBlock(doc, e),

    el("div", { className: "actions-bar" }, [
      el("button", {
        className: "btn primary", disabled: state.busy,
        textContent: blocking.length ? "Post anyway" : "Post & next",
        onclick: () => postFromReview(doc.id, blocking.length > 0),
      }),
      el("button", { className: "btn", textContent: "Re-read", onclick: () => reread(doc.id) }),
      el("span", { className: "grow" }),
      el("button", { className: "btn ghost", textContent: "Skip", disabled: index >= pending.length - 1,
        onclick: () => { state.reviewId = pending[index + 1].id; state.editing = false; renderReview(); } }),
    ]),
  ]);

  body.append(el("div", { className: "review-grid" }, [left, right]));
}

function row2(label, value) {
  return el("tr", {}, [el("td", { textContent: label }), el("td", { textContent: value })]);
}

/* Read-only by default. Most invoices need no correction at all, and 17 input
   boxes is a wall to read past when you only want to confirm the total. */
function fieldsBlock(doc, extracted) {
  const header = el("div", { className: "actions-bar", style: "border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:var(--surface)" }, [
    el("strong", { style: "font-size:.85rem", textContent: "What we read off the page" }),
    el("span", { className: "grow" }),
    el("button", {
      className: "btn sm ghost",
      textContent: state.editing ? "Done editing" : "Correct something",
      onclick: async () => {
        if (state.editing) { await saveEdits(); } else { state.editing = true; renderReview(); }
      },
    }),
  ]);

  const grid = el("div", { className: "fields" });
  state.inputs = {};

  for (const [key, label, kind] of FIELDS) {
    const value = extracted[key];
    let control;
    if (!state.editing) {
      const shown = kind === "bool" ? (value ? "Yes" : "No")
        : value == null || value === "" ? "not on the document" : String(value);
      control = el("div", { className: `v${value == null || value === "" ? " blank" : ""}`, textContent: shown, title: shown });
    } else if (kind === "bool") {
      control = el("select", {});
      control.append(el("option", { value: "false", textContent: "No" }), el("option", { value: "true", textContent: "Yes" }));
      control.value = value ? "true" : "false";
      state.inputs[key] = { control, kind };
    } else {
      control = el("input", {
        type: kind === "number" ? "number" : "text",
        step: kind === "number" ? "0.01" : undefined,
        value: value == null ? "" : value,
        placeholder: "—",
      });
      state.inputs[key] = { control, kind };
    }
    grid.append(el("div", { className: "f" }, [el("div", { className: "k", textContent: label }), control]));
  }

  const notes = extracted.notes
    ? el("div", { className: "fields wide", style: "padding-top:0" },
        el("div", { className: "f", style: "border:none" }, [
          el("div", { className: "k", textContent: "Reader's note" }),
          el("div", { className: "v", style: "white-space:normal", textContent: extracted.notes }),
        ]))
    : null;

  return el("div", {}, [header, grid, notes]);
}

async function saveEdits() {
  const edits = {};
  for (const [key, meta] of Object.entries(state.inputs || {})) {
    const raw = meta.control.value;
    if (meta.kind === "bool") edits[key] = raw === "true";
    else if (meta.kind === "number") edits[key] = raw === "" ? null : Number(raw);
    else edits[key] = raw.trim() === "" ? null : raw.trim();
  }
  try {
    await api(`/api/documents/${state.reviewId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(edits),
    });
    state.editing = false;
    await loadDocuments();
    renderReview();
    toast("Recalculated with your corrections.", "good");
  } catch (err) {
    toast(err.message, "error");
  }
}

// --------------------------------------------------------------------------- //
// Actions
// --------------------------------------------------------------------------- //

async function postOne(docId, override = false) {
  const suffix = override ? "?override=true" : "";
  await api(`/api/documents/${docId}/confirm${suffix}`, { method: "POST" });
}

async function postFromReview(docId, wasBlocked) {
  if (state.editing) await saveEdits();

  // A correction may have cleared the very issue that blocked this invoice.
  const fresh = state.documents.find((d) => d.id === docId);
  const needsOverride = Boolean(
    wasBlocked && (fresh?.issues || []).some((i) => i.severity === "error"),
  );

  if (needsOverride) {
    const ok = confirm(
      "This invoice did not pass every check.\n\n"
      + "Posting it anyway is recorded on its history so an auditor can see the decision was deliberate.\n\n"
      + "Post it?"
    );
    if (!ok) return;
  }
  const pending = queue();
  const index = pending.findIndex((d) => d.id === docId);
  const next = pending[index + 1];

  state.busy = true;
  try {
    await postOne(docId, needsOverride);
    toast("Posted to the workbook.", "good");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    state.busy = false;
  }
  state.reviewId = next ? next.id : null;
  await refresh();
}

async function postSelected() {
  const ids = [...state.selected];
  state.busy = true;
  renderBulkbar();
  let posted = 0;
  const failures = [];
  for (const id of ids) {
    try { await postOne(id); posted += 1; state.selected.delete(id); }
    catch (err) {
      const doc = state.documents.find((d) => d.id === id);
      failures.push(`${(doc && doc.extracted && doc.extracted.invoice_number) || "one invoice"}: ${err.message}`);
    }
  }
  state.busy = false;
  if (posted) toast(`Posted ${posted} row${posted > 1 ? "s" : ""} to the workbook.`, "good");
  if (failures.length) toast(failures[0], "error");
  await refresh();
}

async function unpostOne(docId) {
  if (!confirm("Clear this row from the workbook and send the invoice back for review?")) return;
  try { await api(`/api/documents/${docId}/unpost`, { method: "POST" }); toast("Row cleared."); }
  catch (err) { toast(err.message, "error"); }
  await refresh();
}

async function removeOne(docId) {
  if (!confirm("Remove this invoice from the queue?")) return;
  try { await api(`/api/documents/${docId}`, { method: "DELETE" }); }
  catch (err) { toast(err.message, "error"); }
  await refresh();
}

async function reread(docId) {
  toast("Reading the invoice again…");
  try {
    await api(`/api/documents/${docId}/reprocess`, { method: "POST" });
    await loadDocuments();
    renderReview();
    toast("Re-read complete.", "good");
  } catch (err) { toast(err.message, "error"); }
}

async function upload(files) {
  if (!files.length) return;
  const form = new FormData();
  for (const file of files) form.append("files", file);
  toast(`Reading ${files.length} file${files.length > 1 ? "s" : ""}…`);
  try {
    const result = await api("/api/documents", { method: "POST", body: form });
    const n = result.captured.length;
    if (result.errors && result.errors.length) toast(result.errors.join("; "), "error");
    else toast(`Captured ${n} invoice${n > 1 ? "s" : ""}.`, "good");
  } catch (err) {
    toast(err.message, "error");
  }
  await refresh();
}

// --------------------------------------------------------------------------- //
// Screen 3: Registers
// --------------------------------------------------------------------------- //

async function renderRegisters() {
  const body = $("#registers-body");
  body.textContent = "";

  const tabs = el("div", { style: "display:flex;gap:.4rem;flex-wrap:wrap;margin-bottom:.9rem" },
    REGISTERS.map(([key, label, sheet]) => el("button", {
      className: `btn sm${state.register === key ? " primary" : ""}`,
      textContent: label,
      title: sheet,
      onclick: () => { state.register = key; renderRegisters(); },
    })));
  body.append(tabs);

  let data;
  try {
    data = await api(`/api/registers/${state.register}?period=${encodeURIComponent(state.period)}`);
  } catch (err) {
    body.append(alertBox("error", err.message));
    return;
  }

  const card = el("div", { className: "card" }, el("header", {}, [
    el("h2", { textContent: `${data.sheet} · ${data.period}` }),
    el("span", { className: "sub", textContent: `${data.rows.length} row${data.rows.length === 1 ? "" : "s"}` }),
  ]));

  if (!data.rows.length) {
    card.append(el("div", { className: "empty" }, [
      el("div", { className: "big" }, "No rows yet"),
      "Post an invoice from Review and it appears here, in your workbook.",
    ]));
  } else {
    const numeric = new Set(data.columns.filter((c) => /value|amount|tax|igst|cgst|sgst|cess|total|rate|bags|qty|quantity/i.test(c)));
    const thead = el("thead", {}, el("tr", {}, [
      el("th", { textContent: "Row" }),
      ...data.columns.map((c) => el("th", { className: numeric.has(c) ? "num" : "", textContent: c })),
    ]));
    const tbody = el("tbody", {}, data.rows.map((r) => el("tr", {}, [
      el("td", { style: "color:var(--ink-3)", textContent: r.row }),
      ...data.columns.map((c) => el("td", { className: numeric.has(c) ? "num" : "", textContent: r.values[c] ?? "" })),
    ])));
    card.append(el("div", { className: "table-scroll" }, el("table", {}, [thead, tbody])));
  }
  body.append(card);
}

// --------------------------------------------------------------------------- //
// Screen 4: Tax position
// --------------------------------------------------------------------------- //

const sum3 = (o) => Number(o.igst || 0) + Number(o.cgst || 0) + Number(o.sgst || 0);

async function renderTax() {
  const body = $("#tax-body");
  body.textContent = "";

  let data;
  try {
    data = await api(`/api/tax-payable?period=${encodeURIComponent(state.period)}`);
  } catch (err) {
    body.append(alertBox("error", err.message));
    return;
  }

  const output = sum3(data.output_tax);
  const credit = sum3(data.itc_available);
  const afterCredit = sum3(data.net_payable);
  const rcmCash = sum3(data.rcm_cash_payable);
  const dueNow = afterCredit + rcmCash;

  if (data.opening_credit_unset) {
    body.append(alertBox("warning", [
      el("strong", { textContent: `No opening credit entered for ${data.return_period}. ` }),
      "This period's workbook was created fresh, so its credit carried forward is zero. "
      + "The real figure is the closing balance of the previous return — put it in the Tax Payable "
      + "sheet (cells E6, F6, G6) before filing. Until then the amount below is overstated.",
    ]));
  }

  // The one number this screen leads with.
  body.append(el("div", { className: "hero" }, [
    el("div", { className: "k", textContent: `Payable in cash for ${data.return_period}` }),
    el("div", { className: "v", textContent: moneyRound(dueNow) }),
    el("div", { className: "note" },
      rcmCash
        ? `${money(afterCredit)} after input credit, plus ${money(rcmCash)} reverse-charge tax, which cannot be set off.`
        : "After setting off the input tax credit available."),
  ]));

  body.append(el("div", { className: "tiles" }, [
    tile("Output tax on sales", money(output), `From ${REGISTER_SHEET.sales} for this period`),
    tile("Input credit available", money(credit), "Carried forward, plus this month's purchases"),
    tile("Reverse charge", money(rcmCash), "Always paid in cash, never set off"),
  ]));

  // One ratio against a limit: how much of the output tax the credit covers.
  const covered = output > 0 ? Math.min(1, credit / output) : (credit > 0 ? 1 : 0);
  const short = covered < 1;
  body.append(el("div", { className: "card", style: "margin-bottom:.9rem" }, [
    el("header", {}, el("h2", { textContent: "How much of the output tax your credit covers" })),
    el("div", { className: "body" }, [
      el("div", { className: "meter" }, [
        el("div", { className: "track" }, el("div", { className: `fill${short ? " short" : ""}`, style: `width:${covered * 100}%` })),
        el("div", { className: "legend" }, [
          el("span", {}, [icon(short ? "alert" : "check", 12), ` ${Math.round(covered * 100)}% covered by credit`]),
          el("span", { textContent: output > 0 ? `${money(output)} output tax` : "No sales posted yet" }),
        ]),
      ]),
    ]),
  ]));

  body.append(el("div", { className: "card" }, [
    el("header", {}, [
      el("h2", { textContent: "Breakdown" }),
      el("span", { className: "sub", textContent: "the same arithmetic as your Tax Payable sheet" }),
    ]),
    el("div", { className: "table-scroll" }, el("table", {}, [
      el("thead", {}, el("tr", {}, [
        el("th", { textContent: "" }),
        el("th", { className: "num", textContent: "IGST" }),
        el("th", { className: "num", textContent: "CGST" }),
        el("th", { className: "num", textContent: "SGST" }),
      ])),
      el("tbody", {}, [
        taxRow("Credit carried forward", data.itc_carry_forward),
        taxRow("This month's purchases", data.itc_current_purchases),
        taxRow("Credit notes", data.credit_note_reversal),
        taxRow("Reverse charge credit", data.rcm_input),
        taxRow("Output tax on sales", data.output_tax),
      ]),
      el("tfoot", {}, el("tr", {}, [
        el("td", { textContent: "Credit available" }),
        el("td", { className: "num", textContent: moneyRound(data.itc_available.igst) }),
        el("td", { className: "num", textContent: moneyRound(data.itc_available.cgst) }),
        el("td", { className: "num", textContent: moneyRound(data.itc_available.sgst) }),
      ])),
    ])),
  ]));
}

function tile(label, value, sub) {
  return el("div", { className: "tile" }, [
    el("div", { className: "k", textContent: label }),
    el("div", { className: "v", textContent: value }),
    el("div", { className: "sub", textContent: sub }),
  ]);
}

function taxRow(label, values) {
  return el("tr", {}, [
    el("td", { style: "color:var(--ink)", textContent: label }),
    el("td", { className: "num", textContent: moneyRound(values.igst) }),
    el("td", { className: "num", textContent: moneyRound(values.cgst) }),
    el("td", { className: "num", textContent: moneyRound(values.sgst) }),
  ]);
}

// --------------------------------------------------------------------------- //
// Navigation
// --------------------------------------------------------------------------- //

const SCREENS = ["inbox", "review", "registers", "tax"];

/* Hash routing, so a screen survives a refresh and can be linked to. */
function screenFromHash() {
  const name = (location.hash || "").replace(/^#\/?/, "").split("/")[0];
  return SCREENS.includes(name) ? name : "inbox";
}

function show(name, { push = true } = {}) {
  state.screen = name;
  if (push && screenFromHash() !== name) location.hash = `#/${name}`;
  for (const button of document.querySelectorAll("#nav button")) {
    if (button.dataset.screen === name) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  for (const id of ["inbox", "review", "registers", "tax"]) $(`#screen-${id}`).hidden = id !== name;

  if (name === "inbox") renderInbox();
  if (name === "review") renderReview();
  if (name === "registers") renderRegisters();
  if (name === "tax") renderTax();
}

async function refresh() {
  await loadInfo();
  await loadDocuments();
  show(state.screen);
}

// --------------------------------------------------------------------------- //
// Wiring
// --------------------------------------------------------------------------- //

function wire() {
  for (const button of document.querySelectorAll("#nav button")) {
    button.addEventListener("click", () => show(button.dataset.screen));
  }

  const dropzone = $("#dropzone");
  const input = $("#file-input");
  dropzone.addEventListener("click", () => input.click());
  input.addEventListener("change", () => { upload([...input.files]); input.value = ""; });
  for (const ev of ["dragenter", "dragover"]) {
    dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.add("hot"); });
  }
  for (const ev of ["dragleave", "drop"]) {
    dropzone.addEventListener(ev, (e) => { e.preventDefault(); dropzone.classList.remove("hot"); });
  }
  dropzone.addEventListener("drop", (e) => upload([...e.dataTransfer.files]));

  $("#period-picker").addEventListener("change", (e) => {
    state.period = e.target.value;
    if (state.screen === "registers") renderRegisters();
    if (state.screen === "tax") renderTax();
  });

  $("#btn-download").addEventListener("click", () => {
    window.location.href = url(`/api/workbook/download?period=${encodeURIComponent(state.period)}`);
  });

  $("#btn-reset").addEventListener("click", async () => {
    if (!confirm("Discard every posted row and start again from the master workbook?")) return;
    try { await api("/api/workbook/reset", { method: "POST" }); state.reviewId = null; state.selected.clear(); toast("Reset."); }
    catch (err) { toast(err.message, "error"); }
    await refresh();
  });

  // Reviewing a batch is faster from the keyboard than the mouse.
  window.addEventListener("hashchange", () => {
    const name = screenFromHash();
    if (name !== state.screen) show(name, { push: false });
  });

  document.addEventListener("keydown", (e) => {
    if (state.screen !== "review" || state.editing) return;
    if (e.target.matches("input, select, textarea")) return;
    const pending = queue();
    const index = pending.findIndex((d) => d.id === state.reviewId);
    if (e.key === "ArrowRight" && index < pending.length - 1) {
      state.reviewId = pending[index + 1].id; renderReview();
    } else if (e.key === "ArrowLeft" && index > 0) {
      state.reviewId = pending[index - 1].id; renderReview();
    }
  });
}

(async function start() {
  wire();
  try {
    await loadInfo();
    await loadDocuments();
    show(screenFromHash(), { push: false });
  } catch (err) {
    $("#inbox-list").append(el("div", { className: "card" }, el("div", { className: "empty" }, [
      el("div", { className: "big" }, "The backend is not responding"),
      `This page is served by the frontend server, but the API at ${API} could not be reached. `,
      el("div", { style: "margin-top:.6rem;font-family:var(--mono);font-size:.8rem" },
        "cd backend  →  python -m uvicorn app.main:app --port 8000"),
    ])));
    toast(err.message, "error");
  }
})();
