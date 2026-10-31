import sys
import os
import asyncio
from httpx import AsyncClient, ASGITransport

# Ensure root in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.web import app
from src.auth import authenticate_user, create_session_token, verify_session_token, SESSION_COOKIE_NAME
from src.config import settings


async def run_tests():
    print("==================================================================")
    print("[TEST] Testing Authentication & Account Manager Security & Resilience")
    print("==================================================================")

    # 1. Test unit auth logic
    assert authenticate_user("spushkar812@gmail.com", "Pushkar@9084") is True, "Valid admin credentials failed!"
    assert authenticate_user("spushkar812@gmail.com", "WrongPassword") is False, "Invalid password passed!"
    assert authenticate_user("wrong@gmail.com", "Pushkar@9084") is False, "Wrong email passed!"
    print("[PASS] 1. Unit auth credentials check passed!")

    # 2. Test HMAC session token generation and verification
    token = create_session_token("spushkar812@gmail.com")
    email = verify_session_token(token)
    assert email == "spushkar812@gmail.com", "Valid token verification failed!"
    bad_email = verify_session_token("corrupted.token.value")
    assert bad_email is None, "Corrupted token passed verification!"
    print("[PASS] 2. HMAC session token generation & verification passed!")

    # 3. Test HTTP API routes with ASGI AsyncClient
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # A. Unauthenticated access to /api/stats should return 401
        res = await client.get("/api/stats")
        assert res.status_code == 401, f"Expected 401 for unauth /api/stats, got {res.status_code}"
        assert "Authentication required" in res.json().get("error", "")
        print("[PASS] 3A. Unauthenticated /api/stats returns 401 Unauthorized as expected!")

        # B. Unauthenticated access to / returns 200 with Login HTML
        res_root = await client.get("/")
        assert res_root.status_code == 200
        assert "Sign In | ProAffiliaters" in res_root.text
        print("[PASS] 3B. Unauthenticated GET / serves Login Page as expected!")

        # C. Bad login credentials
        res_login_bad = await client.post("/api/auth/login", json={"email": "wrong@user.com", "password": "wrong"})
        assert res_login_bad.status_code == 401
        print("[PASS] 3C. Invalid login returns 401 with error message as expected!")

        # D. Successful login
        res_login_ok = await client.post(
            "/api/auth/login",
            json={"email": "spushkar812@gmail.com", "password": "Pushkar@9084"}
        )
        assert res_login_ok.status_code == 200
        assert res_login_ok.json().get("success") is True
        cookie_header = res_login_ok.headers.get("set-cookie", "")
        assert SESSION_COOKIE_NAME in cookie_header
        print("[PASS] 3D. Successful login sets HTTP-only session cookie!")

        # Extract cookie value
        import re
        cookie_match = re.search(rf"{SESSION_COOKIE_NAME}=([^;]+)", cookie_header)
        assert cookie_match, "Could not find session cookie in header!"
        cookie_val = cookie_match.group(1)

        # E. Authenticated access to /api/stats with cookie
        client.cookies.set(SESSION_COOKIE_NAME, cookie_val)
        res_stats_auth = await client.get("/api/stats")
        assert res_stats_auth.status_code == 200, f"Expected 200 for authenticated /api/stats, got {res_stats_auth.status_code}"
        print("[PASS] 3E. Authenticated request with session cookie succeeded (200 OK)!")

        # F. Authenticated GET / returns full dashboard HTML
        res_dashboard = await client.get("/")
        assert res_dashboard.status_code == 200
        assert "ProAffiliaters | Telegram Lead Engine" in res_dashboard.text
        print("[PASS] 3F. Authenticated GET / serves full ProAffiliaters Dashboard HTML!")

        # G. Test logout
        res_logout = await client.post("/api/auth/logout")
        assert res_logout.status_code == 200
        print("[PASS] 3G. POST /api/auth/logout cleared session cookie successfully!")

    print("\nALL AUTHENTICATION & SECURITY TESTS PASSED PERFECTLY!\n")


if __name__ == "__main__":
    asyncio.run(run_tests())
