"""기준 레시피 단위 테스트 — 합성 데이터만 쓴다(모든 id·주소는 가상).

실행: python -m unittest discover -s tests -v
템플릿 코드북은 원형·어휘가 비어 있으므로, 테스트는 임시 루트에 가상 원형·어휘를 넣은 코드북 사본을 만든다.
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "method" / "recipes"))

import gen_annotation_schema  # noqa: E402
import record_event  # noqa: E402
import render_wu  # noqa: E402
import score  # noqa: E402
import similar  # noqa: E402
import validate  # noqa: E402
from _common import (  # noqa: E402
    Codebook, append_jsonl, check_observation, observation_validator, paths, read_jsonl, read_observations,
    sha256_text,
)

TS = "2026-10-01T10:00:00+09:00"


def make_root(archetypes: bool = True) -> Path:
    root = Path(tempfile.mkdtemp(prefix="atlas-test-"))
    (root / "method").mkdir()
    shutil.copy(REPO / "method" / "observation.schema.json", root / "method")
    cb = yaml.safe_load((REPO / "method" / "codebook.yaml").read_text(encoding="utf-8"))
    cb["version"] = 1
    if archetypes:
        cb["archetypes"] = [{"id": "sample-ops-fix", "label": "가상 원형 A"}, {"id": "sample-feature", "label": "가상 원형 B"}]
        cb["vocabularies"] = {
            "pitfalls": [{"id": "sample-pitfall"}],
            "subtasks": [{"id": "sample-subtask"}],
            "moves": [{"id": "sample-move"}, {"id": "sample-move-old", "status": "dormant"}],
        }
    (root / "method" / "codebook.yaml").write_text(yaml.safe_dump(cb, allow_unicode=True), encoding="utf-8")
    return root


def obs(obs_id: str, system: str, kind: str, ts: str = TS, objects=None, keys=None, labels=None, **extra) -> dict:
    o = {
        "obs_id": obs_id,
        "source": {"system": system, "instance": "test",
                   "collector_kind": "human" if system in ("atlas", "self_report") else "api"},
        "kind": kind, "ts": ts, "actor_role": "self",
        "objects": objects or [], "keys": keys or {}, "metrics": {},
        "partial": False, "collector": "test@0", "collected_at": ts,
    }
    if labels:
        o["labels"] = labels
    o.update(extra)
    return o


class Base(unittest.TestCase):
    archetypes = True

    def setUp(self):
        self.root = make_root(self.archetypes)
        self.p = paths(self.root)
        self.cb = Codebook.load(self.p)
        self.validator = observation_validator(self.p)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def problems(self, o: dict) -> list[str]:
        return check_observation(o, self.validator, self.cb)


# ------------------------------------------------------------------ 관측 계약

class ObservationContractTest(Base):
    def test_valid_source_observation(self):
        o = obs("issue:T-1#created", "issue", "issue.created", objects=[{"type": "issue", "id": "T-1"}],
                keys={"issue_keys": ["T-1"]}, labels={"component": ["billing"]})
        self.assertEqual(self.problems(o), [])

    def test_payload_only_for_human_sources(self):
        self.assertTrue(self.problems(obs("vcs:abc", "vcs", "vcs.commit", payload={"note": "해석"})))

    def test_human_source_requires_payload(self):
        self.assertTrue(self.problems(obs("atlas:x", "atlas", "atlas.triage")))

    def test_human_source_requires_human_collector(self):
        o = obs("atlas:x", "atlas", "atlas.triage", payload={"pred": {}})
        o["source"]["collector_kind"] = "api"
        self.assertTrue(any("collector_kind" in p for p in self.problems(o)))

    def test_kind_prefix_must_match_system(self):
        self.assertTrue(any("접두" in p for p in self.problems(obs("vcs:abc", "vcs", "issue.created"))))

    def test_metrics_reject_strings(self):
        o = obs("vcs:abc", "vcs", "vcs.commit")
        o["metrics"] = {"status": "done"}
        self.assertTrue(self.problems(o))

    def test_actor_role_rejects_personal_identifier(self):
        o = obs("vcs:abc", "vcs", "vcs.commit")
        o["actor_role"] = "kim.minsu"
        self.assertTrue(any("actor_role" in p for p in self.problems(o)))

    def test_timezone_is_required(self):
        self.assertTrue(self.problems(obs("vcs:abc", "vcs", "vcs.commit", ts="2026-10-01T10:00:00")))

    def test_impossible_date_is_rejected(self):
        self.assertTrue(any("실제 시각" in p for p in self.problems(obs("vcs:a", "vcs", "vcs.commit", ts="2026-02-30T10:00:00+09:00"))))

    def test_correction_needs_target(self):
        self.assertTrue(self.problems(obs("vcs:c", "vcs", "vcs.correction")))
        self.assertEqual(self.problems(obs("vcs:c", "vcs", "vcs.correction", objects=[{"type": "obs", "id": "vcs:a"}])), [])


class ReadObservationsTest(Base):
    def test_first_wins_and_corrections_apply(self):
        d = self.p.observations
        append_jsonl(d / "2026-09.jsonl", obs("a", "vcs", "vcs.commit", metrics={"n": 1}))
        append_jsonl(d / "2026-10.jsonl", obs("a", "vcs", "vcs.commit", metrics={"n": 2}))   # 재수집 중복
        append_jsonl(d / "2026-10.jsonl", obs("b", "vcs", "vcs.commit"))
        append_jsonl(d / "2026-10.jsonl", obs("fix", "vcs", "vcs.correction", objects=[{"type": "obs", "id": "b"}]))
        got = {o["obs_id"]: o for o in read_observations(d)}
        self.assertEqual(set(got), {"a"}, "정정된 b 와 정정 사건 자체는 빠진다")
        self.assertEqual(got["a"]["metrics"]["n"], 1, "같은 obs_id 는 처음 것을 쓴다")


# ------------------------------------------------------------------ 기록

class RecordEventTest(Base):
    def triage(self, **over):
        args = dict(anchor="issue:T-1", archetype="sample-ops-fix", conf="medium", scale="small",
                    pitfalls=["sample-pitfall"], moves=["sample-move"], generation=None, ts=TS)
        args.update(over)
        return record_event.build_triage(self.cb, **args)

    def test_triage_is_recorded_and_valid(self):
        target = record_event.record(self.triage(), self.p, self.cb)
        lines = list(read_jsonl(target))
        self.assertEqual(len(lines), 1)
        self.assertEqual(self.problems(lines[0]), [])
        self.assertEqual(lines[0]["payload"]["pred"]["archetype"], "sample-ops-fix")
        self.assertEqual(target.name, "2026-10.jsonl")
        self.assertNotIn(b"\r", target.read_bytes())

    def test_record_validates_before_writing(self):
        e = self.triage()
        e["actor_role"] = "kim.minsu"
        with self.assertRaises(record_event.RecordError):
            record_event.record(e, self.p, self.cb)
        self.assertFalse(self.p.observations.exists())

    def test_obs_id_is_deterministic(self):
        self.assertEqual(self.triage()["obs_id"], self.triage()["obs_id"])
        self.assertNotEqual(self.triage()["obs_id"], self.triage(scale="large")["obs_id"])

    def test_rejects_ids_outside_codebook(self):
        for bad in (dict(archetype="made-up"), dict(conf="0.9"), dict(scale="huge"),
                    dict(pitfalls=["made-up"]), dict(moves=["sample-move-old"])):
            with self.subTest(bad=bad), self.assertRaises(record_event.RecordError):
                self.triage(**bad)

    def test_residual_is_allowed(self):
        self.assertEqual(self.triage(archetype="residual", pitfalls=["residual"])["payload"]["pred"]["archetype"],
                         "residual")

    def test_anchor_rejects_links_and_malformed(self):
        with self.assertRaisesRegex(record_event.RecordError, "링크"):
            self.triage(anchor="https://tracker.example/browse/T-1")
        for bad in ("T-1", "issue:", "issue: T-1", "issue://T-1"):
            with self.subTest(bad=bad), self.assertRaises(record_event.RecordError):
                self.triage(anchor=bad)

    def test_feedback_needs_prior_triage(self):
        with self.assertRaises(record_event.RecordError):
            record_event.record(record_event.build_feedback("issue:T-1", "yes", "helpful", TS), self.p, self.cb)
        record_event.record(self.triage(), self.p, self.cb)
        record_event.record(record_event.build_feedback("issue:T-1", "yes", "helpful", TS), self.p, self.cb)

    def test_append_only(self):
        target = record_event.record(self.triage(), self.p, self.cb)
        first = target.read_text(encoding="utf-8")
        record_event.record(record_event.build_feedback("issue:T-1", "yes", "helpful", TS), self.p, self.cb)
        after = target.read_text(encoding="utf-8")
        self.assertTrue(after.startswith(first))
        self.assertEqual(len(after.splitlines()), 2)

    def test_self_report(self):
        e = record_event.build_self_report("  운영 요청으로 로그 조회  ", TS)
        self.assertEqual(e["payload"]["text"], "운영 요청으로 로그 조회")
        self.assertEqual(self.problems(e), [])
        with self.assertRaises(record_event.RecordError):
            record_event.build_self_report("   ", TS)

    def test_cli_dry_run_applies_same_preflight(self):
        rc = record_event.main(["--root", str(self.root), "--dry-run", "feedback", "--anchor", "issue:T-1",
                                "--archetype-ok", "yes", "--verdict", "helpful"])
        self.assertEqual(rc, 2, "트리아지 없는 판정은 dry-run 에서도 거부해야 한다")

    def test_cli_dry_run_writes_nothing(self):
        self.assertEqual(record_event.main(["--root", str(self.root), "--dry-run", "self-report", "--text", "가상 작업"]), 0)
        self.assertFalse(self.p.observations.exists())

    def test_cli_rejects_with_exit_2(self):
        rc = record_event.main(["--root", str(self.root), "triage", "--anchor", "issue:T-1",
                                "--archetype", "made-up", "--conf", "low", "--scale", "small"])
        self.assertEqual(rc, 2)
        self.assertFalse(self.p.observations.exists())


class RecordEventTemplateStateTest(Base):
    archetypes = False

    def test_explains_missing_archetypes(self):
        with self.assertRaisesRegex(record_event.RecordError, "A3"):
            record_event.build_triage(self.cb, "issue:T-1", "anything", "low", "small", [], [], None, TS)
        e = record_event.build_triage(self.cb, "issue:T-1", "residual", "low", "small", [], [], None, TS)
        self.assertEqual(self.problems(e), [])


# ------------------------------------------------------------------ 사후 검증

def unit_with(annotation: dict, members=("o1", "o2"), signals=(), end=TS, settled=True) -> dict:
    return {
        "wu_id": "wu:issue:T-1", "anchors": ["issue:T-1"],
        "span": {"start": TS, "end": end, "settled": settled},
        "members": [{"obs_id": m, "link": "key", "confidence": "high"} for m in members],
        "stats": {"deterministic_signals": list(signals)},
        "annotation": annotation,
    }


def claim(value, confidence="medium", evidence=("o1",)):
    return {"value": value, "confidence": confidence, "evidence": list(evidence), "candidate_note": ""}


class ValidateTest(Base):
    def annotation(self) -> dict:
        return {
            "wu_id": "wu:issue:T-1", "codebook_version": 1,
            "title": {"value": "가상 제목", "confidence": "high", "evidence": []},
            "summary": {"value": "가상 요약", "confidence": "medium", "evidence": ["o1"]},
            "facets": {
                "origin": claim("planned_request", "high", ["o1"]),        # R3: 근거 1개인데 high
                "nature": claim("fix", "medium", ["x9"]),                  # R1 + R2 → 위반 값 1개
                "risk": claim(["money"], "high", ["o1", "o2"]),            # 통과
                "scale": claim("huge"),                                     # R4 값
                "failure_class": claim("unknown", "low", []),               # unknown 은 근거 없어도 통과
                "outcome": claim("clean", "high", ["o1", "o2"]),            # R4 결정론 계산 패싯
                "domain": claim(["svc-a"], "medium", ["o1"]),               # R4 트랙 B 패싯
            },
            "archetype": {"value": "sample-ops-fix", "fit": "strong", "evidence": [], "candidate_note": ""},  # R2
            "hidden_subtasks": [],
            "pitfalls": [
                {"id": "sample-pitfall", "note": "", "confidence": "high", "evidence": ["o1", "o1"]},  # 중복 → R3
                {"id": "residual", "note": "새 후보", "confidence": "low", "evidence": []},            # R2 → 뺀다
            ],
            "moves": [{"id": "made-up", "note": "", "confidence": "low", "evidence": ["o1"]}],     # R4 → 뺀다
            "open_questions": [],
        }

    def test_rules(self):
        unit = unit_with(self.annotation())
        before = copy.deepcopy(unit)
        out = validate.validate_unit(unit, self.cb)
        self.assertEqual(unit, before, "입력을 바꾸면 안 된다")
        a = out["annotation"]
        f = a["facets"]
        self.assertEqual(f["origin"]["confidence"], "medium")
        self.assertEqual((f["nature"]["value"], f["nature"]["evidence"]), ("unknown", []))
        self.assertEqual((f["risk"]["value"], f["risk"]["confidence"]), (["money"], "high"))
        self.assertEqual(f["scale"]["value"], "unknown")
        self.assertEqual(f["failure_class"]["value"], "unknown")
        self.assertNotIn("outcome", f, "결정론 계산 패싯은 LLM 값을 받지 않는다")
        self.assertNotIn("domain", f, "트랙 A 에서는 domain 을 주석하지 않는다")
        self.assertEqual(a["archetype"]["value"], "unknown")
        self.assertEqual([(i["id"], i["confidence"], i["evidence"]) for i in a["pitfalls"]],
                         [("sample-pitfall", "medium", ["o1"])], "중복 근거는 하나로 센다")
        self.assertEqual(a["moves"], [])
        self.assertEqual((a["title"]["value"], a["title"]["confidence"]), ("가상 제목", "low"), "서술은 지우지 않는다")

    def test_signal_does_not_exempt_r3(self):
        out = validate.validate_unit(unit_with(self.annotation(), signals=["reopened"]), self.cb)
        self.assertEqual(out["annotation"]["facets"]["origin"]["confidence"], "medium")

    def test_report_counts_values_not_rules(self):
        only_nature = {"codebook_version": 1, "facets": {"nature": claim("fix", "medium", ["x9"])}}
        _, report = validate.run([unit_with(only_nature)], self.cb)
        self.assertEqual((report["values_checked"], report["values_violated"]), (1, 1))
        self.assertEqual(report["violation_rate"], 1.0)
        self.assertEqual(report["by_rule"], {"R1": 1, "R2": 1})

    def test_report(self):
        _, report = validate.run([unit_with(self.annotation()), unit_with({}),
                                  unit_with({"codebook_version": 0, "facets": {}})], self.cb)
        self.assertEqual(report["units"], 3)
        self.assertLessEqual(report["violation_rate"], 1)
        self.assertEqual(report["stale_codebook_units"], 1)


# ------------------------------------------------------------------ 입력 문서

class RenderTest(Base):
    def setUp(self):
        super().setUp()
        self.obs = {
            "o1": obs("o1", "issue", "issue.created", "2026-10-01T09:00:00+09:00",
                      [{"type": "issue", "id": "T-1"}], {"issue_keys": ["T-1"]}, {"component": ["billing"]}),
            "o2": obs("o2", "vcs", "vcs.commit", "2026-10-01T01:30:00Z",   # = 10:30 KST, o1 다음
                      [{"type": "commit", "id": "abc"}], {"paths": ["src/a.py"], "issue_keys": ["T-1"]}),
            "a1": obs("a1", "atlas", "atlas.triage", TS, [{"type": "issue", "id": "T-1"}], payload={"pred": {}}),
        }
        self.unit = unit_with({"archetype": {"value": "sample-ops-fix"}}, members=("o2", "o1", "a1", "gone"))
        self.excerpts = {"o1": "결제 오류\n연락 010-1234-5678, kim@corp.example, 900101-1234567, token=abcd1234 끝",
                         "o2": "x" * 1000}

    def test_deterministic_and_clean(self):
        doc = render_wu.render(self.unit, self.obs, self.excerpts, max_excerpt=50)
        self.assertEqual(sha256_text(doc), sha256_text(render_wu.render(self.unit, self.obs, self.excerpts, max_excerpt=50)))
        self.assertNotIn("atlas", doc.split("## 관측")[1], "atlas 사건은 입력 문서에 들어가면 안 된다")
        self.assertNotIn("sample-ops-fix", doc, "주석이 입력 문서에 새면 안 된다")
        for secret in ("010-1234-5678", "kim@corp.example", "900101-1234567", "abcd1234"):
            self.assertNotIn(secret, doc)
        self.assertIn("…(잘림)", doc)
        self.assertIn("누락 관측: 1건", doc)
        self.assertLess(doc.index("· o1 ·"), doc.index("· o2 ·"), "시간대가 달라도 실제 시각순이어야 한다")

    def test_doc_cap(self):
        self.assertIn("문서 상한 도달", render_wu.render(self.unit, self.obs, self.excerpts, max_doc_bytes=300))

    def test_mask_table(self):
        leaks = {
            "DB_PASSWORD=hunter2": "hunter2", "client_secret=abc123": "abc123",
            "GITHUB_TOKEN=ghp_" + "a" * 36: "ghp_", '{"password": "pw1234"}': "pw1234",
            "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG": "wJalrXUtnFEMI",
            "Authorization: Basic dXNlcjpwYXNzd29yZA==": "dXNlcjpwYXNz",
            "token: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig12345": "eyJhbGci",
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefgh": "eyJhbGci",
            "slack xoxb-1234567890-abcdefghij": "xoxb-",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA": "MIIEp",
            "010 1234 5678": "1234 5678", "010.1234.5678": "1234.5678", "+82-10-1234-5678": "1234-5678",
            "9001011234567": "9001011234567", "900101-5234567": "5234567",
            "https://x.example/u?mail=ab@cd.example": "ab@cd",
            'secret: "two words"': "words", "PASSWORD := x1y2": "x1y2", "sk-" + "a" * 24: "aaaaaaaa",
            "sk_live_" + "b" * 24: "bbbbbbbb", "Cookie: session=abc123; theme=dark": "abc123",
            "02-123-4567": "123-4567", "031.123.4567": "123.4567",
        }
        for text, secret in leaks.items():
            with self.subTest(text=text):
                self.assertNotIn(secret, render_wu.mask(text))
        for text in ("icon@2x.png", "order 20261009-0001", "version 1.2.3", "key: 진행 상태"):
            with self.subTest(keep=text):
                self.assertEqual(render_wu.mask(text), text)

    def test_mask_is_linear_on_long_lines(self):
        import time
        for text in ("a" * 100_000, ("password_" * 20_000)[:100_000], ("Ab9+/" * 20_000)[:100_000],
                     ("password_" * 2_000)[:20_000] + "=x"):
            t = time.perf_counter()
            render_wu.mask(text)
            self.assertLess(time.perf_counter() - t, 2.0, f"긴 한 줄에서 느리다: {text[:12]}…")

    def test_render_masks_after_truncation(self):
        huge = {"o1": "x" * 1_000_000 + " token=abcd1234"}
        import time
        t = time.perf_counter()
        doc = render_wu.render(self.unit, self.obs, huge, max_excerpt=500)
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertIn("…(잘림)", doc)

    def test_cli_unique_names_and_lf(self):
        d = self.p.observations
        for o in self.obs.values():
            append_jsonl(d / "2026-10.jsonl", o)
        units = [dict(self.unit, wu_id="wu:chat:운영-1"), dict(self.unit, wu_id="wu:chat:배포-1")]
        self.p.stitched.parent.mkdir(parents=True)
        self.p.stitched.write_text("".join(json.dumps(u, ensure_ascii=False) + "\n" for u in units), encoding="utf-8")
        self.assertEqual(render_wu.main(["--root", str(self.root)]), 0)
        index = json.loads((self.p.render / "index.json").read_text(encoding="utf-8"))
        files = {v["file"] for v in index.values()}
        self.assertEqual(len(files), 2, "한글이 지워져도 파일 이름이 겹치면 안 된다")
        for v in index.values():
            raw = (self.p.render / v["file"]).read_bytes()
            self.assertNotIn(b"\r", raw)
            self.assertEqual(v["input_hash"], sha256_text(raw.decode("utf-8")), "해시는 파일 바이트와 같아야 한다")


# ------------------------------------------------------------------ 채점

class ScoreTest(Base):
    def units(self):
        return [
            {"wu_id": "wu:issue:T-1", "anchors": ["issue:T-1"], "members": [{"obs_id": "o1"}],
             "span": {"end": TS, "settled": True},
             "annotation": {"archetype": {"value": "sample-ops-fix"}, "facets": {"scale": {"value": "small"}},
                            "pitfalls": [{"id": "sample-pitfall"}], "moves": [{"id": "sample-move"}]}},
            {"wu_id": "wu:issue:T-2", "anchors": ["issue:T-2"], "members": [{"obs_id": "c1"}],
             "span": {"end": None, "settled": False}, "annotation": {"archetype": {"value": "sample-feature"}}},
            {"wu_id": "wu:issue:T-4", "anchors": ["issue:T-4"], "members": [],
             "span": {"end": TS, "settled": False},
             "annotation": {"archetype": {"value": "sample-feature"}, "pitfalls": [{"id": "sample-pitfall"}]}},
        ]

    def tri(self, anchor, arch, conf="medium", scale="small", pit=(), mv=(), ts=TS):
        return record_event.build_triage(self.cb, anchor, arch, conf, scale, list(pit), list(mv), None, ts)

    def fb(self, anchor, ok, verdict="helpful", ts=TS):
        return record_event.build_feedback(anchor, ok, verdict, ts)

    def test_score(self):
        observations = [
            obs("o1", "issue", "issue.created", objects=[{"type": "issue", "id": "T-1"}]),
            obs("c1", "chat", "chat.message", objects=[{"type": "chat", "id": "C-9"}]),
            self.tri("issue:T-1", "sample-ops-fix", "high", "small", ["sample-pitfall"], ["sample-move"]),
            self.fb("issue:T-1", "yes", ts="2026-10-01T01:30:00Z"),                       # 10:30 KST, 트리아지 뒤
            self.tri("chat:C-9", "sample-ops-fix", "medium", "large"),                    # WU T-2 로 연결
            self.fb("chat:C-9", "no", "not_helpful", ts="2026-10-02T09:00:00+09:00"),
            self.tri("issue:T-3", "sample-feature", ts="2026-10-03T09:00:00+09:00"),
            self.fb("issue:T-3", "yes", ts="2026-10-02T09:00:00+09:00"),                  # 트리아지 전 → 무시
            self.tri("issue:T-4", "sample-feature", pit=["sample-pitfall"]),               # 끝났지만 미확정
            self.fb("issue:T-4", "unsure", ts=TS),                                         # 같은 시각, unsure → 제외
            self.tri("issue:T-5", "residual"),
            self.fb("issue:T-5", "yes"),                                                   # 보류 예측 → 제외
        ]
        r = score.score(observations, self.units(), min_judged=2)
        self.assertEqual(r["triages"], 5)
        self.assertEqual((r["archetype_human"]["hit"], r["archetype_human"]["n"]), (1, 2))
        self.assertEqual(r["abstained"], 1)
        self.assertIsNone(r["calibration_inverted"], "칸이 작으면 역전을 판정하지 않는다")
        self.assertEqual(r["consistency"]["archetype"]["n"], 2, "끝난 WU(T-1, T-4)만 잠정 채점")
        self.assertEqual(r["consistency"]["pitfalls"]["n"], 1, "함정은 확정 WU(T-1)만")
        self.assertEqual((r["consistency"]["moves"]["hit"], r["consistency"]["moves"]["n"]), (1, 1))
        self.assertEqual((r["helpful"]["hit"], r["helpful"]["n"]), (3, 4))
        self.assertTrue(r["ready_to_judge"])
        self.assertIn("표본 부족", score.render_text(score.score(observations, self.units(), min_judged=20)))

    def test_feedback_at_same_instant_counts(self):
        r = score.score([self.tri("issue:T-9", "sample-feature"), self.fb("issue:T-9", "yes", ts=TS)], [])
        self.assertEqual(r["archetype_human"]["n"], 1)

    def test_calibration_inversion_needs_large_bins(self):
        evs = []
        for i in range(10):
            evs += [self.tri(f"issue:H{i}", "sample-feature", "high"), self.fb(f"issue:H{i}", "yes" if i < 5 else "no")]
            evs += [self.tri(f"issue:M{i}", "sample-feature", "medium"), self.fb(f"issue:M{i}", "yes" if i < 8 else "no")]
        r = score.score(evs, [])
        self.assertTrue(r["calibration_inverted"])
        self.assertIn("역전", score.render_text(r))


# ------------------------------------------------------------------ 유사 사례

class SimilarTest(Base):
    def test_rank(self):
        o = {
            "pa": obs("pa", "vcs", "vcs.commit", keys={"paths": ["src/x.py"]}),
            "pb": obs("pb", "vcs", "vcs.commit", keys={"paths": ["src/a.py", "src/b.py"]}),
            "pc": obs("pc", "vcs", "vcs.commit", keys={"paths": ["src/a.py"]}),
        }

        def u(wu, arch, member, end, settled=True):
            return {"wu_id": wu, "anchors": [wu], "members": [{"obs_id": member}],
                    "span": {"end": end, "settled": settled}, "annotation": {"archetype": {"value": arch}}}

        units = [
            u("A", "sample-ops-fix", "pa", "2026-01-01T00:00:00+09:00"),
            u("B", "sample-feature", "pb", "2026-09-01T00:00:00+09:00"),
            u("M", "sample-ops-fix", "pc", "2026-05-01T00:00:00+09:00"),
            u("Y", "sample-ops-fix", "pc", "2026-03-01T00:00:00+09:00"),     # M 과 동점 → id 순서와 반대로 최근(M)이 먼저
            u("D", "sample-ops-fix", "pc", "2026-09-30T00:00:00+09:00", settled=False),
            u("E", "sample-feature", "pa", "2026-09-30T00:00:00+09:00"),     # 원형도 단서도 안 겹침 → 제외
        ]
        query = {"archetype": "sample-ops-fix", "path": ["src/a.py", "src/b.py"]}
        self.assertEqual([r["wu_id"] for r in similar.rank(query, units, o, k=5)], ["M", "Y", "A", "B"])

    def test_cli_rejects_unknown_archetype(self):
        self.assertEqual(similar.main(["--root", str(self.root), "--archetype", "made-up"]), 2)


# ------------------------------------------------------------------ 생성물·계약

class GeneratedSchemaTest(unittest.TestCase):
    def test_annotation_schema_in_sync(self):
        self.assertTrue(gen_annotation_schema.check(), "codebook.yaml 을 바꿨으면 gen_annotation_schema.py 를 돌려라")

    def test_observation_schema_is_valid(self):
        import jsonschema
        schema = json.loads((REPO / "method" / "observation.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)

    def test_vocab_items_cannot_be_unknown(self):
        s = json.loads((REPO / "method" / "annotation.schema.json").read_text(encoding="utf-8"))
        self.assertNotIn("unknown", s["properties"]["pitfalls"]["items"]["properties"]["id"]["enum"])


if __name__ == "__main__":
    unittest.main()
