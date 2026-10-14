"""처음 설정 — 저장소의 git 기록에서 이슈 키 접두 · 나의 식별자 · 저장소를 읽어 atlas.toml 초안을 만든다.

  python -m atlas init --repo D:\\src\\server [--repo …] [--jira-url https://jira.회사] [--workspace D:\\work]
                       [--since 2025-10-01] [--print] [--force]

- 읽기만 한다(git log · git config · Claude Code 기록 폴더 이름). 원천에 쓰지 않는다.
- 이슈 키 접두: 최근 커밋 제목과 브랜치 이름에서 키 모양(ABC-123)을 세어, 3번 이상 · 키 모양의 2% 이상인 접두만 keys.projects 로.
  그 접두로 본 커밋 키 커버리지(제목에 키가 있는 커밋 비율)를 미리 보여 준다 — 상관의 상한이 여기서 정해진다.
- 나의 식별자: 저장소마다 git config user.email · user.name. 사람 식별자는 판정에만 쓰이고 관측에는 남지 않는다.
- Claude Code: 이 컴퓨터의 ~/.claude/projects 가 있으면 저장소 경로를 Claude Code 식으로 바꾼 이름(C--src-server 처럼)을 include 로.
- 이미 atlas.toml 이 있으면 쓰지 않고 표준 출력으로만 낸다(--force 로 덮는다). 쓴 뒤 설정을 읽어 검사한다.
- 경로는 Windows 역슬래시도 그대로 쓴다(TOML 문자열로 이스케이프).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from . import config as configmod
from .common import NO_WINDOW, paths, write_text

KEY = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,9})-[0-9]{1,6}(?![0-9])")


def _git(repo: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", **NO_WINDOW,
                           timeout=120)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def toml_str(s: str) -> str:
    """TOML 기본 문자열 — JSON 문자열 규칙과 같다(역슬래시 · 따옴표 · 제어 문자 이스케이프)."""
    return json.dumps(str(s), ensure_ascii=False)


def source_name(repo: Path, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", repo.name.lower()).strip("-") or "repo"
    name, i = base, 2
    while name in taken:
        name, i = f"{base}-{i}", i + 1
    taken.add(name)
    return name


def claude_project_name(path: Path) -> str:
    """Claude Code 가 세션을 저장하는 폴더 이름 — 작업 폴더 경로의 영숫자 아닌 글자를 '-' 로 바꾼 것."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def scan_repo(repo: Path, since: str, exclude: set[str]) -> dict:
    top = _git(repo, "rev-parse", "--show-toplevel").strip()
    if not top:
        return {"path": repo, "ok": False}
    subjects = _git(repo, "log", f"--since={since}", "--no-merges", "--format=%s").splitlines()
    branches = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes").splitlines()
    prefixes = Counter(m for text in subjects + branches for m in KEY.findall(text) if m not in exclude)
    ident = [x.strip() for x in (_git(repo, "config", "user.email"), _git(repo, "config", "user.name")) if x.strip()]
    head = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD").strip()
    return {"path": Path(top), "given": Path(os.path.abspath(repo)), "ok": True, "subjects": subjects, "prefixes": prefixes,
            "identity": ident, "origin_head": head}


def choose_projects(prefixes: Counter, min_n: int = 3, min_share: float = 0.02) -> list[str]:
    total = sum(prefixes.values())
    return sorted(k for k, n in prefixes.items() if n >= min_n and n >= min_share * total)


def coverage(subjects: list[str], projects: list[str]) -> float | None:
    if not subjects:
        return None
    ok = set(projects)
    return round(sum(1 for s in subjects if any(m in ok for m in KEY.findall(s))) / len(subjects), 3)


