"""
capcut_draft.py — CapCut(8.9.x) 드래프트 생성 / in-place 갱신

CutSync의 같은 이름 파일에서 출발했지만 세 층으로 갈랐다.

  build_timeline(...)   디스크를 만지지 않는 순수 조립
  create_project(...)   스켈레톤 복제 → 새 GUID 발급 → 커밋

CutSync와 달라진 배치 규칙 (이유는 계획서 §타이밍):
  · 컷 길이 2초 캡을 없앴다. 캡이 있으면 긴 장면 뒤에 검은 구멍이 생긴다.
  · 컷을 나레이션의 **절대 시각**에 놓는다. 길이를 누적(cursor += dur)하면
    문장 사이의 쉼이 어디에도 들어가지 않아 뒤로 갈수록 화면이 밀린다.
  · 컷이 겹치지 않으므로 비디오 트랙은 하나다(멀티트랙 스태킹 삭제).

갱신에서 절대 하지 말 것:
  · draft_id 재발급 — 프로젝트 폴더와 CapCut 홈 목록을 잇는 유일한 키다.
  · copytree로 스켈레톤 재투입 — timeline GUID가 스켈레톤 값으로 되돌아간다.
  · draft_materials type 0 통째 교체 — 사용자가 넣은 미디어와 내부 플레이스홀더가
    사라지고 draft_virtual_store의 child_id가 죽은 id를 가리킨다.
  · root_meta_info.json 수정 — 전역 인덱스다. 손상되면 모든 프로젝트가 안 보인다.
"""

import json
import os
import shutil
import time
import uuid
from pathlib import Path

US = 1_000_000  # 1초 = 1e6 µs

TEMPLATE_DIR = Path(__file__).resolve().parent / "template" / "skeleton"

# 스켈레톤 지문 — 갱신 대상이 CutSync 산출물인지 판별할 때 쓴다
SKELETON_TIMELINE_ID = "B65C696E-CC40-4e9e-A388-E73A6B01F937"
SKELETON_PROJECT_ID = "D289DD17-95F9-4906-A2DE-33A41F6539B3"
SKELETON_DRAFT_ID = "00000000-0000-0000-0000-000000000000"
SKELETON_TM_CREATE = 1783056185684789

# 우리가 관리하는 머티리얼 버킷. 나머지 버킷은 orphan GC 대상이다.
OUR_BUCKETS = ("videos", "audios", "texts", "speeds", "canvases",
               "sound_channel_mappings", "vocal_separations")

# CapCut 인터내셔널 드래프트 폴더 후보 (%LOCALAPPDATA%는 기기별로 자동 치환됨)
_DRAFT_CANDIDATES = [
    r"%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft",
    r"%APPDATA%\CapCut\User Data\Projects\com.lveditor.draft",
]

CANVAS = {
    "vertical":   (1080, 1920),  # 9:16 세로 쇼츠 (기본)
    "square":     (1080, 1080),
    "horizontal": (1920, 1080),
}


class DraftError(RuntimeError):
    pass


def find_capcut_root():
    """설치된 CapCut의 드래프트 폴더를 찾는다. 없으면 None."""
    for c in _DRAFT_CANDIDATES:
        p = Path(os.path.expandvars(c))
        if p.exists():
            return p
    return None


CAPCUT_DRAFT_ROOT = Path(os.path.expandvars(_DRAFT_CANDIDATES[0]))


def _uid():
    return uuid.uuid4().hex


def _us(seconds):
    return int(round(float(seconds) * US))


# ── 머티리얼 빌더 (CutSync 그대로) ──────────────────────────────────────────

def _video_material(path, duration_us, width, height):
    mid = _uid()
    return mid, {
        "audio_fade": None, "category_id": "", "category_name": "local",
        "check_flag": 63487,
        "crop": {"upper_left_x": 0.0, "upper_left_y": 0.0,
                 "upper_right_x": 1.0, "upper_right_y": 0.0,
                 "lower_left_x": 0.0, "lower_left_y": 1.0,
                 "lower_right_x": 1.0, "lower_right_y": 1.0},
        "crop_ratio": "free", "crop_scale": 1.0,
        "duration": duration_us, "height": height,
        "id": mid, "local_material_id": "", "material_id": mid,
        "material_name": os.path.basename(path), "media_path": "",
        "path": str(path).replace("\\", "/"), "type": "video", "width": width,
    }


def _audio_material(path, duration_us):
    mid = _uid()
    return mid, {
        "app_id": 0, "category_id": "", "category_name": "local", "check_flag": 3,
        "copyright_limit_type": "none", "duration": duration_us,
        "effect_id": "", "formula_id": "",
        "id": mid, "local_material_id": mid, "music_id": mid,
        "name": os.path.basename(path), "path": str(path).replace("\\", "/"),
        "source_platform": 0, "type": "extract_music", "wave_points": [],
    }


def _speed_material():
    sid = _uid()
    return sid, {"curve_speed": None, "id": sid, "mode": 0, "speed": 1.0, "type": "speed"}


