"""
檢查一次 MLB 比分、異動和新聞，把新的消息推送到 Discord 後就結束
給 GitHub Actions 排程使用（不需要電腦開著）；本機常駐版請用 bot.py
"""

import asyncio
import time

import aiohttp
import discord

from bot import CHANNEL_ID, DISCORD_TOKEN, collect_updates, save_state

DISCORD_API = "https://discord.com/api/v10"
LIVE_SECONDS = 150  # 選秀進行中：這次執行再多檢查 2.5 分鐘（GitHub 每次最多跑 5 分鐘，安裝套件約 1 分鐘）
LIVE_INTERVAL = 20  # 每 20 秒檢查一次


async def send_message(session: aiohttp.ClientSession, content: str, embed: discord.Embed) -> None:
    url = f"{DISCORD_API}/channels/{CHANNEL_ID}/messages"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}"}
    payload = {"content": content, "embeds": [embed.to_dict()]}
    for _ in range(3):
        async with session.post(url, headers=headers, json=payload,
                                timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 429:  # 被 Discord 限速，等一下再試
                await asyncio.sleep((await resp.json()).get("retry_after", 5))
                continue
            if resp.status >= 400:
                raise RuntimeError(f"Discord 回傳 {resp.status}：{await resp.text()}")
            return
    raise RuntimeError("Discord 持續限速，放棄發送")


async def main() -> None:
    if not DISCORD_TOKEN or not CHANNEL_ID:
        raise SystemExit("缺少 DISCORD_TOKEN 或 CHANNEL_ID（CHANNEL_ID 必須是數字）")

    deadline = time.monotonic() + LIVE_SECONDS
    async with aiohttp.ClientSession() as session:
        while True:
            messages, state = await collect_updates(session)
            sent = 0
            for content, embed in messages:
                try:
                    await send_message(session, content, embed)
                    sent += 1
                    print(f"已發送：{content} {embed.title}")
                except Exception as e:
                    print(f"⚠️ 發送失敗：{e}")
            save_state(state)
            print(f"檢查完成，發送 {sent}/{len(messages)} 則")
            # 選秀進行中才繼續每 20 秒檢查，其他時候檢查一次就結束
            live = (state.get("draft") or {}).get("live_until", 0) > time.time()
            if not live or time.monotonic() + LIVE_INTERVAL > deadline:
                break
            await asyncio.sleep(LIVE_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
