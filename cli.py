"""
cli.py — PlotCut 명령줄

  python cli.py check <타임라인과자막.txt> --srt <자막.srt> [--seconds 180] [--movie <영화>]
  python cli.py script <자막.srt> [--seconds 180] [--extra "..."]
                       자막만으로 대본 txt 생성 (로그인된 Codex/Claude CLI 사용, 0원)

  python cli.py build [<타임라인과자막.txt>] --srt <자막.srt> [--name <프로젝트명>]
                      [--movie <영화>]        대본을 생략하면 먼저 만든다
                                              영화를 생략하면 자막·타임라인만
                      [--canvas vertical|square|horizontal] [--draft-root <임시폴더>]
  python cli.py list

LLM을 부르지 않는다. 대본은 클로드 코드가 쓰고, 이 도구는 그 txt를 CapCut으로 옮긴다.
"""

import argparse
import re
import sys
from pathlib import Path

# 가벼운 모듈(정규식만 쓴다)이라 위에서 부른다 — 임계값을 한 곳에 두려면
# 모듈 수준에서 참조해야 한다.
import quality

# ── check 임계값 (정답 샘플 실측에서 3배 여유로 역산) ──────────────────────
MAX_ITEM_S = 20.0      # 한 대사 아이템이 덮는 시간   (정답 최대 6.04초)
MAX_CUE_GAP_S = 3.0    # 2큐 아이템 안쪽 간격         (정답 최대 0.88초)
MAX_BROLL_S = 300.0    # b-roll 블록 폭               (정답 249.2초)
# 나레이션 길이 임계는 **여기 없다** — `quality.ITEM_MIN/ITEM_MAX`(5~34) 와
# `MEDIAN_LO/HI`(11~20) 가 살아 있는 값이다. 여기 있던 `NARR_MIN/NARR_MAX`(5~40)
# 와 `NARR_AVG_LO/HI`(12~26) 는 **아무도 안 읽는데 살아 있는 값과 달라서**,
# 고치면 뭔가 바뀔 것처럼 보이는 함정이었다. `SHORT_RATIO_MAX=0.40` 은 정답
# 본인을 걸어서 폐기된 값이다 (selftest [14] 가 그 사실을 회귀로 못 박는다).
# 대사 몇 줄마다 나레이션 하나 — 이 비율이 문장력보다 자연스러움을 좌우한다.
# 내 대본 실측 5.3 / 7.7. 아래로 내려가면 '요약 + 인용문'이 된다.
DLG_PER_NARR_MIN = 4.0
# ENDING_RATIO 는 quality.py 로 옮겼다 — 선택 범위 지표와 한 곳에 있어야
# "전체 요약 모드에서만 본다"는 규칙이 한눈에 보인다.
LEN_TOL = 0.15
# 아래 둘은 잘 나온 대본의 실측값이다. 이게 무너지면 '이야기'가 아니라
# '요약 + 인용문'으로 읽힌다.
BARE_BLOCK_MIN = 0.25  # 나레이션이 아예 없는 블록 비율 (정답 6/18 = 33%)
NARR_RUN_MAX = quality.RUN_MAX  # 나레이션 연속 줄 수 — 값은 quality.py 에 하나만


def _utf8():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")


# 이 모듈들이 정의한 예외는 **전부** 「사람이 고칠 수 있는 상황」이다.
# 자막을 못 읽는다 · 에이전트가 안 깔렸다 · ffmpeg 가 없다 · CapCut 이 없다 ·
# CapCut 이 그 프로젝트를 열고 있다 — 메시지 자체는 이미 친절한데, 예전엔
# 화이트리스트에 이름을 하나씩 적어 두는 방식이라 **세 번 연속으로 빠뜨렸다**
# (GenError · SubtitleError · ProbeError/DraftError). 그때마다 트레이스백이
# 그대로 나갔고 GUI 는 `except Exception` 이라 멀쩡해서 CLI 만 샜다.
#
# 그래서 **이름을 안 적는다.** 모듈에서 긁어모은다. 새 예외를 만들어도 자동으로
# 포함되고, 여기 없는 모듈을 새로 만들면 selftest [27] 이 걸린다.
_ERR_MODULES = ("guards", "script_io", "layout", "script_gen", "subtitle",
                "movie_info", "capcut_draft")


def user_errors():
    """사람이 고칠 수 있는 실패들 — `✘` 한 줄로 보여 줄 예외 튜플."""
    import importlib
    import inspect
    out = []
    for name in _ERR_MODULES:
        mod = importlib.import_module(name)
        for c in vars(mod).values():
            if (inspect.isclass(c) and issubclass(c, Exception)
                    and c.__module__ == name):
                out.append(c)
    return tuple(out)


