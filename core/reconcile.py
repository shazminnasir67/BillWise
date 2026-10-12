"""Arithmetic cross-checks on extracted Level 1 values. Pure Python, no LLM.

It NEVER changes a value. Each check is ok, mismatch, warning or not_checkable (an input is null or missing).
A units-vs-readings difference is only a warning when the bill prints several meter rows (the printed units are then a total).
A payable of 0 against a negative current_bill + arrears is a warning (credit carried forward).
A mismatch can come from the bill's own rounding; it is reported as printed, never "corrected".
"""
from __future__ import annotations

TOLERANCE = 1.0


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _equal(name: str, fields: list[str], expected, printed, how: str) -> dict:
    diff = round(printed - expected, 2)
    return {"check": name, "fields": fields, "status": "ok" if abs(diff) <= TOLERANCE else "mismatch",
            "expected": round(expected, 2), "printed": printed, "difference": diff, "how": how}


def _r2(x: float) -> float:
    return round(x, 2)


def _skip(name: str, fields: list[str], why: str) -> dict:
    return {"check": name, "fields": fields, "status": "not_checkable", "why": why}


def fpa_amounts(ext: dict | None) -> list[float]:
    """Printed FPA amounts (e.g. a \"Total FPA\" row) from the extended facts."""
    return [c["amount"] for c in (ext or {}).get("charge_lines", []) if "fpa" in c["name"].lower() and _num(c.get("amount"))]


def run_checks(l1: dict, meter_rows: int | None = None, fpa: list[float] | None = None) -> list[dict]:
    out = []
    charges = [c["amount"] for c in l1.get("charges") or []]
    taxes = [t["amount"] for t in l1.get("taxes") or []]
    tc, tt, cb = l1.get("total_charges"), l1.get("total_taxes"), l1.get("current_bill")

    f = ["charges", "total_charges"]
    if charges and _num(tc):
        out.append(_equal("charges_sum", f, sum(charges), tc, f"sum of {len(charges)} charge lines vs total_charges"))
    else:
        out.append(_skip("charges_sum", f, "no charge lines or total_charges is null"))

    f = ["taxes", "total_taxes"]
    if taxes and _num(tt):
        out.append(_equal("taxes_sum", f, sum(taxes), tt, f"sum of {len(taxes)} tax lines vs total_taxes"))
    else:
        out.append(_skip("taxes_sum", f, "no tax lines (combined tax only) or total_taxes is null"))

    f = ["total_charges", "total_taxes", "current_bill"]
    if _num(tc) and _num(tt) and _num(cb):
        out.append(_equal("bill_total", f, tc + tt, cb, "total_charges + total_taxes vs current_bill"))
    else:
        out.append(_skip("bill_total", f, "an input is null"))

    f = ["previous_reading", "current_reading", "units_consumed"]
    pr, cr, u = l1.get("previous_reading"), l1.get("current_reading"), l1.get("units_consumed")
    if _num(pr) and _num(cr) and _num(u):
        c = _equal("units", f, cr - pr, u, "current_reading - previous_reading vs units_consumed")
        if c["status"] == "mismatch" and meter_rows is not None and meter_rows > 1:
            c["status"] = "warning"
            c["how"] += f" (bill prints {meter_rows} meter rows, so the printed units are a total)"
        out.append(c)
    else:
        out.append(_skip("units", f, "an input is null"))

    f = ["payable_within_due_date", "current_bill", "arrears"]
    w, cb, ar = l1.get("payable_within_due_date"), l1.get("current_bill"), l1.get("arrears")
    if _num(w) and _num(cb) and _num(ar):
        c = _equal("payable_balance", f, cb + ar, w, "current_bill + arrears vs payable_within_due_date")
        gap = w - (cb + ar)
        if c["status"] == "mismatch" and any(abs(gap - a) <= TOLERANCE for a in fpa or []):
            c["status"] = "ok"
            c["how"] += f" (the gap {_r2(gap)} equals a printed FPA amount that sits outside the current bill)"
        elif c["status"] == "mismatch" and w == 0 and cb + ar < 0:
            c["status"] = "warning"
            c["how"] += " (credit balance: the bill prints 0 payable and carries the credit forward)"
        out.append(c)
    else:
        out.append(_skip("payable_balance", f, "an input is null"))

    f = ["payable_within_due_date", "payable_after_due_date"]
    w, a = l1.get("payable_within_due_date"), l1.get("payable_after_due_date")
    if _num(w) and _num(a):
        ok = a >= w - TOLERANCE
        out.append({"check": "late_not_lower", "fields": f, "status": "ok" if ok else "mismatch",
                    "expected": f">= {w}", "printed": a, "how": "payable_after_due_date must not be below payable_within_due_date"})
    else:
        out.append(_skip("late_not_lower", f, "an input is null"))
    return out


_RANK = {"not_checkable": 0, "ok": 1, "warning": 2, "mismatch": 3}


def reconcile(l1: dict, meter_rows: int | None = None, fpa: list[float] | None = None) -> dict:
    """Returns {"checks", "field_status", "needs_attention", "reasons", "warnings"}.

    field_status per field is the worst status of the checks it takes part in: mismatch > warning > ok > not_checkable.
    meter_rows is the number of meter reading rows printed on the bill, when known; fpa are printed FPA amounts.
    """
    checks = run_checks(l1, meter_rows, fpa)
    status: dict[str, str] = {}
    for c in checks:
        for field in c["fields"]:
            if _RANK[c["status"]] > _RANK.get(status.get(field, "not_checkable"), 0):
                status[field] = c["status"]
            else:
                status.setdefault(field, c["status"])
    def text(c):
        return f"{c['check']}: expected {c['expected']} but printed {c['printed']}"
    reasons = [text(c) for c in checks if c["status"] == "mismatch"]
    warnings = [text(c) for c in checks if c["status"] == "warning"]
    return {"checks": checks, "field_status": status, "needs_attention": bool(reasons), "reasons": reasons,
            "warnings": warnings}
