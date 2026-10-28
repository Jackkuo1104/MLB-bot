"""
MLB 即時比分（每局摘要）/ 勝負結果 / 簽約交易 / 年度獎項 / 每週每月最佳 / 重大新聞 / 紀錄 → Discord 機器人
資料來源：MLB Stats API（比分、球員異動、獎項）、ESPN MLB 新聞 API（皆免費，不需要金鑰）
球隊、獎項清單都從 API 動態取得，球隊搬遷改名、新增獎項都不用改程式
"""

import io
import os
import re
import json
import time
import asyncio
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

try:  # 畫季後賽對戰圖用（需要 Pillow）；沒裝的話季後賽戰果只發文字
    import bracket
except ImportError:
    bracket = None

# ===== 設定 =====
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
# bot 會自動發到它被單獨加進去的頻道（頻道權限裡加入這個 bot 並允許發送訊息）；
# 這裡可以另外指定頻道 ID（選填，好幾個用逗號隔開），每則消息都會發到所有頻道
CHANNEL_IDS = [int(c) for c in re.split(r"[,\s]+", os.getenv("CHANNEL_ID") or "") if c.isdigit()]
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
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"  # Google 免費翻譯
# 標題或摘要符合這些字的新聞當作「紀錄」消息發送（不限 NEWS_TYPES）
# 只認「破／追平紀錄」這類說法；record 當動詞或戰績（road record）、報紙名（Bergen Record）不算
RECORD_NEWS = re.compile(r"\b(?:set|sets|setting|break|breaks|breaking|broke|tie|ties|tying|tied|extend|extends|"
                         r"extended)\b[^.]{0,30}?\brecords?\b|record-(?:breaking|setting|tying)|"
                         r"\b(?:franchise|league|MLB|all-time|single-game|single-season|postseason|career|world) "
                         r"records?\b|\ball-time\b|first player|MLB history|major league history|"
                         r"franchise history", re.I)
# 標題有這些字才標成「重大消息」（交易、簽約、傷病、禁賽、人事異動…），其他的標成一般新聞
BIG_NEWS = re.compile(r"^(?:Sources?|Breaking)\b|\btrade[sd]?\b|\btrading\b|\bsign(?:s|ed|ing)?\b|\bagree[sd]?\b|"
                      r"\bcontract|\bextension|\breleased?\b|\bDFA\b|\bdesignated?\b|\binjur|\bsurgery|"
                      r"\bTommy John\b|\btorn\b|\btears?\b|\bfractur|\bsprain|\bstrain|\bconcussion|"
                      r"\bout (?:for|indefinitely|as)\b|\bruled out\b|\b(?:to|will) miss\b|\bsidelined\b|"
                      r"\bsuspen|\bfined\b|\bbann?ed\b|\barrest|\bcharged\b|\bfire[sd]?\b|\bhire[sd]?\b|\bnamed?\b|"
                      r"\bretir|\bdie[sd]\b|\bdeath\b|\bpassed away\b|\breturn(?:s|ing)? as\b|\bresign|"
                      r"\bsteps? down\b|\bposting\b|\bqualifying offer|\bfree agen|\bnon-tender", re.I)
BIG_NEWS_SOURCES = re.compile(r"\bsources? (?:say|said|told|tell)", re.I)  # 摘要寫「消息人士告訴 ESPN」也算
STATE_FILE = "state.json"
SEEN_LIMIT = 500  # 只保留最近的 ID，避免檔案無限變大
TW = timezone(timedelta(hours=8))
ET = ZoneInfo("America/New_York")  # 自動處理夏令/冬令時間
REFRESH_SECONDS = 12 * 3600  # 球隊、獎項清單多久重新抓一次
LOTTERY_TEAMS = 18  # 樂透抽籤的球隊數（沒進季後賽的 18 隊，前 6 順位用抽的）
LOTTERY_DRAWN = 6
LOTTERY_MONTHS = (12, 1)  # 抽籤在 12 月冬季會議；這段期間每次都檢查順位，其他月份每小時一次
DRAFT_LIVE_SECONDS = 15 * 60  # 最後一次有人被選中後 15 分鐘內算「選秀進行中」，GitHub 版會每 20 秒檢查一次
DRAFT_ROUND_ZH = {"PPI": "新秀獎勵籤", "CB-A": "競爭平衡 A 輪", "CB-B": "競爭平衡 B 輪", "SUP-2": "第 2 輪補償籤"}

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
team_terms: set[str] = set()  # 英文隊名（全名、暱稱），翻譯時保留不翻
_teams_loaded_at = 0.0

# 收錄的獎項：大聯盟層級（含美聯 103、國聯 104）和世界棒球經典賽 160
AWARD_LEAGUES = {103, 104, 160}
# 每週 / 每月獎項另外處理：同一人可能連續得獎，用公布日期區分，美聯國聯合併成一則
PERIODIC_AWARD = re.compile(r"\b(Week|Month)\b", re.I)
PERIODIC_ZH = {  # 獎項 ID 去掉 AL/NL 開頭 → 中文名稱；不在這裡的顯示英文名稱
    "POW": "本週最佳球員", "POM": "本月最佳球員", "PITOM": "本月最佳投手",
    "ROM": "本月最佳新秀", "RRELMON": "本月最佳救援投手", "MLBPLAYOW": "本週最佳美技",
}
AWARD_ZH = {
    "ALMVP": "美聯 MVP", "NLMVP": "國聯 MVP", "ALCY": "美聯賽揚獎", "NLCY": "國聯賽揚獎",
    "ALROY": "美聯新人王", "NLROY": "國聯新人王", "ALMOY": "美聯年度最佳總教練",
    "NLMOY": "國聯年度最佳總教練", "ALCPOY": "美聯年度東山再起獎", "NLCPOY": "國聯年度東山再起獎",
    "ALHAA": "美聯漢克阿倫獎", "NLHAA": "國聯漢克阿倫獎", "ALREL": "美聯年度最佳救援投手",
    "NLREL": "國聯年度最佳救援投手", "ALSS": "美聯銀棒獎", "NLSS": "國聯銀棒獎",
    "ALGG": "美聯金手套獎", "NLGG": "國聯金手套獎", "ALPG": "美聯白金手套獎", "NLPG": "國聯白金手套獎",
    "ALAS": "美聯明星隊名單", "NLAS": "國聯明星隊名單", "ASMVP": "明星賽 MVP",
    "HRDERBYWIN": "全壘打大賽冠軍", "HRDERBY": "全壘打大賽參賽者", "FUTMVP": "未來之星賽 MVP",
    "ALCSMVP": "美聯冠軍賽 MVP", "NLCSMVP": "國聯冠軍賽 MVP", "WSMVP": "世界大賽 MVP",
    "WSCHAMP": "世界大賽冠軍", "WSCHAMPMGR": "世界大賽冠軍總教練",
    "MLBAFIRST": "年度最佳陣容（第一隊）", "MLBSECOND": "年度最佳陣容（第二隊）",
    "MLBHOF": "名人堂", "MLBRC": "羅伯托克萊門特獎", "MLBCOMHA": "主席歷史成就獎",
    "DHOY": "年度最佳指定打擊", "MLBEXECOY": "年度最佳球團主管", "HUTCH": "哈欽森獎",
    "BABERUTH": "貝比魯斯獎", "LOUGEHRIG": "盧蓋瑞格獎", "MLBPCPOY": "球員票選年度最佳球員",
    "MLBPCMOY": "球員票選年度風雲人物", "WBCMVP": "世界棒球經典賽 MVP", "WBCTT": "經典賽明星隊",
}
# 小聯盟的獎項通常不發，這幾個例外（明星週的未來之星賽 MVP）
EXTRA_AWARDS = {"FUTMVP"}

TX_LABELS = {  # 異動類型 → (標題, 顏色)
    "TR": ("🔄 交易", 0x1E88E5), "SFA": ("✍️ 簽約", 0x43A047), "SGN": ("✍️ 簽約", 0x43A047),
    "CLW": ("📋 讓渡認領", 0x8E24AA), "REL": ("👋 釋出", 0x757575), "RET": ("🎖️ 退休", 0xF9A825),
    "DES": ("📤 指定讓渡", 0xFB8C00),
}

# 明星賽和季後賽（外卡、分區、聯盟冠軍、世界大賽）才發即時比分；其他比賽只在全部結束後發比分連結
LIVE_TYPES = {"A", "F", "D", "L", "W"}

# 得分過程翻成中文：打者做了什麼（MLB API 的 eventType → 中文），不認得的事件顯示英文原文
PLAY_ZH = {
    "single": "擊出一壘安打", "double": "擊出二壘安打", "triple": "擊出三壘安打",
    "sac_fly": "擊出高飛犧牲打", "sac_bunt": "犧牲觸擊", "sac_fly_double_play": "高飛犧牲打形成雙殺",
    "walk": "獲得四壞球保送", "intent_walk": "被故意四壞球保送", "hit_by_pitch": "遭觸身球",
    "field_error": "因守備失誤上壘", "fielders_choice": "野手選擇上壘", "fielders_choice_out": "野手選擇",
    "force_out": "擊出滾地球形成封殺", "grounded_into_double_play": "擊出滾地球雙殺",
    "double_play": "形成雙殺", "triple_play": "形成三殺", "strikeout": "被三振",
    "strikeout_double_play": "被三振形成雙殺", "catcher_interf": "因捕手妨礙打擊上壘",
    "wild_pitch": "暴投", "passed_ball": "捕逸", "balk": "投手犯規",
}
OUT_ZH = {  # field_out 依擊球方式細分
    "Groundout": "擊出滾地球出局", "Flyout": "擊出外野高飛球出局", "Lineout": "擊出平飛球出局",
    "Pop Out": "擊出內野高飛球出局", "Bunt Groundout": "觸擊出局", "Bunt Pop Out": "觸擊出局",
}
RUNNER_ZH = {  # 跑者不是靠打者這一棒回來時，說明原因
    "error": "守備失誤", "field_error": "守備失誤", "throwing_error": "傳球失誤", "fielding_error": "守備失誤",
}
MID_AT_BAT_ZH = {  # 打席進行中（打者還沒打完）就發生的得分，先寫這些，再寫打者最後的結果
    "wild_pitch": "投手暴投", "passed_ball": "捕手捕逸", "balk": "投手犯規",
    "pickoff_error": "牽制失誤", "stolen_base_home": "盜回本壘",
}

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
    for key in ("tx", "news", "honors"):
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
        team_terms.update(n for t in data.get("teams", [])
                          for n in (t.get("name"), t.get("teamName"), t.get("clubName")) if n)
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
            if a.get("active")
            and (a.get("sport", {}).get("id") == 1 or a.get("league", {}).get("id") in AWARD_LEAGUES
                 or a.get("id") in EXTRA_AWARDS)
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
    articles = data.get("articles") or []
    if not articles:  # ESPN 一定有新聞；抓到空的代表格式改了，讓健康檢查知道
        raise RuntimeError("ESPN 新聞回傳是空的")
    return articles  # 新到舊