def _canvas_material():
    cid = _uid()
    return cid, {"album_image": "", "blur": 0.0, "color": "", "id": cid,
                 "image": "", "image_id": "", "image_name": "", "source_platform": 0,
                 "team_id": "", "type": "canvas_color"}


def _scm_material():
    """sound_channel_mapping — 비디오/오디오 세그먼트가 참조."""
    sid = _uid()
    return sid, {"audio_channel_mapping": 0, "id": sid, "is_config_open": False, "type": ""}


def _vocal_sep_material():
    vid = _uid()
    return vid, {"choice": 0, "id": vid, "production_path": "",
                 "removed_sounds": [], "time_range": None, "type": "vocal_separation"}


def _text_material(text, color=(1.0, 1.0, 1.0), size=15.0,
                   border_color=(0.0, 0.0, 0.0), border_width=40.0):
    mid = _uid()
    content = {
        "styles": [{
            "fill": {"alpha": 1.0, "content": {"render_type": "solid",
                     "solid": {"alpha": 1.0, "color": list(color)}}},
            "range": [0, len(text)],
            "size": size, "bold": True, "italic": False, "underline": False,
            "strokes": [{"content": {"solid": {"alpha": 1.0, "color": list(border_color)}},
                         "width": border_width / 100.0 * 0.2}],
        }],
        "text": text,
    }
    return mid, {
        "id": mid,
        "content": json.dumps(content, ensure_ascii=False),
        "typesetting": 0, "alignment": 1,
        "letter_spacing": 0.0, "line_spacing": 0.02,
        "line_feed": 1, "line_max_width": 0.82, "force_apply_line_max_width": False,
        "check_flag": 7 | 8,  # 7 기본 + 8 테두리
        "type": "subtitle", "global_alpha": 1.0,
    }


# ── 세그먼트 빌더 (CutSync 그대로) ──────────────────────────────────────────

def _base_segment(material_id, target_start, target_dur, source_start, source_dur, extra_refs):
    return {
        "enable_adjust": True, "enable_color_correct_adjust": False,
        "enable_color_curves": True, "enable_color_match_adjust": False,
        "enable_color_wheels": True, "enable_lut": True,
        "enable_smart_color_adjust": False,
        "last_nonzero_volume": 1.0, "reverse": False,
        "track_attribute": 0, "track_render_index": 0, "visible": True,
        "id": _uid(), "material_id": material_id,
        "target_timerange": {"start": target_start, "duration": target_dur},
        "source_timerange": {"start": source_start, "duration": source_dur},
        "speed": 1.0, "volume": 1.0,
        "extra_material_refs": extra_refs,
        "common_keyframes": [], "keyframe_refs": [],
    }


def _clip_settings(transform_y=0.0):
    return {"alpha": 1.0, "flip": {"horizontal": False, "vertical": False},
            "rotation": 0.0, "scale": {"x": 1.0, "y": 1.0},
            "transform": {"x": 0.0, "y": transform_y}}


def _video_segment(material_id, target_start, target_dur, source_start, source_dur, extra_refs):
    seg = _base_segment(material_id, target_start, target_dur, source_start, source_dur, extra_refs)
    seg["clip"] = _clip_settings()
    seg["uniform_scale"] = {"on": True, "value": 1.0}
    seg["hdr_settings"] = {"intensity": 1.0, "mode": 1, "nits": 1000}
    return seg


def _audio_segment(material_id, target_start, target_dur, source_start, source_dur, extra_refs):
    seg = _base_segment(material_id, target_start, target_dur, source_start, source_dur, extra_refs)
    seg["clip"] = None
    seg["hdr_settings"] = None
    return seg


def _text_segment(material_id, target_start, target_dur, extra_refs, transform_y=-0.75):
    seg = _base_segment(material_id, target_start, target_dur, 0, target_dur, extra_refs)
    seg["source_timerange"] = None
    seg["clip"] = _clip_settings(transform_y=transform_y)
    seg["uniform_scale"] = {"on": True, "value": 1.0}
    return seg


def _track(track_type, segments, render_index, flag=0, attribute=0):
    exported = []
    for s in segments:
        s["track_render_index"] = render_index
        exported.append(s)
    return {"attribute": attribute, "flag": flag, "id": _uid(),
            "is_default_name": True, "name": "", "segments": exported,
            "type": track_type}


# ── 1층: 순수 조립 ──────────────────────────────────────────────────────────

