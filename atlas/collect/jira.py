"""Jira 수집기 — Data Center(REST v2) · Cloud(REST v3). 읽기 전용.

사건 (모두 kind 접두 issue.):
  issue.created     생성 시각. labels 는 **생성 당시** 값 — 지금 값에서 변경 이력을 거꾸로 되감아 복원한다.
                    (지금 값을 생성 시각에 붙이면 시간 분할 평가에 미래가 샌다)
  issue.transition  상태 변경. labels {status, status_from} · metrics {to_done, reopen}
  issue.resolved    해결(resolution 설정). labels {resolution}
  issue.reopened    해결 취소(resolution 해제)
  issue.changed     유형·우선순위·컴포넌트·라벨·수정 버전 변경. labels {field: [이름], <이름>: 변경 **뒤의 전체 값**}
  issue.linked      이슈 연결 추가. keys.links "<관계 문구>|<대상 키>" (예: "is caused by|ABC-1")
  issue.comment     댓글. 본문은 발췌 캐시로만 (comments=false 로 끈다)
역할: 작성자 → self · bot · unknown. 사람 이름·계정은 저장하지 않는다.
API: DC   GET /rest/api/2/search   startAt·total, 인증 Bearer <개인 액세스 토큰>
     Cloud GET /rest/api/3/search/jql nextPageToken·isLast, 인증 Basic email:api_token, 본문은 ADF.
     Cloud 의 expand=changelog 는 잘릴 수 있어 total 이 더 크면 /rest/api/3/issue/{key}/changelog 로 나머지를 읽는다.
증분: 지난번 본 updated 최댓값 - 1일 부터 다시 읽는다(시간대 여유). 중복은 저장소가 건너뛴다.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import CollectError, Result, excerpt, strip_trailers
from ..common import now_iso

COLLECTOR = "jira@1"
FIELDS = ["summary", "issuetype", "status", "resolution", "resolutiondate", "created", "updated", "priority",
          "components", "labels", "fixVersions", "reporter", "creator", "description", "comment", "parent"]
SINGLE = {"issuetype": "type", "priority": "priority"}
MULTI = {"component": "component", "fix version": "fix_version"}
LINK_PHRASE = re.compile(r"^This issue (.+?) ([A-Z][A-Z0-9]{1,9}-\d+)\s*$")


def add_args(ap) -> None:
    g = ap.add_argument_group("jira (설정 없이 바로 수집)")
    g.add_argument("--base-url")
    g.add_argument("--jql")
    g.add_argument("--flavor", choices=["datacenter", "cloud"], default=None)
    g.add_argument("--auth-env", help="자격 증명이 든 환경 변수 이름 (값이 아니라 이름)")
    g.add_argument("--jira-name", help="소스 이름 (기본 jira)")


def source_from_args(args) -> dict | None:
    if getattr(args, "base_url", None) and args.kind in ("jira", "all"):
        return {"name": args.jira_name or "jira", "base_url": args.base_url, "jql": args.jql or "",
                "flavor": args.flavor or "datacenter", "auth_env": args.auth_env or ""}
    return None


# ---------------------------------------------------------------- HTTP

class _SameHostAuth(urllib.request.HTTPRedirectHandler):
    """다른 호스트나 https 가 아닌 주소로 리다이렉트되면 Authorization 을 떼고 따라간다 — 자격 증명이 새지 않게."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        target, origin = urllib.parse.urlsplit(newurl), urllib.parse.urlsplit(req.full_url)
        if new is not None and (target.netloc != origin.netloc or target.scheme != "https"):
            for h in ("Authorization", "Cookie"):
                new.remove_header(h)
        return new


_OPENER = urllib.request.build_opener(_SameHostAuth)


def _headers(source: dict) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "work-atlas (read-only collector)"}
    secret = os.environ.get(source.get("auth_env") or "", "")
    if secret and not str(source.get("base_url", "")).lower().startswith("https://"):
        raise CollectError("자격 증명을 https 가 아닌 주소로 보내지 않는다 — base_url 을 https 로")
    if secret:
        if source.get("flavor", "datacenter") == "cloud":
            headers["Authorization"] = "Basic " + base64.b64encode(secret.encode()).decode()
        else:
            headers["Authorization"] = f"Bearer {secret}"
    return headers


