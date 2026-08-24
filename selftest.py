"""
selftest.py — 정답 샘플로 고정한 회귀 테스트.

    python selftest.py

계획서 §10의 검증 1~7을 자동화한다. 실물 CapCut 확인(9·11~14)은 사람이 한다.
테스트 자료가 없으면(다른 PC) 조용히 건너뛴다.
"""

import inspect
import json
import math
import statistics
import pathlib
import re
import sys
import shutil
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent
TEST = ROOT / "test"
SRT = TEST / "중경삼림 리마스터링_자막.srt"
# 정본 샘플. 이름에 _레퍼런스 를 붙여 대본 생성 출력 경로와 겹치지 않게 했다.
REF = TEST / "중경삼림 리마스터링_타임라인과 자막_레퍼런스.txt"

_fail = []
_pass = 0


def _test_fingerprint():
    """test/ 는 **읽기 전용 자료**다. 결과물은 results/ 로 간다.
    예전엔 대본을 자막 파일 옆에 떨어뜨려서 test/ 에 산출물이 쌓였다."""
    if not TEST.exists():
        return {}
    return {f.name: (f.stat().st_size, f.stat().st_mtime_ns)
            for f in sorted(TEST.iterdir()) if f.is_file()}


_TEST_BEFORE = _test_fingerprint()


def check(name, cond, detail=""):
    global _pass
    if cond:
        _pass += 1
        print("  ✓ %s" % name)
    else:
        _fail.append(name)
        print("  ✗ %s   %s" % (name, detail))


def eq(name, got, want):
    check(name, got == want, "got %r, want %r" % (got, want))


def near(name, got, want, tol):
    check(name, abs(got - want) <= tol, "got %.3f, want %.3f ±%.3f" % (got, want, tol))


def _style_gate(nars, dur):
    """quality 임계값을 적용해 걸린 항목 이름을 돌려준다."""
    import quality as q
    m = q.narration_metrics(nars)
    bad = []
    if not m["n"]:
        return ["나레이션0"]
    if not (q.MEDIAN_LO <= m["median"] <= q.MEDIAN_HI):
        bad.append("중앙")
    if m["long_ratio"] > q.LONG_RATIO_MAX:
        bad.append("21자초과")
    if m["nounend"] > q.NOUNEND_MAX:
        bad.append("명사")
    if not (q.INTERVAL_LO <= dur / m["n"] <= q.INTERVAL_HI):
        bad.append("간격")
    if m["jyo"] < 1:
        bad.append("~죠")
    if m["final_ratio"] > q.FINAL_MAX:
        bad.append("완결형")
    return bad


def style_tests():
    """문체 계측 — **테스트 자산이 없어도 돈다.** 받아 쓴 사람 PC 에서도 회귀가 필요하다."""
    import json
    import quality as q
    print()
    print("[14] 문체 계측 (quality.py)")
    gp = ROOT / "fixtures" / "narration_gold.json"
    if not gp.exists():
        check("정답 픽스처가 있다", False, str(gp))
        return
    gold = {k: v for k, v in
            json.loads(gp.read_text(encoding="utf-8")).items()
            if not k.startswith("_")}

    # ① 골든 — 정답 3편이 임계값을 **전부 통과**해야 한다.
    #    하나라도 걸리면 임계값이 틀린 것이지 대본이 틀린 게 아니다.
    for name, v in sorted(gold.items()):
        m = q.narration_metrics(v["narrations"])
        for k, want in v["expect"].items():
            got = m[k] if k != "interval" else v["duration_s"] / m["n"]
            if isinstance(want, float):      # 비율은 픽스처와 같은 자리에서 비교
                got = round(got, 1 if k == "interval" else 3)
            eq("%s %s" % (name, k), got, want)
        eq("%s 문체 통과" % name, _style_gate(v["narrations"], v["duration_s"]), [])

    # ② 네거티브 — 도구가 실제로 뽑은 대본은 **걸려야** 한다.
    #    이게 없으면 아무 값이나 넣어도 통과하는 임계값을 만들게 된다.
    tool = ["좋아하는 남자의 집에 몰래 들어왔다가 딱 걸려버린 여자",
            "얼마 전, 663의 연인은 이별 편지를 남겼는데",
            "그 편지 안에는 그의 집 열쇠와",
            "그 말에 페이는 열쇠를 들고 그의 집으로 향했고",
            "그렇게 남몰래 그의 집을 조금씩 바꾸는 동안",
            "정작 663은 누군가 다녀간 흔적을 보면서도",
            "위기를 넘긴 뒤 그의 곁에서 잠들어버린 페이",
            "하지만 약속 시간이 지나도록 페이는 오지 않았고",
            "그녀가 남긴 것은 1년 뒤로 향하는 티켓 한 장",
            "그리고 정확히 1년 뒤"]
    hit = _style_gate(tool, 180.4)
    check("도구 출력은 걸린다", len(hit) >= 4, str(hit))
    for k in ("중앙", "명사", "~죠"):
        check("도구 출력이 '%s' 로 걸린다" % k, k in hit, str(hit))

    # ③ 나레이션을 지워서 회피할 수 없다 — 간격이 무한대가 된다
    eq("나레이션 0개는 걸린다", _style_gate([], 180.0), ["나레이션0"])
    eq("나레이션 2개만 남겨도 걸린다",
       "간격" in _style_gate(tool[:2], 180.4), True)

    # ④ 짧은 나레이션은 문제가 아니다 — father 는 15자 미만이 절반이 넘는다.
    #    옛 SHORT_RATIO_MAX=0.40 은 **정답 본인을 걸었다**. 회귀로 못 박는다
    f = gold["father"]["narrations"]
    short = sum(1 for t in f if len(t) < 15) / len(f)
    check("father 는 짧은 줄이 절반 넘는다", short > 0.5, "%.0f%%" % (100 * short))
    eq("그래도 통과한다", _style_gate(f, gold["father"]["duration_s"]), [])

    # ⑤ 종결 분류가 정답 분포를 재현하는가
    allnar = [t for v in gold.values() for t in v["narrations"]]
    m = q.narration_metrics(allnar)
    eq("정답 33문장", m["n"], 33)
    eq("완결형 0개", m["final_ratio"], 0.0)
    check("~는데 가 가장 많다", m["neunde"] >= 0.30, "%.0f%%" % (100 * m["neunde"]))
    check("명사 마무리는 다섯에 하나", 0.15 <= m["nounend"] <= 0.30,
          "%.0f%%" % (100 * m["nounend"]))
    eq("~죠 는 편당 하나 = 3개", m["jyo"], 3)

    # ⑥ 중경삼림 레퍼런스는 **걸리는 게 정상이다** — 문예물이고 이 도구의
    #    문체 기준은 숏츠다. 기대값을 박아 두면 "정본이 걸린다"가 버그가
    #    아니라 의도임이 코드에 남는다.
    _ref = ["좋아하는 남자의 집에 몰래 들어왔다가 딱 걸려버린 여자",
            "사실 얼마 전, 663의 전 여자친구는 그에게 편지 한 통을 남겼는데",
            "그런데 그 편지 안에는", "그의 집 열쇠가 들어 있었고",
            "663을 몰래 좋아하던 페이는 결국 그 열쇠를 들고 그의 집에 들어가기 시작하죠"]
    _hit = _style_gate(_ref, 168.5)
    check("중경삼림 예시는 문체 기준에 걸린다 (의도)", len(_hit) >= 2, str(_hit))
    check("걸리는 이유는 길이다", "중앙" in _hit and "21자초과" in _hit, str(_hit))
    # 그런데 종결·~죠 는 정답과 같다 — 문예물이어도 문장을 안 닫는 건 같다
    # ④ 나레이션 조각내기 — 화면에 어떻게 나뉘는지. 정답 33문장 전수 실측.
    import collections
    import wrap
    _P = [wrap.split_narration(t) for name, v in sorted(gold.items())
          for t in v["narrations"]]
    _cnt = collections.Counter(len(x) for x in _P)
    _pl = [len(y) for x in _P for y in x]
    eq("1조각 문장 11개", _cnt[1], 11)          # 33문장 중 13자 이하가 정확히 11개
    eq("4조각 문장 1개", _cnt[4], 1)
    _avg = sum(len(x) for x in _P) / len(_P)     # 실측 1.70
    check("문장당 조각 1.6~1.9", 1.6 <= _avg <= 1.9, "%.2f" % _avg)
    eq("15자 이상 조각 0", sum(1 for x in _pl if x > wrap.NARR_LINE), 0)
    eq("5자 미만 조각 0", sum(1 for x in _pl if x < 5), 0)
    check("첫 조각 최빈 8자",
          collections.Counter(len(x[0]) for x in _P).most_common(1)[0][0] == 8, "")
    check("나머지 조각 중앙 8자",
          statistics.median([len(y) for x in _P for y in x[1:]]) == 8, "")
    # 유저분이 짚어 준 4문장 — **글자까지** 같아야 한다. 가장 강한 자물쇠다.
    for _t, _want in (
            ("갑작스런 비보를 받게 된 할", ["갑작스런 비보를", "받게 된 할"]),
            ("하지만 맛탱이가 완전히 가버렸고", ["하지만 맛탱이가", "완전히 가버렸고"]),
            ("말콤은 한 소리 하려했는데", ["말콤은 한 소리", "하려했는데"]),
            ("보다못한 말콤은 궁극기를 시전하기로 했고",
             ["보다못한 말콤은", "궁극기를 시전하기로 했고"])):
        eq("실측 그대로: %s" % _t[:12], wrap.split_narration(_t), _want)
    # 네거티브 — 안 자르는 구현으로도 통과하면 안 된다
    check("통째로는 14자를 넘는 문장이 스물 이상",
          sum(1 for name, v in gold.items() for t in v["narrations"]
              if len(t) > wrap.NARR_LINE) >= 20, "")
    eq("조각내기는 기본으로 켜져 있다", wrap.NARR_SPLIT, True)
    # 무성 조각(문장부호만)은 **홀로 남으면 안 된다** — 발화 가중치가 0이라
    # 시각이 이웃과 겹치고 `layout._stack` 이 그 자막을 통째로 버린다.
    # 예전엔 `if merged and …` 라 **첫 조각이 예외**였다.
    import timing as _tm
    for _t in (".......... 그때 미키가 참교육을 시작했는데",
               "?!?!?!?!?! 일진들이 냅다 도망쳤고",
               "그때 미키가 나타났는데 ..........",
               "결국 아비는 떠나버렸는데 ?!?!?!?!?!"):
        _ps = wrap.split_narration(_t)
        check("무성 조각이 홀로 안 남는다: %s" % _t[:14],
              not any(_tm.is_silent(x) for x in _ps), str(_ps))

    _rm = q.narration_metrics(_ref)
    eq("중경삼림도 완결형 0", _rm["final_ratio"], 0.0)
    eq("중경삼림도 ~죠 1개", _rm["jyo"], 1)

    # ④ 종결어미 편중 — `~고` 하나만 재면 안 갈린다. 정답 father 가 45%로
    #    도구 출력(43%)보다 높다. 그래서 **최빈 어미**에 건다.
    _skew = {k: v["expect"]["skew"] for k, v in gold.items()}
    check("정답 3편은 편중 상한을 넘지 않는다",
          all(x <= q.SKEW_MAX for x in _skew.values()),
          str({k: round(v, 2) for k, v in _skew.items()}))
    check("여유가 한 문장보다 좁다는 것을 알고 있다",
          max(_skew.values()) > q.SKEW_MAX - 0.10,
          "정답 최대 %.2f · 상한 %.2f" % (max(_skew.values()), q.SKEW_MAX))
    # 네거티브 — 실제로 뽑힌 편중 대본(~고 7 / 12). 이게 없으면 상한을
    # 아무 값이나 넣어도 테스트가 통과한다.
    _lean = ["점심을 먹으러 돌아온 남자", "시작은 이별 편지 한 통",
             "그런데 편지 안에는", "그의 집 열쇠가 있었고",
             "아비는 그의 근무일까지 알아냈고", "그가 집 안의 물건들을 위로할 때",
             "아비는 그 빈집으로 향했고", "두 사람은 밖에서 계속 스쳐 갔고",
             "초대보다 먼저 집에 들어갔죠", "하지만 꿈은 끝나지 않았고",
             "이번엔 비까지 핑계 삼아 돌아갔고", "그렇게 그의 집은 조금씩 달라졌는데"]
    _lm = q.narration_metrics(_lean)
    eq("편중 대본의 최빈 어미는 ~고", _lm["skew_kind"], "~고")
    check("편중 대본은 상한에 걸린다", _lm["skew"] > q.SKEW_MAX,
          "%.2f > %.2f" % (_lm["skew"], q.SKEW_MAX))
    # 편중 말고는 멀쩡하다 — 그래서 **다른 지표로는 못 잡는다.** 이 검사가 필요한 이유.
    check("편중 대본은 다른 문체 지표는 통과한다",
          not _style_gate(_lean, 181.8), str(_style_gate(_lean, 181.8)))


