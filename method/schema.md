# 데이터 계약 — 관측 · 작업단위 · 모델 · 업무 카테고리 · LLM 출력 · 쓰임 기록 · 수확 상태 · 오버레이 · 사용 기록 · 골든셋 · 매니페스트

> 규칙과 이유의 정본은 이 문서다. **기계 계약**은 둘이다 — 관측 한 줄은 `observation.schema.json`(손으로 관리), LLM 주석 출력은 `annotation.schema.json`(`codebook.yaml` 에서 생성).
> 두 계약은 엔진(`atlas/common.py` 의 `check_observation`, `atlas/validate.py`)이 실행으로 집행하고, `tests/` 가 그 집행을 시험한다.
> 필드를 바꾸면 결정기록(사내 `DECISIONS.md`, 템플릿 `CHANGELOG.md`) 한 줄과 함께 `schema_version` 을 올린다.

schema_version: 1

## 저장 위치 요약

| 무엇 | 경로 | git |
|---|---|---|
| L0 관측 | `data/observations/YYYY-MM.jsonl` | 제외 (사내 저장 경로) |
| 발췌 캐시 | `data/excerpts.jsonl` — 수집기가 만들고 `atlas harvest` 가 `[retention]` 기한(policy §2)으로 지운다 | 제외 |
| 수집 증분 상태 · 훅 세션 기록 · 수확 상태 | `data/state/` — 수확은 `harvest-last.json` · `harvest.log`(1MB 넘으면 `.log.1` 하나만 남긴다) · `harvest.lock` (아래 "수확 상태") | 제외 |
| 쓰임 기록 | `data/usage/<연-월>.jsonl` (아래 "쓰임 기록") | 제외 |
| L1 작업단위 | `data/units/stitched.jsonl`(상관) → `annotated.jsonl`(주석) → `current.jsonl`(사후 검증 통과본) · `stitch_report.json` · 주석 응답 캐시 `data/llm/cache/annotate/` | 제외 |
| L2 마이닝 모델 · 카탈로그 | `data/model/` (아래 "L2 모델") | 제외 (WU·커밋 단위 근거) |
| 업무 지도 · 업무 안내서 | `data/reports/atlas-<세대>.html` · `data/reports/guides/` (카테고리별 Markdown + Mermaid, `README.md` 가 색인) | 제외 |
| 업무 카테고리 정의 | `method/categories.yaml` (아래 "업무 카테고리") | 포함 (템플릿에서는 사람이 채우는 칸이 비어 있다) |
| LLM 캐시 · 장부 · 해설 | `data/llm/` (아래 "LLM 출력") · 주간 요약 `data/reports/<연>-W<주>-digest.md` | 제외 |
| WU 입력 문서 | `data/render/<wu>-<해시8>.md`, `data/render/index.json` | 제외 |
| 주간 수확 리포트 · LLM 실측 리포트 | `data/reports/<날짜>-harvest.md` · `data/reports/llm-bench-<시각>.md` | 제외 |
| 작업 폴더의 맥락 파일 · 작업 기록 초안 | 사람의 작업 폴더 `CONTEXT.md`(`atlas work new · context`) · `WORK.draft.md`(`atlas llm run work_draft --write` — 없을 때만 쓴다. 사람이 골라 `WORK.md` 로 옮긴다). 첫 줄에 생성물 표시가 있으면 수집하지 않는다 | 사람의 폴더 |
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
| `source.system` | `issue · vcs · ci_cd · deploy · wiki · mail · chat · calendar · monitor · ai_session · workspace · self_report · atlas`. 늘릴 땐 이 표를 먼저 고친다 |
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
| `ai_session.session` | `ai_session:<이름>:<세션 id>#<줄 수>` | ai_session | `paths`(편집한 파일이 든 저장소 기준) · `issue_keys` · `branches`(해시 키) · `works`(세션을 연 작업 폴더) | — | `prompts` · `turns` · `tool_calls` · `edits` · `reads` · `bash` · `test_runs` · `commits` · `tool_errors` · `web` · `subagents` · `compactions` · `duration_min` · `unparsed_lines` · `work_edits` |
| `workspace.snapshot` | `workspace:<이름>:<작업 id>#<상태 해시 12자>` | work (`id` = `<이름>:<작업 id>`, `url` = 루트 기준 경로) | `issue_keys`(front-matter 의 key, 비었으면 폴더 이름) · `works` | front-matter 의 `status` · `started` · `closed` · `repos` · `kind` · `tags` | `files` · `bytes` · 역할별 개수(`record` · `instructions` · `plan` · `decision` · `evidence` · `output` · `other`) · `sensitive` · `repos_inside` · `has_problem` · `has_process` · `has_result` · `has_retro` · `record_chars` · `record_error` |

