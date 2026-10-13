"""업무 카테고리 모델 — 카테고리마다 "이 일은 어떤 일이고, 어떻게 흘러가고, 누가 얽히고, 어디를 보면 되나" (결정론, LLM 없음).

카테고리(taxonomy.py)마다 낸다 — 영역 트리의 모든 노드와, 표본이 충분한 영역 × 국면 칸:
  profile     건수 · 소요 P50·P90 · 사후 문제율 · 열린 일 · 최근 추세 · 단계별 머문 시간(병목)
  flow        직접 이음 그래프(DFG): 활동 = 이슈 생성 · 상태 전이(상태 이름) · 커밋(연달아 여럿이면 하나) · 머지 · AI 세션 · 작업 시작.
              댓글·필드 변경·링크는 흐름에 넣지 않고 단계별로 센다. 드문 활동·이음은 걸러 내고(min_share), 그림이
              그대로 담는 흐름의 비율(fitness)을 같이 낸다. 보통의 순서를 거슬러 되돌아가는 이음(재작업 고리)과
              가장 흔한 길(main — 가장 많은 일이 그대로 따른 변형, 건수와 함께)을 표시한다.
  variants    흐름 변형(활동 순서가 똑같은 작업단위 묶음) 상위 몇 개 — 건수·비율·소요·최근 예
  sequence    가장 흔한 길을 참여자(역할 → 트래커·저장소·AI·작업 폴더) 사이의 순서로 — 단계마다 행위자 역할·머문 시간·댓글
  relations   코드(디렉터리·파일) · 다른 카테고리(이슈 링크 · 후속 수정 · 같은 파일) · 역할
  references  대표 사례(텍스트 중심에 가까운 확정 WU) · 최근 깔끔한 사례 · 주의 사례(사후 문제) · 작업 기록 · 상위 이슈 · 사람이 붙인 참조
  contacts    사람이 붙인 접점(팀·채널·역할) + 데이터에서 본 역할별 첫 등장 단계
흐름·순서·변형·머문 시간은 **끝난 일**의 끝까지 사건으로 낸다 — 열린 일은 흐름이 덜 끝나 '생성 → 끝'처럼 보이고,
끝난 뒤의 사건(릴리스 때의 Closed 같은 정리)은 일의 흐름이 아니라서 after_end 로 따로 센다. 열린 일은 지금 단계별로 센다(open).
최근 recent_days 안에 끝난 일로 낸다(워크플로가 바뀌면 옛 흐름이 섞이지 않게). 그게 basis_min 건보다 적으면 가장 최근에 끝난
basis_min 건까지 넓히고 basis 에 밝힌다(extended). 끝난 일이 min_n 건보다 적으면 흐름을 내지 않는다. 수치는 언제나 건수와 같이 낸다 — 연관이지 인과가 아니다.
산출: data/model/categories.json (WU 단위 근거가 있어 git 밖).
"""
from __future__ import annotations

import datetime as dt
import math
import posixpath
from collections import Counter, defaultdict
from dataclasses import dataclass

from ..common import parse_ts
from ..stitch import latest_snapshots
from ..taxonomy import ALL, UNCATEGORIZED, Taxonomy, WuFacts
from .process import PROBLEM, _q, _strength, group_stats

RECIPE = "category@1"
START, END = "(시작)", "(끝)"
CREATED, COMMIT, MERGE, AI, WORK_START = "생성", "커밋", "머지", "AI 세션", "작업 시작"
SYSTEM_OF = {"issue": "트래커", "vcs": "저장소", "ai_session": "AI", "workspace": "작업 폴더"}
RANK = {"issue.created": 0, "workspace.snapshot": 1, "issue.transition": 2}
COUNTED = {"issue.comment": "comments", "issue.changed": "changes", "issue.linked": "links"}


@dataclass
class Event:
    ts: dt.datetime
    activity: str | None        # 흐름의 활동 (None 이면 흐름 밖 — 댓글·변경·링크)
    actor: str
    system: str
    kind: str
    obs_id: str


@dataclass
class Step:
    activity: str
    first: dt.datetime
    last: dt.datetime
    actor: str
    system: str
    count: int


def wu_events(u: dict, obs_by_id: dict) -> list[Event]:
    out = []
    for m in u.get("members", []):
        o = obs_by_id.get(m["obs_id"])
        if not o:
            continue
        k, ts, actor = o["kind"], parse_ts(o["ts"]), o.get("actor_role") or "unknown"
        system = SYSTEM_OF.get(o["source"]["system"], o["source"]["system"])
        act = None
        if k == "issue.created":
            act = CREATED
        elif k == "issue.transition":
            act = (o.get("labels", {}).get("status") or [None])[0]
        elif k == "vcs.commit":
            act = COMMIT
        elif k == "vcs.merge":
            act = None if o["metrics"].get("sync_merge") else MERGE
        elif k == "ai_session.session":
            act = AI
        elif k == "workspace.snapshot":
            started = (o.get("labels", {}).get("started") or [""])[0]
            try:
                day = dt.date.fromisoformat(str(started)[:10])
            except ValueError:
                continue
            ts = min(dt.datetime.combine(day, dt.time(0, 0), tzinfo=ts.tzinfo), ts)
            act = WORK_START
        elif k not in COUNTED:
            continue
        out.append(Event(ts, act, actor, system, k, o["obs_id"]))
    out.sort(key=lambda e: (e.ts, RANK.get(e.kind, 3), e.obs_id))
    return out


