"""git 수집기 — 커밋 하나가 관측 하나 (vcs.commit · vcs.merge).

결정론으로 뽑는 것:
  keys     issue_keys(메시지·브랜치) · paths(바뀐 파일, 이름 바뀐 경우 새 이름) · renames("옛\t새")
           · mr_ids("<저장소>!<번호>": 'Merge pull request #N' · '(#N)' 스쿼시 · 'See merge request …!N')
           · branches(머지한 브랜치의 해시 키 — 이름에 계정이 들어가는 관례가 흔하다. keep_branch_names=true 면 이름)
           · reverts(되돌린 커밋) · commits(머지가 들여온 브랜치 커밋)
  동기화 머지(메인라인을 들여오는 머지 — "Merge branch 'develop' into feature/X")는 브랜치 커밋을 들이지 않는다.
  들이면 메인라인의 남의 커밋이 그 이슈의 작업단위로 들어간다. 판정은 두 겹이다(is_sync_merge · collect):
    메시지 — 들여온 브랜치가 메인라인이다. GitHub PR 은 받는 쪽이 메시지에 없으니 같은 저장소(원격 소유자)일 때만.
             커밋·태그를 브랜치(메인라인 아님)로 들인 머지도 그 브랜치를 최신으로 맞추는 것이다(MR 이 아니면).
    구조 — 둘째 부모가 수집하는 ref(기본: 원격 기본 브랜치)의 첫 부모 사슬 위에 있다(형식과 무관하게 메인라인 커밋을 들인다).
  metrics  files · insertions · deletions · binary_files · parents · is_merge · sync_merge · is_revert · is_fix · author_lag_h
  역할     작성자 → self · bot · unknown (이메일·이름은 저장하지 않는다)
  발췌     커밋 메시지 (Signed-off-by 같은 이름 트레일러를 빼고, 가린 뒤 자름)
ts 는 커미터 시각(저장소에 들어온 때)이다 — 시간 분할 평가와 사후 귀속의 순서가 이 시각을 따른다.
경로가 max_paths 보다 많으면 앞부분만 남기고 partial=true (files 는 전체 수).
ref 기본값은 원격의 기본 브랜치(origin/HEAD)다 — 지금 체크아웃한 작업 브랜치를 모으지 않게. 없으면 HEAD.
증분: 지난번 끝 커밋이 지금 ref 의 조상이면 그 뒤만, 아니면(리베이스·강제 푸시) since 부터 다시 — 중복은 저장소가 건너뛴다.
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
from pathlib import Path

from . import CollectError, Result, branch_key, excerpt, is_mainline, normalize_branch, strip_trailers
from ..common import NO_WINDOW, now_iso

COLLECTOR = "git@1"
RS, US = "\x1e", "\x1f"
FORMAT = f"{RS}%H{US}%P{US}%an{US}%ae{US}%aI{US}%cI{US}%B{US}"
MERGE_LOCAL = re.compile(r"^Merge (remote-tracking )?branch '([^']+)'(?: of \S+)?(?: into '?([^'\s]+)'?)?")
MERGE_PR = re.compile(r"^Merge pull request #\d+ from (?:([^/\s]+)/)?(\S+)")              # GitHub: 소유자/브랜치
MERGE_BBS = re.compile(r"^Merge pull request #\d+ in \S+ from (\S+) to (\S+)")            # Bitbucket Server
MERGE_BB = re.compile(r"^Merged in (\S+)(?: \(pull request #\d+\))?(?: into (\S+))?")      # Bitbucket Cloud
MERGE_REF = re.compile(r"^Merge (?:commit|tag) '[^']+'(?: of \S+)?(?: into '?([^'\s]+)'?)?")  # 들인 것이 브랜치가 아니다
REMOTE_URL = re.compile(r"^(?:[a-z][\w+.-]*://[^/]+/|[\w.-]+@[\w.-]+:)(.+?)(?:\.git)?/?$", re.I)
MR_REFS = [
    re.compile(r"Merge pull request #(\d+)"),
    re.compile(r"See merge request \S*?!(\d+)"),
    re.compile(r"\(pull request #(\d+)\)"),
]
SQUASH_PR = re.compile(r"\(#(\d+)\)\s*$")          # GitHub 스쿼시 머지: 제목 문단 끝의 (#123)
REVERT = re.compile(r"This reverts commit ([0-9a-f]{7,40})")


def add_args(ap) -> None:
    g = ap.add_argument_group("git (설정 없이 바로 수집)")
    g.add_argument("--repo", help="git 저장소 경로")
    g.add_argument("--name", help="저장소 이름 (관측 id 에 들어간다 — 바꾸지 않는다)")
    g.add_argument("--since", help="첫 수집 시작일 (예: 2025-10-01)")
    g.add_argument("--ref", default=None)


def source_from_args(args) -> dict | None:
    if getattr(args, "repo", None) and args.kind in ("git", "all"):
        return {"name": args.name or Path(args.repo).resolve().name, "path": args.repo,
                "ref": args.ref or "", "since": args.since or ""}
    return None


def _git(repo: str, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", repo, *args], capture_output=True, **NO_WINDOW, check=True)
    except FileNotFoundError as e:
        raise CollectError("git 실행 파일이 없다") from e
    except subprocess.CalledProcessError as e:
        raise CollectError(f"git {' '.join(args[:2])} 실패: {e.stderr.decode('utf-8', 'replace').strip()}") from e
    return out.stdout.decode("utf-8", "replace")


def default_ref(repo: str) -> str:
    out = subprocess.run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "origin/HEAD"], capture_output=True, **NO_WINDOW)
    ref = out.stdout.decode("utf-8", "replace").strip()
    return ref if out.returncode == 0 and ref and ref != "origin/HEAD" else "HEAD"


def owner_from_url(url: str) -> str | None:
    """원격 URL 의 소유자(조직·계정) — https://host/owner/repo(.git) · git@host:owner/repo · ssh://host/owner/repo. 로컬 경로는 None."""
    m = REMOTE_URL.match((url or "").strip())
    parts = m.group(1).split("/") if m else []
    return parts[-2] if len(parts) >= 2 else None


