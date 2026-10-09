#!/usr/bin/env python3
"""WU 입력 문서 — LLM 이 읽는 유일한 입력을 결정론으로 만든다 (method/schema.md "WU 입력 문서").

- 같은 WU·같은 관측·같은 발췌면 언제 만들어도 같은 바이트가 나온다. input_hash 는 그 sha256.
- 넣지 않는 것: 주석(annotation)·빌드 정보(build)·atlas 사건·오버레이 — 현재 분류가 블라인드 도출에 새지 않게.
- 발췌는 data/excerpts.jsonl({"obs_id","text"}, 보존 기한이 있는 캐시)에서 읽는다.
  **1차 방어는 수집기다**: 필요한 만큼만 발췌하고, 사람 이름은 역할로, 비밀값은 지운다.
  여기의 마스킹(비밀값·토큰·이메일·전화·주민/외국인 등록번호)은 수집기가 놓친 것을 잡는 안전망이고 완전하지 않다.

사용: python3 render_wu.py [--units data/units/stitched.jsonl] [--out-dir data/render] [--max-excerpt 500] [--max-doc-kb 40]
출력: WU 마다 <out-dir>/<wu>-<hash8>.md 와 <out-dir>/index.json ({wu_id: {file, input_hash}}).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import parse_ts, paths, read_jsonl, read_observations, sha256_text, write_text  # noqa: E402

# 순서가 중요하다: 헤더·토큰 형식을 먼저 가리고, 그다음 "키 = 값" 형식을 가린다.
# 모든 반복에 상한을 둔다 — 상한 없는 반복은 긴 한 줄(base64·압축 코드)에서 시간이 제곱으로 는다.
_SEP = r"[-.\s]?"
_KEY = (r"(?i)([\"']?[\w.-]{0,24}(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key|credential)"
        r"[\w.-]{0,24}[\"']?\s{0,4}[:=]{1,2}\s{0,4})")
MASKS = [
    (re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----(?:.*?-----END [A-Z ]{0,40}PRIVATE KEY-----|.*\Z)", re.S),
     "[비밀키]"),
    (re.compile(r"(?i)\b(bearer|basic)\s{1,4}[A-Za-z0-9._~+/=-]{8,4096}"), r"\1 [가림]"),
    (re.compile(r"(?i)\b((?:set-)?cookie)\s{0,4}:[^\n]{1,4096}"), r"\1: [가림]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,4096}\.[A-Za-z0-9_-]{5,4096}\.[A-Za-z0-9_-]{5,4096}"), "[토큰]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255}|xox[abposr]-[A-Za-z0-9-]{10,255}"
                r"|sk[-_](?:live_|test_)?[A-Za-z0-9]{16,255}|(?:AKIA|ASIA)[0-9A-Z]{16})\b"), "[토큰]"),
    (re.compile(_KEY + r"([\"'])[^\"'\n]{1,500}\2"), r"\1\2[가림]\2"),          # 따옴표 값 (공백 포함)
    (re.compile(_KEY + r"(?![\"'\[])[^\s\"',;}]{1,500}"), r"\1[가림]"),           # 따옴표 없는 값
    (re.compile(r"\b\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])-?[1-8]\d{6}\b"), "[등록번호]"),
    (re.compile(r"(?:\+82" + _SEP + r"1[016789]|\b01[016789])" + _SEP + r"\d{3,4}" + _SEP + r"\d{4}\b"), "[전화]"),
    (re.compile(r"\b0(?:2|[3-6][1-5])" + _SEP + r"\d{3,4}" + _SEP + r"\d{4}\b"), "[전화]"),
    (re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}"
                r"\.(?!(?:png|jpe?g|gif|svg|webp|ico)\b)[A-Za-z]{2,24}\b"), "[이메일]"),
]


def mask(text: str) -> str:
    has_assign = ":" in text or "=" in text
    for pattern, repl in MASKS:
        if not has_assign and pattern.pattern.startswith(_KEY):
            continue
        text = pattern.sub(repl, text)
    return text


def one_line(text: str, limit: int) -> str:
    text = " ⏎ ".join(part.strip() for part in text.strip().splitlines() if part.strip())
    return text if len(text) <= limit else text[:limit] + "…(잘림)"


def _keys(obs: dict) -> str:
    parts = [f"{k}={v}" for k in sorted(obs.get("keys", {})) for v in sorted(obs["keys"][k])]
    return "; ".join(parts) or "-"


def render(unit: dict, obs_by_id: dict[str, dict], excerpts: dict[str, str],
           max_excerpt: int = 500, max_doc_bytes: int = 40_000) -> str:
    members = []
    missing = 0
    for m in unit.get("members", []):
        obs = obs_by_id.get(m["obs_id"])
        if obs is None:
            missing += 1
        elif obs["source"]["system"] != "atlas":
            members.append(obs)
    members.sort(key=lambda o: (parse_ts(o["ts"]), o["obs_id"]))

    labels: dict[str, set[str]] = {}
    for o in members:
        for k, vs in (o.get("labels") or {}).items():
            labels.setdefault(k, set()).update(vs)
    span = unit.get("span", {})
    stats = unit.get("stats", {})
    head = [
        f"# 작업단위 {unit['wu_id']}",
        f"- 앵커: {', '.join(unit.get('anchors', [])) or '-'}",
        f"- 기간: {span.get('start') or '-'} → {span.get('end') or '진행 중'} · 확정: {'예' if span.get('settled') else '아니오'}",
        f"- 소스: {', '.join(sorted({o['source']['system'] for o in members})) or '-'}",
        f"- 기존 라벨: {'; '.join(f'{k}=' + ','.join(sorted(v)) for k, v in sorted(labels.items())) or '-'}",
        f"- 결정론 신호: {', '.join(sorted(stats.get('deterministic_signals', []))) or '없음'}",
        "- 통계: " + (", ".join(f"{k}={stats[k]}" for k in ("duration_h", "n_changes", "n_files") if k in stats) or "-"),
    ]
    if missing:
        head.append(f"- 누락 관측: {missing}건 (members 에 있으나 L0 에 없음)")
    head += ["", "## 관측 (시각순)"]

    body, size = [], len("\n".join(head).encode("utf-8"))
    for i, o in enumerate(members):
        excerpt = excerpts.get(o["obs_id"])
        excerpt = one_line(mask(excerpt[: max_excerpt * 4]), max_excerpt) if excerpt else "-"   # 자른 뒤 가린다
        line = f"- {o['ts']} · {o['kind']} · {o['actor_role']} · 키[{_keys(o)}] · {o['obs_id']} · {excerpt}"
        size += len(line.encode("utf-8")) + 1
        if size > max_doc_bytes:
            body.append(f"- …(문서 상한 도달: 관측 {len(members) - i}건 생략)")
            break
        body.append(line)
    return "\n".join(head + body) + "\n"


def safe_name(wu_id: str) -> str:
    """읽을 수 있는 접두 + wu_id 해시 8자. 한글 등이 지워져 이름이 겹치는 일을 막는다."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", wu_id).strip("_")[:60] or "wu"
    return f"{stem}-{sha256_text(wu_id)[7:15]}.md"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--units")
    ap.add_argument("--out-dir")
    ap.add_argument("--max-excerpt", type=int, default=500, help="사건당 발췌 글자 상한 (policy.md §3)")
    ap.add_argument("--max-doc-kb", type=int, default=40, help="문서당 KB 상한 (policy.md §3)")
    args = ap.parse_args(argv)
    p = paths(args.root)
    units = list(read_jsonl(Path(args.units) if args.units else p.stitched))
    obs_by_id = {o["obs_id"]: o for o in read_observations(p.observations)}
    excerpts = {e["obs_id"]: e["text"] for e in read_jsonl(p.excerpts)} if p.excerpts.exists() else {}
    out_dir = Path(args.out_dir) if args.out_dir else p.render
    index = {}
    for u in units:
        doc = render(u, obs_by_id, excerpts, args.max_excerpt, args.max_doc_kb * 1000)
        name = safe_name(u["wu_id"])
        write_text(out_dir / name, doc)
        index[u["wu_id"]] = {"file": name, "input_hash": sha256_text(doc)}
    write_text(out_dir / "index.json", json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    print(f"rendered {len(index)} WU → {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
