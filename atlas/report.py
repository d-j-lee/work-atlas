"""사람용 업무 지도 — HTML 한 장 (오프라인, 외부 의존·스크립트 없음).

  python -m atlas report [--out data/reports/atlas-<세대>.html]

보여주는 것: 데이터 상태와 경고 · **업무 카테고리**(트리 + 카테고리마다 흐름 · 단계별 머문 시간 · 순서 · 변형 · 관계도 · 참조점 ·
접점, LLM 해설이 있으면 그것도) · 핫스팟 · 동반 변경 · 실천과 결과 · AI 세션 · 세대 변화 · 읽는 법.
카테고리 화면 전환은 링크(#)와 CSS :target 으로 한다 — 스크립트 없이 파일 하나로 열린다.
WU 단위 앵커·제목과 파일 경로가 들어 있으므로 data/reports/ (git 밖)에 쓴다. 원형 단위 월간 집계(reports/)와 다르다.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

from . import catview, diagram as D
from .common import paths, read_jsonl, write_text

CSS = """
:root{--bg:#fbfaf7;--fg:#1f2328;--mut:#656d76;--line:#e4e1da;--card:#fff;--acc:#b4532a;--acc2:#2f6f8f;--ok:#2e7d4f;--warn:#a15c00}
@media (prefers-color-scheme: dark){:root{--bg:#16181b;--fg:#e6e3dc;--mut:#9aa0a6;--line:#2c3036;--card:#1d2024;--acc:#e08a5f;--acc2:#7fb4cf;--ok:#6cc08f;--warn:#e0a84f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,"Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",sans-serif}
main{max-width:1080px;margin:0 auto;padding:28px 16px 60px}h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:36px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.mut{color:var(--mut)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}.card b{display:block;font-size:22px}
table{width:100%;border-collapse:collapse;font-size:13.5px}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:600}td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
code{font:12.5px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;word-break:break-all}
.bar{height:8px;background:var(--acc);border-radius:4px;min-width:2px}.bar.b2{background:var(--acc2)}
.tag{display:inline-block;padding:0 7px;border-radius:9px;font-size:12px;border:1px solid var(--line)}
.high{color:var(--acc);border-color:var(--acc)}.medium{color:var(--acc2);border-color:var(--acc2)}.low{color:var(--mut)}
.warn li{margin:4px 0}.lvl{font-weight:600;color:var(--warn)}.scroll{overflow-x:auto}
footer{margin-top:40px;font-size:13px}
.cat{display:grid;grid-template-columns:260px minmax(0,1fr);gap:18px;align-items:start}
@media (max-width:820px){.cat{grid-template-columns:1fr}}
.tree{position:sticky;top:8px;max-height:92vh;overflow:auto;font-size:13.5px;border:1px solid var(--line);border-radius:10px;padding:8px 10px;background:var(--card)}
.tree ul{list-style:none;margin:0;padding-left:14px}.tree>ul{padding-left:0}.tree li{margin:2px 0}
.tree summary{cursor:pointer}.tree a{color:var(--fg);text-decoration:none}.tree a:hover{text-decoration:underline}
.tree .c{color:var(--mut);font-size:12px;font-variant-numeric:tabular-nums}.tree .cell a{color:var(--mut)}
.panel{display:none}.panel:target{display:block}.panels:not(:has(.panel:target)) .panel.home{display:block}
.panel h3{font-size:20px;margin:2px 0 4px}.panel h4{font-size:15px;margin:22px 0 4px}
.crumb{font-size:13px;color:var(--mut)}.crumb a{color:var(--mut)}
.k{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:8px;margin:12px 0}
.k .card{padding:8px 10px}.k .card b{font-size:18px}
.flow{overflow-x:auto;border:1px solid var(--line);border-radius:10px;padding:4px 8px;background:var(--card)}
.llm{border-left:3px solid var(--acc2);padding:4px 12px;margin:10px 0;background:var(--card)}
.llm .src{font-size:12px;color:var(--mut)}
.panel th{white-space:nowrap}a{color:var(--acc2)}a:visited{color:var(--acc2)}.panel td code{white-space:nowrap;word-break:normal}
"""
CSS += D.CSS


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def _pct(x) -> str:
    return "-" if x is None else f"{x:.0%}"


def _hours(h) -> str:
    if h is None:
        return "-"
    return f"{h:.0f}시간" if h < 48 else f"{h / 24:.0f}일"


def _short(path: str, keep: int = 3) -> str:
    parts = path.split("/")
    return ("…/" if len(parts) > keep else "") + "/".join(parts[-keep:])


def build_html(p) -> str:
    model = p.model
    load = lambda n: json.loads((model / n).read_text(encoding="utf-8")) if (model / n).exists() else {}
    gen, cat = load("generation.json"), load("catalog.json")
    claims = list(read_jsonl(model / "claims.jsonl")) if (model / "claims.jsonl").exists() else []
    st = cat.get("stitch") or {}
    out = [f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>업무 지도 {_e(gen.get('generation'))}</title><style>{CSS}</style></head><body><main>",
           f"<h1>업무 지도</h1><div class='mut'>세대 {_e(gen.get('generation'))} · 기준 시각 {_e((gen.get('as_of') or '')[:16])}"
           f" · 결정론 마이닝(LLM 없음) · 모든 수치는 관측에서 다시 센 것</div>"]

    cards = [("관측", sum(s["observations"] for s in cat.get("sources", []))), ("작업단위", st.get("wus")),
             ("확정", st.get("settled")), ("다중 소스", _pct(st.get("multi_source_ratio"))),
             ("고아 관측", _pct(st.get("orphan_ratio"))), ("주장", sum(sum(v.values()) for v in (gen.get("claims") or {}).values()))]
    out.append("<div class='grid'>" + "".join(f"<div class='card'><span class='mut'>{_e(k)}</span><b>{_e(v)}</b></div>"
                                             for k, v in cards) + "</div>")
    if cat.get("warnings"):
        out.append("<h2>먼저 볼 것</h2><ul class='warn'>" + "".join(
            f"<li><span class='lvl'>{_e(w['level'])}</span> {_e(w['what'])} <span class='mut'>({_e(w['evidence'])})</span> → {_e(w['action'])}</li>"
            for w in cat["warnings"]) + "</ul>")

    out.append("<h2>소스</h2><div class='scroll'><table><tr><th>소스</th><th class='n'>관측</th><th>기간</th><th class='n'>연결</th>"
               "<th class='n'>이슈 키</th><th class='n'>수집</th></tr>")
    for s in cat.get("sources", []):
        out.append(f"<tr><td>{_e(s['system'])}:{_e(s['instance'])}</td><td class='n'>{s['observations']:,}</td>"
                   f"<td>{_e(s['first_ts'][:10])} ~ {_e(s['last_ts'][:10])}</td><td class='n'>{_pct(s.get('linked_ratio'))}</td>"
                   f"<td class='n'>{_pct(s.get('commit_key_coverage'))}</td><td class='n'>{s['freshness_days']}일 전</td></tr>")
    out.append("</table></div>")

    hots = sorted((c for c in claims if c["kind"] == "hotspot"), key=lambda c: (c["scope"], c["stat"]["rank"]))[:25]
    if hots:
        top = max(c["stat"]["fix_damp"] for c in hots) or 1
        out.append("<h2>결함 수정이 몰린 파일</h2><p class='mut'>결함 수정 커밋이 많은 순(같으면 최근일수록 무겁게). "
                   "막대는 최근 가중 점수다. 여기를 바꿀 땐 경계 조건과 회귀 테스트를 더 본다.</p>"
                   "<div class='scroll'><table><tr><th>#</th><th>파일</th><th>최근 가중</th><th class='n'>수정/변경</th><th>최근</th><th>강도</th></tr>")
        for c in hots:
            w = 100 * c["stat"]["fix_damp"] / top
            out.append(f"<tr><td class='n'>{c['stat']['rank']}</td><td><code title='{_e(c['subject']['id'])}'>{_e(_short(c['subject']['id']))}</code></td>"
                       f"<td style='width:22%'><div class='bar' style='width:{w:.0f}%'></div></td>"
                       f"<td class='n'>{c['stat']['fixes']}/{c['stat']['changes']}</td><td>{_e((c['last_ts'] or '')[:10])}</td>"
                       f"<td><span class='tag {c['strength']}'>{c['strength']}</span></td></tr>")
        out.append("</table></div>")

    pairs: dict[tuple, dict] = {}
    for c in claims:                                   # 짝 하나는 한 번만 — 신뢰도가 높은 방향으로
        if c["kind"] == "cochange" and c["strength"] in ("high", "medium"):
            key = (c["scope"], *sorted((c["subject"]["id"], c["object"]["id"])))
            if key not in pairs or c["stat"]["confidence"] > pairs[key]["stat"]["confidence"]:
                pairs[key] = c
    cos = sorted(pairs.values(), key=lambda c: (-c["stat"]["support"] * c["stat"]["confidence"], c["claim_id"]))[:30]
    if cos:
        out.append("<h2>함께 바뀌어 온 파일</h2><p class='mut'>a 를 바꾼 커밋 중 b 도 바꾼 비율(신뢰도)과 횟수. 빠뜨리기 쉬운 짝이다 — 연관이지 요구 사항이 아니다.</p>"
                   "<div class='scroll'><table><tr><th>a 를 바꾸면</th><th>b 도</th><th class='n'>신뢰도</th><th class='n'>횟수</th><th class='n'>향상도</th></tr>")
        for c in cos:
            out.append(f"<tr><td><code title='{_e(c['subject']['id'])}'>{_e(_short(c['subject']['id'], 2))}</code></td>"
                       f"<td><code title='{_e(c['object']['id'])}'>{_e(_short(c['object']['id'], 2))}</code></td>"
                       f"<td class='n'>{c['stat']['confidence']:.0%}</td><td class='n'>{c['stat']['support']}</td><td class='n'>{c['stat']['lift']}</td></tr>")
        out.append("</table></div>")

    cm, _ = catview.load(p)
    if cm:
        out.append(categories_html(p, cm))

    procs = [c for c in claims if c["kind"] == "process" and "|" not in c["subject"]["id"]] if not cm else []
    if procs:
        out.append("<h2>이런 일은 보통</h2><p class='mut'>트래커 라벨별. 소요는 끝난 일, 비율은 확정된 일(열린 창 경과)만 센다.</p>"
                   "<div class='scroll'><table><tr><th>범위</th><th>묶음</th><th class='n'>끝남</th><th class='n'>소요 중앙</th><th class='n'>P90</th>"
                   "<th class='n'>재오픈</th><th class='n'>후속 수정</th><th class='n'>중단</th></tr>")
        for c in sorted(procs, key=lambda c: (c["scope"], -c["stat"]["ended"]))[:30]:
            s = c["stat"]
            out.append(f"<tr><td>{_e(c['scope'])}</td><td>{_e(c['subject']['id'])}</td><td class='n'>{s['ended']}</td>"
                       f"<td class='n'>{_hours(s['duration_h_p50'])}</td><td class='n'>{_hours(s['duration_h_p90'])}</td>"
                       f"<td class='n'>{_pct(s['reopen_rate'])}</td><td class='n'>{_pct(s['followup_rate'])}</td><td class='n'>{_pct(s['abandoned_rate'])}</td></tr>")
        out.append("</table></div>")

    pracs = [c for c in claims if c["kind"] == "practice"]
    names = {"test_changed": "테스트 파일도 바꿈", "ai_assisted": "AI 세션이 있었음", "ai_ran_tests": "AI 세션에서 테스트 실행",
             "multi_commit": "커밋 3개 이상", "wide_change": "파일 10개 이상"}
    if pracs:
        out.append("<h2>실천과 결과</h2><p class='mut'>확정된 이슈 작업에서 실천이 있던 쪽과 없던 쪽의 문제율(재오픈·후속 수정·롤백). "
                   "<b>연관이지 인과가 아니다</b> — 어려운 일일수록 커밋이 많아지는 식의 교란이 있다.</p>"
                   "<div class='scroll'><table><tr><th>실천</th><th class='n'>있음: 문제율</th><th class='n'>없음: 문제율</th><th class='n'>차이</th><th>강도</th></tr>")
        for c in pracs:
            s = c["stat"]
            out.append(f"<tr><td>{_e(names.get(c['subject']['id'], c['subject']['id']))}</td>"
                       f"<td class='n'>{_pct(s['with']['rate'])} ({s['with']['problem']}/{s['with']['n']})</td>"
                       f"<td class='n'>{_pct(s['without']['rate'])} ({s['without']['problem']}/{s['without']['n']})</td>"
                       f"<td class='n'>{s['diff']:+.1%}</td><td><span class='tag {c['strength']}'>{c['strength']}</span></td></tr>")
        out.append("</table></div>")

    ai = cat.get("ai")
    if ai:
        out.append("<h2>AI 세션</h2><div class='grid'>" + "".join(
            f"<div class='card'><span class='mut'>{_e(k)}</span><b>{_e(v)}</b></div>" for k, v in [
                ("세션", ai["sessions"]), ("작업단위 연결", _pct(ai.get("linked_ratio"))), ("편집한 세션", ai["with_edits"]),
                ("편집 세션 중 테스트 실행", _pct(ai["test_ratio_of_editing"])), ("도구 오류율", _pct(ai.get("tool_error_rate"))),
                ("압축", ai["compactions"])]) + "</div>")

    d = gen.get("drift")
    if d:
        out.append(f"<h2>이전 세대 대비</h2><p>새 주장 {d['added']} · 사라짐 {d['removed']} · 강해짐 {d['strengthened']} · 약해짐 {d['weakened']}"
                   f" <span class='mut'>(상위 겹침: {', '.join(f'{k} {v}' for k, v in d['top_overlap'].items())})</span></p>")

    out.append("<h2>읽는 법과 한계</h2><ul class='mut'>"
               "<li>모든 수치는 관측(L0)에서 다시 센 기술통계다. 건수가 작은 칸은 참고만 한다.</li>"
               "<li>업무 카테고리는 method/categories.yaml 이 정한 축(기본: 트래커 컴포넌트 → 유형)으로 묶은 것이다. "
               "흐름은 끝난 일의 끝(해결)까지 사건이고, 끝난 뒤의 정리·열린 일은 따로 센다. 굵기는 비율, 숫자는 다음 단계까지 걸린 시간의 중앙값이다.</li>"
               "<li>LLM 해설은 이 화면의 수치·사례만 근거로 쓰도록 검증을 거친 것만 싣는다. 판단은 수치와 사례로 확인한다.</li>"
               "<li>함께 바뀐 파일·실천과 결과는 연관이다. 인과로 읽지 않는다.</li>"
               "<li>후속 수정은 사람이 남긴 이슈 연결(caused by·broken by)과 되돌림만 센다. 파일 수준 추정(SZZ 근사)은 정밀도가 낮아 결과에서 뺐다.</li>"
               "<li>키 없는 커밋·세션은 작업단위에 안 들어간다(고아 비율). 키 습관이 정확도를 정한다.</li>"
               "<li>사람은 역할로만 있다. 개인 평가에 쓰지 않는다.</li></ul>")
    out.append(f"<footer class='mut'>work-atlas · {_e(', '.join(gen.get('recipes', [])))}</footer></main></body></html>")
    return "\n".join(out)


def _pid(key: str) -> str:
    import hashlib
    return "c-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def _titles(p) -> dict:
    f = p.model / "text_index.json"
    if not f.exists():
        return {}
    try:
        meta = json.loads(f.read_text(encoding="utf-8")).get("meta", {})
    except json.JSONDecodeError:
        return {}
    return {w: (m.get("anchor", ""), m.get("title", "")) for w, m in meta.items()}


def _llm_cards(p, m: dict, titles: dict) -> dict:
    """검증을 통과한 카테고리 해설 (atlas llm) — 카테고리의 모양이 그대로인 것만, 수치는 지금 값으로 채워서."""
    from . import llm
    return llm.rendered_cards(p, m, {w: (a.split(":", 1)[-1], t) for w, (a, t) in titles.items()})


def categories_html(p, m: dict) -> str:
    cats = m["categories"]
    titles = _titles(p)
    cards = _llm_cards(p, m, titles)
    names = {k: e["name"] for k, e in cats.items()}

    def ref(w: str, extra: str = "") -> str:
        anchor, title = titles.get(w, (w.split(":", 1)[-1], ""))
        anchor = anchor.split(":", 1)[-1] if ":" in anchor else anchor
        return f"<li><code>{_e(anchor)}</code> {_e(title[:90])}{(' <span class=mut>' + _e(extra) + '</span>') if extra else ''}</li>"

    def link(k: str, text: str | None = None) -> str:
        return f"<a href='#{_pid(k)}'>{_e(text if text is not None else cats[k]['label'])}</a>"

    def tree(k: str) -> str:
        e = cats[k]
        kids = "".join(tree(c) for c in e["children"])
        cells = "".join(f"<li class='cell'>{link(c, '· ' + cats[c]['value'])} <span class='c'>{cats[c]['profile']['n']:,}</span></li>"
                        for c in e["cells"])
        head = f"{link(k)} <span class='c'>{e['profile']['n']:,}</span>"
        if kids or cells:
            return f"<li><details><summary>{head}</summary><ul>{kids}{cells}</ul></details></li>"
        return f"<li>{head}</li>"

    root = cats["(전체)"]
    cov = m.get("coverage", {})
    areas = "".join(tree(c) for c in root["children"])
    kinds = "".join(f"<li class='cell'>{link(c, '· ' + cats[c]['value'])} <span class='c'>{cats[c]['profile']['n']:,}</span></li>"
                    for c in root["cells"])
    nav = (f"<nav class='tree' aria-label='카테고리'><a href='#{_pid('(전체)')}'><b>전체</b></a> "
           f"<span class='c'>{root['profile']['n']:,}</span><ul>{areas}</ul><ul>{kinds}</ul></nav>")
    panels = [panel(k, cats, names, ref, link, cards) for k in sorted(cats, key=lambda k: (k != "(전체)", k))]
    sugg = (m.get("suggestions") or {}).get("paths") or []
    head = (f"<h2>업무 카테고리</h2><p class='mut'>{_e(m['area']['label'])}({_e(', '.join(m['area']['from']))}) 트리와 "
            f"{_e(', '.join(f['label'] for f in m['facets']))}별 칸. 왼쪽에서 고르면 그 카테고리의 흐름 · 머문 시간 · 순서 · 관계도 · 참조점 · 접점이 나온다. "
            f"미분류 {cov.get('uncategorized', 0):,}건 ({_pct(cov.get('ratio'))})"
            + (f" — 경로 규칙 후보 {len(sugg)}개가 그중 일부를 덮는다(아래 전체 화면)." if sugg else ".") + "</p>")
    return head + f"<div class='cat'>{nav}<div class='panels'>{''.join(panels)}</div></div>"


def panel(k: str, cats: dict, names: dict, ref, link, cards: dict) -> str:
    e = cats[k]
    pr = e["profile"]
    chain = ["(전체)"] + list(e["chain"]) + ([k] if e["kind"] == "facet" else [])
    crumb = " › ".join(link(c, "전체" if c == "(전체)" else cats[c]["label"] if c in cats else c) for c in chain[:-1] if c in cats)
    out = [f"<section class='panel{' home' if k == '(전체)' else ''}' id='{_pid(k)}'>",
           f"<div class='crumb'>{crumb}</div>" if crumb else "",
           f"<h3>{_e('전체' if k == '(전체)' else e['name'])} <span class='tag {e['strength']}'>{e['strength']}</span></h3>"]
    b = e.get("basis") or {}
    if e.get("flow"):
        out.append("<p class='mut'>" + (f"흐름은 {_e(b['ended_since'])} 이후에 끝난 {b['n']:,}건(끝난 일 전체 {b['of_ended']:,}건 중)으로 그렸다."
                                        + (" 최근 기간에 끝난 일이 적어 가장 최근 것들로 넓혔다 — 옛 흐름이 섞였을 수 있다." if b.get("extended") else "")
                                        if b.get("ended_since") else
                                        f"흐름은 끝난 일 {b['n']:,}건으로 그렸다.") + "</p>")
    kpis = [("건수", f"{pr['n']:,}", f"열림 {pr['open_now']:,} · 최근 90일 {pr['recent_90d']:,}"),
            ("소요 중앙", D.dur(pr["duration_h_p50"]) or "-", f"P90 {D.dur(pr['duration_h_p90']) or '-'} · 끝남 {pr['ended']:,}"),
            ("사후 문제", _pct(pr.get("problem_rate")), f"재오픈 {_pct(pr['reopen_rate'])} · 후속 {_pct(pr['followup_rate'])} · 확정 {pr['settled']:,}"),
            ("중단", _pct(pr["abandoned_rate"]), "확정 중"),
            ("댓글 중앙", "-" if pr["comments_p50"] is None else f"{pr['comments_p50']:g}", f"AI {_pct(pr['ai_share'])} · 작업 기록 {_pct(pr['work_share'])}"),
            ("추세", D.spark_svg(pr["trend_quarters"]), f"{_e(pr['first'] or '')} ~ {_e(pr['last'] or '')}")]
    out.append("<div class='k'>" + "".join(f"<div class='card'><span class='mut'>{t}</span><b>{v if t == '추세' else _e(v)}</b>"
                                           f"<span class='mut' style='font-size:12px'>{_e(sub) if t != '추세' else sub}</span></div>"
                                           for t, v, sub in kpis) + "</div>")
    card = cards.get(k)
    if card:
        out.append(f"<div class='llm'><div class='src'>LLM 해설 · 근거 검증 통과 · 수치는 지금 값 · {_e(card.get('model', ''))}"
                   f" · {_e(card.get('created', '')[:10])}</div>"
                   + "".join(f"<p>{_e(x)}</p>" for x in card.get("paragraphs", []))
                   + ("<ul>" + "".join(f"<li>{_e(x)}</li>" for x in card.get("watch", [])) + "</ul>" if card.get("watch") else "")
                   + (f"<div class='src'>인용한 근거: {_e(', '.join(card['refs']))}</div>" if card.get("refs") else "")
                   + f"<div class='src'>틀렸으면: atlas record llm-feedback --key '{_e(k)}' --verdict wrong</div></div>")
    subs = [("하위 영역", e["children"]), ("유형별", e["cells"])]
    for title, keys in subs:
        if not keys:
            continue
        out.append(f"<h4>{title}</h4><div class='scroll'><table><tr><th>카테고리</th><th class='n'>건수</th><th class='n'>소요 중앙</th>"
                   "<th class='n'>사후 문제</th><th>보통의 흐름</th></tr>")
        for c in keys:
            ce = cats[c]
            out.append(f"<tr><td>{link(c, ce['label'] if ce['kind'] == 'area' else ce['value'])}</td><td class='n'>{ce['profile']['n']:,}</td>"
                       f"<td class='n'>{_e(D.dur(ce['profile']['duration_h_p50']) or '-')}</td><td class='n'>{_pct(ce['profile'].get('problem_rate'))}</td>"
                       f"<td class='mut'>{_e(catview.flow_line(ce))}</td></tr>")
        out.append("</table></div>")
    fl = e.get("flow")
    if fl:
        out.append("<h4>흐름</h4><p class='mut'>굵기 = 그 이음을 지난 흐름의 비율 · 숫자 = 비율 · 다음 활동까지 걸린 시간(중앙값). "
                   "진한 선은 가장 흔한 길, 점선은 이미 지난 단계로 되돌아가는 재작업 고리.</p>"
                   f"<div class='flow'>{D.flow_svg(fl, _pid(k) + 'f')}</div>"
                   f"<p class='mut' style='font-size:12.5px'>그림이 그대로 담는 흐름 {_pct(fl['fitness'])} · 드물어서 뺀 활동 {fl['hidden']['nodes']}개 · 이음 {fl['hidden']['edges']}개"
                   f" (흐름의 {_pct(fl.get('min_share'))} 미만)</p>")
        bn = catview.bottleneck(e)
        if e.get("dwell"):
            top = max((r["hours_p50"] or 0) for r in e["dwell"]) or 1
            out.append("<h4>단계별 머문 시간</h4><p class='mut'>그 활동부터 다음 활동까지. "
                       + (f"가장 오래 머무는 곳: <b>{_e(bn['stage'])}</b> (중앙 {_e(D.dur(bn['hours_p50']))}, {bn['n']}회)." if bn else "") + "</p>"
                       "<div class='scroll'><table><tr><th>단계</th><th class='n'>횟수</th><th>중앙</th><th class='n'>P90</th><th class='n'>댓글 중앙</th></tr>")
            for r in e["dwell"]:
                w = 100 * (r["hours_p50"] or 0) / top
                talk = "-" if r["comments_p50"] is None else f"{r['comments_p50']:g}"
                out.append(f"<tr><td>{_e(r['stage'])}</td><td class='n'>{r['n']}</td>"
                           f"<td style='width:34%'><div class='bar b2' style='width:{w:.0f}%'></div><span class='mut'>{_e(D.dur(r['hours_p50']))}</span></td>"
                           f"<td class='n'>{_e(D.dur(r['hours_p90']))}</td><td class='n'>{talk}</td></tr>")
            out.append("</table></div>")
        if e.get("after_end"):
            out.append("<p class='mut'>끝난 뒤: " + " · ".join(f"{_e(r['activity'])} {_pct(r['share'])} (끝에서 중앙 {_e(D.dur(r['hours_p50']))})"
                                                              for r in e["after_end"][:4]) + "</p>")
    op = e.get("open") or {}
    if op.get("n"):
        out.append(f"<h4>지금 열린 일 {op['n']:,}건 — 단계별</h4><p class='mut'>"
                   + " · ".join(f"{_e(r['stage'])} {r['n']:,} (그 단계에서 중앙 {_e(D.dur(r['age_h_p50']))})" for r in op["stages"][:6]) + "</p>")
    if e.get("sequence", {}).get("steps"):
        out.append("<h4>보통의 순서</h4><p class='mut'>가장 흔한 길을 누가(역할) 어디에(시스템) 하는지로. 왼쪽은 앞 단계에서 걸린 시간.</p>"
                   f"<div class='flow'>{D.sequence_svg(e['sequence'], _pid(k) + 's')}</div>")
    if e.get("variants"):
        out.append("<h4>흐름 변형</h4><div class='scroll'><table><tr><th>순서</th><th class='n'>건수</th><th class='n'>비율</th><th class='n'>소요 중앙</th><th>최근 예</th></tr>")
        for v in e["variants"]:
            anchor = v["example"].split(":", 1)[-1].split(":", 1)[-1]
            out.append(f"<tr><td>{_e(' → '.join(v['trace']))}</td><td class='n'>{v['n']}</td><td class='n'>{_pct(v['share'])}</td>"
                       f"<td class='n'>{_e(D.dur(v['duration_h_p50']) or '-')}</td><td><code>{_e(anchor)}</code></td></tr>")
        out.append("</table></div>")
    if e["kind"] == "area" or (e["kind"] == "all" and e.get("code", {}).get("dirs")):
        ppl = catview.people(e)
        rel = e.get("related") or []
        out.append("<h4>관계도</h4><p class='mut'>왼쪽: 관련 카테고리(이슈 링크 · 후속 수정 · 같은 파일 — 같은 깊이) · 오른쪽: 이 일이 주로 바꾸는 디렉터리 · "
                   "아래: 접점. 마우스를 올리면 수치가 나온다.</p>"
                   f"<div class='flow'>{D.relations_svg(k, e['parent'] if e['parent'] not in (None, '(전체)') else None, rel, e['code']['dirs'][:6], ppl, names, _pid(k) + 'r')}</div>")
        if e["code"]["files"]:
            out.append("<p class='mut' style='font-size:12.5px'>자주 바뀐 파일: " + " · ".join(
                f"<code>{_e(_short(f['path'], 2))}</code> {f['n']}" + (f"(결함 {f['fixes']})" if f["fixes"] else "") for f in e["code"]["files"][:6]) + "</p>")
    elif e["kind"] == "facet":
        out.append(f"<p class='mut'>관계도는 영역 화면에: {link(e['area'], cats[e['area']]['name'])}</p>")
    r = e.get("references") or {}
    items = []
    for x in r.get("representative", []):
        items.append(ref(x["wu_id"], f"대표 사례 · 유사도 {x['sim']}"))
    for x in r.get("recent_clean", []):
        items.append(ref(x["wu_id"], f"최근 깔끔히 끝남 {x['end'][:10]}"))
    for x in r.get("cautionary", []):
        items.append(ref(x["wu_id"], f"주의 — {x['outcome']} {x['end'][:10]}"))
    for x in r.get("works", []):
        items.append(f"<li>작업 기록 <code>{_e(x['rel'] or x['id'])}</code> <span class='mut'>{_e(x['status'])}</span></li>")
    for x in r.get("parents", []) + r.get("link_hubs", []):
        items.append(ref(x["wu_id"], f"자주 묶이는 상위·연결 이슈 ({x['n']}건)") if x.get("wu_id") else f"<li><code>{_e(x['key'])}</code> ({x['n']}건)</li>")
    for x in r.get("curated", []):
        where = x.get("url") or x.get("path") or ""
        items.append(f"<li>{_e(x.get('title', where))} <code>{_e(where)}</code> <span class='mut'>{_e(x.get('note', ''))} · 사람이 붙임({_e(x['from'])})</span></li>")
    if items:
        out.append("<h4>참조점</h4><ul>" + "".join(items) + "</ul>")
    ppl = catview.people(e, 8)
    if ppl:
        out.append("<h4>접점</h4><ul>" + "".join(f"<li>{_e(x)}</li>" for x in ppl) + "</ul>"
                   + ("" if (e.get("contacts") or {}).get("configured") else
                      "<p class='mut' style='font-size:12.5px'>사람이 붙인 접점이 없다 — method/categories.yaml 의 contacts 에 팀·채널·역할을 적으면 여기와 맥락에 나온다.</p>"))
    out.append("</section>")
    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas report", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    p = paths(args.root)
    doc = build_html(p)
    gen = json.loads((p.model / "generation.json").read_text(encoding="utf-8")).get("generation", "now") \
        if (p.model / "generation.json").exists() else "now"
    out = Path(args.out) if args.out else p.reports / f"atlas-{gen}.html"
    write_text(out, doc)
    print(f"report → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
