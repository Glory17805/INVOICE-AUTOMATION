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
  screen: "dashboard",
  period: null,
  reviewId: null,
  register: "sales",
  filter: "",
  selected: new Set(),
  editing: false,
  busy: false,

  // The batch the Processing screen is watching, and the History screen's
  // current search - both survive navigating away and back.
  lastBatch: [],
  historyQuery: "",
  historyFilter: "",
  settings: {},
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

/* The session token.

   Kept in localStorage rather than a cookie: the API is on a different origin,
   so a cookie would have to be third-party and cross-site - exactly the thing
   browsers are busy switching off. A token in a header is unambiguous, is never
   sent anywhere the page did not choose to send it, and needs no CSRF defence
   because it is not attached automatically. */
const TOKEN_KEY = "gst.session";

const session = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  user: null,
};

function setToken(token) {
  session.token = token || "";
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

function authHeaders(extra) {
  const headers = { ...(extra || {}) };
  if (session.token) headers.Authorization = `Bearer ${session.token}`;
  return headers;
}

/* Raised when the backend says the session is no longer good. It is caught at
   the boundary and turned into a sign-out, so no caller has to remember to. */
class Unauthenticated extends Error {}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(url(path), { ...options, headers: authHeaders(options.headers) });
  } catch (_) {
    // A separate backend can be down while this page is perfectly alive.
    throw new Error(`Cannot reach the backend at ${API}. Is it running?`);
  }
  if (response.status === 401) {
    // Only a session that *was* good and has stopped being good means sign out.
    // A rejected sign-in attempt is also a 401, and throwing the person back to
    // a freshly-wiped form would erase the message telling them what was wrong.
    if (session.token) {
      setTimeout(() => {
        signOut({ quiet: true });
        gateAlert("Your session has ended. Sign in again.", "info");
      }, 0);
    }
    let detail = "Your session has ended. Sign in again.";
    try { detail = (await response.json()).detail || detail; } catch (_) { /* not JSON */ }
    throw new Unauthenticated(detail);
  }
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return response;
}

async function api(path, options = {}) {
  const response = await request(path, options);
  return response.status === 204 ? null : response.json();
}

/* Fetching bytes for the browser to display or save.

   An authentication header cannot ride on an <iframe src>, an <img src> or a
   plain download link - the browser issues those itself and attaches nothing.
   So the bytes are fetched here, with the header, and handed over as a blob.
   The same path is used whether or not a key is configured, so the viewer
   cannot work locally and then break the day authentication is turned on. */
const objectUrls = new Set();

function releaseObjectUrls() {
  for (const objectUrl of objectUrls) URL.revokeObjectURL(objectUrl);
  objectUrls.clear();
}

async function blobUrl(path) {
  const response = await request(path);
  const objectUrl = URL.createObjectURL(await response.blob());
  objectUrls.add(objectUrl);
  return objectUrl;
}

/* Point a viewer at a document once its bytes have arrived. Rendering stays
   synchronous; only the source is late. */
function attachDocument(node, docId, fragment = "") {
  blobUrl(`/api/documents/${docId}/file`)
    .then((objectUrl) => { node.src = objectUrl + fragment; })
    .catch((err) => { node.dataset.error = err.message; });
}

async function openDocument(doc) {
  try {
    window.open(await blobUrl(`/api/documents/${doc.id}/file`), "_blank", "noopener");
  } catch (err) {
    toast(`Could not open ${doc.filename}: ${err.message}`, "error");
  }
}