def user_message(e):
    """사람이 고칠 수 있는 실패면 한 줄 메시지, 아니면 None. **CLI 와 GUI 가 같이 쓴다.**

    파이썬 내장 예외 둘은 `user_errors()` 에 안 잡혀서 GUI 가 `FileNotFoundError:`
    를 그대로 찍었다(실측). 판정을 한 곳에 두면 두 화면이 어긋나지 않는다.
    """
    if isinstance(e, user_errors()):
        return str(e)
    if isinstance(e, FileNotFoundError):
        return "파일을 찾을 수 없습니다: %s" % (e.filename or e)
    if isinstance(e, UnicodeDecodeError):
        return "파일 인코딩을 읽지 못했습니다 — UTF-8 로 저장해 보세요."
    return None


def main(argv=None):
    _utf8()
    ap = argparse.ArgumentParser(prog="plotcut")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="대본 txt를 자막과 대조 (API 없음, 무료)")
    p.add_argument("script")
    p.add_argument("--srt", required=True)
    p.add_argument("--movie", default=None, help="있으면 길이·싱크까지 검사")
    p.add_argument("--seconds", type=float, default=180.0)
    p.add_argument("--offset", type=float, default=0.0)
    p.add_argument("--fps-scale", type=float, default=1.0)
    p.add_argument("--class", dest="prefer_class", default=None)

    p = sub.add_parser("script", help="자막 → 대본 txt (에이전트 CLI 사용, 0원)")
    p.add_argument("srt")
    p.add_argument("--name", default=None,
                   help="결과 폴더 이름. 생략하면 자막 파일명에서 따온다")
    p.add_argument("--out", default=None,
                   help="대본 경로를 직접 지정. 생략하면 결과 폴더 안에 만든다")
    p.add_argument("--seconds", type=float, default=180.0)
    p.add_argument("--extra", default="", help="연출 지시 (예: 잭 이야기만)")
    p.add_argument("--title", default="",
                   help="영화 제목. 알면 적어라 — 에이전트가 줄거리·결말을 참고한다")
    p.add_argument("--agent", default=None, choices=["codex", "claude"],
                   help="대본 생성기. 생략하면 로그인된 것을 자동으로 고른다")
    p.add_argument("--candidates", type=int, default=1,
                   help="후보를 N개 뽑아 에이전트가 고른다 (약 4배 느림). 기본 1")

    p = sub.add_parser("build", help="CapCut 프로젝트 만들기 (매번 새로 만든다)")
    p.add_argument("script", nargs="?", default=None,
                   help="없으면 자막으로 대본을 먼저 만든다")
    p.add_argument("--srt", default=None,
                   help="대본에 시각이 박혀 있으면(--freeze 로 만든 것) 생략 가능")
    p.add_argument("--movie", default=None,
                   help="없으면 영상 트랙 없이 자막·타임라인만 만든다")
    p.add_argument("--no-freeze", dest="freeze", action="store_false",
                   help="결과 대본에 컷 시각을 박지 않는다 (기본은 박는다)")
    p.add_argument("--mute", dest="mute", action="store_true",
                   help="나레이션 구간에서 원본 대사를 음소거한다 (기본은 안 한다)")
    p.add_argument("--name", default=None)
    p.add_argument("--seconds", type=float, default=180.0)
    p.add_argument("--extra", default="")
    p.add_argument("--title", default="",
                   help="영화 제목. 알면 적어라 — 에이전트가 줄거리·결말을 참고한다")
    p.add_argument("--agent", default=None, choices=["codex", "claude"],
                   help="대본 생성기. 생략하면 로그인된 것을 자동으로 고른다")
    p.add_argument("--canvas", default="vertical",
                   choices=["vertical", "square", "horizontal"])
    p.add_argument("--fit", default="fit", choices=["fit", "fill"])
    p.add_argument("--offset", type=float, default=0.0)
    p.add_argument("--fps-scale", type=float, default=1.0)
    p.add_argument("--class", dest="prefer_class", default=None)
    p.add_argument("--draft-root", default=None, help="CapCut 드래프트 폴더 (테스트용)")

    sub.add_parser("list", help="results/ 프로젝트 목록")

    a = ap.parse_args(argv)
    try:
        return _run(a)
    except KeyboardInterrupt:
        print("\n⛔ 중단되었습니다.")
        return 130
    except Exception as e:                                  # noqa: BLE001
        msg = user_message(e)
        if msg is not None:
            print(chr(10) + "✘ " + msg)
            return 1
        raise


