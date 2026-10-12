# 데이터 계약 — 관측 · 작업단위 · 모델 · 오버레이 · 사용 기록 · 골든셋 · 매니페스트

> 규칙과 이유의 정본은 이 문서다. **기계 계약**은 둘이다 — 관측 한 줄은 `observation.schema.json`(손으로 관리), LLM 주석 출력은 `annotation.schema.json`(`codebook.yaml` 에서 생성).
> 두 계약은 엔진(`atlas/common.py` 의 `check_observation`, `atlas/validate.py`)이 실행으로 집행하고, `tests/` 가 그 집행을 시험한다.
> 필드를 바꾸면 결정기록(사내 `DECISIONS.md`, 템플릿 `CHANGELOG.md`) 한 줄과 함께 `schema_version` 을 올린다.

schema_version: 1

## 저장 위치 요약

| 무엇 | 경로 | git |
|---|---|---|
| L0 관측 | `data/observations/YYYY-MM.jsonl` | 제외 (사내 저장 경로) |
| 발췌 캐시 | `data/excerpts.jsonl` — 수집기가 만들고 policy §2 보존 기한으로 지운다 | 제외 |
| 수집 증분 상태 · 훅 세션 기록 | `data/state/` | 제외 |
| L1 작업단위 | `data/units/stitched.jsonl`(상관) → `annotated.jsonl`(주석) → `current.jsonl`(사후 검증 통과본) · `stitch_report.json` · 주석 캐시 `data/cache/` | 제외 |
| L2 마이닝 모델 · 카탈로그 | `data/model/` (아래 "L2 모델") | 제외 (WU·커밋 단위 근거) |
| 업무 지도 | `data/reports/atlas-<세대>.html` | 제외 |
| WU 입력 문서 | `data/render/<wu>-<해시8>.md`, `data/render/index.json` | 제외 |
| 주간 수확 리포트 | `data/reports/` | 제외 |
| L2 모델·L3 카드 (원형 단위 집계) | `out/model/`, `out/cards/`, `out/MANIFEST.json` | 포함 |
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
| `source.collector_kind` | `api · cli · mcp_sdk · llm_mediated · human`. `llm_mediated` 는 결정론 보장 밖이며 통계에서 분리 표시한다. `human` 은 `atlas record` 로 남긴 사람·사용 사건 |
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
| 쓰기 | 수집기는 덧붙이기 전에 `check_observation` 으로 검증한다(`atlas collect`·`atlas ingest` 가 집행). 사람·사용 사건은 `atlas record` 만 쓴다 |

**금지**: LLM 요약·분류·추정값. L0에 해석이 들어가면 모든 재빌드가 그것을 상속한다.

### 제공 수집기가 내는 사건

| kind | obs_id | objects | keys | labels | metrics |
|---|---|---|---|---|---|
| `vcs.commit` · `vcs.merge` | `vcs:<저장소>:<sha>` | commit, mr | `issue_keys` · `paths` · `renames`(`옛 경로<탭>새 경로`) · `mr_ids`("<저장소>!<번호>") · `branches`(해시 키 `b:…`) · `reverts` · `commits`(머지가 들여온 커밋 — 동기화 머지는 없음) | — | `files` · `insertions` · `deletions` · `binary_files` · `parents` · `is_merge` · `sync_merge` · `is_revert` · `is_fix` · `author_lag_h` |
| `issue.created` | `issue:<트래커>:<키>#created` | issue | `issue_keys` · `parent` | **생성 당시** `type` · `priority` · `component` · `label` · `fix_version` | — |
| `issue.transition` | `…#h<이력 id>:<항목>` | issue | `issue_keys` | `status` · `status_from` | `to_done` · `reopen` |
| `issue.resolved` · `issue.reopened` | 〃 | issue | `issue_keys` | `resolution` · `resolution_from` | — |
| `issue.changed` | 〃 | issue | `issue_keys` | `field` + 그 필드의 **변경 뒤 전체 값** | `cleared` |
| `issue.linked` | 〃 | issue | `issue_keys` · `links`(`<관계 문구>\|<대상 키>`, 예: `is caused by\|ABC-1`) | — | — |
| `issue.comment` | `…#c<댓글 id>` | issue | `issue_keys` · `mentions` | — | `chars` |
| `ai_session.session` | `ai_session:<이름>:<세션 id>#<줄 수>` | ai_session | `paths`(저장소 기준) · `issue_keys` · `branches`(해시 키) | — | `prompts` · `turns` · `tool_calls` · `edits` · `reads` · `bash` · `test_runs` · `commits` · `tool_errors` · `web` · `subagents` · `compactions` · `duration_min` · `unparsed_lines` |

