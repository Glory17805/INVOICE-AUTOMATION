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
  openPanel: null,

  // Export screen only: "All periods" is chosen instead of one period. Kept
  // apart from `period` so the rest of the app never sees a period that is
  // not a real one.
  exportAll: false,
  theme: "system",

  // The batch the Processing screen is watching, and the History screen's
  // current search - both survive navigating away and back.
  lastBatch: [],
  historyQuery: "",
  historyFilter: "",
  settings: {},

  // Which sidebar groups are expanded. Remembered across navigation so the
  // navigation does not reshuffle itself under the pointer.
  navOpen: {},
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
  doc: "M4.5 1.8h5l3 3v9.4h-8zM9.5 1.8v3h3M6 8.5h4M6 11h2.5",
  rupee: "M5 3h6M5 5.8h6M8.6 3c1.6 0 2.6.9 2.6 2.2 0 1.5-1.2 2.4-3 2.4H5l5.4 5.4",
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
  await download(`/api/workbook/download?period=${encodeURIComponent(period)}`,
                 `Ira Innovations GST ${period}.xlsx`);
}

/* Every period at once. The backend zips one workbook per period rather than
   merging them, so what arrives is still one filable return per file. */
async function saveAllWorkbooks() {
  const stamp = new Date().toISOString().slice(0, 10);
  toast("Gathering every period…");
  await download("/api/workbook/download-all",
                 `Ira Innovations GST workbooks ${stamp}.zip`);
}

