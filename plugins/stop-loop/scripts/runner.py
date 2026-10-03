#!/usr/bin/env python3
"""STOP-LOOP 러너 — 이슈 하나를 열 스테이지에 태운다.

스테이지마다 새 세션(`claude -p --agent`)을 띄운다. 같은 대화를 이어가면
검증자가 작업자의 사고 과정을 보게 되어 검증이 변호가 된다.

판정은 세션이 아니라 러너가 한다. 세션의 「끝냈다」는 근거가 아니고,
산출물 파일의 실재와 그 안의 판정 줄이 근거다 (SPEC-STATE §2-1).

  python3 bin/runner.py <프로젝트경로> <이슈파일> [--dry-run] [--from 05]

이슈파일은 러너의 입력이다. 대기 카드를 집어 러너를 띄우는 일은 배차
`bin/dispatch.py` 가 맡는다 — 여기서는 이슈 본문 하나를 파일로 받아 파이프라인만 돌린다.
"""
import argparse
import datetime as dt
import glob
import hashlib
import os
import re
import json
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "workflows", "stop-loop", "engine"))
import stoploop  # noqa: E402  — 전이·상한 판정은 엔진이 한다

CLAUDE = os.path.expanduser("~/.local/bin/claude")
HQ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _plugin_name(root):
    """뿌리의 `.claude-plugin/plugin.json` 이 말하는 플러그인 이름. 없거나 깨졌으면 None."""
    try:
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            name = json.load(fh).get("name")
    except (OSError, ValueError, AttributeError):
        return None
    return name if isinstance(name, str) and name else None


# 플러그인 안에서 돌면 에이전트는 `<플러그인>:<이름>` 으로 불린다. 접두사는 `--agent` 와 미리보기에만
# 붙인다 — 모델 조회·`Loop-Agent:` 표식·STAGES 는 맨 이름이다(models.json 열쇠가 맨 이름).
PLUGIN = _plugin_name(HQ)


def plugin_version(config_dir):
    """설정 폴더의 설치 기록에서 이 플러그인의 지금 판 문자열. 못 읽으면 None — 예외를 내지 않는다.

    플러그인이 아니면(PLUGIN 이 None) 읽지 않는다. 같은 이름의 항목이 여럿이면 열쇠 이름순 첫 것의
    첫 기록만 본다. 읽기 실패는 「대조 안 함」이지 멈춤이 아니다(04 판정).
    """
    if not PLUGIN:
        return None
    try:
        path = os.path.join(os.path.expanduser(config_dir), "plugins", "installed_plugins.json")
        with open(path, encoding="utf-8") as fh:
            plugins = json.load(fh)["plugins"]
        key = next(k for k in sorted(plugins) if k.split("@", 1)[0] == PLUGIN)
        rec = plugins[key][0]
        ver, sha = rec.get("version"), rec.get("gitCommitSha")
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, StopIteration):
        return None
    ver = ver if isinstance(ver, str) and ver else None
    sha = sha[:12] if isinstance(sha, str) and sha else None
    return "%s (%s)" % (ver, sha) if ver and sha else (ver or sha)


def agent_ref(agent):
    return "%s:%s" % (PLUGIN, agent) if PLUGIN and agent else agent

# 저장소 밖 요약 폴더 (ISSUE-status-publish-2026-09-17). 가져오기만 해서는 꺼져 있다 —
# 시험이 main() 을 60번 넘게 불러도 실제 폴더에 가짜 실행을 남기지 않게, 맨 아래
# 진입점 블록이 명령으로 띄울 때만 켠다.
STATUS_DIR = None
# 저장소 밖 신호 폴더 (ISSUE-signals-and-config-2026-09-20). 소각에서 살아남아야 하는 것 —
# 선택 사항 누적 파일과 측정 추세 — 이 산다. 요약 폴더와 같은 이유로 가져오기만 해서는 꺼져 있다.
SIGNALS_DIR = None
# 저장소 밖 실시간 줄 폴더 (ISSUE-live-view-2026-09-20). 세션이 도는 동안의 한 줄 요약이
# 쌓인다 — 실행 디렉터리는 완주 뒤 소각되므로 그 밖이어야 한다. 요약 폴더와 같은 이유로
# 가져오기만 해서는 꺼져 있다. `_live_path` 는 `main()` 이 이 실행의 파일로 정한다.
LIVE_DIR = None
_live_path = None
LIVE_MAX = 200
# 러너가 이번 실행의 이벤트 파일 경로를 세션 훅에 넘기는 전역(ISSUE-session-command-guard).
# `call()` 인자로 넘기지 않는 이유는 인자를 늘리면 가짜 `call` 을 끼우는 시험들이
# 깨지기 때문이다 — `_live_path` 와 같은 이유, 같은 방식이다.
_events_path = None
# 판 지킴·계정 지킴이 `main()` 에서 정한 값을 `commit_stage`·`gh` 에 넘기는 전역 — `_events_path` 와
# 같은 이유(인자를 늘리면 그 함수를 가짜로 바꿔 끼우는 시험이 깨진다). `main()` 머리에서 되돌린다.
_plugin_ver = None
_gh_account = None
_gh_token = None
TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "output_tokens")
# `call()` 이 마지막 결과 줄에서 읽은 시간(ms)·토큰. `call()` 의 반환 네 값을 늘리면
# 그것을 가짜로 바꿔 끼우는 시험 파일 넷이 한꺼번에 깨져서, 반환 밖 값 하나에 남긴다.
LAST_RESULT = {}


# 인계 방식은 프로젝트가 고른다 (기록 37). 기본은 pr — 지금 hq 가 그렇게 돈다.
#   pr    가지를 올리고 PR 을 연다. gh 계정이 필요하다
#   push  가지만 올린다. PR 은 사람이 연다
#   local 원격을 건드리지 않는다. 올리는 것도 병합도 사람이 한다 — 계정이 필요 없다
HANDOFF_MODES = ("pr", "push", "local")


def load_models(project):
    """에이전트별 모델. 프로젝트 설정이 hq 전역 기본을 이긴다.

    모델을 에이전트 정의(`agents/*.md`)에 적지 않는 이유는 SPEC-AGENTS 가
    정한 것이다 — 정의는 역할만 담고 모델은 운영이 정한다. 같은 정의를
    기계·프로젝트마다 다른 모델로 돌릴 수 있어야 한다.

    키는 스테이지 번호("07")와 에이전트 이름("loop-worker") 둘 다 받고
    **스테이지가 이긴다.** loop-worker 하나가 01·03·04·05·07 을 맡으므로
    에이전트 키만으로는 설계와 구현을 가를 수 없다.
    """
    models = {}
    project_cfg = os.path.join(project, "stop-loop.config.json")
    for path in (os.path.join(HQ, "workflows", "stop-loop", "models.json"), project_cfg):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        src = d.get("models", d)              # 프로젝트 설정은 models 키 아래에 둔다
        models.update({k: v for k, v in src.items()
                       if not k.startswith("_") and isinstance(v, str)})
        if path != project_cfg:
            # 크기 칸 — 전역 파일에서만, 값이 사전인 최상위 항목(크기 → 단계·에이전트 열쇠 → 모델).
            # 프로젝트가 같은 열쇠를 적었으면 그 열쇠는 뺀다(프로젝트가 이긴다).
            by_size = {}
            for size, per in d.items():
                if size.startswith("_") or not isinstance(per, dict):
                    continue
                keep = {k: v for k, v in per.items() if isinstance(v, str)}
                if keep:
                    by_size[size] = keep
            if by_size:
                models["_bySize"] = by_size
        else:
            proj = {k for k, v in src.items() if isinstance(v, str)}
            by_size = {}
            for size, per in models.pop("_bySize", {}).items():
                kept = {k: v for k, v in per.items() if k not in proj}
                if kept:
                    by_size[size] = kept
            if by_size:
                models["_bySize"] = by_size
        if isinstance(d.get("checks"), list):
            models["_checks"] = [c for c in d["checks"] if isinstance(c, dict) and c.get("cmd")]
        if d.get("handoff") in HANDOFF_MODES:
            models["_handoff"] = d["handoff"]
        # 새 키 일곱 — 형이 틀린 키는 그 키만 없는 것으로 본다(검증기를 두지 않는다).
        # 없으면 러너가 지금 값으로 돈다. 밑줄을 붙이는 이유는 이 사전이 스테이지·에이전트
        # 이름 → 모델 표이기도 해서다.
        lim = d.get("stageLimits")
        if isinstance(lim, dict):
            ok = {k: (v["silence"], v["hard"]) for k, v in lim.items()
                  if isinstance(v, dict) and all(
                      _pos_int(v.get(x)) for x in ("silence", "hard"))}
            if ok:
                models["_stageLimits"] = ok
        if isinstance(d.get("screen"), bool):
            models["_screen"] = d["screen"]
        if d.get("reviewPolicy") in REVIEW_POLICIES:
            models["_reviewPolicy"] = d["reviewPolicy"]
        if isinstance(d.get("appStart"), str) and d["appStart"].strip():
            models["_appStart"] = d["appStart"].strip()
        tp = d.get("testPaths")
        if isinstance(tp, list) and tp and all(isinstance(w, str) and w for w in tp):
            models["_testPaths"] = list(tp)
        if _pos_int(d.get("mutantCap")):
            models["_mutantCap"] = d["mutantCap"]
        unit = d.get("unit")
        if isinstance(unit, dict) and isinstance(unit.get("docPath"), str) \
                and unit["docPath"].strip():
            models["_unitDocPath"] = unit["docPath"].strip()
        # autoMerge 는 프로젝트 설정에서만 읽는다 — 기록 41 결정 3 「프로젝트 설정으로 켠다(기본 꺼짐)」.
        if path == project_cfg and isinstance(d.get("autoMerge"), bool):
            models["_autoMerge"] = d["autoMerge"]
        # githubAccount 도 프로젝트 설정에서만 읽는다 — 전역 한 줄로 모든 저장소가 그 계정이 되지 않게.
        if path == project_cfg and isinstance(d.get("githubAccount"), str) \
                and d["githubAccount"].strip():
            models["_githubAccount"] = d["githubAccount"].strip()
    return models


REVIEW_POLICIES = ("always", "size-m", "never")   # 09 검토 정책 — 기본은 size-m


def _pos_int(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0

# 스테이지 표 — 01-loop-design §1 과 SPEC-AGENTS 의 표를 그대로 옮긴 것.
# (번호, 이름, 에이전트, 산출물, 게이트인가, 조건부인가)
STAGES = [
    ("01", "analyze",       "loop-worker",          "01-scope.md",        False, None),
    ("02", "scope-review",  "loop-verifier-scope",  "02-review.md",       True,  None),
    ("03", "clarify",       "loop-worker",          "03-questions.md",    False, None),
    ("03a", "answer",       "loop-pm",              "03-answers.md",      False, None),
    ("04", "scope-control", "loop-worker",          "04-verdict.md",      False, "scope-m"),
    ("05", "design",        "loop-worker",          "05-design.md",       False, None),
    ("05u", "design-ux",    "loop-ux",              "05-ux.md",           False, "screen"),
    ("06", "design-review", "loop-verifier-design", "06-design-review.md", True, None),
    ("07", "implement",     "loop-worker",          "07-changes.md",      False, None),
    ("08", "qa",            "loop-verifier-qa",     "08-qa.md",           True,  None),
    ("09", "review",        "loop-verifier-review", "09-review-과범위.md", True,  "size-m"),
    ("10", "handoff",       None,                   "10-handoff.md",      False, None),
    ("11", "retro",         "loop-pm",              "11-retro.md",        False, None),
]

# 위임 메시지 — 러너가 만든다. 20줄 이하 (SPEC-AGENTS 공통 계약).
# 서브에이전트는 백지에서 시작하므로 여기 없는 정보는 존재하지 않는 정보다.
DELEGATE = """[STOP-LOOP {num} {name}]

목표: {goal}

입력 파일 (이것만 읽는다):
{inputs}

산출물: {out}
  - 이 경로에 파일을 쓴다. 파일이 없으면 이 스테이지는 완료가 아니다.
  - 다음에 읽는 이가 이 대화를 못 본다는 전제로 쓴다.

요약도 쓴다: {summary}
  - 한 줄. 이 파일 전체가 요약이다(제목·머리말 없이 문장만).
  - 이 줄이 커밋 제목이 된다. 나중에 이력만 훑어 「그때 무엇을 했나」를
    판단하는 근거이므로, 한 일을 구체적으로 적는다.
  - 예: 「영향 영역 12건 판정, 미결 3건을 03 으로」
{extra}
하지 말 것:
  - 입력에 없는 것을 근거로 삼지 않는다.
  - 지시받지 않은 파일을 고치지 않는다.

완료 판정: {out} 이 존재하고 {done}
"""

# 07a — 07 작업자의 정할 것을 PM 에게 넘기는 위임의 덧붙임
DECIDE_EXTRA = """
묻는 정할 것 — 생긴 곳: {origin}
{items}

쓸 파일: {answers}
  - 「## 답」 표(정할 것 · 라벨 · 답 · 근거 또는 올린 까닭)와 「## 사람 결정」 절. 정할 것 칸은 위 항목의 한 문장
    (` — 고를 수 있는 것:` 앞부분)을 글자 그대로 옮긴다. 파일이 이미 있으면 앞 줄을 지우지 않고 더한다.
"""

GOALS = {
    "01": "이슈를 읽고 영향 영역을 판정한다. excluded 에는 반드시 이유를 적는다.",
    "02": "01 의 판정이 사실인지 저장소와 대조한다. 인용한 경로가 실재하는지 본다.",
    "03": "답에 따라 산출물이 달라지는 질문만 모은다. 없으면 「없음」이라고 적는다.",
    "03a": ("03 의 질문에 답한다. 프로젝트 문서로 답이 서는 것은 답하고, 안 서는 것만 사람 결정으로 남긴다."),
    "07a": ("07 작업자가 「## 정할 것」에 적은 결정에 답한다. 시나리오와 문서로 답이 서면 근거를 붙여 답하고, "
            "안 서는 것만 사람 결정으로 올린다."),
    "04": "진행·축소·중단 중 하나를 정한다. 절반으로 줄이면 무엇이 남는지 적는다.",
    "05": "기능 식별자를 나열하고 테스트 시나리오를 쓴다. 조합 시나리오 필수.",
    "05u": "승인된 범위의 화면 시안을 HTML 로 낸다. tokens.css 의 변수만 쓴다.",
    "06": "설계가 명세와 양방향으로 맞는지 본다. 누락과 초과를 함께 본다.",
    "07": ("실패하는 재현 테스트를 먼저 쓰고 **커밋한다** — 이 커밋이 감사 "
           "기준선이다. 실패 출력을 07-changes.md 에 붙인 뒤, 통과할 때까지 "
           "구현하고 구현을 별도 커밋으로 남긴다. 테스트 커밋이 구현 커밋보다 "
           "앞서야 한다."),
    "08": "실제로 돌려서 확인한다. 저장한 값을 다시 조회해 단언한다.",
    "09": "과범위 관점으로만 본다 — 명세에 없는 것을 더했는가.",
    "11": ("이 실행에서 무엇이 막혔고 왜 막혔는지를 산출물과 이력으로 되짚는다. "
           "규칙 후보는 초안까지만 — 규칙 파일을 고치지 않는다."),
}

VERDICT_STAGES = {"02", "06", "08", "09"}

# 회고(11)만 이 실행 전체를 되짚는다. 다른 스테이지는 앞 산출물만 보지만
# 회고는 판정 파일과 커밋 이력까지 근거로 삼는다 — 무엇이 몇 번 반려됐고
# 어디서 회차를 썼는지는 문서가 아니라 그쪽에 남는다.
RETRO_EXTRA = """
이 실행의 커밋 이력도 근거다:
  git log --grep='Loop-Issue: {issue}' --format='%s'

그리고 **지난 실행들**의 지적을 모아본다 — 이것이 3번의 유일한 근거다:
  python3 {hq}/bin/ledger.py {project}

회고문에 아래 항목을 쓴다(절 목록의 정본은 산출물 틀이다):
  1. 한 일 — 이슈가 무엇을 요구했고 무엇이 나갔는가.
  2. 막힌 자리 — 반려된 스테이지와 지적. 판정 파일의 where 를 그대로 옮긴다.
  3. 되풀이 신호 — 원장에서 횟수가 2 이상인 줄이 있는가. 이 실행의 지적이
     거기 섞여 있는가. 없으면 「없음」이라 적는다 — 없다는 것도 기록이다.
  4. 규칙 후보 — 다음에 이 지적이 안 나오려면 어느 문서에 무엇을 한 줄
     더해야 하는가. **제안까지만 쓴다. 규칙 파일을 고치지 않는다.**
     여기에는 **프로젝트 몫**만 쓴다(아래 두 몫).
  5. 워크플로우 효과 — 02·06·08·09 의 지적마다, 이 워크플로우가 없었다면
     어디서 드러났을지 셋 중 하나로 분류한다: 사람의 PR 검토 / 운영 /
     끝내 못 발견. 반려 기준(정상 운영에서 일어날 법하고, 뒤의 안전장치가 못 잡는다 —
     둘 다일 때만 반려)을 대면 선택 사항이 됐을 지적 수도 적는다.

항목은 고칠 파일의 자리로 두 몫으로 가른다:
  - **워크플로우 몫** — 고칠 파일이 loop 에이전트 정의·러너·틀·표준 구간이면 여기다.
  - **프로젝트 몫** — 나머지. 그 프로젝트에서 고친다.
  워크플로우 몫은 회고문의 「8. 워크플로우 몫」 절에 다섯 칸 표로 쓴다:
  대상 파일 · 증상(워크플로우가 어떻게 동작했나) · 되풀이 횟수(원장의 횟수) · 제안 · 판.
  「판」 칸에는 **미기록** 이라 적는다. **프로젝트 코드·경로·업무 용어를 쓰지 않는다** —
  이 절은 PR 본문으로 나가 공개 저장소 이슈의 재료가 된다. 쓸 것이 없으면 「없음」.

지표 제안도 쓴다 (회고문의 「7. 지표 제안」 절):
  - 근거는 추세 파일이다. 제안 하나에 다섯을 적는다: 지금 값 · 관측값 · 제안 값 · 근거 한 줄 · 되돌리기 기준(이 값을 바꾼 뒤 무엇이 늘면 되돌리는가).
  - 제안해도 되는 지표는 넷뿐이다: 단계 시간 상한 · 변이 상한 · 09 검토 정책 · 반려 뒤 재검증 범위.
  - 반려 뒤 재검증 범위는 설정 키가 아니라 08 검증자 정의(.claude/agents/verifier-qa.md)의 문장이다 — 사람이 고치는 자리도 그 파일이다.
  - 제안 대상이 아니다 — 쓰지 않는다: 회차 상한 · 테스트 먼저 쓰기 게이트 · 승격 규칙 · 게이트의 존재 · 단계별 모델.
  - 값을 고치지 않는다 — `stop-loop.config.json` 을 쓰지 않는다. 사람이 읽고 고친다.
  - 추세 자료가 없거나 줄이 적으면 「없음」이라고 적는다.
{signals}
판정하지 않는다 — 통과·반려는 검증자의 일이고 회고는 끝난 일을 본다.
"""

# 지난 실행들이 남긴 신호 두 파일 — 신호 폴더가 켜져 있을 때만 회고 위임문에 싣는다.
RETRO_SIGNALS = """
**지난 실행들**이 남긴 신호 — 없으면 「없음」이라 적는다:
  - 검증자 선택 사항 누적: {options}
  - 단계별 측정 추세: {trend}  (지표 제안의 근거가 이 파일이다)
"""

# 스테이지가 끝나면 완결성과 무관하게 커밋한다 — 커밋은 원장이다.
# 소각은 「작업 트리에서 치운다」이지 「이력에서 없앤다」가 아니다. 에이전트는
# 작업 트리를 읽지 git 이력을 읽지 않으므로, 이력에 남는 것은 「낡은 문서를
# 확정 전제로 읽는」 위험을 만들지 않는다. 오히려 소각 결정이 기대던 전제
# (「그때 무엇을 했나는 git 이 보존한다」 — 01-loop-design §산출물 수명)를 채운다.
STAGE_LABEL = {
    "01": "01 분석", "02": "02 범위 검증", "03": "03 확인 요청", "03a": "03 답변", "07a": "07 결정",
    "04": "04 범위 통제", "05": "05 설계", "05u": "05 시안",
    "06": "06 설계 검토", "07": "07 구현", "08": "08 품질 확인",
    "09": "09 검토", "10": "10 인계", "11": "11 회고",
}

# 멈춤 한계 — 2026-08-31 실측에서 뽑았다. 정상 세션은 47~610초였고
# 05 설계(309줄)가 가장 길었다. 침묵은 「마지막 출력 이후」이지 총시간이 아니다.
SILENCE, HARD = 300, 1800                    # 기본 5분 / 30분

# 비용은 참고용으로만 모은다 — 멈춤은 회차로만 결정한다 (ISSUE-stop-by-count).
# 상한(STAGE_USD·ISSUE_USD)과 budget_over 는 없앴다. 수집·기록(state.json 의
# cost, 커밋 트레일러 Loop-Cost)은 그대로 둔다.
LIMITS = {                                   # 오래 걸리는 것만 따로
    "05": (600, 3600),                       # 설계 — 실측 610초
    "07": (600, 3600),                       # 구현 — 테스트를 돌린다
    "08": (900, 5400),                       # 품질 확인 — 앱 기동 + 변이
}

def stage_limits(num, models):
    """(침묵, 총시간) 초. 프로젝트 설정 `stageLimits` 가 있으면 그것, 없으면 지금 값."""
    cfg = ((models or {}).get("_stageLimits") or {}).get(num)
    return cfg if cfg else LIMITS.get(num, (SILENCE, HARD))


# 반려는 검증자가 아니라 만든 쪽으로 돌아간다 — 엔진 전이표의
# (running, GATE_FAIL, running) 이 뜻하는 것이 이것이다. 검증자를 다시 부르면
# 입력이 그대로라 같은 발견이 다시 나온다 (2026-08-31 첫 실행에서 실측).
FIXERS = {
    "02": ("loop-worker", "01-scope.md"),
    "06": ("loop-worker", "05-design.md"),
    "08": ("loop-worker", "07-changes.md"),
    "09": ("loop-worker", "07-changes.md"),
}

FIX_MSG = """[STOP-LOOP {num} 반려 수정]

검토자가 반려했다. **지적된 것만 고친다.** 주변을 정리하지 않는다.
지적이 앞서 고친 것과 같은 결과로 가는 다른 경로면, 경로를 또 막지 말고
그 결과를 지키는 한 곳에서 막는다.

읽을 것:
  - {review}  (반려 사유)
  - {target}  (고칠 산출물)
  - {issue}  (이슈 원문)

고친 뒤 {target} 을 갱신한다.
지적에 동의하지 않으면 그 이유를 {target} 에 적는다 — 무시하지 않는다.
새 기능이나 범위를 더하지 않는다.

완료 판정: {target} 이 갱신되어 있고, 각 지적에 반영이나 반론이 있다.
"""


def machine_checks(project, models_cfg):
    """린트·타입검사·테스트를 먼저 돌린다. (통과 여부, 실패 목록).

    비싼 의미 검토 **앞에** 결정론적 검사를 둔다 — 린터가 잡을 것을 opus 세션이
    잡고 있으면 안 된다. 사용자의 다른 워크플로우에 이미 선례가 있다
    (다른 워크플로우의 게이트 러너 — 「LLM 채점 전 단계로 lint·test·build 를 돌리고
    {pass, failures} 만 반환한다. 판단·채점하지 않는다」).

    명령은 2층이 정한다 — 프로젝트마다 다르기 때문이다.
      stop-loop.config.json: {"checks": [{"cmd": "npm test", "cwd": "functions"}]}
    설정이 없으면 건너뛴다. 없다고 스테이지를 세우지 않는다.
    """
    checks = models_cfg.get("_checks") or []
    failed = []
    for c in checks:
        cwd = os.path.join(project, c.get("cwd", "")) if c.get("cwd") else project
        r = subprocess.run(c["cmd"], shell=True, cwd=cwd,
                           capture_output=True, text=True)
        if r.returncode != 0:
            tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-6:]
            failed.append({"cmd": c["cmd"], "rc": r.returncode,
                           "tail": "\n".join(tail)})
    return (not failed), failed


def classify_failures(project, base_sha, failed, models_cfg):
    """실패한 검사만 기준 가지 판에서 다시 돌려 가른다 — 10 인계의 기계 검사 실패용.

    `{"base": 기준 가지에서도 실패한 항목, "card": 합친 판에서만 실패한 항목, "error": None|문자열}`.
    기준 가지 판은 `base_sha` 를 그대로 꺼낸 임시 작업 폴더(카드 변경 없음)이고 끝나면 치운다.
    판을 만들지 못하면 `error` 에 git 이 낸 마지막 줄을 담고 예외를 내지 않는다.
    """
    out = {"base": [], "card": [], "error": None}
    holder = tempfile.mkdtemp(prefix="stoploop-base-")
    base_wd = os.path.join(holder, "base")
    try:
        add = subprocess.run(["git", "worktree", "add", "-q", "--detach", base_wd, base_sha],
                             cwd=project, capture_output=True, text=True)
        if add.returncode:
            out["error"] = ((add.stderr.strip().splitlines() or add.stdout.strip().splitlines()
                             or ["git worktree add 실패"])[-1])
            return out
        # 카드 작업 폴더가 받는 준비물 링크를 기준 가지 판에도 잇는다 — 없으면 도구를 못 찾아
        # 카드 탓 실패가 「기준 가지에서도 실패」로 갈린다. 임시 폴더라 gitignore 확인은 필요 없다.
        for name in CARRY:
            src = os.path.join(project, name)
            if os.path.exists(src) and not os.path.lexists(os.path.join(base_wd, name)):
                os.symlink(os.path.realpath(src), os.path.join(base_wd, name))
        cmds = {x["cmd"] for x in failed}
        print("       기준 가지 판에서 실패한 검사 %d개를 다시 돈다" % len(cmds), flush=True)
        _, still = machine_checks(base_wd, {"_checks": [c for c in models_cfg.get("_checks") or []
                                                        if c["cmd"] in cmds]})
        also = {x["cmd"] for x in still}
        out["base"] = [x for x in failed if x["cmd"] in also]
        out["card"] = [x for x in failed if x["cmd"] not in also]
        return out
    finally:
        rm = subprocess.run(["git", "worktree", "remove", "--force", base_wd],
                            cwd=project, capture_output=True, text=True)
        shutil.rmtree(holder, ignore_errors=True)
        if rm.returncode:
            subprocess.run(["git", "worktree", "prune"], cwd=project, capture_output=True)


