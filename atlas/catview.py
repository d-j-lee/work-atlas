"""카테고리 모델을 읽는 쪽의 공통 — 업무 지도 · 안내서 · 맥락 · 훅 · MCP · LLM 이 같은 말을 하게.

  load(p)            data/model/categories.json (없거나 옛 형식이면 None 과 이유)
  for_wu(m, wu)      그 작업단위가 든 카테고리 — 가장 구체적인 것부터
  best(m, keys)      맥락에 쓸 하나 — 흐름이 있는 가장 구체적인 칸(영역 × 유형 → 영역 → 상위)
  next_steps(e, s)   지금 단계 s 다음에 보통 오는 것 — 그 단계에서 나간 이음 횟수 중 비율 (합 100%)
  people(e)          접점 줄 — 사람이 붙인 것(팀·채널·역할) 먼저, 데이터에서 본 역할(처음 등장 단계)
  summary(e)         한 줄 요약 — 건수와 함께
수치는 모델에 있는 그대로 쓴다(여기서 새로 계산하지 않는다).
"""
from __future__ import annotations

import json

from .diagram import dur, pct, role_label

FORMAT = 1


def load(p) -> tuple[dict | None, str | None]:
    f = p.model / "categories.json"
    if not f.exists():
        return None, "카테고리 모델이 없다 → atlas build"
    try:
        m = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, "카테고리 모델을 읽지 못했다 → atlas build"
    if m.get("format") != FORMAT or "categories" not in m:
        return None, "카테고리 모델이 옛 형식이다 → atlas build"
    return m, None


def for_wu(m: dict, wu: str) -> list[str]:
    keys = m.get("members", {}).get(wu, [])
    cats = m["categories"]
    out = set()
    for k in keys:
        e = cats.get(k)
        while e:
            out.add(e["key"])
            e = cats.get(e["parent"]) if e.get("parent") else None
    return sorted(out, key=lambda k: (-depth(cats[k]), cats[k]["kind"] != "facet", -cats[k]["profile"]["n"], k))


def depth(e: dict) -> int:
    return len(e.get("chain", [])) + (1 if e.get("kind") == "facet" else 0)


def best(m: dict, keys: list[str]) -> dict | None:
    cats = m["categories"]
    for k in keys:
        e = cats.get(k)
        if e and e.get("flow") and e["key"] != "(전체)" and not e["key"].startswith("(미분류)"):
            return e
    return None


def next_steps(e: dict, stage: str, k: int = 3) -> list[dict]:
    """그 단계 다음에 온 활동의 비율 — 그림에 남은 나가는 이음의 횟수 합 중 (합이 100% 가 되게; 고리면 ↺)."""
    fl = e.get("flow") or {}
    outs = [x for x in fl.get("edges", []) if x["from"] == stage]
    total = sum(x["times"] for x in outs)
    if not total:
        return []
    outs.sort(key=lambda x: (-x["times"], x["to"]))
    return [{"to": x["to"], "times": x["times"], "of": total, "share": round(x["times"] / total, 3),
             "gap_h_p50": x.get("gap_h_p50"), "loop": x.get("loop", False)} for x in outs[:k]]


def people(e: dict, k: int = 6, informative: bool = False) -> list[str]:
    """informative=True 면 역할을 모르는 사람·봇은 뺀다(맥락처럼 짧아야 하는 곳 — 정보가 없다)."""
    out = []
    for c in (e.get("contacts") or {}).get("configured", []):
        who = " ".join(x for x in (c.get("team"), c.get("channel"), role_label(c["role"]) if c.get("role") else None) if x)
        if c.get("when"):
            who += f" ({c['when']})"
        out.append(who)
    for r in (e.get("contacts") or {}).get("observed", []):
        if r["role"] in ("unknown", "bot") and (informative or len(out) >= 2):
            continue
        out.append(f"{role_label(r['role'])} · {r['first_stage']}에서 처음 ({pct(r['share'])})")
    return out[:k]


def flow_line(e: dict) -> str:
    fl = e.get("flow") or {}
    return " → ".join(fl.get("main", []))


def summary(e: dict) -> str:
    pr = e["profile"]
    bits = [f"{pr['n']:,}건" + (f"(열림 {pr['open_now']:,})" if pr.get("open_now") else "")]
    if pr.get("duration_h_p50") is not None:
        bits.append(f"소요 중앙 {dur(pr['duration_h_p50'])}·P90 {dur(pr['duration_h_p90'])} (끝난 {pr['ended']:,}건)")
    if pr.get("problem_rate") is not None:
        bits.append(f"사후 문제 {pct(pr['problem_rate'])} (확정 {pr['settled']:,}건)")
    if e.get("flow"):
        bits.append(f"흐름 {flow_line(e)}")
    return f"{e['name']} — " + " · ".join(bits)


def bottleneck(e: dict) -> dict | None:
    """머문 시간이 가장 긴 단계 — 표본 3건 이상인 것만."""
    rows = [r for r in e.get("dwell", []) if r["n"] >= 3 and r["hours_p50"] is not None]
    return max(rows, key=lambda r: (r["hours_p50"], r["stage"])) if rows else None