async function saveWorkbook(period) {
  try {
    const objectUrl = await blobUrl(`/api/workbook/download?period=${encodeURIComponent(period)}`);
    const link = el("a", { href: objectUrl, download: `Ira Innovations GST ${period}.xlsx` });
    document.body.append(link);
    link.click();
    link.remove();
  } catch (err) {
    toast(err.message, "error");
  }
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

  /* Three states, not two. Holding a key is not the same as the model
     answering: an expired key, an exhausted balance or a dropped network all
     fall back to the offline reader. Naming the configured provider through
     that would tell someone their invoices were read by a model that never
     saw them. */
  const PROVIDER_NAMES = { claude: "Claude", gemini: "Gemini", heuristic: "Offline" };
  const provider = state.info.provider || state.info.reader;
  const readerName = PROVIDER_NAMES[provider] || provider || "Offline";
  const configured = provider && provider !== "heuristic";
  const effective = state.info.reader_effective;
  const degraded = configured && effective === "heuristic";

  const dot = $("#reader-dot");
  const label = $("#reader-label");

  if (!configured) {
    dot.className = "dot warn";
    label.textContent = "Offline reader";
    label.title = "No reader key configured. Text-layer PDFs are read in full; scans cannot "
      + "be read. Add a key to backend/.env — no restart needed.";
  } else if (degraded) {
    dot.className = "dot warn";
    label.textContent = `${readerName} unavailable`;
    label.title = state.info.reader_note
      ? `Falling back to the offline reader. ${state.info.reader_note}`
      : "Falling back to the offline reader.";
  } else {
    dot.className = "dot";
    label.textContent = `${readerName} reader`;
    label.title = `Reading with ${state.info.model}.`
      + (state.info.provider_tier ? ` (${state.info.provider_tier} tier)` : "");
  }

  const banner = $("#reader-banner");
  if (banner) {
    banner.textContent = "";

    /* Where the client's invoices go is not a footnote. If the reader's free
       tier may train on what is sent to it, that concerns named counterparties,
       GSTINs and amounts belonging to a real company - so it is stated on every
       screen, and it clears when the underlying fact changes rather than when
       somebody clicks it away. */
    // The sentence from the backend already carries the instruction. Repeating
    // it underneath only made the warning longer, and a longer warning is an
    // easier one to skip.
    if (state.info.training_risk) {
      banner.append(alertBox("warning", [
        el("strong", { textContent: "Invoice data may be used to train the reader's model. " }),
        state.info.training_risk,
      ]));
    }

    if (degraded) {
      banner.append(alertBox("warning", [
        el("strong", { textContent: `${readerName} could not be reached, so invoices are being read offline. ` }),
        state.info.reader_note || "",
        " Scans and photos cannot be read at all this way, and text-layer PDFs get a "
        + "simpler read that is worth checking.",
      ]));
    }

    banner.hidden = !banner.childNodes.length;
  }

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
  // Whole documents, not summaries: the Queue and Review screens read the
  // extraction, the treatment and the issue list off these.
  const page = await api("/api/documents?view=full");
  state.documents = page.documents;
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

  // The pane about to be replaced holds the only references to these; without
  // this the blobs stay in memory for the life of the tab.
  releaseObjectUrls();

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
    viewer = el("iframe", { className: "doc-frame", title: doc.filename });
    attachDocument(viewer, doc.id, "#view=FitH");
  } else if (isImage) {
    viewer = el("img", { className: "doc-frame", style: "object-fit:contain", alt: doc.filename });
    attachDocument(viewer, doc.id);
  } else {
    viewer = el("div", { className: "doc-missing" }, [
      icon("info", 22), doc.filename,
      el("button", { className: "btn sm", textContent: "Open file", onclick: () => openDocument(doc) }),
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
      el("button", {
        className: "btn sm ghost", textContent: "Open in a tab",
        onclick: () => openDocument(doc),
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

  /* The item table, when the reader found one. It is read-only on purpose: the
     register takes one row per invoice, so the lines are here to check the
     total against, not to be edited into it. Editing them would imply they
     drive something they do not. */
  const lines = (extracted.line_items || []).filter((line) => line && (
    line.description || line.taxable_value != null || line.quantity != null));

  if (lines.length) {
    const rows = el("tbody");
    for (const line of lines) {
      rows.append(el("tr", {}, [
        el("td", { textContent: line.description || "—" }),
        el("td", { textContent: line.hsn_sac || "—" }),
        el("td", { className: "num", textContent: line.quantity != null ? line.quantity : "—" }),
        el("td", { className: "num", textContent: line.unit_rate != null
          ? money(line.unit_rate) : "—" }),
        el("td", { className: "num", textContent: line.gst_rate_percent != null
          ? `${line.gst_rate_percent}%` : "—" }),
        el("td", { className: "num", textContent: line.taxable_value != null
          ? money(line.taxable_value) : "—" }),
      ]));
    }

    // Worth showing: if the lines do not add up to the invoice's own taxable
    // value, that is exactly the sort of misread a reviewer is here to catch.
    const summed = lines.reduce((total, line) => total + Number(line.taxable_value || 0), 0);
    const stated = Number(extracted.taxable_value || 0);
    const drifts = stated > 0 && Math.abs(summed - stated) > 1;

    rows.append(el("tr", { className: `total${drifts ? " bad" : ""}` }, [
      el("td", { colSpan: 5, textContent: drifts
        ? "Lines add up to (does not match the invoice total)"
        : "Lines add up to" }),
      el("td", { className: "num", textContent: money(summed) }),
    ]));

    body.append(el("div", { className: "card", style: "margin-top:1rem" }, [
      el("header", {}, [
        el("h2", { textContent: "Line items" }),
        el("span", { className: "grow" }),
        el("span", { className: "muted", textContent: `${lines.length} line${lines.length === 1 ? "" : "s"}` }),
      ]),
      el("div", { className: "table-wrap" }, el("table", { className: "grid lines" }, [
        el("thead", {}, el("tr", {}, [
          el("th", { textContent: "Description" }),
          el("th", { textContent: "HSN / SAC" }),
          el("th", { className: "num", textContent: "Qty" }),
          el("th", { className: "num", textContent: "Rate" }),
          el("th", { className: "num", textContent: "GST" }),
          el("th", { className: "num", textContent: "Amount" }),
        ])),
        rows,
      ])),
    ]));
  }
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

  // Screened here as well as on the server: the server's answer is the one
  // that counts, but this one arrives before the bytes do.
  const { good, rejected } = screenFiles(files);
  for (const reason of rejected) toast(reason, "error");
  if (!good.length) return;

  const form = new FormData();
  for (const file of good) form.append("files", file);
  toast(`Uploading ${good.length} file${good.length > 1 ? "s" : ""}…`);

  try {
    const result = await api("/api/documents", { method: "POST", body: form });
    if (result.errors && result.errors.length) toast(result.errors.join("; "), "error");

    const ids = result.captured.map((doc) => doc.id);
    if (!ids.length) return;

    state.lastBatch = ids;
    toast(`${ids.length} invoice${ids.length > 1 ? "s" : ""} captured — reading…`);

    // Straight to the progress view: an upload that vanishes into a list is
    // the thing this screen exists to avoid.
    show("processing");
    await loadDocuments();
  } catch (err) {
    toast(err.message, "error");
    await refresh();
  }
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

const SCREENS = [
  "dashboard", "upload", "processing", "queue", "review",
  "history", "registers", "tax", "email", "settings", "admin",
];

/* The nav has no Processing button - it is somewhere the app sends you after
   an upload, not somewhere you choose to go. */
const NAV_SCREENS = SCREENS.filter((name) => name !== "processing");

/* Hash routing, so a screen survives a refresh and can be linked to. */
function screenFromHash() {
  const name = (location.hash || "").replace(/^#\/?/, "").split("/")[0];
  return SCREENS.includes(name) ? name : "dashboard";
}

function show(name, { push = true } = {}) {
  if (!SCREENS.includes(name)) name = "dashboard";
  if (name === "admin" && !isAdmin()) {
    toast("That needs an administrator account.", "error");
    name = "dashboard";
  }

  state.screen = name;
  if (push && screenFromHash() !== name) location.hash = `#/${name}`;
  for (const button of document.querySelectorAll("#nav button")) {
    const active = button.dataset.screen === name
      || (name === "processing" && button.dataset.screen === "upload");
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  for (const id of SCREENS) $(`#screen-${id}`).hidden = id !== name;

  // The period picker only means something on the two screens that read the
  // workbook; showing it everywhere invites people to change it expecting
  // something to happen.
  $(".context").hidden = !["registers", "tax", "dashboard"].includes(name);

  const renderers = {
    dashboard: renderDashboard,
    upload: renderUpload,
    processing: renderProcessing,
    queue: renderInbox,
    review: renderReview,
    history: renderHistory,
    registers: renderRegisters,
    tax: renderTax,
    email: renderEmail,
    settings: renderSettings,
    admin: renderAdmin,
  };
  (renderers[name] || renderDashboard)();
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
  $("#gate-form").addEventListener("submit", submitGate);

  $("#btn-logout").addEventListener("click", async () => {
    try { await api("/api/auth/logout", { method: "POST" }); } catch (_) { /* going anyway */ }
    signOut();
  });
  $("#who").addEventListener("click", () => show("settings"));

  for (const button of document.querySelectorAll("#nav button")) {
    button.addEventListener("click", () => show(button.dataset.screen));
  }

  const input = $("#file-input");
  input.addEventListener("change", () => { upload([...input.files]); input.value = ""; });
  wireDropzone($("#dropzone"));

  $("#period-picker").addEventListener("change", (e) => {
    state.period = e.target.value;
    if (state.screen === "registers") renderRegisters();
    if (state.screen === "tax") renderTax();
  });

  $("#btn-scan").addEventListener("click", checkWatchFolder);

  $("#btn-download").addEventListener("click", () => saveWorkbook(state.period));

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

// --------------------------------------------------------------------------- //
// Signing in
//
// One card that switches between four jobs: sign in, create the first account,
// ask for a reset link, and set a new password from one. They share a shape
// because they are the same moment in the product - you are outside, and you
// want in.
// --------------------------------------------------------------------------- //

const gate = { mode: "login", busy: false, resetToken: "", signupMode: "closed" };

function isAdmin() {
  return (session.user || {}).role === "admin";
}

function field(name, label, type = "text", extra = {}) {
  const input = el("input", { type, name, id: `f-${name}`, autocomplete: extra.autocomplete || "on",
                              placeholder: extra.placeholder || "", required: true });
  if (extra.value) input.value = extra.value;
  return el("label", { className: "gate-field" }, [
    el("span", { textContent: label }),
    input,
  ]);
}

function gateAlert(message, kind = "error") {
  const box = $("#gate-alert");
  box.textContent = "";
  if (message) box.append(alertBox(kind, message));
}

function renderGate() {
  $("#boot").hidden = true;
  $("#shell").hidden = true;
  $("#gate").hidden = false;

  const form = $("#gate-form");
  const foot = $("#gate-foot");
  form.textContent = "";
  foot.textContent = "";

  const submit = (label) => el("button", {
    className: "btn primary block", type: "submit",
    textContent: gate.busy ? "Working…" : label, disabled: gate.busy,
  });

  const link = (label, mode) => el("button", {
    className: "linkish", type: "button", textContent: label,
    onclick: () => { gate.mode = mode; gateAlert(""); renderGate(); },
  });

  if (gate.mode === "setup") {
    form.append(
      el("p", { className: "gate-lede" },
        "No accounts exist yet. The first one you create is the administrator."),
      field("name", "Your name", "text", { autocomplete: "name" }),
      field("email", "Email", "email", { autocomplete: "username" }),
      field("password", "Password", "password", { autocomplete: "new-password",
                                                  placeholder: "At least 10 characters" }),
      submit("Create administrator"),
    );
  } else if (gate.mode === "signup") {
    form.append(
      el("p", { className: "gate-lede" }, gate.signupMode === "approval"
        ? "Create your account and an administrator will approve it. You will be able "
          + "to sign in once they do."
        : "Create your account and you will be signed straight in."),
      field("name", "Your name", "text", { autocomplete: "name" }),
      field("email", "Email", "email", { autocomplete: "username" }),
      field("password", "Password", "password", { autocomplete: "new-password",
                                                  placeholder: "At least 10 characters" }),
      submit("Create account"),
    );
    foot.append(link("Already have an account? Sign in", "login"));
  } else if (gate.mode === "forgot") {
    form.append(
      el("p", { className: "gate-lede" },
        "Enter your email and a reset link will be issued. It expires in an hour."),
      field("email", "Email", "email", { autocomplete: "username" }),
      submit("Send reset link"),
    );
    foot.append(link("Back to sign in", "login"));
  } else if (gate.mode === "reset") {
    form.append(
      el("p", { className: "gate-lede" }, "Choose a new password."),
      field("password", "New password", "password", { autocomplete: "new-password",
                                                      placeholder: "At least 10 characters" }),
      submit("Set password"),
    );
    foot.append(link("Back to sign in", "login"));
  } else {
    form.append(
      field("email", "Email", "email", { autocomplete: "username" }),
      field("password", "Password", "password", { autocomplete: "current-password" }),
      submit("Sign in"),
    );
    foot.append(link("Forgot your password?", "forgot"));

    // Only offered when the system actually accepts one. A link that always
    // ends in "signup is closed" is worse than no link.
    if (gate.signupMode === "approval" || gate.signupMode === "open") {
      foot.append(el("span", { className: "gate-sep", textContent: "·" }));
      foot.append(link("Create an account", "signup"));
    }
  }

  const first = form.querySelector("input");
  if (first) first.focus();
}

async function submitGate(event) {
  event.preventDefault();
  if (gate.busy) return;

  const data = Object.fromEntries(new FormData(event.target).entries());
  gate.busy = true;
  renderGate();
  gateAlert("");

  try {
    if (gate.mode === "setup" || gate.mode === "signup") {
      const result = await api("/api/auth/signup", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: data.email, name: data.name, password: data.password }),
      });

      // Under the approval policy there is no session yet - the account exists
      // and that is all it does. Say so instead of pretending to sign in.
      if (result.pending) {
        gate.mode = "login";
        gate.busy = false;
        renderGate();
        gateAlert(result.detail, "good");
        return;
      }

      setToken(result.token);
      session.user = result.user;
      await enterApp();
      return;
    }

    if (gate.mode === "forgot") {
      const result = await api("/api/auth/forgot", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: data.email }),
      });
      gate.mode = "login";
      gate.busy = false;
      renderGate();
      gateAlert(result.detail + " If no mail server is configured, ask your administrator "
                + "for the link — it is written to the server log.", "info");
      return;
    }

    if (gate.mode === "reset") {
      await api("/api/auth/reset", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: gate.resetToken, password: data.password }),
      });
      gate.mode = "login";
      gate.busy = false;
      location.hash = "#/dashboard";
      renderGate();
      gateAlert("Password set. Sign in with it.", "good");
      return;
    }

    const result = await api("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: data.email, password: data.password }),
    });
    setToken(result.token);
    session.user = result.user;
    await enterApp();
  } catch (err) {
    gate.busy = false;
    renderGate();
    gateAlert(err.message);
  }
}

