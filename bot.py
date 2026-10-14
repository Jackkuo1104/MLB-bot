"""
MLB 即時比分 / 勝負結果 / 簽約交易 / 年度獎項 / 重大新聞 → Discord 機器人
資料來源：MLB Stats API（比分、球員異動、獎項）、ESPN MLB 新聞 API（皆免費，不需要金鑰）
球隊、獎項清單都從 API 動態取得，球隊搬遷改名、新增獎項都不用改程式
"""

import os
import re
import json
import time
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

# ===== 設定 =====
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
_channel_id = (os.getenv("CHANNEL_ID") or "").strip()
CHANNEL_ID = int(_channel_id) if _channel_id.isdigit() else 0
CHECK_SECONDS = float(os.getenv("CHECK_SECONDS", "60"))
# 只追蹤這些球隊的比分和異動（逗號分隔的縮寫，例如 LAD,NYY）；留空代表全部球隊
FOLLOW_TEAMS = {t.strip().upper() for t in os.getenv("FOLLOW_TEAMS", "").split(",") if t.strip()}
# 要推送的異動類型：TR 交易、SFA/SGN 簽約、CLW 讓渡認領、REL 釋出、RET 退休、DES 指定讓渡
TX_TYPES = {t.strip().upper() for t in os.getenv("TX_TYPES", "TR,SFA,SGN,CLW,REL,RET").split(",")
            if t.strip()}
SKIP_MINOR_DEALS = os.getenv("SKIP_MINOR_DEALS", "true").strip().lower() == "true"
# 要推送的 ESPN 新聞類型：HeadlineNews 是突發/重大新聞；可加 Story、Recap、Preview、Media
NEWS_TYPES = {t.strip() for t in os.getenv("NEWS_TYPES", "HeadlineNews").split(",") if t.strip()}
# 每幾分鐘檢查一次年度獎項（每次要查約 200 個獎項，不需要每分鐘查）
AWARD_CHECK_MINUTES = float(os.getenv("AWARD_CHECK_MINUTES", "30"))

MLB_API = "https://statsapi.mlb.com/api/v1"
ESPN_NEWS_URL = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/news"
STATE_FILE = "state.json"
SEEN_LIMIT = 500  # 只保留最近的 ID，避免檔案無限變大
TW = timezone(timedelta(hours=8))
ET = ZoneInfo("America/New_York")  # 自動處理夏令/冬令時間
REFRESH_SECONDS = 12 * 3600  # 球隊、獎項清單多久重新抓一次

TEAMS = {  # 預設的 MLB 球隊 ID → (縮寫, 中文隊名)；執行時會用 API 的最新球隊清單更新
    108: ("LAA", "天使"), 109: ("AZ", "響尾蛇"), 110: ("BAL", "金鶯"), 111: ("BOS", "紅襪"),
    112: ("CHC", "小熊"), 113: ("CIN", "紅人"), 114: ("CLE", "守護者"), 115: ("COL", "洛磯"),
    116: ("DET", "老虎"), 117: ("HOU", "太空人"), 118: ("KC", "皇家"), 119: ("LAD", "道奇"),
    120: ("WSH", "國民"), 121: ("NYM", "大都會"), 133: ("ATH", "運動家"), 134: ("PIT", "海盜"),
    135: ("SD", "教士"), 136: ("SEA", "水手"), 137: ("SF", "巨人"), 138: ("STL", "紅雀"),
    139: ("TB", "光芒"), 140: ("TEX", "遊騎兵"), 141: ("TOR", "藍鳥"), 142: ("MIN", "雙城"),
    143: ("PHI", "費城人"), 144: ("ATL", "勇士"), 145: ("CWS", "白襪"), 146: ("MIA", "馬林魚"),
    147: ("NYY", "洋基"), 158: ("MIL", "釀酒人"),
}
ALL_STAR_TEAMS = {159: ("AL", "美聯明星隊"), 160: ("NL", "國聯明星隊")}
# 目前的大聯盟球隊 ID → (縮寫, 顯示名稱)；新球隊沒有中文名時顯示英文隊名
mlb_teams: dict[int, tuple[str, str]] = dict(TEAMS)
_teams_loaded_at = 0.0

