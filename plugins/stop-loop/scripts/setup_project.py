#!/usr/bin/env python3
"""프로젝트에 STOP-LOOP 를 세우고 CLAUDE.md 표준 구간을 플러그인에 담긴 판으로 맞춘다.

  python3 setup_project.py [<프로젝트 뿌리>]      (인자가 없으면 지금 폴더)

플러그인 안(`scripts/`)에서 도는 것을 전제로 한다 — 자기 위치의 한 단계 위가 플러그인 뿌리이고
그 아래 `setup/` 에 틀 둘과 표준 원문이 있다. 표준 라이브러리만 쓴다. git 은 부르지 않고 커밋하지 않는다.
"""
import json
import os
import re
import sys

# bin/sync-standard.py 의 BEGIN·END, bin/bundle.py 의 CONFIG_BODY 두 값과 같아야 한다 — test_setup_project S-20 이 지킨다.
BEGIN, END = "<!-- hq:standard:begin", "<!-- hq:standard:end -->"
CONFIG = {"checks": [{"cmd": "python3 -m pytest -q"}], "handoff": "local"}
MARKETPLACE_REPO = "dawn-island/stop-loop"      # 2026-10-02 사람 확정 — 공개 저장소 주소
PLUGIN_ID = "stop-loop@stop-loop"
PROJECTS_JSON = os.path.expanduser("~/.stop-loop/projects.json")   # bin/dispatch.py 의 값과 같아야 한다 — test_setup_project 가 지킨다
USAGE = "사용법: python3 setup_project.py [<프로젝트 뿌리>]"


class SetupError(Exception):
    pass


