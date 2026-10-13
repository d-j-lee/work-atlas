"""LLM 작업의 공통 — 글자·해시·경로·프롬프트 읽기·JSON 읽기 (다른 모듈이 서로를 부르지 않게 여기에 둔다)."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..common import REPO

PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
SECTIONS = ("flow", "dwell", "variants", "after_end", "open", "related", "code", "people", "cases")
VERDICTS = ("useful", "wrong", "not_useful")
STMT = {"type": "object", "additionalProperties": False, "required": ["text", "refs"],
        "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 600},
                       "refs": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8}}}


def canon(x) -> str:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(x) -> str:
    return hashlib.sha256((x if isinstance(x, str) else canon(x)).encode("utf-8")).hexdigest()


def llm_dir(p) -> Path:
    return p.root / "data" / "llm"


def prompt_body(p, name: str) -> str:
    """method/prompts/<name>.md 의 '---' 아래 본문. 인스턴스에 없으면 엔진의 것."""
    for base in (p.root, REPO):
        f = base / "method" / "prompts" / f"{name}.md"
        if f.exists():
            text = f.read_text(encoding="utf-8").replace("\r\n", "\n")
            return (text.split("\n---\n", 1)[1] if "\n---\n" in text else text).strip()
    raise FileNotFoundError(f"method/prompts/{name}.md 가 없다")


def _read_json(f: Path):
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _parse(ts):
    from ..common import parse_ts
    try:
        return parse_ts(ts) if ts else None
    except ValueError:
        return None