def ending_tests():
    """결말 검사 — **테스트 자산이 없어도 돈다.** 픽스처가 대조 결과를 갖고 있다."""
    import json
    import quality as q
    print()
    print("[16] 결말 검사 (quality.selection_*)")
    gp = ROOT / "fixtures" / "selection_gold.json"
    if not gp.exists():
        check("범위 픽스처가 있다", False, str(gp))
        return
    raw = json.loads(gp.read_text(encoding="utf-8"))
    gold = {k: v for k, v in raw.items() if not k.startswith("_")}

    # 정답 3편 중 둘이 **걸리는 게 정상**이다. 그 셋은 영화 한 대목만 다룬 옛
    # 숏츠(55% · 25% 에서 끝)고, 지금 도구는 결말이 있는 전체 요약을 만든다.
    # 이걸 테스트가 명시적으로 인정하는 편이 낫다 — 나중에 "정답이 걸리네"
    # 하고 임계를 낮추면 도구가 결말을 빼먹어도 아무도 모르게 된다.
    for name, v in sorted(gold.items()):
        times = [b[2] for b in v["blocks"]] + [v["blocks"][-1][3]]
        m = q.selection_metrics(times, v["movie_s"])
        e = v["expect"]
        near("%s 쓴 구간(분)" % name, m["span_min"], e["span_min"], 0.15)
        near("%s 시작 지점(%%)" % name, m["start_pct"], e["start_pct"], 1.0)
        near("%s 끝 지점(%%)" % name, m["end_pct"], e["end_pct"], 1.0)
        w, n = q.selection_issues(m)
        eq("%s 결말 경고" % name, len(w), e["ending_warns"])
        if w:
            check("경고가 결말 이야기다", "결말" in w[0], w[0])
        check("%s 범위를 참고로 찍는다" % name, any("쓴 구간" in x for x in n), str(n))

    # 결말까지 간 대본은 통과해야 한다. 이게 없으면 임계를 아무 값이나 넣게 된다.
    t = raw["_도구출력"]
    w, _ = q.selection_issues({"span_min": t["span_min"], "start_pct": t["start_pct"],
                               "end_pct": t["end_pct"]})
    eq("결말까지 간 대본은 통과", len(w), t["expect"]["ending_warns"])

    # 폭은 이제 안 잰다 — 전체 요약이면 영화 전체를 훑는 게 정상이다.
    check("폭 상한은 없다", not hasattr(q, "SPAN_MAX_MIN"))
    w, _ = q.selection_issues({"span_min": 98.2, "start_pct": 2, "end_pct": 100})
    eq("98분을 훑어도 결말이 있으면 통과", len(w), 0)

    # 시각이 하나도 없으면 아무 말도 안 한다 (자막 없는 대본).
    eq("시각이 없으면 판정을 안 한다", q.selection_metrics([], 6000), {})
    eq("빈 지표는 경고도 참고도 없다", q.selection_issues({}), ([], []))


_DLG_CODE = re.compile(r"^(\d+)([?!.]?)(,?)$")


def _synth(code):
    """`11?` → 11자짜리 합성 줄. **픽스처에 영화 대사를 안 싣기 위한 장치다.**

    길이·끝부호·쉼표만 남긴 코드라 원문은 복원되지 않는다. 그래도
    `quality.dialogue_metrics` 가 재는 것은 전부 재현된다 — 그 함수가 보는 게
    길이·끝부호·쉼표·문자 종류뿐이고 어휘는 애초에 안 재기 때문이다.
    """
    m = _DLG_CODE.match(code)
    n, end, com = int(m.group(1)), m.group(2), m.group(3)
    body = "가" * max(1, n - len(end))
    if com:
        body = body[:-1] + ","          # 쉼표는 끝부호 앞자리에
    return (body + end)[:n]


def dialogue_tests():
    """대사 번역 계측 — **테스트 자산이 없어도 돈다.**"""
    import json
    import quality as q
    print()
    print("[17] 대사 번역 계측 (quality.dialogue_*)")
    gp = ROOT / "fixtures" / "dialogue_gold.json"
    if not gp.exists():
        check("대사 픽스처가 있다", False, str(gp))
        return
    gold = {k: v for k, v in json.loads(gp.read_text(encoding="utf-8")).items()
            if not k.startswith("_")}

    # ① 골든 — 정답 3편이 **전부 통과**해야 한다. 하나라도 걸리면 임계가 틀린 것이다.
    allc = []
    for name, v in sorted(gold.items()):
        lines = [_synth(c) for c in v["codes"]]
        allc += lines
        m = q.dialogue_metrics(lines)
        e = v["expect"]
        for k in ("n", "median", "max"):
            eq("%s %s" % (name, k), m[k], e[k])
        near("%s ?!끝 비율" % name, m["mark"], e["mark"], 0.002)
        near("%s 쉼표 비율" % name, m["comma"], e["comma"], 0.002)
        er, wr, _ = q.dialogue_issues(m)
        eq("%s 는 대사 규범을 통과한다" % name, (len(er), len(wr)), (0, 0))
    m_all = q.dialogue_metrics(allc)
    eq("합계 248줄", m_all["n"], 248)
    eq("20자 이상 0줄", sum(1 for t in allc if len(t) >= 20), 0)
    eq("합계도 통과", tuple(len(x) for x in q.dialogue_issues(m_all)[:2]), (0, 0))

    # ② 네거티브 A — 번역이 안 남은 줄. 정답 0건 · 원본 영어 6,249줄 전수 적중.
    _en = q.dialogue_metrics(["This is an english line.", "I go."])
    eq("영어가 남으면 ✘", len(q.dialogue_issues(_en)[0]), 1)
    eq("두 줄 다 잡힌다", len(_en["foreign"]), 2)
    # 오탐 — **제대로 옮긴 줄을 걸면 안 된다.** 예전 조건(라틴 4글자 연속)이
    # 브랜드·지명·작품명을 잡았고, 이건 유일한 ✘ 라 exit 1 이었다. 사람이 고칠
    # 방법이 번역을 망가뜨리는 것뿐이었다.
    eq("브랜드·지명·약어는 안 걸린다",
       q.dialogue_metrics(["OK 알겠어", "FBI가 왔다", "TV 봤어",
                           "Marlboro 한 갑 줘", "California 로 갔어",
                           "Xbox 사줄게", "Star Trek 봤어"])["foreign"], [])
    # 자모 모음도 한국어다 — `ㅋㅋ` 는 통과하는데 `ㅠㅠ` 는 ✘ 였다
    eq("자모 모음도 한국어", q.dialogue_metrics(["ㅋㅋㅋ", "ㅠㅠ", "ㅡㅡ"])["foreign"], [])
    # 빈 줄이 `?!` 로 끝난 줄로 세이던 것 (`"" in "?!"` 는 True 다)
    eq("빈 줄은 ?! 끝으로 안 센다",
       q.dialogue_metrics(["", "", "", "안녕"])["mark"], 0.0)
    # 동률에서 실행마다 달라지던 것 — 정렬로 고정
    eq("최빈 어미가 동률에서 고정된다",
       q.narration_metrics(["그때 갔는데", "결국 왔고"])["skew_kind"], "~고")

    # ③ 네거티브 B — **나레이션 문체를 대사에 쓰면 걸린다.**
    #    두 규범이 실제로 갈린다는 것을 코드가 증명한다. 「14자 스코프」의 자물쇠다.
    ng = ROOT / "fixtures" / "narration_gold.json"
    if ng.exists():
        _nars = [t for k, v in json.loads(ng.read_text(encoding="utf-8")).items()
                 if not k.startswith("_") for t in v["narrations"]]
        _nm = q.dialogue_metrics(_nars)
        _ne, _nw, _ = q.dialogue_issues(_nm)
        check("나레이션 33문장은 대사 규범에 걸린다 (중앙 16자 · ?! 0%)",
              len(_nw) >= 2 and _nm["median"] > q.DLG_MEDIAN_MAX
              and _nm["mark"] < q.DLG_MARK_MIN,
              "중앙 %d · ?! %.0f%% · ⚠%d" % (_nm["median"], 100 * _nm["mark"], len(_nw)))
        eq("그래도 영어는 아니다", len(_ne), 0)

    # ④ 경로 게이트 — 한국어 자막이면 아예 안 켜진다
    import cli
    _cc = inspect.getsource(cli.cmd_check)
    check("대사 판정은 `if not ko:` 안에 있다",
          "dialogue_metrics" in _cc
          and _cc.index("if not ko:") < _cc.index("quality.dialogue_metrics"))
    check("frozen 문단은 authored_lines 가 뺀다",
          "authored_lines" in _cc and "screen_lines" not in _cc)
    # `@시각` 대본은 `run_script` 의 기본 출력이다. `cues` 만 보는 검사가 남아
    # 있으면 **기본 경로에서 안 도는 검사**가 된다. 실측: 목표 길이 경고가
    # `— <빈칸> 또는 나레이션 약 147줄` 로 목적어 없이 나갔고, 문단 20초·큐
    # 간격 3초 상한은 통째로 건너뛰었다.
    check("문단 시간 상한이 span 도 본다",
          'elif it.get("span"):' in _cc, "cli.cmd_check")
    _ai = inspect.getsource(cli._avg_item_s)
    check("_avg_item_s 도 span 을 센다", 'it.get("span")' in _ai, _ai[:80])

    # ⑤ 프롬프트 — 실제 **출력**을 본다. 소스를 보면 주석까지 걸린다.
    import script_gen as _sg
    _was = _sg._srt_is_korean
    try:
        _sg._srt_is_korean = lambda _p: False        # 외국어 자막인 척
        _pe = _sg.build_prompt("x.srt", "o.txt", 180)
    finally:
        _sg._srt_is_korean = _was
    for _k in ("중앙 10자", "20자 넘는 줄이 0줄", "셋에 하나가", "통째로 버려라",
               "14자 상한은 나레이션 규칙", "쉼표 자리면 줄을 끊어라"):
        check("번역 지침에 '%s'" % _k, _k in _pe)
    # 예전 예시 3줄은 쉼표 100%·평균 13.7자로 실측(0.4%·10자)의 정반대를 가르쳤다.
    # 에이전트는 표의 숫자가 아니라 눈앞의 예시를 베낀다 — 되돌아오면 안 된다.
    check("지어낸 대사 예시가 안 돌아왔다", "대박, 저 귀걸이" not in _pe)
    _ex = [l for l in _pe.splitlines() if l.strip().startswith("#")]
    check("`#` 예시 줄에 쉼표가 없다",
          _ex and not any("," in l for l in _ex), str(_ex))

    # ⑥ 문서 스코프 — style_example 이 나레이션 규칙임을 밝히고 §10 이 있을 것
    _st = (ROOT / "template" / "style_example.txt").read_text(encoding="utf-8")
    check("14자 상한에 나레이션 스코프가 붙어 있다",
          "나레이션" in [l for l in _st.splitlines()
                        if "14자를 절대 안 넘는다" in l][0])
    check("§10 대사 절이 있다", "## 10. 대사" in _st)
    check("§10 은 예시 문장을 안 싣는다고 밝힌다", "예시가 없는 건" in _st)
    for _k in ("중앙 10자", "19자까지", "248줄에 **1개**"):
        check("§10 에 '%s'" % _k, _k in _st)


