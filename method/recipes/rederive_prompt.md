# 원점 재도출 프롬프트 (고정 레시피)

> 사용법: 재도출 실행마다 **새 임시 폴더**를 만들고 표본 WU 파일만 넣은 뒤, 그 폴더에서 밀폐 실행한다.
> 예: `claude --bare -p "$(cat rederive_prompt.md)" --output-format json --json-schema "$(cat rederive.schema.json)" --allowedTools "Read,Glob,Grep" --permission-mode dontAsk`
> 이 프롬프트를 실행마다 바꾸지 않는다. 바꾸면 버전을 올리고 결정기록에 남긴다. 아래 `{{SEEDED}}` 블록만 시드 실행에서 채운다.
> 출력 스키마 `rederive.schema.json` 은 이 프롬프트의 "출력" 절을 그대로 옮겨 1단계에서 만든다.

---

현재 폴더에 작업단위(WU) 파일들이 있다. 각 파일은 한 덩어리의 업무와, 그 업무를 이루는 관측(이슈·커밋·메시지 등)의 발췌다.

목표: 이 업무들을 **실제로 반복되는 모양**으로 묶는 범주를 처음부터 만든다. 기존 분류 체계는 없다고 가정한다.

절차:
1. 개방 코딩 — 파일마다 이 일이 무엇이었는지 짧은 자유 코드를 2~5개 붙인다. 근거가 된 관측 id를 같이 적는다.
2. 축 코딩 — 코드를 비교하며 묶어 후보 범주를 만든다. 범주마다 정의, 판별 신호, 배제 조건, 소속 WU를 적는다.
3. 같은 방식으로 **함정**(일을 어렵게 만들었거나 사후 문제를 낳은 것), **숨은 하위작업**(요청에는 없었지만 실제로 필요했던 일), **처리 수**(진행·해결에서 실제로 취한 결정적 행동 — 진단 단계, 해결 전략, 검증·출시 방식, 조율 방식)의 후보를 만든다. 처리 수는 관측에 남은 행동만 쓴다.
4. 어느 범주에도 잘 안 맞는 WU는 억지로 넣지 말고 잔차로 남긴다. 이유를 한 줄 적는다.
5. 장애·롤백·후속 수정·재화 사고의 흔적이 있는 WU는 한 건뿐이어도 별도 범주 후보로 남겨도 된다. 그 경우 `rare_high_risk: true`.

규칙:
- 폴더 밖의 문서·설정·기억을 쓰지 않는다. 파일에 있는 관측만 근거로 쓴다.
- 근거 없는 추측은 범주 정의에 넣지 않는다. 모르면 모른다고 적는다.
- 범주는 많을수록 좋은 게 아니다. 서로 겹치면 합치고, 한 건뿐인 범주는 `rare_high_risk` 가 아니면 잔차로 둔다.

{{SEEDED}}

출력 (JSON):
- `categories`: [{ `id`, `label`, `definition`, `signals`[], `not_when`, `members`[wu_id], `rare_high_risk`(bool), `evidence`[obs_id] }]
- `pitfall_candidates`: [{ `id`, `label`, `definition`, `members`[wu_id], `evidence`[obs_id] }]
- `subtask_candidates`: [{ `id`, `label`, `definition`, `members`[wu_id], `evidence`[obs_id] }]
- `move_candidates`: [{ `id`, `label`, `definition`, `members`[wu_id], `evidence`[obs_id] }]
- `residual`: [{ `wu_id`, `reason` }]
- `notes`: 이번 표본에서 확신하지 못한 점
