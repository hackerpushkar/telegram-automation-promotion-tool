import os
import sys
import asyncio

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.channel_publisher import publish_promo_post
from src.db import Database


async def main():
    await Database.connect()
    print("==================================================================")
    print("📢 Publishing 5 Promotional Posts to @pro_affiliaters Channel")
    print("==================================================================\n")

    results = []
    for num in range(1, 6):
        try:
            print(f"🚀 Publishing Post #{num}...")
            res = await publish_promo_post(num)
            msg_link = res.get("message_link")
            msg_id = res.get("message_id")
            deep_link = res.get("deep_link")
            title = res.get("title")

            print(f"✅ Post #{num} LIVE: {title}")
            print(f"   Link:      {msg_link}")
            print(f"   ID:        {msg_id}")
            print(f"   Deep Link: {deep_link}\n")
            results.append(res)
            await asyncio.sleep(2.0)
        except Exception as e:
            print(f"❌ Error publishing Post #{num}: {e}\n")

    print("==================================================================")
    print(f"🎉 Complete! Published {len(results)}/5 posts to @pro_affiliaters.")
    print("==================================================================")


if __name__ == "__main__":
    asyncio.run(main())
