import os
import sys
import asyncio

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.channel_publisher import edit_promo_post_caption
from src.db import Database


async def main():
    await Database.connect()
    print("Editing Post #2 on Telegram channel...")
    res = await edit_promo_post_caption(2)
    print("Updated successfully!")
    print(f"Post #2 Link: {res.get('message_link')}")
    print("New Caption preview:\n" + res.get("caption", ""))


if __name__ == "__main__":
    asyncio.run(main())
