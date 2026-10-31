import asyncio
import os
import sys
import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import settings
from src.db import Database


async def main():
    await Database.connect()

    print("==================================================================")
    print("📊 ProAffiliaters Sources & Leads Inspection")
    print("==================================================================")

    # 1. Check MongoDB stored leads
    leads_col = Database.get_leads_collection()
    if leads_col is not None:
        total_leads = await leads_col.count_documents({})
        bots = await leads_col.count_documents({"lead_type": "bot"})
        users = await leads_col.count_documents({"lead_type": "user"})
        print(f"\n📦 MongoDB Stored Leads Collection ('leads'):")
        print(f"   • Total Unique Leads Extracted: {total_leads}")
        print(f"   • Human Channel Owners (@user): {users}")
        print(f"   • Promotion / Deal Bots (@bot):  {bots}")
        cursor = leads_col.find({})
        stored_leads = await cursor.to_list(100)
        if stored_leads:
            print("\n   📋 Currently Stored Leads in Database:")
            for idx, l in enumerate(stored_leads, 1):
                u = l.get('username')
                t = l.get('lead_type')
                stg = l.get('promo_stage', 1)
                src = l.get('channel_username') or l.get('channel_title') or 'N/A'
                print(f"      {idx}. {u} [{t.upper()}] - Funnel Stage {stg} | Extracted from: {src}")
    else:
        print(f"\n📦 MongoDB not connected (using in-memory fallback): {len(Database._fallback_leads)} sample leads.")

    # 2. Check ProAffiliaters Web App External Sources API
    print(f"\n🌐 Querying Web App API: {settings.proaffiliaters_api_url}")
    print(f"   API Key: {settings.proaffiliaters_api_key}")

    headers = {
        "x-api-key": settings.proaffiliaters_api_key,
        "Accept": "application/json",
        "User-Agent": "ProAffiliaters-LeadTool/1.0"
    }

    windows = [
        (30, "Last 30 Minutes (Default Polling Cycle)"),
        (1440, "Last 24 Hours"),
        (10080, "Last 7 Days (1 Week)"),
        (43200, "Last 30 Days (1 Month)"),
        (525600, "All-Time / Last 1 Year")
    ]

    async with httpx.AsyncClient(timeout=15.0) as client:
        for mins, label in windows:
            try:
                res = await client.get(settings.proaffiliaters_api_url, headers=headers, params={"minutes": mins})
                if res.status_code == 200:
                    data = res.json()
                    status = data.get("status")
                    count = data.get("count", 0)
                    sources = data.get("sources", [])
                    print(f"\n⏱️ Window: {label} (minutes={mins}):")
                    print(f"   • Status:  {status}")
                    print(f"   • Count:   {count} sources returned")
                    if sources:
                        print(f"   • Sample Sources Found:")
                        for idx, s in enumerate(sources[:5], 1):
                            name = s.get("name") or "Unnamed"
                            link = s.get("inviteLink") or s.get("chatId") or "No link"
                            created = s.get("createdAt") or "N/A"
                            print(f"      {idx}. {name} | Link: {link} | Added: {created}")
                    else:
                        print(f"   • Message: {data.get('message', 'No sources in this window')}")
                else:
                    print(f"\n⏱️ Window: {label}: HTTP {res.status_code} - {res.text[:120]}")
            except Exception as e:
                print(f"\n❌ Error querying window {mins} mins: {e}")

    print("\n==================================================================")


if __name__ == "__main__":
    asyncio.run(main())
