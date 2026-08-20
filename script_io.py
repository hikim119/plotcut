"""
script_io.py — 「타임라인 대본」 txt 읽기/쓰기 + SRT 큐 매칭

이 txt가 정본이다. 사람이 고친 파일만 있으면 타임라인이 완전히 복원되어야 하므로
큐 번호를 따로 저장하지 않는다 — 대사 문단을 그 블록 범위 안의 SRT 큐와 대조해
되찾는다.

포맷
    제목 한 줄

    [01:25:03 ~ 01:25:07]          ← 블록 헤더. 선택적 ` / 메모`

    우리 집에서 뭐 해요?            ← 대사 문단 = SRT 큐 하나(또는 연속 두 개)

    잠시 보관해 줘요                ← 문단 안 줄바꿈 = 화면 줄바꿈
    나중에 와서 가져갈게요

    (좋아하는 남자의 집에…)         ← 나레이션 = 괄호 안 한 줄

문단 구분은 빈 줄. 블록은 다음 헤더가 나올 때까지.
"""

import difflib
import re

import timing

# ── 파일 이름 ───────────────────────────────────────────────────────────────
# 예전엔 「타임라인과 자막」 이었는데 입력 슬롯의 '영화 자막' 과 같은 단어라
# 어느 파일을 말하는지 헷갈렸다. 대본은 원래 대사와 나레이션을 둘 다 담는 말이다.
# 옛 이름으로 만든 대본이 이미 있으니 **읽기는 계속 받아 준다.**
# ClipMatch·CutSync 와 같은 시각 표기. 이름에 이게 붙어야 같은 영화를 여러 번
# 돌렸을 때 대본 파일·결과 폴더·CapCut 프로젝트가 서로 안 헷갈린다.
STAMP_FMT = "%m%d_%H%M"


def stamp(when=None):
    import datetime
    return (when or datetime.datetime.now()).strftime(STAMP_FMT)


SUFFIX = "_타임라인 대본"
SUFFIX_OLD = ("_타임라인과 자막", "_타임라인")
LABEL = "타임라인 대본"

# 영어·일본어 자막처럼 **화면에 그대로 띄울 수 없는 대사**를 위한 번역 표시.
# 대사 문단 안에서 이 기호로 시작하는 줄은 화면에만 나가고, 매칭은 위의 원문으로
# 한다. 번역문으로 매칭하려 들면 자막과 대조가 안 돼 컷 위치를 잃는다.
TRANS_MARK = ">"

# 자막 번호 앵커. `#476 대박, 저 귀걸이 좀 봐` 처럼 쓴다.
#
# 외국어 자막을 쓸 때는 대사를 한국어로 갈아끼워야 하는데, 그러면 자막 원문과
# 대조할 근거가 사라진다. 그래서 **텍스트 대신 번호로** 큐를 가리킨다.
# 번호는 SRT 파일에 적힌 번호(src_no)다 — 사람이 자막을 열어 바로 찾을 수 있다.
# `#476-477` 은 연속 두 큐를 한 문단으로 합친 것.
CUE_MARK = re.compile(r"^#\s*(\d+)\s*(?:[-~]\s*(\d+))?\s+(.*)$")

# 시각 앵커. `@01:25:02.9~01:25:07.4 우리 집에서 뭐 해요?`
#
# 자막 없이 **대본 + 영화만으로** 만들 수 있게 하는 형식이다. 여기 적힌 구간은
# 앞뒤 여유(PRE/POST)까지 이미 반영된 **최종 소스 구간**이라 그대로 자르면 된다.
#
# 대가: 대사를 고쳐도 시각이 따라오지 않고, 시각이 틀려도 **조용히** 엉뚱한
# 장면이 나온다(글자·번호 앵커는 못 찾으면 강등으로 걸린다). 그래서 기본이
# 아니라 선택이다 — `cli.py build --freeze` 나 GUI 체크박스로 만든다.
TIME_MARK = re.compile(
    r"^@\s*(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)"
    r"\s*[-~]\s*"
    r"(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s+(.*)$")


def _t(h, m, s):
    return int(h or 0) * 3600 + int(m) * 60 + float(s)


