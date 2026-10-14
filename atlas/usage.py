"""사용 중 효용 — 훅·맥락이 낸 제안이 실제 일에서 반영됐나를 잰다. 사람 판정(20건)을 기다리지 않는 쓰임의 지표.

  python -m atlas usage [--root …] [--json]

기록 (data/usage/<연-월>.jsonl — 덧붙이기만, git 밖. 관측(L0)이 아니다: 세대 id 를 흔들지 않게)
  hook.edit      파일을 고친 직후 훅이 알린(또는 보류한) 동반 변경 후보 — 세션 · 고친 파일 · 후보 · arm(shown · held)
  context.issue  이슈 맥락(훅의 프롬프트 · atlas context · MCP)이 낸 '바꿀 가능성이 큰 파일' — 이슈 · 후보
보류군 ([usage] holdout, 기본 0)
  0.2 면 세션의 20% 에서 동반 변경 후보를 알리지 않고 기록만 한다. 세션 단위로 정한다(세션 id 의 해시) — 한 세션 안에서
  알림과 보류가 섞이면 알린 후보가 보류한 쪽 행동에 번져 차이가 흐려진다. 결함이 몰린 파일 경고는 보류하지 않는다.
  알린 쪽과 보류한 쪽의 반영률 차이가 훅이 실제로 행동을 바꾸는지다(인과에 가장 가까운 값). 판단은 양쪽 세션이
  [usage] min_sessions(기본 20) 이상일 때만 — 그 전에는 차이를 적기만 한다. [usage] record = false 면 보류하지 않는다(기록이 없으면 잴 수 없다).
결과 (수집된 관측과 맞춰 본다 — 아직 안 모인 것은 '미확정', 너무 오래 못 맞춘 것은 '만료'로 뺀다)
  hook.edit      후보 중 하나라도 그 세션에서 고쳤거나, 그 세션이 든 작업단위의 이후 커밋이 바꿨으면 '반영'
                 세션이 35일 안에 수집되지 않으면 만료
  context.issue  그 이슈의 작업단위가 이후 커밋으로 바꾼 파일 대비 정밀도 · 재현율. 같은 (이슈, 세션) 은 처음 한 번만 센다
                 이슈가 35일 안에 작업단위로 안 잡히거나 180일 넘게 안 끝나면 만료. 끝났는데 이후 커밋이 없으면 '바뀐 것 없음'으로 따로 센다
연관이다 — 보류군이 없으면 '훅이 맞혔다'이지 '훅 덕분에 바꿨다'가 아니다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import random
import sys
from pathlib import Path

from . import config as configmod
from .common import parse_ts, paths


def usage_dir(p) -> Path:
    return p.root / "data" / "usage"


def record(p, row: dict, cfg: dict | None = None) -> None:
    """한 줄 덧붙인다([usage] record = false 면 쓰지 않는다). 실패해도 조용히 — 훅을 막지 않는다."""
    if cfg is not None and not (cfg.get("usage") or {}).get("record", True):
        return
    try:
        now = dt.datetime.now().astimezone()
        f = usage_dir(p) / f"{now.strftime('%Y-%m')}.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"ts": now.isoformat(timespec="seconds"), **row}, ensure_ascii=False) + "\n")
    except OSError:
        pass


UNSEEN_DAYS = 35            # 이만큼 지나도 세션·이슈가 관측에 안 잡히면 만료(수집 설정 문제일 가능성이 크다)
OPEN_DAYS = 180             # 이슈 맥락: 일이 이만큼 지나도 안 끝나면 만료


def arm(cfg: dict, session: str | None) -> str:
    """세션 단위 무작위 배정 — 같은 세션은 늘 같은 쪽. 기록을 끄거나 세션을 모르면 보류하지 않는다
    (세션 id 가 없는 호출은 모두 한 덩어리라, 보류하면 그 덩어리 전체가 영영 알림을 못 받는다)."""
    u_cfg = cfg.get("usage") or {}
    share = float(u_cfg.get("holdout", 0) or 0)
    if share <= 0 or not u_cfg.get("record", True) or not session or session == "unknown":
        return "shown"
    u = int(hashlib.sha256(f"holdout|{session}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "held" if u < min(share, 0.5) else "shown"


def rows(p) -> list[dict]:
    """기록 줄 — 시각을 읽을 수 없거나 시간대가 없는 줄은 건너뛴다(손으로 고친 줄 · 다른 도구가 쓴 줄)."""
    out = []
    d = usage_dir(p)
    for f in sorted(d.glob("*.jsonl")) if d.exists() else []:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
                ts = parse_ts(r["ts"]) if isinstance(r, dict) and r.get("kind") and r.get("ts") else None
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
            if ts is not None and ts.tzinfo is not None:
                out.append({**r, "_ts": ts})
    return out


def _outcomes(p, observations: list[dict], units: list[dict]):
    from .stitch import latest_sessions
    sessions = {o["objects"][0]["id"]: o for o in latest_sessions(observations)}
    obs = {o["obs_id"]: o for o in observations}
    unit_of_session: dict[str, dict] = {}
    unit_of_issue: dict[str, dict] = {}
    for u in units:
        for m in u["members"]:
            o = obs.get(m["obs_id"])
            if o and o["kind"] == "ai_session.session":
                unit_of_session[o["objects"][0]["id"]] = u
        for a in u.get("anchors", []):
            if a.startswith("issue:"):
                unit_of_issue[a.split(":", 1)[1]] = u

    def changed_after(u: dict | None, ts: dt.datetime) -> set[str]:
        if not u:
            return set()
        return {path for m in u["members"] for o in [obs.get(m["obs_id"])] if o and o["kind"] in ("vcs.commit", "vcs.merge")
                and parse_ts(o["ts"]) >= ts for path in o["keys"].get("paths", [])}
    return sessions, unit_of_session, unit_of_issue, changed_after


def evaluate(p, cfg: dict, observations: list[dict] | None = None, units: list[dict] | None = None,
             now: dt.datetime | None = None) -> dict:
    from .common import load_units, read_observations
    observations = read_observations(p.observations) if observations is None else observations
    units = load_units(p) if units is None else units
    now = (now or dt.datetime.now()).astimezone()                  # 시간대 없는 now 는 이 컴퓨터의 시간대로
    sessions, unit_of_session, unit_of_issue, changed_after = _outcomes(p, observations, units)
    hook = {"shown": [], "held": []}
    ctx = []
    pending = expired = no_change = duplicates = 0
    seen_ctx: set[tuple[str, str]] = set()
    for r in rows(p):
        ts = r["_ts"]
        age = (now - ts).days
        if r["kind"] == "hook.edit":
            s = sessions.get(r.get("session"))
            if not s or parse_ts(s["collected_at"]) < ts:
                if age > UNSEEN_DAYS:
                    expired += 1
                else:
                    pending += 1
                continue
            done = set(s["keys"].get("paths", [])) | changed_after(unit_of_session.get(r["session"]), ts)
            hits = [x for x in r.get("suggested", []) if x in done]
            hook[r.get("arm", "shown")].append({"session": r["session"], "hit": bool(hits), "n": len(r.get("suggested", [])),
                                                "hits": len(hits)})
        elif r["kind"] == "context.issue":
            sug = r.get("suggested", [])
            if not sug:
                continue
            # 같은 세션에서 같은 이슈 맥락을 여러 번 봐도 한 번. 세션이 없는 호출(CLI · MCP)은 같은 날을 한 번으로
            dedupe = (str(r.get("issue", "")), str(r.get("session") or ts.date()))
            if dedupe in seen_ctx:
                duplicates += 1
                continue
            seen_ctx.add(dedupe)
            u = unit_of_issue.get(dedupe[0])
            if not u or not u["span"].get("end"):
                if age > (OPEN_DAYS if u else UNSEEN_DAYS):
                    expired += 1
                else:
                    pending += 1
                continue
            changed = changed_after(u, ts)
            if not changed:
                no_change += 1
                continue
            hits = len(set(sug) & changed)
            ctx.append({"precision": hits / len(sug), "recall": hits / len(changed)})
    out = {"pending": pending, "expired": expired, "no_change": no_change, "duplicates": duplicates, "hook": {}, "context": None}
    for a, xs in hook.items():
        if xs:
            out["hook"][a] = {"n": len(xs), "sessions": len({x["session"] for x in xs}),
                              "uptake": round(sum(x["hit"] for x in xs) / len(xs), 3),
                              "file_rate": round(sum(x["hits"] for x in xs) / max(1, sum(x["n"] for x in xs)), 3)}
    if hook["shown"] and hook["held"]:
        raw = (cfg.get("usage") or {}).get("min_sessions")
        min_sessions = 20 if raw is None else max(0, int(raw))
        out["hook"]["difference"] = _diff_ci(hook["shown"], hook["held"], min_sessions=min_sessions)
    if ctx:
        out["context"] = {"n": len(ctx), "precision": round(sum(x["precision"] for x in ctx) / len(ctx), 3),
                          "recall": round(sum(x["recall"] for x in ctx) / len(ctx), 3)}
    return out


def _diff_ci(a: list[dict], b: list[dict], reps: int = 1000, min_sessions: int = 20) -> dict:
    """알린 쪽 − 보류한 쪽 반영률, 세션 단위 군집 부트스트랩 95% 구간(고정 시드). 양쪽 세션이 min_sessions 미만이면 판단하지 않는다."""
    def by_session(xs):
        g: dict[str, list[int]] = {}
        for x in xs:
            g.setdefault(x["session"], []).append(int(x["hit"]))
        return list(g.values())
    ga, gb = by_session(a), by_session(b)
    rate = lambda gs: sum(map(sum, gs)) / max(1, sum(map(len, gs)))
    point = rate(ga) - rate(gb)
    rnd = random.Random(7)
    ds = sorted(rate([rnd.choice(ga) for _ in ga]) - rate([rnd.choice(gb) for _ in gb]) for _ in range(reps))
    lo, hi = ds[int(0.025 * reps)], ds[int(0.975 * reps) - 1]
    enough = min(len(ga), len(gb)) >= min_sessions
    return {"point": round(point, 3), "ci95": [round(lo, 3), round(hi, 3)], "sessions": [len(ga), len(gb)],
            "min_sessions": min_sessions, "enough": enough, "significant": enough and lo > 0}


def summary(p, cfg: dict, now: dt.datetime | None = None) -> dict:
    ev = evaluate(p, cfg, now=now)
    lines = []
    h = ev["hook"]
    for a, label in (("shown", "알림"), ("held", "보류")):
        if a in h:
            lines.append(f"훅 동반 변경 후보({label}) {h[a]['n']}건 — 하나라도 반영 {h[a]['uptake']:.0%} · 후보 파일 단위 {h[a]['file_rate']:.0%}")
    if "difference" in h:
        d = h["difference"]
        verdict = (f" — 세션이 아직 적다(알림 {d['sessions'][0]} · 보류 {d['sessions'][1]}, 양쪽 {d['min_sessions']} 이상에서 판단)"
                   if not d["enough"] else " — 훅이 반영을 늘린다" if d["significant"] else " — 차이가 확실하지 않다")
        lines.append(f"알림 − 보류: {d['point']:+.0%} (95% 구간 {d['ci95'][0]:+.0%} ~ {d['ci95'][1]:+.0%})" + verdict)
    if ev["context"]:
        c = ev["context"]
        lines.append(f"이슈 맥락의 '바꿀 가능성이 큰 파일' {c['n']}건 — 정밀도 {c['precision']:.0%} · 재현율 {c['recall']:.0%}")
    if ev["no_change"]:
        lines.append(f"이슈가 끝났지만 맥락 뒤의 커밋이 없어 뺀 것 {ev['no_change']}건")
    if ev["pending"]:
        lines.append(f"아직 결과를 모르는 제안 {ev['pending']}건(세션·작업이 수집되거나 끝나면 센다)")
    if ev["expired"]:
        lines.append(f"결과를 맞추지 못해 만료된 제안 {ev['expired']}건 — 세션·이슈가 수집되지 않는다면 수집 설정(claude_code · jira)을 본다")
    return {**ev, "lines": lines}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas usage", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    p = paths(args.root)
    s = summary(p, configmod.load(p.root))
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
    else:
        print("\n".join(s["lines"]) or "기록된 제안이 없다 — 훅·맥락을 쓰면 data/usage/ 에 쌓인다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