def render(scans: list[dict], projects: list[str], identity: list[str], since: str, jira_url: str | None,
           workspace: Path | None, claude_dir: Path | None, claude_hits: dict[str, int]) -> str:
    taken: set[str] = set()
    L = ["# work-atlas 인스턴스 설정 — atlas init 이 git 기록에서 만든 초안. 고친 이유는 method/DECISIONS.md 에 한 줄.",
         "# 모든 칸의 뜻은 atlas.example.toml. 비밀값은 적지 않는다 — 자격 증명은 환경 변수 '이름'만.", "",
         "[identity]", "# 나를 가리키는 식별자(git 이메일·이름). Jira 사용자 이름·이메일·accountId 도 더한다 — 판정에만 쓰고 관측에는 남지 않는다",
         f"self = [{', '.join(toml_str(x) for x in identity)}]",
         "# 다른 사람을 역할로 — 팀 메일·그룹 계정·도메인 패턴 (역할 id 는 method/codebook.yaml roles). 첫 수집 전에 정한다",
         "# [identity.roles]", '# qa = ["@qa\\\\.company\\\\.example$"]', "", "[keys]",
         "# 최근 커밋 제목·브랜치 이름에서 센 이슈 키 접두 (3번 이상 · 2% 이상). 틀리면 고친다 — 아는 접두만 인정하는 것이 가장 정확하다",
         f"projects = [{', '.join(toml_str(x) for x in projects)}]", ""]
    for s in scans:
        if not s["ok"]:
            continue
        name = source_name(s["path"], taken)
        cov = coverage(s["subjects"], projects)
        L += ["[[sources.git]]", f"name = {toml_str(name)}                 # 관측 id 에 들어간다 — 바꾸지 않는다",
              f"path = {toml_str(s['path'])}",
              f"since = {toml_str(since)}" + (f"            # 최근 커밋 {len(s['subjects']):,}개 · 이슈 키 커버리지 {cov:.0%}"
                                               if cov is not None else ""),
              ("# ref = " + toml_str(s["origin_head"]) + "  # 비우면 원격 기본 브랜치") if s["origin_head"] else
              "# ref = \"origin/main\"        # 원격 기본 브랜치를 못 찾았다 — 작업 중인 브랜치를 모으지 않게 정한다", ""]
    if jira_url:
        cloud = "atlassian.net" in jira_url
        jql = (f"project in ({', '.join(projects)})" if projects else "project = ABC") + (" AND updated >= -365d" if cloud else "")
        L += ["[[sources.jira]]", 'name = "jira"', f'flavor = "{"cloud" if cloud else "datacenter"}"',
              f"base_url = {toml_str(jira_url.rstrip('/'))}", f"jql = {toml_str(jql)}",
              'auth_env = "ATLAS_JIRA_AUTH"    # datacenter: 개인 액세스 토큰 · cloud: "email:api_token" — 값은 환경 변수에',
              "# ca_bundle = \"C:/certs/company-root.pem\"   # 사내 CA 로 TLS 오류가 나면 (Windows 인증서 저장소에 있으면 필요 없다)",
              "# tls_strict = false              # Python 3.13+ 가 옛 사내 CA 를 '엄격 검사'로 거절할 때만 (atlas doctor --online 이 알려 준다)",
              'done_statuses = ["Resolved", "Closed", "Done", "완료", "해결됨"]', ""]
    else:
        L += ["# [[sources.jira]]               # 트래커가 Jira 면 atlas init --jira-url 로 다시 만들거나 여기를 채운다",
              '# name = "jira"', '# flavor = "datacenter"', '# base_url = "https://jira.company.example"',
              '# jql = "project = ABC"', '# auth_env = "ATLAS_JIRA_AUTH"', ""]
    if claude_dir:
        inc = list(claude_hits) or [claude_project_name(s["path"]) for s in scans if s["ok"]]
        hits = sum(claude_hits.values())
        L += ["[[sources.claude_code]]", 'name = "claude-code"', f"projects_dir = {toml_str(claude_dir)}",
              f"include = [{', '.join(toml_str(x) for x in inc)}]    # 이 경로에서 연 세션 — 지금 {hits}개 폴더가 맞는다",
              "# Claude Code 는 대화 기록을 기본 30일 뒤 지운다 — 주간 수확(atlas harvest)을 그보다 자주 돌린다", ""]
    if workspace:
        L += ["[[sources.workspace]]", 'name = "ws"', f"root = {toml_str(workspace)}", ""]
    return "\n".join(L).rstrip() + "\n"


