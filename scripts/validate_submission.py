"""Final pre-ZIP validator. Exit code 0 only if every check passes.

    python scripts/validate_submission.py --templates data/test/csv --output output [--zip submission.zip]
"""
import argparse
import csv
import json
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.csv_io import L1_FIELDS, fill_column, find_templates, read_template  # noqa: E402

NUMERIC = ["sanctioned_load_kw", "previous_reading", "current_reading", "units_consumed", "total_charges",
           "total_taxes", "current_bill", "arrears", "payable_within_due_date", "payable_after_due_date"]
DATES = ["reading_date", "issue_date", "due_date"]
CHARGE_TYPES = {"energy", "fixed", "fpa", "quarterly_adjustment", "surcharge", "meter_rent", "subsidy", "other"}
TAX_TYPES = {"gst", "electricity_duty", "income_tax", "municipal_tax", "other_tax"}
PII = [(re.compile(r"\b\d{5}-\d{7}-\d\b"), "CNIC-like number"), (re.compile(r"\b\d{9,}\b"), "long digit string"),
       (re.compile(r"\b(Name|Address)\s*:", re.I), "'Name:'/'Address:' label")]
SECRETS = [re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
           re.compile(r"(?i)(api_key|secret|token)\s*=\s*['\"]?[A-Za-z0-9_\-]{24,}")]
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "data", "cache", "scratch", "output", "node_modules", ".claude"}

failures: list[str] = []


def check(ok: bool, msg: str) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {msg}")
    if not ok:
        failures.append(msg)


def is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def valid_l1(d: object) -> list[str]:
    if not isinstance(d, dict):
        return ["not a JSON object"]
    errs = [f"missing field {k}" for k in L1_FIELDS if k not in d]
    errs += [f"unexpected field {k}" for k in d if k not in L1_FIELDS]
    if errs:
        return errs
    if d["provider"] not in ("KE", "LESCO", "IESCO", None):
        errs.append("provider not in KE/LESCO/IESCO")
    errs += [f"{k} must be a number or null" for k in NUMERIC if d[k] is not None and not is_num(d[k])]
    if d["tariff"] is not None and not isinstance(d["tariff"], str):
        errs.append("tariff must be a string or null")
    for k in DATES:
        try:
            if d[k] is not None:
                datetime.strptime(d[k], "%Y-%m-%d")
        except (ValueError, TypeError):
            errs.append(f"{k} not YYYY-MM-DD")
    if d["bill_month"] is not None and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(d["bill_month"])):
        errs.append("bill_month not YYYY-MM")
    for key, allowed in (("charges", CHARGE_TYPES), ("taxes", TAX_TYPES)):
        if not isinstance(d[key], list):
            errs.append(f"{key} must be a list")
            continue
        for item in d[key]:
            if not (isinstance(item, dict) and item.get("type") in allowed and is_num(item.get("amount"))):
                errs.append(f"bad {key} item {item}")
    return errs


def read_rows(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, newline="", encoding="utf-8") as f:  # strict UTF-8
        r = csv.DictReader(f)
        return list(r.fieldnames or []), list(r)


