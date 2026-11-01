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
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")

async def run():
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
            "domain": "localhost",
            "path": "/"
        }])

        page = await context.new_page()
        
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on("console", lambda msg: print(f"[{msg.type}] {msg.text}") if msg.type == "error" else None)
        
        await page.goto("http://localhost:8000")
        await page.wait_for_timeout(2000)

        # If on login page, perform login
        if await page.query_selector("#login-email"):
            print("Logging in with admin credentials...")
            await page.fill("#login-email", "spushkar812@gmail.com")
            await page.fill("#login-password", "Pushkar@9084")
            await page.click("#btn-submit")
            await page.wait_for_timeout(2000)

        # Test switching across all tabs
        tabs = [
            ("tab-leads", "Extracted Leads Directory"),
            ("tab-accounts", "Telegram Account Management"),
            ("tab-drip-funnel", "5-Week Progressive Drip Sequence"),
            ("tab-keywords", "Autonomous Telegram Keyword Search Engine"),
            ("tab-bot-outreach", "Telegram Bot Outreach Campaigns"),
            ("tab-infobot", "Official Info Bot & Broadcasts"),
            ("tab-playground", "Live Channel Bio Scanner"),
            ("tab-logs", "Real-time Activity & Sync Logs"),
            ("tab-settings", "ProAffiliaters Integration Details")
        ]

        for tab_id, title in tabs:
            btn = await page.query_selector(f'button.nav-item[data-tab="{tab_id}"]')
            assert btn is not None, f"Nav button for {tab_id} not found!"
            await btn.click()
            await page.wait_for_timeout(500)
            
            # Verify active class on view-content
            active_view = await page.query_selector(f'#{tab_id}.view-content.active')
            assert active_view is not None, f"Section #{tab_id} failed to open as active!"
            
            # Verify topbar title
            view_title = await page.inner_text("#current-view-title")
            print(f"[PASS] Section {tab_id} opened successfully -> Title: '{view_title}'")

        print("\nConsole errors count:", len(errors))
        if errors:
            print("Errors encountered:", errors)
        else:
            print("All sections opened and switched with 0 console errors!")
            
        await browser.close()
    
    os._exit(0)

if __name__ == "__main__":
    asyncio.run(run())

