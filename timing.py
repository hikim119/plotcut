"""
timing.py — 발화 길이 추정 (pydub 의존 없음)

align.py의 글자 가중치 로직을 여기로 옮겼다. TTS가 없는 "추정 타이밍" 경로가
ffmpeg/pydub을 요구하지 않게 하려면 이 모듈이 순수해야 한다.
align.py는 여기서 import 한다.

단위(unit) = 발화 길이 근사치. 한국어 음절 1.0 / 일본어 가나 1.0 / 한자 2.0.
"""

# ── 확정 상수 (근거: PlotCut 계획서 §확정 상수) ──────────────────────────────
# test_TTS.mp3 86.852초 ÷ weight 743.0 = 116.9 ms/단위
# 산출 SRT 60큐 = 123.2 ms/단위  →  기하평균 √(116.9×123.2) = 120.0
MS_PER_UNIT     = 120.0   # 기본값
MS_PER_UNIT_MIN = 90.0    # 캘리브레이션·이상치 밴드 하한 (실측 min 109)
MS_PER_UNIT_MAX = 200.0   # 상한 (실측 max 150)

MIN_BEAT_SEC = 0.5        # 추정 경로에서만 쓰는 하한. TTS 경로에서는 클램프 금지
TAIL_S       = 0.3        # 마지막 beat 뒤 여유 (srt_export.py와 동일 값)

SUSPECT_MIN_SEC    = 0.4  # 이보다 짧은 beat는 검수 목록
SUSPECT_MIN_WEIGHT = 3.0  # 이보다 가벼운 beat는 검수 목록


# 요음·소문자 가나 (앞 글자와 합쳐 1모라 → 가중치 낮음)
_SMALL_KANA = set("ぁぃぅぇぉゃゅょゎゕゖァィゥェォャュョヮヵヶ")


def _speech_weight(ch):
    """글자 1개의 발화 길이 근사 가중치 (한국어 음절 / 일본어 모라 기준)."""
    o = ord(ch)
    if ch in _SMALL_KANA:                              return 0.3
    if 0xAC00 <= o <= 0xD7A3:                          return 1.0   # 한글 음절
    if 0x3040 <= o <= 0x30FF:                          return 1.0   # 히라가나·가타카나
    if (0x3400 <= o <= 0x9FFF) or (0xF900 <= o <= 0xFAFF):
        return 2.0                                                  # 한자 (평균 ~2모라)
    if ch.isascii() and ch.isalpha():                  return 0.4   # 라틴 문자
    if ch.isdigit():                                   return 1.2   # 숫자
    return 0.0                                                      # 공백·문장부호 등


def raw_weight(text):
    """클램프 없는 순수 가중치 합. 0.0이면 발음할 것이 하나도 없는 문장."""
    return sum(_speech_weight(c) for c in text)


def _char_weight(sentence):
    """발화 길이 추정 가중치. 최소 1.0 (align.py 하위호환 — 0 나눗셈 방지)."""
    return max(1.0, raw_weight(sentence))


# 공개 별칭 (align.py의 밑줄 이름을 그대로 쓰지 않도록)
char_weight = _char_weight


def is_silent(text):
    """발음할 글자가 하나도 없는가. True면 beat로 쓸 수 없다.

    이걸 통과시키면 align.load_script가 빈 줄을 제거하거나 wrap이 빈 리스트를
    내면서 beat 개수와 정렬 줄 개수가 어긋나 인덱스가 전부 밀린다.
    """
    return raw_weight(text) <= 0.0


# ── 초 ↔ 단위 변환 ───────────────────────────────────────────────────────────

def units_for_seconds(seconds, ms_per_unit=MS_PER_UNIT):
    """목표 길이(초) → 발화 예산(단위). LLM 프롬프트에 넣는 숫자."""
    return seconds * 1000.0 / ms_per_unit


def seconds_for_units(units, ms_per_unit=MS_PER_UNIT):
    """단위 → 예상 초."""
    return units * ms_per_unit / 1000.0


def seconds_for_text(text, ms_per_unit=MS_PER_UNIT):
    """문장 → 예상 발화 시간(초)."""
    return seconds_for_units(_char_weight(text), ms_per_unit)


