"""업무 카테고리 테스트 — 분류 규칙(taxonomy) · 카테고리 모델(흐름 · 순서 · 관계 · 참조점 · 접점) · 소비 채널.

합성 시나리오: 결제(pay.api · pay.batch)와 인증(auth) 두 영역, 상태 전이·커밋·재오픈·끝난 뒤 정리·열린 일·링크·미분류 MR.
실행: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import datetime as dt
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from atlas import pipeline, stitch, taxonomy as T  # noqa: E402
from atlas.common import paths  # noqa: E402
from atlas.mine import category as K, corpus as corpusmod  # noqa: E402
from atlas.store import append_observations  # noqa: E402
from test_engine import cfg_for, commit, issue_created, issue_event, make_root, ob, ts  # noqa: E402

TOML = "[mining]\ncategory_min_n = 2\ncategory_recent_days = 0\n"


def trans(key, n, t, to, frm, role="unknown", done=False, reopen=False):
    o = issue_event(key, n, "issue.transition", t, labels={"status": [to], "status_from": [frm]},
                    metrics={"to_done": done, "reopen": reopen})
    o["actor_role"] = role
    return o


def created(key, t, comps, type_="Bug", role="planner"):
    o = issue_created(key, t, type_=type_, comps=comps)
    o["actor_role"] = role
    return o


def comment(key, n, t, role="unknown"):
    o = ob(f"issue:trk:{key}#c{n}", "issue", "issue.comment", t, objects=[{"type": "issue", "id": key}],
           keys={"issue_keys": [key]}, metrics={"chars": 10}, instance="trk", role=role)
    return o


def scenario() -> list[dict]:
    out = []
    for i in range(6):                                     # 결제 API 결함: 생성 → 진행 → 커밋 → 리뷰 → 완료(QA) → (끝난 뒤) 닫힘
        k, d = f"PAY-{i}", i * 5
        out += [created(k, ts(d), ["pay.api"]),
                trans(k, 1, ts(d, 11), "In Progress", "To Do", "self"),
                commit(f"p{i}", ts(d, 12), ["src/pay/api/Handler.java", f"src/pay/api/H{i}Test.java"], {"issue_keys": [k]}),
                comment(k, 1, ts(d, 13)),
                trans(k, 2, ts(d, 14), "In Review", "In Progress", "self"),
                trans(k, 3, ts(d + 1), "Done", "In Review", "qa", done=True),
                trans(k, 4, ts(d + 60), "Closed", "Done", "unknown")]
    out += [trans("PAY-1", 5, ts(6, 15), "Reopened", "Done", "qa", reopen=True),     # 재작업 고리
            trans("PAY-1", 6, ts(6, 16), "In Progress", "Reopened", "self"),
            trans("PAY-1", 7, ts(6, 20), "Done", "In Progress", "qa", done=True)]
    out = [o for o in out if o["obs_id"] != "issue:trk:PAY-1#h4:0"]                  # PAY-1 은 닫힘 없이
    for i in range(10, 13):                                # 결제 배치 개선
        k = f"PAY-{i}"
        out += [created(k, ts(i * 3), ["pay.batch"], "Improvement"),
                commit(f"b{i}", ts(i * 3, 12), ["src/pay/batch/Job.java"], {"issue_keys": [k]}),
                trans(k, 1, ts(i * 3 + 1), "Done", "To Do", "self", done=True)]
    for i in range(5):                                     # 인증 결함 — AUTH-0 은 PAY-0 을 막는다, AUTH-1 은 결제 파일도 만진다
        k, d = f"AUTH-{i}", 40 + i * 3
        files = ["src/auth/Login.java"] + (["src/pay/api/Handler.java"] if i == 1 else [])
        out += [created(k, ts(d), ["auth"]),
                trans(k, 1, ts(d, 11), "In Progress", "To Do", "self"),
                commit(f"a{i}", ts(d, 12), files, {"issue_keys": [k]}),
                trans(k, 2, ts(d + 1), "Done", "In Progress", "qa", done=True)]
    out.append(issue_event("AUTH-0", 9, "issue.linked", ts(40, 13), keys={"links": ["blocks|PAY-0"]}))
    out += [created("PAY-20", ts(70), ["pay.api", "auth"]),                          # 두 영역에 든다
            commit("x20", ts(70, 12), ["src/pay/api/Handler.java", "src/auth/Login.java"], {"issue_keys": ["PAY-20"]}),
            trans("PAY-20", 1, ts(71), "Done", "To Do", "qa", done=True)]
    out += [created("PAY-30", ts(250), ["pay.api"]),                                 # 열린 일 — 리뷰에 머물러 있다
            trans("PAY-30", 1, ts(250, 11), "In Progress", "To Do", "self"),
            trans("PAY-30", 2, ts(251), "In Review", "In Progress", "self")]
    out += [created("OPS-1", ts(80), [], "Task")]                                    # 컴포넌트 없는 일
    out[-1]["labels"].pop("component")
    for i in range(3):                                     # 키 없는 MR — 결제 API 파일만 만진다 → 경로 규칙 후보
        out.append(commit(f"m{i}", ts(90 + i), ["src/pay/api/Other.java"], {"mr_ids": [f"repo!{i}"]}))
    return out


class CategoryCase(unittest.TestCase):
    def setUp(self):
        self.root = make_root(TOML)
        self.cfg = cfg_for(self.root)
        self.as_of = dt.datetime.fromisoformat(ts(300))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def model(self, obs=None, data=None):
        obs = obs if obs is not None else scenario()
        units, _ = stitch.stitch(obs, self.cfg, self.as_of)
        corp = corpusmod.build(obs, units, self.cfg, self.as_of)
        tax = T.Taxonomy(data or T.load_data(self.root))
        return K.mine(units, corp, tax, self.cfg), units


class TestTaxonomy(unittest.TestCase):
    def tax(self, **over):
        return T.Taxonomy({**T.DEFAULT, **over})

    def test_split_chain_drops_star_and_merges(self):
        t = self.tax(merge={"Lang": "lang.*"})
        self.assertEqual(t.chain("lang.text.translate.*"), ["lang", "lang.text", "lang.text.translate"])
        self.assertEqual(t.chain("Lang"), ["lang"])
        self.assertEqual(t.chain("backend/api"), ["backend", "backend/api"])
        self.assertEqual(t.label("lang.text.translate", "lang.text"), "translate")

    def test_parents_add_levels_and_cycles_stop(self):
        t = self.tax(parents={"java client": "clients", "clients": "sdk", "sdk": "java client"})
        self.assertEqual(t.chain("java client"), ["sdk", "clients", "java client"])

    def test_fold_case_uses_most_common_spelling(self):
        t = self.tax().fit([T.WuFacts({"component": ["lang"]})] * 3 + [T.WuFacts({"component": ["Lang"]})])
        self.assertEqual(t.place(T.WuFacts({"component": ["Lang"]})).areas, [["lang"]])

    def test_sources_in_order_and_uncategorized(self):
        t = self.tax(paths={"src/pay/**": "pay"})
        self.assertEqual(t.place(T.WuFacts({}, ["src/pay/a.py"])).source, "path")
        self.assertEqual(t.place(T.WuFacts({"component": ["auth"]}, ["src/pay/a.py"])).areas, [["auth"]])
        self.assertEqual(t.place(T.WuFacts({}, [], {"tags": ["infra"]})).areas, [["infra"]])
        self.assertEqual(t.place(T.WuFacts({})).areas, [[T.UNCATEGORIZED]])
        self.assertEqual(t.place(T.WuFacts({}, archetype="residual")).areas, [[T.UNCATEGORIZED]])
        f = T.WuFacts({"component": ["a", "b"], "type": ["Bug"]})
        self.assertEqual(t.place(f).areas, [["a"], ["b"]])
        self.assertEqual(t.place(f).facets, {"kind": ["Bug"]})

    def test_facet_merge_and_keys(self):
        t = self.tax(facets=[{"id": "kind", "from": ["type"], "merge": {"Defect": "Bug"}},
                             {"id": "prio", "from": ["priority"]}])
        self.assertEqual(t.place(T.WuFacts({"type": ["Defect"]})).facets["kind"], ["Bug"])
        self.assertEqual(t.facet_key("pay", "kind", "Bug"), "pay / Bug")
        self.assertEqual(t.facet_key("pay", "prio", "Major"), "pay / prio:Major")

    def test_contacts_inherit_down_the_tree(self):
        t = self.tax(contacts={"pay": [{"team": "결제팀"}], "pay.api / Bug": [{"role": "qa", "when": "완료 전"}],
                               "pay / Bug": [{"channel": "#pay-bugs"}], T.ALL: [{"channel": "#dev"}]})
        got = t.contacts("pay.api / Bug", ["pay", "pay.api"], area="pay.api")
        self.assertEqual([c["from"] for c in got], ["pay.api / Bug", "pay / Bug", "pay", T.ALL])
        self.assertEqual(t.contacts("pay.api", ["pay", "pay.api"])[0]["team"], "결제팀")

    def test_problems_reject_unknown_contact_fields(self):
        bad = {**T.DEFAULT, "contacts": {"pay": [{"name": "홍길동"}]}, "area": {"from": ["component"], "split": "["}}
        msgs = " ".join(T.problems(bad))
        self.assertIn("name", msgs)
        self.assertIn("split", msgs)
        self.assertEqual(T.problems(T.DEFAULT), [])
        self.assertEqual(T.human_entries(T.DEFAULT), [])


class TestCategoryModel(CategoryCase):
    def setUp(self):
        super().setUp()
        self.m, self.units = self.model()
        self.c = self.m["categories"]

    def test_tree_counts_roll_up_and_multi_membership(self):
        c = self.c
        self.assertEqual(c["pay"]["children"], ["pay.api", "pay.batch"])
        api = set(w for w, ks in self.m["members"].items() if "pay.api" in ks)
        batch = set(w for w, ks in self.m["members"].items() if "pay.batch" in ks)
        self.assertEqual(c["pay"]["profile"]["n"], len(api | batch))
        self.assertIn("wu:issue:PAY-20", api)
        self.assertIn("auth", self.m["members"]["wu:issue:PAY-20"])
        self.assertEqual(c["pay"]["profile"]["direct"], 0)
        self.assertEqual(c[T.ALL]["profile"]["n"], len(self.units))
        self.assertIn(T.UNCATEGORIZED, c)
        self.assertIn("pay.api / Bug", c["pay.api"]["cells"])
        self.assertEqual(c["pay.api / Bug"]["parent"], "pay.api")
        self.assertEqual(c["pay.api"]["label"], "api")

    def test_flow_ends_at_resolution_and_marks_rework(self):
        f = self.c["pay.api"]["flow"]
        self.assertEqual(f["main"], ["생성", "In Progress", "커밋", "In Review", "Done"])
        ids = [n["id"] for n in f["nodes"]]
        self.assertNotIn("Closed", ids)                                        # 끝난 뒤의 정리는 흐름이 아니다
        loop = [(e["from"], e["to"]) for e in f["edges"] if e["loop"]]           # 보통의 순서를 거슬러 되돌아가는 이음만 고리
        self.assertEqual(loop, [("Reopened", "In Progress")])
        self.assertTrue(f["main"][-1] == "Done" and f["main_n"] == 5)              # 가장 흔한 길은 실제 변형(5건이 그대로 따랐다)
        self.assertFalse(next(e for e in f["edges"] if e["from"] == "커밋")["loop"])
        self.assertEqual(f["n_traces"], 7)                                     # 열린 PAY-30 은 흐름에 없다
        after = {r["activity"]: r for r in self.c["pay.api"]["after_end"]}
        self.assertEqual(after["Closed"]["n"], 5)
        self.assertEqual(after["Closed"]["hours_p50"], round(59 * 24.0, 1))

    def test_open_work_is_counted_by_current_stage(self):
        op = self.c["pay.api"]["open"]
        self.assertEqual(op["stages"][0]["stage"], "In Review")
        self.assertEqual(self.m["open"]["wu:issue:PAY-30"]["stage"], "In Review")

    def test_sequence_names_who_does_each_step(self):
        steps = {s["activity"]: s for s in self.c["pay.api"]["sequence"]["steps"]}
        self.assertEqual(steps["Done"]["actor"], "qa")
        self.assertEqual(steps["생성"]["actor"], "planner")
        self.assertEqual(steps["커밋"]["system"], "저장소")
        self.assertEqual(steps["In Progress"]["gap_h_p50"], 1.0)

    def test_dwell_and_variants(self):
        dw = {r["stage"]: r for r in self.c["pay.api"]["dwell"]}
        self.assertEqual(dw["In Review"]["hours_p50"], 20.0)
        v = self.c["pay.api"]["variants"]
        self.assertEqual(v[0]["trace"], ["생성", "In Progress", "커밋", "In Review", "Done"])
        self.assertEqual(sum(x["n"] for x in v), 7)

    def test_roles_and_observed_contacts(self):
        roles = {r["role"]: r for r in self.c["pay.api"]["roles"]}
        self.assertEqual(roles["qa"]["first_stage"], "Done")
        self.assertEqual(roles["planner"]["first_stage"], "생성")
        self.assertNotIn("self", [r["role"] for r in self.c["pay.api"]["contacts"]["observed"]])

    def test_relations_links_and_shared_files(self):
        rel = self.c["auth"]["related"]
        self.assertIn(("link", "pay", "blocks"), [(r["kind"], r["key"], r["via"]) for r in rel])
        files = [r for r in self.c["pay.api"]["related"] if r["kind"] == "files"]
        self.assertEqual(files, [])                                            # 깊이 2 의 다른 영역 중 파일을 둘 이상 같이 쓴 곳 없음
        top = [r for r in self.c["pay"]["related"] if r["kind"] == "files"]
        self.assertEqual([(r["key"], r["n"]) for r in top], [("auth", 2)])     # Handler(AUTH-1) · Login(PAY-20)
        self.assertEqual(top[0]["jaccard"], round(2 / (len({"src/pay/api/Handler.java", "src/auth/Login.java", "src/pay/batch/Job.java"}
                                                         | {f"src/pay/api/H{i}Test.java" for i in range(6)}) + 2 - 2), 3))
        code = self.c["pay.api"]["code"]
        self.assertEqual(code["files"][0]["path"], "src/pay/api/Handler.java")

    def test_references(self):
        r = self.c["pay.api"]["references"]
        self.assertEqual([x["outcome"] for x in r["cautionary"]], ["reopened"])
        self.assertTrue(all(x["wu_id"] != "wu:issue:PAY-1" for x in r["recent_clean"]))

    def test_path_suggestion_covers_uncategorized(self):
        model = K.Model(*self._inputs())
        got = K.suggest_paths(model, min_n=3)
        self.assertIn({"glob": "src/pay/api/**", "area": "pay.api"}, [{k: g[k] for k in ("glob", "area")} for g in got])
        self.assertEqual(next(g for g in got if g["glob"] == "src/pay/api/**")["covers"], 3)

    def _inputs(self):
        obs = scenario()
        units, _ = stitch.stitch(obs, self.cfg, self.as_of)
        corp = corpusmod.build(obs, units, self.cfg, self.as_of)
        return units, corp, T.Taxonomy(T.load_data(self.root)), self.cfg

    def test_path_rule_places_uncategorized_work(self):
        m, _ = self.model(data={**T.DEFAULT, "paths": {"src/pay/api/**": "pay.api"}})
        self.assertNotIn("mr:repo!0", [w.split(":", 1)[1] for w, ks in m["members"].items() if T.UNCATEGORIZED in ks])
        self.assertEqual(m["coverage"]["by_source"].get("path"), 3)

    def test_deterministic(self):
        a, _ = self.model()
        b, _ = self.model()
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_recent_window_extends_to_minimum_sample_and_says_so(self):
        self.cfg["mining"]["category_recent_days"] = 250                        # ts(50) 이후에 끝난 pay.api 일은 PAY-20 하나
        self.cfg["mining"]["category_basis_min"] = 4
        b = self.model()[0]["categories"]["pay.api"]["basis"]
        self.assertEqual((b["n"], b["of_ended"], b["extended"]), (4, 7, True))   # 가장 최근에 끝난 4건까지
        self.cfg["mining"]["category_basis_min"] = 1
        b = self.model()[0]["categories"]["pay.api"]["basis"]
        self.assertEqual((b["n"], b["extended"]), (1, False))
        self.assertNotIn("flow", self.model()[0]["categories"]["pay.api"])        # 1건 < min_n(2) — 흐름 없음


class TestCategoryBuild(CategoryCase):
    def test_build_writes_model_and_definition_changes_generation(self):
        p = paths(self.root)
        append_observations(p, scenario())
        with redirect_stdout(io.StringIO()):
            s1 = pipeline.run(p, self.cfg, ts(300), report=False, log=lambda *a: None)
        cats = json.loads((p.model / "categories.json").read_text(encoding="utf-8"))
        self.assertEqual(cats["generation"], s1["generation"])
        (self.root / "method" / "categories.yaml").write_text("paths:\n  'src/pay/api/**': pay.api\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            s2 = pipeline.run(p, self.cfg, ts(300), report=False, log=lambda *a: None)
        self.assertNotEqual(s1["generation"], s2["generation"])


if __name__ == "__main__":
    unittest.main()


class TestReviewFixes(CategoryCase):
    """독립 검토에서 나온 결함의 회귀 테스트."""

    def test_main_path_is_a_real_variant_and_reaches_end(self):
        m, _ = self.model()
        for k, e in m["categories"].items():
            if e.get("flow"):
                traces = [v["trace"] for v in e["variants"]]
                self.assertIn(e["flow"]["main"], traces, k)                     # 아무도 따르지 않은 길을 '가장 흔한 길'로 내지 않는다
                self.assertGreaterEqual(e["flow"]["main_n"], 1)

    def test_bad_definitions_are_reported_not_silently_misread(self):
        bad = {**T.DEFAULT, "area": {"from": "component"}, "facets": [{"label": "x", "from": ["type"]}],
               "parents": {"a": "b", "b": "a"}, "acme_notes": {"billing": "x"}}
        msgs = " ".join(T.problems(bad))
        for needle in ("area.from", "facets 의 id", "순환", "모르는 맨 위 칸"):
            self.assertIn(needle, msgs)
        self.assertIn("area 는", " ".join(T.problems({**T.DEFAULT, "area": "component"})))
        leaky = {**T.DEFAULT, "facets": [{"id": "kind", "from": ["type"], "merge": {"결함(내부)": "Bug"}}], "acme": 1}
        self.assertEqual(T.human_entries(leaky), ["facets.kind.merge", "acme"])  # 템플릿 검사가 잡는다

    def test_build_stops_with_reason_on_bad_definition_and_dates_hash(self):
        from atlas import mine
        p = paths(self.root)
        append_observations(p, scenario())
        (self.root / "method" / "categories.yaml").write_text("area: component\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError) as cm:
            pipeline.run(p, self.cfg, ts(300), report=False, log=lambda *a: None)
        self.assertIn("categories.yaml", str(cm.exception))
        (self.root / "method" / "categories.yaml").write_text(
            "references:\n  pay:\n    - {title: 런북, note: 2026-01-05}\n", encoding="utf-8")      # YAML 날짜도 세대 해시에 들어간다
        self.assertTrue(mine.generation_id([], self.cfg, self.as_of, T.load_data(self.root)).startswith("gen-"))

    def test_merge_folds_case_and_slash_separator_is_safe(self):
        t = T.Taxonomy({**T.DEFAULT, "merge": {"lang": "core"}}).fit([T.WuFacts({"component": ["Lang"]})] * 3)
        self.assertEqual(t.place(T.WuFacts({"component": ["Lang"]})).areas, [["core"]])
        t = T.Taxonomy(T.DEFAULT)
        self.assertEqual(t.chain("a / b"), ["a", "a/b"])                       # 키 구분자(' / ')가 값 안에 생기지 않는다

    def test_svg_markers_are_unique_per_diagram(self):
        import re
        p = paths(self.root)
        append_observations(p, scenario())
        with redirect_stdout(io.StringIO()):
            s = pipeline.run(p, self.cfg, ts(300), report=True, log=lambda *a: None)
        html = Path(s["report"]).read_text(encoding="utf-8")
        ids = re.findall(r"<marker id='([^']+)'", html)
        self.assertTrue(ids)
        self.assertEqual(len(ids), len(set(ids)))                             # 숨겨진 패널의 화살촉을 빌려 쓰지 않는다
        self.assertNotIn("url(#ah)", html)
        self.assertNotIn("<script", html)

    def test_guides_escape_text_and_keep_readme(self):
        from atlas import guide
        self.assertEqual(guide._t("List<String> [a](b) x|y"), "List&lt;String&gt; \\[a\\](b) x\\|y")
        m, _ = self.model()
        m["categories"]["README"] = {**m["categories"]["auth"], "key": "README", "name": "README", "value": "README", "chain": ["README"]}
        m["categories"]["(전체)"]["children"].append("README")
        p = paths(self.root)
        files = [f.name for f in guide.write_all(p, m)]
        self.assertEqual(files.count("README.md"), 1)
        self.assertIn("README-2.md", files)
        self.assertTrue((p.reports / "guides" / "README.md").read_text(encoding="utf-8").count("# 업무 안내서") == 1)

    def test_mermaid_text_is_entity_encoded(self):
        from atlas import diagram as D
        seq = {"participants": ["qa", "트래커"], "steps": [{"activity": 'Blocked; "waiting" #1 <x>|y', "actor": "qa", "actor_share": 1,
                                                           "system": "트래커", "gap_h_p50": None, "stay_h_p50": None, "comments_p50": None}]}
        import re
        line = D.sequence_mermaid(seq).splitlines()[-1]
        self.assertNotRegex(re.sub(r"#\w+;", "", line.split(": ", 1)[1]), r'[;"<>|#]')    # 엔티티 말고는 문법 문자가 없다

    def test_role_mapping_is_validated_and_ordered(self):
        from atlas import config as configmod
        (self.root / "atlas.toml").write_text(TOML + '[identity.roles]\n"hong.gildong" = ["x"]\n', encoding="utf-8")
        configmod._load.cache_clear()
        with self.assertRaises(configmod.ConfigError):
            configmod.load(self.root)
        (self.root / "atlas.toml").write_text(TOML + '[identity.roles]\nqa = ["qa-(team"]\n', encoding="utf-8")
        configmod._load.cache_clear()
        with self.assertRaises(configmod.ConfigError):
            configmod.load(self.root)
        (self.root / "atlas.toml").write_text(TOML + '[identity.roles]\nqa = ["^qa-team@"]\nplanner = ["@acme\\\\.example$"]\n',
                                              encoding="utf-8")
        configmod._load.cache_clear()
        P = configmod.Patterns(configmod.load(self.root))
        self.assertEqual(P.role_of("qa-team@acme.example"), "qa")                # 적은 순서대로 — 좁은 패턴이 먼저
        self.assertEqual(P.role_of("kim@acme.example"), "planner")
        configmod._load.cache_clear()


class TestCategoryConsumers(CategoryCase):
    """카테고리가 사람·Claude 에게 닿는 곳 — 맥락 줄 · 다음 단계 · 템플릿 검사."""

    def setUp(self):
        super().setUp()
        self.p = paths(self.root)
        append_observations(self.p, scenario())
        with redirect_stdout(io.StringIO()):
            pipeline.run(self.p, self.cfg, ts(300), report=False, log=lambda *a: None)

    def test_context_shows_stage_and_typical_next_steps(self):
        from atlas import context
        ctx = context.build(self.p, self.cfg, issue="PAY-30")
        cat = ctx["category"]
        self.assertEqual(cat["key"], "pay.api / Bug")
        self.assertEqual(cat["stage"]["stage"], "In Review")
        self.assertEqual([(x["to"], x["share"]) for x in cat["next"]], [("Done", 1.0)])   # 리뷰 다음은 늘 완료였다
        md = context.render_md(ctx)
        self.assertIn("지금 단계 In Review", md)
        self.assertIn("보통 다음: Done 100%", md)
        self.assertIn("atlas guide --category 'pay.api / Bug'", md)

    def test_next_step_shares_sum_to_one(self):
        from atlas import catview
        m, _ = catview.load(self.p)
        steps = catview.next_steps(m["categories"]["pay.api"], "Done")
        self.assertAlmostEqual(sum(x["share"] for x in steps), 1.0, places=2)

    def test_dwell_counts_only_ended_work(self):
        from atlas import catview
        m, _ = catview.load(self.p)
        dw = {r["stage"]: r for r in m["categories"]["pay.api"]["dwell"]}
        self.assertEqual(dw["In Progress"]["n"], 7)                                  # 열린 PAY-30 의 진행 단계는 세지 않는다

    def test_template_rejects_filled_category_definition(self):
        from unittest import mock
        from atlas import check
        tpl = Path(tempfile.mkdtemp(prefix="atlas-tpl-"))
        try:
            (tpl / "method").mkdir()
            (tpl / "method" / "categories.yaml").write_text("contacts:\n  billing:\n    - {team: 결제팀}\n", encoding="utf-8")
            with mock.patch.object(check, "ROOT", tpl):
                check.errors.clear()
                from atlas.taxonomy import human_entries, load_data
                self.assertEqual(human_entries(load_data(tpl)), ["contacts"])
                check.check_categories()
                self.assertEqual(check.errors, [])                                    # 모양은 맞다 — 템플릿 검사가 막는다
                (tpl / ".template").write_text("", encoding="utf-8")
                for rel in ("method/codebook.yaml", "method/policy.md", "atlas.example.toml"):
                    shutil.copy(REPO / rel, tpl / rel)
                check.check_template()
                self.assertEqual([e.split(" [")[0] for e in check.errors], ["categories.yaml 의 사람이 채우는 칸이 비어 있지 않다"])
        finally:
            check.errors.clear()
            check.warnings.clear()
            shutil.rmtree(tpl, ignore_errors=True)


class TestFlowPrimitives(unittest.TestCase):
    """흐름 계산의 작은 규칙 — 시나리오가 우연히 비껴가지 않게 직접 시험한다."""

    def ev(self, h, act, actor="self", kind="issue.transition"):
        t = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(hours=h)
        return K.Event(t, act, actor, "트래커", kind, f"o{h}")

    def view(self, steps_spec, end_h=100):
        evs = [self.ev(h, a, who) for h, a, who in steps_spec]
        u = {"wu_id": f"w{id(steps_spec)}", "span": {"end": "x", "settled": True}, "stats": {"duration_h": 1.0}}
        return K.WuView(u, evs, K.steps_of(evs), [], evs[0].ts, [], evs[0].ts + dt.timedelta(hours=end_h), [])

    def test_consecutive_runs_collapse(self):
        evs = [self.ev(0, "생성"), self.ev(1, "커밋"), self.ev(2, "커밋"), self.ev(3, "커밋"), self.ev(4, "Done")]
        st = K.steps_of(evs)
        self.assertEqual([s.activity for s in st], ["생성", "커밋", "Done"])
        self.assertEqual((st[1].count, st[1].first, st[1].last), (3, evs[1].ts, evs[3].ts))

    def test_rare_steps_are_pruned_from_ten_traces(self):
        views = [self.view([(0, "생성", "self"), (1, "Done", "qa")]) for _ in range(11)]
        views.append(self.view([(0, "생성", "self"), (1, "Odd", "self"), (2, "Done", "qa")]))
        fl = K.flow(views, 0.05)
        self.assertNotIn("Odd", [n["id"] for n in fl["nodes"]])                       # 12건 중 1건 — 잡음으로 본다
        self.assertEqual(fl["hidden"]["nodes"], 1)
        self.assertEqual(fl["fitness"], round(11 / 12, 3))

    def test_sequence_actor_is_the_most_common(self):
        views = [self.view([(0, "생성", "planner"), (1, "Done", "qa")]) for _ in range(3)]
        views.append(self.view([(0, "생성", "self"), (1, "Done", "self")]))
        fl = K.flow(views, 0.05)
        seq = K.sequence(views, fl, [])
        self.assertEqual([(s["activity"], s["actor"], s["actor_share"]) for s in seq["steps"]],
                         [("생성", "planner", 0.75), ("Done", "qa", 0.75)])
