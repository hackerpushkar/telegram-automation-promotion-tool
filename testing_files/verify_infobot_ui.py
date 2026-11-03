import os
import sys
import socket
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import threading
import uvicorn
from playwright.async_api import async_playwright
from src.auth import create_session_token, SESSION_COOKIE_NAME
from src.web import app

def get_free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port

SERVER_PORT = get_free_port()

def start_server():
    uvicorn.run(app, host="127.0.0.1", port=SERVER_PORT, log_level="warning")

async def run():
    print("==================================================================")
    print(f"[UI TEST] Verifying Info Bot Section on Port {SERVER_PORT}")
    print("==================================================================")
    sys.stdout.flush()

    t = threading.Thread(target=start_server, daemon=True)
    t.start()
    await asyncio.sleep(2)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 960})

        token = create_session_token("spushkar812@gmail.com")
        await context.add_cookies([{
            "name": SESSION_COOKIE_NAME,
            "value": token,
            "url": f"http://127.0.0.1:{SERVER_PORT}"
        }])

        page = await context.new_page()

        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on("console", lambda msg: errors.append(f"[{msg.type}] {msg.text}") if msg.type == "error" else None)

        await page.goto(f"http://127.0.0.1:{SERVER_PORT}")
        await page.wait_for_timeout(2000)

        # 1. Verify nav button exists and click it
        btn = await page.query_selector("#nav-btn-infobot")
        assert btn is not None, "Nav button #nav-btn-infobot not found in sidebar!"
        print("[PASS] 1. Info Bot button found in sidebar navigation.")

        await btn.click()
        await page.wait_for_timeout(1000)

        # 2. Verify tab is active and visible
        tab_el = await page.query_selector("#tab-infobot")
        assert tab_el is not None, "#tab-infobot container not found!"
        is_hidden = await tab_el.evaluate("el => el.classList.contains('hidden')")
        assert not is_hidden, "#tab-infobot is unexpectedly hidden!"
        print("[PASS] 2. Info Bot tab is active and visible.")

        # 3. Check Breadcrumb
        breadcrumb = await page.inner_text("#header-breadcrumb")
        assert "Info Bot" in breadcrumb, f"Breadcrumb mismatch: {breadcrumb}"
        print(f"[PASS] 3. Breadcrumb updated: '{breadcrumb}'")

        # 4. Check 4 KPI metric cards
        total_users = await page.inner_text("#infobot-stat-total")
        active_subscribers = await page.inner_text("#infobot-stat-active")
        active_24h = await page.inner_text("#infobot-stat-24h")
        broadcasts_count = await page.inner_text("#infobot-stat-broadcasts")
        print(f"[PASS] 4. KPI Metrics loaded: Total Users: {total_users}, Active: {active_subscribers}, 24h: {active_24h}, Broadcasts: {broadcasts_count}")

        # 5. Test Live Broadcast Composer and Telegram Chat Bubble Preview
        test_message = "🔥 *VIP Deal Alert:* Amazon Freedom Sale is LIVE! Grab 80% OFF now!"
        await page.fill("#infobot-broadcast-text", test_message)
        await page.fill("#infobot-broadcast-btn-text", "🚀 Shop Exclusive Deals")
        await page.fill("#infobot-broadcast-btn-url", "https://app.proaffiliaters.com/deals")
        await page.wait_for_timeout(500)

        preview_text = await page.inner_text("#infobot-preview-text")
        assert "VIP Deal Alert" in preview_text, "Preview text was not updated!"
        btn_label = await page.inner_text("#infobot-preview-btn-label")
        assert "Shop Exclusive Deals" in btn_label, "Button preview text was not updated!"
        print("[PASS] 5. Live Telegram chat bubble mockup updated in real-time!")

        # 6. Verify Bot Subscribers Table exists
        tbody_text = await page.inner_text("#infobot-users-table-body")
        assert tbody_text is not None
        print(f"[PASS] 6. Subscribers table rendered: {tbody_text.strip()[:60]}...")

        # 7. Take Screenshot
        screenshot_path = "C:/Users/spush/.gemini/antigravity/brain/c5e4f9e4-488f-452b-8b71-91441ca1f46c/infobot_section_verified.png"
        await page.screenshot(path=screenshot_path, full_page=True)
        print(f"[PASS] 7. Screenshot saved to {screenshot_path}")

        assert len(errors) == 0, f"Found browser console/page errors: {errors}"
        print(f"\nSUCCESS: Info Bot section verified with 0 console errors!")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())