| 규칙 | 이유 |
|---|---|
| `ts` 는 커밋이면 커미터 시각, 이슈 사건이면 그 이력의 시각 | 시간 분할과 사후 귀속의 순서가 저장소에 들어온 때를 따른다 |
| 이슈 생성 사건의 라벨은 지금 값에서 변경 이력을 거꾸로 되감아 만든다. 이후 변경은 `issue.changed` 로 그 시각에 남긴다 | 지금 값을 생성 시각에 붙이면 시간 분할 평가에 미래가 샌다 |
| AI 세션은 이어 쓰이며 커진다. 줄 수가 obs_id 에 들어가 새 관측이 생기고, **읽는 쪽은 세션별 줄 수가 가장 큰 것 하나만** 쓴다 | 덧붙이기만 하면서 최신을 쓴다 |
| 이슈 키·결함 수정 판정처럼 설정에 기대는 값은 **빌드 때 지금 설정으로 다시 거른다**(키는 좁히기만, 결함 수정은 발췌가 있으면 다시 판정) | L0 는 고칠 수 없고 obs_id 로 중복을 거른다. 수집 때의 판정만 믿으면 규칙을 고쳐도 옛 값이 남는다 |
| 발췌(커밋 메시지·이슈 요약과 설명·댓글·첫 프롬프트)는 가린 뒤 자른다. 커밋 메시지의 이름 트레일러(Signed-off-by 등)는 뺀다. 소스마다 `excerpt = false` 로 끈다 | 1차 방어는 '필요한 만큼만'이다 |
| 사람은 `self`·`bot`·`unknown`(또는 코드북 roles)으로만 남는다. 이메일·이름·계정 id 는 판정에만 쓰고 저장하지 않는다 | 개인 식별자 금지 |
| 브랜치 이름은 정규화(원격 접두·`owner:` 제거)한 뒤 해시 키 `b:<12자>` 로만 남긴다. 메인라인은 남기지 않는다. `keep_branch_names = true` 면 이름 그대로 | 이름에 계정·별칭이 들어가는 관례가 흔하다. 머지와 AI 세션이 같은 규칙으로 해시하므로 조인은 된다 |
| 동기화 머지는 `sync_merge: true` 로 표시하고 `commits` 를 남기지 않는다. 상관도 이 머지의 커밋을 작업으로 세지 않는다. 판정은 두 겹이다. **메시지**: 들여온 브랜치가 메인라인(`master·main·develop·dev·trunk·release…`, 원격 접두만 허용)이다 — GitHub PR 은 받는 쪽이 메시지에 없으므로 소유자가 이 저장소의 원격 소유자(소스 설정 `owner`, 없으면 `upstream`·ref 의 원격·`origin` 순)일 때만이고, 같은 이름 브랜치로 들인 MR(포크의 main → main)은 작업이며, 커밋·태그를 메인라인이 아닌 브랜치로 들인 머지도 MR 이 아니면 동기화다. **구조**: 둘째 부모가 수집하는 ref(기본: 원격 기본 브랜치)의 첫 부모 사슬 위에 있다. 둘 다 아니면 작업이다 | 메인라인의 남의 커밋이 그 이슈의 작업단위로 들어가지 않게. 메시지 형식은 호스팅마다 달라 구조 판정이 형식과 무관하게 받친다. 모를 때는 진짜 작업을 버리지 않는 쪽으로 |

