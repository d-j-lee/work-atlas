"""점검 — 이 인스턴스가 지금 무엇을 할 수 있고, 무엇이 빠졌고, 다음에 무엇을 하면 되는지 한 번에.

  python -m atlas doctor [--root …] [--json]

본다: 실행 환경(파이썬·의존성) · 설정(소스 경로·자격 증명 환경 변수가 있는지 — 값은 보지 않는다 · 역할 매핑) ·
방법(스키마 동기화 · 카테고리 정의) · 데이터(관측 · 마지막 수집 · 세대 · 모델 형식 · 카테고리 · 경고) ·
소비(커밋 전 검사 · Claude Code 훅 · MCP) · LLM(백엔드 · 실행 파일 · 인증 환경 변수 · 이번 달 지출).
표시: ok(됨) · ! (권함 — 없어도 돈다) · ✗(막힘 — 고쳐야 그 기능이 돈다). ✗ 가 하나라도 있으면 종료 코드 1.
아무것도 바꾸지 않는다.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, config as configmod
from .common import paths

OK, WARN, FAIL = "ok", "!", "✗"


def _git_hooks(root: Path) -> str | None:
    try:
        return subprocess.run(["git", "config", "core.hooksPath"], cwd=root, capture_output=True, text=True).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _hook_installed(root: Path, cfg: dict) -> list[Path]:
    from . import workspace as ws
    places = [root / ".claude" / "settings.json", root / ".claude" / "settings.local.json", Path.home() / ".claude" / "settings.json"]
    places += [s.root / ".claude" / "settings.json" for s in ws.sources(cfg, root)]
    for g in cfg.get("sources", {}).get("git", []):
        if g.get("path"):
            places.append(Path(g["path"]).expanduser() / ".claude" / "settings.json")
    found = []
    for f in places:
        try:
            if "atlas.hook" in f.read_text(encoding="utf-8"):
                found.append(f)
        except OSError:
            pass
    return found


def run(p, cfg: dict) -> list[dict]:
    rows: list[dict] = []

    def add(area, status, what, action=""):
        rows.append({"area": area, "status": status, "what": what, "action": action})

    # 실행 환경
    add("환경", OK if sys.version_info >= (3, 11) else FAIL, f"work-atlas {__version__} · Python {sys.version.split()[0]}",
        "" if sys.version_info >= (3, 11) else "Python 3.11 이상")
    for mod, need in (("yaml", True), ("jsonschema", True), ("mcp", False)):
        have = importlib.util.find_spec(mod) is not None
        add("환경", OK if have else (FAIL if need else WARN), f"{mod} {'있음' if have else '없음'}",
            "" if have else ("pip install -r requirements.txt" if need else 'MCP 서버를 쓰려면 pip install "mcp>=1.2"'))
    # 설정
    if not (p.root / "atlas.toml").exists():
        add("설정", WARN, "atlas.toml 이 없다 — 기본값으로 돈다", "atlas.example.toml 을 atlas.toml 로 복사해 소스를 채운다 (BOOTSTRAP.md)")
    src = cfg.get("sources", {})
    if not any(src.get(k) for k in ("git", "jira", "claude_code", "workspace")):
        add("설정", WARN, "설정된 소스가 없다", "atlas.toml 의 [[sources.*]]")
    for g in src.get("git", []):
        path = Path(str(g.get("path", ""))).expanduser()
        add("설정", OK if (path / ".git").exists() else FAIL, f"git:{g.get('name')} {path}",
            "" if (path / ".git").exists() else "저장소 경로를 고친다")
    for j in src.get("jira", []):
        env = j.get("auth_env") or ""
        if not env:
            add("설정", WARN, f"jira:{j.get('name')} 자격 증명 없이(익명) 수집한다", "사내 트래커면 auth_env 에 환경 변수 이름을")
        else:
            have = bool(os.environ.get(env))
            add("설정", OK if have else FAIL, f"jira:{j.get('name')} 자격 증명 환경 변수 {env} {'있음' if have else '없음'}",
                "" if have else f"환경 변수 {env} 를 둔다 (값은 atlas.toml 에 적지 않는다)")
    for c in src.get("claude_code", []):
        d = Path(c.get("projects_dir") or "~/.claude/projects").expanduser()
        add("설정", OK if d.exists() else WARN, f"claude-code:{c.get('name')} {d}", "" if d.exists() else "Claude Code 를 이 컴퓨터에서 쓴 적이 있는지")
    from . import workspace as ws
    for s in ws.sources(cfg, p.root):
        add("설정", OK if s.root.exists() else WARN, f"작업 공간:{s.name} {s.root}", "" if s.root.exists() else "atlas work init 으로 만든다")
    from .common import Codebook
    for msg in configmod.validate(cfg, p.root):
        add("설정", FAIL, msg, "atlas.toml [identity]")
    try:
        cb = Codebook.load(p)
        bad = sorted(set((cfg.get("identity") or {}).get("roles") or {}) - cb.facet_values("roles"))
        add("설정", FAIL if bad else OK, f"역할 매핑 {len((cfg.get('identity') or {}).get('roles') or {})}개" + (f" — 코드북에 없는 역할 {bad}" if bad else ""),
            "identity.roles 의 키는 codebook roles 의 id" if bad else
            ("" if (cfg.get("identity") or {}).get("roles") else "팀 메일·그룹 패턴을 identity.roles 에 — 순서 그림·접점에 역할이 나온다"))
        if not (cfg.get("identity") or {}).get("roles"):
            rows[-1]["status"] = WARN
    except (FileNotFoundError, KeyError) as e:
        add("방법", FAIL, f"코드북을 읽지 못했다: {e}", "method/codebook.yaml")
    # 방법
    from . import schema_gen
    try:
        add("방법", OK if schema_gen.check(p.root) else FAIL, "주석 스키마가 코드북과 같다" if schema_gen.check(p.root) else "주석 스키마가 코드북과 어긋난다",
            "" if schema_gen.check(p.root) else "python -m atlas gen-schema")
    except FileNotFoundError:
        add("방법", FAIL, "method/ 가 없다", "인스턴스 루트가 맞는지 (--root)")
    from .taxonomy import load_data, problems
    try:
        probs = problems(load_data(p.root))
        add("방법", FAIL if probs else OK, "카테고리 정의 " + ("; ".join(probs) if probs else "정상"), "method/categories.yaml" if probs else "")
    except Exception as e:                                   # noqa: BLE001 — 사람에게 보일 진단이다
        add("방법", FAIL, f"카테고리 정의를 읽지 못했다: {e}", "method/categories.yaml")
    # 데이터
    obs_files = sorted(p.observations.glob("*.jsonl")) if p.observations.exists() else []
    if not obs_files:
        add("데이터", WARN, "관측이 없다", "atlas collect all → atlas build")
    else:
        n = sum(1 for f in obs_files for _ in f.open(encoding="utf-8"))
        add("데이터", OK, f"관측 {n:,}줄 · 월 파일 {len(obs_files)}개")
    gen = _json(p.model / "generation.json")
    if gen:
        from .mine import MODEL_FORMAT
        add("데이터", OK if gen.get("format") == MODEL_FORMAT else FAIL, f"세대 {gen.get('generation')} · 기준 {str(gen.get('as_of'))[:10]}",
            "" if gen.get("format") == MODEL_FORMAT else "atlas build (모델이 옛 형식)")
        if not (p.model / "categories.json").exists():
            add("데이터", FAIL, "카테고리 모델이 없다", "atlas build")
    elif obs_files:
        add("데이터", FAIL, "모델이 없다", "atlas build")
    cat = _json(p.model / "catalog.json")
    if cat:
        cg = cat.get("categories") or {}
        if cg:
            add("데이터", OK if (cg.get("ratio") or 0) <= float(cfg["policy"].get("uncategorized_max", 0.3)) else WARN,
                f"업무 카테고리 {cg.get('categories')} · 미분류 {cg.get('uncategorized')}/{cg.get('units')}",
                "" if (cg.get("ratio") or 0) <= float(cfg["policy"].get("uncategorized_max", 0.3)) else
                f"경로 규칙 후보 {cg.get('path_suggestions')}개 — data/reports/guides/README.md")
        for w in cat.get("warnings", [])[:5]:
            add("데이터", WARN, f"[{w['level']}] {w['what']} ({w['evidence']})", w["action"])
    # 소비
    hp = _git_hooks(p.root)
    add("소비", OK if hp == ".githooks" else WARN, f"커밋 전 검사 {'켜짐' if hp == '.githooks' else '꺼짐'}",
        "" if hp == ".githooks" else "git config core.hooksPath .githooks")
    hooks = _hook_installed(p.root, cfg)
    add("소비", OK if hooks else WARN, "Claude Code 훅 " + (f"설치됨 ({hooks[0]})" if hooks else "찾지 못함"),
        "" if hooks else "atlas hook --print-settings 를 .claude/settings.json 에")
    # LLM
    lm = cfg.get("llm") or {}
    backend = lm.get("backend", "none")
    if backend == "none":
        add("LLM", WARN, "백엔드 none — LLM 작업은 prompt · import 로만(대화 세션을 백엔드로)",
            "사내에서 승인한 백엔드가 있으면 [llm] backend = claude 또는 command (policy.md §1)")
    elif backend == "claude":
        from .llm import ClaudeBackend
        exe = ClaudeBackend(cfg).executable()
        found = bool(shutil.which(exe) or Path(exe).exists())
        add("LLM", OK if found else FAIL, f"claude 실행 파일 {exe}", "" if found else "Claude Code 설치 또는 [llm] claude_path")
        if lm.get("bare", True):
            key = bool(os.environ.get("ANTHROPIC_API_KEY")) or any(os.environ.get(v) for v in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"))
            add("LLM", OK if key else WARN, f"--bare 인증 {'환경 변수 있음' if key else 'ANTHROPIC_API_KEY 등 없음'}",
                "" if key else "--bare 는 로그인(OAuth)을 읽지 않는다 — ANTHROPIC_API_KEY · apiKeyHelper · Bedrock/Vertex 자격 증명, 또는 [llm] bare = false")
    elif backend == "command":
        import shlex
        cmd = lm.get("command") or []
        cmd = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
        found = bool(cmd and (shutil.which(cmd[0]) or Path(cmd[0]).exists()))
        add("LLM", OK if found else FAIL, f"게이트웨이 명령 {' '.join(cmd) or '(없음)'}", "" if found else "[llm] command")
    from . import llm as llmmod
    spent = llmmod.month_spent(p)
    cap = float(lm.get("max_usd_per_month", 50))
    add("LLM", OK if spent < 0.8 * cap else WARN, f"이번 달 지출 ${spent:.2f} / ${cap:.2f} · 자동 작업 {lm.get('auto') or '없음'}")
    return rows


def _json(f: Path):
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas doctor", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    p = paths(args.root)
    rows = run(p, configmod.load(p.root))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        area = None
        for r in rows:
            if r["area"] != area:
                area = r["area"]
                print(f"\n[{area}]")
            print(f"  {r['status']:<2} {r['what']}" + (f"\n       → {r['action']}" if r["action"] and r["status"] != OK else ""))
        bad = sum(1 for r in rows if r["status"] == FAIL)
        print(f"\n막힘 {bad} · 권함 {sum(1 for r in rows if r['status'] == WARN)} · 됨 {sum(1 for r in rows if r['status'] == OK)}")
    return 1 if any(r["status"] == FAIL for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
