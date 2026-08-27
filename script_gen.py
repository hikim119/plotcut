"""
script_gen.py — 자막 srt → 「타임라인 대본」 txt

PlotCut은 LLM API를 직접 부르지 않는다. 대신 **이미 로그인된 코딩 에이전트 CLI**
(Codex / Claude Code)를 하위 프로세스로 돌린다.

  · API 키가 필요 없다 — 에이전트의 구독 로그인을 그대로 쓴다
  · 추가 요금이 없다 — ChatGPT Plus / Claude 구독에 포함된다
  · 대본 규칙이 한 군데(AGENTS.md)에 있어 도구와 에이전트가 안 갈라진다

에이전트 CLI의 정확한 인자는 버전마다 바뀔 수 있어 `runner.json` 으로 덮어쓸 수 있고,
알 수 없는 옵션이라고 거절당하면 더 단순한 형태로 한 단계씩 물러나며 재시도한다.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RULES = ROOT / "AGENTS.md"
STYLE = ROOT / "template" / "style_example.txt"
CONFIG = ROOT / "runner.json"

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _p(s):
    """경로 리터럴 — | 를 경로 구분자로 쓴다(이스케이프 사고 방지)."""
    return s.replace("|", os.sep)

# 인자 사다리 — 앞에서부터 시도하고 "알 수 없는 옵션"이면 다음으로 물러난다.
RUNNERS = {
    "codex": {
        "label": "Codex",
        # 윈도우에서는 확장자 있는 셔임을 먼저 찾는다. 확장자 없는 `codex` 는
        # sh 스크립트라 CreateProcess 가 실행하지 못한다.
        "names": ["codex.cmd", "codex.exe", "codex"],
        "extra_paths": [r"%APPDATA%\npm", r"%LOCALAPPDATA%\npm"],
        # `--skip-git-repo-check` 없으면 깃 저장소 밖에서 codex exec 이 거부한다:
        #   "Not inside a trusted directory and --skip-git-repo-check was not specified."
        #
        # `--full-auto` 는 **최신 CLI 에서 삭제됐다.** codex-cli 0.147.0 실측:
        #   error: unexpected argument '--full-auto' found
        # 후임은 `--approve-for-me` — "승인 요청을 workspace-write 샌드박스로
        # 자동 처리". 이게 없으면 세션이 읽기 전용이라 대본 파일을 못 쓴다:
        #   "현재 세션이 읽기 전용이라 생성중_초안.txt 파일 생성이 차단됐습니다"
        # `--approve-for-me` 와 `--sandbox` 는 **같이 못 쓴다**(상호 배타).
        "argv": [["exec", "--skip-git-repo-check", "--approve-for-me", "{prompt}"],
                 ["exec", "--skip-git-repo-check",
                  "--sandbox", "workspace-write", "{prompt}"],
                 ["exec", "--skip-git-repo-check", "{prompt}"],
                 ["exec", "{prompt}"]],
        "auth": [_p("%USERPROFILE%|.codex|auth.json")],
        "login": "Codex설치.bat 을 더블클릭하면 로그인 화면이 뜹니다.",
        "install": "프로젝트 폴더의 Codex설치.bat 을 더블클릭해 설치·로그인하세요.",
    },
    "claude": {
        "label": "Claude Code",
        "names": ["claude", "claude.cmd", "claude.exe"],
        "extra_paths": [
            r"%USERPROFILE%\.vscode\extensions\*\resources\native-binary",
            r"%APPDATA%\npm",
        ],
        # acceptEdits 는 파일 수정만 허용한다 — 그대로 두면 프롬프트의 검증 단계
        # (`python cli.py check`)가 거부당하고, 에이전트가 대신 cli.py·script_io.py·
        # layout.py 를 통째로 읽어 손으로 검산하느라 몇 배 느려진다. python 만 연다.
        #
        # **effort 를 낮추지 않는다.** 한때 `--effort low` 를 넣어 3.4배 빨랐지만
        # 대본이 눈에 띄게 나빠졌다. 같은 자막으로 나란히 뽑아 본 실측:
        #   low  131초 · 블록 16 · 대사 63 · 나레이션 9 (7.0줄당 1개) · 184.2초
        #   기본 447초 · 블록 17 · 대사 67 · 나레이션 9 (7.4줄당 1개) · 188.7초
        # 숫자는 거의 같은데 문장이 다르다. low 는 완결형 명사로 끊고
        # (`…딱 걸려버린 여자`) 방금 나온 대사를 다시 설명하는 줄이 섞인다.
        # 기본은 연결어미로 다음을 열고(`…딱 걸려버린 여자인데`) 대사가 말하지
        # 않는 것을 짚는다(`집이 바뀌고 있는 줄은 꿈에도 모른 채`).
        # AGENTS.md 규칙 3·4 가 요구하는 게 후자다 — `check` 로는 안 잡힌다.
        "argv": [["-p", "{prompt}", "--permission-mode", "acceptEdits",
                  "--allowedTools", "Bash(python:*)"],
                 ["-p", "{prompt}", "--permission-mode", "acceptEdits"],
                 ["-p", "{prompt}"]],
        "auth": [_p("%USERPROFILE%|.claude|.credentials.json")],
        "login": "터미널에서 claude 를 한 번 실행해 로그인하세요.",
        "install": "Claude Code CLI를 설치하세요.",
    },
}
ORDER = ["codex", "claude"]


class GenError(RuntimeError):
    pass


class GenStopped(GenError):
    """사용자가 중단을 눌렀다. 실패가 아니므로 pipeline 이 Stopped 로 바꾼다 —
    안 그러면 화면에 '✘ GenError: 중단되었습니다.' 로 오류처럼 뜬다."""


class QuotaError(GenError):
    """에이전트 CLI 가 자기 사용량 한도로 거절했다. 도구·설정 문제가 아니라서
    진단할 게 없다 — pipeline 이 남은 폴더를 치운다(안 그러면 다시 시도할 때마다
    로그 한 장짜리 폴더가 쌓인다. 실측 8/26: 한 배치에 6개)."""


# ── 러너 찾기 ───────────────────────────────────────────────────────────────

def _load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _which(spec):
    names = spec["names"]
    for n in names:
        p = shutil.which(n)
        if p and Path(p).suffix.lower() in (".cmd", ".exe", ".bat", ""):
            if Path(p).suffix or sys.platform != "win32":
                return p
    for pat in spec.get("extra_paths", []):
        base = os.path.expandvars(pat)
        if "*" in base:
            # * 가 경로 중간에 있을 수 있다 (VS Code 확장 버전 폴더 등)
            head, _, tail = base.partition("*")
            root = Path(head)
            if not root.exists():
                continue
            dirs = [d / tail.lstrip(os.sep) for d in root.iterdir() if d.is_dir()]
        else:
            dirs = [Path(base)]
        for d in sorted(dirs, reverse=True):
            for n in names:
                c = d / n
                if c.exists():
                    return str(c)
    # 확장자 없는 셔임이라도 없는 것보다는 낫다
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def find_runner(prefer=None):
    """쓸 수 있는 에이전트 CLI를 찾는다. 반환: (key, exe, spec) 또는 None."""
    cfg = _load_config()
    if cfg.get("exe"):
        spec = dict(RUNNERS.get(cfg.get("runner", "codex"), RUNNERS["codex"]))
        if cfg.get("argv"):
            spec["argv"] = [cfg["argv"]]
        return cfg.get("runner", "codex"), cfg["exe"], spec

    order = ([prefer] if prefer else []) + [k for k in ORDER if k != prefer]
    for key in order:
        spec = RUNNERS.get(key)
        if not spec:
            continue
        exe = _which(spec)
        if exe:
            return key, exe, spec
    return None


def logged_in(spec):
    """인증 파일이 있는가. 판단 불가면 None (막지 않는다)."""
    files = spec.get("auth")
    if not files:
        return None
    for f in files:
        if Path(os.path.expandvars(f)).exists():
            return True
    return False


def status():
    """(level, 메시지). level: "ok" | "warn" | "none"."""
    found = find_runner()
    if not found:
        return "none", ("대본을 만들 에이전트가 없습니다 — "
                        + RUNNERS["codex"]["install"])
    key, exe, spec = found
    auth = logged_in(spec)
    if auth is False:
        return "warn", ("%s 는 깔려 있지만 로그인 기록이 없습니다 — %s"
                        % (spec["label"], spec.get("login", "")))
    return "ok", "%s 로 대본을 만듭니다 (%s)" % (spec["label"], Path(exe).name)


# ── 프롬프트 ────────────────────────────────────────────────────────────────

# 에이전트가 "읽기 전용이라 못 쓴다"고 끝낸 흔적. 인자를 못 알아들은 것과는
# 원인이 달라서(권한 문제) 사다리를 내려가도 나아지지 않는다 — 따로 안내한다.
_NO_WRITE = (
    "읽기 전용", "쓰기 권한", "쓰기가 차단", "생성이 차단", "권한이 없",
    "read-only", "readonly", "permission denied", "not permitted",
    "operation not permitted", "cannot write", "unable to write",
    "failed to write", "write access",
)


# 에이전트 CLI 가 **자기 사용량 한도**에 걸려 즉시 끝난 흔적. 실측(8/26): Codex 가
# 「You've hit your usage limit ... try again at 8:40 PM」만 찍고 4초 만에 죽었는데,
# 배너에 `sandbox: read-only` 가 있어서 아래 `_NO_WRITE` 에 걸렸다 — 도구가 권한
# 문제라고 오진하고 runner.json 을 고치라고 안내했다. 한도가 먼저다.
_QUOTA = (
    "usage limit", "rate limit", "rate_limit", "quota", "too many requests",
    "insufficient_credit", "insufficient credits", "out of credits",
    "사용량 한도", "요청 한도", "한도를 초과", "크레딧",
)
# 한도 안내에 흔히 붙는 재시도 시각 — 그대로 사용자에게 보여 준다.
# Codex 배너(`sandbox: read-only`, `approval: never` …)는 **모든 실행에 찍힌다.**
# 실패 원인의 증거가 아니므로 권한 판정에서 뺀다. (이 모듈은 `re` 를 안 쓴다 —
# 두 가지 다 앞부분 비교로 충분하다.)
_BANNER_KEYS = ("workdir", "model", "provider", "approval", "sandbox",
                "reasoning", "session id", "--------")


def _is_banner(line):
    head = line.strip().lower()
    return any(head.startswith(k) for k in _BANNER_KEYS)


def _looks_quota(out):
    low = (out or "").lower()
    return any(k in low for k in _QUOTA)


def _quota_hint(out):
    """「try again at 8:40 PM」 → 「 — 8:40 PM 이후에 다시 됩니다」"""
    for ln in (out or "").splitlines():
        low = ln.lower()
        i = low.find("try again at")
        if i < 0:
            i = low.find("try again in")
        if i >= 0:
            when = ln[i + len("try again at"):].split(".")[0].strip()
            if when:
                return " — %s 이후에 다시 됩니다" % when[:40]
    return ""


def _looks_read_only(out):
    body = [l for l in (out or "").splitlines() if not _is_banner(l)]
    low = chr(10).join(body).lower()
    return any(k in low for k in _NO_WRITE)


# ── 프롬프트 변형 (A/B 실험용) ──────────────────────────────────────────────
# `bench.py` 가 같은 영화로 기준선과 변형을 뽑아 **블라인드 쌍대 비교**로
# 승률을 잰다. 자동 지표는 목표가 아니라 관문이다 — 기준선 두 판의 편차가
# 블록 ±1 · 대사 ±4 · 명사끝 **±15%p** 라 지표만으로는 못 가른다.
#
# `variant=None` 이면 아래 코드가 한 줄도 안 돈다. 기본 프롬프트는 **바이트
# 단위로 안 변한다** — selftest 가 해시로 못 박는다.

def _insert_after(lines, needle, add):
    for i, ln in enumerate(lines):
        if needle in ln:
            return lines[:i + 1] + list(add) + lines[i + 1:]
    # `GenError` 로 던진다 — `ValueError` 면 `cli.main` 이 분류를 못 해
    # 파이썬 트레이스백이 그대로 나간다. 프롬프트 문구를 고치면 기준점이
    # 사라지므로 **실험 도중에 실제로 난다.**
    raise GenError("변형 기준점을 못 찾았다: %r — 프롬프트 문구가 바뀌었다면 "
                   "`script_gen` 의 변형 함수도 같이 고쳐라." % needle)


def _drop_containing(lines, needle):
    out = [ln for ln in lines if needle not in ln]
    if len(out) == len(lines):
        raise GenError("지울 줄을 못 찾았다: %r" % needle)
    return out


def _v_reaction(lines):
    """정보를 날라야 하는 대목이 설명조가 된다.

    근거: 판별 시험에서 심사자 4명 이상이 잡아낸 도구 문장 6개가 **전부
    편지·티켓 줄거리 설명**이었다. 유일하게 재현성 있는 약점이다.
    """
    return _insert_after(lines, "· **설명하지 말고 반응을 적어라.**", [
        "     **정보를 날라야 하는 대목도 반응으로 쓴다.** 물건·장치·설정을",
        "     소개할 때가 제일 무너진다. `편지 안에는 열쇠가 있었고` 는 설명이고,",
        "     `열쇠를 손에 넣자 눈이 돌아갔고` 는 대본이다. 무엇이 있었는지가",
        "     아니라 **그걸 본 인물이 어떻게 됐는지**로 적어라.",
    ])


def _v_dense(lines):
    """대사를 잘게 끊는다.

    근거: 같은 목표 길이인데 잘 나간 3편은 대사 문단을 **두 배로** 썼다.
    실측 유저분 111/58/77 vs 도구 61/34/37. 기준선 편차(±4)의 12배가 넘는다.
    """
    return _insert_after(lines, "· **나레이션을 아껴라.", [
        "   · **대사를 잘게 끊어라.** 실측: 잘 나간 3편은 같은 길이에 대사 문단을",
        "     **두 배로** 썼다(111·58·77개). 한 문단에 여러 문장을 몰아넣지 말고",
        "     자막 큐 하나에 한 문단으로 나눠라 — 컷이 빨라야 안 지루하다.",
        "     장면 수를 늘리라는 게 아니다. **고른 장면 안에서 더 많이 인용해라.**",
    ])


def _v_nostyle(lines):
    """`style_example.txt` 를 가리키는 줄을 뺀다 (ablation).

    프롬프트의 61%가 이 파일을 가리킨다. **얼마나 지고 있는지** 모르면
    나머지 튜닝이 헛돈다. `--extra` 로는 못 만드는 변형이다(추가만 된다).
    """
    return _drop_containing(lines, "이게 문체의 정답이다")


def _v_context(lines):
    """따라가지는 대본으로 만든다.

    근거: 눈금 대결(도구 vs 유저분 완성본 3편, 30표)에서 **flow 만 무너졌다.**
      ending 6/6 · gut 4/6 · hook 3/6 · fun 3/6 · **flow 1/6**
    심사평이 한 방향을 가리켰고, 아래 세 줄은 그들이 쓴 말을 그대로 옮긴 것이다:
      「A는 앵커 없이 대사가 빠르게 넘어가 화자 추론에 의존한다」
      「"나도 동의해"가 무엇에 대한 동의인지 앞 대사 없이 붕 뜬다」
      「서로 다른 스레드를 43줄에 욱여넣어서 누가 누구에게 말하는지 계속 놓친다」
    """
    return _insert_after(lines, "· **인물을 `그`·`그녀`로 부르지 마라.**", [
        "   · **누가 말하는지 알게 해라.** 자막만 보는 사람은 화면을 안 본다.",
        "     이름이 불리는 줄(`리스 정신차려!` · `네 말 들린다 말콤`)은 **버리지",
        "     마라** — 그 한 줄이 다음 열 줄의 화자를 잡아 준다.",
        "   · **셋업을 버리고 펀치라인만 가져오지 마라.** `나도 동의해` 는 앞줄이",
        "     없으면 무엇에 대한 동의인지 모른다. 웃긴 줄·중요한 줄을 인용할 거면",
        "     **그 앞의 깔아 주는 줄을 같이** 인용해라. 둘이 한 묶음이다.",
        "   · **줄기를 줄여라.** 여러 갈래를 한 대본에 욱여넣으면 인물 관계가",
        "     끊긴다. **한 줄기를 끝까지** 따라가고 나머지는 통째로 버려라 —",
        "     버린 줄기는 줄거리.txt 에 적으면 된다.",
    ])


# ── 대결 기록: **아무것도 기본에 안 넣었다** ────────────────────────────────
# 같은 영화로 블라인드·순서반전 쌍대 비교를 82표 돌린 결과다.
#
#   v2_dense    75% (6/8)    대사를 잘게
#   v4_context  67% (8/12)   화자 앵커 · 셋업 유지 · 줄기 줄이기
#   v3_nostyle  50% (4/8)    `style_example.txt` 지시 제거 — 차이 없음
#   v1_reaction 38% (3/8)    더 반응체로 — 졌다
#
# 이긴 둘(v2+v4)을 넣고 **검증 세트로 다시 붙였더니 44%(7/16)로 졌다.**
#   father 3/4 · robber 2/4 · **gentleman 0/4** · 중경삼림 2/4
# gentleman 패배 사유는 프롬프트가 시킨 것과 정반대였다 — 「인물 이름 소개가
# 없고 초반 8줄이 설명 없이 터지고 같은 대사가 반복된다」. 지시가 그 판에서
# **안 먹은 것**이지 지시가 나쁜 게 아니다.
#
# 그래서 결론은 **「아직 모른다」**다. 개별 승(6/8·8/12)도 합친 패(7/16)도
# 동전 던지기로 흔히 나오는 크기다. 생성 편차가 워낙 커서(같은 프롬프트로
# 쓴 구간이 89.9분 vs 23.7분) **조건당 한 판으로는 못 가른다.**
# 다시 하려면 조건당 3판 이상 뽑아 놓고 대결해야 한다.
#
# 기본 프롬프트는 **건드리지 않았다**(c0274042ddce636c). 변형 넷은 실험용으로
# 남겨 둔다 — `bench.py gen --variant v2_dense` 로 언제든 다시 잰다.
_SHIPPED = ()


VARIANTS = {
    "v1_reaction": _v_reaction,
    "v2_dense": _v_dense,
    "v3_nostyle": _v_nostyle,
    "v4_context": _v_context,
}


def build_prompt(srt_path, out_path, target_s=180, extra="", movie_title="",
                 plot_path=None, variant=None):
    srt_path = Path(srt_path).resolve()
    out_path = Path(out_path).resolve()
    movie_title = (movie_title or "").strip()
    lines = [
        "PlotCut 영화 리캡 대본을 만들어라. 아래를 순서대로 반드시 수행한다.",
        "",
        # 문체 예시를 **맨 앞**에 둔다. 에이전트는 표의 숫자가 아니라 눈앞의 예시를
        # 베낀다 — 실측: 예시가 중경삼림(중앙 30자)이던 시절 출력이 25자로 따라갔다.
        '· **먼저 "%s" 를 전문 읽어라. 이게 문체의 정답이다.**' % STYLE,
        "   실제로 잘 나간 숏츠 3편의 나레이션 33문장 전문이다.",
        "   **숫자를 맞추려 하지 마라. 저 33문장처럼 들리게 써라.** 읽어 보면 안다.",
        "",
        "   그 33문장이 줄거리 요약과 갈리는 지점은 다섯이다:",
        "",
        "   · **인물을 `그`·`그녀`로 부르지 마라.** 33문장에 **0번** 나온다.",
        "     이름이나 역할로 부른다 — `할` `말콤` `미키` `일진` `강도` `둘`.",
        "     `그는 텅 빈 집에서` 는 줄거리 요약이고,",
        "     `말콤은 눈이 돌아갔고` 는 대본이다.",
        "     **이름은 자막에 나온 것을 우선한다.** 자막에 없어도 널리 통용되는",
        "     이름(규칙 8)은 써도 된다 — 시청자가 아는 이름이 역할 호칭보다 낫다.",
        "     그것도 없으면 `그 남자` 말고 **역할**로: `경찰` `일진` `강도`.",
        "     **인물이 처음 말하기 전에** 그 앞 나레이션이 이름·역할로 소개한다 —",
        "     결말에서 처음 나오는 이름은 시청자에게 아무도 아니다.",
        "   · **설명하지 말고 반응을 적어라.** 무슨 일이 있었나가 아니라 **그래서",
        "     어떻게 됐나**. `맛탱이가 완전히 가버렸고` · `눈이 돌아갔고` ·",
        "     `질려버렸죠` · `쫄았고` · `멘탈이 나가버렸고`.",
        "   · **은어와 과장을 쓴다. 열에 셋이 그렇다** — 나레이션이 12줄이면",
        "     **서너 줄은** 이래야 한다. 담백하게 쓰면 뉴스 자막이 된다.",
        "     같은 뜻인데 왼쪽은 요약이고 오른쪽이 대본이다:",
        "",
        "         크게 상심했고        →  **맛탱이가 완전히 가버렸고**",
        "         마음이 바뀌었고      →  **눈이 돌아갔고**",
        "         마지막 수를 썼고     →  **궁극기를 시전하기로 했고**",
        "         반격이 시작됐죠      →  **참교육이 시작됐죠**",
        "         도망치기 시작했는데  →  **냅다 도망치기 시작했는데**",
        "         충격을 받았고        →  **그대로 멘탈이 나가버렸고**",
        "",
        "     욕은 초성 마스킹(`ㅅ끼` `ㅆ발`). 영화 분위기에 맞게 골라라 —",
        "     문예물에 `궁극기` 를 넣으면 그것도 어긋난다.",
        "   · **문장을 닫지 마라.** `~는데`·`~고` 로 끝내 다음으로 넘긴다.",
        "     `~다.` 는 33문장에 **0개**다. 이야기가 뒤집히는 **딱 한 자리**에",
        "     `~죠` 를 하나 — 세 편 모두 그 줄이 제일 기억에 남는다.",
        "     다만 **한 어미로 몰지 마라.** `~고` 만 여덟 번 이으면 낭독이 죽는다.",
        "   · **나레이션을 아껴라. 이게 위 넷만큼 중요하다.** 대사가 스스로 말하는",
        "     장면에는 **달지 마라** — 방금 들은 말을 요약하면 같은 말을 두 번 듣는다.",
        "     실측: **대사 5~8줄에 하나** · 블록 **열에 셋은 나레이션이 아예 없다**.",
        "     반응을 잘 쓰라고 했지 **많이 쓰라고 한 게 아니다** — 늘리면 대사가",
        "     밀려나고 다시 요약이 된다.",
        "     **아끼는 건 개수지 표현이 아니다.** 줄을 줄이되 남긴 줄은 더 세게 써라 —",
        "     열두 줄만 쓸 거면 그 열둘이 전부 살아 있어야 한다.",
        "",
        "   숫자로 재면 이렇게 나오는데 **이건 결과지 목표가 아니다.** 위처럼 쓰면",
        "   저절로 맞는다: 한 덩어리 중앙 16자 · 8~15초에 하나 · 연속은 2문장까지 ·",
        "   한 종결어미가 절반을 안 넘는다(`~는데`:`~고`:명사 ≒ 4:3:2).",
        "   **영화의 말투를 따라간다** — 액션·코미디면 구어체, 문예물이면 문어체.",
    ]
    lines += [
        '· "%s" 의 ① 절들을 전문 읽어라. 포맷·문체 규칙·분량 공식이 거기 있다.' % RULES,
        "   · **첫 블록은 훅이다** (규칙 2). 중반 이후에서 가장 궁금한 대사로 열고,",
        "     두 번째 블록의 첫 나레이션 한 줄로 되감는다. **훅에 쓴 대사는 뒤에서",
        "     다시 인용하지 않는다** — 같은 큐가 두 번 나오면 check 가 ✘ 를 낸다.",
        "   · **마지막 문단은 14자 이하 짧은 대사**로 끝낸다. 나레이션으로 정리하지 마라",
        "   · 한 문단 = **자막 큐 하나**가 기본 (실측 94%). 큐가 2줄이면 **2줄 그대로**",
        '· 자막 파일: "%s"' % srt_path,
        "   **SRT 파일에 적힌 번호를 기준으로 읽어라.**",
    ]
    # 내 완성본 원본이 있으면(로컬) 대사까지 통째로 읽힌다. gitignore 대상이라
    # 받아 쓴 사람에겐 이 줄이 안 붙고 위의 나레이션 예시로만 간다.
    #
    # **하위 폴더를 명시적으로 막는다.** 게이트는 `glob("*.srt")` 라 비재귀지만
    # 프롬프트 문장이 "안의 srt 전부"였다. 폴더를 `ls` 하면 `원본자막/` 아래
    # 271KB짜리 영어 원본 .vtt 3개가 보이는데, 그건 완성본이 아니라 **번역 전
    # 원문**이다. 읽으면 문체가 영어 대사에 오염되고 읽는 양이 5배가 된다.
    _samples = ROOT / "대본예시"
    if _samples.is_dir() and any(_samples.glob("*.srt")):
        lines += [
            '· "%s" 의 **최상위 srt 3개**는 실제로 잘 나간 완성본이다. 전부 읽어라.'
            % _samples,
            "   대사와 나레이션이 어떤 리듬으로 섞이는지 여기서 배운다. 이게 최종 기준이다.",
            "   **하위 폴더(`원본자막/`)는 읽지 마라** — 번역 전 영어 원문이라",
            "   완성본이 아니다. 읽으면 문체가 오염된다.",
        ]
    if movie_title:
        # 파일 이름이 영화 제목인 경우가 오히려 드물다(`English.srt`,
        # `The.Movie.2019.1080p.BluRay-GROUP.srt`). 제목을 알아야 자막에 안 나오는
        # 상황을 짚을 수 있다 — 액션·코미디는 이야기가 대부분 화면에 있다.
        lines += [
            "   영화 제목: **%s** — 이 영화를 안다면 아는 것도 쓴다." % movie_title,
            "   단 **대사는 언제나 자막 원문 그대로**고, 나레이션도 **그 컷에 실제로",
            "   보이는 것**만 적는다. 영화 지식은 무엇을 고를지 정하는 데만 쓴다.",
        ]
    if plot_path:
        # 줄거리를 머릿속으로만 정리하고 버리면 사람이 편집할 때 다시 영화를
        # 봐야 한다. 파일로 남겨 [줄거리] 탭에서 먼저 읽고 고칠 수 있게 한다.
        lines += [
            '· **대본을 쓰기 전에** 이 경로에 줄거리를 정리해 써라 (UTF-8):',
            '   "%s"' % Path(plot_path).resolve(),
            "   이 형식 그대로, 각 항목은 3~5줄:",
            "",
            "     [줄거리]",
            "     (누가 무엇을 하다가 어떻게 되는지. 시간순으로.)",
            "",
            "     [결말]",
            "     (마지막에 어떻게 끝나는지. 숨기지 말고 그대로 적는다.)",
            "",
            "     [이 대본에서 고른 것]",
            "     (여러 줄기 중 무엇을 골랐고 무엇을 버렸는지, 왜 그랬는지.)",
            "",
            "     [자막에 안 나오는 것]",
            "     (화면에는 보이는데 대사로는 안 나오는 것. 없으면 '없음'.)",
            "",
            "   **자막에서 읽어낸 것만 쓴다. 모르는 건 '자막만으로는 알 수 없음'이라고",
            "   적고 지어내지 마라.** 이 파일은 사람이 편집 전에 읽는 용도다.",
        ]
    lines += [
        "· 목표 길이: %d초" % int(target_s),
        '· 대본을 정확히 이 경로에 UTF-8(BOM 없음)로 써라:',
        '   "%s"' % out_path,
        # `cd "…" && python …` 로 쓰면 명령이 `cd` 로 시작해 allowlist
        # `Bash(python:*)` 에 안 걸린다 → 승인 대기 → -p 모드에서 거부 →
        # 아래 "권한 문제면 건너뛰어라" 가 발동해 검증이 통째로 생략된다.
        # 그래서 **python 으로 시작하는 한 줄**로 준다.
        "· 그 다음 아래를 실행해 ✘ 를 고쳐라. **고쳐 쓰기는 최대 5번까지만** 하고,",
        "   그래도 남으면 남은 채로 끝내라 — 검산에 시간을 더 쓰지 마라.",
        '   python "%s" check "%s" --srt "%s" --seconds %d'
        % (ROOT / "cli.py", out_path, srt_path, int(target_s)),
        "   ✘ 가 안 줄어들면 그 자리에서 멈춰라 — 지시가 서로 상충한다는 뜻이다.",
        "   이 명령이 권한 문제로 실행되지 않으면 **그냥 건너뛰고 끝내라.**",
        "   소스 코드를 읽어 손으로 검산하지 마라.",
        "· 끝나면 총 길이·블록 수·고른 줄거리를 3줄 이내로만 출력해라.",
        "",
        "절대 자막에 없는 대사를 지어내지 마라. 대사는 자막 원문 그대로 옮긴다.",
        "다른 파일은 건드리지 마라.",
    ]
    # 자막 언어를 여기서 판별해 프롬프트에 박는다. 사용자가 켜고 끄는 것이 아니라
    # **한국어가 아니면 무조건** 번역을 달아야 한다 — 안 그러면 외국어가 화면에 나간다.
    if not _srt_is_korean(srt_path):
        lines += [
            "",
            "**이 자막은 한국어가 아니다. 대본은 전부 한국어로 써라.**",
            "대사는 원문을 옮기지 말고, **자막 번호 + 한국어** 로 써라:",
            "",
            # 예시에 **문체를 넣지 않는다.** 예전엔 `#176 대박, 저 귀걸이 좀 봐` 같은
            # 지어낸 문장 3줄을 줬는데, 셋 다 쉼표를 물고 평균 13.7자였다 — 실측
            # (248줄에 쉼표 1개 · 중앙 10자)의 정반대다. 위 :213 주석과 같은 사고다.
            "    #176 여기에 한국어 한 줄",
            "    #181 큐가 바뀌면 새 문단",
            "    #182-183 연속 두 큐만 합친다",
            "",
            "`#176` 은 SRT 파일에 적힌 번호다. 그 번호로 컷 위치를 찾는다 —",
            "번호를 빼면 한국어 문장과 자막을 대조할 수 없어 전부 실패한다.",
            "`#182-183` 은 연속 두 큐를 한 문단으로 합친 것(상한 2개).",
            "빠짐없이 **모든 대사 문단**에 번호를 달아라.",
            "완성된 대본에 영어가 한 글자도 남으면 안 된다.",
            "",
            "**번역은 자막이 아니라 숏츠 대사다.** 완성본 3편의 대사 248줄 전수 실측:",
            "   · 한 줄 **중앙 10자**(평균 10.3). **20자 넘는 줄이 0줄**이다",
            "   · 8~14자가 셋에 둘. 1~6어절, **3어절이 제일 흔하다**",
            "   · **한 문단 한 줄.** 247큐 중 246이 1줄이다",
            "   · 쉼표는 248줄에 **1개**, 따옴표는 **0개**. 쉼표 자리면 줄을 끊어라",
            "   · **셋에 하나가 `?` 나 `!` 로 끝난다**(물음표 38 · 느낌표 40).",
            "     나레이션과 정반대다 — 나레이션 33문장엔 `?`·`!` 가 0개다",
            "   · 입말·은어를 그대로 쓴다. 욕은 초성 마스킹(`ㅅ끼` `ㅆ발` `ㅈ밥` `ㅌ막`)",
            "   · 한글이 영어 원문 글자 수의 **0.24배**다. 절반 넘게 버린다는 뜻이다",
            "",
            '**"%s" 의 14자 상한은 나레이션 규칙이다.** 대사 줄은 19자까지 간다 —'
            % STYLE,
            "대사에 14자를 적용하지 마라. 두 규범이 다르다(같은 파일 §10).",
            "",
            "**옮길 수 없는 것은 통째로 버려라.** 말장난 · 그 나라 사람만 아는 밈 ·",
            "고유명사 나열 · 운율로 웃기는 말은 옮기면 길어지기만 하고 안 웃긴다.",
            "그 큐는 번호째로 빼고 다음 큐로 간다. 실측 — 원본 5큐 이하 장면은",
            "**거의 다** 옮겼고(한국어 큐 ÷ 원본 큐 중앙 1.00), 10큐 넘는 장면은",
            "**중앙 0.61**까지 줄였다(최소 0.12). **긴 장면일수록 더 버린다.**",
        ]
    # `_SHIPPED` 는 지금 비어 있다 — 검증에서 졌기 때문이다(위 기록 참조).
    # 나중에 이기는 조합을 찾으면 여기에 넣으면 되고, 그때도 기본 해시가
    # 바뀌므로 selftest [23] 이 반드시 걸린다.
    for fn in _SHIPPED:
        lines = fn(lines)
    # 변형은 `extra` 앞에 건다 — `추가 지시(최우선)` 는 언제나 맨 끝이어야
    # 사람이 준 지시가 제일 세게 걸린다.
    if variant:
        if variant not in VARIANTS:
            raise GenError("모르는 프롬프트 변형: %s (%s)"
                           % (variant, ", ".join(VARIANTS)))
        lines = VARIANTS[variant](lines)
    # 이름 조항은 **맨 끝**에 둔다 — 문체 블록 안이 아니라.
    # 실험은 이 문구를 `--extra`(맨 끝)로 줘서 기준선을 24:12 로 이겼는데,
    # 같은 문구를 문체 블록 안에 옮겨 심고 다시 뽑아 서로 붙이자 **12:0 으로
    # 전패**했다(순서를 뒤집어도 6/6 동일). 내용이 아니라 **자리**가 효과를
    # 만든다 — 긴 문체 블록의 한 줄로 묻히면 안 지켜진다.
    lines += [
        "",
        "**이름 — 여기서 제일 자주 무너진다:**",
        "- **이 작품을 안다면 그 지식을 이름에 써라.** 자막에 이름이 안 나오는",
        "  인물도 널리 알려진 이름이 있으면 그 이름으로 불러라 — 역할 호칭만",
        "  남기지 마라.",
        "- **인물이 처음 말하기 전에** 그 앞 나레이션이 반드시 역할+이름으로",
        "  소개한다.",
        "- **한 인물은 대본 전체에서 한 이름으로만 불러라.** 나레이션과 대사에서",
        "  다른 이름(성·애칭·약칭)이 섞이면 보는 사람이 다른 사람으로 안다.",
        "- 대사는 여전히 자막 원문 그대로다.",
    ]
    _mins = _srt_minutes(srt_path)
    lines += [
        "",
        "**영화 전체를 훑지 마라. 한 대목만 써라:**",
        "- 가장 좋은 **연속된 구간 하나**를 골라 그 안에서만 대본을 만든다.",
        "  대략 **10~25분짜리 한 대목**이다%s." % (
            " (이 영화는 %d분이다)" % round(_mins) if _mins else ""),
        "  **영화가 짧아도 전체를 다 쓰지는 마라** — 한 대목이다.",
        "- 그 대목 밖의 사건은 아무리 중요해도 버린다. **결말이 그 대목 밖이면",
        "  결말을 쓰지 마라.**",
        "- 블록 수는 그대로 10~12개다. 좁은 구간을 촘촘히 채워라 — 블록끼리",
        "  몇 분씩 건너뛰면 컷이 중구난방이 된다.",
        "- 정답 3편이 실제로 그렇다: 18.5분 · 23.6분 · 12.3분짜리 한 대목이고",
        "  긴 영화 둘은 결말을 안 썼다.",
    ]
    if extra.strip():
        lines += ["", "추가 지시(최우선): " + extra.strip()]
    return "\n".join(lines)


# 「한 대목만 써라」는 **길이와 상관없이 항상** 건다 (8/27, 유저분 지시).
#
# 한때 40분으로 잘랐다. 22분짜리 시트콤에 걸었더니 1:11 로 졌기 때문인데,
# 그때 걸었던 문구에는 「짧은 영화(30분 이하)면 전체를 써도 된다」는 예외가
# 붙어 있었다. 그래서 **구간은 그대로인 채 대사만 빽빽해졌다**
# (12~13블록/53~58대사 → 11~12블록/60~64대사). 진 건 「한 대목만」이 아니라
# 그 예외였던 셈이다. 예외를 빼면 짧은 영화도 실제로 한 대목을 잡는다.
#
# 「10~25분짜리 한 대목」은 정답 3편의 실측이다(18.5 · 23.6 · 12.3분).
# 영화가 짧으면 그만큼 짧게 잡힌다 — 비율이 아니라 분량으로 적는 이유다.
#
# `_srt_minutes` 는 남는다 — 프롬프트에 실제 길이를 적어 주는 데 쓴다.


def _srt_minutes(srt_path):
    """자막이 덮는 시간(분). 못 읽으면 0 — 그러면 장면 지시를 안 건다."""
    try:
        import subtitle
        cues, _ = subtitle.parse_file(srt_path)
        return (cues[-1]["end_s"] / 60.0) if cues else 0.0
    except Exception:                                       # noqa: BLE001
        return 0.0


def _srt_is_korean(srt_path):
    """자막이 한국어인지. 못 읽으면 한국어로 보고 넘어간다(프롬프트만 달라진다)."""
    try:
        import subtitle
        cues, _ = subtitle.parse_file(srt_path)
        return subtitle.is_korean(cues)
    except Exception:                                       # noqa: BLE001
        return True


# ── 실행 ────────────────────────────────────────────────────────────────────

_UNKNOWN_OPT = ("unknown option", "unexpected argument", "unrecognized",
                "invalid value", "no such option", "unknown flag",
                "error: unexpected",
                "not inside a trusted directory", "--skip-git-repo-check")


# 중간 파일은 실행마다 **그 실행의 결과 폴더**에 둔다(pipeline 이 work_dir 로
# 넘긴다). 그러면 한 실행이 한 폴더로 끝나고 results/ 바로 아래가 안 더러워진다.
# work_dir 없이 부르면 여기로 — 프로젝트 **안**이어야 한다. 에이전트 샌드박스가
# 작업 폴더 밖을 읽고 쓰는 것을 막는다.
# 편집 전에 읽을 줄거리·결말. 대본과 같은 폴더에 둔다.
PLOT_NAME = "줄거리.txt"
WORK = ROOT / ".work"
# 실행 시간 기록은 실행마다가 아니라 도구 전체에 하나뿐이므로 여기 남는다.
TIMING = WORK / "gen_timing.json"
DEFAULT_SECS = 450.0        # 실측: 기본 effort 로 447초 (low 였을 땐 131~202초)


def expected_secs(key):
    """이 에이전트가 보통 얼마나 걸리는지. 최근 5회의 중앙값."""
    try:
        past = json.loads(TIMING.read_text(encoding="utf-8")).get(key) or []
    except (OSError, ValueError):
        past = []
    past = [float(x) for x in past if x][-5:]
    if not past:
        return DEFAULT_SECS
    past.sort()
    return past[len(past) // 2]


# 읽고-고치고-쓰기가 겹치면 한쪽 기록이 통째로 사라진다 — 진행률 추정이 오래된
# 값에 묶인다. 동시 실행을 만들던 「둘 다」 모드는 지웠지만, GUI 가 여러 창을
# 띄우면 같은 파일을 두 프로세스가 만질 수 있어 락은 남긴다.
_TIMING_LOCK = threading.Lock()


def record_secs(key, secs):
    """실제 걸린 시간을 남긴다 — 다음 실행의 진행률 기준이 된다."""
    with _TIMING_LOCK:
        return _record_secs(key, secs)


def _record_secs(key, secs):
    try:
        data = json.loads(TIMING.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault(key, []).append(round(float(secs), 1))
    data[key] = data[key][-5:]
    try:
        TIMING.parent.mkdir(parents=True, exist_ok=True)
        TIMING.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _kill_tree(proc):
    """자식까지 확실히 죽인다.

    terminate() 는 셔임(codex.cmd / claude.exe)만 죽이고 그 아래 node 프로세스가
    남아 계속 돈다. 중단을 눌렀는데 몇 분 더 도는 것처럼 보이는 원인이다.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=_NO_WINDOW,
                           timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.SubprocessError:
        pass


