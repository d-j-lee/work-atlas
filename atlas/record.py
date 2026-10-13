#!/usr/bin/env python3
"""사람·사용 사건을 L0에 남기는 유일한 쓰기 경로 (대화 세션·사용 스킬 전용).

  triage       트리아지 때의 예측        → atlas.triage
  feedback     사람의 판정과 도움 여부    → atlas.feedback
  self-report  기록 없이 한 일 한 줄       → self_report.untracked_work
  llm-feedback LLM 출력(카테고리 해설)에 대한 판정 → atlas.llm_feedback ('틀림'이면 다음 빌드부터 그 해설을 숨긴다)

예:
  python -m atlas record triage --anchor issue:ABC-130 --archetype ops-data-fix --conf medium \\
          --scale small --pitfall missing-rollback-script --move snapshot-repro
  python -m atlas record feedback --anchor issue:ABC-130 --archetype-ok yes --verdict helpful
  python -m atlas record self-report --text "운영 요청으로 로그 조회 30분"

위 예의 id 는 설명용이다 — 실제로는 코드북에 있는 활성 id 를 쓴다(원형·어휘는 residual 허용).
앵커는 링크가 아니라 '<type>:<id>' 다 (예: 이슈 링크 …/browse/ABC-130 → issue:ABC-130).
판정(feedback)은 같은 앵커의 트리아지가 먼저 있어야 한다 — 채점할 예측이 없는 판정은 쓰지 않는다.
어긋나면 쓰지 않고 종료 코드 2. 쓰기 전에 method/observation.schema.json 과 교차 규칙으로 검증한다.
기존 줄은 절대 건드리지 않는다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .common import (
    RESERVED, Codebook, Paths, append_jsonl, check_observation, dumps, now_iso,
    object_ref, observation_validator, paths, read_observations, sha256_text,
)

COLLECTOR = "record_event@1"
ANCHOR = re.compile(r"^(?P<type>[a-z_]+):(?P<id>[^\s/][^\s]*)$")


class RecordError(ValueError):
    pass


def parse_anchor(anchor: str) -> dict:
    if re.match(r"^[a-z]+://", anchor):
        raise RecordError(f"'{anchor}' 는 링크다. 앵커는 '<type>:<id>' 로 넘긴다 (예: …/browse/ABC-123 → issue:ABC-123)")
    m = ANCHOR.match(anchor)
    if not m:
        raise RecordError(f"앵커 '{anchor}' 는 '<type>:<id>' 형식이어야 한다 (예: issue:ABC-123)")
    return {"type": m["type"], "id": m["id"]}


def _base(kind: str, objects: list[dict], payload: dict, ts: str) -> dict:
    system = kind.split(".", 1)[0]
    digest = sha256_text(dumps({"kind": kind, "objects": objects, "payload": payload}))[7:19]
    return {
        "obs_id": f"{kind}:{ts}:{digest}",
        "source": {"system": system, "instance": "work-atlas", "collector_kind": "human"},
        "kind": kind,
        "ts": ts,
        "actor_role": "self",
        "objects": objects,
        "keys": {},
        "metrics": {},
        "payload": payload,
        "partial": False,
        "collector": COLLECTOR,
        "collected_at": ts,
    }


def _check_ids(label: str, values: list[str], allowed: set[str]) -> None:
    bad = [v for v in values if v not in allowed]
    if bad:
        raise RecordError(f"{label} {bad} 는 코드북에 없는 id 다. 허용: {sorted(allowed)}")


def build_triage(cb: Codebook, anchor: str, archetype: str, conf: str, scale: str,
                 pitfalls: list[str], moves: list[str], generation: str | None, ts: str) -> dict:
    if not cb.archetypes() and archetype not in RESERVED:
        raise RecordError("코드북에 원형이 아직 없다(BOOTSTRAP A3 도출 전). 지금은 --archetype residual 또는 unknown 만 쓸 수 있다")
    _check_ids("원형", [archetype], cb.archetypes() | set(RESERVED))
    _check_ids("신뢰도", [conf], cb.confidence())
    _check_ids("규모", [scale], cb.facet_values("scale") | set(RESERVED))
    _check_ids("함정", pitfalls, cb.vocabulary("pitfalls") | {"residual"})
    _check_ids("처리 수", moves, cb.vocabulary("moves") | {"residual"})
    payload = {
        "use": "U1",
        "codebook_version": cb.version,
        "generation": generation,
        "pred": {
            "archetype": archetype, "archetype_conf": conf, "scale": scale,
            "pitfalls": sorted(set(pitfalls)), "moves_suggested": sorted(set(moves)),
        },
    }
    return _base("atlas.triage", [parse_anchor(anchor)], payload, ts)


def build_feedback(anchor: str, archetype_ok: str, verdict: str, ts: str) -> dict:
    payload = {"use": "U1", "archetype_ok": archetype_ok, "verdict": verdict}
    return _base("atlas.feedback", [parse_anchor(anchor)], payload, ts)


LLM_FEEDBACK_TASKS = ("category_card",)


def build_llm_feedback(task: str, key: str, digest: str, verdict: str, note: str, ts: str) -> dict:
    if task not in LLM_FEEDBACK_TASKS:
        raise RecordError(f"판정을 받는 LLM 작업은 {LLM_FEEDBACK_TASKS} 뿐이다")
    if not digest:
        raise RecordError(f"'{key}' 에 지금 보이는 {task} 출력이 없다 — 판정할 대상이 없다")
    note = " ".join(note.split())
    if len(note) > 200:
        raise RecordError("메모는 200자까지 (사람 이름은 쓰지 않는다)")
    payload = {"task": task, "key": key, "digest": digest, "verdict": verdict, "note": note}
    return _base("atlas.llm_feedback", [{"type": "llm_output", "id": f"{task}:{key}"}], payload, ts)


def build_self_report(text: str, ts: str) -> dict:
    text = text.strip()
    if not text:
        raise RecordError("자기 보고 내용이 비어 있다")
    return _base("self_report.untracked_work", [], {"text": text}, ts)


def has_triage(p: Paths, anchor: str) -> bool:
    return any(o["kind"] == "atlas.triage" and any(object_ref(x) == anchor for x in o["objects"])
               for o in read_observations(p.observations))


def preflight(event: dict, p: Paths, cb: Codebook) -> None:
    """쓰기 전 검사 — 실제 기록과 --dry-run 이 같은 기준을 쓴다."""
    problems = check_observation(event, observation_validator(p), cb)
    if problems:
        raise RecordError("관측 계약 위반: " + "; ".join(problems))
    if event["kind"] == "atlas.feedback":
        anchor = object_ref(event["objects"][0])
        if not has_triage(p, anchor):
            raise RecordError(f"'{anchor}' 의 트리아지 기록이 없다. 판정은 예측이 있어야 채점된다 — 먼저 triage 를 남겨라")


def record(event: dict, p: Paths, cb: Codebook) -> Path:
    preflight(event, p, cb)
    target = p.observations / f"{event['ts'][:7]}.jsonl"
    append_jsonl(target, event)
    return target


def current_generation(p: Paths) -> str | None:
    """예측이 기댄 지식의 세대 — 마이닝 모델(data/model/generation.json), 없으면 카드 매니페스트(out/)."""
    for f in (p.model / "generation.json", p.manifest):
        try:
            gen = json.loads(f.read_text(encoding="utf-8")).get("generation")
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if gen:
            return gen
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas record", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="work-atlas 인스턴스 루트 (기본: ATLAS_HOME → 현재 폴더에서 위로 찾기)")
    ap.add_argument("--dry-run", action="store_true", help="쓰지 않고 만들 사건만 출력")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("triage")
    t.add_argument("--anchor", required=True)
    t.add_argument("--archetype", required=True)
    t.add_argument("--conf", required=True, help="high · medium · low")
    t.add_argument("--scale", required=True)
    t.add_argument("--pitfall", action="append", default=[])
    t.add_argument("--move", action="append", default=[])

    f = sub.add_parser("feedback")
    f.add_argument("--anchor", required=True)
    f.add_argument("--archetype-ok", required=True, choices=["yes", "no", "unsure"])
    f.add_argument("--verdict", required=True, choices=["helpful", "not_helpful", "unknown"])

    s = sub.add_parser("self-report")
    s.add_argument("--text", required=True)

    lf = sub.add_parser("llm-feedback")
    lf.add_argument("--task", default="category_card", choices=["category_card"])
    lf.add_argument("--key", required=True, help="카테고리 키 · 이름 (atlas guide --list)")
    lf.add_argument("--verdict", required=True, choices=["useful", "wrong", "not_useful"])
    lf.add_argument("--note", default="")

    args = ap.parse_args(argv)
    p = paths(args.root)
    cb = Codebook.load(p)
    ts = now_iso()
    try:
        if args.cmd == "triage":
            event = build_triage(cb, args.anchor, args.archetype, args.conf, args.scale,
                                 args.pitfall, args.move, current_generation(p), ts)
        elif args.cmd == "feedback":
            event = build_feedback(args.anchor, args.archetype_ok, args.verdict, ts)
        elif args.cmd == "llm-feedback":
            from . import catview, guide, llm
            m, why = catview.load(p)
            if not m:
                raise RecordError(why)
            key = guide.resolve(m, args.key) or args.key
            card = llm.rendered_cards(p, m).get(key) or {}
            event = build_llm_feedback(args.task, key, card.get("digest", ""), args.verdict, args.note, ts)
        else:
            event = build_self_report(args.text, ts)
        if args.dry_run:
            print(dumps(event))
            preflight(event, p, cb)
            return 0
        target = record(event, p, cb)
    except RecordError as e:
        print(f"[기록 안 함] {e}", file=sys.stderr)
        return 2
    print(f"recorded {event['obs_id']} → {target.relative_to(p.root).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