# ── 자막 스타일 ─────────────────────────────────────────────────────────────
# 자막은 **한 번에 하나만** 뜨고(`layout._stack` 이 다음 자막 직전에 끊는다)
# 자리는 **안 움직인다** — 전부 y = SUB_Y0. 색으로 대사(흰색)와
# 나레이션(노랑)을 구분한다.
#
# 한때 블록 안 순서(`row`)만큼 y 를 한 칸씩 올렸다. 그건 앞 자막을 **남겨서
# 쌓을 때**의 좌표인데, 앞 자막을 지우도록 바꾼 뒤에도 좌표만 남아 있었다.
# 결과는 화면에 하나뿐인 자막이 아래에서 위로 기어 올라갔다가 블록이 바뀌면
# 뚝 떨어지는 것 — 실측 y −0.50 → −0.14 (여덟 칸). 하나만 보이면 움직일
# 이유가 없다.
#
# **`row` 는 그대로 쓴다 — 다만 y 가 아니라 트랙을 고르는 데만.** 줄 자리마다
# 트랙이 하나씩 있어야 CapCut 에서 "몇 번째 단"을 통째로 고를 수 있다.
SUB_Y0 = -0.50         # 자막 자리 (화면 아래). 모든 자막이 여기 온다
SUB_STYLES = {
    "dialogue":  {"color": (1.0, 1.0, 1.0),     "size": 12.0, "transform_y": SUB_Y0,
                  "border_color": (0.0, 0.0, 0.0), "border_width": 40.0},
    "narration": {"color": (1.0, 0.910, 0.420), "size": 12.0, "transform_y": SUB_Y0,
                  "border_color": (0.0, 0.0, 0.0), "border_width": 40.0},
    "title":     {"color": (1.0, 1.0, 1.0),     "size": 14.0, "transform_y": 0.45,
                  "border_color": (0.0, 0.0, 0.0), "border_width": 40.0},
}
TEXT_RENDER_BASE = 14000       # 실제 CapCut 자막 세그먼트의 render_index 대역


def canvas_scale(src_w, src_h, canvas_w, canvas_h, fit="fit"):
    """세그먼트 clip.scale 값.

    [확인] CapCut 8.9.1에서 scale=1.0 은 캔버스 '맞춤(fit)'이다 —
    1920×1080을 1080×1920 캔버스에 넣으면 위아래 검은 띠가 생기고 좌우는 잘리지 않는다
    (2026-08-18 실제 프로젝트에서 눈으로 확인). fill 은 그 비율만큼 키운다 —
    같은 조합에서 3.16배이고 좌우 69%가 잘려 나간다.
    """
    if not src_w or not src_h:
        return 1.0
    a = min(canvas_w / float(src_w), canvas_h / float(src_h))
    b = max(canvas_w / float(src_w), canvas_h / float(src_h))
    return 1.0 if fit == "fit" else (b / a if a else 1.0)


def _bmp_safe(text):
    """BMP 밖 문자(이모지)를 뺀다 — CapCut은 스타일 범위를 UTF-16 기준으로 읽는데
    우리는 코드포인트로 세므로 그대로 두면 색·테두리 범위가 밀린다."""
    return "".join(ch for ch in text if ord(ch) < 0x10000)


