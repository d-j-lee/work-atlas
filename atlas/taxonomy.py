"""업무 카테고리 — 일을 어떤 단위로 묶어 볼지는 사람이 정하고(method/categories.yaml), 작업단위를 거기에 놓는 것은 계산이다.

카테고리는 두 축이다.
  영역(area)  트리. 값 안의 구분자(split)와 parents 로 상하위가 생긴다: lang.text.translate → lang › lang.text › lang.text.translate
  국면(facet) 영역마다 다시 나눠 보는 축. 기본은 유형(Bug · Improvement …) — 같은 영역이라도 흐름이 크게 다르다.
값을 읽는 원천(`from`, 앞에서부터 처음 값이 있는 것):
  트래커 라벨 이름(component · type · priority · label · fix_version …) · path(경로 규칙 paths) · work_kind · work_tags(작업 기록)
  · archetype(원형 주석 — 사후 검증을 통과한 것만)
정규화 순서: 원래 값 → merge(이름 통일·묶기) → 구분자 분해('*' 조각은 버린다) → 대소문자만 다른 값은 가장 흔한 표기로 → parents.
작업단위 하나가 여러 영역에 들 수 있다(컴포넌트가 둘이면 둘 다). 상위 영역은 하위를 합쳐 센다. 영역 값이 없으면 (미분류).

카테고리 키
  영역        그 노드의 값 (예: "lang.text", "server"). 값은 사슬 안에서 유일하다 — 상위가 값에 들어 있거나 parents 가 정한다
  전체        "(전체)"
  영역 × 국면 "<영역> / <값>" (첫 국면), "<영역> / <국면 id>:<값>" (둘째 국면부터)
사람이 붙이는 것(contacts · references)은 이 키에 달고, 상위 영역·전체의 것을 물려받는다. 사람 이름은 넣지 않는다(팀·채널·역할).
"""
from __future__ import annotations

import copy
import re
from collections import Counter
from dataclasses import dataclass, field

import yaml

from .common import path_filter

ALL = "(전체)"
UNCATEGORIZED = "(미분류)"
SEP = " / "
DEFAULT: dict = {
    "version": 1,
    "area": {"label": "영역", "from": ["component", "path", "work_tags"], "split": r"[./]"},
    "facets": [{"id": "kind", "label": "유형", "from": ["type", "work_kind"]}],
    "fold_case": True,
    "paths": {},
    "merge": {},
    "parents": {},
    "names": {},
    "contacts": {},
    "references": {},
}
HUMAN_FIELDS = ("paths", "merge", "parents", "names", "contacts", "references")
CONTACT_FIELDS = ("team", "channel", "role", "when", "note")
REFERENCE_FIELDS = ("title", "url", "path", "note")


TOP_KEYS = ("version", "area", "facets", "fold_case", *HUMAN_FIELDS)


def load_data(root) -> dict:
    data = copy.deepcopy(DEFAULT)
    f = root / "method" / "categories.yaml"
    if f.exists():
        loaded = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("method/categories.yaml 의 맨 위는 사전이어야 한다")
        for k, v in loaded.items():
            if v is not None:
                data[k] = v
    return data


def _strs(x) -> bool:
    return isinstance(x, list) and all(isinstance(v, str) and v for v in x)


def _scalar(v) -> bool:
    return isinstance(v, (str, int, float, bool))


