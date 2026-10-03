#!/usr/bin/env python3
"""PR 본문의 워크플로우 몫을 회사 정보 검사 뒤 공개 저장소 이슈로 올린다.

  python3 report_workflow.py [프로젝트=.]            올릴 문장을 보여 준다 (아무것도 올리지 않는다)
  python3 report_workflow.py [프로젝트=.] --올리기    사람이 확인한 뒤 — 깨끗한 줄만 올린다

플러그인 안(`scripts/`)에서만 돈다. 판단은 여기서 하고 스킬은 부르고 옮길 뿐이다.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner  # noqa: E402  — 계정 키 읽기·토큰 얻기는 러너의 것을 그대로 쓴다

REPO = "dawn-island/stop-loop"      # bin/release_plugin.py 의 NAME·OWNER 와 같은 값 (그 파일은 판에 담기지 않는다)
LIMIT = 1000
SECTION = "## 워크플로우 몫"
CELLS = ("대상", "증상", "횟수", "제안", "판")
PATHLIKE = re.compile(r"[\w.\-]+(?:/[\w.\-]+)+|[\w\-]+\.(?:py|md|json|sh|ya?ml|txt|ts|tsx|js|jsx)\b")
PREFIX = "- 워크플로우 몫 이슈: "


def gh(cwd, token, *args):
    """`gh` 를 부르는 유일한 자리 — 시험이 바꿔 끼운다. 토큰이 있으면 `GH_TOKEN` 에 넣는다."""
    env = dict(os.environ, GH_TOKEN=token) if token else None
    try:
        return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, env=env)
    except OSError as e:
        return subprocess.CompletedProcess(["gh", *args], 127, "", str(e))


def _err(res):
    lines = (res.stderr or res.stdout or "").strip().splitlines()
    return lines[-1] if lines else "알 수 없는 오류"


def open_issues(cwd, token, fields):
    """`REPO` 의 열린 이슈 목록 — 유일한 자리. 실패하거나 상한에 닿으면 RuntimeError."""
    res = gh(cwd, token, "issue", "list", "-R", REPO, "--state", "open",
             "--limit", str(LIMIT), "--json", fields)
    if res.returncode:
        raise RuntimeError("열린 이슈 목록을 읽지 못했다: " + _err(res))
    items = json.loads(res.stdout or "[]")
    if len(items) >= LIMIT:
        raise RuntimeError("열린 이슈가 %d 개 이상이라 같은 이슈를 다 찾을 수 없다 — "
                           "사람이 닫은 뒤 다시 돌린다" % LIMIT)
    return items


def account_token(project):
    """(계정 또는 None, 토큰 또는 None)."""
    account = runner.load_models(project).get("_githubAccount")
    return account, (runner.gh_token(account) if account else None)


def rows(body):
    """「## 워크플로우 몫」 절의 표 자료 줄을 다섯 칸으로 읽는다."""
    section, on = [], False
    for line in (body or "").splitlines():
        if line.rstrip() == SECTION:
            on = True
        elif line.startswith("## "):
            on = False
        elif on and line.lstrip().startswith("|"):
            section.append(line)
    out = []
    for line in section[2:]:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == len(CELLS):
            out.append(dict(zip(CELLS, cells)))
    return out


def _terms(text):
    """CLAUDE.md 「용어」 절의 낱말. 절이 없으면 None."""
    sect, on = None, False
    for line in text.splitlines():
        if re.match(r"##\s*(?:\d+\.\s*)?용어\b", line):
            on, sect = True, []
        elif line.startswith("## "):
            on = False
        elif on:
            sect.append(line.rstrip())
    if sect is None:
        return None
    words = []
    for i, line in enumerate(sect):
        s = line.strip()
        if s.startswith("|"):
            nxt = sect[i + 1].strip() if i + 1 < len(sect) else ""
            if set(s) <= set("|-: ") or re.fullmatch(r"[|\-: ]+", nxt):   # 구분 줄·머리 줄
                continue
            word = s.strip("|").split("|")[0]
        elif s[:1] in "-*":
            word = re.split(r"\s[—-]\s|:", s[1:].strip(), 1)[0]
        else:
            continue
        word = word.strip().strip("`*")
        if word:
            words.append(word)
    return words


