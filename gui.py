"""
PlotCut — 「타임라인 대본」 txt 하나로 CapCut 컷편집

  [▶ CapCut 프로젝트 만들기]  txt + 자막 + 영화 → 컷 + 대사자막 + 나레이션자막

대본은 클로드 코드가 쓴다. 이 프로그램은 API를 부르지 않는다.
"""

import subprocess
import sys

# Windows: ffmpeg/ffprobe 콘솔 창이 깜빡이지 않게
if sys.platform == "win32":
    _OrigPopen = subprocess.Popen

    class _NoCWindowPopen(_OrigPopen):
        def __init__(self, *args, **kwargs):
            if "creationflags" not in kwargs:
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoCWindowPopen

import os                                                   # noqa: E402
import re                                                   # noqa: E402
import threading                                            # noqa: E402
import time                                                 # noqa: E402
import tkinter as tk                                        # noqa: E402
from pathlib import Path                                    # noqa: E402
from tkinter import filedialog, messagebox                  # noqa: E402

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

import guards                                               # noqa: E402
import pipeline                                             # noqa: E402
import script_io                                             # noqa: E402

# ── 색상 팔레트 (다른 툴들과 통일) ──────────────────────────────────────────
BG = "#1e1e2e"
BG2 = "#2a2a3e"
BG3 = "#313148"
ACCENT = "#7c85f0"
ACCENT2 = "#5a63c8"
TEXT = "#e0e0f0"
TEXTMUT = "#888899"
GREEN = "#50d890"
RED = "#f07070"
YELLOW = "#f0c674"
BORDER = "#3a3a55"

# 자막은 **subtitle.py 가 실제로 읽을 수 있는 것만** 올린다.
# 확장자만 늘리면 파일을 받아 놓고 큐 0개로 실패한다.
SCRIPT_EXTS = (".txt",)
SUB_EXTS = (".srt", ".smi", ".sami", ".vtt", ".ass", ".ssa")
# 영상·오디오는 ffprobe 가 읽고 CapCut 이 가져갈 수 있는 컨테이너들 (CutSync 기준 + α)
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v", ".wmv", ".webm",
              ".flv", ".mpg", ".mpeg", ".m2ts", ".mts", ".3gp", ".ogv")

EXTRA_HINT = "\n".join([
    "비워도 됩니다. 원하는 걸 편하게, 길게 적으세요. 예:",
    "남자 주인공 이름은 잭, 여자 주인공은 에이미로 써 줘",
    "잭이 에이미를 처음 만나는 장면부터 시작해 줘",
    "나레이션은 짧게, 결말은 끝까지 감춰 줘",
])

CANVAS_MAP = {"세로 9:16": "vertical", "정사각": "square", "가로 16:9": "horizontal"}
CANVAS_KO = list(CANVAS_MAP)
# 대본이 영화의 어디를 쓸지. 실측(완성본 3편 × 원본 자막 대조)에서 기본은
# 명확히 `한 장면` 이다 — 영화 12~24분 구간만 쓰고 둘은 결말도 안 썼다.
SCOPE_MAP = {"한 장면": "scene", "영화 전체 요약": "full"}
SCOPE_KO = list(SCOPE_MAP)
SCOPE_TIP = "한 장면 = 결정적인 시퀀스 하나만 (완성본 3편이 쓴 방식)"

SLOTS = [
    ("script", "1.  타임라인 대본", "선택 — 비우면 영화 자막으로 만들어 줍니다", SCRIPT_EXTS),
    ("srt", "2.  영화 자막", "필수 — 대본의 대사를 찾을 원본 자막 (srt/smi/vtt/ass)", SUB_EXTS),
    ("movie", "3.  영화 파일", "선택 — 없으면 자막·타임라인만 (영상은 나중에)", VIDEO_EXTS),
]