function signOut({ quiet = false } = {}) {
  setToken("");
  session.user = null;
  gate.mode = "login";
  gate.busy = false;
  renderGate();
  if (!quiet) gateAlert("You are signed out.", "info");
}

// --------------------------------------------------------------------------- //
// Dashboard
// --------------------------------------------------------------------------- //

function statCard(value, label, tone = "") {
  return el("div", { className: `stat ${tone}` }, [
    el("span", { className: "v", textContent: String(value) }),
    el("span", { className: "k", textContent: label }),
  ]);
}

async function renderDashboard() {
  const body = $("#dashboard-body");
  body.textContent = "";

  const hour = new Date().getHours();
  const part = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  const who = (session.user || {}).name || "";
  $("#dash-greeting").textContent = who ? `${part}, ${who.split(" ")[0]}` : "Dashboard";

  let data;
  try {
    data = await api("/api/dashboard");
  } catch (err) {
    body.append(el("div", { className: "card" }, el("div", { className: "empty" }, err.message)));
    return;
  }

  const t = data.totals;
  body.append(el("div", { className: "stats" }, [
    statCard(t.all, "Invoices in total"),
    statCard(t.processed, "Posted to the workbook", "good"),
    statCard(t.needs_review + t.ready, "Waiting on you", (t.needs_review ? "warn" : "")),
    statCard(t.failed, "Failed", t.failed ? "bad" : ""),
  ]));

  // Drop target, right where someone lands.
  const zone = el("div", { className: "dropzone", id: "dash-dropzone" }, [
    el("div", { className: "big" }, "Drop invoices here, or click to choose files"),
    el("div", { className: "hint" },
      `PDF, PNG or JPG · up to ${(state.info || {}).max_upload_mb || 25} MB each`),
  ]);
  wireDropzone(zone);
  body.append(zone);

  if (t.reading) {
    body.append(alertBox("info", [
      el("strong", { textContent: `${t.reading} invoice${t.reading > 1 ? "s" : ""} still being read. ` }),
      "They will appear as each one finishes.",
    ]));
  }

  const rows = data.recent || [];
  const table = el("div", { className: "card" }, [
    el("header", {}, [
      el("h2", { textContent: "Recent invoices" }),
      el("span", { className: "grow" }),
      el("button", { className: "btn sm ghost", textContent: "See all",
                     onclick: () => show("history") }),
    ]),
    rows.length
      ? historyTable(rows, { compact: true })
      : el("div", { className: "empty" }, [
          el("div", { className: "big" }, "Nothing yet"),
          "Drop an invoice above and it will be read, classified and checked.",
        ]),
  ]);
  body.append(table);
}

