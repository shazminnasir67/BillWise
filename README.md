# BillWise: Utility Bill Decoder

## Problem and solution
Pakistani electricity bills (K-Electric, LESCO, IESCO) are photographed or scanned, tilted, partly in Urdu and hard to read. BillWise reads a bill image with a vision LLM into a fixed JSON schema (Level 1), then answers questions about it in plain English (Level 2).
All arithmetic is done in Python and the model only phrases the results. Answers are grounded in the bill, numbers are tagged printed, calculated or estimated, and missing data is reported as "Not shown on this bill".

## Architecture
- `app/streamlit_app.py` is the demo UI on top of the same `core/` code.
- `run.py` is the batch CLI. It reads the CSV templates and writes `output/level1.csv` and `output/level2.csv` with the `csv` library. One bill failing never stops the run: it gets null-safe rows and is listed at the end.
- `core/` holds the shared logic: `llm.py` (provider adapters, failover, call budget lock), `extract.py` (Level 1 and extended facts, pydantic validation, one retry), `calc.py` (all math), `qa.py` (one call per bill for all questions), `cache.py` (disk cache), `csv_io.py`.
- `prompts/` holds the versioned prompts (`l1_v5`, `ext_v1`, `qa_v2`, `reread_v1`; earlier Level 1 versions are kept for comparison). Per bill there are 3 model calls: Level 1, extended facts (13-month usage, billing history, late-payment amounts, printed shares, message board) and one call for all questions. Answers are checked in code: an answer that thinks out loud or is over 900 characters triggers one retry, and anything still bad is trimmed to its last clean sentences. Thresholds written in a question (e.g. "more than 150 units") are computed in Python, so the model never compares numbers itself.

## Python version
Python 3.11 or newer (developed and tested on Python 3.13).

## Setup
```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then put your real key in .env
```

## Environment variables
| Variable | Meaning |
|---|---|
| `MODEL_PROVIDER` | `gemini` (alias of `google`), `anthropic` or `openai` |
| `MODEL_NAME` | primary model ID |
| `MODEL_CHAIN` | comma-separated failover order (allowed IDs only); defaults to `MODEL_NAME` |
| `API_KEY` | key for `MODEL_PROVIDER`; `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` override it per provider |
| `ALLOW_LLM_CALLS` | must be `true` for any real model call (default `false`) |
| `MAX_LLM_CALLS` | hard cap on model calls per run (default 40) |

Cached results are reused, so a repeat run makes no calls.

## Run: batch CSV pipeline
```
ALLOW_LLM_CALLS=true .venv/bin/python run.py --bills data/test/bills --templates data/test/csv --out output
.venv/bin/python scripts/validate_submission.py --templates data/test/csv --output output
scripts/package_zip.sh submission.zip data/test/csv
```
The bill file name without extension must equal `bill_id` in the template. Before any model call it prints how many calls are needed (cached stages are skipped) and warns if `ALLOW_LLM_CALLS` is off or the cap is too low. A re-run after a crash only repeats uncached stages. The run is throttled to 12 requests per minute and prints rows done, bills needing attention and calls used. Each bill needs 3 calls (Level 1, extended facts, questions), plus at most two re-read calls for a bill with a real mismatch, so set `MAX_LLM_CALLS` above 3 x bills to leave room for retries.

## Run: demo web app (Streamlit)
```
.venv/bin/streamlit run app/streamlit_app.py
```
- **Upload a bill:** pick a PNG, JPEG or WEBP (checked by its content, max 10 MB). The page shows the image beside the extracted values with green (verified by a passing check), amber (unverified or warning) and red (check failed) badges, the reconciliation checks, any re-read or late-amount note, optional answers to questions you type, and downloads of the JSON and a one-row `level1` CSV.
- **Browse submitted results:** shows each bill from `output/` (or `output_v3/`) with its Level 1 values, checks recomputed from the CSV, and its Level 2 answers.
- Live analysis calls the model (2 calls per bill, plus 1 for questions and up to 2 for a re-read), so it only runs when you start Streamlit with `ALLOW_LLM_CALLS=true`; otherwise it shows a notice and only cached bills work. It uses the same `core/` code as `run.py`.

## Tests (mocked, no network)
```
.venv/bin/python -m pytest -q
```

## Models
Only approved model IDs are accepted by the code. In this project:
- `gemini-3.5-flash-lite` (primary, Google Gemini API, vision input): Level 1, extended facts, questions and the first re-read
- `gemini-3.1-flash-lite` (same API): USED as the second reader for targeted re-reads that came back unchanged (it corrected one meter reading); also the configured failover in `MODEL_CHAIN`

