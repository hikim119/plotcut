# -*- coding: utf-8 -*-
"""quality.py — 대본이 **내 문체인가**를 숫자로 잰다 (순수 함수, 디스크 안 만짐)

`cli.py check` 와 `pipeline` 이 같이 쓴다. 문자열 리스트만 받는 함수를 따로 둔 이유:
정답 3편의 나레이션 34문장을 그대로 넣어 테스트할 수 있어야 하기 때문이다.
`test/` 자산이 없는 PC(받아 쓴 사람)에서도 회귀가 돌아야 한다.

임계값은 전부 **정답 3편(father·gentleman·robber, 나레이션 34문장 / 421초)** 에서
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
FINAL_MAX = 0.05                   # 완결형 `~다.` — 정답 34문장에 0개
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
RUN_MAX = 2                        # 연속 나레이션 (정답 최대 2)
GAP_MAX = 60.0                     # 나레이션 없는 최장 구간 (정답 최대 50.9초)
MIN_SAMPLE = 6                     # 비율 지표는 이 개수 미만이면 안 잰다

# 정답이 쓰는 9개 + 도구가 쓰는 8개를 **같은 사전**에 넣는다 (편향 방지)
CONJ = ("그때", "하지만", "곧바로", "그 말에", "결국", "그렇게", "일단", "아까",
        "갑자기", "그런데", "그리고", "사실", "정작", "그제야", "그러자",
        "그러다", "얼마 전", "이윽고")


# 화면에 찍을 이름. `_ending()` 이 돌려주는 키와 짝이다.
ENDING_KO = {"neunde": "~는데", "jyo": "~죠", "conn": "~고",
             "final": "완결형", "noun": "명사"}


def _ending(t):
    """종결 분류. 정답 34문장 기준 ~는데 36 · ~고 30 · 명사 21 · ~죠 9 · 완결형 0."""
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
    """나레이션 **문자열 리스트**만 받는다 — 정답 34문장을 그대로 넣어 테스트한다."""
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
        "skew_kind": ENDING_KO[max(set(end), key=end.count)],
        "final_ratio": end.count("final") / n,
        "conj": sum(1 for t in nars if t.startswith(CONJ)) / n,
        "over": [t for t in nars if len(t) > LONG_CHARS],
    }


# ── 어디를 얼마나 썼는가 ────────────────────────────────────────────────────
# 완성본 3편을 **원본 자막과 짝지어** 실측했다 (대사 블록 32개 전수 대조).
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
