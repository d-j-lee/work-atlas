"""Claude Code 대화 기록 수집기 — 세션 하나가 관측 하나 (ai_session.session).

AI 세션은 티켓 없이 한 일이 가장 많이 남는 곳이다(METHODOLOGY.md §10). 본문은 가져오지 않고 행동의 흔적만 센다.
  keys     paths(편집한 파일, 저장소 루트 기준) · issue_keys(프롬프트·브랜치) · branches(브랜치 해시 키 — git 수집기와 같은 규칙)
  metrics  prompts · turns · tool_calls · edits · reads · bash · test_runs · commits · tool_errors · web
           · subagents · compactions · duration_min · unparsed_lines
  발췌     첫 프롬프트만 (가린 뒤 300자). excerpt=false 로 끈다.
- 기록 형식은 Claude Code 내부 형식이고 버전마다 바뀐다(공식 문서). 모르는 줄은 세고(unparsed_lines) 건너뛰며,
  그런 줄이 있으면 partial=true. 형식이 바뀌면 이 파일만 고친다.
- 세션은 이어서 쓰일 수 있다. obs_id 에 줄 수를 넣어 커지면 새 관측이 생기고, 읽는 쪽은 세션별 **마지막 것**만 쓴다.
  지금 쓰이는 세션(마지막 수정이 min_idle_minutes 이내)은 건너뛴다.
- Claude Code 는 기록을 기본 30일 뒤 지운다(cleanupPeriodDays). 수집 주기는 그보다 짧아야 한다 — 카탈로그가 경고한다.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import time
from pathlib import Path

from . import Result, branch_key, excerpt
from ..common import now_iso

COLLECTOR = "claude-code@1"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob", "LS"}
WEB_TOOLS = {"WebFetch", "WebSearch"}
TEST_CMD = re.compile(r"\b(pytest|unittest|go test|cargo test|ctest|make (?:test|check)|mvn\b[^|;&]*\b(?:test|verify)"
                      r"|gradlew?\b[^|;&]*\btest|npm (?:run )?test|yarn test|pnpm test|jest|vitest|dotnet test|phpunit|rspec)\b")
COMMIT_CMD = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?commit\b")
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def add_args(ap) -> None:
    g = ap.add_argument_group("claude-code (설정 없이 바로 수집)")
    g.add_argument("--projects-dir", help="기본 ~/.claude/projects")
    g.add_argument("--include", action="append", help="프로젝트 폴더 이름에 이 문자열이 든 것만 (여러 번)")


def source_from_args(args) -> dict | None:
    if (getattr(args, "projects_dir", None) or getattr(args, "include", None)) and args.kind in ("claude-code", "all"):
        return {"name": "claude-code", "projects_dir": args.projects_dir or "~/.claude/projects",
                "include": args.include or []}
    return None


def _repo_root(cwd: str | None, cache: dict) -> str | None:
    if not cwd:
        return None
    if cwd not in cache:
        root = None
        if Path(cwd).is_dir():
            out = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace")
            if out.returncode == 0 and out.stdout.strip():
                root = out.stdout.strip()
        cache[cwd] = root or cwd
    return cache[cwd]


def _relative(path: str, root: str | None) -> str | None:
    """저장소 안의 파일만 저장소 기준 상대 경로로. 밖의 경로(개인 폴더 등)는 버린다."""
    if not path:
        return None
    if not Path(path).is_absolute():
        return path.replace("\\", "/")
    if root:
        try:
            return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            return None
    return None


def _prompt_text(content) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
            return None
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(texts) if texts else None
    return None


def summarize(path: Path, patterns, git_cache: dict | None = None, keep_names: bool = False) -> dict | None:
    """대화 기록 파일 하나 → 세션 요약 (해석 없음, 세기만)."""
    git_cache = {} if git_cache is None else git_cache
    m = {k: 0 for k in ("prompts", "turns", "tool_calls", "edits", "reads", "bash", "test_runs", "commits",
                        "tool_errors", "web", "compactions", "unparsed_lines")}
    times, edited, prompts, branches = [], [], [], []
    session_id = cwd = None
    n_lines = 0
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            n_lines += 1
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                m["unparsed_lines"] += 1
                continue
            if not isinstance(d, dict):
                m["unparsed_lines"] += 1
                continue
            session_id = session_id or d.get("sessionId")
            cwd = cwd or d.get("cwd")
            if d.get("gitBranch") and d["gitBranch"] not in branches and branch_key(d["gitBranch"]):     # 메인라인 제외
                branches.append(d["gitBranch"])
            ts = d.get("timestamp")
            if ts:
                try:
                    times.append(dt.datetime.fromisoformat(ts.replace("Z", "+00:00")))
                except ValueError:
                    pass
            t = d.get("type")
            if t == "system" and d.get("subtype") == "compact_boundary":
                m["compactions"] += 1
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            content = msg.get("content")
            if t == "user":
                if isinstance(content, list):
                    m["tool_errors"] += sum(1 for c in content if isinstance(c, dict)
                                            and c.get("type") == "tool_result" and c.get("is_error"))
                text = None if d.get("isMeta") or d.get("isSidechain") else _prompt_text(content)
                if text is not None:
                    text = REMINDER.sub("", text).strip()
                    if text:
                        m["prompts"] += 1
                        prompts.append(text)
            elif t == "assistant":
                m["turns"] += 1
                for c in content if isinstance(content, list) else []:
                    if not isinstance(c, dict) or c.get("type") != "tool_use":
                        continue
                    m["tool_calls"] += 1
                    name, inp = c.get("name", ""), c.get("input") or {}
                    if name in EDIT_TOOLS:
                        m["edits"] += 1
                        edited.append(inp.get("file_path") or inp.get("notebook_path") or "")
                    elif name in READ_TOOLS:
                        m["reads"] += 1
                    elif name in WEB_TOOLS:
                        m["web"] += 1
                    elif name == "Bash":
                        m["bash"] += 1
                        cmd = str(inp.get("command", ""))
                        m["test_runs"] += bool(TEST_CMD.search(cmd))
                        m["commits"] += bool(COMMIT_CMD.search(cmd))
    if not session_id or not times:
        return None
    root = _repo_root(cwd, git_cache)
    paths: dict[str, None] = {}
    for fp in edited:
        rel = _relative(fp, root)
        if rel:
            paths.setdefault(rel, None)
    sub = path.with_suffix("") / "subagents"
    m["subagents"] = len(list(sub.glob("*.jsonl"))) if sub.is_dir() else 0
    m["duration_min"] = round((max(times) - min(times)).total_seconds() / 60, 1)
    return {"session_id": session_id, "start": min(times), "n_lines": n_lines, "metrics": m,
            "paths": list(paths), "branches": list(dict.fromkeys(branch_key(b, keep_names) for b in branches)),
            "prompts": prompts, "issue_keys": patterns.issue_keys(*prompts, *branches)}


def collect(p, cfg: dict, source: dict, patterns, state: dict) -> Result:
    base = Path(source.get("projects_dir") or "~/.claude/projects").expanduser()
    name = source.get("name") or "claude-code"
    include = source.get("include") or []
    idle = float(source.get("min_idle_minutes", 30)) * 60
    use_excerpt = source.get("excerpt", True)
    result = Result()
    if not base.is_dir():
        result.notes.append(f"{base} 가 없다 — 이 컴퓨터에 Claude Code 기록이 없거나 경로가 다르다")
        return result
    collected_at = now_iso()
    seen = dict(state.get("seen", {}))
    git_cache: dict = {}
    skipped_active = 0
    oldest = None
    for project in sorted(d for d in base.iterdir() if d.is_dir()):
        if include and not any(s in project.name for s in include):
            continue
        for path in sorted(project.glob("*.jsonl")):
            stat = path.stat()
            if time.time() - stat.st_mtime < idle:
                skipped_active += 1
                continue
            sig = f"{stat.st_size}:{int(stat.st_mtime)}"
            if seen.get(str(path)) == sig:
                continue
            s = summarize(path, patterns, git_cache, bool(source.get("keep_branch_names", False)))
            seen[str(path)] = sig
            if not s:
                continue
            oldest = min(oldest, s["start"]) if oldest else s["start"]
            obs_id = f"ai_session:{name}:{s['session_id']}#{s['n_lines']}"
            event = {
                "obs_id": obs_id,
                "source": {"system": "ai_session", "instance": name, "collector_kind": "cli"},
                "kind": "ai_session.session",
                "ts": s["start"].astimezone().isoformat(timespec="seconds"),
                "actor_role": "self",
                "objects": [{"type": "ai_session", "id": s["session_id"]}],
                "keys": {k: v for k, v in (("paths", s["paths"]), ("issue_keys", s["issue_keys"]),
                                            ("branches", s["branches"])) if v},
                "metrics": s["metrics"],
                "excerpt_ref": None,
                "partial": s["metrics"]["unparsed_lines"] > 0,
                "collector": COLLECTOR,
                "collected_at": collected_at,
            }
            result.events.append(event)
            if use_excerpt and s["prompts"]:
                result.excerpts[obs_id] = excerpt(s["prompts"][0], int(source.get("excerpt_chars", 300)))
    result.state = {"seen": seen, "collected_at": collected_at}
    if skipped_active:
        result.notes.append(f"지금 쓰이는 세션 {skipped_active}개는 다음 수집으로 미룬다 (min_idle_minutes)")
    if oldest and (dt.datetime.now(dt.timezone.utc) - oldest).days >= 25:
        result.notes.append("가장 오래된 기록이 25일을 넘었다 — Claude Code 는 기본 30일 뒤 지우므로 수집 주기를 확인하라")
    return result