def build_timeline(plan, movie, canvas="vertical", fit="fit",
                   audio_path=None, audio_duration_s=None,
                   sub_styles=None, log=print):
    """layout.build() 결과를 CapCut 타임라인으로 조립한다. 디스크를 만지지 않는다.

    plan  : {"segments","subs","audio","total_s", ...}
    movie : {"path","duration_s","width","height"}
    반환  : {"materials","tracks","duration_us","canvas","media_pool",
             "stats","track_counts"}
    """
    segs_in = plan.get("segments") or []
    if not segs_in:
        raise DraftError("타임라인 구간이 없습니다 — 빈 타임라인은 만들지 않습니다.")

    styles = {k: dict(v) for k, v in SUB_STYLES.items()}
    for k, v in (sub_styles or {}).items():
        styles.setdefault(k, {}).update(v)

    canvas_w, canvas_h = CANVAS.get(canvas, CANVAS["vertical"])
    materials = {k: [] for k in OUR_BUCKETS}

    # 영화 없이도 만든다 — 타임라인 대본은 영화 파일에 의존하지 않는다.
    # 이때는 영상 트랙만 비고, 나중에 --movie 를 주고 다시 만들면 채워진다.
    src_w = int((movie or {}).get("width") or 1920)
    src_h = int((movie or {}).get("height") or 1080)
    movie_dur_us = _us(movie["duration_s"]) if movie else 0
    vid_id = None
    if movie:
        vid_id, vid_mat = _video_material(os.path.abspath(movie["path"]),
                                          movie_dur_us, src_w, src_h)
        materials["videos"].append(vid_mat)
    scale = canvas_scale(src_w, src_h, canvas_w, canvas_h, fit)

    # ── 영상 ────────────────────────────────────────────────────────────
    video_segs = []
    stats = {"cuts": 0, "subs": 0, "muted": 0, "clamped": 0}
    total_us = 0
    for s in (segs_in if movie else []):
        start = _us(s["t_start"])
        dur = _us(s["t_dur"])
        if dur < 1:
            raise DraftError("길이 %.4f초짜리 구간이 있습니다 — layout 버그입니다."
                             % s["t_dur"])
        src = _us(s["src0"])
        if src < 0 or src + dur > movie_dur_us:
            src = max(0, min(src, movie_dur_us - dur))
            stats["clamped"] += 1
        spd_id, spd = _speed_material()
        materials["speeds"].append(spd)
        cv_id, cv = _canvas_material()
        materials["canvases"].append(cv)
        scm_id, scm = _scm_material()
        materials["sound_channel_mappings"].append(scm)
        seg = _video_segment(vid_id, start, dur, src, dur, [spd_id, cv_id, scm_id])
        seg["volume"] = float(s.get("volume", 1.0))
        seg["last_nonzero_volume"] = 1.0       # 음소거를 되돌릴 때 CapCut이 쓰는 값
        seg["clip"]["scale"] = {"x": scale, "y": scale}
        seg["render_index"] = 0
        video_segs.append(seg)
        stats["cuts"] += 1
        if seg["volume"] < 1.0:
            stats["muted"] += 1
        total_us = max(total_us, start + dur)

    # ── 자막 — 대사 / 나레이션 / 제목 세 트랙 ───────────────────────────
    # 제목을 나레이션 트랙에 얹으면 대본이 나레이션으로 시작할 때 둘 다 t=0 이라
    # 겹친다. 화면 위치가 다르니(제목 y=+0.45) 트랙을 나누는 게 맞다.
    lanes = {"title": []}                 # 나머지는 줄 자리(row)마다 하나씩
    for sub in plan.get("subs") or []:
        kind = sub.get("kind", "dialogue")
        style = styles.get(kind, styles["dialogue"])
        text = _bmp_safe("\n".join(sub["lines"]))
        if not text.strip():
            continue
        row = 0 if kind == "title" else int(sub.get("row", 0))
        # y 는 row 를 안 본다 — 자막은 한 번에 하나뿐이라 자리를 옮길 이유가 없다.
        y = float(style["transform_y"]) if kind == "title" else SUB_Y0
        t_mid, t_mat = _text_material(
            text, color=tuple(style["color"]), size=float(style["size"]),
            border_color=tuple(style["border_color"]),
            border_width=float(style["border_width"]))
        materials["texts"].append(t_mat)
        seg = _text_segment(t_mid, _us(sub["t_start"]), max(1, _us(sub["t_dur"])),
                            [], y)
        # **줄 자리마다 트랙을 따로 둔다.** 시간이 안 겹치니 한 트랙에 몰아넣어도
        # 되지만, 그러면 CapCut 에서 "몇 번째 단"을 통째로 고를 수가 없다.
        # 블록 안 순서 = 단 번호 = 트랙 번호이고, 블록이 바뀌면 다시 1단부터.
        lanes.setdefault("title" if kind == "title" else "row%d" % row,
                         []).append(seg)
        stats["subs"] += 1
        total_us = max(total_us, seg["target_timerange"]["start"]
                       + seg["target_timerange"]["duration"])

    # ── 오디오 — mp3 하나를 여러 구간이 서로 다른 source_timerange로 참조 ──
    audio_segs = []
    if audio_path and plan.get("audio"):
        a_id, a_mat = _audio_material(os.path.abspath(audio_path),
                                      _us(audio_duration_s or 0))
        materials["audios"].append(a_mat)
        for a in plan["audio"]:
            spd_id, spd = _speed_material()
            materials["speeds"].append(spd)
            scm_id, scm = _scm_material()
            materials["sound_channel_mappings"].append(scm)
            vs_id, vs = _vocal_sep_material()
            materials["vocal_separations"].append(vs)
            dur = max(1, _us(a["t_dur"]))
            seg = _audio_segment(a_id, _us(a["t_start"]), dur,
                                 _us(a["src_start"]), dur,
                                 [spd_id, scm_id, vs_id])
            seg["render_index"] = 0
            audio_segs.append(seg)
            total_us = max(total_us, seg["target_timerange"]["start"] + dur)

    _assert_no_overlap(video_segs, "영상")
    for name, lane in lanes.items():
        _assert_no_overlap(lane, "자막(%s)" % name)
    _assert_no_overlap(audio_segs, "오디오")

    if video_segs:
        covered = sum(s["target_timerange"]["duration"] for s in video_segs)
        span = max(s["target_timerange"]["start"] + s["target_timerange"]["duration"]
                   for s in video_segs)
        if abs(covered - span) > 2000:         # 2ms 허용(반올림)
            raise DraftError("영상에 빈틈이 있습니다: 덮은 길이 %dµs ≠ 전체 %dµs"
                             % (covered, span))
    elif not any(lanes.values()):
        raise DraftError("영상도 자막도 없습니다 — 빈 프로젝트는 만들지 않습니다.")

    # ── 트랙 ────────────────────────────────────────────────────────────
    # 실제 CapCut은 render_index 를 **세그먼트마다**(자막은 14000+) 매기고
    # track_render_index 를 **트랙마다** 0,1,2… 로 준다. 예전 코드는 상수 15000을
    # track_render_index 에 넣고 render_index 는 아예 쓰지 않았다.
    tracks = []
    tri = 0
    if video_segs:
        tracks.append(_track("video", video_segs, tri))
        tri += 1
    base = TEXT_RENDER_BASE
    # 아래 단부터 위로, 마지막이 제목. 트랙 순서가 화면 위아래와 같아야
    # CapCut 에서 "몇 번째 단"을 찾기 쉽다.
    rows = sorted((n for n in lanes if n.startswith("row")),
                  key=lambda n: int(n[3:]))
    for name in rows + ["title"]:
        lane = lanes.get(name) or []
        if not lane:
            continue
        lane.sort(key=lambda x: x["target_timerange"]["start"])
        for k, s in enumerate(lane):
            s["render_index"] = base + k
        base += 1000
        tracks.append(_track("text", lane, tri))
        tri += 1
    if audio_segs:
        tracks.append(_track("audio", audio_segs, tri))
        tri += 1

    media_pool = ([(movie["path"], movie_dur_us, src_w, src_h, "video")]
                  if movie else [])
    if audio_segs:
        media_pool.append((audio_path, materials["audios"][0]["duration"],
                           0, 0, "music"))

    track_counts = {"video": sum(1 for x in tracks if x["type"] == "video"),
                    "text": sum(1 for t in tracks if t["type"] == "text"),
                    "audio": sum(1 for t in tracks if t["type"] == "audio")}

    log("  ✔ 컷 %d개 · 자막 %d개 · 음소거 %d개 · 트랙 영상%d/자막%d/오디오%d"
        % (stats["cuts"], stats["subs"], stats["muted"], track_counts["video"],
           track_counts["text"], track_counts["audio"]))
    log("  ✔ 캔버스 %d×%d (%s, scale %.3f) · 총 길이 %.1f초"
        % (canvas_w, canvas_h, fit, scale, total_us / US))
    if not movie:
        log("  ! 영화 파일이 없어 영상 트랙은 비어 있습니다 — 자막·타임라인만 들어갑니다")
    if stats["clamped"]:
        log("  ⚠ 영화 끝에 걸려 시작점을 당긴 컷 %d개" % stats["clamped"])

    return {"materials": materials, "tracks": tracks, "duration_us": total_us,
            "canvas": (canvas_w, canvas_h), "media_pool": media_pool,
            "stats": stats, "track_counts": track_counts}


