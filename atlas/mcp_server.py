"""MCP 서버 (stdio) — Claude Code 밖의 MCP 클라이언트(데스크톱·다른 에이전트)도 같은 지식을 읽게 한다. 읽기 전용.

  pip install "mcp>=1.2"        (선택 의존성 — 없으면 이 명령만 안내하고 끝난다)
  python -m atlas mcp [--root …]

도구:
  atlas_context(paths, issue, text)  경로·이슈·문장 → 맥락(마크다운) — `atlas context` 와 같은 내용
  atlas_similar(text, k)             문장 → 비슷한 과거 작업 (JSON)
  atlas_catalog()                    데이터·지식 상태와 경고 (마크다운)
  atlas_categories()                 업무 카테고리 트리 — 키 · 건수 · 소요 중앙 · 보통의 흐름 (마크다운)
  atlas_category(key)                한 카테고리의 안내 — 흐름 · 머문 시간 · 순서 · 관계 · 참조점 · 접점 (마크다운 + Mermaid)
쓰기 도구는 두지 않는다 — 예측·판정 기록은 사용 스킬이 `atlas record` 로만 한다(유일한 쓰기 경로).
SDK 2.x 는 FastMCP 를 MCPServer 로 바꿨다 — 둘 다 받는다.
"""
from __future__ import annotations

import json
import sys

INSTRUCTIONS = ("work-atlas: 이 저장소에서 일이 실제로 어떻게 처리돼 왔는지(결함이 몰린 파일, 함께 바뀌어 온 파일, 비슷한 과거 작업과 "
                "결과, 업무 카테고리별 흐름·순서·관계·참조점·접점)를 근거 건수와 함께 돌려준다. 파일을 고치기 전·이슈를 시작할 때 부른다. "
                "연관이지 인과가 아니다.")


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
        context.record_use(p, cfg, ctx, issue, "mcp")
        return context.render_md(ctx, int(cfg["context"]["max_lines"]))

    @server.tool(description="문장과 비슷한 과거 작업단위(앵커·제목·결과·소요·바꾼 파일)를 유사도 순으로 돌려준다.")
    def atlas_similar(text: str, k: int = 5) -> str:
        return json.dumps(context.similar(context.Model(p), text, k=max(1, min(k, 20))), ensure_ascii=False)

    @server.tool(description="소스 신선도·커버리지·작업단위·주장 수·경고 — 지금 이 지식을 얼마나 믿을 수 있는지.")
    def atlas_catalog() -> str:
        return catalog.render_text(catalog.build(p, cfg))

    @server.tool(description="업무 카테고리 트리(영역 → 하위 영역, 유형별 칸)를 건수·소요 중앙·사후 문제율·보통의 흐름과 함께. "
                             "atlas_category 에 넘길 키를 여기서 고른다.")
    def atlas_categories() -> str:
        from . import catview
        m, why = catview.load(p)
        return tree_md(m) if m else why

    @server.tool(description="한 업무 카테고리의 안내: 요약 · 보통의 흐름(Mermaid) · 단계별 머문 시간 · 끝난 뒤 · 열린 일 · "
                             "보통의 순서(누가 어디에) · 흐름 변형 · 관계도 · 참조점(대표·주의 사례) · 접점. key 는 키·이름 어느 것이든.")
    def atlas_category(key: str) -> str:
        from . import catview, guide
        m, why = catview.load(p)
        if not m:
            return why
        k = guide.resolve(m, key)
        if not k:
            return f"카테고리 '{key}' 가 없다 — atlas_categories 로 키를 본다"
        titles = guide._titles(p)
        return guide.category_md(m, k, titles, 2, guide.cards_for(p, m, titles))

    return server


def tree_md(m: dict, limit: int = 120) -> str:
    from . import catview
    from .diagram import dur, pct
    cats = m["categories"]
    lines = [f"업무 카테고리 (세대 {m.get('generation')}) — 미분류 {m['coverage']['uncategorized']}/{m['coverage']['units']}",
             "키 · 건수 · 소요 중앙 · 사후 문제 · 보통의 흐름"]

    def walk(k: str, depth: int):
        e = cats[k]
        if len(lines) < limit:
            lines.append(f"{'  ' * depth}- `{k}` {e['profile']['n']:,}건 · {dur(e['profile']['duration_h_p50']) or '-'}"
                         f" · {pct(e['profile'].get('problem_rate'))} · {catview.flow_line(e) or '-'}")
        for c in e["children"] + e["cells"]:
            walk(c, depth + 1)

    walk("(전체)", 0)
    if len(lines) >= limit:
        lines.append(f"…(상한 {limit}줄 — 나머지는 atlas guide --list)")
    return "\n".join(lines)


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
