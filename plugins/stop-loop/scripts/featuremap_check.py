#!/usr/bin/env python3
"""기능맵(그룹 폴더 `docs/기능맵/*.md` 와 옛 한 장 `docs/기능맵.md`)이 코드와 맞는지 기계로 검사한다.

검사 넷: ID 가 겹치지 않는다 · 관련 기능이 표에 있는 ID 를 가리킨다 ·
상태가 「구현됨」인 줄의 코드·시험 위치 파일이 있다 · 소스 앞 10줄의 머리 주석
ID 가 표에 있고 그 줄의 코드 위치와 맞다.

  python3 featuremap_check.py [프로젝트 경로]   # 없음·통과 0, 어긋남 1

러너 07 관문이 `check(root)` 를 부른다.
"""
import os
import re
import subprocess
import sys

MAP = os.path.join("docs", "기능맵.md")     # bin/runner.py 의 FEATURE_MAP 과 같다
SHOWN = "docs/기능맵.md"
MAP_DIR = "docs/기능맵"                      # 그룹 폴더 — 파일 하나가 기능 그룹 하나
HEADER_LINES = 10
_ID = re.compile(r"F-\d{3}")
_REL = re.compile(r"[^\s,:|]+:(F-\d{3})")
_NOTE = re.compile(r"^\s*(?:#|//|--|;|/\*|\*|<!--)\s*기능:\s*(F-\d{3})\b")


def rows(text):
    """코드 울타리 밖, 첫 칸이 F-ddd 이고 칸이 여덟인 표 줄 → [(줄번호, 칸 여덟)]."""
    out, fenced = [], False
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("```"):
            fenced = not fenced
        elif not fenced and s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) == 8 and _ID.fullmatch(cells[0]):
                out.append((n, cells))
    return out


def locations(root):
    """있는 기능맵 자리(뿌리 기준, `/` 구분) — 옛 파일(파일일 때만), 그룹 폴더(바로 아래 `.md` 파일이 있을 때만) 순."""
    out = []
    if os.path.isfile(os.path.join(root, MAP)):
        out.append(SHOWN)
    if map_files(root, MAP_DIR):
        out.append(MAP_DIR)
    return out


def map_files(root, only=None):
    """→ [(보이는 경로, 절대 경로)] — 옛 파일, 그다음 그룹 폴더 바로 아래 `.md` 파일 이름순."""
    out = []
    if only in (None, SHOWN) and os.path.isfile(os.path.join(root, MAP)):
        out.append((SHOWN, os.path.join(root, MAP)))
    folder = os.path.join(root, "docs", "기능맵")
    if only in (None, MAP_DIR) and os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if name.endswith(".md") and os.path.isfile(os.path.join(folder, name)):
                out.append(("%s/%s" % (MAP_DIR, name), os.path.join(folder, name)))
    return out


def _paths(cell):
    return [i.split(":")[0].strip() for i in cell.split(",") if i.split(":")[0].strip() not in ("", "—")]


def _tri(rs):
    """rs 원소는 (줄번호, 칸) 또는 (줄번호, 칸, 보이는 경로) — 셋째가 없으면 옛 자리로 본다."""
    return [(x[0], x[1], x[2] if len(x) > 2 else SHOWN) for x in rs]


def check_dupes(rs):
    first, out = {}, []
    for n, c, f in _tri(rs):
        if c[0] not in first:
            first[c[0]] = (n, f)
        elif first[c[0]][1] == f:
            out.append("%s:%d: ID %s 이 겹친다(처음 %d)" % (f, n, c[0], first[c[0]][0]))
        else:
            out.append("%s:%d: ID %s 이 겹친다(처음 %s:%d)" % (f, n, c[0], first[c[0]][1], first[c[0]][0]))
    return out


def check_related(rs):
    ids = {c[0] for _, c, _ in _tri(rs)}
    return ["%s:%d: 관련 기능 %s — 기능맵에 없는 ID" % (f, n, m.group(0))
            for n, c, f in _tri(rs) for m in _REL.finditer(c[6]) if m.group(1) not in ids]


def check_locations(rs, root):
    out = []
    for n, c, f in _tri(rs):
        if c[2] != "구현됨":
            continue
        for label, cell in (("코드", c[3]), ("시험", c[4])):
            if cell.strip() == "—":
                out.append("%s:%d: %s 구현됨인데 %s 위치가 — 다" % (f, n, c[0], label))
            for p in _paths(cell):
                if not os.path.isfile(os.path.join(root, p)):
                    out.append("%s:%d: %s 구현됨인데 %s 위치 파일이 없다: %s"
                               % (f, n, c[0], label, p))
    return out


def _tracked(root):
    p = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True)
    return [f for f in p.stdout.decode("utf-8", "replace").split("\0") if f] if p.returncode == 0 else []


def check_headers(rs, root):
    where = {}
    for _, c, _ in _tri(rs):
        where.setdefault(c[0], []).extend(os.path.normpath(p) for p in _paths(c[3]))
    out = []
    for f in _tracked(root):
        if f.endswith(".md"):
            continue
        try:
            with open(os.path.join(root, f), encoding="utf-8") as fh:
                head = [next(fh, "") for _ in range(HEADER_LINES)]
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(head, 1):
            m = _NOTE.match(line)
            if not m:
                continue
            fid = m.group(1)
            if fid not in where:
                out.append("%s:%d: 머리 주석 기능: %s — 기능맵에 없는 ID" % (f, n, fid))
            elif os.path.normpath(f) not in where[fid]:
                out.append("%s:%d: 머리 주석 기능: %s — 기능맵 코드 위치(%s)와 다르다"
                           % (f, n, fid, ", ".join(where[fid]) or "—"))
    return out


def check(root):
    """기능맵이 없으면 None(건너뜀), 있으면 모든 파일을 넘는 어긋난 줄 문장 목록(빈 목록이 통과)."""
    files = map_files(root)
    if not files:
        return None
    rs = []
    for shown, path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            rs += [(n, c, shown) for n, c in rows(fh.read())]
    return (check_dupes(rs) + check_related(rs) + check_locations(rs, root)
            + check_headers(rs, root))


def main(argv):
    out = check(argv[0] if argv else ".")
    if out is None:
        print("기능맵 없음 — 건너뜀")
        return 0
    if not out:
        print("기능맵 정합 통과")
        return 0
    print("\n".join(out))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