def http_fetch(url: str, headers: dict, retries: int = 5) -> dict:
    """GET → JSON. 429·503 은 Retry-After(최대 60초)를 지키며 다시 시도한다."""
    delay = 2.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with _OPENER.open(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < retries:
                wait = e.headers.get("Retry-After")
                time.sleep(min(float(wait) if wait and wait.isdigit() else delay, 60))
                delay *= 2
                continue
            body = e.read().decode("utf-8", "replace")[:300]
            raise CollectError(f"HTTP {e.code} {url.split('?')[0]} — {body}") from e
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise CollectError(f"{url.split('?')[0]} 에 닿지 못했다 — {e.reason}") from e
    raise CollectError("재시도 한도 초과")


# ---------------------------------------------------------------- 변환

def to_iso(ts: str) -> str:
    """Jira 시각('2026-10-06T20:44:45.000+0000') → 시간대 있는 ISO 8601(초 단위)."""
    return dt.datetime.fromisoformat(ts).isoformat(timespec="seconds")


def adf_text(node) -> str:
    """Cloud v3 의 ADF 문서 → 평문. DC 는 이미 문자열이다."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    out: list[str] = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "text":
                out.append(n.get("text", ""))
            elif n.get("type") in ("hardBreak",):
                out.append("\n")
            for c in n.get("content", []) or []:
                walk(c)
            if n.get("type") in ("paragraph", "heading", "listItem", "codeBlock", "blockquote"):
                out.append("\n")
        elif isinstance(n, list):
            for c in n:
                walk(c)
    walk(node)
    return "".join(out).strip()


def _person(p: dict | None) -> tuple[str | None, ...]:
    p = p or {}
    return (p.get("name"), p.get("key"), p.get("emailAddress"), p.get("accountId"), p.get("displayName"))


def _names(items) -> list[str]:
    return sorted({i.get("name") or i.get("value") or "" for i in (items or [])} - {""})


def _current(fields: dict) -> dict[str, list[str]]:
    return {
        "type": [fields["issuetype"]["name"]] if fields.get("issuetype") else [],
        "priority": [fields["priority"]["name"]] if fields.get("priority") else [],
        "component": _names(fields.get("components")),
        "label": sorted(set(fields.get("labels") or [])),
        "fix_version": _names(fields.get("fixVersions")),
    }


def _items(histories: list[dict]) -> list[tuple[dict, int, dict]]:
    """(history, index, item) 를 시각·id 순으로."""
    rows = []
    for h in histories:
        for i, item in enumerate(h.get("items", [])):
            rows.append((h, i, item))
    rows.sort(key=lambda r: (dt.datetime.fromisoformat(r[0]["created"]), int(r[0].get("id") or 0), r[1]))
    return rows


def _field(item: dict) -> str:
    return (item.get("field") or "").strip().lower()


def initial_labels(fields: dict, rows) -> dict[str, list[str]]:
    """지금 값에서 변경 이력을 최근 것부터 되감아 생성 당시 값을 만든다."""
    state = {k: set(v) for k, v in _current(fields).items()}
    for _, _, item in reversed(rows):
        f = _field(item)
        before, after = item.get("fromString") or "", item.get("toString") or ""
        if f in SINGLE:
            state[SINGLE[f]] = {before} if before else set()
        elif f in MULTI:
            key = MULTI[f]
            if after:
                state[key].discard(after)
            if before:
                state[key].add(before)
        elif f == "labels":
            state["label"] = set(before.split())
    return {k: sorted(v) for k, v in state.items()}


def issue_events(issue: dict, name: str, base_url: str, patterns, done: set[str], collected_at: str,
                 comments: bool, partial_changelog: bool) -> tuple[list[dict], dict[str, str]]:
    key = issue["key"]
    f = issue["fields"]
    url = f"{base_url.rstrip('/')}/browse/{key}"
    obj = [{"type": "issue", "id": key, "url": url}]
    histories = ((issue.get("changelog") or {}).get("histories")) or []
    rows = _items(histories)
    events, texts = [], {}

    def ev(suffix, kind, ts, role, labels=None, keys=None, metrics=None, partial=False):
        e = {
            "obs_id": f"issue:{name}:{key}#{suffix}",
            "source": {"system": "issue", "instance": name, "collector_kind": "api"},
            "kind": kind, "ts": to_iso(ts), "actor_role": role, "objects": obj,
            "keys": {"issue_keys": [key], **{k: v for k, v in (keys or {}).items() if v}},
            "labels": {k: v for k, v in (labels or {}).items() if v},
            "metrics": metrics or {}, "excerpt_ref": None, "partial": partial,
            "collector": COLLECTOR, "collected_at": collected_at,
        }
        events.append(e)
        return e

    parent = (f.get("parent") or {}).get("key")
    created = ev("created", "issue.created", f["created"], patterns.role_of(*_person(f.get("reporter") or f.get("creator"))),
                 labels=initial_labels(f, rows), keys={"parent": [parent] if parent else []}, partial=partial_changelog)
    created["excerpt_ref"] = f"{url}#description"
    texts[created["obs_id"]] = f"{f.get('summary') or ''}\n\n{adf_text(f.get('description'))}".strip()

    state = {k: set(v) for k, v in initial_labels(f, rows).items()}
    for h, i, item in rows:
        fld = _field(item)
        role = patterns.role_of(*_person(h.get("author")))
        before, after = item.get("fromString") or "", item.get("toString") or ""
        suffix = f"h{h.get('id')}:{i}"
        if fld == "status":
            reopen = before.lower() in done and after.lower() not in done
            ev(suffix, "issue.transition", h["created"], role, labels={"status": [after], "status_from": [before]},
               metrics={"to_done": after.lower() in done, "reopen": reopen})
        elif fld == "resolution":
            if after:
                ev(suffix, "issue.resolved", h["created"], role, labels={"resolution": [after]})
            else:
                ev(suffix, "issue.reopened", h["created"], role, labels={"resolution_from": [before]})
        elif fld in SINGLE or fld in MULTI or fld == "labels":
            key_ = SINGLE.get(fld) or MULTI.get(fld) or "label"
            if fld in SINGLE:
                state[key_] = {after} if after else set()
            elif fld in MULTI:
                if before:
                    state[key_].discard(before)
                if after:
                    state[key_].add(after)
            else:
                state[key_] = set(after.split())
            ev(suffix, "issue.changed", h["created"], role, labels={"field": [key_], key_: sorted(state[key_])},
               metrics={"cleared": not state[key_]})
        elif fld == "link" and after:
            m = LINK_PHRASE.match(after)
            if m:
                ev(suffix, "issue.linked", h["created"], role, keys={"links": [f"{m.group(1)}|{m.group(2)}"]})
    if comments:
        block = f.get("comment") or {}
        items = block.get("comments") or []
        for c in items:
            body = strip_trailers(adf_text(c.get("body")))
            e = ev(f"c{c['id']}", "issue.comment", c["created"], patterns.role_of(*_person(c.get("author"))),
                   keys={"mentions": [k for k in patterns.issue_keys(body) if k != key]},
                   metrics={"chars": len(body)})
            texts[e["obs_id"]] = body
        if block.get("total", len(items)) > len(items):
            for e in events:
                if e["kind"] == "issue.comment":
                    e["partial"] = True
    return events, texts


# ---------------------------------------------------------------- 수집

def _jql(source: dict, state: dict) -> str:
    base = re.sub(r"(?is)\s+order\s+by\s+.*$", "", source.get("jql") or "").strip()
    since = state.get("updated_max")
    if since:
        start = (dt.datetime.fromisoformat(since) - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        base = f"({base}) AND updated >= \"{start}\"" if base else f"updated >= \"{start}\""
    return f"{base} ORDER BY created ASC, key ASC" if base else "ORDER BY created ASC, key ASC"


def search_pages(source: dict, state: dict, fetch):
    """페이지를 하나씩 돌려준다 (issues, partial 표시)."""
    base = source["base_url"].rstrip("/")
    headers = _headers(source)
    size = int(source.get("page_size", 100))
    jql = _jql(source, state)
    fields = ",".join(FIELDS)
    if source.get("flavor", "datacenter") == "cloud":
        token = None
        while True:
            q = {"jql": jql, "maxResults": size, "fields": fields, "expand": "changelog"}
            if token:
                q["nextPageToken"] = token
            page = fetch(f"{base}/rest/api/3/search/jql?{urllib.parse.urlencode(q)}", headers)
            for issue in page.get("issues", []):
                cl = issue.get("changelog") or {}
                n, total, cap = len(cl.get("histories", [])), cl.get("total"), cl.get("maxResults")
                if (total is not None and total > n) or (total is None and cap is not None and n >= cap):
                    cl["histories"] = _cloud_changelog(base, issue["key"], headers, fetch)   # 잘렸거나 알 수 없다
                    issue["changelog"] = cl
            yield page.get("issues", []), False
            token = page.get("nextPageToken")
            if page.get("isLast", not token) or not token:
                break
    else:
        start, total = 0, None
        while True:
            q = {"jql": jql, "startAt": start, "maxResults": size, "fields": fields, "expand": "changelog"}
            page = fetch(f"{base}/rest/api/2/search?{urllib.parse.urlencode(q)}", headers)
            issues = page.get("issues", [])
            total = page.get("total", total)
            truncated = False
            for issue in issues:
                cl = issue.get("changelog") or {}
                if cl.get("total", 0) > len(cl.get("histories", [])):
                    truncated = True                      # DC 는 이슈 단위 changelog 페이지 API 가 없다
                    issue["_changelog_truncated"] = True
            yield issues, truncated
            start += len(issues)
            if not issues or (total is not None and start >= total):
                break


def _cloud_changelog(base: str, key: str, headers: dict, fetch) -> list[dict]:
    out, start = [], 0
    while True:
        page = fetch(f"{base}/rest/api/3/issue/{key}/changelog?startAt={start}&maxResults=100", headers)
        values = page.get("values", [])
        out.extend(values)
        start += len(values)
        if page.get("isLast", True) or not values:
            return out


def collect(p, cfg: dict, source: dict, patterns, state: dict, fetch=http_fetch) -> Result:
    name = source.get("name") or "jira"
    done = {s.lower() for s in source.get("done_statuses", ["Resolved", "Closed", "Done", "완료", "해결됨"])}
    comments = source.get("comments", True)
    use_excerpt = source.get("excerpt", True)
    limit = int(source.get("excerpt_chars", 1000))
    collected_at = now_iso()
    result = Result()
    updated_max = state.get("updated_max")
    n_issues = 0
    for issues, _ in search_pages(source, state, fetch):
        for issue in issues:
            n_issues += 1
            events, texts = issue_events(issue, name, source["base_url"], patterns, done, collected_at,
                                         comments, bool(issue.get("_changelog_truncated")))
            result.events.extend(events)
            if use_excerpt:
                for oid, text in texts.items():
                    if text:
                        result.excerpts[oid] = excerpt(text, limit)
            upd = issue["fields"].get("updated")
            if upd:
                iso = to_iso(upd)
                if not updated_max or dt.datetime.fromisoformat(iso) > dt.datetime.fromisoformat(updated_max):
                    updated_max = iso
    result.state = {**state, "collected_at": collected_at}     # 빈 결과여도 '마지막으로 본 시각'은 갱신한다
    if updated_max:
        result.state["updated_max"] = updated_max
    result.notes.append(f"이슈 {n_issues}건 읽음")
    truncated = sum(1 for e in result.events if e["kind"] == "issue.created" and e["partial"])
    if truncated:
        result.notes.append(f"변경 이력이 잘린 이슈 {truncated}건 — 생성 당시 라벨 복원이 부정확할 수 있다 (partial)")
    return result
