#!/usr/bin/env python3
"""배차 — 한 번 띄우면 빌 때까지 도는 대기열 집기 (SC-01 E-3).

프로젝트 목록(hq 는 `projects.yaml` 의 `workflow: stop-loop`, 플러그인은 `~/.stop-loop/projects.json`)의 프로젝트마다 `docs/backlog/*.md` 카드를 읽어,
`상태: 대기` 카드를 러너(`bin/runner.py`)에 한 장씩 넘긴다. 동시 상한은 기계 전체에 하나(기본 CAP=4,
`~/.stop-loop/projects.json` 맨 위 `"maxParallel"` 로 바꾼다). 같은 프로젝트에서는 선행이 끝난 카드가 함께 돌되,
카드 본문 「## 시작점」의 저장소 경로가 돌고 있는 카드와 하나라도 같으면 그 카드가 끝날 때까지 기다린다.
더 돌 것이 없으면 한 줄 요약을 찍고 끝난다.

    python3 bin/dispatch.py

`--상주` 는 끝나지 않는다 — 60초마다 한 바퀴씩 원격을 받고, 병합된 카드를 정리하고, 대기 카드를 집고, 살아 있음 파일을 쓴다.
`--상주-켜기`·`--상주-끄기` 는 로그인 자동 실행을 등록·해제한다. 멈춤 파일 `~/.stop-loop/dispatch.stop` 이 있으면 새 카드를 집지 않는다.

    python3 bin/dispatch.py --상주
"""
import datetime as dt
import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backlog  # noqa: E402
import board  # noqa: E402
import endreport  # noqa: E402
import runner  # noqa: E402

PROJECTS_YAML = os.path.join(os.path.dirname(HERE), "projects.yaml")
PROJECTS_JSON = os.path.expanduser("~/.stop-loop/projects.json")   # 플러그인 안의 목록. setup_project.py 와 같아야 한다 — 시험이 지킨다
RUNNER = os.path.join(HERE, "runner.py")
CAP = 4          # 이 기계의 동시 상한 기본값. 바꾸려면 projects.json 의 maxParallel(max_parallel)
REHANDOFF_CAP = 3  # 기준 가지 탓으로 멈춘 카드를 카드당 다시 인계하는 횟수. CAP 과 독립이다
POLL = 1.0       # 실행이 끝났는지 보는 간격(초)
RESIDENT_EVERY = 60      # 상주 배차의 바퀴 간격(초) — 시험이 바꿔 끼운다
AUTO_RESUME_WAIT = 600   # 일시적 실패 카드를 자동으로 이어받기 전에 기다리는 초 — 시험이 바꿔 끼운다
AUTO_MARK = "자동-이어받음.json"   # 실행 폴더 안 표지 — 그 실행이 자동 이어받기를 한 번 받았다는 유일한 근거
STOP_FILE = os.path.expanduser("~/.stop-loop/dispatch.stop")             # 있으면 상주 배차는 새 카드를 집지 않는다(내용은 읽지 않는다)
STATUS_FILE = os.path.expanduser("~/.stop-loop/status/dispatch.json")    # 상주 배차의 살아 있음 파일 — 보드가 읽는다
PLUGIN_JSON = os.path.join(runner.HQ, ".claude-plugin", "plugin.json")   # 자기 코드가 든 플러그인 폴더의 판 파일
CONFIG_DIR = "~/.claude"                                                 # 설치 기록을 읽는 설정 폴더
LAUNCH_LABEL = "dawn-island.stop-loop.dispatch"
WAIT_PR = "앞 카드 PR 병합 기다림"

gh_token = runner.gh_token      # 시험이 바꿔 끼운다


def list_path():
    """플러그인 안(`runner.PLUGIN`)이면 목록 파일, hq 면 projects.yaml. 호출 때 정한다 — 시험이 바꿔 끼운다."""
    return PROJECTS_JSON if runner.PLUGIN else PROJECTS_YAML


def load_projects(path=None, devices=None):
    """(이름, 펼친 경로) 를 적힌 순서대로. `.json` 이면 목록 파일의 전부, 아니면 projects.yaml 의
    `workflow` 가 정확히 stop-loop 인 것. `yaml` 은 YAML 을 읽을 때만 가져온다 — 플러그인은 표준 라이브러리만 쓴다.
    devices 가 사전이면 이름 → 항목의 `기기` 값(문자열이 아니면 빈 값)을 채운다 — 반환 모양은 그대로다.
    읽기 실패는 OSError, 내용 오류는 ValueError, 모양 오류는 KeyError·TypeError."""
    path = path or list_path()
    with open(path, encoding="utf-8") as f:
        if path.endswith(".json"):
            items = json.load(f)["projects"]
        else:
            import yaml
            try:
                data = yaml.safe_load(f)
            except yaml.YAMLError as e:
                raise ValueError(e)
            items = [p for p in data["projects"] if p.get("workflow") == "stop-loop"]
    if devices is not None:
        for p in items:
            v = p.get("기기", "")
            devices[p["name"]] = v.strip() if isinstance(v, str) else ""
    return [(p["name"], os.path.expanduser(p["path"])) for p in items]


def this_machine():
    """이 기계의 이름 — `PROJECTS_JSON` 맨 위 `"이 기계"`(비지 않은 글), 없거나 잘못이면 빈 값. 알리지 않는다."""
    try:
        with open(PROJECTS_JSON, encoding="utf-8") as f:
            v = json.load(f).get("이 기계")
    except (OSError, ValueError, AttributeError):
        return ""
    return v.strip() if isinstance(v, str) else ""


def is_my_card(run):
    """카드 `돈기계` 가 이 기기의 카드인가 — 이 기기 이름(`"이 기계"`)이거나 이 기기 호스트 이름과 같을 때. 빈 값은 아니다(받을 기기를 모른다)."""
    run = (run or "").strip()
    return bool(run) and (run == this_machine() or run == socket.gethostname())


def _mono():
    """기다림을 재는 시계(초). 시험이 바꿔 끼운다."""
    return time.monotonic()


def auto_resume_candidate(head):
    """보류이고 `멈춘종류` 가 일시적 실패이고 돈 기기가 이 기기인가. 머리말만 본다(`요청결과` 는 보지 않는다). 입력을 바꾸지 않는다."""
    return (head.get("상태") == "보류" and head.get("멈춘종류") == "일시적 실패"
            and is_my_card(head.get("돈기계")))


def has_auto_mark(wd):
    """이 실행 폴더가 자동 이어받기를 이미 한 번 받았는가."""
    return os.path.exists(os.path.join(wd, AUTO_MARK))


