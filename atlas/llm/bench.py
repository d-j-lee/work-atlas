"""실측 — 실제 백엔드로 작업 몇 개를 돌려 통과율 · 거절 이유 · 재표집 일치도 · 비용 · 지연을 재고, 사람이 판정할 목록을 낸다.

  python -m atlas llm bench [--sample 12] [--stability 3] [--digest] [--max-usd 3]

- 대상: 흐름이 있는 카테고리 중 큰 것 절반 + 나머지에서 고르게 절반(크기 구간이 섞이게). 같은 데이터면 같은 대상.
- 캐시를 쓰지 않고 다시 부른다(--refresh 와 같다). 상한은 --max-usd(이 실측 전체)와 [llm] 의 호출·월 상한 중 작은 것.
- 결과: data/reports/llm-bench-<시각>.md — 요약 · 대상별 표 · 거절 이유 · 사람이 읽을 해설(지금 값으로 채운 것)과 판정 명령.
- 숫자는 모두 이번 실행의 장부에서 센다. 사람의 판정(쓸모 있음 · 틀림)은 atlas record llm-feedback 으로 남긴다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
from collections import Counter

from ..common import write_text
from .base import reason_kind
from .runner import Runner, ledger_rows, stability
from .tasks import TASKS, Ctx


def pick_keys(cm: dict, n: int) -> list[str]:
    """큰 카테고리 절반 + 나머지에서 고르게 절반 — 자주 보는 것과 작은 칸이 함께 시험되게. (전체)·(미분류) 는 뺀다."""
    rows = [e for e in cm["categories"].values() if e.get("flow") and not e["key"].startswith("(")]   # (전체)·(미분류) 는 빼고 실제 카테고리만
    rows.sort(key=lambda e: (-e["profile"]["n"], e["key"]))
    big = rows[: math.ceil(n / 2)]
    rest = rows[len(big):]
    k = n - len(big)
    spread = [rest[int(i * len(rest) / k)] for i in range(k)] if k > 0 and rest else []
    return [e["key"] for e in big + spread][:n]


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**{"key": None, "limit": 1, "refresh": True, "dry_run": False, "write": False, "work": None, **kw})


def run(p, cfg: dict, sample: int = 12, stab: int = 3, with_digest: bool = True, max_usd: float = 3.0,
        keys: list[str] | None = None, log=print) -> dict:
    cfg = json.loads(json.dumps(cfg))
    cfg["llm"]["max_usd_per_run"] = min(float(max_usd), float(cfg["llm"].get("max_usd_per_run", max_usd)))
    runner = Runner(p, cfg, log=log)
    if runner.backend.name == "none":
        raise RuntimeError("백엔드가 none 이다 — 실측은 실제 백엔드(claude · command)로만 한다")
    ctx = Ctx(p, cfg)
    started = dt.datetime.now().astimezone()
    keys = keys or pick_keys(ctx.cm, sample)
    with runner.lock():                                   # 실측 전체를 한 실행으로 — 다른 실행이 끼어들어 숫자가 섞이지 않게
        res = _measure(p, cfg, runner, ctx, keys, stab, with_digest, log)
    res["report"] = str(write_report(p, ctx, res, started))
    return res


def _measure(p, cfg: dict, runner: Runner, ctx: Ctx, keys: list[str], stab: int, with_digest: bool, log) -> dict:
    task = TASKS["category_card"]
    accepted: dict[str, tuple[dict, dict]] = {}
    for k in keys:
        if runner.budget_left() <= 0:
            log(f"  예산 소진 — {k} 부터 건너뛴다")
            break
        got, _ = runner.run(task, ctx, _ns(key=k))
        for item, out, meta in got:
            accepted[item.key] = (out, meta)
    stab_res = stability(runner, task, ctx, _ns(), stab) if stab and accepted and runner.budget_left() > 0 else None
    digest_tally = None
    if with_digest and runner.budget_left() > 0:
        got, digest_tally = runner.run(TASKS["weekly_digest"], ctx, _ns())
        if got:
            TASKS["weekly_digest"].sink(ctx, got, _ns())
    if accepted:
        task.sink(ctx, [(it, *accepted[it.key]) for it in task.items(ctx, _ns()) if it.key in accepted], _ns())
    rows = [r for r in ledger_rows(p) if r.get("run") == runner.run_id]
    return summarize(rows, keys, stab_res, runner, cfg)


def summarize(rows: list[dict], keys: list[str], stab_res: dict | None, runner: Runner, cfg: dict) -> dict:
    calls = [r for r in rows if r.get("status") in ("accepted", "rejected", "error")]
    by = Counter(r["status"] for r in calls)
    secs = [float(r["seconds"]) for r in calls if isinstance(r.get("seconds"), (int, float))]
    reasons = Counter(reason_kind(x) for r in calls for x in (r.get("problems") or []))
    cost = round(sum(float(r.get("cost_usd") or 0) for r in rows), 4)
    models = Counter(r.get("model") for r in calls if r.get("model"))
    return {
        "backend": runner.backend.name, "isolation": getattr(runner.backend, "isolation", lambda: "")(),
        "model": (models.most_common(1)[0][0] if models else runner.backend.model) or "",
        "keys": keys, "calls": len(calls), "accepted": by["accepted"], "rejected": by["rejected"], "errors": by["error"],
        "pass_rate": round(by["accepted"] / len(calls), 3) if calls else None,
        "with_dropped": sum(1 for r in calls if r.get("status") == "accepted" and r.get("problems")),
        "reasons": reasons.most_common(8), "cost_usd": cost,
        "cost_per_call": round(cost / len(calls), 4) if calls else None,
        "cost_estimated": sum(1 for r in calls if r.get("cost_estimated")),
        "seconds_p50": round(statistics.median(secs), 1) if secs else None, "seconds_max": round(max(secs), 1) if secs else None,
        "stability": stab_res, "rows": calls,
    }


def _md(x) -> str:
    return str(x).replace("|", "\\|").replace("\n", " ")


def write_report(p, ctx: Ctx, res: dict, started: dt.datetime):
    from .tasks import rendered_cards
    f = p.reports / f"llm-bench-{started.strftime('%Y%m%d-%H%M%S')}.md"
    s = res["stability"] or {}
    lines = [
        f"<!-- atlas:generated LLM 실측 {started.isoformat(timespec='seconds')} — 고치지 않는다 -->",
        f"# LLM 실측 — {started.strftime('%Y-%m-%d %H:%M')}", "",
        f"백엔드 `{res['backend']}` · 격리 `{res['isolation']}` · 모델 `{res['model'] or '기본'}` · 세대 `{ctx.cm.get('generation', '')}`", "",
        "| 항목 | 값 |", "|---|---|",
        f"| 호출 | {res['calls']} (통과 {res['accepted']} · 거절 {res['rejected']} · 오류 {res['errors']}) |",
        f"| 통과율 | {res['pass_rate'] if res['pass_rate'] is not None else '-'} |",
        f"| 통과했지만 문장을 버린 답 | {res['with_dropped']} |",
        f"| 비용 | ${res['cost_usd']:.4f} (호출당 ${res['cost_per_call'] or 0:.4f}, 값을 몰라 상한으로 센 호출 {res['cost_estimated']}) |",
        f"| 지연 | 중앙 {res['seconds_p50']}초 · 최대 {res['seconds_max']}초 |",
        f"| 재표집 일치도 | {('평균 ' + str(s.get('agreement_mean')) + ' · 최저 ' + str(s.get('agreement_min')) + ' (' + str(s.get('sampled')) + '건)') if s else '-'} |",
        "", "## 거절·버림 이유", "",
    ]
    lines += [f"- {_md(r)} × {n}" for r, n in res["reasons"]] or ["- 없음"]
    lines += ["", "## 호출", "", "| 작업 | 대상 | 상태 | 초 | 비용 | 이유 |", "|---|---|---|---:|---:|---|"]
    for r in res["rows"]:
        lines.append(f"| {r.get('task')} | {_md(r.get('item'))} | {r.get('status')} | {r.get('seconds', '')} | "
                     f"{float(r.get('cost_usd') or 0):.4f} | {_md('; '.join(map(str, (r.get('problems') or [])[:2])))} |")
    cards = rendered_cards(p, ctx.cm)
    lines += ["", "## 사람이 판정할 해설 (지금 값으로 채운 것)", "",
              "읽고 판정한다 — 쓸모 있음 · 틀림(사실과 다름 — 채널에서 내린다) · 쓸모없음. 판정은 해설 내용에 붙는다.", ""]
    for k in res["keys"]:
        c = cards.get(k)
        if not c:
            continue
        lines += [f"### {k}", ""] + [f"> {x}" for x in c["paragraphs"]] + [""] + [f"- 주의: {x}" for x in c["watch"]] + [
            "", "```", *(f"atlas record llm-feedback --key {json.dumps(k, ensure_ascii=False)} --verdict {v}"
                         for v in ("useful", "wrong", "not_useful")), "```", ""]
    write_text(f, "\n".join(lines) + "\n")
    return f


def text(res: dict) -> str:
    s = res["stability"] or {}
    return (f"LLM 실측 — {res['backend']}({res['isolation']}) · 모델 {res['model'] or '기본'}\n"
            f"  호출 {res['calls']} · 통과 {res['accepted']} · 거절 {res['rejected']} · 오류 {res['errors']} · 문장 버림 {res['with_dropped']}\n"
            f"  비용 ${res['cost_usd']:.4f} · 지연 중앙 {res['seconds_p50']}초 · 재표집 일치 {s.get('agreement_mean', '-')}\n"
            f"  이유 {', '.join(f'{r}×{n}' for r, n in res['reasons'][:4]) or '없음'}\n  리포트 {res['report']}")


__all__ = ["pick_keys", "run", "summarize", "text"]