def steps_of(events: list[Event]) -> list[Step]:
    """흐름 활동만, 연달아 같은 활동은 하나로 (커밋 여러 개 → 커밋 한 단계)."""
    out: list[Step] = []
    for e in events:
        if not e.activity:
            continue
        if out and out[-1].activity == e.activity:
            out[-1].last = e.ts
            out[-1].count += 1
        else:
            out.append(Step(e.activity, e.ts, e.ts, e.actor, e.system, 1))
    return out


def stages_of(events: list[Event]) -> list[tuple[Event, str]]:
    """사건마다 그때의 단계 — 직전 흐름 활동 (이슈면 상태 전이가 단계를 바꾼다). 흐름 시작 전이면 시작."""
    out, stage = [], START
    for e in events:
        out.append((e, e.activity or stage))
        if e.activity:
            stage = e.activity
    return out


def _hours(a: dt.datetime, b: dt.datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 3600)


@dataclass
class WuView:
    u: dict
    events: list[Event]          # 끝까지의 사건 (열린 일이면 전부)
    steps: list[Step]            # 끝까지의 흐름 단계
    files: list[str]
    start: dt.datetime
    works: list[dict]
    end: dt.datetime | None
    after: list[Event]           # 끝난 뒤의 사건 (정리 · 늦은 재오픈 · 댓글)

    def current(self) -> Step | None:
        """지금 단계 — 끝난 뒤의 사건까지 본다(얼린 뒤 다시 열린 일)."""
        st = steps_of(self.events + self.after)
        return st[-1] if st else None


# ── 흐름 ──
def flow(views: list[WuView], min_share: float) -> dict:
    traces = [[START] + [s.activity for s in v.steps] + [END] for v in views if v.steps]
    n = len(traces)
    if not n:
        return {"n_traces": 0, "nodes": [], "edges": [], "fitness": None, "hidden": {"nodes": 0, "edges": 0}}
    node_n: Counter = Counter()
    edge_n: Counter = Counter()
    edge_times: Counter = Counter()
    rework: Counter = Counter()                          # 이음 a→b 중 b 를 그 흐름에서 이미 지난 경우 (되돌아감)
    before: Counter = Counter()                          # (a, b): 한 흐름에서 a 가 b 보다 먼저 처음 나온 횟수
    gaps: dict[tuple, list[float]] = defaultdict(list)
    for v, tr in zip([v for v in views if v.steps], traces):
        node_n.update(set(tr))
        firsts = list(dict.fromkeys(tr[1:-1]))
        for i, a in enumerate(firsts):
            for b in firsts[i + 1:]:
                before[(a, b)] += 1
        pairs = list(zip(tr, tr[1:]))
        edge_n.update(set(pairs))
        edge_times.update(pairs)
        seen = {START}
        for a, b in pairs:
            if b in seen and b != END:
                rework[(a, b)] += 1
            seen.add(b)
        st = v.steps
        for i in range(len(st) - 1):
            gaps[(st[i].activity, st[i + 1].activity)].append(_hours(st[i].last, st[i + 1].first))
    floor = max(2 if n >= 10 else 1, math.ceil(min_share * n))      # 표본이 10건 이상이면 한 번뿐인 활동·이음은 잡음으로 본다
    keep = {a for a, k in node_n.items() if k >= floor} | {START, END}
    edges = {e for e, k in edge_n.items() if k >= floor and e[0] in keep and e[1] in keep}
    for a in sorted(keep - {START}):                      # 남긴 활동은 들어오는 이음과 나가는 이음을 하나씩은 갖는다
        if a != END and not any(e[0] == a for e in edges):
            cand = sorted((e for e in edge_n if e[0] == a and e[1] in keep), key=lambda e: (-edge_n[e], e))
            edges.update(cand[:1])
        if not any(e[1] == a for e in edges):
            cand = sorted((e for e in edge_n if e[1] == a and e[0] in keep), key=lambda e: (-edge_n[e], e))
            edges.update(cand[:1])
    # 그리는 순서: 둘이 같이 나온 흐름에서 먼저 처음 나온 비율 (클수록 앞) — 드문 활동이 한두 흐름 때문에 앞으로 튀지 않게 비율로
    inner = sorted(keep - {START, END})
    score = {}
    for a in inner:
        won = sum(before[(a, b)] for b in inner if b != a)
        lost = sum(before[(b, a)] for b in inner if b != a)
        score[a] = (won - lost) / (won + lost) if won + lost else 0.0
    ranked = [START] + sorted(inner, key=lambda a: (-score[a], -node_n[a], a)) + [END]
    idx = {a: i for i, a in enumerate(ranked)}
    # 가장 흔한 길 = 실제로 가장 많은 일이 그대로 따른 흐름(변형) — 그림에 남은 이음만으로 그려지는 것 중에서.
    # 굵은 이음을 이어 붙인 길은 아무도 따르지 않은 길이 될 수 있다(이음마다 다른 일이 지난다).
    counts = Counter(tuple(tr) for tr in traces)
    drawn = [(k, tr) for tr, k in counts.items() if all(p in edges for p in zip(tr, tr[1:]))]
    best = min(drawn or [(k, tr) for tr, k in counts.items()], key=lambda x: (-x[0], x[1]))
    main_n, main_tr = best
    main = [p for p in zip(main_tr, main_tr[1:]) if p in edges]
    fit = sum(1 for tr in traces if all(p in edges for p in zip(tr, tr[1:])))
    return {
        "n_traces": n,
        "nodes": [{"id": a, "n": node_n[a], "share": round(node_n[a] / n, 3)} for a in ranked],
        "edges": [{"from": a, "to": b, "n": edge_n[(a, b)], "times": edge_times[(a, b)],
                   "share": round(edge_n[(a, b)] / n, 3),
                   "gap_h_p50": _q(gaps.get((a, b), []), 0.5),
                   "rework": rework[(a, b)],
                   "back": idx[b] <= idx[a] and a != START and b != END,     # 그림에서 왼쪽으로 가는 이음
                   # 재작업 고리: 보통의 순서를 거슬러 이미 지난 활동으로 되돌아간 이음 (재오픈 뒤 다시 완료로 가는 앞 방향 이음은 아니다)
                   "loop": idx[b] <= idx[a] and a != START and b != END and rework[(a, b)] > 0,
                   "main": (a, b) in main}
                  for a, b in sorted(edges, key=lambda e: (idx[e[0]], idx[e[1]]))],
        "main": [a for a in main_tr[1:-1]],
        "main_n": main_n, "main_share": round(main_n / n, 3),
        "fitness": round(fit / n, 3),
        "hidden": {"nodes": len(node_n) - len(keep), "edges": len(edge_n) - len(edges)},
        "min_share": min_share,
    }


