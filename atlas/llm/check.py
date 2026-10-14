"""검증 — LLM 이 낸 문장 하나를 근거 · 자리표시 · 숫자 · 표기로 거른다. 모든 길(백엔드 호출 · import)이 같은 검사를 쓴다."""
from __future__ import annotations

import re

from .base import NUMBER, PLACEHOLDER

# 숫자를 글자로 쓰는 길 — 해설(자리표시만 허용)에서 막는다. 정규식으로 모두 막을 수는 없다: 모델이 실제로 쓰는 꼴과 흔한 변형을 막고,
# 남는 것은 사람 판정(atlas record llm-feedback)과 실측(atlas llm bench)이 잡는다.
#   기호 숫자(원문자 · 로마 숫자 · 분수 · 위 첨자 · 한자 숫자) / 고유어 수사 + 단위, + 띄우고 명사('두 파일') / 어림수(두세 · 한두 · 서너) /
#   한자어 수사(오 퍼센트 · 이십 일 · 삼십 분) / 기간(이틀 · 일주일 · 보름 · 며칠 · 몇 주 · 수개월 · 반년) /
#   비율 말(셋 중 하나 · 열에 아홉 · 삼분의 일 · 반 이상) / 개수 말(단 하나 · 하나뿐 · 한 번도)
_UNIT = r"건|개|번|배|명|가지|차례|군데|곳|단계|개월|달|주일|주|해|시간|퍼센트|프로"
_SINO = r"[일이삼사오육칠팔구]"
# 단위 뒤에는 조사나 끝만 — '한 주기' · '한 개발자' · '백분위' 처럼 다른 낱말의 첫 글자는 단위가 아니다
_AFTER = (r"(?=$|[^가-힣]|(?:이|가|은|는|을|를|의|에|에서|으로|로|도|만|씩|째|간|동안|정도|쯤|가량|뿐|이상|이하|안|내|"
          r"이다|이고|이며|이면|이나|이지만|과|와|부터|까지)(?![가-힣]))")
_NATIVE = r"(?:(?:열|스물|서른|마흔|쉰|예순|일흔|여든|아흔)?(?:두|세|석|네|넉|다섯|여섯|일곱|여덟|아홉)|열|스물|스무|서른|마흔|쉰|예순|일흔|여든|아흔)"
_ROUGH = r"(?:한두|두세(?!요)|두서너|서너|너덧|네다섯|대여섯|예닐곱|일여덟|열댓|두어(?![야서도라요]))"
NUMBER_WORDS = re.compile(
    r"[\u00B2\u00B3\u00B9\u2070-\u2079\u2776-\u2793"
    r"\u00BC-\u00BE\u2460-\u24FF\u2150-\u218F\u3007\u4E00\u4E8C\u4E09\u56DB\u4E94\u516D\u4E03\u516B\u4E5D\u5341\u767E\u5343"
    r"\u4E07\u842C\u5104]"
    rf"|(?<![가-힣])(?:{_NATIVE}|백|천|만|수십|수백|수천|십여|몇십|몇백)\s?(?:{_UNIT}){_AFTER}"
    r"|(?<![가-힣])(?:스무|서른|마흔|예순|일흔|여든|아흔)\s[가-힣]"
    rf"|(?<![가-힣]){_ROUGH}"
    rf"|(?<![가-힣])한\s?(?:건|개|명|차례|군데|개월|달|주일|주|해|시간){_AFTER}"   # '한 번 더' · '한 가지' · '한 단계' 는 관용
    r"|(?<![가-힣])한\s?번(?:뿐|만|도|씩|째)"
    rf"|(?<![가-힣]){_SINO}?(?:십|백|천){_SINO}?\s?(?:{_UNIT}|일|년|분|초|회|%){_AFTER}"
    rf"|(?<![가-힣]){_SINO}\s?(?:개월|주일|년간|년|퍼센트|프로|%){_AFTER}"
    rf"|(?<![가-힣])[삼사오육칠팔구]\s(?:일|주|분|시간){_AFTER}"                    # 이 · 일 은 '이 주' · '일 건' 과 겹쳐 뺀다
    rf"|(?<![가-힣])(?:하루|이틀|사흘|나흘|닷새|엿새|이레|여드레|아흐레|열흘|보름|일주일|이주일|며칠|반년|반나절|당일){_AFTER}"
    rf"|(?<![가-힣])(?:몇|수)\s?(?:주|달|개월|년|해|시간|일|번|건|차례|배){_AFTER}"
    r"|(?<![가-힣])(?:둘|셋|넷|다섯|열|백)\s?(?:중|에)\s?(?:하나|한|둘|셋|넷|아홉|여덟|일곱)"
    r"|(?<![가-힣])(?:단|딱)\s?하나|(?<![가-힣])하나(?:뿐|도)"
    rf"|(?<![가-힣]){_SINO}분의\s?{_SINO}|(?<![가-힣])반\s?(?:이상|이하|넘|가까이|가량|정도|쯤)"
    r"|(?<![가-힣])과반|거의\s?항상|십중팔구|(?<![A-Za-z])(?:twice|thrice|dozens?)(?![A-Za-z])")
