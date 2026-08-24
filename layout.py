"""
layout.py — 「타임라인 대본」 아이템 → 영상 세그먼트 · 자막 · 오디오 (순수 함수)

디스크를 만지지 않는다. 입력은 script_io.read() 의 doc 와 자막 큐, 영화 길이뿐.

핵심 규칙
  · 여유(PRE/POST)는 **이웃 큐 간격의 절반**을 넘지 않는다.
    실측: SRT 인접 간격의 28.7%가 0.083초(24fps 2프레임)라 고정 0.5초를 쓰면
    연속 큐를 인용할 때마다 소스가 0.417초씩 되감긴다(정답 샘플에서 8쌍).
    원본 소리 100%라 같은 대사 끝소리가 두 번 들린다.
  · 타임라인은 소스 창을 이어 붙인 것 그 자체다. 커서 누적을 하지 않으므로
    드리프트가 생길 수 없고, 자막 위치는 세그먼트 안의 상대 오프셋으로 나온다.
  · 나레이션 길이는 절대 깎지 않는다. 자리가 모자라면 기하를 바꾸는 대신
    자막 없는 대사가 새는 구간만 음소거한다.
"""

import math

import timing

# ── 상수 ────────────────────────────────────────────────────────────────────
PRE = 0.20             # 대사 시작 전 여유 (이웃 간격 절반으로 클램프됨)
POST = 0.30            # 대사 끝 후 여유
MERGE_GAP = 1.0        # 이 이하로 벌어진 인접 소스는 하나로 이어 붙인다
MAX_BACKFILL = 8.0     # 앞 나레이션이 이보다 더 거슬러 올라가면 경고
NARR_DUCK_VOLUME = 0.0 # 자막 없는 대사가 새는 구간의 볼륨
TITLE_S = 3.0          # 제목 자막 노출 시간
# 한 블록의 자막은 **위로 쌓인다** — 새 줄이 늘 맨 아래 같은 자리에 오고
# 앞 줄이 한 칸씩 위로 밀린다. 블록이 바뀌면 다시 1번줄부터.
SUB_ROWS_MAX = 8       # 이보다 많이 쌓이면 오래된 줄부터 화면 밖으로 뺀다
# 나레이션 조각이 이보다 짧아지면 안 자르고 문장을 통째로 낸다. 실측 최소
# 조각이 0.48초(중경삼림 레퍼런스 전수)라 이 분기는 안 탄다 — 안전망이다.
# 실측 중앙 0.7초를 임계로 쓰면 정답의 절반이 걸린다. 목표값이 아니라 하한이다.
NARR_MIN_PIECE_S = 0.30

REWIND_BUG = 5.0       # 이보다 작은 음수 간격은 버그, 큰 것은 블록 간 점프


class LayoutError(RuntimeError):
    pass


def narration_seconds(text, ms_per_unit=timing.MS_PER_UNIT):
    """나레이션 발화 길이 추정.

    글자수 × 0.12 가 아니다 — 공백·문장부호는 가중치가 0이라
    정답 샘플 409자는 raw_weight 317단위 = 38.0초지 49초가 아니다.
    """
    return timing.raw_weight(text) * ms_per_unit / 1000.0


def spread_lines(t0, t1, lines):
    """한 덩어리의 여러 줄에 **시각을 나눠** 준다. → [(t_start, 줄), …]

    같은 `t_start` 를 주면 안 된다. `_stack` 이 자막 길이를 「다음 자막이 시작할
    때까지」로 재기 때문에, 두 줄이 같은 시각이면 앞줄이 길이 0이 되어 1프레임
    미만으로 **버려진다.** 실측(중경삼림 레퍼런스): 2줄짜리 자막 10개의 첫 줄이
    10개 전부 화면에서 사라졌다 — `내일 저녁 8시` 가 날아가 약속 시각이 안 나왔다.

    나누는 기준은 글자 수가 아니라 **발화 가중치**다. 공백·문장부호는 가중치가
    0이라(`timing._speech_weight`) `받게 된 할`(6자)은 실제 4음절이다. 글자로
    나누면 실제보다 50% 길게 잡힌다.

    발음할 것이 하나도 없는 줄(문장부호만)은 가중치가 0이라 앞줄과 시각이 겹친다
    — 그런 줄은 최소 몫을 준다.
    """
    if len(lines) <= 1:
        return [(t0, ln) for ln in lines]
    w = [max(timing.raw_weight(ln), 0.5) for ln in lines]
    tot = sum(w)
    span = max(0.0, t1 - t0)
    out, acc = [], 0.0
    for ln, x in zip(lines, w):
        out.append((t0 + span * acc / tot, ln))
        acc += x
    return out


