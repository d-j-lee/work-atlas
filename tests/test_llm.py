"""LLM 작업 테스트 — 검증(근거 · 자리표시 · 숫자) · 백엔드(claude 헤드리스 흉내 · command) · 캐시 · 장부 · 예산 · 카드 렌더링 · 판정.

실제 모델을 부르지 않는다. 가짜 백엔드 스크립트가 작업 스키마를 보고 정해진 답을 낸다.
실행: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from atlas import catview, config as configmod, guide, llm, pipeline  # noqa: E402
from atlas.common import paths, read_jsonl  # noqa: E402
from atlas.store import append_observations  # noqa: E402
from test_category import TOML, scenario  # noqa: E402
from test_engine import make_root, ts  # noqa: E402

FAKE = '''
import json, os, re, sys
argv = sys.argv[1:]
claude = "-p" in argv
if claude:
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    prompt = sys.stdin.read()
    log = os.environ.get("FAKE_LLM_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "prompt_len": len(prompt)}) + "\\n")
else:
    req = json.loads(sys.stdin.read())
    schema, prompt = req["schema"], req["prompt"]
mode = os.environ.get("FAKE_LLM_MODE", "good")
props = schema.get("properties", {})
if "paragraphs" in props:
    out = {"paragraphs": [{"text": "이 일은 {n} 있었고 보통 {main_path} 로 흘러간다.", "refs": ["n", "flow"]},
                          {"text": "숫자를 지어낸 문장 42%.", "refs": ["flow"]}],
           "watch": [{"text": "열린 일 {open_now} 의 단계를 먼저 본다.", "refs": ["open"]},
                     {"text": "주의 사례를 먼저 본다.", "refs": ["cases"]}]}
    if mode == "bad":
        out = {"paragraphs": [{"text": "재오픈은 12% 다.", "refs": ["flow"]}], "watch": [{"text": "없는 근거", "refs": ["nope"]}]}
elif "headline" in props:
    ids = re.findall(r"^- ([A-Z][0-9]+):", prompt, re.M)
    out = {"headline": {"text": "이번 주는 조용하다.", "refs": ids[:1] or ["W1"]},
           "items": [{"kind": "변화", "text": "최근 사후 문제 사례가 있다.", "refs": ids[:1] or ["W1"]},
                     {"kind": "주의", "text": "열린 일 9999건이 쌓였다.", "refs": ids[:1] or ["W1"]}]}
elif "problem" in props:
    ids = re.findall(r"(issue:trk:PAY-30#[a-z0-9:]+)", prompt)
    out = {"problem": {"text": "리뷰에서 멈춘 결제 결함이다.", "refs": ids[:1]},
           "process": [{"text": "진행으로 옮기고 리뷰에 올렸다.", "refs": ids[:2]}, {"text": "근거 없는 단계", "refs": ["x"]}],
           "result": {"text": "아직 리뷰 중이다.", "refs": ids[-1:]}, "retro": [], "open_questions": ["리뷰 의견은?"]}
elif "names" in props:
    keys = re.findall(r'"key":"([^"]+)"', prompt.replace(" ", ""))
    out = {"names": [{"key": k, "name": "이름 " + k, "definition": k + " 의 일", "refs": [k]} for k in keys] +
                    [{"key": "없는키", "name": "x", "definition": "x", "refs": ["없는키"]}]}
else:
    wu = re.search(r"wu_id 는 `([^`]+)`", prompt).group(1)
    claim = lambda v: {"value": v, "confidence": "low", "evidence": [], "candidate_note": ""}
    out = {"wu_id": wu, "codebook_version": 0, "title": {"value": "제목", "confidence": "low", "evidence": []},
           "summary": {"value": "요약", "confidence": "low", "evidence": []},
           "facets": {"origin": claim("defect_report"), "nature": claim("unknown"), "risk": {**claim(["unknown"])},
                      "failure_class": claim("unknown"), "scale": claim("unknown")},
           "archetype": {"value": "unknown", "fit": "weak", "evidence": [], "candidate_note": ""},
           "hidden_subtasks": [], "pitfalls": [], "moves": [], "open_questions": []}
if mode == "error":
    print(json.dumps({"is_error": True, "subtype": "error_max_budget_usd", "result": "budget"}))
    sys.exit(1)
if claude:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "", "structured_output": out,
                      "total_cost_usd": 0.02, "modelUsage": {"fake-sonnet": {}}}))
else:
    print(json.dumps({"output": out, "cost_usd": 0.01, "model": "fake-gw"}))
'''


class LlmCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-llm-"))
        self.fake = self.tmp / "fake_llm.py"
        self.fake.write_text(f"#!{sys.executable}\n" + textwrap.dedent(FAKE), encoding="utf-8")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)
        self.log = self.tmp / "calls.jsonl"
        os.environ["FAKE_LLM_LOG"] = str(self.log)
        os.environ["FAKE_LLM_MODE"] = "good"
        self.root = make_root(TOML + f'[llm]\nbackend = "claude"\nclaude_path = "{self.fake.as_posix()}"\nmodel = "fake"\n')
        shutil.copytree(REPO / "method" / "prompts", self.root / "method" / "prompts")
        self.p = paths(self.root)
        append_observations(self.p, scenario(), {"issue:trk:PAY-1#created": "결제 승인 오류", "issue:trk:AUTH-0#created": "로그인 실패"})
        configmod._load.cache_clear()
        self.cfg = configmod.load(self.root)
        with redirect_stdout(io.StringIO()):
            pipeline.run(self.p, self.cfg, ts(300), report=False, log=lambda *a: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)
        os.environ.pop("FAKE_LLM_LOG", None)
        os.environ.pop("FAKE_LLM_MODE", None)

    def args(self, **kw):
        return argparse.Namespace(**{"key": None, "limit": None, "refresh": False, "dry_run": False, "write": False, "work": None, **kw})

    def run_task(self, name, cfg=None, **kw):
        runner = llm.Runner(self.p, cfg or self.cfg, log=lambda *a: None)
        ctx = llm.Ctx(self.p, cfg or self.cfg)
        task = llm.TASKS[name]
        accepted, tally = runner.run(task, ctx, self.args(**kw))
        msgs = task.sink(ctx, accepted, self.args(**kw)) if accepted else []
        return accepted, tally, msgs


class TestValidators(unittest.TestCase):
    def test_placeholders_digits_and_refs(self):
        allowed, facts = {"n", "flow", "ABC-12"}, {"n"}
        ok, why = llm.check_stmt({"text": "모두 {n} 이다", "refs": ["n", "nope"]}, allowed, facts=facts)
        self.assertEqual(ok["refs"], ["n"])                                         # 허락 밖 ref 는 걸러지고 남은 것으로 통과
        self.assertIsNone(llm.check_stmt({"text": "12% 다", "refs": ["n"]}, allowed, facts=facts)[0])
        self.assertIsNone(llm.check_stmt({"text": "{p90} 걸린다", "refs": ["n"]}, allowed, facts=facts)[0])
        self.assertIsNone(llm.check_stmt({"text": "근거 없음", "refs": ["nope"]}, allowed, facts=facts)[0])
        ok, _ = llm.check_stmt({"text": "ABC-12 를 본다", "refs": ["ABC-12"]}, allowed, facts=facts, literals=["ABC-12"])
        self.assertIsNotNone(ok)                                                    # 이름·키 안의 숫자는 허용

    def test_numbers_must_come_from_the_cited_input(self):
        nums = {"W1": llm.numbers_in("열린 일 1,605건 · 3%"), "W2": llm.numbers_in("2026-10-05 수집")}
        self.assertIsNotNone(llm.check_stmt({"text": "1,605건이 열려 있다", "refs": ["W1"]}, {"W1", "W2"}, numbers=nums)[0])
        self.assertIsNone(llm.check_stmt({"text": "1,700건이 열려 있다", "refs": ["W1"]}, {"W1", "W2"}, numbers=nums)[0])
        self.assertIsNone(llm.check_stmt({"text": "열린 일 10건", "refs": ["W1"]}, {"W1", "W2"}, numbers=nums)[0])  # 10 은 W2 의 날짜에만

    def test_number_words_markup_and_generic_refs(self):
        kw = {"facts": {"n"}, "generic": {"flow"}}
        for text in ("열두 건 중 절반이다", "세 배 늘었다", "⑫건", "十二건", "대부분 끝났다"):
            self.assertIsNone(llm.check_stmt({"text": text, "refs": ["n"]}, {"n", "flow"}, **kw)[0], text)
        self.assertIsNone(llm.check_stmt({"text": "흐름이 길다", "refs": ["flow"]}, {"n", "flow"}, **kw)[0])   # 섹션만으로는 근거가 아니다
        ok, _ = llm.check_stmt({"text": "모두 {n} 이다", "refs": ["flow"]}, {"n", "flow"}, **kw)
        self.assertEqual(ok["refs"], ["flow", "n"])                                  # 쓴 자리표시는 근거로 센다
        for text in ("![x](https://evil.example/q)", "자세히는 https://a.example", "<img src=x>"):
            self.assertIsNone(llm.check_stmt({"text": text, "refs": ["n"]}, {"n"}, facts={"n"})[0], text)

    def test_parse_reply_shapes(self):
        r = llm.parse_reply(0, json.dumps({"structured_output": {"a": 1}, "total_cost_usd": 0.1, "modelUsage": {"m1": {}}}), "", "", 1)
        self.assertEqual((r.output, r.cost_usd, r.model, r.error), ({"a": 1}, 0.1, "m1", None))
        r = llm.parse_reply(0, json.dumps({"result": '{"a": 2}'}), "", "x", 1)
        self.assertEqual(r.output, {"a": 2})
        r = llm.parse_reply(1, json.dumps({"is_error": True, "subtype": "error_max_turns"}), "", "x", 1)
        self.assertIn("error_max_turns", r.error)
        self.assertIn("JSON", llm.parse_reply(0, "not json", "", "x", 1).error)


class TestCategoryCards(LlmCase):
    def test_claude_backend_is_hermetic_and_cards_render_current_numbers(self):
        accepted, tally, msgs = self.run_task("category_card", key="pay.api")
        self.assertEqual(tally["accepted"], 1)
        call = json.loads(self.log.read_text(encoding="utf-8").splitlines()[0])
        a = call["argv"]
        for flag in ("--bare", "-p", "--no-session-persistence", "--json-schema", "--system-prompt", "--max-budget-usd"):
            self.assertIn(flag, a)
        self.assertEqual(a[a.index("--tools") + 1], "")                              # 도구 없음
        self.assertEqual(a[a.index("--model") + 1], "fake")
        self.assertTrue(Path(call["cwd"]).name.startswith("atlas-llm-"))               # 빈 임시 폴더에서 (이 저장소의 CLAUDE.md·훅이 끼지 않게)
        self.assertIn("--strict-mcp-config", a)
        card = accepted[0][1]
        self.assertEqual(len(card["paragraphs"]), 1)                                 # '42%' 문장은 버려졌다
        cm, _ = catview.load(self.p)
        shown = llm.rendered_cards(self.p, cm)
        n = cm["categories"]["pay.api"]["profile"]["n"]
        self.assertIn(f"{n:,}건", shown["pay.api"]["paragraphs"][0])                  # 자리표시가 지금 값으로
        self.assertIn("생성 → In Progress", shown["pay.api"]["paragraphs"][0])
        md = guide.category_md(cm, "pay.api", guide._titles(self.p), 2, shown)
        self.assertIn("**해설**", md)

    def test_cache_ledger_and_budget(self):
        self.run_task("category_card", key="pay.api")
        _, tally, _ = self.run_task("category_card", key="pay.api")
        self.assertEqual(tally["cached_accepted"], 1)                                # 같은 모양 → 다시 부르지 않는다
        self.assertEqual(len(self.log.read_text(encoding="utf-8").splitlines()), 1)
        rows = list(read_jsonl(self.p.root / "data" / "llm" / "ledger.jsonl"))
        self.assertEqual(rows[0]["status"], "accepted")
        self.assertAlmostEqual(llm.month_spent(self.p), 0.02)
        cfg = json.loads(json.dumps(self.cfg))
        cfg["llm"]["max_usd_per_month"] = 0.02
        _, tally, _ = self.run_task("category_card", cfg=cfg, key="pay")
        self.assertEqual(tally["skipped_budget"], 1)                                  # 월 상한에 닿으면 부르지 않는다

    def test_stability_resamples_accepted_answers(self):
        self.run_task("category_card", key="pay.api")
        runner = llm.Runner(self.p, self.cfg, log=lambda *a: None)
        res = llm.stability(runner, llm.TASKS["category_card"], llm.Ctx(self.p, self.cfg), self.args(key="pay.api"), 2)
        self.assertEqual((res["sampled"], res["agreement_mean"]), (1, 1.0))         # 가짜는 늘 같은 답
        self.assertEqual(llm.status(self.p)["stability"]["n"], 1)
        res = llm.stability(runner, llm.TASKS["category_card"], llm.Ctx(self.p, self.cfg), self.args(key="pay.api"), 0, share=0.15)
        self.assertEqual(res["sampled"], 1)                                             # 비율로 줘도 적어도 하나
        self.assertEqual(llm.agreement("annotate", {"facets": {"a": {"value": "x"}}, "archetype": {"value": "u"}},
                                       {"facets": {"a": {"value": "y"}}, "archetype": {"value": "u"}}), 0.5)

    def test_magnitude_change_retires_card(self):
        self.run_task("category_card", key="pay.api")
        cm, _ = catview.load(self.p)
        self.assertIn("pay.api", llm.rendered_cards(self.p, cm))
        cm2 = json.loads(json.dumps(cm))
        cm2["categories"]["pay.api"]["profile"]["problem_rate"] = 0.9                   # '드물다'가 '흔하다'가 되면 문장을 다시 쓴다
        self.assertNotIn("pay.api", llm.rendered_cards(self.p, cm2))
        cm3 = json.loads(json.dumps(cm))
        cm3["categories"]["pay.api"]["profile"]["n"] += 1                              # 같은 구간 안의 변화는 숫자만 바뀐다
        self.assertIn("pay.api", llm.rendered_cards(self.p, cm3))

    def test_rejected_output_is_cached_with_reasons(self):
        os.environ["FAKE_LLM_MODE"] = "bad"
        accepted, tally, _ = self.run_task("category_card", key="pay.api")
        self.assertEqual((accepted, tally["rejected"]), ([], 1))
        st = llm.status(self.p)
        self.assertEqual(st["rejected"], 1)
        self.assertTrue(any("숫자" in k or "근거" in k for k in st["top_reasons"]))

    def test_errors_are_not_cached(self):
        os.environ["FAKE_LLM_MODE"] = "error"
        _, tally, _ = self.run_task("category_card", key="pay.api")
        self.assertEqual(tally["error"], 1)
        os.environ["FAKE_LLM_MODE"] = "good"
        _, tally, _ = self.run_task("category_card", key="pay.api")
        self.assertEqual(tally["accepted"], 1)
        os.environ["FAKE_LLM_MODE"] = "error"                                         # 다시 받기가 실패해도 통과한 답을 지우지 않는다
        _, tally, _ = self.run_task("category_card", key="pay.api", refresh=True)
        self.assertEqual(tally["error"], 1)
        _, tally, _ = self.run_task("category_card", key="pay.api")
        self.assertEqual(tally["cached_accepted"], 1)

    def test_shape_change_hides_card_and_wrong_verdict_suppresses(self):
        self.run_task("category_card", key="pay.api")
        cm, _ = catview.load(self.p)
        cm2 = json.loads(json.dumps(cm))
        cm2["categories"]["pay.api"]["flow"]["main"] = ["생성", "Done"]               # 흐름의 모양이 바뀌면 카드는 낡았다
        self.assertNotIn("pay.api", llm.rendered_cards(self.p, cm2))
        from atlas import record
        with redirect_stdout(io.StringIO()):
            self.assertEqual(record.main(["--root", str(self.root), "llm-feedback", "--key", "pay.api", "--verdict", "wrong"]), 0)
            pipeline.run(self.p, self.cfg, ts(300), report=False, log=lambda *a: None, llm=False)
        cm, _ = catview.load(self.p)
        self.assertNotIn("pay.api", llm.rendered_cards(self.p, cm))
        cat = json.loads((self.p.model / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(cat["llm"]["verdicts"], {"wrong": 1})
        self.assertFalse(any("사람 판정" in w["what"] for w in cat["warnings"]))      # 판정 10건 전에는 경고하지 않는다

    def test_import_from_chat_session_uses_same_validation(self):
        f = self.tmp / "answer.json"
        f.write_text("```json\n" + json.dumps({"paragraphs": [{"text": "보통 {duration_p50} 걸린다.", "refs": ["duration_p50"]}],
                                                "watch": [{"text": "5건 중 1건이 재오픈됐다.", "refs": ["flow"]}]}) + "\n```",
                     encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = llm.main(["--root", str(self.root), "import", "category_card", "--key", "pay.api", "--file", str(f)])
        self.assertEqual(code, 1)                                                     # 주의 항목이 숫자 때문에 버려져 거절
        self.assertIn("rejected", buf.getvalue())

    def test_prompt_works_without_backend(self):
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            self.assertEqual(llm.main(["--root", str(self.root), "prompt", "category_card", "--key", "pay.api"]), 0)
        out = buf.getvalue()
        self.assertIn("duration_p50", out)
        self.assertIn("refs 에 쓸 수 있는 것", out)
        again = io.StringIO()
        with redirect_stdout(again), redirect_stderr(io.StringIO()):                  # --root 는 작업 이름 뒤에 와도 된다
            self.assertEqual(llm.main(["prompt", "category_card", "--key", "pay.api", "--root", str(self.root)]), 0)
        self.assertEqual(again.getvalue(), out)

    def test_build_runs_auto_tasks_and_report_shows_card(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["llm"]["auto"] = ["category_card", "work_draft"]
        with redirect_stdout(io.StringIO()):
            s = pipeline.run(self.p, cfg, ts(300), report=True, log=lambda *a: None)
        self.assertGreaterEqual(s["llm"]["category_card"]["accepted"], 1)
        self.assertNotIn("work_draft", s["llm"])                                        # 작업 폴더에 쓰는 작업은 무인으로 안 돈다
        html = Path(s["report"]).read_text(encoding="utf-8")
        self.assertIn("근거 검증 통과 · 수치는 지금 값", html)                          # 해설 상자 (읽는 법 문구와 다른 표시)
        self.assertNotIn("<script", html)


class TestOtherTasks(LlmCase):
    def test_annotate_writes_annotated_and_build_carries_it(self):
        accepted, tally, _ = self.run_task("annotate", key="wu:issue:PAY-1")
        self.assertEqual(tally["accepted"], 1)
        rows = list(read_jsonl(self.p.annotated))
        self.assertEqual(rows[0]["build"]["annotate_recipe"], "annotate@1")
        with redirect_stdout(io.StringIO()):
            s = pipeline.run(self.p, self.cfg, ts(300), report=False, log=lambda *a: None, llm=False)
        self.assertEqual(s["annotations"]["carried"], 1)                               # 입력 해시가 같아 이어 붙었다
        _, tally, _ = self.run_task("annotate", limit=1)
        self.assertEqual(tally["accepted"], 1)                                          # 아직 안 한 WU 하나만

    def test_weekly_digest_drops_invented_numbers(self):
        accepted, tally, msgs = self.run_task("weekly_digest")
        self.assertEqual(tally["accepted"], 1)
        res = accepted[0][1]
        self.assertEqual([x["kind"] for x in res["items"]], ["변화"])                   # 9999건 문장은 버려졌다
        f = next(self.p.reports.glob("*-digest.md"))
        self.assertIn("## 근거", f.read_text(encoding="utf-8"))

    def test_category_names_go_to_inbox_only(self):
        accepted, tally, msgs = self.run_task("category_names")
        self.assertEqual(tally["accepted"], 1)
        f = next((self.root / "inbox").glob("*-category-names.md"))
        text = f.read_text(encoding="utf-8")
        self.assertIn("names:", text)
        self.assertNotIn("없는키", text)
        self.assertNotIn("names", (self.root / "method").joinpath("categories.yaml").read_text(encoding="utf-8")
                         if (self.root / "method" / "categories.yaml").exists() else "")

    def test_work_draft_cites_observations_and_is_not_observed(self):
        from atlas import workspace as ws
        work = self.tmp / "work"
        (work / "PAY-30-review").mkdir(parents=True)
        (work / "PAY-30-review" / "WORK.md").write_text("---\nkey: PAY-30\nstatus: active\n---\n# 리뷰 대기\n", encoding="utf-8")
        with (self.root / "atlas.toml").open("a", encoding="utf-8") as f:
            f.write(f'\n[[sources.workspace]]\nname = "w"\nroot = "{work.as_posix()}"\n')
        configmod._load.cache_clear()
        cfg = configmod.load(self.root)
        accepted, tally, msgs = self.run_task("work_draft", cfg=cfg, work=str(work / "PAY-30-review"), write=True)
        self.assertEqual(tally["accepted"], 1)
        self.assertEqual(len(accepted[0][1]["process"]), 1)                             # 근거 없는 단계는 버려졌다
        draft = work / "PAY-30-review" / ws.DRAFT
        self.assertTrue(draft.read_text(encoding="utf-8").startswith(ws.GENERATED_MARK))
        draft.write_text(ws.GENERATED_MARK + " 사람이 고친 초안 -->\n고친 내용\n", encoding="utf-8")
        self.run_task("work_draft", cfg=cfg, work=str(work / "PAY-30-review"), write=True)
        self.assertIn("고친 내용", draft.read_text(encoding="utf-8"))                     # 있으면 덮지 않는다
        src = ws.sources(cfg, self.root)[0]
        inv = ws.inventory(src, work / "PAY-30-review")
        self.assertEqual([f for f, _ in inv.files], ["WORK.md"])                          # 생성물(초안)은 관측하지 않는다

    def test_command_backend(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["llm"].update({"backend": "command", "command": [sys.executable, str(self.fake)]})
        accepted, tally, _ = self.run_task("category_card", cfg=cfg, key="pay")
        self.assertEqual(tally["accepted"], 1)
        self.assertEqual(accepted[0][2]["model"], "fake-gw")

    def test_none_backend_says_how_to_proceed(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["llm"]["backend"] = "none"
        with self.assertRaises(llm.NoBackend):
            self.run_task("category_card", cfg=cfg, key="pay")


if __name__ == "__main__":
    unittest.main()


class TestLlmReviewFixes(LlmCase):
    """독립 검토에서 나온 결함의 회귀 테스트."""

    def test_parse_reply_never_crashes(self):
        for out in ("null", "[]", '"ok"', '{"output": {"a": 1}, "cost_usd": "n/a"}'):
            r = llm.parse_reply(0, out, "", "m", 1)
            self.assertIsInstance(r, llm.Reply)
        r = llm.parse_reply(0, '{"output": {"a": 1}, "cost_usd": "n/a"}', "", "m", 1)
        self.assertEqual((r.output, r.cost_known), ({"a": 1}, False))
        r = llm.parse_reply(3, '{"output": {"a": 1}}', "", "m", 1)
        self.assertIsNone(r.output)                                                   # 오류로 끝난 호출의 출력은 쓰지 않는다

    def test_corrupt_ledger_line_does_not_stop_anything(self):
        self.run_task("category_card", key="pay.api")
        with (self.root / "data" / "llm" / "ledger.jsonl").open("a", encoding="utf-8") as f:
            f.write('{"ts": "2026-10-')
        self.assertEqual(llm.status(self.p)["accepted"], 1)
        with redirect_stdout(io.StringIO()):
            pipeline.run(self.p, self.cfg, ts(300), report=False, log=lambda *a: None, llm=False)

    def test_unknown_cost_is_charged_as_the_per_call_cap(self):
        cfg = json.loads(json.dumps(self.cfg))
        script = self.tmp / "nocost.py"
        script.write_text("import sys, json; json.load(sys.stdin); print(json.dumps({'output': {'paragraphs': [], 'watch': []}}))",
                          encoding="utf-8")
        cfg["llm"].update({"backend": "command", "command": [sys.executable, str(script)], "max_usd_per_call": 0.5,
                           "max_usd_per_run": 1.0})
        _, tally, _ = self.run_task("category_card", cfg=cfg)
        self.assertEqual(tally["rejected"], 2)                                         # 실행당 $1 / 호출당 $0.5 로 두 번까지
        self.assertGreaterEqual(tally["skipped_budget"], 1)
        rows = [r for r in llm.ledger_rows(self.p) if r["status"] == "rejected"]
        self.assertTrue(all(r["cost_estimated"] and r["cost_usd"] == 0.5 for r in rows))   # 비용을 모르면 상한으로 친다
        self.assertEqual(llm.month_spent(self.p), 1.0)

    def test_repeated_errors_back_off(self):
        os.environ["FAKE_LLM_MODE"] = "error"
        for _ in range(2):
            _, tally, _ = self.run_task("category_card", key="pay.api")
            self.assertEqual(tally["error"], 1)
        _, tally, _ = self.run_task("category_card", key="pay.api")
        self.assertEqual(tally["backoff"], 1)                                          # 7일 안에 두 번 실패한 대상은 건너뛴다
        _, tally, _ = self.run_task("category_card", key="pay.api", refresh=True)
        self.assertEqual(tally["error"], 1)

    def test_limit_zero_and_lock(self):
        _, tally, _ = self.run_task("category_card", limit=0)
        self.assertEqual(tally["accepted"], 0)
        self.assertGreater(tally["deferred"], 0)
        (self.root / "data" / "llm").mkdir(parents=True, exist_ok=True)
        (self.root / "data" / "llm" / ".lock").write_text("", encoding="utf-8")
        with self.assertRaises(llm.Busy):
            self.run_task("category_card", key="pay.api")

    def test_annotate_redone_after_codebook_change(self):
        self.run_task("annotate", key="wu:issue:PAY-1")
        rows = list(read_jsonl(self.p.annotated))
        rows[0]["build"]["codebook"] = "older"
        from atlas.common import write_jsonl
        write_jsonl(self.p.annotated, rows)
        task, ctx = llm.TASKS["annotate"], llm.Ctx(self.p, self.cfg)
        keys = [i.key for i in task.items(ctx, self.args())]
        self.assertIn("wu:issue:PAY-1", keys)                                          # 코드북이 바뀌면 다시 주석할 대상
        self.run_task("annotate", key="wu:issue:PAY-1")
        self.assertNotIn("wu:issue:PAY-1", [i.key for i in task.items(ctx, self.args())])
        other = json.loads(json.dumps(self.cfg))
        other["llm"]["model"] = "fake-2"                                               # 설정한 모델을 바꿔도 다시 주석할 대상
        self.assertIn("wu:issue:PAY-1", [i.key for i in task.items(llm.Ctx(self.p, other), self.args())])

    def test_cached_proposals_are_not_resunk(self):
        _, _, first = self.run_task("category_names")
        _, tally, again = self.run_task("category_names")                            # 같은 분에 돌아도 파일 이름이 같아 가려지지 않게 반환값으로 본다
        self.assertEqual((len(first), tally["cached_accepted"], again), (1, 1, []))
        self.assertEqual(len(list((self.root / "inbox").glob("*-category-names.md"))), 1)

    def test_llm_and_category_warnings_stay_out_of_the_context(self):
        import datetime as dt
        from atlas import catalog
        cat = json.loads((self.p.model / "catalog.json").read_text(encoding="utf-8"))
        cat["llm"].update(verdicts={"useful": 3, "wrong": 4, "not_useful": 3}, calls=10, accepted=5, accept_rate=0.5,
                          top_reasons=["숫자"], cost_usd=45.0)
        cat["categories"].update(ratio=0.9, uncategorized=9, units=10)
        got = {w["what"]: w["area"] for w in catalog.warnings(cat, self.cfg, dt.datetime.fromisoformat(ts(300)))}
        mine = {k: v for k, v in got.items() if "LLM" in k or "카테고리" in k}
        self.assertEqual(len(mine), 4)                                                # 통과율 · 사람 판정 · 예산 · 미분류
        self.assertEqual(set(mine.values()), {"llm", "category"})                     # 맥락 첫 줄(데이터 신뢰)에는 나오지 않는다

    def test_ledger_times_are_compared_as_times(self):
        import datetime as dt
        from atlas.llm.runner import month_spent
        now = dt.datetime.fromisoformat("2026-11-01T08:00:00+09:00")
        rows = [{"ts": "2026-10-31T22:30:00+00:00", "cost_usd": 1.0},                # 서울로는 11월 1일 — 이번 달
                {"ts": "2026-10-31T23:30:00+09:00", "cost_usd": 2.0},                # 지난달
                {"ts": "깨진 값", "cost_usd": 4.0}]
        self.assertEqual(month_spent(self.p, now, rows), 1.0)

    def test_digest_is_weekly(self):
        accepted, _, _ = self.run_task("weekly_digest")
        self.assertRegex(accepted[0][0].key, r"^\d{4}-W\d{2}$")
        self.assertIn("facts", accepted[0][1])                                        # 그때의 근거를 답과 함께 남긴다
