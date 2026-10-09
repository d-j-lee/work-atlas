# 데이터 계약 — 관측 · 작업단위 · 오버레이 · 사용 기록 · 골든셋 · 매니페스트

> 규칙과 이유의 정본은 이 문서다. **기계 계약**은 둘이다 — 관측 한 줄은 `observation.schema.json`(손으로 관리), LLM 주석 출력은 `annotation.schema.json`(`codebook.yaml` 에서 생성).
> 두 계약은 기준 레시피(`recipes/_common.py` 의 `check_observation`, `recipes/validate.py`)가 실행으로 집행하고, `tests/` 가 그 집행을 시험한다.
> 필드를 바꾸면 `DECISIONS.md` 한 줄과 함께 `schema_version` 을 올린다.

schema_version: 0

## 저장 위치 요약

| 무엇 | 경로 | git |
|---|---|---|
| L0 관측 | `data/observations/YYYY-MM.jsonl` | 제외 (사내 저장 경로) |
| 발췌 캐시 | `data/excerpts.jsonl` — 수집기가 만들고 policy §2 보존 기한으로 지운다 | 제외 |
| L1 작업단위 | `data/units/stitched.jsonl`(상관) → `annotated.jsonl`(주석) → `current.jsonl`(사후 검증 통과본) · 주석 캐시 `data/cache/` | 제외 |
| WU 입력 문서 | `data/render/<wu>-<해시8>.md`, `data/render/index.json` | 제외 |
| 주간 수확 리포트 | `data/reports/` | 제외 |
| L2 모델·L3 카드 (원형 단위 집계) | `build/model/`, `build/cards/`, `build/MANIFEST.json` | 포함 |
| 오버레이 | `overlays/*.yaml` | 포함 |
| 골든셋 | `method/golden/labels.csv` | 포함 (앵커 참조와 라벨만, 본문 없음) |

## L0 관측 — append-only

한 줄 = 한 사건. 기존 줄을 고치거나 지우지 않는다.

```json
{
  "obs_id": "issue:ABC-123#transition:2026-10-08T10:12:00+09:00",
  "source": { "system": "issue", "instance": "사내-트래커", "collector_kind": "api" },
  "kind": "issue.transition",
  "ts": "2026-10-08T10:12:00+09:00",
  "actor_role": "self",
  "objects": [ { "type": "issue", "id": "ABC-123", "url": "…" } ],
  "keys": { "issue_keys": ["ABC-123"], "branches": [], "mr_ids": [], "paths": [], "error_signatures": [] },
  "labels": { "component": ["billing"], "label": ["hotfix"], "epic": ["EP-9"] },
  "metrics": {},
  "excerpt_ref": null,
  "partial": false,
  "collector": "issue-collector@0.1.0",
  "collected_at": "2026-10-08T23:00:00+09:00"
}
```

