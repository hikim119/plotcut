"""
pipeline.py — 「타임라인 대본」 txt → CapCut 프로젝트 (LLM 없음)

GUI와 CLI가 같은 함수를 부른다. log/progress/stop 을 주입받아 GUI가 워커
스레드에서 돌려도 UI를 막지 않는다.
"""

import hashlib
import json
import shutil
import time
from pathlib import Path

import capcut_draft as cd
import guards
import layout
import movie_info
import script_io
import subtitle

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"
SCHEMA_VERSION = 2
TOOL_VERSION = "plotcut 0.2.0"


class Stopped(RuntimeError):
    pass


def _noop(*_a, **_k):
    pass


def _check(stop):
    if stop is not None and stop.is_set():
        raise Stopped()


# 단계가 끝날 때마다 여기까지 채운다. 실측: 대본 만들기 434~593초,
# 나머지 전부 합쳐 1~3초. 그래서 대본을 만들면 그 단계에 90%를 준다.
STEPS_GEN = {0: 0.90, 1: 0.93, 2: 0.95, 3: 0.96, 4: 0.98, 5: 0.99, 6: 1.00}
STEPS_PLAIN = {1: 0.15, 2: 0.40, 3: 0.55, 4: 0.80, 5: 0.95, 6: 1.00}


