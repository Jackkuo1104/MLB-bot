"""
檢查一次 MLB 比分、異動和新聞，把新的消息推送到 Discord 後就結束
給 GitHub Actions 排程使用（不需要電腦開著）；本機常駐版請用 bot.py
"""

import asyncio
import json
import time

import aiohttp
import discord

from bot import CHANNEL_IDS, DISCORD_TOKEN, collect_updates, save_state

DISCORD_API = "https://discord.com/api/v10"
LIVE_SECONDS = 210  # 選秀或比賽進行中：這次執行再多檢查 3.5 分鐘（GitHub 每次最多跑 5 分鐘，安裝套件約 15 秒）
LIVE_INTERVAL = 20  # 選秀進行中每 20 秒檢查一次
GAME_INTERVAL = 30  # 有即時比分的比賽進行中每 30 秒檢查一次
SEND_MESSAGES = 1 << 11  # Discord「發送訊息」權限
TEXT_CHANNEL_TYPES = (0, 5)  # 文字頻道、公告頻道


async def send_message(session: aiohttp.ClientSession, channel_id: int, content: str, embed: discord.Embed,
                       image: bytes | None = None) -> None:
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    payload = {"content": content, "embeds": [embed.to_dict()]}
    for _ in range(3):
        if image:  # 附圖片（季後賽對戰圖）要用 multipart 上傳
            form = aiohttp.FormData()
            form.add_field("payload_json", json.dumps(payload), content_type="application/json")
            form.add_field("files[0]", image, filename="bracket.png", content_type="image/png")
            body = {"data": form}
        else:
            body = {"json": payload}
        async with session.post(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30),
                                **body) as resp:
            if resp.status == 429:  # 被 Discord 限速，等一下再試
                await asyncio.sleep((await resp.json()).get("retry_after", 5))
                continue
            if resp.status >= 400:
                raise RuntimeError(f"Discord 回傳 {resp.status}：{await resp.text()}")
            return
    raise RuntimeError("Discord 持續限速，放棄發送")


async def deliver(session: aiohttp.ClientSession, channel_id: int, content: str, embed: discord.Embed,
                  image: bytes | None) -> bool:
    """發到一個頻道；附圖失敗時改發不附圖。回傳有沒有發成功"""
    try:
        try:
            await send_message(session, channel_id, content, embed, image)
        except Exception as e:
            if not image:
                raise
            print(f"⚠️ 頻道 {channel_id} 附圖發送失敗，改發不附圖：{e}")
            plain = discord.Embed.from_dict(embed.to_dict())
            plain.set_image(url=None)
            await send_message(session, channel_id, content, plain)
        return True
    except Exception as e:
        print(f"⚠️ 發送到頻道 {channel_id} 失敗：{e}")
        return False


async def discord_get(session: aiohttp.ClientSession, path: str):
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    for _ in range(3):
        async with session.get(f"{DISCORD_API}{path}", headers=headers,
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 429:
                await asyncio.sleep((await resp.json()).get("retry_after", 5))
                continue
            resp.raise_for_status()
            return await resp.json()
    raise RuntimeError("Discord 持續限速")


async def joined_channels(session: aiohttp.ClientSession) -> list[int]:
    """bot 被單獨加進去的頻道：頻道權限裡有特別設定這個 bot（或它的專屬身分組）可以發送訊息。
    只是邀請進伺服器、沒有加進任何頻道的話不會發"""
    me = await discord_get(session, "/users/@me")
    found = []
    for guild in await discord_get(session, "/users/@me/guilds"):
        roles = await discord_get(session, f"/guilds/{guild['id']}/roles")
        targets = {me["id"]} | {r["id"] for r in roles if (r.get("tags") or {}).get("bot_id") == me["id"]}
        for ch in await discord_get(session, f"/guilds/{guild['id']}/channels"):
            if ch.get("type") in TEXT_CHANNEL_TYPES and any(
                    o.get("id") in targets and int(o.get("allow") or 0) & SEND_MESSAGES
                    for o in ch.get("permission_overwrites") or []):
                found.append(int(ch["id"]))
                print(f"頻道：{guild.get('name')} #{ch.get('name')}")
    return found


async def target_channels(session: aiohttp.ClientSession) -> list[int]:
    """要發送的頻道 = bot 被加進去的頻道 + CHANNEL_ID 另外指定的頻道"""
    try:
        found = await joined_channels(session)
    except Exception as e:
        print(f"⚠️ 查詢 bot 所在的頻道失敗，只發到 CHANNEL_ID：{e}")
        found = []
    return list(dict.fromkeys(found + CHANNEL_IDS))


async def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit("缺少 DISCORD_TOKEN")

    deadline = time.monotonic() + LIVE_SECONDS
    async with aiohttp.ClientSession() as session:
        channels = await target_channels(session)
        if not channels:  # 不存狀態，等 bot 加進頻道後再發，消息不會漏掉
            raise SystemExit("bot 還沒有被加進任何頻道：請在頻道的權限設定加入這個 bot 並允許發送訊息")
        quick = False  # 第一次全部檢查，之後的加密檢查只看比分和選秀
        while True:
            messages, state = await collect_updates(session, quick)
            sent = 0
            for content, embed, *image in messages:
                # 每則消息發到所有頻道（可以在不同伺服器）
                ok = [await deliver(session, c, content, embed, image[0] if image else None) for c in channels]
                sent += sum(ok)
                print(f"已發送到 {sum(ok)}/{len(channels)} 個頻道：{content} {embed.title}")
            save_state(state)
            print(f"檢查完成，發送 {sent}/{len(messages) * len(channels)} 則（{len(channels)} 個頻道）")
            # 選秀或有即時比分的比賽進行中才繼續檢查，其他時候檢查一次就結束
            draft_live = (state.get("draft") or {}).get("live_until", 0) > time.time()
            if not draft_live and not state.get("live_games"):
                break
            interval = LIVE_INTERVAL if draft_live else GAME_INTERVAL
            if time.monotonic() + interval > deadline:
                break
            await asyncio.sleep(interval)
            quick = True


if __name__ == "__main__":
    asyncio.run(main())