| 규칙 | 이유 |
|---|---|
| `ts` 는 커밋이면 커미터 시각, 이슈 사건이면 그 이력의 시각 | 시간 분할과 사후 귀속의 순서가 저장소에 들어온 때를 따른다 |
| 이슈 생성 사건의 라벨은 지금 값에서 변경 이력을 거꾸로 되감아 만든다. 이후 변경은 `issue.changed` 로 그 시각에 남긴다 | 지금 값을 생성 시각에 붙이면 시간 분할 평가에 미래가 샌다 |
| AI 세션은 이어 쓰이며 커진다. 줄 수가 obs_id 에 들어가 새 관측이 생기고, **읽는 쪽은 세션별 줄 수가 가장 큰 것 하나만** 쓴다 | 덧붙이기만 하면서 최신을 쓴다 |
| 이슈 키·결함 수정 판정처럼 설정에 기대는 값은 **빌드 때 지금 설정으로 다시 거른다**(키는 좁히기만, 결함 수정은 발췌가 있으면 다시 판정) | L0 는 고칠 수 없고 obs_id 로 중복을 거른다. 수집 때의 판정만 믿으면 규칙을 고쳐도 옛 값이 남는다 |
| 발췌(커밋 메시지·이슈 요약과 설명·댓글·첫 프롬프트·작업 기록)는 가린 뒤 자른다. 커밋 메시지의 이름 트레일러(Signed-off-by 등)는 뺀다. 홈 폴더 경로의 계정 이름(`/Users/<계정>` · `/home/<계정>` · `C:\Users\<계정>`)은 `~` 로 바꾼다. 소스마다 `excerpt = false` 로 끈다 | 1차 방어는 '필요한 만큼만'이다. 기계적으로 지울 수 있는 이름은 지운다 |
| 사람은 `self`·`bot`·`unknown`(또는 코드북 roles)으로만 남는다. 이메일·이름·계정 id 는 판정에만 쓰고 저장하지 않는다 | 개인 식별자 금지 |
| 브랜치 이름은 정규화(원격 접두·`owner:` 제거)한 뒤 해시 키 `b:<12자>` 로만 남긴다. 메인라인은 남기지 않는다. `keep_branch_names = true` 면 이름 그대로 | 이름에 계정·별칭이 들어가는 관례가 흔하다. 머지와 AI 세션이 같은 규칙으로 해시하므로 조인은 된다 |
| 작업 폴더는 내용이 바뀔 때마다 다시 관측된다. obs_id 끝은 상태(키·라벨·수치·발췌·파일 목록과 크기)의 해시라 같은 상태는 다시 들어가지 않는다. 예전 상태로 되돌아가면 `#<해시>~<직전 obs_id 의 해시 6자>` 로 새로 남긴다. **읽는 쪽은 작업 폴더별로 가장 나중에 수집한 것 하나만** 쓰고, as_of 로 다시 계산할 때는 그때까지 수집한 것만 본다. `ts` 는 관측한 파일의 마지막 수정 시각이고 수집 시각을 넘지 않는다 | 원천(폴더)이 살아 있어 언제든 다시 찍을 수 있다. 수정 시각만 바뀐 것은 새 상태가 아니다. 파일을 지우면 수정 시각이 거꾸로 가고, 옮겨 온 파일은 수정 시각이 옛날이다 — 그래서 최신과 보이는 범위는 수집 시각으로 정한다 |
| 작업 폴더의 키는 front-matter 의 `key`(`keys`)이고, 비었을 때만 폴더 이름에서 뽑는다. 본문의 키는 쓰지 않는다. 작업 id 는 front-matter 의 `id`, 없으면 루트 기준 경로다. id 가 여러 폴더에서 겹치면 그 이름의 폴더 하나만 id 를 쓰고 나머지는 경로를 쓴다. Claude Code 수집기는 세션을 연 폴더에서 같은 규칙(`workspace.work_ids`)으로 id 를 만들어 `works` 에 남긴다 | 본문은 참고로 다른 이슈를 자주 언급한다. 폴더 이름이 바뀌어도 id 가 같아야 세션·작업단위가 이어진다. 복사한 폴더가 원래 일과 합쳐지지 않게 |
| 작업 폴더에서 내용을 읽는 파일은 작업 기록(제목·문제·결과·회고·처리)과 첫 결정 기록(본문)의 앞부분뿐이다. 나머지는 이름으로 역할을 세기만 한다. 관측하지 않는 것: 첫 줄에 생성물 표시가 있는 `CONTEXT.md` · `scratch/`·`tmp/`·도구 폴더 · 저장소 사본(`.git` 이 있는 폴더 — `repos_inside` 로 셈. 작업 폴더 자체가 워크트리면 맨 위 파일과 역할 폴더만 센다) · 비밀값으로 보이는 파일(열지 않고 `sensitive` 로 셈). 경로는 루트 기준으로만 남기고 루트 자체는 남기지 않는다. front-matter 값의 절대·홈 경로는 마지막 이름만 남긴다 | 요약의 요약 금지(N3) · 필요한 만큼만 · 루트·홈 경로에는 계정 이름이 들어간다 |
| AI 세션의 편집 파일: 저장소에서 연 세션은 그 저장소 루트 기준이고 밖의 파일은 버린다. 저장소가 아닌 폴더(작업 폴더)에서 연 세션은 파일마다 그 파일이 든 저장소의 루트 기준이다. 작업 공간 안의 파일은 `paths` 에 넣지 않고 `work_edits` 로 센다 — 작업 폴더 안에 받아 둔 저장소 사본의 파일은 예외로 코드다 | 세션 경로에는 저장소 구분이 없어 남의 저장소 파일이 엉뚱한 커밋과 겹친다. 작업 폴더에서 연 세션은 `--add-dir` 로 고친 저장소 파일을 잃지 않게. 작업 기록이 코드 경로로 섞이지 않게 |
| 동기화 머지는 `sync_merge: true` 로 표시하고 `commits` 를 남기지 않는다. 상관도 이 머지의 커밋을 작업으로 세지 않는다. 판정은 두 겹이다. **메시지**: 들여온 브랜치가 메인라인(`master·main·develop·dev·trunk·release…`, 원격 접두만 허용)이다 — GitHub PR 은 받는 쪽이 메시지에 없으므로 소유자가 이 저장소의 원격 소유자(소스 설정 `owner`, 없으면 `upstream`·ref 의 원격·`origin` 순)일 때만이고, 같은 이름 브랜치로 들인 MR(포크의 main → main)은 작업이며, 커밋·태그를 메인라인이 아닌 브랜치로 들인 머지도 MR 이 아니면 동기화다. **구조**: 둘째 부모가 수집하는 ref(기본: 원격 기본 브랜치)의 첫 부모 사슬 위에 있다. 둘 다 아니면 작업이다 | 메인라인의 남의 커밋이 그 이슈의 작업단위로 들어가지 않게. 메시지 형식은 호스팅마다 달라 구조 판정이 형식과 무관하게 받친다. 모를 때는 진짜 작업을 버리지 않는 쪽으로 |

