---
name: atlas
description: 업무 링크·요청·장애 증상을 받았을 때 work-atlas 카드로 원형을 판별하고 트리아지 브리프를 낸다. 결과가 좋았던 처리 수와 함정을 건수와 함께 보여주고, 예측과 사람의 판정을 기록한다.
---

# atlas — 트리아지 브리프 (U1)

`ATLAS_HOME`: ______ ← work-atlas 사내 인스턴스의 절대 경로로 채운다.
이 폴더를 `~/.claude/skills/atlas` 로 심볼릭 링크하면 어느 저장소에서 작업하든 같은 규칙으로 동작한다.

## 규칙

- `ATLAS_HOME` 안은 **읽기만** 한다. 쓰기는 `python3 $ATLAS_HOME/method/recipes/record_event.py` 하나뿐이다.
- 카드에 없는 원형·어휘 id를 만들지 않는다. 맞는 것이 없으면 `residual` 이라고 말하고, 후보는 `$ATLAS_HOME/inbox/` 에 파일 하나로 남긴다.
- 신뢰도는 `high · medium · low` 서열로 말한다. 정의는 `$ATLAS_HOME/method/codebook.yaml` 의 `confidence_scale`.
- 처리 수의 결과 연관은 건수와 함께 말하고, 인과로 말하지 않는다.

## 절차

1. 입력(이슈·스레드 링크, 요청 문장, 증상)에서 결정론 단서를 뽑는다: 조인 키, 파일 경로, 오류 시그니처, 기존 라벨.
2. `$ATLAS_HOME/build/model/` 과 `$ATLAS_HOME/build/cards/` 를 읽어 원형 후보 1~3개와 적합도(`strong · partial · weak`)를 판단한다.
3. 브리프를 낸다: 원형과 트랙 A 패싯(서열 신뢰도) · 숨은 하위작업 · 함정 상위 3개 · 결과가 좋았던 처리 수와 건수 · 끌어들일 관계자 · 예상 규모 · 위임 수준 · 유사 사례 3건(`similar.py`).
4. 예측을 남긴다: `record_event.py triage` (원형·규모·함정·제안한 처리 수).
5. 끝에 두 가지를 묻고 남긴다: "원형이 맞나요(예·아니오·모름)?", "도움이 됐나요(됨·안 됨·모름)?" → `record_event.py feedback`.

카드 본문을 이 파일에 복사하지 않는다. 항상 경로로 읽는다.
