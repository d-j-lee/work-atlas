"""단일 진입점: python -m atlas <명령> [인자]

명령마다 모듈을 늦게 불러온다 — 훅처럼 자주 불리는 명령이 무거운 모듈을 기다리지 않게.
"""
from __future__ import annotations

import importlib
import sys

from . import __version__

COMMANDS: dict[str, tuple[str, str]] = {
    # 수집 → 상관
    "collect": ("atlas.collect", "소스에서 관측을 모은다 (git · jira · claude-code · workspace)"),
    "ingest": ("atlas.ingest", "외부 수집기가 낸 관측 JSONL 을 검증해 덧붙인다"),
    "stitch": ("atlas.stitch", "관측을 작업단위(WU)로 묶는다"),
    "render": ("atlas.render", "WU 입력 문서(LLM 이 읽는 유일한 입력)를 만든다"),
    "validate": ("atlas.validate", "LLM 주석을 근거 규칙으로 사후 검증한다"),
    # 마이닝 → 메타
    "mine": ("atlas.mine", "주장(claim)을 캐낸다 — 동반 변경 · 핫스팟 · 처리 통계 · 처리 수별 결과"),
    "catalog": ("atlas.catalog", "데이터·지식의 메타데이터와 세대 간 변화를 낸다"),
    "build": ("atlas.pipeline", "stitch → render → validate → mine → catalog → report 를 한 번에"),
    # 소비
    "context": ("atlas.context", "경로·이슈·문장에 맞는 지식을 Claude 가 읽기 좋은 형태로 낸다"),
    "hook": ("atlas.hook", "Claude Code 훅 — 프롬프트·파일 편집 순간에 맥락을 넣는다"),
    "report": ("atlas.report", "사람용 업무 지도(HTML 한 장) — 업무 카테고리별 흐름 · 순서 · 관계도 · 참조점 · 접점"),
    "guide": ("atlas.guide", "업무 안내서(Markdown + Mermaid) — 카테고리마다 한 장, 또는 한 카테고리를 표준 출력으로"),
    "similar": ("atlas.similar", "확정 WU 중 유사 사례"),
    "work": ("atlas.workspace", "작업 폴더 — 새 작업 · 맥락 파일 · 종료 · 목록과 위생 (작업 공간)"),
    "llm": ("atlas.llm", "LLM 작업 — 카테고리 해설 · 주석 · 주간 요약 · 작업 기록 초안 · 이름 제안 (근거 검증·캐시·예산)"),
    "record": ("atlas.record", "트리아지 예측·판정·자기 보고를 남긴다 (대화 세션의 유일한 쓰기 경로)"),
    "score": ("atlas.score", "예측 채점 — 최상위 효용 지표"),
    "mcp": ("atlas.mcp_server", "MCP 서버(stdio) — 선택 의존성 mcp 필요"),
    # 검증·관리
    "eval": ("atlas.evaluate", "시간 분할 효용 평가 (기준선 대비)"),
    "check": ("atlas.check", "무결성 검사 (커밋 전 훅 · CI)"),
    "doctor": ("atlas.doctor", "점검 — 지금 무엇이 되고 무엇이 빠졌고 다음에 무엇을 할지 (아무것도 바꾸지 않는다)"),
    "gen-schema": ("atlas.schema_gen", "codebook.yaml → annotation.schema.json"),
}


def usage() -> str:
    width = max(map(len, COMMANDS))
    lines = [f"work-atlas {__version__}", "", "사용: python -m atlas <명령> [인자]   (명령별 도움말: <명령> -h)", ""]
    lines += [f"  {name.ljust(width)}  {desc}" for name, (_, desc) in COMMANDS.items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):          # cp949 콘솔에서 표현 못 하는 글자로 죽지 않게
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        return 0
    if argv[0] in ("-V", "--version"):
        print(__version__)
        return 0
    name, rest = argv[0], argv[1:]
    if name not in COMMANDS:
        print(f"알 수 없는 명령: {name}\n\n{usage()}", file=sys.stderr)
        return 2
    module = importlib.import_module(COMMANDS[name][0])
    from .config import ConfigError
    try:
        return int(module.main(rest) or 0)
    except ConfigError as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2
    except BrokenPipeError:                      # atlas guide … | head 처럼 읽는 쪽이 먼저 닫았다 — 오류가 아니다
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
