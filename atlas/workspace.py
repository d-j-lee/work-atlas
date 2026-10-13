"""작업 공간 — 일 하나에 폴더 하나, 일의 정본은 그 폴더의 작업 기록(WORK.md). 형식과 이유는 WORKSPACE.md.

  python -m atlas work init     [--source 이름]                     루트에 공통 규칙(CLAUDE.md)을 둔다 — 있으면 건드리지 않는다
  python -m atlas work new      <이슈 키 | 짧은-이름> [--title …] [--slug …] [--source 이름]
  python -m atlas work context  [경로]                                그 작업의 CONTEXT.md 를 지금 모델로 다시 만든다
  python -m atlas work close    [경로] [--dropped]                    상태·종료일을 적고, 비어 있는 결과·회고를 알린다
  python -m atlas work status   [--all] [--json]                      작업 목록과 위생 표시

규칙
- 관리 단위는 폴더 트리가 아니라 파일이다. 폴더는 일 하나를 담는 그릇이고, 작업의 정체(id)는 작업 기록 front-matter 의 id,
  없으면 루트 기준 경로다. 파일은 역할(기록·지시·계획·결정·근거·산출물·그 밖)로 나눈다.
- 상태는 위치가 아니라 파일에 둔다 — Claude Code 세션은 시작 폴더에 묶여 저장되므로 폴더를 옮기면 이어 쓰기가 끊긴다.
- 이 모듈은 사람이 명령을 부를 때만 작업 폴더에 쓴다: 새 작업의 파일, CONTEXT.md(생성물), close 의 front-matter 두 줄.
  수집(atlas collect workspace)은 읽기만 한다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .common import REPO, glob_regex

RECORD = "WORK.md"
GENERATED = "CONTEXT.md"
GENERATED_MARK = "<!-- atlas:generated"
STATUSES = ("active", "waiting", "done", "dropped")
OPEN_STATUSES = ("active", "waiting")

# 역할 — 작업 폴더 기준 경로(소문자)에 먼저 맞는 것이 이긴다. 기록(WORK.md)은 따로 본다. 소스 설정 roles 로 역할별로 바꾼다.
ROLES: dict[str, list[str]] = {
    "instructions": ["claude.md", "claude.local.md", "agents.md", ".claude/**"],
    "plan": ["plan*.md", "spec*.md", "plan/**", "plans/**", "spec/**", "specs/**", "design/**",
             "**/*-plan.md", "**/*-spec.md", "**/*계획*", "**/*설계*"],
    "decision": ["decision*.md", "adr/**", "decisions/**", "**/adr-*.md", "**/*결정*"],
    "evidence": ["evidence/**", "logs/**", "log/**", "repro/**", "dumps/**", "screenshots/**",
                 "**/*.log", "**/*.har", "**/*.pcap", "**/*.hprof", "**/*.dmp", "**/*.trace",
                 "**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.gif", "**/*.webp"],
    "output": ["out/**", "output/**", "deliverables/**", "report*.md", "handoff*.md", "**/*보고*"],
}
ROLE_NAMES = ("record", *ROLES, "other")
# 관측하지 않는다(세지도 않는다): 버릴 것·편집기 흔적. 폴더 안의 저장소 사본(.git 이 있는 폴더)은 repos_inside 로 세기만 한다.
SCRATCH = ["scratch/**", "tmp/**", "temp/**", ".scratch/**", "**/.ds_store", "**/thumbs.db", "**/~$*", "**/*.swp", "**/*~"]
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".idea", ".vscode", ".gradle", ".mvn",
             ".tox", ".mypy_cache", ".pytest_cache", "target", "dist"}
# 비밀값으로 보이는 파일 — 열지 않고 sensitive 로 세기만 한다(파일 이름 기준, 대소문자 무시)
SENSITIVE = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx", "*.ppk", "*.ovpn",
             "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".netrc", ".pgpass", "*credentials*", "secrets.*",
             "*.secret", "*secret*.json", "*secret*.yml", "*secret*.yaml", "*secret*.env"]
SECTIONS = {   # 작업 기록의 '## ' 머리말 → 섹션. 별칭은 소문자로 비교한다
    "problem": ("문제", "현상", "배경", "problem", "context", "background", "symptom"),
    "process": ("처리", "접근", "진행", "작업", "approach", "work", "process", "log"),
    "result": ("결과", "result", "results", "outcome"),
    "retro": ("회고", "교훈", "다음에", "retro", "retrospective", "lessons", "learnings"),
}
COMMENT = re.compile(r"<!--.*?-->", re.S)
HEADING = re.compile(r"(?m)^##\s+(.+?)\s*#*\s*$")
H1 = re.compile(r"(?m)^#\s+(.+?)\s*$")


def _compile(patterns: list[str]):
    rxs = [glob_regex(p.lower()) for p in patterns]
    return lambda rel: any(rx.match(rel.lower()) for rx in rxs)


_SCRATCH = _compile(SCRATCH)
_SENSITIVE = _compile(SENSITIVE)


def _hidden(name: str) -> bool:
    """`_`·`.` 로 시작하는 폴더는 작업이 아니다(공용 `_shared`·템플릿·도구 폴더)."""
    return name.startswith(("_", "."))


# ---------------------------------------------------------------- 소스와 위치

MAX_LEVELS = 6            # 루트에서 이 깊이까지만 작업 기록을 찾는다 — find_works 와 locate 가 같은 경계를 쓴다
BOM = chr(0xFEFF)


@dataclass
class Source:
    name: str
    root: Path
    depth: int = 1
    record: str = RECORD
    roles: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_config(cls, s: dict, base: Path | None = None) -> "Source":
        """root 가 상대 경로면 인스턴스 루트(base) 기준이다 — 어느 폴더에서 부르든 같은 곳을 가리키게."""
        root = Path(str(s["root"])).expanduser()
        if not root.is_absolute() and base is not None:
            root = Path(base) / root
        return cls(name=s.get("name") or "ws", root=root.resolve(),
                   depth=max(1, min(int(s.get("depth", 1)), MAX_LEVELS)), record=s.get("record") or RECORD,
                   roles={k: list(v) for k, v in (s.get("roles") or {}).items()})

    def object_id(self, wid: str) -> str:
        return f"{self.name}:{wid}"


def sources(cfg: dict, base: Path | None = None) -> list[Source]:
    """설정의 작업 공간 소스(root 가 있는 것만). base 는 인스턴스 루트 — 상대 root 의 기준."""
    return [Source.from_config(s, base) for s in cfg.get("sources", {}).get("workspace", []) if s.get("root")]


def _skip_dir(name: str) -> bool:
    return _hidden(name) or name in SKIP_DIRS


def _checkout(d: Path) -> bool:
    """저장소 사본(.git 폴더) 또는 워크트리(.git 파일)."""
    return (d / ".git").exists()


def _has_record_below(d: Path, record: str, levels: int) -> bool:
    if levels <= 0:
        return False
    try:
        entries = list(os.scandir(d))
    except OSError:
        return False
    for e in entries:
        if not e.is_dir(follow_symlinks=False) or _skip_dir(e.name):
            continue
        sub = Path(e.path)
        if (sub / record).is_file():
            return True
        if not _checkout(sub) and _has_record_below(sub, record, levels - 1):
            return True
    return False


def find_works(src: Source) -> list[tuple[Path, str]]:
    """작업 폴더 목록 [(폴더, 루트 기준 경로)].

    작업 = 작업 기록(WORK.md)이 있는 폴더(깊이 MAX_LEVELS 까지), 또는 깊이 `depth` 의 폴더(기록이 없어도 — '폴더 하나가 일 하나').
    `_`·`.` 로 시작하는 폴더와 기록 없는 저장소 사본은 작업이 아니다(기록이 있으면 워크트리여도 작업이다).
    안에 작업을 품은 폴더는 묶음이다. 작업 안은 더 내려가지 않는다. locate 와 같은 규칙이다.
    """
    out: list[tuple[Path, str]] = []

    def walk(d: Path, level: int) -> bool:
        found = False
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return False
        for e in entries:
            if not e.is_dir(follow_symlinks=False) or _skip_dir(e.name):
                continue
            sub = Path(e.path)
            rel = sub.relative_to(src.root).as_posix()
            if (sub / src.record).is_file():
                out.append((sub, rel))
                found = True
                continue
            if _checkout(sub):
                continue
            nested = walk(sub, level + 1) if level < MAX_LEVELS else False
            if nested:
                found = True
            elif level == src.depth:
                out.append((sub, rel))
                found = True
        return found

    if src.root.is_dir():
        walk(src.root, 1)
    return sorted(out, key=lambda x: x[1])


def locate(path: str | Path, srcs: list[Source]) -> tuple[Source, Path, str] | None:
    """경로가 든 작업 폴더 (소스, 폴더, 루트 기준 경로). 작업 공간 밖이면 None. find_works 와 같은 규칙:
    가장 바깥의 기록(WORK.md)을 가진 조상, 없으면 깊이 `depth` 의 조상(그것이 다른 작업을 품은 묶음이 아닐 때)."""
    try:
        target = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError, TypeError):
        return None
    for src in srcs:
        try:
            parts = target.relative_to(src.root).parts
        except ValueError:
            continue
        for i in range(1, min(len(parts), MAX_LEVELS) + 1):
            if _skip_dir(parts[i - 1]):
                break
            d = src.root.joinpath(*parts[:i])
            if not d.is_dir():
                break
            if (d / src.record).is_file():
                return src, d, Path(*parts[:i]).as_posix()
            if _checkout(d):
                break
        if len(parts) >= src.depth and not any(_skip_dir(x) for x in parts[:src.depth]):
            d = src.root.joinpath(*parts[:src.depth])
            ancestors = [src.root.joinpath(*parts[:j]) for j in range(1, src.depth + 1)]
            if d.is_dir() and not any(_checkout(a) for a in ancestors) \
                    and not _has_record_below(d, src.record, MAX_LEVELS - src.depth):
                return src, d, Path(*parts[:src.depth]).as_posix()
    return None


# ---------------------------------------------------------------- 작업 기록

class _Loader(yaml.SafeLoader):
    """front-matter 전용 — 날짜를 문자열로 둔다. 2026-02-30 같은 잘못된 날짜가 수집 전체를 멈추지 않게."""


_Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:timestamp"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()}
FRONT = re.compile(r"---[ \t]*\r?\n(.*?)^---[ \t]*\r?(?:\n|\Z)", re.S | re.M)


def split_front(text: str) -> tuple[dict, str, bool]:
    """(front-matter, 본문, 형식 오류). front-matter 가 없으면 ({}, 전문, False). 닫는 '---' 는 줄 맨 앞에 있어야 한다."""
    text = text.lstrip(BOM)
    if not text.startswith("---"):
        return {}, text, False
    m = FRONT.match(text)
    if not m:
        return {}, text, True
    try:
        front = yaml.load(m.group(1), Loader=_Loader)
    except Exception:                                  # 어떤 형식 오류도 기록 하나의 문제로 끝낸다
        return {}, text[m.end():], True
    if front is None:
        return {}, text[m.end():], False
    if not isinstance(front, dict):
        return {}, text[m.end():], True
    return front, text[m.end():], False


def _str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()[:10]
    return str(v).strip()


def _list(v) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [s for s in (_str(x) for x in v) if s]
    return [s for s in (x.strip() for x in _str(v).split(",")) if s]


@dataclass
class Record:
    front: dict
    title: str
    sections: dict[str, str]
    error: bool
    chars: int

    @property
    def status(self) -> str:
        return _str(self.front.get("status")).lower()

    def keys(self) -> list[str]:
        return _list(self.front.get("key")) + _list(self.front.get("keys"))


def _section_of(heading: str) -> str | None:
    """머리말 → 섹션. 별칭이 낱말로 맞아야 한다 — '결과물'·'Logs'·'Login 실패' 는 섹션이 아니다."""
    name = heading.strip().lower()
    for sec, aliases in SECTIONS.items():
        for a in aliases:
            if name == a or (name.startswith(a) and not re.match(r"[0-9a-z가-힣]", name[len(a)])):
                return sec
    return None


def read_body(path: Path) -> str:
    """front-matter 와 안내 주석을 뺀 본문."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return COMMENT.sub("", split_front(raw)[1])