def write_auto_mark(wd):
    """실행 폴더에 표지를 쓴다. 썼으면 True, 못 쓰면 False."""
    row = {"카드": os.path.basename(wd.rstrip("/")), "때": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        with open(os.path.join(wd, AUTO_MARK), "w", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        return False
    return True


HUMAN_ANSWERS = "사람의 답"


def append_human_answers(wd, number, answers):
    """실행 폴더 `07-answers.md` 「## 사람의 답」 절 끝에 정할 것마다 답 한 줄을 덧붙인다 — 절이나 파일이 없으면 파일 끝에 절을 만든다.
    「## 답」 표와 「## 사람 결정」 절은 건드리지 않는다. 같은 요청 번호·정할 것의 줄이 이미 있으면 덧붙이지 않는다. 못 쓰면 OSError."""
    path = os.path.join(wd, "07-answers.md")
    try:
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    sec = re.search(r"^## %s[ \t]*$(.*?)(?=^#{1,2} |\Z)" % HUMAN_ANSWERS, text, re.S | re.M)
    lines = []
    for a in answers:
        head = "- [사람 답 %s] %s — " % (number, a["정할 것"])
        if sec and head in sec.group(1):
            continue
        lines.append("%s고른 것: %s — 덧붙인 말: %s\n" % (head, a["고른 것"], a.get("덧붙인 말") or "없음"))
    if not lines:
        return
    if sec:
        end = sec.end(1)
        body = text[:end].rstrip("\n") + "\n" + "".join(lines) + ("\n" if text[end:] else "")
        text = body + text[end:].lstrip("\n") if text[end:].strip() else body
    else:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + "## %s\n\n" % HUMAN_ANSWERS + "".join(lines)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def max_parallel():
    """이 기계의 동시 상한 — `PROJECTS_JSON` 맨 위 `maxParallel`(1 이상 정수), 없으면 CAP.
    잘못된 값·깨진 파일이면 한 줄 알리고 CAP. 파일이 없으면 조용히 CAP."""
    try:
        with open(PROJECTS_JSON, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("맨 위가 JSON 객체가 아니다")
    except FileNotFoundError:
        return CAP
    except (OSError, ValueError) as e:
        print("maxParallel 을 읽지 못해 %d 로 돈다 — %s 를 고친다 (%s)" % (CAP, PROJECTS_JSON, e))
        return CAP
    if "maxParallel" not in data:
        return CAP
    v = data["maxParallel"]
    if isinstance(v, int) and not isinstance(v, bool) and v >= 1:
        return v
    print("maxParallel 값 %r 은 쓸 수 없어 %d 로 돈다 — %s 맨 위의 \"maxParallel\" 을 1 이상 정수로 고친다"
          % (v, CAP, PROJECTS_JSON))
    return CAP


_PATH_EXT = r"\.(?:md|py|json|yaml|yml|sh|js|ts|html|txt)"


def start_paths(text):
    """카드 본문 「## 시작점」(뒤에 「 — …」가 붙어도 된다) 절(다음 `## ` 전까지)의 백틱 조각 가운데 저장소 경로. 끝의 `:줄번호` 는 뗀다.
    경로 = 공백·`(` 없이 `/` 를 품었거나 `이름.확장자` 꼴. 절이 없으면 빈 목록.
    절 안에 「고치는 파일」 줄(`- 고치는 파일: …` 또는 `| 고치는 파일 | … |`)이 있고 거기 경로가 있으면 그 줄들의 경로만 —
    읽기만 하는 인용 문서가 겹침으로 세지 않게 한다."""
    inside, out, fixed = False, [], []
    for line in text.splitlines():
        if line.startswith("## "):
            inside = re.match(r"시작점(\s|$)", line[3:].strip()) is not None   # 「시작점」·「시작점 — 실재를 확인한 경로」
            continue
        if not inside:
            continue
        mine = re.match(r"\s*(?:[-*]\s*고치는 파일\s*:|\|\s*고치는 파일\s*\|)", line) is not None
        for frag in re.findall(r"`([^`]+)`", line):
            frag = re.sub(r":\d+(?:-\d+)?$", "", frag.strip())
            if " " in frag or "(" in frag:
                continue
            if "/" in frag or re.fullmatch(r"[^/\s]+" + _PATH_EXT, frag):
                out.append(frag)
                if mine:
                    fixed.append(frag)
    return fixed or out


def read_cards(project_path):
    """(카드 목록, 건너뛴 까닭). 까닭이 있으면 그 프로젝트는 이번 훑기에서 뺀다."""
    if not os.path.isdir(project_path):
        return [], "경로 없음"
    folder = os.path.join(project_path, "docs", "backlog")
    if not os.path.isdir(folder):
        return [], "docs/backlog 없음"
    cards = []
    for fname in sorted(os.listdir(folder)):
        if not fname.endswith(".md") or fname.startswith("_"):
            continue
        path = os.path.join(folder, fname)
        try:
            head = board.front_matter(path)
            with open(path, encoding="utf-8") as f:
                starts = start_paths(f.read())
        except (OSError, UnicodeDecodeError):
            return [], "카드를 읽지 못함: %s" % fname
        cards.append({"file": fname, "path": path, "시작점": starts, **{k: head.get(k, "")
                      for k in ("상태", "시나리오", "요소", "선행", "기기")}})
    return cards, None


def _ready(card, cards, lists=None):
    """앞선 카드가 모두 완료인가. 묶인 카드는 `선행:` 의 요소 번호를 같은 시나리오 안에서, 시나리오 밖 카드는 카드 이름으로 찾는다.

    lists = {시나리오 번호: 「이어지는 이슈」 표의 카드 이름 집합 또는 None}. 표가 있으면 표 밖 카드(회고 카드)는 선행을 막지 않는다.
    """
    raw = card["선행"].strip()
    if raw in ("", "—", "-"):
        return True
    if not _bound(card):                              # 시나리오 밖 카드: 항목은 카드 이름(파일 이름에서 `.md` 뺀 것)
        for name in (e.strip() for e in raw.split(",") if e.strip()):
            same = [c for c in cards if c["file"][:-3] == name]
            if not same or any(c["상태"] != "완료" for c in same):
                return False
        return True
    names = (lists or {}).get(card["시나리오"])
    for el in (e.strip() for e in raw.split(",") if e.strip()):
        same = [c for c in cards if c["시나리오"] == card["시나리오"] and el in board.list_value(c["요소"])
                and (names is None or c["file"][:-3] in names)]
        if not same or any(c["상태"] != "완료" for c in same):
            return False
    return True


def _bound(card):
    """시나리오에 묶인 카드인가 (`시나리오:` 칸이 비었거나 `—`·`-` 이면 아니다)."""
    return card["시나리오"].strip() not in ("", "—", "-")


OTHER_DEVICE = "다른 기기 카드만 남음"


def _other_device(card, device):
    """이 기기(device)와 다른 기기의 카드인가. device 가 비었거나 카드 `기기` 가 비었거나 `—`·`-` 이면 아니다."""
    dev = card.get("기기", "").strip()
    return bool(device) and dev not in ("", "—", "-") and dev != device


def shared_files(project):
    """프로젝트 설정 `sharedFiles` 의 규칙 `[{"file": str, "touchedBy": [str, ...]}]`. 못 읽거나 모양이 틀린 것은 빼고, 없으면 `[]`.
    빈 접두(`""`)는 모든 경로에 닿으므로 버린다."""
    text = runner.read_config(project)
    try:
        raw = json.loads(text)["sharedFiles"] if text else []
    except (ValueError, KeyError, TypeError):
        return []
    rules = []
    for r in raw if isinstance(raw, list) else []:
        if not (isinstance(r, dict) and isinstance(r.get("file"), str) and r["file"]
                and isinstance(r.get("touchedBy"), list)):
            continue
        rules.append({"file": r["file"], "touchedBy": [t for t in r["touchedBy"] if isinstance(t, str) and t]})
    return rules


def effective_starts(card, shared):
    """겹침 판정에 쓰는 카드 경로 — 시작점에, 규칙의 `touchedBy` 접두로 시작하거나 `file` 과 같은 시작점이 있으면 그 규칙의 `file` 을 더한 것."""
    starts = list(card.get("시작점", []))
    for r in shared:
        if r["file"] not in starts and any(s == r["file"] or s.startswith(tuple(r["touchedBy"])) for s in starts):
            starts.append(r["file"])
    return starts


def pick_next(cards, handed, live=frozenset(), held=frozenset(), lists=None, device="", opened=frozenset(), shared=()):
    """(집을 카드 또는 None, 건너뛴 까닭 또는 None).

    live = 이번 배차가 넘겨 아직 끝나지 않은 카드 경로. 돌고 있는 카드 = 상태 진행 또는 live.
    held = 승인되지 않은 시나리오 번호 — 그 시나리오의 대기 카드는 집지 않는다(E-2).
    lists = 선행 판정에 쓰는 시나리오별 카드 목록(`_ready`).
    device = 이 기기 이름. 다른 기기 카드는 집지도, 돌고 있는 카드로 세지도 않는다(상태는 건드리지 않는다). 선행 판정은 전체 카드를 본다.
    opened = PR 이 열린 채 병합을 기다리는 카드 경로. 그 카드의 시작점은 겹침에 넣지만 돌고 있는 카드로는 세지 않는다 —
    겹치지 않는 다른 카드와 재인계는 막지 않는다. 그것 때문에만 못 집으면 까닭은 `WAIT_PR`.
    돌고 있는 카드와 `시작점` 경로가 하나라도 같은 카드는 집지 않는다(시작점이 비면 겹치지 않는다) — 낱장·시나리오 카드 모두.
    shared = 설정 `sharedFiles` 규칙(`shared_files`). 있으면 겹침 경로를 `effective_starts` 로 넓혀 센다.
    """
    mine = [c for c in cards if not _other_device(c, device)]
    running = [c for c in mine if c["상태"] == "진행" or c["path"] in live]
    skip = "진행 카드 있어 건너뜀"
    busy = {p for c in running for p in effective_starts(c, shared)}
    waiting = {p for c in mine if c["path"] in opened and c not in running for p in effective_starts(c, shared)}
    pending = [c for c in mine if c["상태"] == "대기" and c["path"] not in handed and _ready(c, cards, lists)
               and c["시나리오"] not in held and not busy.intersection(effective_starts(c, shared))]
    ready = [c for c in pending if not waiting.intersection(effective_starts(c, shared))]
    if ready:
        return min(ready, key=lambda c: c["file"]), None
    if running:
        return None, skip
    if len(pending) > len(ready):
        return None, WAIT_PR
    others = any(_other_device(c, device) and c["상태"] == "대기" and c["path"] not in handed for c in cards)
    return (None, OTHER_DEVICE) if others else (None, None)


def set_scenario_status(plan_dir, no, value):
    """시나리오 파일 머리 표와 `README.md` 색인 해당 줄의 「상태」 칸 맨 앞 상태 낱말을 `value` 로 바꾼다."""
    def swap(cell):
        body = cell.lstrip()
        word = next((w for w in board._시나리오상태 if body.startswith(w)), None)
        return cell[:len(cell) - len(body)] + value + body[len(word):] if word else cell

    def rewrite(path, fix):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines(True)
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(fix(lines))

    def head(lines):
        for i, l in enumerate(lines):
            parts = l.split("|")
            if l.strip().startswith("|") and len(parts) > 2 and parts[1].strip() == "상태":
                parts[2] = swap(parts[2])
                lines[i] = "|".join(parts)
                break
        return lines

    def index(lines):
        col = next((i for l in lines if l.strip().startswith("|") and "[SC-" not in l
                    for i, c in enumerate(l.split("|")) if c.strip() == "상태"), None)
        for i, l in enumerate(lines):
            parts = l.split("|")
            if col and "[%s]" % no in l and len(parts) > col:
                parts[col] = swap(parts[col])
                lines[i] = "|".join(parts)
        return lines
    for sc in board.read_scenarios(plan_dir):
        if sc["번호"] == no:
            rewrite(os.path.join(plan_dir, sc["file"]), head)
    readme = os.path.join(plan_dir, "README.md")
    if os.path.exists(readme):
        rewrite(readme, index)


def listed(plan_dir, scenario):
    """시나리오 「## 이어지는 이슈」 표 첫 칸의 카드 이름 집합. 절이 없거나 데이터 줄이 없으면 None."""
    with open(os.path.join(plan_dir, scenario["file"]), encoding="utf-8") as f:
        lines = f.read().splitlines()
    names, inside, header = set(), False, True
    for l in lines:
        if l.startswith("## "):
            inside = l[3:].strip() == "이어지는 이슈"
            header = True
            continue
        if not inside or not l.strip().startswith("|"):
            continue
        if header:                       # 첫 표줄은 머리줄
            header = False
            continue
        first = board._칸(l)[0].strip("` ")
        if not first or "{" in first or set(first) <= set("-: "):
            continue
        names.add(os.path.basename(first)[:-3] if first.endswith(".md") else os.path.basename(first))
    return frozenset(names) if names else None


def _mine(scenario, cards):
    """그 시나리오의 요소 카드. 시나리오에 목록(`이슈`)이 있으면 목록에 이름이 있는 카드만."""
    listed_names = scenario.get("이슈")
    return [c for c in cards if c["시나리오"] == scenario["번호"]
            and (listed_names is None or c["file"][:-3] in listed_names)]


def judge(scenario, cards, board_cards):
    """확정 시나리오의 승인 판정 `(거부 까닭 목록, 올릴 카드 목록)`. 까닭이 있으면 올릴 카드는 없다."""
    no = scenario["번호"]
    mine = _mine(scenario, cards)
    why = []
    if scenario["남은질문"]:
        why.append("「확인 필요」에 질문이 남음 (%s)" % " / ".join(scenario["남은질문"]))
    if not mine:
        why.append("요소 카드 초안 없음")
    else:
        loose = scenario["요소"] - scenario["카드아님"] - {e for c in mine for e in board.list_value(c["요소"])}
        if loose:
            why.append("카드에 담기지 않은 요소: " + ", ".join(sorted(loose)))
        loop = [f for f, m in board.card_marks(board_cards, [scenario]).items()
                if any("고리" in x for x in m)]
        if loop:
            why.append("앞선 관계가 고리를 이룸: " + ", ".join(sorted(loop)))
    if why:
        return why, []
    return why, [c for c in mine if c["상태"] == "아이디어" and _ready(c, cards, {no: scenario.get("이슈")})]


def promote(project, name=None):
    """승인된 시나리오의 요소 카드를 대기로 올린다. 찍을 줄 목록(거부·쓰기 실패)을 돌려준다."""
    name = name or os.path.basename(os.path.abspath(project))
    plan = os.path.join(project, "docs", "plan")
    if not os.path.isdir(plan):
        return []
    cards, why = read_cards(project)
    if why:
        return []
    bcards = board.read_cards(os.path.join(project, "docs", "backlog"))
    lines = []
    for sc in board.read_scenarios(plan):
        no = sc["번호"]
        if sc["오류"] or sc["상태"] not in ("확정", "구현 중"):
            continue
        try:
            sc = dict(sc, 이슈=listed(plan, sc))
        except OSError:                  # 목록을 못 읽으면 회고 카드가 오르는 쪽으로 넘어지지 않게 건너뛴다
            continue
        if sc["상태"] == "구현 중":
            lift = [c for c in _mine(sc, cards) if c["상태"] == "아이디어" and _ready(c, cards, {no: sc["이슈"]})]
            why = []
        else:
            why, lift = judge(sc, cards, bcards)
        if why:
            lines.append("승인 거부 — %s/%s: %s" % (name, no, "; ".join(why)))
            continue
        try:
            for c in lift:
                backlog.set_status(c["path"], "대기")
            if sc["상태"] == "확정":
                log = os.path.join(plan, "사람손-%s.md" % no)
                seen = os.path.exists(log) and "· 승인 ·" in open(log, encoding="utf-8").read()
                board.record_resolved(project, {
                    "시나리오": no, "종류": "승인", "열어 볼 곳": sc["file"],
                    "기다리는 것": "요소 카드 %d장 대기로" % len(lift),
                    "생긴 때": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "셈": "사람 손" if seen else "시작"})
                set_scenario_status(plan, no, "구현 중")   # 마지막 — 이것이 승인을 받아들인 표시다
        except OSError as e:
            lines.append("승격하지 못함 — %s/%s: %s" % (name, no, e))
    return lines


def held(project):
    """시나리오 파일이 있고 상태가 「구현 중」이 아닌 시나리오 번호 집합."""
    return {s["번호"] for s in board.read_scenarios(os.path.join(project, "docs", "plan"))
            if s["상태"] != "구현 중"}


def lists(project):
    """시나리오 번호 → 「이어지는 이슈」 표의 카드 이름 집합(`listed`). 못 읽으면 None — 선행을 거르지 않는다."""
    plan = os.path.join(project, "docs", "plan")
    out = {}
    for s in board.read_scenarios(plan):
        try:
            out[s["번호"]] = listed(plan, s)
        except OSError:
            out[s["번호"]] = None
    return out


def check_account(project_path, card_path):
    """계정 토큰을 얻으면 True. 못 얻으면 카드를 보류로 고치고 False. 계정 칸이 없으면 묻지 않는다."""
    account = runner.load_models(project_path).get("_githubAccount")
    if not account or gh_token(account):
        return True
    old = runner._backlog_path
    runner._backlog_path = card_path
    try:
        runner.backlog_set(상태="보류",
                           멈춘이유="GitHub 계정 %s 의 토큰을 얻지 못해 배차가 시작하지 않음" % account)
    finally:
        runner._backlog_path = old
    return False


def _set_head(card_path, **fields):
    """카드 머리말을 러너의 `backlog_set` 으로 고친다(`check_account` 와 같은 방식). 썼으면 True."""
    old = runner._backlog_path
    runner._backlog_path = card_path
    try:
        return runner.backlog_set(**fields)
    finally:
        runner._backlog_path = old


def _count(card):
    try:
        return int(card["head"].get("재인계", "0") or 0)
    except ValueError:
        return 0


def rehandoff_candidates(cards):
    """재인계 후보 — 머리말이 `상태: 보류`·`차례: 러너` 인 카드(기준 가지를 기다리는 카드). 머리말은 따로 읽는다."""
    out = []
    for c in cards:
        if c["상태"] != "보류":
            continue
        try:
            head = board.front_matter(c["path"])
        except (OSError, UnicodeDecodeError):
            continue
        if head.get("차례") == "러너":
            out.append(dict(c, head=head))
    return out


def failed_checks(project_path, card_file):
    """카드 작업 폴더 최신 실행 디렉터리 `10-merge.md` 의 「기계 검사 실패」 명령 목록. 못 읽으면 빈 목록."""
    stem = os.path.splitext(card_file)[0]
    run, _ = runner.pick_run_dir("%s.%s" % (project_path.rstrip("/"), stem), stem)
    if not run:
        return []
    try:
        with open(os.path.join(run, "10-merge.md"), encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return []
    seen = []
    for cmd, rc in runner.MACHINE_FAIL_RE.findall(text):
        if cmd not in [x["cmd"] for x in seen]:
            seen.append({"cmd": cmd, "rc": int(rc), "tail": ""})
    return seen


def base_fixed(project_path, failed):
    """원격 기준 가지 최신 판에서 실패했던 검사가 모두 통과하면 True. 읽지 못하거나 판을 못 만들면 False."""
    try:
        models = runner.load_models(project_path)
        if any(x["cmd"] not in {c["cmd"] for c in models.get("_checks") or []} for x in failed):
            return False                 # 설정에서 사라진 검사는 다시 돌릴 수 없다 — 고쳐졌다고 보지 않는다
        sha = runner.start_point(project_path)
        if sha is None:
            return False
        split = runner.classify_failures(project_path, sha, failed, models)
    except (SystemExit, OSError):        # start_point 는 원격을 못 읽으면 종료한다 — 배차를 죽이지 않는다
        return False
    return not split["error"] and not split["base"]


def pick_rehandoff(project_path, cards, handed, live, device=""):
    """다시 인계할 카드 또는 None. 상한 도달 카드는 `차례` 를 비워 사람 차례로 올린다. 본 카드는 handed 에 넣는다.
    다른 기기 카드는 후보에도 돌고 있는 카드에도 넣지 않는다."""
    cards = [c for c in cards if not _other_device(c, device)]
    name = os.path.basename(os.path.abspath(project_path))
    cands = rehandoff_candidates(cards)
    keep = []
    for c in cands:
        if _count(c) >= REHANDOFF_CAP and c["path"] not in live:
            if _set_head(c["path"], 차례=""):
                print("내가 할 일로 올림 — %s/%s: 재인계 %d회 소진" % (name, c["file"], _count(c)))
            continue
        keep.append(c)
    running = [c for c in cards if c["상태"] == "진행" or c["path"] in live]
    if any(not _bound(c) for c in running):
        return None
    for c in sorted(keep, key=lambda c: c["file"]):
        if c["path"] in handed or c["path"] in live or (running and not _bound(c)):
            continue
        handed.add(c["path"])            # 프로브는 카드당 배차 한 번
        failed = failed_checks(project_path, c["file"])
        if not failed:
            print("재인계 안 함 — %s/%s: 실패한 검사를 읽지 못함" % (name, c["file"]))
        elif not base_fixed(project_path, failed):
            print("재인계 안 함 — %s/%s: 기준 가지에서 아직 실패" % (name, c["file"]))
        else:
            return c
    return None


def _run(cmd, cwd, env):
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True)


def latest_pr(root, card, env):
    """(PR 또는 None, 오류 문구). `--state all` 이 없으면 gh 는 열린 PR 만 내놓아 병합된 것이 안 보인다.
    정리 스크립트(`bin/ship_cards.py`)와 상주 배차가 이 한 곳을 함께 쓴다."""
    res = _run(["gh", "pr", "list", "--head", "loop/" + card, "--state", "all",
                "--json", "number,state,url,createdAt"], root, env)
    if res.returncode:
        return None, "PR 상태를 읽지 못함: " + (res.stderr or res.stdout).strip()
    try:
        prs = json.loads(res.stdout)
        if any(p["state"] not in ("OPEN", "MERGED", "CLOSED") for p in prs):
            raise ValueError("알 수 없는 상태")
    except (ValueError, KeyError, TypeError) as e:
        return None, "PR 상태를 읽지 못함: %s" % e
    return (max(prs, key=lambda p: p["createdAt"]) if prs else None), None


def align_cards(root, env):
    """pull 이 거부되지 않게: 원격 기준 가지가 기계 열쇠 줄만 바꾼 카드 가운데 작업 파일이 고쳐진 채인 것의
    색인 항목만 원격 내용으로 맞춘다. 작업 파일은 건드리지 않는다. 본문·사람 열쇠까지 바뀐 카드는 맞추지 않는다."""
    git = lambda *a, **k: runner._card_git(root, *a, env=env, **k)       # noqa: E731
    url = git("remote", "get-url", "origin").stdout.strip()
    if not url:
        return
    _, tip, err = runner._remote_tip(root, url, env)
    if err:
        return
    for rel in git("diff", "--name-only", "HEAD", tip, "--", "docs/backlog").stdout.splitlines():
        if git("diff", "--quiet", "--", rel).returncode == 0 or git("diff", "--cached", "--quiet", "--", rel).returncode:
            continue                                  # 작업 파일이 그대로이거나 색인을 사람이 건드렸다
        old, new = git("show", "HEAD:" + rel), git("show", "%s:%s" % (tip, rel))
        ent = git("ls-tree", tip, "--", rel).stdout.split()
        if old.returncode or new.returncode or len(ent) < 3 \
                or runner.card_without_keys(old.stdout) != runner.card_without_keys(new.stdout):
            continue
        git("update-index", "--cacheinfo", "%s,%s,%s" % (ent[0], ent[2], rel))


def open_prs(project_path, cards, env):
    """`(PR 을 기다리는 카드 경로 집합, {경로: 까닭})`. `검토` 카드에만 PR 을 묻는다 — 열린 PR 이거나 읽지 못한 카드(막는 쪽으로 넘어진다)."""
    opened, why = set(), {}
    for c in cards:
        if c["상태"] != "검토":
            continue
        try:
            pr, err = latest_pr(project_path, c["file"][:-3], env)
        except OSError as e:
            pr, err = None, "PR 상태를 읽지 못함: %s" % e
        if err:
            opened.add(c["path"])
            why[c["path"]] = err
        elif pr and pr["state"] == "OPEN":
            opened.add(c["path"])
    return opened, why


def _project_env(project_path):
    """자식 프로세스에만 넣는 계정 토큰. 계정 칸이 없거나 토큰이 없으면 그대로."""
    env = dict(os.environ)
    try:
        account = runner.load_models(project_path).get("_githubAccount")
    except Exception:                # noqa: BLE001 — 설정을 못 읽어도 바퀴는 돈다
        account = None
    token = gh_token(account) if account else None
    if token:
        env["GH_TOKEN"] = token
    return env


def sync_remote(project_path):
    """원격 기준 가지를 빨리감기로만 받는다. 받았거나 받을 것이 없으면 None, 건너뛰면 까닭 글. 예외를 내지 않는다.
    받는 조건: 작업 사본이 기준 가지에 있고 · 커밋 안 된 변경이 없거나 카드 기계 열쇠 줄뿐이고 · 빨리감기가 된다.
    병합·되감기·덮어쓰기는 하지 않는다."""
    try:
        if not os.path.exists(os.path.join(project_path, ".git")):
            return None
        env = _project_env(project_path)
        git = lambda *a, **k: runner._card_git(project_path, *a, env=env, **k)       # noqa: E731
        url = git("remote", "get-url", "origin").stdout.strip()
        if not url:
            return None
        base, tip, err = runner._remote_tip(project_path, url, env)
        if err:
            return "원격을 받지 못함: " + err
        cur = git("symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or "(분리된 HEAD)"
        if cur != base:
            return "기준 가지가 아닌 가지 %s 에 있음" % cur
        for line in git("status", "--porcelain", "--untracked-files=no").stdout.splitlines():
            rel = line[3:].strip().strip('"')
            if line[:2] in (" M", "M ", "MM") and rel.startswith("docs/backlog/") and rel.endswith(".md"):
                old = git("show", "HEAD:" + rel)
                try:
                    with open(os.path.join(project_path, rel), encoding="utf-8") as f:
                        new = f.read()
                except (OSError, UnicodeDecodeError):
                    return "커밋 안 된 변경: " + rel
                if old.returncode == 0 and runner.card_without_keys(old.stdout) == runner.card_without_keys(new):
                    continue
            return "커밋 안 된 변경: " + rel
        if git("merge-base", "--is-ancestor", tip, "HEAD").returncode == 0:
            return None
        if git("merge-base", "--is-ancestor", "HEAD", tip).returncode:
            return "빨리감기가 안 됨: 작업 사본 %s 에 원격에 없는 커밋이 있음" % base
        align_cards(project_path, env)
        res = git("merge", "--ff-only", tip)
        if res.returncode:
            return "빨리감기가 안 됨: " + ((res.stderr or res.stdout).strip().splitlines() or ["?"])[-1]
        return None
    except Exception as e:                # noqa: BLE001 — 원격 맞추기가 바퀴를 죽이지 않는다
        return "원격을 받지 못함: %s" % e


def _head(project_path):
    """작업 사본 `HEAD` 의 sha. 못 읽으면 빈 값."""
    try:
        res = _run(["git", "rev-parse", "HEAD"], project_path, None)
        return res.stdout.strip() if res.returncode == 0 else ""
    except OSError:
        return ""


def version_drift():
    """상주 배차 자신의 판과 지금 설치된 판이 다르면 「<자기 판> → <설치된 판>」. 플러그인이 아니거나 한쪽이라도 못 읽으면 None."""
    if not runner.PLUGIN:
        return None
    try:
        with open(PLUGIN_JSON, encoding="utf-8") as f:
            mine = json.load(f)["version"]
        with open(os.path.join(os.path.expanduser(CONFIG_DIR), "plugins", "installed_plugins.json"), encoding="utf-8") as f:
            plugins = json.load(f)["plugins"]
        key = next(k for k in sorted(plugins) if k.split("@", 1)[0] == runner.PLUGIN)
        # 한 열쇠 아래 사용자 범위와 다른 프로젝트의 프로젝트 범위 항목이 섞인다 — 사용자 범위(또는 옛 모양)만 본다.
        theirs = next(e["version"] for e in plugins[key]
                      if isinstance(e, dict) and (e.get("scope") == "user" or ("scope" not in e and "projectPath" not in e)))
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, StopIteration):
        return None
    if not (isinstance(mine, str) and mine and isinstance(theirs, str) and theirs) or mine == theirs:
        return None
    return "%s → %s" % (mine, theirs)


def 상주_켜기():
    """로그인 항목 파일 하나를 쓰고 등록한다 — 지금 이 dispatch.py 를 가리킨다. 보드의 등록은 건드리지 않는다."""
    path = os.path.join(board.LAUNCH_DIR, LAUNCH_LABEL + ".plist")
    os.makedirs(board.LAUNCH_DIR, exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump({"Label": LAUNCH_LABEL, "ProgramArguments": [sys.executable, os.path.abspath(__file__), "--상주"],
                       "RunAtLoad": True, "KeepAlive": True, "AbandonProcessGroup": True,
                       "EnvironmentVariables": {"PATH": board._상주_PATH()}}, f)
    domain = "gui/%d" % os.getuid()
    board._launchctl(["bootout", "%s/%s" % (domain, LAUNCH_LABEL)])      # 이미 있으면 내린다 — 없어서 실패해도 무방
    rc, err = board._launchctl(["bootstrap", domain, path])
    if rc:
        print("로그인 항목을 등록하지 못했다: %s" % err)
        return 1
    print("켰다 — 로그인 때 상주 배차가 뜬다: %s" % path)
    return 0


def 상주_끄기():
    board._launchctl(["bootout", "gui/%d/%s" % (os.getuid(), LAUNCH_LABEL)])
    try:
        os.remove(os.path.join(board.LAUNCH_DIR, LAUNCH_LABEL + ".plist"))
    except FileNotFoundError:
        pass
    print("껐다")
    return 0


class _Why(dict):
    """프로젝트 이름 → 건너뛴 까닭. 같은 바퀴에 까닭이 둘 이상이면 덮어쓰지 않고 「 · 」 로 잇는다."""

    def __setitem__(self, name, why):
        old = self.get(name)
        if old and why not in old.split(" · "):
            why = "%s · %s" % (old, why)
        elif old:
            why = old
        super().__setitem__(name, why)


class _Resident:
    """상주 배차 — 바퀴마다 프로젝트를 원격과 맞추고, 병합된 카드를 정리하고, 대기 카드를 집고, 살아 있음 파일을 쓴다.
    인자 없는 배차가 「한 번」 동안만 들고 있던 상태(handed·dead·shown)는 여기서 바퀴마다 풀린다."""

    def __init__(self):
        self.handed = set()      # 넘긴 카드 경로 — 러너가 돌지 않고 디스크 상태가 대기가 아니게 된 것을 보면 풀린다
        self.running = {}        # 카드 경로 → (프로젝트 이름, 프로세스, 카드)
        self.dead = set()        # 토큰을 못 얻어 더 보지 않는 프로젝트 — 바퀴마다 토큰을 다시 묻는다
        self.probed = {}         # 재인계 후보 경로 → 판정한 때의 작업 사본 HEAD
        self.prev_shown, self.cur_shown = set(), set()
        self.skipped, self.skipwheel = _Why(), set()
        self.paths = {}
        self.seen = {}           # 자동 이어받기 후보 카드 경로 → 처음 본 때(`_mono`) — 메모리에만 둔다
        self.wheel, self.stop, self.drift = 0, False, ""
        self.cap = max_parallel()

    def write(self):
        data = {"기기": this_machine(), "때": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "바퀴": self.wheel,
                "pid": os.getpid(), "멈춤": self.stop, "판바뀜": self.drift,
                "카드": [{"프로젝트": name, "카드": card["file"]} for name, _, card in self.running.values()],
                "건너뜀": dict(self.skipped)}
        tmp = "%s.tmp%d" % (STATUS_FILE, os.getpid())
        try:
            os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, STATUS_FILE)
        except OSError:
            pass

    def reap(self):
        for key, (name, proc, card) in list(self.running.items()):
            if proc.poll() is None:
                continue
            del self.running[key]
            if board.front_matter(card["path"]).get("상태", "") != "대기":
                self.handed.discard(key)
            backlog.tidy(self.paths[name], 실행=True)     # 병합된 카드를 완료로, 끝 보고 방아쇠
            endreport.auto(self.paths[name])

    def visit(self, name, path, device):
        if name in self.dead:
            account = runner.load_models(path).get("_githubAccount")
            if account and not gh_token(account):
                self.skipped[name] = "계정 토큰 없음"
                return
            self.dead.discard(name)
        why = sync_remote(path)
        if why:
            self.skipped[name] = why
        self.requests(name, path)
        backlog.tidy(path, 실행=True)
        for line in promote(path, name):
            self.cur_shown.add(line)
            if line not in self.prev_shown:
                print(line)
        self.auto_resume(name, path)
        while len(self.running) < self.cap and self.take(name, path, device):
            pass

    def auto_resume(self, name, path):
        """일시적 실패로 멈춘 이 기기의 보류 카드를 AUTO_RESUME_WAIT 초 기다린 뒤 실행마다 한 번 `--resume` 으로 잇는다.
        「한 번」은 실행 폴더의 표지 파일만으로 센다. 사람 요청(`requests`)이 먼저 돌고, 돌고 있는 카드·동시 상한이 찬 바퀴는 미룬다."""
        folder = os.path.join(path, "docs", "backlog")
        live = set()
        for fname in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            card_path = os.path.join(folder, fname)
            if not (fname.endswith(".md") and os.path.isfile(card_path)):
                continue
            if card_path in self.running:
                continue
            head = board.front_matter(card_path)
            if not auto_resume_candidate(head):
                continue
            live.add(card_path)
            first = self.seen.setdefault(card_path, _mono())
            if _mono() - first < AUTO_RESUME_WAIT or len(self.running) >= self.cap:
                continue
            stem = os.path.splitext(fname)[0]
            wd, _ = runner.pick_run_dir("%s.%s" % (path.rstrip("/"), stem), stem)
            if not wd:
                self.skipped[name] = "이어받을 실행 폴더가 없다: %s" % fname
                continue
            if has_auto_mark(wd):
                continue
            if not check_account(path, card_path):
                self.dead.add(name)
                self.skipped[name] = "계정 토큰 없음"
                break
            if not write_auto_mark(wd):
                print("자동 이어받기 안 함 — %s/%s: 표지를 쓰지 못함" % (name, fname))
                continue
            try:
                prev = json.loads(head.get("요청결과") or "{}")
            except ValueError:
                prev = {}
            number = prev.get("번호") if isinstance(prev, dict) else None
            if not (isinstance(number, str) and number) or number.startswith("자동:"):
                number = None                  # 사람 요청 번호만 그대로 둔다 — 지난 자동 번호는 이번 실행 것으로 바꾼다
            row = {"번호": number or "자동:%s" % os.path.basename(wd.rstrip("/")), "종류": "자동 이어받기", "결과": "이어받음",
                   "까닭": "일시적 실패로 멈춘 뒤 10분 기다려 배차가 스스로 이어받음",
                   "때": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
            if not _set_head(card_path, 요청결과=json.dumps(row, ensure_ascii=False)):
                print("자동 이어받기 보임칸을 쓰지 못함 — %s/%s" % (name, fname))
            print("자동 이어받음 — %s/%s" % (name, fname))
            self.seen.pop(card_path, None)
            self.handed.add(card_path)
            env = dict(os.environ, STOP_LOOP_DEVICE=this_machine())
            self.running[card_path] = (name, subprocess.Popen([sys.executable, RUNNER, path, card_path, "--resume"], env=env),
                                       {"file": fname, "path": card_path})
        for key in [k for k in self.seen if os.path.dirname(k) == folder and k not in live]:
            del self.seen[key]

    def requests(self, name, path):
        """원격에서 받은 `docs/backlog/_요청/*.json`(보드가 올린 답·이어받기·보류로 두기)를 이 기기가 돌던 카드에 대해서만 처리한다.
        처리했는가는 카드 머리말 `요청결과` 의 번호로 가른다. 띄우기 전에 그 값을 쓰고 못 쓰면 띄우지 않는다. 동시 상한이 찼거나
        그 카드가 이미 돌고 있으면 아무것도 쓰지 않고 다음 바퀴로 미룬다."""
        folder = os.path.join(path, "docs", "backlog", "_요청")
        if not os.path.isdir(folder):
            return
        for fname in sorted(os.listdir(folder)):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(folder, fname), encoding="utf-8") as f:
                    req = json.load(f)
                card_file, kind, number = req["카드"], req["종류"], req["번호"]
                if not (isinstance(card_file, str) and re.fullmatch(r"[^/\\\0]+\.md", card_file) and kind in ("답", "이어받기", "보류로 두기")
                        and isinstance(number, str) and number):
                    raise ValueError("모양이 맞지 않음")
            except (OSError, ValueError, KeyError, TypeError) as e:
                self.skipped[name] = "요청 파일을 읽지 못함: %s (%s)" % (fname, e)
                continue
            card_path = os.path.join(path, "docs", "backlog", card_file)
            if not os.path.isfile(card_path):
                self.skipped[name] = "요청 카드가 없다: %s" % card_file
                continue
            head = board.front_matter(card_path)
            try:
                done = json.loads(head.get("요청결과") or "{}")
            except ValueError:
                done = {}
            if isinstance(done, dict) and done.get("번호") == number:
                continue
            if not is_my_card(head.get("돈기계")):
                continue
            if card_path in self.running or len(self.running) >= self.cap:
                continue

            def result(결과, 까닭="", **more):
                row = {"번호": number, "종류": kind, "결과": 결과, "까닭": 까닭, "때": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
                return _set_head(card_path, 요청결과=json.dumps(row, ensure_ascii=False), **more)
            why = None
            if head.get("상태") != "보류":
                why = "카드가 보류가 아님(지금 %s)" % (head.get("상태") or "모름")
            elif kind == "이어받기" and head.get("멈춘종류") != "일시적 실패":
                why = "멈춘종류가 일시적 실패가 아님(%s)" % (head.get("멈춘종류") or "없음")
            if why:
                result("못 받음", why)
                continue
            if kind == "보류로 두기":
                if result("보류로 둠", 단계=""):
                    print("보류로 둠 — %s/%s" % (name, card_file))
                continue
            if kind == "답":
                stem = os.path.splitext(card_file)[0]
                wd, _ = runner.pick_run_dir("%s.%s" % (path.rstrip("/"), stem), stem)
                if not wd:
                    result("못 받음", "이어받을 실행 폴더가 없다")
                    continue
                try:
                    append_human_answers(wd, number, req.get("답") or [])
                except (OSError, KeyError, TypeError) as e:
                    result("못 받음", "답을 쓰지 못함: %s" % e)
                    continue
            if not check_account(path, card_path):
                self.dead.add(name)
                self.skipped[name] = "계정 토큰 없음"
                return
            if not result("이어받음"):
                print("요청 안 받음 — %s/%s: 요청 결과를 쓰지 못함" % (name, card_file))
                continue
            print("요청 받음 — %s/%s: %s" % (name, card_file, kind))
            self.handed.add(card_path)
            env = dict(os.environ, STOP_LOOP_DEVICE=this_machine())
            self.running[card_path] = (name, subprocess.Popen([sys.executable, RUNNER, path, card_path, "--resume"], env=env),
                                       {"file": card_file, "path": card_path})

    def take(self, name, path, device):
        """카드 하나를 집어 러너를 띄웠으면 True."""
        cards, why = read_cards(path)
        for c in cards:
            if c["path"] in self.handed and c["path"] not in self.running and c["상태"] != "대기":
                self.handed.discard(c["path"])
        card, pr_why = None, {}
        if not why:
            opened = set()
            if any(c["상태"] == "검토" for c in cards):
                opened, pr_why = open_prs(path, cards, _project_env(path))
            card, why = pick_next(cards, self.handed | self.skipwheel, set(self.running), held(path), lists(path),
                                  device=device, opened=opened, shared=shared_files(path))
            if why == WAIT_PR and pr_why:
                why = next(iter(pr_why.values()))
        again = False
        if card is None and not (why and why not in (None, "진행 카드 있어 건너뜀", OTHER_DEVICE, WAIT_PR)):
            head = _head(path)
            probe = self.handed | {p for p, h in self.probed.items() if h == head}
            before = set(probe)
            card = pick_rehandoff(path, cards, probe, set(self.running), device=device)
            self.probed.update({p: head for p in probe - before})
            again = card is not None
            if again:
                why = None
        if why:
            self.skipped[name] = why
            return False
        if card is None:
            stale = [c["file"] for c in cards if c["path"] in self.handed and c["path"] not in self.running and c["상태"] == "대기"]
            if stale:
                self.skipped[name] = "다시 넘기지 않음 — 러너가 상태를 바꾸지 않고 끝남: " + ", ".join(stale)
            return False
        if board.front_matter(card["path"]).get("상태", "") != ("보류" if again else "대기"):
            self.skipwheel.add(card["path"])        # 그 사이 다른 배차가 집었다 — 띄우지 않는다
            return True
        if not check_account(path, card["path"]):
            self.dead.add(name)
            self.skipped[name] = "계정 토큰 없음"
            return False
        if again:
            n = _count(card) + 1
            if not _set_head(card["path"], 재인계=n):
                print("재인계 안 함 — %s/%s: 재인계 횟수를 쓰지 못함" % (name, card["file"]))
                self.skipwheel.add(card["path"])
                return True
            print("재인계 — %s/%s: 기준 가지가 고쳐짐, %d회째" % (name, card["file"], n))
        self.handed.add(card["path"])
        args = [sys.executable, RUNNER, path, card["path"]] + (["--resume"] if again else [])
        env = dict(os.environ, STOP_LOOP_DEVICE=this_machine() or device)
        self.running[card["path"]] = (name, subprocess.Popen(args, env=env), card)
        return True

    def run(self, wheels):
        self.drift = version_drift() or ""
        self.write()                                  # 뜰 때 한 번 — 바퀴 0
        while True:
            self.wheel += 1
            t0 = time.time()
            self.drift = version_drift() or ""
            self.stop = os.path.exists(STOP_FILE)
            self.skipped, self.skipwheel, self.cur_shown = _Why(), set(), set()
            self.reap()
            self.write()
            if not (self.stop or self.drift):
                devices = {}
                try:
                    projects = load_projects(PROJECTS_JSON, devices)
                except (OSError, ValueError, KeyError, TypeError) as e:
                    self.skipped["프로젝트 목록"] = "프로젝트 목록을 읽지 못함: %s (%s)" % (PROJECTS_JSON, e)
                    projects = []
                self.paths.update(dict(projects))
                for name, path in projects:
                    try:
                        self.visit(name, path, devices.get(name, ""))
                    except Exception as e:        # noqa: BLE001 — 한 프로젝트의 탈이 상주를 죽이지 않는다
                        self.skipped[name] = "오류: %s" % e
                    self.write()
            self.prev_shown = self.cur_shown
            self.write()
            if wheels is not None and self.wheel >= wheels:
                break
            while time.time() < t0 + RESIDENT_EVERY:
                self.reap()
                time.sleep(POLL)
        while self.running:
            self.reap()
            time.sleep(POLL)
        self.write()
        return 0


def _summary(started, results, skipped):
    now = dt.datetime.now()
    if results:
        body = "돌린 카드 %d장: %s" % (len(results), ", ".join(
            "%s/%s=%s(종료 %s)" % r for r in results))
    else:
        body = "돌 것 없음"
    line = "배차 끝 — 띄움 %s · 끝남 %s · %s" % (
        started.strftime("%Y-%m-%d %H:%M"), now.strftime("%H:%M"), body)
    if skipped:
        line += " · 건너뜀: " + ", ".join("%s(%s)" % kv for kv in skipped.items())
    return line


def main(argv=None, wheels=None):
    argv = list(argv or [])
    if "--상주-켜기" in argv:
        return 상주_켜기()
    if "--상주-끄기" in argv:
        return 상주_끄기()
    if "--상주" in argv:
        return _Resident().run(wheels)         # `wheels` 는 시험 전용 — 바퀴를 그만큼 돌고 러너를 기다린 뒤 돌아온다
    started = dt.datetime.now()
    try:
        devices = {}
        projects = load_projects(devices=devices)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print("배차를 시작하지 않는다 — 프로젝트 목록을 읽지 못함: %s (%s)" % (list_path(), e))
        return 2

    handed = set()       # 이번 배차가 넘긴 카드 경로 — 러너가 시작 전에 끝나 대기로 남아도 다시 안 넘긴다
    running = {}         # 카드 경로 → (프로젝트 이름, 프로세스, 카드)
    dead = set()         # 이번 배차에서 더 보지 않는 프로젝트(토큰 없음)
    skipped = {}         # 프로젝트 이름 → 마지막 까닭
    results = []
    shown = set()        # 이미 찍은 승인 거부·승격 실패 줄 — 같은 줄은 배차 한 번에 한 번
    paths = dict(projects)
    cap = max_parallel()   # 배차가 시작할 때 한 번 읽는다(경고도 한 번)
    while True:
        for key, (name, proc, card) in list(running.items()):
            rc = proc.poll()
            if rc is not None:
                del running[key]
                status = board.front_matter(card["path"]).get("상태", "")
                results.append((name, card["file"], status, rc))
                backlog.tidy(paths[name], 실행=True)     # 병합된 카드를 완료로, 끝 보고 방아쇠(E-2 F-6)
                endreport.auto(paths[name])
        for name, path in projects:
            if len(running) >= cap:
                break
            if name in dead:
                continue
            for line in promote(path, name):
                if line not in shown:
                    shown.add(line)
                    print(line)
            cards, why = read_cards(path)
            card = None
            if not why:
                card, why = pick_next(cards, handed, set(running), held(path), lists(path),
                                      device=devices.get(name, ""), shared=shared_files(path))
            again = False
            if card is None and not (why and why not in (None, "진행 카드 있어 건너뜀", OTHER_DEVICE, WAIT_PR)):
                card = pick_rehandoff(path, cards, handed, set(running), device=devices.get(name, ""))
                again = card is not None
                if again:
                    why = None
            if why:
                skipped[name] = why
                continue
            skipped.pop(name, None)
            if card is None:
                continue
            if not check_account(path, card["path"]):
                dead.add(name)
                skipped[name] = "계정 토큰 없음"
                continue
            if again:
                n = _count(card) + 1     # 띄우기 전에 올린다 — 못 쓰면 상한이 안 먹으므로 띄우지 않는다
                if not _set_head(card["path"], 재인계=n):
                    print("재인계 안 함 — %s/%s: 재인계 횟수를 쓰지 못함" % (name, card["file"]))
                    continue
                print("재인계 — %s/%s: 기준 가지가 고쳐짐, %d회째" % (name, card["file"], n))
            handed.add(card["path"])
            args = [sys.executable, RUNNER, path, card["path"]] + (["--resume"] if again else [])
            env = dict(os.environ, STOP_LOOP_DEVICE=this_machine() or devices.get(name, ""))     # 러너가 머리말에 돈 기계로 쓴다
            running[card["path"]] = (name, subprocess.Popen(args, env=env), card)
        if not running:
            break
        time.sleep(POLL)
    print(_summary(started, results, skipped))
    return 0


if __name__ == "__main__":
    runner.CARD_PUSH_DIR = os.path.expanduser("~/.stop-loop/card-push")   # 명령으로 띄울 때만 켠다
    sys.exit(main(sys.argv[1:]))
