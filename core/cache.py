"""Disk cache for extraction results, keyed by bill file hash + prompt version + model."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def cache_dir() -> Path:
    d = Path(os.environ.get("CACHE_DIR", "cache"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def file_hash(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def _path(bill: str | Path, kind: str, prompt_version: str, model: str) -> Path:
    return cache_dir() / f"{Path(bill).stem}__{file_hash(bill)}__{kind}__{prompt_version}__{model}.json"


def get(bill: str | Path, kind: str, prompt_version: str, model: str) -> dict | None:
    p = _path(bill, kind, prompt_version, model)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def put(bill: str | Path, kind: str, prompt_version: str, model: str, data: dict) -> None:
    _path(bill, kind, prompt_version, model).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
