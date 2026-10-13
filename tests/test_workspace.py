"""작업 공간 테스트 — 작업 폴더의 규칙 · 수집 · 상관 · 소비 · 명령. 합성 폴더만 쓴다.

실행: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from atlas import catalog, config as configmod, context, hook, pipeline, stitch, workspace as ws  # noqa: E402
from atlas.collect import COLLECTORS, claude_code, workspace as wscol  # noqa: E402
from atlas.common import paths, read_observations  # noqa: E402
from atlas.mine import text  # noqa: E402
from atlas.store import append_observations  # noqa: E402
from test_engine import cfg_for, git, issue_created, issue_event, make_root, ob, ts  # noqa: E402

COLLECTED = "2026-10-13T09:00:00+09:00"


def put(base: Path, rel: str, content: str = "x", mtime: float | None = None) -> Path:
    f = base / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


def record(key="", status="active", title="", started="2026-01-01", closed="", problem="", result="", retro="", wid=""):
    front = [f"id: {wid}" if wid else "", f"key: {key}", f"title: {json.dumps(title, ensure_ascii=False)}",
             f"status: {status}", f"started: {started}", f"closed: {closed}"]
    body = [f"# {title or '작업'}", "## 문제", "<!-- 안내 -->", problem, "## 처리", "<!-- 안내 -->", "## 결과", result,
            "## 회고", retro]
    return "---\n" + "\n".join(x for x in front if x) + "\n---\n" + "\n".join(body) + "\n"


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-ws-"))
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.root = make_root(f'[[sources.workspace]]\nname = "w"\nroot = "{self.work.as_posix()}"\n')
        self.p = paths(self.root)
        self.cfg = cfg_for(self.root)
        self.src = ws.sources(self.cfg)[0]
        self.P = configmod.Patterns(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def snap(self, rel: str, collected=COLLECTED):
        return wscol.snapshot(self.src, self.work / rel, rel, self.P, collected)


# ---------------------------------------------------------------- 규칙: 무엇이 작업인가

class TestWorkRules(WorkspaceCase):
    def test_find_works(self):
        put(self.work, "ABC-1-x/notes.md")                       # 깊이 1 폴더 — 기록이 없어도 작업
        put(self.work, "ABC-2/WORK.md", record("ABC-2"))
        put(self.work, "_shared/runbook.md")                      # `_` 는 작업이 아니다
        put(self.work, ".cache/x")                                # `.` 도
        put(self.work, "archive/2025/ABC-0/WORK.md", record("ABC-0"))   # 묶음 안의 기록은 깊이와 무관하게 작업
        put(self.work, "archive/2025/loose/notes.md")             # 묶음 안의 기록 없는 폴더는 작업이 아니다
        put(self.work, "repo-clone/.git/HEAD")                    # 저장소 사본은 작업이 아니다
        put(self.work, "ABC-2/sub/WORK.md", record("ABC-9"))      # 작업 안은 더 내려가지 않는다
        self.assertEqual([r for _, r in ws.find_works(self.src)], ["ABC-1-x", "ABC-2", "archive/2025/ABC-0"])
        deep = ws.Source("w", self.work.resolve(), depth=2)
        put(self.work, "team/ABC-3/notes.md")
        self.assertIn("team/ABC-3", [r for _, r in ws.find_works(deep)])

    def test_locate(self):
        put(self.work, "ABC-1/src/deep/a.txt")
        put(self.work, "ABC-2/WORK.md", record("ABC-2"))
        put(self.work, "ABC-2/evidence/log/x.log")
        put(self.work, "_shared/x.md")
        put(self.work, "archive/2025/ABC-0/WORK.md", record("ABC-0"))
        srcs = [self.src]
        self.assertEqual(ws.locate(self.work / "ABC-1/src/deep", srcs)[2], "ABC-1")
        self.assertEqual(ws.locate(self.work / "ABC-2/evidence/log", srcs)[2], "ABC-2")
        self.assertEqual(ws.locate(self.work / "archive/2025/ABC-0", srcs)[2], "archive/2025/ABC-0")
        self.assertIsNone(ws.locate(self.work / "archive", srcs))           # 작업을 품은 묶음
        self.assertIsNone(ws.locate(self.work / "_shared", srcs))
        self.assertIsNone(ws.locate(self.work, srcs))
        self.assertIsNone(ws.locate(self.tmp, srcs))

    def test_read_record(self):
        f = put(self.work, "A/WORK.md", record("ABC-1", title="제목", problem="실제 문제", retro="다음엔 단위부터"))
        rec = ws.read_record(f)
        self.assertEqual(rec.title, "제목")
        self.assertEqual(set(rec.sections), {"problem", "retro"})          # 안내 주석만 있는 섹션은 빈 것으로 본다
        self.assertEqual(rec.keys(), ["ABC-1"])
        en = put(self.work, "B/WORK.md", "# Title\n## Problem\np\n## Result\nr\n")
        self.assertEqual(set(ws.read_record(en).sections), {"problem", "result"})
        self.assertEqual(ws.read_record(en).title, "Title")
        twice = put(self.work, "D/WORK.md", "## 결과\n1차\n## 처리\np\n## 결과 (2차)\n2차\n")
        self.assertEqual(ws.read_record(twice).sections["result"], "1차\n2차")    # 같은 섹션은 이어 붙인다
        bad = put(self.work, "C/WORK.md", "---\nkey: [unclosed\n---\n# t\n")
        self.assertTrue(ws.read_record(bad).error)

    def test_update_front_keeps_everything_else(self):
        text = "\ufeff---\r\nkey: ABC-1\r\nstatus: active   # 메모\r\n---\r\n본문 status: active\r\n"
        out = ws.update_front(text, {"status": "done", "closed": "2026-10-13"})
        self.assertTrue(out.startswith("\ufeff---\r\n"))
        self.assertIn("status: done   # 메모\r\n", out)
        self.assertIn("closed: 2026-10-13\r\n---", out)
        self.assertIn("본문 status: active", out)                         # 본문은 건드리지 않는다
        self.assertEqual(ws.update_front("# 기록\n", {"status": "done"}), "---\nstatus: done\n---\n# 기록\n")


# ---------------------------------------------------------------- 수집

class TestWorkspaceCollector(WorkspaceCase):
    def setUp(self):
        super().setUp()
        d = "ABC-5-login"
        put(self.work, f"{d}/WORK.md", record("ABC-5", "done", "세션 만료", closed="2026-01-05",
                                               problem="세션이 끊긴다 token=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
                                               result="단위를 초로 통일"))
        put(self.work, f"{d}/DECISIONS.md", "초 단위로 통일한다 — 밀리초 혼용이 원인")
        put(self.work, f"{d}/PLAN.md")
        put(self.work, f"{d}/CLAUDE.md")
        put(self.work, f"{d}/evidence/app.log")
        put(self.work, f"{d}/shot.png")
        put(self.work, f"{d}/out/report.md")
        put(self.work, f"{d}/notes.md")
        put(self.work, f"{d}/.env", "SECRET=should-not-be-read")
        put(self.work, f"{d}/scratch/tmp.txt")
        put(self.work, f"{d}/server/.git/HEAD")
        put(self.work, f"{d}/server/a.java")
        put(self.work, f"{d}/CONTEXT.md", ws.GENERATED_MARK + " -->\n맥락")    # 생성물 — 요약의 요약 금지
        self.dir = d

    def test_snapshot_event(self):
        e, text_ = self.snap(self.dir)
        self.assertEqual(e["kind"], "workspace.snapshot")
        self.assertEqual(e["keys"], {"issue_keys": ["ABC-5"], "works": ["w:ABC-5-login"]})
        self.assertEqual(e["objects"], [{"type": "work", "id": "w:ABC-5-login", "url": "ABC-5-login"}])
        self.assertEqual(e["labels"]["status"], ["done"])
        self.assertEqual(e["labels"]["closed"], ["2026-01-05"])
        m = e["metrics"]
        self.assertEqual((m["record"], m["plan"], m["decision"], m["instructions"], m["evidence"], m["output"], m["other"]),
                         (1, 1, 1, 1, 2, 1, 1))
        self.assertEqual((m["sensitive"], m["repos_inside"], m["files"]), (1, 1, 8))
        self.assertTrue(m["has_problem"] and m["has_result"] and not m["has_retro"] and not m["has_process"])
        self.assertIn("세션 만료", text_)
        self.assertIn("결정: 초 단위로 통일", text_)
        self.assertNotIn("ghp_", text_)                                     # 가린 뒤 자른다
        self.assertNotIn("should-not-be-read", text_)
        self.assertEqual(append_observations(self.p, [e], {e["obs_id"]: text_})["invalid"], 0)

    def test_obs_id_follows_content_not_mtime(self):
        a, _ = self.snap(self.dir)
        again, _ = self.snap(self.dir, "2026-10-14T09:00:00+09:00")
        self.assertEqual(a["obs_id"], again["obs_id"])
        os.utime(self.work / self.dir / "notes.md", (time.time() - 99, time.time() - 99))
        self.assertEqual(self.snap(self.dir)[0]["obs_id"], a["obs_id"])      # 수정 시각만 바뀐 것은 새 상태가 아니다
        (self.work / self.dir / "notes.md").rename(self.work / self.dir / "memo.md")    # 이름만 바뀌어도 새 상태다
        renamed = self.snap(self.dir)[0]
        self.assertEqual(renamed["metrics"], a["metrics"])
        self.assertNotEqual(renamed["obs_id"], a["obs_id"])
        (self.work / self.dir / "memo.md").rename(self.work / self.dir / "notes.md")
        put(self.work, f"{self.dir}/notes.md", "바뀜")
        self.assertNotEqual(self.snap(self.dir)[0]["obs_id"], a["obs_id"])
        user_context = put(self.work, f"{self.dir}/CONTEXT.md", "사람이 쓴 맥락")    # 표시가 없으면 사람의 파일이다
        self.assertEqual(self.snap(self.dir)[0]["metrics"]["other"], 2)
        user_context.unlink()

    def test_keys_declared_first_then_folder_name(self):
        put(self.work, "ABC-7-x/WORK.md", record("OPS-1"))
        put(self.work, "ABC-8-y/notes.md")
        self.assertEqual(self.snap("ABC-7-x")[0]["keys"]["issue_keys"], ["OPS-1"])
        self.assertEqual(self.snap("ABC-8-y")[0]["keys"]["issue_keys"], ["ABC-8"])
        put(self.work, "20261013-misc/WORK.md", record("", wid="misc-id"))
        e = self.snap("20261013-misc")[0]
        self.assertNotIn("issue_keys", e["keys"])
        self.assertEqual(e["keys"]["works"], ["w:misc-id"])                  # front-matter id 가 정체다

    def test_ts_never_after_collection(self):
        future = time.time() + 86400 * 400
        put(self.work, "ABC-9/notes.md", mtime=future)
        e = self.snap("ABC-9")[0]
        self.assertEqual(e["ts"], COLLECTED)
        self.assertLessEqual(dt.datetime.fromisoformat(e["ts"]), dt.datetime.fromisoformat(COLLECTED))

    def test_file_cap_marks_partial(self):
        e = wscol.snapshot(self.src, self.work / self.dir, self.dir, self.P, COLLECTED, max_files=2)[0]
        self.assertTrue(e["partial"])
        self.assertEqual(e["metrics"]["files"], 2)

    def test_collect_registered_and_reports(self):
        self.assertIn("workspace", COLLECTORS)
        res = wscol.collect(self.p, self.cfg, {"name": "w", "root": str(self.work)}, self.P, {})
        self.assertEqual(len(res.events), 1)
        self.assertTrue(any("비밀값" in n for n in res.notes))
        self.assertEqual(wscol.source_from_args(Namespace(workspace_root="/x", kind="workspace")),
                         {"name": "ws", "root": "/x"})


# ---------------------------------------------------------------- 상관

def snap_ob(obj, t, keys=(), status=None, started=None, closed=None, collected=None, n=0):
    labels = {k: [v] for k, v in (("status", status), ("started", started), ("closed", closed)) if v}
    o = ob(f"workspace:w:{obj}#{n}", "workspace", "workspace.snapshot", t, objects=[{"type": "work", "id": f"w:{obj}", "url": obj}],
           keys={**({"issue_keys": list(keys)} if keys else {}), "works": [f"w:{obj}"]}, labels=labels,
           metrics={"record": 1}, instance="w")
    o["source"]["collector_kind"] = "cli"
    o["collected_at"] = collected or t
    return o


def session(sid, t, works=(), keys=(), edits=1):
    return ob(f"ai_session:c:{sid}#3", "ai_session", "ai_session.session", t, objects=[{"type": "ai_session", "id": sid}],
              keys={**({"works": [f"w:{w}" for w in works]} if works else {}), **({"issue_keys": list(keys)} if keys else {})},
              metrics={"edits": edits, "prompts": 1, "duration_min": 30})


class TestWorkCatalog(unittest.TestCase):
    def test_tracker_mismatch_only_when_closed_now(self):
        cfg = configmod.load(Path(tempfile.gettempdir()) / "nonexistent-atlas")
        snaps = [snap_ob("a", ts(5), keys=["ABC-1"], status="active"), snap_ob("b", ts(5), keys=["ABC-2"], status="active"),
                 snap_ob("c", ts(5), keys=["ABC-3"], status="done")]

        def unit(key, open_now):
            return {"wu_id": f"wu:issue:{key}", "span": {"end_basis": "issue", "end": ts(3)}, "stats": {"open_now": open_now}}
        w = catalog._works(snaps, [], [unit("ABC-1", False), unit("ABC-2", True), unit("ABC-3", False)], cfg,
                           dt.datetime.fromisoformat(ts(6)))
        self.assertEqual(w["tracker_closed_but_open"], 1)                   # 다시 열린 ABC-2 · 기록도 끝난 ABC-3 은 아니다


class TestWorkStitch(unittest.TestCase):
    def setUp(self):
        self.cfg = configmod.load(Path(tempfile.gettempdir()) / "nonexistent-atlas")

    def units(self, obs, as_of=ts(200)):
        us, rep = stitch.stitch(obs, self.cfg, dt.datetime.fromisoformat(as_of))
        return {u["wu_id"]: u for u in us}, rep

    def test_keyed_work_joins_issue_and_tracker_stays_canonical(self):
        obs = [issue_created("ABC-1", ts(0)), issue_event("ABC-1", 1, "issue.resolved", ts(3), labels={"resolution": ["Fixed"]}),
               snap_ob("ABC-1-x", ts(4), keys=["ABC-1"], status="active", started="2025-12-01")]
        u = self.units(obs)[0]["wu:issue:ABC-1"]
        self.assertIn("workspace:w:ABC-1-x#0", [m["obs_id"] for m in u["members"]])
        self.assertEqual(u["span"]["end_basis"], "issue")                  # 트래커가 정본 — 폴더가 active 여도 끝남
        self.assertEqual(u["span"]["end"], ts(3))
        self.assertEqual(u["stats"]["works"], ["w:ABC-1-x"])

    def test_keyless_work_is_its_own_unit_with_record_dates(self):
        obs = [snap_ob("misc", ts(5), status="active", started="2026-01-02")]
        u = self.units(obs)[0]["wu:work:w:misc"]
        self.assertEqual((u["span"]["end_basis"], u["span"]["end"], u["stats"]["outcome"]), ("work", None, None))
        self.assertEqual(u["span"]["start"][:10], "2026-01-02")
        self.assertTrue(u["stats"]["open_now"])
        done = self.units([snap_ob("misc", ts(5), status="done", started="2026-01-02", closed="2026-01-04")])[0]["wu:work:w:misc"]
        self.assertEqual((done["span"]["end"], done["stats"]["outcome"]), ("2026-01-04T23:59:59+00:00", "clean"))
        dropped = self.units([snap_ob("misc", ts(5), status="dropped", closed="2026-01-04")])[0]["wu:work:w:misc"]
        self.assertEqual(dropped["stats"]["outcome"], "abandoned")
        nostatus = self.units([snap_ob("misc", ts(5))])[0]["wu:work:w:misc"]
        self.assertEqual(nostatus["span"]["end_basis"], "last_activity")     # 상태를 적지 않은 폴더는 다른 WU 처럼

    def test_latest_snapshot_only(self):
        old = snap_ob("misc", ts(5), status="active", collected=ts(5), n=1)
        new = snap_ob("misc", ts(9), status="done", closed="2026-01-10", collected=ts(9), n=2)
        units, rep = self.units([old, new])
        members = [m["obs_id"] for m in units["wu:work:w:misc"]["members"]]
        self.assertEqual(members, ["workspace:w:misc#2"])
        self.assertEqual(rep["linked_by_kind"]["workspace.snapshot"], {"linked": 1, "total": 1})   # 옛 스냅숏은 세지 않는다
        self.assertIsNone(rep["orphan_ratio"])                              # 스스로 앵커인 스냅숏은 고아 비율에 넣지 않는다
        units, _ = self.units([old, new], as_of=ts(6))                       # 그때의 상태로 되감는다
        self.assertEqual([m["obs_id"] for m in units["wu:work:w:misc"]["members"]], ["workspace:w:misc#1"])
        trimmed = snap_ob("misc", ts(7), status="active", collected=ts(12), n=3)     # 가장 늦게 바뀐 파일을 지운 뒤의 상태
        units, _ = self.units([old, new, trimmed])
        self.assertEqual([m["obs_id"] for m in units["wu:work:w:misc"]["members"]], ["workspace:w:misc#3"])

    def test_work_title_drives_doc_like(self):
        s = snap_ob("docs", ts(5), status="done", closed="2026-01-06")
        units, _ = stitch.stitch([s], self.cfg, dt.datetime.fromisoformat(ts(200)),
                                 {s["obs_id"]: "README 문서 정리\n문제: 오타"})
        self.assertTrue(units[0]["stats"]["doc_like"])

    def test_sessions_opened_in_work_folders(self):
        obs = [issue_created("ABC-1", ts(0)), snap_ob("ABC-1-x", ts(4), keys=["ABC-1"], status="active"),
               snap_ob("misc", ts(4), status="active"),
               session("s1", ts(2), works=["ABC-1-x"]), session("s2", ts(2), works=["misc"]),
               session("s3", ts(2), works=["misc"], keys=["ABC-1"]), session("s4", ts(2), works=["gone"], edits=0)]
        units, _ = self.units(obs)
        issue = {m["obs_id"]: m for m in units["wu:issue:ABC-1"]["members"]}
        self.assertEqual(issue["ai_session:c:s1#3"]["signals"], ["work_folder"])
        self.assertIn("ai_session:c:s3#3", issue)                            # 프롬프트의 키도 그대로
        misc = [m["obs_id"] for m in units["wu:work:w:misc"]["members"]]
        self.assertIn("ai_session:c:s2#3", misc)
        self.assertIn("ai_session:c:s3#3", misc)                             # 다대다
        self.assertNotIn("wu:ai_session:s2", units)
        self.assertTrue(all("ai_session:c:s4#3" not in [m["obs_id"] for m in u["members"]] for u in units.values()))


# ---------------------------------------------------------------- Claude Code 세션 → 작업 폴더

class TestClaudeCodeInWorkFolder(WorkspaceCase):
    def test_session_carries_work_id_and_separates_work_edits(self):
        put(self.work, "ABC-3-x/WORK.md", record("ABC-3", wid="abc3"))
        repo = self.tmp / "server"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        cwd = self.work / "ABC-3-x"
        proj = self.tmp / "projects" / "-work-ABC-3-x"
        proj.mkdir(parents=True)
        lines = [
            {"type": "user", "sessionId": "s9", "cwd": str(cwd), "timestamp": "2026-01-02T10:00:00Z",
             "message": {"role": "user", "content": "세션 끊김 고치자"}},
            {"type": "assistant", "sessionId": "s9", "timestamp": "2026-01-02T10:05:00Z", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": str(cwd / "WORK.md")}},
                {"type": "tool_use", "name": "Write", "input": {"file_path": str(cwd / "evidence" / "a.log")}},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": str(repo / "src" / "S.java")}}]}},
        ]
        f = proj / "s9.jsonl"
        f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        os.utime(f, (time.time() - 7200, time.time() - 7200))
        res = claude_code.collect(self.p, self.cfg, {"projects_dir": str(self.tmp / "projects")}, self.P, {})
        e = res.events[0]
        self.assertEqual(e["keys"]["works"], ["w:abc3"])                       # 작업 공간 수집기와 같은 id
        self.assertEqual(e["keys"]["paths"], ["src/S.java"])                   # 다른 저장소의 파일은 그 저장소 기준
        self.assertEqual(e["metrics"]["work_edits"], 2)                        # 작업 기록·근거는 코드 경로가 아니다
        self.assertEqual(append_observations(self.p, res.events)["invalid"], 0)


# ---------------------------------------------------------------- 텍스트 · 소비 · 명령 · 카탈로그

class TestWorkConsumers(WorkspaceCase):
    def setUp(self):
        super().setUp()
        put(self.work, "ABC-5-login/WORK.md", record("ABC-5", "done", "로그인 세션 만료가 너무 빠르다", closed="2026-01-05",
                                                      problem="로그인 직후 세션이 끊긴다 세션 만료 단위", result="단위 통일",
                                                      retro="세션 만료는 단위부터"))
        put(self.work, "ABC-6-pay/WORK.md", record("ABC-6", "done", "결제 보상 누락", closed="2026-01-05",
                                                    problem="보상 지급 배치가 멈춘다"))
        put(self.work, "ABC-7-now/WORK.md", record("ABC-7", "active", "로그아웃 뒤에도 세션이 남는다",
                                                    problem="로그아웃 후 세션 만료가 안 된다"))
        put(self.work, "ABC-7-now/CLAUDE.md", "# 이 작업\n")
        put(self.work, "ABC-8-key/.env", "x")
        put(self.work, "20261001-session-notes/WORK.md", record("", "active", "세션 만료 메모", problem="로그인 세션 만료 단위 메모"))
        append_observations(self.p, [issue_created("ABC-7", "2026-02-01T10:00:00+09:00"),
                                     issue_event("ABC-7", 1, "issue.resolved", "2026-02-03T10:00:00+09:00",
                                                 labels={"resolution": ["Fixed"]})])
        with redirect_stdout(io.StringIO()):
            from atlas.collect import run_one
            run_one(self.p, self.cfg, "workspace", self.cfg["sources"]["workspace"][0], dry_run=False, full=False)
            pipeline.run(self.p, self.cfg, "2026-10-13T12:00:00+09:00", log=lambda *a: None)

    def test_text_index_uses_work_records(self):
        obs = read_observations(self.p.observations)
        units = [json.loads(x) for x in self.p.stitched.read_text(encoding="utf-8").splitlines()]
        ex = {json.loads(x)["obs_id"]: json.loads(x)["text"] for x in self.p.excerpts.read_text(encoding="utf-8").splitlines()}
        texts = text.wu_texts(units, {o["obs_id"]: o for o in obs}, ex)
        self.assertTrue(texts["wu:issue:ABC-5"].startswith("로그인 세션 만료가 너무 빠르다"))
        meta = json.loads((self.p.model / "text_index.json").read_text(encoding="utf-8"))["meta"]["wu:issue:ABC-5"]
        self.assertEqual(meta["title"], "로그인 세션 만료가 너무 빠르다")
        self.assertEqual(meta["works"], [{"id": "w:ABC-5-login", "source": "w", "rel": "ABC-5-login", "status": "done"}])

    def test_text_order_issue_then_record_then_commits(self):
        unit = {"wu_id": "wu:x", "members": [{"obs_id": o, "link": "key"} for o in ("c", "w", "i")]}
        obs = {"c": ob("c", "vcs", "vcs.commit", ts(1)), "w": ob("w", "workspace", "workspace.snapshot", ts(9)),
               "i": ob("i", "issue", "issue.created", ts(5))}
        self.assertEqual(text.wu_texts([unit], obs, {"c": "커밋", "w": "기록", "i": "이슈"})["wu:x"], "이슈\n기록\n커밋")

    def test_context_for_a_work_folder(self):
        ctx = context.build(self.p, self.cfg, work=str(self.work / "ABC-7-now" / "sub"))
        self.assertEqual((ctx["work"]["rel"], ctx["work"]["keys"]), ("ABC-7-now", ["ABC-7"]))
        self.assertEqual(ctx["similar"][0]["anchor"], "issue:ABC-5")
        self.assertTrue(ctx["similar"][0]["work_paths"][0].endswith("ABC-5-login"))
        self.assertNotIn("wu:issue:ABC-7", [s["wu_id"] for s in ctx["similar"]])
        md = context.render_md(ctx)
        self.assertIn("기록 `", md)
        self.assertNotIn("비밀값", md)                                       # 작업 폴더 위생은 맥락 첫 줄에 넣지 않는다
        cat = json.loads((self.p.model / "catalog.json").read_text(encoding="utf-8"))
        self.assertTrue(any(w["area"] == "workspace" and "비밀값" in w["what"] for w in cat["warnings"]))
        outside = context.build(self.p, self.cfg, work=str(self.tmp))
        self.assertFalse(outside["work"]["known"])
        keyless = context.build(self.p, self.cfg, work=str(self.work / "20261001-session-notes"))
        self.assertNotIn("issue", keyless)
        self.assertTrue(keyless["similar"])
        self.assertNotIn("wu:work:w:20261001-session-notes", [x["wu_id"] for x in keyless["similar"]])

    def test_session_start_hook(self):
        def start(cwd, source="startup"):
            ev = {"hook_event_name": "SessionStart", "source": source, "cwd": str(cwd), "session_id": "S"}
            with mock.patch.object(configmod, "load", return_value=self.cfg):
                return hook.handle(ev, str(self.root))
        out = start(self.work / "ABC-7-now")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("issue:ABC-5", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(start(self.work / "ABC-7-now", "resume"))            # 이어 연 대화에는 이미 있다
        self.assertIsNone(start(self.tmp))
        put(self.work, "ABC-7-now/CLAUDE.md", "# 이 작업\n@WORK.md\n@CONTEXT.md\n")
        put(self.work, "ABC-7-now/CONTEXT.md", ws.context_md(self.p, self.cfg, self.work / "ABC-7-now"))
        self.assertIsNone(start(self.work / "ABC-7-now"))                    # 지금 세대의 파일이 같은 맥락을 가져온다
        self.assertIsNotNone(start(self.work / "ABC-7-now", "compact"))      # 압축 뒤에는 늘 다시 넣는다
        put(self.work, "ABC-7-now/CONTEXT.md", ws.GENERATED_MARK + " 세대 gen-old · 2026-01-01 -->\n")
        out = start(self.work / "ABC-7-now")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("세대 gen-old 로 만든 것", out)                           # 낡은 파일이면 지금 맥락을 넣고 알린다
        self.assertIn("SessionStart", hook.settings_snippet(self.root)["hooks"])

    def test_catalog_works(self):
        cat = json.loads((self.p.model / "catalog.json").read_text(encoding="utf-8"))
        w = cat["works"]
        self.assertEqual((w["works"], w["keyed"], w["sensitive_files"], w["no_record"]), (5, 4, 1, 1))
        self.assertEqual(w["done_without_result"], 1)                       # ABC-6
        self.assertEqual(w["tracker_closed_but_open"], 1)                   # ABC-7: 트래커는 끝났는데 기록은 active
        rows = {r["rel"]: r for r in ws.status_rows(self.p, self.cfg)}
        self.assertIn("ABC-7 는 트래커에서 끝남", rows["ABC-7-now"]["flags"])
        put(self.work, "ABC-6-pay/WORK.md", record("ABC-6", "done", "결제 보상 누락", closed="2026-01-05",
                                                    problem="보상 지급 배치가 멈춘다", result="배치 재시작"))
        with redirect_stdout(io.StringIO()):
            from atlas.collect import run_one
            run_one(self.p, self.cfg, "workspace", self.cfg["sources"]["workspace"][0], dry_run=False, full=False)
            pipeline.run(self.p, self.cfg, "2026-10-13T12:00:00+09:00", log=lambda *a: None)
        again = json.loads((self.p.model / "catalog.json").read_text(encoding="utf-8"))
        src = next(s for s in again["sources"] if s["system"] == "workspace")
        self.assertEqual((src["observations"], src["linked_ratio"]), (6, 1.0))   # 옛 스냅숏은 비율에서 뺀다
        self.assertEqual(again["works"]["done_without_result"], 0)
        later = catalog.build(self.p, self.cfg, now=dt.datetime.fromisoformat("2027-06-01T00:00:00+09:00"))
        self.assertEqual(later["works"]["stale_open"], 2)                    # ABC-7 · 키 없는 메모(둘 다 active)


class TestWorkCommands(WorkspaceCase):
    def run_cmd(self, *argv, cwd=None):
        buf = io.StringIO()
        old = os.getcwd()
        try:
            if cwd:
                os.chdir(cwd)
            with redirect_stdout(buf):
                code = ws.main([*argv[:1], "--root", str(self.root), *argv[1:]])
        finally:
            os.chdir(old)
        return code, buf.getvalue()

    def test_init_new_close_status(self):
        self.assertEqual(self.run_cmd("init")[0], 0)
        self.assertTrue((self.work / "CLAUDE.md").exists())
        self.run_cmd("init")                                                  # 있으면 건드리지 않는다
        self.assertEqual(self.run_cmd("new", "ABC-12", "--slug", "session drop", "--title", "세션: 끊김")[0], 0)
        d = self.work / "ABC-12-session-drop"
        rec = ws.read_record(d / "WORK.md")
        self.assertEqual((rec.front["key"], rec.front["title"], rec.status, rec.front["id"]),
                         ("ABC-12", "세션: 끊김", "active", "ABC-12-session-drop"))
        self.assertIn("@CONTEXT.md", (d / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertTrue((d / "CONTEXT.md").read_text(encoding="utf-8").startswith(ws.GENERATED_MARK))
        with self.assertRaises(SystemExit):
            self.run_cmd("new", "ABC-12", "--slug", "session drop")             # 덮어쓰지 않는다
        self.run_cmd("new", "cache warmup")
        self.assertTrue(any(p.name.endswith("-cache-warmup") and p.name[:8].isdigit() for p in self.work.iterdir()))
        code, out = self.run_cmd("close", cwd=d)
        self.assertIn("결과·회고가 비어 있다", out)
        rec = ws.read_record(d / "WORK.md")
        self.assertEqual(rec.status, "done")
        self.assertEqual(str(rec.front["closed"]), dt.date.today().isoformat())
        rows = {r["rel"]: r for r in ws.status_rows(self.p, self.cfg)}
        self.assertIn("결과 비어 있음", rows["ABC-12-session-drop"]["flags"])
        self.assertEqual(rows["ABC-12-session-drop"]["keys"], ["ABC-12"])

    def test_close_refuses_non_utf8_and_reports_triage(self):
        d = self.work / "ABC-20"
        d.mkdir()
        (d / "WORK.md").write_bytes("---\nstatus: active\n---\n# 한글\n".encode("cp949"))
        before = (d / "WORK.md").read_bytes()
        with self.assertRaises(SystemExit):
            self.run_cmd("close", str(d))
        self.assertEqual((d / "WORK.md").read_bytes(), before)
        put(self.work, "ABC-21/WORK.md", record("ABC-21", result="r", retro="t"))
        from atlas import record as recordmod
        with redirect_stdout(io.StringIO()):
            recordmod.main(["--root", str(self.root), "triage", "--anchor", "issue:ABC-21", "--archetype", "residual",
                            "--conf", "low", "--scale", "small"])
        out = self.run_cmd("close", str(self.work / "ABC-21"))[1]
        self.assertIn("atlas record feedback --anchor issue:ABC-21", out)
        self.assertNotIn("비어 있다", out)
        put(self.work, "ABC-22/WORK.md", record("ABC-22", closed="2026-02-02", result="r", retro="t"))
        out = self.run_cmd("close", str(self.work / "ABC-22"))[1]
        self.assertNotIn("record feedback", out)                             # 다른 작업의 예측으로 묻지 않는다
        self.assertEqual(str(ws.read_record(self.work / "ABC-22" / "WORK.md").front["closed"]), "2026-02-02")   # 적힌 날짜는 둔다

    def test_status_flags(self):
        put(self.work, "ABC-30/WORK.md", record("ABC-30", "active"), mtime=time.time() - 86400 * 90)
        put(self.work, "ABC-31/WORK.md", "# 상태 없음\n")
        put(self.work, "ABC-32/WORK.md", record("ABC-32", "doing"))
        rows = {r["rel"]: r for r in ws.status_rows(self.p, self.cfg)}
        self.assertTrue(any("손대지 않음" in f for f in rows["ABC-30"]["flags"]))
        self.assertIn("상태 없음", rows["ABC-31"]["flags"])
        self.assertIn("상태 'doing'?", rows["ABC-32"]["flags"])
        out = self.run_cmd("status")[1]
        self.assertIn("ABC-30", out)


class TestReviewFixes(WorkspaceCase):
    """독립 검토가 재현한 결함들 — 고친 뒤 다시 생기지 않게."""

    def test_bad_dates_do_not_stop_collection(self):
        put(self.work, "ABC-1/WORK.md", "---\nkey: ABC-1\nstatus: done\nstarted: 2026-02-30\nclosed: 2026-09-31\n---\n# t\n")
        rec = ws.read_record(self.work / "ABC-1" / "WORK.md")
        self.assertFalse(rec.error)
        self.assertEqual(rec.front["started"], "2026-02-30")                 # 날짜를 해석하지 않고 적힌 그대로
        e = self.snap("ABC-1")[0]
        self.assertEqual(e["labels"]["closed"], ["2026-09-31"])
        units, _ = stitch.stitch([e], self.cfg, dt.datetime.fromisoformat("2026-12-01T00:00:00+09:00"))
        self.assertEqual(units[0]["span"]["end"], e["ts"])                    # 읽을 수 없는 종료일은 스냅숏 시각
        self.assertFalse(ws.read_record(put(self.work, "B/WORK.md", "---\n---\n# 빈 머리\n")).error)

    def test_reverted_state_becomes_a_new_observation(self):
        f = put(self.work, "ABC-2/WORK.md", record("ABC-2", "active"))
        a = self.snap("ABC-2")[0]
        f.write_text(record("ABC-2", "done", closed="2026-01-03"), encoding="utf-8")
        b = wscol.snapshot(self.src, self.work / "ABC-2", "ABC-2", self.P, COLLECTED, past=[(1, a["obs_id"])])[0]
        f.write_text(record("ABC-2", "active"), encoding="utf-8")              # 되돌린 편집
        past = [(1, a["obs_id"]), (2, b["obs_id"])]
        c = wscol.snapshot(self.src, self.work / "ABC-2", "ABC-2", self.P, COLLECTED, past=past)[0]
        self.assertNotIn(c["obs_id"], (a["obs_id"], b["obs_id"]))
        self.assertTrue(c["obs_id"].startswith(a["obs_id"] + "~"))
        again = wscol.snapshot(self.src, self.work / "ABC-2", "ABC-2", self.P, COLLECTED, past=past + [(3, c["obs_id"])])[0]
        self.assertEqual(again["obs_id"], c["obs_id"])                        # 바뀐 것이 없으면 같은 관측
        same = wscol.snapshot(self.src, self.work / "ABC-2", "ABC-2", self.P, COLLECTED, past=[(1, a["obs_id"])])[0]
        self.assertEqual(same["obs_id"], a["obs_id"])

    def test_copied_folder_keeps_its_own_identity(self):
        put(self.work, "ABC-1-login/WORK.md", record("ABC-1", wid="ABC-1-login"))
        put(self.work, "ABC-2-pay/WORK.md", record("ABC-2", wid="ABC-1-login"))     # 복사해서 키만 바꿨다
        ids = ws.work_ids(self.src)
        self.assertEqual(ids, {"ABC-1-login": "ABC-1-login", "ABC-2-pay": "ABC-2-pay"})
        res = wscol.collect(self.p, self.cfg, {"name": "w", "root": str(self.work)}, self.P, {})
        self.assertEqual(sorted(e["objects"][0]["id"] for e in res.events), ["w:ABC-1-login", "w:ABC-2-pay"])
        self.assertTrue(any("id 가 다른 폴더와 같은" in n for n in res.notes))
        rows = {r["rel"]: r for r in ws.status_rows(self.p, self.cfg)}
        self.assertTrue(any("다른 폴더도 쓴다" in f for f in rows["ABC-2-pay"]["flags"]))

    def test_locate_agrees_with_find_works(self):
        put(self.work, "ABC-1/WORK.md", record("ABC-1"))
        put(self.work, "ABC-1/sub/WORK.md", record("ABC-9"))                # 작업 안의 기록 — 바깥이 작업이다
        put(self.work, "team/a/b/c/d/e/f/WORK.md", record("ABC-8"))          # 깊이 경계 밖
        put(self.work, "team/x/y.txt")
        put(self.work, "ABC-3/server/.git/HEAD")
        put(self.work, "ABC-3/server/src/A.java")
        found = {rel for _, rel in ws.find_works(self.src)}
        for d in [x for x in self.work.rglob("*") if x.is_dir() and ".git" not in x.parts]:
            loc = ws.locate(d, [self.src])
            if loc:
                self.assertIn(loc[2], found, str(d))
        self.assertEqual(ws.locate(self.work / "ABC-1" / "sub", [self.src])[2], "ABC-1")
        self.assertEqual(ws.locate(self.work / "ABC-3" / "server" / "src", [self.src])[2], "ABC-3")

    def test_worktree_work_folder(self):
        put(self.work, "ABC-4/.git", "gitdir: /elsewhere/.git/worktrees/ABC-4")    # 워크트리는 .git 이 파일이다
        put(self.work, "ABC-4/WORK.md", record("ABC-4"))
        put(self.work, "ABC-4/src/main/A.java")
        put(self.work, "ABC-4/evidence/a.log")
        self.assertIn("ABC-4", [r for _, r in ws.find_works(self.src)])
        m = self.snap("ABC-4")[0]["metrics"]
        self.assertEqual((m["record"], m["evidence"], m["other"], m["repos_inside"]), (1, 1, 0, 1))   # 코드는 세지 않는다

    def test_update_front_edge_cases(self):
        dup = "---\nstatus: active\nkey: A-1\nstatus: waiting\n---\n"
        self.assertEqual(ws.update_front(dup, {"status": "done"}).count("status: done"), 2)
        block = "---\nnote: |\n  a\n  ---\n  b\nstatus: active\n---\n본문\n"
        out = ws.update_front(block, {"status": "done", "closed": "2026-10-13"})
        self.assertIn("  ---\n  b\nstatus: done\nclosed: 2026-10-13\n---\n본문", out)
        mixed = "---\r\nstatus: active\nkey: A-1\r\n---\r\n"
        self.assertEqual(ws.update_front(mixed, {"status": "done"}), "---\r\nstatus: done\nkey: A-1\r\n---\r\n")

    def test_headings_match_words_and_adr_body(self):
        f = put(self.work, "A/WORK.md", "## Logs\nx\n## Login 실패 원인\ny\n## 결과물\nz\n## 결과: 고침\nr\n")
        self.assertEqual(ws.read_record(f).sections, {"result": "r"})
        put(self.work, "A/adr/0001-units.md", "# ADR 1\n## Context\nms 와 s 혼용\n## Decision\n초 단위로 통일\n")
        self.assertIn("초 단위로 통일", self.snap("A")[1])

    def test_paths_and_home_folders_stay_out(self):
        put(self.work, "ABC-5/WORK.md", "---\nkey: ABC-5\nrepos: [/Users/kim.minsu/src/api, server]\ntags: [C:\\Users\\kim\\x]\n---\n"
                                         "# t\n## 문제\n/Users/kim.minsu/src/api/A.java 에서 터짐\n")
        e, text_ = self.snap("ABC-5")
        self.assertEqual(e["labels"]["repos"], ["api", "server"])
        self.assertEqual(e["labels"]["tags"], ["x"])
        self.assertNotIn("kim", text_)
        self.assertIn("~/src/api/A.java", text_)

    def test_relative_root_is_instance_relative(self):
        src = ws.Source.from_config({"name": "w", "root": "work"}, self.tmp)
        self.assertEqual(src.root, (self.tmp / "work").resolve())

    def test_as_of_hides_later_collections_and_future_closing(self):
        early = snap_ob("misc", ts(5), status="active", collected=ts(5), n=1)
        late = snap_ob("misc", ts(1), status="done", closed="2026-01-02", collected=ts(30), n=2)   # 옛 수정 시각, 늦은 수집
        units, _ = stitch.stitch([early, late], self.cfg, dt.datetime.fromisoformat(ts(10)))
        self.assertEqual([m["obs_id"] for m in units[0]["members"]], ["workspace:w:misc#1"])
        future = snap_ob("misc", ts(5), status="done", closed="2026-03-31", n=3)
        units, _ = stitch.stitch([future], self.cfg, dt.datetime.fromisoformat(ts(200)))
        self.assertEqual(units[0]["span"]["end"], ts(5))                     # 적힌 종료일은 스냅숏보다 뒤일 수 없다

    def test_keyless_work_session_still_finds_its_commits(self):
        obs = [snap_ob("misc", ts(5), status="active"),
               ob("vcs:r:c1", "vcs", "vcs.commit", ts(2, 12), objects=[{"type": "commit", "id": "c1"}],
                  keys={"paths": ["src/a.py", "src/b.py"]}, metrics={"files": 2}),
               ob("vcs:r:c2", "vcs", "vcs.commit", ts(2, 13), objects=[{"type": "commit", "id": "c2"}],
                  keys={"paths": ["src/a.py"], "issue_keys": ["ABC-9"]}, metrics={"files": 1}),
               {**session("s1", ts(2), works=["misc"]), "keys": {"works": ["w:misc"], "paths": ["src/a.py", "src/b.py"]}}]
        units, _ = stitch.stitch(obs, self.cfg, dt.datetime.fromisoformat(ts(200)))
        by = {u["wu_id"]: {m["obs_id"]: m for m in u["members"]} for u in units}
        self.assertIn("vcs:r:c1", by["wu:work:w:misc"])                       # 아무 데도 안 붙은 커밋은 그 작업으로
        self.assertEqual(by["wu:issue:ABC-9"]["ai_session:c:s1#3"]["link"], "probabilistic")
        self.assertNotIn("wu:ai_session:s1", by)

    def test_context_file_never_lists_its_own_work(self):
        put(self.work, "20261001-notes/WORK.md", record("", "active", "세션 만료 메모", problem="세션 만료 단위"))
        put(self.work, "ABC-1-2/WORK.md", "---\nkeys: [ABC-1, ABC-2]\nstatus: active\n---\n# 세션 만료 두 건\n## 문제\n세션 만료 단위\n")
        put(self.work, "ABC-3/WORK.md", record("ABC-3", "done", "세션 만료 옛 일", problem="세션 만료 단위"))
        with redirect_stdout(io.StringIO()):
            from atlas.collect import run_one
            run_one(self.p, self.cfg, "workspace", self.cfg["sources"]["workspace"][0], dry_run=False, full=False)
            pipeline.run(self.p, self.cfg, "2026-10-13T12:00:00+09:00", log=lambda *a: None)
        mine_ = ws.context_md(self.p, self.cfg, self.work / "20261001-notes")
        self.assertNotIn("20261001-notes (", mine_)
        self.assertIn("issue:ABC-3", mine_)
        ctx = context.build(self.p, self.cfg, work=str(self.work / "ABC-1-2"))
        self.assertFalse({"wu:issue:ABC-1", "wu:issue:ABC-2"} & {x["wu_id"] for x in ctx["similar"]})
        code, out = TestWorkCommands.run_cmd(self, "context", str(self.work / "ABC-3"))
        self.assertTrue((self.work / "ABC-3" / "CONTEXT.md").read_text(encoding="utf-8").startswith(ws.GENERATED_MARK + " 세대 gen-"))
        out = TestWorkCommands.run_cmd(self, "close", str(self.work / "20261001-notes"), "--dropped")[1]
        self.assertIn("그만둔 이유", out)
        self.assertEqual(ws.read_record(self.work / "20261001-notes" / "WORK.md").status, "dropped")


class TestRepoSessions(WorkspaceCase):
    def session_file(self, cwd: Path, edits: list[Path]) -> None:
        proj = self.tmp / "projects" / "p"
        proj.mkdir(parents=True, exist_ok=True)
        lines = [{"type": "user", "sessionId": "s1", "cwd": str(cwd), "timestamp": "2026-01-02T10:00:00Z",
                  "message": {"role": "user", "content": "고치자"}},
                 {"type": "assistant", "sessionId": "s1", "timestamp": "2026-01-02T10:05:00Z", "message": {"content": [
                     {"type": "tool_use", "name": "Edit", "input": {"file_path": str(f)}} for f in edits]}}]
        f = proj / "s1.jsonl"
        f.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        os.utime(f, (time.time() - 7200, time.time() - 7200))

    def collect(self):
        return claude_code.collect(self.p, self.cfg, {"projects_dir": str(self.tmp / "projects")}, self.P, {}).events[0]

    def test_clone_inside_a_work_folder_is_code(self):
        put(self.work, "ABC-3/WORK.md", record("ABC-3"))
        clone = self.work / "ABC-3" / "server"
        clone.mkdir(parents=True)
        git(clone, "init", "-q", "-b", "main")
        self.session_file(clone / "src", [clone / "src" / "A.java", self.work / "ABC-3" / "WORK.md"])
        (clone / "src").mkdir()
        e = self.collect()
        self.assertEqual(e["keys"]["paths"], ["src/A.java"])
        self.assertEqual((e["metrics"]["work_edits"], e["keys"]["works"]), (1, ["w:ABC-3"]))

    def test_repo_session_keeps_other_repos_out(self):
        a, b = self.tmp / "repoA", self.tmp / "repoB"
        for r in (a, b):
            r.mkdir()
            git(r, "init", "-q", "-b", "main")
        self.session_file(a, [a / "x.py", b / "pom.xml"])
        self.assertEqual(self.collect()["keys"]["paths"], ["x.py"])          # 저장소에서 연 세션은 예전과 같다


class TestTemplateGuard(unittest.TestCase):
    def test_workspace_files_allowed_and_example_root_is_placeholder(self):
        from atlas import check
        self.assertTrue(check.template_allows("method/workspace/WORK.md"))
        self.assertTrue(check.template_allows("WORKSPACE.md"))
        cfg = configmod.load(Path(tempfile.gettempdir()) / "nonexistent-atlas")
        self.assertEqual(cfg["sources"]["workspace"], [])
        tmp = Path(tempfile.mkdtemp(prefix="atlas-chk-"))
        try:
            (tmp / "atlas.example.toml").write_text('[[sources.workspace]]\nroot = "/Users/kim/work"\n', encoding="utf-8")
            check.ROOT, check.errors[:] = tmp, []
            check.check_example_config("leak")
            self.assertTrue(any("workspace.root" in e for e in check.errors))
        finally:
            check.ROOT = check.REPO
            check.errors.clear()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
