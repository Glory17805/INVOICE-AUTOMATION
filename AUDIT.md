# Hardening Audit — GST Invoice Automation

**Date:** 26 August 2026
**Scope:** `d:\IRA\gst-automation` — backend and frontend
**Commits:** `c59f3d4` (baseline) → `d100e51`
**Suite:** 104 tests passing before, **135 passing after**

This records a review of the application as it stood after the multi-period
refactor and the backend/frontend split, the changes made in response, and what
is deliberately left for later.

One correctness defect was found and fixed. One reporting defect was found while
verifying the first. Six operational gaps were closed. Three performance changes
were made. Nothing in the GST arithmetic was altered — the figures the suite pins
against the client's own workbook are unchanged.

---

## 1. Defects found and fixed

### 1.1 Output tax overstated on hand-typed inter-state sales — **corrected**

**Severity: high.** Wrong tax figures on a screen an accountant reads.

`workbook.py::_gstr1_amounts` re-derives amounts from rows already sitting in
GSTR-1. It inferred the entire CGST/SGST-vs-IGST split from whether column K held
a *formula* or a *literal*:

```python
if _is_formula(igst_raw):   igst = taxable * rate;  cgst = sgst = 0
else:                       igst = number(K);       cgst = sgst = taxable * rate / 2
```

Rows this application posts always carry formulas, so the literal branch was
never exercised by a test. But the client has kept these sheets by hand for
years and will keep doing so alongside the app — and a typed row carries plain
numbers.

Reproduced against a copy of the real May-26 workbook. An inter-state sale,
taxable ₹10,000 at 18%, IGST ₹1,800 typed into K, L and M left blank:

| | IGST | CGST | SGST | Total |
|---|---|---|---|---|
| On the sheet | 1,800.00 | 0.00 | 0.00 | 11,800.00 |
| Read back as | 1,800.00 | **900.00** | **900.00** | **13,600.00** |

It invented ₹1,800 of tax that is not on the sheet, and because
`tax_payable_summary` sums GSTR-1 through the same function, the error reached
the Tax position screen as well as the Registers display.

**Fix.** K, L and M are each evaluated on their own terms — resolving the
sheet's own idioms (`=I*J`, `=J*I/2`, `=L{row}`) and taking literals at face
value. Template rows and posted rows evaluate exactly as before, so every pinned
figure holds. Four regression tests cover the hand-typed inter-state case, the
hand-typed intra-state case, both formula idioms, and an unrecognisable formula
(which scores zero rather than guessing).

> This was reviewed independently by the session that wrote the original code,
> which reproduced the same numbers and confirmed the diagnosis.

### 1.2 The header claimed Claude was reading when it was not — **corrected**

**Severity: medium.** Silently misrepresents how the work was done.

`/api/info` reported the *configured* reader: holding an `ANTHROPIC_API_KEY`
made the header say "Claude reader" regardless of whether Claude ever answered.

Found live. The configured key returns:

```
HTTP 400 — Your credit balance is too low to access the Anthropic API.
```

The fallback worked exactly as designed: the offline reader took over and the
documents were still captured. But the header still said "Claude reader", and
`reader_note` — which records the reason — was displayed nowhere. Every invoice
was being read offline and nothing said so.

**Fix.** `/api/info` now carries `reader_effective` and `reader_note` taken from
the most recent document actually read. The header gained a third state,
*Claude unavailable*, and a banner above every screen explains the consequence —
scans and photos cannot be read offline at all.

**This is currently live.** Until the Anthropic account is topped up, every
invoice is being read by the offline parser, which cannot read scans and gives
text-layer PDFs a simpler read worth checking. See §4.1.

### 1.3 Every path in the README was wrong — **corrected**

The README contained five real control bytes where literal escape text belonged:
`0x08` for `\b`, `0x0c` for `\f`, and a stray `0x0d`. Readers were instructed to
`cd d:\IRA\gst-automationackend` and to run `.` + a carriage return + `un-all.ps1`.
Every quoted path was uncopyable.

Repaired. A scan of all 37 source files confirms the README was the only file
affected.

### 1.4 The watch-folder button had been dropped — **restored**

The UI rewrite removed *Check watch folder*, while `/api/ingest/folder` and the
README's description of it as the email-intake stand-in both remained. The only
non-upload intake route had no way to trigger it. Restored to the inbox.

---

## 2. Gaps closed

### 2.1 No version control

The tree was not a git repository, despite carrying a `.gitignore`. This is
client financial software that was being actively restructured; there was no way
to diff, revert, or attribute anything.

Initialised, with the pre-existing state committed as the baseline before any
change. `backend/.env` (which holds a live API key) is correctly excluded — only
`.env.example` is tracked.

### 2.2 No authentication