// --------------------------------------------------------------------------- //
// Upload
// --------------------------------------------------------------------------- //

function wireDropzone(zone) {
  const input = $("#file-input");
  zone.addEventListener("click", () => input.click());
  for (const ev of ["dragenter", "dragover"]) {
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("hot"); });
  }
  for (const ev of ["dragleave", "drop"]) {
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("hot"); });
  }
  zone.addEventListener("drop", (e) => upload([...e.dataTransfer.files]));
}

const ACCEPTED = ["pdf", "png", "jpg", "jpeg", "webp", "gif", "txt", "csv"];

/* Checked here as well as on the server. The server's answer is the one that
   counts; this one is instant and tells you before you wait for an upload. */
function screenFiles(files) {
  const limit = ((state.info || {}).max_upload_mb || 25) * 1024 * 1024;
  const good = [];
  const rejected = [];
  for (const file of files) {
    const extension = (file.name.split(".").pop() || "").toLowerCase();
    if (!ACCEPTED.includes(extension)) {
      rejected.push(`${file.name} — not a supported file type.`);
    } else if (file.size > limit) {
      rejected.push(`${file.name} — ${(file.size / 1048576).toFixed(1)} MB, over the `
                    + `${(state.info || {}).max_upload_mb || 25} MB limit.`);
    } else {
      good.push(file);
    }
  }
  return { good, rejected };
}

function renderUpload() {
  const body = $("#upload-body");
  body.textContent = "";

  const zone = el("div", { className: "dropzone tall" }, [
    el("div", { className: "big" }, "Drop invoice files here"),
    el("div", { className: "hint" }, "or click to choose them"),
  ]);
  wireDropzone(zone);

  body.append(
    zone,
    el("div", { className: "upload-facts" }, [
      el("div", {}, [el("span", { className: "k" }, "Accepted"),
                     el("span", { className: "v" }, "PDF, PNG, JPG, WEBP, GIF, TXT, CSV")]),
      el("div", {}, [el("span", { className: "k" }, "Maximum size"),
                     el("span", { className: "v" },
                        `${(state.info || {}).max_upload_mb || 25} MB per file`)]),
      el("div", {}, [el("span", { className: "k" }, "Multiple files"),
                     el("span", { className: "v" }, "Yes — and a print run is split per invoice")]),
    ]),
  );

  if (state.lastBatch && state.lastBatch.length) {
    body.append(el("div", { className: "card" }, [
      el("header", {}, [el("h2", { textContent: "Last upload" })]),
      el("div", { className: "card-body" }, [
        el("button", { className: "btn sm", textContent: "See how it is getting on",
                       onclick: () => show("processing") }),
      ]),
    ]));
  }
}

// --------------------------------------------------------------------------- //
// Processing
// --------------------------------------------------------------------------- //

function stepIcon(stepState) {
  if (stepState === "done") return icon("check", 14);
  if (stepState === "failed") return icon("alert", 14);
  if (stepState === "active") return icon("clock", 14);
  return icon("dot", 14);
}

