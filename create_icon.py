"""
create_icon.py — PlotCut 아이콘 생성 (PIL)

모티프: **결과물 그 자체.** 세로 9:16 프레임 안에 가로 영상 밴드(레터박스)가 들어가고,
그 아래에 대사 자막(흰색) 한 줄과 나레이션 자막(연노랑) 한 줄이 놓인다 —
PlotCut이 실제로 만들어 내는 화면이다.

이전 버전(필름 스트립 + 대본 줄)은 막대그래프 + 상승 화살표로 읽혀서
퀀트/금융 아이콘처럼 보였다. 그래서 통째로 갈아엎었다.
CutSync(3트랙 스택)·LineWrapper와도 한눈에 구분된다.

슈퍼샘플링(4x) 후 축소해 라운드 코너를 매끄럽게 처리, 멀티 사이즈 .ico로 저장.
"""
import os

try:
    from PIL import Image, ImageDraw
except ImportError:
    raise SystemExit("Pillow가 필요합니다:  pip install pillow")

# 팔레트 (gui.py와 동일 계열)
BG_TOP = (0x2e, 0x2e, 0x48)
BG_BOT = (0x16, 0x16, 0x24)
ACCENT = (0x7c, 0x85, 0xf0)
FRAME = (0x8d, 0x95, 0xf5)
BAND_T = (0x5a, 0x63, 0xc8)
BAND_B = (0x39, 0x40, 0x8f)
WHITE = (0xf4, 0xf4, 0xff)
YELLOW = (0xff, 0xe8, 0x6b)
BLACK = (0x0d, 0x0d, 0x16)

S = 256
SS = 4                      # 슈퍼샘플링 배율
W = S * SS


def _lerp(a, b, t):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def _vgrad(draw, box, top, bot):
    x0, y0, x1, y1 = box
    h = max(1, y1 - y0)
    for y in range(y0, y1):
        draw.line([(x0, y), (x1, y)], fill=_lerp(top, bot, (y - y0) / h))


def build():
    img = Image.new("RGB", (W, W), BG_BOT)
    d = ImageDraw.Draw(img)
    _vgrad(d, (0, 0, W, W), BG_TOP, BG_BOT)

    # ── 세로 9:16 프레임 (숏츠 화면) ──────────────────────────────────
    fh = int(W * 0.74)                       # 프레임 높이
    fw = int(fh * 9 / 16)                    # 9:16
    fx = (W - fw) // 2
    fy = (W - fh) // 2
    r = int(W * 0.045)
    d.rounded_rectangle([fx, fy, fx + fw, fy + fh], radius=r, fill=BLACK,
                        outline=FRAME, width=int(W * 0.018))

    inset = int(W * 0.018)
    ix0, iy0 = fx + inset, fy + inset
    ix1, iy1 = fx + fw - inset, fy + fh - inset
    iw = ix1 - ix0

    # ── 가로 영상 밴드 (레터박스) ─────────────────────────────────────
    bh = int(iw * 9 / 16)                    # 16:9 영상이 폭에 맞춰 들어간다
    by0 = iy0 + int((iy1 - iy0) * 0.30) - bh // 2
    _vgrad(d, (ix0, by0, ix1, by0 + bh), BAND_T, BAND_B)

    # 재생 삼각형 — 밴드 한가운데, 프레임 안에 갇혀 있어 차트로 안 보인다
    cx, cy = (ix0 + ix1) // 2, by0 + bh // 2
    t = int(bh * 0.30)
    d.polygon([(cx - t * 0.55, cy - t), (cx - t * 0.55, cy + t),
               (cx + t * 0.85, cy)], fill=WHITE)

    # ── 자막 두 줄 — 대사(흰색) / 나레이션(연노랑) ────────────────────
    lh = int(W * 0.032)                      # 줄 두께
    gap = int(W * 0.042)
    y = by0 + bh + int(W * 0.085)
    pad = int(iw * 0.13)
    d.rounded_rectangle([ix0 + pad, y, ix1 - pad, y + lh],
                        radius=lh // 2, fill=WHITE)
    y += gap
    pad2 = int(iw * 0.24)
    d.rounded_rectangle([ix0 + pad2, y, ix1 - pad2, y + lh],
                        radius=lh // 2, fill=YELLOW)

    # 바깥 라운드 마스크
    out = img.resize((S, S), Image.LANCZOS)
    mask = Image.new("L", (W, W), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, W - 1],
                                           radius=int(W * 0.22), fill=255)
    out.putalpha(mask.resize((S, S), Image.LANCZOS))
    return out


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    icon = build()
    png = os.path.join(here, "icon.png")
    ico = os.path.join(here, "icon.ico")
    icon.save(png)
    icon.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                          (64, 64), (128, 128), (256, 256)])
    print("아이콘 생성:", png)
    print("아이콘 생성:", ico)


if __name__ == "__main__":
    main()
