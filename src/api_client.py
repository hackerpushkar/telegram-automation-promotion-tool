import logging
from typing import List, Set, Optional
import httpx
from src.config import settings
from src.models import SourceChannel, SourceSyncResponse

logger = logging.getLogger(__name__)


class ProaffiliatersApiClient:
    def __init__(self):
        self.api_url = settings.proaffiliaters_api_url
        self.api_key = settings.proaffiliaters_api_key
        self.interval_minutes = settings.sync_interval_minutes
        self._processed_sources: Set[str] = set()

    async def fetch_recent_sources(self) -> List[SourceChannel]:
        """
        Polls the ProAffiliaters External Sources API for newly registered deal sources.
        
        Returns:
            List[SourceChannel]: Unprocessed new source channels.
        """
        headers = {
            "x-api-key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "ProAffiliaters-LeadTool/1.0"
        }
        params = {
            "minutes": self.interval_minutes
        }

        logger.info(
            "[SourceSync] Polling external API: %s (Window: %d mins)",
            self.api_url,
            self.interval_minutes
        )

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(self.api_url, headers=headers, params=params)

                if response.status_code != 200:
                    logger.error(
                        "[SourceSync] API returned non-200 status %d: %s",
                        response.status_code,
                        response.text[:200]
                    )
                    return []

                raw_data = response.json()
                parsed = SourceSyncResponse(**raw_data)

                if parsed.status == "none" or not parsed.sources:
                    logger.info(
                        "[SourceSync] No new sources in the last %d minutes (Server message: %s)",
                        parsed.windowMinutes or self.interval_minutes,
                        parsed.message or "None"
                    )
                    return []

                # Filter new sources against in-memory processed cache
                new_unprocessed: List[SourceChannel] = []
                for src in parsed.sources:
                    cache_key = src.id or src.inviteLink or src.chatId
                    if cache_key and cache_key in self._processed_sources:
                        logger.debug("[SourceSync] Skipping already processed source: %s", cache_key)
                        continue

                    new_unprocessed.append(src)
                    if cache_key:
                        self._processed_sources.add(cache_key)

                logger.info(
                    "[SourceSync] Received %d source(s) from API; %d are new and ready to scan.",
                    len(parsed.sources),
                    len(new_unprocessed)
                )
                return new_unprocessed

        except httpx.TimeoutException:
            logger.warning("[SourceSync] Network timeout after 15s contacting ProAffiliaters API. Will retry next cycle.")
            return []
        except httpx.RequestError as exc:
            logger.error("[SourceSync] Network connection error contacting API: %s", exc)
            return []
        except Exception as exc:
            logger.exception("[SourceSync] Unexpected error during source sync: %s", exc)
            return []


# Standalone runner for testing the API connection
if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    async def _test():
        client = ProaffiliatersApiClient()
        sources = await client.fetch_recent_sources()
        print(f"\n[Test Result] Found {len(sources)} new sources to process:")
        for s in sources:
            print(f" - {s.name} ({s.platform}): {s.inviteLink}")

    asyncio.run(_test())
