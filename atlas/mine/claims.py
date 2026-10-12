"""주장(claim) — 마이닝이 내는 모든 지식의 공통 형식. 소비(맥락·훅·리포트·MCP)는 이것만 읽는다.

{ "claim_id": "<kind>:<scope>:<subject>[=><object>]",   결정론 — 세대가 바뀌어도 같은 주장은 같은 id
  "kind": "cochange | hotspot | process | practice | moves",
  "scope": 저장소·트래커 이름,
  "subject": {"type", "id"}, "object": {"type", "id"} | 없음,
  "stat": 종류별 수치(건수 포함),
  "strength": "high | medium | low",   근거 서열 — 숫자 대신 정의된 서열 (종류별 정의는 각 마이너)
  "evidence": 최근 근거 obs_id·wu_id 최대 5개, "n_evidence": 전체 근거 수,
  "first_ts", "last_ts": 근거의 시간 범위 — 신선도,
  "recipe": "<마이너>@<버전>" }
요약을 다시 요약하지 않는다: 주장은 언제나 L0·L1 에서 다시 센다(P2·N3).
"""
from __future__ import annotations

import datetime as dt

STRENGTHS = ("high", "medium", "low")
RANK = {"high": 3, "medium": 2, "low": 1}


def make(kind: str, scope: str, subject: dict, stat: dict, strength: str, evidence: list[str],
         times: list[dt.datetime], recipe: str, obj: dict | None = None, n_evidence: int | None = None) -> dict:
    assert strength in STRENGTHS
    cid = f"{kind}:{scope}:{subject['id']}" + (f"=>{obj['id']}" if obj else "")
    c = {
        "claim_id": cid, "kind": kind, "scope": scope, "subject": subject, "stat": stat,
        "strength": strength, "evidence": evidence[-5:], "n_evidence": n_evidence if n_evidence is not None else len(evidence),
        "first_ts": min(times).isoformat(timespec="seconds") if times else None,
        "last_ts": max(times).isoformat(timespec="seconds") if times else None,
        "recipe": recipe,
    }
    if obj:
        c["object"] = obj
    return c


def by_support(n: int, high: int, medium: int) -> str | None:
    return "high" if n >= high else "medium" if n >= medium else "low" if n > 0 else None


def drift(prev: list[dict], cur: list[dict], top: int = 20) -> dict:
    """세대 간 변화 — 새로 생김 · 사라짐 · 강해짐 · 약해짐, 그리고 종류별 상위 집합의 겹침(Jaccard)."""
    a = {c["claim_id"]: c for c in prev}
    b = {c["claim_id"]: c for c in cur}
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    up = sorted(i for i in set(a) & set(b) if RANK[b[i]["strength"]] > RANK[a[i]["strength"]])
    down = sorted(i for i in set(a) & set(b) if RANK[b[i]["strength"]] < RANK[a[i]["strength"]])
    overlap = {}
    for kind in sorted({c["kind"] for c in cur} | {c["kind"] for c in prev}):
        ta = {c["claim_id"] for c in sorted((c for c in prev if c["kind"] == kind), key=_order)[:top]}
        tb = {c["claim_id"] for c in sorted((c for c in cur if c["kind"] == kind), key=_order)[:top]}
        overlap[kind] = round(len(ta & tb) / len(ta | tb), 3) if ta | tb else None
    return {"added": len(added), "removed": len(removed), "strengthened": len(up), "weakened": len(down),
            "top_overlap": overlap, "examples": {"added": added[:10], "removed": removed[:10],
                                                  "strengthened": up[:10], "weakened": down[:10]}}


def _order(c: dict):
    return (-RANK[c["strength"]], -c["n_evidence"], c["claim_id"])
