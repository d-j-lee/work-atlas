"""외부 수집기 연결점 — 관측 JSONL 을 검증해 L0 에 덧붙인다.

사내 메신저·메일·위키·배포·CI 처럼 엔진에 수집기가 없는 소스는, 어떤 언어로든 관측 계약
(method/observation.schema.json + method/schema.md)에 맞는 JSONL 을 내기만 하면 여기로 들어온다.

  python -m atlas ingest obs.jsonl [더.jsonl ...] [--excerpts ex.jsonl] [--dry-run]
  cat obs.jsonl | python -m atlas ingest -

- 한 줄이라도 계약을 어기면 그 줄은 쓰지 않고, 종료 코드 1 로 알린다(나머지 정상 줄은 쓴다).
- obs_id 가 이미 있으면 건너뛴다. 외부 수집기도 결정론 obs_id 를 만들어야 재실행이 안전하다.
- 발췌 파일 형식: {"obs_id": "...", "text": "..."} — 1차 방어(필요한 만큼만, 이름은 역할로, 비밀값 제거)는 외부 수집기 몫이다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .common import paths
from .store import append_observations, format_report


def _lines(path: str):
    stream = sys.stdin if path == "-" else Path(path).open(encoding="utf-8")
    try:
        for n, line in enumerate(stream, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    raise SystemExit(f"{path}:{n}: JSON 이 아니다 — {e}")
    finally:
        if stream is not sys.stdin:
            stream.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas ingest", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--excerpts")
    ap.add_argument("--root")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    p = paths(args.root)
    events = [e for f in args.files for e in _lines(f)]
    excerpts = {e["obs_id"]: e["text"] for e in _lines(args.excerpts)} if args.excerpts else None
    report = append_observations(p, events, excerpts, dry_run=args.dry_run)
    print(format_report("ingest", report, args.dry_run))
    return 1 if report["invalid"] else 0


if __name__ == "__main__":
    sys.exit(main())
