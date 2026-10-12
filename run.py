"""One command: bills folder + CSV templates -> output/level1.csv and output/level2.csv.

    python run.py --bills data/test/bills --templates data/test/csv --out output

Real model calls need ALLOW_LLM_CALLS=true in the environment (see README).
"""
import argparse
import logging
import os
import sys
from pathlib import Path

from core import cache, csv_io, extract, llm, qa
from core.calc import compute_stats
from core.extract import extract_bill, extract_extended, raise_late_amount, reread_bill, reread_calls_needed
from core.qa import answer_questions
from core.reconcile import fpa_amounts, reconcile

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
REQUESTS_PER_MIN = 12
log = logging.getLogger("run")


def find_bill(folder: Path, bill_id: str) -> Path | None:
    for p in folder.iterdir():
        if p.suffix.lower() in IMAGE_EXT and p.stem.lower() == bill_id.lower():
            return p
    return None


def _cached_data(path: Path, kind: str, version: str, chain: list[str]) -> dict | None:
    for m in chain:
        hit = cache.get(path, kind, version, m)
        if hit:
            return hit["data"]
    return None


def planned_calls(path: Path, questions: list[dict], chain: list[str]) -> int:
    """Model calls this bill still needs (one per stage that is not in the cache). Reads the cache only.
    The question stage is cached only when Level 1 and extended facts are too, because its key includes the stats."""
    l1 = _cached_data(path, extract.KIND, extract.PROMPT_VERSION, chain)
    ext = _cached_data(path, extract.EXT_KIND, extract.EXT_PROMPT_VERSION, chain)
    calls = (l1 is None) + (ext is None)
    if l1 is None or ext is None:
        return calls + 1
    l1 = raise_late_amount(l1, ext)[0]
    needed = reread_calls_needed(path, l1, ext, chain)
    if needed:
        return calls + needed + 1  # the re-read(s), then the questions again because Level 1 may change
    l1 = reread_bill(path, l1, ext, chain)[0]  # cached or nothing to re-read: no model call
    stats = compute_stats(l1, ext, questions)
    kind = qa.kind_for(questions, stats)
    return calls + (not any(cache.get(path, kind, qa.QA_VERSION, m) for m in chain))


