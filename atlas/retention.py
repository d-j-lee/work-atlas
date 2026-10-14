"""보존 기한 — policy.md §2 의 '캐시 보존'을 엔진이 집행한다. 관측(L0)은 지우지 않는다(본문이 없다). 본문 캐시만 지운다.

  [retention]                      atlas.toml — 0 이면 지우지 않는다(기본)
  excerpt_days = 365               발췌 캐시(data/excerpts.jsonl): 사건 시각이 이보다 오래된 것의 본문
  excerpt_days_by_system = { chat = 90 }   원천 시스템별로 다르게(issue · vcs · ai_session · workspace · chat …)
  llm_cache_days = 180             data/llm/cache 의 답 파일(수정 시각 기준)
  report_days = 180                data/reports 의 생성물(업무 지도 · 안내서 · 수확 리포트 · 요약 · 실측)

발췌를 지우면 그 발췌로 만든 입력 문서 캐시(data/render)도 지운다 — 다음 빌드가 남은 발췌로 다시 만든다.
그 작업단위의 주석(LLM)은 입력이 바뀌어 '낡음'이 된다 — 몇 개인지 함께 돌려준다(다시 주석하면 비용이 든다).
기한은 정정으로 무효가 된 관측의 발췌에도 똑같이 적용한다(무효라고 본문이 영원히 남지 않게).
무엇을 몇 개 지웠는지만 돌려준다(본문은 남기지 않는다). atlas harvest 가 빌드 전에 부른다.
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import time

from .common import parse_ts, read_jsonl, write_text


def _days(cfg: dict, key: str) -> int:
    try:
        return max(0, int((cfg.get("retention") or {}).get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0


def purge(p, cfg: dict, now: dt.datetime | None = None, dry_run: bool = False) -> dict:
    now = now or dt.datetime.now().astimezone()
    ret = cfg.get("retention") or {}
    by_system = {str(k): int(v) for k, v in (ret.get("excerpt_days_by_system") or {}).items() if str(v).isdigit()}
    default = _days(cfg, "excerpt_days")
    out = {"excerpts": 0, "excerpts_kept": 0, "render_cleared": False, "annotations_stale": 0, "llm_cache": 0, "reports": 0}
    if (default or by_system) and p.excerpts.exists():
        when, voided = {}, set()                     # 정정 전의 날 L0 — 무효가 된 관측의 발췌도 기한을 따른다
        for path in sorted(p.observations.glob("*.jsonl")) if p.observations.exists() else []:
            for o in read_jsonl(path):
                when.setdefault(o["obs_id"], (parse_ts(o["ts"]), o["source"]["system"]))
                if o.get("kind", "").endswith(".correction"):
                    voided |= {x["id"] for x in o.get("objects", []) if x.get("type") == "obs"}
        keep, dropped, dropped_ids = [], 0, set()
        for line in p.excerpts.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                keep.append(line)                    # 고치지 않는다 — 읽는 쪽이 건너뛴다
                continue
            ts, system = when.get(row.get("obs_id"), (None, None))
            days = by_system.get(system or "", default)
            if ts is not None and days and (now - ts).days > days:
                dropped += 1
                dropped_ids.add(row.get("obs_id"))
            else:
                keep.append(line)
        out["excerpts"], out["excerpts_kept"] = dropped, len(keep)
        out["annotations_stale"] = _stale_annotations(p, dropped_ids - voided)   # 무효 관측은 정정이 이미 주석을 낡게 했다
        if dropped and not dry_run:
            write_text(p.excerpts, "\n".join(keep) + ("\n" if keep else ""))
            render_dir = p.root / "data" / "render"
            if render_dir.exists():
                shutil.rmtree(render_dir, ignore_errors=True)
                out["render_cleared"] = True
    for key, folder, pattern in (("llm_cache_days", p.root / "data" / "llm" / "cache", "**/*.json"),
                                 ("report_days", p.reports, "**/*")):
        days = _days(cfg, key)
        if not days or not folder.exists():
            continue
        limit = time.time() - days * 86400
        n = 0
        for f in folder.glob(pattern):
            if f.is_file() and f.stat().st_mtime < limit:
                n += 1
                if not dry_run:
                    f.unlink(missing_ok=True)
        out["llm_cache" if key == "llm_cache_days" else "reports"] = n
    return out


def _stale_annotations(p, dropped_ids: set) -> int:
    """지운 발췌를 품은 작업단위 중 주석이 있는 것 — 다음 빌드에서 '낡음'이 된다."""
    if not dropped_ids or not p.annotated.exists() or not p.stitched.exists():
        return 0
    annotated = {a["wu_id"] for a in read_jsonl(p.annotated) if a.get("annotation")}
    return sum(1 for u in read_jsonl(p.stitched)
               if u["wu_id"] in annotated and any(m["obs_id"] in dropped_ids for m in u.get("members", [])))


def describe(r: dict) -> str:
    parts = []
    if r["excerpts"]:
        parts.append(f"발췌 {r['excerpts']:,}개" + (" · 입력 문서 캐시" if r["render_cleared"] else "")
                     + (f" (주석 {r['annotations_stale']:,}개가 입력이 바뀌어 낡음이 된다 — 다시 주석할지 정한다)"
                        if r.get("annotations_stale") else ""))
    if r["llm_cache"]:
        parts.append(f"LLM 답 캐시 {r['llm_cache']:,}개")
    if r["reports"]:
        parts.append(f"리포트 {r['reports']:,}개")
    return ("보존 기한이 지나 지움: " + ", ".join(parts)) if parts else "보존 기한이 지난 것 없음"
