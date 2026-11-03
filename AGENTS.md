# AGENTS.md - Agent Instructions & Operating Rules

Welcome to **Telegram Promotion Tool** (ProAffiliaters Lead Extraction Engine).

This document serves as the **mandatory instructions and operating guidelines** for any AI agent or developer working in this codebase.

---

## 🚨 MANDATORY AGENT DIRECTIVE: UPDATE DOCUMENTATION ON EVERY ACTION

> **CRITICAL RULE**: Whenever an AI agent does **ANYTHING** in this project (implements code, adds a feature, fixes a bug, refactors, creates a script, or runs tests), the agent **MUST ALWAYS UPDATE** the documentation before concluding the turn.

### Required Documentation Update Checklist:
1. **`implementation.md`**:
   - Check off completed tasks `[x]`.
   - Update the **Current Status** section with the latest progress.
   - Add an entry in the **Change & Progress Log** detailing what was modified, created, or tested.
2. **`projectinfo.md`**:
   - If new models, endpoints, schemas, or architectural components were introduced, reflect them immediately in `projectinfo.md`.
3. **`setup.md`**:
   - If new environment variables, dependencies, or configuration keys were introduced, update `setup.md` and `.env.example`.

**Failure to update documentation after making changes is strictly prohibited.**

---

## 📌 Project Overview & Purpose

**Telegram Promotion Tool** is an automated background lead extraction and promotion pipeline connected to the **ProAffiliaters Web Application**.

### VPS Deployment Standard:
- **Server Domain**: `https://telegramtools.proaffiliaters.com`
- **VPS Project Directory**: `telegram-automation-promotion-tool` (Exact path: `cd ~/telegram-automation-promotion-tool` or `cd /root/telegram-automation-promotion-tool`).
- **Quick Deployment Command**:
  ```bash
  cd ~/telegram-automation-promotion-tool && git pull origin main && ./deploy.sh --quick
  ```
- **PM2 Process**: `telegram-promotion-tool`

### Primary Workflow:
1. **External Source Polling (Every 30 Minutes)**:
   - Polls `https://app.proaffiliaters.com/api/external/sources/recent?minutes=30` with header `x-api-key: proaff_ext_source_sync_key_2026`.
   - If API returns `"status": "none"`, gracefully sleep until next 30m cycle.
   - If `"status": "success"`, processes each newly added deal channel's invite link / username.
2. **Channel Bio & Contact Scanning**:
   - Resolves invite links / public usernames using Telegram MTProto client (Telethon).
   - Scans channel entity description, bio, pinned messages, and embedded contact links.
   - Extracts deal owner handles (@usernames) and bots (usernames ending in `bot` / `Bot`).
3. **Lead Storage in MongoDB**:
   - **Do NOT store sources in MongoDB** (sources remain ephemeral or deduplicated in memory/cache).
   - **Store extracted leads only** into MongoDB collection `leads`.
   - Distinguish lead types: `user` (human manager/owner) vs `bot` (promotion/deal bot).
   - Enforce unique index on lead username to prevent duplicates.

---

## 🛠️ Codebase Standards & Best Practices

1. **Language & Frameworks**:
   - **Python 3.10+** (asyncio, Telethon / Pyrogram, Motor / PyMongo, HTTPX, APScheduler).
   - Environment configuration using `python-dotenv` and Pydantic Settings.
2. **Error Handling & Rate Limits**:
   - Telegram MTProto imposes strict `FloodWaitError`. All Telegram operations must catch `FloodWaitError` and sleep `error.seconds + 2`.
   - Handle revoked, expired, or private invite links gracefully without crashing.
   - Web API polling must include a 15-second timeout and clean retry on network failures.
3. **MongoDB Connection**:
   - Maintain a singleton MongoDB connection pool.
   - Ensure indexing is verified on startup (`username` unique index).
4. **Clean Code & Modularity**:
   - Keep polling worker, Telegram scanner, lead extractor, and MongoDB repository in separate modular packages under `src/`.

---

## 📂 Core Documentation Map
- **[`projectinfo.md`](file:///c:/Users/spush/OneDrive/Desktop/Proaffiliaters/Telegram Promotion Tool/projectinfo.md)** - Architecture, flowcharts, API specs, database schemas, and lead classifications.
- **[`implementation.md`](file:///c:/Users/spush/OneDrive/Desktop/Proaffiliaters/Telegram Promotion Tool/implementation.md)** - Implementation tracker, task status, roadmap, and update logs.
- **[`setup.md`](file:///c:/Users/spush/OneDrive/Desktop/Proaffiliaters/Telegram Promotion Tool/setup.md)** - Installation, virtual environment, Telegram session configuration, and launch commands.
- **[`GEMINI.md`](file:///c:/Users/spush/OneDrive/Desktop/Proaffiliaters/Telegram Promotion Tool/GEMINI.md)** - Mirror configuration for Gemini / Antigravity agent instructions.
