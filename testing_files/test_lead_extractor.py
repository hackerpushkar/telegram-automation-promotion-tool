import os
import sys

# Ensure UTF-8 stdout on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.extractor import extract_leads_from_text
from src.models import LeadType


def test_extractor():
    sample_bio = (
        "Welcome to Best Deals India! 🔥\n"
        "For paid promotions & collaborations: contact owner @DealsAdmin or @deal_manager\n"
        "Submit your deals to our automated bot: https://t.me/DealSubmitterBot\n"
        "Check our alert bot @deal_alert_bot\n"
        "Join backup channel t.me/dealsbackup\n"
        "Do not message https://t.me/telegram or support.\n"
    )

    leads = extract_leads_from_text(
        bio_text=sample_bio,
        source_name="Best Deals India",
        source_invite_link="https://t.me/bestdealsindia",
        source_id="test_source_123"
    )

    print(f"\n[Test] Extracted {len(leads)} leads from sample bio:\n")

    for idx, lead in enumerate(leads, 1):
        print(f" {idx}. Username: @{lead.username} (Clean: '{lead.username_clean}')")
        print(f"    Type: {lead.lead_type.value}")
        print(f"    Source: {lead.extracted_from.source_name}")
        print(f"    Context: {lead.bio_context[:60]}...")
        print("-" * 50)

    # Assertions
    usernames = {l.username_clean: l.lead_type for l in leads}
    
    assert "dealsadmin" in usernames, "dealsadmin should be extracted"
    assert usernames["dealsadmin"] == LeadType.USER, "dealsadmin should be classified as USER"
    
    assert "deal_manager" in usernames, "deal_manager should be extracted"
    assert usernames["deal_manager"] == LeadType.USER, "deal_manager should be classified as USER"
    
    assert "dealsubmitterbot" in usernames, "dealsubmitterbot should be extracted"
    assert usernames["dealsubmitterbot"] == LeadType.BOT, "dealsubmitterbot should be classified as BOT"
    
    assert "deal_alert_bot" in usernames, "deal_alert_bot should be extracted"
    assert usernames["deal_alert_bot"] == LeadType.BOT, "deal_alert_bot should be classified as BOT"
    
    assert "telegram" not in usernames, "telegram system name should be ignored"

    print("\n✅ All Lead Extractor assertions passed successfully!")


if __name__ == "__main__":
    test_extractor()
