"""처리 과정의 통계 — "이런 일은 보통 얼마나 걸리고, 어떻게 끝났나" (기술통계만, 건수와 함께).

process   이슈 WU(트래커에 생성 사건이 있는 것)를 원천 라벨(유형·컴포넌트, 그리고 유형×컴포넌트)로 묶어:
          n · 소요 시간 P50·P90(시간) · 재오픈률 · 후속 수정률 · 롤백률 · 중단률 · 변경 수·파일 수 중앙값
          끝난 WU 만 소요를, 확정(settled) WU 만 사후 비율을 센다.
practice  관측된 실천과 결과의 **연관**(인과 아님): 실천이 있던 확정 WU 와 없던 확정 WU 의 문제율(재오픈·후속 수정·롤백).
          실천 = 테스트 파일도 바꿨나 · AI 세션이 있었나 · AI 세션에서 테스트를 돌렸나 · 커밋 3개 이상 · 파일 10개 이상
          양쪽 모두 5건 이상일 때만 낸다. 사후 결과를 관측할 수 있는 이슈 WU 만 센다.
moves     LLM 주석(트랙 A)에 처리 수가 있으면 원형×처리 수별 깔끔 종료 n/N · 후속 문제 m/N (METHODOLOGY.md §5.3).
강도: n(작은 쪽) ≥ 30 high · ≥ 10 medium · 그 밖 low. 표본이 작은 칸은 건수가 곧 경고다.
"""
from __future__ import annotations

from collections import defaultdict

from . import claims as C
from ..common import parse_ts

RECIPE_PROCESS = "process@1"
RECIPE_PRACTICE = "practice@1"
RECIPE_MOVES = "moves@1"
PROBLEM = {"reopened", "followup_fix", "rolled_back"}


