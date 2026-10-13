"""수집 — 소스에서 결정론 관측을 모아 L0 에 덧붙인다.

  python -m atlas collect all                      설정(atlas.toml)의 모든 소스
  python -m atlas collect git [--source server]    한 종류(또는 이름 하나)만
  python -m atlas collect git --repo ../server --name server --since 2025-10-01   설정 없이 바로
  공통: --dry-run(쓰지 않고 보고만) · --full(증분 상태를 무시하고 처음부터) · --root

원칙 (method/schema.md L0):
- 해석하지 않는다. id·시각·역할·키·수치·원천 라벨만 뽑는다. 본문은 발췌 캐시로만, 가린 뒤 자른다.
- 사람은 역할(self·bot·unknown)로만 남긴다. 이름·이메일·계정 id 는 L0 에 들어가지 않는다.
- 읽기만 한다. 원천 시스템에 쓰지 않는다.
- 같은 사건은 언제 다시 수집해도 같은 obs_id 다. 저장소가 중복을 건너뛴다.
- 잘린 수집은 partial=true. 증분 상태(data/state/)는 쓰기가 끝난 뒤에만 앞으로 옮긴다.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import re
import sys
from dataclasses import dataclass, field

from .. import config as configmod
from ..common import paths
from ..store import append_observations, format_report, read_state, write_state

COLLECTORS = {
    "git": ("atlas.collect.git", "git"),
    "jira": ("atlas.collect.jira", "jira"),
    "claude-code": ("atlas.collect.claude_code", "claude_code"),
    "workspace": ("atlas.collect.workspace", "workspace"),
}


@dataclass
class Result:
    events: list[dict] = field(default_factory=list)
    excerpts: dict[str, str] = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _sources(cfg: dict, kind: str, args, module) -> list[dict]:
    adhoc = module.source_from_args(args)
    if adhoc:
        return [adhoc]
    found = list(cfg["sources"].get(COLLECTORS[kind][1], []))
    if args.source:
        found = [s for s in found if s.get("name") == args.source]
    return found


def run_one(p, cfg, kind: str, source: dict, *, dry_run: bool, full: bool) -> int:
    module = importlib.import_module(COLLECTORS[kind][0])
    name = source.get("name") or kind
    state_name = f"{kind}-{name}"
    state = {} if full else read_state(p, state_name)
    result: Result = module.collect(p, cfg, source, configmod.Patterns(cfg), state)
    report = append_observations(p, result.events, result.excerpts, dry_run=dry_run)
    print(format_report(f"{kind}:{name}", report, dry_run))
    for note in result.notes:
        print(f"  참고: {note}")
    if report["invalid"]:
        print("  [실패] 계약 위반 사건이 있어 증분 상태를 옮기지 않는다 — 수집기를 고쳐 다시 돌려라", file=sys.stderr)
        return 1
    if not dry_run and result.state:
        write_state(p, state_name, result.state)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas collect", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=["all", *COLLECTORS])
    ap.add_argument("--root")
    ap.add_argument("--source", help="설정의 소스 이름 하나만")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--full", action="store_true", help="증분 상태를 무시하고 처음부터 (중복은 저장소가 건너뛴다)")
    for module_name, _ in COLLECTORS.values():
        importlib.import_module(module_name).add_args(ap)
    args = ap.parse_args(argv)
    p = paths(args.root)
    cfg = configmod.load(p.root)
    kinds = list(COLLECTORS) if args.kind == "all" else [args.kind]
    status, ran = 0, 0
    for kind in kinds:
        module = importlib.import_module(COLLECTORS[kind][0])
        for source in _sources(cfg, kind, args, module):
            ran += 1
            try:
                status |= run_one(p, cfg, kind, source, dry_run=args.dry_run, full=args.full)
            except CollectError as e:
                print(f"[{kind}:{source.get('name', kind)}] 수집 실패 — {e}", file=sys.stderr)
                status |= 1
    if not ran:
        print("수집할 소스가 없다 — atlas.toml 의 [[sources.*]] 를 채우거나 인자로 넘겨라 (atlas collect -h)", file=sys.stderr)
        return 2
    return status


class CollectError(RuntimeError):
    """원천에 닿지 못했거나 응답이 계약과 다르다. 부분 결과를 쓰지 않고 멈춘다."""


TRAILER = re.compile(r"(?im)^[ \t]*[A-Za-z][A-Za-z-]*-by:[^\n]*\n?")
# 메인라인: 접두 없는 이름만. 원격 접두는 normalize_branch 가 명시된 경우(origin/·upstream/·remote-tracking)에만 벗긴다
# — 'jdoe/main'·'release-notes-fix'·'hotfix/release' 를 메인라인으로 보지 않게.
MAINLINE = re.compile(r"^(?:master|main|develop|dev|trunk|release(?:[-/]v?\d[\w.\-]*)?|HEAD)$")


def normalize_branch(name: str, remote_tracking: bool = False) -> str:
    """브랜치 이름 정규화 — GitHub 'owner:branch', refs/heads·refs/remotes, origin/·upstream/,
    그리고 remote-tracking 머지("Merge remote-tracking branch 'APACHE/master'")의 원격 이름을 벗긴다."""
    n = (name or "").strip()
    if ":" in n and "/" not in n.split(":", 1)[0]:
        n = n.split(":", 1)[1]
    for prefix in ("refs/heads/", "refs/remotes/"):
        if n.startswith(prefix):
            n = n[len(prefix):]
            remote_tracking = remote_tracking or prefix == "refs/remotes/"
    if remote_tracking and "/" in n:
        n = n.split("/", 1)[1]
    for prefix in ("origin/", "upstream/"):
        if n.startswith(prefix):
            n = n[len(prefix):]
    return n


def is_mainline(name: str, remote_tracking: bool = False) -> bool:
    return bool(MAINLINE.match(normalize_branch(name, remote_tracking)))


def strip_trailers(text: str) -> str:
    """Signed-off-by · Co-authored-by · Reviewed-by 같은 사람 이름 트레일러 줄을 뺀다(기계적으로 지울 수 있는 이름)."""
    return TRAILER.sub("", text or "")


def branch_key(name: str, keep_names: bool = False, remote_tracking: bool = False) -> str | None:
    """브랜치 이름 → 조인 키. 메인라인이면 None.
    이름에 계정·별칭이 들어가는 관례(jdoe/PROJ-1, owner:patch-1, users/<별칭>/…)가 흔하므로 기본은 해시만 남긴다.
    머지 메시지와 AI 세션이 같은 규칙으로 정규화·해시하므로 조인은 그대로 된다. keep_branch_names=true 면 이름 그대로.
    해시는 소금 없는 가명이다 — 별칭 목록이 있으면 사전 대조로 되돌릴 수 있다. L0 가 사내 경계 안에 있다는 전제의 최소 조치다."""
    n = normalize_branch(name, remote_tracking)
    if not n or MAINLINE.match(n):
        return None
    if keep_names:
        return n
    return "b:" + hashlib.sha256(n.encode("utf-8")).hexdigest()[:12]


HOME_PATH = re.compile(r"(?<![\w.-])(?:/Users|/home)/[^/\s]+|(?<![\w.-])[A-Za-z]:\\Users\\[^\\\s]+", re.I)


def excerpt(text: str | None, limit: int = 1000) -> str:
    """발췌 캐시에 넣을 본문: 길이를 먼저 자르고(가림 비용 상한) 비밀값·연락처를 가린 뒤 다시 자른다.

    가림은 안전망이다 — 1차 방어는 '필요한 만큼만 발췌'다. 사람 이름은 본문에서 기계적으로 지울 수 없으므로
    발췌 상한을 짧게 두고, 정책이 본문 처리를 허용하지 않는 소스는 excerpt=false 로 끈다.
    홈 폴더 경로(/Users/<계정>·/home/<계정>·C:\\Users\\<계정>)는 계정 이름이 들어 있어 '~' 로 바꾼다 — 기계적으로 지울 수 있는 이름이다.
    """
    from ..render import mask
    text = mask(HOME_PATH.sub("~", (text or "")[: limit * 4])).strip()
    return text if len(text) <= limit else text[:limit] + "…"
