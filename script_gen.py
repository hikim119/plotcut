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


def _looks_read_only(out):
    low = (out or "").lower()
    return any(k in low for k in _NO_WRITE)


def build_prompt(srt_path, out_path, target_s=180, extra="", movie_title="",
                 plot_path=None):
    srt_path = Path(srt_path).resolve()
    out_path = Path(out_path).resolve()
    movie_title = (movie_title or "").strip()
    lines = [
        "PlotCut 영화 리캡 대본을 만들어라. 아래를 순서대로 반드시 수행한다.",
        "",
        # 문체 예시를 **맨 앞**에 둔다. 에이전트는 표의 숫자가 아니라 눈앞의 예시를
        # 베낀다 — 실측: 예시가 중경삼림(중앙 30자)이던 시절 출력이 25자로 따라갔다.
        '· **먼저 "%s" 를 전문 읽어라. 이게 문체의 정답이다.**' % STYLE,
        "   실제로 잘 나간 숏츠 3편의 나레이션 34문장 전문이다.",
        "   **같은 길이·같은 종결·같은 리듬으로 써라.** 특히 이 넷:",
        "   · 한 덩어리 **중앙 16자**. 21자 넘는 줄은 열에 넷 미만",
        "   · **문장을 닫지 마라.** 열에 일곱은 `~는데`·`~고`. `~다.` 는 34문장에 0개",
        "   · **8~15초에 하나.** 180초면 12~20문장. 연속은 2문장까지",
        "   · 이야기가 뒤집히는 **딱 한 자리**에 `~죠` 하나",
        "   · 나레이션은 **말이 아니라 반응·상태 변화**를 적는다",
        "   · **영화의 말투를 그대로 써라.** 액션·코미디면 구어체, 문예물이면 문어체",
        '· "%s" 의 ① 절들을 전문 읽어라. 포맷·문체 규칙·분량 공식이 거기 있다.' % RULES,
        "   · **첫 문단은 훅이다** (규칙 2). 2초 안에 주인공이 드러나게 한다",
        "   · **마지막 문단은 14자 이하 짧은 대사**로 끝낸다. 나레이션으로 정리하지 마라",
        "   · 한 문단 = **자막 큐 하나**가 기본 (실측 94%). 큐가 2줄이면 **2줄 그대로**",
        '· 자막 파일: "%s"' % srt_path,
        "   **SRT 파일에 적힌 번호를 기준으로 읽어라.**",
    ]
    # 내 완성본 원본이 있으면(로컬) 대사까지 통째로 읽힌다. gitignore 대상이라
    # 받아 쓴 사람에겐 이 줄이 안 붙고 위의 나레이션 예시로만 간다.
    _samples = ROOT / "대본예시"
    if _samples.is_dir() and any(_samples.glob("*.srt")):
        lines += [
            '· "%s" 안의 srt 는 **실제로 잘 나간 완성본**이다. 전부 읽어라.' % _samples,
            "   대사와 나레이션이 어떤 리듬으로 섞이는지 여기서 배운다. 이게 최종 기준이다.",
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
            "    #176 대박, 저 귀걸이 좀 봐",
            "    #181 저기요, 이거 얼마예요?",
            "    #182-183 파는 거 아냐, 진짜 유물이거든",
            "",
            "`#176` 은 SRT 파일에 적힌 번호다. 그 번호로 컷 위치를 찾는다 —",
            "번호를 빼면 한국어 문장과 자막을 대조할 수 없어 전부 실패한다.",
            "`#182-183` 은 연속 두 큐를 한 문단으로 합친 것(상한 2개).",
            "빠짐없이 **모든 대사 문단**에 번호를 달아라.",
            "번역은 자막 직역이 아니라 **숏츠 화면에서 읽히게** 짧고 입말로 써라.",
            "완성된 대본에 영어가 한 글자도 남으면 안 된다.",
        ]
    if extra.strip():
        lines += ["", "추가 지시(최우선): " + extra.strip()]
    return "\n".join(lines)


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