def _q(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    i = (len(v) - 1) * q
    lo, hi = int(i), min(int(i) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (i - lo), 1)


def _observable(u: dict) -> bool:
    """사후 결과(재오픈·연결된 후속 수정)를 관측할 수 있는 WU — 트래커에 생성 사건이 있는 이슈 WU 만.
    MR·세션 WU 는 재오픈을 관측할 길이 없어 '문제 없음'이 '관측 못 함'과 섞인다."""
    return u["anchors"][0].startswith("issue:") and u["span"].get("end_basis") == "issue"


def _strength(n: int) -> str:
    return "high" if n >= 30 else "medium" if n >= 10 else "low"


def _rate(k: int, n: int) -> float | None:
    return round(k / n, 3) if n else None


def group_stats(us: list[dict]) -> dict:
    ended = [u for u in us if u["span"]["end"]]
    settled = [u for u in ended if u["span"]["settled"]]
    dur = [u["stats"]["duration_h"] for u in ended if u["stats"]["duration_h"] is not None]
    oc = lambda name: sum(1 for u in settled if u["stats"]["outcome"] == name)
    sig = lambda name: sum(1 for u in settled if name in u["stats"]["deterministic_signals"])
    return {
        "n": len(us), "ended": len(ended), "settled": len(settled),
        "duration_h_p50": _q(dur, 0.5), "duration_h_p90": _q(dur, 0.9),
        "reopen_rate": _rate(sig("reopened"), len(settled)),
        "followup_rate": _rate(sig("followup_fix"), len(settled)),
        "rollback_rate": _rate(sig("rolled_back"), len(settled)),
        "abandoned_rate": _rate(oc("abandoned"), len(settled)),
        "changes_p50": _q([u["stats"]["n_changes"] for u in ended], 0.5),
        "files_p50": _q([u["stats"]["n_files"] for u in ended], 0.5),
    }


def mine_process(units: list[dict], scope_of) -> list[dict]:
    groups: dict[tuple, list] = defaultdict(list)
    for u in units:
        if not _observable(u):
            continue
        labels = u["stats"].get("labels", {})
        scope = scope_of(u)
        types = labels.get("type", []) or ["(유형 없음)"]
        comps = labels.get("component", []) or ["(컴포넌트 없음)"]
        for t in types:
            groups[(scope, f"type={t}")].append(u)
            for c in comps:
                groups[(scope, f"type={t}|component={c}")].append(u)
        for c in comps:
            groups[(scope, f"component={c}")].append(u)
        groups[(scope, "all")].append(u)
    out = []
    for (scope, key), us in sorted(groups.items()):
        st = group_stats(us)
        if st["ended"] < 3:
            continue
        times = [parse_ts(u["span"]["start"]) for u in us]
        out.append(C.make("process", scope, {"type": "group", "id": key}, st, _strength(st["ended"]),
                          [u["wu_id"] for u in sorted(us, key=lambda u: u["span"]["start"])], times,
                          RECIPE_PROCESS, n_evidence=len(us)))
    return out


def practices(u: dict, corpus) -> dict[str, bool]:
    paths = set()
    for m in u["members"]:
        o = corpus.obs_by_id.get(m["obs_id"])
        if o and o["kind"] == "vcs.commit":
            paths.update(o["keys"].get("paths", []))
    s = u["stats"]
    return {
        "test_changed": any(corpus.is_test(p) for p in paths),
        "ai_assisted": s.get("ai_sessions", 0) > 0,
        "ai_ran_tests": s.get("ai_test_runs", 0) > 0,
        "multi_commit": s["n_changes"] >= 3,
        "wide_change": s["n_files"] >= 10,
    }


def mine_practice(units: list[dict], corpus, scope_of) -> list[dict]:
    rows = [(u, practices(u, corpus)) for u in units
            if _observable(u) and u["span"]["settled"] and u["stats"]["outcome"] not in (None, "abandoned")
            and u["stats"]["n_changes"] > 0]
    out = []
    for scope in sorted({scope_of(u) for u, _ in rows}):
        srows = [(u, f) for u, f in rows if scope_of(u) == scope]
        for feat in ("test_changed", "ai_assisted", "ai_ran_tests", "multi_commit", "wide_change"):
            yes = [u for u, f in srows if f[feat]]
            no = [u for u, f in srows if not f[feat]]
            if len(yes) < 5 or len(no) < 5:
                continue
            py = sum(1 for u in yes if u["stats"]["outcome"] in PROBLEM)
            pn = sum(1 for u in no if u["stats"]["outcome"] in PROBLEM)
            stat = {"with": {"n": len(yes), "problem": py, "rate": _rate(py, len(yes))},
                    "without": {"n": len(no), "problem": pn, "rate": _rate(pn, len(no))},
                    "diff": round(py / len(yes) - pn / len(no), 3), "reading": "연관 — 인과 아님"}
            times = [parse_ts(u["span"]["start"]) for u in yes + no]
            out.append(C.make("practice", scope, {"type": "practice", "id": feat}, stat,
                              _strength(min(len(yes), len(no))), [u["wu_id"] for u in yes], times,
                              RECIPE_PRACTICE, n_evidence=len(yes) + len(no)))
    return out


def mine_moves(units: list[dict], scope_of) -> list[dict]:
    """주석된 처리 수별 결과 — 원형별. 주석이 없으면 아무것도 내지 않는다."""
    cells: dict[tuple, list] = defaultdict(list)
    for u in units:
        ann = u.get("annotation") or {}
        arch = (ann.get("archetype") or {}).get("value")
        if not arch or arch in ("unknown", "residual") or not u["span"]["settled"]:
            continue
        for mv in ann.get("moves") or []:
            if mv.get("id") and mv["id"] != "residual":
                cells[(scope_of(u), arch, mv["id"])].append(u)
    out = []
    for (scope, arch, move), us in sorted(cells.items()):
        if len(us) < 3:                                   # policy §5: 같은 원형에서 3건 이상
            continue
        clean = sum(1 for u in us if u["stats"]["outcome"] == "clean")
        bad = sum(1 for u in us if u["stats"]["outcome"] in PROBLEM)
        out.append(C.make("moves", scope, {"type": "archetype", "id": arch},
                          {"n": len(us), "clean": clean, "problem": bad, "recommendable": len(us) >= 5},
                          _strength(len(us)), [u["wu_id"] for u in us],
                          [parse_ts(u["span"]["start"]) for u in us], RECIPE_MOVES,
                          obj={"type": "move", "id": move}, n_evidence=len(us)))
    return out
