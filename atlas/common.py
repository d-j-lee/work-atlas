"""엔진 공통 — 경로, 코드북, JSONL 입출력, 관측 검증.

도메인·소스에 의존하지 않는 것만 둔다. 소스별 코드는 atlas/collect/, 설정은 atlas/config.py.
모든 텍스트 출력은 UTF-8 · LF 로 고정한다 — Windows 에서도 해시가 파일 바이트와 같아야 한다.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import yaml

# Windows 에서 콘솔 없이(pythonw · 작업 스케줄러) 돌 때 자식 프로세스(git · claude)가 콘솔 창을 띄우지 않게 — 다른 OS 는 빈 값
NO_WINDOW: dict = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}


def rooted(path: str) -> bool:
    """절대 경로인가 — Windows 에서 드라이브 없이 / · \\ 로 시작하는 경로(Python 3.13 은 절대로 보지 않는다)도 절대로 본다."""
    return Path(path).is_absolute() or str(path).startswith(("/", "\\"))


def real_path(path) -> Path:
    """있는 데까지는 실제 경로로(심볼릭 링크 · Windows 8.3 짧은 이름 RUNNER~1 을 푼다), 없는 나머지는 그대로 붙인다."""
    p = Path(os.path.abspath(path))
    tail: list[str] = []
    while not p.exists() and p.parent != p:
        tail.append(p.name)
        p = p.parent
    try:
        p = p.resolve()
    except (OSError, RuntimeError):
        pass
    for name in reversed(tail):
        p = p / name
    return p


def relative_inside(path, root) -> str | None:
    """root 안의 파일이면 root 기준 POSIX 상대 경로, 밖이거나 root 자체면 None. Windows 는 대소문자 · 짧은 이름을 가리지 않는다."""
    try:
        rel = os.path.relpath(real_path(path), real_path(root))
    except ValueError:                                              # 다른 드라이브
        return None
    first = rel.replace("\\", "/").split("/")[0]
    if rel == os.curdir or first == os.pardir or os.path.isabs(rel):
        return None
    return Path(rel).as_posix()

REPO = Path(__file__).resolve().parents[1]
RESERVED = ("unknown", "residual")
HUMAN_SYSTEMS = ("atlas", "self_report")
TRACK_ORDER = {"A": 0, "B": 1}


@dataclass(frozen=True)
class Paths:
    """저장소 루트 기준 경로. 테스트·다른 저장소에서 부를 때는 root 를 바꾼다.

    작업단위 파일의 흐름: stitched(상관 결과, 주석 없음) → annotated(주석) → current(사후 검증 통과본).
    읽는 쪽(채점·유사 사례·카드·맥락·마이닝·카탈로그)은 모두 `load_units` 한 규칙을 쓴다: current, 없으면 stitched.
    주석 입력 문서(render)만 stitched 를 직접 읽는다.
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
        return self.root / "out" / "MANIFEST.json"

    @property
    def config(self) -> Path:
        return self.root / "atlas.toml"

    @property
    def state(self) -> Path:
        """수집 증분 상태·훅 중복 방지 기록. 지워도 다시 만들어진다(그 대신 한 번 전수 수집)."""
        return self.root / "data" / "state"

    @property
    def model(self) -> Path:
        """마이닝 산출(주장·색인·카탈로그). WU 단위 근거가 있어 git 밖에 둔다 — 원형 단위 집계만 out/ 로 간다."""
        return self.root / "data" / "model"

    @property
    def reports(self) -> Path:
        return self.root / "data" / "reports"


def paths(root: str | Path | None = None) -> Paths:
    """루트 결정 순서: 인자(--root) → 환경 변수 ATLAS_HOME → 현재 폴더와 그 위에서 method/codebook.yaml 이 있는 곳
    → 이 패키지가 든 저장소. 현재 폴더가 먼저다 — 인스턴스 폴더에서 실행하면 그 인스턴스를 쓴다."""
    if root:
        return Paths(Path(root).resolve())
    if os.environ.get("ATLAS_HOME"):
        return Paths(Path(os.environ["ATLAS_HOME"]).resolve())
    cwd = Path.cwd().resolve()
    for d in (cwd, *cwd.parents):
        if (d / "method" / "codebook.yaml").exists():
            return Paths(d)
    if (REPO / "method" / "codebook.yaml").exists():
        return Paths(REPO)
    raise SystemExit("work-atlas 인스턴스를 찾지 못했다 — 인스턴스 폴더에서 실행하거나 --root · ATLAS_HOME 을 지정한다")


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

try:
    import orjson as _orjson          # 선택 의존성: Rust 로 구현된 JSON 파서
except ImportError:                   # pragma: no cover
    _orjson = None


def _fast_loads(text):
    """JSON 을 읽는다. orjson 이 있으면 먼저 쓰고, orjson 이 거부하는 입력은 표준 json 으로 다시 읽는다.
    그래서 NaN·Infinity·1e400·짝 없는 서로게이트·1024단계 넘는 중첩·잘못된 입력의 오류는 표준 json 과 같다.
    남는 차이: 64비트를 넘는 정수를 orjson 은 오류 없이 float 로 읽는다(현재 데이터에는 그런 값이 없다)."""
    if _orjson is not None:
        try:
            return _orjson.loads(text)
        except _orjson.JSONDecodeError:
            pass
    return json.loads(text)


def read_jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield _fast_loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{n}: JSON 이 아니다 — {e}") from e


def units_path(p: "Paths") -> Path | None:
    """읽을 작업단위 파일: current(사후 검증 통과본), 없으면 stitched, 둘 다 없으면 None.
    `atlas stitch` 를 따로 돌리면 옛 current 를 지워 둘이 어긋나지 않게 한다(stitch.main)."""
    return next((x for x in (p.units, p.stitched) if x.exists()), None)


def load_units(p: "Paths") -> list[dict]:
    path = units_path(p)
    return list(read_jsonl(path)) if path else []


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
    for attempt in range(6):                  # Windows: 다른 프로세스(훅)가 읽는 중이면 잠깐 막힌다
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 5:
                os.unlink(tmp)
                raise
            time.sleep(0.2)


def write_jsonl(path: Path, records: Iterable[dict]) -> None:
    write_text(path, "".join(dumps(r) + "\n" for r in records))


# ---------------------------------------------------------------- 관측 검증

def observation_validator(p: Paths) -> "jsonschema.Draft202012Validator":
    import jsonschema  # 검증할 때만 불러온다(훅처럼 짧게 도는 명령의 기동 시간을 줄인다)
    schema = json.loads(p.observation_schema.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def check_observation(obs: dict, validator: "jsonschema.Draft202012Validator",
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


def glob_regex(pattern: str) -> re.Pattern:
    """경로 glob → 정규식. `*` 는 경로 한 단계, `**/` 는 0단계 이상, 끝의 `**` 는 그 아래 전부."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def path_filter(patterns: list[str]):
    """glob 목록에 걸리는 경로면 True 를 돌려주는 함수."""
    compiled = [glob_regex(p) for p in patterns]
    return lambda path: any(rx.match(path) for rx in compiled)


def is_unknown(value) -> bool:
    if isinstance(value, list):
        return not value or all(v == "unknown" for v in value)
    return value == "unknown"
