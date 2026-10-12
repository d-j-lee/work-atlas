"""마이닝 입력 — 관측·작업단위·설정을 한 번 읽어 모든 마이너가 같은 기준으로 쓰게 한다.

- 경로는 이름 바뀜을 따라 **가장 최근 이름**으로 정규화한다(옛 이름의 이력이 새 이름에 이어진다).
- 장부 파일(mining.ignore_paths)은 뺀다. 테스트 파일은 표시만 한다(mining.test_paths).
- 결함 수정 커밋: 유형이 있는 이슈 WU 에 속하면 **그 유형이 정한다**(stitch 와 같은 기준).
  그런 WU 가 없을 때만 커밋 메시지(발췌가 있으면 지금 설정으로)로 판정한다.
- as_of 이후는 보지 않는다 — stitch 와 같은 시각 기준.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from .. import config as configmod
from ..common import parse_ts, path_filter


@dataclass
class Commit:
    obs_id: str
    scope: str
    ts: dt.datetime
    paths: list[str]
    churn: int
    is_fix: bool
    large: bool
    wus: list[str] = field(default_factory=list)


@dataclass
class Corpus:
    as_of: dt.datetime
    cfg: dict
    commits: list[Commit]
    units: list[dict]
    obs_by_id: dict[str, dict]
    excerpts: dict[str, str]
    is_test: object
    renames: dict[str, dict[str, str]]


def canonical_renames(commits: list[dict]) -> dict[str, dict[str, str]]:
    """저장소별 옛 경로 → 최신 경로. 연쇄 이름 바뀜(a→b→c)을 끝까지 따라간다."""
    edges: dict[str, dict[str, str]] = defaultdict(dict)
    for c in sorted(commits, key=lambda c: (parse_ts(c["ts"]), c["obs_id"])):
        for pair in c["keys"].get("renames", []):
            old, _, new = pair.partition("\t")
            if old and new and old != new:
                edges[c["source"]["instance"]][old] = new
    out: dict[str, dict[str, str]] = {}
    for scope, m in edges.items():
        resolved = {}
        for old in m:
            cur, seen = old, {old}
            while cur in m and m[cur] not in seen:
                cur = m[cur]
                seen.add(cur)
            resolved[old] = cur
        out[scope] = resolved
    return out


def build(observations: list[dict], units: list[dict], cfg: dict, as_of: dt.datetime,
          excerpts: dict[str, str] | None = None) -> Corpus:
    excerpts = excerpts or {}
    patterns = configmod.Patterns(cfg)
    ignored = path_filter(cfg["mining"]["ignore_paths"])
    max_files = int(cfg["mining"]["max_files_per_commit"])
    obs = [o for o in observations if parse_ts(o["ts"]) <= as_of]
    by_id = {o["obs_id"]: o for o in obs}
    raw = [o for o in obs if o["kind"] == "vcs.commit"]
    renames = canonical_renames(raw)
    fix_wu_members: dict[str, list[str]] = defaultdict(list)
    for u in units:
        for m in u.get("members", []):
            if m.get("link") == "key":
                fix_wu_members[m["obs_id"]].append(u["wu_id"])
    fix_wus = {u["wu_id"] for u in units if u.get("stats", {}).get("is_fix")}
    typed_wus = {u["wu_id"] for u in units if (u.get("stats", {}).get("labels") or {}).get("type")}
    commits = []
    for o in raw:
        scope = o["source"]["instance"]
        rmap = renames.get(scope, {})
        paths = list(dict.fromkeys(rmap.get(p, p) for p in o["keys"].get("paths", []) if not ignored(rmap.get(p, p))))
        text = excerpts.get(o["obs_id"])
        wus = fix_wu_members.get(o["obs_id"], [])
        msg_fix = patterns.is_fix(text) if text is not None else bool(o["metrics"].get("is_fix"))
        commits.append(Commit(
            obs_id=o["obs_id"], scope=scope, ts=parse_ts(o["ts"]), paths=paths,
            churn=int(o["metrics"].get("insertions", 0)) + int(o["metrics"].get("deletions", 0)),
            is_fix=(any(w in fix_wus for w in wus if w in typed_wus) if any(w in typed_wus for w in wus)
                    else msg_fix or any(w in fix_wus for w in wus)),
            large=int(o["metrics"].get("files", 0)) > max_files or bool(o.get("partial")),
            wus=sorted(wus),
        ))
    commits.sort(key=lambda c: (c.ts, c.obs_id))
    return Corpus(as_of=as_of, cfg=cfg, commits=commits, units=units, obs_by_id=by_id, excerpts=excerpts,
                  is_test=path_filter(cfg["mining"]["test_paths"]), renames=renames)
