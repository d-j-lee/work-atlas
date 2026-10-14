"""주간 수확 — 무인으로 도는 한 명령: 수집 → 보존 기한 → 빌드(LLM 자동 작업 포함) → 채점 → 수확 리포트.

  python -m atlas harvest [--root …] [--quiet] [--no-collect] [--no-llm] [--as-of …]
  python -m atlas schedule [--root …] [--os windows|cron|launchd] [--day MON] [--time 09:00]

- 소스 하나가 실패해도 나머지는 모으고 빌드한다. 실패는 리포트 첫머리와 종료 코드(1)로 알린다 — 스케줄러가 알릴 수 있게.
- --quiet: 실패도 새 경고도 없으면 아무것도 출력하지 않는다(정상·무변동은 침묵). 결과는 언제나 리포트와 data/state/harvest.log 에.
- 한 번에 하나만 돈다(data/state/harvest.lock — 6시간 넘은 잠금은 죽은 실행이 남긴 것으로 본다).
- 리포트: data/reports/<날짜>-harvest.md (git 밖) — 수집 · 보존 기한 · 세대 변화 · 경고 · 채점 · 확인 질문(5건 이내) · 다음 할 일.
  확인 질문은 판정이 남지 않은 트리아지(끝난 일)와 수신함 제안에서 고른다. 4주 지난 수신함 항목은 만료로 적는다(지우지 않는다).
- 원천 시스템에는 쓰지 않는다. 쓰는 곳은 data/ 와 리포트뿐이다(CLAUDE.md 규칙 9).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import sys
import time
import traceback
from pathlib import Path

from . import config as configmod
from .common import paths, write_text

INBOX_EXPIRE_DAYS = 28
QUESTIONS_MAX = 5


@contextlib.contextmanager
def _lock(p):
    f = p.state / "harvest.lock"
    f.parent.mkdir(parents=True, exist_ok=True)
    if f.exists() and time.time() - f.stat().st_mtime > 6 * 3600:
        f.unlink(missing_ok=True)
    try:
        fd = os.open(f, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(f"다른 수확이 도는 중이다 ({f}) — 끝난 뒤 다시. 남은 잠금이면 지운다") from None
    os.close(fd)
    try:
        yield
    finally:
        f.unlink(missing_ok=True)


def collect_all(p, cfg: dict, log) -> list[dict]:
    """설정된 소스를 모두 — 하나가 실패해도 나머지를 모은다."""
    import importlib
    from .collect import COLLECTORS, CollectError, run_one
    out = []
    for kind, (module_name, key) in COLLECTORS.items():
        importlib.import_module(module_name)
        for source in cfg["sources"].get(key, []):
            name = source.get("name") or kind
            buf = io.StringIO()
            t0 = time.monotonic()
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    code = run_one(p, cfg, kind, source, dry_run=False, full=False)
                ok, err = code == 0, ("계약 위반 사건 — 수집기를 고친다" if code else "")
            except CollectError as e:
                ok, err = False, str(e)
            except Exception as e:                                  # noqa: BLE001 — 한 소스의 버그가 수확 전체를 멈추지 않게
                ok, err = False, f"{type(e).__name__}: {e}"
            written = 0
            for line in buf.getvalue().splitlines():
                if line.startswith(f"[{kind}:{name}]") and " 씀 " in line:
                    try:
                        written = int(line.split(" 씀 ")[1].split()[0])
                    except (IndexError, ValueError):
                        pass
            log(buf.getvalue().rstrip())
            out.append({"kind": kind, "name": name, "ok": ok, "written": written, "error": err[:400],
                        "seconds": round(time.monotonic() - t0, 1)})
    return out


def pending_feedback(observations: list[dict], units: list[dict]) -> list[dict]:
    """트리아지는 남겼는데 판정이 없고, 그 일은 끝난 것 — 확인 질문 후보."""
    from .common import object_ref
    fed = {object_ref(o["objects"][0]) for o in observations if o["kind"] == "atlas.feedback" and o.get("objects")}
    ended = {a for u in units if u["span"].get("end") for a in u.get("anchors", [])}
    seen, out = set(), []
    for o in sorted((o for o in observations if o["kind"] == "atlas.triage" and o.get("objects")), key=lambda o: o["ts"]):
        a = object_ref(o["objects"][0])
        if a in fed or a in seen or a not in ended:
            continue
        seen.add(a)
        out.append({"anchor": a, "ts": o["ts"], "archetype": (o.get("payload") or {}).get("pred", {}).get("archetype")})
    return out


def inbox_items(root: Path, now: dt.datetime) -> list[dict]:
    d = root / "inbox"
    out = []
    for f in sorted(d.glob("*.md")) if d.exists() else []:
        age = (now.timestamp() - f.stat().st_mtime) / 86400
        try:
            title = next((x.lstrip("# ").strip() for x in f.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()), f.name)
        except OSError:
            title = f.name
        out.append({"file": f"inbox/{f.name}", "age_days": int(age), "title": title[:100], "expired": age > INBOX_EXPIRE_DAYS})
    return out


def run(p, cfg: dict, *, collect: bool = True, llm: bool = True, as_of: str | None = None, log=print) -> dict:
    """단계마다 실패를 잡아 리포트에 남긴다 — 어느 단계가 깨져도 harvest-last.json 은 언제나 쓴다(리포트를 못 쓰면 그 사실까지)."""
    from . import pipeline, retention, score as scoremod
    from .common import load_units, read_observations
    started = dt.datetime.now().astimezone()
    res: dict = {"started": started.isoformat(timespec="seconds"), "sources": [], "failures": [], "ok": True, "build": None,
                 "retention": None, "score": None, "pending": [], "inbox": [], "catalog": {}, "usage": None,
                 "warnings": [], "new_warnings": [], "report": None}

    def say(text: str) -> None:
        try:
            log(text)
        except Exception:                                           # noqa: BLE001 — 로그를 못 써도(디스크 가득) 수확은 계속
            pass

    def step(name: str, fn, default=None):
        try:
            return fn()
        except Exception as e:                                      # noqa: BLE001 — 한 단계의 실패가 수확 전체를 멈추지 않게
            res["failures"].append(f"{name} — {type(e).__name__}: {e}")
            say(f"[{name}] " + traceback.format_exc())
            return default

    def build():
        with contextlib.redirect_stdout(io.StringIO()) as out:
            b = pipeline.run(p, cfg, as_of, report=True, llm=llm, log=lambda *a: None)
        say(out.getvalue().rstrip())
        return b

    def fallback_report():
        f = p.reports / f"{started.strftime('%Y-%m-%d')}-harvest.md"
        write_text(f, f"# 주간 수확 — {started.strftime('%Y-%m-%d')} (리포트를 만들지 못했다)\n\n"
                   + "\n".join(f"- {x}" for x in res["failures"]) + "\n")
        return f

    with _lock(p):
        try:
            if collect:
                res["sources"] = step("수집", lambda: collect_all(p, cfg, say), [])
                res["failures"] += [f"{s['kind']}:{s['name']} — {s['error']}" for s in res["sources"] if not s["ok"]]
            res["retention"] = step("보존 기한", lambda: retention.purge(p, cfg, started))
            res["build"] = step("빌드", build)
            observations = step("관측 읽기", lambda: read_observations(p.observations), [])
            units = step("작업단위 읽기", lambda: load_units(p), []) if res["build"] else []
            if units:
                res["score"] = step("채점", lambda: scoremod.score(observations, units, int(cfg["policy"].get("min_judged", 20))))
            res["pending"] = step("확인 질문", lambda: pending_feedback(observations, units), [])
            res["inbox"] = step("수신함", lambda: inbox_items(p.root, started), [])
            res["catalog"] = _read_dict(p.model / "catalog.json")
            res["usage"] = step("쓰임", lambda: _usage(p, cfg))
            prev = _read_dict(p.state / "harvest-last.json")
            warns = [f"{w.get('level')}|{w.get('what')}" for w in res["catalog"].get("warnings") or [] if isinstance(w, dict)]
            res["new_warnings"] = sorted(set(warns) - set(prev.get("warnings") or []))
            res["warnings"] = warns
            res["finished"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            report = step("리포트", lambda: write_report(p, cfg, res, started))
            if report is None:                                      # 리포트가 깨지면 실패 목록만이라도 남긴다
                report = step("리포트(대체)", fallback_report)
            res["report"] = str(report) if report else None
        finally:
            res["ok"] = not res["failures"]
            res.setdefault("finished", dt.datetime.now().astimezone().isoformat(timespec="seconds"))
            write_text(p.state / "harvest-last.json", json.dumps(
                {k: res.get(k) for k in ("started", "finished", "ok", "failures", "warnings", "report")},
                ensure_ascii=False, indent=1, default=str) + "\n")
    return res


def _read(f: Path):
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _read_dict(f: Path) -> dict:
    """JSON 객체만 — 없거나 깨졌거나 객체가 아니면 빈 것(다음 수확이 고쳐 쓴다)."""
    v = _read(f)
    return v if isinstance(v, dict) else {}


def _usage(p, cfg) -> dict | None:
    from . import usage
    return usage.summary(p, cfg)


def questions(res: dict) -> list[str]:
    q = [f"`{x['anchor']}` 는 끝났다 — 트리아지 예측(원형 {x['archetype'] or '-'})이 맞았나? "
         f"`atlas record feedback --anchor {x['anchor']} --archetype-ok yes|no|unsure --verdict helpful|not_helpful|unknown`"
         for x in res["pending"]]
    q += [f"수신함 `{x['file']}` ({x['age_days']}일) — {x['title']}: 채택(방법 변경 절차) · 오버레이 · 반려 중 하나로"
          for x in res["inbox"] if not x["expired"]]
    return q[:QUESTIONS_MAX]


def write_report(p, cfg: dict, res: dict, started: dt.datetime) -> Path:
    b = res.get("build") or {}
    cat = res.get("catalog") or {}
    L = [f"<!-- atlas:generated 주간 수확 {started.isoformat(timespec='seconds')} — 고치지 않는다 -->",
         f"# 주간 수확 — {started.strftime('%Y-%m-%d')}", ""]
    if res["failures"]:
        L += ["**실패가 있다** — 아래를 고친 뒤 `atlas harvest` 를 다시 돌린다.", ""] + [f"- {x}" for x in res["failures"]] + [""]
    L += ["## 수집", ""]
    L += [f"- {'ok' if s['ok'] else '✗'} {s['kind']}:{s['name']} — 새 관측 {s['written']:,}줄 ({s['seconds']}초)"
          + (f" — {s['error']}" if s["error"] else "") for s in res["sources"]] or ["- 수집하지 않았다(--no-collect)"]
    from .retention import describe
    L += ["", f"- {describe(res['retention']) if res.get('retention') else '보존 기한을 집행하지 못했다 — 위 실패를 본다'}",
          "", "## 빌드", ""]
    if b:
        claims = b.get("claims") or {}
        n_claims = sum(sum(v.values()) for v in claims.values()) if isinstance(claims, dict) else claims
        L += [f"- 세대 `{b.get('generation')}` · 작업단위 {b.get('wus', '-')} · 주장 {n_claims}"]
        d = cat.get("drift") or {}
        if d:
            L += [f"- 지난 세대보다: 주장 +{d.get('added', 0)} −{d.get('removed', 0)} · 강해짐 {d.get('strengthened', 0)} · 약해짐 {d.get('weakened', 0)}"]
        if b.get("llm"):
            L += [f"- LLM 자동 작업: {json.dumps(b['llm'], ensure_ascii=False)[:200]}"]
    else:
        L += ["- 빌드하지 못했다 — 위 실패를 본다"]
    L += ["", "## 경고 (카탈로그)", ""]
    new = set(res["new_warnings"])
    L += [f"- [{w['level']}] {w['what']} ({w['evidence']}) → {w['action']}" + (" **새로**" if f"{w['level']}|{w['what']}" in new else "")
          for w in cat.get("warnings", [])] or ["- 없음"]
    sc = res.get("score")
    if sc:
        ah = sc["archetype_human"]
        L += ["", "## 채점", "",
              f"- 트리아지 {sc['triages']} · 사람 판정 {ah['n']}건" + (f" · 원형 적중 {ah['rate']:.0%}" if ah["rate"] is not None else "")
              + (" · **판정 가능**" if sc["ready_to_judge"] else f" · 판정까지 {max(0, sc['min_judged'] - ah['n'])}건")]
    u = res.get("usage")
    if u and u.get("lines"):
        L += ["", "## 쓰임 (제안이 실제로 반영됐나)", ""] + [f"- {x}" for x in u["lines"]]
    qs = questions(res)
    L += ["", f"## 확인 질문 ({len(qs)})", ""] + [f"{i + 1}. {x}" for i, x in enumerate(qs)] if qs else ["", "## 확인 질문", "", "- 없음"]
    expired = [x for x in res["inbox"] if x["expired"]]
    if expired:
        L += ["", f"## 만료된 수신함 ({INBOX_EXPIRE_DAYS}일 넘음 — 처리하거나 지운다)", ""] + [f"- `{x['file']}` ({x['age_days']}일) {x['title']}" for x in expired]
    L += ["", "## 다음", "", "- 업무 지도: `data/reports/` 의 최신 `atlas-<세대>.html` · 안내서: `data/reports/guides/README.md`",
          "- 수확은 무인으로 돌린다: `atlas schedule` 이 정기 실행 설정을 낸다", ""]
    f = p.reports / f"{started.strftime('%Y-%m-%d')}-harvest.md"
    write_text(f, "\n".join(L))
    return f


def _log_to(p):
    f = p.state / "harvest.log"
    f.parent.mkdir(parents=True, exist_ok=True)
    if f.exists() and f.stat().st_size > 1_000_000:                  # 1MB 넘으면 하나만 남기고 새로
        f.replace(f.with_suffix(".log.1"))
    fh = f.open("a", encoding="utf-8")

    def log(text: str = "") -> None:
        if text:
            fh.write(text + "\n")
            fh.flush()
    return log, fh


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas harvest", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root")
    ap.add_argument("--quiet", action="store_true", help="실패도 새 경고도 없으면 출력하지 않는다 (스케줄러용)")
    ap.add_argument("--no-collect", action="store_true")
    ap.add_argument("--no-llm", action="store_true", help="[llm] auto 작업을 이번에는 돌리지 않는다")
    ap.add_argument("--as-of")
    args = ap.parse_args(argv)
    p = paths(args.root)
    cfg = configmod.load(p.root)
    log, fh = _log_to(p)
    log(f"=== 수확 시작 {dt.datetime.now().astimezone().isoformat(timespec='seconds')}")
    try:
        res = run(p, cfg, collect=not args.no_collect, llm=not args.no_llm, as_of=args.as_of, log=log)
    except RuntimeError as e:
        log(f"멈춤: {e}")
        print(f"[harvest] {e}", file=sys.stderr)
        return 2
    except Exception as e:                                          # noqa: BLE001 — 상태 파일조차 못 쓴 경우(디스크 · 권한)
        log(traceback.format_exc())
        print(f"[harvest] 상태를 남기지 못했다 — {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        log(f"=== 끝 {dt.datetime.now().astimezone().isoformat(timespec='seconds')}")
        fh.close()
    noisy = res["failures"] or res["new_warnings"]
    if not args.quiet or noisy:
        print(f"[harvest] {'실패 ' + str(len(res['failures'])) + '건 · ' if res['failures'] else ''}"
              f"새 경고 {len(res['new_warnings'])} · 확인 질문 {len(questions(res))} → {res['report']}")
        for x in res["failures"]:
            print(f"  ✗ {x}", file=sys.stderr)
    return 0 if res["ok"] else 1


# ---------------------------------------------------------------- 정기 실행

DAYS = {"MON": ("Monday", 1), "TUE": ("Tuesday", 2), "WED": ("Wednesday", 3), "THU": ("Thursday", 4), "FRI": ("Friday", 5),
        "SAT": ("Saturday", 6), "SUN": ("Sunday", 0)}


def _ps(s: str) -> str:
    """PowerShell 작은따옴표 문자열 — 안의 ' 는 '' 로."""
    return "'" + str(s).replace("'", "''") + "'"


