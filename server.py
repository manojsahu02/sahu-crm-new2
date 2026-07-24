"""Astrologer Sahu CRM — FastAPI backend."""
from fastapi import FastAPI, APIRouter, Depends, HTTPException, Request, UploadFile, File, Form, Header, Query
from fastapi.responses import JSONResponse, Response
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import asyncio
import os
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from collections import Counter

import razorpay

from models import (
    User, UserSignup, UserLogin, UserPublic,
    Lead, LeadCreate, LeadUpdate,
    Activity, ActivityCreate,
    Appointment, AppointmentCreate,
    Payment, PaymentCreate,
    FollowUp,
    AIChatMessage, AIChatEntry,
)
from auth import hash_password, verify_password, create_token, get_current_user
from ai_service import ask_ai, score_lead
from followup_service import build_followups_for_lead
from sheets_sync import sync_once as sheets_sync_once, scheduler_loop as sheets_scheduler_loop, STATUS as SHEETS_STATUS
import storage_service
import nextel_service
import uuid

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

app = FastAPI(title="Astrologer Sahu CRM API")
api = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("astrologer-crm")


# Razorpay client — lazy init when keys present
def get_razorpay():
    kid = os.environ.get("RAZORPAY_KEY_ID", "")
    ksec = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not kid or not ksec:
        return None
    return razorpay.Client(auth=(kid, ksec))


# ==================== HEALTH ====================
@api.get("/")
async def root():
    return {"service": "Astrologer Sahu CRM", "status": "ok"}


