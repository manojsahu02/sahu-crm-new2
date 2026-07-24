"""Follow-up automation service — schedules follow-ups. Phase 1 logs only."""
from datetime import datetime, timedelta, timezone
from typing import List

FOLLOWUP_SCHEDULE = [
    ("immediate", timedelta(seconds=0),
     "Namaste 🙏 Thank you for reaching out to Astrologer Manoj Sahu. Guruji offers premium consultation (₹5000) with 25+ years of experience. May I know your birth details to check availability?"),
    ("30m", timedelta(minutes=30),
     "Just checking in — would you like me to reserve a slot with Guruji today? Slots are limited between 10 AM–8 PM."),
    ("4h", timedelta(hours=4),
     "Guruji has helped 10,000+ clients resolve marriage, career and vastu issues. Shall I share a payment link so we can lock your consultation?"),
    ("1d", timedelta(days=1),
     "Reminder: Guruji has a few slots open tomorrow. A 45-minute consultation for ₹5000 can offer clarity you've been searching for. Want me to book?"),
    ("3d", timedelta(days=3),
     "Just a gentle nudge — many people book us for urgent life decisions. If timing is a concern, we can also schedule 7-10 days ahead."),
    ("7d", timedelta(days=7),
     "It's been a week since we spoke. If you're still exploring, Guruji is happy to answer any final question before your consultation."),
    ("15d", timedelta(days=15),
     "Last check-in — if the timing isn't right now, feel free to reach out whenever you're ready. Blessings 🙏"),
]


def build_followups_for_lead(lead: dict) -> List[dict]:
    """Generate all 7 follow-up entries for a new lead."""
    now = datetime.now(timezone.utc)
    items = []
    for stage, delta, message in FOLLOWUP_SCHEDULE:
        scheduled_at = (now + delta).isoformat()
        items.append({
            "lead_id": lead["id"],
            "lead_name": lead["name"],
            "scheduled_at": scheduled_at,
            "stage": stage,
            "message": message,
            "channel": "whatsapp",
            "status": "pending",
        })
    return items
