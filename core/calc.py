"""All arithmetic for Level 2, in pure Python. The LLM only phrases these results.

Every stat is {"value", "tag", "how"} with tag in printed | calculated | estimated.
Values that cannot be computed from the bill are left out and listed in stats["missing"].
"""
from __future__ import annotations

import re
import statistics

THRESHOLDS = (100, 200, 300, 500)
WHAT_IF_UNITS = (10, 25, 50, 100)


def _s(value, tag: str, how: str) -> dict:
    return {"value": value, "tag": tag, "how": how}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _r(x: float, nd: int = 2) -> float:
    return round(x, nd)


def _usage(ext: dict) -> list[dict]:
    pts = [p for p in ext.get("usage_history", []) if _num(p.get("units"))]
    return sorted(pts, key=lambda p: p["month"])


def average_usage(ext: dict) -> dict:
    pts = _usage(ext)
    if not pts:
        return {}
    units = [p["units"] for p in pts]
    hi, lo = max(pts, key=lambda p: p["units"]), min(pts, key=lambda p: p["units"])
    n = len(pts)
    return {
        "usage_months_shown": _s(n, "printed", "number of months in the printed usage history"),
        "average_monthly_units": _s(_r(sum(units) / n), "calculated", f"sum of {n} printed monthly units / {n}"),
        "highest_usage_month": _s({"month": hi["month"], "units": hi["units"]}, "calculated", "max of printed monthly units"),
        "lowest_usage_month": _s({"month": lo["month"], "units": lo["units"]}, "calculated", "min of printed monthly units"),
    }


_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_NOT_UNITS = re.compile(r"\s*(?:months?|years?|days?|%|percent)", re.I)


def question_thresholds(questions: list[dict] | None) -> list[float]:
    """Every number written in the question texts (e.g. "more than 150 units"), minus years and month/day counts."""
    found: list[float] = []
    for q in questions or []:
        text = q.get("question", "")
        for m in _NUMBER.finditer(text):
            n = float(m.group(0).rstrip(",").replace(",", ""))
            if n > 0 and not 1900 <= n <= 2100 and not _NOT_UNITS.match(text, m.end()) and n not in found:
                found.append(n)
    return found


