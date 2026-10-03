#!/usr/bin/env python3
"""작업목록 정리 — 병합된 것을 완료로 옮기고 다 쓴 작업 폴더·가지를 치운다.

  python3 bin/backlog.py 정리 [프로젝트 경로]        무엇을 할지 보여준다
  python3 bin/backlog.py 정리 [프로젝트 경로] --실행   실제로 고치고 지운다
  python3 bin/backlog.py 가져오기 [hq 경로]          플러그인 저장소의 열린 이슈를 아이디어 카드로 옮긴다

판단은 없다. 「가지가 기준 가지에 들어갔는가」는 git 이 예·아니오로 답한다
(`git merge-base --is-ancestor`). 그래서 스킬이 아니라 스크립트다 — 세션도
토큰도 쓰지 않고, 같은 입력에 늘 같은 결과를 낸다.

병합할지 말지는 사람이 정한다. 이 도구는 **정해진 사실을 파일에 옮길 뿐이다.**
"""
import datetime as dt
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board  # noqa: E402  — 머리말 읽기는 보드와 같은 것을 쓴다
import endreport  # noqa: E402  — 정리 뒤 끝 보고를 띄운다
import report_workflow as rw  # noqa: E402  — `gh` 호출·열린 이슈 목록·계정 토큰은 한 자리다


def git(project, *args):
    return subprocess.run(("git",) + args, cwd=project,
                          capture_output=True, text=True)


def base_branch(project):
    """기준 가지 — develop · main · master 순. 러너의 `base_branch` 와 같은 순서다."""
    heads = git(project, "branch", "--format=%(refname:short)").stdout.split()
    return next((b for b in ("develop", "main", "master") if b in heads), None)


def fetch_base(project, base):
    """원격 기준 가지를 받아 온다 — 러너 자동 병합은 GitHub 에서 일어나 로컬 기준 가지가 안 움직인다.
    원격이 없거나 실패하면 조용히 넘어간다(로컬 기준 가지만 본다)."""
    if not git(project, "remote", "get-url", "origin").stdout.strip():
        return
    try:
        subprocess.run(("git", "fetch", "-q", "origin", base), cwd=project, capture_output=True,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def merged(project, branch, base):
    """가지가 기준 가지(로컬 또는 `origin/<기준 가지>`)에 들어갔는가. 가지가 없으면 들어간 것으로 본다(이미 치웠다)."""
    if git(project, "rev-parse", "--verify", "-q", branch).returncode:
        return True
    return any(git(project, "merge-base", "--is-ancestor", branch, b).returncode == 0
               for b in (base, "origin/" + base))


def leftovers(project, issue_id):
    """이 이슈가 남긴 작업 폴더와 가지. 없으면 빈 값."""
    worktree = "%s.%s" % (project.rstrip("/"), issue_id)
    branch = "loop/%s" % issue_id
    has_wt = os.path.isdir(worktree)
    has_br = git(project, "rev-parse", "--verify", "-q", branch).returncode == 0
    return (worktree if has_wt else None), (branch if has_br else None)


def set_status(path, value):
    """머리말의 `상태:` 줄만 바꾼다. 나머지는 손대지 않는다."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines(True)
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            break
        if line.split(":", 1)[0].strip() == "상태":
            lines[i] = "상태: %s\n" % value
            break
    else:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return True


def tidy(project, 실행=False):
    """검토 상태의 카드 가운데 병합된 것을 정리한다. 한 일을 줄 목록으로 돌려준다."""
    backlog = os.path.join(project, "docs", "backlog")
    base = base_branch(project)
    if not os.path.isdir(backlog):
        return ["작업목록 폴더가 없다: %s" % backlog]
    if base is None:
        return ["기준 가지를 찾지 못했다 (develop·main·master)"]

    done = []
    fetch_base(project, base)
    for card in board.read_cards(backlog):
        if card["상태"] != "검토":
            continue
        issue_id = os.path.splitext(card["file"])[0]
        branch = "loop/%s" % issue_id
        if not merged(project, branch, base):
            done.append("%s — 아직 %s 에 병합되지 않았다" % (card["title"], base))
            continue
        path = os.path.join(backlog, card["file"])
        worktree, live_branch = leftovers(project, issue_id)
        done.append("%s — 완료로 옮긴다%s%s"
                    % (card["title"],
                       ", 작업 폴더 삭제" if worktree else "",
                       ", 가지 삭제" if live_branch else ""))
        if not 실행:
            continue
        set_status(path, "완료")
        if worktree:
            git(project, "worktree", "remove", "--force", worktree)
        if live_branch:
            git(project, "branch", "-D", live_branch)
    return done or ["검토 상태인 카드가 없다"]


def pull_issues(hq):
    """플러그인 저장소의 열린 이슈 중 아직 카드에 없는 것을 아이디어 카드로 만든다. (종료 코드, 줄 목록)."""
    backlog = os.path.join(hq, "docs", "backlog")
    if not os.path.isdir(backlog):
        return 1, ["작업목록 폴더가 없다: %s" % backlog]
    account, token = rw.account_token(hq)
    if account and not token:
        return 1, ["GitHub 계정 %s 의 토큰을 얻지 못했다" % account]
    try:
        issues = rw.open_issues(hq, token, "number,title,url,body")
    except RuntimeError as e:
        return 1, [str(e)]
    if not issues:
        return 0, ["열린 이슈가 없다"]
    cards = {}
    for f in sorted(os.listdir(backlog)):
        if f.endswith(".md"):
            with open(os.path.join(backlog, f), encoding="utf-8") as fh:
                cards[f] = fh.read()
    lines, today = [], dt.date.today().isoformat()
    for it in sorted(issues, key=lambda i: i["number"]):
        url = it["url"]
        have = next((f for f, t in cards.items() if re.search(re.escape(url) + r"(?!\d)", t)), None)
        if have:
            lines.append("건너뜀 #%d — 이미 %s" % (it["number"], have))
            continue
        name = "ISSUE-stop-loop-%d-%s.md" % (it["number"], today)
        text = ("---\n상태: 아이디어\n크기: \n갱신: %s\n---\n# %s\n\n## 배경\n\n원본: %s\n\n%s\n"
                % (today, it["title"], url, (it.get("body") or "").strip()))
        with open(os.path.join(backlog, name), "w", encoding="utf-8") as fh:
            fh.write(text)
        cards[name] = text
        lines.append("만듦 %s ← %s" % (name, url))
    return 0, lines


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    실행 = "--실행" in argv
    if args and args[0] == "가져오기":
        rc, lines = pull_issues(os.path.abspath(os.path.expanduser(args[1] if len(args) > 1 else ".")))
        for line in lines:
            print("  " + line)
        return rc
    if not args or args[0] != "정리":
        print("\n".join(__doc__.strip().splitlines()[2:5]))
        return 1
    project = os.path.abspath(os.path.expanduser(args[1] if len(args) > 1 else "."))
    for line in tidy(project, 실행):
        print(("  " if 실행 else "  (미리보기) ") + line)
    if 실행:
        endreport.auto(project)
    if not 실행:
        print("\n실제로 고치려면 --실행 을 붙인다.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
