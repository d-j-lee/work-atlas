#!/usr/bin/env python3
"""유사 사례 — 결정론 순위 (U1 트리아지 브리프의 "유사 사례 3건").

순위 키 (앞이 우선):
  1. 같은 원형인가
  2. 단서 겹침 수 — 영역(주석 domain) · 파일 경로 · 오류 시그니처(구성 관측의 keys)
  3. 최근에 끝난 일
확정(settled) WU 만 후보다 — 결과가 다 들어온 사례만 "어떻게 끝났나"를 보여줄 수 있다.
LLM 을 쓰지 않으므로 같은 입력에는 늘 같은 순서가 나온다. 영역 단서는 트랙 B 에서 domain 패싯이 켜지면 쓰인다.
--archetype 은 코드북의 활성 id(또는 unknown·residual)여야 한다. 아니면 종료 코드 2.

사용: python3 similar.py --archetype <id> [--domain d ...] [--path p ...] [--error e ...] [-k 3] [--json]
결과의 앵커는 data/ 에서 읽은 것이다. 카드·리포트처럼 git 에 남는 곳에 붙여 넣지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import RESERVED, Codebook, parse_ts, paths, read_jsonl, read_observations  # noqa: E402


def features(unit: dict, obs_by_id: dict[str, dict]) -> dict[str, set[str]]:
    keys: dict[str, set[str]] = {"path": set(), "error": set()}
    for m in unit.get("members", []):
        k = obs_by_id.get(m["obs_id"], {}).get("keys", {})
        keys["path"].update(k.get("paths", []))
        keys["error"].update(k.get("error_signatures", []))
    domain = ((unit.get("annotation") or {}).get("facets", {}).get("domain") or {}).get("value") or []
    keys["domain"] = {d for d in (domain if isinstance(domain, list) else [domain]) if d not in ("unknown", "residual")}
    return keys


def rank(query: dict, units: list[dict], obs_by_id: dict[str, dict], k: int = 3) -> list[dict]:
    scored = []
    for u in units:
        span = u.get("span", {})
        if not span.get("settled") or not span.get("end"):
            continue
        f = features(u, obs_by_id)
        archetype = ((u.get("annotation") or {}).get("archetype") or {}).get("value", "unknown")
        same = int(query.get("archetype") not in (None, "unknown", "residual") and archetype == query["archetype"])
        overlap = {name: sorted(f[name] & set(query.get(name, []))) for name in ("domain", "path", "error")}
        n_overlap = sum(len(v) for v in overlap.values())
        if not same and not n_overlap:
            continue
        scored.append(((same, n_overlap, parse_ts(span["end"]), u["wu_id"]), {
            "wu_id": u["wu_id"],
            "anchors": u.get("anchors", []),
            "archetype": archetype,
            "same_archetype": bool(same),
            "overlap": {name: v for name, v in overlap.items() if v},
            "ended": span["end"],
            "signals": sorted(u.get("stats", {}).get("deterministic_signals", [])),
        }))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:k]]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--units")
    ap.add_argument("--archetype")
    ap.add_argument("--domain", action="append", default=[])
    ap.add_argument("--path", action="append", default=[])
    ap.add_argument("--error", action="append", default=[])
    ap.add_argument("-k", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    p = paths(args.root)
    allowed = Codebook.load(p).archetypes() | set(RESERVED)
    if args.archetype and args.archetype not in allowed:
        print(f"[거부] 원형 '{args.archetype}' 는 코드북에 없는 id 다. 허용: {sorted(allowed)}", file=sys.stderr)
        return 2
    units_path = Path(args.units) if args.units else p.units
    units = list(read_jsonl(units_path)) if units_path.exists() else []
    obs_by_id = {o["obs_id"]: o for o in read_observations(p.observations)}
    query = {"archetype": args.archetype, "domain": args.domain, "path": args.path, "error": args.error}
    result = rank(query, units, obs_by_id, args.k)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for i, r in enumerate(result, 1):
            why = "같은 원형" if r["same_archetype"] else "단서 겹침"
            extra = "; ".join(f"{k}={','.join(v)}" for k, v in r["overlap"].items())
            print(f"{i}. {', '.join(r['anchors'])} · {r['archetype']} · {why}{' · ' + extra if extra else ''}"
                  f" · 끝남 {r['ended'][:10]} · 신호 {','.join(r['signals']) or '없음'}")
        if not result:
            print("유사 사례 없음 (확정 WU 중 같은 원형·겹치는 단서가 없다)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
