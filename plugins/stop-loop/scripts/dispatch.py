#!/usr/bin/env python3
"""배차 — 한 번 띄우면 빌 때까지 도는 대기열 집기 (SC-01 E-3).

프로젝트 목록(hq 는 `projects.yaml` 의 `workflow: stop-loop`, 플러그인은 `~/.stop-loop/projects.json`)의 프로젝트마다 `docs/backlog/*.md` 카드를 읽어,
`상태: 대기` 카드를 러너(`bin/runner.py`)에 한 장씩 넘긴다. 동시 상한은 CAP(기계 전체). 같은 프로젝트에서는
선행이 완료된 시나리오 카드끼리 함께 돌고, 시나리오에 묶이지 않은 카드는 혼자 돈다.
더 돌 것이 없으면 한 줄 요약을 찍고 끝난다.

    python3 bin/dispatch.py
"""
import datetime as dt
import json
import os
import re
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
CAP = 2          # 이 기계의 동시 상한. ponytail: 설정 열쇠로 빼지 않는다(04 판정)
REHANDOFF_CAP = 3  # 기준 가지 탓으로 멈춘 카드를 카드당 다시 인계하는 횟수. CAP 과 독립이다
POLL = 1.0       # 실행이 끝났는지 보는 간격(초)

gh_token = runner.gh_token      # 시험이 바꿔 끼운다


def list_path():
    """플러그인 안(`runner.PLUGIN`)이면 목록 파일, hq 면 projects.yaml. 호출 때 정한다 — 시험이 바꿔 끼운다."""
    return PROJECTS_JSON if runner.PLUGIN else PROJECTS_YAML


def load_projects(path=None):
    """(이름, 펼친 경로) 를 적힌 순서대로. `.json` 이면 목록 파일의 전부, 아니면 projects.yaml 의
    `workflow` 가 정확히 stop-loop 인 것. `yaml` 은 YAML 을 읽을 때만 가져온다 — 플러그인은 표준 라이브러리만 쓴다.
    읽기 실패는 OSError, 내용 오류는 ValueError, 모양 오류는 KeyError·TypeError."""
    path = path or list_path()
    with open(path, encoding="utf-8") as f:
        if path.endswith(".json"):
            return [(p["name"], os.path.expanduser(p["path"])) for p in json.load(f)["projects"]]
        import yaml
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(e)
    return [(p["name"], os.path.expanduser(p["path"]))
            for p in data["projects"] if p.get("workflow") == "stop-loop"]


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
        except (OSError, UnicodeDecodeError):
            return [], "카드를 읽지 못함: %s" % fname
        cards.append({"file": fname, "path": path, **{k: head.get(k, "")
                      for k in ("상태", "시나리오", "요소", "선행")}})
    return cards, None


def _ready(card, cards, lists=None):
    """앞선 카드(`선행:` 의 요소 번호)가 같은 시나리오 안에서 모두 완료인가.

    lists = {시나리오 번호: 「이어지는 이슈」 표의 카드 이름 집합 또는 None}. 표가 있으면 표 밖 카드(회고 카드)는 선행을 막지 않는다.
    """
    raw = card["선행"].strip()
    if raw in ("", "—", "-"):
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


def pick_next(cards, handed, live=frozenset(), held=frozenset(), lists=None):
    """(집을 카드 또는 None, 건너뛴 까닭 또는 None).

    live = 이번 배차가 넘겨 아직 끝나지 않은 카드 경로. 돌고 있는 카드 = 상태 진행 또는 live.
    held = 승인되지 않은 시나리오 번호 — 그 시나리오의 대기 카드는 집지 않는다(E-2).
    lists = 선행 판정에 쓰는 시나리오별 카드 목록(`_ready`).
    묶이지 않은 카드는 혼자 돈다 — 돌고 있으면 아무것도 안 집고, 다른 카드가 돌면 안 집힌다.
    """
    running = [c for c in cards if c["상태"] == "진행" or c["path"] in live]
    skip = "진행 카드 있어 건너뜀"
    if any(not _bound(c) for c in running):
        return None, skip
    ready = [c for c in cards if c["상태"] == "대기" and c["path"] not in handed and _ready(c, cards, lists)
             and c["시나리오"] not in held and (not running or _bound(c))]
    if ready:
        return min(ready, key=lambda c: c["file"]), None
    return (None, skip) if running else (None, None)


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
    for cmd, rc in re.findall(r"^- 기계 검사 실패: (.+) \(rc=(-?\d+)\)\s*$", text, re.M):
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


def pick_rehandoff(project_path, cards, handed, live):
    """다시 인계할 카드 또는 None. 상한 도달 카드는 `차례` 를 비워 사람 차례로 올린다. 본 카드는 handed 에 넣는다."""
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


def main(argv=None):
    started = dt.datetime.now()
    try:
        projects = load_projects()
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
            if len(running) >= CAP:
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
                card, why = pick_next(cards, handed, set(running), held(path), lists(path))
            again = False
            if card is None and not (why and why not in (None, "진행 카드 있어 건너뜀")):
                card = pick_rehandoff(path, cards, handed, set(running))
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
            running[card["path"]] = (name, subprocess.Popen(args), card)
        if not running:
            break
        time.sleep(POLL)
    print(_summary(started, results, skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
