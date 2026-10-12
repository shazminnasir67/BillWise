"""LLM adapters (Gemini, Anthropic, OpenAI) with vision input, JSON output and failover.

Only the five hackathon-approved model IDs are accepted. The chain comes from
MODEL_CHAIN (comma separated) or, if unset, MODEL_NAME alone.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("llm")

ALLOWED_MODELS = {
    "claude-haiku-4-5": "anthropic",
    "gemini-3.5-flash-lite": "google",
    "gemini-3.1-flash-lite": "google",
    "gpt-4.1-mini": "openai",
    "gpt-5-mini": "openai",
}
TIMEOUT_S = 90


class LLMError(Exception):
    """One model failed; the router moves on to the next one."""


class AllModelsFailed(Exception):
    def __init__(self, reasons: dict[str, str]):
        self.reasons = reasons
        super().__init__("; ".join(f"{m}: {r}" for m, r in reasons.items()))


@dataclass
class LLMResult:
    text: str
    model_used: str
    latency_ms: float
    fallback_count: int
    errors: list[str] = field(default_factory=list)


def get_chain() -> list[str]:
    raw = os.environ.get("MODEL_CHAIN") or os.environ.get("MODEL_NAME", "")
    chain = [m.strip() for m in raw.split(",") if m.strip()]
    bad = [m for m in chain if m not in ALLOWED_MODELS]
    if bad or not chain:
        raise ValueError(f"MODEL_CHAIN/MODEL_NAME must use allowed IDs {sorted(ALLOWED_MODELS)}; got {chain}")
    return chain


def _norm_provider(name: str) -> str:
    return "google" if name.lower() == "gemini" else name.lower()


def _api_key(provider: str) -> str | None:
    names = ["GOOGLE_API_KEY", "GEMINI_API_KEY"] if provider == "google" else [f"{provider.upper()}_API_KEY"]
    key = next((os.environ[n] for n in names if os.environ.get(n)), None)
    if not key and _norm_provider(os.environ.get("MODEL_PROVIDER", "")) == provider:
        key = os.environ.get("API_KEY")
    return key


class LLMBlocked(Exception):
    """Raised (never failed over) when the budget lock forbids a provider call."""


_calls = 0
_last_call = 0.0
_min_interval = float(os.environ.get("LLM_MIN_INTERVAL_S", "0"))


def set_min_interval(seconds: float) -> None:
    """Minimum gap between provider calls (free tier: 12 requests/min -> 5 s)."""
    global _min_interval
    _min_interval = seconds


def _throttle() -> None:
    global _last_call
    wait = _last_call + _min_interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def calls_made() -> int:
    return _calls


def reset_call_counter() -> None:
    global _calls
    _calls = 0


def _guard(model: str, purpose: str) -> None:
    """Budget lock: refuse unless ALLOW_LLM_CALLS=true, stop at MAX_LLM_CALLS, log every call."""
    global _calls
    if os.environ.get("ALLOW_LLM_CALLS", "false").strip().lower() != "true":
        raise LLMBlocked("LLM calls are disabled: set ALLOW_LLM_CALLS=true in your own terminal to allow them")
    cap = int(os.environ.get("MAX_LLM_CALLS", "40"))
    if _calls >= cap:
        raise LLMBlocked(f"MAX_LLM_CALLS={cap} reached; no further LLM calls this run")
    _calls += 1
    log.info("LLM call %d (%s, %s)", _calls, model, purpose)


def _load_images(paths: list[str | Path]) -> list[tuple[bytes, str]]:
    out = []
    for p in paths:
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        out.append((Path(p).read_bytes(), mime))
    return out


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _call_google(model, prompt, system, images, json_mode, max_tokens):
    from google import genai
    from google.genai import errors, types

    client = genai.Client(api_key=_api_key("google"), http_options=types.HttpOptions(timeout=TIMEOUT_S * 1000))
    parts = [types.Part.from_bytes(data=b, mime_type=m) for b, m in images] + [prompt]
    cfg = types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=max_tokens,
        temperature=0,
        response_mime_type="application/json" if json_mode else None,
    )
    try:
        resp = client.models.generate_content(model=model, contents=parts, config=cfg)
    except errors.APIError as e:
        raise LLMError(f"{e.code}: {str(e)[:200]}") from e
    return resp.text or ""


def _call_anthropic(model, prompt, system, images, json_mode, max_tokens):
    import anthropic

    client = anthropic.Anthropic(api_key=_api_key("anthropic"), timeout=TIMEOUT_S)
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": m, "data": base64.b64encode(b).decode()}}
        for b, m in images
    ] + [{"type": "text", "text": prompt}]
    try:
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, temperature=0,
            system=system or anthropic.NOT_GIVEN,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIError as e:
        raise LLMError(f"{getattr(e, 'status_code', 'conn')}: {str(e)[:200]}") from e
    return "".join(b.text for b in resp.content if b.type == "text")


def _call_openai(model, prompt, system, images, json_mode, max_tokens):
    import openai

    client = openai.OpenAI(api_key=_api_key("openai"), timeout=TIMEOUT_S)
    content = [
        {"type": "image_url", "image_url": {"url": f"data:{m};base64,{base64.b64encode(b).decode()}"}}
        for b, m in images
    ] + [{"type": "text", "text": prompt}]
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": content}]
    kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
    try:
        resp = client.chat.completions.create(
            model=model, messages=messages, max_completion_tokens=max_tokens, **kwargs)
    except openai.APIError as e:
        raise LLMError(f"{getattr(e, 'status_code', 'conn')}: {str(e)[:200]}") from e
    return resp.choices[0].message.content or ""


_CALLERS = {"google": _call_google, "anthropic": _call_anthropic, "openai": _call_openai}


def generate(
    prompt: str,
    images: list[str | Path] | None = None,
    system: str | None = None,
    json_mode: bool = False,
    max_tokens: int = 8192,
    chain: list[str] | None = None,
    purpose: str = "unspecified",
) -> LLMResult:
    """Try each model in the chain until one returns non-empty (and, in json_mode, parseable) output."""
    chain = chain or get_chain()
    loaded = _load_images(images or [])
    reasons: dict[str, str] = {}
    for idx, model in enumerate(chain):
        if model not in ALLOWED_MODELS:
            raise ValueError(f"model {model!r} is not an allowed ID")
        provider = ALLOWED_MODELS[model]
        if not _api_key(provider):
            reasons[model] = f"no API key for {provider}"
            continue
        _guard(model, purpose)
        _throttle()
        start = time.perf_counter()
        try:
            text = _CALLERS[provider](model, prompt, system, loaded, json_mode, max_tokens)
            if not text.strip():
                raise LLMError("empty output")
            if json_mode:
                text = _strip_fences(text)
                json.loads(text)
        except json.JSONDecodeError as e:
            reasons[model] = f"invalid JSON: {e}"
        except LLMError as e:
            reasons[model] = str(e)
        else:
            ms = (time.perf_counter() - start) * 1000
            log.info("model=%s latency_ms=%.0f fallbacks=%d", model, ms, idx)
            return LLMResult(text, model, ms, idx, [f"{m}: {r}" for m, r in reasons.items()])
        log.warning("model=%s failed: %s", model, reasons[model])
    raise AllModelsFailed(reasons)