# ==================== AUTH ====================
@api.post("/auth/signup")
async def signup(payload: UserSignup):
    existing = await db.users.find_one({"email": payload.email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    user = User(
        email=payload.email,
        name=payload.name,
        password_hash=hash_password(payload.password),
        phone=payload.phone,
    )
    await db.users.insert_one(user.model_dump())
    token = create_token(user.id, user.email)
    return {"token": token, "user": UserPublic(**user.model_dump()).model_dump()}


@api.post("/auth/login")
async def login(payload: UserLogin):
    doc = await db.users.find_one({"email": payload.email}, {"_id": 0})
    if not doc or not verify_password(payload.password, doc["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(doc["id"], doc["email"])
    return {"token": token, "user": UserPublic(**doc).model_dump()}


@api.get("/auth/me")
async def me(user=Depends(get_current_user)):
    doc = await db.users.find_one({"id": user["id"]}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="User not found")
    return UserPublic(**doc)


@api.post("/auth/forgot")
async def forgot_password(email: str):
    # Phase 1: just acknowledge — real reset would send email
    return {"message": "If an account exists, a reset link has been generated (Phase 1: logged only).",
            "reset_token": "phase1-placeholder"}


# ==================== LEADS ====================
@api.post("/leads", response_model=Lead)
async def create_lead(payload: LeadCreate, user=Depends(get_current_user)):
    lead = Lead(**payload.model_dump())
    priority, score = await score_lead(lead.model_dump())
    lead.priority = priority
    lead.score = score
    await db.leads.insert_one(lead.model_dump())

    # activity
    await db.activities.insert_one(Activity(
        lead_id=lead.id, type="stage_change",
        title="Lead created", body=f"Source: {lead.source}, Stage: {lead.stage}"
    ).model_dump())

    # generate follow-ups
    followups = build_followups_for_lead(lead.model_dump())
    if followups:
        await db.followups.insert_many([FollowUp(**f).model_dump() for f in followups])

    # Push to Nextel audience (non-blocking best-effort)
    asyncio.create_task(nextel_service.push_lead(lead.model_dump()))
    return lead


@api.get("/leads", response_model=List[Lead])
async def list_leads(
    stage: Optional[str] = None,
    priority: Optional[str] = None,
    source: Optional[str] = None,
    q: Optional[str] = None,
    user=Depends(get_current_user),
):
    query = {}
    if stage:
        query["stage"] = stage
    if priority:
        query["priority"] = priority
    if source:
        query["source"] = source
    if q:
        query["$or"] = [
            {"name": {"$regex": q, "$options": "i"}},
            {"phone": {"$regex": q, "$options": "i"}},
            {"city": {"$regex": q, "$options": "i"}},
        ]
    docs = await db.leads.find(query, {"_id": 0}).sort("created_at", -1).to_list(1000)
    return docs


@api.get("/leads/{lead_id}", response_model=Lead)
async def get_lead(lead_id: str, user=Depends(get_current_user)):
    doc = await db.leads.find_one({"id": lead_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Lead not found")
    return doc


@api.patch("/leads/{lead_id}", response_model=Lead)
async def update_lead(lead_id: str, payload: LeadUpdate, user=Depends(get_current_user)):
    doc = await db.leads.find_one({"id": lead_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Lead not found")
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()

    stage_before = doc.get("stage")
    doc.update(updates)

    # Re-score if key fields changed
    if any(k in updates for k in ("stage", "source", "notes", "payment_status")):
        priority, score = await score_lead(doc)
        doc["priority"] = priority
        doc["score"] = score

    await db.leads.update_one({"id": lead_id}, {"$set": {k: doc[k] for k in doc if k != "id"}})

    # Stage change activity
    if "stage" in updates and updates["stage"] != stage_before:
        await db.activities.insert_one(Activity(
            lead_id=lead_id, type="stage_change",
            title=f"Stage: {stage_before} → {updates['stage']}"
        ).model_dump())
        # cancel pending followups if stage is terminal
        if updates["stage"] in ("appointment_confirmed", "consultation_completed", "lost", "spam"):
            await db.followups.update_many(
                {"lead_id": lead_id, "status": "pending"},
                {"$set": {"status": "cancelled"}}
            )
    return doc


@api.delete("/leads/{lead_id}")
async def delete_lead(lead_id: str, user=Depends(get_current_user)):
    r = await db.leads.delete_one({"id": lead_id})
    await db.activities.delete_many({"lead_id": lead_id})
    await db.followups.delete_many({"lead_id": lead_id})
    return {"deleted": r.deleted_count}


# ==================== ACTIVITIES ====================
@api.post("/activities", response_model=Activity)
async def add_activity(payload: ActivityCreate, user=Depends(get_current_user)):
    act = Activity(**payload.model_dump())
    await db.activities.insert_one(act.model_dump())
    return act


@api.get("/activities/lead/{lead_id}", response_model=List[Activity])
async def lead_activities(lead_id: str, user=Depends(get_current_user)):
    docs = await db.activities.find({"lead_id": lead_id}, {"_id": 0}).sort("created_at", -1).to_list(500)
    return docs


@api.get("/activities/recent", response_model=List[Activity])
async def recent_activities(limit: int = 20, user=Depends(get_current_user)):
    docs = await db.activities.find({}, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return docs


# ==================== APPOINTMENTS ====================
@api.post("/appointments", response_model=Appointment)
async def create_appointment(payload: AppointmentCreate, user=Depends(get_current_user)):
    # slot validation 10-20
    hour = int(payload.slot.split(":")[0])
    if hour < 10 or hour >= 20:
        raise HTTPException(status_code=400, detail="Slot must be between 10:00 and 19:59")
    # double booking
    clash = await db.appointments.find_one({
        "date": payload.date, "slot": payload.slot, "status": {"$ne": "cancelled"}
    })
    if clash:
        raise HTTPException(status_code=409, detail="Slot already booked")
    lead = await db.leads.find_one({"id": payload.lead_id}, {"_id": 0})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    appt = Appointment(**payload.model_dump(), lead_name=lead["name"])
    await db.appointments.insert_one(appt.model_dump())
    await db.leads.update_one({"id": payload.lead_id}, {"$set": {"stage": "appointment_confirmed"}})
    await db.activities.insert_one(Activity(
        lead_id=payload.lead_id, type="appointment",
        title=f"Appointment booked — {payload.date} {payload.slot}",
        body=f"Mode: {payload.mode}"
    ).model_dump())
    await db.followups.update_many(
        {"lead_id": payload.lead_id, "status": "pending"},
        {"$set": {"status": "cancelled"}}
    )
    return appt


@api.get("/appointments", response_model=List[Appointment])
async def list_appointments(date: Optional[str] = None, user=Depends(get_current_user)):
    q = {"date": date} if date else {}
    docs = await db.appointments.find(q, {"_id": 0}).sort([("date", 1), ("slot", 1)]).to_list(500)
    return docs


@api.patch("/appointments/{appt_id}")
async def update_appointment(appt_id: str, status: str, user=Depends(get_current_user)):
    if status not in ("confirmed", "cancelled", "completed", "rescheduled"):
        raise HTTPException(status_code=400, detail="Invalid status")
    r = await db.appointments.update_one({"id": appt_id}, {"$set": {"status": status}})
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"updated": True}


@api.get("/appointments/slots/{date}")
async def available_slots(date: str, user=Depends(get_current_user)):
    booked = await db.appointments.find(
        {"date": date, "status": {"$ne": "cancelled"}}, {"_id": 0, "slot": 1}
    ).to_list(200)
    taken = {b["slot"] for b in booked}
    all_slots = [f"{h:02d}:00" for h in range(10, 20)] + [f"{h:02d}:30" for h in range(10, 20)]
    all_slots.sort()
    return {"date": date, "slots": [{"time": s, "available": s not in taken} for s in all_slots]}


# ==================== PAYMENTS ====================
@api.post("/payments/create", response_model=Payment)
async def create_payment(payload: PaymentCreate, user=Depends(get_current_user)):
    lead = await db.leads.find_one({"id": payload.lead_id}, {"_id": 0})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    pay = Payment(
        lead_id=payload.lead_id,
        lead_name=lead["name"],
        amount=payload.amount,
        description=payload.description or f"Consultation with Astrologer Manoj Sahu",
    )
    rzp = get_razorpay()
    if rzp:
        try:
            link = rzp.payment_link.create({
                "amount": payload.amount * 100,
                "currency": "INR",
                "accept_partial": False,
                "description": pay.description,
                "customer": {
                    "name": lead["name"],
                    "contact": lead.get("phone", ""),
                    "email": lead.get("email") or "noemail@example.com",
                },
                "notify": {"sms": True, "email": bool(lead.get("email"))},
                "reminder_enable": True,
                "notes": {"lead_id": lead["id"]},
            })
            pay.razorpay_payment_link_id = link.get("id")
            pay.payment_link = link.get("short_url") or link.get("url")
            pay.status = "pending"
        except Exception as e:
            logger.warning(f"Razorpay error: {e}")
            pay.payment_link = f"https://pay.example.com/placeholder/{pay.id}"
            pay.status = "created"
    else:
        pay.payment_link = f"https://pay.example.com/placeholder/{pay.id}"
        pay.status = "created"

    await db.payments.insert_one(pay.model_dump())
    await db.activities.insert_one(Activity(
        lead_id=payload.lead_id, type="payment",
        title=f"Payment link generated — ₹{payload.amount}",
        body=pay.payment_link or "",
    ).model_dump())
    return pay


@api.get("/payments", response_model=List[Payment])
async def list_payments(user=Depends(get_current_user)):
    docs = await db.payments.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
    return docs


@api.post("/payments/{payment_id}/mark-paid")
async def mark_paid(payment_id: str, user=Depends(get_current_user)):
    """Manual mark-as-paid for phase 1 without live Razorpay keys."""
    now = datetime.now(timezone.utc).isoformat()
    r = await db.payments.update_one(
        {"id": payment_id}, {"$set": {"status": "paid", "paid_at": now}}
    )
    if r.matched_count == 0:
        raise HTTPException(status_code=404, detail="Payment not found")
    pay = await db.payments.find_one({"id": payment_id}, {"_id": 0})
    await db.leads.update_one({"id": pay["lead_id"]}, {"$set": {"payment_status": "paid"}})
    await db.activities.insert_one(Activity(
        lead_id=pay["lead_id"], type="payment",
        title=f"Payment received — ₹{pay['amount']}",
    ).model_dump())
    return {"updated": True}


@api.post("/payments/webhook")
async def razorpay_webhook(request: Request):
    """Razorpay webhook — verifies signature (if secret set), marks payment paid,
    updates the linked lead's payment_status, and creates an activity."""
    payload = await request.body()
    try:
        body = await request.json()
    except Exception:
        body = {}
    secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()
    signature = request.headers.get("X-Razorpay-Signature", "")
    verified = False
    if secret:
        rzp = get_razorpay()
        if rzp:
            try:
                rzp.utility.verify_webhook_signature(payload.decode(), signature, secret)
                verified = True
            except Exception as e:
                logger.warning(f"Webhook signature verify failed: {e}")
                raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event = body.get("event", "")
    p = body.get("payload", {}) or {}
    entity = (p.get("payment", {}) or {}).get("entity", {}) or \
             (p.get("payment_link", {}) or {}).get("entity", {})

    logger.info("Razorpay webhook received event=%s verified=%s", event, verified)

    paid_events = {"payment.captured", "payment_link.paid", "order.paid"}
    if event not in paid_events or not entity:
        return {"ok": True, "event": event, "ignored": True}

    razorpay_payment_id = entity.get("id")
    order_id = entity.get("order_id")
    link_id = entity.get("payment_link_id") or (entity.get("id") if event == "payment_link.paid" else None)
    now = datetime.now(timezone.utc).isoformat()

    # Match payment record by known ids
    query = {"$or": [
        {"razorpay_payment_id": razorpay_payment_id},
        {"razorpay_order_id": order_id},
        {"razorpay_payment_link_id": link_id or "__none__"},
    ]}
    pay = await db.payments.find_one(query, {"_id": 0})
    if not pay:
        logger.warning("Webhook payment not matched: link=%s order=%s payment=%s", link_id, order_id, razorpay_payment_id)
        return {"ok": True, "matched": False}

    await db.payments.update_one(
        {"id": pay["id"]},
        {"$set": {
            "status": "paid",
            "paid_at": now,
            "razorpay_payment_id": razorpay_payment_id or pay.get("razorpay_payment_id"),
        }}
    )
    # Update lead
    await db.leads.update_one({"id": pay["lead_id"]}, {"$set": {"payment_status": "paid"}})
    await db.activities.insert_one(Activity(
        lead_id=pay["lead_id"], type="payment",
        title=f"Payment received via Razorpay — ₹{pay['amount']}",
        body=f"Event {event}, verified={verified}",
        meta={"razorpay_payment_id": razorpay_payment_id, "event": event},
    ).model_dump())
    # Mark any pending appointments for this lead as confirmed (best-effort)
    await db.appointments.update_many(
        {"lead_id": pay["lead_id"], "status": "confirmed"},
        {"$set": {"status": "confirmed"}}  # already confirmed — placeholder for future logic
    )
    logger.info("Payment %s marked paid via webhook", pay["id"])
    return {"ok": True, "payment_id": pay["id"], "verified": verified}


# ==================== FOLLOW-UPS ====================
@api.get("/followups", response_model=List[FollowUp])
async def list_followups(status: Optional[str] = None, user=Depends(get_current_user)):
    q = {"status": status} if status else {}
    docs = await db.followups.find(q, {"_id": 0}).sort("scheduled_at", 1).to_list(500)
    return docs


@api.post("/followups/{fid}/send")
async def send_followup(fid: str, user=Depends(get_current_user)):
    doc = await db.followups.find_one({"id": fid}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Follow-up not found")
    now = datetime.now(timezone.utc).isoformat()
    await db.followups.update_one({"id": fid}, {"$set": {"status": "sent", "sent_at": now}})
    await db.activities.insert_one(Activity(
        lead_id=doc["lead_id"], type="followup",
        title=f"Follow-up ({doc['stage']}) sent via {doc['channel']}",
        body=doc["message"],
    ).model_dump())
    return {"sent": True}


@api.post("/followups/{fid}/cancel")
async def cancel_followup(fid: str, user=Depends(get_current_user)):
    await db.followups.update_one({"id": fid}, {"$set": {"status": "cancelled"}})
    return {"cancelled": True}


# ==================== AI CHAT ====================
@api.post("/ai/chat")
async def ai_chat(payload: AIChatMessage, user=Depends(get_current_user)):
    # Store user message
    await db.ai_chats.insert_one(AIChatEntry(
        session_id=payload.session_id, role="user", content=payload.message
    ).model_dump())
    reply, escalated = await ask_ai(payload.session_id, payload.message)
    await db.ai_chats.insert_one(AIChatEntry(
        session_id=payload.session_id, role="assistant", content=reply, escalated=escalated
    ).model_dump())
    return {"reply": reply, "escalated": escalated}


@api.get("/ai/history/{session_id}", response_model=List[AIChatEntry])
async def ai_history(session_id: str, user=Depends(get_current_user)):
    docs = await db.ai_chats.find({"session_id": session_id}, {"_id": 0}).sort("created_at", 1).to_list(500)
    return docs


# ==================== DASHBOARD & ANALYTICS ====================
@api.get("/dashboard/summary")
async def dashboard_summary(user=Depends(get_current_user)):
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).isoformat()

    leads = await db.leads.find({}, {"_id": 0, "id": 1, "stage": 1, "priority": 1, "source": 1, "service": 1, "created_at": 1, "payment_status": 1}).to_list(2000)
    appts = await db.appointments.find({}, {"_id": 0, "id": 1, "date": 1, "status": 1}).to_list(2000)
    payments = await db.payments.find({}, {"_id": 0, "id": 1, "status": 1, "amount": 1}).to_list(2000)
    followups = await db.followups.find({"status": "pending"}, {"_id": 0, "id": 1}).to_list(2000)

    todays_leads = [l for l in leads if l["created_at"] >= today_start]
    new_leads = [l for l in leads if l["stage"] == "new"]
    hot_leads = [l for l in leads if l["priority"] == "hot"]
    todays_appts = [a for a in appts if a["date"] == today and a["status"] != "cancelled"]
    pending_payments = [p for p in payments if p["status"] in ("created", "pending")]
    paid_payments = [p for p in payments if p["status"] == "paid"]

    total_revenue = sum(p["amount"] for p in paid_payments)
    completed = [l for l in leads if l["stage"] in ("consultation_completed", "review_requested", "referral")]
    conv_rate = round((len(completed) / len(leads)) * 100, 1) if leads else 0.0

    # Top lead sources
    source_counter = Counter(l["source"] for l in leads)
    top_sources = [{"source": s, "count": c} for s, c in source_counter.most_common(6)]

    # Service performance
    svc_counter = Counter(l.get("service", "astrology") for l in leads)
    svc_perf = [{"service": s, "count": c} for s, c in svc_counter.most_common()]

    # Priority section — "Today's Priority"
    priority = {
        "hot_leads_to_call": len([l for l in hot_leads if l["stage"] not in ("appointment_confirmed", "consultation_completed", "lost")]),
        "appointments_to_confirm": len([a for a in appts if a["status"] == "confirmed" and a["date"] == today]),
        "payments_to_collect": len(pending_payments),
        "whatsapp_leads_to_reply": len([l for l in leads if l["source"] == "whatsapp" and l["stage"] in ("new", "contacted")]),
        "reviews_to_request": len([l for l in leads if l["stage"] == "consultation_completed"]),
    }

    return {
        "todays_leads": len(todays_leads),
        "new_leads": len(new_leads),
        "hot_leads": len(hot_leads),
        "todays_appointments": len(todays_appts),
        "pending_followups": len(followups),
        "pending_payments": len(pending_payments),
        "revenue": total_revenue,
        "conversion_rate": conv_rate,
        "top_sources": top_sources,
        "service_performance": svc_perf,
        "todays_priority": priority,
        "total_leads": len(leads),
    }


@api.get("/analytics/overview")
async def analytics_overview(period: str = "monthly", user=Depends(get_current_user)):
    """period: daily|weekly|monthly|yearly — returns time-series."""
    now = datetime.now(timezone.utc)
    payments = await db.payments.find({"status": "paid"}, {"_id": 0, "id": 1, "amount": 1, "created_at": 1}).to_list(2000)
    leads = await db.leads.find({}, {"_id": 0, "id": 1, "stage": 1, "source": 1, "service": 1, "city": 1, "created_at": 1}).to_list(2000)
    appts = await db.appointments.find({"status": {"$ne": "cancelled"}}, {"_id": 0, "id": 1, "date": 1, "status": 1}).to_list(2000)

    if period == "daily":
        # last 14 days
        buckets = [(now - timedelta(days=i)).date().isoformat() for i in range(13, -1, -1)]
        fmt = lambda dt: dt[:10]
    elif period == "weekly":
        buckets = [(now - timedelta(weeks=i)).strftime("W%V") for i in range(11, -1, -1)]
        fmt = lambda dt: datetime.fromisoformat(dt.replace("Z", "+00:00")).strftime("W%V")
    elif period == "yearly":
        buckets = [str(now.year - i) for i in range(4, -1, -1)]
        fmt = lambda dt: dt[:4]
    else:  # monthly
        buckets = [((now.replace(day=1) - timedelta(days=30 * i))).strftime("%Y-%m") for i in range(11, -1, -1)]
        fmt = lambda dt: dt[:7]

    series = {b: {"period": b, "revenue": 0, "leads": 0, "appointments": 0, "conversions": 0} for b in buckets}
    for p in payments:
        b = fmt(p["created_at"])
        if b in series:
            series[b]["revenue"] += p["amount"]
    for l in leads:
        b = fmt(l["created_at"])
        if b in series:
            series[b]["leads"] += 1
        if l["stage"] in ("consultation_completed", "review_requested", "referral"):
            if b in series:
                series[b]["conversions"] += 1
    for a in appts:
        # appointment date is separate
        b = a["date"][:len(buckets[0])] if period == "daily" else a["date"][:7]
        if b in series:
            series[b]["appointments"] += 1

    city_counter = Counter(l.get("city") for l in leads if l.get("city"))
    top_cities = [{"city": c, "count": n} for c, n in city_counter.most_common(6)]

    svc_counter = Counter(l.get("service", "astrology") for l in leads)
    top_services = [{"service": s, "count": n} for s, n in svc_counter.most_common()]

    return {
        "period": period,
        "series": list(series.values()),
        "top_cities": top_cities,
        "top_services": top_services,
    }


# ==================== SEED ====================
@api.post("/dev/seed")
async def seed_data(user=Depends(get_current_user)):
    """Populate with realistic demo leads/appointments/payments (idempotent by clearing first)."""
    await db.leads.delete_many({})
    await db.activities.delete_many({})
    await db.appointments.delete_many({})
    await db.payments.delete_many({})
    await db.followups.delete_many({})

    demo_leads = [
        {"name": "Rajesh Kumar", "phone": "+919812345601", "city": "Delhi", "service": "astrology", "source": "google", "stage": "new", "priority": "warm", "notes": "Career blockage — urgent"},
        {"name": "Priya Sharma", "phone": "+919812345602", "city": "Mumbai", "service": "vastu", "source": "instagram", "stage": "interested", "priority": "hot", "notes": "Ready to pay — new home vastu"},
        {"name": "Amit Verma", "phone": "+919812345603", "city": "Bengaluru", "service": "numerology", "source": "referral", "stage": "appointment_requested", "priority": "vip", "notes": "Business name change referral from Anil"},
        {"name": "Sneha Iyer", "phone": "+919812345604", "city": "Chennai", "service": "palm_reading", "source": "youtube", "stage": "contacted", "priority": "warm", "notes": "Marriage timing question"},
        {"name": "Vikram Singh", "phone": "+919812345605", "city": "Jaipur", "service": "astrology", "source": "whatsapp", "stage": "follow_up", "priority": "warm"},
        {"name": "Anita Gupta", "phone": "+919812345606", "city": "Delhi", "service": "signature_reading", "source": "facebook", "stage": "new", "priority": "cold"},
        {"name": "Rohit Malhotra", "phone": "+919812345607", "city": "Gurgaon", "service": "astrology", "source": "justdial", "stage": "bargaining", "priority": "warm", "notes": "Asking for discount"},
        {"name": "Meena Joshi", "phone": "+919812345608", "city": "Pune", "service": "vastu", "source": "website", "stage": "consultation_completed", "priority": "hot", "notes": "Very satisfied — will refer"},
        {"name": "Karan Bhatia", "phone": "+919812345609", "city": "Chandigarh", "service": "astrology", "source": "referral", "stage": "appointment_confirmed", "priority": "vip"},
        {"name": "Sunita Rao", "phone": "+919812345610", "city": "Hyderabad", "service": "numerology", "source": "phone", "stage": "lost", "priority": "lost", "notes": "Went with another astrologer"},
    ]

    for l in demo_leads:
        lead = Lead(**l)
        priority, score = await score_lead(lead.model_dump())
        lead.priority = priority
        lead.score = score
        await db.leads.insert_one(lead.model_dump())
        await db.activities.insert_one(Activity(
            lead_id=lead.id, type="stage_change", title="Lead created (seed)"
        ).model_dump())
        # follow-ups for active leads
        if lead.stage not in ("consultation_completed", "lost", "spam", "appointment_confirmed"):
            for f in build_followups_for_lead(lead.model_dump()):
                await db.followups.insert_one(FollowUp(**f).model_dump())

    # Seed some appointments & payments
    today = datetime.now(timezone.utc).date().isoformat()
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    all_leads = await db.leads.find({}, {"_id": 0}).to_list(50)
    vip = [l for l in all_leads if l["priority"] == "vip"]
    if vip:
        appt = Appointment(lead_id=vip[0]["id"], lead_name=vip[0]["name"],
                           date=tomorrow, slot="11:00", mode="online")
        await db.appointments.insert_one(appt.model_dump())
    completed = [l for l in all_leads if l["stage"] == "consultation_completed"]
    for l in completed:
        pay = Payment(lead_id=l["id"], lead_name=l["name"], amount=5000, status="paid",
                      paid_at=datetime.now(timezone.utc).isoformat(),
                      description="Consultation")
        await db.payments.insert_one(pay.model_dump())

    return {"seeded": True, "leads": len(demo_leads)}


# ==================== FILE UPLOADS (Emergent Object Storage) ====================
ALLOWED_MIME = {
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif",
    "application/pdf",
}
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "10")) * 1024 * 1024
VALID_CATEGORIES = {"palm", "signature", "houseplan", "other"}


@api.post("/leads/{lead_id}/files")
async def upload_lead_file(
    lead_id: str,
    file: UploadFile = File(...),
    category: str = Form("other"),
    user=Depends(get_current_user),
):
    lead = await db.leads.find_one({"id": lead_id}, {"_id": 0})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    if category not in VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"category must be one of {sorted(VALID_CATEGORIES)}")

    content_type = (file.content_type or "").lower()
    if content_type not in ALLOWED_MIME:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {content_type}. Allowed: images + PDF.")

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large. Max {MAX_UPLOAD_BYTES // (1024*1024)} MB.")
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    ext = (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else \
          ("pdf" if content_type == "application/pdf" else "jpg")
    file_id = uuid.uuid4().hex
    path = f"{storage_service.APP_NAME}/leads/{lead_id}/{file_id}.{ext}"

    try:
        result = await asyncio.to_thread(storage_service.put_object, path, data, content_type)
    except Exception as e:
        logger.exception("Storage upload failed")
        raise HTTPException(status_code=502, detail=f"Storage error: {type(e).__name__}")

    record = {
        "id": file_id,
        "lead_id": lead_id,
        "category": category,
        "original_filename": file.filename,
        "content_type": content_type,
        "size": len(data),
        "storage_path": result.get("path", path),
        "is_deleted": False,
        "uploaded_by": user["id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.files.insert_one(record)

    # Append to lead's inline files array for quick display
    inline = {"id": file_id, "category": category, "name": file.filename,
              "content_type": content_type, "size": len(data),
              "uploaded_at": record["created_at"]}
    await db.leads.update_one({"id": lead_id}, {"$push": {"files": inline}})

    await db.activities.insert_one(Activity(
        lead_id=lead_id, type="file",
        title=f"File uploaded — {category}",
        body=file.filename,
        meta={"file_id": file_id, "size": len(data)},
    ).model_dump())

    return {k: v for k, v in record.items() if k != "_id"}


@api.get("/leads/{lead_id}/files")
async def list_lead_files(lead_id: str, user=Depends(get_current_user)):
    docs = await db.files.find({"lead_id": lead_id, "is_deleted": False}, {"_id": 0}).sort("created_at", -1).to_list(200)
    return docs


@api.delete("/files/{file_id}")
async def delete_file(file_id: str, user=Depends(get_current_user)):
    doc = await db.files.find_one({"id": file_id, "is_deleted": False}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="File not found")
    await db.files.update_one({"id": file_id}, {"$set": {"is_deleted": True}})
    await db.leads.update_one({"id": doc["lead_id"]}, {"$pull": {"files": {"id": file_id}}})
    return {"deleted": True}


@api.get("/files/{file_id}/download")
async def download_file(file_id: str, auth: Optional[str] = Query(None), user_dep=None):
    """Support Authorization header OR ?auth=<token> for <img src>."""
    from auth import decode_token  # local import to avoid cycles
    # Manual token check because we want both header and query auth
    request_scope = None
    token = auth
    if not token:
        raise HTTPException(status_code=401, detail="Missing token")
    try:
        decode_token(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    doc = await db.files.find_one({"id": file_id, "is_deleted": False}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        data, ct = await asyncio.to_thread(storage_service.get_object, doc["storage_path"])
    except Exception as e:
        logger.exception("Storage download failed")
        raise HTTPException(status_code=502, detail=f"Storage error: {type(e).__name__}")

    return Response(content=data, media_type=doc.get("content_type") or ct)


# ==================== NEXTEL OUTBOUND ====================
@api.get("/nextel/status")
async def nextel_status(user=Depends(get_current_user)):
    return nextel_service.status()


@api.post("/nextel/push/{lead_id}")
async def nextel_push_one(lead_id: str, user=Depends(get_current_user)):
    lead = await db.leads.find_one({"id": lead_id}, {"_id": 0})
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    result = await nextel_service.push_lead(lead)
    return {**result, "lead_id": lead_id}


@api.post("/nextel/backfill")
async def nextel_backfill(limit: int = 500, user=Depends(get_current_user)):
    """Push up to `limit` leads to Nextel. Returns aggregate result."""
    leads = await db.leads.find({}, {"_id": 0}).sort("created_at", -1).to_list(limit)
    ok = fail = 0
    for l in leads:
        r = await nextel_service.push_lead(l)
        if r.get("ok"):
            ok += 1
        else:
            fail += 1
    return {"attempted": len(leads), "ok": ok, "failed": fail}


# ==================== STORAGE STATUS ====================
@api.get("/storage/status")
async def storage_status(user=Depends(get_current_user)):
    return storage_service.storage_status()


# ==================== GOOGLE SHEETS SYNC ====================
@api.get("/sync/status")
async def sync_status(user=Depends(get_current_user)):
    return SHEETS_STATUS


@api.post("/sync/sheets")
async def sync_sheets_now(user=Depends(get_current_user)):
    """Trigger an immediate sync from the Nextel MBG Google Sheet."""
    result = await sheets_sync_once(db)
    return {**result, "status": SHEETS_STATUS}


# Mount router
app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def start_background_tasks():
    """Init storage + start Google Sheets sync scheduler."""
    try:
        storage_service.init_storage()
    except Exception:
        logger.exception("Storage init raised — will retry on first upload")
    if os.environ.get("GOOGLE_SHEET_CSV_URL", "").strip():
        asyncio.create_task(sheets_scheduler_loop(db))
        logger.info("Google Sheets sync scheduler queued.")


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
