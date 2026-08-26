"""
guards.py — 갱신 전 사전 검사 · 백업 · 롤백

CapCut 프로젝트를 덮어쓰는 것은 되돌리기 어려운 작업이다. 그래서 여기서 막는다.

가장 중요한 두 가지:
  · CapCut이 열려 있으면 갱신하지 않는다. 프로젝트 폴더에 잠금 파일이 없어서
    우리 쓰기는 성공하지만, CapCut이 다음 저장에서 메모리 상태를 되쓰기 때문에
    갱신이 조용히 사라진다.
  · 대상은 draft_id로만 찾는다. 이름으로 찾으면 남의 프로젝트를 덮어쓴다.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import capcut_draft as cd
import movie_info

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

LOCK_NAME = " .plotcut.lock".strip()

# CapCut 실행 파일 이름 — 정확 일치로만 검사한다.
# 같은 폴더에 VEHelper.exe / ttdaemon.exe / push_detect.exe 같은 것들이 있어서
# 'cap' 부분일치로 검사하면 영구히 막혀버린다.

# CapCut이 스스로 만드는 파일 — 사용자가 이 프로젝트를 편집·저장했다는 신호


class GuardError(RuntimeError):
    """갱신을 중단해야 하는 상황. 메시지를 그대로 사용자에게 보여준다."""


class ProjectLock:
    """같은 프로젝트에 두 작업이 동시에 들어가지 못하게 한다."""

    def __init__(self, result_dir):
        self.path = Path(result_dir) / LOCK_NAME
        self.acquired = False

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            info = self._read()
            pid = info.get("pid")
            if pid and _pid_alive(pid):
                raise GuardError(
                    f"이 프로젝트가 이미 실행 중입니다 (pid {pid}, "
                    f"{info.get('started', '?')}).") from None
            # 죽은 프로세스가 남긴 락 — 기록된 pid가 살아있지 않을 때만 정리한다
            try:
                self.path.unlink()
            except OSError as e:
                raise GuardError(f"오래된 락 파일을 지울 수 없습니다: {self.path}") from e
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(),
                       "started": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
        self.acquired = True
        return self

    def release(self):
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.acquired = False

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def installed_capcut_version():
    """설치된 CapCut 버전 문자열. 못 찾으면 None."""
    base = Path(os.path.expandvars(r"%LOCALAPPDATA%\CapCut\Apps"))
    if not base.exists():
        return None
    vers = []
    for d in base.iterdir():
        if d.is_dir() and d.name[:1].isdigit():
            vers.append(d.name)
    if not vers:
        return None
    return sorted(vers, key=_ver_key)[-1]


def _ver_key(s):
    out = []
    for part in str(s).split("."):
        out.append(int(part) if part.isdigit() else 0)
    return out


def _read_json(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def draft_format_versions(draft_root):
    """그 PC 의 **기존** CapCut 프로젝트들이 쓰는 형식 버전. 읽기 전용.

    CapCut 이 직접 만든 프로젝트를 보면 이 PC 의 CapCut 이 지금 어떤 형식으로
    쓰는지 알 수 있다. 우리 스켈레톤과 같으면 호환은 **확인된 것**이다.
    """
    out = []
    if draft_root is None:
        # 설치된 CapCut 이 **실제로 쓰는** 폴더를 본다. 시험용 `--draft-root` 로
        # 어디에 만들든, 「이 PC 의 CapCut 이 무슨 형식을 읽는가」의 답은 거기 있다.
        try:
            import capcut_draft
            draft_root = capcut_draft.find_capcut_root()
        except Exception:                                   # noqa: BLE001
            draft_root = None
    root = Path(draft_root) if draft_root else None
    if not root or not root.exists():
        return out
    try:
        dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return out
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs[:5]:
        f = d / "draft_content.json"
        try:
            head = f.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        i = head.find('"new_version"')
        if i < 0:
            continue
        j = head.find('"', head.find(":", i) + 1)
        k = head.find('"', j + 1)
        if j > 0 and k > j:
            out.append(head[j + 1:k])
    return out


def version_note(project_version, new_version=None, draft_root=None):
    """만든 프로젝트가 이 PC 의 CapCut 과 안 맞을 것 같으면 알려 준다.

    **앱 버전이 아니라 형식 버전을 본다.** 예전엔 스켈레톤을 뜬 앱 버전(8.9.1)과
    설치된 앱 버전의 주 버전만 비교해서, CapCut 이 9.x 로 올라가자 **형식이
    그대로인데도** 매번 「주 버전이 다릅니다」를 띄웠다. 실측(8/27): 사용자의
    CapCut 9.3.0.3970 이 만든 프로젝트와 우리 스켈레톤이 둘 다
    `new_version 175.0.0` 이었다 — 형식은 같은데 겁만 준 셈이다.

    그래서 순서가 이렇다:
      1) 이 PC 의 기존 프로젝트에서 형식 버전을 읽는다. 우리 것과 같으면 **조용히**
         (호환이 확인된 것이다). 다르면 그 두 형식 버전을 대 놓고 알려 준다.
      2) 비교할 프로젝트가 하나도 없으면 예전처럼 앱 주 버전으로 넘겨짚는다.
    """
    seen = [v for v in draft_format_versions(draft_root) if v]
    if seen and new_version:
        if str(new_version) in seen:
            return None
        return ("이 PC 의 CapCut 프로젝트는 형식 %s 인데 만든 프로젝트는 %s 입니다 — "
                "CapCut에서 꼭 열어 확인하세요." % (seen[0], new_version))
    got = installed_capcut_version()
    if not got or not project_version:
        return None
    if got.split(".")[0] == str(project_version).split(".")[0]:
        return None
    return ("설치된 CapCut %s 과 프로젝트를 뜬 CapCut %s 의 주 버전이 다릅니다 — "
            "만든 프로젝트를 CapCut에서 꼭 열어 확인하세요." % (got, project_version))