Every endpoint was open, including `/api/workbook/download` (the entire GST
workbook) and `/api/documents/{id}/file` (original invoices).

An optional shared secret (`GST_API_KEY`) is now required on every route but
`/api/health`, compared with `secrets.compare_digest`. Accepted as `X-API-Key`
or a bearer token; **refused** in a query string, because query strings reach
browser history, proxy logs and referrer headers. Off by default — correct for a
single machine — and startup logs a warning when it is off rather than leaving
that a silent condition.

Two supporting changes were required:

- CORS is registered *outside* the key check, so a 401 still carries
  `Access-Control-Allow-Origin` and the browser reports the real status rather
  than an opaque CORS failure. Verified.
- The document viewer and workbook download now fetch bytes as blobs. An
  authentication header cannot ride on an `<iframe src>`, an `<img src>` or a
  download link — the browser issues those itself and attaches nothing. The same
  path runs whether or not a key is set, so the viewer cannot work locally and
  break the day authentication is enabled.

**What this is not.** One key for the whole deployment, readable from
`/config.js` by anyone who can load the frontend. It draws a boundary around the
network, not around a person. See §4.2.

### 2.3 Two processes could silently destroy tax records

**This was the most dangerous gap.** `workbook.py` serialises writes with a
`threading.RLock` — correct within one process, worth nothing across two.
Posting a row is read-the-register, pick-the-next-free-line, write, save. Two
backends on the same `data/` directory would interleave those steps and the
second save would drop the first one's row, with no error raised anywhere. That
is an unrecoverable loss of a tax record, and it was one `--workers 2` away.

The backend now takes an exclusive kernel lock on its data directory at startup.
A second process refuses to start:

```
app.singleton.AlreadyRunning: Another backend is already using
D:\IRA\gst-automation\backend\data (held by pid 15812).
Only one process may write these workbooks: a second one would silently
overwrite rows the first has posted.
```

The lock is held by an open file handle, so the OS releases it on exit — crash
or kill included. There is no stale lock file to clear by hand, which is the
failure mode that makes PID-file schemes irritating. Verified by launching a
second backend against the same directory.

### 2.4 Uploads were unbounded

No size limit existed. A cap (`GST_MAX_UPLOAD_MB`, default 25) is now checked
against the declared content length before the body is read, and again per file.

### 2.5 Dependencies could jump a major version silently

`anthropic>=0.40` resolved to `1.0.0` on a fresh install — across a major
version boundary, with nothing to say so. It happened to stay compatible; the
next one will not. All dependencies now carry an upper bound below the next
major. A `requirements-dev.txt` pins the test tooling.

### 2.6 Deprecated startup hook

`@app.on_event("startup")` is deprecated. Migrated to the `lifespan` context
manager, which also gives the instance lock a correct release on shutdown.

---

## 3. Performance and structure

### 3.1 Uploading a print run held the request open for minutes

`POST /api/documents` ran the whole pipeline inline: one model call per invoice,
eighteen for a print run, inside a single HTTP request. A browser sat on a
spinner with nothing to show, and any timeout threw away the response to work
that had in fact happened.

Capture and reading are now separate. The upload registers the documents and
returns; a background task reads them. Measured: **78 ms** to respond, with the
document returned in its `new` state and unread. The page polls the batch out of
`new`, so the inbox fills in as each invoice lands.

Failures in that background task are recorded on the document itself — a
traceback on a detached task is seen by nobody.

### 3.2 Every screen re-parsed the workbook

`read_register` and `tax_payable_summary` each reloaded and re-parsed the whole
`.xlsx`. One visit to Registers did it four times, then Tax position did it
again, against a file that had not changed.

Reads are now cached against the workbook's modification time. Writes evict
their period explicitly, so correctness does not rest on timestamp resolution —
and because the key is the file's own mtime, **a row typed directly into the
workbook in Excel is still picked up**, which a naive cache would have masked.
Pinned by a test. Suite runtime fell from ~90 s to ~70 s.

### 3.3 The queue rewrote a whole file on every change

`store.json` was parsed in full on every read and rewritten in full on every
write, so touching one document cost time proportional to how many had ever
arrived. A write was also a whole-file replace — fine until the process dies
mid-replace.

Replaced with SQLite: one file, standard library, no service to run, real
transactions, and indexes on the two columns the inbox filters by. WAL mode lets
the polling reader overlap the background writer. `store.py`'s interface is
unchanged, so nothing above it knows the difference.

An existing `store.json` is imported on first use and **renamed, not deleted**.
Verified against the live 18-document queue: identical ids, blobs identical
including history.

---

## 4. Recommended future work

Ordered by what would matter most next.

### 4.1 Restore Claude extraction — *operational, blocking quality*

