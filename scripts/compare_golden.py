"""Diff extracted Level 1 JSON against tests/golden/<bill_id>.json (numbers +-1, text/dates exact).

Usage: python scripts/compare_golden.py BILL_ID [--model MODEL] [--pred FILE]
Without --pred the cached extraction for BILL_ID (given model/version, else newest) is used. No LLM call is ever made.
If a cached extended-facts result exists, the late-amount cross-check is applied first (as in run.py); use --raw to skip it.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOL = 1.0


def is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def same(a, b) -> bool:
    if is_num(a) and is_num(b):
        return abs(a - b) <= TOL
    return a == b


def diff(golden: dict, pred: dict) -> list[tuple[str, object, object, bool]]:
    """Return (field, expected, got, ok) rows; list fields are compared item by item in order."""
    rows = []
    for key, exp in golden.items():
        got = pred.get(key, "<missing>")
        if isinstance(exp, list):
            if not isinstance(got, list):
                rows.append((key, exp, got, False))
                continue
            rows.append((f"{key}.count", len(exp), len(got), len(exp) == len(got)))
            for i in range(max(len(exp), len(got))):
                e = exp[i] if i < len(exp) else "<none>"
                g = got[i] if i < len(got) else "<none>"
                ok = isinstance(e, dict) and isinstance(g, dict) and e["type"] == g.get("type") and same(e["amount"], g.get("amount"))
                rows.append((f"{key}[{i}]", e, g, ok))
        else:
            rows.append((key, exp, got, same(exp, got)))
    for key in pred.keys() - golden.keys():
        rows.append((key, "<absent>", pred[key], False))
    return rows


def find_cached(bill_id: str, model: str | None, version: str | None) -> dict:
    pattern = f"{bill_id}__*__l1__{version or '*'}__{model or '*'}.json"
    files = sorted((ROOT / "cache").glob(pattern), key=lambda p: p.stat().st_mtime)
    if not files:
        sys.exit(f"no cached extraction for {bill_id} (model={model}); nothing compared")
    return json.loads(files[-1].read_text(encoding="utf-8"))["data"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bill_id")
    ap.add_argument("--model")
    ap.add_argument("--version", help="prompt version, e.g. l1_v1 (default: newest cached)")
    ap.add_argument("--pred")
    ap.add_argument("--raw", action="store_true", help="compare the cached Level 1 result without the late-amount cross-check")
    args = ap.parse_args()
    golden = json.loads((ROOT / "tests" / "golden" / f"{args.bill_id}.json").read_text(encoding="utf-8"))
    pred = json.loads(Path(args.pred).read_text(encoding="utf-8")) if args.pred else find_cached(args.bill_id, args.model, args.version)
    pred = pred.get("data", pred)
    if not args.pred and not args.raw:
        ext_files = sorted((ROOT / "cache").glob(f"{args.bill_id}__*__ext__*.json"), key=lambda p: p.stat().st_mtime)
        if ext_files:
            sys.path.insert(0, str(ROOT))
            from core.extract import raise_late_amount
            pred, note = raise_late_amount(pred, json.loads(ext_files[-1].read_text(encoding="utf-8"))["data"])
            if note:
                print(f"cross-check: {note}")
    rows = diff(golden, pred)
    for field, exp, got, ok in rows:
        print(f"{'OK  ' if ok else 'FAIL'} {field}: expected={exp} got={got}")
    passed = sum(ok for *_, ok in rows)
    print(f"\n{passed}/{len(rows)} fields match (tolerance +-{TOL:g})")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
