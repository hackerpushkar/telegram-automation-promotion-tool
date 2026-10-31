# setup.md - Setup & Installation Guide

This document describes how to configure, set up, and run the **Telegram Promotion Tool**.

---

## 1. Prerequisites

- **Python**: 3.10 or higher
- **MongoDB**: Local MongoDB instance (`mongodb://localhost:27017`) or MongoDB Atlas URI
- **Telegram API Credentials**:
  - `TELEGRAM_API_ID` & `TELEGRAM_API_HASH` (obtained from [my.telegram.org](https://my.telegram.org))
  - A Telegram account phone number or session string to scan channels
- **ProAffiliaters API Key**:
  - Pre-configured key: `proaff_ext_source_sync_key_2026`

---

## 2. Quick Setup

### Step 1: Create Virtual Environment
```bash
python -m venv venv

# Windows (PowerShell):
.\venv\Scripts\Activate.ps1

# Windows (Command Prompt):
.\venv\Scripts\activate.bat

# Linux / macOS:
source venv/bin/activate
```

### Step 2: Install Dependencies
```bash
pip install -r requirements.txt
```

### Step 3: Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Edit `.env` with your credentials:
```env
# ProAffiliaters External API
PROAFFILIATERS_API_URL=https://app.proaffiliaters.com/api/external/sources/recent
PROAFFILIATERS_API_KEY=proaff_ext_source_sync_key_2026
SYNC_INTERVAL_MINUTES=30

# Telegram Client (Telethon MTProto)
TELEGRAM_API_ID=your_api_id_here
TELEGRAM_API_HASH=your_api_hash_here
TELEGRAM_PHONE_NUMBER=+1234567890
TELEGRAM_SESSION_NAME=lead_scanner_session

# MongoDB Connection
# Only leads and Info Bot user/broadcast logs are stored; source channels remain ephemeral.
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB_NAME=telegram_promotion_tool
MONGODB_LEADS_COLLECTION=leads
MONGODB_INFO_BOT_USERS_COLLECTION=info_bot_users
MONGODB_INFO_BOT_BROADCASTS_COLLECTION=info_bot_broadcasts

# Official Info Bot (@ProaffiliatersInfoBot)
TELEGRAM_INFO_BOT_TOKEN=8777035188:AAFvldFDmnaVasOM3niIRnHPIv6lrI-klng
TELEGRAM_INFO_BOT_ADMIN_ID=7573032265
TELEGRAM_INFO_BOT_USERNAME=ProaffiliatersInfoBot
WEBAPP_URL=https://app.proaffiliaters.com
OFFICIAL_CHANNEL_URL=https://t.me/proaffiliaters
SUPPORT_BOT_URL=https://t.me/ProAffiliatersHelpBot

# Web UI Dashboard Server
WEB_HOST=127.0.0.1
WEB_PORT=8000

# Autonomous Telegram Keyword Search
KEYWORD_SEARCH_ENABLED=true
KEYWORD_SEARCH_INTERVAL_MINUTES=30
KEYWORD_SEARCH_LIMIT_PER_KEYWORD=15

# Logging & Run Mode
LOG_LEVEL=INFO
DEBUG=false
```

---

## 3. Running the Service & Accessing the Dashboard

### Launching the Engine & Web UI
You can start the tool and automatically open the dashboard using either command:

**Option A (Using npm):**
```bash
npm run dev
```

**Option B (Using Python directly):**
```bash
python src/main.py
```
This starts:
1. **Background Polling Engine**: Polls ProAffiliaters API every 30 minutes, inspects Telegram channel bios, and saves leads into MongoDB.
2. **Web UI Dashboard**: Automatically starts and opens in your browser at [http://localhost:8000](http://localhost:8000).

Open your browser to [http://localhost:8000](http://localhost:8000) to view:
- Premium ProAffiliaters sidebar layout with grouped navigation and real-time badge counters.
- Live lead directory (filter by `User` vs `Bot`, search, update outreach status, export CSV).
- Multi-Account management console (Slots 1-5, in-app MTProto credential setup, interactive OTP login).
- 5-Week Drip Funnel monitor with automated progressive delivery across dedicated accounts.
- Interactive Bio Scanner Playground to test channel bios and extraction logic.
- Live sync log stream and manual "Sync API Now" button.

### Initial Telegram MTProto Login
On the first run with valid `TELEGRAM_API_ID` & `TELEGRAM_API_HASH`, Telethon will request your verification code once in the terminal. After that, session is cached in `lead_scanner_session.session`.

### Running in Background / Production (24/7 VPS Deployment)

For production deployment on your Linux VPS (Ubuntu/Debian) bound to **`https://telegramtools.proaffiliaters.com`**, use the master deployment script:

```bash
# Make script executable
chmod +x deploy.sh

# 1. Full Deploy (Git pull, Python venv, dependencies, PM2 24/7 supervision)
./deploy.sh

# 2. Configure Nginx Reverse Proxy for telegramtools.proaffiliaters.com
./deploy.sh --setup-nginx

# 3. Provision Let's Encrypt SSL Certificate (HTTPS)
./deploy.sh --setup-ssl
```

#### Available Deployment Flags:
- `./deploy.sh` — Standard production deployment (pulls branch, updates virtual environment, restarts in PM2, saves process state).
- `./deploy.sh --quick` — Fast restart without re-installing pip dependencies.
- `./deploy.sh --setup-nginx` — Automatically creates and enables `/etc/nginx/sites-available/telegramtools-proaffiliaters` routing port 8000.
- `./deploy.sh --setup-ssl` — Obtains and binds free Let's Encrypt SSL via Certbot.
- `./deploy.sh --setup-all` — Executes full deploy + Nginx configuration + SSL setup in one automated command.
- `./deploy.sh --setup-systemd` — Configures native Linux systemd service (`telegram-promotion-tool.service`) as an alternative to PM2.
- `./deploy.sh --status` — Displays real-time health for PM2, Port 8000 listener, MongoDB, and Nginx.
- `./deploy.sh --logs` — Streams live PM2 stdout/stderr logs.

---

## 4. Verification & Testing

### Testing API Connection Only:
You can run the API client test standalone:
```bash
python -m src.api_client
```
Expected output:
- `[SourceSync] Connected. Status: none (No new sources in last 30 minutes)` or `Status: success (N sources found)`.

### Verifying Info Bot (@ProaffiliatersInfoBot) & MongoDB:
Run the comprehensive Info Bot test suite:
```bash
python testing_files/test_info_bot.py
```
Expected output:
- Validates Telegram Bot API getMe connection (`@ProaffiliatersInfoBot`).
- Tests button markup parser syntax `[Button Title | https://url]`.
- Tests admin authorization (Admin ID: `7573032265`).
- Verifies lead upsert, block detection, and broadcast audit trail in MongoDB collection `info_bot_users` and `info_bot_broadcasts`.

### Verifying Channel Publisher & Bot Forwarder:
Run the Channel Publisher and Bot Forwarder test suite:
```bash
python testing_files/test_bot_forwarder.py
```
Expected output:
- Validates all 5 promotional images exist and smart ad copy texts match.
- Verifies deep-link buttons (`https://t.me/ProaffiliatersInfoBot?start=promo_postX`).
- Tests MongoDB persistence for `published_posts` and `bot_campaigns`.
- Verifies `BotForwarderEngine` state management and offline safety handling.

### Verifying Multi-Account Manager & 5-Week Drip Funnel:
Run the Multi-Account & 5-Week Drip test suite:
```bash
python testing_files/test_multi_account_drip.py
```
Expected output:
- Validates `TelegramAccount` schema and slot mappings (Slot 1 to 5).
- Verifies MongoDB CRUD operations and status updates in `telegram_accounts`.
- Tests progressive stage transitions (Stage 1 to 5), 7-day cooldown rules, and stage 5 permanent completion.
- Verifies `DripEngine` funnel analytics calculation.

### Verifying Autonomous Keyword Search Engine:
Run the Autonomous Keyword Search test suite:
```bash
python testing_files/test_keyword_search.py
```
Expected output:
- Validates `SearchKeyword` and `DiscoveredChannel` schemas.
- Verifies MongoDB CRUD operations and stats tracking for target keywords.
- Tests discovered channels deduplication and 7-day anti-spam cooldown.
- Verifies keyword search KPI analytics calculation.

### Verifying Dashboard Authentication & MTProto Account Login:
Run the authentication and login security test suite:
```bash
python testing_files/test_auth_and_account_login.py
```
Expected output:
- Tests admin credentials check (`spushkar812@gmail.com` / `Pushkar@9084`).
- Tests HMAC session token generation and tamper verification.
- Verifies unauthenticated route guarding (`/api/*` returns 401 Unauthorized, `/` serves Login view).
- Verifies login cookie issuance and authenticated access to stats and dashboard.
- Verifies logout cookie invalidation.

---

## 8. Dashboard Authentication & Public URL Security

When hosted on the public domain `https://telegramtools.proaffiliaters.com`, access to the dashboard and management APIs is protected by a login system:

- **Admin Login Email**: `spushkar812@gmail.com`
- **Admin Password**: `Pushkar@9084`
- **Session Duration**: 30 Days (HTTP-only secure HMAC cookie)

### Environment Variables:
```env
DASHBOARD_AUTH_ENABLED=true
DASHBOARD_AUTH_EMAIL=spushkar812@gmail.com
DASHBOARD_AUTH_PASSWORD=Pushkar@9084
DASHBOARD_SESSION_SECRET=proaff_secret_session_token_key_2026_9084_sec
```

### Connecting Telegram Accounts (2 Easy Ways):

#### Option A: Directly from the Web Dashboard (Recommended)
1. Open [http://localhost:8000](http://localhost:8000).
2. Go to the **🔑 Telegram Accounts** tab.
3. If not already configured, enter your free `API ID` and `API HASH` from [my.telegram.org](https://my.telegram.org) into the setup banner and click **💾 Save & Activate**.
4. Click **➕ Connect New Account** (or click `+ Connect Slot #1`).
5. Enter your phone number with country code (e.g. `+919876543210`), click **📨 Send Code**, and type the 5-digit verification code sent to your Telegram app (plus 2FA password if enabled).
6. Your account is immediately connected and ready to run promotional drip campaigns!

#### Option B: Via Interactive CLI Login Wizard
```bash
python scripts/login_account.py
```
Prompts for phone number, slot assignment (Slot 1 to 5), Telegram verification code, and 2FA cloud password if set. Accounts are saved into MongoDB collection `telegram_accounts` with portable `StringSession` strings.

### Verifying MongoDB Collections:
Inspect stored records with mongosh:
```javascript
use telegram_promotion_tool;
db.leads.find().pretty();
db.info_bot_users.find().pretty();
db.info_bot_broadcasts.find().pretty();
db.published_posts.find().pretty();
db.bot_campaigns.find().pretty();
db.telegram_accounts.find().pretty();
db.search_keywords.find().pretty();
db.discovered_channels.find().pretty();
```


