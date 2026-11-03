import re
from typing import List, Set
from src.models import Lead, LeadType, LeadSourceInfo, LeadStatus

# Regex to detect @usernames and t.me/ links.
# Uses negative lookbehind (?<![a-zA-Z0-9._%+-]) so email addresses like user@gmail.com do NOT match.
HANDLE_REGEX = re.compile(
    r'(?:(?<![a-zA-Z0-9._%+-])@|(?:https?://)?(?:t\.me|telegram\.me)/)([a-zA-Z0-9_]{4,32})',
    re.IGNORECASE
)

# Ignored routes, email domains, and system paths that are not user or bot usernames
IGNORED_SYSTEM_NAMES = {
    "joinchat", "addstickers", "share", "iv", "proxy", "socks",
    "setlanguage", "invoice", "telegram", "channel", "login",
    "contact", "terms", "privacy", "support", "help",
    "gmail", "yahoo", "hotmail", "outlook", "icloud", "protonmail", "mail",
    "admin", "administrator"
}


def extract_leads_from_text(
    bio_text: str,
    source_name: str = "",
    source_invite_link: str = "",
    source_id: str = ""
) -> List[Lead]:
    """
    Parses channel bio/description text, extracts valid Telegram usernames,
    classifies each as either 'user' (human manager) or 'bot' (ends with 'bot'),
    and returns a list of Lead objects.
    """
    if not bio_text:
        return []

    leads: List[Lead] = []
    seen_in_this_bio: Set[str] = set()

    # Find all username matches
    matches = HANDLE_REGEX.findall(bio_text)

    source_info = LeadSourceInfo(
        source_name=source_name,
        source_invite_link=source_invite_link,
        platform="telegram",
        source_id=source_id
    )

    for match in matches:
        raw_username = match.strip()
        clean_username = raw_username.lower()

        # Filter out system paths, numbers-only, or invalid length
        if clean_username in IGNORED_SYSTEM_NAMES:
            continue
        if len(clean_username) < 4 or len(clean_username) > 32:
            continue
        if clean_username.isdigit():
            continue
        if clean_username in seen_in_this_bio:
            continue

        seen_in_this_bio.add(clean_username)

        # Classification: Bot vs Human User
        # Telegram convention: bot usernames always end with 'bot' (e.g. DealsBot, deal_bot)
        if clean_username.endswith("bot"):
            lead_type = LeadType.BOT
        else:
            lead_type = LeadType.USER

        lead = Lead(
            username=raw_username,
            username_clean=clean_username,
            lead_type=lead_type,
            extracted_from=source_info,
            bio_context=bio_text[:500],  # Keep first 500 chars for context
            status=LeadStatus.NEW
        )
        leads.append(lead)

    return leads
