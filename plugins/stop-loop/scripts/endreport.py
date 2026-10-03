#!/usr/bin/env python3
"""끝 보고 — 시나리오의 요소 카드가 모두 끝났을 때 완료 기준을 기계가 돌려 보고 보고서를 쓴다.

  python3 bin/endreport.py <프로젝트 경로> <SC-nn>

`docs/plan/끝보고-SC-nn.md` 하나만 쓴다(있으면 덮어쓴다). 시나리오·색인·카드는 바꾸지 않는다 —
시나리오를 「완료」로 바꾸는 것은 사람이다. 종료 코드: 0 썼다 · 1 쓰지 않았다(까닭을 찍음) · 2 인자 오류.
돌리는 것은 시나리오 「어떻게 확인하나」 칸에 백틱으로 적힌 명령뿐이다.
"""
import datetime as dt
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board  # noqa: E402
import ledger  # noqa: E402
import runner  # noqa: E402

TIMEOUT = 600       # 명령 하나의 시간 상한(초). 끝 보고 전체의 상한은 두지 않는다
_절 = ("인수 조건", "완료 기준")
_백틱 = re.compile(r"`([^`]+)`")
SIGNALS_DIR = os.path.expanduser("~/.stop-loop/signals")   # 시험이 바꿔 끼운다 (board.STATUS_DIR 과 같은 방식)
HQ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 이 저장소의 카드가 「워크플로우 개선 카드」다
PROJECTS_YAML = os.path.join(HQ, "projects.yaml")
_초 = "%Y-%m-%dT%H:%M:%S"
_분 = "%Y-%m-%d %H:%M"
_파일 = re.compile(r"^\.workflow/([^/]+)/(07-changes|03-answers|07-answers)\.md$")


def _명령인가(글):
    try:
        w = shlex.split(글)
    except ValueError:
        return False
    return (len(w) >= 2 and "/" not in w[0] and shutil.which(w[0]) is not None
            and not re.search(r"<[^>]*>", 글))


def read_criteria(text):
    """`[(AC-n, 어떻게 확인하나 글, [돌릴 명령, …])]` — 시나리오의 「인수 조건」(또는 「완료 기준」) 표 순서대로."""
    out, on = [], False
    for l in text.splitlines():
        if l.startswith("## "):
            on = l[3:].strip() in _절
            continue
        if not on or not l.strip().startswith("|"):
            continue
        c = board._칸(l)
        if len(c) >= 3 and re.fullmatch(r"AC-\d+", c[0]):
            out.append((c[0], c[2], [m for m in _백틱.findall(c[2]) if _명령인가(m)]))
    return out