async function renderProcessing() {
  const body = $("#processing-body");
  body.textContent = "";

  const ids = state.lastBatch || [];
  if (!ids.length) {
    body.append(el("div", { className: "card" }, el("div", { className: "empty" }, [
      el("div", { className: "big" }, "Nothing in flight"),
      "Upload an invoice and its progress will show here.",
    ])));
    return;
  }

  const list = el("div", { className: "proc-list" });
  body.append(list);

  const draw = (reports) => {
    list.textContent = "";
    for (const report of reports) {
      if (!report) continue;
      const failed = report.stage === "failed";
      const steps = el("ol", { className: "steps" });
      for (const step of report.steps) {
        steps.append(el("li", { className: `step ${step.state}` },
          [stepIcon(step.state), el("span", { textContent: step.label })]));
      }

      const card = el("div", { className: `card proc${failed ? " failed" : ""}` }, [
        el("header", {}, [
          el("h2", { textContent: report.filename || report.id }),
          el("span", { className: "grow" }),
          el("span", { className: "pct", textContent: failed ? "Failed" : `${report.percent}%` }),
        ]),
        el("div", { className: "card-body" }, [
          el("div", { className: "bar" },
            el("div", { className: `fill${failed ? " bad" : ""}`,
                        style: `width:${failed ? 100 : report.percent}%` })),
          steps,
          failed || report.failure_reason
            ? failureBlock(report)
            : null,
          report.status && report.status !== "new"
            ? el("div", { className: "proc-actions" }, [
                el("button", { className: "btn sm", textContent: "Review this invoice",
                               onclick: () => openReview(report.id) }),
              ])
            : null,
        ]),
      ]);
      list.append(card);
    }
  };

  /* Poll while anything is still moving. Each report is fetched on its own so
     one document that fails does not stop the others being shown. */
  const done = new Set();
  for (let tick = 0; tick < 600; tick += 1) {
    const reports = await Promise.all(ids.map((id) =>
      api(`/api/documents/${id}/progress`).catch(() => null)));
    draw(reports);

    for (const report of reports) {
      if (report && ["done", "posted", "failed"].includes(report.stage)) done.add(report.id);
    }
    if (done.size >= ids.length) break;
    if (state.screen !== "processing") return;   // they navigated away
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  await loadDocuments();
}

function failureBlock(report) {
  return el("div", { className: "failure" }, [
    el("div", { className: "failure-head" }, [icon("alert", 16),
      el("strong", { textContent: "This invoice could not be processed" })]),
    el("p", { textContent: report.failure_reason
      || report.error || "The reason was not recorded." }),
    el("div", { className: "failure-actions" }, [
      el("button", { className: "btn sm", textContent: "Try again",
                     onclick: () => reprocess(report.id) }),
      el("button", { className: "btn sm ghost", textContent: "Upload a different file",
                     onclick: () => show("upload") }),
    ]),
  ]);
}

async function reprocess(docId) {
  try {
    await api(`/api/documents/${docId}/reprocess`, { method: "POST" });
    state.lastBatch = [docId];
    toast("Reading it again…");
    show("processing");
  } catch (err) {
    toast(err.message, "error");
  }
}

// --------------------------------------------------------------------------- //
// History
// --------------------------------------------------------------------------- //

const HISTORY_FILTERS = [
  ["", "All"],
  ["ready,needs_review", "Waiting"],
  ["posted", "Posted"],
  ["failed", "Failed"],
];

function historyTable(rows, { compact = false } = {}) {
  const head = el("thead", {}, el("tr", {}, [
    el("th", { textContent: "Invoice" }),
    el("th", { textContent: "Party" }),
    el("th", { textContent: "Date" }),
    compact ? null : el("th", { textContent: "Period" }),
    el("th", { textContent: "Source" }),
    el("th", { className: "num", textContent: "Amount" }),
    el("th", { textContent: "Status" }),
    el("th", { textContent: "" }),
  ]));

  const tbody = el("tbody");
  for (const row of rows) {
    const actions = el("div", { className: "row-actions" });
    actions.append(el("button", { className: "btn xs ghost", textContent: "Open",
                                  onclick: () => openReview(row.id) }));
    if (row.status === "failed") {
      actions.append(el("button", { className: "btn xs ghost", textContent: "Retry",
                                    onclick: () => reprocess(row.id) }));
    }

    tbody.append(el("tr", { className: row.status === "failed" ? "bad" : "" }, [
      el("td", {}, [
        el("div", { className: "cell-main", textContent: row.invoice_number || "—" }),
        el("div", { className: "cell-sub", textContent: row.filename }),
      ]),
      el("td", { textContent: row.party || "—" }),
      el("td", { textContent: row.invoice_date || "—" }),
      compact ? null : el("td", { textContent: row.period || "—" }),
      el("td", {}, el("span", { className: "src", textContent: row.source || "upload" })),
      el("td", { className: "num", textContent: row.invoice_total != null
        ? money(row.invoice_total) : "—" }),
      el("td", {}, statusPill(row.status)),
      el("td", {}, actions),
    ]));
  }
  return el("div", { className: "table-wrap" }, el("table", { className: "grid" }, [head, tbody]));
}

async function renderHistory() {
  const body = $("#history-body");
  body.textContent = "";

  const controls = el("div", { className: "filters" });
  const search = el("input", {
    className: "control search", type: "search", placeholder: "Search invoice, party or file…",
    value: state.historyQuery || "",
  });
  let timer;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.historyQuery = search.value; renderHistory(); }, 250);
  });
  controls.append(search);

  const chips = el("div", { className: "chips" });
  for (const [value, label] of HISTORY_FILTERS) {
    chips.append(el("button", {
      className: `chip${(state.historyFilter || "") === value ? " on" : ""}`,
      textContent: label,
      onclick: () => { state.historyFilter = value; renderHistory(); },
    }));
  }
  controls.append(chips);
  body.append(controls);

  const params = new URLSearchParams({ view: "summary" });
  if (state.historyQuery) params.set("q", state.historyQuery);
  if (state.historyFilter) params.set("status", state.historyFilter);

  let data;
  try {
    data = await api(`/api/documents?${params}`);
  } catch (err) {
    body.append(el("div", { className: "card" }, el("div", { className: "empty" }, err.message)));
    return;
  }

  if (!data.documents.length) {
    body.append(el("div", { className: "card" }, el("div", { className: "empty" }, [
      el("div", { className: "big" }, "Nothing matches"),
      state.historyQuery || state.historyFilter
        ? "Try a different search or filter."
        : "No invoices have arrived yet.",
    ])));
    return;
  }

  body.append(el("div", { className: "card" }, [
    el("header", {}, [
      el("h2", { textContent: `${data.total} invoice${data.total === 1 ? "" : "s"}` }),
      el("span", { className: "grow" }),
      el("button", {
        className: "btn sm", textContent: "Export the workbook",
        onclick: () => saveWorkbook(state.period),
      }),
    ]),
    historyTable(data.documents),
  ]));
}

// --------------------------------------------------------------------------- //
// Email intake
// --------------------------------------------------------------------------- //

async function renderEmail() {
  const body = $("#email-body");
  body.textContent = "";

  let data;
  try {
    data = await api("/api/email/status");
  } catch (err) {
    body.append(el("div", { className: "card" }, el("div", { className: "empty" }, err.message)));
    return;
  }

  setPip("#pip-email", data.waiting, data.waiting ? "attention" : "");

  body.append(el("div", { className: "card" }, [
    el("header", {}, [
      el("h2", { textContent: "How invoices arrive" }),
      el("span", { className: "grow" }),
      el("span", { className: `pill ${data.connected ? "ready" : "new"}` },
        [icon(data.connected ? "check" : "clock", 12),
         data.connected ? "Watch folder active" : "Turned off"]),
    ]),
    el("div", { className: "card-body" }, [
      el("div", { className: "kv" }, [
        el("span", { className: "k" }, "Mode"),
        el("span", { className: "v" }, "Watch folder"),
        el("span", { className: "k" }, "Folder"),
        el("span", { className: "v mono", textContent: data.folder }),
        el("span", { className: "k" }, "Waiting to be picked up"),
        el("span", { className: "v" }, String(data.waiting)),
      ]),
      el("div", { className: "row-actions", style: "margin-top:.9rem" }, [
        el("button", { className: "btn primary", textContent: "Check for new invoices",
                       onclick: checkWatchFolder }),
      ]),
    ]),
  ]));

  if (data.waiting_files && data.waiting_files.length) {
    body.append(el("div", { className: "card" }, [
      el("header", {}, [el("h2", { textContent: "Sitting in the folder" })]),
      el("ul", { className: "plain" },
        data.waiting_files.map((name) => el("li", { className: "mono", textContent: name }))),
    ]));
  }

  // Honest about what is not built, rather than a Connected badge that means
  // nothing. A green tick against a mailbox nobody wired up is worse than
  // saying plainly that the mail rule is doing the work.
  body.append(el("div", { className: "card" }, [
    el("header", {}, [el("h2", { textContent: "Direct mailbox connection" })]),
    el("div", { className: "card-body" }, [
      alertBox("info", data.mailbox_connector.detail),
      el("p", { className: "muted" },
        "A Microsoft 365 or Gmail connector would remove that step. It needs the mailbox "
        + "invoices actually arrive in, and consent to read it — neither of which this "
        + "system should guess at."),
    ]),
  ]));

  const rules = data.rules || {};
  body.append(el("div", { className: "card" }, [
    el("header", {}, [el("h2", { textContent: "Processing rules" })]),
    el("div", { className: "card-body" }, [
      toggleRow("email_process_pdf_attachments", "Process PDF attachments",
                rules.process_pdf_attachments),
      toggleRow("email_mark_processed", "Mark emails once their invoice is captured",
                rules.mark_processed),
      toggleRow("email_notify", "Send a notification when processing finishes", rules.notify),
      el("p", { className: "muted" },
        "These apply to the mail rule feeding the watch folder. They are saved here so the "
        + "connector honours them the day it is wired up."),
    ]),
  ]));
}

