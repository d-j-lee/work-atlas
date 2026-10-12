"""인스턴스 설정 — `<root>/atlas.toml` 이 있으면 읽고, 없는 값은 기본값을 쓴다.

기본값은 `atlas.example.toml` 과 같다. 설정 파일이 없어도 모든 명령이 돈다(인자로 소스를 넘기면 된다).
"""
from __future__ import annotations

import copy
import re
import tomllib
from functools import lru_cache
from pathlib import Path

DEFAULTS: dict = {
    # self: 나를 가리키는 식별자(이메일·계정 이름·Jira accountId). 역할 판정에만 쓰고 L0 에는 넣지 않는다.
    # bot_patterns 는 식별자를 공백으로 이은 문자열(이름이 맨 앞)에 건다. jenkins·hudson 은 사람 성(姓)과 겹쳐 이름 맨 앞·메일 주소만 본다
    "identity": {"self": [], "bot_patterns": [r"\[bot\]", r"\bbot\b", "dependabot", "renovate", r"^(jenkins|hudson)\b",
                                              r"\b(jenkins|hudson)@", "github-actions", "gitlab-ci"]},
    "keys": {
        # \b 를 쓰지 않는다 — 유니코드 정규식에서 한글은 단어 글자라 "ABC-123에서" 가 안 잡힌다.
        "issue": r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,9}-[0-9]{1,6}(?![0-9])",
        "projects": [],               # 알려진 프로젝트 키만 인정하려면 채운다 (예: ["PROJ", "OPS"])
        "exclude_prefixes": ["UTF", "SHA", "ISO", "RFC", "HTTP", "TLS", "SSL", "CVE", "JDK", "JSR", "PEP",
                             "AES", "MD", "IPV", "GCC", "ARM", "X86", "CP", "EUC", "KSC", "RS", "ES"],
        # 'patch' 는 뺐다 — "Applying X's patch from …" 처럼 결함과 무관한 쓰임이 많다(실측)
        "fix": r"\b(fix(e[sd])?|bug(fix)?|hotfix)\b|버그|결함|오류|장애|핫픽스",
        # fix 에 걸려도 결함 수정이 아닌 것 — 문서·서식·오타 정리. 'comment'·'whitespace' 같은 일반어는 실제 결함 제목에도 나와 넣지 않는다
        "fix_exclude": r"(?i)javadoc|\btypos?\b|\bformatting\b|checkstyle|spotbugs|\bdocs?\b|\bdocumentation\b|오타|주석 수정|문서|포맷",
    },
    "sources": {"git": [], "jira": [], "claude_code": []},
    "stitch": {
        "max_issue_keys": 3,          # 이보다 많은 키를 단 커밋은 조인하지 않는다(릴리스·일괄 커밋)
        "session_link_hours": 12,     # AI 세션이 끝나고 이 시간 안의 커밋을 경로 겹침으로 잇는다
        "session_wu_min_prompts": 3,  # 이슈·커밋에 안 이어진 세션이 독립 WU 가 되는 최소 프롬프트 수(편집이 있으면 1)
        "bug_types": ["Bug", "Defect", "버그", "결함", "장애"],
        "abandoned_resolutions": ["Won't Fix", "Won't Do", "Duplicate", "Invalid", "Incomplete", "Cannot Reproduce",
                                  "Not A Problem", "Not A Bug", "Abandoned", "Rejected", "취소", "반려", "중복"],
    },
    "policy": {"open_window_days": 30, "min_judged": 20,
               # 카탈로그 경고 문턱 (policy.md §7 이 이 키를 가리킨다)
               "orphan_ratio_max": 0.2, "key_coverage_min": 0.5, "stale_days": 8, "transcript_stale_days": 20},
    "mining": {"max_files_per_commit": 30, "min_support": 3, "min_confidence": 0.3,
               "half_life_days": 180, "history_generations": 6,
               "hotspot_rank": "fixes",      # fixes · fix_damp · changes · change_damp · churn (EVALUATION.md)
               # 거의 모든 변경에 따라 바뀌는 장부 파일 — 동반 변경·핫스팟·SZZ 근사에서 뺀다(넣으면 그것만 추천된다)
               "ignore_paths": ["**/CHANGELOG*", "**/CHANGES*", "**/changes.xml", "**/RELEASE-NOTES*", "**/NEWS*",
                                "**/HISTORY*", "**/*.lock", "**/package-lock.json", "**/go.sum", "**/pom.xml",
                                "**/build.gradle", "**/build.gradle.kts", "**/package.json", "**/version.txt",
                                ".github/**", ".gitlab-ci.yml", "**/Jenkinsfile"],
               # 테스트 파일 — 사후 귀속에서 유발자로 보지 않고(테스트가 운영 결함을 만들지 않는다), 실천 특징 '테스트 변경'에 쓴다
               "test_paths": ["**/test/**", "**/tests/**", "**/__tests__/**", "**/*Test.*", "**/*Tests.*",
                              "**/*_test.*", "**/test_*", "**/*.spec.*", "**/*.test.*"]},
    "context": {"max_lines": 40, "hook_min_confidence": 0.5},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@lru_cache(maxsize=8)
