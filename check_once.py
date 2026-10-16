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
LIVE_SECONDS = 150  # 選秀進行中：這次執行再多檢查 2.5 分鐘（GitHub 每次最多跑 5 分鐘，安裝套件約 1 分鐘）
LIVE_INTERVAL = 20  # 每 20 秒檢查一次


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


async def main() -> None:
    if not DISCORD_TOKEN or not CHANNEL_IDS:
        raise SystemExit("缺少 DISCORD_TOKEN 或 CHANNEL_ID（頻道 ID 必須是數字，好幾個用逗號隔開）")

    deadline = time.monotonic() + LIVE_SECONDS
    async with aiohttp.ClientSession() as session:
        while True:
            messages, state = await collect_updates(session)
            sent = 0
            for content, embed, *image in messages:
                # 每則消息發到所有頻道（可以在不同伺服器）
                ok = [await deliver(session, c, content, embed, image[0] if image else None) for c in CHANNEL_IDS]
                sent += sum(ok)
                print(f"已發送到 {sum(ok)}/{len(CHANNEL_IDS)} 個頻道：{content} {embed.title}")
            save_state(state)
            print(f"檢查完成，發送 {sent}/{len(messages) * len(CHANNEL_IDS)} 則（{len(CHANNEL_IDS)} 個頻道）")
            # 選秀進行中才繼續每 20 秒檢查，其他時候檢查一次就結束
            live = (state.get("draft") or {}).get("live_until", 0) > time.time()
            if not live or time.monotonic() + LIVE_INTERVAL > deadline:
                break
            await asyncio.sleep(LIVE_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
