"""
srt_export.py — 정렬 결과를 표준 SRT 자막 파일로 저장

CapCut의 "자막 가져오기"로 바로 불러올 수 있다.
"""


def _ts(seconds):
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(aligned, path, continuous=True, tail_s=0.3):
    """aligned: align.py의 결과 리스트.

    continuous=True — 자막이 끊기지 않도록 각 자막의 끝을 다음 자막 시작까지 연장
                      (나레이션 숏츠 표준). 마지막 자막만 tail_s 만큼 여유.
    """
    blocks = []
    for i, a in enumerate(aligned):
        start = a["start_s"]
        if continuous and i + 1 < len(aligned):
            end = aligned[i + 1]["start_s"]
        else:
            end = a["end_s"] + (tail_s if i + 1 == len(aligned) else 0)
        blocks.append(f"{i + 1}\n{_ts(start)} --> {_ts(end)}\n{a['text']}\n")

    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(blocks))
    return path
