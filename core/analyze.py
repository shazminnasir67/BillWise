"""One bill, end to end for the UI: Level 1, extended facts, late-amount cross-check, targeted re-read, reconcile."""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from core import csv_io
from core import cache, extract as ex
from core.extract import extract_bill, extract_extended, raise_late_amount, reread_bill, reread_calls_needed
from core.reconcile import fpa_amounts, reconcile


def analyze(path: str | Path, chain: list[str] | None = None) -> dict:
    """Same order as run.py. Model calls only happen for uncached stages (and need ALLOW_LLM_CALLS=true)."""
    l1 = extract_bill(path, chain)
    ext = extract_extended(path, chain)
    data, raised = raise_late_amount(l1["data"], ext["data"])
    meter, fpa = ext["data"].get("meter_rows"), fpa_amounts(ext["data"])
    before = reconcile(data, meter, fpa)
    data, reread_notes = reread_bill(path, data, ext["data"], chain)
    rec = reconcile(data, ext["data"].get("meter_rows"), fpa_amounts(ext["data"]))
    return {"l1": data, "ext": ext["data"], "reconcile": rec, "before": before, "notes": [n for n in [raised, *reread_notes] if n],
            "models": {"level1": l1["model_used"], "extended": ext["model_used"]},
            "cached": {"level1": l1["cached"], "extended": ext["cached"]}}


def level1_csv(bill_id: str, data: dict) -> str:
    """One-row CSV in the submission format (bill_id, json), JSON on one line."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["bill_id", "json"])
    w.writerow([bill_id, csv_io.l1_cell(data)])
    return buf.getvalue()


def badge(status: str | None) -> str:
    """Field badge from reconcile: verified by a passing check, unverified, warning, or failed check."""
    return {"ok": "green", "warning": "amber", "mismatch": "red"}.get(status or "", "amber")


def json_text(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def cached_analysis(path: str | Path, chain: list[str] | None = None) -> dict | None:
    """The full analysis if every stage is already cached (so no model call can happen), else None."""
    from core import llm
    chain = chain or llm.get_chain()

    def hit(kind, version):
        return next((cache.get(path, kind, version, m) for m in chain if cache.get(path, kind, version, m)), None)

    l1, ext = hit(ex.KIND, ex.PROMPT_VERSION), hit(ex.EXT_KIND, ex.EXT_PROMPT_VERSION)
    if not l1 or not ext:
        return None
    data = raise_late_amount(l1["data"], ext["data"])[0]
    if reread_calls_needed(path, data, ext["data"], chain):
        return None
    return analyze(path, chain)