def history_rewritten(project, before):
    """세션 전후 HEAD 를 대조한다 — 다시 쓰이지 않았으면 None, 다시 쓰였으면 설명.

    직후 HEAD 가 직전 HEAD 와 같거나 그 위에 커밋만 더한 것이면(`before` 가
    지금 HEAD 의 조상) 다시 쓰이지 않은 것이다. 조상 관계를 판별하지 못하면
    (직전 커밋을 찾을 수 없는 등) 다시 쓰인 것으로 본다 — 기록 32 결정 1
    「이력 재작성은 반드시 막는다」를 따라 모를 때는 막는 쪽이다.

    커밋 개수가 아니라 조상 관계로 보는 이유는 되돌린 뒤 같은 개수의 새
    커밋을 쌓은 경우(실제 사건의 모양)를 개수로는 못 잡기 때문이다.
    """
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project,
                       capture_output=True, text=True)
    after = r.stdout.strip()
    if r.returncode != 0 or after == before:
        return None if r.returncode == 0 else "HEAD 를 읽지 못했다"
    ok = subprocess.run(["git", "merge-base", "--is-ancestor", before, after],
                        cwd=project, capture_output=True).returncode == 0
    if ok:
        return None
    return "HEAD %s → %s" % (before[:7], after[:7])


def tdd_evidence(project, wd, base=None, hints=None):
    """07 의 테스트 주도 증거를 확인한다 — 01-loop-design §1 「07 무결성 장치」.

    규율만 적어두면 지시일 뿐이라 지켜질 수도 안 지켜질 수도 있다.
    러너가 확인해야 게이트다.

    테스트 파일 판별은 경로를 소문자로 낮춰 낱말이 들어가는가로 근사한다. 낱말은
    `hints`(설정 `testPaths`)이고 없으면 test·spec 둘이다. `.md` 문서는 시험 파일이
    아니다 — 빼지 않으면 `SPEC-AGENTS.md` 같은 명세 문서가 시험 커밋으로 잡힌다.

    `base` 는 07 이 이 실행에서 처음 시작될 때의 HEAD 커밋 아이디다 — 그 뒤
    커밋만 07 이 만든 것으로 본다. 이어받아도 바뀌지 않으므로 시간 초과 전
    세션의 시험 커밋이 인정된다. 없으면 전체 이력을 본다.
    """
    problems = []
    words = hints or ("test", "spec")

    body = open(os.path.join(wd, "07-changes.md"), encoding="utf-8",
                errors="replace").read()
    # 빨강 증거 — 주장이 아니라 실행 출력. 실패 표식이 본문에 있어야 한다
    if not any(k in body for k in ("FAILED", "failed", "AssertionError",
                                   "Error", "빨강")):
        problems.append("빨강 증거 없음 — 실패 출력이 07-changes.md 에 없다")

    def commits(include_tests):
        out = subprocess.run(
            ["git", "log", "--format=%H", "--name-only", "--reverse"],
            cwd=project, capture_output=True, text=True).stdout
        seen, cur = [], None
        for line in out.splitlines():
            if len(line) == 40 and " " not in line:
                cur = line
            elif line.strip():
                low = line.lower()
                is_test = not low.endswith(".md") and any(w.lower() in low for w in words)
                if is_test == include_tests and cur not in seen:
                    seen.append(cur)
        return seen

    order = subprocess.run(["git", "log", "--format=%H", "--reverse"],
                           cwd=project, capture_output=True, text=True).stdout.split()
    if base and base not in order:
        # 전체 이력으로 넓히면 다른 이슈의 시험 커밋이 인정될 수 있다
        problems.append("07 시작 지점을 이력에서 찾지 못했다 — %s" % base[:7])
        return problems
    new = order[order.index(base) + 1:] if base else order   # 07 이 만든 커밋만 본다

    # 존재를 먼저 본다. 순서만 보면 「커밋이 아예 없음」이 조용히 통과한다
    # (2026-08-31 실측: 작업 트리에만 있고 커밋 0개인데 게이트가 통과했다).
    if not new:
        problems.append("이 스테이지가 만든 커밋이 없다 — 감사 기준선이 없다")
        return problems

    t = [c for c in commits(True) if c in new]
    srcs = [c for c in commits(False) if c in new]
    if not t:
        problems.append("테스트를 건드린 커밋이 없다")
    elif srcs:
        if min(order.index(c) for c in srcs) < min(order.index(c) for c in t):
            problems.append("구현 커밋이 테스트 커밋보다 앞선다 — 빨강이 먼저가 아니다")
    return problems


