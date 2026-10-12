"""End-to-end pipeline with a MOCKED model: no provider is ever contacted."""
import csv
import json
import re
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run  # noqa: E402
from core import calc, csv_io, llm  # noqa: E402

_GOLDEN_FILE = Path(__file__).parent / "golden" / "KESC_0008.json"
if not _GOLDEN_FILE.exists():  # dev reference data is not shipped in the submission ZIP
    pytest.skip("tests/golden not present", allow_module_level=True)
GOLDEN = json.loads(_GOLDEN_FILE.read_text())
EXT = {
    "usage_history": [{"month": f"2026-0{m}", "units": u} for m, u in [(1, 96), (2, 88), (3, 115)]]
    + [{"month": "2026-04", "units": 151}],
    "billing_history": [{"month": "2026-03", "billed_amount": 2577.31, "payment_date": "2026-03-16", "payment_amount": 2577}],
    "late_payment": [{"label": "5% LPS", "amount": 143.03, "valid_from": None, "valid_until": None}],
    "charge_lines": [{"name": "Fixed Charges", "units": 3, "rate": 300, "amount": 900}],
    "energy_share_percent": 83.39, "tax_share_percent": 16.61, "message_board": None, "meter_rows": 1,
}
QUESTIONS = [("Q01", "Why is my bill higher than last month?"), ("Q02", "What do I pay if I am late?")]


def fake_model(model, prompt, system, images, json_mode, max_tokens):
    if prompt.startswith("You extract structured data"):
        return "not json at all" if "BAD" in str(images) or fake_model.bad else json.dumps(GOLDEN)
    if prompt.startswith("You extract SUPPLEMENTARY"):
        return json.dumps(EXT)
    ids = re.findall(r'"question_id": "(\w+)"', prompt.split("<questions>\n")[-1])
    return json.dumps({"answers": [{"question_id": i, "answer": f"answer for {i}"} for i in ids]})


