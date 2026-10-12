#!/usr/bin/env python3
"""커밋 전 무결성 검사 — 규칙을 '기억'이 아니라 '실행'에 심는다.

검사 항목 (하나라도 어기면 종료 코드 1):
  1. annotation.schema.json 이 codebook.yaml 에서 생성한 결과와 같은가 (손 편집·생성 누락 탐지)
  2. out/ 의 파일이 out/MANIFEST.json 의 해시와 같은가 (생성물 손 편집 탐지)
  3. overlays/*.yaml 항목에 target·op·reason·expires 가 있는가 (만료된 항목은 경고만)
  4. data/ 가 git 에 추적되고 있지 않은가 (관측·WU 단위 데이터는 git 밖 — 보존 정책 집행을 위해)
  5. 루트에 .template 이 있으면(공개 템플릿 저장소) 사내 인스턴스 유래 내용이 없는가
     — 추적 파일이 허용 목록 안에 있는가(허용 목록 방식: 새 파일은 의식적으로 추가해야 한다),
       코드북의 원형·어휘·예시·도출 값이 비어 있는가, policy.md 가 빈칸 상태인가

사용: python -m atlas check [--root …]      (.githooks/pre-commit 과 CI 가 호출)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

from . import schema_gen as gen_annotation_schema
from .common import REPO, glob_regex, paths

ROOT = REPO          # main() 이 --root · ATLAS_HOME · 현재 폴더로 다시 정한다

errors: list[str] = []
warnings: list[str] = []


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def check_schema() -> None:
    if not gen_annotation_schema.check(ROOT):
        errors.append("annotation.schema.json 이 codebook.yaml 과 어긋난다 → python -m atlas gen-schema 를 다시 돌려라")


def check_build() -> None:
    build = ROOT / "out"
    if not build.exists():
        return
    manifest_path = build / "MANIFEST.json"
    files = [p for p in build.rglob("*") if p.is_file() and p != manifest_path]
    if not manifest_path.exists():
        if files:
            errors.append("out/ 에 파일이 있는데 MANIFEST.json 이 없다 → 빌드 레시피로 다시 만들어라")
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    listed = manifest.get("files", {})
    for rel, digest in listed.items():
        p = ROOT / rel
        if not p.exists():
            errors.append(f"MANIFEST 에 있는 {rel} 이 없다")
        elif sha256(p) != digest:
            errors.append(f"{rel} 이 MANIFEST 해시와 다르다 → 손 편집으로 보인다. out/ 는 레시피만 쓴다")
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        if rel not in listed:
            errors.append(f"{rel} 이 MANIFEST 에 없다 → 레시피 밖에서 생긴 파일")


def check_overlays() -> None:
    today = dt.date.today()
    for path in sorted((ROOT / "overlays").glob("*.yaml")):
        items = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        for i, item in enumerate(items):
            where = f"{path.relative_to(ROOT).as_posix()}[{i}]"
            missing = [k for k in ("target", "op", "reason", "expires") if not item.get(k)]
            if missing:
                errors.append(f"{where}: 필수 필드 없음 {missing}")
                continue
            expires = item["expires"]
            if isinstance(expires, str):
                expires = dt.date.fromisoformat(expires)
            if expires < today:
                warnings.append(f"{where}: {expires} 에 만료 — 다음 재빌드 전에 유지·삭제를 정하라")


def check_data_untracked() -> None:
    try:
        out = subprocess.run(["git", "ls-files", "data"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return
    if out.strip():
        errors.append("data/ 가 git 에 추적되고 있다 → git rm --cached -r data 후 .gitignore 확인")


# 공개 템플릿에 존재해도 되는 추적 파일의 패턴. `*` 는 경로 한 단계, `**` 는 여러 단계.
# 일반화된 파일을 새 위치에 넣을 때만 여기에 추가한다 — 사내 파일(atlas.toml, method/sources.yaml 등)은 일부러 없다.
TEMPLATE_ALLOWED = [
    ".claude/settings.json", ".gitattributes", ".githooks/pre-commit", ".github/workflows/*.yml",
    ".gitignore", ".template", "LICENSE", "requirements.txt", "pyproject.toml", "atlas.example.toml",
    "README.md", "CONCEPTS.md", "METHODOLOGY.md", "ARCHITECTURE.md", "BOOTSTRAP.md", "CLAUDE.md", "CHANGELOG.md", "EVALUATION.md",
    "method/annotation.schema.json", "method/observation.schema.json", "method/codebook.yaml",
    "method/policy.md", "method/schema.md", "method/prompts/*.md", "method/skill/atlas/SKILL.md",
    "atlas/*.py", "atlas/**/*.py", "tests/*.py",
]


_ALLOWED = [glob_regex(p) for p in TEMPLATE_ALLOWED]


def template_allows(rel: str) -> bool:
    return any(rx.match(rel) for rx in _ALLOWED)


def check_example_config(leak: str) -> None:
    """atlas.example.toml 은 허용 목록 파일이지만 내용에 사내 값(주소·식별자·프로젝트 키)이 들어갈 수 있다 — 자리표시자만 허용한다."""
    import tomllib
    path = ROOT / "atlas.example.toml"
    if not path.exists():
        return
    cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    bad = []
    for entry in cfg.get("identity", {}).get("self", []):
        if entry not in ("me",) and not str(entry).endswith(".example"):
            bad.append(f"identity.self={entry}")
    for src in cfg.get("sources", {}).get("jira", []):
        host = re.sub(r"^https?://", "", str(src.get("base_url", ""))).split("/")[0]
        if host and not host.endswith(".example"):
            bad.append(f"jira.base_url={host}")
    for src in cfg.get("sources", {}).get("git", []):
        if not str(src.get("path", "")).startswith("/path/to/"):
            bad.append(f"git.path={src.get('path')}")
    if cfg.get("keys", {}).get("projects"):
        bad.append("keys.projects 가 채워져 있다")
    if bad:
        errors.append(f"atlas.example.toml 에 자리표시자가 아닌 값: {bad}. {leak}")


def check_template() -> None:
    """공개 템플릿에 사내 업무에서 유래한 내용이 섞이지 않게 한다. 흐름은 템플릿 → 인스턴스 한 방향이다."""
    if not (ROOT / ".template").exists():
        return
    cb = yaml.safe_load((ROOT / "method" / "codebook.yaml").read_text(encoding="utf-8"))
    leak = "템플릿에 사내 인스턴스 유래 내용이 있다 → 공개 저장소에 올리지 않는다"
    if cb.get("archetypes"):
        errors.append(f"codebook.archetypes 가 비어 있지 않다. {leak}")
    for name, items in (cb.get("vocabularies") or {}).items():
        if items:
            errors.append(f"codebook.vocabularies.{name} 가 비어 있지 않다. {leak}")
    for facet in cb.get("facets", []):
        if facet.get("derive_from") and facet.get("values"):
            errors.append(f"codebook 패싯 {facet['id']} 의 도출 값이 채워져 있다. {leak}")
        for value in facet.get("values", []):
            if value.get("examples") or value.get("boundary"):
                errors.append(f"codebook {facet['id']}.{value['id']} 에 예시·경계 사례가 있다. {leak}")
    check_example_config(leak)
    policy = (ROOT / "method" / "policy.md").read_text(encoding="utf-8")
    if "policy_version: 0" not in policy or "______" not in policy:
        errors.append(f"policy.md 가 빈칸(______)·policy_version 0 상태가 아니다 — 사내 값이 채워진 것으로 보인다. {leak}")
    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        warnings.append("git 을 쓸 수 없어 템플릿 허용 목록 검사를 건너뛴다")
        return
    for rel in sorted(r for r in set(tracked) if not template_allows(r)):
        errors.append(f"{rel} 는 템플릿 허용 목록에 없다. 일반화된 파일이면 atlas/check.py 의 TEMPLATE_ALLOWED 에 추가하고, 아니면 빼라")


def main(argv: list[str] | None = None) -> int:
    global ROOT
    argv = sys.argv[1:] if argv is None else argv
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    ROOT = paths(argv[argv.index("--root") + 1] if "--root" in argv else None).root
    errors.clear()
    warnings.clear()
    check_template()
    check_schema()
    check_build()
    check_overlays()
    check_data_untracked()
    for w in warnings:
        print(f"[경고] {w}", file=sys.stderr)
    for e in errors:
        print(f"[실패] {e}", file=sys.stderr)
    if errors:
        return 1
    print("integrity ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
