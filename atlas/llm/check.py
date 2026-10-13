"""검증 — LLM 이 낸 문장 하나를 근거 · 자리표시 · 숫자 · 표기로 거른다. 모든 길(백엔드 호출 · import)이 같은 검사를 쓴다."""
from __future__ import annotations

import re

from .base import NUMBER, PLACEHOLDER

# 숫자를 글자로 쓰는 길 — 원문자·로마 숫자·한자 숫자, 한국어 수사 + 단위, 양을 말하는 낱말. 해설(자리표시만 허용)에서 막는다.
NUMBER_WORDS = re.compile(
    r"[\u2460-\u24FF\u2150-\u218F\u3007\u4E00\u4E8C\u4E09\u56DB\u4E94\u516D\u4E03\u516B\u4E5D\u5341\u767E\u5343\u4E07\u842C\u5104]"
    r"|(?<![가-힣])(?:두|세|네|다섯|여섯|일곱|여덟|아홉|열|스물|스무|서른|마흔|쉰|예순|일흔|여든|아흔|백|천|만|수십|수백|수천|십여|몇십|몇백)"
    r"\s?(?:건|개|번|배|명|가지|차례|군데|개월|퍼센트|프로)"
    r"|절반|과반|반수|대다수|대부분|극소수|거의\s?(?:모든|없|다\b|항상)")
MARKUP = re.compile(r"(?i)https?://|www\.|\]\(|!\[|<|>|javascript:|data:")


def strip_literals(text: str, literals: list[str]) -> str:
    """입력에 있는 이름·경로·제목(숫자를 품을 수 있는 것)을 지운 나머지 — 그 안의 숫자는 지어낸 것이 아니다."""
    for lit in literals:
        if lit and any(ch.isdigit() for ch in lit):
            text = text.replace(lit, " ")
    return text


def check_stmt(stmt: dict, allowed: set[str], *, facts: set[str] | None = None, literals: list[str] = (),
               numbers: dict[str, set[str]] | None = None, generic: set[str] = frozenset()) -> tuple[dict | None, str | None]:
    """문장 하나 — 근거 · 자리표시 · 숫자 · 표기. 통과하면 (정리된 문장, None), 아니면 (None, 이유).
    facts   주어지면 해설 모드: 숫자는 {사실 id} 로만. 쓴 자리표시는 근거로 센다. 숫자 낱말·양 낱말도 막는다
    numbers 주어지면 근거별 숫자: 문장의 숫자는 그 문장이 인용한 근거에 있는 숫자여야 한다
    generic 그것만으로는 근거가 못 되는 refs (섹션 이름) — 구체적인 근거가 하나는 있어야 한다"""
    text = str(stmt.get("text") or "").strip()
    if not text:
        return None, "빈 문장"
    if MARKUP.search(text):
        return None, "링크·HTML 은 쓰지 않는다"
    holes = PLACEHOLDER.findall(text)
    refs = [r for r in dict.fromkeys(list(stmt.get("refs") or []) + (holes if facts is not None else [])) if r in allowed]
    if not [r for r in refs if r not in generic]:
        return None, "근거 없음(구체적인 refs 가 허락된 목록 밖이거나 섹션 이름뿐)"
    lits = sorted(literals, key=len, reverse=True)
    if facts is not None:
        bad = [h for h in holes if h not in facts]
        if bad:
            return None, f"모르는 사실 id {bad}"
        bare = strip_literals(PLACEHOLDER.sub(" ", text), lits)
        if re.search(r"\d", bare):
            return None, "자리표시 밖의 숫자"
        if NUMBER_WORDS.search(bare):
            return None, f"숫자·양을 낱말로 썼다 ({NUMBER_WORDS.search(bare).group()}) — 사실 id 로 쓴다"
    elif numbers is not None:
        ok = set().union(*(numbers.get(r, set()) for r in refs))
        extra = [n for n in NUMBER.findall(strip_literals(text, lits)) if n.replace(",", "") not in ok]
        if extra:
            return None, f"인용한 근거에 없는 숫자 {extra}"
    return {"text": text, "refs": refs}, None


def numbers_in(*texts: str) -> set[str]:
    return {n.replace(",", "") for t in texts for n in NUMBER.findall(t or "")}


def _stmts(stmts: list[dict], **kw) -> tuple[list[dict], list[str]]:
    kept, why = [], []
    for s in stmts or []:
        ok, reason = check_stmt(s, **kw)
        if ok:
            kept.append(ok)
        else:
            why.append(f"{reason}: {s.get('text', '')[:60]}")
    return kept, why
