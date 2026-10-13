# 작업 공간 규칙

> 이 폴더 아래 모든 작업 폴더에 적용된다 — Claude Code 는 시작 폴더와 그 위 모든 폴더의 CLAUDE.md 를 함께 읽는다.
> 형식과 이유는 work-atlas 의 `WORKSPACE.md`.

## 폴더와 파일

- 폴더 하나가 일 하나다. 새 일은 `atlas work new <이슈 키 | 짧은-이름>` 으로 만든다 — 폴더 이름은 `ABC-123-짧은-설명` 또는 `20261013-짧은-설명`.
- 일의 정본은 그 폴더의 `WORK.md` 다. front-matter 의 `key` · `status` · `started` · `closed` 를 맞게 둔다. 상태: `active` · `waiting` · `done` · `dropped`.
- 파일은 역할로 둔다: 계획·명세 `PLAN.md` · `SPEC.md` · 결정 `DECISIONS.md` · 근거 `evidence/` · 산출물 `out/` · 버릴 것 `scratch/`.
- 끝난 일도 폴더를 옮기지 않는다. 세션이 시작 폴더에 묶여 저장돼 옮기면 `claude --continue` 로 이어지지 않는다. `atlas work close` 로 상태만 바꾼다.
- 여러 일에서 다시 쓸 파일(스크립트·런북)은 `_shared/` 로 옮기고 원래 작업의 `WORK.md` 에 한 줄 남긴다. `_` · `.` 로 시작하는 폴더는 작업으로 보지 않는다.

## Claude 에게

- 작업 폴더에서 시작했으면 `WORK.md` 의 문제부터 읽는다. 하면서 처리에 한 줄씩 남기고, 끝낼 때 결과와 회고를 채운다.
- `CONTEXT.md` 는 생성물이다 — 고치지 않는다.
- 비밀값·개인정보(토큰·고객 데이터)를 작업 폴더에 저장하지 않는다. 필요한 로그는 가린 뒤 `evidence/` 에 둔다.