# ===== 翻譯 =====
_zh_cache: dict[str, str] = {}
_translate_limit = asyncio.Semaphore(5)  # 同時最多 5 個翻譯請求，避免被 Google 擋
_translate_stats = {"ok": 0, "fail": 0}  # 這次檢查的翻譯成功/失敗次數（健康檢查用）
FIXED_ZH = {  # 短詞直接對照，不送去翻譯（比賽狀態、延賽原因）
    "postponed": "延期", "delayed": "延遲", "delayed start": "延後開賽", "suspended": "暫停",
    "cancelled": "取消", "canceled": "取消", "final": "終場", "game over": "比賽結束",
    "completed early": "提前結束", "tied": "平手", "in progress": "比賽進行中", "scheduled": "尚未開賽",
    "pre-game": "賽前", "warmup": "熱身", "manager challenge": "總教練挑戰", "umpire review": "裁判重播審查",
    "rain": "下雨", "weather": "天候因素", "inclement weather": "天候不佳", "cold": "低溫",
    "wet grounds": "場地濕滑", "snow": "下雪", "lightning": "閃電", "fog": "起霧", "wind": "強風",
    "power": "停電", "darkness": "天色太暗", "venue": "場地因素", "other": "其他原因",
}
PRE_ZH = [  # 送去翻譯前先換掉的固定說法
    (re.compile(r"\bGame Highlights\b"), "比賽精華"),
    (re.compile(r"\bGame (\d+)\b"), r"第 \1 戰"),
]
# 運動術語：Google 常翻錯，翻完後修正。（原文有這個英文，譯文裡的錯誤翻法 → 正確說法）
# 只在原文真的有那個英文時才改，不會誤改別的句子；看到新的怪翻譯就加一行
TERM_FIX = [
    (r"^Sources?:", r"^(?:消息)?來源：|^資料來源：", "消息人士："),
    (r"four-seam|four-seamer", r"四縫(?:線)?快球|四接縫快球|四縫線球", "四縫線速球"),
    (r"two-seam|two-seamer", r"二縫(?:線)?快球|兩縫(?:線)?快球", "二縫線速球"),
    (r"fastball|heater", r"快球|快速球|加熱器", "速球"),
    (r"\bsinker", r"下沉球|沉降球|下沉器|沉子", "伸卡球"),
    (r"\bcutter", r"切球|切割器|切割球|刀具", "卡特球"),
    (r"\bslider", r"滑塊|滑桿|滑動球", "滑球"),
    (r"\bsweeper", r"清掃球|掃地機|掃帚|清潔工", "橫掃球"),
    (r"curveball|\bcurve\b", r"弧線球|彎球|曲線球", "曲球"),
    (r"changeup|change-up", r"變化球|換球", "變速球"),
    (r"splitter|split-finger", r"分指球|分裂球|分離器|指球|分裂(?!球)", "指叉球"),
    (r"knuckleball", r"關節球|指節球", "蝴蝶球"),
    (r"walk[- ]?off", r"走下來|走開|步行結束|走出", "再見"),
    (r"\bcloser\b", r"更接近者|更近者|關閉者|近距離", "終結者"),
    (r"blown save|blows? (?:a |the )?save", r"撲救|救球", "救援失敗"),
    (r"\breliever", r"中繼員|救援者|救濟者|緩解者", "後援投手"),
    (r"slugger", r"重擊手|強擊手|猛擊者", "強打者"),
    (r"bullpen", r"牛欄|牛圈", "牛棚"),
    (r"strikeout|struck out", r"罷工", "三振"),
    (r"\bshutout", r"完成封|停賽|關門", "完封"),
    (r"no-hitter|no-hit", r"無安打者|無打擊", "無安打比賽"),
    (r"perfect game", r"完美比賽", "完全比賽"),
    (r"designat\w*\b.{0,60}?for assignment|\bDFA",
     r"指定(.{0,10}?)(?:進行|執行)?(?:任務|分配|指派)", r"將\1指定讓渡"),
    (r"injured list", r"受傷名單|傷病名單|傷者名單", "傷兵名單"),
    (r"grand slam", r"大滿貫", "滿貫全壘打"),
    (r"\bhomer|home run", r"本壘打|全壘打打", "全壘打"),
    (r"\bRBI", r"打點數", "打點"),
    (r"(?-i:\bERA\b)", r"時代", "防禦率"),  # 只認大寫 ERA，避免 new era 這種一般用法
    (r"pennant", r"錦旗|三角旗", "聯盟冠軍"),
    (r"\bALCS\b|American League Championship Series", r"美國聯盟冠軍系列賽|美聯冠軍系列賽|\bALCS\b", "美聯冠軍賽"),
    (r"\bNLCS\b|National League Championship Series", r"國家聯盟冠軍系列賽|國聯冠軍系列賽|\bNLCS\b", "國聯冠軍賽"),
    (r"\bALDS\b|American League Division Series", r"美國聯盟分區系列賽|\bALDS\b", "美聯分區系列賽"),
    (r"\bNLDS\b|National League Division Series", r"國家聯盟分區系列賽|\bNLDS\b", "國聯分區系列賽"),
    (r"\bguards?\b", r"守衛|警衛", "後衛"),
    (r"American League", r"美國聯盟", "美聯"),
    (r"National League", r"國家聯盟", "國聯"),
    (r"Cy Young", r"青年賽|賽揚青年", "賽揚獎"),
    (r"Gold Glove", r"金手套獎", "金手套"),
    (r"Silver Slugger", r"銀色重擊手|銀色強打者|銀色擊球手", "銀棒獎"),
    (r"qualifying offer", r"資格報價|合格要約|資格要約", "合格報價"),
    (r"arbitration", r"(?<!薪資)仲裁", "薪資仲裁"),
    (r"non-tender", r"非投標|不投標", "不續約"),
    (r"(?:club|team) option", r"俱樂部選擇權|俱樂部選項|球隊選擇權|團隊選項", "球隊選項"),
    (r"player option", r"玩家選擇權|球員選擇權|玩家選項", "球員選項"),
    (r"mutual option", r"共同選擇權|相互選擇權|雙方選擇權", "雙方選項"),
    (r"opt[- ]out", r"選擇退出條款|選擇退出|退出條款", "跳出條款"),
    (r"no-trade", r"無交易條款|禁止交易條款|不交易條款", "不可交易條款"),
    (r"free agen", r"免費代理人?|自由代理人?|自由經紀人", "自由球員"),
    (r"\bextension\b", r"延期", "延長合約"),
    (r"pinch[- ]hit", r"捏擊|捏打", "代打"),
    (r"double play", r"雙打", "雙殺"),
    (r"World Series", r"世界職業棒球大賽|世界職棒大賽", "世界大賽"),
    (r"regular season", r"常規賽", "例行賽"),
    (r"\bmanager", r"(?<!總)經理", "總教練"),
    (r"\bout as\b", r"出任", "卸任"),
    (r"\beven(?:s|ed)? (?:the )?(?:series|ALCS|NLCS|ALDS|NLDS|World Series)", r"甚至是?|甚至", "扳平"),
    (r"clubhouse", r"會所|俱樂部會所", "休息室"),
    (r"posting system|\bposted\b", r"發布系統|郵寄系統|張貼系統|過帳系統|發佈系統", "入札制度"),
    (r"broadcaster", r"廣播公司|廣播員", "播報員"),
    (r"rainout|rained out", r"雨停|雨水", "因雨延賽"),
    (r"\bowner", r"所有者|擁有者", "老闆"),
    (r"front office", r"前台|前辦公室", "球團"),
    (r"\bclub\b", r"俱樂部", "球隊"),
    (r"\bteam", r"團隊", "球隊"),
]
TERM_FIX = [(re.compile(s, re.I), re.compile(w), r) for s, w, r in TERM_FIX]
CJK_LATIN = (re.compile(r"([一-鿿])([A-Za-z0-9])"), re.compile(r"([A-Za-z0-9.])([一-鿿])"))
POS_ZH = {"LHP": "左投", "RHP": "右投", "P": "投手", "C": "捕手", "1B": "一壘手", "2B": "二壘手",
          "3B": "三壘手", "SS": "游擊手", "LF": "左外野手", "CF": "中外野手", "RF": "右外野手",
          "OF": "外野手", "IF": "內野手", "INF": "內野手", "DH": "指定打擊", "UT": "工具人",
          "SP": "先發投手", "RP": "後援投手", "TWP": "二刀流"}
POS_BEFORE_NAME = re.compile(r"\b(LHP|RHP|INF|1B|2B|3B|SS|LF|CF|RF|OF|IF|DH|UT|P|C)\s+(?=[A-Z])")


def person_terms(*names: str) -> set[str]:
    """人名和姓氏（文章常只寫姓），翻譯時保留不翻"""
    terms = set()
    for name in names:
        name = (name or "").strip(" .,")
        if name:
            terms.add(name)
            last = re.sub(r"\s+(Jr\.?|Sr\.?|II|III|IV)$", "", name).split()[-1]
            if len(last) >= 3:
                terms.add(last)
            first = name.split()[0]
            if re.search(r"[a-z][A-Z]", first):  # DeAndre、JaVale 這種名字也常單獨出現
                terms.add(first)
    return terms


TRANSLATE_CLIENTS = ["dict-chrome-ex", "gtx"]  # Google 免費翻譯的幾種用法，被擋（429）就換下一個
_translate_client = [0]  # 目前用哪一個