# 고유어 수사 + 띄우고 명사('두 파일' · '세 사람') — 이 문장이 인용한 구체적인 근거(이슈 · 파일 · 카테고리)의 수와 같을 때만 쓴다
# ("ZOOKEEPER-1105 와 ZOOKEEPER-4299 … 두 사례의 논의를 먼저 읽는다"). '열 수 있다' 와 겹쳐 열은 세지 않는다
COUNTED_NOUN = re.compile(r"(?<![가-힣])(두|세|네|다섯|여섯|일곱|여덟|아홉)\s[가-힣]")
NATIVE_VALUE = {"두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9}
# 크기 낱말 — 비율 사실의 크기 구간과 맞을 때만 쓴다(그 사실을 같은 문장에 인용해야 한다). 구간: <5% · <15% · <35% · <65% · 그 이상
# 몫을 말하는 낱말만 센다. 보통 · 대개 · 주로 는 전형을 말하는 부사라 중앙값 사실과 함께 쓰는 것을 막지 않는다
_HALF_TAIL = r"(?=(?:이다|이고|이며|이|은|을|도|만|쯤|정도|가량|이나)?(?![가-힣]))"
MAGNITUDE_WORDS = re.compile(
    r"드물|드문|극소수|거의\s?없|(?<![가-힣])소수(?!점)|절반|반수|(?<![가-힣])반" + _HALF_TAIL
    + r"|대부분|대다수|거의\s?(?:모든|다(?![가-힣]))|흔하|흔한|흔히|흔해|흔치|자주(?!색)|상당수|(?<![가-힣])다수(?!결)")
MAGNITUDE_CLASSES = (("rare", ("드물", "드문", "극소수", "거의없")), ("few", ("소수",)), ("half", ("절반", "반")),
                     ("common", ("흔", "자주", "상당수", "다수")), ("most", ("대부분", "대다수", "거의")))
MAGNITUDE_BUCKETS = {"rare": {0, 1}, "few": {0, 1, 2}, "half": {3}, "common": {3, 4}, "most": {4}}
MAGNITUDE_HINT = {0: "드물다", 1: "드물다", 3: "절반 안팎", 4: "대부분"}       # 프롬프트의 사실 줄에 적는 낱말
# 빈도만 말하는 낱말 — 비율 사실을 함께 인용했을 때만 그 구간과 맞춘다. 관계·동반 변경("X 와 자주 얽힌다")은 비율 사실이 없다
SOFT_MAGNITUDE = ("자주",)
# 크기 낱말을 부정하거나 견주거나 말을 옮기면 구간이 뒤집히거나 벗어난다("드물지 않다" · "절반을 웃돈다" · "대부분이라고 할 수 없다").
# 크기 낱말부터 그 절의 끝(문장부호)까지 본다 — 서술어의 부정("대부분은 기한을 못 지킨다")도 함께 버린다(보수적으로)
MAGNITUDE_TWIST_AFTER = re.compile(r"않|아니|못|넘|이상|이하|미만|초과|밑돌|밑돈|웃돌|웃돈|상회|하회|보다|다고|라고")
MAGNITUDE_TWIST_BEFORE = re.compile(r"(?:^|\s)(?:안|못|덜|더|결코)\s?$")
CLAUSE_END = re.compile(r"[.,;!?…·—–]")
TWIST_WINDOW = 30
# "가장 흔한 흐름" 은 순위(최빈)이지 몫이 아니다 — 크기 검사에서 뺀다
SUPERLATIVE = re.compile(r"(?:가장|제일)\s?$")


def magnitude_class(word: str) -> str:
    w = re.sub(r"\s", "", word)
    return next((c for c, heads in MAGNITUDE_CLASSES if w.startswith(heads)), "most")


MARKUP = re.compile(r"(?i)https?://|www\.|\]\(|!\[|<|>|javascript:|data:")


def strip_literals(text: str, literals: list[str]) -> str:
    """입력에 있는 이름·경로·제목(숫자를 품을 수 있는 것)을 지운 나머지 — 그 안의 숫자는 지어낸 것이 아니다.
    글자가 없는 것('1' · '2024')은 이름이 아니라 숫자라 지우지 않는다. 숫자 한가운데에서 잘라 내지 않는다('1' 로 '11건' 을 풀지 않게)."""
    for lit in literals:
        if lit and any(ch.isdigit() for ch in lit) and re.search(r"[A-Za-z가-힣]", lit):
            text = re.sub(r"(?<![0-9])" + re.escape(lit) + r"(?![0-9])", " ", text)
    return text


def check_stmt(stmt: dict, allowed: set[str], *, facts: set[str] | None = None, literals: list[str] = (),
               numbers: dict[str, set[str]] | None = None, generic: set[str] = frozenset(),
               magnitudes: dict[str, int | None] | None = None) -> tuple[dict | None, str | None]:
    """문장 하나 — 근거 · 자리표시 · 숫자 · 표기. 통과하면 (정리된 문장, None), 아니면 (None, 이유).
    facts   주어지면 해설 모드: 숫자는 {사실 id} 로만. 쓴 자리표시는 근거로 센다. 숫자 낱말은 막는다
    magnitudes  해설 모드의 사실 id → 크기 구간. 크기 낱말(드물다·절반·대부분)은 같은 문장에 인용한 사실의 구간과 맞을 때만
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
            return None, f"자리표시 밖의 숫자 {re.findall(r'[0-9]+(?:[.,][0-9]+)*', bare)[:3]}"
        if NUMBER_WORDS.search(bare):
            return None, f"숫자·양을 낱말로 썼다 ({NUMBER_WORDS.search(bare).group()}) — 사실 id 로 쓴다"
        concrete = len([r for r in refs if r not in facts and r not in generic])
        for m in COUNTED_NOUN.finditer(bare):
            if NATIVE_VALUE[m.group(1)] != concrete:
                return None, f"수를 낱말로 썼다 ({m.group()}…) — 인용한 근거 {concrete}개와 맞지 않는다"
        cited = [(magnitudes or {}).get(r) for r in refs]
        for m in MAGNITUDE_WORDS.finditer(bare):
            if SUPERLATIVE.search(bare[max(0, m.start() - 4):m.start()]):
                continue
            clause = CLAUSE_END.split(bare[m.end():m.end() + TWIST_WINDOW], 1)[0]
            if MAGNITUDE_TWIST_AFTER.search(clause) or MAGNITUDE_TWIST_BEFORE.search(bare[max(0, m.start() - 5):m.start()]):
                return None, f"크기 낱말을 부정하거나 견줬다 ({m.group()}…) — 구간 낱말을 그대로 쓴다"
            if m.group().startswith(SOFT_MAGNITUDE) and all(b is None for b in cited):
                continue                                # 비율 사실 없이 빈도만 — 관계·동반 변경을 말하는 것
            ok_buckets = MAGNITUDE_BUCKETS[magnitude_class(m.group())]
            if not any(b in ok_buckets for b in cited):
                return None, f"크기 낱말이 인용한 사실의 크기와 맞지 않는다 ({m.group()})"
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
            why.append(f"{reason}: {s.get('text', '')[:140]}")
    return kept, why
