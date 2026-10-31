import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on("console", lambda msg: print(f"[{msg.type}] {msg.text}") if msg.type == "error" else None)
        
        await page.goto("http://localhost:8000")
        await page.wait_for_timeout(2500)
        await page.screenshot(path="sidebar_desktop_leads.png")
        print("Captured sidebar_desktop_leads.png")
        
        # Click Telegram Accounts
        await page.click('button.nav-item[data-tab="tab-accounts"]')
        await page.wait_for_timeout(1000)
        await page.screenshot(path="sidebar_desktop_accounts.png")
        print("Captured sidebar_desktop_accounts.png")
        
        # Click 5-Week Drip Funnel
        await page.click('button.nav-item[data-tab="tab-drip-funnel"]')
        await page.wait_for_timeout(1000)
        await page.screenshot(path="sidebar_desktop_drip.png")
        print("Captured sidebar_desktop_drip.png")
        
        # Click Bot Outreach
        await page.click('button.nav-item[data-tab="tab-bot-outreach"]')
        await page.wait_for_timeout(1000)
        await page.screenshot(path="sidebar_desktop_outreach.png")
        print("Captured sidebar_desktop_outreach.png")
        
        print("Console errors count:", len(errors))
        if errors:
            print("Errors:", errors)
        else:
            print("All page tests passed with 0 console errors!")
            
        await browser.close()

if __name__ == "__main__":
    asyncio.run(run())