async function checkWatchFolder() {
  try {
    const result = await api("/api/ingest/folder", { method: "POST" });
    const n = result.captured.length;
    if (result.errors && result.errors.length) toast(result.errors.join("; "), "error");
    if (!n) { toast("Nothing new in the watch folder."); return; }
    state.lastBatch = result.captured.map((d) => d.id);
    toast(`${n} invoice${n > 1 ? "s" : ""} picked up — reading…`);
    show("processing");
  } catch (err) {
    toast(err.message, "error");
  }
}

// --------------------------------------------------------------------------- //
// Settings
// --------------------------------------------------------------------------- //

function toggleRow(key, label, value) {
  const input = el("input", { type: "checkbox", id: `set-${key}`, checked: !!value });
  input.addEventListener("change", () => saveSetting(key, input.checked));
  if (!isAdmin()) {
    input.disabled = true;
    input.title = "Only an administrator can change this.";
  }
  return el("label", { className: "toggle", htmlFor: `set-${key}` }, [
    input, el("span", { textContent: label }),
  ]);
}

async function saveSetting(key, value) {
  try {
    const result = await api("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [key]: value }),
    });
    state.settings = result.values;
    toast("Saved.", "good");
  } catch (err) {
    toast(err.message, "error");
    renderSettings();
  }
}

async function renderSettings() {
  const body = $("#settings-body");
  body.textContent = "";

  let data;
  try {
    data = await api("/api/settings");
  } catch (err) {
    body.append(el("div", { className: "card" }, el("div", { className: "empty" }, err.message)));
    return;
  }
  state.settings = data.values;
  const user = session.user || {};

  // ---- Account -------------------------------------------------------------
  const nameInput = el("input", { className: "control", value: user.name || "" });
  const emailInput = el("input", { className: "control", value: user.email || "", type: "email" });

  body.append(el("div", { className: "card" }, [
    el("header", {}, [el("h2", { textContent: "Your profile" })]),
    el("div", { className: "card-body form" }, [
      el("label", {}, [el("span", {}, "Name"), nameInput]),
      el("label", {}, [el("span", {}, "Email"), emailInput]),
      el("div", { className: "row-actions" }, [
        el("button", {
          className: "btn primary", textContent: "Save profile",
          onclick: async () => {
            try {
              session.user = await api("/api/auth/me", {
                method: "PATCH", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: nameInput.value, email: emailInput.value }),
              });
              paintIdentity();
              toast("Profile saved.", "good");
            } catch (err) { toast(err.message, "error"); }
          },
        }),
      ]),
    ]),
  ]));

  // ---- Password ------------------------------------------------------------
  const currentPw = el("input", { className: "control", type: "password",
                                  autocomplete: "current-password" });
  const newPw = el("input", { className: "control", type: "password",
                              autocomplete: "new-password",
                              placeholder: "At least 10 characters" });

  body.append(el("div", { className: "card" }, [
    el("header", {}, [el("h2", { textContent: "Password" })]),
    el("div", { className: "card-body form" }, [
      el("label", {}, [el("span", {}, "Current password"), currentPw]),
      el("label", {}, [el("span", {}, "New password"), newPw]),
      el("p", { className: "muted" },
        "Changing this signs out every other device you are signed in on."),
      el("div", { className: "row-actions" }, [
        el("button", {
          className: "btn primary", textContent: "Change password",
          onclick: async () => {
            try {
              const result = await api("/api/auth/password", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ current_password: currentPw.value,
                                       new_password: newPw.value }),
              });
              setToken(result.token);
              currentPw.value = newPw.value = "";
              toast("Password changed.", "good");
            } catch (err) { toast(err.message, "error"); }
          },
        }),
      ]),
    ]),
  ]));

  // ---- Invoice processing --------------------------------------------------
  const currency = el("select", { className: "control" });
  for (const code of data.options.currency) {
    currency.append(el("option", { value: code, textContent: code,
                                   selected: code === data.values.currency }));
  }
  currency.addEventListener("change", () => saveSetting("currency", currency.value));

  const dateFormat = el("select", { className: "control" });
  for (const fmt of data.options.date_format) {
    dateFormat.append(el("option", { value: fmt, textContent: fmt,
                                     selected: fmt === data.values.date_format }));
  }
  dateFormat.addEventListener("change", () => saveSetting("date_format", dateFormat.value));

  if (!isAdmin()) { currency.disabled = true; dateFormat.disabled = true; }

  body.append(el("div", { className: "card" }, [
    el("header", {}, [el("h2", { textContent: "Invoice processing" })]),
    el("div", { className: "card-body form" }, [
      el("label", {}, [el("span", {}, "Currency"), currency]),
      el("label", {}, [el("span", {}, "Date format"), dateFormat]),
      toggleRow("auto_post_clean", "Post clean invoices without asking", data.values.auto_post_clean),
      el("p", { className: "muted" },
        "Even with that on, nothing reaches the workbook without passing every check. "
        + "Anything flagged still stops for review."),
    ]),
  ]));

  // ---- Who can join --------------------------------------------------------
  if (isAdmin()) {
    const modes = el("div", { className: "radios" });
    for (const option of data.options.signup_mode) {
      const input = el("input", {
        type: "radio", name: "signup_mode", id: `signup-${option.value}`,
        value: option.value, checked: data.values.signup_mode === option.value,
      });
      input.addEventListener("change", () => {
        if (input.checked) saveSetting("signup_mode", option.value);
      });
      modes.append(el("label", { className: "radio", htmlFor: `signup-${option.value}` },
        [input, el("span", { textContent: option.label })]));
    }

    body.append(el("div", { className: "card" }, [
      el("header", {}, [el("h2", { textContent: "Who can create an account" })]),
      el("div", { className: "card-body form" }, [
        modes,
        el("p", { className: "muted" },
          "This system holds filed tax records, so the default is that anyone may ask "
          + "and you decide. Choosing the second option means whoever finds the sign-in "
          + "page can read every invoice and every register without anyone approving it."),
      ]),
    ]));
  }

  // ---- Notifications -------------------------------------------------------
  body.append(el("div", { className: "card" }, [
    el("header", {}, [el("h2", { textContent: "Notifications" })]),
    el("div", { className: "card-body form" }, [
      toggleRow("notify_on_complete", "When processing finishes", data.values.notify_on_complete),
      toggleRow("notify_on_review", "When an invoice needs a look", data.values.notify_on_review),
      toggleRow("notify_on_failure", "When an invoice fails", data.values.notify_on_failure),
    ]),
  ]));

  if (!isAdmin()) {
    body.append(el("p", { className: "muted" },
      "Some settings are administrator-only and are shown here read-only."));
  }
}