def process_bill(path: Path, questions: list[dict]) -> tuple[dict | None, dict | None, dict[str, str], list[str], str | None]:
    """Returns (level1 or None, extended facts or None, answers, problems, late-amount note or None).
    The late-amount cross-check runs before the stats and questions, so every output sees the same value.
    Each stage fails on its own; never raises except LLMBlocked."""
    problems: list[str] = []
    l1 = ext = None
    try:
        l1 = extract_bill(path)["data"]
    except llm.LLMBlocked:
        raise
    except Exception as e:  # one bad bill must not stop the run
        problems.append(f"level1: {e}")
    try:
        ext = extract_extended(path)["data"]
    except llm.LLMBlocked:
        raise
    except Exception as e:
        problems.append(f"extended facts: {e}")
    answers: dict[str, str] = {}
    raised = None
    if l1 is not None:
        l1, raised = raise_late_amount(l1, ext)
        try:
            l1, reread_notes = reread_bill(path, l1, ext)
            if reread_notes:
                raised = "; ".join(filter(None, [raised, *reread_notes]))
        except llm.LLMBlocked:  # no model calls allowed: keep the printed values and say so
            raised = "; ".join(filter(None, [raised, "re-read skipped (model calls are off or capped)"]))
        except Exception as e:  # a failed re-read keeps the original values
            problems.append(f"re-read: {e}")
        try:
            stats = compute_stats(l1, ext or {}, questions)
            answers = answer_questions(path, l1, ext or {}, stats, questions)["answers"]
        except llm.LLMBlocked:
            raise
        except Exception as e:
            problems.append(f"level2: {e}")
    return l1, ext, answers, problems, raised


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bills", required=True, help="folder of bill images (file stem = bill_id)")
    ap.add_argument("--templates", required=True, help="folder with the level1 and level2 template CSVs")
    ap.add_argument("--out", default="output")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    llm.set_min_interval(60 / REQUESTS_PER_MIN)

    t1, t2 = csv_io.find_templates(args.templates)
    _, l1_rows = csv_io.read_template(t1)
    _, l2_rows = csv_io.read_template(t2)
    bill_ids = list(dict.fromkeys(r["bill_id"] for r in l1_rows))
    folder = Path(args.bills)

    chain = llm.get_chain()
    per_bill = {b: [{"question_id": r["question_id"], "question": r["question"]} for r in l2_rows if r["bill_id"] == b]
                for b in bill_ids}
    paths = {b: find_bill(folder, b) for b in bill_ids}
    planned = sum(planned_calls(p, per_bill[b], chain) for b, p in paths.items() if p)
    cap = int(os.environ.get("MAX_LLM_CALLS", "40"))
    print(f"Plan: {len(bill_ids)} bills, {planned} model call(s) needed (the rest come from cache), "
          f"model chain {chain}, cap MAX_LLM_CALLS={cap}, retries add calls")
    if planned and os.environ.get("ALLOW_LLM_CALLS", "false").strip().lower() != "true":
        print("ALLOW_LLM_CALLS is not true: bills that need a model call will fail and get null-safe rows.")
    if planned > cap:
        print(f"WARNING: {planned} calls needed but the cap is {cap}; raise MAX_LLM_CALLS to finish every bill.")

    l1_results: dict[str, dict] = {}
    answers: dict[str, dict[str, str]] = {}
    attention: dict[str, list[str]] = {}
    notes: dict[str, list[str]] = {}
    failed = False  # a hard failure (missing image, extraction or answer error), as opposed to a reconcile flag
    for bill_id in bill_ids:
        questions = per_bill[bill_id]
        path = paths[bill_id]
        if path is None:
            attention[bill_id] = [f"no image named {bill_id} in {folder}"]
            failed = True
            continue
        try:
            l1, ext, ans, problems, raised = process_bill(path, questions)
        except llm.LLMBlocked as e:
            log.error("stopped: %s", e)
            attention[bill_id] = [str(e)]
            failed = True
            break
        if l1 is not None:
            l1_results[bill_id] = l1
        if raised:
            notes.setdefault(bill_id, []).append(raised)
        answers[bill_id] = ans
        if problems or len(ans) < len(questions):
            attention[bill_id] = problems or ["some answers missing"]
            failed = True
        if l1 is not None:
            rec = reconcile(l1, (ext or {}).get("meter_rows"), fpa_amounts(ext))  # reports only; extracted values are never changed
            if rec["reasons"]:
                attention.setdefault(bill_id, []).extend(f"reconcile {r}" for r in rec["reasons"])
            notes.setdefault(bill_id, []).extend(rec["warnings"])
        log.info("%s: %s", bill_id, "needs attention" if bill_id in attention else "ok")

    for bill_id in bill_ids:
        if bill_id not in answers and bill_id not in attention:
            attention[bill_id] = ["not processed (run stopped early)"]
            failed = True

    out = Path(args.out)
    n1 = csv_io.write_level1(t1, out / "level1.csv", l1_results)
    n2 = csv_io.write_level2(t2, out / "level2.csv", answers)
    print(f"\nlevel1.csv: {n1} rows | level2.csv: {n2} rows | LLM calls used: {llm.calls_made()}")
    print(f"bills ok: {len(bill_ids) - len(attention)}/{len(bill_ids)}")
    for bill_id, probs in attention.items():
        print(f"  NEEDS ATTENTION {bill_id}: {'; '.join(probs)}")
    for bill_id, warns in notes.items():
        if not warns:
            continue
        print(f"  note {bill_id}: {'; '.join(warns)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