def _dec(b):
    """윈도우 콘솔은 cp949로 뱉는 경우가 있어 둘 다 시도한다."""
    if not b:
        return ""
    for enc in ("utf-8", "cp949"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


NODE_DIRS = [r"%ProgramFiles%\nodejs",
             r"%APPDATA%\npm",
             r"%LOCALAPPDATA%\Programs\nodejs"]


def _child_env():
    """codex.cmd / claude.cmd 는 node 를 PATH 에서 찾는다. 방금 설치했다면
    아직 PATH 에 없을 수 있어 알려진 위치를 붙여 준다.
    (실측: node 가 없으면 '"node" is not recognized' 로 죽는다)"""
    env = dict(os.environ)
    have = env.get("PATH", "")
    for d in (os.path.expandvars(x) for x in NODE_DIRS):
        if os.path.isdir(d) and d.lower() not in have.lower():
            have = have + os.pathsep + d
    env["PATH"] = have
    return env


# ── 로그인 / 로그아웃 ───────────────────────────────────────────────────────
# 브라우저를 열고 사람이 눌러야 하는 대화형 절차라, 결과를 삼키지 않고
# **새 콘솔 창**에서 돌린다. GUI는 창이 닫힌 뒤 상태를 다시 읽는다.

_NEW_CONSOLE = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0

# 주의: `/login` `/logout` 은 **세션 안에서 치는 슬래시 명령**이지 CLI 인자가 아니다.
# 인자로 넘기면 claude 가 그냥 대화형으로 켜져 첫 실행 테마 선택 화면이 뜬다.
# CLI 로는 `claude auth login|logout|status` 다 (`claude auth --help` 로 확인).
AUTH_ACTIONS = {
    "codex":  {"login": ["login"], "logout": ["logout"]},
    "claude": {"login": ["auth", "login"], "logout": ["auth", "logout"]},
}


def auth_action(action, prefer=None, log=print):
    """action: "login" | "logout". 새 콘솔 창을 띄우고 곧바로 돌아온다."""
    found = find_runner(prefer)
    if not found:
        raise GenError("에이전트가 설치돼 있지 않습니다.\n  "
                       + RUNNERS["codex"]["install"])
    key, exe, spec = found
    args = AUTH_ACTIONS.get(key, {}).get(action)
    if not args:
        raise GenError("%s 는 %s 를 지원하지 않습니다." % (spec["label"], action))
    try:
        subprocess.Popen([exe] + args, cwd=str(ROOT.parent),
                         env=_child_env(), creationflags=_NEW_CONSOLE)
    except OSError as e:
        raise GenError("%s 실행 실패: %s" % (spec["label"], e)) from e
    log("  %s %s 창을 띄웠습니다 — 그 창에서 진행하세요." % (spec["label"], action))
    return key


def _prune_work(days=7):
    """work_dir 없이 돌렸던 실행의 잔해를 치운다. 성공하면 바로 지우지만
    실패 때는 진단용으로 남기므로, 오래된 것만 뒤늦게 정리한다."""
    if not WORK.exists():
        return
    cut = time.time() - days * 86400
    for f in WORK.iterdir():
        if f.name == TIMING.name or not f.is_file():
            continue
        try:
            if f.stat().st_mtime < cut:
                f.unlink()
        except OSError:
            pass


def _inside_workdir(path):
    """에이전트 작업 폴더(= 프로젝트 상위) 안인가. 샌드박스가 허용하는 범위다."""
    try:
        Path(path).resolve().relative_to(ROOT.parent.resolve())
        return True
    except ValueError:
        return False


def _resolve_runner(prefer=None):
    """쓸 러너 하나를 확정한다. 없거나 로그인 안 됐으면 GenError."""
    found = find_runner(prefer)
    if not found:
        raise GenError(
            "대본을 만들 에이전트를 찾을 수 없습니다." + chr(10) +
            "  " + RUNNERS["codex"]["install"] + chr(10) +
            "  설치 후에도 안 잡히면 PlotCut/runner.json 에 경로를 적으세요:" + chr(10) +
            '  {"runner": "codex", "exe": "C:/…/codex.cmd"}')
    key, exe, spec = found
    # 로그인 안 된 채로 돌리면 CLI가 401 을 내는데, 그 메시지는 인자 사다리를 타고
    # 내려가 마지막 시도의 엉뚱한 오류로 덮인다. 먼저 여기서 걸러 낸다.
    # `is False` 여야 한다 — logged_in 은 판단 불가면 None 을 주고 "막지 마라"는
    # 뜻이다. `not None` 은 참이라 runner.json 으로 지정한 커스텀 러너가 막힌다.
    if logged_in(spec) is False:
        raise GenError(
            "%s 에 로그인되어 있지 않습니다." % spec["label"] + chr(10) +
            "  %s" % spec.get("login", "") + chr(10) +
            "  다른 에이전트가 로그인돼 있으면 GUI의 '대본 생성기'에서 바꿔 쓰세요.")
    return key, exe, spec


AGENT_LOG = "생성중_에이전트로그.txt"


def run_prompt_file(ptxt, work, *, done, key, exe, spec, timeout=1800,
                    timing_key=None, extra_argv=None, log=print, stop=None,
                    progress=None):
    """지시서 파일 하나를 에이전트에게 주고 끝날 때까지 돈다. → (성공, 출력 전문, t0)

    `generate()` 에서 떼어낸 러너 코어다. 계약은 한 줄이다 — 「이 파일을 읽고 수행해라」.
    대본에 대해 아무것도 모르므로 **판정·순위 같은 다른 프롬프트에도 그대로 쓴다.**

    · `done()` 이 참이면 성공. 무엇이 성공인지는 호출자가 안다(대본은 초안 파일 존재,
      판정은 결과 파일 존재).
    · 사다리(`spec["argv"]`)는 `_UNKNOWN_OPT` 에 걸릴 때만 다음 단으로 — 인자 호환성
      문제지 프롬프트와 무관하다.
    · `timing_key` 를 주면 그 키로 진행률 추정을 기록한다. 판정처럼 **짧은 실행은
      None 으로** — 대본 예상 450초에 30초가 섞이면 진행률이 30초 만에 97%를 찍는다.
    · `extra_argv` 는 러너별 추가 인자 `{"codex": [...]}` — `{prompt}` 바로 앞에 끼운다.
      판정은 `--output-schema` `-o` `-s read-only` 를 여기로 준다.
    · 프롬프트는 **파일로** 넘긴다. 여러 줄 문자열을 명령행 인자로 주면 codex.cmd 같은
      배치 래퍼가 %* 로 받으면서 줄바꿈이 깨진다(실측).
    · 출력은 파이프가 아니라 **파일**(`AGENT_LOG`)로 받는다 — 파이프는 버퍼가 차면
      자식이 멈춘다(교착). 성공·실패 뒤 정리는 호출자 몫이다.
    """
    prompt = ('"%s" 파일을 읽고, 거기 적힌 지시를 그대로 순서대로 수행해라.' % ptxt)
    extra = list((extra_argv or {}).get(key, []))
    exp = expected_secs(timing_key) if timing_key else 60.0
    if timing_key:
        log("  (지난 실행 기준 %d분쯤 걸립니다)" % max(1, round(exp / 60)))
    outfile = Path(work) / AGENT_LOG
    last, t0 = "", time.time()
    for attempt, template in enumerate(spec["argv"], start=1):
        if stop is not None and stop.is_set():
            raise GenStopped("중단되었습니다.")
        tpl = list(template)
        if extra and "{prompt}" in tpl:
            i = tpl.index("{prompt}")
            tpl[i:i] = extra
        argv = [exe] + [prompt if a == "{prompt}" else a for a in tpl]
        t0 = time.time()
        # subprocess.run 은 끝날 때까지 블록되므로 진행률도 못 올리고 중단도 못 한다.
        # Popen 으로 띄우고 짧게 폴링한다.
        try:
            with open(outfile, "wb") as fh:
                proc = subprocess.Popen(
                    argv, cwd=str(ROOT.parent), stdin=subprocess.DEVNULL,
                    stdout=fh, stderr=subprocess.STDOUT,
                    creationflags=_NO_WINDOW, env=_child_env())
                while proc.poll() is None:
                    if stop is not None and stop.is_set():
                        _kill_tree(proc)
                        raise GenStopped("중단되었습니다.")
                    el = time.time() - t0
                    if el > timeout:
                        _kill_tree(proc)
                        raise GenError("에이전트가 %d분 안에 끝내지 못했습니다."
                                       % (timeout // 60))
                    if progress:
                        # 97%에서 멈춰 세운다 — 다 됐다고 거짓말하지 않는다
                        progress(min(0.97, el / max(1.0, exp)))
                    time.sleep(0.4)
        except FileNotFoundError as e:
            raise GenError("에이전트를 실행할 수 없습니다: %s" % exe) from e
        out = _dec(outfile.read_bytes() if outfile.exists() else b"")
        last = out.strip()
        if done():
            if timing_key:
                record_secs(timing_key, time.time() - t0)
            if progress:
                progress(1.0)
            return True, out, t0
        low = last.lower()
        if _looks_quota(last):
            # 인자 문제가 아니다 — 다음 단으로 내려가 봐야 같은 벽이다.
            break
        if attempt < len(spec["argv"]) and any(k in low for k in _UNKNOWN_OPT):
            log("  (인자 조합을 바꿔 다시 시도합니다)")
            continue
        break
    return False, last, t0


def generate(srt_path, out_path, target_s=180, extra="", movie_title="",
             prefer=None,
             work_dir=None, timeout=1800, log=print, stop=None, progress=None,
             variant=None, keep_log=False):
    """에이전트를 돌려 대본 txt를 만든다. 반환: Path(out_path)."""
    key, exe, spec = _resolve_runner(prefer)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 기존 파일은 **손대지 않는다.** 사람이 고친 대본을 덮어쓰면 되돌릴 수 없다.
    # (실제로 정답 샘플이 이 경로에 있어서 두 번 날아갔다.)
    # 이미 있으면 옆에 _2, _3 … 으로 새로 만든다.
    if out_path.exists():
        stem, suf, n = out_path.stem, out_path.suffix, 2
        while out_path.exists():
            out_path = out_path.with_name("%s_%d%s" % (stem, n, suf))
            n += 1
        log("  (기존 대본이 있어 '%s' 로 새로 만듭니다)" % out_path.name)

    # 프롬프트는 **파일로** 넘긴다. 여러 줄 문자열을 명령행 인자로 주면
    # codex.cmd / claude.cmd 같은 배치 래퍼가 %* 로 받으면서 줄바꿈이 깨진다
    # (실측: 지시가 한 줄로 뭉개져 에이전트가 출력 경로를 못 찾았다).
    # 이름에 출력 파일명을 섞는다. 고정 이름이면 두 개를 동시에 돌릴 때
    # (자막 여러 편을 한 번에 뽑거나 GUI와 CLI가 겹칠 때) 서로 프롬프트를
    # 덮어써서 엉뚱한 자막으로 대본이 나온다.
    # 폴더가 실행마다 새로 생기므로 파일 이름에 구분자를 섞을 필요가 없다.
    # (전에는 고정 이름이 동시 실행끼리 서로 덮어써서 엉뚱한 자막으로 대본이
    #  나왔고, 그걸 막으려고 이름에 태그를 섞었다.)
    work = Path(work_dir) if work_dir else WORK
    work.mkdir(parents=True, exist_ok=True)
    ptxt = work / "생성중_지시서.txt"
    _prune_work()

    # 에이전트에게는 **프로젝트 안**에 쓰게 하고 우리가 옮긴다.
    # 에이전트 샌드박스는 작업 폴더 밖 쓰기를 막는다 — 자막이 D:\영화\ 처럼
    # 밖에 있으면 대본을 다 써놓고도 엉뚱한 곳에 떨어져
    # "대본 파일을 만들지 못했습니다" 로 실패한다(실측: mkdir 거부).
    staged = work / "생성중_초안.txt"
    staged_plot = work / "생성중_줄거리.txt"
    staged_plot.unlink(missing_ok=True)
    staged.unlink(missing_ok=True)

    # 자막도 **프로젝트 안으로 복사**해서 넘긴다. 샌드박스는 작업 폴더 밖을
    # 읽는 것도 막는다 — 자막이 D:\영화\ 처럼 밖에 있으면 에이전트가
    # "허용 경로 밖이라 Read·Bash 양쪽 다 차단됐습니다" 로 아무것도 못 하고
    # 끝난다(실측). 쓰기만 스테이징해서는 부족하다.
    # check 명령도 이 사본을 쓰므로 큐 번호·본문이 원본과 같아야 한다 → 바이트 복사.
    sub_in = Path(srt_path)
    if _inside_workdir(sub_in):
        sub_for_agent = sub_in
    else:
        sub_for_agent = work / ("생성중_자막사본%s" % (sub_in.suffix.lower() or ".srt"))
        try:
            shutil.copyfile(str(sub_in), str(sub_for_agent))
        except OSError as e:
            raise GenError("자막 파일을 읽을 수 없습니다: %s\n  %s"
                           % (sub_in, e)) from e

    with open(ptxt, "w", encoding="utf-8", newline="\n") as f:
        f.write(build_prompt(sub_for_agent, staged, target_s, extra, movie_title,
                             plot_path=staged_plot, variant=variant))
    def _done():
        return staged.exists() and staged.stat().st_size > 0

    log("  %s 로 대본을 만듭니다 — 몇 분 걸립니다" % spec["label"])
    log("  (%s)" % exe)
    outfile = work / AGENT_LOG
    ok, out, t0 = run_prompt_file(ptxt, work, done=_done, key=key, exe=exe, spec=spec,
                                  timeout=timeout, timing_key=key,
                                  log=log, stop=stop, progress=progress)
    last = out.strip()
    if ok:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(out_path))
        # 줄거리는 있으면 좋고 없어도 그만이다 — 대본이 나온 게 성공이다.
        if staged_plot.exists() and staged_plot.stat().st_size > 0:
            shutil.move(str(staged_plot),
                        str(out_path.parent / PLOT_NAME))
        else:
            log("  (줄거리 파일은 안 나왔습니다 — 대본은 정상입니다)")
        # 성공했으면 중간 파일은 남길 이유가 없다 (실패 때만 남겨 진단에 쓴다).
        # 실험에선 남긴다 — 에이전트가 자기 점검을 **실제로 했는지**는 로그의
        # 편집 이벤트(썼다 → check → 다시 고쳤다)로만 알 수 있다. `생성중_` 접두어를
        # 떼야 `_discard_dir` 이 중간 파일이 아니라 산출물로 본다.
        ptxt.unlink(missing_ok=True)
        if keep_log and outfile.exists():
            shutil.move(str(outfile), str(out_path.parent / "에이전트로그.txt"))
        else:
            outfile.unlink(missing_ok=True)
        if sub_for_agent != sub_in:
            sub_for_agent.unlink(missing_ok=True)
        log("  대본 생성 완료 (%.0f초)" % (time.time() - t0))
        tail = [l for l in out.splitlines() if l.strip()][-3:]
        for l in tail:
            log("    " + l.strip()[:110])
        return out_path

    if _looks_quota(last):
        raise QuotaError(
            "%s 의 **사용량 한도**에 걸렸습니다%s.\n"
            "  도구나 설정 문제가 아닙니다 — 에이전트 CLI 자체가 거절했습니다.\n"
            "  · 잠시 뒤에 다시 돌려 보세요\n"
            "  · 다른 에이전트가 깔려 있으면 그걸로 돌릴 수 있습니다 (설정에서 바꾸세요)\n"
            "  에이전트 출력(끝부분):\n%s"
            % (spec["label"], _quota_hint(last),
               "\n".join(("    " + l) for l in last.splitlines()[-6:])))
    if _looks_read_only(last):
        raise GenError(
            "%s 가 **읽기 전용**으로 돌아 대본 파일을 만들지 못했습니다.\n"
            "  인자가 틀린 게 아니라 에이전트의 쓰기 권한 문제입니다.\n"
            "  · %s 를 최신으로 올려 보세요 (오래된 버전은 쓰기 옵션 이름이 다릅니다)\n"
            "  · 그래도 안 되면 PlotCut/runner.json 에 인자를 직접 적을 수 있습니다:\n"
            '    {\"runner\": \"codex\", \"argv\": [\"exec\", \"--skip-git-repo-check\",\n'
            '                                   \"--approve-for-me\", \"{prompt}\"]}\n'
            "  쓰라고 준 경로: %s\n"
            "  에이전트 출력(끝부분):\n%s"
            % (spec["label"], spec["label"], staged,
               "\n".join(("    " + l) for l in last.splitlines()[-12:])))
    raise GenError(
        "에이전트가 대본 파일을 만들지 못했습니다.\n"
        "  쓰라고 준 경로: %s\n"
        "  에이전트 출력(끝부분):\n%s"
        % (staged, "\n".join(("    " + l) for l in last.splitlines()[-12:])))


# ── 후보 순위 판정 ───────────────────────────────────────────────────────────
# 같은 프롬프트로도 판마다 갈리는 영화가 있다(gentleman: 7판 중 5판이 다른 줄기).
# 후보를 여럿 뽑아 **에이전트에게 순위를 매기게** 하면 그 편차가 이득이 된다 —
# 어떤 대본이 좋은지 우리가 몰라도 된다. 순위는 순환 3순서로 세 번 받는다:
# 심사자는 우열을 못 가르면 전원 1번을 찍으므로(자기 검사 9표 전원) 각 후보가
# 1번 자리에 정확히 한 번씩 서야 그 편향이 상쇄된다.
#
# Codex 는 `--output-schema` + `-o` 로 답을 JSON 파일로 받고 `-s read-only` 로
# 아무것도 못 쓰게 한다. 그 인자가 없는 러너(claude)는 지시서에 「결과 파일을
# 써라」로 대신한다. 어느 쪽이든 파일이 없거나 순위가 순열이 아니면 그 표는 버린다.

RANK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["ranking", "why"],
    "properties": {
        "ranking": {"type": "array", "items": {"type": "integer"},
                    "description": "1위부터. 대본 번호."},
        "why": {"type": "string", "description": "1위를 고른 이유 두 문장 이내"},
    },
}

RANK_RUBRIC = (
    "**종합 관점** — 다섯 가지를 함께 본다:",
    "- 훅: 첫 3줄이 계속 보게 만드는가 (설명부터 시작하면 감점)",
    "- 셋업: 웃기거나 중요한 대사 앞에 깔아 주는 줄이 있는가 (맥락 없이 튀어나오면 감점)",
    "- 결말: 끝이 여운을 남기는가, 뚝 끊기는가",
    "- 줄기: 한 이야기를 끝까지 따라가는가, 여러 갈래를 욱여넣었는가",
    "- 소개: 처음 나오는 인물이 이름·역할로 소개되는가",
)


def neutral_text(doc):
    """대본 → 심사용 중립 형식. `대사`/`나레` + 한 줄. 시각·번호·헤더는 뺀다 —
    형식이 남으면 심사자가 대본이 아니라 형식을 본다."""
    out = []
    for blk in doc["blocks"]:
        for it in blk["items"]:
            tag = "나레" if it["kind"] == "narration" else "대사"
            out.append("%s  %s" % (tag, it["text"].replace(chr(10), " ").strip()))
    return chr(10).join(out)


def _rank_prompt(texts, result_path, can_write):
    n = len(texts)
    lines = [
        "아래는 **같은 영화**로 만든 유튜브 숏츠 영화 리뷰 대본 %d개다." % n,
        "각 줄은 화면에 뜨는 자막 한 장이고, `대사` 는 영화 대사, `나레` 는 내레이션이다.",
        "",
        "**어느 숏츠를 가장 보고 싶은가?** 1위부터 %d위까지 순위를 매겨라." % n,
        "",
        "- 같은 영화라 줄거리가 겹치는 건 당연하다. **어느 쪽이 더 잘 만들었는지**만 봐라.",
        "- 누가 썼는지 추측하지 마라. 오타·띄어쓰기·줄 수로 판단하지 마라.",
        "- 동률은 없다. 반드시 %d개를 다 다른 순위에 놓아라." % n,
        "- 파일을 읽거나 만들거나 고치지 마라. 아래 본문만 보고 답해라.",
        "",
    ]
    lines += list(RANK_RUBRIC)
    lines += ["", "답은 JSON 하나로만 한다 — 다른 말을 붙이지 마라:",
              '  {"ranking": [1위 번호, 2위 번호, ...], "why": "1위를 고른 이유 두 문장 이내"}']
    if can_write:
        lines += ["그 JSON 을 이 파일에 써라: %s" % result_path]
    for i, t in enumerate(texts, 1):
        lines += ["", "=" * 28 + " %d번 " % i + "=" * 28, t]
    return chr(10).join(lines) + chr(10)


def _parse_ranking(path, n):
    """결과 파일 → 자리 번호 순위. 순열이 아니면 None (그 표는 버린다)."""
    try:
        raw = Path(path).read_text(encoding="utf-8-sig").strip()
        i, j = raw.find("{"), raw.rfind("}")
        d = json.loads(raw[i:j + 1])
        r = [int(x) for x in d["ranking"]]
        if sorted(r) != list(range(1, n + 1)):
            return None, ""
        return r, str(d.get("why", ""))[:200]
    except Exception:                                       # noqa: BLE001
        return None, ""


def rank_candidates(paths, cues, work, *, prefer=None, log=print, stop=None,
                    timeout=300):
    """후보 대본들의 순위. → (이긴 후보 인덱스(0부터), 평균 순위 목록, 이유) 또는 None.

    순환 3순서 (1,2,3)(2,3,1)(3,1,2) — 각 후보가 1번 자리에 한 번씩. 표마다 별개
    폴더(`work/심사k/`), 타임아웃 300초, **timing 기록 안 함**(대본 예상을 오염시킨다).
    한 표도 못 받으면 None — 호출자가 후보 1을 쓴다. 심사 실패는 오류가 아니다.
    """
    import script_io
    n = len(paths)
    if n < 2:
        return None
    key, exe, spec = _resolve_runner(prefer)
    texts = []
    for pth in paths:
        doc = script_io.read(Path(pth).read_bytes().decode("utf-8-sig"), cues,
                             log=lambda *_: None)
        texts.append(neutral_text(doc))
    work = Path(work)
    schema = work / "심사스키마.json"
    work.mkdir(parents=True, exist_ok=True)
    schema.write_text(json.dumps(RANK_SCHEMA, ensure_ascii=False), encoding="utf-8")
    orders = [[(s + i) % n for i in range(n)] for s in range(n)]
    sums = [0.0] * n
    firsts = [0] * n
    votes = 0
    why = ""
    for k, order in enumerate(orders, 1):
        if stop is not None and stop.is_set():
            break
        wd = work / ("심사%d" % k)
        wd.mkdir(parents=True, exist_ok=True)
        result = wd / "심사결과.json"
        result.unlink(missing_ok=True)
        ptxt = wd / "생성중_지시서.txt"
        can_write = key != "codex"
        ptxt.write_text(_rank_prompt([texts[i] for i in order], result, can_write),
                        encoding="utf-8")
        extra = {"codex": ["--output-schema", str(schema), "-o", str(result),
                           "-s", "read-only"]}
        try:
            ok, out, _ = run_prompt_file(
                ptxt, wd, done=lambda: result.exists() and result.stat().st_size > 0,
                key=key, exe=exe, spec=spec, timeout=timeout, timing_key=None,
                extra_argv=extra, log=lambda *_: None, stop=stop)
        except GenError as e:
            log("  (심사 %d회 실패 — %s)" % (k, str(e).splitlines()[0][:60]))
            continue
        rank, reason = _parse_ranking(result, n) if ok else (None, "")
        if rank is None:
            log("  (심사 %d회 — 답을 읽지 못해 버립니다)" % k)
            continue
        votes += 1
        for place, slot in enumerate(rank, 1):
            cand = order[slot - 1]
            sums[cand] += place
            if place == 1:
                firsts[cand] += 1
        if not why and reason:
            why = reason
        log("  심사 %d회: %s" % (k, " > ".join("후보%d" % (order[s - 1] + 1) for s in rank)))
    if votes == 0:
        return None
    avg = [x / votes for x in sums]
    # 평균 순위 → 1위 횟수 → 앞 후보 순으로 동률을 푼다
    best = min(range(n), key=lambda i: (avg[i], -firsts[i], i))
    return best, avg, why
