#!/usr/bin/env python3
"""작업 보드 — `docs/backlog/*.md` 의 머리말을 읽어 칸반으로 보여준다 (읽기 전용).

  python3 bin/board.py [프로젝트 경로]          터미널에 칸을 찍고 board.html 을 쓴다
  python3 bin/board.py [프로젝트 경로] --따라가기  10초마다 다시 쓰고 주소로 띄운다 (Ctrl-C 로 끝)

따라가기의 포트는 프로젝트 경로에서 정한다 — 같은 프로젝트는 늘 같은 주소이고,
프로젝트가 달라지면 포트도 달라져 서로 부딪치지 않는다. 쓰이고 있으면 다음 번호를
차례로 본다. 127.0.0.1 로 묶으므로 이 기계 밖에서는 열리지 않는다.

파일이 진실이다. 이 도구는 고치지 않는다 — 상태를 바꾸려면 그 파일의 `상태:` 줄을
고친다(승격은 사람만 한다, `workflows/01-loop-design.md`). 러너는 실행 중에 같은
줄을 되쓴다.

머리말은 파일 맨 앞의 `---` 두 줄 사이에 있고, `열쇠: 값` 형식이다. YAML 라이브러리를
쓰지 않는 이유는 표준 라이브러리만으로 돌리기 위해서다(`bin/ledger.py` 와 같은 규율).
"""
import datetime as dt
import glob
import html
import functools
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import zlib

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
            key, sep, value = line.partition(":")
            if sep:
                # 값 뒤 빈칸 둘 이상에 이어 나오는 `#` 부터는 설명이다 (runner.py 의 _head_value 와 같은 규칙)
                value = re.sub(r"\s{2,}#.*$", "", value)
                out[key.strip()] = value.strip().strip('"').strip("'")
    return out


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


def read_cards(backlog):
    cards = []
    for path in sorted(glob.glob(os.path.join(backlog, "*.md"))):
        if os.path.basename(path).startswith("_"):
            continue            # `_템플릿.md` 처럼 밑줄로 시작하면 작업이 아니다
        fm = front_matter(path)
        cards.append({"file": os.path.basename(path), "title": title(path),
                      "상태": fm.get("상태", "대기"), "크기": fm.get("크기", ""),
                      "갱신": fm.get("갱신", ""), "멈춘이유": fm.get("멈춘이유", ""),
                      "단계": fm.get("단계", ""), "시작": fm.get("시작", ""), "차례": fm.get("차례", "").strip(),
                      "누적": fm.get("누적", ""),
                      "시나리오": fm.get("시나리오", "").strip(), "요소": list_value(fm.get("요소", "")),
                      "인수조건": list_value(fm.get("인수조건", "")),
                      "완료기준": list_value(fm.get("완료기준", "")), "선행": list_value(fm.get("선행", "")),
                      "마지막줄": last_live_line(backlog, os.path.basename(path))
                      if fm.get("상태", "대기") == "진행" else ""})
    # 완료만 갱신 내림차순으로 다시 줄 세운다. 다른 칸은 파일 이름 순 그대로 (이슈 ISSUE-board-done-order)
    return [c for c in cards if c["상태"] != "완료"] + sorted(
        (c for c in cards if c["상태"] == "완료"), key=_done_key)


_시나리오이름 = re.compile(r"^(SC-\d{2})-.*\.md$")
_시나리오상태 = ("초안", "확정", "구현 중", "완료")


def read_scenarios(plan_dir):
    """`docs/plan/SC-nn-*.md` 마다 시나리오 하나. 폴더가 없으면 빈 목록. 못 읽으면 번호와 오류만 갖는다."""
    out = []
    for path in sorted(glob.glob(os.path.join(plan_dir, "SC-*.md"))):
        m = _시나리오이름.match(os.path.basename(path))
        if not m:
            continue
        s = {"번호": m.group(1), "이름": "", "상태": "상태 모름", "요소": set(), "카드아님": set(), "오류": "", "남은질문": [],
             "file": os.path.basename(path)}
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (OSError, UnicodeDecodeError):
            s["오류"] = "시나리오 파일을 읽지 못했다"
            out.append(s)
            continue
        head = next((l[2:].strip() for l in lines if l.startswith("# ")), "")
        s["이름"] = head[len(s["번호"]):].strip() if head.startswith(s["번호"] + " ") else head
        in_elements = ask = False
        for i, l in enumerate(lines):
            if l.startswith("## "):
                in_elements = l[3:].strip() == "요소"
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
            elif in_elements and cells and re.fullmatch(r"E-\d+", cells[0]):
                s["요소"].add(cells[0])
                if any("(카드 아님)" in x for x in cells[1:]):
                    s["카드아님"].add(cells[0])
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


def human_turns(cards, scenarios, project):
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
                _생긴때(project, c), c["file"], c["file"])
        elif c["상태"] == "검토":
            add("사람이 병합할 PR", c["시나리오"], c["title"], "PR 을 보고 병합한다",
                _생긴때(project, c), c["file"], c["file"])
    for sc in scenarios:
        mine = [c for c in cards if c["시나리오"] == sc["번호"]]
        if (sc["상태"] in ("확정", "구현 중") and mine and all(c["상태"] == "완료" for c in mine)
                and end_report_cards(os.path.join(project, "docs", "plan"), sc["번호"]) == {c["file"] for c in mine}):
            때 = max((c["갱신"] for c in mine if c["갱신"]), default=_모름)
            add("완료 선언", sc["번호"], ("%s %s" % (sc["번호"], sc["이름"])).strip(),
                "끝 보고를 보고 완료를 선언한다(시나리오 상태를 완료로)", 때, "../plan/" + END_REPORT % sc["번호"], sc["번호"])
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


