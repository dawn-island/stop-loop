#!/usr/bin/env python3
"""작업 보드 — `docs/backlog/*.md` 의 머리말을 읽어 칸반으로 보여준다.

  python3 bin/board.py [프로젝트 경로]          터미널에 칸을 찍고 board.html 을 쓴다
  python3 bin/board.py [프로젝트 경로] --따라가기  이 기계의 보드 주소를 알린다 (없으면 그 보드를 띄운다)
  python3 bin/board.py --상주                  모든 프로젝트를 보는 보드 서버 하나를 띄우고 사람 차례를 알린다
  python3 bin/board.py --상주-켜기 | --상주-끄기  로그인 때 --상주 가 뜨게 하는 항목을 쓰고 지운다

보드 서버는 이 기계에 하나이고 고정 자리 127.0.0.1:8700 에만 열린다 — 다른 번호로 옮기지 않는다.
첫 화면(`/`)이 보는 목록(`~/.stop-loop/projects.json` 의 `"projects"` 와 `"보기"`)의 모든
프로젝트를 한 줄씩 보이고, 줄을 누르면 `/p/<이름>/board.html` 에 그 프로젝트 칸반이 열린다.

파일이 진실이다. 보드가 파일을 쓰는 것은 둘뿐이다 — 「카드로 만들기」(개선 거리 화면, 새 아이디어 카드
파일 하나를 쓰고 원격 기준 가지에 올린다)와 「대기로 올리기」(시나리오에 안 묶인 아이디어 카드의 `상태:` 줄만
대기로, 확인 창 뒤에 그 파일 하나만 올린다). 둘 다 127.0.0.1 에서 이 기계의 보드 화면이 보낸 요청만 받는다.
상태를 바꾸려면 그 파일의 `상태:` 줄을 고친다(승격은 사람만 한다, `workflows/01-loop-design.md`).
러너는 실행 중에 같은 줄을 되쓴다.

머리말은 파일 맨 앞의 `---` 두 줄 사이에 있고, `열쇠: 값` 형식이다. YAML 라이브러리를
쓰지 않는 이유는 표준 라이브러리만으로 돌리기 위해서다(`bin/ledger.py` 와 같은 규율).
"""
import datetime as dt
import glob
import hashlib
import html
import http.client
import http.server
import json
import os
import plistlib
import re
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


# 러너가 도는 동안 한 줄씩 쌓는 폴더 (ISSUE-live-view-2026-09-20). 러너의 `LIVE_DIR` 과 같은
# 경로이고, 파일 이름은 「저장소 이름 두 밑줄 이슈 이름」이다. 시험이 임시 폴더로 바꿔 끼운다.
LIVE_DIR = os.path.expanduser("~/.stop-loop/live")


def last_live_line(backlog, file):
    """진행 카드의 실시간 줄 파일에서 맨 끝 한 줄. 없거나 못 읽으면 빈 문자열."""
    try:
        repo = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(backlog))))
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
    for path in (MODELS_FILE, os.path.join(project, "stop-loop.config.json")):
        try:
            with open(path, encoding="utf-8") as f:
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
        s = {"번호": m.group(1), "이름": "", "상태": "상태 모름", "요소": set(), "카드아님": set(), "오류": "", "남은질문": [],
             "file": os.path.basename(path), "완료기준": {}, "없어짐": set(), "요소이름": {}, "선행": {}}
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (OSError, UnicodeDecodeError):
            s["오류"] = "시나리오 파일을 읽지 못했다"
            out.append(s)
            continue
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
        out.append(s)
    return sorted(out, key=lambda s: s["번호"])


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
                     "결과": None if end is None else end.get(no, "끝 보고에 없음")})
    return rows


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
            remaining_text(cards, mine).replace("남은 예상", "남은 카드 크기 예상 합", 1).replace("—", "남은 카드 없음")]


def remaining_text(cards, mine):
    """남은(완료 아닌) 카드의 크기 예상 합 — 「남은 예상 N분 (예상 없는 카드 K장)」. 남은 카드가 없으면 「—」."""
    left = [c for c in mine if c["상태"] != "완료"]
    if not left:
        return "—"
    es = [size_estimate(cards, c)["분"] for c in left]
    return "남은 예상 %s (예상 없는 카드 %d장)" % (_분글(sum(e for e in es if e is not None)), es.count(None))


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
                "끝 보고를 보고 완료를 선언한다(시나리오 상태를 완료로)", 때, "../plan/" + END_REPORT % sc["번호"], sc["번호"])
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


def record_resolved(project, turn):
    """해소된 항목 한 줄을 `docs/plan/사람손-<SC-nn>.md` 끝에 덧붙인다. 실패하면 OSError."""
    plan = os.path.join(project, "docs", "plan")
    os.makedirs(plan, exist_ok=True)
    path = os.path.join(plan, "사람손-%s.md" % turn["시나리오"])
    head = ""
    if not os.path.exists(path):
        head = ("# %s 사람 손 기록\n\n사람이 해야만 일이 간 행동을 한 줄씩 덧붙인다. 보드가 사라짐을 알아챈 때를 적는다.\n\n"
                % turn["시나리오"])
    셈 = turn.get("셈", "사람 손")
    if turn["종류"] == "완료 선언":      # 선언했을 때만 끝. 고치러 간 것(확정 그대로·카드가 늘어 내려감)은 사람 손
        now = [s for s in read_scenarios(plan) if s["번호"] == turn["시나리오"]]
        셈 = "끝" if now and now[0]["상태"] == "완료" else "사람 손"
    what = turn["열어 볼 곳"] if turn["종류"] != "완료 선언" else turn["시나리오"]
    row = "- %s · %s · %s · %s — %s · 생긴 때 %s\n" % (
        dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), 셈, turn["종류"], what, turn["기다리는 것"], turn["생긴 때"])
    with open(path, "a", encoding="utf-8") as f:
        f.write(head + row)


def sync_turns(project, turns, 조용히=False):
    """직전 저장 목록과 비교한다. 새 열쇠는 알리고, 사라진 열쇠는 기록한다. 지금 목록에 알림 결과를 붙여 돌려준다.

    다른 기계 항목(`원격`)은 사라져도 이 기계 사람 손 기록에 쓰지 않고, 저장 파일이 없거나 `조용히` 인 바퀴에는 알리지 않고 기록만 한다."""
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
            if t.get("시나리오") and not t.get("원격"):
                record_resolved(project, t)
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
    if c["상태"] != "아이디어" or c["시나리오"] or c.get("출처") == "원격":
        return ""
    물음 = html.escape("return confirm(%s)" % json.dumps(
        "「%s」 카드를 %s 프로젝트의 대기로 올린다. 올리겠습니까?" % (c["title"], project), ensure_ascii=False), quote=True)
    return ('<form method="post" action="/p/%s/promote" onsubmit="%s"><input type="hidden" name="card" value="%s">'
            '<input type="hidden" name="confirm" value="1"><button type="submit">대기로 올리기</button></form>'
            % (urllib.parse.quote(project, safe=""), 물음, html.escape(c["file"], quote=True)))


def render_html(cards, project, every=0, scenarios=(), turns=(), plan_dir=None, alarms=(), 단추=False):
    """`plan_dir` 가 있으면 시나리오마다 끝 보고를 따로 읽어 완료 기준 줄의 결과로 쓴다(`read_scenarios` 는 끝 보고를 모른다)."""
    marks = card_marks(cards, scenarios)
    now = dt.datetime.now()

    def card(c, extra=""):
        here = phase_of(c["단계"])
        strip = ""
        if here:
            strip = '<p class="phases">%s</p>' % "".join(
                '<span class="%s">%s</span>' % ("on" if p == here else "off", p)
                for p in PHASES)
        bits = [x for x in (c["단계"], elapsed(c["시작"], c["누적"], c["상태"]),
                            "규모 %s" % c["크기"] if c["크기"] else "",
                            "갱신 %s" % c["갱신"] if c["갱신"] else "") if x]
        meta = " · ".join(html.escape(b) for b in bits)
        why = ('<p class="why">%s</p>' % html.escape(c["멈춘이유"])) if c["멈춘이유"] else ""
        why += "".join('<p class="why">%s</p>' % html.escape(m) for m in marks[c["file"]]) if extra else ""
        why += "".join('<p class="why">%s</p>' % html.escape(m) for m in c.get("원격표시", []))
        live = ('<p class="live">%s</p>' % html.escape(c["마지막줄"])) if c["마지막줄"] else ""
        plan = '<p class="plan">%s</p>' % html.escape(plan_line(cards, c, now))
        if _넘김(cards, c, now):
            plan += '<p class="why">예상 넘김</p>'
        src = ('<p class="meta">도는 기계 %s · %s 기록</p>' % (html.escape(c["도는 기계"]), html.escape(c["출처"]))
               if "출처" in c else "")
        # 원격에만 있는 카드도 상세로 가는 고리를 갖는다 (상세가 「본문은 이 기계에 없음」을 보인다)
        return ('<a class="card" href="%s"><h3>%s</h3>%s%s%s%s%s%s</a>'
                % (html.escape(c["file"]), html.escape(c["title"]), strip,
                   '<p class="meta">%s</p>' % meta if meta else "", plan, src, why, live))

    cols = "".join(
        '<section><h2>%s <span>%d</span></h2>%s</section>'
        % (col, len([c for c in cards if c["상태"] == col]),
           "".join(card(c) + (승격_폼(project, c) if 단추 else "") for c in cards if c["상태"] == col))
        for col in COLUMNS)
    esc = html.escape

    def piece(s):
        if s["오류"]:
            return ""
        p = scenario_progress(cards, s)
        if plan_dir:
            s = dict(s, 끝보고=end_report_results(plan_dir, s["번호"]))
        n = p["수"]
        line = "%s · 카드 없음 %d · 카드 아님 %d" % (
            " · ".join("%s %d" % (k, n[k]) for k in ("완료", "진행", "검토", "대기", "보류", "아이디어")),
            p["카드 없음"], p["카드 아님"])
        out = ["<p>%s</p>" % esc(line)]
        for r in criteria_rows(cards, s):
            out.append("<p>%s %s — %s — %s%s</p>" % (
                esc(r["번호"]), esc(r["문장"]),
                " · ".join("%s (%s)" % (esc(t), esc(st)) for t, st in r["카드"]) or "담은 카드 없음",
                "모두 완료" if r["모두 완료"] else "모두 완료 아님",
                " — 결과 %s" % esc(r["결과"]) if r["결과"] else ""))
        wait = waiting_rows(cards, s)
        out += ["<p>%s — %s</p>" % (esc(t), esc(", ".join(w))) for t, w in wait]
        if not wait:
            out.append("<p>선행 때문에 기다리는 카드 없음</p>")
        out += ["<p>%s</p>" % esc(x) for x in plan_sums(cards, [c for c in cards if c["시나리오"] == s["번호"]])]
        return '<div class="sc">%s</div>' % "".join(out)

    pieces = [piece(s) for s in scenarios] + [""]
    plans = "".join(
        '<section><h2>%s</h2>%s%s</section>' % (esc(head), pc, "".join(card(c, "marks") for c in mine))
        for (head, mine), pc in zip(scenario_groups(cards, scenarios), pieces))
    if scenarios:
        trs = []
        for s in scenarios:
            p = scenario_progress(cards, s)
            trs.append(["%s %s" % (esc(s["번호"]), esc(s["이름"])), esc(s["상태"]),
                        "%s %s" % (esc(s["오류"]), _막대(None)) if s["오류"] else _막대(p["퍼센트"]),
                        esc(" · ".join(p["기기"]) or "도는 기기 없음"),
                        esc(remaining_text(cards, [c for c in cards if c["시나리오"] == s["번호"]]))])
        progress = _표(["시나리오", "상태", "진행률", "돌고 있는 기기", "예상 완료"], trs)
    else:
        progress = '<div class="progress"><p class="meta">시나리오 없음</p></div>\n'

    def turn(t):
        why = '<p class="why">알림 못 띄움</p>' if t.get("알림") is False else ""
        return ('<a class="card" href="%s"><h3>%s</h3><p class="meta">%s · %s · %s · %s</p>%s</a>'
                % (html.escape(t["열어 볼 곳"], quote=True), html.escape(t["제목"]), html.escape(t["프로젝트"]),
                   html.escape(t["종류"]), html.escape(t["기다리는 것"]), html.escape(t["생긴 때"]), why))

    box = ('<h2>내가 할 일 (%d)</h2>%s' % (len(turns), "".join(turn(t) for t in turns))
           if turns else '<h2>내가 할 일 없음</h2>')
    due = '<div class="due"><h2>예상 완료</h2>%s</div>\n' % "".join(
        "<p>%s — %s</p>" % (k, esc(" · ".join(c["title"] + (" (예상 넘김)" if 넘 else "") for c, 넘 in v)) or "없음")
        for k, v in due_groups(cards, now))
    return TEMPLATE % {"project": html.escape(project), "cols": cols, "plans": plans, "progress": progress, "due": due,
                       "turns": render_alarms(alarms, lambda a: a["file"]) + '<div class="turns">%s</div>\n' % box,
                       "now": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "refresh": ('\n<meta http-equiv="refresh" content="%d">' % every)
                                  if every else ""}


