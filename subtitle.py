"""
subtitle.py — 영화 자막 파싱 → 대사 큐 → 대사 블록 → LLM 입력 digest

지원: .srt (SubRip) · .smi (SAMI) · .vtt (WebVTT) · .ass/.ssa (SubStation Alpha)

핵심 아이디어: 자막에는 이미 타임코드가 있으므로 영상 분석이 전혀 필요 없다.
큐 인덱스가 곧 영화 시각의 주소가 되고, LLM은 "몇 번 큐를 썼는지"만 답하면 된다.
영화 초를 LLM이 직접 찍게 하지 않는 이유 — 환각으로 엉뚱한 시각이 나오면
사람이 검수 없이는 못 잡는다. 큐 인덱스는 파이썬이 검증할 수 있다.
"""

import html
import json
import re
from pathlib import Path

from timing import is_silent

# ── 파라미터 ────────────────────────────────────────────────────────────────
BLOCK_GAP_S      = 7.0    # 큐 사이 간격이 이 이상이면 새 대사 블록
BLOCK_MAX_S      = 120.0  # 블록이 이보다 길면 쪼갠다 (digest 가독성 + 컷 선택 해상도)
SILENT_GAP_S     = 20.0   # 이 이상 자막이 없는 구간 = 무성 구간으로 digest에 표기
CREDIT_SCAN      = 3      # 앞/뒤 이 개수까지만 제작자 크레딧으로 의심
FPS_RATIO_LO     = 0.96   # 자막 끝 / 영화 길이 비율 허용 범위
FPS_RATIO_HI     = 1.04

ENCODINGS = ("utf-8-sig", "cp949", "euc-kr", "utf-16", "latin-1")

# 제작자 크레딧 냄새 (앞/뒤 몇 개 큐에서만 적용)
_CREDIT_PAT = re.compile(
    r"(https?://|www\.|\.com|\.net|자막|字幕|제작|번역|싱크|sync\b|sub(?:title)?s?\s+by|"
    r"릴리[스즈]|배포|blog|cafe|tistory|naver|torrent)",
    re.IGNORECASE)

# 노래/음악 자막.
# 주의: 예전 패턴 `^[\s♪♬※【\[(]*(?:♪|♬|music|instrumental|노래)` 는
# "노래 몇 마디 한다고 가수가 될 것 같아?"(중경삼림 SRT #574) 같은 실제 대사를
# 통째로 삭제했다 — 그 파일의 유일한 music 히트이자 100% 오탐이었다.
# 음표가 있거나, 괄호로 감싼 음악 표기일 때만 버린다.
_MUSIC_PAT = re.compile(
    r"[♪♬]"
    r"|^\s*[\[(【][^\])】]*(?:music|instrumental|음악|노래|가사)[^\[(【]*[\])】]\s*$",
    re.IGNORECASE)

# 영어 CC 는 가사를 `# ... #` 로 감싼다 (♪ 대신). 한국어 자막에서는 오탐 0건(실측).
_LYRIC_PAT = re.compile(r"^\s*[#♪♬].*[#♪♬]\s*$")

# 효과음·상황 설명 자막. 큐 **전체**가 대괄호/괄호일 때만 버린다 —
# `[ Laughter ]` `[ Crowd cheering ]` 은 대사가 아니라 청각장애인용 설명이다.
# 영어 CC 2059큐 중 381개(18%)가 이것. 안 버리면 효과음이 대사로 대본에 들어간다.
_SFX_PAT = re.compile(r"^\s*[\[(]\s*[^\])]*[\])]\s*$")

# 화자 표시 `ALL:` `APRlL:` `WOMAN ON TV:` — 대사는 살리고 **접두어만** 뗀다.
# 대문자만 받는다(소문자가 섞이면 평범한 문장의 콜론이다). CC 특유의
# I→l OCR 오인식을 감안해 소문자 l 만 예외로 허용한다.
_SPEAKER_TAG = re.compile(r"^\s*[A-Z][A-Z0-9 '’.\-l]{0,22}:\s+")
# 화자 이름이 줄 하나를 통째로 쓰고 대사는 다음 줄에 오는 형태 (`ALL:` ⏎ `Go...`)
_SPEAKER_ONLY = re.compile(r"^\s*[A-Z][A-Z0-9 '’.\-l]{0,22}:\s*$")
# 대사 중간에 낀 설명 (`[ High-pitched ] April...`). 줄 전체가 대괄호면 손대지 않는다.
_CC_INLINE = re.compile(r"\[[^\]]{0,60}\]")

