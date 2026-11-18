#!/usr/bin/env python3
"""소각 커밋(Loop-Stage: burn) 이력에서 카드별·단계별 시간·비용을 모아 찍는다. 읽기 전용.

  python3 bin/runcost.py <프로젝트 저장소 경로>

소각이 지운 `.workflow/<실행>/state.json` 을 부모 커밋에서 읽는다. 못 읽은 카드는 빼지 않고
「state.json 못 찾음」으로 찍는다. 있는 기록을 그대로 더하고 보정하지 않는다.
"""
import fnmatch
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger  # noqa: E402
import runner  # noqa: E402

COLS = ("runs", "ms", "cost") + tuple(runner.TOKEN_KEYS)
SHORT = ("runs", "ms", "cost", "in", "cc", "cr", "out")


def burn_commits(repo):
    """FN-01 — [(해시, 카드)], 오래된 것부터. 같은 카드가 두 번이면 두 건."""
    fmt = "\x1e%H\x1f%(trailers:key=Loop-Issue,valueonly)\x1f%(trailers:key=Loop-Stage,valueonly)"
    r = ledger._git(repo, "log", "--all", "--topo-order", "--reverse", "--format=" + fmt)
    if r.returncode != 0:
        raise RuntimeError((r.stderr.strip().splitlines() or ["git log 실패"])[0])
    out = []
    for rec in r.stdout.split("\x1e")[1:]:
        p = rec.split("\x1f")
        if len(p) == 3 and p[2].strip() == "burn":
            out.append((p[0].strip(), p[1].strip()))
    return out


def read_state(repo, sha):
    """FN-02 — (metrics, None) 또는 (None, 까닭). 예외로 죽지 않는다."""
    r = ledger._git(repo, "diff-tree", "--no-commit-id", "-r", "--diff-filter=D", "--name-only", sha)
    paths = [x for x in r.stdout.splitlines() if fnmatch.fnmatch(x, ".workflow/*/state.json")]
    if not paths:
        return None, "소각 커밋이 지운 파일에 state.json 없음"
    s = ledger._git(repo, "show", "%s^:%s" % (sha, paths[0]))
    if s.returncode != 0:
        return None, "부모 커밋에 %s 없음" % paths[0]
    try:
        metrics = json.loads(s.stdout).get("metrics") or {}
    except (ValueError, AttributeError):
        return None, "state.json 을 읽지 못함(JSON 깨짐)"
    return metrics, None


def read_log(repo, sha):
    """(log, None) 또는 (None, 까닭). log 는 세 목록 — 칸이 없는 옛 기록이면 그 값이 None(기록 없음)."""
    r = ledger._git(repo, "diff-tree", "--no-commit-id", "-r", "--diff-filter=D", "--name-only", sha)
    paths = [x for x in r.stdout.splitlines() if fnmatch.fnmatch(x, ".workflow/*/state.json")]
    if not paths:
        return None, "소각 커밋이 지운 파일에 state.json 없음"
    s = ledger._git(repo, "show", "%s^:%s" % (sha, paths[0]))
    if s.returncode != 0:
        return None, "부모 커밋에 %s 없음" % paths[0]
    try:
        d = json.loads(s.stdout)
        return {k: d.get(k) if isinstance(d.get(k), list) else None
                for k in ("gateBlocks", "stops", "resumes")}, None
    except (ValueError, AttributeError):
        return None, "state.json 을 읽지 못함(JSON 깨짐)"


def base_ref(repo):
    """기준 가지 — 러너 규칙(develop, main, master 순)으로 고르고 `origin/<이름>` 이 있으면 그것. 없으면 None."""
    r = ledger._git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes/origin")
    refs = set(r.stdout.split())
    name = runner.base_branch({x[len("origin/"):] if x.startswith("origin/") else x for x in refs})
    if name is None:
        return None
    return "origin/" + name if "origin/" + name in refs else name


def find_merge(repo, burn_sha, base):
    """소각에서 기준 가지까지 이어지는 가장 오래된 병합 커밋 → (해시, 초 단위 시각) 또는 None."""
    r = ledger._git(repo, "log", "--merges", "--ancestry-path", "--reverse", "--format=%H %ct",
                    "%s..%s" % (burn_sha, base))
    first = (r.stdout.splitlines() or [""])[0].split()
    return (first[0], int(first[1])) if r.returncode == 0 and len(first) == 2 else None


def find_revert(repo, merge_sha, base):
    """기준 가지에서 `This reverts commit <병합>` 을 적은 커밋의 해시 또는 None."""
    r = ledger._git(repo, "log", base, "--fixed-strings", "--grep", "This reverts commit %s" % merge_sha,
                    "-n", "1", "--format=%H")
    return r.stdout.strip() or None if r.returncode == 0 else None


def _at(x):
    try:
        return dt.datetime.strptime(x.get("at"), "%Y-%m-%d %H:%M")
    except (ValueError, TypeError, AttributeError):
        return None