def sync_turns(project, turns):
    """직전 저장 목록과 비교한다. 새 열쇠는 알리고, 사라진 열쇠는 기록한다. 지금 목록에 알림 결과를 붙여 돌려준다."""
    path = os.path.join(HUMAN_DIR, os.path.basename(os.path.abspath(project)) + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
        if not isinstance(old, dict):
            old = {}
    except (OSError, ValueError):
        old = {}
    keep = {}
    for k, t in old.items():
        if k in {x["열쇠"] for x in turns}:
            continue
        try:
            if t.get("시나리오"):
                record_resolved(project, t)
        except OSError as e:
            print("사람 손 기록 실패 (다음 바퀴에 다시): %s" % e)
            keep[k] = t
    now = {}
    for t in turns:
        if t["열쇠"] in old:
            t["알림"] = old[t["열쇠"]].get("알림")
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


def render_html(cards, project, every=0, scenarios=(), turns=()):
    marks = card_marks(cards, scenarios)

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
        live = ('<p class="live">%s</p>' % html.escape(c["마지막줄"])) if c["마지막줄"] else ""
        return ('<a class="card" href="%s"><h3>%s</h3>%s%s%s%s</a>'
                % (html.escape(c["file"]), html.escape(c["title"]), strip,
                   '<p class="meta">%s</p>' % meta if meta else "", why, live))

    cols = "".join(
        '<section><h2>%s <span>%d</span></h2>%s</section>'
        % (col, len([c for c in cards if c["상태"] == col]),
           "".join(card(c) for c in cards if c["상태"] == col))
        for col in COLUMNS)
    plans = "".join(
        '<section><h2>%s</h2>%s</section>' % (html.escape(head), "".join(card(c, "marks") for c in mine))
        for head, mine in scenario_groups(cards, scenarios))
    def turn(t):
        why = '<p class="why">알림 못 띄움</p>' if t.get("알림") is False else ""
        return ('<a class="card" href="%s"><h3>%s</h3><p class="meta">%s · %s · %s · %s</p>%s</a>'
                % (html.escape(t["열어 볼 곳"], quote=True), html.escape(t["제목"]), html.escape(t["프로젝트"]),
                   html.escape(t["종류"]), html.escape(t["기다리는 것"]), html.escape(t["생긴 때"]), why))

    box = ('<h2>내가 할 일 (%d)</h2>%s' % (len(turns), "".join(turn(t) for t in turns))
           if turns else '<h2>내가 할 일 없음</h2>')
    return TEMPLATE % {"project": html.escape(project), "cols": cols, "plans": plans,
                       "turns": '<div class="turns">%s</div>\n' % box,
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
.turns { max-width:1200px; margin:0 auto 12px; padding:10px; border:1px solid var(--line); border-radius:10px; }
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
</style></head><body>
<header><h1>작업 보드 — %(project)s</h1>
<p>파일이 진실이다. 상태를 바꾸려면 그 파일의 「상태:」 줄을 고친다. 만든 시각 %(now)s</p></header>
%(turns)s<main>%(cols)s</main>
<div class="plans">%(plans)s</div>
</body></html>
"""


_표_구분줄 = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
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


PORT_BASE = 8700          # 8700~8799 — 프로젝트 경로로 고른다


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


def serve(backlog, project):
    """작업목록 폴더를 127.0.0.1 로 연다. (주소, 서버) 또는 (None, None)."""
    handler = functools.partial(작업파일서버, directory=backlog)
    first = PORT_BASE + zlib.crc32(project.encode("utf-8")) % 100
    for port in range(first, first + 10):
        try:
            httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
        except OSError:
            continue        # 그 번호는 누가 쓰고 있다 — 다음을 본다
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d/board.html" % port, httpd
    return None, None


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
    turns = human_turns(cards, scenarios, project)
    # 알림·저장·기록은 따라가기에서만 — 한 번 찍기는 보이기만 한다. 마지막 한 번(every=0)도 건드리지 않는다.
    turns = sync_turns(project, turns) if every else _알림결과(project, turns)
    out = os.path.join(backlog, "board.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_html(cards, os.path.basename(project), every, scenarios, turns))
    if turns_out is not None:
        turns_out[:] = turns
    return cards, out


def main(argv):
    argv = list(argv)
    every = 10 if "--따라가기" in argv else 0
    argv = [x for x in argv if not x.startswith("--")]
    project = os.path.abspath(os.path.expanduser(argv[0] if argv else "."))
    backlog = os.path.join(project, "docs", "backlog")
    if not os.path.isdir(backlog):
        print("작업목록 폴더가 없다: %s" % backlog)
        return 1

    turns = []
    cards, out = write_once(backlog, project, every, turns)
    print(render_turns_text(turns))
    print(render_text(cards, read_scenarios(os.path.join(project, "docs", "plan"))))
    print("보드: %s" % out)
    if not every:
        return 0

    # 따라가기 — 파일이 진실이므로 여기서는 다시 읽어 쓰기만 한다.
    url, httpd = serve(backlog, project)
    # flush — 로그 파일로 돌릴 때도 주소가 바로 보여야 한다.
    print(url or "주소를 열지 못했다 — 파일로 본다: %s" % out, flush=True)
    print("%d초마다 다시 쓴다. 브라우저도 그만큼마다 새로고침한다 (Ctrl-C 로 끝)" % every,
          flush=True)
    try:
        while True:
            time.sleep(every)
            write_once(backlog, project, every)
    except KeyboardInterrupt:
        write_once(backlog, project, 0)      # 끝낼 때는 새로고침 태그를 뗀다
        if httpd:
            httpd.shutdown()
        print("\n따라가기 끝")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
