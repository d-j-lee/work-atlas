"""Claude Code 훅 — 일하는 순간에 필요한 지식만 짧게 넣는다 (U2 실행 가드의 결정론 버전).

  PostToolUse (Edit|Write|MultiEdit|NotebookEdit)
      방금 바꾼 파일이 결함 수정이 몰린 파일이면 알리고, 함께 바뀌어 온 파일 중 **이 세션에서 아직 안 바꾼 것**을 알린다.
      같은 세션에서 같은 파일은 한 번만 말한다. 신뢰도 hook_min_confidence 미만 동반 변경·low 핫스팟은 말하지 않는다.
  UserPromptSubmit
      프롬프트에 이슈 키가 있으면 그 이슈의 상태 · 업무 카테고리(지금 단계와 보통 다음에 오는 것 · 접점 · 참조 사례) ·
      비슷한 과거 작업 · 그 작업들이 바꾼 파일을 넣는다.
  SessionStart (startup · clear · compact)
      작업 폴더(설정된 작업 공간 안)에서 연 세션이면 그 작업의 맥락을 넣는다 — 작업 기록의 키·제목·문제로 찾는다.
      작업 폴더의 CLAUDE.md 가 CONTEXT.md 를 가져오고(@CONTEXT.md) 그 파일이 지금 세대로 만든 것이면 넣지 않는다
      — 같은 맥락을 두 번 넣지 않게. 압축 뒤(compact)에는 늘 넣는다: 가져온 파일이 압축 뒤 다시 읽히는지는 문서에 없다.

규칙: 막지 않는다 — 어떤 오류에도 출력 없이 종료 코드 0 (오류는 data/state/hook-errors.log 에 한 줄).
출력은 hookSpecificOutput.additionalContext (Claude Code 문서: 1만 자 상한) — 여기서는 1,500자 안팎으로 자른다.

설치: python -m atlas hook --print-settings 가 붙여 넣을 설정(JSON)을 낸다.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import traceback
from pathlib import Path

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
LIMIT = 1500


def _state_path(p, session: str) -> Path:
    safe = "".join(ch for ch in session if ch.isalnum() or ch in "-_")[:80] or "unknown"
    return p.state / "hook" / f"{safe}.json"


def _load_state(p, session: str) -> dict:
    try:
        return json.loads(_state_path(p, session).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"edited": [], "told": []}


def _save_state(p, session: str, state: dict) -> None:
    from .common import write_text
    write_text(_state_path(p, session), json.dumps(state, ensure_ascii=False))


def on_edit(p, cfg: dict, event: dict) -> str | None:
    from . import context
    from .common import path_filter
    inp = event.get("tool_input") or {}
    raw = inp.get("file_path") or inp.get("notebook_path")
    if not raw:
        return None
    rel = context.repo_relative(raw, event.get("cwd"))
    session = event.get("session_id") or "unknown"
    state = _load_state(p, session)
    if rel not in state["edited"]:
        state["edited"].append(rel)
    if rel in state["told"]:
        _save_state(p, session, state)
        return None
    min_conf = float(cfg["context"]["hook_min_confidence"])
    model = context.Model(p)
    info = context.path_info(model, rel, None, path_filter(cfg["mining"]["test_paths"]), min_conf)
    state["told"].append(rel)
    if model.stale and not state.get("stale_told"):
        state["stale_told"] = True
        _save_state(p, session, state)
        return "[work-atlas] 모델이 옛 형식이라 힌트를 낼 수 없다 — 인스턴스에서 `atlas build` 를 다시 돌린다."
    _save_state(p, session, state)
    if not info["known"]:
        return None
    edited = set(state["edited"])
    pending = [x for x in info["with"] if x["path"] not in edited]
    hot = info.get("hot") if (info.get("hot") or {}).get("strength") in ("high", "medium") else None
    if not hot and not pending:
        return None
    lines = [f"[work-atlas] `{info['path']}`"]
    if hot:
        lines.append(f"- 결함 수정이 몰려 온 파일이다({hot['strength']}): 수정 {hot['fixes']}건/변경 {hot['changes']}건. "
                     "경계 조건·회귀 테스트를 한 번 더 본다.")
    if pending:
        lines.append("- 이 파일과 함께 바뀌어 온 파일 중 이번 세션에서 아직 안 바꾼 것: " + ", ".join(
            f"`{x['path']}`{' (테스트)' if x['test'] else ''} {x['confidence']:.0%}·{x['support']}회" for x in pending))
        lines.append("  필요 없으면 무시한다 — 과거의 동반 변경일 뿐 이번 변경의 요구 사항은 아니다.")
    return "\n".join(lines)


def on_prompt(p, cfg: dict, event: dict) -> str | None:
    from . import config as configmod, context
    keys = configmod.Patterns(cfg).issue_keys(event.get("prompt") or "")
    if not keys:
        return None
    ctx = context.build(p, cfg, issue=keys[0], cwd=event.get("cwd"))
    if not ctx.get("issue", {}).get("known") and not ctx.get("similar"):
        return None
    ctx["health"] = []
    return "[work-atlas] " + context.render_md(ctx, max_lines=18)


def on_session_start(p, cfg: dict, event: dict) -> str | None:
    from . import context, workspace as wsmod
    if event.get("source") not in (None, "startup", "clear", "compact"):     # resume·fork 는 대화에 이미 있다
        return None
    cwd = event.get("cwd")
    loc = wsmod.locate(cwd, wsmod.sources(cfg, p.root)) if isinstance(cwd, str) and cwd else None
    if not loc:
        return None
    src, work_dir, rel = loc
    try:
        imports = (work_dir / "CLAUDE.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        imports = ""
    imported = any(x.strip().startswith(f"@{wsmod.GENERATED}") for x in imports.splitlines())
    stamp = wsmod.generated_stamp(work_dir / wsmod.GENERATED) if imported else None
    current = context.Model(p).generation.get("generation")
    if imported and stamp and stamp == current and event.get("source") != "compact":
        return None
    ctx = context.build(p, cfg, work=str(work_dir))
    iss = ctx.get("issue") or {}
    if not ctx.get("similar") and not (iss.get("known") and (iss.get("title") or iss.get("labels"))):
        return None                             # 이 폴더 자신 말고는 알려 줄 것이 없다
    ctx["health"] = []
    stale = (f"\n- CONTEXT.md 는 세대 {stamp} 로 만든 것이다 — 위가 지금 맥락이다(`atlas work context` 로 파일을 새로 만든다)"
             if imported and stamp and stamp != current else "")
    return "[work-atlas] " + context.render_md(ctx, max_lines=18) + stale


def handle(event: dict, root: str | None = None) -> dict | None:
    from . import config as configmod
    from .common import paths
    p = paths(root)
    if not (p.root / "method" / "codebook.yaml").exists():     # 인스턴스가 아니면 아무것도 쓰지 않는다
        return None
    cfg = configmod.load(p.root)
    name = event.get("hook_event_name")
    text = None
    if name in ("PostToolUse", "PreToolUse") and event.get("tool_name") in EDIT_TOOLS:
        text = on_edit(p, cfg, event)
    elif name == "UserPromptSubmit":
        text = on_prompt(p, cfg, event)
    elif name == "SessionStart":
        text = on_session_start(p, cfg, event)
    if not text:
        return None
    if len(text) > LIMIT:
        text = text[: LIMIT - 10] + "\n…(잘림)"
    return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": text}}


def settings_snippet(root: Path) -> dict:
    """셸 문법(환경 변수 접두 등)에 기대지 않는 명령 — 이 명령을 실행한 파이썬의 절대 경로와 인스턴스 루트를 박는다.
    Windows·macOS·Linux 어디서든 같은 문자열이 동작하게 하기 위해서다."""
    from .common import REPO                      # 이 엔진 코드가 있는 곳(인스턴스 루트와 다를 수 있다)
    # 경로는 코드 문자열에 박지 않고 인자로 넘긴다 — 경로에 따옴표·공백·역슬래시가 있어도 깨지지 않게
    code = "import sys; sys.path.insert(0, sys.argv[1]); from atlas.hook import main; raise SystemExit(main(['--root', sys.argv[2]]))"
    cmd = f'"{sys.executable}" -c "{code}" "{REPO}" "{root}"'
    return {"hooks": {
        "PostToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit",
                         "hooks": [{"type": "command", "command": cmd, "timeout": 10}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}],
        "SessionStart": [{"matcher": "startup|clear|compact", "hooks": [{"type": "command", "command": cmd, "timeout": 10}]}],
    }}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "-h" in argv or "--help" in argv or (sys.stdin is not None and sys.stdin.isatty()):
        print(__doc__)
        print("사용: (Claude Code 가 stdin 으로 사건 JSON 을 준다) atlas hook [--root …]  ·  atlas hook --print-settings [--root …]")
        return 0
    if "--print-settings" in argv:
        from .common import paths
        root = paths(argv[argv.index("--root") + 1] if "--root" in argv else None).root
        print(json.dumps(settings_snippet(root), ensure_ascii=False, indent=2))
        print("\n# ~/.claude/settings.json(모든 폴더에 적용 — 작업 폴더에서도 동작한다) 또는 작업 저장소의 .claude/settings.json 의 hooks 에 합친다."
              "\n# 프로젝트 설정은 CLAUDE.md 와 달리 상위 폴더에서 이어받지 않는다.", file=sys.stderr)
        return 0
    root = argv[argv.index("--root") + 1] if "--root" in argv else None
    try:
        # 입출력은 UTF-8 바이트로 — Windows 의 파이프는 기본이 시스템 코드 페이지(cp949)라 한글이 깨진다
        raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode("utf-8")
        event = json.loads(raw.decode("utf-8-sig") or "{}")
        out = handle(event, root)
        if out:
            data = json.dumps(out, ensure_ascii=False).encode("utf-8")
            if hasattr(sys.stdout, "buffer"):
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            else:
                sys.stdout.write(data.decode("utf-8"))
    except BaseException:                               # 훅은 절대 작업을 막지 않는다 — SystemExit 도 삼킨다
        try:
            from .common import paths
            p = paths(root)
            if not (p.root / "method" / "codebook.yaml").exists():
                return 0
            log = p.state / "hook-errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            if not log.exists() or log.stat().st_size < 200_000:
                with log.open("a", encoding="utf-8") as f:
                    f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} "
                            f"{traceback.format_exc().splitlines()[-1]}\n")
        except BaseException:                         # 루트를 못 찾는 SystemExit 도 — 종료 코드는 언제나 0
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
