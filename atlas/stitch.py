"""상관 — 관측을 작업단위(WU)로 묶는다 (L1, 결정론, LLM 없음).

객체 중심이다: WU 는 앵커 객체 하나를 중심으로 하고, 한 관측이 여러 WU 의 구성원이 될 수 있다(다대다).
연결 순서 (앞이 강하다):
  1. 이슈 사건            → issue:<키> WU                                   key · high
  2. 커밋의 이슈 키(≤N개) → 그 이슈 WU                                       key · high
  3. MR·머지 묶음         → 묶음 안 커밋·머지가 가진 키 합집합의 이슈 WU. 키가 없으면 mr:/merge: WU  key · high
  3b. 작업 폴더(최신 스냅숏) → front-matter·폴더 이름의 키의 이슈 WU. 키가 없으면 work:<작업> WU   key · high
  4. AI 세션의 키         → 그 이슈 WU (프롬프트·브랜치에 키)                key · high
     세션을 연 작업 폴더  → 그 작업이 속한 WU (위 3b)                         key · high
       (키 없는 작업 폴더면 아래 경로 겹침도 본다 — 아무 데도 안 붙은 커밋은 그 work: WU 로)
     세션 브랜치 = 머지 브랜치 → 그 묶음의 WU                               key · medium
     세션 끝 + session_link_hours 안의 커밋과 경로 겹침 → 그 커밋의 WU      probabilistic · low/medium
     어디에도 안 이어지면 편집이 있거나 프롬프트가 충분할 때 ai_session:<id> WU
  키가 너무 많은 커밋(릴리스·일괄)·어디에도 안 이어진 커밋·동기화 머지는 고아로 남겨 비율을 보고한다(P8).

사후 신호 (L0 사실에서만 — method/schema.md L1):
  reopened     재오픈 사건(같은 시각의 상태·해결 재오픈은 한 번으로 센다)
  rolled_back  이 WU 의 커밋을 되돌린 커밋('This reverts commit …')이 WU 밖이거나 WU 종료 뒤에 있다
  followup_fix 이 WU 가 유발한 결함 수정 WU 가 있다 — 사람이 남긴 연결: 'is caused by · is broken by'(역방향 'causes · breaks')
  followup_suspected  SZZ 근사(파일 수준 마지막 수정자)가 가리킨 후보 — 결정론 신호로 치지 않는다(결과·카드 자격에 쓰지 않음).
               결함 수정 WU 가 고친 파일을, 결함이 보고되기 전에 마지막으로 바꾼 WU.
                  결함 보고가 그 WU 종료 + 열린 창 안이어야 한다. 장부 파일(mining.ignore_paths)·테스트 파일·큰 커밋은 뺀다.
                  문서 작업(제목이 keys.fix_exclude 에 걸림)은 유발자·수정 어느 쪽도 아니다.
                  줄 수준 blame 이 아니라 근사다 — 뒤엉킨 변경에 약하고, 실데이터 표본 감사 정밀도가 낮다(EVALUATION.md).
결과(outcome, 코드북 computed 패싯): rolled_back > followup_fix > reopened > abandoned > clean. 끝나지 않았으면 없음.
트래커 상태가 없는 WU 에 상태를 적은 작업 기록이 있으면 기간·상태는 그것을 따른다(end_basis "work"): 시작 = started,
끝 = status 가 done·dropped 일 때 closed(없으면 그 스냅숏 시각), dropped 는 abandoned. 트래커가 있으면 트래커가 정본이고,
상태를 적지 않은 폴더(기록 없음)는 다른 WU 처럼 마지막 활동으로 끝을 잡는다.
신호·결과에는 열린 창(종료 + open_window_days) 안에서 **관측된** 사후 사건만 쓴다(재오픈·되돌림 시각, 연결이 만들어진 시각).
창이 지나면 확정(settled)이고 이슈 상태를 얼린다 — 그 뒤의 재오픈은 late_reopens 로 세기만 한다. 그래서 확정 결과는 바뀌지 않는다.
followup_fix_wus 는 연결된 모든 후속 수정(사실)이고, 신호는 그중 창 안에서 관측된 것만이다.

--as-of 이후의 관측은 보지 않는다. 같은 관측·같은 설정·같은 as-of 면 같은 결과다(시간 분할 평가가 이것에 기댄다).
사용: python -m atlas stitch [--as-of 2026-10-01T00:00:00+09:00] [--out data/units/stitched.jsonl]
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from . import config as configmod
from .common import parse_ts, path_filter, paths, read_jsonl, read_observations, write_jsonl, write_text

RECIPE = "stitch@2"
STRENGTH = {"high": 3, "medium": 2, "low": 1}
INDUCER_PHRASES = re.compile(r"^is (caused|broken) by$", re.I)      # 대상이 유발자
INDUCES_PHRASES = re.compile(r"^(causes|breaks)$", re.I)            # 이 이슈가 유발자


def _sha(obs: dict) -> str | None:
    return next((o["id"] for o in obs["objects"] if o["type"] == "commit"), None)


def latest_sessions(observations: list[dict]) -> list[dict]:
    """세션은 커지며 여러 번 관측된다 — 세션별 줄 수가 가장 큰 관측 하나만 쓴다."""
    best: dict[str, dict] = {}
    for o in observations:
        if o["kind"] != "ai_session.session":
            continue
        sid = o["objects"][0]["id"]
        n = int(o["obs_id"].rsplit("#", 1)[-1]) if o["obs_id"].rsplit("#", 1)[-1].isdigit() else 0
        cur = best.get(sid)
        if cur is None or (n, o["collected_at"]) > cur[0]:
            best[sid] = ((n, o["collected_at"]), o)
    return [v[1] for v in best.values()]


def latest_snapshots(observations: list[dict], as_of: dt.datetime | None = None) -> list[dict]:
    """작업 폴더는 내용이 바뀔 때마다 다시 관측된다 — 작업별로 가장 나중에 수집한 스냅숏 하나만 쓴다.
    as_of 가 있으면 그때까지 **수집한** 것만 본다. 스냅숏의 ts 는 파일 수정 시각이라, 옮겨 온 파일(수정 시각이 옛날)이
    든 나중 스냅숏이 과거로 새지 않게 하려면 수집 시각으로 거른다."""
    best: dict[str, tuple] = {}
    for o in observations:
        if o["kind"] != "workspace.snapshot" or not o.get("objects"):
            continue
        if as_of and parse_ts(o["collected_at"]) > as_of:
            continue
        wid = o["objects"][0]["id"]
        rank = (parse_ts(o["collected_at"]), parse_ts(o["ts"]), o["obs_id"])
        if wid not in best or rank > best[wid][0]:
            best[wid] = (rank, o)
    return [v[1] for v in best.values()]


def _day(value: str, ref: dt.datetime, end: bool = False) -> dt.datetime | None:
    """front-matter 날짜(YYYY-MM-DD) → 그 날의 시작(또는 끝) — 스냅숏 시각의 시간대로."""
    try:
        d = dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    t = dt.time(23, 59, 59) if end else dt.time(0, 0, 0)
    return dt.datetime.combine(d, t, tzinfo=ref.tzinfo)


def work_span(snaps: list[dict], start: dt.datetime) -> tuple[dt.datetime, dt.datetime | None, str | None]:
    """작업 기록의 기간과 상태 (시작, 끝 또는 None=진행 중, 대표 상태). 여러 작업이면 모두 끝나야 끝이다."""
    statuses, ends = [], []
    for s in snaps:
        ref = parse_ts(s["ts"])
        lab = s.get("labels", {})
        started = _day(lab.get("started", [""])[0], ref)
        if started and started < start:
            start = started
        status = (lab.get("status") or [""])[0]
        statuses.append(status)
        if status in ("done", "dropped"):           # 적힌 종료일이 그 스냅숏보다 뒤일 수는 없다 — 미래로 새지 않게
            ends.append(min(_day(lab.get("closed", [""])[0], ref, end=True) or ref, ref))
    done = statuses and all(x in ("done", "dropped") for x in statuses)
    end = max(max(ends), start) if done else None
    status = "dropped" if done and all(x == "dropped" for x in statuses) else (statuses[0] if len(set(statuses)) == 1 else None)
    return start, end, status


def replay_issue(events: list[dict], as_of: dt.datetime | None = None, window: dt.timedelta | None = None) -> dict:
    """이슈 사건을 시각순으로 재생해 as_of 시점의 상태를 만든다.

    window 가 있으면 **확정 뒤에는 얼린다**: 끝난 뒤 window 가 지나도록 재오픈이 없으면 그 끝이 최종이고,
    그 뒤의 재오픈·해결은 이 WU 의 결과를 바꾸지 않는다(late_reopens 로 세기만 한다). 라벨 변경은 계속 반영한다.
    지금 열려 있는지(open_now)는 얼림과 무관하게 따라간다 — 확정 결과와 섞지 않고 소비가 따로 보여 준다.
    """
    labels: dict[str, list[str]] = {}
    created = end = resolution = None
    reopen_ts: set[str] = set()
    links: list[tuple[str, str, str]] = []
    frozen = open_now = False
    late_reopen_ts: set[str] = set()                 # 재오픈 하나가 사건 둘(상태 전이·해결 해제)로 온다 — 시각으로 센다
    for e in sorted(events, key=lambda e: (parse_ts(e["ts"]), e["obs_id"])):
        t = parse_ts(e["ts"])
        if as_of and t > as_of:
            break
        if window and end and not frozen and t > parse_ts(end) + window:
            frozen = True
        k, lab = e["kind"], e.get("labels", {})
        reopen = k == "issue.reopened" or (k == "issue.transition" and bool(e["metrics"].get("reopen")))
        if k == "issue.created" or reopen:
            open_now = True
        elif k == "issue.resolved" or (k == "issue.transition" and e["metrics"].get("to_done")):
            open_now = False
        if frozen and k in ("issue.resolved", "issue.reopened", "issue.transition"):
            if reopen:
                late_reopen_ts.add(e["ts"])
            continue
        if k == "issue.created":
            created = e["ts"]
            labels = {f: list(v) for f, v in lab.items()}
        elif k == "issue.changed":
            f = lab["field"][0]
            labels[f] = list(lab.get(f, []))
        elif k == "issue.resolved":
            resolution = lab.get("resolution", [None])[0]
            end = end or e["ts"]
        elif k == "issue.transition":
            if e["metrics"].get("reopen"):
                reopen_ts.add(e["ts"])
                end = None
            elif e["metrics"].get("to_done"):
                end = end or e["ts"]
        elif k == "issue.reopened":
            reopen_ts.add(e["ts"])
            end = resolution = None
        elif k == "issue.linked":
            for link in e["keys"].get("links", []):
                phrase, _, target = link.partition("|")
                links.append((phrase, target, e["ts"]))
    return {"created": created, "end": end, "resolution": resolution, "reopen_count": len(reopen_ts),
            "reopen_ts": sorted(reopen_ts), "late_reopens": len(late_reopen_ts), "open_now": open_now,
            "labels": {f: v for f, v in labels.items() if v}, "links": links}


class _Units:
    def __init__(self):
        self.members: dict[str, dict[str, dict]] = defaultdict(dict)

    def add(self, anchor: str, obs_id: str, link: str, confidence: str, signals: list[str]) -> None:
        cur = self.members[anchor].get(obs_id)
        if cur is None or STRENGTH[confidence] > STRENGTH[cur["confidence"]]:
            m = {"obs_id": obs_id, "link": link, "confidence": confidence}
            if signals:
                m["signals"] = signals
            self.members[anchor][obs_id] = m

    def anchors_of(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for anchor, ms in self.members.items():
            for oid in ms:
                out[oid].add(anchor)
        return out


def stitch(observations: list[dict], cfg: dict, as_of: dt.datetime,
           excerpts: dict[str, str] | None = None) -> tuple[list[dict], dict]:
    """excerpts 가 있으면 결함 수정 판정을 지금 설정으로 다시 계산한다(없으면 수집 당시 값)."""
    sc = cfg["stitch"]
    patterns = configmod.Patterns(cfg)
    excerpts = excerpts or {}
    max_keys = int(sc["max_issue_keys"])
    window = dt.timedelta(days=int(cfg["policy"]["open_window_days"]))
    max_files = int(cfg["mining"]["max_files_per_commit"])
    bug_types = {t.lower() for t in sc["bug_types"]}
    abandoned = {r.lower() for r in sc["abandoned_resolutions"]}
    ignored = path_filter(cfg["mining"]["ignore_paths"])
    is_test = path_filter(cfg["mining"]["test_paths"])

    obs = [o for o in observations
           if o["source"]["system"] not in ("atlas", "self_report") and parse_ts(o["ts"]) <= as_of]
    by_id = {o["obs_id"]: o for o in obs}
    sessions = latest_sessions(obs)
    snaps = latest_snapshots(obs, as_of)
    current = {s["obs_id"] for s in sessions} | {s["obs_id"] for s in snaps}
    obs = [o for o in obs if o["kind"] not in ("ai_session.session", "workspace.snapshot") or o["obs_id"] in current]
    commits = [o for o in obs if o["kind"] in ("vcs.commit", "vcs.merge")]
    sha_index: dict[tuple[str, str], str] = {}
    for c in commits:
        sha_index[(c["source"]["instance"], _sha(c))] = c["obs_id"]

    units = _Units()
    issue_events: dict[str, list[dict]] = defaultdict(list)
    for o in obs:
        if o["source"]["system"] == "issue":
            key = o["objects"][0]["id"]
            issue_events[key].append(o)
            units.add(f"issue:{key}", o["obs_id"], "key", "high", ["issue"])

    def keys_of(o: dict) -> list[str]:
        ks = [k for k in o["keys"].get("issue_keys", []) if patterns.key_ok(k)]
        return ks if len(ks) <= max_keys else []

    def commit_is_fix(o: dict) -> bool:
        text = excerpts.get(o["obs_id"])
        return patterns.is_fix(text) if text is not None else bool(o["metrics"].get("is_fix"))

    # 2. 커밋의 이슈 키
    for c in commits:
        for k in keys_of(c):
            units.add(f"issue:{k}", c["obs_id"], "key", "high", ["issue_key"])

    # 3. MR·머지 묶음
    groups: dict[str, dict] = {}
    for c in commits:
        inst = c["source"]["instance"]
        gids = [f"mr:{m}" for m in c["keys"].get("mr_ids", [])]
        # 수집기가 동기화 머지로 표시한 머지의 들여온 커밋은 쓰지 않는다 — 메인라인의 남의 커밋이다.
        # 형식을 모르는 머지(Azure DevOps 등)는 키·브랜치가 있을 때만 묶는다.
        own_commits = not c["metrics"].get("sync_merge", False)
        if not gids and c["kind"] == "vcs.merge" and (c["keys"].get("branches") or c["keys"].get("issue_keys")):
            gids = [f"merge:{inst}:{_sha(c)}"]
        for gid in gids:
            g = groups.setdefault(gid, {"members": set(), "keys": set(), "branches": set()})
            g["members"].add(c["obs_id"])
            g["keys"].update(keys_of(c))
            g["branches"].update(c["keys"].get("branches", []))
            for sha in (c["keys"].get("commits", []) if own_commits else []):
                oid = sha_index.get((inst, sha))
                if oid:
                    g["members"].add(oid)
                    g["keys"].update(keys_of(by_id[oid]))
    branch_anchor: dict[str, set[str]] = defaultdict(set)
    for gid, g in sorted(groups.items()):
        anchors = [f"issue:{k}" for k in sorted(g["keys"])] if 0 < len(g["keys"]) <= max_keys else [gid]
        for a in anchors:
            for oid in g["members"]:
                units.add(a, oid, "key", "high", ["mr"])
            for b in g["branches"]:
                branch_anchor[b].add(a)

    # 3b. 작업 폴더 — 선언된 키(없으면 폴더 이름의 키)의 이슈 WU, 키가 없으면 그 작업 자신이 앵커다(티켓 없이 한 일)
    work_anchor: dict[str, list[str]] = {}
    for s in snaps:
        obj = s["objects"][0]["id"]
        work_anchor[obj] = [f"issue:{k}" for k in keys_of(s)] or [f"work:{obj}"]
        for a in work_anchor[obj]:
            units.add(a, s["obs_id"], "key", "high", ["work"])

    # 4. AI 세션
    commit_anchor = units.anchors_of()
    commit_times = sorted((parse_ts(c["ts"]), c["obs_id"]) for c in commits if c["kind"] == "vcs.commit")
    commit_ts_only = [t for t, _ in commit_times]
    link_after = dt.timedelta(hours=float(sc["session_link_hours"]))
    session_end: dict[str, dt.datetime] = {}
    for s in sessions:
        start = parse_ts(s["ts"])
        end = start + dt.timedelta(minutes=float(s["metrics"].get("duration_min", 0)))
        session_end[s["obs_id"]] = end
        keys = keys_of(s)
        in_work = [a for w in s["keys"].get("works", []) for a in work_anchor.get(w, ())]
        for a in in_work:                       # 작업 폴더에서 연 세션 — 그 작업이 속한 WU
            units.add(a, s["obs_id"], "key", "high", ["work_folder"])
        if keys:
            for k in keys:
                units.add(f"issue:{k}", s["obs_id"], "key", "high", ["issue_key"])
            continue
        if any(a.startswith("issue:") for a in in_work):     # 이슈에 이어진 작업 폴더 — 그것으로 충분하다
            continue
        linked = False
        for b in s["keys"].get("branches", []):
            for a in branch_anchor.get(b, ()):
                units.add(a, s["obs_id"], "key", "medium", ["branch"])
                linked = True
        if linked:
            continue
        # 키 없는 작업 폴더의 세션도 경로 겹침으로 커밋을 찾는다 — 그 커밋이 아무 데도 안 이어졌으면 그 작업에 들인다
        linked = bool(in_work)
        own = set(in_work) or {f"ai_session:{s['objects'][0]['id']}"}
        spaths = set(s["keys"].get("paths", []))
        if spaths:
            for ts, oid in commit_times[bisect.bisect_left(commit_ts_only, start):]:
                if ts > end + link_after:
                    break
                overlap = spaths & set(by_id[oid]["keys"].get("paths", []))
                if not overlap:
                    continue
                conf = "medium" if len(overlap) >= 2 or len(overlap) / len(spaths) >= 0.5 else "low"
                targets = commit_anchor.get(oid) or own
                for a in targets:
                    units.add(a, s["obs_id"], "probabilistic", conf, ["time_window", "path_overlap"])
                    if a.startswith(("ai_session:", "work:")):
                        units.add(a, oid, "probabilistic", conf, ["time_window", "path_overlap"])
                linked = True
        if not linked and (s["metrics"].get("edits", 0) > 0
                           or s["metrics"].get("prompts", 0) >= int(sc["session_wu_min_prompts"])):
            units.add(f"ai_session:{s['objects'][0]['id']}", s["obs_id"], "key", "high", ["session"])

    # WU 골격
    states = {k: replay_issue(v, as_of, window) for k, v in issue_events.items()}
    wus: dict[str, dict] = {}
    for anchor, members in units.members.items():
        mobs = [by_id[oid] for oid in members]
        times = [parse_ts(o["ts"]) for o in mobs]
        for o in mobs:
            if o["obs_id"] in session_end:
                times.append(session_end[o["obs_id"]])
        start, last = min(times), max(times)
        state = states.get(anchor[6:]) if anchor.startswith("issue:") else None
        wsnaps = [o for o in mobs if o["kind"] == "workspace.snapshot"]
        declared = [o for o in wsnaps if (o.get("labels", {}).get("status") or [""])[0]]
        work_status = None
        if state and state["created"]:
            start = min(start, parse_ts(state["created"]))
            end = parse_ts(state["end"]) if state["end"] else None
            basis = "issue"
        elif declared:                          # 트래커가 없으면 작업 기록(상태를 적은 것)이 기간·상태의 출처다
            start, end, work_status = work_span(declared, start)
            basis = "work"
        else:
            end, basis = last, "last_activity"
        mcommits = [o for o in mobs if o["kind"] == "vcs.commit"]
        labels = state["labels"] if state else {}
        types = {t.lower() for t in labels.get("type", [])}
        title = _title(anchor, mobs, excerpts)
        doc_like = bool(title and patterns.fix_exclude and patterns.fix_exclude.search(title))
        is_fix = not doc_like and (bool(types & bug_types) if types else any(commit_is_fix(o) for o in mcommits))
        wus[anchor] = {
            "anchor": anchor, "start": start, "end": end, "basis": basis, "state": state, "labels": labels,
            "is_fix": is_fix, "doc_like": doc_like, "commits": mcommits, "members": members, "mobs": mobs,
            "work_status": work_status, "works": sorted({o["objects"][0]["id"] for o in wsnaps}),
            "sig": defaultdict(list), "followups": set(), "induced_by": set(), "suspects": set(), "suspected_by": set(),
        }
    for w in wus.values():                     # 신호는 시각과 함께 모은다 — 결과에는 열린 창 안의 것만 쓴다
        for t in (w["state"] or {}).get("reopen_ts", []):
            w["sig"]["reopened"].append(parse_ts(t))

    anchors_of = units.anchors_of()

    # 사후 신호: 롤백
    rollbacks = 0
    for c in commits:
        for ref in c["keys"].get("reverts", []):
            inst = c["source"]["instance"]
            targets = [oid for (i, sha), oid in sha_index.items() if i == inst and sha.startswith(ref)]
            for oid in targets:
                for a in anchors_of.get(oid, ()):
                    w = wus[a]
                    if c["obs_id"] not in w["members"] or (w["end"] and parse_ts(c["ts"]) > w["end"]):
                        w["sig"]["rolled_back"].append(parse_ts(c["ts"]))
                        rollbacks += 1

    # 사후 신호: 후속 수정 — ① 명시 연결 (두 이슈 모두에 연결이 남으므로 쌍으로 센다).
    # 신호의 시각은 연결이 **관측된** 시각(가장 이른 쪽)이다 — 확정 뒤에 만든 연결이 확정 결과를 바꾸지 않게.
    link_seen: dict[tuple[str, str], dt.datetime] = {}
    for anchor, w in wus.items():
        for phrase, target, seen_at in (w["state"] or {}).get("links", []):
            t = f"issue:{target}"
            if t not in wus:
                continue
            if INDUCER_PHRASES.match(phrase):
                inducer, fix = t, anchor
            elif INDUCES_PHRASES.match(phrase):
                inducer, fix = anchor, t
            else:
                continue
            t = parse_ts(seen_at)
            if (inducer, fix) not in link_seen or t < link_seen[(inducer, fix)]:
                link_seen[(inducer, fix)] = t
    link_pairs = set(link_seen)
    for (inducer, fix), t in sorted(link_seen.items()):
        wus[inducer]["followups"].add(fix)
        wus[inducer]["sig"]["followup_fix"].append(max(t, wus[fix]["start"]))   # 연결도 결함 보고도 있어야 안다
        wus[fix]["induced_by"].add(inducer)

    # ② SZZ 근사 — 파일 수준 마지막 수정자
    touches: dict[str, list[tuple[dt.datetime, str]]] = defaultdict(list)
    for c in commits:
        if c["kind"] == "vcs.commit" and 0 < c["metrics"].get("files", 0) <= max_files:
            for path in c["keys"].get("paths", []):
                if not ignored(path) and not is_test(path):
                    touches[path].append((parse_ts(c["ts"]), c["obs_id"]))
    for lst in touches.values():
        lst.sort()
    szz = 0
    for anchor, f in wus.items():
        if not f["is_fix"] or not f["commits"]:
            continue
        reported = f["start"]
        fpaths = {p for c in f["commits"] if c["metrics"].get("files", 0) <= max_files
                  for p in c["keys"].get("paths", []) if not ignored(p) and not is_test(p)}
        for path in fpaths:
            prior = [(ts, oid) for ts, oid in touches.get(path, []) if ts < reported and anchor not in anchors_of.get(oid, ())]
            if not prior:
                continue
            ts, oid = prior[-1]
            for a in anchors_of.get(oid, ()):
                w = wus[a]
                limit = (w["end"] or as_of) + window
                if a != anchor and not w["doc_like"] and reported <= limit and anchor not in w["followups"]:
                    if anchor not in w["suspects"]:
                        szz += 1
                    w["suspects"].add(anchor)
                    w["sig"]["followup_suspected"].append(reported)
                    f["suspected_by"].add(a)

    # 출력
    out = []
    for anchor in sorted(wus):
        w = wus[anchor]
        end = w["end"]
        settled = bool(end and as_of >= end + window)
        # 열린 창(종료 + open_window_days) 안의 사후 사건만 신호·결과가 된다 — 확정 뒤에는 바뀌지 않는다
        w["signals"] = {name for name, tss in w["sig"].items() if tss and (end is None or min(tss) <= end + window)}
        resolution = (w["state"] or {}).get("resolution")
        if end is None:
            outcome = None
        elif "rolled_back" in w["signals"]:
            outcome = "rolled_back"
        elif "followup_fix" in w["signals"]:
            outcome = "followup_fix"
        elif "reopened" in w["signals"]:
            outcome = "reopened"
        elif (resolution and resolution.lower() in abandoned) or w["work_status"] == "dropped":
            outcome = "abandoned"
        else:
            outcome = "clean"
        paths_ = sorted({p for c in w["commits"] for p in c["keys"].get("paths", [])})
        sess = [o for o in w["mobs"] if o["kind"] == "ai_session.session"]
        out.append({
            "wu_id": f"wu:{anchor}",
            "anchors": [anchor],
            "span": {
                "start": w["start"].isoformat(timespec="seconds"),
                "end": end.isoformat(timespec="seconds") if end else None,
                "end_basis": w["basis"],
                "open_window_until": (end + window).isoformat(timespec="seconds") if end else None,
                "settled": settled,
            },
            "members": sorted(w["members"].values(), key=lambda m: m["obs_id"]),
            "sources": sorted({o["source"]["system"] for o in w["mobs"]}),
            "stats": {
                "duration_h": round((end - w["start"]).total_seconds() / 3600, 1) if end else None,
                "n_changes": len(w["commits"]),
                "n_files": len(paths_),
                "insertions": sum(c["metrics"].get("insertions", 0) for c in w["commits"]),
                "deletions": sum(c["metrics"].get("deletions", 0) for c in w["commits"]),
                "roles": sorted({o["actor_role"] for o in w["mobs"]}),
                "labels": w["labels"],
                "resolution": resolution,
                "is_fix": w["is_fix"],
                "reopen_count": (w["state"] or {}).get("reopen_count", 0),
                "late_reopens": (w["state"] or {}).get("late_reopens", 0),
                "open_now": w["state"]["open_now"] if w["state"] else end is None,   # 지금 상태(확정 결과와 별개)
                "followup_fix_wus": sorted(f"wu:{a}" for a in w["followups"]),
                "induced_by_wus": sorted(f"wu:{a}" for a in w["induced_by"]),
                "followup_suspected_wus": sorted(f"wu:{a}" for a in w["suspects"]),
                "suspected_induced_by_wus": sorted(f"wu:{a}" for a in w["suspected_by"]),
                "doc_like": w["doc_like"],
                "deterministic_signals": sorted(w["signals"]),
                "outcome": outcome,
                "outcome_final": settled,
                "ai_sessions": len(sess),
                "ai_edits": sum(o["metrics"].get("edits", 0) for o in sess),
                "ai_test_runs": sum(o["metrics"].get("test_runs", 0) for o in sess),
                "works": w["works"],
            },
            "build": {"stitch_recipe": RECIPE, "as_of": as_of.isoformat(timespec="seconds")},
        })

    # 보고 (P8: 고아·커버리지를 데이터로)
    linked = set(anchors_of)
    kinds = defaultdict(lambda: [0, 0])
    for o in obs:
        k = o["kind"]
        if k == "vcs.merge" and o["metrics"].get("sync_merge", not (     # 표시가 없는 옛 관측은 키로 짐작한다
                o["keys"].get("issue_keys") or o["keys"].get("mr_ids") or o["keys"].get("branches")
                or o["keys"].get("commits"))):
            k = "vcs.merge(sync)"
        kinds[k][1] += 1
        kinds[k][0] += o["obs_id"] in linked
    countable = [o for o in obs if not o["kind"].startswith("issue.") and o["kind"] != "workspace.snapshot"]   # 스스로 앵커인 것은 빼고 센다
    orphans = sum(1 for o in countable if o["obs_id"] not in linked)
    vcs_commits = [c for c in commits if c["kind"] == "vcs.commit"]
    report = {
        "recipe": RECIPE,
        "as_of": as_of.isoformat(timespec="seconds"),
        "observations": len(obs),
        "wus": len(out),
        "by_anchor": dict(sorted(_count(a.split(":", 1)[0] for a in wus).items())),
        "ended": sum(1 for u in out if u["span"]["end"]),
        "settled": sum(1 for u in out if u["span"]["settled"]),
        "multi_source_ratio": round(sum(1 for u in out if len(u["sources"]) > 1) / len(out), 3) if out else None,
        "orphan_ratio": round(orphans / len(countable), 3) if countable else None,
        "linked_by_kind": {k: {"linked": v[0], "total": v[1]} for k, v in sorted(kinds.items())},
        "commit_key_coverage": round(sum(1 for c in vcs_commits if c["keys"].get("issue_keys")) / len(vcs_commits), 3)
        if vcs_commits else None,
        "signals": {"followup_links": len(link_pairs), "followup_suspected_szz": szz, "rollbacks": rollbacks,
                    "reopened_wus": sum(1 for u in out if "reopened" in u["stats"]["deterministic_signals"])},
    }
    return out, report


def _title(anchor: str, mobs: list[dict], excerpts: dict[str, str]) -> str:
    """WU 제목: 이슈 요약(생성 사건 발췌의 첫 줄), 없으면 작업 기록의 제목, 없으면 첫 커밋 제목. 없으면 빈 문자열."""
    first = [o for o in mobs if o["kind"] == "issue.created"] or sorted(
        (o for o in mobs if o["kind"] == "workspace.snapshot" and excerpts.get(o["obs_id"])), key=lambda o: o["obs_id"]) or sorted(
        (o for o in mobs if o["kind"] in ("vcs.commit", "vcs.merge")), key=lambda o: (parse_ts(o["ts"]), o["obs_id"]))
    text = excerpts.get(first[0]["obs_id"], "") if first else ""
    return text.strip().split("\n", 1)[0] if text else ""


def _count(items) -> dict:
    d: dict = defaultdict(int)
    for i in items:
        d[i] += 1
    return d


def parse_as_of(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.now().astimezone().replace(microsecond=0)
    ts = dt.datetime.fromisoformat(value)
    return ts if ts.tzinfo else ts.astimezone()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas stitch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--as-of", help="이 시각까지의 관측만 본다 (기본: 지금)")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    p = paths(args.root)
    cfg = configmod.load(p.root)
    excerpts = {e["obs_id"]: e["text"] for e in read_jsonl(p.excerpts)} if p.excerpts.exists() else {}
    units, report = stitch(read_observations(p.observations), cfg, parse_as_of(args.as_of), excerpts)
    write_jsonl(Path(args.out) if args.out else p.stitched, units)
    write_text(p.stitched.with_name("stitch_report.json"), json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if not args.out and p.units.exists():               # 옛 current 는 이제 stitched 와 어긋난다 — 소비가 섞어 읽지 않게
        p.units.unlink()
        print("참고: 옛 current.jsonl 을 지웠다 — 소비는 stitched 를 읽는다. 주석·사후 검증까지는 atlas build", file=sys.stderr)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