# 收錄的獎項：大聯盟層級（含美聯 103、國聯 104）和世界棒球經典賽 160，排除每週/每月獎
AWARD_LEAGUES = {103, 104, 160}
AWARD_EXCLUDE = re.compile(r"\b(Week|Month)\b", re.I)
AWARD_ZH = {
    "ALMVP": "美聯 MVP", "NLMVP": "國聯 MVP", "ALCY": "美聯賽揚獎", "NLCY": "國聯賽揚獎",
    "ALROY": "美聯新人王", "NLROY": "國聯新人王", "ALMOY": "美聯年度最佳總教練",
    "NLMOY": "國聯年度最佳總教練", "ALCPOY": "美聯年度東山再起獎", "NLCPOY": "國聯年度東山再起獎",
    "ALHAA": "美聯漢克阿倫獎", "NLHAA": "國聯漢克阿倫獎", "ALREL": "美聯年度最佳救援投手",
    "NLREL": "國聯年度最佳救援投手", "ALSS": "美聯銀棒獎", "NLSS": "國聯銀棒獎",
    "ALGG": "美聯金手套獎", "NLGG": "國聯金手套獎", "ALPG": "美聯白金手套獎", "NLPG": "國聯白金手套獎",
    "ALAS": "美聯明星隊名單", "NLAS": "國聯明星隊名單", "ASMVP": "明星賽 MVP",
    "HRDERBYWIN": "全壘打大賽冠軍", "HRDERBY": "全壘打大賽參賽者",
    "ALCSMVP": "美聯冠軍賽 MVP", "NLCSMVP": "國聯冠軍賽 MVP", "WSMVP": "世界大賽 MVP",
    "WSCHAMP": "世界大賽冠軍", "WSCHAMPMGR": "世界大賽冠軍總教練",
    "MLBAFIRST": "年度最佳陣容（第一隊）", "MLBSECOND": "年度最佳陣容（第二隊）",
    "MLBHOF": "名人堂", "MLBRC": "羅伯托克萊門特獎", "MLBCOMHA": "主席歷史成就獎",
    "DHOY": "年度最佳指定打擊", "MLBEXECOY": "年度最佳球團主管", "HUTCH": "哈欽森獎",
    "BABERUTH": "貝比魯斯獎", "LOUGEHRIG": "盧蓋瑞格獎", "MLBPCPOY": "球員票選年度最佳球員",
    "MLBPCMOY": "球員票選年度風雲人物", "WBCMVP": "世界棒球經典賽 MVP", "WBCTT": "經典賽明星隊",
}

TX_LABELS = {  # 異動類型 → (標題, 顏色)
    "TR": ("🔄 交易", 0x1E88E5), "SFA": ("✍️ 簽約", 0x43A047), "SGN": ("✍️ 簽約", 0x43A047),
    "CLW": ("📋 讓渡認領", 0x8E24AA), "REL": ("👋 釋出", 0x757575), "RET": ("🎖️ 退休", 0xF9A825),
    "DES": ("📤 指定讓渡", 0xFB8C00),
}

# 明星賽和季後賽（外卡、分區、聯盟冠軍、世界大賽）才發即時比分；其他比賽只在全部結束後發比分連結
LIVE_TYPES = {"A", "F", "D", "L", "W"}

# 這些狀態（延賽、暫停、取消）出現時另外通知
NOTABLE_STATUS = ("Delayed", "Suspended", "Postponed", "Cancelled")