def variants(views: list[WuView], k: int = 5) -> list[dict]:
    groups: dict[tuple, list[WuView]] = defaultdict(list)
    for v in views:
        if v.steps:
            groups[tuple(s.activity for s in v.steps)].append(v)
    n = sum(len(g) for g in groups.values())
    out = []
    for tr, g in sorted(groups.items(), key=lambda x: (-len(x[1]), x[0]))[:k]:
        ended = [v for v in g if v.u["span"]["end"]]
        ex = sorted(ended or g, key=lambda v: (v.end or v.start, v.u["wu_id"]))[-1]
        out.append({"trace": list(tr), "n": len(g), "share": round(len(g) / n, 3),
                    "duration_h_p50": _q([v.u["stats"]["duration_h"] for v in ended if v.u["stats"]["duration_h"] is not None], 0.5),
                    "example": ex.u["wu_id"]})
    return out


def dwell(views: list[WuView]) -> list[dict]:
    """단계별 머문 시간(그 활동부터 다음 활동까지)과 그동안의 댓글 수 — 병목을 본다. 끝난 일만."""
    hours: dict[str, list[float]] = defaultdict(list)
    comments: dict[str, list[int]] = defaultdict(list)
    for v in views:
        if not v.u["span"]["end"] or not v.steps:
            continue
        st = v.steps
        per_stage: Counter = Counter()
        for e, stage in stages_of(v.events):
            if e.kind == "issue.comment":
                per_stage[stage] += 1
        for i, s in enumerate(st[:-1]):
            hours[s.activity].append(_hours(s.first, st[i + 1].first))
        for a in {s.activity for s in st}:
            comments[a].append(per_stage.get(a, 0))
    rows = [{"stage": a, "n": len(h), "hours_p50": _q(h, 0.5), "hours_p90": _q(h, 0.9),
             "comments_p50": _q([float(c) for c in comments.get(a, [])], 0.5)}
            for a, h in hours.items() if h]
    rows.sort(key=lambda r: (-(r["hours_p50"] or 0), r["stage"]))
    return rows


def after_end(views: list[WuView]) -> list[dict]:
    """끝난 뒤에 보통 무엇이 오나 — 정리(Closed 등)·재오픈. 끝난 일 중 비율과 끝에서 처음 그 활동까지의 시간."""
    ended = [v for v in views if v.end]
    first: dict[str, list[float]] = defaultdict(list)
    for v in ended:
        seen = set()
        for e in v.after:
            if e.activity and e.activity not in seen:
                seen.add(e.activity)
                first[e.activity].append(_hours(v.end, e.ts))
    n = len(ended) or 1
    return sorted(({"activity": a, "n": len(h), "share": round(len(h) / n, 3), "hours_p50": _q(h, 0.5)}
                   for a, h in first.items()), key=lambda r: (-r["n"], r["activity"]))


