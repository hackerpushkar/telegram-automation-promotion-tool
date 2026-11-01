import os
import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.config import settings
from src.db import Database
from src.models import Lead, LeadType, LeadStatus, LeadSourceInfo
from src.extractor import extract_leads_from_text
from src.api_client import ProaffiliatersApiClient
from src.info_bot import info_bot
from src.channel_publisher import get_promo_post_preview, publish_promo_post, publish_all_promo_posts, BASE_PROMO_DIR
from src.bot_forwarder import bot_forwarder
from src.account_manager import AccountManager
from src.drip_engine import DripEngine
from src.keyword_search_engine import KeywordSearchEngine
from src.auth import (
    create_session_token,
    verify_session_token,
    authenticate_user,
    get_current_user_email,
    is_authenticated,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Telegram Promotion Tool Dashboard",
    description="ProAffiliaters Lead Extraction & Management Interface",
    version="1.0.0"
)

# Mount promotional assets directory for web UI thumbnails
if os.path.exists(BASE_PROMO_DIR):
    app.mount("/promo-assets", StaticFiles(directory=BASE_PROMO_DIR), name="promo-assets")


@app.middleware("http")
async def auth_guard_middleware(request: Request, call_next):
    path = request.url.path

    # Public paths exempt from authentication
    if (
        not settings.dashboard_auth_enabled
        or path in ["/login", "/api/auth/login", "/api/auth/status", "/api/auth/logout", "/favicon.ico"]
        or path.startswith("/promo-assets/")
    ):
        return await call_next(request)

    # Protect all /api/* routes
    if path.startswith("/api/"):
        user = get_current_user_email(request)
        if not user:
            return JSONResponse(
                status_code=401,
                content={"error": "Authentication required. Please log in at /login."}
            )

    return await call_next(request)

# In-memory recent activity log for live UI stream
activity_logs: List[Dict[str, Any]] = [
    {
        "timestamp": datetime.utcnow().strftime("%H:%M:%S"),
        "type": "info",
        "message": "Telegram Promotion Tool dashboard initialized."
    },
    {
        "timestamp": datetime.utcnow().strftime("%H:%M:%S"),
        "type": "sync",
        "message": f"Connected to ProAffiliaters API ({settings.proaffiliaters_api_url}) at 30-minute intervals."
    }
]


def log_activity(message: str, log_type: str = "info"):
    """Adds an entry to the live activity log."""
    entry = {
        "timestamp": datetime.utcnow().strftime("%H:%M:%S"),
        "type": log_type,
        "message": message
    }
    activity_logs.insert(0, entry)
    if len(activity_logs) > 100:
        activity_logs.pop()


# --- Pydantic Schemas for API Requests ---
class LoginRequest(BaseModel):
    email: str
    password: str


class StatusUpdateRequest(BaseModel):
    status: str


class ScanTestRequest(BaseModel):
    bio_text: str
    channel_name: Optional[str] = "Manual Test Channel"
    invite_link: Optional[str] = "https://t.me/test_channel"


class BroadcastRequest(BaseModel):
    message_text: str
    buttons_markup: Optional[str] = None
    photo_url: Optional[str] = None


class PublishChannelRequest(BaseModel):
    post_number: int  # 1 to 5, or 0 for all
    channel_username: Optional[str] = None


class TestForwardRequest(BaseModel):
    bot_username: str
    post_number: int
    channel_message_id: Optional[int] = None
    channel_username: Optional[str] = None


class StartCampaignRequest(BaseModel):
    post_number: int
    channel_message_id: int
    channel_username: Optional[str] = None
    delay_seconds: int = 8
    target_limit: int = 100


class CampaignControlRequest(BaseModel):
    action: str  # pause, resume, stop

class TelegramApiConfigRequest(BaseModel):
    api_id: int
    api_hash: str


class StartAccountLoginRequest(BaseModel):
    phone: str
    slot: Optional[int] = None


class CompleteAccountLoginRequest(BaseModel):
    session_token: str
    code: Optional[str] = None
    password: Optional[str] = None


class ExecuteDripStageRequest(BaseModel):
    stage: int
    batch_size: int = 20
    delay_seconds: float = 12.0


class AddKeywordRequest(BaseModel):
    keyword: str
    category: Optional[str] = "deals"


class ToggleKeywordRequest(BaseModel):
    enabled: bool


class SearchKeywordNowRequest(BaseModel):
    keyword: Optional[str] = None
    limit: int = 15


# --- API Routes ---

@app.post("/api/auth/login")
async def login_endpoint(req: LoginRequest):
    """Authenticates admin and returns a signed 30-day session token."""
    if not authenticate_user(req.email, req.password):
        log_activity(f"Failed dashboard login attempt for email: '{req.email}'", "info")
        return JSONResponse(
            status_code=401,
            content={"error": "Invalid email or password. Please verify your credentials."}
        )

    clean_email = req.email.strip().lower()
    token = create_session_token(clean_email)
    log_activity(f"Dashboard login successful for '{clean_email}'", "sync")

    res = JSONResponse(content={
        "success": True,
        "email": clean_email,
        "token": token,
        "message": "Login successful. Welcome to ProAffiliaters Lead Engine!"
    })
    res.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False  # Compatible across HTTP reverse-proxy and HTTPS
    )
    return res


@app.post("/api/auth/logout")
async def logout_endpoint():
    """Logs out admin by clearing the session token cookie."""
    res = JSONResponse(content={"success": True, "message": "Logged out successfully."})
    res.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return res


@app.get("/api/auth/status")
async def auth_status_endpoint(request: Request):
    """Returns current session authentication state."""
    user = get_current_user_email(request)
    return {
        "authenticated": user is not None,
        "email": user,
        "auth_enabled": settings.dashboard_auth_enabled
    }

@app.get("/api/stats")
async def get_dashboard_stats():
    """Returns real-time lead counts and system status."""
    stats = await Database.get_stats()
    stats["api_url"] = settings.proaffiliaters_api_url
    stats["sync_interval"] = f"{settings.sync_interval_minutes} Minutes"
    stats["telegram_configured"] = bool(settings.telegram_api_id and settings.telegram_api_hash)
    return stats


@app.get("/api/leads")
async def list_leads(
    search: Optional[str] = Query(None),
    lead_type: Optional[str] = Query("all"),
    status: Optional[str] = Query("all"),
    limit: int = Query(100, ge=1, le=500),
    skip: int = Query(0, ge=0)
):
    """Returns filtered leads."""
    leads = await Database.get_leads(
        search=search,
        lead_type=lead_type,
        status=status,
        limit=limit,
        skip=skip
    )
    return {"count": len(leads), "leads": leads}


@app.patch("/api/leads/{username_clean}/status")
async def update_lead_status(username_clean: str, req: StatusUpdateRequest):
    """Updates a lead's outreach status."""
    success = await Database.update_status(username_clean, req.status)
    if success:
        log_activity(f"Updated lead @{username_clean} status to '{req.status}'", "update")
        return {"success": True, "username": username_clean, "status": req.status}
    return JSONResponse(status_code=404, content={"error": "Lead not found"})


@app.post("/api/scan-test")
async def scan_test_bio(req: ScanTestRequest):
    """Extracts leads from arbitrary bio text or invite link for testing."""
    leads = extract_leads_from_text(
        bio_text=req.bio_text,
        source_name=req.channel_name or "Test Channel",
        source_invite_link=req.invite_link or "",
        source_id="manual_test"
    )
    # Save the extracted leads into the database
    saved_count = 0
    for lead in leads:
        is_new, _ = await Database.upsert_lead(lead)
        if is_new:
            saved_count += 1
            log_activity(f"Extracted & saved new lead: @{lead.username} ({lead.lead_type.value})", "lead")

    return {
        "extracted_count": len(leads),
        "newly_saved": saved_count,
        "leads": [l.model_dump() for l in leads]
    }


@app.post("/api/trigger-sync")
async def trigger_manual_sync():
    """Triggers an immediate external source fetch from ProAffiliaters API."""
    log_activity("Manual 30-minute sync cycle initiated by user from Web UI.", "sync")
    api_client = ProaffiliatersApiClient()
    sources = await api_client.fetch_recent_sources()
    
    if not sources:
        log_activity("External API sync finished: No new source channels found in current window.", "info")
        return {"status": "none", "message": "No new sources found in window", "sources_count": 0}

    log_activity(f"External API sync returned {len(sources)} source channel(s).", "sync")
    return {
        "status": "success",
        "sources_count": len(sources),
        "sources": [s.model_dump() for s in sources]
    }


@app.get("/api/logs")
async def get_activity_logs():
    """Returns recent activity log stream."""
    return {"logs": activity_logs}


# --- Official Info Bot (@ProaffiliatersInfoBot) REST Endpoints ---

@app.get("/api/info-bot/status")
async def get_info_bot_status():
    """Returns operational status and lead counts of @ProaffiliatersInfoBot."""
    return await info_bot.get_status()


@app.get("/api/info-bot/users")
async def get_info_bot_users_list(
    search: Optional[str] = Query(None),
    is_blocked: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0)
):
    """Returns paginated leads registered with @ProaffiliatersInfoBot."""
    users = await Database.get_info_bot_users(
        search=search,
        is_blocked=is_blocked,
        limit=limit,
        skip=skip
    )
    return {"count": len(users), "users": users}


@app.get("/api/info-bot/broadcasts")
async def get_info_bot_broadcasts_list(limit: int = Query(20, ge=1, le=50)):
    """Returns recent broadcast audit history."""
    broadcasts = await Database.get_info_bot_broadcasts(limit=limit)
    return {"count": len(broadcasts), "broadcasts": broadcasts}


@app.post("/api/info-bot/broadcast")
async def send_info_bot_broadcast_endpoint(req: BroadcastRequest):
    """Triggers a background broadcast across all registered active Info Bot leads."""
    if not req.message_text or not req.message_text.strip():
        return JSONResponse(status_code=400, content={"error": "message_text is required"})

    log_activity(f"Admin triggered Info Bot broadcast: '{req.message_text[:35]}...'", "broadcast")
    res = await info_bot.broadcast_from_api(
        message_text=req.message_text.strip(),
        buttons_markup=req.buttons_markup,
        photo_url=req.photo_url
    )
    return res


# --- Admin Bot Outreach REST Endpoints ---

@app.get("/api/bot-outreach/posts")
async def get_bot_outreach_posts():
    """Returns previews and channel publication statuses of all 5 promo posts."""
    published_map = {p["post_number"]: p for p in await Database.get_all_published_posts()}
    posts = []
    for num in range(1, 6):
        preview = get_promo_post_preview(num)
        pub = published_map.get(num)
        posts.append({
            "post_number": num,
            "title": preview["title"],
            "image_name": preview["image_name"],
            "image_url": f"/promo-assets/{preview['image_name']}",
            "caption": preview["caption"],
            "deep_link": preview["deep_link"],
            "is_published": pub is not None,
            "channel_username": pub.get("channel_username") if pub else settings.promo_channel_username,
            "message_id": pub.get("message_id") if pub else None,
            "message_link": pub.get("message_link") if pub else None,
            "published_at": pub.get("published_at") if pub else None,
        })
    return {"posts": posts, "promo_channel": settings.promo_channel_username}


@app.post("/api/bot-outreach/publish")
async def publish_to_channel_endpoint(req: PublishChannelRequest):
    """Publishes one or all promotional posts into @pro_affiliaters with deep-link inline buttons."""
    try:
        if req.post_number == 0:
            results = await publish_all_promo_posts(req.channel_username)
            log_activity(f"Published all 5 promotional posts to {req.channel_username or settings.promo_channel_username}", "sync")
            return {"success": True, "results": results}
        else:
            rec = await publish_promo_post(req.post_number, req.channel_username)
            log_activity(f"Published Post #{req.post_number} to channel (Msg #{rec['message_id']})", "sync")
            return {"success": True, "published": rec}
    except Exception as e:
        logger.exception("Failed to publish to channel: %s", e)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@app.get("/api/bot-outreach/status")
async def get_bot_outreach_status():
    """Returns live forwarder status, uncontacted bot leads, and campaign history."""
    status = bot_forwarder.get_status()
    uncontacted = await Database.get_uncontacted_bots(limit=500)
    campaigns = await Database.get_bot_campaigns(limit=10)
    return {
        **status,
        "uncontacted_bots_count": len(uncontacted),
        "uncontacted_bots": [b.get("username") for b in uncontacted[:20]],
        "promo_channel": settings.promo_channel_username,
        "campaigns": campaigns
    }


@app.post("/api/bot-outreach/test-forward")
async def test_forward_endpoint(req: TestForwardRequest):
    """Forwards a published post to a single bot handle for testing/dry-run."""
    clean_bot = req.bot_username.strip().lstrip("@")
    if not clean_bot:
        return JSONResponse(status_code=400, content={"error": "bot_username is required"})

    msg_id = req.channel_message_id
    if not msg_id:
        pub = await Database.get_published_post(req.post_number)
        if pub:
            msg_id = pub.get("message_id")

    if not msg_id:
        return JSONResponse(
            status_code=400,
            content={"error": f"Post #{req.post_number} has not been published yet! Publish it to the channel first."}
        )

    res = await bot_forwarder.forward_to_single_bot(
        bot_username=clean_bot,
        channel_message_id=msg_id,
        channel_username=req.channel_username
    )

    if res.get("success"):
        log_activity(f"Test forwarded Post #{req.post_number} to @{clean_bot}", "lead")
    else:
        log_activity(f"Test forward to @{clean_bot} failed: {res.get('error')}", "info")

    return res


@app.post("/api/bot-outreach/start-campaign")
async def start_bot_campaign_endpoint(req: StartCampaignRequest):
    """Starts a batch forwarding campaign targeting uncontacted bot leads from MongoDB."""
    try:
        res = await bot_forwarder.start_campaign(
            post_number=req.post_number,
            channel_message_id=req.channel_message_id,
            channel_username=req.channel_username,
            delay_seconds=req.delay_seconds,
            target_limit=req.target_limit
        )
        log_activity(f"Started Bot Outreach Campaign for Post #{req.post_number} ({res.get('total_targeted')} bots)", "broadcast")
        return {"success": True, "campaign": res}
    except Exception as e:
        logger.exception("Failed to start bot campaign: %s", e)
        return JSONResponse(status_code=400, content={"success": False, "error": str(e)})


@app.post("/api/bot-outreach/control")
async def control_bot_campaign_endpoint(req: CampaignControlRequest):
    """Pauses, resumes, or stops the running bot outreach campaign."""
    action = req.action.lower().strip()
    if action == "pause":
        ok = bot_forwarder.pause_campaign()
    elif action == "resume":
        ok = bot_forwarder.resume_campaign()
    elif action == "stop":
        ok = bot_forwarder.stop_campaign()
    else:
        return JSONResponse(status_code=400, content={"error": f"Invalid action '{req.action}'. Expected pause/resume/stop."})

    return {"success": ok, "status": bot_forwarder.get_status()}


# --- Multi-Account Management Endpoints ---

@app.get("/api/accounts")
async def get_accounts_list():
    """Returns all registered Telegram accounts and their slot assignments."""
    accounts = await Database.get_all_telegram_accounts()
    # Mask session strings for security
    safe_accounts = []
    for acc in accounts:
        safe_copy = dict(acc)
        safe_copy["session_string"] = "********" if safe_copy.get("session_string") else ""
        safe_accounts.append(safe_copy)
    return {"count": len(safe_accounts), "accounts": safe_accounts}


@app.post("/api/accounts/login/start")
async def start_account_login_endpoint(req: StartAccountLoginRequest):
    """Initiates phone login by requesting OTP code from Telegram MTProto."""
    try:
        res = await AccountManager.start_phone_login(req.phone, req.slot)
        log_activity(f"Initiated Telegram login for {req.phone} (Assigned Slot #{res.get('slot')})", "sync")
        return res
    except Exception as e:
        logger.error("Failed to start phone login: %s", e)
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.post("/api/accounts/login/complete")
async def complete_account_login_endpoint(req: CompleteAccountLoginRequest):
    """Submits verification code (and 2FA password) to complete login."""
    try:
        res = await AccountManager.complete_phone_login(req.session_token, req.code, req.password)
        if res.get("status") == "success":
            acc = res.get("account", {})
            log_activity(f"Connected Telegram account {acc.get('phone')} into Slot #{acc.get('slot')}", "sync")
        return res
    except Exception as e:
        logger.error("Failed to complete phone login: %s", e)
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.post("/api/accounts/{phone}/ping")
async def ping_account_endpoint(phone: str):
    """Pings a Telegram account to check authorization and health status."""
    try:
        res = await AccountManager.ping_account(phone)
        return res
    except Exception as e:
        logger.error("Failed to ping account %s: %s", phone, e)
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.delete("/api/accounts/{phone}")
async def delete_account_endpoint(phone: str):
    """Removes a registered Telegram account from MongoDB."""
    ok = await Database.delete_telegram_account(phone)
    if ok:
        log_activity(f"Removed Telegram account {phone}", "info")
    return {"success": ok}


@app.post("/api/accounts/clear-demo")
async def clear_demo_accounts_endpoint():
    """Removes mock/demo accounts (+1999...) from MongoDB."""
    accounts = await Database.get_all_telegram_accounts()
    cleared = 0
    for acc in accounts:
        phone = acc.get("phone", "")
        if phone.startswith("+1999") or "manager_slot" in acc.get("first_name", "").lower():
            if await Database.delete_telegram_account(phone):
                cleared += 1
    log_activity(f"Cleared {cleared} demo Telegram accounts from database.", "info")
    return {"success": True, "cleared_count": cleared}


@app.post("/api/config/telegram")
async def save_telegram_config(req: TelegramApiConfigRequest):
    """Saves Telegram MTProto API ID and Hash into settings and .env."""
    clean_hash = req.api_hash.strip()
    if not req.api_id or not clean_hash:
        return JSONResponse(status_code=400, content={"error": "Both API ID and API HASH are required."})

    settings.telegram_api_id = req.api_id
    settings.telegram_api_hash = clean_hash

    env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            new_lines = []
            id_set = False
            hash_set = False
            for line in lines:
                if line.startswith("TELEGRAM_API_ID="):
                    new_lines.append(f"TELEGRAM_API_ID={req.api_id}\n")
                    id_set = True
                elif line.startswith("TELEGRAM_API_HASH="):
                    new_lines.append(f"TELEGRAM_API_HASH={clean_hash}\n")
                    hash_set = True
                else:
                    new_lines.append(line)
            if not id_set:
                new_lines.append(f"TELEGRAM_API_ID={req.api_id}\n")
            if not hash_set:
                new_lines.append(f"TELEGRAM_API_HASH={clean_hash}\n")
            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
        except Exception as e:
            logger.error("Failed to write to .env: %s", e)

    log_activity(f"Configured Telegram API credentials (API ID: {req.api_id}).", "sync")
    return {"success": True, "telegram_configured": True}


# --- 5-Week Drip Funnel Endpoints ---

@app.get("/api/drip/overview")
async def get_drip_overview_endpoint():
    """Returns funnel analytics, ready bot counts, and 5-stage metadata."""
    overview = await DripEngine.get_drip_overview()
    return overview


@app.post("/api/drip/execute-stage")
async def execute_drip_stage_endpoint(req: ExecuteDripStageRequest):
    """Executes a single week/stage forwarding batch using that stage's assigned account."""
    try:
        log_activity(f"Starting 5-Week Drip Stage #{req.stage} batch forwarding...", "broadcast")
        res = await DripEngine.execute_drip_stage(req.stage, req.batch_size, req.delay_seconds)
        log_activity(
            f"Stage #{req.stage} batch finished: {res.get('successful')}/{res.get('targeted')} delivered successfully.",
            "broadcast"
        )
        return res
    except Exception as e:
        logger.exception("Failed to execute drip stage #%d: %s", req.stage, e)
        return JSONResponse(status_code=400, content={"error": str(e)})


# --- Autonomous Telegram Keyword Search Endpoints ---

@app.get("/api/keywords")
async def get_keywords_overview():
    """Returns all configured target keywords, active statuses, and KPI stats."""
    keywords = await Database.get_search_keywords()
    stats = await Database.get_keyword_search_stats()
    return {
        "status": "success",
        "stats": stats,
        "keywords": keywords,
        "autonomous_enabled": settings.keyword_search_enabled,
        "interval_minutes": settings.keyword_search_interval_minutes
    }


