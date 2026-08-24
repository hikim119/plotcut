# -*- coding: utf-8 -*-
"""quality.py — 대본이 **내 문체인가**를 숫자로 잰다 (순수 함수, 디스크 안 만짐)

`cli.py check` 와 `pipeline` 이 같이 쓴다. 문자열 리스트만 받는 함수를 따로 둔 이유:
정답 3편의 나레이션 33문장을 그대로 넣어 테스트할 수 있어야 하기 때문이다.
`test/` 자산이 없는 PC(받아 쓴 사람)에서도 회귀가 돌아야 한다.

임계값은 전부 **정답 3편(father·gentleman·robber, 나레이션 33문장 / 421초)** 에서
역산했고, 값은 **정답 최댓값과 도구 출력 최솟값의 중점**에 뒀다. 코퍼스 실측:

    지표            정답 F/G/R        중경삼림 참고본   도구 출력 4편
    간격(초)        13.3 / 9.3 / 15.0      8.9        16.1 ~ 21.0
    중앙 길이       13 / 16 / 19           21         22 ~ 26
    21자 초과       17 / 25 / 20%          53%        60 ~ 82%
    명사 마무리     17 / 25 / 30%          32%        44 ~ 70%
    `~죠` 개수      1 / 1 / 1              3          전부 0
    마지막 문단     7 / 3 / 4자            19자       전부 19자

**정답 3편은 전부 통과하고 도구 출력은 각 7~8개가 걸린다.** 겹침이 없다.

문체 예시를 내 대본으로 바꾼 뒤 같은 자막으로 다시 뽑은 결과(실측):

    후 (새 예시)   n=13  중앙 17자 · 21자↑ 0% · 명사 23% · ~죠 1 · 13.7초  **전부 통과**
    전 (옛 예시)   n=10  중앙 25자 · 21자↑ 80% · 명사 60% · ~죠 0 · 18.0초  5건 걸림

주의 — 여기 없는 지표는 일부러 뺐다. 인물 지목률·나레이션 없는 블록 비율·
대사/나레이션 비율은 정답과 도구 출력을 **못 가른다**(실측). ⚠ 로 올리면 소음만 는다.
"""

import re
import statistics

# ── 임계값 ──────────────────────────────────────────────────────────────────
MEDIAN_LO, MEDIAN_HI = 11, 20      # 정답 13~19 · 도구 22~26
LONG_CHARS = 21                    # 이 글자수를 넘으면 "긴 줄"
LONG_RATIO_MAX = 0.40              # 정답 최대 25% · 도구 최소 60%
NOUNEND_MAX = 0.40                 # 정답 최대 30% · 도구 최소 44%
INTERVAL_LO, INTERVAL_HI = 6.0, 16.0   # 정답 9.3~15.0 · 도구 16.1~21.0
FINAL_MAX = 0.05                   # 완결형 `~다.` — 정답 33문장에 0개
TAIL_CHARS = 14                    # 마지막 문단 — 정답 7/3/4자 · 도구 19자
# 한 종결어미가 몰리면 낭독이 단조로워진다. **`~고` 하나만 재면 안 갈린다** —
# 정답 father 가 45%로 도구(43%)보다 높다. 그래서 `~고`가 아니라 **최빈 어미**에
# 걸고, 값은 정답 최대(45%)와 잡아야 할 출력(58%) 사이에 둔다.
#
#     정답 father     ~고5 ~는데3 명사2 ~죠1   최빈 45%
#     정답 gentleman  ~는데5 ~고4 명사2 ~죠1   최빈 42%
#     정답 robber     ~는데4 명사3 ~고2 ~죠1   최빈 40%
#     도구 출력       ~고7 명사3 ~죠1 ~는데1   최빈 58%   ← 잡을 것
#
# 표본이 10~12문장이라 **한 문장이 8~10%p** 다. father 가 `~고` 하나만 늘어도
# 55%가 된다 — 그래서 ✘ 가 아니라 ⚠ 다. 고치라는 게 아니라 알려 주는 값이다.
SKEW_MAX = 0.50
ITEM_MIN, ITEM_MAX = 5, 34         # 한 덩어리 (정답 7~31)
RUN_MAX = 2                        # 연속 나레이션 (정답 최대 2) — `cli` 가 쓴다
# 잰 적은 있으나 **게이트로 안 붙인 둘** — 없앤 이유를 남긴다.
#  · 나레이션 없는 최장 구간(정답 최대 50.9초): 평균 간격 게이트(INTERVAL_HI=16)가
#    구멍이 커지면 같이 커져서 실제로 먼저 걸린다. 43초 구멍을 만들어 봤는데
#    정답 최대 안이라 걸 이유가 없었다.
#  · 비율 지표의 표본 하한(6): 나레이션 2개짜리 대본을 정답 문체로 써서 돌려도
#    문체 ✘ 0건이었다 — 작은 표본이 헛발을 딛지 않는다. 붙일 근거가 없다.

