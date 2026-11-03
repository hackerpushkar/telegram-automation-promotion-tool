import os
import sys

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import asyncio
import logging
import signal
from datetime import datetime
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.config import settings
from src.db import Database
from src.api_client import ProaffiliatersApiClient
from src.scanner import TelegramBioScanner
from src.info_bot import info_bot
from src.bot_forwarder import bot_forwarder
from src.drip_engine import DripEngine
from src.keyword_search_engine import KeywordSearchEngine
from src.web import app, log_activity

# Setup structured logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("TelegramPromotionTool")


class Orchestrator:
    def __init__(self):
        self.api_client = ProaffiliatersApiClient()
        self.scanner = TelegramBioScanner()
        self.scheduler = AsyncIOScheduler()
        self.is_running = False

    async def run_sync_cycle(self):
        """
        Executes one full sync & lead extraction cycle:
        1. Fetches recent sources from ProAffiliaters Web App API (last 30m window).
        2. If none, exits gracefully.
        3. If sources found, scans each channel's bio/description using Telegram MTProto.
        4. Extracts direct owner handles and bots.
        5. Saves leads directly into MongoDB (Sources are NOT stored).
        """
        start_time = datetime.utcnow()
        logger.info("==================================================================")
        logger.info("🔄 Starting 30-Minute Source Sync & Lead Extraction Cycle...")
        logger.info("==================================================================")
        log_activity("Starting scheduled 30-minute source sync cycle...", "sync")

        try:
            # Step 1: Poll External API
            sources = await self.api_client.fetch_recent_sources()

            if not sources:
                logger.info("[SyncCycle] No new sources to process this cycle. Sleeping until next schedule.")
                log_activity("No new sources found in the last 30 minutes from ProAffiliaters API.", "info")
                return

            total_new_leads = 0
            total_updated_leads = 0

            # Step 2: Iterate over each source
            for idx, source in enumerate(sources, 1):
                logger.info(
                    "[SyncCycle] [%d/%d] Processing Source: '%s' (Platform: %s | Link: %s)",
                    idx, len(sources), source.name, source.platform, source.inviteLink
                )
                log_activity(f"Inspecting source [{idx}/{len(sources)}]: {source.name}", "sync")

                if source.platform.lower() != "telegram":
                    logger.info("[SyncCycle] Skipping non-Telegram source: %s", source.platform)
                    continue

                if not source.inviteLink:
                    logger.warning("[SyncCycle] Source '%s' has no inviteLink. Skipping.", source.name)
                    continue

                # Step 3: Scan Channel Bio & Extract Leads
                leads = await self.scanner.scan_source_for_leads(source)

                if not leads:
                    logger.info("[SyncCycle] No leads found in bio of '%s'.", source.name)
                    continue

                # Step 4: Save Leads to MongoDB (Sources are NOT saved to DB)
                for lead in leads:
                    try:
                        is_new, username = await Database.upsert_lead(lead)
                        if is_new:
                            total_new_leads += 1
                            logger.info(
                                "  ✨ [NEW LEAD STORED] @%s | Type: %s | Source: '%s'",
                                username, lead.lead_type.value, source.name
                            )
                            log_activity(f"Discovered new {lead.lead_type.value} lead: @{lead.username} from {source.name}", "lead")
                        else:
                            total_updated_leads += 1
                            logger.info(
                                "  🔁 [EXISTING LEAD UPDATED] @%s | Type: %s",
                                username, lead.lead_type.value
                            )
                    except Exception as exc:
                        logger.error("  ❌ Failed to save lead @%s: %s", lead.username, exc)

            elapsed = (datetime.utcnow() - start_time).total_seconds()
            logger.info("==================================================================")
            logger.info(
                "✅ Cycle Complete in %.2fs. Leads Saved: %d New, %d Updated.",
                elapsed, total_new_leads, total_updated_leads
            )
            logger.info("==================================================================")
            log_activity(f"Cycle finished in {elapsed:.1f}s. Saved: {total_new_leads} new, {total_updated_leads} updated.", "info")

            # Instantly trigger promotional outreach for newly extracted leads
            if total_new_leads > 0:
                logger.info("⚡ [SyncCycle] Extracted %d new lead(s). Triggering automated promotional drip...", total_new_leads)
                asyncio.create_task(DripEngine.run_autonomous_drip_cycle())

        except Exception as exc:
            logger.exception("❌ Error occurred during sync cycle: %s", exc)
            log_activity(f"Sync error: {str(exc)}", "info")

    async def start(self):
        """Starts database, telegram scanner, initial sync, and scheduler."""
        self.is_running = True
        logger.info("🚀 Starting Telegram Promotion Tool...")

        # Connect to MongoDB
        await Database.connect()

        # Connect Telegram MTProto Client
        await self.scanner.start()
        if self.scanner.client:
            bot_forwarder.set_telethon_client(self.scanner.client)
            logger.info("🔗 Bound active Telethon client to BotForwarderEngine.")

        # Connect Official Info Bot (@ProaffiliatersInfoBot)
        await info_bot.start()

        # Schedule recurring job every 30 minutes
        minutes = settings.sync_interval_minutes
        self.scheduler.add_job(
            self.run_sync_cycle,
            trigger="interval",
            minutes=minutes,
            id="source_sync_worker",
            replace_existing=True
        )
        self.scheduler.start()
        logger.info("⏰ APScheduler active: recurring job registered every %d minutes.", minutes)

        # Schedule Autonomous Telegram Keyword Search job
        if settings.keyword_search_enabled:
            kw_minutes = settings.keyword_search_interval_minutes
            self.scheduler.add_job(
                KeywordSearchEngine.run_autonomous_cycle,
                trigger="interval",
                minutes=kw_minutes,
                id="keyword_search_worker",
                replace_existing=True
            )
            logger.info("🔍 APScheduler active: Autonomous Keyword Search registered every %d minutes.", kw_minutes)

        # Schedule Autonomous 5-Week Drip Outreach job (runs every 15 minutes)
        self.scheduler.add_job(
            DripEngine.run_autonomous_drip_cycle,
            trigger="interval",
            minutes=15,
            id="autonomous_drip_worker",
            replace_existing=True
        )
        logger.info("🤖 APScheduler active: Autonomous 5-Week Drip Outreach registered every 15 minutes.")

        # Trigger one initial cycle in background
        asyncio.create_task(self.run_sync_cycle())

        if settings.keyword_search_enabled:
            async def initial_auto_keyword_cycle():
                # Allow 5 seconds for Telegram client connection stabilization
                await asyncio.sleep(5.0)
                try:
                    logger.info("🚀 [AutoStartup] Launching initial autonomous keyword discovery cycle...")
                    await KeywordSearchEngine.run_autonomous_cycle(max_keywords=2)
                except Exception as e:
                    logger.error("[AutoStartup] Error during initial keyword cycle: %s", e)

            asyncio.create_task(initial_auto_keyword_cycle())

        async def initial_auto_drip_cycle():
            # Wait 10 seconds for clients to connect, then process uncontacted userbots
            await asyncio.sleep(10.0)
            try:
                logger.info("🚀 [AutoStartup] Checking for uncontacted userbots to dispatch Promo #1...")
                await DripEngine.run_autonomous_drip_cycle()
            except Exception as e:
                logger.error("[AutoStartup] Error during initial promotional drip check: %s", e)

        asyncio.create_task(initial_auto_drip_cycle())

    async def stop(self):
        """Clean shutdown of scheduler, scanner, info bot, forwarder, and database."""
        if not self.is_running:
            return
        logger.info("🛑 Shutting down Telegram Promotion Tool...")
        self.is_running = False

        bot_forwarder.stop_campaign()

        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

        await info_bot.stop()
        await self.scanner.stop()
        await Database.close()
        logger.info("👋 Shutdown complete.")


async def main():
    orchestrator = Orchestrator()
    await orchestrator.start()

    # Configure Uvicorn server
    config = uvicorn.Config(
        app=app,
        host=settings.web_host,
        port=settings.web_port,
        log_level=settings.log_level.lower(),
        access_log=False
    )
    server = uvicorn.Server(config)

    logger.info("==================================================================")
    logger.info("🌐 ProAffiliaters Telegram Tool Dashboard (Pure Light Mode)")
    logger.info("👉 Open your browser at: http://localhost:%d", settings.web_port)
    logger.info("==================================================================")

    # Automatically launch browser window for instant access
    def _open_dashboard_browser():
        import time
        import webbrowser
        time.sleep(1.2)
        try:
            webbrowser.open(f"http://localhost:{settings.web_port}")
        except Exception:
            pass

    import threading
    threading.Thread(target=_open_dashboard_browser, daemon=True).start()

    try:
        await server.serve()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Exiting application...")
    finally:
        await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())
