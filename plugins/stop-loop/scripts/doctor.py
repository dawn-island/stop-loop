#!/usr/bin/env python3
"""설치 점검 — 이 기계와 이 프로젝트가 러너를 돌릴 준비가 됐는지 한 화면으로 답한다.

  python3 bin/doctor.py [<프로젝트 경로>]          # hq 가 있는 기계
  python3 .stop-loop/bin/doctor.py [<경로>]        # 꾸러미만 푼 기계

**아무것도 고치지 않는다.** 읽기만 한다 — 고치는 것은 사람이나 `sync-standard.py` 가 한다.
항목마다 있음·없음·낡음과 할 일 한 줄을 적는다. 「막는 것」(러너가 시작을 거부하거나 시험 없이
07 을 통과시키는 것)이 하나라도 「있음」이 아니면 종료 코드 1, 아니면 0, 경로 오류는 2.

판정 규칙은 러너와 정의 배포가 이미 가진 것을 그대로 쓴다 — 점검이 「있음」이라 했는데 러너는
멈추는 거짓 안심을 막으려는 것이다. 점검 파일 옆에 `sync-standard.py` 가 있으면(hq 모드)
정의의 낡음까지 견주고, 없으면(꾸러미 모드) 매니페스트의 목록대로 있는지만 본다.
"""
import collections
import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True   # 러너를 가져오기 전에 — 꾸러미 모드에서 프로젝트 안에 캐시가 생긴다
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import runner  # noqa: E402

ROOT = os.path.dirname(HERE)      # 정본 뿌리: hq 저장소, 또는 꾸러미의 `.stop-loop/`
GIT_MIN = (2, 44)                 # workflows/GUIDE.md 「git 2.44+」
SECTIONS = ("돌리는 법", "실행하지 않는다", "고치지 않는다", "이 프로젝트의 규약", "작업 목록", "신원")
TIMEOUT = 10                      # 바깥 명령 시간 제한(초)

Item = collections.namedtuple("Item", "group name status blocks todo note", defaults=("",))
MACHINE, PROJECT = "이 기계", "이 프로젝트"


