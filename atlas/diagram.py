"""그림 — 카테고리 모델을 사람이 한눈에 보는 모양으로 (결정론, 외부 의존·스크립트 없음).

SVG (업무 지도 HTML 에 그대로 넣는다 — 색은 페이지의 CSS 변수를 따라 밝은·어두운 화면 모두 맞는다)
  flow_svg       흐름: 활동을 왼쪽에서 오른쪽으로 놓고, 바로 다음은 화살표, 건너뛰는 이음은 위로, 되돌아가는 이음은 아래로 휜다.
                 굵기 = 그 이음을 지난 흐름의 비율, 글자 = 비율 · 다음 활동까지 걸린 시간(중앙값). 주 경로는 강조, 재작업 고리는 점선.
  sequence_svg   순서: 참여자(역할 → 트래커·저장소·AI·작업 폴더)의 생명선 사이로 단계마다 화살표. 왼쪽은 앞 단계에서 걸린 시간, 오른쪽은 머문 시간·댓글.
  relations_svg  관계도: 가운데 이 카테고리, 왼쪽 관련 카테고리(링크·후속 수정·같은 파일), 오른쪽 코드 영역, 아래 사람(역할·접점), 위 상위.
  spark_svg      분기별 건수 막대.
Mermaid (Markdown 안내서용 — GitLab·GitHub·위키가 그린다)
  flow_mermaid · sequence_mermaid · relations_mermaid
마우스를 올리면 자세한 수치가 뜬다(SVG title — 스크립트 없이).
"""
from __future__ import annotations

import html
import re

ROLE_LABELS = {"self": "나", "bot": "봇", "unknown": "사람(역할 미상)", "planner": "기획", "client_dev": "클라이언트",
               "qa": "QA", "ops_cs": "운영·CS", "pm_biz": "PM·사업", "infra_dba": "인프라·DBA", "security": "보안",
               "external": "외부"}
KIND_LABELS = {"link": "이슈 링크", "followup": "후속 수정", "induced_by": "이 일을 부른 변경", "files": "같은 파일"}
FONT = 12


def role_label(r: str) -> str:
    return ROLE_LABELS.get(r, r)


