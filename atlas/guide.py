"""업무 안내서 — 카테고리마다 Markdown + Mermaid 한 장 (위키·GitLab·GitHub 이 그림을 그린다).

  python -m atlas guide                     data/reports/guides/ 에 README.md(목차·미분류·경로 규칙 후보) + 최상위 영역마다 한 파일
  python -m atlas guide --category <키>     한 카테고리만 표준 출력으로 (대화 세션·MCP·LLM 입력용)
  python -m atlas guide --list              카테고리 키 목록과 건수

한 카테고리의 안내: 요약 → 보통의 흐름(그림) → 단계별 머문 시간 → 끝난 뒤 · 지금 열린 일 → 보통의 순서(그림) → 흐름 변형 →
관계도(그림 + 목록) → 참조점 → 접점 → 유형별 칸. 수치는 모델(data/model/categories.json)에 있는 그대로다.
작업단위 앵커·제목·경로가 들어 있으므로 data/reports/ (git 밖)에 쓴다. 팀 위키에 옮길 때는 사내 공간에만.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

from . import catview, diagram as D
from .common import paths, write_text

ALL = "(전체)"


def _titles(p) -> dict:
    f = p.model / "text_index.json"
    if not f.exists():
        return {}
    meta = json.loads(f.read_text(encoding="utf-8")).get("meta", {})
    return {w: (m.get("anchor", "").split(":", 1)[-1], m.get("title", "")) for w, m in meta.items()}


def slug(key: str) -> str:
    s = re.sub(r"[^0-9A-Za-z가-힣._-]+", "-", key).strip("-.")
    return s or "category"


def _t(x) -> str:
    """데이터·LLM 에서 온 글자를 Markdown 에 — HTML 로 해석되지 않게(&<>), 강조·링크·표·코드 문법이 되지 않게."""
    s = html.escape(str(x), quote=False).replace("\n", " ")
    for ch in "\\`*_[]|":
        s = s.replace(ch, "\\" + ch)
    return s


_cell = _t


def _code(x) -> str:
    return "`" + str(x).replace("`", "'") + "`"


def _ref(titles: dict, w: str, why: str) -> str:
    anchor, title = titles.get(w, (w.split(":", 1)[-1].split(":", 1)[-1], ""))
    return f"- {_code(anchor)} {_t(title[:100])} — {why}"


def category_md(m: dict, key: str, titles: dict, level: int = 2, cards: dict | None = None) -> str:
    cats = m["categories"]
    e = cats[key]
    h = "#" * level
    name = "전체" if key == ALL else e["name"]
    out = [f"{h} {_t(name)}  `{e['strength']}`", ""]
    if e["chain"] and key != ALL:
        out += ["상위: " + " › ".join(["전체"] + [_t(cats[c]["name"]) for c in e["chain"][:-1] if c in cats]
                                      + ([_t(cats[e["area"]]["name"])] if e["kind"] == "facet" else [])), ""]
    out += [f"> {_t(catview.summary(e))}"]
    card = (cards or {}).get(key)
    b = e.get("basis") or {}
    if e.get("flow"):
        out.append("> 흐름 기준: " + (f"{b['ended_since']} 이후에 끝난 {b['n']:,}건 (끝난 일 {b['of_ended']:,}건 중)"
                                     + (" — 최근 기간에 끝난 일이 적어 가장 최근 것들로 넓혔다" if b.get("extended") else "")
                                     if b.get("ended_since") else f"끝난 일 전체 {b['n']:,}건"))
    out.append("")
    if card:
        out += [f"**해설** _(LLM · 근거 검증 통과 · 수치는 지금 값 · {card.get('model', '')} · {card.get('created', '')[:10]})_", ""]
        out += [p for x in card["paragraphs"] for p in (_t(x), "")]
        out += [f"- {_t(x)}" for x in card["watch"]] + ([f"- 인용한 근거: {_t(', '.join(card['refs']))}"] if card.get("refs") else []) + [""]
    fl = e.get("flow")
    if fl:
        out += [f"**보통의 흐름** — {_t(catview.flow_line(e))} ({fl.get('main_n', 0):,}건이 그대로 따랐다 · 그림이 그대로 담는 흐름 {D.pct(fl['fitness'])}, "
                f"드문 활동 {fl['hidden']['nodes']}개·이음 {fl['hidden']['edges']}개 생략. 숫자 = 비율 · 다음까지 중앙값, 점선 = 재작업 고리)", "",
                "```mermaid", D.flow_mermaid(fl), "```", ""]
        if e.get("dwell"):
            bn = catview.bottleneck(e)
            out += ["**단계별 머문 시간**" + (f" — 가장 오래 머무는 곳: {_t(bn['stage'])} (중앙 {D.dur(bn['hours_p50'])}, {bn['n']}회)" if bn else ""), "",
                    "| 단계 | 횟수 | 중앙 | P90 | 댓글 중앙 |", "|---|---:|---:|---:|---:|"]
            for r in e["dwell"]:
                talk = "-" if r["comments_p50"] is None else f"{r['comments_p50']:g}"
                out.append(f"| {_cell(r['stage'])} | {r['n']} | {D.dur(r['hours_p50'])} | {D.dur(r['hours_p90'])} | {talk} |")
            out.append("")
        if e.get("after_end"):
            out += ["**끝난 뒤** — " + " · ".join(f"{_t(r['activity'])} {D.pct(r['share'])} (끝에서 중앙 {D.dur(r['hours_p50'])})"
                                                for r in e["after_end"][:4]), ""]
    op = e.get("open") or {}
    if op.get("n"):
        out += [f"**지금 열린 일 {op['n']:,}건** — " + " · ".join(f"{_t(r['stage'])} {r['n']:,} (그 단계에서 중앙 {D.dur(r['age_h_p50'])})"
                                                           for r in op["stages"][:6]), ""]
    if (e.get("sequence") or {}).get("steps"):
        out += ["**보통의 순서** — 가장 흔한 길을 누가(역할) 어디에(시스템) 하는지", "", "```mermaid", D.sequence_mermaid(e["sequence"]), "```", ""]
    if e.get("variants"):
        out += ["**흐름 변형**", "", "| 순서 | 건수 | 비율 | 소요 중앙 | 최근 예 |", "|---|---:|---:|---:|---|"]
        for v in e["variants"]:
            ex = titles.get(v["example"], (v["example"].split(":", 1)[-1],))[0]
            out.append(f"| {_cell(' → '.join(v['trace']))} | {v['n']} | {D.pct(v['share'])} | {D.dur(v['duration_h_p50']) or '-'} | {_code(ex)} |")
        out.append("")
    names = {k: c["name"] for k, c in cats.items()}
    if e["kind"] != "facet" and (e.get("related") or e["code"]["dirs"]):
        ppl = catview.people(e)
        parent = e["parent"] if e.get("parent") not in (None, ALL) else None
        out += ["**관계도** — 관련 카테고리(이슈 링크 · 후속 수정 · 같은 파일) · 주로 바꾸는 디렉터리 · 접점", "", "```mermaid",
                D.relations_mermaid(key, parent, e.get("related") or [], e["code"]["dirs"][:6], ppl, names), "```", ""]
        for g in D.group_related(e.get("related") or []):
            out.append(f"- {_t(names.get(g['key'], g['key']))}: {_t(g['tip'])}")
        if e["code"]["files"]:
            out.append("- 자주 바뀐 파일: " + " · ".join(f"{_code(f['path'])} {f['n']}" + (f"(결함 {f['fixes']})" if f["fixes"] else "")
                                                     for f in e["code"]["files"][:6]))
        out.append("")
    r = e.get("references") or {}
    refs = ([_ref(titles, x["wu_id"], f"대표 사례 (유사도 {x['sim']})") for x in r.get("representative", [])]
            + [_ref(titles, x["wu_id"], f"최근 깔끔히 끝남 {x['end'][:10]}") for x in r.get("recent_clean", [])]
            + [_ref(titles, x["wu_id"], f"주의 — {x['outcome']} {x['end'][:10]}") for x in r.get("cautionary", [])]
            + [f"- 작업 기록 {_code(x['rel'] or x['id'])} ({_t(x['status'])})" for x in r.get("works", [])]
            + [(_ref(titles, x["wu_id"], f"자주 묶이는 상위·연결 이슈 {x['n']}건") if x.get("wu_id") else f"- {_code(x['key'])} {x['n']}건")
               for x in r.get("parents", []) + r.get("link_hubs", [])]
            + [f"- {_t(x.get('title', ''))} {_t(x.get('url') or x.get('path') or '')} — {_t(x.get('note', ''))} (사람이 붙임: {_t(x['from'])})"
               for x in r.get("curated", [])])
    if refs:
        out += ["**참조점**", ""] + refs + [""]
    ppl = catview.people(e, 8)
    if ppl:
        out += ["**접점**", ""] + [f"- {_t(x)}" for x in ppl] + [""]
    subs = [(c, "하위 영역") for c in e["children"]] + [(c, "유형") for c in e["cells"]]
    if subs:
        out += ["| 하위 | 건수 | 소요 중앙 | 사후 문제 | 보통의 흐름 |", "|---|---:|---:|---:|---|"]
        for c, kind in subs:
            ce = cats[c]
            out.append(f"| {kind}: {_cell(ce['name'])} | {ce['profile']['n']:,} | {D.dur(ce['profile']['duration_h_p50']) or '-'} | "
                       f"{D.pct(ce['profile'].get('problem_rate'))} | {_cell(catview.flow_line(ce))} |")
        out.append("")
    return "\n".join(out)


def area_doc(m: dict, key: str, titles: dict, cards: dict | None = None) -> str:
    """최상위 영역 한 파일 — 자기 · 유형 칸 · 하위 영역(과 그 칸)을 차례로."""
    cats = m["categories"]
    parts = [f"<!-- atlas:generated 세대 {m.get('generation')} · 손으로 고치지 않는다 (atlas guide) -->", ""]

    def walk(k: str, level: int):
        parts.append(category_md(m, k, titles, level, cards))
        for c in cats[k]["cells"]:
            if cats[c].get("flow"):
                parts.append(category_md(m, c, titles, min(level + 1, 6), cards))
        for c in cats[k]["children"]:
            walk(c, min(level + 1, 5))

    walk(key, 1)
    return "\n".join(parts)


def index_md(m: dict, files: dict[str, str]) -> str:
    cats = m["categories"]
    cov = m.get("coverage", {})
    out = [f"<!-- atlas:generated 세대 {m.get('generation')} · 손으로 고치지 않는다 (atlas guide) -->", "",
           "# 업무 안내서", "", f"세대 `{m.get('generation')}` · 기준 {str(m.get('as_of', ''))[:10]} · "
           f"{m['area']['label']}({', '.join(m['area']['from'])}) → {', '.join(f['label'] for f in m['facets'])}", "",
           f"작업단위 {cov.get('units', 0):,}건 중 미분류 {cov.get('uncategorized', 0):,}건 ({D.pct(cov.get('ratio'))}). "
           f"영역을 정한 원천: " + ", ".join(f"{k} {v:,}" for k, v in (cov.get("by_source") or {}).items()), ""]
    sugg = (m.get("suggestions") or {}).get("paths") or []
    if sugg:
        out += ["## 경로 규칙 후보", "", "영역이 정해진 일들이 거의 혼자 쓰는 디렉터리다. 맞으면 `method/categories.yaml` 의 `paths` 에 옮긴다"
                " (자동 적용하지 않는다 — 사람이 정한다).", "", "```yaml", "paths:"]
        out += [f'  {json.dumps(s["glob"], ensure_ascii=False)}: {json.dumps(s["area"], ensure_ascii=False)}    # 정밀도 {D.pct(s["precision"])} ({s["n"]}건) · 미분류 {s["covers"]}건을 덮음' for s in sugg]
        out += ["```", ""]
    out += ["## 카테고리", "", "| 영역 | 건수 | 열림 | 소요 중앙 | 사후 문제 | 보통의 흐름 |", "|---|---:|---:|---:|---:|---|"]

    def row(k: str, depth: int):
        e = cats[k]
        link = f"[{_t(e['name'])}]({files[k]})" if k in files else _t(e["name"])
        out.append(f"| {'　' * depth}{link} | {e['profile']['n']:,} | {e['profile']['open_now']:,} | "
                   f"{D.dur(e['profile']['duration_h_p50']) or '-'} | {D.pct(e['profile'].get('problem_rate'))} | {_cell(catview.flow_line(e))} |")
        for c in e["children"]:
            row(c, depth + 1)

    for k in cats[ALL]["children"]:
        row(k, 0)
    out.append("")
    out.append(category_md(m, ALL, {}, 2))
    return "\n".join(out)


def cards_for(p, m: dict, titles: dict) -> dict:
    from . import llm
    return llm.rendered_cards(p, m, titles)


def write_all(p, m: dict) -> list[Path]:
    titles = _titles(p)
    cards = cards_for(p, m, titles)
    cats = m["categories"]
    base = p.reports / "guides"
    files = {k: f"{slug(k)}.md" for k in cats[ALL]["children"]}
    seen: dict[str, str] = {}
    taken = {"readme.md"}                                    # 목차 이름은 영역이 쓰지 못한다
    for k, f in sorted(files.items()):                     # 이름이 겹치면(대소문자 무시 — 대소문자를 안 가리는 파일 시스템) 뒤에 번호
        n = 2
        while f.lower() in taken:
            f = f"{slug(k)}-{n}.md"
            n += 1
        seen[k] = f
        taken.add(f.lower())
    written = []
    for old in base.glob("*.md") if base.exists() else []:
        if old.read_text(encoding="utf-8").startswith("<!-- atlas:generated"):
            old.unlink()                                     # 지난 세대의 안내서 — 사라진 카테고리 파일이 남지 않게
    write_text(base / "README.md", index_md(m, seen) + "\n")
    written.append(base / "README.md")
    for k, f in seen.items():
        write_text(base / f, area_doc(m, k, titles, cards) + "\n")
        written.append(base / f)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas guide", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--category", help="이 카테고리 하나만 표준 출력으로 (키는 --list)")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)
    p = paths(args.root)
    m, why = catview.load(p)
    if not m:
        print(why, file=sys.stderr)
        return 1
    cats = m["categories"]
    if args.list:
        for k in sorted(cats, key=lambda k: (catview.depth(cats[k]), -cats[k]["profile"]["n"], k)):
            print(f"{cats[k]['profile']['n']:>7,}  {k}")
        return 0
    if args.category:
        key = resolve(m, args.category)
        if not key:
            print(f"카테고리 '{args.category}' 가 없다 → atlas guide --list", file=sys.stderr)
            return 2
        titles = _titles(p)
        print(category_md(m, key, titles, 2, cards_for(p, m, titles)))
        return 0
    for f in write_all(p, m):
        print(f)
    return 0


def resolve(m: dict, text: str) -> str | None:
    """키 · 이름 · 표시 이름 어느 것으로든 (대소문자 무시)."""
    cats = m["categories"]
    if text in cats:
        return text
    low = text.strip().lower()
    for k, e in sorted(cats.items()):
        if low in (k.lower(), e["name"].lower(), str(e["label"]).lower()):
            return k
    return None


if __name__ == "__main__":
    sys.exit(main())