def record_secs(key, secs):
    """실제 걸린 시간을 남긴다 — 다음 실행의 진행률 기준이 된다."""
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


def generate(srt_path, out_path, target_s=180, extra="", movie_title="",
             prefer=None,
             work_dir=None, timeout=1800, log=print, stop=None, progress=None):
    """에이전트를 돌려 대본 txt를 만든다. 반환: Path(out_path)."""
    found = find_runner(prefer)
    if not found:
        raise GenError(
            "대본을 만들 에이전트를 찾을 수 없습니다.\n"
            "  " + RUNNERS["codex"]["install"] + "\n"
            "  설치 후에도 안 잡히면 PlotCut/runner.json 에 경로를 적으세요:\n"
            '  {"runner": "codex", "exe": "C:/…/codex.cmd"}')
    key, exe, spec = found
    # 로그인 안 된 채로 돌리면 CLI가 401 을 내는데, 그 메시지는 인자 사다리를 타고
    # 내려가 마지막 시도의 엉뚱한 오류로 덮인다. 먼저 여기서 걸러 낸다.
    # `is False` 여야 한다 — logged_in 은 판단 불가면 None 을 주고 "막지 마라"는
    # 뜻이다. `not None` 은 참이라 runner.json 으로 지정한 커스텀 러너가 막힌다.
    if logged_in(spec) is False:
        raise GenError(
            "%s 에 로그인되어 있지 않습니다.\n"
            "  %s\n"
            "  다른 에이전트가 로그인돼 있으면 GUI의 '대본 생성기'에서 바꿔 쓰세요."
            % (spec["label"], spec.get("login", "")))
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
                             plot_path=staged_plot))
    prompt = ('"%s" 파일을 읽고, 거기 적힌 지시를 그대로 순서대로 수행해라.' % ptxt)

    log("  %s 로 대본을 만듭니다 — 몇 분 걸립니다" % spec["label"])
    log("  (%s)" % exe)

    last = ""
    exp = expected_secs(key)
    log("  (지난 실행 기준 %d분쯤 걸립니다)" % max(1, round(exp / 60)))
    outfile = work / "생성중_에이전트로그.txt"
    for attempt, template in enumerate(spec["argv"], start=1):
        if stop is not None and stop.is_set():
            raise GenStopped("중단되었습니다.")
        argv = [exe] + [prompt if a == "{prompt}" else a for a in template]
        t0 = time.time()
        # subprocess.run 은 끝날 때까지 블록되므로 진행률도 못 올리고 중단도 못 한다.
        # Popen 으로 띄우고 짧게 폴링한다. 출력은 **파일로** 받는다 —
        # 파이프로 받으면 버퍼가 차면서 자식이 멈춘다(교착).
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

        if staged.exists() and staged.stat().st_size > 0:
            record_secs(key, time.time() - t0)
            if progress:
                progress(1.0)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), str(out_path))
            # 줄거리는 있으면 좋고 없어도 그만이다 — 대본이 나온 게 성공이다.
            if staged_plot.exists() and staged_plot.stat().st_size > 0:
                shutil.move(str(staged_plot),
                            str(out_path.parent / PLOT_NAME))
            else:
                log("  (줄거리 파일은 안 나왔습니다 — 대본은 정상입니다)")
            # 성공했으면 중간 파일은 남길 이유가 없다 (실패 때만 남겨 진단에 쓴다)
            for tmp in (ptxt, outfile):
                tmp.unlink(missing_ok=True)
            if sub_for_agent != sub_in:
                sub_for_agent.unlink(missing_ok=True)
            log("  대본 생성 완료 (%.0f초)" % (time.time() - t0))
            tail = [l for l in out.splitlines() if l.strip()][-3:]
            for l in tail:
                log("    " + l.strip()[:110])
            return out_path

        low = last.lower()
        if attempt < len(spec["argv"]) and any(k in low for k in _UNKNOWN_OPT):
            log("  (인자 조합을 바꿔 다시 시도합니다)")
            continue
        break

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