# 정답이 쓰는 9개 + 도구가 쓰는 8개를 **같은 사전**에 넣는다 (편향 방지)
CONJ = ("그때", "하지만", "곧바로", "그 말에", "결국", "그렇게", "일단", "아까",
        "갑자기", "그런데", "그리고", "사실", "정작", "그제야", "그러자",
        "그러다", "얼마 전", "이윽고")


# 화면에 찍을 이름. `_ending()` 이 돌려주는 키와 짝이다.
ENDING_KO = {"neunde": "~는데", "jyo": "~죠", "conn": "~고",
             "final": "완결형", "noun": "명사"}


def _ending(t):
    """종결 분류. 정답 33문장 기준 ~는데 36 · ~고 33 · 명사 21 · ~죠 9 · 완결형 0."""
    t = t.strip().rstrip(".")
    if t.endswith("는데") or t.endswith("은데"):
        return "neunde"
    if t.endswith("죠"):
        return "jyo"
    if re.search(r"(고|자|며|채|만|지만)$", t):
        return "conn"
    if re.search(r"(다|요|까|래|네)$", t):
        return "final"
    return "noun"          # 관형형 + 명사 마무리


def narration_metrics(nars):
    """나레이션 **문자열 리스트**만 받는다 — 정답 33문장을 그대로 넣어 테스트한다."""
    n = len(nars)
    if not n:
        return {"n": 0}
    L = [len(t) for t in nars]
    end = [_ending(t) for t in nars]
    return {
        "n": n,
        "median": int(statistics.median(L)),
        "mean": round(sum(L) / n, 1),
        "max": max(L),
        "min": min(L),
        "long_ratio": sum(1 for x in L if x > LONG_CHARS) / n,
        "nounend": end.count("noun") / n,
        "neunde": end.count("neunde") / n,
        "conn": end.count("conn") / n,
        "jyo": end.count("jyo"),
        "skew": max(end.count(k) for k in set(end)) / n,
        # `max(set(...))` 은 동률일 때 set 순회 순서가 이기는데, 문자열 해시는
        # 프로세스마다 랜덤이라 **같은 대본을 두 번 검사하면 출력이 달라졌다**
        # (실측 8회에 `~고`/`~는데` 둘 다 나왔다). 정렬해 순서를 고정한다.
        "skew_kind": ENDING_KO[max(sorted(set(end)), key=end.count)],
        "final_ratio": end.count("final") / n,
        "conj": sum(1 for t in nars if t.startswith(CONJ)) / n,
        "over": [t for t in nars if len(t) > LONG_CHARS],
    }


