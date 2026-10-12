"""MCP 서버 (stdio) — Claude Code 밖의 MCP 클라이언트(데스크톱·다른 에이전트)도 같은 지식을 읽게 한다. 읽기 전용.

  pip install "mcp>=1.2"        (선택 의존성 — 없으면 이 명령만 안내하고 끝난다)
  python -m atlas mcp [--root …]

도구:
  atlas_context(paths, issue, text)  경로·이슈·문장 → 맥락(마크다운) — `atlas context` 와 같은 내용
  atlas_similar(text, k)             문장 → 비슷한 과거 작업 (JSON)
  atlas_catalog()                    데이터·지식 상태와 경고 (마크다운)
쓰기 도구는 두지 않는다 — 예측·판정 기록은 사용 스킬이 `atlas record` 로만 한다(유일한 쓰기 경로).
SDK 2.x 는 FastMCP 를 MCPServer 로 바꿨다 — 둘 다 받는다.
"""
from __future__ import annotations

import json
import sys

INSTRUCTIONS = ("work-atlas: 이 저장소에서 일이 실제로 어떻게 처리돼 왔는지(결함이 몰린 파일, 함께 바뀌어 온 파일, 비슷한 과거 작업과 "
                "결과, 처리 통계)를 근거 건수와 함께 돌려준다. 파일을 고치기 전·이슈를 시작할 때 부른다. 연관이지 인과가 아니다.")


def _server_class():
    try:
        from mcp.server.mcpserver import MCPServer          # SDK 2.x
        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP              # SDK 1.x
        return FastMCP


def create(root: str | None = None):
    from . import catalog, config as configmod, context
    from .common import paths
    p = paths(root)
    cfg = configmod.load(p.root)
    server = _server_class()(name="work-atlas", instructions=INSTRUCTIONS)

    @server.tool(description="경로(저장소 루트 기준)·이슈 키·문장에 맞는 업무 지식을 근거 건수와 함께 마크다운으로 돌려준다.")
    def atlas_context(paths: list[str] | None = None, issue: str | None = None, text: str | None = None) -> str:
        ctx = context.build(p, cfg, paths or [], issue, text)
        return context.render_md(ctx, int(cfg["context"]["max_lines"]))

    @server.tool(description="문장과 비슷한 과거 작업단위(앵커·제목·결과·소요·바꾼 파일)를 유사도 순으로 돌려준다.")
    def atlas_similar(text: str, k: int = 5) -> str:
        return json.dumps(context.similar(context.Model(p), text, k=max(1, min(k, 20))), ensure_ascii=False)

    @server.tool(description="소스 신선도·커버리지·작업단위·주장 수·경고 — 지금 이 지식을 얼마나 믿을 수 있는지.")
    def atlas_catalog() -> str:
        return catalog.render_text(catalog.build(p, cfg))

    return server


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    root = argv[argv.index("--root") + 1] if "--root" in argv else None
    try:
        server = create(root)
    except ImportError:
        print('MCP SDK 가 없다 — pip install "mcp>=1.2" 후 다시 실행한다 (선택 기능)', file=sys.stderr)
        return 2
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