def fmt_span(a, b):
    """초 → `@01:25:02.83~01:25:07.41`

    소수 **2자리**여야 한다. 1자리로 줄이면 끝점마다 0.05초까지 어긋나 컷이
    24fps 한 프레임(0.042초)보다 크게 밀린다 — 자막으로 만든 판과 시각으로
    만든 판이 달라진다(실측 최대 0.075초).
    """
    def one(x):
        return "%02d:%02d:%05.2f" % (int(x // 3600), int(x % 3600 // 60), x % 60)
    return "@%s~%s" % (one(a), one(b))


_STAMP_TAIL = re.compile(r"_\d{4}_\d{4}$")


def strip_suffix(stem):
    """파일 이름에서 대본 접미사를 떼어 낸다 (프로젝트명 뽑을 때).

    도구가 만든 대본은 `<영화명>_타임라인 대본_0818_1745` 처럼 시각이 더 붙는다.
    시각은 **접미사가 실제로 나올 때만** 떼어 낸다 — 무조건 떼면
    `영화_2024_1080` 같은 멀쩡한 이름이 잘린다.
    """
    for base in (_STAMP_TAIL.sub("", stem), stem):
        for suf in (SUFFIX,) + SUFFIX_OLD:
            if base.endswith(suf):
                return base[: -len(suf)]
    return stem


def find_scripts(folder):
    """폴더 안의 대본 txt. 새 이름을 먼저, 없으면 옛 이름도 찾는다.

    접미사 뒤에 `_0818_1745` 시각이나 `_2` 가 더 붙을 수 있어 `*` 로 받는다.
    """
    from pathlib import Path
    folder = Path(folder)
    for suf in (SUFFIX,) + SUFFIX_OLD:
        hits = sorted(folder.glob("*%s*.txt" % suf))
        if hits:
            return hits
    return []


# ── 상수 ────────────────────────────────────────────────────────────────────
# 헤더는 정수초라 실제 float 범위의 손실 있는 표현이다. 매칭 후보를 고를 때
# 그대로 쓰면 경계에 걸친 큐를 놓친다 — 실측: 포함(containment) 해석이면
# 무편집 정답 txt에서 이미 3/48 실패, 한 번 쓰고 다시 읽으면 8/48 실패.
# TOL 을 0~30초로 쓸어도 48/48·오매칭 0 (서명 완전일치를 먼저 시도하기 때문).
TOL = 1.5

DIFF_MIN = 0.75      # difflib 최소 유사도
DIFF_TIE = 0.02      # 이 차이 안이면 동점으로 보고 위치로 가른다
MAX_MERGE = 2        # 한 문단에 합칠 수 있는 연속 큐 수

_HEADER_PAT = re.compile(
    r"^\[\s*(\d{1,3}:\d{1,2}:\d{1,2}(?:[.,]\d+)?)"
    r"\s*[~～\-–—]\s*"
    r"(\d{1,3}:\d{1,2}:\d{1,2}(?:[.,]\d+)?)"
    r"\s*(?:/\s*(.*?))?\s*\]$")

_SIG_STRIP = re.compile(r"""[\s"'`.,!?…·:;\-–—~()\[\]{}「」『』《》〈〉""''‥]+""")


class ScriptError(RuntimeError):
    pass


# ── 시각 ────────────────────────────────────────────────────────────────────

def hms(sec):
    """초 → HH:MM:SS (헤더 표기. 정답 샘플이 두 자리 시를 쓴다)."""
    sec = max(0, int(sec))
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def parse_hms(s):
    s = s.replace(",", ".")
    parts = s.split(":")
    h, m, rest = (parts + ["0", "0", "0"])[:3]
    return int(h) * 3600 + int(m) * 60 + float(rest)


def sig(s):
    """매칭용 서명 — 공백·문장부호를 지운 알맹이만."""
    return _SIG_STRIP.sub("", s)


# ── 파싱 ────────────────────────────────────────────────────────────────────

def split_paragraphs(text):
    """빈 줄 기준 문단 분할. 빈 줄이 여러 개여도 하나로 본다."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("﻿"):
        text = text[1:]
    out = []
    for chunk in re.split(r"\n[ \t]*\n", text):
        lines = [ln.rstrip() for ln in chunk.split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            out.append("\n".join(lines))
    return out


def parse_header(p):
    """'[a ~ b / 메모]' → (start_s, end_s, note). 아니면 None."""
    m = _HEADER_PAT.match(p.strip())
    if not m:
        return None
    a, b = parse_hms(m.group(1)), parse_hms(m.group(2))
    return a, b, (m.group(3) or "").strip()


def _is_narration(p):
    q = p.strip()
    return q.startswith("(") or q.startswith("（")


def read(text, cues, log=print):
    """txt → doc. cues 는 subtitle.parse_file 의 결과.

    반환: {"title", "blocks", "warnings", "stats"}
      block = {"header","win","note","items"}
      item  = {"kind","text","lines","cues","demoted","note"}
    """
    warnings = []
    paras = split_paragraphs(text)
    if not paras:
        raise ScriptError("빈 파일입니다.")

    title = ""
    blocks = []
    cur = None
    for p in paras:
        h = parse_header(p)
        if h is not None:
            cur = {"header": p.strip(), "win": (h[0], h[1]), "note": h[2], "items": []}
            blocks.append(cur)
            continue
        if cur is None:
            if not title:
                title = " ".join(ln.strip() for ln in p.split("\n") if ln.strip())
                continue
            # 헤더 없이 문단이 더 나온다 — 범위를 알 수 없어 매칭 근거가 없다
            warnings.append("첫 블록 헤더 앞에 문단이 더 있습니다: %r" % p[:30])
            cur = {"header": None, "win": None, "note": "", "items": []}
            blocks.append(cur)
        if _is_narration(p):
            cur["items"].append(_narration_item(p, warnings))
        else:
            lines = [ln.strip() for ln in p.split("\n") if ln.strip()]
            # `> 번역` 줄은 **화면에만** 나간다. 매칭은 위의 원문으로 한다 —
            # 번역문으로 매칭하려 들면 자막과 대조가 안 돼 컷 위치를 못 찾는다.
            src = [ln for ln in lines if not ln.startswith(TRANS_MARK)]
            trans = [ln[len(TRANS_MARK):].strip() for ln in lines
                     if ln.startswith(TRANS_MARK)]
            trans = [t for t in trans if t]
            if not src:
                warnings.append(
                    "번역(`%s`)만 있고 원문이 없는 문단이 있습니다 — 매칭할 근거가 "
                    "없어 번역을 원문으로 봅니다: %r" % (TRANS_MARK, p[:30]))
                src, trans = lines, []
            # `@01:25:02.9~01:25:07.4 …` 앵커 — 자막 없이 그대로 자를 구간
            span = None
            m = TIME_MARK.match(src[0])
            if m:
                g = m.groups()
                span = (_t(g[0], g[1], g[2]), _t(g[3], g[4], g[5]))
                rest = g[6].strip()
                src = ([rest] if rest else []) + src[1:]
                if span[1] <= span[0]:
                    warnings.append("시각 구간이 뒤집혔습니다: %r" % p[:30])
                    span = None
                if not src:
                    warnings.append("시각 뒤에 대사가 없습니다: %r" % p[:30])
                    src = ["(빈 줄)"]

            # `#476 …` 앵커 — 텍스트가 아니라 자막 번호로 큐를 가리킨다
            ref = None
            m = CUE_MARK.match(src[0]) if not span else None
            if m:
                a, b, rest = int(m.group(1)), m.group(2), m.group(3).strip()
                ref = (a, int(b) if b else a)
                src = ([rest] if rest else []) + src[1:]
                if not src:
                    warnings.append("#%d 뒤에 대사가 없습니다." % a)
                    src = ["(빈 줄)"]
            cur["items"].append({"kind": "dialogue", "text": " ".join(src),
                                 "lines": src, "trans": trans, "cue_ref": ref,
                                 "span": span, "cues": [], "demoted": False,
                                 "note": ""})

    if not blocks:
        raise ScriptError("블록 헤더 '[시각 ~ 시각]' 이 하나도 없습니다.")

    stats = _match_all(blocks, cues, warnings, log)
    return {"title": title, "blocks": blocks,
            "warnings": warnings, "stats": stats}


def _narration_item(p, warnings):
    q = " ".join(ln.strip() for ln in p.split("\n") if ln.strip())
    if "\n" in p:
        warnings.append("나레이션이 여러 줄입니다 — 한 줄로 합쳤습니다: %r" % q[:30])
    body = q
    if body.startswith("(") or body.startswith("（"):
        body = body[1:]
    if body.endswith(")") or body.endswith("）"):
        body = body[:-1]
    else:
        warnings.append("나레이션 괄호가 닫히지 않았습니다: %r" % q[:30])
    body = body.strip()
    return {"kind": "narration", "text": body, "lines": [body],
            "cues": [], "demoted": False, "note": ""}


# ── 매칭 ────────────────────────────────────────────────────────────────────

def _pool(cues, win):
    """블록 범위와 **겹치는** 큐. 헤더가 없으면 전체."""
    if not win:
        return list(cues)
    a, b = win
    return [c for c in cues if c["start_s"] < b + TOL and c["end_s"] > a - TOL]


def _candidates(pool, s):
    """(첫 큐, 마지막 큐) 후보 목록. 서명 → 연속 병합 → difflib 순."""
    exact = [c for c in pool if sig(c["text"]) == s]
    if exact:
        return [(c, c) for c in exact], "서명"

    merged = []
    for i in range(len(pool) - 1):
        for n in range(2, MAX_MERGE + 1):
            if i + n - 1 >= len(pool):
                break
            run = pool[i:i + n]
            if any(run[k + 1]["idx"] != run[k]["idx"] + 1 for k in range(n - 1)):
                break
            if sig("".join(c["text"] for c in run)) == s:
                merged.append((run[0], run[-1]))
    if merged:
        return merged, "병합"

    scored = []
    for c in pool:
        r = difflib.SequenceMatcher(None, sig(c["text"]), s, autojunk=False).ratio()
        scored.append((r, c))
    if not scored:
        return [], "없음"
    best = max(r for r, _ in scored)
    if best < DIFF_MIN:
        return [], "없음"
    near = [(c, c) for r, c in scored if best - r <= DIFF_TIE]
    return near, "유사(%.2f)" % best


def _pick(cands, prev_idx):
    """동점 해소. 단조성은 **필터가 아니라 선호**로만 쓴다 —
    하드 필터로 만들면 사용자가 문단 순서를 바꾸는 순간 매칭이 통째로 죽는다."""
    if len(cands) == 1 or prev_idx is None:
        return min(cands, key=lambda t: t[0]["idx"])
    ahead = [t for t in cands if t[0]["idx"] > prev_idx]
    if ahead:
        return min(ahead, key=lambda t: t[0]["idx"])
    return min(cands, key=lambda t: (abs(t[0]["idx"] - prev_idx), t[0]["idx"]))


def _match_all(blocks, cues, warnings, log):
    stats = {"dialogue": 0, "by_sig": 0, "by_merge": 0, "by_diff": 0,
             "by_ref": 0, "by_time": 0, "demoted": 0, "ambiguous": 0,
             "reused": 0, "overstuffed": 0}
    seen_global = {}
    for bi, blk in enumerate(blocks):
        pool = _pool(cues, blk["win"])
        prev_idx = None
        used_here = {}
        for it in blk["items"]:
            if it["kind"] != "dialogue":
                continue
            stats["dialogue"] += 1

            # 시각 앵커가 있으면 자막을 아예 안 본다 — 그게 이 형식의 목적이다.
            if it.get("span"):
                it["note"] = "시각"
                stats["by_time"] += 1
                continue

            # `#476` 앵커가 있으면 텍스트 대조를 건너뛴다. 한국어로 갈아끼운
            # 대사는 자막 원문과 글자가 하나도 안 겹치므로 번호가 유일한 근거다.
            if it.get("cue_ref"):
                a, b = it["cue_ref"]
                run = [c for c in cues
                       if c.get("src_no") is not None and a <= c["src_no"] <= b]
                if not run:
                    it["demoted"] = True
                    it["note"] = "#%d 번 자막이 없습니다" % a
                    stats["demoted"] += 1
                    warnings.append(
                        "블록%d — SRT#%s 번 자막을 찾지 못했습니다: %r"
                        % (bi + 1, a if a == b else "%d-%d" % (a, b),
                           it["text"][:30]))
                    continue
                # 문단이 큐보다 줄이 많으면 **번호를 안 붙인 다음 큐를 삼킨** 것이다.
                #   #763 아빠?     ← 763 은 한 줄짜리인데
                #   마야?          ← 이건 사실 764 다
                # 이러면 764 시각이 통째로 버려지고(그 장면이 안 나온다) 두 줄이
                # 763 구간에 한꺼번에 뜬다. 실측: 컷 4.17초 → 2.24초, 경고 0건.
                want = sum(len(c["lines"]) for c in run)
                if len(it["lines"]) > want:
                    stats["overstuffed"] += 1
                    warnings.append(
                        "블록%d SRT#%s 는 %d줄인데 문단이 %d줄입니다 — "
                        "다음 자막을 번호 없이 붙인 것 같습니다. "
                        "줄마다 번호를 달아 문단을 나누세요: %r"
                        % (bi + 1, a if a == b else "%d-%d" % (a, b),
                           want, len(it["lines"]), " / ".join(it["lines"])[:40]))
                it["cues"] = run
                it["note"] = "번호"
                stats["by_ref"] += 1
                prev_idx = run[-1]["idx"]
                _mark_used(run, it, bi, blk, used_here, seen_global,
                           warnings, stats)
                continue

            s = sig(it["text"])
            cands, how = _candidates(pool, s)
            if not cands:
                it["demoted"] = True
                it["note"] = "매칭 실패"
                stats["demoted"] += 1
                # 자막을 아예 안 넣은 경우는 "못 찾은" 게 아니라 찾을 데가
                # 없는 것이다. 문단마다 ⚠ 를 뿌리면 대본이 틀린 것처럼 보인다 —
                # 이때는 pipeline 이 "균등 배치합니다" 한 줄로 이미 알려 준다.
                if cues:
                    warnings.append(
                        "블록%d %s — 자막에서 찾지 못했습니다: %r"
                        % (bi + 1, blk["header"] or "(헤더 없음)", it["text"][:34]))
                continue
            if len(cands) > 1:
                stats["ambiguous"] += 1
            first, last = _pick(cands, prev_idx)
            run = [c for c in cues if first["idx"] <= c["idx"] <= last["idx"]]
            it["cues"] = run
            it["note"] = how
            if len(cands) > 1:
                it["note"] += " · 동일 후보 %d건 중 SRT#%s 선택" % (
                    len(cands), first["src_no"])
            if how == "서명":
                stats["by_sig"] += 1
            elif how == "병합":
                stats["by_merge"] += 1
            else:
                stats["by_diff"] += 1
            prev_idx = last["idx"]
            _mark_used(run, it, bi, blk, used_here, seen_global, warnings, stats)
    return stats


def _mark_used(run, it, bi, blk, used_here, seen_global, warnings, stats):
    """같은 큐를 두 번 쓰지 않는지 본다. 같은 블록 안이면 오류, 블록 간이면 경고."""
    for c in run:
        if c["idx"] in used_here:
            raise ScriptError(
                "블록%d %s 안에서 같은 자막(SRT#%s '%s')이 두 문단에 배정됐습니다.\n"
                "  → 문단 하나를 지우거나 다른 대사로 바꾸세요."
                % (bi + 1, blk["header"], c["src_no"], c["text"][:24]))
        used_here[c["idx"]] = it
        if c["idx"] in seen_global and seen_global[c["idx"]] != bi:
            stats["reused"] += 1
            warnings.append(
                "SRT#%s '%s' 를 블록%d와 블록%d가 함께 씁니다."
                % (c["src_no"], c["text"][:20],
                   seen_global[c["idx"]] + 1, bi + 1))
        seen_global.setdefault(c["idx"], bi)


# ── 쓰기 ────────────────────────────────────────────────────────────────────

def render_header(start_s, end_s, note=""):
    """헤더는 floor/ceil 이어야 정수초 표기가 실제 float 범위를 항상 덮는다.
    실측: floor/floor 40/48, round/round 46/48, floor/ceil **48/48** 매칭."""
    import math
    a = hms(math.floor(start_s))
    b = hms(math.ceil(end_s))
    body = "%s ~ %s" % (a, b)
    if note:
        body += " / " + note
    return "[" + body + "]"


def write(doc):
    """doc → txt 문자열.

    헤더는 block["header_out"] 이 있으면 그것을, 없으면 읽어들인 원문
    block["header"] 을 그대로 쓴다. layout 이 범위를 다시 계산했을 때만
    header_out 을 채운다 — 그래야 순수 왕복이 고정점이 된다.
    """
    parts = []
    if doc.get("title"):
        parts.append(doc["title"])
    for blk in doc["blocks"]:
        h = blk.get("header_out") or blk.get("header")
        if h:
            parts.append(h)
        for it in blk["items"]:
            if it["kind"] == "narration":
                parts.append("(" + it["text"] + ")")
            else:
                out = list(it["lines"])
                if it.get("span") and out:
                    out[0] = fmt_span(*it["span"]) + " " + out[0]
                ref = it.get("cue_ref")
                if ref and out:
                    a, b = ref
                    out[0] = ("#%d " % a if a == b else "#%d-%d " % (a, b)) + out[0]
                out += [TRANS_MARK + " " + t for t in (it.get("trans") or [])]
                parts.append("\n".join(out))
    return "\n\n".join(parts) + "\n"


def narration_text(doc, limit=12):
    """타입캐스트 붙여넣기용. 제목은 넣지 않는다(화면 표시용이지 더빙 대상이 아니다)."""
    import wrap
    out = []
    for blk in doc["blocks"]:
        for it in blk["items"]:
            if it["kind"] == "narration":
                out.append(wrap.wrap_beat(it["text"], limit=limit))
    return "\n\n".join(out) + "\n"


def narrations(doc):
    return [it for blk in doc["blocks"] for it in blk["items"]
            if it["kind"] == "narration"]


def items(doc):
    """(블록 인덱스, 아이템) 평탄 목록."""
    return [(bi, it) for bi, blk in enumerate(doc["blocks"]) for it in blk["items"]]
