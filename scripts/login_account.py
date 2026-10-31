import asyncio
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.account_manager import AccountManager
from src.db import Database


async def main():
    await Database.connect()

    print("==================================================================")
    print("🔑 Telegram Multi-Account Login Wizard (Unlimited Accounts)")
    print("==================================================================")

    # Show existing accounts
    accounts = await Database.get_all_telegram_accounts()
    if accounts:
        print(f"\n📱 Currently Connected Accounts ({len(accounts)}):")
        for acc in accounts:
            print(f"   Slot #{acc.get('assigned_slot')}: {acc.get('phone')} ({acc.get('first_name')}) - Status: {acc.get('status')}")
    else:
        print("\nℹ️ No accounts registered yet. Let's add Account #1!")

    next_slot = await AccountManager.get_next_available_slot()
    print("\n------------------------------------------------------------------")
    phone_input = input("Enter Phone Number (with country code, e.g. +919876543210): ").strip()
    if not phone_input:
        print("❌ Phone number cannot be empty.")
        return

    slot_input = input(f"Enter Slot/Week Number (1 to 5+) [Default: {next_slot}]: ").strip()
    slot = int(slot_input) if slot_input.isdigit() else next_slot

    print(f"\n📡 Requesting Telegram login code for {phone_input} (Assigned to Slot #{slot})...")
    try:
        start_res = await AccountManager.start_phone_login(phone_input, slot)
    except Exception as e:
        print(f"❌ Error sending code: {e}")
        return

    session_token = start_res["session_token"]
    print(f"✅ {start_res['message']}")

    # Prompt for OTP
    code_input = input("\nEnter the 5-digit verification code you received: ").strip()
    if not code_input:
        print("❌ Code cannot be empty.")
        return

    print("🔐 Submitting verification code...")
    try:
        complete_res = await AccountManager.complete_phone_login(session_token, code=code_input)

        if complete_res.get("status") == "requires_password":
            print("🔒 Two-Factor Authentication (2FA) is enabled on this account.")
            pwd_input = input("Enter your 2FA Cloud Password: ").strip()
            complete_res = await AccountManager.complete_phone_login(session_token, password=pwd_input)

        if complete_res.get("status") == "success":
            print("\n==================================================================")
            print("🎉 Account Connected Successfully!")
            acc = complete_res.get("account", {})
            print(f"   Phone:    {acc.get('phone')}")
            print(f"   Name:     {acc.get('name')}")
            print(f"   Username: @{acc.get('username') or 'None'}")
            print(f"   Slot:     Slot #{acc.get('slot')} (Week {acc.get('slot')})")
            print("==================================================================")
        else:
            print(f"❌ Login failed: {complete_res.get('message')}")
    except Exception as e:
        print(f"❌ Login error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
