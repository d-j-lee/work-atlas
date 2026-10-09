#!/usr/bin/env python3
"""주석 사후 검증 — "근거 없는 값은 unknown" 을 스키마가 아니라 실행으로 집행한다.

규칙 (method/schema.md "사후 검증"):
  R1 근거 밖   evidence 는 그 WU members 의 obs_id 여야 한다. 밖의 것은 지운다. 중복 근거는 하나로 센다.
  R2 근거 없음  unknown 이 아닌 분류 값(패싯·원형·어휘)은 근거가 1개 이상이어야 한다.
              위반하면 패싯·원형은 unknown 으로 강등, 어휘 항목은 뺀다. 서술(제목·요약)은 값을 두고 신뢰도를 low 로.
  R3 과신      confidence: high 는 서로 다른 근거 2개 이상이어야 한다. 아니면 medium(근거 1개)·low(0개)로 낮춘다.
  R4 계약 밖   코드북에 없거나 휴면인 id, 이번 트랙에서 주석하지 않는 패싯(결정론 계산 패싯 포함)은 강등하거나 뺀다.

집계: 값 하나에 규칙이 여러 개 걸려도 위반 값은 1개로 센다(violation_rate = 위반 값 / 검사한 값 ≤ 1).
     규칙별 건수(by_rule)는 따로 센다. 코드북 버전이 다른 WU 수는 위반이 아니라 경고로 센다.

사용: python3 validate.py [--in data/units/annotated.jsonl] [--out data/units/current.jsonl] [--report r.json]
입력은 바꾸지 않는다. 채점·유사 사례·카드·사용 스킬은 --out(기본 current.jsonl)만 읽는다.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import RESERVED, Codebook, is_unknown, paths, read_jsonl, write_jsonl, write_text  # noqa: E402

VOCABS = {"pitfalls": "pitfalls", "moves": "moves", "hidden_subtasks": "subtasks"}


class _Tally:
    """값 단위 위반 집계. 한 값에 여러 규칙이 걸려도 위반 값은 하나다."""

    def __init__(self) -> None:
        self.by_rule: Counter = Counter()
        self.by_field: Counter = Counter()
        self.values = 0
        self.violated = 0

    def check(self) -> set[str]:
        self.values += 1
        return set()

    def close(self, hits: set[str], field: str) -> None:
        if hits:
            self.violated += 1
            for rule in hits:
                self.by_rule[rule] += 1
                self.by_field[f"{rule} {field}"] += 1


def _support(claim: dict, members: set[str], hits: set[str]) -> list[str]:
    evidence = list(dict.fromkeys(claim.get("evidence", [])))     # 순서 유지 중복 제거
    inside = [e for e in evidence if e in members]
    if len(inside) < len(evidence):
        hits.add("R1")
    claim["evidence"] = inside
    return inside


def _cap(claim: dict, n_evidence: int, hits: set[str]) -> None:
    if claim.get("confidence") == "high" and n_evidence < 2:
        hits.add("R3")
        claim["confidence"] = "medium" if n_evidence == 1 else "low"


def _unknown_like(value):
    return ["unknown"] if isinstance(value, list) else "unknown"


def validate_unit(unit: dict, cb: Codebook, tally: _Tally | None = None) -> dict:
    """검증된 사본을 돌려준다. 입력은 바꾸지 않는다."""
    tally = tally or _Tally()
    out = copy.deepcopy(unit)
    ann = out.get("annotation")
    if not ann:
        return out
    members = {m["obs_id"] for m in out.get("members", [])}
    allowed_facets = {f["id"] for f in cb.annotated_facets()}

    facets = ann.get("facets") or {}
    for fid in list(facets):
        claim, hits = facets[fid], tally.check()
        field = f"facets.{fid}"
        if fid not in allowed_facets:
            hits.add("R4")
            del facets[fid]
            tally.close(hits, field)
            continue
        ev = _support(claim, members, hits)
        value = claim.get("value")
        values = value if isinstance(value, list) else [value]
        if cb.facet_values(fid) and any(v not in cb.facet_values(fid) | set(RESERVED) for v in values):
            hits.add("R4")
            claim["value"], claim["confidence"] = _unknown_like(value), "low"
        elif not is_unknown(value) and not ev:
            hits.add("R2")
            claim["value"], claim["confidence"] = _unknown_like(value), "low"
        else:
            _cap(claim, len(ev), hits)
        tally.close(hits, field)

    arch = ann.get("archetype")
    if arch:
        hits = tally.check()
        ev = _support(arch, members, hits)
        if arch.get("value") not in cb.archetypes() | set(RESERVED):
            hits.add("R4")
            arch["value"], arch["fit"] = "unknown", "weak"
        elif not is_unknown(arch.get("value")) and not ev:
            hits.add("R2")
            arch["value"], arch["fit"] = "unknown", "weak"
        tally.close(hits, "archetype")

    for key, vocab in VOCABS.items():
        if key not in ann:
            continue
        kept = []
        for item in ann[key] or []:
            hits = tally.check()
            ev = _support(item, members, hits)
            if item.get("id") not in cb.vocabulary(vocab) | {"residual"}:
                hits.add("R4")
            elif not ev:
                hits.add("R2")
            else:
                _cap(item, len(ev), hits)
                kept.append(item)
            tally.close(hits, key)
        ann[key] = kept

    for key in ("title", "summary"):
        claim = ann.get(key)
        if claim:
            hits = tally.check()
            ev = _support(claim, members, hits)
            if not ev:
                if claim.get("confidence") != "low":
                    hits.add("R2")
                claim["confidence"] = "low"
            else:
                _cap(claim, len(ev), hits)
            tally.close(hits, key)
    return out


def run(units: list[dict], cb: Codebook) -> tuple[list[dict], dict]:
    tally = _Tally()
    out = [validate_unit(u, cb, tally) for u in units]
    stale = sum(1 for u in units if u.get("annotation") and u["annotation"].get("codebook_version") != cb.version)
    report = {
        "units": len(units),
        "values_checked": tally.values,
        "values_violated": tally.violated,
        "violation_rate": round(tally.violated / tally.values, 4) if tally.values else None,
        "by_rule": dict(sorted(tally.by_rule.items())),
        "by_field": dict(sorted(tally.by_field.items())),
        "stale_codebook_units": stale,
    }
    return out, report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--in", dest="inp", help="기본: data/units/annotated.jsonl")
    ap.add_argument("--out", help="기본: data/units/current.jsonl")
    ap.add_argument("--report")
    args = ap.parse_args(argv)
    p = paths(args.root)
    cb = Codebook.load(p)
    units = list(read_jsonl(Path(args.inp) if args.inp else p.annotated))
    out, report = run(units, cb)
    write_jsonl(Path(args.out) if args.out else p.units, out)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        write_text(Path(args.report), text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