_TAG_PAT     = re.compile(r"<[^>]+>")
_BR_PAT      = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_SPEAKER_PAT = re.compile(r"^\s*[-–—]\s*")     # 대화 하이픈
_WS_PAT      = re.compile(r"[ \t 　]+")


# ── 인코딩 ──────────────────────────────────────────────────────────────────

def read_text(path):
    """자막 파일을 읽는다. 반환: (text, encoding).

    SMI는 cp949(한국어 윈도우)가 흔하고 SRT는 utf-8이 흔하다. 순차 시도하며,
    한글 자막을 latin-1로 잘못 읽는 사고를 막기 위해 한글/가나가 나오는지도 본다.
    """
    raw = Path(path).read_bytes()
    last = None
    for enc in ENCODINGS:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError) as e:
            last = e
            continue
        # latin-1은 무엇이든 성공하므로 마지막 수단으로만 채택
        if enc == "latin-1":
            return text, enc
        return text, enc
    raise UnicodeDecodeError("subtitle", raw[:16], 0, 1,
                             f"지원하는 인코딩으로 읽을 수 없습니다: {last}")


# ── 시각 파싱 ───────────────────────────────────────────────────────────────

_TS_PAT = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[,.](\d{1,3})")


def _parse_ts(s):
    """'01:23:45,678' / '23:45.678' → 초. 실패하면 None."""
    m = _TS_PAT.search(s)
    if not m:
        return None
    h = int(m.group(1) or 0)
    mm, ss = int(m.group(2)), int(m.group(3))
    frac = m.group(4)
    ms = int(frac.ljust(3, "0")[:3])
    return h * 3600 + mm * 60 + ss + ms / 1000.0


