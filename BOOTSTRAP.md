# 시작 절차

> 사내 Claude Code 세션을 사내 인스턴스 저장소 루트에서 열고, 단계마다 아래 프롬프트를 붙여 넣는다.
> **트랙 A(최소 경로)를 먼저** 끝내고, 판정(사람 판정 20건 이상)을 통과하면 트랙 B로 넓힌다. 근거는 `METHODOLOGY.md` §12.
> 각 단계는 종료 기준을 수치로 확인한 뒤 넘어간다. 시간은 사람 기준 추정치다.

## 무엇이 준비돼 있고, 무엇을 만드나

| 레시피 | 상태 | 이유 |
|---|---|---|
| `record_event.py` 기록 · `validate.py` 사후 검증 · `render_wu.py` 입력 문서 · `score.py` 채점 · `similar.py` 유사 사례 · `_common.py` 관측 계약 | **제공됨, 테스트됨** (`tests/`) | 도메인·도구와 무관하다. 계약이 산문으로만 있으면 사내 세션이 다시 짜면서 어긋난다 |
| `collect_<source>.py` 수집 · `stitch.py` 상관 · `annotate.py` 주석 · `cards.py` 카드 | **사내에서 만든다** (아래 단계) | 사내 시스템·인증·LLM 실행 환경에 묶여 있다 |

사내에서 만드는 레시피도 제공된 계약을 쓴다: 관측은 `_common.check_observation` 을 통과해야 쓰고, 주석 입력은 `render_wu.py` 문서, 주석 출력은 `validate.py` 를 거친다.

## 준비 (10분)

1. 이 템플릿을 사내 private 저장소로 복제하고 `.template` 파일을 지운다(템플릿 전용 검사 해제). 회사 데이터는 사내 인스턴스에만 둔다.
   `LICENSE`(MIT 고지)는 지우지 않는다. 회사에 오픈소스 관리 절차가 있으면 반입 목록에 등록한다.
2. `pip install -r requirements.txt`
3. `git config core.hooksPath .githooks` — 커밋 전 무결성 검사를 켠다.
4. `python3 method/recipes/check_integrity.py` 가 `integrity ok`, `python3 -m unittest discover -s tests` 가 전부 통과하는지 본다.

---

## 트랙 A — 최소 경로

### A0. 경계 (1시간)

```text
이 저장소는 work-atlas 사내 인스턴스다. CLAUDE.md, METHODOLOGY.md, method/policy.md 를 먼저 읽어라.
A0 단계다. 아직 어떤 데이터도 수집하지 마라.
1) 이 세션에서 닿을 수 있는 업무 시스템을 도구 목록·MCP 설정·설치된 CLI로 실측해 표로 보여줘
   (시스템, 접근 방식: API/CLI/MCP, 읽기·쓰기 범위, 인증 형태). 확인 못 한 칸은 [미확인].
2) method/policy.md §1 의 빈칸 중 내가 답해야 하는 것만 한 번에 묶어 질문해라.
   특히 --bare 실행에 쓸 수 있는 LLM 인증 형태와 data/ 를 둘 사내 저장 경로.
내 답을 받으면 policy.md §1 을 채우고, method/DECISIONS.md 를 만들어 첫 결정들을 한 줄씩 기록해라.
```

종료 기준: 데이터 경계·저장 경로·실행 위치·LLM 인증 형태가 문장으로 기록됨.

### A1. 정찰 — 트래커와 형상관리만 (반나절)

```text
A1 단계(정찰 실측)다. 소스는 이슈 트래커와 형상관리 두 개만. 읽기 전용, 최근 14일 범위.
1) 소스마다 측정: 접근 경로, 수집기 종류(api·cli·mcp_sdk 중 무엇이 가능한가 — MCP로만 열려 있으면
   LLM 없이 MCP 클라이언트 SDK로 직접 호출하는 스크립트를 시도), 인증, 호출 한도, 원천 보존 기한,
   페이지네이션 한계, 14일간 사건 수, 조인 키 존재율(이슈 키가 커밋 메시지·브랜치·MR 제목에 나타나는 비율),
   기존 라벨(컴포넌트·라벨·에픽)의 존재와 형식.
2) 내가 최근 한 업무 5건을 성격이 다르게 고른다(계획 기능, 결함, 운영 요청, 긴급·장애, 문의).
   두 소스에서 종단 추적해 무엇이 키로 이어지고 무엇이 안 이어지는지 보여줘.
3) 만든다:
   - method/sources.yaml: 소스별 측정값(수집기 종류·원천 보존 기한·조인 키 커버리지·편향 메모). 수치에는 [실측].
     보존 결정은 policy.md §2 에 둔다 — 여기는 측정, 거기는 결정.
   - method/recipes/collect_<source>.py: 결정론 수집기 v0. 사건마다 _common.check_observation 을 통과시키고,
     발췌는 data/excerpts.jsonl 에 따로 쓴다(사람 이름은 역할로 치환). --dry-run 으로 샘플 20줄을 내고
     검증 결과를 보여줘라. 아직 data/ 에 쓰지 마라.
   - annotation.schema.json 을 claude -p --json-schema 에 실제로 넣어 짧은 더미 입력으로 한 번 돌려,
     구조화 출력이 이 스키마를 받아들이는지 확인하고 결과를 sources.yaml 에 [실측]으로 적는다.
4) policy.md §2 표와 §3 입력 문서 상한을 채운다.
```

