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


def version_note(project_version):
    """설치된 CapCut 과 만든 프로젝트 형식의 주 버전이 다르면 알려 준다.

    스켈레톤은 CapCut 8.9.1 에서 뜬 것이다. CapCut 이 9.x 로 올라가면 이 형식을
    그대로 읽는지 확인된 바 없다 — 조용히 깨지는 것보다 한 줄 경고가 낫다.
    """
    got = installed_capcut_version()
    if not got or not project_version:
        return None
    if got.split(".")[0] == str(project_version).split(".")[0]:
        return None
    return ("설치된 CapCut %s 과 프로젝트 형식 %s 의 주 버전이 다릅니다 — "
            "만든 프로젝트를 CapCut에서 꼭 열어 확인하세요." % (got, project_version))
