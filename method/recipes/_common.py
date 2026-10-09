"""레시피 공통 — 경로, 코드북, JSONL 입출력, 관측 검증.

도메인·도구에 의존하지 않는 것만 둔다. 수집기·상관처럼 사내 시스템에 묶인 레시피는 사내 인스턴스에서 만든다.
모든 텍스트 출력은 UTF-8 · LF 로 고정한다 — Windows 에서도 해시가 파일 바이트와 같아야 한다.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import jsonschema
import yaml

REPO = Path(__file__).resolve().parents[2]
RESERVED = ("unknown", "residual")
HUMAN_SYSTEMS = ("atlas", "self_report")
TRACK_ORDER = {"A": 0, "B": 1}


@dataclass(frozen=True)
class Paths:
    """저장소 루트 기준 경로. 테스트·다른 저장소에서 부를 때는 root 를 바꾼다.

    작업단위 파일의 흐름: stitched(상관 결과, 주석 없음) → annotated(주석) → current(사후 검증 통과본).
    채점·유사 사례·카드·사용 스킬은 current 만 읽는다.
    """

    root: Path

    @property
    def codebook(self) -> Path:
        return self.root / "method" / "codebook.yaml"

    @property
    def observation_schema(self) -> Path:
        return self.root / "method" / "observation.schema.json"

    @property
    def observations(self) -> Path:
        return self.root / "data" / "observations"

    @property
    def stitched(self) -> Path:
        return self.root / "data" / "units" / "stitched.jsonl"

    @property
    def annotated(self) -> Path:
        return self.root / "data" / "units" / "annotated.jsonl"

    @property
    def units(self) -> Path:
        return self.root / "data" / "units" / "current.jsonl"

    @property
    def excerpts(self) -> Path:
        return self.root / "data" / "excerpts.jsonl"

    @property
    def render(self) -> Path:
        return self.root / "data" / "render"

    @property
    def manifest(self) -> Path:
        return self.root / "build" / "MANIFEST.json"


def paths(root: str | Path | None = None) -> Paths:
    return Paths(Path(root).resolve() if root else REPO)


# ---------------------------------------------------------------- 코드북

class Codebook:
    """codebook.yaml 의 읽기 전용 보기. 활성(status: active) 항목만 유효한 id 로 본다."""

    def __init__(self, data: dict):
        self.data = data
        self.version = data["version"]
        self.facets = {f["id"]: f for f in data["facets"]}

    @classmethod
    def load(cls, p: Paths) -> "Codebook":
        return cls(yaml.safe_load(p.codebook.read_text(encoding="utf-8")))

    @staticmethod
    def _active(items: list[dict] | None) -> set[str]:
        return {i["id"] for i in (items or []) if i.get("status", "active") == "active"}

    def annotated_facets(self) -> list[dict]:
        """LLM 이 주석할 패싯: 현재 트랙에서 켜져 있고, 결정론 계산(computed) 대상이 아닌 것."""
        track = TRACK_ORDER[self.data.get("track", "B")]
        return [f for f in self.data["facets"]
                if not f.get("computed") and TRACK_ORDER[f.get("active_from", "A")] <= track]

    def facet_values(self, facet_id: str) -> set[str]:
        return self._active(self.facets[facet_id].get("values"))

    def archetypes(self) -> set[str]:
        return self._active(self.data.get("archetypes"))

    def vocabulary(self, name: str) -> set[str]:
        return self._active((self.data.get("vocabularies") or {}).get(name))

    def confidence(self) -> set[str]:
        return self._active(self.data.get("confidence_scale"))

    def fit(self) -> set[str]:
        return self._active(self.data.get("fit_scale"))

    def actor_roles(self) -> set[str]:
        return {"self", "bot", "unknown"} | self.facet_values("roles")


# ---------------------------------------------------------------- 파일 입출력

def read_jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{n}: JSON 이 아니다 — {e}") from e


def apply_corrections(observations: list[dict]) -> list[dict]:
    """`<system>.correction` 사건이 가리키는 관측(objects 의 type "obs")을 무효로 하고, 정정 사건 자체도 뺀다.

    L0 는 고치지 않는다. 잘못된 줄은 정정 사건으로 무효화하고, 필요하면 올바른 사건을 새로 덧붙인다.
    """
    voided = {o["id"] for c in observations if c.get("kind", "").endswith(".correction")
              for o in c.get("objects", []) if o.get("type") == "obs"}
    return [o for o in observations
            if o["obs_id"] not in voided and not o.get("kind", "").endswith(".correction")]


def read_observations(directory: Path) -> list[dict]:
    """월별 파일을 이름순으로 읽고 정정을 적용한다. 같은 obs_id 가 여러 번 있으면 처음 것을 쓴다(재수집 대비)."""
    seen: dict[str, dict] = {}
    if directory.exists():
        for path in sorted(directory.glob("*.jsonl")):
            for obs in read_jsonl(path):
                seen.setdefault(obs["obs_id"], obs)
    return apply_corrections(list(seen.values()))


def dumps(record: dict) -> str:
    """결정론 직렬화 — 같은 레코드는 언제나 같은 바이트."""
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def append_jsonl(path: Path, record: dict) -> None:
    """한 줄을 한 번의 write 로 덧붙인다(POSIX O_APPEND). 기존 줄은 건드리지 않는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (dumps(record) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def write_text(path: Path, text: str) -> None:
    """임시 파일에 다 쓴 뒤 바꿔 끼운다 — 중간에 실패해도 반쯤 쓴 파일이 남지 않는다. UTF-8 · LF 고정."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def write_jsonl(path: Path, records: Iterable[dict]) -> None:
    write_text(path, "".join(dumps(r) + "\n" for r in records))


# ---------------------------------------------------------------- 관측 검증

def observation_validator(p: Paths) -> jsonschema.Draft202012Validator:
    schema = json.loads(p.observation_schema.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def check_observation(obs: dict, validator: jsonschema.Draft202012Validator,
                      codebook: Codebook | None = None) -> list[str]:
    """스키마 위반 + 스키마로 표현하기 어려운 교차 규칙. 빈 목록이면 통과."""
    problems = [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in validator.iter_errors(obs)]
    if problems:
        return problems
    for field in ("ts", "collected_at"):
        try:
            parse_ts(obs[field])
        except ValueError as e:
            problems.append(f"{field} '{obs[field]}' 는 실제 시각이 아니다 — {e}")
    system = obs["source"]["system"]
    if obs["kind"].split(".", 1)[0] != system:
        problems.append(f"kind '{obs['kind']}' 의 접두가 source.system '{system}' 과 다르다")
    if system in HUMAN_SYSTEMS and obs["source"]["collector_kind"] != "human":
        problems.append(f"{system} 사건의 collector_kind 는 human 이어야 한다")
    if obs["kind"].endswith(".correction") and not any(o["type"] == "obs" for o in obs["objects"]):
        problems.append("정정 사건은 objects 에 {type: obs, id: <대상 obs_id>} 가 있어야 한다")
    if codebook is not None and obs["actor_role"] not in codebook.actor_roles():
        problems.append(f"actor_role '{obs['actor_role']}' 는 self·bot·unknown·코드북 roles 가 아니다")
    return problems


# ---------------------------------------------------------------- 기타

def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def parse_ts(ts: str) -> dt.datetime:
    """ISO 8601(시간대 포함) → aware datetime. 문자열 비교는 시간대가 섞이면 틀리므로 항상 이걸 쓴다."""
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def object_ref(obj: dict) -> str:
    """객체 참조의 표준 문자열 — 'issue:ABC-123'. 앵커·오버레이·채점이 같은 형식을 쓴다."""
    return f"{obj['type']}:{obj['id']}"


def is_unknown(value) -> bool:
    if isinstance(value, list):
        return not value or all(v == "unknown" for v in value)
    return value == "unknown"