def read_record(path: Path) -> Record | None:
    """작업 기록 하나. 없으면 None. 주석(<!-- -->)은 비어 있는 자리 안내라 내용으로 치지 않는다."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    front, body, error = split_front(raw)
    body = COMMENT.sub("", body)
    sections: dict[str, str] = {}
    heads = list(HEADING.finditer(body))
    for i, m in enumerate(heads):
        sec = _section_of(m.group(1))
        if sec:                                  # 같은 섹션이 여러 번(예: '결과' · '결과 (2차)')이면 이어 붙인다
            end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
            text = body[m.end():end].strip()
            if text:
                sections[sec] = f"{sections[sec]}\n{text}" if sec in sections else text
    h1 = H1.search(body)
    title = _str(front.get("title")) or (h1.group(1).strip() if h1 else "")
    return Record(front, title, sections, error, sum(len(v) for v in sections.values()))


def work_id(src: Source, work_dir: Path, rel: str, rec: Record | None = None) -> str:
    """작업의 정체: front-matter id, 없으면 루트 기준 경로. 객체 id 는 '<소스 이름>:<이것>'.
    같은 id 를 쓰는 폴더가 여럿이면(폴더를 복사해 쓴 경우) work_ids 가 가른다 — 정체가 필요한 곳은 그쪽을 쓴다."""
    rec = rec if rec is not None else read_record(work_dir / src.record)
    wid = _str((rec.front if rec else {}).get("id"))
    return wid or rel


def work_ids(src: Source, works: list[tuple[Path, str]] | None = None) -> dict[str, str]:
    """루트 기준 경로 → 작업 id. front-matter id 가 겹치면 그 이름의 폴더 하나만 id 를 쓰고 나머지는 경로를 쓴다
    (원래 폴더를 복사해 새 일을 시작한 경우 — 둘이 한 작업으로 합쳐지지 않게). 그런 폴더가 없거나 둘 이상이면 모두 경로."""
    works = find_works(src) if works is None else works
    raw = {rel: work_id(src, d, rel) for d, rel in works}
    counts: dict[str, int] = {}
    for wid in raw.values():
        counts[wid] = counts.get(wid, 0) + 1
    out = {}
    for rel, wid in raw.items():
        if counts[wid] == 1:
            out[rel] = wid
            continue
        keepers = [r for r, w in raw.items() if w == wid and Path(r).name == wid]
        out[rel] = wid if keepers == [rel] else rel
    return out


def resolve(src: Source, work_dir: Path, rel: str) -> str:
    """이 작업 폴더의 객체 id — 수집기·세션·맥락·명령이 같은 규칙으로 만든다."""
    return src.object_id(work_ids(src).get(rel) or work_id(src, work_dir, rel))


@dataclass
class Inventory:
    counts: dict[str, int]
    bytes: int
    files: list[tuple[str, int]]
    last_mtime: float
    sensitive: int
    repos_inside: int
    partial: bool
    decision_files: list[str]


ROLE_DIRS = ("plan", "plans", "spec", "specs", "design", "adr", "decisions", "evidence", "logs", "log", "repro",
             "dumps", "screenshots", "out", "output", "deliverables", ".claude")


def inventory(src: Source, work_dir: Path, max_files: int = 5000) -> Inventory:
    """작업 폴더의 파일을 역할로 센다 — 내용은 읽지 않는다. 버릴 것·생성물·저장소 사본·비밀값 파일은 관측하지 않는다.
    작업 폴더 자체가 저장소 체크아웃(워크트리)이면 맨 위 파일과 역할 폴더만 센다 — 코드 파일을 작업 파일로 세지 않게."""
    counts = {r: 0 for r in ROLE_NAMES}
    files: list[tuple[str, int]] = []
    total = sensitive = repos = 0
    last = 0.0
    partial = False
    decisions: list[str] = []
    role_of = {r: _compile(src.roles.get(r, d)) for r, d in ROLES.items()}
    checkout = _checkout(work_dir)
    repos += checkout
    for dirpath, dirnames, filenames in os.walk(work_dir):
        d = Path(dirpath)
        keep = []
        for name in sorted(dirnames):
            if name in SKIP_DIRS:
                continue
            if d == work_dir and checkout and name.lower() not in ROLE_DIRS:
                continue
            if (d / name / ".git").exists():
                repos += 1
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            full = d / name
            rel = full.relative_to(work_dir).as_posix()
            low = rel.lower()
            if _SCRATCH(low) or low == ".git":
                continue
            if low == GENERATED.lower() and _is_generated(full):
                continue
            if _SENSITIVE(name.lower()):
                sensitive += 1
                continue
            try:
                st = full.stat()
            except OSError:
                continue
            if len(files) >= max_files:
                partial = True
                break
            if low == src.record.lower():
                role = "record"
            else:
                role = next((r for r, f in role_of.items() if f(low)), "other")
            counts[role] += 1
            total += st.st_size
            last = max(last, st.st_mtime)
            files.append((rel, st.st_size))
            if role == "decision" and low.endswith(".md"):
                decisions.append(rel)
        if partial:
            break
    return Inventory(counts, total, sorted(files), last, sensitive, repos, partial, sorted(decisions))


def _is_generated(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            return f.read(len(GENERATED_MARK) + 8).lstrip(BOM).startswith(GENERATED_MARK)
    except OSError:
        return False


def generated_stamp(path: Path) -> str | None:
    """CONTEXT.md 를 만든 세대 — 첫 줄의 표시에서 읽는다. 사람의 파일이거나 없으면 None."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            head = f.readline().lstrip(BOM)
    except OSError:
        return None
    m = re.match(re.escape(GENERATED_MARK) + r".*?세대 (\S+)", head)
    return m.group(1) if m else None