// --------------------------------------------------------------------------- //
// Admin
// --------------------------------------------------------------------------- //

async function renderAdmin() {
  const body = $("#admin-body");
  body.textContent = "";
  if (!isAdmin()) return;

  let users = [];
  let stats = null;
  let log = [];
  try {
    [users, stats, log] = await Promise.all([
      api("/api/admin/users"),
      api("/api/admin/stats"),
      api("/api/admin/activity?limit=60"),
    ]);
  } catch (err) {
    body.append(el("div", { className: "card" }, el("div", { className: "empty" }, err.message)));
    return;
  }

  body.append(el("div", { className: "stats" }, [
    statCard(stats.users.total, "People"),
    statCard(stats.users.awaiting_approval || 0, "Waiting to be let in",
             stats.users.awaiting_approval ? "warn" : ""),
    statCard(stats.documents.posted || 0, "Posted", "good"),
    statCard(stats.documents.failed || 0, "Failed", stats.documents.failed ? "bad" : ""),
  ]));

  // ---- Waiting to be let in ------------------------------------------------
  const pending = users.filter((person) => person.awaiting_approval);
  if (pending.length) {
    const waiting = el("tbody");
    for (const person of pending) {
      waiting.append(el("tr", {}, [
        el("td", {}, [
          el("div", { className: "cell-main", textContent: person.name }),
          el("div", { className: "cell-sub", textContent: person.email }),
        ]),
        el("td", { className: "cell-sub", textContent: person.created_at
          ? person.created_at.replace("T", " ").slice(0, 16) : "" }),
        el("td", {}, el("div", { className: "row-actions" }, [
          el("button", {
            className: "btn xs primary", textContent: "Approve",
            onclick: () => approvePerson(person, "user"),
          }),
          el("button", {
            className: "btn xs ghost", textContent: "Approve as admin",
            onclick: () => approvePerson(person, "admin"),
          }),
          el("button", {
            className: "btn xs danger", textContent: "Reject",
            onclick: async () => {
              if (!confirm(`Reject ${person.email}? Their account is deleted.`)) return;
              try { await api(`/api/admin/users/${person.id}`, { method: "DELETE" }); }
              catch (err) { toast(err.message, "error"); return; }
              toast("Rejected.");
              renderAdmin();
            },
          }),
        ])),
      ]));
    }

    body.append(el("div", { className: "card attention" }, [
      el("header", {}, [
        el("h2", { textContent: "Waiting to be let in" }),
        el("span", { className: "grow" }),
        el("span", { className: "pill needs_review" },
          [icon("clock", 12), `${pending.length} waiting`]),
      ]),
      el("div", { className: "card-body" }, el("p", { className: "muted" },
        "These people created an account from the sign-in page. They cannot sign in, "
        + "and cannot see anything, until you approve them.")),
      el("div", { className: "table-wrap" }, el("table", { className: "grid" }, [
        el("thead", {}, el("tr", {}, [
          el("th", { textContent: "Person" }),
          el("th", { textContent: "Asked" }),
          el("th", { textContent: "" }),
        ])),
        waiting,
      ])),
    ]));
  }

  // ---- People --------------------------------------------------------------
  const rows = el("tbody");
  for (const person of users) {
    const isSelf = person.id === (session.user || {}).id;

    const roleSelect = el("select", { className: "control tiny" });
    for (const role of ["user", "admin"]) {
      roleSelect.append(el("option", { value: role, textContent: role === "admin" ? "Admin" : "User",
                                       selected: person.role === role }));
    }
    roleSelect.addEventListener("change", () =>
      patchUser(person.id, { role: roleSelect.value }));

    const actions = el("div", { className: "row-actions" });
    actions.append(el("button", {
      className: "btn xs ghost",
      textContent: person.is_active ? "Disable" : "Enable",
      onclick: () => patchUser(person.id, { is_active: !person.is_active }),
    }));
    actions.append(el("button", {
      className: "btn xs ghost", textContent: "Reset link",
      onclick: () => resetLinkFor(person),
    }));
    if (!isSelf) {
      actions.append(el("button", {
        className: "btn xs danger", textContent: "Delete",
        onclick: async () => {
          if (!confirm(`Delete ${person.email}? Their posted rows stay in the workbook.`)) return;
          try { await api(`/api/admin/users/${person.id}`, { method: "DELETE" }); }
          catch (err) { toast(err.message, "error"); return; }
          toast("Deleted.");
          renderAdmin();
        },
      }));
    }

    rows.append(el("tr", { className: person.is_active ? "" : "dim" }, [
      el("td", {}, [
        el("div", { className: "cell-main", textContent: person.name }),
        el("div", { className: "cell-sub", textContent: person.email }),
      ]),
      el("td", {}, roleSelect),
      // Three states, not two: waiting to be let in is not the same as
      // switched off, and an administrator acts differently on each.
      el("td", {}, person.awaiting_approval
        ? el("span", { className: "pill needs_review" }, [icon("clock", 12), "Awaiting approval"])
        : el("span", { className: `pill ${person.is_active ? "ready" : "new"}` },
            [icon(person.is_active ? "check" : "dot", 12),
             person.is_active ? "Active" : "Disabled"])),
      el("td", { className: "cell-sub", textContent: person.last_login_at
        ? person.last_login_at.replace("T", " ").slice(0, 16) : "never" }),
      el("td", {}, actions),
    ]));
  }

  body.append(el("div", { className: "card" }, [
    el("header", {}, [
      el("h2", { textContent: "People" }),
      el("span", { className: "grow" }),
      el("button", { className: "btn sm primary", textContent: "Add someone",
                     onclick: () => showNewUserForm() }),
    ]),
    el("div", { id: "new-user-slot" }),
    el("div", { className: "table-wrap" }, el("table", { className: "grid" }, [
      el("thead", {}, el("tr", {}, [
        el("th", { textContent: "Person" }), el("th", { textContent: "Role" }),
        el("th", { textContent: "Status" }), el("th", { textContent: "Last signed in" }),
        el("th", { textContent: "" }),
      ])),
      rows,
    ])),
  ]));

  // ---- Failed jobs ---------------------------------------------------------
  if (stats.failed_jobs.length) {
    body.append(el("div", { className: "card" }, [
      el("header", {}, [el("h2", { textContent: "Failed invoices" })]),
      historyTable(stats.failed_jobs),
    ]));
  }

  // ---- Overrides: the most audit-sensitive thing anyone can do -------------
  if (stats.overrides.length) {
    body.append(el("div", { className: "card" }, [
      el("header", {}, [
        el("h2", { textContent: "Posted despite a failed check" }),
        el("span", { className: "grow" }),
        el("span", { className: "pill needs_review" }, [icon("alert", 12), "Override"]),
      ]),
      el("div", { className: "card-body" }, el("p", { className: "muted" },
        "Someone chose to file these even though validation objected. Each one records who.")),
      historyTable(stats.overrides),
    ]));
  }

  // ---- Activity ------------------------------------------------------------
  body.append(el("div", { className: "card" }, [
    el("header", {}, [el("h2", { textContent: "Activity" })]),
    el("div", { className: "table-wrap" }, el("table", { className: "grid log" }, [
      el("thead", {}, el("tr", {}, [
        el("th", { textContent: "When" }), el("th", { textContent: "Who" }),
        el("th", { textContent: "What" }), el("th", { textContent: "Detail" }),
      ])),
      el("tbody", {}, log.map((entry) => el("tr", {}, [
        el("td", { className: "cell-sub", textContent: entry.at.replace("T", " ").slice(0, 16) }),
        el("td", { textContent: entry.user_email || "—" }),
        el("td", {}, el("code", { textContent: entry.action })),
        el("td", { className: "cell-sub", textContent: entry.detail || "" }),
      ]))),
    ])),
  ]));

  // ---- System --------------------------------------------------------------
  body.append(el("div", { className: "card" }, [
    el("header", {}, [el("h2", { textContent: "System" })]),
    el("div", { className: "card-body" }, el("div", { className: "kv" }, [
      el("span", { className: "k" }, "Reader"),
      el("span", { className: "v" }, stats.reader),
      el("span", { className: "k" }, "Return periods held"),
      el("span", { className: "v" }, (stats.periods || []).join(", ") || "—"),
      el("span", { className: "k" }, "Data directory"),
      el("span", { className: "v mono", textContent: stats.data_dir }),
    ])),
  ]));
}

