"""Pydantic models for Astrologer Sahu CRM."""
from datetime import datetime, timezone
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, EmailStr, ConfigDict
import uuid


def _uuid() -> str:
    return str(uuid.uuid4())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- USER ----------
class User(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_uuid)
    email: EmailStr
    name: str
    password_hash: str
    role: str = "owner"
    phone: Optional[str] = None
    avatar: Optional[str] = None
    created_at: str = Field(default_factory=_now_iso)


class UserSignup(BaseModel):
    email: EmailStr
    name: str
    password: str
    phone: Optional[str] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserPublic(BaseModel):
    id: str
    email: EmailStr
    name: str
    role: str
    phone: Optional[str] = None
    avatar: Optional[str] = None


# ---------- LEAD ----------
LeadStage = Literal[
    "new", "contacted", "qualified", "interested", "follow_up",
    "appointment_requested", "appointment_confirmed", "consultation_completed",
    "review_requested", "referral", "lost", "spam", "bargaining"
]
LeadPriority = Literal["hot", "warm", "cold", "vip", "lost"]
LeadSource = Literal[
    "website", "google", "facebook", "instagram", "youtube",
    "justdial", "whatsapp", "phone", "referral", "manual"
]
Service = Literal["astrology", "numerology", "palm_reading", "vastu", "signature_reading"]


class LeadCreate(BaseModel):
    name: str
    phone: str
    whatsapp: Optional[str] = None
    email: Optional[EmailStr] = None
    city: Optional[str] = None
    dob: Optional[str] = None
    birth_time: Optional[str] = None
    birth_place: Optional[str] = None
    gender: Optional[str] = None
    service: Optional[Service] = "astrology"
    problem_category: Optional[str] = None
    source: LeadSource = "manual"
    stage: LeadStage = "new"
    priority: LeadPriority = "warm"
    notes: Optional[str] = None


class LeadUpdate(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    whatsapp: Optional[str] = None
    email: Optional[EmailStr] = None
    city: Optional[str] = None
    dob: Optional[str] = None
    birth_time: Optional[str] = None
    birth_place: Optional[str] = None
    gender: Optional[str] = None
    service: Optional[Service] = None
    problem_category: Optional[str] = None
    source: Optional[LeadSource] = None
    stage: Optional[LeadStage] = None
    priority: Optional[LeadPriority] = None
    notes: Optional[str] = None


class Lead(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_uuid)
    name: str
    phone: str
    whatsapp: Optional[str] = None
    email: Optional[str] = None
    city: Optional[str] = None
    dob: Optional[str] = None
    birth_time: Optional[str] = None
    birth_place: Optional[str] = None
    gender: Optional[str] = None
    service: str = "astrology"
    problem_category: Optional[str] = None
    source: str = "manual"
    stage: str = "new"
    priority: str = "warm"
    score: int = 0
    notes: Optional[str] = None
    files: List[dict] = Field(default_factory=list)  # {type,name,url,uploaded_at}
    payment_status: str = "pending"
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


# ---------- ACTIVITY / TIMELINE ----------
class Activity(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_uuid)
    lead_id: str
    type: str  # note, call, whatsapp, email, stage_change, payment, appointment, followup, ai
    title: str
    body: Optional[str] = None
    meta: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now_iso)


class ActivityCreate(BaseModel):
    lead_id: str
    type: str
    title: str
    body: Optional[str] = None
    meta: dict = Field(default_factory=dict)


# ---------- APPOINTMENT ----------
class AppointmentCreate(BaseModel):
    lead_id: str
    date: str  # YYYY-MM-DD
    slot: str  # HH:MM (24hr)
    mode: str = "online"  # online, offline, phone, whatsapp, google_meet, zoom
    notes: Optional[str] = None


class Appointment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_uuid)
    lead_id: str
    lead_name: str
    date: str
    slot: str
    mode: str
    status: str = "confirmed"  # confirmed, cancelled, completed, rescheduled
    notes: Optional[str] = None
    created_at: str = Field(default_factory=_now_iso)


# ---------- PAYMENT ----------
class PaymentCreate(BaseModel):
    lead_id: str
    amount: int  # in INR (not paise)
    description: Optional[str] = None


class Payment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_uuid)
    lead_id: str
    lead_name: str
    amount: int
    currency: str = "INR"
    razorpay_order_id: Optional[str] = None
    razorpay_payment_id: Optional[str] = None
    razorpay_payment_link_id: Optional[str] = None
    payment_link: Optional[str] = None
    status: str = "created"  # created, pending, paid, failed, refunded
    description: Optional[str] = None
    created_at: str = Field(default_factory=_now_iso)
    paid_at: Optional[str] = None


# ---------- FOLLOW UP ----------
class FollowUp(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_uuid)
    lead_id: str
    lead_name: str
    scheduled_at: str  # ISO timestamp
    stage: str  # immediate, 30m, 4h, 1d, 3d, 7d, 15d
    message: str
    channel: str = "whatsapp"  # whatsapp, sms, call
    status: str = "pending"  # pending, sent, cancelled
    sent_at: Optional[str] = None
    created_at: str = Field(default_factory=_now_iso)


# ---------- AI CHAT ----------
class AIChatMessage(BaseModel):
    session_id: str
    message: str


class AIChatEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=_uuid)
    session_id: str
    role: str  # user, assistant
    content: str
    created_at: str = Field(default_factory=_now_iso)
    escalated: bool = False