# ---------------------------------------------------------------- front-matter 고치기 (close)

def update_front(text: str, updates: dict[str, str]) -> str:
    """front-matter 의 키 몇 개만 바꾼다. 다른 줄·주석·본문·줄마다의 줄바꿈(CRLF·LF)·BOM 은 그대로 둔다.
    같은 키가 여러 줄이면 모두 바꾼다(YAML 은 마지막 것을 읽는다). 닫는 '---' 는 줄 맨 앞의 것만 본다."""
    bom = BOM if text.startswith(BOM) else ""
    text = text[len(bom):]
    lines = text.splitlines(keepends=True)
    crlf = sum(1 for x in lines if x.endswith("\r\n"))
    nl = "\r\n" if crlf * 2 > len(lines) else "\n"

    def body(x: str) -> str:
        return x.rstrip("\r\n")

    close = None
    if lines and body(lines[0]).rstrip() == "---":
        close = next((i for i in range(1, len(lines)) if body(lines[i]).rstrip() == "---"), None)
    if close is None:
        head = "".join(f"{line}{nl}" for line in ["---", *(f"{k}: {v}" for k, v in updates.items()), "---"])
        return bom + head + text
    for key, value in updates.items():
        rx = re.compile(rf"^({re.escape(key)}:[ \t]*)([^#]*?)([ \t]+#.*)?$")
        found = False
        for i in range(1, close):
            m = rx.match(body(lines[i]))
            if m:
                lead = m.group(1) if m.group(1).endswith((" ", "\t")) else m.group(1) + " "
                lines[i] = f"{lead}{value}{m.group(3) or ''}{lines[i][len(body(lines[i])):]}"
                found = True
        if not found:
            lines.insert(close, f"{key}: {value}{nl}")
            close += 1
    return bom + "".join(lines)


