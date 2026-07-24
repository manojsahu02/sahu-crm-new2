"""Google Sheets → Sahu CRM lead sync.

The Nextel MBG WhatsApp platform auto-writes every incoming lead to a Google
Sheet. That sheet is published as CSV; we poll it every N seconds, upsert new
leads (deduplicated by phone), and mark them as `source=whatsapp` so they
appear alongside all other CRM leads.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("sheets-sync")

# In-memory status shared with the API layer
STATUS: dict = {
    "enabled": False,
    "last_run": None,
    "last_success": None,
    "last_error": None,
    "rows_seen": 0,
    "created": 0,
    "skipped": 0,
    "total_synced_leads": 0,
    "running": False,
}


def _normalize_phone(raw: str) -> Optional[str]:
    """Coerce Nextel-style phone (`919669719555`) to E.164 (`+919669719555`)."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if len(digits) == 10:            # bare Indian mobile
        digits = "91" + digits
    if len(digits) < 10:             # obviously invalid
        return None
    return "+" + digits


def _parse_ts(raw: str) -> Optional[str]:
    """Parse '2024-08-14 17:20:15' → ISO string in UTC. Return None on failure."""
    if not raw or not raw.strip():
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(raw.strip(), fmt).replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    return None


async def _fetch_csv(url: str) -> list[list[str]]:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
    reader = csv.reader(io.StringIO(r.text))
    return [row for row in reader]


async def sync_once(db) -> dict:
    """One sync pass. Idempotent: rows with a phone already in DB are skipped."""
    url = os.environ.get("GOOGLE_SHEET_CSV_URL", "").strip()
    STATUS["enabled"] = bool(url)
    if not url:
        STATUS["last_error"] = "GOOGLE_SHEET_CSV_URL not configured"
        return {"ok": False, "error": STATUS["last_error"]}

    if STATUS["running"]:
        return {"ok": False, "error": "Sync already running"}

    STATUS["running"] = True
    STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
    created = skipped = seen = 0

    try:
        rows = await _fetch_csv(url)
        # Skip header (first row) if it looks like one
        if rows and rows[0] and rows[0][0].strip().lower() in ("sender name", "name", "lead name"):
            rows = rows[1:]

        # Preload existing phone set for O(1) dedup
        existing = await db.leads.find(
            {"phone": {"$exists": True}}, {"_id": 0, "phone": 1}
        ).to_list(20000)
        phones = {p["phone"] for p in existing if p.get("phone")}

        new_docs = []
        for row in rows:
            seen += 1
            if not row or len(row) < 2:
                continue
            name = (row[0] or "").strip() or "WhatsApp Lead"
            phone = _normalize_phone(row[1] if len(row) > 1 else "")
            if not phone:
                skipped += 1
                continue
            if phone in phones:
                skipped += 1
                continue
            phones.add(phone)  # avoid dupes within same sheet

            first_seen = _parse_ts(row[2] if len(row) > 2 else "")
            last_seen = _parse_ts(row[3] if len(row) > 3 else "")
            city = (row[4].strip() if len(row) > 4 else "") or None
            action_note = (row[10].strip() if len(row) > 10 else "") or None
            note_parts = [f"Auto-imported from Nextel MBG WhatsApp sheet."]
            if first_seen:
                note_parts.append(f"First seen: {first_seen}")
            if last_seen:
                note_parts.append(f"Last seen: {last_seen}")
            if action_note:
                note_parts.append(f"Sheet note: {action_note}")

            doc = {
                "id": __import__("uuid").uuid4().hex,
                "name": name[:120],
                "phone": phone,
                "whatsapp": phone,
                "email": None,
                "city": city,
                "dob": None, "birth_time": None, "birth_place": None,
                "gender": None,
                "service": "astrology",
                "problem_category": None,
                "source": "whatsapp",
                "stage": "new",
                "priority": "warm",
                "score": 20,
                "notes": " • ".join(note_parts),
                "files": [],
                "payment_status": "pending",
                "created_at": first_seen or last_seen or datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "synced_from": "google_sheet",
            }
            new_docs.append(doc)
            created += 1

        if new_docs:
            # Batch insert for speed
            await db.leads.insert_many(new_docs, ordered=False)
            # Small activity entry per import (one aggregate — not per-lead — to avoid noise)
            await db.activities.insert_one({
                "id": __import__("uuid").uuid4().hex,
                "lead_id": "__system__",
                "type": "sync",
                "title": f"Google Sheets sync: {created} new leads imported",
                "body": f"Rows scanned: {seen}, skipped (dupes/invalid): {skipped}",
                "meta": {"created": created, "skipped": skipped, "seen": seen},
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

        STATUS["last_success"] = datetime.now(timezone.utc).isoformat()
        STATUS["last_error"] = None
        STATUS["rows_seen"] = seen
        STATUS["created"] = created
        STATUS["skipped"] = skipped
        STATUS["total_synced_leads"] = await db.leads.count_documents({"synced_from": "google_sheet"})
        logger.info("Sheet sync OK — seen=%s created=%s skipped=%s", seen, created, skipped)
        return {"ok": True, "seen": seen, "created": created, "skipped": skipped}
    except Exception as e:
        logger.exception("Sheet sync failed")
        STATUS["last_error"] = f"{type(e).__name__}: {e}"
        return {"ok": False, "error": STATUS["last_error"]}
    finally:
        STATUS["running"] = False


async def scheduler_loop(db):
    """Background task — invoked from FastAPI startup."""
    interval = int(os.environ.get("SHEETS_SYNC_INTERVAL_SECONDS", "300"))
    logger.info("Sheets sync scheduler starting — every %ss", interval)
    # Small initial delay so app has time to fully start
    await asyncio.sleep(5)
    while True:
        try:
            await sync_once(db)
        except Exception:  # never let the loop die
            logger.exception("scheduler_loop caught unexpected error")
        await asyncio.sleep(interval)