def check(criterion, project, run=subprocess.run, timeout=None):
    """완료 기준 하나를 돌려 `(결과, 근거)`. 결과: 통과 · 실패 · 돌리지 못함 · 기계가 확인하지 못함."""
    timeout = TIMEOUT if timeout is None else timeout
    cmds = criterion[2]
    if not cmds:
        return "기계가 확인하지 못함", "「어떻게 확인하나」 칸에 돌릴 명령이 없다 — 사람이 확인한다"
    codes, notes, 못 = [], [], []
    for c in cmds:
        try:
            r = run(shlex.split(c), cwd=project, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            못.append("시간 상한 %s초" % timeout)
            notes.append("`%s` 시간 상한 %s초를 넘김" % (c, timeout))
            continue
        except OSError as ex:
            못.append("띄우지 못함")
            notes.append("`%s` 띄우지 못함: %s" % (c, ex))
            continue
        codes.append(r.returncode)
        tail = " ⏎ ".join(((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-5:])
        notes.append("`%s` 종료 코드 %s%s" % (c, r.returncode, " — " + tail if tail else ""))
    결과 = "실패" if any(x != 0 for x in codes) else ("돌리지 못함" if 못 else "통과")
    return 결과, " / ".join(notes)


def _git_text(project, 커밋, 경로):
    r = ledger._git(project, "show", "%s:%s" % (커밋, 경로))
    return r.stdout if r.returncode == 0 else None


def history_items(project, card_files):
    """`({카드: [참고 사항, …]}, [PM 결정 줄, …])`. 실행 폴더가 소각돼도 HEAD 이력의 마지막 판을 읽는다.
    이력을 못 읽으면 None(「없음」과 다르다)."""
    if not ledger._is_git_repo(project):
        return None
    r = ledger._git(project, "log", "--diff-filter=AM", "--name-only", "--pretty=format:@@%H", "--", ".workflow")
    if r.returncode != 0:
        return None
    latest, commit = {}, None
    for l in r.stdout.splitlines():     # 새 커밋부터 나온다 — 경로마다 처음 나온 것이 마지막 판
        if l.startswith("@@"):
            commit = l[2:]
        elif _파일.match(l):
            latest.setdefault(l, commit)
    notes, pm = {}, []
    for card in card_files:
        name = os.path.splitext(card)[0]
        notes[card] = []
        for path in sorted(latest):
            if ledger._issue_name(path) != name:
                continue
            text = _git_text(project, latest[path], path)
            if text is None:
                return None
            if path.endswith("07-changes.md"):
                with tempfile.TemporaryDirectory() as d:
                    with open(os.path.join(d, "07-changes.md"), "w", encoding="utf-8") as f:
                        f.write(text)
                    for src, item in runner.human_items(d):
                        if src == "07" and item not in notes[card]:
                            notes[card].append(item)
            elif path.endswith("07-answers.md"):
                pm += _pm_lines07(card, text)
            else:
                pm += _pm_lines(card, text)
    return notes, pm


def _pm_lines(card, text):
    out, on = [], False
    for l in text.splitlines():
        if l.startswith("## "):
            on = l[3:].strip() == "답"
            continue
        c = board._칸(l) if on and l.strip().startswith("|") else []
        if len(c) >= 4 and c[1] in ("[답함]", "[기본안]"):
            out.append("%s · 03a · %s — %s — 근거: %s" % (card, c[0], c[2], c[3]))
    return out


def _pm_lines07(card, text):
    out, on = [], False
    for l in text.splitlines():
        if l.startswith("## "):
            on = l[3:].strip() == "답"
            continue
        c = board._칸(l) if on and l.strip().startswith("|") else []
        if len(c) >= 4 and c[1] == "[답함]":
            out.append("%s · 07 · %s — %s — 근거: %s" % (card, c[0], c[2], c[3]))
        elif len(c) >= 4 and c[1] in ("[사람 결정]", "[사람 몫]"):
            out.append("%s · 07 · %s — 사람 결정 — 올린 까닭: %s" % (card, c[0], c[3]))
    return out


def _칸글(s):
    return " / ".join(str(s).splitlines()).replace("|", "\\|")

def _시각(글):
    try:
        return dt.datetime.fromisoformat(글)
    except (ValueError, TypeError):
        return None


def _동시(runs):
    """실행 `(저장소, 카드, 시작, 끝)` 들이 같은 순간에 돌던 서로 다른 저장소 수의 최댓값."""
    return max((len({r[0] for r in runs if r[2] <= t <= r[3]}) for t in (r[2] for r in runs)), default=0)


def _추세_읽기(d):
    """`([(저장소, 카드, 어림 시작, 끝), …], [읽지 못한 줄 설명, …])`. 신호 폴더가 없으면 None."""
    if not os.path.isdir(d):
        return None
    runs, 못 = [], []
    for f in sorted(os.listdir(d)):
        if not f.endswith("__trend.jsonl"):
            continue
        with open(os.path.join(d, f), encoding="utf-8", errors="replace") as fh:
            for n, l in enumerate(fh, 1):
                if not l.strip():
                    continue
                try:
                    j = json.loads(l)
                    끝 = _시각(j["when"])
                except (ValueError, KeyError, TypeError):
                    끝 = None
                if 끝 is None:
                    못.append("%s %d번째 줄 — 읽지 못함" % (f, n))
                    continue
                try:    # stages 가 비거나 ms 가 없으면 걸린 시간 0 — 끝난 때 한 순간
                    ms = sum(v["ms"] for v in j["stages"].values() if isinstance(v, dict) and isinstance(v.get("ms"), (int, float)))
                except (AttributeError, KeyError, TypeError):
                    ms = 0
                runs.append((f[:-len("__trend.jsonl")], str(j.get("issue", "")), 끝 - dt.timedelta(milliseconds=ms), 끝))
    return runs, 못


def _저장소_경로(project):
    """`{폴더 이름: 경로}` — 받은 프로젝트, 등록된 stop-loop 프로젝트, hq. 폴더가 실재하는 것만."""
    경로 = [project]
    try:
        import dispatch  # 늦게 가져온다 — dispatch 가 맨 위에서 endreport 를 가져온다
        경로 += [p for _, p in dispatch.load_projects(None if runner.PLUGIN else PROJECTS_YAML)]
    except Exception:  # noqa: BLE001 — 등록 목록을 못 읽으면 그 저장소 실행은 「경로를 모름」으로 나온다
        pass
    out = {}
    for p in 경로 + [HQ]:
        p = os.path.abspath(p)
        if os.path.isdir(p) and p not in out.values():
            out.setdefault(os.path.basename(p), p)
    return out


def _목표지표(project, no, rows, now):
    """끝 보고의 「## 목표 지표」 절 줄 목록 (SC-01 E-7). 0 은 0 으로, 셈하지 못한 것은 「셈하지 못함」으로 적는다."""
    plan = os.path.join(project, "docs", "plan")
    try:
        with open(os.path.join(plan, "사람손-%s.md" % no), encoding="utf-8") as f:
            손 = [l for l in f.read().splitlines() if l.startswith("- ")]
    except OSError:
        손 = []
    칸 = [l[2:].split(" · ") for l in 손]
    ps = next((_시각(c[0]) for c in 칸 if len(c) >= 3 and c[2] == "승인"), None)
    if ps is None:
        return ["## 목표 지표", "", "시나리오 기간을 정할 수 없어(승인 기록 없음) 지표를 셈하지 않음"]
    목록 = 손 + ([] if any(len(c) >= 2 and c[1] == "끝" for c in 칸) else
                ["- 끝(최종 판정) 대기 — 끝 보고를 만든 때 아직 기록되지 않음, 횟수에 넣지 않음"])
    paths = _저장소_경로(project)
    got = _추세_읽기(SIGNALS_DIR)
    못 = []
    if got is None:
        ns = "셈하지 못함(신호 폴더 없음: %s)" % SIGNALS_DIR
        동시 = 밖 = ns
    else:
        all_runs, 못 = got[0], list(got[1])
        runs = [r for r in all_runs if r[2] <= now and r[3] >= ps]
        동시 = str(_동시(runs))
        cards = {n: board.read_cards(os.path.join(d, "docs", "backlog")) for n, d in paths.items()}
        묶임없음, 모름 = {n: set() for n in paths}, {n: 0 for n in paths}
        for repo, card, _, 끝 in runs:
            끝글 = 끝.strftime(_초)
            if repo not in paths:
                못.append("%s · %s · %s — 저장소 경로를 모름" % (repo, card, 끝글))
            elif not any(c["file"] == card + ".md" for c in cards[repo]):
                못.append("%s · %s · %s — 카드 없음" % (repo, card, 끝글))
            elif next(c for c in cards[repo] if c["file"] == card + ".md")["시나리오"] == "":
                묶임없음[repo].add(card)
        pm = ps.replace(second=0, microsecond=0)
        for n in paths:     # 추세 줄을 남기지 않은 실행 — 카드 머리말 `시작:` 으로 찾는다
            for c in cards[n]:
                stem = c["file"][:-3]
                s = None
                if c["시작"]:
                    try:
                        s = dt.datetime.strptime(c["시작"], _분)
                    except ValueError:
                        못.append("%s · %s · 시작 %s — 시작 시각을 읽지 못함" % (n, stem, c["시작"]))
                        continue
                if not ((s and pm <= s <= now) or c["상태"] == "진행"):
                    continue
                if not any(r[0] == n and r[1] == stem and (s is None or r[3].replace(second=0, microsecond=0) >= s)
                           for r in all_runs):
                    모름[n] += 1
                    못.append("%s · %s · 시작 %s · 상태 %s — 추세 줄 없음(끝까지 가지 않았거나 아직 도는 실행)"
                              % (n, stem, c["시작"] or "—", c["상태"]))
        hq = os.path.basename(os.path.abspath(HQ))
        밖 = " · ".join("%s %d%s%s" % (n, len(묶임없음[n]),
                                       "(그 가운데 워크플로우 개선 %d)" % len(묶임없음[n]) if n == hq else "",
                                       "(셈하지 못함 %d)" % 모름[n] if 모름[n] else "") for n in paths)
    통과 = sum(1 for r in rows if r[1] == "통과")
    return (["## 목표 지표", "",
             "기간: %s(첫 승인) 부터 %s(끝 보고를 만든 때) 까지 — 최종 판정 전이라 끝 보고를 만든 때에서 자른다"
             % (ps.strftime(_초), now.strftime(_초)),
             "실행 시작은 끝난 때에서 단계 시간 합을 뺀 어림값이다(보류로 쉰 시간이 빠져 늦게 잡힌다). 기간과 겹친 실행을 센다. "
             "추세 줄이 없는 실행은 세지 않고 「셈하지 못함」에 적는다.", "",
             "| 지표 | 값 |", "|---|---|",
             "| 사람 손 | %d (목표 2) |" % len(손),
             "| 완료 기준 통과 | %d / %d |" % (통과, len(rows)),
             "| 동시에 돌던 프로젝트 수(가장 많을 때) | %s |" % 동시,
             "| 시나리오에 묶이지 않은 채 돈 카드 | %s |" % 밖,
             "", "### 사람 손 목록", ""] + 목록 + ["", "### 셈하지 못함", ""] + (못 or ["없음"]))


def _개선(project, no, 손수):
    """끝 보고의 「## 워크플로우 개선」 절 줄 목록 (SC-01 E-8). 러너가 모은 `개선모음-<no>.md` 를 읽어 표로 싣는다.
    멈춤 예 항목은 목표 지표의 「사람 손」 값과 이어 보이고, 판정이 없으면 「판정 대기」."""
    head = ["## 워크플로우 개선", ""]
    plan = os.path.join(project, "docs", "plan")
    # 지금 모양 파일 하나와 카드별 파일(`개선모음-<no>-*.md`)을 함께 읽는다. `-` 가 붙어야 SC-010 이 섞이지 않는다.
    paths = [os.path.join(plan, "개선모음-%s.md" % no)] + sorted(
        glob.glob(os.path.join(glob.escape(plan), "개선모음-%s-*.md" % glob.escape(no))))
    secs, merged = {}, {}      # merged: (대상, 증상) → [횟수, 제안, 판, 멈춤, [나온 곳], 판정, 반영]
    seen = False
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            continue
        except OSError:
            return head + ["개선 모음을 읽지 못함"]
        seen = True
        cur = None
        for l in text.splitlines():
            if l.startswith("## "):
                cur = l[3:].strip()
            elif cur and l.startswith("- "):
                secs.setdefault(cur, []).append(l[2:])
                if cur == "개선 항목":
                    c = l[2:].split(" · ")
                    if len(c) == 9:
                        n = int(c[2]) if c[2].isdigit() else 1
                        hit = merged.setdefault((c[0], c[1]), [0, c[3], c[4], "아니오", [], "—", "—"])
                        hit[0] += n
                        if c[5] == "멈춤 예":
                            hit[3] = "예"
                        hit[4] += [x for x in c[6][5:].split(", ") if x not in hit[4]]
                        if hit[5] == "—":
                            hit[5] = c[7][9:]
                        if hit[6] == "—":
                            hit[6] = c[8][6:]
    if not seen:
        return head + ["모인 개선 없음"]
    rows = []
    for (대상, 증상), (n, 제안, 판, 멈춤, 곳, 판정, 반영) in merged.items():
        rows.append("| %s | %s | %d | %s | %s | %s | %s | %s | %s | %s |" % (
            대상, 증상, n, 제안, 판, 멈춤, ", ".join(곳),
            "사람 손 %d (목표 2)" % 손수 if 멈춤 == "예" else "—",
            "판정 대기" if 멈춤 == "예" and 판정 == "—" else 판정, 반영))
    밖, 없음 = len(secs.get("형식 밖", [])), len(secs.get("회고 없음", []))
    out = head
    if rows:
        out += ["| 대상 파일 | 증상 | 되풀이 횟수 | 제안 | 판 | 멈춤 | 나온 곳 | 이어지는 지표 | 즉시 반영 판정 | 반영 여부 |",
                "|---|---|---|---|---|---|---|---|---|---|"] + rows
    elif not 밖:
        out.append("모인 개선 없음")
    return out + (["", "형식 밖 %d건" % 밖] if 밖 else []) + (["", "회고 없음 %d건" % 없음] if 없음 else [])


def _보고서(sc, cards, rows, hist, 지표, now):
    lines = ["# %s 끝 보고 — %s" % (sc["번호"], sc["이름"]),
             "%s %s" % (board._다룬카드, ", ".join(sorted(c["file"] for c in cards))),
             "만든 때: " + now.strftime(_초), "",
             "## 완료 기준 확인", "", "| 완료 기준 | 결과 | 근거 |", "|---|---|---|"]
    lines += ["| %s | %s | %s |" % (n, r, _칸글(g)) for n, r, g in rows] or \
             ["| — | 완료 기준 없음 | 시나리오에 완료 기준 표가 없거나 비어 있다 — 통과로 치지 않는다 |"]
    lines += [""] + 지표 + ["", "## 참고 사항 요약", ""]
    if hist is None:
        lines.append("이력을 읽지 못함")
    elif not any(hist[0].values()):
        lines.append("없음")
    else:
        for card, items in hist[0].items():
            if items:
                lines += ["### " + card] + ["- " + x for x in items]
    lines += ["", "## PM 결정 요약", ""]
    lines += ["이력을 읽지 못함"] if hist is None else (["- " + x for x in hist[1]] or ["없음"])
    lines += ["", "## 사람의 판정", "",
              "완료를 선언하려면 시나리오 머리 표의 상태를 사람이 「완료」로 고친다. 선언하지 않으면 까닭을 시나리오 "
              "「변경 이력」에 적고, 시나리오를 고쳐 다시 승인한다. 이 장치는 시나리오를 바꾸지 않는다.", ""]
    return "\n".join(lines)


def main(argv):
    if len(argv) != 2 or not re.fullmatch(r"SC-\d{2}", argv[1]):
        print("사용법: python3 bin/endreport.py <프로젝트 경로> <SC-nn>")
        return 2
    project, no = os.path.abspath(argv[0]), argv[1]
    plan = os.path.join(project, "docs", "plan")
    sc = next((s for s in board.read_scenarios(plan) if s["번호"] == no), None)
    if sc is None or sc["오류"]:
        print("끝 보고를 쓰지 않음: 시나리오 %s 를 읽지 못함" % no)
        return 1
    cards = [c for c in board.read_cards(os.path.join(project, "docs", "backlog")) if c["시나리오"] == no]
    if not cards:
        print("끝 보고를 쓰지 않음: %s 에 묶인 카드 없음" % no)
        return 1
    rest = [c["file"] for c in cards if c["상태"] != "완료"]
    if rest:
        print("끝 보고를 쓰지 않음: 완료가 아닌 카드 — " + ", ".join(rest))
        return 1
    with open(os.path.join(plan, sc["file"]), encoding="utf-8") as f:
        crit = read_criteria(f.read())
    rows = []
    for c in crit:
        print("%s 돌리는 중: %s" % (c[0], " ; ".join(c[2]) or "(명령 없음)"))
        rows.append((c[0],) + check(c, project))
    hist = history_items(project, sorted(c["file"] for c in cards))
    path = os.path.join(plan, board.END_REPORT % no)
    now = dt.datetime.now().replace(microsecond=0)
    지표 = _목표지표(project, no, rows, now)
    try:
        with open(os.path.join(plan, "사람손-%s.md" % no), encoding="utf-8") as f:
            손수 = sum(1 for l in f.read().splitlines() if l.startswith("- "))
    except OSError:
        손수 = 0
    text = _보고서(sc, cards, rows, hist, 지표 + [""] + _개선(project, no, 손수), now)   # 다 센 뒤에 연다 — 오류가 파일을 비우지 않게
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    n = lambda k: sum(1 for r in rows if r[1] == k)  # noqa: E731
    print("끝 보고: %s — 통과 %d · 실패 %d · 돌리지 못함 %d · 기계가 확인하지 못함 %d"
          % (path, n("통과"), n("실패"), n("돌리지 못함"), n("기계가 확인하지 못함")))
    return 0


def auto(project):
    """카드가 모두 완료인데 끝 보고가 없거나 「다룬 카드」가 지금 묶음과 다른 시나리오에 끝 보고를 쓴다.
    시도한 번호 목록을 돌려준다. 하나가 예외를 던져도 다음으로 간다(정리 `--실행` 이 부른다)."""
    project = os.path.abspath(project)
    plan = os.path.join(project, "docs", "plan")
    cards = board.read_cards(os.path.join(project, "docs", "backlog"))
    done = []
    for sc in board.read_scenarios(plan):
        mine = [c for c in cards if c["시나리오"] == sc["번호"]]
        if (sc["오류"] or sc["상태"] not in ("확정", "구현 중") or not mine
                or any(c["상태"] != "완료" for c in mine)
                or board.end_report_cards(plan, sc["번호"]) == {c["file"] for c in mine}):
            continue
        done.append(sc["번호"])
        try:
            main([project, sc["번호"]])
        except Exception as ex:  # noqa: BLE001 — 끝 보고 하나의 오류가 정리를 멈추지 않는다
            print("끝 보고를 쓰지 못함: %s — %s" % (sc["번호"], ex))
    return done


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
