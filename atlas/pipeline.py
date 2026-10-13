"""빌드 — 정본(관측·방법·오버레이)에서 파생물 전부를 처음부터 다시 만든다 (P2, N10).

  python -m atlas build [--collect] [--as-of …] [--level commit|wu] [--no-report] [--no-llm]

  (collect →) stitch → [render · 주석 이어 붙이기] → validate → mine(카테고리 포함) → catalog → [llm 자동 작업] → report(업무 지도 + 안내서)
- llm 자동 작업: atlas.toml [llm] auto 에 적은 작업(예: category_card)을 백엔드가 있을 때만 돌린다(예산·캐시 안에서). --no-llm 으로 끈다.
- 주석(LLM, 트랙 A)은 atlas llm run annotate 가 data/units/annotated.jsonl 로 낸다. 있으면 입력 문서 해시(input_hash)가
  같은 WU 에만 주석을 이어 붙이고, 해시가 바뀐 WU 는 '다시 주석할 것'으로 센다(캐시 무효화). 없으면 주석 없이 간다.
- 단계마다 걸린 시간을 남긴다(P9: 비용 기록). 한 단계라도 실패하면 거기서 멈춘다 — 반쯤 갱신된 세대를 남기지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import config as configmod
from .common import paths, read_jsonl, sha256_text, write_jsonl, write_text


def merge_annotations(stitched: list[dict], annotated: list[dict], hashes: dict[str, str]) -> tuple[list[dict], dict]:
    by_id = {u["wu_id"]: u for u in annotated if u.get("annotation")}
    out, carried, stale = [], 0, 0
    for u in stitched:
        a = by_id.get(u["wu_id"])
        if a and hashes.get(u["wu_id"]) and (a.get("build") or {}).get("input_hash") == hashes[u["wu_id"]]:
            u = {**u, "annotation": a["annotation"], "build": {**u.get("build", {}), **a.get("build", {})}}
            carried += 1
        elif a:
            stale += 1
        out.append(u)
    return out, {"carried": carried, "stale": stale, "unannotated": len(stitched) - carried}


def run(p, cfg: dict, as_of_text: str | None = None, collect: bool = False, level: str = "commit",
        report: bool = True, log=print, llm: bool = True) -> dict:
    from . import catalog, render, stitch, validate
    from . import mine as minemod
    from .common import Codebook, read_observations
    timings: dict[str, float] = {}

    def step(name):
        timings[name] = time.perf_counter()

    def done(name):
        timings[name] = round(time.perf_counter() - timings[name], 2)

    if collect:
        from .collect import main as collect_main
        step("collect")
        status = collect_main(["all", "--root", str(p.root)])
        done("collect")
        if status:
            raise RuntimeError("수집이 실패해 빌드를 멈춘다 (위 메시지 참고)")
    as_of = stitch.parse_as_of(as_of_text)
    step("stitch")
    observations = read_observations(p.observations)
    excerpts = minemod.load_excerpts(p)
    units, srep = stitch.stitch(observations, cfg, as_of, excerpts)
    write_jsonl(p.stitched, units)
    write_text(p.stitched.with_name("stitch_report.json"), json.dumps(srep, ensure_ascii=False, indent=2) + "\n")
    done("stitch")

    ann = {"carried": 0, "stale": 0, "unannotated": len(units)}
    if p.annotated.exists():
        step("render")
        obs_by_id = {o["obs_id"]: o for o in observations}
        hashes = {u["wu_id"]: sha256_text(render.render(u, obs_by_id, excerpts)) for u in units}
        units, ann = merge_annotations(units, list(read_jsonl(p.annotated)), hashes)
        done("render")
    step("validate")
    current, vrep = validate.run(units, Codebook.load(p))
    write_jsonl(p.units, current)
    done("validate")
    step("mine")
    _, gen = minemod.run(p, cfg, as_of, level)
    done("mine")
    step("catalog")
    cat = catalog.build(p, cfg)
    write_text(p.model / "catalog.json", json.dumps(cat, ensure_ascii=False, indent=2) + "\n")
    done("catalog")
    llm_summary = None
    auto = list((cfg.get("llm") or {}).get("auto") or [])
    if llm and auto and (cfg.get("llm") or {}).get("backend", "none") != "none":
        from . import llm as llmmod
        step("llm")
        llm_summary = {}
        runner = llmmod.Runner(p, cfg, log=log)
        ctx = llmmod.Ctx(p, cfg)
        for name in auto:
            task = llmmod.TASKS.get(name)
            if not task or name == "work_draft":           # 작업 폴더에 쓰는 작업은 무인으로 돌리지 않는다
                continue
            args = argparse.Namespace(key=None, limit=None, refresh=False, dry_run=False, write=False, work=None)
            try:
                accepted, tally = runner.run(task, ctx, args)
                if accepted:
                    task.sink(ctx, accepted, args)
                llm_summary[name] = dict(tally)
            except Exception as e:                       # noqa: BLE001 — 선택 단계다. 빌드(지도·안내서)를 막지 않고 남긴다
                llm_summary[name] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
                log(f"[llm] {name} 실패 — 빌드는 계속한다: {e}")
        llm_summary["cost_usd"] = round(runner.spent, 4)
        done("llm")
    out_report = None
    if report:
        from . import report as reportmod
        step("report")
        out_report = p.reports / f"atlas-{gen['generation']}.html"
        write_text(out_report, reportmod.build_html(p))
        from . import catview, guide
        cm, _ = catview.load(p)
        if cm:
            guide.write_all(p, cm)                       # data/reports/guides/ — 카테고리별 Markdown + Mermaid
        done("report")
    summary = {"generation": gen["generation"], "as_of": gen["as_of"], "wus": srep["wus"],
               "annotations": ann, "violation_rate": vrep["violation_rate"], "claims": gen["claims"],
               "warnings": len(cat["warnings"]), "report": str(out_report) if out_report else None, "llm": llm_summary,
               "seconds": timings}
    write_text(p.model / "build.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas build", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--as-of")
    ap.add_argument("--collect", action="store_true", help="먼저 설정의 모든 소스를 수집한다")
    ap.add_argument("--level", choices=["commit", "wu"], default="commit")
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="[llm] auto 작업을 이번에는 돌리지 않는다")
    args = ap.parse_args(argv)
    p = paths(args.root)
    try:
        summary = run(p, configmod.load(p.root), args.as_of, args.collect, args.level, not args.no_report, llm=not args.no_llm)
    except RuntimeError as e:
        print(f"[빌드 중단] {e}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    cat = json.loads((p.model / "catalog.json").read_text(encoding="utf-8"))
    for w in cat["warnings"]:
        print(f"[{w['level']}] {w['what']} ({w['evidence']}) → {w['action']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
