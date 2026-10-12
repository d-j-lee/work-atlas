"""효용 평가 — "이 지식이 다음 일에서 실제로 맞았나"를 시간 분할로 잰다. 기준선과 짝지은 부트스트랩 신뢰구간 포함.

  python -m atlas eval --cutoff 2024-01-01 [--cutoff …] [--horizon-days 365] [--tasks cochange,hotspot,files,duration]
                       [--json] [--out 결과.json]

과제 (모두 cutoff 이전 관측만으로 배우고, 그 뒤 horizon 안에서 채점한다 — 미래가 새지 않게):
  cochange  파일 하나를 바꿀 때 함께 바꿀 파일 추천 상위 10 (Zimmermann et al. 2004 의 탐색 시나리오).
            hybrid = 마이닝 추천을 앞에 두고 모자라면 이름 짝·같은 디렉터리로 채운다(맥락 명령의 방식).
            시험 커밋마다 그 안의 파일 각각으로 묻고, 답한 뒤 그 커밋을 배운다(온라인 — 원 논문과 같은 방식).
            지표: 정밀도·재현율(질의 평균)·L3(상위 3개 중 하나라도 맞음)·응답률. hook 문턱(신뢰도≥0.5)일 때도 따로.
            기준선: 전체 인기(자주 바뀐 파일) · 같은 디렉터리 인기 · 이름 짝(Foo↔FooTest — Java 에서 강한 순진한 방법)
  hotspot   cutoff 시점 순위 상위 10% 파일이 다음 horizon 의 결함 수정(파일 접촉)을 얼마나 잡나 (Kim et al. 2007 의 적중률 관점).
            기준선: 변경 횟수(Graves et al. 2000 의 '과거 변경 수') · 변경 줄 수 · 무작위(기댓값 = 상위 비율)
  files     새 이슈의 문장만으로 그 이슈가 바꿀 파일 상위 10 (파일을 바꾼 비슷한 과거 작업 10개 → 유사도 가중 합). 지표: 재현율@10·정밀도@10·적중@10
            mined_any = 이웃을 파일 유무로 가리지 않은 변형(설계 비교용)
            기준선: 전체 인기 · 같은 컴포넌트 인기(이슈 생성 당시 라벨)
  duration  새 이슈의 소요 시간 추정 — 비슷한 과거 작업의 중앙값. 지표: log10(시간+1) 절대오차(MAE)·구간 정확도(<1일·<1주·<1달·이상)
            기준선: 전체 중앙값 · 같은 유형 중앙값
차이는 짝지은 **군집** 부트스트랩(1,000회, 고정 시드)의 95% 구간으로 낸다. 구간이 0 을 넘으면 '유의'로 표시한다.
같은 커밋에서 나온 질의(동반 변경), 같은 파일의 결함 수정 접촉(핫스팟)은 서로 독립이 아니므로 커밋·파일 단위로 다시 뽑는다
— 질의 하나하나를 독립으로 뽑으면 구간이 실제보다 좁아진다.
경로는 cutoff 시점에 알던 이름 바뀜만 적용한다(그 뒤의 이름 바뀜은 모델이 모르는 것이 현실이다).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

from . import config as configmod
from .common import parse_ts, path_filter, paths, read_observations, write_text
from .mine import cochange, corpus as corpusmod, hotspot, text as textmod
from .mine import load_excerpts
from .stitch import stitch

TASKS = ("cochange", "hotspot", "files", "duration")


# ---------------------------------------------------------------- 통계

def mean(xs):
    return round(sum(xs) / len(xs), 4) if xs else None


def boot(a: list[float], b: list[float], n: int = 1000, seed: int = 7, clusters: list | None = None) -> dict | None:
    """짝지은 차이(a-b)의 평균과 95% 백분위 구간. clusters 가 있으면 군집 단위로 다시 뽑는다."""
    if not a or len(a) != len(b):
        return None
    d = [x - y for x, y in zip(a, b)]
    groups: dict = defaultdict(list)
    for i, x in enumerate(d):
        groups[clusters[i] if clusters else i].append(x)
    sums = [(sum(v), len(v)) for v in groups.values()]
    rng = random.Random(seed)
    ms = []
    for _ in range(n):
        pick = [rng.choice(sums) for _ in sums]
        ms.append(sum(s for s, _ in pick) / sum(k for _, k in pick))
    ms.sort()
    lo, hi = ms[int(0.025 * n)], ms[int(0.975 * n) - 1]
    return {"diff": round(sum(d) / len(d), 4), "ci95": [round(lo, 4), round(hi, 4)],
            "significant": lo > 0 or hi < 0, "clusters": len(groups)}


# ---------------------------------------------------------------- 공통 준비

class Split:
    def __init__(self, observations, cfg, excerpts, cutoff: dt.datetime, horizon: dt.timedelta):
        self.cfg, self.cutoff, self.end = cfg, cutoff, cutoff + horizon
        last = max(parse_ts(o["ts"]) for o in observations)
        self.train_units, _ = stitch(observations, cfg, cutoff, excerpts)
        self.train = corpusmod.build(observations, self.train_units, cfg, cutoff, excerpts)
        self.full_units, _ = stitch(observations, cfg, last, excerpts)
        full = corpusmod.build(observations, self.full_units, cfg, last, excerpts)
        ignored = path_filter(cfg["mining"]["ignore_paths"])
        by_id = {o["obs_id"]: o for o in observations}
        self.excerpts, self.by_id = excerpts, by_id
        self.max_files = int(cfg["mining"]["max_files_per_commit"])

        def canon(scope, raw):
            rmap = self.train.renames.get(scope, {})
            return list(dict.fromkeys(rmap.get(p, p) for p in raw if not ignored(rmap.get(p, p))))
        self.canon = canon
        self.test_commits = []
        for c in full.commits:
            if cutoff < c.ts <= self.end:
                raw = by_id[c.obs_id]["keys"].get("paths", [])
                self.test_commits.append(corpusmod.Commit(c.obs_id, c.scope, c.ts, canon(c.scope, raw), c.churn,
                                                          c.is_fix, c.large, c.wus))
        self.full = full
        self.created_obs = {o["objects"][0]["id"]: o for o in observations if o["kind"] == "issue.created"}


# ---------------------------------------------------------------- 과제: 동반 변경

def eval_cochange(sp: Split, level: str = "commit", k: int = 10, online: bool = True) -> dict:
    m = sp.cfg["mining"]
    min_s, min_c = int(m["min_support"]), float(m["min_confidence"])
    hook_c = float(sp.cfg["context"]["hook_min_confidence"])
    rules = cochange.Rules()
    for tx in cochange.transactions(sp.train, level):
        rules.add(*tx)
    freq: dict[str, Counter] = defaultdict(Counter)
    for c in sp.train.commits:
        if not c.large:
            freq[c.scope].update(c.paths)
    methods = ("mined", "mined_hook", "hybrid", "popular", "same_dir", "naming")
    res = {name: {"p": [], "r": [], "l3": [], "answered": 0} for name in methods}
    paired: dict[str, list] = {name: [] for name in methods}          # 질의마다 재현율 (응답 없음 = 0)
    qcluster: list[str] = []                                          # 질의가 나온 커밋 — 군집 부트스트랩 단위
    queries = 0
    for c in sp.test_commits:
        if c.large or not (2 <= len(c.paths) <= sp.max_files):
            pass
        else:
            files = set(c.paths)
            pop = [f for f, _ in freq[c.scope].most_common(k + len(files))]
            for f in sorted(files):
                if not rules.single.get((c.scope, f)):
                    continue                                   # 처음 보는 파일은 어느 방법도 묻지 않는다
                queries += 1
                qcluster.append(c.obs_id)
                expected = files - {f}
                recs = rules.recommend(c.scope, f, min_s, min_c, k)
                cands = {
                    "mined": [b for b, _, _ in recs],
                    "mined_hook": [b for b, _, conf in recs if conf >= hook_c],
                    "popular": [x for x in pop if x != f][:k],
                    "same_dir": [x for x, _ in freq[c.scope].most_common() if x != f
                                 and cochange.directory(x) == cochange.directory(f)][:k],
                    "naming": [x for x, _ in freq[c.scope].most_common() if x != f and cochange.stem(x) == cochange.stem(f)][:k],
                }
                cands["hybrid"] = (cands["mined"] + [x for x, _, _ in cochange.fallback(
                    f, freq[c.scope], set(cands["mined"]), k - len(cands["mined"]))])[:k]
                for name, rec in cands.items():
                    hit = len(set(rec) & expected)
                    paired[name].append(hit / len(expected))
                    if rec:
                        r = res[name]
                        r["answered"] += 1
                        r["p"].append(hit / len(rec))
                        r["r"].append(hit / len(expected))
                        r["l3"].append(1.0 if set(rec[:3]) & expected else 0.0)
        # 답한 뒤에 배운다 (온라인). 기준선의 빈도도 같은 규칙으로 갱신한다.
        if online:
            if level == "commit" and not c.large and 2 <= len(c.paths) <= sp.max_files:
                rules.add(c.ts, c.scope, frozenset(c.paths), c.obs_id)
            if not c.large:
                freq[c.scope].update(c.paths)
    out = {"queries": queries, "level": level, "online": online, "methods": {}}
    for name, r in res.items():
        out["methods"][name] = {"answered": round(r["answered"] / queries, 3) if queries else None,
                                "precision": mean(r["p"]), "recall": mean(r["r"]), "L3": mean(r["l3"]),
                                "recall_all_queries": mean(paired[name])}
    for base in ("popular", "same_dir", "naming"):
        out[f"mined_vs_{base}"] = boot(paired["mined"], paired[base], clusters=qcluster)
        out[f"hybrid_vs_{base}"] = boot(paired["hybrid"], paired[base], clusters=qcluster)
    return out


# ---------------------------------------------------------------- 과제: 핫스팟

def eval_hotspot(sp: Split, top: float = 0.1) -> dict:
    stats = hotspot.scores(sp.train)
    files = [k for k in stats]
    touches = Counter()
    for c in sp.test_commits:
        if c.is_fix and not c.large:
            for p in c.paths:
                if not sp.train.is_test(p):
                    touches[(c.scope, p)] += 1
    total = sum(touches.values())
    if not files or not total:
        return {"files": len(files), "fix_touches": total, "note": "시험 기간에 결함 수정이 없거나 학습 파일이 없다"}
    n = max(1, round(len(files) * top))
    rankings = {
        "fix_damp": hotspot.ranked(stats, "fix_damp"),
        "change_damp": hotspot.ranked(stats, "change_damp"),
        "changes": sorted(files, key=lambda k: (-stats[k]["changes"], k)),
        "churn": sorted(files, key=lambda k: (-stats[k]["churn"], k)),
        "fixes": hotspot.ranked(stats, "fixes"),
    }
    out = {"files": len(files), "top_n": n, "fix_touches": total,
           "unknown_file_touches": sum(v for k, v in touches.items() if k not in stats),
           "random_expected": round(n / len(files), 4), "methods": {}}
    per_touch = {}
    touch_cluster = [k for k, v in sorted(touches.items()) for _ in range(v)]     # 같은 파일의 접촉은 한 군집
    for name, order in rankings.items():
        chosen = set(order[:n])
        cap = sum(v for k, v in touches.items() if k in chosen)
        top20 = order[:20]
        out["methods"][name] = {"capture": round(cap / total, 4),
                                "precision_top20": round(sum(1 for k in top20 if touches.get(k)) / len(top20), 3)}
        per_touch[name] = [1.0 if k in chosen else 0.0 for k, v in sorted(touches.items()) for _ in range(v)]
    for base in ("changes", "churn", "fix_damp", "change_damp"):
        out[f"fixes_vs_{base}"] = boot(per_touch["fixes"], per_touch[base], clusters=touch_cluster)
    return out


# ---------------------------------------------------------------- 과제: 이슈 → 파일, 소요

def _issue_text(sp: Split, key: str) -> str:
    o = sp.created_obs.get(key)
    return sp.excerpts.get(o["obs_id"], "") if o else ""


def _test_issues(sp: Split) -> list[dict]:
    out = []
    created = sp.created_obs
    for u in sp.full_units:
        a = u["anchors"][0]
        if not a.startswith("issue:"):
            continue
        key = a[6:]
        o = created.get(key)
        if not o or not (sp.cutoff < parse_ts(o["ts"]) <= sp.end):
            continue
        out.append({"key": key, "unit": u, "created": o})
    return out


def eval_files(sp: Split, k: int = 10, neighbors: int = 10) -> dict:
    texts = textmod.wu_texts(sp.train_units, sp.train.obs_by_id, sp.excerpts)
    idx = textmod.Index(texts)
    files_of: dict[str, Counter] = defaultdict(Counter)
    comp_files: dict[str, Counter] = defaultdict(Counter)
    glob = Counter()
    wu_comp = {u["wu_id"]: u["stats"].get("labels", {}).get("component", []) for u in sp.train_units}
    for c in sp.train.commits:
        if c.large:
            continue
        glob.update(c.paths)
        for w in c.wus:
            files_of[w].update(c.paths)
            for comp in wu_comp.get(w, []):
                comp_files[comp].update(c.paths)
    test_by_id = {c.obs_id: c for c in sp.test_commits}
    rows = []
    for t in _test_issues(sp):
        truth = set()
        for m in t["unit"]["members"]:
            c = test_by_id.get(m["obs_id"])
            if c and not c.large:
                truth.update(c.paths)
        query = _issue_text(sp, t["key"])
        if not truth or not query:
            continue
        def agg(sims):
            score: dict[str, float] = defaultdict(float)
            for wu, s in sims:
                for f, n in files_of.get(wu, {}).items():
                    score[f] += s
            return [f for f, _ in sorted(score.items(), key=lambda x: (-x[1], x[0]))[:k]]
        ranked_all = idx.query(query, k=200)
        mined = agg([x for x in ranked_all if files_of.get(x[0])][:neighbors])        # 파일이 있는 이웃만
        mined_any = agg(ranked_all[:neighbors])                                          # 이웃을 가리지 않음
        comps = t["created"].get("labels", {}).get("component", [])
        cc = Counter()
        for comp in comps:
            cc.update(comp_files.get(comp, {}))
        comp_rec = [f for f, _ in (cc or glob).most_common(k)]
        pop = [f for f, _ in glob.most_common(k)]
        rows.append({name: (len(set(rec) & truth) / len(truth), len(set(rec) & truth) / k, 1.0 if set(rec) & truth else 0.0)
                     for name, rec in (("mined", mined), ("mined_any", mined_any), ("component", comp_rec), ("popular", pop))})
    out = {"issues": len(rows), "methods": {}}
    for name in ("mined", "mined_any", "component", "popular"):
        out["methods"][name] = {"recall@10": mean([r[name][0] for r in rows]), "precision@10": mean([r[name][1] for r in rows]),
                                "hit@10": mean([r[name][2] for r in rows])}
    for base in ("component", "popular", "mined_any"):
        out[f"mined_vs_{base}"] = boot([r["mined"][0] for r in rows], [r[base][0] for r in rows])
    return out


def _bucket(h: float) -> int:
    return 0 if h < 24 else 1 if h < 24 * 7 else 2 if h < 24 * 30 else 3


def eval_duration(sp: Split, neighbors: int = 10) -> dict:
    texts = textmod.wu_texts(sp.train_units, sp.train.obs_by_id, sp.excerpts)
    idx = textmod.Index(texts)
    dur = {u["wu_id"]: u["stats"]["duration_h"] for u in sp.train_units
           if u["anchors"][0].startswith("issue:") and u["span"]["end"] and u["stats"]["duration_h"] is not None}
    types = {u["wu_id"]: tuple(u["stats"].get("labels", {}).get("type", [])) for u in sp.train_units}
    if not dur:
        return {"issues": 0}
    glob_med = statistics.median(dur.values())
    by_type: dict[tuple, list] = defaultdict(list)
    for w, h in dur.items():
        by_type[types.get(w, ())].append(h)
    errs = {"mined": [], "type": [], "global": []}
    hits = {"mined": [], "type": [], "global": []}
    n = 0
    for t in _test_issues(sp):
        u = t["unit"]
        if not u["span"]["end"] or u["stats"]["duration_h"] is None:
            continue
        query = _issue_text(sp, t["key"])
        if not query:
            continue
        truth = u["stats"]["duration_h"]
        sims = [w for w, _ in idx.query(query, k=50) if w in dur][:neighbors]
        mined = statistics.median([dur[w] for w in sims]) if sims else glob_med
        ty = tuple(t["created"].get("labels", {}).get("type", []))
        typed = statistics.median(by_type[ty]) if by_type.get(ty) else glob_med
        n += 1
        for name, pred in (("mined", mined), ("type", typed), ("global", glob_med)):
            errs[name].append(abs(math.log10(pred + 1) - math.log10(truth + 1)))
            hits[name].append(1.0 if _bucket(pred) == _bucket(truth) else 0.0)
    out = {"issues": n, "methods": {name: {"mae_log10h": mean(errs[name]), "bucket_acc": mean(hits[name])} for name in errs}}
    for base in ("type", "global"):
        out[f"mined_vs_{base}"] = boot([-e for e in errs["mined"]], [-e for e in errs[base]])   # 양수 = 오차가 줄었다
    return out


# ---------------------------------------------------------------- 실행

def evaluate(p, cfg: dict, cutoffs: list[dt.datetime], horizon_days: int = 365, tasks=TASKS, log=None) -> dict:
    observations = read_observations(p.observations)
    excerpts = load_excerpts(p)
    results = []
    for cutoff in cutoffs:
        sp = Split(observations, cfg, excerpts, cutoff, dt.timedelta(days=horizon_days))
        r = {"cutoff": cutoff.isoformat(timespec="seconds"), "horizon_days": horizon_days,
             "train_commits": len(sp.train.commits), "test_commits": len(sp.test_commits)}
        if "cochange" in tasks:
            r["cochange"] = eval_cochange(sp)                               # 온라인 — Zimmermann 방식
            r["cochange_static"] = eval_cochange(sp, online=False)          # cutoff 에 한 번 배운 그대로
            r["cochange_wu_static"] = eval_cochange(sp, level="wu", online=False)
        if "hotspot" in tasks:
            r["hotspot"] = eval_hotspot(sp)
        if "files" in tasks:
            r["files"] = eval_files(sp)
        if "duration" in tasks:
            r["duration"] = eval_duration(sp)
        results.append(r)
        if log:
            log(render_one(r))
    return {"config": {"mining": cfg["mining"], "context": cfg["context"]}, "results": results}


def _ci(b) -> str:
    if not b:
        return "-"
    return f"{b['diff']:+.3f} [{b['ci95'][0]:+.3f}, {b['ci95'][1]:+.3f}]{' 유의' if b['significant'] else ''}"


def render_one(r: dict) -> str:
    lines = [f"== cutoff {r['cutoff'][:10]} · horizon {r['horizon_days']}일 · 학습 커밋 {r['train_commits']} · 시험 커밋 {r['test_commits']}"]
    for key in ("cochange", "cochange_static", "cochange_wu_static"):
        c = r.get(key)
        if c:
            lines.append(f"[{key}] 질의 {c['queries']}")
            for name, m in c["methods"].items():
                lines.append(f"  {name:<11} 응답 {m['answered']} · 정밀 {m['precision']} · 재현 {m['recall']} · L3 {m['L3']}"
                             f" · 재현(전 질의) {m['recall_all_queries']}")
            lines.append("  mined-popular " + _ci(c["mined_vs_popular"]) + " · mined-same_dir " + _ci(c["mined_vs_same_dir"])
                         + " · mined-naming " + _ci(c["mined_vs_naming"]))
            lines.append("  hybrid-popular " + _ci(c["hybrid_vs_popular"]) + " · hybrid-same_dir " + _ci(c["hybrid_vs_same_dir"])
                         + " · hybrid-naming " + _ci(c["hybrid_vs_naming"]))
    h = r.get("hotspot")
    if h and "methods" in h:
        lines.append(f"[hotspot] 파일 {h['files']} · 상위 {h['top_n']} · 결함 수정 접촉 {h['fix_touches']} (새 파일 {h['unknown_file_touches']})"
                     f" · 무작위 기댓값 {h['random_expected']}")
        for name, m in h["methods"].items():
            lines.append(f"  {name:<11} 포착 {m['capture']} · 상위20 정밀 {m['precision_top20']}")
        lines.append("  fixes-changes " + _ci(h["fixes_vs_changes"]) + " · fixes-churn " + _ci(h["fixes_vs_churn"])
                     + " · fixes-fix_damp " + _ci(h["fixes_vs_fix_damp"]) + " · fixes-change_damp " + _ci(h["fixes_vs_change_damp"]))
    f = r.get("files")
    if f:
        lines.append(f"[files] 이슈 {f['issues']}")
        for name, m in f["methods"].items():
            lines.append(f"  {name:<11} 재현@10 {m['recall@10']} · 정밀@10 {m['precision@10']} · 적중@10 {m['hit@10']}")
        lines.append("  mined-component " + _ci(f["mined_vs_component"]) + " · mined-popular " + _ci(f["mined_vs_popular"]))
    d = r.get("duration")
    if d and d.get("issues"):
        lines.append(f"[duration] 이슈 {d['issues']}")
        for name, m in d["methods"].items():
            lines.append(f"  {name:<11} MAE(log10 h) {m['mae_log10h']} · 구간 정확도 {m['bucket_acc']}")
        lines.append("  오차 감소 mined-type " + _ci(d["mined_vs_type"]) + " · mined-global " + _ci(d["mined_vs_global"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas eval", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--cutoff", action="append", required=True, help="YYYY-MM-DD (여러 번 가능)")
    ap.add_argument("--horizon-days", type=int, default=365)
    ap.add_argument("--tasks", default=",".join(TASKS))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    p = paths(args.root)
    cfg = configmod.load(p.root)
    cutoffs = []
    for c in args.cutoff:                       # 시간대가 없으면 UTC 로 본다
        t = dt.datetime.fromisoformat(c)
        cutoffs.append(t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc))
    tasks = tuple(t.strip() for t in args.tasks.split(",") if t.strip())
    result = evaluate(p, cfg, cutoffs, args.horizon_days, tasks, log=None if args.json else print)
    if args.out:
        write_text(Path(args.out), json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
