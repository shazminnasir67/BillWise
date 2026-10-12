"""Read the CSV templates and write the submission CSVs with the csv library (UTF-8, same columns and row order)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

L1_FIELDS = ["provider", "tariff", "sanctioned_load_kw", "bill_month", "reading_date", "issue_date", "due_date",
             "previous_reading", "current_reading", "units_consumed", "charges", "total_charges", "taxes",
             "total_taxes", "current_bill", "arrears", "payable_within_due_date", "payable_after_due_date"]
FALLBACK_ANSWER = "Not shown on this bill. The bill could not be read reliably, so no answer is given."


def read_template(path: str | Path) -> tuple[list[str], list[dict]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def find_templates(folder: str | Path) -> tuple[Path, Path]:
    files = list(Path(folder).glob("*.csv"))
    def norm(f: Path) -> str:
        return "".join(ch for ch in f.name.lower() if ch.isalnum())

    l1 = [f for f in files if "level1" in norm(f)]
    l2 = [f for f in files if "level2" in norm(f)]
    if len(l1) != 1 or len(l2) != 1:
        raise FileNotFoundError(f"need exactly one level1 and one level2 CSV in {folder}; found {[f.name for f in files]}")
    return l1[0], l2[0]


def fill_column(fieldnames: list[str], rows: list[dict], preferred: tuple[str, ...]) -> str:
    """The column to fill: a preferred name if present, else the first column that is empty in every template row."""
    for name in preferred:
        if name in fieldnames:
            return name
    for name in fieldnames:
        if all(not (r.get(name) or "").strip() for r in rows):
            return name
    raise ValueError(f"cannot find the column to fill in {fieldnames}")


def null_l1() -> dict:
    """Null-safe Level 1 JSON used when a bill fails: every field present."""
    d = {k: None for k in L1_FIELDS}
    d["charges"], d["taxes"] = [], []
    return d


def _tidy(v):
    """Print whole-number floats as integers (900.0 -> 900); values are otherwise untouched."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, list):
        return [_tidy(x) for x in v]
    if isinstance(v, dict):
        return {k: _tidy(x) for k, x in v.items()}
    return v


def l1_cell(data: dict) -> str:
    return json.dumps(_tidy(data), ensure_ascii=False)


def _write(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)


def write_level1(template: str | Path, out: str | Path, results: dict[str, dict]) -> int:
    """results: bill_id -> L1 dict. Missing bills get the null-safe JSON. Returns rows written."""
    fields, rows = read_template(template)
    col = fill_column(fields, rows, ("json", "extracted_json", "output"))
    for r in rows:
        r[col] = l1_cell(results.get(r["bill_id"]) or null_l1())
    _write(Path(out), fields, rows)
    return len(rows)


def write_level2(template: str | Path, out: str | Path, answers: dict[str, dict[str, str]]) -> int:
    """answers: bill_id -> {question_id: text}. Missing answers get the fallback text. Returns rows written."""
    fields, rows = read_template(template)
    col = fill_column(fields, rows, ("answer",))
    for r in rows:
        text = (answers.get(r["bill_id"], {}).get(r["question_id"]) or "").strip()
        r[col] = " ".join(text.split()) or FALLBACK_ANSWER
    _write(Path(out), fields, rows)
    return len(rows)
