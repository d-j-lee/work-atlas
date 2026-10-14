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
                                              r"\b(jenkins|hudson)@", "github-actions", "gitlab-ci"],
                 # 역할 → 식별자 패턴(정규식, 대소문자 무시). 수집할 때 행위자를 그 역할로 남긴다(식별자는 남기지 않는다).
                 # 역할 id 는 codebook roles 의 것만. 팀 메일·그룹 계정·도메인 같은 묶음 패턴을 권한다 — 업무 카테고리의
                 # 순서 그림·접점(누가 어느 단계에 처음 나오나)이 이것으로 살아난다. 수집 시점에 적용된다(지난 관측은 그대로)
                 "roles": {}},
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
    "sources": {"git": [], "jira": [], "claude_code": [], "workspace": []},
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
               "orphan_ratio_max": 0.2, "key_coverage_min": 0.5, "stale_days": 8, "transcript_stale_days": 20,
               "stale_work_days": 30, "uncategorized_max": 0.3,
               # LLM 작업 경고 — 근거 검증 통과율(이번 달 호출 5건 이상) · 사람 판정 중 쓸모 있음(판정 10건 이상) · 월 예산 비율
               "llm_accept_min": 0.7, "llm_useful_min": 0.6, "llm_budget_warn": 0.8},
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
                              "**/*_test.*", "**/test_*", "**/*.spec.*", "**/*.test.*"],
               # 업무 카테고리 (mine/category.py · method/categories.yaml) — 의미는 CHANNELS.md
               "category_min_n": 5,          # 흐름·순서·변형을 낼 최소 작업단위 수. 영역 × 국면 칸도 이보다 작으면 만들지 않는다
               "flow_min_share": 0.05,       # 흐름 그림에 남길 활동·이음의 최소 비율 (그 카테고리 흐름 중)
               "category_recent_days": 730,  # 흐름은 최근 이 기간에 끝난 일로 (0 이면 끝난 일 전체)
               "category_basis_min": 30},    # 그 기간에 끝난 일이 이보다 적으면 가장 최근에 끝난 이만큼까지 넓힌다
    "context": {"max_lines": 40, "hook_min_confidence": 0.5},
    # 보존 기한 (atlas/retention.py — atlas harvest 가 빌드 전에 집행). 0 이면 지우지 않는다. 결정은 policy.md §2 '캐시 보존'
    "retention": {"excerpt_days": 0, "excerpt_days_by_system": {}, "llm_cache_days": 0, "report_days": 0},
    # 사용 중 효용 (atlas/usage.py) — 훅·맥락의 제안을 data/usage/ 에 남겨 반영률을 잰다. holdout 은 알리지 않고 기록만 할 비율(0~0.5)
    "usage": {"record": True, "holdout": 0.0, "min_sessions": 20},
    # LLM 작업 (atlas/llm/). 기본은 아무것도 보내지 않는다 — 사내에서 승인한 백엔드만 켠다(policy.md §1). 예산의 결정은 §4
    "llm": {"backend": "none",          # none · claude · command
            "model": "",                # 비우면 백엔드 기본
            "bare": True,               # claude: --bare (밀폐 — ANTHROPIC_API_KEY 또는 apiKeyHelper 필요)
            "claude_path": "",          # claude 실행 파일 (비우면 PATH)
            "command": [],              # command: 요청 JSON 을 표준 입력으로 받는 사내 게이트웨이 명령
            "timeout_s": 300,
            "max_usd_per_call": 0.5, "max_usd_per_run": 5.0, "max_usd_per_month": 50.0,
            "limit_per_run": 20,        # 한 번에 새로 부를 대상 수 상한 (캐시 적중은 세지 않는다)
            "auto": []},                # atlas build 가 이어서 돌릴 작업 (예: ["category_card"]) — 백엔드가 none 이면 건너뛴다
}


class ConfigError(ValueError):
    """설정이 틀렸다 — 명령은 이 메시지를 보이고 멈춘다(조용히 다른 뜻으로 돌지 않게)."""


def validate(cfg: dict, root: Path | None = None) -> list[str]:
    """기계가 읽기 전에 막아야 하는 설정 오류 — 정규식 문법, 코드북에 없는 역할 id(L0 에 사람 이름이 들어가는 길)."""
    out = []
    roles = (cfg.get("identity") or {}).get("roles") or {}
    if not isinstance(roles, dict):
        return ["identity.roles 는 '역할 id = [패턴…]' 표여야 한다"]
    allowed = None
    if root is not None and (Path(root) / "method" / "codebook.yaml").exists():
        import yaml
        cb = yaml.safe_load((Path(root) / "method" / "codebook.yaml").read_text(encoding="utf-8")) or {}
        allowed = {v["id"] for f in cb.get("facets", []) if f.get("id") == "roles" for v in f.get("values", [])}
    for role, pats in roles.items():
        if allowed is not None and role not in allowed:
            out.append(f"identity.roles 의 '{role}' 는 codebook roles 의 id 가 아니다 (허용: {', '.join(sorted(allowed))})")
        for pat in ([pats] if isinstance(pats, str) else pats if isinstance(pats, list) else [None]):
            if not isinstance(pat, str):
                out.append(f"identity.roles.{role} 의 패턴은 정규식 문자열이어야 한다 ({pat!r})")
                continue
            try:
                re.compile(pat)
            except re.error as e:
                out.append(f"identity.roles.{role} 의 패턴 {pat!r} 오류: {e}")
    for pat in (cfg.get("identity") or {}).get("bot_patterns", []):
        try:
            re.compile(pat)
        except re.error as e:
            out.append(f"identity.bot_patterns 의 {pat!r} 오류: {e}")
    return out


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
        cfg = _merge(DEFAULTS, tomllib.load(f))
    bad = validate(cfg, p.parent)
    if bad:
        raise ConfigError(f"{p}: " + "; ".join(bad))
    return cfg


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
        # 적은 순서대로 본다(먼저 맞는 역할) — 좁은 패턴을 위에 둔다
        self.roles = [(role, re.compile(pat, re.IGNORECASE)) for role, pats in (cfg["identity"].get("roles") or {}).items()
                      for pat in ([pats] if isinstance(pats, str) else pats)]
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
        """식별자(이메일·이름·계정 id) → self · bot · 설정한 역할 · unknown. 식별자 자체는 돌려주지도 저장하지도 않는다."""
        ids = [str(i).strip() for i in identifiers if i]
        if any(i.lower() in self.self_ids for i in ids):
            return "self"
        who = " ".join(ids)
        if who and any(b.search(who) for b in self.bots):
            return "bot"
        for role, rx in self.roles:
            if who and rx.search(who):
                return role
        return "unknown"