# ── 대사 번역 ───────────────────────────────────────────────────────────────
# 나레이션과 **규범이 다르다.** 화면에 나가는 줄의 83%가 대사인데 여기엔 임계값이
# 하나도 없었다. 완성본 3편 전수 실측 (대사 248줄 / 나레이션 33문장):
#
#     지표                대사              나레이션
#     한 줄 중앙          10자 (평균 10.3)   16자
#     한 줄 최대          19자 (20자↑ 0줄)   31자
#     화면 한 줄 상한     19자              14자
#     `?` `!` 로 끝       75줄 (30%)        0줄 (0%)
#     쉼표 든 줄          1줄 (0.4%)        —
#     따옴표 · 라틴       0 · 0             —
#
# 같은 영화 3편의 **원본 영어 자막 6,249줄**과 견주면 (= 직역하면 되는 꼴):
#
#     라틴 4연속 또는 한글 0글자   대사 0.0%  ·  영어 100%   ← 겹침 0
#     쉼표 든 줄                  대사 0.4%  ·  영어  27%
#     한 줄 중앙                  대사 10자  ·  영어  21자
#     전체 글자                   한글은 영어의 0.24배
#
# **번역 경로에서만 잰다.** 한국어 자막이면 대사가 자막 원문이라(AGENTS.md 규칙 6)
# 사람이 못 고친다 — 켜면 고칠 수 없는 ✘ 만 쌓인다. 재는 대상도 한 번 더 좁힌다:
# `script_io.authored_lines` 가 `@시각`만 붙은 frozen 문단을 빼 준다.
#
# 판정을 `cli` 가 아니라 여기 두는 이유: 나레이션은 판정이 `cli.py` 에 있어서
# selftest 가 `_style_gate` 로 **복사본**을 들고 있다. 그 복사본은 cli 가 바뀌어도
# 안 따라온다. 같은 실수를 되풀이하지 않는다.
#
# 안 재는 것 —
#   · **따옴표**: 정답 0개인데 원본 영어도 0.2%뿐이다. 못 가른다.
#   · **한 큐 줄 수**: 247큐 중 246이 1줄이지만 `script_io` 의 `overstuffed` 가
#     이미 "문단 줄 > 큐 줄"을 ✘ 로 잡는다. 남는 건 2줄 큐를 2줄로 옮긴 경우인데
#     §포맷이 그걸 **허용**하고 정답에도 1건 있다.
#   · **어절 수**(1~6, 중앙 3): 글자 수와 상관이 너무 높다. 겹치면 소음만 는다.
#   · **줄 길이 하한**: 짧은 건 문제가 아니다(나레이션과 같은 규율). "네/응"만
#     늘어놓는 회피는 목표 길이(±15%)가 잡는다.
DLG_LINE_MAX = 22       # 한 줄 상한. 정답 248줄 최대 19 · 20자 이상 0줄.
                        # 꼬리가 15:13 → 17:5 → 19:2 로 반씩 주니 22는 안 나온다
DLG_MEDIAN_MAX = 14     # 중앙 상한. 정답 10/10/11. 나레이션 화면 줄 상한이 14인데
                        # 대사 중앙이 그 위로 가면 규범이 바뀐 것이다
DLG_MARK_MIN = 0.10     # `?`·`!` 로 끝난 줄. 정답 27/24/39% · 나레이션 0%.
                        # 중점은 12%인데 편별로 15%p 흔들려 10%로 내렸다
DLG_COMMA_MAX = 0.10    # 쉼표 든 줄. 정답 0.4% · 원본 영어 27%. 중점 13.7% → 10%
DLG_MIN_SAMPLE = 20     # 비율 지표는 이 개수 미만이면 안 잰다 (정답 최소 58줄)

# 자모를 자음(ㄱ~ㅎ)만 넣으면 `ㅋㅋ` 는 한국어인데 `ㅠㅠ` 는 아닌 게 된다.
# 모음(ㅏ~ㅣ)까지 넣어 자모 블록 전체를 본다.
_HANGUL = re.compile(r"[가-힣ㄱ-ㅣ]")   # 음절 + 자모(자음·모음)
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")


