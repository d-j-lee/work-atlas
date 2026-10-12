"""마이닝 — 작업단위·관측에서 주장(claim)을 캐내 L2 모델을 만든다 (결정론, LLM 없음).

  python -m atlas mine [--as-of …] [--level commit|wu]

산출 (모두 data/model/ — WU 단위 근거가 있어 git 밖):
  claims.jsonl       주장 전부 (cochange · hotspot · process · practice · moves). 형식은 mine/claims.py
  paths.json         경로별 요약 — 훅이 편집 순간 빠르게 읽는다 (핫스팟 + 동반 변경 상위 + 파일별 변경 수)
  text_index.json    비슷한 과거 작업 찾기용 TF-IDF 색인 + WU 요약
  generation.json    이번 세대: id · as_of · 입력 해시 · 주장 수 · 이전 세대 대비 변화(drift)
  history/<세대>.jsonl  이전 세대의 주장 요약 (history_generations 개까지, 변화 계산용)
세대 id 는 입력(관측 id 집합·설정·레시피 버전·as_of 날짜)의 해시다 — 같은 입력이면 같은 세대.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict

from .. import config as configmod
from ..common import dumps, load_units, paths, read_jsonl, read_observations, write_jsonl, write_text
from ..stitch import RECIPE as STITCH_RECIPE, parse_as_of
from . import claims as C
from . import cochange, corpus as corpusmod, hotspot, process, text

MODEL_FORMAT = 1          # data/model 파일 구조의 버전. 바꾸면 올린다 — 소비 쪽이 옛 형식을 조용히 잘못 읽지 않게
# 세대 id 에 들어가는 레시피 버전. 레시피(상관 포함)의 의미가 바뀌면 그 버전을 올린다 — 같은 관측·설정이라도
# 다른 의미로 만든 세대가 같은 id 를 갖지 않게(세대 비교·주석 캐시가 여기에 기댄다).
RECIPES = [STITCH_RECIPE, cochange.RECIPE, hotspot.RECIPE, process.RECIPE_PROCESS, process.RECIPE_PRACTICE,
           process.RECIPE_MOVES, text.RECIPE]


def load_excerpts(p) -> dict[str, str]:
    return {e["obs_id"]: e["text"] for e in read_jsonl(p.excerpts)} if p.excerpts.exists() else {}


def scope_resolver(obs_by_id: dict):
    def scope_of(u: dict) -> str:
        insts = [obs_by_id[m["obs_id"]]["source"]["instance"] for m in u["members"] if m["obs_id"] in obs_by_id]
        issue = [obs_by_id[m["obs_id"]]["source"]["instance"] for m in u["members"]
                 if m["obs_id"] in obs_by_id and obs_by_id[m["obs_id"]]["source"]["system"] == "issue"]
        return (issue or insts or ["-"])[0]
    return scope_of


def generation_id(observations: list[dict], cfg: dict, as_of) -> str:
    h = hashlib.sha256()
    for oid in sorted(o["obs_id"] for o in observations):
        h.update(oid.encode())
    h.update(dumps(cfg).encode())
    h.update(",".join(RECIPES).encode())
    return f"gen-{as_of:%Y%m%d}-{h.hexdigest()[:8]}"


def path_index(claims: list[dict], corpus=None, partners: int = 8) -> dict:
    """범위별 {"entries": 경로 → {hot, with}, "files": 경로 → 변경 수}. files 는 규칙이 모자랄 때의 채움(이름 짝·같은 디렉터리)에 쓴다."""
    out: dict[str, dict] = defaultdict(dict)
    for c in claims:
        if c["kind"] == "hotspot":
            out[c["scope"]].setdefault(c["subject"]["id"], {})["hot"] = {
                "strength": c["strength"], **{k: c["stat"][k] for k in ("rank", "of", "fixes", "changes")},
                "last_ts": c["last_ts"]}
        elif c["kind"] == "cochange":
            entry = out[c["scope"]].setdefault(c["subject"]["id"], {})
            entry.setdefault("with", []).append([c["object"]["id"], c["stat"]["confidence"], c["stat"]["support"],
                                                 c["strength"]])
    for scope in out.values():
        for entry in scope.values():
            if "with" in entry:
                entry["with"].sort(key=lambda r: (-r[1], -r[2], r[0]))
                del entry["with"][partners:]
    freq: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for c in (corpus.commits if corpus else []):
        if not c.large:
            for p in c.paths:
                freq[c.scope][p] += 1
    return {s: {"entries": dict(sorted(out.get(s, {}).items())), "files": dict(sorted(freq.get(s, {}).items()))}
            for s in sorted(set(out) | set(freq))}


def text_index(units: list[dict], corpus, scope_of) -> dict:
    texts = text.wu_texts(units, corpus.obs_by_id, corpus.excerpts)
    idx = text.Index(texts)
    files_of: dict[str, list[str]] = defaultdict(list)
    for c in corpus.commits:
        if c.large:
            continue
        for w in c.wus:
            files_of[w].extend(c.paths)
    meta = {}
    for u in units:
        if u["wu_id"] not in idx.vecs:
            continue
        counts: dict[str, int] = defaultdict(int)
        for p in files_of.get(u["wu_id"], []):
            counts[p] += 1
        files = [p for p, _ in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:15]]
        title = (texts[u["wu_id"]].split("\n", 1)[0])[:120]
        meta[u["wu_id"]] = {"anchor": u["anchors"][0], "scope": scope_of(u), "title": title, "files": files,
                            "duration_h": u["stats"]["duration_h"], "outcome": u["stats"]["outcome"],
                            "settled": u["span"]["settled"], "end": u["span"]["end"],
                            "labels": u["stats"].get("labels", {}), "n_changes": u["stats"]["n_changes"]}
    return {"recipe": text.RECIPE, "n": idx.n, "idf": idx.idf, "vecs": idx.vecs, "meta": meta}


def run(p, cfg: dict, as_of, level: str = "commit") -> tuple[list[dict], dict]:
    observations = read_observations(p.observations)
    units = load_units(p)
    excerpts = load_excerpts(p)
    corp = corpusmod.build(observations, units, cfg, as_of, excerpts)
    scope_of = scope_resolver(corp.obs_by_id)
    claims = (cochange.mine(corp, level) + hotspot.mine(corp) + process.mine_process(units, scope_of)
              + process.mine_practice(units, corp, scope_of) + process.mine_moves(units, scope_of))
    claims.sort(key=lambda c: c["claim_id"])
    gen = generation_id(observations, cfg, as_of)

    model = p.model
    history = model / "history"
    prev_meta = {}
    try:
        prev_meta = json.loads((model / "generation.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    prev_claims = []
    if prev_meta.get("generation") and prev_meta["generation"] != gen and (model / "claims.jsonl").exists():
        prev_claims = list(read_jsonl(model / "claims.jsonl"))
        write_jsonl(history / f"{prev_meta['generation']}.jsonl",
                    ({k: c[k] for k in ("claim_id", "kind", "scope", "strength", "n_evidence")} for c in prev_claims))
        keep = int(cfg["mining"]["history_generations"])
        for old in sorted(history.glob("gen-*.jsonl"))[:-keep] if keep else []:
            old.unlink()
    elif prev_meta.get("generation") == gen:
        prev_claims = [c for f in sorted(history.glob("gen-*.jsonl"))[-1:] for c in read_jsonl(f)]

    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for c in claims:
        counts[c["kind"]][c["strength"]] += 1
    meta = {
        "generation": gen,
        "format": MODEL_FORMAT,
        "previous": prev_meta.get("generation") if prev_meta.get("generation") != gen else prev_meta.get("previous"),
        "as_of": as_of.isoformat(timespec="seconds"),
        "level": level,
        "recipes": RECIPES,
        "inputs": {"observations": len(observations), "units": len(units), "commits": len(corp.commits)},
        "claims": {k: dict(sorted(v.items())) for k, v in sorted(counts.items())},
        "drift": C.drift(prev_claims, claims) if prev_claims else None,
    }
    write_jsonl(model / "claims.jsonl", claims)
    write_text(model / "paths.json", json.dumps({"format": MODEL_FORMAT, "generation": gen, "scopes": path_index(claims, corp)},
                                                ensure_ascii=False, separators=(",", ":")) + "\n")
    write_text(model / "text_index.json", json.dumps({"format": MODEL_FORMAT, "generation": gen,
                                                      **text_index(units, corp, scope_of)},
                                                     ensure_ascii=False, separators=(",", ":")) + "\n")
    write_text(model / "generation.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    return claims, meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas mine", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--as-of")
    ap.add_argument("--level", choices=["commit", "wu"], default="commit", help="동반 변경 거래 단위")
    args = ap.parse_args(argv)
    p = paths(args.root)
    cfg = configmod.load(p.root)
    _, meta = run(p, cfg, parse_as_of(args.as_of), args.level)
    print(json.dumps({k: meta[k] for k in ("generation", "previous", "inputs", "claims")}, ensure_ascii=False, indent=2))
    if meta["drift"]:
        d = meta["drift"]
        print(f"이전 세대 대비: +{d['added']} -{d['removed']} ↑{d['strengthened']} ↓{d['weakened']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
