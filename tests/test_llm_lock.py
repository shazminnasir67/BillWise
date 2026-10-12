"""Mocked tests only: no provider is ever contacted."""
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import cache, extract, llm  # noqa: E402
from scripts import compare_golden  # noqa: E402

_GOLDEN_FILE = Path(__file__).parent / "golden" / "KESC_0008.json"
if not _GOLDEN_FILE.exists():  # dev reference data is not shipped in the submission ZIP
    pytest.skip("tests/golden not present", allow_module_level=True)
GOLDEN = json.loads(_GOLDEN_FILE.read_text())


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MODEL_CHAIN", "gemini-3.5-flash-lite")
    monkeypatch.delenv("ALLOW_LLM_CALLS", raising=False)
    monkeypatch.delenv("MAX_LLM_CALLS", raising=False)
    llm.reset_call_counter()
    calls = []

    def fake(model, prompt, system, images, json_mode, max_tokens):
        calls.append(model)
        return json.dumps(GOLDEN)

    monkeypatch.setitem(llm._CALLERS, "google", fake)
    img = tmp_path / "KESC_0008.png"
    Image.new("RGB", (8, 8), "white").save(img)
    return calls, img


def test_blocked_by_default(env):
    calls, img = env
    with pytest.raises(llm.LLMBlocked, match="ALLOW_LLM_CALLS"):
        llm.generate("x", images=[img], json_mode=True)
    assert calls == [] and llm.calls_made() == 0


def test_cap_stops_calls(env, monkeypatch):
    calls, img = env
    monkeypatch.setenv("ALLOW_LLM_CALLS", "true")
    monkeypatch.setenv("MAX_LLM_CALLS", "2")
    llm.generate("x", images=[img], json_mode=True)
    llm.generate("x", images=[img], json_mode=True)
    with pytest.raises(llm.LLMBlocked, match="MAX_LLM_CALLS"):
        llm.generate("x", images=[img], json_mode=True)
    assert len(calls) == 2


def test_cache_prevents_repeat_calls(env, monkeypatch):
    calls, img = env
    monkeypatch.setenv("ALLOW_LLM_CALLS", "true")
    first = extract.extract_bill(img)
    second = extract.extract_bill(img)
    assert len(calls) == 1
    assert not first["cached"] and second["cached"]
    assert second["data"] == first["data"]


def test_cache_hit_needs_no_permission(env, monkeypatch):
    calls, img = env
    monkeypatch.setenv("ALLOW_LLM_CALLS", "true")
    extract.extract_bill(img)
    monkeypatch.setenv("ALLOW_LLM_CALLS", "false")
    assert extract.extract_bill(img)["cached"]


def test_cache_key_includes_model_and_prompt_version(env):
    _, img = env
    cache.put(img, "l1", "l1_v1", "gemini-3.5-flash-lite", {"data": {}})
    assert cache.get(img, "l1", "l1_v2", "gemini-3.5-flash-lite") is None
    assert cache.get(img, "l1", "l1_v1", "gemini-3.1-flash-lite") is None


def test_one_retry_on_invalid_output(env, monkeypatch):
    calls, img = env
    monkeypatch.setenv("ALLOW_LLM_CALLS", "true")
    bad = {**GOLDEN, "due_date": "21/04/2026"}
    answers = iter([json.dumps(bad), json.dumps(GOLDEN)])
    monkeypatch.setitem(llm._CALLERS, "google", lambda *a: next(answers))
    assert extract.extract_bill(img)["data"]["due_date"] == "2026-04-21"
    assert llm.calls_made() == 2


def test_gemini_alias_and_disallowed_model(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("API_KEY", "k")
    assert llm._api_key("google") == "k"
    monkeypatch.setenv("MODEL_CHAIN", "not-an-allowed-model")
    with pytest.raises(ValueError):
        llm.get_chain()


def test_compare_tolerance():
    near = {**GOLDEN, "total_charges": 2861.4}
    assert all(ok for *_, ok in compare_golden.diff(GOLDEN, near))
    far = {**GOLDEN, "total_charges": 2863}
    assert not all(ok for *_, ok in compare_golden.diff(GOLDEN, far))