def _run(a):
    if a.cmd == "check":
        return cmd_check(a)
    if a.cmd == "script":
        import script_gen
        import pipeline
        r = pipeline.run_script(a.srt, a.out, project_name=a.name,
                                target_s=a.seconds, extra=a.extra,
                                movie_title=a.title, candidates=a.candidates,
                                prefer=a.agent, log=print)
        print("\n대본: %s" % r["script_path"])
        return 0
    if a.cmd == "build":
        import pipeline
        pipeline.run_build(a.script, a.srt, a.movie, a.name,
                           canvas=a.canvas, fit=a.fit, offset_s=a.offset,
                           fps_scale=a.fps_scale, prefer_class=a.prefer_class,
                           draft_root=a.draft_root,
                           target_s=a.seconds, extra=a.extra,
                           movie_title=a.title, prefer=a.agent,
                           freeze=a.freeze, mute=a.mute, log=print)
        return 0
    if a.cmd == "list":
        import pipeline
        names = pipeline.list_projects()
        if not names:
            print("프로젝트가 없습니다.")
        for n in names:
            print("  " + n)
        return 0
    return 1


# ── check ───────────────────────────────────────────────────────────────────

def cmd_check(a):
    import layout
    import script_io
    import subtitle

    cues, meta = subtitle.parse_file(a.srt, a.prefer_class, a.offset, a.fps_scale)
    text = Path(a.script).read_bytes().decode("utf-8-sig")

    dur = None
    fps = 24.0
    if a.movie:
        import movie_info
        info = movie_info.probe(a.movie)
        dur = info["duration_s"]
        fps = info.get("fps") or 24.0
    else:
        dur = (cues[-1]["end_s"] if cues else 0.0) / 0.97 + 60.0

    print("자막  %s" % Path(a.srt).name)
    d = dict(meta["dropped"])
    removed = d.pop("removed", [])
    print("  큐 %d개 (원본 %d) · 인코딩 %s · %s ~ %s"
          % (meta["cue_count"], meta["raw_count"], meta["encoding"],
             _hms(cues[0]["start_s"]), _hms(cues[-1]["end_s"])))
    if removed:
        print("  버린 큐 %d개: %s" % (len(removed),
              ", ".join("SRT#%s(%s)" % (n, w) for n, w, _ in removed[:8])))

    doc = script_io.read(text, cues)
    st = doc["stats"]
    errors, warns, notes = [], [], []
    warns.extend(doc["warnings"])

    print("\n대본  %s" % Path(a.script).name)
    print("  제목: %s" % (doc["title"] or "(없음)"))
    print("  블록 %d · 대사 문단 %d · 나레이션 %d"
          % (len(doc["blocks"]), st["dialogue"],
             len(script_io.narrations(doc))))
    # `시각` 을 안 찍으면 frozen 대본이 "매칭 0 · 강등 0" 으로 보여 **완벽한 줄
    # 착각한다.** 실제로는 매칭을 한 번도 시도하지 않은 것이다.
    _tm = ("" if not st.get("by_time") else
           " · 시각 %d(대조 %d·불일치 %d·큐없음 %d)"
           % (st["by_time"], st.get("time_ok", 0),
              st.get("time_mismatch", 0), st.get("time_nocue", 0)))
    print("  매칭: 서명 %d · 2큐병합 %d · 유사 %d%s · 강등 %d"
          % (st["by_sig"], st["by_merge"], st["by_diff"], _tm, st["demoted"]))
    # `#a-b` 폭 위반과 헤더 섞임은 이제 `script_io.read` 가 **ScriptError 로**
    # 던진다 — check·build·GUI 가 같은 곳에서 멈추게. 여기엔 검사가 없다.
    # 같은 큐를 두 번 인용한 대사 문단 — 훅으로 앞에 뺀 대사를 제자리에서 또 쓴 것이다.
    # 22개 벤치 대본을 다시 재니 **진 판이 전부 이걸 갖고 있었다**(shipped gentleman 2 ·
    # base2 gentleman 4 · v1 father 2), 심사평도 「2~3줄이 36~37줄에서 그대로 반복」.
    # 유저 레퍼런스는 0. 채택 3조건(정답 통과·네거티브 존재·겹침 없음)을 만족한다.
    # ✘ 여야 프롬프트의 check 루프(최대 5회)가 스스로 고친다 — 그게 이 검사의 효과다.
    _dup = _requoted(doc)
    if _dup:
        errors.append(
            "같은 대사를 두 번 인용한 문단이 %d쌍 있습니다 — 훅으로 앞에 뺀 대사는 "
            "제자리에서 다시 쓰지 마세요: %s" % (len(_dup), " / ".join(_dup)[:80]))
    if st.get("time_mismatch") or st.get("time_nocue"):
        errors.append(
            "`@시각` 대본의 대사 %d개가 그 시각의 자막과 다릅니다 — "
            "지어냈거나 시각이 틀렸습니다."
            % (st.get("time_mismatch", 0) + st.get("time_nocue", 0)))

    # 번역 줄 — 자막이 한국어가 아니면 **전부** 번역이 붙어야 한다.
    # 안 붙으면 외국어가 그대로 화면에 나가므로 경고가 아니라 오류다.
    dlg = [it for _, it in script_io.items(doc) if it["kind"] == "dialogue"]
    ko = subtitle.is_korean(cues)
    korean = [it for it in dlg if it.get("cue_ref") or it.get("trans")]
    if not ko:
        print("  자막 언어: 한국어 아님 → 대사를 `#자막번호 한국어` 로 써야 합니다")
    if korean:
        print("  한국어 대사: %d개 / %d개 (번호 %d · 번역줄 %d)"
              % (len(korean), len(dlg),
                 sum(1 for it in dlg if it.get("cue_ref")),
                 sum(1 for it in dlg if it.get("trans"))))
    if not ko and len(korean) < len(dlg):
        errors.append(
            "한국어로 안 바꾼 대사 문단이 %d개입니다 — 자막이 한국어가 아니라서 "
            "그대로 두면 외국어가 화면에 나갑니다. `#자막번호 한국어 대사` 로 쓰세요."
            % (len(dlg) - len(korean)))
        for bi, it in script_io.items(doc):
            if it["kind"] == "dialogue" and not (it.get("cue_ref")
                                                 or it.get("trans")):
                print("    ✘ 블록%d  %s" % (bi + 1, it["text"][:44]))

    if st["demoted"]:
        errors.append(
            "자막에서 찾지 못한 대사 문단 %d개 — 그 컷은 영화 음성과 자막이 어긋납니다."
            % st["demoted"])
        for bi, it in script_io.items(doc):
            if it["kind"] == "dialogue" and not it["cues"]:
                print("    ✘ 블록%d  %s" % (bi + 1, it["text"][:44]))

    # `#763 아빠? / 마야?` 처럼 번호를 안 붙인 줄을 밑에 붙이면, 그 줄이 앞 큐
    # 구간에 같이 뜨고 뒤 큐 장면은 통째로 사라진다 (실측 4.17초 → 2.24초).
    if st.get("overstuffed"):
        errors.append(
            "번호 없는 줄을 붙인 문단 %d개 — 그 자막의 장면이 통째로 빠집니다. "
            "줄마다 번호를 달아 문단을 나누세요." % st["overstuffed"])
        for bi, it in script_io.items(doc):
            ref = it.get("cue_ref")
            if ref and it.get("cues") and \
                    len(it["lines"]) > sum(len(c["lines"]) for c in it["cues"]):
                print("    ✘ 블록%d  #%d  %s"
                      % (bi + 1, ref[0], " / ".join(it["lines"])[:40]))


    for bi, it in script_io.items(doc):
        if it["kind"] == "dialogue" and it["cues"] and "동일 후보" in (it["note"] or ""):
            notes.append("블록%d  %s → %s" % (bi + 1, it["text"][:26], it["note"]))

    # 시간 상한
    for bi, blk in enumerate(doc["blocks"]):
        # `span` 도 같이 본다. `@시각`으로 매칭된 문단은 **`cues` 가 비어 있다** —
        # `--freeze` 우회를 막을 때 일부러 그렇게 뒀다(채우면 자막 노출 시점이
        # 밀리고 되감기 경고가 LayoutError 로 승격된다). `cues` 만 보면 시각이
        # 박힌 대본은 **모든 블록이 「대사 없음」**으로 보여서, 범위가 상한만
        # 넘으면 b-roll 경고가 무조건 뜬다. 실측: 대사 58문단짜리 대본에서
        # 오탐 4건 — 대사가 10문단 있는 블록까지 걸렸다.
        has_dlg = any(i["kind"] == "dialogue" and (i["cues"] or i.get("span"))
                      for i in blk["items"])
        if not has_dlg and blk["win"]:
            w = blk["win"][1] - blk["win"][0]
            if w > MAX_BROLL_S:
                warns.append("블록%d b-roll 범위가 %.0f초입니다 (권장 %.0f초 이하)."
                             % (bi + 1, w, MAX_BROLL_S))
        for it in blk["items"]:
            # `cues` 만 보면 `@시각` 문단을 통째로 건너뛴다 — `run_script` 가 만드는
            # 대본은 기본이 `@시각` 이라 이 검사가 **기본 경로에서 안 돌았다.**
            # b-roll 판정만 span 을 배웠고 형제인 이 둘은 안 배운 채였다.
            if it["kind"] != "dialogue":
                continue
            if it["cues"]:
                span = it["cues"][-1]["end_s"] - it["cues"][0]["start_s"]
            elif it.get("span"):
                span = it["span"][1] - it["span"][0]
            else:
                continue
            if span > MAX_ITEM_S:
                errors.append("블록%d 문단 하나가 %.1f초를 덮습니다 (상한 %.0f초): %s"
                              % (bi + 1, span, MAX_ITEM_S, it["text"][:26]))
            for k in range(len(it["cues"]) - 1):
                g = it["cues"][k + 1]["start_s"] - it["cues"][k]["end_s"]
                if g > MAX_CUE_GAP_S:
                    errors.append(
                        "블록%d 문단 안 자막 사이가 %.1f초 벌어집니다 (상한 %.0f초): %s"
                        % (bi + 1, g, MAX_CUE_GAP_S, it["text"][:26]))

    # 블록 간 시각 역행 (첫 블록 = 훅이므로 제외)
    starts = []
    for blk in doc["blocks"]:
        ds = [i["cues"][0]["start_s"] for i in blk["items"]
              if i["kind"] == "dialogue" and i["cues"]]
        starts.append(min(ds) if ds else (blk["win"][0] if blk["win"] else None))
    for i in range(1, len(starts) - 1):
        if starts[i] is not None and starts[i + 1] is not None and starts[i + 1] < starts[i]:
            warns.append("블록%d(%s)가 블록%d(%s)보다 앞으로 돌아갑니다."
                         % (i + 2, _hms(starts[i + 1]), i + 1, _hms(starts[i])))

    import quality
    nars = script_io.narrations(doc)

    # 구성 — 나레이션(N)과 대사(D)가 블록마다 어떻게 놓였는지.
    # 정답: DDN ND DD NDDDN DN NN DD NDDDDD NDD NNDD NDDDDD NNDDDD NNDDNN DDD DDDD DD NDDDDDD DD
    shape, bare, run_max, n_blk = [], 0, 0, 0
    for blk in doc["blocks"]:
        s = "".join("N" if i["kind"] == "narration" else "D" for i in blk["items"])
        shape.append(s or "-")
        if not s:                      # 헤더만 남은 빈 블록은 비율에서 뺀다
            continue
        n_blk += 1
        if "N" not in s:
            bare += 1
        run_max = max([run_max] + [len(x) for x in s.split("D") if x])
    print("  구성: %s" % " ".join(shape))
    if nars:
        # 문장력보다 이 비율이 자연스러움을 좌우한다 (내 대본 실측 5.3 / 7.7).
        per = st["dialogue"] / len(nars)
        print("  대사 %.1f줄당 나레이션 1개  (내 대본 5~8줄)" % per)
        if per < DLG_PER_NARR_MIN:
            warns.append(
                "대사 %.1f줄마다 나레이션이 하나입니다 (내 대본은 5~8줄) — "
                "나레이션이 대사를 밀어냅니다. 대사가 스스로 말하는 장면에서 "
                "나레이션을 빼세요." % per)
    if n_blk:
        r = bare / n_blk
        print("  나레이션 없는 블록 %d/%d (%.0f%%) · 최대 연속 나레이션 %d줄"
              % (bare, n_blk, r * 100, run_max))
        if r < BARE_BLOCK_MIN:
            warns.append(
                "나레이션 없는 블록이 %.0f%%뿐입니다 (정답 33%%, 기준 %.0f%% 이상) — "
                "블록마다 나레이션을 달면 이야기가 아니라 '요약 + 인용문'으로 읽힙니다. "
                "대사만으로 통하는 블록에서는 나레이션을 빼세요."
                % (r * 100, BARE_BLOCK_MIN * 100))
    if run_max > NARR_RUN_MAX:
        warns.append("나레이션이 %d줄 연속됩니다 (정답 최대 %d줄) — 그 구간은 대사 없이 "
                     "설명만 흐릅니다." % (run_max, NARR_RUN_MAX))

    # 길이
    pl = layout.build(doc, cues, dur, fps=fps, log=lambda *_: None)
    warns.extend(pl["warnings"])
    total = pl["total_s"]
    ps = pl["stats"]

    # ── 나레이션 문체 — 내 대본 3편(33문장)이 기준이다 ─────────────────────
    # 임계값·근거는 quality.py 에. 정답 3편은 전부 통과하고 도구가 뽑은 대본은
    # 5개가 걸리도록 역산했다 (selftest [14] 가 그걸 고정한다).
    m = quality.narration_metrics([n["text"] for n in nars])
    if not m["n"]:
        # 문체 지표를 전부 `if nars:` 안에 두면 **나레이션을 안 쓰는 것으로
        # 전부 회피**된다. 실측: 나레이션을 지운 대본이 경고 3건으로 통과했다.
        errors.append("나레이션이 하나도 없습니다 — 대사 나열은 이야기가 아닙니다.")
    else:
        interval = pl["total_s"] / m["n"]
        print("  나레이션 %d개 · 중앙 %d자 (평균 %.0f · 최대 %d) · %.1f초에 하나"
              % (m["n"], m["median"], m["mean"], m["max"], interval))
        print("  종결: ~는데 %.0f%% · ~고 %.0f%% · 명사 %.0f%% · ~죠 %d개 · "
              "완결형 %.0f%% · 최빈 %s %.0f%%"
              % (100 * m["neunde"], 100 * m["conn"], 100 * m["nounend"],
                 m["jyo"], 100 * m["final_ratio"],
                 m["skew_kind"], 100 * m["skew"]))

        def _bad(msg):
            errors.append("문체 — " + msg)

        if not (quality.MEDIAN_LO <= m["median"] <= quality.MEDIAN_HI):
            _bad("나레이션 중앙 길이가 %d자입니다 (기준 %d~%d · 내 대본 13/15/18자)."
                 % (m["median"], quality.MEDIAN_LO, quality.MEDIAN_HI))
        if m["long_ratio"] > quality.LONG_RATIO_MAX:
            _bad("%d자 넘는 나레이션이 %.0f%%입니다 (기준 %.0f%% 이하 · 내 대본 10~25%%)."
                 % (quality.LONG_CHARS, 100 * m["long_ratio"],
                    100 * quality.LONG_RATIO_MAX))
            for t in m["over"][:3]:
                print("    %2d자  %s" % (len(t), t))
            if len(m["over"]) > 3:
                print("    … 외 %d개" % (len(m["over"]) - 3))
        if m["nounend"] > quality.NOUNEND_MAX:
            _bad("명사로 끝난 나레이션이 %.0f%%입니다 (기준 %.0f%% 이하 · 내 대본 21%%) — "
                 "`~는데`·`~고` 로 다음을 여세요."
                 % (100 * m["nounend"], 100 * quality.NOUNEND_MAX))
        if not (quality.INTERVAL_LO <= interval <= quality.INTERVAL_HI):
            _bad("나레이션이 %.1f초마다 하나입니다 (기준 %.0f~%.0f초 · 내 대본 9.3/14.1/15.0)."
                 % (interval, quality.INTERVAL_LO, quality.INTERVAL_HI))
        if m["jyo"] < 1:
            _bad("`~죠` 로 끝나는 나레이션이 없습니다 — 이야기가 뒤집히는 "
                 "**딱 한 자리**에 하나 넣으세요 (내 대본은 편당 정확히 1개).")
        if m["final_ratio"] > quality.FINAL_MAX:
            _bad("완결형(`~다.`)이 %.0f%%입니다 — 내 대본 33문장에 0개입니다."
                 % (100 * m["final_ratio"]))
        # 편중은 ✘ 가 아니라 ⚠ 다. 표본이 10~12문장이라 한 문장이 8~10%p 이고,
        # 정답 father 도 45%까지 간다 — 고치라는 게 아니라 알려 주는 값이다.
        if m["skew"] > quality.SKEW_MAX:
            warns.append(
                "나레이션이 %s 로만 %.0f%% 끝납니다 (기준 %.0f%% 이하 · "
                "내 대본 40/42/45%%) — 낭독이 단조로워집니다. 몇 개를 %s 로 바꾸세요."
                % (m["skew_kind"], 100 * m["skew"], 100 * quality.SKEW_MAX,
                   "`~는데`" if m["skew_kind"] != "~는데" else "`~고`"))
        for n in nars:
            if not (quality.ITEM_MIN <= len(n["text"]) <= quality.ITEM_MAX):
                warns.append("나레이션 길이 %d자: %s" % (len(n["text"]), n["text"][:30]))
        for name, n in _unknown_names(nars, cues):
            notes.append("'%s' 는 자막에 없는 이름입니다 (%d회 사용)." % (name, n))
        for name in _late_names(doc):
            notes.append("'%s' 는 대본 마지막 25%%에서 처음 등장합니다 — 앞에서 "
                         "역할이나 이름으로 소개했는지 확인하세요." % name)

    # ── 마지막 문단 — 나레이션으로 끝내지 않는다 ─────────────────────────
    # **길이 검사는 뺐다.** 붙였다가 되물렀다 — 근거가 오염돼 있었다.
    # 「도구 대본 22개가 19~44자」로 쟀는데, 그 숫자는 원문 문단을 그대로 센
    # 것이라 `@01:39:28.43~01:39:29.93 ` 접두어 25자가 섞여 있었다. 파싱된
    # 텍스트로 다시 재니 갈리지 않는다:
    #   유저 완성본 3편  2 · 3 · 4자
    #   유저 레퍼런스    19자   ← 상한 14면 **유저분 글이 떨어진다**
    #   도구 A · B       3 · 3자 ← 도구가 오히려 통과한다
    # 정답이 떨어지고 도구가 통과하면 게이트가 아니라 함정이다.
    #
    # 남긴 것은 종류 검사뿐이다. 유저 완성본 3편 · 레퍼런스 · 도구 24개 전부
    # 대사로 끝났다 — 네거티브 표본이 없다는 뜻이라 이건 **안 울리는 검사**다.
    # AGENTS.md 가 명시한 규칙(「나레이션은 이음매지 마침표가 아니다」)을
    # 코드에 박아 두는 값으로만 둔다.
    _blocks = [b for b in doc["blocks"] if b["items"]]
    if _blocks and _blocks[-1]["items"][-1]["kind"] == "narration":
        errors.append(
            "대본이 나레이션으로 끝납니다 — 정답 3편은 전부 짧은 대사로 "
            "끝냅니다. 나레이션은 이음매지 마침표가 아닙니다: %r"
            % _blocks[-1]["items"][-1]["text"][:30])

    # ── 대사 번역 문체 — **번역 경로에서만** ─────────────────────────────
    # 한국어 자막이면 대사가 자막 원문이라(규칙 6) 사람이 못 고친다. 거기서 켜면
    # 고칠 수 없는 ✘ 만 쌓인다. 재는 대상도 `authored_lines` 로 한 번 더 좁힌다 —
    # `@시각`만 붙은 frozen 문단은 자막 원문을 그대로 옮긴 것이다.
    if not ko:
        _wl = [ln for it in dlg for ln in (script_io.authored_lines(it) or [])]
        dm = quality.dialogue_metrics(_wl)
        if dm["n"]:
            de, dw, dn = quality.dialogue_issues(dm)
            errors += de
            warns += dw
            notes += dn
            for t in dm["foreign"][:3]:
                print("    ✘ 한국어 아님  %s" % t[:40])
            for t in dm["over"][:3]:
                print("    %2d자  %s" % (len(t), t[:40]))

    print("\n타임라인")
    print("  컷 %d · 자막 %d · 음소거 구간 %d · 이어붙임 %d"
          % (ps["cuts"], ps["subs"], ps["muted"], ps["merged"]))
    print("  총 길이 %.1f초 (%.1f분)   목표 %.0f초" % (total, total / 60, a.seconds))

    delta = total - a.seconds
    if abs(delta) > a.seconds * LEN_TOL:
        n_item = _avg_item_s(doc, pl)
        n_nar = sum(len(n["text"]) for n in nars) / max(1, len(nars))
        how = ("대사 문단 약 %d개" % round(abs(delta) / max(0.5, n_item))
               if n_item else "")
        how2 = "나레이션 약 %d줄" % round(abs(delta) / max(0.5, n_nar * 0.0930))
        warns.append("목표에서 %+.0f초 벗어납니다 — %s 또는 %s을 %s."
                     % (delta, how, how2, "줄이세요" if delta > 0 else "늘리세요"))

    # 선택 범위 — 영화의 어디를 얼마나 썼는가.
    # 결말까지 갔는지 본다. 정답 3편 중 둘이 여기 걸리는 것은 **정상**이다 —
    # 그 셋은 영화 한 대목만 다룬 옛 숏츠고, 지금 도구는 전체 요약을 만든다.
    times = []
    for blk in doc["blocks"]:
        for it in blk["items"]:
            if it["kind"] != "dialogue":
                continue
            if it.get("span"):
                times.append(it["span"][0])
            elif it.get("cues"):
                times.append(it["cues"][0]["start_s"])
    sel = quality.selection_metrics(times, dur)
    sw, sn = quality.selection_issues(sel)
    warns += sw
    notes += sn
    if sel.get("max_jump_s"):
        notes.append("나레이션 한 문장이 건너뛰는 영화 시간 최대 %.0f초"
                     % sel["max_jump_s"])
    if dur and cues:
        ratio = cues[-1]["end_s"] / dur
        if not (0.90 <= ratio <= 1.04):
            warns.append("자막 끝/영화 길이 = %.3f — 자막과 영상 릴리즈가 다를 수 있습니다."
                         % ratio)

    print("")
    for m in notes:
        print("  · %s" % m)
    for m in warns:
        print("  ⚠ %s" % m)
    for m in errors:
        print("  ✘ %s" % m)
    print("")
    if errors:
        print("실패 — 오류 %d건, 경고 %d건" % (len(errors), len(warns)))
        return 1
    print("통과 — 경고 %d건, 참고 %d건" % (len(warns), len(notes)))
    return 0


