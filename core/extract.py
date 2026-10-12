"""Level 1 extraction: one bill image -> validated JSON dict (cache first, one retry on invalid output)."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictFloat, StrictInt, ValidationError, field_validator

from core import cache, llm
from core.reconcile import fpa_amounts, reconcile

PROMPT_VERSION = "l1_v5"
KIND = "l1"
EXT_PROMPT_VERSION = "ext_v1"
EXT_KIND = "ext"
PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

Num = StrictInt | StrictFloat


class ExtractionError(Exception):
    pass


class Charge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["energy", "fixed", "fpa", "quarterly_adjustment", "surcharge", "meter_rent", "subsidy", "other"]
    amount: Num


class Tax(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["gst", "electricity_duty", "income_tax", "municipal_tax", "other_tax"]
    amount: Num


class BillL1(BaseModel):
    """Every field is required (so it must be present) but nullable."""
    model_config = ConfigDict(extra="forbid")
    provider: Literal["KE", "LESCO", "IESCO"] | None
    tariff: str | None
    sanctioned_load_kw: Num | None
    bill_month: str | None
    reading_date: str | None
    issue_date: str | None
    due_date: str | None
    previous_reading: Num | None
    current_reading: Num | None
    units_consumed: Num | None
    charges: list[Charge]
    total_charges: Num | None
    taxes: list[Tax]
    total_taxes: Num | None
    current_bill: Num | None
    arrears: Num | None
    payable_within_due_date: Num | None
    payable_after_due_date: Num | None

    @field_validator("reading_date", "issue_date", "due_date")
    @classmethod
    def _date(cls, v):
        if v is not None:
            datetime.strptime(v, "%Y-%m-%d")
        return v

    @field_validator("bill_month")
    @classmethod
    def _month(cls, v):
        if v is not None and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", v):
            raise ValueError("bill_month must be YYYY-MM")
        return v


class UsagePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str
    units: Num | None


class BillingPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str
    billed_amount: Num | None
    payment_date: str | None
    payment_amount: Num | None


class LatePayment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    amount: Num
    valid_from: str | None
    valid_until: str | None


class ChargeLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    units: Num | None
    rate: Num | None
    amount: Num


class ExtendedFacts(BaseModel):
    """Extra printed facts needed for Level 2 (history, late payment, shares, notices)."""
    model_config = ConfigDict(extra="forbid")
    usage_history: list[UsagePoint]
    billing_history: list[BillingPoint]
    late_payment: list[LatePayment]
    charge_lines: list[ChargeLine]
    energy_share_percent: Num | None
    tax_share_percent: Num | None
    message_board: str | None
    meter_rows: StrictInt | None


def _cached(path: Path, chain: list[str], kind: str, version: str) -> dict | None:
    for model in chain:
        hit = cache.get(path, kind, version, model)
        if hit:
            return {**hit, "cached": True}
    return None


def _extract(path: Path, chain: list[str] | None, kind: str, version: str, schema: type[BaseModel]) -> dict:
    """Cache first, then one LLM call, then one retry that feeds the validation error back."""
    chain = chain or llm.get_chain()
    hit = _cached(path, chain, kind, version)
    if hit:
        return hit

    def _validate(text: str) -> dict:
        return schema.model_validate_json(text).model_dump()

    prompt = (PROMPT_DIR / f"{version}.md").read_text(encoding="utf-8")
    purpose = f"{kind} extract {path.name}"
    result = llm.generate(prompt, images=[path], json_mode=True, chain=chain, purpose=purpose)
    try:
        data = _validate(result.text)
    except (ValidationError, ValueError) as first:
        retry = (f"{prompt}\n\nYour previous answer was rejected.\nPrevious answer:\n{result.text}\n"
                 f"Validation error:\n{first}\nReturn a corrected JSON object only.")
        result = llm.generate(retry, images=[path], json_mode=True, chain=chain, purpose=purpose + " (retry)")
        try:
            data = _validate(result.text)
        except (ValidationError, ValueError) as second:
            raise ExtractionError(f"{path.name} [{kind}]: invalid output after retry: {second}") from second

    out = {"data": data, "model_used": result.model_used, "latency_ms": round(result.latency_ms), "cached": False}
    cache.put(path, kind, version, result.model_used, {k: out[k] for k in ("data", "model_used", "latency_ms")})
    return out


def extract_bill(path: str | Path, chain: list[str] | None = None) -> dict:
    """Level 1 JSON. Returns {"data", "model_used", "latency_ms", "cached"}."""
    return _extract(Path(path), chain, KIND, PROMPT_VERSION, BillL1)


def extract_extended(path: str | Path, chain: list[str] | None = None) -> dict:
    """Extended facts for Level 2, cached per bill. Same return shape as extract_bill."""
    return _extract(Path(path), chain, EXT_KIND, EXT_PROMPT_VERSION, ExtendedFacts)


def raise_late_amount(l1: dict, ext: dict | None) -> tuple[dict, str | None]:
    """Cross-check payable_after_due_date against the payable amounts printed in the extended facts.

    Only a non-null value is ever raised (to the highest printed late payable amount); a null stays null and a value is
    never lowered. Surcharge-only entries (no "payable" in the label) and "payable within due date" entries are ignored.
    Returns (l1 copy, note or None).
    """
    current = l1.get("payable_after_due_date")
    if current is None or isinstance(current, bool) or not isinstance(current, (int, float)):
        return l1, None
    amounts = [e["amount"] for e in (ext or {}).get("late_payment", [])
               if "payable" in e["label"].lower() and "within" not in e["label"].lower()
               and isinstance(e["amount"], (int, float))]
    best = max(amounts, default=None)
    if best is None or best <= current:
        return l1, None
    return {**l1, "payable_after_due_date": best}, f"late amount raised from {current:g} to {best:g}"


# ---- targeted re-read of fields behind a REAL reconcile mismatch ----
REREAD_VERSION = "reread_v1"
REREAD_FIELDS = {
    "units": ["previous_reading", "current_reading", "units_consumed"],
    "taxes_sum": ["taxes", "total_taxes"],
    "charges_sum": ["charges", "total_charges"],
    "bill_total": ["total_charges", "total_taxes", "current_bill"],
}


def _problem(check: dict) -> str:
    name, exp, got = check["check"], check["expected"], check["printed"]
    return {
        "units": f"current reading minus previous reading is {exp} but the printed units are {got}. Re-read only the meter readings (previous, current) and the units.",
        "taxes_sum": f"the tax lines add up to {exp} but the printed total taxes is {got}. Re-read the taxes section and total_taxes.",
        "charges_sum": f"the charge lines add up to {exp} but the printed total charges is {got}. Re-read the charges section and total_charges.",
        "bill_total": f"total_charges plus total_taxes is {exp} but the printed current bill is {got}. Re-read those three amounts (total_charges, total_taxes, current_bill).",
    }[name]


def real_failures(rec: dict) -> list[dict]:
    return [c for c in rec["checks"] if c["status"] == "mismatch" and c["check"] in REREAD_FIELDS]


SECOND_MODEL = "gemini-3.1-flash-lite"


def _status(rec: dict) -> dict[str, str]:
    return {c["check"]: c["status"] for c in rec["checks"]}


def _hash(items: list) -> str:
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()[:10]


_last_unusable = ""  # why the latest re-read answer was unusable, for the run summary


def _read(path: Path, kind: str, models: list[str], fields: list[str], checks: list[dict], l1: dict, plan_only: bool):
    """Cached or fresh re-read of `fields`. Returns (values or None, model or None, calls_needed).
    values is None when a call would be needed (plan_only) or the answer was unusable (reason in _last_unusable)."""
    global _last_unusable
    for m in models:
        hit = cache.get(path, kind, REREAD_VERSION, m)
        if hit:
            global _last_unusable
            if hit["fields"] is None:  # an earlier unusable answer: do not ask again
                _last_unusable = f"cached: {hit.get('unusable', 'unusable answer')}"
                return None, hit["model_used"], 0
            return hit["fields"], hit["model_used"], 0
    if plan_only:
        return None, None, 1
    prompt = (PROMPT_DIR / f"{REREAD_VERSION}.md").read_text(encoding="utf-8").format(
        problem=" ".join(_problem(c) for c in checks), fields=", ".join(fields),
        rules=(PROMPT_DIR / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8"))
    result = llm.generate(prompt, images=[path], json_mode=True, chain=models, purpose=f"re-read {path.name} {fields}")
    try:
        raw = json.loads(result.text)
        values = {k: raw[k] for k in fields if k in raw}
        if set(values) != set(fields):
            raise ValueError(f"missing keys {sorted(set(fields) - set(values))}")
        BillL1.model_validate({**l1, **values})
    except (ValueError, ValidationError, TypeError) as e:
        _last_unusable = f"{result.model_used}: {str(e).splitlines()[0][:120]}"
        cache.put(path, kind, REREAD_VERSION, result.model_used, {"fields": None, "model_used": result.model_used, "unusable": _last_unusable})
        return None, result.model_used, 0
    cache.put(path, kind, REREAD_VERSION, result.model_used, {"fields": values, "model_used": result.model_used})
    return values, result.model_used, 0


def _apply(current: dict, values: dict, checks: list[dict], model: str, meter, fpa) -> tuple[dict, list[str], list[dict]]:
    """Per failing check: accept that field group if its own check now passes and no check that passed before now fails.
    Returns (l1, notes, checks whose group came back unchanged)."""
    notes, unchanged = [], []
    for c in checks:
        group = [f for f in REREAD_FIELDS[c["check"]] if f in values]
        if all(values[f] == current[f] for f in group):
            if _status(reconcile(current, meter, fpa))[c["check"]] == "mismatch":  # not already fixed by another group
                unchanged.append(c)
            continue
        candidate = {**current, **{f: values[f] for f in group}}
        before, after = _status(reconcile(current, meter, fpa)), _status(reconcile(candidate, meter, fpa))
        ok = after[c["check"]] == "ok" and not any(after[k] == "mismatch" and before[k] != "mismatch" for k in after)
        verdict = f"{'accepted' if ok else 'rejected'}, {model}"
        notes += [f"re-read: {f} {json.dumps(current[f])[:60]} -> {json.dumps(values[f])[:60]} ({verdict})"
                  for f in group if values[f] != current[f]]
        if ok:
            current = candidate
    return current, notes, unchanged


def _reread(path: Path, l1: dict, ext: dict | None, chain: list[str], plan_only: bool) -> tuple[dict, list[str], int]:
    meter, fpa = (ext or {}).get("meter_rows"), fpa_amounts(ext)
    failed = real_failures(reconcile(l1, meter, fpa))
    if not failed:
        return l1, [], 0
    fields = list(dict.fromkeys(f for c in failed for f in REREAD_FIELDS[c["check"]]))
    key = [c["check"] for c in failed] + [c["printed"] for c in failed]  # same key as the cached output_v2 re-reads
    values, model, needed = _read(path, "reread-" + _hash(key), chain, fields, failed, l1, plan_only)
    if values is None:
        return l1, ([] if plan_only else [f"re-read: {', '.join(fields)} unusable or not available (rejected) {_last_unusable}"]), needed
    current, notes, unchanged = _apply(l1, values, failed, model, meter, fpa)
    if unchanged:  # the first reader repeated the original values: one read with the second allowed model
        f2 = list(dict.fromkeys(f for c in unchanged for f in REREAD_FIELDS[c["check"]]))
        v2, m2, need2 = _read(path, "reread2-" + _hash([c["check"] for c in unchanged] + [c["printed"] for c in unchanged]), [SECOND_MODEL], f2, unchanged, l1, plan_only)
        needed += need2
        if v2 is None:
            notes.append(f"re-read: {', '.join(f2)} second reader {SECOND_MODEL} " + ("not yet cached" if plan_only else "unusable (rejected)"))
        else:
            current, more, same = _apply(current, v2, unchanged, m2, meter, fpa)
            notes += more or [f"re-read: {', '.join(f2)} unchanged by {m2} (rejected)"]
    return current, notes or [f"re-read: {', '.join(fields)} unchanged ({model}) (rejected)"], needed


def reread_bill(path: str | Path, l1: dict, ext: dict | None, chain: list[str] | None = None) -> tuple[dict, list[str]]:
    """Targeted re-read for a REAL reconcile mismatch only (never for a passing bill). Values are never changed in code:
    the model's re-read replaces a field group only when that group's failing check passes and nothing that passed fails.
    Returns (l1, notes)."""
    out, notes, _ = _reread(Path(path), l1, ext, chain or llm.get_chain(), plan_only=False)
    return out, notes


def reread_calls_needed(path: str | Path, l1: dict, ext: dict | None, chain: list[str] | None = None) -> int:
    """Model calls the re-read still needs (0 when everything is cached or no mismatch exists). Cache only."""
    return _reread(Path(path), l1, ext, chain or llm.get_chain(), plan_only=True)[2]