def open_stages(views: list[WuView], as_of: dt.datetime) -> dict:
    """열린 일의 지금 단계 분포와 그 단계에 머문 시간 — 밀린 일이 어디에 쌓였나."""
    rows: dict[str, list[float]] = defaultdict(list)
    for v in views:
        if v.u["stats"].get("open_now"):
            cur = v.current()
            if cur:
                rows[cur.activity].append(_hours(cur.last, as_of))
    n = sum(len(h) for h in rows.values())
    return {"n": n, "stages": sorted(({"stage": a, "n": len(h), "share": round(len(h) / n, 3), "age_h_p50": _q(h, 0.5)}
                                      for a, h in rows.items()), key=lambda r: (-r["n"], r["stage"]))}


def sequence(views: list[WuView], fl: dict, dw: list[dict]) -> dict:
    """가장 흔한 길을 참여자 사이의 메시지로: 행위자 역할 → 시스템. 단계마다 행위자 역할은 그 활동에서 가장 흔한 것."""
    path = [a for a in fl.get("main", []) if a not in (START, END)]
    if not path:
        return {"participants": [], "steps": []}
    actors: dict[str, Counter] = defaultdict(Counter)
    systems: dict[str, Counter] = defaultdict(Counter)
    for v in views:
        for s in v.steps:
            actors[s.activity][s.actor] += 1
            systems[s.activity][s.system] += 1
    gap = {(e["from"], e["to"]): e["gap_h_p50"] for e in fl["edges"]}
    stay = {r["stage"]: r for r in dw}
    steps, prev = [], START
    for a in path:
        actor = sorted(actors[a].items(), key=lambda x: (-x[1], x[0]))[0][0] if actors[a] else "unknown"
        system = sorted(systems[a].items(), key=lambda x: (-x[1], x[0]))[0][0] if systems[a] else "트래커"
        total = sum(actors[a].values()) or 1
        steps.append({"activity": a, "actor": actor, "actor_share": round(actors[a][actor] / total, 3), "system": system,
                      "gap_h_p50": gap.get((prev, a)) if prev != START else None,
                      "stay_h_p50": (stay.get(a) or {}).get("hours_p50"),
                      "comments_p50": (stay.get(a) or {}).get("comments_p50")})
        prev = a
    parts = list(dict.fromkeys([s["actor"] for s in steps] + [s["system"] for s in steps]))
    return {"participants": parts, "steps": steps}


# ── 관계 · 참조 · 접점 ──
def code_relations(views: list[WuView], fixes: set[str], k: int = 8) -> dict:
    files: Counter = Counter()
    fixed: Counter = Counter()
    dirs: Counter = Counter()
    touched = 0
    for v in views:
        if not v.files:
            continue
        touched += 1
        for f in set(v.files):
            files[f] += 1
            if v.u["wu_id"] in fixes:
                fixed[f] += 1
        for d in {posixpath.dirname(f) or "." for f in v.files}:
            dirs[d] += 1
    return {
        "wus_with_code": touched,
        "dirs": [{"dir": d, "n": c, "share": round(c / touched, 3)} for d, c in sorted(dirs.items(), key=lambda x: (-x[1], x[0]))[:k]],
        "files": [{"path": f, "n": c, "fixes": fixed.get(f, 0)} for f, c in sorted(files.items(), key=lambda x: (-x[1], x[0]))[:k]],
    }


def role_relations(views: list[WuView]) -> list[dict]:
    """역할별 — 얽힌 작업단위 수, 처음 등장하는 단계(가장 흔한 것), 주로 하는 일."""
    wus: Counter = Counter()
    first: dict[str, Counter] = defaultdict(Counter)
    acts: dict[str, Counter] = defaultdict(Counter)
    for v in views:
        seen = set()
        for e, stage in stages_of(v.events):
            what = e.activity or {"issue.comment": "댓글", "issue.changed": "필드 변경", "issue.linked": "링크"}.get(e.kind, e.kind)
            acts[e.actor][what] += 1
            if e.actor not in seen:
                seen.add(e.actor)
                wus[e.actor] += 1
                first[e.actor][stage] += 1
    n = len(views) or 1
    return [{"role": r, "n": c, "share": round(c / n, 3),
             "first_stage": sorted(first[r].items(), key=lambda x: (-x[1], x[0]))[0][0],
             "first_stage_share": round(sorted(first[r].values())[-1] / c, 3),
             "does": [a for a, _ in sorted(acts[r].items(), key=lambda x: (-x[1], x[0]))[:3]]}
            for r, c in sorted(wus.items(), key=lambda x: (-x[1], x[0]))]


