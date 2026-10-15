"""
畫季後賽對戰圖（NBA、MLB 共用）
左右兩邊各一個分區／聯盟，由外往內一輪一輪晉級，中間是總冠軍賽
"""

import asyncio
import io
import os
import tempfile
from dataclasses import dataclass, field

import aiohttp
from PIL import Image, ImageDraw, ImageFont

# 中文字型：先找電腦裡現成的，找不到才從 Google Fonts 下載（GitHub Actions 上沒有中文字型）
FONT_FILES = [
    os.getenv("BRACKET_FONT", ""),
    "C:/Windows/Fonts/msjhbd.ttc", "C:/Windows/Fonts/msjh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
FONT_URLS = [
    "https://github.com/google/fonts/raw/main/ofl/notosanstc/NotoSansTC%5Bwght%5D.ttf",
    "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/notosanstc/NotoSansTC%5Bwght%5D.ttf",
]
FONT_CACHE = os.path.join(tempfile.gettempdir(), "NotoSansTC.ttf")

BOX_W, ROW_H, CAP_H = 196, 36, 26
BOX_H = ROW_H * 2 + CAP_H
COL_GAP, V_GAP = 34, 20
MARGIN, HEADER = 28, 118
LOGO = 28

BG, BOX_BG, BORDER = (243, 245, 248), (255, 255, 255), (208, 213, 221)
TEXT, MUTED, LINE = (20, 28, 45), (150, 156, 168), (170, 178, 190)


@dataclass
class Team:
    name: str = ""            # 縮寫，例如 GSW；空字串代表還沒確定
    logo: bytes | None = None
    seed: int | None = None
    wins: int | None = None
    won: bool = False          # 贏下這個系列賽
    lost: bool = False         # 輸掉這個系列賽


@dataclass
class Box:
    teams: list[Team] = field(default_factory=lambda: [Team(), Team()])
    caption: str = ""


async def ensure_font(session: aiohttp.ClientSession) -> str:
    for path in FONT_FILES + [FONT_CACHE]:
        if path and os.path.exists(path):
            return path
    for url in FONT_URLS:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                resp.raise_for_status()
                data = await resp.read()
            with open(FONT_CACHE, "wb") as f:
                f.write(data)
            return FONT_CACHE
        except Exception as e:
            print(f"⚠️ 下載中文字型失敗：{e}")
    raise RuntimeError("找不到中文字型")


async def fetch_logos(session: aiohttp.ClientSession, urls: dict) -> dict:
    """{key: 網址} → {key: 圖片 bytes}，抓不到的就不放"""
    limit = asyncio.Semaphore(8)

    async def one(url):
        async with limit:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    resp.raise_for_status()
                    return await resp.read()
            except Exception:
                return None

    results = await asyncio.gather(*(one(u) for u in urls.values()))
    return {k: r for k, r in zip(urls, results) if r}


def _font(path: str, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(path, size)
    try:  # 可變字型（下載的 Noto Sans TC）要指定粗細
        font.set_variation_by_name(b"Bold" if bold else b"Medium")
    except Exception:
        pass
    return font


def _logo(data: bytes | None) -> Image.Image | None:
    if not data:
        return None
    try:
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        img.thumbnail((LOGO, LOGO), Image.LANCZOS)
        return img
    except Exception:
        return None


def _positions(rounds: list[list], top: int) -> list[list[float]]:
    """每一輪每個框的上緣 y 座標：數量減半就放在兩個來源中間，數量一樣就對齊"""
    ys = [[top + i * (BOX_H + V_GAP) for i in range(len(rounds[0]))]]
    for r in range(1, len(rounds)):
        prev, n = ys[-1], len(rounds[r])
        if n * 2 == len(prev):
            ys.append([(prev[2 * i] + prev[2 * i + 1]) / 2 for i in range(n)])
        else:
            ys.append(prev[:n])
    return ys


def render(path: str, title: str, subtitle: str, labels: tuple[str, str, str],
           left: list[list[Box]], right: list[list[Box]], final: Box,
           accent: tuple[int, int, int]) -> bytes:
    """labels = (左邊名稱, 右邊名稱, 中間名稱)；left/right 由最外圈一輪排到最內圈"""
    f_title, f_sub = _font(path, 34, True), _font(path, 17)
    f_label, f_team = _font(path, 18, True), _font(path, 18, True)
    f_seed, f_cap = _font(path, 13), _font(path, 14)

    rows = max(len(left[0]), len(right[0]))
    cols = len(left) * 2 + 1
    width = MARGIN * 2 + cols * BOX_W + (cols - 1) * COL_GAP
    height = HEADER + rows * BOX_H + (rows - 1) * V_GAP + MARGIN
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    draw.rectangle((0, 0, width, 8), fill=accent)
    draw.text((width / 2, 46), title, font=f_title, fill=TEXT, anchor="mm")
    draw.text((width / 2, 80), subtitle, font=f_sub, fill=MUTED, anchor="mm")

    def col_x(c: int) -> float:
        return MARGIN + c * (BOX_W + COL_GAP)

    def draw_box(box: Box, x: float, y: float, highlight: bool = False) -> None:
        draw.rounded_rectangle((x, y, x + BOX_W, y + BOX_H), 8, fill=BOX_BG,
                               outline=accent if highlight else BORDER, width=3 if highlight else 1)
        for i, team in enumerate(box.teams[:2]):
            ry = y + i * ROW_H
            if i:
                draw.line((x + 8, ry, x + BOX_W - 8, ry), fill=BORDER)
            if team.won:
                draw.rectangle((x + 1, ry + 4, x + 5, ry + ROW_H - 4), fill=accent)
            color = MUTED if team.lost or not team.name else TEXT
            tx = x + 12
            if team.seed:
                draw.text((tx, ry + ROW_H / 2), str(team.seed), font=f_seed, fill=MUTED, anchor="lm")
            tx += 16
            logo = _logo(team.logo)
            if logo:
                if team.lost:
                    logo.putalpha(logo.getchannel("A").point(lambda a: a * 0.45))
                img.paste(logo, (int(tx + (LOGO - logo.width) / 2), int(ry + (ROW_H - logo.height) / 2)), logo)
            tx += LOGO + 8
            draw.text((tx, ry + ROW_H / 2), team.name or "待定", font=f_team, fill=color, anchor="lm")
            if team.wins is not None:
                draw.text((x + BOX_W - 14, ry + ROW_H / 2), str(team.wins), font=f_team,
                          fill=accent if team.won else color, anchor="rm")
        if box.caption:
            draw.text((x + BOX_W / 2, y + ROW_H * 2 + CAP_H / 2), box.caption, font=f_cap,
                      fill=MUTED, anchor="mm")

    def connect(x1: float, y1: float, x2: float, y2: float) -> None:
        mid = (x1 + x2) / 2
        draw.line([(x1, y1), (mid, y1), (mid, y2), (x2, y2)], fill=LINE, width=2)

    mid_y = []
    for side, rounds in (("L", left), ("R", right)):
        ys = _positions(rounds, HEADER)
        for r, boxes in enumerate(rounds):
            c = r if side == "L" else cols - 1 - r
            for i, box in enumerate(boxes):
                draw_box(box, col_x(c), ys[r][i])
                if r:  # 跟上一輪的來源框連線
                    feeders = [2 * i, 2 * i + 1] if len(rounds[r - 1]) == len(boxes) * 2 else [i]
                    pc = c - 1 if side == "L" else c + 1
                    for f in feeders:
                        fy = ys[r - 1][f] + ROW_H
                        if side == "L":
                            connect(col_x(pc) + BOX_W, fy, col_x(c), ys[r][i] + ROW_H)
                        else:
                            connect(col_x(pc), fy, col_x(c) + BOX_W, ys[r][i] + ROW_H)
        mid_y.append(ys[-1][0])
        label_x = col_x(0) + (len(rounds) * (BOX_W + COL_GAP) - COL_GAP) / 2
        if side == "R":
            label_x = width - label_x
        draw.text((label_x, HEADER - 16), labels[0 if side == "L" else 1], font=f_label, fill=accent, anchor="mm")

    # 中間的總冠軍賽
    cx, fy = col_x(len(left)), sum(mid_y) / 2
    draw.text((cx + BOX_W / 2, fy - 18), labels[2], font=f_label, fill=accent, anchor="mm")
    draw_box(final, cx, fy, highlight=True)
    connect(col_x(len(left) - 1) + BOX_W, mid_y[0] + ROW_H, cx, fy + ROW_H)
    connect(col_x(len(left) + 1), mid_y[1] + ROW_H, cx + BOX_W, fy + ROW_H)

    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
