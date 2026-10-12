# PROJECT.md — BillWise (living state)

## Clock
- Quest start: 2026-10-10 11:35 PKT (recorded at start of session)
- Test data drops: 12:30 PKT

## Status
- STEP 1 environment + core/llm.py: done
- Budget lock (ALLOW_LLM_CALLS, MAX_LLM_CALLS, cache): implemented, mocked tests only
- STEP 2 bill study: done (notes below, read by my own vision, no model call)
- STEP 3 Level 1 extractor + compare script: written, NOT yet run on a real model (awaiting approval)
- STEP 4 model comparison: pending

## Decisions
- Python 3.13.13 in .venv. Allowed models only: claude-haiku-4-5, gemini-3.5-flash-lite, gemini-3.1-flash-lite, gpt-4.1-mini, gpt-5-mini.
- Keys present in .env: GEMINI only. Anthropic/OpenAI not available yet.
- Old 241-line pip-freeze requirements.txt replaced by a minimal pinned file.

## Risks / blockers
- tests/golden/KESC_0008.json was pasted by the user in chat and saved verbatim; it matches my own reading of the image.

## Bill layout notes (training set, studied with own vision)
All bills: PII blocks are solid grey boxes; labels are bilingual English/Urdu, values are Latin digits. No bill is mirrored.

### KESC_0008.png (KE, 1653x2339, flat digital render, no tilt)
- Header: "Bill Month: Apr-26", "Issue Date: 07-Apr-2026". Top-right Customer Information: Sanc Load 3, Tariff A1-R.
- Four orange summary boxes: Amount Due 3,430 (late: 22-24 Apr 3,574; after 24-Apr 3,716), Current Month 151 units = 3,430.24, Previous Dues -0.53, Due Date 21st Apr 2026.
- "Electricity You Have Used" table (one Energy row): previous 8819, current 8970, units 151.
- Right column "Bill Calculation": Reading Date 03-Apr-26; lines Fixed 900, Variable <=100 1,054, Variable <=200 663.51, UQA 52.91, FCA 125.27, Additional Surcharge (PHL) 64.93; subtotal "Electricity Charges" 2,860.62; Electricity Duty 29.41, Sales Tax 520.21, MUCT (KMC) 20.00; "Taxes and Duties" 569.62; current month 3,430.24; Previous Dues/Remaining -0.53; Late Payment Surcharge 5% 143.03 and 10% 286.06.
- 13-month bar chart (units per month), Billing & Payment History (3 rows), Message Board, energy/tax percent box (one value greyed).
- Ambiguities: two late amounts (3,574 and 3,716) -> highest. Previous dues is negative (credit). "Opening balance" and "Payments/Adjustment" rows are not schema fields.

### KESC_0004.png (KE, same template, Jun-26, issue 29-Jun-2026, net-metering "NM")
- Sanc Load 10, Tariff A1-R (the tariff row is partly overlapped by a grey box). Due 13th Jul 2026. Reading date 25-Jun-26.
- Readings table has FOUR rows: Energy Off Peak 1163.29->1923.73 (760.44), Energy Peak 402.35->542.85 (140.5), Import Energy Off Peak 10865.55->12238.4 (1372.85), Import Energy Peak 4.97->6.53 (1.56). Headline "901 units" = 760.44+140.5 (900.94). AMBIGUOUS: no single previous/current reading pair; the schema needs one decision (flag for user).
- Charges: Fixed 3,375, Variable Off Peak 26,257.99, Variable Peak 6,582.43, UQA -1,442.74 (negative as printed), UQA 61.10 (two UQA lines), FCA 354.19, Additional Surcharge 2,910.04; subtotal 38,098.01. Taxes: Electricity Duty 520.85, Sales Tax 6,951.39 (no MUCT); total 7,472.24. Current bill 45,570.25.
- Credits: Previous Dues -91,877.02, Net Metering Benefit -114,640.13 (no schema field), Outstanding Balance -160,946.90. Amount payable 0 before and after due date (late boxes show Rs.0).
- Ambiguity: due_date vs "Till 14-Jul-26-16-Jul-26" grace window; arrears -91,877.02 printed negative.

### LESCO_0005.png (LESCO, 1080x1198, phone photo of paper, slight perspective, glare, cropped bottom)
- BILL MONTH JUL 2026, Reading 17 JUL 26, Issue 20 JUL 26, Due 30 JUL 26. San Load 10, MDI 5. Category DOMESTIC; Tariff text A-1b(03)T is overlapped by the category text (hard to read). "NET METERING CONNECTION".
- Meter Info: 4 rows (MF 1): 11561->12578 = 1,017; 3260->3562 = 302; 21822->22859 = 1,037; 5->5. AMBIGUOUS which row is the billed consumption.
- Bill Charges Breakdown shows only totals, no per-line charges: Total Electricity Charges 17,367.77, Subsidies blank, Net 17,367.77, Taxes 3,358.23 (combined only), Current Bill 20726, Grand Total 54770CR. Arrears -75496 printed in the right column.
- Payable within due date "54770CR" -> -54770. LP surcharge 0 / 0; payable after due date says "NOT TO BE PAID" -> null.
- Bill History: 12 months in two blocks; column headers in the first block are garbled by the photo; units are negative (net metering).
- Checks: 17,367.77+3,358.23=20,726; -75,496+20,726=-54,770.

