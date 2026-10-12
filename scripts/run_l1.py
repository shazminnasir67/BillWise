"""Extract Level 1 JSON for bills (cache first). Real model calls need ALLOW_LLM_CALLS=true.

Usage: python scripts/run_l1.py [--model MODEL] [--ext] BILL [BILL ...]
--ext also runs the extended-facts pass (one more call per uncached bill) and applies the late-amount cross-check.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.extract import ExtractionError, extract_bill, extract_extended, raise_late_amount  # noqa: E402
from core.llm import AllModelsFailed, LLMBlocked  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="single allowed model ID; default is MODEL_CHAIN")
    ap.add_argument("--ext", action="store_true", help="also run the extended-facts pass")
    ap.add_argument("bills", nargs="+")
    args = ap.parse_args()
    status = 0
    for bill in args.bills:
        try:
            r = extract_bill(bill, [args.model] if args.model else None)
        except (LLMBlocked, AllModelsFailed, ExtractionError) as e:
            print(f"{Path(bill).name}: FAILED {e}", file=sys.stderr)
            status = 1
            if isinstance(e, LLMBlocked):
                break
            continue
        print(f"== {Path(bill).name} model={r['model_used']} latency_ms={r['latency_ms']} cached={r['cached']}")
        print(json.dumps(r["data"], ensure_ascii=False))
        if args.ext:
            try:
                e = extract_extended(bill, [args.model] if args.model else None)
            except (LLMBlocked, AllModelsFailed, ExtractionError) as err:
                print(f"{Path(bill).name}: extended facts FAILED {err}", file=sys.stderr)
                status = 1
                if isinstance(err, LLMBlocked):
                    break
                continue
            final, note = raise_late_amount(r["data"], e["data"])
            late = [(x["label"], x["amount"]) for x in e["data"]["late_payment"]]
            print(f"   ext model={e['model_used']} cached={e['cached']} meter_rows={e['data']['meter_rows']} late_payment={late}")
            print(f"   payable_within={final['payable_within_due_date']} payable_after={final['payable_after_due_date']}"
                  f"{' <- ' + note if note else ' (cross-check: unchanged)'}")
    return status


if __name__ == "__main__":
    sys.exit(main())
