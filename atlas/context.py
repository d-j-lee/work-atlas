"""맥락 — 경로·이슈·문장에 맞는 지식을 Claude 가 바로 쓸 수 있게 짧게 낸다 (근거와 건수 포함).

  python -m atlas context --path src/a.java [--path …] [--issue ABC-123] [--text "…"] [--scope 이름]
                          [--format md|json] [--max-lines 40]

담는 것:
  경로  핫스팟(결함 수정이 몰린 파일인가 — 강도·건수) · 함께 바뀌어 온 파일(신뢰도·횟수, 테스트 짝 표시)
  이슈  지금 상태(유형·컴포넌트·기간·결과) · 비슷한 과거 작업 · 그 작업들이 바꾼 파일(가능성 순) ·
        같은 유형·컴포넌트의 처리 통계(소요 P50·P90 · 재오픈률)
  문장  비슷한 과거 작업과 그 파일 — 이슈 키가 없는 일도 찾는다
원칙: 숫자에는 건수를 붙이고, 연관을 인과로 말하지 않는다. 데이터 상태가 나쁘면(키 커버리지 등) 첫 줄에 밝힌다.
앵커(이슈 키 등)는 이 출력 안에서만 쓴다 — 커밋·문서에 붙여 넣지 않는다(CLAUDE.md).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import config as configmod
from .common import path_filter, paths, read_jsonl, units_path
from .mine import MODEL_FORMAT, text as textmod


class Model:
    """data/model 을 필요한 만큼만 늦게 읽는다 — 훅처럼 자주 불리는 경로가 큰 파일을 기다리지 않게."""

    def __init__(self, p):
        self.p = p
        self.stale = False
        self._paths = self._text = self._claims = self._catalog = self._gen = None

    def _json(self, name):
        try:
            data = json.loads((self.p.model / name).read_text(encoding="utf-8"))
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
    def catalog(self) -> dict:
        if self._catalog is None:
            self._catalog = self._json("catalog.json") or {}
        return self._catalog


def repo_relative(path: str, cwd: str | None = None) -> str:
    """절대 경로면 그 파일이 든 git 저장소 루트 기준으로 바꾼다. 실패하면 받은 그대로."""
    pth = Path(path)
    if not pth.is_absolute():
        return path.replace("\\", "/")
    base = pth.parent if pth.parent.exists() else Path(cwd or ".")
    out = subprocess.run(["git", "-C", str(base), "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    if out.returncode == 0 and out.stdout.strip():
        try:
            return pth.resolve().relative_to(Path(out.stdout.strip()).resolve()).as_posix()
        except ValueError:
            pass
    return path


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
    idx = textmod.Index.load(t["idf"], t["vecs"], t["n"])
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


def issue_info(model: Model, p, key: str) -> dict:
    wu_id = f"wu:issue:{key}"
    path = units_path(p)
    unit = next((u for u in read_jsonl(path) if u["wu_id"] == wu_id), None) if path else None
    meta = ((model.text or {}).get("meta") or {}).get(wu_id)
    text = ""
    if model.text and wu_id in model.text["vecs"]:
        text = " ".join(t for t, _ in sorted(model.text["vecs"][wu_id].items(), key=lambda x: -x[1])[:30])
    info = {"key": key, "known": bool(unit), "title": (meta or {}).get("title")}
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


def build(p, cfg: dict, paths_: list[str] = (), issue: str | None = None, text: str | None = None,
          scope: str | None = None, cwd: str | None = None) -> dict:
    model = Model(p)
    is_test = path_filter(cfg["mining"]["test_paths"])
    out: dict = {"generation": model.generation.get("generation"), "as_of": model.generation.get("as_of"),
                 "health": [w for w in model.catalog.get("warnings", []) if w["level"] in ("주의", "제안")][:2]}
    out["paths"] = [path_info(model, repo_relative(x, cwd), scope, is_test, fill=5) for x in paths_]
    if model.stale:
        out["health"] = [{"level": "주의", "what": "모델이 옛 형식이다", "evidence": "data/model",
                          "action": "atlas build 로 다시 만든다"}] + out["health"]
    if issue:
        info = issue_info(model, p, issue)
        query = " ".join(filter(None, [info.get("title") or "", text or "", info.pop("text", "")]))
        exclude = {f"wu:issue:{issue}"}
        out["issue"] = info
        out["similar"] = similar(model, query, exclude=exclude)
        out["likely_files"] = likely_files(similar(model, query, k=10, exclude=exclude, with_files=True))
    elif text:
        out["similar"] = similar(model, text)
        out["likely_files"] = likely_files(similar(model, text, k=10, with_files=True))
    return out


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


def render_md(ctx: dict, max_lines: int = 40) -> str:
    lines = [f"### work-atlas 맥락 (세대 {ctx.get('generation') or '-'}, 기준 {(ctx.get('as_of') or '-')[:10]})"]
    for w in ctx.get("health", []):
        lines.append(f"- 데이터 상태: {w['what']} ({w['evidence']})")
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
            lines.append(f"- 이슈 {iss['key']}: {iss.get('title') or ''} — 유형 {','.join(lab.get('type', [])) or '-'}"
                         f" · 컴포넌트 {','.join(lab.get('component', [])) or '-'} · 시작 {iss['start'][:10]}"
                         f" · {'끝남 ' + iss['end'][:10] if iss.get('end') else '진행 중'}"
                         f"{' · 결과 ' + iss['outcome'] if iss.get('outcome') else ''}{_late(iss)}")
            pr = iss.get("process")
            if pr:
                lines.append(f"  - 같은 {pr['group']} 처리 {pr['ended']}건: 소요 중앙 {_hours(pr['duration_h_p50'])}"
                             f" · P90 {_hours(pr['duration_h_p90'])} · 재오픈 {pr['reopen_rate']:.0%}"
                             if pr["reopen_rate"] is not None else f"  - 같은 {pr['group']} 처리 {pr['ended']}건")
        else:
            lines.append(f"- 이슈 {iss['key']}: 아직 관측되지 않았다(수집 전이거나 다른 트래커)")
    sims = ctx.get("similar") or []
    if sims:
        lines.append("- 비슷한 과거 작업:")
        for s in sims:
            dur = _hours(s.get("duration_h"))
            lines.append(f"  - {s.get('anchor', s['wu_id'])} ({s['similarity']:.2f}) {(s.get('title') or '')[:70]}"
                         f" — {s.get('outcome') or '진행 중'} · {dur} · 변경 {s.get('n_changes', 0)}")
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas context", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--path", action="append", default=[])
    ap.add_argument("--issue")
    ap.add_argument("--text")
    ap.add_argument("--scope")
    ap.add_argument("--format", choices=["md", "json"], default="md")
    ap.add_argument("--max-lines", type=int)
    args = ap.parse_args(argv)
    p = paths(args.root)
    cfg = configmod.load(p.root)
    ctx = build(p, cfg, args.path, args.issue, args.text, args.scope)
    if args.format == "json":
        print(json.dumps(ctx, ensure_ascii=False, indent=2))
    else:
        print(render_md(ctx, args.max_lines or int(cfg["context"]["max_lines"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