def untranslated(line):
    """번역이 안 남은 줄인가. 정답 248줄 **0건** · 원본 영어 6,249줄 **전수** 적중.

**한글이 한 글자도 없거나**, 라틴 **단어가 셋 이상**이면 안 옮긴 줄이다.

    「라틴 4글자 연속」이던 조건을 버렸다 — **제대로 옮긴 줄을 걸었다.** 실측 오탐:
    `Marlboro 한 갑 줘` · `California 로 갔어` · `Xbox 사줄게` · `Star Trek 봤어`.
    브랜드·지명·작품명은 번역해도 로마자로 남는다. 게다가 이건 이 파일의 **유일한
    ✘** 라 걸리면 `check` 가 exit 1 이고, 사람이 고칠 방법이 번역을 망가뜨리는
    것뿐이었다.

    단어 셋이 기준인 이유: 영어 문장은 단어가 여럿이고 고유명사는 하나둘이다.
    실측 — 정답 248줄 오탐 **0개**, 원본 영어 6,249줄 **100% 적중**(옛 조건과 같다).
    `I go.` 처럼 짧은 영어는 「한글 0글자」가 잡는다.
    """
    return (not _HANGUL.search(line)) or len(_LATIN_WORD.findall(line)) >= 3


def dialogue_metrics(lines):
    """**사람이 한국어로 쓴 대사 줄** 문자열 리스트만 받는다 — 정답 248줄을
    그대로 넣어 테스트한다(`narration_metrics` 와 같은 규율).

    문단이 아니라 **줄** 단위인 이유: 화면은 한 줄이 한 카드이고(`layout` 이 줄마다
    자막을 하나씩 낸다), 문단 줄 수는 `overstuffed` 가 이미 본다.
    """
    n = len(lines)
    if not n:
        return {"n": 0}
    L = [len(t) for t in lines]
    return {
        "n": n,
        "median": int(statistics.median(L)),
        "mean": round(sum(L) / n, 1),
        "max": max(L), "min": min(L),
        "over": [t for t in lines if len(t) > DLG_LINE_MAX],
        # `""[-1:]` 은 `""` 고 `"" in "?!"` 는 True 다 — 빈 줄이 섞이면 mark 가
        # 위로 밀려 경고가 가려진다. 빈 줄을 먼저 걷어낸다.
        "mark": sum(1 for t in lines
                    if t.rstrip() and t.rstrip()[-1] in "?!") / n,
        "comma": sum(1 for t in lines if "," in t) / n,
        "foreign": [t for t in lines if untranslated(t)],
    }


def dialogue_issues(m):
    """→ (errors, warns, notes). **번역 경로에서만** 부른다.

    ✘ 는 `foreign` 하나뿐이다. 나머지가 ⚠ 인 이유는 **실패한 한국어 번역 표본이
    없어서**다 — 네거티브가 원본 영어 자막뿐이라 임계를 중점에 못 놓는다. 나중에
    실제로 못 쓴 번역 대본이 나오면 그 최솟값으로 다시 잡아 ✘ 로 올릴 수 있다.
    """
    errors, warns, notes = [], [], []
    if not m.get("n"):
        return errors, warns, notes
    if m["foreign"]:
        errors.append(
            "한국어로 안 옮긴 대사 줄이 %d개입니다 — 그대로 두면 외국어가 화면에 "
            "나갑니다." % len(m["foreign"]))
    if m["median"] > DLG_MEDIAN_MAX:
        warns.append(
            "대사 줄 중앙이 %d자입니다 (기준 %d 이하 · 내 대본 10자) — "
            "직역하면 길어집니다. 말을 줄이세요."
            % (m["median"], DLG_MEDIAN_MAX))
    if m["over"]:
        warns.append(
            "%d자 넘는 대사 줄이 %d개입니다 (내 대본 248줄 최대 19자)."
            % (DLG_LINE_MAX, len(m["over"])))
    if m["n"] >= DLG_MIN_SAMPLE and m["mark"] < DLG_MARK_MIN:
        warns.append(
            "`?`·`!` 로 끝난 대사가 %.0f%%뿐입니다 (내 대본 30%% · 나레이션은 0%%) "
            "— 대사를 나레이션처럼 평서문으로 옮겼습니다." % (100 * m["mark"]))
    if m["n"] >= DLG_MIN_SAMPLE and m["comma"] > DLG_COMMA_MAX:
        warns.append(
            "쉼표 든 대사 줄이 %.0f%%입니다 (내 대본 0.4%% · 영어 원문 27%%) "
            "— 쉼표 자리면 줄을 끊으세요." % (100 * m["comma"]))
    notes.append(
        "대사 번역 %d줄 · 중앙 %d자 (평균 %.1f · 최대 %d) · `?`·`!` 끝 %.0f%% · "
        "쉼표 %.0f%%"
        % (m["n"], m["median"], m["mean"], m["max"],
           100 * m["mark"], 100 * m["comma"]))
    return errors, warns, notes