async def google_translate(session: aiohttp.ClientSession, text: str) -> list:
    for _ in TRANSLATE_CLIENTS:
        n = _translate_client[0]
        try:
            async with _translate_limit, session.get(
                    TRANSLATE_URL, params={"client": TRANSLATE_CLIENTS[n], "sl": "en", "tl": "zh-TW", "dt": "t",
                                           "q": text},
                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except aiohttp.ClientResponseError as e:
            if e.status not in (403, 429, 500, 502, 503):  # 被擋或伺服器暫時故障：換下一個
                raise
            if _translate_client[0] == n:  # 這個被擋了，之後都改用下一個
                _translate_client[0] = (n + 1) % len(TRANSLATE_CLIENTS)
    raise RuntimeError("Google 翻譯的每種用法都被擋")


async def translate(session: aiohttp.ClientSession, text: str, keep=()) -> str:
    """英文翻成繁體中文；keep 和所有隊名先換成代號保護起來，翻完再換回英文。失敗時回傳原文"""
    if not text or not re.search(r"[A-Za-z]{2}", text):
        return text
    if text in _zh_cache:
        return _zh_cache[text]
    fixed = [FIXED_ZH.get(p.strip().lower()) for p in text.split(":")]  # 例如 Delayed: Rain
    if all(fixed):
        _zh_cache[text] = "：".join(fixed)
        return _zh_cache[text]
    terms = sorted({t for t in (*keep, *team_terms) if t and t in text}, key=len, reverse=True)
    protected = text
    for pattern, repl in PRE_ZH:
        protected = pattern.sub(repl, protected)
    if terms:
        pattern = re.compile("|".join(rf"(?<!\w){re.escape(t)}(?!\w)" for t in terms))
        protected = pattern.sub(lambda m: f"__N{terms.index(m.group(0))}__", protected)
    try:
        data = await google_translate(session, protected)
        out = "".join(seg[0] for seg in data[0] if seg and seg[0])
        out = re.sub(r"_+\s*[Nn]\s*(\d+)\s*_+",
                     lambda m: terms[int(m.group(1))] if int(m.group(1)) < len(terms) else m.group(0), out)
        if not out.strip() or "__N" in out:
            raise ValueError(f"翻譯結果異常：{out}")
        for source, wrong, right in TERM_FIX:  # 修正 Google 翻錯的運動術語
            if source.search(text):
                out = wrong.sub(right, out)
        out = re.sub(r"\b(Jr|Sr|St)。", r"\1.", out)  # Jr. 的句點不是句號
        # 保留英文的名字後面的所有格：Uecker's 家人 → Uecker 的家人、Dodgers' → Dodgers 的
        out = re.sub(r"([A-Za-z])(?:['’]s|(s)['’])(?=[\s「“\"一-鿿]|$)",
                     lambda m: m.group(1) + (m.group(2) or "") + " 的", out)
        out = re.sub(r"(?<=[一-鿿]) +(?=[一-鿿「“])", "", out)  # 換掉術語後，中文之間不留空格
        for pattern in CJK_LATIN:  # 中文和英文、數字之間加空格比較好讀
            out = pattern.sub(r"\1 \2", out)
    except Exception as e:
        print(f"⚠️ 翻譯失敗，顯示英文原文：{e}")
        _translate_stats["fail"] += 1
        return text
    _translate_stats["ok"] += 1
    _zh_cache[text] = out
    return out


def zh(text: str) -> str:
    """已經翻譯過的文字回傳中文，沒翻過的回傳原文（翻譯要先用 prefetch 做好）"""
    return _zh_cache.get(text, text)


async def prefetch(session: aiohttp.ClientSession, texts, keep=()) -> None:
    await asyncio.gather(*(translate(session, t, keep) for t in set(texts) if t))


_T, _W = r"(?P<team>.+?)", r"(?P<who>.+?)"
TX_RULES = [  # MLB 異動的固定句型 → 中文（Google 常翻錯這些術語）；rest 是句尾補充說明，交給 Google
    (rf"{_T} designated {_W} for assignment", "{team} 將 {who} 指定讓渡（DFA）"),
    (rf"{_T} optioned {_W} to (?P<to>.+?)", "{team} 將 {who} 下放至 {to}"),
    (rf"{_T} released {_W}", "{team} 釋出 {who}"),
    (rf"{_T} claimed {_W} off waivers from (?P<frm>.+?)", "{team} 從 {frm} 的讓渡名單認領 {who}"),
    (rf"{_T} recalled {_W} from (?P<frm>.+?)", "{team} 從 {frm} 召回 {who}"),
    (rf"{_T} placed {_W} on the (?P<n>\d+)-day injured list", "{team} 將 {who} 放進 {n} 天傷兵名單"),
    (rf"{_T} transferred {_W} from the (?P<a>\d+)-day injured list to the (?P<n>\d+)-day injured list",
     "{team} 將 {who} 從 {a} 天轉到 {n} 天傷兵名單"),
    (rf"{_T} sent {_W} on a rehab assignment to (?P<to>.+?)", "{team} 派 {who} 到 {to} 復健出賽"),
    (rf"{_W} roster status changed by {_T}", "{team} 更動了 {who} 的名單狀態"),
    (rf"{_T} activated {_W} from the (?P<n>\d+)-day injured list", "{team} 讓 {who} 從 {n} 天傷兵名單回歸"),
    (rf"{_T} reinstated {_W} from the (?P<n>\d+)-day injured list", "{team} 讓 {who} 從 {n} 天傷兵名單回歸"),
    (rf"{_T} activated {_W}", "{team} 將 {who} 重新啟用"),
    (rf"{_T} placed {_W} on the bereavement list", "{team} 將 {who} 放進喪假名單"),
    (rf"{_T} placed {_W} on the paternity list", "{team} 將 {who} 放進陪產假名單"),
    (rf"{_T} placed {_W} on the restricted list", "{team} 將 {who} 放進限制名單"),
    (rf"{_T} signed free agent {_W} to a minor league contract", "{team} 與自由球員 {who} 簽下小聯盟合約"),
    (rf"{_T} signed free agent {_W}", "{team} 簽下自由球員 {who}"),
    (rf"{_T} signed {_W}", "{team} 簽下 {who}"),
    (rf"{_T} selected the contract of {_W} from (?P<frm>.+?)", "{team} 將 {who} 從 {frm} 升上大聯盟"),
    (rf"{_T} traded {_W} to (?P<to>.+?) for cash", "{team} 將 {who} 交易至 {to}，換取現金"),
    (rf"{_W} retired", "{who} 宣布退休"),
]
# 句尾補充說明（傷勢、追溯日期）在句號後面，或以 retroactive 開頭
#（St. Paul、Jr. 這種縮寫的句點不算）
TX_RULES = [(re.compile(rf"^{p}(?:(?:(?<!St)(?<!Ft)(?<!Mt)(?<!Jr)(?<!Sr)\.|(?=\s+retroactive\b))\s+"
                        rf"(?P<rest>.+?))?\.?$"), t) for p, t in TX_RULES]


async def translate_tx(session: aiohttp.ClientSession, tx: dict) -> None:
    """異動敘述翻成中文：位置先自己翻，常見句型直接對照，其他交給 Google；球員、球隊名稱保留英文"""
    text = tx.get("description", "")
    if not text or text in _zh_cache:
        return
    keep = person_terms((tx.get("person") or {}).get("fullName", ""))
    keep |= {n for n in ((tx.get("toTeam") or {}).get("name"), (tx.get("fromTeam") or {}).get("name")) if n}
    body = POS_BEFORE_NAME.sub(lambda m: f"{POS_ZH[m.group(1)]} ", text)
    for pattern, template in TX_RULES:
        m = pattern.match(body)
        if m:
            groups = {k: v for k, v in m.groupdict().items() if v is not None}
            out = template.format(**groups)
            if groups.get("rest"):  # 例如 Retroactive to October 1. Right elbow inflammation.
                out += f"（{await translate(session, groups['rest'], keep)}）"
            _zh_cache[text] = out
            return
    _zh_cache[text] = await translate(session, body, keep)


def game_texts(game: dict) -> list[str]:
    """這場比賽的訊息裡會出現的英文（系列賽說明、比賽狀態、延賽原因）"""
    status = game.get("status", {})
    return [series_text(game), status.get("detailedState", ""), status.get("reason", "")]


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
    # 手機一行大約只放得下 33 個字，所以每格只留需要的寬度（9 局約 30 個字），太寬會自動換行跑版；
    # 延長賽分段：前 9 局一段，之後局數是兩位數比較寬，每 6 局一段；R H E 放在最後一段
    sides = ("away", "home")
    names = {s: team_abbr(game["teams"][s]["team"]) for s in sides}
    name_w = max(len(n) for n in names.values())
    rhe = {s: [str(totals.get(s, {}).get(k, 0)) for k in ("runs", "hits", "errors")] for s in sides}
    rhe_w = [max(len(rhe[s][k]) for s in sides) for k in range(3)]
    blocks = []
    starts = [0, *range(9, len(innings), 6)]
    for n, start in enumerate(starts):
        last = n == len(starts) - 1
        chunk = innings[start:starts[n + 1] if not last else len(innings)]
        nums = [str(i.get("num", "")) for i in chunk]
        runs = {s: [str(i.get(s, {}).get("runs", "X")) for i in chunk] for s in sides}
        widths = [max(len(nums[k]), *(len(runs[s][k]) for s in sides)) for k in range(len(chunk))]

        def line(label: str, cells: list[str], total: list[str]) -> str:
            text = f"{label:<{name_w}} " + " ".join(f"{c:>{w}}" for c, w in zip(cells, widths))
            return text + (" | " + " ".join(f"{t:>{w}}" for t, w in zip(total, rhe_w)) if last else "")

        blocks.append("\n".join([line("", nums, ["R", "H", "E"])]
                                + [line(names[s], runs[s], rhe[s]) for s in sides]))
    return "```\n" + "\n\n".join(blocks) + "\n```"


def game_embed(game: dict, title: str, lines: list[str], color: int) -> discord.Embed:
    embed = discord.Embed(title=title[:256], url=game_url(game),
                          description="\n".join(l for l in lines if l)[:4000], color=color)
    series = zh(series_text(game))
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


def play_text(play: dict) -> str:
    """把一筆得分紀錄寫成中文，例如「Austin Riley 擊出 3 分全壘打，Michael Harris II、Ozzie Albies 跟著跑回」"""
    res = play.get("result") or {}
    kind = res.get("eventType", "")
    batter = ((play.get("matchup") or {}).get("batter") or {}).get("fullName", "")
    action = OUT_ZH.get(res.get("event", "")) if kind == "field_out" else PLAY_ZH.get(kind)
    if kind == "home_run":
        rbi = res.get("rbi") or 1
        action = "擊出陽春全壘打" if rbi == 1 else "擊出滿貫全壘打" if rbi == 4 else f"擊出 {rbi} 分全壘打"
    if not action or not batter:
        return res.get("description", "")  # 沒看過的事件：顯示英文原文
    # 依「怎麼回來的」把跑回本壘的人分組；打席中的暴投、捕逸等和打者這一棒分開
    mid: dict[str, list[str]] = {}
    groups: dict[str, list[str]] = {}
    for r in play.get("runners") or []:
        if (r.get("movement") or {}).get("end") != "score":
            continue
        name = ((r.get("details") or {}).get("runner") or {}).get("fullName", "")
        if not name or name == batter:
            continue
        how = (r.get("details") or {}).get("eventType", "")
        if how != kind and how in MID_AT_BAT_ZH:
            mid.setdefault(how, []).append(name)
        else:
            groups.setdefault(RUNNER_ZH.get(how, "") if how != kind else "", []).append(name)
    parts = []
    for how, names in mid.items():
        who = "、".join(names)
        parts.append(f"{who} 盜回本壘" if how == "stolen_base_home" else f"{MID_AT_BAT_ZH[how]}，{who} 跑回得分")
    hit = [f"{batter} {action}"]
    for reason, names in groups.items():
        who = "、".join(names)
        if kind == "home_run" and not reason:
            hit.append(f"{who} 跟著跑回")
        else:
            hit.append(f"{who} 趁{reason}跑回得分" if reason else f"{who} 跑回得分")
    if parts and not groups:  # 分數都是打席中進來的，打者的結果放後面當補充
        return "；".join(parts) + f"（{batter} 這個打席最後{action}）"
    return "；".join(parts + ["，".join(hit)])


def completed_innings(game: dict) -> int:
    """已經打完幾局（上下半局都結束才算一局）"""
    ls = game.get("linescore") or {}
    if game.get("status", {}).get("abstractGameState") == "Final":
        return len(ls.get("innings") or [])
    n = ls.get("currentInning") or 0
    return n if ls.get("inningState") == "End" else max(n - 1, 0)


def inning_message(game: dict, n: int, plays: list[dict]) -> tuple[str, discord.Embed]:
    """第 n 局的摘要：上下半局各得幾分、誰打回分數，標題是這局結束時的比分"""
    innings = (game.get("linescore") or {}).get("innings") or []
    totals = {side: sum(i.get(side, {}).get("runs") or 0 for i in innings[:n]) for side in ("away", "home")}
    inning = innings[n - 1] if n <= len(innings) else {}
    lines = []
    for half, side, mark in (("top", "away", "▲"), ("bottom", "home", "▼")):
        runs = inning.get(side, {}).get("runs")
        result = "不需進攻" if runs is None else f"得 {runs} 分" if runs else "無得分"
        lines.append(f"{mark} **{n} 局{'上' if half == 'top' else '下'}｜"
                     f"{team_label(game['teams'][side]['team'])} {result}**")
        for p in plays:
            about, res = p.get("about") or {}, p.get("result") or {}
            if about.get("inning") == n and about.get("halfInning") == half:
                lines.append(f"　• {play_text(p)}"
                             f"（{res.get('awayScore', 0)} : {res.get('homeScore', 0)}）")
    lines.append(linescore_table(game))
    away, home = game["teams"]["away"]["team"], game["teams"]["home"]["team"]
    title = f"{team_label(away)} {totals['away']} : {totals['home']} {team_label(home)}"
    label = f"第 {n} 局結束" + ("（延長賽）" if n > 9 else "")
    return f"⚾ **{label}**", game_embed(game, title, lines, 0xD50032)


def scoring_alert(game: dict, plays: list[dict]) -> tuple[str, discord.Embed]:
    """還沒打完的這局有人得分時先發快報，整局結束後再發完整摘要"""
    lines = []
    for p in plays:
        about, res = p.get("about") or {}, p.get("result") or {}
        top = about.get("halfInning") == "top"
        team = game["teams"]["away" if top else "home"]["team"]
        lines.append(f"{'▲' if top else '▼'} **{about.get('inning', '?')} 局{'上' if top else '下'}｜"
                     f"{team_label(team)}**（{res.get('awayScore', 0)} : {res.get('homeScore', 0)}）\n"
                     f"　{play_text(p)}")
    return "🔥 **得分！**", game_embed(game, score_title(game), lines, 0xFF6F00)


async def try_scoring_plays(session: aiohttp.ClientSession, game: dict) -> list[dict] | None:
    try:
        return await fetch_scoring_plays(session, game["gamePk"])
    except Exception as e:
        print(f"⚠️ 抓取得分過程失敗：{e}")
        return None


def inning_messages(game: dict, done: int, now: int, plays: list[dict] | None) -> list:
    """第 done+1 到第 now 局的摘要（GitHub 排程延遲時可能一次補好幾局）"""
    return [inning_message(game, n, plays or []) for n in range(done + 1, now + 1)]


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
    lines = [f"狀態：{zh(status.get('detailedState', ''))}", inning_text(game)]
    if status.get("reason"):
        lines.append(f"原因：{zh(status['reason'])}")
    return "⏸️ **比賽狀態更新**", game_embed(game, score_title(game), lines, 0x757575)


def tx_message(tx: dict, contracts: dict | None = None) -> tuple[str, discord.Embed]:
    label, color = TX_LABELS.get(tx.get("typeCode"), (zh(tx.get("typeDesc", "異動")), 0x757575))
    description = zh(tx.get("description", ""))
    money = contract_lines(tx, contracts or {})
    if money:
        description += "\n\n" + "\n".join(money)
    embed = discord.Embed(title=label, description=description[:4000], color=color)
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
    return f"{season} {AWARD_ZH.get(award_id) or zh(award.get('name', award_id))}"


def recipient_text(r: dict) -> str:
    player = r.get("player") or {}
    pos = (player.get("primaryPosition") or {}).get("abbreviation", "")
    pos = POS_ZH.get(pos, pos)
    name = player.get("nameFirstLast") or ""
    team = team_name((r.get("team") or {}).get("id"))
    text = " ".join(p for p in (pos, name) if p) or team or "?"
    if name and team:
        text += f"（{team}）"
    if r.get("notes"):
        text += f"｜{zh(r['notes'])}"
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
    embed.set_footer(text="MLB 獎項")
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


def is_record(article: dict) -> bool:
    return bool(RECORD_NEWS.search(f"{article.get('headline') or ''} {article.get('description') or ''}"))


def wanted_news(article: dict) -> bool:
    return article.get("type") in NEWS_TYPES or is_record(article)


def is_big(article: dict) -> bool:
    return bool(BIG_NEWS.search(article.get("headline") or "")
                or BIG_NEWS_SOURCES.search(article.get("description") or ""))


def news_label(article: dict) -> str:
    if is_record(article):
        return "📊 **MLB 紀錄**"
    return "🚨 **MLB 重大消息**" if is_big(article) else "📰 **MLB 新聞**"


def is_periodic(award: dict) -> bool:
    return bool(PERIODIC_AWARD.search(award.get("name", "")))


def periodic_kind(award_id: str) -> str:
    """ALPOW / NLPOW → POW，美聯國聯同一種獎項合併成一則"""
    return award_id[2:] if award_id[:2] in ("AL", "NL") else award_id


def periodic_text(award: dict, date: str) -> str:
    """週獎的日期是該週最後一天，月獎的日期是該月最後一天"""
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return date
    if re.search(r"\bWeek\b", award.get("name", ""), re.I):
        s = d - timedelta(days=6)
        return f"{s.month}/{s.day}–{d.month}/{d.day}"
    return f"{d.month}月"


def honor_message(items: list[tuple[dict, dict]]) -> tuple[str, discord.Embed]:
    """items：同一種獎項、同一期的 [(獎項, 得獎紀錄)]（通常是美聯、國聯各一位）"""
    award, first = items[0]
    name = PERIODIC_ZH.get(periodic_kind(award.get("id", ""))) or zh(award.get("name", ""))
    league = {"AL": "美聯", "NL": "國聯"}
    lines = []
    for a, r in sorted(items, key=lambda x: x[0].get("id", "")):
        prefix = league.get(a.get("id", "")[:2])
        lines.append(f"**{prefix}**：{recipient_text(r)}" if prefix else f"• {recipient_text(r)}")
    embed = discord.Embed(title=f"{first.get('season', '')} {name}｜{periodic_text(award, first.get('date', ''))}"[:256],
                          description="\n".join(lines)[:4000], color=0xFFD700)
    player_id = (first.get("player") or {}).get("id")
    if len(items) == 1 and player_id:
        embed.set_thumbnail(url=headshot_url(player_id))
    embed.set_footer(text="MLB 獎項")
    embed.timestamp = datetime.now(timezone.utc)
    return f"🏅 **MLB {name}**", embed


def article_id(article: dict) -> str:
    return str(article.get("id") or article.get("links", {}).get("web", {}).get("href", ""))


def wanted_tx(tx: dict) -> bool:
    if tx.get("typeCode") not in TX_TYPES or not follows(*tx_team_ids(tx)):
        return False
    return not (SKIP_MINOR_DEALS and "minor league" in tx.get("description", "").lower())


# ===== 合約金額：官方異動沒有金額，從新聞標題記下「球員 → 金額」，簽約時附上；沒有金額就寫合約種類 =====
CONTRACT_KEEP_DAYS = 150  # 新聞報的金額保留幾天（談妥到官方公布可能隔很久）
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10}
CONTRACT_MONEY = re.compile(
    r"(?:\b(?P<years>\d+|one|two|three|four|five|six|seven|eight|nine|ten)[- ]years?,?\s+(?:deal\s+worth\s+)?)?"
    r"\$(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>million|billion|M|B)\b", re.I)