### 사용 기록 — `source.system: "atlas"`

체계 자신의 사용과 사람의 자기 보고도 관측이다. 대화 세션이 L0에 쓸 수 있는 **유일한 경로**이며, 반드시 `atlas record` 를 거친다(append 전용). 이 레시피는 예측 값이 코드북의 활성 id 인지 확인하고, 아니면 쓰지 않는다.

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
  "span": { "start": "…", "end": "…", "end_basis": "issue", "open_window_until": "…", "settled": false },
  "members": [
    { "obs_id": "…", "link": "key", "confidence": "high" },
    { "obs_id": "…", "link": "probabilistic", "confidence": "low", "signals": ["time_window", "actor", "text_sim"] }
  ],
  "sources": ["issue", "vcs", "chat"],
  "annotation": { "...": "annotation.schema.json 형식" },
  "stats": {
    "duration_h": 0, "n_changes": 0, "n_files": 0, "insertions": 0, "deletions": 0, "roles": [],
    "labels": { "type": ["Bug"], "component": ["billing"] }, "resolution": "Fixed", "is_fix": true, "doc_like": false,
    "reopen_count": 0, "late_reopens": 0, "open_now": false, "followup_fix_wus": [], "induced_by_wus": [],
    "followup_suspected_wus": [], "suspected_induced_by_wus": [],
    "deterministic_signals": ["reopened", "rolled_back"],
    "outcome": "rolled_back", "outcome_final": true,
    "ai_sessions": 1, "ai_edits": 4, "ai_test_runs": 2
  },
  "build": { "stitch_recipe": "stitch@1", "as_of": "…", "annotate_recipe": "annotate@0", "codebook_version": 0,
             "model": "<모델 ID>", "input_hash": "sha256:…" }
}
```

| 규칙 | 이유 |
|---|---|
| `wu_id` 는 **앵커 객체**(가장 먼저 생긴 이슈·MR·스레드)에서 결정론적으로 만든다 | 재빌드해도 ID가 유지돼야 링크가 안 깨진다 |
| `settled` 는 열린 창이 지나면 `true`. 통계와 사후 항목 채점은 확정 WU만 쓴다 | 사후 결과가 다 들어오기 전에 판정하지 않는다 |
| 확정되면 이슈 상태(종료 시각·재오픈 수)를 얼린다. 창 뒤의 재오픈·재해결은 `late_reopens` 로 세기만 하고, 라벨 변경은 계속 반영한다. 지금 열려 있는지는 얼림과 무관하게 `open_now` 로 따로 남긴다 | 같은 WU 의 확정 결과가 나중 사건으로 뒤집히지 않게. 그러면서도 맥락은 "확정 뒤 다시 열려 지금 진행 중"을 말해야 한다 |
| `source.system` 이 `atlas` 인 사건은 `members` 에 넣지 않는다 | 예측 누수 차단 |
| 확률 연결은 `confidence` 서열과 `signals` 를 남긴다 | 상관 레시피를 고칠 때 근거가 된다 |
| 주석 캐시 키는 `(input_hash, annotate_recipe, codebook_version, model)` | 바뀐 것만 다시 코딩한다. 모델 교체도 드러난다 |
| 재빌드마다 무작위 15% 는 캐시를 무시하고 다시 주석한다 | 재주석 불일치율(주석 안정성) 측정 |
| `deterministic_signals` 는 L0 사실에서만, **열린 창(종료 + open_window_days) 안의 사건으로만** 계산한다: `reopened`(재오픈 사건) · `rolled_back`(WU 밖이나 종료 뒤의 되돌림) · `followup_fix`(사람이 남긴 caused by·broken by 연결 — 시각은 연결이 처음 관측된 때와 후속 이슈 시작 중 늦은 쪽) | 카드에 실릴 함정의 자격 조건. 확정 뒤에 만든 연결로 확정 결과가 바뀌지 않는다 |
| `followup_fix_wus`·`induced_by_wus` 는 창과 무관하게 연결된 모든 쌍(사실)이다. 두 이슈 양쪽에 남은 같은 연결은 한 쌍으로 센다 | 늦게 발견된 회귀도 지식이다. 결과에만 쓰지 않는다 |
| 파일 수준 SZZ 근사는 `followup_suspected` 로 따로 두고 결과·카드에 쓰지 않는다 | 실데이터 표본 정밀도 약 25% (EVALUATION.md) |
| `outcome` 은 코드북의 computed 패싯이다: rolled_back > followup_fix > reopened > abandoned > clean. 끝나지 않았으면 null, 확정 전이면 `outcome_final: false` | LLM 이 결과를 코딩하지 않는다 |
| `end_basis`: 이슈 WU 는 트래커의 해결 시각(`issue`), 그 밖은 마지막 활동(`last_activity`). 사후 비율은 `issue` 인 WU 만 센다 | 재오픈을 관측할 수 없는 WU 에서 '문제 없음'과 '관측 못 함'이 섞이지 않게 |
| `labels` 는 기준 시각(as_of)까지의 변경을 재생한 값 | 시간 분할에서도 그 시점의 라벨이다 |

## WU 입력 문서 — `atlas render` (주석·재도출의 유일한 입력)

LLM이 읽는 것은 이 문서뿐이다. 같은 WU면 언제 만들어도 같은 바이트가 나와야 한다.

| 항목 | 규칙 |
|---|---|
| 머리 | 앵커, 기간, 소스 목록, 기존 라벨, 결정론 신호, 기술 통계 |
| 본문 | 구성 관측을 시각순으로: 시각 · `system.kind` · 행위자 역할 · 조인 키 · 발췌 |
| 발췌 | 원천에서 빌드 때 조회. 비밀값 마스킹, 사람 이름은 역할로 치환. 사건당·문서당 길이 상한(policy) |
| 제외 | 주석(`annotation`), 빌드 정보(`build`), `atlas` 사건, 오버레이 — 현재 분류가 블라인드 도출에 새지 않게 |
| 해시 | `input_hash` = 이 문서의 sha256. 주석 캐시 키의 첫 요소 |

기준 구현: `atlas/render.py`. **1차 방어는 수집기다** — 필요한 만큼만 발췌해 `data/excerpts.jsonl`(`{"obs_id", "text"}`)에 두고, 사람 이름은 역할로, 비밀값은 지운다. 렌더러의 마스킹(비밀값·토큰·쿠키·이메일·전화·주민/외국인 등록번호)은 수집기가 놓친 것을 잡는 안전망이며 완전하지 않다. 발췌를 사건당 상한의 4배로 먼저 자른 뒤 가리고, 모든 패턴의 반복에 상한을 둬서 긴 한 줄에서도 시간이 선형으로 는다.

## 사후 검증 — `atlas validate` (스키마가 아니라 엔진이 집행)

| 규칙 | 내용 | 위반 시 |
|---|---|---|
| R1 근거 밖 | `evidence` 는 그 WU `members` 의 `obs_id` 여야 한다. 중복 근거는 하나로 센다 | 밖의 근거를 지운다 |
| R2 근거 없음 | `unknown` 이 아닌 분류 값(패싯·원형·어휘)은 근거 1개 이상 | 패싯·원형은 `unknown`, 어휘 항목은 뺀다. 서술(제목·요약)은 값을 두고 신뢰도를 `low` 로 |
| R3 과신 | `confidence: high` 는 서로 다른 근거 2개 이상 | `medium`(근거 1개)·`low`(0개)로 낮춘다 |
| R4 계약 밖 | 코드북에 없거나 휴면인 id, 이번 트랙에서 주석하지 않는 패싯(결정론 계산 패싯 포함) | `unknown` 으로 강등하거나 뺀다 |

입력은 바꾸지 않고 검증된 사본과 집계를 낸다. `violation_rate` 는 **위반이 하나라도 걸린 값 / 검사한 값**(규칙이 여러 개 걸려도 값 하나로 센다)이고, 규칙·필드별 건수와 코드북 버전이 다른 WU 수는 따로 낸다. 위반률은 policy §7 의 지표다.

## L2 모델 — `data/model/` (`atlas mine` · `atlas catalog`)

모두 파생물이다. 지우고 `atlas build` 하면 같은 입력에서 같은 세대가 나온다.

| 파일 | 내용 |
|---|---|
| `claims.jsonl` | 주장 전부 — 아래 형식 |
| `paths.json` | `{format, generation, scopes: {<저장소>: {entries: {<경로>: {hot, with}}, files: {<경로>: 변경 수}}}}` — 훅이 편집 순간 읽는다 |
| `text_index.json` | `{format, generation, idf, vecs, meta}` — 비슷한 과거 작업 검색. `meta` 는 WU 별 앵커·제목·결과·소요·바꾼 파일 |
| `generation.json` | 세대 id(관측 id·설정·레시피 버전의 해시 — 레시피의 의미가 바뀌면 버전을 올린다) · 이전 세대 · as_of · 입력 수 · 주장 수(종류×강도) · 세대 변화(drift) · `format` |
| `catalog.json` | 소스·상관·결과·주장·AI 세션·사용의 메타와 경고(권하는 조치 포함) |
| `build.json` | 마지막 빌드의 요약과 단계별 시간 |
| `history/<세대>.jsonl` | 이전 세대 주장의 요약(id·강도·근거 수). `mining.history_generations` 개까지 |

```json
{ "claim_id": "cochange:server:src/pay/Reward.java=>src/pay/RewardTest.java",
  "kind": "cochange", "scope": "server",
  "subject": { "type": "path", "id": "src/pay/Reward.java" },
  "object":  { "type": "path", "id": "src/pay/RewardTest.java" },
  "stat": { "support": 12, "antecedent": 18, "confidence": 0.667, "lift": 9.1, "level": "commit" },
  "strength": "high", "evidence": ["vcs:server:…"], "n_evidence": 12,
  "first_ts": "…", "last_ts": "…", "recipe": "cochange@1" }
