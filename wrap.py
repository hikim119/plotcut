"""
wrap.py — 나레이션 문장을 12자 이내로 줄바꿈 (LineWrapper 로직 이식)

LineWrapper와 다른 점 두 가지 — 둘 다 의도한 것이다.

1. 언어 판정을 **beat(문장)별로** 한다.
   LineWrapper의 format_script()는 전체 텍스트를 한 번만 판정하기 때문에
   한국어 대본 안에 가나 한 글자만 섞여도 전체가 일본어(글자 단위) 모드로 넘어간다.

2. wrap 결과에서 **빈 줄을 반드시 걸러낸다.**
   _pack_cjk는 루프 끝에서 무조건 lines.append(cur)를 하므로 마지막에 빈 줄이
   붙을 수 있다. 빈 줄이 남으면 자막 세그먼트가 비고, 줄 수 기반 매핑이 밀린다.
"""

LIMIT = 12   # 한 줄 최대 글자 수


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