def took(t0):
    """`3분 12초` / `47초`. 끝났을 때 얼마나 걸렸는지 로그에 남긴다 —
    대본 만들기가 5~10분이라 이 숫자가 다음 실행의 기준이 된다."""
    d = max(0.0, time.time() - t0)
    return ("%d분 %d초" % (d // 60, d % 60)) if d >= 60 else ("%.0f초" % d)


# ── 결과 폴더 · state.json ──────────────────────────────────────────────────

stamp = script_io.stamp          # 시각 표기는 script_io 가 정본이다


def movie_base(movie_path=None, srt_path=None, script_path=None):
    """파일 이름에서 영화 이름만 뽑는다 (`_자막` 같은 꼬리를 뗀다)."""
    src = movie_path or srt_path or script_path
    if not src:
        return "project"
    base = Path(src).stem
    for suf in ("_자막", "_subtitle", "_sub"):
        if base.endswith(suf):
            return base[: -len(suf)]
    return script_io.strip_suffix(base)


def default_project_name(movie_path=None, srt_path=None, when=None):
    """<영화명>_MMDD_HHMM — 돌릴 때마다 별개 프로젝트가 된다."""
    return "%s_%s" % (movie_base(movie_path, srt_path), stamp(when))


def _discard_dir(d):
    """중단으로 버려진 결과 폴더를 지운다. 대본이 이미 나왔으면 손대지 않는다."""
    if not d:
        return
    try:
        left = [f for f in Path(d).iterdir()
                if not f.name.startswith("생성중_")
                and f.name != guards.LOCK_NAME]
        if not left:
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def fresh_results_dir(project_name, suffix=""):
    """겹치면 `이름_2`, `이름_3` … 으로 **새로** 만든다.

    갱신(덮어쓰기)을 하지 않으므로 결과 폴더도 절대 재사용하지 않는다 —
    이전 대본·상태가 새 결과에 섞이면 어느 게 무엇인지 알 수 없다.

    번호는 **접미사 앞**에 붙는다 (`이름_2_대본만만들기`). 접미사가 항상 끝에
    와야 폴더 목록에서 종류가 한눈에 보인다.
    """
    base = _safe_name(project_name)
    name, n = base + suffix, 2
    while True:
        # exists() 로 보고 나중에 mkdir 하면 두 실행이 같은 폴더를 잡는다.
        # 중간 파일(생성중_지시서.txt 등)이 이 폴더 안에 있으므로 그러면
        # 서로 프롬프트를 덮어써 **엉뚱한 자막으로 대본이 나온다**.
        # mkdir 자체를 경쟁 판정으로 쓴다 — 이긴 쪽만 그 이름을 갖는다.
        d = RESULTS / name
        try:
            d.mkdir(parents=True, exist_ok=False)
            return d
        except FileExistsError:
            name, n = "%s_%d%s" % (base, n, suffix), n + 1


def _safe_name(name):
    bad = '<>:"/\\|?*'
    out = "".join("_" if c in bad else c for c in str(name)).strip(" .")
    return out or "project"


def list_projects():
    """최근에 손댄 것부터. 이름의 시각만 믿지 않고 폴더 mtime 으로 정렬한다."""
    if not RESULTS.exists():
        return []
    dirs = [d for d in RESULTS.iterdir()
            if d.is_dir() and (d / "state.json").exists()]
    dirs.sort(key=lambda d: (d / "state.json").stat().st_mtime, reverse=True)
    return [d.name for d in dirs]


def write_atomic(path, text):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".plotcut.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        import os
        os.fsync(f.fileno())
    tmp.replace(path)
    return path


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def save_state(path, state):
    state = dict(state)
    state.pop("self_sha256", None)
    state["self_sha256"] = hashlib.sha256(_canon(state).encode("utf-8")).hexdigest()
    write_atomic(path, json.dumps(state, ensure_ascii=False, indent=1))
    return state


def load_state(path, verify=True):
    p = Path(path)
    if not p.exists():
        return None
    state = json.loads(p.read_text(encoding="utf-8"))
    if verify and state.get("self_sha256"):
        want = state["self_sha256"]
        probe = dict(state)
        probe.pop("self_sha256")
        got = hashlib.sha256(_canon(probe).encode("utf-8")).hexdigest()
        if got != want:
            raise guards.GuardError(
                "state.json이 손상됐거나 손으로 고쳐졌습니다 (%s).\n"
                "  프로젝트를 다시 만드세요." % p)
    return state


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── 공통 준비 ───────────────────────────────────────────────────────────────

def _last_span(script_path):
    """자막도 영화도 없을 때 대본에 박힌 마지막 시각으로 길이를 어림한다."""
    try:
        txt = Path(script_path).read_bytes().decode("utf-8-sig")
        d = script_io.read(txt, [], log=lambda *_: None)
        ends = [it["span"][1] for _, it in script_io.items(d) if it.get("span")]
        return max(ends) if ends else 600.0
    except Exception:                                       # noqa: BLE001
        return 600.0


def _prepare(script_path, srt_path, movie_path, offset_s, fps_scale,
             prefer_class, log, stop):
    """자막·영화·대본을 읽고 타임라인을 계산한다. 디스크에 쓰지 않는다."""
    _check(stop)
    if srt_path:
        cues, meta = subtitle.parse_file(srt_path, prefer_class, offset_s,
                                         fps_scale)
        if not cues:
            raise script_io.ScriptError("자막에서 대사를 하나도 읽지 못했습니다.")
        d = dict(meta["dropped"])
        removed = d.pop("removed", [])
        log("  ✔ 자막 %d개 (원본 %d · %s) %s"
            % (meta["cue_count"], meta["raw_count"], meta["encoding"],
               ("· 버림 %d" % len(removed)) if removed else ""))
    else:
        # 대본이 시각을 들고 있으면(`@…~…`) 자막이 없어도 만들 수 있다.
        cues, meta = [], {"cue_count": 0, "raw_count": 0, "encoding": "",
                          "dropped": {}, "offset_s": offset_s,
                          "fps_scale": fps_scale, "parser": ""}
        log("  ! 자막 없음 — 대본에 박힌 시각을 그대로 씁니다")

    _check(stop)
    if movie_path:
        info = movie_info.probe(movie_path)
        log("  ✔ 영화 %s · %d×%d · %.3ffps"
            % (movie_info.fmt_dur(info["duration_s"]), info["width"],
               info["height"], info.get("fps") or 0))
        ratio = (cues[-1]["end_s"] / max(1.0, info["duration_s"])) if cues else 1.0
        if not (0.90 <= ratio <= 1.04):
            log("  ⚠ 자막 끝 / 영화 길이 = %.3f — 릴리즈가 다를 수 있습니다 "
                "(자막 오프셋으로 보정하세요)" % ratio)
    else:
        # 영화 없이도 타임라인·자막은 만들 수 있다. 소스 시각을 클램프할 기준이
        # 없으므로 자막 끝보다 넉넉히 잡는다(나중에 영화를 주면 다시 계산된다).
        last = cues[-1]["end_s"] if cues else _last_span(script_path)
        info = {"path": None, "duration_s": last + 600.0,
                "width": 1920, "height": 1080, "fps": 24.0, "size": 0, "mtime": 0}
        log("  ! 영화 파일 없음 — 자막·타임라인만 만듭니다")

    _check(stop)
    text = Path(script_path).read_bytes().decode("utf-8-sig")
    doc = script_io.read(text, cues, log=log)
    st = doc["stats"]
    log("  ✔ 대본 블록 %d · 대사 %d(서명 %d·병합 %d·유사 %d) · 나레이션 %d"
        % (len(doc["blocks"]), st["dialogue"], st["by_sig"], st["by_merge"],
           st["by_diff"], len(script_io.narrations(doc))))
    if not cues and st["demoted"]:
        # 시각도 자막도 없는 옛 대본. 막지 않고 **블록 범위에 균등 배치**한다 —
        # 컷이 대사 순간에 딱 맞지는 않지만(실측 중앙 5.4초 오차) 프로젝트는 나온다.
        log("  ! 시각이 없는 문단 %d개 — 블록 범위에 길이만큼 균등 배치합니다"
            % st["demoted"])
        log("    (컷이 대사 순간과 어긋납니다. 자막을 함께 넣으면 정확해집니다)")
    elif st["demoted"]:
        log("  ⚠ 자막에서 못 찾은 대사 문단 %d개 — 그 컷은 음성과 자막이 어긋납니다"
            % st["demoted"])
    for w in doc["warnings"]:
        log("  ⚠ " + w)
    return cues, meta, info, doc


def _build_layout(doc, cues, info, narration_durs=None, mute=False, log=print):
    pl = layout.build(doc, cues, info["duration_s"],
                      fps=info.get("fps") or 24.0,
                      narration_durs=narration_durs,
                      mute_under_narration=mute, log=log)
    for w in pl["warnings"]:
        log("  ⚠ " + w)
    s = pl["stats"]
    log("  ✔ 컷 %d · 자막 %d · 음소거 %d · 이어붙임 %d · 총 %.1f초"
        % (s["cuts"], s["subs"], s["muted"], s["merged"], s["total_s"]))
    return pl


def _write_script_files(rdir, doc, pl, movie_name, freeze=False):
    """계산된 범위를 반영한 txt 사본과 나레이션 파일.

    freeze 면 대사마다 소스 구간을 박아 **자막 없이도 다시 만들 수 있는** 대본이 된다.
    """
    layout.apply_headers(doc, pl["headers"])
    if freeze:
        layout.apply_spans(doc, pl)
    out = rdir / ("%s%s.txt" % (movie_name, script_io.SUFFIX))
    write_atomic(out, script_io.write(doc))
    write_atomic(rdir / "narration.txt", script_io.narration_text(doc))
    return out


def _cuts_of(pl):
    return [{"order": i, "src_start_s": round(s["src0"], 3),
             "src_dur_s": round(s["t_dur"], 3), "volume": s["volume"],
             "kind": s["kind"], "tag": s.get("tag", "")}
            for i, s in enumerate(pl["segments"])]


# ── 대본만 만들기 ───────────────────────────────────────────────────────────

# 대본만 만들기 결과도 results/ 안에 자기 폴더를 갖는다.
# 자막 파일 옆에 떨어뜨리면 남의 폴더(영화·자막 보관함)를 어지럽힌다.
SCRIPT_DIR_SUFFIX = "_대본만만들기"

def run_script(srt_path, out=None, project_name=None, target_s=180.0, extra="",
               movie_title="",
               prefer=None, offset_s=0.0, fps_scale=1.0, prefer_class=None,
               log=print, progress=_noop, stop=None):
    """자막 → 대본 txt 까지만. CapCut은 만들지 않는다."""
    import script_gen
    t0 = time.time()
    base = movie_base(srt_path=srt_path)
    # out 을 직접 준 건 "내가 위치를 정하겠다"는 뜻이다 — 빈 결과 폴더를
    # 만들어 두지 않는다. 중간 파일은 script_gen 이 .work/ 로 보낸다.
    rdir = None if out else fresh_results_dir(project_name or base,
                                              SCRIPT_DIR_SUFFIX)
    if rdir is not None:
        out = rdir / ("%s%s.txt" % (base, script_io.SUFFIX))
    log("[1] 대본 만들기")
    if rdir is not None:
        log("  폴더: %s" % rdir)
    # 중간 파일(지시서·초안·에이전트 로그)도 이 폴더에 둔다 — 한 실행이
    # 한 폴더로 끝나고, 성공하면 대본만 남는다.
    # 에이전트가 도는 동안 경과/예상으로 실제로 차오른다 (0 → 90%)
    try:
        path = script_gen.generate(srt_path, out, target_s=target_s, extra=extra,
                                   movie_title=movie_title,
                                   prefer=prefer, work_dir=rdir,
                                   log=log, stop=stop,
                                   progress=lambda f: progress(0.90 * f))
    except script_gen.GenStopped:
        # 중단은 실패가 아니라 취소다 — 진단할 게 없으니 만든 폴더째 치운다.
        _discard_dir(rdir)
        raise Stopped() from None
    log("  %s" % path)

    progress(0.93)
    log("[2] 자막과 대조")
    cues, meta = subtitle.parse_file(srt_path, prefer_class, offset_s, fps_scale)
    doc = script_io.read(Path(path).read_bytes().decode("utf-8-sig"), cues, log=log)
    st = doc["stats"]
    dur = (cues[-1]["end_s"] + 600.0) if cues else 600.0
    pl = layout.build(doc, cues, dur, log=lambda *_: None)
    log("  블록 %d · 대사 %d(서명 %d·병합 %d·유사 %d) · 나레이션 %d"
        % (len(doc["blocks"]), st["dialogue"], st["by_sig"], st["by_merge"],
           st["by_diff"], len(script_io.narrations(doc))))
    if st["demoted"]:
        log("  ⚠ 자막에서 못 찾은 대사 문단 %d개 — 고쳐야 합니다" % st["demoted"])
    for w in doc["warnings"][:6]:
        log("  ⚠ " + w)
    log("  총 길이 %.1f초 (%.1f분) · 목표 %.0f초"
        % (pl["total_s"], pl["total_s"] / 60, target_s))

    # 에이전트가 쓴 대본에는 시각이 없다. 한국어 자막이면 대사 텍스트로 다시
    # 찾을 수 있지만, 외국어 자막이면 `#763` 번호뿐이라 **자막 파일이 없으면
    # 아무 시각도 알 수 없다.** 여기서 박아 두면 대본 하나로 완결된다.
    if st["demoted"] < st["dialogue"]:
        layout.apply_headers(doc, pl["headers"])
        layout.apply_spans(doc, pl)
        write_atomic(Path(path), script_io.write(doc))
        write_atomic(Path(path).parent / "narration.txt",
                     script_io.narration_text(doc))
        log("  ✔ 대사마다 시각을 박았습니다 — 자막 파일 없이도 만들 수 있습니다")

    progress(1.0)
    log("\n완료 (%s) — [%s] 탭에서 고칠 수 있습니다."
        % (took(t0), script_io.LABEL))
    plot = Path(path).parent / script_gen.PLOT_NAME
    if plot.exists():
        log("  ✔ %s — 편집 전에 읽어 보세요" % plot.name)
    return {"script_path": str(path),
            "results_dir": str(rdir) if rdir else None,
            "plot_path": str(plot) if plot.exists() else None,
            "doc": doc, "plan": pl}


# ── 만들기 ──────────────────────────────────────────────────────────────────

def run_build(script_path, srt_path, movie_path=None, project_name=None,
              canvas="vertical", fit="fit", offset_s=0.0, fps_scale=1.0,
              prefer_class=None, draft_root=None,
              target_s=180.0, extra="", movie_title="",
              prefer=None, freeze=True, mute=False,
              log=print, progress=_noop, stop=None):
    t0 = time.time()
    made_script = False
    if not script_path and not srt_path:
        raise guards.GuardError("자막 파일이 필요합니다.")
    if not project_name:
        project_name = default_project_name(movie_path,
                                            srt_path or script_path)

    # 폴더를 **대본 생성보다 먼저** 만든다. 대본을 자막 파일 옆에 떨어뜨리지
    # 않고 이 프로젝트 폴더 안에 두려면 폴더가 먼저 있어야 한다.
    rdir = fresh_results_dir(project_name)
    try:
        with guards.ProjectLock(rdir):
            if rdir.name != _safe_name(project_name):
                log("  (같은 이름이 있어 '%s' 로 새로 만듭니다)" % rdir.name)
            project_name = rdir.name

            # 대본이 없으면 자막만으로 만든다 — 이미 로그인된 에이전트 CLI를 부른다
            # (API 키 없음, 구독에 포함). script_gen 참고.
            # [3] 단계가 범위를 다시 계산해 같은 경로에 덮어쓴다 — 그래서 폴더에는
            # 대본 txt 가 하나만 남는다.
            if not script_path:
                import script_gen
                out = rdir / ("%s%s.txt"
                              % (movie_base(movie_path, srt_path), script_io.SUFFIX))
                log("[0] 대본 만들기 (자막만 있으므로)")
                try:
                    script_path = script_gen.generate(
                        srt_path, out, target_s=target_s, extra=extra,
                        movie_title=movie_title, prefer=prefer,
                        work_dir=rdir, log=log, stop=stop,
                        progress=lambda f: progress(0.90 * f))
                except script_gen.GenStopped:
                    raise Stopped() from None
                made_script = True
                log("  대본: %s" % script_path)
            # 자막이 없으면 대본이 시각을 들고 있어야 한다 — _prepare 가 확인한다.

            step = STEPS_GEN if made_script else STEPS_PLAIN
            log("[1] 자막·영화·대본 읽기")
            cues, meta, info, doc = _prepare(script_path, srt_path, movie_path,
                                             offset_s, fps_scale, prefer_class, log, stop)

            progress(step[1])
            log("[2] 타임라인 계산")
            pl = _build_layout(doc, cues, info, mute=mute, log=log)

            progress(step[2])
            log("[3] 대본 사본 저장")
            script_copy = _write_script_files(
                rdir, doc, pl, movie_base(movie_path, srt_path, script_path),
                freeze=freeze)
            log("  ✔ %s" % script_copy.name)

            progress(step[3])
            log("[4] CapCut 타임라인 조립")
            _check(stop)
            movie_arg = ({"path": movie_path, "duration_s": info["duration_s"],
                          "width": info["width"], "height": info["height"]}
                         if movie_path else None)
            timeline = cd.build_timeline(pl, movie_arg, canvas=canvas, fit=fit, log=log)

            progress(step[4])
            # 항상 새로 만든다. 기존 프로젝트를 덮어쓰지 않으므로 편집 감지·백업·
            # 되돌리기가 필요 없고, CapCut 을 켜 둔 채로도 안전하다.
            log("[5] CapCut 프로젝트 생성")
            ident = cd.create_project(project_name, timeline,
                                      draft_root=draft_root, log=log)
            note = guards.version_note(ident.get("app_version"))
            if note:
                log("  ⚠ " + note)

            progress(step[5])
            log("[6] 상태 저장")
            state = _new_state(project_name, script_copy, srt_path, meta, movie_path,
                               info, pl, ident, canvas, fit, offset_s, fps_scale)
            save_state(rdir / "state.json", state)
            progress(step[6])
            log("\n완료 (%s) — CapCut을 껐다 켜면 '%s' 프로젝트가 보입니다."
                % (took(t0), ident["folder_name"]))
            plot = rdir / "줄거리.txt"
        return {"state": state, "results_dir": str(rdir),
                "plot_path": str(plot) if plot.exists() else None,
                "plan": pl, "doc": doc}
    except Stopped:
        # 중단은 실패가 아니라 취소다 — 아직 아무 결과도 안 나왔으면
        # 만들어 둔 폴더를 지운다(락이 풀린 뒤라야 지워진다).
        _discard_dir(rdir)
        raise


# ── state ──────────────────────────────────────────────────────────────────

def _new_state(name, script_copy, srt_path, meta, movie_path, info, pl, ident,
               canvas, fit, offset_s, fps_scale, keep=None):
    now = int(time.time() * 1_000_000)
    keep = keep or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "project_name": name,
        "created_at": keep.get("created_at", now),
        "updated_at": now,
        "settings": {"canvas": canvas, "fit": fit},
        "inputs": {
            "script_txt": {"path": str(Path(script_copy).resolve()),
                           "sha256": file_sha256(script_copy)},
            # 대본에 시각이 박혀 있으면 자막 없이도 만든다 — 그때는 비워 둔다.
            "subtitle": ({"path": str(Path(srt_path).resolve()),
                          "sha256": file_sha256(srt_path),
                          "encoding": meta["encoding"],
                          "cue_count": meta["cue_count"],
                          "offset_s": offset_s, "fps_scale": fps_scale,
                          "prefer_class": meta.get("chosen_class")}
                         if srt_path else None),
            "movie": ({"path": str(Path(movie_path).resolve()),
                       "size": info["size"], "mtime": info["mtime"],
                       "duration_s": info["duration_s"],
                       "width": info["width"], "height": info["height"],
                       "fps": info.get("fps")} if movie_path else None),
        },
        "cuts": _cuts_of(pl),
        "capcut": ident,
        "stats": dict(pl["stats"]),
    }

