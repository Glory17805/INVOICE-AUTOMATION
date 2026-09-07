# Vendor templates

Drop a `.json` file here to teach the reader one supplier's invoice. Nothing
in this directory is required — the general rules handle most layouts, and a
template exists for the ones they get wrong.

A template only ever **adds**. Everything it does not mention keeps working
the way it works for every other invoice, so a vendor with one odd label
costs you two lines, not a parser.

```json
{
  "name": "Sharma Traders",
  "match": { "gstin": "37AAACS1234F1Z5" },
  "labels": {
    "invoice_number": ["challan no", "challan number"]
  },
  "columns": {
    "taxable": ["net value"]
  },
  "notes": "Prints the challan number where other vendors print an invoice number."
}
```

## Fields

| Key | What it does |
|---|---|
| `name` | Shown on the invoice's note, so a reviewer knows which template applied. |
| `match.gstin` | The supplier's GSTIN. The strong signal — it identifies one business, and on its own is enough. |
| `match.text` | Phrases that must **all** appear. For a vendor whose GSTIN you do not have yet. One common phrase alone would claim invoices it knows nothing about. |
| `labels` | Extra wordings for a field. Keys are field names — see `FIELD_ALIASES` in `app/extract/labels.py`. |
| `columns` | Extra headings for an item-table column. Keys are `description`, `hsn`, `quantity`, `unit_rate`, `taxable`, `discount`, `total`. |
| `notes` | Free text, shown with the template name. Say *why* this vendor needs one. |

A template that matches nothing is ignored with a warning, rather than loaded
and never applied — a template nobody notices is not doing its job.

Aliases are checked before use: anything with regex punctuation in it, or
longer than 60 characters, is dropped. A config file should not be able to
stop the reader matching anything at all.

## After editing

Templates are cached. Restart the backend, or call `templates.refresh()`.

## Checking one works

```
python -c "from app.extract import templates, heuristic; \
           from pathlib import Path; \
           print(heuristic.read(Path('their-invoice.pdf'))[0])"
```

The invoice's note names the template that applied, or says the general rules
were used.
