"""
wrap.py — 나레이션 문장을 화면에 낼 **조각**으로 나눈다 (LineWrapper 로직 이식)

LineWrapper와 다른 점 두 가지 — 둘 다 의도한 것이다.

1. 언어 판정을 **beat(문장)별로** 한다.
   LineWrapper의 format_script()는 전체 텍스트를 한 번만 판정하기 때문에
   한국어 대본 안에 가나 한 글자만 섞여도 전체가 일본어(글자 단위) 모드로 넘어간다.

2. wrap 결과에서 **빈 줄을 반드시 걸러낸다.**
   _pack_cjk는 루프 끝에서 무조건 lines.append(cur)를 하므로 마지막에 빈 줄이
   붙을 수 있다. 빈 줄이 남으면 자막 세그먼트가 비고, 줄 수 기반 매핑이 밀린다.
"""

# ── 나레이션 조각 ───────────────────────────────────────────────────────────
# 완성 숏츠 3편의 나레이션 33문장이 화면에서 어떻게 나뉘는지 전수 실측:
#
#     조각 수    1조각 11문장 · 2조각 17 · 3조각 3 · 4조각 1  (문장당 1.7)
#     첫 조각    중앙 9자 · 범위 6~14 · **최빈 8자**
#     나머지     중앙 8자 · 범위 5~13
#     화면 한 줄 중앙 8자 · 최대 14자 · **15자 초과 0줄** (58줄 전수)
#     5자 미만   **0개**
#
# 아래 세 값이 그 분포를 재현한다 (캘리브레이션 표로 확인):
#   조각 {1:11, 2:18, 3:3, 4:1} · 첫 최빈 8 · 나머지 중앙 8 · 15자↑ 0 · 5자↓ 0
# 그리고 유저 실측 4예를 **글자까지 그대로** 낸다:
#   `갑작스런 비보를 받게 된 할` → 갑작스런 비보를(8) │ 받게 된 할(6)
#   `보다못한 말콤은 궁극기를 시전하기로 했고` → 보다못한 말콤은(8) │ 궁극기를 시전하기로 했고(13)
#
# 값을 만지지 마라 — 셋이 얽혀 있다. WHOLE 을 14로 올리면 1조각이 11 → 13이 되고,
# HEAD 를 8로 낮추면 4조각 문장이 사라진다. 고칠 일이 생기면 표를 다시 찍어라.
NARR_WHOLE = 13   # 이보다 짧으면 안 자른다 (33문장 중 13자 이하가 정확히 11문장)
NARR_HEAD = 9     # 앞에서부터 이만큼씩 묶는다
NARR_LINE = 14    # 조각 한 줄 상한. 꼬리를 다시 묶을 때의 한도
NARR_SPLIT = True  # False 면 문장을 통째로 낸다 — 조각내기 이전 그림으로 되돌리기

# 예전 이름. **12는 측정된 값이 아니라** LineWrapper 에서 그대로 물려받은 것이고
# 근거가 코드에도 문서에도 없었다. 실측은 14다(나레이션 큐 58개 전수).
LIMIT = NARR_LINE


def _length(s, count_spaces):
    """줄 길이 계산. count_spaces=False면 공백을 제외한 글자 수."""
    return len(s) if count_spaces else len(s.replace(" ", ""))


def wrap_clause(clause, limit=LIMIT, count_spaces=True):
    """한 구절을 단어 단위로 limit 이내 줄들로 나눈다. 단어는 절대 안 쪼갬."""
    words = clause.split()
    lines = []
    cur = ""
    for w in words:
        cand = w if cur == "" else cur + " " + w
        if _length(cand, count_spaces) <= limit:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w            # limit보다 긴 단어면 홀로 한 줄 (쪼갤 수 없으므로)
    if cur:
        lines.append(cur)
    return lines


# 줄 맨 앞에 오면 안 되는 글자 (일본어 줄머리 금칙) — 앞 줄 끝에 붙여 처리
LINE_START_NG = set(
    "、。，．・：；！？!?｡､）〕〉》」』】〙〗）］｝‐゠〜～…‥"
    "ぁぃぅぇぉっゃゅょゎゕゖ"
    "ァィゥェォッャュョヮヵヶ"
    "ーゝゞ々ヽヾ"
    "”’＂＇"
)


def _display_len(s):
    """일본어 폭 계산 — 각 글자 1칸(전각 기준), 공백 제외."""
    return len(s.replace(" ", ""))


