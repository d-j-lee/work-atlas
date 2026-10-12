"""동반 변경 — "이 파일을 바꿀 때 함께 바뀌어 온 파일" (Zimmermann et al. 2004/2005, ROSE 의 탐색 시나리오).

거래(transaction) = 커밋 하나의 파일 집합 (level="commit", 기본) 또는 작업단위 하나의 파일 합집합 (level="wu").
규칙 a → b:  support = a·b 가 함께 바뀐 거래 수, confidence = support / a 가 바뀐 거래 수, lift = confidence / P(b).
- 파일이 max_files_per_commit 보다 많은 커밋은 뺀다(대량 변경·머지 — Zimmermann 2004 는 30 개체 초과를 무시).
- 장부 파일은 뺀다(corpus). 이름 바뀐 파일은 최신 이름으로 이어진다.
- 강도: high = support ≥ 10 · confidence ≥ 0.6 / medium = support ≥ 5 · confidence ≥ 0.4 / low = 문턱(min_support·min_confidence)만 넘음.
- 앞쪽(antecedent)마다 상위 top_k 규칙만 남긴다 — 추천은 상위 몇 개만 쓰인다.
"""
from __future__ import annotations

from collections import defaultdict

from . import claims as C

RECIPE = "cochange@1"


def transactions(corpus, level: str = "commit") -> list[tuple]:
    """(시각, 범위, 파일 집합, 근거 id) 목록. 시각순."""
    max_files = int(corpus.cfg["mining"]["max_files_per_commit"])
    if level == "commit":
        return [(c.ts, c.scope, frozenset(c.paths), c.obs_id) for c in corpus.commits
                if not c.large and 2 <= len(c.paths) <= max_files]
    by_wu: dict[str, list] = defaultdict(list)
    for c in corpus.commits:
        if c.large:
            continue
        for w in c.wus:
            by_wu[w].append(c)
    out = []
    for w, cs in by_wu.items():
        files = frozenset(p for c in cs for p in c.paths)
        if 2 <= len(files) <= max_files * 2:
            out.append((max(c.ts for c in cs), cs[0].scope, files, w))
    out.sort(key=lambda t: (t[0], t[3]))
    return out


class Rules:
    """증분으로 셀 수 있는 규칙 표 — 평가에서 거래를 하나씩 더하며 쓴다."""

    def __init__(self):
        self.single: dict[tuple, int] = defaultdict(int)
        self.pair: dict[tuple, list] = {}
        self.n: dict[str, int] = defaultdict(int)
        self.adj: dict[tuple, set] = defaultdict(set)

    def add(self, ts, scope: str, files: frozenset, eid: str) -> None:
        self.n[scope] += 1
        for a in files:
            self.single[(scope, a)] += 1
        fl = sorted(files)
        for i, a in enumerate(fl):
            for b in fl[i + 1:]:
                rec = self.pair.get((scope, a, b))
                if rec is None:
                    self.pair[(scope, a, b)] = [1, ts, ts, [eid]]
                    self.adj[(scope, a)].add(b)
                    self.adj[(scope, b)].add(a)
                else:
                    rec[0] += 1
                    rec[1] = min(rec[1], ts)
                    rec[2] = max(rec[2], ts)
                    rec[3].append(eid)
                    if len(rec[3]) > 5:
                        del rec[3][0]

    def recommend(self, scope: str, a: str, min_support: int, min_conf: float, k: int = 10) -> list[tuple]:
        """a 하나에 대한 추천 [(b, support, confidence)] — 신뢰도·지지도 순. 평가에서 거래마다 부른다."""
        n_a = self.single.get((scope, a), 0)
        if not n_a:
            return []
        out = []
        for b in self.adj.get((scope, a), ()):
            s = self.pair[(scope, min(a, b), max(a, b))][0]
            if s >= min_support and s / n_a >= min_conf:
                out.append((b, s, s / n_a))
        out.sort(key=lambda r: (-r[2], -r[1], r[0]))
        return out[:k]

    def consequents(self, min_support: int, min_conf: float, top_k: int = 10) -> dict[tuple, list[tuple]]:
        """(범위, a) → [(b, support, confidence, lift, pair_record)] 신뢰도·지지도 순."""
        out: dict[tuple, list] = defaultdict(list)
        for (scope, a, b), rec in self.pair.items():
            s = rec[0]
            if s < min_support:
                continue
            for x, y in ((a, b), (b, a)):
                conf = s / self.single[(scope, x)]
                if conf >= min_conf:
                    lift = conf / (self.single[(scope, y)] / self.n[scope])
                    out[(scope, x)].append((y, s, conf, lift, rec))
        for k in out:
            out[k].sort(key=lambda r: (-r[2], -r[1], r[0]))
            del out[k][top_k:]
        return out


def directory(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def stem(path: str) -> str:
    """이름 짝 비교용 줄기 — Foo.java·FooTest.java·foo_test.go → foo."""
    name = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    for suf in ("tests", "test", "_test", "spec"):
        if name.endswith(suf) and len(name) > len(suf) + 2:
            return name[: -len(suf)].rstrip("_")
    return name


def fallback(path: str, freq: dict[str, int], exclude: set[str], k: int) -> list[tuple[str, str, int]]:
    """규칙이 모자랄 때 채우는 후보 [(경로, 이유, 변경 수)]: 이름 짝 → 같은 디렉터리, 각각 자주 바뀐 순.
    평가(EVALUATION.md)에서 마이닝 + 이 채움이 모든 기준선보다 나았다(hybrid)."""
    ranked = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    out, seen = [], set(exclude) | {path}
    for reason, test in (("이름 짝", lambda x: stem(x) == stem(path)), ("같은 디렉터리", lambda x: directory(x) == directory(path))):
        for x, n in ranked:
            if len(out) >= k:
                return out
            if x not in seen and test(x):
                out.append((x, reason, n))
                seen.add(x)
    return out


def strength(support: int, conf: float) -> str:
    if support >= 10 and conf >= 0.6:
        return "high"
    if support >= 5 and conf >= 0.4:
        return "medium"
    return "low"


def mine(corpus, level: str = "commit") -> list[dict]:
    m = corpus.cfg["mining"]
    rules = Rules()
    for tx in transactions(corpus, level):
        rules.add(*tx)
    out = []
    for (scope, a), cons in sorted(rules.consequents(int(m["min_support"]), float(m["min_confidence"])).items()):
        for b, s, conf, lift, rec in cons:
            out.append(C.make(
                "cochange", scope, {"type": "path", "id": a},
                {"support": s, "antecedent": rules.single[(scope, a)], "confidence": round(conf, 3),
                 "lift": round(lift, 2), "level": level},
                strength(s, conf), list(rec[3]), [rec[1], rec[2]], RECIPE, obj={"type": "path", "id": b},
                n_evidence=s))
    return out