# ── 배치 ────────────────────────────────────────────────────────────────────

def _pre_post(cue_a, cue_b, by_idx):
    """여유를 이웃 간격의 절반 안으로 묶는다 → 두 아이템의 경계가 중점에서 만난다."""
    prev = by_idx.get(cue_a["idx"] - 1)
    nxt = by_idx.get(cue_b["idx"] + 1)
    gap_b = (cue_a["start_s"] - prev["end_s"]) if prev else 99.0
    gap_a = (nxt["start_s"] - cue_b["end_s"]) if nxt else 99.0
    return min(PRE, max(0.0, gap_b) / 2.0), min(POST, max(0.0, gap_a) / 2.0)


def _place_block(bi, blk, by_idx, dur_of, warn):
    """한 블록의 아이템에 소스 창 (src0, src1) 을 매긴다."""
    items = blk["items"]
    anchored = [k for k, it in enumerate(items)
                if it["kind"] == "dialogue" and (it["cues"] or it.get("span"))]

    placed = [None] * len(items)

    # 1) 대사 아이템 — 여유는 이웃 간격 절반으로 클램프
    for k in anchored:
        it = items[k]
        if it.get("span"):
            # 대본에 박힌 구간은 여유까지 이미 반영된 최종값이라 그대로 쓴다
            placed[k] = list(it["span"])
            continue
        a, b = it["cues"][0], it["cues"][-1]
        pre, post = _pre_post(a, b, by_idx)
        placed[k] = [a["start_s"] - pre, b["end_s"] + post]

    if not anchored:
        # b-roll / 대사 없는 블록 — 헤더 범위에 균등 분산.
        # j/n 균등이면 마지막 구간에 안 닿아 왕복마다 범위가 절반씩 줄어든다.
        if not blk["win"]:
            raise LayoutError(
                "블록%d 에 대사도 헤더 범위도 없습니다 — 어디를 잘라야 할지 알 수 없습니다."
                % (bi + 1))
        S, E = blk["win"]
        n = len(items)
        for j, it in enumerate(items):
            L = dur_of(it)
            pos = S if n == 1 else S + max(0.0, (E - S - L)) * j / (n - 1)
            placed[j] = [pos, pos + L]
        return placed

    # 2) 첫 대사보다 앞의 나레이션 — 뒤로 거슬러 채운다 (소스가 붙어 컷이 안 보인다)
    first = anchored[0]
    cursor = placed[first][0]
    for k in range(first - 1, -1, -1):
        L = dur_of(items[k])
        placed[k] = [cursor - L, cursor]
        cursor -= L
    if first > 0:
        back = placed[first][0] - placed[0][0]
        if back > MAX_BACKFILL:
            warn("블록%d 앞 나레이션이 %.1f초 거슬러 올라갑니다 — 장면이 바뀔 수 있습니다."
                 % (bi + 1, back))

    # 3) 그 뒤의 나레이션·강등 문단 — 직전 아이템에 이어 붙인다
    cursor = placed[first][1]
    for k in range(first + 1, len(items)):
        if placed[k] is not None:            # 대사 아이템
            cursor = placed[k][1]
            continue
        L = dur_of(items[k])
        placed[k] = [cursor, cursor + L]
        cursor += L
    return placed


def _mute_spans(src0, src1, cues):
    """소스 창 [src0, src1) 안에서 **자막 없이 들리는 대사** 구간."""
    out = []
    for c in cues:
        if c["end_s"] <= src0 or c["start_s"] >= src1:
            continue
        out.append((max(src0, c["start_s"]), min(src1, c["end_s"])))
    if not out:
        return []
    out.sort()
    merged = [list(out[0])]
    for a, b in out[1:]:
        if a <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [tuple(m) for m in merged]