def wrap_cjk(text, limit=LIMIT):
    """공백 없는 일본어용: 글자 수 기준으로 limit 이내 줄들로 나눈다.
    줄머리 금칙 글자는 앞 줄 끝에 끌어붙여 다음 줄 맨 앞에 오지 않게 한다."""
    chars = list(text)
    lines, cur, i, n = [], "", 0, len(chars)
    while i < n:
        ch = chars[i]
        if cur == "" and ch.isspace():
            i += 1
            continue                       # 줄 맨 앞 공백은 버림
        if _display_len(cur) < limit:
            cur += ch
            i += 1
            continue
        # 줄이 꽉 참 — 다음 글자가 줄머리 금칙이면 이 줄 끝에 끌어붙임
        pulled = 0
        while i < n and chars[i] in LINE_START_NG and pulled < 4:
            cur += chars[i]
            i += 1
            pulled += 1
        lines.append(cur)
        cur = ""
    if cur:
        lines.append(cur)                  # LineWrapper와 달리 빈 줄은 넣지 않는다
    return lines


def is_japanese(text):
    """가나(히라가나/가타카나)가 있으면 일본어로 판단. 한국어(한글)와 자동 구분."""
    for ch in text:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF or 0xFF66 <= o <= 0xFF9D:
            return True
    return False


def detect_lang(text):
    """'ja' | 'ko' — beat별 판정 결과를 plan.json에 기록해 재현 가능하게 한다."""
    return "ja" if is_japanese(text) else "ko"


def wrap_lines(narration, limit=LIMIT, lang=None):
    """문장 하나 → 12자 이내 줄 리스트. 빈 줄은 제거된다."""
    if lang is None:
        lang = detect_lang(narration)
    if lang == "ja":
        lines = wrap_cjk(narration, limit)
    else:
        lines = wrap_clause(narration, limit, count_spaces=True)
    return [ln for ln in (s.strip() for s in lines) if ln]


def split_narration(text, whole=NARR_WHOLE, head=NARR_HEAD, line=NARR_LINE,
                    lang=None):
    """나레이션 문장 하나 → 화면에 **차례로** 띄울 조각들. 항상 1개 이상.

    `wrap_beat` 을 못 쓰는 이유: 그건 줄바꿈을 넣은 **문자열 하나**를 돌려준다.
    조각은 각자 자기 시각을 가져야 하므로 **리스트**여야 한다.

    앞에서부터 `head` 자씩 묶은 뒤, 조각이 셋 이상이면 **꼬리만 다시 묶어**
    조각 수를 줄인다. 그냥 greedy 로 두면 앞을 꽉 채우고 꼬리가 2자로 남는데,
    유저 실측 47조각에는 **5자 미만이 하나도 없다.**
    """
    text = (text or "").strip()
    if not NARR_SPLIT or not text or len(text) <= whole:
        return [text] if text else []
    parts = wrap_lines(text, limit=head, lang=lang)
    if len(parts) >= 3:
        glue = "" if (lang or detect_lang(text)) == "ja" else " "
        out = [parts[0]]
        for pp in parts[1:]:
            cand = out[-1] + glue + pp
            if len(out) > 1 and len(cand) <= line:
                out[-1] = cand
            else:
                out.append(pp)
        parts = out
    # 발음할 글자가 없는 조각(문장부호만)은 앞에 붙인다 — 홀로 두면 발화
    # 가중치가 0이라 시각이 앞 조각과 겹치고 `layout._stack` 이 통째로 버린다.
    import timing
    merged = []
    for pp in parts:
        if merged and timing.is_silent(pp):
            merged[-1] = merged[-1] + pp
        else:
            merged.append(pp)
    return [x for x in (y.strip() for y in merged) if x] or [text]


def wrap_beat(narration, limit=LIMIT, lang=None):
    """문장 하나 → 자막 세그먼트에 넣을 '줄바꿈 포함 문자열'.

    이 결과를 plan.json의 sub_text에 문자열 그대로 저장한다. 나중에 재계산하지
    않기 때문에 limit이 바뀌거나 언어 판정이 달라져도 이미 만든 프로젝트의
    자막이 밀리지 않는다.
    """
    lines = wrap_lines(narration, limit, lang)
    return "\n".join(lines) if lines else narration.strip()


def wrap_script(narrations, limit=LIMIT, blank_between=True):
    """전체 대본 → 타입캐스트에 붙여넣을 텍스트.

    blank_between=True면 beat 사이에 빈 줄을 넣는다. TTS가 문장 사이에서 더 긴
    쉼을 주도록 유도해 정렬 청크 수(M)를 확보하기 위한 것이다.
    ★ 매핑은 이 빈 줄에 절대 의존하지 않는다 — LineWrapper의 내보내기가
      빈 줄을 전부 제거하므로 왕복 한 번에 사라진다.
    """
    blocks = ["\n".join(wrap_lines(n, limit)) for n in narrations]
    blocks = [b for b in blocks if b]
    return ("\n\n" if blank_between else "\n").join(blocks)


def max_line_len(text):
    """가장 긴 줄의 길이 (검수 표시용)."""
    return max((len(ln) for ln in text.splitlines()), default=0)