def clamp_ms_per_unit(value):
    """캘리브레이션 결과를 실측 밴드로 클램프."""
    return min(MS_PER_UNIT_MAX, max(MS_PER_UNIT_MIN, float(value)))


def calibrate_ms_per_unit(total_seconds, total_units):
    """실제 TTS 길이로 ms/단위를 역산 (다음 실행의 추정 정확도 향상).

    total_seconds: 정렬된 마지막 문장의 end_s
    total_units:   전체 문장 가중치 합
    """
    if total_units <= 0:
        return MS_PER_UNIT
    return clamp_ms_per_unit(total_seconds * 1000.0 / total_units)


# ── 추정 타이밍 (TTS 없는 경로) ──────────────────────────────────────────────

def estimate_timeline(texts, ms_per_unit=MS_PER_UNIT, min_beat_s=MIN_BEAT_SEC,
                      tail_s=TAIL_S):
    """TTS 없이 글자수만으로 beat별 (start_s, end_s)를 만든다.

    이 경로에는 오디오 트랙이 없어 컷·자막이 같은 값에서 파생되므로
    하한 클램프(min_beat_s)가 안전하다. TTS 경로에서는 절대 클램프하지 않는다.

    반환: [{"line","text","start_s","end_s","dur_s","chars"}] — align.process()의
    aligned와 같은 모양이라 이후 코드가 두 경로를 구분하지 않아도 된다.
    """
    out = []
    pos = 0.0
    for i, text in enumerate(texts):
        w = _char_weight(text)
        dur = max(min_beat_s, seconds_for_units(w, ms_per_unit))
        out.append({
            "line": i + 1, "text": text,
            "start_s": pos, "end_s": pos + dur,
            "dur_s": dur, "chars": w,
        })
        pos += dur
    if out:
        out[-1]["end_s"] = out[-1]["start_s"] + out[-1]["dur_s"]
    return out


def suspect_indices(aligned):
    """검수가 필요한 beat 인덱스 (0-based).

    - 너무 짧다 (dur_s < 0.4초)
    - 가중치가 너무 작다 (weight < 3.0)
    - ms/단위가 실측 밴드(90~200) 밖이다
    """
    bad = []
    for i, a in enumerate(aligned):
        chars = max(1e-9, a.get("chars", 0.0))
        per = a["dur_s"] * 1000.0 / chars
        if (a["dur_s"] < SUSPECT_MIN_SEC or chars < SUSPECT_MIN_WEIGHT
                or per < MS_PER_UNIT_MIN or per > MS_PER_UNIT_MAX):
            bad.append(i)
    return bad


def out_of_band_ratio(aligned):
    """ms/단위가 밴드 밖인 beat 비율 (0.0~1.0). 0.2 초과면 정렬 품질 의심."""
    if not aligned:
        return 1.0
    n = 0
    for a in aligned:
        chars = max(1e-9, a.get("chars", 0.0))
        per = a["dur_s"] * 1000.0 / chars
        if per < MS_PER_UNIT_MIN or per > MS_PER_UNIT_MAX:
            n += 1
    return n / len(aligned)


def format_table(aligned):
    """문장별 타이밍 검수 테이블 (단위당 발화시간 이상치 눈검사용)."""
    lines = ["  줄 |    시작 ~    끝   | 길이(s) | 단위 | ms/단위 | 문장"]
    lines.append("  " + "─" * 78)
    for a in aligned:
        chars = max(1e-9, a.get("chars", 0.0))
        per_char = a["dur_s"] * 1000.0 / chars
        flag = " ⚠" if per_char < MS_PER_UNIT_MIN or per_char > MS_PER_UNIT_MAX else ""
        text = a["text"][:24] + ("…" if len(a["text"]) > 24 else "")
        lines.append(f"  {a['line']:3d} | {a['start_s']:7.2f} ~ {a['end_s']:7.2f} |"
                     f" {a['dur_s']:6.2f} | {chars:4.0f} | {per_char:6.0f}{flag} | {text}")
    return "\n".join(lines)
