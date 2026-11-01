import sys
import os
import asyncio
from httpx import AsyncClient, ASGITransport

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.web import app
from src.auth import create_session_token, SESSION_COOKIE_NAME

async def run_api_tests():
    print("==================================================================")
    print("[API TEST] Testing Full 4-Section Architecture REST APIs")
    print("==================================================================")

    token = create_session_token("spushkar812@gmail.com")
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        client.cookies.set(SESSION_COOKIE_NAME, token)

        # 1. Global Stats
        res = await client.get("/api/stats")
        assert res.status_code == 200, f"Stats failed: {res.status_code}"
        stats = res.json()
        assert "userbots_count" in stats and "usernames_count" in stats
        print(f"[PASS] 1. GET /api/stats -> Userbots: {stats['userbots_count']}, Usernames: {stats['usernames_count']}")

        # 2. Leads: Add, List, Update Status, Delete, Export CSV
        # Add a test userbot
        res = await client.post("/api/leads", json={"username": "@test_deal_bot", "lead_type": "bot", "source": "API Test"})
        assert res.status_code == 200, f"Add lead failed: {res.status_code}"
        print("[PASS] 2A. POST /api/leads -> Successfully created @test_deal_bot (Userbot)")

        # Add a test username
        res = await client.post("/api/leads", json={"username": "@test_owner_admin", "lead_type": "user", "source": "API Test"})
        assert res.status_code == 200
        print("[PASS] 2B. POST /api/leads -> Successfully created @test_owner_admin (Username)")

        # List Userbots
        res = await client.get("/api/leads?lead_type=bot")
        assert res.status_code == 200
        data = res.json()
        assert any(l["username"] == "@test_deal_bot" for l in data["leads"])
        print(f"[PASS] 2C. GET /api/leads?lead_type=bot -> Found {data['count']} userbots")

        # List Usernames
        res = await client.get("/api/leads?lead_type=user")
        assert res.status_code == 200
        data = res.json()
        assert any(l["username"] == "@test_owner_admin" for l in data["leads"])
        print(f"[PASS] 2D. GET /api/leads?lead_type=user -> Found {data['count']} usernames")

        # Update Status
        res = await client.patch("/api/leads/test_deal_bot/status", json={"status": "contacted"})
        assert res.status_code == 200
        print("[PASS] 2E. PATCH /api/leads/test_deal_bot/status -> Updated to 'contacted'")

        # Export Leads CSV
        res = await client.get("/api/leads/export-csv?lead_type=bot")
        assert res.status_code == 200
        assert "text/csv" in res.headers.get("content-type", "")
        assert "@test_deal_bot" in res.text
        print("[PASS] 2F. GET /api/leads/export-csv?lead_type=bot -> CSV generated successfully")

        # Delete Test Leads
        res = await client.delete("/api/leads/test_deal_bot")
        assert res.status_code == 200
        res = await client.delete("/api/leads/test_owner_admin")
        assert res.status_code == 200
        print("[PASS] 2G. DELETE /api/leads/{username} -> Cleaned up test leads")

        # 3. Posts: Create, List, Delete
        res = await client.post("/api/posts", json={
            "title": "Welcome Offer Template",
            "message_text": "Join *ProAffiliaters* today for 50% commission rev-share!",
            "image_url": "https://app.proaffiliaters.com/hero.png",
            "button_text": "Sign Up",
            "button_url": "https://app.proaffiliaters.com"
        })
        assert res.status_code == 200
        post_id = res.json().get("post")
        print(f"[PASS] 3A. POST /api/posts -> Saved template (ID: {post_id})")

        res = await client.get("/api/posts")
        assert res.status_code == 200
        posts = res.json().get("posts", [])
        assert any(p.get("title") == "Welcome Offer Template" for p in posts)
        print(f"[PASS] 3B. GET /api/posts -> Loaded {len(posts)} saved templates")

        if post_id:
            res = await client.delete(f"/api/posts/{post_id}")
            assert res.status_code == 200
            print(f"[PASS] 3C. DELETE /api/posts/{post_id} -> Deleted template successfully")

        # 4. Sending Engine: Start, Status, Stop
        res = await client.get("/api/sending/status")
        assert res.status_code == 200
        st = res.json()
        assert "is_running" in st and "status" in st
        print(f"[PASS] 4A. GET /api/sending/status -> Engine status: '{st['status']}'")

        # Test stop when idle (safe)
        res = await client.post("/api/sending/stop")
        assert res.status_code == 200
        print("[PASS] 4B. POST /api/sending/stop -> Safe signal handled")

        # 5. Accounts: List
        res = await client.get("/api/accounts")
        assert res.status_code == 200
        accs = res.json().get("accounts", [])
        print(f"[PASS] 5A. GET /api/accounts -> Loaded {len(accs)} registered accounts")

        # 6. Reports: Analytics, List, Export CSV
        res = await client.get("/api/reports/analytics")
        assert res.status_code == 200
        analytics = res.json()
        assert "total_sent" in analytics and "sent_today" in analytics
        print(f"[PASS] 6A. GET /api/reports/analytics -> Total Sent: {analytics['total_sent']}, Today: {analytics['sent_today']}")

        res = await client.get("/api/reports?limit=50")
        assert res.status_code == 200
        reports_data = res.json()
        print(f"[PASS] 6B. GET /api/reports -> Retrieved {reports_data['count']} audit logs")

        res = await client.get("/api/reports/export-csv")
        assert res.status_code == 200
        assert "text/csv" in res.headers.get("content-type", "")
        print("[PASS] 6C. GET /api/reports/export-csv -> Delivery audit CSV generated successfully")

    print("\nALL 4-SECTION APIS TESTED AND FUNCTIONING WITH 100% SUCCESS!\n")

if __name__ == "__main__":
    asyncio.run(run_api_tests())
