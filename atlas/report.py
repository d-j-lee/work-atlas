"""사람용 업무 지도 — HTML 한 장 (오프라인, 외부 의존 없음).

  python -m atlas report [--out data/reports/atlas-<세대>.html]

보여주는 것: 데이터 상태와 경고 · 핫스팟 · 동반 변경 · 처리 통계 · 실천과 결과 · AI 세션 · 세대 변화 · 읽는 법.
WU 단위 앵커와 파일 경로가 들어 있으므로 data/reports/ (git 밖)에 쓴다. 원형 단위 월간 집계(reports/)와 다르다.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

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
"""


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

    procs = [c for c in claims if c["kind"] == "process" and "|" not in c["subject"]["id"]]
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
               "<li>함께 바뀐 파일·실천과 결과는 연관이다. 인과로 읽지 않는다.</li>"
               "<li>후속 수정은 사람이 남긴 이슈 연결(caused by·broken by)과 되돌림만 센다. 파일 수준 추정(SZZ 근사)은 정밀도가 낮아 결과에서 뺐다.</li>"
               "<li>키 없는 커밋·세션은 작업단위에 안 들어간다(고아 비율). 키 습관이 정확도를 정한다.</li>"
               "<li>사람은 역할로만 있다. 개인 평가에 쓰지 않는다.</li></ul>")
    out.append(f"<footer class='mut'>work-atlas · {_e(', '.join(gen.get('recipes', [])))}</footer></main></body></html>")
    return "\n".join(out)


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
