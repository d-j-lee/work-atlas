"""카탈로그 — 데이터와 지식에 대한 데이터(메타). "지금 무엇을 얼마나 믿을 수 있나"를 한 장으로.

  python -m atlas catalog [--json]

담는 것 (data/model/catalog.json):
  sources   소스별 관측 수·종류·기간·마지막 수집·신선도·부분 수집·수집기 버전·키 커버리지
  stitch    WU 수·앵커 종류·확정 수·다중 소스 비율·고아 비율·사후 신호 수 (stitch_report.json)
  outcomes  WU 결과 분포 (확정만)
  claims    주장 종류×강도 · 이전 세대 대비 변화 (generation.json)
  ai        AI 세션: 수·작업단위 연결 비율·편집·테스트 실행 비율·도구 오류율·압축 횟수
  usage     트리아지·판정·자기 보고 건수
  warnings  정책 임계치(policy.md §7)·운영 위험에 걸린 것과 권하는 조치 — 사람이 볼 곳은 여기다
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter, defaultdict

from . import config as configmod
from .common import load_units, parse_ts, paths, read_observations, write_text
from .stitch import latest_sessions


def _load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


STATE_SYSTEM = {"git": "vcs", "jira": "issue", "claude-code": "ai_session"}


def last_runs(p) -> dict[tuple, dt.datetime]:
    """수집기의 마지막 성공 시각(data/state/<종류>-<이름>.json). 새 사건이 없는 조용한 소스도 '수집은 돌았다'를 안다."""
    out = {}
    for f in sorted(p.state.glob("*.json")) if p.state.exists() else []:
        for kind, system in STATE_SYSTEM.items():
            if f.stem.startswith(kind + "-"):
                try:
                    ts = json.loads(f.read_text(encoding="utf-8")).get("collected_at")
                    if ts:
                        out[(system, f.stem[len(kind) + 1:])] = parse_ts(ts)
                except (json.JSONDecodeError, ValueError):
                    pass
    return out


def build(p, cfg: dict, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now().astimezone()
    observations = read_observations(p.observations)
    units = load_units(p)
    stitch_report = _load(p.stitched.with_name("stitch_report.json")) or {}
    gen = _load(p.model / "generation.json") or {}
    linked = {m["obs_id"] for u in units for m in u.get("members", [])}

    groups: dict[tuple, list] = defaultdict(list)
    for o in observations:
        groups[(o["source"]["system"], o["source"]["instance"])].append(o)
    last_run = last_runs(p)
    sources = []
    for (system, inst), obs in sorted(groups.items()):
        ts = [parse_ts(o["ts"]) for o in obs]
        collected = max([parse_ts(o["collected_at"]) for o in obs]
                        + ([last_run[(system, inst)]] if (system, inst) in last_run else []))
        entry = {
            "system": system, "instance": inst, "observations": len(obs),
            "by_kind": dict(sorted(Counter(o["kind"] for o in obs).items())),
            "first_ts": min(ts).isoformat(timespec="seconds"), "last_ts": max(ts).isoformat(timespec="seconds"),
            "last_collected_at": collected.isoformat(timespec="seconds"),
            "freshness_days": round((now - collected).total_seconds() / 86400, 1),
            "partial": sum(1 for o in obs if o["partial"]),
            "collectors": sorted({o["collector"] for o in obs}),
            "collector_kinds": sorted({o["source"]["collector_kind"] for o in obs}),
            "linked_ratio": round(sum(1 for o in obs if o["obs_id"] in linked) / len(obs), 3) if units else None,
        }
        if system == "vcs":
            commits = [o for o in obs if o["kind"] == "vcs.commit"]
            if commits:
                entry["commit_key_coverage"] = round(sum(1 for o in commits if o["keys"].get("issue_keys")) / len(commits), 3)
                entry["commit_mr_coverage"] = round(sum(1 for o in commits if o["keys"].get("mr_ids")) / len(commits), 3)
        sources.append(entry)

    sessions = latest_sessions(observations)
    ai = None
    if sessions:
        m = [s["metrics"] for s in sessions]
        calls = sum(x.get("tool_calls", 0) for x in m)
        ai = {
            "sessions": len(sessions),
            "linked_ratio": round(sum(1 for s in sessions if s["obs_id"] in linked) / len(sessions), 3) if units else None,
            "with_edits": sum(1 for x in m if x.get("edits", 0) > 0),
            "with_tests": sum(1 for x in m if x.get("test_runs", 0) > 0),
            "test_ratio_of_editing": round(sum(1 for x in m if x.get("edits", 0) and x.get("test_runs", 0))
                                           / max(1, sum(1 for x in m if x.get("edits", 0))), 3),
            "tool_error_rate": round(sum(x.get("tool_errors", 0) for x in m) / calls, 3) if calls else None,
            "compactions": sum(x.get("compactions", 0) for x in m),
            "newest": max(parse_ts(s["ts"]) for s in sessions).isoformat(timespec="seconds"),
            "oldest": min(parse_ts(s["ts"]) for s in sessions).isoformat(timespec="seconds"),
        }

    outcomes = Counter(u["stats"].get("outcome") or "진행 중" for u in units if u["span"].get("settled")
                       or not u["span"].get("end"))
    usage = {
        "triages": sum(1 for o in observations if o["kind"] == "atlas.triage"),
        "feedback": sum(1 for o in observations if o["kind"] == "atlas.feedback"),
        "self_reports": sum(1 for o in observations if o["kind"].startswith("self_report.")),
    }
    cat = {
        "built_at": now.isoformat(timespec="seconds"),
        "generation": gen.get("generation"),
        "as_of": gen.get("as_of") or stitch_report.get("as_of"),
        "sources": sources,
        "stitch": stitch_report,
        "outcomes": dict(sorted(outcomes.items())),
        "claims": gen.get("claims"),
        "drift": gen.get("drift"),
        "ai": ai,
        "usage": usage,
    }
    cat["warnings"] = warnings(cat, cfg, now)
    return cat


def warnings(cat: dict, cfg: dict, now: dt.datetime) -> list[dict]:
    """문턱은 atlas.toml [policy] 에서 읽는다(의미는 policy.md §7). 각 경고에는 근거 수치와 권하는 조치를 붙인다."""
    pol = cfg["policy"]
    out = []

    def warn(level, what, evidence, action):
        out.append({"level": level, "what": what, "evidence": evidence, "action": action})

    for s in cat["sources"]:
        if s["system"] in ("atlas", "self_report"):
            continue
        if s["freshness_days"] > float(pol["stale_days"]):
            warn("주의", f"{s['system']}:{s['instance']} 수집이 오래됐다", f"마지막 수집 {s['freshness_days']}일 전",
                 "주간 수확(atlas collect all)이 돌고 있는지 확인")
        if s["partial"]:
            warn("정보", f"{s['system']}:{s['instance']} 부분 수집 {s['partial']}건", "partial=true",
                 "그 위의 통계는 표본이다 — 리포트에 밝힌다(CLAUDE.md 규칙 10)")
        cov = s.get("commit_key_coverage")
        if cov is not None and cov < float(pol["key_coverage_min"]):
            warn("제안", f"{s['instance']} 커밋의 이슈 키 커버리지가 낮다", f"{cov:.0%}",
                 "브랜치 이름이나 커밋 제목에 이슈 키를 넣는 습관 — 상관·사후 귀속의 정확도가 바로 오른다")
    st = cat.get("stitch") or {}
    if st.get("orphan_ratio") is not None and st["orphan_ratio"] > float(pol["orphan_ratio_max"]):
        warn("주의", f"고아 관측 비율이 높다 (> {float(pol['orphan_ratio_max']):.0%})", f"{st['orphan_ratio']:.0%}",
             "키 없는 커밋·세션이 많다 — 키 습관을 들이거나 상관 규칙(stitch)을 점검")
    ai = cat.get("ai")
    if ai:
        age = (now - parse_ts(ai["newest"])).days
        if age > float(pol["transcript_stale_days"]):
            warn("주의", "Claude Code 기록 수집이 멈춘 것 같다", f"가장 최근 세션 {age}일 전",
                 "기본 30일 뒤 기록이 지워진다 — 수집 주기를 확인(atlas collect claude-code)")
        if ai["linked_ratio"] is not None and ai["linked_ratio"] < 0.3 and ai["sessions"] >= 10:
            warn("제안", "AI 세션 대부분이 작업단위에 안 이어진다", f"연결 {ai['linked_ratio']:.0%}",
                 "세션 첫 프롬프트에 이슈 키를 쓰거나, 이슈 브랜치에서 세션을 연다")
    d = cat.get("drift")
    if d and d.get("removed", 0) > 0.3 * max(1, sum(sum(v.values()) for v in (cat.get("claims") or {}).values())):
        warn("주의", "이전 세대 대비 주장이 많이 사라졌다", f"-{d['removed']}",
             "수집 범위·설정 변경 여부를 확인 — 의도한 변화가 아니면 재수집")
    if not cat["sources"]:
        warn("주의", "관측이 없다", "data/observations 비어 있음", "atlas collect all 부터")
    return out


def render_text(cat: dict) -> str:
    lines = [f"세대 {cat['generation'] or '-'} · 기준 시각 {cat['as_of'] or '-'}", "", "소스"]
    for s in cat["sources"]:
        extra = ""
        if "commit_key_coverage" in s:
            extra = f" · 키 {s['commit_key_coverage']:.0%} · MR {s['commit_mr_coverage']:.0%}"
        linked = f" · 연결 {s['linked_ratio']:.0%}" if s.get("linked_ratio") is not None else ""
        lines.append(f"  {s['system']}:{s['instance']:<14} {s['observations']:>7}건 · {s['first_ts'][:10]}~{s['last_ts'][:10]}"
                     f" · 수집 {s['freshness_days']}일 전{linked}{extra}{' · 부분 ' + str(s['partial']) if s['partial'] else ''}")
    st = cat.get("stitch") or {}
    if st:
        lines += ["", f"작업단위 {st.get('wus')} ({', '.join(f'{k} {v}' for k, v in st.get('by_anchor', {}).items())})"
                  f" · 확정 {st.get('settled')} · 다중 소스 {st.get('multi_source_ratio')} · 고아 {st.get('orphan_ratio')}"]
    if cat["outcomes"]:
        lines.append("결과(확정·진행 중)  " + ", ".join(f"{k} {v}" for k, v in cat["outcomes"].items()))
    if cat.get("claims"):
        lines.append("주장  " + " · ".join(f"{k} {sum(v.values())}({'/'.join(f'{s[0]}{n}' for s, n in v.items())})"
                                          for k, v in cat["claims"].items()))
    if cat.get("drift"):
        d = cat["drift"]
        lines.append(f"변화  +{d['added']} -{d['removed']} ↑{d['strengthened']} ↓{d['weakened']}")
    if cat.get("ai"):
        a = cat["ai"]
        lines.append(f"AI 세션 {a['sessions']} · 연결 {a['linked_ratio']} · 편집 세션 중 테스트 실행 {a['test_ratio_of_editing']:.0%}"
                     f" · 도구 오류율 {a['tool_error_rate']}")
    u = cat["usage"]
    lines.append(f"사용  트리아지 {u['triages']} · 판정 {u['feedback']} · 자기 보고 {u['self_reports']}")
    if cat["warnings"]:
        lines += ["", "경고·제안"]
        lines += [f"  [{w['level']}] {w['what']} ({w['evidence']}) → {w['action']}" for w in cat["warnings"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas catalog", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    p = paths(args.root)
    cat = build(p, configmod.load(p.root))
    write_text(p.model / "catalog.json", json.dumps(cat, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(cat, ensure_ascii=False, indent=2) if args.json else render_text(cat))
    return 0


if __name__ == "__main__":
    sys.exit(main())