def _e(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def text_w(s: str, size: int = FONT) -> float:
    """글자 폭 어림 — 한글·한자는 1em, 그 밖은 0.6em."""
    return sum(size if ord(ch) >= 0x1100 else size * 0.6 for ch in s)


def clip(s: str, px: float, size: int = FONT) -> str:
    if text_w(s, size) <= px:
        return s
    out = ""
    for ch in s:
        if text_w(out + ch + "…", size) > px:
            break
        out += ch
    return out + "…"


def dur(h) -> str:
    """시간(시) → 사람이 읽는 길이."""
    if h is None:
        return ""
    if h < 1:
        return "1시간 안"
    if h < 48:
        return f"{h:.0f}시간"
    d = h / 24
    return f"{d:.0f}일" if d < 60 else f"{d / 30:.0f}개월"


def pct(x) -> str:
    return "-" if x is None else f"{x:.0%}"


def _svg(w: float, h: float, body: list[str], label: str, uid: str | None = None) -> str:
    """그림 하나. 화살촉(marker) id 는 그림마다 다르게 — 한 페이지에 그림이 여럿이고 일부가 숨겨져도(:target) 각자 제 것을 쓴다.
    uid 는 페이지 안에서 유일해야 한다(같은 그림이 두 패널에 나와도) — 부르는 쪽이 패널 id 로 준다. 없으면 내용 해시.
    화살촉 크기는 선 굵기와 무관하게(userSpaceOnUse)."""
    import hashlib
    inner = "".join(body)
    uid = re.sub(r"[^A-Za-z0-9_-]", "", uid or "") or hashlib.sha1(inner.encode("utf-8")).hexdigest()[:8]
    marks = "".join(f"<marker id='{m}-{uid}' viewBox='0 0 10 10' refX='9' refY='5' markerUnits='userSpaceOnUse' markerWidth='9' "
                    f"markerHeight='9' orient='auto-start-reverse'><path d='M0,0L10,5L0,10z' class='{m}'/></marker>" for m in ("ah", "ahm", "ahl"))
    inner = inner.replace("url(#ah)", f"url(#ah-{uid})").replace("url(#ahm)", f"url(#ahm-{uid})").replace("url(#ahl)", f"url(#ahl-{uid})")
    return (f"<svg class='dg' viewBox='0 0 {w:.0f} {h:.0f}' width='{w:.0f}' role='img' aria-label='{_e(label)}' "
            f"xmlns='http://www.w3.org/2000/svg' preserveAspectRatio='xMinYMin meet'><defs>{marks}</defs>{inner}</svg>")


def _levels(spans: list[tuple[int, int, int]]) -> dict[int, int]:
    """호(arc)의 높이 단계 — x 범위가 겹치는 호끼리는 다른 높이로 (짧은 것이 아래). spans: (시작, 끝, 번호)."""
    placed: list[tuple[int, int, int]] = []
    out = {}
    for lo, hi, k in sorted(spans, key=lambda t: (t[1] - t[0], t[0], t[2])):
        lvl = 0
        while any(lo < phi and plo < hi and pl == lvl for plo, phi, pl in placed):
            lvl += 1
        placed.append((lo, hi, lvl))
        out[k] = lvl
    return out


# ── 흐름 ──
def flow_svg(fl: dict, uid: str | None = None) -> str:
    nodes = fl.get("nodes") or []
    if not nodes:
        return ""
    n = fl["n_traces"]
    boxes, x = [], 10.0
    for nd in nodes:
        terminal = nd["id"] in ("(시작)", "(끝)")
        label = "시작" if nd["id"] == "(시작)" else "끝" if nd["id"] == "(끝)" else nd["id"]
        w = max(46.0, min(150.0, text_w(label, 12) + 18)) if not terminal else 40.0
        boxes.append({"id": nd["id"], "label": clip(label, 132), "full": label, "x": x, "w": w, "n": nd["n"],
                      "share": nd["share"], "terminal": terminal})
        x += w + 34
    width = x - 24
    idx = {b["id"]: i for i, b in enumerate(boxes)}
    edges = fl.get("edges") or []
    ups = _levels([(idx[e["from"]], idx[e["to"]], i) for i, e in enumerate(edges) if idx[e["to"]] - idx[e["from"]] > 1])
    downs = _levels([(idx[e["to"]], idx[e["from"]], i) for i, e in enumerate(edges) if idx[e["to"]] <= idx[e["from"]]])
    step = 17
    top = 20 + ((max(ups.values()) + 1) * step + 16 if ups else 0)
    bh = 38
    height = top + bh + ((max(downs.values()) + 1) * step + 26 if downs else 16)
    body = []
    order = sorted(range(len(edges)), key=lambda i: (edges[i]["main"], edges[i]["share"]))   # 굵은 것·주 경로가 위에 그려지게
    for i in order:
        e = edges[i]
        a, b = boxes[idx[e["from"]]], boxes[idx[e["to"]]]
        span = idx[e["to"]] - idx[e["from"]]
        sw = 1 + 6 * e["share"]
        cls, mk = ("main", "ahm") if e["main"] else ("loop", "ahl") if e.get("loop") else ("", "ah")
        if e["main"] and e.get("loop"):
            cls = "main loop"                               # 가장 흔한 길에 든 고리 — 강조색 점선
        tip = f"{e['from']} → {e['to']}: {e['n']}/{n} 흐름 ({pct(e['share'])})"
        if e.get("gap_h_p50") is not None:
            tip += f" · 다음까지 중앙값 {dur(e['gap_h_p50'])}"
        if e.get("rework"):
            tip += f" · 되돌아감 {e['rework']}회"
        lab = pct(e["share"]) + (f" · {dur(e['gap_h_p50'])}" if e.get("gap_h_p50") is not None and e["to"] != "(끝)" else "")
        if span == 1:
            x1, x2, y = a["x"] + a["w"], b["x"] - 2, top + bh / 2
            path = f"M{x1:.1f},{y:.1f}L{x2:.1f},{y:.1f}"
            lx, ly = (x1 + x2) / 2, y - 6
            lab = pct(e["share"])
        elif span > 1:                                    # 높이 h 의 호: 곡선 정점은 제어점 높이의 3/4
            x1, x2 = a["x"] + a["w"] * 0.62, b["x"] + b["w"] * 0.38
            hgt = ((ups[i] + 1) * step + 6) / 0.75
            path = f"M{x1:.1f},{top:.1f}C{x1:.1f},{top - hgt:.1f} {x2:.1f},{top - hgt:.1f} {x2:.1f},{top - 1:.1f}"
            lx, ly = (x1 + x2) / 2, top - hgt * 0.75 - 3
        else:
            x1, x2 = a["x"] + a["w"] * 0.38, b["x"] + b["w"] * 0.62
            hgt = ((downs[i] + 1) * step + 6) / 0.75
            yb = top + bh
            path = f"M{x1:.1f},{yb:.1f}C{x1:.1f},{yb + hgt:.1f} {x2:.1f},{yb + hgt:.1f} {x2:.1f},{yb + 1:.1f}"
            lx, ly = (x1 + x2) / 2, yb + hgt * 0.75 + 11
        body.append(f"<g class='ed {cls}'><title>{_e(tip)}</title><path d='{path}' style='stroke-width:{sw:.1f}' marker-end='url(#{mk})'/>"
                    f"<text x='{lx:.1f}' y='{ly:.1f}' class='el'>{_e(lab)}</text></g>")
    for bx in boxes:
        tip = f"{bx['full']}: {bx['n']}/{n} 흐름 ({pct(bx['share'])})"
        cls = "nd term" if bx["terminal"] else "nd"
        body.append(f"<g class='{cls}'><title>{_e(tip)}</title><rect x='{bx['x']:.1f}' y='{top}' width='{bx['w']:.1f}' height='{bh}' rx='7'/>"
                    f"<text x='{bx['x'] + bx['w'] / 2:.1f}' y='{top + 16}' class='nl'>{_e(bx['label'])}</text>"
                    + ("" if bx["terminal"] else f"<text x='{bx['x'] + bx['w'] / 2:.1f}' y='{top + 30}' class='ns'>{pct(bx['share'])}</text>")
                    + "</g>")
    return _svg(width, height, body, "흐름 그림", uid)


# ── 순서 ──
def sequence_svg(seq: dict, uid: str | None = None) -> str:
    steps = seq.get("steps") or []
    if not steps:
        return ""
    parts = seq["participants"]
    colw, left, head = 128, 64, 34
    xs = {p: left + i * colw + colw / 2 for i, p in enumerate(parts)}
    width = left + len(parts) * colw + 150
    rowh = 40
    height = head + 18 + len(steps) * rowh + 12
    body = []
    actors = {s["actor"] for s in steps}
    for p in parts:
        x = xs[p]
        label = role_label(p) if p in actors else p
        w = min(colw - 10, max(54, text_w(label) + 18))
        cls = "pa" if p in actors else "ps"
        body.append(f"<g class='{cls}'><rect x='{x - w / 2:.1f}' y='4' width='{w:.1f}' height='24' rx='6'/>"
                    f"<text x='{x:.1f}' y='20' class='nl'>{_e(clip(label, w - 8))}</text></g>"
                    f"<line class='life' x1='{x:.1f}' y1='{head - 4}' x2='{x:.1f}' y2='{height - 6}'/>")
    for i, s in enumerate(steps):
        y = head + 18 + i * rowh + 14
        xa, xb = xs[s["actor"]], xs[s["system"]]
        d = 1 if xb > xa else -1
        tip = f"{s['activity']} — {role_label(s['actor'])} ({pct(s['actor_share'])}) → {s['system']}"
        body.append(f"<g class='msg'><title>{_e(tip)}</title>"
                    f"<line x1='{xa + d * 3:.1f}' y1='{y:.1f}' x2='{xb - d * 4:.1f}' y2='{y:.1f}' marker-end='url(#ahm)'/>"
                    f"<text x='{(xa + xb) / 2:.1f}' y='{y - 6:.1f}' class='el'>{_e(clip(s['activity'], abs(xb - xa) - 6))}</text></g>")
        if s.get("gap_h_p50") is not None:
            body.append(f"<text x='{left - 8}' y='{y + 4:.1f}' class='gap'>+{_e(dur(s['gap_h_p50']))}</text>")
        notes = []
        if s.get("stay_h_p50") is not None:
            notes.append(f"머묾 {dur(s['stay_h_p50'])}")
        if s.get("comments_p50"):
            notes.append(f"댓글 {s['comments_p50']:g}")
        if notes:
            body.append(f"<text x='{left + len(parts) * colw + 6}' y='{y + 4:.1f}' class='note'>{_e(' · '.join(notes))}</text>")
    return _svg(width, height, body, "순서 그림", uid)


# ── 관계도 ──
SHORT_KIND = {"link": "링크", "followup": "후속 수정", "induced_by": "유발", "files": "같은 파일"}
KIND_ORDER = {"link": 0, "followup": 1, "induced_by": 2, "files": 3}


def group_related(related: list[dict], k: int = 8) -> list[dict]:
    """관련 카테고리를 대상별로 하나로 — 링크 문구가 여럿이어도 한 줄. 링크·후속 수정이 있는 것을 같은 파일보다 앞에."""
    groups: dict[str, dict] = {}
    for r in related:
        g = groups.setdefault(r["key"], {"key": r["key"], "parts": [], "kinds": {}})
        g["parts"].append(r)
        g["kinds"][r["kind"]] = g["kinds"].get(r["kind"], 0) + r["n"]
    out = []
    for g in groups.values():
        g["kind"] = min(g["kinds"], key=lambda x: KIND_ORDER.get(x, 9))
        g["summary"] = " · ".join(f"{SHORT_KIND.get(x, x)} {n}" for x, n in sorted(g["kinds"].items(), key=lambda t: KIND_ORDER.get(t[0], 9)))
        g["tip"] = "; ".join(f"{KIND_LABELS.get(r['kind'], r['kind'])}{'' if r['kind'] == 'files' else ' ' + r['via']} {r['n']}"
                             + (f" (자카드 {r['jaccard']})" if "jaccard" in r else "") for r in g["parts"])
        out.append(g)
    out.sort(key=lambda g: (KIND_ORDER.get(g["kind"], 9), -sum(n for x, n in g["kinds"].items() if x != "files"),
                            -g["kinds"].get("files", 0), g["key"]))
    return out[:k]


def relations_svg(center: str, parent: str | None, related: list[dict], dirs: list[dict], people: list[str],
                  labels: dict[str, str] | None = None, uid: str | None = None) -> str:
    labels = labels or {}
    related = group_related(related)
    rows = max(len(related), len(dirs), 1)
    rowh = 28
    colw, lw = 210, 250                                   # 가운데 칸 폭 · 왼쪽 상자 폭
    cx, cw = lw + 50 + colw / 2, 170
    shown = unique_suffixes([d["dir"] for d in dirs[:10]])
    top = 60 if parent else 24
    mid_h = rows * rowh
    cy = top + mid_h / 2
    width = lw + 50 + colw + 250 + 10
    height = top + mid_h + (70 if people else 16)
    body = []
    for i, g in enumerate(related):
        y = top + i * rowh + rowh / 2
        name = labels.get(g["key"], g["key"])
        body.append(f"<g class='rel {g['kind']}'><title>{_e(name + ' — ' + g['tip'])}</title>"
                    f"<path d='M{lw + 10},{y:.1f}C{cx - cw / 2 - 30},{y:.1f} {cx - cw / 2 - 30},{cy:.1f} {cx - cw / 2},{cy:.1f}'/>"
                    f"<rect x='10' y='{y - 11:.1f}' width='{lw}' height='22' rx='11'/>"
                    f"<text x='{10 + lw / 2}' y='{y + 4:.1f}'><tspan class='nl'>{_e(clip(name, 110))}</tspan>"
                    f"<tspan class='el'> {_e(clip(g['summary'], lw - 30 - min(110, text_w(name)), 11))}</tspan></text></g>")
    for i, d in enumerate(dirs[:10]):
        y = top + i * rowh + rowh / 2
        x0 = cx + cw / 2 + 40
        tip = f"{d['dir']} — 이 카테고리 작업 중 {pct(d['share'])} ({d['n']})"
        short = shown[i]
        body.append(f"<g class='rel code'><title>{_e(tip)}</title>"
                    f"<path d='M{cx + cw / 2:.1f},{cy:.1f}C{x0 - 20:.1f},{cy:.1f} {x0 - 20:.1f},{y:.1f} {x0:.1f},{y:.1f}'/>"
                    f"<rect x='{x0:.1f}' y='{y - 11:.1f}' width='200' height='22' rx='4'/>"
                    f"<text x='{x0 + 6:.1f}' y='{y + 4:.1f}' class='cl'>{_e(clip(short + '  ' + pct(d['share']), 188, 11))}</text></g>")
    if parent:
        body.append(f"<g class='rel up'><line x1='{cx:.1f}' y1='34' x2='{cx:.1f}' y2='{cy - 16:.1f}'/>"
                    f"<rect x='{cx - 80:.1f}' y='10' width='160' height='24' rx='12'/>"
                    f"<text x='{cx:.1f}' y='26' class='nl'>{_e(clip('상위: ' + labels.get(parent, parent), 150))}</text></g>")
    if people:
        y = top + mid_h + 36
        n = len(people[:4])
        for i, who in enumerate(people[:4]):
            x = cx + (i - (n - 1) / 2) * 176
            body.append(f"<g class='rel who'><title>{_e(who)}</title><line x1='{cx:.1f}' y1='{cy + 16:.1f}' x2='{x:.1f}' y2='{y - 12:.1f}'/>"
                        f"<rect x='{x - 84:.1f}' y='{y - 12:.1f}' width='168' height='24' rx='12'/>"
                        f"<text x='{x:.1f}' y='{y + 4:.1f}' class='nl'>{_e(clip(who, 158))}</text></g>")
    body.append(f"<g class='nd me'><rect x='{cx - cw / 2:.1f}' y='{cy - 16:.1f}' width='{cw}' height='32' rx='8'/>"
                f"<text x='{cx:.1f}' y='{cy + 5:.1f}' class='nl'>{_e(clip(labels.get(center, center), cw - 12))}</text></g>")
    return _svg(width, height, body, "관계도", uid)


def unique_suffixes(dirs: list[str], least: int = 2) -> list[str]:
    """디렉터리를 서로 구별되는 가장 짧은 뒷부분으로 (최소 least 단계)."""
    out = []
    for d in dirs:
        parts = d.split("/")
        k = min(least, len(parts))
        while k < len(parts) and sum(1 for o in dirs if o != d and o.split("/")[-k:] == parts[-k:]):
            k += 1
        out.append(("…/" if k < len(parts) else "") + "/".join(parts[-k:]))
    return out


def spark_svg(values: list[int]) -> str:
    if not values:
        return ""
    top = max(values) or 1
    w, h, bw = len(values) * 9, 22, 7
    bars = "".join(f"<rect x='{i * 9}' y='{h - 2 - 18 * v / top:.1f}' width='{bw}' height='{max(1.0, 18 * v / top):.1f}'/>"
                   for i, v in enumerate(values))
    return (f"<svg class='spark' viewBox='0 0 {w} {h}' width='{w}' height='{h}' role='img' "
            f"aria-label='분기별 건수 {', '.join(map(str, values))}' xmlns='http://www.w3.org/2000/svg'>"
            f"<title>최근 8분기 시작 건수 (마지막은 이번 분기): {', '.join(map(str, values))}</title>{bars}</svg>")


# 그림에 쓰는 CSS — 업무 지도 페이지가 넣는다
CSS = """
svg.dg{max-width:100%;height:auto;display:block;margin:6px 0 2px;font:12px -apple-system,"Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",sans-serif}
svg.dg text{fill:var(--fg);text-anchor:middle;paint-order:stroke;stroke:var(--bg);stroke-width:3px;stroke-linejoin:round}
svg.dg .nd rect{fill:var(--card);stroke:var(--fg);stroke-width:1.2}svg.dg .nd.term rect{fill:var(--line);stroke:var(--mut)}
svg.dg .nd.me rect{fill:var(--acc);stroke:var(--acc)}svg.dg .nd.me text{fill:#fff;stroke:none;font-weight:600}
svg.dg .nl{font-weight:600}svg.dg .ns{fill:var(--mut);font-size:11px}svg.dg .el{font-size:11px;fill:var(--mut)}
svg.dg .ed path{fill:none;stroke:var(--mut);opacity:.55}svg.dg .ed.main path{stroke:var(--acc);opacity:.95}
svg.dg .ed.loop path{stroke:var(--warn);stroke-dasharray:5 4;opacity:.9}svg.dg .ed.main.loop path{stroke:var(--acc);stroke-dasharray:6 3}svg.dg .ed.main .el{fill:var(--acc)}
svg.dg .ah{fill:var(--mut)}svg.dg .ahm{fill:var(--acc)}svg.dg .ahl{fill:var(--warn)}
svg.dg .life{stroke:var(--line);stroke-dasharray:4 4}svg.dg .pa rect{fill:var(--acc2);stroke:var(--acc2)}svg.dg .pa text{fill:#fff;stroke:none}
svg.dg .ps rect{fill:var(--card);stroke:var(--fg)}svg.dg .msg line{stroke:var(--acc);stroke-width:1.6}
svg.dg .gap{text-anchor:end;fill:var(--mut);font-size:11px}svg.dg .note{text-anchor:start;fill:var(--mut);font-size:11px}
svg.dg .rel path,svg.dg .rel line{fill:none;stroke:var(--line);stroke-width:1.6}svg.dg .rel rect{fill:var(--card);stroke:var(--mut)}
svg.dg .rel.link rect{stroke:var(--acc2)}svg.dg .rel.link path{stroke:var(--acc2)}
svg.dg .rel.followup rect,svg.dg .rel.induced_by rect{stroke:var(--warn)}svg.dg .rel.followup path,svg.dg .rel.induced_by path{stroke:var(--warn)}
svg.dg .rel.code rect{stroke:var(--line);fill:var(--bg)}svg.dg .cl{text-anchor:start;font:11px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
svg.dg .rel.who rect{stroke:var(--acc2)}svg.dg .rel.up rect{stroke:var(--fg)}
svg.spark{vertical-align:middle}svg.spark rect{fill:var(--acc2)}
"""


# ── Mermaid ──
def _m(s: str) -> str:
    """Mermaid 글자 — 문법 문자를 엔티티로 (따옴표 · 문장 구분 ; · # · 꺾쇠 · 줄바꿈)."""
    ent = {"#": "#35;", '"': "#quot;", ";": "#59;", "<": "#lt;", ">": "#gt;", "|": "#124;"}
    return re.sub(r'[#";<>|]', lambda m: ent[m.group()], str(s).replace("\n", " "))


def flow_mermaid(fl: dict) -> str:
    nodes = fl.get("nodes") or []
    if not nodes:
        return ""
    ids = {nd["id"]: f"n{i}" for i, nd in enumerate(nodes)}
    out = ["flowchart LR"]
    for nd in nodes:
        if nd["id"] == "(시작)":
            out.append(f'  {ids[nd["id"]]}(["시작"])')
        elif nd["id"] == "(끝)":
            out.append(f'  {ids[nd["id"]]}(["끝"])')
        else:
            out.append(f'  {ids[nd["id"]]}["{_m(nd["id"])}<br/>{pct(nd["share"])}"]')
    links = []
    for e in fl.get("edges") or []:
        lab = pct(e["share"]) + (f" · {dur(e['gap_h_p50'])}" if e.get("gap_h_p50") is not None and e["to"] != "(끝)" else "")
        arrow = "==>" if e["main"] else "-.->" if e.get("loop") else "-->"
        out.append(f'  {ids[e["from"]]} {arrow}|"{_m(lab)}"| {ids[e["to"]]}')
        links.append(e)
    loops = [str(i) for i, e in enumerate(links) if e.get("loop")]
    if loops:
        out.append(f"  linkStyle {','.join(loops)} stroke:#c77700,stroke-dasharray:4 3")
    return "\n".join(out)


def sequence_mermaid(seq: dict) -> str:
    steps = seq.get("steps") or []
    if not steps:
        return ""
    actors = {s["actor"] for s in steps}
    alias = {p: f"P{i}" for i, p in enumerate(seq["participants"])}
    out = ["sequenceDiagram"]
    for p in seq["participants"]:
        kind = "actor" if p in actors else "participant"
        out.append(f"  {kind} {alias[p]} as {_m(role_label(p) if p in actors else p)}")
    for s in steps:
        if s.get("gap_h_p50") is not None:
            out.append(f"  Note over {alias[s['actor']]}: +{_m(dur(s['gap_h_p50']))}")
        out.append(f"  {alias[s['actor']]}->>{alias[s['system']]}: {_m(s['activity'])}")
        notes = []
        if s.get("stay_h_p50") is not None:
            notes.append(f"머묾 {dur(s['stay_h_p50'])}")
        if s.get("comments_p50"):
            notes.append(f"댓글 {s['comments_p50']:g}")
        if notes:
            out.append(f"  Note right of {alias[s['system']]}: {_m(' · '.join(notes))}")
    return "\n".join(out)


def relations_mermaid(center: str, parent: str | None, related: list[dict], dirs: list[dict], people: list[str],
                      labels: dict[str, str] | None = None) -> str:
    labels = labels or {}
    out = ["flowchart LR", f'  me(["{_m(labels.get(center, center))}"])']
    if parent:
        out.append(f'  up["상위: {_m(labels.get(parent, parent))}"] --- me')
    for i, g in enumerate(group_related(related)):
        out.append(f'  r{i}["{_m(labels.get(g["key"], g["key"]))}"] ---|"{_m(g["summary"])}"| me')
    for i, d in enumerate(dirs[:6]):
        out.append(f'  me -->|"{pct(d["share"])}"| d{i}["{_m(d["dir"])}"]')
    for i, who in enumerate(people[:6]):
        out.append(f'  me -.- p{i}(("{_m(who)}"))')
    return "\n".join(out)