종료 기준: 조인 키 커버리지 수치 · 수집기 dry-run 이 관측 계약 통과 · 스키마 수용 여부 실측.

### A2. 백필 · 작업단위 · 입력 문서 · 블라인드 골든 (반나절)

```text
A2 단계다.
1) 수집기로 최근 3~6개월을 data/observations/ 에 백필한다(git 밖). 잘린 구간은 partial.
2) method/recipes/stitch.py v0: 결정론 키 조인 → 확률 연결(시간 창, 행위자 역할, 텍스트 유사도).
   연결마다 confidence 서열과 signals. atlas 사건은 members 에서 뺀다. 출력 data/units/stitched.jsonl
   (method/schema.md L1 형식, annotation 없이). 다중 소스 비율·고아 비율을 출력.
3) 제공된 render_wu.py 로 입력 문서를 만든다(policy.md §3 상한을 인자로). 주석·재도출의 유일한 입력이다.
4) 블라인드 골든 20건: WU에서 층화 표본을 고른다(규모·기원이 고르게, 장애·롤백 흔적 있는 것 포함).
   먼저 나에게 입력 문서만 보여주고 트랙 A 패싯(codebook active_from: A) 라벨을 받는다.
   내 답을 받은 뒤에만 네 제안을 보여줘라. method/golden/labels.csv 에 blind=true, codebook_version 과 함께 기록한다.
```

종료 기준: WU 약 100건 `[추정]` · 입력 문서 생성 · 블라인드 골든 20건 · 다중 소스·고아 비율 기준선.

### A3. 첫 도출 (2시간)

```text
A3 단계(첫 원형 도출)다. METHODOLOGY.md §6.1 을 축소해서 한다.
1) 이 세션에서 개방 코딩 1회: 입력 문서를 훑어 후보 원형·함정·하위작업·처리 수를 만든다.
2) 격리 블라인드 도출 1회: 새 임시 폴더에 표본 WU의 입력 문서만 넣고(예외 트랙 WU는 반드시 포함),
   그 폴더에서 claude --bare -p 로 실행한다. 프롬프트는 method/recipes/rederive_prompt.md 의 --- 아래 본문만 보내고,
   시드 없는 실행이므로 {{SEEDED}} 는 빈 문자열로 바꾼다.
   이 저장소의 문서·코드북·네 1)번 결과가 그 프로세스에 보이면 안 된다.
3) 두 결과를 소속 WU 겹침으로 대응시켜 비교표를 보여주고, 원형 10개 이하로 병합하는 안을 낸다.
   예외 트랙(장애·롤백·후속 수정·결제 사고) 범주는 1건이어도 남긴다.
4) 내가 채택한 원형과 어휘를 codebook.yaml 에 코드북 형식으로 기록한다(examples 는 실제 WU 앵커만).
   version 을 1로 올리고 스키마를 다시 생성한다. 골든셋 20건에 원형 라벨을 블라인드로 받는다.
```

종료 기준: 원형 v1(10개 이하) · 잔차 목록 · 골든셋 원형 라벨.

### A4. U1 트리아지 (2~3시간)

