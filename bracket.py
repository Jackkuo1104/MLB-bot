"""
畫季後賽對戰圖（NBA、MLB 共用）
上下兩區各一個分區／聯盟，每一輪由左往右晉級，最右邊中間是總冠軍賽
（比例接近方形，在 Discord 不點開也看得清楚）
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

BOX_W, ROW_H, CAP_H = 236, 46, 32
BOX_H = ROW_H * 2 + CAP_H
COL_GAP, V_GAP = 44, 18
MARGIN, HEADER = 30, 120
SECTION_LABEL, SECTION_GAP = 40, 34  # 每一區上方標題的高度、上下兩區的間距
LOGO = 36

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


def _positions(rounds: list[list], top: float) -> list[list[float]]:
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
           top: list[list[Box]], bottom: list[list[Box]], final: Box,
           accent: tuple[int, int, int]) -> bytes:
    """labels = (上面那區名稱, 下面那區名稱, 總冠軍賽名稱)；top/bottom 由第一輪排到最後一輪"""
    f_title, f_sub = _font(path, 42, True), _font(path, 20)
    f_label, f_team = _font(path, 24, True), _font(path, 24, True)
    f_seed, f_cap = _font(path, 16), _font(path, 17)

    rounds_n = len(top)
    cols = rounds_n + 1
    width = MARGIN * 2 + cols * BOX_W + (cols - 1) * COL_GAP

    def section_h(rounds: list[list[Box]]) -> float:
        n = len(rounds[0])
        return SECTION_LABEL + n * BOX_H + (n - 1) * V_GAP

    tops = [HEADER, HEADER + section_h(top) + SECTION_GAP]
    height = int(tops[1] + section_h(bottom) + MARGIN)
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    draw.rectangle((0, 0, width, 10), fill=accent)
    draw.text((width / 2, 52), title, font=f_title, fill=TEXT, anchor="mm")
    draw.text((width / 2, 92), subtitle, font=f_sub, fill=MUTED, anchor="mm")

    def col_x(c: int) -> float:
        return MARGIN + c * (BOX_W + COL_GAP)

    def draw_box(box: Box, x: float, y: float, highlight: bool = False) -> None:
        draw.rounded_rectangle((x, y, x + BOX_W, y + BOX_H), 10, fill=BOX_BG,
                               outline=accent if highlight else BORDER, width=3 if highlight else 1)
        for i, team in enumerate(box.teams[:2]):
            ry = y + i * ROW_H
            if i:
                draw.line((x + 10, ry, x + BOX_W - 10, ry), fill=BORDER)
            if team.won:
                draw.rectangle((x + 1, ry + 5, x + 6, ry + ROW_H - 5), fill=accent)
            color = MUTED if team.lost or not team.name else TEXT
            tx = x + 14
            if team.seed:
                draw.text((tx, ry + ROW_H / 2), str(team.seed), font=f_seed, fill=MUTED, anchor="lm")
            tx += 18
            logo = _logo(team.logo)
            if logo:
                if team.lost:
                    logo.putalpha(logo.getchannel("A").point(lambda a: a * 0.45))
                img.paste(logo, (int(tx + (LOGO - logo.width) / 2), int(ry + (ROW_H - logo.height) / 2)), logo)
            tx += LOGO + 10
            draw.text((tx, ry + ROW_H / 2), team.name or "待定", font=f_team, fill=color, anchor="lm")
            if team.wins is not None:
                draw.text((x + BOX_W - 16, ry + ROW_H / 2), str(team.wins), font=f_team,
                          fill=accent if team.won else color, anchor="rm")
        if box.caption:
            draw.text((x + BOX_W / 2, y + ROW_H * 2 + CAP_H / 2), box.caption, font=f_cap,
                      fill=MUTED, anchor="mm")

    def connect(x1: float, y1: float, x2: float, y2: float) -> None:
        mid = (x1 + x2) / 2
        draw.line([(x1, y1), (mid, y1), (mid, y2), (x2, y2)], fill=LINE, width=2)

    last_y = []  # 兩區最後一輪（分區冠軍賽）的位置
    for s, rounds in enumerate((top, bottom)):
        draw.text((MARGIN, tops[s] + 14), labels[s], font=f_label, fill=accent, anchor="lm")
        ys = _positions(rounds, tops[s] + SECTION_LABEL)
        for r, boxes in enumerate(rounds):
            for i, box in enumerate(boxes):
                draw_box(box, col_x(r), ys[r][i])
                if r:  # 跟上一輪的來源框連線
                    feeders = [2 * i, 2 * i + 1] if len(rounds[r - 1]) == len(boxes) * 2 else [i]
                    for f in feeders:
                        connect(col_x(r - 1) + BOX_W, ys[r - 1][f] + ROW_H, col_x(r), ys[r][i] + ROW_H)
        last_y.append(ys[-1][0])

    # 最右邊中間的總冠軍賽
    fx, fy = col_x(rounds_n), sum(last_y) / 2
    draw.text((fx + BOX_W / 2, fy - 22), labels[2], font=f_label, fill=accent, anchor="mm")
    draw_box(final, fx, fy, highlight=True)
    for y in last_y:
        connect(col_x(rounds_n - 1) + BOX_W, y + ROW_H, fx, fy + ROW_H)

    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