def run(root: Path, repos: list[Path], since: str, jira_url: str | None = None, workspace: Path | None = None,
        claude_dir: Path | None = None) -> tuple[str, dict]:
    cfg = configmod.load(root)
    exclude = set(cfg["keys"].get("exclude_prefixes", []))
    scans = [scan_repo(r.expanduser(), since, exclude) for r in repos]
    prefixes = sum((s["prefixes"] for s in scans if s["ok"]), Counter())
    projects = choose_projects(prefixes)
    identity = list(dict.fromkeys(x for s in scans if s["ok"] for x in s["identity"]))
    if claude_dir is None:
        guess = Path.home() / ".claude" / "projects"
        claude_dir = guess if guess.is_dir() else None
    hits: dict[str, int] = {}
    if claude_dir:
        names = [d.name for d in claude_dir.iterdir() if d.is_dir()] if claude_dir.is_dir() else []
        # 같은 폴더도 적는 법이 여럿이다(git 의 긴 경로 · 넘겨받은 경로 · Windows 8.3 짧은 이름) — 실제로 있는 이름을 고르고, 없으면 git 의 것
        groups = [[s["path"], s.get("given") or s["path"]] for s in scans if s["ok"]] + ([[workspace, workspace.absolute()]] if workspace else [])
        for group in groups:
            cands = list(dict.fromkeys(claude_project_name(x) for x in group))
            found = {c: sum(1 for n in names if c in n) for c in cands}
            for c in [c for c in cands if found[c]] or cands[:1]:
                hits[c] = found[c]
    text = render(scans, projects, identity, since, jira_url, workspace, claude_dir, hits)
    summary = {"repos": [{"path": str(s["path"]), "ok": s["ok"], "commits": len(s.get("subjects", [])),
                          "coverage": coverage(s.get("subjects", []), projects) if s["ok"] else None} for s in scans],
               "projects": projects, "prefixes": prefixes.most_common(8), "identity": len(identity), "claude_hits": hits}
    return text, summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas init", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--repo", action="append", default=[], help="저장소 경로 (여럿이면 여러 번)")
    ap.add_argument("--jira-url")
    ap.add_argument("--workspace", help="작업 공간 루트 (일 하나에 폴더 하나 — WORKSPACE.md)")
    ap.add_argument("--claude-projects", help="Claude Code 기록 폴더 (기본 ~/.claude/projects)")
    ap.add_argument("--since", default=(dt.date.today() - dt.timedelta(days=365)).isoformat(), help="첫 수집 시작일 (기본 1년 전)")
    ap.add_argument("--print", action="store_true", help="쓰지 않고 표준 출력으로")
    ap.add_argument("--force", action="store_true", help="atlas.toml 이 있어도 덮는다")
    args = ap.parse_args(argv)
    p = paths(args.root)
    if not (p.root / "method" / "codebook.yaml").exists():
        print(f"[init] {p.root} 는 work-atlas 인스턴스가 아니다 — 인스턴스 저장소의 루트에서(또는 --root)", file=sys.stderr)
        return 2
    repos = [Path(r) for r in args.repo]
    if not repos:
        print("[init] --repo 로 저장소를 하나 이상 준다 (예: --repo D:\\src\\server)", file=sys.stderr)
        return 2
    text, s = run(p.root, repos, args.since, args.jira_url, Path(args.workspace).expanduser() if args.workspace else None,
                  Path(args.claude_projects).expanduser() if args.claude_projects else None)
    target = p.root / "atlas.toml"
    for r in s["repos"]:
        mark = "ok" if r["ok"] else "✗"
        cov = f" · 이슈 키 커버리지 {r['coverage']:.0%}" if r["coverage"] is not None else ""
        print(f"  {mark} {r['path']}" + (f" — 최근 커밋 {r['commits']:,}개{cov}" if r["ok"] else " — git 저장소가 아니다"), file=sys.stderr)
    print(f"  이슈 키 접두 {s['projects'] or '없음'} (센 것: {', '.join(f'{k} {n}' for k, n in s['prefixes']) or '-'})", file=sys.stderr)
    if s["claude_hits"]:
        print(f"  Claude Code 기록 폴더 일치 {sum(s['claude_hits'].values())}개", file=sys.stderr)
    if args.print or (target.exists() and not args.force):
        if target.exists() and not args.force and not args.print:
            print(f"[init] {target} 가 이미 있다 — 아래 초안에서 골라 붙이거나 --force 로 덮는다", file=sys.stderr)
        print(text)
        return 0
    write_text(target, text)
    configmod._load.cache_clear()
    try:
        configmod.load(p.root)
    except configmod.ConfigError as e:
        print(f"[init] 만든 설정에 오류가 있다 — {e}", file=sys.stderr)
        return 1
    print(f"[init] {target} 를 만들었다. 다음: atlas doctor --online → atlas collect all --dry-run → atlas collect all → atlas build",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