def main():
    # 픽스처만 쓰는 회귀는 **test/ 없이도** 돈다 — 두 함수의 독스트링이 그렇게
    # 적혀 있는데도 예전엔 아래 조기 return 뒤에 있어서, 받아 쓴 사람 PC 에서는
    # 문체·결말 임계값이 통째로 안 돌았다. 조용히 죽은 검사가 제일 위험하다.
    style_tests()
    ending_tests()
    dialogue_tests()
    if not SRT.exists() or not REF.exists():
        print("\n테스트 자료가 없습니다 (%s). 나머지는 건너뜁니다." % TEST)
        print("\n" + "=" * 60)
        if _fail:
            print("실패 %d건: %s" % (len(_fail), " / ".join(_fail)))
            return 1
        print("통과 %d건 — 픽스처 회귀만 돌았습니다" % _pass)
        return 0

    import subtitle
    import script_io

    cues, meta = subtitle.parse_file(SRT)
    ref = REF.read_bytes().decode("utf-8-sig")

    print("\n[0] subtitle.py")
    eq("큐 수 890 (오탐 삭제되던 대사 포함)", meta["cue_count"], 890)
    eq("music 오탐 0건", meta["dropped"]["music"], 0)
    eq("2줄 이상 큐 212개", sum(1 for c in cues if len(c["lines"]) > 1), 212)
    check("lines 불변식", all(" ".join(c["lines"]) == c["text"] for c in cues))
    c752 = next(c for c in cues if c["src_no"] == 752)
    eq("SRT#752 = '우리 집에서 뭐 해요?'", c752["text"], "우리 집에서 뭐 해요?")
    check("src_no != idx 인 큐가 실재 (재번호 함정)",
          any(c["src_no"] != c["idx"] for c in cues))

    print("\n[3] 매칭 — 정답 48문단")
    doc = script_io.read(ref, cues)
    st = doc["stats"]
    eq("대사 문단 48개", st["dialogue"], 48)
    eq("서명 일치 45", st["by_sig"], 45)
    eq("2큐 병합 3", st["by_merge"], 3)
    eq("강등 0건", st["demoted"], 0)
    eq("블록 18개", len(doc["blocks"]), 18)
    eq("제목", doc["title"], "좋아한다는 말 대신 몰래 그의 삶에 들어간 여자")
    eq("나레이션 19줄", len(script_io.narrations(doc)), 19)

    # 블록4: '"자기 좌석은 취소됐어"' 가 SRT#465·#467 두 곳에 있다.
    blk4 = doc["blocks"][3]
    dup_item = [it for it in blk4["items"]
                if it["kind"] == "dialogue" and "자기 좌석" in it["text"]][0]
    eq("동점 해소 — 앞선 SRT#465 선택", dup_item["cues"][0]["src_no"], 465)

    print("\n[1] 고정점 — write(read(x)) 가 두 번째부터 안 변할 것")
    w1 = script_io.write(doc)
    doc2 = script_io.read(w1, cues)
    w2 = script_io.write(doc2)
    check("w1 == w2 (바이트 동일)", w1 == w2,
          "len %d vs %d" % (len(w1), len(w2)))

    print("\n[2] 보존 — read(ref) 와 read(w1) 이 같을 것")
    eq("제목", doc2["title"], doc["title"])
    eq("블록 수", len(doc2["blocks"]), len(doc["blocks"]))
    eq("헤더 원문", [b["header"] for b in doc2["blocks"]],
       [b["header"] for b in doc["blocks"]])
    eq("아이템 kind 순서",
       [[i["kind"] for i in b["items"]] for b in doc2["blocks"]],
       [[i["kind"] for i in b["items"]] for b in doc["blocks"]])
    eq("나레이션 원문",
       [i["text"] for i in script_io.narrations(doc2)],
       [i["text"] for i in script_io.narrations(doc)])
    eq("매칭 큐",
       [[c["idx"] for c in i["cues"]] for _, i in script_io.items(doc2)],
       [[c["idx"] for c in i["cues"]] for _, i in script_io.items(doc)])
    two = [i["lines"] for _, i in script_io.items(doc)
           if i["kind"] == "dialogue" and len(i["lines"]) > 1]
    eq("2줄 문단 13개", len(two), 13)
    eq("2줄 문단 줄바꿈 보존",
       [i["lines"] for _, i in script_io.items(doc2)
        if i["kind"] == "dialogue" and len(i["lines"]) > 1], two)
    eq("쓰기 정규화 — 끝 개행 1개", w1.endswith("\n") and not w1.endswith("\n\n"), True)
    check("쓰기 정규화 — CR 없음", "\r" not in w1)

    print("\n[4] 동점 회귀 — 같은 대사가 잇달아 3번")
    run = None
    for i in range(len(cues) - 2):
        a, b, c = cues[i], cues[i + 1], cues[i + 2]
        if a["text"] == b["text"] == c["text"]:
            run = [a, b, c]
            break
    check("SRT에 3연속 동일 대사가 실재", run is not None)
    if run:
        print("     SRT#%s~#%s  %r" % (run[0]["src_no"], run[2]["src_no"], run[0]["text"]))
        synth = "제목\n\n%s\n\n%s\n" % (
            script_io.render_header(run[0]["start_s"], run[-1]["end_s"]),
            "\n\n".join(c["text"] for c in run))
        d = script_io.read(synth, cues)
        got = [it["cues"][0]["idx"] if it["cues"] else None
               for _, it in script_io.items(d) if it["kind"] == "dialogue"]
        eq("세 문단이 서로 다른 큐로 1:1", got, [c["idx"] for c in run])

        # 순서를 뒤집어도 매칭이 죽지 않을 것 (단조성은 선호이지 필터가 아니다)
        rev = "제목\n\n%s\n\n%s\n" % (
            script_io.render_header(run[0]["start_s"], run[-1]["end_s"]),
            "\n\n".join(c["text"] for c in reversed(run)))
        dr = script_io.read(rev, cues)
        eq("역순으로 써도 강등 0", dr["stats"]["demoted"], 0)

    print("\n[5] 레이아웃 — 정답 18블록")
    import layout
    doc = script_io.read(ref, cues)          # 매칭 상태를 새로 (앞 테스트가 doc을 씀)
    pl = layout.build(doc, cues, 6159.104, fps=24.0,
                      mute_under_narration=True)
    st = pl["stats"]
    near("총 길이 168.5초", pl["total_s"], 168.5, 0.05)
    eq("영상 구간 48개", st["cuts"], 48)
    eq("자막 107개 (대사 61 + 나레이션 45조각 + 제목 1)", st["subs"], 107)
    # 나레이션은 문장을 통째로 내지 않고 **조각으로 잘라 차례로** 띄운다.
    # 예전엔 화면 한 줄이 중앙 21자 · 최대 44자였고 열에 일곱이 14자를 넘었다.
    import wrap
    _nl = [len(l) for x in pl["subs"] if x["kind"] == "narration" for l in x["lines"]]
    eq("나레이션 조각 45개", len(_nl), 45)
    eq("14자 넘는 나레이션 줄 0", sum(1 for x in _nl if x > wrap.NARR_LINE), 0)
    check("나레이션 줄 중앙 8자", statistics.median(_nl) == 8, str(sorted(_nl)[:8]))
    # 조각이 1프레임 미만이면 `_stack` 이 조용히 버린다 — 실측 최소 0.48초
    _nd = [x["t_dur"] for x in pl["subs"] if x["kind"] == "narration"]
    check("조각이 1프레임보다 짧지 않다", min(_nd) >= 1.0 / 24, "%.3f초" % min(_nd))
    # 하한 가드가 **평균**을 보던 시절엔 통과한 문장 안에 하한보다 짧은 조각이
    # 생겼다(실측 평균 0.317초 통과 · 조각 0.25초). 개별 조각으로 못 박는다.
    _multi = {}
    for x in pl["subs"]:
        if x["kind"] == "narration":
            _multi.setdefault(x.get("bi"), []).append(x)
    check("모든 나레이션 조각이 하한을 지킨다",
          min(_nd) >= layout.NARR_MIN_PIECE_S - 1e-9,
          "최소 %.3f초 · 하한 %.2f초" % (min(_nd), layout.NARR_MIN_PIECE_S))
    # 조각내기를 꺼도 **자막 카드 수 말고는 아무것도 안 달라져야** 한다.
    # 이 한 쌍이 "렌더링만 바꿨다"를 실행 가능한 명제로 만들고, 되돌리기
    # 스위치가 실제로 도는지도 영원히 검증한다.
    _saved = wrap.NARR_SPLIT
    try:
        wrap.NARR_SPLIT = False
        _off = layout.build(doc, cues, 6159.104, fps=24.0,
                            mute_under_narration=True)
        eq("끄면 자막 81개 — 조각내기 이전과 같다", _off["stats"]["subs"], 81)
        eq("끄나 켜나 컷이 한 글자도 안 달라진다",
           [(round(x["src0"], 4), round(x["t_dur"], 4)) for x in pl["segments"]],
           [(round(x["src0"], 4), round(x["t_dur"], 4)) for x in _off["segments"]])
        eq("헤더도 불변", pl["headers"], _off["headers"])
        near("총 길이도 불변", _off["total_s"], pl["total_s"], 0.001)
    finally:
        wrap.NARR_SPLIT = _saved
    # 예전엔 **한 덩어리의 여러 줄이 같은 t_start** 를 받아서, `_stack` 이 길이를
    # 「다음 자막 시작까지」로 재는 순간 앞줄이 0초가 되어 버려졌다. 실측으로
    # 2줄짜리 자막 10개의 첫 줄이 10개 전부 사라졌다 — `내일 저녁 8시` 가 날아가
    # 약속 시각이 화면에 안 나왔다. 대본이 기대하는 줄이 **전부** 살아야 한다.
    _want = 0
    for _, _it in script_io.items(doc):
        if _it["kind"] != "dialogue":
            continue
        _tr = script_io.screen_lines(_it)
        _want += len(_tr) if _tr else sum(len(c["lines"]) for c in _it["cues"])
    eq("대사 줄이 하나도 안 사라진다",
       sum(1 for x in pl["subs"] if x["kind"] == "dialogue"), _want)
    _multi = [c for _, i2 in script_io.items(doc) if i2["kind"] == "dialogue"
              for c in i2["cues"] if len(c["lines"]) > 1]
    _shown = {x["lines"][0] for x in pl["subs"] if x["kind"] == "dialogue"}
    check("2줄 큐의 첫 줄도 화면에 있다 (실측 10개)",
          _multi and all(c["lines"][0] in _shown for c in _multi),
          "%d개 중 %d개 사라짐"
          % (len(_multi), sum(1 for c in _multi if c["lines"][0] not in _shown)))
    # 대사에는 시작과 끝이 있다. 끝났는데도 남겨 두면 같은 글자가 뒤에 또 나온다.
    _seen = {}
    for x in pl["subs"]:
        _k = (tuple(x["lines"]), x["bi"], x["kind"])
        _seen[_k] = _seen.get(_k, 0) + 1
    eq("같은 자막이 두 번 나오지 않는다", sum(1 for v in _seen.values() if v > 1), 0)
    # 한 번에 하나만 뜬다 (앞 자막이 안 남는다)
    _ev = sorted([(x["t_start"], 1) for x in pl["subs"] if x["kind"] != "title"]
                 + [(x["t_start"] + x["t_dur"], -1) for x in pl["subs"]
                    if x["kind"] != "title"])
    _n, _mx = 0, 0
    for _t, _d in _ev:
        _n += _d
        _mx = max(_mx, _n)
    eq("동시에 뜨는 자막은 하나뿐", _mx, 1)
    eq("단 8개", 1 + max(x.get("row", 0) for x in pl["subs"]), 8)
    # 같은 줄끼리 시간이 겹치면 CapCut 이 "세그먼트가 겹칩니다" 로 죽는다
    _ov = 0
    for _r in {x.get("row", 0) for x in pl["subs"] if x["kind"] != "title"}:
        _ss = sorted((x for x in pl["subs"]
                      if x["kind"] != "title" and x.get("row", 0) == _r),
                     key=lambda x: x["t_start"])
        _ov += sum(1 for i in range(len(_ss) - 1)
                   if _ss[i]["t_start"] + _ss[i]["t_dur"] > _ss[i + 1]["t_start"] + 1e-9)
    eq("같은 단 안에서 겹치지 않는다", _ov, 0)
    # 블록이 바뀌면 다시 1단부터
    _first = {}
    for x in sorted(pl["subs"], key=lambda x: x["t_start"]):
        if x["kind"] != "title":
            _first.setdefault(x["bi"], x.get("row", 0))
    check("블록마다 맨 아래 단에서 시작", set(_first.values()) == {0}, str(_first))
    eq("강등 0", st["demoted"], 0)
    check("되감기 raise 없음", True)
    intruded = {}
    for u in pl["units"]:
        if u["item"].get("intruded"):
            intruded.setdefault(u["bi"] + 1, 0.0)
            intruded[u["bi"] + 1] += u["src1"] - u["src0"]
    for b in (9, 10, 13):
        check("블록%d 침범 감지" % b, b in intruded, str(sorted(intruded)))
    near("블록9 나레이션 길이 유지", intruded.get(9, 0), 3.192, 0.01)
    near("블록10 나레이션 길이 유지", intruded.get(10, 0), 3.792, 0.01)
    check("음소거 구간이 실재", st["muted"] > 0, str(st["muted"]))

    # 소스 되감기 0건 — 대사 구간끼리
    segs = pl["segments"]
    rew = [i for i in range(len(segs) - 1)
           if -5.0 < segs[i + 1]["src0"] - segs[i]["src1"] < -1e-6]
    eq("소스 되감기 0건", len(rew), 0)

    print("\n[6] b-roll 왕복 — 블록6(249초)이 줄지 않을 것")
    txt = ref
    spans = []
    for _ in range(6):
        d = script_io.read(txt, cues)
        p = layout.build(d, cues, 6159.104, fps=24.0,
                         mute_under_narration=True)
        layout.apply_headers(d, p["headers"])
        txt = script_io.write(d)
        a, b = d["blocks"][5]["win"]
        spans.append(round(b - a, 1))
    eq("6회 rebuild 후에도 249.x초", len(set(spans)), 1)
    near("블록6 폭", spans[-1], 250.0, 1.0)
    check("헤더 메모 보존", "집에 드나드는 구간" in txt)

    print("\n[7] 되풀이 안정성 — rebuild 를 반복해도 길이가 안 흐를 것")
    d = script_io.read(txt, cues)
    p = layout.build(d, cues, 6159.104, fps=24.0,
                     mute_under_narration=True)
    near("총 길이 유지", p["total_s"], pl["total_s"], 0.05)

    print("\n[8] 대본 편집 왕복 — 고친 뒤에도 나머지가 살아 있을 것")
    paras = ref.replace("\r\n", "\n").split("\n\n")

    def run(t):
        d = script_io.read(t, cues)
        return d, layout.build(d, cues, 6159.104, fps=24.0,
                               mute_under_narration=True, log=lambda *_: None)

    _, p1 = run(ref.replace("(그런데 그 편지 안에는)",
                            "(그 편지 안에는 뜻밖의 물건이 함께 들어 있었는데)"))
    eq("① 나레이션 수정 — 컷 유지", p1["stats"]["cuts"], 48)
    check("① 길이만 늘어남", p1["total_s"] > pl["total_s"])

    i = next(k for k, p in enumerate(paras) if p.strip().startswith("[01:18:30"))
    j = next(k for k in range(i + 1, len(paras)) if paras[k].strip().startswith("["))
    d2, p2 = run("\n\n".join(paras[:i] + paras[j:]))
    eq("② 블록 삭제 — 블록 17", p2["stats"]["blocks"], 17)
    eq("② 강등 0", d2["stats"]["demoted"], 0)

    k = next(x for x, p in enumerate(paras) if p.strip() == "우리 집에서 뭐 해요?")
    sw = list(paras)
    sw[k], sw[k + 1] = sw[k + 1], sw[k]
    d3, p3 = run("\n\n".join(sw))
    eq("③ 문단 순서 교체 — 강등 0 (raise 하지 않을 것)", d3["stats"]["demoted"], 0)
    check("③ 되감기는 경고로만", any("되감" in w for w in p3["warnings"]))

    d4, _ = run(ref.replace("그만뒀어\n캘리포니아에 간대", "그만뒀어, 캘리포니아에 간대"))
    eq("④ 대사 줄바꿈을 바꿔도 매칭", d4["stats"]["demoted"], 0)

    d5, _ = run(ref.replace("(그리고 결국)", "(그리고 결국)\n\n내가 지어낸 대사입니다"))
    eq("⑤ 지어낸 대사만 강등 1건", d5["stats"]["demoted"], 1)

    d6, _ = run(ref.replace("[01:26:27 ~ 01:26:46]", "[01:26:27 ~ 01:26:36]"))
    eq("⑥ 범위를 좁히면 밖의 대사가 강등", d6["stats"]["demoted"], 1)

    # ⑦ 대본이 나레이션으로 시작하면 제목과 첫 나레이션이 둘 다 t=0 이다.
    #    예전엔 한 텍스트 트랙에 같이 얹어서 CapCut 조립이
    #    "자막(narration) 트랙 세그먼트가 겹칩니다 (0 < 41667)" 로 죽었다.
    i7 = next(k for k, p in enumerate(paras) if p.strip().startswith("["))
    d7, p7 = run("\n\n".join(paras[:i7 + 1]
                             + ["(잭은 그날 무엇을 잃었는지 몰랐고)"]
                             + paras[i7 + 1:]))
    first = sorted(p7["subs"], key=lambda s: s["t_start"])[:2]
    check("⑦ 대본이 나레이션으로 시작 — 제목과 나레이션이 둘 다 t=0",
          {s["kind"] for s in first} == {"title", "narration"}
          and all(abs(s["t_start"]) < 1e-9 for s in first))
    eq("⑦ 제목이 1프레임으로 뭉개지지 않을 것",
       round(next(s["t_dur"] for s in first if s["kind"] == "title"), 3),
       round(layout.TITLE_S, 3))
    # ⑧ 헤더만 남기고 문단을 지운 블록. 탭에서 흔히 하는 편집인데
    #    예전엔 layout 의 헤더 재계산에서 min() 이 빈 시퀀스로 ValueError 를 냈다.
    d8, p8 = run("\n\n".join(paras + ["[01:30:00 ~ 01:30:05]"]))
    check("⑧ 빈 블록 — raise 하지 않고 경고로", any("비어 있습니다" in w for w in p8["warnings"]))
    eq("⑧ 빈 블록이 컷을 만들지 않을 것", p8["stats"]["cuts"], pl["stats"]["cuts"])
    eq("⑧ 블록 수는 유지", len(script_io.read(script_io.write(
        layout.apply_headers(d8, p8["headers"]) or d8), cues)["blocks"]),
       len(d8["blocks"]))

    # ⑨ 헤더 앞 빈 줄을 빼면 헤더가 대사 문단에 먹혀 들어간다 (탭에서 흔한 실수).
    #    강등으로 잡히고, 문단 안에 헤더 문자열이 남아 있어야 cli 가 원인을 짚어 준다.
    # 헤더가 먹히면 그 블록이 통째로 사라져 뒤 문단들까지 앞 블록 범위 밖으로 밀린다.
    # 강등 개수는 대본마다 다르므로 "잡힌다 + 원인이 남는다" 만 고정한다.
    d9, _ = run(ref.replace("\n\n[01:36:47 ~ 01:36:54]", "\n[01:36:47 ~ 01:36:54]"))
    check("⑨ 헤더가 문단에 붙으면 강등된다", d9["stats"]["demoted"] >= 1)
    # 나레이션에 먹히면 블록이 통째로 사라지고 엉뚱한 문단이 강등된다.
    # 원인을 짚으려면 **강등 여부와 무관하게** 전체 문단에서 헤더를 찾아야 한다.
    check("⑨ 어느 문단엔가 헤더 문자열이 남아 cli 가 원인을 짚을 수 있을 것",
          any("[01:36:47" in it["text"] for _, it in script_io.items(d9)))
    check("⑨ 블록 하나가 사라진다", len(d9["blocks"]) < len(doc["blocks"]))

    import capcut_draft
    tl7 = capcut_draft.build_timeline(
        p7, {"path": r"C:\없는영화.mp4", "width": 1920, "height": 1080,
             "duration_s": 6159.104, "fps": 24.0},
        "vertical", "fit", None, None, None, log=lambda *_: None)
    # 단(줄 자리)마다 트랙이 하나씩 + 제목 트랙. 시간이 안 겹치니 한 트랙에
    # 몰아넣어도 되지만, 그러면 CapCut 에서 "몇 번째 단"을 통째로 못 고른다.
    _rows7 = 1 + max((x.get("row", 0) for x in p7["subs"]
                      if x["kind"] != "title"), default=0)
    eq("⑦ CapCut 조립 성공 — 자막 트랙 = 단 수 + 제목",
       tl7["track_counts"]["text"], _rows7 + 1)

    print("\n[9] 대본 생성기 — 실패해도 기존 대본을 지우지 말 것")
    import script_gen
    with tempfile.TemporaryDirectory() as td:
        keep = pathlib.Path(td) / "기존대본.txt"
        keep.write_text("사람이 쓴 소중한 대본\n", encoding="utf-8")
        cfg = ROOT / "runner.json"
        saved = cfg.read_text(encoding="utf-8") if cfg.exists() else None
        # 무조건 실패하는 러너를 물린다
        dud = pathlib.Path(td) / "dud.cmd"
        with open(dud, "w", encoding="utf-8", newline="\r\n") as f:
            f.write("@echo off\r\nexit /b 9\r\n")
        cfg.write_text('{"runner": "codex", "exe": "%s", "argv": ["exec", "{prompt}"]}'
                       % str(dud).replace("\\", "\\\\"), encoding="utf-8")
        try:
            script_gen.generate(SRT, keep, target_s=100, log=lambda *_: None)
            check("실패해야 하는데 성공했다", False)
        except script_gen.GenError:
            check("에이전트 실패 → GenError", True)
        finally:
            if saved is None:
                cfg.unlink(missing_ok=True)
            else:
                cfg.write_text(saved, encoding="utf-8")
        check("기존 대본이 살아 있을 것", keep.exists())
        eq("내용도 그대로", keep.read_text(encoding="utf-8"), "사람이 쓴 소중한 대본\n")

    print("\n[10] 파일 이름 · ASS 자막")
    NL = chr(92) + "N"          # 소스에 직접 쓰면 이스케이프 사고가 난다
    for stem, want in (("영화_타임라인과 자막", "영화"),
                       ("영화_타임라인 대본", "영화"),
                       ("영화_타임라인 대본_0818_1745", "영화"),
                       ("영화_2024_1080", "영화_2024_1080"),
                       ("그냥이름", "그냥이름")):
        eq("접미사 제거 %s" % stem, script_io.strip_suffix(stem), want)

    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        for n in ("a_타임라인 대본.txt", "b_타임라인 대본_0818_1745.txt"):
            (d / n).write_text("x", encoding="utf-8")
        eq("시각 붙은 대본도 찾는다", len(script_io.find_scripts(d)), 2)
        d2 = d / "old"
        d2.mkdir()
        (d2 / "c_타임라인과 자막.txt").write_text("x", encoding="utf-8")
        eq("옛 이름 대본도 찾는다", len(script_io.find_scripts(d2)), 1)

        # ASS: [V4+ Styles] 에도 Format 줄이 있어 그걸 잡으면 큐가 0개가 된다
        a = d / "샘플.ass"
        a.write_text("\n".join([
            "[Script Info]", "ScriptType: v4.00+", "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, Encoding",
            "Style: Default,Arial,48,&H00FFFFFF,1", "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text",
            "Dialogue: 0,0:01:23.45,0:01:25.67,Default,,0,0,0,,"
            "{" + chr(92) + "i1}우리 집에서{" + chr(92) + "i0} 뭐 해요?",
            "Dialogue: 0,0:01:26.00,0:01:28.50,Default,,0,0,0,,"
            "잠시 보관해 줘요" + NL + "나중에 와서 가져갈게요",
            "Comment: 0,0:01:30.00,0:01:31.00,Default,,0,0,0,,안 나온다",
            "Dialogue: 0,0:01:32.00,0:01:34.00,Default,,0,0,0,,쉼표가, 든, 대사",
        ]) + "\n", encoding="utf-8")
        acues, ameta = subtitle.parse_file(a)
        eq("ASS 파서 선택", ameta["parser"], "ass")
        eq("Comment 줄 제외", len(acues), 3)
        eq("override 태그 제거", acues[0]["text"], "우리 집에서 뭐 해요?")
        check("ASS 시각은 1/100초", abs(acues[0]["start_s"] - 83.45) < 0.01)
        eq("ASS 줄바꿈 보존", acues[1]["lines"],
           ["잠시 보관해 줘요", "나중에 와서 가져갈게요"])
        eq("대사 안 쉼표 보존", acues[2]["text"], "쉼표가, 든, 대사")
        check("ASS 도 불변식 유지",
              all(" ".join(c["lines"]) == c["text"] for c in acues))

    print("\n[11] `>` 번역 줄 — 외국어 자막")
    # 원문으로 매칭하고 화면에는 번역이 나가야 한다.
    src1 = next(c for c in cues if len(c["lines"]) == 1 and len(c["text"]) > 8)
    src2 = next(c for c in cues
                if c["idx"] > src1["idx"] + 1 and len(c["lines"]) == 1)
    t = "\n\n".join([
        "제목",
        "[%s ~ %s]" % (script_io.hms(src1["start_s"] - 2),
                       script_io.hms(src2["end_s"] + 2)),
        src1["text"] + "\n> 번역된 첫 대사",
        src2["text"],
    ]) + "\n"
    dT = script_io.read(t, cues, log=lambda *_: None)
    eq("번역이 있어도 원문으로 매칭 (강등 0)", dT["stats"]["demoted"], 0)
    its = [it for _, it in script_io.items(dT) if it["kind"] == "dialogue"]
    eq("번역 붙은 문단만 trans 를 가진다",
       [bool(it.get("trans")) for it in its], [True, False])
    eq("매칭 텍스트는 원문 그대로", its[0]["text"], src1["text"])

    pT = layout.build(dT, cues, 6159.104, log=lambda *_: None)
    dsubs = {tuple(x["lines"]) for x in pT["subs"] if x["kind"] == "dialogue"}
    check("화면에는 번역이 나간다", ("번역된 첫 대사",) in dsubs, str(dsubs))
    check("번역 없는 문단은 자막 원문 그대로",
          tuple(src2["lines"]) in dsubs, str(dsubs))
    check("원문은 화면에 안 나간다", (src1["text"],) not in dsubs, str(dsubs))

    w1 = script_io.write(dT)
    check("번역 줄이 다시 쓰인다", "> 번역된 첫 대사" in w1)
    eq("번역이 있어도 왕복 고정점",
       script_io.write(script_io.read(w1, cues, log=lambda *_: None)), w1)

    # `#자막번호` 앵커 — 대본이 전부 한국어여도 컷을 찾아야 한다
    r1, r2 = src1["src_no"], src2["src_no"]
    tN = "\n\n".join([
        "제목",
        "[%s ~ %s]" % (script_io.hms(src1["start_s"] - 2),
                       script_io.hms(src2["end_s"] + 2)),
        "#%d 번호로 가리킨 한국어 대사" % r1,
        "#%d 두 번째 한국어 대사" % r2,
    ]) + "\n"
    dN = script_io.read(tN, cues, log=lambda *_: None)
    eq("번호로 매칭 (강등 0)", dN["stats"]["demoted"], 0)
    eq("번호매칭 2건", dN["stats"]["by_ref"], 2)
    itsN = [it for _, it in script_io.items(dN) if it["kind"] == "dialogue"]
    eq("가리킨 큐가 맞다", itsN[0]["cues"][0]["src_no"], r1)
    eq("번호는 텍스트에서 빠진다", itsN[0]["text"], "번호로 가리킨 한국어 대사")
    pN = layout.build(dN, cues, 6159.104, log=lambda *_: None)
    dsN = [s for s in sorted(pN["subs"], key=lambda x: x["t_start"])
           if s["kind"] == "dialogue"]
    eq("화면에 대본의 한국어가 나간다", dsN[0]["lines"],
       ["번호로 가리킨 한국어 대사"])
    wN = script_io.write(dN)
    check("번호가 다시 쓰인다", ("#%d 번호로" % r1) in wN)
    eq("번호 앵커도 왕복 고정점",
       script_io.write(script_io.read(wN, cues, log=lambda *_: None)), wN)

    tM = tN.replace("#%d 두 번째" % r2, "#%d-%d 두 번째" % (r2, r2 + 1))
    dM = script_io.read(tM, cues, log=lambda *_: None)
    itsM = [it for _, it in script_io.items(dM) if it["kind"] == "dialogue"]
    eq("#N-M 은 연속 두 큐를 합친다", len(itsM[1]["cues"]), 2)

    dX = script_io.read(tN.replace("#%d " % r1, "#999999 "), cues,
                        log=lambda *_: None)
    eq("없는 번호는 강등", dX["stats"]["demoted"], 1)

    # 자막 언어 판별 — 이게 틀리면 번역 강제가 엉뚱하게 걸린다
    check("한국어 자막을 한국어로 본다", subtitle.is_korean(cues))
    eng = [{"text": "This is by far the worst day of my life!"},
           {"text": "I was your only friend."},
           {"text": "We'd be bestest friends forever."}] * 20
    check("영어 자막을 한국어가 아니라고 본다", not subtitle.is_korean(eng))
    mixed = eng[:18] + [{"text": "한글 한 줄"}, {"text": "또 한 줄"}]
    check("영어에 한글 몇 줄이 섞여도 한국어가 아니다",
          not subtitle.is_korean(mixed))
    check("큐가 없으면 한국어로 본다(막지 않는다)", subtitle.is_korean([]))

    # 프롬프트는 자막 언어에 따라 저절로 달라진다 (사용자가 켜는 스위치가 아니다)
    import script_gen
    with tempfile.TemporaryDirectory() as td:
        e = pathlib.Path(td) / "eng.srt"
        e.write_text("\n\n".join(
            "%d\n00:00:%02d,000 --> 00:00:%02d,000\nThis is an english line."
            % (i + 1, i, i + 1) for i in range(30)) + "\n", encoding="utf-8")
        pe = script_gen.build_prompt(e, "out.txt", 180)
        check("영어 자막이면 프롬프트에 번역 지시가 붙는다",
              "이 자막은 한국어가 아니다" in pe)
    pk = script_gen.build_prompt(SRT, "out.txt", 180)
    check("한국어 자막이면 번역 지시가 안 붙는다",
          "이 자막은 한국어가 아니다" not in pk)

    print("\n[12] 로그인/로그아웃 인자")
    # `/login` `/logout` 은 세션 안 슬래시 명령이다. CLI 인자로 넘기면 에이전트가
    # 대화형으로 켜져 첫 실행 테마 선택 화면이 뜨고 로그아웃은 되지 않는다.
    for key, acts in script_gen.AUTH_ACTIONS.items():
        for name, args in acts.items():
            check("%s %s 인자가 슬래시로 시작하지 않을 것" % (key, name),
                  bool(args) and not any(a.startswith("/") for a in args))
    eq("claude 는 auth 서브커맨드를 쓴다",
       script_gen.AUTH_ACTIONS["claude"]["logout"], ["auth", "logout"])
    eq("codex 는 최상위 서브커맨드를 쓴다",
       script_gen.AUTH_ACTIONS["codex"]["logout"], ["logout"])

    print("\n[13] `@시각` 앵커 · 음소거 옵션 · 항상 새로 만들기")
    import pipeline

    # 시각을 박으면 자막 없이도 같은 컷이 나와야 한다
    pl0 = layout.build(doc, cues, 6159.104, fps=24.0,
                       mute_under_narration=False, log=lambda *_: None)
    import copy
    d2 = script_io.read(ref, cues, log=lambda *_: None)
    p2 = layout.build(d2, cues, 6159.104, fps=24.0,
                      mute_under_narration=False, log=lambda *_: None)
    n_dlg = d2["stats"]["dialogue"]
    layout.apply_spans(d2, p2)
    frozen = script_io.write(d2)
    check("시각이 박힌다", frozen.count("@") >= n_dlg)
    check("소수 2자리로 쓴다 (1자리면 프레임보다 크게 밀린다)",
          re.search(r"@\d\d:\d\d:\d\d\.\d\d~", frozen) is not None)

    d3 = script_io.read(frozen, [], log=lambda *_: None)   # 자막 없이 읽는다
    eq("자막 0개로도 강등 0", d3["stats"]["demoted"], 0)
    eq("전부 시각으로 매칭", d3["stats"]["by_time"], n_dlg)
    p3 = layout.build(d3, [], 6159.104, fps=24.0,
                      mute_under_narration=False, log=lambda *_: None)
    eq("컷 수가 같다", len(p3["segments"]), len(pl0["segments"]))
    worst = max(abs(a["src0"] - b["src0"])
                for a, b in zip(pl0["segments"], p3["segments"]))
    check("컷 시작이 1프레임(0.042초) 안에서 같다  (실측 %.3f초)" % worst,
          worst <= 0.011)
    eq("시각 앵커도 왕복 고정점",
       script_io.write(script_io.read(frozen, [], log=lambda *_: None)), frozen)

    # 음소거 옵션
    on = layout.build(script_io.read(ref, cues, log=lambda *_: None), cues,
                      6159.104, fps=24.0, mute_under_narration=True,
                      log=lambda *_: None)
    off = layout.build(script_io.read(ref, cues, log=lambda *_: None), cues,
                       6159.104, fps=24.0, mute_under_narration=False,
                       log=lambda *_: None)
    check("음소거 켜면 볼륨 0 구간이 생긴다", on["stats"]["muted"] > 0)
    # 기본은 **끔** — 영화 소리를 100% 그대로 둔다 (GUI 체크박스도 해제 상태)
    plain = layout.build(script_io.read(ref, cues, log=lambda *_: None), cues,
                         6159.104, fps=24.0, log=lambda *_: None)
    eq("음소거 기본값은 꺼짐", plain["stats"]["muted"], 0)
    eq("run_build 기본값도 꺼짐",
       inspect.signature(pipeline.run_build).parameters["mute"].default, False)
    eq("음소거 끄면 0개", off["stats"]["muted"], 0)
    check("음소거를 끄면 컷이 덜 쪼개진다",
          len(off["segments"]) < len(on["segments"]))
    check("총 길이는 같다", abs(on["total_s"] - off["total_s"]) < 0.01)

    # 마지막 자막이 영상보다 길면 검은 화면에 자막만 뜨는 꼬리가 생긴다
    ref_pl = layout.build(script_io.read(ref, cues, log=lambda *_: None), cues,
                          6159.104, fps=24.0, log=lambda *_: None)
    sub_end = max(x["t_start"] + x["t_dur"] for x in ref_pl["subs"])
    check("자막이 영상 끝을 넘지 않는다", sub_end <= ref_pl["total_s"] + 1e-6,
          "자막 %.3f > 영상 %.3f" % (sub_end, ref_pl["total_s"]))
    last = max(ref_pl["subs"], key=lambda x: x["t_start"])
    check("그래도 마지막 자막은 충분히 보인다", last["t_dur"] >= 1.0,
          "%.2f초" % last["t_dur"])

    # 같은 이름이면 덮지 않고 새로 만든다
    with tempfile.TemporaryDirectory() as td:
        saved = pipeline.RESULTS
        try:
            pipeline.RESULTS = pathlib.Path(td)
            names = [pipeline.fresh_results_dir("어떤영화").name for _ in range(3)]
            eq("같은 이름 3번 → 새 폴더 3개", names,
               ["어떤영화", "어떤영화_2", "어떤영화_3"])
            # 대본만 만들기도 results/ 안에 자기 폴더를 갖는다.
            # 번호는 접미사 **앞**에 — 접미사가 끝에 와야 종류가 한눈에 보인다.
            sfx = pipeline.SCRIPT_DIR_SUFFIX
            eq("대본만 만들기 폴더 3개",
               [pipeline.fresh_results_dir("어떤영화", sfx).name for _ in range(3)],
               ["어떤영화" + sfx, "어떤영화_2" + sfx, "어떤영화_3" + sfx])
            check("만들기 폴더와 안 겹친다",
                  not (pathlib.Path(td) / ("어떤영화" + sfx)).samefile(
                      pathlib.Path(td) / "어떤영화"))
        finally:
            pipeline.RESULTS = saved

    # CapCut 폴더도 겹치면 새로 만들고, **보이는 이름이 폴더명과 같아야** 한다.
    # (draft_name 이 원래 이름으로 남으면 CapCut 목록에 같은 이름 셋이 뜬다)
    with tempfile.TemporaryDirectory() as td:
        tl = capcut_draft.build_timeline(
            layout.build(script_io.read(ref, cues, log=lambda *_: None), cues,
                         6159.104, fps=24.0, log=lambda *_: None),
            {"path": str(TEST / "없는영화.mp4"), "duration_s": 6159.104,
             "width": 1920, "height": 1080},
            log=lambda *_: None)
        got = []
        for _ in range(3):
            ident = capcut_draft.create_project("겹침", tl, draft_root=td,
                                                log=lambda *_: None)
            meta = json.loads((pathlib.Path(ident["fold_path"])
                               / "draft_meta_info.json").read_text(encoding="utf-8"))
            got.append((ident["folder_name"], meta.get("draft_name")))
        eq("CapCut 폴더 3개 + 보이는 이름 일치", got,
           [("겹침", "겹침"), ("겹침_2", "겹침_2"), ("겹침_3", "겹침_3")])

    # 갱신·백업·되돌리기·리포트는 사라졌다
    import guards as g
    for gone in ("backup_project", "restore_backup", "list_backups",
                 "prune_backups", "detect_user_edits", "NeedsConfirm"):
        check("guards.%s 제거됨" % gone, not hasattr(g, gone))
    check("capcut_draft.update_project 제거됨",
          not hasattr(capcut_draft, "update_project"))
    check("pipeline.undo 제거됨", not hasattr(pipeline, "undo"))
    check("pipeline._report 제거됨", not hasattr(pipeline, "_report"))
    check("report.py 없음", not (ROOT / "report.py").exists())

    # 대본 생성 중간 파일은 results/ 를 더럽히지 않는다
    import script_gen as sg
    sfx = pipeline.SCRIPT_DIR_SUFFIX
    check("중간 파일은 .work/ 로", sg.WORK.name == ".work"
          and sg.WORK.parent == ROOT)
    check("타이밍 기록도 .work/ 안", sg.TIMING.parent == sg.WORK)
    check("results/ 는 프로젝트 폴더만", "results" not in str(sg.WORK))
    check("자막 옆에 떨어뜨리는 경로 계산은 없다",
          not hasattr(sg, "default_out_path"))
    check("generate 가 work_dir 를 받는다",
          "work_dir" in inspect.signature(sg.generate).parameters)
    check("run_script 가 project_name 을 받는다",
          "project_name" in inspect.signature(pipeline.run_script).parameters)

    # 대본만 만들기 결과에도 시각을 박는다. 외국어 자막이면 `#763` 번호뿐이라
    # 자막 파일이 없으면 아무 시각도 알 수 없다.
    _rs = inspect.getsource(pipeline.run_script)
    check("run_script 가 시각을 박는다", "apply_spans" in _rs)
    check("나레이션 파일도 같이 쓴다", "narration_text" in _rs)
    # 전부 강등된 대본(자막이 아예 안 맞는 경우)에는 박을 시각이 없다
    check("강등뿐이면 박지 않는다", "st[\"demoted\"] < st[\"dialogue\"]" in _rs)
    check("중단은 실패와 구분된다", issubclass(sg.GenStopped, sg.GenError))

    # codex 는 --full-auto 를 없앴다 (0.147.0: "unexpected argument '--full-auto'").
    # 그대로 두면 1단이 항상 튕기고, 뒤 단계는 읽기 전용이라 대본을 못 쓴다.
    cx = sg.RUNNERS["codex"]["argv"]
    # effort 를 낮추면 대본이 나빠진다 (실측: low 는 완결형으로 끊고 대사를
    # 다시 설명한다). check 로는 안 잡히므로 인자로 못 박는다.
    # 문체 예시가 **내 대본**이어야 한다. 중경삼림(중앙 30자)을 보여 주면
    # 에이전트가 그걸 베껴 출력이 25자로 길어진다 (실측).
    _sty = (ROOT / "template" / "style_example.txt").read_text(encoding="utf-8")
    check("문체 예시가 내 대본이다", "나레이션 33문장 전문" in _sty)
    check("문체 예시에 중경삼림 대사가 없다",
          "663" not in _sty and "페이" not in _sty)
    # 문체 지시는 **목소리가 먼저, 숫자는 뒤**여야 한다. 숫자만 주면 지표는
    # 맞는데 줄거리 요약문이 나온다 — 실측: 중앙 15자·최빈 42%로 다 맞춘 대본이
    # `그는 텅 빈 집에서` 를 썼다. 유저 33문장엔 `그는/그녀는` 이 **0개**다.
    import script_gen as _sg2
    _pp = _sg2.build_prompt("x.srt", "o.txt", 180)
    for _k in ("숫자를 맞추려 하지 마라", "`그`·`그녀`로 부르지 마라",
               "설명하지 말고 반응을 적어라", "은어와 과장을 쓴다",
               "나레이션을 아껴라", "많이 쓰라고 한 게 아니다",
               "아끼는 건 개수지 표현이 아니다", "이름은 자막에 나온 것만 쓴다",
               "이건 결과지 목표가 아니다"):
        check("프롬프트에 '%s'" % _k, _k in _pp)
    check("목소리가 숫자보다 앞에 온다",
          _pp.index("`그`·`그녀`로 부르지 마라") < _pp.index("이건 결과지 목표가 아니다"))

    for _k in ("중앙 16자", "~는데", "~죠", "다섯에 하나만", "14자를 절대 안 넘는다"):
        check("문체 예시에 '%s'" % _k, _k in _sty)
    # 정답 34문장이 전수 1회씩 실렸는지 (표본으로 확인)
    for _n in ("일진은 좀 쫄았고", "그대로 멘탈이 나가버렸고",
               "말빨에 말문이 막힌 일진은 화가나 결국 칼을 꺼내버렸는데",
               "결국 인내심에 한계가 온 미키"):
        eq("문체 예시에 %r 한 번" % _n[:14], _sty.count(_n), 1)

    # 프롬프트 — 문체 예시가 규칙 문서보다 **앞**에 와야 베낀다
    _pr = sg.build_prompt(SRT, "o.txt", 180)
    check("프롬프트가 문체 예시를 먼저 가리킨다",
          _pr.index("style_example.txt") < _pr.index("AGENTS.md"))
    # 없는 앵커를 가리키면 에이전트가 그 항목을 통째로 건너뛴다
    check("없는 절 §구성 을 안 가리킨다", "§구성" not in _pr)
    check("AGENTS.md 도 §구성 을 안 가리킨다",
          "§구성" not in (ROOT / "AGENTS.md").read_text(encoding="utf-8"))
    # `cd …` 로 시작하면 allowlist Bash(python:*) 에 안 걸려 검증이 통째로 생략된다
    check("검증 명령이 python 으로 시작한다",
          not any(l.strip().startswith("cd ") for l in _pr.splitlines()))
    check("검증 명령이 절대경로 cli.py",
          any("cli.py" in l and "check" in l and l.strip().startswith("python ")
              for l in _pr.splitlines()))
    # 통계를 잘못 옮긴 자리 — 원문은 "문단당 인용 큐 94%가 1개"다
    check("한 문단 = 자막 '큐' 하나", "자막 큐 하나" in _pr)
    check("'자막 한 줄' 오기 없음", "한 문단 = 자막 한 줄" not in _pr)
    # 실측값이 프롬프트에 박혔는지
    for _k in ("중앙 16자", "8~15초에 하나", "14자 이하 짧은 대사"):
        check("프롬프트에 '%s'" % _k, _k in _pr)

    check("effort 를 낮추지 않는다",
          all("--effort" not in a for a in sg.RUNNERS["claude"]["argv"]))

    # 영화 제목 — 파일 이름이 제목이 아닌 경우가 많다(English.srt)
    check("build_prompt 가 제목을 받는다",
          "movie_title" in inspect.signature(sg.build_prompt).parameters)
    for _f in (sg.generate, pipeline.run_script, pipeline.run_build):
        check("%s 가 제목을 넘긴다" % _f.__name__,
              "movie_title" in inspect.signature(_f).parameters)
    _with = sg.build_prompt(SRT, "o.txt", 180, "", "중경삼림")
    _without = sg.build_prompt(SRT, "o.txt", 180, "", "")
    check("제목을 주면 영화 지식을 쓰라고 한다", "이 영화를 안다면" in _with)
    check("비우면 안 붙는다", "이 영화를 안다면" not in _without)
    check("알아도 대사는 자막 원문 그대로",
          "대사는 언제나 자막 원문 그대로" in _with)
    check("영화 지식은 고르는 데만", "고를지 정하는 데만" in _with)
    _gui = (ROOT / "gui.py").read_text(encoding="utf-8")
    eq("GUI 두 실행 경로에 제목 전달",
       _gui.count("movie_title=self._title()"), 2)

    # 줄거리·결말을 파일로 남긴다 — 편집 전에 [줄거리] 탭에서 읽으라고.
    check("build_prompt 가 줄거리 경로를 받는다",
          "plot_path" in inspect.signature(sg.build_prompt).parameters)
    _pp = sg.build_prompt(SRT, "o.txt", 180, "", "", plot_path="results/x/줄거리.txt")
    _np = sg.build_prompt(SRT, "o.txt", 180, "", "")
    for _k in ("[줄거리]", "[결말]", "[이 대본에서 고른 것]", "[자막에 안 나오는 것]"):
        check("줄거리 지시에 %s 항목" % _k, _k in _pp)
    check("줄거리는 대본보다 먼저 쓰라고 한다", "대본을 쓰기 전에" in _pp)
    check("줄거리도 지어내지 말라고 한다", "지어내지 마라" in _pp)
    check("경로를 안 주면 줄거리 지시가 없다", "[줄거리]" not in _np)
    eq("줄거리 파일 이름", sg.PLOT_NAME, "줄거리.txt")
    # 대본이 나왔는데 줄거리가 없다고 실패하면 안 된다 (있으면 좋고 없어도 그만)
    _gen = inspect.getsource(sg.generate)
    check("줄거리가 없어도 성공으로 본다", "줄거리 파일은 안 나왔습니다" in _gen)
    # GUI 탭
    check("GUI 에 줄거리 탭", '"plot", "줄거리"' in _gui)
    check("줄거리 탭은 읽기 전용", 'sb3.set, state="disabled"' in _gui)
    check("실행 결과에서 줄거리를 싣는다", 'res.get("plot_path")' in _gui)

    check("codex 사다리에 --full-auto 없음",
          all("--full-auto" not in a for a in cx))
    check("1단이 쓰기 가능 모드", "--approve-for-me" in cx[0])
    # --approve-for-me 와 --sandbox 는 상호 배타 — 같이 넣으면 codex 가 거부한다
    check("--approve-for-me 와 --sandbox 를 같이 안 쓴다",
          not any("--approve-for-me" in a and "--sandbox" in a for a in cx))
    check("모든 단이 인자로 시작하지 않음", all(not a[0].startswith("-") for a in cx))

    # 쓰기 거부는 인자 오류와 원인이 달라 따로 안내해야 한다
    check("읽기 전용 메시지를 알아본다",
          sg._looks_read_only("현재 세션이 읽기 전용이라 파일 생성이 차단됐습니다"))
    check("permission denied 도 알아본다",
          sg._looks_read_only("EACCES: permission denied, open 'draft.txt'"))
    # 자막을 안 넣은 건 "못 찾은" 게 아니다 — 문단마다 ⚠ 를 뿌리면
    # 대본이 틀린 것처럼 보인다 (GUI 팝업을 없앤 것과 같은 이유).
    plain_txt = "\n\n".join([
        "제목", "[00:10:00 ~ 00:10:40]", "우리 집에서 뭐 해요?",
        "(그렇게 시작됐는데)", "편지 안 가져가요?"])
    d_no = script_io.read(plain_txt, [], log=lambda *_: None)
    d_yes = script_io.read(plain_txt, cues, log=lambda *_: None)
    eq("자막 없으면 '못 찾음' 경고 0건",
       sum(1 for w in d_no["warnings"] if "찾지 못했" in w), 0)
    check("그래도 강등 수는 센다", d_no["stats"]["demoted"] > 0)
    check("자막을 넣으면 경고는 그대로",
          any("찾지 못했" in w for w in d_yes["warnings"])
          or d_yes["stats"]["demoted"] == 0)

    # 자막 없는 빌드는 팝업으로 막지 않는다 — 로그로만 알린다
    gui_src = (ROOT / "gui.py").read_text(encoding="utf-8")
    import ast
    _cls = next(n for n in ast.parse(gui_src).body if isinstance(n, ast.ClassDef))
    _fn = next(n for n in _cls.body
               if isinstance(n, ast.FunctionDef) and n.name == "_do_build")
    _seg = ast.get_source_segment(gui_src, _fn)
    check("_do_build 에 팝업 없음", "messagebox" not in _seg)
    check("대신 로그로 알린다", "균등 배치합니다" in _seg)
    check("쓰지 않는 _ask 제거됨", "def _ask(" not in gui_src)

    # `#763 아빠? / 마야?` — 번호 없는 줄을 밑에 붙이면 그 줄이 앞 큐 구간에
    # 같이 뜨고 뒤 큐 장면은 통째로 빠진다. 전에는 경고 0건으로 조용했다.
    _one = next(c for c in cues if len(c["lines"]) == 1 and c["src_no"] > 460)
    _nxt = next(c for c in cues if c["src_no"] == _one["src_no"] + 1)
    _two = next(c for c in cues if len(c["lines"]) == 2 and c["src_no"] > 460)
    def _mk(body, a_, b_):
        h = "[%s ~ %s]" % (subtitle.hms(a_ - 2), subtitle.hms(b_ + 2))
        return script_io.read("제목\n\n" + h + "\n\n" + body, cues,
                              log=lambda *_: None)
    _bad = _mk("#%d 아빠?\n마야?" % _one["src_no"],
               _one["start_s"], _nxt["end_s"])
    _sep = _mk("#%d 아빠?\n\n#%d 마야?" % (_one["src_no"], _nxt["src_no"]),
               _one["start_s"], _nxt["end_s"])
    _rng = _mk("#%d-%d 아빠?\n마야?" % (_one["src_no"], _nxt["src_no"]),
               _one["start_s"], _nxt["end_s"])
    _2ln = _mk("#%d %s\n%s" % (_two["src_no"], _two["lines"][0], _two["lines"][1]),
               _two["start_s"], _two["end_s"])
    eq("번호 없는 줄을 붙이면 잡는다", _bad["stats"]["overstuffed"], 1)
    eq("큐마다 번호를 달면 조용", _sep["stats"]["overstuffed"], 0)
    eq("#a-b 범위는 조용", _rng["stats"]["overstuffed"], 0)
    eq("2줄짜리 큐에 2줄은 조용", _2ln["stats"]["overstuffed"], 0)
    # 실제 피해: 뒤 큐 장면이 빠져 컷이 짧아진다
    _pb = layout.build(_bad, cues, 6159.104, fps=24.0, log=lambda *_: None)
    _ps = layout.build(_sep, cues, 6159.104, fps=24.0, log=lambda *_: None)
    check("붙여 쓰면 실제로 짧아진다", _pb["total_s"] < _ps["total_s"] - 1.0,
          "%.2f초 vs %.2f초" % (_pb["total_s"], _ps["total_s"]))

    # 시각 박기를 하면 write 가 `#461 @00:52:01.59~… 아빠?` 로 쓴다.
    # read 가 시각을 먼저 보면 `#` 로 시작하는 줄에서 매칭이 안 돼 시각을
    # 통째로 놓치고, `@…` 가 대사 텍스트에 섞여 들어간다 (자막 없이 못 연다).
    _fz = _mk("#%d 아빠?\n\n#%d 마야?" % (_one["src_no"], _nxt["src_no"]),
              _one["start_s"], _nxt["end_s"])
    _pf = layout.build(_fz, cues, 6159.104, fps=24.0, log=lambda *_: None)
    layout.apply_headers(_fz, _pf["headers"])
    layout.apply_spans(_fz, _pf)
    _out = script_io.write(_fz)
    check("번호와 시각이 같이 쓰인다", "#%d @" % _one["src_no"] in _out, _out[:60])
    _back = script_io.read(_out, [], log=lambda *_: None)      # 자막 없이
    _dl = [it for _, it in script_io.items(_back) if it["kind"] == "dialogue"]
    check("자막 없이도 시각을 읽는다", all(it["span"] for it in _dl))
    check("번호도 살아 있다", all(it["cue_ref"] for it in _dl))
    check("텍스트에 @ 가 안 섞인다", all("@" not in it["text"] for it in _dl))
    eq("자막 없이 읽어도 강등 0", sum(1 for it in _dl if it["demoted"]), 0)
    eq("번호+시각도 왕복 고정점",
       script_io.write(script_io.read(_out, [], log=lambda *_: None)), _out)

    # 가까운 두 큐 사이에 나레이션을 넣으면 조각이 서로 겹친다.
    # 음소거를 끄면 볼륨이 같아 대사와 합쳐지고, 합친 조각을 되감기 검사가
    # "대사→대사"로 오판해 죽었다 (실측 708쌍 중 43건).
    # 음소거를 켜면 자르는 위치가 1프레임을 못 채워 죽었다 (12건).
    _near = [k for k in range(len(cues) - 1)
             if 0 <= cues[k + 1]["start_s"] - cues[k]["end_s"] < 3.0]
    check("3초 미만 간격이 실제로 흔하다", len(_near) > 500, str(len(_near)))
    for _m in (True, False):
        _bad = 0
        for k in _near[::37]:
            _a, _b = cues[k], cues[k + 1]
            _h = "[%s ~ %s]" % (subtitle.hms(_a["start_s"] - 3),
                                subtitle.hms(_b["end_s"] + 3))
            _t = ("제목\n\n" + _h + "\n\n" + _a["text"] +
                  "\n\n(그 사이에 무슨 일이 있었냐면)\n\n" + _b["text"])
            try:
                layout.build(script_io.read(_t, cues, log=lambda *_: None), cues,
                             6159.104, fps=24.0, mute_under_narration=_m,
                             log=lambda *_: None)
            except Exception:
                _bad += 1
        eq("나레이션을 대사 사이에 넣어도 안 죽는다 (음소거 %s)"
           % ("켬" if _m else "끔"), _bad, 0)

    check("정상 출력은 오판하지 않는다",
          not sg._looks_read_only("대본을 다 썼습니다. 총 172초 · 블록 9개."))
    check("쓰지 않는 results_dir 제거됨", not hasattr(pipeline, "results_dir"))

    # 폴더 이름 경쟁 — exists() 로 보고 나중에 mkdir 하면 두 실행이 같은 폴더를
    # 잡고, 중간 파일이 그 안에 있으므로 서로 프롬프트를 덮어쓴다.
    with tempfile.TemporaryDirectory() as td:
        saved = pipeline.RESULTS
        try:
            pipeline.RESULTS = pathlib.Path(td)
            got, bar = [], threading.Barrier(8)
            def _one():
                bar.wait()
                got.append(pipeline.fresh_results_dir("동시", sfx).name)
            ts = [threading.Thread(target=_one) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            eq("동시 8회 → 폴더 8개", len(set(got)), 8)
        finally:
            pipeline.RESULTS = saved

    # 샌드박스 판정 — 자막이 밖에 있으면 사본을 만들어야 한다
    check("프로젝트 안 자막은 그대로", sg._inside_workdir(TEST / "x.srt"))
    check("다른 드라이브 자막은 사본 필요", not sg._inside_workdir("D:/영화/x.srt"))

    # 시각 앵커 판별은 `"@" in text` 가 아니어야 한다 —
    # 대사에 메일 주소가 있으면 자막 요구를 건너뛰고 조용히 균등 배치된다.
    check("메일 주소를 시각으로 오판하지 않을 것",
          not script_io.TIME_MARK.match("메일은 a@b.com 으로 보내"))
    check("진짜 시각 앵커는 잡을 것",
          bool(script_io.TIME_MARK.match("@01:25:02.80~01:25:04.29 대사")))

    # test/ 는 읽기 전용 자료다 — 어떤 테스트도 여기에 쓰면 안 된다.
    # 산출물은 전부 results/ 나 임시 폴더로 간다.
    after = _test_fingerprint()
    added = sorted(set(after) - set(_TEST_BEFORE))
    changed = sorted(k for k in set(after) & set(_TEST_BEFORE)
                     if after[k] != _TEST_BEFORE[k])
    gone = sorted(set(_TEST_BEFORE) - set(after))
    check("test/ 를 건드리지 않았다", not (added or changed or gone),
          "새로 생김 %s · 바뀜 %s · 사라짐 %s" % (added, changed, gone))

    print()
    print("[15] `@시각` 우회 차단")
    # 예전엔 span 이 있으면 자막을 아예 안 봐서 **대사를 통째로 지어내도
    # 강등 0 · 경고 0** 으로 통과했다 (실측). 자막을 줬을 때만 대조한다.
    _d = script_io.read(ref, cues, log=lambda *_: None)
    _p = layout.build(_d, cues, 6159.104, fps=24.0, log=lambda *_: None)
    layout.apply_headers(_d, _p["headers"])
    layout.apply_spans(_d, _p)
    _frozen = script_io.write(_d)

    _ok = script_io.read(_frozen, cues, log=lambda *_: None)
    eq("정상 frozen — 불일치 0", _ok["stats"]["time_mismatch"], 0)
    eq("정상 frozen — 전부 대조됨", _ok["stats"]["time_ok"], _ok["stats"]["by_time"])
    eq("정상 frozen — 경고 0 (오탐 없음)", len(_ok["warnings"]), 0)

    _line = [l for l in _frozen.splitlines() if l.startswith("@")][0]
    _bad = _frozen.replace(_line, _line.split(" ", 1)[0] + " 완전히 지어낸 대사입니다", 1)
    _tam = script_io.read(_bad, cues, log=lambda *_: None)
    eq("대사를 지어내면 잡는다", _tam["stats"]["time_mismatch"], 1)
    check("경고 문구에 자막 원문이 있다",
          any("자막" in w and "다릅니다" in w for w in _tam["warnings"]))

    # 자막을 안 주면 지금과 완전히 동일하게 동작해야 한다 (자급자족이 이 형식의 목적)
    _no = script_io.read(_frozen, [], log=lambda *_: None)
    eq("자막 없으면 대조 안 함", _no["stats"]["time_ok"], 0)
    eq("자막 없으면 경고 0", len(_no["warnings"]), 0)

    # **layout 입력을 건드리면 안 된다** — it["cues"] 를 채우면 자막 노출이
    # PRE 만큼 밀리고 되감기 경고가 raise 로 승격된다
    _sp = [it for _, it in script_io.items(_ok) if it.get("span")]
    check("span 아이템이 실재", len(_sp) > 20, str(len(_sp)))
    eq("it['cues'] 를 안 채운다", sum(1 for it in _sp if it["cues"]), 0)
    check("결과는 새 키에만", all("span_ratio" in it for it in _sp))
    _a = layout.build(_ok, cues, 6159.104, fps=24.0, log=lambda *_: None)
    _b = layout.build(_no, [], 6159.104, fps=24.0, log=lambda *_: None)
    eq("컷 시각 불변",
       [round(x["src0"], 4) for x in _a["segments"]],
       [round(x["src0"], 4) for x in _b["segments"]])
    eq("자막 시각 불변",
       [round(x["t_start"], 4) for x in _a["subs"]],
       [round(x["t_start"], 4) for x in _b["subs"]])

    print("\n" + "=" * 60)
    if _fail:
        print("실패 %d건: %s" % (len(_fail), " / ".join(_fail)))
        return 1
    print("통과 %d건 — 전부 정상" % _pass)
    return 0


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