The adapters in `core/llm.py` also support `claude-haiku-4-5`, `gpt-4.1-mini` and `gpt-5-mini`, which were not used.

## Reconciliation approach
`core/reconcile.py` is pure Python (no LLM) and checks the extracted Level 1 values with a tolerance of +-1:
sum(charges) = total_charges, sum(taxes) = total_taxes, total_charges + total_taxes = current_bill, current_bill + arrears = payable_within_due_date, current_reading - previous_reading = units_consumed, and payable_after_due_date >= payable_within_due_date.
Each check is `ok`, `mismatch`, `warning` or `not_checkable` (an input is null, or the bill prints only a combined tax or only totals). The units check is only a `warning` when the bill prints several meter rows, because the printed units are then a total. The payable check is likewise a `warning` when the bill prints 0 payable against a credit balance (current_bill + arrears is negative). Every involved field gets the worst status of its checks.
Reconcile itself never changes an extracted value: a mismatch caused by the bill's own rounding or layout is reported as printed, not corrected. `run.py` lists bills with a mismatch under "NEEDS ATTENTION" with the reason and prints warnings as notes, but still writes the printed values.
When a check that compares extracted numbers fails for real (units vs readings, charge or tax sums vs printed totals, charges + taxes vs current bill), `run.py` makes a targeted re-read of just those fields (same image, the failing check stated, prompt `reread_v1`). Each field group is judged on its own check: its re-read values are kept only if that group's failing check now passes and no check that passed before starts failing. A failure in another, unrelated check does not block a fix. If the first re-read returns the same values as the original, one more read is made with the second allowed model (`gemini-3.1-flash-lite`) under the same rule.
Code never edits a value itself. Re-reads are cached, count against `MAX_LLM_CALLS`, and are listed in the run summary as "re-read: field old -> new (accepted/rejected, model)". Bills that pass reconcile are never re-called. A payable gap equal to a printed FPA amount is not flagged.
`core/calc.py` separately derives every Level 2 figure (shares, averages, changes, late-payment cost, what-if estimates) from the printed values.

## Scorecard
Only KESC_0008 has a provided ground truth (`tests/golden/KESC_0008.json`, not shipped in the ZIP). Measured with `python scripts/compare_golden.py KESC_0008`:
| Prompt | Fields matching (+-1) | Misses |
|---|---|---|
| l1_v1 | 27/27 | none |
| l1_v2 | 25/27 | electricity duty read as 64.91 (printed 29.41); payable after due date 3574 (the 5% tier) instead of 3716 |
| l1_v3 | 27/27 | none |

The reconciliation engine flagged the electricity-duty misread (taxes do not add up to the printed total) but cannot catch the wrong late tier. `l1_v3` scored 27/27 on KESC_0008 but misread KESC_0004's payable amounts (computed -46306.77 instead of the printed 0). `l1_v4` read payable amounts exactly as printed (fixing KESC_0004) but dropped LESCO_0005's CR sign and scored 26/27 on KESC_0008 (late tier 3574 instead of 3716); with the late-amount cross-check applied to the cached results it is 27/27. `l1_v5` (current) restores the CR sentence and has not been scored yet.
The other four training bills (KESC_0004, LESCO_0005, LESCO_0006, IESCO_0004) have no ground truth. Their key fields were read against the images by the AI coding assistant only; they are not participant-verified, so no accuracy figure is claimed for them.

## Limitations and responsible AI
- Vision models can misread blurry or tilted photos, mix up signs on credits (CR) or confuse repeated meter rows. Values the model cannot read should be null; the pipeline does not recalculate or guess.
- Estimates (for example "what if I use 50 fewer units") are labelled as estimates with their formula and ignore tariff slabs.
- Privacy: names, addresses and ID or meter numbers are never requested, written to the CSVs or logged. Bill text is treated as untrusted data and instructions inside it are ignored. Images are sent to the Google Gemini API for processing.
- Accuracy can differ across providers and photo quality; check important numbers against the paper bill.

## AI usage
- Models and APIs: Google Gemini API with `gemini-3.5-flash-lite` (primary) and `gemini-3.1-flash-lite` (second reader for targeted re-reads, and failover). No other model or AI service is used at run time.
- AI coding tools: Claude Code (with a Claude Pro subscription) and the Claude chat assistant for planning. Cursor: none.
- Prohibited invoice-extraction services (Azure prebuilt-invoice, Textract AnalyzeExpense, Mindee): none. Local OCR: none.
