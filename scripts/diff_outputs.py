"""Before/after table of the Level 1 fields that differ between two output folders. Makes no LLM call.

Usage: python scripts/diff_outputs.py output output_v2
"""
import csv
import json
import sys
from pathlib import Path


def load(folder: str) -> dict[str, dict]:
    with open(Path(folder) / "level1.csv", newline="", encoding="utf-8") as f:
        return {r["bill_id"]: json.loads(r["json"]) for r in csv.DictReader(f)}


def level2_changes(a: str, b: str) -> int:
    def rows(folder):
        with open(Path(folder) / "level2.csv", newline="", encoding="utf-8") as f:
            return {(r["bill_id"], r["question_id"]): r["answer"] for r in csv.DictReader(f)}
    ra, rb = rows(a), rows(b)
    return sum(ra[k] != rb.get(k) for k in ra)


def main(before: str, after: str) -> int:
    old, new = load(before), load(after)
    changed = 0
    print(f"{'bill':<11} {'field':<24} {'before':<42} after")
    for bill, d in old.items():
        for field, value in d.items():
            if new.get(bill, {}).get(field) != value:
                changed += 1
                print(f"{bill:<11} {field:<24} {json.dumps(value)[:40]:<42} {json.dumps(new[bill][field])[:60]}")
    print(f"\n{changed} Level 1 field(s) changed; {level2_changes(before, after)} of 120 Level 2 answers differ")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