CONTRACT_YEARS = re.compile(r"\b(?P<n>\d+|one|two|three|four|five|six|seven|eight|nine|ten)[- ]year\b", re.I)
CONTRACT_WORDS = re.compile(r"\b(agree|agrees|agreed|sign|signs|signed|re-sign|extension|deal|contract)\b", re.I)
CONTRACT_TYPES = [  # 異動敘述裡的合約種類 → 中文（由上往下找第一個符合的）
    (r"minor league contract", "小聯盟合約"), (r"major league contract", "大聯盟合約"),
    (r"\b(?P<n>\d+)-year (?:contract )?extension", "{n} 年延長合約"), (r"\bextension\b", "延長合約"),
    (r"\b(?P<n>\d+)-year", "{n} 年合約"), (r"\bone-year", "1 年合約"),
]
CONTRACT_TYPES = [(re.compile(p, re.I), t) for p, t in CONTRACT_TYPES]
SIGN_CODES = ("SFA", "SGN")


def norm_name(name: str) -> str:
    """比對球員名字用：去掉重音符號、大小寫、標點（Velázquez = Velazquez）"""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", "", text.lower()).strip()


def money_zh(amount: float) -> str:
    if amount >= 1e8:
        return f"{amount / 1e8:.2f}".rstrip("0").rstrip(".") + " 億美元"
    return f"{amount / 1e4:,.0f} 萬美元"


# 新聞裡寫到的簽約方式、附帶條款（同一組只取第一個符合的，例如寫了非納稅中產就不再寫中產）
CONTRACT_DETAILS = [  # (分類, 組, 英文, 中文)；分類 how = 簽約方式、terms = 附帶條款
    ("how", "mle", r"non-taxpayer mid-level", "非納稅中產特例"),
    ("how", "mle", r"taxpayer mid-level", "納稅中產特例"),
    ("how", "mle", r"room (?:mid-level )?exception", "空間中產特例"),
    ("how", "mle", r"mid-level exception|\bMLE\b", "中產特例"),
    ("how", "bae", r"bi-annual exception", "雙年特例"),
    ("how", "min", r"veteran(?:'s)? minimum", "老將底薪"),
    ("how", "min", r"\b(?:league )?minimum (?:contract|deal|salary)", "底薪"),
    ("how", "max", r"supermax|designated veteran", "超級頂薪"),
    ("how", "max", r"rookie max|Rose Rule", "新秀頂薪"),
    ("how", "max", r"\bmax(?:imum)? (?:contract|extension|deal)|\bfull max\b", "頂薪"),
    ("how", "qo", r"qualifying offer", "合格報價"),
    ("how", "sheet", r"offer sheet", "報價單"),
    ("how", "snt", r"sign-and-trade", "先簽後換"),
    ("how", "hard", r"hardship exception", "傷病特例"),
    ("how", "arb", r"avoid(?:ed|ing)? arbitration|arbitration", "薪資仲裁相關"),
    ("terms", "po", r"player option", "球員選項"),
    ("terms", "to", r"(?:team|club) option", "球隊選項"),
    ("terms", "mo", r"mutual option", "雙方選項"),
    ("terms", "vo", r"vesting option", "達標生效選項"),
    ("terms", "oo", r"opt[- ]outs?\b", "跳出條款"),
    ("terms", "ntc", r"full no-trade", "完整不可交易條款"),
    ("terms", "ntc", r"(?:partial|limited) no-trade|no-trade protection", "部分不可交易條款"),
    ("terms", "ntc", r"no-trade", "不可交易條款"),
    ("terms", "tk", r"trade kicker", "交易獎金"),
    ("terms", "sb", r"signing bonus", "簽約獎金"),
    ("terms", "def", r"deferr", "延遲支付"),
    ("terms", "gtd", r"fully guaranteed", "全額保障"),
    ("terms", "gtd", r"partially guaranteed", "部分保障"),
    ("terms", "gtd", r"non-guaranteed", "非保障"),
    ("terms", "inc", r"incentive", "附激勵獎金"),
]
CONTRACT_DETAILS = [(kind, group, re.compile(p, re.I), zh_text) for kind, group, p, zh_text in CONTRACT_DETAILS]