| 필드 | 규칙 |
|---|---|
| `obs_id` | 결정론적이고 재수집해도 같은 값. 중복 제거 키 |
| `source.system` | `issue · vcs · ci_cd · deploy · wiki · mail · chat · calendar · monitor · ai_session · self_report · atlas`. 늘릴 땐 이 표를 먼저 고친다 |
| `source.collector_kind` | `api · cli · mcp_sdk · llm_mediated · human`. `llm_mediated` 는 결정론 보장 밖이며 통계에서 분리 표시한다. `human` 은 `record_event.py` 로 남긴 사람·사용 사건 |
| `kind` | `<system>.<event>` 형식이고 접두가 `source.system` 과 같아야 한다. 예: `vcs.commit`, `vcs.mr.merged`, `chat.message`, `self_report.untracked_work`, `atlas.triage` |
| `ts` · `collected_at` | ISO 8601, **시간대 필수**. 비교는 문자열이 아니라 시각으로 한다(시간대가 섞이면 문자열 순서가 틀린다) |
| `actor_role` | `self`, codebook `roles` 의 id, `bot`, `unknown` 중 하나. **개인 식별자를 넣지 않는다** |
| `objects` | 이 사건이 닿는 모든 객체. 한 사건이 여러 객체에 연결될 수 있다(객체 중심) |
| `keys` | 상관에 쓰는 조인 키. 원문에서 **결정론적으로** 뽑은 것만 |
| `labels` | 원천 시스템에 이미 붙어 있는 분류(컴포넌트·라벨·에픽 등). 창발 원형과 비교하는 기준선 |
| `metrics` | 결정론 수치·참거짓만 (변경 줄 수, 파일 수, 응답 시간 등). 문자열은 해석을 부르므로 받지 않는다 |
| `payload` | `atlas`·`self_report` 사건 전용(그 둘은 필수, 나머지는 금지): 사람의 입력과 그 시점의 예측. 해석이 아니라 "그때 그렇게 기록됐다"는 사실이다 |
| `excerpt_ref` | 원천 본문의 위치(선택). 본문은 원천에 두고, 빌드에 필요한 발췌만 `data/excerpts.jsonl` 캐시에 둔다 |
| `partial` | 페이지네이션·권한으로 잘린 수집이면 `true` |
| 정정 | 잘못 수집한 사건은 고치지 않고 `<system>.correction` 사건을 추가해 `objects` 에 `{"type": "obs", "id": "<대상 obs_id>"}` 를 건다 |
| 쓰기 | 수집기는 덧붙이기 전에 `check_observation` 으로 검증한다. 사람·사용 사건은 `record_event.py` 만 쓴다 |

**금지**: LLM 요약·분류·추정값. L0에 해석이 들어가면 모든 재빌드가 그것을 상속한다.

### 사용 기록 — `source.system: "atlas"`

체계 자신의 사용과 사람의 자기 보고도 관측이다. 대화 세션이 L0에 쓸 수 있는 **유일한 경로**이며, 반드시 `method/recipes/record_event.py` 를 거친다(append 전용). 이 레시피는 예측 값이 코드북의 활성 id 인지 확인하고, 아니면 쓰지 않는다.

트리아지 시점 — 예측을 남긴다:
```json
{ "kind": "atlas.triage", "objects": [ { "type": "issue", "id": "ABC-130" } ],
  "payload": { "use": "U1", "codebook_version": 1, "generation": "gen-2026-11",
               "pred": { "archetype": "ops-data-fix", "archetype_conf": "medium", "scale": "small",
                         "pitfalls": ["missing-rollback-script"], "moves_suggested": ["snapshot-repro"] } } }
```
피드백 — 사람의 판정과 도움 여부:
```json
{ "kind": "atlas.feedback", "objects": [ { "type": "issue", "id": "ABC-130" } ],
  "payload": { "use": "U1", "archetype_ok": "yes", "verdict": "helpful" } }
```
자기 보고(일일 표집):
```json
{ "kind": "self_report.untracked_work", "objects": [], "payload": { "text": "운영팀 요청으로 로그 조회 30분" } }
```

| 규칙 | 이유 |
|---|---|
| `archetype_ok`(`yes · no · unsure`)가 원형 예측의 **1차 정답**이다 | 나중의 LLM 주석을 정답으로 쓰면 LLM이 LLM을 채점하게 된다. LLM 주석과의 일치는 '일관성'으로 따로 보고한다 |
| 원형·규모는 WU **종료 시 잠정 채점**, 함정·처리 수는 **확정 시 채점** | 열린 창(30일)을 기다리면 판정이 몇 주 밀린다. 사후 결과가 필요한 항목만 기다린다 |
| 판정(`atlas.feedback`)은 같은 앵커의 트리아지가 먼저 있어야 쓴다. 채점은 트리아지 **이후**의 판정만 쓴다 | 채점할 예측이 없는 판정은 지표를 흐린다 |
| `unknown`·`residual` 원형 예측은 "보류"로 따로 센다 | 고른 것이 없으니 맞혀도 적중이 아니다 |
| `atlas` 사건은 작업단위의 `members` 와 주석 입력에서 **제외**한다(채점 전용) | 예측이 자기 정답지로 새어 들어가지 않게 |
| `verdict`: `helpful · not_helpful · unknown` | 보조 지표 (새로움 편향이 있다) |

