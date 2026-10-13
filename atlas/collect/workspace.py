"""작업 공간 수집기 — 작업 폴더 하나가 관측 하나 (workspace.snapshot). 형식과 이유는 WORKSPACE.md.

작업 폴더는 사람이 자기 말로 남긴 문제·처리·결과가 있는 곳이고, 티켓 없이 한 일도 여기에는 남는다(METHODOLOGY.md §10).
  keys     issue_keys(front-matter 의 key, 비었으면 폴더 이름) · works(작업 객체 id — Claude Code 세션이 같은 id 를 남긴다)
  labels   front-matter 의 status · started · closed · repos · kind · tags — 적힌 그대로(해석 없음)
  metrics  files · bytes · 역할별 파일 수(record · instructions · plan · decision · evidence · output · other)
           · sensitive(비밀값으로 보이는 파일 — 열지 않는다) · repos_inside(폴더 안의 저장소 사본)
           · has_problem · has_process · has_result · has_retro · record_chars · record_error
  발췌     작업 기록의 제목·문제·결과·회고·처리와 첫 결정 기록 본문의 앞부분 (가린 뒤 자른다). excerpt=false 로 끈다.
- 내용이 바뀌면 새 관측이 생긴다(obs_id 끝의 상태 해시). 읽는 쪽은 작업별 **최신 하나**만 쓴다 — AI 세션과 같은 방식.
  같은 상태를 다시 수집하면 obs_id 가 같아 저장소가 건너뛴다. 예전 상태로 되돌아간 것(되돌린 편집)은 직전 관측을 해시에
  더해 새 관측으로 남긴다 — 그러지 않으면 옛 obs_id 와 겹쳐 버려지고 그 뒤의 상태가 최신으로 남는다.
  원천(폴더)이 살아 있으므로 언제든 다시 찍을 수 있다.
- ts 는 관측한 파일들의 마지막 수정 시각(수집 시각을 넘지 않게) — 그 시각까지 쓰인 내용이라 시간 분할에 미래가 새지 않는다.
- 읽지 않는 것: scratch/·tmp/, atlas 가 만든 CONTEXT.md(요약을 다시 요약하지 않는다), 폴더 안의 저장소 사본,
  비밀값으로 보이는 파일, 기록·결정 밖 파일의 내용. 경로는 루트 기준으로만 남기고 루트 자체(홈 폴더·계정 이름)는 남기지 않는다.
- 읽기만 한다. 작업 폴더에 쓰지 않는다.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re

from . import Result, excerpt
from .. import workspace as ws
from ..common import dumps, now_iso, parse_ts

COLLECTOR = "workspace@1"
ABS_PATH = re.compile(r"^(?:~|/|[A-Za-z]:[\\/])|\\")
LABEL_FIELDS = ("status", "started", "closed", "repos", "kind", "tags")
SECTION_CAPS = (("problem", "문제", 500), ("result", "결과", 400), ("retro", "회고", 300),
                ("decisions", "결정", 300), ("process", "처리", 300))


def add_args(ap) -> None:
    g = ap.add_argument_group("workspace (설정 없이 바로 수집)")
    g.add_argument("--workspace-root", help="작업 공간 루트 — 그 아래 폴더 하나가 일 하나")


def source_from_args(args) -> dict | None:
    if getattr(args, "workspace_root", None) and args.kind in ("workspace", "all"):
        return {"name": "ws", "root": args.workspace_root}
    return None


def _excerpt_text(rec: ws.Record | None, decisions: str) -> str:
    if rec is None:
        return ""
    parts = [rec.title] if rec.title else []
    secs = {**rec.sections, "decisions": decisions.strip()}
    for sec, name, cap in SECTION_CAPS:
        text = " ".join(secs.get(sec, "").split())
        if text:
            parts.append(f"{name}: {text[:cap]}")
    return "\n".join(parts)


def _label(value: str) -> str:
    """front-matter 값에 절대·홈 경로(계정 이름이 든다)가 있으면 마지막 이름만 남긴다 — repos: [/Users/kim/src/api] → api."""
    return re.split(r"[\\/]", value.rstrip("\\/"))[-1] or value if ABS_PATH.search(value) else value


def history(p, name: str) -> dict[str, list[tuple]]:
    """이 소스의 지난 스냅숏 — 작업 객체 id → [(수집 시각, obs_id)]. 되돌아간 상태를 새 관측으로 남기는 데만 쓴다."""
    out: dict[str, list[tuple]] = {}
    needle = f'"instance":{json.dumps(name, ensure_ascii=False)}'
    for f in sorted(p.observations.glob("*.jsonl")) if p.observations.exists() else []:
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                if '"workspace.snapshot"' not in line or needle not in line:
                    continue
                o = json.loads(line)
                if o.get("kind") == "workspace.snapshot" and o["source"]["instance"] == name and o.get("objects"):
                    out.setdefault(o["objects"][0]["id"], []).append((parse_ts(o["collected_at"]), o["obs_id"]))
    return out


def _content(obs_id: str) -> str:
    return obs_id.rsplit("#", 1)[-1].split("~", 1)[0]


def snapshot(src: ws.Source, work_dir, rel: str, patterns, collected_at: str, *, wid: str | None = None,
             past: list[tuple] | None = None, max_files: int = 5000, use_excerpt: bool = True,
             excerpt_chars: int = 1500) -> tuple[dict, str | None]:
    """작업 폴더 하나 → (관측, 발췌). 같은 상태·같은 이력이면 언제 불러도 같은 obs_id·같은 바이트.
    wid 는 work_ids 가 가른 작업 id(없으면 front-matter id·경로), past 는 그 작업의 지난 스냅숏(history)."""
    rec = ws.read_record(work_dir / src.record)
    inv = ws.inventory(src, work_dir, max_files)
    wid = wid or ws.work_id(src, work_dir, rel, rec)
    obj = src.object_id(wid)
    front = rec.front if rec else {}
    declared = patterns.issue_keys(*rec.keys()) if rec else []
    keys = declared or patterns.issue_keys(work_dir.name)
    labels = {}
    for f in LABEL_FIELDS:
        vals = ws._list(front.get(f)) if f in ("repos", "kind", "tags") else [v for v in [ws._str(front.get(f))] if v]
        if f == "status":
            vals = [v.lower() for v in vals]
        vals = [_label(v) for v in vals]
        if vals:
            labels[f] = vals
    metrics = {"files": sum(inv.counts.values()), "bytes": inv.bytes, **inv.counts,
               "sensitive": inv.sensitive, "repos_inside": inv.repos_inside,
               "record_chars": rec.chars if rec else 0, "record_error": bool(rec and rec.error)}
    for sec in ("problem", "process", "result", "retro"):
        metrics[f"has_{sec}"] = bool(rec and sec in rec.sections)
    # 결정 기록은 형식이 제각각이다(ADR 의 Context·Decision 등) — 섹션을 고르지 않고 본문 앞부분을 쓴다
    decisions = ws.read_body(work_dir / inv.decision_files[0]) if inv.decision_files else ""
    text = excerpt(_excerpt_text(rec, decisions), excerpt_chars) if use_excerpt else ""
    state = dumps({"keys": keys, "labels": labels, "metrics": metrics, "excerpt": text, "files": inv.files,
                   "id": wid, "rel": rel})
    collected = parse_ts(collected_at)
    last = dt.datetime.fromtimestamp(inv.last_mtime or work_dir.stat().st_mtime).astimezone()
    ts = min(last, collected).isoformat(timespec="seconds")
    content = hashlib.sha256(state.encode("utf-8")).hexdigest()[:12]
    base = f"workspace:{src.name}:{wid}"
    latest = max(past) if past else None
    if latest and _content(latest[1]) == content:
        obs_id = latest[1]                                  # 바뀐 것 없음 — 같은 관측
    elif past and any(_content(o) == content for _, o in past):
        obs_id = f"{base}#{content}~{hashlib.sha256(latest[1].encode('utf-8')).hexdigest()[:6]}"   # 예전 상태로 되돌아감
    else:
        obs_id = f"{base}#{content}"
    event = {
        "obs_id": obs_id,
        "source": {"system": "workspace", "instance": src.name, "collector_kind": "cli"},
        "kind": "workspace.snapshot",
        "ts": ts,
        "actor_role": "self",
        "objects": [{"type": "work", "id": obj, "url": rel}],
        "keys": {k: v for k, v in (("issue_keys", keys), ("works", [obj])) if v},
        "labels": labels,
        "metrics": metrics,
        "excerpt_ref": None,
        "partial": inv.partial,
        "collector": COLLECTOR,
        "collected_at": collected_at,
    }
    return event, (text or None)


def collect(p, cfg: dict, source: dict, patterns, state: dict) -> Result:
    src = ws.Source.from_config(source, p.root)
    result = Result()
    if not src.root.is_dir():
        result.notes.append(f"{src.root} 가 없다 — 작업 공간 루트(root)를 확인하라")
        return result
    collected_at = now_iso()
    works = ws.find_works(src)
    ids = ws.work_ids(src, works)
    past = history(p, src.name)
    sensitive = no_record = 0
    shared = sum(1 for d, rel in works if ids[rel] != ws.work_id(src, d, rel))
    for work_dir, rel in works:
        event, text = snapshot(src, work_dir, rel, patterns, collected_at, wid=ids[rel],
                               past=past.get(src.object_id(ids[rel])),
                               max_files=int(source.get("max_files", 5000)),
                               use_excerpt=source.get("excerpt", True),
                               excerpt_chars=int(source.get("excerpt_chars", 1500)))
        result.events.append(event)
        if text:
            result.excerpts[event["obs_id"]] = text
        sensitive += event["metrics"]["sensitive"]
        no_record += event["metrics"]["record"] == 0
    result.state = {"collected_at": collected_at, "works": len(works)}
    if no_record:
        result.notes.append(f"작업 기록({src.record})이 없는 폴더 {no_record}개 — 폴더 이름의 키로만 잇는다 (atlas work status)")
    if shared:
        result.notes.append(f"front-matter id 가 다른 폴더와 같은 작업 {shared}개 — 경로로 구분했다 (atlas work status)")
    if sensitive:
        result.notes.append(f"비밀값으로 보이는 파일 {sensitive}개 — 열지 않았다. 작업 폴더에 자격 증명을 두지 않는다")
    return result
