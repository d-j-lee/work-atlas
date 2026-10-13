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


def run_process(args: list[str], stdin: str, timeout: int, cwd: str | None = None) -> tuple[int, str, str]:
    """하위 프로세스 — 시간이 넘으면 자식까지 끝낸다(.cmd → node 처럼 손주가 남지 않게). 출력은 UTF-8, 깨진 바이트는 대체 문자로."""
    import os
    import signal
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                            text=True, encoding="utf-8", errors="replace", **kw)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
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
    name = "claude"

    def executable(self) -> str:
        given = self.cfg.get("claude_path") or ""
        return (shutil.which(given) or given) if given else (shutil.which("claude") or "claude")

    def args(self, system: str, schema: dict, budget_usd: float) -> list[str]:
        a = [self.executable()] + (["--bare"] if self.cfg.get("bare", True) else []) + [
            "-p", "--output-format", "json", "--json-schema", canon(schema), "--tools", "", "--strict-mcp-config",
            "--no-session-persistence", "--system-prompt", system, "--max-budget-usd", f"{max(0.01, budget_usd):.2f}"]
        if self.model:
            a += ["--model", self.model]
        return a

    def call(self, system: str, prompt: str, schema: dict, budget_usd: float) -> Reply:
        import os
        args = self.args(system, schema, budget_usd)
        if os.name == "nt" and len(subprocess.list2cmdline(args)) > WINDOWS_CMD_LIMIT:
            return Reply(None, error=f"명령 줄이 Windows 상한을 넘는다({len(subprocess.list2cmdline(args))}자) — "
                                     "backend = command(게이트웨이)나 WSL 에서 돌린다", cost_known=True)
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="atlas-llm-", ignore_cleanup_errors=True) as d:   # 빈 폴더 — CLAUDE.md·훅이 끼지 않게
            try:
                code, out, err = run_process(args, prompt, int(self.cfg.get("timeout_s", 300)), cwd=d)
            except subprocess.TimeoutExpired:
                return Reply(None, error="시간 초과", seconds=time.monotonic() - t0, cost_known=False)
            except OSError as e:
                return Reply(None, error=f"실행 실패: {e}", seconds=time.monotonic() - t0)
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
    used = str(data.get("model") or next(iter(usage), "") or model)
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
