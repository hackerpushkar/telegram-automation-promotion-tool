import os
from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # ProAffiliaters External API
    proaffiliaters_api_url: str = Field(
        default="https://app.proaffiliaters.com/api/external/sources/recent",
        alias="PROAFFILIATERS_API_URL"
    )
    proaffiliaters_api_key: str = Field(
        default="proaff_ext_source_sync_key_2026",
        alias="PROAFFILIATERS_API_KEY"
    )
    sync_interval_minutes: int = Field(
        default=30,
        alias="SYNC_INTERVAL_MINUTES"
    )

    # Telegram MTProto Client (Telethon)
    telegram_api_id: Optional[int] = Field(default=None, alias="TELEGRAM_API_ID")
    telegram_api_hash: Optional[str] = Field(default=None, alias="TELEGRAM_API_HASH")
    telegram_phone_number: Optional[str] = Field(default=None, alias="TELEGRAM_PHONE_NUMBER")
    telegram_session_name: str = Field(default="lead_scanner_session", alias="TELEGRAM_SESSION_NAME")

    @field_validator("telegram_api_id", mode="before")
    @classmethod
    def empty_str_to_none(cls, v):
        if v == "" or v is None:
            return None
        return int(v)

    # MongoDB Settings
    mongodb_uri: str = Field(default="mongodb://localhost:27017", alias="MONGODB_URI")
    mongodb_db_name: str = Field(default="telegram_promotion_tool", alias="MONGODB_DB_NAME")
    mongodb_leads_collection: str = Field(default="leads", alias="MONGODB_LEADS_COLLECTION")
    mongodb_info_bot_users_collection: str = Field(default="info_bot_users", alias="MONGODB_INFO_BOT_USERS_COLLECTION")
    mongodb_info_bot_broadcasts_collection: str = Field(default="info_bot_broadcasts", alias="MONGODB_INFO_BOT_BROADCASTS_COLLECTION")
    mongodb_published_posts_collection: str = Field(default="published_posts", alias="MONGODB_PUBLISHED_POSTS_COLLECTION")
    mongodb_bot_campaigns_collection: str = Field(default="bot_campaigns", alias="MONGODB_BOT_CAMPAIGNS_COLLECTION")
    mongodb_telegram_accounts_collection: str = Field(default="telegram_accounts", alias="MONGODB_TELEGRAM_ACCOUNTS_COLLECTION")
    mongodb_search_keywords_collection: str = Field(default="search_keywords", alias="MONGODB_SEARCH_KEYWORDS_COLLECTION")
    mongodb_discovered_channels_collection: str = Field(default="discovered_channels", alias="MONGODB_DISCOVERED_CHANNELS_COLLECTION")
    mongodb_promotional_posts_collection: str = Field(default="promotional_posts", alias="MONGODB_PROMOTIONAL_POSTS_COLLECTION")
    mongodb_sending_reports_collection: str = Field(default="sending_reports", alias="MONGODB_SENDING_REPORTS_COLLECTION")

    # Autonomous Telegram Keyword Search
    keyword_search_enabled: bool = Field(default=True, alias="KEYWORD_SEARCH_ENABLED")
    keyword_search_interval_minutes: int = Field(default=30, alias="KEYWORD_SEARCH_INTERVAL_MINUTES")
    keyword_search_limit_per_keyword: int = Field(default=15, alias="KEYWORD_SEARCH_LIMIT_PER_KEYWORD")

    # Promotion Staging Channel
    promo_channel_username: str = Field(default="@pro_affiliaters", alias="PROMO_CHANNEL_USERNAME")

    # Telegram Official Info Bot (@ProaffiliatersInfoBot)
    telegram_info_bot_token: Optional[str] = Field(
        default="8777035188:AAFvldFDmnaVasOM3niIRnHPIv6lrI-klng",
        alias="TELEGRAM_INFO_BOT_TOKEN"
    )
    telegram_info_bot_admin_id: str = Field(default="7573032265", alias="TELEGRAM_INFO_BOT_ADMIN_ID")
    telegram_info_bot_username: str = Field(default="ProaffiliatersInfoBot", alias="TELEGRAM_INFO_BOT_USERNAME")
    webapp_url: str = Field(default="https://app.proaffiliaters.com", alias="WEBAPP_URL")
    official_channel_url: str = Field(default="https://t.me/pro_affiliaters", alias="OFFICIAL_CHANNEL_URL")
    support_bot_url: str = Field(default="https://t.me/ProAffiliatersHelpBot", alias="SUPPORT_BOT_URL")

    # Web UI Server
    web_host: str = Field(default="127.0.0.1", alias="WEB_HOST")
    web_port: int = Field(default=8000, alias="WEB_PORT")

    # Dashboard Public Authentication
    dashboard_auth_enabled: bool = Field(default=True, alias="DASHBOARD_AUTH_ENABLED")
    dashboard_auth_email: str = Field(default="spushkar812@gmail.com", alias="DASHBOARD_AUTH_EMAIL")
    dashboard_auth_password: str = Field(default="Pushkar@9084", alias="DASHBOARD_AUTH_PASSWORD")
    dashboard_session_secret: str = Field(
        default="proaff_secret_session_token_key_2026_9084_sec",
        alias="DASHBOARD_SESSION_SECRET"
    )

    # General
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    debug: bool = Field(default=False, alias="DEBUG")


settings = Settings()
