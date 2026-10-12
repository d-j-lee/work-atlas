"""핫스팟 — 결함 수정이 몰려 온 파일. "여기를 고칠 땐 더 조심하라"의 근거.

점수 (Graves et al. 2000 의 가중 시간 감쇠를 결함 수정에 적용, Kim et al. 2007 의 '결함은 몰려서 온다'):
  fix_damp    = Σ_결함수정커밋 0.5 ^ (경과일 / half_life_days)
  change_damp = Σ_모든커밋   0.5 ^ (경과일 / half_life_days)
  순위 = mining.hotspot_rank (기본 fixes: 결함 수정 커밋 수, 같으면 fix_damp). 커밋 수·변경 줄 수(churn)도 건수로 둔다.
  기본값은 실데이터 평가로 정했다(EVALUATION.md): 시간 감쇠는 단순 건수보다 낫지 않았고, 상위 20개 정밀도는 결함 수정 수가 가장 높았다.
- 대량 커밋·장부 파일은 뺀다(corpus). 테스트 파일은 순위에서 뺀다(운영 결함의 자리가 아니다).
- 강도: 결함 수정이 있는 파일 중 상위 5% = high, 상위 15% = medium, 결함 수정 3건 이상인 나머지 = low.
  그 밖의 파일은 주장으로 내지 않는다(조용한 파일은 말할 것이 없다).
"""
from __future__ import annotations

import math
from collections import defaultdict

from . import claims as C

RECIPE = "hotspot@1"


def scores(corpus, before=None) -> dict[tuple, dict]:
    """(범위, 경로) → 통계. before 가 있으면 그 시각 이전 커밋만(평가용)."""
    half = float(corpus.cfg["mining"]["half_life_days"])
    ref = before or corpus.as_of
    out: dict[tuple, dict] = defaultdict(lambda: {"changes": 0, "fixes": 0, "churn": 0, "fix_damp": 0.0,
                                                   "change_damp": 0.0, "first": None, "last": None, "ev": []})
    for c in corpus.commits:
        if c.large or (before and c.ts >= before):
            continue
        w = math.pow(0.5, max((ref - c.ts).total_seconds(), 0) / 86400 / half)
        for p in c.paths:
            if corpus.is_test(p):
                continue
            s = out[(c.scope, p)]
            s["changes"] += 1
            s["churn"] += c.churn
            s["change_damp"] += w
            s["first"] = s["first"] or c.ts
            s["last"] = c.ts
            if c.is_fix:
                s["fixes"] += 1
                s["fix_damp"] += w
                s["ev"].append(c.obs_id)
    return out


def ranked(stats: dict[tuple, dict], key: str = "fixes") -> list[tuple]:
    second = "fix_damp" if key != "fix_damp" else "change_damp"
    return sorted(stats, key=lambda k: (-stats[k][key], -stats[k][second], k))


def mine(corpus) -> list[dict]:
    stats = scores(corpus)
    out = []
    by_scope: dict[str, list] = defaultdict(list)
    for k in ranked(stats, corpus.cfg["mining"].get("hotspot_rank", "fixes")):
        if stats[k]["fixes"]:
            by_scope[k[0]].append(k)
    for scope, keys in sorted(by_scope.items()):
        n = len(keys)
        for i, k in enumerate(keys):
            s = stats[k]
            pct = (i + 1) / n
            strength = "high" if pct <= 0.05 else "medium" if pct <= 0.15 else "low" if s["fixes"] >= 3 else None
            if not strength:
                continue
            out.append(C.make(
                "hotspot", scope, {"type": "path", "id": k[1]},
                {"rank": i + 1, "of": n, "fix_damp": round(s["fix_damp"], 3), "change_damp": round(s["change_damp"], 3),
                 "fixes": s["fixes"], "changes": s["changes"], "churn": s["churn"]},
                strength, s["ev"], [s["first"], s["last"]], RECIPE, n_evidence=s["fixes"]))
    return out
