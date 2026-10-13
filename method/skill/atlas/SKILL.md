---
name: atlas
description: 업무 링크·이슈 키·요청·장애 증상을 받았거나 고칠 파일을 정할 때 work-atlas 로 트리아지 브리프를 낸다. 그 일의 업무 카테고리(보통의 흐름 · 지금 단계 → 보통 다음 · 병목 · 참조 사례 · 접점), 비슷한 과거 작업과 결과, 바꿀 가능성이 큰 파일, 함께 바뀌어 온 파일, 결함이 몰린 파일을 건수와 함께 보여주고 예측과 사람의 판정을 기록한다.
---

# atlas — 트리아지 브리프

`ATLAS_HOME`: ______ ← work-atlas 사내 인스턴스의 절대 경로로 채운다.
이 폴더를 `~/.claude/skills/atlas` 로 심볼릭 링크하면 어느 저장소에서 작업하든 같은 규칙으로 동작한다.
`atlas` 명령은 `pip install -e "$ATLAS_HOME"` 로 한 번 설치해 둔다. 설치하지 않았으면 `atlas …` 를 `PYTHONPATH="$ATLAS_HOME" python -m atlas …` 로 바꿔 쓴다.

## 규칙

- `ATLAS_HOME` 안은 **읽기만** 한다. 예외는 셋이다: 예측·판정 기록(`atlas record`), 수신함 제안 파일(`inbox/`) 하나, 사람이 시킨 LLM 답 들이기(`atlas llm import` — 검증을 통과한 것만 그 작업의 자리로 간다: 해설 `data/llm/`, 주석 `data/units/annotated.jsonl`, 주간 요약 `data/reports/`, 이름 제안 `inbox/`).
- 맥락 출력 첫 줄의 **데이터 상태 경고**(키 커버리지·고아 비율·옛 형식)가 있으면 브리프 첫머리에 그대로 밝힌다.
- 이슈 줄에 "확정 뒤 다시 열려 지금 진행 중"이 있으면 과거 결과(clean 등)보다 지금 상태를 앞세운다. 확정 결과는 당시의 결과일 뿐이다.
- 수치는 건수와 함께 말한다. 함께 바뀐 파일·실천과 결과는 **연관**이지 인과가 아니다.
- 카드에 없는 원형·어휘 id 를 만들지 않는다. 맞는 것이 없으면 `residual` 이라고 말하고, 후보는 `$ATLAS_HOME/inbox/` 에 파일 하나로 남긴다.
- 원형·패싯 판단의 신뢰도는 `high · medium · low` 서열로 말한다(동반 변경의 "40%" 같은 비율 신뢰도와 다르다). 정의는 `$ATLAS_HOME/method/codebook.yaml` 의 `confidence_scale`.
- 소요 시간을 예측하지 않는다. 같은 카테고리의 처리 통계(중앙·P90, 건수)와 "보통 다음" 단계의 비율을 보여 줄 뿐이다 — 평가에서 예측이 기준선보다 낫지 않았다(`EVALUATION.md`). 카테고리의 흐름도 연관이다.
- 카테고리 해설(LLM)이 보이면 그 문장의 수치는 지금 값으로 채워진 것이다. 해설에 없는 수치를 지어 붙이지 않는다. 틀린 해설을 보면 사람에게 알리고, 사람이 동의하면 `atlas record --root "$ATLAS_HOME" llm-feedback --key '<카테고리>' --verdict wrong --note "<왜>"` 로 내린다.
- 유사 사례의 앵커는 브리프에만 보여 준다. 커밋·문서·메시지에 붙여 넣지 않는다.

## 절차

1. 입력에서 결정론 단서를 뽑는다: 이슈 키, 파일 경로(저장소 기준), 오류 시그니처, 기존 라벨, 요청·증상 문장.
2. 맥락을 읽는다 — 원형이 없어도 첫날부터 동작한다:
   `atlas context --root "$ATLAS_HOME" [--issue <키>] [--path <경로>]… [--text "<요청·증상 한두 문장>"]`
3. 코드북에 원형이 있으면(`$ATLAS_HOME/out/cards/` 가 있으면) 원형 후보 1~3개와 적합도(`strong · partial · weak`)를 판단하고 원형 기준 유사 사례를 찾는다:
   `atlas similar --root "$ATLAS_HOME" --archetype <id> [--path <경로>] [--error <시그니처>] -k 3`
