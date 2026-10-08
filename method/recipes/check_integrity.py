#!/usr/bin/env python3
"""커밋 전 무결성 검사 — 규칙을 '기억'이 아니라 '실행'에 심는다.

검사 항목 (하나라도 어기면 종료 코드 1):
  1. annotation.schema.json 이 codebook.yaml 에서 생성한 결과와 같은가 (손 편집·생성 누락 탐지)
  2. build/ 의 파일이 build/MANIFEST.json 의 해시와 같은가 (생성물 손 편집 탐지)
  3. overlays/*.yaml 항목에 target·op·reason·expires 가 있는가 (만료된 항목은 경고만)
  4. data/ 가 git 에 추적되고 있지 않은가 (관측·WU 단위 데이터는 git 밖 — 보존 정책 집행을 위해)

사용: python3 method/recipes/check_integrity.py      (.githooks/pre-commit 이 호출)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_annotation_schema  # noqa: E402

errors: list[str] = []
warnings: list[str] = []


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def check_schema() -> None:
    if not gen_annotation_schema.check():
        errors.append("annotation.schema.json 이 codebook.yaml 과 어긋난다 → gen_annotation_schema.py 를 다시 돌려라")


def check_build() -> None:
    build = ROOT / "build"
    if not build.exists():
        return
    manifest_path = build / "MANIFEST.json"
    files = [p for p in build.rglob("*") if p.is_file() and p != manifest_path]
    if not manifest_path.exists():
        if files:
            errors.append("build/ 에 파일이 있는데 MANIFEST.json 이 없다 → 빌드 레시피로 다시 만들어라")
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    listed = manifest.get("files", {})
    for rel, digest in listed.items():
        p = ROOT / rel
        if not p.exists():
            errors.append(f"MANIFEST 에 있는 {rel} 이 없다")
        elif sha256(p) != digest:
            errors.append(f"{rel} 이 MANIFEST 해시와 다르다 → 손 편집으로 보인다. build/ 는 레시피만 쓴다")
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


def main() -> int:
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