def remote_owner(repo: str, ref: str) -> str | None:
    """PR 이 모이는 저장소의 소유자. GitHub PR 이 같은 저장소의 브랜치에서 왔는지 가리는 데만 쓰고 저장하지 않는다.
    포크 클론에서는 origin 이 내 포크다 — upstream 이 있으면 그것, 없으면 ref 의 원격, 그것도 아니면 origin."""
    out = subprocess.run(["git", "-C", repo, "remote"], capture_output=True, **NO_WINDOW)
    remotes = out.stdout.decode("utf-8", "replace").split() if out.returncode == 0 else []
    head = ref.split("/", 1)[0] if "/" in ref else ""
    remote = next((r for r in ("upstream", head, "origin") if r and r in remotes), None)
    if remote is None:
        return None
    out = subprocess.run(["git", "-C", repo, "remote", "get-url", remote], capture_output=True, **NO_WINDOW)
    return owner_from_url(out.stdout.decode("utf-8", "replace")) if out.returncode == 0 else None


def _is_ancestor(repo: str, old: str, ref: str) -> bool:
    return subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", old, ref],
                          capture_output=True, **NO_WINDOW).returncode == 0


def parse_log(raw: str) -> list[dict]:
    """`git log -z --numstat -M --format=FORMAT` 출력 → 커밋 사전 목록."""
    commits = []
    for chunk in raw.split(RS)[1:]:
        head, _, stat = chunk.rpartition(US)
        fields = head.split(US)
        if len(fields) != 7:
            raise CollectError(f"git log 형식이 예상과 다르다 (필드 {len(fields)}개)")
        sha, parents, an, ae, adate, cdate, message = fields
        files, renames = [], []
        tokens = stat.lstrip("\0").lstrip("\n").split("\0")
        i = 0
        while i < len(tokens):
            tok = tokens[i].lstrip("\n")
            if not tok:
                i += 1
                continue
            parts = tok.split("\t", 2)                         # 경로에 탭이 있어도 셋으로
            if len(parts) == 3 and parts[2] == "":             # 이름 바뀜: added\tdeleted\t\0옛\0새
                old, new = tokens[i + 1], tokens[i + 2]
                files.append((parts[0], parts[1], new))
                renames.append(f"{old}\t{new}")
                i += 3
            elif len(parts) == 3:
                files.append((parts[0], parts[1], parts[2]))
                i += 1
            else:
                i += 1
        commits.append({"sha": sha, "parents": parents.split(), "author_name": an, "author_email": ae,
                        "author_date": adate, "commit_date": cdate, "message": message.strip(),
                        "files": files, "renames": renames})
    return commits


