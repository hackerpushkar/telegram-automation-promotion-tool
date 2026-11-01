import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
from src.db import Database

async def check():
    await Database.connect()
    col = Database.get_leads_collection()
    count = await col.count_documents({})
    bot_count = await col.count_documents({'lead_type': 'bot'})
    user_count = await col.count_documents({'lead_type': 'user'})
    print(f"Total Leads: {count} | Userbots: {bot_count} | Usernames: {user_count}")

    chan_col = Database.get_discovered_channels_collection()
    chan_count = await chan_col.count_documents({})
    print(f"Total Discovered Channels: {chan_count}")

    print("\nRecent Extracted Leads:")
    recent = await col.find({}).sort("first_seen_at", -1).to_list(10)
    for r in recent:
        uname = r.get("username_clean")
        ltype = r.get("lead_type")
        src_name = r.get("extracted_from", {}).get("source_name", "Unknown") if isinstance(r.get("extracted_from"), dict) else "Direct"
        safe_src = src_name.encode('ascii', 'ignore').decode('ascii')
        print(f"  - @{uname} ({ltype}) from channel: {safe_src}")

    await Database.close()

if __name__ == "__main__":
    asyncio.run(check())