```

| 규칙 | 이유 |
|---|---|
| `claim_id` 는 결정론이다(종류·범위·주어·목적어) | 세대 간 같은 주장을 비교한다(drift) |
| `strength` 는 숫자가 아니라 종류별로 정의된 서열이다(각 마이너 모듈 문서) | 보정되지 않은 숫자를 문턱에 쓰지 않는다(METHODOLOGY.md §5.5) |
| 주장은 언제나 L0·L1 에서 다시 센다. 주장을 입력으로 주장을 만들지 않는다 | 요약의 요약 금지(N3) |
| 파일 형식을 바꾸면 `MODEL_FORMAT`(atlas/mine)을 올린다. 소비 쪽은 형식이 다르면 읽지 않고 '다시 빌드'라고 알린다 | 옛 형식을 조용히 잘못 읽지 않게 |

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

## 매니페스트 — `out/MANIFEST.json`

```json
{ "generation": "gen-2026-10", "built_at": "…", "codebook_version": 1, "model": "<모델 ID>",
  "recipes": { "stitch": "stitch@0", "annotate": "annotate@0", "cards": "cards@0" },
  "inputs": { "observations": "sha256:…", "overlays": "sha256:…" },
  "cost_usd": 0.0,
  "files": { "out/cards/ops-data-fix.md": "sha256:…" } }
```
빌드 레시피만 쓴다. `atlas check` 가 커밋 전에 `files` 해시를 대조한다.

## 수신함 — `inbox/YYYYMMDD-HHMM-<주제>.md`

대화 세션이 남기는 제안(새 원형 후보, 코드북 문구, 함정 사례). 수확 단계가 주간 확인 질문(최대 5건)으로 올리고, 처리 결과는 오버레이·코드북 변경·반려 중 하나로 남긴다. 4주가 지나도 처리되지 않으면 그 주 수확 리포트에 만료로 적고 지운다(결정기록에는 남기지 않는다 — 결정이 아니므로).