def has_07_pass_record(project, issue_id):
    """07 이 게이트를 통과했거나 사람이 넘겼다는 기록이 이력에 있는가.

    `commit_stage` 가 게이트 통과 뒤에만 `Loop-Gate: tdd-passed` 를 남기고, 사람은
    `Loop-Stage: 07-human` 을 남긴다. 작업자가 스스로 단 `Loop-Stage: 07` 과
    「이어받음」 빈 커밋은 그 줄이 없어 통과가 아니다.
    ponytail: 작업자가 그 줄까지 흉내 내면 속는다 — 막으려면 러너만 아는 값을 둔다. git 의 트레일러 해석은 마지막 문단만 읽어 사람 판정 커밋
    (뒤에 Co-Authored-By 문단이 붙는다)을 놓치므로 메시지 줄을 직접 읽는다.
    메시지 단위로 판단한다 — 이력을 못 읽으면 거짓이다(07 을 다시 돌리는 쪽이 안전).
    """
    r = subprocess.run(["git", "log", "--format=%B%x01"], cwd=project,
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    for msg in r.stdout.split("\x01"):
        lines = [l.strip() for l in msg.splitlines()]
        if "Loop-Issue: %s" % issue_id not in lines:
            continue
        if "Loop-Stage: 07-human" in lines:
            return True
        if "Loop-Stage: 07" in lines and "Loop-Gate: tdd-passed" in lines:
            return True
    return False


# ── 작업목록 파일 되쓰기 (기록 34) ───────────────────────────────────
# 러너가 실행하는 동안 `docs/backlog/<이슈>.md` 머리말의 상태·단계를 고친다 —
# 보드(`bin/board.py`)가 물어보지 않아도 맞게 하려는 것이다. 본체 저장소의
# 파일을 고치되 **커밋하지 않는다**. 기준 가지에 직접 쓰는 것은 막는 쪽이고
# (기록 32), 상태는 가지마다 다른 값이 아니라 지금 무슨 일이 벌어지는지다.
BACKLOG_STATE = {"queued": "대기", "running": "진행", "review": "검토",
                 "blocked": "보류", "closed": "완료"}
_backlog_path = None


def _head_value(raw):
    """머리말 값 하나. 값 뒤 빈칸 둘 이상에 이어 나오는 `#` 부터는 설명이라 버리고, 겉 따옴표를 벗긴다.

    보드(`bin/board.py` 의 `front_matter`)와 같은 규칙이다 — 둘이 다르면 화면과 실행이 어긋난다."""
    return re.sub(r"\s{2,}#.*$", "", raw).strip().strip('"').strip("'")


def size_models(models, size):
    """카드 크기가 정확히 일치하는 크기 칸의 단계·에이전트 열쇠 → 모델. 없으면 빈 사전."""
    return dict(models.get("_bySize", {}).get(size, {})) if size else {}


def backlog_get(key):
    """작업 파일 머리말의 값 하나. 없으면 빈 문자열."""
    if not _backlog_path or not os.path.exists(_backlog_path):
        return ""
    with open(_backlog_path, encoding="utf-8") as f:
        if f.readline().strip() != "---":
            return ""
        for line in f:
            if line.strip() == "---":
                break
            k, sep, v = line.partition(":")
            if sep and k.strip() == key:
                return _head_value(v)
    return ""


def skip_reason(cond, screen, policy="size-m"):
    """조건부 스테이지를 건너뛰는 사유. 돌려야 하면 None.

    `size-m` 은 카드 머리말의 크기가 정확히 `S` 일 때만 건너뛴다 — 읽지 못하거나
    모르는 값이면 검증하는 쪽이다. `policy`(설정 `reviewPolicy`)가 `always` 면 크기와
    무관하게 돌고 `never` 면 늘 건너뛴다. 셋 밖의 값은 `size-m` 이다."""
    if cond == "screen" and not screen:
        return "화면 영역 꺼짐"
    if cond == "scope-m" and backlog_get("크기") == "S":
        # 04 는 크기 S 에서만 건너뛴다 — 09 정책(`policy`)과 무관하다(기록 43).
        return "규모 S — 04 범위 통제는 M 이상에서만 돈다"
    if cond == "size-m":
        if policy == "never":
            return "설정 reviewPolicy=never — 09 검토를 돌리지 않는다"
        if policy != "always" and backlog_get("크기") == "S":
            return "규모 S — 09 검토는 M 이상에서만 돈다"
    return None


# 사람 결정이 있어 자동 병합에서 빠진다는 까닭의 머리. 10 이 쓰고 소각 뒤 재판정이 이 머리로 이어받는다.
HUMAN_REASON = "사람 결정 미결이 있다"
OLD_HUMAN_REASON = "사람 몫 미결이 있다"      # E-12 이전 실행이 state.json 에 남긴 옛 머리 — 읽기만 한다

# 새 절 이름 → 옛 절 이름 (E-12). 옛 정의로 쓴 산출물도 읽고 틀 검사를 지나게 한다.
OLD_SECTION = {"사람 결정": "사람 몫", "참고 사항": "사람이 볼 것", "완료 기준": "인수 조건"}


def _section_items(wd, name, head, guide):
    """실행 디렉터리 파일 `name` 의 「## <head>」 절 항목 `[원문]`. 절은 다음 `# `·`## ` 머리에서 끝난다(반려 수정이
    붙인 꼬리 절은 항목이 아니다). 열 0 의 「- 」 줄이 항목을 열고, 들여쓴 줄(빈 줄 뒤여도)과 빈 줄 없이 바로 이어진
    줄은 그 항목에 공백 하나로 붙는다. 틀의 안내 줄(`guide` 로 시작)·자리표시·빈 줄은 항목이 아니고, 첫 항목이
    「없음」이면 그 절은 없음이다. 파일이 없거나 못 읽으면 항목이 없다."""
    try:
        with open(os.path.join(wd, name), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    sec = re.search(r"^## %s[ \t]*$(.*?)(?=^#{1,2} |\Z)" % head, text, re.S | re.M)
    groups, is_open, blank, found = [], False, False, []
    for line in (sec.group(1).splitlines() if sec else []):
        s = line.strip()
        if not s:
            blank = True
            continue
        if line.startswith("- "):
            groups.append(s[2:].strip())
            is_open = True
        elif is_open and (line[0] in " \t" or not blank):
            groups[-1] += " " + s
        else:
            groups.append(s[2:].strip() if s.startswith("- ") else s)
            is_open = False
        blank = False
    for item in groups:
        if not item or item.startswith(("{", guide)):
            continue
        if item.lstrip("*").startswith("없음"):
            break
        found.append(item)
    return found


def decision_items(wd):
    """07 산출물 「## 정할 것」 항목 `[원문]` — 07 작업자가 PM 에게 묻는 결정 (E-11)."""
    return _section_items(wd, "07-changes.md", "정할 것", "구현하다 고를")


def _decision_key(item):
    return item.split(" — 고를 수 있는 것:")[0].strip()


def _answered_decisions(wd):
    """`07-answers.md` 「## 답」 표 첫 칸 — PM 이 답을 단(어느 라벨이든) 정할 것의 한 문장."""
    try:
        with open(os.path.join(wd, "07-answers.md"), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return set()
    sec = re.search(r"^## 답[ \t]*$(.*?)(?=^#{1,2} |\Z)", text, re.S | re.M)
    keys = set()
    for line in (sec.group(1).splitlines() if sec else []):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.strip().startswith("|") and len(cells) >= 2 and cells[1] in ("[답함]", "[사람 결정]", "[사람 몫]"):
            keys.add(cells[0])
    return keys


def human_items(wd):
    """실행 디렉터리의 03a 「## 사람 결정」·07 「## 참고 사항」·07a 항목을 `[(출처, 원문)]` 로 — 이 순서.
    옛 이름 절(「## 사람 몫」·「## 사람이 볼 것」)도 같은 출처로 읽는다(`OLD_SECTION`) — 새 절 먼저.
    07a 는 `07-answers.md` 「## 사람 결정」과, 마지막 판 `07-changes.md` 「## 정할 것」 가운데 PM 의 답 표에 없는 것
    (07a 를 거치지 않은 것 — 08·09 반려 수정이 적은 것 등)이다. 절 읽는 규칙은 `_section_items`.
    `retro_skip_reason` 도 이 함수를 쓴다."""
    found = []
    for src, name, head, guide in (("03a", "03-answers.md", "사람 결정", "사람이 정해야"),
                                   ("07", "07-changes.md", "참고 사항", "머지 전에 사람이"),
                                   ("07a", "07-answers.md", "사람 결정", "사람이 정해야")):
        for h in (head, OLD_SECTION[head]):
            found += [(src, item) for item in _section_items(wd, name, h, guide)]
    answered = _answered_decisions(wd)
    found += [("07a", "%s — 올린 까닭: PM 이 답하지 않은 정할 것" % d)
              for d in decision_items(wd) if _decision_key(d) not in answered]
    return found


def retro_skip_reason(state, wd):
    """11 회고를 건너뛰는 사유. 돌려야 하면 None (기록 43).

    반려 회차·게이트 실패 줄(`events.jsonl` 의 `gate`)·이어받기·03a 의 「## 사람 결정」 항목,
    넷 가운데 하나라도 있으면 돈다. 파일을 못 읽거나 줄이 깨져도 그 신호만 없는 것으로 본다."""
    if state.get("rounds") or state.get("resumed"):
        return None
    try:
        with open(os.path.join(wd, "events.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and "gate" in row:
                    return None
    except OSError:
        pass
    if any(src == "03a" for src, _ in human_items(wd)):
        return None
    return "반려·게이트 실패·이어받기·사람 결정이 없었다 — 11 회고는 일이 있었던 실행에서만 돈다"


def minutes_since(stamp):
    """`YYYY-MM-DD HH:MM` 부터 지금까지 분. 형식이 아니면 0."""
    try:
        began = dt.datetime.strptime(stamp.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return 0
    return max(0, int((dt.datetime.now() - began).total_seconds() // 60))


def backlog_set(**fields):
    """작업 파일 머리말의 열쇠를 고친다. 머리말이 없으면 아무것도 하지 않는다.

    작업목록이 없는 저장소(옛 `issues/` 나 임시 파일)에서도 러너가 그대로 돌게
    조용히 넘어간다 — 상태 표시가 실행을 막을 이유가 없다.
    """
    if not _backlog_path or not os.path.exists(_backlog_path):
        return False
    with open(_backlog_path, encoding="utf-8") as f:
        lines = f.read().splitlines(True)
    if not lines or lines[0].strip() != "---":
        return False
    end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
    if end is None:
        return False

    fields.setdefault("갱신", dt.date.today().isoformat())
    head = lines[1:end]
    for key, value in fields.items():
        value = str(value).split("\n", 1)[0].rstrip("\r")      # 머리말 값은 한 줄이다 — 자세한 것은 10-merge.md
        line = "%s: %s\n" % (key, value)
        for i, l in enumerate(head):
            if l.split(":", 1)[0].strip() == key:
                head[i] = line
                break
        else:
            head.append(line)
    tmp = _backlog_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines([lines[0]] + head + lines[end:])
    os.replace(tmp, _backlog_path)
    return True


def list_cards(bl):
    """작업목록 디렉터리의 카드 파일 이름 집합. 디렉터리가 없으면 빈 집합."""
    try:
        return {n for n in os.listdir(bl) if n.endswith(".md")}
    except OSError:
        return set()


def _tracked_cards(bl):
    """작업목록 폴더에서 깃이 추적하는 파일 이름 집합. 깃 저장소가 아니거나 못 부르면 빈 집합."""
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=bl, capture_output=True,
                             timeout=30, check=True).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(out.split("\0")) - {""}


def demote_new_cards(bl, before):
    """`before` 뒤에 새로 생겼고 깃이 추적하지 않는 카드의 `상태:` 가 아이디어가 아니면 아이디어로 되돌린다.

    회고(11)가 카드를 쓰는 자리다. 위임문이 「아이디어로 쓴다」고 지시하지만 세션이
    어길 수 있고, 승격은 사람만 한다(기록 34). 머리말 줄만 바꾼다 — 본문의 같은 글자,
    원래 있던 카드, 사람이 커밋한 카드(깃 추적), 머리말을 못 읽는 파일은 건드리지 않는다. 바꾼 파일 이름을 돌려준다.
    """
    changed = []
    tracked = _tracked_cards(bl)        # 깃이 추적하는 카드는 사람이 만들어 커밋한 것이다
    for name in sorted(list_cards(bl) - set(before) - tracked):
        path = os.path.join(bl, name)
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                lines = fh.read().splitlines(True)
        except (OSError, UnicodeDecodeError):
            continue
        if not lines or lines[0].strip() != "---":
            continue
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                break
            k, sep, v = line.partition(":")
            if sep and k.strip() == "상태":
                if _head_value(v) != "아이디어":
                    eol = line[len(line.rstrip("\r\n")):]
                    lines[i] = "상태: 아이디어" + eol
                    with open(path, "w", encoding="utf-8", newline="") as fh:
                        fh.writelines(lines)
                    changed.append(name)
                break
    return changed


CONFIG_NAME = "stop-loop.config.json"


def read_config(project):
    """`project` 의 설정 파일 본문. 못 읽으면 None."""
    try:
        with open(os.path.join(project, CONFIG_NAME), encoding="utf-8", newline="") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def restore_config(project, before):
    """설정 파일이 `before` 와 달라졌으면(지워진 것 포함) 되돌리고 True.

    회고(11)가 상한을 올려 막힘을 없애지 못하게 한다 — 위임문이 「값을 고치지 않는다」고
    지시하지만 세션이 어길 수 있고, 스테이지 커밋이 `git add -A` 라 고친 값이 그대로 나간다.
    원래 없던 파일(`before` 가 None)은 지킬 원본이 없으니 손대지 않는다.
    """
    if before is None or read_config(project) == before:
        return False
    try:
        with open(os.path.join(project, CONFIG_NAME), "w", encoding="utf-8", newline="") as fh:
            fh.write(before)
    except OSError:
        return False
    return True


def collect_options(wd):
    """검증자 산출물(02·06·08·09)의 「선택 사항」 절 항목. [(스테이지, 항목 글)].

    자리표시(`{` 로 시작)와 「없음」은 항목이 아니다. 소각이 이 파일들을 치우기 전에
    꺼내야 한다 — 그 뒤에는 이력에만 남아 다음 회고가 읽지 못한다.
    """
    found = []
    for num, _, _, out, _, _ in STAGES:
        if num not in VERDICT_STAGES:
            continue
        try:
            with open(os.path.join(wd, out), encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        sec = re.search(r"^## 선택 사항[ \t]*$(.*?)(?=^## |\Z)", text, re.S | re.M)
        cur = None
        for line in (sec.group(1).splitlines() if sec else []):
            if line.startswith("- "):
                cur = [line[2:].strip()]
                found.append((num, cur))
            elif cur is not None and line.startswith((" ", "\t")) and line.strip():
                cur.append(line.strip())
            else:
                cur = None
    items = [(n, " ".join(t)) for n, t in found]
    return [(n, t) for n, t in items if t and not t.startswith("{") and t.rstrip(".") != "없음"]


def write_signals(wd, repo, issue_id, state):
    """소각 직전에 선택 사항과 측정을 저장소 밖 신호 폴더에 덧붙인다.

    실패해도 실행을 멈추지 않는다 — `write_status` 와 같은 원칙이다. 선택 사항이 하나도
    없으면 그 파일에는 아무것도 쓰지 않는다. `stoploop.write_state` 는 부르지 않는다
    (그 함수를 부르는 자리는 `save_state`·`write_status` 둘뿐이라는 구조 시험이 있다).
    """
    if not SIGNALS_DIR:
        return
    try:
        os.makedirs(SIGNALS_DIR, exist_ok=True)
        items = collect_options(wd)
        if items:
            with open(os.path.join(SIGNALS_DIR, "%s__options.md" % repo), "a",
                      encoding="utf-8") as fh:
                fh.write("## %s · %s\n%s\n\n" % (
                    issue_id, dt.date.today().isoformat(),
                    "\n".join("- %s — %s" % it for it in items)))
        line = {"when": dt.datetime.now().isoformat(timespec="seconds"),
                "issue": issue_id, "state": state.get("state", ""),
                "cost": state.get("cost", 0.0), "stages": state.get("metrics", {})}
        with open(os.path.join(SIGNALS_DIR, "%s__trend.jsonl" % repo), "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as e:
        print("       ! 신호를 못 썼다 — %s" % e)


_status_ctx = None    # main() 이 --dry-run 이 아닐 때만 채운다: repo·branch·worktree


def write_status(state):
    """저장소 밖 요약 파일 하나를 덮어쓴다. 실패해도 실행을 멈추지 않는다.

    `backlog_set` 과 같은 원칙이다 — 상태를 내놓는 일이 실행을 막을 이유가 없다.
    """
    if not STATUS_DIR or not _status_ctx:
        return
    b = state.get("breaker") or {}
    summary = dict(_status_ctx, issue=state.get("issue", ""),
                   state=state.get("state", ""), stage=state.get("stage", ""),
                   reason=(b.get("detail") or b.get("kind") or "멈춤")
                   if state.get("state") == "blocked" else "",
                   stages=state.get("metrics", {}),
                   updated=dt.datetime.now().isoformat(timespec="seconds"))
    try:
        os.makedirs(STATUS_DIR, exist_ok=True)
        stoploop.write_state(os.path.join(
            STATUS_DIR, "%s__%s.json" % (summary["repo"], summary["issue"])), summary)
    except OSError as e:
        print("       ! 요약을 못 썼다 — %s" % e)


def save_state(statef, state):
    """상태 파일을 쓰고 요약도 같은 순간에 쓴다. 러너가 상태를 쓰는 자리는 모두 여기다.

    자리마다 요약 쓰기를 따로 부르면 한 자리가 빠질 때 요약이 상태 파일보다 뒤처진다.
    """
    stoploop.write_state(statef, state)
    write_status(state)


def fire(state, event, guards, events_path, turn=None):
    """전이표를 통해서만 상태를 바꾼다. (성공 여부, 막은 guard).

    표가 표준 정의이고 코드는 해석기여야 한다 (SPEC-STATE §3). 러너가 상태를
    직접 조작하면 표와 코드가 갈린다 — 실제로 갈렸다. 표의
    `(running, GATE_FAIL, running)` 은 「만든 쪽으로 돌아간다」는 뜻인데,
    손으로 옮기며 방향을 놓쳐 반려를 검증자에게 되돌려 보냈다 (2026-08-31).
    """
    ok, nxt, blocked = stoploop.transition(state["state"], event, guards)
    stoploop.append_event(events_path, {"event": event, "from": state["state"],
                                        "to": nxt, "ok": ok, "blocked": blocked})
    if ok:
        prev = state["state"]
        state["state"] = nxt
        extra = {}
        # 진행 시간은 「진행」 구간만 더한다. 보류로 밤을 넘겨도 그 시간은 세지 않는다.
        # `시작` 은 지금 구간의 시작이고, 구간이 끝날 때 `누적`(분)에 더한다.
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        if nxt == "running" and prev != "running":
            extra["시작"] = now
        elif prev == "running" and nxt != "running":
            try:
                total = int(backlog_get("누적") or 0)
            except ValueError:
                total = 0
            extra["누적"] = total + minutes_since(backlog_get("시작"))
        if nxt == "blocked":
            b = state.get("breaker") or {}
            extra["멈춘이유"] = b.get("detail") or b.get("kind") or "멈춤"
        if nxt in ("review", "closed"):
            extra["단계"] = ""
            extra["멈춘이유"] = ""
        # `차례: 러너` = 멈춤·검토지만 사람이 움직일 차례가 아니다(보드가 사람 차례에서 뺀다).
        # 요청한 전이(turn)만 쓰고, 그 밖의 전이는 값이 있을 때만 비운다 — 상태와 같은 쓰기 한 번.
        if turn is not None:
            extra["차례"] = turn
        elif backlog_get("차례"):
            extra["차례"] = ""
        backlog_set(상태=BACKLOG_STATE.get(nxt, nxt), **extra)
    else:
        print("       ! 전이 거부: %s 에서 %s — %s" % (state["state"], event, blocked))
    return ok, blocked


def summary_of(wd, num, outp, fallback):
    """스테이지 요약 한 줄. `NN-summary.txt` 전체가 값이라 파싱이 없다.

    없으면 러너가 아는 사실로 대신한다 — 요약이 없다고 스테이지를 세우지 않는다.
    """
    p = os.path.join(wd, "%s-summary.txt" % num)
    try:
        with open(p, encoding="utf-8") as f:
            line = " ".join(f.read().split())
        if line:
            return line[:120]
    except OSError:
        pass
    return fallback


def commit_stage(project, wd, num, issue_id, attempt, agent, model,
                 outp, verdict=None, findings=None, cost=None, used=None):
    """스테이지 커밋. 트레일러로 작업·단계·회차를 추적한다.

    `git log --grep='Loop-Issue: <값>'` 이 그 작업의 전 여정을 뽑고,
    `--format='%(trailers:key=Loop-Stage,valueonly)'` 로 기계가 읽는다.
    제목은 저장소 이력 형식과 충돌하지 않도록 트레일러와 분리한다.
    """
    if subprocess.run(["git", "check-ignore", "-q", ".workflow"],
                      cwd=project).returncode == 0:
        print("       ! .workflow 가 gitignore 대상이다 — 산출물이 이력에 안 남는다")

    lines = 0
    try:
        with open(outp, encoding="utf-8", errors="replace") as f:
            lines = sum(1 for _ in f)
    except OSError:
        pass
    fallback = "%s줄" % lines if lines else "산출물 없음"
    subject = "%s: %s" % (STAGE_LABEL.get(num, num),
                          summary_of(wd, num, outp, fallback))

    trailers = ["Loop-Issue: %s" % issue_id,
                "Loop-Stage: %s" % num,
                "Loop-Round: %d" % attempt]
    if agent:
        trailers.append("Loop-Agent: %s" % agent)
    if used:
        # 세션이 실제로 돈 모델 ID. 설정의 별칭이 다르면 따로 남긴다.
        trailers.append("Loop-Model: %s" % used)
        if model and model != used:
            trailers.append("Loop-Model-Alias: %s" % model)
    elif model:
        trailers.append("Loop-Model: %s" % model)
    if _plugin_ver:
        trailers.append("Loop-Plugin: %s" % _plugin_ver)
    if cost:
        trailers.append("Loop-Cost: $%.4f" % cost)
    if num == "07":
        # 07 은 테스트 주도 게이트를 통과해야만 여기 온다. 작업자도 Loop-Stage: 07 을
        # 달 수 있어서(main 실측 9건), 통과 기록은 이 줄로 읽는다 — has_07_pass_record
        trailers.append("Loop-Gate: tdd-passed")
    if verdict:
        trailers.append("Loop-Verdict: %s" % verdict)
        trailers.append("Loop-Findings: %d" % len(findings or []))

    subprocess.run(["git", "add", "-A"], cwd=project, capture_output=True)
    r = subprocess.run(["git", "commit", "--allow-empty", "-q", "-F", "-"],
                       cwd=project, input=subject + "\n\n" + "\n".join(trailers),
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("       ! 커밋 실패: %s" % (r.stderr or r.stdout).strip()[:160])
        return False
    print("       커밋 %s" % subprocess.run(
        ["git", "log", "-1", "--format=%h %s"], cwd=project,
        capture_output=True, text=True).stdout.strip())
    return True


def round_decision(state, num, findings):
    """반려가 났을 때 다음 행동을 정한다 — (행동, 재발 여부, 발견 열쇠).

    「고쳤다」는 주장이 아니라 결과로 판정한다. 같은 지적이 또 나오면 회차를
    소모하지 않고 즉시 중단한다 — 고치는 능력이 없다는 뜻이므로 계속 돌면
    무한히 왕복한다 (01 §개선 반복의 상한, 엔진 고정값).

    회차 상한 3 은 **스테이지마다** 센다(ISSUE-stop-by-count). `state["counters"]`
    를 그대로 엔진에 넘기면 다른 스테이지의 회차가 섞인다 — 02 의 반려가 06 의
    몫을 깎던 결함이 이것이었다. 그래서 `rounds`(반려마다 기록되는 목록, 이 중
    같은 스테이지 항목만이 「계속」 결정을 받은 새 지적 반려다)에서 이 스테이지
    항목 수를 세어 일회용 계수로 엔진에 넘긴다. `rounds` 에 `stage` 가 없는
    항목은 세지 않는다 — 회차를 줄이는 쪽이 아니라 주는 쪽이 안전하다.

    main() 에서 떼어낸 이유는 시험 때문이다. 이 판단은 반려가 두 번 나야
    발동하는데 실행으로는 그 상황을 자연스럽게 만들기 어렵다.
    """
    rounds = state.setdefault("rounds", [])
    seen = {k for r in rounds if r.get("stage") == num
            for k in r.get("findingKeys", [])}
    keys = [finding_key(f) for f in findings]
    repeated = bool(seen & set(keys))
    stage_rounds = sum(1 for r in rounds if r.get("stage") == num)
    action = stoploop.on_review_round({"reviewRounds": stage_rounds},
                                      len(set(keys) - seen), repeated)
    return action, repeated, keys


def record_round(state, num, attempt, keys, target_path):
    """회차를 남기고 진동인지 돌려준다. 같은 산출물로 돌아오면 진동이다."""
    rounds = state.setdefault("rounds", [])
    rounds.append({"stage": num, "round": attempt, "findingKeys": keys,
                   "diffHash": file_hash(target_path)})
    return stoploop.detect_oscillation([r for r in rounds if r["stage"] == num])


def resume_state(state, statef):
    """이어받을 때 `counters`·`rounds`·`cost` 를 상태 파일에서 잇는다.

    `stages` 는 잇지 않는다 — 명세가 「passed 인데 산출물이 없으면 파일이 맞다」로
    정해뒀으므로(SPEC-STATE §2-3) 스테이지 완료는 산출물로 판단한다.

    이 둘을 안 이으면 상한이 이어받을 때마다 0 으로 돌아간다. 같은 지적을
    세 번째 받았는데 첫 회차로 세게 되고, 이어받기를 반복하면 상한이 사실상
    없어진다.
    """
    try:
        prev = stoploop.read_state(statef)
    except (OSError, ValueError):
        return False
    state["counters"] = prev.get("counters", {}) or {}
    state["rounds"] = prev.get("rounds", []) or []
    # 돈도 같은 이유로 잇는다 (2026-09-09). 안 이으면 이슈 상한이 이어받을
    # 때마다 0 으로 돌아가, 예산으로 멈춘 실행을 이어받는 것만으로 상한이
    # 사라진다 — 위 두 줄이 막으려던 것과 같은 실패다.
    state["cost"] = float(prev.get("cost") or 0.0)
    # 스테이지별 측정도 같은 이유로 잇는다 — 안 이으면 이어받은 실행의 요약에서 앞
    # 세션 측정이 사라져 비용 누계와 어긋난다. 이 기능 이전의 상태 파일엔 없다.
    state["metrics"] = prev.get("metrics") or {}
    # 07 시작 지점도 잇는다 — 이어받은 세션의 게이트가 앞 세션의 시험 커밋을 보게.
    # 옛 상태 파일에는 없다. 그러면 옮기지 않고 07 진입 때 지금 HEAD 를 적는다.
    if prev.get("tddBase"):
        state["tddBase"] = prev["tddBase"]
    return bool(state["counters"] or state["rounds"] or state["cost"]
                or state.get("tddBase"))


def run_dir(project, issue_id):
    d = os.path.join(project, ".workflow",
                     "%s-%s" % (issue_id, dt.date.today().isoformat()))
    os.makedirs(d, exist_ok=True)
    return d


def pick_run_dir(project, issue_id):
    """이어받을 실행 디렉터리를 이슈 이름으로 고른다 (ISSUE-resume-and-merge).

    실행 디렉터리 이름 형식(`<이슈 이름>-<날짜>`)은 바꾸지 않는다(28 결정 1).
    `bin/ledger.py` 의 `_issue_name` 이 끝의 날짜 하나를 떼는 규칙을 그대로
    쓴다 — 같은 규칙을 두 곳에 두면 어긋난다. (고른 경로 또는 None, 버린 후보
    경로 목록)을 돌려준다. 후보는 이름이 일치하는 디렉터리 전부이고, 그중
    `state.json` 이 있는 것만 남겨 가장 최근 날짜인 것 하나를 고른다.
    """
    base = os.path.join(project, ".workflow")
    if not os.path.isdir(base):
        return None, []
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ledger  # noqa: E402 — 순환 가져오기를 피하려고 함수 안에서 가져온다

    candidates = []
    for name in sorted(os.listdir(base)):
        d = os.path.join(base, name)
        if not os.path.isdir(d):
            continue
        if ledger._issue_name(os.path.join(d, "state.json")) == issue_id:
            candidates.append(d)
    if not candidates:
        return None, []

    with_state = [d for d in candidates if os.path.isfile(os.path.join(d, "state.json"))]
    if not with_state:
        return None, candidates

    chosen = max(with_state, key=lambda d: os.path.basename(d))
    discarded = [d for d in candidates if d != chosen]
    return chosen, discarded


TEMPLATES = os.path.join(HQ, "workflows", "stop-loop", "templates")


def template_path(num):
    """스테이지의 산출물 틀. 없으면 None — 틀이 없는 스테이지도 돈다."""
    hits = sorted(glob.glob(os.path.join(TEMPLATES, "%s-*.template.md" % num)))
    return hits[0] if hits else None


def template_sections(path):
    """틀이 요구하는 `## ` 절 제목. 「…때만」이 붙은 절은 조건부라 세지 않는다."""
    with open(path, encoding="utf-8") as f:
        return [l[3:].strip() for l in f
                if l.startswith("## ") and "때만" not in l]


def workflow_share(path):
    """회고문 「8. 워크플로우 몫」 절의 본문. 표 자료 줄이 없거나 파일·절이 없으면 빈 글."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    body, on = [], False
    for l in lines:
        if l.rstrip() == "## 8. 워크플로우 몫":
            on = True
        elif l.startswith("## "):
            on = False
        elif on:
            body.append(l)
    rows = [l for l in body if l.lstrip().startswith("|")][2:]
    return "\n".join(body).strip() if rows else ""


IMPROVE_SECS = ("개선 항목", "형식 밖", "회고 없음")
IMPROVE_NOTE = ("시나리오가 도는 동안 회고의 워크플로우 몫을 여기에 모은다 — hq 에 카드로 올리지 않는다. "
                "사람은 「즉시 반영 판정」·「반영 여부」 두 칸만 고친다(다시 모아도 보존된다). "
                "구역 제목과 한 줄 모양을 고치면 끝 보고가 읽지 못한다.")


def collect_improvements(plan, no, origin, share_text, retro_present, stopped):
    """시나리오 카드의 회고 워크플로우 몫을 `<plan>/개선모음-<no>.md` 에 모은다 (SC-01 E-8).
    같은 (대상, 증상) 은 한 줄로 묶어 횟수를 더하고, 같은 나온 곳은 다시 세지 않는다.
    5칸이 아닌 표 줄은 「형식 밖」, 회고가 안 돈 실행은 「회고 없음」. 쓰기 실패는 OSError 로 던진다."""
    path = os.path.join(plan, "개선모음-%s.md" % no)
    secs = {k: [] for k in IMPROVE_SECS}
    try:
        with open(path, encoding="utf-8") as f:
            cur = None
            for l in f.read().splitlines():
                if l.startswith("## "):
                    cur = l[3:].strip()
                elif cur in secs and l.startswith("- "):
                    secs[cur].append(l)
    except FileNotFoundError:
        pass
    items = []          # [대상, 증상, 횟수, 제안, 판, 멈춤, [나온 곳], 판정, 반영]
    for l in secs["개선 항목"]:
        c = l[2:].split(" · ")
        if len(c) != 9:
            continue    # ponytail: 사람이 모양을 깬 줄은 읽지 못하고 버려진다 — 끝 보고가 같은 규칙으로 센다
        items.append(c[:2] + [int(c[2]) if c[2].isdigit() else 1] + c[3:5]
                     + [c[5][3:], c[6][5:].split(", "), c[7][9:], c[8][6:]])
    clean = lambda t: " ".join(t.split()).replace(" · ", "/")  # noqa: E731
    if not retro_present:
        if "- " + origin not in secs["회고 없음"]:
            secs["회고 없음"].append("- " + origin)
    else:
        rows = [l.strip() for l in share_text.splitlines() if l.lstrip().startswith("|")][2:]
        for row in rows:
            cells = [clean(c) for c in row.strip("|").split("|")]
            if len(cells) != 5:
                line = "- %s · %s" % (origin, row)
                if line not in secs["형식 밖"]:
                    secs["형식 밖"].append(line)
                continue
            n = int(cells[2]) if cells[2].isdigit() else 1
            hit = next((i for i in items if i[:2] == cells[:2]), None)
            if hit is None:
                items.append(cells[:2] + [n] + cells[3:5] + ["예" if stopped else "아니오", [origin], "—", "—"])
            elif origin not in hit[6]:
                hit[2] += n
                hit[6].append(origin)
                if stopped:
                    hit[5] = "예"
    secs["개선 항목"] = ["- %s · %s · %d · %s · %s · 멈춤 %s · 나온 곳 %s · 즉시 반영 판정 %s · 반영 여부 %s"
                       % (i[0], i[1], i[2], i[3], i[4], i[5], ", ".join(i[6]), i[7], i[8]) for i in items]
    text = "# %s 개선 모음\n\n%s\n" % (no, IMPROVE_NOTE) + "".join(
        "\n## %s\n\n%s" % (k, "".join(l + "\n" for l in secs[k])) for k in IMPROVE_SECS)
    os.makedirs(plan, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def share_to_pr(project, issue_id, body, share, pr_opened):
    """워크플로우 몫을 PR 본문 끝에 덧붙인다. 실패해도 멈추지 않고 한 줄만 돌려준다."""
    if not (pr_opened and share):
        return []
    new = body.rstrip() + "\n\n## 워크플로우 몫\n\n" + share
    res = gh(project, "pr", "edit", "loop/%s" % issue_id, "--body", new)
    if res.returncode == 0:
        return ["워크플로우 몫을 PR 본문에 실었다"]
    tail = (res.stderr or "").strip().splitlines()
    return ["워크플로우 몫을 PR 본문에 싣지 못했다 — %s" % (tail[-1] if tail else "오류")]


def template_gaps(num, artifact):
    """산출물에 없는 틀의 절 목록. 틀이 없으면 빈 목록.

    한 줄 전체가(끝 공백은 무시) `## <절 제목>` 인 줄이 있을 때만 그 절이 있다고 센다 —
    본문 문장 속 인용이나 더 긴 제목에 속지 않는다(ISSUE-template-heading-match-2026-09-29).
    """
    tpl = template_path(num)
    if not tpl or not os.path.exists(artifact):
        return []
    with open(artifact, encoding="utf-8") as f:
        lines = {l.rstrip() for l in f}
    return [s for s in template_sections(tpl)
            if ("## " + s) not in lines and ("## " + OLD_SECTION.get(s, s)) not in lines]


_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_BACKTICKED = re.compile(r'`([^`]+)`')

# 후보에서 빼는 자리 — 이 작업이 고치지 않는 곳(작업 카드·번호 기록 문서·실행
# 디렉터리)이다. 프로젝트에 이 폴더가 없으면 아무것도 빠지지 않는다(03 답변 Q3).
CANDIDATE_SKIP = re.compile(r'^(?:docs/backlog/|\.workflow/)|^workflows/\d[^/]*\.md$')

CANDIDATES_FILE = "02-candidates.txt"


def search_terms(manifest):
    """매니페스트에서 검색어를 뽑는다. 예외를 올리지 않는다(FN-01).

    `files` 의 경로마다 전체 경로와 확장자를 뺀 파일 이름을 담는다. `citations`
    항목은 객체면 `what`, 문자열이면 그 문자열 자체에서 역따옴표로 감싼 토막
    가운데 식별자 모양(영문자·숫자·밑줄)인 것만 담는다. 모양이 다르거나
    읽히지 않는 항목은 건너뛴다. 같은 검색어는 한 번만 담는다.
    """
    terms = []
    seen = set()

    def add(term):
        if term and term not in seen:
            seen.add(term)
            terms.append(term)

    for path in manifest.get("files") or []:
        if not isinstance(path, str):
            continue
        add(path)
        add(os.path.splitext(os.path.basename(path))[0])

    for citation in manifest.get("citations") or []:
        if isinstance(citation, dict):
            text = citation.get("what")
        elif isinstance(citation, str):
            text = citation
        else:
            text = None
        if not isinstance(text, str):
            continue
        for chunk in _BACKTICKED.findall(text):
            if _IDENTIFIER.match(chunk):
                add(chunk)

    return terms


def scope_candidates(project, wd):
    """01-scope.json 이 가리키는 파일과 인용 식별자로 저장소를 검색해, 매니페스트에
    없는데 바꾸는 파일을 가리키는 후보를 찾는다.

    `(candidates, error)` 를 돌려준다. `candidates` 는 `[(경로, [검색어, ...]), ...]`
    로 경로 순 정렬이고, `error` 는 성공이면 `None`, 실패면 이유 한 줄이다.
    절대 예외를 올리지 않는다(FN-02, FN-03).
    """
    manifest_path = os.path.join(wd, "01-scope.json")
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except OSError:
        return [], "01-scope.json 을 읽을 수 없다: %s" % manifest_path
    except json.JSONDecodeError as e:
        return [], "01-scope.json 이 JSON 형식이 아니다: %s" % e

    files = manifest.get("files")
    if not isinstance(files, list):
        return [], "01-scope.json 의 files 가 목록이 아니다"

    terms = search_terms(manifest)
    if not terms:
        return [], None

    known = set(files)
    hits = {}
    for term in terms:
        res = subprocess.run(["git", "grep", "-l", "-w", "-F", "-e", term],
                             cwd=project, capture_output=True, text=True)
        if res.returncode == 1:
            continue                          # 일치 없음 — 실패 아님
        if res.returncode != 0:
            return [], "git grep 실패 (rc=%d): %s" % (res.returncode, res.stderr.strip())
        for path in res.stdout.splitlines():
            if path in known or CANDIDATE_SKIP.match(path):
                continue
            hits.setdefault(path, set()).add(term)

    candidates = [(p, sorted(hits[p])) for p in sorted(hits)]
    return candidates, None


def write_candidates_file(wd, candidates, error):
    """후보 목록을 실행 디렉터리에 쓴다. 돌려주는 값은 파일 첫 줄이다(터미널에도 찍는다)."""
    path = os.path.join(wd, CANDIDATES_FILE)
    if error:
        first = "후보 검색 실패 — %s" % error
        lines = [first]
    elif not candidates:
        first = "후보 없음"
        lines = [first]
    else:
        first = "후보 %d건" % len(candidates)
        lines = [first] + ["- %s  (검색어: %s)" % (p, ", ".join(terms))
                           for p, terms in candidates]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return first


def scope_halted(verdict_path):
    """04 가 「중단」을 적었는가. 「## 판정」 절의 첫 굵은 글씨가 정확히 중단일 때만 참.

    파일 전체를 찾지 않는다 — 다른 절과 틀의 안내문에도 「중단」 글자가 있고,
    채우지 않은 자리표시 `**{진행 · 축소 · 중단}**` 도 그 글자를 품는다.
    못 읽으면(파일 없음·빈 파일·절 없음) 거짓이라 지금처럼 05 로 간다.
    """
    try:
        with open(verdict_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    sec = re.search(r"^## 판정[ \t]*$(.*?)(?=^## |\Z)", text, re.S | re.M)
    bold = re.search(r"\*\*(.+?)\*\*", sec.group(1)) if sec else None
    return bool(bold) and bold.group(1).strip() == "중단"


# 검증자(06·08)는 비싼 자리라 입력을 닫는다 — 이슈 원문 말고는 이 파일들뿐이다.
# 나머지는 실행 디렉터리 경로 한 줄로 찾게 한다(ISSUE-verify-cheaper).
VERIFIER_INPUTS = {
    "06": ("04-verdict.md", "05-design.md"),
    "08": ("05-design.md", "07-changes.md"),
}

# 프로젝트 조작 안내서의 자리 — 설정 키가 아니라 고정 경로다(ISSUE-qa-drive-guide-2026-09-28).
# 프로젝트 뿌리(실행 디렉터리의 두 단계 위) 기준 상대 경로.
QA_GUIDE = "docs/qa-guide.md"


def delegate_text(num, name, out, issue_path, wd, extra="", models=None):
    inputs = ["  - %s  (이슈 원문)" % issue_path]
    if num in VERIFIER_INPUTS:
        os.listdir(wd)                      # 실행 디렉터리를 못 읽으면 예외를 그대로 올린다
        for fn in VERIFIER_INPUTS[num]:
            if os.path.isfile(os.path.join(wd, fn)):
                inputs.append("  - %s" % os.path.join(wd, fn))
            elif fn == "04-verdict.md" and os.path.isfile(os.path.join(wd, "03-answers.md")):
                # 04 가 안 돈 실행(규모 S) — 03 답변이 승인 범위다(기록 43).
                inputs.append("  - %s  (04 가 없어 승인 범위)" % os.path.join(wd, "03-answers.md"))
        if num == "08":
            root = os.path.dirname(os.path.dirname(wd))
            guide = os.path.join(root, QA_GUIDE)
            if os.path.isfile(guide):
                inputs.append("  - %s  (프로젝트 조작 안내서)" % guide)
            doc = (models or {}).get("_unitDocPath")
            if doc and "{key}" not in doc:
                docp = os.path.join(root, doc)
                if os.path.isfile(docp):
                    inputs.append("  - %s  (기능 단위 문서)" % docp)
        inputs.append("  - 목록에 없는 것은 실행 디렉터리에서 찾는다: %s"
                      "  (정말 필요할 때만, 필요한 부분만)" % wd)
    else:
        for prev in sorted(os.listdir(wd)):
            if prev.endswith(".md") and prev != os.path.basename(out):
                inputs.append("  - %s" % os.path.join(wd, prev))
    if num in ("03a", "07a") and scenario_path():
        inputs.append("  - %s  (이 카드의 시나리오)" % scenario_path())
    if num == "02":
        inputs.append("  - %s  (러너가 뽑은 후보 목록)" % os.path.join(wd, CANDIDATES_FILE))
    if num == "11":
        for vf in sorted(os.listdir(wd)):
            if vf.endswith("-verdict.json"):
                inputs.append("  - %s" % os.path.join(wd, vf))
        # 실행 디렉터리 이름(`run_dir`, 날짜가 붙는다)이 아니라 이슈 이름을
        # 쓴다 — 커밋 트레일러(`commit_stage` 의 Loop-Issue)에 적히는 값과
        # 같아야 이력 검색이 걸린다. issue_path 에서 같은 방식으로 뽑는다
        # (main() 의 issue_id = os.path.splitext(os.path.basename(issue))[0]).
        issue_name = os.path.splitext(os.path.basename(issue_path))[0]
        signals = ""
        if SIGNALS_DIR:
            repo = (models or {}).get("_repo") or os.path.basename(
                os.path.dirname(os.path.dirname(wd)))
            signals = RETRO_SIGNALS.format(
                options=os.path.join(SIGNALS_DIR, "%s__options.md" % repo),
                trend=os.path.join(SIGNALS_DIR, "%s__trend.jsonl" % repo))
        extra = RETRO_EXTRA.format(issue=issue_name,
                                   hq=os.path.normpath(HQ),
                                   project=os.path.dirname(os.path.dirname(wd)),
                                   signals=signals) + extra
    if num == "08":
        given = []
        if (models or {}).get("_appStart"):
            given.append("  - 앱 기동 명령: %s" % models["_appStart"])
        if (models or {}).get("_mutantCap"):
            given.append("  - 변이 상한: 한 회차에 %d개" % models["_mutantCap"])
        if given:
            extra = "\n프로젝트 설정이 정한 값 — 정의의 기본값보다 이것을 따른다:\n" \
                    + "\n".join(given) + "\n" + extra
    if num in VERDICT_STAGES:
        vp = os.path.join(os.path.dirname(out), "%s-verdict.json" % num)
        extra = ("""
판정 파일도 쓴다: %s
  - 사람이 읽는 문서와 기계가 읽는 값을 가른다. 문서에는 자유롭게 쓰되
    판정은 이 JSON 이 유일한 근거다.
  - 형식: {"verdict": "통과" 또는 "반려",
           "findings": [{"severity": "major", "kind": "초과",
                         "where": "파일:줄 또는 문서 절 — 문제가 사는 자리",
                         "what": "무엇이 문제인가 한 줄"}]}
  - **where 를 반드시 채운다.** 러너가 이 값으로 같은 지적인지 가른다.
    제목과 표현은 회차마다 흔들리지만 문제가 사는 자리는 흔들리지 않는다.
  - 통과면 findings 는 빈 배열이다. 선택 사항은 findings 에 넣지 않는다.
""" % vp) + extra
        done = "%s 와 %s 가 둘 다 있어야 한다." % (out, vp)
    else:
        done = "내용이 비어 있지 않아야 한다."
    tpl = template_path(num)
    if tpl:
        extra = ("""
산출물 틀: %s
  - 이 틀의 `## ` 절을 모두 채운다. 제목을 바꾸거나 지우지 않는다.
  - 채울 것이 없는 절에는 「없음」이라고 적는다. 빈 절은 미완료다.
  - 「…때만」이 붙은 절은 해당될 때만 채운다.
""" % tpl) + extra
    return DELEGATE.format(num=num, name=name, goal=GOALS.get(num, name),
                           inputs="\n".join(inputs), out=out, extra=extra,
                           summary=os.path.join(os.path.dirname(out),
                                                "%s-summary.txt" % num),
                           done=done)


# 규모 S 의 묶인 세션 — 앞 단계 세션 하나가 뒤 단계 일까지 한다(01→03, 02→06).
# 판정·회차·커밋은 단계마다 따로 남는다. 세션만 묶는다.
MERGED = {"01": "03", "02": "06"}
MERGED_LABEL = {"01": "01+03 분석·확인 요청", "02": "02+06 범위·설계 검증"}


def merged_run():
    """카드 머리말 크기가 정확히 `S` 인가. 못 읽으면(없음·빈 값·소문자) 거짓 — 지금 순서."""
    return backlog_get("크기") == "S"


def run_order(merged):
    """단계를 도는 순서. 묶이면 02 가 06 바로 앞으로 간다 — 표 `STAGES` 는 바뀌지 않는다."""
    order = list(STAGES)
    if merged:
        t = order.pop([s[0] for s in order].index("02"))
        order.insert([s[0] for s in order].index("06"), t)
    return order


def design_verifier_def():
    """설계 검증자(06 규칙) 정의 파일 — 정본이 있으면 그것, 없으면 플러그인 배치."""
    canon = os.path.join(HQ, "workflows", "stop-loop", "agents", "verifier-design.md")
    return canon if os.path.isfile(canon) else os.path.join(HQ, "agents", "loop-verifier-design.md")


def merged_delegate(lead, follow, follow_num):
    """앞 단계 세션 하나에 넘기는 글 — 머리 안내 + 두 위임문 전문(각자의 산출물·요약·판정 파일)."""
    lead_num = re.match(r"\[STOP-LOOP (\S+) ", lead).group(1)
    lines = ["[STOP-LOOP %s+%s 묶음]" % (lead_num, follow_num), "",
             "이 세션이 두 단계를 한다. 아래 위임문 둘을 차례로 수행한다.",
             "산출물·요약·판정 파일은 위임문마다 따로 쓴다 — 하나로 합치지 않는다."]
    if follow_num == "06":
        lines.append("06 은 설계 검증자의 규칙으로 본다 — 먼저 읽는다: %s"
                     % os.path.normpath(design_verifier_def()))
    else:
        lines.append("둘째 위임문은 첫째 산출물(`01-scope.md`)도 읽는다.")
    return "\n".join(lines) + "\n\n" + lead + "\n" + follow


def free_suffix(path):
    """`<path>.r<n>` 가운데 아직 없는 첫 n — 앞 근거를 덮지 않는다."""
    n = 1
    while os.path.exists("%s.r%d" % (path, n)):
        n += 1
    return n


def drop_follower(wd, num, attempt):
    """묶인 세션이 뒤 단계(06)로 쓴 산출물·판정을 읽지 않고 치운다 — 반려로 낡은 설계 위의 것이다."""
    out = [x[3] for x in STAGES if x[0] == num][0]
    for path in (os.path.join(wd, out), verdict_path(wd, num)):
        if os.path.exists(path):
            os.rename(path, "%s.dropped-r%d" % (path, attempt))


def live_lines(raw):
    """stream-json 한 줄에서 사람이 읽을 한 줄들을 뽑는다. 없으면 빈 목록, 예외는 없다.

    도구 호출은 「이름: 명령의 첫 줄」(명령이 없으면 파일 경로, 그것도 없으면 이름만),
    모델 글은 그 글의 첫 줄이다. 그 밖의 줄(준비·훅·한도·도구 결과·마지막 결과)은 버린다.
    """
    if not raw.startswith("{"):
        return []
    try:
        o = json.loads(raw)
        blocks = (o.get("message") or {}).get("content") or []
    except (ValueError, TypeError, AttributeError):
        return []
    if o.get("type") != "assistant" or not isinstance(blocks, list):
        return []

    def first(v):
        rows = str(v or "").strip().splitlines()
        return rows[0].strip() if rows else ""

    out = []
    for blk in blocks:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") == "tool_use":
            inp = blk.get("input") if isinstance(blk.get("input"), dict) else {}
            what = first(inp.get("command")) or first(inp.get("file_path"))
            name = str(blk.get("name") or "")
            row = "%s: %s" % (name, what) if what else name
        elif blk.get("type") == "text":
            row = first(blk.get("text"))
        else:
            continue
        if row:
            out.append(row if len(row) <= LIVE_MAX else row[:LIVE_MAX - 1] + "…")
    return out


def live_append(num, raw):
    """받은 줄 하나의 요약을 이 실행의 줄 파일 끝에 덧붙인다. 예외를 밖으로 내지 않는다.

    `call()` 의 줄 받는 반복문 안에서 불린다 — 여기서 예외가 새면 출력 읽기가 멈추고
    침묵 판정이 멀쩡한 세션을 죽인다. 그래서 무엇이 실패해도 경고 한 줄만 찍는다.
    """
    if not (LIVE_DIR and _live_path):
        return
    try:
        rows = live_lines(raw)
        if not rows:
            return
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        os.makedirs(os.path.dirname(_live_path), exist_ok=True)
        with open(_live_path, "a", encoding="utf-8") as f:
            f.write("".join("%s %s %s\n" % (stamp, num or "--", row) for row in rows))
    except Exception as e:   # noqa: BLE001 — 뜨거운 길이라 무엇이든 실행을 멈추지 않는다
        print("       ! 실시간 줄을 못 썼다 — %s" % e)


def session_tree(pid):
    """`pid` 자신과 부모 번호를 따라 내려간 자손 전체의 목록.

    각 줄은 `ps` 출력 한 줄을 앞뒤 공백만 걷어 낸 「번호 부모번호 경과시간 명령」이다.
    첫 원소는 `pid` 자신의 줄(살아 있을 때), 그 뒤로 자손이다. `pid` 가 이미 없으면
    빈 목록을 돌려준다. `ps` 가 실패하거나 5초를 넘기면 예외를 낸다 — 부르는 쪽이 삼킨다.
    """
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,etime=,command="],
                         capture_output=True, text=True, timeout=5, check=True).stdout
    rows, children = {}, {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        p, pp = int(parts[0]), int(parts[1])
        rows[p] = line.strip()
        children.setdefault(pp, []).append(p)
    result = [rows[pid]] if pid in rows else []
    stack = list(children.get(pid, []))
    while stack:
        p = stack.pop()
        if p in rows:
            result.append(rows[p])
        stack.extend(children.get(p, []))
    return result


_SECRET_OPT_RE = re.compile(
    r"(--[\w.-]*(?:key|token|secret|password)[\w.-]*)(=|\s+)(?!-)(\S+)", re.IGNORECASE)
_SECRET_SHAPE_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[A-Z0-9]{16}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|figd_[A-Za-z0-9]{16,})\b")


def redact_secrets(text):
    """멈춤 단서 줄에서 비밀로 보이는 값을 `<REDACTED>` 로 가린다(05-design.md FN-02).

    (1) 이름에 key·token·secret·password 가 든(대소문자 무관) `--옵션=값`·`--옵션 값`
    꼴의 값 자리. 값이 없는 옵션(예: `--use-key`)은 다음 옵션을 삼키지 않는다.
    (2) 흔한 키 모양 문자열 여섯 가지를 통째로. 둘 다 없으면 입력을 그대로 돌려준다.
    """
    text = _SECRET_OPT_RE.sub(r"\1\2<REDACTED>", text)
    text = _SECRET_SHAPE_RE.sub("<REDACTED>", text)
    return text


def hang_clues(pid, ended):
    """끊기 직전에 찍는 단서 묶음. 예외를 밖으로 내지 않는다 (`call()` 의 두 끊기 자리에서 쓴다).

    `pid` 는 세션 프로세스 번호, `ended` 는 `result` 이벤트의 `subtype`(없었으면 `None`).
    """
    lines = ["# 멈춤 단서 — 러너가 끊기 직전에 찍었다",
             "# result 이벤트: %s" % (("있음 (subtype=%s)" % ended) if ended else "없음"),
             "# 표준 오류는 표준 출력에 합쳐 받았다 — 아래 출력 꼬리에 섞여 있다"]
    try:
        rows = session_tree(pid)
    except Exception as e:   # noqa: BLE001 — 끊기를 늦추거나 막으면 안 된다
        lines.append("# 자손 프로세스를 못 찍었다 — %s: %s" % (type(e).__name__, e))
    else:
        if rows:
            lines.append("# 세션 프로세스와 자손 %d개 (번호 부모번호 경과시간 명령):" % len(rows))
            lines.extend("#   %s" % redact_secrets(row) for row in rows)
        else:
            lines.append("# 세션 프로세스와 자손 0개 — 찍기 전에 이미 끝났다")
    lines.append("# ---- 마지막 출력 60줄 ----")
    return "\n".join(lines) + "\n"


# ── 셸 명령 검사 (ISSUE-session-command-guard-2026-09-28) ───────────
#
# 러너가 띄운 세션에서 되돌릴 수 없는 명령을 실행 전에 막는다. PreToolUse 훅
# (Claude Code 가 `Bash` 도구를 실행하기 직전에 부르는 외부 명령)으로 붙는다 —
# `call()` 이 세션마다 `--settings` 로 주입하므로 `.claude/settings.json` 은
# 바뀌지 않고, 사람이 여는 대화형 세션에는 걸리지 않는다(05-design.md FN-08).

_GUARD_WRAPPERS = {"sudo", "command", "exec", "nohup", "time"}
_GUARD_BOUNDARY = {";", "&&", "||", "|", "&", "(", ")", ">", "<"}
_GUARD_REWRITE = {"rebase", "filter-branch", "filter-repo", "update-ref"}
_GUARD_NEW_BRANCH_OPTS = {"-b", "-B", "-c", "-C", "--orphan"}


def guard_settings():
    """PreToolUse 훅 JSON 문자열. 실행 중인 `runner.py` 자신을 절대경로로
    `--guard` 모드로 부른다 — 새 파일을 만들지 않고, 꾸러미(`bin/bundle.py`)가
    이미 담는 파일로 동작한다(03 답변 Q2). 종료 코드가 1 이면 통과되므로
    `|| exit 2` 로 감싸 검사 오류도 막는 쪽으로 떨어뜨린다(01 실측, FN-07)."""
    cmd = "%s %s --guard || exit 2" % (
        shlex.quote(sys.executable), shlex.quote(os.path.abspath(__file__)))
    return json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": cmd}]}]}})


def _guard_strip_wrappers(tokens):
    """`sudo`·`env FOO=1`·`이름=값` 대입처럼 실제 명령 앞에 붙는 말을 걷어낸다."""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        base = os.path.basename(t)
        if base in _GUARD_WRAPPERS:
            i += 1
            continue
        if base == "env":
            i += 1
            while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
                i += 1
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            i += 1
            continue
        break
    return tokens[i:]


def _guard_split_segments(command):
    """`;`·`&&`·`||`·`|`·`&`·`(`·`)`·`<`·`>`·백틱·줄바꿈에서 토막으로 나눈다
    (FN-06). 따옴표가 안 맞으면 `shlex.shlex` 가 `ValueError` 를 낸다 — 여기서
    잡지 않는다. 막는 것은 호출하는 쪽(`guard_main`)의 몫이다."""
    text = command.replace("`", ";").replace("\n", ";")
    lex = shlex.shlex(text, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    segments, cur = [], []
    for tok in lex:
        if tok in _GUARD_BOUNDARY:
            segments.append(cur)
            cur = []
        else:
            cur.append(tok)
    segments.append(cur)
    return segments


def _guard_resolve_dir(path, base):
    expanded = os.path.expanduser(os.path.expandvars(path))
    if not os.path.isabs(expanded):
        expanded = os.path.join(base, expanded)
    return os.path.realpath(expanded)


def _guard_outside(path, base, root, tmp):
    """경로를 풀어 작업 트리·임시 디렉터리 밖인지 본다(03 답변 Q3, FN-04).
    변수·명령 치환을 풀고 나서도 `$`·백틱이 남으면(모르는 경로) 밖으로 본다."""
    expanded = os.path.expanduser(os.path.expandvars(path))
    if "$" in expanded or "`" in expanded:
        return True
    if not os.path.isabs(expanded):
        expanded = os.path.join(base, expanded)
    resolved = os.path.realpath(expanded)
    if resolved == root or resolved.startswith(root + os.sep):
        return False
    if resolved == tmp or resolved.startswith(tmp + os.sep):
        return False
    return True


def _guard_rm_paths(tokens):
    paths, after_dashdash = [], False
    for t in tokens[1:]:
        if t == "--":
            after_dashdash = True
            continue
        if not after_dashdash and t.startswith("-"):
            continue
        paths.append(t)
    return paths


# 값을 다음 토막으로 받는 git 전역 옵션 — 이것들은 값까지 함께 건너뛴다.
_GUARD_GIT_VALUE_OPTS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace",
                         "--config-env", "--super-prefix")


def _guard_git_args(tokens):
    """`git` 과 하위 명령 사이의 전역 옵션을 모두 걷어내고(`-C` 값은 따로 뽑는다),
    하위 명령부터의 인자를 돌려준다. `-c k=v`·`--no-pager`·`--git-dir=…` 가
    끼어도 하위 명령을 놓치지 않는다(08 반려 r1 발견 1)."""
    args = tokens[1:]
    repo_dir, i, unsure = None, 0, False
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a in _GUARD_GIT_VALUE_OPTS:
            if a == "-C" and i + 1 < len(args):
                repo_dir = args[i + 1]
            i += 2
            continue
        if a.startswith("-C") and len(a) > 2:
            repo_dir = a[2:]
        else:
            unsure = True  # 값을 받는지 모르는 옵션 — 하위 명령을 확정하지 못한다
        i += 1
    return repo_dir, args[i:], unsure


def _guard_current_branch(repo):
    cp = subprocess.run(["git", "-C", repo, "symbolic-ref", "--short", "HEAD"],
                        capture_output=True, text=True)
    return cp.stdout.strip() if cp.returncode == 0 else None


def _guard_check_git(tokens, base, root, tmp):
    repo_dir, args, unsure = _guard_git_args(tokens)
    if not args:
        return None
    if unsure:  # 모를 때는 막는 쪽 — 인자 어디에든 이름이 낱말로 서 있으면 막는다
        if "push" in args:
            return ("push", "러너 세션은 원격으로 올리지 않는다 — 올리기는 러너의 10 인계가 한다")
        if (_GUARD_REWRITE.intersection(args)
                or ("commit" in args and "--amend" in args)
                or ("reset" in args and "--hard" in args)):
            return ("rewrite", "러너 세션은 이력을 다시 쓰지 않는다 — 알 수 없는 git 옵션 뒤의 재작성 명령은 막는다")
        if "commit" in args:
            branch = _guard_current_branch(repo_dir or base)
            if branch and base_branch([branch]):
                return ("base-branch", "러너 세션은 기준 가지에 커밋하지 않는다")
        if ("checkout" in args or "switch" in args) and base_branch(args):
            return ("base-branch", "러너 세션은 기준 가지로 옮겨 가지 않는다")
    sub, rest = args[0], args[1:]
    if sub == "push":
        return ("push", "러너 세션은 원격으로 올리지 않는다 — 올리기는 러너의 10 인계가 한다")
    if sub in _GUARD_REWRITE:
        return ("rewrite", "러너 세션은 이력을 다시 쓰지 않는다 — git %s 는 막는다" % sub)
    if sub == "commit" and "--amend" in rest:
        return ("rewrite", "러너 세션은 이력을 다시 쓰지 않는다 — commit --amend 는 막는다")
    if sub == "reset" and "--hard" in rest:
        return ("rewrite", "러너 세션은 이력을 다시 쓰지 않는다 — reset --hard 는 막는다")
    if sub in ("checkout", "switch"):
        if "--" in rest or any(o in rest for o in _GUARD_NEW_BRANCH_OPTS):
            return None
        positional = [a for a in rest if not a.startswith("-")]
        if base_branch(positional):
            return ("base-branch", "러너 세션은 기준 가지로 옮겨 가지 않는다")
        return None
    if sub == "commit":
        branch = _guard_current_branch(repo_dir or base)
        if branch and base_branch([branch]):
            return ("base-branch", "러너 세션은 기준 가지에 커밋하지 않는다")
        return None
    if sub == "clean" and repo_dir:
        if _guard_outside(repo_dir, base, root, tmp):
            return ("outside-delete",
                    "작업 트리 밖을 지우는 명령은 막는다 — 대상: %s" % repo_dir)
        return None
    return None


def _guard_check_segment(tokens, base, root, tmp):
    name = os.path.basename(tokens[0])
    if name == "git":
        return _guard_check_git(tokens, base, root, tmp)
    if name == "rm":
        for p in _guard_rm_paths(tokens):
            if _guard_outside(p, base, root, tmp):
                return ("outside-delete",
                        "작업 트리 밖을 지우는 명령은 막는다 — 대상: %s" % p)
        return None
    if name == "find" and len(tokens) > 1 and not tokens[1].startswith("-") \
            and "-delete" in tokens[1:]:
        if _guard_outside(tokens[1], base, root, tmp):
            return ("outside-delete",
                    "작업 트리 밖을 지우는 명령은 막는다 — 대상: %s" % tokens[1])
        return None
    return None


def guard_check(command, cwd, root):
    """명령 문자열 하나를 판정한다(FN-01~FN-06). 막을 이유가 있으면
    `(규칙 이름, 이유 문장)` 을, 없으면 `None` 을 돌려준다. 나눌 수 없는
    명령은 `shlex` 의 `ValueError` 를 그대로 낸다."""
    root = os.path.realpath(root)
    tmp = os.path.realpath(tempfile.gettempdir())
    base = os.path.realpath(cwd) if cwd else root
    for tokens in _guard_split_segments(command):
        stripped = _guard_strip_wrappers(tokens)
        if not stripped:
            continue
        if stripped[0] in ("cd", "pushd") and len(stripped) > 1:
            base = _guard_resolve_dir(stripped[1], base)
            continue
        hit = _guard_check_segment(stripped, base, root, tmp)
        if hit:
            return hit
    return None


def guard_main():
    """PreToolUse 훅의 진입점 — 표준 입력의 JSON 을 읽어 판정한다. 검사가
    어떤 식으로든 실패하면 막는다(FN-07). 종료 코드 2 를 돌려주면 명령이
    실행되지 않는다(01 실측); 0 이면 통과다."""
    stage = os.environ.get("STOP_LOOP_STAGE", "")
    try:
        payload = json.load(sys.stdin)
        if payload.get("tool_name") != "Bash":
            return 0
        command = payload["tool_input"]["command"]
        root = os.environ["STOP_LOOP_WORKTREE"]
        cwd = payload.get("cwd") or root
        hit = guard_check(command, cwd, root)
    except Exception as e:   # noqa: BLE001 — 검사 실패는 막는 쪽이다(FN-07)
        _guard_block("guard-error", "검사가 실패했다(%s: %s) — 막는다"
                     % (type(e).__name__, e), stage)
        return 2
    if hit is None:
        return 0
    rule, reason = hit
    _guard_block(rule, reason, stage)
    return 2


def _guard_block(rule, reason, stage=""):
    sys.stderr.write("막힘(%s): %s\n" % (rule, reason))
    events_path = os.environ.get("STOP_LOOP_EVENTS")
    if events_path:
        try:
            stoploop.append_event(events_path,
                                  {"stage": stage, "guard": "blocked", "rule": rule})
        except Exception:   # noqa: BLE001 — 이벤트 기록 실패가 막기를 못 하게 하면 안 된다
            pass


def call(agent, prompt, cwd, config_dir, num=None, models=None):
    """세션을 띄우고 출력을 흘려 받는다. (상태, 로그, 사유) 를 돌려준다.

    멈춤을 두 가지로 가른다 — 둘을 섞으면 느린 것과 멈춘 것이 구분되지 않는다.
      · 침묵  — 마지막 출력 이후 경과. 진짜 멈춤의 신호다
      · 총시간 — 출력은 계속 나오는데 끝나지 않는 경우의 마지막 그물

    `stream-json` 으로 받는 이유가 침묵 측정이다. 기본 `text` 는 끝에 한 번에
    나와서 정상 세션도 통째로 침묵으로 보인다.

    stdin 을 끊는다 — 물려받으면 세션이 입력을 기다리다 멈춘다
    (2026-08-31 실측: "no stdin data received in 3s" 경고가 실제로 찍혔다).
    """
    silence, hard = stage_limits(num, models)
    LAST_RESULT.clear()
    env = dict(os.environ, CLAUDE_CONFIG_DIR=os.path.expanduser(config_dir),
               STOP_LOOP_WORKTREE=os.path.abspath(cwd), STOP_LOOP_STAGE=num or "")
    if _events_path:
        env["STOP_LOOP_EVENTS"] = _events_path
    cmd = [CLAUDE, "-p", "--output-format", "stream-json", "--verbose",
           "--dangerously-skip-permissions", "--settings", guard_settings()]
    models = models or {}
    model = models.get(num) or models.get(agent)   # 스테이지가 에이전트를 이긴다
    if model:
        cmd += ["--model", model]
    if agent:
        cmd += ["--agent", agent_ref(agent)]
    cmd.append(prompt)

    proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    tail, last, cost, ended = [], [time.monotonic()], [0.0], [None]

    def pump():
        for line in proc.stdout:
            last[0] = time.monotonic()
            live_append(num, line)           # 도는 동안 쌓는다 — 죽어도 직전 줄이 남게
            if '"assistant"' in line and "model" not in LAST_RESULT:
                # 본 세션(하위 에이전트 줄이 아닌) 첫 assistant 줄의 실제 모델 ID.
                # 해석 실패가 반복문을 죽이면 침묵 판정이 멀쩡한 세션을 죽인다.
                try:
                    ev = json.loads(line)
                    m = ev["message"]["model"]
                    if (ev.get("type") == "assistant" and not ev.get("parent_tool_use_id")
                            and isinstance(m, str) and m and not m.startswith("<")):
                        LAST_RESULT["model"] = m
                except (ValueError, TypeError, KeyError, AttributeError):
                    pass
            if '"total_cost_usd"' in line:   # 마지막 result 이벤트가 비용을 싣는다
                try:
                    ev = json.loads(line)
                except (ValueError, TypeError):
                    ev = {}
                try:
                    cost[0] = float(ev.get("total_cost_usd") or 0.0)
                except (ValueError, TypeError):
                    pass
                # 턴 상한(`maxTurns`)에 닿으면 세션은 **정상 종료한다** — 종료 코드만
                # 보면 완료와 구분되지 않고, 부분 산출물이 게이트를 지난다.
                ended[0] = ev.get("subtype")
                # 같은 줄이 시간과 토큰도 싣는다. 없으면(옛 모양) 없는 값으로 둔다.
                if ev.get("duration_ms") is not None:
                    LAST_RESULT["ms"] = ev["duration_ms"]
                u = ev.get("usage") or {}
                for k in TOKEN_KEYS:
                    LAST_RESULT[k] = u.get(k) or 0
            tail.append(line)
            del tail[:-60]                   # 꼬리만 남긴다 — 로그가 메모리를 먹지 않게

    t = threading.Thread(target=pump, daemon=True)
    t.start()

    start = time.monotonic()
    while proc.poll() is None:
        time.sleep(2)
        quiet = time.monotonic() - last[0]
        if quiet > silence:
            clues = hang_clues(proc.pid, ended[0])
            proc.kill(); t.join(timeout=5)
            return "timeout", clues + "".join(tail), "출력이 %d초 끊겼다" % quiet, cost[0]
        if time.monotonic() - start > hard:
            clues = hang_clues(proc.pid, ended[0])
            proc.kill(); t.join(timeout=5)
            return "timeout", clues + "".join(tail), ("총 %d분을 넘겼다" % (hard // 60)
                                              if hard >= 60 else "총 %d초를 넘겼다" % hard), cost[0]
    t.join(timeout=5)
    if ended[0] and ended[0] != "success":
        why = {"error_max_turns": "턴 상한에 닿았다 — 산출물이 부분일 수 있다"}.get(
            ended[0], "세션이 %s 로 끝났다" % ended[0])
        return "timeout", "".join(tail), why, cost[0]
    status = "ok" if proc.returncode == 0 else "rc%d" % proc.returncode
    return status, "".join(tail), None, cost[0]


def readable(log):
    """stream-json 꼬리에서 사람이 읽을 만한 것만 뽑는다."""
    out = []
    for line in log.splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        for b in (o.get("message", {}) or {}).get("content", []) or []:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b["text"])
    return "\n".join(out)[-400:] or log[-400:]


def tree_outside_run(project):
    """실행 디렉터리 밖의 작업 트리 상태. (수정된 것, 새로 생긴 것).

    검증자에게 쓰기 도구를 준 대신 여기서 지킨다 — 보호는 「쓸 수 있는가」가
    아니라 「무엇을 바꿨는가」에 건다 (SPEC-AGENTS 2026-08-31 정정).

    **둘을 가른다** (2026-09-01). 검증자가 테스트를 돌리면 `__pycache__` 같은
    부산물이 생기는데, 그건 검증자가 만든 게 아니라 도구가 만든 것이고 지워도
    아무 일이 없다. 그것까지 위반으로 보면 멀쩡한 실행이 멈춘다 — 실제로 멈췄다.

    막아야 할 것은 「검토하다 이왕 본 김에 고치는 것」이고, 그것은 **기존 파일의
    수정**으로 나타난다. 새 파일은 경고만 하고 넘어간다 — 드문 일이고, 이제
    스테이지마다 커밋하므로 이력에 남아 사후에 드러난다.
    """
    out = subprocess.run(["git", "status", "--porcelain"], cwd=project,
                         capture_output=True, text=True).stdout
    modified, created = set(), set()
    for l in out.splitlines():
        if ".workflow/" in l:
            continue
        (created if l.startswith("??") else modified).add(l)
    return modified, created


def verdict_path(wd, num):
    return os.path.join(wd, "%s-verdict.json" % num)


def read_verdict(wd, num):
    """판정과 발견 목록을 JSON 에서 읽는다. (판정, 발견) 또는 (None, []).

    사람이 읽는 문서와 기계가 읽는 값을 파일로 가른다. 한 파일이 두 독자를
    섬기면 문서를 잘 쓸수록 기계가 헷갈린다 — 2026-08-31 에 두 번 겪었다.
    재검토 문서의 「앞선 검토가 반려했고」에 걸렸고(통과한 06 을 세웠다),
    제목 기호 `# 판정: 통과` 에 걸렸다(통과한 08 을 불명으로 읽었다).
    둘 다 **통과를 실패로** 뒤집었다.

    발견 목록을 함께 받는 이유는 엔진의 회차 판정 때문이다 —
    `on_review_round` 가 새 지적 수와 재발 여부를 인자로 받는데, 마크다운에서는
    셀 수가 없어 그 기능이 놀고 있었다.
    """
    return _read_verdict_file(verdict_path(wd, num))


def _read_verdict_file(path):
    """`read_verdict` 이 쓰는 실제 읽기. 임의 경로(회차가 지난 판정 파일)에도 쓴다 — FN-02."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None, []
    v = d.get("verdict")
    if v not in ("통과", "반려"):
        return None, []
    return v, [f for f in d.get("findings", []) if isinstance(f, dict)]


def finding_key(f):
    """같은 지적인지 가르는 열쇠 — **문제가 사는 자리**로 가른다.

    지적 문장으로 가르면 안 된다. 같은 결함을 회차마다 다르게 서술하면 새 지적으로
    세어 회차 상한이 헛돈다. **제목과 줄번호는 검토자마다 흔들리지만 문제의 코드는
    흔들리지 않는다** (ECC orch-review 의 근거, 2026-09-08 채택).

    ponytail: `where` 가 없으면 `what` 으로 떨어진다 — 열쇠가 섞이면 같은 결함을
    다르게 셀 수 있다. 그래서 위임 메시지가 `where` 를 필수로 요구한다.
    """
    where = " ".join(str(f.get("where", "")).split())
    if where:
        # 경로의 줄번호는 수정하면 밀리므로 뗀다 — 자리는 파일·절까지만 본다
        where = re.sub(r"[:#]L?\d+([-~]\d+)?", "", where)[:80]
        return "%s@%s" % (f.get("kind", ""), where)
    what = " ".join(str(f.get("what", "")).split())[:80]
    return "%s|%s" % (f.get("kind", ""), what)


def file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return None


# ── 08 재검증 재생 (ISSUE-qa-recheck-replay-2026-09-28) ─────────────
# 08 이 반려하며 지적마다 남긴 `replay`(재현 명령·변이)를 세션 없이 그대로
# 다시 돌린다. 결정론적으로 답이 나오는 부분(명령을 돌려 종료 코드를 비교)만
# 러너가 하고, 조금이라도 모르면 지금처럼 08 세션을 띄운다 — 「모를 때는
# 막는 쪽」(`history_rewritten` 의 규율과 같다).
REPLAY_TIMEOUT = 120                       # 초. 08 스테이지 총시간(LIMITS) 안쪽


def replay_shape_ok(f):
    """지적 f 의 `replay` 가 판정 스키마 모양인가 (FN-01·FN-02).

    파일이 git 추적 대상인지는 프로젝트가 있어야 알 수 있어 여기서 보지
    않는다 — `_mutant_target_ok` 가 프로젝트를 받아 따로 본다.
    """
    r = f.get("replay")
    if not isinstance(r, dict):
        return False
    repro = r.get("repro")
    if not isinstance(repro, dict):
        return False
    if not isinstance(repro.get("cmd"), str) or not repro["cmd"].strip():
        return False
    if not isinstance(repro.get("rc"), int) or isinstance(repro.get("rc"), bool):
        return False
    mutants = r.get("mutants", [])
    if not isinstance(mutants, list):
        return False
    for m in mutants:
        if not isinstance(m, dict):
            return False
        for k in ("file", "from", "to", "test"):
            if not isinstance(m.get(k), str):
                return False
        if not m["file"] or not m["from"]:
            return False
    return True


def _mutant_target_ok(project, rel_path):
    """변이 대상이 프로젝트 안의 git 추적 파일인가."""
    if os.path.isabs(rel_path) or ".." in rel_path.replace("\\", "/").split("/"):
        return False
    r = subprocess.run(["git", "ls-files", "--error-unmatch", rel_path],
                       cwd=project, capture_output=True)
    return r.returncode == 0


def can_replay(project, findings, checks_present):
    """이 지적들을 재생해도 되는가 — (가능한가, 안 되면 이유) (FN-02).

    지적 **모두**가 올바른 모양의 `replay` 를 가지고, 프로젝트 설정에 전체
    시험 명령이 있어야 한다. 하나라도 걸리면 재생하지 않고 지금처럼 08
    세션을 띄운다.
    """
    if not checks_present:
        return False, "전체 시험 설정(checks)이 없다"
    if not findings:
        return False, "재생할 지적이 없다"
    for f in findings:
        if not replay_shape_ok(f):
            return False, "재생 형태가 없는 지적: %s" % f.get("where", "?")
        for m in f["replay"].get("mutants", []):
            if not _mutant_target_ok(project, m["file"]):
                return False, "변이 대상이 추적되지 않는다: %s" % m["file"]
    return True, None


def _run_repro(finding, repro, project, timeout):
    """재현 명령 하나 (FN-03)."""
    try:
        p = subprocess.run(repro["cmd"], shell=True, cwd=project,
                           capture_output=True, text=True, timeout=timeout)
        rc, tail = p.returncode, ((p.stdout or "") + (p.stderr or ""))
    except subprocess.TimeoutExpired:
        rc, tail = None, "시간 상한(%d초)을 넘겼다" % timeout
    return {"kind": "재현", "where": finding.get("where", ""), "cmd": repro["cmd"],
           "expect": repro["rc"], "actual": rc, "ok": rc == repro["rc"],
           "tail": "\n".join(tail.strip().splitlines()[-6:])}


def _run_mutant(finding, mutant, project, timeout):
    """변이 하나 — 넣고, 시험을 돌리고, 무슨 일이 있어도 원본으로 되돌린다 (FN-03).

    `from` 이 파일에 정확히 한 번 나오지 않으면 건드리지 않고 「기대와 다름」이다.
    """
    path = os.path.join(project, mutant["file"])
    base = {"kind": "변이", "where": finding.get("where", ""), "file": mutant["file"],
           "test": mutant["test"], "expect": "실패(0 아님)"}
    try:
        with open(path, "rb") as fh:
            original = fh.read()
        text = original.decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return dict(base, actual=None, ok=False, tail="파일을 읽지 못했다 — %s" % e)
    if text.count(mutant["from"]) != 1:
        return dict(base, actual=None, ok=False,
                   tail="from 문자열이 정확히 한 번 나오지 않는다")
    mutated = text.replace(mutant["from"], mutant["to"], 1).encode("utf-8")
    rc, tail = None, ""
    try:
        with open(path, "wb") as fh:
            fh.write(mutated)
        try:
            p = subprocess.run(mutant["test"], shell=True, cwd=project,
                               capture_output=True, text=True, timeout=timeout)
            rc = p.returncode
            tail = "\n".join(((p.stdout or "") + (p.stderr or "")).strip()
                             .splitlines()[-6:])
        except subprocess.TimeoutExpired:
            tail = "시간 상한(%d초)을 넘겼다" % timeout
    finally:
        with open(path, "wb") as fh:                # 무슨 일이 있어도 원본으로
            fh.write(original)
    return dict(base, actual=rc, ok=(rc is not None and rc != 0), tail=tail)


def run_replay(project, findings, timeout=REPLAY_TIMEOUT):
    """지적마다 재현·변이를 재생한다. (모두 기대대로인가, 결과 목록, 트리 불일치 상세) (FN-03·FN-04).

    변이 하나를 되돌릴 때마다 실행 디렉터리 밖 작업 트리가 재생 시작 전과
    같은지 본다. 다르면 그 자리에서 멈추고 더 돌리지 않는다 — 세 번째 값이
    상세 문자열이면 호출하는 쪽이 즉시 BREAK 해야 한다는 뜻이다.
    """
    results = []
    baseline_mod, _ = tree_outside_run(project)
    for f in findings:
        r = f["replay"]
        results.append(_run_repro(f, r["repro"], project, timeout))
        for m in r.get("mutants", []):
            results.append(_run_mutant(f, m, project, timeout))
            now_mod, _ = tree_outside_run(project)
            if now_mod != baseline_mod:
                extra = sorted(now_mod - baseline_mod)
                return False, results, ("재생 뒤 작업 트리가 원래대로가 아니다: %s"
                                        % ", ".join(extra))
    return all(x["ok"] for x in results), results, None


def prior_verdict_path(wd, num, attempt, resumed_fix_this_run):
    """이 재검증 차례가 다시 봐야 할 앞 반려 판정 파일 경로. 없으면 None (FN-02).

    시도 2회차 이상이면 반려 처리가 `.r{attempt-1}` 로 옮겨 둔 파일이고,
    시도 1회차인데 이어받기 수정을 이번 실행에서 했으면 제자리의 파일(수정
    전 반려가 그대로 남아 있다)이다. 그 밖(진짜 1회차)에는 다시 볼 반려가
    없다.
    """
    if attempt >= 2:
        p = verdict_path(wd, num) + ".r%d" % (attempt - 1)
    elif attempt == 1 and resumed_fix_this_run:
        p = verdict_path(wd, num)
    else:
        return None
    return p if os.path.exists(p) else None


def write_replay_pass(wd, num, results):
    """재생이 모두 기대대로일 때 08 세션 없이 남기는 통과 흔적 (FN-05).

    통과 판정 파일과 빈 지적은 기존 통과와 같은 모양이라 이어받기·09·11 이
    그대로 읽는다. 문서는 사람이 읽는 것이라 산출물 틀을 거치지 않는다
    (재생은 세션이 아니라 이 함수가 쓰기 때문이다).
    """
    with open(verdict_path(wd, num), "w", encoding="utf-8") as f:
        json.dump({"verdict": "통과", "findings": []}, f, ensure_ascii=False, indent=2)
    n_repro = sum(1 for r in results if r["kind"] == "재현")
    n_mut = sum(1 for r in results if r["kind"] == "변이")
    lines = ["# %s — 러너 재생" % STAGE_LABEL.get(num, num), "", "판정: 통과", "",
             "08 세션 없이 러너 재생으로 판정했다 — 앞 반려의 재현 명령과 변이를 "
             "세션 없이 그대로 다시 돌렸고 모두 기대대로였다.", "", "## 재생 결과", ""]
    for r in results:
        cmd = r.get("cmd") or r.get("test", "")
        lines.append("- [%s] %s — 명령: `%s` 기대: %s 실제: %s"
                     % (r["kind"], r.get("where", ""), cmd, r["expect"], r["actual"]))
    with open(os.path.join(wd, "%s-qa.md" % num), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(wd, "%s-summary.txt" % num), "w", encoding="utf-8") as f:
        f.write("재생으로 통과 — 재현 %d건·변이 %d건" % (n_repro, n_mut))


def full_suite_line(who, commit, secs):
    """러너가 이미 돌린 전체 시험 한 줄 — 08 위임문 덩이와 재생 결과 덩이가 같은 모양을 쓴다."""
    return "  - %s: 커밋 %s · 통과 · %d초" % (who, commit, secs)


def head_commit(project):
    """HEAD 전체 해시. 못 읽으면 빈 문자열 — 틀린 값을 싣느니 안 싣는다."""
    h = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project,
                       capture_output=True, text=True)
    return h.stdout.strip() if h.returncode == 0 else ""


def replay_failed_block(results, machine_failed):
    """재생이 실패했을 때 위임문 끝에 붙이는 「[재생 결과]」 덩이 (FN-06)."""
    lines = ["\n[재생 결과] 러너가 앞 반려의 재생을 먼저 돌렸고 다음이 기대와 달랐다 — "
            "참고만 하고 직접 다시 확인한다:"]
    for r in results:
        if not r["ok"]:
            cmd = r.get("cmd") or r.get("test", "")
            lines.append("- [%s] %s — 명령: `%s` 기대: %s 실제: %s\n  %s"
                         % (r["kind"], r.get("where", ""), cmd, r["expect"],
                            r["actual"], r.get("tail", "")))
    for x in machine_failed:
        lines.append("- [전체 시험] `%s` rc=%d\n  %s" % (x["cmd"], x["rc"], x["tail"]))
    return "\n".join(lines) + "\n"


# 워크트리가 이어받지 못하는 것 — 추적되지 않는 파일이라 새 폴더에는 없다.
# 없으면 08 품질 확인이 도구를 못 찾아 실패한다. 링크로 때운다.
# ponytail: 셋 고정. 이걸로 모자란 프로젝트가 나오면 그때 2층 설정으로 뺀다.
CARRY = ["node_modules", ".env", ".env.local"]


def carry_in(root, path):
    """추적되지 않는 준비물을 잇는다 — 새 폴더에는 없어서 08 이 도구를 못 찾는다.

    **gitignore 대상만 잇는다.** 아니면 `git add -A` 가 링크를 커밋에 담는다.
    """
    for name in CARRY:
        src = os.path.join(root, name)
        if not os.path.exists(src) or os.path.lexists(os.path.join(path, name)):
            continue
        if subprocess.run(["git", "check-ignore", "-q", name], cwd=root).returncode:
            print("       ! %s 가 gitignore 대상이 아니라 잇지 않는다" % name)
            continue
        os.symlink(src, os.path.join(path, name))


# 지식 그래프는 CARRY 처럼 링크로 잇지 않는다 — 워크트리에서 갱신해도
# 본체가 안 바뀌어야 한다(ISSUE-small-fixes-2026-09-17 완료 조건). 그래서
# `copytree` 로 따로 둔다. 그래프 갱신·낡음 판단은 범위 밖이라 다루지 않는다.
GRAPH_DIR = "graphify-out"


def carry_graph(root, path):
    """본체의 지식 그래프를 워크트리에 복사한다 — 없으면, 이미 있으면,
    무시 대상이 아니면 아무것도 하지 않는다.
    """
    src = os.path.join(root, GRAPH_DIR)
    if not os.path.isdir(src) or os.path.lexists(os.path.join(path, GRAPH_DIR)):
        return
    if subprocess.run(["git", "check-ignore", "-q", GRAPH_DIR], cwd=root).returncode:
        return                                # 무시 대상이 아니면 복사본이 커밋된다
    shutil.copytree(src, os.path.join(path, GRAPH_DIR))


# graft 코드 색인 — `.gitignore` 대상이라 새 워크트리에는 없다. 없으면 에이전트의
# `graft grep` 이 매번 실패하고 조용히 grep 으로 돌아가 이득이 소리 없이 사라진다.
# `GRAFT_DIR`·`--dir` 로 본체 색인을 보게 하는 안은 본체 색인이 워크트리 파일로
# 고쳐 쓰여 버렸다(실측) — 그래서 워크트리 안에서 따로 만든다.
INDEX_DIR = "graft"


def carry_index(root, path):
    """워크트리 안에 graft 색인을 만든다 — graft 가 없거나, 본체가 색인을 안 쓰거나,
    이미 있거나, 워크트리에서 무시 대상이 아니면 아무것도 하지 않는다. 실패해도
    경고만 찍고 계속한다: 에이전트는 `graft grep` 이 안 되면 `grep` 으로 넘어간다.

    무시 대상 검사는 **워크트리에서** 한다 — 스테이지 커밋의 `git add -A` 가 따르는
    것이 워크트리의 `.gitignore` 라서, 본체에서 검사하면 규칙이 아직 커밋 안 된
    저장소에서 색인 폴더가 통째로 커밋된다. 폴더가 아직 없으므로 끝에 `/` 를 붙여야
    `/graft/` 규칙이 걸린다. `--no-ignore --no-gitignore` 는 graft 가 `.ignore` 를
    만들고 `.gitignore` 를 고치는 것을 막는다(둘 다 추적되는 파일이다).
    """
    if (not shutil.which("graft") or not os.path.isdir(os.path.join(root, INDEX_DIR))
            or os.path.lexists(os.path.join(path, INDEX_DIR))):
        return
    if subprocess.run(["git", "check-ignore", "-q", INDEX_DIR + "/"], cwd=path).returncode:
        return
    try:
        r = subprocess.run(["graft", "build", "--no-ignore", "--no-gitignore", "."],
                           cwd=path, capture_output=True, text=True)
    except OSError as e:
        print("       ! graft 색인을 만들지 못했다: %s" % e)
        return
    if r.returncode:
        print("       ! graft 색인을 만들지 못했다 (종료 코드 %d)" % r.returncode)


def search_tools(project):
    """이 실행에서 검색 도구가 실제로 쓰일 수 있는지 한 줄로 말한다.

    둘 다 `.gitignore` 대상이라 꾸러미로도 저장소로도 옮겨지지 않는다. 없으면
    에이전트는 아무 말 없이 `grep` 으로 돌아가므로(정의의 `# Code search`),
    없다는 것이 화면에 보이지 않으면 조용히 비싸게 검색한다.
    """
    if not shutil.which("graft"):
        first = "graft 없음"
    elif os.path.isdir(os.path.join(project, INDEX_DIR)):
        first = "graft 색인 있음"
    else:
        first = "graft 색인 없음"
    report = os.path.join(project, GRAPH_DIR, "GRAPH_REPORT.md")
    try:
        day = dt.date.fromtimestamp(os.path.getmtime(report)).isoformat()
        second = "graphify 그래프 %s" % day
    except OSError:
        second = "graphify 그래프 없음"
    return "%s · %s" % (first, second)


def wrong_account(project):
    """원격에 못 닿은 이유가 gh 활성 계정이면 그렇게 말한다.

    `Repository not found` 는 저장소가 없을 때와 **볼 권한이 없을 때** 둘 다
    나온다. 계정은 호스트당 하나인 전역 상태여서(hq CLAUDE.md §4) 다른 회사
    작업을 하다 오면 조용히 틀린 채로 남는다. 2026-09-09 실측: 같은 소유자의
    저장소 둘이 있는데 활성 계정이 다른 소유자의 것이라 저장소가 없는 것처럼
    보였다.

    **전환은 하지 않는다.** 다른 세션이 그 계정으로 일하고 있을 수 있다.
    """
    url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=project,
                         capture_output=True, text=True).stdout.strip()
    m = re.search(r"[:/]([^/]+)/[^/]+?(?:\.git)?$", url)
    if not m or shutil.which("gh") is None:
        return []
    owner = m.group(1)
    st = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    text = st.stdout + st.stderr
    if owner not in text:
        return []
    active = re.search(r"account (\S+) \([^)]*\)\s*\n\s*- Active account: true", text)
    if active and active.group(1) == owner:
        return []
    return ["원격은 `%s` 소유인데 gh 활성 계정이 `%s` 다 — 사람이 바꾼다: "
            "`gh auth switch --user %s`"
            % (owner, active.group(1) if active else "알 수 없음", owner)]


def base_branch(heads):
    """기준 가지 이름을 고른다 — `handoff` 와 병합(`merge_base`)이 같은 규칙을
    쓴다(03 답변 1(나)). 원격 가지 목록에서 develop, main, master 순서로 처음
    있는 것, 없으면 None."""
    return next((b for b in ("develop", "main", "master") if b in heads), None)


def merge_base(project, issue_id):
    """인계 전에 원격 기준 가지를 병합한다 — 이슈 「원하는 것」 2번.

    결과 사전을 돌려준다: `result`(no-remote·remote-fail·no-base·merged·
    up-to-date·conflict 중 하나) 와 `base`·`sha`·`files`·`lines`(있을 때만).

    **merge 만 쓴다.** rebase·강제 push·로컬 기준 가지 병합은 하지 않는다
    (03 답변 1(가)·2, 04 울타리). 기준 가지 이름은 `projects.yaml` 에서
    읽지 않는다(영역 A26 제외 유지) — `base_branch` 로 `handoff` 와 규칙을 공유한다.
    """
    def git(*args):
        return subprocess.run(("git",) + args, cwd=project,
                              capture_output=True, text=True)

    def tail(r):
        return (r.stderr.strip().splitlines() or r.stdout.strip().splitlines()
                or [""])[-1]

    if git("remote", "get-url", "origin").returncode:
        return {"result": "no-remote"}

    ls = git("ls-remote", "--heads", "origin")
    if ls.returncode:
        return {"result": "remote-fail", "lines": [tail(ls)] + wrong_account(project)}

    heads = {l.rsplit("refs/heads/", 1)[-1] for l in ls.stdout.splitlines()
             if "refs/heads/" in l}
    base = base_branch(heads)
    if base is None:
        return {"result": "no-base"}

    fetch = git("fetch", "origin", base)
    if fetch.returncode:
        return {"result": "remote-fail", "lines": [tail(fetch)] + wrong_account(project)}

    sha = git("rev-parse", "FETCH_HEAD").stdout.strip()
    before = git("rev-parse", "HEAD").stdout.strip()
    base_ref = "origin/%s" % base
    # --no-ff 로 빨리 감기를 막는다 — 없으면 이미 앞서 있는 경우 병합 커밋 없이
    # 가지 끝만 옮겨져 「병합은 커밋으로 남긴다」가 보장되지 않는다(06 반려 2회차).
    subject = "10 인계: 기준 가지 병합 — %s@%s" % (base_ref, sha[:7])
    message = "%s\n\nLoop-Issue: %s\nLoop-Stage: 10-merge" % (subject, issue_id)
    m = git("merge", "--no-ff", "--no-edit", "-m", message, "FETCH_HEAD")
    result = {"base": base_ref, "sha": sha}
    if m.returncode == 0:
        after = git("rev-parse", "HEAD").stdout.strip()
        result["result"] = "up-to-date" if after == before else "merged"
        return result

    unmerged = git("diff", "--name-only", "--diff-filter=U").stdout.splitlines()
    result["files"] = unmerged
    result["result"] = "conflict"
    if not unmerged:
        # 충돌 경로 없는 실패(예: 로컬 변경이 덮일 위험) — 작업자 경로에 태우되
        # 사유는 lines 에 남긴다.
        result["lines"] = [tail(m)]
    return result


def upload_markers(project, base_sha, ref="HEAD"):
    """올릴 결과물의 충돌 표식 검사 — push 직전의 마지막 그물(F39, 사람 지시 1~3).

    `git diff --check` 로 새로 들어간 줄만 본다. 이 명령은 공백 문제도 같은
    종료 코드 2 로 내므로, 출력 줄 가운데 `leftover conflict marker` 로 끝나는
    줄만 문제로 센다(05 설계 §1 확인 6). `.workflow/`(실행 디렉터리, 표식을
    인용할 수 있는 문서)는 뺀다. `base_sha` 가 없으면(`no-base`) 빈 트리와
    비교한다 — 커밋이 아니라 `...` 를 쓸 수 없다.

    `ref` 는 올릴 가지다 — `HEAD` 가 아니라 `handoff` 가 이름으로 올리는 가지를
    봐야 작업자가 가지를 옮겨도 검사 대상과 올리는 대상이 같다(08 발견 1).
    속성은 빈 트리에서 읽는다(`--attr-source`, git 2.40+) — `.gitattributes` 의
    `-diff`·`binary` 파일은 `--check` 가 건너뛰기 때문이다(08 발견 2).
    ponytail: `$GIT_DIR/info/attributes` 는 이 방법으로 못 끈다. 옛 git 은
    종료 코드 129 라 「검사를 돌리지 못했다」로 멈춘다.

    돌려주는 것은 문제 줄 목록이다. 빈 목록이면 올려도 된다.
    """
    empty = subprocess.run(["git", "hash-object", "-t", "tree", "/dev/null"],
                           cwd=project, capture_output=True, text=True).stdout.strip()

    def git(*args, **kw):
        env = dict(os.environ, LC_ALL="C")
        return subprocess.run(("git", "--attr-source=%s" % empty,
                               "-c", "core.attributesFile=/dev/null") + args,
                              cwd=project, capture_output=True, text=True, env=env, **kw)

    if base_sha:
        rng = ["%s...%s" % (base_sha, ref)]
    else:
        rng = [empty, ref]

    r = git("diff", "--check", *rng, "--", ".", ":(exclude).workflow")
    if r.returncode == 0:
        return []
    if r.returncode != 2:
        tail = (r.stderr.strip() or r.stdout.strip()).splitlines()
        return ["표식 검사를 돌리지 못했다 — %s" % (tail[-1] if tail else "알 수 없는 오류")]

    suffix = "leftover conflict marker"
    problems = []
    for line in r.stdout.splitlines():
        if line.endswith(suffix):
            where = line[:-len(suffix)].rstrip().rstrip(":").rstrip()
            problems.append("충돌 표식이 올릴 커밋에 있다: %s" % where)
    return problems


PR_DRAFT_PREFIX = "PR 을 초안으로 열었다"


ACCOUNT_FAIL = "GitHub 계정 확인 실패"


def gh_token(account):
    """`gh auth token --user <계정>` 의 토큰. 못 얻으면 None — `gh()` 를 거치지 않는다(토큰을 얻기 전이다)."""
    try:
        # 사람이 띄울 때 넣은 토큰이 이 질문의 답을 바꾸지 않게 환경에서 뺀다.
        env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
        res = subprocess.run(["gh", "auth", "token", "--user", account],
                             capture_output=True, text=True, env=env)
    except OSError:
        return None
    return (res.stdout.strip() or None) if res.returncode == 0 else None


def gh(project, *args):
    """`gh` 를 부르는 유일한 자리 — 시험이 이 함수를 바꿔 끼워 실제 GitHub 에 닿지 않는다.

    프로젝트 설정에 계정 키가 있으면 그 계정의 토큰만 `GH_TOKEN` 에 넣는다 — 사람이 띄울 때 넣은
    값은 쓰지 않는다. 토큰을 못 얻었으면 지금 토큰으로 떨어지지 않고 실행하지 않는다.
    """
    env = None
    if _gh_account:
        if not _gh_token:
            return subprocess.CompletedProcess(
                ["gh", *args], 1, "", "GitHub 계정 %s 의 토큰을 얻지 못했다" % _gh_account)
        env = dict(os.environ, GH_TOKEN=_gh_token)
    return subprocess.run(["gh", *args], cwd=project, capture_output=True, text=True, env=env)


def github_login_problem(project):
    """계정 키가 있을 때 로그인 계정이 그 계정인지 확인한다. 같거나 키가 없으면 None."""
    if not _gh_account:
        return None
    res = gh(project, "api", "user", "--jq", ".login")
    login = res.stdout.strip()
    if res.returncode or not login:
        return "%s — 설정 계정 %s, 로그인을 확인하지 못했다: %s" % (
            ACCOUNT_FAIL, _gh_account,
            (res.stderr.strip().splitlines() or ["응답 없음"])[-1])
    if login != _gh_account:
        return "%s — 설정 계정 %s, 실제 로그인 %s" % (ACCOUNT_FAIL, _gh_account, login)
    return None


def handoff(project, issue_id, body_path, mode="pr"):
    """가지를 올리고 PR 을 초안으로 연다 — 반자동. 준비됨으로 바꾸고 병합하는 것은
    소각 뒤 `ready_and_merge` 의 일이다(기록 41).

    못 하면 조용히 넘기지 않고 무엇이 없어서 못 했는지 돌려준다. 인계가 말없이
    실패하면 결과가 이 기계에만 남고 아무도 모른다.

    **gh 계정을 전환하지 않는다.** 계정은 호스트당 하나인 전역 상태여서
    (hq CLAUDE.md §4) 전환이 조용히 실패하면 남의 계정으로 나간다. 지금 계정으로
    안 되면 사람에게 남긴다.

    강제 push 는 어느 경우에도 하지 않는다 — 되돌리기 어려운 쪽이다.
    """
    branch = "loop/%s" % issue_id

    def git(*args):
        return subprocess.run(("git",) + args, cwd=project,
                              capture_output=True, text=True)

    if mode == "local":
        # 원격을 건드리지 않는다. 결과는 이 기계의 가지에 있고, 올릴지 병합할지는
        # 사람이 정한다. `bin/status.sh` 는 프로젝트 디렉터리의 현재 HEAD 와 그
        # 상류만 보므로, 워크트리에서 만들어져 완주 뒤 워크트리가 치워지는 이
        # 가지는 잡지 못한다 — 안 올린 가지는 이 함수가 돌려주는 문장(인계
        # 본문)으로만 드러난다(ISSUE-handoff-defects-2026-09-18).
        return ["가지 `%s` 를 남겼다 — 인계 방식이 local 이다" % branch,
                "올리려면: git push -u origin %s" % branch,
                "바로 병합하려면: git merge --no-ff %s" % branch,
                "**올리는 것도 병합도 사람이 한다.**"]

    def tail(r):
        return (r.stderr.strip().splitlines() or r.stdout.strip().splitlines()
                or [""])[-1]

    if git("remote", "get-url", "origin").returncode:
        return ["원격이 없다 — 가지 `%s` 는 이 기계에만 있다" % branch]

    ls = git("ls-remote", "--heads", "origin")
    if ls.returncode:
        return ["원격에 닿지 못했다 — %s" % tail(ls)] + wrong_account(project)

    heads = {l.rsplit("refs/heads/", 1)[-1] for l in ls.stdout.splitlines()
             if "refs/heads/" in l}
    problem = github_login_problem(project) if mode in ("pr", "push") else None
    if problem:
        return [problem]
    push = git("push", "-u", "origin", branch)
    if push.returncode:
        return ["가지를 올리지 못했다 — %s" % tail(push)]
    out = ["가지 `%s` 를 올렸다" % branch]

    base = base_branch(heads)
    if base is None:
        out.append("PR 을 열지 않았다 — 원격에 base 가 될 가지가 없다 "
                   "(있는 것: %s)" % (", ".join(sorted(heads)) or "없음"))
        return out
    if mode == "push":
        out.append("PR 을 열지 않았다 — 인계 방식이 push 다. 사람이 연다: "
                   "%s → %s" % (branch, base))
        return out
    if shutil.which("gh") is None:
        out.append("PR 을 열지 않았다 — gh 가 없다. 사람이 연다: "
                   "%s → %s" % (branch, base))
        return out

    # 제목의 `[loop]` 는 표식이다. 머지 커밋에 따라붙어
    # `git log <base> --grep '\[loop\]'` 로 기계가 낸 것만 골라 되돌릴 수 있다
    # (01-loop-design §12-1 의 처방).
    pr = gh(project, "pr", "create", "--draft", "--base", base, "--head", branch,
            "--title", "[loop] %s" % issue_id, "--body-file", body_path)
    if pr.returncode:
        out.append("PR 을 열지 못했다 — %s" % tail(pr))
        return out
    out.append("%s (base %s) — %s" % (PR_DRAFT_PREFIX, base, pr.stdout.strip()))
    out.append("회고·소각 뒤 준비됨으로 바꾼다")
    return out


def repush(project, issue_id, mode):
    """소각 뒤 가지를 강제 없이 한 번 더 올린다 — 회고·소각 커밋이 인계(10)의
    push 한 번 뒤에 생겨 원격에 못 올라가던 구멍(이슈 「배경」 3)을 막는다.

    `local` 이면 원격을 건드리지 않는다(기록 37 결정 1). 강제 push 는 하지
    않는다 — 사람이 그 사이 원격 가지에 올렸으면 거부되고, 그때는 알리고
    끝낸다(기록 32, 외부 장애에 복구 장치를 만들지 않는다).
    """
    if mode == "local":
        return []
    branch = "loop/%s" % issue_id

    def git(*args):
        return subprocess.run(("git",) + args, cwd=project,
                              capture_output=True, text=True)

    def tail(r):
        return (r.stderr.strip().splitlines() or r.stdout.strip().splitlines()
                or [""])[-1]

    if git("remote", "get-url", "origin").returncode:
        return ["원격이 없다 — 소각 커밋은 이 기계의 가지에만 있다"]

    push = git("push", "-u", "origin", branch)
    if push.returncode:
        return ["소각 뒤 가지를 올리지 못했다 — %s" % tail(push)]
    return ["소각 뒤 가지를 다시 올렸다"]


# ── 병합 판정 (기록 41) ──────────────────────────────────────────────
# 보호 경로 — 하나라도 건드리면 사람이 병합한다. 기록 41 「보호 경로」와 같은 목록이다.
PROTECTED_PREFIXES = ("workflows/stop-loop/templates/", "workflows/stop-loop/engine/",
                      ".claude/hooks/", "dotfiles/hooks/")
PROTECTED_EXACT = ("bin/runner.py", "workflows/STANDARD.md", "stop-loop.config.json")
PROTECTED_PATTERNS = (r"workflows/stop-loop/agents/verifier-[^/]*\.md",
                      r"\.claude/agents/verifier-[^/]*\.md",
                      r"\.claude/settings[^/]*\.json",
                      r"workflows/\d+-[^/]*\.md")
JUDGE_STAGES = ("02", "06", "08", "09")


def is_protected(path):
    return (path in PROTECTED_EXACT or path.startswith(PROTECTED_PREFIXES)
            or any(re.fullmatch(p, path) for p in PROTECTED_PATTERNS))


def scenario_path():
    """PM 에게 싣는 시나리오 파일 경로 — 카드가 확정·구현 중인 시나리오를 가리킬 때만, 아니면 None.
    카드가 있는 저장소의 `plan/` 에서 찾는다(`scenario_of` 와 같은 규칙, 작업 폴더가 아니다)."""
    if not scenario_of()[0]:
        return None
    plan = os.path.join(os.path.dirname(os.path.dirname(_backlog_path)), "plan")
    for name in sorted(glob.glob(os.path.join(plan, backlog_get("시나리오") + "-*.md"))):
        return name
    return None


def scenario_of():
    """카드 머리말 `시나리오:` 가 확정·구현 중인 시나리오를 가리키는가 — `(시나리오 카드인가, 판단표 줄)`.

    시나리오 파일은 카드 폴더의 형제 `plan/` 의 `<번호>-*.md`, 상태는 표 줄 `| 상태 | <값> |` 의 값이
    「확정」·「구현 중」으로 시작하는지로 가른다(`bin/board.py` `read_scenarios` 와 같은 규칙).
    읽지 못하면 시나리오 카드가 아니다(엄한 쪽). `시나리오:` 가 비었으면 줄도 없다."""
    num = backlog_get("시나리오")
    if not num:
        return False, None
    no = "시나리오 카드가 아니다: %s " % num
    tail = " — 기록 41 조건으로 판정했다"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", num) or not _backlog_path:
        return False, no + "를 읽지 못했다" + tail
    plan = os.path.join(os.path.dirname(os.path.dirname(_backlog_path)), "plan")
    status = None
    for name in sorted(glob.glob(os.path.join(plan, num + "-*.md"))):
        try:
            with open(name, encoding="utf-8") as fh:
                for line in fh:
                    cells = [c.strip() for c in line.strip().strip("|").split("|")]
                    if line.startswith("|") and len(cells) >= 2 and cells[0] == "상태":
                        status = cells[1]
                        break
        except OSError:
            pass
        if status is not None:
            break
    if status is None:
        return False, no + "의 상태를 읽지 못했다" + tail
    word = status.split(" (")[0]
    if status.startswith(("확정", "구현 중")):
        return True, "시나리오 카드: %s (%s)" % (num, word)
    return False, no + "가 %s이다" % word + tail


# 되돌리기 어려운 변경 판정 (SC-01 E-4) — 10 이 쓰고 소각 뒤 재판정이 이 머리로 이어받는다.
IRREVERSIBLE_REASON = "되돌리기 어려운 변경을"
IRREVERSIBLE_HEAD = "되돌리기 어려운 변경"


def irreversible_items(wd):
    """07 「## 되돌리기 어려운 변경」 절의 `- <부류>: <근거>` 항목 `[(부류, 근거)]`.
    항목이 없고 첫 내용 줄이 정확히 「없음」이면 `[]`. 그 밖 — 파일·절 없음, 빈 절, 안내문만,
    줄글 — 은 모두 `None`(모름)이다: 빈 목록이 아니므로 병합 쪽으로 새지 않는다. 첫 절만 읽는다."""
    try:
        with open(os.path.join(wd, "07-changes.md"), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    sec = re.search(r"^## %s[ \t]*$(.*?)(?=^#{1,2} |\Z)" % IRREVERSIBLE_HEAD, text, re.S | re.M)
    if not sec:
        return None
    lines = [l for l in sec.group(1).splitlines() if l.strip()]
    items = []
    for l in lines:
        m = re.match(r"- ([^:]+):\s*(.+)$", l)
        if m and not m.group(1).startswith("{"):
            items.append((m.group(1).strip(), m.group(2).strip()))
    if items:
        return items
    first = re.sub(r"^- ", "", lines[0]).strip() if lines else ""
    return [] if first == "없음" else None


def declared_irreversible():
    """카드 머리말 `되돌리기:` 가 승인 때 밝힌 부류 집합. 빈 값·「없음」은 빈 집합."""
    value = backlog_get("되돌리기")
    return {x.strip() for x in value.split(",") if x.strip()} - {"없음"}


def irreversible_check(actual, declared):
    """실제(`irreversible_items`)와 밝힌 부류를 견준다 — `(까닭 목록, 판단표 줄)`."""
    fmt = lambda xs: ", ".join(sorted(xs)) or "없음"
    if actual is None:
        reason = "%s 판정하지 못했다 — 07 「%s」 절이 없거나 읽을 항목이 없다" % (IRREVERSIBLE_REASON, IRREVERSIBLE_HEAD)
        return [reason], "%s: 실제 판정하지 못함 · 밝힌 것 %s" % (IRREVERSIBLE_HEAD, fmt(declared))
    real = {k for k, _ in actual}
    undeclared, unused = real - declared, declared - real
    reasons = (["%s을 승인 때 밝히지 않았다: %s" % (IRREVERSIBLE_HEAD, ", ".join(sorted(undeclared)))]
               if undeclared else [])
    return reasons, ("%s: 실제 %s · 밝힌 것 %s · 밝히지 않은 것 %s · 밝혔지만 없던 것 %s"
                     % (IRREVERSIBLE_HEAD, fmt(real), fmt(declared), fmt(undeclared), fmt(unused)))


def merge_reasons(files, rounds, size, resumed, scenario=False):
    """기록 41 의 자동 병합 조건 가운데 못 채운 것을 까닭 문장으로 돌려준다.
    빈 목록이면 자동 병합 대상이다. 크기는 정확히 `S` 일 때만 채운 것이다.
    시나리오 카드(`scenario`)는 반려·크기 까닭을 내지 않는다(SC-01 E-4)."""
    reasons = []
    hit = [f for f in files if is_protected(f)]
    if hit:
        reasons.append("보호 경로를 건드렸다: %s" % ", ".join(hit))
    if scenario:
        rounds, size = [], "S"
    if rounds:
        per = {}
        for x in rounds:
            per[x.get("stage")] = per.get(x.get("stage"), 0) + 1
        reasons.append("반려가 있었다: %s" % ", ".join(
            "%s %d회" % (k, v) for k, v in sorted(per.items(), key=lambda kv: str(kv[0]))))
    if size != "S":
        reasons.append("크기가 S 가 아니다: %s" % (size or "없음"))
    if resumed:
        reasons.append("이어받은 실행이다")
    return reasons


def changed_files(project, base_sha, ref="HEAD"):
    """`base_sha` 뒤 `ref` 에서 바뀐 파일. `.workflow/`(실행 디렉터리)는 뺀다 —
    `upload_markers` 와 같은 범위·같은 제외 규칙이다. `base_sha` 가 없으면 빈 트리와 비교한다."""
    if not base_sha:
        base_sha = subprocess.run(["git", "hash-object", "-t", "tree", "/dev/null"],
                                  cwd=project, capture_output=True, text=True).stdout.strip()
        rng = [base_sha, ref]
    else:
        rng = ["%s...%s" % (base_sha, ref)]
    d = subprocess.run(["git", "diff", "--name-only", *rng, "--", ".", ":(exclude).workflow"],
                       cwd=project, capture_output=True, text=True)
    return d.stdout.splitlines()


def qa_skip_reason(project):
    """08 을 건너뛸 까닭 — 바뀐 파일이 모두 `.md` 이고 세 자리(에이전트 정의·산출물 틀·
    SKILL.md)가 없으면 「문서만 바뀜」. 목록을 못 얻으면 None(모르면 돈다)."""
    try:
        d = subprocess.run(["git", "for-each-ref", "--format=%(refname:lstrip=3)",
                            "refs/remotes/origin"], cwd=project, capture_output=True, text=True)
        base = base_branch(set(d.stdout.split())) if d.returncode == 0 else None
        if not base:
            return None
        files = [f for f in changed_files(project, "origin/" + base)
                 if not f.startswith("docs/backlog/")]
    except OSError:
        return None
    # 빈 목록은 판단 근거가 아니다 — git 실패도 빈 목록으로 돌아온다
    if not files:
        return None
    guarded = (".claude/agents/", "workflows/stop-loop/agents/", "workflows/stop-loop/templates/")
    for f in files:
        if not f.endswith(".md") or f.startswith(guarded) or os.path.basename(f) == "SKILL.md":
            return None
    return "문서만 바뀜"


def merge_table(reasons, files, rounds, auto, stages=None, notes=None):
    """PR 본문 맨 위에 쓰는 병합 판단표 — 사람이 왜 병합해야 하는지 한눈에 본다."""
    out = ["## 병합 판단", ""]
    out += ["- %s" % x for x in reasons] or ["- 자동 병합 대상"]
    hit = [f for f in files if is_protected(f)]
    out.append("- 건드린 보호 경로: %s" % (", ".join(hit) if hit else "없음"))
    out += ["- %s" % x for x in notes or []]
    if not auto:
        out.append("- 이 프로젝트는 자동 병합이 꺼져 있다 — 사람이 병합한다")
    out += ["", "| 스테이지 | 반려 회차 | 지적 수 |", "|---|---|---|"]
    for num in JUDGE_STAGES:
        st = (stages or {}).get(num) or {}
        if st.get("status") == "skipped":   # 건너뛴 단계는 통과가 아니다 — 숫자 대신 까닭
            out.append("| %s | 돌지 않음%s | — |" % (num, " — " + st["reason"] if st.get("reason") else ""))
            continue
        mine = [x for x in rounds if x.get("stage") == num]
        out.append("| %s | %d | %d |" % (num, len(mine),
                                         sum(len(x.get("findingKeys") or []) for x in mine)))
    pairs = []
    for st in (stages or {}).values():
        if st.get("merged") and st["merged"] not in pairs:
            pairs.append(st["merged"])
    if pairs:
        out += ["", "묶인 세션(규모 S): %s — 02·06 판정은 위 표에 따로 남는다"
                % " · ".join(pairs)]
    return "\n".join(out) + "\n\n"


def ready_and_merge(project, issue_id, repush_lines, pr_opened, state, models):
    """소각 뒤 PR 을 준비됨으로 바꾸고, 설정이 켜졌고 조건을 모두 채우면 병합한다.

    `(찍을 줄, 멈춤 까닭 또는 None)` 을 돌려준다. 다시 올리지 못했거나(PR 이 아직
    소각 전 상태다) 이 실행이 PR 을 안 열었으면 `gh` 를 부르지 않는다. 준비됨·병합이
    실패하면 PR 을 그대로 두고 멈춘다 — 재시도·되돌리기는 하지 않는다(기록 32).
    """
    branch = "loop/%s" % issue_id
    if not pr_opened:
        return [], None
    if repush_lines and repush_lines[0].startswith("소각 뒤 가지를 올리지 못했다"):
        return [], "준비됨으로 바꾸지 못했다 — %s" % repush_lines[0]
    if repush_lines != ["소각 뒤 가지를 다시 올렸다"]:
        return [], None                       # 원격이 없거나 local — PR 을 만질 수 없다

    def tail(x):
        return (x.stderr.strip().splitlines() or x.stdout.strip().splitlines() or [""])[-1]

    base = (state.get("merge") or {}).get("base") or "origin/main"
    base_name = base.split("/", 1)[-1]
    target = branch                    # pr view·comment·merge 가 받는 대상
    known = (state.get("merge") or {}).get("reasons") or []
    human = [x for x in known if x.startswith((HUMAN_REASON, OLD_HUMAN_REASON, IRREVERSIBLE_REASON))]   # 이른 병합이 known 을 비우기 전에
    ready = gh(project, "pr", "ready", branch)
    if ready.returncode:
        stop = "준비됨으로 바꾸지 못했다 — %s" % tail(ready)
        # 이른 병합이면 소각만 반영하는 후속 PR 을 연다 — 상태는 한 번만 읽는다.
        lst = gh(project, "pr", "list", "--head", branch, "--state", "all",
                 "--json", "number,state,mergedAt")
        try:
            prs = json.loads(lst.stdout) if lst.returncode == 0 else []
            first = max(prs, key=lambda x: x["number"]) if prs else None
        except (ValueError, TypeError, KeyError):
            first = None
        if not first or not first.get("mergedAt"):
            return [], stop
        head = "PR #%s 이 러너 마무리 전에 병합됐다" % first["number"]
        fetch = subprocess.run(["git", "fetch", "origin", base_name],
                               cwd=project, capture_output=True, text=True)
        if fetch.returncode:
            return [], "PR #%s 이 이미 병합됐지만 기준 가지를 받지 못했다 — 후속 PR 을 열지 않았다" % first["number"]
        titles = subprocess.run(["git", "log", "--reverse", "--format=%s", "FETCH_HEAD..HEAD"],
                                cwd=project, capture_output=True, text=True).stdout.splitlines()
        if not titles:
            return [head + " — 남은 커밋이 없다 — 후속 PR 이 필요 없다"], None
        outside = changed_files(project, "FETCH_HEAD", "HEAD")
        if outside:
            return [], ("PR #%s 이 이미 병합됐지만 가지에 실행 디렉터리 밖 변경이 남았다 — %s"
                        % (first["number"], ", ".join(outside)))
        body = ("첫 PR #%s 이 %s 에 병합됐다. 그때 러너는 마무리(11 회고·소각) 중이었고 "
                "병합 뒤 가지에 남은 커밋은 다음과 같다(오래된 것부터).\n\n%s\n\n"
                "main 에 대한 변경은 실행 디렉터리(`.workflow/`) 삭제뿐이다.\n\nLoop-Issue: %s"
                % (first["number"], first["mergedAt"], "\n".join("- " + t for t in titles), issue_id))
        pr = gh(project, "pr", "create", "--base", base_name, "--head", branch,
                "--title", "[loop] %s — 병합 뒤 남은 소각 반영" % issue_id, "--body", body)
        if pr.returncode:
            return [], "후속 PR 을 열지 못했다 — %s" % tail(pr)
        target = pr.stdout.strip().splitlines()[-1]
        known = []                     # 후속 PR 에는 까닭 전부를 댓글로 단다
        out = [head, "후속 PR 을 열었다 — %s" % target]
    else:
        out = ["PR 을 준비됨으로 바꿨다"]
    if not models.get("_autoMerge"):
        return out, None

    # 소각 뒤·11 회고 커밋까지 든 파일로 다시 판정한다 — 10 판단표는 그 앞에서 썼다.
    reasons = []
    fetch = subprocess.run(["git", "fetch", "origin", base_name],
                           cwd=project, capture_output=True, text=True)
    if fetch.returncode:
        files, reasons = [], ["건드린 파일을 다시 세지 못했다"]
    else:
        files = changed_files(project, "FETCH_HEAD", "HEAD")
        reasons = merge_reasons(files, state.get("rounds", []), backlog_get("크기"),
                                bool(state.get("resumed")), scenario=scenario_of()[0])
    reasons = reasons + human
    if not reasons:
        for wait in range(7):     # ponytail: 5초 × 6번 쉼. 벽시계를 읽지 않고 횟수로만 센다
            view = gh(project, "pr", "view", target, "--json", "mergeable", "-q", ".mergeable")
            value = view.stdout.strip() if view.returncode == 0 else ""
            if value in ("MERGEABLE", "CONFLICTING") or wait == 6:
                break
            time.sleep(5)
        if value == "CONFLICTING":
            reasons = ["충돌이 있다"]
        elif value != "MERGEABLE":
            reasons = ["충돌 판정을 받지 못했다"]
    if reasons:
        fresh = [x for x in reasons if x not in known]
        for x in reasons:
            out.append("자동 병합하지 않는다 — %s" % x)
        if fresh:
            c = gh(project, "pr", "comment", target, "--body",
                   "자동 병합하지 않는다 — 사람이 병합한다:\n" + "\n".join("- " + x for x in fresh))
            if c.returncode:
                out.append("PR 댓글을 달지 못했다 — %s" % tail(c))
        return out, None

    m = gh(project, "pr", "merge", target, "--merge",
           "--subject", "[loop] %s — 자동 병합" % issue_id,
           "--body", "기록 41 조건을 모두 채워 러너가 병합했다\n\nLoop-Issue: %s" % issue_id)
    if m.returncode:
        return out, "병합하지 못했다 — %s" % tail(m)
    out.append("자동 병합했다")
    return out, None


def start_point(root):
    """새 작업 폴더의 시작점 — 원격 기준 가지를 가져온 최신 커밋 sha, 원격이 없으면 None.

    원격이 있는데 읽지 못하거나 기준 가지가 없으면 멈춘다 — 낡은 로컬 기준 가지로
    조용히 폴백하면 앞 카드의 병합이 빠진 바탕에서 일하게 된다(SC-01 E-3, 03 답변 Q1).
    본체 작업 트리는 건드리지 않는다: 가져오기만 하고 FETCH_HEAD 를 시작점으로 쓴다.
    """
    def git(*args):
        return subprocess.run(("git",) + args, cwd=root, capture_output=True, text=True)

    def stop(why, res=None):
        tail = ((res.stderr.strip().splitlines() or res.stdout.strip().splitlines()
                 or [""])[-1] if res else "")
        sys.exit("원격 기준 가지에서 작업 폴더를 열지 못했다 — %s%s\n%s"
                 % (why, ": " + tail if tail else "", "\n".join(wrong_account(root))))

    if git("remote", "get-url", "origin").returncode:
        return None
    ls = git("ls-remote", "--heads", "origin")
    if ls.returncode:
        stop("원격을 읽지 못했다", ls)
    heads = {l.rsplit("refs/heads/", 1)[-1] for l in ls.stdout.splitlines()
             if "refs/heads/" in l}
    base = base_branch(heads)
    if base is None:
        stop("원격에 기준 가지(develop·main·master)가 없다")
    fetch = git("fetch", "origin", base)
    if fetch.returncode:
        stop("원격에서 가져오지 못했다", fetch)
    return git("rev-parse", "FETCH_HEAD").stdout.strip()


def open_worktree(root, issue_id, resume):
    """이슈마다 자기 작업 폴더를 연다 — 같은 저장소, 다른 자리.

    한 체크아웃에서 여럿이 돌면 `git add -A` 가 남의 변경까지 담고 검증자가
    자기가 만들지 않은 것을 검증한다 (tenk 2026-09 실측). 워크트리는 규율이
    아니라 구조로 막는다 — **같은 브랜치를 두 곳에서 열면 git 이 거부한다.**
    그래서 따로 잠금 파일을 두지 않는다. git 이 이미 잠금이다.
    """
    path = "%s.%s" % (root.rstrip("/"), issue_id)
    if os.path.isdir(path):
        if resume:
            carry_in(root, path)             # 닫을 때 끊었으므로 다시 잇는다
            carry_graph(root, path)
            carry_index(root, path)
            print("작업 폴더 %s  (이어받음)" % path)
            return path
        sys.exit("이미 있는 작업 폴더다: %s\n"
                 "앞 실행이 남긴 것이면 사람이 보고 치운다 — "
                 "git worktree remove %s" % (path, path))
    start = start_point(root)
    r = subprocess.run(["git", "worktree", "add", "-b", "loop/%s" % issue_id, path]
                       + ([start] if start else []),
                       cwd=root, capture_output=True, text=True)
    if r.returncode:
        sys.exit("작업 폴더를 열지 못했다:\n%s" % (r.stderr.strip() or r.stdout.strip()))
    carry_in(root, path)
    carry_graph(root, path)
    carry_index(root, path)
    print("작업 폴더 %s  (가지 loop/%s)" % (path, issue_id))
    return path


def close_worktree(root, path):
    """커밋 안 된 것이 남았으면 지우지 않는다 — 그게 사람이 볼 증거다.

    가지는 남긴다. 결과가 거기 있으므로 지우면 실행 전체가 사라진다.
    """
    for name in CARRY:                       # 링크는 우리가 만들었으니 우리가 치운다
        link = os.path.join(path, name)
        if os.path.islink(link):
            os.unlink(link)
    r = subprocess.run(["git", "worktree", "remove", path],
                       cwd=root, capture_output=True, text=True)
    if r.returncode:
        print("       ! 작업 폴더를 남겨둔다 — %s"
              % (r.stderr.strip().splitlines() + [""])[0])
        return
    print("       작업 폴더 정리 — 결과는 가지에 있다")


# `handoff` 가 가지를 올리기 전에 끝나면 이 머리 중 하나로 시작하는 문장을 돌려준다
# (ISSUE-handoff-defects-2026-09-18). 가지가 이미 올라간 뒤의 결과(PR 못 엶 등)와
# `local` 방식의 문장은 여기 없다 — 그 경우는 멈추지 않는다.
HANDOFF_FAIL_PREFIXES = ("원격이 없다", "원격에 닿지 못했다", "가지를 올리지 못했다", ACCOUNT_FAIL)


def stage_handoff(project, wd, issue, issue_id, state, models, events, outp, config_dir):
    """10 인계 — 기준 가지를 병합해 보고, 기계 검사와 올릴 커밋의 충돌 표식을 본다.
    하나라도 걸리면 올리지 않고 멈춰 사람에게 넘긴다. 통과하면 지금처럼 인계한다.

    통과한 뒤에도 `handoff` 가 가지를 올리기 전에 끝나면(원격 없음·원격에 닿지
    못함·push 거부) 같은 자리에서 멈춘다 — 반환값과 무관하게 통과로 적고
    `HANDOFF` 를 쏘던 결함(ISSUE-handoff-defects-2026-09-18)을 막는다. `local`
    방식은 이 문장들을 돌려주지 않으므로 멈추지 않는다.

    충돌 해결을 작업자에게 맡기지 않는다 — 머지는 사람이 하고, 충돌은 사람이
    몇 분이면 푼다(기록 32). 종료 코드 0 은 인계를 마쳤다는 뜻이다.
    """
    statef = os.path.join(wd, "state.json")
    branch = "loop/%s" % issue_id
    m = merge_base(project, issue_id)
    result = m.get("result")
    ev = {"stage": "10", "merge": result, "base": m.get("base"), "sha": m.get("sha")}
    for k in ("files", "lines"):
        if m.get(k):
            ev[k] = m[k]
    stoploop.append_event(events, ev)

    def stop(reason, problems, code, title="사람이 볼 차례", detail=None, turn=None):
        """기존 인계 게이트 실패 흐름 — 병합·검사 실패와 가지를 못 올린 경우가
        함께 쓴다. `10-handoff.md` 를 남기지 않아야 이어받기가 10 을 건너뛰지
        않고 다시 병합·인계를 시도한다."""
        for x in problems:
            print("       ! %s" % x)
        if os.path.exists(outp):
            os.remove(outp)
        with open(os.path.join(wd, "10-merge.md"), "w", encoding="utf-8") as f:
            f.write("# 10 병합 — %s\n\n기준 가지: %s\n해시: %s\n결과: %s\n\n문제:\n"
                    % (title, m.get("base"), m.get("sha"), result))
            for x in problems:
                f.write("- %s\n" % x)
        stoploop.append_event(events, {"stage": "10", "gate": "handoff", "problems": problems})
        state["stages"]["10"] = {"status": "failed", "reason": reason}
        state["breaker"] = {"kind": "dod-miss", "stage": "10", "detail": detail or problems[0]}
        fire(state, "BREAK", {}, events, **({"turn": turn} if turn else {}))
        save_state(statef, state)
        print("\n중단 — %s. %s (10-merge.md)." % (
            reason, "기준 가지가 고쳐지면 --resume 으로 다시 띄운다" if turn else "사람이 볼 차례다"))
        return code

    problems = []
    split = None
    if result == "remote-fail":
        problems = m.get("lines") or ["원격을 읽지 못함"]
    elif result == "conflict":
        subprocess.run(["git", "merge", "--abort"], cwd=project, capture_output=True)
        problems = ["충돌: %s" % f for f in m.get("files", [])] + m.get("lines", [])
    elif result in ("merged", "up-to-date", "no-base"):
        failed = []
        if result != "no-base":
            ok, failed = machine_checks(project, models)
        marks = upload_markers(project, m.get("sha"), branch)
        problems += ["기계 검사 실패: %s (rc=%d)\n         %s"
                     % (x["cmd"], x["rc"], x["tail"].replace("\n", "\n         "))
                     for x in failed]
        if failed and not marks:
            # 실패가 카드 탓인지 기준 가지 탓인지 가른다 — 충돌 표식이 있으면 지금처럼 사람 차례다.
            split = classify_failures(project, m.get("sha"), failed, models)
            if split["error"]:
                problems.append("기준 가지 판을 만들지 못했다 — %s" % split["error"])
        problems += marks
    # no-remote 는 검사하지 않는다 — 올릴 곳이 없다.

    if split and not split["error"] and split["card"]:
        # 합친 판에서만 실패하는 검사가 하나라도 있으면 카드 탓이다 — 기다려도 안 고쳐지므로 07 이 고친다.
        for x in problems:
            print("       ! %s" % x)
        if os.path.exists(outp):
            os.remove(outp)
        findings = [{"kind": "machine-check", "where": x["cmd"],
                     "what": (x["tail"].strip().splitlines() or [""])[-1]} for x in split["card"]]
        with open(os.path.join(wd, "10-merge.md"), "w", encoding="utf-8") as f:
            f.write("# 10 병합 — 07 이 고칠 차례\n\n기준 가지: %s\n해시: %s\n결과: %s\n\n## 고칠 것\n\n"
                    % (m.get("base"), m.get("sha"), result))
            for x in split["card"]:
                f.write("- 기계 검사 실패: %s (rc=%d)\n         %s\n"
                        % (x["cmd"], x["rc"], x["tail"].replace("\n", "\n         ")))
            if split["base"]:
                f.write("\n## 기준 가지에서도 실패 — 참고\n\n")
                for x in split["base"]:
                    f.write("- 기계 검사 실패: %s (rc=%d)\n" % (x["cmd"], x["rc"]))
        stoploop.append_event(events, {"stage": "10", "gate": "handoff-card-fault",
                                       "problems": [x["cmd"] for x in split["card"]]})
        state["fix10"] = findings
        print("\n10 검사 실패가 카드 탓이다 — 07 작성자가 고친 뒤 10 을 다시 돈다 (10-merge.md).")
        return "07"

    if problems:
        base_only = bool(split and not split["error"] and split["base"] and not split["card"])
        return stop("인계 전 병합·검사 실패", problems, 7 if result == "remote-fail" else 2,
                    title="기준 가지를 기다릴 차례" if base_only else "사람이 볼 차례",
                    detail=("기준 가지에서도 실패하는 검사 — 기준 가지가 고쳐지면 --resume (%s)"
                            % split["base"][0]["cmd"]) if base_only else None,
                    turn="러너" if base_only else None)

    files = changed_files(project, m.get("sha"), branch)
    scenario, line = scenario_of()
    reasons = merge_reasons(files, state.get("rounds", []), backlog_get("크기"),
                            bool(state.get("resumed")), scenario=scenario)
    human = human_items(wd)
    notes = [line] if line else []
    if scenario:
        # 07 「참고 사항」은 병합과 무관한 메모라 병합을 막지 않는다 — 03a·07a 사람 결정만 센다.
        counted = [x for x in human if x[0] in ("03a", "07a")]
        why, row = irreversible_check(irreversible_items(wd), declared_irreversible())
        reasons = reasons + why
        notes += [row, "반려 이력은 판정에 쓰지 않고 아래 표에 보이기만 한다"]
    else:
        counted = human
    if counted:
        reasons = reasons + ["%s: %d건" % (HUMAN_REASON, len(counted))]
    with open(outp, "w", encoding="utf-8") as f:
        f.write(merge_table(reasons, files, state.get("rounds", []),
                            bool(models.get("_autoMerge")), state["stages"], notes=notes))
        f.write("## 사람 결정 미결\n\n%s\n\n"
                % ("\n".join("- %s: %s" % x for x in human) if human else "없음"))
        f.write("# 인계\n\n스테이지 결과:\n")
        for k, v in state["stages"].items():
            f.write("- %s: %s%s%s\n" % (k, v["status"],
                    " (%s)" % v["reason"] if v.get("reason") else "",
                    " [%s 한 세션]" % v["merged"] if v.get("merged") else ""))
    lines = handoff(project, issue_id, outp, models.get("_handoff", "pr"))
    for line in lines:
        print("       %s" % line)
        with open(outp, "a", encoding="utf-8") as f:
            f.write("\n- %s" % line)

    if lines and lines[0].startswith(HANDOFF_FAIL_PREFIXES):
        return stop("가지를 올리지 못함", lines, 7)

    state["stages"]["10"] = {"status": "passed"}
    # 소각 뒤 `ready_and_merge` 가 읽는다. stages["10"] 은 기존 시험이 통째로 비교하므로 따로 둔다.
    state["merge"] = {"base": m.get("base"), "reasons": reasons}
    # 자동 병합 대상(병합 까닭이 없고 설정이 켜짐)이면 사람 차례가 아니다 — 상태 「검토」와 한 번에 표시한다.
    fire(state, "HANDOFF", {"dod_pass": True}, events,
         **({"turn": "러너"} if models.get("_autoMerge") and not reasons else {}))
    print("  %-4s %-14s 작성 — 상태 %s" % ("10", "handoff", state["state"]))
    commit_stage(project, wd, "10", issue_id, 1, None, None, outp)
    return 0


def missing_commands():
    """이 기계에 없는 실행 명령 이름 — 있으면 러너가 시작하지 않는다.

    순서는 항상 git, gh, python3, claude 다(ISSUE-env-contract-2026-09-17).
    앞 셋은 PATH 조회, `claude` 는 러너가 실제로 부르는 고정 경로(`CLAUDE`)가
    실행 가능한지로 본다 — PATH 의 `claude` 는 계정 선택 별칭이라 실제 호출과
    다를 수 있다(workflows/36-env-contract.md). 이 목록 중 실제로 시작을 막는 것은
    `blocking` 이 인계 방식으로 정한다.
    """
    missing = [name for name in ("git", "gh", "python3") if shutil.which(name) is None]
    if not os.access(CLAUDE, os.X_OK):
        missing.append("claude")
    return missing


def blocking(missing, handoff):
    """`missing_commands()` 결과 중 실제로 시작을 막는 것만 — 원래 순서 그대로.

    `gh` 는 인계 방식이 pr 일 때만 막는다. push 는 `git push` 만 쓰고 local 은 그 자리를
    지나간다(ISSUE-doctor-followups-2026-09-21). 러너 `main` 과 점검 명령(`doctor.py`)이
    이 하나의 규칙을 같이 쓴다.
    """
    return [n for n in missing if n != "gh" or handoff == "pr"]


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("issue")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resume", action="store_true",
                    help="산출물이 있는 스테이지는 건너뛴다 (게이트는 통과한 것만)")
    ap.add_argument("--screen", action="store_true", help="화면 영역이 켜진 작업")
    ap.add_argument("--config-dir", default="~/.claude")
    a = ap.parse_args(argv)

    root = os.path.abspath(os.path.expanduser(a.project))
    models = load_models(root)               # 설정은 가지가 아니라 기계의 것이다
    missing = blocking(missing_commands(), models.get("_handoff", "pr"))
    if missing:
        sys.exit("시작하지 않는다 — 이 기계에 없는 명령: %s" % ", ".join(missing))

    issue = os.path.abspath(os.path.expanduser(a.issue))
    issue_id = os.path.splitext(os.path.basename(issue))[0]

    # 여기서부터 project 는 이 이슈만의 작업 폴더다. 아래 전부가 이 값을 쓴다.
    project = root if a.dry_run else open_worktree(root, issue_id, a.resume)

    # 이어받기는 이슈 이름으로 기존 실행 디렉터리를 찾는다 — 날짜가 바뀌어도
    # 앞 실행의 산출물과 상태를 잇는다(ISSUE-resume-and-merge). 이름 형식
    # (run_dir)은 바꾸지 않는다 — 후보가 없을 때만 오늘 날짜로 새로 만든다.
    picked, discarded = pick_run_dir(project, issue_id) if a.resume else (None, [])
    wd = picked if picked else run_dir(project, issue_id)
    # 고른 것이 없으면 run_dir 이 버린 후보 중 오늘 날짜 디렉터리를 그대로 쓴다 —
    # 쓰는 디렉터리를 「치울 후보」로 적지 않는다(08 반려 발견 2).
    discarded = [d for d in discarded if d != wd]

    models["_repo"] = os.path.basename(root)  # 신호 파일 이름 — 워크트리 이름이 아니다
    state = {"issue": issue_id, "state": stoploop.INITIAL, "stages": {},
             "counters": {}, "models": models, "resumed": bool(a.resume)}
    events = os.path.join(wd, "events.jsonl")
    global _events_path
    _events_path = events
    statef = os.path.join(wd, "state.json")
    if picked or discarded:
        stoploop.append_event(events, {"resume_dir": picked, "discarded": discarded})
    # 앞 실행이 10 수정 도중 끊기면(Ctrl-C·재시작) 병합이 진행 중인 채 남는다.
    # 그대로 두면 아래 「이어받음」 빈 커밋이 표식 검사 없이 그 병합을 확정한다
    # (08 반려 3회차 발견 1). 되돌리고, 10 이 병합부터 다시 한다.
    if a.resume and not a.dry_run and subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
            cwd=project, capture_output=True).returncode == 0:
        subprocess.run(["git", "merge", "--abort"], cwd=project, capture_output=True)
        print("  남은 병합을 되돌렸다 — 10 에서 다시 병합한다")
        stoploop.append_event(events, {"merge": "aborted-on-resume"})

    print("프로젝트 %s" % project)
    print("실행 디렉터리 %s" % wd)
    print("검색 도구 — %s\n" % search_tools(project))
    if a.resume and resume_state(state, statef):
        print("  상태 이어받음 — 회차 %d건, 상한 %s\n"
              % (len(state["rounds"]), state["counters"]))
    global _plugin_ver, _gh_account, _gh_token
    _plugin_ver = _gh_account = _gh_token = None      # 앞 main() 의 값을 물려받지 않는다
    if not a.dry_run:
        # 이어받아도 앞 실행의 판을 잇지 않는다 — 이어받는 그때의 판이 이 실행의 시작 때 판이다.
        _plugin_ver = plugin_version(a.config_dir)
        if _plugin_ver:
            state["plugin"] = _plugin_ver
            stoploop.append_event(events, {"plugin": _plugin_ver})
        if models.get("_githubAccount"):
            _gh_account = models["_githubAccount"]
            _gh_token = gh_token(_gh_account)
    global _status_ctx
    _status_ctx = None                # 앞 main() 의 값을 물려받지 않는다
    global _live_path
    _live_path = None
    if not a.dry_run:
        global _backlog_path
        _backlog_path = issue
        models.update(size_models(models, backlog_get("크기")))   # 크기 칸 — 단계 모델 고르기 앞에서 한 번
        _status_ctx = {"repo": os.path.basename(root), "branch": "loop/%s" % issue_id,
                       "worktree": project}
        if LIVE_DIR:
            _live_path = os.path.join(
                LIVE_DIR, "%s__%s.log" % (_status_ctx["repo"], issue_id))
        fire(state, "PICK", {"can_pick": True}, events)
        backlog_set(멈춘이유="")               # 시작은 PICK 전이가 찍는다

    def session(agent, prompt, cwd, num, stage_tag, fix_of=None):
        """세션을 띄우고 직전·직후 HEAD 를 대조한다 (FN-03).

        `call()` 을 그대로 감싼다 — 판정(`history_rewritten`)을 `call()` 안에
        넣지 않는 이유는 시험이 `r.call` 을 가짜로 바꿔 끼우기 때문이다.
        `state`·`events`·`statef` 를 감싸는 이유는 멈출 때 쓰는 이 값들이
        `main()` 의 지역이라서다. 다시 쓰였으면 이 자리에서 멈춤을 기록하고
        상태 `"rewritten"` 을 돌려준다(판이 바뀌어 멈추면 `"halted"`) — 호출하는 쪽은 이것만 보고 8 로 끝낸다.
        시간 초과와 겹쳐도 이력 재작성이 우선한다(증거가 사라지면 안 된다).
        """
        now = plugin_version(a.config_dir) if _plugin_ver else None
        if now and now != _plugin_ver:
            # 실행 도중 플러그인이 갱신됐다 — 앞 단계와 뒤 단계가 다른 정의로 돌지 않게 세션을 띄우기 전에 멈춘다.
            detail = ("플러그인 판이 바뀌었다: %s → %s — --resume 으로 이어받으면 새 판으로 돈다"
                      % (_plugin_ver, now))
            print("       ! %s" % detail)
            state["stages"][num] = {"status": "failed", "reason": "플러그인 판 바뀜"}
            state["breaker"] = {"kind": "dod-miss", "stage": stage_tag, "detail": detail}
            fire(state, "BREAK", {}, events)
            stoploop.append_event(events, {"stage": stage_tag, "breaker": "plugin-changed",
                                           "from": _plugin_ver, "to": now})
            save_state(statef, state)
            print("\n중단 — 플러그인 판이 바뀌었다. 이어받으면 새 판으로 돈다.")
            return "halted", "", None, 0.0
        before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                                capture_output=True, text=True).stdout.strip()
        LAST_RESULT.clear()           # 가짜 call 은 안 채운다 — 앞 세션 값을 물려받지 않게
        t0 = time.time()
        # 수정 세션(fix_of)은 고치는 산출물 단계의 모델로 돈다 — 판정 단계 열쇠를 따르지 않는다.
        # 비용·시간 집계는 판정 단계 칸(num)에 그대로 쌓이고 call() 의 인자 모양은 바뀌지 않는다.
        use = models if not fix_of else dict(models, **{num: models.get(fix_of) or models.get(agent)})
        status, log, why, spent = call(agent, prompt, cwd, a.config_dir, num, use)
        m = state.setdefault("metrics", {}).setdefault(
            num, dict(runs=0, ms=0, cost=0.0, **{k: 0 for k in TOKEN_KEYS}))
        ms = LAST_RESULT.get("ms")
        m["runs"] += 1
        m["ms"] += int((time.time() - t0) * 1000) if ms is None else ms
        m["cost"] = round(m["cost"] + spent, 4)
        for k in TOKEN_KEYS:
            m[k] += LAST_RESULT.get(k, 0)
        rewritten = history_rewritten(cwd, before)
        if rewritten:
            print("       ! 이력이 다시 쓰였다 — %s" % rewritten)
            state["stages"][num] = {"status": "failed", "reason": "이력 재작성"}
            state["breaker"] = {"kind": "dod-miss", "stage": stage_tag,
                                "detail": "이력 재작성: " + rewritten}
            fire(state, "BREAK", {}, events)
            stoploop.append_event(events, {"stage": stage_tag,
                                           "breaker": "history-rewritten",
                                           "detail": rewritten})
            save_state(statef, state)
            print("\n중단 — 이력이 다시 쓰였다. 사람이 볼 차례다.")
            return "rewritten", log, why, spent
        return status, log, why, spent

    # 규모 S 는 앞 단계 세션 하나가 뒤 단계 일까지 한다. dry-run 은 카드를 읽지 않는다.
    merged = (not a.dry_run) and merged_run()
    order = run_order(merged)
    prebuilt = {}      # 뒤 단계 번호 → 그 산출물을 이번 실행의 묶인 세션이 써 둔 앞 단계 (다음 한 시도는 세션 없이 소비)
    redo = set()       # 02 반려 되돌림 뒤 이어받기 판단에서 빼는 단계
    tried = {}         # 단계 → 이번 실행에서 쓴 마지막 시도 번호 (다시 들어오면 이어 센다)
    asked = set()      # 이번 실행에서 PM(07a)에게 이미 물은 정할 것 원문 — 같은 것은 두 번 묻지 않는다
    idx = 0
    while idx < len(order):
        num, name, agent, out, is_gate, cond = order[idx]
        idx += 1
        outp = os.path.join(wd, out)
        redone = num in redo
        redo.discard(num)
        mark = None      # 이번 실행의 묶인 세션이 쓴 단계면 "01+03"·"02+06"
        stage_model = models.get(num) or models.get(agent)

        why_skip = skip_reason(cond, a.screen or models.get("_screen", False),
                               models.get("_reviewPolicy", "size-m"))
        if num == "11" and not why_skip and not a.dry_run:
            why_skip = retro_skip_reason(state, wd)
        if num == "08" and not why_skip and not a.dry_run:
            why_skip = qa_skip_reason(project)
        if why_skip:
            state["stages"][num] = {"status": "skipped", "reason": why_skip}
            print("  %-4s %-14s 생략 — %s" % (num, name, why_skip))
            continue

        # 04 가 중단을 적은 채 남은 산출물은 끝난 것이 아니다 — 건너뛰면 멈춘
        # 실행을 이어받는 순간 05 가 돈다. 04 를 다시 돌려 새 판정을 받는다.
        # 07 은 판정 파일이 없어 산출물만으로는 게이트를 통과했는지 알 수 없다 —
        # 통과 기록(이력의 트레일러)이 있어야 건너뛴다.
        if a.resume and not redone and num not in prebuilt and os.path.exists(outp) and (
                not is_gate or read_verdict(wd, num)[0] == "통과") and not (
                num == "04" and scope_halted(outp)) and (
                num != "07" or has_07_pass_record(project, issue_id)):
            state["stages"][num] = {"status": "passed", "reason": "이어받음"}
            print("  %-4s %-14s 이어받음 — 산출물이 이미 있다" % (num, name))
            # 건너뛴 것도 진행 상황이다. 원장에 구멍이 나면 나중에 이력만 보고
            # 「02 는 왜 없지」를 되짚을 수 없다 (2026-09-01 실측: 실제로 비었다).
            subprocess.run(["git", "commit", "--allow-empty", "-q", "-F", "-"],
                           cwd=project, text=True, capture_output=True,
                           input="%s: 이어받음 — 앞 실행의 산출물을 그대로 쓴다\n\n"
                                 "Loop-Issue: %s\nLoop-Stage: %s\nLoop-Resumed: true\n"
                                 % (STAGE_LABEL.get(num, num), issue_id, num))
            continue

        # 반려로 멈춘 스테이지를 이어받으면 검증자보다 수정부터 부른다 —
        # 안 그러면 마지막 반려를 고치지 않은 산출물로 검증자를 다시 부르게
        # 된다(ISSUE-stop-by-count). 판정 파일이 없거나 통과이거나(위에서
        # 이미 건너뜀) 지적이 0건이면 여기 해당하지 않는다 — 넘길 수정
        # 입력이 없다.
        resume_fix = None
        if a.resume and not redone and num not in prebuilt and is_gate and os.path.exists(outp):
            v0, findings0 = read_verdict(wd, num)
            if v0 == "반려" and findings0:
                resume_fix = FIXERS.get(num)

        own_agent, own_model = agent, stage_model
        stage_cost = 0.0
        gate_extra = ""
        if num == "08" and state.get("gateRun"):
            g = state["gateRun"]
            gate_extra = ("\n러너가 이미 돌린 전체 시험 — HEAD 가 이 커밋이면 다시 "
                          "돌리지 않는다(정의 5번):\n%s\n"
                          % full_suite_line("07 게이트", g["commit"], g["secs"]))
        prompt = delegate_text(num, name, outp, issue, wd, extra=gate_extra,
                               models=models)

        if a.dry_run:
            print("  %-4s %-14s %-22s %-8s → %s"
                  % (num, name, agent_ref(agent) or "러너",
                     (models.get(num) or models.get(agent) or "상속")
                     if agent else "—", out))
            continue

        # 스테이지에 들어서는 순간 요약이 이 스테이지를 말하게 한다.
        state["stage"] = num
        save_state(statef, state)

        if num == "07" and not state.get("tddBase"):
            # 07 시작 지점 — 세션을 띄우기 전에 적고 곧바로 디스크에 쓴다. 시간 초과·
            # Ctrl-C·재부팅으로 끊겨도 첫 시작 지점이 남아, 이어받은 게이트가 앞 세션의
            # 시험 커밋을 본다. 이미 있으면(이어받음) 바꾸지 않는다.
            h = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project,
                               capture_output=True, text=True)
            if h.returncode == 0 and h.stdout.strip():
                state["tddBase"] = h.stdout.strip()
                save_state(statef, state)

        if agent is None:                      # 10 인계는 러너가 직접 쓴다
            # 인계 직전에 기준 가지를 병합해 보고, 충돌·검사 실패면 멈춰
            # 사람에게 넘긴다 — stage_handoff 가 원장·이벤트까지 남긴다
            # (ISSUE-resume-and-merge, 기록 32). 소각하지 않는다 — 11 회고가 이
            # 디렉터리를 읽어야 한다. 소각은 실행이 끝나는 자리의 일이다(아래).
            rc = stage_handoff(project, wd, issue, issue_id, state, models,
                               events, outp, a.config_dir)
            if rc == "07":
                # 합친 판에서만 실패한 검사 — 카드 탓이다. 08·09 반려와 같은 틀로 07 작성자가 고치고 10 을 다시 돈다.
                findings = state.pop("fix10")
                action, repeated, keys = round_decision(state, "10", findings)
                if action == "break":
                    why = "같은 지적 재발" if repeated else "검토 회차 상한"
                    state["stages"]["10"] = {"status": "failed", "reason": why}
                    state["breaker"] = {"kind": "rounds-exhausted", "stage": "10", "detail": why}
                    fire(state, "BREAK", {}, events)
                    save_state(statef, state)
                    print("\n중단 — 10 (%s). 사람이 볼 차례다." % why)
                    return 2
                fire(state, "GATE_FAIL", {"gate_retry_available": True}, events)
                tp = os.path.join(wd, "07-changes.md")
                fs, flog, fwhy, fspent = session("loop-worker", FIX_MSG.format(
                    num="10", review=os.path.join(wd, "10-merge.md"),
                    target=tp, issue=issue), project, "10", "10-fix", fix_of="07")
                state["cost"] = round(state.get("cost", 0.0) + fspent, 4)
                if fs in ("rewritten", "halted"):
                    return 8
                if fs == "timeout":
                    print("       ! 수정 세션이 멈췄다 — %s" % fwhy)
                    state["breaker"] = {"kind": "timeout", "detail": fwhy, "stage": "10-fix"}
                    fire(state, "BREAK", {}, events)
                    save_state(statef, state)
                    return 4
                commit_stage(project, wd, "10-fix", issue_id, len(state["rounds"]) + 1, "loop-worker",
                             models.get("07") or models.get("loop-worker"), tp,
                             used=LAST_RESULT.get("model"))
                oscillated = record_round(state, "10", len(state["rounds"]) + 1, keys, tp)
                state["counters"]["reviewRounds"] = len(state["rounds"])
                if oscillated:
                    state["stages"]["10"] = {"status": "failed", "reason": "진동"}
                    state["breaker"] = {"kind": "rounds-exhausted", "stage": "10", "detail": "진동"}
                    fire(state, "BREAK", {}, events)
                    save_state(statef, state)
                    return 2
                save_state(statef, state)
                idx -= 1                          # 10 을 처음부터 다시 돈다
                continue
            if rc:
                return rc
            continue

        if resume_fix:
            fa, target = resume_fix
            tp = os.path.join(wd, target)
            print("       이어받기 — 마지막 반려 지적으로 %s 부터 수정" % target)
            fs, flog, fwhy, fspent = session(fa, FIX_MSG.format(
                num=num, review=outp, target=tp, issue=issue),
                project, num, num + "-resume-fix", fix_of=target[:2])
            state["cost"] = round(state.get("cost", 0.0) + fspent, 4)
            stage_cost = round(stage_cost + fspent, 4)
            if fs in ("rewritten", "halted"):
                return 8
            if fs == "timeout":
                print("       ! 수정 세션이 멈췄다 — %s" % fwhy)
                state["breaker"] = {"kind": "timeout", "detail": fwhy,
                                    "stage": num + "-resume-fix"}
                fire(state, "BREAK", {}, events)
                save_state(statef, state)
                return 4
            commit_stage(project, wd, num + "-fix", issue_id, 0, fa,
                         models.get(target[:2]) or models.get(fa), tp, used=LAST_RESULT.get("model"))
            # 이어받은 수정은 회차로 세지 않는다 — rounds 에도 기록하지 않는다.
            # 회차는 복원된 값을 그대로 잇는다 (0 으로 돌리지 않는다).
            if merged and num in MERGED:
                # 묶인 실행의 02 반려 — 01 을 고쳤으니 그 위에 선 03a·05 도 새로 돌린다.
                n = free_suffix(outp)
                os.rename(outp, "%s.r%d" % (outp, n))
                if os.path.exists(verdict_path(wd, num)):
                    os.rename(verdict_path(wd, num), "%s.r%d" % (verdict_path(wd, num), n))
                drop_follower(wd, MERGED[num], n)
                redo.update(("03a", "04", "05", "05u", "02"))
                idx = [x[0] for x in order].index("03a")
                continue

        cards_before = list_cards(os.path.dirname(issue)) if num == "11" else None
        config_before = read_config(project) if num == "11" else None
        mprompt = None                         # 묶인 세션의 글 — 첫 시도에만 쓴다
        if merged and num in MERGED:
            f = [x for x in order if x[0] == MERGED[num]][0]
            mprompt = merged_delegate(prompt, delegate_text(
                f[0], f[1], os.path.join(wd, f[3]), issue, wd, models=models), f[0])
        base = tried.get(num, 0)
        for attempt in range(base + 1, base + 7):  # 상한은 엔진이 쥔다. 이건 안전망
            tried[num] = attempt
            backlog_set(단계="%s%s" % ((MERGED_LABEL[num] if mprompt else STAGE_LABEL.get(num, num)),
                                       " · %d회차" % attempt if attempt > 1 else ""))

            # 08 재검증을 세션 앞에서 재생한다 (F58, ISSUE-qa-recheck-replay-2026-09-28).
            # 다른 스테이지는 건드리지 않는다 — 이슈 「범위 밖」.
            replay_extra = ""
            if num == "08":
                prior = prior_verdict_path(wd, num, attempt, bool(resume_fix))
                if prior:
                    _, findings0 = _read_verdict_file(prior)
                    ok, why_not = can_replay(project, findings0,
                                             bool(models.get("_checks")))
                    if not ok:
                        print("       · 재생 건너뜀 — %s" % why_not, flush=True)
                    if ok:
                        print("  %-4s %-14s 재생 — 앞 반려 지적 %d건"
                              % (num, name, len(findings0)), flush=True)
                        tree_before = tree_outside_run(project)[0]
                        replay_ok, results, mismatch = run_replay(project, findings0)
                        if mismatch:
                            print("       ! %s" % mismatch)
                            state["stages"][num] = {"status": "failed",
                                                    "reason": "재생 뒤 작업 트리 불일치"}
                            state["breaker"] = {"kind": "dod-miss", "stage": num,
                                                "detail": mismatch}
                            fire(state, "BREAK", {}, events)
                            save_state(statef, state)
                            print("\n중단 — %s 재생 뒤 작업 트리가 원래대로가 "
                                  "아니다. 사람이 볼 차례다." % num)
                            return 3
                        t0 = time.monotonic()
                        machine_ok, machine_failed = machine_checks(project, models)
                        replay_secs = int(time.monotonic() - t0)
                        tree_changed = sorted(tree_outside_run(project)[0] - tree_before)
                        if replay_ok and machine_ok and tree_changed:
                            print("       ! 재생 중 추적 파일이 바뀌었다 — 재생 통과로 치지 "
                                  "않고 %s 세션을 띄운다: %s"
                                  % (num, ", ".join(tree_changed)))
                        elif replay_ok and machine_ok:
                            write_replay_pass(wd, num, results)
                            state["stages"][num] = {"status": "passed"}
                            fire(state, "STAGE_DONE", {"artifact_exists": True}, events)
                            stoploop.on_gate_pass(state["counters"], num)
                            print("       → %s  재생으로 통과 (세션 없음)" % outp)
                            commit_stage(project, wd, num, issue_id, attempt,
                                         "runner-replay", None, outp,
                                         "통과", [], cost=stage_cost)
                            break
                        if not (replay_ok and machine_ok):
                            print("       ! 재생 결과가 기대와 다르다 — %s 세션을 "
                                  "띄운다" % num)
                            replay_extra = replay_failed_block(results, machine_failed)
                            if machine_ok and not tree_changed:
                                # 전체 시험이 통과했고 트리도 그대로 — 08 이 다시 돌릴 필요가 없다
                                rh = head_commit(project)
                                if rh:
                                    replay_extra += full_suite_line(
                                        "08 재생", rh, replay_secs) + "\n"

            # 02 세션 직전에 후보를 새로 뽑는다 — 반려 뒤 재검증 회차도 매번 다시
            # 뽑는다(01 이 매니페스트를 고치면 후보가 달라진다, ISSUE-scope-omission-
            # candidates-2026-09-28).
            if num == "02":
                candidates, cand_err = scope_candidates(project, wd)
                first_line = write_candidates_file(wd, candidates, cand_err)
                print("       · %s" % first_line)

            before = tree_outside_run(project) if is_gate else (None, None)
            if num in prebuilt:                # 앞 단계 세션이 이미 썼다 — 세션 없이 검사만 한다
                lead_num, agent = prebuilt.pop(num)     # 커밋에는 실제로 돈 앞 단계를 적는다
                stage_model = models.get(lead_num) or models.get(agent)
                mark = "%s+%s" % (lead_num, num)
                status, log, why, spent = "ok", "", None, 0.0
            else:
                agent, stage_model = own_agent, own_model   # 반려 뒤 다시 보는 시도는 제 에이전트다
                mark = None
                print("  %-4s %-14s %s 기동…" % (num, name, agent), flush=True)
                if mprompt:
                    prebuilt[MERGED[num]] = (num, agent)
                    mark = "%s+%s" % (num, MERGED[num])
                status, log, why, spent = session(agent, mprompt or (prompt + replay_extra),
                                                  project, num, num)
                mprompt = None
            state["cost"] = round(state.get("cost", 0.0) + spent, 4)
            stage_cost = round(stage_cost + spent, 4)
            if cards_before is not None:        # 회고가 쓴 카드는 어떤 끝이든 아이디어로
                for name_ in demote_new_cards(os.path.dirname(issue), cards_before):
                    print("       · 회고가 쓴 카드 %s 의 상태를 아이디어로 되돌렸다" % name_)
            if restore_config(project, config_before):    # 값은 사람이 바꾼다
                print("       · 회고가 고친 %s 을 되돌렸다" % CONFIG_NAME)
            if status in ("rewritten", "halted"):
                return 8
            if status == "timeout":
                print("       ! 멈춤 — %s" % why)
                # 무엇을 하다 멈췄는지 남긴다. 안 남기면 다음에 할 수 있는 것이
                # 「상한을 올린다」뿐이고, 그건 원인을 모른 채 기다리는 것이다
                # (2026-09-09 실측: hq 01 이 30분에 걸렸는데 근거가 0이었다).
                stopped = os.path.join(wd, "%s-멈춤.log" % num)
                with open(stopped, "w", encoding="utf-8") as f:
                    f.write(redact_secrets(log))  # 꼬리에도 가리기 — 멈춤 기록은 커밋에 실린다
                print("       " + readable(log).strip().replace("\n", "\n       "))
                print("       마지막 출력 60줄: %s" % stopped)
                stoploop.append_event(events, {"stage": num, "breaker": "timeout",
                                               "detail": why})
                state["stages"][num] = {"status": "failed", "reason": "timeout"}
                state["breaker"] = {"kind": "timeout", "detail": why, "stage": num}
                fire(state, "BREAK", {}, events)
                save_state(statef, state)
                print("\n중단 — %s 세션이 응답을 멈췄다. 사람이 볼 차례다." % num)
                return 4
            if before[0] is not None:
                now_mod, now_new = tree_outside_run(project)
                for t in sorted(now_new - before[1]):
                    print("       · 새 파일 (경고만): %s" % t)
                touched = now_mod - before[0]
                if touched:
                    print("       ! 검증자가 기존 파일을 고쳤다:")
                    for t in sorted(touched):
                        print("         %s" % t)
                    state["stages"][num] = {"status": "failed",
                                            "reason": "검증자가 기존 파일 수정"}
                    state["breaker"] = {"kind": "dod-miss", "stage": num,
                                        "detail": "검증자가 기존 파일 수정"}
                    fire(state, "BREAK", {}, events)
                    save_state(statef, state)
                    return 3
            if not os.path.exists(outp):
                print("       산출물 없음 — 스테이지 미완료 (%s)" % status)
                print("       " + readable(log).strip().replace("\n", "\n       "))
                state["stages"][num] = {"status": "failed", "reason": "산출물 없음"}
                save_state(statef, state)
                return 1

            gaps = template_gaps(num, outp)
            if gaps:
                print("       ! 틀의 절이 비었다: %s" % ", ".join(gaps))
                stoploop.append_event(events, {"stage": num, "gate": "틀",
                                               "problems": gaps})
                if stoploop.on_gate_fail(state["counters"], num) == "break":
                    state["stages"][num] = {"status": "failed", "reason": "틀 미충족"}
                    state["breaker"] = {"kind": "dod-miss", "stage": num,
                                        "detail": "틀의 절이 비었다: %s" % ", ".join(gaps)}
                    fire(state, "BREAK", {}, events)
                    save_state(statef, state)
                    print("\n중단 — %s 산출물이 틀을 채우지 못했다. 사람이 볼 차례다." % num)
                    return 2
                prompt += ("\n\n[틀 미충족] 다음 절이 없다 — 제목 그대로 더해 채운다: %s"
                           % ", ".join(gaps))
                os.rename(outp, outp + ".r%d" % attempt)
                continue

            if not is_gate:
                pending = [d for d in decision_items(wd) if d not in asked] if num == "07" else []
                if pending:                    # 07 작업자가 정할 것을 남겼다 — 게이트 전에 PM 이 답한다 (E-11)
                    asked.update(pending)
                    ap = os.path.join(wd, "07-answers.md")
                    backlog_set(단계=STAGE_LABEL["07a"])
                    ptext = delegate_text("07a", "decide", ap, issue, wd, extra=DECIDE_EXTRA.format(
                        origin="%s · 07" % issue_id, answers=ap,
                        items="\n".join("  - " + d for d in pending)), models=models)
                    print("  %-4s %-14s loop-pm 기동…" % ("07a", "decide"), flush=True)
                    ps, plog, pwhy, pspent = session("loop-pm", ptext, project, "07a", "07a")
                    state["cost"] = round(state.get("cost", 0.0) + pspent, 4)
                    stage_cost = round(stage_cost + pspent, 4)
                    if ps in ("rewritten", "halted"):
                        return 8
                    if ps == "timeout" or not os.path.exists(ap):
                        why = pwhy if ps == "timeout" else "산출물 없음"
                        print("       ! 07a 멈춤 — %s" % why)
                        state["stages"]["07a"] = {"status": "failed", "reason": why}
                        state["breaker"] = {"kind": "timeout" if ps == "timeout" else "dod-miss",
                                            "detail": why, "stage": "07a"}
                        fire(state, "BREAK", {}, events)
                        save_state(statef, state)
                        print("\n중단 — 07a PM 세션이 답을 내지 못했다. 사람이 볼 차례다.")
                        return 4 if ps == "timeout" else 1
                    stoploop.append_event(events, {"stage": "07a", "decisions": len(pending)})
                    commit_stage(project, wd, "07a", issue_id, attempt, "loop-pm",
                                 models.get("loop-pm"), ap, cost=pspent, used=LAST_RESULT.get("model"))
                    prompt += ("\n\n[PM 답] 07-answers.md 의 답대로 잇는다. "
                               "사람 결정으로 올라간 결정은 구현하지 않는다")
                    os.rename(outp, outp + ".r%d" % attempt)
                    continue
                if num == "07":                # 테스트 주도 + 기계 검사 게이트
                    hints = ({"hints": models["_testPaths"]}
                             if "_testPaths" in models else {})
                    probs = tdd_evidence(project, wd, state.get("tddBase"), **hints)
                    t0 = time.monotonic()
                    ok, failed = machine_checks(project, models)
                    gate_secs = int(time.monotonic() - t0)
                    if not ok:
                        for x in failed:
                            probs.append("기계 검사 실패: %s (rc=%d)\n         %s"
                                         % (x["cmd"], x["rc"],
                                            x["tail"].replace("\n", "\n         ")))
                    if probs:
                        for x in probs:
                            print("       ! %s" % x)
                        stoploop.append_event(events, {"stage": "07",
                                                       "gate": "tdd",
                                                       "problems": probs})
                        if stoploop.on_gate_fail(state["counters"], "07") == "break":
                            state["stages"][num] = {"status": "failed",
                                                    "reason": "TDD 증거 없음"}
                            state["breaker"] = {"kind": "dod-miss", "stage": num,
                                                "detail": "TDD 증거 없음"}
                            fire(state, "BREAK", {}, events)
                            save_state(statef, state)
                            print("\n중단 — 07 테스트 주도 게이트. 사람이 볼 차례다.")
                            return 2
                        # 재시도 위임문에 막힌 이유를 넣는다(FN-01) — 안 넣으면
                        # 두 번째 시도가 첫 시도와 같은 지시를 받아 같은 곳에서
                        # 다시 걸린다(이슈 「배경」 1·2).
                        prompt += ("\n\n[07 게이트 실패] 앞 시도가 다음 이유로 "
                                  "막혔다 — 이것을 고친다:\n"
                                  + "\n".join("- " + p for p in probs))
                        os.rename(outp, outp + ".r%d" % attempt)
                        continue
                    print("       ✓ 테스트 주도 게이트 통과")
                state["stages"][num] = dict({"status": "passed"},
                                            **({"merged": mark} if mark else {}))
                # 회고(11)는 이슈의 생애주기를 움직이지 않는다 — 10 인계가 이미
                # review 로 보냈고, 되짚는 일은 그 상태를 바꾸지 않는다. 상태기는
                # 이슈의 것이지 스테이지의 것이 아니다 (2026-09-09 실측: 여기서
                # 「전이 거부: review 에서 STAGE_DONE」이 찍혔다).
                if num != "11":
                    fire(state, "STAGE_DONE", {"artifact_exists": True}, events)
                print("       → %s" % out)
                committed = commit_stage(project, wd, num, issue_id, attempt, agent,
                                         stage_model, outp,
                                         cost=stage_cost, used=LAST_RESULT.get("model"))
                if num == "07" and committed and models.get("_checks"):
                    # 설정에 checks 가 없으면 machine_checks 는 명령 없이 통과한다 —
                    # 안 돌린 시험을 「통과」로 싣지 않는다.
                    head = head_commit(project)
                    if head:
                        state["gateRun"] = {"commit": head, "ok": True,
                                            "secs": gate_secs}
                if num == "04" and scope_halted(outp):
                    # 커밋 뒤에 멈춘다 — 사람이 풀 조건은 04 본문에 있고 이력에 남아야 읽는다.
                    state["stages"][num] = {"status": "failed",
                                            "reason": "04 범위 통제 중단"}
                    state["breaker"] = {"kind": "dod-miss", "stage": num,
                                        "detail": "04 범위 통제 중단"}
                    fire(state, "BREAK", {}, events)
                    save_state(statef, state)
                    print("\n중단 — 04 범위 통제가 중단을 적었다. 풀려야 할 조건은 "
                          "%s 에 있다. 사람이 볼 차례다." % out)
                    return 2
                break

            v, findings = read_verdict(wd, num)
            stoploop.append_event(events, {"stage": num, "verdict": v,
                                           "findings": len(findings),
                                           "attempt": attempt})
            print("       → %s  판정: %s  발견 %d건"
                  % (out, v or "읽지 못함", len(findings)))
            # 「불명」은 반려가 아니라 「읽지 못했다」다. 되돌림으로 보내면
            # 통과한 산출물을 근거로 작업자가 멀쩡한 구현을 고치러 간다
            # (2026-08-31: `# 판정: 통과` 를 못 읽어 실제로 그 직전까지 갔다).
            if v is None:
                state["stages"][num] = {"status": "failed",
                                        "reason": "판정 파일을 읽지 못함"}
                state["breaker"] = {"kind": "verdict-unreadable", "stage": num,
                                    "detail": outp}
                fire(state, "BREAK", {}, events)
                save_state(statef, state)
                print("\n중단 — %s-verdict.json 이 없거나 형식이 아니다. 사람이 볼 차례다." % num)
                return 5

            if v == "통과":
                stoploop.on_gate_pass(state["counters"], num)
                state["stages"][num] = dict({"status": "passed"},
                                            **({"merged": mark} if mark else {}))
                fire(state, "STAGE_DONE", {"artifact_exists": True}, events)
                commit_stage(project, wd, num, issue_id, attempt, agent,
                             stage_model, outp,
                             v, findings, cost=stage_cost, used=LAST_RESULT.get("model"))
                break

            # 반려인데 지적이 없다 — 넘길 수정 입력이 없다. 회차로 세면
            # 같은 산출물로 검증만 되풀이하다 시도 6회를 공회전하고, 스테이지가
            # 기록에서 빠진 채 다음 스테이지로 넘어간다(ISSUE-stop-by-count).
            # 판정 파일을 읽지 못했을 때와 같은 방식으로 즉시 멈춘다.
            if not findings:
                state["stages"][num] = {"status": "failed",
                                        "reason": "반려인데 지적이 없음"}
                state["breaker"] = {"kind": "verdict-unreadable", "stage": num,
                                    "detail": "%s 반려인데 findings 가 비어 있음" % outp}
                fire(state, "BREAK", {}, events)
                save_state(statef, state)
                print("\n중단 — %s 반려인데 지적이 없다. 사람이 볼 차례다." % num)
                return 5

            # 같은 지적이 또 나왔는가 — 엔진은 재발 1회로 즉시 중단한다.
            # 「고쳤다」는 주장과 무관하게 결과로 판정한다.
            action, repeated, keys = round_decision(state, num, findings)
            seen = {k for r in state["rounds"] if r.get("stage") == num
                    for k in r.get("findingKeys", [])}
            if repeated:
                print("       ! 같은 지적이 다시 나왔다 — %d건"
                      % len(seen & set(keys)))
            if action == "break":
                why = "같은 지적 재발" if repeated else "검토 회차 상한"
                state["stages"][num] = {"status": "failed", "reason": why}
                state["breaker"] = {"kind": "rounds-exhausted", "stage": num,
                                    "detail": why}
                fire(state, "BREAK", {}, events)
                save_state(statef, state)
                print("\n중단 — %s (%s). 사람이 볼 차례다." % (num, why))
                return 2
            fire(state, "GATE_FAIL", {"gate_retry_available": True}, events)
            commit_stage(project, wd, num, issue_id, attempt, agent,
                         stage_model, outp, v, findings,
                         used=LAST_RESULT.get("model"))
            kept = outp + ".r%d" % attempt
            os.rename(outp, kept)                      # 반려 근거를 보존한다
            if os.path.exists(verdict_path(wd, num)):
                os.rename(verdict_path(wd, num),
                          verdict_path(wd, num) + ".r%d" % attempt)
            fixer = FIXERS.get(num)
            if fixer:
                fa, target = fixer
                tp = os.path.join(wd, target)
                # 고치기 전 판을 남긴다. 없으면 「무엇이 어떻게 바뀌었나」를
                # 되짚을 수 없고, 진동 감지도 못 한다 (엔진 detect_oscillation).
                if os.path.exists(tp):
                    shutil.copy(tp, tp + ".before-r%d" % attempt)
                print("       %s 로 되돌린다 — %s 수정" % (fa, target))
                fs, flog, fwhy, fspent = session(fa, FIX_MSG.format(
                    num=num, review=kept, target=os.path.join(wd, target),
                    issue=issue), project, num, num + "-fix", fix_of=target[:2])
                state["cost"] = round(state.get("cost", 0.0) + fspent, 4)
                stage_cost = round(stage_cost + fspent, 4)
                if fs in ("rewritten", "halted"):
                    return 8
                if fs == "timeout":
                    print("       ! 수정 세션이 멈췄다 — %s" % fwhy)
                    state["breaker"] = {"kind": "timeout", "detail": fwhy,
                                        "stage": num + "-fix"}
                    fire(state, "BREAK", {}, events)
                    save_state(statef, state)
                    return 4
                commit_stage(project, wd, num + "-fix", issue_id, attempt, fa,
                             models.get(target[:2]) or models.get(fa), tp, used=LAST_RESULT.get("model"))
                oscillated = record_round(state, num, attempt, keys, tp)
                # 참고용 합계 — 실행 전체에서 계속 결정을 받은 새 지적 반려의 수.
                # 판단에는 더 쓰지 않는다(회차 상한은 round_decision 이 rounds 에서
                # 스테이지별로 다시 센다).
                state["counters"]["reviewRounds"] = len(state["rounds"])
                if oscillated:
                    print("       ! 진동 — 같은 산출물로 되돌아왔다")
                    state["stages"][num] = {"status": "failed", "reason": "진동"}
                    state["breaker"] = {"kind": "rounds-exhausted",
                                        "stage": num, "detail": "진동"}
                    fire(state, "BREAK", {}, events)
                    save_state(statef, state)
                    return 2
                if merged and num in MERGED:
                    # 묶인 실행의 02 반려 — 01 을 고쳤으니 그 위에 선 03a·05 부터 다시 돈다.
                    # 같은 세션이 쓴 06 은 낡은 설계 위의 것이라 읽지 않고 치운다(회차도 안 센다).
                    drop_follower(wd, MERGED[num], attempt)
                    prebuilt.pop(MERGED[num], None)
                    redo.update(("03a", "04", "05", "05u", "02"))
                    idx = [x[0] for x in order].index("03a")
                    break
        else:
            # 시도 6회를 다 썼는데 통과도 멈춤도 아니다 — 정상 경로에서는
            # 회차 상한(3)·게이트 실패 상한(2) 안에서 먼저 멈추므로 닿지 않아야
            # 하지만(05 설계 F4), 닿았다면 조용히 다음 스테이지로 넘어가면 안
            # 된다. 이 안전망이 없으면 스테이지가 기록에서 빠진 채 완주로
            # 끝난다(ISSUE-stop-by-count 재현).
            state["stages"][num] = {"status": "failed", "reason": "시도 안전망 소진"}
            state["breaker"] = {"kind": "rounds-exhausted", "stage": num,
                                "detail": "시도 안전망 소진"}
            fire(state, "BREAK", {}, events)
            save_state(statef, state)
            print("\n중단 — %s 가 시도 안전망(6회)을 소진했다. 사람이 볼 차례다." % num)
            return 2

        save_state(statef, state)

    save_state(statef, state)
    if a.dry_run:
        print("\n완료. 산출물: %s" % wd)
        return 0

    # 신호는 소각 **앞에서** 옮긴다 — 뒤에는 실행 디렉터리가 없어 꺼낼 것이 없다.
    write_signals(wd, models["_repo"], issue_id, state)

    # 이 실행의 PR 이 초안으로 열렸는지는 소각 앞에서 읽는다 — 뒤에는 파일이 없다.
    pr_opened = False
    pr_body = ""
    try:
        with open(os.path.join(wd, "10-handoff.md"), encoding="utf-8") as f:
            pr_body = f.read()
        pr_opened = ("- %s" % PR_DRAFT_PREFIX) in pr_body
    except OSError:
        pass
    # 회고의 워크플로우 몫도 소각 앞에서 읽는다 — 뒤에는 회고문이 없다.
    share = workflow_share(os.path.join(wd, "11-retro.md"))
    # 시나리오 카드면 그 몫을 시나리오 개선 모음에 모은다(카드 저장소의 plan/ — 소각 커밋에 안 담긴다).
    if scenario_of()[0]:
        try:
            plan = os.path.join(os.path.dirname(os.path.dirname(_backlog_path)), "plan")
            pool_no = backlog_get("시나리오")
            rel = os.path.relpath(plan, root)
            if not rel.startswith(".."):
                # 카드가 본체 저장소 안이면 작업 폴더의 같은 자리에 카드별 파일로 쓴다 — 소각 커밋·PR 에
                # 실려 사람 커밋 없이 기준 가지에 들어가고, 함께 돈 두 카드의 PR 이 같은 파일을 고치지 않는다.
                plan = os.path.join(project, rel)
                pool_no = "%s-%s" % (pool_no, issue_id)
            collect_improvements(
                plan, pool_no, "%s/%s" % (models["_repo"], issue_id), share,
                os.path.exists(os.path.join(wd, "11-retro.md")), bool(state.get("resumed")))
        except OSError as ex:
            print("       개선 모음을 쓰지 못했다 — %s" % ex)

    # 소각 — 작업 트리에서만 치운다. 이력에는 남는다. 회고까지 끝난 뒤여야
    # 한다: 회고의 근거가 바로 이 디렉터리의 판정 파일들이다.
    shutil.rmtree(wd, ignore_errors=True)
    try:
        os.rmdir(os.path.dirname(wd))     # 빈 `.workflow/` 껍데기를 남기지 않는다
    except OSError:
        pass                              # 다른 실행이 들어 있으면 그대로 둔다
    subprocess.run(["git", "add", "-A"], cwd=project, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-q", "-F", "-"],
                   cwd=project, text=True,
                   input="소각: 실행 디렉터리를 작업 트리에서 치운다\n\n"
                         "다음 작업이 낡은 산출물을 확정 전제로 읽지 않게 한다.\n"
                         "내용은 이력에 남는다 — git log --grep='Loop-Issue: %s'\n\n"
                         "Loop-Issue: %s\nLoop-Stage: burn\n" % (issue_id, issue_id),
                   capture_output=True)
    print("       소각 — 이력에는 남는다 (git log --grep='Loop-Issue: %s')" % issue_id)
    # 소각 커밋 뒤 가지를 한 번 더 올린다 — 인계(10)의 push 는 회고·소각보다
    # 앞서 끝나 있어 그 뒤 커밋이 원격에 못 올라간다(이슈 「배경」 3).
    # close_worktree 앞에서 한다 — 정리 뒤에는 이 작업 폴더가 없어진다.
    pushed = repush(project, issue_id, models.get("_handoff", "pr"))
    for line in pushed:
        print("       %s" % line)
    # 다시 올린 뒤에만 준비됨으로 바꾸고, 켜져 있으면 병합한다 (기록 41).
    # close_worktree 앞이다 — 정리 뒤에는 작업 폴더가 없다.
    for line in share_to_pr(project, issue_id, pr_body, share, pr_opened):
        print("       %s" % line)
    lines, stop = ready_and_merge(project, issue_id, pushed, pr_opened, state, models)
    for line in lines:
        print("       %s" % line)
    if not any("자동 병합했다" in l for l in lines) and backlog_get("차례"):
        backlog_set(차례="")      # 자동 병합이 안 됐다 — 이제 사람이 움직일 차례다
    close_worktree(root, project)
    if stop:
        print("\n중단 — %s. PR 은 그대로 두었다. 사람이 볼 차례다." % stop)
        backlog_set(멈춘이유=stop)
        return 7
    # 11 회고가 인계 뒤에 돌아 `단계` 를 다시 채운다 — 끝났으면 비운다.
    backlog_set(단계="")
    print("\n완주 — $%.2f. 이력: git log --grep='Loop-Issue: %s'"
          % (state.get("cost", 0.0), issue_id))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--guard"]:      # 세션 훅이 부르는 검사 모드(FN-07~FN-09)
        sys.exit(guard_main())
    STATUS_DIR = os.path.expanduser("~/.stop-loop/status")   # 명령으로 띄울 때만 켠다
    SIGNALS_DIR = os.path.expanduser("~/.stop-loop/signals")
    LIVE_DIR = os.path.expanduser("~/.stop-loop/live")
    sys.exit(main(sys.argv[1:]))