def _mr_ids(name: str, message: str) -> list[str]:
    found: dict[str, None] = {}
    for rx in MR_REFS:
        for m in rx.finditer(message):
            found.setdefault(f"{name}!{m.group(1)}", None)
    subject = " ".join(message.split("\n\n", 1)[0].split())     # 제목 문단 (줄바꿈된 제목 포함)
    m = SQUASH_PR.search(subject)
    if m:
        found.setdefault(f"{name}!{m.group(1)}", None)
    return list(found)


def merge_parts(message: str) -> tuple[str | None, str | None, bool]:
    """머지 메시지 첫 줄 → (들여온 브랜치, 받은 브랜치, remote-tracking 인가).
    커밋·태그 머지는 (None, 받은 브랜치, False), 모르는 형식이면 (None, None, False)."""
    first = message.splitlines()[0] if message else ""
    m = MERGE_LOCAL.match(first)
    if m:
        return m.group(2), m.group(3), bool(m.group(1))
    m = MERGE_BBS.match(first)
    if m:
        return m.group(1), m.group(2), False
    m = MERGE_PR.match(first)
    if m:
        return m.group(2), None, False
    m = MERGE_BB.match(first)
    if m:
        return m.group(1), m.group(2), False
    m = MERGE_REF.match(first)
    if m:
        return None, m.group(1), False
    return None, None, False


def is_sync_merge(message: str, repo_owner: str | None = None) -> bool:
    """메시지로 본 동기화 머지 — 들인 커밋들은 이 머지의 작업이 아니다(이미 각자의 키로 이어진다).
    · 들여온 브랜치가 메인라인이면 동기화다(develop→main 릴리스 MR 포함).
    · GitHub PR 은 받는 쪽이 메시지에 없다: "from <소유자>/master" 는 소유자가 이 저장소의 원격 소유자일 때만
      동기화이고, 다르거나 모르면 포크에서 올린 실제 작업이다. 같은 이름 브랜치로 들인 MR(포크의 main → main)도 작업이다.
    · 커밋·태그를 메인라인이 아닌 브랜치로 들인 머지("Merge commit 'abc' into feature/X")는 그 브랜치를 최신으로
      맞추는 것이다(MR 이 아니면). 형식을 모르는 머지는 작업으로 둔다 — 구조 판정(collect)이 보완한다."""
    source, target, remote = merge_parts(message)
    if source is None:
        has_mr = any(rx.search(message or "") for rx in MR_REFS)
        return bool(target) and not has_mr and not is_mainline(target)
    if not is_mainline(source, remote):
        return False
    if target and normalize_branch(target) == normalize_branch(source, remote) and any(
            rx.search(message) for rx in MR_REFS):
        return False                                    # 같은 이름으로 MR — 포크의 main 에서 온 작업
    m = MERGE_PR.match(message.splitlines()[0])
    if m:
        return bool(m.group(1) and repo_owner and m.group(1).lower() == repo_owner.lower())
    return True


