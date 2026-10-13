"""작업 — 무엇을 묻고(입력), 무엇을 받고(스키마), 어떻게 거르고(검증), 어디에 두나(sink).

  category_card   카테고리 해설 (수치는 사실 id 자리표시 — 보여 줄 때 지금 값으로)
  annotate        트랙 A 주석 → data/units/annotated.jsonl
  weekly_digest   주간 요약 → data/reports/<주>-digest.md
  work_draft      작업 기록 초안 → 표준 출력 또는 작업 폴더의 WORK.draft.md (있으면 덮지 않는다)
  category_names  카테고리 이름·정의 제안 → inbox/
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..common import read_jsonl, write_jsonl, write_text
from ..diagram import dur, pct, role_label
from .base import PLACEHOLDER, SECTIONS, STMT, _parse, _read_json, canon, digest, llm_dir, prompt_body
from .check import _stmts, check_stmt, numbers_in

@dataclass
class Item:
    key: str                       # 사람이 읽는 대상 이름 (카테고리 키 · wu id · 주 · 작업 폴더)
    prompt: str                    # 입력(사용자 프롬프트)
    shape: dict                    # 캐시 정체성 — 이것이 같으면 같은 답을 다시 쓴다
    check: dict = field(default_factory=dict)      # 검증에 쓰는 것 (허락된 refs · 사실 · 숫자 …)


class Ctx:
    """작업들이 함께 쓰는 읽기 — 필요할 때 한 번만."""

    def __init__(self, p, cfg: dict):
        self.p, self.cfg = p, cfg
        self._cm = self._titles = self._obs = self._units = self._ex = None

    @property
    def cm(self) -> dict:
        if self._cm is None:
            from .. import catview
            self._cm, why = catview.load(self.p)
            if not self._cm:
                raise RuntimeError(why)
        return self._cm

    @property
    def titles(self) -> dict:
        if self._titles is None:
            from ..guide import _titles
            self._titles = _titles(self.p)
        return self._titles

    @property
    def obs_by_id(self) -> dict:
        if self._obs is None:
            from ..common import read_observations
            self._obs = {o["obs_id"]: o for o in read_observations(self.p.observations)}
        return self._obs

    @property
    def excerpts(self) -> dict:
        if self._ex is None:
            from ..mine import load_excerpts
            self._ex = load_excerpts(self.p)
        return self._ex

    def units(self, which: str = "current") -> list[dict]:
        from ..common import load_units
        if which == "stitched":
            return list(read_jsonl(self.p.stitched)) if self.p.stitched.exists() else []
        if self._units is None:
            self._units = load_units(self.p)
        return self._units


class Task:
    id = ""
    version = "1"
    describe = ""

    def schema(self, ctx: Ctx) -> dict:
        raise NotImplementedError

    def items(self, ctx: Ctx, args) -> list[Item]:
        raise NotImplementedError

    def check(self, ctx: Ctx, item: Item, out: dict) -> tuple[dict | None, list[str]]:
        raise NotImplementedError

    def sink(self, ctx: Ctx, accepted: list[tuple[Item, dict, str]], args) -> list[str]:
        return []

    def system(self, ctx: Ctx) -> str:
        return prompt_body(ctx.p, self.id)


# · 카테고리 해설
def card_facts(e: dict) -> dict[str, tuple[str, str]]:
    """사실 id → (설명, 보여 줄 값). 값은 지금 모델에서 다시 만든다 — 카드는 id 만 갖는다."""
    from .. import catview
    F: dict[str, tuple[str, str]] = {}

    def put(k, label, v):
        if v not in (None, "", "-"):
            F[k] = (label, v)

    pr = e["profile"]
    put("n", "건수", f"{pr['n']:,}건")
    put("open_now", "지금 열린 일", f"{pr['open_now']:,}건")
    put("recent_90d", "최근 90일에 시작한 일", f"{pr['recent_90d']:,}건")
    put("ended", "끝난 일", f"{pr['ended']:,}건")
    put("settled", "사후 결과까지 확정된 일", f"{pr['settled']:,}건")
    put("duration_p50", "소요 중앙값(끝난 일)", dur(pr["duration_h_p50"]))
    put("duration_p90", "소요 P90(끝난 일)", dur(pr["duration_h_p90"]))
    for k, label in (("problem_rate", "사후 문제율(재오픈·후속 수정·롤백, 확정 중)"), ("reopen_rate", "재오픈율(확정 중)"),
                     ("abandoned_rate", "중단률(확정 중)")):
        if pr.get(k) is not None:
            put(k, label, pct(pr[k]))
    if pr.get("comments_p50") is not None:
        put("comments_p50", "댓글 수 중앙값(끝난 일)", f"{pr['comments_p50']:g}개")
    if pr.get("ai_share"):
        put("ai_share", "AI 세션이 있었던 비율", pct(pr["ai_share"]))
    fl = e.get("flow")
    if fl:
        put("flow_n", "흐름 표본(최근에 끝난 일)", f"{(e.get('basis') or {}).get('n', 0):,}건")
        put("main_path", "가장 흔한 흐름", " → ".join(fl["main"]))
        if e.get("variants"):
            v = e["variants"][0]
            put("variant1", "가장 흔한 변형", f"{' → '.join(v['trace'])} ({pct(v['share'])})")
        bn = catview.bottleneck(e)
        if bn:
            put("bottleneck_stage", "가장 오래 머무는 단계", bn["stage"])
            put("bottleneck_p50", "그 단계에 머문 시간 중앙값", dur(bn["hours_p50"]))
        loops = [x for x in fl["edges"] if x.get("loop")]
        if loops:
            put("rework", "재작업 고리", ", ".join(f"{x['from']} → {x['to']} ({pct(x['share'])})" for x in loops[:3]))
        if e.get("after_end"):
            a = e["after_end"][0]
            put("after_end", "끝난 뒤 가장 흔한 활동", f"{a['activity']} ({pct(a['share'])}, 끝에서 중앙 {dur(a['hours_p50'])})")
    op = e.get("open") or {}
    if op.get("n"):
        top = op["stages"][0]
        put("open_top", "열린 일이 가장 많이 머무는 단계", f"{top['stage']} ({top['n']:,}건, 그 단계에서 중앙 {dur(top['age_h_p50'])})")
    return F


def _bucket(v, edges) -> int | None:
    return None if v is None else sum(1 for x in edges if v >= x)


def card_magnitudes(e: dict) -> dict:
    """수치의 '크기 구간' — 해설은 숫자를 쓰지 않지만 "드물다·길다"처럼 크기를 말할 수 있다. 구간이 바뀌면 카드를 다시 쓴다."""
    from .. import catview
    pr = e["profile"]
    rate = (0.05, 0.15, 0.35, 0.65)
    hours = (24, 168, 720, 2160, 8760)
    count = (10, 100, 1000, 10000)
    bn = catview.bottleneck(e) or {}
    op = (e.get("open") or {}).get("stages") or [{}]
    ae = (e.get("after_end") or [{}])[0]
    return {"problem_rate": _bucket(pr.get("problem_rate"), rate), "reopen_rate": _bucket(pr.get("reopen_rate"), rate),
            "abandoned_rate": _bucket(pr.get("abandoned_rate"), rate), "ai_share": _bucket(pr.get("ai_share"), rate),
            "duration_p50": _bucket(pr.get("duration_h_p50"), hours), "duration_p90": _bucket(pr.get("duration_h_p90"), hours),
            "bottleneck": _bucket(bn.get("hours_p50"), hours), "n": _bucket(pr["n"], count),
            "open_now": _bucket(pr.get("open_now"), count), "recent_90d": _bucket(pr.get("recent_90d"), count),
            "open_top": op[0].get("stage"), "open_top_share": _bucket(op[0].get("share"), rate),
            "after_end": ae.get("activity"), "after_end_share": _bucket(ae.get("share"), rate),
            "files": [f["path"] for f in (e.get("code") or {}).get("files", [])[:3]]}


def card_context(e: dict, cats: dict, titles: dict) -> tuple[dict, set[str], list[str], dict]:
    """맥락(숫자 없는 것) · 허락된 refs · 숫자를 품어도 되는 이름들 · 모양."""
    from ..diagram import group_related
    fl = e.get("flow") or {}
    cases = []
    r = e.get("references") or {}
    for kind, rows in (("대표 사례", r.get("representative", [])), ("최근 깔끔히 끝남", r.get("recent_clean", [])),
                       ("주의 사례", r.get("cautionary", []))):
        for x in rows:
            anchor, title = titles.get(x["wu_id"], (x["wu_id"].split(":")[-1], ""))
            cases.append({"ref": anchor, "kind": kind + (f" — {x['outcome']}" if x.get("outcome") else ""), "title": title[:120]})
    curated = [{"ref": f"ref{i + 1}", "title": x.get("title", ""), "where": x.get("url") or x.get("path") or "", "note": x.get("note", "")}
               for i, x in enumerate(r.get("curated", []))]
    related = [{"key": g["key"], "name": cats[g["key"]]["name"] if g["key"] in cats else g["key"], "how": sorted(g["kinds"])}
               for g in group_related(e.get("related") or [])[:6]]
    dirs = [d["dir"] for d in (e.get("code") or {}).get("dirs", [])[:5]]
    files = [f["path"] for f in (e.get("code") or {}).get("files", [])[:5]]
    people = {"configured": (e.get("contacts") or {}).get("configured", []),
              "observed_roles": [{"role": role_label(x["role"]), "first_stage": x["first_stage"]}
                                 for x in (e.get("contacts") or {}).get("observed", [])[:4]]}
    chain = [cats[c]["name"] for c in e.get("chain", []) if c in cats]
    context = {
        "category": {"key": e["key"], "name": e["name"], "chain": chain, "kind": e["kind"],
                     "kinds_inside": list((e.get("facets") or {}).get("kind", {}))[:6],
                     "children": [cats[c]["name"] for c in e.get("children", [])][:8]},
        "flow": {"main": fl.get("main", []), "variants": [v["trace"] for v in e.get("variants", [])[:3]],
                 "stages_longest_first": [x["stage"] for x in e.get("dwell", []) if x["n"] >= 3][:4],   # bottleneck 과 같은 기준
                 "loops": [f"{x['from']} → {x['to']}" for x in fl.get("edges", []) if x.get("loop")]},
        "related": related, "code": {"dirs": dirs, "files": files}, "people": people, "cases": cases, "curated": curated,
    }
    allowed = set(SECTIONS) | {c["ref"] for c in cases} | {x["key"] for x in related} | {c["ref"] for c in curated}
    literals = ([e["name"], e["key"], *chain] + [x["name"] for x in related] + dirs + files + [c["ref"] for c in cases]
                + [c["title"] for c in cases] + fl.get("main", []) + [s for v in e.get("variants", []) for s in v["trace"]]
                + [x["stage"] for x in e.get("dwell", [])] + [c["title"] for c in curated] + [c["where"] for c in curated]
                + [str(v) for x in people["configured"] for v in x.values()])
    shape = {"key": e["key"], "name": e["name"], "main": fl.get("main", []), "variants": context["flow"]["variants"],
             "longest": context["flow"]["stages_longest_first"][:2], "loops": context["flow"]["loops"],
             "related": [x["key"] for x in related], "dirs": dirs[:3], "cases": sorted(c["ref"] for c in cases),
             "people": canon(people), "curated": canon(curated), "magnitudes": card_magnitudes(e)}
    return context, allowed, literals, shape


def card_shape(e: dict, cats: dict, titles: dict) -> str:
    _, _, _, shape = card_context(e, cats, titles)
    return digest({**shape, "facts": sorted(card_facts(e))})[:16]


class CategoryCard(Task):
    id = "category_card"
    describe = "카테고리 해설 — 흐름·막히는 곳·조심할 점·볼 곳·접점 (수치는 사실 id 자리표시)"

    def schema(self, ctx):
        return {"type": "object", "additionalProperties": False, "required": ["paragraphs", "watch"],
                "properties": {"paragraphs": {"type": "array", "minItems": 1, "maxItems": 3, "items": STMT},
                               "watch": {"type": "array", "minItems": 1, "maxItems": 4, "items": STMT}}}

    def targets(self, ctx: Ctx, args) -> list[dict]:
        cats = ctx.cm["categories"]
        if getattr(args, "key", None):
            from ..guide import resolve
            k = resolve(ctx.cm, args.key)
            if not k:
                raise RuntimeError(f"카테고리 '{args.key}' 가 없다 → atlas guide --list")
            return [cats[k]]
        rows = [e for e in cats.values() if e.get("flow") and not e["key"].startswith("(미분류)")]
        return sorted(rows, key=lambda e: (-e["profile"]["n"], e["key"]))

    def items(self, ctx, args):
        cats = ctx.cm["categories"]
        out = []
        for e in self.targets(ctx, args):
            facts = card_facts(e)
            context, allowed, literals, shape = card_context(e, cats, ctx.titles)
            prompt = ("아래는 업무 카테고리 하나의 사실·맥락·참조 목록이다. 시스템 지시대로 JSON 으로 답한다.\n\n"
                      "## 사실 (본문에는 {id} 로만 쓴다)\n" + "\n".join(f"- {k}: {v[0]} = {v[1]}" for k, v in facts.items())
                      + "\n\n## 맥락\n```json\n" + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
                      + "\n```\n\n## refs 에 쓸 수 있는 것\n" + ", ".join(sorted(allowed | set(facts))) + "\n")
            out.append(Item(e["key"], prompt, {**shape, "facts": sorted(facts)},
                            {"allowed": allowed | set(facts), "facts": set(facts), "literals": literals}))
        return out

    def check(self, ctx, item, out):
        c = item.check
        kw = {"allowed": c["allowed"], "facts": c["facts"], "literals": c["literals"], "generic": set(SECTIONS)}
        paras, w1 = _stmts(out.get("paragraphs"), **kw)
        watch, w2 = _stmts(out.get("watch"), **kw)
        problems = w1 + w2
        if not paras or not watch:
            return None, problems + ["남은 문단이나 주의 항목이 없다"]
        res = {"paragraphs": paras, "watch": watch}
        return {**res, "digest": digest(res)[:16]}, problems

    def sink(self, ctx, accepted, args):
        f = llm_dir(ctx.p) / "category_card.json"
        data = _read_json(f) or {"task": self.id, "cards": {}}
        for item, out, meta in accepted:
            data["cards"][item.key] = {"shape": digest(item.shape)[:16], **out, "model": meta, "task_version": self.version,
                                       "created": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        data["cards"] = dict(sorted(data["cards"].items()))
        write_text(f, json.dumps(data, ensure_ascii=False, indent=1) + "\n")
        return [f"{len(accepted)}개 카드 → {f}"]


def fill(text: str, facts: dict[str, tuple[str, str]]) -> str:
    return PLACEHOLDER.sub(lambda m: facts[m.group(1)][1] if m.group(1) in facts else m.group(0), text)


def rendered_cards(p, cm: dict, titles: dict | None = None) -> dict:
    """지금 모델로 채운 카드 — 카테고리의 모양이 카드를 쓸 때와 같고, 사람이 '틀림'이라 하지 않은 것만."""
    data = _read_json(llm_dir(p) / "category_card.json") or {}
    if not data.get("cards"):
        return {}
    if titles is None:
        from ..guide import _titles
        titles = _titles(p)
    wrong = {tuple(x) for x in ((_read_json(p.model / "catalog.json") or {}).get("llm") or {}).get("suppressed", [])}
    curated = re.compile(r"^ref\d+$")
    cats = cm["categories"]
    out = {}
    for key, card in data["cards"].items():
        e = cats.get(key)
        if not e or ("category_card", key, card.get("digest")) in wrong:
            continue
        facts = card_facts(e)
        if card_shape(e, cats, titles) != card["shape"]:
            continue
        refs = sorted({r for s in card["paragraphs"] + card["watch"] for r in s["refs"]
                       if r not in facts and r not in SECTIONS and not curated.match(r)})
        out[key] = {"paragraphs": [fill(s["text"], facts) for s in card["paragraphs"]],
                    "watch": [fill(s["text"], facts) for s in card["watch"]], "refs": refs,
                    "model": (card.get("model") or {}).get("model", "") if isinstance(card.get("model"), dict) else card.get("model", ""),
                    "created": card.get("created", ""), "shape": card["shape"], "digest": card.get("digest", "")}
    return out


# · 트랙 A 주석
def codebook_for_prompt(cb) -> str:
    import yaml
    keep = ("id", "label", "short", "use_when", "not_when", "signals")
    trim = lambda items: [{k: v for k, v in i.items() if k in keep and v} for i in items or [] if i.get("status", "active") == "active"]
    data = {"version": cb.version, "track": cb.data.get("track"),
            "reserved_values": trim(cb.data.get("reserved_values")),
            "confidence_scale": trim(cb.data.get("confidence_scale")), "fit_scale": trim(cb.data.get("fit_scale")),
            "facets": [{"id": f["id"], "label": f.get("label"), "question": f.get("question"), "multi": f.get("multi", False),
                        "values": trim(f.get("values"))} for f in cb.annotated_facets()],
            "archetypes": trim(cb.data.get("archetypes")),
            "vocabularies": {k: trim(v) for k, v in (cb.data.get("vocabularies") or {}).items()}}
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000)


class Annotate(Task):
    id = "annotate"
    describe = "트랙 A 주석 — WU 입력 문서 + 코드북 → 주석 (사후 검증은 빌드가)"

    def schema(self, ctx):
        return json.loads((ctx.p.root / "method" / "annotation.schema.json").read_text(encoding="utf-8"))

    def items(self, ctx, args):
        from .. import render
        from ..common import Codebook, sha256_text
        cb = Codebook.load(ctx.p)
        book = codebook_for_prompt(cb)
        stamp = {"annotate_recipe": f"annotate@{self.version}", "codebook_version": cb.version,
                 "codebook": digest(book)[:16], "prompt": digest(self.system(ctx))[:16],
                 "llm_model": str((ctx.cfg.get("llm") or {}).get("model", ""))}      # 설정한 모델을 바꾸면 다시 주석한다
        done = {u["wu_id"]: (u.get("build") or {}) for u in read_jsonl(ctx.p.annotated)} if ctx.p.annotated.exists() else {}
        units = ctx.units("stitched")
        if getattr(args, "key", None):
            units = [u for u in units if u["wu_id"] == args.key or u["anchors"][0] == args.key]
            if not units:
                raise RuntimeError(f"작업단위 '{args.key}' 가 없다 (wu:issue:ABC-1 또는 issue:ABC-1)")
        else:
            units = sorted((u for u in units if u["span"].get("end")), key=lambda u: (u["span"]["end"], u["wu_id"]), reverse=True)
        out = []
        for u in units:
            doc = render.render(u, ctx.obs_by_id, ctx.excerpts)
            h = sha256_text(doc)
            prev = done.get(u["wu_id"], {})
            if not getattr(args, "refresh", False) and not getattr(args, "key", None) and prev.get("input_hash") == h \
                    and all(prev.get(k) == v for k, v in stamp.items()):     # 입력·레시피·코드북·프롬프트·모델이 같으면 이미 했다
                continue
            prompt = (f"# 코드북 (버전 {cb.version}, 트랙 {cb.data.get('track')})\n\n```yaml\n{book}```\n\n# WU 입력 문서\n\n{doc}\n\n"
                      f"출력의 wu_id 는 `{u['wu_id']}`, codebook_version 은 {cb.version} 이다.")
            out.append(Item(u["wu_id"], prompt, {"input_hash": h, "codebook": stamp["codebook"]},
                            {"unit": {"wu_id": u["wu_id"], "members": u["members"]}, "input_hash": h, "stamp": stamp}))
        return out

    def check(self, ctx, item, out):
        from .. import validate
        from ..common import Codebook
        problems = []
        if out.get("wu_id") != item.key:
            problems.append(f"wu_id 를 {item.key} 로 바로잡음 (받은 값 {out.get('wu_id')})")
            out = {**out, "wu_id": item.key}
        tally = validate._Tally()
        validate.validate_unit({**item.check["unit"], "annotation": out}, Codebook.load(ctx.p), tally)
        if tally.violated:
            problems.append(f"사후 검증에서 강등될 값 {tally.violated}/{tally.values} ({', '.join(f'{k}×{v}' for k, v in sorted(tally.by_rule.items()))})")
        return out, problems

    def sink(self, ctx, accepted, args):
        f = ctx.p.annotated
        rows = {u["wu_id"]: u for u in read_jsonl(f)} if f.exists() else {}
        for item, out, meta in accepted:
            rows[item.key] = {"wu_id": item.key, "annotation": out,
                              "build": {"input_hash": item.check["input_hash"], **item.check["stamp"], "model": meta.get("model", "")}}
        write_jsonl(f, [rows[k] for k in sorted(rows)])
        return [f"{len(accepted)}건 주석 → {f} (다음 atlas build 가 사후 검증 뒤 반영)"]


# · 주간 요약
class WeeklyDigest(Task):
    id = "weekly_digest"
    describe = "주간 요약 — 경고 · 변화 · 제안 · 질문 (숫자는 입력에 있는 것만)"

    def schema(self, ctx):
        item = {"type": "object", "additionalProperties": False, "required": ["kind", "text", "refs"],
                "properties": {"kind": {"enum": ["주의", "변화", "제안", "질문"]}, **STMT["properties"]}}
        return {"type": "object", "additionalProperties": False, "required": ["headline", "items"],
                "properties": {"headline": STMT, "items": {"type": "array", "minItems": 1, "maxItems": 7, "items": item}}}

    def facts(self, ctx) -> dict[str, str]:
        from .. import catview
        from ..mine.process import PROBLEM
        cat = _read_json(ctx.p.model / "catalog.json") or {}
        gen = _read_json(ctx.p.model / "generation.json") or {}
        cm = ctx.cm
        cats = cm["categories"]
        F: dict[str, str] = {}
        for i, w in enumerate(cat.get("warnings", [])[:8]):
            F[f"W{i + 1}"] = f"[{w['level']}] {w['what']} ({w['evidence']}) → {w['action']}"
        d = gen.get("drift")
        if d:
            F["D1"] = f"이전 세대 대비 주장 +{d['added']} -{d['removed']} · 강해짐 {d['strengthened']} · 약해짐 {d['weakened']}"
        areas = [e for e in cats.values() if e["kind"] == "area" and not e["key"].startswith("(미분류)")]
        for i, e in enumerate(sorted((e for e in areas if e["profile"]["recent_90d"]), key=lambda e: (-e["profile"]["recent_90d"], e["key"]))[:5]):
            F[f"C{i + 1}"] = (f"{e['name']}: 최근 90일 시작 {e['profile']['recent_90d']}건 · 지금 열린 일 {e['profile']['open_now']}건"
                              f" · 소요 중앙 {dur(e['profile']['duration_h_p50']) or '-'}")
        for i, e in enumerate(sorted((e for e in areas if (e.get("open") or {}).get("n")), key=lambda e: (-e["open"]["n"], e["key"]))[:3]):
            top = e["open"]["stages"][0]
            F[f"B{i + 1}"] = f"{e['name']}: 열린 일 {e['open']['n']}건 — 가장 많은 단계 {top['stage']} {top['n']}건(그 단계에서 중앙 {dur(top['age_h_p50'])})"
        as_of = _parse(cm.get("as_of"))
        recent = []
        for u in ctx.units():
            end = _parse(u["span"].get("end"))
            if end and as_of and end <= as_of and (as_of - end).days <= 21 and u["stats"].get("outcome") in PROBLEM:
                recent.append(u)
        for i, u in enumerate(sorted(recent, key=lambda u: (_parse(u["span"]["end"]), u["wu_id"]), reverse=True)[:5]):
            anchor, title = ctx.titles.get(u["wu_id"], (u["anchors"][0], ""))
            best = catview.best(cm, catview.for_wu(cm, u["wu_id"]))
            F[f"P{i + 1}"] = f"{anchor} {title[:80]} — {u['stats']['outcome']} ({u['span']['end'][:10]})" + (f" · {best['name']}" if best else "")
        sg = (cm.get("suggestions") or {}).get("paths") or []
        if sg:
            F["S1"] = f"경로 규칙 후보 {len(sg)}개 — 미분류 {sum(s['covers'] for s in sg)}건을 덮는다 (data/reports/guides/README.md)"
        inbox = ctx.p.root / "inbox"
        for i, f in enumerate(sorted(inbox.glob("*.md"), reverse=True)[:5] if inbox.exists() else []):    # 최근 것부터
            first = next((ln.strip("# ").strip() for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()), "")
            F[f"I{i + 1}"] = f"수신함 {f.name}: {first[:100]}"
        return F

    def items(self, ctx, args):
        F = self.facts(ctx)
        as_of = _parse(ctx.cm.get("as_of"))
        y, w, _ = as_of.isocalendar() if as_of else (0, 0, 0)
        week = f"{y}-W{w:02d}"                                 # 주에 한 번 — 매일 빌드해도 그 주의 첫 답을 다시 쓴다
        prompt = ("아래는 이번 주 빌드에서 뽑은 사실이다(id: 내용). 시스템 지시대로 JSON 으로 답한다.\n\n"
                  + "\n".join(f"- {k}: {v}" for k, v in F.items()) + "\n")
        return [Item(week, prompt, {"week": week}, {"allowed": set(F), "numbers": {k: numbers_in(v) for k, v in F.items()}, "facts": F})]

    def check(self, ctx, item, out):
        c = item.check
        head, w1 = _stmts([out.get("headline") or {}], allowed=c["allowed"], numbers=c["numbers"])
        items, w2 = [], []
        for x in out.get("items") or []:
            ok, why = check_stmt(x, c["allowed"], numbers=c["numbers"])
            if ok:
                items.append({"kind": x["kind"], **ok})
            else:
                w2.append(f"{why}: {x.get('text', '')[:60]}")
        if not head or not items:
            return None, w1 + w2 + ["머리말이나 항목이 남지 않았다"]
        used = sorted({r for x in [head[0], *items] for r in x["refs"]})
        return {"headline": head[0], "items": items, "facts": {k: c["facts"][k] for k in used}}, w1 + w2   # 그때의 근거를 답과 함께

    def sink(self, ctx, accepted, args):
        out = []
        for item, res, meta in accepted:
            F = res.get("facts") or {}
            lines = [f"<!-- atlas:generated 주간 요약 {item.key} · {meta.get('model', '')} · LLM 이 쓰고 근거 검증을 통과한 것 — 고치지 않는다 -->", "",
                     f"# 주간 요약 {item.key}", "", f"**{res['headline']['text']}**", ""]
            for x in res["items"]:
                lines.append(f"- **{x['kind']}** {x['text']} _(근거: {', '.join(x['refs'])})_")
            lines += ["", "## 근거 (답을 받을 때의 사실)", ""] + [f"- `{k}` {v}" for k, v in F.items()]
            f = ctx.p.reports / f"{item.key}-digest.md"
            write_text(f, "\n".join(lines) + "\n")
            out.append(f"주간 요약 → {f}")
        return out


# · 작업 기록 초안
class WorkDraft(Task):
    id = "work_draft"
    describe = "작업 기록 초안 — 관측 문서 → 문제·처리·결과·회고 (사람이 고쳐 WORK.md 로)"

    def schema(self, ctx):
        lst = lambda lo, hi: {"type": "array", "minItems": lo, "maxItems": hi, "items": STMT}
        return {"type": "object", "additionalProperties": False,
                "required": ["problem", "process", "result", "retro", "open_questions"],
                "properties": {"problem": STMT, "process": lst(1, 5), "result": STMT, "retro": lst(0, 3),
                               "open_questions": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 300}}}}

    def items(self, ctx, args):
        from .. import config as cfgmod, context, render, workspace as wsmod
        where = getattr(args, "work", None) or "."
        loc = wsmod.locate(str(Path(where).resolve()), wsmod.sources(ctx.cfg, ctx.p.root))
        if not loc:
            raise RuntimeError(f"'{where}' 는 설정된 작업 공간의 작업 폴더가 아니다 (atlas.toml [[sources.workspace]])")
        src, wdir, rel = loc
        rec = wsmod.read_record(wdir / src.record)
        pat = cfgmod.Patterns(ctx.cfg)
        keys = pat.issue_keys(*(rec.keys() if rec else [])) or pat.issue_keys(wdir.name)
        units = {u["wu_id"]: u for u in ctx.units()}
        wid = wsmod.resolve(src, wdir, rel)
        u = next((units[w] for w in [*(f"wu:issue:{k}" for k in keys), f"wu:work:{wid}"] if w in units), None)
        if not u:
            raise RuntimeError("이 작업이 아직 관측되지 않았다 → atlas collect workspace · atlas build 뒤 다시")
        doc = render.render(u, ctx.obs_by_id, ctx.excerpts)
        cat = ""
        try:
            info = context.category_info(ctx.cm, u["wu_id"])
            cat = "\n".join(context.category_lines(info)) if info else ""
        except RuntimeError:
            pass
        prompt = f"# 이 일의 관측 문서\n\n{doc}\n\n# 이 일이 든 업무 카테고리\n\n{cat or '(없음)'}\n"
        members = {m["obs_id"] for m in u["members"]}
        nums: dict[str, set[str]] = {}
        for line in doc.splitlines():                          # 관측 한 줄 = "- 시각 · 종류 · 역할 · 키[…] · obs_id · 발췌"
            parts = line.split(" · ")
            for i, part in enumerate(parts):
                if part in members:
                    nums[part] = numbers_in(" · ".join(parts[i + 1:]), parts[3] if len(parts) > 3 else "")
        return [Item(u["wu_id"], prompt, {"doc": digest(doc)[:16], "cat": digest(cat)[:16]},
                     {"allowed": members, "numbers": nums, "dir": str(wdir)})]

    def check(self, ctx, item, out):
        c = item.check
        kw = {"allowed": c["allowed"], "numbers": c["numbers"]}
        prob, w1 = _stmts([out.get("problem") or {}], **kw)
        proc, w2 = _stmts(out.get("process"), **kw)
        res, w3 = _stmts([out.get("result") or {}], **kw)
        retro, w4 = _stmts(out.get("retro"), **kw)
        problems = w1 + w2 + w3 + w4
        if not prob or not proc or not res:
            return None, problems + ["문제·처리·결과 중 남지 않은 칸이 있다"]
        return {"problem": prob[0], "process": proc, "result": res[0], "retro": retro,
                "open_questions": [q.strip() for q in out.get("open_questions") or [] if q.strip()]}, problems

    def markdown(self, item: Item, res: dict, meta: dict) -> str:
        from ..workspace import GENERATED_MARK
        ref = lambda s: f" _({', '.join(x.split('#')[-1] for x in s['refs'])})_"
        lines = [f"{GENERATED_MARK} 작업 기록 초안 · {item.key} · {meta.get('model', '')} — 사람이 읽고 고쳐 WORK.md 로 옮긴다. 이 파일은 관측하지 않는다 -->",
                 "", "## 문제", "", res["problem"]["text"] + ref(res["problem"]), "", "## 처리", ""]
        lines += [f"{i + 1}. {s['text']}{ref(s)}" for i, s in enumerate(res["process"])]
        lines += ["", "## 결과", "", res["result"]["text"] + ref(res["result"]), ""]
        if res["retro"]:
            lines += ["## 회고", ""] + [f"- {s['text']}{ref(s)}" for s in res["retro"]] + [""]
        if res["open_questions"]:
            lines += ["## 사람이 채울 것", ""] + [f"- [ ] {q}" for q in res["open_questions"]] + [""]
        return "\n".join(lines)

    def sink(self, ctx, accepted, args):
        from ..workspace import DRAFT, write_user_file
        out = []
        for item, res, meta in accepted:
            md = self.markdown(item, res, meta)
            f = Path(item.check["dir"]) / DRAFT
            if getattr(args, "write", False) and not f.exists():
                write_user_file(f, md + "\n")
                out.append(f"초안 → {f}")
            else:                                            # 이미 있으면 덮지 않는다 — 사람이 고쳤을 수 있다
                out.append(md + (f"\n\n({f.name} 가 이미 있어 쓰지 않았다 — 지우고 다시 하면 쓴다)" if getattr(args, "write", False) else ""))
        return out


# · 카테고리 이름 제안
class CategoryNames(Task):
    id = "category_names"
    describe = "카테고리 이름·정의 제안 → inbox/ (사람이 골라 categories.yaml 의 names 로)"

    def schema(self, ctx):
        row = {"type": "object", "additionalProperties": False, "required": ["key", "name", "definition", "refs"],
               "properties": {"key": {"type": "string"}, "name": {"type": "string", "maxLength": 40},
                              "definition": {"type": "string", "maxLength": 300},
                              "refs": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 6}}}
        return {"type": "object", "additionalProperties": False, "required": ["names"],
                "properties": {"names": {"type": "array", "maxItems": 60, "items": row}}}

    def items(self, ctx, args):
        from ..taxonomy import load_data
        named = set((load_data(ctx.p.root).get("names") or {}))
        cats = ctx.cm["categories"]
        rows, allowed, literals = [], set(), []
        min_n = int(ctx.cfg["mining"].get("category_min_n", 5))
        for e in sorted(cats.values(), key=lambda e: (-e["profile"]["n"], e["key"])):
            if e["kind"] != "area" or e["value"] in named or e["key"].startswith("(") or e["profile"]["n"] < min_n:
                continue
            titles = [ctx.titles.get(x["wu_id"], ("", ""))[1][:100] for x in (e.get("references") or {}).get("representative", [])]
            dirs = [d["dir"] for d in e["code"]["dirs"][:3]]
            rows.append({"key": e["key"], "parent": e["parent"], "dirs": [{"id": f"{e['key']}#d{i + 1}", "path": d} for i, d in enumerate(dirs)],
                         "titles": [{"id": f"{e['key']}#t{i + 1}", "title": t} for i, t in enumerate(titles) if t]})
            allowed |= {e["key"]} | {d["id"] for d in rows[-1]["dirs"]} | {t["id"] for t in rows[-1]["titles"]}
            literals += [e["key"], *dirs, *titles]
            if len(rows) >= 40:
                break
        if not rows:
            return []
        prompt = "아래 카테고리마다 이름과 정의를 제안한다. 시스템 지시대로 JSON 으로 답한다.\n\n```json\n" + \
                 json.dumps(rows, ensure_ascii=False, indent=1) + "\n```\n"
        return [Item("names", prompt, {"rows": rows}, {"allowed": allowed, "keys": {r["key"] for r in rows}, "literals": literals})]

    def check(self, ctx, item, out):
        kept, problems = [], []
        for r in out.get("names") or []:
            if r["key"] not in item.check["keys"]:
                problems.append(f"모르는 키 {r['key']}")
                continue
            ok, why = check_stmt({"text": f"{r['name']} — {r['definition']}", "refs": r["refs"]}, item.check["allowed"],
                                 facts=set(), literals=item.check["literals"])
            if ok:
                kept.append({"key": r["key"], "name": r["name"].strip(), "definition": r["definition"].strip(), "refs": ok["refs"]})
            else:
                problems.append(f"{why}: {r['key']}")
        return ({"names": kept} if kept else None), problems + ([] if kept else ["남은 제안이 없다"])

    def sink(self, ctx, accepted, args):
        out = []
        for item, res, meta in accepted:
            if meta.get("cached"):                           # 이미 수신함에 낸 답 — 같은 제안을 또 쌓지 않는다
                continue
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
            f = ctx.p.root / "inbox" / f"{stamp}-category-names.md"
            lines = [f"# 카테고리 이름 제안 ({meta.get('model', '')}, LLM — 근거 검증 통과)", "",
                     "사람이 골라 고친 뒤 `method/categories.yaml` 의 `names` 로 옮긴다. 자동으로 적용하지 않는다.", "", "```yaml", "names:"]
            lines += [f'  {json.dumps(r["key"], ensure_ascii=False)}: {json.dumps(r["name"], ensure_ascii=False)}'
                      f'    # {" ".join(r["definition"].split())}' for r in res["names"]]
            lines += ["```", ""]
            write_text(f, "\n".join(lines))
            out.append(f"{len(res['names'])}개 제안 → {f}")
        return out


TASKS: dict[str, Task] = {t.id: t for t in (CategoryCard(), Annotate(), WeeklyDigest(), WorkDraft(), CategoryNames())}