def contract_details(text: str) -> dict:
    found, used = {"how": [], "terms": []}, set()
    for kind, group, pattern, zh_text in CONTRACT_DETAILS:
        if group not in used and pattern.search(text):
            used.add(group)
            found[kind].append(zh_text)
    return found


def remember_contracts(state: dict, articles: list[dict]) -> None:
    """新聞寫到合約的，記下 {球員: 金額、簽約方式、附帶條款}；只記新聞明確寫到的，不推測"""
    book = state.setdefault("contracts", {})
    now = time.time()
    for a in articles:
        headline, desc = a.get("headline") or "", a.get("description") or ""
        if not CONTRACT_WORDS.search(headline):
            continue
        text = headline
        m = CONTRACT_MONEY.search(headline)
        if not m:
            text, m = desc, CONTRACT_MONEY.search(desc)
        details = contract_details(headline + " " + desc)
        if not m and not details["how"] and not details["terms"]:
            continue
        players = [c.get("description", "") for c in a.get("categories") or []
                   if c.get("type") == "athlete" and c.get("description")]
        named = [p for p in players if re.sub(r"\s+(Jr\.?|Sr\.?|II|III|IV)$", "", p).split()[-1] in headline]
        named = named or (players if len(players) == 1 else [])
        money = None
        if m:
            # 年數可能寫在金額後面（$1 million, 1-year deal）
            y = m["years"] or (CONTRACT_YEARS.search(text) or {"n": None})["n"]
            years = y and (NUM_WORDS.get(y.lower()) or int(y))
            amount = float(m["amount"]) * (1e9 if m["unit"].lower().startswith("b") else 1e6)
            money = f"{years} 年 {money_zh(amount)}" if years else money_zh(amount)
        for p in named:
            entry = book.setdefault(norm_name(p), {})
            if money:
                entry["text"] = money
            for kind in ("how", "terms"):  # 同一個球員好幾則新聞：條款合併
                entry[kind] = list(dict.fromkeys(entry.get(kind, []) + details[kind]))
            entry["t"] = now
    for k in [k for k, v in book.items() if now - v.get("t", 0) > CONTRACT_KEEP_DAYS * 86400]:
        del book[k]


def contract_extra(entry: dict) -> list[str]:
    """簽約方式、附帶條款各一行（新聞有寫才有）"""
    lines = []
    if entry.get("how"):
        lines.append(f"　🏷️ 簽約方式：{'、'.join(entry['how'])}")
    if entry.get("terms"):
        lines.append(f"　📑 附帶：{'、'.join(entry['terms'])}")
    return lines


def contract_type(sentence: str, default: str) -> str:
    for pattern, zh_type in CONTRACT_TYPES:
        m = pattern.search(sentence)
        if m:
            return zh_type.format(**m.groupdict())
    return default


def contract_lines(tx: dict, book: dict) -> list[str]:
    """簽約的球員一行：有報導金額就附金額，沒有就寫合約種類（官方沒寫小聯盟合約的就是大聯盟合約）"""
    name = (tx.get("person") or {}).get("fullName")
    if tx.get("typeCode") not in SIGN_CODES or not name:
        return []
    kind = contract_type(tx.get("description", ""), "大聯盟合約")
    entry = book.get(norm_name(name)) or {}
    money = entry.get("text")
    return ([f"💰 {name}：{kind}｜{money}（依 ESPN 報導）" if money else f"📄 {name}：{kind}（金額未公開）"]
            + contract_extra(entry))


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


def is_title_clincher(game: dict) -> bool:
    """世界大賽打完的那一場"""
    return (game.get("gameType") == "W" and game.get("status", {}).get("abstractGameState") == "Final"
            and bool((game.get("seriesStatus") or {}).get("isOver")))


POSTSEASON_TYPES = {"F", "D", "L", "W"}  # 外卡賽、分區系列賽、聯盟冠軍賽、世界大賽
SEASON_STATE_VERSION = 3  #球季狀態加了新項目時調高，舊狀態會重新初始化一次（不發文）


def season_start_message(content: str, title: str, intro: str, games: list[dict],
                         color: int) -> tuple[str, discord.Embed]:
    lines = [f"{intro}今天的比賽："]
    lines += [f"• {team_label(g['teams']['away']['team'])} @ {team_label(g['teams']['home']['team'])}"
              for g in games]
    embed = discord.Embed(title=title, url="https://www.mlb.com/schedule",
                          description="\n".join(lines)[:4000], color=color)
    embed.set_footer(text="MLB")
    embed.timestamp = datetime.now(timezone.utc)
    return content, embed


def first_started(games: list[dict], types: set[str]) -> tuple[int, list[dict]]:
    """這幾種賽事裡已經開打的第一天：回傳 (球季年份, 那天這幾種賽事的所有比賽)"""
    started = [g for g in games if g.get("gameType") in types
               and g.get("status", {}).get("abstractGameState") in ("Live", "Final")]
    if not started:
        return 0, []
    day = started[0].get("officialDate")
    return (int(started[0].get("season") or 0),
            [g for g in games if g.get("officialDate") == day and g.get("gameType") in types])


def season_end_message(season: int, game: dict) -> tuple[str, discord.Embed]:
    s = game.get("seriesStatus") or {}
    winner, loser = s.get("winningTeam") or {}, s.get("losingTeam") or {}
    result = re.search(r"(\d+)-(\d+)", s.get("result", ""))
    series = f" {result[1]}-{result[2]}" if result else ""
    lines = [f"🏆 **{team_label(winner)}** 在世界大賽以{series} 擊敗 {team_label(loser)}，"
             f"拿下 {season} 年世界大賽冠軍！",
             "", "MLB 正式進入休賽季，接下來是年度獎項、自由球員市場和冬季會議。"]
    embed = discord.Embed(title=f"MLB {season} 球季結束", url=game_url(game),
                          description="\n".join(lines), color=0xFFB81C)
    embed.set_footer(text="MLB")
    embed.timestamp = datetime.now(timezone.utc)
    return "🏖️ **MLB 休賽季開始**", embed


REG_END_MONTHS = (9, 10)  # 例行賽在 9 月底打完；這幾個月才檢查是不是最後一天
REG_END_CHECK_SECONDS = 30 * 60
PLAYOFF_DAY_DELAY = 10 * 60  # 當天比賽全部打完後，等 10 分鐘再發戰果
LEAGUE_ZH = {103: "美聯", 104: "國聯"}
DIVISION_ZH = {200: "西區", 201: "東區", 202: "中區", 203: "西區", 204: "東區", 205: "中區"}
BRACKET_LAYOUT = {  # 系列賽編號 → 對戰圖位置（每年固定）；外卡 F_2 的勝隊打 D_1，F_1 打 D_2，以此類推
    103: [["F_2", "F_1"], ["D_1", "D_2"], ["L_1"]],
    104: [["F_4", "F_3"], ["D_3", "D_4"], ["L_2"]],
}


async def regular_season_over(session: aiohttp.ClientSession, date: str) -> bool:
    """這天之後兩週都沒有還沒打的例行賽"""
    end = datetime.strptime(date, "%Y-%m-%d").date() + timedelta(days=14)
    data = await get_json(session, f"{MLB_API}/schedule", {
        "sportId": "1", "gameType": "R", "startDate": date, "endDate": str(end)})
    return not [g for d in data.get("dates", []) for g in d.get("games", [])
                if g.get("status", {}).get("abstractGameState") != "Final" and not is_called_off(g)]


async def fetch_standings(session: aiohttp.ClientSession, season: int) -> dict:
    """{聯盟 ID: {"divisions": [(分區 ID, [球隊戰績...])], "seeds": [(種子, 戰績, 怎麼晉級)]}}"""
    data = await get_json(session, f"{MLB_API}/standings", {
        "leagueId": "103,104", "season": str(season), "standingsTypes": "regularSeason"})
    leagues: dict = {}
    for rec in data.get("records") or []:
        league = (rec.get("league") or {}).get("id")
        teams = sorted(rec.get("teamRecords") or [], key=lambda t: int(t.get("divisionRank") or 99))
        leagues.setdefault(league, {"divisions": [], "seeds": []})["divisions"].append(
            ((rec.get("division") or {}).get("id"), teams))
    for league, info in leagues.items():
        teams = [t for _, ts in info["divisions"] for t in ts]
        # 分區冠軍看分區排名第 1（API 的 divisionChamp 欄位偶爾會標錯）
        champs = sorted((t for t in teams if str(t.get("divisionRank")) == "1"),
                        key=lambda t: int(t.get("leagueRank") or 99))
        wild = sorted((t for t in teams if t not in champs and int(t.get("wildCardRank") or 99) <= 3),
                      key=lambda t: int(t.get("wildCardRank") or 99))
        div_of = {t["team"]["id"]: d for d, ts in info["divisions"] for t in ts}
        info["seeds"] = ([(n + 1, t, f"{DIVISION_ZH.get(div_of[t['team']['id']], '')}冠軍") for n, t in enumerate(champs)]
                         + [(n + 1 + len(champs), t, "外卡") for n, t in enumerate(wild)])
    return leagues


def standings_message(season: int, leagues: dict) -> tuple[str, discord.Embed]:
    lines = ["例行賽全部打完！"]
    for league in (103, 104):
        info = leagues.get(league)
        if not info:
            continue
        lines += ["", f"**{LEAGUE_ZH[league]}季後賽名單**"]
        for seed, t, how in info["seeds"]:
            lines.append(f"{seed}. {team_label(t['team'])}　{t['wins']}-{t['losses']}（{how}）")
        seeded = {t["team"]["id"] for _, t, _ in info["seeds"]}
        for div, teams in sorted(info["divisions"], key=lambda d: "東中西".find(DIVISION_ZH.get(d[0], "  ")[0])):
            lines.append(f"{LEAGUE_ZH[league]}{DIVISION_ZH.get(div, '')}：" + "、".join(
                f"{team_name(t['team']['id'])} {t['wins']}-{t['losses']}{' ✅' if t['team']['id'] in seeded else ''}"
                for t in teams))
    embed = discord.Embed(title=f"MLB {season} 例行賽結束", url="https://www.mlb.com/standings",
                          description="\n".join(lines)[:4000], color=0x041E42)
    embed.set_footer(text="MLB")
    embed.timestamp = datetime.now(timezone.utc)
    return "🏁 **MLB 例行賽結束**", embed


# ----- 季後賽每天的戰果和對戰圖 -----
def series_name(desc: str) -> str:
    """'AL Championship Series' → '美聯冠軍賽'"""
    for en, zh_name in (("Wild Card Series", "外卡系列賽"), ("Division Series", "分區系列賽"),
                        ("Championship Series", "冠軍賽"), ("World Series", "世界大賽")):
        if en in desc:
            prefix = "美聯" if desc.startswith("AL") else "國聯" if desc.startswith("NL") else ""
            return prefix + zh_name
    return desc