# ── 어디를 얼마나 썼는가 ────────────────────────────────────────────────────
# 완성본 3편을 **원본 자막과 짝지어** 실측했다 (대사 블록 32개 전수 대조 —
# 소스 구간은 33개다. 한 블록이 7분 떨어진 두 구간을 이어 붙였다).
#
#     대본        영화     숏츠    쓴 구간   영화 대비   시작~끝     그 안의 장면
#     father      22분     160초   18.5분     82%       9% ~ 92%      11개
#     gentleman  111분     111초   23.6분     1/5      34% ~ 55%      12개
#     robber      97분     150초   12.3분     1/8      13% ~ 25%      10개
#
# 이 3편은 **영화 한 대목만** 다뤘고 결말도 안 썼다. 한때 그걸 `scene` 모드로
# 지원했지만 뺐다 — 사용자가 **결말이 있는 전체 요약이 낫다**고 판단했다.
# 위 숫자는 지우지 않는다. 그 3편이 여전히 **문체의 정답**이고, 나중에 왜
# 뺐는지 알려면 근거가 남아 있어야 한다.
#
# 그래서 지금 검사는 하나다: **결말까지 갔는가.**
ENDING_RATIO = 0.70    # 마지막 장면이 이 앞이면 결말 누락 의심

# 안 재는 것: **사용 구간 안 채택 밀도**(정답 18/30/60% · 도구 15%)와
# **나레이션 한 문장이 건너뛰는 최장 시간**(정답 123~630초 · 도구 414초).
# 둘 다 정답과 도구가 겹친다 — ⚠ 로 올리면 소음만 는다. 참고로만 찍는다.
# **쓴 구간 폭**도 안 잰다 — 전체 요약이면 영화 전체를 훑는 게 정상이다.


def selection_metrics(times, movie_s=None):
    """대사 문단이 가리키는 원본 시각들 → 어디를 얼마나 썼는지.

    times   원본 영화 기준 초. None 은 걸러낸다 (시각도 자막도 못 찾은 문단).
    movie_s 영화 길이(초). 없으면 비율 항목이 빠진다.
    """
    ts = sorted(t for t in times if t is not None)
    if not ts:
        return {}
    lo, hi = ts[0], ts[-1]
    m = {"n": len(ts), "lo": lo, "hi": hi,
         "span_s": hi - lo, "span_min": (hi - lo) / 60.0}
    if len(ts) > 1:
        m["max_jump_s"] = max(ts[i + 1] - ts[i] for i in range(len(ts) - 1))
    if movie_s:
        m["movie_s"] = movie_s
        m["start_pct"] = 100.0 * lo / movie_s
        m["end_pct"] = 100.0 * hi / movie_s
        m["cover"] = (hi - lo) / movie_s
    return m


def selection_issues(m):
    """결말 판정 → (warns, notes). ✘ 는 안 낸다 — 사람이 일부러 그럴 수 있다."""
    warns, notes = [], []
    if not m:
        return warns, notes
    if "start_pct" in m:
        notes.append("쓴 구간 %.1f분 (영화의 %.0f%% ~ %.0f%% 지점)"
                     % (m["span_min"], m["start_pct"], m["end_pct"]))
    else:
        notes.append("쓴 구간 %.1f분" % m["span_min"])
    if "end_pct" in m and m["end_pct"] < ENDING_RATIO * 100:
        warns.append("마지막 장면이 영화의 %.0f%% 지점입니다 — 결말이 빠졌을 수 있습니다."
                     % m["end_pct"])
    return warns, notes
