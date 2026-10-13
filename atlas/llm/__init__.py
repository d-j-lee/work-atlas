"""LLM 작업 — 숫자는 계산이 내고 LLM 은 말만 한다. LLM 이 낸 것은 근거 검증을 통과한 것만 쓴다.

  python -m atlas llm tasks                                작업 목록 · 지금 백엔드 · 예산
  python -m atlas llm prompt <작업> [--key K] [--work 폴더]   프롬프트(시스템 + 입력)를 표준 출력으로 — 백엔드 없이 대화 세션에 붙여 쓴다
  python -m atlas llm import <작업> --key K --file 답.json    대화 세션에서 받은 답(JSON)을 같은 검증으로 들인다
  python -m atlas llm run <작업> [--key K] [--limit N] [--refresh] [--dry-run] [--work 폴더 --write]
  python -m atlas llm status                               백엔드 · 장부: 이번 달 호출 · 비용 · 통과율 · 거절 이유 · 재표집 일치도
  python -m atlas llm stability <작업> [--sample 3 | --share 0.15]  통과한 답 일부를 캐시 없이 다시 받아 같은지 잰다(재주석 일치)

작업 (method/prompts/<작업>.md 가 시스템 프롬프트 — 본문의 내용 해시가 캐시 키에 들어간다)
  category_card   카테고리 해설: 이 일은 무엇이고 어떻게 흘러가고 어디서 막히고 무엇을 조심하고 어디·누구를 보나.
                  수치는 사실 id 자리표시({duration_p50})로만 쓰고 보여 줄 때 지금 값으로 채운다 — 그래서 카드는 수치가 바뀌어도
                  맞고, 카테고리의 '모양'(흐름·병목·관련 카테고리·사례)이 바뀔 때만 다시 쓴다. → 업무 지도 · 안내서 · MCP
  annotate        트랙 A 주석: WU 입력 문서 + 코드북 → annotation.schema.json. 사후 검증(R1~R4)은 빌드가 한다 → data/units/annotated.jsonl
  weekly_digest   주간 요약: 카탈로그 경고 · 세대 변화 · 카테고리 움직임 · 최근 사후 문제 · 수신함 → data/reports/<연>-W<주>-digest.md
  work_draft      작업 기록 초안: 그 작업의 관측 문서 → 문제·처리·결과·회고 초안 (사람이 고쳐 WORK.md 로)
  category_names  카테고리 이름·정의 제안 → inbox/ (사람이 골라 categories.yaml 의 names 로 — 자동 적용하지 않는다)

검증 (모든 길 — 백엔드 호출 · import — 이 같다)
  스키마      출력이 작업의 JSON 스키마에 맞나 (jsonschema)
  근거        문장마다 refs 가 있고, 모두 입력이 허락한 목록(사실 id · 섹션 · 이슈 키 · 관측 id)에 있나 — 없으면 그 문장을 버린다
  수치 충실   category_card: 자리표시 밖에 숫자가 없어야 한다(입력에 있는 이름·경로·제목 안의 숫자는 허용).
              weekly_digest · work_draft: 숫자는 입력에 있는 것만. 어기면 그 문장을 버린다
  남은 양     필수 칸이 비면 거절. 거절도 장부와 캐시에 남는다(같은 입력을 다시 부르지 않게, 이유를 볼 수 있게)
백엔드 (atlas.toml [llm] — 기본 none: 아무것도 보내지 않는다)
  none     호출하지 않는다. prompt · import 로 사람이 대화 세션을 백엔드로 쓴다
  claude   Claude Code 헤드리스: claude [--bare] -p --output-format json --json-schema … --tools "" --strict-mcp-config --no-session-persistence
           --system-prompt … --max-budget-usd …  (입력은 표준 입력, 빈 임시 폴더에서 — 이 저장소의 CLAUDE.md·훅이 끼지 않게)
  command  사내 게이트웨이: 요청 JSON({system, prompt, schema, model, max_budget_usd})을 표준 입력으로 받아
           응답 JSON({output, cost_usd, model})을 표준 출력으로 내는 명령
캐시·장부: data/llm/cache/<작업>/ · data/llm/ledger.jsonl (git 밖). 예산은 호출당 · 실행당 · 월 상한을 넘지 않는다.
보내는 것: 사내에서 승인한 백엔드에만(policy.md §1). 입력은 이미 가린 발췌·집계뿐이고, 사람은 역할로만 있다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import config as configmod
from ..common import paths
from .backends import Backend, ClaudeBackend, CommandBackend, NoBackend, Reply, backend_for, parse_reply, run_process  # noqa: F401
from .base import PLACEHOLDER, SECTIONS, STMT, VERDICTS, canon, digest, llm_dir, prompt_body  # noqa: F401
from .check import MARKUP, NUMBER_WORDS, check_stmt, numbers_in, strip_literals  # noqa: F401
from .runner import Busy, Runner, agreement, feedback_index, ledger_rows, month_spent, stability, status  # noqa: F401
from .tasks import (TASKS, Annotate, CategoryCard, CategoryNames, Ctx, Item, Task, WeeklyDigest, WorkDraft,  # noqa: F401
                    card_context, card_facts, card_magnitudes, card_shape, codebook_for_prompt, fill, rendered_cards)

__all__ = [  # 다른 모듈·테스트가 쓰는 이름 — 패키지 안 어디에 있든 atlas.llm.<이름> 으로
    "Backend", "ClaudeBackend", "CommandBackend", "NoBackend", "Reply", "backend_for", "parse_reply", "run_process",
    "PLACEHOLDER", "SECTIONS", "STMT", "VERDICTS", "canon", "digest", "llm_dir", "prompt_body",
    "MARKUP", "NUMBER_WORDS", "check_stmt", "numbers_in", "strip_literals",
    "Busy", "Runner", "agreement", "feedback_index", "ledger_rows", "month_spent", "stability", "status",
    "TASKS", "Annotate", "CategoryCard", "CategoryNames", "Ctx", "Item", "Task", "WeeklyDigest", "WorkDraft",
    "card_context", "card_facts", "card_magnitudes", "card_shape", "codebook_for_prompt", "fill", "rendered_cards", "main"]

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas llm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    # --root 는 작업 이름 뒤에 와도 된다(atlas llm prompt X --root …) — 앞의 값을 덮지 않게 SUPPRESS
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tasks", parents=[common])
    sub.add_parser("status", parents=[common])
    for name in ("prompt", "run", "import"):
        s = sub.add_parser(name, parents=[common])
        s.add_argument("task", choices=sorted(TASKS))
        s.add_argument("--key", help="카테고리 키 · wu id (작업에 따라)")
        s.add_argument("--work", help="work_draft: 작업 폴더 (기본: 지금 폴더)")
        if name == "run":
            s.add_argument("--limit", type=int)
            s.add_argument("--refresh", action="store_true", help="캐시를 무시하고 다시 부른다")
            s.add_argument("--dry-run", action="store_true", help="부를 대상만 보인다")
            s.add_argument("--write", action="store_true", help="work_draft: 작업 폴더에 WORK.draft.md 로 쓴다")
        if name == "import":
            s.add_argument("--file", required=True, help="대화 세션에서 받은 답 JSON")
            s.add_argument("--model", default="manual", help="답을 낸 모델 이름(장부용)")
            s.add_argument("--write", action="store_true")
    st = sub.add_parser("stability", parents=[common])
    st.add_argument("task", choices=sorted(TASKS))
    st.add_argument("--sample", type=int, default=3)
    st.add_argument("--share", type=float, help="통과한 답의 이 비율을 다시 받는다 (재주석 표본 — 예: 0.15). 주면 --sample 대신")
    st.add_argument("--key")
    args = ap.parse_args(argv)
    p = paths(args.root)
    cfg = configmod.load(p.root)
    if args.cmd == "tasks":
        b = backend_for(cfg)
        print(f"백엔드 {b.name}" + (f" · 모델 {b.model}" if b.model else "") + f" · 이번 달 지출 ${month_spent(p):.2f}"
              f" / 상한 ${float(cfg['llm']['max_usd_per_month']):.2f}")
        for t in TASKS.values():
            print(f"  {t.id:<15} v{t.version}  {t.describe}")
        return 0
    if args.cmd == "status":
        print(json.dumps({"backend": backend_for(cfg).name, **status(p)}, ensure_ascii=False, indent=2))
        return 0
    task = TASKS[args.task]
    ctx = Ctx(p, cfg)
    if args.cmd == "stability":
        try:
            res = stability(Runner(p, cfg), task, ctx, argparse.Namespace(key=args.key, limit=None, refresh=True, dry_run=False,
                                                                         write=False, work=None), args.sample, args.share)
        except (RuntimeError, NoBackend) as e:
            print(f"[llm] {e}", file=sys.stderr)
            return 2
        print(json.dumps(res, ensure_ascii=False))
        return 0
    try:
        if args.cmd == "prompt":
            items = task.items(ctx, args)
            if not items:
                print("할 대상이 없다", file=sys.stderr)
                return 1
            item = items[0]
            print(f"<!-- 시스템 프롬프트 (method/prompts/{task.id}.md) -->\n{task.system(ctx)}\n\n<!-- 입력 ({item.key}) -->\n{item.prompt}\n"
                  f"<!-- 출력 JSON 스키마 -->\n```json\n{json.dumps(task.schema(ctx), ensure_ascii=False)}\n```")
            if len(items) > 1:
                print(f"\n(대상 {len(items)}개 중 첫째 — --key 로 고른다)", file=sys.stderr)
            return 0
        if args.cmd == "import":
            return _import(p, cfg, ctx, task, args)
        runner = Runner(p, cfg)
        accepted, tally = runner.run(task, ctx, args)
    except (RuntimeError, NoBackend, FileNotFoundError) as e:
        print(f"[llm] {e}", file=sys.stderr)
        return 2
    print(f"{task.id}: " + " · ".join(f"{k} {v}" for k, v in sorted(tally.items())) + f" · 이번 실행 ${runner.spent:.2f}")
    if accepted and not getattr(args, "dry_run", False):
        for line in task.sink(ctx, accepted, args):
            print(line)
    return 0


def _import(p, cfg, ctx: Ctx, task: Task, args) -> int:
    """사람이 대화 세션에서 받은 답 — 백엔드 호출과 같은 검증·캐시·장부를 거친다."""
    items = task.items(ctx, args)
    if not items:
        print("할 대상이 없다", file=sys.stderr)
        return 1
    if len(items) > 1 and not args.key:
        print("대상이 여럿이다 — --key 로 하나를 고른다", file=sys.stderr)
        return 2
    item = items[0]
    try:
        raw = Path(args.file).read_text(encoding="utf-8").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        out = json.loads(raw)
    except (OSError, json.JSONDecodeError, IndexError) as e:
        print(f"[llm] 답을 읽지 못했다: {e}", file=sys.stderr)
        return 2
    runner = Runner(p, cfg, backend=Backend(cfg))
    runner.backend.name = "manual"
    schema, system = task.schema(ctx), task.system(ctx)
    key = runner.cache_key(task, ctx, item, schema, system)
    meta = {"backend": "manual", "model": args.model, "cost_usd": 0.0, "seconds": 0}
    status_, cleaned, problems = runner.accept(task, ctx, item, out, meta, key, schema)
    runner.store(task, item, key, status_, cleaned, problems, meta)
    print(f"{status_}: {task.id} {item.key}" + ("".join(f"\n  - {x}" for x in problems) if problems else ""))
    if cleaned is not None:
        for line in task.sink(ctx, [(item, cleaned, meta)], args):
            print(line)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