def problems(data: dict) -> list[str]:
    """설정의 모양 검사 — 빌드 · 무결성 검사 · doctor 가 쓴다. 틀린 모양은 조용히 미분류가 되지 않고 여기서 멈춘다.
    접점 · 참조점은 정해진 칸만 받는다(사람 이름을 받을 칸이 없게)."""
    out = []
    for k in data:
        if k not in TOP_KEYS:
            out.append(f"모르는 맨 위 칸 '{k}' — 쓸 수 있는 칸: {', '.join(TOP_KEYS)}")
    specs = []
    area = data.get("area")
    if not isinstance(area, dict):
        out.append("area 는 {label, from: [...], split} 사전이어야 한다")
    else:
        specs.append(("area", area))
    facets = data.get("facets") or []
    if not isinstance(facets, list) or not all(isinstance(f, dict) for f in facets):
        out.append("facets 는 {id, label, from: [...]} 사전의 목록이어야 한다")
        facets = []
    ids = [f.get("id") for f in facets]
    if len(ids) != len(set(ids)) or not all(isinstance(i, str) and i for i in ids):
        out.append("facets 의 id 가 비었거나 겹친다")
    specs += [(f"facets.{f.get('id')}", f) for f in facets]
    for where, spec in specs:
        if not _strs(spec.get("from")):
            out.append(f"{where}.from 은 원천 이름의 목록이어야 한다 (예: [component, path])")
        if spec.get("split") is not None:
            if not isinstance(spec["split"], str):
                out.append(f"{where}.split 은 정규식 문자열이어야 한다")
            else:
                try:
                    re.compile(spec["split"])
                except re.error as e:
                    out.append(f"{where}.split 정규식 오류: {e}")
        if spec.get("merge") is not None and not (isinstance(spec["merge"], dict) and all(_scalar(v) for v in spec["merge"].values())):
            out.append(f"{where}.merge 는 '옛 값: 새 값' 사전이어야 한다")
    for name in ("paths", "merge", "parents", "names"):
        table = data.get(name) or {}
        if not isinstance(table, dict) or not all(_scalar(v) for v in table.values()):
            out.append(f"{name} 는 '값: 값' 사전이어야 한다")
    parents = {str(a): str(b) for a, b in (data.get("parents") or {}).items()} if isinstance(data.get("parents"), dict) else {}
    for start in sorted(parents):
        seen, cur = {start}, start
        while cur in parents:
            cur = parents[cur]
            if cur in seen:
                out.append(f"parents 에 순환이 있다 ({start} → … → {cur})")
                break
            seen.add(cur)
    for name, allowed in (("contacts", CONTACT_FIELDS), ("references", REFERENCE_FIELDS)):
        table = data.get(name) or {}
        if not isinstance(table, dict):
            out.append(f"{name} 는 '카테고리 키: [항목…]' 사전이어야 한다")
            continue
        for key, items in table.items():
            if not isinstance(items, list):
                out.append(f"{name}[{key}] 는 항목의 목록이어야 한다")
                continue
            for item in items:
                if not isinstance(item, dict):
                    out.append(f"{name}[{key}] 항목은 사전이어야 한다")
                    continue
                extra = sorted(set(item) - set(allowed))
                if extra:
                    out.append(f"{name}[{key}] 에 모르는 칸 {extra} — 쓸 수 있는 칸: {', '.join(allowed)}")
                if not all(_scalar(v) for v in item.values()):
                    out.append(f"{name}[{key}] 의 값은 글자여야 한다")
    return out


def human_entries(data: dict) -> list[str]:
    """사람이 채운 칸 — 공개 템플릿에서는 비어 있어야 한다(사내 업무 유래 내용). 국면의 merge 와 모르는 칸도 센다."""
    out = [f for f in HUMAN_FIELDS if data.get(f)]
    out += [f"facets.{f.get('id')}.merge" for f in data.get("facets") or [] if isinstance(f, dict) and f.get("merge")]
    return out + [k for k in data if k not in TOP_KEYS]


@dataclass
class WuFacts:
    """카테고리를 정하는 데 쓰는 작업단위의 사실 — 모두 L0·L1 에서 결정론으로."""
    labels: dict
    paths: list[str] = field(default_factory=list)
    work_labels: dict = field(default_factory=dict)
    archetype: str | None = None


@dataclass
class Placement:
    areas: list[list[str]]                 # 영역 사슬들 (위 → 아래)
    facets: dict[str, list[str]]           # 국면 id → 값들
    source: str                            # 영역 값을 정한 원천 (없으면 "")


