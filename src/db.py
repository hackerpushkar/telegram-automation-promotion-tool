import logging
import asyncio
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase, AsyncIOMotorCollection
from pymongo.errors import ServerSelectionTimeoutError
from src.config import settings
from src.models import Lead, LeadType, LeadStatus, LeadSourceInfo

logger = logging.getLogger(__name__)


class Database:
    client: Optional[AsyncIOMotorClient] = None
    db: Optional[AsyncIOMotorDatabase] = None
    is_connected: bool = False
    _fallback_leads: Dict[str, Dict[str, Any]] = {}
    _fallback_info_users: Dict[str, Dict[str, Any]] = {}
    _fallback_info_broadcasts: List[Dict[str, Any]] = []
    _fallback_published_posts: Dict[int, Dict[str, Any]] = {}
    _fallback_bot_campaigns: Dict[str, Dict[str, Any]] = {}
    _fallback_telegram_accounts: Dict[str, Dict[str, Any]] = {}
    _fallback_keywords: Dict[str, Dict[str, Any]] = {}
    _fallback_discovered_channels: Dict[int, Dict[str, Any]] = {}
    _fallback_promotional_posts: Dict[str, Dict[str, Any]] = {}
    _fallback_sending_reports: List[Dict[str, Any]] = []

    @classmethod
    async def connect(cls):
        """Establish MongoDB connection and create required indexes. Gracefully falls back if unavailable."""
        if cls.client is None:
            try:
                logger.info("Connecting to MongoDB: %s", settings.mongodb_uri)
                # Short serverSelectionTimeoutMS so we fail-fast to fallback if no Mongo daemon is up
                cls.client = AsyncIOMotorClient(settings.mongodb_uri, serverSelectionTimeoutMS=2000)
                # Test connection
                await cls.client.admin.command('ping')
                cls.db = cls.client[settings.mongodb_db_name]
                await cls._ensure_indexes()
                cls.is_connected = True
                logger.info("MongoDB connected successfully. Database: '%s'", settings.mongodb_db_name)
            except Exception as exc:
                cls.is_connected = False
                logger.warning("MongoDB not accessible (%s). Operating in-memory mode.", exc)
                cls._init_fallback_samples()

    @classmethod
    def _init_fallback_samples(cls):
        """Seed sample leads into memory if MongoDB is unavailable so the UI is immediately interactive."""
        if not cls._fallback_leads:
            now = datetime.utcnow()
            samples = [
                {
                    "username": "DealsManagerPro",
                    "username_clean": "dealsmanagerpro",
                    "lead_type": "user",
                    "status": "new",
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "extracted_from": {
                        "source_name": "Loot Deals Prime",
                        "source_invite_link": "https://t.me/+AbCdEf_TestHash999",
                        "platform": "telegram",
                        "source_id": "src_loot_001"
                    },
                    "bio_context": "Official deal alerts channel. For sponsorships and affiliate posts DM owner @DealsManagerPro or submit deals to @AutoPostDealsBot."
                },
                {
                    "username": "AutoPostDealsBot",
                    "username_clean": "autopostdealsbot",
                    "lead_type": "bot",
                    "status": "contacted",
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "extracted_from": {
                        "source_name": "Loot Deals Prime",
                        "source_invite_link": "https://t.me/+AbCdEf_TestHash999",
                        "platform": "telegram",
                        "source_id": "src_loot_001"
                    },
                    "bio_context": "Official deal alerts channel. For sponsorships and affiliate posts DM owner @DealsManagerPro or submit deals to @AutoPostDealsBot."
                },
                {
                    "username": "AmazonOffersAdmin",
                    "username_clean": "amazonoffersadmin",
                    "lead_type": "user",
                    "status": "converted",
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "extracted_from": {
                        "source_name": "Amazon Flash Deals Hub",
                        "source_invite_link": "https://t.me/flashdealshub",
                        "platform": "telegram",
                        "source_id": "src_amz_002"
                    },
                    "bio_context": "Handpicked verified discount coupons. Promotion inquiries: @AmazonOffersAdmin | Bot: @FlashSubmitBot"
                },
                {
                    "username": "FlashSubmitBot",
                    "username_clean": "flashsubmitbot",
                    "lead_type": "bot",
                    "status": "new",
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "extracted_from": {
                        "source_name": "Amazon Flash Deals Hub",
                        "source_invite_link": "https://t.me/flashdealshub",
                        "platform": "telegram",
                        "source_id": "src_amz_002"
                    },
                    "bio_context": "Handpicked verified discount coupons. Promotion inquiries: @AmazonOffersAdmin | Bot: @FlashSubmitBot"
                },
                {
                    "username": "AffiliateMaster_VIP",
                    "username_clean": "affiliatemaster_vip",
                    "lead_type": "user",
                    "status": "new",
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "extracted_from": {
                        "source_name": "Tech Gadget Giveaways",
                        "source_invite_link": "https://t.me/+TechGadgetOfficial",
                        "platform": "telegram",
                        "source_id": "src_tech_003"
                    },
                    "bio_context": "Daily unboxings & cashback vouchers. Contact founder @AffiliateMaster_VIP for campaign bookings."
                }
            ]
            for s in samples:
                cls._fallback_leads[s["username_clean"]] = s

    @classmethod
    async def close(cls):
        """Close MongoDB connection pool."""
        if cls.client:
            cls.client.close()
            cls.client = None
            cls.db = None
            cls.is_connected = False
            logger.info("MongoDB connection closed.")

    @classmethod
    def get_leads_collection(cls) -> Optional[AsyncIOMotorCollection]:
        """Returns the leads collection if connected."""
        if cls.is_connected and cls.db is not None:
            return cls.db[settings.mongodb_leads_collection]
        return None

    @classmethod
    def get_info_bot_users_collection(cls) -> Optional[AsyncIOMotorCollection]:
        """Returns the info_bot_users collection if connected."""
        if cls.is_connected and cls.db is not None:
            return cls.db[settings.mongodb_info_bot_users_collection]
        return None

    @classmethod
    def get_info_bot_broadcasts_collection(cls) -> Optional[AsyncIOMotorCollection]:
        """Returns the info_bot_broadcasts collection if connected."""
        if cls.is_connected and cls.db is not None:
            return cls.db[settings.mongodb_info_bot_broadcasts_collection]
        return None

    @classmethod
    def get_published_posts_collection(cls) -> Optional[AsyncIOMotorCollection]:
        """Returns the published_posts collection if connected."""
        if cls.is_connected and cls.db is not None:
            return cls.db[settings.mongodb_published_posts_collection]
        return None

    @classmethod
    def get_bot_campaigns_collection(cls) -> Optional[AsyncIOMotorCollection]:
        """Returns the bot_campaigns collection if connected."""
        if cls.is_connected and cls.db is not None:
            return cls.db[settings.mongodb_bot_campaigns_collection]
        return None

    @classmethod
    def get_telegram_accounts_collection(cls) -> Optional[AsyncIOMotorCollection]:
        """Returns the telegram_accounts collection if connected."""
        if cls.is_connected and cls.db is not None:
            return cls.db[settings.mongodb_telegram_accounts_collection]
        return None

    @classmethod
    def get_search_keywords_collection(cls) -> Optional[AsyncIOMotorCollection]:
        """Returns the search_keywords collection if connected."""
        if cls.is_connected and cls.db is not None:
            return cls.db[settings.mongodb_search_keywords_collection]
        return None

    @classmethod
    def get_discovered_channels_collection(cls) -> Optional[AsyncIOMotorCollection]:
        """Returns the discovered_channels collection if connected."""
        if cls.is_connected and cls.db is not None:
            return cls.db[settings.mongodb_discovered_channels_collection]
        return None

    @classmethod
    def get_promotional_posts_collection(cls) -> Optional[AsyncIOMotorCollection]:
        """Returns the promotional_posts collection if connected."""
        if cls.is_connected and cls.db is not None:
            return cls.db[settings.mongodb_promotional_posts_collection]
        return None

    @classmethod
    def get_sending_reports_collection(cls) -> Optional[AsyncIOMotorCollection]:
        """Returns the sending_reports collection if connected."""
        if cls.is_connected and cls.db is not None:
            return cls.db[settings.mongodb_sending_reports_collection]
        return None

    @classmethod
    async def _ensure_indexes(cls):
        """Ensures required indexes on all collections."""
        leads_col = cls.get_leads_collection()
        if leads_col is not None:
            await leads_col.create_index("username_clean", unique=True)
            await leads_col.create_index("lead_type")
            await leads_col.create_index("status")
            await leads_col.create_index("first_seen_at")
            logger.info("Indexes verified on '%s' collection.", settings.mongodb_leads_collection)

        info_users_col = cls.get_info_bot_users_collection()
        if info_users_col is not None:
            await info_users_col.create_index("telegram_chat_id", unique=True)
            await info_users_col.create_index("joined_at")
            await info_users_col.create_index("start_payload")
            await info_users_col.create_index("is_blocked")
            logger.info("Indexes verified on '%s' collection.", settings.mongodb_info_bot_users_collection)

        info_broadcasts_col = cls.get_info_bot_broadcasts_collection()
        if info_broadcasts_col is not None:
            await info_broadcasts_col.create_index("created_at")
            logger.info("Indexes verified on '%s' collection.", settings.mongodb_info_bot_broadcasts_collection)

        pub_posts_col = cls.get_published_posts_collection()
        if pub_posts_col is not None:
            await pub_posts_col.create_index("post_number", unique=True)
            logger.info("Indexes verified on '%s' collection.", settings.mongodb_published_posts_collection)

        campaigns_col = cls.get_bot_campaigns_collection()
        if campaigns_col is not None:
            await campaigns_col.create_index("created_at")
            await campaigns_col.create_index("status")
            logger.info("Indexes verified on '%s' collection.", settings.mongodb_bot_campaigns_collection)

        accounts_col = cls.get_telegram_accounts_collection()
        if accounts_col is not None:
            await accounts_col.create_index("phone", unique=True)
            await accounts_col.create_index("assigned_slot")
            await accounts_col.create_index("status")
            logger.info("Indexes verified on '%s' collection.", settings.mongodb_telegram_accounts_collection)

        # Index drip progression fields on leads
        if leads_col is not None:
            await leads_col.create_index("promo_stage")
            await leads_col.create_index("next_eligible_at")

        # Autonomous Keyword Search Indexes
        keywords_col = cls.get_search_keywords_collection()
        if keywords_col is not None:
            await keywords_col.create_index("keyword", unique=True)
            await keywords_col.create_index("enabled")
            await keywords_col.create_index("created_at")
            logger.info("Indexes verified on '%s' collection.", settings.mongodb_search_keywords_collection)
            await cls._seed_default_keywords()

        channels_col = cls.get_discovered_channels_collection()
        if channels_col is not None:
            await channels_col.create_index("channel_id", unique=True)
            await channels_col.create_index("username")
            await channels_col.create_index("matching_keyword")
            await channels_col.create_index("discovered_at")
            logger.info("Indexes verified on '%s' collection.", settings.mongodb_discovered_channels_collection)

        # Promotional Posts & Sending Reports Indexes
        posts_col = cls.get_promotional_posts_collection()
        if posts_col is not None:
            await posts_col.create_index("created_at")
            logger.info("Indexes verified on '%s' collection.", settings.mongodb_promotional_posts_collection)

        reports_col = cls.get_sending_reports_collection()
        if reports_col is not None:
            await reports_col.create_index("sent_at")
            await reports_col.create_index("recipient_username")
            await reports_col.create_index("recipient_type")
            await reports_col.create_index("status")
            await reports_col.create_index("sender_account_phone")
            logger.info("Indexes verified on '%s' collection.", settings.mongodb_sending_reports_collection)

    @classmethod
    async def _seed_default_keywords(cls):
        """Seeds standard deal and affiliate keywords if none exist."""
        keywords_col = cls.get_search_keywords_collection()
        if keywords_col is not None:
            count = await keywords_col.count_documents({})
            if count == 0:
                defaults = [
                    {"keyword": "loot deals", "category": "deals"},
                    {"keyword": "amazon deals", "category": "amazon"},
                    {"keyword": "flipkart deals", "category": "flipkart"},
                    {"keyword": "shopping offers", "category": "deals"},
                    {"keyword": "discount coupons", "category": "coupons"},
                    {"keyword": "telegram deals", "category": "deals"},
                    {"keyword": "loot offers", "category": "deals"},
                    {"keyword": "free loot", "category": "loot"},
                    {"keyword": "affiliate deals", "category": "affiliate"},
                ]
                now = datetime.utcnow()
                for d in defaults:
                    doc = {
                        "keyword": d["keyword"],
                        "category": d["category"],
                        "enabled": True,
                        "total_channels_found": 0,
                        "total_leads_extracted": 0,
                        "last_searched_at": None,
                        "created_at": now
                    }
                    try:
                        await keywords_col.insert_one(doc)
                    except Exception:
                        pass
                logger.info("Pre-seeded %d default target search keywords.", len(defaults))

    @classmethod
    async def upsert_lead(cls, lead: Lead) -> Tuple[bool, str]:
        """
        Upsert a lead into MongoDB or fallback store.
        Sources are NOT stored in MongoDB; only extracted leads.
        """
        now = datetime.utcnow()
        username_clean = lead.username_clean
        leads_col = cls.get_leads_collection()

        if leads_col is not None:
            try:
                lead_doc = lead.model_dump()
                result = await leads_col.update_one(
                    {"username_clean": username_clean},
                    {
                        "$setOnInsert": {
                            "username": lead.username,
                            "username_clean": username_clean,
                            "lead_type": lead.lead_type.value,
                            "first_seen_at": now,
                            "status": lead.status.value,
                            "extracted_from": lead_doc["extracted_from"]
                        },
                        "$set": {
                            "last_seen_at": now,
                            "bio_context": lead.bio_context
                        },
                        "$addToSet": {
                            "occurrences": {
                                "source_name": lead.extracted_from.source_name,
                                "source_invite_link": lead.extracted_from.source_invite_link,
                                "seen_at": now
                            }
                        }
                    },
                    upsert=True
                )
                is_new = (result.upserted_id is not None)
                return is_new, lead.username
            except Exception as e:
                logger.error("MongoDB upsert error: %s. Using memory fallback.", e)

        # Fallback in-memory
        is_new = username_clean not in cls._fallback_leads
        if is_new:
            cls._fallback_leads[username_clean] = {
                "username": lead.username,
                "username_clean": username_clean,
                "lead_type": lead.lead_type.value,
                "first_seen_at": now,
                "last_seen_at": now,
                "status": lead.status.value,
                "extracted_from": lead.extracted_from.model_dump(),
                "bio_context": lead.bio_context
            }
        else:
            cls._fallback_leads[username_clean]["last_seen_at"] = now
            cls._fallback_leads[username_clean]["bio_context"] = lead.bio_context

        return is_new, lead.username

    @classmethod
    async def get_leads(
        cls,
        search: Optional[str] = None,
        lead_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        skip: int = 0
    ) -> List[Dict[str, Any]]:
        """Fetch filtered leads with pagination."""
        leads_col = cls.get_leads_collection()
        if leads_col is not None:
            try:
                query: Dict[str, Any] = {}
                if lead_type and lead_type != "all":
                    query["lead_type"] = lead_type
                if status and status != "all":
                    query["status"] = status
                if search:
                    regex = {"$regex": search, "$options": "i"}
                    query["$or"] = [
                        {"username": regex},
                        {"bio_context": regex},
                        {"extracted_from.source_name": regex}
                    ]

                cursor = leads_col.find(query, {"_id": 0}).sort("first_seen_at", -1).skip(skip).limit(limit)
                return await cursor.to_list(length=limit)
            except Exception as e:
                logger.error("Error fetching leads from MongoDB: %s", e)

        # Fallback memory search
        results = list(cls._fallback_leads.values())
        if lead_type and lead_type != "all":
            results = [l for l in results if l.get("lead_type") == lead_type]
        if status and status != "all":
            results = [l for l in results if l.get("status") == status]
        if search:
            s_low = search.lower()
            results = [
                l for l in results
                if s_low in l.get("username", "").lower()
                or s_low in l.get("bio_context", "").lower()
                or s_low in l.get("extracted_from", {}).get("source_name", "").lower()
            ]

        results.sort(key=lambda x: str(x.get("first_seen_at", "")), reverse=True)
        return results[skip : skip + limit]

    @classmethod
    async def get_stats(cls) -> Dict[str, Any]:
        """Aggregate lead statistics."""
        leads_col = cls.get_leads_collection()
        if leads_col is not None:
            try:
                total = await leads_col.count_documents({})
                total_users = await leads_col.count_documents({"lead_type": "user"})
                total_bots = await leads_col.count_documents({"lead_type": "bot"})
                new_status = await leads_col.count_documents({"status": "new"})
                return {
                    "total_leads": total,
                    "total_users": total_users,
                    "total_bots": total_bots,
                    "new_leads": new_status,
                    "storage_engine": "MongoDB (Connected)"
                }
            except Exception as e:
                logger.error("Error getting stats from MongoDB: %s", e)

        # Fallback
        leads = list(cls._fallback_leads.values())
        return {
            "total_leads": len(leads),
            "total_users": sum(1 for l in leads if l.get("lead_type") == "user"),
            "total_bots": sum(1 for l in leads if l.get("lead_type") == "bot"),
            "new_leads": sum(1 for l in leads if l.get("status") == "new"),
            "storage_engine": "In-Memory Buffer (MongoDB Disconnected)"
        }

    @classmethod
    async def update_status(cls, username_clean: str, new_status: str) -> bool:
        """Update the outreach status of a lead."""
        leads_col = cls.get_leads_collection()
        if leads_col is not None:
            try:
                res = await leads_col.update_one(
                    {"username_clean": username_clean},
                    {"$set": {"status": new_status, "updated_at": datetime.utcnow()}}
                )
                return res.modified_count > 0
            except Exception as e:
                logger.error("Error updating lead status in MongoDB: %s", e)

        # Fallback
        if username_clean in cls._fallback_leads:
            cls._fallback_leads[username_clean]["status"] = new_status
            return True
        return False

    @classmethod
    async def delete_lead(cls, username_clean: str) -> bool:
        """Deletes a lead from MongoDB or fallback store."""
        leads_col = cls.get_leads_collection()
        if leads_col is not None:
            try:
                res = await leads_col.delete_one({"username_clean": username_clean})
                return res.deleted_count > 0
            except Exception as e:
                logger.error("Error deleting lead in MongoDB: %s", e)

        # Fallback
        if username_clean in cls._fallback_leads:
            del cls._fallback_leads[username_clean]
            return True
        return False

    # ==========================================
    # Official Info Bot (@ProaffiliatersInfoBot) DB Operations
    # ==========================================

    @classmethod
    async def upsert_info_bot_user(cls, user_data: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """
        Upserts an Info Bot user upon /start command or activity.
        Captures attribution start_payload and updates last_active_at.
        """
        now = datetime.utcnow()
        chat_id = str(user_data["telegram_chat_id"])
        users_col = cls.get_info_bot_users_collection()

        update_set: Dict[str, Any] = {
            "username": user_data.get("username"),
            "first_name": user_data.get("first_name", ""),
            "last_name": user_data.get("last_name", ""),
            "last_active_at": now,
            "is_blocked": False,
        }
        if user_data.get("start_payload"):
            update_set["start_payload"] = user_data["start_payload"]

        if users_col is not None:
            try:
                res = await users_col.update_one(
                    {"telegram_chat_id": chat_id},
                    {
                        "$set": update_set,
                        "$setOnInsert": {
                            "telegram_chat_id": chat_id,
                            "joined_at": now,
                        }
                    },
                    upsert=True
                )
                is_new = (res.upserted_id is not None)
                user_doc = await users_col.find_one({"telegram_chat_id": chat_id}, {"_id": 0})
                return is_new, user_doc or update_set
            except Exception as e:
                logger.error("MongoDB upsert_info_bot_user error: %s", e)

        # Fallback in-memory
        is_new = chat_id not in cls._fallback_info_users
        if is_new:
            record = {
                "telegram_chat_id": chat_id,
                "username": user_data.get("username"),
                "first_name": user_data.get("first_name", ""),
                "last_name": user_data.get("last_name", ""),
                "start_payload": user_data.get("start_payload"),
                "joined_at": now,
                "last_active_at": now,
                "is_blocked": False,
            }
            cls._fallback_info_users[chat_id] = record
            return True, record
        else:
            cls._fallback_info_users[chat_id].update(update_set)
            return False, cls._fallback_info_users[chat_id]

    @classmethod
    async def set_info_bot_user_blocked(cls, telegram_chat_id: str, is_blocked: bool = True) -> bool:
        """Mark an Info Bot user as blocked to avoid wasted Telegram API requests."""
        chat_id = str(telegram_chat_id)
        users_col = cls.get_info_bot_users_collection()
        if users_col is not None:
            try:
                res = await users_col.update_one(
                    {"telegram_chat_id": chat_id},
                    {"$set": {"is_blocked": is_blocked, "last_active_at": datetime.utcnow()}}
                )
                return res.modified_count > 0
            except Exception as e:
                logger.error("MongoDB set_info_bot_user_blocked error: %s", e)

        if chat_id in cls._fallback_info_users:
            cls._fallback_info_users[chat_id]["is_blocked"] = is_blocked
            return True
        return False

    @classmethod
    async def get_all_active_info_bot_users(cls) -> List[Dict[str, Any]]:
        """Returns all unblocked Info Bot users targeted for broadcasting."""
        users_col = cls.get_info_bot_users_collection()
        if users_col is not None:
            try:
                cursor = users_col.find({"is_blocked": {"$ne": True}}, {"_id": 0, "telegram_chat_id": 1, "username": 1, "first_name": 1})
                return await cursor.to_list(length=10000)
            except Exception as e:
                logger.error("MongoDB get_all_active_info_bot_users error: %s", e)

        return [
            {"telegram_chat_id": u["telegram_chat_id"], "username": u.get("username"), "first_name": u.get("first_name")}
            for u in cls._fallback_info_users.values()
            if not u.get("is_blocked", False)
        ]

    @classmethod
    async def get_info_bot_users(
        cls,
        search: Optional[str] = None,
        is_blocked: Optional[bool] = None,
        limit: int = 50,
        skip: int = 0
    ) -> List[Dict[str, Any]]:
        """Retrieve paginated Info Bot users with search and filter."""
        users_col = cls.get_info_bot_users_collection()
        if users_col is not None:
            try:
                query: Dict[str, Any] = {}
                if is_blocked is not None:
                    query["is_blocked"] = is_blocked
                if search:
                    regex = {"$regex": search, "$options": "i"}
                    query["$or"] = [
                        {"username": regex},
                        {"first_name": regex},
                        {"last_name": regex},
                        {"telegram_chat_id": regex},
                        {"start_payload": regex},
                    ]
                cursor = users_col.find(query, {"_id": 0}).sort("joined_at", -1).skip(skip).limit(limit)
                return await cursor.to_list(length=limit)
            except Exception as e:
                logger.error("MongoDB get_info_bot_users error: %s", e)

        # Fallback
        results = list(cls._fallback_info_users.values())
        if is_blocked is not None:
            results = [u for u in results if u.get("is_blocked") == is_blocked]
        if search:
            s_low = search.lower()
            results = [
                u for u in results
                if s_low in str(u.get("username", "")).lower()
                or s_low in str(u.get("first_name", "")).lower()
                or s_low in str(u.get("telegram_chat_id", "")).lower()
                or s_low in str(u.get("start_payload", "")).lower()
            ]
        results.sort(key=lambda x: str(x.get("joined_at", "")), reverse=True)
        return results[skip : skip + limit]

    @classmethod
    async def get_info_bot_stats(cls) -> Dict[str, Any]:
        """Aggregate analytics metrics for Info Bot leads and broadcasts."""
        users_col = cls.get_info_bot_users_collection()
        broadcasts_col = cls.get_info_bot_broadcasts_collection()
        now = datetime.utcnow()
        one_day_ago = datetime.utcfromtimestamp(now.timestamp() - 24 * 3600)

        if users_col is not None and broadcasts_col is not None:
            try:
                total_leads = await users_col.count_documents({})
                active_24h = await users_col.count_documents({"last_active_at": {"$gte": one_day_ago}})
                blocked_leads = await users_col.count_documents({"is_blocked": True})
                broadcasts_count = await broadcasts_col.count_documents({})
                return {
                    "total_leads": total_leads,
                    "active_24h": active_24h,
                    "blocked_leads": blocked_leads,
                    "broadcasts_count": broadcasts_count,
                    "status": "online"
                }
            except Exception as e:
                logger.error("MongoDB get_info_bot_stats error: %s", e)

        # Fallback
        users = list(cls._fallback_info_users.values())
        return {
            "total_leads": len(users),
            "active_24h": sum(1 for u in users if u.get("last_active_at", now) >= one_day_ago),
            "blocked_leads": sum(1 for u in users if u.get("is_blocked", False)),
            "broadcasts_count": len(cls._fallback_info_broadcasts),
            "status": "in-memory"
        }

    @classmethod
    async def create_info_bot_broadcast(cls, broadcast_data: Dict[str, Any]) -> str:
        """Records a completed broadcast into MongoDB."""
        now = datetime.utcnow()
        broadcast_data["created_at"] = now
        broadcasts_col = cls.get_info_bot_broadcasts_collection()

        if broadcasts_col is not None:
            try:
                res = await broadcasts_col.insert_one(broadcast_data)
                return str(res.inserted_id)
            except Exception as e:
                logger.error("MongoDB create_info_bot_broadcast error: %s", e)

        cls._fallback_info_broadcasts.insert(0, broadcast_data)
        return f"mem_{len(cls._fallback_info_broadcasts)}"

    @classmethod
    async def get_info_bot_broadcasts(cls, limit: int = 20) -> List[Dict[str, Any]]:
        """Retrieve recent broadcasts history."""
        broadcasts_col = cls.get_info_bot_broadcasts_collection()
        if broadcasts_col is not None:
            try:
                cursor = broadcasts_col.find({}, {"_id": 0}).sort("created_at", -1).limit(limit)
                return await cursor.to_list(length=limit)
            except Exception as e:
                logger.error("MongoDB get_info_bot_broadcasts error: %s", e)

        return cls._fallback_info_broadcasts[:limit]

    # ==========================================
    # Channel Published Posts & Bot Forwarding Operations
    # ==========================================

    @classmethod
    async def save_published_post(cls, post_data: Dict[str, Any]) -> bool:
        """Stores or updates a published channel post metadata (message ID, buttons, links)."""
        now = datetime.utcnow()
        post_number = int(post_data["post_number"])
        post_data["updated_at"] = now
        if "published_at" not in post_data:
            post_data["published_at"] = now

        col = cls.get_published_posts_collection()
        if col is not None:
            try:
                await col.update_one(
                    {"post_number": post_number},
                    {"$set": post_data},
                    upsert=True
                )
                return True
            except Exception as e:
                logger.error("MongoDB save_published_post error: %s", e)

        cls._fallback_published_posts[post_number] = post_data
        return True

    @classmethod
    async def get_published_post(cls, post_number: int) -> Optional[Dict[str, Any]]:
        """Retrieves published post metadata for a specific post number (1 to 5)."""
        col = cls.get_published_posts_collection()
        if col is not None:
            try:
                return await col.find_one({"post_number": int(post_number)}, {"_id": 0})
            except Exception as e:
                logger.error("MongoDB get_published_post error: %s", e)

        return cls._fallback_published_posts.get(int(post_number))

    @classmethod
    async def get_all_published_posts(cls) -> List[Dict[str, Any]]:
        """Returns all published posts sorted by post_number."""
        col = cls.get_published_posts_collection()
        if col is not None:
            try:
                cursor = col.find({}, {"_id": 0}).sort("post_number", 1)
                return await cursor.to_list(length=10)
            except Exception as e:
                logger.error("MongoDB get_all_published_posts error: %s", e)

        return sorted(list(cls._fallback_published_posts.values()), key=lambda x: x.get("post_number", 0))

    @classmethod
    async def get_uncontacted_bots(cls, limit: int = 500) -> List[Dict[str, Any]]:
        """Retrieves bot leads that have not yet been contacted."""
        leads_col = cls.get_leads_collection()
        if leads_col is not None:
            try:
                query = {
                    "lead_type": "bot",
                    "status": {"$in": ["new", "pending"]}
                }
                cursor = leads_col.find(query, {"_id": 0}).sort("first_seen_at", -1).limit(limit)
                return await cursor.to_list(length=limit)
            except Exception as e:
                logger.error("MongoDB get_uncontacted_bots error: %s", e)

        # Fallback
        results = [
            l for l in cls._fallback_leads.values()
            if l.get("lead_type") == "bot" and l.get("status") in ["new", "pending"]
        ]
        return results[:limit]

    @classmethod
    async def mark_lead_contacted(
        cls,
        username_clean: str,
        contact_method: str = "channel_forward",
        campaign_id: Optional[str] = None
    ) -> bool:
        """Updates lead status to contacted and records timestamp."""
        now = datetime.utcnow()
        clean = username_clean.lower().strip().lstrip("@")
        leads_col = cls.get_leads_collection()

        update_fields: Dict[str, Any] = {
            "status": "contacted",
            "last_contacted_at": now,
            "contact_method": contact_method,
            "updated_at": now
        }
        if campaign_id:
            update_fields["last_campaign_id"] = campaign_id

        if leads_col is not None:
            try:
                res = await leads_col.update_one(
                    {"username_clean": clean},
                    {"$set": update_fields}
                )
                return res.modified_count > 0
            except Exception as e:
                logger.error("MongoDB mark_lead_contacted error: %s", e)

        if clean in cls._fallback_leads:
            cls._fallback_leads[clean].update(update_fields)
            return True
        return False

    @classmethod
    async def create_bot_campaign(cls, campaign_data: Dict[str, Any]) -> str:
        """Records a new bot forwarding campaign."""
        now = datetime.utcnow()
        campaign_data["created_at"] = now
        campaign_data["updated_at"] = now
        campaigns_col = cls.get_bot_campaigns_collection()

        if campaigns_col is not None:
            try:
                res = await campaigns_col.insert_one(campaign_data)
                return str(res.inserted_id)
            except Exception as e:
                logger.error("MongoDB create_bot_campaign error: %s", e)

        cid = f"camp_{len(cls._fallback_bot_campaigns) + 1}_{int(now.timestamp())}"
        campaign_data["id"] = cid
        cls._fallback_bot_campaigns[cid] = campaign_data
        return cid

    @classmethod
    async def update_bot_campaign(cls, campaign_id: str, updates: Dict[str, Any]) -> bool:
        """Updates an ongoing or completed bot forwarding campaign."""
        from bson import ObjectId
        updates["updated_at"] = datetime.utcnow()
        campaigns_col = cls.get_bot_campaigns_collection()

        if campaigns_col is not None:
            try:
                query = {"_id": ObjectId(campaign_id)} if ObjectId.is_valid(campaign_id) else {"id": campaign_id}
                res = await campaigns_col.update_one(query, {"$set": updates})
                return res.modified_count > 0
            except Exception as e:
                logger.error("MongoDB update_bot_campaign error: %s", e)

        if campaign_id in cls._fallback_bot_campaigns:
            cls._fallback_bot_campaigns[campaign_id].update(updates)
            return True
        return False

    @classmethod
    async def get_bot_campaigns(cls, limit: int = 20) -> List[Dict[str, Any]]:
        """Retrieve recent bot forwarding campaign history."""
        campaigns_col = cls.get_bot_campaigns_collection()
        if campaigns_col is not None:
            try:
                cursor = campaigns_col.find({}).sort("created_at", -1).limit(limit)
                docs = await cursor.to_list(length=limit)
                for d in docs:
                    if "_id" in d:
                        d["id"] = str(d.pop("_id"))
                return docs
            except Exception as e:
                logger.error("MongoDB get_bot_campaigns error: %s", e)

        return sorted(list(cls._fallback_bot_campaigns.values()), key=lambda x: str(x.get("created_at", "")), reverse=True)[:limit]

    # =========================================================================
    # Telegram Multi-Account Management
    # =========================================================================

    @classmethod
    async def save_telegram_account(cls, account_data: Dict[str, Any]) -> bool:
        """Upsert a Telegram account by phone number."""
        now = datetime.utcnow()
        account_data["last_active_at"] = now
        phone = account_data.get("phone", "").strip()

        accounts_col = cls.get_telegram_accounts_collection()
        if accounts_col is not None:
            try:
                await accounts_col.update_one(
                    {"phone": phone},
                    {"$set": account_data},
                    upsert=True
                )
                return True
            except Exception as e:
                logger.error("MongoDB save_telegram_account error: %s", e)

        cls._fallback_telegram_accounts[phone] = account_data
        return True

    @classmethod
    async def get_telegram_account_by_phone(cls, phone: str) -> Optional[Dict[str, Any]]:
        """Retrieve an account by phone number."""
        phone = phone.strip()
        accounts_col = cls.get_telegram_accounts_collection()
        if accounts_col is not None:
            try:
                doc = await accounts_col.find_one({"phone": phone})
                if doc and "_id" in doc:
                    doc["id"] = str(doc.pop("_id"))
                return doc
            except Exception as e:
                logger.error("MongoDB get_telegram_account_by_phone error: %s", e)

        return cls._fallback_telegram_accounts.get(phone)

    @classmethod
    async def get_telegram_account_by_slot(cls, slot: int) -> Optional[Dict[str, Any]]:
        """Retrieve the active account assigned to a specific week/slot (1 to 5)."""
        accounts_col = cls.get_telegram_accounts_collection()
        if accounts_col is not None:
            try:
                doc = await accounts_col.find_one({"assigned_slot": slot, "status": "active"})
                if doc and "_id" in doc:
                    doc["id"] = str(doc.pop("_id"))
                return doc
            except Exception as e:
                logger.error("MongoDB get_telegram_account_by_slot error: %s", e)

        for acc in cls._fallback_telegram_accounts.values():
            if acc.get("assigned_slot") == slot and acc.get("status") == "active":
                return acc
        return None

    @classmethod
    async def get_all_telegram_accounts(cls) -> List[Dict[str, Any]]:
        """Retrieve all registered accounts sorted by assigned slot."""
        accounts_col = cls.get_telegram_accounts_collection()
        if accounts_col is not None:
            try:
                cursor = accounts_col.find({}).sort("assigned_slot", 1)
                docs = await cursor.to_list(length=100)
                for d in docs:
                    if "_id" in d:
                        d["id"] = str(d.pop("_id"))
                return docs
            except Exception as e:
                logger.error("MongoDB get_all_telegram_accounts error: %s", e)

        return sorted(list(cls._fallback_telegram_accounts.values()), key=lambda x: x.get("assigned_slot", 99))

    @classmethod
    async def delete_telegram_account(cls, phone: str) -> bool:
        """Remove a registered account."""
        phone = phone.strip()
        accounts_col = cls.get_telegram_accounts_collection()
        if accounts_col is not None:
            try:
                res = await accounts_col.delete_one({"phone": phone})
                return res.deleted_count > 0
            except Exception as e:
                logger.error("MongoDB delete_telegram_account error: %s", e)

        if phone in cls._fallback_telegram_accounts:
            del cls._fallback_telegram_accounts[phone]
            return True
        return False

    @classmethod
    async def update_telegram_account_status(cls, phone: str, status: str) -> bool:
        """Update account health status (active, flood_wait, restricted, logged_out)."""
        phone = phone.strip()
        accounts_col = cls.get_telegram_accounts_collection()
        if accounts_col is not None:
            try:
                res = await accounts_col.update_one(
                    {"phone": phone},
                    {"$set": {"status": status, "last_active_at": datetime.utcnow()}}
                )
                return res.modified_count > 0
            except Exception as e:
                logger.error("MongoDB update_telegram_account_status error: %s", e)

        if phone in cls._fallback_telegram_accounts:
            cls._fallback_telegram_accounts[phone]["status"] = status
            cls._fallback_telegram_accounts[phone]["last_active_at"] = datetime.utcnow()
            return True
        return False

    # =========================================================================
    # 5-Week Progressive Drip Forwarding Operations
    # =========================================================================

    @classmethod
    async def get_eligible_bots_for_stage(
        cls,
        stage: int,
        min_cooldown_days: int = 7,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Finds deal contact bots eligible for Stage N:
        - Stage 1: promo_stage is 0 or None (new contact bots).
        - Stage 2 to 5: promo_stage == stage - 1 AND last_promo_sent_at <= now - 7 days.
        - Stage > 5: bots that already received all 5 are excluded permanently.
        """
        now = datetime.utcnow()
        leads_col = cls.get_leads_collection()

        if stage < 1 or stage > 5:
            return []

        if stage == 1:
            # Stage 1: Bots never messaged or with promo_stage == 0
            mongo_query = {
                "lead_type": "bot",
                "$or": [
                    {"promo_stage": {"$exists": False}},
                    {"promo_stage": 0},
                    {"promo_stage": None}
                ]
            }
        else:
            # Stage 2 to 5: Must have received previous stage and passed cooldown
            from datetime import timedelta
            cutoff_date = now - timedelta(days=min_cooldown_days)
            mongo_query = {
                "lead_type": "bot",
                "promo_stage": stage - 1,
                "last_promo_sent_at": {"$lte": cutoff_date}
            }

        if leads_col is not None:
            try:
                cursor = leads_col.find(mongo_query).sort("first_seen_at", 1).limit(limit)
                docs = await cursor.to_list(length=limit)
                for d in docs:
                    if "_id" in d:
                        d["id"] = str(d.pop("_id"))
                return docs
            except Exception as e:
                logger.error("MongoDB get_eligible_bots_for_stage error: %s", e)

        # Fallback in-memory
        from datetime import timedelta
        cutoff = now - timedelta(days=min_cooldown_days)
        results = []
        for lead in cls._fallback_leads.values():
            if lead.get("lead_type") != "bot":
                continue
            cur_stage = lead.get("promo_stage", 0) or 0
            if stage == 1 and cur_stage == 0:
                results.append(lead)
            elif stage > 1 and cur_stage == (stage - 1):
                last_sent = lead.get("last_promo_sent_at")
                if last_sent is None or last_sent <= cutoff:
                    results.append(lead)
            if len(results) >= limit:
                break
        return results

    @classmethod
    async def advance_lead_promo_stage(
        cls,
        username_clean: str,
        new_stage: int,
        account_phone: str,
        post_number: int,
        message_link: str
    ) -> bool:
        """
        Advances a contact bot's progression after successfully forwarding Post N.
        Records history and sets next_eligible_at to now + 7 days (or None if stage 5 complete).
        """
        now = datetime.utcnow()
        from datetime import timedelta
        next_eligible = now + timedelta(days=7) if new_stage < 5 else None
        new_status = "converted" if new_stage >= 5 else "contacted"

        history_entry = {
            "post_number": post_number,
            "account_phone": account_phone,
            "sent_at": now,
            "message_link": message_link,
            "status": "sent"
        }

        update_doc = {
            "$set": {
                "promo_stage": new_stage,
                "status": new_status,
                "last_promo_sent_at": now,
                "last_contacted_at": now,
                "contact_method": f"bot_forward_post_{post_number}",
                "next_eligible_at": next_eligible
            },
            "$push": {
                "promo_history": history_entry
            }
        }

        leads_col = cls.get_leads_collection()
        if leads_col is not None:
            try:
                res = await leads_col.update_one({"username_clean": username_clean.lower()}, update_doc)
                return res.modified_count > 0
            except Exception as e:
                logger.error("MongoDB advance_lead_promo_stage error: %s", e)

        clean = username_clean.lower()
        if clean in cls._fallback_leads:
            lead = cls._fallback_leads[clean]
            lead["promo_stage"] = new_stage
            lead["status"] = new_status
            lead["last_promo_sent_at"] = now
            lead["last_contacted_at"] = now
            lead["contact_method"] = f"bot_forward_post_{post_number}"
            lead["next_eligible_at"] = next_eligible
            if "promo_history" not in lead:
                lead["promo_history"] = []
            lead["promo_history"].append(history_entry)
            return True
        return False

    @classmethod
    async def backdate_lead_last_promo(cls, username_clean: str, backdated_time: datetime) -> bool:
        """Helper to backdate last_promo_sent_at for testing/simulation."""
        leads_col = cls.get_leads_collection()
        clean = username_clean.lower()
        if leads_col is not None:
            try:
                res = await leads_col.update_one(
                    {"username_clean": clean},
                    {"$set": {"last_promo_sent_at": backdated_time}}
                )
                return res.modified_count > 0
            except Exception as e:
                logger.error("MongoDB backdate_lead_last_promo error: %s", e)

        if clean in cls._fallback_leads:
            cls._fallback_leads[clean]["last_promo_sent_at"] = backdated_time
            return True
        return False

    @classmethod
    async def get_drip_stage_funnel(cls) -> Dict[str, Any]:
        """Calculates bot counts across all 5 weekly stages."""
        leads_col = cls.get_leads_collection()
        now = datetime.utcnow()
        from datetime import timedelta
        cutoff_7d = now - timedelta(days=7)

        counts = {
            "stage_0_pending": 0,
            "stage_1_sent": 0,
            "stage_1_eligible_for_stage_2": 0,
            "stage_2_sent": 0,
            "stage_2_eligible_for_stage_3": 0,
            "stage_3_sent": 0,
            "stage_3_eligible_for_stage_4": 0,
            "stage_4_sent": 0,
            "stage_4_eligible_for_stage_5": 0,
            "stage_5_completed": 0,
            "total_bots": 0
        }

        if leads_col is not None:
            try:
                counts["total_bots"] = await leads_col.count_documents({"lead_type": "bot"})
                counts["stage_0_pending"] = await leads_col.count_documents({
                    "lead_type": "bot",
                    "$or": [{"promo_stage": {"$exists": False}}, {"promo_stage": 0}, {"promo_stage": None}]
                })
                for s in range(1, 6):
                    c = await leads_col.count_documents({"lead_type": "bot", "promo_stage": s})
                    if s == 5:
                        counts["stage_5_completed"] = c
                    else:
                        counts[f"stage_{s}_sent"] = c
                        counts[f"stage_{s}_eligible_for_stage_{s+1}"] = await leads_col.count_documents({
                            "lead_type": "bot",
                            "promo_stage": s,
                            "last_promo_sent_at": {"$lte": cutoff_7d}
                        })
                return counts
            except Exception as e:
                logger.error("MongoDB get_drip_stage_funnel error: %s", e)

        # Fallback calculation
        for lead in cls._fallback_leads.values():
            if lead.get("lead_type") != "bot":
                continue
            counts["total_bots"] += 1
            st = lead.get("promo_stage", 0) or 0
            if st == 0:
                counts["stage_0_pending"] += 1
            elif st == 5:
                counts["stage_5_completed"] += 1
            elif 1 <= st <= 4:
                counts[f"stage_{st}_sent"] += 1
                last_sent = lead.get("last_promo_sent_at")
                if last_sent is None or last_sent <= cutoff_7d:
                    counts[f"stage_{st}_eligible_for_stage_{st+1}"] += 1

        return counts

    # ==========================================
    # Autonomous Keyword Search Methods
    # ==========================================

    @classmethod
    async def get_search_keywords(cls, enabled_only: bool = False) -> List[Dict[str, Any]]:
        """Retrieves list of configured target search keywords."""
        keywords_col = cls.get_search_keywords_collection()
        query = {"enabled": True} if enabled_only else {}
        if keywords_col is not None:
            try:
                cursor = keywords_col.find(query).sort("created_at", -1)
                docs = await cursor.to_list(length=200)
                for d in docs:
                    if "_id" in d:
                        d["id"] = str(d.pop("_id"))
                return docs
            except Exception as e:
                logger.error("MongoDB get_search_keywords error: %s", e)

        # Fallback in-memory
        results = []
        for kw, doc in cls._fallback_keywords.items():
            if enabled_only and not doc.get("enabled", True):
                continue
            results.append(doc)
        return results

    @classmethod
    async def add_search_keyword(cls, keyword: str, category: str = "deals") -> Dict[str, Any]:
        """Adds a new target search keyword."""
        clean_kw = keyword.strip().lower()
        if not clean_kw:
            raise ValueError("Keyword cannot be empty.")

        now = datetime.utcnow()
        doc = {
            "keyword": clean_kw,
            "category": category.strip().lower() or "deals",
            "enabled": True,
            "total_channels_found": 0,
            "total_leads_extracted": 0,
            "last_searched_at": None,
            "created_at": now
        }

        keywords_col = cls.get_search_keywords_collection()
        if keywords_col is not None:
            try:
                res = await keywords_col.update_one(
                    {"keyword": clean_kw},
                    {"$setOnInsert": doc},
                    upsert=True
                )
                doc["id"] = str(res.upserted_id) if res.upserted_id else clean_kw
                return doc
            except Exception as e:
                logger.error("MongoDB add_search_keyword error: %s", e)

        cls._fallback_keywords[clean_kw] = doc
        return doc

    @classmethod
    async def toggle_search_keyword(cls, keyword: str, enabled: bool) -> bool:
        """Enables or disables an automated search keyword."""
        clean_kw = keyword.strip().lower()
        keywords_col = cls.get_search_keywords_collection()
        if keywords_col is not None:
            try:
                res = await keywords_col.update_one(
                    {"keyword": clean_kw},
                    {"$set": {"enabled": enabled}}
                )
                return res.modified_count > 0 or res.matched_count > 0
            except Exception as e:
                logger.error("MongoDB toggle_search_keyword error: %s", e)

        if clean_kw in cls._fallback_keywords:
            cls._fallback_keywords[clean_kw]["enabled"] = enabled
            return True
        return False

    @classmethod
    async def delete_search_keyword(cls, keyword: str) -> bool:
        """Deletes a target search keyword."""
        clean_kw = keyword.strip().lower()
        keywords_col = cls.get_search_keywords_collection()
        if keywords_col is not None:
            try:
                res = await keywords_col.delete_one({"keyword": clean_kw})
                return res.deleted_count > 0
            except Exception as e:
                logger.error("MongoDB delete_search_keyword error: %s", e)

        if clean_kw in cls._fallback_keywords:
            del cls._fallback_keywords[clean_kw]
            return True
        return False

    @classmethod
    async def update_keyword_stats(cls, keyword: str, channels_count: int, leads_count: int):
        """Updates performance metrics for a searched keyword."""
        clean_kw = keyword.strip().lower()
        now = datetime.utcnow()
        keywords_col = cls.get_search_keywords_collection()
        if keywords_col is not None:
            try:
                await keywords_col.update_one(
                    {"keyword": clean_kw},
                    {
                        "$inc": {
                            "total_channels_found": channels_count,
                            "total_leads_extracted": leads_count
                        },
                        "$set": {"last_searched_at": now}
                    }
                )
            except Exception as e:
                logger.error("MongoDB update_keyword_stats error: %s", e)

        if clean_kw in cls._fallback_keywords:
            doc = cls._fallback_keywords[clean_kw]
            doc["total_channels_found"] = doc.get("total_channels_found", 0) + channels_count
            doc["total_leads_extracted"] = doc.get("total_leads_extracted", 0) + leads_count
            doc["last_searched_at"] = now

    @classmethod
    async def save_discovered_channel(cls, channel_data: Dict[str, Any]) -> bool:
        """Saves or updates a channel discovered via Telegram keyword search."""
        channel_id = channel_data.get("channel_id")
        if not channel_id:
            return False

        now = datetime.utcnow()
        channel_data["last_scanned_at"] = now
        channels_col = cls.get_discovered_channels_collection()

        if channels_col is not None:
            try:
                set_data = dict(channel_data)
                set_data.pop("discovered_at", None)
                await channels_col.update_one(
                    {"channel_id": channel_id},
                    {
                        "$setOnInsert": {
                            "discovered_at": now
                        },
                        "$set": set_data
                    },
                    upsert=True
                )
                return True
            except Exception as e:
                logger.error("MongoDB save_discovered_channel error: %s", e)

        cls._fallback_discovered_channels[channel_id] = channel_data
        return True

    @classmethod
    async def is_channel_recently_scanned(cls, channel_id: int, cooldown_days: int = 7) -> bool:
        """Checks if a channel was scanned recently within the cooldown window."""
        channels_col = cls.get_discovered_channels_collection()
        now = datetime.utcnow()
        from datetime import timedelta
        cutoff = now - timedelta(days=cooldown_days)

        if channels_col is not None:
            try:
                doc = await channels_col.find_one({
                    "channel_id": channel_id,
                    "last_scanned_at": {"$gte": cutoff}
                })
                return doc is not None
            except Exception as e:
                logger.error("MongoDB is_channel_recently_scanned error: %s", e)

        if channel_id in cls._fallback_discovered_channels:
            last = cls._fallback_discovered_channels[channel_id].get("last_scanned_at")
            if last and last >= cutoff:
                return True
        return False

    @classmethod
    async def get_discovered_channels(
        cls,
        limit: int = 50,
        skip: int = 0,
        search: Optional[str] = None,
        keyword: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Returns list of channels discovered via keyword searches."""
        channels_col = cls.get_discovered_channels_collection()
        query: Dict[str, Any] = {}

        if keyword:
            query["matching_keyword"] = keyword.strip().lower()

        if search:
            s_regex = {"$regex": search.strip(), "$options": "i"}
            query["$or"] = [
                {"title": s_regex},
                {"username": s_regex},
                {"matching_keyword": s_regex}
            ]

        if channels_col is not None:
            try:
                cursor = channels_col.find(query).sort("last_scanned_at", -1).skip(skip).limit(limit)
                docs = await cursor.to_list(length=limit)
                for d in docs:
                    if "_id" in d:
                        d["id"] = str(d.pop("_id"))
                return docs
            except Exception as e:
                logger.error("MongoDB get_discovered_channels error: %s", e)

        results = list(cls._fallback_discovered_channels.values())
        return results[skip:skip+limit]

    @classmethod
    async def get_keyword_search_stats(cls) -> Dict[str, Any]:
        """Provides high-level KPI metrics for the Autonomous Keyword Search System."""
        keywords_col = cls.get_search_keywords_collection()
        channels_col = cls.get_discovered_channels_collection()
        leads_col = cls.get_leads_collection()

        stats = {
            "total_keywords": 0,
            "active_keywords": 0,
            "total_channels_discovered": 0,
            "total_leads_from_keywords": 0
        }

        if keywords_col is not None:
            try:
                stats["total_keywords"] = await keywords_col.count_documents({})
                stats["active_keywords"] = await keywords_col.count_documents({"enabled": True})
            except Exception:
                pass

        if channels_col is not None:
            try:
                stats["total_channels_discovered"] = await channels_col.count_documents({})
            except Exception:
                pass

        if leads_col is not None:
            try:
                stats["total_leads_from_keywords"] = await leads_col.count_documents({
                    "extracted_from.source_id": {"$regex": r"^keyword_search:", "$options": "i"}
                })
            except Exception:
                pass

        return stats

    # =========================================================================
    # Promotional Posts (Create & Manage Posts to Send)
    # =========================================================================

    @classmethod
    async def save_promotional_post(cls, post_data: Any) -> str:
        """Saves or updates a promotional post template."""
        if hasattr(post_data, "model_dump"):
            post_data = post_data.model_dump()
        elif not isinstance(post_data, dict):
            post_data = dict(post_data)

        col = cls.get_promotional_posts_collection()
        now = datetime.utcnow()
        import uuid
        post_id = post_data.get("id") or str(uuid.uuid4())
        post_data["id"] = post_id
        if "created_at" not in post_data:
            post_data["created_at"] = now

        if col is not None:
            try:
                await col.update_one(
                    {"id": post_id},
                    {"$set": post_data},
                    upsert=True
                )
                return post_id
            except Exception as e:
                logger.error("MongoDB save_promotional_post error: %s", e)

        cls._fallback_promotional_posts[post_id] = post_data
        return post_id

    @classmethod
    async def get_all_promotional_posts(cls) -> List[Dict[str, Any]]:
        """Retrieves all promotional post templates."""
        col = cls.get_promotional_posts_collection()
        if col is not None:
            try:
                cursor = col.find({}, {"_id": 0}).sort("created_at", -1)
                return await cursor.to_list(length=100)
            except Exception as e:
                logger.error("MongoDB get_all_promotional_posts error: %s", e)

        return sorted(
            list(cls._fallback_promotional_posts.values()),
            key=lambda x: x.get("created_at", datetime.min),
            reverse=True
        )

    @classmethod
    async def get_promotional_post_by_id(cls, post_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a single promotional post by ID."""
        col = cls.get_promotional_posts_collection()
        if col is not None:
            try:
                return await col.find_one({"id": post_id}, {"_id": 0})
            except Exception as e:
                logger.error("MongoDB get_promotional_post_by_id error: %s", e)

        return cls._fallback_promotional_posts.get(post_id)

    @classmethod
    async def delete_promotional_post(cls, post_id: str) -> bool:
        """Deletes a promotional post template."""
        col = cls.get_promotional_posts_collection()
        if col is not None:
            try:
                res = await col.delete_one({"id": post_id})
                return res.deleted_count > 0
            except Exception as e:
                logger.error("MongoDB delete_promotional_post error: %s", e)

        if post_id in cls._fallback_promotional_posts:
            del cls._fallback_promotional_posts[post_id]
            return True
        return False

    # =========================================================================
    # Automatic Sending Reports (Delivery Audit Logs & Analytics)
    # =========================================================================

    @classmethod
    async def save_sending_report(cls, report_data: Any) -> bool:
        """Saves a delivery report log entry."""
        if hasattr(report_data, "model_dump"):
            report_data = report_data.model_dump()
        elif not isinstance(report_data, dict):
            report_data = dict(report_data)

        col = cls.get_sending_reports_collection()
        now = datetime.utcnow()
        import uuid
        if "id" not in report_data or not report_data["id"]:
            report_data["id"] = str(uuid.uuid4())
        if "sent_at" not in report_data:
            report_data["sent_at"] = now

        if col is not None:
            try:
                await col.insert_one(report_data)
                return True
            except Exception as e:
                logger.error("MongoDB save_sending_report error: %s", e)

        cls._fallback_sending_reports.insert(0, report_data)
        if len(cls._fallback_sending_reports) > 500:
            cls._fallback_sending_reports.pop()
        return True

    @classmethod
    async def get_sending_reports(
        cls,
        limit: int = 50,
        skip: int = 0,
        search: Optional[str] = None,
        status: Optional[str] = None,
        recipient_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Queries sending report logs with filtering and pagination."""
        col = cls.get_sending_reports_collection()
        query: Dict[str, Any] = {}
        if status and status != "all":
            query["status"] = status
        if recipient_type and recipient_type != "all":
            query["recipient_type"] = recipient_type
        if search:
            query["$or"] = [
                {"recipient_username": {"$regex": search, "$options": "i"}},
                {"sender_account_phone": {"$regex": search, "$options": "i"}},
                {"post_title": {"$regex": search, "$options": "i"}}
            ]

        if col is not None:
            try:
                cursor = col.find(query, {"_id": 0}).sort("sent_at", -1).skip(skip).limit(limit)
                return await cursor.to_list(length=limit)
            except Exception as e:
                logger.error("MongoDB get_sending_reports error: %s", e)

        results = cls._fallback_sending_reports
        if status and status != "all":
            results = [r for r in results if r.get("status") == status]
        if recipient_type and recipient_type != "all":
            results = [r for r in results if r.get("recipient_type") == recipient_type]
        if search:
            s_low = search.lower()
            results = [
                r for r in results
                if s_low in r.get("recipient_username", "").lower()
                or s_low in r.get("sender_account_phone", "").lower()
                or s_low in r.get("post_title", "").lower()
            ]
        return results[skip : skip + limit]

    @classmethod
    async def get_sending_reports_count(
        cls,
        search: Optional[str] = None,
        status: Optional[str] = None,
        recipient_type: Optional[str] = None
    ) -> int:
        """Returns count of matching sending report logs."""
        col = cls.get_sending_reports_collection()
        query: Dict[str, Any] = {}
        if status and status != "all":
            query["status"] = status
        if recipient_type and recipient_type != "all":
            query["recipient_type"] = recipient_type
        if search:
            query["$or"] = [
                {"recipient_username": {"$regex": search, "$options": "i"}},
                {"sender_account_phone": {"$regex": search, "$options": "i"}},
                {"post_title": {"$regex": search, "$options": "i"}}
            ]

        if col is not None:
            try:
                return await col.count_documents(query)
            except Exception as e:
                logger.error("MongoDB get_sending_reports_count error: %s", e)

        return len(cls._fallback_sending_reports)

    @classmethod
    async def get_sending_reports_analytics(cls) -> Dict[str, Any]:
        """Calculates executive delivery analytics and breakdown stats."""
        col = cls.get_sending_reports_collection()
        now = datetime.utcnow()
        from datetime import timedelta
        start_of_today = datetime(now.year, now.month, now.day)
        start_of_week = now - timedelta(days=7)

        stats = {
            "total_sent": 0,
            "total_failed": 0,
            "sent_today": 0,
            "sent_this_week": 0,
            "by_lead_type": {"userbot": 0, "username": 0},
            "by_account": [],
            "recent_activity": []
        }

        if col is not None:
            try:
                stats["total_sent"] = await col.count_documents({"status": "sent"})
                stats["total_failed"] = await col.count_documents({"status": {"$ne": "sent"}})
                stats["sent_today"] = await col.count_documents({"status": "sent", "sent_at": {"$gte": start_of_today}})
                stats["sent_this_week"] = await col.count_documents({"status": "sent", "sent_at": {"$gte": start_of_week}})

                # Breakdown by lead type
                userbots_count = await col.count_documents({"recipient_type": "userbot", "status": "sent"})
                usernames_count = await col.count_documents({"recipient_type": "username", "status": "sent"})
                stats["by_lead_type"] = {"userbot": userbots_count, "username": usernames_count}

                # Breakdown by account via aggregation
                pipeline = [
                    {"$match": {"status": "sent"}},
                    {"$group": {
                        "_id": "$sender_account_phone",
                        "sender_name": {"$first": "$sender_account_name"},
                        "count": {"$sum": 1}
                    }},
                    {"$sort": {"count": -1}},
                    {"$limit": 10}
                ]
                acc_agg = await col.aggregate(pipeline).to_list(length=10)
                stats["by_account"] = [
                    {"phone": item["_id"], "name": item.get("sender_name") or item["_id"], "count": item["count"]}
                    for item in acc_agg if item["_id"]
                ]

                # Recent activity
                stats["recent_activity"] = await col.find({}, {"_id": 0}).sort("sent_at", -1).limit(6).to_list(length=6)
                return stats
            except Exception as e:
                logger.error("MongoDB get_sending_reports_analytics error: %s", e)

        # Fallback in-memory calculation
        reports = cls._fallback_sending_reports
        sent_reports = [r for r in reports if r.get("status") == "sent"]
        stats["total_sent"] = len(sent_reports)
        stats["total_failed"] = len([r for r in reports if r.get("status") != "sent"])
        stats["sent_today"] = len([r for r in sent_reports if r.get("sent_at", datetime.min) >= start_of_today])
        stats["sent_this_week"] = len([r for r in sent_reports if r.get("sent_at", datetime.min) >= start_of_week])
        stats["by_lead_type"] = {
            "userbot": len([r for r in sent_reports if r.get("recipient_type") == "userbot"]),
            "username": len([r for r in sent_reports if r.get("recipient_type") == "username"])
        }
        stats["recent_activity"] = reports[:6]
        return stats