def _assert_no_overlap(segs, label):
    last = -1
    for s in segs:
        st = s["target_timerange"]["start"]
        if st < last:
            raise DraftError(f"{label} 트랙 세그먼트가 겹칩니다 ({st} < {last})")
        last = st + s["target_timerange"]["duration"]


# ── 공용: content/meta 패치 ─────────────────────────────────────────────────

def _referenced_ids(tracks):
    ids = set()
    for t in tracks:
        for s in t.get("segments", []):
            if s.get("material_id"):
                ids.add(s["material_id"])
            ids.update(s.get("extra_material_refs") or [])
            ids.update(s.get("keyframe_refs") or [])
    return ids


def _gc_orphans(content, tracks):
    """우리가 관리하지 않는 머티리얼 버킷과 keyframes에서 고아 항목을 제거한다.

    남겨두면 존재하지 않는 세그먼트를 가리키는 참조가 되어, CapCut이 스스로는
    절대 만들지 않는 상태의 JSON이 된다.
    """
    ids = _referenced_ids(tracks)
    removed = 0
    mats = content.get("materials") or {}
    for key, val in list(mats.items()):
        if key in OUR_BUCKETS or not isinstance(val, list):
            continue
        keep = [it for it in val
                if not isinstance(it, dict) or it.get("id") in ids or "id" not in it]
        removed += len(val) - len(keep)
        mats[key] = keep
    kf = content.get("keyframes")
    if isinstance(kf, dict):
        for key, val in list(kf.items()):
            if isinstance(val, list):
                keep = [it for it in val
                        if not isinstance(it, dict) or it.get("id") in ids]
                removed += len(val) - len(keep)
                kf[key] = keep
    return removed


def _patch_content(content, timeline, timeline_id, keep_text_track_flag=None):
    """draft_content.json 패치. 버전 필드는 건드리지 않는다."""
    content["id"] = timeline_id
    content["duration"] = timeline["duration_us"]
    w, h = timeline["canvas"]
    content["canvas_config"] = {"width": w, "height": h,
                                "ratio": "original", "background": None}
    for k, v in timeline["materials"].items():
        content.setdefault("materials", {})[k] = v

    tracks = [dict(t) for t in timeline["tracks"]]
    if keep_text_track_flag is not None:
        for t in tracks:
            if t["type"] == "text":
                t["flag"] = keep_text_track_flag.get("flag", t.get("flag", 0))
                t["attribute"] = keep_text_track_flag.get("attribute", t.get("attribute", 0))
    content["tracks"] = tracks

    gc = _gc_orphans(content, tracks)

    # 새 길이를 넘는 커버/마크는 정리
    if content.get("time_marks"):
        content["time_marks"] = []
    for key in ("cover", "static_cover_image_path"):
        if key in content and content[key]:
            content[key] = None if key == "cover" else ""

    # 기기 고유정보 제거 — 이식성·프라이버시 + "사용자가 CapCut에서 저장했는지"
    # 판별 신호로도 쓴다(우리는 항상 빈 문자열로 둔다).
    for pf in ("platform", "last_modified_platform"):
        if isinstance(content.get(pf), dict):
            content[pf]["device_id"] = ""
            content[pf]["mac_address"] = ""
            content[pf]["hard_disk_id"] = ""
    return gc


