# Ira Innovations — GST Invoice Automation

An implementation of the *Invoice Automation Proposal* and the *Invoice Automation
Blueprint*: an invoice arrives, the system reads it, decides which of the four GST
registers it belongs on and how it is taxed, checks its own arithmetic, and appends a
finished row to the existing workbook.

The workbook does not change shape. Its four registers keep their columns, its Tax
Payable sheet keeps its formulas. Only the typing — and the manual CGST/SGST-vs-IGST
judgment call — goes away.

Built as the blueprint's recommended option: a Python service, LLM-based extraction
(Claude reading the PDF directly against a fixed schema, so a new vendor layout costs
nothing), and openpyxl writing into the real sheets.

---

## Architecture

Two independent servers. Neither embeds the other, and either can be restarted,
redeployed or replaced on its own.

```
  browser  ->  frontend :3000  (static files, no logic)
               |
               `-- tells the page where the API is, via /config.js
                        |
  browser  ------------->  backend :8000  (JSON API, pipeline, workbooks)
```

| | Port | Holds | Depends on |
|---|---|---|---|
| **backend/** | 8000 | The pipeline, the GST rules, the workbooks, the API key | FastAPI, openpyxl, pypdf, anthropic |
| **frontend/** | 3000 | The four screens: HTML, CSS, JavaScript | Python standard library only |

The browser calls the backend directly, cross-origin. The backend allows exactly the
frontend's origin (`GST_FRONTEND_ORIGINS`), not `*`. The frontend holds no business
logic, no workbook and no credentials — it is a web server in front of a folder, which
is why it can be swapped for nginx, a CDN, or a framework's own dev server without the
backend caring.

The frontend learns the backend's address from `/config.js`, generated at request time
from `--api`. Pointing it at a different backend is a restart, not a rebuild.

## Running it

Two terminals — or `.un-all.ps1` to open both at once.

```powershell
# Terminal 1 - backend
cd d:\IRA\gst-automationackend
python -m uvicorn app.main:app --port 8000

# Terminal 2 - frontend
cd d:\IRA\gst-automationrontend
python server.py --port 3000 --api http://127.0.0.1:8000
```

Open <http://127.0.0.1:3000>. API docs are at <http://127.0.0.1:8000/docs>.

First time on a new machine:

```powershell
cd d:\IRA\gst-automationackend
python -m pip install -r requirements.txt
copy .env.example .env        # then paste your ANTHROPIC_API_KEY into it
```

The frontend needs no packages at all.

Without an API key the backend still runs, but falls back to an offline reader that
parses the PDF text layer only — it cannot read scans. That is a safety net, not the
intended path.

### Running them apart

They are only coupled by a URL and an allowed origin, so either can move:

```powershell
# Backend on another port
python -m uvicorn app.main:app --port 9000
python server.py --api http://127.0.0.1:9000        # point the frontend at it

