"""사내 수집기 골격 — 내보낸 JSON(메신저 · 위키 · 메일 · 리뷰 …)을 관측으로 바꿔 atlas ingest 로 넣는다.

복사해서 `fetch()` 와 `to_events()` 만 그 소스에 맞게 고친다. 나머지 규칙(행위자는 역할로 · 키는 결정론으로 · 본문은 발췌로만 ·
obs_id 는 결정론)은 atlas.collect.sdk.Kit 이 지킨다. 사건 종류와 키의 뜻은 method/schema.md "사내 수집기가 내는 사건".

  python method/recipes/collector_template.py --root . --input export.json --out data/inbox/chat
  python -m atlas ingest data/inbox/chat.obs.jsonl --excerpts data/inbox/chat.excerpts.jsonl --dry-run   # 계약 · 연결 미리보기
  python -m atlas ingest data/inbox/chat.obs.jsonl --excerpts data/inbox/chat.excerpts.jsonl

입력 예(내보내기 한 줄): {"id": "m-1", "channel": "pay-dev", "thread": "t-9", "time": "2026-10-01T10:00:00+09:00",
                          "author": "kim@corp.example", "text": "PAY-12 재현됨, 롤백 검토", "url": "…"}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))       # 인스턴스 루트(엔진) — pip install -e . 했으면 필요 없다
from atlas.collect.sdk import Kit  # noqa: E402

SYSTEM, INSTANCE, COLLECTOR = "chat", "사내-메신저", "chat-export@1"   # 소스에 맞게 — system 은 method/schema.md 의 목록에서


def fetch(path: Path):
    """소스에서 항목을 읽는다. 여기서는 내보낸 JSON 줄 — API 를 부르려면 이 함수만 바꾼다(읽기만, 증분이면 마지막 시각을 기억)."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def to_events(kit: Kit, item: dict) -> None:
    """항목 하나 → 관측. 본문은 text 로 넘기면 발췌 캐시로만 간다(관측에는 키만)."""
    kit.event(f"{SYSTEM}.message", obs_id=kit.obs_id(item["channel"], item["id"]), ts=item["time"], who=[item.get("author", "")],
              objects=[{"type": "thread", "id": f"{item['channel']}/{item.get('thread') or item['id']}"}],
              metrics={"chars": len(item.get("text") or "")}, text=item.get("text"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True, help="출력 접두 (예: data/inbox/chat → .obs.jsonl · .excerpts.jsonl)")
    args = ap.parse_args(argv)
    kit = Kit(args.root, SYSTEM, INSTANCE, COLLECTOR)
    for item in fetch(Path(args.input)):
        to_events(kit, item)
    obs, ex = kit.write(args.out)
    print(f"관측 {len(kit.events)}줄 → {obs}\n발췌 {len(kit.excerpts)}줄 → {ex}\n"
          f"다음: python -m atlas ingest {obs} --excerpts {ex} --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