def _mat_entry(path, dur_us, w, h, metetype, keep=None):
    now = int(time.time())
    base = {
        "ai_group_type": "", "create_time": now, "duration": dur_us,
        "enter_from": 0, "extra_info": os.path.basename(path),
        "file_Path": os.path.abspath(path).replace("\\", "/"),
        "height": h, "id": str(uuid.uuid4()), "import_time": now,
        "import_time_ms": now * 1000, "item_source": 1, "md5": "",
        "metetype": metetype,
        "roughcut_time_range": {"duration": dur_us, "start": 0},
        "sub_time_range": {"duration": -1, "start": -1},
        "type": 0, "width": w,
    }
    if keep:      # 같은 파일이 이미 있으면 id와 등록 시각을 물려받는다
        for k in ("id", "create_time", "import_time", "import_time_ms", "md5"):
            if k in keep:
                base[k] = keep[k]
    return base


def _norm(p):
    return os.path.normcase(os.path.normpath(str(p)))


def _merge_media_pool(old_pool, new_items, our_paths=()):
    """type 0 미디어풀 병합.

    · file_Path가 빈 항목(CapCut 내부 플레이스홀더)은 무조건 보존
    · 같은 경로가 다시 쓰이면 기존 id 재사용 (draft_virtual_store의 child_id가
      죽은 id를 가리키는 것을 막는다)
    · 우리가 이전에 넣은 경로 중 이번에 안 쓰는 것만 제거
    """
    old_pool = list(old_pool or [])
    by_path = {}
    for it in old_pool:
        fp = it.get("file_Path") or ""
        if fp:
            by_path[_norm(fp)] = it

    new_norm = {_norm(p) for (p, *_rest) in new_items}
    ours = {_norm(p) for p in our_paths}

    merged = []
    for it in old_pool:
        fp = it.get("file_Path") or ""
        if not fp:
            merged.append(it)                       # 내부 플레이스홀더
            continue
        n = _norm(fp)
        if n in new_norm:
            continue                                # 아래에서 갱신해 다시 넣는다
        if n in ours:
            continue                                # 우리가 넣었고 이제 안 쓰는 것
        merged.append(it)                           # 사용자가 직접 넣은 미디어

    for (path, dur_us, w, h, metetype) in new_items:
        merged.append(_mat_entry(path, dur_us, w, h, metetype,
                                 keep=by_path.get(_norm(path))))
    return merged


def _patch_meta(meta, proj_dir, name, timeline, is_update=False,
                our_paths=()):
    """draft_meta_info.json 패치.

    is_update=True면 draft_id / draft_name / tm_draft_create / draft_cover /
    draft_timeline_materials_size_ 를 보존한다.
    """
    fold = str(proj_dir).replace("\\", "/")
    root = str(proj_dir.parent).replace("\\", "/")
    if _norm(meta.get("draft_fold_path") or "") != _norm(fold):
        meta["draft_fold_path"] = fold
    if _norm(meta.get("draft_root_path") or "") != _norm(root):
        meta["draft_root_path"] = root

    if not is_update:
        meta["draft_id"] = str(uuid.uuid4()).upper()
        meta["draft_name"] = name
        meta["tm_draft_create"] = int(time.time() * US)
    else:
        if not meta.get("draft_id") or meta["draft_id"] == SKELETON_DRAFT_ID:
            raise DraftError("갱신 대상의 draft_id가 비어 있습니다 — 갱신을 중단합니다.")

    meta["tm_draft_modified"] = int(time.time() * US)
    meta["tm_duration"] = timeline["duration_us"]

    pool_bucket = None
    for bucket in meta.get("draft_materials", []):
        if bucket.get("type") == 0:
            pool_bucket = bucket
            break
    if pool_bucket is None:
        pool_bucket = {"type": 0, "value": []}
        meta.setdefault("draft_materials", []).append(pool_bucket)
    pool_bucket["value"] = _merge_media_pool(
        pool_bucket.get("value") if is_update else [],
        timeline["media_pool"], our_paths)
    return meta


def _reid_timeline(proj_dir):
    """스켈레톤을 복사한 직후, 이 프로젝트만의 타임라인 GUID를 발급한다.

    [확인] 안 하면 모든 PlotCut 프로젝트가 스켈레톤 GUID를 공유한다.
    옛 GUID는 **폴더명 1곳 + 4파일 5곳**에 박혀 있다:
      Timelines/<GUID>/ 폴더명 · Timelines/project.json(main_timeline_id, timelines[0].id)
      · timeline_layout.json(dockItems[0].timelineIds[0]) · draft_content.json(id)
      · Timelines/<GUID>/draft_content.json(id)
    resolve_timeline_id() 는 이 중 셋을 교차검증하므로 **읽기 전에** 전부 바꿔야 한다.
    """
    proj_dir = Path(proj_dir)
    new_tl = str(uuid.uuid4()).upper()
    new_pj = str(uuid.uuid4()).upper()

    old_dir = proj_dir / "Timelines" / SKELETON_TIMELINE_ID
    if old_dir.is_dir():
        old_dir.rename(proj_dir / "Timelines" / new_tl)

    def _sub(rel, pairs):
        p = proj_dir / rel
        if not p.exists():
            return
        t = p.read_text(encoding="utf-8")
        for a, b in pairs:
            t = t.replace(a, b)
        _write_atomic(p, t)

    tl_only = [(SKELETON_TIMELINE_ID, new_tl)]
    _sub("draft_content.json", tl_only)
    _sub(Path("Timelines") / new_tl / "draft_content.json", tl_only)
    _sub("timeline_layout.json", tl_only)
    _sub(Path("Timelines") / "project.json",
         tl_only + [(SKELETON_PROJECT_ID, new_pj)])

    left = [p.name for p in proj_dir.rglob("*.json") if p.is_file()
            and SKELETON_TIMELINE_ID in p.read_text(encoding="utf-8", errors="ignore")]
    if left:
        raise DraftError("스켈레톤 타임라인 GUID가 남았습니다: %s" % ", ".join(left))
    return new_tl