FILE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(name)s</title>
<style>
:root { --bg:#fbfaf8; --fg:#1c1b19; --dim:#6b6762; --line:#e2ded8; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#171614; --fg:#eceae6; --dim:#9a958d; --line:#302e2a; } }
body { margin:0; padding:24px 16px 64px; background:var(--bg); color:var(--fg);
  font:15px/1.6 -apple-system, "Apple SD Gothic Neo", sans-serif; }
main { max-width:820px; margin:0 auto; }
a { color:var(--dim); font-size:13px; text-decoration:none; }
a:hover { color:var(--fg); }
h1 { font-size:15px; margin:12px 0 16px; color:var(--dim); font-weight:600; }
pre { white-space:pre-wrap; word-break:break-word; margin:0 0 16px; padding:16px;
  border:1px solid var(--line); border-radius:10px;
  font:13px/1.7 ui-monospace, SFMono-Regular, Menlo, monospace; }
.md h1, .md h2, .md h3, .md h4, .md h5, .md h6 { color:var(--fg); margin:24px 0 8px; line-height:1.35; }
.md h1 { font-size:22px; } .md h2 { font-size:18px; } .md h3 { font-size:16px; }
.md h4, .md h5, .md h6 { font-size:15px; }
.md p { margin:0 0 12px; }
.md ul, .md ol { margin:0 0 12px; padding-left:24px; }
.md table { border-collapse:collapse; margin:0 0 16px; font-size:14px; }
.md th, .md td { border:1px solid var(--line); padding:6px 10px; text-align:left; vertical-align:top; }
.md th { color:var(--dim); font-weight:600; }
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
<title>작업 보드 — 이 기계</title>
<style>
:root { --bg:#fbfaf8; --fg:#1c1b19; --dim:#6b6762; --line:#e2ded8; --card:#fff; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#171614; --fg:#eceae6; --dim:#9a958d; --line:#302e2a; --card:#201f1c; } }
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--fg);
  font:15px/1.6 -apple-system, "Apple SD Gothic Neo", sans-serif; }
