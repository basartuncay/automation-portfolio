# Invoice Extractor

Turns a folder of PDF invoices into clean spreadsheets: one row per invoice and one row per line item. Before an invoice is accepted, the tool checks that the numbers add up. Anything that doesn't goes to a review list instead of silently landing in your books.

## What it does

- **Reads text PDFs** from several vendor layouts. Each vendor is one template in `TEMPLATES`, so adding a vendor is a few lines of config, not new code.
- **Handles regional formats:** `1,234.50` and `1 234,50`, ISO and `DD.MM.YYYY` dates, different labels ("Invoice Number" vs "Bill No.", "Tax" vs "VAT").
- **Checks the math:**
  - each line is qty × unit price = amount
  - the lines sum to the subtotal
  - subtotal + tax = total
- **Sends problems to review** with the reason, for example a total that doesn't add up, an unknown vendor, a missing field or a corrupt file. One bad file never stops the batch.

## Run it

```bash
uv sync
uv run python make_samples.py samples   # synthetic invoices in 2 layouts + 2 problem cases
uv run python -m invoice_extractor samples --out out
```

Output:

| File | Content |
|---|---|
| `invoices.csv` | file, vendor, number, date, currency, subtotal, tax, total |
| `line_items.csv` | invoice, description, qty, unit_price, amount |
| `review.csv` | file, problems |

## Tests

```bash
uv run pytest -q
```

They cover both layouts, comma-decimal numbers, a total that doesn't add up, an unknown vendor and a corrupt PDF.

## Extending

- **Scanned (image) PDFs:** add an OCR step (Tesseract or a vision model) before `parse`. The rest of the pipeline stays the same.
- **Output elsewhere:** Google Sheets, a database or an accounting API (QuickBooks, Xero) replace the CSV writer.
- **Automatic intake:** watch an email inbox or a Drive folder and process new files on arrival.

All sample invoices are synthetic, so no real customer data is involved.