def _avg_item_s(doc, pl):
    """대사 문단 하나가 평균 몇 초인가. 목표 길이 경고가 "대사 문단 약 N개" 를
    말하는 데 쓴다.

    `cues` 만 세면 `@시각` 대본에서 n=0 → 0.0 이 되고, 그러면 경고가
    `목표에서 -227초 벗어납니다 —  또는 나레이션 약 147줄을 늘리세요` 로
    **목적어 없이** 나갔다. `run_script` 의 기본 출력이 `@시각` 이라 그게 기본
    경로였다. span 도 센다.
    """
    import script_io

    def counts(it):
        return it["kind"] == "dialogue" and (it["cues"] or it.get("span"))

    n = sum(1 for _, it in script_io.items(doc) if counts(it))
    if not n:
        return 0.0
    tot = sum(u["src1"] - u["src0"] for u in pl["units"] if counts(u["item"]))
    return tot / n


# 주격·목적격 조사가 붙은 말만 본다. 그래야 '페이는'·'663을'은 잡고
# '몰래'·'결국' 같은 부사는 안 잡는다.
_NAME_PAT = re.compile(r"(?<![가-힣0-9])([가-힣]{2,4}|[0-9]{2,4})(은|는|이|가|을|를|의)(?![가-힣])")
# 용언 활용 어미로 끝나면 이름이 아니다 ('들어왔다가' → '들어왔다')
_VERBISH = ("다", "고", "서", "며", "면", "지", "네", "죠", "데", "게", "든", "니", "어", "아")