def _label(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def months_above_below(ext: dict, thresholds) -> dict:
    """For each threshold n: the months with units > n, < n and == n, as counts and month lists."""
    pts = _usage(ext)
    if not pts:
        return {}
    out = {}
    for n in thresholds:
        above = [p["month"] for p in pts if p["units"] > n]
        below = [p["month"] for p in pts if p["units"] < n]
        equal = [p["month"] for p in pts if p["units"] == n]
        name = _label(n)
        out[f"months_above_{name}_units"] = _s({"count": len(above), "months": above, "exactly_equal_months": equal},
                                               "calculated", f"months in the printed usage history with units > {name}")
        out[f"months_below_{name}_units"] = _s({"count": len(below), "months": below, "exactly_equal_months": equal},
                                               "calculated", f"months in the printed usage history with units < {name}")
    return out


def _ym(month: str) -> int:
    y, m = month.split("-")
    return int(y) * 12 + int(m) - 1


def _last_12(ext: dict) -> list[dict]:
    return _usage(ext)[-12:]


def usage_context(l1: dict, ext: dict) -> dict:
    """Comparisons that the questions ask for: vs average, same month last year, last 3 months, highest in 12, season split."""
    pts = _usage(ext)
    if not pts:
        return {}
    out = {}
    cur = l1.get("units_consumed")
    avg = sum(p["units"] for p in pts) / len(pts)
    if _num(cur):
        d = cur - avg
        out["current_units_vs_average"] = _s({"current_units": cur, "average_units": _r(avg), "difference": _r(d),
                                             "percent": _r(d / avg * 100, 1) if avg else None}, "calculated",
                                            f"{cur} units this bill vs the average of the {len(pts)} printed months")
    month = l1.get("bill_month") or pts[-1]["month"]
    ly = next((p for p in pts if _ym(p["month"]) == _ym(month) - 12), None)
    if ly and _num(cur):
        out["same_month_last_year"] = _s({"month": ly["month"], "units": ly["units"], "current_units": cur,
                                         "change_units": _r(cur - ly["units"])}, "calculated",
                                        f"{cur} units now - {ly['units']} units in {ly['month']}")
    last3 = pts[-3:]
    if len(last3) == 3:
        u = [p["units"] for p in last3]
        direction = "up" if u[0] < u[1] < u[2] else "down" if u[0] > u[1] > u[2] else "mixed"
        out["last_3_months_trend"] = _s({"months": [p["month"] for p in last3], "units": u, "direction": direction},
                                       "calculated", "the three most recent printed months, compared in order")
    l12 = _last_12(ext)
    hi = max(l12, key=lambda p: p["units"])
    out["highest_usage_month_last_12"] = _s({"month": hi["month"], "units": hi["units"]}, "calculated",
                                           f"max units among the {len(l12)} most recent printed months")
    summer = [p for p in l12 if int(p["month"][5:]) in (6, 7, 8)]
    winter = [p for p in l12 if int(p["month"][5:]) in (12, 1, 2)]
    if summer and winter:
        sa, wa = sum(p["units"] for p in summer) / len(summer), sum(p["units"] for p in winter) / len(winter)
        out["summer_vs_winter_units"] = _s({
            "summer_months": [p["month"] for p in summer], "summer_average": _r(sa), "summer_total": sum(p["units"] for p in summer),
            "winter_months": [p["month"] for p in winter], "winter_average": _r(wa), "winter_total": sum(p["units"] for p in winter),
            "extra_units_per_month_in_summer": _r(sa - wa)}, "calculated",
            "summer = Jun-Aug, winter = Dec-Feb among the 12 most recent printed months; averages of the months shown")
    return out


def forecast_next_month(ext: dict) -> dict:
    """Estimated next-month units from the 12-month trend (least-squares line) and simple averages."""
    l12 = _last_12(ext)
    if len(l12) < 6:
        return {}
    xs, ys = list(range(len(l12))), [p["units"] for p in l12]
    slope, intercept = statistics.linear_regression(xs, ys)
    trend = max(0.0, slope * len(l12) + intercept)
    last3 = sum(ys[-3:]) / 3
    return {"forecast_next_month_units": _s({
        "trend_line_estimate": _r(trend), "average_of_last_3_months": _r(last3), "average_of_all_months_shown": _r(sum(ys) / len(ys)),
        "months_used": len(l12)}, "estimated",
        f"estimate: least-squares line through the {len(l12)} most recent printed months extended one month; "
        "also the simple averages; real usage depends on weather and habits")}


def billing_stats(l1: dict, ext: dict) -> dict:
    """Payment completeness, yearly spread and a same-units bill estimate from the printed billing history."""
    hist = [b for b in ext.get("billing_history", []) if _num(b.get("billed_amount"))]
    if not hist:
        return {}
    out = {}
    paid = [b["month"] for b in hist if _num(b.get("payment_amount")) and b["payment_amount"] > 0]
    unpaid = [b["month"] for b in hist if b["month"] not in paid]
    partial = [b["month"] for b in hist if b["month"] in paid and b["payment_amount"] < b["billed_amount"] - 1]
    out["payment_history_check"] = _s({"months_shown": len(hist), "months_with_payment": len(paid),
                                      "months_without_payment_shown": unpaid, "months_paid_less_than_billed": partial},
                                     "calculated", "a month counts as paid when a payment amount above 0 is printed for it")
    total = sum(b["billed_amount"] for b in hist)
    out["billed_history_total_and_monthly_average"] = _s({"months_shown": len(hist), "total": _r(total),
                                                         "monthly_average": _r(total / len(hist))}, "calculated",
                                                        f"sum of the {len(hist)} printed billed amounts / {len(hist)}")
    cur_month, u = l1.get("bill_month"), None
    prev = [b for b in hist if cur_month and b["month"] < cur_month]
    pts = {p["month"]: p["units"] for p in _usage(ext)}
    if prev:
        pb = max(prev, key=lambda b: b["month"])
        item = {"previous_month": pb["month"], "previous_bill_printed": pb["billed_amount"]}
        cb, cu = l1.get("current_bill"), l1.get("units_consumed")
        if pb["month"] in pts and _num(cb) and _num(cu) and cu:
            item["previous_month_units"] = pts[pb["month"]]
            item["estimate_at_current_cost_per_unit"] = _r(pts[pb["month"]] * cb / cu)
        out["bill_if_units_same_as_previous_month"] = _s(item, "estimated",
            "the previous month's printed bill, and an estimate = previous month's units x (current bill / current units); ignores slab and tax changes")
    return out


def usage_change(l1: dict, ext: dict) -> dict:
    """Units this month vs the previous month and vs the average (history months other than the current one)."""
    pts = _usage(ext)
    cur = l1.get("units_consumed")
    if not pts or not _num(cur):
        return {}
    out = {}
    cur_month = l1.get("bill_month")
    prior = [p for p in pts if p["month"] < cur_month] if cur_month else pts[:-1]
    if prior:
        prev = prior[-1]
        d = cur - prev["units"]
        pct = _r(d / prev["units"] * 100, 1) if prev["units"] else None
        out["units_change_vs_previous_month"] = _s(
            {"previous_month": prev["month"], "previous_units": prev["units"], "change_units": _r(d), "change_percent": pct},
            "calculated", f"{cur} units this bill - {prev['units']} units in {prev['month']}")
    return out


def tax_share(l1: dict, ext: dict) -> dict:
    out = {}
    tc, tt, cb = l1.get("total_charges"), l1.get("total_taxes"), l1.get("current_bill")
    if _num(tt) and _num(cb) and cb:
        out["tax_share_of_current_bill_percent"] = _s(_r(tt / cb * 100, 2), "calculated", f"total taxes {tt} / current bill {cb} x 100")
    if _num(tc) and _num(cb) and cb:
        out["charges_share_of_current_bill_percent"] = _s(_r(tc / cb * 100, 2), "calculated", f"total charges {tc} / current bill {cb} x 100")
    energy = [c["amount"] for c in l1.get("charges", []) if c["type"] == "energy"]
    if energy and _num(cb) and cb:
        e = sum(energy)
        out["energy_charges_total"] = _s(_r(e), "calculated", f"sum of {len(energy)} printed energy lines")
        out["energy_share_of_current_bill_percent"] = _s(_r(e / cb * 100, 2), "calculated", f"energy lines {_r(e)} / current bill {cb} x 100")
    for key in ("energy_share_percent", "tax_share_percent"):
        if _num(ext.get(key)):
            out[f"printed_{key}"] = _s(ext[key], "printed", "percentage printed on the bill")
    for t in l1.get("taxes", []):
        if _num(t["amount"]) and _num(cb) and cb:
            out[f"tax_{t['type']}_percent_of_bill"] = _s(_r(t["amount"] / cb * 100, 2), "calculated", f"{t['type']} {t['amount']} / current bill {cb} x 100")
    return out


def charge_change(l1: dict, ext: dict) -> dict:
    """Current bill vs the most recent earlier billed amount in the billing history."""
    cb, cur_month = l1.get("current_bill"), l1.get("bill_month")
    hist = [b for b in ext.get("billing_history", []) if _num(b.get("billed_amount")) and cur_month and b["month"] < cur_month]
    if not _num(cb) or not hist:
        return {}
    prev = max(hist, key=lambda b: b["month"])
    d = cb - prev["billed_amount"]
    pct = _r(d / prev["billed_amount"] * 100, 1) if prev["billed_amount"] else None
    return {"bill_change_vs_previous_bill": _s(
        {"previous_month": prev["month"], "previous_bill": prev["billed_amount"], "change_amount": _r(d), "change_percent": pct},
        "calculated", f"current bill {cb} - billed amount {prev['billed_amount']} in {prev['month']}")}


def cost_per_unit(l1: dict) -> dict:
    cb, u, tc = l1.get("current_bill"), l1.get("units_consumed"), l1.get("total_charges")
    out = {}
    if _num(cb) and _num(u) and u:
        out["all_in_cost_per_unit"] = _s(_r(cb / u, 2), "calculated", f"current bill {cb} / {u} units")
    if _num(tc) and _num(u) and u:
        out["charges_cost_per_unit"] = _s(_r(tc / u, 2), "calculated", f"total charges {tc} / {u} units")
    return out


def late_payment_cost(l1: dict, ext: dict) -> dict:
    w, a = l1.get("payable_within_due_date"), l1.get("payable_after_due_date")
    out = {}
    if _num(w) and _num(a):
        out["late_payment_extra_cost"] = _s(_r(a - w), "calculated", f"payable after due date {a} - payable within due date {w}")
        if w:
            out["late_payment_extra_percent"] = _s(_r((a - w) / w * 100, 1), "calculated", f"({a} - {w}) / {w} x 100")
    if ext.get("late_payment"):
        out["late_payment_amounts_printed"] = _s(ext["late_payment"], "printed", "late-payment amounts as printed on the bill")
    return out


def what_if_units(l1: dict, fewer=WHAT_IF_UNITS) -> dict:
    """Estimate: saving per unit = printed energy lines / units; taxes scaled by the bill's tax-to-charges ratio."""
    energy = [c["amount"] for c in l1.get("charges", []) if c["type"] == "energy"]
    u, tc, tt = l1.get("units_consumed"), l1.get("total_charges"), l1.get("total_taxes")
    if not energy or not _num(u) or not u or not _num(tc) or not tc:
        return {}
    per_unit = sum(energy) / u
    ratio = tt / tc if _num(tt) else 0.0
    rows = []
    for n in fewer:
        if n >= u:
            continue
        saving = n * per_unit * (1 + ratio)
        rows.append({"fewer_units": n, "estimated_saving": _r(saving)})
    if not rows:
        return {}
    how = (f"estimate: printed energy lines {_r(sum(energy))} / {u} units = {_r(per_unit, 4)} per unit, "
           f"x fewer units, x (1 + tax/charges ratio {_r(ratio, 4)}); ignores fixed charges and slab changes, so the real saving may differ")
    return {"what_if_fewer_units": _s(rows, "estimated", how)}


def compute_stats(l1: dict, ext: dict, questions: list[dict] | None = None) -> dict:
    """questions: [{"question_id", "question"}]; numbers written in them become extra month thresholds."""
    stats: dict = {}
    thresholds = list(dict.fromkeys([*THRESHOLDS, *question_thresholds(questions)]))
    for part in (average_usage(ext), months_above_below(ext, thresholds), usage_change(l1, ext), usage_context(l1, ext),
                 forecast_next_month(ext), billing_stats(l1, ext), tax_share(l1, ext),
                 charge_change(l1, ext), cost_per_unit(l1), late_payment_cost(l1, ext), what_if_units(l1)):
        stats.update(part)
    expected = ["average_monthly_units", "units_change_vs_previous_month", "bill_change_vs_previous_bill",
                "tax_share_of_current_bill_percent", "late_payment_extra_cost", "what_if_fewer_units"]
    return {"stats": stats, "missing": [k for k in expected if k not in stats]}