class Model:
    """카테고리 모델 계산 — 한 번 읽고 모든 카테고리를 같은 기준으로 센다."""

    def __init__(self, units: list[dict], corpus, tax: Taxonomy, cfg: dict, vecs: dict | None = None):
        self.cfg = cfg
        m = cfg["mining"]
        self.min_n = int(m.get("category_min_n", 5))
        self.min_share = float(m.get("flow_min_share", 0.05))
        self.recent_days = int(m.get("category_recent_days", 730))
        self.basis_min = int(m.get("category_basis_min", 30))
        self.as_of = corpus.as_of
        self.tax = tax
        self.vecs = vecs or {}
        obs = corpus.obs_by_id
        files_of: dict[str, list[str]] = defaultdict(list)
        for c in corpus.commits:
            if not c.large:
                for w in c.wus:
                    files_of[w].extend(c.paths)
        snaps = {s["objects"][0]["id"]: s for s in latest_snapshots(list(obs.values()), corpus.as_of)}
        self.views: dict[str, WuView] = {}
        facts: dict[str, WuFacts] = {}
        for u in units:
            ws = [snaps[w] for w in u["stats"].get("works", []) if w in snaps]
            wl: dict[str, list[str]] = {"kind": [], "tags": []}
            for s in ws:
                for k in wl:
                    wl[k].extend(s.get("labels", {}).get(k, []))
            ev = wu_events(u, obs)
            end = parse_ts(u["span"]["end"]) if u["span"]["end"] else None
            inside = [e for e in ev if end is None or e.ts <= end]
            self.views[u["wu_id"]] = WuView(u, inside, steps_of(inside), sorted(set(files_of.get(u["wu_id"], []))),
                                            parse_ts(u["span"]["start"]),
                                            [{"id": s["objects"][0]["id"], "rel": s["objects"][0].get("url", ""),
                                              "status": (s.get("labels", {}).get("status") or [""])[0], "ts": s["ts"]} for s in ws],
                                            end, [e for e in ev if end is not None and e.ts > end])
            arch = ((u.get("annotation") or {}).get("archetype") or {}).get("value")
            facts[u["wu_id"]] = WuFacts(u["stats"].get("labels", {}), self.views[u["wu_id"]].files, wl, arch)
        tax.fit(list(facts.values()))
        self.place = {w: tax.place(f) for w, f in facts.items()}
        self.key_of = {}                                  # wu → 이슈 키 (링크 대상 찾기)
        for u in units:
            a = u["anchors"][0]
            if a.startswith("issue:"):
                self.key_of[a.split(":", 1)[1]] = u["wu_id"]
        self.fixes = {u["wu_id"] for u in units if u["stats"].get("is_fix")}

    # 카테고리 구성
    def tree(self) -> tuple[dict[str, dict], dict[str, list[str]]]:
        nodes: dict[str, dict] = {ALL: {"key": ALL, "value": ALL, "chain": [], "parent": None, "members": set(), "direct": set()}}
        member_keys: dict[str, list[str]] = defaultdict(list)
        for w, pl in self.place.items():
            nodes[ALL]["members"].add(w)
            for chain in pl.areas:
                for i, value in enumerate(chain):
                    node = nodes.setdefault(value, {"key": value, "value": value, "chain": chain[:i + 1],
                                                    "parent": chain[i - 1] if i else ALL, "members": set(), "direct": set()})
                    node["members"].add(w)
                nodes[chain[-1]]["direct"].add(w)
                member_keys[w].append(chain[-1])
        cells: dict[str, dict] = {}
        for key, node in list(nodes.items()):
            for spec in self.tax.facet_specs:
                by: dict[str, set] = defaultdict(set)
                for w in node["members"]:
                    for v in self.place[w].facets.get(spec["id"], []):
                        by[v].add(w)
                node.setdefault("facets", {})[spec["id"]] = {v: len(ws) for v, ws in by.items()}
                for v, ws in by.items():
                    if len(ws) < self.min_n:
                        continue
                    ck = self.tax.facet_key(key, spec["id"], v)
                    cells[ck] = {"key": ck, "value": v, "facet": spec["id"], "area": key, "chain": node["chain"],
                                 "parent": key, "members": ws, "direct": ws}
        for w in self.place:
            for spec in self.tax.facet_specs:
                for v in self.place[w].facets.get(spec["id"], []):
                    for leaf in list(member_keys[w]):
                        ck = self.tax.facet_key(leaf, spec["id"], v)
                        if ck in cells:
                            member_keys[w].append(ck)
        return {**nodes, **cells}, {w: sorted(set(ks)) for w, ks in member_keys.items()}

    def basis(self, ws: list[WuView]) -> tuple[list[WuView], dict]:
        """흐름을 낼 표본 — 최근 recent_days 안에 끝난 일. 그게 basis_min 건보다 적으면 가장 최근에 끝난 basis_min 건까지 넓힌다
        (최근성과 표본 크기 중 표본을 지킨다 — 몇 건으로 그린 흐름은 우연이 크다)."""
        ended = sorted((v for v in ws if v.end), key=lambda v: (v.end, v.u["wu_id"]), reverse=True)
        if self.recent_days <= 0:
            return ended, {"ended_since": None, "n": len(ended), "of_ended": len(ended)}
        since = self.as_of - dt.timedelta(days=self.recent_days)
        r = [v for v in ended if v.end >= since]
        extended = len(r) < self.basis_min and len(ended) > len(r)
        if extended:
            r = ended[:self.basis_min]
        first = min((v.end for v in r), default=None)
        return r, {"ended_since": first.date().isoformat() if first else None, "n": len(r), "of_ended": len(ended),
                   "window_days": self.recent_days, "extended": extended}

    def profile(self, ws: list[WuView], node: dict) -> dict:
        us = [v.u for v in ws]
        st = group_stats(us)
        starts = sorted(v.start for v in ws)
        q_end = (self.as_of.year * 4 + (self.as_of.month - 1) // 3)
        trend = [0] * 8
        for t in starts:
            q = q_end - (t.year * 4 + (t.month - 1) // 3)
            if 0 <= q < 8:
                trend[7 - q] += 1
        comments = [float(sum(1 for e in v.events if e.kind == "issue.comment")) for v in ws if v.u["span"]["end"]]
        settled = [u for u in us if u["span"]["settled"] and u["stats"]["outcome"]]
        return {**st,
                "direct": len(node.get("direct", ())),
                "open_now": sum(1 for u in us if u["stats"].get("open_now")),
                "recent_90d": sum(1 for t in starts if t >= self.as_of - dt.timedelta(days=90)),
                "first": starts[0].date().isoformat() if starts else None,
                "last": starts[-1].date().isoformat() if starts else None,
                "trend_quarters": trend,
                "problem_rate": round(sum(1 for u in settled if u["stats"]["outcome"] in PROBLEM) / len(settled), 3) if settled else None,
                "ai_share": round(sum(1 for u in us if u["stats"].get("ai_sessions")) / len(us), 3) if us else None,
                "work_share": round(sum(1 for u in us if u["stats"].get("works")) / len(us), 3) if us else None,
                "comments_p50": _q(comments, 0.5),
                "anchors": dict(sorted(Counter(u["anchors"][0].split(":", 1)[0] for u in us).items()))}

    def related(self, key: str, ws: list[WuView], node: dict, area_of: dict[str, set[str]], k: int = 4) -> list[dict]:
        """다른 카테고리와의 관계 — 같은 깊이의 영역끼리. 이슈 링크 · 후속 수정 · 같은 파일."""
        mine = {v.u["wu_id"] for v in ws}
        depth = len(node["chain"])
        own = set(node["chain"])

        def targets(w: str) -> set[str]:
            """그 작업단위가 든 같은 깊이의 영역 (자기 자신과 조상은 빼고 — 형제는 관계다)."""
            if not depth:
                return set()
            return {c[min(depth, len(c)) - 1] for c in self.place[w].areas if c[0] != UNCATEGORIZED} - own

        rows: dict[tuple, dict] = {}

        def add(kind: str, via: str, other: str, w: str):
            r = rows.setdefault((kind, other, via), {"kind": kind, "key": other, "via": via, "n": 0, "examples": []})
            r["n"] += 1
            if len(r["examples"]) < 3 and w not in r["examples"]:
                r["examples"].append(w)

        for v in ws:
            w = v.u["wu_id"]
            for e in v.events:
                if e.kind != "issue.linked":
                    continue
                o_links = self._links.get(e.obs_id, [])
                for phrase, target in o_links:
                    tw = self.key_of.get(target)
                    if tw and tw not in mine:
                        for t in targets(tw):
                            add("link", phrase, t, w)
            for f in v.u["stats"].get("followup_fix_wus", []):
                if f in self.place and f not in mine:
                    for t in targets(f):
                        add("followup", "후속 수정", t, w)
            for f in v.u["stats"].get("induced_by_wus", []):
                if f in self.place and f not in mine:
                    for t in targets(f):
                        add("induced_by", "이 일을 부른 변경", t, w)
        out = []
        for kind in ("link", "followup", "induced_by"):
            out += sorted((r for r in rows.values() if r["kind"] == kind), key=lambda r: (-r["n"], r["key"], r["via"]))[:k]
        mine_files = {f for v in ws for f in v.files}
        if depth and mine_files:                             # 같은 파일 — 겹친 파일 수가 아니라 자카드로 (큰 파일 묶음이 모두를 잇지 않게)
            shared: Counter = Counter()
            for f in mine_files:
                for t in area_of.get(f, ()):
                    if t not in own and len(self._chain_of[t]) == depth:
                        shared[t] += 1
            files = [{"kind": "files", "key": t, "via": "같은 파일", "n": c,
                      "jaccard": round(c / (len(mine_files) + len(self._files_of_area[t]) - c), 3), "examples": []}
                     for t, c in shared.items() if c >= 2]
            out += sorted(files, key=lambda r: (-r["jaccard"], -r["n"], r["key"]))[:k]
        return out

    def references(self, key: str, ws: list[WuView], node: dict) -> dict:
        ended = [v for v in ws if v.u["span"]["end"]]
        settled = [v for v in ended if v.u["span"]["settled"]]
        by_end = lambda v: (v.end, v.u["wu_id"])                   # 시각 비교 — 문자열이면 시간대가 섞일 때 틀린다
        rep = []
        vecs = [self.vecs[v.u["wu_id"]] for v in settled if v.u["wu_id"] in self.vecs]
        if vecs:
            cen: dict[str, float] = defaultdict(float)
            for vec in vecs:
                for t, x in vec.items():
                    cen[t] += x / len(vecs)
            norm = math.sqrt(sum(x * x for x in cen.values())) or 1.0
            scored = sorted(((sum(x * cen.get(t, 0.0) for t, x in self.vecs[v.u["wu_id"]].items()) / norm, v.u["wu_id"])
                             for v in settled if v.u["wu_id"] in self.vecs), key=lambda x: (-x[0], x[1]))
            rep = [{"wu_id": w, "sim": round(s, 3)} for s, w in scored[:3]]
        clean = sorted((v for v in settled if v.u["stats"]["outcome"] == "clean"), key=by_end)[-3:][::-1]
        trouble = sorted((v for v in settled if v.u["stats"]["outcome"] in PROBLEM), key=by_end)[-3:][::-1]
        works = sorted(((wk, v) for v in ws for wk in v.works), key=lambda x: (parse_ts(x[0]["ts"]), x[0]["id"]))[-3:][::-1]
        parents: Counter = Counter()
        for v in ws:
            for e in v.events:
                if e.kind == "issue.created":
                    parents.update(self._parent.get(e.obs_id, []))
        hubs: Counter = Counter()
        for v in ws:
            for e in v.events:
                if e.kind == "issue.linked":
                    hubs.update(t for _, t in self._links.get(e.obs_id, []))
        return {
            "representative": rep,
            "recent_clean": [{"wu_id": v.u["wu_id"], "end": v.u["span"]["end"]} for v in clean],
            "cautionary": [{"wu_id": v.u["wu_id"], "end": v.u["span"]["end"], "outcome": v.u["stats"]["outcome"]} for v in trouble],
            "works": [{**wk, "wu_id": v.u["wu_id"]} for wk, v in works],
            "parents": [{"key": p, "n": c, "wu_id": self.key_of.get(p)} for p, c in sorted(parents.items(), key=lambda x: (-x[1], x[0]))[:3] if c >= 2],
            "link_hubs": [{"key": t, "n": c, "wu_id": self.key_of.get(t)} for t, c in sorted(hubs.items(), key=lambda x: (-x[1], x[0]))[:3] if c >= 2],
            "curated": self.tax.references(key, node["chain"], node.get("area")),
        }

    def build(self, obs_by_id: dict) -> dict:
        self._links = {oid: [tuple(x.partition("|")[::2]) for x in o["keys"].get("links", [])]
                       for oid, o in obs_by_id.items() if o["kind"] == "issue.linked"}
        self._parent = {oid: [p for p in ([o["keys"]["parent"]] if isinstance(o["keys"].get("parent"), str) else o["keys"].get("parent", [])) if p]
                        for oid, o in obs_by_id.items() if o["kind"] == "issue.created" and o["keys"].get("parent")}
        cats, member_keys = self.tree()
        self._chain_of = {k: c["chain"] for k, c in cats.items() if "facet" not in c and k != ALL}
        area_of: dict[str, set[str]] = defaultdict(set)
        self._files_of_area: dict[str, set[str]] = defaultdict(set)
        for w, pl in self.place.items():
            for f in self.views[w].files:
                for chain in pl.areas:
                    for value in chain:
                        if value != UNCATEGORIZED:
                            area_of[f].add(value)
                            self._files_of_area[value].add(f)
        out = {}
        for key in sorted(cats, key=lambda k: (k != ALL, k)):
            c = cats[key]
            ws = sorted((self.views[w] for w in c["members"]), key=lambda v: (v.start, v.u["wu_id"]))
            recent, basis = self.basis(ws)
            fl = flow(recent, self.min_share) if len(recent) >= self.min_n else None
            dw = dwell(recent) if fl else []
            facet = c.get("facet")
            entry = {
                "key": key, "value": c["value"], "chain": c["chain"], "parent": c["parent"],
                "kind": "all" if key == ALL else "facet" if facet else "area",
                "label": self.tax.label(c["value"], c["chain"][-2] if facet is None and len(c["chain"]) > 1 else None),
                "name": (f"{self.tax.names.get(c['area'], c['area'])} · {c['value']}" if facet
                         else self.tax.names.get(c["value"], c["value"])),
                "profile": self.profile(ws, c),
                "strength": _strength(sum(1 for v in ws if v.u["span"]["end"])),
                "basis": basis,
            }
            if facet:
                entry.update({"facet": facet, "area": c["area"]})
            else:
                entry["facets"] = {f: dict(sorted(d.items(), key=lambda x: (-x[1], x[0]))) for f, d in c.get("facets", {}).items()}
            if fl:
                entry["flow"] = fl
                entry["variants"] = variants(recent)
                entry["dwell"] = dw
                entry["sequence"] = sequence(recent, fl, dw)
                entry["after_end"] = after_end(recent)
            entry["open"] = open_stages(ws, self.as_of)
            entry["code"] = code_relations(ws, self.fixes)
            entry["roles"] = role_relations(recent or ws)
            if key != ALL:
                entry["related"] = self.related(key, ws, c if not facet else cats[c["area"]], area_of)
            entry["references"] = self.references(key, ws, c)
            entry["contacts"] = {"configured": self.tax.contacts(key, c["chain"], c.get("area")),
                                 "observed": [r for r in entry["roles"] if r["role"] != "self"]}
            out[key] = entry
        children: dict[str, list[str]] = defaultdict(list)
        for key, e in out.items():
            if e["kind"] == "area" or e["kind"] == "facet":
                children[e["parent"]].append(key)
        for key, e in out.items():
            e["children"] = sorted((k for k in children.get(key, []) if out[k]["kind"] == "area"),
                                   key=lambda k: (-out[k]["profile"]["n"], k))
            e["cells"] = sorted((k for k in children.get(key, []) if out[k]["kind"] == "facet"),
                                key=lambda k: (-out[k]["profile"]["n"], k))
        return {"categories": out, "members": member_keys}


def open_positions(model: Model) -> dict[str, dict]:
    """지금 열린 작업단위의 현재 단계 — 맥락·훅이 '다음에 보통 무엇이 오나'를 보여 줄 때 쓴다."""
    out = {}
    for w, v in model.views.items():
        cur = v.current() if v.u["stats"].get("open_now") else None
        if cur:
            out[w] = {"stage": cur.activity, "since": cur.last.isoformat(timespec="seconds")}
    return out


def suggest_paths(model: Model, min_n: int = 8, min_precision: float = 0.8, k: int = 10) -> list[dict]:
    """경로 규칙 후보 — 영역이 정해진 일들이 어느 디렉터리를 거의 혼자 쓰나. 미분류 작업단위를 덮는 것만 낸다.
    사람이 보고 categories.yaml 의 paths 에 옮긴다(자동 적용하지 않는다)."""
    by_dir: dict[str, Counter] = defaultdict(Counter)
    wus_dir: Counter = Counter()                          # 영역이 정해진 작업단위 중 그 디렉터리를 만진 수 (정밀도의 분모)
    uncat_dirs: Counter = Counter()
    for w, pl in model.place.items():
        files = model.views[w].files
        if not files:
            continue
        dirs = set()
        for f in files:
            parts = f.split("/")[:-1]
            dirs.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
        if pl.areas == [[UNCATEGORIZED]]:
            uncat_dirs.update(dirs)
        elif model.place[w].source != "path":
            leaves = {c[-1] for c in pl.areas}
            for d in dirs:
                by_dir[d].update(leaves)
                wus_dir[d] += 1
    cand = []
    for d, cnt in by_dir.items():
        total = wus_dir[d]
        area, c = sorted(cnt.items(), key=lambda x: (-x[1], x[0]))[0]
        if total >= min_n and c / total >= min_precision and uncat_dirs.get(d):
            cand.append((d, area, c, total))
    chosen = []
    for d, area, c, total in sorted(cand, key=lambda x: (x[0].count("/"), x[0])):
        if any(d.startswith(p + "/") and a == area for p, a, *_ in chosen):     # 같은 영역의 더 넓은 후보가 있으면 뺀다
            continue
        chosen.append((d, area, c, total))
    rows = [{"glob": f"{d}/**", "area": area, "precision": round(c / total, 3), "n": total, "covers": uncat_dirs[d]}
            for d, area, c, total in chosen]
    return sorted(rows, key=lambda r: (-r["covers"], -r["precision"], r["glob"]))[:k]


def mine(units: list[dict], corpus, tax: Taxonomy, cfg: dict, vecs: dict | None = None) -> dict:
    model = Model(units, corpus, tax, cfg, vecs)
    built = model.build(corpus.obs_by_id)
    cats = built["categories"]
    n = len(units)
    uncategorized = cats[UNCATEGORIZED]["profile"]["n"] if UNCATEGORIZED in cats else 0
    sources = Counter(pl.source or "(없음)" for pl in model.place.values())
    return {
        "recipe": RECIPE,
        "area": {"label": tax.area.get("label", "영역"), "from": tax.area.get("from", [])},
        "facets": [{"id": f["id"], "label": f.get("label", f["id"])} for f in tax.facet_specs],
        "coverage": {"units": n, "uncategorized": uncategorized,
                     "ratio": round(uncategorized / n, 3) if n else None, "by_source": dict(sorted(sources.items()))},
        "params": {"min_n": model.min_n, "flow_min_share": model.min_share, "recent_days": model.recent_days,
                   "basis_min": model.basis_min},
        "categories": cats,
        "members": built["members"],
        "open": open_positions(model),
        "suggestions": {"paths": suggest_paths(model)},
    }
