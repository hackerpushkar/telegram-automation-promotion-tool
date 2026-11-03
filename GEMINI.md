# GEMINI.md - Agent Instructions & Project Guidelines

> **CRITICAL DIRECTIVE**: Whenever an AI agent performs **ANY** action in this project (coding, testing, fixing, refactoring), the agent **MUST ALWAYS UPDATE** the documentation before concluding its turn:
> 1. `implementation.md` (Update task statuses, checkboxes, and the change log)
> 2. `projectinfo.md` (Update if any architectural or data model change was made)
> 3. `setup.md` (Update if dependencies, environment variables, or run commands change)

See **[`AGENTS.md`](file:///c:/Users/spush/OneDrive/Desktop/Proaffiliaters/Telegram Promotion Tool/AGENTS.md)** for full agent operating standards and workflow rules.

---

## Quick Reference
- **VPS Project Directory**: `telegram-automation-promotion-tool` (Exact path: `cd ~/telegram-automation-promotion-tool` or `cd /root/telegram-automation-promotion-tool`)
- **Quick VPS Update Command**:
  ```bash
  cd ~/telegram-automation-promotion-tool && git pull origin main && ./deploy.sh --quick
  ```
- **Source Web API**: `GET https://app.proaffiliaters.com/api/external/sources/recent?minutes=30`
- **Auth Header**: `x-api-key: proaff_ext_source_sync_key_2026`
- **Interval**: Every 30 minutes
- **Database**: MongoDB (Only store leads in collection `leads`; do NOT store sources)
- **Lead Classification**:
  - `user`: Human channel owners/managers (`@username`)
  - `bot`: Telegram bots (usernames ending in `bot` or `Bot`)
