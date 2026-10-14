"""백엔드 — none(보내지 않는다) · claude(Claude Code 헤드리스) · command(사내 게이트웨이). 응답 모양이 틀려도 죽지 않는다."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass

from .base import canon

class NoBackend(RuntimeError):
    pass


@dataclass
class Reply:
    output: dict | None
    cost_usd: float = 0.0
    model: str = ""
    error: str | None = None
    seconds: float = 0.0
    cost_known: bool = True


LLM_ENV = "ATLAS_LLM_CALL"        # LLM 하위 프로세스에 둔다 — atlas 훅은 이 값을 보면 아무것도 넣지 않는다(입력 오염 방지)


def run_process(args: list[str], stdin: str, timeout: int, cwd: str | None = None,
                env: dict | None = None) -> tuple[int, str, str]:
    """하위 프로세스 — 시간이 넘으면 자식까지 끝낸다(.cmd → node 처럼 손주가 남지 않게). 출력은 UTF-8, 깨진 바이트는 대체 문자로."""
    import os
    import signal
    kw = ({"start_new_session": True} if os.name != "nt" else      # Windows: 프로세스 그룹 + 콘솔 창 없음(무인 실행에서 창이 뜨지 않게)
          {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)})
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                            text=True, encoding="utf-8", errors="replace", env=env, **kw)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            proc.kill()
        proc.communicate()
        raise
    return proc.returncode, out, err


class Backend:
    name = "none"

    def __init__(self, cfg: dict):
        self.cfg = cfg.get("llm", {})

    @property
    def model(self) -> str:
        return str(self.cfg.get("model") or "")

    def call(self, system: str, prompt: str, schema: dict, budget_usd: float) -> Reply:
        raise NoBackend("LLM 백엔드가 없다(atlas.toml [llm] backend = none). atlas llm prompt 로 뽑아 대화 세션에서 쓰고 import 로 들인다")


WINDOWS_CMD_LIMIT = 8000          # cmd.exe 한 줄 상한(8,191자) — claude.cmd 로 부르면 여기에 걸린다


class ClaudeBackend(Backend):
    """Claude Code 헤드리스. 격리는 세 겹이다:
    1) 실행 모드 — bare = true 면 --bare(훅·CLAUDE.md·로그인 모두 읽지 않음, API 키 필요). bare = false 면 로그인(구독)을 쓰되
       --safe-mode 가 있는 CLI 에서는 그것으로 사용자 설정의 CLAUDE.md·훅·MCP·스킬을 끈다.
    2) 빈 임시 폴더에서, 도구를 모두 끄고(--tools "") MCP 를 막는다(--strict-mcp-config).
    3) 하위 프로세스에 ATLAS_LLM_CALL=1 — 그래도 남은 atlas 훅이 있으면 아무것도 넣지 않는다.
    시스템 프롬프트는 파일로 넘긴다(--system-prompt-file) — Windows 명령 줄 상한을 피하고, 모르는 CLI 면 한 번 인라인으로 다시."""
    name = "claude"
    _help: dict[str, str] = {}
    _inline: set[str] = set()

    def executable(self) -> str:
        given = self.cfg.get("claude_path") or ""
        return (shutil.which(given) or given) if given else (shutil.which("claude") or "claude")

    def help_text(self) -> str:
        exe = self.executable()
        if exe not in ClaudeBackend._help:
            try:
                _, out, err = run_process([exe, "--help"], "", 30)
                ClaudeBackend._help[exe] = out + err
            except (OSError, subprocess.TimeoutExpired):
                ClaudeBackend._help[exe] = ""
        return ClaudeBackend._help[exe]

    def isolation(self) -> str:
        """bare · safe-mode · env-only(이 CLI 에는 --safe-mode 가 없다 — 사용자 설정의 CLAUDE.md·훅이 낄 수 있다)."""
        if self.cfg.get("bare", True):
            return "bare"
        return "safe-mode" if "--safe-mode" in self.help_text() else "env-only"

    def args(self, system: str, schema: dict, budget_usd: float, system_file: str | None = None) -> list[str]:
        mode = {"bare": ["--bare"], "safe-mode": ["--safe-mode"]}.get(self.isolation(), [])
        a = [self.executable()] + mode + [
            "-p", "--output-format", "json", "--json-schema", canon(schema), "--tools", "", "--strict-mcp-config",
            "--no-session-persistence"]
        a += ["--system-prompt-file", system_file] if system_file else ["--system-prompt", system]
        a += ["--max-budget-usd", f"{max(0.01, budget_usd):.2f}"]
        if self.model:
            a += ["--model", self.model]
        return a

    def call(self, system: str, prompt: str, schema: dict, budget_usd: float) -> Reply:
        import os
        from pathlib import Path
        env = {**os.environ, LLM_ENV: "1"}
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="atlas-llm-", ignore_cleanup_errors=True) as d:   # 빈 폴더 — CLAUDE.md·훅이 끼지 않게
            work = Path(d) / "work"
            work.mkdir()
            sp = Path(d) / "system-prompt.md"                         # 작업 폴더 밖에 둔다 — 세션이 읽을 파일이 아니다
            sp.write_text(system, encoding="utf-8")
            inline = self.executable() in ClaudeBackend._inline
            for attempt in range(2):
                args = self.args(system, schema, budget_usd, None if inline else str(sp))
                if os.name == "nt" and len(subprocess.list2cmdline(args)) > WINDOWS_CMD_LIMIT:
                    return Reply(None, error=f"명령 줄이 Windows 상한을 넘는다({len(subprocess.list2cmdline(args))}자) — "
                                             "backend = command(게이트웨이)나 WSL 에서 돌린다", cost_known=True)
                try:
                    code, out, err = run_process(args, prompt, int(self.cfg.get("timeout_s", 300)), cwd=str(work), env=env)
                except subprocess.TimeoutExpired:
                    return Reply(None, error="시간 초과", seconds=time.monotonic() - t0, cost_known=False)
                except OSError as e:
                    return Reply(None, error=f"실행 실패: {e}", seconds=time.monotonic() - t0)
                if not inline and code != 0 and "--system-prompt-file" in (err or "") and "unknown option" in (err or ""):
                    ClaudeBackend._inline.add(self.executable())        # 옛 CLI — 인자로 다시(호출 전에 거부돼 비용 없음)
                    inline = True
                    continue
                break
        return parse_reply(code, out, err, self.model, time.monotonic() - t0)


class CommandBackend(Backend):
    name = "command"

    def call(self, system: str, prompt: str, schema: dict, budget_usd: float) -> Reply:
        import shlex
        cmd = self.cfg.get("command") or []
        cmd = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
        if not cmd:
            raise NoBackend("backend = command 인데 [llm] command 가 비었다")
        req = {"system": system, "prompt": prompt, "schema": schema, "model": self.model, "max_budget_usd": budget_usd}
        t0 = time.monotonic()
        try:
            code, out, err = run_process(cmd, json.dumps(req, ensure_ascii=False), int(self.cfg.get("timeout_s", 300)))
        except subprocess.TimeoutExpired:
            return Reply(None, error="시간 초과", seconds=time.monotonic() - t0, cost_known=False)
        except OSError as e:
            return Reply(None, error=f"실행 실패: {e}", seconds=time.monotonic() - t0)
        return parse_reply(code, out, err, self.model, time.monotonic() - t0)


def _money(x) -> float | None:
    try:
        v = float(x)
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None


def parse_reply(code: int, stdout: str, stderr: str, model: str, seconds: float) -> Reply:
    """claude -p --output-format json 의 결과(structured_output · result · total_cost_usd) 또는 command 의 응답({output, cost_usd, model}).
    모양이 틀려도 죽지 않고 오류로 돌려준다. 비용이 없으면 모른다고 표시한다(예산은 상한으로 친다)."""
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return Reply(None, error=f"응답이 JSON 이 아니다 (종료 코드 {code}): {(stderr or stdout)[:200]}", seconds=seconds, cost_known=False)
    if not isinstance(data, dict):
        return Reply(None, error=f"응답이 JSON 객체가 아니다 (종료 코드 {code})", seconds=seconds, cost_known=False)
    cost = next((v for v in (_money(data.get("total_cost_usd")), _money(data.get("cost_usd"))) if v is not None), None)
    usage = data.get("modelUsage") if isinstance(data.get("modelUsage"), dict) else {}
    # Claude Code 는 보조 일에 작은 모델을 함께 쓴다 — 답을 낸 모델은 비용(없으면 출력 토큰)이 가장 큰 것
    weight = lambda v: ((_money(v.get("costUSD")) or 0.0), float(v.get("outputTokens") or 0)) if isinstance(v, dict) else (0.0, 0.0)
    used = str(data.get("model") or max(usage, key=lambda k: weight(usage[k]), default="") or model)
    out = data.get("structured_output") if isinstance(data.get("structured_output"), dict) else data.get("output")
    if not isinstance(out, dict) and isinstance(data.get("result"), str):
        try:
            out = json.loads(data["result"])
        except json.JSONDecodeError:
            out = None
    err = None
    if code != 0 or data.get("is_error"):
        err = f"오류 (종료 코드 {code}, {data.get('subtype') or ''}): {(stderr or str(data.get('result', '')))[:200]}"
    elif not isinstance(out, dict):
        err = "구조화된 출력이 없다"
    return Reply(out if isinstance(out, dict) and not err else None, cost or 0.0, used, err, seconds, cost is not None)


def backend_for(cfg: dict) -> Backend:
    name = (cfg.get("llm") or {}).get("backend", "none")
    return {"claude": ClaudeBackend, "command": CommandBackend}.get(name, Backend)(cfg)
