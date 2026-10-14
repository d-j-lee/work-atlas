"""사내 수집기 도구 — 엔진 밖에서 만드는 수집기(메신저 · 위키 · 메일 · 배포 · 리뷰 …)가 관측 계약을 지키게 돕는 작은 함수들.

  from atlas.collect.sdk import Kit
  kit = Kit("/path/to/instance", system="chat", instance="사내-메신저", collector="chat-export@1")
  ev = kit.event("chat.message", obs_id=kit.obs_id("msg", channel, msg_id), ts=iso, who=[author_email],
                 objects=[{"type": "thread", "id": f"{channel}/{thread_id}"}], text=body)
  kit.write("out/chat")            # → out/chat.obs.jsonl · out/chat.excerpts.jsonl → atlas ingest … --excerpts …

- 행위자: 식별자(이메일·계정)는 판정에만 쓰고 남기지 않는다 → self · bot · 설정한 역할 · unknown.
- 키: 본문·제목에서 이슈 키를 결정론으로 뽑는다(atlas.toml [keys] 와 같은 규칙). 경로·MR 번호는 수집기가 아는 것만 넣는다.
- 본문: 관측에는 넣지 않고 발췌 캐시로만 — 길이를 자르고 비밀값·연락처·홈 경로를 가린다(안전망이다. 1차 방어는 필요한 만큼만).
- 그릇 이름(채널 · DM · 메일 스레드 · 페이지 경로)은 사람 이름을 품기 쉽다 → obs_id 와 objects 의 id 는 해시로 남긴다.
  같은 값은 늘 같은 해시라 묶기(스레드 단위)는 그대로 된다. 엔진이 다른 소스와 잇는 객체(issue · commit · mr · obs ·
  ai_session · work)는 그대로 둔다.
  기본 해시는 인스턴스 이름으로 소금을 친다 — 눈으로 읽히지 않게 할 뿐, 후보 이름을 넣어 보면 되짚을 수 있다(인스턴스 이름이
  관측에 그대로 있다). 되짚지 못하게 하려면 환경 변수 ATLAS_HASH_KEY 에 비밀 값을 두면 HMAC 이 된다. 한 번 정하면 바꾸지
  않는다 — 바꾸면 모든 id 가 바뀌어 다시 수집할 때 같은 사건이 두 번 들어온다.
- obs_id 는 결정론이어야 한다(같은 사건 = 같은 id) — 다시 돌려도 중복이 생기지 않는다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

from .. import config as configmod
from ..common import now_iso
from . import excerpt


LINK_TYPES = {"issue", "commit", "mr", "obs", "ai_session", "work"}   # 다른 소스와 잇는 객체 — 해시하지 않는다
HASH_KEY_ENV = "ATLAS_HASH_KEY"


class Kit:
    def __init__(self, root: str | Path, system: str, instance: str, collector: str, excerpt_chars: int = 600):
        self.cfg = configmod.load(Path(root))
        self.patterns = configmod.Patterns(self.cfg)
        self.system, self.instance, self.collector = system, instance, collector
        self.limit = excerpt_chars
        self.collected_at = now_iso()
        self.events: list[dict] = []
        self.excerpts: dict[str, str] = {}

    def ref(self, *parts) -> str:
        """그릇 이름 → 해시 16자(ATLAS_HASH_KEY 가 있으면 HMAC). 같은 값은 늘 같은 해시."""
        raw = "\x1f".join([self.instance, *(str(x) for x in parts)]).encode("utf-8")
        key = os.environ.get(HASH_KEY_ENV, "")
        if key:
            return hmac.new(key.encode("utf-8"), raw, hashlib.sha256).hexdigest()[:16]
        return hashlib.sha256(raw).hexdigest()[:16]

    def obs_id(self, *parts) -> str:
        """<system>:<instance 해시 6자>:<부분들의 해시 16자> — 채널·스레드 이름이 id 에 드러나지 않는다."""
        inst = hashlib.sha256(self.instance.encode("utf-8")).hexdigest()[:6]
        return ":".join([self.system, inst, self.ref(*parts)])

    def _object(self, o: dict) -> dict:
        t = str(o.get("type") or "")
        if t in LINK_TYPES:
            return dict(o)
        return {"type": t, "id": self.ref(t, o.get("id"))}          # url 같은 덧붙임도 그릇 이름을 품을 수 있어 뺀다

    def event(self, kind: str, *, obs_id: str, ts: str, who: list[str] | None = None, objects: list[dict] | None = None,
              keys: dict | None = None, labels: dict | None = None, metrics: dict | None = None, text: str | None = None,
              partial: bool = False) -> dict:
        if not kind.startswith(self.system + "."):
            raise ValueError(f"kind 는 '{self.system}.' 로 시작해야 한다: {kind}")
        k = {"issue_keys": [], **(keys or {})}
        if text:
            k["issue_keys"] = sorted(set(k["issue_keys"]) | set(self.patterns.issue_keys(text)))
        ev = {"obs_id": obs_id, "source": {"system": self.system, "instance": self.instance, "collector_kind": "api"},
              "kind": kind, "ts": ts, "actor_role": self.patterns.role_of(*(who or [])),
              "objects": [self._object(o) for o in objects or []],
              "keys": k, "labels": labels or {}, "metrics": metrics or {}, "partial": partial,
              "collector": self.collector, "collected_at": self.collected_at}
        self.events.append(ev)
        if text and self.limit:
            self.excerpts[obs_id] = excerpt(text, self.limit)
        return ev

    def write(self, prefix: str | Path) -> tuple[Path, Path]:
        prefix = Path(prefix)
        prefix.parent.mkdir(parents=True, exist_ok=True)
        obs, ex = Path(f"{prefix}.obs.jsonl"), Path(f"{prefix}.excerpts.jsonl")
        with obs.open("w", encoding="utf-8", newline="\n") as f:
            for e in self.events:
                f.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
        with ex.open("w", encoding="utf-8", newline="\n") as f:
            for i, t in self.excerpts.items():
                if t:
                    f.write(json.dumps({"obs_id": i, "text": t}, ensure_ascii=False) + "\n")
        return obs, ex
