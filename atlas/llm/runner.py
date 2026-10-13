"""실행 — 캐시 · 장부 · 예산 · 잠금 · 오류 물러서기 · 재샘플 일치도 · 장부 요약 · 사람 판정 색인."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from collections import Counter
from pathlib import Path

import jsonschema

from ..common import append_jsonl, parse_ts, write_text
from .backends import Backend, Reply, _money, backend_for
from .base import PLACEHOLDER, _read_json, digest, llm_dir
from .tasks import Ctx, Item, Task

def ledger_rows(p) -> list[dict]:
    """장부 — 한 줄이 깨져도(쓰다 끊김 등) 나머지는 읽는다. 장부 때문에 빌드가 멈추지 않게."""
    f = llm_dir(p) / "ledger.jsonl"
    out = []
    if f.exists():
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                out.append(row)
    return out


def _when(row: dict) -> dt.datetime | None:
    """장부 줄의 시각 — 문자열로 비교하지 않는다(시간대가 다른 기계에서 쓴 줄이 섞이면 순서가 틀린다)."""
    try:
        t = parse_ts(str(row.get("ts", "")))
    except ValueError:
        return None
    return t if t.tzinfo else None


def _this_month(rows: list[dict], now: dt.datetime) -> list[dict]:
    return [r for r in rows if (t := _when(r)) and (t.astimezone(now.tzinfo).year, t.astimezone(now.tzinfo).month) == (now.year, now.month)]


def month_spent(p, now: dt.datetime | None = None, rows: list[dict] | None = None) -> float:
    now = now or dt.datetime.now().astimezone()
    return round(sum(_money(r.get("cost_usd")) or 0.0 for r in _this_month(rows if rows is not None else ledger_rows(p), now)), 4)


class Busy(RuntimeError):
    pass


class Runner:
    """한 번의 실행. 예산은 보수적으로 센다 — 백엔드가 비용을 알려 주지 않거나 시간이 넘으면 호출당 상한을 쓴 것으로 친다.
    같은 대상이 7일 안에 두 번 오류였으면 건너뛴다(--refresh 로 다시). 한 인스턴스에서 실행은 하나씩(잠금 파일)."""

    def __init__(self, p, cfg: dict, backend: Backend | None = None, log=print):
        self.p, self.cfg, self.log = p, cfg, log
        self.backend = backend or backend_for(cfg)
        self.lim = cfg.get("llm", {})
        self.spent = 0.0

    def cache_key(self, task: Task, ctx: Ctx, item: Item, schema: dict, system: str) -> str:
        """같은 작업·프롬프트·스키마·모델·대상 모양이면 같은 답을 다시 쓴다. 백엔드 종류는 넣지 않는다 —
        대화 세션에서 들인(import) 답을 나중의 자동 실행이 그대로 쓴다."""
        return digest({"task": task.id, "v": task.version, "system": digest(system), "schema": digest(schema),
                       "model": self.backend.model, "shape": item.shape})[:24]

    def _cache(self, task: Task, key: str) -> Path:
        return llm_dir(self.p) / "cache" / task.id / f"{key}.json"

    def per_call(self) -> float:
        return float(self.lim.get("max_usd_per_call", 0.5))

    def budget_left(self) -> float:
        run = float(self.lim.get("max_usd_per_run", 5)) - self.spent
        month = float(self.lim.get("max_usd_per_month", 50)) - month_spent(self.p)
        return round(min(run, month, self.per_call()), 4)

    def charge(self, reply: Reply) -> float:
        cost = reply.cost_usd if reply.cost_known else self.per_call()
        self.spent += cost
        return cost

    def ledger(self, **row):
        append_jsonl(llm_dir(self.p) / "ledger.jsonl", {"ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"), **row})

    def recent_errors(self, task: Task) -> Counter:
        since = dt.datetime.now().astimezone() - dt.timedelta(days=7)
        return Counter(r.get("key") for r in ledger_rows(self.p)
                       if r.get("task") == task.id and r.get("status") == "error" and (t := _when(r)) and t >= since)

    def accept(self, task: Task, ctx: Ctx, item: Item, out: dict | None, meta: dict, key: str, schema: dict) -> tuple[str, dict | None, list[str]]:
        if meta.get("error") or out is None:                  # 오류로 끝난 호출의 출력은 쓰지 않는다
            return "error", None, [meta.get("error") or "출력 없음"]
        try:
            jsonschema.validate(out, schema)
        except jsonschema.ValidationError as e:
            return "rejected", None, [f"스키마: {e.message[:200]}"]
        cleaned, problems = task.check(ctx, item, out)
        return ("accepted" if cleaned is not None else "rejected"), cleaned, problems

    def lock(self):
        import contextlib
        import os
        f = llm_dir(self.p) / ".lock"

        @contextlib.contextmanager
        def held():
            f.parent.mkdir(parents=True, exist_ok=True)
            try:
                if f.exists() and time.time() - f.stat().st_mtime > 3 * 3600:
                    f.unlink()                                   # 죽은 실행이 남긴 잠금
                fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                raise Busy(f"다른 LLM 실행이 진행 중이다 ({f}) — 끝난 뒤 다시. 남은 잠금이면 지운다") from None
            os.close(fd)
            try:
                yield
            finally:
                f.unlink(missing_ok=True)
        return held()

    def run(self, task: Task, ctx: Ctx, args) -> tuple[list[tuple[Item, dict, dict]], Counter]:
        schema, system = task.schema(ctx), task.system(ctx)
        items = task.items(ctx, args)
        given = getattr(args, "limit", None)
        limit = int(self.lim.get("limit_per_run", 20)) if given is None else int(given)
        accepted, tally = [], Counter()
        calls = 0
        refresh = getattr(args, "refresh", False)
        errors = self.recent_errors(task) if not refresh else Counter()
        with self.lock():
            for item in items:
                key = self.cache_key(task, ctx, item, schema, system)
                cached = _read_json(self._cache(task, key)) if not refresh else None
                if isinstance(cached, dict) and cached.get("status") in ("accepted", "rejected"):
                    tally[f"cached_{cached['status']}"] += 1
                    if cached["status"] == "accepted":
                        accepted.append((item, cached["output"], {**(cached.get("meta") or {}), "cached": True}))
                    continue
                if errors.get(key, 0) >= 2:
                    tally["backoff"] += 1
                    continue
                if calls >= limit:
                    tally["deferred"] += 1
                    continue
                if getattr(args, "dry_run", False):
                    tally["would_call"] += 1
                    self.log(f"  호출 예정: {task.id} {item.key}")
                    calls += 1
                    continue
                budget = self.budget_left()
                if budget <= 0:
                    tally["skipped_budget"] += 1
                    self.ledger(task=task.id, item=item.key, key=key, backend=self.backend.name, status="skipped_budget", cost_usd=0)
                    continue
                reply = self.backend.call(system, item.prompt, schema, budget)
                calls += 1
                cost = self.charge(reply)
                meta = {"backend": self.backend.name, "model": reply.model or self.backend.model, "cost_usd": cost,
                        "cost_estimated": not reply.cost_known, "seconds": round(reply.seconds, 1), "error": reply.error}
                status, cleaned, problems = self.accept(task, ctx, item, reply.output, meta, key, schema)
                self.store(task, item, key, status, cleaned, problems, meta)
                tally[status] += 1
                if cleaned is not None:
                    accepted.append((item, cleaned, meta))
                self.log(f"  {status:<9} {task.id} {item.key}" + (f" — {'; '.join(problems[:2])}" if problems else ""))
        return accepted, tally

    def store(self, task: Task, item: Item, key: str, status: str, cleaned, problems: list[str], meta: dict):
        if status in ("accepted", "rejected"):            # 오류는 캐시하지 않는다 — 다음에 다시 시도(7일에 두 번까지)
            write_text(self._cache(task, key), json.dumps({"key": key, "task": task.id, "version": task.version, "item": item.key,
                                                           "status": status, "output": cleaned, "problems": problems, "meta": meta},
                                                          ensure_ascii=False, indent=1) + "\n")
        self.ledger(task=task.id, version=task.version, item=item.key, key=key, backend=meta.get("backend"), model=meta.get("model"),
                    status=status, cost_usd=meta.get("cost_usd", 0), cost_estimated=meta.get("cost_estimated", False),
                    seconds=meta.get("seconds"), problems=problems[:5])


def agreement(task_id: str, a: dict, b: dict) -> float:
    """같은 입력에 두 번 받은 답이 얼마나 같은가 (0~1). 주석은 분류 값의 일치율, 나머지는 인용·자리표시 집합의 자카드."""
    if task_id == "annotate":
        fa, fb = (a.get("facets") or {}), (b.get("facets") or {})
        pairs = [(fa.get(k, {}).get("value"), fb.get(k, {}).get("value")) for k in sorted(set(fa) | set(fb))]
        pairs.append(((a.get("archetype") or {}).get("value"), (b.get("archetype") or {}).get("value")))
        return round(sum(1 for x, y in pairs if x == y) / len(pairs), 3) if pairs else 1.0

    def marks(x) -> set[str]:
        out = set()
        for v in (x.values() if isinstance(x, dict) else x if isinstance(x, list) else []):
            if isinstance(v, dict) and "text" in v:
                out |= {f"ref:{r}" for r in v.get("refs", [])} | {f"fact:{h}" for h in PLACEHOLDER.findall(v["text"])}
            elif isinstance(v, (dict, list)):
                out |= marks(v)
        return out
    ma, mb = marks(a), marks(b)
    return round(len(ma & mb) / len(ma | mb), 3) if ma | mb else 1.0


def stability(runner: "Runner", task: Task, ctx: Ctx, args, sample: int, share: float | None = None) -> dict:
    """통과한 답 몇 개를 캐시 없이 다시 받아 같은지 잰다. 표본은 세대마다 바뀌는 결정론 표본(세대 id 를 섞은 해시 순)이다 —
    같은 세대에서는 다시 돌려도 같은 표본, 세대가 바뀌면 다른 표본. share 를 주면 통과한 답의 그 비율(재주석 표본, policy §6)."""
    import math
    schema, system = task.schema(ctx), task.system(ctx)
    gen = str((_read_json(ctx.p.model / "generation.json") or {}).get("generation", ""))
    probe = argparse.Namespace(**{**vars(args), "refresh": True, "key": getattr(args, "key", None)})
    eligible = []
    for item in sorted(task.items(ctx, probe), key=lambda i: digest(f"{gen}:{i.key}")):
        key = runner.cache_key(task, ctx, item, schema, system)
        cached = _read_json(runner._cache(task, key))
        if cached and cached.get("status") == "accepted":
            eligible.append((item, key, cached))
    picked = eligible[:max(1, math.ceil(share * len(eligible))) if share else sample]
    scores = []
    for item, key, cached in picked:
        budget = runner.budget_left()
        if budget <= 0:
            break
        reply = runner.backend.call(system, item.prompt, schema, budget)
        cost = runner.charge(reply)
        meta = {"backend": runner.backend.name, "model": reply.model, "cost_usd": cost, "error": reply.error}
        status_, cleaned, problems = runner.accept(task, ctx, item, reply.output, meta, key, schema)
        score = agreement(task.id, cached["output"], cleaned) if cleaned is not None else 0.0
        scores.append(score)
        runner.ledger(task=task.id, version=task.version, item=item.key, key=key, backend=runner.backend.name, model=reply.model,
                      status="stability", agreement=score, cost_usd=cost, problems=problems[:3])
    return {"task": task.id, "sampled": len(scores), "agreement_mean": round(sum(scores) / len(scores), 3) if scores else None,
            "agreement_min": min(scores) if scores else None}


def status(p) -> dict:
    """장부 요약 — 이번 달 · 작업별. 카탈로그의 llm 칸도 이것이다."""
    rows = ledger_rows(p)
    now = dt.datetime.now().astimezone()
    month = now.strftime("%Y-%m")
    cur = _this_month(rows, now)
    by_task: dict[str, Counter] = {}
    reasons: Counter = Counter()
    for r in cur:
        by_task.setdefault(str(r.get("task")), Counter())[str(r.get("status"))] += 1
        for x in r.get("problems") or []:
            reasons[str(x).split(":")[0][:40]] += 1
    calls = [r for r in cur if r.get("status") in ("accepted", "rejected", "error")]
    stab = [r["agreement"] for r in cur if r.get("status") == "stability" and isinstance(r.get("agreement"), (int, float))]
    cards = ((_read_json(llm_dir(p) / "category_card.json") or {}).get("cards") or {})
    return {"month": month, "calls": len(calls), "cost_usd": month_spent(p, rows=rows),
            "cost_estimated": sum(1 for r in cur if r.get("cost_estimated")),
            "accepted": sum(1 for r in calls if r["status"] == "accepted"),
            "rejected": sum(1 for r in calls if r["status"] == "rejected"),
            "errors": sum(1 for r in calls if r["status"] == "error"),
            "accept_rate": round(sum(1 for r in calls if r["status"] == "accepted") / len(calls), 3) if calls else None,
            "by_task": {k: dict(v) for k, v in sorted(by_task.items())}, "top_reasons": dict(reasons.most_common(5)),
            "cards": len(cards), "stability": {"n": len(stab), "mean": round(sum(stab) / len(stab), 3) if stab else None},
            "last": rows[-1].get("ts") if rows else None}


def feedback_index(observations: list[dict]) -> dict:
    """사람의 LLM 출력 판정(atlas.llm_feedback, atlas record llm-feedback 로 남긴다) — 작업·대상·출력 내용(digest)별 마지막 판정.
    '틀림'이면 그 출력을 보이지 않는다. 다시 받은 답은 내용이 달라 판정을 물려받지 않는다."""
    last: dict[tuple, dict] = {}
    for o in sorted((o for o in observations if o["kind"] == "atlas.llm_feedback"), key=lambda o: (parse_ts(o["ts"]), o["obs_id"])):
        pl = o.get("payload") or {}
        last[(pl.get("task"), pl.get("key"), pl.get("digest"))] = pl
    verdicts = Counter(v.get("verdict") for v in last.values())
    return {"verdicts": dict(sorted(verdicts.items())),
            "suppressed": sorted([list(k) for k, v in last.items() if v.get("verdict") == "wrong"])}