def _sync_virtual_store(proj_dir, pool):
    """draft_virtual_store.json 의 type=1 child_id 를 미디어풀과 맞춘다.

    [확인] child_id 는 타임라인 GUID가 아니라 **draft_meta_info 의 미디어풀 항목 id**다
    (실제 프로젝트에서 1:1 대응). 예전 코드는 이 파일을 한 번도 열지 않아
    스켈레톤의 죽은 child_id 3개가 그대로 남았다.
    """
    p = Path(proj_dir) / "draft_virtual_store.json"
    if not p.exists():
        return
    try:
        vs = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for bucket in vs.get("draft_virtual_store", []):
        if bucket.get("type") == 1:
            bucket["value"] = [{"child_id": e.get("id", ""), "parent_id": ""}
                               for e in pool if e.get("id")]
            break
    _write_atomic(p, json.dumps(vs, ensure_ascii=False))


def _media_pool_of(meta):
    for bucket in meta.get("draft_materials", []):
        if bucket.get("type") == 0:
            return bucket.get("value") or []
    return []


# ── 2층: 신규 생성 ──────────────────────────────────────────────────────────

def create_project(project_name, timeline, draft_root=None, log=print):
    """스켈레톤을 복제해 새 CapCut 프로젝트를 만든다.

    반환: {"fold_path","folder_name","draft_id","timeline_id","project_json_id",
           "app_version","content_version","new_version","content_sha256",
           "content_mtime","our_media_paths"}
    """
    root = Path(draft_root) if draft_root else find_capcut_root()
    if root is None:
        raise DraftError(
            "CapCut 드래프트 폴더를 찾을 수 없습니다.\n"
            "CapCut(인터내셔널 버전)이 설치되어 있는지 확인하세요.\n"
            f"예상 경로: {CAPCUT_DRAFT_ROOT}")

    folder = project_name
    n = 2
    while (root / folder).exists():
        folder = f"{project_name}_{n}"
        n += 1
    if folder != project_name:
        log(f"  (같은 이름이 있어 '{folder}' 폴더로 만듭니다)")

    proj_dir = root / folder
    try:
        shutil.copytree(TEMPLATE_DIR, proj_dir)
    except (shutil.Error, OSError) as e:
        # 드래프트 안에 `Timelines/<GUID>/common_attachment/…json` 이 있어 폴더
        # 이름이 길면 **경로 전체가 MAX_PATH(260자)** 를 넘는다. `copytree` 는
        # 그걸 `shutil.Error`(안에 WinError 206)로 던지는데 사용자 오류 목록에
        # 없어서 트레이스백이 그대로 나갔다(실측: --name 300자). 반쯤 복사된
        # 폴더는 치우고 — CapCut 이 깨진 프로젝트로 읽는다 — 무엇을 줄이라고 말한다.
        shutil.rmtree(proj_dir, ignore_errors=True)
        raise DraftError(
            "CapCut 프로젝트 폴더를 만들 수 없습니다: %s\n  %s\n"
            "  프로젝트 이름(--name)이 너무 길면 경로가 윈도우 상한(260자)을 "
            "넘습니다 — 이름을 줄이세요." % (proj_dir, str(e)[:160])) from e
    # 반드시 resolve_timeline_id 앞에서 — 저 함수는 세 곳을 교차검증만 한다
    _reid_timeline(proj_dir)
    timeline_id = resolve_timeline_id(proj_dir)

    content_path = proj_dir / "draft_content.json"
    content = json.loads(content_path.read_text(encoding="utf-8"))
    gc = _patch_content(content, timeline, timeline_id)
    body = json.dumps(content, ensure_ascii=False)

    meta_path = proj_dir / "draft_meta_info.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    _patch_meta(meta, proj_dir, folder, timeline, is_update=False)
    meta_body = json.dumps(meta, ensure_ascii=False)

    _commit(proj_dir, timeline_id, body, meta_body, log=log)
    _sync_virtual_store(proj_dir, _media_pool_of(meta))
    if gc:
        log(f"  (참조 없는 머티리얼 {gc}개 정리)")

    ident = _identity(proj_dir, folder, content, meta, timeline)
    log(f"  폴더: {proj_dir}")
    return ident