def stop_wait(log):
    """(멈춤 대기 분, 멈춤 건수, 짝 없음 건수) 또는 None(기록 없음). 번호가 아니라 시각으로 짝짓는다."""
    if not log or log.get("stops") is None or log.get("resumes") is None:
        return None
    resumes = sorted(t for t in map(_at, log["resumes"]) if t)
    stops = log["stops"]
    good = sorted(t for t in map(_at, stops) if t)
    minutes, unpaired = 0, len(stops) - len(good)
    for t in good:
        k = next((i for i, r in enumerate(resumes) if r >= t), None)
        if k is None:
            unpaired += 1
        else:
            minutes += int((resumes.pop(k) - t).total_seconds() // 60)
    return minutes, len(stops), unpaired


def wait_info(repo, burn_sha, burn_ts, log):
    base = base_ref(repo)
    merge = find_merge(repo, burn_sha, base) if base else None
    return {"merge": merge, "base": base,
            "revert": find_revert(repo, merge[0], base) if merge else None,
            "stop": stop_wait(log),
            "minutes": max(0, (merge[1] - burn_ts) // 60) if merge else None}


def _dur(m):
    return "%d분" % m if m < 60 else "%d시간 %d분" % (m // 60, m % 60)


def _wait_lines(w):
    if w == "합침":
        return ["  병합 대기·멈춤 대기  마지막 소각 줄에 합침"]
    if w["base"] is None:
        out = ["  병합 대기  기준 가지 못 찾음"]
    elif w["merge"] is None:
        out = ["  병합 대기  미병합 (스쿼시·리베이스면 실제로 병합됐어도 이렇게 찍힘)"]
    else:
        out = ["  병합 대기  %s (병합 %s)" % (_dur(w["minutes"]), w["merge"][0][:7])]
    st = w["stop"]
    out.append("  멈춤 대기  기록 없음" if st is None
               else "  멈춤 대기  %s (멈춤 %d건, 짝 없음 %d건)" % (_dur(st[0]), st[1], st[2]))
    if w["merge"]:
        out.append("  되돌림  " + (w["revert"][:7] if w["revert"] else "없음"))
    return out


def _count(items, key):
    """줄 목록 → 「종류=횟수 …」(처음 나온 순서). 빈 목록은 「없음」."""
    c = {}
    for x in items:
        k = x.get(key, "?") if isinstance(x, dict) else "?"
        c[k] = c.get(k, 0) + 1
    return " ".join("%s=%d" % kv for kv in c.items()) or "없음"


def _fmt(tot):
    return " ".join("%s=%s" % (n, ("%.4f" % tot[c]) if c == "cost" else int(tot[c]))
                    for n, c in zip(SHORT, COLS))


def render(rows, logs=None, waits=None):
    """FN-03 — rows=[(카드, metrics 또는 None, 까닭)] → 여러 줄 문자열.
    logs 는 rows 와 같은 길이의 `read_log` 결과(없으면 None) — 주면 막힘·멈춤·이어받기 줄을 더한다.
    waits 는 rows 와 같은 길이의 `wait_info` 결과·「합침」·None — 주면 병합 대기·멈춤 대기·되돌림 줄을 더한다."""
    lines, grand, missing = [], dict.fromkeys(COLS, 0), 0
    allb, alls, allr, nolog = [], [], 0, 0
    for i, (card, metrics, why) in enumerate(rows):
        if metrics is None:
            missing += 1
            lines.append("%s  state.json 못 찾음 (%s)" % (card, why))
            continue
        lines.append(card)
        sub = dict.fromkeys(COLS, 0)
        for st in sorted(metrics):
            lines.append("  %s  %s" % (st, _fmt({c: metrics[st].get(c, 0) for c in COLS})))
            for c in COLS:
                sub[c] += metrics[st].get(c, 0)
        lines.append("  소계  %s" % (_fmt(sub) if metrics else "단계 기록 없음 " + _fmt(sub)))
        for c in COLS:
            grand[c] += sub[c]
        if waits is not None and waits[i]:
            lines += _wait_lines(waits[i])
        if logs is not None:
            log = logs[i]
            if not log or any(log.get(k) is None for k in ("gateBlocks", "stops", "resumes")):
                nolog += 1
                lines.append("  관문 막힘·멈춤·이어받기  기록 없음")
                continue
            lines.append("  관문 막힘  " + _count(log["gateBlocks"], "kind"))
            lines.append("  멈춤  " + _count(log["stops"], "kind"))
            for s in log["stops"]:
                lines.append("    %s %s %s" % (s.get("stage"), s.get("kind"), s.get("reason")))
            lines.append("  이어받기 %d회" % len(log["resumes"]))
            allb += log["gateBlocks"]
            alls += log["stops"]
            allr += len(log["resumes"])
    lines.append("전체 합계  %s" % _fmt(grand))
    if logs is not None:
        lines.append("전체 관문 막힘  " + _count(allb, "kind"))
        lines.append("전체 멈춤  " + _count(alls, "kind"))
        lines.append("전체 이어받기 %d회" % allr)
        lines.append("기록 없음 %d건" % nolog)
    lines.append("못 찾음 %d건" % missing)
    return "\n".join(lines)


def main(argv):
    """FN-04 — 종료 코드 0(성공·소각 없음) 또는 2(인자·저장소 오류)."""
    if len(argv) != 1:
        print("사용: python3 bin/runcost.py <프로젝트 저장소 경로>")
        return 2
    if not ledger._is_git_repo(argv[0]):
        print("git 저장소가 아님: %s" % argv[0])
        return 2
    try:
        commits = burn_commits(argv[0])
    except RuntimeError as e:
        print("git log 실패: %s" % e)
        return 2
    if not commits:
        print("소각 커밋 없음")
        return 0
    rows, logs, waits = [], [], []
    last = {card: i for i, (_, card) in enumerate(commits)}
    for i, (sha, card) in enumerate(commits):
        m, why = read_state(argv[0], sha)
        rows.append((card, m, why))
        log = read_log(argv[0], sha)[0]
        logs.append(log)
        if last[card] != i:
            waits.append("합침")
        else:
            ts = ledger._git(argv[0], "show", "-s", "--format=%ct", sha).stdout.strip()
            waits.append(wait_info(argv[0], sha, int(ts), log) if ts.isdigit() else None)
    print(render(rows, logs, waits))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