function showNewUserForm() {
  const slot = $("#new-user-slot");
  slot.textContent = "";

  const name = el("input", { className: "control", placeholder: "Name" });
  const email = el("input", { className: "control", type: "email", placeholder: "Email" });
  const password = el("input", { className: "control", type: "password",
                                 placeholder: "Temporary password, 10+ characters" });
  const role = el("select", { className: "control" }, [
    el("option", { value: "user", textContent: "User" }),
    el("option", { value: "admin", textContent: "Admin" }),
  ]);

  slot.append(el("div", { className: "card-body inset form-row" }, [
    name, email, password, role,
    el("button", {
      className: "btn primary", textContent: "Create",
      onclick: async () => {
        try {
          await api("/api/admin/users", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name.value, email: email.value,
                                   password: password.value, role: role.value }),
          });
          toast("Account created.", "good");
          renderAdmin();
        } catch (err) { toast(err.message, "error"); }
      },
    }),
    el("button", { className: "btn ghost", textContent: "Cancel",
                   onclick: () => { slot.textContent = ""; } }),
  ]));
}

async function approvePerson(person, role) {
  try {
    await api(`/api/admin/users/${person.id}/approve?role=${role}`, { method: "POST" });
    toast(`${person.name || person.email} can sign in now.`, "good");
  } catch (err) {
    toast(err.message, "error");
  }
  renderAdmin();
}

async function patchUser(userId, changes) {
  try {
    await api(`/api/admin/users/${userId}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    });
    toast("Saved.", "good");
  } catch (err) {
    toast(err.message, "error");
  }
  renderAdmin();
}

async function resetLinkFor(person) {
  try {
    const result = await api(`/api/admin/users/${person.id}/reset-link`, { method: "POST" });
    // No mail server, so the link is shown for an administrator to pass on by
    // whatever channel they already trust.
    window.prompt(
      `Password reset link for ${result.email}. It expires in `
      + `${result.expires_in_minutes} minutes. Copy it and send it to them.`,
      result.link,
    );
  } catch (err) {
    toast(err.message, "error");
  }
}

// --------------------------------------------------------------------------- //
// Boot
// --------------------------------------------------------------------------- //

function paintIdentity() {
  const user = session.user || {};
  const initials = (user.name || user.email || "?")
    .split(/[\s@._-]+/).filter(Boolean).slice(0, 2).map((p) => p[0].toUpperCase()).join("");
  $("#who-initials").textContent = initials || "?";
  $("#who-name").textContent = user.name || user.email || "—";
  $("#who-role").textContent = user.role === "admin" ? "Administrator" : "User";
  $("#nav-admin").hidden = !isAdmin();
}

async function enterApp() {
  $("#boot").hidden = true;
  $("#gate").hidden = true;
  $("#shell").hidden = false;
  paintIdentity();
  await loadInfo();
  await loadDocuments();
  show(screenFromHash(), { push: false });
}

function resetTokenFromHash() {
  const match = (location.hash || "").match(/[?&]token=([^&]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

/* Anything that escapes start() would otherwise leave the loading card on
   screen for ever, with the real reason sitting only in the console. */
function bootFailed(err) {
  const boot = $("#boot");
  if (!boot) return;
  boot.hidden = false;
  const message = boot.querySelector(".boot-msg");
  if (message) {
    message.textContent = `Could not start: ${err && err.message ? err.message : err}`;
    message.style.color = "var(--critical)";
  }
}

(async function start() {
  try {
    await boot();
  } catch (err) {
    bootFailed(err);
    throw err;
  }
})();

async function boot() {
  wire();

  // A reset link is a way in, so it is checked before any session is.
  const resetToken = resetTokenFromHash();
  if (location.hash.startsWith("#/reset") && resetToken) {
    gate.mode = "reset";
    gate.resetToken = resetToken;
    renderGate();
    return;
  }

  let setup = { needs_setup: false };
  try {
    setup = await api("/api/bootstrap");
    gate.signupMode = setup.signup_mode || "closed";
    if (setup.company) $("#gate-company").textContent = setup.company;
  } catch (err) {
    renderGate();
    gateAlert(`Cannot reach the backend at ${API}. Start it with: `
              + "cd backend, then .\\run.ps1", "error");
    return;
  }

  if (setup.needs_setup) {
    gate.mode = "setup";
    renderGate();
    return;
  }

  if (!session.token) {
    renderGate();
    return;
  }

  try {
    session.user = await api("/api/auth/me");
    await enterApp();
  } catch (err) {
    // A token that no longer works is not an error worth alarming anyone with.
    setToken("");
    renderGate();
    if (!(err instanceof Unauthenticated)) gateAlert(err.message);
  }
}