### 사내 수집기가 내는 사건

엔진이 수집기를 갖지 않은 소스(메신저 · 메일 · 위키 · 배포 · 리뷰 · 캘린더)는 사내에서 수집기를 만들어 JSONL 로 내고 `atlas ingest` 로 넣는다.
`atlas.collect.sdk.Kit` 이 아래 규칙을 지켜 주고, 골격은 `method/recipes/collector_template.py`(내보낸 JSON → `chat.message`)다.
`atlas ingest … --dry-run` 이 계약 위반과 함께 연결 미리보기(아는 이슈 키 · 단서 없음 비율)를 낸다.

| 칸 | 규칙 | 이유 |
|---|---|---|
| `kind` | `<system>.<사건>` — `system` 은 위 목록에서(예: `chat.message` · `wiki.page_edit` · `mail.message` · `deploy.release` · `review.comment`) | 접두와 `source.system` 이 같아야 한다 |
| `obs_id` | `<system>:<인스턴스 이름 해시 6자>:<부분들의 해시 16자>` — 같은 사건은 늘 같은 id | 다시 돌려도 중복이 생기지 않는다. 사내 시스템 이름 · 채널 이름이 id 에 드러나지 않게 |
| `objects` | 그릇(스레드 · 채널 · 페이지 · 메일 스레드)의 id 는 해시(`type` 은 그대로, `url` 은 뺀다). 엔진이 다른 소스와 잇는 객체 `issue · commit · mr · obs · ai_session · work` 는 그대로 | 그릇 이름은 사람 이름을 품기 쉽다(DM · 개인 채널). 같은 값은 같은 해시라 스레드로 묶인다 |
| 해시 키 | 기본은 인스턴스 이름을 소금으로 친 SHA-256 — 눈으로 읽히지 않을 뿐 후보를 넣어 보면 되짚을 수 있다. 환경 변수 `ATLAS_HASH_KEY` 가 있으면 HMAC(되짚을 수 없다). 한 번 정하면 바꾸지 않는다 | 바꾸면 모든 id 가 바뀌어 같은 사건이 두 번 들어온다 |
| `keys.issue_keys` | 본문에서 `[keys]` 규칙으로 결정론으로 뽑는다(수집기가 아는 키를 더할 수 있다) | 다른 소스와 이어지는 유일한 다리 — 키가 없으면 고아 관측이 된다 |
| `actor_role` | 식별자(이메일 · 계정)는 `[identity]` 판정에만 쓰고 남기지 않는다 → `self · bot · <역할> · unknown` | 개인 식별자 금지 |
| 본문 | 관측에는 넣지 않는다. `text` 는 발췌 캐시로만 가고 자르고 가린다(비밀값 · 연락처 · 홈 경로) | 1차 방어는 '필요한 만큼만' |

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
LLM 출력 판정(`atlas record llm-feedback`) — 지금 보이는 출력 하나에 대한 사람의 판정:
```json
{ "kind": "atlas.llm_feedback", "objects": [ { "type": "llm_output", "id": "category_card:server / Bug" } ],
  "payload": { "task": "category_card", "key": "server / Bug", "digest": "<출력 내용 해시>", "verdict": "wrong", "note": "재오픈은 이 칸에서 드물지 않다" } }
```