def build(doc, cues, movie_duration_s, fps=24.0, narration_durs=None,
          ms_per_unit=timing.MS_PER_UNIT, mute_under_narration=False, log=print):
    """doc → {"segments","subs","audio","total_s","headers","warnings","stats"}

    narration_durs — TTS 정렬 결과가 있으면 나레이션 순서대로의 실제 길이.
    """
    # 함수 안에서 부른다 — `script_io` 는 `timing`·`wrap` 만 쓰므로 순환은 없지만,
    # 모듈 상단에서 서로를 부르면 나중에 한쪽이 다른 쪽을 import 할 때 터진다.
    import script_io
    warnings = []

    def warn(msg):
        warnings.append(msg)

    by_idx = {c["idx"]: c for c in cues}
    min_seg = 1.0 / max(1.0, fps)

    nar_i = [0]

    def dur_of(it):
        if it["kind"] == "narration" and narration_durs is not None:
            k = nar_i[0]
            nar_i[0] += 1
            if k < len(narration_durs):
                return max(min_seg, float(narration_durs[k]))
        return max(min_seg, narration_seconds(it["text"], ms_per_unit))

    # ── 1. 아이템별 소스 창 ──────────────────────────────────────────────
    units = []            # 문서 순서. {"bi","item","src0","src1"}
    for bi, blk in enumerate(doc["blocks"]):
        placed = _place_block(bi, blk, by_idx, dur_of, warn)
        for it, (s0, s1) in zip(blk["items"], placed):
            units.append({"bi": bi, "item": it, "src0": s0, "src1": s1})

    # ── 2. 영화 밖으로 나간 창 되돌리기 ─────────────────────────────────
    for u in units:
        L = u["src1"] - u["src0"]
        if u["src0"] < 0:
            warn("블록%d 의 한 구간이 영화 시작 이전을 가리켜 0초로 당겼습니다." % (u["bi"] + 1))
            u["src0"], u["src1"] = 0.0, L
        if u["src1"] > movie_duration_s:
            warn("블록%d 의 한 구간이 영화 끝(%s)을 넘어 당겼습니다."
                 % (u["bi"] + 1, _hms(movie_duration_s)))
            u["src1"] = movie_duration_s
            u["src0"] = max(0.0, movie_duration_s - L)
        if u["src1"] - u["src0"] < min_seg:
            raise LayoutError(
                "블록%d 에 길이 %.3f초짜리 구간이 생겼습니다 (최소 %.3f초). "
                "나레이션이 비었거나 범위가 뒤집혔습니다."
                % (u["bi"] + 1, u["src1"] - u["src0"], min_seg))

    # ── 3. 자막 없는 대사가 새는 구간 음소거 ───────────────────────────
    pieces = []           # {"bi","src0","src1","volume","kind","tag","unit"}
    for u in units:
        it = u["item"]
        kind = ("dialogue" if (it["kind"] == "dialogue"
                               and (it["cues"] or it.get("span")))
                else "narration")
        if kind == "dialogue":
            pieces.append(_piece(u, u["src0"], u["src1"], 1.0, kind, ""))
            continue
        spans = (_mute_spans(u["src0"], u["src1"], cues)
                 if mute_under_narration else [])
        if not spans:
            pieces.append(_piece(u, u["src0"], u["src1"], 1.0, kind, ""))
            continue
        it["intruded"] = True
        # 자르는 위치를 그대로 쓰면 1프레임보다 짧은 조각이 나온다 —
        # CapCut 은 그런 세그먼트를 못 받는다(실측: 708쌍 중 12건이
        # "구간이 1프레임보다 짧습니다: 0.0235초" 로 죽었다).
        # 조각이 한 프레임을 못 채우면 그 경계를 아예 버려 앞뒤와 합친다.
        bounds = [u["src0"]]
        for a, b in spans:
            bounds += [max(u["src0"], a), min(u["src1"], b)]
        bounds.append(u["src1"])
        keep = [bounds[0]]
        for x in bounds[1:]:
            if x - keep[-1] >= min_seg - 1e-9:
                keep.append(x)
        keep[-1] = u["src1"]
        if len(keep) < 2 or keep[1] - keep[0] < min_seg - 1e-9:
            pieces.append(_piece(u, u["src0"], u["src1"], 1.0, kind, ""))
            continue
        for i in range(len(keep) - 1):
            a, b = keep[i], keep[i + 1]
            mid = (a + b) / 2.0
            muted = any(s <= mid < e for s, e in spans)
            pieces.append(_piece(u, a, b,
                                 NARR_DUCK_VOLUME if muted else 1.0,
                                 kind, "음소거" if muted else ""))

    # ── 4. 인접 소스 잇기 ───────────────────────────────────────────────
    segs = []
    for p in pieces:
        if segs:
            prev = segs[-1]
            d = p["src0"] - prev["src1"]
            if d < -1e-6 and abs(d) < REWIND_BUG:
                # 자막 번호가 **앞으로 가는데** 소스가 뒤로 가면 기하학적으로 불가능하다
                # = 여유 클램프 버그. 번호가 뒤로 가는 건 사용자가 문단 순서를 바꾼
                # 것이므로 막지 않는다(막으면 순서 교체 편집이 통째로 죽는다).
                forward = (prev.get("cue_to") is not None
                           and p.get("cue_from") is not None
                           and prev["cue_to"] < p["cue_from"])
                if (prev["kind"] == "dialogue" and p["kind"] == "dialogue"
                        and forward and not prev.get("mixed")):
                    raise LayoutError(
                        "대사 구간이 %.3f초 되감깁니다 (블록%d). 여유 클램프 버그입니다."
                        % (-d, p["bi"] + 1))
                warn("블록%d 에서 화면이 %.1f초 되감깁니다 — "
                     "문단 순서를 바꿨거나 나레이션이 다음 대사를 넘어섭니다."
                     % (p["bi"] + 1, -d))
            elif (-1e-6 <= d <= MERGE_GAP
                  and abs(prev["volume"] - p["volume"]) < 1e-9
                  and prev["bi"] == p["bi"]):
                # 종류가 다른 조각이 섞였다고 표시해 둔다. 음소거를 끄면 나레이션과
                # 대사가 볼륨이 같아 여기서 합쳐지는데, 합친 조각은 kind·cue_to 를
                # 대사 것으로 물려받아 **아래 되감기 검사가 "대사→대사"로 오판**한다.
                # 실측: 이 표시가 없으면 가까운 두 큐 사이에 나레이션을 넣은 45건 중
                # 43건이 "여유 클램프 버그입니다" 로 죽었다.
                if prev["kind"] != p["kind"]:
                    prev["mixed"] = True
                prev["src1"] = p["src1"]
                p["merged_into"] = prev
                continue
        segs.append(p)

    # ── 5. 타임라인 — 소스 창을 그대로 이어 붙인다 ─────────────────────
    t = 0.0
    for s in segs:
        s["t_start"] = t
        s["t_dur"] = s["src1"] - s["src0"]
        t += s["t_dur"]
    total = t

    def seg_of(piece):
        while piece.get("merged_into"):
            piece = piece["merged_into"]
        return piece

    def t_at(piece, src):
        s = seg_of(piece)
        return s["t_start"] + (src - s["src0"])

    # ── 6. 자막 ─────────────────────────────────────────────────────────
    # 자막은 **아이템** 단위로 낸다. 음소거로 조각난 나레이션도 자막은 하나다
    # (조각 기준으로 돌면 첫 조각이 음소거일 때 자막이 통째로 사라진다).
    first_piece = {}
    for p in pieces:
        first_piece.setdefault(id(p["unit"]), p)
    subs = []
    for u in units:
        p = first_piece[id(u)]
        it = u["item"]
        u["t_start"] = t_at(p, u["src0"])      # TTS 오디오를 꽂을 자리
        u["t_dur"] = u["src1"] - u["src0"]
        if it["kind"] == "dialogue" and (it["cues"] or it.get("span")):
            # 화면에 띄울 것: 번역(`>`)이나 번호 앵커(`#476`)가 있으면 대본에 적힌
            # 한국어를, 아니면 자막 원문을 그대로 쓴다.
            # 판단을 여기 인라인으로 두면 검사가 따로 구현하게 되고 언젠가
            # 화면과 어긋난다 — `script_io` 에 하나만 둔다.
            tr = script_io.screen_lines(it)
            # 자막은 **한 줄이 한 칸**을 차지한다. 두 줄짜리 큐를 카드 하나로
            # 띄우면 그 칸만 두 배 높아져 줄 간격이 들쭉날쭉해진다.
            # 같은 시각에 시작하므로 두 줄이 함께 떴다가 함께 밀려 올라간다.
            if tr:
                # 시각 앵커면 자막 큐가 아예 없다 — 구간 시작에 띄운다
                c0 = it["cues"][0] if it["cues"] else None
                at = max(u["src0"], c0["start_s"]) if c0 else u["src0"]
                end = max(at, it["cues"][-1]["end_s"]) if it["cues"] else u["src1"]
                for t, ln in spread_lines(t_at(p, at), t_at(p, end), tr):
                    subs.append({"t_start": t,
                                 "lines": [ln], "kind": "dialogue",
                                 "bi": u["bi"],
                                 "src_no": c0["src_no"] if c0 else None})
            else:
                for c in it["cues"]:
                    at = t_at(p, max(u["src0"], c["start_s"]))
                    end = t_at(p, max(c["start_s"], c["end_s"]))
                    for t, ln in spread_lines(at, max(at, end), c["lines"]):
                        subs.append({"t_start": t, "lines": [ln],
                                     "kind": "dialogue",
                                     "bi": u["bi"], "src_no": c["src_no"]})
        else:
            subs.append({"t_start": t_at(p, u["src0"]),
                         "lines": [it["text"]], "kind": "narration",
                         "bi": u["bi"], "src_no": None,
                         "speech_s": max(0.0, u["src1"] - u["src0"])})
    subs.sort(key=lambda s: s["t_start"])
    # 정렬 **뒤에** 조각낸다 — 「다음 자막까지」를 알아야 구간을 클램프할 수 있고,
    # 조각은 원래 자리 안에서만 나뉘므로 정렬이 안 깨진다.
    subs = _split_narration(subs, min_seg)
    subs = _stack(subs, total, min_seg)

    if doc.get("title"):
        # 제목은 화면 위(y=+0.45), 자막은 아래(y=-0.50)라 겹쳐 보이지 않는다.
        # 그래서 제목만 쓰는 트랙을 따로 둔다 — 예전처럼 나레이션과 한 트랙에
        # 두면 대본이 나레이션으로 시작할 때(탭에서 흔히 하는 편집) 둘 다 t=0 이라
        # CapCut 트랙 규칙에 걸려 "세그먼트가 겹칩니다" 로 죽었다.
        subs.insert(0, {"t_start": 0.0,
                        "t_dur": max(min_seg, min(TITLE_S, total)),
                        "lines": [doc["title"]], "kind": "title", "bi": 0,
                        "src_no": None})

    # ── 7. 블록별 헤더 재계산 (있던 범위는 잃지 않는다) ────────────────
    headers = []
    for bi, blk in enumerate(doc["blocks"]):
        mine = [u for u in units if u["bi"] == bi]
        if not mine:
            # 헤더만 남고 내용이 지워진 블록. 탭에서 문단을 지우면 이렇게 된다.
            # 컷도 자막도 만들지 않으니 그냥 원래 헤더를 그대로 두고 알려준다.
            # (예전엔 여기서 min() 이 빈 시퀀스로 ValueError 를 냈다.)
            headers.append(blk["header"])
            warnings.append("블록%d 이 비어 있습니다 (헤더만 있고 대사·나레이션이 "
                            "없습니다) — 아무것도 만들지 않고 건너뜁니다." % (bi + 1))
            continue
        a = min(u["src0"] for u in mine)
        b = max(u["src1"] for u in mine)
        if blk["win"]:
            a, b = min(a, blk["win"][0]), max(b, blk["win"][1])
        headers.append(script_io.render_header(a, b, blk["note"]))

    # ── 8. 오디오 (TTS) ─────────────────────────────────────────────────
    audio = []

    # ── 9. 조립 검사 — 전부 raise ───────────────────────────────────────
    for s in segs:
        if not (s["src0"] < s["src1"]):
            raise LayoutError("구간 시작≥끝: %r" % s)
        if s["t_dur"] < min_seg - 1e-9:
            raise LayoutError("구간이 1프레임보다 짧습니다: %.4f초" % s["t_dur"])
        if s["src0"] < -1e-6 or s["src1"] > movie_duration_s + 1e-6:
            raise LayoutError("구간이 영화 밖입니다: %.3f~%.3f" % (s["src0"], s["src1"]))
        if abs(s["t_dur"] - (s["src1"] - s["src0"])) > 1e-6:
            raise LayoutError("타임라인 길이와 소스 길이가 다릅니다")
    if not segs:
        raise LayoutError("영상 구간이 하나도 없습니다.")
    if abs(sum(s["t_dur"] for s in segs) - total) > 1e-6:
        raise LayoutError("길이 합이 총 길이와 다릅니다")
    for i in range(len(segs) - 1):
        if abs(segs[i]["t_start"] + segs[i]["t_dur"] - segs[i + 1]["t_start"]) > 1e-6:
            raise LayoutError("타임라인에 빈틈이 있습니다")

    stats = {
        "blocks": len(doc["blocks"]),
        "cuts": len(segs),
        "subs": len(subs),
        "narrations": sum(1 for _, it in _items(doc) if it["kind"] == "narration"),
        "dialogues": sum(1 for _, it in _items(doc) if it["kind"] == "dialogue"),
        "demoted": sum(1 for _, it in _items(doc)
                       if it["kind"] == "dialogue" and not it["cues"]),
        "muted": sum(1 for s in segs if s["volume"] < 1.0),
        "merged": len(pieces) - len(segs),
        "total_s": total,
    }
    return {"segments": segs, "subs": subs, "audio": audio, "total_s": total,
            "headers": headers, "warnings": warnings, "stats": stats,
            "units": units}