def _load(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return copy.deepcopy(DEFAULTS)
    with p.open("rb") as f:
        return _merge(DEFAULTS, tomllib.load(f))


def load(root: Path) -> dict:
    return copy.deepcopy(_load(str(Path(root) / "atlas.toml")))


class Patterns:
    """설정의 정규식을 한 번만 컴파일한다."""

    def __init__(self, cfg: dict):
        self.issue = re.compile(cfg["keys"]["issue"])
        self.projects = {p.upper() for p in cfg["keys"].get("projects", [])}
        self.exclude = {p.upper() for p in cfg["keys"].get("exclude_prefixes", [])}
        self.fix = re.compile(cfg["keys"]["fix"], re.IGNORECASE)
        self.fix_exclude = re.compile(cfg["keys"]["fix_exclude"]) if cfg["keys"].get("fix_exclude") else None
        self.bots = [re.compile(p, re.IGNORECASE) for p in cfg["identity"]["bot_patterns"]]
        self.self_ids = {str(e).strip().lower() for e in cfg["identity"].get("self", []) if str(e).strip()}

    def key_ok(self, key: str) -> bool:
        """지금 설정으로 인정되는 키인가. 빌드 때 L0 의 키를 다시 거른다 — 설정을 좁히면 재수집 없이 반영된다."""
        prefix = key.rsplit("-", 1)[0]
        return prefix not in self.exclude and (not self.projects or prefix in self.projects)

    def issue_keys(self, *texts: str) -> list[str]:
        """등장 순서를 지킨 중복 없는 이슈 키."""
        seen: dict[str, None] = {}
        for t in texts:
            for k in self.issue.findall(t or ""):
                if self.key_ok(k):
                    seen.setdefault(k, None)
        return list(seen)

    def is_fix(self, message: str) -> bool:
        """결함 수정 메시지인가 — fix 패턴에 걸리고, 제목 문단이 fix_exclude(문서·서식·오타)에 걸리지 않는다."""
        if not self.fix.search(message or ""):
            return False
        subject = (message or "").split("\n\n", 1)[0]
        return not (self.fix_exclude and self.fix_exclude.search(subject))

    def role_of(self, *identifiers: str | None) -> str:
        """식별자(이메일·이름·계정 id) → self · bot · unknown. 식별자 자체는 돌려주지도 저장하지도 않는다."""
        ids = [str(i).strip() for i in identifiers if i]
        if any(i.lower() in self.self_ids for i in ids):
            return "self"
        who = " ".join(ids)
        if who and any(b.search(who) for b in self.bots):
            return "bot"
        return "unknown"