# ── 3층: in-place 갱신 ──────────────────────────────────────────────────────

def _commit(proj_dir, timeline_id, content_body, meta_body, log=print):
    """content(2곳) + meta를 원자적으로 쓴다.

    실패해도 되돌릴 것이 없다 — 항상 **새 폴더**에 쓰므로 반쯤 써진 폴더가
    남을 뿐이고, 기존 프로젝트를 망가뜨릴 일이 없다.
    """
    targets = [
        (proj_dir / "draft_content.json", content_body),
        (proj_dir / "Timelines" / timeline_id / "draft_content.json", content_body),
        (proj_dir / "draft_meta_info.json", meta_body),
    ]
    try:
        for path, body in targets:
            _write_atomic(path, body)
    except OSError as e:
        if isinstance(e, PermissionError):
            raise DraftError(
                "파일을 쓸 수 없습니다 — CapCut이 이 프로젝트를 열고 있을 수 있습니다.\n"
                "CapCut을 완전히 종료(트레이 포함)한 뒤 다시 시도하세요.") from e
        raise DraftError(f"쓰기 실패: {e}") from e

    # 커밋 후 자기검증
    a = (proj_dir / "draft_content.json").read_text(encoding="utf-8")
    b = (proj_dir / "Timelines" / timeline_id / "draft_content.json").read_text(encoding="utf-8")
    if a != b:
        raise DraftError("두 draft_content.json이 다릅니다 — 커밋이 깨졌습니다.")
    try:
        c = json.loads(a)
        m = json.loads((proj_dir / "draft_meta_info.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise DraftError(f"커밋한 JSON을 다시 읽을 수 없습니다: {e}") from e
    if c.get("duration") != m.get("tm_duration"):
        raise DraftError(f"길이 불일치: content.duration={c.get('duration')} "
                         f"≠ meta.tm_duration={m.get('tm_duration')}")


def _write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".plotcut.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def resolve_timeline_id(proj_dir):
    """timeline_id를 3중 교차검증으로 확정한다.

    iterdir의 첫 폴더를 쓰면 순서 보장이 없어 다중 타임라인·부가 폴더에서 틀린다.
    """
    proj_dir = Path(proj_dir)
    ids = {}
    pj = proj_dir / "Timelines" / "project.json"
    if pj.exists():
        try:
            ids["project.json"] = json.loads(pj.read_text(encoding="utf-8")).get("main_timeline_id")
        except json.JSONDecodeError:
            pass
    dc = proj_dir / "draft_content.json"
    if dc.exists():
        try:
            ids["draft_content"] = json.loads(dc.read_text(encoding="utf-8")).get("id")
        except json.JSONDecodeError:
            pass
    tl = proj_dir / "Timelines"
    folders = [d.name for d in tl.iterdir() if d.is_dir()] if tl.exists() else []

    candidates = [v for v in ids.values() if v]
    if not candidates:
        if len(folders) == 1:
            return folders[0]
        raise DraftError(f"timeline_id를 확정할 수 없습니다 (폴더 {folders})")
    if len(set(candidates)) > 1:
        raise DraftError(f"timeline_id가 파일마다 다릅니다: {ids} — 갱신을 중단합니다.")
    tid = candidates[0]
    if folders and tid not in folders:
        raise DraftError(f"timeline_id({tid}) 폴더가 없습니다. 실제 폴더: {folders}")
    return tid


def _identity(proj_dir, folder_name, content, meta, timeline):
    content_path = proj_dir / "draft_content.json"
    st = content_path.stat()
    import hashlib
    h = hashlib.sha256(content_path.read_bytes()).hexdigest()
    return {
        "fold_path": str(proj_dir).replace("\\", "/"),
        "folder_name": folder_name,
        "draft_id": meta.get("draft_id"),
        "timeline_id": content.get("id"),
        "project_json_id": _project_json_id(proj_dir),
        "app_version": (content.get("platform") or {}).get("app_version"),
        "content_version": content.get("version"),
        "new_version": content.get("new_version"),
        "content_sha256": h,
        "content_mtime": round(st.st_mtime, 3),
        "our_media_paths": [str(Path(p).resolve()).replace("\\", "/")
                            for (p, *_r) in timeline["media_pool"]],
        "created_by": "plotcut",
        "last_write_at": int(time.time() * US),
        "duration_us": timeline["duration_us"],
        # 우리가 실제로 쓴 트랙 구성 (기록용).
        # 자막 트랙 2개를 "사용자 편집"으로 오판해 갱신 경로를 스스로 막는다.
        # TTS 전후로 오디오 트랙이 0↔1로 바뀌므로 상수로 박아선 안 된다.
        "expected_tracks": timeline.get("track_counts") or {
            k: sum(1 for t in timeline["tracks"] if t["type"] == k)
            for k in ("video", "text", "audio")},
    }


def _project_json_id(proj_dir):
    pj = Path(proj_dir) / "Timelines" / "project.json"
    if pj.exists():
        try:
            return json.loads(pj.read_text(encoding="utf-8")).get("id")
        except json.JSONDecodeError:
            return None
    return None