def _piece(u, s0, s1, vol, kind, tag):
    cs = u["item"].get("cues") or []
    return {"bi": u["bi"], "src0": s0, "src1": s1, "volume": vol,
            "kind": kind, "tag": tag, "unit": u,
            "cue_from": cs[0]["idx"] if cs else None,
            "cue_to": cs[-1]["idx"] if cs else None}


def _items(doc):
    return [(bi, it) for bi, blk in enumerate(doc["blocks"]) for it in blk["items"]]


def _hms(sec):
    sec = max(0, int(sec))
    return "%d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def _split_narration(subs, min_seg, min_piece_s=NARR_MIN_PIECE_S):
    """나레이션 자막을 **조각으로 나눠 차례로** 띄운다.

    유저 완성본 3편은 나레이션 한 문장을 1.5~2조각으로 잘라 순차로 보여 준다
    (화면 한 줄 중앙 8자 · 최대 14자 · 15자 초과 0줄, 58줄 전수). 도구는 문장을
    통째로 한 줄에 밀어 넣어 **중앙 21자 · 최대 44자**가 나갔다 — 열에 일곱이
    기준 초과였다.

    시각은 조각의 **발화 가중치 비례**로 나눈다(`spread_lines` 와 같은 이유).
    「초당 10.4자」에 새 상수를 두지 않는다 — `narration_seconds` 가 이미
    raw_weight × 120ms 라서 10.2자/초이고, 상수를 또 두면 `timing.MS_PER_UNIT`
    캘리브레이션이 한쪽에만 먹는다.

    조각 하나가 `min_piece_s` 보다 짧아지면 **조각 수를 줄이지 말고 문장을 통째로**
    낸다. 줄이면 「반만 뜨는」 상태가 생기고 어디를 합칠지가 또 하나의 조율 대상이
    된다. 전부 아니면 전무면 최악이 「오늘과 똑같음」이라 안전하다. 실측 최소
    조각이 0.48초라 이 분기는 사실상 안 탄다 — 순수 안전망이다.
    """
    import wrap
    out = []
    for i, s in enumerate(subs):
        if s["kind"] != "narration":
            out.append(s)
            continue
        parts = wrap.split_narration(s["lines"][0])
        span = s.get("speech_s") or 0.0
        nxt = next((x["t_start"] for x in subs[i + 1:]), None)
        if nxt is not None:
            # 되감기 경고 상황에서도 조각이 다음 자막을 넘지 않게 — 넘으면
            # 정렬이 깨지고 「동시에 뜨는 자막은 하나뿐」이 무너진다.
            span = min(span, max(0.0, nxt - s["t_start"]))
        if len(parts) < 2 or span / len(parts) < min_piece_s:
            out.append(s)
            continue
        for k, (t, ln) in enumerate(spread_lines(s["t_start"],
                                                 s["t_start"] + span, parts)):
            piece = dict(s, t_start=t, lines=[ln])
            piece.pop("speech_s", None)     # 조각에는 뜻이 없다 — 헷갈리지 않게
            if k:
                piece["cont"] = True        # 첫 조각과 같은 단에 선다
            out.append(piece)
    return out


