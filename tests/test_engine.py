"""엔진 테스트 — 수집 · 상관 · 마이닝 · 메타 · 소비 · 평가. 합성 데이터만 쓴다(가상 저장소·가상 트래커 응답·가상 대화 기록).

실행: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from atlas import catalog, config as configmod, context, hook, pipeline, stitch  # noqa: E402
from atlas import evaluate  # noqa: E402
from atlas.collect import claude_code, git as gitcol, jira, run_one  # noqa: E402
from atlas.common import glob_regex, paths, read_jsonl, read_observations  # noqa: E402
from atlas.mine import claims as C, cochange, corpus as corpusmod, hotspot, process, text  # noqa: E402
from atlas.store import append_observations  # noqa: E402

UTC = dt.timezone.utc


def make_root(toml: str = "") -> Path:
    root = Path(tempfile.mkdtemp(prefix="atlas-eng-"))
    (root / "method").mkdir()
    for name in ("observation.schema.json", "codebook.yaml", "annotation.schema.json"):
        shutil.copy(REPO / "method" / name, root / "method")
    if toml:
        (root / "atlas.toml").write_text(toml, encoding="utf-8")
    return root


def ts(day: int, hour: int = 10) -> str:
    return dt.datetime(2026, 1, 1, hour, tzinfo=UTC).__add__(dt.timedelta(days=day)).isoformat(timespec="seconds")


def ob(obs_id, system, kind, t, objects=None, keys=None, labels=None, metrics=None, role="self", instance="t"):
    o = {"obs_id": obs_id, "source": {"system": system, "instance": instance, "collector_kind": "api"},
         "kind": kind, "ts": t, "actor_role": role, "objects": objects or [], "keys": keys or {},
         "metrics": metrics or {}, "partial": False, "collector": "test@0", "collected_at": t}
    if labels is not None:
        o["labels"] = labels
    return o


def commit(sha, t, paths_, keys=None, fix=False, instance="repo", files=None, merge=False):
    k = {"paths": paths_, **(keys or {})}
    return ob(f"vcs:{instance}:{sha}", "vcs", "vcs.merge" if merge else "vcs.commit", t,
              objects=[{"type": "commit", "id": sha}], keys=k, instance=instance,
              metrics={"files": len(paths_) if files is None else files, "insertions": 10, "deletions": 2,
                       "is_fix": fix, "is_merge": merge, "is_revert": bool((keys or {}).get("reverts"))})


def issue_created(key, t, type_="Bug", comps=("core",), instance="trk"):
    return ob(f"issue:{instance}:{key}#created", "issue", "issue.created", t,
              objects=[{"type": "issue", "id": key}], keys={"issue_keys": [key]}, instance=instance,
              labels={"type": [type_], "component": list(comps)})


def issue_event(key, n, kind, t, labels=None, metrics=None, keys=None, instance="trk"):
    k = {"issue_keys": [key], **(keys or {})}
    return ob(f"issue:{instance}:{key}#h{n}:0", "issue", kind, t, objects=[{"type": "issue", "id": key}],
              keys=k, labels=labels or {}, metrics=metrics or {}, instance=instance)


def cfg_for(root: Path) -> dict:
    configmod._load.cache_clear()
    return configmod.load(root)


# ---------------------------------------------------------------- 설정·패턴

class TestPatterns(unittest.TestCase):
    def setUp(self):
        self.P = configmod.Patterns(configmod.load(Path(tempfile.gettempdir()) / "nonexistent-atlas"))

    def test_issue_key_next_to_hangul(self):
        self.assertEqual(self.P.issue_keys("ABC-123에서 터짐, OPS-9 참고"), ["ABC-123", "OPS-9"])

    def test_issue_key_excludes_encodings_and_standards(self):
        self.assertEqual(self.P.issue_keys("UTF-8 인코딩, SHA-256, CVE-2021, ISO-8859"), [])

    def test_projects_allowlist(self):
        cfg = configmod.load(Path(tempfile.gettempdir()) / "nonexistent-atlas")
        cfg["keys"]["projects"] = ["ABC"]
        P = configmod.Patterns(cfg)
        self.assertEqual(P.issue_keys("ABC-1 XYZ-2"), ["ABC-1"])
        self.assertFalse(P.key_ok("XYZ-2"))

    def test_fix_detection_with_exclusions(self):
        self.assertTrue(self.P.is_fix("Fix NPE in pool"))
        self.assertTrue(self.P.is_fix("버그수정: 로그인"))
        self.assertFalse(self.P.is_fix("기능 수정"))
        self.assertFalse(self.P.is_fix("[javadoc] Fix link"))
        self.assertFalse(self.P.is_fix("Fix typo in README"))
        self.assertTrue(self.P.is_fix("Fix race in pool\n\nAlso update docs"))   # 제외 판정은 제목 문단만

    def test_roles_never_store_identity(self):
        cfg = configmod.load(Path(tempfile.gettempdir()) / "nonexistent-atlas")
        cfg["identity"]["self"] = ["me@corp.example"]
        P = configmod.Patterns(cfg)
        self.assertEqual(P.role_of("Me", "ME@corp.example"), "self")
        self.assertEqual(P.role_of("dependabot[bot]", "x@y"), "bot")
        self.assertEqual(P.role_of("ASF GitHub Bot", None), "bot")
        self.assertEqual(P.role_of("Robert Bottom", "rb@x.example"), "unknown")

    def test_glob(self):
        self.assertTrue(glob_regex("atlas/**/*.py").match("atlas/collect/git.py"))
        self.assertTrue(glob_regex("**/changes.xml").match("changes.xml"))
        self.assertFalse(glob_regex("atlas/*.py").match("atlas/collect/git.py"))


# ---------------------------------------------------------------- 저장소

class TestStore(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.p = paths(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_validate_dedupe_month_and_excerpts(self):
        good = ob("vcs:t:1", "vcs", "vcs.commit", ts(0))
        dup = dict(good)
        bad = ob("vcs:t:2", "vcs", "issue.created", ts(40))            # kind 접두 불일치
        later = ob("vcs:t:3", "vcs", "vcs.commit", ts(40))
        r = append_observations(self.p, [good, dup, bad, later], {"vcs:t:1": "메시지", "vcs:t:2": "x"})
        self.assertEqual((r["written"], r["duplicate"], r["invalid"]), (2, 1, 1))
        self.assertEqual(sorted(f.name for f in self.p.observations.glob("*.jsonl")), ["2026-01.jsonl", "2026-02.jsonl"])
        self.assertEqual([e["obs_id"] for e in read_jsonl(self.p.excerpts)], ["vcs:t:1"])
        r2 = append_observations(self.p, [good])
        self.assertEqual((r2["written"], r2["duplicate"]), (0, 1))

    def test_dry_run_writes_nothing(self):
        append_observations(self.p, [ob("vcs:t:1", "vcs", "vcs.commit", ts(0))], dry_run=True)
        self.assertFalse(self.p.observations.exists())


# ---------------------------------------------------------------- git 수집기

def git(repo: Path, *args, date="2026-01-02T10:00:00+09:00", name="Me", email="me@corp.example"):
    env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_NAME": name,
           "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def write(repo: Path, rel: str, content: str):
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content, encoding="utf-8")


class TestGitCollector(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="atlas-git-"))
        r = cls.repo = cls.tmp / "repo"
        r.mkdir()
        git(r, "init", "-q", "-b", "main")
        write(r, "src/pool.py", "a\n" * 40)
        write(r, "README.md", "x\n")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "초기 구성", date="2026-01-01T09:00:00+09:00")
        write(r, "src/pool.py", "b\n" * 40)
        write(r, "tests/test_pool.py", "t\n")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "ABC-12에서 터진 버그 수정\n\npassword=hunter2 는 지운다", date="2026-01-02T10:00:00+09:00")
        git(r, "mv", "src/pool.py", "src/conn_pool.py")
        git(r, "commit", "-q", "-m", "Rename pool (#34)", date="2026-01-03T10:00:00+09:00")
        git(r, "checkout", "-q", "-b", "feature/ABC-13")
        write(r, "src/conn_pool.py", "c\n" * 40)
        git(r, "commit", "-q", "-am", "work in progress", date="2026-01-04T10:00:00+09:00",
            name="dependabot[bot]", email="bot@x.example")
        git(r, "checkout", "-q", "main")
        git(r, "merge", "-q", "--no-ff", "feature/ABC-13", "-m", "Merge branch 'feature/ABC-13' into 'main'",
            date="2026-01-05T10:00:00+09:00")
        sha = git(r, "rev-parse", "HEAD~1").strip()
        git(r, "revert", "--no-edit", "HEAD~1", date="2026-01-06T10:00:00+09:00")
        cls.reverted_sha = sha

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def collect(self, root, state=None, source=None):
        cfg = cfg_for(root)
        cfg["identity"]["self"] = ["me@corp.example"]
        src = source or {"name": "repo", "path": str(self.repo)}
        return gitcol.collect(paths(root), cfg, src, configmod.Patterns(cfg), state or {})

    def test_extraction(self):
        root = make_root()
        try:
            res = self.collect(root)
            by_msg = {res.excerpts[e["obs_id"]].split("\n")[0]: e for e in res.events}
            fix = by_msg["ABC-12에서 터진 버그 수정"]
            self.assertEqual(fix["keys"]["issue_keys"], ["ABC-12"])
            self.assertTrue(fix["metrics"]["is_fix"])
            self.assertEqual(fix["actor_role"], "self")
            self.assertNotIn("hunter2", res.excerpts[fix["obs_id"]])           # 발췌는 가린 뒤에 둔다
            ren = by_msg["Rename pool (#34)"]
            self.assertEqual(ren["keys"]["renames"], ["src/pool.py\tsrc/conn_pool.py"])
            self.assertEqual(ren["keys"]["mr_ids"], ["repo!34"])
            merge = next(e for e in res.events if e["kind"] == "vcs.merge")
            from atlas.collect import branch_key
            self.assertEqual(merge["keys"]["branches"], [branch_key("feature/ABC-13")])   # 이름이 아니라 해시 키
            self.assertEqual(merge["keys"]["issue_keys"], ["ABC-13"])
            self.assertEqual(len(merge["keys"]["commits"]), 1)
            wip = by_msg["work in progress"]
            self.assertEqual(wip["actor_role"], "bot")
            rev = next(e for e in res.events if e["metrics"]["is_revert"])
            self.assertTrue(self.reverted_sha.startswith(rev["keys"]["reverts"][0]))
            for e in res.events:                                             # 개인 식별자가 L0 에 없다
                self.assertNotIn("me@corp.example", json.dumps(e, ensure_ascii=False))
            rep = append_observations(paths(root), res.events, res.excerpts)
            self.assertEqual(rep["invalid"], 0, rep["problems"])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_incremental_and_truncation(self):
        root = make_root()
        try:
            first = self.collect(root)
            again = self.collect(root, first.state)
            self.assertEqual(again.events, [])
            trunc = self.collect(root, source={"name": "repo", "path": str(self.repo), "max_paths": 1})
            big = [e for e in trunc.events if e["metrics"]["files"] > 1]
            self.assertTrue(big and all(e["partial"] and len(e["keys"]["paths"]) == 1 for e in big))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_cli_run_one_writes_state(self):
        root = make_root()
        try:
            cfg = cfg_for(root)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(run_one(paths(root), cfg, "git", {"name": "repo", "path": str(self.repo)},
                                         dry_run=False, full=False), 0)
            self.assertTrue((paths(root).state / "git-repo.json").exists())
            self.assertEqual(len(read_observations(paths(root).observations)), 6)
        finally:
            shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- Jira 수집기

def jira_issue(key, created="2026-01-01T10:00:00.000+0000", histories=None, comments=None, type_="Improvement",
               comps=("core", "net"), labels=("x",), description="본문 a@b.example", total_comments=None, cl_total=None):
    h = histories or []
    return {"key": key, "fields": {
        "summary": f"{key} 요약", "issuetype": {"name": type_}, "priority": {"name": "Major"},
        "components": [{"name": c} for c in comps], "labels": list(labels), "fixVersions": [],
        "created": created, "updated": "2026-01-09T10:00:00.000+0000", "description": description,
        "reporter": {"name": "me", "emailAddress": "me@corp.example"},
        "comment": {"comments": comments or [], "total": total_comments if total_comments is not None else len(comments or [])}},
        "changelog": {"histories": h, "total": cl_total if cl_total is not None else len(h)}}


def hist(hid, created, field, before, after, author="other"):
    return {"id": str(hid), "created": created, "author": {"name": author},
            "items": [{"field": field, "fromString": before, "toString": after}]}


class TestJiraCollector(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.cfg = cfg_for(self.root)
        self.cfg["identity"]["self"] = ["me"]
        self.P = configmod.Patterns(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_initial_labels_reconstructed_and_events(self):
        issue = jira_issue("ABC-1", histories=[
            hist(1, "2026-01-02T10:00:00.000+0000", "issuetype", "Bug", "Improvement"),
            hist(2, "2026-01-02T11:00:00.000+0000", "Component", "", "net"),
            hist(3, "2026-01-03T10:00:00.000+0000", "status", "Open", "Resolved"),
            hist(4, "2026-01-03T10:00:00.000+0000", "resolution", "", "Fixed"),
            hist(5, "2026-01-04T10:00:00.000+0000", "status", "Resolved", "Reopened"),
            hist(6, "2026-01-05T10:00:00.000+0000", "Link", "", "This issue is caused by ABC-9"),
            hist(7, "2026-01-06T10:00:00.000+0000", "labels", "x", "x y"),
        ], comments=[{"id": "77", "created": "2026-01-02T12:00:00.000+0000", "author": {"name": "me"},
                      "body": "ABC-2 와 같다. 연락 010-1234-5678"}])
        events, texts = jira.issue_events(issue, "trk", "https://jira.example", self.P, {"resolved", "closed"},
                                          "2026-01-10T00:00:00+00:00", True, False)
        created = next(e for e in events if e["kind"] == "issue.created")
        self.assertEqual(created["labels"]["type"], ["Bug"])                 # 생성 당시 값으로 되감음
        self.assertEqual(created["labels"]["component"], ["core"])
        self.assertEqual(created["labels"]["label"], ["x"])
        self.assertEqual(created["actor_role"], "self")
        kinds = [e["kind"] for e in events]
        for k in ("issue.changed", "issue.transition", "issue.resolved", "issue.linked", "issue.comment"):
            self.assertIn(k, kinds)
        reopen = [e for e in events if e["kind"] == "issue.transition" and e["metrics"]["reopen"]]
        self.assertEqual(len(reopen), 1)
        link = next(e for e in events if e["kind"] == "issue.linked")
        self.assertEqual(link["keys"]["links"], ["is caused by|ABC-9"])
        comment = next(e for e in events if e["kind"] == "issue.comment")
        self.assertEqual(comment["keys"]["mentions"], ["ABC-2"])
        state = stitch.replay_issue(events)
        self.assertEqual(state["labels"]["type"], ["Improvement"])          # 재생하면 지금 값으로 돌아온다
        self.assertEqual(state["labels"]["component"], ["core", "net"])
        self.assertEqual(state["labels"]["label"], ["x", "y"])
        self.assertEqual(state["reopen_count"], 1)
        self.assertIsNone(state["end"])
        rep = append_observations(paths(self.root), events)
        self.assertEqual(rep["invalid"], 0, rep["problems"])

    def test_dc_pagination_and_auth(self):
        pages = {0: {"issues": [jira_issue("ABC-1")], "total": 2}, 1: {"issues": [jira_issue("ABC-2")], "total": 2}}
        seen = []

        def fetch(url, headers):
            seen.append((url, headers))
            start = int(url.split("startAt=")[1].split("&")[0])
            return pages[start]
        with mock.patch.dict(os.environ, {"TEST_JIRA_TOKEN": "tok"}):
            res = jira.collect(paths(self.root), self.cfg, {"name": "trk", "base_url": "https://jira.example",
                                                            "jql": "project = ABC ORDER BY key", "auth_env": "TEST_JIRA_TOKEN",
                                                            "page_size": 1}, self.P, {}, fetch=fetch)
        self.assertEqual(len([e for e in res.events if e["kind"] == "issue.created"]), 2)
        self.assertEqual(seen[0][1]["Authorization"], "Bearer tok")
        self.assertIn("ORDER+BY+created+ASC", seen[0][0])
        self.assertEqual(seen[0][0].count("ORDER"), 1)
        self.assertEqual(res.state["updated_max"], "2026-01-09T10:00:00+00:00")
        jql = jira._jql({"jql": "project = ABC"}, res.state)
        self.assertIn('updated >= "2026-01-08 10:00"', jql)

    def test_cloud_adf_basic_and_truncated_changelog(self):
        adf = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "ADF 본문"}]}]}
        issue = jira_issue("ABC-3", description=adf, cl_total=2, histories=[
            hist(1, "2026-01-02T10:00:00.000+0000", "status", "Open", "Done")])
        calls = []

        def fetch(url, headers):
            calls.append((url, headers))
            if "/changelog" in url:
                return {"values": [hist(1, "2026-01-02T10:00:00.000+0000", "status", "Open", "Done"),
                                   hist(2, "2026-01-03T10:00:00.000+0000", "status", "Done", "Open")], "isLast": True}
            return {"issues": [issue], "isLast": True}
        with mock.patch.dict(os.environ, {"TEST_CLOUD": "me@corp.example:tok"}):
            res = jira.collect(paths(self.root), self.cfg, {"name": "trk", "flavor": "cloud", "base_url": "https://x.atlassian.net",
                                                            "jql": "project = ABC", "auth_env": "TEST_CLOUD",
                                                            "done_statuses": ["Done"]}, self.P, {}, fetch=fetch)
        self.assertTrue(calls[0][0].startswith("https://x.atlassian.net/rest/api/3/search/jql?"))
        self.assertEqual(calls[0][1]["Authorization"], "Basic " + base64.b64encode(b"me@corp.example:tok").decode())
        self.assertTrue(any("/issue/ABC-3/changelog" in u for u, _ in calls))
        self.assertEqual(len([e for e in res.events if e["kind"] == "issue.transition"]), 2)
        created = next(e for e in res.events if e["kind"] == "issue.created")
        self.assertIn("ADF 본문", res.excerpts[created["obs_id"]])

    def test_truncated_comments_marked_partial(self):
        issue = jira_issue("ABC-4", comments=[{"id": "1", "created": "2026-01-02T10:00:00.000+0000", "author": {}, "body": "a"}],
                           total_comments=5)
        events, _ = jira.issue_events(issue, "trk", "https://j", self.P, {"done"}, "2026-01-10T00:00:00+00:00", True, False)
        self.assertTrue(next(e for e in events if e["kind"] == "issue.comment")["partial"])


# ---------------------------------------------------------------- Claude Code 수집기

class TestClaudeCodeCollector(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-cc-"))
        self.repo = self.tmp / "work"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        proj = self.tmp / "projects" / "-work"
        proj.mkdir(parents=True)
        lines = [
            {"type": "user", "sessionId": "s1", "cwd": str(self.repo), "gitBranch": "feature/ABC-7",
             "timestamp": "2026-01-02T10:00:00Z", "message": {"role": "user", "content": "ABC-7 고쳐줘 <system-reminder>XYZ-1</system-reminder>"}},
            {"type": "assistant", "sessionId": "s1", "timestamp": "2026-01-02T10:01:00Z", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": str(self.repo / "src" / "a.py")}},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/etc/hosts"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "python -m pytest -q"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "git commit -m x"}}]}},
            {"type": "user", "sessionId": "s1", "timestamp": "2026-01-02T10:02:00Z", "message": {"content": [
                {"type": "tool_result", "is_error": True, "content": "boom"}]}},
            {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-01-02T10:30:00Z"},
        ]
        self.path = proj / "s1.jsonl"
        self.path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n", encoding="utf-8")
        old = time.time() - 7200
        os.utime(self.path, (old, old))
        self.root = make_root()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def test_session_summary(self):
        cfg = cfg_for(self.root)
        res = claude_code.collect(paths(self.root), cfg, {"projects_dir": str(self.tmp / "projects")},
                                  configmod.Patterns(cfg), {})
        self.assertEqual(len(res.events), 1)
        e = res.events[0]
        m = e["metrics"]
        self.assertEqual((m["prompts"], m["edits"], m["bash"], m["test_runs"], m["commits"], m["tool_errors"],
                          m["compactions"], m["unparsed_lines"]), (1, 2, 2, 1, 1, 1, 1, 1))
        self.assertEqual(e["keys"]["paths"], ["src/a.py"])                  # 저장소 밖 경로는 버린다
        self.assertEqual(e["keys"]["issue_keys"], ["ABC-7"])                 # 시스템 알림 안의 키는 세지 않는다
        self.assertTrue(e["partial"])
        self.assertEqual(e["obs_id"], "ai_session:claude-code:s1#5")
        self.assertEqual(append_observations(paths(self.root), res.events)["invalid"], 0)
        again = claude_code.collect(paths(self.root), cfg, {"projects_dir": str(self.tmp / "projects")},
                                    configmod.Patterns(cfg), res.state)
        self.assertEqual(again.events, [])

    def test_active_session_deferred(self):
        os.utime(self.path, None)
        cfg = cfg_for(self.root)
        res = claude_code.collect(paths(self.root), cfg, {"projects_dir": str(self.tmp / "projects")},
                                  configmod.Patterns(cfg), {})
        self.assertEqual(res.events, [])

    def test_latest_snapshot_wins(self):
        a = ob("ai_session:c:s1#5", "ai_session", "ai_session.session", ts(0), objects=[{"type": "ai_session", "id": "s1"}])
        b = ob("ai_session:c:s1#9", "ai_session", "ai_session.session", ts(0), objects=[{"type": "ai_session", "id": "s1"}])
        self.assertEqual([s["obs_id"] for s in stitch.latest_sessions([a, b])], ["ai_session:c:s1#9"])


# ---------------------------------------------------------------- 상관

def scenario() -> list[dict]:
    """ABC-1 결함(해결 후 재오픈·재해결), ABC-2 가 ABC-1 에서 유발됨(연결), MR·세션·되돌림·키 많은 커밋."""
    return [
        issue_created("ABC-1", ts(0)),
        issue_event("ABC-1", 1, "issue.resolved", ts(2), labels={"resolution": ["Fixed"]}),
        issue_event("ABC-1", 2, "issue.reopened", ts(3), labels={"resolution_from": ["Fixed"]}),
        issue_event("ABC-1", 3, "issue.resolved", ts(4), labels={"resolution": ["Fixed"]}),
        issue_created("ABC-2", ts(10), type_="Improvement"),
        issue_event("ABC-2", 1, "issue.linked", ts(11), keys={"links": ["is caused by|ABC-1"]}),
        issue_event("ABC-2", 2, "issue.resolved", ts(12), labels={"resolution": ["Fixed"]}),
        issue_created("ABC-3", ts(20)),
        issue_event("ABC-3", 1, "issue.resolved", ts(21), labels={"resolution": ["Won't Fix"]}),
        commit("c1", ts(1), ["src/a.py", "src/b.py"], {"issue_keys": ["ABC-1"]}, fix=True),
        commit("c2", ts(11), ["src/a.py"], {"issue_keys": ["ABC-2"]}),
        commit("c3", ts(30), ["src/c.py"], {"mr_ids": ["repo!5"]}),
        commit("m1", ts(31), [], {"mr_ids": ["repo!5"], "issue_keys": ["ABC-3"], "commits": ["c4"]}, merge=True),
        commit("c4", ts(30, 12), ["src/d.py"]),
        commit("c5", ts(40), ["src/e.py"], {"issue_keys": ["A-1", "B-2", "C-3", "D-4"]}),
        commit("r1", ts(20), ["src/a.py"], {"reverts": ["c1"]}),                    # ABC-1 종료(4일) + 16일 — 창 안
        commit("r2", ts(80), ["src/a.py"], {"reverts": ["c2"]}),                    # ABC-2 종료(12일) + 68일 — 창 밖
        ob("ai_session:c:s1#3", "ai_session", "ai_session.session", ts(1, 9), objects=[{"type": "ai_session", "id": "s1"}],
           keys={"issue_keys": ["ABC-1"]}, metrics={"duration_min": 30, "edits": 1, "test_runs": 2, "prompts": 2}),
        ob("ai_session:c:s2#3", "ai_session", "ai_session.session", ts(40, 8), objects=[{"type": "ai_session", "id": "s2"}],
           keys={"paths": ["src/e.py"]}, metrics={"duration_min": 60, "edits": 1, "prompts": 1}),
        ob("ai_session:c:s3#3", "ai_session", "ai_session.session", ts(50), objects=[{"type": "ai_session", "id": "s3"}],
           keys={}, metrics={"duration_min": 5, "edits": 0, "prompts": 1}),
        ob("atlas.triage:x", "atlas", "atlas.triage", ts(0), objects=[{"type": "issue", "id": "ABC-1"}]),
    ]


class TestStitch(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.cfg = cfg_for(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_(self, as_of=ts(200)):
        units, rep = stitch.stitch(scenario(), self.cfg, dt.datetime.fromisoformat(as_of))
        return {u["wu_id"]: u for u in units}, rep

    def test_links_and_signals(self):
        u, rep = self.run_()
        a1 = u["wu:issue:ABC-1"]
        members = {m["obs_id"]: m for m in a1["members"]}
        self.assertIn("vcs:repo:c1", members)
        self.assertEqual(members["ai_session:c:s1#3"]["link"], "key")
        self.assertNotIn("atlas.triage:x", members)                       # 예측은 작업단위에 섞이지 않는다
        self.assertEqual(a1["stats"]["reopen_count"], 1)
        self.assertEqual(a1["span"]["end"], ts(4))                        # 마지막 재해결 시각
        self.assertIn("wu:issue:ABC-2", a1["stats"]["followup_fix_wus"])  # 사람이 남긴 연결
        self.assertIn("rolled_back", a1["stats"]["deterministic_signals"])
        self.assertEqual(a1["stats"]["outcome"], "rolled_back")          # 우선순위: 롤백 > 후속 수정 > 재오픈
        self.assertEqual(a1["stats"]["ai_test_runs"], 2)
        self.assertEqual(u["wu:issue:ABC-3"]["stats"]["outcome"], "abandoned")
        m3 = {m["obs_id"] for m in u["wu:issue:ABC-3"]["members"]}       # MR 묶음은 머지의 키로 이슈에 붙는다
        self.assertTrue({"vcs:repo:c3", "vcs:repo:m1", "vcs:repo:c4"} <= m3)
        self.assertNotIn("wu:issue:A-1", u)                               # 키가 너무 많은 커밋은 조인하지 않는다
        s2 = u["wu:ai_session:s2"]                                        # 키 없는 세션 → 시간·경로로 커밋을 끌어온다
        prob = {m["obs_id"]: m for m in s2["members"]}
        self.assertEqual(prob["vcs:repo:c5"]["link"], "probabilistic")
        self.assertNotIn("wu:ai_session:s3", u)                           # 편집·프롬프트가 적은 세션은 WU 가 아니다
        self.assertEqual(rep["signals"]["followup_links"], 1)
        self.assertNotIn("rolled_back", u["wu:issue:ABC-2"]["stats"]["deterministic_signals"])   # 창 밖 되돌림은 결과를 안 바꾼다

    def test_as_of_hides_future(self):
        u, _ = self.run_(as_of=ts(3, 12))
        a1 = u["wu:issue:ABC-1"]
        self.assertIsNone(a1["span"]["end"])                              # ts(3) 재오픈 뒤 ts(4) 재해결은 아직 미래
        self.assertNotIn("wu:issue:ABC-2", u)
        self.assertFalse(a1["span"]["settled"])

    def test_settled_needs_open_window(self):
        u, _ = self.run_(as_of=ts(20))
        self.assertFalse(u["wu:issue:ABC-1"]["span"]["settled"])          # 끝난 지 16일 < 30일
        u, _ = self.run_(as_of=ts(60))
        self.assertTrue(u["wu:issue:ABC-1"]["span"]["settled"])

    def test_deterministic(self):
        a, _ = stitch.stitch(scenario(), self.cfg, dt.datetime.fromisoformat(ts(200)))
        b, _ = stitch.stitch(list(reversed(scenario())), self.cfg, dt.datetime.fromisoformat(ts(200)))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_szz_suspects_not_outcome(self):
        obs = [issue_created("ABC-10", ts(0), type_="Improvement"),
               issue_event("ABC-10", 1, "issue.resolved", ts(2), labels={"resolution": ["Fixed"]}),
               commit("k1", ts(1), ["src/core.py"], {"issue_keys": ["ABC-10"]}),
               issue_created("ABC-11", ts(10)),
               commit("k2", ts(11), ["src/core.py", "CHANGES.md"], {"issue_keys": ["ABC-11"]}, fix=True)]
        units, _ = stitch.stitch(obs, self.cfg, dt.datetime.fromisoformat(ts(200)))
        u = {x["wu_id"]: x for x in units}
        self.assertEqual(u["wu:issue:ABC-10"]["stats"]["followup_suspected_wus"], ["wu:issue:ABC-11"])
        self.assertEqual(u["wu:issue:ABC-10"]["stats"]["outcome"], "clean")   # 의심은 결과에 넣지 않는다


# ---------------------------------------------------------------- 마이닝

def mining_obs() -> list[dict]:
    out = []
    for i in range(6):                                                     # a·b 가 5번 함께, a 혼자 1번
        files = ["src/a.py", "src/b.py", "CHANGES.md"] if i < 5 else ["src/a.py", "src/z.py"]
        out.append(commit(f"m{i}", ts(i), files, {"issue_keys": [f"ABC-{i}"]}, fix=i % 2 == 0))
    out.append(commit("big", ts(7), [f"src/f{i}.py" for i in range(40)], files=40))
    out.append(commit("ren", ts(8), ["src/b2.py"], {"renames": ["src/b.py\tsrc/b2.py"]}))
    for i in range(6):
        out.append(issue_created(f"ABC-{i}", ts(i) if i else ts(0, 9)))
        out.append(issue_event(f"ABC-{i}", 1, "issue.resolved", ts(i, 20), labels={"resolution": ["Fixed"]}))
    return out


class TestMining(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.cfg = cfg_for(self.root)
        self.as_of = dt.datetime.fromisoformat(ts(100))
        obs = mining_obs()
        self.units, _ = stitch.stitch(obs, self.cfg, self.as_of)
        self.corp = corpusmod.build(obs, self.units, self.cfg, self.as_of)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_corpus_filters_and_renames(self):
        paths_all = {p for c in self.corp.commits for p in c.paths}
        self.assertNotIn("CHANGES.md", paths_all)                          # 장부 파일
        self.assertNotIn("src/b.py", paths_all)                            # 최신 이름으로 이어진다
        self.assertIn("src/b2.py", paths_all)
        self.assertTrue(next(c for c in self.corp.commits if c.obs_id.endswith(":big")).large)

    def test_cochange_rules(self):
        cl = {c["claim_id"]: c for c in cochange.mine(self.corp)}
        r = cl["cochange:repo:src/b2.py=>src/a.py"]
        self.assertEqual((r["stat"]["support"], r["stat"]["confidence"]), (5, 1.0))
        r2 = cl["cochange:repo:src/a.py=>src/b2.py"]
        self.assertAlmostEqual(r2["stat"]["confidence"], round(5 / 6, 3))
        self.assertEqual(r2["strength"], "medium")
        self.assertFalse(any("f1.py" in k for k in cl))                     # 대량 커밋은 빠진다
        rules = cochange.Rules()
        for tx in cochange.transactions(self.corp):
            rules.add(*tx)
        self.assertEqual(rules.recommend("repo", "src/a.py", 3, 0.3)[0][0], "src/b2.py")

    def test_fallback_candidates(self):
        freq = {"src/pool.py": 5, "tests/test_pool.py": 3, "src/PoolTest.py": 2, "src/other.py": 9}
        got = cochange.fallback("src/Pool.py", freq, set(), 3)
        self.assertEqual(got[0][1], "이름 짝")
        self.assertIn(("src/other.py", "같은 디렉터리", 9), got)

    def test_hotspot_and_process(self):
        hs = hotspot.mine(self.corp)
        self.assertEqual(hs[0]["subject"]["id"], "src/a.py")
        scope_of = lambda u: "trk"
        pr = {c["subject"]["id"]: c for c in process.mine_process(self.units, scope_of)}
        self.assertEqual(pr["type=Bug"]["stat"]["ended"], 6)
        self.assertEqual(pr["type=Bug"]["stat"]["reopen_rate"], 0.0)

    def test_text_tokens_and_index(self):
        toks = text.tokens("NumberUtils.createNumber 오류 발생")
        for t in ("numberutils", "number", "utils", "createnumber", "create", "오류", "발생"):
            self.assertIn(t, toks)
        idx = text.Index({"w1": "결제 보상 지급 오류", "w2": "로그인 세션 만료", "w3": "보상 지급 누락"})
        self.assertEqual(idx.query("보상 지급이 안 됨", k=2)[0][0] in ("w1", "w3"), True)
        again = text.Index.load(idx.idf, idx.vecs, idx.n)
        self.assertEqual(again.query("로그인"), idx.query("로그인"))

    def test_drift(self):
        a = [C.make("hotspot", "s", {"type": "path", "id": "x"}, {}, "high", [], [], "t@1")]
        b = [C.make("hotspot", "s", {"type": "path", "id": "x"}, {}, "low", [], [], "t@1"),
             C.make("hotspot", "s", {"type": "path", "id": "y"}, {}, "high", [], [], "t@1")]
        d = C.drift(a, b)
        self.assertEqual((d["added"], d["removed"], d["weakened"]), (1, 0, 1))


# ---------------------------------------------------------------- 빌드 · 메타 · 소비

class TestBuildAndConsumers(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.p = paths(self.root)
        self.cfg = cfg_for(self.root)
        append_observations(self.p, mining_obs(), {f"issue:trk:ABC-{i}#created": f"결제 보상 지급 오류 {i}" for i in range(6)})

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def build(self, as_of=ts(100)):
        with redirect_stdout(io.StringIO()):
            return pipeline.run(self.p, self.cfg, as_of, log=lambda *a: None)

    def test_build_outputs_and_generation(self):
        s1 = self.build()
        for f in ("claims.jsonl", "paths.json", "text_index.json", "generation.json", "catalog.json", "build.json"):
            self.assertTrue((self.p.model / f).exists(), f)
        self.assertTrue(Path(s1["report"]).exists())
        s2 = self.build()
        self.assertEqual(s1["generation"], s2["generation"])               # 같은 입력 → 같은 세대
        append_observations(self.p, [commit("new", ts(90), ["src/a.py", "src/n.py"])])
        s3 = self.build()
        self.assertNotEqual(s1["generation"], s3["generation"])
        gen = json.loads((self.p.model / "generation.json").read_text(encoding="utf-8"))
        self.assertEqual(gen["previous"], s1["generation"])
        self.assertIsNotNone(gen["drift"])
        html = Path(s3["report"]).read_text(encoding="utf-8")
        self.assertIn("업무 지도", html)
        self.assertNotIn("<script", html)

    def test_catalog_warnings(self):
        self.build()
        cat = catalog.build(self.p, self.cfg, now=dt.datetime.fromisoformat(ts(120)))
        whats = " ".join(w["what"] for w in cat["warnings"])
        self.assertIn("수집이 오래됐다", whats)
        vcs = next(s for s in cat["sources"] if s["system"] == "vcs")
        self.assertIn("commit_key_coverage", vcs)

    def test_context_and_hook(self):
        self.build()
        ctx = context.build(self.p, self.cfg, ["src/a.py"], issue="ABC-1", text=None)
        pi = ctx["paths"][0]
        self.assertTrue(pi["known"])
        self.assertEqual(pi["with"][0]["path"], "src/b2.py")
        self.assertTrue(ctx["issue"]["known"])
        md = context.render_md(ctx, max_lines=6)
        self.assertLessEqual(len(md.splitlines()), 6)
        self.cfg["context"]["hook_min_confidence"] = 0.5
        ev = {"hook_event_name": "PostToolUse", "tool_name": "Edit", "session_id": "S", "tool_input": {"file_path": "src/a.py"}}
        with mock.patch.object(configmod, "load", return_value=self.cfg):
            first = hook.handle(ev, str(self.root))
            self.assertIn("src/b2.py", first["hookSpecificOutput"]["additionalContext"])
            self.assertIsNone(hook.handle(ev, str(self.root)))             # 같은 세션·같은 파일은 한 번만
            ev2 = {**ev, "session_id": "T", "tool_input": {"file_path": "src/b2.py"}}
            hook.handle(ev2, str(self.root))
            ev3 = {**ev, "session_id": "T"}
            out = hook.handle(ev3, str(self.root))                         # 이미 바꾼 짝은 말하지 않는다
            self.assertTrue(out is None or "src/b2.py" not in out["hookSpecificOutput"]["additionalContext"])
            p = hook.handle({"hook_event_name": "UserPromptSubmit", "prompt": "ABC-1 다시 보자"}, str(self.root))
            self.assertIn("ABC-1", p["hookSpecificOutput"]["additionalContext"])

    def test_stale_model_is_reported_not_misread(self):
        self.build()
        f = self.p.model / "paths.json"
        data = json.loads(f.read_text(encoding="utf-8"))
        data.pop("format")
        f.write_text(json.dumps(data), encoding="utf-8")
        ctx = context.build(self.p, self.cfg, ["src/a.py"])
        self.assertIn("옛 형식", ctx["health"][0]["what"])
        self.assertFalse(ctx["paths"][0]["known"])
        ev = {"hook_event_name": "PostToolUse", "tool_name": "Edit", "session_id": "S", "tool_input": {"file_path": "src/a.py"}}
        with mock.patch.object(configmod, "load", return_value=self.cfg):
            self.assertIn("atlas build", hook.handle(ev, str(self.root))["hookSpecificOutput"]["additionalContext"])
            ev["tool_input"] = {"file_path": "src/b2.py"}
            self.assertIsNone(hook.handle(ev, str(self.root)))             # 세션당 한 번만 알린다

    def test_hook_never_blocks(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO("not json")), redirect_stdout(buf):
            self.assertEqual(hook.main(["--root", str(self.root)]), 0)
        self.assertEqual(buf.getvalue(), "")
        self.assertTrue((self.p.state / "hook-errors.log").exists())

    def test_mcp_server_tools(self):
        try:
            from atlas import mcp_server
            srv = mcp_server.create(str(self.root))
        except ImportError:
            self.skipTest("mcp 선택 의존성이 없다")
        import asyncio
        names = [t.name for t in asyncio.run(srv.list_tools())]
        self.assertEqual(sorted(names), ["atlas_catalog", "atlas_context", "atlas_similar"])


# ---------------------------------------------------------------- 평가

class TestEvaluate(unittest.TestCase):
    def test_temporal_split_runs_and_bounds(self):
        root = make_root()
        try:
            p = paths(root)
            obs = []
            for i in range(30):                                            # ts(0..20) 학습, ts(21..29) 시험
                obs.append(commit(f"e{i}", ts(i), ["src/a.py", "src/b.py"] if i % 2 else ["src/a.py", "src/c.py"],
                                  {"issue_keys": [f"ABC-{i}"]}, fix=i % 3 == 0))
                obs.append(issue_created(f"ABC-{i}", ts(i, 9)))
            append_observations(p, obs, {f"issue:trk:ABC-{i}#created": "a b 수정" for i in range(30)})
            cfg = cfg_for(root)
            res = evaluate.evaluate(p, cfg, [dt.datetime.fromisoformat(ts(20))], horizon_days=30)
            r = res["results"][0]
            self.assertEqual(r["train_commits"], 21)                      # cutoff 시각과 같은 것은 학습 쪽(≤)
            self.assertEqual(r["test_commits"], 9)
            for name, m in r["cochange"]["methods"].items():
                for k in ("precision", "recall", "L3", "answered"):
                    self.assertTrue(m[k] is None or 0 <= m[k] <= 1, (name, k, m[k]))
            self.assertGreater(r["cochange"]["methods"]["mined"]["recall_all_queries"], 0)
            b = evaluate.boot([1.0, 1.0, 0.0], [0.0, 0.0, 0.0])
            self.assertTrue(b["ci95"][0] <= b["diff"] <= b["ci95"][1])
        finally:
            shutil.rmtree(root, ignore_errors=True)



# ---------------------------------------------------------------- 독립 검토에서 살아남은 변이를 잡는 회귀 테스트

class TestReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.p = paths(self.root)
        self.cfg = cfg_for(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # --- 평가 누수
    def test_eval_cochange_answers_before_learning(self):
        self.cfg["mining"].update(min_support=1, min_confidence=0.1)
        obs = [commit(f"t{i}", ts(i), ["src/a.py", "src/b.py"]) for i in range(3)]
        obs.append(commit("q", ts(10), ["src/a.py", "src/c.py"]))            # 시험: a 와 처음 보는 짝 c
        append_observations(self.p, obs)
        sp = evaluate.Split(read_observations(self.p.observations), self.cfg, {}, dt.datetime.fromisoformat(ts(5)),
                            dt.timedelta(days=30))
        r = evaluate.eval_cochange(sp)
        self.assertEqual(r["methods"]["mined"]["recall_all_queries"], 0.0)  # 답하기 전에 배우면 c 를 맞힌다

    def test_eval_files_index_ignores_future_text_of_training_work(self):
        obs = [issue_created("ABC-1", ts(0)), commit("x", ts(1), ["src/x.py"], {"issue_keys": ["ABC-1"]}),
               commit("z", ts(12), ["src/z.py"], {"issue_keys": ["ABC-1"]}),           # cutoff 뒤 ABC-1 의 커밋
               issue_created("ABC-2", ts(10)), commit("y", ts(11), ["src/x.py"], {"issue_keys": ["ABC-2"]})]
        append_observations(self.p, obs, {"issue:trk:ABC-1#created": "alpha", "vcs:repo:z": "gamma delta",
                                           "issue:trk:ABC-2#created": "gamma delta"})
        sp = evaluate.Split(read_observations(self.p.observations), self.cfg, load_ex(self.p),
                            dt.datetime.fromisoformat(ts(5)), dt.timedelta(days=30))
        r = evaluate.eval_files(sp)
        self.assertEqual(r["methods"]["mined"]["recall@10"], 0.0)            # 미래 커밋 글로 ABC-1 을 찾으면 1.0

    def test_eval_files_index_is_train_only(self):
        obs = [issue_created("ABC-1", ts(0)), commit("x", ts(1), ["src/x.py"], {"issue_keys": ["ABC-1"]}),
               issue_created("ABC-2", ts(10)), commit("y", ts(11), ["src/y.py"], {"issue_keys": ["ABC-2"]})]
        append_observations(self.p, obs, {"issue:trk:ABC-1#created": "alpha beta", "issue:trk:ABC-2#created": "gamma delta"})
        sp = evaluate.Split(read_observations(self.p.observations), self.cfg, load_ex(self.p),
                            dt.datetime.fromisoformat(ts(5)), dt.timedelta(days=30))
        r = evaluate.eval_files(sp)
        self.assertEqual(r["issues"], 1)
        self.assertEqual(r["methods"]["mined"]["recall@10"], 0.0)            # 시험 이슈 자신이 색인에 있으면 1.0

    def test_eval_hotspot_truth_excludes_tests(self):
        obs = [commit("h1", ts(0), ["src/a.py"], fix=True), commit("h2", ts(10), ["tests/test_a.py"], fix=True)]
        append_observations(self.p, obs)
        sp = evaluate.Split(read_observations(self.p.observations), self.cfg, {}, dt.datetime.fromisoformat(ts(5)),
                            dt.timedelta(days=30))
        self.assertEqual(evaluate.eval_hotspot(sp)["fix_touches"], 0)

    def test_bootstrap_negative_and_clustered(self):
        b = evaluate.boot([0.0] * 20, [1.0] * 20)
        self.assertTrue(b["significant"] and b["diff"] < 0)
        wide = evaluate.boot([1, 1, 1, 1, 0, 0, 0, 0], [0] * 8, clusters=["a"] * 4 + ["b"] * 4)
        narrow = evaluate.boot([1, 1, 1, 1, 0, 0, 0, 0], [0] * 8)
        self.assertEqual(wide["clusters"], 2)
        self.assertGreater(wide["ci95"][1] - wide["ci95"][0], narrow["ci95"][1] - narrow["ci95"][0])

    # --- 재생·Jira
    def test_replay_end_is_first_done_after_last_reopen(self):
        ev = [issue_created("ABC-1", ts(0)),
              issue_event("ABC-1", 1, "issue.transition", ts(2), labels={"status": ["Resolved"]}, metrics={"to_done": True}),
              issue_event("ABC-1", 2, "issue.transition", ts(5), labels={"status": ["Closed"]}, metrics={"to_done": True})]
        self.assertEqual(stitch.replay_issue(ev)["end"], ts(2))

    def test_jira_unsorted_changelog_and_reopen_rules(self):
        P = configmod.Patterns(self.cfg)
        hs = [hist(2, "2026-01-03T10:00:00.000+0000", "issuetype", "Improvement", "Task"),
              hist(1, "2026-01-02T10:00:00.000+0000", "issuetype", "Bug", "Improvement"),
              hist(3, "2026-01-04T10:00:00.000+0000", "status", "Open", "In Progress"),
              {"id": "4", "created": "2026-01-05T10:00:00.000+0000", "author": {}, "items": [
                  {"field": "status", "fromString": "Resolved", "toString": "Reopened"},
                  {"field": "resolution", "fromString": "Fixed", "toString": ""}]}]
        events, _ = jira.issue_events(jira_issue("ABC-1", type_="Task", histories=hs), "trk", "https://j", P,
                                      {"resolved", "closed"}, "2026-01-10T00:00:00+00:00", False, False)
        self.assertEqual(next(e for e in events if e["kind"] == "issue.created")["labels"]["type"], ["Bug"])
        moves = {e["labels"]["status"][0]: e["metrics"]["reopen"] for e in events if e["kind"] == "issue.transition"}
        self.assertEqual(moves, {"In Progress": False, "Reopened": True})   # 진행 중으로 옮김은 재오픈이 아니다
        self.assertEqual(stitch.replay_issue(events)["reopen_count"], 1)     # 같은 시각의 상태·해결 재오픈은 한 번

    def test_jira_redirect_strips_auth_and_requires_https(self):
        import urllib.request
        req = urllib.request.Request("https://jira.corp.example/rest/api/2/search", headers={"Authorization": "Bearer t"})
        h = jira._SameHostAuth()
        other = h.redirect_request(req, None, 302, "Found", {}, "https://sso.elsewhere.example/login")
        same = h.redirect_request(req, None, 302, "Found", {}, "https://jira.corp.example/rest/api/2/search?x=1")
        self.assertFalse(other.has_header("Authorization"))
        self.assertTrue(same.has_header("Authorization"))
        with mock.patch.dict(os.environ, {"T": "tok"}):
            with self.assertRaises(jira.CollectError):
                jira._headers({"base_url": "http://jira.corp.example", "auth_env": "T"})

    # --- 상관
    def test_revert_from_outside_before_end_is_rollback(self):
        obs = [issue_created("ABC-6", ts(0)),
               commit("x1", ts(1), ["src/a.py"], {"issue_keys": ["ABC-6"]}),
               commit("rv", ts(2), ["src/a.py"], {"reverts": ["x1"]}),                # 다른 데서 되돌림 (WU 밖)
               issue_event("ABC-6", 1, "issue.resolved", ts(3), labels={"resolution": ["Fixed"]})]
        u = {x["wu_id"]: x for x in stitch.stitch(obs, self.cfg, dt.datetime.fromisoformat(ts(100)))[0]}
        self.assertIn("rolled_back", u["wu:issue:ABC-6"]["stats"]["deterministic_signals"])

    def test_revert_inside_own_work_is_not_rollback(self):
        obs = [issue_created("ABC-5", ts(0)),
               commit("w1", ts(1), ["src/a.py"], {"issue_keys": ["ABC-5"]}),
               commit("w2", ts(2), ["src/a.py"], {"issue_keys": ["ABC-5"], "reverts": ["w1"]}),
               issue_event("ABC-5", 1, "issue.resolved", ts(3), labels={"resolution": ["Fixed"]})]
        u, _ = stitch.stitch(obs, self.cfg, dt.datetime.fromisoformat(ts(100)))
        self.assertNotIn("rolled_back", u[0]["stats"]["deterministic_signals"])

    def test_szz_respects_open_window(self):
        obs = [issue_created("ABC-10", ts(0), type_="Improvement"),
               issue_event("ABC-10", 1, "issue.resolved", ts(2), labels={"resolution": ["Fixed"]}),
               commit("k1", ts(1), ["src/core.py"], {"issue_keys": ["ABC-10"]}),
               issue_created("ABC-11", ts(90)),                               # 종료 + 88일: 창 밖
               commit("k2", ts(91), ["src/core.py"], {"issue_keys": ["ABC-11"]}, fix=True)]
        u = {x["wu_id"]: x for x in stitch.stitch(obs, self.cfg, dt.datetime.fromisoformat(ts(200)))[0]}
        self.assertEqual(u["wu:issue:ABC-10"]["stats"]["followup_suspected_wus"], [])

    def test_sync_merge_commits_are_ignored(self):
        obs = [issue_created("ABC-9", ts(0)),
               commit("u1", ts(1), ["src/other.py"]),
               commit("sm", ts(2), [], {"issue_keys": ["ABC-9"], "commits": ["u1"]}, merge=True)]
        obs[-1]["metrics"]["sync_merge"] = True                                  # 수집기가 동기화 머지로 표시
        units, rep = stitch.stitch(obs, self.cfg, dt.datetime.fromisoformat(ts(100)))
        u = {x["wu_id"]: x for x in units}
        self.assertNotIn("vcs:repo:u1", {m["obs_id"] for m in u["wu:issue:ABC-9"]["members"]})
        self.assertIn("vcs.merge(sync)", rep["linked_by_kind"])                  # 보고도 같은 표시를 따른다(키가 있어도)
        obs[-1]["metrics"]["sync_merge"] = False                                 # 형식을 모르는 머지(키 있음)는 들여온 커밋을 쓴다
        u = {x["wu_id"]: x for x in stitch.stitch(obs, self.cfg, dt.datetime.fromisoformat(ts(100)))[0]}
        self.assertIn("vcs:repo:u1", {m["obs_id"] for m in u["wu:issue:ABC-9"]["members"]})

    def test_settled_outcome_is_final(self):
        obs = scenario()
        later = dt.datetime.fromisoformat(ts(400))
        a = {x["wu_id"]: x for x in stitch.stitch(obs, self.cfg, dt.datetime.fromisoformat(ts(60)))[0]}
        b = {x["wu_id"]: x for x in stitch.stitch(obs + [commit("late", ts(300), ["src/a.py"], {"reverts": ["c2"]})],
                                                  self.cfg, later)[0]}
        self.assertTrue(a["wu:issue:ABC-2"]["span"]["settled"])
        self.assertEqual(a["wu:issue:ABC-2"]["stats"]["outcome"], b["wu:issue:ABC-2"]["stats"]["outcome"])

    # --- 마이닝
    def test_rename_chain_follows_all_hops(self):
        cs = [commit("r1", ts(1), ["b.py"], {"renames": ["a.py\tb.py"]}), commit("r2", ts(2), ["c.py"], {"renames": ["b.py\tc.py"]})]
        self.assertEqual(corpusmod.canonical_renames(cs)["repo"]["a.py"], "c.py")

    def test_hotspot_excludes_tests(self):
        obs = [commit(f"f{i}", ts(i), ["tests/test_pool.py", "src/pool.py"] if i < 3 else ["tests/test_pool.py"], fix=True)
               for i in range(6)]                                              # 테스트 파일이 수정은 더 많다
        units, _ = stitch.stitch(obs, self.cfg, dt.datetime.fromisoformat(ts(50)))
        corp = corpusmod.build(obs, units, self.cfg, dt.datetime.fromisoformat(ts(50)))
        self.assertEqual([c["subject"]["id"] for c in hotspot.mine(corp)], ["src/pool.py"])

    def test_issue_type_decides_commit_fix(self):
        obs = [issue_created("ABC-1", ts(0), type_="Improvement"),
               commit("p", ts(1), ["src/a.py"], {"issue_keys": ["ABC-1"]}, fix=True)]       # 메시지는 fix 처럼 보인다
        units, _ = stitch.stitch(obs, self.cfg, dt.datetime.fromisoformat(ts(50)))
        corp = corpusmod.build(obs, units, self.cfg, dt.datetime.fromisoformat(ts(50)))
        self.assertFalse(corp.commits[0].is_fix)

    # --- 소비·저장·메타
    def test_find_path_ambiguous_suffix_is_unknown(self):
        m = context.Model(self.p)
        m._paths = {"r": {"entries": {}, "files": {"a/util/X.java": 1, "b/util/X.java": 1}}}
        self.assertIsNone(context.find_path(m, "util/X.java"))
        self.assertEqual(context.find_path(m, "a/util/X.java"), ("r", "a/util/X.java"))

    def test_ingest_rejects_human_events(self):
        fb = ob("atlas.feedback:x", "atlas", "atlas.feedback", ts(0), objects=[{"type": "issue", "id": "ABC-1"}])
        fb["source"]["collector_kind"] = "human"
        fb["payload"] = {"use": "U1", "archetype_ok": "yes", "verdict": "helpful"}
        r = append_observations(self.p, [fb])
        self.assertEqual((r["written"], r["invalid"]), (0, 1))

    def test_catalog_freshness_uses_last_successful_run(self):
        append_observations(self.p, [commit("o", ts(0), ["src/a.py"])])
        from atlas.store import write_state
        write_state(self.p, "git-repo", {"collected_at": ts(119)})
        cat = catalog.build(self.p, self.cfg, now=dt.datetime.fromisoformat(ts(120)))
        self.assertLess(next(s for s in cat["sources"] if s["system"] == "vcs")["freshness_days"], 2)

    def test_root_prefers_current_folder(self):
        here = os.getcwd()
        try:
            os.chdir(self.root)
            with mock.patch.dict(os.environ, {"ATLAS_HOME": ""}):
                self.assertEqual(paths().root, self.root.resolve())
        finally:
            os.chdir(here)

    def test_branch_key_and_trailers(self):
        from atlas.collect import branch_key, strip_trailers
        self.assertIsNone(branch_key("origin/main"))
        self.assertIsNone(branch_key("APACHE/master", remote_tracking=True))   # remote-tracking 머지의 원격 이름
        self.assertIsNotNone(branch_key("jdoe/main"))                         # 사람 이름공간의 main 은 메인라인이 아니다
        self.assertIsNotNone(branch_key("release-notes-fix"))
        self.assertIsNone(branch_key("release/2.1"))
        self.assertEqual(branch_key("upstream/feature/x"), branch_key("feature/x"))
        self.assertEqual(branch_key("voyageth:patch-1"), branch_key("patch-1"))
        self.assertTrue(branch_key("jdoe/PROJ-1").startswith("b:"))
        self.assertNotIn("jdoe", branch_key("jdoe/PROJ-1"))
        self.assertEqual(strip_trailers("Fix\n\nSigned-off-by: Kim <k@x>\nCo-authored-by: Lee <l@x>\n"), "Fix\n\n")

    def test_hook_reads_and_writes_utf8_bytes(self):
        ev = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "ABC-1 고쳐줘"}, ensure_ascii=False).encode("utf-8")
        fake_in = io.TextIOWrapper(io.BytesIO(ev), encoding="cp949")
        out = io.BytesIO()
        fake_out = io.TextIOWrapper(out, encoding="cp949")
        seen = []
        with mock.patch.object(sys, "stdin", fake_in), mock.patch.object(sys, "stdout", fake_out), \
                mock.patch.object(hook, "handle", side_effect=lambda e, r: seen.append(e) or
                                  {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "한글 맥락"}}):
            self.assertEqual(hook.main(["--root", str(self.root)]), 0)
        self.assertEqual(seen[0]["prompt"], "ABC-1 고쳐줘")                   # 콘솔 인코딩과 무관하게 UTF-8 로 읽는다
        self.assertIn("한글 맥락", out.getvalue().decode("utf-8"))             # UTF-8 바이트로 쓴다


class TestTemplateGuard(unittest.TestCase):
    def test_example_config_must_hold_placeholders(self):
        from atlas import check
        tmp = Path(tempfile.mkdtemp(prefix="atlas-tpl-"))
        try:
            shutil.copy(REPO / "atlas.example.toml", tmp / "atlas.example.toml")
            with mock.patch.object(check, "ROOT", tmp):
                check.errors.clear()
                check.check_example_config("유출")
                self.assertEqual(check.errors, [])                          # 템플릿의 자리표시자는 통과
                text = (tmp / "atlas.example.toml").read_text(encoding="utf-8")
                (tmp / "atlas.example.toml").write_text(
                    text.replace("https://jira.company.example", "https://jira.realcorp.co.kr"), encoding="utf-8")
                check.check_example_config("유출")
                self.assertTrue(any("jira.realcorp.co.kr" in e for e in check.errors))
        finally:
            check.errors.clear()
            shutil.rmtree(tmp, ignore_errors=True)


class TestReviewRegressions2(unittest.TestCase):
    """재검증에서 살아남은 변이(N1, N9~N17)와 확정 결과의 불변성."""

    def setUp(self):
        self.root = make_root()
        self.p = paths(self.root)
        self.cfg = cfg_for(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def stitch_(self, obs, day):
        return {x["wu_id"]: x for x in stitch.stitch(obs, self.cfg, dt.datetime.fromisoformat(ts(day)))[0]}

    def test_fork_pr_from_master_is_work_not_sync(self):                       # N1
        from atlas.collect.git import is_sync_merge
        self.assertFalse(is_sync_merge("Merge pull request #5 from user/master"))
        self.assertTrue(is_sync_merge("Merge branch 'develop' into 'main'\n\nSee merge request grp/app!12"))

    def test_link_recorded_on_both_issues_counts_once(self):                   # N9
        obs = [issue_created("ABC-1", ts(0)), issue_event("ABC-1", 1, "issue.resolved", ts(1), labels={"resolution": ["Fixed"]}),
               issue_created("ABC-2", ts(5)),
               issue_event("ABC-1", 2, "issue.linked", ts(6), keys={"links": ["causes|ABC-2"]}),
               issue_event("ABC-2", 1, "issue.linked", ts(6), keys={"links": ["is caused by|ABC-1"]})]
        _, rep = stitch.stitch(obs, self.cfg, dt.datetime.fromisoformat(ts(100)))
        self.assertEqual(rep["signals"]["followup_links"], 1)

    def test_late_link_and_late_reopen_do_not_change_settled_outcome(self):    # N10 · 확정 불변
        base = [issue_created("ABC-1", ts(0)), issue_event("ABC-1", 1, "issue.resolved", ts(5), labels={"resolution": ["Fixed"]}),
                issue_created("ABC-2", ts(20))]                                  # 결함 보고는 창 안
        early = self.stitch_(base, 60)["wu:issue:ABC-1"]
        later = base + [issue_event("ABC-2", 1, "issue.linked", ts(500), keys={"links": ["is caused by|ABC-1"]}),
                        issue_event("ABC-1", 2, "issue.reopened", ts(100)),
                        issue_event("ABC-1", 3, "issue.resolved", ts(110), labels={"resolution": ["Fixed"]})]
        for day in (105, 200, 600):
            u = self.stitch_(later, day)["wu:issue:ABC-1"]
            self.assertTrue(u["span"]["settled"])
            self.assertEqual(u["stats"]["outcome"], early["stats"]["outcome"])
            self.assertEqual(u["span"]["end"], early["span"]["end"])
        self.assertEqual(self.stitch_(later, 600)["wu:issue:ABC-1"]["stats"]["late_reopens"], 1)
        self.assertIn("wu:issue:ABC-2", self.stitch_(later, 600)["wu:issue:ABC-1"]["stats"]["followup_fix_wus"])  # 사실은 남는다

    def test_link_observed_in_window_counts(self):
        obs = [issue_created("ABC-1", ts(0)), issue_event("ABC-1", 1, "issue.resolved", ts(5), labels={"resolution": ["Fixed"]}),
               issue_created("ABC-2", ts(20)),
               issue_event("ABC-2", 1, "issue.linked", ts(21), keys={"links": ["is caused by|ABC-1"]})]
        self.assertEqual(self.stitch_(obs, 100)["wu:issue:ABC-1"]["stats"]["outcome"], "followup_fix")

    def test_default_ref_is_remote_default_branch(self):                       # N11
        tmp = Path(tempfile.mkdtemp(prefix="atlas-ref-"))
        try:
            up = tmp / "up"
            up.mkdir()
            git(up, "init", "-q", "-b", "main")
            write(up, "a.txt", "1")
            git(up, "add", "-A")
            git(up, "commit", "-q", "-m", "base")
            subprocess.run(["git", "clone", "-q", str(up), str(tmp / "clone")], check=True, capture_output=True)
            c = tmp / "clone"
            git(c, "checkout", "-q", "-b", "wip")
            write(c, "w.txt", "1")
            git(c, "add", "-A")
            git(c, "commit", "-q", "-m", "local wip")
            self.assertEqual(gitcol.default_ref(str(c)), "origin/main")
            res = gitcol.collect(self.p, self.cfg, {"name": "repo", "path": str(c)}, configmod.Patterns(self.cfg), {})
            self.assertEqual(len(res.events), 1)                                # 작업 브랜치의 커밋은 모으지 않는다
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_collectors_refresh_last_run_time(self):                           # N12 · N13
        with mock.patch("atlas.collect.jira.now_iso", return_value="2026-03-01T00:00:00+00:00"):
            res = jira.collect(self.p, self.cfg, {"name": "trk", "base_url": "https://j.example", "jql": "project = X"},
                               configmod.Patterns(self.cfg), {"updated_max": "2026-01-01T00:00:00+00:00"},
                               fetch=lambda url, headers: {"issues": [], "total": 0})
        self.assertEqual(res.state["collected_at"], "2026-03-01T00:00:00+00:00")
        self.assertEqual(res.state["updated_max"], "2026-01-01T00:00:00+00:00")
        with mock.patch("atlas.collect.jira.now_iso", return_value="2026-03-01T00:00:00+00:00"):
            res = jira.collect(self.p, self.cfg, {"name": "trk", "base_url": "https://j.example", "jql": "project = X"},
                               configmod.Patterns(self.cfg), {}, fetch=lambda url, headers: {"issues": [], "total": 0})
        self.assertEqual(res.state, {"collected_at": "2026-03-01T00:00:00+00:00"})    # 첫 실행이 빈 결과여도
        with mock.patch.object(gitcol, "_git", side_effect=lambda repo, *a: "abc\n"), \
                mock.patch("atlas.collect.git.now_iso", return_value="2026-03-02T00:00:00+00:00"):
            res = gitcol.collect(self.p, self.cfg, {"name": "r", "path": "/x", "ref": "HEAD"}, configmod.Patterns(self.cfg),
                                 {"head": "abc", "collected_at": "2026-01-01T00:00:00+00:00"})
        self.assertEqual(res.state["collected_at"], "2026-03-02T00:00:00+00:00")

    def test_hook_does_nothing_outside_an_instance(self):                      # N14 · E
        empty = Path(tempfile.mkdtemp(prefix="atlas-noinst-"))
        try:
            ev = {"hook_event_name": "PostToolUse", "tool_name": "Edit", "session_id": "s", "tool_input": {"file_path": "a.py"}}
            self.assertIsNone(hook.handle(ev, str(empty)))
            self.assertFalse((empty / "data").exists())
            here = os.getcwd()
            os.chdir(empty)
            try:
                with mock.patch.dict(os.environ, {"ATLAS_HOME": ""}), mock.patch("atlas.common.REPO", empty), \
                        mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"not json"))), redirect_stdout(io.StringIO()):
                    self.assertEqual(hook.main([]), 0)                          # 루트를 못 찾아도 0
            finally:
                os.chdir(here)
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_standalone_stitch_invalidates_current(self):                      # N15
        append_observations(self.p, mining_obs())
        with redirect_stdout(io.StringIO()):
            pipeline.run(self.p, self.cfg, ts(100))
            self.assertTrue(self.p.units.exists())
            import contextlib
            with contextlib.redirect_stderr(io.StringIO()):
                stitch.main(["--root", str(self.root), "--as-of", ts(100)])
        self.assertFalse(self.p.units.exists())

    def test_cloud_changelog_fetched_when_total_missing(self):                 # N16
        issue = jira_issue("ABC-7", histories=[hist(1, "2026-01-02T10:00:00.000+0000", "status", "Open", "Done")])
        issue["changelog"] = {"histories": issue["changelog"]["histories"], "maxResults": 1}
        calls = []

        def fetch(url, headers):
            calls.append(url)
            if "/changelog" in url:
                return {"values": [hist(1, "2026-01-02T10:00:00.000+0000", "status", "Open", "Done"),
                                   hist(2, "2026-01-03T10:00:00.000+0000", "status", "Done", "Open")], "isLast": True}
            return {"issues": [issue], "isLast": True}
        jira.collect(self.p, self.cfg, {"name": "trk", "flavor": "cloud", "base_url": "https://x.example", "jql": "project = A"},
                     configmod.Patterns(self.cfg), {}, fetch=fetch)
        self.assertTrue(any("/issue/ABC-7/changelog" in u for u in calls))

    def test_octopus_merge_lists_all_branches(self):                            # N17
        tmp = Path(tempfile.mkdtemp(prefix="atlas-oct-"))
        try:
            r = tmp / "r"
            r.mkdir()
            git(r, "init", "-q", "-b", "main")
            write(r, "a.txt", "1")
            git(r, "add", "-A")
            git(r, "commit", "-q", "-m", "base")
            for b in ("feature/ABC-1", "feature/ABC-2"):
                git(r, "checkout", "-q", "-b", b, "main")
                write(r, f"{b[-5:]}.txt", "1")
                git(r, "add", "-A")
                git(r, "commit", "-q", "-m", f"{b[-5:]} work")
            git(r, "checkout", "-q", "main")
            write(r, "m.txt", "1")                                             # 빨리 감기 없이 진짜 문어발 머지가 되게
            git(r, "add", "-A")
            git(r, "commit", "-q", "-m", "mainline")
            git(r, "merge", "-q", "feature/ABC-1", "feature/ABC-2", "-m", "Merge branches 'feature/ABC-1' and 'feature/ABC-2'")
            self.assertEqual(len(git(r, "log", "-1", "--format=%P").split()), 3)
            res = gitcol.collect(self.p, self.cfg, {"name": "repo", "path": str(r), "ref": "HEAD"}, configmod.Patterns(self.cfg), {})
            merge = next(e for e in res.events if e["kind"] == "vcs.merge")
            self.assertEqual(len(merge["keys"]["commits"]), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_redirect_to_http_on_same_host_strips_auth(self):                   # F
        import urllib.request
        req = urllib.request.Request("https://jira.corp.example/a", headers={"Authorization": "Bearer t"})
        new = jira._SameHostAuth().redirect_request(req, None, 302, "Found", {}, "http://jira.corp.example/a")
        self.assertFalse(new.has_header("Authorization"))

    def test_comment_trailers_are_stripped(self):
        issue = jira_issue("ABC-8", comments=[{"id": "1", "created": "2026-01-02T10:00:00.000+0000", "author": {},
                                               "body": "LGTM\nSigned-off-by: Kim Cheolsu <k@x>"}])
        _, texts = jira.issue_events(issue, "trk", "https://j", configmod.Patterns(self.cfg), {"done"},
                                     "2026-01-10T00:00:00+00:00", True, False)
        self.assertNotIn("Cheolsu", texts["issue:trk:ABC-8#c1"])


def load_ex(p):
    return {e["obs_id"]: e["text"] for e in read_jsonl(p.excerpts)} if p.excerpts.exists() else {}


class TestGitSyncMerge(unittest.TestCase):
    def test_sync_merge_has_no_branch_commits(self):
        tmp = Path(tempfile.mkdtemp(prefix="atlas-sync-"))
        root = make_root()
        try:
            r = tmp / "r"
            r.mkdir()
            git(r, "init", "-q", "-b", "main")
            write(r, "a.txt", "1")
            git(r, "add", "-A")
            git(r, "commit", "-q", "-m", "base")
            git(r, "checkout", "-q", "-b", "feature/ABC-20")
            write(r, "f.txt", "1")
            git(r, "add", "-A")
            git(r, "commit", "-q", "-m", "ABC-20 work")
            git(r, "checkout", "-q", "main")
            write(r, "u.txt", "1")
            git(r, "add", "-A")
            git(r, "commit", "-q", "-m", "unrelated mainline work")
            git(r, "checkout", "-q", "feature/ABC-20")
            git(r, "merge", "-q", "--no-ff", "main", "-m", "Merge branch 'main' into feature/ABC-20")
            cfg = cfg_for(root)
            res = gitcol.collect(paths(root), cfg, {"name": "repo", "path": str(r), "ref": "HEAD"}, configmod.Patterns(cfg), {})
            merge = next(e for e in res.events if e["kind"] == "vcs.merge")
            self.assertNotIn("commits", merge["keys"])
            self.assertNotIn("branches", merge["keys"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            shutil.rmtree(root, ignore_errors=True)


class TestReviewRegressions3(unittest.TestCase):
    """세 번째 검토: 동기화 판정의 남은 형식·구조 판정, 확정 뒤의 지금 상태, 살아남은 변이(X2·X3·X5·X7·X9·X10·X13·X14)."""

    def setUp(self):
        self.root = make_root()
        self.p = paths(self.root)
        self.cfg = cfg_for(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def stitch_(self, obs, day):
        return {x["wu_id"]: x for x in stitch.stitch(obs, self.cfg, dt.datetime.fromisoformat(ts(day)))[0]}

    # --- 동기화 판정 (메시지)
    def test_sync_message_forms(self):                                          # 3차 1·2 · X9
        sync = gitcol.is_sync_merge
        cases = [
            ("Merge pull request #12 from org/develop\n\nRelease OPS-2", "org", True),     # 같은 저장소의 릴리스 PR
            ("Merge pull request #12 from org/develop", "ORG", True),
            ("Merge pull request #12 from org/develop", None, False),                       # 소유자를 모르면 작업
            ("Merge pull request #5 from user/master", "org", False),                       # 포크의 master
            ("Merge pull request #6 from org/feature/x", "org", False),
            ("Merge pull request #9 in PROJ/repo from develop to master", None, True),      # Bitbucket Server
            ("Merge pull request #9 in PROJ/repo from feature/ABC-1 to master", None, False),
            ("Merge pull request #9 in PROJ/repo from ~USER/repo:master to master", None, False),
            ("Merged in develop (pull request #9)", None, True),                            # Bitbucket Cloud
            ("Merged in feature/x (pull request #9)", None, False),
            ("Merge commit '6e1d19b' into feature/ABC-1", None, True),                      # 기능 브랜치를 최신으로
            ("Merge tag 'v1' of https://h/r into feature/X-1", None, True),
            ("Merge commit '6e1d19b' into main", None, False),                              # 메인라인으로 — 모른다
            ("Merge commit '6e1d19b'", None, False),
            ("Merge commit 'abc' into feature/X\n\nSee merge request g/r!3", None, False),
            ("Merge remote-tracking branch 'APACHE/master'", None, True),                   # 원격 이름이 무엇이든
            ("Merge branch 'main' into 'main'\n\nSee merge request acme/repo!7", None, False),  # GitLab 포크의 main
            ("Merge branch 'develop' of https://h/r into develop", None, True),             # 같은 이름이어도 pull 은 동기화
            ("Merged PR 7: improve parser", None, False),                                   # 모르는 형식
        ]
        for msg, owner, want in cases:
            self.assertEqual(sync(msg, owner), want, msg)

    def test_owner_from_remote_url(self):
        f = gitcol.owner_from_url
        self.assertEqual(f("https://github.com/apache/commons-lang.git"), "apache")
        self.assertEqual(f("git@github.com:apache/zookeeper.git"), "apache")
        self.assertEqual(f("ssh://git@host:7999/proj/repo.git"), "proj")
        self.assertEqual(f("https://gitlab.corp/grp/sub/app"), "sub")
        self.assertIsNone(f("/home/me/repo"))
        self.assertIsNone(f("file:///home/me/repo"))

    # --- 동기화 판정 (구조) · 수집기가 지표를 남긴다
    def test_collector_marks_sync_structurally_and_lists_unknown_work(self):  # X7 · X10
        tmp = Path(tempfile.mkdtemp(prefix="atlas-sync3-"))
        try:
            r = tmp / "r"
            r.mkdir()
            git(r, "init", "-q", "-b", "main")
            write(r, "a.txt", "1")
            git(r, "add", "-A")
            git(r, "commit", "-q", "-m", "base")
            git(r, "checkout", "-q", "-b", "topic")
            write(r, "t.txt", "1")
            git(r, "add", "-A")
            git(r, "commit", "-q", "-m", "topic work")
            git(r, "checkout", "-q", "main")
            write(r, "u.txt", "1")
            git(r, "add", "-A")
            git(r, "commit", "-q", "-m", "unrelated mainline work")
            git(r, "checkout", "-q", "topic")
            git(r, "merge", "-q", "--no-ff", "main", "-m", "catch up with trunk")          # 모르는 형식의 동기화
            git(r, "checkout", "-q", "main")
            git(r, "merge", "-q", "--no-ff", "topic", "-m", "Merged PR 7: improve parser")  # 모르는 형식의 작업
            res = gitcol.collect(self.p, self.cfg, {"name": "repo", "path": str(r), "ref": "main"},
                                 configmod.Patterns(self.cfg), {})
            merges = [e for e in res.events if e["kind"] == "vcs.merge"]
            catch = next(e for e in merges if e["metrics"]["sync_merge"])          # 둘째 부모가 main 의 첫 부모 사슬 위
            work = next(e for e in merges if not e["metrics"]["sync_merge"])
            self.assertEqual(len(merges), 2)
            self.assertNotIn("commits", catch["keys"])                               # 메인라인 커밋을 들이지 않는다
            self.assertEqual(len(work["keys"]["commits"]), 1)                         # 주제 브랜치의 커밋만
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_collector_uses_remote_owner_for_github_release_pr(self):          # 3차 1
        tmp = Path(tempfile.mkdtemp(prefix="atlas-sync3o-"))
        try:
            r = tmp / "r"
            r.mkdir()
            git(r, "init", "-q", "-b", "main")
            git(r, "remote", "add", "origin", "https://github.com/org/app.git")
            write(r, "a.txt", "1")
            git(r, "add", "-A")
            git(r, "commit", "-q", "-m", "base")
            git(r, "checkout", "-q", "-b", "develop")
            write(r, "d.txt", "1")
            git(r, "add", "-A")
            git(r, "commit", "-q", "-m", "OPS-1 develop work")
            git(r, "checkout", "-q", "main")
            git(r, "merge", "-q", "--no-ff", "develop", "-m", "Merge pull request #12 from org/develop\n\nRelease OPS-2")
            res = gitcol.collect(self.p, self.cfg, {"name": "repo", "path": str(r)}, configmod.Patterns(self.cfg), {})
            merge = next(e for e in res.events if e["kind"] == "vcs.merge")
            self.assertTrue(merge["metrics"]["sync_merge"])
            self.assertNotIn("commits", merge["keys"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_owner_prefers_upstream_in_fork_clone(self):                       # 4차 2 · S7
        tmp = Path(tempfile.mkdtemp(prefix="atlas-owner-"))
        try:
            r = tmp / "r"
            r.mkdir()
            git(r, "init", "-q", "-b", "main")
            git(r, "remote", "add", "origin", "https://github.com/jdoe/app.git")      # 내 포크
            self.assertEqual(gitcol.remote_owner(str(r), "HEAD"), "jdoe")
            git(r, "remote", "add", "upstream", "https://github.com/acme/app.git")    # PR 이 모이는 곳
            self.assertEqual(gitcol.remote_owner(str(r), "origin/main"), "acme")
            self.assertEqual(gitcol.remote_owner(str(r), "release/2.0"), "acme")       # 로컬 브랜치 ref
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_octopus_with_one_mainline_parent_is_work(self):                   # S2
        tmp = Path(tempfile.mkdtemp(prefix="atlas-oct-"))
        try:
            r = tmp / "r"
            r.mkdir()
            git(r, "init", "-q", "-b", "main")
            write(r, "a.txt", "1")
            git(r, "add", "-A")
            git(r, "commit", "-q", "-m", "base")
            for b in ("topic", "feat"):
                git(r, "checkout", "-q", "-b", b, "main")
                write(r, f"{b}.txt", "1")
                git(r, "add", "-A")
                git(r, "commit", "-q", "-m", f"{b} work")
            git(r, "checkout", "-q", "main")
            write(r, "m.txt", "1")
            git(r, "add", "-A")
            git(r, "commit", "-q", "-m", "mainline work")
            git(r, "checkout", "-q", "feat")
            git(r, "merge", "-q", "--no-ff", "main", "topic", "-m", "combine")       # 문어발: 메인라인 + 주제
            git(r, "checkout", "-q", "main")
            git(r, "merge", "-q", "--no-ff", "feat", "-m", "land feat")
            res = gitcol.collect(self.p, self.cfg, {"name": "repo", "path": str(r), "ref": "main"},
                                 configmod.Patterns(self.cfg), {})
            octo = next(e for e in res.events if e["metrics"]["parents"] == 3)
            self.assertFalse(octo["metrics"]["sync_merge"])                          # 하나라도 작업이면 버리지 않는다
            self.assertIn("commits", octo["keys"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # --- 확정 뒤 · 연결 시각 · 창 경계
    def test_link_signal_uses_earliest_observation(self):                      # X2
        obs = [issue_created("ABC-1", ts(0)), issue_event("ABC-1", 1, "issue.resolved", ts(5), labels={"resolution": ["Fixed"]}),
               issue_created("ABC-2", ts(10)),
               issue_event("ABC-2", 1, "issue.linked", ts(12), keys={"links": ["is caused by|ABC-1"]}),   # 창 안
               issue_event("ABC-1", 2, "issue.linked", ts(300), keys={"links": ["causes|ABC-2"]})]        # 같은 연결, 늦게
        u = self.stitch_(obs, 400)["wu:issue:ABC-1"]
        self.assertEqual(u["stats"]["outcome"], "followup_fix")

    def test_labels_keep_updating_after_freeze(self):                          # X3
        obs = [issue_created("ABC-1", ts(0), comps=("core",)),
               issue_event("ABC-1", 1, "issue.resolved", ts(5), labels={"resolution": ["Fixed"]}),
               issue_event("ABC-1", 2, "issue.changed", ts(200), labels={"field": ["component"], "component": ["net"]})]
        self.assertEqual(self.stitch_(obs, 300)["wu:issue:ABC-1"]["stats"]["labels"]["component"], ["net"])

    def test_freeze_starts_exactly_after_the_window(self):                     # X5
        def run(reopen_day):
            obs = [issue_created("ABC-1", ts(0)), issue_event("ABC-1", 1, "issue.resolved", ts(5), labels={"resolution": ["Fixed"]}),
                   issue_event("ABC-1", 2, "issue.reopened", ts(reopen_day))]
            return self.stitch_(obs, 200)["wu:issue:ABC-1"]
        inside, outside = run(34), run(36)                                       # 창 30일: 35일째가 경계
        self.assertIsNone(inside["span"]["end"])                                 # 창 안의 재오픈은 끝을 지운다
        self.assertEqual(outside["stats"]["outcome"], "clean")                   # 창 뒤의 재오픈은 결과를 못 바꾼다
        self.assertEqual(outside["stats"]["late_reopens"], 1)

    def test_late_reopen_counted_once_per_time(self):                          # 4차 1
        t = ts(100)
        obs = [issue_created("ABC-1", ts(0)), issue_event("ABC-1", 1, "issue.resolved", ts(5), labels={"resolution": ["Fixed"]}),
               issue_event("ABC-1", 2, "issue.transition", t, labels={"status": ["Reopened"]}, metrics={"reopen": True}),
               issue_event("ABC-1", 3, "issue.reopened", t)]                       # Jira: 재오픈 하나 = 사건 둘
        self.assertEqual(self.stitch_(obs, 120)["wu:issue:ABC-1"]["stats"]["late_reopens"], 1)

    def test_open_now_follows_transition_only_workflow(self):                  # S5 · S6
        done = {"to_done": True}
        obs = [issue_created("ABC-1", ts(0)),
               issue_event("ABC-1", 1, "issue.transition", ts(5), labels={"status": ["Done"]}, metrics=done),
               issue_event("ABC-1", 2, "issue.transition", ts(100), labels={"status": ["Open"]}, metrics={"reopen": True}),
               issue_event("ABC-1", 3, "issue.transition", ts(110), labels={"status": ["Done"]}, metrics=done),
               commit("m", ts(3), ["x.py"], {"mr_ids": ["repo!9"]})]
        u = self.stitch_(obs, 120)
        self.assertEqual((u["wu:issue:ABC-1"]["stats"]["late_reopens"], u["wu:issue:ABC-1"]["stats"]["open_now"]), (1, False))
        self.assertFalse(u["wu:mr:repo!9"]["stats"]["open_now"])                 # 이슈가 아닌 WU: 끝났으면 닫힘

    def test_ordinary_open_issue_is_not_called_reopened(self):                 # S11
        line = context.render_md({"issue": {"key": "ABC-1", "known": True, "start": ts(0), "end": None, "outcome": None,
                                            "late_reopens": 0, "open_now": True, "labels": {}}})
        self.assertIn("진행 중", line)
        self.assertNotIn("확정 뒤", line)

    def test_context_shows_reopen_after_settlement(self):                      # 3차 3
        obs = [issue_created("ABC-1", ts(0)), issue_event("ABC-1", 1, "issue.resolved", ts(5), labels={"resolution": ["Fixed"]}),
               issue_event("ABC-1", 2, "issue.reopened", ts(100))]
        u = self.stitch_(obs, 120)["wu:issue:ABC-1"]
        self.assertEqual((u["stats"]["outcome"], u["stats"]["open_now"]), ("clean", True))
        self.p.stitched.parent.mkdir(parents=True, exist_ok=True)
        self.p.stitched.write_text(json.dumps(u, ensure_ascii=False) + "\n", encoding="utf-8")
        info = context.issue_info(context.Model(self.p), self.p, "ABC-1")              # 실제 경로: 작업단위 → 맥락
        self.assertIn("지금 진행 중", context.render_md({"issue": info}))
        closed = obs + [issue_event("ABC-1", 3, "issue.resolved", ts(110), labels={"resolution": ["Fixed"]})]
        self.assertFalse(self.stitch_(closed, 120)["wu:issue:ABC-1"]["stats"]["open_now"])

    # --- 마이닝 · 읽기 규칙
    def test_typed_issue_overrides_untyped_fix_unit(self):                     # X13
        obs = [issue_created("ABC-1", ts(0), type_="Improvement"),                # ABC-2 는 트래커에 없다(유형 모름)
               commit("p", ts(1), ["src/a.py"], {"issue_keys": ["ABC-1", "ABC-2"]}, fix=True)]
        units, _ = stitch.stitch(obs, self.cfg, dt.datetime.fromisoformat(ts(50)))
        self.assertTrue(any(u["wu_id"] == "wu:issue:ABC-2" and u["stats"]["is_fix"] for u in units))  # 전제: 타입 없는 fix WU
        corp = corpusmod.build(obs, units, self.cfg, dt.datetime.fromisoformat(ts(50)))
        self.assertFalse(corp.commits[0].is_fix)

    def test_generation_id_covers_stitch_recipe(self):                         # 상관 의미가 바뀌면 세대도 바뀐다
        import atlas.mine as minemod
        self.assertIn(stitch.RECIPE, minemod.RECIPES)
        obs = [commit("a", ts(1), ["x.py"])]
        g1 = minemod.generation_id(obs, self.cfg, dt.datetime.fromisoformat(ts(2)))
        with mock.patch.object(minemod, "RECIPES", [r for r in minemod.RECIPES if r != stitch.RECIPE] + ["stitch@99"]):
            self.assertNotEqual(g1, minemod.generation_id(obs, self.cfg, dt.datetime.fromisoformat(ts(2))))

    def test_all_readers_prefer_current_units(self):                           # X14 · H
        from atlas.common import load_units, units_path
        self.assertIsNone(units_path(self.p))
        self.assertEqual(load_units(self.p), [])
        self.p.stitched.parent.mkdir(parents=True)
        self.p.stitched.write_text('{"wu_id": "s"}\n', encoding="utf-8")
        self.assertEqual([u["wu_id"] for u in load_units(self.p)], ["s"])
        self.p.units.write_text('{"wu_id": "c"}\n', encoding="utf-8")
        self.assertEqual([u["wu_id"] for u in load_units(self.p)], ["c"])


if __name__ == "__main__":
    unittest.main()