header, .turns, .alarms, .nums, main { max-width:1200px; margin:0 auto 12px; }
h1 { font-size:20px; margin:0 0 4px; }
header p { margin:0; color:var(--dim); font-size:13px; }
.turns, .alarms { padding:10px; border:1px solid var(--line); border-radius:10px; }
.alarms { border-color:#b4553c; }
.nums { display:grid; gap:8px; grid-template-columns:repeat(auto-fit, minmax(200px,1fr)); }
.num { border:1px solid var(--line); border-radius:10px; padding:8px 12px; background:var(--card); }
.num summary { cursor:pointer; font-size:13px; color:var(--dim); }
.num summary b { font-size:22px; color:var(--fg); margin-right:6px; }
.num.risk summary b { color:#b4553c; }
.num.risk { border-color:#b4553c; }
.tabs a { font-size:13px; margin-right:12px; color:var(--dim); text-decoration:none; }
.tabs a.on { color:var(--fg); font-weight:600; border-bottom:2px solid var(--fg); }
main { display:grid; gap:12px; grid-template-columns:repeat(auto-fill, minmax(280px,1fr)); }
h2 { font-size:13px; margin:0 0 10px; color:var(--dim); font-weight:600; }
.card { display:block; background:var(--card); border:1px solid var(--line);
  border-radius:8px; padding:10px 12px; margin-bottom:8px; text-decoration:none; color:inherit; }
.card:hover { border-color:var(--dim); }
.card h3 { font-size:14px; margin:0; font-weight:600; line-height:1.4; }
.meta, .why { margin:6px 0 0; font-size:12px; color:var(--dim); }
.why { color:#b4553c; }
.progress { max-width:1200px; margin:12px auto 0; overflow-x:auto; }
.progress table { border-collapse:collapse; width:100%%; font-size:13px; }
.progress th, .progress td { border-bottom:1px solid var(--line); padding:4px 8px; text-align:left; white-space:nowrap; }
.progress th { color:var(--dim); font-weight:600; }
.bar { display:inline-flex; width:120px; height:8px; vertical-align:middle; background:var(--line);
  border-radius:4px; overflow:hidden; }
.bar > span { display:block; height:100%%; background:#4a8f5c; }
.bar > .아이디어 { background:#b9b4ab; } .bar > .대기 { background:#8aa4c8; } .bar > .진행 { background:#d4a53c; }
.bar > .검토 { background:#9a7cc4; } .bar > .보류 { background:#b4553c; } .bar > .완료 { background:#4a8f5c; }
.dot { display:inline-block; width:8px; height:8px; margin-left:6px; border-radius:50%%; background:var(--line); }
.dot.on { background:#b4553c; }
.sc p { margin:2px 0; font-size:12px; color:var(--dim); }
.machines { max-width:1200px; margin:12px auto 0; }
</style></head><body>
<header><h1>작업 보드 — 이 기계</h1>
<p>파일이 진실이다. 줄을 누르면 그 프로젝트 보드가 열린다. 만든 시각 %(now)s · <a href="/timeline">시간 막대</a> · <a href="/signals">비용·품질</a></p>%(err)s</header>
%(numbers)s%(alarms)s<div class="turns">%(turns)s</div>
%(progress)s<main>%(rows)s</main>%(machines)s
</body></html>
"""

TEMPLATE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">%(refresh)s
<title>작업 보드 — %(project)s</title>
<style>
:root { --bg:#fbfaf8; --fg:#1c1b19; --dim:#6b6762; --line:#e2ded8; --card:#fff; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#171614; --fg:#eceae6; --dim:#9a958d; --line:#302e2a; --card:#201f1c; } }
* { box-sizing: border-box; }
body { margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--fg);
  font:15px/1.6 -apple-system, "Apple SD Gothic Neo", sans-serif; }
header { max-width:1200px; margin:0 auto 20px; }
h1 { font-size:20px; margin:0 0 4px; }
header p { margin:0; color:var(--dim); font-size:13px; }
main { max-width:1200px; margin:0 auto; display:grid; gap:12px;
  grid-template-columns:repeat(6, minmax(0,1fr)); }
@media (max-width:900px) { main { grid-template-columns:1fr; } }
.turns, .alarms { max-width:1200px; margin:0 auto 12px; padding:10px; border:1px solid var(--line); border-radius:10px; }
.alarms { border-color:#b4553c; }
.plans { max-width:1200px; margin:12px auto 0; display:grid; gap:12px; }
section { background:transparent; border:1px solid var(--line); border-radius:10px; padding:10px; }
h2 { font-size:13px; margin:0 0 10px; color:var(--dim); font-weight:600;
  display:flex; justify-content:space-between; }
.card { display:block; background:var(--card); border:1px solid var(--line);
  border-radius:8px; padding:10px 12px; margin-bottom:8px; text-decoration:none; color:inherit; }
.card:hover { border-color:var(--dim); }
.card h3 { font-size:14px; margin:0; font-weight:600; line-height:1.4; }
.phases { display:flex; gap:3px; margin:8px 0 0; }
.phases span { flex:1; font-size:10px; text-align:center; padding:3px 0; border-radius:4px;
  border:1px solid var(--line); color:var(--dim); }
.phases .on { background:var(--fg); border-color:var(--fg); color:var(--bg); font-weight:600; }
.meta, .why { margin:6px 0 0; font-size:12px; color:var(--dim); }
.why { color:#b4553c; }
.live { margin:6px 0 0; font-size:12px; color:var(--dim); white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; }
.progress { max-width:1200px; margin:12px auto 0; overflow-x:auto; }
.progress table { border-collapse:collapse; width:100%%; font-size:13px; }
.progress th, .progress td { border-bottom:1px solid var(--line); padding:4px 8px; text-align:left; white-space:nowrap; }
.progress th { color:var(--dim); font-weight:600; }
.bar { display:inline-flex; width:120px; height:8px; vertical-align:middle; background:var(--line);
  border-radius:4px; overflow:hidden; }
.bar > span { display:block; height:100%%; background:#4a8f5c; }
.bar > .아이디어 { background:#b9b4ab; } .bar > .대기 { background:#8aa4c8; } .bar > .진행 { background:#d4a53c; }
.bar > .검토 { background:#9a7cc4; } .bar > .보류 { background:#b4553c; } .bar > .완료 { background:#4a8f5c; }
.dot { display:inline-block; width:8px; height:8px; margin-left:6px; border-radius:50%%; background:var(--line); }
.dot.on { background:#b4553c; }
.sc p { margin:2px 0; font-size:12px; color:var(--dim); }
.plan { margin:6px 0 0; font-size:12px; color:var(--dim); }
.due { max-width:1200px; margin:12px auto 0; font-size:13px; }
.due h2 { margin:0 0 4px; } .due p { margin:2px 0; }
</style></head><body>
<header><h1>작업 보드 — %(project)s</h1>
<p>파일이 진실이다. 상태를 바꾸려면 그 파일의 「상태:」 줄을 고친다. 만든 시각 %(now)s</p></header>
%(turns)s<main>%(cols)s</main>
%(progress)s%(due)s<div class="plans">%(plans)s</div>
</body></html>
"""

# ── 명령 팔레트 (ISSUE-sc02-e6-search-2026-11-20) ──
# 화면 틀 셋의 `</body>` 앞에 끼우는 인라인 조각 하나 — 바깥 파일을 부르지 않는다. 틀은 `%` 서식이라 끼울 때 `%` 를 겹친다.
# 주소 글자열에 `board.html` 을 쓰지 않는다(기존 시험이 화면에서 `board.html"` 의 첫 자리를 센다).
PALETTE = """<div id="palette" hidden><div id="pbox"><input id="pq" type="text" autocomplete="off" placeholder="검색 · 최근 카드 · 화면 이동"><div id="plist"></div></div></div>
<style>
#palette { position:fixed; top:0; left:0; right:0; bottom:0; z-index:99; background:rgba(0,0,0,.35);
  display:flex; justify-content:center; align-items:flex-start; padding-top:12vh; }
#palette[hidden] { display:none; }
#pbox { width:min(640px, 92vw); max-height:70vh; overflow:auto; background:var(--bg); color:var(--fg);
  border:1px solid var(--line); border-radius:12px; padding:10px; }
#pq { width:100%; box-sizing:border-box; padding:8px 10px; font:15px sans-serif; color:var(--fg); background:var(--bg);
  border:1px solid var(--line); border-radius:8px; }
.ph { margin:10px 0 4px; font-size:12px; color:var(--dim); font-weight:600; }
.pm { margin:2px 0; padding:4px 8px; font-size:13px; color:var(--dim); }
.pi { padding:6px 8px; border-radius:6px; font-size:14px; cursor:pointer; }
.pi.on { background:var(--line); }
.pi small { display:block; color:var(--dim); font-size:12px; }
</style>
<script>
(function () {
  var KEY = 'stoploop.recent', P = document.getElementById('palette'), Q = document.getElementById('pq');
  var L = document.getElementById('plist'), online = location.protocol !== 'file:';
  var rows = [], sel = 0, seq = 0, found = null, proj = [];
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
  function item(t, h, sub) {
    var i = rows.length, d = el(i === sel ? 'pi on' : 'pi', t, sub); rows.push(h);
    d.onclick = function () { location.href = h; };
  }
  function draw() {
    L.textContent = ''; rows = [];
    var q = Q.value.trim();
    el('ph', '검색');
    if (!online || found === false) el('pm', '서버에서만 됩니다');
    else if (q) {
      if (found === null) el('pm', '찾는 중…');
      else {
        if (!found.length) el('pm', '찾은 것 없음');
        found.slice(0, 10).forEach(function (c) { item(c['title'], c['href'], c['프로젝트'] + (c['줄'] ? ' · ' + c['줄'] : '')); });
        item('검색 화면에서 모두 보기', '/search?q=' + encodeURIComponent(q));
      }
    }
    el('ph', '최근 연 카드');
    var rc = recent().filter(function (x) { return hit(x.title); });
    if (!rc.length) el('pm', '아직 연 카드 없음');
    rc.forEach(function (x) { item(x.title, x.href); });
    el('ph', '화면 이동');
    [['첫 화면', '/'], ['검색 화면', '/search']].concat(proj.map(function (p) { return [p['name'] + ' 보드', p['href']]; }))
      .filter(function (x) { return hit(x[0]); }).forEach(function (x) { item(x[0], x[1]); });
    var pm = /^\\/p\\/([^\\/]+)\\//.exec(location.pathname);
    var names = (pm ? [decodeURIComponent(pm[1])] : proj.map(function (p) { return p['name']; }))
      .filter(function (n) { return hit('카드로 만들기 — ' + n); });
    if (names.length) {
      el('ph', '동작');
      names.forEach(function (n) { item('카드로 만들기 — ' + n, '/p/' + encodeURIComponent(n) + '/improve?made=0#고르기'); });
    }
  }
  function ask() {
    var my = ++seq, q = Q.value.trim(); found = q ? null : []; draw();
    if (!online) return;
    fetch('/search?as=json&q=' + encodeURIComponent(q)).then(function (r) { return r.json(); }).then(function (d) {
      if (my !== seq) return; found = d['카드']; proj = d['프로젝트'] || []; draw();
    }).catch(function () { if (my === seq) { found = false; draw(); } });
  }
  function open() { P.hidden = false; Q.value = ''; sel = 0; ask(); Q.focus(); }
  function close() { P.hidden = true; }
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
FILE_TEMPLATE = FILE_TEMPLATE.replace("</body>", PALETTE.replace("%", "%%") + "</body>")
HOME_TEMPLATE = HOME_TEMPLATE.replace("</body>", PALETTE.replace("%", "%%") + "</body>")
TEMPLATE = TEMPLATE.replace("</body>", PALETTE.replace("%", "%%") + "</body>")


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
            page = (FILE_TEMPLATE % {"name": html.escape(os.path.basename(path)),
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
    """(보는 목록, 오류 글). 항목은 {name, path, 표시, 기기}. 같은 이름은 앞의 것 하나, 목록 파일은 부를 때마다 다시 읽는다."""
    try:
        with open(LIST_FILE, encoding="utf-8") as f:
            data = json.load(f)
        raw = [x for k in ("projects", "보기") for x in (data.get(k) or [])]
    except FileNotFoundError:
        return [], ""
    except (OSError, ValueError, AttributeError, TypeError) as e:
        return [], "목록 파일을 읽지 못했다 — %s: %s" % (LIST_FILE, e)
    out = {}
    for x in raw:
        if not isinstance(x, dict) or not x.get("name") or not x.get("path"):
            continue
        name, path = str(x["name"]), os.path.expanduser(str(x["path"]))
        if name in out:
            if os.path.abspath(out[name]["path"]) != os.path.abspath(path):
                out[name]["표시"].append("같은 이름이 다른 경로로도 있다: %s" % path)
            continue
        out[name] = {"name": name, "path": path, "표시": [] if os.path.isdir(path) else ["경로 없음"],
                     "기기": str(x.get("기기") or "")}
    return list(out.values()), ""


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
CARD_PUSH_DIR = os.path.expanduser("~/.stop-loop/card-push")   # 러너가 올리기 결과를 쓰는 폴더. 보드는 읽기만 한다
REMOTE = {}                # {프로젝트 절대 경로: 그림}


def _토큰(account):
    """`gh auth token --user <계정>` 의 토큰. 못 얻으면 None (`runner.gh_token` 과 같다 — 보드는 runner 를 가져오지 못한다)."""
    try:
        env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
        res = subprocess.run(["gh", "auth", "token", "--user", account], capture_output=True, text=True, env=env)
    except OSError:
        return None
    return (res.stdout.strip() or None) if res.returncode == 0 else None


def _원격_git(project, *args, env=None):
    """원격 읽기가 git 을 부르는 유일한 자리. 묻는 창을 띄우지 않고, 시간 넘김·실행 불가도 실패한 결과로 돌려준다."""
    e = dict(os.environ if env is None else env)
    e["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(["git", *args], cwd=project, env=e, capture_output=True, encoding="utf-8",
                              errors="replace", timeout=REMOTE_GIT_TIMEOUT)
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


def _그림_없음():
    return {"없음": True, "때": None, "카드": {}, "오류": ""}


def _그림_실패(why):
    return {"없음": False, "때": None, "카드": {}, "오류": why}


def read_remote(project):
    """프로젝트 원격(origin)의 기준 가지에 있는 `docs/backlog/*.md` 의 머리말과 제목 — 그림 하나. 예외를 내지 않는다.

    주소로 직접 받고(`fetch <주소> <가지>`, FETCH_HEAD 를 쓰지 않음) 객체만 얻으므로 작업 사본의 파일·색인·
    `origin/*` 참조는 그대로다."""
    try:
        if not os.path.exists(os.path.join(project, ".git")):
            return _그림_없음()
        cfg = _프로젝트_설정(project)
        if cfg.get("handoff") == "local":
            return _그림_없음()
        got = _원격_git(project, "remote", "get-url", "origin")
        url = got.stdout.strip() if got.returncode == 0 else ""
        if not url:
            return _그림_없음()
        env = dict(os.environ)
        account = cfg.get("githubAccount")
        if isinstance(account, str) and account.strip():
            token = _토큰(account.strip())
            if not token:
                return _그림_실패("GitHub 계정 %s 의 토큰을 얻지 못함" % account.strip())
            env["GH_TOKEN"] = token
        r = _원격_git(project, "ls-remote", "--heads", url, env=env)
        if r.returncode != 0:
            return _그림_실패(_끝줄(r))
        heads = {}
        for line in r.stdout.splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                heads[ref[len("refs/heads/"):]] = sha.strip()
        base = next((x for x in ("develop", "main", "master") if x in heads), None)
        if base is None:
            return _그림_실패("원격에 기준 가지가 없다")
        r = _원격_git(project, "fetch", "-q", "--no-write-fetch-head", url, "refs/heads/" + base, env=env)
        if r.returncode != 0:
            return _그림_실패(_끝줄(r))
        tip = heads[base]
        r = _원격_git(project, "ls-tree", "--name-only", tip, "--", "docs/backlog/")
        if r.returncode != 0:
            return _그림_실패(_끝줄(r))
        cards = {}
        for name in sorted(os.path.basename(x) for x in r.stdout.splitlines()):
            if not name.endswith(".md") or name.startswith("_"):
                continue
            shown = _원격_git(project, "show", "%s:./docs/backlog/%s" % (tip, name))
            if shown.returncode != 0:
                cards[name] = {"머리말": None, "title": name, "커밋때": None}
                continue
            head = next((x[2:].strip() for x in shown.stdout.splitlines() if x.startswith("# ")), name)
            fm = remote_front_matter(shown.stdout)
            cards[name] = {"머리말": fm, "title": head,
                           "커밋때": _커밋때(project, tip, name) if fm and fm.get("상태") == "진행" else None}
        return {"없음": False, "때": dt.datetime.now(), "카드": cards, "오류": "", "끝": tip}
    except Exception as e:                           # 읽기가 무엇으로 실패하든 화면은 이 기계 것을 보인다
        return _그림_실패(str(e) or type(e).__name__)


def _커밋때(project, tip, name):
    """원격 기준 가지 끝에서 그 카드 파일을 마지막으로 고친 커밋의 때. 못 읽으면 None."""
    r = _원격_git(project, "log", "-1", "--format=%ct", tip, "--", "docs/backlog/" + name)
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
            pic = {**old, "오류": pic["오류"]}
        REMOTE[key] = pic


def 원격바퀴(extra, stop):
    """`stop` 이 서기 전까지 REMOTE_EVERY 초마다 보는 목록의 원격을 읽는다. 기다림은 Event 로 — 상주가 끝나면 곧 멈춘다."""
    while True:
        try:
            refresh_remote(_보는목록(extra)[0])
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
               else "원격 없음 — 다른 기계에서 보이지 않음"]
    elif pic["없음"]:
        out = ["원격 없음 — 다른 기계에서 보이지 않음"]
    else:
        when = pic["때"]
        읽음 = ("원격 읽음 " + when.strftime("%H:%M" if when.date() == dt.date.today() else "%Y-%m-%d %H:%M")) if when else ""
        if not pic["오류"]:
            out = [읽음]
        elif when:
            out = ["원격 못 읽음 — " + pic["오류"], 읽음]
        else:
            out = ["원격 못 읽음 — 이 기계 것만 — " + pic["오류"]]
    밀림, 거부 = _올리기_결과(item, cards)
    if 밀림:
        out.append("올리지 못한 고침 있음 (%d장)" % 밀림)
    if 거부:
        out.append("진행이 다른 기계에 보이지 않음 — 올리기 거부")
    return out


def _읽기(item, 합치기=False):
    """(카드, 시나리오, 할 일 카드, 사람 차례) — 작업목록 폴더를 못 읽으면 OSError. 합치기면 카드에 원격 그림을 합친다."""
    backlog = os.path.join(item["path"], "docs", "backlog")
    if not os.path.isdir(backlog):
        raise OSError("작업목록 폴더를 읽지 못했다: %s" % backlog)
    cards = read_cards(backlog)
    if 합치기:
        cards = merge_cards(cards, REMOTE.get(os.path.abspath(item["path"])), item.get("기기", ""))
    scenarios = read_scenarios(os.path.join(item["path"], "docs", "plan"))
    todo = read_todo_cards(os.path.join(item["path"], "docs", "todo"))
    return cards, scenarios, todo, human_turns(cards, scenarios, item["path"], todo)


_미읽음 = object()


def stalled_alarms(item, cards, 그림=None, now=None, lines=_미읽음):
    """한 프로젝트의 진행 카드마다 경보 0개 이상. `cards` 는 `merge_cards` 를 거친 카드(도는 기계·출처가 붙은 것).

    이 기계 카드(출처 이 기계, 도는 기계가 보는 목록 `기기` 와 같음)는 러너가 살아 있는지·실행 기록이 침묵 상한보다 조용한지,
    다른 기계 카드(출처 원격)는 원격 머리말 커밋이 시간 상한보다 오래됐는지를 본다. 도는 기계나 이 기계 이름을 모르면 판단하지 않는다.
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

        if c.get("출처") == "이 기계" and mine and _기기_이름(c.get("도는 기계")) == mine:
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
                add("다른 기계 — %d분째 바뀌지 않음, 확인 못함" % ((now - 때).total_seconds() // 60),
                    "원격 머리말 마지막 커밋 %s · %s시간 상한 %d분" % (때.strftime("%H:%M"), label, hard // 60))
    return out


def home_numbers(rows, today):
    """첫 화면 숫자 네 칸 — 보는 목록 모든 프로젝트의 합친 카드로 센다. 칸 = {이름, 수, 설명, 카드: [(프로젝트, 제목, 주소)], 위험}."""
    monday = (today - dt.timedelta(days=today.weekday())).isoformat()
    run, wait, done, stop = [], [], [], {}
    hold = alarm = over = 0
    remote = 0
    for r in rows:
        cards = {c["file"]: c for c in r.get("카드") or []}
        ref = lambda c, r=r: (r["name"], c["title"], _주소(r["name"]) + urllib.parse.quote(c["file"]))  # noqa: E731
        for c in cards.values():
            if c["상태"] == "진행":
                run.append(ref(c))
                remote += c.get("출처") == "원격"
            elif c["상태"] == "대기":
                wait.append(ref(c))
            elif c["상태"] == "완료" and _date(c["갱신"][:10]) and c["갱신"][:10] >= monday:
                done.append(ref(c))
            elif c["상태"] == "보류":
                hold += 1
                stop[(r["name"], c["file"])] = ref(c)
        files = {a["file"] for a in r.get("경보") or []}
        alarm += len(files)
        over += len(r.get("예상 넘김") or [])
        for f in sorted(files | set(r.get("예상 넘김") or [])):
            if f in cards:
                stop[(r["name"], f)] = ref(cards[f])
    return [
        {"이름": "진행 중", "수": len(run), "설명": "이 중 %d장 다른 기계" % remote, "카드": run, "위험": False},
        {"이름": "대기", "수": len(wait), "설명": "프로젝트 %d곳" % len({x[0] for x in wait}), "카드": wait, "위험": False},
        {"이름": "이번 주 완료", "수": len(done), "설명": "%s(월)부터" % monday[5:], "카드": done, "위험": False},
        {"이름": "멈춤·지연", "수": len(stop), "설명": "보류 %d · 경보 %d · 예상 넘김 %d" % (hold, alarm, over),
         "카드": list(stop.values()), "위험": len(stop) >= 1}]


def read_resolved(items):
    """보는 목록 프로젝트의 `docs/plan/사람손-*.md` 기록 줄들 — [{프로젝트, 때, 글}], 때가 늦은 것이 위. 못 읽은 파일은 때가 빈 한 줄."""
    out = []
    for item in items:
        for path in sorted(glob.glob(os.path.join(item["path"], "docs", "plan", "사람손-*.md"))):
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


def project_row(item):
    """첫 화면의 프로젝트 한 줄. 못 읽으면 그 줄의 「까닭」에만 적는다."""
    row = {"name": item["name"], "수": {c: 0 for c in COLUMNS[1:5]}, "도는 카드": [], "차례 수": 0,
           "마지막 움직임": "없음", "까닭": "", "표시": list(item["표시"]), "원격": [], "남은 예상": "—",
           "상태수": {c: 0 for c in COLUMNS}, "배지": "", "진행률": None, "기기": [], "위험": [],
           "경보": [], "카드": [], "예상 넘김": []}
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
    row["도는 카드"] = [(c["title"], c["단계"]) for c in cards if c["상태"] == "진행"]
    row["차례 수"] = len(turns)
    row["원격"] = _원격_문구(item, cards)
    backlog = os.path.join(item["path"], "docs", "backlog")
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
                turns = sync_turns(item["path"], turns, 조용히)
                remote_files = {c["file"] for c in cards if c.get("출처") == "원격"}
                alarms = [{**a, "원격": a["file"] in remote_files} for a in stalled_alarms(item, cards, 그림)]
                sync_alarms(item, alarms, 조용히)
            else:
                turns = _알림결과(item["path"], turns)
        except Exception as e:                       # 한 프로젝트가 실패해도 나머지는 계속 돈다
            bad.append({"열쇠": "못 읽음:" + item["name"], "종류": "못 읽음", "프로젝트": item["name"], "시나리오": "",
                        "제목": "%s — 이 프로젝트 사람 차례 못 읽음" % item["name"], "기다리는 것": str(e),
                        "생긴 때": "", "열어 볼 곳": _주소(item["name"]) + "board.html", "알림": None})
            continue
        out += [{**t, "프로젝트": item["name"], "열어 볼 곳": _주소(item["name"]) + t["열어 볼 곳"]} for t in turns]
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


def render_alarms(alarms, href=lambda a: a["열어 볼 곳"]):
    """경보 묶음. 없으면 빈 글 — 그 자리를 그리지 않는다."""
    if not alarms:
        return ""
    esc = html.escape
    return '<div class="alarms"><h2>멈춘 카드 경보 (%d)</h2>%s</div>\n' % (len(alarms), "".join(
        '<a class="card" href="%s"><h3>%s · %s</h3><p class="meta">%s — %s%s</p></a>'
        % (esc(href(a), quote=True), esc(a["프로젝트"]), esc(a["title"]), esc(a["종류"]), esc(a["근거"]),
           " · 알림 못 띄움" if a.get("알림") is False else "") for a in alarms))


TABS = (("", "기다리는 것", "/"), ("old", "오래 기다린 것", "/?tab=old"), ("done", "처리한 것", "/?tab=done"))


def render_home(rows, turns, 오류, tab="", 처리=(), today=None, machines=""):
    """첫 화면 — 맨 위 숫자 네 칸과 멈춘 카드 경보, 그 아래 사람 차례(세 탭)와 프로젝트 줄. `tab` 이 old·done 이 아니면 기다리는 것."""
    esc = html.escape
    today = today or dt.date.today()
    tab = tab if tab in ("old", "done") else ""

    def turn(t):
        return ('<a class="card" href="%s"><h3>%s</h3><p class="meta">%s</p></a>'
                % (esc(t["열어 볼 곳"], quote=True), esc(t["제목"]),
                   esc(" · ".join(x for x in (t["프로젝트"], t["종류"], t["기다리는 것"], t["생긴 때"],
                                              "알림 못 띄움" if t.get("알림") is False else "") if x))))

    def row(r):
        n = r["수"]
        도는 = " · ".join("%s (%s)" % (t, s or "단계 모름") for t, s in r["도는 카드"]) or "도는 카드 없음"
        extra = "".join('<p class="why">%s</p>' % esc(x) for x in r["표시"] + ([r["까닭"]] if r["까닭"] else []) + r.get("원격", []))
        dot = ('<span class="dot on" title="%s"></span>' % esc("; ".join(r["위험"]), quote=True)
               if r["위험"] else '<span class="dot"></span>')
        return ('<a class="card" href="%sboard.html"><h3>%s%s</h3><p class="meta">%s</p><p class="meta">%s</p>'
                '<p class="meta">사람 차례 %d · 마지막 움직임 %s</p>%s</a>'
                % (esc(_주소(r["name"]), quote=True), esc(r["name"]), dot,
                   " · ".join("%s %d" % (c, n[c]) for c in n), esc(도는), r["차례 수"], esc(r["마지막 움직임"]), extra))

    def lines(title, empty, htmls):
        return "<h2>%s (%d)</h2>%s" % (title, len(htmls), "".join(htmls)) if htmls else "<h2>%s</h2>" % empty

    if tab == "old":
        box = lines("오래 기다린 것", "오래 기다린 것 없음", [turn(t) for t in turns if t["종류"] != "못 읽음"
                    and _date(t["생긴 때"][:10]) and t["생긴 때"][:10] < today.isoformat()])
    elif tab == "done":
        box = lines("처리한 것", "처리한 것 없음", ['<div class="card"><h3>%s</h3><p class="meta">%s</p></div>'
                    % (esc(x["글"]), esc(" · ".join(y for y in (x["때"], x["프로젝트"]) if y))) for x in 처리])
    else:
        box = ("<h2>사람 차례 (%d)</h2>%s" % (len(turns), "".join(turn(t) for t in turns))
               if turns else "<h2>사람 차례 없음</h2>")
    nav = '<nav class="tabs">%s</nav>' % "".join(
        '<a%s href="%s">%s</a>' % (' class="on"' if k == tab else "", href, label) for k, label, href in TABS)
    box = nav + box

    def num(c):
        return ('<details class="num%s"><summary><b>%d</b> %s</summary><p class="meta">%s</p>%s</details>'
                % (" risk" if c["위험"] else "", c["수"], esc(c["이름"]), esc(c["설명"]),
                   "".join('<a class="card" href="%s"><h3>%s · %s</h3></a>' % (esc(h, quote=True), esc(p), esc(t))
                           for p, t, h in c["카드"])))
    numbers = '<div class="nums">%s</div>\n' % "".join(num(c) for c in home_numbers(rows, today))
    alarms = render_alarms([a for r in rows for a in r.get("경보") or []])
    err = '<p class="why">%s</p>' % esc(오류) if 오류 else ""
    body = ("".join(row(r) for r in rows) if rows else
            '<p class="meta">보는 프로젝트 없음 — %s 의 "projects"·"보기" 에 올리면 보인다</p>' % esc(LIST_FILE))
    def prow(r):
        if r["까닭"]:
            return [esc(r["name"]), "읽지 못함", "—", "—", "—", ""]
        total = sum(r["상태수"].values())
        segs = "".join('<span class="%s" style="width:%d%%" title="%s %d"></span>'
                       % (c, r["상태수"][c] * 100 // total if total else 0, c, r["상태수"][c]) for c in COLUMNS)
        segs = segs.replace('class="', 'class="seg ')
        return [esc(r["name"]), esc(r["배지"]), _막대(r["진행률"]), esc(" · ".join(r["기기"]) or "도는 기기 없음"), esc(r["남은 예상"]),
                '<span class="bar" title="카드 %d">%s</span>' % (total, segs)]
    progress = (_표(["프로젝트", "상태", "진행률", "돌고 있는 기기", "예상 완료", "칸반 비율"], [prow(r) for r in rows])
                if rows else "")
    return HOME_TEMPLATE % {"err": err, "turns": box, "rows": body, "progress": progress, "numbers": numbers, "alarms": alarms, "machines": machines,
                            "now": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


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
    return subprocess.run(["git", "-C", project] + list(args), capture_output=True, text=True, timeout=30)


def _소각_읽기(project, sha):
    """소각 커밋이 지운 `state.json` 의 metrics. (metrics, None) 또는 (None, 까닭). 예외를 내지 않는다."""
    r = _git_읽기(project, "diff-tree", "--no-commit-id", "-r", "--diff-filter=D", "--name-only", sha)
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
           "판정수": {n: {"판정": 0, "반려": 0} for n in ("02", "06", "08", "09")}}
    stages, out["상태오류"] = read_status_stages(os.path.basename(os.path.abspath(project)), stem)
    for st, v in stages.items():
        if isinstance(v, dict):
            _더하기(out["단계"], str(st), _수(v.get("runs")), _수(v.get("ms")), _수(v.get("cost")), str(v.get("model") or ""), v)
    try:
        r = _git_읽기(project, "log", "--all", "--topo-order", "--reverse", "--date=format:%Y-%m-%d %H:%M",
                      "--format=" + _이력_서식)
        if r.returncode != 0:
            out["오류"] = "커밋 이력을 읽지 못했다"
            return out
        for rec in r.stdout.split("\x1e")[1:]:
            f = [x.strip() for x in rec.split(_구분)]
            if len(f) != 9:
                continue
            sha, 때, 제목, issue, card, num, resumed, verdict, findings = f
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
            out["커밋"].append((때, num or ("머리말" if card else "—"), 제목))
    except Exception:                                # git 이 없거나 느려도 화면은 나머지를 그대로 낸다
        out["오류"] = "커밋 이력을 읽지 못했다"
    return out


# ── 비용·품질 화면 (ISSUE-sc02-e10-cost-quality-2026-11-20) ──
# 요청마다 git 과 파일을 읽는다 — 캐시·색인이 없다. 비용·토큰·판정 회차는 카드 상세와 같은 `read_card_history` 에서 온다.
_멈춤_까닭 = "같은 지적 재발"
_판정_단계 = ("02", "06", "08", "09")


def read_stops(project):
    """실행 폴더(`.workflow/<카드>-<날짜>/`)마다 git 에 마지막으로 쓰인 state.json 의 `stops` 에서 카드별 「같은 지적 재발」 수.
    ({카드: 수}, {못 읽은 카드: 까닭}, 오류 글). 폴더가 하나도 읽히지 않은 카드만 못 읽은 것이다. 예외를 내지 않는다."""
    try:
        r = _git_읽기(project, "-c", "core.quotepath=false", "log", "--all", "--diff-filter=AM", "--name-only", "--format=\x1e%H", "--",
                     ".workflow/*/state.json")
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
        r = _git_읽기(project, "log", "--all", "--merges", "--format=%s")
        if r.returncode != 0:
            return {}, "커밋 이력을 읽지 못했다"
    except Exception:
        return {}, "커밋 이력을 읽지 못했다"
    out = {}
    for 제목 in r.stdout.splitlines():
        m = re.match(r"\[loop\] (.+?) — 자동 병합", 제목)
        if m:
            out[m.group(1)] = "러너"
            continue
        m = re.match(r"Merge pull request #\d+ from [^/\s]+/loop/(\S+)", 제목) or re.match(r"Merge branch 'loop/([^']+)'", 제목)
        if m:
            out.setdefault(m.group(1), "사람")
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
        r = {"name": item["name"], "오류": [], "제목": {}, "묶음": {}}
        out.append(r)
        backlog = os.path.join(item["path"], "docs", "backlog")
        try:
            if not os.path.isdir(backlog):
                raise OSError("작업목록 폴더를 읽지 못했다: %s" % backlog)
            cards = merge_cards(read_cards(backlog), REMOTE.get(os.path.abspath(item["path"])), item.get("기기", ""))
        except Exception as e:
            r["오류"].append(str(e))
            continue
        started = []
        for c in cards:
            if not _카드_쓸것(c, cond):
                continue
            h = read_card_history(item["path"], c["file"])
            if (c.get("시작") or "").strip() or any(x[1].isdigit() for x in h["커밋"]):
                started.append((c, h))
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
            for 이름 in ("전체", c.get("시나리오") or "시나리오 없음"):
                _합에_더하기(r["묶음"].setdefault(이름, _합()), stem, 정보)
    return out, ""


_숫자이름 = re.compile(r"cost|stop|merged|auto|cost:[0-9][0-9a-z]*|(?:verdict|rej):\d+|none:(?:cost|verdict|stop|merge)")


def render_signals(qs, 결과, 까닭):
    """비용·품질 화면 HTML. 값은 모두 escape 한다. 숫자는 고리이고 누르면 `show=<숫자 이름>` 으로 카드 목록이 아래에 나온다."""
    esc = html.escape

    def v(k):
        return (qs.get(k) or [""])[0]
    box = lambda k, label, typ="text": '<label>%s <input type="%s" name="%s" value="%s"></label> ' % (
        label, typ, k, esc(v(k), quote=True))
    parts = ['<form method="get" action="/signals"><p>%s%s%s%s<button type="submit">보기</button></p></form>'
             % (box("p", "프로젝트"), box("sc", "시나리오"), box("from", "기간 시작", "date"), box("to", "끝", "date")),
             "<p>기간은 카드의 갱신 날짜 기준이다. 시작한 적이 있는 카드만 센다.</p>"]
    if 까닭:
        parts.append("<p>%s</p>" % esc(까닭))
    elif not 결과:
        parts.append("<p>보는 프로젝트 없음</p>")

    def 고리(프로젝트, 묶음, show, 글):
        q = {k: v(k) for k in ("sc", "from", "to") if v(k)}
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
    틀 = FILE_TEMPLATE.replace('<a href="board.html">← 보드</a>', '<a href="/">← 첫 화면</a>')
    return 틀 % {"name": "비용·품질", "body": "\n".join(parts)}


def _표_html(head, rows):
    return ("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
            % ("".join("<th>%s</th>" % html.escape(h) for h in head),
               "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % html.escape(str(c)) for c in r) for r in rows)))


def _시나리오_절(card, cards, scenarios):
    no = card.get("시나리오", "")
    if not no:
        return ""
    s = next((x for x in scenarios if x["번호"] == no), None)
    if s is None:
        return "<h2>시나리오</h2><p>%s — 시나리오 파일을 찾지 못했다</p>" % html.escape(no)
    if s["오류"]:
        return "<h2>시나리오</h2><p>%s — %s</p>" % (html.escape(no), html.escape(s["오류"]))
    rows = [("시나리오", '%s %s' % (no, s["이름"]))]
    rows += [("요소", "%s %s" % (e, s["요소이름"].get(e, "")))
             for e in card["요소"]]
    for ac in card["완료기준"] + card["인수조건"]:
        rows.append(("완료 기준 " + ac, s["완료기준"].get(ac, "시나리오에 없는 완료 기준")))
    for e in card["선행"]:
        held = next((c for c in cards if c["시나리오"] == no and e in c["요소"]), None)
        rows.append(("선행", "%s (%s)" % (e, held["상태"] if held else "카드 없음")))
    return '<h2>시나리오</h2><p><a href="board.html">보드로</a></p>' + _표_html(("항목", "내용"), rows)


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


def render_card_detail(item, card, cards, scenarios, history, 머리말=None):
    """카드 상세 HTML 문자열. 예외 없이 문자열을 돌려준다. 값을 못 읽은 자리는 이유를 적고 나머지는 그대로 낸다."""
    esc = html.escape
    path = os.path.join(item["path"], "docs", "backlog", card["file"])
    parts = [승격_폼(item["name"], card)]
    try:
        fm = 머리말 if 머리말 is not None else front_matter(path)
    except (OSError, UnicodeDecodeError):
        fm = {}
    if "출처" in card:
        parts.append("<p>출처: %s · 도는 기계 %s</p>" % (esc(card["출처"]), esc(card["도는 기계"])))
    parts.append("<h2>머리말</h2>" + (_표_html(("키", "값"), list(fm.items())) if fm else "<p>머리말 없음</p>"))
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        try:
            shown = 본문_그리기(text)
        except Exception:                            # 그리기가 무엇으로 실패하든 원문이 보인다
            shown = "<pre>%s</pre>" % esc(text)
        parts.append("<h2>본문</h2>" + shown)
    except OSError:
        parts.append("<h2>본문</h2><p>본문은 이 기계에 없음 — 원격에만 있는 카드</p>")
    h = history
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
    parts.append(_시나리오_절(card, cards, scenarios))
    parts.append(_기능_절(card, read_featuremap(item["path"])))
    return FILE_TEMPLATE % {"name": esc(card["title"]), "body": "\n".join(parts)}


def _없는_카드(name):
    body = '<p>그런 카드 없음 — %s</p>' % html.escape(name)
    return FILE_TEMPLATE % {"name": "그런 카드 없음", "body": body}


# ── 지난 작업 검색 (ISSUE-sc02-e6-search-2026-11-20) ──
# 요청마다 git 과 파일을 읽는다 — 색인·캐시를 두지 않는다. 카드와 커밋을 잇는 규칙은 `read_card_history` 와 같다.
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
            "반려": g("rej") == "1", "아래": None, "위": None}
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
    r = _git_읽기(project, "log", "--all", "--diff-filter=D", "-p", "--format=\x1e%H", "--", ".workflow/*/10-handoff.md")
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
                found += [dict(x, 출처="커밋 메시지", 어디="%s %s" % (sha[:7], 때)) for x in find_lines(msg, 낱말)]
            for folder in sorted(f for f, o in owner.items() if o == stem):
                found += [dict(x, 출처="인계 문서", 어디=folder) for x in find_lines(handoffs[folder], 낱말)]
            if not found:
                continue
        if cond["아래"] is not None or cond["위"] is not None:
            cost = sum(v["cost"] for v in read_card_history(item["path"], c["file"])["단계"].values())
            if (cond["아래"] is not None and cost < cond["아래"]) or (cond["위"] is not None and cost > cond["위"]):
                continue
        out["카드"].append({"file": c["file"], "title": c["title"], "찾은 것": found,
                           "href": "/p/%s/%s" % (urllib.parse.quote(item["name"], safe=""), urllib.parse.quote(c["file"], safe=""))})
    return out


def search(items, qs):
    """(결과, 까닭) — 낱말·거르개가 모두 비면 아무것도 읽지 않는다. 까닭이 있으면 찾지 않는다. 결과는 카드나 오류가 있는 프로젝트만."""
    cond, 까닭 = _조건(qs)
    if 까닭 or not (cond["낱말"] or _거르개_글(cond)):
        return [], 까닭
    found = [_프로젝트_찾기(p, cond) for p in items]
    return [r for r in found if r["카드"] or r["오류"]], ""


def _검색_입력(qs):
    def v(k):
        return html.escape((qs.get(k) or [""])[0], quote=True)
    box = lambda k, label, typ="text": '<label>%s <input type="%s" name="%s" value="%s"></label> ' % (label, typ, k, v(k))
    rej = (qs.get("rej") or [""])[0] == "1"
    return ('<form method="get" action="/search"><p>%s</p><p>%s%s%s</p><p>%s%s'
            '<label>반려 <select name="rej"><option value="">—</option><option value="1"%s>반려 있음</option></select></label> '
            '%s%s</p><p><button type="submit">찾기</button></p></form>'
            % (box("q", "낱말"), box("sc", "시나리오"), box("fn", "기능 ID"), box("dev", "기기"),
               box("from", "기간 시작", "date"), box("to", "끝", "date"), " selected" if rej else "",
               box("cmin", "비용 아래(달러)", "number"), box("cmax", "위", "number")))


def render_search(qs, 결과, 까닭):
    """검색 화면 HTML. 값은 모두 escape 한다."""
    esc = html.escape
    cond = _조건(qs)[0]
    거르개 = _거르개_글(cond)
    parts = [_검색_입력(qs)]
    if 까닭:
        parts.append("<p>%s</p>" % esc(까닭))
    elif not (cond["낱말"] or 거르개):
        parts.append("<p>낱말이나 거르개를 넣고 찾는다</p>")
    else:
        n = sum(len(r["카드"]) for r in 결과)
        parts.append("<p>%d 건 — 거르개: %s</p>" % (n, esc(", ".join(거르개) or "없음")))
        if not n:
            parts.append("<p>찾은 것 없음</p>")
        for r in 결과:
            parts.append("<h2>%s</h2>" % esc(r["name"]))
            parts += ["<p>찾지 못함 — %s</p>" % esc(x) for x in r["오류"]]
            for c in r["카드"]:
                parts.append('<h3><a href="%s">%s</a></h3>' % (esc(c["href"], quote=True), esc(c["title"])))
                for f in c["찾은 것"]:
                    parts.append("<p>%s%s</p><pre>%s</pre>" % (
                        esc(f["출처"]), esc(" · " + f["어디"]) if f["어디"] else "",
                        esc("\n".join(([f["앞"]] if f["앞"] else []) + [f["줄"]] + ([f["뒤"]] if f["뒤"] else [])))))
    틀 = FILE_TEMPLATE.replace('<a href="board.html">← 보드</a>', '<a href="/">← 첫 화면</a>')
    return 틀 % {"name": "지난 작업 검색", "body": "\n".join(parts)}


def search_json(items, 결과, 까닭):
    cards = [{"프로젝트": r["name"], "title": c["title"], "href": c["href"],
              "줄": c["찾은 것"][0]["줄"] if c["찾은 것"] else ""} for r in 결과 for c in r["카드"]]
    return json.dumps({"건": len(cards), "까닭": 까닭, "카드": cards,
                       "프로젝트": [{"name": p["name"], "href": "/p/%s/board.html" % urllib.parse.quote(p["name"], safe="")}
                                 for p in items]}, ensure_ascii=False)


# ── 기계별 시간 막대 (ISSUE-sc02-e8-timeline-2026-11-20) ──
# 요청마다 git 을 읽는다 — 캐시·색인을 두지 않는다. 이어받기·재인계를 세는 규칙은 `read_card_history` 와 같다 —
# 바꿀 때 두 곳(`_이력_서식` 의 `Loop-Resumed` 읽기·`read_card_history` 의 셈과 아래 `read_timeline_project`)을 함께 고친다.
_시간_서식 = "\x1e" + _구분.join(["%H", "%ct", "%s"] + ["%(trailers:key=Loop-" + k + ",valueonly)" for k in ("Issue", "Card", "Resumed")])


def _카드_내용들(project, 쪽지):
    """`[(해시, 카드 파일)]` 의 그 커밋 때 카드 파일 내용 `{(해시, 카드 파일): 글}`. 없는 것은 빠진다."""
    if not 쪽지:
        return {}
    r = subprocess.run(["git", "-C", project, "cat-file", "--batch"], capture_output=True, timeout=30,
                       input="".join("%s:docs/backlog/%s\n" % x for x in 쪽지).encode("utf-8"))
    out, data, pos = {}, r.stdout, 0
    for key in 쪽지:
        end = data.index(b"\n", pos)
        head = data[pos:end].decode("utf-8", "replace").split()
        pos = end + 1
        if len(head) == 3 and head[1] == "blob":
            size = int(head[2])
            out[key] = data[pos:pos + size].decode("utf-8", "replace")
            pos += size + 1
    return out


def read_timeline_project(project, 끝=None, now=None):
    """한 프로젝트의 시간 막대 자료 — {"구간": [...], "작은 수정": [(때, 제목)], "완료 때": {카드 파일: 때}}. git 을 못 읽으면 OSError."""
    now = now or dt.datetime.now()
    log = ["git", "-C", project, "log", "--all"] + ([끝] if 끝 else []) + ["--format=" + _시간_서식]
    r = subprocess.run(log, capture_output=True, text=True, timeout=30)
    if r.returncode != 0 and 끝:                       # 원격 끝 객체가 이 기계에 없으면 이 기계 참조만 읽는다
        r = subprocess.run(log[:5] + log[6:], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise OSError("커밋 이력을 읽지 못했다: %s" % (r.stderr.strip().splitlines() or ["git 실패"])[-1])
    commits = []
    for rec in r.stdout.split("\x1e")[1:]:
        f = [x.strip() for x in rec.split(_구분)]
        if len(f) == 6:
            commits.append({"sha": f[0], "때": dt.datetime.fromtimestamp(int(f[1])), "제목": f[2],
                            "issue": f[3].removesuffix(".md"), "card": f[4].removesuffix(".md"), "resumed": f[5] == "true"})
    commits.sort(key=lambda c: c["때"])
    heads = [c for c in commits if c["card"] and not c["issue"] and c["제목"].startswith("작업목록:")]
    내용 = _카드_내용들(project, [(c["sha"], c["card"] + ".md") for c in heads])
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

    def 기계(fm):
        return next((v for v in (_기기_이름(fm.get("돈기계")), _기기_이름(fm.get("기기"))) if v), "기기 모름")

    구간, 완료때 = [], {}
    cards = set(by_card) | set(steps)
    for card in sorted(cards):
        resumed = [x["때"] for x in commits if x["resumed"] and card in (x["issue"], x["card"])]
        now_fm, now_title = 지금(card)
        n_resumed = len(resumed)
        n_hand = int(now_fm.get("재인계", "")) if now_fm.get("재인계", "").isdigit() else 0
        seq = by_card.get(card)
        if not seq:
            times = [c["때"] for c in steps[card]]
            구간.append({"카드": card + ".md", "제목": now_title, "기계": 기계(now_fm), "시작": min(times), "끝": max(times), "진행 중": False,
                       "기록 없음": True, "끊김": [(t, "이어받기") for t in sorted(set(resumed)) if min(times) <= t <= max(times)],
                       "이어받기": n_resumed, "재인계": n_hand})
            완료때[card + ".md"] = max(times)
            continue
        run, prev = None, None
        for c in seq:
            state = c["fm"].get("상태", "대기")
            if state == "진행":
                run = (run or {"시작": c["때"], "커밋": []})
                run["커밋"].append(c)
            elif run:
                구간.append(_구간_만들기(card, run, c["때"], False, 기계, n_resumed, n_hand, resumed))
                run = None
            if state == "완료" and prev != "완료":
                완료때[card + ".md"] = c["때"]
            prev = state
        if run:
            구간.append(_구간_만들기(card, run, now, True, 기계, n_resumed, n_hand, resumed))
    작은 = [(c["때"], c["제목"]) for c in commits if c["제목"].startswith("작은 수정:")]
    return {"구간": 구간, "작은 수정": 작은, "완료 때": 완료때}


def _구간_만들기(card, run, 끝, 진행중, 기계, n_resumed, n_hand, resumed):
    last = run["커밋"][-1]
    cuts = [(t, "이어받기") for t in sorted(set(resumed)) if run["시작"] <= t <= 끝]
    cuts += [(c["때"], "재인계") for c in run["커밋"] if "재인계" in [k.strip() for k in c["열쇠"].split(",")]]
    return {"카드": card + ".md", "제목": last["제목글"], "기계": 기계(last["fm"]), "시작": run["시작"], "끝": 끝, "진행 중": 진행중,
            "기록 없음": False, "끊김": sorted(cuts), "이어받기": n_resumed, "재인계": n_hand}


def timeline_period(qs, today):
    """볼 기간 `(시작 datetime, 끝 datetime, 까닭)`. 끝은 마지막 날 다음 날 00:00. 기본은 오늘을 끝으로 한 7일."""
    today = dt.datetime(today.year, today.month, today.day)
    기본 = (today - dt.timedelta(days=6), today + dt.timedelta(days=1), "볼 기간을 읽지 못해 오늘까지 7일로 보인다")
    g = lambda k: (qs.get(k) or [""])[0].strip()          # noqa: E731
    a, z = g("시작"), g("끝")
    if not a and not z:
        return 기본[0], 기본[1], ""
    if (a and not _날짜(a)) or (z and not _날짜(z)):
        return 기본
    last = dt.datetime.strptime(z, "%Y-%m-%d") if z else today
    first = dt.datetime.strptime(a, "%Y-%m-%d") if a else last - dt.timedelta(days=6)
    if first > last:
        return 기본
    return first, last + dt.timedelta(days=1), ""


_움직임_서식 = "\x1e" + _구분.join(["%ct"] + ["%(trailers:key=Loop-" + k + ",valueonly)" for k in ("Issue", "Card", "Plugin")])


def read_moves(project, 끝=None):
    """커밋마다 `{"때", "카드"(.md 뺀 이름), "단계"(러너 단계 커밋이면 True), "판"}`. 카드는 `Loop-Issue`, 없으면 `Loop-Card`. 못 읽으면 OSError."""
    log = ["git", "-C", project, "log", "--all"] + ([끝] if 끝 else []) + ["--format=" + _움직임_서식]
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
        r = subprocess.run(["git", "-C", project, *args], capture_output=True, text=True, timeout=30)
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
            줄 += [{**x, "프로젝트": item["name"]} for x in read_feed(item["path"], 끝, 맵)]
        except Exception:                              # 한 프로젝트의 실패가 다른 프로젝트 줄을 막지 않는다
            못.append(item["name"])
    if 거르개.get("기계"):
        줄 = [x for x in 줄 if x["기계"] == 거르개["기계"]]
    줄.sort(key=lambda x: (x["때"], x["프로젝트"], x["카드"], x["결과"]), reverse=True)
    return 줄[:FEED_MAX], 못


def _얼마_전(때):
    분 = max(0, int((dt.datetime.now() - 때).total_seconds() // 60))
    return "방금" if 분 < 1 else "%d분 전" % 분 if 분 < 60 else "%d시간 전" % (분 // 60) if 분 < 1440 else "%d일 전" % (분 // 1440)


def render_feed(줄, 못, 거르개, 프로젝트들, 기계들):
    """최근 활동 구역 하나. 줄 전체가 그 카드 상세 링크이고 거르개는 GET 폼이다."""
    esc = html.escape

    def select(name, 고른, 값들, 처음):
        opts = "".join('<option value="%s"%s>%s</option>' % (esc(v, quote=True), " selected" if v == 고른 else "", esc(v)) for v in 값들)
        return '<select name="%s"><option value="">%s</option>%s</select>' % (name, 처음, opts)

    form = ('<form method="get">%s %s <button type="submit">거르기</button></form>'
            % (select("feed_project", 거르개.get("프로젝트", ""), 프로젝트들, "모든 프로젝트"),
               select("feed_machine", 거르개.get("기계", ""), 기계들, "모든 기계")))
    body = "".join('<p><a href="/p/%s/%s">%s</a></p>' % (
        urllib.parse.quote(x["프로젝트"], safe=""), urllib.parse.quote(x["카드"], safe=""),
        esc("%s · %s · %s (%s, %s)" % (x["카드"].removesuffix(".md"), x["단계"], x["결과"], x["기계"], _얼마_전(x["때"]))))
        for x in 줄) or "<p>최근 활동 없음</p>"
    why = "".join('<p class="why">%s: 이 프로젝트 활동 못 읽음</p>' % esc(n) for n in 못)
    return '<div class="feed"><h2>최근 활동</h2>%s%s%s</div>' % (form, why, body)


def _피드_구역(items, rows, qs, 고정=None):
    """화면에 끼울 피드 HTML. 어떤 예외가 나도 나머지 화면이 뜨도록 구역 안에서 막는다. `고정` 은 프로젝트 보드가 거르는 프로젝트."""
    g = lambda k: (qs.get(k) or [""])[0].strip()          # noqa: E731
    거르개 = {"프로젝트": 고정 or g("feed_project"), "기계": g("feed_machine")}
    try:
        줄, 못 = collect_feed(items, rows, 거르개, dt.datetime.now())
        기계들 = sorted({c["도는 기계"] for r in rows for c in r["카드"]})
        return render_feed(줄, 못, 거르개, [i["name"] for i in items], 기계들)
    except Exception:
        return '<div class="feed"><h2>최근 활동</h2><p class="why">활동 못 읽음</p></div>'


def plugin_mismatch(줄):
    """판을 아는 기계(「기기 모름」 제외)의 판이 둘 이상 다르면 `[(기계, 판)]`, 아니면 `[]`."""
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


def render_machines(data, 시작, 끝, 까닭):
    """첫 화면 기계 현황 조각 — 기간 입력, 판 다름 경고, 기계마다 한 줄(작업량은 `.bar` 막대)."""
    esc = html.escape
    lines = data["줄"]
    form = ('<form method="get" action="/"><input type="date" name="시작" value="%s"> ~ <input type="date" name="끝" value="%s">'
            ' <button type="submit">보기</button></form>' % (시작.date().isoformat(), (끝 - dt.timedelta(days=1)).date().isoformat()))
    why = "".join('<p class="why">%s</p>' % esc(x) for x in (
        ["판 다름 — " + " · ".join("%s %s" % kv for kv in data["판 다름"])] if data["판 다름"] else []) + ([까닭] if 까닭 else []) +
        ["%s — 이력을 못 읽어 기계 현황에서 뺐다" % n for n, _ in data["못 읽음"]])
    most = (max(x["시간"] for x in lines) or 0, max(x["카드 수"] for x in lines) or 0) if lines else (0, 0)

    def bar(v, top):
        return '<span class="bar"><span style="width:%d%%"></span></span>' % (v * 100 // top if top else 0)

    def row(x):
        cards = "<br>".join(esc("%s · %s (%s)" % (p, t, s or "단계 모름")) for p, t, s in x["도는 카드"]) or "도는 카드 없음"
        move = x["마지막 움직임"].strftime("%Y-%m-%d %H:%M") if x["마지막 움직임"] else "움직임 없음"
        return ["%s%s" % (esc(x["기계"]), " · 이 기계" if x["이 기계"] else ""), cards, move, esc(x["판"] or "판 모름"),
                "%s %s" % (bar(x["시간"], most[0]), _시간_글(x["시간"])), "%s %d" % (bar(x["카드 수"], most[1]), x["카드 수"])]
    table = (_표(["기계", "지금 도는 카드", "마지막 움직임", "플러그인 판", "진행 시간", "돈 카드"], [row(x) for x in lines])
             if lines else '<p class="meta">알려진 기계 없음</p>')
    return '<div class="machines"><h2>기계 (%d)</h2>%s%s%s</div>' % (len(lines), form, why, table)


def running_counts(cards):
    """합친 카드 가운데 상태가 진행인 것을 도는 기계별로 센다 — `([(기계, 수)], 전체)`. 기계는 이름순, 「기기 모름」은 끝."""
    n = {}
    for c in cards:
        if c["상태"] == "진행":
            n[c["도는 기계"]] = n.get(c["도는 기계"], 0) + 1
    return sorted(n.items(), key=lambda kv: (kv[0] == "기기 모름", kv[0])), sum(n.values())


def running_text(per, total):
    return " · ".join(["%s %d" % kv for kv in per] + ["전체 %d" % total]) if total else "지금 도는 실행 없음 · 전체 0"


def milestones(cards, scenarios, 완료때, project):
    """시나리오 완료 기준마다 `{프로젝트, 시나리오, 번호, 문장, 때}`. 그 기준을 담은 카드가 모두 완료면 가장 늦은 완료 때, 아니면 None."""
    out = []
    for s in scenarios:
        for row in criteria_rows(cards, s):
            have = [c for c in cards if c["시나리오"] == s["번호"] and row["번호"] in (c.get("완료기준") or c.get("인수조건", []))]
            때 = max((완료때[c["file"]] for c in have if c["file"] in 완료때), default=None) if row["모두 완료"] else None
            out.append({"프로젝트": project, "시나리오": s["번호"], "번호": row["번호"], "문장": row["문장"], "때": 때})
    return out


def read_timeline(items, 시작, 끝, now):
    """보는 목록 모든 프로젝트의 시간 막대 자료. 한 프로젝트의 실패는 `못 읽음` 에 모은다."""
    줄, 작은, 마일, 못, per_all, 선행 = {}, [], [], [], {}, []
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
        마일 += milestones(cards, scenarios, tl["완료 때"], item["name"])
        선행 += 그림
        for m, n in running_counts(cards)[0]:
            per_all[m] = per_all.get(m, 0) + n
    동시 = sorted(per_all.items(), key=lambda kv: (kv[0] == "기기 모름", kv[0]))
    return {"줄": 줄, "작은 수정": sorted(작은), "마일스톤": 마일, "동시": 동시, "전체": sum(per_all.values()), "못 읽음": 못, "선행": 선행}


_시간_CSS = """main { max-width:1200px; }
.tl-body { position:relative; margin:8px 0 16px; }
.tl-track, .tl-common, .tl-milestones { position:relative; border:1px solid var(--line); border-radius:6px; margin:0 0 6px; min-height:26px; }
.tl-bar { position:absolute; height:22px; overflow:hidden; white-space:nowrap; font-size:12px; line-height:22px; padding:0 4px;
  background:var(--line); color:var(--fg); border-radius:4px; box-sizing:border-box; }
.tl-cut { position:absolute; top:0; bottom:0; width:2px; background:var(--fg); }
.tl-today { position:absolute; top:0; bottom:0; width:2px; background:#c0392b; }
.tl-dot { position:absolute; top:8px; width:10px; height:10px; border-radius:5px; background:var(--fg); }
.tl-pending { font-size:13px; color:var(--dim); padding:4px 0; }
.tl-run { font-size:14px; margin:0 0 8px; }
h3.tl-name { font-size:13px; margin:8px 0 2px; color:var(--dim); }"""

_그림_CSS = """.tl-graph { position:relative; display:flex; gap:12px; align-items:flex-start; }
.tl-main { flex:1; min-width:0; }
.tl-aside { width:200px; flex:none; font-size:12px; }
.tl-wait { display:block; border:1px dashed var(--line); border-radius:4px; padding:2px 4px; margin:0 0 4px; }
.tl-arrows { position:absolute; left:0; top:0; width:100%; height:100%; pointer-events:none; overflow:visible; }
.tl-edge { fill:none; stroke:var(--fg); stroke-width:1.5; }
.tl-겹침 { stroke-dasharray:5 4; }
.tl-head { fill:var(--fg); }
.tl-legend { font-size:12px; color:var(--dim); }"""

_화살표_스크립트 = """<script>
(function () {
  function draw() {
    var svg = document.querySelector('.tl-arrows');
    if (!svg) return;
    var box = svg.parentNode.getBoundingClientRect(), NS = 'http://www.w3.org/2000/svg';
    Array.prototype.slice.call(svg.querySelectorAll('.tl-head')).forEach(function (h) { svg.removeChild(h); });
    function find(href, last) {
      var l = Array.prototype.filter.call(document.querySelectorAll('a.tl-bar, a.tl-wait'), function (a) { return a.getAttribute('href') === href; });
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


def render_timeline(data, 시작, 끝, now, 까닭, 고른=""):
    """시간 막대 화면 — 맨 위 동시 실행 수·마일스톤 줄, 기계마다 한 줄, 「작은 수정:」 공통 줄, 오늘 세로선.

    `data["선행"]` 이 있으면 요소 카드 사이 화살표·「아직」 자리·요소 목록을 더하고, `고른`(`프로젝트:SC-nn`)이면 그 시나리오만 보인다."""
    esc = html.escape
    span = (끝 - 시작).total_seconds()
    선행 = data.get("선행") or []
    모름 = ""
    if 선행 and 고른:
        mine = [p for p in 선행 if "%s:%s" % (p["프로젝트"], p["시나리오"]) == 고른]
        if mine:
            선행 = mine
        else:
            모름 = "그런 시나리오 없음 — %s" % 고른
    고른카드 = {(p["프로젝트"], c["file"]) for p in 선행 for e in p["요소"] for c in e["카드"]} if 선행 and 고른 and not 모름 else None

    def pct(t):
        return min(max((t - 시작).total_seconds() / span * 100, 0.0), 100.0)

    def clip(x):
        return x["끝"] >= 시작 and x["시작"] < 끝

    parts = []
    form = ('<form method="get" action="/timeline"><input type="date" name="시작" value="%s"> ~ <input type="date" name="끝" value="%s">'
            ' <button type="submit">보기</button></form>' % ((시작).date().isoformat(), (끝 - dt.timedelta(days=1)).date().isoformat()))
    if 선행:
        opts = '<option value="">모든 시나리오</option>' + "".join(
            '<option value="%s"%s>%s</option>' % (esc(v, quote=True), " selected" if v == 고른 else "", esc(v))
            for v in ("%s:%s" % (p["프로젝트"], p["시나리오"]) for p in data["선행"]))
        form = form.replace(' <button', ' <select name="sc">%s</select> <button' % opts)
    parts.append(form)
    if 모름:
        parts.append("<p>%s</p>" % esc(모름))
    if 까닭:
        parts.append("<p>%s</p>" % esc(까닭))
    parts.append('<p class="tl-run">%s</p>' % esc(running_text(data["동시"], data["전체"])))
    dots = pend = ""
    for m in data["마일스톤"]:
        label = "%s %s" % (m["번호"], m["문장"])
        if m["때"] is None:
            pend += '<span title="%s">%s</span> ' % (esc(m["프로젝트"] + " " + m["시나리오"], quote=True), esc(label))
        elif 시작 <= m["때"] < 끝:
            dots += '<span class="tl-dot tl-ms" style="left:%.2f%%" title="%s"></span>' % (pct(m["때"]), esc("%s %s" % (m["프로젝트"], label), quote=True))
    parts.append('<div class="tl-milestones">%s</div>' % (dots or "&nbsp;"))
    parts.append('<div class="tl-pending">%s</div>' % (("아직: " + pend) if pend else "아직 없음") if data["마일스톤"] else
                 '<div class="tl-pending">완료 기준 없음</div>')
    names = sorted(data["줄"], key=lambda k: (k == "기기 모름", k))
    rows, shown = [], set()
    for name in names:
        bars = sorted((x for x in data["줄"][name] if clip(x) and (고른카드 is None or (x["프로젝트"], x["카드"]) in 고른카드)),
                      key=lambda x: x["시작"])
        if not bars:
            continue
        lanes, out = [], []
        for x in bars:
            lane = next((i for i, last in enumerate(lanes) if last <= x["시작"]), None)
            if lane is None:
                lane = len(lanes)
                lanes.append(x["끝"])
            else:
                lanes[lane] = x["끝"]
            s, e = max(x["시작"], 시작), min(x["끝"], 끝)
            left, width = pct(s), max(pct(e) - pct(s), 0.3)
            marks = "".join('<span class="tl-cut" style="left:%.2f%%" title="%s"></span>'
                            % (((t - s).total_seconds() / max((e - s).total_seconds(), 1)) * 100, esc(kind)) for t, kind in x["끊김"] if s <= t <= e)
            notes = [t for t, on in (("구간 기록 없음", x["기록 없음"]), ("기간 앞에서 시작함", x["시작"] < 시작), ("진행 중", x["진행 중"])) if on]
            if x["이어받기"] or x["재인계"]:
                notes.append("이어받기 %d · 재인계 %d" % (x["이어받기"], x["재인계"]))
            text = "%s · %s%s" % (x["프로젝트"], x["제목"], " [" + "] [".join(notes) + "]" if notes else "")
            href = "/p/%s/%s" % (urllib.parse.quote(x["프로젝트"], safe=""), urllib.parse.quote(x["카드"], safe=""))
            shown.add(href)
            out.append('<a class="tl-bar" href="%s" style="left:%.2f%%;width:%.2f%%;top:%dpx" title="%s">%s%s</a>'
                       % (esc(href, quote=True), left, width, 2 + lane * 24, esc("%s · %s" % (x["프로젝트"], x["제목"]), quote=True), marks, esc(text)))
        rows.append('<h3 class="tl-name">%s</h3><div class="tl-row" data-machine="%s"><div class="tl-track" style="height:%dpx">%s</div></div>'
                    % (esc(name), esc(name, quote=True), 4 + len(lanes) * 24, "".join(out)))
    today = '<div class="tl-today" style="left:%.2f%%"></div>' % pct(now) if 시작 <= now < 끝 else ""
    body = '<div class="tl-body">%s%s</div>' % (today, "".join(rows) or "<p>이 기간에 돈 카드 없음</p>")
    if 선행:
        돈 = {(x["프로젝트"], x["카드"]) for xs in data["줄"].values() for x in xs}
        waits, seen = [], set()
        for p in 선행:
            for e in p["요소"]:
                for c in e["카드"]:
                    if (p["프로젝트"], c["file"]) not in 돈 and (p["프로젝트"], c["file"]) not in seen:
                        seen.add((p["프로젝트"], c["file"]))
                        href = "/p/%s/%s" % (urllib.parse.quote(p["프로젝트"], safe=""), urllib.parse.quote(c["file"], safe=""))
                        shown.add(href)
                        waits.append('<a class="tl-wait" href="%s" title="%s">%s · %s [%s]</a>'
                                     % (esc(href, quote=True), esc(c["title"], quote=True), esc(p["프로젝트"]), esc(c["title"]), esc(c["상태"])))
        edges, drawn = [], set()
        for p in 선행:
            for a in p["화살표"]:
                q = lambda f: "/p/%s/%s" % (urllib.parse.quote(p["프로젝트"], safe=""), urllib.parse.quote(f, safe=""))    # noqa: E731
                key = (q(a["앞"]), q(a["뒤"]), a["종류"])
                if q(a["앞"]) in shown and q(a["뒤"]) in shown and key not in drawn:
                    drawn.add(key)
                    edges.append('<path class="tl-edge tl-%s" data-from="%s" data-to="%s"></path>'
                                 % (a["종류"], esc(key[0], quote=True), esc(key[1], quote=True)))
        body = ('<div class="tl-graph"><div class="tl-main">%s</div><div class="tl-aside"><h3 class="tl-name">아직</h3>%s</div>'
                '<svg class="tl-arrows">%s</svg></div><p class="tl-legend">실선 결과물 선행 · 점선 겹침 선행</p>'
                % (body, "".join(waits) or "아직 없음", "".join(edges)))
    parts.append(body)
    small = "".join('<span class="tl-dot tl-small" style="left:%.2f%%" title="%s"></span>' % (pct(t), esc("%s %s" % (p, s), quote=True))
                    for t, p, s in data["작은 수정"] if 시작 <= t < 끝)
    parts.append('<h3 class="tl-name">작은 수정</h3><div class="tl-common">%s</div>' % (small or "&nbsp;"))
    for name, why in data["못 읽음"]:
        parts.append("<p>%s — 이력을 못 읽어 막대 없이 그렸다: %s</p>" % (esc(name), esc(why)))
    if 선행:
        parts.append('<h3 class="tl-name">요소 목록</h3>' + _요소_목록(선행) + _화살표_스크립트)
    틀 = FILE_TEMPLATE.replace('<a href="board.html">← 보드</a>', '<a href="/">← 첫 화면</a>')
    return (틀 % {"name": "시간 막대", "body": "\n".join(parts)}).replace("</style>", _시간_CSS + ("\n" + _그림_CSS if 선행 else "") + "\n</style>", 1)


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
    return [t for t in texts if t and not t.startswith("{") and t.rstrip(".") != "없음"]


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
            out["원격"] = ("원격을 읽지 못함 — 이 기계의 이력과 신호 폴더만", "원격 끝 커밋이 이 기계에 없음")
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
            elif (카드, m.group(1), _정리(m.group(2))) not in 있는:
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
        out["원격"] = ("원격을 아직 읽지 않음 — 이 기계의 이력과 신호 폴더만", "")
    elif 그림.get("없음"):
        out["원격"] = ("원격 없음", "")
    elif 그림.get("오류"):
        out["원격"] = ("원격을 읽지 못함 — 이 기계의 이력과 신호 폴더만", 그림["오류"])
    else:
        끝 = 그림.get("끝") or ""
        out["원격"] = ("원격 끝 " + 끝, "") if 끝 else ("원격 없음", "")
    try:
        burns = _이력_읽기(item["path"], 끝, out)
        _신호_읽기(item["path"], burns, out)
    except Exception as e:          # noqa: BLE001 — 읽기가 화면을 죽이지 않는다
        out["이력오류"] = out["이력오류"] or "읽지 못함: %s" % (str(e) or type(e).__name__)
    return out


def filter_improvements(항목들, qs):
    """(거른 목록, 까닭). 카드 c·단계 st·종류 k·카드 안 됨 made=0 은 겹쳐 걸고, 기간 from·to 는 측정 추세 항목에만 건다."""
    def g(k):
        return (qs.get(k) or [""])[0].strip()
    if g("k") and g("k") not in IMPROVE_KINDS:
        return [], "종류는 여덟 가운데 하나"
    cond, 까닭 = _조건(qs)
    if 까닭:
        return [], 까닭
    out = []
    for x in 항목들:
        if (g("c") and x["카드"] != g("c")) or (g("st") and x["단계"] != g("st")) or (g("k") and x["종류"] != g("k")):
            continue
        if g("made") == "0" and x["카드됨"]:
            continue
        if x["종류"] == "측정 추세" and ((cond["시작"] and x["때"] < cond["시작"]) or (cond["끝"] and x["때"] > cond["끝"])):
            continue
        out.append(x)
    return out, ""


def group_improvements(항목들):
    """공백을 정리한 원문이 같은 항목끼리 한 줄로. 횟수는 서로 다른 카드 수이고 큰 것부터 나온다."""
    묶음 = {}
    for x in 항목들:
        묶음.setdefault(_정리(x["원문"]), []).append(x)
    rows = [{"원문": k, "횟수": len({x["카드"] for x in v}), "카드들": sorted({x["카드"] for x in v}), "낱낱": v}
            for k, v in 묶음.items()]
    return sorted(rows, key=lambda r: -r["횟수"])


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
        배경.append((c, lines))
    for x in 항목들:
        if not x["단계"]:
            continue
        step = re.compile(r"(?<![0-9A-Za-z])%s(?![0-9A-Za-z])" % re.escape(x["단계"]))
        for c, lines in 배경:
            if any(x["카드"] in l and step.search(l.replace(x["카드"], " ")) for l in lines):
                x["카드됨"] = (c["file"], c["title"], c["상태"])
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
    body = ["<p>%s</p>" % esc(x["원문"]), "<p>종류 %s · 출처 %s · 커밋 %s</p>" % (esc(x["종류"]), esc(x["경로"]), esc(sha))]
    고르기 = ""
    if x["카드됨"]:
        file, title, 상태 = x["카드됨"]
        body.append('<p><a href="/p/%s/%s">카드됨 — %s(%s)</a></p>' % (
            urllib.parse.quote(x.get("카드됨곳") or 프로젝트, safe=""), urllib.parse.quote(file, safe=""), esc(title), esc(상태)))
    elif not x["단계"]:
        고르기 = "<p>단계가 없어 카드로 이을 수 없음</p>"
    else:
        고르기 = '<label><input type="checkbox" name="pick" value="%s"> 고르기</label>' % 항목_열쇠(x)
    return 고르기 + '<details class="항목"><summary>%s · 카드 %s · 단계 %s · 때 %s · 경로 %s</summary>%s</details>' % (
        esc(첫), esc(x["카드"]), esc(x["단계"]), esc(x["때"]), esc(x["경로"]), "".join(body))


def render_improve(item, 결과, qs, 까닭, 쓰기=None):
    """개선 거리 화면 HTML. `결과` 는 `read_improvements` 의 사전에 거른 「항목」·「전체수」·「작업목록오류」를 얹은 것. 값은 모두 escape 한다."""
    esc = html.escape
    name = item["name"]

    def v(k):
        return (qs.get(k) or [""])[0]
    kinds = "".join('<option value="%s"%s>%s</option>' % (esc(k, quote=True), " selected" if v("k") == k else "", esc(k))
                    for k in ("",) + IMPROVE_KINDS)
    쓰기 = 쓰기 or {}
    parts = ["<p>%s</p>" % esc(c) for c in ([쓰기["결과줄"]] if 쓰기.get("결과줄") else []) + 쓰기.get("까닭들", [])]
    parts += ['<form method="get" action="/p/%s/improve"><p><label>카드 <input type="text" name="c" value="%s"></label> '
             '<label>단계 <input type="text" name="st" value="%s"></label> <label>종류 <select name="k">%s</select></label> '
             '<label><input type="checkbox" name="made" value="0"%s> 아직 카드 안 됨만</label> '
             '<label>기간 <input type="date" name="from" value="%s"> ~ <input type="date" name="to" value="%s"></label> '
             '<button type="submit">보기</button></p></form>'
             % (urllib.parse.quote(name, safe=""), esc(v("c"), quote=True), esc(v("st"), quote=True), kinds,
                " checked" if v("made") == "0" else "", esc(v("from"), quote=True), esc(v("to"), quote=True)),
             "<p>공백을 정리한 글이 같으면 한 줄로 묶음</p>"]
    읽음 = ["이력을 읽지 못함" if 결과["이력오류"] else "이력 소각 커밋 %d건" % 결과["소각수"]]
    읽음 += ["신호 폴더 %s %s" % (p, "있음" if ok else "없음") for p, ok in 결과["신호"]]
    상태글, 원격까닭 = 결과["원격"]
    읽음.append(상태글 + (" (%s)" % 원격까닭 if 원격까닭 else ""))
    parts.append("<p>읽은 것 — %s</p>" % esc(" · ".join(읽음)))
    if 결과["이력오류"] and 결과["이력오류"] != "이력을 읽지 못함":
        parts.append("<p>%s</p>" % esc(결과["이력오류"]))
    if 결과.get("작업목록오류"):
        parts.append("<p>%s</p>" % esc(결과["작업목록오류"]))
    if 결과["못읽음"] or 결과["기록못찾음"]:
        parts.append("<h3>읽지 못한 산출물</h3><ul>%s%s</ul>" % (
            "".join("<li>%s (커밋 %s) — %s</li>" % (esc(p), esc(c[:7] or "없음"), esc(w)) for p, c, w in 결과["못읽음"]),
            "".join("<li>기록 못 찾음 — 카드 %s (커밋 %s)</li>" % (esc(c), esc(s[:7])) for c, s in 결과["기록못찾음"])))
    항목 = 결과["항목"]
    골라 = "".join('<option value="%s">%s</option>' % (esc(n, quote=True), esc(n)) for n in 쓰기.get("프로젝트들") or [name])
    parts.append('<form method="post" action="/p/%s/make-card" id="고르기"><p><label>제목 <input type="text" name="title"></label> '
                 '<label>짧은 이름 <input type="text" name="slug"></label> '
                 '<label>둘 프로젝트 <select name="target"><option value="">처음 값(항목이 나온 프로젝트)</option>%s</select></label> '
                 '<button type="submit">카드로 만들기</button></p>' % (urllib.parse.quote(name, safe=""), 골라))
    if 까닭:
        parts.append("<p>%s</p>" % esc(까닭))
    elif not 항목:
        parts.append("<p>%s</p>" % ("개선 거리 없음" if not 결과.get("전체수") else "거르개에 맞는 개선 거리 없음"))
    else:
        for kind in IMPROVE_KINDS:
            mine = [x for x in 항목 if x["종류"] == kind]
            parts.append("<h2>%s (%d)</h2>" % (esc(kind), len(mine)))
            if kind == "측정 추세":
                줄 = trend_lines(항목, name, qs)
                for t in 줄:
                    parts.append('<p><a href="%s">단계 %s · 실행 %d · 평균 시간 %s · 비용 $%.2f · 토큰 %s · 첫 %s $%.2f 토큰 %s · 마지막 %s $%.2f 토큰 %s</a></p>' % (
                        esc(t["고리"]), esc(t["단계"]), t["수"], _분초(t["평균ms"]), t["평균cost"], "{:,.0f}".format(t["평균토큰"]),
                        _분초(t["첫"][0]), t["첫"][1], "{:,.0f}".format(t["첫"][2]),
                        _분초(t["끝"][0]), t["끝"][1], "{:,.0f}".format(t["끝"][2])))
                if not 줄:
                    parts.append("<p>측정 추세 없음</p>")
            if not mine:
                parts.append("<p>없음</p>")
            for g in group_improvements(mine):
                첫 = g["원문"] if len(g["원문"]) <= 60 else g["원문"][:60] + "…"
                parts.append('<details class="묶음"><summary>횟수 %d · %s · 카드 %s</summary>%s</details>' % (
                    g["횟수"], esc(첫), esc(", ".join(g["카드들"])), "".join(_항목_html(x, name) for x in g["낱낱"])))
    parts.append("</form>")
    틀 = FILE_TEMPLATE
    return 틀 % {"name": "개선 거리 — " + esc(name), "body": "\n".join(parts)}


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
    결과 = read_improvements(item, REMOTE.get(os.path.abspath(item["path"])))
    오류 = "작업목록을 읽지 못함 — 카드됨을 잇지 못했다" if _카드됨_잇기(item, 결과["항목"], items) else ""
    return 결과, 오류


def improve_page(item, qs, items=None, 쓰기=None):
    """`/p/<이름>/improve` 의 화면 한 장. 읽고 → 카드됨 잇고 → 거르고 → 그린다. `쓰기` 는 만든 뒤의 결과 줄·까닭 줄."""
    items = items or [item]
    결과, 오류 = _개선_읽기(item, items)
    전체 = len(결과["항목"])
    항목, 까닭 = filter_improvements(결과["항목"], qs)
    return render_improve(item, dict(결과, 항목=항목, 전체수=전체, 작업목록오류=오류), qs, 까닭,
                          dict(쓰기 or {}, 프로젝트들=[p["name"] for p in items]))


# ── 보드가 파일을 쓰는 단 하나의 길 — 개선 거리를 카드로 (ISSUE-sc02-e18-make-card-2026-11-20) ──
쓰기_상한 = 64 * 1024


def 쓰기_허락(보낸쪽, 머리, 자리):
    """쓰기 요청(POST)을 받아도 되는가 — (받음, 까닭). 보낸 쪽이 이 기계이고 `Host`·`Origin` 이 이 보드 자리이며
    `Sec-Fetch-Site` 가 있으면 `same-origin` 일 때만. 보드는 127.0.0.1 에만 묶이므로 보낸 쪽 검사는 다른 기계를,
    `Origin`·`Host` 검사는 이 기계 브라우저에서 연 다른 사이트 화면(과 DNS 재결합)을 막는다."""
    글 = "이 기계의 보드 화면에서 보낸 요청만 받는다"
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


def 카드_초안(제목, 항목들, 오늘):
    """카드 틀(`docs/backlog/_템플릿.md`)을 따르는 아이디어 카드 글. 크기·사람이 하면은 비운다. 원문은 두 칸 들여 「## 배경」 안에."""
    배경 = []
    for x in 항목들:
        배경.append("- 카드 %s · 단계 %s · 종류 %s · 출처 `%s` · 커밋 %s" % (
            x["카드"], x["단계"], x["종류"], x["경로"], x["커밋"][:7] if x["커밋"] else "없음(신호 폴더)"))
        배경 += ["  " + l for l in x["원문"].splitlines()]
    return ("---\n상태: 아이디어\n크기:\n갱신: %s\n시나리오:\n요소:\n완료기준:\n선행:\n되돌리기:\n기능:\n---\n# %s\n\n"
            "## 배경\n\n%s\n\n## 사람이 하면\n\n## 원하는 것 — 최소한\n\n## 시작점 — 실재를 확인한 경로\n\n"
            "## 범위 밖\n\n## 완료 조건\n") % (오늘, " ".join(제목.split()), "\n".join(배경))


def 카드_입력_검사(item, 항목들, form, items, 오늘):
    """(까닭 목록, 정리된 값). 까닭이 하나라도 있으면 아무것도 쓰지 않는다. `form` 은 `parse_qs` 모양."""
    def g(k):
        return (form.get(k) or [""])[0]
    까닭, 고른 = [], []
    by = {항목_열쇠(x): x for x in 항목들}
    if not form.get("pick"):
        까닭.append("고른 항목이 없다")
    for k in form.get("pick") or []:
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
        까닭.append("짧은 이름은 영문 소문자·숫자·붙임표만")
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


def 카드_올리기(project, 파일이름, 글="", 승격=False):
    """새 카드 파일 하나만 원격 기준 가지 끝 위에 커밋해 올린다. {"결과": 올림 | 올리지 못함 | 원격 없음, "까닭", "커밋"}.
    `승격=True` 면 새 파일이 아니라 원격 끝의 그 카드(아이디어일 때만)에서 `상태:` 줄만 대기로 바꿔 올린다. 결과에 「이미 아이디어 아님」(까닭=원격 끝의 상태)·
    「원격에 없음」 이 더 있다. 예외를 내지 않고, 이 기계의 카드 파일은 건드리지 않는다. 밀리면 다시 받아 최대 3번(원격 이력은 다시 쓰지 않는다)."""
    def 끝(결과, 까닭="", 커밋=""):
        return {"결과": 결과, "까닭": 까닭, "커밋": 커밋}
    try:
        cfg = _프로젝트_설정(project)
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
        제목 = "작업목록: %s 대기로 올림 — 보드의 대기로 올리기" if 승격 else "작업목록: %s 새 카드 — 보드의 카드로 만들기"
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
            r = _원격_git(project, "ls-tree", "--name-only", tip, "--", rel)
            if r.returncode:
                return 끝("올리지 못함", _끝줄(r))
            내용 = None
            if 승격:
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
        줄 = "카드를 만들었다 — %s/%s" % (대상["name"], 파일)
        if r["결과"] == "올림":
            줄 += " · 원격 기준 가지에 올림(커밋 %s)" % r["커밋"][:7]
        elif r["결과"] == "원격 없음":
            줄 += " · 원격 없음 — 커밋하지 않았다"
        else:
            줄 += " · 올리지 못함 — %s(카드는 이 기계에 있다)" % r["까닭"]
        서버._그리기(improve_page(item, {}, items, {"결과줄": 줄}))
    except Exception as e:                              # noqa: BLE001
        서버.send_error(500, "Internal Error", "카드를 만들다 실패 — %s" % (str(e) or type(e).__name__))


def _승격_처리(서버, item, items, form):
    """`POST /p/<이름>/promote` — 확인 값·카드 이름 검사 → 이 기계 카드가 시나리오 없는 아이디어인지 → 상태 줄만 대기로 → 원격 끝 위에 그 파일 하나만 올리기 → 결과 줄."""
    def g(k):
        return (form.get(k) or [""])[0]

    def 보이기(줄, 상태=200):
        본문 = '<p>%s</p><p><a href="/p/%s/board.html">보드로</a></p>' % (html.escape(줄), urllib.parse.quote(item["name"], safe=""))
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
            보이기("카드 파일이 이 기계에 없다 — %s" % 파일)
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
        if r["결과"] in ("이미 아이디어 아님", "원격에 없음"):
            try:
                with open(경로, "w", encoding="utf-8", newline="") as f:
                    f.write(옛)
            except OSError:
                pass
            줄 = ("올리지 않았다 — 원격 끝에서는 이미 %s(이 기계 파일은 그대로 아이디어)" % r["까닭"] if r["결과"] == "이미 아이디어 아님"
                  else "올리지 않았다 — %s(이 기계 파일은 그대로 아이디어)" % r["까닭"])
        else:
            줄 = "%s 상태 대기" % 파일
            if r["결과"] == "올림":
                줄 += " · 원격 기준 가지에 올림(커밋 %s)" % r["커밋"][:7]
            elif r["결과"] == "원격 없음":
                줄 += " · 원격 없음 — 커밋하지 않았다"
            else:
                줄 += " · 올리지 못함 — %s(이 기계 파일은 대기로 바뀌어 있다)" % r["까닭"]
        보이기(줄)
    except Exception as e:                              # noqa: BLE001
        서버.send_error(500, "Internal Error", "대기로 올리다 실패 — %s" % (str(e) or type(e).__name__))


쓰기_길 = {"make-card": _카드만들기_처리, "promote": _승격_처리}      # 보드가 파일을 바꾸는 길의 유일한 목록


class 보드서버(작업파일서버):
    """`/` 첫 화면, `/p/<이름>/…` 그 프로젝트 보드와 카드 화면. 요청마다 그 자리에서 그린다. 파일을 쓰는 길은 「카드로 만들기」
    (`POST /p/<이름>/make-card`)와 「대기로 올리기」(`POST /p/<이름>/promote`) 둘이고, 이 기계(127.0.0.1)의 보드 화면이 보낸 요청만 받는다."""
    extra = None

    def end_headers(self):
        self.send_header(BOARD_HEADER, "1")
        super().end_headers()

    def _그리기(self, page, status=200):
        data = page.encode("utf-8")
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
        쓰기_길[urllib.parse.unquote(m.group(2))](self, item, items, urllib.parse.parse_qs(body, keep_blank_values=True))

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        items, err = _보는목록(self.extra)
        if path == "/search":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            결과, 까닭 = search(items, qs)
            if (qs.get("as") or [""])[0] == "json":
                data = search_json(items, 결과, 까닭).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._그리기(render_search(qs, 결과, 까닭))
            return
        if path == "/signals":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            결과, 까닭 = read_signals(items, qs)
            self._그리기(render_signals(qs, 결과, 까닭))
            return
        if path == "/timeline":
            시작, 끝, 까닭 = timeline_period(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query), dt.date.today())
            now = dt.datetime.now()
            고른 = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("sc") or [""])[0].strip()
            self._그리기(render_timeline(read_timeline(items, 시작, 끝, now), 시작, 끝, now, 까닭, 고른))
            return
        if path == "/":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            tab = (qs.get("tab") or [""])[0]
            rows = [project_row(p) for p in items]
            시작, 끝, 까닭 = timeline_period(qs, dt.date.today())
            machines = render_machines(read_machines(items, rows, 시작, 끝, dt.datetime.now()), 시작, 끝, 까닭)
            machines += _피드_구역(items, rows, qs)
            self._그리기(render_home(rows, all_turns(items), err, tab, read_resolved(items) if tab == "done" else (), machines=machines))
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
            page = render_html(cards, item["name"], 10, scenarios, _알림결과(item["path"], turns),
                               os.path.join(item["path"], "docs", "plan"),
                               경보_알림결과(item["path"], stalled_alarms(item, cards, REMOTE.get(os.path.abspath(item["path"])))),
                               단추=True)
            feed = _피드_구역([item], [{"까닭": "", "카드": cards}], urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query), item["name"])
            page = page.replace("</body>", feed + "</body>", 1)
            self._그리기(page.replace("</h1>", '</h1><p><a href="/p/%s/improve">개선 거리</a></p>' % urllib.parse.quote(item["name"], safe=""), 1))
            return
        if sub == "improve":
            self._그리기(improve_page(item, urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query), items))
            return
        card_file = urllib.parse.unquote(sub)
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
            self._그리기(render_card_detail(item, card, cards, scenarios, read_card_history(item["path"], card_file), fm))
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
        httpd = socketserver.TCPServer(("127.0.0.1", port), handler, bind_and_activate=False)
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


def 상주_켜기():
    """로그인 항목 파일 하나를 쓰고 등록한다 — 지금 이 board.py 를 가리킨다. 다시 켜면 경로를 덮어쓴다."""
    path = os.path.join(LAUNCH_DIR, LAUNCH_LABEL + ".plist")
    os.makedirs(LAUNCH_DIR, exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump({"Label": LAUNCH_LABEL, "ProgramArguments": [sys.executable, os.path.abspath(__file__), "--상주"],
                       "RunAtLoad": True, "KeepAlive": True}, f)
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