def company_words(project):
    """([(낱말, 갈래)], [알림]) — 원격을 못 읽으면 RuntimeError."""
    res = subprocess.run(["git", "remote", "get-url", "origin"], cwd=project,
                         capture_output=True, text=True)
    m = re.search(r"[:/]([^/:\s]+)/([^/\s]+?)(?:\.git)?/?$", res.stdout.strip()) \
        if res.returncode == 0 else None
    if not m:
        raise RuntimeError("origin 원격 주소에서 소유자·저장소 이름을 읽지 못했다")
    words, notes = [(m.group(1), "가"), (m.group(2), "가")], []
    try:
        with open(os.path.join(project, "CLAUDE.md"), encoding="utf-8") as fh:
            terms = _terms(fh.read())
    except OSError:
        terms = None
    if terms is None:
        notes.append("용어 절 없음 — 그 갈래는 건너뛰었다")
    else:
        words += [(w, "나") for w in terms]
    return words, notes


def plugin_names(plugin_root):
    """플러그인 안 파일의 끝 이름 집합 — 경로 갈래의 허용 목록."""
    return {f for _, _, fs in os.walk(plugin_root) for f in fs}


def check(row, words, names):
    """[(칸, 낱말, 갈래)] — 빈 목록이면 깨끗하다."""
    hits = []
    for cell, text in row.items():
        for w, kind in words:
            m = re.search(r"(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])" % re.escape(w), text, re.I)
            if m:
                hits.append((cell, m.group(0), kind))
        for m in PATHLIKE.finditer(text):
            if os.path.basename(m.group(0)) not in names:
                hits.append((cell, m.group(0), "다"))
    return hits


def _texts(row, existing, version):
    title = "%s — %s" % (row["대상"], row["증상"])
    detail = "- 대상 파일: %s\n- 증상: %s\n- 되풀이 횟수: %s\n- 제안: %s\n- 판: %s" % (
        row["대상"], row["증상"], row["횟수"], row["제안"], row["판"])
    if existing:
        return title, "다시 나옴 · 판 %s\n\n%s" % (version, detail)
    return title, detail


def _note_card(project, url):
    """지금 가지 `loop/<카드>` 의 카드 끝에 주소 한 줄. 커밋하지 않는다."""
    br = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=project,
                        capture_output=True, text=True).stdout.strip()
    name = br[len("loop/"):] if br.startswith("loop/") else None
    path = os.path.join(project, "docs", "backlog", "%s.md" % (name or br))
    if not name or not os.path.isfile(path):
        print("카드가 없어 주소를 적지 않았다: %s" % path)
        return
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if url in text:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(("" if text.endswith("\n") else "\n") + PREFIX + url + "\n")


def main(argv, plugin_root=None):
    flags = [a for a in argv if a.startswith("--")]
    args = [a for a in argv if not a.startswith("--")]
    post = "--올리기" in flags
    project = os.path.abspath(os.path.expanduser(args[0] if args else "."))
    root = plugin_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            version = json.load(fh)["version"]
    except (OSError, ValueError, KeyError):
        print("플러그인 안에서만 돈다 — .claude-plugin/plugin.json 을 읽지 못했다: %s" % root)
        return 1
    account, token = account_token(project)
    if account and not token:
        print("GitHub 계정 %s 의 토큰을 얻지 못했다" % account)
        return 1
    try:
        words, notes = company_words(project)
    except RuntimeError as e:
        print("올리지 않는다 — %s" % e)
        return 1
    res = gh(project, token, "pr", "view", "--json", "body")
    if res.returncode:
        print("PR 을 읽지 못했다: %s" % _err(res))
        return 1
    found = rows(json.loads(res.stdout or "{}").get("body"))
    if not found:
        print("올릴 것이 없다 — PR 본문에 「%s」 표의 자료 줄이 없다" % SECTION)
        return 0
    for n in notes:
        print(n)
    names, clean, rc = plugin_names(root), [], 0
    for row in found:
        hits = check(row, words, names)
        for cell, word, kind in hits:
            print("올리지 않는다 — %s: %s (%s)" % (cell, word, kind))
        if not hits:
            clean.append(row)
    if not clean:
        print("올릴 문장이 없다")
        return 0
    try:
        opened = {i["title"]: i for i in open_issues(project, token, "number,title,url")}
    except RuntimeError as e:
        print(e)
        return 1
    for row in clean:
        old = opened.get("%s — %s" % (row["대상"], row["증상"]))
        title, body = _texts(row, old, version)
        if not post:
            print("%s\n제목: %s\n%s\n" % ("댓글을 단다 #%d" % old["number"] if old else "올릴 문장",
                                         title, body))
            continue
        if old:
            res = gh(project, token, "issue", "comment", str(old["number"]), "-R", REPO,
                     "--body", body)
        else:
            res = gh(project, token, "issue", "create", "-R", REPO, "--title", title,
                     "--body", body)
        if res.returncode:
            print("올리지 못했다 — %s" % _err(res))
            rc = 1
            continue
        url = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
        print("올렸다: %s" % url)
        if not old:
            _note_card(project, url)
    if not post:
        print("확인하려면 --올리기 를 붙인다")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
