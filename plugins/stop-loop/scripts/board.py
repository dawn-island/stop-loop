#!/usr/bin/env python3
"""작업 보드 — `docs/backlog/*.md` 의 머리말을 읽어 칸반으로 보여준다.

  python3 bin/board.py [프로젝트 경로]          터미널에 칸을 찍고 board.html 을 쓴다
  python3 bin/board.py [프로젝트 경로] --따라가기  이 기계의 보드 주소를 알린다 (없으면 그 보드를 띄운다)
  python3 bin/board.py --상주                  모든 프로젝트를 보는 보드 서버 하나를 띄우고 사람 차례를 알린다
  python3 bin/board.py --상주-켜기 | --상주-끄기  로그인 때 --상주 가 뜨게 하는 항목을 쓰고 지운다

보드 서버는 이 기계에 하나이고 고정 자리 127.0.0.1:8700 에만 열린다 — 다른 번호로 옮기지 않는다.
첫 화면(`/`)이 보는 목록(`~/.stop-loop/projects.json` 의 `"projects"` 와 `"보기"`)의 모든
프로젝트를 한 줄씩 보이고, 줄을 누르면 `/p/<이름>/board.html` 에 그 프로젝트 칸반이 열린다.

파일이 진실이다. 보드가 파일을 쓰는 길은 셋뿐이다 — 「카드로 만들기」(개선 거리 화면, 새 아이디어 카드
파일 하나를 쓰고 원격 기준 가지에 올린다), 「대기로 올리기」(시나리오에 안 묶인 아이디어 카드의 `상태:` 줄만
대기로, 확인 창 뒤에 그 파일 하나만 올린다), 「이 카드에 잇기」(개선 거리 화면, 이미 있는 카드 「## 배경」 끝에 출처 줄만
더하고 그 파일 하나만 올린다). 모두 127.0.0.1 에서 이 기계의 보드 화면이 보낸 요청만 받는다.
상태를 바꾸려면 그 파일의 `상태:` 줄을 고친다(승격은 사람만 한다, `workflows/01-loop-design.md`).
러너는 실행 중에 같은 줄을 되쓴다.

머리말은 파일 맨 앞의 `---` 두 줄 사이에 있고, `열쇠: 값` 형식이다. YAML 라이브러리를
쓰지 않는 이유는 표준 라이브러리만으로 돌리기 위해서다(`bin/ledger.py` 와 같은 규율).
"""
import collections
import copy
import datetime as dt
import glob
import hashlib
import html
import http.client
import http.server
import json
import math
import os
import plistlib
import re
import shutil
import socket
import socketserver
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

# 엔진 상태(`stoploop.py` 의 STATES)와 1대1이고, 맨 앞 「아이디어」만 그 앞에 있다 —
# 승격 전이라 러너가 집어가지 않는다(`CLAUDE.md` §5, 기록 34 정정).
COLUMNS = ["아이디어", "대기", "진행", "검토", "보류", "완료"]

# 11개 스테이지를 다섯 국면으로 묶는다. 칸을 늘리지 않는 이유는 칸이 엔진 상태와
# 1대1이어야 하기 때문이다 — 국면은 「진행」 안의 위치일 뿐이라 카드가 보인다.
PHASES = ["분석", "설계", "구현", "검증", "인계"]
STAGE_PHASE = {"01": "분석", "02": "분석", "03": "분석", "03a": "분석", "04": "분석",
               "05": "설계", "05u": "설계", "06": "설계",
               "07": "구현",
               "08": "검증", "09": "검증",
               "10": "인계", "11": "인계",
               "01+03": "분석", "02+06": "설계"}   # 규모 S 의 묶인 세션(runner.MERGED_LABEL)


def phase_of(단계):
    """`단계:` 줄(예: 「08 품질 확인 · 2회차」)에서 국면을 뽑는다. 모르면 None."""
    head = 단계.split()[0] if 단계.strip() else ""
    return STAGE_PHASE.get(head)


def front_matter(path):
    """맨 앞 `---` 블록을 사전으로. 없으면 빈 사전."""
    out = {}
    with open(path, encoding="utf-8") as f:
        if f.readline().strip() != "---":
            return out
        for line in f:
            if line.strip() == "---":
                break
            _머리말_줄(line, out)
    return out


def _머리말_줄(line, out):
    """머리말 한 줄 `열쇠: 값` 을 사전에 담는다."""
    key, sep, value = line.partition(":")
    if sep:
        # 값 뒤 빈칸 둘 이상에 이어 나오는 `#` 부터는 설명이다 (runner.py 의 _head_value 와 같은 규칙)
        value = re.sub(r"\s{2,}#.*$", "", value)
        out[key.strip()] = value.strip().strip('"').strip("'")


def remote_front_matter(text):
    """원격에서 읽은 글의 머리말. 첫 줄이 `---` 가 아니거나 닫는 `---` 가 없으면 None(깨짐)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    out = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        _머리말_줄(line, out)
    return None


def title(path):
    """첫 `# ` 제목. 없으면 파일 이름."""
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("# "):
                return line[2:].strip()
    return os.path.basename(path)


def elapsed(시작, 누적="", 상태="진행"):
    """진행한 시간을 사람 말로 — **멈춘 시간은 빼고** 센다.

    `누적` 은 끝난 진행 구간들의 합(분)이고, `시작` 은 지금 구간의 시작이다.
    진행 중이면 누적에 지금 구간을 더하고, 멈춰 있으면 누적만 보인다.
    """
    try:
        minutes = int(누적 or 0)
    except ValueError:
        minutes = 0
    읽음 = False
    if 상태 == "진행":
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                began = dt.datetime.strptime(시작.strip(), fmt)
            except ValueError:
                continue
            minutes += max(0, int((dt.datetime.now() - began).total_seconds() // 60))
            읽음 = True
            break
    if not 읽음 and not str(누적).strip():
        return ""                      # 잴 근거가 없으면 아무것도 보이지 않는다
    word = "째" if 상태 == "진행" else " 진행 (멈춤 제외)"
    if minutes < 60:
        return "%d분%s" % (minutes, word)
    return "%d시간 %d분%s" % (minutes // 60, minutes % 60, word)


# 화면 이름 표 — 사이드바·제목·명령 팔레트·돌아가기 고리·빈 문구가 모두 여기서 읽는다(ISSUE-ui-11-terms-2026-11-23).
# 그리는 함수가 부를 때마다 읽는다. 주소(`/`·`/timeline`·`/signals`)와 읽는 열쇠는 이 표와 무관하다.
화면이름 = {"home": "홈", "search": "검색", "timeline": "타임라인", "usage": "사용량·품질", "retro": "워크플로우 회고", "improve": "개선 후보",
         "todo": "내 할 일", "board": "보드", "cards": "카드 목록", "features": "기능 진척", "graph": "연관 지도", "scenario": "시나리오 상세"}
# 개선 종류의 보이는 글 — 산출물 절 제목을 읽는 열쇠(`IMPROVE_KINDS`)와 탭 주소 `k=` 값은 옛 글자 그대로이고 화면에 낼 때만 거친다.
개선_보임 = {"선택 사항": "권고", "이연 후보": "미룬 일", "사람 결정 미결": "결정 대기", "되풀이 신호": "반복 지적", "회고 몫": "회고 제안"}


def _종류_글(종류):
    return 개선_보임.get(종류, 종류)


def _기기_글(값):
    """기기 이름을 화면에 낼 때만 — 자료 값 「기기 모름」은 거르기·정렬이 비교하므로 그대로 두고 보이는 글만 바꾼다."""
    return str(값).replace("기기 모름", "기기 기록 없음")


def _화면들_채우기(page):
    """틀 안의 `@@NAMES@@` 자리(명령 팔레트 화면 목록)를 지금의 이름 표로 채운다."""
    return page.replace("@@NAMES@@", json.dumps(화면이름, ensure_ascii=False).replace("<", "\\u003c"))


# 러너가 도는 동안 한 줄씩 쌓는 폴더 (ISSUE-live-view-2026-09-20). 러너의 `LIVE_DIR` 과 같은
# 경로이고, 파일 이름은 「저장소 이름 두 밑줄 이슈 이름」이다. 시험이 임시 폴더로 바꿔 끼운다.
LIVE_DIR = os.path.expanduser("~/.stop-loop/live")


def last_live_line(backlog, file):
    """진행 카드의 실시간 줄 파일에서 맨 끝 한 줄. 없거나 못 읽으면 빈 문자열."""
    try:
        return _실시간_줄(os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(backlog)))), file)
    except TypeError:
        return ""


def _실시간_줄(repo, file):
    try:
        name = "%s__%s.log" % (repo, os.path.splitext(file)[0])
        with open(os.path.join(LIVE_DIR, name), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 8192))
            rows = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError):
        return ""
    return next((x.strip() for x in reversed(rows) if x.strip()), "")


# ── 멈춘 카드 경보 (ISSUE-sc02-e9-stalled-alarm-2026-11-20) ──
# 단계 상한은 `bin/runner.py:414-423` 과 같은 값이다 — 바꿀 때 두 곳을 함께 고친다(시험 S-01 이 러너 값과 같은지 본다).
SILENCE, HARD = 300, 1800
LIMITS = {"05": (600, 3600), "07": (600, 3600), "08": (900, 5400)}
MODELS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "workflows", "stop-loop", "models.json")


def _양의정수(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def stage_limit_table(project):
    """{단계 번호: (침묵 초, 시간 초)} — 러너 `load_models` 와 같은 두 파일·차례·거르기. 나중 파일의 유효한 표가 앞 표를 통째로 대신한다."""
    table = {}
    for src in (MODELS_FILE, None):
        try:
            if src is None:
                lim = _유효_설정(project).get("stageLimits")          # 원격 끝에서 읽은 설정이 있으면 그것, 없으면 사본
            else:
                with open(src, encoding="utf-8") as f:
                    lim = json.load(f).get("stageLimits")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(lim, dict):
            ok = {k: (v["silence"], v["hard"]) for k, v in lim.items()
                  if isinstance(v, dict) and all(_양의정수(v.get(x)) for x in ("silence", "hard"))}
            if ok:
                table = ok
    return table


def limits_for(table, 단계):
    """(번호 또는 None, 침묵, 시간, 단계 모름). `단계:` 앞 낱말(`01+03` 은 `+` 앞)이 번호. 모르면 표와 기본값 가운데 가장 긴 상한."""
    head = 단계.split()[0] if 단계.strip() else ""
    num = head.split("+")[0]
    if num in STAGE_PHASE:
        sil, hard = table.get(num) or LIMITS.get(num, (SILENCE, HARD))
        return num, sil, hard, False
    every = list(table.values()) + list(LIMITS.values()) + [(SILENCE, HARD)]
    return None, max(x[0] for x in every), max(x[1] for x in every), True


def live_last_time(backlog, file):
    """실행 기록 마지막 줄 맨 앞의 시각. 파일이 없거나 시각을 못 읽으면 None."""
    line = last_live_line(backlog, file)
    try:
        return dt.datetime.fromisoformat(line.split()[0])
    except (IndexError, ValueError):
        return None


def process_lines():
    """이 기계 프로세스 목록의 줄들. 읽지 못했거나 비면 None."""
    try:
        r = subprocess.run(["ps", "-A", "-o", "command="], capture_output=True, encoding="utf-8", errors="replace", timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = r.stdout.splitlines() if r.returncode == 0 else []
    return lines or None


def runner_alive(lines, file):
    """그 카드의 러너가 살아 있나. 목록을 못 읽었으면 None. 러너 줄은 「파이썬 runner.py … 카드 파일」 모양이다 —
    `bin/dispatch.py:526`. claude 세션·셸 줄에 `runner.py` 글자가 들어 있어도 첫 낱말이 파이썬이 아니면 치지 않는다."""
    if not lines:
        return None
    for line in lines:
        w = line.split()
        if (len(w) >= 3 and os.path.basename(w[0]).lower().startswith("python") and os.path.basename(w[1]) == "runner.py"
                and any(os.path.basename(x) == file for x in w[2:])):
            return True
    return False


def _done_key(card):
    """완료 카드 정렬 열쇠(오름차순): 갱신 늦은 순 → 시작 늦은 순 → 파일 이름. 못 읽으면 각 단계의 맨 뒤."""
    try:
        d = dt.datetime.strptime(card["갱신"], "%Y-%m-%d")
    except ValueError:
        return (1, 0, 1, 0, card["file"])
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            s = dt.datetime.strptime(card["시작"], fmt)
        except ValueError:
            continue
        return (0, -d.timestamp(), 0, -s.timestamp(), card["file"])
    return (0, -d.timestamp(), 1, 0, card["file"])


def list_value(value):
    """머리말 값 하나를 목록으로. 쉼표로 나누고 빈칸을 떼며 `—`·`-`·빈 조각은 버린다."""
    return [x for x in (p.strip() for p in value.split(",")) if x and x not in ("—", "-")]


def _카드_만들기(file, title, fm, 마지막줄="", 사람분=None):
    return {"file": file, "title": title,
            "상태": fm.get("상태", "대기"), "크기": fm.get("크기", ""),
            "갱신": fm.get("갱신", ""), "멈춘이유": fm.get("멈춘이유", ""),
            "단계": fm.get("단계", ""), "시작": fm.get("시작", ""), "차례": fm.get("차례", "").strip(),
            "누적": fm.get("누적", ""),
            "시나리오": fm.get("시나리오", "").strip(), "요소": list_value(fm.get("요소", "")),
            "인수조건": list_value(fm.get("인수조건", "")),
            "완료기준": list_value(fm.get("완료기준", "")), "선행": list_value(fm.get("선행", "")),
            "돈기계": fm.get("돈기계", ""), "기기": fm.get("기기", ""), "기능": list_value(fm.get("기능", "")),
            "마지막줄": 마지막줄, "사람분": 사람분}


def _줄_세우기(cards):
    """완료만 갱신 내림차순으로 줄 세운다. 다른 칸은 파일 이름 순 그대로 (이슈 ISSUE-board-done-order)."""
    cards = sorted(cards, key=lambda c: c["file"])
    return [c for c in cards if c["상태"] != "완료"] + sorted(
        (c for c in cards if c["상태"] == "완료"), key=_done_key)


def human_minutes(path):
    """본문 `## 사람이 하면` 절의 첫 비지 않은 줄이 아라비아 숫자 바로 뒤 「분」으로 시작할 때만 그 숫자. 아니면 None."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    return _사람분_줄들(lines)


def _사람분_줄들(lines):
    inside = False
    for l in lines:
        if l.startswith("## "):
            if inside:
                return None
            inside = l[3:].strip() == "사람이 하면"
        elif inside and l.strip():
            m = re.match(r"^(\d+)분", l.strip())
            return int(m.group(1)) if m else None
    return None


def read_cards(backlog):
    cards = []
    for path in sorted(glob.glob(os.path.join(backlog, "*.md"))):
        if os.path.basename(path).startswith("_"):
            continue            # `_템플릿.md` 처럼 밑줄로 시작하면 작업이 아니다
        fm = front_matter(path)
        cards.append(_카드_만들기(
            os.path.basename(path), title(path), fm,
            last_live_line(backlog, os.path.basename(path)) if fm.get("상태", "대기") == "진행" else "",
            human_minutes(path)))
    return _줄_세우기(cards)


_시나리오이름 = re.compile(r"^(SC-\d{2})-.*\.md$")
_시나리오상태 = ("초안", "확정", "구현 중", "완료")
_기준절 = ("인수 조건", "완료 기준")      # `bin/endreport.py` 의 `_절` 과 같은 목록 — 바꿀 때 두 곳을 함께 고친다


def read_scenarios(plan_dir):
    """`docs/plan/SC-nn-*.md` 마다 시나리오 하나. 폴더가 없으면 빈 목록. 못 읽으면 번호와 오류만 갖는다."""
    out = []
    for path in sorted(glob.glob(os.path.join(plan_dir, "SC-*.md"))):
        m = _시나리오이름.match(os.path.basename(path))
        if not m:
            continue
        s = _시나리오_빈것(m.group(1), os.path.basename(path))
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (OSError, UnicodeDecodeError):
            s["오류"] = "시나리오 파일을 읽지 못했다"
            out.append(s)
            continue
        _시나리오_풀기(s, lines)
        out.append(s)
    return sorted(out, key=lambda s: s["번호"])


_시나리오_저장 = {}                                    # {(프로젝트, 원격 끝): 시나리오 목록} — 끝이 같으면 거울을 다시 읽지 않는다


def _거울_시나리오(project):
    """거울 끝의 `docs/plan/SC-*.md` 로 만든 시나리오 목록. 거울에서 못 읽으면 None."""
    pic = REMOTE.get(os.path.abspath(project)) or {}
    mirror = _거울_경로(project)
    if pic.get("없음") or not pic.get("끝") or not os.path.isdir(mirror):
        return None
    열쇠 = (os.path.abspath(project), pic["끝"])
    if 열쇠 in _시나리오_저장:
        return copy.deepcopy(_시나리오_저장[열쇠])
    try:
        r = subprocess.run(["git", "-C", mirror, "-c", "core.quotepath=false", "ls-tree", "-z", "--name-only", pic["끝"], "--", "docs/plan/"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        names = [n for n in sorted(os.path.basename(x) for x in r.stdout.split("\0") if x) if _시나리오이름.match(n)]
        글들 = _묶음_읽기(mirror, ["%s:docs/plan/%s" % (pic["끝"], n) for n in names])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    out = []
    for n in names:
        s = _시나리오_빈것(_시나리오이름.match(n).group(1), n)
        text = 글들.get("%s:docs/plan/%s" % (pic["끝"], n))
        if text is None:
            s["오류"] = "시나리오 파일을 읽지 못했다"
        else:
            _시나리오_풀기(s, text.splitlines())
        out.append(s)
    out = sorted(out, key=lambda s: s["번호"])
    _시나리오_저장[열쇠] = copy.deepcopy(out)
    return out


def 시나리오들(project):
    """프로젝트의 시나리오 목록 — 그림에 끝이 있고 거울에서 읽히면 거울 끝의 것, 아니면 작업 사본 `docs/plan/` 의 것."""
    got = _거울_시나리오(project)
    return got if got is not None else read_scenarios(os.path.join(project, "docs", "plan"))


def _시나리오_빈것(번호, file):
    return {"번호": 번호, "이름": "", "상태": "상태 모름", "요소": set(), "카드아님": set(), "오류": "", "남은질문": [],
            "file": file, "완료기준": {}, "없어짐": set(), "요소이름": {}, "선행": {}}


def _시나리오_풀기(s, lines):
    """시나리오 글 줄들을 읽어 `s`(`_시나리오_빈것`)를 채운다 — 파일에서 읽든 거울에서 읽든 같은 사전이 된다."""
    head = next((l[2:].strip() for l in lines if l.startswith("# ")), "")
    s["이름"] = head[len(s["번호"]):].strip() if head.startswith(s["번호"] + " ") else head
    in_elements = ask = in_criteria = False
    pre_col = None                                  # 요소 표 머리에서 찾은 「선행」 칸 자리
    for i, l in enumerate(lines):
        if l.startswith("## "):
            in_elements = l[3:].strip() == "요소"
            in_criteria = l[3:].strip() in _기준절
            ask = l[3:].strip() == "확인 필요"
            continue
        if ask:     # 질문 = 표 데이터 줄(머리줄·구분줄·자리표시·빈 칸 줄 뺀 것)과 `- ` 목록 줄
            c = _칸(l) if l.strip().startswith("|") else []
            if l.startswith("- ") and l[2:].strip():
                s["남은질문"].append(l[2:].strip())
            elif (c and not _표_구분줄.match(l) and "{" not in l and any(c)
                  and not (i + 1 < len(lines) and _표_구분줄.match(lines[i + 1]))):
                s["남은질문"].append(" · ".join(x for x in c if x))
        cells = _칸(l) if l.strip().startswith("|") else []
        if len(cells) >= 2 and cells[0] == "상태":
            s["상태"] = next((w for w in _시나리오상태 if cells[1].startswith(w)), cells[1])
        elif in_elements and cells and "선행" in cells:
            pre_col = cells.index("선행")
        elif in_elements and cells and re.fullmatch(r"E-\d+", cells[0]):
            s["요소"].add(cells[0])
            s["요소이름"][cells[0]] = cells[1] if len(cells) > 1 else ""
            cell = cells[pre_col] if pre_col is not None and pre_col < len(cells) else ""
            s["선행"][cells[0]] = [(m.group(1), "겹침" if m.group(2) else "결과물")
                                  for m in re.finditer(r"(E-\d+)(\s*\(겹침\))?", cell)]
            if any("(카드 아님)" in x for x in cells[1:]):
                s["카드아님"].add(cells[0])
            if any(x.startswith("없어짐") for x in cells[1:]):
                s["없어짐"].add(cells[0])
        elif in_criteria and len(cells) >= 2 and re.fullmatch(r"AC-\d+", cells[0]):
            s["완료기준"][cells[0]] = cells[1]


def card_marks(cards, scenarios):
    """카드마다 잘못된 입력 표시 글 목록 `{file: [글, …]}`. 시나리오가 없는 카드는 빈 목록."""
    by_no = {s["번호"]: s for s in scenarios}
    marks = {c["file"]: [] for c in cards}
    for c in cards:
        no = c["시나리오"]
        if not no:
            continue
        mine = marks[c["file"]]
        s = by_no.get(no)
        if s is None:
            mine.append("가리키는 시나리오 없음 (%s)" % no)
            continue
        if not s["오류"]:
            mine += ["가리키는 시나리오 없음 — 요소 %s" % e for e in c["요소"] if e not in s["요소"]]
        if not c["요소"]:
            mine.append("요소 번호 없음")
        old, new = c["인수조건"], c.get("완료기준", [])
        if not s["오류"] and s.get("완료기준"):
            mine += ["가리키는 완료 기준이 시나리오에 없음 — %s" % a for a in (new or old) if a not in s["완료기준"]]
        if not any(re.fullmatch(r"AC-\d+", a) for a in new or old):
            mine.append("완료 기준 번호 없음")
        elif old and new and set(old) != set(new):
            mine.append("완료 기준 번호가 옛 열쇠와 다름 (옛 %s · 새 %s)" % (", ".join(old), ", ".join(new)))
        same = [x for x in cards if x["시나리오"] == no]
        for p in c["선행"]:
            if not any(p in x["요소"] for x in same):
                mine.append("앞선 카드 없음 — %s" % p)
    for no in {c["시나리오"] for c in cards if c["시나리오"] in by_no}:
        same = [c for c in cards if c["시나리오"] == no]
        nxt = {c["file"]: [x["file"] for x in same for p in c["선행"] if p in x["요소"]] for c in same}

        def reaches(start):
            seen, todo = set(), list(nxt[start])
            while todo:
                f = todo.pop()
                if f == start:
                    return True
                if f not in seen:
                    seen.add(f)
                    todo += nxt[f]
            return False
        for c in same:
            if reaches(c["file"]):
                marks[c["file"]].append("앞선 관계가 고리를 이룸")
    return marks


def precedence(project, cards, scenarios):
    """시나리오마다 선행 그림 자료 — 요소마다 그 카드와 표시 글, 카드 사이 화살표 `{"앞", "뒤", "종류"}`.

    화살표의 출처는 요소 표 선행 칸(종류 정본)과 카드 머리말 `선행:`(결과물). 같은 카드 안은 잇지 않는다."""
    marks = card_marks(cards, scenarios)
    out = []
    for s in scenarios:
        same = [c for c in cards if c["시나리오"] == s["번호"]]
        held = lambda e: [c for c in same if e in c["요소"]]        # noqa: E731
        edges = {}
        for e, pres in s.get("선행", {}).items():
            for p, kind in pres:
                for later in held(e):
                    for early in held(p):
                        if early["file"] != later["file"]:
                            pair = (early["file"], later["file"])
                            edges[pair] = "겹침" if "겹침" in (kind, edges.get(pair)) else "결과물"
        for c in same:
            for p in c["선행"]:
                for early in held(p):
                    if early["file"] != c["file"]:
                        edges.setdefault((early["file"], c["file"]), "결과물")
        els = []
        for e in sorted(s["요소"], key=lambda x: int(x[2:])):
            texts = []
            for c in held(e):
                texts += [t for t in marks[c["file"]] if t.startswith("앞선") and t not in texts]
            texts += ["없는 요소 — %s" % p for p, _ in s.get("선행", {}).get(e, []) if p not in s["요소"]]
            els.append({"번호": e, "이름": s["요소이름"].get(e, ""), "카드아님": e in s["카드아님"], "없어짐": e in s["없어짐"],
                        "카드": [{"file": c["file"], "title": c["title"], "상태": c["상태"]} for c in held(e)], "표시": texts})
        out.append({"프로젝트": project, "시나리오": s["번호"], "이름": s["이름"], "오류": s["오류"], "요소": els,
                    "화살표": [{"앞": a, "뒤": z, "종류": k} for (a, z), k in sorted(edges.items())]})
    return out


def _기기들(cards):
    """진행 카드가 돌고 있는 기기 이름, 겹침 없이 카드 순서대로. 기기 정보가 없으면 「기기 모름」."""
    out = []
    for c in cards:
        if c["상태"] == "진행":
            d = c.get("도는 기계") or c.get("돈기계") or c.get("기기") or "기기 모름"
            if d not in out:
                out.append(d)
    return out


def scenario_progress(cards, s):
    """시나리오 하나의 요소 진행. 「(카드 아님)」·「없어짐」은 뺀다. 요소마다 정확히 한 상태로 센다.

    한 요소의 카드가 모두 완료면 완료, 아니면 완료 아닌 카드 가운데 보류 > 진행 > 검토 > 대기 > 아이디어.
    「요소가 완료」 규칙은 `bin/dispatch.py` 의 `_ready` 와 같다 — 바꿀 때 두 곳을 함께 고친다."""
    mine = [c for c in cards if c["시나리오"] == s["번호"]]
    count = {st: 0 for st in COLUMNS}
    none = 0
    skip = set(s.get("카드아님", ())) | set(s.get("없어짐", ()))
    elements = [e for e in s["요소"] if e not in skip]
    for e in elements:
        states = [c["상태"] for c in mine if e in c["요소"]]
        if not states:
            none += 1
        elif all(x == "완료" for x in states):
            count["완료"] += 1
        else:
            count[next(x for x in ("보류", "진행", "검토", "대기", "아이디어") if x in states)] += 1
    return {"수": count, "카드 없음": none, "카드 아님": len(set(s.get("카드아님", ())) & s["요소"]),
            "분모": len(elements), "퍼센트": count["완료"] * 100 // len(elements) if elements else None,
            "기기": _기기들(mine)}


def criteria_rows(cards, s):
    """완료 기준마다 한 줄 `{번호, 문장, 카드: [(제목, 상태)], 모두 완료, 결과}`. 결과는 끝 보고가 없으면 None."""
    mine = [c for c in cards if c["시나리오"] == s["번호"]]
    end = s.get("끝보고")
    rows = []
    for no, text in s.get("완료기준", {}).items():
        have = [c for c in mine if no in (c.get("완료기준") or c.get("인수조건", []))]
        rows.append({"번호": no, "문장": text if len(text) <= 40 else text[:40] + "…",
                     "카드": [(c["title"], c["상태"]) for c in have],
                     "모두 완료": bool(have) and all(c["상태"] == "완료" for c in have),
                     "결과": None if end is None else end.get(no, "완료 보고서에 없음")})
    return rows


_칸_클래스 = {"완료": "done", "진행": "doing", "보류": "hold", "멈춤": "stop", "아이디어": "idea", "없음": "none"}
_칸_모양 = {"완료": "완료", "진행": "진행", "검토": "진행", "대기": "진행", "아이디어": "아이디어", "카드 없음": "없음"}


def 요소_칸(cards, s):
    """시나리오 하나의 요소 칸 — 「(카드 아님)」·「없어짐」을 뺀 요소마다 하나, `E-` 숫자 순서.

    상태는 `scenario_progress` 를 그 요소 하나만 든 시나리오로 불러 얻는다(요소 상태 규칙을 한 곳에만 둔다).
    모양은 완료·진행(진행·검토·대기)·보류·멈춤(러너가 멈춘 보류)·아이디어·없음. 읽지 못한 시나리오는 빈 목록."""
    if s["오류"]:
        return []
    mine = [c for c in cards if c["시나리오"] == s["번호"]]
    skip = set(s.get("카드아님", ())) | set(s.get("없어짐", ()))
    out = []
    for e in sorted((x for x in s["요소"] if x not in skip), key=lambda x: int(x[2:])):
        p = scenario_progress(cards, {**s, "요소": {e}})
        상태 = "카드 없음" if p["카드 없음"] else next(k for k, n in p["수"].items() if n)
        have = sorted((c for c in mine if e in c["요소"]), key=lambda c: c["file"])
        정한 = [c for c in have if 상태 == "완료" or c["상태"] == 상태]
        모양 = "멈춤" if 상태 == "보류" and any(러너가_멈춤(c) for c in have) else "보류" if 상태 == "보류" else _칸_모양[상태]
        까닭 = next((c["멈춘이유"].strip() for c in 정한 if c["멈춘이유"].strip()), "까닭 없음") if 상태 == "보류" else ""
        out.append({"요소": e, "이름": s["요소이름"].get(e, ""), "상태": 상태, "모양": 모양,
                    "카드": 정한[0] if 정한 else None, "카드들": have, "까닭": 까닭})
    return out


def waiting_rows(cards, s):
    """선행 때문에 기다리는 카드 `[(제목, ["E-3 완료를 기다림", …])]`. 완료가 아닌 카드만."""
    mine = [c for c in cards if c["시나리오"] == s["번호"]]

    def done(e):
        have = [c for c in mine if e in c["요소"]]
        return bool(have) and all(c["상태"] == "완료" for c in have)
    out = []
    for c in mine:
        if c["상태"] == "완료":
            continue
        wait = ["%s 완료를 기다림" % e for e in c["선행"] if not done(e)]
        if wait:
            out.append((c["title"], wait))
    return out


# ── 계획 대비 (ISSUE-sc02-e7-plan-vs-actual-2026-11-20) ──
def _시각들(text):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    return None


def _숫자(text):
    t = str(text).strip()
    return int(t) if re.fullmatch(r"\d+", t) else None


def actual_minutes(card, now=None):
    """카드의 실제 진행 시간(분) — 누적 + (진행이면) 지금 구간. 멈춘 시간은 빠진다. `elapsed` 와 같은 셈이니 바꿀 때 두 곳을 함께 고친다."""
    # ponytail: elapsed 는 글을 내고 이쪽은 숫자를 낸다 — 합치지 않고 두 곳을 함께 고친다
    m = _숫자(card["누적"]) or 0
    if card["상태"] == "진행":
        began = _시각들(card["시작"])
        if began:
            m += max(0, int(((now or dt.datetime.now()) - began).total_seconds() // 60))
    return m


def size_estimate(cards, card):
    """같은 크기 완료 카드(자기 뺀)들의 누적 중앙값. `{"분", "표본", "뺀", "까닭"}`. 표본이 없으면 분이 None."""
    if card["크기"] not in ("S", "M", "L"):
        return {"분": None, "표본": 0, "뺀": 0, "까닭": "크기가 S·M·L 이 아님"}
    same = [c for c in cards if c["file"] != card["file"] and c["크기"] == card["크기"] and c["상태"] == "완료"]
    vals = [v for v in (_숫자(c["누적"]) for c in same) if v is not None]
    if not vals:
        return {"분": None, "표본": 0, "뺀": len(same), "까닭": "같은 크기 완료 카드 0장"}
    return {"분": int(statistics.median(vals) + 0.5), "표본": len(vals), "뺀": len(same) - len(vals), "까닭": ""}


def _분글(m):
    return "%d분" % m if m < 60 else "%d시간 %d분" % (m // 60, m % 60)


def _넘김(cards, c, now=None):
    e = size_estimate(cards, c)["분"]
    return c["상태"] == "진행" and e is not None and actual_minutes(c, now) >= e


_묶음이름 = ("오늘", "내일", "이번 주", "그 뒤", "예상치 없음")


def due_groups(cards, now=None):
    """진행 카드만 예상 완료 때(지금 + 크기 예상 − 실제)로 묶는다. `[(묶음, [(카드, 넘김)])]`. 넘김 카드는 「오늘」 맨 위."""
    now = now or dt.datetime.now()
    today = now.date()
    sunday = today + dt.timedelta(days=6 - today.weekday())
    out = {k: [] for k in _묶음이름}
    for c in cards:
        if c["상태"] != "진행":
            continue
        e = size_estimate(cards, c)["분"]
        if e is None:
            out["예상치 없음"].append((c, False))
            continue
        left = e - actual_minutes(c, now)
        d = (now + dt.timedelta(minutes=max(left, 0))).date()
        k = ("오늘" if left <= 0 or d == today else "내일" if d == today + dt.timedelta(days=1)
             else "이번 주" if d <= sunday else "그 뒤")
        out[k].append((c, left <= 0))
    out["오늘"].sort(key=lambda x: not x[1])
    return [(k, out[k]) for k in _묶음이름]


def plan_sums(cards, mine):
    """시나리오 조각의 합 줄 셋. 끝난 카드는 예상(또는 사람분)과 누적이 모두 있을 때만 두 합에 함께 든다."""
    done = [c for c in mine if c["상태"] == "완료"]
    est = [(size_estimate(cards, c)["분"], _숫자(c["누적"])) for c in done]
    est = [x for x in est if x[0] is not None and x[1] is not None]
    hum = [(c.get("사람분"), _숫자(c["누적"])) for c in done if c.get("사람분") is not None and _숫자(c["누적"]) is not None]
    tail = "(예상이나 누적이 없어 뺀 카드 %d장)"
    return ["끝난 카드 크기 예상 합 %s 대 실제 %s %s" % (_분글(sum(x for x, _ in est)), _분글(sum(y for _, y in est)), tail % (len(done) - len(est))),
            "사람이 하면 합 %s 대 실제 %s %s" % (_분글(sum(x for x, _ in hum)), _분글(sum(y for _, y in hum)), tail % (len(done) - len(hum))),
            remaining_text(cards, mine).replace("남은 시간(예상)", "남은 카드 크기 예상 합", 1).replace("—", "남은 카드 없음")]


def remaining_text(cards, mine):
    """남은(완료 아닌) 카드의 크기 예상 합 — 「남은 예상 N분 (예상 없는 카드 K장)」. 남은 카드가 없으면 「—」."""
    left = [c for c in mine if c["상태"] != "완료"]
    if not left:
        return "—"
    es = [size_estimate(cards, c)["분"] for c in left]
    return "남은 시간(예상) %s (예상 없는 카드 %d장)" % (_분글(sum(e for e in es if e is not None)), es.count(None))


def plan_line(cards, c, now=None):
    """카드 타일 계획 줄 글."""
    e, a = size_estimate(cards, c), actual_minutes(c, now)

    def diff(label, est):
        d = a - est
        return "%s %s" % (label, "예상과 같음" if d == 0 else "%s %s" % (_분글(abs(d)), "늦음" if d > 0 else "빠름"))
    if e["분"] is None:
        bits = ["크기 예상치 없음 — " + e["까닭"]]
    else:
        bits = ["크기 예상 %s (표본 %d장 · 중앙값%s)" % (_분글(e["분"]), e["표본"], " · 뺀 카드 %d장" % e["뺀"] if e["뺀"] else "")]
    bits += ["사람이 하면 %d분 (카드에 적힌 분 · 표본 1장)" % c.get("사람분") if c.get("사람분") is not None else "사람이 하면 예상치 없음",
             "실제 " + _분글(a)]
    if e["분"] is not None:
        bits.append(diff("크기 예상보다", e["분"]).replace("보다 예상과 같음", "과 같음"))
    if c.get("사람분") is not None:
        bits.append(diff("사람이 하면보다", c.get("사람분")).replace("보다 예상과 같음", "과 같음"))
    return " · ".join(bits)


def scenario_groups(cards, scenarios):
    """묶음 목록 `[(머리 글, [카드, …])]`. 시나리오마다 하나, 마지막은 묶이지 않은 카드."""
    known = {s["번호"] for s in scenarios}
    groups = []
    for s in scenarios:
        mine = [c for c in cards if c["시나리오"] == s["번호"]]
        head = "%s %s — %s · 완료 %d / %d" % (
            s["번호"], s["이름"], s["상태"], len([c for c in mine if c["상태"] == "완료"]), len(mine))
        head = head.replace("%s  —" % s["번호"], "%s —" % s["번호"])
        if s["오류"]:
            head += " · " + s["오류"]
        groups.append((head, mine))
    loose = [c for c in cards if c["시나리오"] not in known]
    groups.append(("시나리오에 묶이지 않은 카드 (%d)" % len(loose), loose))
    return groups

# ── 내가 할 일 — 옛 이름 사람 차례 (ISSUE-sc01-e5-human-turn-2026-10-02) ──
# 러너의 `STATUS_DIR` 과 같은 경로. HUMAN_DIR 은 따라가기가 직전 목록을 저장하는 자리다. 시험이 바꿔 끼운다.
STATUS_DIR = os.path.expanduser("~/.stop-loop/status")
HUMAN_DIR = os.path.expanduser("~/.stop-loop/human")
_모름 = "생긴 때 모름"


def _생긴때(project, card):
    """요약의 `updated` (요약 state 가 카드 상태와 맞을 때만) → 카드 갱신 → 「생긴 때 모름」."""
    want = {"검토": "review", "보류": "blocked"}.get(card["상태"])
    name = "%s__%s.json" % (os.path.basename(project), os.path.splitext(card["file"])[0])
    try:
        with open(os.path.join(STATUS_DIR, name), encoding="utf-8") as f:
            s = json.load(f)
        if s.get("state") == want and s.get("updated"):
            return str(s["updated"])
    except (OSError, ValueError, AttributeError):
        pass
    return card["갱신"] or _모름


END_REPORT = "끝보고-%s.md"      # `bin/endreport.py` 가 쓰고 여기서 읽는다. `SC-` 로 시작하지 않아 시나리오로 안 읽힌다
_다룬카드 = "다룬 카드:"


def end_report_cards(plan_dir, 번호):
    """끝 보고가 다룬 카드 파일 집합. 파일이 없거나 「다룬 카드:」 줄을 못 읽으면 None."""
    try:
        with open(os.path.join(plan_dir, END_REPORT % 번호), encoding="utf-8") as f:
            for l in f.read().splitlines():
                if l.startswith(_다룬카드):
                    return {x.strip() for x in l[len(_다룬카드):].split(",") if x.strip()}
    except (OSError, UnicodeDecodeError):
        pass
    return None


def end_report_results(plan_dir, 번호):
    """끝 보고 「## 완료 기준 확인」 표의 `{기준 번호: 결과 글}`. 파일이 없거나 못 읽으면 None."""
    try:
        with open(os.path.join(plan_dir, END_REPORT % 번호), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    out, on = {}, False
    for l in lines:
        if l.startswith("## "):
            on = l[3:].strip() == "완료 기준 확인"
        elif on and l.strip().startswith("|"):
            c = _칸(l)
            if len(c) >= 2 and re.fullmatch(r"AC-\d+", c[0]):
                out[c[0]] = c[1]
    return out


def _date(v):
    """`YYYY-MM-DD` 실재 날짜면 그 글자, 아니면 None. 형식 아닌 값은 조용히 무시한다."""
    try:
        return v if dt.datetime.strptime(v, "%Y-%m-%d").strftime("%Y-%m-%d") == v else None
    except ValueError:
        return None


def _section_empty(path, 제목):
    """`## <제목>` 절이 없거나 공백·HTML 주석뿐이면 True."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"^## %s[ \t]*\n(.*?)(?=^## |\Z)" % re.escape(제목), text, re.S | re.M)
    return not m or not re.sub(r"<!--.*?-->", "", m.group(1), flags=re.S).strip()


def read_todo_cards(todo_dir):
    """`docs/todo/*.md`(밑줄 시작은 틀이라 건너뜀)를 카드 사전 목록으로. 폴더가 없으면 빈 목록."""
    cards = []
    for path in sorted(glob.glob(os.path.join(todo_dir, "*.md"))):
        if os.path.basename(path).startswith("_"):
            continue
        try:
            fm = front_matter(path)
            empty = _section_empty(path, "답" if fm.get("종류") == "고객 답" else "결과")
            t = title(path)
        except (OSError, UnicodeDecodeError):
            continue            # 읽을 수 없는 파일은 그 카드만 건너뛴다
        cards.append({"file": os.path.basename(path), "title": t, "종류": fm.get("종류", ""),
                      "상태": fm.get("상태", ""), "담당": fm.get("담당", ""), "기한": fm.get("기한", ""),
                      "다음 확인": fm.get("다음 확인", ""), "막는 것": list_value(fm.get("막는 것", "")),
                      "갱신": fm.get("갱신", ""), "결과 빔": empty})
    return cards


def todo_turns(cards, today, project):
    """할 일 카드 중 조건에 걸린 것을 「내가 할 일」 항목으로. 걸린 조건은 한 줄에 쉼표로 잇는다."""
    name = os.path.basename(os.path.abspath(project))
    out = []
    for c in cards:
        기한, 확인 = _date(c["기한"]), _date(c["다음 확인"])
        why = []
        if c["종류"] == "직접" and c["상태"] in ("할 일", "진행"):
            why.append("직접 · %s" % c["상태"])
        if c["종류"] == "고객 답" and c["상태"] == "기다림" and 확인 and 확인 <= today:
            why.append("답 확인일 도래")
        if 기한 and 기한 < today and c["상태"] not in ("끝", "취소"):
            why.append("기한 지남")
        if 기한 and 기한 < today and c["결과 빔"]:
            why.append("결과 빔")
        if why:
            out.append({"열쇠": "%s:%s" % (c["종류"], c["file"]), "종류": c["종류"], "프로젝트": name, "시나리오": "",
                        "제목": c["title"], "기다리는 것": ", ".join(why), "생긴 때": c["갱신"] or _모름,
                        "열어 볼 곳": "../todo/" + c["file"], "알림": None})
    return out


def todo_warnings(cards, backlog_dir):
    """`막는 것` 값 중 작업 카드(`docs/backlog/<값>.md`)도 관문 모양(`G7`)도 아닌 것이 있는 카드마다 경고 한 줄."""
    out = []
    for c in cards:
        gone = [v for v in c["막는 것"]
                if not re.match(r"^[A-Z]\d+$", v) and not os.path.isfile(os.path.join(backlog_dir, v + ".md"))]
        if gone:
            out.append("경고: 할 일 카드 %s 의 막는 것 %s 에 맞는 작업 카드가 없다" % (c["file"], ", ".join(gone)))
    return out


def human_turns(cards, scenarios, project, todo=(), today=None):
    """사람이 움직여야 일이 가는 항목. 보류=멈춤, 검토=사람이 병합할 PR, 그리고 완료 선언 대기. 오래된 것이 위."""
    name = os.path.basename(os.path.abspath(project))
    out = []

    def add(종류, 시나리오, 제목, 기다림, 때, 곳, key):
        out.append({"열쇠": "%s:%s" % (종류, key), "종류": 종류, "프로젝트": name, "시나리오": 시나리오,
                    "제목": 제목, "기다리는 것": 기다림, "생긴 때": 때, "열어 볼 곳": 곳, "알림": None})

    for c in cards:
        if c.get("차례") == "러너":
            continue            # 러너가 움직일 차례(기준 가지를 기다림·자동 병합) — 칸반엔 남고 사람 차례만 아니다
        if c["상태"] == "보류":
            add("멈춤", c["시나리오"], c["title"], c["멈춘이유"] or "까닭을 읽지 못한 멈춤",
                _생긴때(project, c), "board.html" if c.get("원격만") else c["file"], c["file"])
        elif c["상태"] == "검토":
            add("사람이 병합할 PR", c["시나리오"], c["title"], "PR 을 보고 병합한다",
                _생긴때(project, c), "board.html" if c.get("원격만") else c["file"], c["file"])
    for sc in scenarios:
        mine = [c for c in cards if c["시나리오"] == sc["번호"]]
        if (sc["상태"] in ("확정", "구현 중") and mine and all(c["상태"] == "완료" for c in mine)
                and end_report_cards(os.path.join(project, "docs", "plan"), sc["번호"]) == {c["file"] for c in mine}):
            때 = max((c["갱신"] for c in mine if c["갱신"]), default=_모름)
            add("완료 선언", sc["번호"], ("%s %s" % (sc["번호"], sc["이름"])).strip(),
                "완료 보고서를 보고 완료를 선언한다(시나리오 상태를 완료로)", 때, "../plan/" + END_REPORT % sc["번호"], sc["번호"])
    out += todo_turns(todo, today or dt.date.today().isoformat(), project)
    # 생긴 때를 모르는 것은 맨 아래. 날짜 글자는 사전순이 곧 시간순이다.
    return sorted(out, key=lambda t: (t["생긴 때"] == _모름, t["생긴 때"]))


def render_turns_text(turns):
    if not turns:
        return "내가 할 일 없음"
    lines = ["내가 할 일 (%d)" % len(turns)]
    for t in turns:
        tail = " · 알림 못 띄움" if t.get("알림") is False else ""
        lines.append("  · %s · %s · %s — %s · %s · %s%s" % (
            t["프로젝트"], t["종류"], t["제목"], t["기다리는 것"], t["생긴 때"], t["열어 볼 곳"], tail))
    return "\n".join(lines)


def notify(title, body):
    """맥 알림 한 번. 제목·본문은 스크립트 글에 끼우지 않고 인자로 넘긴다. 실패는 예외 없이 False."""
    try:
        r = subprocess.run(["osascript", "-e", "on run argv",
                            "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                            "-e", "end run", title, body], capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def record_resolved(project, turn, 자리=None):
    """해소된 항목 한 줄을 `사람손-<SC-nn>.md` 끝에 덧붙인다. `자리` 가 없으면 사본 `docs/plan/`(배차가 부르는 모양), 있으면 그 폴더. 실패하면 OSError."""
    plan = 자리 or os.path.join(project, "docs", "plan")
    os.makedirs(plan, exist_ok=True)
    path = os.path.join(plan, "사람손-%s.md" % turn["시나리오"])
    head = ""
    if not os.path.exists(path):
        head = ("# %s 사람 손 기록\n\n사람이 해야만 일이 간 행동을 한 줄씩 덧붙인다. 보드가 사라짐을 알아챈 때를 적는다.\n\n"
                % turn["시나리오"])
    셈 = turn.get("셈", "사람 손")
    if turn["종류"] == "완료 선언":      # 선언했을 때만 끝. 고치러 간 것(확정 그대로·카드가 늘어 내려감)은 사람 손
        now = [s for s in 시나리오들(project) if s["번호"] == turn["시나리오"]]
        셈 = "끝" if now and now[0]["상태"] == "완료" else "사람 손"
    what = turn["열어 볼 곳"] if turn["종류"] != "완료 선언" else turn["시나리오"]
    row = "- %s · %s · %s · %s — %s · 생긴 때 %s\n" % (
        dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), 셈, turn["종류"], what, turn["기다리는 것"], turn["생긴 때"])
    with open(path, "a", encoding="utf-8") as f:
        f.write(head + row)


def sync_turns(project, turns, 조용히=False, 기록=True):
    """직전 저장 목록과 비교한다. 새 열쇠는 알리고, 사라진 열쇠는 기록한다. 지금 목록에 알림 결과를 붙여 돌려준다.

    다른 기계 항목(`원격`)은 사라져도 이 기계 사람 손 기록에 쓰지 않고, 저장 파일이 없거나 `조용히` 인 바퀴에는 알리지 않고 기록만 한다.
    `기록` 이 거짓이면(보기 전용 프로젝트 사본) 사라진 항목도 사본에 쓰지 않는다 — 알림·저장 목록은 그대로."""
    path = os.path.join(HUMAN_DIR, os.path.basename(os.path.abspath(project)) + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
        if not isinstance(old, dict):
            old = {}
    except (OSError, ValueError):
        old = {}
        조용히 = True if not os.path.exists(path) else 조용히
    keep = {}
    for k, t in old.items():
        if k in {x["열쇠"] for x in turns}:
            continue
        try:
            if 기록 and t.get("시나리오") and not t.get("원격"):
                record_resolved(project, t, os.path.join(RESOLVED_DIR, _폴더이름(project)))   # 보드는 사본에 쓰지 않는다
        except OSError as e:
            print("사람 손 기록 실패 (다음 바퀴에 다시): %s" % e)
            keep[k] = t
    now = {}
    for t in turns:
        if t["열쇠"] in old:
            t["알림"] = old[t["열쇠"]].get("알림")
        elif t.get("원격") and 조용히:
            t["알림"] = None
        else:
            t["알림"] = notify("내가 할 일 — %s" % t["프로젝트"],
                              "%s · %s — %s" % (t["종류"], t["제목"], t["기다리는 것"]))
        now[t["열쇠"]] = t
    try:
        os.makedirs(HUMAN_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({**keep, **now}, f, ensure_ascii=False)
    except OSError as e:
        print("내가 할 일 저장 실패: %s" % e)
    return turns


def _막대(pct):
    """진행률 막대 `<span class="bar">` 안 인라인 너비 — 바깥 파일 없이 같은 HTML 안에서만 그린다."""
    return '<span class="bar"><span style="width:%d%%"></span></span> %s' % (
        pct or 0, "%d%%" % pct if pct is not None else "—")


def _표(head, rows):
    return ('<div class="progress"><table><tr>%s</tr>%s</table></div>\n'
            % ("".join("<th>%s</th>" % h for h in head), "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % x for x in r) for r in rows)))


def render_text(cards, scenarios=()):
    lines = []
    for col in COLUMNS:
        mine = [c for c in cards if c["상태"] == col]
        lines.append("%s (%d)" % (col, len(mine)))
        for c in mine:
            mark = "  (규모 %s)" % c["크기"] if c["크기"] else ""
            why = "  — %s" % c["멈춘이유"] if c["멈춘이유"] else ""
            lines.append("  · %s%s%s" % (c["title"], mark, why))
            here = phase_of(c["단계"])
            if here:
                strip = " › ".join("[%s]" % p if p == here else p for p in PHASES)
                tail = [c["단계"]] + [x for x in (elapsed(c["시작"], c["누적"], c["상태"]),) if x]
                lines.append("      %s   %s" % (strip, " · ".join(tail)))
            elif c["단계"]:
                lines.append("      %s" % c["단계"])
            if c["마지막줄"]:
                lines.append("      %s" % c["마지막줄"])
        lines.append("")
    unknown = [c for c in cards if c["상태"] not in COLUMNS]
    for c in unknown:
        lines.append("! 모르는 상태 「%s」 — %s" % (c["상태"], c["file"]))
    if unknown:
        lines.append("")
    marks = card_marks(cards, scenarios)
    for head, mine in scenario_groups(cards, scenarios):
        lines.append(head)
        for c in mine:
            lines.append("  · %s  [%s]" % (c["title"], c["상태"]))
            lines += ["      ! %s" % m for m in marks[c["file"]]]
    return "\n".join(lines)


def 승격_폼(project, c):
    """시나리오에 안 묶인 아이디어 카드에만 「대기로 올리기」 폼(확인 창 인라인). 카드 링크 `<a>` 안에는 폼을 못 넣으므로 밖에 둔다."""
    if c["상태"] != "아이디어" or c["시나리오"] or c.get("원격만"):       # 이 기기 사본에 파일이 없는 카드(다른 기기가 만든 것)는 단추가 고칠 파일이 없다
        return ""
    물음 = html.escape("return confirm(%s)" % json.dumps(
        "「%s」 카드를 %s 프로젝트의 대기로 올린다. 올리겠습니까?" % (c["title"], project), ensure_ascii=False), quote=True)
    return ('<form method="post" action="/p/%s/promote" onsubmit="%s"><input type="hidden" name="card" value="%s">'
            '<input type="hidden" name="confirm" value="1"><button type="submit">대기로 올리기</button></form>'
            % (urllib.parse.quote(project, safe=""), 물음, html.escape(c["file"], quote=True)))


앞면_최대 = 120      # 카드 앞면 아래 줄에 싣는 글자 수 — 넘으면 「실패한 검사 — 자세히」


def _한줄(cards, c, 누구, now):
    """멈춘 까닭 → 기다리는 것 → 예상 차이 → 「—」 가운데 첫 번째. (글, 갈래). `누구` = {카드 파일: 사람 차례}."""
    if c["멈춘이유"]:
        return c["멈춘이유"], "warn"
    if c["file"] in 누구:
        return 누구[c["file"]]["기다리는 것"], "warn"
    e = size_estimate(cards, c)["분"]
    if e is not None and c["상태"] not in ("대기", "아이디어"):
        d = actual_minutes(c, now) - e
        if d == 0:
            return "예상과 같음", ""
        return "예상보다 %s %s" % (_분글(abs(d)), "늦음" if d > 0 else "빠름"), "late" if d > 0 else "fast"
    return "—", ""


def _보드_고리(qs, **바꿈):
    """프로젝트 보드 주소 인자 `qs`(parse_qs 결과) 사본에 `바꿈` 을 넣은 `?…` 고리. 값이 빈 글이면 그 인자를 뺀다 — 다른 인자(보기·기간·다른 칩)는 그대로 싣는다."""
    q = {k: list(v) for k, v in qs.items()}
    for k, v in 바꿈.items():
        if v:
            q[k] = [v]
        else:
            q.pop(k, None)
    return "?" + urllib.parse.urlencode(q, doseq=True)


def _프로젝트_탭(name, 현재):
    """프로젝트 화면 넷의 머리 탭. `현재` 는 `board`·`features`·`graph`·`improve` 중 지금 화면 — 그 탭만 선택 표시.
    사이드바 `render_shell` 의 `sub` 가 같은 넷을 같은 순서·주소로 그린다. 바꿀 때 두 곳을 함께 고친다."""
    return '<div class="vt" role="tablist" aria-label="프로젝트 화면">%s</div>' % "".join(
        '<a role="tab" href="%s" aria-selected="%s"%s>%s</a>' % (
            html.escape(_주소(name) + 끝, quote=True), "true" if 키 == 현재 else "false", ' aria-current="page"' if 키 == 현재 else "", html.escape(화면이름[키]))
        for 키, 끝 in (("board", ""), ("features", "features"), ("graph", "graph"), ("improve", "improve")))


def render_html(cards, project, every=0, scenarios=(), turns=(), plan_dir=None, alarms=(), 단추=False, 거르개=None):
    """프로젝트 보드 화면 한 장(ISSUE-ui-2). `plan_dir` 는 끝 보고를 읽던 옛 시나리오 조각이 빠져 쓰이지 않지만 부르는 곳을 건드리지 않으려고 남긴다.
    칸 안 카드는 `<a class="card">` 바로 뒤에 `<template class="pk">`(엿보기 자료)가 붙는다 — 아래 TEMPLATE 의 스크립트가 이 순서를 안다. 바꿀 때 두 곳을 함께 고친다.

    `거르개` 는 서버 화면(`단추=True`)의 탭·필터 양식이 읽는 `{"qs": parse_qs 결과, "반려된": 파일 이름 집합 또는 None, "까닭": 글}` 이다. 정적 화면은 탭·필터 줄이 없고 거르지 않는다. 내 할 일 띠는 `turns` 와 `alarms` 를 `my_turns` 로 합쳐 한 줄로 그린다(ISSUE-ui-14)."""
    marks = card_marks(cards, scenarios)
    now = dt.datetime.now()
    esc = html.escape
    누구 = {}
    for t in turns:
        누구.setdefault(t["열어 볼 곳"], t)

    def 기계(c):
        return c.get("도는 기계") or c.get("돈기계") or c.get("기기") or "—"

    한줄 = lambda c: _한줄(cards, c, 누구, now)      # noqa: E731

    def card(c):
        stop = (bool(c["멈춘이유"]) or c["상태"] == "보류") and c["상태"] not in ("완료", "아이디어")
        here = phase_of(c["단계"])
        if c["상태"] in ("완료", "검토"):
            켜짐, 갈래, 이름 = len(PHASES), " done", c["상태"]
        elif c["상태"] in ("진행", "보류") and here:
            켜짐, 갈래, 이름 = PHASES.index(here) + 1, " stop" if stop else "", here + " 단계"
        else:
            켜짐, 갈래, 이름 = 0, "", c["상태"]
        pips = '<p class="pips%s" aria-label="%s">%s</p>' % (갈래, esc(이름), '<i class="on"></i>' * 켜짐 + "<i></i>" * (len(PHASES) - 켜짐))
        tag = "%s %s" % (c["시나리오"], ", ".join(c["요소"])) if c["시나리오"] else "시나리오 밖"
        if c["상태"] == "완료":                          # 완료: 갱신 날짜 · 기기뿐 — 멈춘이유·예상 대비는 상세에서
            글, 종류 = c["갱신"] or "—", ""
        elif c["상태"] == "보류":                        # 보류: 까닭 한 줄 — 「기다리는 것」·예상 대비는 붙지 않는다
            글, 종류 = c["멈춘이유"] or "까닭 없음 — 적어 주세요", "hold"
        else:
            글, 종류 = 한줄(c)
        if len(글) > 앞면_최대:                          # 명령줄·긴 출력은 앞면에 싣지 않는다 — 카드 전체가 상세로 가는 고리다
            글 = "실패한 검사 — 자세히"
        if c["상태"] == "아이디어":                      # 아이디어: 시나리오 표시 · 크기 · 제목뿐
            pips = 아래 = ""
        else:
            if c["상태"] == "보류":                      # 보류: 까닭이 한 줄 전체 폭, 기기는 그 아래 줄
                아래 = '<p class="ln wide"><span class="d %s">%s</span></p><p class="ln wide">%s</p>' % (종류, esc(글), esc(_기기_글(기계(c))))
            else:
                아래 = '<p class="ln"><span class="d %s">%s</span><span>%s</span></p>' % (종류, esc(글), esc(_기기_글(기계(c))))
        front = ('<a class="card" href="%s"%s><p class="tg"><span>%s</span><span>%s</span></p><h3>%s</h3>%s%s</a>'
                 % (esc(c["file"], quote=True), " data-stop" if stop else "", esc(tag.strip()), esc(c["크기"]), esc(_취소선_뺌(c["title"])),
                    pips, 아래))
        # 엿보기 자료 — 앞면에서 빠진 정보
        strip = ""
        if here:
            strip = '<p class="phases">%s</p>' % "".join(
                '<span class="%s">%s</span>' % ("on" if p == here else "off", p) for p in PHASES)
        bits = [x for x in (c["단계"], elapsed(c["시작"], c["누적"], c["상태"]),
                            "규모 %s" % c["크기"] if c["크기"] else "",
                            "갱신 %s" % c["갱신"] if c["갱신"] else "") if x]
        meta = '<p class="meta">%s</p>' % " · ".join(esc(x) for x in bits) if bits else ""
        e = size_estimate(cards, c)["분"]
        배지 = '<span class="badge b-gray">%s</span>' % esc(c["상태"])
        if c["멈춘이유"]:
            배지 += '<span class="badge b-orange">%s</span>' % esc(c["멈춘이유"])
        if c["크기"]:
            배지 += '<span class="badge b-gray">%s</span>' % esc(c["크기"])
        배지 += '<span class="badge b-gray">%s</span>' % esc(_기기_글(기계(c)))
        기다림 = 누구[c["file"]]["기다리는 것"] if c["file"] in 누구 else "—"
        why = "".join('<p class="why">%s</p>' % esc(m) for m in marks[c["file"]] + list(c.get("원격표시", [])))
        if _넘김(cards, c, now):
            why += '<p class="why">예상 넘김</p>'
        소식 = c.get("소식때")
        소식글 = " · 마지막 소식 %d분 전" % max(0, int((now - 소식).total_seconds() // 60)) if 소식 else ""    # 다른 기기가 돌리는 진행 카드 — 원격 머리말이 마지막으로 바뀐 때
        src = ('<p class="meta">실행 기기 %s · %s 기록%s</p>' % (esc(_기기_글(c["도는 기계"])), esc(c["출처"].replace("이 기계", "이 기기")), esc(소식글)) if "출처" in c else "")
        live = ('<p class="live">%s</p>' % esc(c["마지막줄"])) if c["마지막줄"] else ""
        pk = ('<template class="pk"><h2>%s</h2><p class="bg">%s</p><dl><dt>걸린 시간</dt><dd>%s · 예상 %s</dd><dt>비용</dt><dd>—</dd>'
              '<dt>기다리는 것</dt><dd>%s</dd><dt>고치는 파일</dt><dd>—</dd></dl>%s%s%s%s%s<a class="open" href="%s">상세 열기</a></template>'
              % (esc(_취소선_뺌(c["title"])), 배지, _분글(actual_minutes(c, now)), _분글(e) if e is not None else "—", esc(기다림),
                 strip, meta, src, why, live, esc(c["file"], quote=True)))
        return front + pk

    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())

    def 이번주(c):
        try:
            return dt.datetime.strptime(c["갱신"], "%Y-%m-%d").date() >= monday
        except ValueError:
            return False

    거 = 거르개 or {}
    qs = 거.get("qs") or {}
    g1 = lambda k: (qs.get(k) or [""])[0].strip()      # noqa: E731
    보기 = g1("보기") if 단추 and g1("보기") in ("목록", "시나리오") else "보드"
    기계값, 시나리오값 = [], sorted({s["번호"] for s in scenarios} | {c["시나리오"] for c in cards if c["시나리오"]})
    for c in cards:
        if 기계(c) not in ("—", "기기 모름") and 기계(c) not in 기계값:
            기계값.append(기계(c))
    반려된 = 거.get("반려된")
    켜진 = 단추 and bool(g1("기계") or g1("시나리오") or g1("반려") == "1" or g1("찾기"))

    def 맞나(c):
        return ((not g1("기계") or 기계(c) == g1("기계"))
                and (not g1("시나리오") or c["시나리오"] == (g1("시나리오") if g1("시나리오") != "시나리오 밖" else ""))
                and (g1("반려") != "1" or 반려된 is None or c["file"] in 반려된)
                and (not g1("찾기") or g1("찾기").lower() in c["title"].lower() or g1("찾기").lower() in c["file"].lower()))
    보임 = [c for c in cards if 맞나(c)] if 단추 else cards

    cols = ""
    for col in ("아이디어", "대기", "진행", "검토", "완료", "보류"):
        mine = [c for c in 보임 if c["상태"] == col]
        if not mine:
            cols += '<section class="col c-%s"><h2>%s <span>0</span></h2></section>' % (col, col)
            continue
        shown = "".join(card(c) for c in mine[:4] if col == "완료") if col == "완료" else "".join(card(c) for c in mine)
        if col == "완료" and len(mine) > 4:
            shown += ('<details class="more"><summary>%d장 더 보기 · 이번 주 %d</summary>%s</details>'
                      % (len(mine) - 4, len([c for c in mine if 이번주(c)]), "".join(card(c) for c in mine[4:])))
        cols += '<section class="col c-%s"><h2>%s <span>%d</span></h2>%s</section>' % (col, col, len(mine), shown)

    def 띠(s):
        여는 = '<a class="sx" href="plan/%s">' % esc(s["file"], quote=True) if 단추 else '<div class="sx">'
        닫는 = "</a>" if 단추 else "</div>"
        if s["오류"]:
            return '%s<span class="sid">%s</span><span class="sn">%s</span><span class="why">%s</span>%s' % (
                여는, esc(s["번호"]), esc(_취소선_뺌(s["이름"])), esc(s["오류"]), 닫는)
        p = scenario_progress(cards, s)
        pct = p["퍼센트"] or 0
        return ('%s<span class="sid">%s</span><span class="sn">%s</span>'
                '<div class="tr2"><i%s style="width:%d%%"></i></div><span class="sf">%d/%d</span>%s'
                % (여는, esc(s["번호"]), esc(_취소선_뺌(s["이름"])), ' class="full"' if pct == 100 else "", pct, p["수"]["완료"], p["분모"], 닫는))

    strip = '<div class="strip">%s</div>' % ("".join(띠(s) for s in scenarios) or '<p class="meta">시나리오 없음</p>')
    if 단추 and g1("반려") == "1" and 반려된 is None and 거.get("까닭"):
        strip = '<p class="why">%s</p>' % esc(거["까닭"]) + strip
    if 보기 == "목록" and 보임:
        cols = '<div class="lv">%s</div>' % _표(["상태", "제목", "크기", "걸린 시간", "기기"], [
            [esc(c["상태"]), '<a href="%s">%s</a>' % (esc(c["file"], quote=True), esc(c["title"])), esc(c["크기"]),
             _분글(actual_minutes(c, now)), esc(_기기_글(기계(c)))] for c in 보임])
    elif 보기 == "시나리오" and 보임:
        번호들 = {s["번호"] for s in scenarios}
        묶음 = "".join('<section class="sg">%s%s</section>' % (띠(s), "".join(card(c) for c in 보임 if c["시나리오"] == s["번호"]))
                     for s in scenarios if not g1("시나리오") or s["번호"] == g1("시나리오"))
        낱장 = [c for c in 보임 if c["시나리오"] not in 번호들]
        cols = 묶음 + ('<section class="sg"><h2>시나리오 밖 카드</h2>%s</section>' % "".join(card(c) for c in 낱장) if 낱장 else "")
        strip = ""
    if 단추 and not 보임 and (켜진 or cards):
        cols = '<p class="meta lv">%s</p>' % ('필터에 맞는 카드 없음 · <a href="%s">필터 풀기</a>' % esc(_보드_고리(qs, 기계="", 시나리오="", 반려="", 찾기=""), quote=True)
                                              if 켜진 else "카드 없음")
        strip = "" if 보기 == "시나리오" else strip

    행 = [{"name": project, "카드": cards, "경보": [{**a, "프로젝트": project, "열어 볼 곳": a["file"]} for a in alarms]}]
    내것 = my_turns(행, [{**t, "프로젝트": project} for t in turns])      # UI-12 정의 — 미룬 보류는 빠지고 멈춘 카드 경보가 합쳐진다

    def 띠_항목(t):
        why = '<p class="why">알림 못 띄움</p>' if t.get("알림") is False else ""
        return '<li><a href="%s">%s</a> — %s%s</li>' % (esc(t["열어 볼 곳"], quote=True), esc(t["제목"]), esc(t["기다리는 것"]), why)

    if 내것:
        box = ('<div class="tb"><h2>%s (%d)</h2>%s%s<details><summary>까닭 보기</summary><ul>%s</ul></details></div>\n' % (
            esc(화면이름["todo"]), len(내것),
            "".join('<a href="%s">%s</a>' % (esc(t["열어 볼 곳"], quote=True), esc(t["제목"])) for t in 내것[:NOW_MAX]),
            "<span>외 %d건</span>" % (len(내것) - NOW_MAX) if len(내것) > NOW_MAX else "",
            "".join(띠_항목(t) for t in 내것)))
    else:
        box = '<div class="tb"><h2>%s 없음</h2></div>\n' % esc(화면이름["todo"])

    def 탭(글, 값):
        return '<a role="tab" href="%s" aria-selected="%s">%s</a>' % (esc(_보드_고리(qs, 보기=값), quote=True), "true" if 보기 == (값 or "보드") else "false", 글)
    p = esc(project)
    def 고르개(이름, 글, 값들, 지금, 보임글=lambda v: v):
        옵션 = '<option value="">전체</option>' + "".join(
            '<option value="%s"%s>%s</option>' % (esc(v, quote=True), " selected" if v == 지금 else "", esc(보임글(v))) for v in 값들)
        return '<label>%s <select name="%s" onchange="this.form.submit()">%s</select></label>' % (글, 이름, 옵션)

    조작 = ""
    if 단추:
        밖 = "시나리오 밖" if any(not c["시나리오"] for c in cards) or g1("시나리오") == "시나리오 밖" else None
        모름 = "기기 모름" if any(기계(c) == "기기 모름" for c in cards) or g1("기계") == "기기 모름" else None
        숨은 = "".join('<input type="hidden" name="%s" value="%s">' % (esc(k, quote=True), esc(v, quote=True))
                     for k, vs in qs.items() if k not in ("시나리오", "기계", "반려", "찾기", "빈칸") for v in vs)      # 빈칸 은 없어진 인자 — 싣지 않는다
        조작 = (_프로젝트_탭(project, "board") + '<div class="vt" role="tablist" aria-label="보기">%s%s</div>'
              '<form method="get" class="flt" aria-label="필터">%s%s%s%s<input type="search" name="찾기" value="%s" aria-label="이 보드에서 찾기" placeholder="이 보드에서 찾기"></form>' % (
                  탭("보드", ""), 탭("목록", "목록"),
                  고르개("시나리오", "시나리오", 시나리오값 + ([밖] if 밖 else []), g1("시나리오")),
                  고르개("기계", "기기", 기계값 + ([모름] if 모름 else []), g1("기계"), _기기_글),
                  '<label><input type="checkbox" name="반려" value="1"%s onchange="this.form.submit()">반려 있었던 것만</label>' % (
                      " checked" if g1("반려") == "1" else ""),
                  숨은, esc(g1("찾기"), quote=True)))
    head = '%s<h1>%s</h1><span class="cnt">시나리오 %d · 카드 %d</span>%s' % (
        '<a href="/">%s</a><span aria-hidden="true">/</span>' % esc(화면이름["home"]) if 단추 else "", p, len(scenarios), len(cards), 조작)
    return _화면들_채우기(TEMPLATE) % {"project": p, "head": head, "cols": cols, "strip": strip,
                       "turns": box,
                       "now": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "refresh": ('\n<meta http-equiv="refresh" content="%d">' % every) if every else ""}


# ── 색·글꼴·모서리와 공통 틀 규칙 (ISSUE-ui-1-shell-home-2026-11-22) ──
# 색 글자(16진수 색·rgba 함수)는 이 덩이 안에만 적는다 — 시험(test_board_shell S-01)이 이 파일에서 센다. 화면 틀 셋의 `<style>` 머리에 그대로 들어가므로 `%` 글자를 쓰지 않는다.
THEME = """:root {
  --bg:#f7f7f5; --panel:#fff; --line:#e6e6e3; --fg:#1a1a1a; --dim:#5f6368;
  --blue:#2f5bd3; --orange:#c2410c; --teal:#0f766e; --gray:#8a8f98;
  --pj-0:#7c4dbd; --pj-1:#c0398a; --pj-2:#27337a; --pj-3:#6b7a1f;
  --side:#fbfbfa; --soft:#ececea; --line2:#efefed; --ink2:#333;
  --ochre:#b08a2e; --ochre-bg:#fbf3dc; --ochre-ink:#6b531a;
  --blue-bg:#eaf0fd; --orange-bg:#fdf0e8; --teal-bg:#e7f4f2; --gray-bg:#f2f2f0; --orange-line:#f1c6ad; --orange-soft:#fffaf6;
  --card:var(--panel); --off:#e3e3e0; --dash:#dcdcd8;
  --st-아이디어:var(--gray); --st-대기:var(--blue); --st-진행:var(--blue); --st-검토:var(--blue); --st-보류:var(--ochre); --st-완료:var(--teal);
  --overlay:rgba(0,0,0,.35); --today:#c0392b;
  --ph-분석:#9db5ef; --ph-설계:#5f84e0; --ph-멈춤:#b0b3b8; --lane-head:#f3f3f1;
  --sans:'Noto Sans KR', system-ui, -apple-system, 'Apple SD Gothic Neo', sans-serif;
  --mono:'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  --r-panel:12px; --r-card:10px; --r-pill:999px; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#171614; --panel:#201f1c; --line:#302e2a; --fg:#eceae6; --dim:#9a958d;
  --blue:#8aa4e8; --orange:#e8895f; --teal:#4fb3a8; --gray:#8a8f98;
  --pj-0:#b197e0; --pj-1:#e58bb8; --pj-2:#7a86d6; --pj-3:#a9b552;
  --side:#1b1a18; --soft:#2a2926; --line2:#2a2926; --ink2:#d6d3cd;
  --ochre:#d1a94a; --ochre-bg:#352d1c; --ochre-ink:#e0c27a;
  --blue-bg:#232b3f; --orange-bg:#3a2a22; --teal-bg:#1f3330; --gray-bg:#2a2926; --orange-line:#6b3f2c; --orange-soft:#2e231d;
  --off:#33312d; --dash:#3a3834;
  --ph-분석:#5a6f9e; --ph-설계:#6f8fdc; --ph-멈춤:#5c5f64; --lane-head:#25241f; } }
* { box-sizing: border-box; }
body.with-side { padding:0; }
.shell { display:flex; flex-wrap:wrap; min-height:100vh; }
.page { flex:999 1 640px; min-width:0; padding:24px 24px 48px; }
nav.side { flex:1 1 232px; max-width:260px; padding:20px 14px; border-right:1px solid var(--line); background:var(--side);
  display:flex; flex-direction:column; gap:22px; font-size:14px; line-height:1.5; }
nav.side .brand { display:flex; align-items:center; gap:10px; padding:0 6px; }
nav.side .logo { width:26px; height:26px; border-radius:6px; background:var(--fg); color:var(--panel); display:flex;
  align-items:center; justify-content:center; font-size:12px; font-weight:700; }
nav.side .brand b { display:block; font-weight:600; }
nav.side .brand small { display:block; font-size:12px; color:var(--dim); }
nav.side button#pk { display:flex; align-items:center; gap:8px; min-height:36px; padding:0 10px; border:1px solid var(--line);
  border-radius:8px; background:var(--panel); color:var(--dim); font:inherit; font-size:13px; cursor:pointer; text-align:left; }
nav.side button#pk span { flex-grow:1; }
nav.side button#pk kbd { font:11px var(--mono); border:1px solid var(--line); border-radius:4px; padding:1px 5px; }
nav.side .grp { display:flex; flex-direction:column; gap:2px; }
nav.side .gh { font-size:11px; font-weight:600; letter-spacing:.04em; color:var(--dim); padding:0 10px 6px; }
nav.side a, nav.side .nolink { display:flex; align-items:center; gap:10px; min-height:36px; padding:0 10px; border-radius:8px;
  color:var(--ink2); text-decoration:none; font-size:14px; }
nav.side .nolink { color:var(--dim); }
nav.side a:hover { background:var(--soft); }
nav.side a[aria-current="page"] { background:var(--soft); color:var(--fg); font-weight:600; }
nav.side .n { margin-left:auto; font-size:12px; color:var(--dim); font-weight:400; }
nav.side .n.hot { color:var(--orange); font-weight:600; }
nav.side .pn { flex-grow:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
nav.side .sbox { margin-top:auto; padding:10px; border:1px solid var(--line); border-radius:8px; background:var(--panel);
  font-size:12px; color:var(--dim); display:flex; flex-direction:column; gap:4px; }
nav.side .sbox .warn { color:var(--orange); font-weight:600; }
.dot-orange, .dot-blue, .dot-gray, .dot-teal, .dot-ochre { display:inline-block; width:8px; height:8px; flex-shrink:0; border-radius:var(--r-pill); }
.dot-orange { background:var(--orange); } .dot-blue { background:var(--blue); } .dot-gray { background:var(--gray); } .dot-teal { background:var(--teal); } .dot-ochre { background:var(--ochre); }
nav.side .sub { display:flex; flex-direction:column; gap:2px; padding-left:18px; }
nav.side .sub a { min-height:32px; font-size:13px; }
.top h1 { margin:0; font-size:22px; font-weight:700; color:var(--fg); }
.top p { margin:4px 0 0; color:var(--dim); font-size:13px; }
.tiles { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:12px; margin:24px 0; }
.tile { display:flex; flex-direction:column; gap:6px; padding:16px 18px; border:1px solid var(--line); border-radius:var(--r-panel);
  background:var(--panel); color:var(--fg); text-decoration:none; }
.tile:hover { text-decoration:none; border-color:var(--dim); }
.tile .lb, .tile .sub { font-size:12px; color:var(--dim); }
.tile .v { font-size:28px; font-weight:700; line-height:1.1; }
.tile.risk { border-color:var(--orange-line); }
.tile.risk .v { color:var(--orange); }
.tile.good .v { color:var(--teal); }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-panel); margin:0 0 24px; min-width:0; }
.ph { display:flex; flex-wrap:wrap; align-items:center; gap:12px; padding:14px 18px; border-bottom:1px solid var(--line2); }
.ph h2 { display:block; margin:0; font-size:15px; font-weight:600; color:var(--fg); flex-grow:1; }
.ph .hint { font-size:12px; color:var(--dim); }
.progress table { color:var(--ink2); }
.progress th { padding:10px 12px; font-weight:500; font-size:12px; color:var(--dim); text-align:left; border:0; white-space:nowrap; }
.progress td { padding:12px; border:0; border-top:1px solid var(--line2); vertical-align:middle; white-space:normal; }
.progress td.pn { padding-left:18px; }
.progress td a.pl { color:var(--fg); font-weight:600; }
.progress .sub { font-size:12px; color:var(--dim); }
.badge { display:inline-flex; align-items:center; gap:6px; font-size:12px; padding:2px 8px; border-radius:var(--r-pill); white-space:nowrap; }
.b-orange { background:var(--orange-bg); color:var(--orange); } .b-blue { background:var(--blue-bg); color:var(--blue); }
.b-ochre { background:var(--ochre-bg); color:var(--ochre-ink); }
.b-teal { background:var(--teal-bg); color:var(--teal); } .b-gray { background:var(--gray-bg); color:var(--dim); }
.pbar { display:flex; align-items:center; gap:10px; min-width:160px; }
.pbar .tr { flex-grow:1; height:6px; border-radius:3px; background:var(--soft); overflow:hidden; }
.pbar .tr i { display:block; height:6px; background:var(--blue); }
.pbar .tr i.full { background:var(--teal); } .pbar .tr i.zero { background:var(--gray); }
.pbar span { font:12px var(--mono); color:var(--dim); }
.pbar .pb2 { display:flex; flex-grow:1; height:6px; border-radius:3px; background:var(--soft); overflow:hidden; }
.pb2 .seg { display:block; height:6px; }
.seg-done { background:var(--teal); } .seg-doing { background:var(--blue); } .seg-hold { background:var(--ochre); }
.seg-stop { background:var(--orange); } .seg-idea { background:var(--gray); opacity:.5; } .seg-none { background:var(--line); }
.progress .fl { font-size:12px; }
.duo { display:flex; flex-wrap:wrap; gap:24px; align-items:flex-start; }
.duo > .panel { margin:0 0 24px; }
.duo > .turnp { flex:3 1 520px; } .duo > .feedp { flex:2 1 340px; }
.tabs { display:flex; gap:4px; margin-left:auto; }
.tabs a { min-height:32px; display:inline-flex; align-items:center; padding:0 12px; border-radius:6px; font-size:12px; color:var(--dim); }
.tabs a.on { background:var(--soft); color:var(--fg); font-weight:600; }
.tlist, .flist { list-style:none; margin:0; padding:4px 0; }
.turn { display:flex; align-items:center; gap:14px; padding:12px 18px; border-top:1px solid var(--line2); }
.turn:first-child { border-top:0; }
.kind { min-width:84px; font-size:11px; font-weight:600; padding:3px 8px; border-radius:6px; text-align:center; }
.k-orange { background:var(--orange-bg); color:var(--orange); } .k-blue { background:var(--blue-bg); color:var(--blue); }
.k-teal { background:var(--teal-bg); color:var(--teal); } .k-gray { background:var(--gray-bg); color:var(--ink2); }
.tbody { flex-grow:1; min-width:0; }
.tt { font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.tm { font-size:12px; color:var(--dim); }
.tact { flex-shrink:0; min-height:32px; display:inline-flex; align-items:center; padding:0 12px; border:1px solid var(--line);
  border-radius:8px; font-size:12px; color:var(--fg); }
.more { padding:10px 18px 14px; font-size:12px; color:var(--dim); }
.act { display:flex; gap:12px; padding:8px 18px; font-size:13px; }
.fd { flex-shrink:0; width:8px; height:8px; margin-top:7px; border-radius:var(--r-pill); }
.fd-teal { background:var(--teal); } .fd-orange { background:var(--orange); } .fd-blue { background:var(--blue); }
.mach { display:flex; flex-wrap:wrap; gap:18px; align-items:center; padding:14px 18px; }
.mach h2 { margin:0; display:block; font-size:15px; font-weight:600; color:var(--fg); }
.pill { display:flex; align-items:center; gap:10px; padding:8px 12px; border:1px solid var(--line2); border-radius:var(--r-card); }
.pill b { font-weight:600; } .pill .tm { font-size:12px; }
.mach .why { flex-basis:100vw; }
@media (max-width:700px) {
  nav.side { max-width:none; flex-basis:100vw; border-right:0; border-bottom:1px solid var(--line); }
  .page { padding:16px 14px 40px; }
  .tiles { grid-template-columns:repeat(2, minmax(0, 1fr)); }
  .turn { flex-wrap:wrap; }
}"""


FILE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(name)s</title>
<style>
@@THEME@@
body { margin:0; padding:24px 16px 64px; background:var(--bg); color:var(--fg);
  font:15px/1.6 var(--sans); }
main { max-width:820px; margin:0 auto; }
a { color:var(--dim); font-size:13px; text-decoration:none; }
a:hover { color:var(--fg); }
.vt { display:flex; flex-wrap:wrap; gap:2px; margin:0 0 12px; padding:2px; width:fit-content; max-width:100%%; border-radius:8px; background:var(--soft); }
.vt a[role="tab"] { display:inline-flex; align-items:center; min-height:30px; padding:0 12px; border-radius:6px; font-size:12px; color:var(--dim); }
.vt a[aria-selected="true"] { background:var(--panel); color:var(--fg); font-weight:600; }
h1 { font-size:15px; margin:12px 0 16px; color:var(--dim); font-weight:600; }
pre { white-space:pre-wrap; word-break:break-word; margin:0 0 16px; padding:16px;
  border:1px solid var(--line); border-radius:var(--r-card);
  font:13px/1.7 var(--mono); }
.md h1, .md h2, .md h3, .md h4, .md h5, .md h6 { color:var(--fg); margin:24px 0 8px; line-height:1.35; }
.md h1 { font-size:22px; } .md h2 { font-size:18px; } .md h3 { font-size:16px; }
.md h4, .md h5, .md h6 { font-size:15px; }
.md p { margin:0 0 12px; }
.md ul, .md ol { margin:0 0 12px; padding-left:24px; }
.md table { border-collapse:collapse; margin:0 0 16px; font-size:14px; display:block; max-width:100%%; overflow-x:auto; }
.md th, .md td { border:1px solid var(--line); padding:6px 10px; text-align:left; vertical-align:top; }
.md th { color:var(--dim); font-weight:600; }
main.cd { max-width:1200px; overflow-wrap:anywhere; }
.cd .crumb { margin:0 0 8px; font-size:12px; color:var(--dim); }
.cd .badges, .cd .btns { display:flex; flex-wrap:wrap; gap:8px; margin:0 0 8px; }
.cd .btns { margin:8px 0 16px; }
.cd h1 { font-size:22px; font-weight:700; color:var(--fg); margin:4px 0; }
.cd .sub { margin:0; font-size:13px; color:var(--dim); }
.cd .btn { font-size:12px; padding:4px 10px; border:1px solid var(--line); border-radius:var(--r-pill); background:var(--panel); color:var(--fg); }
.cd .badge { display:inline-flex; font-size:12px; padding:2px 8px; border-radius:var(--r-pill); white-space:nowrap; }
.cd .b-orange { background:var(--orange-bg); color:var(--orange); } .cd .b-blue { background:var(--blue-bg); color:var(--blue); }
.cd .b-teal { background:var(--teal-bg); color:var(--teal); } .cd .b-gray { background:var(--gray-bg); color:var(--dim); }
.cd .tiles { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:12px; margin:16px 0; }
.cd .tile { display:flex; flex-direction:column; gap:4px; padding:12px 16px; border:1px solid var(--line); border-radius:var(--r-panel); background:var(--panel); }
.cd .tile .lb, .cd .tile .sub { font-size:12px; color:var(--dim); }
.cd .tile .v { font-size:24px; font-weight:700; line-height:1.1; }
.cd .tile.risk { border-color:var(--orange-line); } .cd .tile.risk .v { color:var(--orange); }
.cd .panel { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-panel); margin:0 0 16px; min-width:0; }
.cd .ph { display:flex; flex-wrap:wrap; align-items:center; gap:12px; padding:12px 18px; border-bottom:1px solid var(--line2); }
.cd .ph h2 { margin:0; font-size:15px; font-weight:600; flex-grow:1; }
.cd .hint, .cd .more { font-size:12px; color:var(--dim); }
.cd .more { padding:12px 18px; margin:0; }
.cd .rail { display:flex; gap:10px; padding:14px 18px; overflow-x:auto; }
.cd .stg { flex:1 0 96px; display:flex; flex-direction:column; gap:6px; padding:10px; border:1px solid var(--line); border-radius:var(--r-card); }
.cd .stg.rej { border-color:var(--orange-line); background:var(--orange-soft); }
.cd .stg .hd { display:flex; gap:6px; font-size:12px; } .cd .stg .no { font-weight:700; } .cd .stg .nm { color:var(--dim); }
.cd .stg .trk { position:relative; height:6px; border-radius:3px; background:var(--soft); }
.cd .stg .bar { display:block; height:100%%; border-radius:3px; background:var(--blue); } .cd .stg.rej .bar { background:var(--orange); }
.cd .stg .md { position:absolute; top:-3px; bottom:-3px; width:2px; background:var(--fg); }
.cd .stg .tm { font-size:13px; font-weight:600; } .cd .stg .memo { font-size:12px; color:var(--dim); }
.cd .stg.rej .memo { color:var(--orange); }
.cd .cols { display:grid; grid-template-columns:minmax(0, 2fr) minmax(0, 1fr); gap:16px; }
.cd .flist { margin:0; padding:8px 18px; font-size:13px; }
.cd .flist > div { display:flex; flex-wrap:wrap; gap:8px; align-items:baseline; padding:6px 0; border-top:1px solid var(--line2); }
.cd .flist > div:first-child { border-top:0; }
.cd .wh { font:12px var(--mono); color:var(--dim); }
.sr-box { display:flex; align-items:center; gap:10px; min-height:44px; padding:0 14px; margin:0 0 12px; border:1px solid var(--line2); border-radius:var(--r-panel); background:var(--panel); }
.sr-box input { flex-grow:1; min-width:0; border:0; outline:0; font:inherit; font-size:15px; background:transparent; color:var(--fg); }
.sr-cnt { font-size:12px; color:var(--dim); white-space:nowrap; }
.sr-sub { position:absolute; left:-9999px; }
.sr-chips { display:flex; flex-wrap:wrap; gap:6px; margin:0 0 12px; }
.sr-chip { display:inline-flex; align-items:center; min-height:32px; padding:0 12px; box-sizing:border-box; border:1px solid var(--line); border-radius:var(--r-pill); background:var(--panel); color:var(--ink2); font-size:12px; cursor:pointer; list-style:none; }
.sr-chip.on { background:var(--fg); border-color:var(--fg); color:var(--bg); }
.sr-d[open] { flex-basis:100%%; } .sr-d label { display:inline-block; margin:8px 12px 0 0; font-size:12px; color:var(--dim); }
.sr-flt { margin:0 0 12px; font-size:12px; color:var(--dim); }
.sr-g { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-panel); margin:0 0 14px; min-width:0; }
.sr-h { display:flex; align-items:center; gap:8px; padding:10px 16px; border-bottom:1px solid var(--line2); font-size:12px; }
.sr-h b { font-weight:600; } .sr-n { color:var(--dim); } .sr-more { margin-left:auto; font-size:12px; }
.sr-r { display:flex; flex-direction:column; gap:4px; padding:10px 16px; border-top:1px solid var(--line2); color:var(--fg); font-size:14px; overflow-wrap:anywhere; }
.sr-h + .sr-r { border-top:0; }
.sr-meta { display:flex; align-items:center; gap:8px; font-size:12px; color:var(--dim); }
.sr-b { padding:1px 6px; border-radius:4px; background:var(--gray-bg); color:var(--ink2); }
.sr-t { font-weight:500; }
.sr-l { font:12px var(--mono); color:var(--ink2); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.sr-g mark { background:var(--orange-bg); color:inherit; border-radius:2px; padding:0 1px; }
@media (max-width:700px) { .cd .tiles { grid-template-columns:repeat(2, minmax(0, 1fr)); } .cd .cols { grid-template-columns:minmax(0, 1fr); } }
</style></head><body><main>
<a href="board.html">← 보드</a>
<h1>%(name)s</h1>
<div class="md">%(body)s</div>
</main></body></html>
"""

HOME_TEMPLATE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>작업 보드 — 이 기기</title>
<style>
@@THEME@@
body { margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--fg); font:14px/1.5 var(--sans); }
a { color:var(--blue); text-decoration:none; }
a:hover { text-decoration:underline; }
.progress table { border-collapse:collapse; width:100%%; font-size:13px; min-width:720px; }
.progress { overflow:auto; max-height:520px; }   /* 줄이 몇이든 첫 화면 900px 안에서 끝나게 — 넘친 줄은 상자 안에서 민다 */
.progress th { position:sticky; top:0; background:var(--panel); }
.bar { display:inline-flex; width:120px; height:8px; vertical-align:middle; background:var(--line);
  border-radius:4px; overflow:hidden; }
.bar > span { display:block; height:100%%; background:var(--st-완료); }
.bar > .아이디어 { background:var(--st-아이디어); } .bar > .대기 { background:var(--st-대기); } .bar > .진행 { background:var(--st-진행); }
.bar > .검토 { background:var(--st-검토); } .bar > .보류 { background:var(--st-보류); } .bar > .완료 { background:var(--st-완료); }
.why { margin:6px 0 0; font-size:12px; color:var(--orange); }
.meta { margin:6px 0 0; font-size:12px; color:var(--dim); }
</style></head><body>
<header class="top"><h1>%(title)s</h1><p>%(sub)s</p>%(err)s</header>
%(numbers)s%(progress)s<div class="duo">%(turns)s%(feed)s</div>
%(machines)s
</body></html>
"""

TEMPLATE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">%(refresh)s
<title>작업 보드 — %(project)s</title>
<style>
@@THEME@@
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.6 var(--sans); }
.bh { display:flex; flex-wrap:wrap; align-items:center; gap:12px; padding:16px 20px; border-bottom:1px solid var(--line); background:var(--panel); }
.bh h1 { font-size:18px; margin:0; font-weight:700; }
.bh a { font-size:13px; color:var(--dim); text-decoration:none; }
.cnt { font-size:12px; color:var(--dim); }
.vt { display:flex; flex-wrap:wrap; gap:2px; margin-left:12px; padding:2px; border-radius:8px; background:var(--soft); }
.vt button { min-height:30px; padding:0 12px; border:0; border-radius:6px; background:transparent; font:inherit; font-size:12px; color:var(--dim); }
.vt a { display:inline-flex; align-items:center; min-height:30px; padding:0 12px; border-radius:6px; font-size:12px; color:var(--dim); }
.vt button[aria-selected="true"], .vt a[aria-selected="true"] { background:var(--panel); color:var(--fg); font-weight:600; }
.sg { flex:0 0 300px; min-width:0; display:flex; flex-direction:column; gap:8px; }
.lv { flex:1 1 auto; min-width:0; }
.note { margin:8px 20px 0; font-size:12px; color:var(--dim); }
.bd { display:flex; flex-wrap:wrap; align-items:flex-start; }
.bm { flex:999 1 720px; min-width:0; padding:16px 20px; display:flex; flex-direction:column; gap:16px; }
#peek { flex:1 1 340px; max-width:380px; padding:20px 22px; border-left:1px solid var(--line); background:var(--panel);
  display:flex; flex-direction:column; gap:14px; }
#peek[hidden] { display:none; }
.pkh { display:flex; align-items:center; gap:8px; font-size:12px; color:var(--dim); }
.pkh button { margin-left:auto; width:32px; height:32px; border:0; border-radius:6px; background:var(--soft); color:var(--fg); font:inherit; }
#pkb h2 { display:block; margin:0 0 12px; font-size:16px; color:var(--fg); }
#pkb .bg { display:flex; flex-wrap:wrap; gap:6px; margin:0 0 12px; }
#pkb dl { display:grid; grid-template-columns:96px 1fr; gap:8px 12px; margin:0 0 12px; font-size:13px; }
#pkb dt { color:var(--dim); } #pkb dd { margin:0; }
#pkb .open { display:inline-flex; align-items:center; min-height:36px; padding:0 14px; border-radius:8px; background:var(--fg); color:var(--panel);
  font-size:13px; font-weight:500; text-decoration:none; }
.strip { display:flex; flex-wrap:wrap; gap:12px; }
.sx { color:inherit; text-decoration:none; flex:1 1 280px; min-width:0; display:flex; align-items:center; gap:12px; padding:12px 14px; border:1px solid var(--line);
  border-radius:var(--r-card); background:var(--panel); }
.sid { font:12px var(--mono); color:var(--dim); }
.sn { flex-grow:1; min-width:0; font-weight:500; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.tr2 { width:90px; height:6px; border-radius:3px; background:var(--soft); overflow:hidden; }
.tr2 i { display:block; height:6px; background:var(--blue); } .tr2 i.full { background:var(--teal); }
.sf { font:12px var(--mono); color:var(--dim); }
main { display:flex; gap:12px; align-items:flex-start; overflow-x:auto; padding-bottom:8px; }
.col { flex:1 1 0; min-width:170px; display:flex; flex-direction:column; gap:8px; }
.col h2 { display:flex; justify-content:flex-start; align-items:center; gap:8px; margin:0; padding:0 4px 4px; font-size:14px; color:var(--fg); font-weight:600; }
.col h2::before { content:""; width:8px; height:8px; border-radius:50%%; background:var(--gray); }
.col h2 span { font-size:12px; color:var(--dim); font-weight:400; }
.c-아이디어 h2::before { background:var(--st-아이디어); } .c-대기 h2::before { background:var(--st-대기); }
.c-진행 h2::before { background:var(--st-진행); } .c-검토 h2::before { background:var(--st-검토); }
.c-보류 h2::before { background:var(--st-보류); } .c-완료 h2::before { background:var(--st-완료); }
.tb { position:relative; display:flex; flex-wrap:nowrap; align-items:center; gap:10px; padding:8px 12px; border:1px solid var(--line);
  border-radius:var(--r-card); background:var(--panel); white-space:nowrap; font-size:13px; }
.tb h2 { font-size:13px; margin:0; color:var(--dim); font-weight:600; }
.tb > a { min-width:0; max-width:16em; overflow:hidden; text-overflow:ellipsis; color:var(--fg); text-decoration:none; padding:2px 8px; border-radius:var(--r-pill); background:var(--soft); }
.tb > span { color:var(--dim); font-size:12px; }
.tb details { margin-left:auto; }
.tb summary { cursor:pointer; color:var(--dim); font-size:12px; }
.tb details ul { position:absolute; right:0; top:100%%; z-index:5; margin:4px 0 0; padding:10px 14px 10px 28px; min-width:320px; max-width:90vw;
  white-space:normal; border:1px solid var(--line); border-radius:var(--r-card); background:var(--panel); }
.flt { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-left:auto; font-size:12px; color:var(--ink2); }
.flt select, .flt input[type="search"] { min-height:32px; border:1px solid var(--line); border-radius:var(--r-pill); background:var(--panel); font:inherit; font-size:12px; color:var(--ink2); padding:0 10px; }
.card { display:flex; flex-direction:column; gap:8px; background:var(--card); border:1px solid var(--line);
  border-radius:var(--r-card); padding:12px; margin:0; text-decoration:none; color:inherit; }
.card:hover { border-color:var(--dim); }
.card[data-stop] { border-color:var(--orange-line); }
.c-보류 .card { background:var(--ochre-bg); border-color:var(--ochre); }
.d.hold { color:var(--ochre-ink); }
.card h3 { font-size:13px; margin:0; font-weight:500; line-height:1.45; }
.tg, .ln { display:flex; gap:8px; margin:0; font-size:12px; color:var(--dim); }
.tg { font-size:11px; align-items:center; } .tg span:first-child { font-family:var(--mono); }
.tg span:last-child, .ln span:last-child { margin-left:auto; }
.ln.wide { display:block; } .ln.wide span:last-child { margin-left:0; } .ln.wide .d { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.d.warn, .d.late { color:var(--orange); font-weight:500; } .d.fast { color:var(--teal); font-weight:500; }
.pips { display:flex; gap:3px; margin:0; }
.pips i { flex:1; height:4px; border-radius:2px; background:var(--off); }
.pips i.on { background:var(--blue); } .pips.stop i.on { background:var(--orange); } .pips.done i.on { background:var(--teal); }
.col .more { padding:0; font-size:12px; color:var(--dim); display:flex; flex-direction:column; gap:8px; }
.more summary { min-height:36px; display:flex; align-items:center; justify-content:center; border:1px dashed var(--dash); border-radius:var(--r-card); cursor:pointer; margin-bottom:8px; }
.more[open] summary { margin-bottom:0; }
.col form { margin:0; }
.phases { display:flex; gap:3px; margin:8px 0 0; }
.phases span { flex:1; font-size:10px; text-align:center; padding:3px 0; border-radius:4px;
  border:1px solid var(--line); color:var(--dim); }
.phases .on { background:var(--fg); border-color:var(--fg); color:var(--bg); font-weight:600; }
.meta, .why { margin:6px 0 0; font-size:12px; color:var(--dim); }
.why { color:var(--orange); }
.live { margin:6px 0 0; font-size:12px; color:var(--dim); white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; }
details.extra { margin:16px 20px 24px; padding:10px 14px; border:1px solid var(--line); border-radius:var(--r-card); background:var(--panel); }
details.extra summary { cursor:pointer; font-size:13px; color:var(--dim); }
@media (max-width:700px) { .bh { padding:12px 14px; } .bm { padding:12px 14px; } #peek { max-width:none; border-left:0; border-top:1px solid var(--line); } }
</style></head><body>
<header class="bh">%(head)s</header>
<div class="bd"><div class="bm">%(turns)s%(strip)s<main>%(cols)s</main></div>
<aside id="peek" aria-label="미리 보기" hidden><div class="pkh"><span>미리 보기</span><button type="button" id="pkx" aria-label="닫기">×</button></div><div id="pkb"></div></aside></div>
<details class="note"><summary>도움말</summary><p>파일이 진실이다. 상태를 바꾸려면 그 파일의 「상태:」 줄을 고친다. 만든 시각 %(now)s</p></details>
<script>
(function () {
  // 엿보기 — 카드(a.card) 바로 뒤 형제 template.pk 를 패널로 옮긴다. 순서를 바꾸면 render_html 의 card() 와 함께 고친다.
  var peek = document.getElementById('peek'), body = document.getElementById('pkb'), main = document.querySelector('main'), cur = null;
  var K = 'pk:' + location.pathname;
  function close() { peek.hidden = true; cur = null; try { sessionStorage.removeItem(K); } catch (x) {} }
  function show(a, t) {
    body.innerHTML = '';
    body.appendChild(t.content.cloneNode(true));
    cur = a;
    peek.hidden = false;
  }
  main.addEventListener('click', function (e) {
    var a = e.target.closest ? e.target.closest('a.card') : null;
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
    var t = a.nextElementSibling;
    if (!t || t.className !== 'pk') return;
    e.preventDefault();
    if (cur === a) { close(); return; }
    show(a, t);
    try { sessionStorage.setItem(K, a.getAttribute('href')); } catch (x) {}
  });
  // 10초 새로 그리기로 화면을 다시 받아도 열어 둔 카드를 다시 연다(탭 저장소)
  try {
    var h = sessionStorage.getItem(K);
    if (h) Array.prototype.some.call(document.querySelectorAll('main a.card'), function (a) {
      var t = a.nextElementSibling;
      if (a.getAttribute('href') !== h || !t || t.className !== 'pk') return false;
      show(a, t);
      return true;
    });
  } catch (x) {}
  document.getElementById('pkx').addEventListener('click', close);
})();
</script>
</body></html>
"""

# ── 명령 팔레트 (ISSUE-sc02-e6-search-2026-11-20) ──
# 화면 틀 셋의 `</body>` 앞에 끼우는 인라인 조각 하나 — 바깥 파일을 부르지 않는다. 틀은 `%` 서식이라 끼울 때 `%` 를 겹친다.
# 주소 글자열에 `board.html` 을 쓰지 않는다(기존 시험이 화면에서 `board.html"` 의 첫 자리를 센다).
PALETTE = """<div id="palette" hidden><div id="pbox"><div id="prow"><input id="pq" type="text" autocomplete="off" placeholder="명령이나 검색어"><kbd id="pesc">Esc</kbd></div><div id="plist"></div><div id="pfoot">↑↓ 고르기 · ↵ 열기 · Esc 닫기</div></div></div>
<style>
#palette { position:fixed; top:0; left:0; right:0; bottom:0; z-index:99; background:var(--overlay);
  display:flex; justify-content:center; align-items:flex-start; padding-top:12vh; }
#palette[hidden] { display:none; }
#pbox { width:min(640px, 92vw); max-height:70vh; overflow:auto; background:var(--bg); color:var(--fg);
  border:1px solid var(--line); border-radius:12px; padding:10px; }
#prow { display:flex; align-items:center; gap:10px; }
#pq { flex-grow:1; min-width:0; box-sizing:border-box; padding:8px 10px; font:15px sans-serif; color:var(--fg); background:var(--bg);
  border:1px solid var(--line); border-radius:8px; }
#pesc { font:11px var(--mono); border:1px solid var(--line); border-radius:4px; padding:1px 6px; color:var(--dim); }
#pfoot { margin-top:8px; padding-top:8px; border-top:1px solid var(--line2); font-size:11px; color:var(--dim); }
.ph { margin:10px 0 4px; font-size:12px; color:var(--dim); font-weight:600; }
.pm { margin:2px 0; padding:4px 8px; font-size:13px; color:var(--dim); }
.pi { display:flex; flex-wrap:wrap; align-items:center; padding:6px 8px; border-radius:6px; font-size:14px; cursor:pointer; }
.pi.on { background:var(--line); }
.pi small { flex-basis:100%; color:var(--dim); font-size:12px; }
.pi .pr { margin-left:auto; font-size:12px; color:var(--dim); }
</style>
<script>
(function () {
  var KEY = 'stoploop.recent', P = document.getElementById('palette'), Q = document.getElementById('pq');
  var L = document.getElementById('plist'), online = location.protocol !== 'file:';
  var rows = [], sel = 0, seq = 0, found = null, st = 'ok', proj = [];
  function recent() {
    try { var r = JSON.parse(localStorage.getItem(KEY) || '[]'); return Array.isArray(r) ? r : []; } catch (e) { return []; }
  }
  if (/^\\/p\\/[^\\/]+\\/[^\\/]+\\.md$/.test(location.pathname) && document.title !== '그런 카드 없음') {
    try {
      var rs = recent().filter(function (x) { return x.href !== location.pathname; });
      rs.unshift({title: document.title, href: location.pathname});
      localStorage.setItem(KEY, JSON.stringify(rs.slice(0, 10)));
    } catch (e) {}
  }
  function el(cls, text, sub) {
    var d = document.createElement('div'); d.className = cls; d.textContent = text;
    if (sub) { var s = document.createElement('small'); s.textContent = sub; d.appendChild(s); }
    L.appendChild(d); return d;
  }
  function hit(t) { var q = Q.value.trim().toLowerCase(); return !q || String(t).toLowerCase().indexOf(q) >= 0; }
  function item(t, h, sub, hint) {
    var i = rows.length, d = el(i === sel ? 'pi on' : 'pi', t, sub); rows.push(h);
    if (hint) { var r = document.createElement('span'); r.className = 'pr'; r.textContent = hint; d.insertBefore(r, d.querySelector('small')); }
    d.onclick = function () { location.href = h; };
  }
  // 입력이 화면 이름에 들면([이름, 주소]) 그 화면 하나 — 이름이 입력으로 시작하는 것이 먼저, 그다음 목록 순서
  function screenMatch(q, list) {
    var t = String(q).trim().toLowerCase(), best = null;
    if (!t) return null;
    list.forEach(function (x) {
      var n = String(x[0]).toLowerCase();
      if (n.indexOf(t) < 0) return;
      if (!best || (n.indexOf(t) === 0 && String(best[0]).toLowerCase().indexOf(t) !== 0)) best = x;
    });
    return best;
  }
  function draw() {
    L.textContent = ''; rows = [];
    var q = Q.value.trim(), pm = /^\\/p\\/([^\\/]+)\\//.exec(location.pathname), cmds = [];
    var rc = recent().filter(function (x) { return hit(x.title); });
    var names = (pm ? [decodeURIComponent(pm[1])] : proj.map(function (p) { return p['name']; }))
      .filter(function (n) { return hit('카드로 만들기 — ' + n); });
    names.forEach(function (n) { cmds.push(['카드로 만들기 — ' + n, '/p/' + encodeURIComponent(n) + '/improve?made=0#고르기', '동작']); });
    if (pm) {
      var mine = proj.filter(function (p) { return p['name'] === decodeURIComponent(pm[1]); })[0];
      if (mine && hit('대기로 올리기…')) cmds.push(['대기로 올리기…', mine['href'], '아이디어 카드']);
    }
    var NM = @@NAMES@@;
    var screens = [[NM['home'], '/'], [NM['search'], '/search'], [NM['timeline'], '/timeline'], [NM['usage'], '/signals'], [NM['retro'], '/retro'], [NM['cards'], '/cards?보기=진행']]
      .concat(proj.map(function (p) { return [p['name'] + ' ' + NM['board'], p['href']]; }))
      .concat(proj.map(function (p) { return [NM['improve'] + ' — ' + p['name'], '/p/' + encodeURIComponent(p['name']) + '/improve']; }))
      .concat(proj.map(function (p) { return [NM['features'] + ' — ' + p['name'], '/p/' + encodeURIComponent(p['name']) + '/features']; }))
      .concat(proj.map(function (p) { return [NM['graph'] + ' — ' + p['name'], '/p/' + encodeURIComponent(p['name']) + '/graph']; }));
    screens.filter(function (x) { return hit(x[0]); }).forEach(function (x) { cmds.push([x[0], x[1], '화면 이동']); });
    el('ph', '가장 맞는 것');
    var sm = q && online && st !== 'err' ? screenMatch(q, screens) : null;
    if (sm) item(sm[0], sm[1], '', '화면 이동');
    if (!online || st === 'err') el('pm', '서버에서만 됩니다');
    else if (q) {
      if (st === 'load') el('pm', '찾는 중…');
      else if (found) item(found['title'], found['href'], found['프로젝트'] + (found['줄'] ? ' · ' + found['줄'] : ''));
      else if (!cmds.length && !rc.length) {
        el('pm', '찾은 것 없음');
        item('검색 화면에서 필터와 함께 찾기', '/search?q=' + encodeURIComponent(q));
      } else el('pm', '찾은 것 없음');
    }
    if (q) cmds.push(['검색 화면에서 모두 보기 — ' + q, '/search?q=' + encodeURIComponent(q), '검색']);
    el('ph', '명령');
    cmds.forEach(function (c) { item(c[0], c[1], '', c[2]); });
    el('ph', '최근 연 카드');
    if (!rc.length) el('pm', '아직 연 카드 없음');
    rc.forEach(function (x) { item(x.title, x.href); });
  }
  function ask() {
    var my = ++seq, q = Q.value.trim(); found = null; st = q ? 'load' : 'ok'; draw();
    if (!online) return;
    fetch('/search?as=json&q=' + encodeURIComponent(q)).then(function (r) { return r.json(); }).then(function (d) {
      if (my !== seq) return; found = d['가장 맞는 것']; proj = d['프로젝트'] || []; st = 'ok'; draw();
    }).catch(function () { if (my === seq) { st = 'err'; draw(); } });
  }
  function open() { P.hidden = false; Q.value = ''; sel = 0; ask(); Q.focus(); }
  function close() { P.hidden = true; }
  var PK = document.getElementById('pk'); if (PK) PK.addEventListener('click', open);
  Q.addEventListener('input', function () { sel = 0; ask(); });
  P.addEventListener('mousedown', function (e) { if (e.target === P) close(); });
  document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && String(e.key).toLowerCase() === 'k') { e.preventDefault(); if (P.hidden) open(); else close(); return; }
    if (P.hidden) return;
    if (e.key === 'Escape') { close(); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); sel = Math.min(sel + 1, rows.length - 1); draw(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); sel = Math.max(sel - 1, 0); draw(); }
    else if (e.key === 'Enter' && rows[sel]) { location.href = rows[sel]; }
  });
})();
</script>
"""
FILE_TEMPLATE = FILE_TEMPLATE.replace("</body>", PALETTE.replace("%", "%%") + "</body>").replace("@@THEME@@", THEME)
# 카드 상세 틀 — 같은 틀에서 윗부분(`<a>`·`<h1>`)만 `%(head)s` 로 바꿨다. 상세 전용 CSS 는 위 FILE_TEMPLATE 의 `<style>` 에 있다.
CARD_TEMPLATE = FILE_TEMPLATE.replace('<main>\n<a href="board.html">← 보드</a>\n<h1>%(name)s</h1>', '<main class="cd">\n%(head)s')
HOME_TEMPLATE = HOME_TEMPLATE.replace("</body>", PALETTE.replace("%", "%%") + "</body>").replace("@@THEME@@", THEME)
TEMPLATE = TEMPLATE.replace("</body>", PALETTE.replace("%", "%%") + "</body>").replace("@@THEME@@", THEME)


_표_구분줄 =re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_제목줄 = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_목록줄 = re.compile(r"^([-*]|\d+\.)\s+(.*)$")


def _칸(line):
    """표 한 줄을 칸으로 나눈다. 바깥 막대는 뗀다."""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _표_시작(lines, i):
    return ("|" in lines[i] and i + 1 < len(lines) and "|" in lines[i + 1]
            and bool(_표_구분줄.match(lines[i + 1])))


def _블록_시작(lines, i):
    line = lines[i]
    return (line.strip().startswith("```") or bool(_제목줄.match(line))
            or bool(_목록줄.match(line)) or _표_시작(lines, i))


def 본문_그리기(text):
    """작업 파일 글을 화면 본문 HTML 로 바꾼다. 제목·표·목록·코드 블록 넷만 그린다.

    모든 글자는 태그를 붙이기 전에 html.escape 를 거친다 — 파일 안의 태그는
    글자로만 남고, 링크·이미지처럼 파일 내용으로 태그 속성을 만드는 모양은 없다.
    실패하면 예외를 그대로 올린다. 잡아서 원문으로 돌아가는 것은 부르는 쪽이다.
    """
    esc = html.escape
    lines = text.split("\n")
    out, i = [], 0
    if lines and lines[0].strip() == "---":               # 머리말은 원문 그대로
        end = next((j for j in range(1, len(lines)) if lines[j].strip() == "---"), None)
        if end is not None:
            out.append("<pre>%s</pre>" % esc("\n".join(lines[:end + 1])))
            i = end + 1
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.strip().startswith("```"):              # 닫히지 않으면 끝까지 코드다
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            out.append("<pre>%s</pre>" % esc("\n".join(code)))
        elif _제목줄.match(line):
            m = _제목줄.match(line)
            out.append("<h%d>%s</h%d>" % (len(m.group(1)), esc(m.group(2)), len(m.group(1))))
            i += 1
        elif _표_시작(lines, i):
            head = "".join("<th>%s</th>" % esc(c) for c in _칸(line))
            rows = []
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append("<tr>%s</tr>" % "".join("<td>%s</td>" % esc(c) for c in _칸(lines[i])))
                i += 1
            out.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
                       % (head, "".join(rows)))
        elif _목록줄.match(line):
            tag = "ol" if line[0].isdigit() else "ul"
            items = []
            while i < len(lines):
                m = _목록줄.match(lines[i])
                if m and ("ol" if m.group(1)[0].isdigit() else "ul") == tag:
                    items.append(m.group(2))
                elif m or not lines[i].strip() or not lines[i][0].isspace():
                    break                                # 다른 목록·빈 줄·들여쓰기 없는 글에서 끝
                else:
                    items[-1] += "\n" + lines[i].strip() # 들여 쓴 줄은 앞 항목에 붙는다
                i += 1
            out.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % esc(t) for t in items), tag))
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not (para and _블록_시작(lines, i)):
                para.append(lines[i].strip())
                i += 1
            out.append("<p>%s</p>" % esc("\n".join(para)))
    return "\n".join(out)


PORT_BASE = 8700          # 고정 자리 — 이 기계의 보드 서버는 여기 하나다


class 작업파일서버(http.server.SimpleHTTPRequestHandler):
    """작업 파일(`.md`)은 브라우저가 읽을 수 있게 감싸서 내보낸다.

    그냥 두면 브라우저가 `.md` 를 내려받기로 처리해 내용이 안 보인다. 제목·표·목록·
    코드 블록 넷만 그리는 작은 변환기(`본문_그리기`)를 두고, 그리다 실패하면 원문을
    글자 그대로 보인다. 보드로 돌아가는 줄은 어느 쪽이든 있다.
    """

    def log_message(self, *args, **kw):
        pass                                            # 접속 기록은 안 찍는다

    def do_GET(self):
        path = self.translate_path(self.path)           # 폴더 밖으로 못 나간다
        if path.endswith(".md") and os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                body = f.read()
            try:
                shown = 본문_그리기(body)
            except Exception:                            # 그리기가 무엇으로 실패하든 원문이 보인다
                shown = "<pre>%s</pre>" % html.escape(body)
            page = (_화면들_채우기(FILE_TEMPLATE) % {"name": html.escape(os.path.basename(path)),
                                     "body": shown}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        return http.server.SimpleHTTPRequestHandler.do_GET(self)


# ── 이 기계의 보드 하나 (ISSUE-sc02-e1-machine-board-2026-11-20) ──
# 보는 목록 파일 — 배차의 `dispatch.PROJECTS_JSON` 과 같은 파일이다(보드는 배차를 가져오지 않는다).
# 등록 목록은 `"projects"`, 보기 목록은 새 열쇠 `"보기"` — 배차는 `"projects"` 만 읽으므로 보기만 한 프로젝트는 안 집는다.
LIST_FILE = os.path.expanduser("~/.stop-loop/projects.json")
BOARD_HEADER = "X-Stop-Loop-Board"
LAUNCH_DIR = os.path.expanduser("~/Library/LaunchAgents")
LAUNCH_LABEL = "dawn-island.stop-loop.board"


def watched_projects():
    """(보는 목록, 오류 글). 항목은 {name, path, 표시, 기기, 보기}. `보기` 는 `"보기"` 목록에서만 온 항목이면 참(`"projects"` 에도 있으면 거짓). 같은 이름은 앞의 것 하나, 목록 파일은 부를 때마다 다시 읽는다."""
    try:
        with open(LIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
        raw = [(k, x) for k in ("projects", "보기") for x in (data.get(k) or [])]
    except FileNotFoundError:
        return [], ""
    except (OSError, ValueError, AttributeError, TypeError) as e:
        return [], "목록 파일을 읽지 못했다 — %s: %s" % (LIST_FILE, e)
    out = {}
    for k, x in raw:
        if not isinstance(x, dict) or not x.get("name") or not x.get("path"):
            continue
        name, path = str(x["name"]), os.path.expanduser(str(x["path"]))
        if name in out:
            if os.path.abspath(out[name]["path"]) != os.path.abspath(path):
                out[name]["표시"].append("같은 이름이 다른 경로로도 있다: %s" % path)
            continue
        out[name] = {"name": name, "path": path, "표시": [] if os.path.isdir(path) else ["경로 없음"],
                     "기기": str(x.get("기기") or ""), "보기": k == "보기"}
    return list(out.values()), ""


def 목록_올리기(경로, 기기="", 배차=False):
    """보는 목록 파일에 프로젝트 하나를 올린다(기본 `"보기"`, 배차면 `"projects"`). (바꿨나, 알림 한 줄).
    같은 경로가 어느 열쇠에든 있으면·같은 이름이 다른 경로로 있으면·목록 파일이 깨졌으면 파일을 쓰지 않는다. 쓸 때는 임시 파일에 쓰고 바꿔 끼운다."""
    경로 = os.path.abspath(os.path.expanduser(경로))
    이름 = os.path.basename(경로.rstrip(os.sep)) or 경로
    깨짐 = lambda why: (False, "목록 파일을 읽지 못했다 — %s: %s — 쓰지 않았다" % (LIST_FILE, why))  # noqa: E731
    try:
        with open(LIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return 깨짐("가장 바깥이 객체가 아니다")
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as e:
        return 깨짐(e)
    for k in ("projects", "보기"):
        if not isinstance(data.get(k) or [], list):
            return 깨짐("%s 가 목록이 아니다" % k)
        for x in data.get(k) or []:
            if not isinstance(x, dict) or not x.get("name") or not x.get("path"):
                continue
            if os.path.abspath(os.path.expanduser(str(x["path"]))) == 경로:
                return False, "이미 보는 목록에 있다 — %s · %s — 바꾸지 않았다" % (k, x["name"])
            if str(x["name"]) == 이름:
                return False, "같은 이름 %s 이 다른 경로로 보는 목록에 있다(%s) — 쓰지 않았다" % (이름, x["path"])
    열쇠 = "projects" if 배차 else "보기"
    항목 = {"name": 이름, "path": 경로}
    if 기기:
        항목["기기"] = 기기
    data[열쇠] = list(data.get(열쇠) or []) + [항목]
    tmp = LIST_FILE + ".쓰는중"
    try:
        os.makedirs(os.path.dirname(LIST_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, LIST_FILE)
    except OSError as e:
        return False, "목록 파일을 쓰지 못했다 — %s: %s" % (LIST_FILE, e)
    return True, "보는 목록에 올렸다 — %s · %s · %s" % (열쇠, 이름, 경로)


def 연결(경로, 기기="", 배차=False):
    """`--연결` — 그 프로젝트의 원격을 한 번 읽어 연결 확인을 찍고, 보는 목록에 올린다. 프로젝트 파일은 쓰지 않는다. 종료 값을 돌려준다."""
    경로 = os.path.abspath(os.path.expanduser(경로))
    if not os.path.isdir(경로):
        print("폴더가 아니다: %s" % 경로)
        return 1
    확인 = read_remote(경로).get("확인") or []
    for x in 확인:
        print("연결 확인 — %s — 고칠 값: %s" % (x["까닭"], x["고칠"]))
    if not 확인:
        print("연결 확인 — 문제 없음")
    바뀜, 말 = 목록_올리기(경로, 기기, 배차)
    print(말)
    return 0 if 바뀜 or 말.startswith("이미") else 1


def _이름(project):
    """프로젝트 경로의 보는 목록 이름. 목록에 없으면 폴더 이름."""
    for p in watched_projects()[0]:
        if os.path.abspath(p["path"]) == os.path.abspath(project):
            return p["name"]
    return os.path.basename(os.path.abspath(project))


def _보는목록(extra=None):
    items, err = watched_projects()
    if extra and extra["name"] not in {p["name"] for p in items}:
        items.append(extra)
    return items, err


# ── 다른 기계의 진행 (ISSUE-sc02-e3-merge-machines-2026-11-20) ──
# 원격 기준 가지의 카드 머리말을 읽어 이 기계 카드와 합쳐 보인다. 읽기는 상주의 `원격바퀴` 실이 하고,
# 화면 요청은 그 실이 메모리에 둔 그림(`REMOTE`)만 쓴다. 작업 사본의 파일·색인·참조는 건드리지 않는다.
REMOTE_EVERY = 60          # 원격바퀴의 주기(초)
REMOTE_GIT_TIMEOUT = 20    # 원격 git 호출 한 번의 상한(초) — 러너 `CARD_GIT_TIMEOUT` 과 같다
REMOTE_FETCH_TIMEOUT = 90  # 읽기(`read_remote`)의 `fetch` 한 줄만의 상한(초). 큰 저장소는 20초를 넘긴다. 쓰기 경로는 20 그대로
CARD_PUSH_DIR = os.path.expanduser("~/.stop-loop/card-push")   # 러너가 올리기 결과를 쓰는 폴더. 보드는 읽기만 한다
REMOTE = {}                # {프로젝트 절대 경로: 그림}
REMOTE_STALE_SEC = 300     # 마지막 성공 때가 이보다 오래면 사이드바가 「5분 넘게 못 받음」 경고를 낸다
REMOTE_CLONE_TIMEOUT = 900  # 거울을 처음 만드는 `clone` 의 상한(초) — 사본 객체를 복사하므로 큰 저장소도 내려받지는 않는다

# ── 보드의 원격 출처: 거울 · 그림 파일 · 사람 손 기록 자리 (ISSUE-board-remote-b-mirror-2026-11-26) ──
# 거울 = 프로젝트마다 보드 전용 bare 저장소. 원격 기준 가지를 `refs/remotes/origin/<가지>` 로 받아 둔다. 작업 사본에는 쓰지 않는다.
# 그림 파일 = `REMOTE` 의 그림을 프로젝트마다 파일로 남긴 것 — 다시 띄우면 첫 원격 확인 전에도 이것으로 시작한다.
# 이름은 프로젝트 폴더 이름(basename)이다 — `HUMAN_DIR` 저장 이름과 같은 규칙이라 보는 목록 안에서 겹치지 않는다고 본다.
MIRROR_DIR = os.path.expanduser("~/.stop-loop/mirrors")
CACHE_DIR = os.path.expanduser("~/.stop-loop/board-cache")
RESOLVED_DIR = os.path.expanduser("~/.stop-loop/human-resolved")


def _폴더이름(project):
    return os.path.basename(os.path.abspath(project))


def _거울_경로(project):
    return os.path.join(MIRROR_DIR, _폴더이름(project) + ".git")


def _끝_있음(project):
    """원격 그림에 끝이 있는 프로젝트(한 번이라도 읽었거나 그림 파일에서 불러옴) — 카드·시나리오·이력의 출처가 거울이다."""
    return bool((REMOTE.get(os.path.abspath(project)) or {}).get("끝"))


def _읽을_저장소(project):
    """이력을 읽는 git 의 대상 — 그림에 끝이 있고 거울이 있으면 거울, 아니면 작업 사본. 거울 자신을 넘기면 그대로 돌려준다."""
    return _거울_경로(project) if _끝_있음(project) and os.path.isdir(_거울_경로(project)) else project

# ── 읽기 계산을 잠깐 저장한다 (ISSUE-ui-0-server-threads-2026-11-22) ──
# 열쇠 = 화면 종류 + 인자 + 보는 프로젝트마다 (원격 끝, 로컬 `git rev-parse --all`). 하나라도 바뀌면 다시 계산한다.
# 원격 끝이 없는 프로젝트는 로컬 해시만 넣는다. 로컬 해시를 못 읽으면 저장도 재사용도 하지 않는다. 프로세스 메모리뿐 — 만료·상한 없음(커지면 그때 건다).
_저장소 = {}               # {열쇠: 결과}


def 저장_비우기():
    _저장소.clear()


def _열쇠(이름, items, 인자):
    if not items:
        return None
    조각 = []
    for item in items:
        그림 = REMOTE.get(os.path.abspath(item["path"])) or {}
        끝 = (그림.get("끝") or "", 그림.get("오류") or "", bool(그림.get("없음")))   # 끝이 없으면 로컬 해시만 열쇠에 든다. 오류·없음은 결과 문구에 들어간다
        if 그림.get("끝") and os.path.isdir(_거울_경로(item["path"])):    # 거울에서 읽는 프로젝트는 원격 끝이 열쇠다 — 사본 해시를 부르지 않는다
            조각.append((item["path"], 끝, ""))
            continue
        try:
            r = subprocess.run(["git", "-C", item["path"], "rev-parse", "--all"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0 or not isinstance(r.stdout, str):
            return None
        조각.append((item["path"], 끝, hashlib.sha1(r.stdout.encode("utf-8")).hexdigest()))
    return (이름, repr(sorted(인자.items())), tuple(조각))


def _잠깐저장(이름, items, 인자, 계산):
    """같은 열쇠면 앞서 계산한 결과의 사본을, 아니면 `계산()` 의 결과를 저장하고 그 사본을 돌려준다. 예외는 저장 없이 그대로 올린다."""
    열쇠 = _열쇠(이름, items, 인자)
    if 열쇠 is None:
        return 계산()
    if 열쇠 in _저장소:
        return copy.deepcopy(_저장소[열쇠])
    값 = 계산()
    _저장소[열쇠] = copy.deepcopy(값)
    return 값


def _토큰(account):
    """`gh auth token --user <계정>` 의 토큰. 못 얻으면 None (`runner.gh_token` 과 같다 — 보드는 runner 를 가져오지 못한다)."""
    try:
        env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
        res = subprocess.run(["gh", "auth", "token", "--user", account], capture_output=True, text=True, env=env)
    except OSError:
        return None
    return (res.stdout.strip() or None) if res.returncode == 0 else None


def _원격_git(project, *args, env=None, timeout=None):
    """보드가 원격 git 을 부르는 자리(읽기와 카드 올리기). 묻는 창을 띄우지 않고, 시간 넘김·실행 불가도 실패한 결과로 돌려준다.
    `timeout` 이 없으면 REMOTE_GIT_TIMEOUT."""
    e = dict(os.environ if env is None else env)
    e["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(["git", *args], cwd=project, env=e, capture_output=True, encoding="utf-8",
                              errors="replace", timeout=REMOTE_GIT_TIMEOUT if timeout is None else timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "시간 초과")
    except OSError as err:
        return subprocess.CompletedProcess(args, 127, "", str(err))


def _끝줄(r):
    rows = [x.strip() for x in ((r.stderr or "") or (r.stdout or "")).strip().splitlines() if x.strip()]
    return rows[-1] if rows else "git 이 실패했다 (종료 %s)" % r.returncode


def _프로젝트_설정(project):
    try:
        with open(os.path.join(project, "stop-loop.config.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _유효_설정(project):
    """보드가 쓰는 프로젝트 설정 — 그 프로젝트 그림에 원격 끝에서 읽은 설정(dict, 깨졌으면 `{}`)이 있으면 그것, 없으면 작업 사본의 것. git 을 부르지 않는다."""
    설정 = (REMOTE.get(os.path.abspath(project)) or {}).get("설정")
    return 설정 if isinstance(설정, dict) else _프로젝트_설정(project)


def _그림_없음():
    return {"없음": True, "때": None, "카드": {}, "오류": ""}


def _그림_실패(why):
    return {"없음": False, "때": None, "카드": {}, "오류": why, "시도": dt.datetime.now()}


# 오프라인·서버 장애처럼 설정 문제가 아닌 `ls-remote` 실패의 오류 글 조각 — git 은 이때도 인증 실패와 같은 종료 128 을 낸다
_네트워크_실패_조각 = ("Could not resolve host", "Couldn't connect to server", "Connection refused", "The requested URL returned error: 5")


def 연결_확인(주소, 설정, 막힘="", 계정="", 종료=0, 글=""):
    """프로젝트 연결 확인 — `[{"까닭", "고칠"}]`, 문제없으면 `[]`. 계산만 한다(git·gh 를 부르지 않고 예외도 내지 않는다).
    막힘은 원격 읽기가 어디서 막혔는지: `""`·`"토큰"`·`"gh 없음"`·`"ls-remote"`. 종료·글은 막힘이 `"ls-remote"` 일 때 그 종료 값과 오류 글 전체."""
    if not 주소:
        return [{"까닭": "원격(origin)이 없어 다른 기기에서 상태가 안 보임", "고칠": "git remote add origin <주소>"}]
    설정 = 설정 if isinstance(설정, dict) else {}
    out = []
    if 설정.get("handoff") == "local":
        out.append({"까닭": "handoff local 이라 원격에 상태가 안 올라옴", "고칠": '"handoff": "pr" 넣기'})
    if not 계정:
        a = 설정.get("githubAccount")
        계정 = a.strip() if isinstance(a, str) else ""
    github = bool(re.match(r"(?:git@github\.com:|https://github\.com/)", 주소))
    if 막힘 == "토큰":
        out.append({"까닭": "GitHub 계정 %s 의 토큰을 얻지 못함" % 계정,
                    "고칠": 'gh auth login 으로 %s 로그인 또는 "githubAccount": "<계정>" 넣기' % 계정})
    elif 막힘 == "ls-remote" and 종료 not in (124, 127) and not any(x in (글 or "") for x in _네트워크_실패_조각):
        if not github:
            out.append({"까닭": "원격을 못 읽음", "고칠": "origin 주소와 읽기 권한 확인 — git ls-remote origin"})
        elif 계정:
            out.append({"까닭": "GitHub 계정 %s 의 토큰으로 원격을 못 읽음" % 계정, "고칠": '"githubAccount": "<원격을 읽을 수 있는 계정>" 넣기'})
        else:
            out.append({"까닭": "GitHub 계정이 없어 원격을 못 읽음", "고칠": '"githubAccount": "<계정>" 넣기'})
    return out


def _거울_만들기(project, url, base, env):
    """거울(`MIRROR_DIR/<이름>.git`)이 없을 때 만든다 — 사본 객체를 참조해 복사하고(`--reference` + `--dissociate`) 모자란 것만 원격에서 받는다.
    끝나면 거울이 사본을 가리키는 연결(alternates)은 남지 않는다. 실패하면 반쯤 만든 폴더를 지우고 까닭을 돌려준다(성공이면 빈 글)."""
    mirror = _거울_경로(project)
    tmp = mirror + ".만드는중"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        os.makedirs(MIRROR_DIR, exist_ok=True)
        r = _원격_git(project, "clone", "-q", "--bare", "--single-branch", "--branch", base, "--reference", project,
                     "--dissociate", url, tmp, env=env, timeout=REMOTE_CLONE_TIMEOUT)
        if r.returncode != 0:
            return _끝줄(r)
        os.replace(tmp, mirror)
        return ""
    except OSError as e:
        return str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _거울_끝(mirror, base):
    """거울 이름표 `refs/remotes/origin/<가지>` 의 끝. 없으면 빈 글."""
    if not os.path.isdir(mirror):
        return ""
    r = _원격_git(mirror, "rev-parse", "--verify", "-q", "refs/remotes/origin/" + base)
    return r.stdout.strip() if r.returncode == 0 else ""


def read_remote(project):
    """프로젝트 원격(origin)의 기준 가지에 있는 `docs/backlog/*.md` 의 머리말과 제목 — 그림 하나. 예외를 내지 않는다.

    원격 끝(`ls-remote` 한 번)이 앞서 읽은 그림·거울 이름표의 끝과 같으면 아무것도 받지도 읽지도 않고 때만 새로 한다.
    다르면 보드 전용 거울로 받는다(`fetch <주소> +refs/heads/<가지>:refs/remotes/origin/<가지>`). 작업 사본은 거울을 처음 만들 때
    객체를 참조해 복사할 뿐 파일·색인·참조·객체를 바꾸지 않는다."""
    url = ""
    try:
        if not os.path.exists(os.path.join(project, ".git")):
            return {**_그림_없음(), "확인": 연결_확인("", _유효_설정(project))}
        got = _원격_git(project, "remote", "get-url", "origin")
        url = got.stdout.strip() if got.returncode == 0 else ""
        if not url:
            return {**_그림_없음(), "확인": 연결_확인("", _유효_설정(project))}
        cfg = _유효_설정(project)                    # 앞 바퀴에 원격 끝에서 읽은 설정이 있으면 그것 — 처음에는 사본
        싣기 = lambda pic, **막힘: {**pic, "확인": 연결_확인(url, cfg, **막힘)}  # noqa: E731
        env = dict(os.environ)
        account = cfg.get("githubAccount")
        if isinstance(account, str) and account.strip():
            token = _토큰(account.strip())
            if not token:
                if shutil.which("gh") is None:
                    return 싣기(_그림_실패("gh 를 찾지 못함 — `--상주-켜기` 를 다시 하라"), 막힘="gh 없음", 계정=account.strip())
                return 싣기(_그림_실패("GitHub 계정 %s 의 토큰을 얻지 못함" % account.strip()), 막힘="토큰", 계정=account.strip())
            env["GH_TOKEN"] = token
        r = _원격_git(project, "ls-remote", "--heads", url, env=env)
        if r.returncode != 0:
            return 싣기(_그림_실패(_끝줄(r)), 막힘="ls-remote", 종료=r.returncode, 글=(r.stderr or "") + (r.stdout or ""))
        heads = {}
        for line in r.stdout.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                heads[ref[len("refs/heads/"):]] = sha.strip()
        base = next((x for x in ("develop", "main", "master") if x in heads), None)
        if base is None:
            return 싣기(_그림_실패("원격에 기준 가지가 없다"))
        tip = heads[base]
        mirror = _거울_경로(project)
        old = REMOTE.get(os.path.abspath(project))
        if (old and "설정" in old and (old.get("설정끝") if old.get("없음") else old.get("끝")) == tip and old.get("가지") == base
                and _거울_끝(mirror, base) == tip and (cfg.get("handoff") == "local") == bool(old.get("없음"))):
            return 싣기({**{k: v for k, v in old.items() if k != "시도"}, "때": old["때"] if old.get("없음") else dt.datetime.now(), "오류": ""})   # 끝이 같다 — 받기·설정·카드 읽기·커밋 때 읽기를 하지 않는다
        if not os.path.isdir(mirror):
            why = _거울_만들기(project, url, base, env)
            if why:
                return 싣기(_그림_실패(why))
        r = _원격_git(mirror, "fetch", "-q", "--no-write-fetch-head", url, "+refs/heads/%s:refs/remotes/origin/%s" % (base, base),
                     env=env, timeout=REMOTE_FETCH_TIMEOUT)
        if r.returncode != 0:
            return 싣기(_그림_실패(_끝줄(r)))
        설정 = None                                   # 원격 끝의 설정 — 파일이 없으면 None, 깨졌으면 `{}`. 거울에서 로컬로 읽는다
        r = _원격_git(mirror, "cat-file", "blob", "%s:stop-loop.config.json" % tip)
        if r.returncode == 0:
            try:
                설정 = json.loads(r.stdout)
            except ValueError:
                설정 = {}
            if not isinstance(설정, dict):
                설정 = {}
        cfg = 설정 if isinstance(설정, dict) else _프로젝트_설정(project)
        if cfg.get("handoff") == "local":
            # 「없음」 그림은 `끝` 을 담지 않는다 — `끝` 이 있으면 이력·열쇠·묶음 읽기가 출처를 거울로 바꾼다. 읽은 끝은 `설정끝` 에 둔다
            return 싣기({**_그림_없음(), "설정끝": tip, "가지": base, "설정": 설정})
        r = _원격_git(mirror, "ls-tree", "-z", "--name-only", tip, "--", "docs/backlog/")
        if r.returncode != 0:
            return 싣기(_그림_실패(_끝줄(r)))
        names = [n for n in sorted(os.path.basename(x) for x in r.stdout.split("\0") if x) if n.endswith(".md") and not n.startswith("_")]
        글들 = _묶음_읽기(mirror, ["%s:docs/backlog/%s" % (tip, n) for n in names])
        cards = {}
        for name in names:
            text = 글들.get("%s:docs/backlog/%s" % (tip, name))
            if text is None:
                cards[name] = {"머리말": None, "title": name, "사람분": None, "커밋때": None}
                continue
            head = next((x[2:].strip() for x in text.splitlines() if x.startswith("# ")), name)
            fm = remote_front_matter(text)
            cards[name] = {"머리말": fm, "title": head, "사람분": _사람분_줄들(text.splitlines()),
                           "커밋때": _커밋때(mirror, tip, name) if fm and fm.get("상태") == "진행" else None}
        return {"없음": False, "때": dt.datetime.now(), "카드": cards, "오류": "", "끝": tip, "가지": base, "설정": 설정,
                "끝때": _커밋때(mirror, tip, None), "저장소": _폴더이름(project), "확인": 연결_확인(url, cfg)}
    except Exception as e:                           # 읽기가 무엇으로 실패하든 화면은 이 기계 것을 보인다
        return {**_그림_실패(str(e) or type(e).__name__), "확인": []}


def _커밋때(project, tip, name):
    """원격 기준 가지 끝에서 그 카드 파일을 마지막으로 고친 커밋의 때(`name` 이 None 이면 끝 커밋 자신의 때). 못 읽으면 None."""
    r = _원격_git(project, "log", "-1", "--format=%ct", tip, *(("--", "docs/backlog/" + name) if name else ()))
    try:
        return dt.datetime.fromtimestamp(int(r.stdout.strip())) if r.returncode == 0 else None
    except ValueError:
        return None


def refresh_remote(items):
    """보는 목록 항목마다 원격을 읽어 `REMOTE` 의 그림을 갈아 끼운다. 실패하면 앞서 읽은 카드와 때는 두고 까닭만 바꾼다."""
    for item in items:
        key = os.path.abspath(item["path"])
        try:
            pic = read_remote(item["path"])
        except Exception as e:                       # 한 프로젝트의 실패가 다른 프로젝트 읽기를 막지 않는다
            pic = _그림_실패(str(e) or type(e).__name__)
        old = REMOTE.get(key)
        if pic["오류"] and old and not old["없음"]:
            pic = {**old, "오류": pic["오류"], "시도": pic.get("시도"), "확인": pic.get("확인") or []}       # `앞카드수` 도 old 에서 이어진다
        elif not pic["오류"] and not pic["없음"] and not pic["카드"]:
            # 오류 없이 0장이면 앞서 읽은 카드 수를 남겨 「원격 카드를 읽지 못함」 경고의 근거로 쓴다. 카드가 읽히면 새 그림에 이 키가 없어 경고가 풀린다
            pic = {**pic, "앞카드수": len((old or {}).get("카드") or {}) or (old or {}).get("앞카드수", 0)}
        REMOTE[key] = pic
        _그림_저장(item["path"], pic)


def _시각_글(t):
    return t.isoformat(timespec="seconds") if isinstance(t, dt.datetime) else None


def _시각_풀기(v):
    return dt.datetime.fromisoformat(v) if isinstance(v, str) and v else None


def _그림_파일(project):
    return os.path.join(CACHE_DIR, _폴더이름(project) + ".json")


def _그림_저장(project, pic):
    """그림을 `CACHE_DIR/<이름>.json` 에 남긴다 — 끝을 한 번이라도 읽은 프로젝트만. 쓰기 실패는 화면에 영향이 없고 다음 바퀴에 다시 쓴다."""
    if not (pic.get("설정끝") if pic.get("없음") else pic.get("끝")):
        return
    if pic.get("없음"):
        data = {"없음": True, "설정끝": pic["설정끝"], "가지": pic.get("가지", ""), "설정": pic.get("설정")}
    else:
        data = _그림_글(pic)
    _그림_쓰기(project, data)


def _그림_글(pic):
    data = {"끝": pic["끝"], "가지": pic.get("가지", ""), "끝때": _시각_글(pic.get("끝때")), "때": _시각_글(pic.get("때")),
            "시도": _시각_글(pic.get("시도") or pic.get("때")), "오류": pic.get("오류", ""), "앞카드수": pic.get("앞카드수", 0),
            "카드": {n: {"머리말": c.get("머리말"), "title": c.get("title", n), "사람분": c.get("사람분"), "커밋때": _시각_글(c.get("커밋때"))}
                   for n, c in (pic.get("카드") or {}).items()}}
    if "설정" in pic:
        data["설정"] = pic["설정"]
    return data


def _그림_쓰기(project, data):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _그림_파일(project)
        with open(path + ".쓰는중", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(path + ".쓰는중", path)
    except OSError as e:
        print("원격 그림 파일 저장 실패 (다음 바퀴에 다시): %s" % e, flush=True)


def _그림_풀기(data, 저장소=""):
    if data.get("없음") is True:                      # 「없음」 그림 — `끝` 없이 푼다(출처가 사본이다)
        if not isinstance(data["설정끝"], str) or not data["설정끝"]:
            raise ValueError("설정끝")
        설정 = data["설정"]
        if 설정 is not None and not isinstance(설정, dict):
            raise ValueError("설정")
        return {**_그림_없음(), "설정끝": data["설정끝"], "가지": str(data.get("가지", "")), "설정": 설정}
    cards = {}
    for n, c in data["카드"].items():
        fm = c.get("머리말")
        if fm is not None and not isinstance(fm, dict):
            raise ValueError("머리말")
        cards[n] = {"머리말": fm, "title": str(c.get("title", n)), "사람분": c.get("사람분"), "커밋때": _시각_풀기(c.get("커밋때"))}
    if not isinstance(data["끝"], str) or not data["끝"]:
        raise ValueError("끝")
    pic = {"없음": False, "때": _시각_풀기(data.get("때")), "카드": cards, "오류": str(data.get("오류", "")), "끝": data["끝"],
           "가지": str(data.get("가지", "")), "끝때": _시각_풀기(data.get("끝때")), "앞카드수": int(data.get("앞카드수", 0) or 0), "저장소": 저장소}
    시도 = _시각_풀기(data.get("시도"))
    if 시도:
        pic["시도"] = 시도
    if isinstance(data.get("설정"), dict) or ("설정" in data and data["설정"] is None):
        pic["설정"] = data["설정"]
    return pic


def 원격_불러오기(items):
    """보는 목록 프로젝트마다 그림 파일을 읽어 `REMOTE` 에 아직 없는 프로젝트만 채운다. git 을 부르지 않고 예외도 내지 않는다.
    파일이 없거나 깨졌으면 그 프로젝트는 비워 둔다(「원격 읽는 중」)."""
    for item in items:
        key = os.path.abspath(item["path"])
        if key in REMOTE:
            continue
        try:
            with open(_그림_파일(item["path"]), encoding="utf-8") as f:
                REMOTE[key] = _그림_풀기(json.load(f), _폴더이름(item["path"]))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue


def _비용품질_인자(qs):
    """`/signals` 요청이 인자를 채우는 방식 그대로 — period 가 이기고, 아무것도 없으면 이번 주를 날짜로 풀어 넣는다(저장 열쇠에 날짜가 든다). 요청과 미리 계산이 같은 열쇠를 만들려고 한 곳에 둔다."""
    qs = {k: list(v) for k, v in qs.items()}
    g = lambda k: (qs.get(k) or [""])[0].strip()          # noqa: E731
    if g("period") or not (g("from") or g("to")):
        qs["period"] = [g("period") or "week"]
        qs["from"], qs["to"] = ([x] for x in signals_기간(g("period"), "", "", dt.date.today())[:2])
    return qs


_미리계산_잠금 = threading.Lock()


def 미리_계산(items):
    """비용·품질 화면의 기본 기간 결과를 요청과 같은 열쇠로 저장해 둔다 — 첫 열기가 30초 넘게 걸리지 않게. 앞선 미리 계산이 돌면 곧바로 돌아오고, 예외는 삼킨다."""
    if not _미리계산_잠금.acquire(blocking=False):
        return
    try:
        시작 = time.monotonic()
        qs = _비용품질_인자({})
        새로, 건너뜀 = 0, 0
        for 이름, 읽기 in (("signals", read_signals), ("retro", read_retro)):
            try:
                열쇠 = _열쇠(이름, items, qs)
                if 열쇠 is not None and 열쇠 in _저장소:
                    건너뜀 += 1
                else:
                    새로 += 1
                _잠깐저장(이름, items, qs, lambda 읽기=읽기: 읽기(items, qs))
            except Exception:                        # noqa: BLE001 — 한쪽이 터져도 다른 쪽은 이어 돈다
                pass
        print("비용·품질 미리 계산 끝 (%d초) · 새로 계산 %d건 · 건너뜀 %d건" % (round(time.monotonic() - 시작), 새로, 건너뜀), flush=True)
    except Exception:                                # noqa: BLE001 — 미리 계산이 상주를 죽이지 않는다. 화면 요청은 직접 계산한다
        pass
    finally:
        _미리계산_잠금.release()


def 원격바퀴(extra, stop):
    """`stop` 이 서기 전까지 REMOTE_EVERY 초마다 보는 목록의 원격을 읽는다. 기다림은 Event 로 — 상주가 끝나면 곧 멈춘다."""
    while True:
        try:
            items = _보는목록(extra)[0]
            refresh_remote(items)
            threading.Thread(target=미리_계산, args=(items,), name="미리계산", daemon=True).start()   # 바퀴는 기다리지 않는다
        except Exception:                            # noqa: BLE001 — 원격 읽기가 상주를 죽이지 않는다
            pass
        if stop.wait(REMOTE_EVERY):
            return


def _기기_이름(v):
    v = (v or "").strip()
    return "" if v in ("—", "-") else v


def merge_cards(cards, 그림, 이기계):
    """이 기계 카드(`read_cards`)와 원격 그림을 합친 카드 목록. 카드마다 `도는 기계`·`출처`·`원격만`·`원격표시` 가 붙는다.

    도는 기계 = 원격 돈기계 → 이 기계 돈기계 → 원격 기기 → 이 기계 기기 → 「기기 모름」. 이 기계 이름과 도는 기계를 둘 다 알면
    같을 때 이 기계 기록, 다를 때 원격 기록. 하나라도 모르면 상태가 「대기」가 아닌 쪽이 하나뿐일 때 그쪽, 아니면 이 기계 기록."""
    remote = {} if not 그림 or 그림.get("없음") else 그림.get("카드") or {}
    이기계 = _기기_이름(이기계)
    mine = {c["file"]: c for c in cards}
    if remote and 그림.get("끝"):                      # 거울에서 읽은 그림 — 카드 상태의 출처는 원격 하나다. 사본 카드는 상태를 못 바꾼다
        out = []
        for name in sorted(remote):
            rem = remote[name]
            fm = rem["머리말"]
            run = next((v for v in (_기기_이름(x) for x in ((fm or {}).get("돈기계"), (fm or {}).get("기기"))) if v), "")
            c = _카드_만들기(name, rem["title"], fm or {}, 사람분=rem.get("사람분"))
            if c["상태"] == "진행" and run and 이기계 and run == 이기계:    # 이 기기가 돌리는 카드만 이 기기의 실시간 줄을 붙인다
                c["마지막줄"] = _실시간_줄(그림.get("저장소", ""), name)
            이기기 = c["상태"] == "진행" and run and 이기계 and run == 이기계
            out.append({**c, "소식때": rem.get("커밋때") if c["상태"] == "진행" and not 이기기 else None,
                        "도는 기계": run or "기기 모름", "출처": "원격", "이기기돌림": bool(이기기), "원격만": name not in mine,
                        "원격표시": ["원격 머리말 읽지 못함"] if fm is None else []})
        return _줄_세우기(out)
    if 그림 and not 그림.get("없음") and 그림.get("끝") and 그림.get("저장소"):    # 거울에서 읽었는데 원격에 카드가 0장 — 사본 카드로 돌아가지 않는다. 「저장소」는 읽은 그림만 갖는 표시다(끝만 적은 자리표시는 사본을 쓴다)
        return []
    out = []
    for name in sorted(set(mine) | set(remote)):
        local, rem = mine.get(name), remote.get(name)
        fm = rem["머리말"] if rem else None
        broken = bool(rem) and fm is None
        rcard = _카드_만들기(name, rem["title"], fm or {}) if rem else None
        run = next((v for v in (_기기_이름(x) for x in (
            (fm or {}).get("돈기계"), local and local["돈기계"], (fm or {}).get("기기"), local and local["기기"])) if v), "")
        if rem is None or broken:
            chosen = local or rcard
        elif local is None:
            chosen = rcard
        elif run and 이기계:
            chosen = local if run == 이기계 else rcard
        else:
            live = [x for x in (local, rcard) if x["상태"] != "대기"]
            chosen = live[0] if len(live) == 1 else local
        out.append({**chosen, "사람분": local.get("사람분") if local else None, "도는 기계": run or "기기 모름", "출처": "이 기계" if chosen is local else "원격",
                    "원격만": local is None, "원격표시": ["원격 머리말 읽지 못함"] if broken else []})
    return _줄_세우기(out)


def _올리기_결과(item, cards):
    """이 기계 러너의 올리기 결과 가운데 이 작업목록 카드의 것 — (밀림 수, 거부 있음)."""
    repo = os.path.basename(os.path.abspath(item["path"]))
    밀림, 거부 = 0, False
    for c in cards:
        if c.get("원격만"):
            continue
        try:
            with open(os.path.join(CARD_PUSH_DIR, "%s__%s.json" % (repo, os.path.splitext(c["file"])[0])),
                      encoding="utf-8") as f:
                kind = json.load(f).get("결과")
        except (OSError, ValueError, AttributeError):
            continue
        밀림 += kind == "밀림"
        거부 = 거부 or kind == "거부"
    return 밀림, 거부


def _원격_문구(item, cards):
    pic = REMOTE.get(os.path.abspath(item["path"]))
    if pic is None:
        out = ["원격 읽는 중" if os.path.exists(os.path.join(item["path"], ".git"))
               else "원격 없음 — 다른 기기에서 보이지 않음"]
    elif pic["없음"]:
        out = ["원격 없음 — 다른 기기에서 보이지 않음"]
    else:
        when = pic["때"]
        읽음 = ("원격 읽음 " + when.strftime("%H:%M" if when.date() == dt.date.today() else "%Y-%m-%d %H:%M")) if when else ""
        if not pic["오류"]:
            out = [읽음]
        elif when:
            out = ["원격 저장소를 아직 못 읽음 — " + pic["오류"], 읽음]
        else:
            out = ["원격 저장소를 아직 못 읽음 — 이 기기 것만 — " + pic["오류"]]
            시도 = pic.get("시도")
            if 시도:
                out.append("읽은 적 없음 · 마지막 시도 " + 시도.strftime("%H:%M" if 시도.date() == dt.date.today() else "%Y-%m-%d %H:%M"))
        if when:                                     # 마지막으로 받은 때 — 실패해도 그 때로 센다. 오래되면(상주 바퀴가 멈춘 경우도) 경고
            초 = (dt.datetime.now() - when).total_seconds()
            out.append("원격 기준 %d분 전" % max(0, int(초 // 60)) + (" — 5분 넘게 못 받음" if 초 > REMOTE_STALE_SEC else ""))
        if not pic["오류"] and not pic["카드"] and (any(c.get("출처") != "원격" for c in cards) or pic.get("앞카드수", 0) > 0):
            out.append("원격 카드를 읽지 못함 — 원격 카드 0장" if pic.get("끝") else "원격 카드를 읽지 못함 — 이 기기 사본 기준으로 보임")
    for x in (pic or {}).get("확인") or []:
        out.append("연결 확인 — %s — %s" % (x["까닭"], x["고칠"]))
    밀림, 거부 = _올리기_결과(item, cards)
    if 밀림:
        out.append("올리지 못한 고침 있음 (%d장)" % 밀림)
    if 거부:
        out.append("진행이 다른 기기에 보이지 않음 — 올리기 거부")
    return out


def _원격_경고줄(x):
    """사이드바 원격 줄 가운데 경고색으로 그릴 것 — 받기 실패·5분 초과·원격 0장."""
    return x.startswith(("원격 저장소를 아직 못 읽음", "읽은 적 없음", "연결 확인")) or "5분 넘게 못 받음" in x or x.startswith("원격 카드를 읽지 못함")


def _읽기(item, 합치기=False):
    """(카드, 시나리오, 할 일 카드, 사람 차례) — 작업목록 폴더를 못 읽으면 OSError. 합치기면 카드에 원격 그림을 합친다."""
    backlog = os.path.join(item["path"], "docs", "backlog")
    if not os.path.isdir(backlog):
        raise OSError("작업목록 폴더를 읽지 못했다: %s" % backlog)
    cards = read_cards(backlog)
    if 합치기:
        cards = merge_cards(cards, REMOTE.get(os.path.abspath(item["path"])), item.get("기기", ""))
    scenarios = 시나리오들(item["path"])
    todo = read_todo_cards(os.path.join(item["path"], "docs", "todo"))
    return cards, scenarios, todo, human_turns(cards, scenarios, item["path"], todo)


_미읽음 = object()


def stalled_alarms(item, cards, 그림=None, now=None, lines=_미읽음):
    """한 프로젝트의 진행 카드마다 경보 0개 이상. `cards` 는 `merge_cards` 를 거친 카드(도는 기계·출처가 붙은 것).

    이 기기가 돌리는 카드(도는 기계가 보는 목록 `기기` 와 같음 — 출처와 상관없음)는 러너가 살아 있는지·실행 기록이 침묵 상한보다 조용한지,
    다른 기기가 돌리는 카드(출처 원격)는 원격 머리말 커밋이 시간 상한보다 오래돼 「소식 끊김」인지를 본다. 도는 기계나 이 기계 이름을 모르면 판단하지 않는다.
    `lines` 는 프로세스 목록(None 이면 못 읽음) — 주지 않으면 이 기계 카드가 있을 때 한 번 읽는다."""
    now = now or dt.datetime.now()
    mine = _기기_이름(item.get("기기"))
    table = stage_limit_table(item["path"])
    backlog = os.path.join(item["path"], "docs", "backlog")
    out = []
    for c in cards:
        if c["상태"] != "진행":
            continue
        num, sil, hard, 모름 = limits_for(table, c["단계"])
        label = "%s " % num if num else "단계 모름 · "

        def add(종류, 근거):
            out.append({"프로젝트": item["name"], "file": c["file"], "title": c["title"], "종류": 종류, "근거": 근거,
                        "열어 볼 곳": _주소(item["name"]) + urllib.parse.quote(c["file"])})

        if mine and _기기_이름(c.get("도는 기계")) == mine:      # 이 기기가 돌리는 카드 — 카드가 거울에서 왔어도 러너·실행 기록을 본다
            if lines is _미읽음:
                lines = process_lines()
            if runner_alive(lines, c["file"]) is False:
                add("실행이 살아 있지 않음", "프로세스 목록에 %s 의 러너 줄이 없음" % c["file"])
            last = live_last_time(backlog, c["file"])
            if last and (now - last).total_seconds() > sil:
                add("%d분째 조용함 — 상한 %d분" % ((now - last).total_seconds() // 60, sil // 60),
                    "실행 기록 마지막 줄 %s · %s침묵 상한 %d분" % (last.strftime("%H:%M"), label, sil // 60))
        elif c.get("출처") == "원격" and 그림:
            때 = ((그림.get("카드") or {}).get(c["file"]) or {}).get("커밋때")
            if 때 and (now - 때).total_seconds() > hard:
                add("소식 끊김 — 돌던 기기 확인",
                    "원격 머리말 마지막 커밋 %s(%d분째 바뀌지 않음) · %s시간 상한 %d분" % (때.strftime("%H:%M"), (now - 때).total_seconds() // 60, label, hard // 60))
    return out


def 러너가_멈춤(c):
    """보류 카드가 러너가 실행하다 멈춘 것인가 — 러너는 단계마다 `단계` 를 쓰고 보류로 바꿀 때 지우지 않는다. `단계` 가 빈 보류는 사람이 일부러 미룬 것.
    보류를 이 둘로 가르는 유일한 자리다."""
    return c["상태"] == "보류" and c.get("단계", "").strip() != ""


def _칸_고름(rows):
    """숫자 칸과 카드 목록이 함께 쓰는 고르기 — `{진행: [(줄, 카드)], 대기: [...], 멈춤: [(줄, 카드, 갈래)], 보류: [(줄, 카드)]}`.
    멈춤은 카드 한 장이 한 번만 나온다(러너가 멈춤 → 경보 → 예상 넘김 가운데 앞선 갈래). 일부러 미룬 보류는 멈춤이 아니라 보류 열쇠에 담는다.
    카드 목록에 없는 경보·넘김 파일은 센 적이 없다."""
    out = {"진행": [], "대기": [], "멈춤": [], "보류": []}
    for r in rows:
        cards = {c["file"]: c for c in r.get("카드") or []}
        stop = {}
        for c in cards.values():
            if c["상태"] == "진행":
                out["진행"].append((r, c))
            elif c["상태"] == "대기":
                out["대기"].append((r, c))
            elif c["상태"] == "보류":
                if 러너가_멈춤(c):
                    stop[c["file"]] = "러너가 멈춤"
                else:
                    out["보류"].append((r, c))
        for 갈래, files in (("경보", {a["file"] for a in r.get("경보") or []}), ("예상 넘김", set(r.get("예상 넘김") or []))):
            for f in sorted(files):
                if f in cards:
                    stop.setdefault(f, 갈래)
        out["멈춤"] += [(r, cards[f], g) for f, g in stop.items()]
    return out


def home_numbers(rows, today, 병합=None):
    """첫 화면 숫자 네 칸 — 보는 목록 모든 프로젝트의 합친 카드로 센다. 칸마다 `/cards?보기=…` 목록이 있고 줄 수가 칸 수와 같다.
    칸 = {이름, 수, 설명, 카드: [(프로젝트, 제목, 주소)], 위험, 고리}. `병합` 은 `week_merges` 결과(없으면 None → 수 「—」)."""
    고름 = _칸_고름(rows)
    ref = lambda r, c: (r["name"], c["title"], _주소(r["name"]) + urllib.parse.quote(c["file"]))  # noqa: E731
    run = [ref(r, c) for r, c in 고름["진행"]]
    wait = [ref(r, c) for r, c in 고름["대기"]]
    stop = [ref(r, c) for r, c, _ in 고름["멈춤"]]
    remote = sum(c.get("출처") == "원격" and not c.get("이기기돌림") for _, c in 고름["진행"])     # 출처는 거울에서 온 카드면 늘 원격이다 — 다른 기기 카드는 이 기기가 돌리는 카드를 뺀 것
    hold = sum(g == "러너가 멈춤" for _, _, g in 고름["멈춤"])
    alarm = sum(len({a["file"] for a in r.get("경보") or []}) for r in rows)
    over = sum(len(r.get("예상 넘김") or []) for r in rows)
    if 병합 is None:
        수, 설명 = "—", "원격 저장소를 아직 못 읽음"
    else:
        수 = 병합["자동"] + 병합["사람"]
        설명 = "자동 병합 %d · 사람 병합 %d" % (병합["자동"], 병합["사람"]) + (" · 원격 모름 %d곳" % 병합["모름"] if 병합["모름"] else "")
    고리 = lambda 보기: "/cards?보기=" + urllib.parse.quote(보기)  # noqa: E731
    연결 = sum(any(x.startswith("연결 확인") for x in r.get("원격") or []) for r in rows)
    return [
        {"이름": "진행 중", "수": len(run), "설명": "이 중 %d장 다른 기기" % remote, "카드": run, "위험": False, "고리": 고리("진행")},
        {"이름": "대기", "수": len(wait), "설명": "프로젝트 %d곳" % len({x[0] for x in wait}), "카드": wait, "위험": False, "고리": 고리("대기")},
        {"이름": "이번 주 완료", "수": 수, "설명": 설명, "카드": [], "위험": False, "고리": 고리("병합")},
        {"이름": "손볼 것", "수": len(stop), "설명": "멈춘 실행 %d · 예상 초과 %d" % (hold + alarm, over) + (" · 연결 확인 %d곳" % 연결 if 연결 else ""),
         "카드": stop, "위험": len(stop) >= 1, "고리": 고리("멈춤")}]


def _사람손_파일들(project):
    """사람 손 기록 파일 — 보드가 새로 쓰는 자리(`RESOLVED_DIR/<이름>`)와 옛 기록·배차가 쓰는 사본 `docs/plan/` 두 자리 모두."""
    return (sorted(glob.glob(os.path.join(RESOLVED_DIR, _폴더이름(project), "사람손-*.md")))
            + sorted(glob.glob(os.path.join(project, "docs", "plan", "사람손-*.md"))))


def read_resolved(items):
    """보는 목록 프로젝트의 `docs/plan/사람손-*.md` 기록 줄들 — [{프로젝트, 때, 글}], 때가 늦은 것이 위. 못 읽은 파일은 때가 빈 한 줄."""
    out = []
    for item in items:
        for path in _사람손_파일들(item["path"]):
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.read().splitlines()
            except (OSError, UnicodeDecodeError):
                out.append({"프로젝트": item["name"], "때": "", "글": "%s — 처리 기록 못 읽음" % item["name"]})
                continue
            for line in lines:
                if line.startswith("- "):
                    때, _, 글 = line[2:].partition(" · ")
                    out.append({"프로젝트": item["name"], "때": 때.strip(), "글": 글.strip()})
    return sorted(out, key=lambda x: x["때"], reverse=True)


def _진행_상태(mine):
    return "멈춤" if any(러너가_멈춤(c) for c in mine) else "진행" if any(c["상태"] in ("진행", "검토") for c in mine) else "쉼"


def _시나리오_배지(cards, s):
    """시나리오 하나의 배지 글 — 「완료」(시나리오 상태가 완료일 때만)·「완료 선언 대기」·「멈춤」·「보류 n으로 남음」·「진행」·「쉼」."""
    if s["상태"] == "완료":
        return "완료"
    mine = [c for c in cards if c["시나리오"] == s["번호"]]
    p = scenario_progress(cards, s)
    return ("완료 선언 대기" if p["분모"] and p["수"]["완료"] == p["분모"] else "멈춤" if any(러너가_멈춤(c) for c in mine)
            else "보류 %d으로 남음" % p["수"]["보류"] if p["수"]["보류"] and p["수"]["완료"] + p["수"]["보류"] == p["분모"] else _진행_상태(mine))


def _시나리오_줄(cards, scenarios):
    """첫 화면 진행 표의 줄 — 완료 아닌 시나리오마다 하나, 시나리오 없는 완료 아닌 카드가 있으면 「낱장 카드 n」, 아무것도 없으면 「열린 일 없음」."""
    상태 = _진행_상태
    out = []
    known = {s["번호"] for s in scenarios}
    for s in scenarios:
        if s["상태"] == "완료":
            continue
        mine = [c for c in cards if c["시나리오"] == s["번호"]]
        p = scenario_progress(cards, s)
        배지 = _시나리오_배지(cards, s)
        out.append({"시나리오": ("%s %s" % (s["번호"], s["이름"])).strip(), "상태": 배지, "완료": p["수"]["완료"], "분모": p["분모"],
                    "퍼센트": p["퍼센트"], "남은 예상": remaining_text(cards, mine), "기계": " · ".join(p["기기"]) or "—"})
    낱장 = [c for c in cards if c["시나리오"] not in known and c["상태"] != "완료"]
    if 낱장:
        out.append({"시나리오": "시나리오 밖 카드 %d" % len(낱장), "상태": 상태(낱장), "완료": None, "분모": None, "퍼센트": None,
                    "남은 예상": remaining_text(cards, 낱장), "기계": " · ".join(_기기들(낱장)) or "—"})
    return out or [{"시나리오": "열린 일 없음", "상태": "쉼", "완료": None, "분모": None, "퍼센트": None, "남은 예상": "—", "기계": "—"}]


def project_row(item):
    """첫 화면의 프로젝트 한 줄. 못 읽으면 그 줄의 「까닭」에만 적는다."""
    row = {"name": item["name"], "수": {c: 0 for c in COLUMNS[1:5]}, "도는 카드": [], "차례 수": 0,
           "마지막 움직임": "없음", "까닭": "", "표시": list(item["표시"]), "원격": [], "남은 예상": "—",
           "상태수": {c: 0 for c in COLUMNS}, "배지": "", "진행률": None, "기기": [], "위험": [],
           "경보": [], "카드": [], "예상 넘김": [], "시나리오 줄": [], "요소 칸 수": {"완료": 0, "진행": 0, "보류": 0, "멈춤": 0, "아이디어": 0, "없음": 0}}
    try:
        cards, scenarios, _, turns = _읽기(item, 합치기=True)
    except Exception as e:                           # 한 프로젝트의 실패가 다른 줄을 막지 않는다
        row["까닭"] = str(e)
        return row
    for c in cards:
        if c["상태"] in row["수"]:
            row["수"][c["상태"]] += 1
    for c in cards:
        if c["상태"] in row["상태수"]:
            row["상태수"][c["상태"]] += 1
    st = row["상태수"]
    row["배지"] = "멈춤 있음" if st["보류"] else "진행 중" if st["진행"] else "쉼"
    ps = [scenario_progress(cards, s) for s in scenarios]
    total = sum(p["분모"] for p in ps)
    row["진행률"] = sum(p["수"]["완료"] for p in ps) * 100 // total if total else None
    row["기기"] = _기기들(cards)
    row["위험"] = ["보류 카드 %d장" % st["보류"]] if st["보류"] else []
    over = [c["file"] for c in cards if _넘김(cards, c)]
    if over:
        row["위험"].append("예상 넘김 카드 %d장" % len(over))
    row["예상 넘김"], row["카드"] = over, cards
    row["경보"] = 경보_알림결과(item["path"], stalled_alarms(item, cards, REMOTE.get(os.path.abspath(item["path"]))))
    if row["경보"]:
        row["위험"].append("멈춘 카드 경보 %d장" % len({a["file"] for a in row["경보"]}))
    known = {s["번호"] for s in scenarios}
    row["남은 예상"] = remaining_text(cards, [c for c in cards if c["시나리오"] in known])
    row["시나리오 줄"] = _시나리오_줄(cards, scenarios)
    for s in scenarios:
        for k in 요소_칸(cards, s):
            row["요소 칸 수"][k["모양"]] += 1
    row["도는 카드"] = [(c["title"], c["단계"]) for c in cards if c["상태"] == "진행"]
    row["차례 수"] = len(turns)
    row["원격"] = _원격_문구(item, cards)
    backlog = os.path.join(item["path"], "docs", "backlog")
    그림 = REMOTE.get(os.path.abspath(item["path"])) or {}
    if 그림.get("끝") and 그림.get("끝때"):            # 거울에서 읽는 프로젝트 — 원격 끝 커밋 때
        row["마지막 움직임"] = 그림["끝때"].strftime("%Y-%m-%d %H:%M")
        return row
    try:
        last = max(os.path.getmtime(os.path.join(backlog, c["file"])) for c in cards if not c.get("원격만"))
        row["마지막 움직임"] = dt.datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        pass
    return row


def _주소(name):
    return "/p/%s/" % urllib.parse.quote(name, safe="")


_원격_빔 = set()      # 원격 그림이 비어 있거나 못 읽은 채로 본 프로젝트 경로 — 그 뒤 처음 채워진 바퀴는 조용히 기록만 한다


def _원격_처음_채움(project):
    """이 바퀴가 「비어 있던 원격 그림이 처음 채워진 바퀴」인가. 읽은 적 없는 오류 그림(때 없음)은 비어 있는 것으로 센다.
    앞서 읽은 카드를 두고 오류만 붙은 그림은 비어 있지 않다 — 회복 바퀴를 처음 채워진 바퀴로 오인하지 않는다."""
    key = os.path.abspath(project)
    pic = REMOTE.get(key)
    if not pic or pic.get("없음") or (pic.get("오류") and not pic.get("때")):
        _원격_빔.add(key)
        return False
    if key in _원격_빔:
        _원격_빔.discard(key)
        return True
    return False


def _서버_고리(x):
    """서버 화면에서 끝 보고·할 일 카드 고리 `../plan/<x>`·`../todo/<x>` 를 `plan/<x>`·`todo/<x>` 로 — 서버는 `docs/plan/`·`docs/todo/` 를 `/p/<이름>/` 아래에 낸다.
    정적 board.html 은 이 풀이를 안 거친다."""
    for 폴더 in ("plan", "todo"):
        if x.startswith("../%s/" % 폴더):
            return x[len("../"):]
    return x


def all_turns(projects, sync=False):
    """보는 목록 모든 프로젝트의 사람 차례 한 목록 — 오래 기다린 것이 위. 못 읽은 프로젝트는 맨 위 한 줄.

    sync=True 면 프로젝트마다 `sync_turns` 로 새 항목을 알리고 사라진 항목을 기록한다."""
    bad, out = [], []
    for item in projects:
        try:
            cards, _, _, turns = _읽기(item, 합치기=True)
            if sync:
                mine = {t["열쇠"] for t in _읽기(item, 합치기=False)[3]}
                for t in turns:
                    t["원격"] = t["열쇠"] not in mine
                그림 = REMOTE.get(os.path.abspath(item["path"]))
                조용히 = _원격_처음_채움(item["path"])
                turns = sync_turns(item["path"], turns, 조용히, 기록=not item.get("보기"))
                remote_files = {c["file"] for c in cards if c.get("출처") == "원격" and not c.get("이기기돌림")}
                alarms = [{**a, "원격": a["file"] in remote_files} for a in stalled_alarms(item, cards, 그림)]
                sync_alarms(item, alarms, 조용히)
            else:
                turns = _알림결과(item["path"], turns)
        except Exception as e:                       # 한 프로젝트가 실패해도 나머지는 계속 돈다
            bad.append({"열쇠": "못 읽음:" + item["name"], "종류": "못 읽음", "프로젝트": item["name"], "시나리오": "",
                        "제목": "%s — 이 프로젝트 사람 차례 못 읽음" % item["name"], "기다리는 것": str(e),
                        "생긴 때": "", "열어 볼 곳": _주소(item["name"]) + "board.html", "알림": None})
            continue
        out += [{**t, "프로젝트": item["name"], "열어 볼 곳": _주소(item["name"]) + _서버_고리(t["열어 볼 곳"])} for t in turns]
    return bad + sorted(out, key=lambda t: (t["생긴 때"] == _모름, t["생긴 때"]))


def _경보_파일(project):
    return os.path.join(HUMAN_DIR, os.path.basename(os.path.abspath(project)) + ".경보.json")


def _경보_열쇠(a):
    return "경보:%s:%s:%s" % (a["프로젝트"], a["file"], re.sub(r"\d+분째", "분째", a["종류"]))


def sync_alarms(item, alarms, 조용히=False):
    """경보 저장 목록(`<폴더명>.경보.json`)과 비교해 새 경보를 맥 알림 한 번으로 알린다. 사라진 경보는 지운다.
    저장 파일이 없거나 `조용히` 이거나 다른 기계 경보(`원격`)가 조용히 바퀴면 알리지 않고 기록만 한다. 알림 결과를 붙인 목록을 돌려준다."""
    path = _경보_파일(item["path"])
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
        if not isinstance(old, dict):
            old = {}
    except (OSError, ValueError):
        old = {}
        조용히 = True if not os.path.exists(path) else 조용히
    now = {}
    for a in alarms:
        k = _경보_열쇠(a)
        if k in old:
            a["알림"] = old[k].get("알림")
        elif 조용히 and (a.get("원격") or not os.path.exists(path)):
            a["알림"] = None
        else:
            a["알림"] = notify("멈춘 카드 경보 — %s" % a["프로젝트"], "%s — %s" % (a["종류"], a["title"]))
        now[k] = {"알림": a["알림"]}
    try:
        os.makedirs(HUMAN_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(now, f, ensure_ascii=False)
    except OSError as e:
        print("경보 저장 실패: %s" % e)
    return alarms


def 경보_알림결과(project, alarms):
    """저장 목록의 알림 결과만 경보에 붙인다 (쓰지 않는다)."""
    try:
        with open(_경보_파일(project), encoding="utf-8") as f:
            old = json.load(f)
        for a in alarms:
            a["알림"] = old.get(_경보_열쇠(a), {}).get("알림")
    except (OSError, ValueError, AttributeError):
        pass
    return alarms


# ── 공통 사이드바 (ISSUE-ui-1-shell-home-2026-11-22) ──
# 서버가 그리는 모든 화면의 본문 왼쪽에 `보드서버._그리기` 한 곳에서 씌운다. 정적 board.html(file://)에는 붙지 않는다.
# 사이드바에는 `<section`·`<h2`·`class="card"`·`<script`·`board.html"` 글자를 쓰지 않는다 — 화면에서 그 글자를 세는 시험이 있다.
CAP_기본 = 4                # 배차의 `CAP`(bin/dispatch.py)과 같다 — 보드는 표준 라이브러리만 가져와 다시 적고, 같은지는 시험이 대조한다
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def max_parallel_read():
    """이 기계의 동시 상한 — 목록 파일 맨 위 `maxParallel`(1 이상 정수, 참·거짓 값 아님), 없거나 잘못이면 `CAP_기본`. 알리지 않는다."""
    try:
        with open(LIST_FILE, encoding="utf-8") as f:
            v = json.load(f).get("maxParallel", CAP_기본)
    except (OSError, ValueError, AttributeError):
        return CAP_기본
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else CAP_기본


def this_machine_read():
    """이 기계의 이름 — 목록 파일 맨 위 `"이 기계"`(비지 않은 글), 없거나 잘못이면 빈 값. 알리지 않는다. 배차·러너에도 같은 읽기가 있다(보드는 표준 라이브러리만)."""
    try:
        with open(LIST_FILE, encoding="utf-8") as f:
            v = json.load(f).get("이 기계")
    except (OSError, ValueError, AttributeError):
        return ""
    return v.strip() if isinstance(v, str) else ""


def plugin_version_read():
    """보드 뿌리의 `.claude-plugin/plugin.json` 의 `version` 글, 못 읽으면 None."""
    try:
        with open(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            v = json.load(f).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, str) and v else None


_사이드바_머리 = ('<nav class="side" aria-label="보드 메뉴"><div class="brand"><span class="logo">SL</span><div><b>작업 보드</b>'
              '<small>이 기기 · %s</small></div></div>'
              '<button type="button" id="pk"><span>검색·이동</span><kbd>⌘K</kbd></button>')


def _사이드바_고리(href, 글, 현재, 배지="", 뜨거움=False):
    return '<a href="%s"%s>%s%s</a>' % (html.escape(href, quote=True), ' aria-current="page"' if 현재 else "", 글,
                                       '<span class="n%s">%s</span>' % (" hot" if 뜨거움 else "", 배지) if 배지 != "" else "")


DISPATCH_STALE_SEC = 300     # 이보다 오래 쓰지 않은 `dispatch.json` 은 「배차 꺼짐」 — 바퀴(60초)보다 길고 원격 낡음 경고와 같은 값


def _pid_살아있나(pid):
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OSError, OverflowError):
        return False
    return True


def dispatch_lines(items, rows, now=None):
    """사이드바 상자에 더할 배차 줄 `[(글, 경고인가)]`. 이 기기는 `STATUS_DIR/dispatch.json`, 다른 기기는 원격 카드 머리말로 추정한다.
    보드는 배차를 가져오지 않는다 — 파일 이름과 꺼짐 판단을 여기서 따로 안다."""
    now = now or dt.datetime.now()
    out, this = [], ""
    try:
        with open(os.path.join(STATUS_DIR, "dispatch.json"), encoding="utf-8") as f:
            data = json.load(f)
        때 = dt.datetime.strptime(data["때"], "%Y-%m-%dT%H:%M:%S")
        pid = int(data["pid"])
        this = _기기_이름(data.get("기기"))
        분 = max(0, int((now - 때).total_seconds() // 60))
        if (now - 때).total_seconds() <= DISPATCH_STALE_SEC and _pid_살아있나(pid):
            글, 경고 = "배차 켜짐 · 마지막 바퀴 %d분 전" % 분, False
            if data.get("멈춤"):
                글 += " · 멈춤 파일 있음"
            if data.get("판바뀜"):
                글 += " · 판 바뀜 %s — 새 판에서 --상주-켜기 해야 새 카드를 집음" % data["판바뀜"]
                경고 = True
            out.append((글, 경고))
        else:
            out.append(("배차 꺼짐", False))
    except FileNotFoundError:
        out.append(("배차 꺼짐", False))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        out.append(("배차 상태 못 읽음", True))
    이름들, 소식 = {_기기_이름(i.get("기기")) for i in items}, {}
    for i in items:
        for c in ((REMOTE.get(os.path.abspath(i["path"])) or {}).get("카드") or {}).values():
            fm = c.get("머리말") or {}
            기계 = _기기_이름(fm.get("돈기계"))
            if fm.get("상태") == "진행" and 기계:
                이름들.add(기계)
                if c.get("커밋때") and (기계 not in 소식 or c["커밋때"] > 소식[기계]):
                    소식[기계] = c["커밋때"]
    for 이름 in sorted(이름들 - {"", this}):
        글 = "%s · 배차 원격으로 모름" % 이름
        if 이름 in 소식:
            글 += " · 마지막 카드 소식 %d분 전" % max(0, int((now - 소식[이름]).total_seconds() // 60))
        out.append((글, False))
    return out


def render_shell(items, rows, turns, path):
    """모든 서버 화면 왼쪽의 `<nav class="side">`. `rows` 는 `items` 와 같은 순서의 `project_row`, `turns` 는 `my_turns` 결과, `path` 는 요청 경로.
    어떤 예외가 나도 최소 사이드바(고리 넷)를 돌려준다 — 화면 본문은 사이드바 때문에 막히지 않는다."""
    try:
        esc = html.escape
        names = {_기기_이름(i.get("기기")) for i in items} - {""}
        이기계 = next(iter(names)) if len(names) == 1 else ""
        러너 = (str(sum(1 for r in rows for c in r["카드"] if c["상태"] == "진행" and _기기_이름(c.get("도는 기계")) == 이기계))
              if 이기계 else "—")
        지금 = len(split_turns(turns, dt.datetime.now())[0])
        m = re.match(r"/p/([^/]+)/", path)
        현재 = urllib.parse.unquote(m.group(1)) if m else None
        이름 = 화면이름
        링크 = [_사이드바_고리("/", 이름["home"], path == "/"), _사이드바_고리("/#todo", 이름["todo"], False, str(지금), 지금 > 0),
              _사이드바_고리("/cards?보기=진행", 이름["cards"], path == "/cards")]
        분석 = [_사이드바_고리("/timeline", 이름["timeline"], path == "/timeline"), _사이드바_고리("/signals", 이름["usage"], path == "/signals"),
              _사이드바_고리("/retro", 이름["retro"], path == "/retro")]
        프로젝트 = []
        for item, r in zip(items, rows):
            수 = sum(1 for c in r["카드"] if c["상태"] == "진행")
            고름 = _칸_고름([r])
            막힘 = len(고름["멈춤"])
            색 = "gray" if r["까닭"] else "orange" if 막힘 else "blue" if 수 else "ochre" if 고름["보류"] else "gray"
            아래 = ""
            if item["name"] == 현재:
                주 = _주소(item["name"])
                아래 = '<div class="sub">%s</div>' % "".join(
                    _사이드바_고리(주 + 끝, 이름[키], path == 주 + 끝) for 키, 끝 in (("board", ""), ("features", "features"), ("graph", "graph"), ("improve", "improve")))
            프로젝트.append('<a href="%s"%s><span class="dot-%s"></span><span class="pn">%s</span><span class="n">%d · %d</span></a>%s'
                        % (esc(_주소(item["name"]), quote=True), ' aria-current="page"' if item["name"] == 현재 else "", 색, esc(item["name"]), 수, 막힘, 아래))
        때들 = [(REMOTE.get(os.path.abspath(i["path"])) or {}).get("때") for i in items]
        때들 = [t for t in 때들 if t]
        가장 = min(때들) if 때들 else None
        원격 = ["원격 읽음 %s" % (가장.strftime("%H:%M" if 가장.date() == dt.date.today() else "%Y-%m-%d %H:%M") if 가장 else "—")]
        # 못 읽은 프로젝트는 자기 마지막 읽은 시각(「원격 읽음」)을 걸러내지 않는다 — 위의 전체 시각이 다른 프로젝트 것이어도 가려지지 않게
        원격 += [("%s · %s" % (i["name"], x), _원격_경고줄(x)) for i, r in zip(items, rows) for x in r.get("원격", [])
               if not x.startswith("원격 읽음") or any(y.startswith("원격 저장소를 아직 못 읽음") for y in r.get("원격", []))]
        상자 = ["러너 %s / 상한 %d" % (러너, max_parallel_read())] + 원격 + dispatch_lines(items, rows) + ["stop-loop %s" % (plugin_version_read() or "—")]
        return ((_사이드바_머리 % esc(이기계 or "—")) + '<div class="grp">%s</div>' % "".join(링크)
                + '<div class="grp"><span class="gh">프로젝트</span>%s<span class="gh">숫자는 진행 중 · 막힘</span></div>' % ("".join(프로젝트) or '<span class="nolink">보는 프로젝트 없음</span>')
                + '<div class="grp"><span class="gh">분석</span>%s</div>' % "".join(분석)
                + '<div class="sbox">%s</div></nav>' % "".join('<span class="warn">%s</span>' % esc(x[0]) if isinstance(x, tuple) and x[1] else "<span>%s</span>" % esc(x[0] if isinstance(x, tuple) else x) for x in 상자))
    except Exception:
        return 최소_사이드바(path)


def 최소_사이드바(path):
    def 묶음(고리들):
        return "".join(_사이드바_고리(href, 화면이름[k], path == cur) for href, k, cur in 고리들)
    return ('<nav class="side" aria-label="보드 메뉴"><div class="grp">%s</div><div class="grp"><span class="gh">분석</span>%s</div>'
            '<div class="sbox"><span>사이드바 못 그림</span></div></nav>'
            % (묶음((("/", "home", "/"), ("/#todo", "todo", None), ("/cards?보기=진행", "cards", "/cards"))),
               묶음((("/timeline", "timeline", "/timeline"), ("/signals", "usage", "/signals"), ("/retro", "retro", "/retro")))))


def with_shell(page, nav):
    """첫 `<body>` 를 사이드바와 본문 상자로, 마지막 `</body>` 를 닫는 글로 바꾼다. `<body>` 가 없으면 그대로."""
    if "<body>" not in page:
        return page
    page = _화면들_채우기(page).replace("<body>", '<body class="with-side"><div class="shell">' + nav + '<div class="page">', 1)
    i = page.rindex("</body>")
    return page[:i] + "</div></div>" + page[i:]


def render_alarms(alarms, href=lambda a: a["열어 볼 곳"]):
    """경보 묶음. 없으면 빈 글 — 그 자리를 그리지 않는다."""
    if not alarms:
        return ""
    esc = html.escape
    return '<div class="alarms"><h2>멈춘 카드 경보 (%d)</h2>%s</div>\n' % (len(alarms), "".join(
        '<a class="card" href="%s"><h3>%s · %s</h3><p class="meta">%s — %s%s</p></a>'
        % (esc(href(a), quote=True), esc(a["프로젝트"]), esc(a["title"]), esc(a["종류"]), esc(a["근거"]),
           " · 알림 못 띄움" if a.get("알림") is False else "") for a in alarms))


NOW_MAX = 5                 # 첫 화면 「지금」 탭에 보이는 줄 수
_종류_색 = {"멈춤": "orange", "경보": "orange", "못 읽음": "orange", "사람이 병합할 PR": "blue", "완료 선언": "teal"}


def my_turns(rows, turns):
    """내 차례 한 목록 — 사람 차례(`all_turns`)에 멈춘 카드 경보(종류 「경보」)를 합친 것. 못 읽음 줄이 맨 위, 그 아래 경보, 그 아래 나머지."""
    alarms = [{"열쇠": "경보:%s:%s" % (a["프로젝트"], a["file"]), "종류": "경보", "프로젝트": a["프로젝트"], "시나리오": "", "제목": a["title"],
               "기다리는 것": "%s — %s" % (a["종류"], a["근거"]), "생긴 때": "", "열어 볼 곳": a["열어 볼 곳"], "알림": a.get("알림")}
              for r in rows for a in r.get("경보") or []]
    미룸 = {(r["name"], c["file"]) for r, c in _칸_고름(rows)["보류"]}       # 일부러 미룬 보류는 내 할 일이 아니다 — 보류 보기에서 본다
    bad = [t for t in turns if t["종류"] == "못 읽음"]
    return bad + alarms + [t for t in turns if t["종류"] != "못 읽음" and not (t["종류"] == "멈춤" and (t["프로젝트"], t["열쇠"].partition(":")[2]) in 미룸)]


def split_turns(turns, now):
    """`(지금, 오래된)` — 오래된 것은 종류 「멈춤」이면서 생긴 때가 지금보다 24시간 넘게 앞선 것. 생긴 때를 읽거나 뺄 수 없으면 오래된 것이 아니다."""
    지금, 오래 = [], []
    for t in turns:
        old = False
        if t["종류"] == "멈춤":
            try:
                old = now - dt.datetime.fromisoformat(t["생긴 때"]) > dt.timedelta(hours=24)
            except (ValueError, TypeError):
                pass
        (오래 if old else 지금).append(t)
    return 지금, 오래


def _병합_누가(제목):
    """병합 커밋 제목 → `(누가, 카드)`(누가 = 「러너」·「사람」), 규칙에 맞지 않으면 None."""
    m = re.match(r"\[loop\] (.+?) — 자동 병합", 제목)
    if m:
        return "러너", m.group(1)
    m = re.match(r"Merge pull request #\d+ from [^/\s]+/loop/(\S+)", 제목) or re.match(r"Merge branch 'loop/([^']+)'", 제목)
    return ("사람", m.group(1)) if m else None


def _이번주_병합(items, today):
    """이번 주(월요일 0시부터) 원격 기준 가지 끝에서 닿는 병합 커밋 — `(줄들, 모름)`. 줄 = {프로젝트, 누가, 카드, 때}.
    끝을 읽은 프로젝트가 없으면 줄들은 None. 끝을 모르거나 git 이 실패한 프로젝트는 `모름` 으로 센다."""
    monday = dt.datetime.combine(today - dt.timedelta(days=today.weekday()), dt.time()).timestamp()
    줄들, 모름 = [], 0
    읽음 = 0
    for item in items:
        끝 = (REMOTE.get(os.path.abspath(item["path"])) or {}).get("끝")
        try:
            r = subprocess.run(["git", "-C", _읽을_저장소(item["path"]), "log", 끝, "--merges", "--format=%ct\t%s"],
                               capture_output=True, text=True, timeout=30) if 끝 else None
        except (OSError, subprocess.SubprocessError):
            r = None
        if r is None or r.returncode != 0:
            모름 += 1
            continue
        읽음 += 1
        for 줄 in r.stdout.splitlines():             # 때는 직접 거른다 — `--since` 는 날짜가 거꾸로 선 이력에서 앞에서 멈춘다
            때, _, 제목 = 줄.partition("\t")
            누가 = _병합_누가(제목) if 때.isdigit() and int(때) >= monday else None
            if 누가:
                줄들.append({"프로젝트": item["name"], "누가": 누가[0], "카드": 누가[1], "때": dt.datetime.fromtimestamp(int(때))})
    return (줄들 if 읽음 else None), 모름


def week_merges(items, today):
    """이번 주 병합 커밋 수 — `{자동, 사람, 모름}`. 끝을 읽은 프로젝트가 없으면 None. 세는 규칙은 `_이번주_병합` 하나다."""
    줄들, 모름 = _이번주_병합(items, today)
    if 줄들 is None:
        return None
    return {"자동": sum(x["누가"] == "러너" for x in 줄들), "사람": sum(x["누가"] == "사람" for x in 줄들), "모름": 모름}


def _배지_색(상태):
    if 상태.startswith("보류"):
        return "ochre"
    return {"완료": "teal", "완료 선언 대기": "teal", "멈춤": "orange", "읽지 못함": "orange", "진행": "blue", "검토": "blue", "대기": "blue"}.get(상태, "gray")


카드_보기 = ("진행", "대기", "병합", "멈춤", "보류")


def 카드_목록(rows, 보기, 병합줄, now):
    """`/cards` 의 묶음 — `[{이름, 줄: [{프로젝트, 상태, 제목, 까닭, 때, 기계, href}]}]`. href 가 빈 글자면 고리 없음.
    `보기` 가 넷 밖이거나 병합인데 `병합줄` 이 None(원격 끝을 못 읽음)이면 None. 칸 숫자와 같은 고르기(`_칸_고름`)·같은 병합 줄(`_이번주_병합`)이다."""
    if 보기 not in 카드_보기 or (보기 == "병합" and 병합줄 is None):
        return None
    묶음 = {}

    def 더(이름, 줄):
        묶음.setdefault(이름, []).append(줄)

    if 보기 == "병합":
        파일 = {r["name"]: {c["file"]: c for c in r.get("카드") or []} for r in rows}
        for x in sorted(병합줄, key=lambda x: (x["프로젝트"], -x["때"].timestamp())):
            c = 파일.get(x["프로젝트"], {}).get(x["카드"] + ".md")
            더(x["프로젝트"], {"프로젝트": x["프로젝트"], "상태": "병합", "제목": c["title"] if c else x["카드"], "까닭": x["누가"] + " 병합",
                            "때": x["때"].strftime("%Y-%m-%d"), "기계": "—", "href": _주소(x["프로젝트"]) + urllib.parse.quote(c["file"]) if c else ""})
        return [{"이름": k, "줄": v} for k, v in 묶음.items()]
    for r, c, *갈래 in _칸_고름(rows)[보기]:
        if 갈래:
            종류 = next((a["종류"] for a in r.get("경보") or [] if a["file"] == c["file"]), "") if 갈래[0] == "경보" else ""
            글 = 종류 or _한줄(r.get("카드") or [], c, {}, now)[0]
            if 보기 == "멈춤" and 갈래[0] != "경보":
                글 = c["멈춘이유"] or "까닭 없음"
        else:
            글 = _한줄(r.get("카드") or [], c, {}, now)[0]
        더(갈래[0] if 갈래 else r["name"],
           {"프로젝트": r["name"], "상태": c["상태"], "제목": c["title"], "까닭": 글, "때": c["갱신"],
            "기계": c.get("도는 기계") or c.get("돈기계") or c.get("기기") or "—", "href": _주소(r["name"]) + urllib.parse.quote(c["file"])})
    순서 = ("러너가 멈춤", "경보", "예상 넘김")
    이름들 = [k for k in 순서 if k in 묶음] if 보기 == "멈춤" else list(묶음)
    return [{"이름": k, "줄": 묶음[k]} for k in 이름들]


_보기_제목 = {"진행": "진행 중인 카드", "대기": "대기 중인 카드", "병합": "이번 주 완료", "멈춤": "손볼 것", "보류": "보류"}


def render_cards(보기, 묶음들, 덧=()):
    """카드 목록 화면 HTML — 검색 결과 묶음(`.sr-g`·`.sr-h`·`.sr-r`)과 같은 모양. `묶음들` 이 None 이면 원격을 아직 못 읽은 병합 목록.
    한 줄은 고리가 있으면 `<a class="sr-r">`, 없으면 `<div class="sr-r">`(줄 세기는 둘 다 같다). 값은 모두 escape 한다."""
    esc = html.escape
    parts = ["<h1>%s</h1>" % esc(_보기_제목.get(보기, 보기))]
    parts += ["<p>%s</p>" % esc(x) for x in 덧]
    if 묶음들 is None:
        parts.append("<p>원격 저장소를 아직 못 읽음</p>")
    elif not 묶음들:
        parts.append('<p>없음 · <a href="/">%s</a></p>' % 화면이름["home"])
    for g in 묶음들 or []:
        rows = []
        for x in g["줄"]:
            속 = ('<span class="sr-meta"><span class="badge b-%s">%s</span><span class="sr-b">%s</span><span class="sr-m">%s · %s</span></span>'
                 '<span class="sr-t">%s</span><span class="sr-l">%s</span>'
                 % (_배지_색(x["상태"]), esc(x["상태"]), esc(x["프로젝트"]), esc(x["때"]), esc(_기기_글(x["기계"])), esc(x["제목"]), esc(x["까닭"])))
            rows.append('<a class="sr-r" href="%s">%s</a>' % (esc(x["href"], quote=True), 속) if x["href"] else '<div class="sr-r">%s</div>' % 속)
        parts.append('<div class="sr-g" data-g="%s"><div class="sr-h"><b>%s</b><span class="sr-n">%d</span></div>%s</div>'
                     % (esc(g["이름"]), esc(g["이름"]), len(g["줄"]), "".join(rows)))
    틀 = FILE_TEMPLATE.replace('<a href="board.html">← 보드</a>', '<a href="/">← %s</a>' % 화면이름["home"])
    틀 = 틀.replace("<h1>%(name)s</h1>\n", "", 1)           # 제목은 본문 `<h1>` 하나
    return 틀 % {"name": esc(_보기_제목.get(보기, 보기)), "body": "\n".join(parts)}


def render_home(rows, turns, 오류, tab="", 처리=(), today=None, machines="", now=None, 병합=None, 기계수=None, feed=""):
    """첫 화면 — 숫자 네 칸, 프로젝트 진행 표, 내 차례(세 탭)와 최근 활동 나란히, 기계 줄 순서. `turns` 는 `my_turns` 결과.
    `tab` 이 old·done 이 아니면 「지금」. `machines`·`feed` 는 서버가 그린 조각."""
    esc = html.escape
    today = today or dt.date.today()
    now = now or dt.datetime.now()
    tab = tab if tab in ("old", "done") else ""
    지금, 오래 = split_turns(turns, now)

    def li(t):
        색 = _종류_색.get(t["종류"], "gray")
        단추 = "완료 보고서 보기" if t["종류"] == "완료 선언" else "보드 보기" if t["종류"] == "못 읽음" else "카드 보기"
        메타 = " · ".join(x for x in (t["프로젝트"], t["기다리는 것"], t["생긴 때"], "알림 못 띄움" if t.get("알림") is False else "") if x)
        return ('<li class="turn"><span class="kind k-%s">%s</span><div class="tbody"><div class="tt">%s</div><div class="tm">%s</div></div>'
                '<a class="tact" href="%s">%s</a></li>' % (색, esc(t["종류"]), esc(t["제목"]), esc(메타), esc(t["열어 볼 곳"], quote=True), 단추))

    def lst(items, 없음):
        return '<ul class="tlist">%s</ul>' % "".join(li(t) for t in items) if items else '<p class="more">%s</p>' % 없음

    if tab == "old":
        box = lst(오래, "오래 기다림 없음")
    elif tab == "done":
        box = ('<ul class="tlist">%s</ul>' % "".join(
            '<li class="turn"><div class="tbody"><div class="tt">%s</div><div class="tm">%s</div></div></li>'
            % (esc(x["글"]), esc(" · ".join(y for y in (x["때"], x["프로젝트"]) if y))) for x in 처리)) if 처리 else '<p class="more">처리함 없음</p>'
    else:
        box = lst(지금[:NOW_MAX], "%s 없음" % 화면이름["todo"] if not turns else "새로 온 %s 없음" % 화면이름["todo"])
        if len(지금) > NOW_MAX:
            box += '<div class="more">%d개 더</div>' % (len(지금) - NOW_MAX)
        if 오래:
            per = {}
            for t in 오래:
                per[t["프로젝트"]] = per.get(t["프로젝트"], 0) + 1
            box += '<div class="more">오래된 멈춤 %d장은 「오래 기다림」에 — %s</div>' % (len(오래), esc(" · ".join("%s %d" % kv for kv in per.items())))
    탭들 = (("", "새로 온 것 %d" % len(지금), "/"), ("old", "오래 기다림 %d" % len(오래), "/?tab=old"), ("done", "처리함 %d" % len(처리), "/?tab=done"))
    tabs = '<nav class="tabs">%s</nav>' % "".join(
        '<a%s href="%s">%s</a>' % (' class="on"' if k == tab else "", href, label) for k, label, href in 탭들)
    미룸 = len(_칸_고름(rows)["보류"])
    if 미룸:
        box += '<p class="more"><a href="/cards?보기=%s">보류 %d장은 따로 보기</a></p>' % (urllib.parse.quote("보류"), 미룸)
    turnp = '<section class="panel turnp" id="todo" aria-labelledby="h-turn"><div class="ph"><h2 id="h-turn">%s</h2>%s</div>%s</section>' % (esc(화면이름["todo"]), tabs, box)

    def tile(c):
        return ('<a class="tile%s%s" href="%s"><span class="lb">%s</span><span class="v">%s</span><span class="sub">%s</span></a>'
                % (" risk" if c["위험"] else "", " good" if c["이름"] == "이번 주 완료" and c["수"] != "—" else "",
                   esc(c["고리"], quote=True), esc(c["이름"]), c["수"], esc(c["설명"])))
    numbers = '<section class="tiles" aria-label="요약">%s</section>\n' % "".join(tile(c) for c in home_numbers(rows, today, 병합))

    def prows(r):
        link = '<a class="pl" href="%sboard.html">%s</a>' % (esc(_주소(r["name"]), quote=True), esc(r["name"]))
        덧 = r["표시"] + ([r["까닭"]] if r["까닭"] else [])
        sub = '<div class="sub">%s</div>' % esc(" · ".join(덧)) if 덧 else ""
        if r["까닭"]:
            return ('<tr><td class="pn">%s%s</td><td><span class="badge b-orange">읽지 못함</span></td><td><div class="pbar"><span>—</span></div></td>'
                    '<td>—</td><td>—</td></tr>' % (link, sub))
        줄 = r.get("시나리오 줄") or []
        기다림 = sum(1 for x in 줄 if x["상태"] == "완료 선언 대기")
        보류 = (r.get("상태수") or {}).get("보류", 0)
        배지 = ('<span class="badge b-teal">완료 선언 대기 %d</span> ' % 기다림 if 기다림 else "") + ('<span class="badge b-ochre">보류 %d</span>' % 보류 if 보류 else "")
        수 = r.get("요소 칸 수") or {}
        전체 = sum(수.values())
        if 전체:
            조각 = "".join('<i class="seg seg-%s" style="width:%d%%"></i>' % (cls, 수.get(k, 0) * 100 // 전체)
                         for k, cls in _칸_클래스.items() if 수.get(k, 0))
            막대 = '<div class="pbar"><div class="pb2">%s</div><span>%d / %d 요소</span></div>' % (조각, 수.get("완료", 0), 전체)
        else:
            막대 = '<div class="pbar"><span>시나리오 없음</span></div>'
        막대 += ' <a class="fl" href="%sfeatures">%s</a>' % (esc(_주소(r["name"]), quote=True), esc(화면이름["features"]))
        기계 = " · ".join(r.get("기기") or []) or "—"
        return ('<tr><td class="pn">%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
                % (link, sub, 배지, 막대, esc(r.get("남은 예상", "—")), esc(_기기_글(기계))))
    head = "".join("<th>%s</th>" % h for h in ("프로젝트", "상태", "진행률", "남은 시간(예상)", "실행 기기"))
    표 = ('<div class="progress"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (head, "".join(prows(r) for r in rows))
         if rows else '<p class="more">보는 프로젝트 없음 — %s 의 "projects"·"보기" 에 올리면 보인다</p>' % esc(LIST_FILE))
    progress = ('<section class="panel" aria-labelledby="h-proj"><div class="ph"><h2 id="h-proj">프로젝트 진행</h2>'
                '<span class="hint">줄을 누르면 그 프로젝트 보드</span></div>%s</section>\n' % 표)
    sub = " · ".join(["%s" % now.strftime("%Y-%m-%d %H:%M"), "프로젝트 %d" % len(rows)] + (["기기 %d" % 기계수] if 기계수 is not None else []))
    err = '<p class="why">%s</p>' % esc(오류) if 오류 else ""
    return HOME_TEMPLATE % {"title": esc(화면이름["home"]), "err": err, "sub": esc(sub), "numbers": numbers, "progress": progress, "turns": turnp, "feed": feed, "machines": machines}


def render_home_feed(줄, 못):
    """첫 화면 「최근 활동」 — 새것이 위인 앞 10줄과 「전체」 고리. 거르개 폼은 프로젝트 보드에만 있다."""
    esc = html.escape

    def li(x):
        색 = "teal" if x["결과"] in ("통과", "상태 → 완료") else "orange" if x["결과"] in ("반려", "멈춤") else "blue"
        글 = esc("%s · %s · %s" % (x["카드"].removesuffix(".md"), x["단계"], x["결과"]))
        if x.get("카드있음", True):                                # 카드 파일이 없는 줄(옛 작업 폴더 이름)은 눌러도 404 라 글만 둔다
            글 = '<a href="/p/%s/%s">%s</a>' % (urllib.parse.quote(x["프로젝트"], safe=""), urllib.parse.quote(x["카드"], safe=""), 글)
        return ('<li class="act"><span class="fd fd-%s"></span><div><div>%s</div><div class="tm">%s</div></div></li>'
                % (색, 글, esc("%s · %s · %s" % (x["프로젝트"], _기기_글(x["기계"]), _얼마_전(x["때"])))))
    top = sorted(줄, key=lambda x: x["때"], reverse=True)[:10]
    why = "".join('<p class="why">%s: 이 프로젝트 활동 못 읽음</p>' % esc(n) for n in 못)
    body = '<ol class="flist">%s</ol>' % "".join(li(x) for x in top) if top else '<p class="more">최근 활동 없음</p>'
    return ('<section class="panel feedp" aria-labelledby="h-act"><div class="ph"><h2 id="h-act">최근 활동</h2>'
            '<a href="/timeline">전체</a></div>%s%s</section>' % (why, body))


def _홈_피드(items, rows, qs):
    """첫 화면에 끼울 「최근 활동」. 어떤 예외가 나도 나머지 화면이 뜨도록 구역 안에서 막는다. 주소의 거르개는 그대로 걸린다."""
    g = lambda k: (qs.get(k) or [""])[0].strip()          # noqa: E731
    try:
        줄, 못 = collect_feed(items, rows, {"프로젝트": g("feed_project"), "기계": g("feed_machine")}, dt.datetime.now())
        return render_home_feed(줄, 못)
    except Exception:
        return ('<section class="panel feedp" aria-labelledby="h-act"><div class="ph"><h2 id="h-act">최근 활동</h2>'
                '<a href="/timeline">전체</a></div><p class="why">활동 못 읽음</p></section>')


def render_machine_pills(data):
    """첫 화면 기계 줄 — 기계마다 알약 하나(점·이름·한 줄 설명)와 판 다름·못 읽음 경고."""
    esc = html.escape
    pills = "".join(
        '<div class="pill"><span class="%s"></span><b>%s%s</b><span class="tm">카드 %d · %s · 올리지 않은 커밋 —</span></div>'
        % ("dot-blue" if x["도는 카드"] else "dot-gray", esc(_기기_글(x["기계"])), " · 이 기기" if x["이 기계"] else "", len(x["도는 카드"]),
           _판_글(x["판"]) if x["판"] else "판 모름") for x in data["줄"]) or '<p class="meta">알려진 기기 없음</p>'
    why = "".join('<p class="why">%s</p>' % esc(x) for x in (
        ["판 다름 — " + _판_다름_글(data["판 다름"])] if data["판 다름"] else [])
        + ["%s — 이력을 못 읽어 기기 현황에서 뺐다" % n for n, _ in data["못 읽음"]])
    return '<section class="panel mach" aria-labelledby="h-mach"><h2 id="h-mach">기기</h2>%s%s</section>' % (pills, why)


# ── 카드 상세 화면 (ISSUE-sc02-e5-card-detail-2026-11-20) ──
# 소각 커밋을 알아보는 규칙은 `bin/runcost.py` 의 `burn_commits`·`read_state` 와 같다 — 보드는 표준 라이브러리만
# 가져와야 해서(test_board_discipline D01) 같은 바탕의 git 읽기를 여기 작게 다시 쓴다. 바꿀 때 두 곳을 함께 고친다.
_구분 = "\x1f"
_이력_서식 = "\x1e" + _구분.join(
    ["%H", "%ad", "%s"] + ["%(trailers:key=Loop-" + k + ",valueonly)"
                           for k in ("Issue", "Card", "Stage", "Resumed", "Verdict", "Findings")])


def _걸린(ms):
    """밀리초를 사람 말로 — 3분 27초 · 1시간 5분 · 40초."""
    sec = int(ms // 1000)
    h, rest = divmod(sec, 3600)
    m, sec = divmod(rest, 60)
    return "%d시간 %d분" % (h, m) if h else "%d분 %d초" % (m, sec) if m else "%d초" % sec


def read_status_stages(폴더명, 카드):
    """`STATUS_DIR/<폴더명>__<카드>.json` 의 `stages`. (단계 사전, 오류 글) — 파일이 없으면 ({}, ""), 깨지면 ({}, 글)."""
    path = os.path.join(STATUS_DIR, "%s__%s.json" % (폴더명, 카드))
    if not os.path.isfile(path):
        return {}, ""
    try:
        with open(path, encoding="utf-8") as f:
            stages = json.load(f).get("stages") or {}
        return (stages, "") if isinstance(stages, dict) else ({}, "상태 파일을 읽지 못했다")
    except (OSError, ValueError, AttributeError):
        return {}, "상태 파일을 읽지 못했다"


def _git_읽기(project, *args):
    """프로젝트의 이력 읽기 git 한 번. 그림에 끝이 있고 거울이 있으면 거울에서 읽는다(설정을 읽는 `remote` 만 사본)."""
    대상 = project if args[:1] == ("remote",) else _읽을_저장소(project)
    return subprocess.run(["git", "-C", 대상] + list(args), capture_output=True, text=True, timeout=30)


def _전체_log(project, *뒤, 앞=()):
    """`git log --all` 에 원격 읽기가 받아 둔 원격 끝(`REMOTE[…]["끝"]`)에서 닿는 커밋을 더해 읽는다 — 같은 커밋은 한 번만 나온다.
    끝은 해시 인자로만 넘기고 참조를 쓰지 않아 작업 사본은 그대로다. 끝이 없거나 이 기계에 그 객체가 없으면 끝 없이 이 기계 참조만 읽는다."""
    끝 = (REMOTE.get(os.path.abspath(project)) or {}).get("끝")
    r = _git_읽기(project, *앞, "log", "--all", *([끝] if 끝 else []), *뒤)
    if r.returncode != 0 and 끝:
        r = _git_읽기(project, *앞, "log", "--all", *뒤)
    return r


def _소각_읽기(project, sha):
    """소각 커밋이 지운 `state.json` 의 metrics. (metrics, None) 또는 (None, 까닭). 예외를 내지 않는다."""
    r = _git_읽기(project, "-c", "core.quotepath=false", "diff-tree", "--no-commit-id", "-r", "--diff-filter=D", "--name-only", sha)
    paths = [x for x in r.stdout.splitlines() if re.fullmatch(r"\.workflow/[^/]+/state\.json", x)]
    if not paths:
        return None, "소각 커밋이 지운 파일에 state.json 없음"
    got = _git_읽기(project, "show", "%s^:%s" % (sha, paths[0]))
    if got.returncode != 0:
        return None, "부모 커밋에 %s 없음" % paths[0]
    try:
        return json.loads(got.stdout).get("metrics") or {}, None
    except (ValueError, AttributeError):
        return None, "state.json 을 읽지 못함(JSON 깨짐)"


TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")   # bin/runner.py 와 같은 이름


def _빈줄():
    return dict({"runs": 0, "ms": 0, "cost": 0.0, "model": "", "판정": "", "지적": ""}, **{k: 0 for k in TOKEN_KEYS})


def _더하기(rows, 단계, runs, ms, cost, model="", 토큰=None):
    row = rows.setdefault(단계, _빈줄())
    for k in TOKEN_KEYS:
        row[k] += _수((토큰 or {}).get(k))
    row["runs"] += runs
    row["ms"] += ms
    row["cost"] += cost
    row["model"] = row["model"] or model


def _수(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def read_card_history(project, 카드):
    """한 카드의 이력 — {"단계": {단계: {runs, ms, cost, model, 판정, 지적}}, "못찾음": [까닭], "이어받기": n,
    "커밋": [(때, 단계, 제목)], "오류": git 을 못 읽은 까닭, "상태오류": 상태 파일 오류}. 예외를 내지 않는다.

    단계 값은 상태 파일과 소각된 실행(`state.json` 의 metrics)을 같은 단계끼리 더한 것이다 — 보정하지 않는다.
    다만 상태 파일에 남은 끝난 실행의 요약과 값이 같은 소각 단계는 같은 실행이라 더하지 않는다."""
    stem = os.path.splitext(카드)[0]
    out = {"단계": {}, "못찾음": [], "이어받기": 0, "커밋": [], "오류": "", "상태오류": "",
           "판정수": {n: {"판정": 0, "반려": 0} for n in ("02", "06", "08", "09")},
           "PR": None, "병합": "", "이어받은 단계": [], "이어받은 실행": 0, "이어받은 실행 단계": []}
    앞이어받음, 대기, 시작때 = False, None, None      # 대기 = 이어받은 실행이 막 시작돼 아직 단계를 새로 돌리지 않은 때 [마지막으로 건너뛴 단계]
    stages, out["상태오류"] = read_status_stages(os.path.basename(os.path.abspath(project)), stem)
    for st, v in stages.items():
        if isinstance(v, dict):
            _더하기(out["단계"], str(st), _수(v.get("runs")), _수(v.get("ms")), _수(v.get("cost")), str(v.get("model") or ""), v)
    try:
        r = _전체_log(project, "--topo-order", "--reverse", "--date=format:%Y-%m-%d %H:%M",
                    "--format=" + _이력_서식)
        if r.returncode != 0:
            out["오류"] = "커밋 이력을 읽지 못했다"
            return out
        for rec in r.stdout.split("\x1e")[1:]:
            f = [x.strip() for x in rec.split(_구분)]
            if len(f) != 9:
                continue
            sha, 때, 제목, issue, card, num, resumed, verdict, findings = f
            누가 = _병합_누가(제목)               # 병합 커밋은 Loop 꼬리표가 없어 카드 이름으로 거르기 전에 본다
            if 누가 and 누가[1] == stem:
                out["병합"] = 누가[0]
                pr = re.match(r"Merge pull request #(\d+) ", 제목)
                out["PR"] = int(pr.group(1)) if pr else None
            if stem not in (issue, card, issue.removesuffix(".md"), card.removesuffix(".md")):
                continue
            if issue and num == "burn":
                metrics, why = _소각_읽기(project, sha)
                if metrics is None:
                    out["못찾음"].append("%s — %s" % (제목, why))
                else:
                    for st, v in metrics.items():
                        # 상태 파일은 끝난 실행의 요약을 소각 뒤에도 남기므로, 같은 값이면 같은 실행이다 — 한 번만 센다
                        if isinstance(v, dict) and stages.get(str(st)) != v:
                            _더하기(out["단계"], str(st), _수(v.get("runs")), _수(v.get("ms")), _수(v.get("cost")),
                                   str(v.get("model") or ""), v)
            elif num.isdigit() and issue:
                row = out["단계"].setdefault(num, _빈줄())
                if verdict:
                    row["판정"], row["지적"] = verdict, findings
                    if num in out["판정수"]:
                        out["판정수"][num]["판정"] += 1
                        out["판정수"][num]["반려"] += verdict == "반려"
            if resumed == "true":
                out["이어받기"] += 1
                if num:
                    out["이어받은 단계"].append(num)
                # 러너는 이어받을 때 산출물이 있는 단계마다 이 꼬리표 커밋을 남기므로(bin/runner.py 이어받음 빈 커밋) 한 실행이 여러 커밋이다 —
                # 연속한 묶음이나 01 에서 다시 시작한 것을 한 번으로 센다(`_이어받기_묶음_첫` 과 같은 규칙)
                if not 앞이어받음 or (num == "01" and 때 != 시작때):
                    out["이어받은 실행"] += 1
                    out["이어받은 실행 단계"] += 대기 or []         # 앞 실행이 새로 돈 단계 없이 끝났으면 마지막으로 건너뛴 단계
                    대기, 시작때 = [], 때
                if 대기 is not None and num:
                    대기[:] = [num]
            elif 대기 is not None and re.fullmatch(r"\d+[a-z]?", num):
                out["이어받은 실행 단계"].append(num)          # 묶음 뒤 처음 꼬리표 없이 실제로 돈 단계 — 이어받아 막혔던 자리
                대기 = None
            앞이어받음 = resumed == "true"
            out["커밋"].append((때, num or ("머리말" if card else "—"), 제목))
        out["이어받은 실행 단계"] += 대기 or []
    except Exception:                                # git 이 없거나 느려도 화면은 나머지를 그대로 낸다
        out["오류"] = "커밋 이력을 읽지 못했다"
    return out


# ── 카드 상세 — 단계 흐름·중앙값·판정과 지적 (ISSUE-ui-3-card-detail-2026-11-22) ──
# 단계 이름은 `bin/runner.py` 의 `STAGE_LABEL` 이름 부분과 같다. 보드는 러너를 가져오지 못해(test_board_discipline D01) 사본을 둔다 — 바꿀 때 두 곳을 함께 고친다.
STAGE_NAME = {"01": "분석", "02": "범위 검증", "03": "확인 요청", "03a": "답변", "07a": "결정", "04": "범위 통제", "05": "설계",
              "05u": "시안", "06": "설계 검토", "07": "구현", "08": "품질 확인", "09": "검토", "10": "인계", "11": "회고"}


def _원격_웹(project):
    """`origin` 이 깃허브면 `https://github.com/<주인>/<저장소>`, 아니면(없음·다른 곳·읽기 실패) 빈 글."""
    try:
        r = _git_읽기(project, "remote", "get-url", "origin")
    except (OSError, subprocess.SubprocessError):
        return ""
    if r.returncode != 0:
        return ""
    m = re.fullmatch(r"(?:git@github\.com:|https://github\.com/)([\w.-]+)/([\w.-]+?)(?:\.git)?", r.stdout.strip())
    return "https://github.com/%s/%s" % m.groups() if m else ""


def _소각_로그(project):
    """소각 커밋(`Loop-Stage: burn`)마다 `(sha, 카드 이름, [지운 .workflow 파일])` — git 한 번."""
    r = _전체_log(project, "--diff-filter=D", "--name-only",
                  "--format=\x1e%H" + _구분 + "%(trailers:key=Loop-Issue,valueonly)" + _구분 + "%(trailers:key=Loop-Stage,valueonly)" + _구분,
                  "--", ".workflow", 앞=("-c", "core.quotepath=false"))
    if r.returncode != 0:
        raise OSError("커밋 이력을 읽지 못했다")
    out = []
    for rec in r.stdout.split("\x1e")[1:]:
        f = rec.split(_구분)
        if len(f) == 4 and f[2].strip() == "burn" and f[1].strip():
            out.append((f[0].strip(), f[1].strip(), [x for x in f[3].splitlines() if x.strip()]))
    return out


def _묶음_읽기(project, 열쇠들):
    """`git cat-file --batch` 한 번으로 `<sha>^:<경로>` 들의 글 — {열쇠: 글 또는 None(없음)}."""
    if not 열쇠들:
        return {}
    r = subprocess.run(["git", "-C", _읽을_저장소(project), "cat-file", "--batch"], input="".join(x + "\n" for x in 열쇠들).encode("utf-8"),
                       capture_output=True, timeout=30)
    data, pos, out = r.stdout, 0, {}
    for k in 열쇠들:
        nl = data.index(b"\n", pos)
        head = data[pos:nl].split()
        pos = nl + 1
        if head[-1] == b"missing":
            out[k] = None
            continue
        size = int(head[2])
        out[k] = data[pos:pos + size].decode("utf-8", errors="replace")
        pos += size + 1
    return out


def read_size_medians(item, cards, card):
    """같은 크기·완료·자기 뺀 카드들의 단계별 걸린 시간 중앙값과 카드당 비용 합의 중앙값.
    `{"단계": {단계: ms}, "비용": float|None, "표본": 카드 수, "오류": 글}`. git 은 표본 수와 상관없이 둘(로그·묶음 읽기). 예외를 내지 않는다.

    표본 카드의 단계 값은 `read_card_history` 와 같은 규칙(상태 파일 + 소각 metrics, 같은 값이면 한 번)이다 — 바꿀 때 두 곳을 함께 고친다."""
    out = {"단계": {}, "비용": None, "표본": 0, "오류": ""}
    try:
        if card["크기"] not in ("S", "M", "L"):
            return out
        same = [c["file"] for c in cards if c["file"] != card["file"] and c["크기"] == card["크기"] and c["상태"] == "완료"]
        if not same:
            return out
        인자 = {"크기": card["크기"], "뺀 카드": card["file"], "표본": tuple(sorted(same))}
        return _잠깐저장("크기중앙값", [item], 인자, lambda: _중앙값_계산(item["path"], same, out))
    except Exception:
        out["오류"] = "같은 크기 중앙값을 읽지 못했다"
        return out


def _중앙값_계산(project, same, out):
    stems = {os.path.splitext(x)[0] for x in same}
    out["표본"] = len(same)
    burns = [x for x in _소각_로그(project) if x[1] in stems]
    paths = {sha: [p for p in names if re.fullmatch(r"\.workflow/[^/]+/state\.json", p)] for sha, _, names in burns}
    글 = _묶음_읽기(project, ["%s^:%s" % (sha, p) for sha, ps in paths.items() for p in ps])
    rows, 상태 = {}, {}
    for st in stems:
        stages, _ = read_status_stages(os.path.basename(os.path.abspath(project)), st)
        상태[st] = stages
        rows[st] = {}
        for k, v in stages.items():
            if isinstance(v, dict):
                _더하기(rows[st], str(k), _수(v.get("runs")), _수(v.get("ms")), _수(v.get("cost")), str(v.get("model") or ""), v)
    for sha, st, _ in burns:
        for p in paths[sha]:
            try:
                metrics = json.loads(글.get("%s^:%s" % (sha, p)) or "").get("metrics") or {}
            except (ValueError, AttributeError):
                continue
            for k, v in metrics.items():
                if isinstance(v, dict) and 상태[st].get(str(k)) != v:
                    _더하기(rows[st], str(k), _수(v.get("runs")), _수(v.get("ms")), _수(v.get("cost")), str(v.get("model") or ""), v)
    per = {}
    for st, r in rows.items():
        for k, v in r.items():
            if v["ms"] > 0:
                per.setdefault(k, []).append(v["ms"])
    out["단계"] = {k: int(statistics.median(v) + 0.5) for k, v in per.items()}
    costs = [sum(v["cost"] for v in r.values()) for r in rows.values()]
    costs = [c for c in costs if c > 0]
    out["비용"] = float(statistics.median(costs)) if costs else None
    return out


_판정_파일 = re.compile(r"\.workflow/[^/]+/(\d{2}[a-z]?)-verdict\.json(?:\.r(\d+))?")


def read_card_review(project, 카드):
    """그 카드의 소각 커밋 부모에 있던 판정 파일과 검토 문서 항목.
    `{"판정": [{"단계", "회차"(끝 파일은 None), "판정", "지적": [{"what", "where"}], "오류"}], "항목": [{"종류", "단계", "원문", "출처"}], "있음": 소각 커밋을 찾았는가}`.
    판정은 단계·회차 순. 예외를 내지 않는다."""
    out = {"판정": [], "항목": [], "있음": False}
    stem = os.path.splitext(카드)[0]
    try:
        burns = [x for x in _소각_로그(project) if x[1] == stem]
        out["있음"] = bool(burns)
        파일 = {}      # (sha, 경로) → 이름
        for sha, _, names in reversed(burns):
            for p in names:
                base = p.rsplit("/", 1)[-1]
                if _판정_파일.fullmatch(p) or base in _개선_절:
                    파일[(sha, p)] = base
        글 = _묶음_읽기(project, ["%s^:%s" % k for k in 파일])
        for (sha, p), base in 파일.items():
            text = 글.get("%s^:%s" % (sha, p))
            m = _판정_파일.fullmatch(p)
            if m:
                row = {"단계": m.group(1), "회차": int(m.group(2)) if m.group(2) else None, "판정": "", "지적": [], "오류": ""}
                try:
                    data = json.loads(text)
                    row["판정"] = str(data.get("verdict") or "")
                    row["지적"] = [{"what": str(x.get("what") or "—"), "where": str(x.get("where") or "—")}
                                  for x in data.get("findings") or [] if isinstance(x, dict)]
                except (ValueError, TypeError, AttributeError):
                    row["오류"] = "판정 파일을 읽지 못함"
                row["이름"] = base
                out["판정"].append(row)
            elif text is not None:
                for 제목, 종류, 단계 in _개선_절[base]:
                    if 종류 in ("선택 사항", "이연 후보", "사람 결정 미결"):
                        for 원문 in _절_항목(text, 제목) or []:
                            out["항목"].append({"종류": 종류, "단계": 단계 or "", "원문": 원문, "출처": base})
        out["판정"].sort(key=lambda r: (r["단계"], 10 ** 9 if r["회차"] is None else r["회차"]))
    except Exception:
        pass
    return out


# ── 비용·품질 화면 (ISSUE-sc02-e10-cost-quality-2026-11-20) ──
# 요청마다 git 과 파일을 읽는다 — 화면 단위 잠깐 저장(`_잠깐저장`)만 있다. 비용·토큰·판정 회차는 카드 상세와 같은 `read_card_history` 에서 온다.
_멈춤_까닭 = "같은 지적 재발"
_판정_단계 = ("02", "06", "08", "09")


def read_stops(project):
    """실행 폴더(`.workflow/<카드>-<날짜>/`)마다 git 에 마지막으로 쓰인 state.json 의 `stops` 에서 카드별 「같은 지적 재발」 수.
    ({카드: 수}, {못 읽은 카드: 까닭}, 오류 글). 폴더가 하나도 읽히지 않은 카드만 못 읽은 것이다. 예외를 내지 않는다."""
    try:
        r = _전체_log(project, "--diff-filter=AM", "--name-only", "--format=\x1e%H", "--",
                    ".workflow/*/state.json", 앞=("-c", "core.quotepath=false"))
        if r.returncode != 0:
            return {}, {}, "커밋 이력을 읽지 못했다"
        last = {}
        for rec in r.stdout.split("\x1e")[1:]:
            lines = [x for x in rec.splitlines() if x.strip()]
            for path in lines[1:]:
                last.setdefault(path, lines[0])               # 가장 새 커밋이 먼저 나온다
        counts, bad = {}, {}
        for path, sha in last.items():
            m = re.fullmatch(r"\.workflow/(.+)-\d{4}-\d{2}-\d{2}/state\.json", path)
            if not m:
                continue
            card, got = m.group(1), _git_읽기(project, "show", "%s:%s" % (sha, path))
            try:
                stops = json.loads(got.stdout).get("stops") if got.returncode == 0 else None
            except (ValueError, AttributeError):
                stops = None
            if isinstance(stops, list):
                counts[card] = counts.get(card, 0) + sum(1 for x in stops if isinstance(x, dict) and x.get("reason") == _멈춤_까닭)
                bad.pop(card, None)
            elif card not in counts:
                bad[card] = "state.json 의 stops 를 읽지 못함"
        return counts, bad, ""
    except Exception:
        return {}, {}, "커밋 이력을 읽지 못했다"


def read_merges(project):
    """병합 커밋 제목에서 카드별 「러너」(`[loop] <카드> — 자동 병합`) 또는 「사람」. 둘 다 있으면 러너. ({카드: 누가}, 오류 글)."""
    try:
        r = _전체_log(project, "--merges", "--format=%s")
        if r.returncode != 0:
            return {}, "커밋 이력을 읽지 못했다"
    except Exception:
        return {}, "커밋 이력을 읽지 못했다"
    out = {}
    for 제목 in r.stdout.splitlines():
        누가 = _병합_누가(제목)
        if 누가 and 누가[0] == "러너":
            out[누가[1]] = "러너"
        elif 누가:
            out.setdefault(누가[1], "사람")
    return out, ""


def _합():
    return {"단계": {}, "비용": 0.0, "토큰": {k: 0 for k in TOKEN_KEYS}, "판정": {n: [0, 0] for n in _판정_단계},
            "멈춤": 0, "병합": [0, 0], "기록 없음": {"비용": 0, "판정": 0, "멈춤": 0, "병합": 0}, "카드": {}}


def _합에_더하기(합, stem, 정보):
    def 올림(이름):
        합["카드"].setdefault(이름, []).append(stem)
    for st, row in 정보["단계"].items():
        합["단계"].setdefault(st, {"cost": 0.0, "토큰": {k: 0 for k in TOKEN_KEYS}})
        합["단계"][st]["cost"] += row["cost"]
        합["비용"] += row["cost"]
        for k in TOKEN_KEYS:
            합["단계"][st]["토큰"][k] += row[k]
            합["토큰"][k] += row[k]
        올림("cost:" + st)
    if any(r["cost"] > 0 for r in 정보["단계"].values()):
        올림("cost")
    for n, (판정, 반려) in 정보["판정"].items():
        합["판정"][n][0] += 반려
        합["판정"][n][1] += 판정
        if 판정:
            올림("verdict:" + n)
        if 반려:
            올림("rej:" + n)
    if 정보["멈춤"]:
        합["멈춤"] += 정보["멈춤"]
        올림("stop")
    if 정보["병합"]:
        합["병합"][1] += 1
        합["병합"][0] += 정보["병합"] == "러너"
        올림("merged")
        if 정보["병합"] == "러너":
            올림("auto")
    for 신호, 이름 in (("비용", "cost"), ("판정", "verdict"), ("멈춤", "stop"), ("병합", "merge")):
        if 신호 in 정보["없음"]:
            합["기록 없음"][신호] += 1
            올림("none:" + 이름)


def read_signals(items, qs):
    """(결과, 까닭). 결과는 프로젝트마다 {"name", "오류", "제목": {카드: (파일, 제목)}, "묶음": {"전체"·시나리오: 합}}.
    거르개 p·sc·from·to 에 맞는 **시작한 적이 있는 카드**(머리말 시작 또는 단계 커밋)만 센다. 기간은 카드 갱신 날짜 기준."""
    cond, 까닭 = _조건(qs)
    if 까닭:
        return [], 까닭
    only = (qs.get("p") or [""])[0].strip()
    out = []
    for item in items:
        if only and item["name"] != only:
            continue
        r = {"name": item["name"], "오류": [], "제목": {}, "묶음": {}, "카드정보": {}, "앞비용": 0.0}
        out.append(r)
        backlog = os.path.join(item["path"], "docs", "backlog")
        try:
            if not os.path.isdir(backlog):
                raise OSError("작업목록 폴더를 읽지 못했다: %s" % backlog)
            cards = merge_cards(read_cards(backlog), REMOTE.get(os.path.abspath(item["path"])), item.get("기기", ""))
        except Exception as e:
            r["오류"].append(str(e))
            continue
        def 시작한(조건):
            got = []
            for c in cards:
                if not _카드_쓸것(c, 조건):
                    continue
                h = read_card_history(item["path"], c["file"])
                if (c.get("시작") or "").strip() or any(x[1].isdigit() for x in h["커밋"]):
                    got.append((c, h))
            return got
        started = 시작한(cond)
        앞시작, 앞끝 = _앞기간(cond["시작"], cond["끝"])
        if 앞시작:
            r["앞비용"] = sum(row["cost"] for _, h in 시작한(dict(cond, 시작=앞시작, 끝=앞끝)) for row in h["단계"].values())
        if not started:
            continue
        counts, bad, 멈춤오류 = read_stops(item["path"])
        merges, 병합오류 = read_merges(item["path"])
        r["오류"] += [x for x in (멈춤오류, 병합오류) if x]
        for c, h in started:
            stem = os.path.splitext(c["file"])[0]
            r["제목"][stem] = (c["file"], c["title"])
            단계 = {st: row for st, row in h["단계"].items() if row["runs"] or row["cost"] or any(row[k] for k in TOKEN_KEYS)}
            없음 = set()
            if not 단계:
                없음.add("비용")
            if h["오류"]:
                없음.add("판정")
            if 멈춤오류 or stem not in counts:
                없음.add("멈춤")
            if 병합오류:
                없음.add("병합")
            정보 = {"단계": 단계, "판정": {n: (v["판정"], v["반려"]) for n, v in h["판정수"].items()},
                    "멈춤": counts.get(stem, 0), "병합": merges.get(stem, ""), "없음": 없음}
            r["카드정보"][stem] = {"비용": sum(row["cost"] for row in 단계.values()), "크기": c.get("크기", ""), "갱신": c["갱신"],
                               "반려": any(v["반려"] for v in h["판정수"].values()),
                               "판정": any(v["판정"] for v in h["판정수"].values()), "병합": merges.get(stem, "")}
            for 이름 in ("전체", c.get("시나리오") or "시나리오 없음"):
                _합에_더하기(r["묶음"].setdefault(이름, _합()), stem, 정보)
    return out, ""


_신호_기간_이름 = {"week": "이번 주", "last": "지난주", "30": "30일"}


def _앞기간(시작, 끝):
    """보는 기간 바로 앞의 같은 길이 구간 `(시작, 끝)`. 둘 다 있고 올바른 날짜일 때만, 아니면 ("", "")."""
    if not (시작 and 끝 and _날짜(시작) and _날짜(끝)):
        return "", ""
    a, z = (dt.datetime.strptime(x, "%Y-%m-%d").date() for x in (시작, 끝))
    n = (z - a).days + 1
    return (a - dt.timedelta(days=n)).isoformat(), (a - dt.timedelta(days=1)).isoformat()


def signals_기간(period, 시작, 끝, today):
    """`(시작, 끝, 앞시작, 앞끝, 까닭)` 날짜 글. 우선순위: period > from·to(맞춤 기간, 빈 쪽은 열린 채) > 이번 주. 모르는 period 는 이번 주 + 까닭."""
    period, 시작, 끝 = period.strip(), 시작.strip(), 끝.strip()
    if not period and (시작 or 끝):
        return (시작, 끝) + _앞기간(시작, 끝) + ("",)
    까닭 = ""
    if period not in _신호_기간_이름 and period:
        까닭, period = "기간은 이번 주·지난주·30일", "week"
    월 = today - dt.timedelta(days=today.weekday())
    a, z = {"last": (월 - dt.timedelta(days=7), 월 - dt.timedelta(days=1)), "30": (today - dt.timedelta(days=29), today)}.get(
        period, (월, 월 + dt.timedelta(days=6)))
    a, z = a.isoformat(), z.isoformat()
    return (a, z) + _앞기간(a, z) + (까닭,)


def _돈(x):
    return "$%.2f" % x


def _비율(수, 모):
    return ("%d%%" % round(수 * 100 / 모)) if 모 else "—"


def _중앙(infos):
    기록 = [i["비용"] for i in infos if i["비용"] > 0]       # 비용 기록 없는 카드(비용 0)는 센 수에서 뺀다
    return _돈(statistics.median(기록)) if 기록 else "—"


def signals_지표(infos, 앞비용):
    """지표 네 칸의 글. 분모·표본이 0 이면 「—」."""
    합 = sum(i["비용"] for i in infos)
    판정 = [i for i in infos if i["판정"]]
    통과 = [i for i in 판정 if not i["반려"]]
    병합 = [i for i in infos if i["병합"]]
    러너 = [i for i in 병합 if i["병합"] == "러너"]
    차이 = "—" if 앞비용 <= 0 else ("+" if 합 >= 앞비용 else "-") + _돈(abs(합 - 앞비용))
    return {"비용": _돈(합), "차이": 차이, "중앙": _중앙(infos),
            "S": _중앙([i for i in infos if i["크기"] == "S"]), "M": _중앙([i for i in infos if i["크기"] == "M"]),
            "통과": _비율(len(통과), len(판정)), "통과수": "%d장 중 %d장" % (len(판정), len(통과)) if 판정 else "—",
            "자동": _비율(len(러너), len(병합)), "자동수": "%d장 중 %d장" % (len(병합), len(러너)) if 병합 else "—"}


def signals_일별(결과, 시작, 끝):
    """날짜마다(카드 없는 날도) `{"날짜", "합", "구간": {프로젝트: 비용}, "높이": 0~1}`. 비용은 카드 갱신 날짜에 통째로 넣는다. 시작이 비면 가장 이른 갱신, 끝이 비면 오늘."""
    카드 = [(r["name"], i) for r in 결과 for i in r["카드정보"].values()]
    시작 = 시작 or min([i["갱신"] for _, i in 카드] or [dt.date.today().isoformat()])
    끝 = 끝 or dt.date.today().isoformat()
    a, z = (dt.datetime.strptime(x, "%Y-%m-%d").date() for x in (시작, 끝))
    일 = [{"날짜": (a + dt.timedelta(days=n)).isoformat(), "합": 0.0, "구간": {}, "높이": 0} for n in range((z - a).days + 1)]
    by = {d["날짜"]: d for d in 일}
    for 이름, i in 카드:
        d = by.get(i["갱신"])
        if d:
            d["합"] += i["비용"]
            d["구간"][이름] = d["구간"].get(이름, 0.0) + i["비용"]
    최대 = max([d["합"] for d in 일] or [0])
    for d in 일:
        d["높이"] = d["합"] / 최대 if 최대 > 0 else 0
    return 일


def signals_반려율(결과):
    """판정 단계마다 `(단계, 퍼센트 글, 막대 폭)`. 판정 회차가 0 이면 (「—」, 0)."""
    out = []
    for n in _판정_단계:
        반려, 판정 = (sum((r["묶음"].get("전체", {}).get("판정", {}).get(n) or [0, 0])[k] for r in 결과) for k in (0, 1))
        out.append((n, _비율(반려, 판정), 반려 * 100 / 판정 if 판정 else 0))
    return out


def signals_프로젝트표(결과):
    """프로젝트마다 한 행. 카드가 없으면 숫자 칸은 「—」. `비싼` = (파일, 제목, 비용 글) 또는 None."""
    rows = []
    for r in 결과:
        infos = r["카드정보"]
        if not infos:
            rows.append({"이름": r["name"], "카드": "—", "비용": "—", "중앙": "—", "자동": "—", "비싼": None})
            continue
        지표 = signals_지표(list(infos.values()), 0)
        stem = max(infos, key=lambda k: infos[k]["비용"])
        rows.append({"이름": r["name"], "카드": str(len(infos)), "비용": 지표["비용"], "중앙": 지표["중앙"], "자동": 지표["자동"],
                     "비싼": r["제목"][stem] + (_돈(infos[stem]["비용"]),)})
    return rows


_숫자이름 = re.compile(r"cost|stop|merged|auto|cost:[0-9][0-9a-z]*|(?:verdict|rej):\d+|none:(?:cost|verdict|stop|merge)")


_신호_CSS = """main { max-width:1280px; }
.sg-top { display:flex; flex-wrap:wrap; gap:12px; align-items:center; margin:0 0 16px; }
.sg-top h1 { margin:0; font-size:20px; font-weight:700; color:var(--fg); }
.sg-pg { display:flex; gap:2px; padding:2px; border-radius:8px; background:var(--soft); margin-left:auto; }
.sg-pg a { display:inline-flex; align-items:center; min-height:30px; padding:0 12px; border-radius:6px; font-size:12px; color:var(--dim); }
.sg-pg a[aria-pressed="true"] { background:var(--panel); color:var(--fg); font-weight:600; }
.sg-hint { font-size:12px; color:var(--dim); }
.sg-k { margin:0 0 16px; }
@media (max-width:640px) { .sg-k { grid-template-columns:repeat(2, minmax(0, 1fr)); } }
.sg-row { display:flex; flex-wrap:wrap; gap:16px; align-items:stretch; margin:0 0 16px; }
.sg-row .panel { margin:0; }
.sg-day { flex:3 1 520px; min-width:0; }
.sg-rej { flex:2 1 340px; min-width:0; }
.sg-in { padding:16px 18px; display:flex; flex-direction:column; gap:12px; }
.sg-lg { display:inline-flex; flex-wrap:wrap; gap:12px; font-size:12px; color:var(--ink2); }
.sg-lg i { display:inline-block; width:10px; height:10px; margin-right:4px; border-radius:2px; }
.sg-bars { display:flex; align-items:flex-end; gap:6px; height:200px; padding:0 4px; border-bottom:1px solid var(--line); }
.sg-col { flex:1; min-width:0; height:100%; display:flex; flex-direction:column; align-items:center; justify-content:flex-end; gap:4px; overflow:hidden; }
.sg-dt { max-width:100%; font-size:11px; color:var(--dim); white-space:nowrap; overflow:hidden; }
.sg-stack { width:100%; max-width:44px; display:flex; flex-direction:column; justify-content:flex-end; }
.sg-c0 { background:var(--pj-0); } .sg-c1 { background:var(--pj-1); } .sg-c2 { background:var(--pj-2); } .sg-c3 { background:var(--pj-3); }
.sg-lbls { display:flex; gap:6px; padding:0 4px; font-size:11px; color:var(--dim); }
.sg-lbls span { flex:1; min-width:0; text-align:center; overflow:hidden; white-space:nowrap; }
.sg-rr { display:grid; grid-template-columns:minmax(0, 90px) minmax(0, 1fr) 48px; gap:10px; align-items:center; font-size:13px; }
.sg-track { height:8px; border-radius:4px; background:var(--soft); overflow:hidden; }
.sg-fill { height:8px; background:var(--blue); } .sg-fill.hot { background:var(--orange); }
.sg-pct { text-align:right; font:12px var(--mono); }
.sg-wrap { overflow-x:auto; }
.sg-wrap h2 { margin:0; padding:14px 18px; font-size:15px; font-weight:600; border-bottom:1px solid var(--line2); }
.sg-t { width:100%; min-width:760px; border-collapse:collapse; font-size:13px; }
.sg-t th { padding:10px 12px; font-weight:500; font-size:12px; color:var(--dim); text-align:left; border:0; }
.sg-t td { padding:12px; border:0; border-top:1px solid var(--line2); }
.sg-t th:first-child, .sg-t td:first-child { padding-left:18px; font-weight:600; }
.sg-empty { margin:0 0 16px; font-size:13px; color:var(--dim); }
details.sg-old { margin:16px 0; }
details.sg-old > summary { cursor:pointer; font-size:13px; color:var(--dim); }
"""


def render_signals(qs, 결과, 까닭):
    """비용·품질 화면 HTML. 값은 모두 escape 한다. 맨 위는 목업 구성(기간 단추 → 지표 네 칸 → 일별 막대·단계별 반려율 → 프로젝트별 표),
    옛 단계별 토큰 표·숫자 고리·거르개 폼은 `<details>` 한 칸에 접어 둔다. 숫자는 고리이고 누르면 `show=<숫자 이름>` 으로 카드 목록이 아래에 나온다."""
    esc = html.escape

    def v(k):
        return (qs.get(k) or [""])[0]
    시작, 끝, _, _, 기간까닭 = signals_기간(v("period"), v("from"), v("to"), dt.date.today())
    눌림 = v("period") if v("period") in _신호_기간_이름 else ("week" if v("period") or not (v("from") or v("to")) else "")
    거른 = {k: v(k) for k in ("p", "sc") if v(k)}
    단추 = "".join('<a href="/signals?%s" aria-pressed="%s">%s</a>' % (
        esc(urllib.parse.urlencode(dict(거른, period=k))), "true" if k == 눌림 else "false", 글) for k, 글 in _신호_기간_이름.items())
    parts = ['<div class="sg-top"><h1>%s</h1><div class="sg-pg" role="group" aria-label="기간">%s</div>'
             '<span class="sg-hint">기간을 바꾸면 아래 숫자가 모두 바뀐다</span></div>' % (esc(화면이름["usage"]), 단추)]
    if 까닭 or 기간까닭:
        parts.append("<p>%s</p>" % esc(까닭 or 기간까닭))
    elif not 결과:
        parts.append("<p>보는 프로젝트 없음</p>")
    if not 까닭 and 결과:
        infos = [i for r in 결과 for i in r["카드정보"].values()]
        m = signals_지표(infos, sum(r["앞비용"] for r in 결과))
        타일 = [("비용", m["비용"], "앞 기간보다 " + m["차이"]), ("카드당 중앙값", m["중앙"], "S %s · M %s" % (m["S"], m["M"])),
              ("반려 없이 통과", m["통과"], m["통과수"]), ("자동 병합", m["자동"], m["자동수"])]
        parts.append('<section aria-label="요약" class="tiles sg-k">%s</section>' % "".join(
            '<div class="tile"><div class="lb">%s</div><div class="v">%s</div><div class="sub">%s</div></div>' % tuple(esc(x) for x in t)
            for t in 타일))
        if not infos:
            parts.append('<p class="sg-empty">이 기간에 시작한 카드가 없다</p>')
        일 = signals_일별(결과, 시작, 끝)
        이름들 = [r["name"] for r in 결과]
        색 = lambda 이름: "sg-c%d" % (이름들.index(이름) % 4)       # noqa: E731
        막대 = "".join(
            '<div class="sg-col"><span class="sg-dt">%s</span><div class="sg-stack" style="height:%.1f%%">%s</div></div>' % (
                _돈(d["합"]) if d["합"] else "", d["높이"] * 85,
                "".join('<div class="%s" style="height:%.1f%%"></div>' % (색(n), x * 100 / d["합"]) for n, x in d["구간"].items() if d["합"]))
            for d in 일)
        글 = ", ".join("%s %s" % (d["날짜"][5:], _돈(d["합"])) for d in 일 if d["합"]) or "비용 없음"
        parts.append(
            '<div class="sg-row"><section aria-labelledby="h-day" class="panel sg-day"><div class="ph"><h2 id="h-day">일별 비용</h2>'
            '<span class="sg-lg">%s</span></div><div class="sg-in"><div class="sg-bars" role="img" aria-label="%s">%s</div>'
            '<div class="sg-lbls">%s</div></div></section>'
            % ("".join('<span><i class="%s"></i>%s</span>' % (색(n), esc(n)) for n in 이름들),
               esc("일별 비용 막대 — " + 글, quote=True), 막대, "".join("<span>%s</span>" % d["날짜"][5:] for d in 일)))
        parts.append(
            '<section aria-labelledby="h-rej" class="panel sg-rej"><div class="ph"><h2 id="h-rej">단계별 반려율</h2></div><div class="sg-in">%s</div></section></div>'
            % "".join('<div class="sg-rr"><span>%s</span><div class="sg-track"><div class="sg-fill%s" style="width:%.1f%%"></div></div>'
                      '<span class="sg-pct">%s</span></div>' % (n, " hot" if 폭 >= 25 else "", 폭, 퍼센트)
                      for n, 퍼센트, 폭 in signals_반려율(결과)))
        행 = []
        for row, r in zip(signals_프로젝트표(결과), 결과):
            비싼 = ('<a href="/p/%s/%s">%s %s</a>' % (urllib.parse.quote(r["name"], safe=""), urllib.parse.quote(row["비싼"][0], safe=""),
                                                    esc(row["비싼"][1]), esc(row["비싼"][2])) if row["비싼"] else "—")
            행.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                esc(row["이름"]), row["카드"], row["비용"], row["중앙"], row["자동"], 비싼))
            행 += ['<tr><td colspan="6">읽지 못함 — %s</td></tr>' % esc(x) for x in r["오류"]]
        parts.append('<section aria-labelledby="h-proj" class="panel sg-wrap"><h2 id="h-proj">프로젝트별</h2><table class="sg-t"><thead><tr>%s</tr></thead>'
                     "<tbody>%s</tbody></table></section>" % (
                         "".join("<th>%s</th>" % x for x in ("프로젝트", "카드", "비용", "카드당 중앙값", "자동 병합", "비싼 카드")), "".join(행)))

    box = lambda k, label, typ="text": '<label>%s <input type="%s" name="%s" value="%s"></label> ' % (
        label, typ, k, esc(v(k), quote=True))
    parts.append('<details class="sg-old"><summary>필터와 단계별 토큰 표</summary>'
                 '<form method="get" action="/signals"><p>%s%s%s%s<button type="submit">보기</button></p></form>'
                 "<p>기간은 카드의 갱신 날짜 기준이다. 시작한 적이 있는 카드만 센다.</p>"
                 % (box("p", "프로젝트"), box("sc", "시나리오"), box("from", "기간 시작", "date"), box("to", "끝", "date")))

    def 고리(프로젝트, 묶음, show, 글):
        q = {k: v(k) for k in ("sc", "from", "to", "period") if v(k)}
        q.update(p=프로젝트, b=묶음, show=show)
        return '<a href="/signals?%s">%s</a>' % (esc(urllib.parse.urlencode(q)), esc(str(글)))

    def 표(head, rows):
        return ("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
                % ("".join("<th>%s</th>" % esc(h) for h in head),
                   "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in r) for r in rows)))
    for r in 결과:
        P = r["name"]
        parts.append("<h2>%s</h2>" % esc(P))
        parts += ["<p>읽지 못함 — %s</p>" % esc(x) for x in r["오류"]]
        if not r["묶음"]:
            parts.append("<p>기록 없음</p>")
        for 이름, 합 in r["묶음"].items():
            parts.append("<h4>%s</h4>" % esc(이름))
            rows = [[esc(st)] + [고리(P, 이름, "cost:" + st, x) for x in
                                ["%.2f" % d["cost"]] + ["{:,}".format(d["토큰"][k]) for k in TOKEN_KEYS]]
                    for st, d in sorted(합["단계"].items())]
            rows.append(["합계"] + [고리(P, 이름, "cost", x) for x in
                                  ["%.2f" % 합["비용"]] + ["{:,}".format(합["토큰"][k]) for k in TOKEN_KEYS]])
            parts.append(표(["단계", "비용", "입력", "캐시 만들기", "캐시 읽기", "출력"], rows))
            q = [[n, (고리(P, 이름, "rej:" + n, 합["판정"][n][0]) + "/" + 고리(P, 이름, "verdict:" + n, 합["판정"][n][1]))
                  if 합["판정"][n][1] else "없음"] for n in _판정_단계]
            q.append(["멈춤", 고리(P, 이름, "stop", 합["멈춤"])])
            q.append(["러너병합", (고리(P, 이름, "auto", 합["병합"][0]) + "/" + 고리(P, 이름, "merged", 합["병합"][1]))
                      if 합["병합"][1] else "없음"])
            q += [["기록 없음 · " + 글, 고리(P, 이름, "none:" + 키, 합["기록 없음"][신호])]
                  for 신호, 키, 글 in (("비용", "cost", "비용"), ("판정", "verdict", "판정"),
                                      ("멈춤", "stop", "멈춤"), ("병합", "merge", "병합"))]
            parts.append(표(["신호", "값"], q))
    parts.append("</details>")
    show = v("show")
    if _숫자이름.fullmatch(show):
        묶음 = v("b") or "전체"
        for r in 결과:
            names = (r["묶음"].get(묶음) or {}).get("카드", {}).get(show, [])
            parts.append("<h2>%s</h2>" % esc(r["name"]))
            if not names:
                parts.append("<p>해당 카드 없음</p>")
            for stem in names:
                file, title = r["제목"][stem]
                parts.append('<h3><a href="/p/%s/%s">%s</a></h3>' % (urllib.parse.quote(r["name"], safe=""),
                                                                    urllib.parse.quote(file, safe=""), esc(title)))
    틀 = FILE_TEMPLATE.replace('<a href="board.html">← 보드</a>', '<a href="/">← %s</a>' % 화면이름["home"]).replace("<h1>%(name)s</h1>\n", "", 1)
    return (틀 % {"name": 화면이름["usage"], "body": "\n".join(parts)}).replace("</style>", _신호_CSS + "</style>", 1)


# ── 워크플로우 회고 화면 (ISSUE-ui-17-workflow-retro-2026-11-24) ──
# 단계·게이트·에이전트·러너가 어디서 사람 손과 시간과 돈을 새게 하나를 본다. 읽기만 한다 — 카드 만들기는 기존 `POST /p/<이름>/make-card` 길이다.
# 여섯 칸이 모두 같은 카드 묶음(시작한 적이 있는 카드, 기간은 머리말 `갱신`)에서 계산한다. 칸 5 만 변경 표 줄의 날짜로 건다.
# 멈춘 종류 이름·처방은 `bin/runner.py` 의 `STOP_KIND_RULES`·`stop_kind` 종류 이름의 사본이다 — 보드는 러너를 가져오지 못해(test_board_discipline D01) 바꿀 때 두 곳을 함께 고친다.
_멈춤_처방 = {"일시적 실패": "다시 시도로 풀릴 멈춤", "범위 밖 파일 변경": "규칙을 바꿀 멈춤", "산출물 형식 오류": "규칙을 바꿀 멈춤",
          "사람 결정": "규칙을 바꿀 멈춤", "기준 가지 실패": "규칙을 바꿀 멈춤", "기타": "규칙을 바꿀 멈춤"}
# 자동 병합을 막은 까닭의 머리 — `bin/runner.py` 의 `merge_reasons`·`HUMAN_REASON`·`IRREVERSIBLE_HEAD` 글의 사본이다. 바꿀 때 두 곳을 함께 고친다.
_막은_까닭_머리 = ("보호 경로를 건드렸다", "반려가 있었다", "크기가 S 가 아니다", "이어받은 실행이다", "사람 결정 미결이 있다", "되돌리기 어려운 변경")
_회고_절 = (("3", "3. 되풀이 신호", "되풀이 신호"), ("4", "4. 규칙 후보", "규칙 후보"),
          ("7", "7. 지표 제안", "지표 제안"), ("8", "8. 워크플로우 몫", "워크플로우 몫"))
_오래머묾_날 = 14           # 채택 뒤 이 날수를 넘겨 반영되지 않으면 경고 — 근거 없이 정한 값이라 한 곳에 둔다(03 답 Q7)
_아는_지표 = {"첫 통과율": "첫 통과율", "자동 병합": "자동 병합", "멈춘 종류별 건수": "멈춘 종류", "회고 제안 연결 비율": "회고 제안", "사람 개입": "사람 개입"}
_변경표_경로 = os.path.join("workflows", "metrics", "워크플로우-변경.md")


def read_stop_kinds(project):
    """카드 파일 이력에서 러너가 쓴 `멈춘종류:` 값 — `({카드: {종류: 수}}, 오류 글)`. 빈 값(지움)은 세지 않고, 같은 카드·같은 작성 시각·같은 값은 한 번만 센다."""
    try:
        r = _전체_log(project, "--no-renames", "-p", "-U0", "--format=\x1e%at", "--", "docs/backlog", 앞=("-c", "core.quotepath=false"))
        if r.returncode != 0:
            return {}, "커밋 이력을 읽지 못했다"
        본 = set()
        out = {}
        for rec in r.stdout.split("\x1e")[1:]:
            때, _, 몸 = rec.partition("\n")
            카드 = None
            for line in 몸.splitlines():
                m = re.match(r"diff --git a/docs/backlog/(.+)\.md b/", line)
                if m:
                    카드 = m.group(1)
                elif 카드 and line.startswith("+멈춘종류:"):
                    값 = line[len("+멈춘종류:"):].strip()
                    if 값 and (카드, 때.strip(), 값) not in 본:
                        본.add((카드, 때.strip(), 값))
                        out.setdefault(카드, {})[값] = out.get(카드, {}).get(값, 0) + 1
        return out, ""
    except (OSError, subprocess.SubprocessError):
        return {}, "커밋 이력을 읽지 못했다"


def _막은_까닭(글):
    """`10-handoff.md` 의 `## 병합 판단` 절에서 자동 병합을 막은 까닭의 머리 목록. 절이 없으면 None(기록 없음), 까닭이 없으면 빈 목록."""
    항목 = _절_항목(글 or "", "병합 판단")
    if 항목 is None:
        return None
    머리들 = (t.split(":", 1)[0].strip() for t in 항목)
    return [x for x in 머리들 if x.startswith(_막은_까닭_머리)]


def _회고_제안_읽기(글):
    """회고 글의 3·4·7·8절 제안 줄 — `[(절 번호, 원문, 열쇠 또는 None)]`. 열쇠는 표 바로 아래 `열쇠: R-1 · R-2` 한 줄이고, 줄 수와 다르면 그 절의 열쇠는 모른다."""
    줄들 = 글.splitlines()
    out = []
    for 번호, 제목, _ in _회고_절:
        글들 = _절_항목(글, 제목)
        if not 글들:
            continue
        열쇠 = None
        시작 = next(i for i, l in enumerate(줄들) if l.strip() == "## " + 제목)
        for l in 줄들[시작 + 1:]:
            if re.match(r"#{1,6} ", l):
                break
            if l.strip().startswith("열쇠:"):
                열쇠 = re.findall(r"R-\d+", l)
        if 열쇠 is not None and len(열쇠) != len(글들):
            열쇠 = None
        out += [(번호, t, 열쇠[i] if 열쇠 else None) for i, t in enumerate(글들)]
    return out


def _제안_묶기(회고들):
    """`[(회고한 카드, 날짜, 제안 줄들)]` → 같은 원문끼리 묶은 `[{원문, 절, 근거수(서로 다른 회고 카드 수), 새로나옴(가장 이른 날짜), 출처: [(카드, 열쇠)]}]`."""
    묶음 = {}
    for 카드, 날짜, 줄들 in 회고들:
        for 절, 원문, 열쇠 in 줄들:
            z = 묶음.setdefault(원문, {"원문": 원문, "절": 절, "근거수": 0, "새로나옴": 날짜, "출처": [], "카드들": set()})
            z["카드들"].add(카드)
            z["새로나옴"] = min(z["새로나옴"], 날짜)
            if (카드, 열쇠) not in z["출처"]:
                z["출처"].append((카드, 열쇠))
    for z in 묶음.values():
        z["근거수"] = len(z.pop("카드들"))
    return list(묶음.values())


def _제안_진행(제안들, 카드들, 변경, 오늘):
    """제안 줄마다 채택(그 제안을 머리말 `회고제안:` 으로 가리킨 카드)·반영(그 카드 완료)·효과 확인(변경 표 줄이 그 카드를 가리키고 판정이 「—」이 아님)·경고(채택 뒤 오래 반영 안 됨)."""
    채택표 = {}
    for c in 카드들:
        m = re.fullmatch(r"(\S+)\s+(R-\d+)", (c.get("회고제안") or "").strip())
        if m:
            채택표.setdefault((m.group(1), m.group(2)), c)
    out = []
    for z in 제안들:
        c = next((채택표[s] for s in z["출처"] if s in 채택표), None)
        stem = c["file"].removesuffix(".md") if c else None
        반영 = bool(c) and c["상태"] == "완료"
        효과 = bool(c) and any(stem in x["변경"] and x["판정"] != "—" for x in 변경)
        경고 = False
        if c and not 반영:
            m = re.search(r"(\d{4}-\d{2}-\d{2})$", stem)
            날 = m.group(1) if m else c.get("갱신", "")
            경고 = _날짜(날) and (오늘 - dt.datetime.strptime(날, "%Y-%m-%d").date()).days > _오래머묾_날
        out.append(dict(z, 채택=stem, 반영=반영, 효과=효과, 경고=bool(경고)))
    return out


def _변경표_읽기(글):
    """변경 표의 줄 — `([{날짜, 변경, 지표, 기대, 아는지표}], 건너뛴 줄 수)`. 네 칸이 아닌 줄은 건너뛴다."""
    줄들, 건너뜀 = [], 0
    for l in 글.splitlines():
        if not l.strip().startswith("|") or _표_줄.match(l.strip()):
            continue
        칸 = [c.strip() for c in l.strip().strip("|").split("|")]
        if 칸[0] == "날짜":
            continue
        if len(칸) != 4:
            건너뜀 += 1
            continue
        아는 = [이름 for 조각 in 칸[2].split(" · ") for 이름, 낱말 in _아는_지표.items() if 낱말 in 조각]
        줄들.append({"날짜": 칸[0], "변경": 칸[1], "지표": 칸[2], "기대": 칸[3], "아는지표": 아는})
    return 줄들, 건너뜀


def _수치(글):
    m = re.search(r"-?\d+(?:\.\d+)?", str(글))
    return float(m.group()) if m else None


def _판정(전, 뒤, 기대):
    """기대 방향과 전·뒤 값만 본다(문턱 없음) — 기대대로·반대로·변화 없음, 계산할 수 없으면 「—」."""
    a, c = _수치(전), _수치(뒤)
    낮을수록 = True if "줄" in 기대 else False if ("늘" in 기대 or "오르" in 기대) else None
    if a is None or c is None or 낮을수록 is None:
        return "—"
    if a == c:
        return "변화 없음"
    return "기대대로" if (c < a) == 낮을수록 else "반대로"


def _병합_찾기(project):
    """병합 커밋 제목에서 `{카드: (누가, 병합 커밋)}` — 러너가 있으면 러너. 못 읽으면 OSError."""
    r = _전체_log(project, "--merges", "--format=%H" + _구분 + "%s")
    if r.returncode != 0:
        raise OSError("커밋 이력을 읽지 못했다")
    out = {}
    for line in r.stdout.splitlines():
        sha, _, 제목 = line.partition(_구분)
        누가 = _병합_누가(제목)
        if 누가 and (누가[1] not in out or (누가[0] == "러너" and out[누가[1]][0] != "러너")):
            out[누가[1]] = (누가[0], sha)
    return out


def _손으로_고친(project, 병합):
    """병합 커밋 가지의 꼬리표 달린 마지막 커밋 뒤에 쌓인 꼬리표 없는 병합 아닌 커밋 수 — 러너가 마지막 단계를 커밋한 뒤 사람이 고친 것. 못 읽으면 None."""
    r = _git_읽기(project, "log", "--topo-order", "--no-merges", "--format=\x1e%H" + _구분 + "%(trailers:key=Loop-Issue,valueonly)",
                  "%s^1..%s^2" % (병합, 병합))
    if r.returncode != 0:
        return None
    n = 0
    for rec in r.stdout.split("\x1e")[1:]:
        if rec.partition(_구분)[2].strip():
            break
        n += 1
    return n


def _회고_맥락(item):
    """프로젝트 하나를 한 번 읽은 것 — 카드 목록·멈춘종류 이력·병합·소각된 실행. 못 읽으면 예외."""
    path = item["path"]
    backlog = os.path.join(path, "docs", "backlog")
    if not os.path.isdir(backlog):
        raise OSError("작업목록 폴더를 읽지 못했다: %s" % backlog)
    cards = merge_cards(read_cards(backlog), REMOTE.get(os.path.abspath(path)), item.get("기기", ""))
    stops, 오류 = read_stop_kinds(path)
    if 오류:
        raise OSError(오류)
    소각 = {}
    for sha, stem, names in _소각_로그(path):
        폴더들 = {}
        for n in names:
            m = re.fullmatch(r"\.workflow/([^/]+)/(.+)", n)
            if m:
                폴더들.setdefault(m.group(1), []).append(m.group(2))
        for 폴더, 파일 in sorted(폴더들.items()):
            소각.setdefault(stem, []).append({"sha": sha, "폴더": 폴더, "파일": 파일})
    for rs in 소각.values():
        rs.sort(key=lambda r: r["폴더"])
    채택 = []
    for c in cards:                          # 채택은 시작하지 않은 카드의 머리말도 센다
        try:
            fm = front_matter(os.path.join(backlog, c["file"]))
        except (OSError, UnicodeDecodeError):
            fm = {}
        채택.append({"file": c["file"], "상태": c["상태"], "갱신": c["갱신"], "회고제안": fm.get("회고제안", "")})
    return {"item": item, "path": path, "cards": cards, "stops": stops, "병합": _병합_찾기(path), "소각": 소각, "글": None, "메모": {}, "채택": 채택}


def _본문(ctx, 실행, 이름):
    """소각된 실행이 지운 산출물의 글 — 부모 커밋의 글이다(`git show <소각>^:<경로>` 와 같다). 한 프로젝트의 것을 git 한 번으로 읽는다."""
    if ctx["글"] is None:
        ctx["글"] = _묶음_읽기(ctx["path"], ["%s^:.workflow/%s/%s" % (r["sha"], r["폴더"], n)
                                             for rs in ctx["소각"].values() for r in rs for n in r["파일"]
                                             if n in ("11-retro.md", "10-handoff.md", "state.json")])
    return ctx["글"].get("%s^:.workflow/%s/%s" % (실행["sha"], 실행["폴더"], 이름))


def _카드_자료(ctx, c):
    """시작한 적이 있는 카드의 자료, 아니면 None."""
    if c["file"] in ctx["메모"]:
        return ctx["메모"][c["file"]]
    stem = c["file"].removesuffix(".md")
    h = read_card_history(ctx["path"], c["file"])
    d = None
    if (c.get("시작") or "").strip() or any(x[1].isdigit() for x in h["커밋"]):
        실행 = ctx["소각"].get(stem, [])
        병합 = ctx["병합"].get(stem)
        막은 = {}
        for r in 실행:
            for 까닭 in _막은_까닭(_본문(ctx, r, "10-handoff.md") or "") or []:
                막은[까닭] = 막은.get(까닭, 0) + 1
        d = {"카드": stem, "제목": c["title"], "file": c["file"], "갱신": c["갱신"], "크기": c.get("크기", ""), "h": h, "c": c, "ctx": ctx,
             "멈춤": ctx["stops"].get(stem, {}), "병합": 병합[0] if 병합 else "", "실행": 실행,
             "답": sum(1 for r in 실행 if "07-answers.md" in r["파일"]),
             "손커밋": _손으로_고친(ctx["path"], 병합[1]) if 병합 else None, "막은까닭": 막은}
    ctx["메모"][c["file"]] = d
    return d


def _카드들(ctx, cond):
    return [d for c in ctx["cards"] if _카드_쓸것(c, cond) for d in [_카드_자료(ctx, c)] if d]


def _지표(ds):
    """칸 1 의 지표 — 카드 묶음에서. 카드가 없으면 숫자는 모두 「—」."""
    n = len(ds)
    infos = [{"비용": sum(r["cost"] for r in d["h"]["단계"].values()), "크기": d["크기"], "갱신": d["갱신"],
              "반려": any(v["반려"] for v in d["h"]["판정수"].values()), "판정": any(v["판정"] for v in d["h"]["판정수"].values()),
              "병합": d["병합"]} for d in ds]
    s = signals_지표(infos, 0)
    넷 = {"사람 결정 멈춤": sum(d["멈춤"].get("사람 결정", 0) for d in ds), "07 답": sum(d["답"] for d in ds),
         "이어받기": sum(d["h"]["이어받은 실행"] for d in ds)}
    센 = [d for d in ds if d["손커밋"] is not None]
    넷["손으로 고친 커밋"] = sum(d["손커밋"] for d in 센) if 센 else None
    시간 = [x for x in (sum(r["ms"] for r in d["h"]["단계"].values()) for d in ds) if x > 0]
    막은 = {}
    for d in ds:
        for 까닭, 수 in d["막은까닭"].items():
            막은[까닭] = 막은.get(까닭, 0) + 수
    return {"개입": "%.1f" % (sum(v or 0 for v in 넷.values()) / n) if n else "—", "개입넷": 넷, "손커밋표본": (len(센), n),
            "통과": s["통과"], "통과수": s["통과수"], "자동": s["자동"], "자동수": s["자동수"], "막은까닭": 막은,
            "비용중앙": s["중앙"], "시간중앙": _걸린(statistics.median(시간)) if 시간 else "—"}


def _진하기(값, 가장):
    if not 값 or not 가장:
        return 0
    return min(3, int(-(-round(3 * 값 / 가장, 9) // 1)))


def _단계_표(ds):
    """칸 2 — 단계마다 반려율·평균 시간·예상 대비·이어받기·헛도는 회차와 진하기, 막힌 카드와 막힌 자리. 고른 카드 어디에도 자료가 없는 단계는 줄을 내지 않는다."""
    순서 = list(STAGE_NAME)
    단계들 = {st for d in ds for st in d["h"]["단계"]} | {st for d in ds for st, v in d["h"]["판정수"].items() if v["판정"]}
    줄들 = []
    for st in sorted(단계들, key=lambda x: (순서.index(x) if x in 순서 else len(순서), x)):
        판 = [d["h"]["판정수"].get(st) for d in ds if st in d["h"]["판정수"]]
        j, rj = sum(v["판정"] for v in 판), sum(v["반려"] for v in 판)
        ms = [d["h"]["단계"][st]["ms"] for d in ds if st in d["h"]["단계"] and d["h"]["단계"][st]["ms"] > 0]
        배 = []
        for d in ds:
            중앙 = d["ctx"] and read_size_medians(d["ctx"]["item"], d["ctx"]["cards"], d["c"])["단계"].get(st)
            row = d["h"]["단계"].get(st)
            if row and row["ms"] > 0 and 중앙:
                배.append(row["ms"] / 중앙)
        값 = {"반려율": (rj / j) if j else None, "평균": (sum(ms) / len(ms) / 60000) if ms else None,
             "예상대비": (sum(배) / len(배)) if 배 else None, "이어받기": sum(d["h"]["이어받은 실행 단계"].count(st) for d in ds),
             "헛돎": sum(1 for d in ds if st in d["h"]["단계"] and d["h"]["단계"][st]["runs"] > 1)}
        글 = {"반려율": _비율(rj, j), "평균": "%d분" % int(값["평균"] + 0.5) if ms else "—",
             "예상대비": "%.1f배" % 값["예상대비"] if 배 else "—", "이어받기": str(값["이어받기"]), "헛돎": str(값["헛돎"])}
        막힌, 자리 = [], []
        for d in ds:
            row = d["h"]["단계"].get(st)
            조각 = [("반려", d["h"]["판정수"].get(st, {}).get("반려", 0)), ("이어받기", d["h"]["이어받은 실행 단계"].count(st)),
                  ("회차", row["runs"] if row and row["runs"] > 1 else 0)]
            까닭 = " · ".join("%s %d" % x for x in 조각 if x[1])
            if not 까닭:
                continue
            막힌.append((d["카드"], d["제목"], 까닭))
            for r in d["실행"]:
                for 문장 in _절_항목(_본문(d["ctx"], r, "11-retro.md") or "", "2. 막힌 자리") or []:
                    if 문장.split(" · ")[0].strip().startswith(st):
                        자리.append((d["카드"], 문장))
        줄들.append({"단계": st, "이름": STAGE_NAME.get(st, ""), "값": 값, "글": 글, "진하기": {}, "막힌카드": 막힌, "막힌자리": 자리})
    for 열 in ("반려율", "평균", "예상대비", "이어받기", "헛돎"):
        가장 = max([z["값"][열] or 0 for z in 줄들] or [0])
        for z in 줄들:
            z["진하기"][열] = 0 if 열 == "예상대비" and (z["값"][열] or 0) <= 1.2 else _진하기(z["값"][열], 가장)
    return 줄들


def _회고_줄들(ctx, ds):
    """카드들의 소각된 실행 가운데 회고 글이 있는 것 — `[(카드, 날짜, 제안 줄들)]` 와 `{(카드, 원문): (프로젝트, 경로, 커밋, 종류)}`."""
    out, 위치 = [], {}
    for d in ds:
        for r in d["실행"]:
            글 = _본문(ctx, r, "11-retro.md")
            if not 글:
                continue
            줄들 = _회고_제안_읽기(글)
            날 = re.search(r"(\d{4}-\d{2}-\d{2})$", r["폴더"])
            out.append((d["카드"], 날.group(1) if 날 else "", 줄들))
            for 절, 원문, _ in 줄들:
                위치.setdefault((d["카드"], 원문), (ctx["item"]["name"], ".workflow/%s/11-retro.md" % r["폴더"], r["sha"],
                                                 next(t for n, _, t in _회고_절 if n == 절)))
    return out, 위치


def _구간_값(ctx, 구간, 이름, 캐시):
    """변경 표 줄의 전·뒤 값 — 그 프로젝트의 시작한 카드 가운데 `갱신` 날짜가 구간에 드는 것으로 칸 1·3·4 와 같은 규칙으로 센다."""
    if 구간 not in 캐시:
        ds = _카드들(ctx, {"시나리오": "", "기능": "", "기기": "", "시작": 구간[0], "끝": 구간[1]})
        회고들, _ = _회고_줄들(ctx, ds)
        진행 = _제안_진행(_제안_묶기(회고들), ctx["채택"], [], dt.date.today())
        캐시[구간] = (ds, _지표(ds), 진행)
    ds, m, 진행 = 캐시[구간]
    if not ds:
        return "—"
    if 이름 == "첫 통과율":
        return m["통과"]
    if 이름 == "자동 병합":
        return m["자동"]
    if 이름 == "사람 개입":
        return m["개입"]
    if 이름 == "멈춘 종류별 건수":
        return str(sum(sum(d["멈춤"].values()) for d in ds))
    return _비율(sum(1 for x in 진행 if x["채택"]), len(진행))


def _변경_칸(ctx, 시작, 끝):
    """칸 5 — 변경 표 줄 가운데 날짜가 기간 안인 것에 전·뒤 값과 판정을 단다. 파일이 없으면 줄이 없다."""
    try:
        with open(os.path.join(ctx["path"], _변경표_경로), encoding="utf-8") as f:
            줄들, 건너뜀 = _변경표_읽기(f.read())
    except (OSError, UnicodeDecodeError):
        return [], 0
    일수 = ((dt.datetime.strptime(끝, "%Y-%m-%d") - dt.datetime.strptime(시작, "%Y-%m-%d")).days + 1) if 시작 and 끝 and _날짜(시작) and _날짜(끝) else None
    캐시, out = {}, []
    for x in 줄들:
        if not _날짜(x["날짜"]) or (시작 and x["날짜"] < 시작) or (끝 and x["날짜"] > 끝):
            continue
        전 = 뒤 = "—"
        if x["아는지표"] and 일수:
            날 = dt.datetime.strptime(x["날짜"], "%Y-%m-%d").date()
            이름 = x["아는지표"][0]
            전 = _구간_값(ctx, ((날 - dt.timedelta(days=일수)).isoformat(), (날 - dt.timedelta(days=1)).isoformat()), 이름, 캐시)
            뒤 = _구간_값(ctx, (날.isoformat(), (날 + dt.timedelta(days=일수 - 1)).isoformat()), 이름, 캐시)
        out.append(dict(x, 전=전, 뒤=뒤, 판정=_판정(전, 뒤, x["기대"]), 프로젝트=ctx["item"]["name"]))
    return out, 건너뜀


def _막힘_글(d):
    조각 = ["%s 반려 %d" % (st, v["반려"]) for st, v in d["h"]["판정수"].items() if v["반려"]]
    if d["h"]["이어받은 실행"]:
        조각.append("이어받기 %d" % d["h"]["이어받은 실행"])
    조각 += ["%s 멈춤 %d" % (k, n) for k, n in d["멈춤"].items() if n]
    return " · ".join(조각) or "막힘 없음"


def _비용_글(ctx, 실행):
    try:
        metrics = json.loads(_본문(ctx, 실행, "state.json") or "").get("metrics") or {}
        비용 = [v["cost"] for v in metrics.values() if isinstance(v, dict) and isinstance(v.get("cost"), (int, float))]
    except (ValueError, AttributeError):
        return "—"
    return _돈(sum(비용)) if 비용 else "—"


def read_retro(items, qs):
    """`/retro` 의 자료 — `(자료, 까닭)`. 예외를 내지 않는다 — 못 읽은 프로젝트는 `자료["오류"]` 한 줄이 되고 나머지 칸은 그대로 낸다."""
    qs = _비용품질_인자(qs)
    g = lambda k: (qs.get(k) or [""])[0].strip()          # noqa: E731
    시작, 끝, 앞시작, 앞끝, 기간까닭 = signals_기간(g("period"), g("from"), g("to"), dt.date.today())
    자료 = {"기간": (시작, 끝, 앞시작, 앞끝), "기간까닭": 기간까닭, "프로젝트들": [i["name"] for i in items], "오류": [], "지표": _지표([]), "앞지표": _지표([]),
          "단계": [], "멈춤": [], "제안": [], "변경": {"줄": [], "건너뜀": 0}, "회고": []}
    cond, 까닭 = _조건(qs)
    if 까닭:
        return 자료, 까닭
    현재, 앞, 변경, 채택, 회고들, 위치, 회고줄 = [], [], [], [], [], {}, []
    for item in items:
        if cond["프로젝트"] and item["name"] != cond["프로젝트"]:
            continue
        try:
            ctx = _회고_맥락(item)
            ds = _카드들(ctx, cond)
            앞ds = _카드들(ctx, dict(cond, 시작=앞시작, 끝=앞끝)) if 앞시작 else []
            줄, 건너뜀 = _변경_칸(ctx, 시작, 끝)
            회, 위 = _회고_줄들(ctx, ds)
            행 = [{"카드": d["카드"], "제목": d["제목"], "폴더": r["폴더"], "날짜": (re.search(r"(\d{4}-\d{2}-\d{2})$", r["폴더"]) or [None, ""])[1],
                  "커밋": r["sha"], "글": _본문(ctx, r, "11-retro.md"), "막힘": _막힘_글(d), "비용": _비용_글(ctx, r)}
                 for d in ds for r in d["실행"]]
        except Exception as e:                       # 한 프로젝트의 실패가 다른 프로젝트 읽기를 막지 않는다
            자료["오류"].append("%s: 못 읽음 — %s" % (item["name"], str(e) or type(e).__name__))
            continue
        현재 += ds
        앞 += 앞ds
        변경 += 줄
        자료["변경"]["건너뜀"] += 건너뜀
        채택 += ctx["채택"]
        회고들 += 회
        위치.update(위)
        회고줄 += 행
    자료["지표"], 자료["앞지표"] = _지표(현재), _지표(앞)
    자료["단계"] = _단계_표(현재)
    종류 = {}
    for d in 현재:
        for k, n in d["멈춤"].items():
            z = 종류.setdefault(k, {"종류": k, "수": 0, "사례": [], "처방": _멈춤_처방.get(k, "분류 없음")})
            z["수"] += n
            if len(z["사례"]) < 3 and d["카드"] not in z["사례"]:
                z["사례"].append(d["카드"])
    자료["멈춤"] = sorted(종류.values(), key=lambda z: (-z["수"], z["종류"]))
    자료["변경"]["줄"] = 변경
    for z in _제안_진행(_제안_묶기(회고들), 채택, 변경, dt.date.today()):
        카드, 열쇠 = z["출처"][0]
        프로젝트, 경로, 커밋, 종류_ = 위치.get((카드, z["원문"]), (None, None, None, None))
        z["종류"] = 종류_ or ""
        z["양식"] = ({"프로젝트": 프로젝트, "pick": 항목_열쇠(_항목(종류_, 카드, "11", z["새로나옴"], z["원문"], 경로, 커밋, "이력"))}
                   if not z["채택"] and 프로젝트 else None)
        자료["제안"].append(z)
    자료["회고"] = 회고줄
    return 자료, ""


_회고_CSS = """.rt-lv { display:inline-block; min-width:44px; padding:2px 6px; border-radius:4px; text-align:center; }
.rt-lv[data-lv="1"] { background:var(--soft); } .rt-lv[data-lv="2"] { background:var(--orange-bg); color:var(--orange); } .rt-lv[data-lv="3"] { background:var(--orange-line); color:var(--orange); }
.rt-head { display:grid; grid-template-columns:minmax(120px,1.4fr) repeat(5, minmax(70px,1fr)); gap:8px; font-size:12px; color:var(--dim); padding:6px 0; }
.stg > summary { display:grid; grid-template-columns:minmax(120px,1.4fr) repeat(5, minmax(70px,1fr)); gap:8px; align-items:center; cursor:pointer; padding:6px 0; }
.rt pre { white-space:pre-wrap; font-family:ui-monospace,Menlo,monospace; font-size:12px; overflow:auto; }
.rt-warn { color:var(--orange); font-weight:600; }
.rt-empty { color:var(--dim); font-size:13px; }
.rt-sub { font-size:12px; color:var(--dim); }
.sg-t form input { width:130px; }
"""


def render_retro(qs, 자료, 까닭):
    """워크플로우 회고 화면 HTML. 칸마다 `<section class="panel" data-panel="n">`, 숫자 칸마다 `data-k`. 값은 모두 escape 한다."""
    esc = html.escape
    qs = _비용품질_인자(qs)
    v = lambda k: (qs.get(k) or [""])[0]          # noqa: E731
    눌림 = v("period") if v("period") in _신호_기간_이름 else ("week" if v("period") or not (v("from") or v("to")) else "")
    거른 = {k: v(k) for k in ("p",) if v(k)}
    단추 = "".join('<a href="/retro?%s" aria-pressed="%s">%s</a>' % (
        esc(urllib.parse.urlencode(dict(거른, period=k))), "true" if k == 눌림 else "false", 글) for k, 글 in _신호_기간_이름.items())
    고르개 = ('<form method="get" action="/retro"><select name="p" aria-label="프로젝트"><option value="">모든 프로젝트</option>%s</select>'
           '<input type="hidden" name="period" value="%s"><button type="submit">보기</button></form>'
           % ("".join('<option value="%s"%s>%s</option>' % (esc(n, quote=True), " selected" if n == v("p") else "", esc(n)) for n in 자료.get("프로젝트들", [])),
              esc(눌림 or "week", quote=True)))
    parts = ['<div class="sg-top"><h1>%s</h1><div class="sg-pg" role="group" aria-label="기간">%s</div>%s'
             '<span class="sg-hint">기간을 바꾸면 여섯 칸이 모두 바뀐다</span></div>' % (esc(화면이름["retro"]), 단추, 고르개)]
    if 까닭 or 자료.get("기간까닭"):
        parts.append("<p>%s</p>" % esc(까닭 or 자료["기간까닭"]))
    parts += ['<p class="sg-hint">%s</p>' % esc(x) for x in 자료.get("오류", [])]
    비었음 = '<p class="rt-empty">기록이 아직 없음</p>'

    def 칸(번호, 제목, 본문):
        return '<section class="panel" data-panel="%d"><div class="ph"><h2>%s</h2></div><div class="sg-in">%s</div></section>' % (번호, esc(제목), 본문)

    # 1. 핵심 지표
    m, 앞 = 자료["지표"], 자료["앞지표"]
    넷 = m["개입넷"]
    칸글 = lambda x: "—" if x is None else str(x)          # noqa: E731
    타일 = [("개입", "카드당 사람 개입", "사람 결정 멈춤 %s · 07 답 %s · 이어받기 %s · 손으로 고친 커밋 %s" % tuple(칸글(넷[k]) for k in ("사람 결정 멈춤", "07 답", "이어받기", "손으로 고친 커밋"))),
          ("통과", "첫 통과율", m["통과수"]),
          ("자동", "자동 병합 비율", "막은 까닭: " + (", ".join("%s %d" % x for x in m["막은까닭"].items()) or "—")),
          ("비용", "카드당 비용 중앙값", "")]
    칸1 = ['<div class="tiles sg-k">']
    for 열쇠, 이름, 아래 in 타일:
        값, 앞값 = (m["비용중앙"], 앞["비용중앙"]) if 열쇠 == "비용" else (m[열쇠], 앞[열쇠])
        부 = esc(아래) + ('카드당 걸린 시간 중앙값 <span data-k="지표:시간">%s</span> (직전 <span data-k="앞:시간">%s</span>)' % (esc(m["시간중앙"]), esc(앞["시간중앙"])) if 열쇠 == "비용" else "")
        칸1.append('<div class="tile"><div class="lb">%s</div><div class="v" data-k="지표:%s">%s</div><div class="sub">%s</div>'
                 '<div class="sub">직전 기간 <span data-k="앞:%s">%s</span></div></div>' % (esc(이름), 열쇠, esc(값), 부, 열쇠, esc(앞값)))
    칸1.append("</div>")
    if m["손커밋표본"][1]:
        칸1.append('<p class="rt-sub">손으로 고친 커밋: 병합된 카드만 센다(%d장 중 %d장)</p>' % (m["손커밋표본"][1], m["손커밋표본"][0]))
    # 2. 단계별 표
    열 = (("반려율", "반려율"), ("평균", "평균 시간"), ("예상대비", "예상 대비"), ("이어받기", "이어받기"), ("헛돎", "헛도는 회차"))
    칸2 = ""
    if 자료["단계"]:
        칸2 = '<div class="rt-head"><span>단계</span>%s</div>' % "".join("<span>%s</span>" % esc(n) for _, n in 열)
        for z in 자료["단계"]:
            칸들 = "".join('<span class="rt-lv" data-k="%s" data-lv="%d">%s</span>' % (
                esc("단계:%s:%s" % (z["단계"], k), quote=True), z["진하기"][k], esc(z["글"][k])) for k, _ in 열)
            막힌 = "".join("<li>%s %s — %s</li>" % (esc(c), esc(t), esc(why)) for c, t, why in z["막힌카드"]) or "<li>막힌 카드 없음</li>"
            자리 = "".join("<li>%s — %s</li>" % (esc(c), esc(s)) for c, s in z["막힌자리"]) or "<li>기록이 아직 없음</li>"
            칸2 += ('<details class="stg" data-st="%s"><summary><b>%s %s</b>%s</summary><p class="rt-sub">막힌 카드</p><ul>%s</ul>'
                  '<p class="rt-sub">막힌 자리</p><ul>%s</ul></details>' % (esc(z["단계"], quote=True), esc(z["단계"]), esc(z["이름"]), 칸들, 막힌, 자리))
    # 3. 멈춤 원인
    칸3 = ""
    if 자료["멈춤"]:
        칸3 = ("<table class=\"sg-t\"><thead><tr><th>종류</th><th>수</th><th>사례</th><th>처방</th></tr></thead><tbody>%s</tbody></table>" % "".join(
            '<tr><td>%s</td><td data-k="%s">%d</td><td>%s</td><td data-k="%s">%s</td></tr>' % (
                esc(z["종류"]), esc("멈춤:%s:수" % z["종류"], quote=True), z["수"], esc(", ".join(z["사례"])),
                esc("멈춤:%s:처방" % z["종류"], quote=True), esc(z["처방"])) for z in 자료["멈춤"]))
    # 4. 반복 지적에서 규칙까지
    칸4 = ""
    if 자료["제안"]:
        행 = []
        for z in 자료["제안"]:
            양식 = z.get("양식")
            만들기 = ('<form method="post" action="/p/%s/make-card"><input type="hidden" name="pick" value="%s">'
                   '<input name="title" placeholder="제목" required><input name="slug" placeholder="파일 이름(영문 소문자)" required>'
                   '<button type="submit">카드로 만들기</button></form>' % (esc(urllib.parse.quote(양식["프로젝트"], safe=""), quote=True), esc(양식["pick"], quote=True))
                   if 양식 else "—")
            행.append("<tr><td>%s</td><td>%d</td><td>%s</td><td>%s</td><td>%s</td><td>%s%s</td><td>%s</td></tr>" % (
                esc(z["원문"]), z["근거수"], esc(z["새로나옴"]), esc(z["채택"] or "—"), "예" if z["반영"] else "—",
                "예" if z["효과"] else "—", ' <span class="rt-warn">오래 머묾</span>' if z["경고"] else "", 만들기))
        칸4 = ("<table class=\"sg-t\"><thead><tr><th>제안</th><th>근거 수</th><th>새로 나옴</th><th>채택</th><th>반영</th><th>효과 확인</th><th>카드로 만들기</th></tr></thead>"
             "<tbody>%s</tbody></table>" % "".join(행))
    # 5. 바꾼 것의 효과
    칸5 = ""
    if 자료["변경"]["줄"]:
        행 = []
        for i, x in enumerate(자료["변경"]["줄"], 1):
            행.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td>%s</tr>" % (
                esc(x["날짜"]), esc(x["변경"]), esc(x["지표"]), esc(x["기대"]),
                "".join('<td data-k="변경:%d:%s">%s</td>' % (i, k, esc(x[k])) for k in ("전", "뒤", "판정"))))
        칸5 = ("<table class=\"sg-t\"><thead><tr><th>날짜</th><th>변경</th><th>겨냥한 지표</th><th>기대 방향</th><th>전</th><th>뒤</th><th>판정</th></tr></thead>"
             "<tbody>%s</tbody></table>" % "".join(행))
    if 자료["변경"]["건너뜀"]:
        칸5 += '<p class="rt-sub">형식이 다른 줄 %d개를 건너뜀</p>' % 자료["변경"]["건너뜀"]
    # 6. 이 기간의 회고
    칸6 = ""
    for x in 자료["회고"]:
        본문 = '<pre>%s</pre>' % esc(x["글"]) if x["글"] is not None else 비었음
        칸6 += ('<details class="rt"><summary><b>%s</b> %s %s <code>%s</code> <span data-k="%s">%s</span> <span data-k="%s">%s</span></summary>%s</details>' % (
            esc(x["제목"]), esc(x["카드"]), esc(x["날짜"]), esc(x["커밋"][:7]), esc("회고:%s:막힘" % x["카드"], quote=True), esc(x["막힘"]),
            esc("회고:%s:비용" % x["카드"], quote=True), esc(x["비용"]), 본문))
    칸들 = [("핵심 지표", "".join(칸1)), ("단계별 표", 칸2), ("멈춤 원인", 칸3),
          ("반복 지적에서 규칙까지", 칸4), ("바꾼 것의 효과", 칸5), ("이 기간의 회고", 칸6)]
    for i, (제목, 본문) in enumerate(칸들, 1):
        parts.append(칸(i, 제목, 본문 + (비었음 if i == 1 and not m["손커밋표본"][1] else "") if i == 1 else 본문 or 비었음))
    틀 = FILE_TEMPLATE.replace('<a href="board.html">← 보드</a>', '<a href="/">← %s</a>' % 화면이름["home"]).replace("<h1>%(name)s</h1>\n", "", 1)
    return (틀 % {"name": 화면이름["retro"], "body": "\n".join(parts)}).replace("</style>", _신호_CSS + _회고_CSS + "</style>", 1)


# ── 기능 진척 화면과 정리된 시나리오 상세 (ISSUE-ui-13-features-scenario-2026-11-24) ──
# 요소 상태는 `요소_칸`(→ `scenario_progress`)·완료 기준은 `criteria_rows` 가 정한다 — 이 화면들은 그 결과를 그리기만 한다.
_기능_CSS = """main { max-width:1200px; }\n.fx { font-size:14px; }
.fx h1 { font-size:22px; font-weight:700; color:var(--fg); margin:4px 0 12px; }
.fx h1 span { font-size:13px; font-weight:400; color:var(--dim); margin-left:8px; }
.fx .ftabs { display:flex; flex-wrap:wrap; gap:4px; margin:0 0 16px; }
.fx .ftabs a { padding:4px 12px; border-radius:6px; font-size:13px; color:var(--dim); }
.fx .ftabs a[aria-current] { background:var(--soft); color:var(--fg); font-weight:600; }
.fx .tiles { display:flex; flex-wrap:wrap; gap:12px; margin:0 0 16px; }
.fx .tile { flex:1; min-width:150px; padding:12px 16px; border:1px solid var(--line); border-radius:var(--r-card); display:flex; flex-direction:column; }
.fx .tile .lb { font-size:12px; color:var(--dim); } .fx .tile .v { font-size:22px; font-weight:700; color:var(--fg); } .fx .tile .sub { font-size:12px; color:var(--dim); }
.fx .lg { display:flex; flex-wrap:wrap; gap:8px; margin:0 0 12px; font-size:12px; color:var(--dim); }
.fx .fr { padding:12px 0; border-top:1px solid var(--line); }
.fx .fh { display:flex; flex-wrap:wrap; align-items:center; gap:10px; margin:0 0 8px; }
.fx .fh .sid { font:12px var(--mono); color:var(--dim); } .fx .fh .fn { font-size:15px; font-weight:600; color:var(--fg); }
.fx .fm { margin:8px 0 0; font-size:12px; color:var(--dim); }
.fx .cells { display:flex; flex-wrap:wrap; gap:6px; }
.fx .ec { min-width:34px; padding:3px 8px; border-radius:6px; border:1px solid transparent; font:12px var(--mono); text-align:center; }
.fx .ec-done { background:var(--teal-bg); color:var(--teal); } .fx .ec-doing { background:var(--blue-bg); color:var(--blue); }
.fx .ec-hold { background:var(--ochre-bg); color:var(--ochre-ink); } .fx .ec-stop { background:var(--orange-bg); color:var(--orange); }
.fx .ec-idea { border:1px dashed var(--gray); color:var(--dim); } .fx .ec-none { background:var(--gray-bg); color:var(--dim); }
.fx .fbar { width:160px; height:6px; border-radius:3px; background:var(--soft); overflow:hidden; } .fx .fbar i { display:block; height:6px; background:var(--blue); }
.fx .who { display:flex; gap:12px; flex-wrap:wrap; margin:0 0 16px; color:var(--fg); }
.fx .who-box { flex:1 1 260px; background:var(--panel); border:1px solid var(--line); border-radius:var(--r-card); padding:12px 14px; }
.fx .who-box h3 { margin:0 0 6px; font-size:12px; color:var(--dim); font-weight:600; } .fx .who-box p { margin:0; }
.fx details { margin:6px 0; } .fx summary { cursor:pointer; color:var(--fg); }
.fx .crumb { font-size:12px; color:var(--dim); margin:0 0 8px; }
.fx table { display:table; }
"""
_취소선 = re.compile(r"~~(.+?)~~")


def _취소선_뺌(x):
    """칸 글에서 `~~지운 글~~` 조각을 빼고 공백을 하나로 줄인다."""
    return re.sub(r"\s+", " ", _취소선.sub("", x).replace("~~", "")).strip()


def _기능_틀():
    return FILE_TEMPLATE.replace('<a href="board.html">← 보드</a>\n<h1>%(name)s</h1>\n', "", 1).replace("</style>", _기능_CSS + "</style>", 1)


def render_features(item, items, cards, scenarios):
    """`/p/<이름>/features` — 프로젝트 탭, 요약 넷, 시나리오마다 한 줄(요소 칸), 시나리오 밖 카드 한 줄."""
    esc = html.escape
    주소 = _주소(item["name"])
    탭 = "".join('<a href="%sfeatures"%s>%s</a>' % (esc(_주소(p["name"]), quote=True), ' aria-current="page"' if p["name"] == item["name"] else "", esc(p["name"]))
               for p in items)
    칸들 = {s["번호"]: 요소_칸(cards, s) for s in scenarios}
    기준들 = {s["번호"]: criteria_rows(cards, s) for s in scenarios if not s["오류"]}
    요소 = [k for v in 칸들.values() for k in v]
    기준 = [r for v in 기준들.values() for r in v]
    보류 = [c for c in cards if c["상태"] == "보류"]
    적힘 = sum(1 for c in 보류 if c["멈춘이유"].strip())
    기다림 = sum(1 for s in scenarios if not s["오류"] and _시나리오_배지(cards, s) == "완료 선언 대기")

    def tile(이름, 값, 설명=""):
        return '<div class="tile"><span class="lb">%s</span><span class="v">%s</span><span class="sub">%s</span></div>' % (esc(이름), esc(값), esc(설명))
    요약 = '<div class="tiles">%s</div>' % "".join([
        tile("시나리오", str(len(scenarios)), "완료 선언 대기 %d" % 기다림),
        tile("요소 완료", "%d/%d" % (sum(1 for k in 요소 if k["상태"] == "완료"), len(요소)) if scenarios else "—", "카드 없음 %d" % sum(1 for k in 요소 if k["상태"] == "카드 없음")),
        tile("완료 기준 확인", "%d/%d" % (sum(1 for r in 기준 if r["모두 완료"]), len(기준)) if 기준 else "—", ""),
        tile("보류", str(len(보류)), "까닭 적힘 %d · 까닭 없음 %d" % (적힘, len(보류) - 적힘))])

    def 셀(k):
        제목 = "%s · %s%s" % (k["요소"], k["상태"], " — " + k["까닭"] if k["상태"] == "보류" else "")
        cls = "ec ec-%s" % _칸_클래스[k["모양"]]
        if k["카드"]:
            return '<a class="%s" href="%s" title="%s">%s</a>' % (cls, esc(주소 + urllib.parse.quote(k["카드"]["file"]), quote=True), esc(제목, quote=True), esc(k["요소"]))
        return '<span class="%s" title="%s">%s</span>' % (cls, esc(제목, quote=True), esc(k["요소"]))

    def 줄(s):
        머리 = '<span class="sid">%s</span>' % esc(s["번호"])
        if s["오류"]:
            return '<section class="fr" data-sc="%s"><div class="fh">%s<span class="fn">%s</span><span class="why">%s</span></div></section>' % (
                esc(s["번호"]), 머리, esc(s["이름"]), esc(s["오류"]))
        p = scenario_progress(cards, s)
        r = 기준들[s["번호"]]
        배지 = _시나리오_배지(cards, s)
        셀들 = "".join(셀(k) for k in 칸들[s["번호"]]) or '<span class="meta">요소 없음</span>'
        return ('<section class="fr" data-sc="%s"><div class="fh">%s<a class="fn" href="%splan/%s">%s</a><span class="badge b-%s">%s</span>'
                '<div class="fbar"><i style="width:%d%%"></i></div></div><div class="cells">%s</div>'
                '<p class="fm">완료 %d/%d · 완료 기준 확인 %s</p></section>'
                % (esc(s["번호"]), 머리, esc(주소, quote=True), esc(s["file"], quote=True), esc(_취소선_뺌(s["이름"])), _배지_색(배지), esc(배지),
                   p["퍼센트"] or 0, 셀들, p["수"]["완료"], p["분모"],
                   "%d/%d" % (sum(1 for x in r if x["모두 완료"]), len(r)) if r else "—"))
    known = {s["번호"] for s in scenarios}
    낱장 = [c for c in cards if c["시나리오"] not in known]
    분포 = sorted(collections.Counter(c["상태"] for c in 낱장).items(), key=lambda kv: (-kv[1], COLUMNS.index(kv[0]) if kv[0] in COLUMNS else 99))
    밖 = "시나리오 밖 카드 %d" % len(낱장) + (" — " + " · ".join("%s %d" % kv for kv in 분포) if 낱장 else "")
    범례 = '<p class="lg">%s</p>' % "".join('<span class="ec ec-%s">%s</span>' % (c, esc(n)) for c, n in (
        ("done", "완료"), ("doing", "진행·대기"), ("hold", "보류"), ("stop", "멈춘 실행"), ("idea", "아이디어"), ("none", "카드 없음")))
    본문 = ('<div class="fx"><p class="crumb"><a href="/">%s</a> / <a href="%s">%s</a> / %s</p>%s<div class="ftabs">%s</div>'
          '<h1>%s<span>%s</span></h1>%s%s%s<p class="fm">%s</p></div>'
          % (esc(화면이름["home"]), esc(주소, quote=True), esc(item["name"]), esc(화면이름["features"]), _프로젝트_탭(item["name"], "features"), 탭, esc(화면이름["features"]), esc(item["name"]), 요약,
             범례 if scenarios else "", "".join(줄(s) for s in scenarios) or '<p class="meta">시나리오 없음 — docs/plan/SC-*.md 가 없다</p>', esc(밖)))
    return _기능_틀() % {"name": esc("%s · %s" % (화면이름["features"], item["name"])), "body": 본문}


def _시나리오_머리(text):
    """시나리오 문서 원문에서 (머리 표 칸 `{열쇠: 값}`, `## ` 절 `[(제목, [줄])]`). 맨 위 `---` 머리말 블록은 건너뛴다."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        끝 = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if 끝 is not None:
            lines = lines[끝 + 1:]
    칸, 절, 지금 = {}, [], None
    for l in lines:
        if l.startswith("## "):
            지금 = (l[3:].strip(), [])
            절.append(지금)
        elif 지금 is not None:
            지금[1].append(l)
        elif l.strip().startswith("|") and not _표_구분줄.match(l):
            c = _칸(l)
            if len(c) >= 2:
                칸.setdefault(c[0], c[1])
    return 칸, 절


def render_scenario(item, cards, scenarios, s, text):
    """`/p/<이름>/plan/SC-nn-*.md` 의 정리된 화면 — 머리말 블록과 `~~` 는 보이지 않고, 요소 표 줄마다 카드 고리가 있다. 원문은 `?원본=1`."""
    esc = html.escape
    주소 = _주소(item["name"])
    칸값, 절들 = _시나리오_머리(text)
    칸 = {k["요소"]: k for k in 요소_칸(cards, s)}
    mine = [c for c in cards if c["시나리오"] == s["번호"]]
    p = scenario_progress(cards, s)
    기준 = criteria_rows(cards, s)
    배지 = _시나리오_배지(cards, s)
    바뀜 = [x for k in ("누가", "왜") for x in _취소선.findall(칸값.get(k, ""))]
    상자 = {k: '<div class="who-box"><h3>%s</h3><p>%s</p>' % (k, esc(_취소선_뺌(칸값.get(k, ""))) or "—") for k in ("누가", "왜")}
    if 바뀜:
        상자["누가"] += ('<details class="chg"><summary>바뀐 내역 %d</summary><ul>%s</ul></details>'
                      % (len(바뀜), "".join("<li><del>%s</del></li>" % esc(x) for x in 바뀜)))
    누가왜 = "".join(v + "</div>" for v in 상자.values())

    def 링크(c):
        return '<a href="%s">%s</a>' % (esc(주소 + urllib.parse.quote(c["file"]), quote=True), esc(_취소선_뺌(c["title"])))
    줄들 = []
    for e in sorted(s["요소"], key=lambda x: int(x[2:])):
        have = sorted((c for c in mine if e in c["요소"]), key=lambda c: c["file"])
        k = 칸.get(e)
        상태 = k["상태"] if k else "카드 아님" if e in s["카드아님"] else "없어짐"
        까닭 = k["까닭"] if k and k["상태"] == "보류" else "—"
        이은 = ", ".join(sorted({a for c in have for a in (c["완료기준"] or c["인수조건"])})) or "—"
        기기 = " · ".join(dict.fromkeys(x for c in have for x in (c["돈기계"] or c["기기"],) if x)) or "—"
        줄들.append('<tr><td>%s %s</td><td>%s</td><td><span class="badge b-%s">%s</span></td><td>%s</td><td>%s</td><td>%s</td></tr>'
                  % (esc(e), esc(_취소선_뺌(s["요소이름"].get(e, ""))), "<br>".join(링크(c) for c in have) or "—", _배지_색(상태), esc(상태), esc(까닭), esc(이은), esc(기기)))
    요소표 = ('<table class="et"><thead><tr>%s</tr></thead><tbody>%s</tbody></table>'
            % ("".join("<th>%s</th>" % h for h in ("요소", "카드", "상태", "보류 까닭", "이은 완료 기준", "기기")), "".join(줄들))
            if 줄들 else '<p class="meta">요소 없음</p>')
    기준줄 = []
    for r in 기준:
        판정 = "확인됨" if r["모두 완료"] else "남음" if r["카드"] else "카드 없음 — 확인 필요"
        기준줄.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                    % (esc(r["번호"]), esc(_취소선_뺌(s["완료기준"].get(r["번호"], ""))), esc(", ".join("%s (%s)" % x for x in r["카드"]) or "—"), esc(판정)))
    기준표 = ('<table class="ct"><thead><tr>%s</tr></thead><tbody>%s</tbody></table>'
            % ("".join("<th>%s</th>" % h for h in ("번호", "기준", "이은 카드", "판정")), "".join(기준줄))
            if 기준줄 else '<p class="meta">완료 기준 없음</p>')
    접힘 = []
    for 제목, 줄 in 절들:
        if 제목 == "요소" or 제목 in _기준절:
            continue
        글 = "\n".join(_취소선.sub("", x).replace("~~", "") for x in 줄)
        try:
            속 = 본문_그리기(글)
        except Exception:                                # 그리기가 실패해도 글은 보인다
            속 = "<pre>%s</pre>" % esc(글)
        접힘.append('<details class="sec"><summary>%s</summary><div class="md">%s</div></details>' % (esc(_취소선_뺌(제목)), 속))
    본문 = ('<div class="fx"><p class="crumb"><a href="/">%s</a> / <a href="%s">%s</a> / <a href="%sfeatures">%s</a> / %s</p>'
          '<h1>%s %s</h1><p><span class="badge b-%s">%s</span> <span class="badge b-%s">%s</span></p>'
          '<div class="fbar"><i style="width:%d%%"></i></div><p class="fm">요소 %d/%d 완료 · 완료 기준 %s 확인</p>'
          '<section class="who">%s</section><h2>요소</h2>%s<h2>완료 기준</h2>%s%s%s'
          '<p><a href="%splan/%s?원본=1">원본 문서 보기</a></p></div>'
          % (esc(화면이름["home"]), esc(주소, quote=True), esc(item["name"]), esc(주소, quote=True), esc(화면이름["features"]), esc(화면이름["scenario"]),
             esc(s["번호"]), esc(_취소선_뺌(s["이름"])), _배지_색(배지), esc(배지), _배지_색(s["상태"]), esc(s["상태"]), p["퍼센트"] or 0,
             p["수"]["완료"], p["분모"], "%d/%d" % (sum(1 for x in 기준 if x["모두 완료"]), len(기준)) if 기준 else "—",
             누가왜, 요소표, 기준표, "".join(접힘), _지도_절(item, cards, scenarios, "시나리오:" + s["번호"]), esc(주소, quote=True), esc(s["file"], quote=True)))
    return _기능_틀() % {"name": esc("%s %s" % (s["번호"], _취소선_뺌(s["이름"]))), "body": 본문}


def _표_html(head, rows, 날것=False):
    """`날것` 이면 칸 글을 이미 이스케이프된 HTML 로 보고 그대로 둔다(고리가 든 칸)."""
    칸글 = (lambda c: str(c)) if 날것 else (lambda c: html.escape(str(c)))                 # noqa: E731
    return ("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
            % ("".join("<th>%s</th>" % html.escape(h) for h in head),
               "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % 칸글(c) for c in r) for r in rows)))


def _선행_고리(card, cards, scenarios, 주소, e):
    """선행 요소 e 의 보이는 HTML 과 그 요소를 가진 같은 시나리오 카드 — 카드가 있으면 고리, 없으면 글. 어느 카드로 가는지는 기능 진척 칸과 같다."""
    no = card.get("시나리오", "")
    s = next((x for x in scenarios if x["번호"] == no), None)
    칸 = {k["요소"]: k["카드"] for k in 요소_칸(cards, s)} if s else {}
    held = 칸[e] if e in 칸 else next(iter(sorted((c for c in cards if c["시나리오"] == no and e in c["요소"]), key=lambda c: c["file"])), None)
    if held is None:
        return html.escape(e), None
    return '<a href="%s">%s</a>' % (html.escape(주소 + urllib.parse.quote(held["file"]), quote=True), html.escape(e)), held


def _시나리오_절(card, cards, scenarios, 주소=""):
    no = card.get("시나리오", "")
    if not no:
        return ""
    s = next((x for x in scenarios if x["번호"] == no), None)
    if s is None:
        return "<h2>시나리오</h2><p>%s — 시나리오 파일을 찾지 못했다</p>" % html.escape(no)
    if s["오류"]:
        return "<h2>시나리오</h2><p>%s — %s</p>" % (html.escape(no), html.escape(s["오류"]))
    esc = html.escape
    rows = [("시나리오", '<a href="%splan/%s">%s</a>' % (esc(주소, quote=True), esc(s["file"], quote=True), esc('%s %s' % (no, s["이름"]))))]
    rows += [("요소", esc("%s %s" % (e, s["요소이름"].get(e, "")))) for e in card["요소"]]
    for ac in card["완료기준"] + card["인수조건"]:
        rows.append((esc("완료 기준 " + ac), esc(s["완료기준"].get(ac, "시나리오에 없는 완료 기준"))))
    for e in card["선행"]:
        글, held = _선행_고리(card, cards, scenarios, 주소, e)
        rows.append(("선행", "%s (%s)" % (글, esc(held["상태"]) if held else "카드 없음")))
    return '<h2>시나리오</h2><p><a href="board.html">← %s</a></p>' % esc(화면이름["board"]) + _표_html(("항목", "내용"), rows, 날것=True)


_기능줄 = re.compile(r"F-\d{3}")
_기능맵_머리 = ("ID", "정의", "상태", "코드 위치", "시험 위치", "근거 문서", "관련 기능", "알려진 문제")


def read_featuremap(project):
    """프로젝트 기능맵 `{"있음", "줄": {ID: {"경로", "칸"}}, "오류": [(보이는 경로, 까닭)]}`. 옛 한 장, 그다음 그룹 폴더 이름순.

    줄 판별은 `bin/featuremap_check.py` 의 `rows`·`map_files` 와 같다 — 보드는 표준 라이브러리만 가져올 수 있어 따로 둔다.
    한쪽 규칙을 바꾸면 다른 쪽도 고친다. 같은 ID 는 먼저 읽은 줄을 쓴다. 파일을 고치지 않는다."""
    files = []
    old = os.path.join(project, "docs", "기능맵.md")
    if os.path.isfile(old):
        files.append(("docs/기능맵.md", old))
    folder = os.path.join(project, "docs", "기능맵")
    if os.path.isdir(folder):
        files += [("docs/기능맵/" + n, os.path.join(folder, n)) for n in sorted(os.listdir(folder))
                  if n.endswith(".md") and os.path.isfile(os.path.join(folder, n))]
    줄, 오류 = {}, []
    for shown, full in files:
        try:
            with open(full, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as e:
            오류.append((shown, str(e) or type(e).__name__))
            continue
        fenced = False
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("```"):
                fenced = not fenced
            elif not fenced and s.startswith("|"):
                cells = [c.strip() for c in s.strip("|").split("|")]
                if len(cells) == 8 and _기능줄.fullmatch(cells[0]):
                    줄.setdefault(cells[0], {"경로": shown, "칸": cells})
    return {"있음": bool(files), "줄": 줄, "오류": 오류}


def _기능_절(card, fmap):
    ids = card.get("기능") or []
    if not ids:
        return ""
    esc = html.escape
    parts = ["<h2>기능</h2>"]
    if not fmap["있음"]:
        return parts[0] + "<p>%s</p>" % esc(", ".join(ids))
    rows = []
    for i in ids:
        r = fmap["줄"].get(i)
        if r:
            rows.append('<tr><td><a href="/search?fn=%s">%s</a></td>%s</tr>'
                        % (esc(urllib.parse.quote(i), quote=True), esc(i), "".join("<td>%s</td>" % esc(c) for c in r["칸"][1:])))
        else:
            rows.append("<tr><td>%s</td><td>기능맵에 없음</td>%s</tr>" % (esc(i), "<td></td>" * 6))
    parts.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
                 % ("".join("<th>%s</th>" % h for h in _기능맵_머리), "".join(rows)))
    parts += ["<p>기능맵 읽지 못함 — %s</p>" % esc(p) for p, _ in fmap["오류"]]
    return "".join(parts)


def _단계_글(n):
    return "%s %s" % (n, STAGE_NAME[n]) if n in STAGE_NAME else n


def _메모(n, v, h):
    """단계 칸 한 줄 메모 — 판정 단계는 「통과」·「반려 n → 통과」, 이어받은 단계는 「이어받음 n」, 없으면 「—」."""
    조각 = []
    판 = (h.get("판정수") or {}).get(n)
    if 판 and 판["판정"]:
        조각.append(("반려 %d → 통과" if v["판정"] == "통과" else "반려 %d") % 판["반려"] if 판["반려"] else "통과")
    받음 = (h.get("이어받은 단계") or []).count(n)
    if 받음:
        조각.append("이어받음 %d" % 받음)
    return " · ".join(조각) or "—"


def _머리_html(card, h, fm, 웹, 시나리오=(), 카드들=(), 주소="", 프로젝트=""):
    esc = html.escape
    stem = os.path.splitext(card["file"])[0]
    꼬리 = (card["시나리오"] + (" " + ", ".join(card["요소"]) if card["요소"] else "")) if card["시나리오"] else "시나리오 밖"
    상태 = card["상태"] + (" · %s 병합" % h["병합"] if h.get("병합") else "")
    색 = {"완료": "teal", "진행": "blue", "보류": "orange"}.get(card["상태"], "gray")
    배지 = ('<span class="badge b-%s">%s</span><span class="badge b-gray">%s</span><span class="badge b-gray">%s</span>'
           '<span class="badge b-gray">되돌리기: %s</span>'
           % (색, esc(상태), esc(card["크기"] or "—"), esc(card["돈기계"] or "—"), esc((fm.get("되돌리기") or "").strip() or "—")))
    pr = h.get("PR")
    버튼 = ('<a class="btn" href="%s/pull/%d">PR #%d</a>' % (esc(웹, quote=True), pr, pr)) if 웹 and pr else '<span class="btn">PR —</span>'
    버튼 += ('<a class="btn" href="%s/blob/HEAD/docs/backlog/%s">카드 파일</a>' % (esc(웹, quote=True), urllib.parse.quote(card["file"])) if 웹
            else '<span class="btn">카드 파일 —</span>')
    sc = next((x for x in 시나리오 if x["번호"] == card["시나리오"]), None)
    시 = ('<a href="%splan/%s">%s</a>' % (esc(주소, quote=True), esc(sc["file"], quote=True), esc(card["시나리오"]))
        if sc else esc(card["시나리오"] or "—"))
    선 = ", ".join(_선행_고리(card, 카드들, 시나리오, 주소, e)[0] for e in card["선행"]) or "—"
    return ('<p class="crumb"><a href="/">%s</a> / <a href="board.html">%s</a> / %s</p>'
            '<div class="badges">%s</div><h1>%s</h1><p class="sub">%s · 시나리오 %s · 선행 %s</p><div class="btns">%s</div>'
            % (esc(화면이름["home"]), esc(프로젝트), esc(꼬리), 배지, esc(card["title"]), esc(stem), 시, 선, 버튼))


def _요약_html(card, cards, h, 중앙값):
    esc = html.escape
    분 = actual_minutes(card)
    예상 = size_estimate(cards, card)["분"]
    늦음 = 예상 is not None and 분 > 예상
    if 예상 is None:
        차이 = "예상 —"
    else:
        퍼센트 = int(abs(분 - 예상) / 예상 * 100 + 0.5) if 예상 else 0
        차이 = "예상 %s · %d%% %s" % (_분글(예상), 퍼센트, "늦음" if 늦음 else "빠름") if 분 != 예상 else "예상 %s · 같음" % _분글(예상)
    비용 = sum(v["cost"] for v in h["단계"].values())
    중비 = 중앙값.get("비용")
    반려 = [n for n, x in (h.get("판정수") or {}).items() if x["반려"]]
    받음 = sorted(set(h.get("이어받은 단계") or []))
    칸 = [("걸린 시간", _분글(분), 차이, " risk" if 늦음 else ""),
         ("비용", "$%.2f" % 비용 if 비용 else "—", "같은 크기 중앙값 " + ("$%.2f" % 중비 if 중비 else "—"), ""),
         ("반려", str(sum(h["판정수"][n]["반려"] for n in 반려)) if h.get("판정수") else "0", ", ".join(_단계_글(n) for n in 반려) or "없음", ""),
         ("이어받기", str(h["이어받기"]), ", ".join(_단계_글(n) for n in 받음) or "없음", "")]
    return '<section class="tiles" aria-label="요약">%s</section>' % "".join(
        '<div class="tile%s"><span class="lb">%s</span><span class="v">%s</span><span class="sub">%s</span></div>' % (c, esc(a), esc(v), esc(d))
        for a, v, d, c in 칸)


def _흐름_html(h, 중앙값):
    esc = html.escape
    단계 = h["단계"]
    중 = 중앙값.get("단계") or {}
    기준 = max([v["ms"] for v in 단계.values()] + list(중.values()) + [0])
    칸 = []
    for n in sorted(단계):
        v = 단계[n]
        퍼 = lambda ms: int(ms / 기준 * 100 + 0.5) if 기준 else 0          # noqa: E731
        눈금 = '<i class="md" style="left:%d%%"></i>' % 퍼(중[n]) if n in 중 else ""
        반려 = bool((h.get("판정수") or {}).get(n, {}).get("반려"))
        칸.append('<div class="stg%s"><div class="hd"><span class="no">%s</span><span class="nm">%s</span></div>'
                 '<div class="trk"><i class="bar" style="width:%d%%"></i>%s</div><span class="tm">%s</span><span class="memo">%s</span></div>'
                 % (" rej" if 반려 else "", esc(n), esc(STAGE_NAME.get(n, "")), 퍼(v["ms"]), 눈금,
                    _걸린(v["ms"]) if v["ms"] else "—", esc(_메모(n, v, h))))
    return ('<section class="panel flow" aria-label="단계 흐름"><div class="ph"><h2>단계 흐름</h2>'
            '<span class="hint">막대 = 걸린 시간 · 눈금 = 같은 크기 카드의 중앙값</span></div>%s</section>'
            % ('<div class="rail">%s</div>' % "".join(칸) if 칸 else '<p class="more">실행 기록 없음</p>'))


def _판정_html(card, 검토):
    esc = html.escape
    줄 = []
    for r in (검토 or {}).get("판정", []):
        이름 = "%s %s" % (r["단계"], r["판정"])
        if r["오류"]:
            줄.append('<div class="fnd"><span class="badge b-gray">%s</span><span class="tx">%s</span><code class="wh">%s</code></div>'
                    % (esc(r["단계"] + " 읽지 못함"), esc(r["오류"]), esc(r["이름"])))
        for x in r["지적"]:
            줄.append('<div class="fnd"><span class="badge b-%s">%s</span><span class="tx">%s</span><code class="wh">%s</code></div>'
                    % ("orange" if r["판정"] == "반려" else "teal", esc(이름), esc(x["what"]), esc(x["where"])))
        if not r["오류"] and not r["지적"]:
            줄.append('<div class="fnd"><span class="badge b-%s">%s</span><span class="tx">지적 없음</span></div>'
                    % ("orange" if r["판정"] == "반려" else "teal", esc(이름)))
    줄 += ['<div class="fnd"><span class="badge b-gray">%s</span><span class="tx">%s</span><code class="wh">%s</code></div>'
          % (esc(_종류_글("선택 사항")), esc(x["원문"]), esc(x["출처"])) for x in (검토 or {}).get("항목", []) if x["종류"] == "선택 사항"]
    if not 줄:
        줄 = ['<div class="more">판정 기록 없음%s</div>' % (" — 실행이 끝나 소각된 뒤에 읽힌다" if card["상태"] == "진행" else "")]
    return '<section class="panel judge"><div class="ph"><h2>판정과 지적</h2></div><div class="flist">%s</div></section>' % "".join(줄)


def _사람_html(card, 검토):
    esc = html.escape
    항목 = (검토 or {}).get("항목", [])
    수 = lambda 종류: sum(1 for x in 항목 if x["종류"] == 종류)          # noqa: E731
    줄 = ["<div class=\"fnd\">%s</div>" % esc(x["원문"]) for x in 항목 if x["종류"] == "사람 결정 미결"] or ["<div class=\"fnd\">%s 없음</div>" % esc(_종류_글("사람 결정 미결"))]
    줄.append('<div class="fnd">%s %d · %s %d → <a href="improve?c=%s">%s</a></div>'
             % (_종류_글("이연 후보"), 수("이연 후보"), _종류_글("선택 사항"), 수("선택 사항"),
                urllib.parse.quote(os.path.splitext(card["file"])[0], safe=""), esc(화면이름["improve"])))
    return '<section class="panel human"><div class="ph"><h2>사람이 볼 것</h2></div><div class="flist">%s</div></section>' % "".join(줄)


# ── 점과 선 지도 (ISSUE-ui-15-graph-2026-11-24) ──
# 점 = 시나리오·카드·문서·기능, 선 = 선행·요소·기능·인용·언급. 계산은 모두 여기(서버)서 하고, 화면 안 스크립트는 배치·끌기·확대·누름만 맡는다.
_점_종류 = ("시나리오", "카드", "문서", "기능")
_선_글 = {"선행": "선행", "요소": "시나리오·요소", "기능": "기능", "인용": "문서 인용", "언급": "다른 시나리오 언급"}
_색_기준 = ("상태", "시나리오", "기기")
_지도_경로 = re.compile(r"docs/[A-Za-z]+/[^\s`)|,\]\"'<>]+")
_지도_번호 = re.compile(r"(?<![A-Za-z0-9_])SC-(\d{2})(?![0-9])")
_지도_색들 = 8


def _깊이(x):
    """주변 깊이 — 1~4 로 묶고 숫자가 아니면 2."""
    try:
        return max(1, min(4, int(x)))
    except (TypeError, ValueError):
        return 2


def _지도_본문(project, s):
    try:
        with open(os.path.join(project, "docs", "plan", s["file"]), encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def 연관_자료(project, cards, scenarios, 기능맵):
    """프로젝트 하나의 점과 선 `{"점": {ID: {...}}, "선": [{"앞", "뒤", "종류", ("요소")}]}`. 파일을 고치지 않고 예외를 내지 않는다."""
    점, 선, 봄 = {}, [], set()

    def 더하기(id_, 종류, 이름, 상태="", 시나리오="", 기기="", 고리=""):
        점.setdefault(id_, {"종류": 종류, "이름": 이름, "상태": 상태, "시나리오": 시나리오, "기기": 기기, "고리": 고리})

    def 잇기(앞, 뒤, 종류, 요소=None):
        if 앞 == 뒤 or 앞 not in 점 or 뒤 not in 점 or (앞, 뒤, 종류) in 봄:
            return
        봄.add((앞, 뒤, 종류))
        선.append({"앞": 앞, "뒤": 뒤, "종류": 종류, **({"요소": list(요소 or [])} if 종류 == "요소" else {})})
    for s in scenarios:
        더하기("시나리오:" + s["번호"], "시나리오", "%s %s" % (s["번호"], s["오류"] or _취소선_뺌(s["이름"])), s["상태"], s["번호"], "", "plan/" + urllib.parse.quote(s["file"]))
    for c in cards:
        더하기("카드:" + c["file"], "카드", _취소선_뺌(c["title"]), c["상태"], c["시나리오"], c["기기"], urllib.parse.quote(c["file"]))
    for 폴더 in ("analysis", "requirements"):
        for 위, 밑, 파일 in os.walk(os.path.join(project, "docs", 폴더)):
            밑.sort()
            for f in sorted(파일):
                if f.endswith((".md", ".txt")):
                    rel = os.path.relpath(os.path.join(위, f), project).replace(os.sep, "/")
                    더하기("문서:" + rel, "문서", rel)
    for fid, row in 기능맵.get("줄", {}).items():
        더하기("기능:" + fid, "기능", "%s %s" % (fid, row["칸"][1]), row["칸"][2])
    try:
        for p in precedence(project, cards, scenarios):
            for a in p["화살표"]:
                잇기("카드:" + a["앞"], "카드:" + a["뒤"], "선행")
    except Exception:                                 # noqa: BLE001 — 선행 그림이 못 읽혀도 나머지 선은 그린다
        pass
    for c in cards:
        for v in c["선행"]:
            잇기("카드:%s.md" % v, "카드:" + c["file"], "선행")
        if c["시나리오"]:
            해당 = "시나리오:" + c["시나리오"]
            if 해당 in 점:
                if (해당, "카드:" + c["file"], "요소") not in 봄:
                    잇기(해당, "카드:" + c["file"], "요소", c["요소"])
        for f in c["기능"]:
            잇기("카드:" + c["file"], "기능:" + f, "기능")
    for s in scenarios:
        if s["오류"]:
            continue
        나 = "시나리오:" + s["번호"]
        본문 = _지도_본문(project, s)
        for m in _지도_경로.finditer(본문):
            경로 = m.group(0).rstrip(".:;")
            조각 = 경로.split("/")
            이름 = 조각[-1]
            if not 이름.endswith((".md", ".txt")):
                continue
            if 조각[1] in ("analysis", "requirements"):
                잇기(나, "문서:" + 경로, "인용")
            elif len(조각) == 3 and 조각[1] == "backlog":
                잇기(나, "카드:" + 이름, "인용")
            elif (len(조각) == 3 and 조각[1] == "plan" and not _시나리오이름.match(이름)
                  and not 이름.startswith(("끝보고-", "사람손-")) and os.path.isfile(os.path.join(project, 경로))):
                더하기("문서:" + 경로, "문서", 경로)
                잇기(나, "문서:" + 경로, "인용")
        for m in _지도_번호.finditer(본문):
            잇기(나, "시나리오:SC-" + m.group(1), "언급")
    return {"점": 점, "선": 선}


def _보이는(자료, 거르개, 가운데=None):
    """거르개(`점종류`·`선종류`·`완료숨김`, 없으면 모두)를 건 점과 선. 가운데 점은 걸려도 남는다."""
    g = 거르개 or {}
    종류들, 선들, 숨김 = g.get("점종류"), g.get("선종류"), g.get("완료숨김")

    def 남나(i):
        p = 자료["점"][i]
        return i == 가운데 or not ((종류들 is not None and p["종류"] not in 종류들) or (숨김 and p["종류"] == "카드" and p["상태"] == "완료"))
    점 = {i: p for i, p in 자료["점"].items() if 남나(i)}
    return {"점": 점, "선": [e for e in 자료["선"] if (선들 is None or e["종류"] in 선들) and e["앞"] in 점 and e["뒤"] in 점]}


def 주변(자료, 가운데, 깊이, 거르개=None):
    """가운데에서 선(방향 무시)을 깊이 번 이하로 따라 닿는 점과 그 사이 선. 거르개가 먼저 걸리고 남은 선으로만 이웃을 센다."""
    if 가운데 not in 자료["점"]:
        return {"점": {}, "선": []}
    보임 = _보이는(자료, 거르개, 가운데)
    이웃 = {}
    for e in 보임["선"]:
        이웃.setdefault(e["앞"], []).append(e["뒤"])
        이웃.setdefault(e["뒤"], []).append(e["앞"])
    닿음, 끝 = {가운데}, [가운데]
    for _ in range(_깊이(깊이)):
        다음 = []
        for i in 끝:
            for j in 이웃.get(i, ()):
                if j not in 닿음:
                    닿음.add(j)
                    다음.append(j)
        끝 = 다음
    return {"점": {i: p for i, p in 보임["점"].items() if i in 닿음}, "선": [e for e in 보임["선"] if e["앞"] in 닿음 and e["뒤"] in 닿음]}


def _지도_주소(기본, 인자):
    """`기본?키=값&…` — 값만 퍼센트 부호로, 키는 그대로. 이 화면의 고리는 모두 여기서 만든다."""
    return 기본 + ("?" + "&".join("%s=%s" % (k, urllib.parse.quote(str(v), safe="")) for k, v in 인자) if 인자 else "")


def _화살촉(x1, y1, x2, y2, r):
    """(x1,y1)→(x2,y2) 선의 뒤 끝 동그라미(반지름 r) 가장자리에 놓는 작은 삼각형 `points` 값."""
    dx, dy = x2 - x1, y2 - y1
    d = (dx * dx + dy * dy) ** 0.5 or 1.0
    ux, uy = dx / d, dy / d
    tx, ty = x2 - ux * r, y2 - uy * r
    bx, by = tx - ux * 8, ty - uy * 8
    return "%.1f,%.1f %.1f,%.1f %.1f,%.1f" % (tx, ty, bx - uy * 3.5, by + ux * 3.5, bx + uy * 3.5, by - ux * 3.5)


def _원형(ids, cx, cy, R):
    out = {}
    for k, i in enumerate(ids):
        a = 2 * math.pi * k / max(len(ids), 1) - math.pi / 2
        out[i] = (cx + R * math.cos(a), cy + R * math.sin(a))
    return out


_지도_CSS = """.rmsvg { width:100%; border:1px solid var(--line); border-radius:8px; background:var(--panel); }
.rm-c0 { fill:var(--blue); } .rm-c1 { fill:var(--orange); } .rm-c2 { fill:var(--teal); } .rm-c3 { fill:var(--gray); }
.rm-st-teal { fill:var(--teal); } .rm-st-blue { fill:var(--blue); } .rm-st-ochre { fill:var(--ochre); } .rm-st-orange { fill:var(--orange); } .rm-st-gray { fill:var(--gray); }
.rm-c4 { fill:var(--pj-0); } .rm-c5 { fill:var(--ochre); } .rm-c6 { fill:var(--pj-1); } .rm-c7 { fill:var(--pj-3); }
.rmsvg line { stroke:var(--gray); stroke-width:1; opacity:.75; } .rmsvg line.d { stroke-dasharray:4 3; }
.rmsvg polygon.ah { fill:var(--gray); } .rmsvg circle { stroke:var(--fg); stroke-width:.6; }
.rmsvg .sel circle, .rmsvg .ctr circle { stroke-width:3; } .rmsvg text { font-size:10px; fill:var(--fg); pointer-events:none; }
.rmsvg g.pt { cursor:pointer; }"""


def _작은_지도(자료, 가운데, 주소):
    """카드·시나리오 상세 아래 절 — 가운데 점과 깊이 1 이웃을 원형으로 놓은 스크립트 없는 그림과 지도 고리."""
    esc = html.escape
    부분 = 주변(자료, 가운데, 1)
    if 가운데 not in 부분["점"]:
        return ""
    남 = sorted(i for i in 부분["점"] if i != 가운데)
    자리 = {가운데: (180.0, 130.0), **_원형(남, 180.0, 130.0, 95.0)}
    반지름 = lambda i: 9 if 부분["점"][i]["종류"] == "시나리오" else 6          # noqa: E731
    색 = {"시나리오": 1, "카드": 0, "문서": 3, "기능": 2}
    줄 = []
    for e in 부분["선"]:
        (x1, y1), (x2, y2) = 자리[e["앞"]], 자리[e["뒤"]]
        줄.append('<line class="%s" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>' % ("d" if e["종류"] in ("인용", "언급") else "", x1, y1, x2, y2))
        if e["종류"] == "선행":
            줄.append('<polygon class="ah" points="%s"/>' % _화살촉(x1, y1, x2, y2, 반지름(e["뒤"])))
    for i in [가운데] + 남:
        p, (x, y) = 부분["점"][i], 자리[i]
        줄.append('<g class="%s"><title>%s</title><circle cx="%.1f" cy="%.1f" r="%d" class="rm-c%d"/><text x="%.1f" y="%.1f" text-anchor="middle">%s</text></g>'
                % ("ctr" if i == 가운데 else "pt", esc(p["이름"]), x, y, 반지름(i), 색[p["종류"]], x, y + 반지름(i) + 11, esc(p["이름"][:8])))
    고리 = _지도_주소(주소 + "graph", [("범위", "주변"), ("점", 가운데), ("깊이", 1)])
    return ('<h2>%s</h2><style>%s</style><svg class="rmsvg" viewBox="0 0 360 260" role="img" aria-label="%s">%s</svg>%s<p><a href="%s">%s 열기</a></p>'
            % (esc(화면이름["graph"]), _지도_CSS, esc(화면이름["graph"], quote=True), "".join(줄),
               "" if 남 else '<p class="meta">이어진 점 없음</p>', esc(고리, quote=True), esc(화면이름["graph"])))


def _지도_절(item, cards, scenarios, 가운데):
    try:
        return _작은_지도(연관_자료(item["path"], cards, scenarios, read_featuremap(item["path"])), 가운데, _주소(item["name"]))
    except Exception:                                  # noqa: BLE001 — 작은 지도가 막혀도 상세 화면은 그대로 나온다
        return ""


def _지도_상태(qs):
    def 하나(k):
        return (qs.get(k) or [""])[0]
    return {"범위": "주변" if 하나("범위") == "주변" else "전체", "점": 하나("점"), "깊이": _깊이(하나("깊이") or None), "고름": 하나("고름"),
            "거름": 하나("거름") == "1", "점종류": list(qs.get("점종류", [])), "선종류": list(qs.get("선종류", [])),
            "색": 하나("색") if 하나("색") in _색_기준 else "상태", "완료숨김": 하나("완료숨김") == "1"}


def _지도_인자(상태, 바꿈=None):
    s = {**상태, **(바꿈 or {})}
    out = []
    if s["범위"] == "주변":
        out += [("범위", "주변"), ("점", s["점"]), ("깊이", s["깊이"])]
    if s["고름"]:
        out.append(("고름", s["고름"]))
    if s["거름"]:
        out += [("거름", "1")] + [("점종류", x) for x in s["점종류"]] + [("선종류", x) for x in s["선종류"]]
    if s["색"] != "상태":
        out.append(("색", s["색"]))
    if s["완료숨김"]:
        out.append(("완료숨김", "1"))
    return out


def _지도_색값(p, 기준):
    if 기준 == "시나리오":
        return p["시나리오"] or "시나리오 없음"
    if 기준 == "기기":
        return _기기_글(p["기기"] or "기기 모름") if p["종류"] == "카드" else ""
    return p["상태"]


_지도_스크립트 = r"""(function () {
  var MAX_ITER = 300, STOP_MOVE = 0.5, W = 800, H = 600, REP = 10, REST = 40, SPRING = 0.05, PULL = 0.02, DAMP = 0.8;
  var data = JSON.parse(document.getElementById("relmap-data").textContent);
  var svg = document.getElementById("relmap-svg"), status = document.getElementById("relmap-status");
  var nodes = data.nodes, links = data.links, n = nodes.length, NS = "http://www.w3.org/2000/svg";
  if (!n) { status.textContent = "자리 잡음 · 0 ms"; return; }
  var gs = svg.querySelectorAll("g.pt"), ls = svg.querySelectorAll("line"), arrows = {};
  Array.prototype.forEach.call(svg.querySelectorAll("polygon.ah"), function (h) { arrows[h.getAttribute("data-l")] = h; });
  var px = [], py = [], vx = [], vy = [], i, j, k;
  for (i = 0; i < n; i++) { px[i] = nodes[i].x; py[i] = nodes[i].y; vx[i] = 0; vy[i] = 0; }
  if (n <= 60) {
    for (i = 0; i < n; i++) {
      var t = document.createElementNS(NS, "text");
      t.setAttribute("x", 0); t.setAttribute("y", nodes[i].r + 11); t.setAttribute("text-anchor", "middle");
      t.textContent = nodes[i].n.slice(0, 10); gs[i].appendChild(t);
    }
  }
  function draw() {
    for (i = 0; i < n; i++) gs[i].setAttribute("transform", "translate(" + px[i].toFixed(1) + " " + py[i].toFixed(1) + ")");
    for (k = 0; k < links.length; k++) {
      var a = links[k][0], b = links[k][1], l = ls[k];
      l.setAttribute("x1", px[a].toFixed(1)); l.setAttribute("y1", py[a].toFixed(1));
      l.setAttribute("x2", px[b].toFixed(1)); l.setAttribute("y2", py[b].toFixed(1));
      var h = arrows[k];
      if (h) {
        var dx = px[b] - px[a], dy = py[b] - py[a], d = Math.sqrt(dx * dx + dy * dy) || 1, ux = dx / d, uy = dy / d;
        var tx = px[b] - ux * nodes[b].r, ty = py[b] - uy * nodes[b].r, bx = tx - ux * 8, by = ty - uy * 8;
        h.setAttribute("points", tx.toFixed(1) + "," + ty.toFixed(1) + " " + (bx - uy * 3.5).toFixed(1) + "," + (by + ux * 3.5).toFixed(1) +
          " " + (bx + uy * 3.5).toFixed(1) + "," + (by - ux * 3.5).toFixed(1));
      }
    }
  }
  var fx = [], fy = [], iter = 0, start = performance.now(), held = -1;
  function step() {
    var dx, dy, d2, d, f, m, maxMove = 0;
    for (i = 0; i < n; i++) { fx[i] = (W / 2 - px[i]) * PULL; fy[i] = (H / 2 - py[i]) * PULL; }
    for (i = 0; i < n; i++) for (j = i + 1; j < n; j++) {
      dx = px[i] - px[j]; dy = py[i] - py[j]; d2 = dx * dx + dy * dy;
      if (d2 < 1) { dx = 1; dy = 0; d2 = 1; }
      f = REP / d2; fx[i] += dx * f; fy[i] += dy * f; fx[j] -= dx * f; fy[j] -= dy * f;
    }
    for (k = 0; k < links.length; k++) {
      var a = links[k][0], b = links[k][1];
      dx = px[b] - px[a]; dy = py[b] - py[a]; d = Math.sqrt(dx * dx + dy * dy) || 1; f = (d - REST) * SPRING / d;
      fx[a] += dx * f; fy[a] += dy * f; fx[b] -= dx * f; fy[b] -= dy * f;
    }
    for (i = 0; i < n; i++) {
      if (i === held) continue;
      vx[i] = (vx[i] + fx[i]) * DAMP; vy[i] = (vy[i] + fy[i]) * DAMP;
      m = Math.sqrt(vx[i] * vx[i] + vy[i] * vy[i]);
      if (m > 12) { vx[i] *= 12 / m; vy[i] *= 12 / m; m = 12; }
      px[i] = Math.min(W - 20, Math.max(20, px[i] + vx[i])); py[i] = Math.min(H - 20, Math.max(20, py[i] + vy[i])); if (m > maxMove) maxMove = m;
    }
    iter++;
    return maxMove;
  }
  function settle() {
    var moved = 1, c = 0;
    while (c++ < 10 && iter < MAX_ITER && moved >= STOP_MOVE) moved = step();
    draw();
    if (iter < MAX_ITER && moved >= STOP_MOVE) requestAnimationFrame(settle);
    else status.textContent = "자리 잡음 · " + Math.round(performance.now() - start) + " ms";
  }
  draw(); requestAnimationFrame(settle);
  var anchors = {};
  Array.prototype.forEach.call(document.querySelectorAll("a[data-pid]"), function (a) { anchors[a.getAttribute("data-pid")] = a; });
  function go(idx, center) { var a = anchors[nodes[idx].id]; if (a) location.href = a.getAttribute(center ? "data-ctr" : "href"); }
  var vb = { x: 0, y: 0, w: W, h: H }, pan = null, moved = 0, timer = null, lastHit = -1;
  function setView() { svg.setAttribute("viewBox", vb.x + " " + vb.y + " " + vb.w + " " + vb.h); }
  function at(e) { var r = svg.getBoundingClientRect(); return [vb.x + (e.clientX - r.left) * vb.w / r.width, vb.y + (e.clientY - r.top) * vb.h / r.height]; }
  function hit(e) { var g = e.target.closest ? e.target.closest("g.pt") : null; return g ? Array.prototype.indexOf.call(gs, g) : -1; }
  svg.addEventListener("pointerdown", function (e) {
    moved = 0; held = hit(e); pan = [e.clientX, e.clientY]; svg.setPointerCapture(e.pointerId);
  });
  svg.addEventListener("pointermove", function (e) {
    if (!pan) return;
    var r = svg.getBoundingClientRect(), dx = e.clientX - pan[0], dy = e.clientY - pan[1];
    moved += Math.abs(dx) + Math.abs(dy); pan = [e.clientX, e.clientY];
    if (held >= 0) { var p = at(e); px[held] = p[0]; py[held] = p[1]; vx[held] = 0; vy[held] = 0; draw(); }
    else { vb.x -= dx * vb.w / r.width; vb.y -= dy * vb.h / r.height; setView(); }
  });
  svg.addEventListener("pointerup", function (e) {
    var h = held; pan = null; held = -1;
    if (h < 0 || moved > 4) return;
    if (timer && lastHit === h) { clearTimeout(timer); timer = null; go(h, true); return; }
    lastHit = h; timer = setTimeout(function () { timer = null; go(h, false); }, 260);
  });
  svg.addEventListener("wheel", function (e) {
    e.preventDefault();
    var p = at(e), z = e.deltaY < 0 ? 0.9 : 1.1, w = Math.min(Math.max(vb.w * z, 80), 6000), s = w / vb.w;
    vb.x = p[0] - (p[0] - vb.x) * s; vb.y = p[1] - (p[1] - vb.y) * s; vb.w = w; vb.h = vb.h * s; setView();
  }, { passive: false });
})();"""


def render_relmap(item, items, 자료, qs):
    """`/p/<이름>/graph` — 범위 탭·거르개·그림·점 목록·고른 점 상자. 범위·깊이·가운데·고름·거르개·색은 모두 주소 인자이고 여기서 그대로 그린다."""
    esc = html.escape
    상태 = _지도_상태(qs)
    기본 = _주소(item["name"]) + "graph"
    거르개 = {"점종류": set(상태["점종류"]) if 상태["거름"] else None, "선종류": set(상태["선종류"]) if 상태["거름"] else None, "완료숨김": 상태["완료숨김"]}
    없음글 = ""
    if 상태["범위"] == "주변":
        보임 = 주변(자료, 상태["점"], 상태["깊이"], 거르개)
        if 상태["점"] not in 자료["점"]:
            없음글 = "그 점이 없다 — %s" % 상태["점"]
    else:
        보임 = _보이는(자료, 거르개)
        if not 자료["점"]:
            없음글 = "점이 없다 — 카드·시나리오·문서가 하나도 없다"
    ids = sorted(보임["점"], key=lambda i: (_점_종류.index(보임["점"][i]["종류"]), i))
    주소_ = lambda 바꿈: esc(_지도_주소(기본, _지도_인자(상태, 바꿈)), quote=True)      # noqa: E731

    # 탭
    가운데이름 = 자료["점"][상태["점"]]["이름"] if 상태["점"] in 자료["점"] else 상태["점"]
    탭 = ['<a href="%s"%s>프로젝트 전체</a>' % (주소_({"범위": "전체"}), ' aria-current="page"' if 상태["범위"] == "전체" else "")]
    if 상태["범위"] == "주변":
        탭.append('<a href="%s" aria-current="page">%s 주변</a>' % (주소_({}), esc(가운데이름)))
        탭 += ['<a class="dp" href="%s"%s>깊이 %d</a>' % (주소_({"깊이": n}), ' aria-current="true"' if n == 상태["깊이"] else "", n) for n in range(1, 5)]

    # 왼쪽 거르개
    def 켬(값, 종류들):
        return not 상태["거름"] or 값 in 종류들
    기능있음 = any(p["종류"] == "기능" for p in 자료["점"].values())
    점수 = collections.Counter(p["종류"] for p in 자료["점"].values())
    선수 = collections.Counter(e["종류"] for e in 자료["선"])
    점칸 = []
    for k in _점_종류:
        글 = "기능맵 기능" if k == "기능" else k
        if k == "기능" and not 기능있음:
            점칸.append('<span class="meta">기능맵 기능 (없음)</span>')
        else:
            점칸.append('<label><input type="checkbox" name="점종류" value="%s"%s> %s %d</label>' % (k, " checked" if 켬(k, 상태["점종류"]) else "", esc(글), 점수[k]))
    선칸 = ['<label><input type="checkbox" name="선종류" value="%s"%s> %s %d</label>' % (k, " checked" if 켬(k, 상태["선종류"]) else "", esc(_선_글[k]), 선수[k])
          for k in _선_글]
    색칸 = ['<label><input type="radio" name="색" value="%s"%s> %s</label>' % (k, " checked" if 상태["색"] == k else "", k) for k in _색_기준]
    숨은 = "".join('<input type="hidden" name="%s" value="%s">' % (k, esc(str(v), quote=True)) for k, v in
                  [("범위", "주변"), ("점", 상태["점"]), ("깊이", 상태["깊이"])] * (상태["범위"] == "주변") + [("고름", 상태["고름"])] * bool(상태["고름"]))
    왼쪽 = ('<form class="rm-left" method="get" action="%s">%s<input type="hidden" name="거름" value="1">'
          '<fieldset><legend>점 종류</legend>%s</fieldset><fieldset><legend>선 종류</legend>%s</fieldset>'
          '<fieldset><legend>색 기준</legend>%s</fieldset><label><input type="checkbox" name="완료숨김" value="1"%s> 완료 카드 숨기기</label>'
          '<button type="submit">적용</button></form>'
          % (esc(기본, quote=True), 숨은, "".join(점칸), "".join(선칸), "".join(색칸), " checked" if 상태["완료숨김"] else ""))

    # 가운데 그림
    값들 = {i: _지도_색값(보임["점"][i], 상태["색"]) for i in ids}
    색순서 = {v: k % _지도_색들 for k, v in enumerate(sorted({v for v in 값들.values() if v}))}
    자리 = _원형(ids, 400.0, 300.0, 230.0)
    반지름 = lambda i: 9 if 보임["점"][i]["종류"] == "시나리오" else 5 if 보임["점"][i]["종류"] == "문서" else 6    # noqa: E731
    번호 = {i: k for k, i in enumerate(ids)}
    선그림, 화살 = [], []
    for k, e in enumerate(보임["선"]):
        (x1, y1), (x2, y2) = 자리[e["앞"]], 자리[e["뒤"]]
        선그림.append('<line class="%s" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>' % ("d" if e["종류"] in ("인용", "언급") else "", x1, y1, x2, y2))
        if e["종류"] == "선행":
            화살.append('<polygon class="ah" data-l="%d" points="%s"/>' % (k, _화살촉(x1, y1, x2, y2, 반지름(e["뒤"]))))
    점색 = (lambda v: "rm-st-" + _배지_색(v)) if 상태["색"] == "상태" else (lambda v: "rm-c%d" % 색순서.get(v, 3))   # noqa: E731
    점그림 = []
    for i in ids:
        p, (x, y) = 보임["점"][i], 자리[i]
        cls = "pt k-%s%s%s" % (p["종류"], " sel" if i == 상태["고름"] else "", " ctr" if 상태["범위"] == "주변" and i == 상태["점"] else "")
        점그림.append('<g class="%s" data-id="%s" data-색="%s" transform="translate(%.1f %.1f)"><title>%s</title><circle r="%d" class="%s"/></g>'
                    % (cls, esc(i, quote=True), esc(값들[i], quote=True), x, y, esc(p["이름"]), 반지름(i), 점색(값들[i])))
    데이터 = json.dumps({"nodes": [{"id": i, "n": 보임["점"][i]["이름"], "x": round(자리[i][0], 1), "y": round(자리[i][1], 1), "r": 반지름(i)} for i in ids],
                       "links": [[번호[e["앞"]], 번호[e["뒤"]], 1 if e["종류"] == "선행" else 0] for e in 보임["선"]]},
                      ensure_ascii=False).replace("<", "\\u003c")
    범례 = "".join('<span class="lgd"><svg width="10" height="10"><circle cx="5" cy="5" r="4" class="%s"/></svg> %s</span>' % (점색(v), esc(v))
                 for v in 색순서)
    그림 = ('<p class="rm-cnt">보이는 점 %d · 선 %d (프로젝트 전체 점 %d · 선 %d)</p><p class="meta" id="relmap-status">자리 잡는 중</p>%s'
          '<svg id="relmap-svg" class="rmsvg" viewBox="0 0 800 600" role="img" aria-label="%s" style="height:560px;touch-action:none">%s%s%s</svg>'
          '<div class="rm-lg">%s</div>'
          % (len(보임["점"]), len(보임["선"]), len(자료["점"]), len(자료["선"]), '<p class="meta">%s</p>' % esc(없음글) if 없음글 else "",
             esc(화면이름["graph"], quote=True), "".join(선그림), "".join(화살), "".join(점그림), 범례))

    # 점 목록 — 키보드로도 점을 고를 수 있다
    목록 = "".join('<li><a href="%s" data-pid="%s" data-ctr="%s">%s · %s</a></li>'
                 % (주소_({"고름": i}), esc(i, quote=True), 주소_({"범위": "주변", "점": i, "깊이": 1, "고름": i}), esc(보임["점"][i]["종류"]), esc(보임["점"][i]["이름"]))
                 for i in ids)

    # 오른쪽 — 고른 점
    고름 = 상태["고름"]
    if not 고름:
        오른쪽 = '<p class="meta">점을 고르면 들어오는 선·나가는 선이 보인다</p>'
    elif 고름 not in 자료["점"]:
        오른쪽 = '<p class="meta">그 점이 없다 — %s</p>' % esc(고름)
    else:
        p = 자료["점"][고름]

        def 줄(e, 방향):
            다른 = e["뒤"] if 방향 == "→" else e["앞"]
            return ('<li>%s %s <a href="%s">%s</a>%s</li>'
                    % (esc(_선_글[e["종류"]]), 방향, 주소_({"고름": 다른}), esc(자료["점"][다른]["이름"]), esc(" · " + ", ".join(e["요소"])) if e.get("요소") else ""))
        들 = [줄(e, "←") for e in 자료["선"] if e["뒤"] == 고름]
        나 = [줄(e, "→") for e in 자료["선"] if e["앞"] == 고름]
        열기 = ('<p><a href="%s">카드 상세 열기</a></p>' % esc(_주소(item["name"]) + p["고리"], quote=True)) if p["고리"] else \
            '<p class="meta">%s</p>' % esc(고름[len(p["종류"]) + 1:])
        오른쪽 = ('<h3>%s</h3><p class="meta">%s · %s</p><h4>들어오는 선 %d</h4><ul>%s</ul><h4>나가는 선 %d</h4><ul>%s</ul>%s<p><a href="%s">이 점을 가운데로</a></p>'
               % (esc(p["이름"]), esc(p["종류"]), esc(p["상태"] or "상태 없음"), len(들), "".join(들) or "<li>없음</li>", len(나), "".join(나) or "<li>없음</li>",
                  열기, 주소_({"범위": "주변", "점": 고름, "깊이": 1, "고름": 고름})))
    본문 = ('<style>%s .rm { font-size:14px; } .rm h1 span { font-size:13px; font-weight:400; color:var(--dim); margin-left:8px; }'
          ' .rm-tabs { display:flex; flex-wrap:wrap; gap:4px; margin:0 0 12px; } .rm-tabs a { padding:4px 12px; border-radius:6px; }'
          ' .rm-tabs a[aria-current] { background:var(--soft); color:var(--fg); font-weight:600; }'
          ' .rm-grid { display:flex; gap:16px; align-items:flex-start; } .rm-left { flex:0 0 190px; display:flex; flex-direction:column; gap:8px; }'
          ' .rm-left fieldset { display:flex; flex-direction:column; gap:4px; border:1px solid var(--line); border-radius:6px; }'
          ' .rm-mid { flex:1; min-width:0; } .rm-right { flex:0 0 260px; } .plist { columns:2; font-size:13px; margin:12px 0; }'
          ' .lgd { margin-right:10px; font-size:12px; }</style>'
          '<div class="rm"><p class="crumb"><a href="/">%s</a> / <a href="%s">%s</a> / %s</p><h1>%s<span>%s</span></h1>%s<div class="rm-tabs">%s</div>'
          '<div class="rm-grid">%s<div class="rm-mid">%s<ul class="plist">%s</ul></div><aside class="rm-right">%s</aside></div></div>'
          '<script type="application/json" id="relmap-data">%s</script><script>%s</script>'
          % (_지도_CSS, esc(화면이름["home"]), esc(_주소(item["name"]), quote=True), esc(item["name"]), esc(화면이름["graph"]), esc(화면이름["graph"]), esc(item["name"]),
             _프로젝트_탭(item["name"], "graph"), "".join(탭), 왼쪽, 그림, 목록, 오른쪽, 데이터, _지도_스크립트))
    return _기능_틀() % {"name": esc("%s · %s" % (화면이름["graph"], item["name"])), "body": 본문}


class _본문_그림(Exception):
    """카드 상세 본문 절을 이미 그렸다는 표시 — 아래 `except` 가 받아 넘어간다."""


def render_card_detail(item, card, cards, scenarios, history, 머리말=None, 중앙값=None, 검토=None, 웹="", 본문=None):
    """카드 상세 HTML 문자열. 예외 없이 문자열을 돌려준다. 값을 못 읽은 자리는 이유를 적고 나머지는 그대로 낸다.
    위쪽은 목업(머리·요약·단계 흐름·판정과 지적·사람이 볼 것), 아래쪽은 지금까지의 절이다. 새 인자가 비면 중앙값 없음·판정 기록 없음·원격 없음."""
    esc = html.escape
    path = os.path.join(item["path"], "docs", "backlog", card["file"])
    parts = []
    try:
        fm = 머리말 if 머리말 is not None else front_matter(path)
    except (OSError, UnicodeDecodeError):
        fm = {}
    중앙값 = 중앙값 or {"단계": {}, "비용": None, "표본": 0, "오류": ""}
    h = history
    위 = [_머리_html(card, h, fm, 웹, scenarios, cards, _주소(item["name"]), item["name"]), 승격_폼(item["name"], card), _요약_html(card, cards, h, 중앙값), _흐름_html(h, 중앙값),
         '<div class="cols">%s%s</div>' % (_판정_html(card, 검토), _사람_html(card, 검토))]
    if "출처" in card:
        parts.append("<p>출처: %s · 실행 기기 %s</p>" % (esc(card["출처"].replace("이 기계", "이 기기")), esc(_기기_글(card["도는 기계"]))))
    parts.append("<h2>머리말</h2>" + (_표_html(("키", "값"), list(fm.items())) if fm else "<p>머리말 없음</p>"))
    try:
        if isinstance(본문, tuple):                  # 거울에서 못 읽음 — 사본 글로 돌아가지 않는다(머리말은 원격, 본문은 사본인 화면을 만들지 않는다)
            parts.append("<h2>본문</h2><p>본문을 원격에서 읽지 못함 — %s</p>" % esc(본문[1]))
            raise _본문_그림
        if isinstance(본문, str):
            text = 본문
        else:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        try:
            shown = 본문_그리기(text)
        except Exception:                            # 그리기가 무엇으로 실패하든 원문이 보인다
            shown = "<pre>%s</pre>" % esc(text)
        parts.append("<h2>본문</h2>" + shown)
    except _본문_그림:
        pass
    except OSError:
        parts.append("<h2>본문</h2><p>본문은 이 기기에 없음 — 원격에만 있는 카드</p>")
    parts.append("<h2>단계별 실행 기록</h2>")
    if h.get("상태오류"):
        parts.append("<p>%s</p>" % esc(h["상태오류"]))
    if h["단계"]:
        parts.append(_표_html(("단계", "회차", "시간", "비용", "모델", "판정", "지적 수"), [
            (st, v["runs"] or "—", _걸린(v["ms"]) if v["ms"] else "—", "$%.4f" % v["cost"] if v["cost"] else "—",
             v["model"] or "—", v["판정"] or "—", v["지적"] or "—") for st, v in sorted(h["단계"].items())]))
    elif not h["못찾음"]:
        parts.append("<p>실행 기록 없음</p>")
    parts += ["<p>기록 못 찾음 — %s</p>" % esc(x) for x in h["못찾음"]]
    try:
        재인계 = int(fm.get("재인계", "0") or 0)
    except ValueError:
        재인계 = 0
    parts.append("<p>이어받기 %d · 재인계 %d</p>" % (h["이어받기"], 재인계))
    parts.append("<h2>커밋</h2>")
    parts.append("<p>%s</p>" % esc(h["오류"]) if h["오류"] else
                 _표_html(("때", "단계", "제목"), h["커밋"]) if h["커밋"] else "<p>커밋 없음</p>")
    parts.append(_시나리오_절(card, cards, scenarios, _주소(item["name"])))
    parts.append(_기능_절(card, read_featuremap(item["path"])))
    parts.append(_지도_절(item, cards, scenarios, "카드:" + card["file"]))
    return CARD_TEMPLATE % {"name": esc(card["title"]), "head": "\n".join(위), "body": "\n".join(parts)}


def _없는_카드(name):
    body = '<p>그런 카드 없음 — %s</p>' % html.escape(name)
    return FILE_TEMPLATE % {"name": "그런 카드 없음", "body": body}


# ── 지난 작업 검색 (ISSUE-sc02-e6-search-2026-11-20) ──
# 요청마다 git 과 파일을 읽는다 — 색인을 두지 않고 화면 단위 잠깐 저장(`_잠깐저장`)만 둔다. 카드와 커밋을 잇는 규칙은 `read_card_history` 와 같다.
def find_lines(text, 낱말):
    """낱말(글자 그대로, 대소문자 무시)이 든 줄마다 {"앞", "줄", "뒤"}."""
    lines = text.splitlines()
    needle = 낱말.lower()
    return [{"앞": lines[i - 1] if i else "", "줄": line, "뒤": lines[i + 1] if i + 1 < len(lines) else ""}
            for i, line in enumerate(lines) if needle in line.lower()]


def _날짜(s):
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d") == s
    except ValueError:
        return False


def _조건(qs):
    """쿼리 사전(`parse_qs` 모양)에서 (조건 사전, 까닭). 까닭이 비어 있지 않으면 찾지 않는다."""
    def g(k):
        return (qs.get(k) or [""])[0].strip()
    cond = {"낱말": g("q"), "시나리오": g("sc"), "기능": g("fn"), "기기": g("dev"), "시작": g("from"), "끝": g("to"),
            "반려": g("rej") == "1", "아래": None, "위": None, "프로젝트": g("p"), "종류": g("kind")}
    if cond["종류"] not in ("", "카드", "커밋", "인계"):
        return cond, "종류는 카드·커밋·인계"
    for k in ("시작", "끝"):
        if cond[k] and not _날짜(cond[k]):
            return cond, "날짜는 YYYY-MM-DD"
    if cond["시작"] and cond["끝"] and cond["시작"] > cond["끝"]:
        return cond, "기간의 시작이 끝보다 늦다"
    for k, key in (("아래", "cmin"), ("위", "cmax")):
        if g(key):
            try:
                v = float(g(key))
            except ValueError:
                v = -1.0
            if not 0 <= v < float("inf"):
                return cond, "비용 범위는 0 이상의 수"
            cond[k] = v
    return cond, ""


def _거르개_글(cond):
    out = []
    for k in ("시나리오", "기능", "기기"):
        if cond[k]:
            out.append("%s %s" % (k if k != "기능" else "기능 ID", cond[k]))
    if cond["시작"] or cond["끝"]:
        out.append("기간 %s~%s" % (cond["시작"], cond["끝"]))
    if cond["반려"]:
        out.append("반려 있음")
    if cond["아래"] is not None or cond["위"] is not None:
        out.append("비용 %s~%s 달러" % ("" if cond["아래"] is None else "%g" % cond["아래"],
                                     "" if cond["위"] is None else "%g" % cond["위"]))
    return out


_커밋_서식 = "\x1e" + _구분.join(["%H", "%ad", "%(trailers:key=Loop-Issue,valueonly)", "%(trailers:key=Loop-Card,valueonly)",
                                 "%(trailers:key=Loop-Verdict,valueonly)", "%B"])


def _커밋들(project):
    """[(해시, 날짜, 연결 이름들, 반려인가, 메시지 전체)] — 못 읽으면 예외."""
    r = _git_읽기(project, "log", "--all", "--date=format:%Y-%m-%d %H:%M", "--format=" + _커밋_서식)
    if r.returncode != 0:
        raise OSError("커밋 이력을 읽지 못했다")
    out = []
    for rec in r.stdout.split("\x1e")[1:]:
        f = rec.split(_구분, 5)
        if len(f) != 6:
            continue
        names = {x.strip().removesuffix(".md") for x in (f[2], f[3]) if x.strip()}
        out.append((f[0], f[1], names, f[4].strip() == "반려", f[5]))
    return out


def _소각_인계(project):
    """소각 커밋이 지운 인계 문서 {폴더 이름: 지워지기 전 글} — 같은 폴더가 여럿이면 가장 최근 것."""
    r = _git_읽기(project, "-c", "core.quotepath=false", "log", "--all", "--diff-filter=D", "-p", "--format=\x1e%H", "--", ".workflow/*/10-handoff.md")
    out = {}
    if r.returncode != 0:
        return out
    for rec in r.stdout.split("\x1e")[1:]:
        folder, body, hunk = None, [], False
        for line in rec.splitlines():
            m = re.match(r"diff --git a/\.workflow/([^/]+)/10-handoff\.md ", line)
            if m:
                if folder and folder not in out:
                    out[folder] = "\n".join(body)
                folder, body, hunk = m.group(1), [], False
            elif line.startswith("@@"):
                hunk = True
            elif hunk and line.startswith("-"):
                body.append(line[1:])
        if folder and folder not in out:
            out[folder] = "\n".join(body)
    return out


def _카드_쓸것(card, cond):
    """머리말만으로 거르는 조건(시나리오·기능 ID·기기·기간)을 채우는가."""
    if cond["시나리오"] and card["시나리오"] != cond["시나리오"]:
        return False
    if cond["기능"] and cond["기능"] not in card["기능"]:
        return False
    if cond["기기"] and card["도는 기계"] != cond["기기"]:
        return False
    if cond["시작"] or cond["끝"]:
        d = card["갱신"]
        if not _날짜(d) or (cond["시작"] and d < cond["시작"]) or (cond["끝"] and d > cond["끝"]):
            return False
    return True


def _프로젝트_찾기(item, cond):
    out = {"name": item["name"], "오류": [], "카드": []}
    backlog = os.path.join(item["path"], "docs", "backlog")
    try:
        if not os.path.isdir(backlog):
            raise OSError("작업목록 폴더를 읽지 못했다: %s" % backlog)
        cards = merge_cards(read_cards(backlog), REMOTE.get(os.path.abspath(item["path"])), item.get("기기", ""))
    except Exception as e:                            # 한 프로젝트의 실패가 다른 프로젝트를 막지 않는다
        out["오류"].append(str(e))
        return out
    cards = [c for c in cards if _카드_쓸것(c, cond)]
    낱말 = cond["낱말"]
    commits = None
    if 낱말 or cond["반려"]:
        try:
            commits = _커밋들(item["path"])
        except Exception:
            out["오류"].append("커밋 이력을 읽지 못했다")
    stems = {os.path.splitext(c["file"])[0] for c in cards}
    linked = {s: [] for s in stems}
    for 커밋 in commits or []:
        for s in stems & 커밋[2]:
            linked[s].append(커밋)
    if cond["반려"]:
        cards = [c for c in cards if any(x[3] for x in linked[os.path.splitext(c["file"])[0]])]
    handoffs = {}
    if 낱말 and commits is not None:
        for p in glob.glob(os.path.join(item["path"], ".workflow", "*", "10-handoff.md")):
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    handoffs[os.path.basename(os.path.dirname(p))] = f.read()
            except OSError:
                pass
        try:
            for folder, text in _소각_인계(item["path"]).items():
                handoffs.setdefault(folder, text)
        except Exception:
            pass
    owner = {}                                        # 폴더 이름 → 가장 긴 카드 이름
    for folder in handoffs:
        names = [s for s in stems if folder.startswith(s + "-")]
        if names:
            owner[folder] = max(names, key=len)
    for c in cards:
        stem = os.path.splitext(c["file"])[0]
        found = []
        if 낱말:
            if not c.get("원격만"):
                try:
                    with open(os.path.join(backlog, c["file"]), encoding="utf-8", errors="replace") as f:
                        found += [dict(x, 출처="카드 본문", 어디="") for x in find_lines(f.read(), 낱말)]
                except OSError:
                    pass
            for sha, 때, _, _, msg in linked[stem]:
                found += [dict(x, 출처="커밋 메시지", 어디="%s %s" % (sha[:7], 때), 제목=(msg.strip().splitlines() or [""])[0])
                          for x in find_lines(msg, 낱말)]
            for folder in sorted(f for f, o in owner.items() if o == stem):
                found += [dict(x, 출처="인계 문서", 어디=folder) for x in find_lines(handoffs[folder], 낱말)]
            if not found:
                continue
        if cond["아래"] is not None or cond["위"] is not None:
            cost = sum(v["cost"] for v in read_card_history(item["path"], c["file"])["단계"].values())
            if (cond["아래"] is not None and cost < cond["아래"]) or (cond["위"] is not None and cost > cond["위"]):
                continue
        out["카드"].append({"file": c["file"], "title": c["title"], "상태": c["상태"], "갱신": c["갱신"], "찾은 것": found,
                           "href": "/p/%s/%s" % (urllib.parse.quote(item["name"], safe=""), urllib.parse.quote(c["file"], safe=""))})
    return out


def search(items, qs):
    """(결과, 까닭) — 낱말·거르개가 모두 비면 아무것도 읽지 않는다. 까닭이 있으면 찾지 않는다. 결과는 카드나 오류가 있는 프로젝트만."""
    cond, 까닭 = _조건(qs)
    if 까닭 or not (cond["낱말"] or _거르개_글(cond)):
        return [], 까닭
    found = [_프로젝트_찾기(p, cond) for p in items if not cond["프로젝트"] or p["name"] == cond["프로젝트"]]
    return [r for r in found if r["카드"] or r["오류"]], ""


def _찾은줄(줄, 낱말):
    """찾은 줄 하나를 escape 한 HTML 로. 낱말이 앞쪽 40자보다 뒤면 앞을 「… 」로 자르고, 낱말마다 `<mark>` 를 씌운다."""
    esc = html.escape
    줄 = 줄.strip()
    if not 낱말:
        return esc(줄)
    m = re.search(re.escape(낱말), 줄, re.I)
    if m and m.start() > 40:
        줄 = "… " + 줄[m.start() - 20:]
    out, pos = [], 0
    for m in re.finditer(re.escape(낱말), 줄, re.I):
        out += [esc(줄[pos:m.start()]), "<mark>%s</mark>" % esc(m.group())]
        pos = m.end()
    return "".join(out) + esc(줄[pos:])


def _짧은_날짜(d):
    return d[5:10] if len(d) >= 10 and d[4] == "-" else "—"


def _묶음(결과, 낱말):
    """결과를 종류별 줄로 묶는다 — {"가장 맞는 것", "카드", "커밋", "인계 문서"} 각각 줄 목록. 줄 = 프로젝트·상태·날짜·제목·줄·href."""
    def 줄(r, c, 상태, 날짜, 제목, 찾음):
        return {"프로젝트": r["name"], "상태": 상태, "날짜": _짧은_날짜(날짜), "제목": 제목, "줄": 찾음, "href": c["href"]}

    def 카드줄(r, c):
        본문 = [x for x in c["찾은 것"] if x["출처"] == "카드 본문"]
        return 줄(r, c, c["상태"], c["갱신"], c["title"], (본문 or c["찾은 것"] or [{"줄": ""}])[0]["줄"])

    카드, 커밋, 인계, 본 = [], [], [], set()
    for r in 결과:
        for c in r["카드"]:
            if not 낱말 or any(x["출처"] == "카드 본문" for x in c["찾은 것"]):
                카드.append(카드줄(r, c))
            for x in c["찾은 것"]:
                if x["출처"] == "커밋 메시지":
                    sha, _, 때 = x["어디"].partition(" ")
                    키 = ("커밋", r["name"], sha)
                    if 키 not in 본:
                        본.add(키)
                        커밋.append(줄(r, c, sha, 때, x.get("제목", ""), x["줄"]))
                elif x["출처"] == "인계 문서":
                    키 = ("인계", r["name"], x["어디"])
                    if 키 not in 본:
                        본.add(키)
                        인계.append(줄(r, c, c["상태"], c["갱신"], c["title"], x["줄"]))
    best = []
    if 낱말:
        제목 = [(r, c) for r in 결과 for c in r["카드"] if 낱말.lower() in c["title"].lower()]
        본문 = [(r, c) for r in 결과 for c in r["카드"] if any(x["출처"] == "카드 본문" for x in c["찾은 것"])]
        고름 = max(제목, key=lambda rc: rc[1]["갱신"]) if 제목 else (본문[0] if 본문 else None)
        best = [카드줄(*고름)] if 고름 else []
    return {"가장 맞는 것": best, "카드": 카드, "커밋": 커밋, "인계 문서": 인계}


def _검색_주소(qs, **바꿈):
    """지금 쿼리에서 `바꿈` 만 바꾼 검색 주소. 값이 빈 글자면 그 열쇠를 뺀다. 화면 형식(`as`)은 가져가지 않는다."""
    q = {k: v for k, v in qs.items() if k != "as"}
    for k, v in 바꿈.items():
        if v:
            q[k] = [v]
        else:
            q.pop(k, None)
    return "/search?" + urllib.parse.urlencode(q, doseq=True)


def _검색_입력(qs, 이름들=(), 건수=""):
    """검색 한 줄·거르개 칩 줄·숨은 제출 단추 — 폼 하나. 쿼리 이름 아홉(q·sc·fn·dev·from·to·rej·cmin·cmax)이 모두 이 안에 있다."""
    esc = html.escape
    cond = _조건(qs)[0]

    def v(k):
        return esc((qs.get(k) or [""])[0], quote=True)
    box = lambda k, label, typ="text": '<label>%s <input type="%s" name="%s" value="%s"></label>' % (label, typ, k, v(k))
    rej = (qs.get("rej") or [""])[0] == "1"
    칩 = lambda 글, 켬, href: '<a class="sr-chip%s" href="%s">%s</a>' % (" on" if 켬 else "", esc(href, quote=True), esc(글))
    chips = [칩("모든 프로젝트", not cond["프로젝트"], _검색_주소(qs, p=""))]
    chips += [칩(n, cond["프로젝트"] == n, _검색_주소(qs, p="" if cond["프로젝트"] == n else n)) for n in 이름들]
    chips += [칩(k if k != "인계" else "인계 문서", cond["종류"] == k, _검색_주소(qs, kind="" if cond["종류"] == k else k))
              for k in ("카드", "커밋", "인계")]
    기간 = "기간: %s~%s" % (cond["시작"], cond["끝"]) if cond["시작"] or cond["끝"] else "기간"
    chips.append('<details class="sr-d"%s><summary class="sr-chip%s">%s</summary>%s %s</details>' % (
        " open" if cond["시작"] or cond["끝"] else "", " on" if cond["시작"] or cond["끝"] else "", esc(기간),
        box("from", "시작", "date"), box("to", "끝", "date")))
    더 = any(cond[k] for k in ("시나리오", "기능", "기기", "반려")) or cond["아래"] is not None or cond["위"] is not None
    chips.append('<details class="sr-d"%s><summary class="sr-chip%s">+ 필터</summary>%s %s %s '
                 '<label>반려 <select name="rej"><option value="">—</option><option value="1"%s>반려 있음</option></select></label> %s %s</details>'
                 % (" open" if 더 else "", " on" if 더 else "", box("sc", "시나리오"), box("fn", "기능 ID"), box("dev", "기기"),
                    " selected" if rej else "", box("cmin", "비용 아래(달러)", "number"), box("cmax", "위", "number")))
    숨김 = "".join('<input type="hidden" name="%s" value="%s">' % (k, v(k)) for k in ("p", "kind") if v(k))
    return ('<form method="get" action="/search"><label class="sr-box"><span aria-hidden="true">⌕</span>'
            '<input type="search" name="q" value="%s" placeholder="낱말" aria-label="검색어"><span class="sr-cnt">%s</span></label>%s'
            '<div class="sr-chips">%s</div><button type="submit" class="sr-sub">찾기</button></form>'
            % (v("q"), esc(건수), 숨김, "".join(chips)))


def _묶음_그리기(이름, 줄들, 낱말, qs, 모두):
    esc = html.escape
    kind = {"카드": "카드", "커밋": "커밋", "인계 문서": "인계"}[이름]
    보일 = 줄들 if 모두 else 줄들[:5]
    head = '<div class="sr-h"><b>%s</b><span class="sr-n">%d</span>' % (esc(이름), len(줄들))
    if len(줄들) > 5 and not 모두:
        head += '<a class="sr-more" href="%s">%d건 모두</a>' % (esc(_검색_주소(qs, kind=kind), quote=True), len(줄들))
    rows = []
    for x in 보일:
        rows.append('<a class="sr-r" href="%s"><span class="sr-meta"><span class="sr-b">%s</span><span class="sr-m">%s · %s</span></span>'
                    '<span class="sr-t">%s</span>%s</a>'
                    % (esc(x["href"], quote=True), esc(x["프로젝트"]), esc(x["상태"]), esc(x["날짜"]), esc(x["제목"]),
                       '<span class="sr-l">%s</span>' % _찾은줄(x["줄"], 낱말) if x["줄"] else ""))
    return '<div class="sr-g" data-g="%s">%s</div>%s</div>' % (esc(이름), head, "".join(rows))


def render_search(qs, 결과, 까닭, 초=None, 이름들=None):
    """검색 화면 HTML. 값은 모두 escape 한다. `이름들` 은 칩으로 보일 프로젝트 이름(없으면 결과에 나온 프로젝트)."""
    esc = html.escape
    cond = _조건(qs)[0]
    거르개 = _거르개_글(cond)
    활성 = bool(cond["낱말"] or 거르개) and not 까닭
    n = sum(len(r["카드"]) for r in 결과)
    건수 = ("%d건" % n + (" · %.1f초" % 초 if 초 is not None else "")) if 활성 else ""
    parts = [_검색_입력(qs, [r["name"] for r in 결과] if 이름들 is None else 이름들, 건수)]
    if 까닭:
        parts.append("<p>%s</p>" % esc(까닭))
    elif not 활성:
        parts.append("<p>낱말이나 필터를 넣고 찾는다</p>")
    else:
        if 거르개:
            parts.append('<p class="sr-flt">필터: %s</p>' % esc(", ".join(거르개)))
        for r in 결과:
            parts += ["<p>%s: 찾지 못함 — %s</p>" % (esc(r["name"]), esc(x)) for x in r["오류"]]
        if not n:
            parts.append("<p>찾은 것 없음</p>")
            if cond["낱말"] and 거르개:
                parts.append('<p><a href="%s">필터를 풀고 「%s」만으로 찾기</a></p>'
                             % (esc(_검색_주소({"q": [cond["낱말"]]}), quote=True), esc(cond["낱말"])))
            elif not cond["낱말"]:
                parts.append("<p>낱말을 넣고 찾는다</p>")
        묶음 = _묶음(결과, cond["낱말"])
        if not cond["종류"] and 묶음["가장 맞는 것"]:
            x = 묶음["가장 맞는 것"][0]
            parts.append('<div class="sr-g" data-g="가장 맞는 것"><div class="sr-h"><b>가장 맞는 것</b><span class="sr-n">1</span></div>'
                         '<a class="sr-r" href="%s"><span class="sr-meta"><span class="sr-b">%s</span><span class="sr-m">%s · %s</span></span>'
                         '<span class="sr-t">%s</span>%s</a></div>'
                         % (esc(x["href"], quote=True), esc(x["프로젝트"]), esc(x["상태"]), esc(x["날짜"]), esc(x["제목"]),
                            '<span class="sr-l">%s</span>' % _찾은줄(x["줄"], cond["낱말"]) if x["줄"] else ""))
        for 이름 in ("카드", "커밋", "인계 문서"):
            kind = {"카드": "카드", "커밋": "커밋", "인계 문서": "인계"}[이름]
            if 묶음[이름] and cond["종류"] in ("", kind):
                parts.append(_묶음_그리기(이름, 묶음[이름], cond["낱말"], qs, bool(cond["종류"])))
    틀 = FILE_TEMPLATE.replace('<a href="board.html">← 보드</a>', '<a href="/">← %s</a>' % 화면이름["home"])
    return 틀 % {"name": "지난 작업 검색", "body": "\n".join(parts)}


def search_json(items, 결과, 까닭, 낱말=""):
    cards = [{"프로젝트": r["name"], "title": c["title"], "href": c["href"],
              "줄": c["찾은 것"][0]["줄"] if c["찾은 것"] else ""} for r in 결과 for c in r["카드"]]
    best = _묶음(결과, 낱말)["가장 맞는 것"]
    best = {"프로젝트": best[0]["프로젝트"], "title": best[0]["제목"], "href": best[0]["href"], "줄": best[0]["줄"]} if best else None
    return json.dumps({"건": len(cards), "까닭": 까닭, "카드": cards, "가장 맞는 것": best,
                       "프로젝트": [{"name": p["name"], "href": "/p/%s/board.html" % urllib.parse.quote(p["name"], safe="")}
                                 for p in items]}, ensure_ascii=False)


# ── 기계별 시간 막대 (ISSUE-sc02-e8-timeline-2026-11-20) ──
# 요청마다 git 을 읽는다 — 색인을 두지 않고 화면 단위 잠깐 저장(`_잠깐저장`)만 둔다. 이어받기·재인계를 세는 규칙은 `read_card_history` 와 같다 —
# 바꿀 때 두 곳(`_이력_서식` 의 `Loop-Resumed` 읽기·`read_card_history` 의 셈과 아래 `read_timeline_project`)을 함께 고친다.
_시간_서식 = "\x1e" + _구분.join(["%H", "%ct", "%s"] + ["%(trailers:key=Loop-" + k + ",valueonly)" for k in ("Issue", "Card", "Resumed", "Stage")])


_카드글_저장 = {}                                       # {(프로젝트, 해시, 카드 파일): 글 또는 None(커밋은 있는데 파일이 없음)} — 커밋 내용은 안 바뀌어 무효화가 없다
_카드글_저장_상한 = 20000


def _카드_내용들(project, 쪽지):
    """`[(해시, 카드 파일)]` 의 그 커밋 때 카드 파일 내용 `{(해시, 카드 파일): 글}`. 없는 것은 빠진다.
    읽은 것은 저장해 두고 다시 묻지 않는다. 「커밋 객체가 이 기계에 없음」만 저장하지 않는다 — 원격을 받으면 읽히기 때문이다."""
    out, 새로 = {}, []
    for x in dict.fromkeys(쪽지):
        if (project,) + x in _카드글_저장:
            if _카드글_저장[(project,) + x] is not None:
                out[x] = _카드글_저장[(project,) + x]
        else:
            새로.append(x)
    if not 새로:
        return out
    커밋들 = list(dict.fromkeys(h for h, _ in 새로))     # 같은 입력에 커밋마다 `<해시>^{commit}` 줄을 덧붙여 「커밋 없음」과 「파일 없음」을 가른다
    r = subprocess.run(["git", "-C", _읽을_저장소(project), "cat-file", "--batch"], capture_output=True, timeout=30,
                       input="".join(["%s:docs/backlog/%s\n" % x for x in 새로] + ["%s^{commit}\n" % h for h in 커밋들]).encode("utf-8"))
    data, pos, 읽음, 있는커밋 = r.stdout, 0, {}, set()
    for key in 새로 + [(h, None) for h in 커밋들]:
        end = data.index(b"\n", pos)
        head = data[pos:end].decode("utf-8", "replace").split()
        pos = end + 1
        if len(head) == 3 and head[1] in ("blob", "commit"):
            size = int(head[2])
            if key[1] is None:
                있는커밋.add(key[0])
            elif head[1] == "blob":
                읽음[key] = data[pos:pos + size].decode("utf-8", "replace")
            pos += size + 1
    if r.returncode == 0:
        if len(_카드글_저장) + len(새로) > _카드글_저장_상한:
            _카드글_저장.clear()
        for key in 새로:
            if key in 읽음:
                _카드글_저장[(project,) + key] = 읽음[key]
            elif key[0] in 있는커밋:
                _카드글_저장[(project,) + key] = None
    out.update(읽음)
    return out


def read_timeline_project(project, 끝=None, now=None):
    """한 프로젝트의 시간 막대 자료 — {"구간": [...], "작은 수정": [(때, 제목)], "완료 때": {카드 파일: 때},
    "기다림": [...], "병합": [(때, 누가)], "이어받기 때": [때]}. 구간마다 `국면` 조각이 붙는다. git 을 못 읽으면 OSError."""
    now = now or dt.datetime.now()
    log = ["git", "-C", _읽을_저장소(project), "log", "--all"] + ([끝] if 끝 else []) + ["--format=" + _시간_서식]
    r = subprocess.run(log, capture_output=True, text=True, timeout=30)
    if r.returncode != 0 and 끝:                       # 원격 끝 객체가 이 기계에 없으면 이 기계 참조만 읽는다
        r = subprocess.run(log[:5] + log[6:], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise OSError("커밋 이력을 읽지 못했다: %s" % (r.stderr.strip().splitlines() or ["git 실패"])[-1])
    commits = []
    for rec in r.stdout.split("\x1e")[1:]:
        f = [x.strip() for x in rec.split(_구분)]
        if len(f) == 7:
            commits.append({"sha": f[0], "때": dt.datetime.fromtimestamp(int(f[1])), "제목": f[2],
                            "issue": f[3].removesuffix(".md"), "card": f[4].removesuffix(".md"), "resumed": f[5] == "true", "stage": f[6]})
    commits.sort(key=lambda c: c["때"])
    heads = [c for c in commits if c["card"] and not c["issue"] and c["제목"].startswith("작업목록:")]
    기록없음 = sorted({c["issue"] for c in commits if c["issue"]} - {c["card"] for c in heads}) if 끝 else []   # 원격 끝 머리말을 볼 후보 — 같은 프로세스로 함께 읽는다
    내용 = _카드_내용들(project, [(c["sha"], c["card"] + ".md") for c in heads] + [(끝, card + ".md") for card in 기록없음])
    by_card, steps = {}, {}
    for c in heads:
        text = 내용.get((c["sha"], c["card"] + ".md"))
        if text is None:                                # 카드 파일 내용을 못 읽은 커밋은 건너뛴다
            continue
        fm = remote_front_matter(text) or {}
        by_card.setdefault(c["card"], []).append({**c, "fm": fm, "제목글": next((x[2:].strip() for x in text.splitlines() if x.startswith("# ")), c["card"]),
                                                   "열쇠": c["제목"].partition(" — ")[2]})
    for c in commits:
        if c["issue"]:
            steps.setdefault(c["issue"], []).append(c)

    def 지금(card):                                      # 지금 카드 머리말 — 작업 사본 파일, 없으면 마지막 머리말 커밋 내용
        path = os.path.join(project, "docs", "backlog", card + ".md")
        if os.path.isfile(path):
            return front_matter(path), title(path)
        last = (by_card.get(card) or [None])[-1]
        return (last["fm"], last["제목글"]) if last else ({}, card)

    이기계 = this_machine_read()
    호스트 = socket.gethostname() if 이기계 else ""

    def 기계(fm):
        돈 = _기기_이름(fm.get("돈기계"))
        if 이기계 and 돈 == 호스트:                       # 옛 기록이 호스트 이름으로 남긴 이 기계 — 같은 이름으로 묶는다
            돈 = 이기계
        return next((v for v in (돈, _기기_이름(fm.get("기기"))) if v), "기기 모름")

    구간, 완료때, 기다림 = [], {}, []
    cards = set(by_card) | set(steps)
    for card in sorted(cards):
        resumed = [x["때"] for x in commits if x["resumed"] and card in (x["issue"], x["card"])]
        now_fm, now_title = 지금(card)
        n_resumed = len(resumed)
        n_hand = int(now_fm.get("재인계", "")) if now_fm.get("재인계", "").isdigit() else 0
        seq = by_card.get(card)
        if not seq:
            times = [c["때"] for c in steps[card]]
            기 = 기계(now_fm)
            if 기 == "기기 모름" and 끝:                    # 이 기계 머리말에 기기가 없으면 원격 끝 트리의 같은 카드 머리말을 본다
                원격글 = _카드_내용들(project, [(끝, card + ".md")]).get((끝, card + ".md"))
                기 = 기계(remote_front_matter(원격글) or {}) if 원격글 else 기
            구간.append({"카드": card + ".md", "제목": now_title, "기계": 기, "시작": min(times), "끝": max(times), "진행 중": False,
                       "기록 없음": True, "끊김": [(t, "이어받기") for t in sorted(set(resumed)) if min(times) <= t <= max(times)],
                       "이어받기": n_resumed, "재인계": n_hand, "국면": []})
            완료때[card + ".md"] = max(times)
            continue
        run, prev, wait, 아는기계 = None, None, None, None
        for c in seq:
            state = c["fm"].get("상태", "대기")
            if 기계(c["fm"]) != "기기 모름":
                아는기계 = 기계(c["fm"])
            if state == "진행":
                run = (run or {"시작": c["때"], "커밋": []})
                run["커밋"].append(c)
            elif run:
                구간.append(_구간_만들기(card, run, c["때"], False, 기계, n_resumed, n_hand, resumed, steps.get(card, [])))
                run = None
            if state in ("보류", "검토") or c["fm"].get("멈춘이유", "").strip():       # 사람 기다림 — 풀리는 머리말 커밋까지
                wait = wait or {"카드": card + ".md", "제목": c["제목글"], "기계": 아는기계 or "기기 모름", "시작": c["때"]}
            elif wait:
                기다림.append({**wait, "끝": c["때"], "진행 중": False})
                wait = None
            if state == "완료" and prev != "완료":
                완료때[card + ".md"] = c["때"]
            prev = state
        if run:
            구간.append(_구간_만들기(card, run, now, True, 기계, n_resumed, n_hand, resumed, steps.get(card, [])))
        if wait:
            기다림.append({**wait, "끝": now, "진행 중": True})
    작은 = [(c["때"], c["제목"]) for c in commits if c["제목"].startswith("작은 수정:")]
    병합 = [(c["때"], _병합_누가(c["제목"])[0]) for c in commits if _병합_누가(c["제목"])]
    return {"구간": 구간, "작은 수정": 작은, "완료 때": 완료때, "기다림": 기다림, "병합": 병합,
            "이어받기 때": [c["때"] for card in sorted(steps) for c in _이어받기_묶음_첫(steps[card])]}


def _이어받기_묶음_첫(단계들):
    """한 카드의 단계 커밋에서 이어받아 다시 시작한 실행마다 한 커밋 — 앞 커밋이 이어받기가 아닌 이어받기 커밋,
    또는 앞 커밋도 이어받기지만 앞 실행과 다른 때에 01 단계가 다시 시작된 것(새 실행은 01 부터 「이어받음」 커밋을 남긴다).
    한 실행의 커밋은 같은 때에 찍혀 순서가 섞이므로 「앞 실행의 때」와 견준다."""
    앞, 시작때 = None, None
    for c in sorted(단계들, key=lambda x: x["때"]):
        if c["resumed"] and (not (앞 and 앞["resumed"]) or (c["stage"] == "01" and c["때"] != 시작때)):
            yield c
            시작때 = c["때"]
        앞 = c


def _국면_나누기(시작, 끝, 단계들):
    """구간 `[시작, 끝]` 을 단계 커밋으로 자른 `[{"국면", "시작", "끝"}]`. 조각은 앞 단계 커밋(첫 조각은 구간 시작)부터 이 커밋까지이고
    색은 이 커밋 단계의 국면이다. 이어받기 묶음의 첫 커밋(앞 단계 커밋이 이어받기가 아닌 것)으로 끝나는 조각은 「멈춤」이다.
    국면을 모르는 단계(`07a` 등)는 조각을 만들지 않아 그 시간이 다음 조각에 들어간다."""
    out, prev, 앞 = [], 시작, None
    for c in sorted(단계들, key=lambda x: x["때"]):
        if c["때"] > 끝:
            break
        if c["때"] >= 시작:
            국면 = "멈춤" if c["resumed"] and 앞 is not None and not 앞["resumed"] else STAGE_PHASE.get(c["stage"])
            if 국면 and c["때"] > prev:
                out.append({"국면": 국면, "시작": prev, "끝": c["때"]})
                prev = c["때"]
        앞 = c
    return out


def _구간_만들기(card, run, 끝, 진행중, 기계, n_resumed, n_hand, resumed, 단계들=()):
    last = run["커밋"][-1]
    cuts = [(t, "이어받기") for t in sorted(set(resumed)) if run["시작"] <= t <= 끝]
    cuts += [(c["때"], "재인계") for c in run["커밋"] if "재인계" in [k.strip() for k in c["열쇠"].split(",")]]
    return {"카드": card + ".md", "제목": last["제목글"], "기계": 기계(last["fm"]), "시작": run["시작"], "끝": 끝, "진행 중": 진행중,
            "기록 없음": False, "끊김": sorted(cuts), "이어받기": n_resumed, "재인계": n_hand, "국면": _국면_나누기(run["시작"], 끝, 단계들)}


def timeline_period(qs, today, 일수=7):
    """볼 기간 `(시작 datetime, 끝 datetime, 까닭)`. 끝은 마지막 날 다음 날 00:00. 기본은 오늘을 끝으로 한 `일수`일(첫 화면은 7, `/timeline` 은 2)."""
    today = dt.datetime(today.year, today.month, today.day)
    기본 = (today - dt.timedelta(days=일수 - 1), today + dt.timedelta(days=1),
          "볼 기간을 읽지 못해 오늘까지 %s로 보인다" % ("48시간" if 일수 == 2 else "%d일" % 일수))
    g = lambda k: (qs.get(k) or [""])[0].strip()          # noqa: E731
    a, z = g("시작"), g("끝")
    if not a and not z:
        return 기본[0], 기본[1], ""
    if (a and not _날짜(a)) or (z and not _날짜(z)):
        return 기본
    last = dt.datetime.strptime(z, "%Y-%m-%d") if z else today
    first = dt.datetime.strptime(a, "%Y-%m-%d") if a else last - dt.timedelta(days=일수 - 1)
    if first > last:
        return 기본
    return first, last + dt.timedelta(days=1), ""


_움직임_서식 = "\x1e" + _구분.join(["%ct"] + ["%(trailers:key=Loop-" + k + ",valueonly)" for k in ("Issue", "Card", "Plugin")])


def read_moves(project, 끝=None):
    """커밋마다 `{"때", "카드"(.md 뺀 이름), "단계"(러너 단계 커밋이면 True), "판"}`. 카드는 `Loop-Issue`, 없으면 `Loop-Card`. 못 읽으면 OSError."""
    log = ["git", "-C", _읽을_저장소(project), "log", "--all"] + ([끝] if 끝 else []) + ["--format=" + _움직임_서식]
    r = subprocess.run(log, capture_output=True, text=True, timeout=30)
    if r.returncode != 0 and 끝:                       # 원격 끝 객체가 이 기계에 없으면 이 기계 참조만 읽는다
        r = subprocess.run(log[:5] + log[6:], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise OSError("커밋 이력을 읽지 못했다: %s" % (r.stderr.strip().splitlines() or ["git 실패"])[-1])
    out = []
    for rec in r.stdout.split("\x1e")[1:]:
        f = [x.strip() for x in rec.split(_구분)]
        if len(f) == 4 and (f[1] or f[2]):
            out.append({"때": dt.datetime.fromtimestamp(int(f[0])), "카드": (f[1] or f[2]).removesuffix(".md"), "단계": bool(f[1]), "판": f[3]})
    return sorted(out, key=lambda m: m["때"])


# ── 최근 활동 피드 (ISSUE-sc02-e20-activity-feed-2026-11-20) ──
FEED_MAX = 30
_피드_서식 = "\x1e" + _구분.join(["%H", "%ct"] + ["%(trailers:key=Loop-" + k + ",valueonly)" for k in ("Issue", "Card", "Stage", "Verdict")])


def read_feed(project, 끝, 기계맵):
    """프로젝트 한 곳의 활동 줄 `{"때", "카드", "단계", "결과", "기계"}`. 못 읽으면 OSError.

    단계 커밋(`Loop-Stage`)은 `Loop-Verdict` 가 통과·반려일 때만, 머리말 커밋(`Loop-Card` 만)은 카드 파일의 `멈춘이유:` 가
    빈 값에서 채워지면 「멈춤」, 아니면 `상태:` 가 바뀌면 「상태 → 새 값」. 기계는 `기계맵`(카드 파일 → 도는 기계), 모르면 「기기 모름」."""
    def git(*args):
        r = subprocess.run(["git", "-C", _읽을_저장소(project), "-c", "core.quotepath=false", *args], capture_output=True, text=True, timeout=30)
        if r.returncode != 0 and 끝 and args[0] == "log" and 끝 in args:      # 원격 끝 객체가 이 기계에 없으면 이 기계 참조만 읽는다
            return git(*[a for a in args if a != 끝])
        if r.returncode != 0:
            raise OSError("커밋 이력을 읽지 못했다: %s" % (r.stderr.strip().splitlines() or ["git 실패"])[-1])
        return r.stdout

    commits = []
    for rec in git("log", "--all", *([끝] if 끝 else []), "--format=" + _피드_서식).split("\x1e")[1:]:
        f = [x.strip() for x in rec.split(_구분)]
        if len(f) == 6:
            commits.append(dict(zip(("sha", "때", "이슈", "카드", "단계", "판정"), f)))
    heads = [c for c in commits if c["카드"] and not c["단계"]]
    diffs = {}
    if heads:                                          # 머리말 커밋의 카드 파일 diff 만 한 번에 읽는다
        for rec in git("log", "--no-walk=unsorted", "--unified=0", "--no-color", "--format=\x1e%H", *[c["sha"] for c in heads],
                       "--", "docs/backlog").split("\x1e")[1:]:
            sha, _, diff = rec.partition("\n")
            diffs[sha.strip()] = diff
    out = []
    for c in commits:
        card = c["카드"] if c["카드"].endswith(".md") else c["카드"] + ".md"
        if c["단계"]:
            if c["판정"] not in ("통과", "반려") or not c["이슈"]:
                continue
            card, 단계, 결과 = c["이슈"].removesuffix(".md") + ".md", c["단계"], c["판정"]
        elif c["카드"]:
            old, new, 파일 = {}, {}, ""
            for line in diffs.get(c["sha"], "").splitlines():
                if line.startswith("+++ "):
                    파일 = os.path.basename(line[4:])
                for sign, d in (("-", old), ("+", new)):
                    for k in ("상태", "멈춘이유"):
                        if 파일 == card and line.startswith(sign + k + ":"):
                            d[k] = line[len(sign) + len(k) + 1:].strip()
            if new.get("멈춘이유") and not old.get("멈춘이유"):
                단계, 결과 = "머리말", "멈춤"
            elif "상태" in old and new.get("상태") and old["상태"] != new["상태"]:
                단계, 결과 = "머리말", "상태 → " + new["상태"]
            else:
                continue
        else:
            continue
        out.append({"때": dt.datetime.fromtimestamp(int(c["때"])), "카드": card, "단계": 단계, "결과": 결과, "기계": 기계맵.get(card) or "기기 모름"})
    return out


def collect_feed(items, rows, 거르개, now):
    """`(줄, 못 읽은 프로젝트 이름들)`. 줄은 새것이 위이고 거르개를 적용한 **뒤** 앞 `FEED_MAX` 줄. `rows` 는 `items` 와 같은 순서이고 `카드`·`까닭` 을 가진다."""
    줄, 못 = [], []
    for item, row in zip(items, rows):
        if 거르개.get("프로젝트") and 거르개["프로젝트"] != item["name"]:
            continue
        try:
            if row["까닭"]:
                raise OSError(row["까닭"])
            끝 = (REMOTE.get(os.path.abspath(item["path"])) or {}).get("끝")
            맵 = {c["file"]: c["도는 기계"] for c in row["카드"]}
            줄 += [{**x, "프로젝트": item["name"], "카드있음": x["카드"] in 맵} for x in read_feed(item["path"], 끝, 맵)]
        except Exception:                              # 한 프로젝트의 실패가 다른 프로젝트 줄을 막지 않는다
            못.append(item["name"])
    if 거르개.get("기계"):
        줄 = [x for x in 줄 if x["기계"] == 거르개["기계"]]
    줄.sort(key=lambda x: (x["때"], x["프로젝트"], x["카드"], x["결과"]), reverse=True)
    return 줄[:FEED_MAX], 못


def _얼마_전(때):
    분 = max(0, int((dt.datetime.now() - 때).total_seconds() // 60))
    return "방금" if 분 < 1 else "%d분 전" % 분 if 분 < 60 else "%d시간 전" % (분 // 60) if 분 < 1440 else "%d일 전" % (분 // 1440)


def render_feed(줄, 못, 거르개, 프로젝트들, 기계들, 폼=True):
    """최근 활동 구역 하나. 줄 전체가 그 카드 상세 링크이고 거르개는 GET 폼이다(`폼=False` 면 폼 없이 줄만 — 프로젝트 보드는 기계 칩이 거른다)."""
    esc = html.escape

    def select(name, 고른, 값들, 처음):
        opts = "".join('<option value="%s"%s>%s</option>' % (esc(v, quote=True), " selected" if v == 고른 else "", esc(_기기_글(v))) for v in 값들)
        return '<select name="%s"><option value="">%s</option>%s</select>' % (name, 처음, opts)

    form = ('<form method="get">%s %s <button type="submit">적용</button></form>'
            % (select("feed_project", 거르개.get("프로젝트", ""), 프로젝트들, "모든 프로젝트"),
               select("feed_machine", 거르개.get("기계", ""), 기계들, "모든 기기")))
    body = "".join('<p><a href="/p/%s/%s">%s</a></p>' % (
        urllib.parse.quote(x["프로젝트"], safe=""), urllib.parse.quote(x["카드"], safe=""),
        esc("%s · %s · %s (%s, %s)" % (x["카드"].removesuffix(".md"), x["단계"], x["결과"], _기기_글(x["기계"]), _얼마_전(x["때"]))))
        for x in 줄) or "<p>최근 활동 없음</p>"
    why = "".join('<p class="why">%s: 이 프로젝트 활동 못 읽음</p>' % esc(n) for n in 못)
    return '<div class="feed"><h2>최근 활동</h2>%s%s%s</div>' % (form if 폼 else "", why, body)


def _피드_구역(items, rows, qs, 고정=None, 폼=True):
    """화면에 끼울 피드 HTML. 어떤 예외가 나도 나머지 화면이 뜨도록 구역 안에서 막는다. `고정` 은 프로젝트 보드가 거르는 프로젝트."""
    g = lambda k: (qs.get(k) or [""])[0].strip()          # noqa: E731
    거르개 = {"프로젝트": 고정 or g("feed_project"), "기계": g("feed_machine") or g("기계")}
    try:
        줄, 못 = collect_feed(items, rows, 거르개, dt.datetime.now())
        기계들 = sorted({c["도는 기계"] for r in rows for c in r["카드"]})
        return render_feed(줄, 못, 거르개, [i["name"] for i in items], 기계들, 폼)
    except Exception:
        return '<div class="feed"><h2>최근 활동</h2><p class="why">활동 못 읽음</p></div>'


def plugin_mismatch(줄):
    """판을 아는기계(「기기 모름」 제외)의 판이 둘 이상 다르면 `[(기계, 판)]`, 아니면 `[]`."""
    known = [(x["기계"], x["판"]) for x in 줄 if x["판"] and x["기계"] != "기기 모름"]
    return known if len({v for _, v in known}) > 1 else []


def read_machines(items, rows, 시작, 끝, now):
    """첫 화면 기계 현황 자료. `rows` 는 `items` 와 같은 순서의 `project_row` 결과. 한 프로젝트의 실패는 `못 읽음` 에 모은다.

    단계 커밋(`Loop-Issue`)은 같은 카드의 진행 구간 가운데 시작이 그 커밋 시각 이하인 마지막 구간의 기계에 잇고(없으면 「기기 모름」),
    머리말 커밋(`Loop-Card`)은 그 시각을 품은 같은 카드 구간의 기계에 잇는다."""
    lines, 못 = {}, []

    def line(기계):
        return lines.setdefault(기계, {"기계": 기계, "이 기계": False, "도는 카드": [], "마지막 움직임": None, "판": "", "판 때": None,
                                    "시간": 0, "카드 수": 0, "_카드": set()})

    names = {_기기_이름(i.get("기기")) for i in items} - {""}
    for n in names:
        line(n)
    for item, row in zip(items, rows):
        try:
            if row["까닭"]:
                raise OSError(row["까닭"])
            끝점 = (REMOTE.get(os.path.abspath(item["path"])) or {}).get("끝")
            tl, moves = read_timeline_project(item["path"], 끝점, now), read_moves(item["path"], 끝점)
        except Exception as e:                           # 한 프로젝트의 실패가 다른 프로젝트 그리기를 막지 않는다
            못.append((item["name"], str(e) or type(e).__name__))
            continue
        for c in row["카드"]:
            if c["상태"] == "진행":
                line(c["도는 기계"])["도는 카드"].append((item["name"], c["title"], c["단계"]))
        by_card = {}
        for x in tl["구간"]:
            by_card.setdefault(x["카드"], []).append(x)
            if x["끝"] >= 시작 and x["시작"] < 끝:
                L = line(x["기계"])
                L["시간"] += int((min(x["끝"], 끝) - max(x["시작"], 시작)).total_seconds())
                L["_카드"].add((item["name"], x["카드"]))
        for m in moves:
            ivs = by_card.get(m["카드"] + ".md", [])
            if m["단계"]:
                prior = [x for x in ivs if x["시작"] <= m["때"]]
                기계 = max(prior, key=lambda x: x["시작"])["기계"] if prior else "기기 모름"
            else:
                inside = [x for x in ivs if x["시작"] <= m["때"] <= x["끝"]]
                if not inside:
                    continue
                기계 = inside[0]["기계"]
            L = line(기계)
            if L["마지막 움직임"] is None or m["때"] > L["마지막 움직임"]:
                L["마지막 움직임"] = m["때"]
            if m["판"] and (L["판 때"] is None or m["때"] >= L["판 때"]):
                L["판"], L["판 때"] = m["판"], m["때"]
    out = []
    for L in sorted(lines.values(), key=lambda x: (x["기계"] == "기기 모름", x["기계"])):
        L["카드 수"] = len(L.pop("_카드"))
        L.pop("판 때")
        out.append(L)
    이기계 = next(iter(names)) if len(names) == 1 else ""
    for L in out:
        L["이 기계"] = bool(이기계) and L["기계"] == 이기계
    return {"이 기계": 이기계, "줄": out, "판 다름": plugin_mismatch(out), "못 읽음": 못}


def _시간_글(초):
    h, m = divmod(초 // 60, 60)
    return "%d시간 %d분" % (h, m) if h else "%d분" % m


def _판_번호(v):
    """`0.13.0 (364e29210a09)` → `0.13.0`. 그 꼴이 아니면 그대로."""
    m = re.fullmatch(r"(\S+) \((.+)\)", v or "")
    return m.group(1) if m else v


def _판_글(v):
    """보이는 곳의 플러그인 판 — 번호만 보이고 전체 값은 마우스를 올렸을 때(title)."""
    n = _판_번호(v)
    return html.escape(v) if n == v else '<span title="%s">%s</span>' % (html.escape(v, quote=True), html.escape(n))


def _판_다름_글(쌍):
    """판 다름 경고의 `기계 판` 이음. 번호만으로도 서로 갈리면 번호, 번호가 같고 해시만 다르면 전체 값."""
    짧게 = len({_판_번호(v) for _, v in 쌍}) > 1
    return " · ".join("%s %s" % (k, _판_번호(v) if 짧게 else v) for k, v in 쌍)


def render_machines(data, 시작, 끝, 까닭):
    """첫 화면 기계 현황 조각 — 기간 입력, 판 다름 경고, 기계마다 한 줄(작업량은 `.bar` 막대)."""
    esc = html.escape
    lines = data["줄"]
    form = ('<form method="get"><input type="date" name="시작" value="%s"> ~ <input type="date" name="끝" value="%s">'
            ' <button type="submit">보기</button></form>' % (시작.date().isoformat(), (끝 - dt.timedelta(days=1)).date().isoformat()))
    why = "".join('<p class="why">%s</p>' % esc(x) for x in (
        ["판 다름 — " + _판_다름_글(data["판 다름"])] if data["판 다름"] else []) + ([까닭] if 까닭 else []) +
        ["%s — 이력을 못 읽어 기기 현황에서 뺐다" % n for n, _ in data["못 읽음"]])
    most = (max(x["시간"] for x in lines) or 0, max(x["카드 수"] for x in lines) or 0) if lines else (0, 0)

    def bar(v, top):
        return '<span class="bar"><span style="width:%d%%"></span></span>' % (v * 100 // top if top else 0)

    def row(x):
        cards = "<br>".join(esc("%s · %s (%s)" % (p, t, s or "단계 모름")) for p, t, s in x["도는 카드"]) or "도는 카드 없음"
        move = x["마지막 움직임"].strftime("%Y-%m-%d %H:%M") if x["마지막 움직임"] else "움직임 없음"
        return ["%s%s" % (esc(_기기_글(x["기계"])), " · 이 기기" if x["이 기계"] else ""), cards, move, _판_글(x["판"]) if x["판"] else "판 모름",
                "%s %s" % (bar(x["시간"], most[0]), _시간_글(x["시간"])), "%s %d" % (bar(x["카드 수"], most[1]), x["카드 수"])]
    table = (_표(["기기", "지금 도는 카드", "마지막 움직임", "플러그인 판", "진행 시간", "돈 카드"], [row(x) for x in lines])
             if lines else '<p class="meta">알려진 기기 없음</p>')
    return '<div class="machines"><h2>기기 (%d)</h2>%s%s%s</div>' % (len(lines), form, why, table)


def running_counts(cards):
    """합친 카드 가운데 상태가 진행인 것을 도는 기계별로 센다 — `([(기계, 수)], 전체)`. 기계는 이름순, 「기기 모름」은 끝."""
    n = {}
    for c in cards:
        if c["상태"] == "진행":
            n[c["도는 기계"]] = n.get(c["도는 기계"], 0) + 1
    return sorted(n.items(), key=lambda kv: (kv[0] == "기기 모름", kv[0])), sum(n.values())


def running_text(per, total):
    return " · ".join(["%s %d" % kv for kv in per] + ["전체 %d" % total]) if total else "지금 도는 실행 없음 · 전체 0"


def element_done(p, 완료때):
    """`precedence` 결과 하나 `p` 의 요소 가운데 카드가 모두 완료인 것 — `[{프로젝트, 시나리오, 요소, 이름, 때}]`. 때는 그 카드들의 가장 늦은 완료 때."""
    out = []
    for e in p["요소"]:
        때들 = [완료때[c["file"]] for c in e["카드"] if c["file"] in 완료때]
        if e["카드"] and all(c["상태"] == "완료" for c in e["카드"]) and 때들:
            out.append({"프로젝트": p["프로젝트"], "시나리오": p["시나리오"], "요소": e["번호"], "이름": e["이름"], "때": max(때들)})
    return out


def read_timeline(items, 시작, 끝, now):
    """보는 목록 모든 프로젝트의 시간 막대 자료. 한 프로젝트의 실패는 `못 읽음` 에 모은다."""
    줄, 작은, 완료, 못, per_all, 선행, 기다림, 병합, 이어 = {}, [], [], [], {}, [], [], [], []
    for item in items:
        try:
            tl = read_timeline_project(item["path"], (REMOTE.get(os.path.abspath(item["path"])) or {}).get("끝"), now)
            cards, scenarios, _, _ = _읽기(item, 합치기=True)
            그림 = precedence(item["name"], cards, scenarios)
        except Exception as e:                           # 한 프로젝트의 실패가 다른 프로젝트 그리기를 막지 않는다
            못.append((item["name"], str(e) or type(e).__name__))
            continue
        for x in tl["구간"]:
            줄.setdefault(x["기계"], []).append({**x, "프로젝트": item["name"]})
        작은 += [(t, item["name"], s) for t, s in tl["작은 수정"]]
        완료 += [e for p in 그림 for e in element_done(p, tl["완료 때"])]
        기다림 += [{**x, "프로젝트": item["name"]} for x in tl["기다림"]]
        병합 += [(t, w, item["name"]) for t, w in tl["병합"]]
        이어 += [(t, item["name"]) for t in tl["이어받기 때"]]
        선행 += 그림
        for m, n in running_counts(cards)[0]:
            per_all[m] = per_all.get(m, 0) + n
    동시 = sorted(per_all.items(), key=lambda kv: (kv[0] == "기기 모름", kv[0]))
    return {"줄": 줄, "작은 수정": sorted(작은), "요소 완료": 완료, "동시": 동시, "전체": sum(per_all.values()), "못 읽음": 못, "선행": 선행,
            "기다림": 기다림, "병합": 병합, "이어받기 때": 이어, "프로젝트": [i["name"] for i in items]}


_시간_CSS = """main { max-width:1320px; }
.tl-top { display:flex; flex-wrap:wrap; gap:12px; align-items:center; margin:0 0 16px; }
.tl-top h1 { margin:0; font-size:20px; font-weight:700; color:var(--fg); }
.tl-pg { display:flex; gap:2px; padding:2px; border-radius:8px; background:var(--soft); }
.tl-pb { display:inline-flex; align-items:center; min-height:30px; padding:0 12px; border-radius:6px; font-size:12px; color:var(--dim); }
.tl-pb[aria-pressed="true"] { background:var(--panel); color:var(--fg); font-weight:600; }
.tl-form { display:flex; flex-wrap:wrap; gap:12px; margin-left:auto; }
.tl-form label { display:flex; align-items:center; gap:6px; font-size:12px; color:var(--dim); }
.tl-form select { min-height:32px; border:1px solid var(--line); border-radius:8px; background:var(--panel); color:var(--fg); font:inherit; font-size:12px; padding:0 8px; }
.tl-sum { margin:0 0 16px; }
.tl-sum .tile { padding:12px 14px; gap:0; }
.tl-sum .tl-nt { font-size:11px; color:var(--dim); }
.tl-sum .tile .v { font-size:18px; line-height:1.5; }
.tl-lanes { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-panel); overflow-x:auto; margin:0 0 16px; }
.tl-in { --lab:280px; position:relative; min-width:1100px; }
.tl-hd, .tl-doneln, .tl-line { display:flex; border-bottom:1px solid var(--line2); }
.tl-hd { font-size:11px; color:var(--dim); }
.tl-doneln { background:var(--side); }
.tl-lab { flex:0 0 var(--lab); padding:10px 16px; font-size:12px; font-weight:600; }
.tl-hd .tl-lab { font-weight:400; font-size:11px; }
.tl-axis { flex-grow:1; position:relative; height:34px; }
.tl-axis span { white-space:nowrap; position:absolute; top:10px; transform:translateX(-50%); }
.tl-trk { flex-grow:1; position:relative; height:32px; }
.tl-dia { position:absolute; top:10px; width:10px; height:10px; transform:translateX(-50%) rotate(45deg); background:var(--teal); }
.tl-row > summary { display:flex; align-items:center; gap:8px; padding:10px 16px; background:var(--lane-head); border-bottom:1px solid var(--soft); font-size:12px; cursor:pointer; }
.tl-mn { font-weight:700; }
.tl-mm { color:var(--dim); }
a.tl-label { flex:0 0 var(--lab); padding:8px 16px; font-size:12px; color:var(--fg); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.tl-line { border-bottom-color:var(--lane-head); }
.tl-track { flex-grow:1; position:relative; height:36px; }
.tl-bar { position:absolute; top:9px; height:18px; min-width:4px; border-radius:4px; overflow:hidden; background:var(--line); display:block; }
.tl-seg { position:absolute; top:0; bottom:0; }
.tl-track > .tl-seg { top:9px; bottom:9px; border-radius:4px; }
.tl-ph-분석 { background:var(--ph-분석); }
.tl-ph-설계 { background:var(--ph-설계); }
.tl-ph-구현 { background:var(--blue); }
.tl-ph-검증, .tl-ph-인계 { background:var(--teal); }
.tl-ph-기다림 { background:var(--orange); }
.tl-ph-멈춤 { background:var(--ph-멈춤); }
.tl-cut { position:absolute; top:0; bottom:0; width:2px; background:var(--fg); }
.tl-today { position:absolute; top:0; bottom:0; width:2px; background:var(--orange); pointer-events:none; z-index:2; }
.tl-lgd { display:flex; flex-wrap:wrap; gap:16px; align-items:center; font-size:12px; color:var(--ink2); margin:0 0 12px; }
.tl-lg { display:inline-flex; align-items:center; gap:6px; }
.tl-lg i { width:14px; height:10px; border-radius:3px; display:inline-block; }
.tl-hint { margin-left:auto; color:var(--dim); }
.tl-dot { position:absolute; top:8px; width:10px; height:10px; border-radius:5px; background:var(--fg); }
.tl-common { position:relative; border:1px solid var(--line); border-radius:6px; margin:0 0 6px; min-height:26px; }
.tl-run { font-size:12px; color:var(--dim); margin:0 0 8px; }
h3.tl-name { font-size:13px; margin:8px 0 2px; color:var(--dim); }
@media (max-width:700px) { .tl-form { margin-left:0; } .tl-hint { margin-left:0; } }"""

_그림_CSS = """.tl-graph { position:relative; }
.tl-aside { font-size:12px; margin:0 0 12px; }
.tl-wait { display:inline-block; border:1px dashed var(--line); border-radius:4px; padding:2px 4px; margin:0 6px 4px 0; }
.tl-arrows { position:absolute; left:0; top:0; width:100%; height:100%; pointer-events:none; overflow:visible; }
.tl-edge { fill:none; stroke:var(--fg); stroke-width:1.5; }
.tl-겹침 { stroke-dasharray:5 4; }
.tl-head { fill:var(--fg); }
.tl-els > summary { cursor:pointer; font-size:13px; color:var(--dim); margin:8px 0; }
.tl-legend { font-size:12px; color:var(--dim); margin:0 0 8px; }"""


_화살표_스크립트 = """<script>
(function () {
  function draw() {
    var svg = document.querySelector('.tl-arrows');
    if (!svg) return;
    var box = svg.parentNode.getBoundingClientRect(), NS = 'http://www.w3.org/2000/svg';
    Array.prototype.slice.call(svg.querySelectorAll('.tl-head')).forEach(function (h) { svg.removeChild(h); });
    function find(href, last) {
      var l = Array.prototype.filter.call(document.querySelectorAll('a.tl-bar, a.tl-wait'), function (a) { return a.getAttribute('data-card') === href; });
      return l.length ? l[last ? l.length - 1 : 0] : null;
    }
    Array.prototype.slice.call(svg.querySelectorAll('.tl-edge')).forEach(function (p) {
      var a = find(p.getAttribute('data-from'), true), z = find(p.getAttribute('data-to'), false);
      if (!a || !z) return;
      var r = a.getBoundingClientRect(), t = z.getBoundingClientRect();
      var x1 = r.right - box.left, y1 = r.top + r.height / 2 - box.top, x2 = t.left - box.left, y2 = t.top + t.height / 2 - box.top, mx = (x1 + x2) / 2;
      p.setAttribute('d', 'M' + x1 + ' ' + y1 + ' C' + mx + ' ' + y1 + ' ' + mx + ' ' + y2 + ' ' + x2 + ' ' + y2);
      var h = document.createElementNS(NS, 'path');
      h.setAttribute('class', 'tl-head');
      h.setAttribute('d', 'M' + x2 + ' ' + y2 + ' l-8 -4 l0 8 z');
      svg.appendChild(h);
    });
  }
  draw();
  window.addEventListener('resize', draw);
  window.addEventListener('load', draw);
})();
</script>"""


def _요소_목록(선행):
    """시나리오마다 요소 목록 HTML — 요소 번호·이름, 카드 상태와 카드 상세 고리, 표시 글."""
    esc = html.escape
    out = []
    for p in 선행:
        head = "%s · %s %s" % (p["프로젝트"], p["시나리오"], p["이름"])
        if p["오류"]:
            out.append("<p>%s — %s</p>" % (esc(p["시나리오"]), esc(p["오류"])))
            continue
        if not p["요소"]:
            out.append("<p>%s — 요소 없음</p>" % esc(head))
            continue
        rows = []
        for e in p["요소"]:
            if e["없어짐"]:
                card = "없어짐"
            elif e["카드아님"]:
                card = "(카드 아님)"
            elif not e["카드"]:
                card = "카드 없음"
            else:
                card = " ".join('<a href="%s">%s</a> %s' % (esc("/p/%s/%s" % (urllib.parse.quote(p["프로젝트"], safe=""), urllib.parse.quote(c["file"], safe="")), quote=True),
                                                           esc(c["title"]), esc(c["상태"])) for c in e["카드"])
            rows.append('<tr class="tl-el"><td>%s %s</td><td>%s</td><td>%s</td></tr>'
                        % (esc(e["번호"]), esc(e["이름"]), card, esc(" · ".join(e["표시"]))))
        out.append("<h4>%s</h4><table><thead><tr><th>요소</th><th>카드</th><th>표시</th></tr></thead><tbody>%s</tbody></table>" % (esc(head), "".join(rows)))
    return "".join(out)


def _겹침_최대(구간들):
    """`[(시작, 끝, 기계)]` 의 기계별 겹침 최대 가운데 큰 값과 그 기계들(이름 순). 다른 기계의 겹침은 더하지 않는다."""
    기계별 = {}
    for x in 구간들:
        기계별.setdefault(x[2], []).append(x)
    각 = {m: _겹침_하나(xs) for m, xs in 기계별.items()}
    최대 = max(각.values(), default=0)
    return 최대, sorted(m for m, n in 각.items() if n == 최대) if 최대 else []


def _겹침_하나(구간들):
    """한 기계의 구간 `[(시작, 끝, 기계)]` 가 같은 때 겹친 최대 수."""
    ev = sorted([(s, 1, m) for s, e, m in 구간들 if e > s] + [(e, 0, m) for s, e, m in 구간들 if e > s], key=lambda x: (x[0], x[1]))
    지금 = 최대 = 0
    for _, kind, _m in ev:
        지금 += 1 if kind else -1
        최대 = max(최대, 지금)
    return 최대


def render_timeline(data, 시작, 끝, now, 까닭, 고른="", 카드="", 프로젝트=""):
    """시간 막대 화면(목업 `Timeline.dc.html`) — 기간 단추·거르개, 요약 네 칸, 기계 레인(카드 한 줄, 국면 색 조각), 범례, 그 아래 옛 구역.

    `고른`(`프로젝트:SC-nn`)이면 그 시나리오 카드만, `프로젝트`면 그 프로젝트 것만 보인다. `카드`(`프로젝트:카드파일`)를 골라야 그 카드가 한 끝인 선행 화살표만 그려진다."""
    esc = html.escape
    span = (끝 - 시작).total_seconds()
    선행_전체 = data.get("선행") or []
    이름들 = data.get("프로젝트") or sorted({x["프로젝트"] for xs in data["줄"].values() for x in xs})
    프로젝트모름 = ""
    if 프로젝트 and 프로젝트 not in 이름들:
        프로젝트모름, 프로젝트 = "그런 프로젝트 없음 — %s" % 프로젝트, ""
    ok = lambda 이름: not 프로젝트 or 이름 == 프로젝트                                   # noqa: E731
    선행 = [p for p in 선행_전체 if ok(p["프로젝트"])]
    열쇠 = lambda p: "%s:%s" % (p["프로젝트"], p["시나리오"])                              # noqa: E731
    모름, 고른카드 = "", None
    if 선행_전체 and 고른:
        if any(열쇠(p) == 고른 for p in 선행_전체):
            선행 = [p for p in 선행 if 열쇠(p) == 고른]
            고른카드 = {(p["프로젝트"], c["file"]) for p in 선행 for e in p["요소"] for c in e["카드"]}
        else:
            모름 = "그런 시나리오 없음 — %s" % 고른

    def pct(t):
        return min(max((t - 시작).total_seconds() / span * 100, 0.0), 100.0)

    def 기간안(x):
        return x["끝"] >= 시작 and x["시작"] < 끝 and ok(x["프로젝트"])

    def 보임(x):
        return 기간안(x) and (고른카드 is None or (x["프로젝트"], x["카드"]) in 고른카드)

    def 질의(a, z, 더=()):
        q = [("시작", a), ("끝", z)] + ([("p", 프로젝트)] if 프로젝트 else []) + ([("sc", 고른)] if 고른 else []) + list(더)
        return "/timeline?" + urllib.parse.urlencode(q)

    def 상세(프, 카드파일):
        return "/p/%s/%s" % (urllib.parse.quote(프, safe=""), urllib.parse.quote(카드파일, safe=""))

    첫, 끝날 = 시작.date(), (끝 - dt.timedelta(days=1)).date()
    오늘 = now.date()
    단추 = "".join('<a class="tl-pb"%s href="%s">%s</a>' % (
        ' aria-pressed="true"' if (첫, 끝날) == (오늘 - dt.timedelta(days=n), 오늘) else "",
        esc(질의((오늘 - dt.timedelta(days=n)).isoformat(), 오늘.isoformat()), quote=True), 이름)
        for 이름, n in (("오늘", 0), ("48시간", 1), ("7일", 6)))

    def 고르개(이름, 라벨, 값들, 고름):
        opts = '<option value="">모두</option>' + "".join('<option value="%s"%s>%s</option>' % (esc(v, quote=True), " selected" if v == 고름 else "", esc(v)) for v in 값들)
        return '<label>%s <select name="%s" onchange="this.form.submit()">%s</select></label>' % (라벨, 이름, opts)
    form = ('<form class="tl-form" method="get" action="/timeline"><input type="hidden" name="시작" value="%s"><input type="hidden" name="끝" value="%s">%s%s</form>'
            % (첫.isoformat(), 끝날.isoformat(), 고르개("p", "프로젝트", 이름들, 프로젝트),
               고르개("sc", "시나리오", [열쇠(p) for p in 선행_전체], 고른) if 선행_전체 else ""))
    parts = ['<div class="tl-top"><h1>%s</h1><div class="tl-pg" role="group" aria-label="기간">%s</div>%s</div>' % (esc(화면이름["timeline"]), 단추, form)]
    for 글 in (프로젝트모름, 모름, 까닭):
        if 글:
            parts.append("<p>%s</p>" % esc(글))

    # 요약 네 칸 — 기간·프로젝트로 거른다. 자료에 그 키가 없으면 「—」
    안 = [x for xs in data["줄"].values() for x in xs if 기간안(x)]
    최대, 기계들 = _겹침_최대([(max(x["시작"], 시작), min(x["끝"], 끝), x["기계"]) for x in 안])
    기다림들 = [x for x in data.get("기다림", []) if 기간안(x)]
    칸 = [("기간 안 병합", "%d장" % sum(1 for t, _, p in data["병합"] if 시작 <= t < 끝 and ok(p)) if "병합" in data else "—"),
         ("동시에 돈 최대", "%d장 (%s)" % (최대, "·".join(_기기_글(m) for m in 기계들)) if 최대 else "—"),
         ("사람 기다림 합", _시간_글(int(sum((min(x["끝"], 끝) - max(x["시작"], 시작)).total_seconds() for x in 기다림들))) if "기다림" in data else "—"),
         ("이어받기", "%d번" % sum(1 for t, p in data["이어받기 때"] if 시작 <= t < 끝 and ok(p)) if "이어받기 때" in data else "—")]
    설명 = {"이어받기": "이어받아 다시 시작한 실행(카드·실행 단위)"}
    parts.append('<section class="tiles tl-sum" aria-label="요약">%s</section>' % "".join(
        '<div class="tile"><span class="lb">%s</span><span class="v">%s</span>%s</div>' % (
            esc(a), esc(v), '<span class="tl-nt">%s</span>' % esc(설명[a]) if a in 설명 else "") for a, v in 칸))

    # 기계 레인
    ticks, 앞날 = "", None
    for i in range(7):
        t = 시작 + dt.timedelta(seconds=span * (i * 16 + 2) / 100)
        ticks += '<span style="left:%d%%">%s</span>' % (i * 16 + 2, t.strftime("%m-%d %H시" if t.date() != 앞날 else "%H시"))
        앞날 = t.date()
    inner = ['<div class="tl-hd"><div class="tl-lab">카드</div><div class="tl-axis">%s</div></div>' % ticks]
    if 고른카드 is not None and not 모름:
        dias = "".join('<span class="tl-dia" style="left:%.2f%%" title="%s"></span>' % (pct(e["때"]), esc("%s %s" % (e["요소"], e["이름"]), quote=True))
                       for e in data.get("요소 완료", []) if 열쇠(e) == 고른 and 시작 <= e["때"] < 끝)
        inner.append('<div class="tl-doneln"><div class="tl-lab">%s 요소 완료</div><div class="tl-trk">%s</div></div>' % (esc(고른.partition(":")[2]), dias))
    shown, 줄수 = set(), 0
    접힘 = (끝 - 시작) > dt.timedelta(days=2)               # 48시간보다 긴 기간은 레인을 접고 시작 — 머리줄을 누르면 펼친다
    for name in sorted(data["줄"], key=lambda k: (k == "기기 모름", k)) + [k for k in sorted({x["기계"] for x in data.get("기다림", [])} - set(data["줄"]))]:
        cards = {}
        for x in data["줄"].get(name, []):
            if 보임(x):
                cards.setdefault((x["프로젝트"], x["카드"]), {"제목": x["제목"], "막대": [], "기다림": []})["막대"].append(x)
        for x in data.get("기다림", []):
            if x["기계"] == name and 보임(x):
                cards.setdefault((x["프로젝트"], x["카드"]), {"제목": x["제목"], "막대": [], "기다림": []})["기다림"].append(x)
        if not cards:
            continue
        lines = []
        for (프, 파일), c in sorted(cards.items(), key=lambda kv: min(x["시작"] for x in kv[1]["막대"] + kv[1]["기다림"])):
            주소 = 상세(프, 파일)
            bars = ""
            for x in sorted(c["막대"], key=lambda x: x["시작"]):
                s, e = max(x["시작"], 시작), min(x["끝"], 끝)
                left, width = pct(s), max(pct(e) - pct(s), 0.3)
                len_ = max((e - s).total_seconds(), 1)
                segs = "".join('<span class="tl-seg tl-ph-%s" style="left:%.2f%%;width:%.2f%%" title="%s"></span>' % (
                    g["국면"], (max(g["시작"], s) - s).total_seconds() / len_ * 100, (min(g["끝"], e) - max(g["시작"], s)).total_seconds() / len_ * 100,
                    esc("%s · %s" % (g["국면"], _시간_글(int((min(g["끝"], e) - max(g["시작"], s)).total_seconds()))), quote=True))
                    for g in x.get("국면", []) if min(g["끝"], e) > max(g["시작"], s))
                marks = "".join('<span class="tl-cut" style="left:%.2f%%" title="%s"></span>' % ((t - s).total_seconds() / len_ * 100, esc(kind))
                                for t, kind in x["끊김"] if s <= t <= e)
                notes = [t for t, on in (("구간 기록 없음", x["기록 없음"]), ("기간 앞에서 시작함", x["시작"] < 시작), ("진행 중", x["진행 중"])) if on]
                if x["이어받기"] or x["재인계"]:
                    notes.append("이어받기 %d · 재인계 %d" % (x["이어받기"], x["재인계"]))
                shown.add(주소)
                bars += ('<a class="tl-bar" data-card="%s" href="%s" style="left:%.2f%%;width:%.2f%%" title="%s">%s%s</a>' % (
                    esc(주소, quote=True), esc(질의(첫.isoformat(), 끝날.isoformat(), [("card", "%s:%s" % (프, 파일))]), quote=True), left, width,
                    esc("%s · %s%s" % (프, x["제목"], " [" + "] [".join(notes) + "]" if notes else ""), quote=True), segs, marks))
            for x in c["기다림"]:
                s, e = max(x["시작"], 시작), min(x["끝"], 끝)
                bars += '<span class="tl-seg tl-ph-기다림" style="left:%.2f%%;width:%.2f%%" title="%s"></span>' % (
                    pct(s), max(pct(e) - pct(s), 0.3), esc("사람 기다림 · %s" % _시간_글(int((e - s).total_seconds())), quote=True))
            전체 = "%s · %s" % (프, c["제목"])
            lines.append('<div class="tl-line"><a class="tl-label" href="%s" title="%s">%s</a><div class="tl-track">%s</div></div>'
                         % (esc(주소, quote=True), esc(전체, quote=True), esc(전체), bars))
        줄수 += len(lines)
        병렬 = _겹침_최대([(max(x["시작"], 시작), min(x["끝"], 끝), name) for c in cards.values() for x in c["막대"]])[0]
        inner.append('<details%s class="tl-row" data-machine="%s"><summary class="tl-mh"><span class="tl-mn">%s</span><span class="tl-mm">카드 %d · 병렬 최대 %d</span></summary>%s</details>'
                     % ("" if 접힘 else " open", esc(name, quote=True), esc(_기기_글(name)), len(cards), 병렬, "".join(lines)))
    if not 줄수:
        inner.append("<p>이 기간에 돈 카드 없음</p>")
    if 시작 <= now < 끝:
        inner.append('<div class="tl-today" style="left:calc(var(--lab) + (100%% - var(--lab)) * %.4f)"></div>' % (pct(now) / 100))
    lanes = '<section class="tl-lanes" aria-label="기기별 레인"><div class="tl-in">%s</div></section>' % "".join(inner)

    legend = '<div class="tl-lgd">%s<span class="tl-hint">줄을 누르면 그 카드의 선행 화살표만 그린다 · 기기 레인은 접을 수 있다</span></div>' % "".join(
        '<span class="tl-lg"><i class="tl-ph-%s"></i>%s</span>' % kv for kv in (
            ("분석", "분석"), ("설계", "설계"), ("구현", "구현"), ("검증", "검증·인계"), ("기다림", "사람 기다림·오류"), ("멈춤", "멈춤(세션 끝남)")))
    graph = [lanes, legend]
    if 선행:
        graph.append('<p class="tl-legend">실선 결과물 선행 · 점선 겹침 선행</p>')
    graph.append('<p class="tl-run">%s</p>' % esc(_기기_글(running_text(data["동시"], data["전체"]))))
    if 선행:
        돈 = {(x["프로젝트"], x["카드"]) for xs in data["줄"].values() for x in xs}
        waits, seen = [], set()
        for p in 선행:
            for e in p["요소"]:
                for c in e["카드"]:
                    if (p["프로젝트"], c["file"]) not in 돈 and (p["프로젝트"], c["file"]) not in seen:
                        seen.add((p["프로젝트"], c["file"]))
                        href = 상세(p["프로젝트"], c["file"])
                        shown.add(href)
                        waits.append('<a class="tl-wait" href="%s" data-card="%s" title="%s">%s · %s [%s]</a>'
                                     % (esc(href, quote=True), esc(href, quote=True), esc(c["title"], quote=True), esc(p["프로젝트"]), esc(c["title"]), esc(c["상태"])))
        edges, drawn = [], set()
        for p in 선행:
            for a in p["화살표"]:
                앞, 뒤 = 상세(p["프로젝트"], a["앞"]), 상세(p["프로젝트"], a["뒤"])
                if 카드 and 카드 in ("%s:%s" % (p["프로젝트"], a["앞"]), "%s:%s" % (p["프로젝트"], a["뒤"])) and 앞 in shown and 뒤 in shown and (앞, 뒤, a["종류"]) not in drawn:
                    drawn.add((앞, 뒤, a["종류"]))
                    edges.append('<path class="tl-edge tl-%s" data-from="%s" data-to="%s"></path>' % (a["종류"], esc(앞, quote=True), esc(뒤, quote=True)))
        graph.append(('<div class="tl-aside"><h3 class="tl-name">아직</h3>%s</div>' % "".join(waits) if waits else "") + '<svg class="tl-arrows">%s</svg>' % "".join(edges))
    parts.append('<div class="tl-graph">%s</div>' % "".join(graph))
    small = "".join('<span class="tl-dot tl-small" style="left:%.2f%%" title="%s"></span>' % (pct(t), esc("%s %s" % (p, s), quote=True))
                    for t, p, s in data["작은 수정"] if 시작 <= t < 끝 and ok(p))
    parts.append('<h3 class="tl-name">작은 수정</h3><div class="tl-common">%s</div>' % (small or "&nbsp;"))
    for name, why in data["못 읽음"]:
        parts.append("<p>%s — 이력을 못 읽어 막대 없이 그렸다: %s</p>" % (esc(name), esc(why)))
    if 선행:
        parts.append('<details class="tl-els"><summary>요소 목록</summary>' + _요소_목록(선행) + "</details>" + _화살표_스크립트)
    틀 = FILE_TEMPLATE.replace('<a href="board.html">← 보드</a>', '<a href="/">← %s</a>' % 화면이름["home"])
    틀 = 틀.replace("<h1>%(name)s</h1>\n", "", 1)           # 제목은 화면 안 `tl-top` 의 것 하나
    return (틀 % {"name": 화면이름["timeline"], "body": "\n".join(parts)}).replace("</style>", _시간_CSS + ("\n" + _그림_CSS if 선행 else "") + "\n</style>", 1)


# ── 개선 거리 화면 (ISSUE-sc02-e17-improvement-view-2026-11-20, SC-02 E-17) ──
# 소각 커밋이 지운 산출물을 `git log -p` 한 번으로 읽고(이 기계의 모든 참조 + 원격 그림의 `끝`), 신호 폴더는 이력에 없는 것만 보탠다.
# 캐시·색인 없이 요청마다 그 자리에서 읽는다. 읽기만 한다 — 카드를 만들거나 고치지 않는다.
SIGNALS_DIR = os.path.expanduser("~/.stop-loop/signals")   # bin/runner.py 와 같은 자리. 시험은 바꿔 끼운다
IMPROVE_KINDS = ("선택 사항", "측정 추세", "되풀이 신호", "규칙 후보", "지표 제안", "워크플로우 몫", "이연 후보", "사람 결정 미결")
_개선_절 = {      # 산출물 파일 → [(절 제목, 종류, 단계)]. 단계가 None 이면 줄 머리 `- <단계>:` 에서 읽는다
    "02-review.md": [("선택 사항", "선택 사항", "02")],
    "06-design-review.md": [("선택 사항", "선택 사항", "06")],
    "08-qa.md": [("선택 사항", "선택 사항", "08")],
    "09-review-과범위.md": [("선택 사항", "선택 사항", "09")],
    "07-changes.md": [("이연 후보", "이연 후보", "07")],
    "10-handoff.md": [("사람 결정 미결", "사람 결정 미결", None)],
    "11-retro.md": [("3. 되풀이 신호", "되풀이 신호", "11"), ("4. 규칙 후보", "규칙 후보", "11"),
                    ("7. 지표 제안", "지표 제안", "11"), ("8. 워크플로우 몫", "워크플로우 몫", "11")],
}
_짝_초 = 600     # 신호 `when` 뒤 이 초 안에 생긴 같은 카드의 소각 커밋을 그 실행의 소각으로 본다
_표_줄 = re.compile(r"^\|[\s:\-|]+\|?$")


def _없음만(t):
    """「없음」 만 적은 문장(`없음`, `…(「없음」).`)은 지적이 아니다 — 개선 거리의 두 출처가 함께 쓴다."""
    return t.rstrip(".") == "없음" or bool(re.search(r"\(「없음」\)\.?$", t))


def _절_항목(text, 제목):
    """그 `## <제목>` 절의 항목 글 목록(절이 없으면 None). `- ` 줄(이어지는 들여 쓴 줄 포함)과 첫 표의 본문 줄(칸을 ` · ` 로 이음)."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == "## " + 제목), None)
    if start is None:
        return None
    sec = []
    for l in lines[start + 1:]:
        if re.match(r"#{1,6} ", l):
            break
        sec.append(l)
    found, cur, block, 닫힘 = [], None, [], False
    for l in sec:
        if l.startswith("- "):
            cur = [l[2:].strip()]
            found.append(cur)
        elif cur is not None and l.startswith((" ", "\t")) and l.strip():
            cur.append(l.strip())
        else:
            cur = None
        if l.strip().startswith("|") and not 닫힘:
            block.append(l.strip())
        elif block:
            닫힘 = True
    texts = [" ".join(x) for x in found]
    for row in block[1:]:
        if _표_줄.match(row):
            continue
        cells = [c.strip() for c in row.strip("|").split("|")]
        if cells and cells[0] != "없음":
            texts.append(" · ".join(c for c in cells if c))
    return [t for t in texts if t and not t.startswith("{") and not _없음만(t)]


def _분초(ms):
    return "%d분 %d초" % divmod(int(round(ms / 1000)), 60)


def _추세_항목(카드, 단계, m, 때, 경로, 커밋, 곳):
    값 = {"ms": _수(m.get("ms")), "cost": _수(m.get("cost")), "토큰": {k: _수(m.get(k)) for k in TOKEN_KEYS}}
    t = 값["토큰"]
    원문 = "시간 %s · 비용 $%.2f · 토큰 입력 %s · 캐시 쓰기 %s · 캐시 읽기 %s · 출력 %s" % (
        _분초(값["ms"]), 값["cost"], t[TOKEN_KEYS[0]], t[TOKEN_KEYS[1]], t[TOKEN_KEYS[2]], t[TOKEN_KEYS[3]])
    return {"종류": "측정 추세", "카드": 카드, "단계": 단계, "때": 때, "원문": 원문, "경로": 경로, "커밋": 커밋,
            "곳": 곳, "값": 값, "카드됨": None}


def _항목(종류, 카드, 단계, 때, 원문, 경로, 커밋, 곳):
    return {"종류": 종류, "카드": 카드, "단계": 단계, "때": 때, "원문": 원문, "경로": 경로, "커밋": 커밋,
            "곳": 곳, "값": None, "카드됨": None}


def _정리(s):
    return " ".join(s.split())


def _소각_산출물(out, 글, 카드, 때, sha):
    """소각 커밋 하나가 지운 산출물 `글`({경로: 글})을 항목으로 풀어 out 에 더한다. 그 소각의 측정 추세 단계 집합을 돌려준다."""
    폴더들 = {}
    for path, text in 글.items():
        m = re.fullmatch(r"\.workflow/([^/]+)/(.+)", path)
        if m:
            폴더들.setdefault(m.group(1), {})[m.group(2)] = text
    단계들 = set()
    for 폴더, files in 폴더들.items():
        if "state.json" not in files:
            out["기록못찾음"].append((카드, sha))
        else:
            try:
                metrics = json.loads(files["state.json"]).get("metrics") or {}
                if not isinstance(metrics, dict):
                    raise ValueError
            except (ValueError, AttributeError):
                out["못읽음"].append((".workflow/%s/state.json" % 폴더, sha, "state.json 을 읽지 못함(JSON 깨짐)"))
                metrics = {}
            for st in sorted(metrics):
                if isinstance(metrics[st], dict):
                    단계들.add(st)
                    out["항목"].append(_추세_항목(카드, st, metrics[st], 때, ".workflow/%s/state.json" % 폴더, sha, "이력"))
        for name, specs in _개선_절.items():
            if name not in files:
                continue
            경로 = ".workflow/%s/%s" % (폴더, name)
            for 절, 종류, 단계 in specs:
                texts = _절_항목(files[name], 절)
                if texts is None:
                    out["못읽음"].append((경로, sha, 절))
                    continue
                for t in texts:
                    st = 단계
                    if st is None:
                        m = re.match(r"(\d{2}):\s*(.*)", t)
                        st, t = (m.group(1), m.group(2)) if m else ("", t)
                    out["항목"].append(_항목(종류, 카드, st, 때, t, 경로, sha, "이력"))
    return 단계들


def _이력_읽기(project, 끝, out):
    """소각 커밋 목록 [{카드, ct, sha, 단계들}]. 못 읽으면 out["이력오류"] 를 채우고 빈 목록."""
    pathspec = [":(glob).workflow/*/" + n for n in list(_개선_절) + ["state.json"]]
    fmt = "\x1e%H\x1f%ad\x1f%(trailers:key=Loop-Issue,valueonly)\x1f%ct"

    def run(tip):
        return _git_읽기(project, "-c", "core.quotepath=false", "log", "--all", *([tip] if tip else []),
                        "--no-renames", "--grep=^Loop-Stage: burn$", "--diff-filter=D", "-p",
                        "--date=format:%Y-%m-%d", "--format=" + fmt, "--", *pathspec)
    try:
        r = run(끝)
        if r.returncode != 0 and 끝:
            out["원격"] = ("원격을 읽지 못함 — 이 기기의 이력과 신호 폴더만", "원격 끝 커밋이 이 기기에 없음")
            r = run("")
    except (OSError, subprocess.SubprocessError):
        out["이력오류"] = "이력을 읽지 못함"
        return []
    if r.returncode != 0:
        out["이력오류"] = "이력을 읽지 못함"
        return []
    burns = []
    for rec in r.stdout.split("\x1e")[1:]:
        f = rec.split("\x1f", 3)
        if len(f) != 4:
            continue
        sha, 때, 카드, rest = f[0], f[1], f[2].strip(), f[3]
        ct, _, diff = rest.partition("\n")
        글, path, hunk = {}, None, False
        for line in diff.splitlines():
            m = re.match(r"diff --git a/(\.workflow/\S+) b/", line)
            if m:
                path, hunk = m.group(1), False
                글[path] = []
            elif line.startswith("@@"):
                hunk = True
            elif hunk and path and line.startswith("-"):
                글[path].append(line[1:])
        out["소각수"] += 1
        단계들 = _소각_산출물(out, {p: "\n".join(v) for p, v in 글.items()}, 카드, 때, sha)
        burns.append({"카드": 카드, "ct": int(ct.strip() or 0), "sha": sha, "단계들": 단계들})
    return burns


def _신호_읽기(project, burns, out):
    """신호 폴더의 두 파일에서 이력에 없는 것만 out["항목"] 에 보탠다."""
    base = os.path.join(SIGNALS_DIR, os.path.basename(os.path.abspath(project)))
    opt, trend = base + "__options.md", base + "__trend.jsonl"
    out["신호"] = [(opt, os.path.exists(opt)), (trend, os.path.exists(trend))]
    있는 = {(x["카드"], x["단계"], _정리(x["원문"])) for x in out["항목"] if x["종류"] == "선택 사항"}
    try:
        with open(opt, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    except (OSError, UnicodeDecodeError):
        lines = []
        out["못읽음"].append((opt, "", "읽지 못함"))
    카드, 때 = None, ""
    for n, line in enumerate(lines, 1):
        h = re.fullmatch(r"## (.+) · (\d{4}-\d{2}-\d{2})\s*", line)
        m = re.fullmatch(r"- (\d{2}) — (.+)", line)
        if h:
            카드, 때 = h.group(1), h.group(2)
        elif line.startswith("- "):
            if not (m and 카드):
                out["못읽음"].append(("%s:%d" % (opt, n), "", "형식이 다른 줄"))
            elif not _없음만(m.group(2).strip()) and (카드, m.group(1), _정리(m.group(2))) not in 있는:
                있는.add((카드, m.group(1), _정리(m.group(2))))
                out["항목"].append(_항목("선택 사항", 카드, m.group(1), 때, m.group(2).strip(), opt, "", "신호"))
    try:
        with open(trend, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    except (OSError, UnicodeDecodeError):
        lines = []
        out["못읽음"].append((trend, "", "읽지 못함"))
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            when, issue, stages = d["when"], d["issue"], d["stages"]
            ts = dt.datetime.fromisoformat(when).timestamp()
            if not isinstance(stages, dict) or not isinstance(issue, str):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            out["못읽음"].append(("%s:%d" % (trend, n), "", "형식이 다른 줄"))
            continue
        짝 = [x for x in burns if x["카드"] == issue and 0 <= x["ct"] - ts <= _짝_초]
        짝 = min(짝, key=lambda x: x["ct"]) if 짝 else None
        for st in sorted(stages):
            if isinstance(stages[st], dict) and not (짝 and st in 짝["단계들"]):
                out["항목"].append(_추세_항목(issue, st, stages[st], when[:10], trend, "", "신호"))


def read_improvements(item, 그림):
    """한 프로젝트의 개선 거리 — {"항목", "못읽음": [(경로, 커밋, 까닭)], "기록못찾음": [(카드, 커밋)], "소각수", "이력오류",
    "신호": [(경로, 있음)], "원격": (상태 글, 까닭)}. 예외를 내지 않는다. `그림` 은 `REMOTE` 의 그림 또는 None."""
    out = {"항목": [], "못읽음": [], "기록못찾음": [], "소각수": 0, "이력오류": "", "신호": [], "원격": ("", "")}
    끝 = ""
    if 그림 is None:
        out["원격"] = ("원격을 아직 읽지 않음 — 이 기기의 이력과 신호 폴더만", "")
    elif 그림.get("없음"):
        out["원격"] = ("원격 없음", "")
    elif 그림.get("오류"):
        out["원격"] = ("원격을 읽지 못함 — 이 기기의 이력과 신호 폴더만", 그림["오류"])
    else:
        끝 = 그림.get("끝") or ""
        out["원격"] = ("원격 끝 " + 끝, "") if 끝 else ("원격 없음", "")
    try:
        burns = _이력_읽기(item["path"], 끝, out)
        _신호_읽기(item["path"], burns, out)
    except Exception as e:          # noqa: BLE001 — 읽기가 화면을 죽이지 않는다
        out["이력오류"] = out["이력오류"] or "읽지 못함: %s" % (str(e) or type(e).__name__)
    return out


회고_몫 = ("되풀이 신호", "규칙 후보", "지표 제안", "워크플로우 몫")     # 탭 「회고 몫」 이 묶는 네 종류
개선_탭 = (("전부", ""), ("선택 사항", "선택 사항"), ("이연 후보", "이연 후보"), ("사람 결정 미결", "사람 결정 미결"), ("회고 몫", "회고 몫"))
개선_쪽 = 20                                                         # 한 쪽에 보이는 묶인 줄 수


def filter_improvements(항목들, qs):
    """(거른 목록, 까닭). 카드 c·단계 st·종류 k·카드 안 됨 made=0 은 겹쳐 걸고, 기간 from·to 는 측정 추세 항목에만 건다. 종류 k 는 여덟 가운데 하나 또는 「회고 몫」(네 종류)."""
    def g(k):
        return (qs.get(k) or [""])[0].strip()
    if g("k") and g("k") not in IMPROVE_KINDS + ("회고 몫",):
        return [], "종류는 여덟 가운데 하나"
    cond, 까닭 = _조건(qs)
    if 까닭:
        return [], 까닭
    종류들 = 회고_몫 if g("k") == "회고 몫" else ((g("k"),) if g("k") else None)
    out = []
    for x in 항목들:
        if (g("c") and x["카드"] != g("c")) or (g("st") and x["단계"] != g("st")) or (종류들 and x["종류"] not in 종류들):
            continue
        if g("made") == "0" and x["카드됨"]:
            continue
        if x["종류"] == "측정 추세" and ((cond["시작"] and x["때"] < cond["시작"]) or (cond["끝"] and x["때"] > cond["끝"])):
            continue
        out.append(x)
    return out, ""


def 줄_열쇠(원문):
    """묶인 줄의 열쇠 — 공백을 정리한 원문의 해시 앞 16자. 화면의 `g` 값이다."""
    return hashlib.sha1(_정리(원문).encode("utf-8")).hexdigest()[:16]


_되풀이_꼴1 = re.compile(r"`?([^\s@`]+)@`?([^\s:`(),]+)")
_되풀이_꼴2 = re.compile(r"파일 `([^`]+)` — \d+회")


def _되풀이_열쇠(x):
    """되풀이 신호 항목이 「종류@파일」 또는 「파일 `경로` — n회」 꼴이면 (단계, 종류, 파일), 아니면 None. 단계는 파일 이름 앞 두 자리 숫자."""
    if x["종류"] != "되풀이 신호":
        return None
    글 = _정리(x["원문"])
    m = _되풀이_꼴1.match(글)
    종류, 파일 = (m.group(1), m.group(2)) if m else ("", "")
    if not m:
        m = _되풀이_꼴2.match(글)
        if not m:
            return None
        파일 = m.group(1)
    단계 = re.match(r"\d\d", os.path.basename(파일))
    return (단계.group(0) if 단계 else "", 종류, 파일)


def 묶음_글(x):
    """같은 줄인지 가르는 글 — 되풀이 신호 두 꼴은 (단계, 종류, 파일), 나머지는 공백을 정리한 원문."""
    k = _되풀이_열쇠(x)
    return "되풀이 신호\x1f" + "\x1f".join(k) if k else _정리(x["원문"])


def _줄_제목(x):
    k = _되풀이_열쇠(x)
    if not k:
        return _정리(x["원문"])
    단계, 종류, 파일 = k
    앞 = ("%s %s — " % (단계, STAGE_NAME[단계])) if 단계 in STAGE_NAME else ""
    return 앞 + (("%s 지적이 %s 에서 되풀이" % (종류, 파일)) if 종류 else ("%s 에서 지적이 되풀이" % 파일))


def _빈_지적(x):
    """「열쇠 단위: 없음 · 해당 없음」 — 콜론 뒤 값이 「없음」·「해당 없음」뿐인 지적."""
    return all(t.strip() in ("없음", "해당 없음") for t in _정리(x["원문"]).rpartition(":")[2].split("·"))


def 개선_후보(항목들):
    """개선 후보로 세는 항목 — 측정 추세(사용량·품질의 몫)와 빈 지적은 뺀다. 측정 추세 접힘은 원 항목을 따로 읽는다."""
    return [x for x in 항목들 if x["종류"] != "측정 추세" and not _빈_지적(x)]


def group_improvements(항목들, 정렬="되풀이"):
    """같은 묶음 글(되풀이 신호 두 꼴은 단계·종류·파일, 나머지는 공백을 정리한 원문)인 항목끼리 한 줄로. 횟수는 서로 다른 카드 수. 정렬 「되풀이」 는 횟수 큰 순, 「최근」 은 마지막 때가 늦은 순(같으면 횟수 큰 순)."""
    묶음 = {}
    for x in 개선_후보(항목들):
        묶음.setdefault(묶음_글(x), []).append(x)
    rows = [{"원문": _정리(v[0]["원문"]), "제목": _줄_제목(v[0]), "열쇠": 줄_열쇠(k), "횟수": len({x["카드"] for x in v}), "카드들": sorted({x["카드"] for x in v}), "낱낱": v,
             "종류들": list(dict.fromkeys(x["종류"] for x in v)), "단계들": sorted({x["단계"] for x in v if x["단계"]}),
             "마지막": max(x["때"] for x in v)}
            for k, v in 묶음.items()]
    rows.sort(key=lambda r: -r["횟수"])
    if 정렬 == "최근":
        rows.sort(key=lambda r: r["마지막"], reverse=True)
    return rows


def 탭_개수(항목들, qs):
    """[(탭 이름, k 값, 묶인 줄 수)] 다섯 — 그 탭을 눌렀을 때 보일 줄 수(다른 거르개는 걸린 채)."""
    return [(이름, k, len(group_improvements(filter_improvements(항목들, dict(qs, k=[k]))[0]))) for 이름, k in 개선_탭]


def _출처_적힘(x):
    """카드됨 항목의 원문 앞 20자(공백 정리)가 그 카드 파일 글에 실제로 있으면 참 — 카드가 이 지적을 담았다는 뜻."""
    return bool(x["카드됨"]) and _정리(x["원문"])[:20] in x.get("카드글", "")


def 비슷한_카드(전체, 줄, 프로젝트):
    """그 줄과 같은 묶음인 항목(거르기 전 전체)이 이미 카드됨이고 그 카드 파일에 원문이 적혀 있고 줄에 고를 항목이 남았으면 {"카드": (파일, 제목, 상태), "곳": 프로젝트 이름, "남은": [항목 열쇠]}, 아니면 None."""
    남은 = [항목_열쇠(x) for x in 줄["낱낱"] if not x["카드됨"] and x["단계"]]
    if not 남은:
        return None
    for x in 전체:
        if _출처_적힘(x) and 줄_열쇠(묶음_글(x)) == 줄["열쇠"]:
            return {"카드": x["카드됨"], "곳": x.get("카드됨곳") or 프로젝트, "남은": 남은}
    return None


def mark_made(항목들, cards, bodies, project, 끝=""):
    """항목의 「카드됨」을 채운다 — 카드 「## 배경」 절 안 한 줄에 카드 이름과 단계 번호(앞뒤가 숫자·영문자가 아닌 것)가 함께 있을 때 —
    단계 번호는 카드 이름을 뺀 나머지 글에서만 찾는다(이름 끝 날짜 `-11-01` 에 맞지 않게).
    `bodies` 에 없는 카드는 이 기계 파일을, 이 기계에 없는 원격 카드는 `끝` 의 `git show` 로 읽는다. 못 읽은 카드 파일 목록을 돌려준다."""
    못 = []
    배경 = []
    for c in cards:
        body = bodies.get(c["file"])
        if body is None:
            try:
                with open(os.path.join(project, "docs", "backlog", c["file"]), encoding="utf-8") as f:
                    body = f.read()
            except (OSError, UnicodeDecodeError):
                body = None
                if c.get("출처") == "원격" and 끝:
                    r = _git_읽기(project, "show", "%s:docs/backlog/%s" % (끝, c["file"]))
                    body = r.stdout if r.returncode == 0 else None
        if body is None:
            못.append(c["file"])
            continue
        lines, on = [], False
        for l in body.splitlines():
            if re.match(r"#{1,6} ", l):
                on = l.strip() == "## 배경"
            elif on:
                lines.append(l)
        배경.append((c, lines, _정리(body)))
    for x in 항목들:
        if not x["단계"]:
            continue
        step = re.compile(r"(?<![0-9A-Za-z])%s(?![0-9A-Za-z])" % re.escape(x["단계"]))
        for c, lines, 글 in 배경:
            if any(x["카드"] in l and step.search(l.replace(x["카드"], " ")) for l in lines):
                x["카드됨"] = (c["file"], c["title"], c["상태"])
                x["카드글"] = 글
                break
    return 못


def trend_lines(항목들, 프로젝트, qs):
    """측정 추세 줄 — 단계마다 {단계, 수, 평균ms, 평균cost, 평균토큰, 첫, 끝, 고리}. 기록 값을 그대로 쓴다(보정 없음)."""
    cond, 까닭 = _조건(qs)
    if 까닭:
        return []
    by = {}
    for x in 항목들:
        if x["종류"] != "측정 추세" or not x.get("값"):
            continue
        if (cond["시작"] and x["때"] < cond["시작"]) or (cond["끝"] and x["때"] > cond["끝"]):
            continue
        by.setdefault(x["단계"], []).append(x)
    q = [("p", 프로젝트)] + [(k, v) for k, v in (("from", cond["시작"]), ("to", cond["끝"])) if v]
    rows = []
    for st in sorted(by):
        v = sorted(by[st], key=lambda x: x["때"])
        vals = [(x["값"]["ms"], x["값"]["cost"], sum(x["값"]["토큰"].values())) for x in v]
        n = len(vals)
        rows.append({"단계": st, "수": n, "평균ms": sum(a for a, _, _ in vals) / n, "평균cost": sum(a for _, a, _ in vals) / n,
                     "평균토큰": sum(a for _, _, a in vals) / n, "첫": vals[0], "끝": vals[-1],
                     "고리": "/signals?" + urllib.parse.urlencode(q)})
    return rows


def _항목_html(x, 프로젝트):
    esc = html.escape
    첫 = x["원문"].splitlines()[0] if x["원문"] else ""
    첫 = 첫 if len(첫) <= 60 else 첫[:60] + "…"
    sha = x["커밋"][:7] if x["커밋"] else "커밋 없음(신호 폴더)"
    body = ["<p>%s</p>" % esc(x["원문"]), "<p>종류 %s · 출처 %s · 커밋 %s</p>" % (esc(_종류_글(x["종류"])), esc(x["경로"]), esc(sha))]
    안내 = ""
    if x["카드됨"]:
        file, title, 상태 = x["카드됨"]
        body.append('<p><a href="/p/%s/%s">카드됨 — %s(%s)</a></p>' % (
            urllib.parse.quote(x.get("카드됨곳") or 프로젝트, safe=""), urllib.parse.quote(file, safe=""), esc(title), esc(상태)))
    elif not x["단계"]:
        안내 = "<p>단계가 없어 카드로 이을 수 없음</p>"
    return 안내 + '<details class="항목"><summary>%s · 카드 %s · 단계 %s · 때 %s · 경로 %s</summary>%s</details>' % (
        esc(첫), esc(x["카드"]), esc(x["단계"]), esc(x["때"]), esc(x["경로"]), "".join(body))


# 색은 THEME 변수만 쓴다. 두 칸은 flex-wrap 이라 좁은 폭에서는 오른쪽 상자가 아래로 내려간다.
_개선_CSS = """main { max-width:none; }
.개선 { display:flex; flex-wrap:wrap; gap:16px; align-items:flex-start; font-size:14px; line-height:1.5; color:var(--fg); }
.개선 a { font-size:inherit; color:var(--blue); }
.개선 .본 { flex:999 1 560px; min-width:0; display:flex; flex-direction:column; gap:14px; }
.개선 .머리 { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
.개선 .머리 h1 { margin:0; font-size:20px; font-weight:700; color:var(--fg); }
.개선 .머리 a { color:var(--dim); font-size:13px; }
.개선 .작은 { font-size:12px; color:var(--dim); margin:0; }
.개선 .줄머리 { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
.개선 .탭 { display:flex; flex-wrap:wrap; gap:2px; padding:2px; border-radius:8px; background:var(--soft); }
.개선 .탭 a { min-height:30px; display:inline-flex; align-items:center; padding:0 10px; border-radius:6px; font-size:12px; color:var(--ink2); }
.개선 .탭 a.on { background:var(--panel); font-weight:600; }
.개선 .거르개 { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-left:auto; font-size:12px; color:var(--dim); }
.개선 .거르개 label { display:inline-flex; align-items:center; gap:6px; }
.개선 select, .개선 input[type=text] { min-height:32px; border:1px solid var(--line); border-radius:8px; background:var(--panel); color:var(--fg); font:inherit; font-size:12px; padding:0 8px; }
.개선 button { min-height:32px; padding:0 12px; border:1px solid var(--line); border-radius:8px; background:var(--panel); color:var(--fg); font:inherit; font-size:12px; }
.개선 button.주 { border:0; background:var(--fg); color:var(--bg); font-weight:600; }
.개선 .목록 { background:var(--panel); border:1px solid var(--line); border-radius:var(--r-panel); }
.개선 .도구 { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:10px 16px; border-bottom:1px solid var(--line2); font-size:12px; color:var(--dim); }
.개선 .줄 { display:flex; align-items:center; gap:12px; padding:12px 16px; border-top:1px solid var(--line2); }
.개선 .도구 + .줄 { border-top:0; }
.개선 .줄.고름 { background:var(--blue-bg); }
.개선 .줄 input[type=checkbox] { width:16px; height:16px; flex-shrink:0; }
.개선 .줄 .빈칸 { width:16px; flex-shrink:0; }
.개선 .줄몸 { flex-grow:1; min-width:0; }
.개선 .줄제목 { display:block; font-weight:500; color:var(--fg); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.개선 .줄메타 { display:flex; flex-wrap:wrap; gap:6px; margin-top:4px; font-size:11px; color:var(--dim); }
.개선 .배지 { padding:1px 6px; border-radius:4px; }
.개선 .bg-gray { background:var(--gray-bg); color:var(--ink2); } .개선 .bg-blue { background:var(--blue-bg); color:var(--blue); }
.개선 .bg-orange { background:var(--orange-bg); color:var(--orange); } .개선 .bg-teal { background:var(--teal-bg); color:var(--teal); }
.개선 .횟수 { flex-shrink:0; min-width:48px; text-align:right; font:600 13px var(--mono); }
.개선 .이어짐 { flex-shrink:1; width:120px; min-width:0; font-size:12px; text-align:right; color:var(--dim); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.개선 .발 { padding:10px 16px; border-top:1px solid var(--line2); font-size:12px; color:var(--dim); }
.개선 details { font-size:12px; color:var(--dim); }
.개선 summary { cursor:pointer; min-height:32px; }
.개선 .자세히상자 { flex:1 1 320px; max-width:420px; min-width:0; background:var(--panel); border:1px solid var(--line); border-radius:var(--r-panel); padding:20px; display:flex; flex-direction:column; gap:14px; }
.개선 .자세히상자 h2 { margin:0; font-size:16px; font-weight:600; line-height:1.45; overflow-wrap:anywhere; }
.개선 .자세히상자 ol { margin:0; padding-left:18px; display:flex; flex-direction:column; gap:6px; font-size:13px; }
.개선 .자세히상자 details details { margin:4px 0 4px 8px; overflow-wrap:anywhere; }
.개선 .제안 { padding:12px; border:1px dashed var(--blue); border-radius:var(--r-card); background:var(--blue-bg); display:flex; flex-direction:column; gap:6px; }
.개선 .제안 .표지 { font-size:11px; font-weight:600; color:var(--blue); }
.개선 .제안 .단추줄 { display:flex; gap:6px; align-items:center; }
.개선 .제안 form { margin:0; }
.개선 .제안 button.주 { background:var(--blue); color:var(--panel); }
.개선 .제안 .따로 { min-height:32px; display:inline-flex; align-items:center; padding:0 12px; border:1px solid var(--line); border-radius:8px; background:var(--panel); color:var(--fg); font-size:12px; }"""
_종류_배지 = {"선택 사항": "bg-gray", "측정 추세": "bg-gray", "이연 후보": "bg-blue", "사람 결정 미결": "bg-orange",
          "되풀이 신호": "bg-teal", "규칙 후보": "bg-teal", "지표 제안": "bg-teal", "워크플로우 몫": "bg-teal"}


def _정수(값, 기본=1):
    try:
        return int(값)
    except (TypeError, ValueError):
        return 기본


def render_improve(item, 결과, qs, 까닭, 쓰기=None):
    """개선 거리 화면 HTML(목업 `design/proposals/ui-2026-11-22/Improve.dc.html`). `결과` 는 `read_improvements` 의 사전에
    거른 「항목」·거르기 전 「전체항목」·「전체수」·「작업목록오류」를 얹은 것. 값은 모두 escape 한다."""
    esc = html.escape
    name = item["name"]

    def v(k):
        return (qs.get(k) or [""])[0]

    def 주소(**바꿈):
        q = {k: v(k) for k in ("c", "st", "k", "made", "from", "to", "sort") if v(k)}
        for k, 값 in 바꿈.items():
            if 값 in (None, ""):
                q.pop(k, None)
            else:
                q[k] = 값
        return esc("/p/%s/improve" % urllib.parse.quote(name, safe="") + ("?" + urllib.parse.urlencode(q) if q else ""), quote=True)
    쓰기 = 쓰기 or {}
    parts = ["<p>%s</p>" % esc(c) for c in ([쓰기["결과줄"]] if 쓰기.get("결과줄") else []) + 쓰기.get("까닭들", [])]
    항목 = 결과["항목"]
    전체 = 결과.get("전체항목", 항목)
    줄들 = group_improvements(항목, "최근" if v("sort") == "최근" else "되풀이")
    후보 = 개선_후보(항목)
    실행수 = len({(x["카드"], x["커밋"] or x["경로"]) for x in 후보})
    parts.append('<style>%s</style><div class="개선"><div class="본">' % _개선_CSS)
    parts.append('<header class="머리"><a href="/p/%s/board.html">%s</a><span aria-hidden="true">/</span><h1>%s</h1>'
                 '<span class="작은">%d 실행에서 모은 지적 %d건 → 같은 지적끼리 묶어 %d줄</span></header>'
                 % (urllib.parse.quote(name, safe=""), esc(name), esc(화면이름["improve"]), 실행수, len(후보), len(줄들)))
    읽음 = ["이력을 읽지 못함" if 결과["이력오류"] else "이력 소각 커밋 %d건" % 결과["소각수"]]
    읽음 += ["신호 폴더 %s %s" % (p, "있음" if ok else "없음") for p, ok in 결과["신호"]]
    상태글, 원격까닭 = 결과["원격"]
    읽음.append(상태글 + (" (%s)" % 원격까닭 if 원격까닭 else ""))
    parts.append(_프로젝트_탭(name, "improve"))
    parts.append('<details class="도움말"><summary>도움말</summary><p class="작은">읽은 것 — %s</p></details>' % esc(" · ".join(읽음)))
    if 결과["이력오류"] and 결과["이력오류"] != "이력을 읽지 못함":
        parts.append('<p class="작은">%s</p>' % esc(결과["이력오류"]))
    if 결과.get("작업목록오류"):
        parts.append('<p class="작은">%s</p>' % esc(결과["작업목록오류"]))
    탭들 = "".join('<a href="%s"%s>%s %d</a>' % (주소(k=k, g=None, pg=None), ' class="on"' if v("k") == k else "", esc(_종류_글(이름)), n)
                  for 이름, k, n in 탭_개수(전체, qs))
    숨은 = "".join('<input type="hidden" name="%s" value="%s">' % (k, esc(v(k), quote=True)) for k in ("c", "st", "k", "from", "to") if v(k))
    parts.append('<div class="줄머리"><div class="탭" role="tablist" aria-label="종류">%s</div>'
                 '<form method="get" action="/p/%s/improve" class="거르개">%s<label><input type="checkbox" name="made" value="0"%s>아직 카드 안 된 것만</label>'
                 '<label>정렬 <select name="sort"><option value="되풀이"%s>되풀이 많은 순</option><option value="최근"%s>최근 순</option></select></label>'
                 '<button type="submit">보기</button></form></div>'
                 % (탭들, urllib.parse.quote(name, safe=""), 숨은, " checked" if v("made") == "0" else "",
                    "" if v("sort") == "최근" else " selected", " selected" if v("sort") == "최근" else ""))
    골라 = "".join('<option value="%s">%s</option>' % (esc(n, quote=True), esc(n)) for n in 쓰기.get("프로젝트들") or [name])
    parts.append('<form method="post" action="/p/%s/make-card" id="고르기" class="목록"><div class="도구">'
                 '<label>제목 <input type="text" name="title"></label> <label>파일 이름 <input type="text" name="slug"></label> '
                 '<label>만들 프로젝트 <select name="target"><option value="">처음 값(항목이 나온 프로젝트)</option>%s</select></label> '
                 '<button type="submit" class="주">고른 것으로 카드 만들기</button></div>' % (urllib.parse.quote(name, safe=""), 골라))
    고른줄 = None
    if 까닭:
        parts.append('<p class="발">%s</p>' % esc(까닭))
    elif not 줄들:
        parts.append('<p class="발">%s</p>' % ("개선 거리 없음" if not 결과.get("전체수") else "거르개에 맞는 개선 거리 없음"))
    else:
        쪽수 = (len(줄들) + 개선_쪽 - 1) // 개선_쪽
        pg = min(max(_정수(v("pg")), 1), 쪽수)
        시작 = (pg - 1) * 개선_쪽
        쪽 = 줄들[시작:시작 + 개선_쪽]
        고른줄 = next((r for r in 줄들 if r["열쇠"] == v("g")), 쪽[0])
        이어짐 = {}
        for x in 전체:
            if _출처_적힘(x):
                이어짐.setdefault(줄_열쇠(묶음_글(x)), x)
        for r in 쪽:
            골 = [항목_열쇠(x) for x in r["낱낱"] if not x["카드됨"] and x["단계"]]
            칸 = ('<input type="checkbox" name="pick" value="%s" aria-label="고르기">' % ",".join(골)) if 골 else '<span class="빈칸"></span>'
            이 = 이어짐.get(r["열쇠"])
            링크 = ('<a href="/p/%s/%s">%s(%s)</a>' % (urllib.parse.quote(이.get("카드됨곳") or name, safe=""), urllib.parse.quote(이["카드됨"][0], safe=""),
                                                    esc(이["카드됨"][1]), esc(이["카드됨"][2]))) if 이 else "카드 아님"
            배지 = "".join('<span class="배지 %s">%s</span>' % (_종류_배지.get(k, "bg-gray"), esc(_종류_글(k))) for k in r["종류들"])
            parts.append('<div class="줄%s">%s<div class="줄몸"><a class="줄제목" href="%s">%s</a><div class="줄메타">%s<span>%s</span><span>· 마지막 %s</span></div></div>'
                         '<span class="횟수">×%d</span><span class="이어짐">%s</span></div>'
                         % (" 고름" if r is 고른줄 else "", 칸, 주소(g=r["열쇠"], pg=pg if pg > 1 else None), esc(r["제목"].splitlines()[0] if r["제목"] else ""),
                            배지, esc(" · ".join(r["단계들"])), esc(r["마지막"]), r["횟수"], 링크))
        발 = "%d줄 중 %d–%d줄" % (len(줄들), 시작 + 1, 시작 + len(쪽))
        if pg < 쪽수:
            발 += ' · <a href="%s">다음 %d줄</a>' % (주소(pg=pg + 1, g=None), 개선_쪽)
        parts.append('<div class="발">%s</div>' % 발)
    parts.append("</form>")
    줄 = trend_lines(항목, name, qs) if not v("k") else []
    if 줄:
        parts.append('<details class="추세"><summary>측정 추세 — 단계마다 평균·첫값·끝값</summary>%s</details>' % "".join(
            '<p><a href="%s">단계 %s · 실행 %d · 평균 시간 %s · 비용 $%.2f · 토큰 %s · 첫 %s $%.2f 토큰 %s · 마지막 %s $%.2f 토큰 %s</a></p>' % (
                esc(t["고리"]), esc(t["단계"]), t["수"], _분초(t["평균ms"]), t["평균cost"], "{:,.0f}".format(t["평균토큰"]),
                _분초(t["첫"][0]), t["첫"][1], "{:,.0f}".format(t["첫"][2]),
                _분초(t["끝"][0]), t["끝"][1], "{:,.0f}".format(t["끝"][2])) for t in 줄))
    못 = len(결과["못읽음"]) + len(결과["기록못찾음"])
    if 못:
        parts.append('<details class="못읽음"><summary>형식이 맞지 않는 기록 %d — 접어 둠</summary><ul>%s%s</ul></details>' % (
            못, "".join("<li>%s (커밋 %s) — %s</li>" % (esc(p), esc(c[:7] or "없음"), esc(_종류_글(w))) for p, c, w in 결과["못읽음"]),
            "".join("<li>기록 못 찾음 — 카드 %s (커밋 %s)</li>" % (esc(c), esc(s[:7])) for c, s in 결과["기록못찾음"])))
    parts.append("</div>")
    if 고른줄:
        카드별 = {}
        for x in 고른줄["낱낱"]:
            카드별.setdefault(x["카드"], []).append(x)
        실행 = "".join('<li><details class="실행"><summary>%s — 단계 %s · %d번</summary>%s</details></li>' % (
            esc(c), esc(" · ".join(sorted({x["단계"] for x in xs if x["단계"]})) or "—"), len(xs), "".join(_항목_html(x, name) for x in xs))
            for c, xs in 카드별.items())
        상자 = ('<aside class="자세히상자" aria-label="고른 줄 자세히"><span class="작은">고른 줄</span><h2>%s</h2>'
               '<div>%s · 카드 %d곳에서 %d번</div><ol>%s</ol>' % (
                   esc(고른줄["제목"].splitlines()[0] if 고른줄["제목"] else ""), esc(" · ".join(_종류_글(k) for k in 고른줄["종류들"])), 고른줄["횟수"], len(고른줄["낱낱"]), 실행))
        비 = 비슷한_카드(전체, 고른줄, name)
        if 비:
            file, title, 상태 = 비["카드"]
            상자 += ('<div class="제안"><span class="표지">시스템 제안 · 비슷한 카드</span><a href="/p/%s/%s">%s (%s)</a>'
                   '<span class="작은">이미 이 지적을 담은 카드가 있다. 새로 만들지 말고 잇기를 권한다.</span><div class="단추줄">'
                   '<form method="post" action="/p/%s/link-card"><input type="hidden" name="pick" value="%s"><input type="hidden" name="card" value="%s">'
                   '<input type="hidden" name="at" value="%s"><button type="submit" class="주">이 카드에 잇기</button></form>'
                   '<a class="따로" href="#고르기">따로 만들기</a></div></div>' % (
                       urllib.parse.quote(비["곳"], safe=""), urllib.parse.quote(file, safe=""), esc(title), esc(상태),
                       urllib.parse.quote(name, safe=""), ",".join(비["남은"]), esc(file, quote=True), esc(비["곳"], quote=True)))
        parts.append(상자 + "</aside>")
    parts.append("</div>")
    틀 = FILE_TEMPLATE.replace("<h1>%(name)s</h1>\n", "", 1)
    return 틀 % {"name": 화면이름["improve"] + " — " + esc(name), "body": "\n".join(parts)}


def 항목_열쇠(x):
    """항목 하나의 열쇠 — 화면의 고르기 칸 값과 POST 처리가 같은 함수로 만든다."""
    글 = "\x1f".join(str(x[k]) for k in ("종류", "카드", "단계", "경로", "커밋", "원문"))
    return hashlib.sha1(글.encode("utf-8")).hexdigest()[:16]


def _카드됨_잇기(item, 항목들, items):
    """그 프로젝트 카드로 `mark_made` 를 먼저 부르고, 아직 안 이어진 항목은 보는 목록의 다른 프로젝트 카드로 차례로 잇는다
    (다른 프로젝트 카드로 이어지면 `x["카드됨곳"]` 에 그 이름). 이 프로젝트 작업목록을 못 읽었으면 True."""
    못읽음 = False
    for p in [item] + [q for q in items if q["name"] != item["name"]]:
        남은 = [x for x in 항목들 if not x["카드됨"]]
        if not 남은:
            break
        그림 = REMOTE.get(os.path.abspath(p["path"]))
        try:
            backlog = os.path.join(p["path"], "docs", "backlog")
            if not os.path.isdir(backlog):
                raise OSError(backlog)
            cards = merge_cards(read_cards(backlog), 그림, p.get("기기", ""))
            if mark_made(남은, cards, {}, p["path"], (그림 or {}).get("끝") or "") and p is item:
                못읽음 = True
        except OSError:
            못읽음 = 못읽음 or p is item
        if p is not item:
            for x in 남은:
                if x["카드됨"]:
                    x["카드됨곳"] = p["name"]
    return 못읽음


def _개선_읽기(item, items):
    결과 = _잠깐저장("improve", [item], {}, lambda: read_improvements(item, REMOTE.get(os.path.abspath(item["path"]))))
    오류 = "작업목록을 읽지 못함 — 카드됨을 잇지 못했다" if _카드됨_잇기(item, 결과["항목"], items) else ""
    return 결과, 오류


def improve_page(item, qs, items=None, 쓰기=None):
    """`/p/<이름>/improve` 의 화면 한 장. 읽고 → 카드됨 잇고 → 거르고 → 그린다. `쓰기` 는 만든 뒤의 결과 줄·까닭 줄."""
    items = items or [item]
    결과, 오류 = _개선_읽기(item, items)
    전체 = len(결과["항목"])
    항목, 까닭 = filter_improvements(결과["항목"], qs)
    return render_improve(item, dict(결과, 항목=항목, 전체항목=결과["항목"], 전체수=전체, 작업목록오류=오류), qs, 까닭,
                          dict(쓰기 or {}, 프로젝트들=[p["name"] for p in items]))


def improve_all_page(items):
    """`/improve` — 모든 프로젝트 목록은 그리지 않는다. 프로젝트를 고르라는 안내와 프로젝트별 `/p/<이름>/improve` 고리만 낸다(ISSUE-ui-19)."""
    esc = html.escape
    고리 = ['<li><a href="/p/%s/improve">%s</a></li>' % (urllib.parse.quote(i["name"], safe=""), esc(i["name"])) for i in items]
    body = "<p>프로젝트를 고르세요</p>\n" + ("<ul>%s</ul>" % "".join(고리) if 고리 else "<p>보는 프로젝트 없음</p>")
    return FILE_TEMPLATE % {"name": 화면이름["improve"], "body": body}


# ── 보드가 파일을 쓰는 단 하나의 길 — 개선 거리를 카드로 (ISSUE-sc02-e18-make-card-2026-11-20) ──
쓰기_상한 = 64 * 1024


def 쓰기_허락(보낸쪽, 머리, 자리):
    """쓰기 요청(POST)을 받아도 되는가 — (받음, 까닭). 보낸 쪽이 이 기계이고 `Host`·`Origin` 이 이 보드 자리이며
    `Sec-Fetch-Site` 가 있으면 `same-origin` 일 때만. 보드는 127.0.0.1 에만 묶이므로 보낸 쪽 검사는 다른 기계를,
    `Origin`·`Host` 검사는 이 기계 브라우저에서 연 다른 사이트 화면(과 DNS 재결합)을 막는다."""
    글 = "이 기기의 보드 화면에서 보낸 요청만 받는다"
    hosts = {"127.0.0.1:%d" % 자리, "localhost:%d" % 자리}
    if 보낸쪽 != "127.0.0.1":
        return False, "%s — 보낸 쪽 %s" % (글, 보낸쪽)
    if 머리.get("Host") not in hosts:
        return False, "%s — Host 가 이 보드 자리가 아니다" % 글
    if 머리.get("Origin") not in {"http://" + h for h in hosts}:
        return False, "%s — Origin 이 없거나 이 보드 화면이 아니다" % 글
    if 머리.get("Sec-Fetch-Site") not in (None, "same-origin"):
        return False, "%s — 다른 사이트에서 보낸 요청이다" % 글
    return True, ""


def 처음_둘프로젝트(고른, 나온, 이름들, 고름=""):
    """둘 프로젝트 — 사람이 고르면 그것, 고른 항목이 모두 「워크플로우 몫」이고 목록에 hq 가 있으면 hq, 아니면 항목이 나온 프로젝트."""
    if 고름:
        return 고름
    if 고른 and all(x["종류"] == "워크플로우 몫" for x in 고른) and "hq" in 이름들:
        return "hq"
    return 나온


def 배경_줄들(x):
    """항목 하나의 「## 배경」 출처 줄(카드 이름·단계 번호가 한 줄에 든다 — `mark_made` 가 이 줄로 카드됨을 안다)과 두 칸 들인 원문 줄."""
    return (["- 카드 %s · 단계 %s · 종류 %s · 출처 `%s` · 커밋 %s" % (
        x["카드"], x["단계"], x["종류"], x["경로"], x["커밋"][:7] if x["커밋"] else "없음(신호 폴더)")]
        + ["  " + l for l in x["원문"].splitlines()])


def 배경에_더하기(글, 줄들):
    """카드 글의 「## 배경」 절 마지막 글 줄 뒤(다음 제목 앞)에 `줄들` 을 더한 새 글. 「## 배경」 이 없으면 None. 나머지 바이트는 그대로."""
    ls = 글.splitlines(keepends=True)
    s0 = next((i for i, l in enumerate(ls) if l.strip() == "## 배경"), None)
    if s0 is None:
        return None
    끝 = next((i for i in range(s0 + 1, len(ls)) if re.match(r"#{1,6} ", ls[i])), len(ls))
    자리 = s0 + 1
    for i in range(s0 + 1, 끝):
        if ls[i].strip():
            자리 = i + 1
    if not ls[자리 - 1].endswith("\n"):
        ls[자리 - 1] += "\n"
    return "".join(ls[:자리] + [l + "\n" for l in 줄들] + ls[자리:])


def 카드_초안(제목, 항목들, 오늘):
    """카드 틀(`docs/backlog/_템플릿.md`)을 따르는 아이디어 카드 글. 크기·사람이 하면은 비운다. 원문은 두 칸 들여 「## 배경」 안에."""
    배경 = []
    for x in 항목들:
        배경 += 배경_줄들(x)
    return ("---\n상태: 아이디어\n크기:\n갱신: %s\n시나리오:\n요소:\n완료기준:\n선행:\n되돌리기:\n기능:\n---\n# %s\n\n"
            "## 배경\n\n%s\n\n## 사람이 하면\n\n## 원하는 것 — 최소한\n\n## 시작점 — 실재를 확인한 경로\n\n"
            "## 범위 밖\n\n## 완료 조건\n") % (오늘, " ".join(제목.split()), "\n".join(배경))


def 픽_펼치기(값들):
    """`pick` 값들(줄 고르기 칸은 항목 열쇠를 쉼표로 이은 것)을 항목 열쇠 목록으로 — 차례대로, 중복은 한 번."""
    return list(dict.fromkeys(k.strip() for v in 값들 or [] for k in v.split(",") if k.strip()))


def 카드_입력_검사(item, 항목들, form, items, 오늘):
    """(까닭 목록, 정리된 값). 까닭이 하나라도 있으면 아무것도 쓰지 않는다. `form` 은 `parse_qs` 모양."""
    def g(k):
        return (form.get(k) or [""])[0]
    까닭, 고른 = [], []
    by = {항목_열쇠(x): x for x in 항목들}
    픽 = 픽_펼치기(form.get("pick"))
    if not 픽:
        까닭.append("고른 항목이 없다")
    for k in 픽:
        x = by.get(k)
        if x is None:
            까닭.append("목록이 바뀌었다 — 화면을 다시 열어 고르라")
        elif x["카드됨"]:
            까닭.append("이미 카드가 된 항목 — %s" % x["카드됨"][0])
        else:
            고른.append(x)
    제목 = " ".join(g("title").split())
    if not 제목:
        까닭.append("제목이 비었다")
    slug = g("slug")
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug):
        까닭.append("파일 이름은 영문 소문자·숫자·붙임표만")
    이름 = 처음_둘프로젝트(고른, item["name"], [p["name"] for p in items], g("target"))
    대상 = next((p for p in items if p["name"] == 이름), None)
    if 대상 is None:
        까닭.append("보는 목록에 없는 프로젝트 — %s" % 이름)
    파일 = "ISSUE-%s-%s.md" % (slug, 오늘)
    if 대상 is not None and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug):
        그림 = REMOTE.get(os.path.abspath(대상["path"])) or {}
        if os.path.exists(os.path.join(대상["path"], "docs", "backlog", 파일)) or 파일 in (그림.get("카드") or {}):
            까닭.append("같은 이름 카드가 있다 — 이름을 바꾸라")
    return list(dict.fromkeys(까닭)), {"고른": 고른, "제목": 제목, "대상": 대상, "파일": 파일}


def _카드_커밋(project, tip, rel, msg, 내용=None):
    """원격 끝 `tip` 위에 파일 `rel` 하나만 더한(`내용` 이 있으면 그 글로 바꾼) 커밋 — 임시 색인으로 만들어 사본의 색인·`HEAD`·참조를 건드리지 않는다. (커밋, 까닭).
    같은 순서가 `bin/runner.py` `_make_commit` 에 있다 — 보드는 러너를 가져오지 못해 두 벌이다. 한쪽을 고치면 다른 쪽도 본다."""
    fd, idx = tempfile.mkstemp(prefix="board-index-")
    os.close(fd)
    os.unlink(idx)
    env = dict(os.environ, GIT_INDEX_FILE=idx)
    try:
        r = _원격_git(project, "read-tree", tip, env=env)
        if r.returncode:
            return None, _끝줄(r)
        if 내용 is None:
            r = _원격_git(project, "hash-object", "-w", "--", rel)
        else:                                           # `_원격_git` 은 표준 입력을 못 받아 임시 파일로 블롭을 만든다
            fd2, tmp = tempfile.mkstemp(prefix="board-card-")
            try:
                with os.fdopen(fd2, "w", encoding="utf-8", newline="") as f:
                    f.write(내용)
                r = _원격_git(project, "hash-object", "-w", "--", tmp)
            finally:
                os.unlink(tmp)
        if r.returncode:
            return None, _끝줄(r)
        r = _원격_git(project, "update-index", "--add", "--cacheinfo", "100644,%s,%s" % (r.stdout.strip(), rel), env=env)
        if r.returncode:
            return None, _끝줄(r)
        r = _원격_git(project, "write-tree", env=env)
        if r.returncode:
            return None, _끝줄(r)
        r = _원격_git(project, "commit-tree", r.stdout.strip(), "-p", tip, "-m", msg, env=env)
        return (r.stdout.strip(), "") if r.returncode == 0 else (None, _끝줄(r))
    finally:
        try:
            os.unlink(idx)
        except OSError:
            pass


def 승격_글(text):
    """카드 글 머리말의 `상태: 아이디어` 한 줄만 `상태: 대기` 로 바꾼 글. 머리말에 그 줄이 없으면 None. 나머지 바이트는 그대로."""
    줄들 = text.splitlines(keepends=True)
    if not 줄들 or 줄들[0].strip() != "---":
        return None
    for i, l in enumerate(줄들[1:], 1):
        if l.strip() == "---":
            return None
        if l.startswith("상태:"):
            줄들[i] = "상태: 대기" + l[len(l.rstrip("\r\n")):]
            return "".join(줄들)
    return None


def _올린_뒤_그림(project, r):
    """단추로 원격에 올린 직후 그 프로젝트의 그림을 바로 새로 읽는다 — 카드 상태의 출처가 원격이라, 다음 바퀴(최대 1분)를 기다리면 방금 올린 카드가 화면에 안 보인다."""
    if r.get("결과") == "올림":
        try:
            refresh_remote([{"path": project}])
        except Exception:                            # noqa: BLE001 — 그림 갱신 실패가 단추의 결과를 바꾸지 않는다
            pass


def 카드_올리기(project, 파일이름, 글="", 승격=False, 잇기=None):
    """새 카드 파일 하나만 원격 기준 가지 끝 위에 커밋해 올린다. {"결과": 올림 | 올리지 못함 | 원격 없음, "까닭", "커밋"}.
    `승격=True` 면 새 파일이 아니라 원격 끝의 그 카드(아이디어일 때만)에서 `상태:` 줄만 대기로 바꿔 올린다. `잇기` 가 줄 목록이면 원격 끝의 그 카드 「## 배경」 끝에 그 줄들을 더해 올린다
    (카드가 없으면 「원격에 없음」, 배경 절이 없으면 「올리지 못함」). 결과에 「이미 아이디어 아님」(까닭=원격 끝의 상태)·
    「원격에 없음」 이 더 있다. 예외를 내지 않고, 이 기계의 카드 파일은 건드리지 않는다. 밀리면 다시 받아 최대 3번(원격 이력은 다시 쓰지 않는다)."""
    def 끝(결과, 까닭="", 커밋=""):
        return {"결과": 결과, "까닭": 까닭, "커밋": 커밋}
    try:
        cfg = _유효_설정(project)
        if cfg.get("handoff") == "local":
            return 끝("원격 없음")
        got = _원격_git(project, "remote", "get-url", "origin")
        url = got.stdout.strip() if got.returncode == 0 else ""
        if not url:
            return 끝("원격 없음")
        env = dict(os.environ)
        account = cfg.get("githubAccount")
        if isinstance(account, str) and account.strip():
            token = _토큰(account.strip())
            if not token:
                return 끝("올리지 못함", "GitHub 계정 %s 의 토큰을 얻지 못함" % account.strip())
            env["GH_TOKEN"] = token
        rel = "docs/backlog/" + 파일이름
        카드 = 파일이름.removesuffix(".md")
        제목 = ("작업목록: %s 출처 이음 — 보드의 이 카드에 잇기" if 잇기 is not None else
              "작업목록: %s 대기로 올림 — 보드의 대기로 올리기" if 승격 else "작업목록: %s 새 카드 — 보드의 카드로 만들기")
        msg = (제목 + "\n\nLoop-Card: %s\nLoop-Card-Host: %s") % (카드, 카드, os.uname().nodename)
        까닭 = "원격이 계속 움직여 올리지 못함"
        for _ in range(3):
            r = _원격_git(project, "ls-remote", "--heads", url, env=env)
            if r.returncode:
                return 끝("올리지 못함", _끝줄(r))
            heads = {}
            for line in r.stdout.splitlines():
                sha, _, ref = line.partition("\t")
                if ref.startswith("refs/heads/"):
                    heads[ref[len("refs/heads/"):]] = sha.strip()
            base = next((x for x in ("develop", "main", "master") if x in heads), None)
            if base is None:
                return 끝("올리지 못함", "원격에 기준 가지가 없다")
            tip = heads[base]
            r = _원격_git(project, "fetch", "-q", "--no-write-fetch-head", url, "refs/heads/" + base, env=env)
            if r.returncode:
                return 끝("올리지 못함", _끝줄(r))
            r = _원격_git(project, "-c", "core.quotepath=false", "ls-tree", "--name-only", tip, "--", rel)
            if r.returncode:
                return 끝("올리지 못함", _끝줄(r))
            내용 = None
            if 잇기 is not None:
                if not r.stdout.strip():
                    return 끝("원격에 없음", "원격 끝에 그 카드가 없다")
                r = _원격_git(project, "show", "%s:%s" % (tip, rel))
                if r.returncode:
                    return 끝("올리지 못함", _끝줄(r))
                내용 = 배경에_더하기(r.stdout, 잇기)
                if 내용 is None:
                    return 끝("올리지 못함", "원격 끝 카드에 배경 절이 없다")
            elif 승격:
                if not r.stdout.strip():
                    return 끝("원격에 없음", "원격 끝에 그 카드가 없다")
                r = _원격_git(project, "show", "%s:%s" % (tip, rel))
                if r.returncode:
                    return 끝("올리지 못함", _끝줄(r))
                fm = remote_front_matter(r.stdout) or {}
                if fm.get("상태") != "아이디어" or fm.get("시나리오"):
                    return 끝("이미 아이디어 아님", fm.get("상태") or "상태 모름")
                내용 = 승격_글(r.stdout)
                if 내용 is None:
                    return 끝("올리지 못함", "원격 끝 카드 머리말에 상태 줄이 없다")
            elif r.stdout.strip():
                return 끝("올리지 못함", "원격에 같은 이름 카드가 있다")
            sha, why = _카드_커밋(project, tip, rel, msg, 내용)
            if not sha:
                return 끝("올리지 못함", why)
            r = _원격_git(project, "push", url, "%s:refs/heads/%s" % (sha, base), env=env)
            if r.returncode == 0:
                return 끝("올림", 커밋=sha)
            까닭 = _끝줄(r)
            if "rejected" not in (r.stderr or "") + (r.stdout or "") and "non-fast-forward" not in (r.stderr or ""):
                break
        return 끝("올리지 못함", 까닭)
    except Exception as e:                              # noqa: BLE001 — 올리기가 보드를 죽이지 않는다
        return 끝("올리지 못함", str(e) or type(e).__name__)


def _카드만들기_처리(서버, item, items, form):
    """`POST /p/<이름>/make-card` — 검사 → 카드 파일 쓰기(덮어쓰지 않음) → 올리기 → 개선 거리 화면(맨 위 결과 줄)."""
    오늘 = dt.date.today().isoformat()
    try:
        결과, _ = _개선_읽기(item, items)
        까닭들, 값 = 카드_입력_검사(item, 결과["항목"], form, items, 오늘)
        if 까닭들:
            서버._그리기(improve_page(item, {}, items, {"까닭들": 까닭들}), 400)
            return
        대상, 파일 = 값["대상"], 값["파일"]
        글 = 카드_초안(값["제목"], 값["고른"], 오늘)
        try:
            os.makedirs(os.path.join(대상["path"], "docs", "backlog"), exist_ok=True)
            with open(os.path.join(대상["path"], "docs", "backlog", 파일), "x", encoding="utf-8") as f:
                f.write(글)
        except OSError as e:
            서버._그리기(improve_page(item, {}, items, {"까닭들": ["카드 파일을 쓰지 못함 — %s" % e]}), 500)
            return
        r = 카드_올리기(대상["path"], 파일, 글)
        _올린_뒤_그림(대상["path"], r)
        줄 = "카드를 만들었다 — %s/%s" % (대상["name"], 파일)
        if r["결과"] == "올림":
            줄 += " · 원격 저장소에 올림(커밋 %s)" % r["커밋"][:7]
        elif r["결과"] == "원격 없음":
            줄 += " · 원격 없음 — 커밋하지 않았다"
        else:
            줄 += " · 올리지 못함 — %s(카드는 이 기기에 있다)" % r["까닭"]
        서버._그리기(improve_page(item, {}, items, {"결과줄": 줄}))
    except Exception as e:                              # noqa: BLE001
        서버.send_error(500, "Internal Error", "카드를 만들다 실패 — %s" % (str(e) or type(e).__name__))


def _잇기_처리(서버, item, items, form):
    """`POST /p/<이름>/link-card` — 검사(고른 항목·카드 이름·카드가 있는 프로젝트와 배경 절) → 이 기계 카드 파일에 출처 줄 더하기 → 원격 끝 위에 그 파일 하나만 올리기 → 개선 거리 화면(맨 위 결과 줄).
    검사에서 하나라도 어긋나면 400 과 까닭 줄이고 아무것도 쓰지 않는다."""
    def g(k):
        return (form.get(k) or [""])[0]
    try:
        결과, _ = _개선_읽기(item, items)
        by = {항목_열쇠(x): x for x in 결과["항목"]}
        까닭, 고른 = [], []
        픽 = 픽_펼치기(form.get("pick"))
        if not 픽:
            까닭.append("고른 항목이 없다")
        for k in 픽:
            x = by.get(k)
            if x is None:
                까닭.append("목록이 바뀌었다 — 화면을 다시 열어 고르라")
            elif x["카드됨"]:
                까닭.append("이미 카드가 된 항목 — %s" % x["카드됨"][0])
            elif not x["단계"]:
                까닭.append("단계가 없어 카드로 이을 수 없는 항목")
            else:
                고른.append(x)
        파일 = g("card")
        if not re.fullmatch(r"[^/\\\0]+\.md", 파일) or 파일.startswith("."):
            까닭.append("카드 이름이 이상하다")
        대상 = next((p for p in items if p["name"] == (g("at") or item["name"])), None)
        if 대상 is None:
            까닭.append("보는 목록에 없는 프로젝트 — %s" % (g("at") or item["name"]))
        줄들 = [l for x in 고른 for l in 배경_줄들(x)]
        경로, 옛 = "", None
        if not 까닭:
            경로 = os.path.join(대상["path"], "docs", "backlog", 파일)
            원격에 = 파일 in ((REMOTE.get(os.path.abspath(대상["path"])) or {}).get("카드") or {})
            try:
                with open(경로, encoding="utf-8", newline="") as f:
                    옛 = f.read()
            except OSError:
                옛 = None
            if 옛 is None and not 원격에:
                까닭.append("그런 카드가 없다 — %s" % 파일)
            elif 옛 is not None and 배경에_더하기(옛, 줄들) is None:
                까닭.append("카드 글에 배경 절이 없다 — %s" % 파일)
        if 까닭:
            서버._그리기(improve_page(item, {}, items, {"까닭들": list(dict.fromkeys(까닭))}), 400)
            return
        if 옛 is not None:
            try:
                with open(경로, "w", encoding="utf-8", newline="") as f:
                    f.write(배경에_더하기(옛, 줄들))
            except OSError as e:
                서버._그리기(improve_page(item, {}, items, {"까닭들": ["카드 파일을 쓰지 못함 — %s" % e]}), 500)
                return
        r = 카드_올리기(대상["path"], 파일, 잇기=줄들)
        _올린_뒤_그림(대상["path"], r)
        줄 = "카드에 이었다 — %s/%s" % (대상["name"], 파일)
        if r["결과"] == "올림":
            줄 += " · 원격 저장소에 올림(커밋 %s)" % r["커밋"][:7] + (" · 원격 끝에만 더했다" if 옛 is None else "")
        elif r["결과"] == "원격 없음":
            줄 += " · 원격 없음 — 커밋하지 않았다"
        else:
            줄 += " · 올리지 못함 — %s(%s)" % (r["까닭"], "이 기기 파일에는 더했다" if 옛 is not None else "이 기기에는 파일이 없다")
        서버._그리기(improve_page(item, {}, items, {"결과줄": 줄}))
    except Exception as e:                              # noqa: BLE001
        서버.send_error(500, "Internal Error", "카드에 잇다 실패 — %s" % (str(e) or type(e).__name__))


def _승격_처리(서버, item, items, form):
    """`POST /p/<이름>/promote` — 확인 값·카드 이름 검사 → 이 기계 카드가 시나리오 없는 아이디어인지 → 상태 줄만 대기로 → 원격 끝 위에 그 파일 하나만 올리기 → 결과 줄."""
    def g(k):
        return (form.get(k) or [""])[0]

    def 보이기(줄, 상태=200):
        본문 = '<p>%s</p><p><a href="/p/%s/board.html">← %s</a></p>' % (html.escape(줄), urllib.parse.quote(item["name"], safe=""), html.escape(화면이름["board"]))
        서버._그리기(FILE_TEMPLATE % {"name": "대기로 올리기", "body": 본문}, 상태)
    try:
        파일 = g("card")
        if g("confirm") != "1" or not re.fullmatch(r"[^/\\\0]+\.md", 파일) or 파일.startswith("."):
            서버.send_error(400, "Bad Request", "확인한 요청이 아니거나 카드 이름이 이상하다")
            return
        경로 = os.path.join(item["path"], "docs", "backlog", 파일)
        try:
            with open(경로, encoding="utf-8", newline="") as f:
                옛 = f.read()
        except OSError:
            보이기("카드 파일이 이 기기에 없다 — %s" % 파일)
            return
        fm = remote_front_matter(옛) or {}
        if fm.get("상태") != "아이디어" or fm.get("시나리오"):
            보이기("바꾸지 않았다 — %s 은 지금 상태 %s%s" % (파일, fm.get("상태") or "모름", " · 시나리오에 묶임" if fm.get("시나리오") else ""))
            return
        새 = 승격_글(옛)
        if 새 is None:
            보이기("바꾸지 않았다 — 머리말에 상태 줄이 없다")
            return
        try:
            with open(경로, "w", encoding="utf-8", newline="") as f:
                f.write(새)
        except OSError as e:
            보이기("카드 파일을 쓰지 못함 — %s(상태는 그대로)" % e, 500)
            return
        r = 카드_올리기(item["path"], 파일, 승격=True)
        _올린_뒤_그림(item["path"], r)
        if r["결과"] in ("이미 아이디어 아님", "원격에 없음"):
            try:
                with open(경로, "w", encoding="utf-8", newline="") as f:
                    f.write(옛)
            except OSError:
                pass
            줄 = ("올리지 않았다 — 원격 끝에서는 이미 %s(이 기기 파일은 그대로 아이디어)" % r["까닭"] if r["결과"] == "이미 아이디어 아님"
                  else "올리지 않았다 — %s(이 기기 파일은 그대로 아이디어)" % r["까닭"])
        else:
            줄 = "%s 상태 대기" % 파일
            if r["결과"] == "올림":
                줄 += " · 원격 저장소에 올림(커밋 %s)" % r["커밋"][:7]
            elif r["결과"] == "원격 없음":
                줄 += " · 원격 없음 — 커밋하지 않았다"
            else:
                줄 += " · 올리지 못함 — %s(이 기기 파일은 대기로 바뀌어 있다)" % r["까닭"]
        보이기(줄)
    except Exception as e:                              # noqa: BLE001
        서버.send_error(500, "Internal Error", "대기로 올리다 실패 — %s" % (str(e) or type(e).__name__))


_쓰기_잠금 = threading.Lock()
쓰기_길 = {"make-card": _카드만들기_처리, "promote": _승격_처리, "link-card": _잇기_처리}      # 보드가 파일을 바꾸는 길의 유일한 목록


class 보드서버(작업파일서버):
    """`/` 첫 화면, `/p/<이름>/…` 그 프로젝트 보드와 카드 화면. 요청마다 그 자리에서 그린다. 파일을 쓰는 길은 「카드로 만들기」
    (`POST /p/<이름>/make-card`)·「대기로 올리기」(`POST /p/<이름>/promote`)·「이 카드에 잇기」(`POST /p/<이름>/link-card`) 셋이고, 이 기계(127.0.0.1)의 보드 화면이 보낸 요청만 받는다."""
    extra = None

    def end_headers(self):
        self.send_header(BOARD_HEADER, "1")
        super().end_headers()

    def _그리기(self, page, status=200, 틀=None):
        """HTML 화면의 유일한 출구 — 왼쪽 사이드바를 씌워 보낸다. `틀` = (items, rows, turns) 이미 계산한 것, 없으면 여기서 읽는다.
        사이드바를 못 그려도 본문은 보낸다(최소 사이드바)."""
        path = urllib.parse.urlsplit(self.path).path
        try:
            if 틀 is None:
                items, _ = _보는목록(self.extra)
                rows = [project_row(p) for p in items]
                틀 = (items, rows, my_turns(rows, all_turns(items)))
            nav = render_shell(*틀, path)
        except Exception:
            nav = 최소_사이드바(path)
        data = with_shell(page, nav).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        ok, why = 쓰기_허락(self.client_address[0], self.headers, self.server.server_address[1])
        if not ok:
            self.close_connection = True
            self.send_error(403, "Forbidden", why)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > 쓰기_상한:
            self.close_connection = True
            self.rfile.read(max(0, min(n, 1 << 20)))
            self.send_error(413, "Payload Too Large", "본문은 64KB 까지")
            return
        body = self.rfile.read(n).decode("utf-8", "replace")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/x-www-form-urlencoded":
            self.send_error(415, "Unsupported Media Type", "양식 본문만 받는다")
            return
        m = re.fullmatch(r"/p/([^/]+)/([^/]+)", urllib.parse.urlsplit(self.path).path)
        items, _ = _보는목록(self.extra)
        item = next((p for p in items if m and p["name"] == urllib.parse.unquote(m.group(1))), None)
        if item is None or urllib.parse.unquote(m.group(2)) not in 쓰기_길:
            self.send_error(404, "Not Found", "쓰는 길이 아니다")
            return
        with _쓰기_잠금:                                 # 쓰기는 한 번에 하나 — 검사는 잠금 밖, 읽기는 잠금과 무관
            쓰기_길[urllib.parse.unquote(m.group(2))](self, item, items, urllib.parse.parse_qs(body, keep_blank_values=True))

    def _plan_문서(self, item, name, 폴더="plan"):
        """`docs/<폴더>/<name>` 을 카드 본문처럼 그려 낸다(폴더는 plan 또는 todo). 이름에 `/`·`\\`·`..` 이 들거나 .md 가 아니거나 파일이 없으면 404 — 폴더 밖 글은 나오지 않는다."""
        plan = os.path.realpath(os.path.join(item["path"], "docs", 폴더))
        path = os.path.realpath(os.path.join(plan, name))
        if (not name.endswith(".md") or any(x in name for x in ("/", "\\")) or ".." in name
                or os.path.dirname(path) != plan or not os.path.isfile(path)):
            self.send_error(404, "Not Found", "그런 문서 없음")
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if 폴더 == "plan" and _시나리오이름.match(name) and not (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("원본") or [""])[0]:
            try:                                          # 정리 화면 — 읽지 못한 시나리오이거나 그리기가 실패하면 아래 원문 그리기로 간다
                cards, scenarios, _, _ = _읽기(item, 합치기=True)
                s = next((x for x in scenarios if x["file"] == name), None)
                page = render_scenario(item, cards, scenarios, s, text) if s and not s["오류"] else None
            except Exception:                             # noqa: BLE001
                page = None
            if page is not None:
                self._그리기(page)
                return
        try:
            shown = 본문_그리기(text)
        except Exception:                                # 그리기가 무엇으로 실패하든 원문이 보인다
            shown = "<pre>%s</pre>" % html.escape(text)
        page = FILE_TEMPLATE % {"name": html.escape(name), "body": shown}
        self._그리기(page.replace('href="board.html"', 'href="../board.html"', 1))

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        items, err = _보는목록(self.extra)
        if path == "/search":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            계산 = lambda: search(items, qs)
            시작 = time.monotonic()
            cond = _조건(qs)[0]
            # 찾을 것이 없는 화면(쿼리 없음, `p`·`kind` 만)은 git 을 부르지 않는다 — 잠깐저장의 열쇠도 git 을 읽는다
            결과, 까닭 = _잠깐저장("search", items, qs, 계산) if cond["낱말"] or _거르개_글(cond) else 계산()
            걸린 = time.monotonic() - 시작
            if (qs.get("as") or [""])[0] == "json":
                data = search_json(items, 결과, 까닭, _조건(qs)[0]["낱말"]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._그리기(render_search(qs, 결과, 까닭, 걸린, [p["name"] for p in items]))
            return
        if path == "/improve":
            self._그리기(improve_all_page(items))
            return
        if path == "/cards":
            보기 = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("보기") or [""])[0]
            if 보기 not in 카드_보기:
                self._그리기(render_cards(보기, [], ["보기는 진행·대기·병합·멈춤·보류 중 하나"]), status=404)
                return
            rows = [project_row(p) for p in items]
            병합줄, 모름 = _이번주_병합(items, dt.date.today()) if 보기 == "병합" else (None, 0)
            덧 = ["%s: 못 읽음 — %s" % (r["name"], r["까닭"]) for r in rows if r["까닭"]] + (["원격 모름 %d곳" % 모름] if 병합줄 is not None and 모름 else [])
            self._그리기(render_cards(보기, 카드_목록(rows, 보기, 병합줄, dt.datetime.now()), 덧), 틀=(items, rows, my_turns(rows, all_turns(items))))
            return
        if path == "/signals":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            qs = _비용품질_인자(qs)                                  # period 가 이기고, 아무것도 없으면 이번 주 — 읽기에는 날짜로 풀어 넘겨 캐시 열쇠에 날짜가 든다
            결과, 까닭 = _잠깐저장("signals", items, qs, lambda: read_signals(items, qs))
            self._그리기(render_signals(qs, 결과, 까닭))
            return
        if path == "/retro":
            qs = _비용품질_인자(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query))
            자료, 까닭 = _잠깐저장("retro", items, qs, lambda: read_retro(items, qs))
            self._그리기(render_retro(qs, 자료, 까닭))
            return
        if path == "/timeline":
            시작, 끝, 까닭 = timeline_period(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query), dt.date.today(), 2)
            now = dt.datetime.now()
            쿼리 = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            고른, 카드, 프로젝트 = ((쿼리.get(k) or [""])[0].strip() for k in ("sc", "card", "p"))
            # 진행 중 구간의 끝이 now 라 열쇠에 분 단위로 넣는다 — 같은 분 안에서만 재사용(막대 끝이 최대 59초 늦을 수 있다)
            막대 = _잠깐저장("timeline", items, {"시작": 시작, "끝": 끝, "분": now.strftime("%Y-%m-%d %H:%M")}, lambda: read_timeline(items, 시작, 끝, now))
            self._그리기(render_timeline(막대, 시작, 끝, now, 까닭, 고른, 카드, 프로젝트))
            return
        if path == "/":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            tab = (qs.get("tab") or [""])[0]
            rows = [project_row(p) for p in items]
            turns = my_turns(rows, all_turns(items))
            기계 = read_machines(items, rows, *timeline_period(qs, dt.date.today())[:2], dt.datetime.now())
            self._그리기(render_home(rows, turns, err, tab, read_resolved(items), machines=render_machine_pills(기계), 기계수=len(기계["줄"]),
                                    병합=week_merges(items, dt.date.today()), feed=_홈_피드(items, rows, qs)), 틀=(items, rows, turns))
            return
        name, _, sub = path[3:].partition("/") if path.startswith("/p/") else ("", "", "")
        item = next((p for p in items if p["name"] == urllib.parse.unquote(name)), None)
        if item is None:
            self.send_error(404, "Not Found", "보는 목록에 없는 프로젝트")   # 상태 줄은 latin-1 이라 한글은 본문 설명으로
            return
        if sub in ("", "board.html"):
            try:
                cards, scenarios, todo, turns = _읽기(item, 합치기=True)
            except OSError as e:
                self.send_error(500, "Internal Error", str(e))
                return
            turns = [{**t, "열어 볼 곳": _서버_고리(t["열어 볼 곳"])} for t in _알림결과(item["path"], turns)]
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            반려된, 까닭 = None, ""
            if (qs.get("반려") or [""])[0] == "1":              # 반려 칩 — 커밋 이력의 반려 판정이 있었던 카드만(검색의 반려 거르기와 같은 읽기)
                try:
                    커밋 = _커밋들(item["path"])
                    반려된 = {c["file"] for c in cards if any(x[3] and os.path.splitext(c["file"])[0] in x[2] for x in 커밋)}
                except Exception:                              # noqa: BLE001
                    까닭 = "반려 이력을 읽지 못해 거르지 않았다"
            page = render_html(cards, item["name"], 10, scenarios, turns,
                               os.path.join(item["path"], "docs", "plan"),
                               경보_알림결과(item["path"], stalled_alarms(item, cards, REMOTE.get(os.path.abspath(item["path"])))),
                               단추=True, 거르개={"qs": qs, "반려된": 반려된, "까닭": 까닭})
            feed = _피드_구역([item], [{"까닭": "", "카드": cards}], qs, item["name"], 폼=False)
            전체 = [project_row(p) for p in items]                    # 첫 화면에서 옮겨 온 것 — 이 프로젝트의 칸별 수, 보는 목록 전체의 기계 현황
            행 = 전체[items.index(item)]
            시작, 끝, 까닭 = timeline_period(qs, dt.date.today())
            기계 = render_machines(read_machines(items, 전체, 시작, 끝, dt.datetime.now()), 시작, 끝, 까닭)
            줄 = ('<p class="meta">%s · 사람 차례 %d · 마지막 움직임 %s</p>'
                 % (html.escape(" · ".join("%s %d" % (c, n) for c, n in 행["수"].items())), 행["차례 수"], html.escape(행["마지막 움직임"])))
            page = page.replace("</body>", '<details class="extra"><summary>칸별 수 · 기기 현황 · 최근 활동</summary>%s</details>' % (줄 + 기계 + feed) + "</body>", 1)
            self._그리기(page)
            return
        if sub == "features":
            try:
                cards, scenarios, _, _ = _읽기(item, 합치기=True)
            except OSError as e:
                self.send_error(500, "Internal Error", str(e))
                return
            self._그리기(render_features(item, items, cards, scenarios))
            return
        if sub == "graph":
            try:
                cards, scenarios, _, _ = _읽기(item, 합치기=True)
            except OSError as e:
                self.send_error(500, "Internal Error", str(e))
                return
            self._그리기(render_relmap(item, items, 연관_자료(item["path"], cards, scenarios, read_featuremap(item["path"])),
                                       urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)))
            return
        if sub == "improve":
            self._그리기(improve_page(item, urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query), items))
            return
        card_file = urllib.parse.unquote(sub)
        if card_file.startswith("plan/"):                          # 끝 보고·시나리오 문서 — `docs/plan/` 안의 .md 만, 카드 본문처럼
            self._plan_문서(item, card_file[len("plan/"):])
            return
        if card_file.startswith("todo/"):                          # 할 일 카드 — `docs/todo/` 안의 .md 만
            self._plan_문서(item, card_file[len("todo/"):], "todo")
            return
        if card_file.endswith(".md") and "/" not in card_file:     # 카드 고리 — 상세 화면. 폴더 밖 주소는 아래로 간다
            try:
                cards, scenarios, _, _ = _읽기(item, 합치기=True)
            except OSError as e:
                self.send_error(500, "Internal Error", str(e))
                return
            card = next((c for c in cards if c["file"] == card_file), None)
            if card is None:
                self._그리기(_없는_카드(card_file), 404)
                return
            remote = ((REMOTE.get(os.path.abspath(item["path"])) or {}).get("카드") or {}).get(card_file) or {}
            fm = remote.get("머리말") if card.get("출처") == "원격" else None
            본문 = None
            끝 = (REMOTE.get(os.path.abspath(item["path"])) or {}).get("끝")
            if card.get("출처") == "원격" and 끝:         # 본문도 머리말과 같은 끝에서 — 거울의 로컬 `cat-file`, 원격에는 닿지 않는다
                if not os.path.isdir(_거울_경로(item["path"])):
                    본문 = ("못읽음", "거울 폴더가 없음")
                else:
                    글 = _카드_내용들(item["path"], [(끝, card_file)]).get((끝, card_file))
                    본문 = 글 if 글 is not None else ("못읽음", "거울의 원격 끝에 그 카드 글이 없음")
            self._그리기(render_card_detail(item, card, cards, scenarios, read_card_history(item["path"], card_file), fm,
                                            read_size_medians(item, cards, card), read_card_review(item["path"], card_file),
                                            _원격_웹(item["path"]), 본문))
            return
        self.directory = os.path.join(item["path"], "docs", "backlog")
        self.path = "/" + sub
        super().do_GET()


def serve(backlog=None, project=None, port=0):
    """보드 서버 하나를 127.0.0.1 에 연다. (주소, 서버) 또는 (None, None).

    project 를 주면 그 프로젝트는 보는 목록에 없어도 내보내고 주소는 그 프로젝트 보드다.
    자리가 막혔으면 다른 번호를 보지 않는다. backlog 는 옛 부름 모양을 위해 받을 뿐 쓰지 않는다."""
    extra = {"name": _이름(project), "path": project, "표시": []} if project else None
    handler = type("보드서버", (보드서버,), {"extra": extra})
    try:
        # 요청마다 스레드 — 아무것도 안 보내는 연결이 서버를 붙잡지 않는다. 시험이 TCPServer 를 바꿔치기하므로 호출 때 만든다.
        서버클래스 = type("스레드서버", (socketserver.ThreadingMixIn, socketserver.TCPServer), {"daemon_threads": True, "request_queue_size": 64})   # 기본 5 는 동시 요청 여섯에 연결이 끊긴다
        httpd = 서버클래스(("127.0.0.1", port), handler, bind_and_activate=False)
        httpd.allow_reuse_address = True    # 방금 내린 서버의 닫힌 연결(TIME_WAIT)이 남아도 같은 자리에 다시 묶인다
        try:
            httpd.server_bind()
            httpd.server_activate()
        except OSError:
            httpd.server_close()
            raise
    except OSError:
        return None, None
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]
    return (base + _주소(extra["name"]) + "board.html" if extra else base + "/"), httpd


def who_holds(port):
    """고정 자리를 쓰는 프로그램을 알리는 글 한 줄."""
    알림 = "127.0.0.1:%d 을 다른 프로그램이 쓰고 있다 — " % port
    try:
        r = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-Fpc"],
                           capture_output=True, text=True, timeout=5)
        f = {l[0]: l[1:] for l in r.stdout.splitlines() if l[:1] in "pc"}
        if f.get("p"):
            return 알림 + "%s (프로세스 %s)" % (f.get("c", "이름 모름"), f["p"])
    except (OSError, subprocess.SubprocessError, AttributeError):
        pass
    return 알림 + "무엇이 쓰는지 알아내지 못했다"


def _보드인가(port):
    """고정 자리에서 우리 보드가 답하는가 — 응답 머리 표시로 가린다."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/")
        return conn.getresponse().getheader(BOARD_HEADER) == "1"
    except (OSError, http.client.HTTPException):
        return False


def 상주(project=None, every=10):
    """보드 서버를 띄우고 every 초마다 모든 프로젝트의 사람 차례를 알린다. 자리가 막혔으면 알리고 1."""
    extra = {"name": _이름(project), "path": project, "표시": []} if project else None
    원격_불러오기(_보는목록(extra)[0])         # 첫 원격 확인 전에도 마지막 그림으로 시작한다
    url, httpd = serve(project=project, port=PORT_BASE)
    if not httpd:
        print(who_holds(PORT_BASE))
        return 1
    print(url, flush=True)
    stop = threading.Event()
    threading.Thread(target=원격바퀴, args=(extra, stop), name="원격바퀴", daemon=True).start()
    try:
        while True:
            all_turns(_보는목록(extra)[0], sync=True)
            time.sleep(every)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.shutdown()
        httpd.server_close()
    return 0


def _launchctl(args):
    """launchctl 한 번 — (종료 코드, 오류 글). 시험이 바꿔 끼운다."""
    try:
        r = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)
        return r.returncode, (r.stderr or r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def _상주_PATH():
    """등록 파일에 적을 PATH — 켜는 순간의 PATH 에 지금 파이썬의 폴더가 빠져 있으면 앞에 더한다. launchd 의 기본 PATH 는 gh 가 있는 폴더를 담지 않는다."""
    경로 = os.environ.get("PATH") or os.defpath
    폴더 = os.path.dirname(sys.executable)
    return 경로 if 폴더 in 경로.split(os.pathsep) else 폴더 + os.pathsep + 경로


def 상주_켜기():
    """로그인 항목 파일 하나를 쓰고 등록한다 — 지금 이 board.py 를 가리킨다. 다시 켜면 경로를 덮어쓴다."""
    path = os.path.join(LAUNCH_DIR, LAUNCH_LABEL + ".plist")
    os.makedirs(LAUNCH_DIR, exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump({"Label": LAUNCH_LABEL, "ProgramArguments": [sys.executable, os.path.abspath(__file__), "--상주"],
                       "RunAtLoad": True, "KeepAlive": True,
                       "EnvironmentVariables": {"PATH": _상주_PATH()}}, f)
    domain = "gui/%d" % os.getuid()
    _launchctl(["bootout", "%s/%s" % (domain, LAUNCH_LABEL)])      # 이미 있으면 내린다 — 없어서 실패해도 무방
    rc, err = _launchctl(["bootstrap", domain, path])
    if rc:
        print("로그인 항목을 등록하지 못했다: %s" % err)
        return 1
    print("켰다 — 로그인 때 보드가 뜬다: %s" % path)
    return 0


def 상주_끄기():
    """떠 있는 것을 내리고 로그인 항목 파일을 지운다."""
    _launchctl(["bootout", "gui/%d/%s" % (os.getuid(), LAUNCH_LABEL)])
    try:
        os.remove(os.path.join(LAUNCH_DIR, LAUNCH_LABEL + ".plist"))
    except FileNotFoundError:
        pass
    print("껐다")
    return 0


def 따라가기(project):
    """새 서버를 띄우지 않고 보드 주소를 알린다. 고정 자리가 비어 있으면 모든 프로젝트를 보는 서버를 띄운다."""
    if not _보드인가(PORT_BASE):
        return 상주(project)
    print("http://127.0.0.1:%d%sboard.html" % (PORT_BASE, _주소(_이름(project))))
    if not any(os.path.abspath(p["path"]) == project for p in watched_projects()[0]):
        print("보는 목록에 없다 — 보기 목록에 올리면 보인다(%s)" % LIST_FILE)
    return 0


def _알림결과(project, turns):
    """저장 목록의 알림 실패 표시만 붙인다 (쓰지 않는다)."""
    try:
        with open(os.path.join(HUMAN_DIR, os.path.basename(os.path.abspath(project)) + ".json"), encoding="utf-8") as f:
            old = json.load(f)
        for t in turns:
            t["알림"] = old.get(t["열쇠"], {}).get("알림")
    except (OSError, ValueError, AttributeError):
        pass
    return turns


def write_once(backlog, project, every=0, turns_out=None):
    cards = read_cards(backlog)
    scenarios = read_scenarios(os.path.join(project, "docs", "plan"))
    turns = human_turns(cards, scenarios, project, read_todo_cards(os.path.join(project, "docs", "todo")))
    # 알림·저장·기록은 따라가기에서만 — 한 번 찍기는 보이기만 한다. 마지막 한 번(every=0)도 건드리지 않는다.
    turns = sync_turns(project, turns) if every else _알림결과(project, turns)
    out = os.path.join(backlog, "board.html")
    item = next((p for p in watched_projects()[0] if os.path.abspath(p["path"]) == os.path.abspath(project)),
                {"name": os.path.basename(project), "path": project, "기기": ""})
    # 정적 화면은 원격을 읽지 않으므로 이 기계 경보만 — 합친 카드의 「출처」를 이 기계로 채우려고 그림 없이 합친다
    alarms = 경보_알림결과(project, stalled_alarms(item, merge_cards(cards, None, item.get("기기", "")), None))
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_html(cards, os.path.basename(project), every, scenarios, turns,
                              os.path.join(project, "docs", "plan"), alarms))
    if turns_out is not None:
        turns_out[:] = turns
    return cards, out


def main(argv):
    argv = list(argv)
    if "--연결" in argv:
        rest = [x for x in argv if x != "--연결"]
        배차 = "--배차" in rest
        rest = [x for x in rest if x != "--배차"]
        기기 = ""
        if "--기기" in rest:
            i = rest.index("--기기")
            if i + 1 >= len(rest):
                print("--기기 뒤에 기기 이름이 없다")
                return 2
            기기 = rest[i + 1]
            del rest[i:i + 2]
        rest = [x for x in rest if not x.startswith("--")]
        return 연결(rest[0] if rest else ".", 기기, 배차)
    for flag, run in (("--상주-켜기", 상주_켜기), ("--상주-끄기", 상주_끄기), ("--상주", 상주)):
        if flag in argv:
            return run()
    follow = "--따라가기" in argv
    argv = [x for x in argv if not x.startswith("--")]
    project = os.path.abspath(os.path.expanduser(argv[0] if argv else "."))
    backlog = os.path.join(project, "docs", "backlog")
    if not os.path.isdir(backlog):
        print("작업목록 폴더가 없다: %s" % backlog)
        return 1
    if follow:
        return 따라가기(project)

    turns = []
    cards, out = write_once(backlog, project, 0, turns)
    print(render_turns_text(turns))
    print(render_text(cards, read_scenarios(os.path.join(project, "docs", "plan"))))
    print("보드: %s" % out)
    for w in todo_warnings(read_todo_cards(os.path.join(project, "docs", "todo")), backlog):
        print(w)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
