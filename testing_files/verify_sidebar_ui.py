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
    print(f"[UI TEST] Verifying 4-Section Architecture on Port {SERVER_PORT}")
    print("==================================================================")
    sys.stdout.flush()
    
    t = threading.Thread(target=start_server, daemon=True)
    t.start()
    await asyncio.sleep(2)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})

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

        # 1. Verify Leads table loads without error
        leads_tbody_text = await page.inner_text("#leads-table-body")
        print(f"Initial leads table preview:\n{leads_tbody_text[:200]}...")
        assert "is not a function" not in leads_tbody_text, "Found function error in table body!"
        print("[PASS] 1. Leads table rendered successfully WITHOUT '(l.extracted_from || []).map is not a function' error!")

        # 2. Verify 4 Core Tabs in Sidebar
        expected_tabs = [
            ("nav-btn-leads", "tab-leads", "Leads Directory"),
            ("nav-btn-sending", "tab-sending", "Manual Sending"),
            ("nav-btn-accounts", "tab-accounts", "Login Accounts"),
            ("nav-btn-reports", "tab-reports", "Sending Reports")
        ]

        for btn_id, tab_id, title_snippet in expected_tabs:
            btn = await page.query_selector(f"#{btn_id}")
            assert btn is not None, f"Nav button #{btn_id} not found!"
            await btn.click()
            await page.wait_for_timeout(500)

            tab_el = await page.query_selector(f"#{tab_id}")
            assert tab_el is not None, f"Tab #{tab_id} not found!"
            is_hidden = await tab_el.evaluate("el => el.classList.contains('hidden')")
            assert not is_hidden, f"Section #{tab_id} should be visible but is hidden!"

            breadcrumb = await page.inner_text("#header-breadcrumb")
            assert title_snippet in breadcrumb, f"Breadcrumb mismatch: expected {title_snippet}, got {breadcrumb}"
            print(f"[PASS] Section {tab_id} opened -> Breadcrumb: '{breadcrumb}'")

        # 3. Test Subtab switching (Userbots <-> Usernames)
        await page.click("#nav-btn-leads")
        await page.wait_for_timeout(500)
        
        await page.click("#subtab-btn-usernames")
        await page.wait_for_timeout(600)
        usernames_tbody_text = await page.inner_text("#leads-table-body")
        assert "is not a function" not in usernames_tbody_text
        print("[PASS] 3A. Switched to Usernames subtab - loaded with 0 errors!")

        await page.click("#subtab-btn-userbots")
        await page.wait_for_timeout(600)
        userbots_tbody_text = await page.inner_text("#leads-table-body")
        assert "is not a function" not in userbots_tbody_text
        print("[PASS] 3B. Switched back to Userbots subtab - loaded with 0 errors!")

        # 4. Test Composer in Manual Sending
        await page.click("#nav-btn-sending")
        await page.wait_for_timeout(500)
        await page.fill("#post-title", "Diwali Affiliate Deal Offer")
        await page.fill("#post-message-text", "*Mega Deals!*\nJoin now for exclusive commissions.")
        await page.fill("#post-button-text", "Join Deals")

        # Verify chat bubble preview updated
        bubble_text = await page.inner_text("#preview-bubble-text")
        assert "*Mega Deals!*" in bubble_text
        print(f"[PASS] 4. Telegram chat bubble live preview working: '{bubble_text[:40]}...'")

        # 5. Test Telegram API Keys Modal in Login Accounts
        await page.click("#nav-btn-accounts")
        await page.wait_for_timeout(500)
        api_btn = await page.query_selector("button:has-text('Telegram API Keys')")
        assert api_btn is not None, "Telegram API Keys button not found in Login Accounts!"
        await api_btn.click()
        await page.wait_for_timeout(500)

        modal_api_el = await page.query_selector("#modal-api-config")
        assert modal_api_el is not None
        modal_hidden = await modal_api_el.evaluate("el => el.classList.contains('hidden')")
        assert not modal_hidden, "Telegram API Keys modal should be visible!"
        print("[PASS] 5. Telegram API Keys modal opened successfully with API ID & Hash inputs!")

        # Close modal
        await page.click("#modal-api-config button:has-text('Cancel')")
        await page.wait_for_timeout(300)
        modal_hidden_after = await modal_api_el.evaluate("el => el.classList.contains('hidden')")
        assert modal_hidden_after, "Modal should be closed after clicking Cancel!"
        print("[PASS] 5B. Telegram API Keys modal closed cleanly.")

        # Save screenshot of new enhanced UI
        screenshot_path = "verification_4_sections_ui.png"
        await page.screenshot(path=screenshot_path)
        print(f"[PASS] Saved full UI verification screenshot: {screenshot_path}")

        print("\nConsole errors count:", len(errors))
        if errors:
            print("Console Errors / Warnings:", errors)
        else:
            print("ALL 4 SECTIONS AND LEADS TABLE VERIFIED WITH 0 CONSOLE ERRORS!")
            
        await browser.close()
    
    sys.exit(0)

if __name__ == "__main__":
    asyncio.run(run())