async function download(path, filename) {
  try {
    const objectUrl = await blobUrl(path);
    const link = el("a", { href: objectUrl, download: filename });
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
  // The company is named in the account chip and on the Rules screen; the
  // sidebar carries the product mark rather than repeating it.
  document.title = `${state.info.company} — InvoiceFlow`;

  /* Three states, not two. Holding a key is not the same as the model
     answering: an expired key, an exhausted balance or a dropped network all
     fall back to the offline reader. Naming the configured provider through
     that would tell someone their invoices were read by a model that never
     saw them. */
  const PROVIDER_NAMES = {
    claude: "Claude", gemini: "Gemini",
    // Offline is a provider in its own right, not a fallback. Both spellings
    // appear: "offline" is what is configured, "heuristic" is the reader that
    // actually runs.
    offline: "Offline", heuristic: "Offline",
  };
  const provider = state.info.provider || state.info.reader;
  const readerName = PROVIDER_NAMES[provider] || provider || "Offline";

  // Whether an external service is meant to be doing the reading. Offline is
  // not one: it cannot be unreachable, so it can never be "degraded". Reading
  // it as one produced the nonsense "offline could not be reached, so invoices
  // are being read offline".
  const usesService = Boolean(provider) && provider !== "heuristic" && provider !== "offline";
  const effective = state.info.reader_effective;
  const degraded = usesService && effective === "heuristic";

  const dot = $("#reader-dot");
  const label = $("#reader-label");

  if (!usesService) {
    // Not a warning. Reading offline is a supported way to run this, and on
    // the invoices it handles it reads every field - so the dot stays neutral
    // and the tooltip says what it does rather than what it lacks.
    dot.className = "dot";
    label.textContent = "Offline reader";
    label.title = "Invoices are read on this machine, with no external service. "
      + "Scans need OCR installed; everything else is read from the document itself.";
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

/* Keeps the chosen period valid. The picker itself now lives on the screens
   that actually read the workbook, rather than in a bar above every screen
   where changing it appeared to do nothing. */
function renderPeriods() {
  const known = state.info.periods || [];
  if (!state.period || !known.includes(state.period)) {
    state.period = known[0] || state.info.master_period;
  }
}

/* A period picker, built where it is needed. */
function periodPicker(onChange) {
  const picker = el("select", { className: "control" });
  for (const period of state.info.periods || []) {
    picker.append(el("option", { value: period, textContent: period,
                                 selected: period === state.period }));
  }
  if (!(state.info.periods || []).length) {
    picker.append(el("option", { textContent: state.period || "—" }));
  }
  picker.addEventListener("change", () => { state.period = picker.value; onChange(); });
  return el("div", { className: "field compact" }, [
    el("label", { textContent: "Return period" }), picker,
  ]);
}

async function loadDocuments() {
  // Whole documents, not summaries: the Queue and Review screens read the
  // extraction, the treatment and the issue list off these.
  const page = await api("/api/documents?view=full");
  state.documents = page.documents;
  for (const id of [...state.selected]) {
    if (!state.documents.some((d) => d.id === id && d.status === "ready")) state.selected.delete(id);
  }
  paintPips();
}

/* The counts on the navigation and the bell. Called from loadDocuments and
   again whenever the navigation is rebuilt, because rebuilding it throws the
   previous pip elements away. */
function paintPips() {
  const counts = tally();
  setPip("#pip-inbox", counts.open, counts.needs_review ? "attention" : "ready");
  setPip("#pip-review", counts.needs_review + counts.ready,
         counts.needs_review ? "attention" : "ready");

  /* The bell counts only what a person has to decide about - things flagged or
     failed. Counting everything in the queue would leave a permanent badge,
     which is the same as no badge. */
  const attention = counts.needs_review + counts.failed;
  const badge = $("#alerts-count");
  badge.textContent = String(attention);
  badge.hidden = !attention;
  $("#btn-alerts").title = attention
    ? `${attention} invoice${attention === 1 ? "" : "s"} need attention`
    : "Nothing needs attention";
}

function tally() {
  const c = { new: 0, needs_review: 0, ready: 0, posted: 0, failed: 0 };
  for (const doc of state.documents) c[doc.status] = (c[doc.status] || 0) + 1;
  c.open = c.new + c.needs_review + c.ready + c.failed;
  return c;
}

function setPip(sel, value, kind) {
  // A pip only exists while its group is expanded, so a missing one is normal
  // rather than a fault.
  const node = $(sel);
  if (!node) return;
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
    // Never disabled. A double-click is already refused inside postRow, and a
    // disabled attribute here can only ever be wrong: the list is not redrawn
    // while a post is in flight, so it would be painting a stale busy flag.
    acts.append(el("button", {
      type: "button", className: "btn sm primary", textContent: "Post",
      onclick: (ev) => postRow(doc.id, ev.currentTarget),
    }));
  }
  if (doc.status === "posted") {
    acts.append(el("button", { className: "btn sm ghost", textContent: "Un-post", onclick: () => unpostOne(doc.id) }));
  }
  acts.append(deleteButton(doc));

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
      deleteButton(doc, { className: "btn danger" }),
    ]),
  ]);

  body.append(el("div", { className: "review-grid" }, [left, right]));

  /* What this supplier has done before, from invoices already posted. Shown,
     never used: the reader is not told any of this, because handing it a
     plausible prior invites it to report history instead of the document. */
  const seen = doc.supplier_history;
  if (seen && seen.invoice_count) {
    const facts = el("div", { className: "kv" });
    const add = (k, v) => {
      facts.append(el("span", { className: "k", textContent: k }));
      facts.append(el("span", { className: "v", textContent: v }));
    };
    add("Invoices filed", String(seen.invoice_count));
    if (seen.gstin) add("GSTIN used", seen.gstin);
    if (seen.usual_rate != null) {
      const rates = (seen.rates_seen || []).length;
      add("Usual rate", `${(Number(seen.usual_rate) * 100).toFixed(2)}%`
        + (rates > 1 ? ` (${rates} different rates seen)` : ""));
    }
    if ((seen.registers || []).length) {
      add("Register", seen.registers.map((r) => REGISTER_SHEET[r] || r).join(", "));
    }
    if (seen.last_seen) add("Last filed", seen.last_seen.replace("T", " ").slice(0, 16));

    body.append(el("div", { className: "card", style: "margin-top:1rem" }, [
      el("header", {}, [
        el("h2", { textContent: `Previously from ${seen.name}` }),
        el("span", { className: "grow" }),
        el("span", { className: "muted", textContent: "from posted invoices only" }),
      ]),
      el("div", { className: "card-body" }, [
        facts,
        el("p", { className: "muted", style: "margin:.8rem 0 0" },
          "Context for you, not for the reader. Anything on this invoice that "
          + "disagrees with the above is raised as a warning at the top."),
      ]),
    ]));
  }

  /* The item table, when the reader found one. It is read-only on purpose: the
     register takes one row per invoice, so the lines are here to check the
     total against, not to be edited into it. Editing them would imply they
     drive something they do not. */
  const lines = (e.line_items || []).filter((line) => line && (
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
    const stated = Number(e.taxable_value || 0);
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

/* The bare call. It reports nothing and repaints nothing, because the two
   callers below need to control both: Review moves on to the next invoice,
   and a bulk post must not repaint once per invoice. Anything else should
   call postRow. */
async function postOne(docId, override = false) {
  const suffix = override ? "?override=true" : "";
  await api(`/api/documents/${docId}/confirm${suffix}`, { method: "POST" });
}

/* Posting one invoice from a list row. Without this the button looked dead:
   the row it was sitting in never changed, and a refusal from the backend -
   the workbook open in Excel, a period already filed - was thrown away
   unseen. */
async function postRow(docId, button) {
  if (state.busy) return;
  state.busy = true;

  // The button says so itself, straight away. Writing to the workbook can take
  // a moment, and a toast at the top of the screen is a long way from the row
  // being clicked.
  const label = button ? button.textContent : null;
  if (button) { button.disabled = true; button.textContent = "Posting…"; }

  try {
    await postOne(docId);
    toast("Posted to the workbook.", "good");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    state.busy = false;
    // Restored even though the refresh below usually replaces the row: if that
    // refresh fails, the button must not be left saying "Posting…" for good.
    if (button) { button.disabled = false; button.textContent = label; }
  }
  await refresh();
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

/* One delete, used from every screen that shows an invoice.

   A posted invoice cannot simply be deleted - the backend refuses it with a
   409, correctly, because a row of it is sitting in the workbook. That left
   someone looking at a posted document with no way forward except finding the
   un-post button on a different screen. So this handles the posted case
   itself: it says plainly that the workbook row goes too, then un-posts and
   deletes as one action.

   Takes the document rather than an id so it can tell the two cases apart
   without a second fetch. */
async function deleteInvoice(doc) {
  const name = doc.invoice_number
    || (doc.extracted && doc.extracted.invoice_number)
    || (doc.treatment && doc.treatment.counterparty_name)
    || doc.filename
    || "this invoice";
  const posted = doc.status === "posted";

  const question = posted
    ? `Delete ${name}?

It is posted to ${doc.sheet || "the workbook"}`
      + `${doc.row ? ` row ${doc.row}` : ""} for ${doc.period || "this period"}. `
      + `That row will be cleared as well, and the totals will recalculate.`
    : `Delete ${name}?

The invoice and its stored copy are removed from the queue.`;

  if (!confirm(question)) return false;

  try {
    if (posted) {
      // Clear the register row first; the delete is refused while it stands.
      await api(`/api/documents/${doc.id}/unpost`, { method: "POST" });
    }
    await api(`/api/documents/${doc.id}`, { method: "DELETE" });
    toast(posted ? `Deleted, and the workbook row was cleared.` : "Deleted.", "good");
  } catch (err) {
    toast(err.message, "error");
    return false;
  }
  await refresh();
  return true;
}


/* The button, so every screen gets the same affordance rather than each one
   inventing its own wording. */
function deleteButton(doc, { label = "Delete", className = "btn sm danger" } = {}) {
  return el("button", {
    className,
    textContent: label,
    title: doc.status === "posted"
      ? "Delete this invoice and clear its row from the workbook"
      : "Delete this invoice",
    onclick: (event) => { event.stopPropagation(); deleteInvoice(doc); },
  });
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
    let result = await api("/api/documents", { method: "POST", body: form });
    if (result.errors && result.errors.length) toast(result.errors.join("; "), "error");

    /* Files the system already holds. Skipped rather than read again - the
       usual cause is uploading the same batch twice, and doing that quietly
       leaves two identical sets of rows to work through. Insisting is allowed;
       it just has to be deliberate. */
    if (result.duplicates && result.duplicates.length) {
      const again = confirm(
        `${result.duplicates.join("\n\n")}\n\nUpload ${result.duplicates.length > 1
          ? "them" : "it"} again anyway?`
      );
      if (again) {
        const retry = new FormData();
        for (const file of good) retry.append("files", file);
        result = await api("/api/documents?allow_duplicates=true",
                           { method: "POST", body: retry });
      } else if (!result.captured.length) {
        toast("Nothing new to read.");
        await refresh();
        return;
      }
    }

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

  const tabs = el("div", { className: "filters" }, [
    ...REGISTERS.map(([key, label, sheet]) => el("button", {
      className: `btn sm${state.register === key ? " primary" : ""}`,
      textContent: label,
      title: sheet,
      onclick: () => { state.register = key; renderRegisters(); },
    })),
    el("span", { className: "grow" }),
    periodPicker(renderRegisters),
  ]);
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
  body.append(el("div", { className: "filters" }, [
    el("span", { className: "grow" }), periodPicker(renderTax),
  ]));

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

/* The signed-in person, on the screen they land on. It was only in the sidebar
   footer, which is below the fold on a short window and hidden entirely when
   the sidebar collapses. */
function profileCard() {
  const user = session.user || {};
  const name = user.name || user.email || "Signed in";
  const initials = (user.name || user.email || "?")
    .split(/[\s@._-]+/).filter(Boolean).slice(0, 2)
    .map((part) => part[0].toUpperCase()).join("") || "?";
  const isAdmin = user.role === "admin";

  const facts = el("div", { className: "facts" });
  if (state.info && state.info.company) {
    facts.append(el("div", { className: "f" }, [
      el("div", { className: "k", textContent: "Filing for" }),
      el("div", { className: "v", textContent: state.info.company }),
    ]));
  }
  facts.append(el("div", { className: "f" }, [
    el("div", { className: "k", textContent: "Return period" }),
    el("div", { className: "v", textContent: state.period || "—" }),
  ]));
  if (state.info && state.info.reader) {
    facts.append(el("div", { className: "f" }, [
      el("div", { className: "k", textContent: "Reader" }),
      el("div", { className: "v", textContent:
        state.info.reader === "heuristic" ? "Offline" : state.info.reader }),
    ]));
  }

  return el("div", { className: "profile-card" }, [
    el("div", { className: "avatar-lg", textContent: initials }),
    el("div", { className: "who-block" }, [
      el("div", { className: "nm" }, [
        name, " ",
        el("span", { className: `role-chip${isAdmin ? " admin" : ""}`,
                     textContent: isAdmin ? "Administrator" : "User" }),
      ]),
      el("div", { className: "em", textContent: user.email || "" }),
    ]),
    facts,
    el("div", { className: "acts" }, [
      el("button", { className: "btn sm", textContent: "Account settings",
                     onclick: () => show("settings") }),
      el("button", { className: "btn sm ghost", textContent: "Sign out",
                     onclick: () => signOut() }),
    ]),
  ]);
}

// --------------------------------------------------------------------------- //
// Theme
// --------------------------------------------------------------------------- //

/* Three states rather than a two-way switch. "System" is the default and keeps
   following the operating system, which is what most people actually want;
   Light and Dark are an explicit override that survives a reload. Storing the
   override means the absence of a stored value IS "system", so there is no
   fourth state to reconcile. */
const THEMES = [
  ["system", "System", "M8 1.5v13M14.5 8a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0z"],
  ["light", "Light", "M8 3.2V1.8M8 14.2v-1.4M12.4 3.6l-1 1M4.6 11.4l-1 1M14.2 8h-1.4M3.2 8H1.8M12.4 12.4l-1-1M4.6 4.6l-1-1M11 8a3 3 0 1 1-6 0 3 3 0 0 1 6 0z"],
  ["dark", "Dark", "M13.5 9.3A5.8 5.8 0 0 1 6.7 2.5 5.9 5.9 0 1 0 13.5 9.3z"],
];

function storedTheme() {
  try {
    const value = localStorage.getItem("gst.theme");
    return value === "light" || value === "dark" ? value : "system";
  } catch (_) {
    return "system";
  }
}

function applyTheme(choice) {
  const root = document.documentElement;
  if (choice === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", choice);
  try {
    if (choice === "system") localStorage.removeItem("gst.theme");
    else localStorage.setItem("gst.theme", choice);
  } catch (_) { /* the choice still applies for this page */ }
  state.theme = choice;
  renderThemeSwitch();
}

function renderThemeSwitch() {
  const box = $("#theme-switch");
  if (!box) return;
  box.textContent = "";
  for (const [value, label, path] of THEMES) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("width", "13"); svg.setAttribute("height", "13");
    svg.setAttribute("fill", "none"); svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.5");
    svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    const d = document.createElementNS("http://www.w3.org/2000/svg", "path");
    d.setAttribute("d", path);
    svg.append(d);

    box.append(el("button", {
      type: "button",
      "aria-pressed": String(state.theme === value),
      title: value === "system" ? "Follow the operating system" : `${label} mode`,
      onclick: () => applyTheme(value),
    }, [svg, el("span", { className: "seg-label", textContent: label })]));
  }
}

// --------------------------------------------------------------------------- //
// Navigation
// --------------------------------------------------------------------------- //

const SCREENS = [
  "dashboard", "upload", "processing", "queue", "review", "history",
  "export", "registers", "tax",
  "vendors", "categories", "templates", "rules",
  "settings", "admin", "audit",
];

// Screens only an administrator may open. Checked here as well as on the
// server, which is the half that actually enforces it.
const ADMIN_SCREENS = new Set(["admin", "audit"]);

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
  if (ADMIN_SCREENS.has(name) && !isAdmin()) {
    toast("That needs an administrator account.", "error");
    name = "dashboard";
  }

  state.screen = name;
  if (push && screenFromHash() !== name) location.hash = `#/${name}`;

  // A child screen reached from elsewhere leaves its parent group open, rather
  // than collapsing the thing the current page sits inside.
  const parent = groupHolding(name);
  if (parent) state.navOpen[parent.label] = true;
  renderNav();

  for (const id of SCREENS) $(`#screen-${id}`).hidden = id !== name;

  /* The page's own heading moves into the top bar, so the title sits on the
     same line as the search and the controls. Read from the section rather
     than kept in a second list here - a new screen brings its own words. */
  const section = $(`#screen-${name}`);
  const source = section ? section.querySelector("header") : null;
  const heading = source ? source.querySelector("h1") : null;
  const lede = source ? source.querySelector("p") : null;
  $("#page-title").textContent = heading ? heading.textContent : "Dashboard";
  $("#page-lede").textContent = lede ? lede.textContent : "";
  if (source) source.hidden = true;

  // A greeting belongs on the screen you land on, not on every screen.
  if (name === "dashboard") {
    const hour = new Date().getHours();
    const part = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
    const who = (session.user || {}).name || "";
    if (who) $("#page-title").textContent = `${part}, ${who.split(" ")[0]}`;
  }

  const renderers = {
    dashboard: renderDashboard,
    upload: renderUpload,
    processing: renderProcessing,
    queue: renderInbox,
    review: renderReview,
    history: renderHistory,
    export: renderExport,
    registers: renderRegisters,
    tax: renderTax,
    vendors: renderVendors,
    categories: renderCategories,
    templates: renderTemplates,
    rules: renderRules,
    settings: renderSettings,
    admin: renderAdmin,
    audit: renderAudit,
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
  applyTheme(storedTheme());
  $("#gate-form").addEventListener("submit", submitGate);

  $("#btn-logout").addEventListener("click", async () => {
    try { await api("/api/auth/logout", { method: "POST" }); } catch (_) { /* going anyway */ }
    signOut();
  });
  // The account chip opens a small menu rather than jumping straight to
  // settings: signing out is the other thing people come here for.
  const menu = $("#who-menu");
  $("#who").addEventListener("click", (event) => {
    event.stopPropagation();
    menu.hidden = !menu.hidden;
  });
  document.addEventListener("click", () => { menu.hidden = true; });
  menu.addEventListener("click", (event) => {
    const target = event.target.closest("[data-screen]");
    if (target) { menu.hidden = true; show(target.dataset.screen); }
  });

  $("#btn-guide").addEventListener("click", () => show("rules"));

  // The navigation is rebuilt on every screen change, so its buttons carry
  // their own handlers rather than being wired once from here.

  const input = $("#file-input");
  input.addEventListener("change", () => { upload([...input.files]); input.value = ""; });
  wireDropzone($("#dropzone"));

  /* Search from anywhere lands on History with the term already applied,
     rather than being a second, weaker search of its own. */
  const search = $("#global-search");
  search.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    state.historyQuery = search.value.trim();
    state.historyFilter = "";
    show("history");
  });

  // The bell is a shortcut to the work, not a notification centre.
  $("#btn-activity").addEventListener("click", (e) => { e.stopPropagation(); openPanel("activity"); });

  // A panel that only closes by clicking its own button is a panel people leave
  // open by accident.
  document.addEventListener("click", (e) => {
    if (state.openPanel && !e.target.closest("#popover")) closePanel();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.openPanel) closePanel();
  });

  $("#btn-alerts").addEventListener("click", () => {
    const counts = tally();
    if (counts.needs_review) { state.filter = "needs_review"; show("queue"); }
    else if (counts.failed) { state.historyFilter = "failed"; show("history"); }
    else toast("Nothing needs your attention.", "good");
  });

  // One button, cycling light -> dark -> follow the system, as the mockup has
  // it. The three-way segmented control it replaces is still reachable from
  // Settings for anyone who wants to pick explicitly.
  $("#btn-theme").addEventListener("click", () => {
    const order = ["light", "dark", "system"];
    const next = order[(order.indexOf(state.theme) + 1) % order.length];
    applyTheme(next);
    toast(next === "system" ? "Following your system theme" : `${next} theme`);
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
      el("p", { className: "gate-lede" },
        "Create your account and you will be signed straight in."),
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
    if (gate.signupMode === "open") {
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

// --------------------------------------------------------------------------- //
// Charts
//
// Hand-drawn inline SVG. There is no build step and the Content-Security-Policy
// forbids loading anything off a CDN, so a charting library was never an option
// - which turns out to cost very little for four small figures.
//
// Marks follow the house rules: hairline recessive grid, thin strokes, a legend
// or direct label wherever more than one thing is drawn, and status colour that
// is always accompanied by a word and a number. Nothing here asks anyone to
// compare two shapes by eye to get the answer.
// --------------------------------------------------------------------------- //

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null) continue;
    node.setAttribute(key, String(value));
  }
  for (const child of [].concat(children)) {
    if (child) node.append(child);
  }
  return node;
}

/* Reads a CSS custom property so charts follow the theme rather than hard-coding
   a colour that would be wrong in the other one. */
function token(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

/* A sparkline: one series, no axes, no legend. The tile's own label says what it
   is, and the exact number is right beside it - this only carries the shape. */
function sparkline(values, { width = 68, height = 30 } = {}) {
  const svg = svgEl("svg", {
    width, height, viewBox: `0 0 ${width} ${height}`,
    role: "img", "aria-label": "trend over the last 30 days", class: "spark",
  });
  const points = values.length ? values : [0];
  const top = Math.max(...points, 1);
  const step = points.length > 1 ? width / (points.length - 1) : width;
  const y = (v) => height - 3 - (v / top) * (height - 6);

  const path = points.map((v, i) => `${i ? "L" : "M"}${(i * step).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  svg.append(svgEl("path", {
    d: path, fill: "none", stroke: token("--viz-1", "#2a78d6"),
    "stroke-width": 1.6, "stroke-linecap": "round", "stroke-linejoin": "round",
  }));
  // The endpoint, direct-labelled by position rather than by a number on it.
  svg.append(svgEl("circle", {
    cx: (points.length - 1) * step, cy: y(points[points.length - 1]), r: 2.4,
    fill: token("--viz-1", "#2a78d6"),
  }));
  return svg;
}

/* A donut, for part-to-whole at a glance. Legitimate here because the segments
   are few and one dominates; it would be the wrong form for comparing close
   values. The legend beside it carries the exact count and share, so the arcs
   are the summary and never the source. */
function donut(segments, { size = 168, thickness = 22 } = {}) {
  const total = segments.reduce((sum, s) => sum + s.value, 0);
  const radius = (size - thickness) / 2;
  const centre = size / 2;
  const circumference = 2 * Math.PI * radius;
  const surface = token("--surface", "#fff");

  const svg = svgEl("svg", {
    width: size, height: size, viewBox: `0 0 ${size} ${size}`,
    role: "img",
    "aria-label": total
      ? `${total} documents: ` + segments.filter((s) => s.value)
          .map((s) => `${s.value} ${s.label}`).join(", ")
      : "nothing yet",
  });

  svg.append(svgEl("circle", {
    cx: centre, cy: centre, r: radius, fill: "none",
    stroke: token("--surface-3", "#eee"), "stroke-width": thickness,
  }));

  let offset = 0;
  for (const segment of segments) {
    if (!segment.value) continue;
    const length = (segment.value / total) * circumference;
    svg.append(svgEl("circle", {
      cx: centre, cy: centre, r: radius, fill: "none",
      stroke: segment.color, "stroke-width": thickness,
      // A 2px gap of surface between neighbours, rather than a border on each.
      "stroke-dasharray": `${Math.max(length - 2, 0.5)} ${circumference}`,
      "stroke-dashoffset": -offset,
      transform: `rotate(-90 ${centre} ${centre})`,
    }));
    offset += length;
  }
  // Masks the seam where the first segment starts.
  svg.append(svgEl("circle", {
    cx: centre, cy: centre, r: radius - thickness / 2, fill: "none",
    stroke: surface, "stroke-width": 0,
  }));

  svg.append(svgEl("text", {
    x: centre, y: centre - 2, "text-anchor": "middle",
    "font-size": 26, "font-weight": 680, fill: token("--ink", "#111"),
    "letter-spacing": "-0.02em",
  }, document.createTextNode(String(total))));
  svg.append(svgEl("text", {
    x: centre, y: centre + 16, "text-anchor": "middle",
    "font-size": 11, fill: token("--ink-3", "#777"),
  }, document.createTextNode("documents")));

  return svg;
}

/* An area chart over time, with a crosshair and tooltip. One series, so the
   title names it and no legend is needed. */
function areaChart(points, { height = 190 } = {}) {
  const width = 640;                       // viewBox units; the SVG scales to fit
  const pad = { top: 12, right: 12, bottom: 24, left: 34 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const values = points.map((p) => p.count);
  const top = Math.max(...values, 4);
  const niceTop = Math.ceil(top / 4) * 4;
  const x = (i) => pad.left + (points.length > 1 ? (i / (points.length - 1)) * plotW : plotW / 2);
  const y = (v) => pad.top + plotH - (v / niceTop) * plotH;

  const svg = svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none",
    role: "img", "aria-label": "invoices received per day over the last 30 days",
  });

  // Hairline grid, solid - dashes read as a threshold when they are just a grid.
  for (let step = 0; step <= 4; step += 1) {
    const value = (niceTop / 4) * step;
    svg.append(svgEl("line", {
      x1: pad.left, x2: width - pad.right, y1: y(value), y2: y(value),
      stroke: token("--viz-grid", "#eee"), "stroke-width": 1,
    }));
    svg.append(svgEl("text", {
      x: pad.left - 7, y: y(value) + 3.5, "text-anchor": "end",
      "font-size": 10, fill: token("--viz-axis", "#999"),
    }, document.createTextNode(String(Math.round(value)))));
  }

  const line = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.count).toFixed(1)}`).join(" ");
  const accent = token("--viz-1", "#2a78d6");

  const fillId = `areafill-${Math.random().toString(36).slice(2, 8)}`;
  const gradient = svgEl("linearGradient", { id: fillId, x1: 0, y1: 0, x2: 0, y2: 1 }, [
    svgEl("stop", { offset: "0%", "stop-color": accent, "stop-opacity": 0.22 }),
    svgEl("stop", { offset: "100%", "stop-color": accent, "stop-opacity": 0 }),
  ]);
  svg.append(svgEl("defs", {}, gradient));
  svg.append(svgEl("path", {
    d: `${line} L${x(points.length - 1)},${pad.top + plotH} L${x(0)},${pad.top + plotH} Z`,
    fill: `url(#${fillId})`, stroke: "none",
  }));
  svg.append(svgEl("path", {
    d: line, fill: "none", stroke: accent, "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round",
  }));

  // A few dates only. One label per day would collide and go unread.
  const ticks = [0, Math.floor(points.length / 3), Math.floor((points.length * 2) / 3),
                 points.length - 1];
  for (const i of [...new Set(ticks)]) {
    const day = points[i];
    if (!day) continue;
    svg.append(svgEl("text", {
      x: x(i), y: height - 6, "text-anchor": i === 0 ? "start"
        : i === points.length - 1 ? "end" : "middle",
      "font-size": 10, fill: token("--viz-axis", "#999"),
    }, document.createTextNode(shortDate(day.date))));
  }

  const crosshair = svgEl("line", {
    y1: pad.top, y2: pad.top + plotH, stroke: token("--viz-axis", "#999"),
    "stroke-width": 1, opacity: 0,
  });
  const marker = svgEl("circle", {
    r: 4, fill: accent, stroke: token("--surface", "#fff"), "stroke-width": 2, opacity: 0,
  });
  svg.append(crosshair, marker);

  return { svg, x, y, points, crosshair, marker, width, pad, plotW };
}

function shortDate(iso) {
  const [, month, day] = iso.split("-");
  const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${Number(day)} ${names[Number(month) - 1] || ""}`.trim();
}

/* Wraps a chart in a positioned holder and wires the hover layer. An HTML chart
   is interactive by nature; a static one throws away the detail it already has. */
function timeChart(points) {
  const hold = el("div", { className: "chart-hold" });
  const built = areaChart(points);
  const tip = el("div", { className: "viz-tip" });
  hold.append(built.svg, tip);

  const place = (event) => {
    const box = built.svg.getBoundingClientRect();
    const scale = built.width / box.width;
    const localX = (event.clientX - box.left) * scale;
    let nearest = 0;
    let best = Infinity;
    for (let i = 0; i < points.length; i += 1) {
      const distance = Math.abs(built.x(i) - localX);
      if (distance < best) { best = distance; nearest = i; }
    }
    const day = points[nearest];
    built.crosshair.setAttribute("x1", built.x(nearest));
    built.crosshair.setAttribute("x2", built.x(nearest));
    built.crosshair.setAttribute("opacity", 0.45);
    built.marker.setAttribute("cx", built.x(nearest));
    built.marker.setAttribute("cy", built.y(day.count));
    built.marker.setAttribute("opacity", 1);

    tip.textContent = `${shortDate(day.date)} · ${day.count} invoice${day.count === 1 ? "" : "s"}`;
    tip.style.left = `${(built.x(nearest) / built.width) * box.width}px`;
    tip.style.top = `${((built.y(day.count) / 190) * box.height)}px`;
    tip.classList.add("on");
  };

  built.svg.addEventListener("pointermove", place);
  built.svg.addEventListener("pointerleave", () => {
    tip.classList.remove("on");
    built.crosshair.setAttribute("opacity", 0);
    built.marker.setAttribute("opacity", 0);
  });
  return hold;
}

// --------------------------------------------------------------------------- //
// Navigation
//
// Built from a description rather than written out in the markup, because
// several entries are groups that expand and one is admin-only. Every entry
// leads somewhere real: nothing here is a link to a screen that does not exist.
// --------------------------------------------------------------------------- //

const NAV_ICON = {
  dashboard: "M2 2.2h5.2v5.2H2zM8.8 2.2H14v5.2H8.8zM2 8.8h5.2V14H2zM8.8 8.8H14V14H8.8z",
  upload:    "M8 11V3m0 0L5 6m3-3 3 3M2.5 12.5h11",
  invoices:  "M4.5 1.8h5l3 3v9.4h-8zM9.5 1.8v3h3M6 8.5h4M6 11h2.5",
  gears:     "M8 2.2a5.8 5.8 0 0 1 5.5 4M13.8 8A5.8 5.8 0 0 1 8 13.8M2.5 6A5.8 5.8 0 0 1 8 2.2"
             + "M8 13.8A5.8 5.8 0 0 1 2.2 8M12 1.6v3h-3M4 14.4v-3h3",
  export:    "M8 2v7m0 0L5.4 6.4M8 9l2.6-2.6M2.5 11v2.5h11V11",
  reports:   "M2.4 13.2h11.2M4.6 13V8.4M7.5 13V4.2M10.4 13V6.8M13.3 13V9.6",
  vendors:   "M5.6 7.4a2.2 2.2 0 1 0 0-4.4 2.2 2.2 0 0 0 0 4.4zM1.8 13.4c0-2.1 1.7-3.4 3.8-3.4"
             + "s3.8 1.3 3.8 3.4M11 4.2a1.9 1.9 0 1 1 0 3.8M11.6 10.2c1.6.2 2.6 1.3 2.6 3.2",
  category:  "M2 3.6h12M2 8h12M2 12.4h7",
  templates: "M2.4 2.6h11.2v3.2H2.4zM2.4 7.8h5v5.6h-5zM8.8 7.8h4.8v5.6H8.8z",
  rules:     "M3 2.6h10v10.8H3zM5.4 5.6h5.2M5.4 8h5.2M5.4 10.4h3",
  users:     "M6 7.6a2.3 2.3 0 1 0 0-4.6 2.3 2.3 0 0 0 0 4.6zM1.9 13.4c0-2.2 1.8-3.6 4.1-3.6"
             + "s4.1 1.4 4.1 3.6M11.4 4.4a1.9 1.9 0 1 1 0 3.8M12 10.4c1.5.3 2.4 1.4 2.4 3",
  settings:  "M8 10.2a2.2 2.2 0 1 0 0-4.4 2.2 2.2 0 0 0 0 4.4zM8 1.6v1.6M8 12.8v1.6M14.4 8h-1.6"
             + "M3.2 8H1.6M12.5 3.5l-1.1 1.1M4.6 11.4l-1.1 1.1M12.5 12.5l-1.1-1.1M4.6 4.6 3.5 3.5",
  audit:     "M8 4.4V8l2.4 1.4M14.2 8A6.2 6.2 0 1 1 8 1.8",
};

/* label, screen or children, icon, and whether it is admin-only. */
const NAV = [
  { label: "Dashboard", screen: "dashboard", icon: "dashboard" },

  { group: "Core" },
  { label: "Upload Invoices", screen: "upload", icon: "upload" },
  { label: "Invoices", icon: "invoices", children: [
      { label: "Queue", screen: "queue", pip: "pip-inbox" },
      { label: "Review", screen: "review", pip: "pip-review" },
      { label: "History", screen: "history" },
  ] },
  { label: "Processing", screen: "processing", icon: "gears" },
  { label: "Excel Export", screen: "export", icon: "export" },
  { label: "Reports", icon: "reports", children: [
      { label: "GST registers", screen: "registers" },
      { label: "Tax position", screen: "tax" },
  ] },

  { group: "Management" },
  { label: "Vendors", screen: "vendors", icon: "vendors" },
  { label: "Categories", screen: "categories", icon: "category" },
  { label: "Templates", screen: "templates", icon: "templates" },
  { label: "Rules", screen: "rules", icon: "rules" },

  { group: "Admin" },
  { label: "Users", screen: "admin", icon: "users", adminOnly: true },
  { label: "Settings", screen: "settings", icon: "settings" },
  { label: "Audit Logs", screen: "audit", icon: "audit", adminOnly: true },
];

function navIcon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "16");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.45");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.append(svgEl("path", { d: NAV_ICON[name] || NAV_ICON.dashboard }));
  return svg;
}

function chevron() {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", "13");
  svg.setAttribute("height", "13");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.6");
  svg.setAttribute("class", "chevron");
  svg.setAttribute("aria-hidden", "true");
  svg.append(svgEl("path", { d: "m6 4 4 4-4 4", "stroke-linecap": "round",
                             "stroke-linejoin": "round" }));
  return svg;
}

/* Which group, if any, contains a screen - so opening a child screen from
   somewhere else leaves its parent expanded rather than collapsed over it. */
function groupHolding(screen) {
  return NAV.find((entry) => entry.children
    && entry.children.some((child) => child.screen === screen));
}

function renderNav() {
  const nav = $("#nav");
  nav.textContent = "";
  const current = state.screen;

  for (const entry of NAV) {
    if (entry.group) {
      nav.append(el("div", { className: "nav-group", textContent: entry.group }));
      continue;
    }
    if (entry.adminOnly && !isAdmin()) continue;

    if (entry.children) {
      const holds = entry.children.some((child) => child.screen === current);
      const open = state.navOpen[entry.label] ?? holds;
      const button = el("button", {
        type: "button", "aria-expanded": String(open),
        onclick: () => { state.navOpen[entry.label] = !open; renderNav(); },
      }, [
        navIcon(entry.icon),
        el("span", { className: "label", textContent: entry.label }),
        chevron(),
      ]);
      button.setAttribute("aria-expanded", String(open));
      nav.append(button);

      if (open) {
        const sub = el("div", { className: "sub" });
        for (const child of entry.children) {
          sub.append(navButton(child, current));
        }
        nav.append(sub);
      }
      continue;
    }

    nav.append(navButton(entry, current));
  }

  // The pips are written by loadDocuments, which may have run already.
  paintPips();
}

function navButton(entry, current) {
  const button = el("button", {
    type: "button",
    onclick: () => show(entry.screen),
  }, [
    entry.icon ? navIcon(entry.icon) : null,
    el("span", { className: "label", textContent: entry.label }),
    entry.pip ? el("span", { className: "pip", id: entry.pip }) : null,
  ]);
  if (entry.screen === current) button.setAttribute("aria-current", "page");
  button.dataset.screen = entry.screen;
  return button;
}

/* A headline figure with its month-on-month change and a 30-day shape.

   The change is omitted rather than invented when last month had nothing to
   divide by: "+100%" against a base of zero is noise dressed as insight. */
function kpi(label, value, { tone = "blue", glyph = "doc", change = null, spark = null } = {}) {
  const delta = el("div", { className: "delta" });
  if (change === null || change === undefined) {
    delta.append(el("span", { textContent: "nothing to compare against yet" }));
  } else if (change === 0) {
    delta.append(el("span", { textContent: "level with the 30 days before" }));
  } else {
    const up = change > 0;
    delta.append(el("span", { className: up ? "up" : "down",
                              textContent: `${up ? "+" : ""}${change}%` }));
    delta.append(el("span", { textContent: " vs the 30 days before" }));
  }

  return el("div", { className: "kpi" }, [
    el("div", { className: `mark ${tone}` }, icon(glyph, 18)),
    el("div", { className: "body" }, [
      el("div", { className: "k", textContent: label }),
      el("div", { className: "v", textContent: value }),
      delta,
    ]),
    spark && spark.length ? sparkline(spark) : null,
  ]);
}

const ACTIVITY_WORDS = {
  posted: ["Posted to the workbook", "good"],
  posted_with_override: ["Posted despite a failed check", "warn"],
  unposted: ["Row cleared and returned to review", "warn"],
  uploaded: ["Invoices uploaded", "good"],
  folder_ingested: ["Picked up from the watch folder", "good"],
  reprocess: ["Read again", ""],
  document_deleted: ["Document removed", "warn"],
  login: ["Signed in", ""],
  login_failed: ["Failed sign-in", "bad"],
  logout: ["Signed out", ""],
  signup: ["Account created", "good"],
  user_approved: ["Account approved", "good"],
  user_created: ["Account added", "good"],
  user_deleted: ["Account removed", "warn"],
  workbook_reset: ["Workbook reset", "bad"],
  workbook_downloaded: ["Workbook downloaded", ""],
  settings_updated: ["Settings changed", ""],
  password_changed: ["Password changed", ""],
};

function activityFeed(entries) {
  if (!entries.length) {
    return el("div", { className: "empty" }, "Nothing has happened yet.");
  }
  const feed = el("div", { className: "feed" });
  for (const entry of entries) {
    const [words, tone] = ACTIVITY_WORDS[entry.action] || [entry.action.replace(/_/g, " "), ""];
    const glyph = tone === "good" ? "check" : tone === "bad" ? "alert"
      : tone === "warn" ? "clock" : "dot";
    feed.append(el("div", { className: "entry" }, [
      el("span", { className: `dot-icon ${tone}` }, icon(glyph, 12)),
      el("div", { className: "what" }, [
        el("div", { textContent: words }),
        el("div", { className: "when", textContent: whenish(entry.at) }),
      ]),
      el("span", { className: "who-did", textContent: entry.user_email || "" }),
    ]));
  }
  return feed;
}

/* "2 minutes ago" beats a timestamp for anything within the day, and a
   timestamp beats it for anything older. */
function whenish(iso) {
  if (!iso) return "";
  const then = new Date(iso);
  const seconds = Math.max(0, (Date.now() - then.getTime()) / 1000);
  if (seconds < 90) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} minutes ago`;
  if (seconds < 86400) {
    const hours = Math.round(seconds / 3600);
    return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  }
  return iso.replace("T", " ").slice(0, 16);
}

async function renderDashboard() {
  const body = $("#dashboard-body");
  body.textContent = "";

  let data;
  try {
    data = await api("/api/dashboard");
  } catch (err) {
    body.append(el("div", { className: "card" }, el("div", { className: "empty" }, err.message)));
    return;
  }

  const t = data.totals;
  const change = (data.trend || {}).change_percent || {};
  const arrivals = data.arrivals || [];
  const dailyCounts = arrivals.map((point) => point.count);

  // ---- Headline figures ----------------------------------------------------
  body.append(el("div", { className: "kpis" }, [
    kpi("Invoices in total", String(t.all),
        { tone: "blue", glyph: "doc", change: change.all, spark: dailyCounts }),
    kpi("Posted to the workbook", String(t.processed),
        { tone: "good", glyph: "check", change: change.processed, spark: dailyCounts }),
    kpi("Waiting on you", String(t.needs_review + t.ready),
        { tone: "warn", glyph: "clock", change: change.needs_review }),
    kpi("Value posted", money(t.value_posted),
        { tone: "money", glyph: "rupee", change: change.value_posted }),
  ]));

  if (t.reading) {
    body.append(alertBox("info", [
      el("strong", { textContent: `${t.reading} invoice${t.reading > 1 ? "s" : ""} still being read. ` }),
      "They will appear as each one finishes.",
    ]));
  }

  // ---- Where everything sits, over time, and what just happened ------------
  const segments = [
    { label: "Posted", value: t.processed, color: token("--good", "#0ca30c") },
    { label: "Ready to post", value: t.ready, color: token("--viz-1", "#2a78d6") },
    { label: "Needs a look", value: t.needs_review, color: token("--warning", "#fab219") },
    { label: "Failed", value: t.failed, color: token("--critical", "#d03b3b") },
    { label: "Still reading", value: t.reading, color: token("--ink-3", "#888") },
  ];
  const total = segments.reduce((sum, s) => sum + s.value, 0);

  const legend = el("div", { className: "donut-legend" });
  for (const segment of segments) {
    if (!segment.value && total) continue;
    legend.append(el("div", { className: "row" }, [
      el("span", { className: "swatch", style: `background:${segment.color}` }),
      el("span", { className: "name", textContent: segment.label }),
      el("span", { className: "count", textContent: String(segment.value) }),
      el("span", { className: "pct",
                   textContent: total ? `${((segment.value / total) * 100).toFixed(1)}%` : "—" }),
    ]));
  }

  body.append(el("div", { className: "dash-grid" }, [
    el("div", { className: "card" }, [
      el("header", {}, [el("h2", { textContent: "Where everything sits" })]),
      total
        ? el("div", { className: "donut-wrap" }, [donut(segments), legend])
        : el("div", { className: "empty" }, "No invoices yet."),
    ]),

    el("div", { className: "card" }, [
      el("header", {}, [
        el("h2", { textContent: "Largest by value" }),
        el("span", { className: "grow" }),
        el("span", { className: "sub", textContent: "posted only" }),
      ]),
      (data.top_parties || []).length
        ? partiesList(data.top_parties)
        : el("div", { className: "empty" }, "Nothing posted yet."),
    ]),
  ]));

  // ---- Recent invoices -----------------------------------------------------
  const rows = data.recent || [];
  body.append(el("div", { className: "dash-split" }, [
    el("div", { className: "card span-row" }, [
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
            "Upload an invoice and it will be read, classified and checked.",
          ]),
    ]),
  ]));
}

// --------------------------------------------------------------------------- //
// Topbar panels
//
// Recent activity and Largest by value used to be dashboard cards. They are
// reference, not work: you glance at them, you do not act on them, and they
// were taking two of the three columns on the screen someone lands on. Moved
// behind topbar buttons, they are one click away from every screen instead of
// occupying the dashboard on all of them.
// --------------------------------------------------------------------------- //

const PANELS = {
  activity: {
    button: "btn-activity",
    title: "Recent activity",
    note: "invoices only",
    empty: "Nothing has happened yet.",
    build: (data) => (data.activity || []).length ? activityFeed(data.activity) : null,
  },
};

/* Built here rather than inside renderDashboard so the panel and any future
   card render the same thing. */
function partiesList(rows) {
  const box = el("div", { className: "parties" });
  const biggest = Math.max(...rows.map((p) => Number(p.value)), 1);
  for (const party of rows) {
    box.append(el("div", { className: "party" }, [
      el("div", { className: "line" }, [
        el("span", { className: "who-name", textContent: party.name }),
        el("span", { className: "amount", textContent: money(party.value) }),
      ]),
      el("div", { className: "track" },
        el("div", { className: "fill",
                    style: `width:${(Number(party.value) / biggest) * 100}%` })),
      el("div", { className: "sub",
                  textContent: `${party.invoices} invoice${party.invoices === 1 ? "" : "s"}` }),
    ]));
  }
  return box;
}

function closePanel() {
  const box = $("#popover");
  if (!box) return;
  box.hidden = true;
  box.textContent = "";
  state.openPanel = null;
  for (const key of Object.keys(PANELS)) {
    const button = $(`#${PANELS[key].button}`);
    if (button) button.setAttribute("aria-expanded", "false");
  }
}

async function openPanel(key) {
  const spec = PANELS[key];
  const box = $("#popover");
  if (!spec || !box) return;

  if (state.openPanel === key) { closePanel(); return; }   // second click closes
  closePanel();
  state.openPanel = key;
  $(`#${spec.button}`).setAttribute("aria-expanded", "true");

  box.hidden = false;
  box.append(
    el("header", {}, [
      el("h2", { textContent: spec.title }),
      el("span", { className: "grow" }),
      el("span", { className: "muted", textContent: spec.note }),
    ]),
    el("div", { className: "body" }, el("div", { className: "empty" }, "Loading…")),
  );

  let data;
  try {
    data = await api("/api/dashboard");
  } catch (err) {
    if (state.openPanel !== key) return;
    box.lastChild.replaceChildren(el("div", { className: "empty" }, err.message));
    return;
  }
  if (state.openPanel !== key) return;   // closed, or another opened, while loading

  const content = spec.build(data);
  box.lastChild.replaceChildren(content || el("div", { className: "empty" }, spec.empty));
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
                deleteButton(
                  { id: report.id, filename: report.filename, status: report.stage },
                  { className: "btn sm danger" },
                ),
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
    actions.append(deleteButton(row, { className: "btn xs danger" }));

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

  // ---- Appearance ----------------------------------------------------------
  // The top bar has a single button that cycles; this is where the choice can
  // be made explicitly, including "follow the system", which a cycling button
  // makes awkward to land on deliberately.
  const themeBox = el("div", { className: "segmented", id: "theme-switch",
                               role: "group", "aria-label": "Colour theme" });
  body.append(el("div", { className: "card" }, [
    el("header", {}, [el("h2", { textContent: "Appearance" })]),
    el("div", { className: "card-body form" }, [
      el("label", {}, [el("span", {}, "Colour theme"), themeBox]),
      el("p", { className: "muted" },
        "Following the system means the page changes with your operating system's "
        + "light and dark setting."),
    ]),
  ]));
  renderThemeSwitch();

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
    statCard(stats.documents.posted || 0, "Posted", "good"),
    statCard(stats.documents.failed || 0, "Failed", stats.documents.failed ? "bad" : ""),
  ]));


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
      // Two states now. An account either works or is switched off; there is
      // no longer a third where it exists but cannot sign in.
      el("td", {}, el("span", { className: `pill ${person.is_active ? "ready" : "new"}` },
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
// Vendors
// --------------------------------------------------------------------------- //

async function renderVendors() {
  const body = $("#vendors-body");
  body.textContent = "";

  let vendors;
  try {
    vendors = await api("/api/suppliers");
  } catch (err) {
    body.append(el("div", { className: "card" }, el("div", { className: "empty" }, err.message)));
    return;
  }

  if (!vendors.length) {
    body.append(el("div", { className: "card" }, el("div", { className: "empty" }, [
      el("div", { className: "big" }, "No vendors yet"),
      "A party appears here once one of their invoices has been posted. Nothing is "
      + "learned from a document still waiting in the queue.",
    ])));
    return;
  }

  const rows = el("tbody");
  for (const vendor of vendors) {
    rows.append(el("tr", {}, [
      el("td", {}, [
        el("div", { className: "cell-main", textContent: vendor.name }),
        el("div", { className: "cell-sub mono", textContent: vendor.gstin || "no GSTIN recorded" }),
      ]),
      el("td", { className: "num", textContent: String(vendor.invoice_count) }),
      el("td", { textContent: vendor.usual_rate != null
        ? `${(Number(vendor.usual_rate) * 100).toFixed(2)}%`
          + (vendor.rates_seen.length > 1 ? ` (+${vendor.rates_seen.length - 1} more)` : "")
        : "—" }),
      el("td", { textContent: (vendor.registers || [])
        .map((r) => REGISTER_SHEET[r] || r).join(", ") || "—" }),
      el("td", { className: "cell-sub", textContent: vendor.last_seen
        ? vendor.last_seen.replace("T", " ").slice(0, 16) : "—" }),
    ]));
  }

  body.append(el("div", { className: "card" }, [
    el("header", {}, [
      el("h2", { textContent: `${vendors.length} vendor${vendors.length === 1 ? "" : "s"}` }),
      el("span", { className: "grow" }),
      el("span", { className: "muted", textContent: "learned from posted invoices only" }),
    ]),
    el("div", { className: "table-wrap" }, el("table", { className: "grid" }, [
      el("thead", {}, el("tr", {}, [
        el("th", { textContent: "Vendor" }),
        el("th", { className: "num", textContent: "Invoices" }),
        el("th", { textContent: "Usual rate" }),
        el("th", { textContent: "Register" }),
        el("th", { textContent: "Last filed" }),
      ])),
      rows,
    ])),
  ]));

  body.append(el("p", { className: "muted", style: "margin-top:.8rem" },
    "A new invoice that disagrees with any of this — a different GSTIN, a rate this "
    + "vendor has never used — is flagged for you on the Review screen."));
}

// --------------------------------------------------------------------------- //
// Categories - the four registers, and how an invoice lands on one
// --------------------------------------------------------------------------- //

const CATEGORY_NOTES = [
  ["sales", "Anything we issued. Decided by the supplier GSTIN or name matching this company."],
  ["credit_note", "Checked first, whichever direction it points — a credit note is a credit "
                  + "note whether we issued it or received it."],
  ["rcm", "A purchase where the bill says reverse charge applies. It goes here rather than to "
          + "the purchase register because the RCM sheet has no CGST/SGST columns at all: the "
          + "buyer pays that tax directly instead of the seller collecting it."],
  ["purchase", "Everything else we received."],
];

async function renderCategories() {
  const body = $("#categories-body");
  body.textContent = "";

  const explain = el("div", { className: "explain" });
  for (const [key, note] of CATEGORY_NOTES) {
    explain.append(el("div", { className: "explain-row" }, [
      el("div", { className: "term" }, [
        document.createTextNode(TYPE_LABEL[key] || key),
        el("span", { className: "sub", textContent: REGISTER_SHEET[key] || "" }),
      ]),
      el("div", { className: "detail", textContent: note }),
    ]));
  }

  body.append(el("div", { className: "card" }, [
    el("header", {}, [
      el("h2", { textContent: "The four registers" }),
      el("span", { className: "grow" }),
      el("button", { className: "btn sm ghost", textContent: "See the rows",
                     onclick: () => show("registers") }),
    ]),
    explain,
  ]));

  body.append(el("div", { className: "card", style: "margin-top:.9rem" }, [
    el("header", {}, [el("h2", { textContent: "The order matters" })]),
    el("div", { className: "card-body" }, el("p", { className: "muted" },
      "Credit note first, then reverse charge, then seller-or-buyer. An invoice that is both "
      + "a credit note and reverse charge is a credit note, because that is the register it "
      + "has to be reported on.")),
  ]));
}

// --------------------------------------------------------------------------- //
// Templates - honestly empty
// --------------------------------------------------------------------------- //

function renderTemplates() {
  const body = $("#templates-body");
  body.textContent = "";
  body.append(el("div", { className: "card" }, el("div", { className: "not-built" }, [
    el("div", { className: "glyph" }, icon("info", 22)),
    el("div", { className: "big" }, "Not built"),
    el("p", {}, "This would hold a saved layout per vendor — where on the page their invoice "
      + "number sits, which column carries the taxable value — so a familiar format could be "
      + "read without a model call at all."),
    el("p", {}, "Nothing here is pretending to work. The reader currently handles every layout "
      + "the same way, which costs more but needs no setup when a vendor changes their format."),
    el("button", { className: "btn sm", textContent: "See what is known per vendor",
                   onclick: () => show("vendors") }),
  ])));
}

// --------------------------------------------------------------------------- //
// Rules - every judgment the system makes without asking
// --------------------------------------------------------------------------- //

function ruleRow(term, sub, detail) {
  return el("div", { className: "explain-row" }, [
    el("div", { className: "term" }, [
      document.createTextNode(term),
      sub ? el("span", { className: "sub", textContent: sub }) : null,
    ]),
    el("div", { className: "detail" }, detail),
  ]);
}

function renderRules() {
  const body = $("#rules-body");
  body.textContent = "";
  const info = state.info || {};

  const tax = el("div", { className: "explain" }, [
    ruleRow("Intra-state", "same state", [
      "Supplier state and place of supply agree, so the tax splits: CGST and SGST each get ",
      el("code", { textContent: "taxable value × rate ÷ 2" }), ".",
    ]),
    ruleRow("Inter-state", "different states", [
      "The whole ", el("code", { textContent: "taxable value × rate" }), " goes to IGST.",
    ]),
    ruleRow("Which state", "from the GSTIN", [
      "The supplier's state is the first two characters of their GSTIN, compared against the "
      + "place of supply printed on the document. Andhra Pradesh is a special case: invoices "
      + "still print the pre-2014 code 28 against a 37 GSTIN, and both are treated as the "
      + "same state.",
    ]),
    ruleRow("Return period", "from the invoice date", [
      "Never configured. An invoice files against the month it was raised in, so a July "
      + "invoice goes to July's workbook whatever else is in the queue beside it.",
    ]),
  ]);

  const checks = el("div", { className: "explain" }, [
    ruleRow("GSTIN checksum", "blocks posting", [
      "The fifteenth character is recomputed from the first fourteen. A single misread "
      + "character fails it.",
    ]),
    ruleRow("Arithmetic", "blocks posting", [
      "The tax printed on the document is reconciled against the tax its own figures imply, "
      + "to within one rupee — invoices round each line before totalling, so exact equality "
      + "is the wrong bar.",
    ]),
    ruleRow("Duplicates", "blocks posting", [
      "Checked by reading the register back, not by trusting the queue. Also checked at "
      + "upload, by the file's content, before anything is read.",
    ]),
    ruleRow("Vendor history", "warns only", [
      "A GSTIN or a rate that disagrees with every previous invoice from that vendor. A "
      + "warning rather than a blocker: vendors do re-register, and a rate legitimately "
      + "differs by what was sold.",
    ]),
  ]);

  body.append(
    el("div", { className: "card" }, [
      el("header", {}, [el("h2", { textContent: "How the tax is split" })]), tax,
    ]),
    el("div", { className: "card", style: "margin-top:.9rem" }, [
      el("header", {}, [el("h2", { textContent: "What is checked before a row is written" })]),
      checks,
    ]),
    el("div", { className: "card", style: "margin-top:.9rem" }, [
      el("header", {}, [el("h2", { textContent: "Settings in force" })]),
      el("div", { className: "card-body" }, el("div", { className: "kv" }, [
        el("span", { className: "k" }, "Approval"),
        el("span", { className: "v" }, info.approval_mode === "every_row"
          ? "Every document stops at Review first"
          : "Clean documents go straight to Ready; flagged ones stop"),
        el("span", { className: "k" }, "Reader"),
        el("span", { className: "v" }, `${info.provider || info.reader || "offline"}`
          + (info.model ? ` · ${info.model}` : "")),
        el("span", { className: "k" }, "Company"),
        el("span", { className: "v" }, `${info.company || ""} · ${info.gstin || ""}`),
      ])),
    ]),
    el("p", { className: "muted", style: "margin-top:.8rem" },
      "These are decided in code and covered by tests, not configured here. The reader is "
      + "told explicitly not to decide any of it — it reports what is printed."),
  );
}

// --------------------------------------------------------------------------- //
// Excel export
// --------------------------------------------------------------------------- //

async function renderExport() {
  const body = $("#export-body");
  body.textContent = "";
  const info = state.info || {};

  const ALL = "__all__";
  const periodList = info.periods || [];

  const picker = el("select", { className: "control" });
  for (const period of periodList) {
    picker.append(el("option", { value: period, textContent: period,
                                 selected: !state.exportAll && period === state.period }));
  }
  // Every period at once, as a zip of one workbook per period. Offered only
  // when there is more than one, since with a single period it would just be
  // the same file wrapped in a zip.
  if (periodList.length > 1) {
    picker.append(el("option", { value: ALL, textContent: "All periods",
                                 selected: state.exportAll === true }));
  }

  picker.addEventListener("change", () => {
    state.exportAll = picker.value === ALL;
    if (!state.exportAll) state.period = picker.value;
    renderExport();
  });

  const all = state.exportAll === true && periodList.length > 1;

  body.append(el("div", { className: "card" }, [
    el("header", {}, [el("h2", { textContent: "Download the workbook" })]),
    el("div", { className: "card-body form" }, [
      el("label", {}, [el("span", {}, "Return period"), picker]),
      el("p", { className: "muted" },
        all
          ? `A zip holding all ${periodList.length} workbooks, one file per return period. `
            + "They stay separate: each period is a return in its own right, with its own "
            + "totals and its own tax position."
          : "One workbook per return period, seeded from your master and appended to. The source "
            + "file is never written to."),
      el("div", { className: "row-actions" }, [
        el("button", {
          className: "btn primary",
          textContent: all ? "Download .zip" : "Download .xlsx",
          onclick: () => (all ? saveAllWorkbooks() : saveWorkbook(state.period)),
        }),
        el("button", { className: "btn", textContent: "See the rows first",
                       onclick: () => show("registers") }),
      ]),
    ]),
  ]));

  if (isAdmin()) {
    body.append(el("div", { className: "card", style: "margin-top:.9rem" }, [
      el("header", {}, [el("h2", { textContent: "Start this period again" })]),
      el("div", { className: "card-body" }, [
        el("p", { className: "muted" },
          "Discards every posted row for this period and re-seeds the workbook from the "
          + "master. The archived originals are kept."),
        el("div", { className: "row-actions" }, [
          el("button", { className: "btn danger", textContent: "Reset this period",
                         onclick: resetWorkbook }),
        ]),
      ]),
    ]));
  }
}

// --------------------------------------------------------------------------- //
// Audit logs
// --------------------------------------------------------------------------- //

async function renderAudit() {
  const body = $("#audit-body");
  body.textContent = "";

  let log;
  try {
    log = await api("/api/admin/activity?limit=200");
  } catch (err) {
    body.append(el("div", { className: "card" }, el("div", { className: "empty" }, err.message)));
    return;
  }

  if (!log.length) {
    body.append(el("div", { className: "card" }, el("div", { className: "empty" }, "Nothing yet.")));
    return;
  }

  const rows = el("tbody");
  for (const entry of log) {
    const [words] = ACTIVITY_WORDS[entry.action] || [entry.action.replace(/_/g, " ")];
    rows.append(el("tr", {}, [
      el("td", { className: "cell-sub", textContent: entry.at.replace("T", " ").slice(0, 16) }),
      el("td", { textContent: entry.user_email || "—" }),
      el("td", {}, [
        el("div", { textContent: words }),
        el("div", { className: "cell-sub mono", textContent: entry.action }),
      ]),
      el("td", { className: "cell-sub", textContent: entry.detail || "" }),
    ]));
  }

  body.append(el("div", { className: "card" }, [
    el("header", {}, [
      el("h2", { textContent: `${log.length} entries` }),
      el("span", { className: "grow" }),
      el("span", { className: "muted", textContent: "newest first" }),
    ]),
    el("div", { className: "table-wrap" }, el("table", { className: "grid log" }, [
      el("thead", {}, el("tr", {}, [
        el("th", { textContent: "When" }), el("th", { textContent: "Who" }),
        el("th", { textContent: "What" }), el("th", { textContent: "Detail" }),
      ])),
      rows,
    ])),
  ]));
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
  $("#who-role").textContent = user.role === "admin" ? "Admin" : "User";
  // The admin-only entries are filtered out when the navigation is built.
  renderNav();
}

/* Shared by the Excel export screen and anywhere else that offers it. */
async function resetWorkbook() {
  if (!confirm("Discard every posted row and start again from the master workbook?")) return;
  try {
    await api("/api/workbook/reset", { method: "POST" });
    state.reviewId = null;
    state.selected.clear();
    toast("Reset.");
  } catch (err) {
    toast(err.message, "error");
  }
  await refresh();
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

  // Two separate failures, deliberately not caught together. Only the first is
  // about the session; folding them into one catch meant a rendering bug threw
  // the user out and reported itself as a sign-in error, which sends someone
  // hunting for a password problem that does not exist.
  try {
    session.user = await api("/api/auth/me");
  } catch (err) {
    // A token that no longer works is not an error worth alarming anyone with.
    setToken("");
    renderGate();
    if (!(err instanceof Unauthenticated)) gateAlert(err.message);
    return;
  }

  try {
    await enterApp();
  } catch (err) {
    // The session is good; a screen failed to draw. Keep them signed in, say
    // what happened, and fall back to the inbox so the app is still usable.
    console.error("Failed to render", state.screen, err);
    toast(`Could not open that screen: ${err.message}`, "error");
    try {
      show("inbox");
    } catch (fallbackErr) {
      console.error("Inbox failed too", fallbackErr);
      bootFailed(fallbackErr);
    }
  }
}