```text
A4 단계다. U1 트리아지만 구현한다.
1) method/recipes/annotate.py: render_wu.py 문서를 입력으로
   claude --bare -p --output-format json --json-schema "$(cat method/annotation.schema.json)" 를 호출하고
   structured_output 을 WU 의 annotation 에 넣어 data/units/annotated.jsonl 로 쓴다. 캐시 키는 (input_hash, recipe, codebook_version, model).
   --max-budget-usd 로 실행마다 상한을 걸고 total_cost_usd 를 모은다.
2) 제공된 validate.py 로 사후 검증한다(annotated → data/units/current.jsonl). 위반률을 출력해라(policy.md §7 지표).
   채점·유사 사례·카드·스킬은 current.jsonl 만 읽는다.
3) method/recipes/cards.py: 원형 카드를 build/cards/ 에 만든다. 형식은 METHODOLOGY.md §8.
   카드에는 어휘 id와 건수만 둔다(WU 링크 없음). 함정은 결정론 신호가 있는 것만.
   빌드 전체(주석 포함)의 비용 합계와 함께 build/MANIFEST.json 을 마지막에 쓴다.
4) 사용 스킬: method/skill/atlas/SKILL.md 의 ATLAS_HOME 을 이 저장소 절대 경로로 채우고,
   ~/.claude/skills/atlas 로 심볼릭 링크한다. 서버 코드 저장소 등 어디서 작업하든 같은 규칙으로 동작한다.
   유사 사례는 similar.py, 예측·판정 기록은 record_event.py 를 쓴다(둘 다 제공됨).
5) 실제 업무 3건에 써 보고 결과를 보여줘.
```

종료 기준: 실제 업무 3건 사용 · L0에 예측·판정 사건 · `check_integrity.py` 통과.

### A5. 운영 (주 10분)

```text
A5 단계다. policy.md §1 의 실행 위치에 맞춰 주기 작업을 정의한다.
- 일일 표집: 하루 한 번 "오늘 기록 없이 한 일" 한 줄을 묻고 record_event.py self-report 로 남긴다.
- 주간 수확(무인): 수집 → WU 갱신 → score.py 로 채점(원형·규모는 WU 종료 시 잠정, 함정·처리 수는 확정 시)
  → data/reports/YYYY-MM-DD-harvest.md (git 밖. 델타, 확인 질문 5건 이내: 불확실 WU와 수신함 합산, 만료된 수신함 항목).
- 월간 재빌드: 무작위 15% 재주석 포함 → build/ 교체 + 지표 표(원형 단위 집계는 reports/) → 내가 git tag gen-YYYY-MM 으로 채택.
- 무인 호출은 --bare, --permission-mode dontAsk, 최소 --allowedTools, --max-turns, --max-budget-usd,
  필요하면 --mcp-config 와 --strict-mcp-config 로 범위를 닫는다.
- 알림: 정상·무변동은 침묵. 사람 결정이 필요한 것, 지표 경고, 실행 실패만 알린다.
```

**판정**: 달력이 아니라 `score.py` 가 "판정 가능"(사람 판정 20건 이상)을 낼 때 한다 — 대개 4~8주. 원형 적중률(사람 판정 기준)과 "도움 됨"(policy.md §7)을 본다. 미달이면 카드가 아니라 용도와 트리거 지점을 다시 설계한다. 통과하면 트랙 B.

---

## 트랙 B — 넓히기 (세대 2개 이후)

```text
트랙 B다. policy.md §3 의 가동 조건을 먼저 확인하고, 충족된 것만 켠다.
1) 소스 확장: 위키·메일·메신저·배포·캘린더·AI 세션 중 정책이 허용하는 것을 A1과 같은 방식으로 실측·수집.
   llm_mediated 소스는 분리 표시한다.
2) 코드북 track 을 B 로 올려 나머지 패싯을 켜고 스키마를 다시 생성한다. 골든셋을 40~50건으로 늘린다(블라인드 우선).
   패싯별 Krippendorff α 를 원시 일치율·부트스트랩 신뢰구간과 함께 계산한다. 다중값 패싯은 값별 이진 일치도.
3) 격리 독립 도출 3~5회(METHODOLOGY.md §6.1 전체): 실행마다 다른 표본 + 예외 트랙 고정 포함,
   2회 이상 시드 없음, 1회 시드 제공, 가능하면 1회 다른 모델. 합의·비교표·기존 라벨 AMI 를 낸다.
4) 일치도·재주석·안정성 게이트와 오버레이를 켠다.
5) U2~U5 를 U1 적중률이 기준 이상일 때 하나씩 연다.
```

**세대 3개 판정**: 적중률·잔차·일치도·비용 추세로 방법의 성숙도를 판정한다. 개선이 없으면 패싯 축 자체를 원점 재도출한다.