def _run(cmd):
    """읽기 명령 하나를 돌려 (종료 코드, 출력). 시간을 넘기면 코드 None, 못 부르면 127."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, ""
    except OSError:
        return 127, ""
    return p.returncode, (p.stdout + p.stderr).strip()


def _ok(group, name, note=""):
    return Item(group, name, "있음", False, "", note)


def _config(project):
    """(상태, checks 개수, 인계 방식). 상태는 missing · unreadable · ok.

    `checks` 판정은 러너의 `load_models` 가 읽는 것과 같다 — 러너가 건너뛰는 경우와 점검이
    「없음」이라 하는 경우가 일치해야 한다.
    """
    path = os.path.join(project, "stop-loop.config.json")
    try:
        with open(path, encoding="utf-8") as f:
            top = json.load(f)
    except FileNotFoundError:
        return "missing", 0, "pr"
    except (OSError, ValueError):
        return "unreadable", 0, "pr"
    if not isinstance(top, dict):
        return "unreadable", 0, "pr"
    try:
        models = runner.load_models(project)
    except Exception:             # 설정 안쪽 모양이 틀려 러너도 못 읽는 경우
        return "unreadable", 0, "pr"
    return "ok", len(models.get("_checks") or []), models.get("_handoff", "pr")


def machine_items(handoff):
    items = []
    missing = runner.missing_commands()
    if "claude" in missing:
        items.append(Item(MACHINE, "claude", "없음", True,
                          "%s 에 claude 실행 파일을 둔다 — 러너가 이 고정 경로를 부른다" % runner.CLAUDE))
    else:
        items.append(_ok(MACHINE, "claude"))
    stops = runner.blocking(missing, handoff)   # gh 는 인계 방식이 pr 일 때만 막는다 — 러너와 같은 규칙
    for name in ("python3", "git", "gh"):
        if name in missing:
            items.append(Item(MACHINE, name, "없음", name in stops, "%s 를 설치한다" % name))
        else:
            items.append(_ok(MACHINE, name))
    if "git" not in missing:
        rc, out = _run(["git", "--version"])
        m = re.search(r"git version (\d+)\.(\d+)", out) if rc == 0 else None
        if not m:
            items.append(Item(MACHINE, "git 판", "낡음", False,
                              "git 판을 읽지 못했다 — %s 이상인지 손으로 확인한다" % ".".join(map(str, GIT_MIN)),
                              out or "(출력 없음)"))
        elif (int(m.group(1)), int(m.group(2))) < GIT_MIN:
            items.append(Item(MACHINE, "git 판", "낡음", False,
                              "git 을 %s 이상으로 올린다" % ".".join(map(str, GIT_MIN)),
                              "%s.%s" % m.groups()))
        else:
            items.append(_ok(MACHINE, "git 판", "%s.%s" % m.groups()))
    if handoff not in ("push", "local") and "gh" not in missing:     # 기본값 pr
        rc, _ = _run(["gh", "auth", "status"])
        if rc is None:
            items.append(Item(MACHINE, "gh 로그인", "없음", True,
                              "`gh auth status` 가 %d초 안에 답하지 않았다 — 네트워크를 확인하고 다시 돌린다" % TIMEOUT))
        elif rc:
            items.append(Item(MACHINE, "gh 로그인", "없음", True, "`gh auth login` 으로 로그인한다"))
        else:
            items.append(_ok(MACHINE, "gh 로그인"))
    return items


def _config_item(project):
    state, n, _ = _config(project)
    if state == "missing":
        todo = "`stop-loop.config.example.json` 을 복사해 `checks` 를 이 프로젝트의 시험 명령으로 고친다"
    elif state == "unreadable":
        todo = "JSON 문법을 고친다"
    elif n == 0:
        todo = "`checks` 에 시험 명령을 적는다"
    else:
        return _ok(PROJECT, "checks", "%d개" % n)
    return Item(PROJECT, "checks", "없음", True, todo)


def _sync_standard():
    spec = importlib.util.spec_from_file_location(
        "sync_standard", os.path.join(ROOT, "bin", "sync-standard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest(text_path):
    """(정본 커밋, 담긴 에이전트 정의 이름들). 읽지 못하면 (None, [])."""
    try:
        with open(text_path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, ValueError):
        return None, []
    sha = re.search(r"정본 커밋: ([0-9a-f]{40})", text)
    names = re.findall(r"^- `\.claude/agents/([^`/]+\.md)`$", text, re.M)
    return (sha.group(1) if sha else None), names


def bundle_note():
    """꾸러미 모드일 때만 안내 한 줄. 정본과의 낡음은 여기서 가리지 못한다."""
    if os.path.isfile(os.path.join(ROOT, "bin", "sync-standard.py")):
        return ""
    sha, _ = _manifest(os.path.join(ROOT, "MANIFEST.md"))
    if not sha:
        return ""
    return ("  · 정본 커밋 %s — 정의가 낡았는지는 hq 가 있는 기계에서 "
            "`python3 bin/doctor.py <경로>` 로 확인한다" % sha[:7])


def _agent_items(project):
    name, redo = "에이전트 정의", "꾸러미를 다시 푼다"
    if os.path.isfile(os.path.join(ROOT, "bin", "sync-standard.py")):
        missing, stale = _sync_standard().agent_diff(project)
        howto = ("`python3 bin/sync-standard.py --apply --only <projects.yaml 의 프로젝트 이름>` "
                 "또는 정본 `workflows/stop-loop/agents/` 에서 손으로 맞춘다")
        expected = None
    else:
        _, expected = _manifest(os.path.join(ROOT, "MANIFEST.md"))
        if not expected:
            return [Item(PROJECT, name, "없음", True, redo, "매니페스트를 읽지 못했다")]
        agents = os.path.join(project, ".claude", "agents")
        missing = [n for n in expected if not os.path.isfile(os.path.join(agents, n))]
        stale, howto = [], redo
    items = []
    if missing:
        items.append(Item(PROJECT, name, "없음", True, howto, ", ".join(missing)))
    if stale:
        items.append(Item(PROJECT, name, "낡음", False, howto, ", ".join(stale)))
    return items or [_ok(PROJECT, name, "%d개" % len(expected) if expected else "정본과 같다")]


def _claude_md_item(project):
    try:
        with open(os.path.join(project, "CLAUDE.md"), encoding="utf-8") as f:
            headings = [l for l in f.read().splitlines() if l.startswith("#")]
    except (OSError, ValueError):
        headings = []
    lost = [s for s in SECTIONS if not any(s in h for h in headings)]
    if not lost:
        return _ok(PROJECT, "CLAUDE.md 여섯 절")
    return Item(PROJECT, "CLAUDE.md 여섯 절", "없음", False,
                "`workflows/GUIDE.md` 의 「`CLAUDE.md` 에 적을 여섯 절」(꾸러미에서는 `CLAUDE.stop-loop.md`)을 보고 채운다",
                ", ".join(lost))


def project_items(project):
    items = [_config_item(project)]
    items += _agent_items(project)
    if os.path.isdir(os.path.join(project, "docs", "backlog")):
        items.append(_ok(PROJECT, "docs/backlog/"))
    else:
        items.append(Item(PROJECT, "docs/backlog/", "없음", False,
                          "`docs/backlog/` 를 만든다 — 카드 틀은 꾸러미의 `docs/backlog/_템플릿.md`"))
    items.append(_claude_md_item(project))
    # 선택 도구 둘 — 없으면 러너는 돌고 에이전트가 grep 으로 돌아간다. 판정은 러너 `search_tools` 와 같다
    if not shutil.which("graft"):
        items.append(Item(PROJECT, "graft", "없음", False, "graft 를 설치한다(없어도 실행은 된다)", "실행 파일 없음"))
    elif not os.path.isdir(os.path.join(project, runner.INDEX_DIR)):
        items.append(Item(PROJECT, "graft", "없음", False,
                          "`graft build .` 로 색인을 만든다(없어도 실행은 된다)", "색인 없음"))
    else:
        items.append(_ok(PROJECT, "graft"))
    report = os.path.join(project, runner.GRAPH_DIR, "GRAPH_REPORT.md")
    try:
        items.append(_ok(PROJECT, "graphify", dt.date.fromtimestamp(os.path.getmtime(report)).isoformat()))
    except OSError:
        items.append(Item(PROJECT, "graphify", "없음", False,
                          "`graphify .` 로 그래프를 만든다(없어도 실행은 된다)"))
    return items


def _render(i):
    line = "  [%s] %s" % (i.status, i.name)
    if i.note:
        line += " " + i.note
    if i.status != "있음":
        line += (" (막음)" if i.blocks else "") + (" — " + i.todo if i.todo else "")
    return line


def main(argv):
    project = os.path.abspath(os.path.expanduser(argv[0])) if len(argv) == 1 else (
        os.path.abspath(".") if not argv else None)
    if not project or not os.path.isdir(project):
        print("사용법: python3 bin/doctor.py [<프로젝트 경로>] — 경로는 폴더여야 한다", file=sys.stderr)
        return 2
    items = machine_items(_config(project)[2]) + project_items(project)
    for group in (MACHINE, PROJECT):
        print(group)
        for i in items:
            if i.group == group:
                print(_render(i))
        if group == PROJECT and bundle_note():
            print(bundle_note())
    bad = [i for i in items if i.status != "있음"]
    blocking = sum(1 for i in bad if i.blocks)
    print("막는 것 %d · 아쉬운 것 %d" % (blocking, len(bad) - blocking))
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