4. 브리프를 낸다 (있는 것만, 짧게):
   - 데이터 상태
   - 업무 카테고리: 지금 단계 → 보통 다음(비율 · 걸린 시간), 오래 머무는 단계, 먼저 볼 사례(주의 · 대표), 접점(팀 · 채널 · 역할)
     — 더 필요하면 `atlas guide --root "$ATLAS_HOME" --category '<카테고리>'` (흐름 · 순서 · 관계 · 참조점 전부, Markdown)
   - 비슷한 과거 작업과 결과 · 그 작업들이 바꾼 파일
   - 함께 바뀌어 온 파일 · 결함이 몰린 파일
   - [원형이 있으면] 원형·트랙 A 패싯(서열 신뢰도) · 숨은 하위작업 · 함정 상위 3개 · 결과가 좋았던 처리 수와 건수 · 끌어들일 관계자 · 위임 수준
5. 예측을 남긴다. 원형이 아직 없으면 `--archetype residual` 로 규모만 예측한다:
   `atlas record --root "$ATLAS_HOME" triage --anchor <type:id> --archetype <id|residual> --conf <서열> --scale <규모> [--pitfall <id>]… [--move <id>]…`
6. 끝에 두 가지를 묻고 남긴다 — "원형이 맞나요(예·아니오·모름)?", "도움이 됐나요(됨·안 됨·모름)?":
   `atlas record --root "$ATLAS_HOME" feedback --anchor <type:id> --archetype-ok yes|no|unsure --verdict helpful|not_helpful|unknown`

`atlas record` 가 종료 코드 2로 거부하면(코드북에 없는 id, 트리아지 없는 판정 등) 값을 고쳐 다시 부르고, 우회하지 않는다.
카드·맥락 본문을 이 파일에 복사하지 않는다. 항상 명령으로 읽는다.
MCP 서버를 켰으면 같은 것을 도구로 읽는다 — `atlas_context` · `atlas_categories`(카테고리 트리) · `atlas_category`(카테고리 하나의 안내서).
파일을 고치는 순간의 힌트는 훅이 따로 넣는다(`atlas hook --print-settings`, BOOTSTRAP A2).

## 작업 폴더에서

작업 공간의 작업 폴더(`$ATLAS_HOME/WORKSPACE.md`)에서 일할 때:
- 맥락은 `atlas context --root "$ATLAS_HOME" --work .` — 그 작업 기록의 키·제목·문제로 찾는다. 작업 CLAUDE.md 가 `@CONTEXT.md` 를 가져오면 이미 들어와 있으니 다시 부르지 않는다.
- 비슷한 과거 작업에 기록 위치가 붙어 있으면 그 `WORK.md` 의 결과·회고를 먼저 읽는다. 브리프의 요점(조심할 파일·과거의 함정)은 이 작업의 `WORK.md` 처리에 한 줄로 남긴다.
- 끝낼 때 `WORK.md` 의 결과·회고를 채우고 `atlas work close --root "$ATLAS_HOME"` 을 부른다. 예측을 남긴 일이면 그다음 판정 두 질문(위 6).
- `CONTEXT.md` 는 생성물이다 — 고치지 않는다(`atlas work context` 로 다시 만든다). 작업 폴더를 옮기거나 지우지 않는다.
- 작업 기록 초안이 필요하면 `atlas llm run work_draft --root "$ATLAS_HOME" --work . --write` (백엔드가 있을 때) — `WORK.draft.md` 로만 쓴다. 사람이 골라 `WORK.md` 로 옮긴다.

## 대화 세션이 LLM 백엔드일 때

사내에서 승인한 LLM 백엔드가 없으면(`atlas llm status` 의 백엔드가 none) 사람이 시킬 때 이 세션이 그 역할을 한다:
1. `atlas llm prompt <작업> --root "$ATLAS_HOME" --key '<대상>'` 의 출력(시스템 프롬프트 · 입력 · 출력 스키마)을 그대로 따른다. 입력 밖의 지식 · 수치를 쓰지 않는다.
2. 답(JSON)을 파일로 쓰고 `atlas llm import <작업> --root "$ATLAS_HOME" --key '<대상>' --file <답.json>` 으로 들인다. 거절되면 이유를 보고 고쳐 다시 들인다 — 검증을 우회하지 않는다.