| 규칙 | 이유 |
|---|---|
| `archetype_ok`(`yes · no · unsure`)가 원형 예측의 **1차 정답**이다 | 나중의 LLM 주석을 정답으로 쓰면 LLM이 LLM을 채점하게 된다. LLM 주석과의 일치는 '일관성'으로 따로 보고한다 |
| 원형·규모는 WU **종료 시 잠정 채점**, 함정·처리 수는 **확정 시 채점** | 열린 창(30일)을 기다리면 판정이 몇 주 밀린다. 사후 결과가 필요한 항목만 기다린다 |
| 판정(`atlas.feedback`)은 같은 앵커의 트리아지가 먼저 있어야 쓴다. 채점은 트리아지 **이후**의 판정만 쓴다 | 채점할 예측이 없는 판정은 지표를 흐린다 |
| `unknown`·`residual` 원형 예측은 "보류"로 따로 센다 | 고른 것이 없으니 맞혀도 적중이 아니다 |
| `atlas` 사건은 작업단위의 `members` 와 주석 입력에서 **제외**한다(채점 전용) | 예측이 자기 정답지로 새어 들어가지 않게 |
| `verdict`: `helpful · not_helpful · unknown` | 보조 지표 (새로움 편향이 있다) |
| LLM 판정의 `verdict`: `useful · wrong · not_useful`. 판정은 출력 내용(`digest`)에 붙는다 — 같은 대상의 마지막 판정이 이기고, 다시 받은 답(내용이 다름)은 판정을 물려받지 않는다. `wrong` 인 출력은 채널에 다시 싣지 않는다. `note` 는 200자까지(사람 이름 금지) | 사람이 틀렸다고 한 문장이 다음 빌드에 되살아나지 않게. 고친 답은 새로 판정받는다 |

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
    "ai_sessions": 1, "ai_edits": 4, "ai_test_runs": 2, "works": ["ws:ABC-123-session-drop"]
  },
  "build": { "stitch_recipe": "stitch@1", "as_of": "…", "annotate_recipe": "annotate@0", "codebook_version": 0,
             "model": "<모델 ID>", "input_hash": "sha256:…" }
}
```

| 규칙 | 이유 |
|---|---|
| `wu_id` 는 **앵커 객체**(가장 먼저 생긴 이슈·MR·스레드, 키 없는 작업 폴더 `work:<이름>:<작업 id>`)에서 결정론적으로 만든다 | 재빌드해도 ID가 유지돼야 링크가 안 깨진다 |
| 작업 폴더(최신 스냅숏)는 키의 이슈 WU 에(키가 여럿이면 각각), 키가 없으면 자기 `work:` WU 에 든다. `works` 가 있는 AI 세션은 그 작업 폴더가 속한 WU 에 든다(`work_folder`, 프롬프트의 키 연결과 함께 — 다대다). 키 없는 작업 폴더의 세션은 경로 겹침으로 커밋도 찾고, 아무 데도 안 붙은 커밋은 그 `work:` WU 에 들인다. `stats.works` 는 그 WU 의 작업 객체 id 목록이다 | 작업 폴더에서 연 세션이 고아로 남지 않게. 티켓 없이 한 일도 작업단위가 되고 그 일의 커밋을 가진다 |
| 트래커 상태가 없고 상태를 적은 작업 기록이 있으면 기간·상태는 작업 기록을 따른다(`end_basis: work`): 시작 = `started`(그날 0시), 끝 = `status` 가 `done`·`dropped` 일 때 `closed`(그날 끝, 없으면 스냅숏 시각), `dropped` 는 `abandoned`. 트래커가 있으면 트래커가 정본이다 | 필드별 신뢰 소스(METHODOLOGY.md §10). 상태를 적지 않은 폴더는 다른 WU 처럼 마지막 활동으로 끝을 잡는다 |
| `settled` 는 열린 창이 지나면 `true`. 통계와 사후 항목 채점은 확정 WU만 쓴다 | 사후 결과가 다 들어오기 전에 판정하지 않는다 |
| 확정되면 이슈 상태(종료 시각·재오픈 수)를 얼린다. 창 뒤의 재오픈·재해결은 `late_reopens` 로 세기만 하고, 라벨 변경은 계속 반영한다. 지금 열려 있는지는 얼림과 무관하게 `open_now` 로 따로 남긴다 | 같은 WU 의 확정 결과가 나중 사건으로 뒤집히지 않게. 그러면서도 맥락은 "확정 뒤 다시 열려 지금 진행 중"을 말해야 한다 |
| `source.system` 이 `atlas` 인 사건은 `members` 에 넣지 않는다 | 예측 누수 차단 |
| 확률 연결은 `confidence` 서열과 `signals` 를 남긴다 | 상관 레시피를 고칠 때 근거가 된다 |
| 다시 주석할지는 `(input_hash, annotate_recipe, codebook_version, 코드북 내용 해시, 프롬프트 해시, 설정한 모델)` 로 정한다(`build` 에 남는다) | 바뀐 것만 다시 코딩한다. 모델 교체도 드러난다 |
| 재빌드마다 주석한 WU 의 15% 를 캐시 없이 다시 받아 비교한다(`atlas llm stability annotate --share 0.15` — 세대마다 바뀌는 결정론 표본) | 재주석 불일치율(주석 안정성) 측정 |
| `deterministic_signals` 는 L0 사실에서만, **열린 창(종료 + open_window_days) 안의 사건으로만** 계산한다: `reopened`(재오픈 사건) · `rolled_back`(WU 밖이나 종료 뒤의 되돌림) · `followup_fix`(사람이 남긴 caused by·broken by 연결 — 시각은 연결이 처음 관측된 때와 후속 이슈 시작 중 늦은 쪽) | 카드에 실릴 함정의 자격 조건. 확정 뒤에 만든 연결로 확정 결과가 바뀌지 않는다 |
| `followup_fix_wus`·`induced_by_wus` 는 창과 무관하게 연결된 모든 쌍(사실)이다. 두 이슈 양쪽에 남은 같은 연결은 한 쌍으로 센다 | 늦게 발견된 회귀도 지식이다. 결과에만 쓰지 않는다 |
| 파일 수준 SZZ 근사는 `followup_suspected` 로 따로 두고 결과·카드에 쓰지 않는다 | 실데이터 표본 정밀도 약 25% (EVALUATION.md) |
| `outcome` 은 코드북의 computed 패싯이다: rolled_back > followup_fix > reopened > abandoned > clean. 끝나지 않았으면 null, 확정 전이면 `outcome_final: false` | LLM 이 결과를 코딩하지 않는다 |
| `end_basis`: 이슈 WU 는 트래커의 해결 시각(`issue`), 트래커 없이 상태를 적은 작업 기록이 있으면 그 기록(`work`), 그 밖은 마지막 활동(`last_activity`). 사후 비율은 `issue` 인 WU 만 센다 | 재오픈을 관측할 수 없는 WU 에서 '문제 없음'과 '관측 못 함'이 섞이지 않게 |
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
| `text_index.json` | `{format, generation, idf, vecs, meta}` — 비슷한 과거 작업 검색. 문서 = 이슈 요약 → 작업 기록 → 커밋·프롬프트 발췌. `meta` 는 WU 별 앵커·제목·결과·소요·바꾼 파일·작업 기록 위치(`works`: 소스 이름·루트 기준 경로·상태 — 절대 경로는 소비가 설정의 루트로 붙인다) |
| `generation.json` | 세대 id(관측 id·설정·레시피 버전의 해시 — 레시피의 의미가 바뀌면 버전을 올린다) · 이전 세대 · as_of · 입력 수 · 주장 수(종류×강도) · 세대 변화(drift) · `format` |
| `catalog.json` | 소스·상관·결과·주장·AI 세션·작업 폴더·업무 카테고리·LLM·사용의 메타와 경고(권하는 조치 포함). 경고의 `area` 는 `data`(맥락 첫 줄에 나온다) · `workspace`(작업 폴더 위생 — 카탈로그·`atlas work status` 에만) · `category` · `llm`(카탈로그에만) |
| `categories.json` | 업무 카테고리 — 아래 "업무 카테고리" |
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

## 업무 카테고리 — `method/categories.yaml` → `data/model/categories.json`

정의(사람)와 계산(엔진)을 나눈다. 뜻과 읽는 법은 `CHANNELS.md` §2.

**정의** `method/categories.yaml`:

| 키 | 규칙 |
|---|---|
| `version` | 정의 형식 버전 |
| `area` | `label` · `from`(값을 읽는 원천 — 트래커 라벨 이름 · `path` · `work_kind` · `work_tags` · `archetype`, 앞에서부터 처음 값이 있는 것) · `split`(값 안의 상하위 구분 정규식) |
| `facets` | 영역을 다시 나누는 축 목록 — `id` · `label` · `from` · `merge`. 첫 축이 기본 칸이다 |
| `fold_case` | 대소문자만 다른 영역 값을 가장 흔한 표기로 모은다 |
| `paths` · `merge` · `parents` · `names` | 경로 → 영역 · 값 통일 · 상위 · 표시 이름 (사람이 채우는 칸) |
| `contacts` | 카테고리 키 → `[{team, channel, role, when, note}]`. **이 다섯 칸만 받는다** — 사람 이름을 받을 칸이 없게. 하위가 상위의 것을 물려받는다 |
| `references` | 카테고리 키 → `[{title, url, path, note}]` |

- 모르는 최상위 키 · 컴파일되지 않는 정규식 · 상위의 순환 · 정해지지 않은 칸은 빌드 전에 막는다(`atlas check` · `atlas build` 가 멈춘다 — 조용히 다른 뜻으로 돌지 않게).
- 이 파일은 세대 id 의 입력이다. 바꾸면 새 세대다.

**카테고리 키**: 영역 값(`lang.text`) · 첫 축의 칸 `<영역> / <값>`(`server / Bug`) · 다음 축의 칸 `<영역> / <축>:<값>` · 전체 `(전체)` · 영역이 없는 일 `(미분류)`. 값 안의 ` / ` 는 `/` 로 바꾼다. 상하위는 `split` 과 `parents` 로만 생긴다.

**계산** `categories.json` (레시피 `category@1`, 결정론):

| 필드 | 내용 |
|---|---|
| `format` · `generation` · `as_of` · `recipe` | 다른 모델 파일과 같다 |
| `area` · `facets` · `params` | 쓴 정의와 문턱(`mining.category_*` · `flow_min_share`) |
| `coverage` | `units` · `uncategorized` · `ratio` · `by_source`(영역을 정한 원천별 수) |
| `categories` | 키 → 항목. `key · value · chain`(뿌리부터) `· parent · kind`(`all · area · facet` — `facet` 항목은 `facet` · `area` 도 갖는다) `· label · name · profile · strength · basis`(표본 설명) · `flow`(`n_traces · nodes · edges · main · main_n · main_share · fitness · hidden · min_share`) · `variants · dwell · sequence · after_end · open · code · roles · related · references · contacts`(`configured · observed`) `· children · cells` |
| `members` | `wu_id` → 그 일이 든 카테고리 키들 |
| `open` | 열린 `wu_id` → 지금 단계와 그 단계에 들어선 시각 |
| `suggestions.paths` | 경로 규칙 후보 — 디렉터리 · 영역 · 정밀도 · 표본 · 덮게 될 미분류 수. 적용하지 않는다 |

| 규칙 | 이유 |
|---|---|
| 흐름은 끝난 일의 해결 시점까지로 그린다. 끝난 뒤의 사건은 `after_end`, 열린 일은 `open` 으로 따로 | 열린 일과 릴리스 정리가 흐름을 왜곡하지 않게 |
| `main` 은 실제로 그대로 따른 변형 중 가장 흔한 것이다(건수와 함께) | 굵은 이음을 이은 길은 아무도 따르지 않은 길일 수 있다 |
| 접점의 `observed` 는 역할(`actor_role`)만 쓴다 | 사람은 역할로만 남는다 |
| 수치는 건수 · 강도와 함께 낸다 | 작은 칸의 비율은 참고만 |

## LLM 출력 — `data/llm/`

LLM 은 계산된 사실을 말로 옮기고, 엔진이 검증한 것만 채널에 싣는다. 원칙 · 작업 · 백엔드는 `CHANNELS.md` §3.

| 파일 | 형식 |
|---|---|
| `cache/<작업>/<캐시 키>.json` | `{key, task, version, item, status, output, problems, meta}`. `status` 는 `accepted · rejected` 만 남긴다 — 오류는 캐시하지 않는다(다음에 다시, 7일에 두 번 오류면 물러선다). 캐시 키 = 작업 · 버전 · 시스템 프롬프트 · 출력 스키마 · 모델 · 대상의 모양의 해시(백엔드 종류는 넣지 않는다 — 대화 세션에서 들인 답을 자동 실행이 그대로 쓴다) |
| `ledger.jsonl` | 호출마다 한 줄: `ts · task · version · item · key · backend · model · status`(`accepted · rejected · error · skipped_budget · stability`) `· cost_usd · cost_estimated · seconds · problems · run`(실행 하나의 id — 실측이 자기 호출만 센다). 덧붙이기만. 깨진 줄은 건너뛰고 읽는다. 시각은 시각으로 비교한다 |
| `category_card.json` | `{task, cards: {<카테고리 키>: {shape, paragraphs, watch, digest, model, task_version, created}}}` — 문장 안의 수치는 `{사실 id}` 자리표시로만 있고 보여 줄 때 지금 값으로 채운다. `shape` 가 지금 카테고리의 모양(흐름 · 크기 구간)과 다르면 보이지 않는다 |
| `.lock` | 실행 하나씩. 3시간 넘은 잠금은 죽은 실행이 남긴 것으로 본다. 같은 프로세스 안에서는 다시 잡을 수 있다(실측 `atlas llm bench` 가 여러 작업을 한 잠금 안에서 돈다) |

| 규칙 | 이유 |
|---|---|
| 출력은 스키마 · 근거 인용(허락된 사실 id · 관측 줄) · 숫자 출처(자리표시 또는 인용한 근거에 있는 숫자) · 표지(URL · HTML · 링크 금지) · 남는 양을 통과해야 쓴다. 문장 단위로 버리고, 남는 것이 모자라면 답 전체를 거절한다 | 숫자는 계산이 내고 LLM 은 말만 한다 |
| 주석(`annotate`)의 출력은 기존 계약(`annotation.schema.json` · 사후 검증 R1~R4)을 그대로 따른다. 다시 주석할지는 입력 해시 · 레시피 · 코드북 버전 · 코드북 내용 · 프롬프트로 정한다 | 코드북이나 프롬프트가 바뀌면 옛 주석을 새 기준으로 착각하지 않게 |
| 이름 제안은 수신함으로만, 작업 기록 초안은 `WORK.draft.md` 로만 간다(경로 규칙 후보는 LLM 이 아니라 엔진이 `categories.json` 에 낸다). 정본(`method/` · `WORK.md`)에 직접 쓰지 않는다 | 사람이 고른다 |
| L0 에 쓰는 것은 사람의 판정(`atlas.llm_feedback`)뿐이다. LLM 출력 자체는 관측이 아니다 | L0 에 해석을 넣지 않는다 |

## 쓰임 기록 — `data/usage/<연-월>.jsonl`

훅 · 맥락이 낸 제안과 그것이 실제 일에서 반영됐는지(`atlas usage`, 수확 리포트의 '쓰임'). **관측(L0)이 아니다** — 세대 id 를 흔들지 않고, 지워도 지식은 그대로다.

| `kind` | 칸 |
|---|---|
| `hook.edit` | `ts · session · subject`(고친 파일, 저장소 기준) `· suggested`(아직 안 바꾼 동반 변경 후보) `· arm`(`shown` · `held`) |
| `context.issue` | `ts · issue · via`(`hook` · `cli` · `mcp`) `· session`(있으면) `· suggested`(이슈 맥락의 '바꿀 가능성이 큰 파일') |

| 규칙 | 이유 |
|---|---|
| 덧붙이기만. 시각을 읽을 수 없거나 시간대가 없는 줄은 건너뛴다. `[usage] record = false` 면 쓰지 않는다(보류도 하지 않는다) | 훅을 막지 않는다. 기록 없이 보류하면 잴 수 없는 손해만 남는다 |
| 보류군(`[usage] holdout`)은 세션 id 의 해시로 정한다 — 세션 단위, 상한 0.5. 세션을 모르면 보류하지 않는다 | 한 세션 안에서 알림과 보류가 섞이면 차이가 흐려진다. 차이의 구간은 세션 군집 부트스트랩이라 배정 단위와 같아야 한다 |
| 결과는 관측과 맞춰 셈한다: `hook.edit` 는 그 세션에서 고쳤거나 그 세션이 든 작업단위의 이후 커밋이 바꿨으면 반영. `context.issue` 는 그 이슈 작업단위의 이후 커밋 대비 정밀도 · 재현율(같은 (이슈, 세션) — 세션이 없으면 같은 날 — 은 한 번) | 사람 판정 없이 매주 잴 수 있는 쓰임의 지표 |
| 아직 모르는 것은 미확정, 세션 · 이슈가 35일 안에 안 잡히거나 이슈가 180일 넘게 안 끝나면 만료, 끝났는데 이후 커밋이 없으면 '바뀐 것 없음'으로 따로 센다 | 분모를 숨기지 않는다 |

## 수확 상태 — `data/state/harvest-last.json`

`{started, finished, ok, failures, warnings, report}` — 마지막 `atlas harvest` 의 결과. 단계(수집 · 보존 기한 · 빌드 · 채점 · 쓰임 · 리포트)마다 실패를 잡아 `failures` 에 적고, 어느 단계가 깨져도(리포트를 못 써도) 이 파일은 쓴다. `ok` 가 거짓이면 종료 코드 1. `warnings` 는 다음 수확이 '새 경고'를 가리는 데 쓴다. `atlas doctor` 가 읽어 실패 · 오래됨(`policy.stale_days`)을 알린다.

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
