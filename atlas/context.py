"""맥락 — 경로·이슈·문장에 맞는 지식을 Claude 가 바로 쓸 수 있게 짧게 낸다 (근거와 건수 포함).

  python -m atlas context --path src/a.java [--path …] [--issue ABC-123] [--text "…"] [--work 작업폴더] [--scope 이름]
                          [--format md|json] [--max-lines 40]

담는 것:
  경로  핫스팟(결함 수정이 몰린 파일인가 — 강도·건수) · 함께 바뀌어 온 파일(신뢰도·횟수, 테스트 짝 표시)
  이슈  지금 상태(유형·컴포넌트·기간·결과) · 비슷한 과거 작업 · 그 작업들이 바꾼 파일(가능성 순) ·
        같은 유형·컴포넌트의 처리 통계(소요 P50·P90 · 재오픈률)
  문장  비슷한 과거 작업과 그 파일 — 이슈 키가 없는 일도 찾는다
  작업  작업 폴더(WORK.md)의 키·제목·문제로 위를 찾는다. 비슷한 과거 작업에 그때의 작업 기록 위치를 붙인다
  카테고리  이 일이 든 업무 카테고리(이슈·작업 폴더면 그대로, 문장뿐이면 비슷한 과거 작업들의 다수결로 '아마'):
        요약 · 지금 단계와 보통 다음에 오는 것 · 가장 오래 머무는 단계 · 접점 · 참조할 사례 (data/model/categories.json)
원칙: 숫자에는 건수를 붙이고, 연관을 인과로 말하지 않는다. 데이터 상태가 나쁘면(키 커버리지 등) 첫 줄에 밝힌다.
앵커(이슈 키 등)는 이 출력 안에서만 쓴다 — 커밋·문서에 붙여 넣지 않는다(CLAUDE.md).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import config as configmod
from .common import NO_WINDOW, path_filter, paths, read_jsonl, relative_inside, rooted, units_path
from .mine import MODEL_FORMAT, text as textmod


class Model:
    """data/model 을 필요한 만큼만 늦게 읽는다 — 훅처럼 자주 불리는 경로가 큰 파일을 기다리지 않게."""

    def __init__(self, p):
        self.p = p
        self.stale = False
        self._paths = self._text = self._claims = self._catalog = self._gen = None
        self._cats: tuple | None = None

    def _json(self, name):
        try:
            from .common import _fast_loads
            data = _fast_loads((self.p.model / name).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        if name in ("paths.json", "text_index.json") and data.get("format") != MODEL_FORMAT:
            self.stale = True                       # 옛 형식은 읽지 않는다 — 조용히 틀리느니 '다시 빌드하라'고 말한다
            return None
        return data

    @property
    def generation(self) -> dict:
        if self._gen is None:
            self._gen = self._json("generation.json") or {}
        return self._gen

    @property
    def paths(self) -> dict:
        if self._paths is None:
            self._paths = (self._json("paths.json") or {}).get("scopes", {})
        return self._paths

    @property
    def text(self) -> dict | None:
        if self._text is None:
            self._text = self._json("text_index.json") or {}
        return self._text or None

    @property
    def claims(self) -> list[dict]:
        if self._claims is None:
            f = self.p.model / "claims.jsonl"
            self._claims = list(read_jsonl(f)) if f.exists() else []
        return self._claims

    @property
    def categories(self) -> dict | None:
        if self._cats is None:
            from . import catview
            self._cats = catview.load(self.p)
            if self._cats[1] and "옛 형식" in self._cats[1]:
                self.stale = True
        return self._cats[0]

    @property
    def catalog(self) -> dict:
        if self._catalog is None:
            self._catalog = self._json("catalog.json") or {}
        return self._catalog


def repo_relative(path: str, cwd: str | None = None) -> str:
    """절대 경로면 그 파일이 든 git 저장소 루트 기준으로 바꾼다. 실패하면 받은 그대로."""
    if not rooted(path):
        return path.replace("\\", "/")
    pth = Path(path)
    base = pth.parent if pth.parent.exists() else Path(cwd or ".")
    out = subprocess.run(["git", "-C", str(base), "rev-parse", "--show-toplevel"], capture_output=True, **NO_WINDOW,
                         text=True, encoding="utf-8", errors="replace")
    if out.returncode == 0 and out.stdout.strip():
        rel = relative_inside(pth, out.stdout.strip())
        if rel:
            return rel
    return pth.as_posix()                    # 저장소를 못 찾았다 — 뒷부분 맞춤(find_path)이 되게 / 로 (Windows 의 \ 도)


def find_path(model: Model, path: str, scope: str | None = None) -> tuple[str, str] | None:
    """(범위, 경로). 정확히 같은 경로가 없으면 뒷부분이 맞는 경로가 하나뿐일 때 그것."""
    scopes = {scope: model.paths.get(scope, {})} if scope else model.paths
    for s, sc in scopes.items():
        if path in sc.get("files", {}) or path in sc.get("entries", {}):
            return s, path
    hits = [(s, k) for s, sc in scopes.items() for k in sc.get("files", {})
            if k.endswith("/" + path) or path.endswith("/" + k)]
    return hits[0] if len(hits) == 1 else None


def path_info(model: Model, path: str, scope: str | None, is_test, min_conf: float = 0.0, k: int = 5,
              fill: int = 0) -> dict:
    """경로 하나의 지식. fill > 0 이면 규칙이 모자란 만큼 이름 짝·같은 디렉터리 후보로 채운다(평가의 hybrid)."""
    from .mine import cochange
    hit = find_path(model, path, scope)
    if not hit:
        return {"path": path, "known": False}
    s, key = hit
    sc = model.paths[s]
    entry = sc.get("entries", {}).get(key, {})
    partners = [{"path": p, "confidence": c, "support": n, "strength": st, "test": bool(is_test(p))}
                for p, c, n, st in entry.get("with", []) if c >= min_conf][:k]
    extra = []
    if fill and len(partners) < fill:
        extra = [{"path": p, "reason": why, "changes": n, "test": bool(is_test(p))}
                 for p, why, n in cochange.fallback(key, sc.get("files", {}), {x["path"] for x in partners},
                                                    fill - len(partners))]
    return {"path": key, "scope": s, "known": True, "hot": entry.get("hot"), "with": partners, "also": extra,
            "changes": sc.get("files", {}).get(key, 0)}


def similar(model: Model, text: str, k: int = 5, exclude: set[str] | None = None, with_files: bool = False) -> list[dict]:
    """with_files=True 면 파일을 바꾼 작업만 — '바꿀 파일' 추정에 쓴다(평가: 이 편이 재현율이 높았다)."""
    t = model.text
    if not t or not text.strip():
        return []
    idx = getattr(model, "_text_index", None)          # 한 프로세스에서 색인은 한 번만 짓는다
    if idx is None:
        idx = model._text_index = textmod.Index.load(t["idf"], t["vecs"], t["n"])
    out = []
    for wu, score in idx.query(text, k=200 if with_files else k, exclude=exclude):
        meta = t["meta"].get(wu, {})
        if with_files and not meta.get("files"):
            continue
        out.append({"wu_id": wu, "similarity": score, **meta})
        if len(out) >= k:
            break
    return out


def likely_files(sims: list[dict], k: int = 8) -> list[dict]:
    """비슷한 작업들이 바꾼 파일을 유사도로 가중해 모은다."""
    score: dict[str, float] = {}
    count: dict[str, int] = {}
    for s in sims:
        files = s.get("files", [])
        for f in files:
            score[f] = score.get(f, 0.0) + s["similarity"]
            count[f] = count.get(f, 0) + 1
    return [{"path": f, "score": round(v, 3), "in_similar": count[f]}
            for f, v in sorted(score.items(), key=lambda x: (-x[1], x[0]))[:k]]


def work_path(cfg: dict, w: dict, base: Path | None = None) -> str | None:
    """작업 기록의 위치(이 컴퓨터) — 설정의 작업 공간 루트 + 루트 기준 경로. 표시용이고 어디에도 저장하지 않는다."""
    from . import workspace as wsmod
    src = next((x for x in wsmod.sources(cfg, base) if x.name == w.get("source")), None)
    if not src or not w.get("rel"):
        return None
    full = src.root / w["rel"]
    home = str(Path.home())
    text = str(full)
    return "~" + text[len(home):] if text.startswith(home + os.sep) else text


def _work_here(cfg: dict, where: str, base: Path | None = None) -> dict:
    from . import config as configmod, workspace as wsmod
    loc = wsmod.locate(where, wsmod.sources(cfg, base))
    if not loc:
        return {"known": False, "path": str(where)}
    src, wdir, rel = loc
    rec = wsmod.read_record(wdir / src.record)
    pat = configmod.Patterns(cfg)
    keys = pat.issue_keys(*(rec.keys() if rec else [])) or pat.issue_keys(wdir.name)
    return {"known": True, "id": wsmod.resolve(src, wdir, rel), "source": src.name, "rel": rel,
            "path": work_path(cfg, {"source": src.name, "rel": rel}, base) or str(wdir), "keys": keys,
            "status": rec.status if rec else "", "title": rec.title if rec else "",
            "problem": (rec.sections.get("problem", "") if rec else "")[:600]}


def issue_info(model: Model, p, key: str) -> dict:
    wu_id = f"wu:issue:{key}"
    path = units_path(p)
    unit = next((u for u in read_jsonl(path) if u["wu_id"] == wu_id), None) if path else None
    meta = ((model.text or {}).get("meta") or {}).get(wu_id)
    text = ""
    if model.text and wu_id in model.text["vecs"]:
        text = " ".join(t for t, _ in sorted(model.text["vecs"][wu_id].items(), key=lambda x: -x[1])[:30])
    info = {"key": key, "known": bool(unit), "title": (meta or {}).get("title"), "works": (meta or {}).get("works", [])}
    if unit:
        s = unit["stats"]
        info.update({"labels": s.get("labels", {}), "start": unit["span"]["start"], "end": unit["span"]["end"],
                     "outcome": s.get("outcome"), "settled": unit["span"]["settled"], "n_changes": s["n_changes"],
                     "late_reopens": s.get("late_reopens", 0), "open_now": s.get("open_now"),
                     "files": (meta or {}).get("files", [])[:8], "signals": s.get("deterministic_signals", [])})
        labels = s.get("labels", {})
        groups = [f"type={t}|component={c}" for t in labels.get("type", []) for c in labels.get("component", [])]
        groups += [f"type={t}" for t in labels.get("type", [])]
        for g in groups:
            c = next((c for c in model.claims if c["kind"] == "process" and c["subject"]["id"] == g), None)
            if c:
                info["process"] = {"group": g, "strength": c["strength"], **{k: c["stat"][k] for k in (
                    "ended", "duration_h_p50", "duration_h_p90", "reopen_rate", "followup_rate")}}
                break
    info["text"] = text
    return info


def category_info(cm: dict, wu_id: str) -> dict | None:
    """그 작업단위의 카테고리 — 흐름이 있는 가장 구체적인 칸과, 지금 단계에서 보통 다음에 오는 것."""
    from . import catview
    keys = catview.for_wu(cm, wu_id)
    e = catview.best(cm, keys)
    if not e:
        return None
    pos = (cm.get("open") or {}).get(wu_id)
    refs = e.get("references") or {}
    return {"key": e["key"], "name": e["name"], "summary": catview.summary(e), "basis_n": (e.get("basis") or {}).get("n"),
            "stage": pos, "next": catview.next_steps(e, pos["stage"]) if pos else [],
            "bottleneck": catview.bottleneck(e), "after_end": (e.get("after_end") or [])[:2],
            "people": catview.people(e, 4, informative=True),
            "cautionary": [x["wu_id"] for x in refs.get("cautionary", [])[:2] if x["wu_id"] != wu_id],
            "representative": [x["wu_id"] for x in refs.get("representative", [])[:2] if x["wu_id"] != wu_id],
            "curated": refs.get("curated", [])[:3],
            "also": [cm["categories"][k]["name"] for k in keys                 # 이 칸의 영역·조상 말고 함께 든 영역
                     if cm["categories"][k]["kind"] == "area" and k not in e["chain"] and k != e.get("area")][:3]}


def category_guess(cm: dict, sims: list[dict]) -> dict | None:
    """키 없는 일 — 비슷한 과거 작업들이 든 카테고리의 다수결. 과반일 때만 '아마'로 말한다."""
    from . import catview
    votes: dict[str, int] = {}
    for s in sims:
        e = catview.best(cm, catview.for_wu(cm, s["wu_id"]))
        if e:
            votes[e["key"]] = votes.get(e["key"], 0) + 1
    if not votes or not sims:
        return None
    key, n = sorted(votes.items(), key=lambda x: (-x[1], x[0]))[0]
    if n * 2 <= len(sims):
        return None
    info = category_info_by_key(cm, key)
    info["guess"] = {"votes": n, "of": len(sims)}
    return info


def category_info_by_key(cm: dict, key: str) -> dict:
    from . import catview
    e = cm["categories"][key]
    refs = e.get("references") or {}
    return {"key": key, "name": e["name"], "summary": catview.summary(e), "stage": None, "next": [],
            "bottleneck": catview.bottleneck(e), "after_end": (e.get("after_end") or [])[:2],
            "people": catview.people(e, 4, informative=True),
            "cautionary": [x["wu_id"] for x in refs.get("cautionary", [])[:2]],
            "representative": [x["wu_id"] for x in refs.get("representative", [])[:2]],
            "curated": refs.get("curated", [])[:3], "also": []}


def build(p, cfg: dict, paths_: list[str] = (), issue: str | None = None, text: str | None = None,
          scope: str | None = None, cwd: str | None = None, work: str | None = None) -> dict:
    model = Model(p)
    exclude: set[str] = set()
    here = _work_here(cfg, work, p.root) if work else None
    if here and here["known"]:                  # 작업 폴더: 선언된 키로 이슈를, 제목·문제로 비슷한 작업을 찾는다
        issue = issue or (here["keys"][0] if here["keys"] else None)
        text = " ".join(filter(None, [text, here["title"], here["problem"]])) or None
        exclude.update([f"wu:work:{here['id']}", *(f"wu:issue:{k}" for k in here["keys"])])   # 이 작업 자신은 비슷한 작업이 아니다
    is_test = path_filter(cfg["mining"]["test_paths"])
    out: dict = {"generation": model.generation.get("generation"), "as_of": model.generation.get("as_of"),
                 "health": [w for w in model.catalog.get("warnings", [])        # 작업 폴더 위생은 카탈로그·work status 에서만
                            if w["level"] in ("주의", "제안") and w.get("area", "data") == "data"][:2]}
    if here:
        out["work"] = {k: v for k, v in here.items() if k != "problem"}
    out["paths"] = [path_info(model, repo_relative(x, cwd), scope, is_test, fill=5) for x in paths_]
    if model.stale:
        out["health"] = [{"level": "주의", "what": "모델이 옛 형식이다", "evidence": "data/model",
                          "action": "atlas build 로 다시 만든다"}] + out["health"]
    if issue:
        info = issue_info(model, p, issue)
        query = " ".join(filter(None, [info.get("title") or "", text or "", info.pop("text", "")]))
        exclude.add(f"wu:issue:{issue}")
        out["issue"] = info
        out["similar"] = similar(model, query, exclude=exclude)
        out["likely_files"] = likely_files(similar(model, query, k=10, exclude=exclude, with_files=True))
    elif text:
        out["similar"] = similar(model, text, exclude=exclude)
        out["likely_files"] = likely_files(similar(model, text, k=10, exclude=exclude, with_files=True))
    target = f"wu:issue:{issue}" if issue else (f"wu:work:{here['id']}" if here and here.get("known") else None)
    if target or out.get("similar"):
        cm = model.categories
        if cm:
            cat = category_info(cm, target) if target else None
            if not cat and out.get("similar") and not (out.get("issue") or {}).get("known"):
                cat = category_guess(cm, out["similar"])
            if cat:
                meta = ((model.text or {}).get("meta") or {})
                cat["anchors"] = {w: meta.get(w, {}).get("anchor", w).split(":", 1)[-1]
                                  for w in cat["cautionary"] + cat["representative"]}
                out["category"] = cat
    for item in [out.get("issue") or {}, *(out.get("similar") or [])]:
        item["work_paths"] = [x for x in (work_path(cfg, w, p.root) for w in item.get("works", [])) if x]
    return out


def record_use(p, cfg: dict, ctx: dict, issue: str | None, via: str, session: str | None = None) -> None:
    """이슈 맥락이 낸 '바꿀 가능성이 큰 파일'을 남긴다 — 그 이슈의 작업단위가 실제로 바꾼 파일과 맞춰 본다(atlas usage)."""
    files = [x["path"] for x in ctx.get("likely_files") or []]
    if issue and files:
        from . import usage
        usage.record(p, {"kind": "context.issue", "issue": issue, "via": via, "session": session, "suggested": files}, cfg)


def _hours(h) -> str:
    if h is None:
        return "-"
    return f"{h:.0f}시간" if h < 48 else f"{h / 24:.0f}일"


def _late(iss: dict) -> str:
    """확정 뒤의 재오픈은 결과를 바꾸지 않지만, 지금 다시 열려 있다는 사실은 일을 받는 사람에게 가장 중요하다."""
    if iss.get("end") and iss.get("open_now"):
        return f" · **확정 뒤 다시 열려 지금 진행 중**(재오픈 {iss.get('late_reopens') or 1}회)"
    if iss.get("late_reopens"):
        return f" · 확정 뒤 재오픈 {iss['late_reopens']}회(지금은 닫힘)"
    return ""


def _records(item: dict) -> str:
    paths_ = item.get("work_paths") or []
    return f" · 기록 `{paths_[0]}`" + (f" 외 {len(paths_) - 1}" if len(paths_) > 1 else "") if paths_ else ""


def render_md(ctx: dict, max_lines: int = 40) -> str:
    lines = [f"### work-atlas 맥락 (세대 {ctx.get('generation') or '-'}, 기준 {(ctx.get('as_of') or '-')[:10]})"]
    for w in ctx.get("health", []):
        lines.append(f"- 데이터 상태: {w['what']} ({w['evidence']})")
    wk = ctx.get("work")
    if wk:
        lines.append(f"- 작업 폴더 `{wk['path']}` — " + (
            f"{wk.get('title') or '(제목 없음)'} · 상태 {wk.get('status') or '-'} · 키 {','.join(wk.get('keys') or []) or '없음'}"
            if wk.get("known") else "설정된 작업 공간 밖이다 (atlas.toml [[sources.workspace]])"))
    for pi in ctx.get("paths", []):
        if not pi["known"]:
            lines.append(f"- `{pi['path']}`: 이 파일의 이력 지식 없음")
            continue
        h = pi.get("hot")
        head = f"- `{pi['path']}`"
        if h:
            head += (f" — **결함 수정이 몰린 파일**({h['strength']}): 수정 {h['fixes']}건/변경 {h['changes']}건,"
                     f" 순위 {h['rank']}/{h['of']}, 최근 {(h.get('last_ts') or '')[:10]}")
        lines.append(head)
        if pi["with"]:
            parts = [f"`{x['path'].rsplit('/', 1)[-1]}`{' (테스트)' if x['test'] else ''} {x['confidence']:.0%}·{x['support']}회"
                     for x in pi["with"]]
            lines.append(f"  - 함께 바뀌어 온 파일: {', '.join(parts)}")
        if pi.get("also"):
            parts = [f"`{x['path'].rsplit('/', 1)[-1]}`({x['reason']})" for x in pi["also"]]
            lines.append(f"  - 그 밖에 볼 만한 파일: {', '.join(parts)}")
    iss = ctx.get("issue")
    if iss:
        if iss["known"]:
            lab = iss.get("labels", {})
            lines.append(f"- 이슈 {iss['key']}: {iss.get('title') or '(제목 없음)'} — 유형 {','.join(lab.get('type', [])) or '-'}"
                         f" · 컴포넌트 {','.join(lab.get('component', [])) or '-'} · 시작 {iss['start'][:10]}"
                         f" · {'끝남 ' + iss['end'][:10] if iss.get('end') else '진행 중'}"
                         f"{' · 결과 ' + iss['outcome'] if iss.get('outcome') else ''}{_late(iss)}"
                         f"{'' if (wk or {}).get('known') else _records(iss)}")
            pr = iss.get("process") if not ctx.get("category") else None      # 카테고리 줄이 같은 것을 더 자세히 말한다
            if pr:
                lines.append(f"  - 같은 {pr['group']} 처리 {pr['ended']}건: 소요 중앙 {_hours(pr['duration_h_p50'])}"
                             f" · P90 {_hours(pr['duration_h_p90'])} · 재오픈 {pr['reopen_rate']:.0%}"
                             if pr["reopen_rate"] is not None else f"  - 같은 {pr['group']} 처리 {pr['ended']}건")
        else:
            lines.append(f"- 이슈 {iss['key']}: 아직 관측되지 않았다(수집 전이거나 다른 트래커)")
    cat = ctx.get("category")
    if cat:
        lines += category_lines(cat)
    sims = ctx.get("similar") or []
    if sims:
        lines.append("- 비슷한 과거 작업:")
        for s in sims:
            dur = _hours(s.get("duration_h"))
            lines.append(f"  - {s.get('anchor', s['wu_id'])} ({s['similarity']:.2f}) {(s.get('title') or '')[:70]}"
                         f" — {s.get('outcome') or '진행 중'} · {dur} · 변경 {s.get('n_changes', 0)}{_records(s)}")
    lf = ctx.get("likely_files") or []
    if lf:
        lines.append("- 비슷한 작업들이 바꾼 파일(가능성 순, 괄호는 그 파일을 바꾼 이웃 수): "
                     + ", ".join(f"`{x['path'].rsplit('/', 1)[-1]}`({x['in_similar']})" for x in lf))
    if len(lines) == 1:
        lines.append("- 해당하는 지식이 없다")
    lines.append("- 읽는 법: 건수가 작으면 참고만. 함께 바뀐 것은 연관이지 인과가 아니다.")
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [f"- …(줄 상한 {max_lines})"]
    return "\n".join(lines)


def category_lines(cat: dict) -> list[str]:
    from .diagram import dur, pct
    head = "- 카테고리 " + (f"(아마 — 비슷한 작업 {cat['guess']['of']}건 중 {cat['guess']['votes']}건) " if cat.get("guess") else "")
    out = [head + cat["summary"] + (f" · 함께 든 영역: {', '.join(cat['also'])}" if cat.get("also") else "")]
    st = cat.get("stage")
    if st:
        nxt = " · ".join(f"{x['to']} {pct(x['share'])}" + (f"(중앙 {dur(x['gap_h_p50'])})" if x.get("gap_h_p50") is not None else "")
                         + (" ↺" if x.get("loop") else "") for x in cat.get("next", []) if x["to"] != "(끝)")
        out.append(f"  - 지금 단계 {st['stage']} ({st['since'][:10]}부터)" + (f" → 보통 다음: {nxt}" if nxt else ""))
    bn = cat.get("bottleneck")
    tail = []
    if bn:
        tail.append(f"가장 오래 머무는 단계 {bn['stage']}(중앙 {dur(bn['hours_p50'])})")
    if cat.get("after_end"):
        tail.append("끝난 뒤 " + " · ".join(f"{r['activity']} {pct(r['share'])}" for r in cat["after_end"]))
    if tail:
        out.append("  - " + " · ".join(tail))
    if cat.get("people"):
        out.append("  - 접점: " + " · ".join(cat["people"]))
    anchors = cat.get("anchors") or {}
    refs = [f"주의 {anchors.get(w, w.split(':')[-1])}" for w in cat.get("cautionary", [])] + \
           [f"대표 {anchors.get(w, w.split(':')[-1])}" for w in cat.get("representative", [])] + \
           [f"{x.get('title', '')} {x.get('url') or x.get('path') or ''}".strip() for x in cat.get("curated", [])]
    if refs:
        out.append("  - 참조: " + " · ".join(refs) + f" — 자세히: atlas guide --category '{cat['key']}'")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas context", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--path", action="append", default=[])
    ap.add_argument("--issue")
    ap.add_argument("--text")
    ap.add_argument("--scope")
    ap.add_argument("--work", nargs="?", const=".", help="작업 폴더 (값 없이 쓰면 지금 폴더)")
    ap.add_argument("--format", choices=["md", "json"], default="md")
    ap.add_argument("--max-lines", type=int)
    args = ap.parse_args(argv)
    p = paths(args.root)
    cfg = configmod.load(p.root)
    ctx = build(p, cfg, args.path, args.issue, args.text, args.scope, work=args.work)
    record_use(p, cfg, ctx, args.issue, "cli")
    if args.format == "json":
        print(json.dumps(ctx, ensure_ascii=False, indent=2))
    else:
        print(render_md(ctx, args.max_lines or int(cfg["context"]["max_lines"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