def schedule_text(root: Path, os_name: str, day: str = "MON", at: str = "09:00", name: str = "work-atlas harvest",
                  python: str | None = None) -> str:
    import shlex
    python = python or sys.executable
    day_name, day_num = DAYS[day.upper()]
    hh, mm = (int(x) for x in at.split(":"))
    if os_name == "windows":
        exe = Path(python)
        quiet_exe = exe.with_name("pythonw.exe") if exe.name.lower() == "python.exe" and exe.with_name("pythonw.exe").exists() else exe
        arg = f'-m atlas harvest --root "{root}" --quiet'
        return "\n".join([
            "# work-atlas 주간 수확 — PowerShell(일반 권한)에 붙여 넣는다. 지금 로그인한 사용자로, 로그인해 있을 때 돈다.",
            "# PC 가 꺼져 있어 지나친 실행은 켜진 뒤 바로 돈다(StartWhenAvailable). 지울 때: Unregister-ScheduledTask -TaskName "
            + _ps(name) + " -Confirm:$false",
            f"$action = New-ScheduledTaskAction -Execute {_ps(quiet_exe)} -Argument {_ps(arg)} -WorkingDirectory {_ps(root)}",
            f"$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek {day_name} -At '{hh:02d}:{mm:02d}'",
            "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew",
            f"Register-ScheduledTask -TaskName {_ps(name)} -Action $action -Trigger $trigger -Settings $settings "
            f"-Description {_ps('work-atlas: 수집 → 빌드 → 채점 → 수확 리포트 (data/reports, 로그 data/state/harvest.log)')} -Force",
            "# 확인: Start-ScheduledTask -TaskName " + _ps(name) + " ; 결과는 data\\state\\harvest-last.json",
        ]) + "\n"
    if os_name == "cron":
        root_s = Path(root).as_posix()                              # cron · launchd 는 POSIX — 다른 OS 에서 만들어도 / 로
        log = f"{root_s}/data/state/harvest.cron.log"
        return (f"# crontab -e 에 한 줄 (매주 {day_name} {hh:02d}:{mm:02d}). 꺼져 있던 시각은 건너뛴다 — 노트북이면 anacron 이나 launchd\n"
                f"{mm} {hh} * * {day_num} cd {shlex.quote(root_s)} && {shlex.quote(python)} -m atlas harvest "
                f"--root {shlex.quote(root_s)} --quiet >> {shlex.quote(log)} 2>&1\n")
    if os_name == "launchd":
        from xml.sax.saxutils import escape
        label = "com.work-atlas.harvest"
        root_s = Path(root).as_posix()
        log = f"{root_s}/data/state/harvest.launchd.log"
        args = "".join(f"<string>{escape(str(a))}</string>" for a in (python, "-m", "atlas", "harvest", "--root", root_s, "--quiet"))
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                f"<!-- ~/Library/LaunchAgents/{label}.plist 로 저장하고 launchctl load 한다. 잠들어 있던 시각은 깨어난 뒤 돈다 -->\n"
                '<plist version="1.0"><dict>'
                f"<key>Label</key><string>{label}</string><key>ProgramArguments</key><array>{args}</array>"
                f"<key>WorkingDirectory</key><string>{escape(root_s)}</string>"
                f"<key>StartCalendarInterval</key><dict><key>Weekday</key><integer>{day_num}</integer>"
                f"<key>Hour</key><integer>{hh}</integer><key>Minute</key><integer>{mm}</integer></dict>"
                f"<key>StandardOutPath</key><string>{escape(str(log))}</string><key>StandardErrorPath</key><string>{escape(str(log))}</string>"
                "</dict></plist>\n")
    raise ValueError(f"모르는 OS: {os_name}")


def schedule_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atlas schedule", description="주간 수확을 정기 실행으로 — 붙여 넣을 설정을 출력한다(아무것도 등록하지 않는다).")
    ap.add_argument("--root")
    ap.add_argument("--os", choices=["windows", "cron", "launchd"],
                    default="windows" if os.name == "nt" else "launchd" if sys.platform == "darwin" else "cron")
    ap.add_argument("--day", default="MON", choices=sorted(DAYS), help="요일 (기본 MON)")
    ap.add_argument("--time", default="09:00", help="시각 HH:MM (기본 09:00)")
    ap.add_argument("--name", default="work-atlas harvest")
    args = ap.parse_args(argv)
    p = paths(args.root)
    if not (p.root / "method" / "codebook.yaml").exists():
        print(f"[schedule] {p.root} 는 work-atlas 인스턴스가 아니다", file=sys.stderr)
        return 2
    print(schedule_text(p.root, args.os, args.day, args.time, args.name), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
