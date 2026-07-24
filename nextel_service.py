"""Nextel MBG outbound sync — pushes CRM leads to the Nextel Audience endpoint.

Nextel accepts `application/x-www-form-urlencoded` at:
  https://api.nextel.io/WEBHOOK_V1/Audience/set/{token}

The token in the URL is the auth (per user's setup — no bearer header needed).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("nextel")

_STATUS = {
    "configured": False,
    "last_push_ok": None,
    "last_push_at": None,
    "last_error": None,
    "total_pushed": 0,
    "total_failed": 0,
}


def _endpoint() -> Optional[str]:
    url = os.environ.get("NEXTEL_AUDIENCE_URL", "").strip()
    _STATUS["configured"] = bool(url)
    return url or None


async def push_lead(lead: dict) -> dict:
    """Push a single lead to Nextel. Non-fatal: returns {ok, detail}."""
    url = _endpoint()
    if not url:
        return {"ok": False, "detail": "NEXTEL_AUDIENCE_URL not set"}

    payload = {
        "name": (lead.get("name") or "")[:120],
        "phone": (lead.get("phone") or "").lstrip("+"),  # Nextel expects digits, e.g. 919...
        "email": lead.get("email") or "",
        "city": lead.get("city") or "",
        "source": lead.get("source") or "",
        "stage": lead.get("stage") or "",
        "priority": lead.get("priority") or "",
        "tags": f"crm,{lead.get('service') or ''}".strip(","),
        "note": (lead.get("notes") or "")[:500],
    }
    # Add extra bearer support: if NEXTEL_BEARER_TOKEN is set, send it too — harmless if not used.
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    bearer = os.environ.get("NEXTEL_BEARER_TOKEN", "").strip()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, data=payload, headers=headers)
        ok = 200 <= resp.status_code < 300
        detail = resp.text[:300]
        _STATUS["last_push_at"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        _STATUS["last_push_ok"] = ok
        if ok:
            _STATUS["last_error"] = None
            _STATUS["total_pushed"] += 1
            logger.info("Nextel push OK phone=%s status=%s", payload["phone"], resp.status_code)
        else:
            _STATUS["last_error"] = f"HTTP {resp.status_code}: {detail}"
            _STATUS["total_failed"] += 1
            logger.warning("Nextel push failed phone=%s status=%s body=%s", payload["phone"], resp.status_code, detail[:100])
        return {"ok": ok, "detail": detail, "status_code": resp.status_code}
    except Exception as e:
        _STATUS["last_push_ok"] = False
        _STATUS["last_error"] = f"{type(e).__name__}: {e}"
        _STATUS["total_failed"] += 1
        logger.exception("Nextel push exception")
        return {"ok": False, "detail": str(e)}


def status() -> dict:
    return {**_STATUS, "configured": bool(_endpoint())}
