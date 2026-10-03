"""STOP-LOOP 엔진 핵심 — 전이표 해석 · 상한 · 감지 · 상태 저장 (표준 라이브러리만).

전이표의 표준 정의는 문서다(SPEC-STATE.md §3). 이 모듈은 같은 표를 데이터로
갖고, test_stoploop 의 동기화 검사가 문서와 코드가 어긋나면 실패한다 —
표를 고치면 양쪽을 함께 고쳐야 커밋이 통과한다.

러너 데몬(폴링·gh·워크트리·세션 기동)은 이 모듈을 부르는 껍데기로 따로 온다.
"""
import json
import os
import re
import tempfile

STATES = ["queued", "running", "blocked", "review", "closed"]
TERMINAL = {"closed"}
INITIAL = "queued"

# (from, event, to, guard 이름 또는 None) — SPEC-STATE §3 과 1:1
TRANSITIONS = [
    ("queued",  "PICK",        "running", "can_pick"),
    ("running", "STAGE_DONE",  "running", "artifact_exists"),
    ("running", "GATE_FAIL",   "running", "gate_retry_available"),
    ("running", "STUCK",       "running", "stuck_retry_available"),
    ("running", "HANDOFF",     "review",  "dod_pass"),
    ("running", "BREAK",       "blocked", None),
    ("running", "USAGE_LIMIT", "queued",  None),
    ("blocked", "RESUME",      "running", "breaker_cleared"),
    ("blocked", "ABANDON",     "closed",  None),
    ("review",  "MERGED",      "closed",  None),
    ("review",  "PR_CLOSED",   "blocked", None),
]

# 엔진 고정 상한 (01 §4 — 설정으로 열지 않는다)
GATE_FAIL_CAP = 2      # 같은 게이트 연속 실패
REVIEW_ROUND_CAP = 3   # 새 지적이 나오는 검토 회차
STUCK_CAP = 2          # 같은 지점 막힘 — 1회째 접근 교체, 2회째 중단


# ── 전이 ────────────────────────────────────────────────────────────

def transition(state, event, guards=None):
    """(성공 여부, 다음 상태, 막은 것) 을 돌려준다. guards 는 {이름: bool}."""
    guards = guards or {}
    for frm, ev, to, guard in TRANSITIONS:
        if frm != state or ev != event:
            continue
        if guard is not None and not guards.get(guard, False):
            return (False, state, guard)
        return (True, to, None)
    return (False, state, "no_transition")


# ── 상한 계산 (counters 는 state.json 의 counters 그대로) ───────────

def on_gate_fail(counters, gate):
    """게이트 실패. 'retry'(같은 작업자가 고침) 또는 'break'(BREAK 발화)."""
    fails = counters.setdefault("gateFails", {})
    fails[gate] = fails.get(gate, 0) + 1
    return "retry" if fails[gate] < GATE_FAIL_CAP else "break"


def on_gate_pass(counters, gate):
    """통과하면 그 게이트의 연속 실패가 0 으로 돌아간다."""
    counters.setdefault("gateFails", {})[gate] = 0


def on_review_round(counters, new_findings, repeated):
    """검토 회차 판정. repeated(같은 지적 재발)는 1회로 즉시 'break'.
    새 지적이 있으면 회차를 소모하고, 상한을 넘기면 'break'.
    지적 0건이면 'pass'."""
    if repeated:
        return "break"
    if new_findings == 0:
        return "pass"
    counters["reviewRounds"] = counters.get("reviewRounds", 0) + 1
    return "continue" if counters["reviewRounds"] <= REVIEW_ROUND_CAP else "break"


def on_stuck(counters):
    """같은 지점 막힘. 1회째 'swap'(워킹트리 되돌리고 접근 교체), 2회째 'break'."""
    counters["stuck"] = counters.get("stuck", 0) + 1
    return "swap" if counters["stuck"] < STUCK_CAP else "break"


# ── 진동·퇴행 감지 (rounds 는 state.json 의 rounds 그대로) ──────────

def detect_oscillation(rounds):
    """같은 diff 해시가 두 번 나타나면 진동이다."""
    seen = set()
    for r in rounds:
        h = r.get("diffHash")
        if h in seen:
            return True
        seen.add(h)
    return False


def detect_regression(prev_round, curr_round):
    """직전 회차에 통과하던 테스트가 이번에 실패하면 그 목록을 돌려준다."""
    prev_passed = set(prev_round.get("testsPassed", []))
    curr_failed = set(curr_round.get("testsFailed", []))
    return sorted(prev_passed & curr_failed)


# ── 상태 저장 (러너만 쓴다 — 원자적 쓰기) ───────────────────────────

def write_state(path, state):
    """임시 파일에 쓰고 rename — 크래시가 나도 반쪽 파일이 남지 않는다."""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_state(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def append_event(path, event):
    """events.jsonl 에 한 줄 추가. 닫힌 어휘만 — 자유 텍스트는 넣지 않는다."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


# ── 도달성 검사 (전이표를 고칠 때마다 돈다) ─────────────────────────

def check_reachability(transitions=None, states=None):
    """갇히는 상태·도달 불능 상태를 문제 목록으로 돌려준다. 빈 목록이 정상."""
    transitions = TRANSITIONS if transitions is None else transitions
    states = STATES if states is None else states
    problems = []

    for s in states:
        if s in TERMINAL:
            continue
        if not any(t[0] == s for t in transitions):
            problems.append("갇힘: %s 에서 나가는 전이가 없다" % s)

    reachable = {INITIAL}
    changed = True
    while changed:
        changed = False
        for frm, _ev, to, _g in transitions:
            if frm in reachable and to not in reachable:
                reachable.add(to)
                changed = True
    for s in states:
        if s not in reachable:
            problems.append("도달 불능: %s 로 가는 전이가 없다" % s)
    return problems


# ── 문서 동기화 — 표가 표준 정의다 ──────────────────────────────────

_EVENT = re.compile(r"^[A-Z][A-Z_]*$")


def parse_spec_transitions(md_path):
    """SPEC-STATE.md §3 표에서 (from, event, to) 집합을 뽑는다."""
    triples = set()
    with open(md_path, encoding="utf-8") as f:
        for line in f:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 5 and cells[0] in STATES and _EVENT.match(cells[1]):
                triples.add((cells[0], cells[1], cells[2]))
    return triples