## L1 작업단위 — `data/units/{stitched,annotated,current}.jsonl`

상관 결과(`stitched`, 주석 없음) → 주석(`annotated`) → 사후 검증 통과본(`current`). 채점·유사 사례·카드·사용 스킬은 `current` 만 읽는다.

```json
{
  "wu_id": "wu:issue:ABC-123",
  "anchors": ["issue:ABC-123"],
  "span": { "start": "…", "end": "…", "open_window_until": "…", "settled": false },
  "members": [
    { "obs_id": "…", "link": "key", "confidence": "high" },
    { "obs_id": "…", "link": "probabilistic", "confidence": "low", "signals": ["time_window", "actor", "text_sim"] }
  ],
  "sources": ["issue", "vcs", "chat"],
  "annotation": { "...": "annotation.schema.json 형식" },
  "stats": {
    "duration_h": 0, "n_changes": 0, "n_files": 0, "roles": [],
    "reopen_count": 0, "followup_fix_wus": [], "induced_by_wus": [],
    "deterministic_signals": ["reopened", "rolled_back"]
  },
  "build": { "stitch_recipe": "stitch@0", "annotate_recipe": "annotate@0", "codebook_version": 0,
             "model": "<모델 ID>", "input_hash": "sha256:…", "generation": "gen-2026-10" }
}
```

| 규칙 | 이유 |
|---|---|
| `wu_id` 는 **앵커 객체**(가장 먼저 생긴 이슈·MR·스레드)에서 결정론적으로 만든다 | 재빌드해도 ID가 유지돼야 링크가 안 깨진다 |
| `settled` 는 열린 창이 지나면 `true`. 통계와 사후 항목 채점은 확정 WU만 쓴다 | 사후 결과가 다 들어오기 전에 판정하지 않는다 |
| `source.system` 이 `atlas` 인 사건은 `members` 에 넣지 않는다 | 예측 누수 차단 |
| 확률 연결은 `confidence` 서열과 `signals` 를 남긴다 | 상관 레시피를 고칠 때 근거가 된다 |
| 주석 캐시 키는 `(input_hash, annotate_recipe, codebook_version, model)` | 바뀐 것만 다시 코딩한다. 모델 교체도 드러난다 |
| 재빌드마다 무작위 15% 는 캐시를 무시하고 다시 주석한다 | 재주석 불일치율(주석 안정성) 측정 |
| `deterministic_signals` 는 L0 사실(재오픈·롤백·후속 수정·유발 결함)에서만 계산한다 | 카드에 실릴 함정의 자격 조건 |

## WU 입력 문서 — `render_wu.py` (주석·재도출의 유일한 입력)

LLM이 읽는 것은 이 문서뿐이다. 같은 WU면 언제 만들어도 같은 바이트가 나와야 한다.

| 항목 | 규칙 |
|---|---|
| 머리 | 앵커, 기간, 소스 목록, 기존 라벨, 결정론 신호, 기술 통계 |
| 본문 | 구성 관측을 시각순으로: 시각 · `system.kind` · 행위자 역할 · 조인 키 · 발췌 |
| 발췌 | 원천에서 빌드 때 조회. 비밀값 마스킹, 사람 이름은 역할로 치환. 사건당·문서당 길이 상한(policy) |
| 제외 | 주석(`annotation`), 빌드 정보(`build`), `atlas` 사건, 오버레이 — 현재 분류가 블라인드 도출에 새지 않게 |
| 해시 | `input_hash` = 이 문서의 sha256. 주석 캐시 키의 첫 요소 |

기준 구현: `method/recipes/render_wu.py`. **1차 방어는 수집기다** — 필요한 만큼만 발췌해 `data/excerpts.jsonl`(`{"obs_id", "text"}`)에 두고, 사람 이름은 역할로, 비밀값은 지운다. 렌더러의 마스킹(비밀값·토큰·쿠키·이메일·전화·주민/외국인 등록번호)은 수집기가 놓친 것을 잡는 안전망이며 완전하지 않다. 발췌를 사건당 상한의 4배로 먼저 자른 뒤 가리고, 모든 패턴의 반복에 상한을 둬서 긴 한 줄에서도 시간이 선형으로 는다.