def check_csvs(templates: Path, output: Path, n1: int, n2: int) -> None:
    try:
        t1, t2 = find_templates(templates)
    except FileNotFoundError as e:
        check(False, str(e))
        return
    for name, tpl, n in (("level1.csv", t1, n1), ("level2.csv", t2, n2)):
        out = output / name
        check(out.exists(), f"{name} exists")
        if not out.exists():
            continue
        tf, trows = read_template(tpl)
        of, orows = read_rows(out)
        check(len(orows) == n, f"{name} has exactly {n} rows (found {len(orows)})")
        check(of == tf, f"{name} column names match the template")
        locked = [c for c in tf if c != fill_column(tf, trows, ("json", "extracted_json", "output") if name == "level1.csv" else ("answer",))]
        same = len(orows) == len(trows) and all(o.get(c) == t.get(c) for o, t in zip(orows, trows) for c in locked)
        check(same, f"{name} row order and locked columns (bill_id/question_id/question) unchanged")
        fill = fill_column(tf, trows, ("json", "extracted_json", "output") if name == "level1.csv" else ("answer",))
        check(all((r.get(fill) or "").strip() for r in orows), f"{name} has no empty '{fill}' cells")
        check(all(all((v or "").strip() != "" for v in r.values()) for r in orows), f"{name} has no empty cells at all")
        if name == "level1.csv":
            bad = []
            for r in orows:
                cell = r[fill]
                try:
                    errs = ["multi-line cell"] if "\n" in cell or "\r" in cell else valid_l1(json.loads(cell))
                except json.JSONDecodeError as e:
                    errs = [f"invalid JSON ({e})"]
                if errs:
                    bad.append(f"{r.get('bill_id')}: {errs[:3]}")
            check(not bad, f"level1 JSON valid, one line, correct types {bad[:3] if bad else ''}")
        hits = [f"{r.get('bill_id')}: {label}" for r in orows for cell in r.values() for rx, label in PII if rx.search(cell or "")]
        check(not hits, f"{name} has no PII-like patterns {hits[:3] if hits else ''}")


def source_files() -> list[Path]:
    out = []
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if p.is_file() and not (set(rel.parts) & SKIP_DIRS) and p.suffix in {".py", ".md", ".js", ".html", ".css", ".sh", ".txt", ".example", ".json", ".ini"}:
            out.append(p)
    return out


def check_source() -> None:
    leaks = [str(p.relative_to(ROOT)) for p in source_files() if p.name != "validate_submission.py"
             and any(rx.search(p.read_text(encoding="utf-8", errors="ignore")) for rx in SECRETS)]
    check(not leaks, f"no key-like strings in source {leaks}")
    check((ROOT / ".env.example").exists(), ".env.example present")
    reqs = ROOT / "requirements.txt"
    lines = [l.strip() for l in reqs.read_text().splitlines() if l.strip() and not l.startswith("#")] if reqs.exists() else []
    check(bool(lines) and all("==" in l for l in lines), "requirements.txt present with exact == pins")
    readme = (ROOT / "README.md").read_text(encoding="utf-8") if (ROOT / "README.md").exists() else ""
    check(bool(re.search(r"Python\s*3\.\d+", readme)), "README states the Python version")
    check("python run.py" in readme, "README has the run command")
    check(bool(re.search(r"(?im)^#+\s*AI usage", readme)), "README has an AI usage section")


def check_zip(zip_path: Path) -> None:
    check(zip_path.exists(), f"{zip_path.name} exists")
    if not zip_path.exists():
        return
    mb = zip_path.stat().st_size / 1_048_576
    check(mb <= 15, f"ZIP size {mb:.2f} MB <= 15 MB")
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        tops = {n.split("/")[0] for n in names}
        check(tops <= {"output", "source", "demo"} and {"output", "source"} <= tops, f"ZIP top-level is output/, source/, optional demo/ (found {sorted(tops)})")
        bad = [n for n in names if re.search(r"(^|/)(\.env|\.venv|venv|node_modules|cache|data)(/|$)", n) or n.endswith(".pyc")]
        check(not bad, f"ZIP has no .env/venv/cache/data/node_modules {bad[:3]}")
        leaks = [n for n in names if n.endswith((".py", ".md", ".txt", ".js", ".html", ".example"))
                 and any(rx.search(z.read(n).decode("utf-8", "ignore")) for rx in SECRETS) and not n.endswith("validate_submission.py")]
        check(not leaks, f"ZIP content has no key-like strings {leaks}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--templates", default="data/test/csv")
    ap.add_argument("--output", default="output")
    ap.add_argument("--zip", help="also validate this ZIP")
    ap.add_argument("--l1-rows", type=int, default=10)
    ap.add_argument("--l2-rows", type=int, default=120)
    a = ap.parse_args()
    check_csvs(Path(a.templates), Path(a.output), a.l1_rows, a.l2_rows)
    check_source()
    if a.zip:
        check_zip(Path(a.zip))
    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
