"""텍스트 색인 — 새 일의 문장으로 과거의 비슷한 작업단위를 찾는다 (TF-IDF · 코사인, 결정론, 외부 의존 없음).

토큰: 영문 식별자는 소문자 통째 + camelCase·snake_case 조각(3자 이상), 한글은 음절 바이그램
(형태소 분석기 없이 한국어 검색에 흔히 쓰는 방식), 숫자·이슈 키·2자 미만은 버린다.
문서 = WU 의 이슈 요약·설명 발췌, 작업 기록(제목·문제·결과·회고·처리와 첫 결정 기록), 커밋 메시지, AI 세션 첫 프롬프트(발췌 캐시에 있는 만큼).
색인은 WU 단위 근거라 data/model/ 에만 둔다.
"""
from __future__ import annotations

import math
import re
from collections import Counter

RECIPE = "text@2"
WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*|[가-힣]+")
CAMEL = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+")
STOP = set("""a an and are as at be by can could do does for from has have how i if in into is it its may might
not of on or our should so that the their them then there these they this to use used using was we were what when
where which while will with would you your can't don't doesn't also any all some more most other such only same than
too very just get set new add added adds fix fixed fixes issue issues bug bugs patch please thanks code see test tests
java class method methods null true false return returns value values""".split())


ISSUE_KEY = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,9}-[0-9]+")


def tokens(text: str) -> list[str]:
    out = []
    for m in WORD.finditer(ISSUE_KEY.sub(" ", text or "")):     # 이슈 키(프로젝트 접두 포함)는 토큰에서 뺀다
        w = m.group()
        if "가" <= w[0] <= "힣":
            out.extend([w] if len(w) == 1 else [w[i:i + 2] for i in range(len(w) - 1)])
            continue
        low = w.lower()
        if len(low) >= 2 and low not in STOP:
            out.append(low)
        parts = [p.lower() for piece in w.split("_") for p in CAMEL.findall(piece)]
        if len(parts) > 1:
            out.extend(p for p in parts if len(p) >= 3 and p not in STOP and not p.isdigit())
    return out


class Index:
    def __init__(self, docs: dict[str, str]):
        """docs: id → 텍스트."""
        tf = {d: Counter(tokens(t)) for d, t in docs.items()}
        tf = {d: c for d, c in tf.items() if c}
        self.n = len(tf)
        df = Counter(term for c in tf.values() for term in c)
        self.idf = {t: math.log((1 + self.n) / (1 + k)) + 1 for t, k in df.items()}
        self.vecs = {d: self._vec(c) for d, c in tf.items()}
        self.postings: dict[str, list[str]] = {}
        for d, v in self.vecs.items():
            for t in v:
                self.postings.setdefault(t, []).append(d)

    @classmethod
    def load(cls, idf: dict[str, float], vecs: dict[str, dict[str, float]], n: int) -> "Index":
        """저장된 색인(text_index.json)에서 다시 만든다 — 문서를 다시 토큰화하지 않는다."""
        self = cls.__new__(cls)
        self.idf, self.vecs, self.n = idf, vecs, n
        self.postings = {}
        for d, v in vecs.items():
            for t in v:
                self.postings.setdefault(t, []).append(d)
        return self

    def _vec(self, counts: Counter) -> dict[str, float]:
        v = {t: (1 + math.log(k)) * self.idf.get(t, 0.0) for t, k in counts.items() if t in self.idf}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}

    def query(self, text: str, k: int = 10, exclude: set[str] | None = None) -> list[tuple[str, float]]:
        q = self._vec(Counter(tokens(text)))
        scores: dict[str, float] = {}
        for t, w in q.items():
            for d in self.postings.get(t, ()):
                scores[d] = scores.get(d, 0.0) + w * self.vecs[d][t]
        if exclude:
            for d in exclude:
                scores.pop(d, None)
        return sorted(((d, round(s, 4)) for d, s in scores.items() if s > 0), key=lambda x: (-x[1], x[0]))[:k]

    def top_terms(self, doc: str, k: int = 8) -> list[str]:
        return [t for t, _ in sorted(self.vecs.get(doc, {}).items(), key=lambda x: (-x[1], x[0]))[:k]]


ORDER = {"issue.created": 0, "workspace.snapshot": 1, "vcs.commit": 2, "vcs.merge": 2, "ai_session.session": 2}


def wu_texts(units: list[dict], obs_by_id: dict, excerpts: dict[str, str], chars: int = 1500) -> dict[str, str]:
    """WU → 색인할 텍스트. 이슈 생성 발췌 → 작업 기록 → 커밋 메시지·세션 프롬프트 순(앞부분이 상한 안에 남는다)."""
    out = {}
    for u in units:
        parts = []
        for m in u.get("members", []):
            o = obs_by_id.get(m["obs_id"])
            if not o or m.get("link") != "key":
                continue
            if o["kind"] in ORDER:
                t = excerpts.get(o["obs_id"])
                if t:
                    parts.append((ORDER[o["kind"]], o["ts"], t))
        if parts:
            parts.sort()
            out[u["wu_id"]] = "\n".join(t for _, _, t in parts)[:chars]
    return out