def series_caption(names: list[str], wins: list[int], need: int, final: bool) -> str:
    """兩隊的中文隊名、勝場 → 「道奇 晉級 3-1」「道奇 領先 2-1」「系列賽 1-1 平手」"""
    lead = 0 if wins[0] >= wins[1] else 1
    w, l = wins[lead], wins[1 - lead]
    if w >= need:
        return f"{names[lead]} {'奪冠' if final else '晉級'} {w}-{l}"
    if w == l:
        return "尚未開打" if w == 0 else f"系列賽 {w}-{l} 平手"
    return f"{names[lead]} 領先 {w}-{l}"


def game_series_text(game: dict) -> str:
    """seriesStatus 的 'SEA wins 3-2'、'LAD leads 2-1'、'Series tied 1-1' → 中文"""
    result = (game.get("seriesStatus") or {}).get("result", "")
    m = re.match(r"(\w+) (wins|leads) (\d+)-(\d+)", result)
    if m:
        name = next((n for a, n in mlb_teams.values() if a == m[1]), m[1])
        verb = ("奪冠" if game.get("gameType") == "W" else "晉級") if m[2] == "wins" else "領先"
        return f"{name} {verb} {m[3]}-{m[4]}"
    m = re.match(r"Series tied (\d+)-(\d+)", result)
    return f"系列賽 {m[1]}-{m[2]} 平手" if m else result


