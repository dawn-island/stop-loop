#!/usr/bin/env python3
"""소각 커밋(Loop-Stage: burn) 이력에서 카드별·단계별 시간·비용을 모아 찍는다. 읽기 전용.

  python3 bin/runcost.py <프로젝트 저장소 경로>

소각이 지운 `.workflow/<실행>/state.json` 을 부모 커밋에서 읽는다. 못 읽은 카드는 빼지 않고
「state.json 못 찾음」으로 찍는다. 있는 기록을 그대로 더하고 보정하지 않는다.
"""
import fnmatch
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


def _fmt(tot):
    return " ".join("%s=%s" % (n, ("%.4f" % tot[c]) if c == "cost" else int(tot[c]))
                    for n, c in zip(SHORT, COLS))


def render(rows):
    """FN-03 — rows=[(카드, metrics 또는 None, 까닭)] → 여러 줄 문자열."""
    lines, grand, missing = [], dict.fromkeys(COLS, 0), 0
    for card, metrics, why in rows:
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
    lines.append("전체 합계  %s" % _fmt(grand))
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
    rows = []
    for sha, card in commits:
        m, why = read_state(argv[0], sha)
        rows.append((card, m, why))
    print(render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