### LESCO_0006.png (LESCO, 720x790, phone photo on grass, rotated/skewed, crumpled, shadows, low resolution)
- BILL MONTH AUG 2026, Reading 26 AUG 26, Issue 29 AUG 26, Due 09 SEP 26. San Load 1, MDI 1, Category COMMERCIAL, Tariff A-2a(04).
- Meter: previous 22238, present 22303, units 65 (single row).
- Charges breakdown totals only: Total Electricity Charges 3,777.49, Subsidies blank, Net 3,777.49, Taxes 1,723.51 (combined), Current Bill 5501, Grand Total 44912. Arrears printed "39411 / 2" (likely 39411.2; the printed value is ambiguous).
- Payable within due date 44912; LP surcharge 176 / 351; payable after due date: Upto 14/09/26 45088, After 14/09/26 45263 -> highest 45263. Note the late tiers use 14/09/26, not the 09 SEP due date.
- Checks: 3,777.49+1,723.51=5,501; 5,501+39,411.2=44,912.2.

### IESCO_0004.jpg (IESCO, 698x985, low-resolution screenshot/photo, no visible tilt)
- BILL MONTH SEP 26, Reading 25 SEP 26, Issue 26 SEP 26, Due 08 OCT 26. San Load 3, Category Domestic ("Protected"), Tariff A-1A(01).
- Meter: previous 9301, present 9459, units 158.
- Breakdown: Total Electricity Charges 6102, Subsidies 3332, Net Electricity Charges (84.76%) 2770, Taxes (15.24%) 498 (combined), Current Bill 3267, Arrears 0, Installment 0, Adjustments 0, W.E Credit 0, Lock Open Credit 0, Total FPA 267, Grand Total 3535.
- Payable within due date 3535; LP surcharge 139 / 277; after due date: Till 12-Oct-26 3,674, After 12-Oct-26 3,812 -> highest 3812.
- Ambiguities: 2770+498=3268 vs printed 3267 and 3267+267=3534 vs 3535 (rounding on the bill; report, do not fix). total_charges could be 6102 (gross) or 2770 (net): rule says as printed under "Total Electricity Charges". Subsidy 3332 is positive on the page but is a deduction -> negative. FPA 267 is a separate line outside Current Bill.
- Percentages 84.76/15.24 sit next to the labels; do not mistake them for amounts.

## Progress log (12:05)
- Real calls so far: 5 (all L1 v1, gemini-3.5-flash-lite). KESC_0008 v1 = 27/27 vs golden. User runs every further real call himself/herself.
- Built (mock-tested, 17 pytest): Level 2 pipeline (extended facts, calc, qa, csv_io, run.py), reconcile.py, validate_submission.py, package_zip.sh (verified on a fake output copy).
- prompts/l1_v2.md written, extract.PROMPT_VERSION = l1_v2 (v1 cache no longer used by run.py). NOT yet run on any bill.
- v1 issues to confirm with v2: LESCO_0005 payable CR sign, IESCO_0004 combined tax + FPA, KESC_0004 headline units/electricity_duty type, LESCO_0006 arrears null.
- Skipped for now: FastAPI + frontend (step D).

## Progress log (12:45)
- Real calls so far: 5 (v1) + 5 (v2) + 5 (v3) + 2 (ext+qa KESC_0008) by the user. KESC_0008: v1 27/27, v2 25/27, v3 27/27.
- v3 bug: KESC_0004 payable computed (-46306.77) instead of printed 0 -> fixed in l1_v4 (not yet run). qa_v2 + answer-quality retry/trim + question-number thresholds written (mock-tested, 28 pytest).

## Fixes and accuracy log (issue -> what we did -> measured result)
Measured = real run by the user on the 5 training bills with gemini-3.5-flash-lite. Only KESC_0008 has ground truth.
| Issue | What we did | Result |
|---|---|---|
| LESCO_0005 payable lost its CR sign (54770 instead of -54770) | v2 rule: CR makes every amount negative | fixed in v2 and v3; v4 REGRESSED: payable_within is now null (CR wording removed from the payable rule) |
| IESCO_0004 combined tax typed as gst | v2 rule: combined tax -> taxes [], amount in total_taxes | fixed in v2, v3, v4 |
| KESC_0004 electricity duty typed other_tax | v2 name->type map | fixed in v2, v3, v4 |
| KESC_0004 units 760.44 vs headline 901 | v2 rule: use the printed "N units" headline | fixed; reconcile now shows it as a warning for multi-meter bills |
| LESCO_0006 arrears null ("39411 / 2") | v2 rule: slash = decimal point | fixed (39411.2) in v2, v3, v4 |
| LESCO charges wrongly got an energy line | v3: gross/net handling only for a non-zero subsidy | fixed in v3, v4 |
| KESC_0004 payable computed as -46306.77 (printed 0) | v4: payables read exactly as printed, never computed | fixed in v4 (0 / 0) |
| KESC_0008 electricity duty misread 64.91 (v2) | v3: read every digit; reconcile taxes_sum flags such misreads | v3, v4 correct (29.41) |
| KESC_0008 payable_after flips between 3716 (v1, v3) and 3574 (v2, v4) | prompts say "use the highest" | NOT FIXED: v4 = 26/27 on golden; model is inconsistent on this field |
| IESCO FPA 267 | decided to leave out (printed outside the charges lines); reconcile flags payable_balance | by design |
| Level 2 Q06 rambling answer, Q07 outside knowledge | qa_v2 prompt + code check (retry, then trim), thresholds computed from numbers in the question | written and mock-tested; NOT yet measured on a real run |
| LESCO_0005 payable null in v4 (CR sentence removed) | v5: added "a payable amount printed with a CR suffix is negative (e.g. 54770CR -> -54770)" | written; NOT yet measured on a real run |
| KESC_0008 payable_after flips 3716 / 3574 | code cross-check `raise_late_amount`: raises a non-null payable_after to the highest printed "payable" late amount from the extended facts; never lowers, never touches null; note in run summary | mock-tested (4 tests). Offline check on the cached v4 + extended facts: 26/27 -> 27/27. NOT yet measured on a fresh real run |
