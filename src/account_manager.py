import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
    UserDeactivatedError,
)
from telethon.sessions import StringSession
from telethon.tl.functions import PingRequest

from src.config import settings
from src.db import Database

logger = logging.getLogger("telegram_promotion.account_manager")


class AccountManager:
    """
    Manages unlimited Telegram user accounts, handles phone OTP + 2FA interactive login,
    session storage in MongoDB, and account health monitoring.
    """

    # In-memory storage for active sign-in flows: session_token -> dict
    _pending_logins: Dict[str, Dict[str, Any]] = {}

    @classmethod
    async def cleanup_expired_logins(cls):
        """Removes pending login attempts older than 10 minutes."""
        now = datetime.utcnow()
        expired = [
            token
            for token, data in cls._pending_logins.items()
            if now - data.get("created_at", now) > timedelta(minutes=10)
        ]
        for token in expired:
            pending = cls._pending_logins.pop(token, None)
            if pending:
                task = pending.get("keepalive_task")
                if task and not task.done():
                    task.cancel()
                if pending.get("client"):
                    try:
                        await pending["client"].disconnect()
                    except Exception:
                        pass

    @classmethod
    async def get_next_available_slot(cls) -> int:
        """Determines the next sequential slot (1 to 5+) for a newly registered account."""
        accounts = await Database.get_all_telegram_accounts()
        existing_slots = {acc.get("assigned_slot", 0) for acc in accounts}
        for s in range(1, 100):
            if s not in existing_slots:
                return s
        return len(accounts) + 1

    @classmethod
    async def start_phone_login(
        cls,
        phone: str,
        slot: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Step 1: Starts a phone login by creating a Telethon client and requesting an SMS/Telegram OTP code.
        Returns a session_token to be used in Step 2.
        """
        await cls.cleanup_expired_logins()

        clean_phone = phone.strip().replace(" ", "").replace("-", "")
        if not clean_phone.startswith("+"):
            clean_phone = f"+{clean_phone}"

        if not settings.telegram_api_id or not settings.telegram_api_hash:
            raise ValueError("TELEGRAM_API_ID and TELEGRAM_API_HASH must be configured in .env!")

        target_slot = slot if slot is not None and slot > 0 else await cls.get_next_available_slot()

        # Check if phone is already registered
        existing = await Database.get_telegram_account_by_phone(clean_phone)
        if existing and existing.get("status") == "active":
            logger.info("Account %s already registered in slot %d. Re-authorizing session...", clean_phone, existing.get("assigned_slot", 1))

        session_token = str(uuid.uuid4())
        client = TelegramClient(StringSession(), settings.telegram_api_id, settings.telegram_api_hash)

        try:
            await client.connect()
            sent_code = await client.send_code_request(clean_phone)
            phone_code_hash = sent_code.phone_code_hash

            async def _keep_alive(cl: TelegramClient, tok: str):
                try:
                    while tok in cls._pending_logins:
                        await asyncio.sleep(12)
                        if tok not in cls._pending_logins:
                            break
                        if cl.is_connected():
                            try:
                                await cl(PingRequest(ping_id=0))
                            except Exception:
                                pass
                except (asyncio.CancelledError, Exception):
                    pass

            keepalive_task = asyncio.create_task(_keep_alive(client, session_token))

            cls._pending_logins[session_token] = {
                "client": client,
                "phone": clean_phone,
                "phone_code_hash": phone_code_hash,
                "slot": target_slot,
                "created_at": datetime.utcnow(),
                "keepalive_task": keepalive_task
            }

            logger.info("OTP Code sent to %s (Session Token: %s, Assigned Slot: %d)", clean_phone, session_token, target_slot)
            return {
                "status": "code_sent",
                "session_token": session_token,
                "phone": clean_phone,
                "slot": target_slot,
                "message": f"Verification code sent to {clean_phone} via Telegram/SMS.",
                "timeout_seconds": 300
            }
        except PhoneNumberInvalidError:
            await client.disconnect()
            raise ValueError(f"The phone number '{clean_phone}' is invalid.")
        except FloodWaitError as e:
            await client.disconnect()
            raise RuntimeError(f"Telegram FloodWait: please wait {e.seconds} seconds before trying again.")
        except Exception as e:
            await client.disconnect()
            logger.error("Failed to start phone login for %s: %s", clean_phone, e)
            raise RuntimeError(f"Failed to request login code: {e}")

    @classmethod
    async def complete_phone_login(
        cls,
        session_token: str,
        code: Optional[str] = None,
        password: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Step 2: Submits the verification code (and 2FA password if enabled).
        On success, extracts the StringSession and saves the account in MongoDB.
        """
        pending = cls._pending_logins.get(session_token)
        if not pending:
            raise ValueError("Login session expired or invalid. Please click 'Back to Phone Input' and request a new code.")

        client: TelegramClient = pending["client"]
        phone: str = pending["phone"]
        phone_code_hash: str = pending["phone_code_hash"]
        slot: int = pending["slot"]
        code_verified: bool = pending.get("code_verified", False)

        clean_code = code.strip().replace(" ", "").replace("-", "") if code else None
        if clean_code:
            pending["code"] = clean_code

        clean_pwd = password.strip() if password and password.strip() else None

        try:
            if not client.is_connected():
                logger.info("Pending login client for %s was disconnected, reconnecting...", phone)
                await client.connect()

            # Scenario 1: OTP code needs to be verified first
            if clean_code and not code_verified:
                logger.info("Submitting OTP code for %s...", phone)
                try:
                    await client.sign_in(phone, clean_code, phone_code_hash=phone_code_hash)
                    # Succeeded without 2FA!
                except SessionPasswordNeededError:
                    logger.info("2FA Cloud Password required for %s.", phone)
                    pending["code_verified"] = True
                    # If user also supplied 2FA password in the input, complete it immediately
                    if clean_pwd:
                        logger.info("Submitting provided 2FA Cloud Password for %s...", phone)
                        await client.sign_in(password=clean_pwd)
                    else:
                        return {
                            "status": "requires_password",
                            "session_token": session_token,
                            "phone": phone,
                            "slot": slot,
                            "message": "Two-Factor Authentication (2FA) cloud password is required."
                        }

            # Scenario 2: Code was already verified in previous turn, now submitting 2FA password
            elif clean_pwd:
                logger.info("Submitting 2FA Cloud Password for %s...", phone)
                try:
                    await client.sign_in(password=clean_pwd)
                except (AuthKeyUnregisteredError, Exception) as pwd_err:
                    err_msg = str(pwd_err).lower()
                    if "key is not registered" in err_msg or "authkeyunregistered" in err_msg or isinstance(pwd_err, AuthKeyUnregisteredError):
                        logger.warning("Auth key dropped or unauthenticated for %s during 2FA; re-binding OTP code before 2FA check...", phone)
                        saved_code = pending.get("code") or clean_code
                        if not saved_code:
                            raise RuntimeError("Telegram login session expired. Please click 'Back to Phone Input' and request a fresh login code.")

                        if not client.is_connected():
                            await client.connect()
                        try:
                            await client.sign_in(phone, saved_code, phone_code_hash=phone_code_hash)
                        except SessionPasswordNeededError:
                            pass
                        # Now retry password verification with freshly bound 2FA state
                        await client.sign_in(password=clean_pwd)
                    else:
                        raise

            elif code_verified:
                return {
                    "status": "requires_password",
                    "session_token": session_token,
                    "phone": phone,
                    "slot": slot,
                    "message": "Please enter your 2FA Cloud Password to finish connecting."
                }
            else:
                raise ValueError("Verification code must be provided.")

            # Login successful: save session string
            session_str = client.session.save()
            me = await client.get_me()

            account_data = {
                "account_index": slot,
                "phone": phone,
                "user_id": me.id,
                "first_name": me.first_name or "",
                "last_name": me.last_name or "",
                "username": me.username or "",
                "session_string": session_str,
                "status": "active",
                "assigned_slot": slot,
                "daily_send_count": 0,
                "last_send_date": None,
                "last_active_at": datetime.utcnow(),
                "created_at": datetime.utcnow()
            }

            await Database.save_telegram_account(account_data)
            logger.info("✅ Telegram Account %s (%s, ID: %d) successfully saved in Slot #%d!", phone, me.first_name, me.id, slot)

            # Cleanup
            kp_task = pending.get("keepalive_task")
            if kp_task and not kp_task.done():
                kp_task.cancel()
            cls._pending_logins.pop(session_token, None)
            await client.disconnect()

            return {
                "status": "success",
                "message": f"Account {phone} ({me.first_name}) successfully connected and assigned to Slot #{slot}.",
                "account": {
                    "phone": phone,
                    "user_id": me.id,
                    "name": f"{me.first_name or ''} {me.last_name or ''}".strip(),
                    "username": me.username,
                    "slot": slot,
                    "status": "active"
                }
            }

        except SessionPasswordNeededError:
            pending["code_verified"] = True
            return {
                "status": "requires_password",
                "session_token": session_token,
                "phone": phone,
                "slot": slot,
                "message": "Two-Factor Authentication (2FA) cloud password is required."
            }
        except PasswordHashInvalidError:
            return {
                "status": "requires_password",
                "session_token": session_token,
                "phone": phone,
                "slot": slot,
                "message": "Incorrect 2FA Cloud Password. Please try again."
            }
        except PhoneCodeInvalidError:
            raise ValueError("The 5-digit verification code you entered is invalid. Please check your Telegram app.")
        except PhoneCodeExpiredError:
            raise ValueError("The verification code has expired. Please click 'Back to Phone Input' and request a new code.")
        except AuthKeyUnregisteredError as e:
            logger.error("AuthKeyUnregisteredError for %s: %s", phone, e)
            raise RuntimeError("Telegram session expired or invalidated. Please click 'Back to Phone Input' and request a fresh login code.")
        except FloodWaitError as e:
            logger.error("FloodWait on login for %s: %d seconds", phone, e.seconds)
            raise RuntimeError(f"Telegram FloodWait: wait {e.seconds}s before retrying.")
        except Exception as e:
            err_str = str(e)
            if "key is not registered" in err_str.lower() or "getpasswordrequest" in err_str.lower():
                raise RuntimeError("Telegram 2FA session expired. Please enter both your 5-digit code and 2FA password together, or click 'Back to Phone Input' to request a new code.")
            logger.error("Error completing login for %s: %s", phone, e)
            raise RuntimeError(f"Login failed: {e}")

    @classmethod
    def get_client(cls, session_string: str) -> TelegramClient:
        """Instantiates a Telethon client from an account's StringSession."""
        if not settings.telegram_api_id or not settings.telegram_api_hash:
            raise ValueError("TELEGRAM_API_ID and TELEGRAM_API_HASH are not configured!")
        return TelegramClient(StringSession(session_string), settings.telegram_api_id, settings.telegram_api_hash)

    @classmethod
    async def ping_account(cls, phone: str) -> Dict[str, Any]:
        """
        Tests if an account's session is still valid, updates its status and last_active_at.
        """
        acc = await Database.get_telegram_account_by_phone(phone)
        if not acc:
            raise ValueError(f"Account with phone '{phone}' not found.")

        session_str = acc.get("session_string")
        if not session_str:
            await Database.update_telegram_account_status(phone, "invalid_session")
            return {"status": "invalid_session", "phone": phone, "error": "No session string"}

        client = cls.get_client(session_str)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await Database.update_telegram_account_status(phone, "logged_out")
                await client.disconnect()
                return {"status": "logged_out", "phone": phone, "error": "Session is not authorized"}

            me = await client.get_me()
            await Database.update_telegram_account_status(phone, "active")
            await client.disconnect()

            return {
                "status": "active",
                "phone": phone,
                "user_id": me.id,
                "first_name": me.first_name or "",
                "username": me.username or "",
                "slot": acc.get("assigned_slot", 1)
            }
        except AuthKeyUnregisteredError:
            await Database.update_telegram_account_status(phone, "logged_out")
            return {"status": "logged_out", "phone": phone, "error": "Session revoked"}
        except UserDeactivatedError:
            await Database.update_telegram_account_status(phone, "banned")
            return {"status": "banned", "phone": phone, "error": "User deactivated / banned by Telegram"}
        except FloodWaitError as e:
            await Database.update_telegram_account_status(phone, f"flood_wait_{e.seconds}s")
            return {"status": "flood_wait", "phone": phone, "seconds": e.seconds}
        except Exception as e:
            logger.error("Ping error for %s: %s", phone, e)
            return {"status": "error", "phone": phone, "error": str(e)}

    @classmethod
    async def ping_all_accounts(cls) -> List[Dict[str, Any]]:
        """Pings all accounts in MongoDB and reports their statuses."""
        accounts = await Database.get_all_telegram_accounts()
        results = []
        for acc in accounts:
            phone = acc.get("phone")
            if phone:
                res = await cls.ping_account(phone)
                results.append(res)
        return results