@app.post("/api/keywords")
async def add_keyword_endpoint(req: AddKeywordRequest):
    """Adds a new target keyword for automated channel discovery."""
    try:
        doc = await Database.add_search_keyword(req.keyword, req.category or "deals")
        log_activity(f"Added target keyword '{req.keyword}' in category '{req.category}'", "sync")
        return {"status": "success", "keyword": doc}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.patch("/api/keywords/{keyword}/toggle")
async def toggle_keyword_endpoint(keyword: str, req: ToggleKeywordRequest):
    """Enables or disables an automated search keyword."""
    success = await Database.toggle_search_keyword(keyword, req.enabled)
    if success:
        action = "enabled" if req.enabled else "paused"
        log_activity(f"Keyword '{keyword}' {action}", "update")
        return {"status": "success", "keyword": keyword, "enabled": req.enabled}
    return JSONResponse(status_code=404, content={"error": "Keyword not found"})


@app.delete("/api/keywords/{keyword}")
async def delete_keyword_endpoint(keyword: str):
    """Deletes a target search keyword."""
    success = await Database.delete_search_keyword(keyword)
    if success:
        log_activity(f"Deleted target keyword '{keyword}'", "update")
        return {"status": "success", "keyword": keyword}
    return JSONResponse(status_code=404, content={"error": "Keyword not found"})


@app.post("/api/keywords/search-now")
async def search_keyword_now_endpoint(req: SearchKeywordNowRequest):
    """Executes an immediate live MTProto search for a keyword and extracts leads."""
    if not req.keyword:
        return JSONResponse(status_code=400, content={"error": "Keyword must be provided."})

    log_activity(f"Starting live Telegram directory search for '{req.keyword}'...", "sync")
    res = await KeywordSearchEngine.search_keyword(req.keyword, limit=req.limit or 15)
    if res.get("status") == "success":
        log_activity(
            f"Search '{req.keyword}' complete: Discovered {res.get('channels_found')} channels, extracted {res.get('leads_extracted')} leads.",
            "sync"
        )
    return res


@app.post("/api/keywords/autonomous/cycle")
async def run_autonomous_cycle_endpoint():
    """Triggers one full autonomous discovery cycle across oldest searched keywords."""
    log_activity("Triggered manual autonomous keyword discovery cycle...", "sync")
    res = await KeywordSearchEngine.run_autonomous_cycle(max_keywords=2)
    return res


@app.get("/api/keywords/channels")
async def get_discovered_channels_endpoint(
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
    search: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None)
):
    """Returns channels discovered via Telegram keyword searches."""
    channels = await Database.get_discovered_channels(limit=limit, skip=skip, search=search, keyword=keyword)
    return {"count": len(channels), "channels": channels}


# --- Web UI Main Page (Pure Clean Modern Light Mode) ---
@app.get("/login", response_class=HTMLResponse)
async def serve_login(request: Request):
    """Serves the login page or redirects to dashboard if already authenticated."""
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)
    return LOGIN_HTML_CONTENT


@app.get("/logout")
async def logout_redirect():
    """Logs out by clearing cookie and redirecting to login page."""
    res = RedirectResponse(url="/login", status_code=302)
    res.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return res


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    """Serves the dashboard for authenticated users or the login screen if unauthenticated."""
    if settings.dashboard_auth_enabled and not is_authenticated(request):
        return LOGIN_HTML_CONTENT
    return HTML_CONTENT