# ---------------------------------------------------------------- 명령

def write_user_file(path: Path, text: str) -> None:
    """작업 폴더(사람의 파일)에 쓴다 — UTF-8 · LF, 권한은 그 사람의 기본값(umask)을 따른다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _cfg_and_paths(root: str | None):
    from . import config as configmod
    from .common import paths
    p = paths(root)
    return p, configmod.load(p.root)


def _pick_source(p, cfg: dict, name: str | None) -> Source:
    srcs = sources(cfg, p.root)
    if name:
        srcs = [s for s in srcs if s.name == name]
    if not srcs:
        raise SystemExit("작업 공간 소스가 없다 — atlas.toml 에 [[sources.workspace]] name·root 를 더한다 (atlas.example.toml)")
    return srcs[0]


def _template(p, name: str) -> str:
    for base in (p.root / "method" / "workspace", REPO / "method" / "workspace"):
        if (base / name).exists():
            return (base / name).read_text(encoding="utf-8")
    raise SystemExit(f"템플릿 method/workspace/{name} 이 없다")


def _here(p, path: str | None, cfg: dict) -> tuple[Source, Path, str]:
    loc = locate(path or Path.cwd(), sources(cfg, p.root))
    if not loc:
        raise SystemExit(f"{path or Path.cwd()} 는 설정된 작업 공간 안의 작업 폴더가 아니다")
    return loc


def context_md(p, cfg: dict, work_dir: Path) -> str:
    """CONTEXT.md 본문 — 첫 줄의 표시로 생성물임과 만든 세대를 알린다(수집기는 건너뛰고 — 요약을 다시 요약하지 않는다 —
    훅은 세대가 바뀌었으면 새 맥락을 넣는다). 이 작업 자신은 비슷한 과거 작업에서 뺀다."""
    from . import context
    ctx = context.build(p, cfg, work=str(work_dir))
    ctx.pop("work", None)                     # 이 폴더 안에서 읽는 파일이다 — 제 위치는 말하지 않는다
    head = (f"{GENERATED_MARK} 세대 {ctx.get('generation') or '-'} · {dt.date.today().isoformat()} — work-atlas 가 만든 맥락."
            " 고치지 않는다: `atlas work context` 로 다시 만든다. -->")
    return head + "\n" + context.render_md(ctx, int(cfg["context"]["max_lines"])) + "\n"


def cmd_init(p, cfg, args) -> int:
    src = _pick_source(p, cfg, args.source)
    src.root.mkdir(parents=True, exist_ok=True)
    target = src.root / "CLAUDE.md"
    body = _template(p, "root.CLAUDE.md")
    if target.exists():
        print(f"{target} 가 이미 있다 — 건드리지 않는다. 아래 규칙을 필요한 만큼 합친다:\n")
        print(body)
        return 0
    write_user_file(target, body)
    print(f"만들었다: {target}")
    print("다음: atlas work new <이슈 키 | 짧은-이름>  ·  훅(SessionStart 포함)은 atlas hook --print-settings")
    return 0


SLUG = re.compile(r"[^0-9A-Za-z가-힣._-]+")


def cmd_new(p, cfg, args) -> int:
    from . import config as configmod
    src = _pick_source(p, cfg, args.source)
    keys = configmod.Patterns(cfg).issue_keys(args.name)
    is_key = bool(keys) and keys[0] == args.name.strip()
    if not is_key and re.fullmatch(cfg["keys"]["issue"], args.name.strip()):
        print(f"참고: '{args.name.strip()}' 는 키 모양이지만 설정(keys.projects · exclude_prefixes)이 인정하지 않아 키 없는 일로 만든다")
    slug = SLUG.sub("-", (args.slug or "").strip()).strip("-.")
    if is_key:
        folder = f"{args.name.strip()}-{slug}" if slug else args.name.strip()
    else:
        name = SLUG.sub("-", args.name.strip()).strip("-.")
        if not name:
            raise SystemExit("이름이 비었다 — 이슈 키나 짧은-이름(영문·숫자·한글·-)을 준다")
        folder = f"{dt.date.today():%Y%m%d}-{name}"
    work_dir = src.root / folder
    if work_dir.exists():
        raise SystemExit(f"{work_dir} 가 이미 있다 — 덮어쓰지 않는다")
    title = args.title or ""
    if not title and is_key:
        from .context import Model
        meta = ((Model(p).text or {}).get("meta") or {}).get(f"wu:issue:{args.name.strip()}") or {}
        title = meta.get("title") or ""
    fill = {"{{id}}": json.dumps(folder, ensure_ascii=False), "{{key}}": args.name.strip() if is_key else "",
            "{{title}}": json.dumps(title, ensure_ascii=False), "{{today}}": dt.date.today().isoformat(),
            "{{heading}}": title or folder}
    record = _template(p, "WORK.md")
    for k, v in fill.items():
        record = record.replace(k, v)
    work_dir.mkdir(parents=True)
    write_user_file(work_dir / src.record, record)
    write_user_file(work_dir / "CLAUDE.md", _template(p, "work.CLAUDE.md"))
    write_user_file(work_dir / GENERATED, context_md(p, cfg, work_dir))
    print(f"만들었다: {work_dir}")
    print(f"  {src.record} — 일의 정본(문제부터 채운다) · CLAUDE.md — 기록과 맥락을 세션마다 불러온다 · {GENERATED} — 생성물")
    print(f"  시작: cd \"{work_dir}\" && claude   (코드 저장소는 --add-dir <경로>)")
    return 0


def cmd_context(p, cfg, args) -> int:
    src, work_dir, rel = _here(p, args.path, cfg)
    write_user_file(work_dir / GENERATED, context_md(p, cfg, work_dir))
    print(f"다시 만들었다: {work_dir / GENERATED}")
    return 0


def cmd_close(p, cfg, args) -> int:
    from . import config as configmod
    src, work_dir, rel = _here(p, args.path, cfg)
    path = work_dir / src.record
    status = "dropped" if args.dropped else "done"
    if not path.exists():
        raise SystemExit(f"{path} 가 없다 — 작업 기록이 없는 폴더다. atlas work new 로 만든 폴더에서 쓴다")
    try:
        raw = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise SystemExit(f"{path} 가 UTF-8 이 아니다 — 깨뜨리지 않으려고 고치지 않는다. UTF-8 로 저장한 뒤 다시 부른다")
    rec = read_record(path)
    updates = {"status": status}
    if not _str(rec.front.get("closed")):
        updates["closed"] = dt.date.today().isoformat()
    new = update_front(raw, updates)
    with path.open("w", encoding="utf-8", newline="") as f:      # 줄바꿈 형식을 그대로 둔다
        f.write(new)
    print(f"{rel}: status {status}" + (f" · closed {updates['closed']}" if "closed" in updates else ""))
    missing = [name for sec, name in (("result", "결과"), ("retro", "회고")) if sec not in rec.sections]
    if missing:
        why = "그만둔 이유 한 줄이면 된다" if args.dropped else "한 단락이면 된다"
        print(f"  [알림] {'·'.join(missing)}가 비어 있다 — {why}. 비슷한 일을 받을 때 맥락으로 돌아오는 곳이다.")
    keys = configmod.Patterns(cfg).issue_keys(*rec.keys()) or configmod.Patterns(cfg).issue_keys(work_dir.name)
    anchor = f"issue:{keys[0]}" if keys else f"work:{resolve(src, work_dir, rel)}"
    from .record import has_triage
    if has_triage(p, anchor):
        print(f"  트리아지 예측이 있다 — 판정을 남긴다: atlas record feedback --anchor {anchor} "
              "--archetype-ok yes|no|unsure --verdict helpful|not_helpful|unknown")
    return 0


def status_rows(p, cfg: dict, now: dt.datetime | None = None) -> list[dict]:
    """작업마다 상태와 위생 표시. 트래커 상태는 마지막 빌드의 작업단위에서 읽는다."""
    from . import config as configmod
    from .common import load_units
    now = now or dt.datetime.now().astimezone()
    stale = float(cfg["policy"].get("stale_work_days", 30))
    pat = configmod.Patterns(cfg)
    units = {u["wu_id"]: u for u in load_units(p)}
    rows = []
    for src in sources(cfg, p.root):
        works = find_works(src)
        ids = work_ids(src, works)
        for work_dir, rel in works:
            rec = read_record(work_dir / src.record)
            inv = inventory(src, work_dir)
            keys = pat.issue_keys(*(rec.keys() if rec else [])) or pat.issue_keys(work_dir.name)
            status = rec.status if rec else ""
            age = (now.timestamp() - inv.last_mtime) / 86400 if inv.last_mtime else None
            flags = []
            if not rec:
                flags.append("기록 없음")
            elif rec.error:
                flags.append("front-matter 오류")
            if rec and not status:
                flags.append("상태 없음")
            elif status and status not in STATUSES:
                flags.append(f"상태 '{status}'?")
            if status == "done" and "result" not in rec.sections:
                flags.append("결과 비어 있음")
            if status in OPEN_STATUSES and age is not None and age > stale:
                flags.append(f"{age:.0f}일 손대지 않음")
            for k in keys:
                u = units.get(f"wu:issue:{k}")
                if u and status in OPEN_STATUSES and u["span"].get("end_basis") == "issue" and u["span"].get("end") \
                        and not u["stats"].get("open_now"):
                    flags.append(f"{k} 는 트래커에서 끝남")
            if inv.sensitive:
                flags.append(f"비밀값으로 보이는 파일 {inv.sensitive}개")
            if ids[rel] != work_id(src, work_dir, rel, rec):
                flags.append(f"id '{work_id(src, work_dir, rel, rec)}' 를 다른 폴더도 쓴다 — 경로로 구분한다(복사했다면 id 를 고친다)")
            rows.append({"source": src.name, "rel": rel, "path": str(work_dir), "id": src.object_id(ids[rel]),
                         "keys": keys, "status": status or "-", "title": rec.title if rec else "",
                         "days_idle": round(age, 1) if age is not None else None, "flags": flags})
    return rows


def cmd_status(p, cfg, args) -> int:
    rows = status_rows(p, cfg)
    if not args.all:
        rows = [r for r in rows if r["status"] not in ("done", "dropped") or r["flags"]]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("표시할 작업이 없다" + ("" if args.all else " (끝난 작업까지: --all)"))
        return 0
    for r in rows:
        idle = f"{r['days_idle']:.0f}일 전" if r["days_idle"] is not None else "-"
        print(f"{r['status']:<8} {r['rel']:<40} {','.join(r['keys']) or '-':<12} {idle:>7}  {(r['title'] or '')[:40]}")
        for fl in r["flags"]:
            print(f"{'':<9}- {fl}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas work", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", help="work-atlas 인스턴스 (기본: ATLAS_HOME · 현재 폴더 위)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("init", parents=[common])
    a.add_argument("--source")
    a = sub.add_parser("new", parents=[common])
    a.add_argument("name", help="이슈 키(ABC-123) 또는 짧은-이름")
    a.add_argument("--title")
    a.add_argument("--slug", help="폴더 이름 뒤에 붙일 짧은 설명 (이슈 키일 때)")
    a.add_argument("--source")
    a = sub.add_parser("context", parents=[common])
    a.add_argument("path", nargs="?")
    a = sub.add_parser("close", parents=[common])
    a.add_argument("path", nargs="?")
    a.add_argument("--dropped", action="store_true", help="끝내지 않고 그만둔 일 (status: dropped)")
    a = sub.add_parser("status", parents=[common])
    a.add_argument("--all", action="store_true")
    a.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    p, cfg = _cfg_and_paths(args.root)
    return {"init": cmd_init, "new": cmd_new, "context": cmd_context, "close": cmd_close,
            "status": cmd_status}[args.cmd](p, cfg, args)


if __name__ == "__main__":
    sys.exit(main())
