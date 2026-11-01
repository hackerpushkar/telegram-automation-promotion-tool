import os
import csv
import io
import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.config import settings
from src.db import Database
from src.models import Lead, LeadType, LeadStatus, LeadSourceInfo, PromotionalPost, SendingReport
from src.extractor import extract_leads_from_text
from src.api_client import ProaffiliatersApiClient
from src.account_manager import AccountManager
from src.delivery_engine import delivery_engine
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
    description="ProAffiliaters Lead Extraction & Outreach Interface",
    version="2.0.0"
)

@app.middleware("http")
async def auth_guard_middleware(request: Request, call_next):
    path = request.url.path

    # Public paths exempt from authentication
    if (
        not settings.dashboard_auth_enabled
        or path in ["/login", "/api/auth/login", "/api/auth/status", "/api/auth/logout", "/favicon.ico"]
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
        "message": "ProAffiliaters Lead Engine initialized."
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


# --- Pydantic Schemas ---
class LoginRequest(BaseModel):
    email: str
    password: str

class StatusUpdateRequest(BaseModel):
    status: str

class AddLeadRequest(BaseModel):
    username: str
    lead_type: str = "bot"  # "bot" or "user"
    source: Optional[str] = "Manual Entry"

class CreatePostRequest(BaseModel):
    title: str
    message_text: str
    image_url: Optional[str] = None
    button_text: Optional[str] = None
    button_url: Optional[str] = None

class StartManualSendingRequest(BaseModel):
    target_type: str = "all_userbots"  # all_userbots, all_usernames, uncontacted, custom
    custom_leads: Optional[List[str]] = None
    message_text: str
    image_url: Optional[str] = None
    button_text: Optional[str] = None
    button_url: Optional[str] = None
    post_title: Optional[str] = "Manual Campaign"
    delay_seconds: int = 15
    max_per_account: int = 50

class StartAccountLoginRequest(BaseModel):
    phone: str
    slot: Optional[int] = None

class CompleteAccountLoginRequest(BaseModel):
    session_token: str
    code: Optional[str] = None
    password: Optional[str] = None

class ScanTestRequest(BaseModel):
    bio_text: str
    channel_name: Optional[str] = "Manual Test Channel"
    invite_link: Optional[str] = "https://t.me/test_channel"


# --- API Routes ---

# 1. Authentication
@app.post("/api/auth/login")
async def login_endpoint(req: LoginRequest):
    """Authenticates admin and returns a signed 30-day session token."""
    if not authenticate_user(req.email, req.password):
        log_activity(f"Failed dashboard login attempt for: '{req.email}'", "info")
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
        secure=False
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

# 2. Real-time Dashboard Stats
@app.get("/api/stats")
async def get_dashboard_stats():
    """Returns aggregated stats across leads, accounts, and sending engine."""
    leads = await Database.get_leads(limit=10000)
    accounts = await Database.get_all_telegram_accounts()
    reports_analytics = await Database.get_sending_reports_analytics()

    userbots_count = sum(1 for l in leads if l.get("lead_type") == "bot" or (str(l.get("username", "")).lower().endswith("bot")))
    usernames_count = sum(1 for l in leads if l.get("lead_type") == "user" and not (str(l.get("username", "")).lower().endswith("bot")))
    contacted_count = sum(1 for l in leads if str(l.get("status", "")).lower() in ["contacted", "replied", "converted"])

    active_accounts = sum(1 for a in accounts if a.get("is_authorized", True))

    return {
        "total_leads": len(leads),
        "userbots_count": userbots_count,
        "usernames_count": usernames_count,
        "contacted_count": contacted_count,
        "pending_count": max(0, len(leads) - contacted_count),
        "total_accounts": len(accounts),
        "active_accounts": active_accounts,
        "messages_sent_today": reports_analytics.get("sent_today", 0),
        "total_messages_sent": reports_analytics.get("total_sent", 0),
        "success_rate_percent": reports_analytics.get("success_rate_percent", 100.0),
        "api_url": settings.proaffiliaters_api_url,
        "sync_interval": f"{settings.sync_interval_minutes} Minutes",
        "telegram_configured": bool(settings.telegram_api_id and settings.telegram_api_hash),
        "campaign_status": delivery_engine.get_campaign_status()
    }

# 3. Section 1: Leads
@app.get("/api/leads")
async def list_leads(
    search: Optional[str] = Query(None),
    lead_type: Optional[str] = Query("all"),
    status: Optional[str] = Query("all"),
    limit: int = Query(300, ge=1, le=1000),
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

@app.post("/api/leads")
async def add_lead_manual(req: AddLeadRequest):
    """Manually adds a new lead to MongoDB."""
    clean_handle = req.username.strip().lstrip("@")
    if not clean_handle:
        return JSONResponse(status_code=400, content={"error": "Username is required."})

    is_bot = (req.lead_type.lower() == "bot") or clean_handle.lower().endswith("bot")
    lead_type_enum = LeadType.BOT if is_bot else LeadType.USER

    lead = Lead(
        username=f"@{clean_handle}",
        username_clean=clean_handle.lower(),
        lead_type=lead_type_enum,
        status=LeadStatus.PENDING,
        first_seen_at=datetime.utcnow(),
        extracted_from=LeadSourceInfo(
            source_name=req.source or "Manual Entry",
            source_invite_link="",
            platform="telegram",
            source_id="manual_entry"
        )
    )
    is_new, _ = await Database.upsert_lead(lead)
    log_activity(f"Added lead: @{clean_handle} ({lead_type_enum.value})", "lead")
    return {"success": True, "is_new": is_new, "username": clean_handle}

@app.patch("/api/leads/{username_clean}/status")
async def update_lead_status(username_clean: str, req: StatusUpdateRequest):
    """Updates lead outreach status."""
    clean = username_clean.strip().lstrip("@").lower()
    success = await Database.update_status(clean, req.status)
    if success:
        log_activity(f"Updated lead @{clean} status to '{req.status}'", "update")
        return {"success": True, "username": clean, "status": req.status}
    return JSONResponse(status_code=404, content={"error": "Lead not found."})

@app.delete("/api/leads/{username_clean}")
async def delete_lead_endpoint(username_clean: str):
    """Deletes a lead from MongoDB."""
    clean = username_clean.strip().lstrip("@").lower()
    ok = await Database.delete_lead(clean)
    if ok:
        log_activity(f"Deleted lead @{clean}", "info")
    return {"success": ok}

@app.get("/api/leads/export-csv")
async def export_leads_csv(
    lead_type: Optional[str] = Query("all"),
    status: Optional[str] = Query("all"),
    search: Optional[str] = Query(None)
):
    """Exports filtered leads as CSV."""
    leads = await Database.get_leads(search=search, lead_type=lead_type, status=status, limit=10000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Username", "Lead Type", "Status", "Discovered From", "First Seen At", "Last Contacted At"])
    for l in leads:
        raw_src = l.get("extracted_from")
        source_str = "Direct"
        if isinstance(raw_src, dict):
            source_str = raw_src.get("source_name") or raw_src.get("channel_name") or "Direct"
        elif isinstance(raw_src, list):
            items = []
            for item in raw_src:
                if isinstance(item, dict):
                    name = item.get("source_name") or item.get("channel_name")
                    if name:
                        items.append(name)
                elif isinstance(item, str):
                    items.append(item)
            source_str = ", ".join(items) or "Direct"
        elif isinstance(raw_src, str):
            source_str = raw_src or "Direct"

        writer.writerow([
            l.get("username", ""),
            l.get("lead_type", ""),
            l.get("status", ""),
            source_str,
            l.get("first_seen_at", ""),
            l.get("last_contacted_at", "")
        ])
    filename = f"leads_{lead_type}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# 4. Section 2: Promotional Posts & Manual Sending
@app.get("/api/posts")
async def list_promotional_posts():
    """Returns all saved promotional post templates."""
    posts = await Database.get_all_promotional_posts()
    return {"count": len(posts), "posts": posts}

@app.post("/api/posts")
async def save_promotional_post_endpoint(req: CreatePostRequest):
    """Creates/saves a promotional post template."""
    if not req.title.strip() or not req.message_text.strip():
        return JSONResponse(status_code=400, content={"error": "Title and Message Text are required."})

    post = PromotionalPost(
        title=req.title.strip(),
        message_text=req.message_text.strip(),
        image_url=req.image_url.strip() if req.image_url else None,
        button_text=req.button_text.strip() if req.button_text else None,
        button_url=req.button_url.strip() if req.button_url else None
    )
    saved = await Database.save_promotional_post(post)
    log_activity(f"Saved promotional post template: '{post.title}'", "sync")
    return {"success": True, "post": saved}

@app.delete("/api/posts/{post_id}")
async def delete_promotional_post_endpoint(post_id: str):
    """Deletes a promotional post template."""
    ok = await Database.delete_promotional_post(post_id)
    if ok:
        log_activity(f"Deleted promotional post template {post_id}", "info")
    return {"success": ok}

@app.post("/api/sending/start")
async def start_manual_sending(req: StartManualSendingRequest):
    """Starts a promotional sending campaign with account rotation."""
    if not req.message_text.strip():
        return JSONResponse(status_code=400, content={"error": "Message text is required."})

    status = delivery_engine.get_campaign_status()
    if status.get("is_running"):
        return JSONResponse(status_code=400, content={"error": "A campaign is already running. Please stop or wait for it to complete."})

    success, message = await delivery_engine.start_campaign(
        target_type=req.target_type,
        custom_leads=req.custom_leads,
        message_text=req.message_text,
        image_url=req.image_url,
        button_text=req.button_text,
        button_url=req.button_url,
        post_title=req.post_title or "Manual Campaign",
        delay_seconds=req.delay_seconds,
        max_per_account=req.max_per_account
    )
    if not success:
        return JSONResponse(status_code=400, content={"error": message})

    log_activity(f"Promotional campaign dispatched: {message}", "sync")
    return {"success": True, "message": message, "status": delivery_engine.get_campaign_status()}

@app.get("/api/sending/status")
async def get_sending_campaign_status():
    """Returns the live status, progress, and real-time logs of the dispatch engine."""
    return delivery_engine.get_campaign_status()

@app.post("/api/sending/stop")
async def stop_sending_campaign():
    """Stops the active sending campaign."""
    ok = delivery_engine.stop_campaign()
    log_activity("Promotional sending campaign stopped by user.", "info")
    return {"success": ok, "status": delivery_engine.get_campaign_status()}

# 5. Section 3: Login Accounts
@app.get("/api/accounts")
async def get_accounts_list():
    """Returns all registered Telegram accounts."""
    accounts = await Database.get_all_telegram_accounts()
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
        log_activity(f"Initiated Telegram login for {req.phone}", "sync")
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
            log_activity(f"Connected Telegram account {acc.get('phone')} (Ready for Rotation)", "sync")
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
    """Removes mock/demo accounts from MongoDB."""
    accounts = await Database.get_all_telegram_accounts()
    cleared = 0
    for acc in accounts:
        phone = acc.get("phone", "")
        if phone.startswith("+1999") or "manager_slot" in acc.get("first_name", "").lower():
            if await Database.delete_telegram_account(phone):
                cleared += 1
    log_activity(f"Cleared {cleared} demo Telegram accounts from database.", "info")
    return {"success": True, "cleared_count": cleared}

# 6. Section 4: Automatic Sending Report
@app.get("/api/reports/analytics")
async def get_reports_analytics():
    """Returns analytics on all sent promotional messages."""
    return await Database.get_sending_reports_analytics()

@app.get("/api/reports")
async def get_sending_reports_list(
    limit: int = Query(100, ge=1, le=1000),
    skip: int = Query(0, ge=0),
    status: Optional[str] = Query(None)
):
    """Returns recent message sending audit logs."""
    reports = await Database.get_sending_reports(limit=limit, skip=skip, status=status)
    total_count = await Database.get_sending_reports_count(status=status)
    return {"count": len(reports), "total_count": total_count, "reports": reports}

@app.get("/api/reports/export-csv")
async def export_sending_reports_csv(status: Optional[str] = Query(None)):
    """Exports sending reports audit log as CSV."""
    reports = await Database.get_sending_reports(limit=10000, status=status)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Timestamp", "Recipient Handle", "Lead Type", "Sender Account Phone", "Sender Account Name", "Post Title", "Status", "Error Message", "Message Snippet"])
    for r in reports:
        writer.writerow([
            r.get("sent_at", ""),
            r.get("recipient_username", ""),
            r.get("recipient_type", ""),
            r.get("sender_account_phone", ""),
            r.get("sender_account_name", ""),
            r.get("post_title", ""),
            r.get("status", ""),
            r.get("error_message", ""),
            r.get("message_snippet", "")
        ])
    filename = f"sending_reports_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# 7. Utilities & Manual Trigger
@app.post("/api/trigger-sync")
async def trigger_manual_sync():
    """Triggers an immediate external source fetch from ProAffiliaters API."""
    log_activity("Manual 30-minute sync cycle initiated by admin from Web UI.", "sync")
    api_client = ProaffiliatersApiClient()
    sources = await api_client.fetch_recent_sources()
    if not sources:
        log_activity("External API sync finished: No new source channels found.", "info")
        return {"status": "none", "message": "No new sources found in window", "sources_count": 0}

    log_activity(f"External API sync found {len(sources)} source channels. Processing...", "lead")
    return {"status": "success", "message": f"Discovered {len(sources)} channels", "sources_count": len(sources)}

@app.get("/api/logs")
async def get_recent_activity_logs():
    """Returns recent dashboard activity logs."""
    return {"logs": activity_logs[:50]}

@app.post("/api/scan-test")
async def scan_test_bio(req: ScanTestRequest):
    """Extracts leads from bio text for testing."""
    leads = extract_leads_from_text(
        bio_text=req.bio_text,
        source_name=req.channel_name or "Test Channel",
        source_invite_link=req.invite_link or "",
        source_id="manual_test"
    )
    saved_count = 0
    for lead in leads:
        is_new, _ = await Database.upsert_lead(lead)
        if is_new:
            saved_count += 1
            log_activity(f"Extracted lead: @{lead.username} ({lead.lead_type.value})", "lead")
    return {"extracted_count": len(leads), "newly_saved": saved_count, "leads": [l.model_dump() for l in leads]}

# --- UI Routes ---
@app.get("/login", response_class=HTMLResponse)
async def serve_login(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)
    return LOGIN_HTML_CONTENT

@app.get("/logout")
async def logout_redirect():
    res = RedirectResponse(url="/login", status_code=302)
    res.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return res

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
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
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Outfit:wght@500;600;700;800&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      theme: {
        extend: {
          fontFamily: {
            sans: ['"Plus Jakarta Sans"', 'sans-serif'],
            display: ['Outfit', 'sans-serif'],
          },
          colors: {
            brand: {
              50: '#eef2ff',
              100: '#e0e7ff',
              200: '#c7d2fe',
              500: '#4f46e5',
              600: '#4338ca',
              700: '#3730a3',
            }
          },
          boxShadow: {
            'card': '0 1px 3px 0 rgba(0, 0, 0, 0.05), 0 1px 2px -1px rgba(0, 0, 0, 0.05)',
            'card-hover': '0 10px 15px -3px rgba(0, 0, 0, 0.07), 0 4px 6px -4px rgba(0, 0, 0, 0.05)',
            'nav-active': '0 4px 12px rgba(79, 70, 229, 0.25)',
          }
        }
      }
    }
  </script>
  <style>
    body { font-family: 'Plus Jakarta Sans', sans-serif; background-color: #f8fafc; color: #1e293b; }
    .font-display { font-family: 'Outfit', sans-serif; }
    
    /* Clean Active Navigation Item */
    .nav-item.active {
      background: linear-gradient(135deg, #4f46e5 0%, #4338ca 100%);
      color: #ffffff !important;
      box-shadow: 0 4px 14px 0 rgba(79, 70, 229, 0.35);
      font-weight: 700;
    }
    .nav-item.active svg { color: #ffffff !important; }
    .nav-item.active .nav-badge {
      background: rgba(255, 255, 255, 0.22) !important;
      color: #ffffff !important;
    }

    /* Scrollbars */
    ::-webkit-scrollbar { width: 5px; height: 5px; }
    ::-webkit-scrollbar-track { background: #f1f5f9; }
    ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 9999px; }
    ::-webkit-scrollbar-thumb:hover { background: #94a3b8; }
  </style>
</head>
<body class="min-h-screen text-slate-800 flex flex-col antialiased selection:bg-indigo-500 selection:text-white">

  <!-- Toast Notification Container -->
  <div id="toast-container" class="fixed top-5 right-5 z-50 flex flex-col gap-2 pointer-events-none"></div>

  <div class="flex-1 flex min-h-screen">

    <!-- ======================================================== -->
    <!-- SIDEBAR: EXACTLY 4 PRODUCTION SECTIONS (NO TEXT WRAP)   -->
    <!-- ======================================================== -->
    <aside class="w-72 bg-white border-r border-slate-200/90 flex flex-col shrink-0 select-none">
      
      <!-- Logo Header -->
      <div class="h-20 flex items-center px-6 border-b border-slate-100 gap-3.5">
        <div class="w-10 h-10 rounded-2xl bg-gradient-to-tr from-indigo-600 via-indigo-700 to-blue-600 flex items-center justify-center text-white font-display font-black text-xl shadow-md shadow-indigo-500/25">
          P
        </div>
        <div>
          <div class="font-display font-extrabold text-slate-900 text-base leading-tight flex items-center gap-1.5">
            ProAffiliaters
            <span class="text-[9px] uppercase font-black tracking-wider px-1.5 py-0.5 rounded-md bg-indigo-50 text-indigo-700 border border-indigo-200/60">PRO</span>
          </div>
          <div class="text-[11px] text-slate-400 font-semibold tracking-wide">Telegram Lead Engine</div>
        </div>
      </div>

      <!-- Navigation Menu -->
      <div class="px-5 pt-6 pb-2 text-[11px] font-bold text-slate-400 uppercase tracking-wider">Outreach Pipeline</div>
      
      <nav class="space-y-1.5 px-3 flex-1">
        
        <!-- Tab 1: Leads -->
        <button id="nav-btn-leads" onclick="switchTab('tab-leads')" class="nav-item active w-full flex items-center gap-3.5 px-4 py-3 rounded-xl text-sm font-semibold transition-all whitespace-nowrap">
          <svg class="w-5 h-5 shrink-0 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z"></path>
          </svg>
          <span class="truncate">Leads Directory</span>
          <span id="nav-leads-badge" class="nav-badge ml-auto text-xs font-bold px-2 py-0.5 rounded-full bg-white/20 text-white">0</span>
        </button>

        <!-- Tab 2: Manual Sending -->
        <button id="nav-btn-sending" onclick="switchTab('tab-sending')" class="nav-item w-full flex items-center gap-3.5 px-4 py-3 rounded-xl text-sm font-medium text-slate-600 hover:bg-slate-50 hover:text-slate-900 transition-all whitespace-nowrap">
          <svg class="w-5 h-5 shrink-0 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"></path>
          </svg>
          <span class="truncate">Manual Sending</span>
          <span id="nav-sending-badge" class="nav-badge ml-auto text-[10px] font-bold px-2 py-0.5 rounded-full bg-slate-100 text-slate-600">Rotation</span>
        </button>

        <!-- Tab 3: Login Accounts -->
        <button id="nav-btn-accounts" onclick="switchTab('tab-accounts')" class="nav-item w-full flex items-center gap-3.5 px-4 py-3 rounded-xl text-sm font-medium text-slate-600 hover:bg-slate-50 hover:text-slate-900 transition-all whitespace-nowrap">
          <svg class="w-5 h-5 shrink-0 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z"></path>
          </svg>
          <span class="truncate">Login Accounts</span>
          <span id="nav-accounts-badge" class="nav-badge ml-auto text-[10px] font-bold px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200/60">0 Active</span>
        </button>

        <!-- Tab 4: Automatic Sending Report -->
        <button id="nav-btn-reports" onclick="switchTab('tab-reports')" class="nav-item w-full flex items-center gap-3.5 px-4 py-3 rounded-xl text-sm font-medium text-slate-600 hover:bg-slate-50 hover:text-slate-900 transition-all whitespace-nowrap">
          <svg class="w-5 h-5 shrink-0 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"></path>
          </svg>
          <span class="truncate">Sending Reports</span>
          <span id="nav-reports-badge" class="nav-badge ml-auto text-xs font-bold px-2 py-0.5 rounded-full bg-slate-100 text-slate-600">0</span>
        </button>

      </nav>

      <!-- Sidebar Footer Status Card -->
      <div class="p-4 mx-3 mb-4 rounded-2xl bg-slate-50 border border-slate-200/80 space-y-2 text-xs">
        <div class="flex items-center gap-2 text-slate-700 font-semibold">
          <span class="w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse"></span>
          <span>30m Polling Active</span>
        </div>
        <div class="text-slate-400 text-[11px] truncate font-mono" id="sidebar-user-email">spushkar812@gmail.com</div>
        <div class="pt-1 border-t border-slate-200/60 flex items-center justify-between">
          <span class="text-[10px] text-slate-400 font-semibold uppercase">Engine Online</span>
          <a href="/logout" class="text-red-500 hover:text-red-600 font-bold text-xs hover:underline">Sign Out &rarr;</a>
        </div>
      </div>

    </aside>

    <!-- ======================================================== -->
    <!-- MAIN CONTENT AREA                                        -->
    <!-- ======================================================== -->
    <main class="flex-1 flex flex-col min-w-0 overflow-y-auto">

      <!-- TOP STICKY BAR -->
      <header class="h-20 bg-white/90 backdrop-blur-md border-b border-slate-200/90 flex items-center justify-between px-8 sticky top-0 z-30">
        <div>
          <div class="text-xs font-bold uppercase tracking-wider text-slate-400">Section View</div>
          <h1 id="header-breadcrumb" class="font-display font-extrabold text-xl text-slate-900 flex items-center gap-2">
            ProAffiliaters <span class="text-slate-300">/</span> <span class="text-indigo-600">Leads Directory</span>
          </h1>
        </div>

        <div class="flex items-center gap-3">
          <!-- Active Senders Pill -->
          <div class="hidden sm:flex items-center gap-2 px-3.5 py-1.5 rounded-full bg-emerald-50 text-emerald-800 border border-emerald-200/70 text-xs font-bold shadow-2xs">
            <span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
            <span id="top-accounts-status">0 Sending Accounts Ready</span>
          </div>

          <!-- Refresh Button -->
          <button onclick="refreshCurrentTab()" title="Refresh Data" class="p-2.5 rounded-xl border border-slate-200 bg-white hover:bg-slate-50 text-slate-600 shadow-2xs transition-all active:scale-95">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
          </button>

          <!-- Sync API Now Button -->
          <button onclick="triggerSyncNow()" class="flex items-center gap-2 px-4 py-2 rounded-xl bg-gradient-to-r from-indigo-600 to-blue-600 hover:from-indigo-700 hover:to-blue-700 text-white text-xs font-bold shadow-sm shadow-indigo-500/25 transition-all active:scale-95">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
            <span>Sync API Now</span>
          </button>
        </div>
      </header>

      <!-- PANELS WRAPPER -->
      <div class="p-8 max-w-7xl mx-auto w-full flex-1">

        <!-- ======================================================== -->
        <!-- SECTION 1: LEADS DIRECTORY (USERBOTS & USERNAMES TABS)   -->
        <!-- ======================================================== -->
        <section id="tab-leads" class="tab-content space-y-7">
          
          <!-- Section Header & Segmented Subtab Switch -->
          <div class="flex flex-col lg:flex-row lg:items-center justify-between gap-5 pb-2">
            <div>
              <h2 class="text-2xl font-display font-extrabold text-slate-900 tracking-tight">Leads Directory</h2>
              <p class="text-sm text-slate-500 mt-1">Manage deal channel userbots and human admin usernames extracted from external sources.</p>
            </div>

            <!-- Segmented Subtabs Pill -->
            <div class="inline-flex p-1.5 bg-slate-200/70 rounded-2xl border border-slate-300/60 shadow-inner select-none self-start lg:self-auto">
              <button id="subtab-btn-userbots" onclick="switchLeadSubTab('bot')" class="px-5 py-2.5 text-xs font-bold rounded-xl bg-indigo-600 text-white shadow-sm flex items-center gap-2.5 transition-all">
                <span>🤖 Userbots</span>
                <span id="badge-count-userbots" class="px-2 py-0.5 rounded-full bg-white/25 text-white text-[11px] font-extrabold">0</span>
              </button>
              <button id="subtab-btn-usernames" onclick="switchLeadSubTab('user')" class="px-5 py-2.5 text-xs font-semibold rounded-xl text-slate-600 hover:text-slate-900 hover:bg-slate-200/60 flex items-center gap-2.5 transition-all">
                <span>👤 Usernames</span>
                <span id="badge-count-usernames" class="px-2 py-0.5 rounded-full bg-slate-300 text-slate-700 text-[11px] font-extrabold">0</span>
              </button>
            </div>
          </div>

          <!-- 3 Modern Metric KPI Cards -->
          <div class="grid grid-cols-1 sm:grid-cols-3 gap-5">
            
            <!-- Card 1: Total Userbots -->
            <div class="bg-white p-6 rounded-3xl border border-slate-200/80 shadow-card hover:shadow-card-hover transition-all flex items-center gap-5 relative overflow-hidden group">
              <div class="w-14 h-14 rounded-2xl bg-indigo-50 text-indigo-600 border border-indigo-100 flex items-center justify-center font-bold text-2xl group-hover:scale-110 transition-transform">
                🤖
              </div>
              <div>
                <div class="text-xs font-bold text-slate-400 uppercase tracking-wider">Total Userbots</div>
                <div id="leads-stat-userbots" class="text-3xl font-display font-extrabold text-slate-900 mt-0.5">0</div>
                <div class="text-[11px] text-slate-400 mt-0.5 font-medium">Automated deal forwarding bots</div>
              </div>
              <div class="absolute top-0 right-0 w-24 h-24 bg-indigo-500/5 rounded-full blur-2xl pointer-events-none"></div>
            </div>

            <!-- Card 2: Total Usernames -->
            <div class="bg-white p-6 rounded-3xl border border-slate-200/80 shadow-card hover:shadow-card-hover transition-all flex items-center gap-5 relative overflow-hidden group">
              <div class="w-14 h-14 rounded-2xl bg-purple-50 text-purple-600 border border-purple-100 flex items-center justify-center font-bold text-2xl group-hover:scale-110 transition-transform">
                👤
              </div>
              <div>
                <div class="text-xs font-bold text-slate-400 uppercase tracking-wider">Total Usernames</div>
                <div id="leads-stat-usernames" class="text-3xl font-display font-extrabold text-slate-900 mt-0.5">0</div>
                <div class="text-[11px] text-slate-400 mt-0.5 font-medium">Channel owners & deal curators</div>
              </div>
              <div class="absolute top-0 right-0 w-24 h-24 bg-purple-500/5 rounded-full blur-2xl pointer-events-none"></div>
            </div>

            <!-- Card 3: Contacted Leads -->
            <div class="bg-white p-6 rounded-3xl border border-slate-200/80 shadow-card hover:shadow-card-hover transition-all flex items-center gap-5 relative overflow-hidden group">
              <div class="w-14 h-14 rounded-2xl bg-emerald-50 text-emerald-600 border border-emerald-100 flex items-center justify-center font-bold text-2xl group-hover:scale-110 transition-transform">
                ✅
              </div>
              <div>
                <div class="text-xs font-bold text-slate-400 uppercase tracking-wider">Contacted Leads</div>
                <div id="leads-stat-contacted" class="text-3xl font-display font-extrabold text-slate-900 mt-0.5">0</div>
                <div class="text-[11px] text-slate-400 mt-0.5 font-medium">Successfully reached via promos</div>
              </div>
              <div class="absolute top-0 right-0 w-24 h-24 bg-emerald-500/5 rounded-full blur-2xl pointer-events-none"></div>
            </div>

          </div>

          <!-- Controls Bar (Search, Status Filter, Add, Export) -->
          <div class="bg-white p-4 rounded-2xl border border-slate-200/80 shadow-card flex flex-col md:flex-row items-stretch md:items-center justify-between gap-3.5">
            <div class="flex flex-1 items-center gap-3">
              <!-- Search -->
              <div class="relative flex-1 max-w-md">
                <svg class="w-4 h-4 absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
                <input id="leads-search-input" oninput="debounceLeadsSearch()" type="text" placeholder="Search by @username or channel source..." class="w-full pl-10 pr-4 py-2.5 bg-slate-50 border border-slate-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:bg-white transition-all font-medium">
              </div>

              <!-- Status Filter -->
              <select id="leads-status-select" onchange="loadLeads()" class="py-2.5 px-3.5 bg-slate-50 border border-slate-200 rounded-xl text-xs font-semibold text-slate-700 focus:outline-none focus:ring-2 focus:ring-indigo-500 cursor-pointer">
                <option value="all">All Outreach Statuses</option>
                <option value="pending">⏳ Pending</option>
                <option value="contacted">✉️ Contacted</option>
                <option value="replied">💬 Replied</option>
                <option value="converted">🏆 Converted</option>
                <option value="ignored">🚫 Ignored</option>
              </select>
            </div>

            <div class="flex items-center gap-2.5">
              <button onclick="openAddLeadModal()" class="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-bold shadow-sm shadow-indigo-500/20 transition-all active:scale-95">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"></path></svg>
                <span>Add Lead</span>
              </button>

              <button onclick="exportLeadsCsv()" class="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-bold transition-all active:scale-95">
                <svg class="w-4 h-4 text-slate-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"></path></svg>
                <span>Export CSV</span>
              </button>
            </div>
          </div>

          <!-- Dynamic Leads Data Table -->
          <div class="bg-white rounded-3xl border border-slate-200/80 shadow-card overflow-hidden">
            <div class="overflow-x-auto">
              <table class="w-full text-left text-sm">
                <thead class="bg-slate-50/80 border-b border-slate-200/80 text-slate-500 uppercase text-[11px] font-bold tracking-wider">
                  <tr>
                    <th class="py-4 px-6">Lead Handle</th>
                    <th class="py-4 px-4">Classification</th>
                    <th class="py-4 px-4">Discovered Source</th>
                    <th class="py-4 px-4">Outreach Status</th>
                    <th class="py-4 px-4">Discovered Date</th>
                    <th class="py-4 px-6 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody id="leads-table-body" class="divide-y divide-slate-100">
                  <tr>
                    <td colspan="6" class="py-16 text-center text-slate-400">Loading leads...</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <!-- Table Footer -->
            <div class="p-4 bg-slate-50/60 border-t border-slate-100 flex items-center justify-between text-xs text-slate-500">
              <div id="leads-pagination-info" class="font-semibold text-slate-600">Showing 0 leads</div>
              <div class="flex gap-2">
                <button onclick="prevLeadsPage()" id="btn-leads-prev" class="px-3 py-1.5 rounded-lg border border-slate-200 bg-white font-semibold hover:bg-slate-50 disabled:opacity-40 transition-all">Previous</button>
                <button onclick="nextLeadsPage()" id="btn-leads-next" class="px-3 py-1.5 rounded-lg border border-slate-200 bg-white font-semibold hover:bg-slate-50 disabled:opacity-40 transition-all">Next</button>
              </div>
            </div>
          </div>

        </section>


        <!-- ======================================================== -->
        <!-- SECTION 2: MANUAL SENDING WITH ACCOUNT ROTATION          -->
        <!-- ======================================================== -->
        <section id="tab-sending" class="tab-content hidden space-y-7">
          
          <div class="pb-2">
            <h2 class="text-2xl font-display font-extrabold text-slate-900 tracking-tight">Manual Promotional Sending</h2>
            <p class="text-sm text-slate-500 mt-1">Compose promotional posts and dispatch to leads with automatic multi-account round-robin rotation and flood-wait protection.</p>
          </div>

          <!-- Two Column Composer & Config Layout -->
          <div class="grid grid-cols-1 lg:grid-cols-12 gap-7">

            <!-- LEFT: POST COMPOSER (7 COLS) -->
            <div class="lg:col-span-7 bg-white p-7 rounded-3xl border border-slate-200/80 shadow-card space-y-5">
              
              <div class="flex items-center justify-between pb-4 border-b border-slate-100">
                <h3 class="font-display font-bold text-slate-900 text-base flex items-center gap-2">
                  <span>✍️ Compose Promotional Post</span>
                </h3>
                
                <!-- Saved Templates Loader -->
                <div class="flex items-center gap-2">
                  <select id="composer-template-select" onchange="onTemplateSelected()" class="text-xs py-2 px-3 bg-slate-50 border border-slate-200 rounded-xl text-slate-700 font-semibold focus:ring-2 focus:ring-indigo-500">
                    <option value="">-- Load Saved Template --</option>
                  </select>
                  <button onclick="deleteSelectedTemplate()" title="Delete Template" class="p-2 text-slate-400 hover:text-red-500 rounded-xl border border-slate-200 hover:bg-red-50 transition-all">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path></svg>
                  </button>
                </div>
              </div>

              <!-- Post Title -->
              <div>
                <label class="block text-xs font-bold text-slate-600 uppercase tracking-wider mb-1.5">Post Title / Template Name</label>
                <input id="post-title" type="text" placeholder="e.g. Exclusive Festive Deal Offer" class="w-full px-4 py-2.5 bg-slate-50 border border-slate-200 rounded-xl text-sm font-medium focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:bg-white transition-all">
              </div>

              <!-- Promotional Message Textarea -->
              <div>
                <div class="flex items-center justify-between mb-1.5">
                  <label class="block text-xs font-bold text-slate-600 uppercase tracking-wider">Message Content</label>
                  <span class="text-[11px] text-slate-400 font-mono">*bold* _italic_ [link](url)</span>
                </div>
                <textarea id="post-message-text" oninput="updateTelegramBubblePreview()" rows="6" placeholder="Write promotional copy here...&#10;&#10;🔥 *Special Affiliate Offer:* Join India's top deal automation platform!&#10;👉 High commission rates & instant payouts." class="w-full px-4 py-3 bg-slate-50 border border-slate-200 rounded-xl text-sm font-sans focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:bg-white transition-all"></textarea>
              </div>

              <!-- Image URL & Preview -->
              <div>
                <label class="block text-xs font-bold text-slate-600 uppercase tracking-wider mb-1.5">Banner Image URL (Optional)</label>
                <input id="post-image-url" oninput="updateImagePreview(); updateTelegramBubblePreview();" type="url" placeholder="https://example.com/promo.jpg" class="w-full px-4 py-2.5 bg-slate-50 border border-slate-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:bg-white transition-all">
              </div>

              <!-- Inline Button Inputs -->
              <div class="grid grid-cols-1 sm:grid-cols-2 gap-3.5 pt-1">
                <div>
                  <label class="block text-xs font-bold text-slate-600 uppercase tracking-wider mb-1.5">Button Text (Optional)</label>
                  <input id="post-button-text" oninput="updateTelegramBubblePreview()" type="text" placeholder="e.g. 🚀 Join VIP Deals" class="w-full px-4 py-2.5 bg-slate-50 border border-slate-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                </div>
                <div>
                  <label class="block text-xs font-bold text-slate-600 uppercase tracking-wider mb-1.5">Button URL (Optional)</label>
                  <input id="post-button-url" oninput="updateTelegramBubblePreview()" type="url" placeholder="https://t.me/proaffiliaters" class="w-full px-4 py-2.5 bg-slate-50 border border-slate-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                </div>
              </div>

              <!-- Telegram Chat Bubble Mockup Preview -->
              <div class="pt-2">
                <div class="text-[11px] font-bold text-slate-400 uppercase tracking-wider mb-2">Telegram Client Chat Preview</div>
                <div class="p-4 bg-slate-100 rounded-2xl border border-slate-200/80">
                  <div class="max-w-sm bg-white rounded-2xl p-3.5 shadow-sm border border-slate-200/70 space-y-2.5">
                    <div id="preview-bubble-image-box" class="hidden rounded-xl overflow-hidden max-h-40 bg-slate-100">
                      <img id="preview-bubble-img" src="" alt="Promo banner" class="w-full object-cover">
                    </div>
                    <div id="preview-bubble-text" class="text-xs text-slate-800 whitespace-pre-wrap leading-relaxed font-sans">
                      Message preview will appear here...
                    </div>
                    <div id="preview-bubble-btn-box" class="hidden pt-1">
                      <div id="preview-bubble-btn-label" class="w-full py-2 px-3 text-center rounded-xl bg-blue-50 text-blue-600 font-bold text-xs border border-blue-200 shadow-2xs">
                        🚀 Button Preview
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <!-- Save Template Button -->
              <div class="pt-2 flex justify-end">
                <button onclick="savePostTemplate()" class="flex items-center gap-2 px-5 py-2.5 rounded-xl border border-indigo-600 text-indigo-600 hover:bg-indigo-50 font-bold text-xs transition-all active:scale-95">
                  <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7H5a2 2 0 00-2 2v9a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-3m-1 4l-3 3m0 0l-3-3m3 3V4"></path></svg>
                  <span>Save as Post Template</span>
                </button>
              </div>

            </div>

            <!-- RIGHT: DISPATCH & ACCOUNT ROTATION CONFIG (5 COLS) -->
            <div class="lg:col-span-5 space-y-6">

              <!-- Target Audience Box -->
              <div class="bg-white p-7 rounded-3xl border border-slate-200/80 shadow-card space-y-4">
                <h3 class="font-display font-bold text-slate-900 text-base flex items-center gap-2">
                  <span>🎯 Target Audience Selection</span>
                </h3>

                <div class="space-y-2.5">
                  <label class="flex items-center gap-3.5 p-3.5 rounded-2xl border border-slate-200/80 hover:border-indigo-400 bg-slate-50/60 cursor-pointer transition-all">
                    <input type="radio" name="sending-target" value="all_userbots" checked onchange="updateAudienceSelectionUI()" class="w-4 h-4 text-indigo-600 focus:ring-indigo-500">
                    <div class="flex-1">
                      <div class="text-sm font-bold text-slate-800">All Userbots</div>
                      <div class="text-xs text-slate-500 font-medium">Sends to all automated deal bots (<span id="target-count-userbots" class="font-bold text-indigo-600">0</span> ready)</div>
                    </div>
                  </label>

                  <label class="flex items-center gap-3.5 p-3.5 rounded-2xl border border-slate-200/80 hover:border-indigo-400 bg-slate-50/60 cursor-pointer transition-all">
                    <input type="radio" name="sending-target" value="all_usernames" onchange="updateAudienceSelectionUI()" class="w-4 h-4 text-indigo-600 focus:ring-indigo-500">
                    <div class="flex-1">
                      <div class="text-sm font-bold text-slate-800">All Usernames</div>
                      <div class="text-xs text-slate-500 font-medium">Sends to channel owners (<span id="target-count-usernames" class="font-bold text-purple-600">0</span> ready)</div>
                    </div>
                  </label>

                  <label class="flex items-center gap-3.5 p-3.5 rounded-2xl border border-slate-200/80 hover:border-indigo-400 bg-slate-50/60 cursor-pointer transition-all">
                    <input type="radio" name="sending-target" value="uncontacted" onchange="updateAudienceSelectionUI()" class="w-4 h-4 text-indigo-600 focus:ring-indigo-500">
                    <div class="flex-1">
                      <div class="text-sm font-bold text-slate-800">Uncontacted Leads Only</div>
                      <div class="text-xs text-slate-500 font-medium">Avoids leads already contacted (<span id="target-count-uncontacted" class="font-bold text-emerald-600">0</span> ready)</div>
                    </div>
                  </label>

                  <label class="flex items-center gap-3.5 p-3.5 rounded-2xl border border-slate-200/80 hover:border-indigo-400 bg-slate-50/60 cursor-pointer transition-all">
                    <input type="radio" name="sending-target" value="custom" onchange="updateAudienceSelectionUI()" class="w-4 h-4 text-indigo-600 focus:ring-indigo-500">
                    <div class="flex-1">
                      <div class="text-sm font-bold text-slate-800">Custom Handles List</div>
                      <div class="text-xs text-slate-500 font-medium">Paste custom handles to target directly</div>
                    </div>
                  </label>
                </div>

                <!-- Custom Handles Input (Hidden by default) -->
                <div id="custom-handles-box" class="hidden pt-1">
                  <label class="block text-xs font-bold text-slate-600 uppercase tracking-wider mb-1">Enter Handles (separated by commas or lines)</label>
                  <textarea id="custom-handles-text" rows="3" placeholder="@deal_robot, @owner1, @shopping_bot" class="w-full px-3.5 py-2.5 bg-slate-50 border border-slate-200 rounded-xl text-xs font-mono"></textarea>
                </div>
              </div>

              <!-- Account Rotation & Safety Settings -->
              <div class="bg-white p-7 rounded-3xl border border-slate-200/80 shadow-card space-y-5">
                <div class="flex items-center justify-between">
                  <h3 class="font-display font-bold text-slate-900 text-base">🔄 Account Rotation Pool</h3>
                  <span id="rotation-active-count-badge" class="text-xs font-bold px-3 py-1 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200/60">0 Accounts Ready</span>
                </div>

                <!-- Active Accounts In Rotation Pill List -->
                <div id="rotation-accounts-list" class="flex flex-wrap gap-2 p-3.5 rounded-2xl bg-slate-50 border border-slate-200/80 min-h-[48px]">
                  <span class="text-xs text-slate-400 italic">No sending accounts detected.</span>
                </div>

                <!-- Delay Slider -->
                <div class="space-y-2">
                  <div class="flex justify-between text-xs">
                    <span class="font-bold text-slate-700">Pacing Delay</span>
                    <span id="sending-delay-value" class="font-extrabold text-indigo-600">15s Safe Delay</span>
                  </div>
                  <input id="sending-delay-slider" oninput="document.getElementById('sending-delay-value').innerText = this.value + 's Safe Delay'" type="range" min="5" max="60" value="15" class="w-full h-2 bg-slate-200 rounded-lg appearance-none cursor-pointer accent-indigo-600">
                  <div class="flex justify-between text-[10px] text-slate-400 font-semibold">
                    <span>5s (Aggressive)</span>
                    <span class="text-indigo-600">15s (Recommended)</span>
                    <span>60s (Ultra-Safe)</span>
                  </div>
                </div>

                <!-- Max per account -->
                <div class="flex items-center justify-between pt-1">
                  <div>
                    <div class="text-xs font-bold text-slate-700">Max Sends per Account</div>
                    <div class="text-[11px] text-slate-400">Rotates to next account after N sends</div>
                  </div>
                  <input id="sending-max-per-account" type="number" value="25" min="1" max="100" class="w-20 px-3 py-1.5 bg-slate-50 border border-slate-200 rounded-xl text-sm font-bold text-center">
                </div>

                <!-- Dispatch Button -->
                <div class="pt-2">
                  <button id="btn-start-sending" onclick="startManualSending()" class="w-full py-4 px-6 rounded-2xl bg-gradient-to-r from-indigo-600 via-indigo-700 to-blue-600 hover:from-indigo-700 hover:to-blue-700 text-white font-display font-extrabold text-base shadow-lg shadow-indigo-500/25 transition-all flex items-center justify-center gap-2.5 active:scale-98">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"></path></svg>
                    <span>Start Manual Sending Campaign</span>
                  </button>
                </div>
              </div>

            </div>
          </div>

          <!-- LIVE CAMPAIGN DISPATCH MONITOR -->
          <div id="campaign-monitor-card" class="bg-white p-7 rounded-3xl border border-slate-200/80 shadow-card space-y-5">
            <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pb-3 border-b border-slate-100">
              <div class="flex items-center gap-3">
                <span id="monitor-status-pill" class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold bg-slate-100 text-slate-600">
                  <span class="w-2 h-2 rounded-full bg-slate-400"></span>
                  <span>Idle</span>
                </span>
                <span id="monitor-campaign-title" class="text-sm font-bold text-slate-900">Campaign Dispatch Monitor</span>
              </div>

              <div>
                <button id="btn-stop-campaign" onclick="stopCampaign()" class="hidden px-4 py-1.5 rounded-xl bg-red-50 text-red-600 hover:bg-red-100 font-bold text-xs transition-all flex items-center gap-1.5">
                  <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
                  <span>Stop Campaign</span>
                </button>
              </div>
            </div>

            <!-- Progress Bar -->
            <div class="space-y-1.5">
              <div class="flex justify-between text-xs font-bold">
                <span id="monitor-progress-text" class="text-slate-600">0 of 0 Completed</span>
                <span id="monitor-progress-percent" class="text-indigo-600 font-extrabold">0%</span>
              </div>
              <div class="w-full h-3.5 bg-slate-100 rounded-full overflow-hidden">
                <div id="monitor-progress-bar" class="h-full bg-gradient-to-r from-indigo-500 to-blue-600 rounded-full transition-all duration-300" style="width: 0%"></div>
              </div>
            </div>

            <!-- 4 Metrics Chips -->
            <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 text-center">
              <div class="p-3.5 bg-slate-50 rounded-2xl border border-slate-100">
                <div class="text-[10px] font-bold text-slate-400 uppercase">Total Targets</div>
                <div id="monitor-stat-total" class="text-2xl font-extrabold text-slate-900 mt-0.5">0</div>
              </div>
              <div class="p-3.5 bg-emerald-50 rounded-2xl border border-emerald-100">
                <div class="text-[10px] font-bold text-emerald-600 uppercase">Successfully Sent</div>
                <div id="monitor-stat-sent" class="text-2xl font-extrabold text-emerald-700 mt-0.5">0</div>
              </div>
              <div class="p-3.5 bg-red-50 rounded-2xl border border-red-100">
                <div class="text-[10px] font-bold text-red-600 uppercase">Failed / Skipped</div>
                <div id="monitor-stat-failed" class="text-2xl font-extrabold text-red-700 mt-0.5">0</div>
              </div>
              <div class="p-3.5 bg-slate-50 rounded-2xl border border-slate-100">
                <div class="text-[10px] font-bold text-slate-400 uppercase">Remaining</div>
                <div id="monitor-stat-remaining" class="text-2xl font-extrabold text-slate-700 mt-0.5">0</div>
              </div>
            </div>

            <!-- Current Active Rotation Info -->
            <div class="flex flex-wrap items-center justify-between text-xs px-5 py-3 rounded-2xl bg-slate-50 text-slate-600 border border-slate-200/80">
              <div class="flex items-center gap-2">
                <span class="font-bold text-slate-800">Active Account:</span>
                <span id="monitor-active-account" class="font-mono text-indigo-600 font-bold">None</span>
              </div>
              <div class="flex items-center gap-2">
                <span class="font-bold text-slate-800">Target Lead:</span>
                <span id="monitor-active-lead" class="font-mono text-purple-600 font-bold">None</span>
              </div>
            </div>

            <!-- Live Dispatch Console Output -->
            <div class="space-y-1.5">
              <div class="text-xs font-bold text-slate-500 uppercase tracking-wider">Live Dispatch Terminal</div>
              <div id="monitor-log-terminal" class="bg-slate-950 text-slate-200 p-5 rounded-2xl font-mono text-xs h-52 overflow-y-auto space-y-1.5 border border-slate-800 shadow-inner">
                <div class="text-slate-500 italic">[Waiting for dispatch campaign initiation...]</div>
              </div>
            </div>
          </div>

        </section>


        <!-- ======================================================== -->
        <!-- SECTION 3: LOGIN ACCOUNTS MANAGEMENT                    -->
        <!-- ======================================================== -->
        <section id="tab-accounts" class="tab-content hidden space-y-7">
          
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-2">
            <div>
              <h2 class="text-2xl font-display font-extrabold text-slate-900 tracking-tight">Login Accounts Management</h2>
              <p class="text-sm text-slate-500 mt-1">Connect and monitor Telegram MTProto accounts used for distributing promotional messages via account rotation.</p>
            </div>

            <div>
              <button onclick="openConnectAccountModal()" class="flex items-center gap-2 px-5 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white text-xs font-bold shadow-sm shadow-indigo-500/25 transition-all active:scale-95">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"></path></svg>
                <span>Connect Telegram Account</span>
              </button>
            </div>
          </div>

          <!-- Accounts Cards Grid -->
          <div id="accounts-cards-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            <div class="col-span-full py-16 text-center text-slate-400 bg-white rounded-3xl border border-slate-200/80">
              Loading registered accounts...
            </div>
          </div>

        </section>


        <!-- ======================================================== -->
        <!-- SECTION 4: AUTOMATIC SENDING REPORT                     -->
        <!-- ======================================================== -->
        <section id="tab-reports" class="tab-content hidden space-y-7">
          
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-2">
            <div>
              <h2 class="text-2xl font-display font-extrabold text-slate-900 tracking-tight">Automatic Sending Reports</h2>
              <p class="text-sm text-slate-500 mt-1">Comprehensive audit logs and performance statistics across all sending accounts and dispatched campaigns.</p>
            </div>

            <div>
              <button onclick="exportReportsCsv()" class="flex items-center gap-2 px-4 py-2.5 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-bold transition-all active:scale-95">
                <svg class="w-4 h-4 text-slate-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"></path></svg>
                <span>Export Audit CSV</span>
              </button>
            </div>
          </div>

          <!-- 4 Summary KPI Cards -->
          <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-5">
            
            <div class="bg-white p-6 rounded-3xl border border-slate-200/80 shadow-card">
              <div class="text-xs font-bold text-slate-400 uppercase tracking-wider">Total Messages Sent</div>
              <div id="report-stat-total" class="text-3xl font-display font-extrabold text-slate-900 mt-1.5">0</div>
              <div class="text-[11px] text-slate-400 mt-1">All-time promotional deliveries</div>
            </div>

            <div class="bg-white p-6 rounded-3xl border border-slate-200/80 shadow-card">
              <div class="text-xs font-bold text-slate-400 uppercase tracking-wider">Sent Today</div>
              <div id="report-stat-today" class="text-3xl font-display font-extrabold text-indigo-600 mt-1.5">0</div>
              <div class="text-[11px] text-slate-400 mt-1">Messages dispatched today</div>
            </div>

            <div class="bg-white p-6 rounded-3xl border border-slate-200/80 shadow-card">
              <div class="text-xs font-bold text-slate-400 uppercase tracking-wider">Success Rate</div>
              <div id="report-stat-success-rate" class="text-3xl font-display font-extrabold text-emerald-600 mt-1.5">100%</div>
              <div class="text-[11px] text-slate-400 mt-1">Successful deliveries ratio</div>
            </div>

            <div class="bg-white p-6 rounded-3xl border border-slate-200/80 shadow-card">
              <div class="text-xs font-bold text-slate-400 uppercase tracking-wider">Active Senders</div>
              <div id="report-stat-accounts" class="text-3xl font-display font-extrabold text-purple-600 mt-1.5">0</div>
              <div class="text-[11px] text-slate-400 mt-1">Telegram accounts rotating</div>
            </div>

          </div>

          <!-- Analytics Breakdown: By Account & By Lead Type -->
          <div class="grid grid-cols-1 lg:grid-cols-12 gap-7">
            
            <!-- Left: Breakdown by Sending Account -->
            <div class="lg:col-span-7 bg-white p-7 rounded-3xl border border-slate-200/80 shadow-card space-y-4">
              <h3 class="font-display font-bold text-slate-900 text-sm">📊 Deliveries by Sender Account</h3>
              <div class="overflow-x-auto">
                <table class="w-full text-left text-xs">
                  <thead class="bg-slate-50 text-slate-500 uppercase font-bold">
                    <tr>
                      <th class="py-3 px-3.5">Sender Phone</th>
                      <th class="py-3 px-3.5">Account Role</th>
                      <th class="py-3 px-3.5 text-right">Messages Sent</th>
                    </tr>
                  </thead>
                  <tbody id="reports-by-account-tbody" class="divide-y divide-slate-100 font-medium">
                    <tr><td colspan="3" class="py-5 text-center text-slate-400">No dispatch data recorded yet.</td></tr>
                  </tbody>
                </table>
              </div>
            </div>

            <!-- Right: Breakdown by Lead Type -->
            <div class="lg:col-span-5 bg-white p-7 rounded-3xl border border-slate-200/80 shadow-card space-y-4">
              <h3 class="font-display font-bold text-slate-900 text-sm">🎯 Target Segmentation Breakdown</h3>
              <div id="reports-type-breakdown-box" class="space-y-5 py-2">
                <div>
                  <div class="flex justify-between text-xs font-bold mb-1.5">
                    <span class="text-slate-700">🤖 Userbots</span>
                    <span id="breakdown-userbots-count" class="text-indigo-600 font-extrabold">0 (0%)</span>
                  </div>
                  <div class="w-full h-3 bg-slate-100 rounded-full overflow-hidden">
                    <div id="breakdown-userbots-bar" class="h-full bg-indigo-500 rounded-full transition-all duration-500" style="width: 0%"></div>
                  </div>
                </div>

                <div>
                  <div class="flex justify-between text-xs font-bold mb-1.5">
                    <span class="text-slate-700">👤 Usernames</span>
                    <span id="breakdown-usernames-count" class="text-purple-600 font-extrabold">0 (0%)</span>
                  </div>
                  <div class="w-full h-3 bg-slate-100 rounded-full overflow-hidden">
                    <div id="breakdown-usernames-bar" class="h-full bg-purple-500 rounded-full transition-all duration-500" style="width: 0%"></div>
                  </div>
                </div>
              </div>
            </div>

          </div>

          <!-- Comprehensive Delivery Audit Log Table -->
          <div class="bg-white rounded-3xl border border-slate-200/80 shadow-card overflow-hidden">
            <div class="p-5 border-b border-slate-100 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <div class="font-display font-extrabold text-slate-900 text-base">Delivery Audit Logs</div>
              <div>
                <select id="reports-status-filter" onchange="loadReports()" class="text-xs py-2 px-3 bg-slate-50 border border-slate-200 rounded-xl text-slate-700 font-semibold">
                  <option value="">All Statuses</option>
                  <option value="sent">✅ Sent Only</option>
                  <option value="failed">❌ Failed Only</option>
                </select>
              </div>
            </div>

            <div class="overflow-x-auto">
              <table class="w-full text-left text-sm">
                <thead class="bg-slate-50/80 border-b border-slate-200/80 text-slate-500 uppercase text-[11px] font-bold tracking-wider">
                  <tr>
                    <th class="py-4 px-6">Timestamp</th>
                    <th class="py-4 px-4">Recipient Handle</th>
                    <th class="py-4 px-4">Type</th>
                    <th class="py-4 px-4">Sender Phone</th>
                    <th class="py-4 px-4">Post Title</th>
                    <th class="py-4 px-4">Status</th>
                    <th class="py-4 px-6">Details / Note</th>
                  </tr>
                </thead>
                <tbody id="reports-table-body" class="divide-y divide-slate-100 font-medium">
                  <tr>
                    <td colspan="7" class="py-16 text-center text-slate-400">Loading delivery history...</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

        </section>

      </div>
    </main>
  </div>

  <!-- MODAL: ADD LEAD -->
  <div id="modal-add-lead" class="fixed inset-0 z-50 bg-slate-900/60 backdrop-blur-sm hidden flex items-center justify-center p-4">
    <div class="bg-white w-full max-w-md rounded-3xl border border-slate-200 shadow-2xl p-7 space-y-5">
      <div class="flex items-center justify-between pb-3 border-b border-slate-100">
        <h3 class="font-display font-extrabold text-lg text-slate-900">Add New Lead</h3>
        <button onclick="closeAddLeadModal()" class="text-slate-400 hover:text-slate-600 text-2xl font-bold">&times;</button>
      </div>

      <div class="space-y-4">
        <div>
          <label class="block text-xs font-bold text-slate-600 uppercase tracking-wider mb-1.5">Telegram Handle / Username</label>
          <input id="modal-lead-username" type="text" placeholder="e.g. @loot_deal_bot or owner_name" class="w-full px-4 py-2.5 bg-slate-50 border border-slate-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
        </div>

        <div>
          <label class="block text-xs font-bold text-slate-600 uppercase tracking-wider mb-1.5">Classification</label>
          <select id="modal-lead-type" class="w-full px-4 py-2.5 bg-slate-50 border border-slate-200 rounded-xl text-sm font-semibold focus:outline-none focus:ring-2 focus:ring-indigo-500">
            <option value="bot">🤖 Userbot (Deal Bot)</option>
            <option value="user">👤 Username (Human Channel Owner)</option>
          </select>
        </div>

        <div>
          <label class="block text-xs font-bold text-slate-600 uppercase tracking-wider mb-1.5">Discovered Source / Note (Optional)</label>
          <input id="modal-lead-source" type="text" placeholder="e.g. Amazon Deals Telegram Channel" class="w-full px-4 py-2.5 bg-slate-50 border border-slate-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
        </div>
      </div>

      <div class="flex items-center justify-end gap-3 pt-3 border-t border-slate-100">
        <button onclick="closeAddLeadModal()" class="px-5 py-2.5 rounded-xl border border-slate-200 text-slate-600 hover:bg-slate-50 font-bold text-xs">Cancel</button>
        <button onclick="submitAddLead()" class="px-5 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white font-bold text-xs shadow-sm shadow-indigo-500/25">Save Lead</button>
      </div>
    </div>
  </div>

  <!-- MODAL: CONNECT TELEGRAM ACCOUNT -->
  <div id="modal-connect-account" class="fixed inset-0 z-50 bg-slate-900/60 backdrop-blur-sm hidden flex items-center justify-center p-4">
    <div class="bg-white w-full max-w-md rounded-3xl border border-slate-200 shadow-2xl p-7 space-y-5">
      <div class="flex items-center justify-between pb-3 border-b border-slate-100">
        <h3 class="font-display font-extrabold text-lg text-slate-900">Connect Telegram Account</h3>
        <button onclick="closeConnectAccountModal()" class="text-slate-400 hover:text-slate-600 text-2xl font-bold">&times;</button>
      </div>

      <!-- Step 1: Phone -->
      <div id="account-step-1" class="space-y-4">
        <p class="text-xs text-slate-500 leading-relaxed font-medium">Enter your international phone number associated with your Telegram account (e.g. +919876543210).</p>
        <div>
          <label class="block text-xs font-bold text-slate-600 uppercase tracking-wider mb-1.5">Phone Number</label>
          <input id="modal-account-phone" type="text" placeholder="+919876543210" class="w-full px-4 py-3 bg-slate-50 border border-slate-200 rounded-xl text-sm font-mono font-bold focus:outline-none focus:ring-2 focus:ring-indigo-500">
        </div>
        <div class="pt-2">
          <button id="btn-modal-send-code" onclick="submitStartAccountLogin()" class="w-full py-3 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white font-bold text-sm shadow-sm shadow-indigo-500/25 flex items-center justify-center gap-2">
            <span>Send Telegram OTP Code</span>
          </button>
        </div>
      </div>

      <!-- Step 2: OTP & 2FA -->
      <div id="account-step-2" class="space-y-4 hidden">
        <p class="text-xs text-slate-500 leading-relaxed font-medium">Enter the 5-digit verification code sent to your Telegram app for <span id="modal-display-phone" class="font-bold text-slate-900"></span>.</p>
        <div>
          <label class="block text-xs font-bold text-slate-600 uppercase tracking-wider mb-1.5">Verification Code</label>
          <input id="modal-account-code" type="text" placeholder="12345" class="w-full px-4 py-3 bg-slate-50 border border-slate-200 rounded-xl text-base font-mono font-extrabold tracking-widest text-center focus:outline-none focus:ring-2 focus:ring-indigo-500">
        </div>
        <div>
          <label class="block text-xs font-bold text-slate-600 uppercase tracking-wider mb-1.5">2FA Cloud Password (Optional)</label>
          <input id="modal-account-password" type="password" placeholder="Leave blank if not set" class="w-full px-4 py-2.5 bg-slate-50 border border-slate-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
        </div>
        <div class="pt-2">
          <button id="btn-modal-complete-login" onclick="submitCompleteAccountLogin()" class="w-full py-3 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white font-bold text-sm shadow-sm shadow-emerald-500/25 flex items-center justify-center gap-2">
            <span>Authorize & Connect Account</span>
          </button>
        </div>
      </div>
    </div>
  </div>


  <!-- CLIENT JAVASCRIPT -->
  <script>
    // State
    let currentTab = 'tab-leads';
    let currentLeadType = 'bot'; // 'bot' (Userbots) or 'user' (Usernames)
    let leadsData = [];
    let accountsData = [];
    let savedTemplates = [];
    let campaignPollInterval = null;
    let pendingLoginToken = null;
    let searchDebounceTimer = null;

    // Toast Notification Utility
    function showToast(message, type = 'info') {
      const container = document.getElementById('toast-container');
      const toast = document.createElement('div');
      const colors = {
        success: 'bg-emerald-600 text-white shadow-emerald-600/30',
        error: 'bg-red-600 text-white shadow-red-600/30',
        info: 'bg-slate-900 text-white shadow-slate-900/30',
        warning: 'bg-amber-600 text-white shadow-amber-600/30'
      };
      toast.className = `px-5 py-3.5 rounded-2xl shadow-xl text-xs font-bold pointer-events-auto transform transition-all duration-300 translate-y-2 opacity-0 flex items-center gap-2.5 ${colors[type] || colors.info}`;
      toast.innerHTML = `<span>${message}</span>`;
      container.appendChild(toast);
      setTimeout(() => { toast.classList.remove('translate-y-2', 'opacity-0'); }, 10);
      setTimeout(() => {
        toast.classList.add('opacity-0', 'translate-y-2');
        setTimeout(() => toast.remove(), 300);
      }, 4000);
    }

    // Robust Source Formatter (Never crashes on object, array, or string)
    function formatLeadSource(src) {
      if (!src) return 'Direct';
      if (typeof src === 'string') return src;
      if (Array.isArray(src)) {
        const names = src.map(s => {
          if (typeof s === 'object' && s) {
            return s.channel_name || s.source_name || s.name || '';
          }
          return String(s || '');
        }).filter(Boolean);
        return names.join(', ') || 'Direct';
      }
      if (typeof src === 'object') {
        return src.channel_name || src.source_name || src.name || 'Direct';
      }
      return String(src);
    }

    // Switch Top-level Tabs
    function switchTab(tabId) {
      currentTab = tabId;
      document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
      const activeEl = document.getElementById(tabId);
      if (activeEl) activeEl.classList.remove('hidden');

      document.querySelectorAll('.nav-item').forEach(el => {
        el.classList.remove('active');
        el.classList.add('text-slate-600');
      });
      const btn = document.getElementById('nav-btn-' + tabId.replace('tab-', ''));
      if (btn) {
        btn.classList.add('active');
        btn.classList.remove('text-slate-600');
      }

      const titles = {
        'tab-leads': 'Leads Directory',
        'tab-sending': 'Manual Sending',
        'tab-accounts': 'Login Accounts',
        'tab-reports': 'Sending Reports'
      };
      document.getElementById('header-breadcrumb').innerHTML = `ProAffiliaters <span class="text-slate-300">/</span> <span class="text-indigo-600">${titles[tabId] || 'Dashboard'}</span>`;

      if (tabId === 'tab-leads') loadLeads();
      if (tabId === 'tab-sending') { loadSendingData(); checkCampaignStatus(); }
      if (tabId === 'tab-accounts') loadAccounts();
      if (tabId === 'tab-reports') loadReports();
    }

    function refreshCurrentTab() {
      loadStats();
      if (currentTab === 'tab-leads') loadLeads();
      if (currentTab === 'tab-sending') { loadSendingData(); checkCampaignStatus(); }
      if (currentTab === 'tab-accounts') loadAccounts();
      if (currentTab === 'tab-reports') loadReports();
      showToast('Dashboard refreshed.', 'info');
    }

    // --- LEADS SECTION ---
    function switchLeadSubTab(type) {
      currentLeadType = type;
      const botBtn = document.getElementById('subtab-btn-userbots');
      const userBtn = document.getElementById('subtab-btn-usernames');

      if (type === 'bot') {
        botBtn.className = 'px-5 py-2.5 text-xs font-bold rounded-xl bg-indigo-600 text-white shadow-sm flex items-center gap-2.5 transition-all';
        botBtn.querySelector('span:last-child').className = 'px-2 py-0.5 rounded-full bg-white/25 text-white text-[11px] font-extrabold';
        userBtn.className = 'px-5 py-2.5 text-xs font-semibold rounded-xl text-slate-600 hover:text-slate-900 hover:bg-slate-200/60 flex items-center gap-2.5 transition-all';
        userBtn.querySelector('span:last-child').className = 'px-2 py-0.5 rounded-full bg-slate-300 text-slate-700 text-[11px] font-extrabold';
      } else {
        userBtn.className = 'px-5 py-2.5 text-xs font-bold rounded-xl bg-indigo-600 text-white shadow-sm flex items-center gap-2.5 transition-all';
        userBtn.querySelector('span:last-child').className = 'px-2 py-0.5 rounded-full bg-white/25 text-white text-[11px] font-extrabold';
        botBtn.className = 'px-5 py-2.5 text-xs font-semibold rounded-xl text-slate-600 hover:text-slate-900 hover:bg-slate-200/60 flex items-center gap-2.5 transition-all';
        botBtn.querySelector('span:last-child').className = 'px-2 py-0.5 rounded-full bg-slate-300 text-slate-700 text-[11px] font-extrabold';
      }
      loadLeads();
    }

    function debounceLeadsSearch() {
      clearTimeout(searchDebounceTimer);
      searchDebounceTimer = setTimeout(loadLeads, 300);
    }

    async function loadLeads() {
      const search = document.getElementById('leads-search-input').value.trim();
      const status = document.getElementById('leads-status-select').value;
      const tbody = document.getElementById('leads-table-body');

      try {
        const res = await fetch(`/api/leads?lead_type=${currentLeadType}&status=${status}&search=${encodeURIComponent(search)}&limit=300`);
        const data = await res.json();
        leadsData = data.leads || [];

        if (leadsData.length === 0) {
          tbody.innerHTML = `<tr><td colspan="6" class="py-16 text-center text-slate-400 font-medium">No ${currentLeadType === 'bot' ? 'userbots' : 'usernames'} found matching your filters.</td></tr>`;
          document.getElementById('leads-pagination-info').innerText = 'Showing 0 leads';
          return;
        }

        tbody.innerHTML = leadsData.map(l => {
          let rawUser = String(l.username || l.username_clean || 'unknown').trim();
          if (!rawUser.startsWith('@')) rawUser = '@' + rawUser;
          const clean = rawUser.replace(/^@/, '');

          const isBot = l.lead_type === 'bot' || clean.toLowerCase().endsWith('bot');
          const typeBadge = isBot
            ? `<span class="px-2.5 py-1 rounded-full text-xs font-bold bg-indigo-50 text-indigo-700 border border-indigo-200/60">🤖 Userbot</span>`
            : `<span class="px-2.5 py-1 rounded-full text-xs font-bold bg-purple-50 text-purple-700 border border-purple-200/60">👤 Username</span>`;

          const sources = formatLeadSource(l.extracted_from);
          const firstSeen = l.first_seen_at ? String(l.first_seen_at).substring(0, 10) : 'Recent';

          const statusColors = {
            pending: 'bg-amber-50 text-amber-700 border-amber-200/80',
            contacted: 'bg-blue-50 text-blue-700 border-blue-200/80',
            replied: 'bg-indigo-50 text-indigo-700 border-indigo-200/80',
            converted: 'bg-emerald-50 text-emerald-700 border-emerald-200/80',
            ignored: 'bg-slate-100 text-slate-600 border-slate-200'
          };
          const currentStatus = String(l.status || 'pending').toLowerCase();

          return `
            <tr class="hover:bg-slate-50/80 transition-all">
              <td class="py-3.5 px-6 font-bold text-slate-900">
                <a href="https://t.me/${clean}" target="_blank" class="hover:text-indigo-600 hover:underline flex items-center gap-2">
                  <span class="w-7 h-7 rounded-full bg-blue-50 text-blue-600 flex items-center justify-center shrink-0">
                    <svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 6.8c-.15 1.58-.8 5.42-1.13 7.19-.14.75-.42 1-.68 1.03-.58.05-1.02-.38-1.58-.75-.88-.58-1.38-.94-2.23-1.5-.99-.65-.35-1.01.22-1.59.15-.15 2.71-2.48 2.76-2.69a.2.2 0 00-.05-.18c-.06-.05-.14-.03-.21-.02-.09.02-1.49.95-4.22 2.79-.4.27-.76.41-1.08.4-.36-.01-1.04-.2-1.55-.37-.63-.2-1.12-.31-1.08-.66.02-.18.27-.36.74-.55 2.92-1.27 4.86-2.11 5.83-2.51 2.78-1.16 3.35-1.36 3.73-1.36.08 0 .27.02.39.12.1.08.13.19.14.27-.01.06.01.24 0 .37z"/></svg>
                  </span>
                  <span>${rawUser}</span>
                </a>
              </td>
              <td class="py-3.5 px-4">${typeBadge}</td>
              <td class="py-3.5 px-4 text-xs font-semibold text-slate-600 truncate max-w-[200px]" title="${sources}">${sources}</td>
              <td class="py-3.5 px-4">
                <select onchange="updateLeadStatus('${clean}', this.value)" class="text-xs py-1 px-3 rounded-lg border font-bold ${statusColors[currentStatus] || 'bg-slate-100 text-slate-700'} focus:outline-none cursor-pointer">
                  <option value="pending" ${currentStatus === 'pending' ? 'selected' : ''}>⏳ Pending</option>
                  <option value="contacted" ${currentStatus === 'contacted' ? 'selected' : ''}>✉️ Contacted</option>
                  <option value="replied" ${currentStatus === 'replied' ? 'selected' : ''}>💬 Replied</option>
                  <option value="converted" ${currentStatus === 'converted' ? 'selected' : ''}>🏆 Converted</option>
                  <option value="ignored" ${currentStatus === 'ignored' ? 'selected' : ''}>🚫 Ignored</option>
                </select>
              </td>
              <td class="py-3.5 px-4 text-xs font-semibold text-slate-500">${firstSeen}</td>
              <td class="py-3.5 px-6 text-right">
                <button onclick="deleteLead('${clean}')" title="Delete Lead" class="p-2 text-slate-400 hover:text-red-600 rounded-xl hover:bg-red-50 transition-all active:scale-95">
                  <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path></svg>
                </button>
              </td>
            </tr>
          `;
        }).join('');

        document.getElementById('leads-pagination-info').innerText = `Showing ${leadsData.length} leads`;
      } catch (e) {
        tbody.innerHTML = `<tr><td colspan="6" class="py-12 text-center text-red-500 font-bold">Failed to load leads: ${e.message}</td></tr>`;
      }
    }

    async function updateLeadStatus(username, newStatus) {
      try {
        const res = await fetch(`/api/leads/${username}/status`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: newStatus })
        });
        if (res.ok) {
          showToast(`Updated @${username} status to ${newStatus}`, 'success');
          loadStats();
        } else {
          showToast('Failed to update status', 'error');
        }
      } catch (e) {
        showToast('Error updating status: ' + e.message, 'error');
      }
    }

    async function deleteLead(username) {
      if (!confirm(`Are you sure you want to delete lead @${username}?`)) return;
      try {
        const res = await fetch(`/api/leads/${username}`, { method: 'DELETE' });
        if (res.ok) {
          showToast(`Deleted lead @${username}`, 'info');
          loadLeads();
          loadStats();
        }
      } catch (e) {
        showToast('Error deleting lead', 'error');
      }
    }

    function exportLeadsCsv() {
      const status = document.getElementById('leads-status-select').value;
      const search = document.getElementById('leads-search-input').value.trim();
      window.location.href = `/api/leads/export-csv?lead_type=${currentLeadType}&status=${status}&search=${encodeURIComponent(search)}`;
    }

    // Modal Add Lead
    function openAddLeadModal() {
      document.getElementById('modal-lead-username').value = '';
      document.getElementById('modal-lead-source').value = '';
      document.getElementById('modal-add-lead').classList.remove('hidden');
    }
    function closeAddLeadModal() {
      document.getElementById('modal-add-lead').classList.add('hidden');
    }
    async function submitAddLead() {
      const username = document.getElementById('modal-lead-username').value.trim();
      const lead_type = document.getElementById('modal-lead-type').value;
      const source = document.getElementById('modal-lead-source').value.trim();

      if (!username) {
        alert('Please enter a username or handle.');
        return;
      }

      try {
        const res = await fetch('/api/leads', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username, lead_type, source: source || 'Manual Entry' })
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`Lead @${data.username} added successfully!`, 'success');
          closeAddLeadModal();
          loadLeads();
          loadStats();
        } else {
          alert(data.error || 'Failed to add lead');
        }
      } catch (e) {
        alert('Error: ' + e.message);
      }
    }


    // --- MANUAL SENDING SECTION ---
    function updateImagePreview() {
      const url = document.getElementById('post-image-url').value.trim();
      const container = document.getElementById('preview-bubble-image-box');
      const img = document.getElementById('preview-bubble-img');
      if (url && (url.startsWith('http://') || url.startsWith('https://'))) {
        img.src = url;
        container.classList.remove('hidden');
      } else {
        container.classList.add('hidden');
      }
    }

    function updateTelegramBubblePreview() {
      const text = document.getElementById('post-message-text').value.trim();
      const btnText = document.getElementById('post-button-text').value.trim();
      const bubbleText = document.getElementById('preview-bubble-text');
      const btnBox = document.getElementById('preview-bubble-btn-box');
      const btnLabel = document.getElementById('preview-bubble-btn-label');

      bubbleText.innerText = text || 'Message preview will appear here...';
      if (btnText) {
        btnLabel.innerText = btnText;
        btnBox.classList.remove('hidden');
      } else {
        btnBox.classList.add('hidden');
      }
    }

    function updateAudienceSelectionUI() {
      const selected = document.querySelector('input[name="sending-target"]:checked').value;
      const customBox = document.getElementById('custom-handles-box');
      if (selected === 'custom') {
        customBox.classList.remove('hidden');
      } else {
        customBox.classList.add('hidden');
      }
    }

    async function loadSendingData() {
      // 1. Load saved templates
      try {
        const res = await fetch('/api/posts');
        const data = await res.json();
        savedTemplates = data.posts || [];
        const select = document.getElementById('composer-template-select');
        select.innerHTML = '<option value="">-- Load Saved Template --</option>' +
          savedTemplates.map(t => `<option value="${t.id}">${t.title}</option>`).join('');
      } catch (e) {
        console.error('Error loading templates', e);
      }

      // 2. Load accounts for rotation badge
      try {
        const res = await fetch('/api/accounts');
        const data = await res.json();
        accountsData = data.accounts || [];
        const activeAccounts = accountsData.filter(a => a.is_authorized !== false);

        document.getElementById('rotation-active-count-badge').innerText = `${activeAccounts.length} Accounts Ready`;
        const listEl = document.getElementById('rotation-accounts-list');

        if (activeAccounts.length === 0) {
          listEl.innerHTML = `<div class="p-3 w-full text-xs font-semibold text-amber-800 bg-amber-50 rounded-xl border border-amber-200 flex items-center justify-between">
            <span>⚠️ No active Telegram accounts connected!</span>
            <button onclick="switchTab('tab-accounts')" class="text-xs font-bold underline">Connect Accounts &rarr;</button>
          </div>`;
        } else {
          listEl.innerHTML = activeAccounts.map(a => `
            <span class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-bold bg-white border border-slate-200/90 text-slate-800 shadow-2xs">
              <span class="w-2 h-2 rounded-full bg-emerald-500"></span>
              <span class="font-mono">${a.phone}</span>
            </span>
          `).join('');
        }
      } catch (e) {
        console.error('Error loading accounts for sending', e);
      }
    }

    function onTemplateSelected() {
      const select = document.getElementById('composer-template-select');
      const id = select.value;
      if (!id) return;
      const t = savedTemplates.find(x => x.id === id);
      if (!t) return;

      document.getElementById('post-title').value = t.title || '';
      document.getElementById('post-message-text').value = t.message_text || '';
      document.getElementById('post-image-url').value = t.image_url || '';
      document.getElementById('post-button-text').value = t.button_text || '';
      document.getElementById('post-button-url').value = t.button_url || '';
      updateImagePreview();
      updateTelegramBubblePreview();
      showToast(`Loaded template: "${t.title}"`, 'info');
    }

    async function savePostTemplate() {
      const title = document.getElementById('post-title').value.trim();
      const message_text = document.getElementById('post-message-text').value.trim();
      const image_url = document.getElementById('post-image-url').value.trim();
      const button_text = document.getElementById('post-button-text').value.trim();
      const button_url = document.getElementById('post-button-url').value.trim();

      if (!title || !message_text) {
        alert('Please enter a Title and Message Text to save the template.');
        return;
      }

      try {
        const res = await fetch('/api/posts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title, message_text, image_url, button_text, button_url })
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`Saved template "${title}" successfully!`, 'success');
          loadSendingData();
        } else {
          alert(data.error || 'Failed to save template');
        }
      } catch (e) {
        alert('Error: ' + e.message);
      }
    }

    async function deleteSelectedTemplate() {
      const select = document.getElementById('composer-template-select');
      const id = select.value;
      if (!id) {
        alert('Please select a saved template from the dropdown first.');
        return;
      }
      if (!confirm('Are you sure you want to delete this template?')) return;
      try {
        const res = await fetch(`/api/posts/${id}`, { method: 'DELETE' });
        if (res.ok) {
          showToast('Template deleted.', 'info');
          loadSendingData();
        }
      } catch (e) {
        alert('Error deleting template');
      }
    }

    async function startManualSending() {
      const title = document.getElementById('post-title').value.trim() || 'Manual Campaign';
      const message_text = document.getElementById('post-message-text').value.trim();
      const image_url = document.getElementById('post-image-url').value.trim();
      const button_text = document.getElementById('post-button-text').value.trim();
      const button_url = document.getElementById('post-button-url').value.trim();
      const target_type = document.querySelector('input[name="sending-target"]:checked').value;
      const delay_seconds = parseInt(document.getElementById('sending-delay-slider').value, 10) || 15;
      const max_per_account = parseInt(document.getElementById('sending-max-per-account').value, 10) || 25;

      if (!message_text) {
        alert('Please write promotional message text before starting the campaign.');
        return;
      }

      let custom_leads = [];
      if (target_type === 'custom') {
        const raw = document.getElementById('custom-handles-text').value;
        const nl = String.fromCharCode(10);
        const items = raw.split(',').join(nl).split(nl);
        custom_leads = items.map(s => {
          const t = s.trim();
          return t.startsWith('@') ? t.substring(1) : t;
        }).filter(Boolean);
        if (custom_leads.length === 0) {
          alert('Please enter at least one target handle in the custom list.');
          return;
        }
      }

      if (!confirm(`Launch promotional sending campaign across ${target_type.replace('_', ' ')} with account rotation?`)) return;

      try {
        const res = await fetch('/api/sending/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            post_title: title,
            message_text,
            image_url,
            button_text,
            button_url,
            target_type,
            custom_leads,
            delay_seconds,
            max_per_account
          })
        });
        const data = await res.json();
        if (res.ok) {
          showToast('Promotional campaign launched!', 'success');
          startCampaignPolling();
        } else {
          alert(data.error || 'Failed to start campaign');
        }
      } catch (e) {
        alert('Error launching campaign: ' + e.message);
      }
    }

    function startCampaignPolling() {
      if (campaignPollInterval) clearInterval(campaignPollInterval);
      checkCampaignStatus();
      campaignPollInterval = setInterval(checkCampaignStatus, 2000);
    }

    async function checkCampaignStatus() {
      try {
        const res = await fetch('/api/sending/status');
        const st = await res.json();

        // Update progress bar
        const total = st.total_targets || 0;
        const sent = st.sent_count || 0;
        const failed = st.failed_count || 0;
        const completed = sent + failed;
        const percent = total > 0 ? Math.round((completed / total) * 100) : (st.status === 'completed' ? 100 : 0);

        document.getElementById('monitor-progress-bar').style.width = percent + '%';
        document.getElementById('monitor-progress-percent').innerText = percent + '%';
        document.getElementById('monitor-progress-text').innerText = `${completed} of ${total} Completed`;

        document.getElementById('monitor-stat-total').innerText = total;
        document.getElementById('monitor-stat-sent').innerText = sent;
        document.getElementById('monitor-stat-failed').innerText = failed;
        document.getElementById('monitor-stat-remaining').innerText = Math.max(0, total - completed);

        document.getElementById('monitor-active-account').innerText = st.current_account || 'None';
        document.getElementById('monitor-active-lead').innerText = st.current_lead || 'None';
        document.getElementById('monitor-campaign-title').innerText = st.post_title ? `Campaign: ${st.post_title}` : 'Campaign Dispatch Monitor';

        // Status pill
        const pill = document.getElementById('monitor-status-pill');
        const stopBtn = document.getElementById('btn-stop-campaign');

        if (st.is_running) {
          pill.className = 'inline-flex items-center gap-1.5 px-3.5 py-1 rounded-full text-xs font-bold bg-indigo-100 text-indigo-700 animate-pulse';
          pill.innerHTML = '<span class="w-2 h-2 rounded-full bg-indigo-600"></span><span>Running</span>';
          stopBtn.classList.remove('hidden');
        } else if (st.status === 'completed') {
          pill.className = 'inline-flex items-center gap-1.5 px-3.5 py-1 rounded-full text-xs font-bold bg-emerald-100 text-emerald-700';
          pill.innerHTML = '<span class="w-2 h-2 rounded-full bg-emerald-600"></span><span>Completed</span>';
          stopBtn.classList.add('hidden');
        } else if (st.status === 'stopped') {
          pill.className = 'inline-flex items-center gap-1.5 px-3.5 py-1 rounded-full text-xs font-bold bg-amber-100 text-amber-700';
          pill.innerHTML = '<span class="w-2 h-2 rounded-full bg-amber-600"></span><span>Stopped</span>';
          stopBtn.classList.add('hidden');
        } else {
          pill.className = 'inline-flex items-center gap-1.5 px-3.5 py-1 rounded-full text-xs font-bold bg-slate-100 text-slate-600';
          pill.innerHTML = '<span class="w-2 h-2 rounded-full bg-slate-400"></span><span>Idle</span>';
          stopBtn.classList.add('hidden');
        }

        // Live Log Terminal
        const logs = st.recent_logs || [];
        const terminal = document.getElementById('monitor-log-terminal');
        if (logs.length > 0) {
          terminal.innerHTML = logs.map(l => {
            let color = 'text-slate-300';
            if (l.includes('✅') || l.includes('Success')) color = 'text-emerald-400';
            if (l.includes('❌') || l.includes('Error') || l.includes('Flood')) color = 'text-red-400';
            if (l.includes('🔄') || l.includes('Rotating')) color = 'text-indigo-400';
            return `<div class="${color}">${l}</div>`;
          }).join('');
          terminal.scrollTop = terminal.scrollHeight;
        }

        if (!st.is_running && campaignPollInterval) {
          clearInterval(campaignPollInterval);
          campaignPollInterval = null;
        }
      } catch (e) {
        console.error('Error checking campaign status', e);
      }
    }

    async function stopCampaign() {
      if (!confirm('Are you sure you want to stop the promotional sending campaign?')) return;
      try {
        const res = await fetch('/api/sending/stop', { method: 'POST' });
        if (res.ok) {
          showToast('Campaign stopped.', 'warning');
          checkCampaignStatus();
        }
      } catch (e) {
        alert('Error stopping campaign');
      }
    }


    // --- LOGIN ACCOUNTS SECTION ---
    async function loadAccounts() {
      const grid = document.getElementById('accounts-cards-grid');
      try {
        const res = await fetch('/api/accounts');
        const data = await res.json();
        accountsData = data.accounts || [];

        if (accountsData.length === 0) {
          grid.innerHTML = `
            <div class="col-span-full p-12 text-center bg-white rounded-3xl border border-slate-200/80 space-y-4 shadow-card">
              <div class="w-14 h-14 rounded-2xl bg-indigo-50 text-indigo-600 flex items-center justify-center mx-auto text-2xl font-bold">🔑</div>
              <h4 class="font-display font-extrabold text-slate-900 text-lg">No Sending Accounts Connected</h4>
              <p class="text-xs text-slate-500 max-w-sm mx-auto font-medium">Connect your Telegram user accounts here. The Manual Sending engine uses them to rotate outbound messages safely.</p>
              <button onclick="openConnectAccountModal()" class="px-5 py-2.5 rounded-xl bg-indigo-600 text-white font-bold text-xs shadow-sm shadow-indigo-500/25 hover:bg-indigo-700">Connect First Account</button>
            </div>
          `;
          return;
        }

        grid.innerHTML = accountsData.map(a => {
          const isAuthed = a.is_authorized !== false;
          const statusPill = isAuthed
            ? `<span class="px-3 py-1 rounded-full text-xs font-bold bg-emerald-50 text-emerald-700 border border-emerald-200/70">🟢 Active</span>`
            : `<span class="px-3 py-1 rounded-full text-xs font-bold bg-red-50 text-red-700 border border-red-200/70">🔴 Re-auth</span>`;

          const name = [a.first_name, a.last_name].filter(Boolean).join(' ') || 'Telegram User';
          const username = a.username ? `@${a.username}` : 'No username';
          const sentToday = a.messages_sent_today || 0;

          return `
            <div class="bg-white p-6 rounded-3xl border border-slate-200/80 shadow-card hover:shadow-card-hover transition-all space-y-4">
              <div class="flex items-start justify-between">
                <div>
                  <div class="font-mono font-extrabold text-slate-900 text-base">${a.phone}</div>
                  <div class="text-xs text-slate-500 mt-1 font-semibold">${name} &bull; <span class="text-indigo-600 font-bold">${username}</span></div>
                </div>
                <div>${statusPill}</div>
              </div>

              <div class="grid grid-cols-2 gap-2 text-center p-3 bg-slate-50 rounded-2xl border border-slate-100 text-xs">
                <div>
                  <div class="text-[10px] text-slate-400 font-bold uppercase">Assigned Slot</div>
                  <div class="font-bold text-slate-800">#${a.slot || 1}</div>
                </div>
                <div>
                  <div class="text-[10px] text-slate-400 font-bold uppercase">Sent Today</div>
                  <div class="font-extrabold text-indigo-600">${sentToday}</div>
                </div>
              </div>

              <div class="flex items-center gap-2 pt-1 border-t border-slate-100">
                <button onclick="pingAccount('${a.phone}')" class="flex-1 py-2 px-3 rounded-xl border border-slate-200 text-slate-700 hover:bg-slate-50 text-xs font-bold flex items-center justify-center gap-1.5 transition-all active:scale-95">
                  <svg class="w-3.5 h-3.5 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"></path></svg>
                  <span>Ping Test</span>
                </button>
                <button onclick="deleteAccount('${a.phone}')" class="py-2 px-3.5 rounded-xl border border-red-200 text-red-600 hover:bg-red-50 text-xs font-bold transition-all active:scale-95">
                  <span>Disconnect</span>
                </button>
              </div>
            </div>
          `;
        }).join('');
      } catch (e) {
        grid.innerHTML = `<div class="col-span-full py-8 text-center text-red-500">Error loading accounts: ${e.message}</div>`;
      }
    }

    async function pingAccount(phone) {
      showToast(`Pinging ${phone}...`, 'info');
      try {
        const res = await fetch(`/api/accounts/${phone}/ping`, { method: 'POST' });
        const data = await res.json();
        if (data.authorized) {
          showToast(`Account ${phone} is HEALTHY and AUTHORIZED!`, 'success');
        } else {
          showToast(`Account ${phone} ping failed: ${data.error || 'Not authorized'}`, 'error');
        }
      } catch (e) {
        showToast('Ping error: ' + e.message, 'error');
      }
    }

    async function deleteAccount(phone) {
      if (!confirm(`Are you sure you want to disconnect Telegram account ${phone}?`)) return;
      try {
        const res = await fetch(`/api/accounts/${phone}`, { method: 'DELETE' });
        if (res.ok) {
          showToast(`Disconnected account ${phone}`, 'info');
          loadAccounts();
          loadStats();
        }
      } catch (e) {
        showToast('Error removing account', 'error');
      }
    }

    // Modal Connect Account
    function openConnectAccountModal() {
      document.getElementById('modal-account-phone').value = '';
      document.getElementById('modal-account-code').value = '';
      document.getElementById('modal-account-password').value = '';
      document.getElementById('account-step-1').classList.remove('hidden');
      document.getElementById('account-step-2').classList.add('hidden');
      document.getElementById('modal-connect-account').classList.remove('hidden');
    }
    function closeConnectAccountModal() {
      document.getElementById('modal-connect-account').classList.add('hidden');
    }

    async function submitStartAccountLogin() {
      const phone = document.getElementById('modal-account-phone').value.trim();
      if (!phone || !phone.startsWith('+')) {
        alert('Please enter a valid phone number with international country code (e.g. +919876543210).');
        return;
      }

      const btn = document.getElementById('btn-modal-send-code');
      btn.disabled = true;
      btn.innerText = 'Requesting code from Telegram...';

      try {
        const res = await fetch('/api/accounts/login/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ phone })
        });
        const data = await res.json();
        if (res.ok && data.session_token) {
          pendingLoginToken = data.session_token;
          document.getElementById('modal-display-phone').innerText = phone;
          document.getElementById('account-step-1').classList.add('hidden');
          document.getElementById('account-step-2').classList.remove('hidden');
          showToast('OTP sent! Check your Telegram app.', 'info');
        } else {
          alert(data.error || 'Failed to send OTP code');
        }
      } catch (e) {
        alert('Error: ' + e.message);
      } finally {
        btn.disabled = false;
        btn.innerText = 'Send Telegram OTP Code';
      }
    }

    async function submitCompleteAccountLogin() {
      const code = document.getElementById('modal-account-code').value.trim();
      const password = document.getElementById('modal-account-password').value.trim();

      if (!code) {
        alert('Please enter the verification code from your Telegram app.');
        return;
      }

      const btn = document.getElementById('btn-modal-complete-login');
      btn.disabled = true;
      btn.innerText = 'Authorizing...';

      try {
        const res = await fetch('/api/accounts/login/complete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_token: pendingLoginToken,
            code,
            password: password || undefined
          })
        });
        const data = await res.json();
        if (data.status === 'success') {
          showToast('Telegram account connected successfully!', 'success');
          closeConnectAccountModal();
          loadAccounts();
          loadStats();
        } else {
          alert(data.error || data.message || 'Verification failed');
        }
      } catch (e) {
        alert('Error: ' + e.message);
      } finally {
        btn.disabled = false;
        btn.innerText = 'Authorize & Connect Account';
      }
    }


    // --- AUTOMATIC SENDING REPORT SECTION ---
    async function loadReports() {
      const statusFilter = document.getElementById('reports-status-filter').value;
      const tbody = document.getElementById('reports-table-body');

      // 1. Load Analytics Stats
      try {
        const res = await fetch('/api/reports/analytics');
        const analytics = await res.json();

        document.getElementById('report-stat-total').innerText = analytics.total_sent || 0;
        document.getElementById('report-stat-today').innerText = analytics.sent_today || 0;
        document.getElementById('report-stat-success-rate').innerText = (analytics.success_rate_percent || 100) + '%';
        document.getElementById('report-stat-accounts').innerText = Object.keys(analytics.by_account || {}).length;

        // Breakdown by Account table
        const byAccTbody = document.getElementById('reports-by-account-tbody');
        const accountsList = Object.entries(analytics.by_account || {});
        if (accountsList.length === 0) {
          byAccTbody.innerHTML = `<tr><td colspan="3" class="py-5 text-center text-slate-400">No account dispatches recorded yet.</td></tr>`;
        } else {
          byAccTbody.innerHTML = accountsList.map(([phone, count]) => `
            <tr class="hover:bg-slate-50/80 transition-all">
              <td class="py-3 px-3.5 font-mono font-bold text-slate-800">${phone}</td>
              <td class="py-3 px-3.5 text-slate-500 font-semibold">Sending Node</td>
              <td class="py-3 px-3.5 text-right font-extrabold text-indigo-600">${count} msgs</td>
            </tr>
          `).join('');
        }

        // Breakdown by Lead Type
        const byType = analytics.by_recipient_type || {};
        const botsCount = byType.userbot || byType.bot || 0;
        const usersCount = byType.username || byType.user || 0;
        const totalTyped = botsCount + usersCount;
        const botPct = totalTyped > 0 ? Math.round((botsCount / totalTyped) * 100) : 0;
        const userPct = totalTyped > 0 ? Math.round((usersCount / totalTyped) * 100) : 0;

        document.getElementById('breakdown-userbots-count').innerText = `${botsCount} (${botPct}%)`;
        document.getElementById('breakdown-userbots-bar').style.width = botPct + '%';
        document.getElementById('breakdown-usernames-count').innerText = `${usersCount} (${userPct}%)`;
        document.getElementById('breakdown-usernames-bar').style.width = userPct + '%';

      } catch (e) {
        console.error('Error loading report analytics', e);
      }

      // 2. Load Detailed Reports List
      try {
        const url = statusFilter ? `/api/reports?status=${statusFilter}&limit=100` : '/api/reports?limit=100';
        const res = await fetch(url);
        const data = await res.json();
        const reports = data.reports || [];

        if (reports.length === 0) {
          tbody.innerHTML = `<tr><td colspan="7" class="py-16 text-center text-slate-400 font-medium">No delivery logs recorded yet. Launch a campaign from Manual Sending!</td></tr>`;
          return;
        }

        tbody.innerHTML = reports.map(r => {
          const sentAt = r.sent_at ? String(r.sent_at).substring(0, 19).replace('T', ' ') : 'Recent';
          const handle = r.recipient_username || '@unknown';
          const clean = handle.replace('@', '');
          const isBot = r.recipient_type === 'userbot' || r.recipient_type === 'bot';
          const typeBadge = isBot
            ? `<span class="px-2.5 py-0.5 rounded-full text-xs font-bold bg-indigo-50 text-indigo-700">🤖 Bot</span>`
            : `<span class="px-2.5 py-0.5 rounded-full text-xs font-bold bg-purple-50 text-purple-700">👤 User</span>`;

          const isSuccess = r.status === 'sent';
          const statusBadge = isSuccess
            ? `<span class="px-3 py-1 rounded-full text-xs font-bold bg-emerald-50 text-emerald-700 border border-emerald-200/80">✅ Sent</span>`
            : `<span class="px-3 py-1 rounded-full text-xs font-bold bg-red-50 text-red-700 border border-red-200/80">❌ Failed</span>`;

          const note = isSuccess ? (r.message_snippet || 'Delivered successfully') : (r.error_message || 'Delivery error');

          return `
            <tr class="hover:bg-slate-50/80 transition-all">
              <td class="py-3.5 px-6 text-xs text-slate-500 font-mono font-medium">${sentAt}</td>
              <td class="py-3.5 px-4 font-bold text-slate-900">
                <a href="https://t.me/${clean}" target="_blank" class="hover:text-indigo-600 hover:underline">${handle}</a>
              </td>
              <td class="py-3.5 px-4">${typeBadge}</td>
              <td class="py-3.5 px-4 text-xs font-mono font-bold text-slate-700">${r.sender_account_phone || 'Rotated'}</td>
              <td class="py-3.5 px-4 text-xs font-bold text-slate-700 truncate max-w-[160px]">${r.post_title || 'Promotional Post'}</td>
              <td class="py-3.5 px-4">${statusBadge}</td>
              <td class="py-3.5 px-6 text-xs text-slate-500 truncate max-w-[220px]" title="${note}">${note}</td>
            </tr>
          `;
        }).join('');
      } catch (e) {
        tbody.innerHTML = `<tr><td colspan="7" class="py-12 text-center text-red-500 font-bold">Failed to load reports: ${e.message}</td></tr>`;
      }
    }

    function exportReportsCsv() {
      const status = document.getElementById('reports-status-filter').value;
      window.location.href = `/api/reports/export-csv?status=${status}`;
    }


    // --- GLOBAL STATS & SYNC ---
    async function loadStats() {
      try {
        const res = await fetch('/api/stats');
        const s = await res.json();

        // Nav Badges
        document.getElementById('nav-leads-badge').innerText = s.total_leads || 0;
        document.getElementById('nav-accounts-badge').innerText = `${s.active_accounts || 0} Active`;
        document.getElementById('nav-reports-badge').innerText = s.total_messages_sent || 0;
        document.getElementById('top-accounts-status').innerText = `${s.active_accounts || 0} Sending Accounts Ready`;

        // Leads section stats
        document.getElementById('leads-stat-userbots').innerText = s.userbots_count || 0;
        document.getElementById('leads-stat-usernames').innerText = s.usernames_count || 0;
        document.getElementById('leads-stat-contacted').innerText = s.contacted_count || 0;
        document.getElementById('badge-count-userbots').innerText = s.userbots_count || 0;
        document.getElementById('badge-count-usernames').innerText = s.usernames_count || 0;

        // Target selector counts
        document.getElementById('target-count-userbots').innerText = s.userbots_count || 0;
        document.getElementById('target-count-usernames').innerText = s.usernames_count || 0;
        document.getElementById('target-count-uncontacted').innerText = s.pending_count || 0;

      } catch (e) {
        console.error('Error loading global stats', e);
      }
    }

    async function triggerSyncNow() {
      showToast('Initiating external source sync...', 'info');
      try {
        const res = await fetch('/api/trigger-sync', { method: 'POST' });
        const data = await res.json();
        if (data.status === 'success') {
          showToast(`Sync complete! Found ${data.sources_count} source channels.`, 'success');
          loadLeads();
          loadStats();
        } else {
          showToast(data.message || 'No new channels in this window.', 'info');
        }
      } catch (e) {
        showToast('Sync error: ' + e.message, 'error');
      }
    }

    // INIT
    window.addEventListener('DOMContentLoaded', () => {
      loadStats();
      loadLeads();
      setInterval(loadStats, 20000);
    });
  </script>
</body>
</html>"""