fake_model.bad = False


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("ALLOW_LLM_CALLS", "true")
    monkeypatch.setenv("MAX_LLM_CALLS", "40")
    monkeypatch.setenv("MODEL_CHAIN", "gemini-3.5-flash-lite")
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setitem(llm._CALLERS, "google", fake_model)
    monkeypatch.setattr(run, "REQUESTS_PER_MIN", 6000)
    fake_model.bad = False
    llm.reset_call_counter()
    bills, tpl = tmp_path / "bills", tmp_path / "tpl"
    bills.mkdir(); tpl.mkdir()
    for i in ("BILL_A", "BILL_B"):
        Image.new("RGB", (8, 8), "white" if i == "BILL_A" else "black").save(bills / f"{i}.png")
    with open(tpl / "level1_template.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["bill_id", "json"]); w.writerows([["BILL_A", ""], ["BILL_B", ""]])
    with open(tpl / "level2_template.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["bill_id", "question_id", "question", "answer"])
        for b in ("BILL_A", "BILL_B"):
            for qid, q in QUESTIONS:
                w.writerow([b, qid, q, ""])
    return bills, tpl, tmp_path / "out"


def read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_end_to_end_and_cache(setup):
    bills, tpl, out = setup
    assert run.main(["--bills", str(bills), "--templates", str(tpl), "--out", str(out)]) == 0
    l1, l2 = read(out / "level1.csv"), read(out / "level2.csv")
    assert [r["bill_id"] for r in l1] == ["BILL_A", "BILL_B"] and len(l2) == 4
    assert json.loads(l1[0]["json"])["total_taxes"] == 569.62 and "\n" not in l1[0]["json"]
    assert [r["question_id"] for r in l2] == ["Q01", "Q02", "Q01", "Q02"]
    assert all(r["answer"] for r in l2) and l2[0]["question"] == QUESTIONS[0][1]
    assert llm.calls_made() == 6  # 3 calls per bill
    llm.reset_call_counter()
    run.main(["--bills", str(bills), "--templates", str(tpl), "--out", str(out)])
    assert llm.calls_made() == 0  # fully cached


def test_failed_bill_gets_null_safe_rows(setup, monkeypatch):
    bills, tpl, out = setup
    real = run.extract_bill

    def bill_b_fails(path, *a, **k):
        if path.stem == "BILL_B":
            raise RuntimeError("boom")
        return real(path, *a, **k)

    monkeypatch.setattr(run, "extract_bill", bill_b_fails)
    assert run.main(["--bills", str(bills), "--templates", str(tpl), "--out", str(out)]) == 1
    l1, l2 = read(out / "level1.csv"), read(out / "level2.csv")
    b = json.loads(l1[1]["json"])
    assert b["provider"] is None and b["charges"] == [] and set(b) == set(csv_io.L1_FIELDS)
    assert all(r["answer"] for r in l2) and l2[2]["answer"] == csv_io.FALLBACK_ANSWER


def test_blocked_run_still_writes_valid_csvs(setup, monkeypatch):
    bills, tpl, out = setup
    monkeypatch.setenv("ALLOW_LLM_CALLS", "false")
    assert run.main(["--bills", str(bills), "--templates", str(tpl), "--out", str(out)]) == 1
    assert len(read(out / "level2.csv")) == 4 and all(r["answer"] for r in read(out / "level2.csv"))


def test_calc_stats():
    s = calc.compute_stats(GOLDEN, EXT)["stats"]
    assert s["average_monthly_units"]["value"] == 112.5
    assert s["units_change_vs_previous_month"]["value"]["change_units"] == 36
    assert s["bill_change_vs_previous_bill"]["value"]["change_amount"] == 852.93
    assert s["late_payment_extra_cost"]["value"] == 286
    assert s["tax_share_of_current_bill_percent"]["value"] == 16.61
    assert s["what_if_fewer_units"]["tag"] == "estimated"
    assert s["months_above_100_units"]["value"]["months"] == ["2026-03", "2026-04"]
    assert calc.compute_stats(csv_io.null_l1(), {})["stats"] == {}


def test_validator_accepts_good_and_rejects_bad_output(setup):
    from scripts import validate_submission as v
    bills, tpl, out = setup
    run.main(["--bills", str(bills), "--templates", str(tpl), "--out", str(out)])
    v.failures.clear()
    v.check_csvs(tpl, out, 2, 4)
    assert v.failures == []

    rows = read(out / "level1.csv")
    rows[0]["json"] = rows[0]["json"].replace('"KE"', '"XX"').replace("2860.62", '"2860.62"')
    with open(out / "level1.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["bill_id", "json"]); w.writeheader(); w.writerows(rows)
    v.failures.clear()
    v.check_csvs(tpl, out, 2, 4)
    assert any("level1 JSON valid" in m for m in v.failures)
    v.failures.clear()


def test_reconcile_golden_is_all_ok_and_never_mutates():
    import copy
    from core.reconcile import reconcile
    before = copy.deepcopy(GOLDEN)
    r = reconcile(GOLDEN)
    assert GOLDEN == before
    assert not r["needs_attention"] and all(c["status"] == "ok" for c in r["checks"])
    assert r["field_status"]["total_taxes"] == "ok"


def test_reconcile_flags_mismatch_and_skips_nulls():
    from core.reconcile import reconcile
    bad = {**GOLDEN, "total_charges": 2870.0, "payable_after_due_date": 3000}
    r = reconcile(bad)
    names = {c["check"]: c["status"] for c in r["checks"]}
    assert names["charges_sum"] == "mismatch" and names["bill_total"] == "mismatch"
    assert names["late_not_lower"] == "mismatch" and names["units"] == "ok"
    assert r["field_status"]["total_charges"] == "mismatch" and r["needs_attention"] and len(r["reasons"]) == 3
    combined = {**GOLDEN, "taxes": [], "payable_within_due_date": None, "previous_reading": None}
    n = {c["check"]: c["status"] for c in reconcile(combined)["checks"]}
    assert n["taxes_sum"] == n["late_not_lower"] == n["units"] == "not_checkable" and n["bill_total"] == "ok"


def test_reconcile_tolerance_is_one_rupee():
    from core.reconcile import reconcile
    iesco_like = {**GOLDEN, "taxes": [], "total_charges": 2770, "total_taxes": 498, "current_bill": 3267}
    assert {c["check"]: c["status"] for c in reconcile(iesco_like)["checks"]}["bill_total"] == "ok"
    assert {c["check"]: c["status"] for c in reconcile({**iesco_like, "current_bill": 3270})["checks"]}["bill_total"] == "mismatch"


def test_run_summary_lists_reconcile_flag_but_exit_zero(setup, monkeypatch, capsys):
    bills, tpl, out = setup
    bad = json.dumps({**GOLDEN, "total_charges": 3000})
    real = fake_model

    def model(m, prompt, system, images, json_mode, max_tokens):
        return bad if prompt.startswith("You extract structured data") else real(m, prompt, system, images, json_mode, max_tokens)

    monkeypatch.setitem(llm._CALLERS, "google", model)
    assert run.main(["--bills", str(bills), "--templates", str(tpl), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "NEEDS ATTENTION BILL_A" in text and "reconcile charges_sum" in text
    assert json.loads(read(out / "level1.csv")[0]["json"])["total_charges"] == 3000  # printed value kept


def test_reconcile_payable_balance_and_multi_row_warning():
    from core.reconcile import reconcile
    names = lambda r: {c["check"]: c["status"] for c in r["checks"]}  # noqa: E731
    cr = {**GOLDEN, "taxes": [], "total_charges": 17367.77, "charges": [], "total_taxes": 3358.23,
          "current_bill": 20726, "arrears": -75496, "payable_within_due_date": -54770, "payable_after_due_date": None}
    assert names(reconcile(cr))["payable_balance"] == "ok"
    wrong_sign = reconcile({**cr, "payable_within_due_date": 54770})
    assert names(wrong_sign)["payable_balance"] == "mismatch" and wrong_sign["needs_attention"]
    assert names(reconcile({**GOLDEN, "arrears": None}))["payable_balance"] == "not_checkable"
    assert names(reconcile(GOLDEN))["payable_balance"] == "ok"  # 3430.24 - 0.53 = 3429.71 vs 3430

    multi = {**GOLDEN, "units_consumed": 301}  # readings differ by 151
    assert names(reconcile(multi))["units"] == "mismatch"
    assert names(reconcile(multi, meter_rows=1))["units"] == "mismatch"
    r = reconcile(multi, meter_rows=4)
    assert names(r)["units"] == "warning" and not r["needs_attention"] and len(r["warnings"]) == 1
    assert r["field_status"]["units_consumed"] == "warning"


# ---- pre-flight: the real template format, KESC_0002-style ids, call plan, resume after a crash ----

IDS = ["KESC_0002", "LESCO_0001", "IESCO_0003", "KESC_0010"]


@pytest.fixture
def preflight(setup, monkeypatch):
    bills, tpl, out = setup
    for f in list(bills.iterdir()) + list(tpl.iterdir()):
        f.unlink()
    for i, b in enumerate(IDS):
        Image.new("RGB", (8, 8), (i * 40, 10, 10)).save(bills / (f"{b}.JPG" if b.startswith("IESCO") else f"{b}.png"))
    with open(tpl / "Level 1 Template.csv", "w", newline="", encoding="utf-8") as f:  # spaces and capitals in the name
        w = csv.writer(f); w.writerow(["bill_id", "json"]); w.writerows([[b, ""] for b in IDS])
    with open(tpl / "level2-template.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["bill_id", "question_id", "question", "answer"])
        for b in IDS:
            for q in range(1, 13):
                w.writerow([b, f"Q{q:02d}", f"Question {q}, with a comma?", ""])
    return bills, tpl, out


def test_preflight_real_format_prints_plan_first(preflight, capsys):
    bills, tpl, out = preflight
    assert run.main(["--bills", str(bills), "--templates", str(tpl), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert text.index("Plan: 4 bills, 12 model call(s) needed") < text.index("level1.csv: 4 rows")
    l1, l2 = read(out / "level1.csv"), read(out / "level2.csv")
    assert [r["bill_id"] for r in l1] == IDS and len(l2) == 48 and list(l1[0]) == ["bill_id", "json"]
    assert all(r["answer"] for r in l2) and l2[0]["question"] == "Question 1, with a comma?"
    assert llm.calls_made() == 12


def test_preflight_plan_warns_when_blocked_or_over_cap(preflight, monkeypatch, capsys):
    bills, tpl, out = preflight
    monkeypatch.setenv("ALLOW_LLM_CALLS", "false")
    monkeypatch.setenv("MAX_LLM_CALLS", "5")
    run.main(["--bills", str(bills), "--templates", str(tpl), "--out", str(out)])
    text = capsys.readouterr().out
    assert "ALLOW_LLM_CALLS is not true" in text and "WARNING: 12 calls needed but the cap is 5" in text
    assert llm.calls_made() == 0


def test_preflight_resumes_from_cache_after_crash(preflight, monkeypatch, capsys):
    bills, tpl, out = preflight
    seen = []

    def crashing(model, prompt, system, images, json_mode, max_tokens):
        seen.append(1)
        if len(seen) == 8:  # dies during the third bill
            raise KeyboardInterrupt
        return fake_model(model, prompt, system, images, json_mode, max_tokens)

    monkeypatch.setitem(llm._CALLERS, "google", crashing)
    with pytest.raises(KeyboardInterrupt):
        run.main(["--bills", str(bills), "--templates", str(tpl), "--out", str(out)])
    done = llm.calls_made()
    assert done == 8
    llm.reset_call_counter()
    monkeypatch.setitem(llm._CALLERS, "google", fake_model)
    capsys.readouterr()
    assert run.main(["--bills", str(bills), "--templates", str(tpl), "--out", str(out)]) == 0
    assert "Plan: 4 bills, 5 model call(s) needed" in capsys.readouterr().out  # 12 total - 7 completed
    assert llm.calls_made() == 5


def test_reconcile_credit_balance_with_zero_payable_is_warning():
    from core.reconcile import reconcile
    net_metering = {**GOLDEN, "current_bill": 45570.25, "arrears": -91877.02, "payable_within_due_date": 0,
                    "payable_after_due_date": 0, "total_charges": 38098.01, "total_taxes": 7472.24,
                    "charges": [], "taxes": []}
    r = reconcile(net_metering)
    status = {c["check"]: c["status"] for c in r["checks"]}
    assert status["payable_balance"] == "warning" and not r["needs_attention"] and len(r["warnings"]) == 1
    # a nonzero wrong payable, or zero payable against a positive balance, is still a mismatch
    assert reconcile({**net_metering, "payable_within_due_date": 500})["needs_attention"]
    assert reconcile({**net_metering, "arrears": 10, "payable_within_due_date": 0})["needs_attention"]


# ---- number-in-question thresholds, answer-quality retry, extended facts for the multi-meter note ----

THREE_QUESTIONS = [
    {"question_id": "Q01", "question": "Which months did I use more than 150 units?"},
    {"question_id": "Q02", "question": "Did I ever go below 100 units, or above 250?"},
    {"question_id": "Q03", "question": "What would I save with 50 fewer units over the last 12 months of 2026?"},
]


def test_calc_thresholds_from_question_numbers():
    from core import calc
    assert calc.question_thresholds(THREE_QUESTIONS) == [150.0, 100.0, 250.0, 50.0]  # not 12 (months) or 2026 (year)
    s = calc.compute_stats(GOLDEN, EXT, THREE_QUESTIONS)["stats"]
    assert s["months_above_150_units"]["value"]["months"] == ["2026-04"]
    assert s["months_above_150_units"]["value"]["count"] == 1
    assert s["months_below_100_units"]["value"]["months"] == ["2026-01", "2026-02"]
    assert s["months_above_250_units"]["value"]["count"] == 0
    assert s["months_above_50_units"]["value"]["count"] == 4 and s["months_below_50_units"]["value"]["count"] == 0
    assert "months_above_100_units" in s and "months_above_500_units" in s  # default thresholds kept
    assert "months_above_150_units" not in calc.compute_stats(GOLDEN, EXT)["stats"]


def _qa_env(setup, monkeypatch, answers_by_call):
    bills, _tpl, _out = setup
    seen = []

    def model(m, prompt, system, images, json_mode, max_tokens):
        seen.append(prompt)
        qs = re.findall(r'"question_id": "(\w+)"', prompt.split("<questions>\n")[-1].split("</questions>")[0])
        text = answers_by_call[min(len(seen), len(answers_by_call)) - 1]
        return json.dumps({"answers": [{"question_id": q, "answer": text} for q in qs]})

    monkeypatch.setitem(llm._CALLERS, "google", model)
    return bills / "BILL_A.png", seen


def _ask(img):
    from core import qa
    return qa.answer_questions(img, GOLDEN, EXT, {"stats": {}, "missing": []}, THREE_QUESTIONS)


def test_qa_retries_once_on_thinking_aloud_then_accepts_clean(setup, monkeypatch):
    img, seen = _qa_env(setup, monkeypatch, ["Wait, let me check. It is 3,430 (printed).", "Your bill is Rs. 3,430.24 (printed)."])
    out = _ask(img)
    assert len(seen) == 2 and llm.calls_made() == 2
    assert "thinking-out-loud" in seen[1] and "Q01" in seen[1]
    assert set(out["answers"].values()) == {"Your bill is Rs. 3,430.24 (printed)."}


def test_qa_trims_in_code_when_retry_still_bad(setup, monkeypatch):
    bad = "Actually, let's look closely. The bill is Rs. 3,430.24 (printed). Wait, is it? Due 21-Apr-2026 (printed)."
    img, seen = _qa_env(setup, monkeypatch, [bad, bad])
    out = _ask(img)
    assert llm.calls_made() == 2
    assert set(out["answers"].values()) == {"The bill is Rs. 3,430.24 (printed). Due 21-Apr-2026 (printed)."}
    from core import qa
    assert qa.trim_answer("Wait, let me think.") == qa.NOT_SHOWN


def test_qa_long_answer_is_retried_then_cut_to_limit(setup, monkeypatch):
    from core import qa
    long = " ".join(f"Sentence number {i} is fine." for i in range(60))
    img, seen = _qa_env(setup, monkeypatch, [long, long])
    out = _ask(img)
    assert len(seen) == 2 and all(len(a) <= qa.MAX_ANSWER_CHARS and a.endswith("fine.") for a in out["answers"].values())
    assert out["answers"]["Q01"].endswith("Sentence number 59 is fine.")  # the LAST clean sentences are kept


def test_qa_retry_counts_against_cap(setup, monkeypatch):
    img, seen = _qa_env(setup, monkeypatch, ["Wait, let me check.", "Clean answer."])
    monkeypatch.setenv("MAX_LLM_CALLS", "1")
    with pytest.raises(llm.LLMBlocked, match="MAX_LLM_CALLS"):
        _ask(img)
    assert len(seen) == 1


def test_run_extended_pass_runs_for_every_bill_and_shows_multi_meter_note(setup, monkeypatch, capsys):
    bills, tpl, out = setup
    calls = {"ext": 0}
    multi = {**GOLDEN, "units_consumed": 901}  # readings differ by 151 but the bill prints a total
    multi_ext = {**EXT, "meter_rows": 4}

    def model(m, prompt, system, images, json_mode, max_tokens):
        if prompt.startswith("You extract structured data"):
            return json.dumps(multi)
        if prompt.startswith("You extract SUPPLEMENTARY"):
            calls["ext"] += 1
            return json.dumps(multi_ext)
        return fake_model(m, prompt, system, images, json_mode, max_tokens)

    monkeypatch.setitem(llm._CALLERS, "google", model)
    assert run.main(["--bills", str(bills), "--templates", str(tpl), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert calls["ext"] == 2  # one per bill
    assert "note BILL_A: units: expected 151 but printed 901" in text and "NEEDS ATTENTION" not in text


# ---- late-amount cross-check ----

def _late(*pairs):
    return {"late_payment": [{"label": l, "amount": a, "valid_from": None, "valid_until": None} for l, a in pairs]}


def test_late_amount_lower_value_is_raised():
    from core.extract import raise_late_amount
    l1 = {**GOLDEN, "payable_after_due_date": 3574}
    out, note = raise_late_amount(l1, _late(("Amount Payable from 22-Apr - 24-Apr with 5% LPS", 3574),
                                            ("Amount Payable after 24-Apr-26 with 10% LPS", 3716),
                                            ("Late Payment Surcharge (10%)", 286.06)))
    assert out["payable_after_due_date"] == 3716 and note == "late amount raised from 3574 to 3716"
    assert l1["payable_after_due_date"] == 3574  # the input is not mutated


def test_late_amount_null_equal_empty_and_zero_are_unchanged():
    from core.extract import raise_late_amount
    payable = _late(("Amount Payable after Due Date", 3716))
    assert raise_late_amount({**GOLDEN, "payable_after_due_date": None}, payable) == ({**GOLDEN, "payable_after_due_date": None}, None)
    same = {**GOLDEN, "payable_after_due_date": 3716}
    assert raise_late_amount(same, payable) == (same, None)
    assert raise_late_amount(same, {"late_payment": []}) == (same, None)
    assert raise_late_amount(same, None) == (same, None)
    zero = {**GOLDEN, "payable_within_due_date": 0, "payable_after_due_date": 0}
    assert raise_late_amount(zero, _late(("Amount Payable after Due Date", 0), ("Amount Payable 14-Jul-26", 0))) == (zero, None)


def test_late_amount_never_lowers_and_ignores_surcharge_only_entries():
    from core.extract import raise_late_amount
    high = {**GOLDEN, "payable_after_due_date": 3716}
    assert raise_late_amount(high, _late(("Amount Payable from 22-Apr", 3574)))[1] is None
    zero = {**GOLDEN, "payable_after_due_date": 0}
    assert raise_late_amount(zero, _late(("Late Payment Surcharge (5%)", 143.03), ("Surcharge", 286.06)))[1] is None


def test_run_applies_cross_check_to_csv_and_prints_it(setup, monkeypatch, capsys):
    bills, tpl, out = setup
    low = json.dumps({**GOLDEN, "payable_after_due_date": 3574})
    ext = {**EXT, "late_payment": [{"label": "Amount Payable after 24-Apr-26 with 10% LPS", "amount": 3716,
                                    "valid_from": None, "valid_until": None}]}

    def model(m, prompt, system, images, json_mode, max_tokens):
        if prompt.startswith("You extract structured data"):
            return low
        if prompt.startswith("You extract SUPPLEMENTARY"):
            return json.dumps(ext)
        return fake_model(m, prompt, system, images, json_mode, max_tokens)

    monkeypatch.setitem(llm._CALLERS, "google", model)
    assert run.main(["--bills", str(bills), "--templates", str(tpl), "--out", str(out)]) == 0
    assert "note BILL_A: late amount raised from 3574 to 3716" in capsys.readouterr().out
    assert json.loads(read(out / "level1.csv")[0]["json"])["payable_after_due_date"] == 3716


def test_late_amount_ignores_payable_within_entries():
    from core.extract import raise_late_amount
    zero = {**GOLDEN, "payable_after_due_date": 0}
    assert raise_late_amount(zero, _late(("Payable Within Due Date", 54770), ("PAYABLE AFTER DUE DATE", 0)))[1] is None


# ---- stats for the real question types ----
USAGE13 = [141, 138, 197, 239, 203, 176, 173, 187, 124, 96, 88, 115, 151]
EXT13 = {**EXT, "usage_history": [{"month": f"{2025 + (3 + i) // 12}-{(3 + i) % 12 + 1:02d}", "units": u} for i, u in enumerate(USAGE13)],
         "billing_history": [{"month": "2026-03", "billed_amount": 2577.31, "payment_date": "2026-03-16", "payment_amount": 2577},
                             {"month": "2026-02", "billed_amount": 2782.51, "payment_date": None, "payment_amount": None},
                             {"month": "2026-01", "billed_amount": 2780.55, "payment_date": "2026-01-15", "payment_amount": 2000}]}


def test_stats_for_real_question_types():
    import statistics
    from core import calc
    assert EXT13["usage_history"][0]["month"] == "2025-04" and EXT13["usage_history"][-1]["month"] == "2026-04"
    s = calc.compute_stats(GOLDEN, EXT13)["stats"]
    assert s["same_month_last_year"]["value"] == {"month": "2025-04", "units": 141, "current_units": 151, "change_units": 10}
    assert s["last_3_months_trend"]["value"]["direction"] == "up" and s["last_3_months_trend"]["value"]["units"] == [88, 115, 151]
    sw = s["summer_vs_winter_units"]["value"]
    assert sw["summer_months"] == ["2025-06", "2025-07", "2025-08"] and sw["summer_average"] == 213.0
    assert sw["winter_months"] == ["2025-12", "2026-01", "2026-02"] and sw["winter_average"] == 102.67
    assert sw["extra_units_per_month_in_summer"] == 110.33
    assert s["highest_usage_month_last_12"]["value"] == {"month": "2025-07", "units": 239}
    l12 = USAGE13[-12:]
    slope, icpt = statistics.linear_regression(range(12), l12)
    f = s["forecast_next_month_units"]
    assert f["tag"] == "estimated" and f["value"]["trend_line_estimate"] == round(slope * 12 + icpt, 2)
    assert f["value"]["average_of_last_3_months"] == round(sum(l12[-3:]) / 3, 2)
    pay = s["payment_history_check"]["value"]
    assert pay["months_without_payment_shown"] == ["2026-02"] and pay["months_paid_less_than_billed"] == ["2026-01"]
    assert s["billed_history_total_and_monthly_average"]["value"]["monthly_average"] == round((2577.31 + 2782.51 + 2780.55) / 3, 2)
    same = s["bill_if_units_same_as_previous_month"]
    assert same["tag"] == "estimated" and same["value"]["previous_bill_printed"] == 2577.31
    assert same["value"]["estimate_at_current_cost_per_unit"] == round(115 * 3430.24 / 151, 2)
    assert s["current_units_vs_average"]["value"]["current_units"] == 151


def test_stats_are_skipped_when_history_is_missing():
    from core import calc
    s = calc.compute_stats(GOLDEN, {**EXT13, "usage_history": [], "billing_history": []})["stats"]
    for name in ("same_month_last_year", "summer_vs_winter_units", "forecast_next_month_units", "payment_history_check"):
        assert name not in s


def test_qa_cache_key_changes_with_stats(setup):
    from core import qa
    assert qa.kind_for(THREE_QUESTIONS, {"a": 1}) != qa.kind_for(THREE_QUESTIONS, {"a": 2})
    assert qa.kind_for(THREE_QUESTIONS, {"a": 1}) == qa.kind_for(THREE_QUESTIONS, {"a": 1})


# ---- FPA-aware payable balance and the targeted re-read ----

def test_fpa_gap_is_not_flagged_but_other_gaps_are():
    from core.reconcile import fpa_amounts, reconcile
    lesco = {**GOLDEN, "total_charges": 5200, "taxes": [], "charges": [], "total_taxes": 800, "current_bill": 6000 - 200,
             "arrears": 0, "payable_within_due_date": 6100, "payable_after_due_date": 6500}
    names = lambda r: {c["check"]: c["status"] for c in r["checks"]}  # noqa: E731
    assert names(reconcile(lesco))["payable_balance"] == "mismatch"
    ext = {**EXT, "charge_lines": [{"name": "Total FPA", "units": None, "rate": None, "amount": 300}]}
    assert fpa_amounts(ext) == [300]
    assert names(reconcile(lesco, None, fpa_amounts(ext)))["payable_balance"] == "ok"
    assert names(reconcile({**lesco, "payable_within_due_date": 8900}, None, [300]))["payable_balance"] == "mismatch"


BAD_PREV = {**GOLDEN, "previous_reading": 8419}  # units 151 but readings differ by 551


def _reread_model(values, seen):
    def model(m, prompt, system, images, json_mode, max_tokens):
        assert prompt.startswith("You re-read SPECIFIC fields")
        seen.append(prompt)
        return json.dumps(values)
    return model


def test_reread_accepted_when_failing_check_passes(setup, monkeypatch):
    from core.extract import reread_bill
    bills, _t, _o = setup
    seen = []
    monkeypatch.setitem(llm._CALLERS, "google", _reread_model(
        {"previous_reading": 8819, "current_reading": 8970, "units_consumed": 151}, seen))
    out, notes = reread_bill(bills / "BILL_A.png", BAD_PREV, EXT)
    assert out["previous_reading"] == 8819 and llm.calls_made() == 1
    assert notes == ["re-read: previous_reading 8419 -> 8819 (accepted, gemini-3.5-flash-lite)"]
    assert "current reading minus previous reading is 551" in seen[0] and "previous_reading, current_reading, units_consumed" in seen[0]
    again, _ = reread_bill(bills / "BILL_A.png", BAD_PREV, EXT)  # cached: no second call
    assert again["previous_reading"] == 8819 and llm.calls_made() == 1


def test_reread_rejected_when_check_still_fails_or_another_breaks(setup, monkeypatch):
    from core.extract import reread_bill
    bills, _t, _o = setup
    monkeypatch.setitem(llm._CALLERS, "google", _reread_model(
        {"previous_reading": 8000, "current_reading": 8970, "units_consumed": 151}, []))
    out, notes = reread_bill(bills / "BILL_A.png", BAD_PREV, EXT)
    assert out == BAD_PREV and "rejected" in notes[0] and llm.calls_made() == 1  # changed values: no second reader
    # taxes check fixed by editing total_taxes, but that breaks total_charges + total_taxes = current_bill
    bad_tax = {**GOLDEN, "total_taxes": 600}
    monkeypatch.setitem(llm._CALLERS, "google", _reread_model({"taxes": GOLDEN["taxes"], "total_taxes": 569.62, "total_charges": 2860.62, "current_bill": 3430.24}, []))
    out2, notes2 = reread_bill(bills / "BILL_A.png", bad_tax, EXT)
    assert out2["total_taxes"] == 569.62 and notes2 == ["re-read: total_taxes 600 -> 569.62 (accepted, gemini-3.5-flash-lite)"]
    worse = {**GOLDEN, "total_taxes": 570, "taxes": [{"type": "gst", "amount": 100}]}  # taxes_sum fails; bill_total passes
    monkeypatch.setitem(llm._CALLERS, "google", _reread_model({"taxes": [{"type": "gst", "amount": 100}], "total_taxes": 100}, []))
    out3, notes3 = reread_bill(bills / "BILL_B.png", worse, EXT)
    assert out3 == worse and "rejected" in notes3[0]  # fixes taxes_sum but breaks the passing bill_total


def test_passing_bill_is_never_reread(setup, monkeypatch):
    from core.extract import reread_bill, reread_calls_needed
    bills, _t, _o = setup
    monkeypatch.setitem(llm._CALLERS, "google", lambda *a: pytest.fail("a passing bill must not be re-called"))
    assert reread_bill(bills / "BILL_A.png", GOLDEN, EXT) == (GOLDEN, [])
    assert reread_calls_needed(bills / "BILL_A.png", GOLDEN, EXT) == 0 and llm.calls_made() == 0


def test_reread_respects_the_call_cap(setup, monkeypatch):
    from core.extract import reread_bill
    bills, _t, _o = setup
    monkeypatch.setenv("MAX_LLM_CALLS", "0")
    with pytest.raises(llm.LLMBlocked):
        reread_bill(bills / "BILL_A.png", BAD_PREV, EXT)


def test_run_rereads_only_the_failing_bill_and_reports_it(setup, monkeypatch, capsys):
    bills, tpl, out = setup
    counts = {"reread": 0, "l1": 0}

    def model(m, prompt, system, images, json_mode, max_tokens):
        if prompt.startswith("You extract structured data"):
            counts["l1"] += 1
            return json.dumps(BAD_PREV if counts["l1"] == 1 else GOLDEN)  # only the first bill is wrong
        if prompt.startswith("You re-read SPECIFIC fields"):
            counts["reread"] += 1
            return json.dumps({"previous_reading": 8819, "current_reading": 8970, "units_consumed": 151})
        return fake_model(m, prompt, system, images, json_mode, max_tokens)

    monkeypatch.setitem(llm._CALLERS, "google", model)
    assert run.main(["--bills", str(bills), "--templates", str(tpl), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert counts["reread"] == 1 and llm.calls_made() == 7  # 6 normal calls + 1 re-read
    assert "re-read: previous_reading 8419 -> 8819 (accepted, gemini-3.5-flash-lite)" in text and "NEEDS ATTENTION" not in text
    assert json.loads(read(out / "level1.csv")[0]["json"])["previous_reading"] == 8819


# ---- per-check acceptance and the second reader ----
LESCO1 = {**GOLDEN, "previous_reading": 5419, "current_reading": 6119, "units_consumed": 200, "charges": [], "taxes": [],
          "total_charges": 5200, "total_taxes": 800, "current_bill": 5800, "arrears": 0,
          "payable_within_due_date": 6100, "payable_after_due_date": 6500}


def _by_model(answers, seen):
    """answers: model id -> JSON dict returned for that model."""
    def model(m, prompt, system, images, json_mode, max_tokens):
        seen.append(m)
        return json.dumps({k: v for k, v in answers[m].items() if f'"{k}"' in prompt or k in prompt.split("ONLY these keys:")[1].split(".")[0]})
    return model


def test_lesco_0001_units_fix_accepted_although_bill_total_still_fails(setup, monkeypatch):
    from core.extract import reread_bill
    from core.reconcile import reconcile
    bills, _t, _o = setup
    seen = []
    same = {"total_charges": 5200, "total_taxes": 800, "current_bill": 5800}
    first = {"previous_reading": 5919, "current_reading": 6119, "units_consumed": 200, **same}
    monkeypatch.setitem(llm._CALLERS, "google", _by_model({"gemini-3.5-flash-lite": first, "gemini-3.1-flash-lite": same}, seen))
    names = {c["check"]: c["status"] for c in reconcile(LESCO1)["checks"]}
    assert names["units"] == "mismatch" and names["bill_total"] == "mismatch"
    out, notes = reread_bill(bills / "BILL_A.png", LESCO1, EXT)
    assert out["previous_reading"] == 5919
    assert "re-read: previous_reading 5419 -> 5919 (accepted, gemini-3.5-flash-lite)" in notes
    assert {c["check"]: c["status"] for c in reconcile(out)["checks"]}["bill_total"] == "mismatch"  # untouched, still reported
    assert seen == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]  # bill_total came back unchanged -> second reader
    assert any("unchanged by gemini-3.1-flash-lite (rejected)" in n for n in notes)


def test_unchanged_first_read_triggers_second_model_whose_value_is_accepted(setup, monkeypatch):
    from core.extract import reread_bill
    bills, _t, _o = setup
    seen = []
    wrong = {"previous_reading": 5419, "current_reading": 6119, "units_consumed": 200}
    right = {"previous_reading": 5919, "current_reading": 6119, "units_consumed": 200}
    bill = {k: v for k, v in LESCO1.items()}
    bill.update(total_charges=5200 - 200, current_bill=5800)  # bill_total passes (5000 + 800 = 5800)
    monkeypatch.setitem(llm._CALLERS, "google", _by_model({"gemini-3.5-flash-lite": wrong, "gemini-3.1-flash-lite": right}, seen))
    out, notes = reread_bill(bills / "BILL_A.png", bill, EXT)
    assert out["previous_reading"] == 5919 and seen == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    assert notes[-1] == "re-read: previous_reading 5419 -> 5919 (accepted, gemini-3.1-flash-lite)"
    assert llm.calls_made() == 2
    out2, _ = reread_bill(bills / "BILL_A.png", bill, EXT)  # both reads are cached now
    assert out2["previous_reading"] == 5919 and llm.calls_made() == 2


def test_second_reader_respects_the_cap_and_plan_counts_calls(setup, monkeypatch):
    from core.extract import reread_bill, reread_calls_needed
    bills, _t, _o = setup
    bill = {**LESCO1, "total_charges": 5000}
    wrong = {"previous_reading": 5419, "current_reading": 6119, "units_consumed": 200}
    monkeypatch.setitem(llm._CALLERS, "google", _by_model({"gemini-3.5-flash-lite": wrong, "gemini-3.1-flash-lite": wrong}, []))
    assert reread_calls_needed(bills / "BILL_A.png", bill, EXT) == 1  # first read not cached
    monkeypatch.setenv("MAX_LLM_CALLS", "1")
    with pytest.raises(llm.LLMBlocked, match="MAX_LLM_CALLS"):
        reread_bill(bills / "BILL_A.png", bill, EXT)  # first read used the only call; the second reader is blocked
    assert llm.calls_made() == 1
    assert reread_calls_needed(bills / "BILL_A.png", bill, EXT) == 1  # now only the second reader is missing


# ---- UI helpers: upload validation, one-bill analysis, CSV/JSON download, app smoke test ----

def test_check_image_accepts_images_and_rejects_everything_else(tmp_path):
    import io
    from core.images import MAX_BYTES, check_image
    png = io.BytesIO(); Image.new("RGB", (4, 4)).save(png, "PNG")
    jpg = io.BytesIO(); Image.new("RGB", (4, 4)).save(jpg, "JPEG")
    assert check_image(png.getvalue()) == ".png" and check_image(jpg.getvalue()) == ".jpg"
    for bad in (b"", b"%PDF-1.7 not an image", b"MZ\x90\x00 executable", png.getvalue()[:20], b"\x89PNG\r\n\x1a\n" + b"x" * 10):
        with pytest.raises(ValueError):
            check_image(bad)
    with pytest.raises(ValueError, match="10 MB"):
        check_image(b"\x89PNG\r\n\x1a\n" + b"0" * MAX_BYTES)


def test_analyze_returns_reconcile_notes_and_downloads(setup, monkeypatch):
    from core import analyze as az
    bills, _t, _o = setup
    low = {**GOLDEN, "payable_after_due_date": 3574}
    ext = {**EXT, "late_payment": [{"label": "Amount Payable after 24-Apr-26", "amount": 3716, "valid_from": None, "valid_until": None}]}

    def model(m, prompt, system, images, json_mode, max_tokens):
        return json.dumps(low if prompt.startswith("You extract structured data") else ext)

    monkeypatch.setitem(llm._CALLERS, "google", model)
    res = az.analyze(bills / "BILL_A.png")
    assert res["l1"]["payable_after_due_date"] == 3716 and res["notes"] == ["late amount raised from 3574 to 3716"]
    assert not res["reconcile"]["needs_attention"] and llm.calls_made() == 2
    assert az.badge(res["reconcile"]["field_status"]["total_taxes"]) == "green" and az.badge(None) == "amber"
    text = az.level1_csv("BILL_A", res["l1"])
    row = list(csv.DictReader(text.splitlines(keepends=True)))[0]
    assert row["bill_id"] == "BILL_A" and json.loads(row["json"])["payable_after_due_date"] == 3716 and "\n" not in row["json"]
    assert json.loads(az.json_text(res["l1"])) == json.loads(row["json"])


def test_streamlit_app_runs_in_both_modes_without_model_calls(monkeypatch):
    from streamlit.testing.v1 import AppTest
    root = Path(__file__).resolve().parent.parent
    if not any((root / d / "level1.csv").exists() for d in ("output_v3", "output")):
        pytest.skip("no output folder to browse")
    monkeypatch.delenv("ALLOW_LLM_CALLS", raising=False)
    llm.reset_call_counter()
    at = AppTest.from_file(str(root / "app" / "streamlit_app.py"), default_timeout=30).run()
    assert not at.exception and any("Live analysis is off" in w.value for w in at.warning)
    at.sidebar.radio[0].set_value("Browse submitted results").run()
    assert not at.exception and len(at.tabs) == 4 and llm.calls_made() == 0


# ---- demo readiness: blocked re-read is skipped, unusable answers are cached, cache-only analysis ----

def test_run_skips_a_blocked_reread_and_keeps_the_bill(setup, monkeypatch, capsys):
    bills, tpl, out = setup
    answered = {"n": 0}

    def model(m, prompt, system, images, json_mode, max_tokens):
        if prompt.startswith("You extract structured data"):
            return json.dumps(BAD_PREV)
        return fake_model(m, prompt, system, images, json_mode, max_tokens)

    monkeypatch.setitem(llm._CALLERS, "google", model)
    monkeypatch.setenv("MAX_LLM_CALLS", "40")
    run.main(["--bills", str(bills), "--templates", str(tpl), "--out", str(out)])  # first run caches L1, ext and QA (and re-reads)
    capsys.readouterr()
    for f in out.parent.glob("cache/*reread*"):
        f.unlink()  # forget the re-read, then forbid calls
    monkeypatch.setenv("ALLOW_LLM_CALLS", "false")
    llm.reset_call_counter()
    assert run.main(["--bills", str(bills), "--templates", str(tpl), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "re-read skipped (model calls are off or capped)" in text and "LLM calls used: 0" in text
    assert json.loads(read(out / "level1.csv")[0]["json"])["previous_reading"] == 8419  # printed value kept, bill not lost


def test_unusable_reread_is_cached_and_not_asked_again(setup, monkeypatch):
    from core.extract import reread_bill, reread_calls_needed
    bills, _t, _o = setup
    monkeypatch.setitem(llm._CALLERS, "google", lambda *a: "{}")
    out, notes = reread_bill(bills / "BILL_A.png", BAD_PREV, EXT)
    assert out == BAD_PREV and llm.calls_made() == 1 and any("unusable" in n for n in notes)
    assert reread_calls_needed(bills / "BILL_A.png", BAD_PREV, EXT) == 0
    out2, notes2 = reread_bill(bills / "BILL_A.png", BAD_PREV, EXT)
    assert out2 == BAD_PREV and llm.calls_made() == 1 and any("cached" in n for n in notes2)


def test_cached_analysis_never_calls_a_model(setup, monkeypatch):
    from core import analyze as az
    bills, _t, _o = setup
    assert az.cached_analysis(bills / "BILL_A.png") is None and llm.calls_made() == 0  # nothing cached: no analysis, no call
    monkeypatch.setitem(llm._CALLERS, "google", lambda m, prompt, *a: json.dumps(BAD_PREV if prompt.startswith("You extract structured data") else EXT)
                        if not prompt.startswith("You re-read") else json.dumps({"previous_reading": 8819, "current_reading": 8970, "units_consumed": 151}))
    first = az.analyze(bills / "BILL_A.png")
    assert first["l1"]["previous_reading"] == 8819 and first["before"]["needs_attention"] and not first["reconcile"]["needs_attention"]
    calls = llm.calls_made()
    monkeypatch.setenv("ALLOW_LLM_CALLS", "false")
    again = az.cached_analysis(bills / "BILL_A.png")
    assert again["l1"]["previous_reading"] == 8819 and llm.calls_made() == calls