async def bracket_image(session: aiohttp.ClientSession, season: int, date: str) -> bytes:
    data = await get_json(session, f"{MLB_API}/schedule/postseason/series",
                          {"season": str(season), "sportId": "1"})
    standings = await fetch_standings(session, season)
    seed = {t["team"]["id"]: n for info in standings.values() for n, t, _ in info["seeds"]}
    league_of = {t["team"]["id"]: lg for lg, info in standings.items() for _, ts in info["divisions"] for t in ts}
    series = {}
    for sr in data.get("series") or []:
        games = sr.get("games") or []
        if not games:
            continue
        sides = games[0].get("teams") or {}
        ids = [(sides.get(k) or {}).get("team", {}).get("id") for k in ("away", "home")]
        ids = [i if i in mlb_teams else None for i in ids]  # AL Champion 這類預留名稱 → 待定
        wins = [0, 0]
        for g in games:
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            for side in (g.get("teams") or {}).values():
                tid = (side.get("team") or {}).get("id")
                if side.get("isWinner") and tid in ids:
                    wins[ids.index(tid)] += 1
        series[(sr.get("series") or {}).get("id")] = {
            "ids": ids, "wins": wins, "need": (games[0].get("gamesInSeries") or 7) // 2 + 1}
    teams = {i for s in series.values() for i in s["ids"] if i}
    logos = await bracket.fetch_logos(session, {i: f"https://midfield.mlbstatic.com/v1/team/{i}/spots/128"
                                                for i in teams})
    font = await bracket.ensure_font(session)

    def box(sid: str, final: bool = False):
        s = series.get(sid)
        if not s:
            return bracket.Box()
        order = sorted(range(2), key=lambda n: seed.get(s["ids"][n], 99))
        if final:  # 世界大賽：美聯冠軍在上
            order = sorted(range(2), key=lambda n: league_of.get(s["ids"][n]) != 103)
        w = next((n for n in range(2) if s["wins"][n] >= s["need"]), None)
        started = any(s["wins"])
        out = [bracket.Team(mlb_teams[s["ids"][n]][0] if s["ids"][n] else "", logos.get(s["ids"][n]),
                            None if final else seed.get(s["ids"][n]),
                            s["wins"][n] if started or all(s["ids"]) else None,
                            won=n == w, lost=w is not None and n != w) for n in order]
        caption = (series_caption([team_name(s["ids"][n]) for n in order], [s["wins"][n] for n in order],
                                  s["need"], final) if all(s["ids"]) else "")
        return bracket.Box(out, caption)

    left = [[box(i) for i in rnd] for rnd in BRACKET_LAYOUT[103]]
    right = [[box(i) for i in rnd] for rnd in BRACKET_LAYOUT[104]]
    return bracket.render(font, f"MLB {season} 季後賽", f"更新至 {date}（美東時間）",
                          ("美聯", "國聯", "世界大賽"), left, right, box("W_1", final=True), (4, 30, 66))


async def playoff_day_message(session: aiohttp.ClientSession, date: str, games: list[dict]) -> tuple:
    lines = []
    for g in games:
        away, home = g["teams"]["away"], g["teams"]["home"]
        lines.append(f"**{series_name(g.get('seriesDescription', ''))} 第 {g.get('seriesGameNumber', '?')} 戰**")
        if is_called_off(g):
            lines.append(f"{team_label(away['team'])} @ {team_label(home['team'])}："
                         f"{zh(g.get('status', {}).get('detailedState', ''))}")
            continue
        series = game_series_text(g)
        lines.append(f"{team_label(away['team'])} **{away.get('score', 0)} : {home.get('score', 0)}** "
                     f"{team_label(home['team'])}" + (f"\n　└ {series}" if series else ""))
    embed = discord.Embed(title=f"MLB 季後賽戰果｜{date}", url=f"https://www.mlb.com/scores/{date}",
                          description="\n".join(lines)[:4000], color=0xC8102E)
    embed.set_footer(text="MLB")
    embed.timestamp = datetime.now(timezone.utc)
    content = "📋 **今日 MLB 季後賽全部結束**"
    if bracket:
        try:
            png = await bracket_image(session, int(games[0].get("season") or 0), date)
            embed.set_image(url="attachment://bracket.png")
            return content, embed, png
        except Exception as e:
            print(f"⚠️ 畫季後賽對戰圖失敗：{e}")
    return content, embed


async def playoff_day_updates(session: aiohttp.ClientSession, games: list[dict], s: dict, quiet: bool) -> list:
    """季後賽的日子，當天比賽全部結束後發一則戰果，附上對戰圖"""
    done = s.setdefault("post_days", [])
    by_date: dict[str, list[dict]] = {}
    for g in games:
        if g.get("gameType") in POSTSEASON_TYPES:
            by_date.setdefault(g.get("officialDate", ""), []).append(g)
    messages = []
    last_date = str(datetime.now(timezone.utc).date())  # fetch_games 查詢範圍的最後一天
    for date, day_games in sorted(by_date.items()):
        # 延賽的比賽會帶著改期後的日期出現，那天的比賽其實還沒打，先跳過
        if date in done or date > last_date or all(is_called_off(g) for g in day_games):
            continue
        if not all(g.get("status", {}).get("abstractGameState") == "Final" or is_called_off(g) for g in day_games):
            continue
        ready = s.setdefault("post_ready", {}).setdefault(date, time.time())  # 第一次看到全部打完的時間
        if not quiet and time.time() - ready < PLAYOFF_DAY_DELAY:
            continue
        done.append(date)
        if not quiet:
            messages.append(await playoff_day_message(session, date, day_games))
    s["post_days"] = done[-10:]
    s["post_ready"] = {d: t for d, t in s.get("post_ready", {}).items() if d not in done and d in by_date}
    return messages


async def season_updates(session: aiohttp.ClientSession, games: list[dict], state: dict,
                         initial: bool) -> tuple[list, list]:
    """例行賽第一場開打發「開季」、最後一天打完發「例行賽結束」和最終排名，季後賽（外卡賽）第一場開打發
    「季後賽開打」、每天打完發戰果和對戰圖，世界大賽打完發「休賽季開始」
    回傳 (要排在比賽訊息前面的, 要排在後面的)"""
    s = state.setdefault("season", {})
    first_time = s.get("v") != SEASON_STATE_VERSION
    if first_time:  # 第一次執行（或舊版升級）：已經開打的球季、季後賽當作發過了，避免季中才裝就發
        today = datetime.now(ET).date()
        try:
            data = await get_json(session, f"{MLB_API}/seasons/{today.year}", {"sportId": "1"})
        except Exception as e:  # 不影響這次的比分訊息，下次再試
            print(f"⚠️ 抓取球季日期失敗：{e}")
            return [], []
        dates = (data.get("seasons") or [{}])[0]
        if str(today) >= dates.get("regularSeasonStartDate", "9999"):
            s["opened"] = max(s.get("opened", 0), today.year)
        if str(today) >= dates.get("postSeasonStartDate", "9999"):
            s["playoffs"] = s["reg_end"] = today.year
        s["v"] = SEASON_STATE_VERSION
    quiet = initial or first_time
    before, after = [], []
    year, today_games = first_started(games, {"R"})
    if year > s.get("opened", 0):
        s["opened"] = year
        if not quiet:
            before.append(season_start_message("🎉 **MLB 開季**", f"MLB {year} 球季開幕",
                                               "例行賽正式開打！", today_games, 0x041E42))
    year, today_games = first_started(games, POSTSEASON_TYPES)
    if year > s.get("playoffs", 0):
        s["playoffs"] = year
        s["opened"] = max(s.get("opened", 0), year)
        if not quiet:
            before.append(season_start_message("🔥 **MLB 季後賽開打**", f"MLB {year} 季後賽開打",
                                               "外卡賽開打，季後賽正式開始！", today_games, 0xC8102E))
    # 例行賽最後一天：當天的例行賽都打完，之後兩週也沒有例行賽（延賽補賽也打完了）
    regular = [g for g in games if g.get("gameType") == "R"]
    last = max((g.get("officialDate", "") for g in regular), default="")
    last_day = [g for g in regular if g.get("officialDate") == last]
    year = int(last_day[0].get("season") or 0) if last_day else 0
    if (year > s.get("reg_end", 0) and datetime.now(ET).month in REG_END_MONTHS
            and all(g.get("status", {}).get("abstractGameState") == "Final" or is_called_off(g) for g in last_day)
            and time.time() - s.get("reg_checked", 0) >= REG_END_CHECK_SECONDS):
        s["reg_checked"] = time.time()
        try:
            standings = await fetch_standings(session, year) if await regular_season_over(session, last) else None
        except Exception as e:
            print(f"⚠️ 檢查例行賽是否結束失敗：{e}")
            standings = None
        if standings:
            s["reg_end"] = year
            if not quiet:
                after.append(standings_message(year, standings))
    after += await playoff_day_updates(session, games, s, quiet)
    for game in games:
        year = int(game.get("season") or 0)
        # 等當天的季後賽戰果發出後，才接著發休賽季開始
        if (is_title_clincher(game) and year > s.get("closed", 0)
                and game.get("officialDate") in s["post_days"]):
            s["closed"] = year
            if not quiet:
                after.append(season_end_message(year, game))
    return before, after


# ===== 比對新舊資料，產生要發送的訊息 =====
async def game_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    games = await fetch_games(session)
    messages = day_done_updates(games, state, initial)
    new_games, live = {}, 0
    for game in games:
        pk = str(game["gamePk"])
        status = game.get("status", {})
        abstract, detailed = status.get("abstractGameState", ""), status.get("detailedState", "")
        prev = state["games"].get(pk)
        innings = completed_innings(game)
        score = [game["teams"]["away"].get("score", 0), game["teams"]["home"].get("score", 0)]
        cur = {
            "abstract": abstract, "status": detailed, "innings": innings, "score": score,
            "started": bool(prev and prev["started"]) or detailed == "In Progress"
                       or abstract == "Final",
            # 已經看過幾筆得分紀錄；None 代表還不知道，下次抓到時只記錄不發快報
            "scored": prev.get("scored") if prev else (0 if abstract == "Preview" else None),
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
        live += abstract == "Live"

        notable = any(w in detailed for w in NOTABLE_STATUS)
        # 舊版的狀態沒有 innings，當作已經發到目前這局，不補發
        done = prev.get("innings", innings)
        # 這場比賽有變化（可能要發訊息）時，先翻譯訊息裡會用到的英文
        if (abstract, detailed, innings, score) != (prev["abstract"], prev["status"], done, prev.get("score")):
            await prefetch(session, game_texts(game))
        if abstract == "Final":
            if prev["abstract"] != "Final":
                if notable:
                    messages.append(status_message(game))
                else:  # 先補最後幾局的摘要（含最後的得分），再另外發一則終場結果
                    plays = await try_scoring_plays(session, game) if innings > done else []
                    messages += inning_messages(game, done, innings, plays)
                    messages.append(final_message(game))
            continue
        if detailed != prev["status"] and notable:
            messages.append(status_message(game))
        if abstract != "Live":
            continue
        if detailed == "In Progress" and not prev["started"]:
            messages.append(start_message(game))
        # 有局打完或比分變了才去抓得分過程
        if innings <= done and score == prev.get("score") and cur["scored"] is not None:
            continue
        plays = await try_scoring_plays(session, game)
        messages += inning_messages(game, done, innings, plays)
        if plays is None:
            cur["score"] = prev.get("score")  # 下次再試
            continue
        seen = len(plays) if cur["scored"] is None else cur["scored"]
        # 已經打完的局會出現在上面的摘要裡，快報只發還在進行中這局的得分
        fresh = [p for p in plays[seen:] if (p.get("about") or {}).get("inning", 0) > innings]
        if fresh:
            messages.append(scoring_alert(game, fresh))
        cur["scored"] = len(plays)
    state["games"] = new_games
    state["live_games"] = live  # 有即時比分的比賽正在進行：check_once 這次會每 30 秒再檢查
    before, after = await season_updates(session, games, state, initial)
    return before + messages + after


async def tx_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    seen = set(state["tx"])
    new_txs = [t for t in await fetch_transactions(session)
               if wanted_tx(t) and str(t.get("id")) not in seen]
    state["tx"] += [str(t.get("id")) for t in new_txs]
    try:  # 先從最新的新聞記下合約金額（簽約時附上）
        remember_contracts(state, await fetch_news(session))
    except Exception as e:
        print(f"⚠️ 從新聞找合約金額失敗：{e}")
    if initial:
        return []
    await asyncio.gather(*(translate_tx(session, t) for t in new_txs))
    await prefetch(session, [t.get("typeDesc", "") for t in new_txs if t.get("typeCode") not in TX_LABELS])
    messages, posted = [], set()
    for tx in new_txs:
        desc = tx.get("description", "")
        if desc in posted:  # 多人交易常會每個球員各一筆相同敘述，只發一次
            continue
        posted.add(desc)
        messages.append(tx_message(tx, state.get("contracts")))
    return messages


async def news_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    seen = set(state["news"])
    new_articles = [a for a in await fetch_news(session)
                    if wanted_news(a) and article_id(a) not in seen]
    new_articles.reverse()  # 舊的先發
    state["news"] += [article_id(a) for a in new_articles]
    if initial:
        return []
    return [(news_label(a), news_embed(a)) for a in new_articles]  # 新聞直接發英文原文


def recipient_key(r: dict) -> str:
    return ":".join(str(x) for x in (r.get("id"), r.get("season"), (r.get("player") or {}).get("id"),
                                     (r.get("team") or {}).get("id"), r.get("notes") or ""))


async def award_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    if not initial and time.time() - state.get("awards_checked", 0) < AWARD_CHECK_MINUTES * 60:
        return []
    pairs = [(a, s) for a in await fetch_award_defs(session) if not is_periodic(a)
             for s in award_seasons()]
    limit = asyncio.Semaphore(8)  # 同時最多 8 個請求，避免對 MLB API 太兇

    async def fetch(award: dict, season: int) -> list[dict]:
        async with limit:
            return await fetch_award_recipients(session, award["id"], season)

    results = await asyncio.gather(*(fetch(a, s) for a, s in pairs))
    seen = set(state["awards"])
    # 剛加進 EXTRA_AWARDS 的獎項：已經公布過的得主只記錄、不補發
    quiet = EXTRA_AWARDS - set(state.get("extra_awards", []))
    state["extra_awards"] = sorted(EXTRA_AWARDS)
    keys, new = [], {}
    for (award, season), recipients in zip(pairs, results):
        for r in recipients:
            key = recipient_key(r)
            keys.append(key)
            if key not in seen and award["id"] not in quiet:
                new.setdefault((award["id"], season), (award, []))[1].append(r)
    state["awards"] = keys  # 只保留目前檢查的年度，檔案不會一直變大
    state["awards_checked"] = time.time()
    if initial:
        return []
    await prefetch(session, [a.get("name", "") for (_, _), (a, _) in new.items() if a.get("id") not in AWARD_ZH]
                   + [r.get("notes", "") for _, rs in new.values() for r in rs])
    return [award_message(award, str(season), recipients)
            for (_, season), (award, recipients) in new.items()]


async def honor_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    """每週 / 每月最佳球員、投手、新秀、救援投手、美技"""
    if not initial and time.time() - state.get("honors_checked", 0) < AWARD_CHECK_MINUTES * 60:
        return []
    awards = [a for a in await fetch_award_defs(session) if is_periodic(a)]
    season = award_seasons()[-1]
    results = await asyncio.gather(*(fetch_award_recipients(session, a["id"], season) for a in awards))
    seen = set(state["honors"])
    groups: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
    for award, recipients in zip(awards, results):
        for r in recipients:
            key = f"{recipient_key(r)}:{r.get('date', '')}"  # 同一人連續得獎時用日期區分
            if key in seen:
                continue
            seen.add(key)
            state["honors"].append(key)
            groups.setdefault((r.get("date", ""), periodic_kind(award["id"])), []).append((award, r))
    state["honors_checked"] = time.time()
    if initial:
        return []
    items = [x for g in groups.values() for x in g]
    await prefetch(session, [a.get("name", "") for a, _ in items if periodic_kind(a.get("id", "")) not in PERIODIC_ZH]
                   + [r.get("notes", "") for _, r in items])
    return [honor_message(items) for _, items in sorted(groups.items())]  # 舊的先發


# ===== 選秀、樂透抽籤 =====
def draft_year() -> int:
    """選秀在 7 月，8 月以後就看明年的選秀（12 月的抽籤也是明年的選秀）"""
    now = datetime.now(ET)
    return now.year if now.month <= 7 else now.year + 1


async def fetch_draft_picks(session: aiohttp.ClientSession, year: int, **params) -> list[tuple[str, dict]]:
    """[(輪次, 順位資料)]，依順位排序；順位還沒決定時回傳空清單"""
    data = await get_json(session, f"{MLB_API}/draft/{year}", params or None)
    picks = [(r.get("round", ""), p) for r in (data.get("drafts") or {}).get("rounds") or []
             for p in r.get("picks") or []]
    return sorted(picks, key=lambda x: x[1].get("pickNumber") or 0)


async def fetch_latest_pick(session: aiohttp.ClientSession, year: int) -> dict | None:
    """最新選出的一位（資料很小，每次都可以查）；選秀還沒開始時 MLB 會回錯誤，當作沒有"""
    try:
        return (await get_json(session, f"{MLB_API}/draft/{year}/latest")).get("pick")
    except Exception:
        return None


def pick_made(p: dict) -> bool:
    return bool(p.get("isDrafted") and (p.get("person") or {}).get("fullName"))


def round_name(label: str) -> str:
    if label.isdigit():
        return f"第 {label} 輪"
    m = re.fullmatch(r"(\d+)C", label)
    return f"第 {m.group(1)} 輪補償籤" if m else DRAFT_ROUND_ZH.get(label, label)


def pick_line(p: dict) -> str:
    person = p.get("person") or {}
    pos = (person.get("primaryPosition") or {}).get("abbreviation", "")
    school = (p.get("school") or {}).get("name", "")
    extra = "｜".join(x for x in (POS_ZH.get(pos, pos), school) if x)
    return (f"**#{p.get('pickNumber')}** {team_label(p.get('team') or {})}："
            f"{person.get('fullName', '')}" + (f"｜{extra}" if extra else ""))


def draft_embed(year: int, title: str, lines: list[str], color: int = 0x041E42,
                url: str = "https://www.mlb.com/draft/tracker") -> discord.Embed:
    embed = discord.Embed(title=title[:256], description="\n".join(lines)[:4000], color=color, url=url)
    embed.set_footer(text=f"{year} MLB 選秀")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def move_text(team_id: str, slot: int, before: list[str] | None) -> str:
    if not before or team_id not in before:
        return ""
    was = before.index(team_id) + 1
    return (f"⬆️ 上升 {was - slot} 名（原第 {was}）" if was > slot else
            f"⬇️ 下滑 {slot - was} 名（原第 {was}）" if was < slot else "➖ 維持原順位")


def order_lines(first_round: list[dict], before: list[str] | None) -> list[str]:
    lines = []
    for slot, p in enumerate(first_round, 1):
        team = p.get("team") or {}
        mark = "（抽籤）" if slot <= LOTTERY_DRAWN else ""
        move = move_text(str(team.get("id")), slot, before) if slot <= LOTTERY_TEAMS else ""
        lines.append(f"{'🏆 狀元' if slot == 1 else f'第 {slot} 順位'}{mark}：{team_label(team)}"
                     + (f"　{move}" if move else ""))
    return lines


def lottery_reveal(year: int, first_round: list[dict], before: list[str] | None) -> list:
    """樂透抽籤揭曉：抽出來的第 6 順位一隊一隊發到狀元，最後發第 1 輪完整順位"""
    messages = []
    for slot in range(LOTTERY_DRAWN, 0, -1):
        team = first_round[slot - 1].get("team") or {}
        if slot == 1:
            content, title = "🏆 **MLB 狀元籤揭曉！**", f"{year} 狀元籤：{team_label(team)}"
        else:
            content, title = "🎰 **MLB 樂透抽籤**", f"第 {slot} 順位：{team_label(team)}"
        messages.append((content, draft_embed(year, title, [move_text(str(team.get("id")), slot, before)],
                                              0x7B1FA2, "https://www.mlb.com/draft/order")))
    messages.append(("📋 **MLB 樂透抽籤結果與第 1 輪順位**", draft_embed(
        year, f"{year} MLB 選秀第 1 輪順位", order_lines(first_round, before), 0x7B1FA2,
        "https://www.mlb.com/draft/order")))
    return messages


def pick_message(year: int, label: str, p: dict) -> tuple[str, discord.Embed]:
    """選秀：每選一位發一則"""
    person = p.get("person") or {}
    pos = (person.get("primaryPosition") or {}).get("abbreviation", "")
    school, home = p.get("school") or {}, p.get("home") or {}
    lines = [f"{round_name(label)}第 {p.get('roundPickNumber') or '?'} 順位"]
    if pos:
        lines.append(f"位置：{POS_ZH.get(pos, pos)}")
    if school.get("name"):
        lines.append(f"來自：{school['name']}" + (f"（{school['schoolClass']}）" if school.get("schoolClass") else ""))
    if home.get("city"):
        lines.append(f"家鄉：{', '.join(x for x in (home.get('city'), home.get('state'), home.get('country')) if x)}")
    embed = draft_embed(year, f"第 {p.get('pickNumber')} 順位｜{team_label(p.get('team') or {})} 選中 "
                              f"{person.get('fullName', '')}", lines)
    if p.get("headshotLink"):
        embed.set_thumbnail(url=p["headshotLink"])
    return "⚾ **MLB 選秀**", embed


def draft_picks_messages(year: int, picks: list[tuple[str, dict]], title: str, content: str) -> list:
    """一則最多 35 位，太多就分成好幾則（後面的標題加「續」）"""
    messages = []
    for i in range(0, len(picks), 35):
        chunk = picks[i:i + 35]
        rounds = list(dict.fromkeys(round_name(r) for r, _ in chunk))
        embed = discord.Embed(title=(title.format(rounds="、".join(rounds)) + ("（續）" if i else ""))[:256],
                              description="\n".join(pick_line(p) for _, p in chunk)[:4000],
                              color=0x041E42, url="https://www.mlb.com/draft/tracker")
        embed.set_footer(text=f"{year} MLB 選秀")
        embed.timestamp = datetime.now(timezone.utc)
        messages.append((content, embed))
    return messages


async def draft_updates(session: aiohttp.ClientSession, state: dict, initial: bool) -> list:
    """樂透抽籤一隊一隊揭曉 → 第 1 輪順位 → 選秀每選一位發一則（第一天）→ 選秀結果"""
    year = draft_year()
    d = state["draft"]
    if d.get("year") != year:  # 換年度：重新開始記錄
        d.clear()
        d["year"] = year
    messages = []
    latest = await fetch_latest_pick(session, year)
    drafting = latest is not None and pick_made(latest) and latest.get("pickNumber", 0) > d.get("last_pick", 0)

    if not drafting:
        # 選秀還沒開始：看第 1 輪順位有沒有出爐（MLB 抽籤後才公布順位）
        if d.get("last_pick") or d.get("lottery_done"):
            return []
        if datetime.now(ET).month not in LOTTERY_MONTHS and time.time() - d.get("checked", 0) < 3600:
            return []
        d["checked"] = time.time()
        first_round = [p for _, p in await fetch_draft_picks(session, year, round="1")]
        ids = [str((p.get("team") or {}).get("id")) for p in first_round[:LOTTERY_TEAMS]]
        if len(ids) < LOTTERY_TEAMS or "None" in ids:
            return []
        before = d.get("order")
        reordered = bool(before) and sorted(before) == sorted(ids) and before != ids
        d["order"] = ids
        if reordered or (not before and datetime.now(ET).month in LOTTERY_MONTHS):
            d["lottery_done"] = True
            messages += lottery_reveal(year, first_round, before if reordered else None)
        elif not before:  # 抽籤前就看到順位：先公布，之後順位重新排列時再發抽籤結果
            messages.append(("📋 **MLB 選秀順位公布（樂透抽籤前）**", draft_embed(
                year, f"{year} MLB 選秀第 1 輪順位", order_lines(first_round, None))))
        return [] if initial else messages

    # 選秀進行中：抓完整名單
    picks = await fetch_draft_picks(session, year)
    labels = list(dict.fromkeys(r for r, _ in picks))
    live = set(labels[:labels.index("3")] if "3" in labels else labels)  # 第一天：第 3 輪之前
    last = d.get("last_pick", 0)
    new = [(r, p) for r, p in picks if pick_made(p) and p.get("pickNumber", 0) > last]
    d["last_pick"] = max([last] + [p.get("pickNumber", 0) for _, p in picks if pick_made(p)])
    d["lottery_done"] = True
    if new:
        d["live_until"] = time.time() + DRAFT_LIVE_SECONDS
    # 第一天（第 1、2 輪和補償籤）每選一位發一則；第 3 輪以後人很多，每次檢查合併成一則
    messages += [pick_message(year, r, p) for r, p in new if r in live]
    messages += draft_picks_messages(year, [(r, p) for r, p in new if r not in live],
                                     f"{year} MLB 選秀｜{{rounds}}", "⚾ **MLB 選秀進行中**")

    if not d.get("finished") and picks and all(pick_made(p) or p.get("isPass") for _, p in picks):
        d["finished"] = True
        day_one = [(r, p) for r, p in picks if r in live and pick_made(p)]
        messages += draft_picks_messages(year, day_one, f"{year} MLB 選秀結果｜{{rounds}}",
                                         f"📋 **{year} MLB 選秀結果**")
        messages.append(("🏁 **MLB 選秀結束**", draft_embed(
            year, f"{year} MLB 選秀結束",
            [f"共 {sum(pick_made(p) for _, p in picks)} 位球員被選中，上面是第 1、2 輪和補償籤名單",
             "👉 點上方標題查看全部 20 輪名單"])))
    return [] if initial else messages


# ===== 健康檢查：資料來源連續好幾個小時抓不到時，在 Discord 提醒 =====
HEALTH_ALERT_HOURS = 6  # 連續失敗這麼久才提醒（偶爾故障幾分鐘不吵）
HEALTH_REMIND_DAYS = 7  # 一直沒恢復時，每隔幾天再提醒一次


def health_fail(health: dict, name: str, error) -> list:
    now = time.time()
    h = health.setdefault(name, {"since": now})
    h["error"] = str(error)[:300]
    hours = (now - h["since"]) / 3600
    if hours < HEALTH_ALERT_HOURS or now - h.get("alerted", 0) < HEALTH_REMIND_DAYS * 86400:
        return []
    h["alerted"] = now
    embed = discord.Embed(
        title=f"{name}已經 {hours:.0f} 小時抓不到資料",
        description=f"最後的錯誤：{h['error']}\n\n可能是資料來源暫時故障，或是改了格式。"
                    f"恢復後會再通知；如果好幾天都沒恢復，可能需要修改程式。",
        color=0xF57C00)
    embed.set_footer(text="MLB bot 健康檢查")
    embed.timestamp = datetime.now(timezone.utc)
    return [("⚠️ **MLB bot 健康檢查**", embed)]


def health_ok(health: dict, name: str) -> list:
    h = health.pop(name, None)
    if not h or not h.get("alerted"):
        return []
    embed = discord.Embed(title=f"{name}已恢復正常",
                          description=f"中斷了大約 {(time.time() - h['since']) / 3600:.0f} 小時", color=0x43A047)
    embed.set_footer(text="MLB bot 健康檢查")
    embed.timestamp = datetime.now(timezone.utc)
    return [("✅ **MLB bot 健康檢查**", embed)]


async def collect_updates(session: aiohttp.ClientSession) -> tuple[list, dict]:
    """回傳 (要發送的 [(文字, embed)], 更新後的狀態)；呼叫端發完訊息後要 save_state"""
    state = load_state() or {}
    health = state.setdefault("health", {})
    _translate_stats.update(ok=0, fail=0)
    await refresh_teams(session)
    messages = []
    for key, name, check, empty in (("games", "比分", game_updates, {}),
                                    ("tx", "異動", tx_updates, []),
                                    ("news", "新聞", news_updates, []),
                                    ("awards", "獎項", award_updates, []),
                                    ("honors", "每週/每月獎項", honor_updates, []),
                                    ("draft", "選秀", draft_updates, {})):
        # 第一次抓到某個來源時只記錄現有資料，不發文，避免洗版
        initial = key not in state
        state.setdefault(key, empty)
        try:
            messages += await check(session, state, initial)
        except Exception as e:
            print(f"⚠️ 抓取{name}失敗：{e}")
            messages += health_fail(health, name, e)
            if initial:
                del state[key]  # 下次成功時再初始化
            continue
        messages += health_ok(health, name)
        if initial:
            print(f"初始化完成，已記錄 {len(state[key])} 筆{name}")
    # 翻譯：這次有翻譯但全部失敗才算故障（失敗時訊息會改顯示英文）
    if _translate_stats["fail"] and not _translate_stats["ok"]:
        messages += health_fail(health, "中文翻譯", "Google 翻譯的請求全部失敗，訊息會先顯示英文")
    elif _translate_stats["ok"]:
        messages += health_ok(health, "中文翻譯")
    return messages, state


# ===== Discord 機器人 =====
def joined_channels(client: discord.Client) -> list:
    """bot 被單獨加進去的頻道（頻道權限裡特別允許這個 bot 或它的專屬身分組發送訊息），加上 CHANNEL_ID 指定的頻道。
    只是邀請進伺服器、沒有加進任何頻道的話不會發"""
    found = []
    for guild in client.guilds:
        for ch in guild.text_channels:
            for target, ow in ch.overwrites.items():
                is_me = target.id == client.user.id or (
                    isinstance(target, discord.Role) and target.tags and target.tags.bot_id == client.user.id)
                if is_me and ow.send_messages:
                    found.append(ch)
                    break
    found += [c for c in map(client.get_channel, CHANNEL_IDS) if c is not None and c not in found]
    return found


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
        channels = joined_channels(self)
        if not channels:
            print("⚠️ bot 還沒有被加進任何頻道：請在頻道的權限設定加入這個 bot 並允許發送訊息")
            return  # 不存狀態，下次再試
        for content, embed, *image in messages:  # 有些訊息附一張圖（季後賽對戰圖）
            for channel in channels:
                try:
                    files = [discord.File(io.BytesIO(image[0]), "bracket.png")] if image else []
                    await channel.send(content=content, embed=embed, files=files)
                    print(f"已發送到 {channel.guild.name}：{content} {embed.title}")
                except discord.HTTPException as e:
                    print(f"⚠️ 發送到 {channel.guild.name} 失敗：{e}")
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
        return f"🏁 {score}｜{zh(status.get('detailedState', ''))}"
    return f"🔴 {score}｜{inning_text(game) or zh(status.get('detailedState', ''))}"


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
    await prefetch(bot.session, [g.get("status", {}).get("detailedState", "") for g in games])
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
        articles = [a for a in await fetch_news(bot.session) if wanted_news(a)]
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
    txs = txs[-40:]  # 最多翻譯最近 40 筆，embed 也放不下更多
    await asyncio.gather(*(translate_tx(bot.session, t) for t in txs))
    lines = list(dict.fromkeys(
        f"{TX_LABELS.get(t.get('typeCode'), ('•',))[0]}｜{t.get('date', '')}｜{zh(t.get('description', ''))}"
        for t in reversed(txs)))
    text = ""
    for line in lines:  # 新的在上面，塞到 embed 上限為止
        if len(text) + len(line) > 3900:
            break
        text += line + "\n"
    embed = discord.Embed(title=f"MLB 簽約 / 交易（最近 {days} 天）", description=text, color=0x1E88E5)
    await interaction.followup.send(embed=embed)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("請先在 .env 設定 DISCORD_TOKEN")
    bot.run(DISCORD_TOKEN)
