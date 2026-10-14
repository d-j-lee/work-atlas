"""도입 · 운영 테스트 — 처음 설정(init) · 사내망 진단 · 점검(--online) · 주간 수확 · 정기 실행 · 보존 기한. 합성 데이터만 쓴다.

실행: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from atlas import config as configmod, init as initmod  # noqa: E402
from atlas.collect import CollectError, jira  # noqa: E402
from test_engine import make_root  # noqa: E402


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_DATE": "2026-09-01T10:00:00+09:00", "GIT_COMMITTER_DATE": "2026-09-01T10:00:00+09:00"})


def make_repo(base: Path, name: str, subjects: list[str]) -> Path:
    repo = base / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "me@corp.example")
    git(repo, "config", "user.name", "Me Example")
    for i, s in enumerate(subjects):
        (repo / f"f{i}.txt").write_text(s, encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", s)
    return repo


class TestInit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="atlas-init-"))
        self.root = make_root()
        subjects = [f"ABC-{i} 결제 오류 수정" for i in range(1, 9)] + ["OPS-3 배포 스크립트", "OPS-4 로그", "OPS-5 경보",
                                                                      "UTF-8 인코딩 정리", "README 고침", "SHA-256 해시로"]
        self.repo = make_repo(self.tmp / "src dir", "Server_App", subjects)      # 경로에 공백
        git(self.repo, "branch", "feature/ABC-77-login")
        self.claude = self.tmp / "projects"
        (self.claude / initmod.claude_project_name(self.repo)).mkdir(parents=True)
        (self.claude / "-other-project").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)
        configmod._load.cache_clear()

    def test_infers_keys_identity_and_sources_and_writes_a_valid_config(self):
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = initmod.main(["--root", str(self.root), "--repo", str(self.repo), "--jira-url", "https://jira.corp.example/",
                                 "--claude-projects", str(self.claude), "--since", "2020-01-01"])
        self.assertEqual(code, 0, err.getvalue())
        configmod._load.cache_clear()
        cfg = configmod.load(self.root)
        self.assertEqual(cfg["keys"]["projects"], ["ABC", "OPS"])                       # UTF·SHA 는 빼고, 3번 이상만
        self.assertEqual(cfg["identity"]["self"], ["me@corp.example", "Me Example"])
        g = cfg["sources"]["git"][0]
        self.assertEqual((g["name"], Path(g["path"]).name), ("server-app", "Server_App"))
        j = cfg["sources"]["jira"][0]
        self.assertEqual((j["flavor"], j["base_url"], j["jql"], j["auth_env"]),
                         ("datacenter", "https://jira.corp.example", "project in (ABC, OPS)", "ATLAS_JIRA_AUTH"))
        self.assertEqual(cfg["sources"]["claude_code"][0]["include"], [initmod.claude_project_name(self.repo)])
        self.assertIn("이슈 키 커버리지 79%", (self.root / "atlas.toml").read_text(encoding="utf-8"))   # 14개 중 11개
        self.assertIn("Claude Code 기록 폴더 일치 1개", err.getvalue())

    def test_does_not_overwrite_and_quotes_windows_paths(self):
        (self.root / "atlas.toml").write_text("# 사람이 고친 설정\n", encoding="utf-8")
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            self.assertEqual(initmod.main(["--root", str(self.root), "--repo", str(self.repo)]), 0)
        self.assertEqual((self.root / "atlas.toml").read_text(encoding="utf-8"), "# 사람이 고친 설정\n")
        self.assertIn("[[sources.git]]", out.getvalue())
        self.assertEqual(initmod.toml_str("C:\\Users\\me\\src"), '"C:\\\\Users\\\\me\\\\src"')
        self.assertEqual(initmod.claude_project_name(Path("C:/Users/me/src/server")).lstrip("-")[:2], "C-")

    def test_refuses_outside_an_instance_and_without_repos(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(initmod.main(["--root", str(self.tmp), "--repo", str(self.repo)]), 2)
            self.assertEqual(initmod.main(["--root", str(self.root)]), 2)

    def test_choose_projects_needs_three_hits_and_two_percent(self):
        from collections import Counter
        self.assertEqual(initmod.choose_projects(Counter({"ABC": 10, "OPS": 3, "TMP": 2})), ["ABC", "OPS"])   # 우연한 2회는 뺀다
        self.assertEqual(initmod.choose_projects(Counter({"ABC": 300, "OPS": 5})), ["ABC"])                   # 2% 미만도 뺀다

    def test_cloud_jql_is_bounded(self):
        text, _ = initmod.run(self.root, [self.repo], "2020-01-01", "https://corp.atlassian.net", None, self.claude)
        self.assertIn('flavor = "cloud"', text)
        self.assertIn("AND updated >= -365d", text)


class FakeOpener:
    def __init__(self, exc=None, body=None, raw=None, ctype="application/json"):
        self.exc, self.body, self.raw, self.ctype, self.calls = exc, body, raw, ctype, 0

    def open(self, req, timeout=60):
        self.calls += 1
        if self.exc:
            raise self.exc
        resp = io.BytesIO(self.raw if self.raw is not None else json.dumps(self.body).encode())
        resp.headers = {"Content-Type": self.ctype}
        return resp


class TestCorporateNetwork(unittest.TestCase):
    def test_certificate_and_name_errors_fail_fast_with_guidance(self):
        cert = urllib.error.URLError(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in chain"))
        o = FakeOpener(cert)
        with self.assertRaises(CollectError) as e:
            jira.http_fetch("https://jira.corp.example/rest/api/2/search", {}, opener=o)
        self.assertEqual(o.calls, 1)                                                  # 다시 시도하지 않는다(예전엔 1분)
        self.assertIn("ca_bundle", str(e.exception))
        o = FakeOpener(urllib.error.URLError(socket.gaierror(-2, "Name or service not known")))
        with self.assertRaises(CollectError) as e:
            jira.http_fetch("https://jira.corp.example/x", {}, opener=o)
        self.assertEqual(o.calls, 1)
        self.assertIn("VPN", str(e.exception))
        self.assertIsNone(jira.network_problem(ConnectionResetError()))               # 일시적일 수 있는 것은 다시 시도한다

    def test_auth_and_proxy_errors_carry_a_hint(self):
        for code, word in ((401, "토큰"), (403, "권한"), (407, "HTTPS_PROXY")):
            err = urllib.error.HTTPError("https://j/x", code, "x", {}, io.BytesIO(b"{}"))
            with self.assertRaises(CollectError) as e:
                jira.http_fetch("https://j/x", {}, opener=FakeOpener(err))
            self.assertIn(word, str(e.exception))

    def test_proxy_tunnel_407_strict_ca_and_html_pages_fail_fast_with_guidance(self):
        o = FakeOpener(urllib.error.URLError(OSError("Tunnel connection failed: 407 Proxy Authentication Required")))
        with self.assertRaises(CollectError) as e:
            jira.http_fetch("https://jira.corp.example/x", {}, opener=o)
        self.assertEqual(o.calls, 1)                                                  # HTTPS 프록시의 407 은 HTTPError 가 아니다
        self.assertIn("NTLM", str(e.exception))
        strict = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                                                 "Basic Constraints of CA cert not marked critical")
        self.assertIn("tls_strict = false", jira.network_problem(strict))
        self.assertIn("tls_strict", jira.network_problem(OSError("Missing Authority Key Identifier")))
        self.assertIn("tls_strict", jira.network_problem(OSError("certificate verify failed: CA cert does not include key usage extension")))
        o = FakeOpener(raw=b"<html><body>SSO login</body></html>", ctype="text/html; charset=utf-8")
        with self.assertRaises(CollectError) as e:
            jira.http_fetch("https://jira.corp.example/x", {}, opener=o)
        self.assertIn("SSO", str(e.exception))
        self.assertEqual(jira.http_fetch("https://j/x", {}, opener=FakeOpener(body={"a": 1})), {"a": 1})

    def test_tls_strict_false_clears_only_the_strict_flag(self):
        if not hasattr(ssl, "VERIFY_X509_STRICT"):
            self.skipTest("이 Python 에는 엄격 플래그가 없다")
        for value in (False, "false", "no"):                                          # 따옴표로 적어도 같은 뜻
            opener = jira.opener_for({"tls_strict": value})
            ctx = next(h._context for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler))
            self.assertFalse(ctx.verify_flags & ssl.VERIFY_X509_STRICT)
        self.assertIs(jira.opener_for({"tls_strict": "true"}), jira._OPENER)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)                          # 검증 자체는 그대로
        self.assertTrue(ctx.check_hostname)

    def test_ca_bundle_is_added_to_the_system_store(self):
        with self.assertRaises(CollectError):
            jira.opener_for({"ca_bundle": "/nonexistent/company-root.pem"})
        bad = Path(tempfile.mkdtemp()) / "x.pem"
        bad.write_text("not a certificate", encoding="utf-8")
        with self.assertRaises(CollectError):
            jira.opener_for({"ca_bundle": str(bad)})
        cafile = ssl.get_default_verify_paths().cafile
        if not cafile or not Path(cafile).exists():
            self.skipTest("이 환경에는 시스템 CA 파일이 없다")
        self.assertIsNot(jira.opener_for({"ca_bundle": cafile}), jira._OPENER)
        self.assertIs(jira.opener_for({}), jira._OPENER)

    def test_probe_reports_reachability_then_credentials(self):
        seen = []

        def fetch(url, headers):
            seen.append((url, "Authorization" in headers))
            if url.endswith("/myself"):
                raise CollectError("HTTP 401 — 인증 실패")
            return {"serverTitle": "사내 Jira", "version": "9.12"}
        os.environ["ATLAS_TEST_JIRA"] = "token"
        try:
            rows = jira.probe({"base_url": "https://jira.corp.example", "auth_env": "ATLAS_TEST_JIRA"}, fetch=fetch)
        finally:
            os.environ.pop("ATLAS_TEST_JIRA", None)
        self.assertEqual([ok for ok, _ in rows], [True, False])
        self.assertIn("9.12", rows[0][1])
        self.assertEqual(seen, [("https://jira.corp.example/rest/api/2/serverInfo", False),     # 서버 정보는 자격 증명 없이
                                ("https://jira.corp.example/rest/api/2/myself", True)])


class BuiltCase(unittest.TestCase):
    """카테고리 시나리오로 빌드한 인스턴스 — 수확 · 보존 기한 · 쓰임 시험이 함께 쓴다."""
    extra_toml = ""

    def setUp(self):
        from atlas import pipeline
        from atlas.common import paths
        from atlas.store import append_observations
        from test_category import TOML, scenario
        self.root = make_root(TOML + self.extra_toml)
        self.p = paths(self.root)
        append_observations(self.p, scenario(), {"issue:trk:PAY-1#created": "결제 승인 오류"})
        configmod._load.cache_clear()
        self.cfg = configmod.load(self.root)
        with redirect_stdout(io.StringIO()):
            pipeline.run(self.p, self.cfg, "2026-04-01T00:00:00+00:00", report=False, log=lambda *a: None)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        configmod._load.cache_clear()


class TestHarvest(BuiltCase):
    def test_runs_end_to_end_quietly_and_reports(self):
        from atlas import harvest, record
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            record.main(["--root", str(self.root), "triage", "--anchor", "issue:PAY-1", "--archetype", "residual", "--conf", "low",
                         "--scale", "small"])
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(harvest.main(["--root", str(self.root), "--quiet", "--as-of", "2026-04-01T00:00:00+00:00"]), 0)
        first = out.getvalue()
        last = json.loads((self.p.state / "harvest-last.json").read_text(encoding="utf-8"))
        self.assertTrue(last["ok"])
        md = Path(last["report"]).read_text(encoding="utf-8")
        for part in ("## 수집", "## 빌드", "## 경고", "## 확인 질문", "issue:PAY-1", "atlas record feedback"):
            self.assertIn(part, md)                                                   # 판정이 없는 끝난 일을 묻는다
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            harvest.main(["--root", str(self.root), "--quiet", "--as-of", "2026-04-01T00:00:00+00:00"])
        self.assertEqual(out.getvalue(), "")                                          # 실패도 새 경고도 없으면 침묵
        self.assertIn("수확 시작", (self.p.state / "harvest.log").read_text(encoding="utf-8"))
        self.assertTrue(first == "" or "새 경고" in first)

    def test_one_failing_source_does_not_stop_the_rest_and_exit_code_says_so(self):
        from atlas import harvest
        cfg = json.loads(json.dumps(self.cfg))
        cfg["sources"]["git"] = [{"name": "gone", "path": str(Path(tempfile.gettempdir()) / "no-such-repo-atlas")}]
        res = harvest.run(self.p, cfg, as_of="2026-04-01T00:00:00+00:00", log=lambda *a: None)
        self.assertFalse(res["ok"])
        self.assertTrue(res["failures"][0].startswith("git:gone"))
        self.assertIsNotNone(res["build"])                                            # 빌드는 했다
        self.assertIn("**실패가 있다**", Path(res["report"]).read_text(encoding="utf-8"))

    def test_a_broken_step_still_writes_the_report_and_last_state(self):
        from atlas import harvest, retention
        with unittest.mock.patch.object(retention, "purge", side_effect=OSError("disk")):
            res = harvest.run(self.p, self.cfg, collect=False, as_of="2026-04-01T00:00:00+00:00", log=lambda *a: None)
        self.assertFalse(res["ok"])
        self.assertTrue(res["failures"][0].startswith("보존 기한 — OSError"))
        self.assertIsNotNone(res["build"])                                            # 다음 단계는 돌았다
        last = json.loads((self.p.state / "harvest-last.json").read_text(encoding="utf-8"))
        self.assertFalse(last["ok"])
        self.assertIn("보존 기한을 집행하지 못했다", Path(last["report"]).read_text(encoding="utf-8"))
        with unittest.mock.patch.object(harvest, "write_report", side_effect=KeyError("x")):
            res = harvest.run(self.p, self.cfg, collect=False, as_of="2026-04-01T00:00:00+00:00", log=lambda *a: None)
        self.assertIn("리포트를 만들지 못했다", Path(res["report"]).read_text(encoding="utf-8"))
        self.assertFalse(res["ok"])                                                   # 리포트 단계의 실패도 실패다(종료 코드 1)
        self.assertFalse(json.loads((self.p.state / "harvest-last.json").read_text(encoding="utf-8"))["ok"])
        self.assertIn("리포트 — KeyError", json.loads((self.p.state / "harvest-last.json").read_text(encoding="utf-8"))["failures"][0])
        from atlas import usage
        real_write = harvest.write_text
        def no_reports(path, text):
            if Path(path).parent == self.p.reports:
                raise OSError("reports 에 쓸 수 없다")
            return real_write(path, text)
        (self.p.state / "harvest-last.json").write_text('"x"', encoding="utf-8")       # 객체가 아닌 지난 상태
        with unittest.mock.patch.object(harvest, "write_text", side_effect=no_reports), \
                unittest.mock.patch.object(usage, "summary", side_effect=ValueError("깨진 기록")):
            res = harvest.run(self.p, self.cfg, collect=False, as_of="2026-04-01T00:00:00+00:00", log=lambda *a: 1 / 0)
        last = json.loads((self.p.state / "harvest-last.json").read_text(encoding="utf-8"))
        self.assertFalse(last["ok"])                                                  # 리포트를 어디에도 못 써도 상태는 남는다
        self.assertIsNone(last["report"])
        self.assertTrue(any(x.startswith("쓰임 — ValueError") for x in last["failures"]))   # 쓰임 실패도 조용히 사라지지 않는다
        self.assertTrue(any(x.startswith("리포트(대체) — OSError") for x in last["failures"]))

    def test_doctor_reads_a_broken_last_state_without_crashing(self):
        from atlas import doctor
        self.p.state.mkdir(parents=True, exist_ok=True)
        for bad in ('"x"', '{"ok": false}', '{"finished": "어제", "ok": true}', "{깨짐"):
            (self.p.state / "harvest-last.json").write_text(bad, encoding="utf-8")
            with self.subTest(state=bad), redirect_stdout(io.StringIO()):
                doctor.run(self.p, self.cfg)                                          # 상태 파일이 깨져도 점검은 끝까지

    def test_lock_and_expired_inbox(self):
        from atlas import harvest
        (self.root / "inbox").mkdir()
        old = self.root / "inbox" / "20260101-0900-old.md"
        old.write_text("# 오래된 제안\n", encoding="utf-8")
        t = __import__("time").time() - 40 * 86400
        os.utime(old, (t, t))
        res = harvest.run(self.p, self.cfg, collect=False, as_of="2026-04-01T00:00:00+00:00", log=lambda *a: None)
        self.assertIn("만료된 수신함", Path(res["report"]).read_text(encoding="utf-8"))
        self.assertTrue(old.exists())                                                 # 무인 실행은 지우지 않는다
        (self.p.state / "harvest.lock").write_text("", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            harvest.run(self.p, self.cfg, collect=False, log=lambda *a: None)


class TestNoConsoleWindow(unittest.TestCase):
    def test_every_child_process_is_started_without_a_console_window_on_windows(self):
        """무인 실행(pythonw · 작업 스케줄러)에서 git · claude 가 콘솔 창을 띄우지 않게 — 새 호출이 빠뜨리지 않도록 소스에서 본다."""
        import ast
        missing = []
        for f in sorted((REPO / "atlas").rglob("*.py")):
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("run", "Popen")
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"):
                    kws = {k.arg for k in node.keywords}
                    if "creationflags" not in kws and None not in kws:                # creationflags= 또는 **NO_WINDOW · **kw
                        missing.append(f"{f.relative_to(REPO)}:{node.lineno}")
        self.assertEqual(missing, [])
        from atlas.common import NO_WINDOW
        self.assertEqual(NO_WINDOW, {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {})


class TestPortablePaths(unittest.TestCase):
    def test_rooted_and_inside(self):
        from atlas.common import relative_inside, rooted
        self.assertTrue(rooted("/etc/hosts") and rooted("\\\\srv\\share\\x") and not rooted("src/a.py"))  # 드라이브 없는 루트도 절대
        root = Path(tempfile.mkdtemp(prefix="atlas-path-"))
        try:
            (root / "src").mkdir()
            self.assertEqual(relative_inside(root / "src" / "new.py", root), "src/new.py")          # 아직 없는 파일도
            self.assertEqual(relative_inside(str(root / "src" / ".." / "src" / "a.py"), str(root)), "src/a.py")
            self.assertIsNone(relative_inside(root.parent / "elsewhere.py", root))                  # 밖
            self.assertIsNone(relative_inside(root, root))                                          # 루트 자체
            (root / "..weird").mkdir()
            self.assertEqual(relative_inside(root / "..weird" / "x", root), "..weird/x")            # '..' 로 시작하는 이름은 안
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestSchedule(unittest.TestCase):
    def test_windows_task_is_powershell_with_catch_up_and_safe_quoting(self):
        from atlas import harvest
        txt = harvest.schedule_text(Path("C:/Users/o'brien/atlas inst"), "windows", "TUE", "08:30", python="C:/Py/python.exe")
        self.assertIn("Register-ScheduledTask", txt)
        self.assertIn("-StartWhenAvailable", txt)                                     # 꺼져 있어 지나친 실행은 켜진 뒤
        self.assertIn("-DaysOfWeek Tuesday -At '08:30'", txt)
        self.assertIn("o''brien", txt)                                                # 작은따옴표 이스케이프
        self.assertIn('--root "C:', txt)

    def test_cron_and_launchd(self):
        from xml.dom import minidom
        from atlas import harvest
        cron = harvest.schedule_text(Path("/srv/atlas inst"), "cron", "MON", "09:00", python="/usr/bin/python3")
        self.assertIn("0 9 * * 1 cd '/srv/atlas inst'", cron)
        plist = harvest.schedule_text(Path("/Users/me/atlas"), "launchd", "SUN", "07:05", python="/usr/bin/python3")
        doc = minidom.parseString(plist.encode("utf-8"))
        ints = [n.firstChild.data for n in doc.getElementsByTagName("integer")]
        self.assertEqual(ints, ["0", "7", "5"])


class TestRetention(BuiltCase):
    extra_toml = '[retention]\nexcerpt_days = 30\nllm_cache_days = 10\n[retention.excerpt_days_by_system]\nvcs = 100000\n'

    def test_purges_old_excerpts_and_derived_caches_only(self):
        import datetime as dt
        from atlas import retention
        from atlas.common import read_observations
        obs = read_observations(self.p.observations)
        iss = next(o for o in obs if o["source"]["system"] == "issue")
        vcs = next(o for o in obs if o["source"]["system"] == "vcs")
        self.p.excerpts.write_text("\n".join(json.dumps({"obs_id": o["obs_id"], "text": "본문"}) for o in (iss, vcs)) + "\n",
                                   encoding="utf-8")
        render = self.root / "data" / "render"
        render.mkdir(parents=True, exist_ok=True)
        (render / "x.md").write_text("발췌가 든 입력 문서", encoding="utf-8")
        cache = self.root / "data" / "llm" / "cache" / "category_card"
        cache.mkdir(parents=True)
        (cache / "old.json").write_text("{}", encoding="utf-8")
        t = __import__("time").time() - 20 * 86400
        os.utime(cache / "old.json", (t, t))
        n_obs = len(obs)
        r = retention.purge(self.p, self.cfg, dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc))
        self.assertEqual((r["excerpts"], r["excerpts_kept"], r["render_cleared"], r["llm_cache"]), (1, 1, True, 1))
        kept = [json.loads(x)["obs_id"] for x in self.p.excerpts.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(kept, [vcs["obs_id"]])                                       # 시스템별 기한이 이긴다
        self.assertEqual(len(read_observations(self.p.observations)), n_obs)          # 관측은 지우지 않는다
        self.assertIn("발췌 1개", retention.describe(r))

    def test_counts_annotations_the_purge_makes_stale(self):
        import datetime as dt
        from atlas import retention
        from atlas.common import read_jsonl, read_observations
        iss = next(o for o in read_observations(self.p.observations) if o["source"]["system"] == "issue")
        self.p.excerpts.write_text(json.dumps({"obs_id": iss["obs_id"], "text": "본문"}) + "\n", encoding="utf-8")
        unit = next(u for u in read_jsonl(self.p.stitched) if any(m["obs_id"] == iss["obs_id"] for m in u["members"]))
        self.p.annotated.write_text(json.dumps({"wu_id": unit["wu_id"], "annotation": {"x": 1}}) + "\n", encoding="utf-8")
        r = retention.purge(self.p, self.cfg, dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc), dry_run=True)
        self.assertEqual((r["excerpts"], r["annotations_stale"]), (1, 1))
        self.assertIn("주석 1개", retention.describe(r))
        self.assertTrue(self.p.excerpts.read_text(encoding="utf-8"))                  # 미리보기는 지우지 않는다

    def test_voided_observations_follow_the_limit_and_stale_annotations_are_counted(self):
        import datetime as dt
        from atlas import retention
        from atlas.common import read_jsonl, read_observations
        from atlas.store import append_observations
        from test_engine import ob
        iss = next(o for o in read_observations(self.p.observations) if o["source"]["system"] == "issue")
        append_observations(self.p, [ob("issue:t:fix#void", "issue", "issue.correction", "2026-03-01T00:00:00+00:00",
                                        objects=[{"type": "obs", "id": iss["obs_id"]}])])
        self.assertNotIn(iss["obs_id"], {o["obs_id"] for o in read_observations(self.p.observations)})   # 무효가 됐다
        self.p.excerpts.write_text(json.dumps({"obs_id": iss["obs_id"], "text": "무효가 된 본문"}) + "\n", encoding="utf-8")
        unit = next(u for u in read_jsonl(self.p.stitched) if any(m["obs_id"] == iss["obs_id"] for m in u["members"]))
        self.p.annotated.write_text(json.dumps({"wu_id": unit["wu_id"], "annotation": {"x": 1}}) + "\n", encoding="utf-8")
        r = retention.purge(self.p, self.cfg, dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc))
        self.assertEqual((r["excerpts"], r["annotations_stale"]), (1, 0))             # 무효여도 기한이 지나면 지운다 — 낡게 한 것은 정정이다

    def test_zero_means_keep(self):
        from atlas import retention
        cfg = json.loads(json.dumps(self.cfg)); cfg["retention"] = {"excerpt_days": 0}
        self.p.excerpts.write_text(json.dumps({"obs_id": "x", "text": "t"}) + "\n", encoding="utf-8")
        self.assertEqual(retention.purge(self.p, cfg)["excerpts"], 0)


class TestUsage(BuiltCase):
    def _row(self, **kw):
        f = self.root / "data" / "usage" / "2026-01.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(kw, ensure_ascii=False) + "\n")

    def _session(self, sid, paths_, collected):
        from atlas.store import append_observations
        from test_engine import ob
        o = ob(f"ai_session:cc:{sid}#40", "ai_session", "ai_session.session", "2026-01-02T09:00:00+00:00",
               objects=[{"type": "ai_session", "id": sid}], keys={"paths": paths_}, metrics={"edits": 2, "prompts": 3})
        o["collected_at"] = collected
        append_observations(self.p, [o])

    def test_uptake_by_arm_pending_and_context_precision(self):
        import datetime as dt
        from atlas import pipeline, usage
        self._row(ts="2026-01-02T09:10:00+00:00", kind="hook.edit", session="S1", subject="src/pay/api/Handler.java",
                  suggested=["src/pay/api/H1Test.java", "x.java"], arm="shown")
        self._row(ts="2026-01-02T09:10:00+00:00", kind="hook.edit", session="S2", subject="a.java", suggested=["y.java"], arm="held")
        self._row(ts="2026-01-02T09:10:00+00:00", kind="hook.edit", session="S3", subject="a.java", suggested=["y.java"], arm="shown")
        for _ in range(3):                                                            # 같은 세션의 같은 이슈 맥락은 한 번
            self._row(ts="2026-01-01T00:00:00+00:00", kind="context.issue", issue="PAY-1", via="hook", session="S1",
                      suggested=["src/pay/api/Handler.java", "nope.java"])
        self._session("S1", ["src/pay/api/Handler.java", "src/pay/api/H1Test.java"], "2026-01-03T00:00:00+00:00")
        self._session("S2", ["a.java", "z.java"], "2026-01-03T00:00:00+00:00")
        with redirect_stdout(io.StringIO()):
            pipeline.run(self.p, self.cfg, "2026-04-01T00:00:00+00:00", report=False, log=lambda *a: None)
        soon = dt.datetime(2026, 1, 20, tzinfo=dt.timezone.utc)
        ev = usage.evaluate(self.p, self.cfg, now=soon)
        self.assertEqual(ev["hook"]["shown"], {"n": 1, "sessions": 1, "uptake": 1.0, "file_rate": 0.5})
        self.assertEqual(ev["hook"]["held"]["uptake"], 0.0)
        self.assertEqual((ev["pending"], ev["expired"]), (1, 0))                       # S3 는 아직 수집되지 않았다
        self.assertEqual((ev["context"]["n"], ev["duplicates"]), (1, 2))
        self.assertEqual(ev["context"]["precision"], 0.5)                             # PAY-1 이 Handler.java 를 바꿨다
        d = ev["hook"]["difference"]
        self.assertEqual((d["sessions"], d["enough"], d["significant"]), ([1, 1], False, False))   # 세션 1:1 로는 판단하지 않는다
        lines = usage.summary(self.p, self.cfg, now=soon)["lines"]
        self.assertTrue(any("세션이 아직 적다" in x for x in lines), lines)
        late = usage.evaluate(self.p, self.cfg, now=dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc))
        self.assertEqual((late["pending"], late["expired"]), (0, 1))                  # 35일 넘게 세션이 안 잡히면 만료

    def test_issue_context_expires_when_unseen_or_open_too_long(self):
        import datetime as dt
        from atlas import usage
        self._row(ts="2026-01-01T00:00:00+00:00", kind="context.issue", issue="NOPE-9", via="cli", suggested=["a.java"])
        self._row(ts="2026-03-25T00:00:00+00:00", kind="context.issue", issue="OPS-1", via="cli", suggested=["a.java"])
        ev = lambda y, m, d: usage.evaluate(self.p, self.cfg, now=dt.datetime(y, m, d, tzinfo=dt.timezone.utc))
        self.assertEqual(((e := ev(2026, 1, 20))["pending"], e["expired"]), (2, 0))
        self.assertEqual(((e := ev(2026, 5, 1))["pending"], e["expired"]), (1, 1))    # 안 잡힌 이슈는 35일, 열린 이슈는 180일
        self.assertEqual(((e := ev(2026, 10, 1))["pending"], e["expired"]), (0, 2))

    def test_bad_rows_unknown_sessions_and_cli_days_do_not_break_or_bias(self):
        import datetime as dt
        from atlas import usage
        self._row(ts="2026-03-30T00:00:00", kind="context.issue", issue="PAY-1", suggested=["a.java"])       # 시간대 없음
        self._row(ts="어제", kind="hook.edit", session="S9", suggested=["a.java"])                            # 시각이 아님
        for day in ("2026-03-20", "2026-03-20", "2026-03-21"):
            self._row(ts=f"{day}T09:00:00+00:00", kind="context.issue", issue="PAY-1", via="cli", suggested=["a.java"])
        ev = usage.evaluate(self.p, self.cfg, now=dt.datetime(2026, 4, 1))                                  # 시간대 없는 now 도 된다
        self.assertEqual((ev["duplicates"], ev["no_change"] + (ev["context"] or {}).get("n", 0)), (1, 2))  # 세션 없는 CLI 는 하루에 한 번
        self.assertEqual({usage.arm({"usage": {"holdout": 0.5}}, s) for s in ("unknown", "", None)}, {"shown"})  # 모르는 세션은 보류하지 않는다
        shown = [{"session": "a", "hit": True}]
        held = [{"session": "b", "hit": False}]
        self.assertTrue(usage._diff_ci(shown, held, min_sessions=0)["enough"])
        cfg = json.loads(json.dumps(self.cfg)); cfg["usage"]["min_sessions"] = 0
        self._row(ts="2026-01-02T09:10:00+00:00", kind="hook.edit", session="S1", suggested=["x.java"], arm="shown")
        self._row(ts="2026-01-02T09:10:00+00:00", kind="hook.edit", session="S2", suggested=["x.java"], arm="held")
        self._session("S1", ["x.java"], "2026-01-03T00:00:00+00:00")
        self._session("S2", ["y.java"], "2026-01-03T00:00:00+00:00")
        self.assertTrue(usage.evaluate(self.p, cfg, now=dt.datetime(2026, 1, 10, tzinfo=dt.timezone.utc))["hook"]["difference"]["enough"])

    def test_finished_issue_without_later_commits_is_counted_apart(self):
        import datetime as dt
        from atlas import usage
        self._row(ts="2026-03-30T00:00:00+00:00", kind="context.issue", issue="PAY-1", via="cli", suggested=["a.java"])
        ev = usage.evaluate(self.p, self.cfg, now=dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc))
        self.assertEqual((ev["no_change"], ev["context"]), (1, None))                 # 맥락 뒤 커밋이 없다 — 정밀도에 넣지 않는다

    def test_difference_needs_enough_sessions_on_both_sides(self):
        from atlas import usage
        shown = [{"session": f"a{i}", "hit": i % 3 != 0} for i in range(30)]
        held = [{"session": f"b{i}", "hit": i % 3 == 0} for i in range(30)]
        d = usage._diff_ci(shown, held, min_sessions=20)
        self.assertTrue(d["enough"])
        self.assertTrue(d["significant"])
        self.assertFalse(usage._diff_ci(shown, held[:19], min_sessions=20)["significant"])

    def test_arm_is_per_session_stable_capped_and_off_without_records_and_hook_holds_back(self):
        from atlas import hook, usage
        self.assertEqual(usage.arm({"usage": {"holdout": 0}}, "s"), "shown")
        arms = [usage.arm({"usage": {"holdout": 1.0}}, f"s{i}") for i in range(200)]
        self.assertIn("shown", arms)                                                  # 0.5 로 묶인다 — 알림이 사라지지 않는다
        self.assertLess(arms.count("held"), 140)
        self.assertEqual(usage.arm({"usage": {"holdout": 0.3}}, "s7"), usage.arm({"usage": {"holdout": 0.3}}, "s7"))
        self.assertEqual({usage.arm({"usage": {"holdout": 0.5, "record": False}}, f"s{i}") for i in range(40)}, {"shown"})
        from atlas import pipeline
        from atlas.store import append_observations
        from test_engine import commit
        append_observations(self.p, [commit(f"c{i}", f"2026-02-0{i + 1}T10:00:00+00:00", ["src/core/A.java", "src/core/ATest.java"])
                                     for i in range(4)])                             # 늘 함께 바뀐 짝
        with redirect_stdout(io.StringIO()):
            pipeline.run(self.p, self.cfg, "2026-04-01T00:00:00+00:00", report=False, log=lambda *a: None)
        cfg = json.loads(json.dumps(self.cfg)); cfg["usage"]["holdout"] = 0.5
        held = next(f"sess{i}" for i in range(200) if usage.arm(cfg, f"sess{i}") == "held")
        shown = next(f"sess{i}" for i in range(200) if usage.arm(cfg, f"sess{i}") == "shown")
        ev = lambda s: {"hook_event_name": "PostToolUse", "tool_name": "Edit", "session_id": s, "cwd": str(self.root),
                        "tool_input": {"file_path": str(self.root / "src/core/A.java")}}
        configmod._load.cache_clear()
        with unittest.mock.patch.object(configmod, "load", return_value=cfg):
            told = hook.handle(ev(shown), str(self.root))
            quiet = hook.handle(ev(held), str(self.root))
        self.assertIn("함께 바뀌어 온", json.dumps(told, ensure_ascii=False))
        self.assertNotIn("함께 바뀌어 온", json.dumps(quiet, ensure_ascii=False) if quiet else "")
        logged = [r for r in usage.rows(self.p) if r["kind"] == "hook.edit"]
        self.assertEqual(sorted(r["arm"] for r in logged), ["held", "shown"])


class TestCollectorKit(BuiltCase):
    def test_template_output_passes_the_contract_and_previews_links(self):
        import importlib.util
        from atlas import ingest
        spec = importlib.util.spec_from_file_location("collector_template", REPO / "method/recipes/collector_template.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        export = self.root / "export.json"
        export.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in (
            {"id": "m1", "channel": "pay-dev", "thread": "t9", "time": "2026-03-01T10:00:00+09:00", "author": "kim@corp.example",
             "text": "PAY-1 재현됨, 토큰 sk-abcdefghijklmnopqrstuvwx 확인 바람"},
            {"id": "m2", "channel": "pay-dev", "time": "2026-03-01T11:00:00+09:00", "author": "lee@corp.example", "text": "점심 메뉴"},
        )) + "\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mod.main(["--root", str(self.root), "--input", str(export), "--out", str(self.root / "out/chat")]), 0)
        rows = [json.loads(x) for x in (self.root / "out/chat.obs.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["keys"]["issue_keys"] for r in rows], [["PAY-1"], []])
        self.assertEqual({r["actor_role"] for r in rows}, {"unknown"})                # 사람 식별자는 남지 않는다
        self.assertNotIn("kim@", json.dumps(rows, ensure_ascii=False))
        self.assertNotIn("pay-dev", json.dumps(rows, ensure_ascii=False))               # 그릇 이름도 해시로
        self.assertEqual(rows[0]["objects"][0]["type"], "thread")
        kit = mod.Kit(str(self.root), "chat", "사내-메신저", "t@1")
        self.assertEqual(rows[0]["objects"][0]["id"], kit.ref("thread", "pay-dev/t9"))  # 같은 값 = 같은 해시 — 스레드로 묶인다
        self.assertEqual(kit._object({"type": "issue", "id": "PAY-1"}), {"type": "issue", "id": "PAY-1"})   # 잇는 객체는 그대로
        self.assertEqual(kit._object({"type": "work", "id": "PAY-1"}), {"type": "work", "id": "PAY-1"})
        plain = kit.ref("thread", "pay-dev/t9")
        with unittest.mock.patch.dict(os.environ, {"ATLAS_HASH_KEY": "s3cret"}):
            keyed = kit.ref("thread", "pay-dev/t9")
            self.assertEqual(keyed, kit.ref("thread", "pay-dev/t9"))                  # 비밀 값이 있어도 결정론
        self.assertNotEqual(plain, keyed)                                             # 인스턴스 이름만으로는 되짚지 못한다
        self.assertNotIn("sk-abcdefghij", (self.root / "out/chat.excerpts.jsonl").read_text(encoding="utf-8"))
        out = io.StringIO()
        with redirect_stdout(out):
            code = ingest.main(["--root", str(self.root), str(self.root / "out/chat.obs.jsonl"),
                                "--excerpts", str(self.root / "out/chat.excerpts.jsonl"), "--dry-run"])
        self.assertEqual(code, 0, out.getvalue())                                     # 계약 위반 없음
        self.assertIn("아는 이슈 키 1/2 (50%)", out.getvalue())
        self.assertIn("단서 없음 1/2 (50%)", out.getvalue())


class TestDoctorOnline(unittest.TestCase):
    def test_online_probe_rows(self):
        from unittest import mock
        from atlas import doctor
        from atlas.common import paths
        root = make_root('[[sources.jira]]\nname = "j"\nbase_url = "https://jira.corp.example"\n')
        try:
            configmod._load.cache_clear()
            cfg = configmod.load(root)
            with mock.patch.object(jira, "probe", return_value=[(True, "서버에 닿는다"), (False, "HTTP 401 → 인증 실패")]):
                rows = doctor.run(paths(str(root)), cfg, online=True)
            got = [(r["status"], r["what"]) for r in rows if r["what"] in ("jira:j 서버에 닿는다", "jira:j HTTP 401 → 인증 실패")]
            self.assertEqual(got, [("ok", "jira:j 서버에 닿는다"), ("✗", "jira:j HTTP 401 → 인증 실패")])
            self.assertFalse(any(r["what"].startswith("jira:j 서버") for r in doctor.run(paths(str(root)), cfg)))
        finally:
            shutil.rmtree(root, ignore_errors=True)
            configmod._load.cache_clear()


if __name__ == "__main__":
    unittest.main()