def _unknown_names(nars, cues):
    """나레이션에서 되풀이되는데 자막엔 없는 이름.
    (중경삼림 SRT에 '페이'는 0회지만 정답 대본은 5줄에서 쓴다 — 규칙 8)"""
    hay = " ".join(c["text"] for c in cues)
    count = {}
    for n in nars:
        for stem, _josa in set(_NAME_PAT.findall(n["text"])):
            if stem in hay or stem.endswith(_VERBISH):
                continue
            count[stem] = count.get(stem, 0) + 1
    return sorted(((k, v) for k, v in count.items() if v >= 2),
                  key=lambda kv: -kv[1])[:6]


# 주격 조사만 본다. `_unknown_names` 의 목적격까지 받으면 `티켓을` 같은 사물이 걸린다.
_SUBJ_PAT = re.compile(r"(?<![가-힣0-9])([가-힣]{2,4}|[0-9]{2,4})(은|는|이|가)(?![가-힣])")
# 주격 조사가 붙지만 이름이 아닌 것 — 실측 잡음. 참고 출력이라 목록으로 누른다.
_NOT_NAME = {"무언", "슬픔", "구미", "한계", "하나", "깨달", "멘탈", "참교육",
             "강도", "일진", "일진들", "해결사", "경찰", "사장", "주인공"}