# ===== 狀態紀錄 =====
def load_state() -> dict | None:
    """回傳上次的狀態；檔案不存在代表第一次執行，回傳 None"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        print("⚠️ state.json 格式錯誤，當作第一次執行處理")
        return None


def save_state(state: dict) -> None:
    for key in ("tx", "news"):
        if key in state:
            state[key] = state[key][-SEEN_LIMIT:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ===== API =====
async def get_json(session: aiohttp.ClientSession, url: str, params: dict | None = None) -> dict:
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_games(session: aiohttp.ClientSession) -> list[dict]:
    """昨天到今天（UTC）的所有比賽，涵蓋美國當地今天的比賽和跨午夜的晚場"""
    today = datetime.now(timezone.utc).date()
    data = await get_json(session, f"{MLB_API}/schedule", {
        "sportId": "1",
        "startDate": str(today - timedelta(days=1)),
        "endDate": str(today),
        "hydrate": "linescore,decisions,probablePitcher,seriesStatus",
    })
    return [g for d in data.get("dates", []) for g in d.get("games", [])]


async def fetch_scoring_plays(session: aiohttp.ClientSession, game_pk: int) -> list[dict]:
    data = await get_json(session, f"{MLB_API}/game/{game_pk}/playByPlay")
    plays = data.get("allPlays", [])
    return [plays[i] for i in data.get("scoringPlays", []) if i < len(plays)]


async def fetch_transactions(session: aiohttp.ClientSession, days: int = 2) -> list[dict]:
    """最近幾天大聯盟球隊的異動，舊到新"""
    today = datetime.now(timezone.utc).date()
    data = await get_json(session, f"{MLB_API}/transactions", {
        "startDate": str(today - timedelta(days=days)), "endDate": str(today)})
    txs = [t for t in data.get("transactions", [])
           if tx_team_ids(t) & mlb_teams.keys()]
    return sorted(txs, key=lambda t: t.get("id", 0))


async def refresh_teams(session: aiohttp.ClientSession) -> None:
    """用 API 的最新大聯盟球隊清單更新 mlb_teams；失敗時沿用舊的"""
    global _teams_loaded_at
    if time.time() - _teams_loaded_at < REFRESH_SECONDS:
        return
    try:
        data = await get_json(session, f"{MLB_API}/teams", {"sportId": "1"})
    except Exception as e:
        print(f"⚠️ 抓取球隊清單失敗，沿用舊資料：{e}")
        return
    teams = {t["id"]: (t.get("abbreviation") or t.get("teamName", "?"),
                       TEAMS.get(t["id"], (None, t.get("teamName") or t.get("name", "?")))[1])
             for t in data.get("teams", []) if t.get("id")}
    if teams:
        mlb_teams.clear()
        mlb_teams.update(teams)
        _teams_loaded_at = time.time()


_award_defs: list[dict] = []
_awards_loaded_at = 0.0


async def fetch_award_defs(session: aiohttp.ClientSession) -> list[dict]:
    """要追蹤的獎項清單（每 12 小時重新抓一次，新設立的獎項會自動加入）"""
    global _award_defs, _awards_loaded_at
    if not _award_defs or time.time() - _awards_loaded_at > REFRESH_SECONDS:
        data = await get_json(session, f"{MLB_API}/awards")
        _award_defs = [
            a for a in data.get("awards", [])
            if a.get("active") and not AWARD_EXCLUDE.search(a.get("name", ""))
            and (a.get("sport", {}).get("id") == 1 or a.get("league", {}).get("id") in AWARD_LEAGUES)
        ]
        _awards_loaded_at = time.time()
    return _award_defs


async def fetch_award_recipients(session: aiohttp.ClientSession, award_id: str,
                                 season: int) -> list[dict]:
    async with session.get(f"{MLB_API}/awards/{award_id}/recipients", params={"season": str(season)},
                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status == 404:  # 該年度還沒公布
            return []
        resp.raise_for_status()
        return (await resp.json()).get("awards", [])


def award_seasons() -> list[int]:
    """1~3 月也檢查去年，接住跨年才公布的獎項"""
    now = datetime.now(ET)
    return [now.year - 1, now.year] if now.month <= 3 else [now.year]


async def fetch_news(session: aiohttp.ClientSession, limit: int = 50) -> list[dict]:
    data = await get_json(session, ESPN_NEWS_URL, {"limit": str(limit)})
    return data.get("articles", [])  # 新到舊


# ===== 小工具 =====
def team_label(team: dict) -> str:
    abbr, name = mlb_teams.get(team.get("id")) or ALL_STAR_TEAMS.get(team.get("id"), (None, None))
    return f"{name} {abbr}" if name else team.get("name", "?")


def team_abbr(team: dict) -> str:
    known = mlb_teams.get(team.get("id")) or ALL_STAR_TEAMS.get(team.get("id"))
    return known[0] if known else team.get("name", "?")


def team_name(team_id: int | None) -> str:
    known = mlb_teams.get(team_id) or TEAMS.get(team_id)
    return known[1] if known else ""


def follows(*team_ids: int) -> bool:
    return not FOLLOW_TEAMS or any(mlb_teams.get(i, ("",))[0] in FOLLOW_TEAMS for i in team_ids)


def tx_team_ids(tx: dict) -> set[int]:
    return {tx.get(k, {}).get("id") for k in ("toTeam", "fromTeam")} - {None}


def game_url(game: dict) -> str:
    return f"https://www.mlb.com/gameday/{game['gamePk']}"


def inning_text(game: dict) -> str:
    ls = game.get("linescore") or {}
    n = ls.get("currentInning")
    if not n:
        return ""
    state = ls.get("inningState", "")
    if state in ("Top", "Bottom"):
        return f"{n} 局{'上' if state == 'Top' else '下'}，{ls.get('outs', 0)} 出局"
    if state == "Middle":
        return f"{n} 局上結束"
    if state == "End":
        return f"{n} 局下結束"
    return f"{n} 局"


def score_title(game: dict) -> str:
    away, home = game["teams"]["away"], game["teams"]["home"]
    return (f"{team_label(away['team'])} {away.get('score', 0)} : "
            f"{home.get('score', 0)} {team_label(home['team'])}")


def series_text(game: dict) -> str:
    s = game.get("seriesStatus") or {}
    if game.get("gameType") in ("R", "S", "E") or not s.get("shortDescription"):
        return ""
    return f"{s['shortDescription']}｜{s.get('result', '')}".rstrip("｜")


def linescore_table(game: dict) -> str:
    ls = game.get("linescore") or {}
    innings = ls.get("innings") or []
    if not innings:
        return ""
    totals = ls.get("teams") or {}

    def row(side: str) -> str:
        cells = " ".join(f"{str(i.get(side, {}).get('runs', 'X')):>2}" for i in innings)
        t = totals.get(side, {})
        name = team_abbr(game["teams"][side]["team"])
        return f"{name:<4}{cells} |{t.get('runs', 0):>3}{t.get('hits', 0):>3}{t.get('errors', 0):>3}"

    header = "    " + " ".join(f"{i.get('num', ''):>2}" for i in innings) + " |  R  H  E"
    return "```\n" + "\n".join([header, row("away"), row("home")]) + "\n```"


def game_embed(game: dict, title: str, lines: list[str], color: int) -> discord.Embed:
    embed = discord.Embed(title=title[:256], url=game_url(game),
                          description="\n".join(l for l in lines if l)[:4000], color=color)
    series = series_text(game)
    embed.set_footer(text=f"MLB｜{series}" if series else "MLB")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


# ===== 訊息內容 =====
def start_message(game: dict) -> tuple[str, discord.Embed]:
    away, home = game["teams"]["away"], game["teams"]["home"]
    pitchers = [t.get("probablePitcher", {}).get("fullName") for t in (away, home)]
    lines = [f"📍 {game.get('venue', {}).get('name', '')}"]
    if all(pitchers):
        lines.append(f"先發：{pitchers[0]} vs {pitchers[1]}")
    title = f"{team_label(away['team'])} @ {team_label(home['team'])}"
    return "⚾ **比賽開始**", game_embed(game, title, lines, 0x041E42)


def score_message(game: dict, plays: list[dict]) -> tuple[str, discord.Embed]:
    lines = [f"⏱️ {inning_text(game)}"]
    lines += [f"• {p.get('result', {}).get('description', '')}" for p in plays[-3:]]
    return "🔥 **得分！**", game_embed(game, score_title(game), lines, 0xD50032)


def final_message(game: dict) -> tuple[str, discord.Embed]:
    away, home = game["teams"]["away"], game["teams"]["home"]
    winner = away if away.get("isWinner") else home if home.get("isWinner") else None
    lines = [f"🏆 **{team_label(winner['team'])}** 獲勝" if winner else "🤝 平手"]
    d = game.get("decisions") or {}
    names = [f"{label}：{d[k]['fullName']}" for k, label in
             (("winner", "勝投"), ("loser", "敗投"), ("save", "救援")) if d.get(k)]
    if names:
        lines.append("｜".join(names))
    lines.append(linescore_table(game))
    return "🏁 **終場**", game_embed(game, score_title(game), lines, 0xFFB81C)


def status_message(game: dict) -> tuple[str, discord.Embed]:
    status = game["status"]
    lines = [f"狀態：{status.get('detailedState', '')}", inning_text(game)]
    if status.get("reason"):
        lines.append(f"原因：{status['reason']}")
    return "⏸️ **比賽狀態更新**", game_embed(game, score_title(game), lines, 0x757575)


def tx_message(tx: dict) -> tuple[str, discord.Embed]:
    label, color = TX_LABELS.get(tx.get("typeCode"), (tx.get("typeDesc", "異動"), 0x757575))
    embed = discord.Embed(title=label, description=tx.get("description", "")[:4000], color=color)
    team_id = next((i for i in (tx.get("toTeam", {}).get("id"), tx.get("fromTeam", {}).get("id"))
                    if i in mlb_teams), None)
    if team_id:
        embed.set_author(name=team_name(team_id),
                         icon_url=f"https://midfield.mlbstatic.com/v1/team/{team_id}/spots/72")
    person_id = tx.get("person", {}).get("id")
    if person_id:
        embed.set_thumbnail(url=headshot_url(person_id))
    embed.set_footer(text=f"MLB 異動｜{tx.get('date', '')}")
    return "📰 **MLB 球員異動**", embed


def headshot_url(person_id: int) -> str:
    return ("https://img.mlbstatic.com/mlb-photos/image/upload/w_180,q_100/"
            f"v1/people/{person_id}/headshot/67/current")


def award_title(award: dict, season: str) -> str:
    award_id = award.get("id", "")
    if award_id.startswith("RETIREDUNI_"):  # 各隊退休背號，ID 後面是球隊 ID
        suffix = award_id.split("_")[-1]
        team = team_name(int(suffix)) if suffix.isdigit() else ""
        return f"{season} {team} 退休背號".replace("  ", " ")
    return f"{season} {AWARD_ZH.get(award_id) or award.get('name', award_id)}"


def recipient_text(r: dict) -> str:
    player = r.get("player") or {}
    pos = (player.get("primaryPosition") or {}).get("abbreviation", "")
    name = player.get("nameFirstLast") or ""
    team = team_name((r.get("team") or {}).get("id"))
    text = " ".join(p for p in (pos, name) if p) or team or "?"
    if name and team:
        text += f"（{team}）"
    if r.get("notes"):
        text += f"｜{r['notes']}"
    return text


def award_message(award: dict, season: str, recipients: list[dict]) -> tuple[str, discord.Embed]:
    if len(recipients) > 12:  # 明星隊、冠軍隊這種名單很長的，依球隊分組
        by_team: dict[str, list[str]] = {}
        for r in recipients:
            team = team_name((r.get("team") or {}).get("id")) or "其他"
            by_team.setdefault(team, []).append((r.get("player") or {}).get("nameFirstLast", "?"))
        lines = [f"**{team}**：{'、'.join(names)}" for team, names in by_team.items()]
        lines.insert(0, f"共 {len(recipients)} 人")
    else:
        lines = [f"• {recipient_text(r)}" for r in recipients]
    embed = discord.Embed(title=award_title(award, season)[:256],
                          description="\n".join(lines)[:4000], color=0xFFD700)
    player_id = (recipients[0].get("player") or {}).get("id")
    if len(recipients) == 1 and player_id:
        embed.set_thumbnail(url=headshot_url(player_id))
    embed.set_footer(text=f"MLB 獎項｜{award.get('name', '')}"[:2048])
    embed.timestamp = datetime.now(timezone.utc)
    return "🏆 **MLB 獎項揭曉**", embed


def news_embed(article: dict) -> discord.Embed:
    embed = discord.Embed(
        title=(article.get("headline") or "（無標題）")[:256],
        url=article.get("links", {}).get("web", {}).get("href"),
        description=(article.get("description") or "")[:4000],
        color=0xCC0000,
    )
    if article.get("byline"):
        embed.set_author(name=article["byline"][:256])
    images = article.get("images") or []
    if images and images[0].get("url"):
        embed.set_image(url=images[0]["url"])
    published = article.get("published")
    if published:
        try:
            embed.timestamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            pass
    embed.set_footer(text="ESPN MLB")
    return embed


def article_id(article: dict) -> str:
    return str(article.get("id") or article.get("links", {}).get("web", {}).get("href", ""))


def wanted_tx(tx: dict) -> bool:
    if tx.get("typeCode") not in TX_TYPES or not follows(*tx_team_ids(tx)):
        return False
    return not (SKIP_MINOR_DEALS and "minor league" in tx.get("description", "").lower())


def day_done_message(date: str, games: list[dict]) -> tuple[str, discord.Embed]:
    finished = [g for g in games if not is_called_off(g)]
    lines = [f"共 {len(finished)} 場比賽已全部結束"]
    if len(finished) < len(games):
        lines.append(f"另有 {len(games) - len(finished)} 場延賽或暫停")
    lines.append("👉 點上方標題查看所有比分")
    embed = discord.Embed(title=f"MLB 全部比分｜{date}", url=f"https://www.mlb.com/scores/{date}",
                          description="\n".join(lines), color=0x041E42)
    embed.set_footer(text="MLB")
    embed.timestamp = datetime.now(timezone.utc)
    return "📋 **今日 MLB 比賽全部結束**", embed


def is_called_off(game: dict) -> bool:
    return any(w in game.get("status", {}).get("detailedState", "")
               for w in ("Suspended", "Postponed", "Cancelled"))


def day_done_updates(games: list[dict], state: dict, initial: bool) -> list:
    """例行賽、熱身賽的日子，當天所有比賽都結束（或延賽）後，發一則比分連結"""
    by_date: dict[str, list[dict]] = {}
    for game in games:
        by_date.setdefault(game.get("officialDate", ""), []).append(game)
    first_time = "days_done" not in state
    done = state.setdefault("days_done", [])
    messages = []
    last_date = str(datetime.now(timezone.utc).date())  # fetch_games 查詢範圍的最後一天
    for date, day_games in sorted(by_date.items()):
        regular = [g for g in day_games if g.get("gameType") not in LIVE_TYPES]
        # 延賽改期的比賽可能帶出範圍外的日期，那天的其他比賽還沒抓到，先跳過
        if not regular or date in done or date > last_date:
            continue
        if all(is_called_off(g) for g in regular):
            continue
        if not all(g.get("status", {}).get("abstractGameState") == "Final" or is_called_off(g)
                   for g in regular):
            continue
        done.append(date)
        if not (initial or first_time):
            messages.append(day_done_message(date, regular))
    state["days_done"] = done[-10:]
    return messages


# ===== 比對新舊資料，產生要發送的訊息 =====
async def game_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    games = await fetch_games(session)
    messages = day_done_updates(games, state, initial)
    new_games = {}
    for game in games:
        pk = str(game["gamePk"])
        status = game.get("status", {})
        abstract, detailed = status.get("abstractGameState", ""), status.get("detailedState", "")
        away, home = game["teams"]["away"].get("score", 0), game["teams"]["home"].get("score", 0)
        prev = state["games"].get(pk)
        cur = {
            "abstract": abstract, "status": detailed, "away": away, "home": home,
            "started": bool(prev and prev["started"]) or detailed == "In Progress"
                       or abstract == "Final",
            "plays": prev["plays"] if prev else (0 if abstract == "Preview" else None),
        }
        new_games[pk] = cur
        # 新出現的比賽只記錄不發文（第一次執行，或機器人關掉一段時間後）
        if prev is None or initial:
            continue
        # 例行賽、熱身賽不發即時比分，改成全部結束後發一次比分連結（見 day_done_updates）
        if game.get("gameType") not in LIVE_TYPES:
            continue
        if game.get("gameType") != "A" and not follows(  # 明星賽不受 FOLLOW_TEAMS 限制
                game["teams"]["away"]["team"].get("id"), game["teams"]["home"]["team"].get("id")):
            continue

        notable = any(w in detailed for w in NOTABLE_STATUS)
        if abstract == "Final":
            if prev["abstract"] != "Final":
                messages.append(status_message(game) if notable else final_message(game))
            continue
        if detailed != prev["status"] and notable:
            messages.append(status_message(game))
        if abstract != "Live":
            continue
        if detailed == "In Progress" and not prev["started"]:
            messages.append(start_message(game))
        if (away, home) != (prev["away"], prev["home"]):
            try:
                plays = await fetch_scoring_plays(session, game["gamePk"])
            except Exception as e:
                print(f"⚠️ 抓取得分過程失敗：{e}")
                plays = []
            new_plays = plays[prev["plays"]:] if prev["plays"] is not None else plays[-1:]
            cur["plays"] = len(plays) if plays else prev["plays"]
            messages.append(score_message(game, new_plays))
    state["games"] = new_games
    return messages


async def tx_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    seen = set(state["tx"])
    new_txs = [t for t in await fetch_transactions(session)
               if wanted_tx(t) and str(t.get("id")) not in seen]
    state["tx"] += [str(t.get("id")) for t in new_txs]
    if initial:
        return []
    messages, posted = [], set()
    for tx in new_txs:
        desc = tx.get("description", "")
        if desc in posted:  # 多人交易常會每個球員各一筆相同敘述，只發一次
            continue
        posted.add(desc)
        messages.append(tx_message(tx))
    return messages


async def news_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    seen = set(state["news"])
    new_articles = [a for a in await fetch_news(session)
                    if a.get("type") in NEWS_TYPES and article_id(a) not in seen]
    new_articles.reverse()  # 舊的先發
    state["news"] += [article_id(a) for a in new_articles]
    if initial:
        return []
    return [("🚨 **MLB 重大消息**", news_embed(a)) for a in new_articles]


def recipient_key(r: dict) -> str:
    return ":".join(str(x) for x in (r.get("id"), r.get("season"), (r.get("player") or {}).get("id"),
                                     (r.get("team") or {}).get("id"), r.get("notes") or ""))


async def award_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    if not initial and time.time() - state.get("awards_checked", 0) < AWARD_CHECK_MINUTES * 60:
        return []
    pairs = [(a, s) for a in await fetch_award_defs(session) for s in award_seasons()]
    limit = asyncio.Semaphore(8)  # 同時最多 8 個請求，避免對 MLB API 太兇

    async def fetch(award: dict, season: int) -> list[dict]:
        async with limit:
            return await fetch_award_recipients(session, award["id"], season)

    results = await asyncio.gather(*(fetch(a, s) for a, s in pairs))
    seen = set(state["awards"])
    keys, new = [], {}
    for (award, season), recipients in zip(pairs, results):
        for r in recipients:
            key = recipient_key(r)
            keys.append(key)
            if key not in seen:
                new.setdefault((award["id"], season), (award, []))[1].append(r)
    state["awards"] = keys  # 只保留目前檢查的年度，檔案不會一直變大
    state["awards_checked"] = time.time()
    if initial:
        return []
    return [award_message(award, str(season), recipients)
            for (_, season), (award, recipients) in new.items()]


async def collect_updates(session: aiohttp.ClientSession) -> tuple[list, dict]:
    """回傳 (要發送的 [(文字, embed)], 更新後的狀態)；呼叫端發完訊息後要 save_state"""
    state = load_state() or {}
    await refresh_teams(session)
    messages = []
    for key, name, check, empty in (("games", "比分", game_updates, {}),
                                    ("tx", "異動", tx_updates, []),
                                    ("news", "新聞", news_updates, []),
                                    ("awards", "獎項", award_updates, [])):
        # 第一次抓到某個來源時只記錄現有資料，不發文，避免洗版
        initial = key not in state
        state.setdefault(key, empty)
        try:
            messages += await check(session, state, initial)
        except Exception as e:
            print(f"⚠️ 抓取{name}失敗：{e}")
            if initial:
                del state[key]  # 下次成功時再初始化
            continue
        if initial:
            print(f"初始化完成，已記錄 {len(state[key])} 筆{name}")
    return messages, state


# ===== Discord 機器人 =====
class MLBBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.session = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        await self.tree.sync()
        self.check_updates.change_interval(seconds=CHECK_SECONDS)
        self.check_updates.start()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        print(f"✅ 已登入：{self.user}，每 {CHECK_SECONDS:g} 秒檢查一次 MLB 比分、異動和新聞")

    @tasks.loop(seconds=60)
    async def check_updates(self):
        messages, state = await collect_updates(self.session)
        channel = self.get_channel(CHANNEL_ID)
        if messages and channel is None:
            print("⚠️ 找不到頻道，請確認 CHANNEL_ID 和機器人權限")
        elif channel is not None:
            for content, embed in messages:
                try:
                    await channel.send(content=content, embed=embed)
                    print(f"已發送：{content} {embed.title}")
                except discord.HTTPException as e:
                    print(f"⚠️ 發送失敗：{e}")
        save_state(state)

    @check_updates.before_loop
    async def before_check(self):
        await self.wait_until_ready()


bot = MLBBot()


def scoreboard_line(game: dict) -> str:
    away, home = game["teams"]["away"], game["teams"]["home"]
    status = game.get("status", {})
    matchup = f"{team_label(away['team'])} @ {team_label(home['team'])}"
    if status.get("abstractGameState") == "Preview":
        start = datetime.fromisoformat(game["gameDate"].replace("Z", "+00:00")).astimezone(TW)
        return f"🕒 {matchup}｜台灣時間 {start:%m/%d %H:%M}"
    score = f"{team_label(away['team'])} **{away.get('score', 0)} : {home.get('score', 0)}** " \
            f"{team_label(home['team'])}"
    if status.get("abstractGameState") == "Final":
        return f"🏁 {score}｜{status.get('detailedState', '')}"
    return f"🔴 {score}｜{inning_text(game) or status.get('detailedState', '')}"


@bot.tree.command(name="score", description="查看今天的 MLB 比分")
async def score_command(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        games = await fetch_games(bot.session)
    except Exception as e:
        await interaction.followup.send(f"⚠️ 抓取比分失敗：{e}")
        return
    today = str(datetime.now(ET).date())
    games = [g for g in games if g.get("officialDate") == today]
    if not games:
        await interaction.followup.send("今天（美國時間）沒有 MLB 比賽。")
        return
    embed = discord.Embed(title=f"MLB 比分｜{today}",
                          description="\n".join(scoreboard_line(g) for g in games)[:4000],
                          color=0x041E42)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="mlb", description="查看最近的 MLB 重大新聞")
@app_commands.describe(count="要顯示幾則（1-5）")
async def mlb_command(interaction: discord.Interaction,
                      count: app_commands.Range[int, 1, 5] = 3):
    await interaction.response.defer()
    try:
        articles = [a for a in await fetch_news(bot.session) if a.get("type") in NEWS_TYPES]
    except Exception as e:
        await interaction.followup.send(f"⚠️ 抓取 ESPN 新聞失敗：{e}")
        return
    if not articles:
        await interaction.followup.send("ESPN 最近 50 則新聞裡沒有重大消息。")
        return
    await interaction.followup.send(embeds=[news_embed(a) for a in articles[:count]])


@bot.tree.command(name="moves", description="查看最近的 MLB 簽約、交易")
@app_commands.describe(days="往回查幾天（1-14）")
async def moves_command(interaction: discord.Interaction,
                        days: app_commands.Range[int, 1, 14] = 3):
    await interaction.response.defer()
    try:
        txs = [t for t in await fetch_transactions(bot.session, days) if wanted_tx(t)]
    except Exception as e:
        await interaction.followup.send(f"⚠️ 抓取 MLB 異動失敗：{e}")
        return
    if not txs:
        await interaction.followup.send(f"最近 {days} 天沒有簽約或交易。")
        return
    lines = list(dict.fromkeys(
        f"{TX_LABELS.get(t.get('typeCode'), ('•',))[0]}｜{t.get('date', '')}｜{t.get('description', '')}"
        for t in reversed(txs)))
    text = ""
    for line in lines:  # 新的在上面，塞到 embed 上限為止
        if len(text) + len(line) > 3900:
            break
        text += line + "\n"
    embed = discord.Embed(title=f"MLB 簽約 / 交易（最近 {days} 天）", description=text, color=0x1E88E5)
    await interaction.followup.send(embed=embed)


if __name__ == "__main__":
    missing = [k for k, v in {"DISCORD_TOKEN": DISCORD_TOKEN, "CHANNEL_ID": CHANNEL_ID}.items()
               if not v]
    if missing:
        raise SystemExit(f"請先在 .env 設定：{', '.join(missing)}（CHANNEL_ID 必須是數字）")
    bot.run(DISCORD_TOKEN)
