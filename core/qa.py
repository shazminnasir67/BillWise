"""Level 2: ONE LLM call per bill answers every question from extracted facts + Python-computed stats."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from core import cache, llm
from core.extract import PROMPT_DIR, ExtractionError

QA_VERSION = "qa_v2"
MAX_ANSWER_CHARS = 900
THINKING_ALOUD = re.compile(r"\b(wait|let me|actually|let's look|let's check|let us|hmm)\b", re.I)
NOT_SHOWN = "Not shown on this bill."


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_id: str
    answer: str

    @field_validator("answer")
    @classmethod
    def _non_empty(cls, v):
        if not v.strip():
            raise ValueError("answer must not be empty")
        return v.strip()


class Answers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answers: list[Answer]


def kind_for(questions: list[dict], stats: dict | None = None) -> str:
    """Cache kind: the question set and the computed stats, so a change in either re-asks."""
    digest = hashlib.sha256(json.dumps([questions, stats], sort_keys=True, default=str).encode()).hexdigest()[:10]
    return f"qa-{digest}"


def _build_prompt(l1: dict, ext: dict, stats: dict, questions: list[dict]) -> str:
    facts = {"level1": l1, "extended": ext}
    return (
        (PROMPT_DIR / f"{QA_VERSION}.md").read_text(encoding="utf-8")
        + "\n<bill_facts>\n" + json.dumps(facts, ensure_ascii=False) + "\n</bill_facts>\n"
        + "<computed_stats>\n" + json.dumps(stats, ensure_ascii=False) + "\n</computed_stats>\n"
        + "<questions>\n" + json.dumps(questions, ensure_ascii=False) + "\n</questions>\n"
    )


def _check(text: str, questions: list[dict]) -> dict[str, str]:
    parsed = Answers.model_validate_json(text)
    got = {a.question_id: a.answer for a in parsed.answers}
    missing = [q["question_id"] for q in questions if q["question_id"] not in got]
    if missing:
        raise ValueError(f"missing answers for question_id(s): {missing}")
    return {q["question_id"]: got[q["question_id"]] for q in questions}


def quality_problems(answers: dict[str, str]) -> dict[str, str]:
    """question_id -> reason, for answers that think out loud or are too long."""
    bad = {}
    for qid, text in answers.items():
        m = THINKING_ALOUD.search(text)
        if m:
            bad[qid] = f"contains thinking-out-loud text ({m.group(0)!r})"
        elif len(text) > MAX_ANSWER_CHARS:
            bad[qid] = f"is {len(text)} characters, over the {MAX_ANSWER_CHARS} limit"
    return bad


def trim_answer(text: str) -> str:
    """Last resort: keep the trailing clean sentences that fit the limit; fall back to 'Not shown on this bill.'"""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    clean = [s for s in sentences if not THINKING_ALOUD.search(s)]
    kept: list[str] = []
    for sentence in reversed(clean):
        if len(" ".join([sentence, *kept])) > MAX_ANSWER_CHARS:
            break
        kept.insert(0, sentence)
    return " ".join(kept) or NOT_SHOWN


def answer_questions(bill: str | Path, l1: dict, ext: dict, stats: dict, questions: list[dict],
                     chain: list[str] | None = None) -> dict:
    """questions: [{"question_id", "question"}]. Returns {"answers": {id: text}, "model_used", "cached"}.

    At most one retry (structure error or answer-quality problem, counted against MAX_LLM_CALLS);
    answers that are still bad after it are trimmed in code.
    """
    bill = Path(bill)
    chain = chain or llm.get_chain()
    kind = kind_for(questions, stats)
    for model in chain:
        hit = cache.get(bill, kind, QA_VERSION, model)
        if hit:
            return {"answers": hit["answers"], "model_used": hit["model_used"], "cached": True}

    prompt = _build_prompt(l1, ext, stats, questions)
    purpose = f"qa {bill.name} ({len(questions)} questions)"
    result = llm.generate(prompt, images=[bill], json_mode=True, chain=chain, purpose=purpose, max_tokens=8192)
    first: dict[str, str] | None = None
    try:
        first = _check(result.text, questions)
        problems = quality_problems(first)
        problem = "; ".join(f"{q} {why}" for q, why in problems.items())
    except (ValidationError, ValueError) as e:
        problem = str(e)
    answers = first
    if problem:
        retry = (f"{prompt}\nYour previous answer was rejected.\nPrevious answer:\n{result.text}\n"
                 f"Problem:\n{problem}\nReturn the corrected JSON object only: short final answers, "
                 "no self-correction or thinking out loud.")
        result = llm.generate(retry, images=[bill], json_mode=True, chain=chain, purpose=purpose + " (retry)", max_tokens=8192)
        try:
            answers = _check(result.text, questions)
        except (ValidationError, ValueError) as second:
            if first is None:
                raise ExtractionError(f"{bill.name} [qa]: invalid output after retry: {second}") from second
            answers = first  # the first answer was well-formed; only its quality was bad
    answers = {q: (trim_answer(a) if q in quality_problems(answers) else a) for q, a in answers.items()}
    out = {"answers": answers, "model_used": result.model_used}
    cache.put(bill, kind, QA_VERSION, result.model_used, out)
    return {**out, "cached": False}
