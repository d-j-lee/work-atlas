"""Claude Code 대화 기록 수집기 — 세션 하나가 관측 하나 (ai_session.session).

AI 세션은 티켓 없이 한 일이 가장 많이 남는 곳이다(METHODOLOGY.md §10). 본문은 가져오지 않고 행동의 흔적만 센다.
  keys     paths(편집한 파일, 그 파일이 든 저장소의 루트 기준) · issue_keys(프롬프트·브랜치)
           · branches(브랜치 해시 키 — git 수집기와 같은 규칙) · works(세션을 연 작업 폴더의 작업 id — 작업 공간 수집기와 같은 규칙)
  metrics  prompts · turns · tool_calls · edits · reads · bash · test_runs · commits · tool_errors · web
           · subagents · compactions · duration_min · unparsed_lines · work_edits(작업 공간 안 파일의 편집 — 코드 경로로 세지 않는다)
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
from .. import workspace as ws
from ..common import now_iso

COLLECTOR = "claude-code@2"
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


def _toplevel(directory: str | Path | None, cache: dict) -> str | None:
    """그 폴더가 든 git 저장소의 루트(없으면 None). 같은 폴더는 한 번만 묻는다."""
    if not directory:
        return None
    key = ("top", str(directory))
    if key not in cache:
        top = None
        if Path(directory).is_dir():
            out = subprocess.run(["git", "-C", str(directory), "rev-parse", "--show-toplevel"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace")
            if out.returncode == 0 and out.stdout.strip():
                top = out.stdout.strip()
        cache[key] = top
    return cache[key]


def _repo_root(cwd: str | None, cache: dict) -> str | None:
    """세션 폴더의 저장소 루트, 저장소가 아니면 세션 폴더 자체."""
    return (_toplevel(cwd, cache) or cwd) if cwd else None


def _parent_dir(pth: Path) -> Path | None:
    return next((d for d in pth.parents if d.is_dir()), None)


def _file_root(path: str, cwd_root: str | None, cache: dict) -> str | None:
    """세션 폴더가 저장소가 아닐 때(작업 폴더 등) 편집한 파일이 든 저장소의 루트 — --add-dir 로 고친 저장소 파일을
    그 저장소 기준으로 남긴다. 어느 저장소에도 없으면 세션 폴더 기준(이전과 같다)."""
    pth = Path(path)
    if not pth.is_absolute():
        return cwd_root
    return _toplevel(_parent_dir(pth), cache) or cwd_root


def _in_workspace(path: str, cwd: str | None, srcs, cache: dict) -> bool:
    """작업 공간 안의 파일(작업 기록·근거 등)인가. 작업 폴더 안에 받아 둔 저장소 사본의 파일은 코드다 — 그 파일의
    저장소 루트가 작업 공간 루트보다 안쪽이면 작업 공간 파일이 아니다(작업 공간 자체가 저장소인 경우는 작업 공간 파일)."""
    if not srcs or not path:
        return False
    pth = Path(path) if Path(path).is_absolute() else Path(cwd or ".") / path
    try:
        pth = pth.resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    for src in srcs:
        try:
            pth.relative_to(src.root)
        except ValueError:
            continue
        top = _toplevel(_parent_dir(pth), cache)
        return not (top and src.root in Path(top).resolve().parents)
    return False


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


def summarize(path: Path, patterns, git_cache: dict | None = None, keep_names: bool = False, srcs=None,
              ids: dict | None = None) -> dict | None:
    """대화 기록 파일 하나 → 세션 요약 (해석 없음, 세기만).
    srcs 는 작업 공간 소스(workspace.sources), ids 는 소스 이름 → workspace.work_ids (작업 공간 수집기와 같은 작업 id)."""
    git_cache = {} if git_cache is None else git_cache
    srcs = srcs or []
    ids = ids or {}
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
    git_root = _toplevel(cwd, git_cache)
    root = git_root or cwd
    works = []
    loc = ws.locate(cwd, srcs) if cwd and srcs else None
    if loc:
        src, wdir, wrel = loc
        works.append(src.object_id(ids.get(src.name, {}).get(wrel) or ws.work_id(src, wdir, wrel)))
    paths: dict[str, None] = {}
    m["work_edits"] = 0
    for fp in edited:
        if _in_workspace(fp, cwd, srcs, git_cache):  # 작업 기록·근거 파일은 코드가 아니다 — 경로 겹침 연결에 섞지 않는다
            m["work_edits"] += 1
            continue
        # 저장소에서 연 세션은 그 저장소의 파일만(이전과 같다 — 경로에 저장소 구분이 없어 남의 저장소 파일이 엉뚱하게 겹치지 않게).
        # 저장소가 아닌 폴더(작업 폴더)에서 연 세션은 파일마다 그 파일의 저장소 기준.
        rel = _relative(fp, root if git_root else _file_root(fp, root, git_cache))
        if rel:
            paths.setdefault(rel, None)
    sub = path.with_suffix("") / "subagents"
    m["subagents"] = len(list(sub.glob("*.jsonl"))) if sub.is_dir() else 0
    m["duration_min"] = round((max(times) - min(times)).total_seconds() / 60, 1)
    return {"session_id": session_id, "start": min(times), "n_lines": n_lines, "metrics": m,
            "paths": list(paths), "branches": list(dict.fromkeys(branch_key(b, keep_names) for b in branches)),
            "prompts": prompts, "issue_keys": patterns.issue_keys(*prompts, *branches), "works": works}


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
    srcs = ws.sources(cfg, p.root)
    ids = {src.name: ws.work_ids(src) for src in srcs}
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
            s = summarize(path, patterns, git_cache, bool(source.get("keep_branch_names", False)), srcs, ids)
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
                                            ("branches", s["branches"]), ("works", s["works"])) if v},
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
