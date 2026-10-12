"""관측 저장소 — 수집기와 ingest 가 L0 에 일괄로 덧붙이는 경로. (사람 사건 한 줄은 record.py)

- 쓰기 전에 모든 사건을 check_observation 으로 검증한다. 하나라도 어긋나면 그 사건은 쓰지 않고 보고한다.
- obs_id 가 이미 있으면 건너뛴다 — 재수집해도 같은 사건이 두 번 들어가지 않는다(수집기는 결정론 id 를 만든다).
- 월 파일(data/observations/YYYY-MM.jsonl)에 덧붙이기만 한다. 기존 줄은 건드리지 않는다.
- 발췌(본문 일부)는 data/excerpts.jsonl 캐시에 따로 둔다 — L0 에는 본문이 들어가지 않는다.
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

from .common import (
    HUMAN_SYSTEMS, Codebook, Paths, check_observation, dumps, observation_validator, read_jsonl, write_text,
)


def existing_ids(p: Paths) -> set[str]:
    ids: set[str] = set()
    if p.observations.exists():
        for path in sorted(p.observations.glob("*.jsonl")):
            ids.update(o["obs_id"] for o in read_jsonl(path))
    return ids


def _append_lines(path: Path, lines: list[str]) -> None:
    """여러 줄을 O_APPEND 로 덧붙인다. 기존 바이트는 건드리지 않는다."""
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(lines).encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def append_observations(p: Paths, events: Iterable[dict], excerpts: dict[str, str] | None = None,
                        dry_run: bool = False) -> dict:
    """검증 → 중복 제거 → 월별 덧붙이기. 집계 보고를 돌려준다(쓰지 않은 이유 포함).
    사람·사용 사건(atlas·self_report)은 받지 않는다 — 그 쓰기 경로는 `atlas record` 하나다(트리아지 없는 판정 거부 등)."""
    validator = observation_validator(p)
    codebook = Codebook.load(p)
    seen = existing_ids(p)
    by_month: dict[str, list[str]] = defaultdict(list)
    report = {"received": 0, "written": 0, "duplicate": 0, "invalid": 0, "partial": 0,
              "by_kind": Counter(), "problems": []}
    written_ids: list[str] = []
    for ev in events:
        report["received"] += 1
        problems = check_observation(ev, validator, codebook)
        if not problems and ev["source"]["system"] in HUMAN_SYSTEMS:
            problems = [f"{ev['source']['system']} 사건은 atlas record 로만 쓴다"]
        if problems:
            report["invalid"] += 1
            if len(report["problems"]) < 10:
                report["problems"].append(f"{ev.get('obs_id', '?')}: {'; '.join(problems)}")
            continue
        if ev["obs_id"] in seen:
            report["duplicate"] += 1
            continue
        seen.add(ev["obs_id"])
        by_month[ev["ts"][:7]].append(dumps(ev) + "\n")
        report["written"] += 1
        report["partial"] += bool(ev["partial"])
        report["by_kind"][ev["kind"]] += 1
        written_ids.append(ev["obs_id"])
    if not dry_run:
        for month, lines in sorted(by_month.items()):
            _append_lines(p.observations / f"{month}.jsonl", lines)
        if excerpts:
            have = {e["obs_id"] for e in read_jsonl(p.excerpts)} if p.excerpts.exists() else set()
            _append_lines(p.excerpts, [dumps({"obs_id": i, "text": excerpts[i]}) + "\n"
                                       for i in written_ids if i in excerpts and i not in have and excerpts[i]])
    report["by_kind"] = dict(sorted(report["by_kind"].items()))
    return report


def format_report(name: str, report: dict, dry_run: bool = False) -> str:
    kinds = ", ".join(f"{k} {n}" for k, n in report["by_kind"].items()) or "-"
    head = (f"[{name}] 받음 {report['received']} · {'쓸 것' if dry_run else '씀'} {report['written']}"
            f" · 중복 {report['duplicate']} · 계약 위반 {report['invalid']} · 부분 수집 {report['partial']}")
    lines = [head, f"  종류: {kinds}"]
    lines += [f"  위반: {x}" for x in report["problems"]]
    return "\n".join(lines)


# ---------------------------------------------------------------- 증분 상태

def read_state(p: Paths, name: str) -> dict:
    path = p.state / f"{name}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_state(p: Paths, name: str, state: dict) -> None:
    write_text(p.state / f"{name}.json", json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
