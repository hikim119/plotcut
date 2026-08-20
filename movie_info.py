"""
movie_info.py — 영화 파일에서 길이·해상도를 읽는다 (ffprobe)

ffmpeg는 pydub이 이미 요구하므로 새 의존성이 아니다. 콘솔 창이 뜨지 않게
CREATE_NO_WINDOW로 실행한다(GUI에서 창이 번쩍이는 것을 막는다).

프로브 결과에 size·mtime을 함께 담는 이유: 갱신 시 "그때 쓴 그 파일인가"를
지문으로 확인해야 한다. 파일이 교체됐는데 예전 타임코드를 그대로 쓰면
엉뚱한 장면이 컷으로 들어간다.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".mov", ".ts", ".m4v", ".wmv", ".webm", ".mpg", ".mpeg")


class ProbeError(RuntimeError):
    pass


def ffprobe_available():
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True,
                       creationflags=_NO_WINDOW, timeout=15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def probe(path, timeout=60):
    """영화 파일 1개 → dict. ffprobe가 없거나 실패하면 ProbeError."""
    p = Path(path)
    if not p.exists():
        raise ProbeError(f"파일이 없습니다: {path}")
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(p)]
    try:
        r = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW,
                           timeout=timeout)
    except OSError as e:
        raise ProbeError(
            "ffprobe를 실행할 수 없습니다. ffmpeg가 설치되어 PATH에 있는지 확인하세요.\n"
            "  winget install Gyan.FFmpeg") from e
    except subprocess.TimeoutExpired as e:
        raise ProbeError(f"ffprobe 시간 초과: {path}") from e
    if r.returncode != 0:
        raise ProbeError(f"ffprobe 실패({r.returncode}): "
                         f"{r.stderr.decode('utf-8', 'replace')[:300]}")

    try:
        info = json.loads(r.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise ProbeError(f"ffprobe 출력을 해석할 수 없습니다: {path}") from e

    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if v is None:
        raise ProbeError(f"비디오 스트림이 없습니다: {path}")

    dur = _first_float(info.get("format", {}).get("duration"), v.get("duration"))
    if not dur:
        raise ProbeError(f"영상 길이를 읽을 수 없습니다: {path}")

    st = p.stat()
    return {
        "path": str(p.resolve()),
        "size": st.st_size,
        "mtime": round(st.st_mtime, 3),
        "duration_s": round(float(dur), 3),
        "width": int(v.get("width") or 0) or 1920,
        "height": int(v.get("height") or 0) or 1080,
        "fps": _parse_fps(v.get("avg_frame_rate") or v.get("r_frame_rate")),
        "vcodec": v.get("codec_name", ""),
    }


def probe_all(paths, log=print):
    """여러 파트(분할 영화) → {index: info}. 넣은 순서를 파트 순서로 본다."""
    media = {}
    for i, path in enumerate(paths):
        info = probe(path)
        info["index"] = i
        media[i] = info
        log(f"  🎬 파트{i}: {Path(info['path']).name} · "
            f"{info['width']}×{info['height']} · {fmt_dur(info['duration_s'])}")
    return media


def audio_duration(path, timeout=60):
    """오디오 길이(초)만 필요한 경우 (pydub 로드 없이)."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-print_format", "json", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW,
                           timeout=timeout)
        d = json.loads(r.stdout.decode("utf-8", "replace"))
        return round(float(d["format"]["duration"]), 3)
    except Exception:
        return None


def fingerprint_ok(info, current):
    """갱신 시 미디어 지문 비교. 반환: (동일한가, 사유)"""
    if current is None:
        return False, "파일이 없습니다"
    if int(current["size"]) != int(info.get("size", -1)):
        return False, (f"파일 크기가 다릅니다 "
                       f"({info.get('size')} → {current['size']})")
    if abs(float(current["mtime"]) - float(info.get("mtime", -1))) > 2.0:
        return False, "수정 시각이 다릅니다"
    if abs(float(current["duration_s"]) - float(info.get("duration_s", -1))) > 0.5:
        return False, (f"길이가 다릅니다 "
                       f"({fmt_dur(info.get('duration_s', 0))} → "
                       f"{fmt_dur(current['duration_s'])})")
    return True, ""


def find_by_basename(missing_path, search_dirs):
    """경로가 바뀐 파일을 같은 이름으로 찾아본다 (자동 채택은 하지 않는다)."""
    name = Path(missing_path).name
    for d in search_dirs:
        cand = Path(d) / name
        if cand.exists():
            return str(cand.resolve())
    return None


def _first_float(*vals):
    for v in vals:
        try:
            f = float(v)
            if f > 0:
                return f
        except (TypeError, ValueError):
            continue
    return None


def _parse_fps(s):
    try:
        num, _, den = str(s).partition("/")
        den = float(den or 1)
        return round(float(num) / den, 3) if den else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def fmt_dur(sec):
    """초 → '1시간 23분 45초' / '3분 12초' / '45초'"""
    sec = int(round(float(sec or 0)))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}시간 {m}분 {s}초"
    if m:
        return f"{m}분 {s}초"
    return f"{s}초"
