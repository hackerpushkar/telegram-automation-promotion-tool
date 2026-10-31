from datetime import datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class LeadType(str, Enum):
    USER = "user"
    BOT = "bot"


class LeadStatus(str, Enum):
    NEW = "new"
    CONTACTED = "contacted"
    CONVERTED = "converted"
    IGNORED = "ignored"


class SourceChannel(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    platform: str = "telegram"
    chatId: Optional[str] = None
    inviteLink: Optional[str] = None
    active: bool = True
    createdAt: Optional[datetime] = None


class SourceSyncResponse(BaseModel):
    status: str
    count: int = 0
    message: Optional[str] = None
    windowMinutes: Optional[int] = 30
    since: Optional[datetime] = None
    serverTime: Optional[datetime] = None
    sources: List[SourceChannel] = Field(default_factory=list)


class LeadSourceInfo(BaseModel):
    source_name: Optional[str] = None
    source_invite_link: Optional[str] = None
    platform: str = "telegram"
    source_id: Optional[str] = None


class LeadPromoHistory(BaseModel):
    post_number: int
    account_phone: str
    sent_at: datetime = Field(default_factory=datetime.utcnow)
    message_link: str
    status: str = "sent"


class Lead(BaseModel):
    username: str
    username_clean: str
    lead_type: LeadType
    extracted_from: LeadSourceInfo
    bio_context: Optional[str] = None
    status: LeadStatus = LeadStatus.NEW
    first_seen_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)
    last_contacted_at: Optional[datetime] = None
    contact_method: Optional[str] = None
    promo_stage: int = 0  # 0: never contacted, 1: Post 1, 2: Post 2, 3: Post 3, 4: Post 4, 5: Post 5 (Completed)
    last_promo_sent_at: Optional[datetime] = None
    next_eligible_at: Optional[datetime] = None  # sent_at + 7 days
    promo_history: List[LeadPromoHistory] = Field(default_factory=list)


# ==========================================
# Official Info Bot (@ProaffiliatersInfoBot) Models
# ==========================================

class InfoBotUser(BaseModel):
    telegram_chat_id: str
    username: Optional[str] = None
    first_name: Optional[str] = ""
    last_name: Optional[str] = ""
    start_payload: Optional[str] = None
    joined_at: datetime = Field(default_factory=datetime.utcnow)
    last_active_at: datetime = Field(default_factory=datetime.utcnow)
    is_blocked: bool = False


class InlineButton(BaseModel):
    text: str
    url: str


class InfoBotBroadcast(BaseModel):
    admin_chat_id: str
    message_text: str
    media_url: Optional[str] = None
    media_type: Optional[str] = None
    inline_buttons: List[InlineButton] = Field(default_factory=list)
    total_targeted: int = 0
    successful_sent: int = 0
    failed_count: int = 0
    status: str = "completed"
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ==========================================
# Channel Post & Bot Forwarder Models
# ==========================================

class ChannelPublishedPost(BaseModel):
    post_number: int
    channel_username: str
    message_id: int
    message_link: str
    caption: str
    image_name: str
    inline_buttons: List[InlineButton] = Field(default_factory=list)
    published_at: datetime = Field(default_factory=datetime.utcnow)


class BotCampaign(BaseModel):
    id: Optional[str] = None
    post_number: int
    channel_username: str
    channel_message_id: int
    delay_seconds: int = 8
    total_targeted: int = 0
    successful_sent: int = 0
    failed_count: int = 0
    status: str = "in_progress"  # in_progress, completed, paused, cancelled
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    logs: List[str] = Field(default_factory=list)
 
 
# ==========================================
# Telegram Multi-Account Models
# ==========================================

class TelegramAccount(BaseModel):
    account_index: int = 1
    phone: str
    user_id: int
    first_name: str
    last_name: Optional[str] = None
    username: Optional[str] = None
    session_string: str
    status: str = "active"  # "active", "flood_wait", "logged_out", "restricted"
    assigned_slot: int = 1   # Slot 1 to 5 (mapped to Post 1 to 5 / Week 1 to 5)
    daily_send_count: int = 0
    last_send_date: Optional[str] = None
    last_active_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ==========================================
# Autonomous Keyword Search Models
# ==========================================

class SearchKeyword(BaseModel):
    id: Optional[str] = None
    keyword: str
    category: str = "deals"  # deals, amazon, loot, coupons, general
    enabled: bool = True
    total_channels_found: int = 0
    total_leads_extracted: int = 0
    last_searched_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class DiscoveredChannel(BaseModel):
    id: Optional[str] = None
    channel_id: int
    title: str
    username: Optional[str] = None
    invite_link: Optional[str] = None
    bio: Optional[str] = None
    matching_keyword: str
    extracted_leads_count: int = 0
    status: str = "scanned"  # discovered, scanned, ignored
    discovered_at: datetime = Field(default_factory=datetime.utcnow)
    last_scanned_at: Optional[datetime] = None