def _stack(subs, total, min_seg):
    """자막마다 **자기 구간에 딱 한 번**. 블록 안 순서대로 줄 자리를 옮겨 간다.

    대사에는 시작과 끝이 있다. 끝났는데도 화면에 남겨 두면 같은 글자가 뒤에
    또 나오고, 편집 화면에서는 조각이 여러 트랙에 흩어진다. 그러니 다음 자막이
    나오면 앞 자막은 **지운다.**

    row 는 블록 안 순서다 — 첫 자막이 0(맨 아래), 다음이 1, … 블록이 바뀌면
    다시 0부터. 같은 row 끼리는 시간이 안 겹쳐 CapCut 트랙 하나에 들어간다.

    길이는 **다음 자막이 나올 때까지**다. 대사가 끝나는 순간 지우면 컷 사이에
    자막 없는 구간이 생겨 화면이 휑해진다.
    """
    out = []
    for i, s in enumerate(subs):
        t0 = s["t_start"]
        t1 = subs[i + 1]["t_start"] if i + 1 < len(subs) else total
        dur = min(t1, total) - t0
        if dur < min_seg:
            # 1프레임을 못 채우면 화면에 보이지도 않고 CapCut 이 받지도 않는다
            continue
        # 블록 안에서 몇 번째인가 = 줄 자리
        # 같은 문장의 뒷조각은 **새 단이 아니다.** 한 문장이 한 단이라야 CapCut 에서
        # "몇 번째 단"을 통째로 골라 스타일을 바꿀 수 있다. 안 묶으면 8단 꼭대기에
        # 4개 → 13개가 몰리고 한 문장의 조각이 세 트랙에 흩어진다.
        row = 0
        for k in range(i - 1, -1, -1):
            if subs[k]["bi"] != s["bi"]:
                break
            if not subs[k].get("cont"):
                row += 1
        if s.get("cont"):
            row -= 1                       # 내 첫 조각과 같은 단에 선다
        out.append(dict(s, t_start=t0, t_dur=dur,
                        row=min(max(0, row), SUB_ROWS_MAX - 1)))
    return out


def apply_headers(doc, headers):
    """계산된 범위를 doc 에 실어 script_io.write 가 쓰게 한다."""
    for blk, h in zip(doc["blocks"], headers):
        blk["header_out"] = h
    return doc


def apply_spans(doc, pl):
    """계산된 소스 구간을 대사 문단에 박는다 (`@…~…` 로 쓰이게 한다).

    이러면 그 대본은 **자막 없이도** 다시 만들 수 있다. 대신 대사를 고쳐도
    시각이 따라오지 않고, 시각이 틀려도 조용히 지나간다 — 그래서 선택이다.
    """
    for u in pl["units"]:
        it = u["item"]
        if it["kind"] == "dialogue" and (it["cues"] or it.get("span")):
            it["span"] = (round(u["src0"], 2), round(u["src1"], 2))
    return doc
