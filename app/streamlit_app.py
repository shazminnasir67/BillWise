"""BillWise demo UI: upload a bill (live) or browse the submitted results. Run: streamlit run app/streamlit_app.py

Model calls happen only when the "Allow model calls" switch in the sidebar is on (it sets ALLOW_LLM_CALLS for this
process) or ALLOW_LLM_CALLS=true was set when Streamlit started. Cached bills never need a call.
"""
import csv
import hashlib
import html
import json
import os
import sys
import tempfile
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import analyze as az  # noqa: E402
from core import calc, extract, llm, qa  # noqa: E402
from core.images import check_image  # noqa: E402
from core.reconcile import reconcile  # noqa: E402

SCALAR_FIELDS = ["provider", "tariff", "sanctioned_load_kw", "bill_month", "reading_date", "issue_date", "due_date",
                 "previous_reading", "current_reading", "units_consumed", "total_charges", "total_taxes", "current_bill",
                 "arrears", "payable_within_due_date", "payable_after_due_date"]
BADGE_TEXT = {"green": "Verified", "amber": "Unverified", "red": "Check failed"}
CHECK_TEXT = {"ok": ("Passed", "green"), "mismatch": ("Failed", "red"), "warning": ("Warning", "amber"),
              "not_checkable": ("Not checkable", "grey")}

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&display=swap');
html, body, [class*="css"], .stApp, button, input, textarea, select { font-family: 'Instrument Sans', system-ui, sans-serif !important; }
.stApp { background: #fbfcfe; color: #1b2430; }
.block-container { padding-top: 1.6rem; max-width: 1280px; }
[data-testid="stSidebar"] { background: #f1f5fb; border-right: 1px solid #e2e8f2; }
h1, h2, h3 { letter-spacing: -0.01em; color: #101828; }
.brand { font-size: 1.6rem; font-weight: 700; color: #101828; letter-spacing: -0.02em; }
.brand span { color: #2563eb; }
.tag { color: #566274; font-size: 0.95rem; margin-top: 0.15rem; }
.hero { background: linear-gradient(135deg, #eaf1ff 0%, #f6faff 60%, #ffffff 100%); border: 1px solid #dbe6fb;
        border-radius: 16px; padding: 1.2rem 1.5rem; margin-bottom: 1.2rem; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0.8rem; margin: 0.4rem 0 1rem; }
.card { background: #ffffff; border: 1px solid #e4e9f2; border-radius: 14px; padding: 0.8rem 1rem; box-shadow: 0 1px 2px rgba(16,24,40,.04); }
.card .k { font-size: 0.74rem; text-transform: uppercase; letter-spacing: .06em; color: #667085; font-weight: 600; }
.card .v { font-size: 1.35rem; font-weight: 700; color: #101828; margin-top: 0.15rem; }
.pill { display: inline-block; padding: 0.12rem 0.65rem; border-radius: 999px; font-size: 0.78rem; font-weight: 600; white-space: nowrap; }
.pill.green { background: #e7f6ec; color: #146c3a; } .pill.amber { background: #fff2dc; color: #9a5b00; }
.pill.red { background: #fde8e8; color: #a61b1b; } .pill.grey { background: #eef1f6; color: #566274; }
.banner { border-radius: 14px; padding: 0.85rem 1.1rem; margin-bottom: 0.9rem; font-weight: 500; border: 1px solid; }
.banner.green { background: #eefaf2; border-color: #bfe6cc; color: #146c3a; } .banner.red { background: #fff1f1; border-color: #f5c2c2; color: #a61b1b; }
.banner.amber { background: #fff8ea; border-color: #f3d9a4; color: #8a5200; }
table.t { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #e4e9f2; border-radius: 12px; overflow: hidden; font-size: 0.9rem; }
table.t th { text-align: left; background: #f4f7fc; color: #566274; font-weight: 600; padding: 0.55rem 0.8rem; font-size: 0.78rem; text-transform: uppercase; letter-spacing: .05em; }
table.t td { padding: 0.5rem 0.8rem; border-top: 1px solid #edf0f6; vertical-align: top; }
table.t td.num { font-variant-numeric: tabular-nums; }
.sub { font-weight: 600; color: #101828; margin: 0.9rem 0 0.4rem; }
.muted { color: #667085; font-size: 0.85rem; }
.qa { background: #fff; border: 1px solid #e4e9f2; border-radius: 12px; padding: 0.8rem 1rem; margin-bottom: 0.7rem; }
.qa .q { font-weight: 600; color: #101828; margin-bottom: 0.25rem; }
div[data-testid="stTabs"] button { font-weight: 600; }
.stButton > button[kind="primary"] { background: #2563eb; border: 0; border-radius: 10px; font-weight: 600; }
</style>
"""


def esc(v) -> str:
    return html.escape(str(v))


def pill(text: str, kind: str) -> str:
    return f'<span class="pill {kind}">{esc(text)}</span>'


def table(headers: list[str], rows: list[list[str]], raw_cols: tuple[int, ...] = ()) -> str:
    """HTML table; every cell is escaped (model output is untrusted) except columns in raw_cols, which hold our own pills."""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c if i in raw_cols else esc(c)}</td>" for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f'<table class="t"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def money(v) -> str:
    if v is None:
        return "not printed"
    return f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else f"{v:,}"


def results_folder() -> Path | None:
    for c in (ROOT / "output_v3", ROOT / "output", ROOT.parent / "output"):
        if (c / "level1.csv").exists():
            return c
    return None


def show_values(l1: dict, rec: dict) -> None:
    status = rec["field_status"]
    cards = [("Provider", l1["provider"] or "not printed"), ("Bill month", l1["bill_month"] or "not printed"),
             ("Units", money(l1["units_consumed"])), ("Current bill", "Rs " + money(l1["current_bill"])),
             ("Pay by due date", "Rs " + money(l1["payable_within_due_date"])), ("Pay after due date", "Rs " + money(l1["payable_after_due_date"]))]
    st.markdown('<div class="cards">' + "".join(f'<div class="card"><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div></div>' for k, v in cards) + "</div>", unsafe_allow_html=True)
    rows = [[f.replace("_", " "), "null" if l1[f] is None else str(l1[f]), pill(BADGE_TEXT[az.badge(status.get(f))], az.badge(status.get(f)))] for f in SCALAR_FIELDS]
    st.markdown(table(["Field", "Value", "Status"], rows, raw_cols=(2,)), unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    for col, key, label, empty in ((c1, "charges", "Charges", "none printed (totals only)"), (c2, "taxes", "Taxes", "combined tax only")):
        with col:
            st.markdown(f'<div class="sub">{label} {pill(BADGE_TEXT[az.badge(status.get(key))], az.badge(status.get(key)))}</div>', unsafe_allow_html=True)
            items = [[i["type"].replace("_", " "), money(i["amount"])] for i in l1[key]]
            st.markdown(table(["Type", "Amount"], items or [[empty, "-"]]), unsafe_allow_html=True)


def show_verification(rec: dict, notes: list[str], extra: dict | None = None, before: dict | None = None) -> None:
    failed_before = [c for c in (before or {}).get("checks", []) if c["status"] == "mismatch"]
    if failed_before and notes:
        st.markdown('<div class="sub">Failed on the first read</div><div class="muted">These checks failed before the targeted re-read; the log below shows what the re-read did.</div>', unsafe_allow_html=True)
        st.markdown(table(["Check", "Expected", "Printed", "Detail"], [[c["check"].replace("_", " "), str(c["expected"]), str(c["printed"]), c["how"]] for c in failed_before]), unsafe_allow_html=True)
    if rec["needs_attention"]:
        st.markdown(f'<div class="banner red">Needs attention: {esc("; ".join(rec["reasons"]))}</div>', unsafe_allow_html=True)
    elif rec["warnings"]:
        st.markdown(f'<div class="banner amber">Notes: {esc("; ".join(rec["warnings"]))}</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="banner green">Every check that could run passed.</div>', unsafe_allow_html=True)
    rows = [[c["check"].replace("_", " "), pill(*CHECK_TEXT[c["status"]]), str(c.get("expected", "-")), str(c.get("printed", "-")), c.get("how") or c.get("why", "")] for c in rec["checks"]]
    st.markdown('<div class="sub">Reconciliation checks</div><div class="muted">Printed values are never changed by these checks.</div>', unsafe_allow_html=True)
    st.markdown(table(["Check", "Result", "Expected", "Printed", "Detail"], rows, raw_cols=(1,)), unsafe_allow_html=True)
    st.markdown('<div class="sub">Re-reads and cross-checks</div>', unsafe_allow_html=True)
    st.markdown(table(["Log"], [[n] for n in notes] or [["none needed"]]), unsafe_allow_html=True)
    if extra:
        st.markdown('<div class="muted" style="margin-top:.6rem">' + esc(" | ".join(f"{k}: {v}" for k, v in extra.items())) + "</div>", unsafe_allow_html=True)


def show_answers(pairs: list[tuple[str, str]]) -> None:
    for q, a in pairs:
        st.markdown(f'<div class="qa"><div class="q">{esc(q)}</div>{esc(a)}</div>', unsafe_allow_html=True)


def downloads(bill_id: str, l1: dict) -> None:
    c1, c2 = st.columns(2)
    c1.download_button("Download JSON", az.json_text(l1), f"{bill_id}.json", "application/json", width="stretch")
    c2.download_button("Download CSV (level1 format)", az.level1_csv(bill_id, l1), f"{bill_id}_level1.csv", "text/csv", width="stretch")
    st.code(az.json_text(l1), language="json")


def sidebar() -> str:
    with st.sidebar:
        st.markdown('<div class="brand">Bill<span>Wise</span></div><div class="tag">Electricity bill decoder</div>', unsafe_allow_html=True)
        mode = st.radio("Mode", ["Upload a bill", "Browse submitted results"])
        st.markdown('<div class="sub">Model calls</div>', unsafe_allow_html=True)
        on = st.toggle("Allow model calls", value=os.environ.get("ALLOW_LLM_CALLS", "").lower() == "true",
                       help="Uses your Gemini API quota. Off by default; cached bills never need a call.")
        cap = st.number_input("Call cap for this session", 1, 200, int(os.environ.get("MAX_LLM_CALLS", "40")))
        if on:
            os.environ["ALLOW_LLM_CALLS"], os.environ["MAX_LLM_CALLS"] = "true", str(cap)
        else:
            os.environ.pop("ALLOW_LLM_CALLS", None)
        st.markdown(f'<div class="muted">{"Calls are allowed. Each new bill takes about 2 calls." if on else "Calls are off. Only cached results can be shown."}</div>', unsafe_allow_html=True)
    return mode


def upload_mode() -> None:
    st.markdown('<div class="hero"><div class="brand">Upload a bill</div><div class="tag">PNG, JPEG or WEBP, up to 10 MB. The image is read by the model, then every number is checked by plain arithmetic.</div></div>', unsafe_allow_html=True)
    if os.environ.get("ALLOW_LLM_CALLS", "").lower() != "true":
        st.warning("Live analysis is off. Switch on Allow model calls in the sidebar to analyse a new bill.")
    up = st.file_uploader("Bill image", type=["png", "jpg", "jpeg", "webp"], label_visibility="collapsed")
    questions_text = st.text_area("Questions about this bill (optional, one per line, costs 1 extra call)", "", height=90)
    if up is None:
        st.info("Upload a bill image to begin.")
        return
    data = up.getvalue()
    try:
        ext = check_image(data)
    except ValueError as e:
        st.error(f"Rejected: {e}")
        return
    bill_id = Path(up.name).stem[:40] or "uploaded_bill"
    path = Path(tempfile.gettempdir()) / "billwise_uploads" / f"{bill_id}{ext}"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    key = hashlib.sha256(data).hexdigest()[:12] + questions_text
    if st.button("Extract and verify", type="primary") or st.session_state.get("key") == key:
        if st.session_state.get("key") != key:
            llm.reset_call_counter()
            try:
                with st.spinner("Reading the bill, then checking the numbers..."):
                    res = az.analyze(path)
                    questions = [{"question_id": f"Q{i}", "question": q.strip()} for i, q in enumerate(questions_text.splitlines(), 1) if q.strip()]
                    answers = None
                    if questions:
                        stats = calc.compute_stats(res["l1"], res["ext"], questions)
                        answers = qa.answer_questions(path, res["l1"], res["ext"], stats, questions)["answers"]
                st.session_state.update(key=key, res=res, answers=answers, questions=questions, calls=llm.calls_made())
            except llm.LLMBlocked as e:
                st.error(str(e))
                return
            except (llm.AllModelsFailed, extract.ExtractionError) as e:
                st.error(f"Could not read this bill reliably: {str(e)[:300]}")
                return
        res, answers, questions = st.session_state["res"], st.session_state["answers"], st.session_state["questions"]
        tabs = st.tabs(["Extracted values", "Verification", "Answers", "Download"])
        with tabs[0]:
            left, right = st.columns([5, 7], gap="large")
            left.image(data, caption="Uploaded bill", width="stretch")
            with right:
                show_values(res["l1"], res["reconcile"])
        with tabs[1]:
            show_verification(res["reconcile"], res["notes"], {"models": res["models"], "cached": res["cached"], "model calls this analysis": st.session_state["calls"]}, res["before"])
        with tabs[2]:
            if answers:
                show_answers([(q["question"], answers[q["question_id"]]) for q in questions])
            else:
                st.info("Type questions above and run again to get answers grounded in this bill.")
        with tabs[3]:
            downloads(bill_id, res["l1"])


def browse_mode() -> None:
    folder = results_folder()
    if folder is None:
        st.info("No output folder found. Run run.py first, or use the upload mode.")
        return
    with open(folder / "level1.csv", newline="", encoding="utf-8") as f:
        l1_rows = {r["bill_id"]: json.loads(r["json"]) for r in csv.DictReader(f)}
    with open(folder / "level2.csv", newline="", encoding="utf-8") as f:
        l2_rows = list(csv.DictReader(f))
    st.markdown(f'<div class="hero"><div class="brand">Submitted results</div><div class="tag">{len(l1_rows)} bills from the {esc(folder.name)} folder. Checks are recomputed from the saved values.</div></div>', unsafe_allow_html=True)
    bill_id = st.selectbox("Bill", list(l1_rows))
    l1 = l1_rows[bill_id]
    rec = reconcile(l1)
    image = next(iter(p for d in (ROOT / "data" / "test" / "bills", ROOT / "data" / "train" / "bills") for p in (d.glob(f"{bill_id}.*") if d.exists() else [])), None)
    full = az.cached_analysis(image) if image else None  # cache only: never calls a model
    if full:
        l1, rec = full["l1"], full["reconcile"]
    tabs = st.tabs(["Extracted values", "Verification", "Answers", "Download"])
    with tabs[0]:
        left, right = st.columns([5, 7], gap="large")
        if image:
            left.image(str(image), caption=bill_id, width="stretch")
        else:
            left.info("The bill image is not included in this folder.")
        with right:
            show_values(l1, rec)
    with tabs[1]:
        if full:
            show_verification(rec, full["notes"], {"source": "saved run", "models": full["models"]}, full["before"])
        else:
            show_verification(rec, [], {"source": folder.name, "note": "checks recomputed from the CSV values only"})
    with tabs[2]:
        show_answers([(f"{r['question_id']}. {r['question']}", r["answer"]) for r in l2_rows if r["bill_id"] == bill_id])
    with tabs[3]:
        downloads(bill_id, l1)


st.set_page_config(page_title="BillWise", layout="wide")
st.markdown(CSS, unsafe_allow_html=True)
mode = sidebar()
upload_mode() if mode == "Upload a bill" else browse_mode()
