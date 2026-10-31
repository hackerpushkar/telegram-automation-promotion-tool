import hmac
import hashlib
import time
import logging
from typing import Optional
from fastapi import Request

from src.config import settings

logger = logging.getLogger("telegram_promotion.auth")

SESSION_COOKIE_NAME = "session_token"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 days


def create_session_token(email: str) -> str:
    """
    Generates a secure HMAC-SHA256 signed session token containing email and timestamp.
    Format: <email>:<timestamp>:<signature>
    """
    clean_email = email.strip().lower()
    ts = int(time.time())
    data = f"{clean_email}:{ts}"
    signature = hmac.new(
        settings.dashboard_session_secret.encode("utf-8"),
        data.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return f"{data}:{signature}"


def verify_session_token(token: str, max_age: int = SESSION_MAX_AGE_SECONDS) -> Optional[str]:
    """
    Validates a session token signature and timestamp.
    Returns the authenticated email if valid, or None if expired/tampered.
    """
    if not token or not isinstance(token, str):
        return None

    parts = token.strip().split(":")
    if len(parts) != 3:
        return None

    email, ts_str, signature = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return None

    # Check expiration
    if time.time() - ts > max_age:
        logger.debug("Session token expired for %s", email)
        return None

    # Verify cryptographic signature
    data = f"{email}:{ts_str}"
    expected_sig = hmac.new(
        settings.dashboard_session_secret.encode("utf-8"),
        data.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_sig):
        logger.warning("Invalid session token signature attempt for %s", email)
        return None

    return email


def authenticate_user(email: str, password: str) -> bool:
    """
    Verifies user credentials against configured dashboard admin email and password.
    """
    if not email or not password:
        return False

    clean_email = email.strip().lower()
    expected_email = settings.dashboard_auth_email.strip().lower()

    if clean_email != expected_email:
        return False

    return hmac.compare_digest(password.strip(), settings.dashboard_auth_password.strip())


def get_current_user_email(request: Request) -> Optional[str]:
    """
    Extracts and validates session token from Cookie or Authorization header.
    """
    if not settings.dashboard_auth_enabled:
        return settings.dashboard_auth_email

    # Check HTTP-only cookie first
    token = request.cookies.get(SESSION_COOKIE_NAME)

    # Check Authorization header (Bearer <token>)
    if not token:
        auth_header = request.headers.get("Authorization") or ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

    if not token:
        return None

    return verify_session_token(token)


def is_authenticated(request: Request) -> bool:
    """
    Returns True if request carries a valid session or if authentication is disabled.
    """
    return get_current_user_email(request) is not None

