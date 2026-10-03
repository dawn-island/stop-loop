#!/usr/bin/env python3
"""ledger — 실행을 가로지르는 지적을 모아 보는 도구.

STOP-LOOP 은 한 번의 실행 안에서 같은 지적이 되풀이되면 멈춘다
(`bin/runner.py` 의 `round_decision`). 그러나 실행을 가로질러 같은 지적이
몇 주 뒤 다른 이슈에서 또 나오는 것은 아무도 세지 않는다. 이 도구는 저장소
하나 이상의 git 이력에서 판정 파일(`NN-verdict.json`)을 모아, 같은 지적이
서로 다른 이슈 몇 개에서 나왔는지 표 하나로 보여준다. 승격(규칙 파일에 한
줄 적는 것)은 사람이 한다 — 이 도구는 하지 않는다.

  python3 bin/ledger.py <저장소> [저장소…]

설계: .workflow/ISSUE-ledger-2026-09-09-2026-09-09/05-design.md
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner  # noqa: E402 — 열쇠(finding_key)는 러너 것을 그대로 쓴다. 복제하지 않는다.

VERDICT_RE = re.compile(r"^\d{2}-verdict\.json$")
DATE_SUFFIX_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(repo)] + list(args),
        capture_output=True, text=True, encoding="utf-8")


def _is_git_repo(path):
    if not path or not os.path.isdir(path):
        return False
    return _git(path, "rev-parse", "--git-dir").returncode == 0


def _added_verdict_files(repo):
    """(커밋 해시, 커밋 시각, 경로) — 판정 파일을 **추가한** 커밋마다.

    소각이 작업 폴더에서 지운 파일도 여기 나온다 — 마지막 커밋의 트리가 아니라
    이력 전체에서 「추가」로 잡힌 커밋을 본다.
    """
    r = _git(repo, "log", "--diff-filter=A", "--name-status",
             "--pretty=format:@@%H %at")
    if r.returncode != 0:
        return []
    commit, when = None, None
    out = []
    for line in r.stdout.splitlines():
        if line.startswith("@@"):
            h, t = line[2:].split(" ", 1)
            commit, when = h, int(t)
        elif line.startswith("A\t"):
            path = line[2:]
            if VERDICT_RE.match(os.path.basename(path)):
                out.append((commit, when, path))
    return out


def _read_at_commit(repo, commit, path):
    r = _git(repo, "show", "%s:%s" % (commit, path))
    return r.stdout if r.returncode == 0 else None


def _findings(text):
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        return []
    if not isinstance(d, dict):
        return []
    return [f for f in d.get("findings", []) if isinstance(f, dict)]


def _issue_name(path):
    """판정 파일 경로의 실행 폴더 이름에서 실행 날짜를 뗀다.

    실행 폴더는 `이슈이름-오늘날짜` 형태다(`bin/runner.py` 의 `run_dir`).
    끝에 `-YYYY-MM-DD` 가 있으면 그 하나만 떼고, 없으면 그대로 쓴다.
    """
    run_dir = os.path.basename(os.path.dirname(path))
    return DATE_SUFFIX_RE.sub("", run_dir)


def _harvest(repo):
    """이 저장소의 (커밋 시각, 경로, 이슈 이름, 지적) 전부."""
    records = []
    for commit, when, path in _added_verdict_files(repo):
        text = _read_at_commit(repo, commit, path)
        if text is None:
            continue
        issue = _issue_name(path)
        for finding in _findings(text):
            records.append((when, path, issue, finding))
    return records


def aggregate(repo_records):
    """[(저장소이름, records)] → 열쇠별 집계 행 목록.

    횟수는 지적이 나온 횟수가 아니라 **서로 다른 (저장소, 이슈) 수**다.
    한 이슈 안에서 같은 지적이 여러 번 나와도(반려 뒤 재시도 등) 1로 접는다.
    """
    agg = {}
    for repo_index, (repo_name, records) in enumerate(repo_records):
        for when, path, issue, finding in records:
            key = runner.finding_key(finding)
            entry = agg.setdefault(key, {"issues": set(), "candidates": []})
            entry["issues"].add("%s/%s" % (repo_name, issue))
            what = " ".join(str(finding.get("what", "")).split())[:80]
            entry["candidates"].append((when, repo_index, path, what))

    rows = []
    for key, entry in agg.items():
        # 「무엇이 문제였나」는 가장 먼저 나온 것 — 커밋 시각, 그다음 저장소
        # 인자 순서, 그래도 같으면 파일 경로 오름차순 (05-design.md D6).
        first = min(entry["candidates"], key=lambda c: (c[0], c[1], c[2]))
        rows.append({
            "key": key,
            "count": len(entry["issues"]),
            "issues": sorted(entry["issues"]),
            "what": first[3],
        })
    rows.sort(key=lambda r: (-r["count"], r["key"]))
    return rows


def _render(headers, data):
    widths = [max([len(h)] + [len(row[i]) for row in data])
              for i, h in enumerate(headers)]

    def fmt(cells):
        return " | ".join(c.ljust(w) for c, w in zip(cells, widths))

    lines = [fmt(headers), "-+-".join("-" * w for w in widths)]
    lines += [fmt(row) for row in data]
    return "\n".join(lines)


def format_table(rows):
    headers = ["열쇠", "횟수", "나온 이슈들", "무엇이 문제였나"]
    data = [[r["key"], str(r["count"]), ", ".join(r["issues"]), r["what"]]
            for r in rows]
    return _render(headers, data)


NO_PLACE = "자리 없음"


def _file_of(finding):
    """지적의 `where` 에서 줄번호를 떼고 앞의 파일 부분만. `where` 가 없으면 「자리 없음」.

    열쇠(`runner.finding_key`)는 이 값을 쓰지 않는다 — 실행 안 재발 감지가 그 열쇠를 쓰므로
    이 묶음은 보여 주는 용도일 뿐이다.
    """
    where = " ".join(str(finding.get("where", "")).split())
    where = re.sub(r"[:#]L?\d+([-~]\d+)?", "", where)
    head = re.split(r"[:#\s]", where, maxsplit=1)[0]
    return head or NO_PLACE


def aggregate_files(repo_records):
    """[(저장소이름, records)] → 파일별 집계 행. 횟수는 서로 다른 (저장소, 이슈) 수다.

    ponytail: `where` 의 첫 낱말을 파일로 본다. 자유 서술 `where` 는 낱말째 묶인다 —
    파일 경로를 적게 하는 위임문(runner.delegate_text)이 이 근사를 받친다.
    """
    agg = {}
    for repo_name, records in repo_records:
        for when, path, issue, finding in records:
            entry = agg.setdefault(_file_of(finding), {"issues": set(), "kinds": set()})
            entry["issues"].add("%s/%s" % (repo_name, issue))
            entry["kinds"].add(str(finding.get("kind", "")))
    rows = [{"file": f, "count": len(e["issues"]), "kinds": ", ".join(sorted(e["kinds"]))}
            for f, e in agg.items()]
    rows.sort(key=lambda r: (-r["count"], r["file"]))
    return rows


def format_file_table(rows):
    return _render(["파일", "횟수", "지적 종류"],
                   [[r["file"], str(r["count"]), r["kinds"]] for r in rows])


def main(argv):
    if not argv:
        print("저장소를 하나 이상 지정한다.", file=sys.stderr)
        return 1

    invalid = [p for p in argv if not _is_git_repo(p)]
    if invalid:
        for p in invalid:
            print("git 저장소가 아니다: %s" % p, file=sys.stderr)
        return 1

    repo_records = [
        (os.path.basename(os.path.normpath(os.path.abspath(p))), _harvest(p))
        for p in argv
    ]
    rows = aggregate(repo_records)
    if not rows:
        print("반복을 셀 판정 파일이 없다.")
        return 0

    print(format_table(rows))
    print()
    print(format_file_table(aggregate_files(repo_records)))    # 같은 파일에서 반복되는 지적
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