# Backend reachable from other machines
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# then in backend/.env, allow the origin the browser will use:
#   GST_FRONTEND_ORIGINS=http://192.168.1.50:3000
```

If the backend is down, the frontend still serves the page and says so plainly rather
than failing silently.

### Tests

```powershell
cd d:\IRA\gst-automationackend
python -m pytest tests -q      # 104 tests
```

The suite runs against a scratch copy of the real May-26 workbook, and every expected
figure is taken from the source documents — the invoice's ₹439.30 CGST/SGST, GSTR-2B's
₹1,080 IGST on the Waymiro bill, the ₹6,16,509 opening IGST credit — so a pass means
the system reproduces the client's own numbers.

---

## The four screens

| Screen | What it does |
|---|---|
| **Invoice Inbox** | Everything that has arrived, already classified. Drag PDFs in, or drop them into `data/dropbox` and press *Check watch folder*. |
| **Quick Review** | The original document beside what the reader found. Correct any field and the tax treatment recalculates before you confirm. |
| **GST Registers** | The rows as they now exist in the workbook — same four sheets, same columns. The period picker in the header chooses which return you are looking at. |
| **Tax Payable** | The net position for the selected period, recomputed from the registers as rows are posted. |

---

## The pipeline

| Stage | Where |
|---|---|
| 1. Capture | `app/pipeline.py` — upload, or the `data/dropbox` watch folder |
| 2. Classify | `app/gst/rules.py` — sale / purchase / credit note / reverse charge |
| 3. Extract | `app/extract/llm.py` (Claude), `app/extract/heuristic.py` (offline fallback) |
| 4. Apply GST rules | `app/gst/rules.py` — intra- vs inter-state, then recompute the tax |
| 5. Validate | `app/gst/validate.py` — GSTIN checksum, arithmetic, duplicates |
| 6. Write & notify | `app/workbook.py` — append the row, archive the source |

A document that fails validation is flagged, never dropped. A reviewer can still post it
with an explicit override, and the override is recorded on the document's history.

### One file is not one invoice

Billing software exports a month as a single print run — `Multiprint (40).pdf` holds 18
invoices across 18 pages. Reading only the first and dropping the other 17 would be the
worst failure this system could have, because it is silent.

`app/extract/split.py` finds boundaries by **invoice number, not page count**, so an
invoice whose item table spills onto a second page stays in one piece: a page repeating
the previous number, or carrying none, is treated as a continuation. Each invoice found
becomes its own document with its own PDF, so Quick Review shows that invoice alone and
the archived audit copy is the invoice rather than the print run it arrived in. Every
document keeps its provenance (`Multiprint (40).pdf · p3 · 3 of 18`).

A PDF with no readable invoice numbers — a scan — is deliberately **left whole**.
Splitting on a guess could tear one invoice in half, which is worse than not splitting.

### What counts as an issue

Only things a reviewer can actually act on. Two deliberate exclusions:

- **A B2C sale is not an issue.** Selling to an unregistered buyer is ordinary — the
  GSTR-1 sheet has a B2B/B2C split precisely because both are normal. It is reported as
  context on the treatment (`supply_category`), not raised for resolution.
- **Which reader is running is not a document defect.** It is system state, stated once
  on the header banner rather than repeated on every document it touches.

### The return period is derived, never configured

A GST return is monthly, and which return an invoice belongs to is a property of the
invoice — its own date. Nothing configures it. That is deliberate: a configured period
means the application only works for whichever month it was last pointed at, and files
July sales into a May return the first time someone forgets to change it.

So **each invoice is routed to the return period its date falls in**, and each period
gets its own workbook under `data/workbook/gst-<period>.xlsx`, created on demand from the
master. Upload a July print run and a May invoice together and they land in July's and
May's workbooks respectively, with no intervention. The period picker in the header
chooses which one the Registers and Tax Payable screens are showing.

The master workbook is the template for every period, and it declares its own period in
`GSTR-1!A5` (`Return Period : May-26`) — read from the file rather than told to the app.
Opening the period it already covers keeps its rows; any other period starts from the
same structure with the four registers empty, because May's purchases are not July's.

An invoice whose date cannot be read is **blocked**, not guessed at — without a date
there is no return to file it against.

#### Opening credit on a new period

A period created fresh starts with its input-credit carry-forward at **zero**, and the
Tax Payable screen says so until someone enters the real figure. The correct value is the
closing balance of the previous return, which only that return can supply. Zero is the
deliberate choice over copying the master's: an overstated opening credit understates the
tax due, which is the expensive direction to be wrong in.

### Who approves what

The blueprint left this open — "whether a human must approve every row before it's
posted, or only the ones validation flags" — so it is a setting, not a hard-coded policy:

```
GST_APPROVAL_MODE=flagged_only   # default: clean documents go straight to Ready
GST_APPROVAL_MODE=every_row      # every document stops at Quick Review first
```

Either way nothing reaches the workbook without someone pressing Post. The mode only
decides which queue a clean document lands in.

### The classification order

A credit note is a credit note whichever direction it points, so it is checked first. A
reverse-charge bill goes to the RCM register rather than the ordinary purchase register,
because the RCM sheet has no CGST/SGST columns at all — the buyer pays that tax directly
instead of the seller collecting it. Only then does "are we the seller or the buyer?"
decide between GSTR-1 and GSTR-2B.

### The tax split

The supplier's state (from the first two characters of their GSTIN) is compared against
the place of supply. Same state → CGST + SGST, each `taxable value × rate ÷ 2`. Different
state → the whole `taxable value × rate` into IGST. This mirrors the workbook's own
formulas, and posted rows carry those formulas rather than pasted numbers, so a row the
system wrote recalculates in Excel exactly like a row a person typed.

The reader is explicitly told **not** to decide the tax treatment. It reports what is
printed; `app/gst/rules.py` owns every judgment call, deterministically and under test.

---

## Safety properties

- **The source workbook is never written to.** On first run it is copied to
  `data/workbook/gst-workbook.xlsx` and only that copy is appended to. *Download workbook*
  gives you the copy; *Reset* re-seeds it from the master.
- **Registers can grow.** GSTR-1 ships with 64 pre-formatted rows and those are filled
  first, reusing the sheet's formatting. When a register runs out, a row is inserted above
  the totals row, the `SUM()` ranges are rewritten, and — because openpyxl does not adjust
  formulas across an insert — every Tax Payable reference into that sheet is repointed.
  Tested in `test_gstr1_grows_past_its_last_template_row`.
- **Every original is archived** next to the row it produced, under
  `data/archive/<sheet>/row<NNNN>__<filename>`.
- **Duplicates are caught** by reading the register back, not by trusting the queue.

---

## Two things found in the source data

**1. The place-of-supply code on the sample invoice is the legacy one.** It prints
"Andhra Pradesh ** ( 28 )" while the GSTIN says `37`. Code 28 was Andhra Pradesh before
the 2014 bifurcation. Taken literally, 28 ≠ 37 would make a local sale look inter-state
and put the tax in the wrong column. `app/gst/states.py` resolves the state by name first
and treats 28 and 37 as the same state; `test_legacy_andhra_pradesh_code_does_not_break_the_split`
pins this.

**2. The Waymiro supplier is in Odisha, not West Bengal.** The blueprint's Review mockup
labels GSTIN `21AADCW9393G1Z3` as West Bengal. State code 21 is Odisha; 19 is West Bengal.
The IGST conclusion in the mockup is unaffected — either way it is not Andhra Pradesh — but
the state name shown to a reviewer is now the correct one.

---

## Open items

- **GSTR-1's B2B/B2C reconciliation block** (rows 75–80) is built from a hand-written list
  of row references that covers rows 8–50 only, so rows 51–71 fall outside it. That is
  pre-existing in the workbook and it is your tax logic, so the app deliberately does not
  rewrite it — posted rows land in the register and the totals row, and the reconciliation
  block is left exactly as you maintain it. Worth a decision before Phase 1 goes live.
- **Email intake** is currently the `data/dropbox` watch folder: point a mail rule or the
  scanner's output at it. A direct IMAP/Graph connector is a small addition once you
  confirm which mailbox invoices arrive in.
- **Single tenant.** The `SRI JAYAGURUDATTA TREDARS` history in tabs `GSTR-1A`,
  `GSTR-1 (2)` and `GSTR-1 (3)` is left untouched. If that workbook format is genuinely
  reused across clients, the company profile in `app/config.py` becomes per-tenant
  configuration — the blueprint's first open question.
- **openpyxl round-trips formulas and values, not charts or pivot tables.** The May-26
  workbook has neither, so nothing is lost today; worth re-checking if the file gains them.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/info` | Company, return period, reader, counts |
| `GET` `POST` | `/api/documents` | List / upload |
| `POST` | `/api/ingest/folder` | Pick up the watch folder |
| `GET` | `/api/documents/{id}/file` | The original, for the review pane |
| `PATCH` | `/api/documents/{id}` | Apply corrections, re-run rules |
| `POST` | `/api/documents/{id}/confirm` | Post to the register (`?override=true` to force) |
| `POST` | `/api/documents/{id}/unpost` | Clear the row, return to review |
| `GET` | `/api/registers/{sales\|purchase\|credit_note\|rcm}?period=` | Register rows for one period |
| `GET` | `/api/tax-payable?period=` | Net position for one period |
| `GET` | `/api/workbook/download?period=` | That period's workbook |

`period` defaults to the most recent one held, and takes the `Jul-26` form.