def _set_app_icon(root):
    """작업표시줄·창 아이콘을 PlotCut 것으로.

    pythonw.exe 로 띄우면 윈도우가 **호스트 프로세스(python)의 아이콘**을 물려준다.
    iconbitmap 만으로는 창 좌상단만 바뀌고 작업표시줄은 그대로다 —
    SetCurrentProcessExplicitAppUserModelID 로 별개 앱이라고 알려야 바뀐다.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            # 이게 없으면 작업표시줄이 pythonw.exe 의 아이콘을 쓴다.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "PlotCut.PlotCut")
        except Exception:                                   # noqa: BLE001
            pass
    ico = Path(__file__).resolve().parent / "icon.ico"
    if not ico.exists():
        return
    try:
        root.iconbitmap(default=str(ico))   # 이 앱의 모든 창에 적용
    except tk.TclError:
        pass
    try:
        root.iconbitmap(str(ico))           # 이 창에 직접
    except tk.TclError:
        pass


class App:
    def __init__(self, root):
        self.root = root
        root.title("PlotCut — 타임라인 대본 → CapCut")
        root.geometry("860x900")
        root.minsize(760, 620)
        root.configure(bg=BG)
        _set_app_icon(root)

        self.paths = {k: None for k, *_ in SLOTS}
        self.labels = {}
        self._stop = threading.Event()
        self._busy = False
        self.name_var = tk.StringVar()
        self.title_var = tk.StringVar()
        self.canvas_ko = tk.StringVar(value=CANVAS_KO[0])
        self.offset_var = tk.DoubleVar(value=0.0)
        self.secs_var = tk.StringVar(value="180")
        # 기본은 **끔** — 영화 소리를 100% 그대로 둔다.
        self.mute_var = tk.BooleanVar(value=False)
        # 기본은 **한 장면** — 완성본 3편이 전부 그렇다(영화 12~24분 구간만 씀).
        self.scope_ko = tk.StringVar(value=SCOPE_KO[0])
        self.agent_ko = tk.StringVar()
        self.auth_lbl = None
        self._frac = 0.0
        self._t0 = 0.0
        self._step = ""

        self._build()
        # 로그는 **작업 기록**이다. 시작할 때 안내문·자랑을 채우지 않는다.
        # 에이전트 상태는 위 라벨에 이미 색으로 떠 있다.
        self._log("자막 파일을 넣고 [CapCut 프로젝트 만들기]를 누르세요.")
        try:
            self._refresh_auth()
        except Exception:                                   # noqa: BLE001
            pass
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── 화면 ────────────────────────────────────────────────────────────
    def _build(self):
        root = self.root

        # 하단 바를 먼저 pack 해야 창이 작아져도 버튼이 잘리지 않는다
        bottom = tk.Frame(root, bg=BG)
        bottom.pack(side="bottom", fill="x", padx=16, pady=(0, 12))
        self._build_bottom(bottom)

        head1 = tk.Label(root, text="PlotCut", bg=BG, fg=ACCENT,
                         font=("맑은 고딕", 16, "bold"))
        head1.pack(pady=(14, 2))
        head2 = tk.Label(root, text="영화 자막 → 리캡 대본 → CapCut 컷편집",
                         bg=BG, fg=TEXTMUT, font=("맑은 고딕", 9))
        head2.pack(pady=(0, 10))

        # 슬롯은 CutSync처럼 한 박스에 묶는다 — 카드 4장보다 훨씬 덜 산만하다
        box = tk.Frame(root, bg=BG2, highlightbackground=BORDER,
                       highlightthickness=1)
        box.pack(fill="x", padx=16, pady=(0, 10))
        for key, title, hint, _ex in SLOTS:
            self._slot(box, key, title, hint)
        tk.Label(box,
                 text=("파일을 창에 끌어다 놓으면 종류를 자동으로 알아봅니다"
                       if DND_AVAILABLE else "[선택] 버튼으로 파일을 지정하세요"),
                 bg=BG2, fg=TEXTMUT, font=("맑은 고딕", 8)).pack(pady=(2, 8))

        self._build_settings(root)
        self.name_var.trace_add("write", self._update_name_hint)
        self._build_actions(root)
        self._build_progress(root)
        self._build_tabs(root)
        self._update_name_hint()

        if DND_AVAILABLE:
            try:
                root.drop_target_register(DND_FILES)
                root.dnd_bind("<<Drop>>", self._on_drop)
            except (tk.TclError, AttributeError):
                pass

    def _hint(self, entry, text):
        """빈 칸일 때만 흐리게 보이는 안내문 (플레이스홀더)."""
        def show():
            if not entry.get():
                entry.insert(0, text)
                entry.configure(fg=TEXTMUT)

        def clear(_e=None):
            if entry.get() == text:
                entry.delete(0, "end")
                entry.configure(fg=TEXT)

        def restore(_e=None):
            show()

        entry.bind("<FocusIn>", clear)
        entry.bind("<FocusOut>", restore)
        entry.__dict__["_placeholder"] = text
        show()

    def _hint_text(self, widget, text):
        """여러 줄 칸용 플레이스홀더. 비어 있을 때만 흐린 안내문을 보여준다."""
        def show():
            if not widget.get("1.0", "end").strip():
                widget.insert("1.0", text)
                widget.configure(fg=TEXTMUT)

        def clear(_e=None):
            if widget.get("1.0", "end").strip() == text.strip():
                widget.delete("1.0", "end")
                widget.configure(fg=TEXT)

        widget.bind("<FocusIn>", clear)
        widget.bind("<FocusOut>", lambda _e=None: show())
        show()

    def _slot(self, parent, key, title, hint):
        row = tk.Frame(parent, bg=BG2)
        row.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(row, text=title, bg=BG2, fg=TEXT, width=15, anchor="w",
                 font=("맑은 고딕", 9, "bold")).pack(side="left")
        lbl = tk.Label(row, text=hint, bg=BG3, fg=TEXTMUT, anchor="w",
                       font=("맑은 고딕", 8), padx=6, pady=3)
        lbl.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.labels[key] = lbl
        tk.Button(row, text="선택", command=lambda k=key: self._pick(k),
                  bg=BG3, fg=TEXT, relief="flat", cursor="hand2", padx=10,
                  font=("맑은 고딕", 8),
                  activebackground=ACCENT2).pack(side="right")
        tk.Button(row, text="✕", command=lambda k=key: self._clear(k),
                  bg=BG2, fg=TEXTMUT, relief="flat", cursor="hand2", padx=4,
                  font=("맑은 고딕", 8),
                  activebackground=RED).pack(side="right", padx=(0, 2))

    def _build_settings(self, root):
        box = tk.Frame(root, bg=BG2, highlightbackground=BORDER,
                       highlightthickness=1)
        box.pack(fill="x", padx=16, pady=(0, 10))
        self._settings_box = box

        def row(pad=(8, 4)):
            f = tk.Frame(box, bg=BG2)
            f.pack(fill="x", padx=12, pady=pad)
            return f

        def title(f, t):
            tk.Label(f, text=t, bg=BG2, fg=TEXT, width=13, anchor="w",
                     font=("맑은 고딕", 9, "bold")).pack(side="left")

        r = row((10, 4))
        title(r, "프로젝트명")
        tk.Entry(r, textvariable=self.name_var, bg=BG3, fg=TEXT, relief="flat",
                 insertbackground=TEXT, font=("맑은 고딕", 9)).pack(
                     side="left", fill="x", expand=True, ipady=3)

        # 파일 이름이 영화 제목인 경우가 오히려 드물다(`English.srt`,
        # `The.Movie.2019.1080p.BluRay-GROUP.srt`). 제목을 알려 주면 에이전트가
        # 아는 줄거리·결말로 어느 장면이 결정적인지 고른다. 몰라도 그만이다.
        r = row((0, 4))
        title(r, "영화 제목")
        e = tk.Entry(r, textvariable=self.title_var, bg=BG3, fg=TEXT,
                     relief="flat", insertbackground=TEXT,
                     font=("맑은 고딕", 9))
        e.pack(side="left", fill="x", expand=True, ipady=3)
        self._hint(e, "모르면 비워 두세요 — 알면 줄거리·결말을 참고해 씁니다")

        # 비워 두면 무엇으로 만들어지는지 미리 보여 준다. 칸에 직접 채워 넣지
        # 않는 이유: 채워 두면 다른 영화를 넣어도 그 이름이 남아 엉뚱한
        # 프로젝트를 덮어쓴다. 비어 있으면 만들 때의 시각으로 새로 짓는다.
        h = row((0, 4))
        tk.Label(h, text="", bg=BG2, fg=TEXTMUT, width=13).pack(side="left")
        self.name_hint = tk.Label(h, text="", bg=BG2, fg=TEXTMUT, anchor="w",
                                  font=("맑은 고딕", 8))
        self.name_hint.pack(side="left", fill="x", expand=True)

        def chk(parent, var, text, tip):
            c = tk.Checkbutton(parent, text=text, variable=var, bg=BG2, fg=TEXT,
                               selectcolor=BG3, relief="flat", anchor="w",
                               activebackground=BG2, activeforeground=ACCENT,
                               font=("맑은 고딕", 9))
            c.pack(side="left")
            tk.Label(parent, text=tip, bg=BG2, fg=TEXTMUT,
                     font=("맑은 고딕", 8)).pack(side="left", padx=(6, 0))

        r = row()
        title(r, "옵션")
        chk(r, self.mute_var, "나레이션 구간 음소거",
            "나레이션 밑에서 원본 대사가 새는 구간을 지웁니다")
        # 영화 전체를 훑을지 한 장면만 쓸지는 **문체보다 먼저** 정해지는 문제라
        # 연출 지시에 묻어 두지 않고 칸으로 뺀다. 실측: 완성본 3편은 영화의
        # 12~24분 구간만 썼고 둘은 결말도 안 썼다. 도구는 49분을 훑었다.
        r = row()
        title(r, "대본 범위")
        tk.OptionMenu(r, self.scope_ko, *SCOPE_KO).pack(side="left")
        tk.Label(r, text=SCOPE_TIP, bg=BG2, fg=TEXTMUT,
                 font=("맑은 고딕", 8)).pack(side="left", padx=(6, 0))

        r = row()
        title(r, "대본 길이")
        tk.Entry(r, textvariable=self.secs_var, bg=BG3, fg=TEXT, width=5,
                 relief="flat", insertbackground=TEXT,
                 font=("맑은 고딕", 9)).pack(side="left", ipady=2)
        tk.Label(r, text="초", bg=BG2, fg=TEXTMUT,
                 font=("맑은 고딕", 9)).pack(side="left", padx=(4, 0))

        # 연출 지시는 한 줄로는 부족하다 — 인물 이름·시작 장면·문체를 한꺼번에
        # 적을 수 있게 여러 줄 칸으로 둔다. 여기 적은 말은 프롬프트 맨 끝에
        # '추가 지시(최우선)' 으로 붙는다.
        r = row()
        tk.Label(r, text="연출 지시", bg=BG2, fg=TEXT, width=13, anchor="nw",
                 font=("맑은 고딕", 9, "bold")).pack(side="left", anchor="n")
        self.extra_text = tk.Text(r, bg=BG3, fg=TEXT, relief="flat", height=4,
                                  wrap="word", padx=6, pady=4, undo=True,
                                  insertbackground=TEXT, font=("맑은 고딕", 9))
        self.extra_text.pack(side="left", fill="x", expand=True)
        self._hint_text(self.extra_text, EXTRA_HINT)

        a = row((4, 10))
        title(a, "대본 생성기")
        import script_gen
        labels = [script_gen.RUNNERS[k]["label"] for k in script_gen.ORDER]
        self._agent_key = {script_gen.RUNNERS[k]["label"]: k for k in script_gen.ORDER}
        if not self.agent_ko.get():
            self.agent_ko.set(labels[0])
        self.agent_ko.trace_add("write", lambda *_: self._refresh_auth())
        tk.OptionMenu(a, self.agent_ko, *labels).pack(side="left", padx=(0, 8))
        for text, cmd in (("로그인", lambda: self._auth("login")),
                          ("로그아웃", lambda: self._auth("logout")),
                          ("상태 새로고침", self._refresh_auth)):
            tk.Button(a, text=text, command=cmd, bg=BG3, fg=TEXT, relief="flat",
                      cursor="hand2", font=("맑은 고딕", 8), padx=8,
                      activebackground=ACCENT2).pack(side="left", padx=(0, 4))
        self.auth_lbl = tk.Label(a, text="", bg=BG2, fg=TEXTMUT,
                                 font=("맑은 고딕", 9), anchor="w")
        self.auth_lbl.pack(side="left", padx=(8, 0), fill="x", expand=True)

    def _build_actions(self, root):
        f = tk.Frame(root, bg=BG)
        f.pack(fill="x", padx=16, pady=(4, 6))
        self._actions_box = f
        self.btn_build = tk.Button(
            f, text="▶  CapCut 프로젝트 만들기", command=self._do_build,
            bg=ACCENT, fg="#ffffff", relief="flat", cursor="hand2",
            font=("맑은 고딕", 11, "bold"), pady=9, activebackground=ACCENT2)
        self.btn_build.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.btn_script = tk.Button(
            f, text="대본만 만들기", command=self._do_script,
            bg=BG3, fg=TEXT, relief="flat", cursor="hand2",
            font=("맑은 고딕", 10), pady=9, padx=14, activebackground=ACCENT2)
        self.btn_script.pack(side="left", padx=(0, 6))
        # 중단·초기화는 실행 버튼 바로 옆에 둔다 — 맨 아래는 눈이 안 간다
        for text, cmd in (("중단", self._do_stop), ("초기화", self._reset)):
            tk.Button(f, text=text, command=cmd, bg=BG2, fg=TEXTMUT,
                      relief="flat", cursor="hand2", font=("맑은 고딕", 9),
                      pady=9, padx=10, activebackground=BG3).pack(side="left",
                                                                  padx=(0, 4))

    def _build_progress(self, root):
        self.bar_bg = tk.Canvas(root, height=7, bg=BG3, highlightthickness=0)
        self.bar_bg.pack(fill="x", padx=16, pady=(0, 2))
        self.bar = self.bar_bg.create_rectangle(0, 0, 0, 7, fill=ACCENT, width=0)
        self.prog_lbl = tk.Label(root, text="", bg=BG, fg=TEXTMUT, anchor="w",
                                 font=("맑은 고딕", 8))
        self.prog_lbl.pack(fill="x", padx=16, pady=(0, 4))

    def _build_bottom(self, f):
        for text, cmd in (("나레이션 복사", self._copy_narration),):
            tk.Button(f, text=text, command=cmd, bg=BG2, fg=TEXTMUT,
                      relief="flat", cursor="hand2", font=("맑은 고딕", 9),
                      padx=8, pady=4, activebackground=BG3).pack(side="left", padx=(0, 5))

    def _build_tabs(self, root):
        wrap_f = self.tab_wrap = tk.Frame(root, bg=BG)
        wrap_f.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        bar = tk.Frame(wrap_f, bg=BG)
        bar.pack(fill="x")
        self.tab_btns = {}
        for key, title in (("log", "로그"), ("plot", "줄거리"),
                           ("script", script_io.LABEL)):
            b = tk.Button(bar, text=title, command=lambda k=key: self._show_tab(k),
                          bg=BG2, fg=TEXTMUT, relief="flat", cursor="hand2",
                          font=("맑은 고딕", 9), padx=14, pady=5)
            b.pack(side="left", padx=(0, 3))
            self.tab_btns[key] = b

        self.tab_body = tk.Frame(wrap_f, bg=BG2, highlightbackground=BORDER,
                                 highlightthickness=1)
        self.tab_body.pack(fill="both", expand=True)

        self.log_frame = tk.Frame(self.tab_body, bg=BG2)
        self.log = tk.Text(self.log_frame, bg=BG2, fg=TEXT, relief="flat",
                           font=("Consolas", 9), wrap="word", padx=10, pady=8)
        sb = tk.Scrollbar(self.log_frame, command=self.log.yview, bg=BG3)
        self.log.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)

        # 편집 전에 영화를 이해하고 들어가라고 두는 탭. 읽기 전용이다 —
        # 대본과 달리 여기서 고쳐도 결과가 달라지지 않으니 오해를 만들지 않는다.
        self.plot_frame = tk.Frame(self.tab_body, bg=BG2)
        self.plot_text = tk.Text(self.plot_frame, bg=BG2, fg=TEXT, relief="flat",
                                 font=("맑은 고딕", 11), wrap="word",
                                 padx=16, pady=10, spacing1=2, spacing3=2)
        sb3 = tk.Scrollbar(self.plot_frame, command=self.plot_text.yview, bg=BG3)
        self.plot_text.configure(yscrollcommand=sb3.set, state="disabled")
        sb3.pack(side="right", fill="y")
        self.plot_text.pack(fill="both", expand=True)
        self._set_plot("")

        self.script_frame = tk.Frame(self.tab_body, bg=BG2)
        sf_bottom = tk.Frame(self.script_frame, bg=BG2)
        sf_bottom.pack(side="bottom", fill="x", padx=8, pady=6)
        tk.Button(sf_bottom, text="저장하고 다시 만들기", command=self._save_script,
                  bg=ACCENT, fg="#fff", relief="flat", cursor="hand2",
                  font=("맑은 고딕", 9), padx=10, pady=4).pack(side="left")
        tk.Label(sf_bottom, text="  고친 내용은 결과 폴더의 txt에 저장됩니다",
                 bg=BG2, fg=TEXTMUT, font=("맑은 고딕", 9)).pack(side="left")
        self.script_text = tk.Text(self.script_frame, bg=BG2, fg=TEXT, relief="flat",
                                   font=("맑은 고딕", 11), wrap="word",
                                   padx=16, pady=10, spacing1=2, spacing3=2,
                                   undo=True, insertbackground=TEXT)
        sb2 = tk.Scrollbar(self.script_frame, command=self.script_text.yview, bg=BG3)
        self.script_text.configure(yscrollcommand=sb2.set)
        sb2.pack(side="right", fill="y")
        self.script_text.pack(fill="both", expand=True)

        self._show_tab("log")

    def _show_tab(self, key):
        for k, b in self.tab_btns.items():
            b.configure(bg=BG3 if k == key else BG2,
                        fg=TEXT if k == key else TEXTMUT)
        for f in (self.log_frame, self.plot_frame, self.script_frame):
            f.pack_forget()
        {"log": self.log_frame, "plot": self.plot_frame,
         "script": self.script_frame}[key].pack(fill="both", expand=True)

    # ── 파일 ────────────────────────────────────────────────────────────
    def _pick(self, key):
        exts = dict((k, e) for k, _t, _h, e in SLOTS)[key]
        p = filedialog.askopenfilename(
            filetypes=[("지원 파일", " ".join("*" + e for e in exts)),
                       ("모든 파일", "*.*")])
        if p:
            self._assign(key, p)

    def _clear(self, key):
        self.paths[key] = None
        hint = dict((k, h) for k, _t, h, _e in SLOTS)[key]
        self.labels[key].configure(text=hint, fg=TEXTMUT)

    def _update_name_hint(self, *_a):
        """프로젝트명 칸이 비었을 때 어떤 규칙으로 지어지는지 알려 준다.

        실제 영화 이름을 넣어 보여주지 않는다 — 넣어 둔 파일이 바뀌면 그때마다
        달라져서, 마치 그 영화로 고정된 것처럼 읽힌다. 규칙만 말하는 편이 낫다.
        """
        if not getattr(self, "name_hint", None):
            return
        self.name_hint.configure(
            text=("적은 이름 그대로 만듭니다 (같은 이름이 있으면 그 자리에서 갱신)"
                  if self.name_var.get().strip() else
                  "비워 두면 영화 이름 뒤에 날짜·시각을 붙여 만듭니다"))

    def _assign(self, key, path):
        self.paths[key] = path
        self.labels[key].configure(text=Path(path).name, fg=GREEN)
        # 처음 넣은 파일 이름을 프로젝트명으로 채워 준다 (확장자와 `_자막`
        # 같은 꼬리는 뗀다). 이미 적혀 있으면 건드리지 않는다 — 사람이 고친
        # 이름이나 '기존' 목록에서 고른 것을 덮으면 안 된다.
        if not self.name_var.get().strip():
            self.name_var.set(pipeline.movie_base(script_path=path))
        self._update_name_hint()
        if key == "script":
            try:
                self._set_script(Path(path).read_bytes().decode("utf-8-sig"))
            except OSError:
                pass

    def _on_drop(self, event):
        for raw in self.root.tk.splitlist(event.data):
            p = raw.strip("{}")
            ext = Path(p).suffix.lower()
            for key, _t, _h, exts in SLOTS:
                if ext in exts:
                    self._assign(key, p)
                    break

    # ── 실행 ────────────────────────────────────────────────────────────
    def _need(self, *keys):
        for k in keys:
            if not self.paths.get(k):
                title = dict((x, t) for x, t, _h, _e in SLOTS)[k]
                messagebox.showwarning("필요한 파일", "%s 을(를) 넣어 주세요." % title)
                return False
        return True

    def _extra(self):
        v = self.extra_text.get("1.0", "end").strip()
        return "" if v == EXTRA_HINT.strip() else v

    def _scope(self):
        """대본 범위 → pipeline 이 쓰는 키."""
        return SCOPE_MAP.get(self.scope_ko.get(), "scene")

    def _title(self):
        """영화 제목. 플레이스홀더가 남아 있으면 빈 값으로 본다."""
        v = self.title_var.get().strip()
        return "" if v.startswith("모르면 비워") else v

    def _agent(self):
        return self._agent_key.get(self.agent_ko.get())

    def _refresh_auth(self):
        import script_gen
        found = script_gen.find_runner(self._agent())
        if not found:
            self.auth_lbl.configure(text="설치 안 됨 — Codex설치.bat 실행", fg=RED)
            return
        key, exe, spec = found
        if key != self._agent():
            self.auth_lbl.configure(
                text="%s 없음 — %s 로 대체" % (self.agent_ko.get(), spec["label"]),
                fg=YELLOW)
            return
        auth = script_gen.logged_in(spec)
        if auth is False:
            self.auth_lbl.configure(text="로그인 필요", fg=YELLOW)
        elif auth is True:
            self.auth_lbl.configure(text="로그인됨", fg=GREEN)
        else:
            self.auth_lbl.configure(text="설치됨", fg=TEXTMUT)

    def _auth(self, action):
        import script_gen
        try:
            script_gen.auth_action(action, prefer=self._agent(), log=self._log)
        except Exception as e:                              # noqa: BLE001
            messagebox.showerror("로그인", str(e))
            return
        self._log("새 창에서 %s 를 마친 뒤 [상태 새로고침]을 누르세요."
                  % ("로그인" if action == "login" else "로그아웃"))
        self.root.after(4000, self._refresh_auth)

    def _script_has_spans(self):
        """대본이 시각을 들고 있으면(`@…~…`) 자막 없이도 만들 수 있다.

        `"@" in text` 로 보면 안 된다 — 대사에 메일 주소나 아이디가 있으면
        시각이 있다고 오판해서 자막 요구를 건너뛰고, 컷이 조용히 균등 배치된다.
        실제 앵커 형식으로 확인한다.
        """
        p = self.paths.get("script")
        if not p:
            return False
        try:
            text = Path(p).read_bytes().decode("utf-8-sig")
            return any(script_io.TIME_MARK.match(ln.strip())
                       for ln in text.split("\n"))
        except OSError:
            return False

    def _do_build(self):
        if self._busy:
            return
        # 자막은 시각의 출처다. 대본에 시각이 박혀 있으면(`@…~…`) 없어도 되고,
        # 둘 다 없으면 블록 범위에 균등 배치된다. **팝업으로 묻지 않는다** —
        # 막을 일이 아니라 알려 줄 일인데, 팝업이 뜨면 오류처럼 보인다.
        if not self.paths.get("srt") and not self._script_has_spans():
            self._log("영화 자막이 없어 컷을 블록 범위에 균등 배치합니다 "
                      "— 대사가 나오는 순간과는 어긋납니다.")
        try:
            secs = float(self.secs_var.get())
        except ValueError:
            secs = 180.0
        self._start(lambda: pipeline.run_build(
            self.paths["script"], self.paths["srt"], self.paths["movie"],
            self.name_var.get().strip() or None,
            canvas=CANVAS_MAP[self.canvas_ko.get()],
            fit="fit",     # 세로 캔버스는 레터박스가 정답 (좌우를 안 자른다)
            mute=self.mute_var.get(),   # 시각 박기는 항상 켜짐(기본값)
            offset_s=float(self.offset_var.get()),
            target_s=secs, extra=self._extra(),
            movie_title=self._title(), scope=self._scope(),
            prefer=self._agent(),
            log=self._log, progress=self._progress, stop=self._stop))

    def _do_script(self):
        """자막만으로 대본 txt까지. CapCut은 만들지 않는다."""
        if self._busy or not self._need("srt"):
            return
        try:
            secs = float(self.secs_var.get())
        except ValueError:
            secs = 180.0
        self._start(lambda: pipeline.run_script(
            self.paths["srt"], project_name=self.name_var.get().strip() or None,
            target_s=secs, extra=self._extra(),
            movie_title=self._title(), scope=self._scope(),
            prefer=self._agent(), offset_s=float(self.offset_var.get()),
            log=self._log, progress=self._progress, stop=self._stop))

    def _start(self, fn):
        self._busy = True
        self._stop.clear()
        self._frac, self._t0, self._step = 0.0, time.time(), "시작"
        self._tick()
        for b in (self.btn_build, self.btn_script):
            b.configure(state="disabled")
        self._show_tab("log")
        threading.Thread(target=self._work, args=(fn,), daemon=True).start()

    def _work(self, fn):
        try:
            res = fn()
            self._after_run(res)
        except pipeline.Stopped:
            self._log("\n⛔ 중단되었습니다.")
        except Exception as e:                              # noqa: BLE001
            self._log("\n✘ %s: %s" % (type(e).__name__, e))
        finally:
            self.root.after(0, self._done)

    def _after_run(self, res):
        if not isinstance(res, dict):
            return
        # 줄거리는 두 경로 모두에서 나올 수 있다. 있으면 탭을 채운다.
        pp = res.get("plot_path")
        if pp:
            try:
                self._set_plot(Path(pp).read_bytes().decode("utf-8-sig"))
            except OSError:
                pass
        sp = res.get("script_path")
        if sp:                                   # 대본만 만들기
            p = Path(sp)
            self.paths["script"] = str(p)
            try:
                self._set_script(p.read_bytes().decode("utf-8-sig"))
            except OSError:
                pass
            self.root.after(0, lambda: self.labels["script"].configure(
                text=p.name, fg=GREEN))
            self.root.after(0, lambda: self._show_tab("script"))
            return
        rdir = res.get("results_dir")
        if not rdir:
            return
        self.last_dir = rdir
        for p in script_io.find_scripts(rdir):
            try:
                self._set_script(p.read_bytes().decode("utf-8-sig"))
                self.paths["script"] = str(p)
                self.root.after(0, lambda: self.labels["script"].configure(
                    text=p.name, fg=GREEN))
            except OSError:
                pass
            break

    def _done(self):
        self._busy = False           # 여기서 _tick 이 멈춘다
        for b in (self.btn_build, self.btn_script):
            b.configure(state="normal")
        self._frac, self._step = 0.0, ""

        def do():
            self.bar_bg.coords(self.bar, 0, 0, 0, 7)
            self.prog_lbl.configure(text="")
            self.root.title("PlotCut — 타임라인 대본 → CapCut")
        self.root.after(0, do)

    def _do_stop(self):
        if self._busy:
            self._stop.set()
            self._log("중단 — 에이전트를 강제 종료합니다…")

    def _reset(self):
        for k in list(self.paths):
            self._clear(k)
        self.name_var.set("")
        self._set_script("")

    # ── 워커 → UI ───────────────────────────────────────────────────────
    _STEP_PAT = re.compile(r"^\[(\d)\]\s*(.+)$")

    def _log(self, msg=""):
        m = self._STEP_PAT.match(str(msg).strip())
        if m:
            self._step = m.group(2).strip()
        def do():
            self.log.configure(state="normal")
            self.log.insert("end", str(msg) + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.root.after(0, do)

    def _progress(self, frac):
        """워커 스레드에서 부른다. 0.0~1.0.

        대본 만들기(5~10분)는 경과/예상으로 차오르고, 나머지 단계는 끝날 때마다
        정해진 지점까지 뛴다. 퍼센트는 **창 제목**에도 넣는다 — 창을 내려 놔도
        작업표시줄에서 보인다(CutSync와 같은 방식).
        """
        try:
            self._frac = max(0.0, min(1.0, float(frac)))
        except (TypeError, ValueError):
            pass

    def _tick(self):
        if not self._busy:
            return
        w = max(1, self.bar_bg.winfo_width())
        el = time.time() - self._t0
        frac = self._frac or 0.0
        pct = int(frac * 100)
        self.bar_bg.coords(self.bar, 0, 0, int(w * frac), 7)
        self.root.title("PlotCut — %d%%" % pct)
        self.prog_lbl.configure(
            text="%s   %d%%   %d:%02d 경과" % (self._step, pct, el // 60, el % 60))
        self.root.after(200, self._tick)

    def _set_plot(self, text):
        """줄거리 탭 내용. 비면 왜 비었는지 알려 준다."""
        body = text.strip() or "\n".join([
            "대본을 만들면 여기에 줄거리와 결말이 들어옵니다.",
            "",
            "  · [줄거리]  누가 무엇을 하다가 어떻게 되는지",
            "  · [결말]  마지막에 어떻게 끝나는지",
            "  · [이 대본에서 고른 것]  무엇을 골랐고 무엇을 버렸는지",
            "  · [자막에 안 나오는 것]  화면에는 보이는데 대사엔 없는 것",
            "",
            "편집하기 전에 먼저 읽어 보세요.",
        ])

        def do():
            self.plot_text.configure(state="normal")
            self.plot_text.delete("1.0", "end")
            self.plot_text.insert("1.0", body)
            self.plot_text.configure(state="disabled")
        self.root.after(0, do)

    def _set_script(self, text):
        def do():
            self.script_text.delete("1.0", "end")
            self.script_text.insert("1.0", text)
        self.root.after(0, do)

    # ── 보조 ────────────────────────────────────────────────────────────
    def _script_content(self):
        return self.script_text.get("1.0", "end-1c")

    def _save_script(self):
        p = self.paths.get("script")
        if not p:
            messagebox.showwarning("대본", "먼저 대본 txt를 넣어 주세요.")
            return
        text = self._script_content()
        if not text.strip():
            return
        pipeline.write_atomic(p, text if text.endswith("\n") else text + "\n")
        self._log("저장: %s" % p)
        self._do_build()

    def _copy_narration(self):
        import script_io
        import subtitle
        if not self._need("script", "srt"):
            return
        try:
            cues, _ = subtitle.parse_file(self.paths["srt"])
            doc = script_io.read(self._script_content() or
                                 Path(self.paths["script"]).read_bytes()
                                 .decode("utf-8-sig"), cues, log=lambda *_: None)
            text = script_io.narration_text(doc)
        except Exception as e:                              # noqa: BLE001
            messagebox.showerror("복사 실패", str(e))
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        n = len(script_io.narrations(doc))
        self._log("나레이션 %d줄을 클립보드에 복사했습니다 — 타입캐스트에 붙여넣으세요." % n)


    def _on_close(self):
        if self._busy and not messagebox.askyesno(
                "종료", "작업이 진행 중입니다. 정말 닫을까요?"):
            return
        self._stop.set()
        self.root.destroy()


def main():
    root = TkinterDnD.Tk() if DND_AVAILABLE else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
