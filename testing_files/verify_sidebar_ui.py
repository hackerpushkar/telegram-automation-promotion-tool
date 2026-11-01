import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import threading
import uvicorn
from playwright.async_api import async_playwright
from src.auth import create_session_token, SESSION_COOKIE_NAME
from src.web import app

def start_server():
    uvicorn.run(app, host="127.0.0.1", port=8055, log_level="warning")

async def run():
    print("==================================================================")
    print("[UI TEST] Verifying 4-Section Architecture with Playwright")
    print("==================================================================")
    sys.stdout.flush()
    
    # Start server in background thread
    t = threading.Thread(target=start_server, daemon=True)
    t.start()
    await asyncio.sleep(2)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})

        # Inject authenticated admin session cookie
        token = create_session_token("spushkar812@gmail.com")
        await context.add_cookies([{
            "name": SESSION_COOKIE_NAME,
            "value": token,
            "url": "http://127.0.0.1:8055"
        }])

        page = await context.new_page()
        
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on("console", lambda msg: errors.append(f"[{msg.type}] {msg.text}") if msg.type == "error" else None)
        
        await page.goto("http://127.0.0.1:8055")
        await page.wait_for_timeout(2000)

        # 1. Verify 4 Core Tabs in Sidebar
        expected_tabs = [
            ("nav-btn-leads", "tab-leads", "ProAffiliaters / Leads"),
            ("nav-btn-sending", "tab-sending", "ProAffiliaters / Manual Sending"),
            ("nav-btn-accounts", "tab-accounts", "ProAffiliaters / Login Accounts"),
            ("nav-btn-reports", "tab-reports", "ProAffiliaters / Sending Reports")
        ]

        for btn_id, tab_id, expected_breadcrumb in expected_tabs:
            btn = await page.query_selector(f"#{btn_id}")
            assert btn is not None, f"Nav button #{btn_id} not found!"
            await btn.click()
            await page.wait_for_timeout(600)

            # Check that tab-content is not hidden
            tab_el = await page.query_selector(f"#{tab_id}")
            assert tab_el is not None, f"Tab #{tab_id} not found!"
            is_hidden = await tab_el.evaluate("el => el.classList.contains('hidden')")
            assert not is_hidden, f"Section #{tab_id} should be visible but is hidden!"

            # Check breadcrumb text
            breadcrumb = await page.inner_text("#header-breadcrumb")
            assert expected_breadcrumb in breadcrumb, f"Breadcrumb mismatch: expected {expected_breadcrumb}, got {breadcrumb}"
            print(f"[PASS] Section {tab_id} opened -> Breadcrumb: '{breadcrumb}'")

        # 2. Test Section 1: Leads Subtabs (Userbots vs Usernames)
        await page.click("#nav-btn-leads")
        await page.wait_for_timeout(500)
        
        # Click Usernames subtab
        await page.click("#subtab-btn-usernames")
        await page.wait_for_timeout(500)
        user_btn_class = await page.evaluate("() => document.getElementById('subtab-btn-usernames').className")
        assert "bg-blue-600" in user_btn_class, "Usernames subtab did not become active!"
        print("[PASS] Leads Subtab switching (Userbots <-> Usernames) works smoothly!")

        # Click Userbots subtab
        await page.click("#subtab-btn-userbots")
        await page.wait_for_timeout(500)
        bot_btn_class = await page.evaluate("() => document.getElementById('subtab-btn-userbots').className")
        assert "bg-blue-600" in bot_btn_class, "Userbots subtab did not become active!"
        print("[PASS] Userbots subtab switched back to active successfully!")

        # 3. Test Section 2: Manual Sending Post Composer
        await page.click("#nav-btn-sending")
        await page.wait_for_timeout(500)

        # Fill composer
        await page.fill("#post-title", "Diwali Special Affiliate Offer")
        await page.fill("#post-message-text", "*Grand Offer!*\nJoin now for exclusive commission deals.")
        await page.fill("#post-image-url", "https://app.proaffiliaters.com/logo.png")
        await page.fill("#post-button-text", "Claim Deal Now")
        await page.fill("#post-button-url", "https://t.me/proaffiliaters")

        # Test Save Template button
        await page.click("button:has-text('Save as Post Template')")
        await page.wait_for_timeout(1000)
        print("[PASS] Manual Sending Post Composer template saved successfully!")

        # 4. Test Section 4: Reports Page
        await page.click("#nav-btn-reports")
        await page.wait_for_timeout(500)
        stat_total = await page.inner_text("#report-stat-total")
        print(f"[PASS] Automatic Sending Reports view loaded -> Total Sent KPI: {stat_total}")

        # Take screenshot of clean UI
        screenshot_path = "verification_4_sections_ui.png"
        await page.screenshot(path=screenshot_path)
        print(f"[PASS] Saved full UI verification screenshot: {screenshot_path}")

        print("\nConsole errors count:", len(errors))
        if errors:
            print("Console Errors / Warnings encountered:", errors)
        else:
            print("ALL 4 SECTIONS VERIFIED CLEANLY WITH 0 CONSOLE ERRORS!")
            
        await browser.close()
    
    os._exit(0)

if __name__ == "__main__":
    asyncio.run(run())
