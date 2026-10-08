# 데이터 계약 — 관측 · 작업단위 · 오버레이 · 사용 기록 · 골든셋 · 매니페스트

> 정본은 이 문서다. 수집기·빌드 레시피는 여기에 맞춘다. 필드를 바꾸면 `DECISIONS.md` 한 줄과 함께 `schema_version` 을 올린다.
> LLM 주석 출력의 정본 스키마는 `annotation.schema.json` 이며 `codebook.yaml` 에서 생성된다.

schema_version: 0

## 저장 위치 요약

| 무엇 | 경로 | git |
|---|---|---|
| L0 관측 | `data/observations/YYYY-MM.jsonl` | 제외 (사내 저장 경로) |
| L1 작업단위·주석 | `data/units/current.jsonl`, 주석 캐시 `data/cache/` | 제외 |
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
  "labels": { "component": ["match"], "label": ["hotfix"], "epic": ["EP-9"] },
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
| `source.collector_kind` | `api · cli · mcp_sdk · llm_mediated`. `llm_mediated` 는 결정론 보장 밖이며 통계에서 분리 표시한다 |
| `kind` | `<system>.<event>` 형식. 예: `vcs.commit`, `vcs.mr.merged`, `chat.message`, `self_report.untracked_work`, `atlas.triage` |
| `actor_role` | `self`, codebook `roles` 의 id, `bot`, `unknown` 중 하나. **개인 식별자를 넣지 않는다** |
| `objects` | 이 사건이 닿는 모든 객체. 한 사건이 여러 객체에 연결될 수 있다(객체 중심) |
| `keys` | 상관에 쓰는 조인 키. 원문에서 **결정론적으로** 뽑은 것만 |
| `labels` | 원천 시스템에 이미 붙어 있는 분류(컴포넌트·라벨·에픽 등). 창발 원형과 비교하는 기준선 |
| `metrics` | 결정론 수치만 (변경 줄 수, 파일 수, 응답 시간 등) |
| `excerpt_ref` | 본문은 원천 시스템에 둔다. 보존 기한이 짧은 소스만 사내 캐시 참조를 둔다 |
| `partial` | 페이지네이션·권한으로 잘린 수집이면 `true` |
| 정정 | 잘못 수집한 사건은 고치지 않고 `kind: "correction"` 사건을 추가해 `objects` 에 대상 `obs_id` 를 건다 |

**금지**: LLM 요약·분류·추정값. L0에 해석이 들어가면 모든 재빌드가 그것을 상속한다.

### 사용 기록 — `source.system: "atlas"`

체계 자신의 사용도 관측이다. 대화 세션이 L0에 쓸 수 있는 **유일한 경로**이며, 반드시 `method/recipes/record_feedback.py` 를 거친다(append 전용).

트리아지 시점 — 예측을 남긴다:
```json
{ "kind": "atlas.triage", "objects": [ { "type": "issue", "id": "ABC-130" } ],
  "metrics": { "use": "U1", "codebook_version": 1, "generation": "gen-2026-11",
               "pred": { "archetype": "ops-data-fix", "archetype_conf": "medium",
                         "pitfalls": ["missing-rollback-script"], "scale": "small" } } }
```
피드백 — 도움 여부:
```json
{ "kind": "atlas.feedback", "objects": [ { "type": "issue", "id": "ABC-130" } ],
  "metrics": { "use": "U1", "verdict": "helpful" } }
```
`verdict`: `helpful · not_helpful · unknown`. 예측은 WU가 확정되면 수확 단계가 채점한다(적중률·서열 보정).

## L1 작업단위 — `data/units/current.jsonl`

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
| `settled` 는 열린 창이 지나면 `true`. 예측 채점·통계는 확정 WU만 쓴다 | 사후 결과가 다 들어오기 전에 판정하지 않는다 |
| 확률 연결은 `confidence` 서열과 `signals` 를 남긴다 | 상관 레시피를 고칠 때 근거가 된다 |
| 주석 캐시 키는 `(input_hash, annotate_recipe, codebook_version, model)` | 바뀐 것만 다시 코딩한다. 모델 교체도 드러난다 |
| 재빌드마다 무작위 15% 는 캐시를 무시하고 다시 주석한다 | 재주석 불일치율(주석 안정성) 측정 |
| `deterministic_signals` 는 L0 사실(재오픈·롤백·후속 수정·유발 결함)에서만 계산한다 | 카드에 실릴 함정의 자격 조건 |

**사후 검증**(`method/recipes/validate.py`, 스키마가 아니라 레시피가 집행):
- 값이 `unknown` 이 아니면 `evidence` 1개 이상.
- 모든 `evidence` 는 그 WU `members` 안의 `obs_id`.
- `confidence: high` 는 직접 근거 2개 이상 또는 결정론 신호가 있어야 한다.
- 위반한 값은 `unknown` 으로 강등하고 위반 건수를 지표로 남긴다.

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

대화 세션이 남기는 제안(새 원형 후보, 코드북 문구, 함정 사례). 수확 단계가 주간 확인 질문(최대 5건)으로 올리고, 처리 결과는 오버레이·코드북 변경·반려 중 하나로 남긴다. 4주가 지나도 처리되지 않으면 만료 반려로 결정기록에 한 줄 남기고 지운다.
