#!/usr/bin/env python3
"""codebook.yaml → annotation.schema.json 생성기.

코드북이 패싯 값·서열·어휘의 유일한 정본이다. LLM 주석 출력 스키마는 여기서 생성하고 손으로 고치지 않는다.

사용: python3 method/recipes/gen_annotation_schema.py [--check]
  --check : 생성 결과가 디스크의 파일과 다르면 종료 코드 1 (커밋 전 검사에서 드리프트 감지용)

스키마는 조건부 키워드(if/then, anyOf 등)를 쓰지 않는다. 구조화 출력이 지원하는 키워드 범위가
런타임마다 다를 수 있어서다(1단계에서 실측). "unknown 이 아니면 evidence 1개 이상",
"evidence 는 그 WU 의 구성 관측" 같은 조건부 규칙은 사후 검증 레시피가 집행한다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml  # PyYAML

METHOD_DIR = Path(__file__).resolve().parent.parent
CODEBOOK = METHOD_DIR / "codebook.yaml"
OUT = METHOD_DIR / "annotation.schema.json"


def ids(items: list[dict] | None) -> list[str]:
    return [i["id"] for i in (items or []) if i.get("status", "active") == "active"]


def facet_value_schema(facet: dict, reserved: list[str]) -> dict:
    values = ids(facet.get("values"))
    # 값이 아직 도출되지 않은 패싯(예: domain)은 자유 문자열. 도출 후 enum 으로 닫힌다.
    item = {"type": "string", "enum": values + reserved} if values else {"type": "string"}
    return {"type": "array", "items": item} if facet.get("multi") else item


def vocab_claim(vocab_ids: list[str], reserved: list[str], what: str) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "note", "confidence", "evidence"],
        "properties": {
            "id": {"type": "string", "enum": vocab_ids + reserved},
            "note": {"type": "string", "description": f"residual 이면 새 {what} 후보를 한 줄로. 아니면 이 WU 에서의 구체 내용"},
            "confidence": {"$ref": "#/$defs/confidence"},
            "evidence": {"$ref": "#/$defs/evidence"},
        },
    }


def build(cb: dict) -> dict:
    reserved = ids(cb.get("reserved_values"))
    confidence = ids(cb.get("confidence_scale"))
    fit = ids(cb.get("fit_scale"))
    vocab = cb.get("vocabularies", {}) or {}
    facets = cb["facets"]

    facet_props = {
        f["id"]: {
            "type": "object",
            "additionalProperties": False,
            "required": ["value", "confidence", "evidence", "candidate_note"],
            "properties": {
                "value": facet_value_schema(f, reserved),
                "confidence": {"$ref": "#/$defs/confidence"},
                "evidence": {"$ref": "#/$defs/evidence"},
                "candidate_note": {"type": "string", "description": "residual 이면 가장 가까운 값과 이유. 아니면 빈 문자열"},
            },
            "description": f.get("question", ""),
        }
        for f in facets
    }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$comment": (
            f"GENERATED from codebook.yaml version {cb['version']} by method/recipes/gen_annotation_schema.py "
            "— 손으로 고치지 않는다. 코드북을 고치고 다시 생성한다."
        ),
        "title": "WorkUnitAnnotation",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "wu_id", "codebook_version", "title", "summary", "facets",
            "archetype", "hidden_subtasks", "pitfalls", "moves", "open_questions",
        ],
        "properties": {
            "wu_id": {"type": "string"},
            "codebook_version": {"const": cb["version"]},
            "title": {"$ref": "#/$defs/text_claim"},
            "summary": {"$ref": "#/$defs/text_claim"},
            "facets": {
                "type": "object",
                "additionalProperties": False,
                "required": [f["id"] for f in facets],
                "properties": facet_props,
            },
            "archetype": {
                "type": "object",
                "additionalProperties": False,
                "required": ["value", "fit", "evidence", "candidate_note"],
                "properties": {
                    "value": {"type": "string", "enum": ids(cb.get("archetypes")) + reserved},
                    "fit": {"type": "string", "enum": fit},
                    "evidence": {"$ref": "#/$defs/evidence"},
                    "candidate_note": {"type": "string", "description": "residual 이면 새 원형 후보 한 줄. 아니면 빈 문자열"},
                },
            },
            "hidden_subtasks": {"type": "array", "items": vocab_claim(ids(vocab.get("subtasks")), reserved, "하위작업")},
            "pitfalls": {"type": "array", "items": vocab_claim(ids(vocab.get("pitfalls")), reserved, "함정")},
            "moves": {
                "type": "array",
                "items": vocab_claim(ids(vocab.get("moves")), reserved, "처리 수"),
                "description": "진행·해결에서 실제로 취한 결정적 행동. 관측된 행동만 — 의도·바람직한 절차를 추정하지 않는다",
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "관측만으로 판단하지 못한 것. 사람 확인 후보가 된다",
            },
        },
        "$defs": {
            "confidence": {"type": "string", "enum": confidence, "description": "근거 서열. 정의는 codebook.yaml confidence_scale"},
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "근거 관측 obs_id 목록. 근거가 없으면 값은 unknown 이어야 한다",
            },
            "text_claim": {
                "type": "object",
                "additionalProperties": False,
                "required": ["value", "confidence", "evidence"],
                "properties": {
                    "value": {"type": "string"},
                    "confidence": {"$ref": "#/$defs/confidence"},
                    "evidence": {"$ref": "#/$defs/evidence"},
                },
            },
        },
    }


def render(cb: dict) -> str:
    return json.dumps(build(cb), ensure_ascii=False, indent=2) + "\n"


def check() -> bool:
    """디스크의 스키마가 코드북에서 생성한 결과와 같은지. 다른 검사 스크립트가 재사용한다."""
    cb = yaml.safe_load(CODEBOOK.read_text(encoding="utf-8"))
    return OUT.exists() and OUT.read_text(encoding="utf-8") == render(cb)


def main() -> int:
    if "--check" in sys.argv:
        if not check():
            print("annotation.schema.json 이 codebook.yaml 과 어긋난다. 생성기를 다시 돌려라.", file=sys.stderr)
            return 1
        return 0
    cb = yaml.safe_load(CODEBOOK.read_text(encoding="utf-8"))
    OUT.write_text(render(cb), encoding="utf-8")
    print(f"wrote {OUT.relative_to(METHOD_DIR.parent)} (codebook v{cb['version']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