def _late_names(doc):
    """나레이션에서 **처음** 등장하는 이름이 대본 마지막 25% 에서만 나오는 것.

    심사평이 반복해서 때린 자리다 — 「대본 전체에 한 번도 나온 적 없는 로이스가
    결말에서 소개 없이 계약을 막는다」. 실측으로 father 3판의 로이스 · robber 의
    테런스 · gentleman 의 코치가 잡힌다. `_unknown_names` 처럼 자막에 있는 어간만
    남기면 안 된다 — 번역 대본은 자막이 영어라 이름이 한글로 안 나온다.
    대사에서 먼저 불린 이름은 뺀다(그건 대사가 소개한 것이다). **참고**다 — 오탐
    표본이 없어 게이트로 안 올린다.
    """
    import script_io
    items = [it for _, it in script_io.items(doc)]
    n = len(items)
    if n < 4:
        return []
    first, dlg = {}, {}
    for i, it in enumerate(items):
        for stem, _ in _SUBJ_PAT.findall(it["text"]):
            if stem.endswith(_VERBISH) or stem in _NOT_NAME:
                continue
            (dlg if it["kind"] == "dialogue" else first).setdefault(stem, i)
    return [s for s, i in first.items()
            if i >= n * 0.75 and dlg.get(s, n) >= i]


def _requoted(doc):
    """같은 큐(또는 같은 `@시각` 구간)를 인용한 대사 문단이 둘 이상인 것. → 대사 앞부분 목록."""
    import script_io
    seen, out = {}, []
    for _, it in script_io.items(doc):
        if it["kind"] != "dialogue":
            continue
        if it.get("cues"):
            key = tuple(c["src_no"] for c in it["cues"])
        elif it.get("span"):
            key = ("@", round(it["span"][0], 1))
        else:
            continue
        if key in seen:
            out.append(it["text"][:14])
        seen[key] = True
    return out


def _hms(sec):
    sec = max(0, int(sec or 0))
    return "%d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


if __name__ == "__main__":
    sys.exit(main())