## 사후 검증 — `validate.py` (스키마가 아니라 레시피가 집행)

| 규칙 | 내용 | 위반 시 |
|---|---|---|
| R1 근거 밖 | `evidence` 는 그 WU `members` 의 `obs_id` 여야 한다. 중복 근거는 하나로 센다 | 밖의 근거를 지운다 |
| R2 근거 없음 | `unknown` 이 아닌 분류 값(패싯·원형·어휘)은 근거 1개 이상 | 패싯·원형은 `unknown`, 어휘 항목은 뺀다. 서술(제목·요약)은 값을 두고 신뢰도를 `low` 로 |
| R3 과신 | `confidence: high` 는 서로 다른 근거 2개 이상 | `medium`(근거 1개)·`low`(0개)로 낮춘다 |
| R4 계약 밖 | 코드북에 없거나 휴면인 id, 이번 트랙에서 주석하지 않는 패싯(결정론 계산 패싯 포함) | `unknown` 으로 강등하거나 뺀다 |

입력은 바꾸지 않고 검증된 사본과 집계를 낸다. `violation_rate` 는 **위반이 하나라도 걸린 값 / 검사한 값**(규칙이 여러 개 걸려도 값 하나로 센다)이고, 규칙·필드별 건수와 코드북 버전이 다른 WU 수는 따로 낸다. 위반률은 policy §7 의 지표다.

## 오버레이 — `overlays/*.yaml`

```yaml
- target: "issue:ABC-123"        # wu_id 가 아니라 앵커 객체를 가리킨다 (재상관에도 살아남게)
  op: set_facet                  # set_facet | set_archetype | merge_into | split | note | link
  field: risk
  value: [money]
  reason: "보상 재지급이 포함됐는데 티켓에 안 적혀 있었다"
  by: self
  created: 2026-10-08
  expires: 2027-01-08            # 필수. 만료되면 다음 재빌드 전에 유지·삭제를 정한다
```

## 골든셋 — `method/golden/labels.csv`

| 열 | 설명 |
|---|---|
| `anchor` | WU 앵커 객체 참조 (본문 없음) |
| `field` | 패싯 id 또는 `archetype` |
| `value` | 라벨 (다중값은 `|` 로 구분) |
| `blind` | `true` 면 사람이 AI 제안을 보기 전에 붙인 라벨. **일치도는 blind 라벨로만 계산한다** |
| `codebook_version` | 라벨을 붙일 때의 코드북 버전 |
| `labeled_at` | 날짜 |

코드북이 바뀌면 영향받는 `field` 만 다시 라벨한다. 분기마다 20% 안팎을 최근 업무로 교체하고, 빠진 항목은 회귀 검증용으로 남긴다.

## 매니페스트 — `build/MANIFEST.json`

```json
{ "generation": "gen-2026-10", "built_at": "…", "codebook_version": 1, "model": "<모델 ID>",
  "recipes": { "stitch": "stitch@0", "annotate": "annotate@0", "cards": "cards@0" },
  "inputs": { "observations": "sha256:…", "overlays": "sha256:…" },
  "cost_usd": 0.0,
  "files": { "build/cards/ops-data-fix.md": "sha256:…" } }
```
빌드 레시피만 쓴다. `check_integrity.py` 가 커밋 전에 `files` 해시를 대조한다.

## 수신함 — `inbox/YYYYMMDD-HHMM-<주제>.md`

대화 세션이 남기는 제안(새 원형 후보, 코드북 문구, 함정 사례). 수확 단계가 주간 확인 질문(최대 5건)으로 올리고, 처리 결과는 오버레이·코드북 변경·반려 중 하나로 남긴다. 4주가 지나도 처리되지 않으면 그 주 수확 리포트에 만료로 적고 지운다(결정기록에는 남기지 않는다 — 결정이 아니므로).