def _clean_lines(s):
    """자막 한 장을 **줄 구조를 살린 채** 정규화. 반환: [str] (빈 줄 없음).

    화면 줄바꿈은 버리면 복원할 수 없다. 「타임라인 대본」 txt는 원본 큐의
    줄바꿈을 그대로 쓰므로(실측: 893큐 중 212개가 2줄 이상) 여기서 접으면
    왕복이 깨진다. 평탄한 한 줄이 필요한 곳은 _clean_text 를 쓴다.
    """
    s = _BR_PAT.sub("\n", s)
    s = _TAG_PAT.sub("", s)
    s = html.unescape(s)
    s = s.replace("​", "").replace("﻿", "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for ln in s.split("\n"):
        ln = _SPEAKER_TAG.sub("", _SPEAKER_PAT.sub("", ln))
        ln = _SPEAKER_ONLY.sub("", ln)          # `ALL:` 만 있고 대사는 다음 줄
        if not _SFX_PAT.match(ln):
            ln = _CC_INLINE.sub(" ", ln)        # `[ High-pitched ] April...`
        ln = _WS_PAT.sub(" ", ln).strip()
        if ln:
            out.append(ln)
    # 줄 단위로 효과음·가사를 걷어낸다. 단 **전부 그런 줄이면 그대로 둔다** —
    # 큐 전체가 효과음인 경우는 _clean_cues 가 'sfx' 사유로 버려야 보고가 정확하다.
    keep = [ln for ln in out
            if not (_SFX_PAT.match(ln) or _LYRIC_PAT.match(ln))]
    return keep or out


def _clean_text(s):
    """자막 한 장의 텍스트를 한 줄로 정규화 (매칭·중복판정·digest용)."""
    return " ".join(_clean_lines(s))


# ── SRT ─────────────────────────────────────────────────────────────────────

def parse_srt(text):
    """SubRip. 블록 사이 빈 줄 구분, '-->' 타임코드."""
    cues = []
    # 번호 줄이 없는 변종도 있어 타임코드 줄을 기준으로 쪼갠다
    chunks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    for chunk in chunks:
        lines = [ln for ln in chunk.split("\n") if ln.strip() != ""]
        if not lines:
            continue
        ti = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ti is None:
            continue
        left, _, right = lines[ti].partition("-->")
        start, end = _parse_ts(left), _parse_ts(right)
        if start is None:
            continue
        # 타임코드 줄 바로 앞의 숫자 = SRT 파일에 적힌 번호.
        # 사람이 SRT를 열어 찾는 번호이자 대본에서 인용을 지목하는 주소다.
        # 정리 후 재부여하는 idx 와는 다르다 (노이즈 큐가 빠지면서 어긋난다).
        src_no = None
        if ti > 0:
            head = lines[ti - 1].strip().lstrip("﻿")
            if head.isdigit():
                src_no = int(head)
        body_lines = _clean_lines("\n".join(lines[ti + 1:]))
        if body_lines:
            cues.append({"src_no": src_no, "start_s": start, "end_s": end,
                         "text": " ".join(body_lines), "lines": body_lines})
    return cues


# ── VTT ─────────────────────────────────────────────────────────────────────

def parse_vtt(text):
    """WebVTT — SRT와 구조가 같고 헤더·NOTE·큐 설정만 다르다."""
    text = re.sub(r"^WEBVTT[^\n]*\n", "", text.lstrip(), count=1)
    text = re.sub(r"^NOTE[^\n]*(?:\n(?!\n).*)*", "", text, flags=re.MULTILINE)
    return parse_srt(text)


# ── ASS / SSA ───────────────────────────────────────────────────────────────

_ASS_TAG = re.compile(r"\{[^}]*\}")          # {\i1} 같은 스타일 override
_ASS_TS = re.compile(r"\s*(\d+):(\d{1,2}):(\d{1,2})[.:](\d{1,3})\s*$")
_ASS_DEFAULT_FIELDS = ["layer", "start", "end", "style", "name",
                       "marginl", "marginr", "marginv", "effect", "text"]


def _ass_time(s):
    """0:01:23.45 → 83.45초. 소수부는 1/100초다(SRT의 1/1000이 아니다)."""
    m = _ASS_TS.match(s or "")
    if not m:
        return None
    h, mi, sec, frac = m.groups()
    return (int(h) * 3600 + int(mi) * 60 + int(sec)
            + int(frac.ljust(2, "0")[:2]) / 100.0)


def parse_ass(text):
    """Advanced SubStation Alpha (.ass) / SubStation Alpha (.ssa).

    `[Events]` 의 `Dialogue:` 줄만 읽는다(`Comment:` 는 화면에 안 나온다).
    필드 순서는 `Format:` 줄이 정한다 — ASS는 Layer로, SSA는 Marked로 시작해
    고정 위치를 가정하면 어긋난다. Text 는 규격상 **항상 마지막**이라
    쉼표를 그 앞까지만 자르면 대사 안의 쉼표가 살아남는다.
    """
    fields, cues = None, []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        low = line.lower()
        if low.startswith("format:"):
            # `[V4+ Styles]` 에도 Format: 줄이 있다(Name, Fontname, Fontsize…).
            # 그걸 잡으면 필드가 통째로 어긋나 큐가 0개가 된다 —
            # **Text 칸이 있는 Format 만** 이벤트용이다.
            got = [f.strip().lower()
                   for f in line.split(":", 1)[1].split(",")]
            if "text" in got and "start" in got:
                fields = got
            continue
        if not low.startswith("dialogue:"):
            continue
        if fields is None:
            fields = list(_ASS_DEFAULT_FIELDS)
        parts = line.split(":", 1)[1].split(",", len(fields) - 1)
        if len(parts) < len(fields):
            continue
        row = dict(zip(fields, parts))
        start, end = _ass_time(row.get("start")), _ass_time(row.get("end"))
        if start is None or end is None:
            continue
        body = _ASS_TAG.sub("", row.get("text", ""))
        body = (body.replace("\\N", "\n").replace("\\n", "\n")
                    .replace("\\h", " "))
        body_lines = _clean_lines(body)
        if body_lines:
            cues.append({"src_no": len(cues) + 1, "start_s": start,
                         "end_s": end, "text": " ".join(body_lines),
                         "lines": body_lines})
    return cues


# ── SMI (SAMI) ──────────────────────────────────────────────────────────────

# 닫는 '>'까지 삼킨다. 안 그러면 본문이 '>...'로 시작해서 &nbsp;만 있는 SYNC가
# 빈 값으로 인식되지 않고(=자막 지우기 신호를 놓치고) 큐가 하나 더 생긴다.
_SYNC_PAT = re.compile(r"<SYNC\s+START\s*=\s*[\"']?(-?\d+)[^>]*>?", re.IGNORECASE)
_PCLASS_PAT = re.compile(r"<P\b[^>]*\bCLASS\s*=\s*[\"']?([A-Za-z0-9_\-]+)", re.IGNORECASE)


_HANGUL_PAT = re.compile(r"[가-힣]")


def is_korean(cues, sample=400, ratio=0.30):
    """자막이 한국어인가 — 한글이 든 큐의 비율로 본다.

    화면에 그대로 띄울 수 있는 자막인지 가르는 판단이다. 아니면 대본에
    `> 한국어 번역` 을 반드시 달아야 한다(§AGENTS 포맷).
    한 줄만 보면 영어 자막에 섞인 한글 한 줄에 속으므로 비율로 본다.
    실측: 한국어 SRT 99% · 영어 CC 0%.
    """
    if not cues:
        return True
    head = cues[:sample]
    hit = sum(1 for c in head if _HANGUL_PAT.search(c.get("text") or ""))
    return hit >= len(head) * ratio


def _has_cjk(text):
    """한글 또는 가나가 있는가 (SMI 다국어 Class 선택 기준)."""
    for ch in text:
        o = ord(ch)
        if 0xAC00 <= o <= 0xD7A3 or 0x3040 <= o <= 0x30FF:
            return True
    return False


def parse_smi(text, prefer_class=None):
    """SAMI. `<SYNC Start=ms>` 단위이고 **다음 SYNC가 이전 큐의 끝**이다.

    여러 언어가 Class로 섞여 있으면(KRCC/ENCC/JPCC …) 큐가 가장 많은 Class를
    채택한다. prefer_class로 강제할 수 있다.

    `&nbsp;`만 있는 SYNC는 자막을 지우라는 신호이므로 큐를 만들지 않고
    이전 큐의 끝시각으로만 쓴다.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    events = []          # (ms, class or None, body)
    for m in _SYNC_PAT.finditer(text):
        ms = int(m.group(1))
        body_start = m.end()
        nxt = _SYNC_PAT.search(text, body_start)
        body = text[body_start:nxt.start() if nxt else len(text)]
        cm = _PCLASS_PAT.search(body)
        cls = cm.group(1).upper() if cm else None
        events.append((ms, cls, body))

    classes, cjk_hits = {}, {}
    for _ms, cls, body in events:
        t = _clean_text(body)
        if cls and t:
            classes[cls] = classes.get(cls, 0) + 1
            if _has_cjk(t):
                cjk_hits[cls] = cjk_hits.get(cls, 0) + 1

    chosen = None
    if prefer_class:
        chosen = prefer_class.upper()
    elif classes:
        # 한글·가나가 있는 Class를 우선(이 파이프라인은 한국어/일본어 자막을 읽는다),
        # 그다음 큐 수. 동점이면 이름순으로 고정해 실행마다 결과가 흔들리지 않게 한다.
        chosen = max(classes.items(),
                     key=lambda kv: (cjk_hits.get(kv[0], 0) > 0, kv[1], kv[0]))[0]

    # 채택한 Class(또는 Class 표기가 없는 파일)의 이벤트만 시간순으로
    picked = [(ms, _clean_lines(body))
              for ms, cls, body in events
              if chosen is None or cls is None or cls == chosen]
    picked.sort(key=lambda t: t[0])

    # SMI에는 큐 번호가 없다. 지우기 신호(&nbsp;)를 뺀 실제 자막의 등장 순서를
    # src_no 로 준다 — 사람이 파일을 열어 셀 수 있는 유일한 주소다.
    cues = []
    for i, (ms, body_lines) in enumerate(picked):
        start = ms / 1000.0
        end = picked[i + 1][0] / 1000.0 if i + 1 < len(picked) else None
        if not body_lines:
            continue          # &nbsp; 등 — 지우기 신호. 끝시각으로만 쓰인다
        cues.append({"src_no": len(cues) + 1, "start_s": start, "end_s": end,
                     "text": " ".join(body_lines), "lines": body_lines})
    return cues, {"classes": classes, "chosen_class": chosen}


# ── 통합 파싱 + 정리 ────────────────────────────────────────────────────────

class SubtitleError(RuntimeError):
    """자막을 읽을 수 없다 — 사람이 고칠 수 있는 상황이라 트레이스백 대신
    `cli.main` 이 `✘` 한 줄로 보여 준다."""


def parse_file(path, prefer_class=None, offset_s=0.0, fps_scale=1.0):
    """자막 파일 → 정리된 큐 목록 + 메타.

    반환: (cues, meta)
      cues[i] = {"idx","src_no","start_s","end_s","text","lines"}
        idx    — 1부터 연속. 인접 판정·블록화가 연속성에 의존하므로 여기서만 쓴다.
        src_no — SRT 파일에 적힌 번호. 노이즈 큐가 빠지면 idx와 어긋나므로,
                 **사람이 보는 모든 표면(로그·check)은 src_no 만 쓴다.**
        lines  — 화면 줄바꿈을 살린 줄 목록. 불변식: " ".join(lines) == text
      meta    = {"parser","encoding","classes","chosen_class","raw_count",
                 "dropped": {...}, "offset_s","fps_scale"}
    """
    p = Path(path)
    text, enc = read_text(p)
    ext = p.suffix.lower()
    info = {}

    head = text[:4000]
    if ext == ".smi" or "<SYNC" in head.upper():
        cues, info = parse_smi(text, prefer_class)
        parser = "smi"
    elif ext == ".vtt" or text.lstrip().upper().startswith("WEBVTT"):
        cues, parser = parse_vtt(text), "vtt"
    elif ext in (".ass", ".ssa") or "[script info]" in head.lower():
        cues, parser = parse_ass(text), "ass"
    else:
        cues, parser = parse_srt(text), "srt"

    raw_count = len(cues)
    cues, dropped = _clean_cues(cues)

    # 싱크 보정: 자막 시각 = raw × fps_scale + offset
    if fps_scale != 1.0 or offset_s:
        for c in cues:
            c["start_s"] = c["start_s"] * fps_scale + offset_s
            if c["end_s"] is not None:
                c["end_s"] = c["end_s"] * fps_scale + offset_s

    for i, c in enumerate(cues, start=1):
        c["idx"] = i
        if c.get("src_no") is None:      # 번호 줄 없는 SRT 변종·VTT
            c["src_no"] = i
        c.setdefault("lines", [c["text"]])
        # 줄 구조와 평탄 텍스트가 어긋나면 왕복이 조용히 깨진다
        assert " ".join(c["lines"]) == c["text"], (c["idx"], c["lines"], c["text"])

    meta = {
        "parser": parser, "encoding": enc,
        "classes": info.get("classes", {}),
        "chosen_class": info.get("chosen_class"),
        "raw_count": raw_count, "cue_count": len(cues),
        "dropped": dropped,
        "offset_s": offset_s, "fps_scale": fps_scale,
    }
    # 큐가 하나도 없으면 **여기서 막는다.** 자막이 아닌 파일이나 0바이트를
    # 넣으면 예전엔 그대로 통과해 `cli.py:188` 의 `cues[0]` 에서
    # `IndexError: list index out of range` 트레이스백이 났다(실측).
    # 처음 쓰는 사람이 제일 흔히 하는 실수인데 제일 안 친절하게 죽었다.
    # 큐 0개짜리 자막은 어느 경로에서도 쓸모가 없으므로 근본에서 거른다.
    if not cues:
        raise SubtitleError(
            "자막을 한 줄도 읽지 못했습니다: %s" % p.name + chr(10)
            + "  자막 파일이 맞는지, 비어 있지 않은지 확인하세요"
              " (srt · smi · sami · vtt · ass · ssa).")
    return cues, meta


def _clean_cues(cues):
    """노이즈 제거 + 끝시각 보정. 무엇을 몇 개 버렸는지 반드시 돌려준다.

    조용히 버리면 "왜 이 장면이 안 뽑혔지?"를 추적할 수 없다.
    """
    dropped = {"empty": 0, "music": 0, "sfx": 0, "credit": 0, "dup": 0,
               "bad_time": 0}
    removed = []          # [(src_no, 사유, 텍스트앞부분)] — 조용히 버리지 않기 위해
    n = len(cues)
    out = []

    def _drop(cue, why):
        dropped[why] += 1
        removed.append((cue.get("src_no"), why, (cue.get("text") or "")[:40]))

    for i, c in enumerate(cues):
        text = c["text"]
        if not text or is_silent(text):
            _drop(c, "empty")
            continue
        if _MUSIC_PAT.search(text) or _LYRIC_PAT.match(text):
            _drop(c, "music")
            continue
        # 둘 다 봐야 한다.
        #  · 합친 텍스트 — 대괄호가 두 줄에 걸친 경우
        #    (`[ lndistinct conversations,` ⏎ `laughter ]`)
        #  · 줄 단위 전부 — 종류가 다른 노이즈가 섞인 경우
        #    (`# 가사 #` ⏎ `[ Girls giggling ]`)
        lines = c.get("lines") or [text]
        if (_SFX_PAT.match(text)
                or all(_SFX_PAT.match(x) or _LYRIC_PAT.match(x) for x in lines)):
            _drop(c, "sfx")
            continue
        near_edge = i < CREDIT_SCAN or i >= n - CREDIT_SCAN
        if near_edge and _CREDIT_PAT.search(text):
            _drop(c, "credit")
            continue
        if c["start_s"] is None or c["start_s"] < 0:
            _drop(c, "bad_time")
            continue
        # 바로 앞과 텍스트가 같고 시간이 붙어 있으면 같은 자막의 연장으로 본다.
        # 살아남는 쪽의 lines/src_no 는 그대로 둔다 (다시 join 하면 줄 구조가 죽는다).
        if out and out[-1]["text"] == text and c["start_s"] - _end_of(out[-1]) < 0.5:
            out[-1]["end_s"] = c["end_s"] if c["end_s"] is not None else out[-1]["end_s"]
            out[-1]["src_no_end"] = c.get("src_no")
            _drop(c, "dup")
            continue
        out.append(dict(c))

    # 끝시각 없거나 뒤집힌 큐 보정: 다음 큐 시작 또는 글자수 기반 최소 길이
    for i, c in enumerate(out):
        nxt = out[i + 1]["start_s"] if i + 1 < len(out) else None
        est = c["start_s"] + min(6.0, max(1.0, len(c["text"]) * 0.09))
        if c["end_s"] is None or c["end_s"] <= c["start_s"]:
            c["end_s"] = min(est, nxt) if nxt else est
        if nxt is not None and c["end_s"] > nxt:
            c["end_s"] = nxt                     # 겹침 제거
        if c["end_s"] <= c["start_s"]:
            c["end_s"] = c["start_s"] + 0.2
    dropped["removed"] = removed
    return out, dropped


def _end_of(cue):
    return cue["end_s"] if cue["end_s"] is not None else cue["start_s"]


# ── 블록 · 무성 구간 ────────────────────────────────────────────────────────

def build_blocks(cues, gap_s=BLOCK_GAP_S, max_s=BLOCK_MAX_S, silent_gap_s=SILENT_GAP_S):
    """큐 → 대사 블록 + 무성 구간.

    블록 = 대사가 몰려 있는 한 덩어리(대략 한 장면). 블록 사이의 긴 공백은
    "대사 없는 구간"이며, 자막만 보는 우리에게는 이 표기가 액션·클라이맥스를
    알 수 있는 유일한 힌트다.
    """
    blocks, gaps = [], []
    if not cues:
        return blocks, gaps

    cur = [cues[0]]
    for prev, c in zip(cues, cues[1:]):
        gap = c["start_s"] - prev["end_s"]
        too_long = (c["end_s"] - cur[0]["start_s"]) > max_s
        if gap >= gap_s or too_long:
            blocks.append(cur)
            if gap >= silent_gap_s:
                gaps.append({"start_s": prev["end_s"], "end_s": c["start_s"],
                             "dur_s": gap, "after_block": len(blocks)})
            cur = [c]
        else:
            cur.append(c)
    blocks.append(cur)

    out = []
    for bi, group in enumerate(blocks, start=1):
        out.append({
            "id": bi,
            "start_s": group[0]["start_s"], "end_s": group[-1]["end_s"],
            "from_idx": group[0]["idx"], "to_idx": group[-1]["idx"],
            "cues": group,
        })
    return out, gaps


# ── digest (LLM 입력) ───────────────────────────────────────────────────────

def hms(sec):
    """초 → 'H:MM:SS'."""
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    return f"{h}:{m:02d}:{s:02d}"


def format_digest(blocks, gaps, max_cue_chars=140):
    """LLM에게 줄 자막 요약.

    형식 — 큐 인덱스를 줄마다 붙인다. 토큰을 조금 더 쓰지만, LLM이 답으로 낼
    sub_from/sub_to가 실제 존재하는 큐 번호인지 파이썬이 검증할 수 있게 된다.

        [B12] 0:14:32~0:15:10
        402| 나 여기 있어
        403| 어떻게 찾았어?
        (무성 34초: 0:15:10~0:15:44)
    """
    gap_after = {g["after_block"]: g for g in gaps}
    lines = []
    for b in blocks:
        lines.append(f"[B{b['id']}] {hms(b['start_s'])}~{hms(b['end_s'])}")
        for c in b["cues"]:
            t = c["text"]
            if len(t) > max_cue_chars:
                t = t[:max_cue_chars] + "…"
            lines.append(f"{c['idx']}| {t}")
        g = gap_after.get(b["id"])
        if g:
            lines.append(f"(무성 {int(g['dur_s'])}초: {hms(g['start_s'])}~{hms(g['end_s'])})")
    return "\n".join(lines)


# ── 검사 ────────────────────────────────────────────────────────────────────

def fps_ratio(cues, movie_duration_s):
    """자막 마지막 시각 / 영화 길이. 1.0에서 멀면 릴리즈가 다른 자막이다."""
    if not cues or not movie_duration_s:
        return None
    return cues[-1]["end_s"] / float(movie_duration_s)


def fps_warning(cues, movie_duration_s):
    """프레임레이트 불일치 경고 문자열 또는 None."""
    r = fps_ratio(cues, movie_duration_s)
    if r is None:
        return None
    if FPS_RATIO_LO <= r <= FPS_RATIO_HI:
        return None
    hint = ""
    for name, scale in (("23.976→25", 25 / 23.976), ("25→23.976", 23.976 / 25),
                        ("23.976→24", 24 / 23.976), ("24→23.976", 23.976 / 24)):
        if abs(r * scale - 1.0) < 0.01:
            hint = f" (fps_scale={1/scale:.5f} 로 {name} 보정이 맞을 수 있음)"
            break
    return (f"자막 끝({hms(cues[-1]['end_s'])})과 영화 길이"
            f"({hms(movie_duration_s)})의 비율이 {r:.3f} — 다른 릴리즈의 자막일 수 있습니다.{hint}")


def summarize(cues, meta, blocks, gaps):
    """로그용 한 줄 요약 dict."""
    d = meta.get("dropped", {})
    return {
        "parser": meta["parser"], "encoding": meta["encoding"],
        "chosen_class": meta.get("chosen_class"),
        "cue_count": len(cues), "raw_count": meta.get("raw_count"),
        "dropped_total": sum(d.values()), "dropped": d,
        "block_count": len(blocks), "silent_gap_count": len(gaps),
        "silent_total_s": round(sum(g["dur_s"] for g in gaps), 1),
        "first_cue_s": cues[0]["start_s"] if cues else None,
        "last_cue_s": cues[-1]["end_s"] if cues else None,
    }


def save_json(path, cues, meta, blocks, gaps):
    """중간 산출물 저장 (재현·디버그용). blocks의 cues는 인덱스만 남긴다."""
    payload = {
        "meta": meta,
        "cues": [{"idx": c["idx"], "start_s": round(c["start_s"], 3),
                  "end_s": round(c["end_s"], 3), "text": c["text"]} for c in cues],
        "blocks": [{"id": b["id"], "start_s": round(b["start_s"], 3),
                    "end_s": round(b["end_s"], 3),
                    "from_idx": b["from_idx"], "to_idx": b["to_idx"]} for b in blocks],
        "silent_gaps": [{"start_s": round(g["start_s"], 3),
                         "end_s": round(g["end_s"], 3),
                         "dur_s": round(g["dur_s"], 3),
                         "after_block": g["after_block"]} for g in gaps],
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    return str(path)