def to_event(c: dict, name: str, patterns, max_paths: int, collected_at: str,
             branch_commits: list[str] | None = None, keep_names: bool = False,
             sync: bool | None = None) -> tuple[dict, str]:
    """sync 를 주지 않으면 메시지로만 판정한다(collect 는 구조 판정까지 합쳐서 넘긴다)."""
    message = c["message"]
    is_merge = len(c["parents"]) > 1
    sync = is_merge and (is_sync_merge(message) if sync is None else sync)
    source, _, remote = merge_parts(message) if is_merge and not sync else (None, None, False)
    branch = source
    bkey = branch_key(branch, keep_names, remote) if branch else None
    branches = [bkey] if bkey else []
    paths = [f[2] for f in c["files"]]
    truncated = len(paths) > max_paths
    reverts = REVERT.findall(message)
    insertions = sum(int(f[0]) for f in c["files"] if f[0].isdigit())
    deletions = sum(int(f[1]) for f in c["files"] if f[1].isdigit())
    lag = (dt.datetime.fromisoformat(c["commit_date"]) - dt.datetime.fromisoformat(c["author_date"])).total_seconds()
    keys = {
        "issue_keys": patterns.issue_keys(message, *([branch] if bkey else [])),
        "paths": paths[:max_paths],
        "mr_ids": _mr_ids(name, message),
        "branches": branches,
        "reverts": reverts,
    }
    if c["renames"]:
        keys["renames"] = c["renames"][:max_paths]
    if branch_commits:
        keys["commits"] = branch_commits
    objects = [{"type": "commit", "id": c["sha"]}] + [{"type": "mr", "id": m} for m in keys["mr_ids"]]
    event = {
        "obs_id": f"vcs:{name}:{c['sha']}",
        "source": {"system": "vcs", "instance": name, "collector_kind": "cli"},
        "kind": "vcs.merge" if is_merge else "vcs.commit",
        "ts": c["commit_date"],
        "actor_role": patterns.role_of(c["author_name"], c["author_email"]),
        "objects": objects,
        "keys": {k: v for k, v in keys.items() if v},
        "metrics": {
            "files": len(paths), "insertions": insertions, "deletions": deletions,
            "binary_files": sum(1 for f in c["files"] if f[0] == "-"),
            "parents": len(c["parents"]), "is_merge": is_merge, "sync_merge": sync,
            "is_revert": bool(reverts) or message.startswith("Revert "),
            "is_fix": patterns.is_fix(message),
            "author_lag_h": round(max(lag, 0) / 3600, 2),
        },
        "excerpt_ref": f"vcs:{name}:{c['sha']}#message",
        "partial": truncated,
        "collector": COLLECTOR,
        "collected_at": collected_at,
    }
    return event, message


def collect(p, cfg: dict, source: dict, patterns, state: dict) -> Result:
    repo = str(Path(source["path"]).expanduser())
    name = source["name"]
    ref = source.get("ref") or default_ref(repo)
    max_paths = int(source.get("max_paths", 500))
    max_branch_commits = int(source.get("max_branch_commits", 200))
    use_excerpt = source.get("excerpt", True)
    keep_names = bool(source.get("keep_branch_names", False))
    head = _git(repo, "rev-parse", ref).strip()
    rng = [ref]
    notes = []
    last = state.get("head")
    if last and last == head:
        return Result(state={**state, "collected_at": now_iso()}, notes=["새 커밋 없음"])
    if last and _is_ancestor(repo, last, ref):
        rng = [f"{last}..{ref}"]
    else:
        if last:
            notes.append("지난 HEAD 가 조상이 아니다(리베이스·강제 푸시) — since 부터 다시 읽는다")
        if source.get("since"):
            rng.append(f"--since={source['since']}")
    raw = _git(repo, "log", *rng, "-z", "--numstat", "-M", "--no-color", f"--format={FORMAT}")
    commits = parse_log(raw)
    collected_at = now_iso()
    result = Result(state={"head": head, "ref": ref, "collected_at": collected_at}, notes=notes)
    owner = source.get("owner") or remote_owner(repo, ref)
    mainline = set(_git(repo, "rev-list", "--first-parent", ref).split()) if any(
        len(c["parents"]) >= 2 for c in commits) else set()          # 수집하는 ref 의 첫 부모 사슬
    for c in commits:
        branch_commits = None
        merge = len(c["parents"]) >= 2
        sync = merge and (is_sync_merge(c["message"], owner) or all(x in mainline for x in c["parents"][1:]))
        if merge and not sync:
            msg = c["message"]
            merged, _, remote = merge_parts(msg)                    # (source 는 설정 사전이라 다른 이름을 쓴다)
            interesting = patterns.issue_keys(msg) or _mr_ids(name, msg) or branch_key(merged or "", False, remote) \
                or merged is None                                   # 모르는 형식(Azure DevOps 등)도 들여온 커밋은 남긴다
            if interesting:                                    # 문어발 머지는 둘째 부모부터 전부
                out = _git(repo, "rev-list", "--no-merges", f"--max-count={max_branch_commits}",
                           *c["parents"][1:], f"^{c['parents'][0]}")
                branch_commits = out.split()
        event, message = to_event(c, name, patterns, max_paths, collected_at, branch_commits, keep_names, sync)
        result.events.append(event)
        if use_excerpt and message:
            result.excerpts[event["obs_id"]] = excerpt(strip_trailers(message), int(source.get("excerpt_chars", 1000)))
    if not commits:
        notes.append("범위에 커밋이 없다")
    return result