The Anthropic account currently returns *credit balance too low*, so every
invoice is being read by the offline parser. That parser cannot read scans or
photographs at all, and gives text-layer PDFs a simpler read. The application
handles this correctly and now says so plainly, but the system is running in its
degraded mode until the account is topped up. **Nothing in the code will fix
this.**

### 4.2 Per-user authentication and an attribution trail

The current key identifies a deployment, not a person. For a filing system the
question "who posted this row?" is a reasonable one to be asked by an auditor,
and today there is no answer: the document history records *that* a row was
posted and whether validation was overridden, but not by whom.

Worth doing together: user accounts, a session or token per user, and a `posted_by`
field carried onto the document history and into the archive path. This also
allows the override action — posting a document that failed validation — to be
attributed, which is the single most audit-sensitive action in the system.

### 4.3 Protect the key against guessing

There is no rate limiting. A shared secret on an exposed port invites offline
guessing, and nothing currently slows that down or records the attempt. A simple
per-IP failure counter with a backoff, plus a logged warning, would be
proportionate. Pairs naturally with 4.2.

### 4.4 TLS

The key and every invoice currently travel in clear text. On one machine that is
fine. Reachable across a network it is not — the key is in a header on every
request. Terminate TLS at a reverse proxy rather than in the application.

### 4.5 Back up `data/`

`backend/data/` holds the per-period workbooks, the archived originals that
constitute the audit trail, and the queue. It is gitignored, correctly, and
nothing backs it up. The master workbook can re-seed structure but not posted
rows. A scheduled copy of `data/workbook/` and `data/archive/` is the cheapest
insurance here.

### 4.6 Carry the opening credit forward automatically

A new period starts with input-credit carry-forward at zero, and the screen says
so. The correct figure is the closing balance of the previous return, which the
application already holds once that period exists. Computing it — with an
explicit confirmation step, since it is a filed figure — would remove a manual
step that is easy to forget and expensive to get wrong.

### 4.7 Direct email intake

Intake is the `data/dropbox` watch folder, driven by a button. An IMAP or Graph
connector polling the mailbox invoices actually arrive in would close the last
manual step in capture. Small, and gated only on knowing which mailbox.

### 4.8 The reconciliation block in GSTR-1

Rows 75–80 are built from a hand-written list of row references covering rows
8–50, so rows 51–71 fall outside it. Pre-existing in the client's workbook and
their tax logic, so the application deliberately does not rewrite it. It needs a
decision before the register grows past row 50 in a live month.

### 4.9 Scaling past one writer

The single-instance lock makes the current limit explicit and safe, but it is
still a limit. Serving more than one concurrent user means moving workbook
writes behind a single queue consumer, not adding worker processes. Nothing
today needs it; the constraint should be understood before someone reaches for
`--workers`.

### 4.10 Smaller items

- **No frontend tests.** All 135 tests are backend. The UI is exercised by hand.
- **No CI.** The suite runs when someone remembers. A hook or pipeline would fix
  the class of problem this audit found in §1.3 — a corruption committed unnoticed.
- **Secrets sit in plaintext** in `backend/.env`. Acceptable on a single trusted
  machine; a secret store is the answer if this moves to a server.
- **Duplicate detection is per-period.** The same invoice number in two different
  months is not flagged. Probably correct, but it is an assumption worth stating.
- **Single tenant.** The company profile is hard-coded in `config.py`. If the
  workbook format is genuinely reused across clients, this becomes per-tenant
  configuration.
- **openpyxl does not round-trip charts or pivot tables.** The May-26 workbook has
  neither, so nothing is lost today. Re-check if the file gains them.

---

## 5. Verification

| Check | Result |
|---|---|
| Test suite | 135 passed (was 104) |
| Tax read-back fix | Reproduced before, pinned after; pinned client figures unchanged |
| Auth: no key / wrong key / right key / bearer | 401 / 401 / 200 / 200 |
| Auth: key in query string | 401 (rejected by design) |
| Auth: `/api/health` without a key | 200 |
| CORS header present on a 401 | Confirmed |
| Second backend, same data directory | Refused to start, named the holder |
| Upload response time | 78 ms, document returned unread |
| Background reading | Completed, document reached `ready` with correct extraction |
| SQLite import of the live queue | 18 in, 18 out, identical ids and blobs |
| Frontend + backend together, auth on | All assets served, authenticated calls succeeded |
| Control-character scan, 37 files | Clean |
| Backend server log | No errors, no warnings, no unexpected non-2xx |

Two things were **not** verified: the Claude extraction path end-to-end, which is
blocked on account credit (the request reaches the API and returns a well-formed
billing error, so the SDK integration itself is confirmed working); and the
frontend UI, which was checked by asset delivery and API contract rather than by
driving a browser.