def _read(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _block(text, standard):
    """CLAUDE.md 본문에 표준 구간을 맞춘 새 본문. 구간 바깥은 그대로다."""
    version = re.search(r"v\d+", standard.splitlines()[0])
    marker = "%s %s — hq/workflows/STANDARD.md 에서 배포됨. 여기를 직접 고치지 말 것 -->" % (
        BEGIN, version.group(0) if version else "v0")
    body = standard.strip()
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        tail = rest.split("-->", 1)[1].split(END, 1)[1]
        return "%s%s\n%s\n%s%s" % (head, marker, body, END, tail)
    if text.startswith("#"):
        first, rest = (text.split("\n", 1) + [""])[:2]
        return "%s\n\n%s\n%s\n%s\n\n%s" % (first, marker, body, END, rest.lstrip("\n"))
    return "%s\n%s\n%s\n\n%s" % (marker, body, END, text)


def _agent_name(path):
    try:
        m = re.match(r"---\r?\n(.*?)\r?\n---", _read(path), re.S)
    except (OSError, UnicodeDecodeError):
        return None
    n = m and re.search(r"^name: *(\S+) *$", m.group(1), re.M)
    return n.group(1) if n else None


def plan(proj, root):
    """(쓸 파일 목록 [(경로, 내용, 새로 만드는가)], 남은 사본 줄). 아무것도 쓰지 않는다."""
    setup = os.path.join(root, "setup")
    assets = {n: os.path.join(setup, n) for n in ("CLAUDE-새프로젝트.md", "_템플릿.md", "standard.md")}
    gone = [p for p in assets.values() if not os.path.isfile(p)]
    if gone:
        raise SetupError("셋팅 자산이 없다(플러그인 안에서 부른 것이 맞는지 본다): %s" % ", ".join(gone))
    standard = _read(assets["standard.md"])
    writes = []

    def put(rel, content, old):
        if content != old:
            writes.append((rel, content, old is None))

    path = os.path.join(proj, "CLAUDE.md")
    old = _read(path) if os.path.isfile(path) else None
    put("CLAUDE.md", _block(_read(assets["CLAUDE-새프로젝트.md"]) if old is None else old, standard), old)

    if not os.path.exists(os.path.join(proj, "stop-loop.config.json")):
        put("stop-loop.config.json", json.dumps(CONFIG, indent=2, ensure_ascii=False) + "\n", None)
    if not os.path.exists(os.path.join(proj, "docs", "backlog", "_템플릿.md")):
        put("docs/backlog/_템플릿.md", _read(assets["_템플릿.md"]), None)

    spath = os.path.join(proj, ".claude", "settings.json")
    old = None
    if os.path.exists(spath):
        try:
            old = json.loads(_read(spath))
            if not isinstance(old, dict):
                raise ValueError("객체가 아니다")
        except (ValueError, OSError) as e:
            raise SetupError("읽을 수 없다: .claude/settings.json (%s)" % e)
    new = json.loads(json.dumps(old or {}))
    new.setdefault("extraKnownMarketplaces", {})["stop-loop"] = {
        "source": {"source": "github", "repo": MARKETPLACE_REPO}}
    new.setdefault("enabledPlugins", {})[PLUGIN_ID] = True
    if new != old:
        writes.append((".claude/settings.json", json.dumps(new, indent=2, ensure_ascii=False) + "\n",
                       old is None))

    mine = {f[:-3] for f in os.listdir(os.path.join(root, "agents")) if f.endswith(".md")} \
        if os.path.isdir(os.path.join(root, "agents")) else set()
    copies = []
    adir = os.path.join(proj, ".claude", "agents")
    for f in sorted(os.listdir(adir)) if os.path.isdir(adir) else []:
        name = _agent_name(os.path.join(adir, f)) if f.endswith(".md") else None
        if name in mine:
            copies.append("남은 사본 .claude/agents/%s (%s) — 프로젝트 쪽이 먼저 읽혀 플러그인 새 판이 무시된다. "
                          "지울지는 사람이 정한다" % (f, name))
    return writes, copies


def register(proj):
    """목록 파일에 이 프로젝트를 더할 새 내용(없으면 None — 이미 있다). 아무것도 쓰지 않는다."""
    data = {"projects": []}
    if os.path.exists(PROJECTS_JSON):
        try:
            data = json.loads(_read(PROJECTS_JSON))
            if not isinstance(data.get("projects"), list):
                raise ValueError("projects 배열이 없다")
            for p in data["projects"]:
                p["name"], p["path"]
        except (ValueError, OSError, AttributeError, KeyError, TypeError) as e:
            raise SetupError("읽을 수 없다: %s (%s)" % (PROJECTS_JSON, e))
    name = os.path.basename(proj)
    for p in data["projects"]:
        if os.path.abspath(os.path.expanduser(p["path"])) == proj:
            return None
        if p["name"] == name:
            raise SetupError("같은 이름 %s 의 다른 경로가 이미 등록돼 있다: %s" % (name, p["path"]))
    data["projects"].append({"name": name, "path": proj})
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main(argv, plugin_root=None):
    if len(argv) > 1:
        print(USAGE, file=sys.stderr)
        return 2
    proj = os.path.abspath(argv[0] if argv else ".")
    root = plugin_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        if not os.path.isdir(proj):
            raise SetupError("폴더가 아니다: %s" % proj)
        writes, copies = plan(proj, root)
        listed = register(proj)
    except SetupError as e:
        print("실패: %s" % e, file=sys.stderr)
        return 1
    for rel, content, _ in writes:                      # 읽기·판정을 끝낸 뒤에만 쓴다
        path = os.path.join(proj, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
    if listed is not None:
        os.makedirs(os.path.dirname(PROJECTS_JSON), exist_ok=True)
        with open(PROJECTS_JSON, "w", encoding="utf-8", newline="") as fh:
            fh.write(listed)
    for rel, _, created in writes:
        print("%s %s" % ("생성" if created else "갱신", rel))
    if listed is not None:
        print("등록 ~/.stop-loop/projects.json (%s)" % os.path.basename(proj))
    if not writes and listed is None:
        print("바꾼 파일 없음")
    for line in copies:
        print(line)
    print("커밋하지 않았다 — git status 로 확인한다")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
