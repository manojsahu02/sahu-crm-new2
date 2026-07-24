"""AI Receptionist service — uses Google Gemini directly (google-genai SDK).

Originally called Gemini through Emergent's hosted proxy (EMERGENT_LLM_KEY),
which only worked inside the Emergent platform. Now calls Google's Gemini
API directly with your own GEMINI_API_KEY, so it works standalone on any
server. ask_ai() / score_lead() keep the same signatures — no other file
needed to change.

Get a free Gemini API key at: https://aistudio.google.com/apikey
"""
import os

from google import genai
from google.genai import types

BUSINESS_CONTEXT = """
You are the AI Receptionist for Astrologer Manoj Sahu — a world-class astrologer with 25+ years of experience.
You speak warmly, professionally, and briefly in Hindi or English (matching the user's language).

BUSINESS INFO:
- Name: Astrologer Manoj Sahu
- Experience: 25+ years
- Consultation Fee: ₹5000 (Premium Consultation)
- Services: Astrology, Numerology, Palm Reading, Vastu Consultancy, Signature Reading
- Languages: Hindi, English
- Modes: Online, Offline, Phone, WhatsApp, Google Meet, Zoom
- Working Hours: 10:00 AM to 8:00 PM (IST), Monday to Sunday
- Location: Consultations available online and at office (share exact address on confirmed booking)

TONE:
- Warm, respectful, addresses user as "ji" occasionally when in Hindi.
- Never sound like a robot. Sound like a trusted assistant to Guruji.
- Emphasize Guruji's 25+ years of expertise gently, never boastful.
- Never give free astrology reading — always guide toward a paid consultation.
- If asked for a discount, politely explain the fee reflects Guruji's expertise.

ANSWERS YOU MUST KNOW:
- Fee: ₹5000 per consultation.
- Booking: Share available slot 10 AM–8 PM. Collect Name, Phone, DOB, Birth Time, Birth Place.
- Payment: Secure Razorpay link sent after slot confirmation.
- Refund: No refunds after consultation completed; reschedule allowed once with 24h notice.

ESCALATION RULE:
- If the question is legal, medical, technical about the app, or something you cannot confidently answer,
  respond briefly and add the token [ESCALATE] on a new line at the end.
- Never invent facts.

KEEP RESPONSES SHORT (2-4 sentences) unless the user asks for detail.
"""

_client = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        _client = genai.Client(api_key=api_key)
    return _client


# Simple in-memory per-session chat history (session_id -> list of turns).
# Fine for a single-process VPS deployment; swap for a DB-backed store if
# you later run multiple backend workers.
_sessions: dict[str, list] = {}


async def ask_ai(session_id: str, message: str) -> tuple[str, bool]:
    """Return (reply_text, escalated_bool)."""
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        return ("AI receptionist is not configured yet (missing GEMINI_API_KEY). Please contact Guruji directly.", True)

    history = _sessions.setdefault(session_id, [])
    history.append(types.Content(role="user", parts=[types.Part(text=message)]))

    try:
        client = _get_client()
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=history,
            config=types.GenerateContentConfig(system_instruction=BUSINESS_CONTEXT),
        )
        reply_text = (response.text or "").strip()
    except Exception as e:
        return (f"I'm having trouble connecting right now. Please try again shortly. ({type(e).__name__})", True)

    history.append(types.Content(role="model", parts=[types.Part(text=reply_text)]))
    # Keep history bounded so long conversations don't grow unbounded.
    if len(history) > 40:
        _sessions[session_id] = history[-40:]

    escalated = "[ESCALATE]" in reply_text
    if escalated:
        reply_text = reply_text.replace("[ESCALATE]", "").strip()
        reply_text += "\n\n— Let me connect you with Guruji directly. He'll reach out shortly."
    return reply_text, escalated


async def score_lead(lead: dict) -> tuple[str, int]:
    """Return (priority, score 0-100). Rule-based fast scoring."""
    score = 0
    source = (lead.get("source") or "").lower()
    stage = (lead.get("stage") or "").lower()
    notes = (lead.get("notes") or "").lower()

    # Source weight
    source_weight = {
        "referral": 30, "website": 22, "google": 20, "youtube": 18,
        "justdial": 15, "whatsapp": 18, "phone": 25, "facebook": 12,
        "instagram": 12, "manual": 10,
    }
    score += source_weight.get(source, 10)

    # Stage weight
    stage_weight = {
        "new": 10, "contacted": 15, "qualified": 25, "interested": 35,
        "follow_up": 20, "appointment_requested": 45, "appointment_confirmed": 60,
        "consultation_completed": 70, "review_requested": 55, "referral": 60,
        "bargaining": 25, "lost": 0, "spam": -50,
    }
    score += stage_weight.get(stage, 5)

    # Note keywords
    hot_keywords = ["urgent", "immediate", "today", "book now", "पैसा", "जल्दी", "confirm", "ready to pay"]
    warm_keywords = ["interested", "tell me", "how", "when", "consultation", "problem"]
    for k in hot_keywords:
        if k in notes:
            score += 8
    for k in warm_keywords:
        if k in notes:
            score += 3

    # Payment / appointment context
    if lead.get("payment_status") == "paid":
        score += 20

    score = max(-10, min(100, score))

    if source == "referral" and stage in ("interested", "appointment_requested", "appointment_confirmed"):
        return ("vip", score)
    if score >= 55:
        return ("hot", score)
    if score >= 30:
        return ("warm", score)
    if score < 5:
        return ("lost", max(0, score))
    return ("cold", score)