class Taxonomy:
    def __init__(self, data: dict):
        self.data = data
        self.area = data.get("area") or DEFAULT["area"]
        self.facet_specs = list(data.get("facets") or [])
        self.path_rules = [(path_filter([glob]), str(value)) for glob, value in (data.get("paths") or {}).items()]
        self.merge = {str(a): str(b) for a, b in (data.get("merge") or {}).items()}
        self.fold = bool(data.get("fold_case", True))
        self.merge_low = {a.lower(): b for a, b in self.merge.items()} if self.fold else {}
        self.parents = {str(a): str(b) for a, b in (data.get("parents") or {}).items()}
        self.names = {str(a): str(b) for a, b in (data.get("names") or {}).items()}
        self.split = re.compile(self.area["split"]) if self.area.get("split") else None
        self.canon: dict[str, str] = {}

    @classmethod
    def load(cls, root) -> "Taxonomy":
        return cls(load_data(root))

    # ── 값 읽기 ──
    def _values(self, sources: list[str], f: WuFacts) -> tuple[list[str], str]:
        for src in sources:
            if src == "path":
                vals = list(dict.fromkeys(v for p in f.paths for rule, v in self.path_rules if rule(p)))
            elif src == "work_kind":
                vals = list(f.work_labels.get("kind", []))
            elif src == "work_tags":
                vals = list(f.work_labels.get("tags", []))
            elif src == "archetype":
                vals = [f.archetype] if f.archetype and f.archetype not in ("unknown", "residual") else []
            else:
                vals = list(f.labels.get(src, []))
            vals = list(dict.fromkeys(str(v).strip() for v in vals if str(v).strip()))
            if vals:
                return vals, src
        return [], ""

    def _split(self, value: str) -> list[str]:
        """merge(대소문자 접기를 켜면 대소문자 무시) → 구분자로 사슬. 조각의 앞뒤 공백은 버리고, 값 안의 ' / '(키 구분자)는 '/' 로."""
        value = self.merge.get(value, self.merge_low.get(value.lower(), value)).replace(SEP, "/").strip()
        if not self.split:
            return [value]
        chain, pos = [], 0
        for m in list(self.split.finditer(value)) + [None]:
            end = m.start() if m else len(value)
            piece = value[pos:end].strip()
            if piece and piece != "*":
                chain.append(value[:end].strip())
            if m:
                pos = m.end()
        return list(dict.fromkeys(chain)) or [value]

    def fit(self, facts: list[WuFacts]) -> "Taxonomy":
        """대소문자만 다른 영역 노드를 가장 흔한 표기로 모은다(동률이면 사전순). 데이터 전체를 한 번 본 뒤 정한다."""
        if not self.fold:
            return self
        counts: Counter = Counter()
        for f in facts:
            vals, _ = self._values(self.area.get("from", []), f)
            for v in vals:
                counts.update(self._split(v))
        groups: dict[str, list[tuple[int, str]]] = {}
        for v, n in counts.items():
            groups.setdefault(v.lower(), []).append((-n, v))
        self.canon = {v: sorted(g)[0][1] for g in groups.values() if len(g) > 1 for _, v in g}
        return self

    def chain(self, value: str) -> list[str]:
        chain = list(dict.fromkeys(self.canon.get(x, x) for x in self._split(value)))
        seen = set(chain)
        while chain[0] in self.parents and self.parents[chain[0]] not in seen:     # 순환은 끊는다
            chain.insert(0, self.parents[chain[0]])
            seen.add(chain[0])
        return chain

    def place(self, f: WuFacts) -> Placement:
        vals, src = self._values(self.area.get("from", []), f)
        areas = sorted({tuple(self.chain(v)) for v in vals}) if vals else [(UNCATEGORIZED,)]
        facets = {}
        for spec in self.facet_specs:
            fv, _ = self._values(spec.get("from", []), f)
            merge = {str(a): str(b) for a, b in (spec.get("merge") or {}).items()}
            facets[spec["id"]] = sorted({merge.get(v, v) for v in fv})
        return Placement([list(a) for a in areas], facets, src)

    # ── 키 · 표시 ──
    def facet_key(self, area_key: str, facet_id: str, value: str) -> str:
        first = self.facet_specs and self.facet_specs[0]["id"] == facet_id
        return f"{area_key}{SEP}{value}" if first else f"{area_key}{SEP}{facet_id}:{value}"

    def label(self, value: str, parent: str | None = None) -> str:
        """표시 이름 — names 가 있으면 그것, 상위 값으로 시작하면 그 뒤 조각(lang.text 아래의 lang.text.translate → translate)."""
        if value in self.names:
            return self.names[value]
        if parent and value.startswith(parent) and len(value) > len(parent):
            rest = value[len(parent):]
            if self.split and self.split.match(rest):
                return rest[self.split.match(rest).end():] or value
        return value

    def _inherited(self, name: str, keys: list[str]) -> list[dict]:
        table = self.data.get(name) or {}
        out, seen = [], set()
        for key in keys:
            for item in table.get(key) or []:
                if not isinstance(item, dict):
                    continue
                sig = tuple(sorted((k, str(v)) for k, v in item.items()))
                if sig in seen:
                    continue
                seen.add(sig)
                out.append({**{k: str(v) for k, v in item.items()}, "from": key})
        return out

    def lineage(self, key: str, chain: list[str], area: str | None = None) -> list[str]:
        """사람이 붙인 것을 물려받는 순서 — 자기 자신 → (칸이면) 상위 영역의 같은 칸 → 영역 사슬 아래에서 위로 → 전체.
        area: 이 키가 영역 × 국면 칸이면 그 영역 키 (키 모양으로 추측하지 않는다)."""
        keys = [key]
        if area and key.startswith(area + SEP):
            suffix = key[len(area) + len(SEP):]
            keys += [f"{a}{SEP}{suffix}" for a in reversed(chain[:-1])] + [f"{ALL}{SEP}{suffix}"]
        keys += [a for a in reversed(chain) if a != key] + [ALL]
        return list(dict.fromkeys(keys))

    def contacts(self, key: str, chain: list[str], area: str | None = None) -> list[dict]:
        return self._inherited("contacts", self.lineage(key, chain, area))

    def references(self, key: str, chain: list[str], area: str | None = None) -> list[dict]:
        return self._inherited("references", self.lineage(key, chain, area))