LOGIN_HTML_CONTENT = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Sign In | ProAffiliaters Telegram Lead Engine</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=Outfit:wght@500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-body: #f4f7fe;
      --bg-card: #ffffff;
      --text-main: #1a1f5e;
      --text-muted: #475569;
      --text-subtle: #6b78c4;
      --primary: #3b5bfa;
      --primary-hover: #2f4fe6;
      --primary-light: #eef2ff;
      --border-subtle: rgba(59, 91, 250, 0.12);
      --danger: #ef4444;
      --danger-light: #fef2f2;
      --success: #10b981;
      --radius-lg: 16px;
      --radius-md: 10px;
      --shadow-lg: 0 20px 40px -15px rgba(59, 91, 250, 0.15);
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    }

    body {
      background: var(--bg-body);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
      position: relative;
      overflow-x: hidden;
    }

    .bg-sphere-1 {
      position: absolute;
      width: 480px;
      height: 480px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(59, 91, 250, 0.12) 0%, rgba(244, 247, 254, 0) 70%);
      top: -120px;
      left: -100px;
      pointer-events: none;
    }

    .bg-sphere-2 {
      position: absolute;
      width: 420px;
      height: 420px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(139, 92, 246, 0.10) 0%, rgba(244, 247, 254, 0) 70%);
      bottom: -100px;
      right: -80px;
      pointer-events: none;
    }

    .login-container {
      width: 100%;
      max-width: 440px;
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-lg);
      padding: 36px 32px;
      box-shadow: var(--shadow-lg);
      position: relative;
      z-index: 10;
    }

    .brand-header {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 24px;
    }

    .brand-logo-box {
      width: 44px;
      height: 44px;
      border-radius: 12px;
      background: linear-gradient(135deg, #3b5bfa 0%, #2f4fe6 100%);
      display: flex;
      align-items: center;
      justify-content: center;
      color: #fff;
      font-size: 24px;
      font-weight: 800;
      font-family: 'Outfit', sans-serif;
      box-shadow: 0 8px 16px rgba(59, 91, 250, 0.25);
    }

    .brand-title-wrap {
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .brand-title {
      font-family: 'Outfit', sans-serif;
      font-size: 20px;
      font-weight: 800;
      color: var(--text-main);
      letter-spacing: -0.02em;
    }

    .brand-pill {
      font-size: 9px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      padding: 3px 7px;
      background: var(--primary-light);
      color: var(--primary);
      border-radius: 20px;
      border: 1px solid rgba(59, 91, 250, 0.2);
    }

    .brand-subtitle {
      font-size: 12px;
      color: var(--text-subtle);
      margin-top: 1px;
    }

    .form-heading {
      font-size: 20px;
      font-weight: 800;
      color: var(--text-main);
      margin-bottom: 6px;
      letter-spacing: -0.01em;
    }

    .form-subtext {
      font-size: 13px;
      color: var(--text-muted);
      margin-bottom: 24px;
      line-height: 1.5;
    }

    .alert-box {
      padding: 12px 14px;
      border-radius: var(--radius-md);
      font-size: 13px;
      font-weight: 500;
      margin-bottom: 20px;
      display: none;
      align-items: center;
      gap: 10px;
      animation: fadeIn 0.2s ease-in-out;
    }

    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(-4px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .alert-error {
      background: var(--danger-light);
      border: 1px solid #fecaca;
      color: #b91c1c;
    }

    .alert-success {
      background: #ecfdf5;
      border: 1px solid #a7f3d0;
      color: #065f46;
    }

    .form-group {
      margin-bottom: 18px;
    }

    .form-label {
      display: block;
      font-size: 12px;
      font-weight: 600;
      color: var(--text-main);
      margin-bottom: 6px;
    }

    .input-wrapper {
      position: relative;
      display: flex;
      align-items: center;
    }

    .input-icon {
      position: absolute;
      left: 14px;
      color: var(--text-subtle);
      font-size: 15px;
      pointer-events: none;
    }

    .form-input {
      width: 100%;
      padding: 12px 14px 12px 42px;
      border: 1.5px solid var(--border-subtle);
      border-radius: var(--radius-md);
      font-size: 14px;
      color: var(--text-main);
      background: #fff;
      transition: all 0.2s ease;
      outline: none;
    }

    .form-input:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(59, 91, 250, 0.12);
    }

    .toggle-password-btn {
      position: absolute;
      right: 12px;
      background: transparent;
      border: none;
      cursor: pointer;
      color: var(--text-subtle);
      font-size: 16px;
      padding: 4px;
    }

    .btn-submit {
      width: 100%;
      padding: 13px;
      border-radius: var(--radius-md);
      border: none;
      background: linear-gradient(135deg, #3b5bfa 0%, #2f4fe6 100%);
      color: #ffffff;
      font-size: 14px;
      font-weight: 700;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      box-shadow: 0 4px 12px rgba(59, 91, 250, 0.25);
      transition: all 0.2s ease;
      margin-top: 24px;
    }

    .btn-submit:hover {
      background: linear-gradient(135deg, #2f4fe6 0%, #2341d4 100%);
      transform: translateY(-1px);
      box-shadow: 0 6px 16px rgba(59, 91, 250, 0.35);
    }

    .btn-submit:disabled {
      opacity: 0.65;
      cursor: not-allowed;
      transform: none;
    }

    .login-footer {
      margin-top: 24px;
      padding-top: 18px;
      border-top: 1px solid var(--border-subtle);
      text-align: center;
      font-size: 11px;
      color: var(--text-subtle);
    }

    .spinner {
      width: 16px;
      height: 16px;
      border: 2px solid rgba(255, 255, 255, 0.4);
      border-top-color: #ffffff;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }

    @keyframes spin {
      to { transform: rotate(360deg); }
    }
  </style>
</head>
<body>
  <div class="bg-sphere-1"></div>
  <div class="bg-sphere-2"></div>

  <div class="login-container">
    <div class="brand-header">
      <div class="brand-logo-box">P</div>
      <div>
        <div class="brand-title-wrap">
          <span class="brand-title">ProAffiliaters</span>
          <span class="brand-pill">LEAD ENGINE</span>
        </div>
        <div class="brand-subtitle">Telegram Automation Suite</div>
      </div>
    </div>

    <h1 class="form-heading">Sign In to Dashboard</h1>
    <p class="form-subtext">Access your 24/7 autonomous Telegram lead extraction and outreach campaigns.</p>

    <div id="alert-box" class="alert-box">
      <span id="alert-icon">⚠️</span>
      <span id="alert-msg">Error message</span>
    </div>

    <form id="login-form" onsubmit="event.preventDefault(); submitLogin();">
      <div class="form-group">
        <label class="form-label" for="login-email">Admin Email Address</label>
        <div class="input-wrapper">
          <span class="input-icon">✉️</span>
          <input
            type="email"
            id="login-email"
            class="form-input"
            placeholder="Enter admin email address"
            required
            autocomplete="email"
          />
        </div>
      </div>

      <div class="form-group">
        <label class="form-label" for="login-password">Password</label>
        <div class="input-wrapper">
          <span class="input-icon">🔒</span>
          <input
            type="password"
            id="login-password"
            class="form-input"
            placeholder="Enter dashboard password"
            required
            autocomplete="current-password"
          />
          <button type="button" class="toggle-password-btn" onclick="togglePasswordVisibility()" title="Toggle visibility">
            👁️
          </button>
        </div>
      </div>

      <button type="submit" id="btn-submit" class="btn-submit">
        <span>🔐 Sign In to Dashboard</span>
      </button>
    </form>

    <div class="login-footer">
      Protected with HMAC-SHA256 encrypted 30-day session tokens.<br />
      ProAffiliaters Lead Promotion Engine &copy; 2026.
    </div>
  </div>

  <script>
    function togglePasswordVisibility() {
      const pass = document.getElementById('login-password');
      if (pass.type === 'password') {
        pass.type = 'text';
      } else {
        pass.type = 'password';
      }
    }

    function showAlert(msg, isSuccess = false) {
      const box = document.getElementById('alert-box');
      const msgSpan = document.getElementById('alert-msg');
      const iconSpan = document.getElementById('alert-icon');
      
      box.className = 'alert-box ' + (isSuccess ? 'alert-success' : 'alert-error');
      iconSpan.textContent = isSuccess ? '✅' : '⚠️';
      msgSpan.textContent = msg;
      box.style.display = 'flex';
    }

    async function submitLogin() {
      const email = document.getElementById('login-email').value.trim();
      const password = document.getElementById('login-password').value;
      const btn = document.getElementById('btn-submit');

      if (!email || !password) {
        showAlert('Please enter both your email address and password.');
        return;
      }

      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> <span>Verifying credentials...</span>';

      try {
        const res = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, password })
        });
        const data = await res.json();

        if (res.ok && data.success) {
          showAlert('Login successful! Redirecting to dashboard...', true);
          setTimeout(() => {
            window.location.href = '/';
          }, 300);
        } else {
          showAlert(data.error || 'Authentication failed. Please verify your credentials.');
          btn.disabled = false;
          btn.innerHTML = '<span>🔐 Sign In to Dashboard</span>';
        }
      } catch (err) {
        showAlert('Network error connecting to login server.');
        btn.disabled = false;
        btn.innerHTML = '<span>🔐 Sign In to Dashboard</span>';
      }
    }
  </script>
</body>
</html>
"""


HTML_CONTENT = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ProAffiliaters | Telegram Lead Engine</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=Outfit:wght@500;600;700&display=swap" rel="stylesheet">
  <style>
    /* =========================================================
       PROAFFILIATERS SIGNATURE SYSTEM (PREMIUM SAAS SIDEBAR THEME)
       ========================================================= */
    :root {
      --bg-body: #f4f7fe;
      --bg-card: #ffffff;
      --bg-card-subtle: #f8faff;
      --bg-input: #ffffff;
      
      --border-subtle: rgba(59, 91, 250, 0.1);
      --border-strong: rgba(59, 91, 250, 0.2);
      
      --text-main: #1a1f5e;
      --text-muted: #475569;
      --text-subtle: #6b78c4;
      
      /* ProAffiliaters Signature Palette */
      --primary: #3b5bfa;
      --primary-hover: #2f4fe6;
      --primary-light: #eef2ff;
      --primary-border: rgba(59, 91, 250, 0.2);
      
      --success: #059669;
      --success-light: #d1fae5;
      --success-border: #a7f3d0;
      
      --warning: #d97706;
      --warning-light: #fef3c7;
      
      --info: #0284c7;
      --info-light: #e0f2fe;
      
      --purple: #7c3aed;
      --purple-light: #ede9fe;
      
      --radius-sm: 8px;
      --radius-md: 12px;
      --radius-lg: 16px;
      --radius-full: 9999px;
      
      --shadow-sm: 0 1px 3px rgba(59, 91, 250, 0.05);
      --shadow-md: 0 4px 20px rgba(59, 91, 250, 0.06);
      --shadow-lg: 0 10px 30px rgba(59, 91, 250, 0.1);
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      background-color: var(--bg-body);
      color: var(--text-main);
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
      overflow-x: hidden;
    }

    /* Master Layout Shell */
    .app-shell {
      display: flex;
      min-height: 100vh;
      width: 100%;
      position: relative;
    }

    /* Left Sidebar */
    .app-sidebar {
      width: 270px;
      flex-shrink: 0;
      background: #ffffff;
      border-right: 1px solid var(--border-subtle);
      box-shadow: 2px 0 24px rgba(59, 91, 250, 0.05);
      display: flex;
      flex-direction: column;
      position: sticky;
      top: 0;
      height: 100vh;
      overflow-y: auto;
      z-index: 100;
      transition: transform 0.26s cubic-bezier(0.4, 0, 0.2, 1);
    }

    .sidebar-brand {
      padding: 20px 20px 18px;
      display: flex;
      align-items: center;
      gap: 12px;
      border-bottom: 1px solid var(--border-subtle);
    }

    .brand-logo-box {
      width: 40px;
      height: 40px;
      border-radius: 12px;
      background: linear-gradient(135deg, #3b5bfa 0%, #7c3aed 100%);
      color: white;
      display: flex;
      align-items: center;
      justify-content: center;
      font-family: 'Outfit', sans-serif;
      font-size: 21px;
      font-weight: 800;
      box-shadow: 0 4px 14px rgba(59, 91, 250, 0.35);
      flex-shrink: 0;
    }

    .brand-title {
      font-family: 'Outfit', sans-serif;
      font-size: 17px;
      font-weight: 800;
      color: var(--text-main);
      display: flex;
      align-items: center;
      gap: 6px;
      line-height: 1.2;
    }

    .brand-pill {
      font-size: 9px;
      font-weight: 800;
      padding: 2px 6px;
      border-radius: 6px;
      background: var(--primary-light);
      color: var(--primary);
      border: 1px solid var(--primary-border);
      letter-spacing: 0.5px;
    }

    .brand-subtitle {
      font-size: 11.5px;
      color: var(--text-subtle);
      font-weight: 500;
      margin-top: 2px;
    }

    .sidebar-menu {
      padding: 14px 14px 20px;
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 2px;
    }

    .menu-section-label {
      font-size: 10.5px;
      font-weight: 700;
      color: var(--text-subtle);
      text-transform: uppercase;
      letter-spacing: 0.8px;
      padding: 16px 10px 6px;
    }

    .menu-section-label:first-child {
      padding-top: 4px;
    }

    .nav-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 10px 12px;
      border-radius: 10px;
      font-size: 13.5px;
      font-weight: 500;
      color: #475569;
      background: transparent;
      border: none;
      cursor: pointer;
      text-align: left;
      width: 100%;
      transition: all 0.16s ease;
      font-family: inherit;
      position: relative;
    }

    .nav-item-left {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .nav-icon {
      font-size: 16px;
      width: 22px;
      text-align: center;
      display: inline-block;
    }

    .nav-label {
      line-height: 1;
    }

    .nav-item:hover {
      background: #f0f3ff;
      color: var(--primary);
      transform: translateX(2px);
    }

    .nav-item.active {
      background: var(--primary-light);
      color: var(--primary);
      font-weight: 700;
      box-shadow: inset 3px 0 0 var(--primary);
    }

    .nav-badge {
      font-size: 11px;
      font-weight: 700;
      padding: 2px 7px;
      border-radius: 9999px;
      background: #f1f5f9;
      color: #64748b;
      min-width: 20px;
      text-align: center;
      line-height: 1.3;
    }

    .nav-item.active .nav-badge {
      background: #dde3ff;
      color: var(--primary);
    }

    .badge-primary {
      background: #e0e7ff;
      color: var(--primary);
    }

    /* Sidebar Footer Status Widget */
    .sidebar-status-card {
      margin: 12px 14px 18px;
      background: #f8faff;
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-md);
      padding: 12px 14px;
    }

    .status-indicator-row {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      font-weight: 700;
      color: var(--success);
      margin-bottom: 4px;
    }

    .status-api-name {
      font-size: 11px;
      color: var(--text-subtle);
      margin-bottom: 8px;
      font-family: monospace;
    }

    .status-footer-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 11px;
      color: #64748b;
      border-top: 1px solid rgba(59, 91, 250, 0.08);
      padding-top: 8px;
    }

    .status-tag {
      background: #d1fae5;
      color: #059669;
      padding: 2px 6px;
      border-radius: 4px;
      font-weight: 700;
    }

    /* Main App Area */
    .main-wrapper {
      flex: 1;
      display: flex;
      flex-direction: column;
      min-width: 0;
      min-height: 100vh;
    }

    /* App Top Bar */
    .app-topbar {
      background: #ffffff;
      border-bottom: 1px solid var(--border-subtle);
      padding: 14px 28px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 40;
      box-shadow: 0 1px 3px rgba(59, 91, 250, 0.04);
    }

    .topbar-left {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .mobile-toggle-btn {
      display: none;
      background: transparent;
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 18px;
      cursor: pointer;
      color: var(--text-main);
    }

    .view-breadcrumb {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 13.5px;
    }

    .crumb-root {
      font-weight: 500;
      color: var(--text-subtle);
    }

    .crumb-separator {
      color: #cbd5e1;
    }

    .crumb-active {
      font-weight: 700;
      color: var(--text-main);
      font-family: 'Outfit', sans-serif;
    }

    .topbar-right {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .pill-active {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: var(--success-light);
      border: 1px solid var(--success-border);
      color: var(--success);
      padding: 6px 14px;
      border-radius: var(--radius-full);
      font-size: 12px;
      font-weight: 600;
    }

    .pulse-dot {
      width: 8px;
      height: 8px;
      background: var(--success);
      border-radius: 50%;
      box-shadow: 0 0 0 0 rgba(5, 150, 105, 0.7);
      animation: pulse 2s infinite;
    }

    @keyframes pulse {
      0% { box-shadow: 0 0 0 0 rgba(5, 150, 105, 0.6); }
      70% { box-shadow: 0 0 0 8px rgba(5, 150, 105, 0); }
      100% { box-shadow: 0 0 0 0 rgba(5, 150, 105, 0); }
    }

    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 9px 18px;
      font-size: 13px;
      font-weight: 600;
      border-radius: var(--radius-md);
      border: 1px solid transparent;
      cursor: pointer;
      transition: all 0.18s ease;
      font-family: inherit;
    }

    .btn-primary {
      background: linear-gradient(135deg, #3b5bfa 0%, #4338ca 100%);
      color: white;
      box-shadow: 0 2px 6px rgba(59, 91, 250, 0.25);
    }

    .btn-primary:hover {
      background: linear-gradient(135deg, #2f4fe6 0%, #3730a3 100%);
      box-shadow: 0 4px 12px rgba(59, 91, 250, 0.35);
      transform: translateY(-1px);
    }

    .btn-outline {
      background: var(--bg-card);
      border-color: var(--border-strong);
      color: var(--text-main);
    }

    .btn-outline:hover {
      background: var(--bg-card-subtle);
      border-color: var(--text-subtle);
    }

    /* Main Content Area */
    .main-content {
      padding: 24px 28px;
      flex: 1;
      width: 100%;
      max-width: 1560px;
    }

    .app-footer {
      padding: 16px 28px;
      text-align: center;
      font-size: 12px;
      color: var(--text-subtle);
      border-top: 1px solid var(--border-subtle);
      background: #ffffff;
    }

    /* Mobile Drawer */
    .sidebar-backdrop {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.45);
      backdrop-filter: blur(3px);
      z-index: 90;
    }

    @media (max-width: 1024px) {
      .app-sidebar {
        position: fixed;
        top: 0;
        left: 0;
        bottom: 0;
        transform: translateX(-100%);
        box-shadow: 4px 0 30px rgba(15, 23, 42, 0.15);
      }
      .app-sidebar.open {
        transform: translateX(0);
      }
      .sidebar-backdrop.open {
        display: block;
      }
      .mobile-toggle-btn {
        display: inline-flex !important;
      }
      .main-content {
        padding: 16px;
      }
      .app-topbar {
        padding: 12px 16px;
      }
    }

    /* Stat Cards Grid */
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }

    .stat-card {
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-lg);
      padding: 20px;
      box-shadow: var(--shadow-sm);
      position: relative;
      overflow: hidden;
      transition: transform 0.2s ease, box-shadow 0.2s ease;
    }

    .stat-card:hover {
      transform: translateY(-2px);
      box-shadow: var(--shadow-md);
      border-color: var(--primary-border);
    }

    .stat-card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
    }

    .stat-title {
      font-size: 12.5px;
      font-weight: 700;
      color: var(--text-subtle);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .stat-icon {
      width: 36px;
      height: 36px;
      border-radius: var(--radius-md);
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 18px;
    }

    .stat-value {
      font-size: 32px;
      font-weight: 800;
      color: var(--text-main);
      font-family: 'Outfit', sans-serif;
      line-height: 1.1;
      margin-bottom: 6px;
    }

    .stat-subtitle {
      font-size: 12px;
      color: var(--text-subtle);
      font-weight: 500;
    }

    /* Section Views */
    .view-content {
      display: none;
      animation: fadeIn 0.18s ease-in-out;
    }

    .view-content.active {
      display: block;
    }

    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(4px); }
      to { opacity: 1; transform: translateY(0); }
    }

    /* Filter Bar */
    .filter-bar {
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-lg);
      padding: 16px 20px;
      margin-bottom: 20px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      box-shadow: var(--shadow-sm);
    }

    .filter-left {
      display: flex;
      align-items: center;
      gap: 12px;
      flex: 1;
      min-width: 320px;
    }

    .search-box {
      position: relative;
      flex: 1;
    }

    .search-box input {
      width: 100%;
      padding: 10px 14px 10px 38px;
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-md);
      font-size: 13px;
      font-family: inherit;
      background: var(--bg-card);
      color: var(--text-main);
      outline: none;
      transition: border-color 0.15s ease;
    }

    .search-box input:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.1);
    }

    .search-icon {
      position: absolute;
      left: 12px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--text-subtle);
      pointer-events: none;
    }

    .type-pills {
      display: flex;
      background: var(--bg-card-subtle);
      padding: 3px;
      border-radius: var(--radius-md);
      gap: 4px;
    }

    .type-pill-btn {
      padding: 6px 14px;
      font-size: 12px;
      font-weight: 600;
      border: none;
      background: transparent;
      color: var(--text-subtle);
      border-radius: var(--radius-sm);
      cursor: pointer;
      transition: all 0.15s ease;
    }

    .type-pill-btn.active {
      background: var(--bg-card);
      color: var(--text-main);
      box-shadow: var(--shadow-sm);
    }

    .select-status {
      padding: 9px 14px;
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-md);
      font-size: 13px;
      font-family: inherit;
      background: var(--bg-card);
      color: var(--text-main);
      cursor: pointer;
      outline: none;
    }

    /* Leads Table */
    .table-container {
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow-sm);
      overflow-x: auto;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
    }

    thead th {
      background: var(--bg-card-subtle);
      padding: 14px 18px;
      font-size: 12px;
      font-weight: 700;
      color: var(--text-subtle);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      border-bottom: 1px solid var(--border-subtle);
    }

    tbody td {
      padding: 16px 18px;
      font-size: 13px;
      border-bottom: 1px solid var(--border-subtle);
      color: var(--text-main);
      vertical-align: middle;
    }

    tbody tr:last-child td {
      border-bottom: none;
    }

    tbody tr:hover {
      background-color: #fafbfc;
    }

    /* Lead Item Elements */
    .lead-profile {
      display: flex;
      align-items: center;
      gap: 12px;
    }

    .lead-avatar {
      width: 36px;
      height: 36px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 14px;
      flex-shrink: 0;
    }

    .avatar-user {
      background: var(--info-light);
      color: var(--info);
      border: 1px solid #bae6fd;
    }

    .avatar-bot {
      background: var(--purple-light);
      color: var(--purple);
      border: 1px solid #ddd6fe;
    }

    .lead-meta {
      display: flex;
      flex-direction: column;
    }

    .lead-username {
      font-weight: 700;
      color: var(--text-main);
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }

    .lead-username:hover {
      color: var(--primary);
    }

    .btn-copy {
      background: transparent;
      border: none;
      cursor: pointer;
      color: var(--text-subtle);
      font-size: 12px;
      padding: 2px 4px;
      border-radius: 4px;
      transition: color 0.15s ease;
    }

    .btn-copy:hover {
      color: var(--primary);
      background: var(--primary-light);
    }

    .badge-type {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 3px 10px;
      border-radius: var(--radius-full);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.3px;
    }

    .badge-user {
      background: var(--info-light);
      color: var(--info);
      border: 1px solid #bae6fd;
    }

    .badge-bot {
      background: var(--purple-light);
      color: var(--purple);
      border: 1px solid #ddd6fe;
    }

    .badge-status {
      display: inline-block;
      padding: 4px 10px;
      border-radius: var(--radius-full);
      font-size: 11px;
      font-weight: 700;
      cursor: pointer;
      border: 1px solid transparent;
    }

    .status-new {
      background: var(--success-light);
      color: var(--success);
      border-color: var(--success-border);
    }

    .status-contacted {
      background: var(--warning-light);
      color: var(--warning);
      border-color: #fde68a;
    }

    .status-converted {
      background: var(--primary-light);
      color: var(--primary);
      border-color: var(--primary-border);
    }

    .status-ignored {
      background: var(--bg-card-subtle);
      color: var(--text-subtle);
      border-color: var(--border-strong);
    }

    .channel-source {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }

    .channel-name {
      font-weight: 600;
      color: var(--text-main);
    }

    .channel-link {
      font-size: 11px;
      color: var(--text-subtle);
      text-decoration: none;
    }

    .channel-link:hover {
      color: var(--primary);
      text-decoration: underline;
    }

    .bio-snippet {
      max-width: 320px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      color: var(--text-muted);
      font-size: 12px;
      background: var(--bg-card-subtle);
      padding: 4px 8px;
      border-radius: 6px;
      border: 1px solid var(--border-subtle);
    }

    /* Live Activity Log Stream */
    .log-container {
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-lg);
      padding: 20px;
      box-shadow: var(--shadow-sm);
    }

    .log-entry {
      display: flex;
      align-items: flex-start;
      gap: 12px;
      padding: 10px 0;
      border-bottom: 1px solid var(--border-subtle);
      font-size: 13px;
    }

    .log-entry:last-child {
      border-bottom: none;
    }

    .log-time {
      font-family: monospace;
      color: var(--text-subtle);
      font-size: 12px;
      flex-shrink: 0;
    }

    .log-badge {
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
      flex-shrink: 0;
    }

    .log-sync { background: var(--primary-light); color: var(--primary); }
    .log-lead { background: var(--success-light); color: var(--success); }
    .log-info { background: var(--info-light); color: var(--info); }
    .log-update { background: var(--warning-light); color: var(--warning); }

    /* Playground / Scanner Test Card */
    .scanner-card {
      background: var(--bg-card);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-lg);
      padding: 24px;
      box-shadow: var(--shadow-sm);
      max-width: 900px;
    }

    .form-group {
      margin-bottom: 18px;
    }

    .form-group label {
      display: block;
      font-size: 13px;
      font-weight: 600;
      color: var(--text-main);
      margin-bottom: 6px;
    }

    .form-input, .form-textarea {
      width: 100%;
      padding: 11px 14px;
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-md);
      font-size: 13px;
      font-family: inherit;
      color: var(--text-main);
      background: var(--bg-card);
      outline: none;
      transition: border-color 0.15s ease;
    }

    .form-input:focus, .form-textarea:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.1);
    }

    /* Toast Notification */
    #toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: #0f172a;
      color: white;
      padding: 12px 20px;
      border-radius: var(--radius-md);
      font-size: 13px;
      font-weight: 600;
      box-shadow: var(--shadow-lg);
      transform: translateY(100px);
      opacity: 0;
      transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
      z-index: 9999;
    }

    #toast.show {
      transform: translateY(0);
      opacity: 1;
    }

    /* Empty state */
    .empty-state {
      text-align: center;
      padding: 48px 24px;
      color: var(--text-subtle);
    }

    .empty-icon {
      font-size: 40px;
      margin-bottom: 12px;
    }

    /* Footer */
    footer {
      border-top: 1px solid var(--border-subtle);
      background: var(--bg-card);
      padding: 16px 24px;
      text-align: center;
      font-size: 12px;
      color: var(--text-subtle);
    }
  </style>
</head>
<body>

  <!-- ProAffiliaters Master App Shell -->
  <div class="app-shell">

    <!-- Mobile Drawer Backdrop -->
    <div class="sidebar-backdrop" id="sidebar-backdrop" onclick="toggleSidebar(false)"></div>

    <!-- LEFT PREMIUM SIDEBAR -->
    <aside class="app-sidebar" id="app-sidebar">
      
      <!-- Brand Box -->
      <div class="sidebar-brand">
        <div class="brand-logo-box">P</div>
        <div class="brand-info">
          <div class="brand-title">
            <span>ProAffiliaters</span>
            <span class="brand-pill">LEAD ENGINE</span>
          </div>
          <div class="brand-subtitle">Telegram Automation & Drip</div>
        </div>
      </div>

      <!-- Navigation Sections -->
      <nav class="sidebar-menu">
        <div class="menu-section-label">Core Pipeline</div>

        <button class="nav-item active" data-tab="tab-leads" onclick="switchTab('tab-leads', this, 'Extracted Leads Directory')">
          <div class="nav-item-left">
            <span class="nav-icon">📋</span>
            <span class="nav-label">Extracted Leads</span>
          </div>
          <span class="nav-badge" id="leads-sidebar-count">0</span>
        </button>

        <button class="nav-item" data-tab="tab-accounts" onclick="switchTab('tab-accounts', this, 'Telegram Account Management')">
          <div class="nav-item-left">
            <span class="nav-icon">🔑</span>
            <span class="nav-label">Telegram Accounts</span>
          </div>
          <span class="nav-badge" id="accounts-sidebar-count">0</span>
        </button>

        <button class="nav-item" data-tab="tab-drip-funnel" onclick="switchTab('tab-drip-funnel', this, '5-Week Progressive Drip Sequence')">
          <div class="nav-item-left">
            <span class="nav-icon">📆</span>
            <span class="nav-label">5-Week Drip Funnel</span>
          </div>
          <span class="nav-badge badge-primary" id="drip-sidebar-count">5 Stages</span>
        </button>

        <button class="nav-item" data-tab="tab-keywords" onclick="switchTab('tab-keywords', this, 'Autonomous Telegram Keyword Search Engine')">
          <div class="nav-item-left">
            <span class="nav-icon">🔍</span>
            <span class="nav-label">Keyword Search</span>
          </div>
          <span class="nav-badge" id="keywords-sidebar-count">0</span>
        </button>

        <div class="menu-section-label">Outreach & Campaigns</div>

        <button class="nav-item" data-tab="tab-bot-outreach" onclick="switchTab('tab-bot-outreach', this, 'Telegram Bot Outreach Campaigns')">
          <div class="nav-item-left">
            <span class="nav-icon">🚀</span>
            <span class="nav-label">Bot Outreach</span>
          </div>
          <span class="nav-badge" id="bot-outreach-sidebar-count">0</span>
        </button>

        <button class="nav-item" data-tab="tab-infobot" onclick="switchTab('tab-infobot', this, 'Official Info Bot & Broadcasts')">
          <div class="nav-item-left">
            <span class="nav-icon">🤖</span>
            <span class="nav-label">Info Bot & Ads</span>
          </div>
          <span class="nav-badge" id="infobot-sidebar-count">0</span>
        </button>

        <div class="menu-section-label">Tools & System</div>

        <button class="nav-item" data-tab="tab-playground" onclick="switchTab('tab-playground', this, 'Live Channel Bio Scanner')">
          <div class="nav-item-left">
            <span class="nav-icon">🧪</span>
            <span class="nav-label">Bio Scanner</span>
          </div>
        </button>

        <button class="nav-item" data-tab="tab-logs" onclick="switchTab('tab-logs', this, 'Real-time Activity & Sync Logs')">
          <div class="nav-item-left">
            <span class="nav-icon">⚡</span>
            <span class="nav-label">Activity Logs</span>
          </div>
        </button>

        <button class="nav-item" data-tab="tab-settings" onclick="switchTab('tab-settings', this, 'ProAffiliaters Integration Details')">
          <div class="nav-item-left">
            <span class="nav-icon">⚙️</span>
            <span class="nav-label">Integration Specs</span>
          </div>
        </button>
      </nav>

      <!-- Sidebar Status Card -->
      <div class="sidebar-status-card">
        <div class="status-indicator-row">
          <span class="pulse-dot"></span>
          <span>30m Sync Active</span>
        </div>
        <div class="status-api-name">API: app.proaffiliaters.com</div>
        <div class="status-footer-row">
          <span>Engine: Telethon MTProto</span>
          <span class="status-tag">Connected</span>
        </div>
        <div style="margin-top:10px; padding-top:8px; border-top:1px solid var(--border-subtle); display:flex; justify-content:space-between; align-items:center;">
          <span style="font-size:11px; color:var(--text-subtle); font-weight:600;" id="sidebar-user-email">spushkar812@gmail.com</span>
          <a href="/logout" style="font-size:11px; color:var(--danger); text-decoration:none; font-weight:700;" title="Sign out of dashboard">Logout 🚪</a>
        </div>
      </div>
    </aside>

    <!-- MAIN APP WRAPPER -->
    <div class="main-wrapper">

      <!-- Topbar Header -->
      <header class="app-topbar">
        <div class="topbar-left">
          <button class="mobile-toggle-btn" onclick="toggleSidebar(true)" title="Toggle Navigation">
            ☰
          </button>
          <div class="view-breadcrumb">
            <span class="crumb-root">ProAffiliaters</span>
            <span class="crumb-separator">/</span>
            <span class="crumb-active" id="current-view-title">Extracted Leads Directory</span>
          </div>
        </div>

        <div class="topbar-right">
          <div class="pill-active">
            <span class="pulse-dot"></span>
            <span>Polling 30m Active</span>
          </div>
          <div style="display:flex; align-items:center; gap:8px; padding:5px 10px; background:var(--bg-card-subtle); border:1px solid var(--border-subtle); border-radius:var(--radius-md); font-size:12px; font-weight:600; color:var(--text-main);">
            <span style="display:inline-block; width:7px; height:7px; border-radius:50%; background:#10b981;"></span>
            <span id="topbar-user-email">spushkar812@gmail.com</span>
            <button onclick="handleDashboardLogout()" class="btn btn-sm btn-outline" style="padding:2px 7px; font-size:11px; color:var(--danger); border-color:#fecaca;" title="Sign out of dashboard">Logout</button>
          </div>
          <button class="btn btn-outline" id="btn-refresh" onclick="loadAll()">
            🔄 Refresh
          </button>
          <button class="btn btn-primary" id="btn-sync-now" onclick="triggerManualSync()">
            ⚡ Sync API Now
          </button>
        </div>
      </header>

      <!-- Main Content Area -->
      <main class="main-content">

        <!-- Top KPI Stat Cards -->
        <section class="stats-grid">
          <div class="stat-card">
            <div class="stat-card-header">
              <span class="stat-title">Total Leads Extracted</span>
              <div class="stat-icon" style="background:#e0e7ff; color:var(--primary);">🎯</div>
            </div>
            <div class="stat-value" id="kpi-total">0</div>
            <div class="stat-subtitle" id="kpi-storage">Saved directly in MongoDB</div>
          </div>

          <div class="stat-card">
            <div class="stat-card-header">
              <span class="stat-title">Human Owners / Managers</span>
              <div class="stat-icon" style="background:var(--info-light); color:var(--info);">👤</div>
            </div>
            <div class="stat-value" id="kpi-users">0</div>
            <div class="stat-subtitle">Direct @username contacts</div>
          </div>

          <div class="stat-card">
            <div class="stat-card-header">
              <span class="stat-title">Promotion / Deal Bots</span>
              <div class="stat-icon" style="background:var(--purple-light); color:var(--purple);">🤖</div>
            </div>
            <div class="stat-value" id="kpi-bots">0</div>
            <div class="stat-subtitle">Handles ending in "bot"</div>
          </div>

          <div class="stat-card">
            <div class="stat-card-header">
              <span class="stat-title">Polling Schedule</span>
              <div class="stat-icon" style="background:var(--success-light); color:var(--success);">⏰</div>
            </div>
            <div class="stat-value" style="font-size:24px; padding-top:4px;" id="kpi-interval">30 Mins</div>
            <div class="stat-subtitle" id="kpi-api-status">API: app.proaffiliaters.com</div>
          </div>
        </section>

    <!-- VIEW 1: Leads Directory -->
    <div id="tab-leads" class="view-content active">
      
      <!-- Filter and Search Bar -->
      <div class="filter-bar">
        <div class="filter-left">
          <div class="search-box">
            <span class="search-icon">🔍</span>
            <input type="text" id="search-input" placeholder="Search by handle, channel, or bio text..." oninput="filterLeads()" />
          </div>

          <div class="type-pills">
            <button class="type-pill-btn active" data-type="all" onclick="setTypeFilter('all', this)">All</button>
            <button class="type-pill-btn" data-type="user" onclick="setTypeFilter('user', this)">👤 Users</button>
            <button class="type-pill-btn" data-type="bot" onclick="setTypeFilter('bot', this)">🤖 Bots</button>
          </div>
        </div>

        <div style="display:flex; gap:10px; align-items:center;">
          <select id="status-select" class="select-status" onchange="filterLeads()">
            <option value="all">All Outreach Statuses</option>
            <option value="new">Status: New</option>
            <option value="contacted">Status: Contacted</option>
            <option value="converted">Status: Converted</option>
            <option value="ignored">Status: Ignored</option>
          </select>

          <button class="btn btn-outline" onclick="exportCSV()">
            📥 Export CSV
          </button>
        </div>
      </div>

      <!-- Leads Table -->
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>Lead Handle</th>
              <th>Lead Type</th>
              <th>Status</th>
              <th>Extracted From Channel</th>
              <th>Bio / Context Snippet</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody id="leads-tbody">
            <!-- Dynamically populated -->
          </tbody>
        </table>
      </div>
    </div>

    <!-- VIEW: Official Info Bot & Broadcasts Console -->
    <div id="tab-infobot" class="view-content">
      
      <!-- Info Bot Header Banner -->
      <div style="background:var(--bg-card); border:1px solid var(--border-subtle); border-radius:var(--radius-lg); padding:20px 24px; margin-bottom:20px; box-shadow:var(--shadow-sm); display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:16px;">
        <div style="display:flex; align-items:center; gap:16px;">
          <div style="width:48px; height:48px; border-radius:14px; background:linear-gradient(135deg, #0284c7 0%, #38bdf8 100%); color:white; display:flex; align-items:center; justify-content:center; font-size:24px; box-shadow:0 4px 12px rgba(2, 132, 199, 0.3);">
            🤖
          </div>
          <div>
            <div style="display:flex; align-items:center; gap:10px;">
              <h2 style="font-size:18px; font-weight:700; color:var(--text-main);" id="infobot-handle-display">@ProaffiliatersInfoBot</h2>
              <span class="pill-active" id="infobot-status-badge">
                <span class="pulse-dot"></span>
                <span id="infobot-status-text">Online & Polling</span>
              </span>
            </div>
            <p style="font-size:12px; color:var(--text-subtle); margin-top:2px;">
              Official ProAffiliaters Ad Landing & Lead Capture Bot • Admin Console ID: <code>7573032265</code>
            </p>
          </div>
        </div>

        <div style="display:flex; gap:10px; align-items:center;">
          <a id="infobot-deeplink-btn" href="https://t.me/ProaffiliatersInfoBot" target="_blank" class="btn btn-primary" style="text-decoration:none;">
            🚀 Open Bot in Telegram
          </a>
          <button class="btn btn-outline" onclick="loadInfoBotData()">
            🔄 Refresh Leads
          </button>
        </div>
      </div>

      <!-- Info Bot KPI Metrics Grid -->
      <div class="stats-grid" style="margin-bottom:20px;">
        <div class="stat-card">
          <div class="stat-card-header">
            <span class="stat-title">Captured Info Bot Leads</span>
            <div class="stat-icon" style="background:#e0f2fe; color:#0284c7;">👥</div>
          </div>
          <div class="stat-value" id="infobot-kpi-leads">0</div>
          <div class="stat-subtitle">Registered via /start & Telegram ads</div>
        </div>

        <div class="stat-card">
          <div class="stat-card-header">
            <span class="stat-title">Active in Last 24h</span>
            <div class="stat-icon" style="background:#d1fae5; color:#059669;">⚡</div>
          </div>
          <div class="stat-value" id="infobot-kpi-active">0</div>
          <div class="stat-subtitle">Recently engaged users</div>
        </div>

        <div class="stat-card">
          <div class="stat-card-header">
            <span class="stat-title">Blocked / Inactive</span>
            <div class="stat-icon" style="background:#fee2e2; color:#dc2626;">🚫</div>
          </div>
          <div class="stat-value" id="infobot-kpi-blocked">0</div>
          <div class="stat-subtitle">Auto-skipped during broadcasts</div>
        </div>

        <div class="stat-card">
          <div class="stat-card-header">
            <span class="stat-title">Broadcasts Dispatched</span>
            <div class="stat-icon" style="background:#ede9fe; color:#7c3aed;">📢</div>
          </div>
          <div class="stat-value" id="infobot-kpi-broadcasts">0</div>
          <div class="stat-subtitle">Total announcement campaigns sent</div>
        </div>
      </div>

      <!-- Two-Column Layout: Broadcast Composer & Leads Directory -->
      <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(440px, 1fr)); gap:20px; margin-bottom:24px;">
        
        <!-- Broadcast Composer Panel -->
        <div class="scanner-card" style="margin:0;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
            <h3 style="font-size:16px; font-weight:700; color:var(--text-main);">📢 Web Broadcast Dispatcher</h3>
            <span style="font-size:11px; padding:3px 8px; border-radius:6px; background:var(--bg-card-subtle); color:var(--text-subtle);">Admin Only</span>
          </div>
          <p style="font-size:12px; color:var(--text-subtle); margin-bottom:16px;">
            Instantly dispatch announcements to all registered active leads. Supports HTML tags (<code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, <code>&lt;code&gt;</code>).
          </p>

          <div class="form-group">
            <label>Broadcast Message Text *</label>
            <textarea id="broadcast-message-text" class="form-textarea" rows="4" placeholder="✨ Special Announcement for ProAffiliaters Creators!&#10;&#10;New feature released: Swapping Amazon tags in seconds.&#10;Tap below to try it out!"></textarea>
          </div>

          <div class="form-group">
            <label>Optional Photo Image URL / Telegram File ID</label>
            <input type="text" id="broadcast-photo-url" class="form-input" placeholder="https://example.com/banner.jpg (or leave blank)" />
          </div>

          <div class="form-group">
            <label>Interactive Inline Buttons Markup</label>
            <input type="text" id="broadcast-buttons-markup" class="form-input" placeholder="[🔥 Open Web App | https://app.proaffiliaters.com] [📢 Join Channel | https://t.me/proaffiliaters]" />
            <p style="font-size:11px; color:var(--text-subtle); margin-top:4px;">
              Syntax format: <code>[Button Title | https://link]</code>. Multiple buttons allowed; automatically formatted in rows.
            </p>
          </div>

          <button class="btn btn-primary" id="btn-send-broadcast" onclick="sendInfoBotBroadcast()" style="width:100%;">
            🚀 Dispatch Broadcast to All Leads
          </button>
        </div>

        <!-- Registered Info Bot Leads Panel -->
        <div class="scanner-card" style="margin:0; display:flex; flex-direction:column;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
            <h3 style="font-size:16px; font-weight:700; color:var(--text-main);">👥 Captured User Leads</h3>
            <span style="font-size:12px; font-weight:600; color:var(--primary);" id="infobot-leads-count-label">0 Leads</span>
          </div>
          
          <div style="margin-bottom:12px;">
            <input type="text" id="infobot-lead-search" class="form-input" placeholder="Search by handle, name, ID, or campaign tag..." oninput="filterInfoBotUsers()" />
          </div>

          <div class="table-container" style="flex:1; max-height:360px; overflow-y:auto; border:1px solid var(--border-subtle); border-radius:var(--radius-md);">
            <table style="width:100%; border-collapse:collapse; font-size:12px;">
              <thead>
                <tr style="background:var(--bg-card-subtle);">
                  <th style="padding:8px 12px; text-align:left;">User</th>
                  <th style="padding:8px 12px; text-align:left;">Chat ID</th>
                  <th style="padding:8px 12px; text-align:left;">Campaign Tag</th>
                  <th style="padding:8px 12px; text-align:left;">Joined</th>
                  <th style="padding:8px 12px; text-align:left;">Status</th>
                </tr>
              </thead>
              <tbody id="infobot-leads-tbody">
                <!-- Dynamically populated -->
              </tbody>
            </table>
          </div>
        </div>

      </div>

      <!-- Broadcast Audit Trail / History -->
      <div class="scanner-card" style="margin:0;">
        <h3 style="font-size:16px; font-weight:700; color:var(--text-main); margin-bottom:6px;">📜 Recent Broadcast Audit Trail</h3>
        <p style="font-size:12px; color:var(--text-subtle); margin-bottom:16px;">
          Delivery metrics and delivery logs for all campaigns dispatched through @ProaffiliatersInfoBot.
        </p>

        <div class="table-container" style="border:1px solid var(--border-subtle); border-radius:var(--radius-md);">
          <table style="width:100%; border-collapse:collapse; font-size:12px;">
            <thead>
              <tr style="background:var(--bg-card-subtle);">
                <th style="padding:10px 14px; text-align:left;">Dispatched Date</th>
                <th style="padding:10px 14px; text-align:left;">Message Content</th>
                <th style="padding:10px 14px; text-align:center;">Buttons</th>
                <th style="padding:10px 14px; text-align:center;">Targeted</th>
                <th style="padding:10px 14px; text-align:center;">Delivered</th>
                <th style="padding:10px 14px; text-align:center;">Failed / Blocked</th>
              </tr>
            </thead>
            <tbody id="infobot-broadcasts-tbody">
              <!-- Dynamically populated -->
            </tbody>
          </table>
        </div>
      </div>

    </div>

    <!-- VIEW: Admin Bot Outreach Console -->
    <div id="tab-bot-outreach" class="view-content">
      
      <!-- Header Banner -->
      <div style="background:var(--bg-card); border:1px solid var(--border-subtle); border-radius:var(--radius-lg); padding:20px 24px; margin-bottom:20px; box-shadow:var(--shadow-sm); display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:16px;">
        <div style="display:flex; align-items:center; gap:16px;">
          <div style="width:48px; height:48px; border-radius:14px; background:linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%); color:white; display:flex; align-items:center; justify-content:center; font-size:24px; box-shadow:0 4px 12px rgba(79, 70, 229, 0.3);">
            🚀
          </div>
          <div>
            <div style="display:flex; align-items:center; gap:10px;">
              <h2 style="font-size:18px; font-weight:700; color:var(--text-main);">Admin Bot Outreach Engine</h2>
              <span class="pill-active" id="forwarder-telethon-badge">
                <span class="pulse-dot"></span>
                <span id="forwarder-telethon-text">Telethon Ready</span>
              </span>
            </div>
            <p style="font-size:12px; color:var(--text-subtle); margin-top:2px;">
              Forwards promotional channel posts to deal channel contact bots with full banners & clickable inline deep-link buttons.
            </p>
          </div>
        </div>

        <div style="display:flex; gap:10px; align-items:center;">
          <a id="channel-link-btn" href="https://t.me/pro_affiliaters" target="_blank" class="btn btn-outline" style="text-decoration:none;">
            📢 View @pro_affiliaters
          </a>
          <button class="btn btn-primary" onclick="publishAllPostsToChannel()" id="btn-publish-all">
            ⚡ Publish All 5 Posts to Channel
          </button>
        </div>
      </div>

      <!-- Section: 5 Staged Channel Posts Cards -->
      <div style="margin-bottom:24px;">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
          <div>
            <h3 style="font-size:16px; font-weight:700; color:var(--text-main);">📁 5 Promotional Posts (Banners + Text + Deep-link Buttons)</h3>
            <p style="font-size:12px; color:var(--text-subtle);">Publish posts once to your channel. Clickable buttons deep-link directly to @ProaffiliatersInfoBot to capture leads!</p>
          </div>
          <button class="btn btn-outline" onclick="loadBotOutreachData()" style="padding:6px 12px; font-size:12px;">
            🔄 Refresh Posts Status
          </button>
        </div>

        <div id="promo-posts-grid" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); gap:16px;">
          <!-- Dynamically populated promo post cards -->
        </div>
      </div>

      <!-- Section: Outreach Dispatcher & Live Forwarder Monitor -->
      <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(440px, 1fr)); gap:20px; margin-bottom:24px;">
        
        <!-- Outreach Configuration Card -->
        <div class="scanner-card" style="margin:0;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
            <h3 style="font-size:16px; font-weight:700; color:var(--text-main);">🎯 Bot Forwarder Dispatcher</h3>
            <span style="font-size:11px; padding:3px 8px; border-radius:6px; background:var(--primary-light); color:var(--primary); font-weight:600;" id="outreach-pending-bots-badge">0 Pending Bots</span>
          </div>
          <p style="font-size:12px; color:var(--text-subtle); margin-bottom:16px;">
            Select a published post to forward to channel contact bots from your MongoDB leads.
          </p>

          <div class="form-group">
            <label>Select Promo Post to Forward *</label>
            <select id="campaign-post-select" class="form-input" onchange="onCampaignPostSelected()">
              <option value="1">Post 1: Automate Your Telegram Deal Channels</option>
              <option value="2">Post 2: 24/7 Hands-Free AutoPilot</option>
              <option value="3">Post 3: Custom Templates & Visual Branding</option>
              <option value="4">Post 4: Multi-Template Engine & Rotation</option>
              <option value="5">Post 5: 1-Click Quick Post & Auto-Delete</option>
            </select>
          </div>

          <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
            <div class="form-group">
              <label>Pacing Delay (Seconds)</label>
              <input type="number" id="campaign-delay" class="form-input" value="8" min="4" max="60" />
            </div>
            <div class="form-group">
              <label>Target Limit (Max Bots)</label>
              <input type="number" id="campaign-limit" class="form-input" value="50" min="1" max="500" />
            </div>
          </div>

          <!-- Single Bot Test Box -->
          <div style="background:var(--bg-card-subtle); border:1px solid var(--border-subtle); border-radius:var(--radius-md); padding:12px 14px; margin-bottom:16px;">
            <label style="font-size:12px; font-weight:700; color:var(--text-main); display:block; margin-bottom:6px;">🧪 Single Bot Test (Dry Run)</label>
            <div style="display:flex; gap:8px;">
              <input type="text" id="test-single-bot-input" class="form-input" placeholder="e.g. @MyDealSubmitBot" style="font-size:12px; padding:8px 10px;" />
              <button class="btn btn-outline" onclick="testForwardSingleBot()" id="btn-test-forward" style="padding:8px 14px; font-size:12px; white-space:nowrap;">
                Forward Test
              </button>
            </div>
          </div>

          <button class="btn btn-primary" onclick="startBotCampaign()" id="btn-start-campaign" style="width:100%; justify-content:center; padding:12px; font-size:14px;">
            🚀 Launch Bot Outreach Campaign
          </button>
        </div>

        <!-- Live Campaign Monitor & Real-Time Log Viewer -->
        <div class="scanner-card" style="margin:0; display:flex; flex-direction:column;">
          <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
            <h3 style="font-size:16px; font-weight:700; color:var(--text-main);">📡 Live Campaign Monitor</h3>
            <span id="campaign-state-pill" style="font-size:11px; font-weight:700; padding:3px 10px; border-radius:9999px; background:var(--bg-card-subtle); color:var(--text-subtle);">
              IDLE
            </span>
          </div>

          <!-- Progress Bar -->
          <div style="margin-bottom:16px;">
            <div style="display:flex; justify-content:space-between; font-size:12px; margin-bottom:6px; color:var(--text-subtle);">
              <span>Progress: <strong id="campaign-progress-pct" style="color:var(--text-main);">0%</strong></span>
              <span><span id="campaign-sent-count" style="color:var(--success); font-weight:700;">0</span> / <span id="campaign-total-count">0</span> Delivered</span>
            </div>
            <div style="height:10px; background:var(--bg-card-subtle); border-radius:9999px; overflow:hidden; border:1px solid var(--border-subtle);">
              <div id="campaign-progress-bar" style="height:100%; width:0%; background:linear-gradient(90deg, #4f46e5, #06b6d4); transition:width 0.3s ease;"></div>
            </div>
          </div>

          <!-- Pacing & Current Target -->
          <div style="display:flex; justify-content:space-between; align-items:center; background:var(--bg-card-subtle); padding:8px 12px; border-radius:var(--radius-sm); font-size:12px; margin-bottom:12px;">
            <span>Current Target: <strong id="campaign-current-bot" style="color:var(--primary);">None</strong></span>
            <span>Failed/Blocked: <strong id="campaign-failed-count" style="color:#dc2626;">0</strong></span>
          </div>

          <!-- Controls -->
          <div style="display:flex; gap:8px; margin-bottom:14px;">
            <button class="btn btn-outline" onclick="controlCampaign('pause')" id="btn-pause-camp" style="flex:1; justify-content:center; padding:8px;" disabled>
              ⏸️ Pause
            </button>
            <button class="btn btn-outline" onclick="controlCampaign('resume')" id="btn-resume-camp" style="flex:1; justify-content:center; padding:8px;" disabled>
              ▶️ Resume
            </button>
            <button class="btn btn-outline" onclick="controlCampaign('stop')" id="btn-stop-camp" style="flex:1; justify-content:center; padding:8px; color:#dc2626; border-color:#fca5a5;" disabled>
              ⏹️ Stop
            </button>
          </div>

          <!-- Terminal Log Box -->
          <div style="flex:1; min-height:160px; max-height:220px; background:#0f172a; border-radius:var(--radius-md); padding:10px 14px; font-family:monospace; font-size:11px; color:#94a3b8; overflow-y:auto; line-height:1.6;" id="campaign-terminal-logs">
            <div style="color:#64748b;">// Bot forwarder logs will appear here in real-time...</div>
          </div>
        </div>

      </div>

      <!-- Section: Past Campaigns History -->
      <div class="scanner-card">
        <h3 style="font-size:16px; font-weight:700; color:var(--text-main); margin-bottom:14px;">📜 Bot Outreach Campaign Audit</h3>
        <div style="overflow-x:auto;">
          <table style="width:100%; border-collapse:collapse; font-size:12px;">
            <thead>
              <tr style="background:var(--bg-card-subtle); text-align:left; border-bottom:1px solid var(--border-subtle); color:var(--text-subtle);">
                <th style="padding:10px 14px;">Date & Time</th>
                <th style="padding:10px 14px;">Post #</th>
                <th style="padding:10px 14px;">Channel Msg</th>
                <th style="padding:10px 14px; text-align:center;">Targeted</th>
                <th style="padding:10px 14px; text-align:center;">Delivered</th>
                <th style="padding:10px 14px; text-align:center;">Failed</th>
                <th style="padding:10px 14px; text-align:center;">Status</th>
              </tr>
            </thead>
            <tbody id="bot-campaigns-tbody">
              <!-- Dynamically populated -->
            </tbody>
          </table>
        </div>
      </div>

    </div>

    <!-- VIEW: Telegram Multi-Account Manager -->
    <div id="tab-accounts" class="view-content">
      <div class="scanner-card" style="margin-bottom:20px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:16px;">
        <div>
          <h2 style="font-size:18px; font-weight:700; color:var(--text-main); margin-bottom:4px;">🔑 Telegram Account Management</h2>
          <p style="font-size:13px; color:var(--text-subtle);">
            Connect unlimited Telegram accounts. Assign accounts to <strong>Slots 1 to 5</strong> for weekly rotating drip campaigns to deal contact bots.
          </p>
        </div>
        <div style="display:flex; gap:10px; align-items:center;">
          <button class="btn btn-outline" style="font-size:12px; color:#64748b;" onclick="clearDemoAccounts()" title="Clear mock demo accounts">
            🧹 Clear Demo Accounts
          </button>
          <button class="btn btn-primary" onclick="openAddAccountModal()">
            ➕ Connect New Account
          </button>
        </div>
      </div>

      <!-- Telegram API Config Banner (shown if not configured) -->
      <div id="telegram-config-banner" style="display:none; background:#fffbeb; border:1px solid #fde68a; border-radius:var(--radius-lg); padding:20px; margin-bottom:20px;">
        <div style="display:flex; gap:14px; align-items:flex-start;">
          <div style="font-size:26px;">⚠️</div>
          <div style="flex:1;">
            <h3 style="font-size:15px; font-weight:700; color:#92400e; margin-bottom:4px;">Telegram MTProto API Credentials Required</h3>
            <p style="font-size:13px; color:#b45309; line-height:1.5; margin-bottom:12px;">
              To connect real Telegram accounts and receive login OTPs, you need a free <strong>API ID</strong> and <strong>API HASH</strong> from Telegram.<br/>
              <strong>Quick 1-minute setup:</strong><br/>
              1. Open <a href="https://my.telegram.org" target="_blank" style="color:#d97706; font-weight:700; text-decoration:underline;">my.telegram.org</a> in your browser.<br/>
              2. Log in with your phone number and enter the confirmation code Telegram sends you.<br/>
              3. Click <strong>API development tools</strong> and create an app (name can be <code>ProAffTool</code>).<br/>
              4. Copy your <code>api_id</code> and <code>api_hash</code> and paste them below:
            </p>
            <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
              <input type="number" id="cfg-api-id" class="form-input" placeholder="API ID (e.g. 12345678)" style="width:200px; padding:8px 12px;" />
              <input type="text" id="cfg-api-hash" class="form-input" placeholder="API HASH (32 hex characters)" style="width:320px; padding:8px 12px;" />
              <button class="btn btn-primary" onclick="saveTelegramConfig()" style="padding:8px 18px;">💾 Save & Activate</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Account Slots Grid (Slot 1 to 5) -->
      <div id="accounts-grid" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:18px; margin-bottom:24px;">
        <!-- Populated dynamically by loadAccounts() -->
      </div>
    </div>

    <!-- VIEW: 5-Week Drip Funnel -->
    <div id="tab-drip-funnel" class="view-content">
      <div class="scanner-card" style="margin-bottom:20px;">
        <h2 style="font-size:18px; font-weight:700; color:var(--text-main); margin-bottom:4px;">📆 5-Week Progressive Drip Forwarding Sequence</h2>
        <p style="font-size:13px; color:var(--text-subtle);">
          Progressive weekly forward sequence to contact bots. Each post is forwarded from <code>@pro_affiliaters</code> using that week's dedicated account, enforcing a <strong>7-day cooldown</strong> between drops.
        </p>

        <!-- Funnel Metric Chips -->
        <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(170px, 1fr)); gap:12px; margin-top:18px;" id="drip-kpi-grid">
          <div style="background:var(--bg-card-subtle); border:1px solid var(--border-subtle); border-radius:var(--radius-md); padding:12px 14px;">
            <div style="font-size:11px; font-weight:700; color:var(--text-subtle); text-transform:uppercase;">Total Deal Bots</div>
            <div style="font-size:22px; font-weight:800; color:var(--text-main); margin-top:2px;" id="funnel-total-bots">0</div>
          </div>
          <div style="background:var(--bg-card-subtle); border:1px solid var(--border-subtle); border-radius:var(--radius-md); padding:12px 14px;">
            <div style="font-size:11px; font-weight:700; color:var(--text-subtle); text-transform:uppercase;">Pending Week 1</div>
            <div style="font-size:22px; font-weight:800; color:var(--primary); margin-top:2px;" id="funnel-pending-w1">0</div>
          </div>
          <div style="background:var(--bg-card-subtle); border:1px solid var(--border-subtle); border-radius:var(--radius-md); padding:12px 14px;">
            <div style="font-size:11px; font-weight:700; color:var(--text-subtle); text-transform:uppercase;">Week 1 Sent</div>
            <div style="font-size:22px; font-weight:800; color:var(--info); margin-top:2px;" id="funnel-stage1-sent">0</div>
          </div>
          <div style="background:var(--bg-card-subtle); border:1px solid var(--border-subtle); border-radius:var(--radius-md); padding:12px 14px;">
            <div style="font-size:11px; font-weight:700; color:var(--text-subtle); text-transform:uppercase;">Week 2 Sent</div>
            <div style="font-size:22px; font-weight:800; color:var(--info); margin-top:2px;" id="funnel-stage2-sent">0</div>
          </div>
          <div style="background:var(--bg-card-subtle); border:1px solid var(--border-subtle); border-radius:var(--radius-md); padding:12px 14px;">
            <div style="font-size:11px; font-weight:700; color:var(--text-subtle); text-transform:uppercase;">Week 3 Sent</div>
            <div style="font-size:22px; font-weight:800; color:var(--info); margin-top:2px;" id="funnel-stage3-sent">0</div>
          </div>
          <div style="background:var(--bg-card-subtle); border:1px solid var(--border-subtle); border-radius:var(--radius-md); padding:12px 14px;">
            <div style="font-size:11px; font-weight:700; color:var(--text-subtle); text-transform:uppercase;">Week 4 Sent</div>
            <div style="font-size:22px; font-weight:800; color:var(--info); margin-top:2px;" id="funnel-stage4-sent">0</div>
          </div>
          <div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:var(--radius-md); padding:12px 14px;">
            <div style="font-size:11px; font-weight:700; color:#15803d; text-transform:uppercase;">Stage 5 Completed</div>
            <div style="font-size:22px; font-weight:800; color:#15803d; margin-top:2px;" id="funnel-stage5-completed">0</div>
          </div>
        </div>
      </div>

      <!-- 5 Weekly Stage Cards -->
      <div style="display:flex; flex-direction:column; gap:16px; margin-bottom:24px;" id="drip-stages-container">
        <!-- Rendered dynamically by loadDripOverview() -->
      </div>
    </div>

    <!-- VIEW: Autonomous Telegram Keyword Search System -->
    <div id="tab-keywords" class="view-content">
      
      <!-- Top Action Bar & Status Banner -->
      <div class="scanner-card" style="margin-bottom:20px; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:16px;">
        <div>
          <div style="display:flex; align-items:center; gap:10px; margin-bottom:4px;">
            <h2 style="font-size:20px; font-weight:800; color:var(--text-main); margin:0;">🔍 Autonomous Telegram Keyword Search</h2>
            <span class="status-chip chip-active" id="autonomous-engine-badge">🟢 30m Auto-Cycle Active (Boot + Every 30m)</span>
          </div>
          <p style="font-size:13px; color:var(--text-subtle); margin:0;">
            Autonomously discovers public Telegram deal channels matching keywords, inspects bios, and extracts deal owner handles & submission bots into MongoDB.
          </p>
        </div>
        <div style="display:flex; gap:10px;">
          <button class="btn btn-outline" onclick="openAddKeywordModal()">
            ➕ Add Keyword
          </button>
          <button class="btn btn-primary" onclick="runAutonomousCycleNow()" id="btn-run-auto-cycle">
            ⚡ Run Auto-Cycle Now
          </button>
        </div>
      </div>

      <!-- KPI Stat Cards -->
      <div class="stats-grid" style="margin-bottom:20px;">
        <div class="stat-card">
          <div class="stat-icon-wrapper icon-users">🎯</div>
          <div class="stat-info">
            <div class="stat-value" id="kw-active-count">0</div>
            <div class="stat-label">Active Keywords</div>
            <div class="stat-sub" id="kw-total-count">0 configured targets</div>
          </div>
        </div>

        <div class="stat-card">
          <div class="stat-icon-wrapper icon-bots">📡</div>
          <div class="stat-info">
            <div class="stat-value" id="kw-channels-count">0</div>
            <div class="stat-label">Channels Discovered</div>
            <div class="stat-sub">Saved in database</div>
          </div>
        </div>

        <div class="stat-card">
          <div class="stat-icon-wrapper icon-total">👥</div>
          <div class="stat-info">
            <div class="stat-value" id="kw-leads-count">0</div>
            <div class="stat-label">Leads Extracted</div>
            <div class="stat-sub">Direct @user & @bot contacts</div>
          </div>
        </div>

        <div class="stat-card">
          <div class="stat-icon-wrapper icon-clock">🛡️</div>
          <div class="stat-info">
            <div class="stat-value" style="font-size:18px;">7-Day Anti-Dupe</div>
            <div class="stat-label">Safe MTProto Scanner</div>
            <div class="stat-sub">Channels deduplicated</div>
          </div>
        </div>
      </div>

      <!-- Instant Live Search & Inspection Tool -->
      <div class="scanner-card" style="margin-bottom:24px;">
        <h3 style="font-size:16px; font-weight:700; margin-bottom:6px; color:var(--text-main);">⚡ Instant Telegram Global Search & Bio Extraction</h3>
        <p style="font-size:13px; color:var(--text-subtle); margin-bottom:14px;">
          Execute a live MTProto global directory search using an active connected Telegram account to instantly discover channels, read their bios, and pull leads.
        </p>
        <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
          <input type="text" id="live-search-input" class="form-input" style="flex:1; min-width:240px;" placeholder="e.g. loot deals, amazon offers, discount coupons, flipkart loot..." />
          <select id="live-search-limit" class="form-input" style="width:130px;">
            <option value="10">10 Channels</option>
            <option value="15" selected>15 Channels</option>
            <option value="25">25 Channels</option>
          </select>
          <button class="btn btn-primary" onclick="executeInstantSearch()" id="btn-live-search">
            🔍 Search & Extract Now
          </button>
        </div>
        <div id="live-search-results" style="display:none; margin-top:16px; padding:14px; background:var(--bg-card-subtle); border-radius:var(--radius-md); border:1px solid var(--border-subtle);">
          <!-- Live results populated by JS -->
        </div>
      </div>

      <!-- Two-Column Section: Target Keywords Management & Discovered Channels Directory -->
      <div style="display:grid; grid-template-columns: 1fr; gap:24px;">
        
        <!-- Target Keywords Table -->
        <div class="leads-card">
          <div class="leads-card-header" style="display:flex; justify-content:space-between; align-items:center;">
            <div>
              <h3 style="font-size:16px; font-weight:700; color:var(--text-main); margin:0;">🎯 Configured Target Keywords</h3>
              <p style="font-size:12px; color:var(--text-subtle); margin:2px 0 0 0;">Keywords rotated sequentially during autonomous 60-minute background cycles.</p>
            </div>
            <button class="btn btn-sm btn-outline" onclick="openAddKeywordModal()">➕ Add Keyword</button>
          </div>
          <div class="table-container">
            <table class="leads-table">
              <thead>
                <tr>
                  <th style="width:24%;">KEYWORD</th>
                  <th style="width:16%;">CATEGORY</th>
                  <th style="width:14%;">CHANNELS</th>
                  <th style="width:14%;">LEADS</th>
                  <th style="width:16%;">LAST SEARCHED</th>
                  <th style="width:8%;">STATUS</th>
                  <th style="width:8%;">ACTION</th>
                </tr>
              </thead>
              <tbody id="keywords-table-body">
                <tr><td colspan="7" style="text-align:center; padding:30px; color:var(--text-subtle);">Loading keywords...</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Discovered Channels Directory -->
        <div class="leads-card">
          <div class="leads-card-header" style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
            <div>
              <h3 style="font-size:16px; font-weight:700; color:var(--text-main); margin:0;">📡 Discovered Telegram Channels Directory</h3>
              <p style="font-size:12px; color:var(--text-subtle); margin:2px 0 0 0;">Public deal channels found via keyword searches with inspected bios and extracted leads.</p>
            </div>
            <div style="display:flex; gap:10px;">
              <input type="text" id="discovered-channels-search" class="form-input" style="width:220px; font-size:12px; padding:6px 10px;" placeholder="Search channels..." oninput="debounceDiscoveredSearch()" />
              <button class="btn btn-sm btn-outline" onclick="loadDiscoveredChannels()">🔄 Refresh</button>
            </div>
          </div>
          <div class="table-container">
            <table class="leads-table">
              <thead>
                <tr>
                  <th style="width:28%;">CHANNEL NAME</th>
                  <th style="width:22%;">USERNAME / INVITE</th>
                  <th style="width:18%;">MATCHING KEYWORD</th>
                  <th style="width:14%;">SUBSCRIBERS</th>
                  <th style="width:10%;">LEADS FOUND</th>
                  <th style="width:8%;">ACTION</th>
                </tr>
              </thead>
              <tbody id="discovered-channels-body">
                <tr><td colspan="6" style="text-align:center; padding:30px; color:var(--text-subtle);">Loading discovered channels...</td></tr>
              </tbody>
            </table>
          </div>
        </div>

      </div>

    </div>

    <!-- VIEW 3: Bio Scanner Playground -->
    <div id="tab-playground" class="view-content">
      <div class="scanner-card">
        <h2 style="font-size:18px; margin-bottom:6px; color:var(--text-main);">🧪 Test Telegram Channel Bio Scanner</h2>
        <p style="font-size:13px; color:var(--text-subtle); margin-bottom:20px;">
          Paste sample channel descriptions, pinned message text, or invite links to test instant handle extraction and bot classification.
        </p>

        <div class="form-group">
          <label>Source Channel Name (Simulation)</label>
          <input type="text" id="test-channel-name" class="form-input" value="Top Deals India VIP" />
        </div>

        <div class="form-group">
          <label>Source Channel Invite Link</label>
          <input type="text" id="test-invite-link" class="form-input" value="https://t.me/topdealsvip" />
        </div>

        <div class="form-group">
          <label>Channel Description / Bio Text</label>
          <textarea id="test-bio-text" class="form-textarea" rows="4" placeholder="e.g. Welcome! For paid promotions contact owner @DealsAdminVIP or message our deal alert bot @LootAlertBot...">🔥 Official Deals Channel!
For paid promotions & business inquiries, DM @OwnerAlex or @DealsPartner.
To submit deal suggestions, use our automated bot: @DealSubmissionBot.
Backups: https://t.me/DealBackups</textarea>
        </div>

        <button class="btn btn-primary" onclick="runScannerTest()">
          🚀 Extract Leads & Save
        </button>

        <div id="test-results" style="margin-top:24px; display:none;">
          <h3 style="font-size:15px; margin-bottom:12px;">Extraction Results</h3>
          <div id="test-results-list" style="display:flex; flex-direction:column; gap:8px;"></div>
        </div>
      </div>
    </div>

    <!-- VIEW 3: Live Activity Logs -->
    <div id="tab-logs" class="view-content">
      <div class="log-container">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
          <h2 style="font-size:16px; font-weight:700;">⚡ Real-time Activity Log</h2>
          <button class="btn btn-outline" onclick="loadLogs()" style="padding:6px 12px; font-size:12px;">Refresh Logs</button>
        </div>
        <div id="log-list">
          <!-- Populated dynamically -->
        </div>
      </div>
    </div>

    <!-- VIEW 4: Integration Settings -->
    <div id="tab-settings" class="view-content">
      <div class="scanner-card">
        <h2 style="font-size:18px; margin-bottom:16px;">⚙️ ProAffiliaters Integration Details</h2>
        <table style="width:100%; border-collapse:collapse;">
          <tbody>
            <tr>
              <td style="padding:12px; font-weight:600; width:220px; color:var(--text-subtle);">External API Endpoint</td>
              <td style="padding:12px; font-family:monospace; background:var(--bg-card-subtle); border-radius:6px;">https://app.proaffiliaters.com/api/external/sources/recent</td>
            </tr>
            <tr>
              <td style="padding:12px; font-weight:600; color:var(--text-subtle);">Auth Header</td>
              <td style="padding:12px; font-family:monospace; background:var(--bg-card-subtle); border-radius:6px;">x-api-key: proaff_ext_source_sync_key_2026</td>
            </tr>
            <tr>
              <td style="padding:12px; font-weight:600; color:var(--text-subtle);">Sync Window Interval</td>
              <td style="padding:12px;"><strong>Every 30 Minutes</strong> (<code>minutes=30</code>)</td>
            </tr>
            <tr>
              <td style="padding:12px; font-weight:600; color:var(--text-subtle);">Storage Rule</td>
              <td style="padding:12px; color:var(--success); font-weight:600;">✅ Only Leads stored in MongoDB; Sources ephemeral</td>
            </tr>
            <tr>
              <td style="padding:12px; font-weight:600; color:var(--text-subtle);">Bot vs. User Classification</td>
              <td style="padding:12px;">Usernames ending in <code>bot</code> / <code>_bot</code> classified as <strong>bot</strong>, all others as <strong>user</strong>.</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

      </main>

      <!-- Footer -->
      <footer class="app-footer">
        ProAffiliaters Lead Promotion Engine &copy; 2026 • Automated 30-Minute Channel Polling & Outreach.
      </footer>

    </div><!-- /.main-wrapper -->
  </div><!-- /.app-shell -->

  <!-- Modal: Connect Telegram Account -->
  <div id="modal-add-account" style="display:none; position:fixed; inset:0; background:rgba(15,23,42,0.6); backdrop-filter:blur(3px); z-index:999; align-items:center; justify-content:center; padding:16px;">
    <div style="background:var(--bg-card); border:1px solid var(--border-subtle); border-radius:var(--radius-lg); width:100%; max-width:440px; padding:24px; box-shadow:var(--shadow-md); position:relative;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
        <h3 style="font-size:16px; font-weight:700; color:var(--text-main);">➕ Connect Telegram Account</h3>
        <button onclick="closeAddAccountModal()" style="border:none; background:transparent; font-size:20px; cursor:pointer; color:var(--text-subtle);">&times;</button>
      </div>

      <!-- Step 1: Phone + Slot -->
      <div id="account-step-1">
        <div class="form-group">
          <label style="font-size:12px; font-weight:600; color:var(--text-main); display:block; margin-bottom:6px;">Phone Number (with Country Code)</label>
          <input type="text" id="acc-phone-input" class="form-input" placeholder="+919876543210" style="width:100%;" />
          <span style="font-size:11px; color:var(--text-subtle); display:block; margin-top:4px;">Example: +1234567890 or +919876543210</span>
        </div>
        <div class="form-group" style="margin-top:14px;">
          <label style="font-size:12px; font-weight:600; color:var(--text-main); display:block; margin-bottom:6px;">Assign to Slot / Week</label>
          <select id="acc-slot-select" class="form-input" style="width:100%; padding:8px 10px;">
            <option value="1">Slot 1 - Week 1 (Post #1 Forwarder)</option>
            <option value="2">Slot 2 - Week 2 (Post #2 Forwarder)</option>
            <option value="3">Slot 3 - Week 3 (Post #3 Forwarder)</option>
            <option value="4">Slot 4 - Week 4 (Post #4 Forwarder)</option>
            <option value="5">Slot 5 - Week 5 (Post #5 Forwarder)</option>
            <option value="6">Slot 6 (Reserve / Backup)</option>
          </select>
        </div>
        <button class="btn btn-primary" id="btn-acc-send-code" onclick="submitStartLogin()" style="width:100%; margin-top:20px; justify-content:center;">
          📨 Send Telegram Login Code
        </button>
      </div>

      <!-- Step 2: OTP Code + 2FA -->
      <div id="account-step-2" style="display:none;">
        <div style="background:#eff6ff; border:1px solid #bfdbfe; border-radius:var(--radius-sm); padding:10px 12px; margin-bottom:14px; font-size:12px; color:#1d4ed8;">
          Verification code sent to <strong id="acc-display-phone"></strong>. Please check your official Telegram app or SMS.
        </div>
        <div class="form-group">
          <label style="font-size:12px; font-weight:600; color:var(--text-main); display:block; margin-bottom:6px;">5-Digit Verification Code</label>
          <input type="text" id="acc-code-input" class="form-input" placeholder="12345" style="width:100%; letter-spacing:4px; font-weight:700; font-size:16px; text-align:center;" />
        </div>
        <div class="form-group" style="margin-top:12px;" id="acc-pwd-group">
          <label style="font-size:12px; font-weight:600; color:var(--text-main); display:block; margin-bottom:6px;">2FA Cloud Password (if enabled)</label>
          <input type="password" id="acc-pwd-input" class="form-input" placeholder="Leave blank if 2FA is off" style="width:100%;" />
        </div>
        <button class="btn btn-primary" id="btn-acc-verify-code" onclick="submitCompleteLogin()" style="width:100%; margin-top:20px; justify-content:center;">
          ✅ Verify & Connect Account
        </button>
        <button class="btn btn-outline" onclick="resetLoginStep()" style="width:100%; margin-top:8px; justify-content:center;">
          Back to Phone Input
        </button>
      </div>
    </div>
  </div>

  <!-- Modal: Add Target Keyword -->
  <div id="modal-add-keyword" style="display:none; position:fixed; inset:0; background:rgba(15,23,42,0.6); backdrop-filter:blur(3px); z-index:999; align-items:center; justify-content:center; padding:16px;">
    <div style="background:var(--bg-card); border:1px solid var(--border-subtle); border-radius:var(--radius-lg); width:100%; max-width:440px; padding:24px; box-shadow:var(--shadow-md); position:relative;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
        <h3 style="font-size:16px; font-weight:700; color:var(--text-main);">➕ Add Target Search Keyword</h3>
        <button onclick="closeAddKeywordModal()" style="border:none; background:transparent; font-size:20px; cursor:pointer; color:var(--text-subtle);">&times;</button>
      </div>
      <p style="font-size:13px; color:var(--text-subtle); margin-bottom:16px;">
        The autonomous engine will search Telegram's global directory for this keyword, inspect matching channel bios, and pull leads into MongoDB.
      </p>
      <div class="form-group">
        <label style="font-size:12px; font-weight:600; color:var(--text-main); display:block; margin-bottom:6px;">Keyword (e.g. loot deals, amazon deals, coupons)</label>
        <input type="text" id="modal-kw-input" class="form-input" placeholder="e.g. loot deals" style="width:100%;" />
      </div>
      <div class="form-group" style="margin-top:14px;">
        <label style="font-size:12px; font-weight:600; color:var(--text-main); display:block; margin-bottom:6px;">Category Tag</label>
        <select id="modal-kw-category" class="form-input" style="width:100%; padding:8px 10px;">
          <option value="deals">Deals & Loot</option>
          <option value="amazon">Amazon Deals</option>
          <option value="flipkart">Flipkart Deals</option>
          <option value="coupons">Discount Coupons</option>
          <option value="affiliate">Affiliate Programs</option>
          <option value="general">General Shopping</option>
        </select>
      </div>
      <div style="display:flex; gap:10px; margin-top:20px;">
        <button class="btn btn-outline" onclick="closeAddKeywordModal()" style="flex:1; justify-content:center;">Cancel</button>
        <button class="btn btn-primary" onclick="submitAddKeyword()" style="flex:1; justify-content:center;">Add Keyword</button>
      </div>
    </div>
  </div>

  <!-- Toast Element -->
  <div id="toast">Message</div>

  <!-- JavaScript Logic -->
  <script>
    let allLeads = [];
    let currentTypeFilter = "all";
    let isTelegramConfigured = false;

    async function loadStats() {
      try {
        const res = await fetch('/api/stats');
        const data = await res.json();
        document.getElementById('kpi-total').textContent = data.total_leads || 0;
        document.getElementById('kpi-users').textContent = data.total_users || 0;
        document.getElementById('kpi-bots').textContent = data.total_bots || 0;
        const totalLeads = data.total_leads || 0;
        ['leads-tab-count', 'leads-sidebar-count'].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.textContent = totalLeads;
        });
        document.getElementById('kpi-storage').textContent = data.storage_engine || "MongoDB";

        isTelegramConfigured = !!data.telegram_configured;
        const banner = document.getElementById('telegram-config-banner');
        if (banner) {
          banner.style.display = isTelegramConfigured ? 'none' : 'block';
        }
      } catch (e) {
        console.error("Failed to load stats", e);
      }
    }

    async function loadLeads() {
      try {
        const res = await fetch('/api/leads?limit=300');
        const data = await res.json();
        allLeads = data.leads || [];
        renderLeadsTable();
      } catch (e) {
        console.error("Failed to load leads", e);
      }
    }

    async function loadLogs() {
      try {
        const res = await fetch('/api/logs');
        const data = await res.json();
        const container = document.getElementById('log-list');
        container.innerHTML = '';
        
        (data.logs || []).forEach(l => {
          const div = document.createElement('div');
          div.className = 'log-entry';
          div.innerHTML = `
            <span class="log-time">${l.timestamp}</span>
            <span class="log-badge log-${l.type}">${l.type}</span>
            <span style="flex:1;">${escapeHtml(l.message)}</span>
          `;
          container.appendChild(div);
        });
      } catch (e) {
        console.error("Failed to load logs", e);
      }
    }

    function renderLeadsTable() {
      const tbody = document.getElementById('leads-tbody');
      tbody.innerHTML = '';

      const searchVal = document.getElementById('search-input').value.toLowerCase();
      const statusVal = document.getElementById('status-select').value;

      const filtered = allLeads.filter(l => {
        // Type filter
        if (currentTypeFilter !== "all" && l.lead_type !== currentTypeFilter) return false;
        // Status filter
        if (statusVal !== "all" && l.status !== statusVal) return false;
        // Search text filter
        if (searchVal) {
          const u = (l.username || '').toLowerCase();
          const b = (l.bio_context || '').toLowerCase();
          const s = (l.extracted_from?.source_name || '').toLowerCase();
          if (!u.includes(searchVal) && !b.includes(searchVal) && !s.includes(searchVal)) return false;
        }
        return true;
      });

      if (filtered.length === 0) {
        tbody.innerHTML = `
          <tr>
            <td colspan="6" class="empty-state">
              <div class="empty-icon">🔍</div>
              <p>No leads found matching current filters.</p>
            </td>
          </tr>
        `;
        return;
      }

      filtered.forEach(lead => {
        const isBot = lead.lead_type === 'bot';
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>
            <div class="lead-profile">
              <div class="lead-avatar ${isBot ? 'avatar-bot' : 'avatar-user'}">
                ${isBot ? '🤖' : '👤'}
              </div>
              <div class="lead-meta">
                <a href="https://t.me/${lead.username}" target="_blank" class="lead-username">
                  @${escapeHtml(lead.username)}
                  <span style="font-size:10px; color:var(--primary);">↗</span>
                </a>
                <button class="btn-copy" onclick="copyText('@${lead.username}')">📋 Copy</button>
              </div>
            </div>
          </td>
          <td>
            <span class="badge-type ${isBot ? 'badge-bot' : 'badge-user'}">
              ${isBot ? 'Bot' : 'Human User'}
            </span>
          </td>
          <td>
            <select class="select-status" style="padding:4px 8px; font-size:11px; font-weight:700;" onchange="changeStatus('${lead.username_clean}', this.value)">
              <option value="new" ${lead.status === 'new' ? 'selected' : ''}>New</option>
              <option value="contacted" ${lead.status === 'contacted' ? 'selected' : ''}>Contacted</option>
              <option value="converted" ${lead.status === 'converted' ? 'selected' : ''}>Converted</option>
              <option value="ignored" ${lead.status === 'ignored' ? 'selected' : ''}>Ignored</option>
            </select>
          </td>
          <td>
            <div class="channel-source">
              <span class="channel-name">${escapeHtml(lead.extracted_from?.source_name || 'Direct Channel')}</span>
              ${lead.extracted_from?.source_invite_link ? `<a href="${lead.extracted_from.source_invite_link}" target="_blank" class="channel-link">${escapeHtml(lead.extracted_from.source_invite_link)}</a>` : ''}
            </div>
          </td>
          <td>
            <div class="bio-snippet" title="${escapeHtml(lead.bio_context || '')}">
              ${escapeHtml(lead.bio_context || 'No bio snippet recorded')}
            </div>
          </td>
          <td>
            <a href="https://t.me/${lead.username}" target="_blank" class="btn btn-outline" style="padding:5px 12px; font-size:12px;">
              Open Chat
            </a>
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    async function changeStatus(usernameClean, newStatus) {
      try {
        const res = await fetch(`/api/leads/${usernameClean}/status`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: newStatus })
        });
        if (res.ok) {
          showToast(`Updated @${usernameClean} status to ${newStatus}`);
          loadStats();
        }
      } catch (e) {
        showToast("Failed to update status");
      }
    }

    async function triggerManualSync() {
      const btn = document.getElementById('btn-sync-now');
      btn.disabled = true;
      btn.textContent = '⏳ Syncing...';
      try {
        const res = await fetch('/api/trigger-sync', { method: 'POST' });
        const data = await res.json();
        showToast(data.message || `Sync completed! Found ${data.sources_count || 0} sources.`);
        await loadAll();
      } catch (e) {
        showToast("Error connecting to ProAffiliaters API");
      } finally {
        btn.disabled = false;
        btn.textContent = '⚡ Sync API Now';
      }
    }

    async function runScannerTest() {
      const bioText = document.getElementById('test-bio-text').value;
      const channelName = document.getElementById('test-channel-name').value;
      const inviteLink = document.getElementById('test-invite-link').value;

      try {
        const res = await fetch('/api/scan-test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ bio_text: bioText, channel_name: channelName, invite_link: inviteLink })
        });
        const data = await res.json();
        
        const resultsBox = document.getElementById('test-results');
        const list = document.getElementById('test-results-list');
        resultsBox.style.display = 'block';
        list.innerHTML = '';

        if (!data.leads || data.leads.length === 0) {
          list.innerHTML = '<p style="color:var(--text-subtle); font-size:13px;">No usernames or bot handles detected in this bio text.</p>';
        } else {
          data.leads.forEach(l => {
            const isBot = l.lead_type === 'bot';
            const item = document.createElement('div');
            item.style.cssText = "display:flex; justify-content:space-between; align-items:center; background:var(--bg-card-subtle); padding:10px 14px; border-radius:8px;";
            item.innerHTML = `
              <div>
                <strong>@${escapeHtml(l.username)}</strong>
                <span style="font-size:12px; color:var(--text-subtle); margin-left:8px;">Clean: '${l.username_clean}'</span>
              </div>
              <span class="badge-type ${isBot ? 'badge-bot' : 'badge-user'}">
                ${isBot ? '🤖 Bot' : '👤 User'}
              </span>
            `;
            list.appendChild(item);
          });
        }

        showToast(`Extracted ${data.extracted_count} leads (${data.newly_saved} newly saved to DB)!`);
        await loadAll();
      } catch (e) {
        showToast("Error executing scanner test");
      }
    }

    function setTypeFilter(type, btn) {
      currentTypeFilter = type;
      document.querySelectorAll('.type-pill-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      renderLeadsTable();
    }

    function filterLeads() {
      renderLeadsTable();
    }

    function switchTab(tabId, btn, viewTitle) {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.view-content').forEach(v => v.classList.remove('active'));
      
      if (btn) btn.classList.add('active');
      const navTarget = document.querySelector(`.nav-item[data-tab="${tabId}"]`);
      if (navTarget) navTarget.classList.add('active');

      const view = document.getElementById(tabId);
      if (view) view.classList.add('active');

      if (viewTitle) {
        const titleEl = document.getElementById('current-view-title');
        if (titleEl) titleEl.textContent = viewTitle;
      }

      // Close mobile drawer if open
      toggleSidebar(false);

      if (tabId === 'tab-infobot') loadInfoBotData();
      if (tabId === 'tab-bot-outreach') loadBotOutreachData();
      if (tabId === 'tab-accounts') loadAccounts();
      if (tabId === 'tab-drip-funnel') loadDripOverview();
      if (tabId === 'tab-keywords') loadKeywordsData();
    }

    function toggleSidebar(open) {
      const sidebar = document.getElementById('app-sidebar');
      const backdrop = document.getElementById('sidebar-backdrop');
      if (!sidebar) return;
      if (open === undefined) {
        sidebar.classList.toggle('open');
        if (backdrop) backdrop.classList.toggle('open');
      } else if (open) {
        sidebar.classList.add('open');
        if (backdrop) backdrop.classList.add('open');
      } else {
        sidebar.classList.remove('open');
        if (backdrop) backdrop.classList.remove('open');
      }
    }

    function copyText(text) {
      navigator.clipboard.writeText(text);
      showToast(`Copied ${text} to clipboard!`);
    }

    function showToast(msg) {
      const toast = document.getElementById('toast');
      toast.textContent = msg;
      toast.classList.add('show');
      setTimeout(() => toast.classList.remove('show'), 3000);
    }

    function escapeHtml(text) {
      if (!text) return '';
      return text.replace(/[&<>"']/g, function(m) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[m];
      });
    }

    function exportCSV() {
      if (!allLeads.length) {
        showToast("No leads to export");
        return;
      }
      let csv = "Username,Type,Status,Source_Channel,Invite_Link,Bio_Context\\n";
      allLeads.forEach(l => {
        const u = `"${(l.username || '').replace(/"/g, '""')}"`;
        const t = `"${l.lead_type || ''}"`;
        const s = `"${l.status || ''}"`;
        const src = `"${(l.extracted_from?.source_name || '').replace(/"/g, '""')}"`;
        const link = `"${(l.extracted_from?.source_invite_link || '').replace(/"/g, '""')}"`;
        const bio = `"${(l.bio_context || '').replace(/"/g, '""')}"`;
        csv += `${u},${t},${s},${src},${link},${bio}\\n`;
      });
      const blob = new Blob([csv], { type: 'text/csv' });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.setAttribute('href', url);
      a.setAttribute('download', `telegram_leads_${new Date().toISOString().slice(0,10)}.csv`);
      a.click();
      showToast("CSV export generated!");
    }

    let allInfoBotUsers = [];
    let allInfoBotBroadcasts = [];

    async function loadInfoBotData() {
      try {
        // 1. Fetch Status
        const resStatus = await fetch('/api/info-bot/status');
        const statusData = await resStatus.json();
        
        const username = statusData.bot_username || "ProaffiliatersInfoBot";
        document.getElementById('infobot-handle-display').textContent = `@${username}`;
        document.getElementById('infobot-deeplink-btn').href = statusData.deep_link || `https://t.me/${username}`;
        document.getElementById('infobot-kpi-leads').textContent = statusData.total_leads || 0;
        document.getElementById('infobot-kpi-active').textContent = statusData.active_24h || 0;
        document.getElementById('infobot-kpi-blocked').textContent = statusData.blocked_leads || 0;
        document.getElementById('infobot-kpi-broadcasts').textContent = statusData.broadcasts_count || 0;
        ['infobot-tab-count', 'infobot-sidebar-count'].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.textContent = statusData.total_leads || 0;
        });

        const badge = document.getElementById('infobot-status-badge');
        const text = document.getElementById('infobot-status-text');
        if (statusData.running) {
          text.textContent = "Online & Polling";
          badge.className = "pill-active";
        } else {
          text.textContent = "Offline / Inactive";
          badge.style.background = "#fee2e2";
          badge.style.color = "#dc2626";
          badge.style.borderColor = "#fca5a5";
        }

        // 2. Fetch Users
        const resUsers = await fetch('/api/info-bot/users?limit=100');
        const usersData = await resUsers.json();
        allInfoBotUsers = usersData.users || [];
        document.getElementById('infobot-leads-count-label').textContent = `${allInfoBotUsers.length} Leads`;
        renderInfoBotUsersTable();

        // 3. Fetch Broadcasts
        const resBroadcasts = await fetch('/api/info-bot/broadcasts?limit=20');
        const broadcastsData = await resBroadcasts.json();
        allInfoBotBroadcasts = broadcastsData.broadcasts || [];
        renderInfoBotBroadcastsTable();

      } catch (err) {
        console.error("Failed to load Info Bot data:", err);
      }
    }

    function renderInfoBotUsersTable() {
      const tbody = document.getElementById('infobot-leads-tbody');
      tbody.innerHTML = '';

      const query = (document.getElementById('infobot-lead-search')?.value || '').toLowerCase();
      const filtered = allInfoBotUsers.filter(u => {
        if (!query) return true;
        const name = `${u.first_name || ''} ${u.last_name || ''}`.toLowerCase();
        const handle = (u.username || '').toLowerCase();
        const cid = (u.telegram_chat_id || '').toLowerCase();
        const tag = (u.start_payload || '').toLowerCase();
        return name.includes(query) || handle.includes(query) || cid.includes(query) || tag.includes(query);
      });

      if (!filtered.length) {
        tbody.innerHTML = '<tr><td colspan="5" style="padding:24px; text-align:center; color:var(--text-subtle);">No registered leads found.</td></tr>';
        return;
      }

      filtered.forEach(u => {
        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid var(--border-subtle)';
        const name = [u.first_name, u.last_name].filter(Boolean).join(" ") || "Unnamed";
        const handle = u.username ? `@${escapeHtml(u.username)}` : '<span style="color:var(--text-subtle);">No handle</span>';
        const tag = u.start_payload ? `<code style="background:#e0e7ff; color:#4338ca; padding:2px 6px; border-radius:4px;">${escapeHtml(u.start_payload)}</code>` : '<span style="color:var(--text-subtle);">Organic</span>';
        const joined = u.joined_at ? new Date(u.joined_at).toLocaleDateString("en-IN") : "-";
        const isBlocked = Boolean(u.is_blocked);

        tr.innerHTML = `
          <td style="padding:10px 12px;">
            <strong>${escapeHtml(name)}</strong><br>
            <span style="font-size:11px; color:var(--primary);">${handle}</span>
          </td>
          <td style="padding:10px 12px; font-family:monospace; color:var(--text-subtle);">${u.telegram_chat_id}</td>
          <td style="padding:10px 12px;">${tag}</td>
          <td style="padding:10px 12px; color:var(--text-subtle);">${joined}</td>
          <td style="padding:10px 12px;">
            <span style="display:inline-block; padding:2px 8px; border-radius:9999px; font-size:10px; font-weight:700; ${isBlocked ? 'background:#fee2e2; color:#dc2626;' : 'background:#d1fae5; color:#059669;'}">
              ${isBlocked ? 'BLOCKED' : 'ACTIVE'}
            </span>
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    function filterInfoBotUsers() {
      renderInfoBotUsersTable();
    }

    function renderInfoBotBroadcastsTable() {
      const tbody = document.getElementById('infobot-broadcasts-tbody');
      tbody.innerHTML = '';

      if (!allInfoBotBroadcasts.length) {
        tbody.innerHTML = '<tr><td colspan="6" style="padding:24px; text-align:center; color:var(--text-subtle);">No previous broadcasts recorded.</td></tr>';
        return;
      }

      allInfoBotBroadcasts.forEach((b, i) => {
        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid var(--border-subtle)';
        const date = b.created_at ? new Date(b.created_at).toLocaleString("en-IN") : "-";
        const preview = (b.message_text || "").slice(0, 50).replace(/<[^>]*>?/gm, '');
        const btnCount = (b.inline_buttons || []).length;

        tr.innerHTML = `
          <td style="padding:10px 14px; font-weight:500;">${date}</td>
          <td style="padding:10px 14px; color:var(--text-main); font-weight:500;">
            ${escapeHtml(preview)}${preview.length >= 50 ? '...' : ''}
          </td>
          <td style="padding:10px 14px; text-align:center;">
            <span style="background:var(--bg-card-subtle); padding:2px 8px; border-radius:6px; font-weight:600;">${btnCount}</span>
          </td>
          <td style="padding:10px 14px; text-align:center; font-weight:600;">${b.total_targeted || 0}</td>
          <td style="padding:10px 14px; text-align:center; color:var(--success); font-weight:700;">${b.successful_sent || 0}</td>
          <td style="padding:10px 14px; text-align:center; color:${b.failed_count > 0 ? '#dc2626' : 'var(--text-subtle)'}; font-weight:700;">${b.failed_count || 0}</td>
        `;
        tbody.appendChild(tr);
      });
    }

    async function sendInfoBotBroadcast() {
      const text = document.getElementById('broadcast-message-text').value.trim();
      const photo = document.getElementById('broadcast-photo-url').value.trim();
      const markup = document.getElementById('broadcast-buttons-markup').value.trim();

      if (!text) {
        showToast("Please enter broadcast message text.");
        return;
      }

      if (!confirm("Are you sure you want to dispatch this broadcast to ALL active registered leads?")) {
        return;
      }

      const btn = document.getElementById('btn-send-broadcast');
      btn.disabled = true;
      btn.textContent = "⏳ Dispatching Broadcast...";

      try {
        const res = await fetch('/api/info-bot/broadcast', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message_text: text,
            buttons_markup: markup || null,
            photo_url: photo || null
          })
        });

        const data = await res.json();
        if (res.ok) {
          showToast(data.message || "Broadcast successfully queued!");
          document.getElementById('broadcast-message-text').value = '';
          document.getElementById('broadcast-photo-url').value = '';
          document.getElementById('broadcast-buttons-markup').value = '';
          await loadInfoBotData();
        } else {
          showToast(data.error || "Failed to dispatch broadcast");
        }
      } catch (err) {
        showToast("Network error while dispatching broadcast");
      } finally {
        btn.disabled = false;
        btn.textContent = "🚀 Dispatch Broadcast to All Leads";
      }
    }

    async function loadAll() {
      await loadStats();
      await loadLeads();
      await loadLogs();
      await loadInfoBotData();
      await loadBotOutreachData();
    }

    // ==========================================
    // Admin Bot Outreach Logic
    // ==========================================
    let allPromoPosts = [];
    let botCampaignPollInterval = null;

    async function loadBotOutreachData() {
      try {
        // 1. Fetch Posts Status
        const resPosts = await fetch('/api/bot-outreach/posts');
        const postsData = await resPosts.json();
        allPromoPosts = postsData.posts || [];
        renderPromoPostsGrid();

        // 2. Fetch Forwarder & Campaign Status
        const resStatus = await fetch('/api/bot-outreach/status');
        const statusData = await resStatus.json();
        updateForwarderStatus(statusData);

      } catch (err) {
        console.error("Failed to load Bot Outreach data:", err);
      }
    }

    function renderPromoPostsGrid() {
      const container = document.getElementById('promo-posts-grid');
      container.innerHTML = '';

      allPromoPosts.forEach(p => {
        const card = document.createElement('div');
        card.className = 'scanner-card';
        card.style.cssText = "margin:0; padding:14px; display:flex; flex-direction:column; justify-content:space-between; border-radius:12px;";

        const isPub = Boolean(p.is_published);
        const pubBadge = isPub 
          ? `<span style="display:inline-flex; align-items:center; gap:4px; padding:3px 8px; border-radius:9999px; font-size:11px; font-weight:700; background:#d1fae5; color:#059669; border:1px solid #a7f3d0;">
               ✅ Published (#${p.message_id})
             </span>`
          : `<span style="display:inline-flex; align-items:center; gap:4px; padding:3px 8px; border-radius:9999px; font-size:11px; font-weight:700; background:#f1f5f9; color:#64748b; border:1px solid #cbd5e1;">
               ⏳ Staging Ready
             </span>`;

        card.innerHTML = `
          <div>
            <div style="position:relative; border-radius:8px; overflow:hidden; margin-bottom:10px; background:#0f172a; aspect-ratio:16/9;">
              <img src="${p.image_url}" alt="Post ${p.post_number}" style="width:100%; height:100%; object-fit:cover;" onerror="this.style.display='none'" />
              <div style="position:absolute; top:6px; left:6px; background:rgba(15,23,42,0.85); color:white; font-size:10px; font-weight:700; padding:2px 8px; border-radius:6px; backdrop-filter:blur(4px);">
                Post #${p.post_number}
              </div>
            </div>

            <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:8px; margin-bottom:6px;">
              <h4 style="font-size:13px; font-weight:700; color:var(--text-main); margin:0;">${escapeHtml(p.title)}</h4>
              ${pubBadge}
            </div>

            <div style="background:var(--bg-card-subtle); border:1px solid var(--border-subtle); border-radius:6px; padding:8px; font-size:11px; margin-bottom:10px;">
              <div style="color:var(--text-subtle); margin-bottom:2px;"><strong>Deep-Link Bot Button:</strong></div>
              <code style="word-break:break-all; color:var(--primary); font-size:10px;">${escapeHtml(p.deep_link)}</code>
            </div>

            ${isPub && p.message_link ? `
              <div style="font-size:11px; margin-bottom:10px;">
                <a href="${p.message_link}" target="_blank" style="color:var(--primary); text-decoration:none; font-weight:600;">
                  📢 Open Live Post in Telegram ↗
                </a>
              </div>
            ` : ''}
          </div>

          <button class="btn btn-outline" style="width:100%; justify-content:center; padding:8px; font-size:12px;" onclick="publishPostToChannel(${p.post_number})" id="btn-pub-${p.post_number}">
            ${isPub ? '🔁 Re-publish to Channel' : '📢 Publish to @pro_affiliaters'}
          </button>
        `;
        container.appendChild(card);
      });
    }

    async function publishPostToChannel(postNum) {
      const btn = document.getElementById(`btn-pub-${postNum}`);
      if (btn) {
        btn.disabled = true;
        btn.textContent = "⏳ Publishing...";
      }

      try {
        const res = await fetch('/api/bot-outreach/publish', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ post_number: postNum })
        });
        const data = await res.json();
        if (data.success) {
          showToast(`Post #${postNum} successfully published to channel!`);
          await loadBotOutreachData();
        } else {
          showToast(data.error || "Failed to publish post to channel");
        }
      } catch (err) {
        showToast("Error connecting to server for channel publication");
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function publishAllPostsToChannel() {
      if (!confirm("Are you sure you want to publish all 5 promotional deal posts into @pro_affiliaters?")) {
        return;
      }

      const btn = document.getElementById('btn-publish-all');
      btn.disabled = true;
      btn.textContent = "⏳ Publishing 5 Posts...";

      try {
        const res = await fetch('/api/bot-outreach/publish', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ post_number: 0 })
        });
        const data = await res.json();
        if (data.success) {
          showToast("All 5 promotional posts published to @pro_affiliaters!");
          await loadBotOutreachData();
        } else {
          showToast(data.error || "Error publishing all posts");
        }
      } catch (err) {
        showToast("Network error publishing posts");
      } finally {
        btn.disabled = false;
        btn.textContent = "⚡ Publish All 5 Posts to Channel";
      }
    }

    function onCampaignPostSelected() {
      // Auto-update UI if needed
    }

    function updateForwarderStatus(statusData) {
      const badge = document.getElementById('forwarder-telethon-badge');
      const text = document.getElementById('forwarder-telethon-text');
      if (statusData.telethon_connected) {
        text.textContent = "Telethon Client Connected";
        badge.className = "pill-active";
      } else {
        text.textContent = "Telethon Ready (Session Configured)";
        badge.className = "pill-active";
        badge.style.background = "#e0e7ff";
        badge.style.color = "#4338ca";
        badge.style.borderColor = "#c7d2fe";
      }

      const uncontactedCount = statusData.uncontacted_bots_count || 0;
      document.getElementById('outreach-pending-bots-badge').textContent = `${uncontactedCount} Pending Bots`;
      ['bot-outreach-tab-count', 'bot-outreach-sidebar-count'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.textContent = uncontactedCount;
      });

      // Update Campaign Monitor
      const camp = statusData.campaign || {};
      const state = statusData.engine_state || "idle";
      const pill = document.getElementById('campaign-state-pill');
      pill.textContent = state.toUpperCase();

      if (state === "running") {
        pill.style.background = "#d1fae5";
        pill.style.color = "#059669";
        document.getElementById('btn-pause-camp').disabled = false;
        document.getElementById('btn-resume-camp').disabled = true;
        document.getElementById('btn-stop-camp').disabled = false;
        document.getElementById('btn-start-campaign').disabled = true;

        if (!botCampaignPollInterval) {
          botCampaignPollInterval = setInterval(loadBotOutreachData, 2500);
        }
      } else if (state === "paused") {
        pill.style.background = "#fef3c7";
        pill.style.color = "#d97706";
        document.getElementById('btn-pause-camp').disabled = true;
        document.getElementById('btn-resume-camp').disabled = false;
        document.getElementById('btn-stop-camp').disabled = false;
        document.getElementById('btn-start-campaign').disabled = true;
      } else {
        pill.style.background = "var(--bg-card-subtle)";
        pill.style.color = "var(--text-subtle)";
        document.getElementById('btn-pause-camp').disabled = true;
        document.getElementById('btn-resume-camp').disabled = true;
        document.getElementById('btn-stop-camp').disabled = true;
        document.getElementById('btn-start-campaign').disabled = false;

        if (botCampaignPollInterval) {
          clearInterval(botCampaignPollInterval);
          botCampaignPollInterval = null;
        }
      }

      const targeted = camp.total_targeted || 0;
      const sent = camp.successful_sent || 0;
      const failed = camp.failed_count || 0;
      const pct = targeted > 0 ? Math.round(((sent + failed) / targeted) * 100) : 0;

      document.getElementById('campaign-total-count').textContent = targeted;
      document.getElementById('campaign-sent-count').textContent = sent;
      document.getElementById('campaign-failed-count').textContent = failed;
      document.getElementById('campaign-progress-pct').textContent = `${pct}%`;
      document.getElementById('campaign-progress-bar').style.width = `${pct}%`;
      document.getElementById('campaign-current-bot').textContent = camp.current_bot ? `@${camp.current_bot}` : 'None';

      // Logs
      if (camp.logs && camp.logs.length) {
        const term = document.getElementById('campaign-terminal-logs');
        term.innerHTML = camp.logs.map(l => `<div>${escapeHtml(l)}</div>`).join('');
        term.scrollTop = term.scrollHeight;
      }

      // Render past campaigns table
      renderBotCampaignsTable(statusData.campaigns || []);
    }

    function renderBotCampaignsTable(campaigns) {
      const tbody = document.getElementById('bot-campaigns-tbody');
      tbody.innerHTML = '';

      if (!campaigns.length) {
        tbody.innerHTML = '<tr><td colspan="7" style="padding:20px; text-align:center; color:var(--text-subtle);">No bot outreach campaigns recorded yet.</td></tr>';
        return;
      }

      campaigns.forEach(c => {
        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid var(--border-subtle)';
        const date = c.created_at ? new Date(c.created_at).toLocaleString("en-IN") : "-";
        const st = c.status || "completed";

        tr.innerHTML = `
          <td style="padding:10px 14px; font-weight:500;">${date}</td>
          <td style="padding:10px 14px;"><strong>Post #${c.post_number || 1}</strong></td>
          <td style="padding:10px 14px; color:var(--primary); font-family:monospace;">Msg #${c.channel_message_id || '-'}</td>
          <td style="padding:10px 14px; text-align:center; font-weight:600;">${c.total_targeted || 0}</td>
          <td style="padding:10px 14px; text-align:center; color:var(--success); font-weight:700;">${c.successful_sent || 0}</td>
          <td style="padding:10px 14px; text-align:center; color:${c.failed_count > 0 ? '#dc2626' : 'var(--text-subtle)'}; font-weight:700;">${c.failed_count || 0}</td>
          <td style="padding:10px 14px; text-align:center;">
            <span style="display:inline-block; padding:2px 8px; border-radius:9999px; font-size:10px; font-weight:700; ${st === 'completed' ? 'background:#d1fae5; color:#059669;' : 'background:#e0e7ff; color:#4338ca;'}">
              ${st.toUpperCase()}
            </span>
          </td>
        `;
        tbody.appendChild(tr);
      });
    }

    async function testForwardSingleBot() {
      const botInput = document.getElementById('test-single-bot-input');
      const botUsername = botInput.value.trim();
      const postNum = parseInt(document.getElementById('campaign-post-select').value, 10);

      if (!botUsername) {
        showToast("Please enter a bot handle (e.g. @MyDealBot)");
        return;
      }

      const btn = document.getElementById('btn-test-forward');
      btn.disabled = true;
      btn.textContent = "⏳ Forwarding...";

      try {
        const res = await fetch('/api/bot-outreach/test-forward', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            bot_username: botUsername,
            post_number: postNum
          })
        });

        const data = await res.json();
        if (data.success) {
          showToast(`Successfully forwarded Post #${postNum} to @${data.bot}!`);
          botInput.value = '';
          await loadBotOutreachData();
        } else {
          showToast(data.error || "Forwarding failed");
        }
      } catch (err) {
        showToast("Network error forwarding test message");
      } finally {
        btn.disabled = false;
        btn.textContent = "Forward Test";
      }
    }

    async function startBotCampaign() {
      const postNum = parseInt(document.getElementById('campaign-post-select').value, 10);
      const delay = parseInt(document.getElementById('campaign-delay').value, 10) || 8;
      const limit = parseInt(document.getElementById('campaign-limit').value, 10) || 50;

      // Check if post is published
      const targetPost = allPromoPosts.find(p => p.post_number === postNum);
      if (!targetPost || !targetPost.is_published || !targetPost.message_id) {
        showToast(`Post #${postNum} is not published yet! Please click "Publish to @pro_affiliaters" first.`);
        return;
      }

      if (!confirm(`Launch outreach campaign forwarding Post #${postNum} to up to ${limit} bot leads with ${delay}s delay?`)) {
        return;
      }

      try {
        const res = await fetch('/api/bot-outreach/start-campaign', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            post_number: postNum,
            channel_message_id: targetPost.message_id,
            delay_seconds: delay,
            target_limit: limit
          })
        });

        const data = await res.json();
        if (data.success) {
          showToast("Campaign started successfully!");
          await loadBotOutreachData();
        } else {
          showToast(data.error || "Failed to start campaign");
        }
      } catch (err) {
        showToast("Error starting outreach campaign");
      }
    }

    async function controlCampaign(action) {
      try {
        const res = await fetch('/api/bot-outreach/control', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: action })
        });
        const data = await res.json();
        showToast(`Campaign ${action} command dispatched`);
        await loadBotOutreachData();
      } catch (err) {
        showToast(`Error dispatching ${action}`);
      }
    }

    // ==========================================
    // Multi-Account Management JavaScript
    // ==========================================
    let currentLoginToken = null;
    let allAccounts = [];

    async function loadAccounts() {
      try {
        const res = await fetch('/api/accounts');
        const data = await res.json();
        allAccounts = data.accounts || [];
        ['accounts-tab-count', 'accounts-sidebar-count'].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.textContent = allAccounts.length;
        });

        const grid = document.getElementById('accounts-grid');
        if (!grid) return;
        grid.innerHTML = '';

        const accountsBySlot = {};
        allAccounts.forEach(acc => {
          accountsBySlot[acc.assigned_slot] = acc;
        });

        // Always render at least Slots 1 to 5
        const maxSlot = Math.max(5, ...allAccounts.map(a => a.assigned_slot || 0));

        for (let slot = 1; slot <= maxSlot; slot++) {
          const acc = accountsBySlot[slot];
          const card = document.createElement('div');

          if (acc) {
            const isActive = acc.status === 'active';
            const isFlood = (acc.status || '').startsWith('flood_wait');
            const statusColor = isActive ? '#059669' : (isFlood ? '#d97706' : '#dc2626');
            const statusBg = isActive ? '#d1fae5' : (isFlood ? '#fef3c7' : '#fee2e2');

            card.style.cssText = 'background:var(--bg-card); border:1px solid var(--border-subtle); border-radius:var(--radius-lg); padding:18px; box-shadow:var(--shadow-sm); display:flex; flex-direction:column; justify-content:space-between;';
            card.innerHTML = `
              <div>
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                  <span style="font-size:11px; font-weight:700; background:#e0e7ff; color:var(--primary); padding:3px 10px; border-radius:9999px;">
                    Slot #${slot} • Week ${slot} (Post #${slot})
                  </span>
                  <span style="font-size:11px; font-weight:700; background:${statusBg}; color:${statusColor}; padding:2px 8px; border-radius:9999px;">
                    ${acc.status.toUpperCase()}
                  </span>
                </div>
                <div style="font-size:16px; font-weight:700; color:var(--text-main); margin-bottom:2px;">
                  ${escapeHtml(acc.phone)}
                </div>
                <div style="font-size:13px; color:var(--text-subtle); margin-bottom:8px;">
                  ${escapeHtml(acc.first_name || '')} ${escapeHtml(acc.last_name || '')} ${acc.username ? `(@${escapeHtml(acc.username)})` : ''}
                </div>
                <div style="font-size:11px; color:var(--text-subtle); font-family:monospace; margin-bottom:14px;">
                  User ID: ${acc.user_id}
                </div>
              </div>
              <div style="display:flex; gap:8px; border-top:1px solid var(--border-subtle); padding-top:12px; margin-top:6px;">
                <button class="btn btn-outline" style="flex:1; padding:6px; font-size:12px; justify-content:center;" onclick="pingAccount('${acc.phone}', this)">
                  ⚡ Ping
                </button>
                <button class="btn btn-outline" style="padding:6px 10px; font-size:12px; color:#dc2626; border-color:#fca5a5;" onclick="deleteAccount('${acc.phone}')" title="Remove Account">
                  🗑️
                </button>
              </div>
            `;
          } else {
            card.style.cssText = 'background:var(--bg-card-subtle); border:2px dashed var(--border-subtle); border-radius:var(--radius-lg); padding:20px; display:flex; flex-direction:column; align-items:center; justify-content:center; text-align:center; min-height:160px;';
            card.innerHTML = `
              <div style="font-size:24px; margin-bottom:6px;">📱</div>
              <div style="font-size:13px; font-weight:700; color:var(--text-main); margin-bottom:2px;">Slot #${slot} Unassigned</div>
              <div style="font-size:11px; color:var(--text-subtle); margin-bottom:12px;">Assigned to Week ${slot} (Post #${slot})</div>
              <button class="btn btn-outline" style="font-size:12px; padding:6px 14px;" onclick="openAddAccountModal(${slot})">
                + Connect Slot #${slot}
              </button>
            `;
          }
          grid.appendChild(card);
        }
      } catch (e) {
        console.error("Failed to load accounts", e);
      }
    }

    function openAddAccountModal(preselectSlot) {
      if (!isTelegramConfigured) {
        showToast("⚠️ Telegram API ID & Hash must be configured first! See the banner on this tab.");
        const banner = document.getElementById('telegram-config-banner');
        if (banner) {
          banner.style.display = 'block';
          banner.scrollIntoView({ behavior: 'smooth' });
        }
        return;
      }
      if (preselectSlot) {
        document.getElementById('acc-slot-select').value = String(preselectSlot);
      }
      document.getElementById('account-step-1').style.display = 'block';
      document.getElementById('account-step-2').style.display = 'none';
      document.getElementById('acc-phone-input').value = '';
      document.getElementById('acc-code-input').value = '';
      document.getElementById('acc-pwd-input').value = '';
      document.getElementById('modal-add-account').style.display = 'flex';
    }

    async function saveTelegramConfig() {
      const apiIdInput = document.getElementById('cfg-api-id').value.trim();
      const apiHashInput = document.getElementById('cfg-api-hash').value.trim();
      if (!apiIdInput || !apiHashInput) {
        showToast("Please enter both API ID and API HASH");
        return;
      }
      try {
        const res = await fetch('/api/config/telegram', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ api_id: parseInt(apiIdInput, 10), api_hash: apiHashInput })
        });
        const data = await res.json();
        if (data.success) {
          showToast("Telegram API credentials saved successfully!");
          isTelegramConfigured = true;
          document.getElementById('telegram-config-banner').style.display = 'none';
          await loadStats();
          await loadAccounts();
        } else {
          showToast(data.error || "Failed to save credentials");
        }
      } catch (e) {
        showToast("Error saving Telegram credentials");
      }
    }

    async function clearDemoAccounts() {
      if (!confirm("Remove mock demo accounts (+1999...) from database?")) return;
      try {
        const res = await fetch('/api/accounts/clear-demo', { method: 'POST' });
        const data = await res.json();
        showToast(`Cleared ${data.cleared_count || 0} demo accounts!`);
        await loadAccounts();
      } catch (e) {
        showToast("Error clearing demo accounts");
      }
    }

    function closeAddAccountModal() {
      document.getElementById('modal-add-account').style.display = 'none';
      currentLoginToken = null;
    }

    function resetLoginStep() {
      document.getElementById('account-step-1').style.display = 'block';
      document.getElementById('account-step-2').style.display = 'none';
      currentLoginToken = null;
      const codeInp = document.getElementById('acc-code-input');
      const pwdInp = document.getElementById('acc-pwd-input');
      if (codeInp) codeInp.value = '';
      if (pwdInp) {
        pwdInp.value = '';
        pwdInp.style.borderColor = 'var(--border-subtle)';
      }
      const btn = document.getElementById('btn-acc-verify-code');
      if (btn) {
        btn.disabled = false;
        btn.textContent = "✅ Verify & Connect Account";
      }
    }

    async function submitStartLogin() {
      const phone = document.getElementById('acc-phone-input').value.trim();
      const slot = parseInt(document.getElementById('acc-slot-select').value, 10);

      if (!phone) {
        showToast("Please enter a valid phone number with country code.");
        return;
      }

      const btn = document.getElementById('btn-acc-send-code');
      btn.disabled = true;
      btn.textContent = "⏳ Sending Code...";

      try {
        const res = await fetch('/api/accounts/login/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ phone: phone, slot: slot })
        });
        const data = await res.json();
        if (data.status === 'code_sent') {
          currentLoginToken = data.session_token;
          document.getElementById('acc-display-phone').textContent = data.phone;
          document.getElementById('account-step-1').style.display = 'none';
          document.getElementById('account-step-2').style.display = 'block';
          showToast(data.message || "Verification code sent!");
        } else {
          showToast(data.error || "Failed to send code.");
        }
      } catch (e) {
        showToast("Error contacting server");
      } finally {
        btn.disabled = false;
        btn.textContent = "📨 Send Telegram Login Code";
      }
    }

    async function submitCompleteLogin() {
      const code = document.getElementById('acc-code-input').value.trim();
      const pwd = document.getElementById('acc-pwd-input').value.trim();

      if (!code && !pwd) {
        showToast("Please enter verification code");
        return;
      }

      const btn = document.getElementById('btn-acc-verify-code');
      btn.disabled = true;
      btn.textContent = pwd ? "⏳ Verifying 2FA Password..." : "⏳ Verifying Code...";

      try {
        const res = await fetch('/api/accounts/login/complete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_token: currentLoginToken,
            code: code || null,
            password: pwd || null
          })
        });
        const data = await res.json();

        if (data.status === 'requires_password') {
          showToast("🔐 2FA Cloud Password Required! Please enter your 2FA password.");
          const pwdInp = document.getElementById('acc-pwd-input');
          pwdInp.focus();
          pwdInp.style.borderColor = "var(--primary)";
          btn.textContent = "🔐 Submit 2FA Cloud Password";
        } else if (data.status === 'success') {
          showToast(data.message || "Account connected successfully!");
          closeAddAccountModal();
          await loadAccounts();
        } else {
          showToast(data.error || "Verification failed");
          if (data.error && (data.error.includes("expired") || data.error.includes("invalidated"))) {
            setTimeout(() => resetLoginStep(), 2500);
          }
        }
      } catch (e) {
        showToast("Error verifying credentials");
      } finally {
        btn.disabled = false;
        if (!document.getElementById('acc-pwd-input').value.trim() && !btn.textContent.includes("2FA")) {
          btn.textContent = "✅ Verify & Connect Account";
        }
      }
    }

    async function pingAccount(phone, btn) {
      if (btn) {
        btn.disabled = true;
        btn.textContent = "⏳";
      }
      try {
        const res = await fetch(`/api/accounts/${encodeURIComponent(phone)}/ping`, { method: 'POST' });
        const data = await res.json();
        if (data.status === 'active') {
          showToast(`Account ${phone} is ONLINE & ACTIVE!`);
        } else {
          showToast(`Account ${phone} status: ${data.status} (${data.error || ''})`);
        }
        await loadAccounts();
      } catch (e) {
        showToast("Error pinging account");
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = "⚡ Ping";
        }
      }
    }

    async function deleteAccount(phone) {
      if (!confirm(`Are you sure you want to disconnect and remove account ${phone}?`)) {
        return;
      }
      try {
        const res = await fetch(`/api/accounts/${encodeURIComponent(phone)}`, { method: 'DELETE' });
        const data = await res.json();
        if (data.success) {
          showToast(`Account ${phone} disconnected.`);
          await loadAccounts();
        }
      } catch (e) {
        showToast("Error deleting account");
      }
    }

    // ==========================================
    // 5-Week Progressive Drip Funnel JavaScript
    // ==========================================

    async function loadDripOverview() {
      try {
        const res = await fetch('/api/drip/overview');
        const data = await res.json();
        const funnel = data.funnel || {};
        const stages = data.stages || [];

        // Update Funnel Stats Chips
        if (document.getElementById('funnel-total-bots')) document.getElementById('funnel-total-bots').textContent = funnel.total_bots || 0;
        if (document.getElementById('funnel-pending-w1')) document.getElementById('funnel-pending-w1').textContent = funnel.stage_0_pending || 0;
        if (document.getElementById('funnel-stage1-sent')) document.getElementById('funnel-stage1-sent').textContent = funnel.stage_1_sent || 0;
        if (document.getElementById('funnel-stage2-sent')) document.getElementById('funnel-stage2-sent').textContent = funnel.stage_2_sent || 0;
        if (document.getElementById('funnel-stage3-sent')) document.getElementById('funnel-stage3-sent').textContent = funnel.stage_3_sent || 0;
        if (document.getElementById('funnel-stage4-sent')) document.getElementById('funnel-stage4-sent').textContent = funnel.stage_4_sent || 0;
        if (document.getElementById('funnel-stage5-completed')) document.getElementById('funnel-stage5-completed').textContent = funnel.stage_5_completed || 0;

        const totalActiveInFunnel = (funnel.stage_0_pending || 0) + (funnel.stage_1_sent || 0);
        ['drip-tab-count', 'drip-sidebar-count'].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.textContent = totalActiveInFunnel || "5 Stages";
        });

        const container = document.getElementById('drip-stages-container');
        if (!container) return;
        container.innerHTML = '';

        stages.forEach(st => {
          const card = document.createElement('div');
          const isReady = st.ready_to_send > 0 && st.has_active_account;

          card.style.cssText = 'background:var(--bg-card); border:1px solid var(--border-subtle); border-radius:var(--radius-lg); padding:20px; box-shadow:var(--shadow-sm); display:flex; flex-wrap:wrap; justify-content:space-between; align-items:center; gap:16px;';
          card.innerHTML = `
            <div style="flex:1; min-width:300px;">
              <div style="display:flex; align-items:center; gap:10px; margin-bottom:6px;">
                <span style="font-size:12px; font-weight:800; background:var(--primary); color:white; padding:3px 10px; border-radius:9999px;">
                  ${st.week} • Post #${st.post_number}
                </span>
                <span style="font-size:15px; font-weight:700; color:var(--text-main);">
                  ${escapeHtml(st.post_title)}
                </span>
                ${st.post_link ? `<a href="${st.post_link}" target="_blank" style="font-size:12px; color:var(--primary); font-weight:600; text-decoration:none;">View Channel Msg ↗</a>` : '<span style="color:#dc2626; font-size:11px; font-weight:600;">(Not Published)</span>'}
              </div>

              <div style="display:flex; flex-wrap:wrap; gap:14px; align-items:center; font-size:12px; color:var(--text-subtle); margin-top:8px;">
                <div>
                  <strong>Assigned Account (Slot #${st.stage}):</strong>
                  ${st.account_phone ? `<span style="color:var(--text-main); font-weight:600;">${st.account_phone} (${st.account_name})</span>` : '<span style="color:#dc2626; font-weight:600;">No Account Connected</span>'}
                </div>
                <div>
                  <strong>Current Stage Bots:</strong>
                  <span style="font-weight:700; color:var(--text-main);">${st.leads_in_stage}</span>
                </div>
                <div>
                  <strong>Cooldown Rule:</strong>
                  <span>${st.stage === 1 ? 'Immediate for new bots' : '7 Days after previous post'}</span>
                </div>
              </div>
            </div>

            <div style="display:flex; align-items:center; gap:12px;">
              <div style="text-align:right;">
                <div style="font-size:11px; font-weight:700; color:var(--text-subtle); text-transform:uppercase;">Eligible & Ready</div>
                <div style="font-size:22px; font-weight:800; color:${st.ready_to_send > 0 ? 'var(--primary)' : 'var(--text-subtle)'};">
                  ${st.ready_to_send} bots
                </div>
              </div>

              <div style="display:flex; align-items:center; gap:6px;">
                <select id="drip-limit-${st.stage}" style="padding:8px 10px; border:1px solid var(--border-subtle); border-radius:var(--radius-md); font-size:12px; font-weight:600;">
                  <option value="10">10 bots</option>
                  <option value="25" selected>25 bots</option>
                  <option value="50">50 bots</option>
                </select>

                <button class="btn btn-primary" onclick="runDripStage(${st.stage})" ${isReady ? '' : 'disabled'} style="white-space:nowrap; ${isReady ? '' : 'opacity:0.5; cursor:not-allowed;'}">
                  🚀 Run Week ${st.stage} Batch
                </button>
              </div>
            </div>
          `;
          container.appendChild(card);
        });

      } catch (e) {
        console.error("Failed to load drip overview", e);
      }
    }

    async function runDripStage(stage) {
      const limitSelect = document.getElementById(`drip-limit-${stage}`);
      const limit = limitSelect ? parseInt(limitSelect.value, 10) : 25;

      if (!confirm(`Execute 5-Week Drip for Week #${stage}? This will forward Post #${stage} to up to ${limit} eligible deal bots with an assigned user account.`)) {
        return;
      }

      showToast(`Starting Week #${stage} forwarding batch...`);

      try {
        const res = await fetch('/api/drip/execute-stage', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            stage: stage,
            batch_size: limit,
            delay_seconds: 12.0
          })
        });
        const data = await res.json();
        if (data.status === 'completed') {
          showToast(`Stage #${stage} complete! Delivered: ${data.successful}, Failed: ${data.failed} (Targeted: ${data.targeted})`);
          await loadDripOverview();
        } else {
          showToast(data.error || "Stage execution failed");
        }
      } catch (e) {
        showToast("Network error executing drip stage");
      }
    }

    // ==========================================
    // Autonomous Keyword Search JavaScript
    // ==========================================

    let discoveredChannelsDebounceTimer = null;
    function debounceDiscoveredSearch() {
      clearTimeout(discoveredChannelsDebounceTimer);
      discoveredChannelsDebounceTimer = setTimeout(loadDiscoveredChannels, 300);
    }

    function openAddKeywordModal() {
      const modal = document.getElementById('modal-add-keyword');
      if (modal) {
        modal.style.display = 'flex';
        const input = document.getElementById('modal-kw-input');
        if (input) input.focus();
      }
    }

    function closeAddKeywordModal() {
      const modal = document.getElementById('modal-add-keyword');
      if (modal) modal.style.display = 'none';
    }

    async function submitAddKeyword() {
      const kwInput = document.getElementById('modal-kw-input');
      const catSelect = document.getElementById('modal-kw-category');
      const kw = kwInput ? kwInput.value.trim() : '';
      const cat = catSelect ? catSelect.value : 'deals';

      if (!kw) {
        showToast("Please enter a keyword.");
        return;
      }

      try {
        const res = await fetch('/api/keywords', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ keyword: kw, category: cat })
        });
        const data = await res.json();
        if (data.status === 'success') {
          showToast(`Added keyword "${kw}" successfully!`);
          closeAddKeywordModal();
          if (kwInput) kwInput.value = '';
          await loadKeywordsData();
        } else {
          showToast(data.error || "Failed to add keyword");
        }
      } catch (e) {
        showToast("Network error adding keyword");
      }
    }

    async function loadKeywordsData() {
      try {
        const res = await fetch('/api/keywords');
        const data = await res.json();
        if (data.status !== 'success') return;

        const stats = data.stats || {};
        const kwActive = document.getElementById('kw-active-count');
        const kwTotal = document.getElementById('kw-total-count');
        const kwChannels = document.getElementById('kw-channels-count');
        const kwLeads = document.getElementById('kw-leads-count');
        const kwSidebar = document.getElementById('keywords-sidebar-count');

        if (kwActive) kwActive.textContent = stats.active_keywords || 0;
        if (kwTotal) kwTotal.textContent = `${stats.total_keywords || 0} configured targets`;
        if (kwChannels) kwChannels.textContent = stats.total_channels_discovered || 0;
        if (kwLeads) kwLeads.textContent = stats.total_leads_from_keywords || 0;
        if (kwSidebar) kwSidebar.textContent = stats.total_channels_discovered || 0;

        const tbody = document.getElementById('keywords-table-body');
        if (!tbody) return;

        const keywords = data.keywords || [];
        if (keywords.length === 0) {
          tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding:30px; color:var(--text-subtle);">No keywords added yet. Click "+ Add Keyword" above.</td></tr>';
          return;
        }

        tbody.innerHTML = keywords.map(k => {
          const lastStr = k.last_searched_at ? new Date(k.last_searched_at).toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}) : 'Never';
          const isEnabled = k.enabled !== false;
          const statusBadge = isEnabled 
            ? `<span class="status-chip chip-active" style="cursor:pointer;" onclick="toggleKeywordStatus('${k.keyword}', false)">🟢 Active</span>`
            : `<span class="status-chip chip-ignored" style="cursor:pointer;" onclick="toggleKeywordStatus('${k.keyword}', true)">⏸️ Paused</span>`;
          
          return `
            <tr>
              <td>
                <span style="font-weight:700; color:var(--text-main); font-size:14px;">"${k.keyword}"</span>
              </td>
              <td>
                <span class="badge badge-secondary" style="text-transform:uppercase; font-size:10px; font-weight:700;">${k.category || 'deals'}</span>
              </td>
              <td><span style="font-weight:700; color:var(--primary);">${k.total_channels_found || 0}</span></td>
              <td><span style="font-weight:700; color:var(--success);">${k.total_leads_extracted || 0}</span></td>
              <td><span style="font-size:12px; color:var(--text-subtle);">${lastStr}</span></td>
              <td>${statusBadge}</td>
              <td>
                <div style="display:flex; gap:6px;">
                  <button class="btn btn-sm btn-outline" style="padding:3px 8px; font-size:11px;" title="Search this keyword immediately" onclick="quickSearchKeyword('${k.keyword}')">⚡ Run</button>
                  <button class="btn btn-sm btn-outline" style="padding:3px 8px; font-size:11px; color:var(--danger); border-color:#fecaca;" title="Delete keyword" onclick="deleteKeyword('${k.keyword}')">🗑️</button>
                </div>
              </td>
            </tr>
          `;
        }).join('');

      } catch (e) {
        console.error("Error loading keywords:", e);
      }
    }

    async function toggleKeywordStatus(keyword, enable) {
      try {
        const res = await fetch(`/api/keywords/${encodeURIComponent(keyword)}/toggle`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: enable })
        });
        const data = await res.json();
        if (data.status === 'success') {
          showToast(`Keyword "${keyword}" ${enable ? 'activated' : 'paused'}.`);
          await loadKeywordsData();
        }
      } catch (e) {
        showToast("Error updating keyword status");
      }
    }

    async function deleteKeyword(keyword) {
      if (!confirm(`Delete target keyword "${keyword}"?`)) return;
      try {
        const res = await fetch(`/api/keywords/${encodeURIComponent(keyword)}`, {
          method: 'DELETE'
        });
        const data = await res.json();
        if (data.status === 'success') {
          showToast(`Keyword "${keyword}" deleted.`);
          await loadKeywordsData();
        }
      } catch (e) {
        showToast("Error deleting keyword");
      }
    }

    async function quickSearchKeyword(keyword) {
      const input = document.getElementById('live-search-input');
      if (input) input.value = keyword;
      await executeInstantSearch();
    }

    async function executeInstantSearch() {
      const input = document.getElementById('live-search-input');
      const kw = input ? input.value.trim() : '';
      if (!kw) {
        showToast("Please enter a keyword to search.");
        return;
      }
      const limitSelect = document.getElementById('live-search-limit');
      const limit = limitSelect ? parseInt(limitSelect.value, 10) : 15;
      const btn = document.getElementById('btn-live-search');
      const resultsDiv = document.getElementById('live-search-results');

      if (btn) {
        btn.disabled = true;
        btn.textContent = "⏳ Searching Telegram MTProto...";
      }
      if (resultsDiv) {
        resultsDiv.style.display = 'block';
        resultsDiv.innerHTML = `<div style="text-align:center; padding:12px; color:var(--text-subtle);">Connecting to Telegram MTProto and scanning global directory for <b>"${kw}"</b>...</div>`;
      }
      showToast(`Searching Telegram global directory for "${kw}"...`);

      try {
        const res = await fetch('/api/keywords/search-now', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ keyword: kw, limit: limit })
        });
        const data = await res.json();

        if (data.status === 'success') {
          showToast(`Search complete: ${data.channels_found} channels found, ${data.leads_extracted} leads extracted!`);
          let html = `
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
              <span style="font-weight:700; color:var(--text-main);">Discovered ${data.channels_found} Channels & ${data.leads_extracted} Leads</span>
              <span style="font-size:12px; color:var(--success); font-weight:700;">✅ Saved to MongoDB</span>
            </div>
            <div style="display:flex; flex-direction:column; gap:8px;">
          `;
          (data.discovered || []).forEach(c => {
            const leadsBadges = (c.leads || []).map(l => `<span class="badge ${l.toLowerCase().endsWith('bot') ? 'badge-primary' : 'badge-success'}" style="font-size:11px;">@${l}</span>`).join(' ') || '<span style="font-size:11px; color:var(--text-subtle);">No contact in bio</span>';
            html += `
              <div style="background:#fff; border:1px solid var(--border-subtle); border-radius:6px; padding:10px; display:flex; justify-content:space-between; align-items:center;">
                <div>
                  <div style="font-weight:600; font-size:13px; color:var(--text-main);">${c.title} ${c.username ? `<a href="https://t.me/${c.username}" target="_blank" style="color:var(--primary); font-size:11px; text-decoration:none;">(@${c.username})</a>` : ''}</div>
                  <div style="font-size:11px; color:var(--text-subtle); margin-top:2px;">Subscribers: ${c.subscribers ? c.subscribers.toLocaleString() : 'N/A'}</div>
                </div>
                <div>${leadsBadges}</div>
              </div>
            `;
          });
          html += '</div>';
          if (resultsDiv) resultsDiv.innerHTML = html;
          await Promise.all([loadKeywordsData(), loadDiscoveredChannels(), loadStats(), loadLeads()]);
        } else {
          showToast(data.message || data.error || "Search failed");
          if (resultsDiv) resultsDiv.innerHTML = `<div style="color:var(--danger); font-size:13px;">Error: ${data.message || data.error || "Search failed"}</div>`;
        }
      } catch (e) {
        showToast("Network error executing search");
        if (resultsDiv) resultsDiv.innerHTML = `<div style="color:var(--danger); font-size:13px;">Network error during search execution.</div>`;
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = "🔍 Search & Extract Now";
        }
      }
    }

    async function runAutonomousCycleNow() {
      const btn = document.getElementById('btn-run-auto-cycle');
      if (btn) {
        btn.disabled = true;
        btn.textContent = "⏳ Running Cycle...";
      }
      showToast("Starting autonomous discovery cycle across target keywords...");
      try {
        const res = await fetch('/api/keywords/autonomous/cycle', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'completed') {
          showToast(`Cycle complete! Discovered ${data.channels_found} channels and ${data.leads_extracted} leads.`);
          await Promise.all([loadKeywordsData(), loadDiscoveredChannels(), loadStats(), loadLeads()]);
        } else {
          showToast(data.message || data.status || "Cycle executed");
        }
      } catch (e) {
        showToast("Error executing autonomous cycle");
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = "⚡ Run Auto-Cycle Now";
        }
      }
    }

    async function loadDiscoveredChannels() {
      const searchInput = document.getElementById('discovered-channels-search');
      const search = searchInput ? searchInput.value.trim() : '';
      const tbody = document.getElementById('discovered-channels-body');
      if (!tbody) return;

      try {
        const url = search 
          ? `/api/keywords/channels?limit=50&search=${encodeURIComponent(search)}`
          : `/api/keywords/channels?limit=50`;
        const res = await fetch(url);
        const data = await res.json();
        const channels = data.channels || [];

        if (channels.length === 0) {
          tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:30px; color:var(--text-subtle);">No channels discovered yet. Run a search to populate.</td></tr>';
          return;
        }

        tbody.innerHTML = channels.map(c => {
          const userLink = c.username 
            ? `<a href="https://t.me/${c.username}" target="_blank" style="color:var(--primary); text-decoration:none; font-weight:600;">@${c.username}</a>`
            : (c.invite_link ? `<a href="${c.invite_link}" target="_blank" style="color:var(--text-subtle); font-size:11px;">Join Link</a>` : '<span style="color:var(--text-subtle); font-size:11px;">Private</span>');
          const subs = c.subscribers_count ? c.subscribers_count.toLocaleString() : 'N/A';
          const leadsBadge = c.extracted_leads_count > 0
            ? `<span class="badge badge-success" style="font-weight:700;">${c.extracted_leads_count} Leads</span>`
            : `<span style="font-size:11px; color:var(--text-subtle);">0 Leads</span>`;

          return `
            <tr>
              <td>
                <div style="font-weight:600; color:var(--text-main);">${c.title || 'Untitled'}</div>
                ${c.bio ? `<div style="font-size:11px; color:var(--text-subtle); max-width:260px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${c.bio}</div>` : ''}
              </td>
              <td>${userLink}</td>
              <td><span class="badge badge-secondary" style="font-weight:600; font-size:11px;">"${c.matching_keyword || ''}"</span></td>
              <td><span style="font-weight:600; color:var(--text-muted);">${subs}</span></td>
              <td>${leadsBadge}</td>
              <td>
                ${c.username ? `<a href="https://t.me/${c.username}" target="_blank" class="btn btn-sm btn-outline" style="padding:3px 8px; font-size:11px; text-decoration:none;">Open ↗</a>` : '-'}
              </td>
            </tr>
          `;
        }).join('');
      } catch (e) {
        console.error("Error loading discovered channels:", e);
      }
    }

    // Global fetch interceptor: redirect to login if session expires
    const _nativeFetch = window.fetch;
    window.fetch = async function(...args) {
      const resp = await _nativeFetch.apply(this, args);
      if (resp && resp.status === 401) {
        const url = (typeof args[0] === 'string') ? args[0] : (args[0]?.url || '');
        if (!url.includes('/api/auth/')) {
          window.location.href = '/login';
        }
      }
      return resp;
    };

    async function handleDashboardLogout() {
      try {
        await fetch('/api/auth/logout', { method: 'POST' });
      } catch (e) {}
      window.location.href = '/login';
    }

    async function checkAuthSession() {
      try {
        const res = await fetch('/api/auth/status');
        const data = await res.json();
        if (!data.authenticated) {
          window.location.href = '/login';
        } else if (data.email) {
          const topEmail = document.getElementById('topbar-user-email');
          const sideEmail = document.getElementById('sidebar-user-email');
          if (topEmail) topEmail.textContent = data.email;
          if (sideEmail) sideEmail.textContent = data.email;
        }
      } catch (e) {}
    }

    async function loadAll() {
      await Promise.all([
        loadStats(),
        loadLeads(),
        loadLogs(),
        loadAccounts(),
        loadDripOverview(),
        loadBotOutreachData(),
        loadKeywordsData(),
        loadDiscoveredChannels()
      ]);
    }

    // Auto-refresh every 20 seconds
    window.addEventListener('DOMContentLoaded', () => {
      checkAuthSession();
      loadAll();
      setInterval(loadAll, 20000);
    });
  </script>
</body>
</html>
"""
