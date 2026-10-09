#!/usr/bin/env python3
"""예측 채점 — 이 체계의 최상위 효용 지표 (METHODOLOGY.md §7.2, policy.md §7).

정답의 위계:
  원형      사람 판정(atlas.feedback archetype_ok)이 1차 정답. unsure 는 채점에서 뺀다.
            unknown·residual 예측은 "보류"로 따로 세고 적중률에 넣지 않는다(맞혀도 고른 것이 없다).
  일관성    WU 주석과의 일치는 '일관성'으로 따로 센다 — LLM 이 LLM 을 채점한 값이라 정답이 아니다.
            원형·규모는 WU 가 끝나면(span.end) 잠정, 함정·처리 수는 확정(settled) 뒤에만 센다.
  서열 보정 원형 예측 신뢰도(high·medium·low)별 사람 판정 적중률. 칸마다 10건 이상일 때만 역전을 판정한다 —
            그 전에는 건수만 보인다(작은 칸의 비율은 잡음이다).

트리아지는 앵커별 마지막 것, 판정도 앵커별 마지막 것을 쓴다. 앵커는 WU 의 anchors 와
구성 관측의 objects 로 WU 에 연결한다(대화 스레드에서 트리아지한 일이 나중에 이슈 WU 로 묶여도 찾는다).

사용: python3 score.py [--units data/units/current.jsonl] [--min-judged 20] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import object_ref, parse_ts, paths, read_jsonl, read_observations  # noqa: E402


MIN_BIN = 10
ABSTAIN = ("unknown", "residual")


def _rate(hit: int, n: int) -> dict:
    return {"n": n, "hit": hit, "rate": round(hit / n, 3) if n else None}


def _latest(events: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in sorted(events, key=lambda e: (parse_ts(e["ts"]), e["obs_id"])):
        for o in e["objects"]:
            out[object_ref(o)] = e
    return out


def index_units(units: list[dict], obs_by_id: dict[str, dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for u in units:
        for m in u.get("members", []):
            for o in obs_by_id.get(m["obs_id"], {}).get("objects", []):
                idx.setdefault(object_ref(o), u)
    for u in units:                      # 앵커가 구성 관측보다 우선한다
        for a in u.get("anchors", []):
            idx[a] = u
    return idx


def _ann_ids(unit: dict, key: str) -> set[str]:
    return {i["id"] for i in (unit.get("annotation") or {}).get(key) or [] if i.get("id") != "residual"}


def score(observations: list[dict], units: list[dict], min_judged: int = 20) -> dict:
    obs_by_id = {o["obs_id"]: o for o in observations}
    triages = _latest([o for o in observations if o["kind"] == "atlas.triage"])
    feedback = _latest([o for o in observations if o["kind"] == "atlas.feedback"])
    idx = index_units(units, obs_by_id)

    human = [0, 0]
    abstained = 0
    by_conf: dict[str, list[int]] = {}
    cons = {k: [0, 0] for k in ("archetype", "scale", "pitfalls", "moves")}
    helpful = [0, 0]

    for anchor, t in triages.items():
        pred = t["payload"]["pred"]
        fb = feedback.get(anchor)
        if fb and parse_ts(fb["ts"]) >= parse_ts(t["ts"]):     # 판정·도움 여부 모두 같은 규칙: 트리아지 뒤의 것만
            v = fb["payload"].get("verdict")
            if v in ("helpful", "not_helpful"):
                helpful[1] += 1
                helpful[0] += v == "helpful"
            ok = fb["payload"].get("archetype_ok")
            if ok in ("yes", "no") and pred["archetype"] in ABSTAIN:
                abstained += 1
            elif ok in ("yes", "no"):
                human[1] += 1
                human[0] += ok == "yes"
                c = by_conf.setdefault(pred["archetype_conf"], [0, 0])
                c[1] += 1
                c[0] += ok == "yes"
        unit = idx.get(anchor)
        ann = (unit or {}).get("annotation")
        if not unit or not ann:
            continue
        span = unit.get("span", {})
        if span.get("end"):
            a = ann.get("archetype", {}).get("value")
            if a not in (None, "unknown"):
                cons["archetype"][1] += 1
                cons["archetype"][0] += a == pred["archetype"]
            s = (ann.get("facets", {}).get("scale") or {}).get("value")
            if s not in (None, "unknown") and pred["scale"] not in ABSTAIN:
                cons["scale"][1] += 1
                cons["scale"][0] += s == pred["scale"]
        if span.get("settled"):
            for key, pkey in (("pitfalls", "pitfalls"), ("moves", "moves_suggested")):
                predicted = [i for i in pred.get(pkey, []) if i != "residual"]
                if predicted:
                    found = _ann_ids(unit, key)
                    cons[key][1] += len(predicted)
                    cons[key][0] += sum(i in found for i in predicted)

    calibration = {k: _rate(*by_conf[k]) for k in ("high", "medium", "low") if k in by_conf}
    hi, med = calibration.get("high"), calibration.get("medium")
    inverted = (None if not (hi and med and hi["n"] >= MIN_BIN and med["n"] >= MIN_BIN)
                else hi["rate"] < med["rate"])
    return {
        "triages": len(triages),
        "archetype_human": _rate(*human),
        "abstained": abstained,
        "calibration": calibration,
        "calibration_inverted": inverted,
        "consistency": {k: _rate(*v) for k, v in cons.items()},
        "helpful": _rate(*helpful),
        "ready_to_judge": human[1] >= min_judged,
        "min_judged": min_judged,
    }


def render_text(r: dict) -> str:
    def fmt(x: dict) -> str:
        return "-" if x["rate"] is None else f"{x['rate']:.0%} ({x['hit']}/{x['n']})"
    lines = [
        f"트리아지 {r['triages']}건 · 판정 {'가능' if r['ready_to_judge'] else '표본 부족'}"
        f" (사람 판정 {r['archetype_human']['n']}/{r['min_judged']})",
        f"원형 적중(사람 판정)  {fmt(r['archetype_human'])}",
        "서열 보정            " + (", ".join(f"{k} {fmt(v)}" for k, v in r["calibration"].items()) or "-")
        + {True: "  (!) 역전", False: "", None: f"  (칸마다 {MIN_BIN}건 전엔 판정 안 함)"}[r["calibration_inverted"]],
        f"보류 예측            {r['abstained']}건 (unknown·residual - 적중률에서 뺌)",
        "일관성(주석과)       " + ", ".join(f"{k} {fmt(v)}" for k, v in r["consistency"].items()),
        f"도움 됨              {fmt(r['helpful'])}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--units")
    ap.add_argument("--min-judged", type=int, default=20, help="판정에 필요한 사람 판정 수 (policy.md §7)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    p = paths(args.root)
    units_path = Path(args.units) if args.units else p.units
    units = list(read_jsonl(units_path)) if units_path.exists() else []
    report = score(read_observations(p.observations), units, args.min_judged)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
