"""Cloud storage service (Cloudflare R2 / any S3-compatible bucket) — file
uploads for Sahu CRM.

Originally called Emergent's hosted object storage (only worked inside the
Emergent platform). Since we're deploying on managed platforms (Render /
Railway) with ephemeral disks — not a VPS with persistent storage — files
are stored in Cloudflare R2 instead (S3-compatible, free 10GB, no egress
fee). Same function signatures as before (init_storage, put_object,
get_object, storage_status) so server.py needed zero changes.

Setup (5 min, free): https://dash.cloudflare.com -> R2 -> Create bucket
-> Manage API tokens -> create an S3 API token. Fill the 4 env vars below.

If you later move to a VPS and want local disk instead, swap this file's
three functions for filesystem read/write and keep the same signatures.
"""
from __future__ import annotations

import logging
import mimetypes
import os
from typing import Optional

import boto3
from botocore.client import Config

logger = logging.getLogger("storage")

APP_NAME = "astrologer-sahu-crm"

_client = None
_ready: bool = False
_init_error: Optional[str] = None

BUCKET = os.environ.get("R2_BUCKET_NAME", "")


def _get_client():
    global _client
    if _client is None:
        account_id = os.environ.get("R2_ACCOUNT_ID", "")
        _client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID", ""),
            aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", ""),
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
    return _client


def init_storage() -> Optional[str]:
    """Called once at FastAPI startup. Verifies the bucket is reachable."""
    global _ready, _init_error
    try:
        if not (BUCKET and os.environ.get("R2_ACCOUNT_ID") and os.environ.get("R2_ACCESS_KEY_ID")):
            raise ValueError("R2 storage env vars not set (R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_BUCKET_NAME)")
        client = _get_client()
        client.head_bucket(Bucket=BUCKET)
        _ready = True
        _init_error = None
        logger.info(f"R2 storage ready (bucket: {BUCKET})")
        return "r2"
    except Exception as e:
        _ready = False
        _init_error = f"{type(e).__name__}: {e}"
        logger.warning(f"Storage not ready: {_init_error}")
        return None


def put_object(path: str, data: bytes, content_type: str) -> dict:
    """Upload bytes to R2 under key `path`."""
    if not _ready:
        init_storage()
    client = _get_client()
    client.put_object(Bucket=BUCKET, Key=path, Body=data, ContentType=content_type or "application/octet-stream")
    return {"path": path, "size": len(data), "content_type": content_type}


def get_object(path: str) -> tuple[bytes, str]:
    """Download bytes from R2. Returns (content, content_type)."""
    client = _get_client()
    try:
        obj = client.get_object(Bucket=BUCKET, Key=path)
    except client.exceptions.NoSuchKey:
        raise FileNotFoundError(f"Object not found: {path}")
    data = obj["Body"].read()
    content_type = obj.get("ContentType") or mimetypes.guess_type(path)[0] or "application/octet-stream"
    return data, content_type


def storage_status() -> dict:
    return {"ready": _ready, "error": _init_error, "app": APP_NAME, "backend": "cloudflare-r2", "bucket": BUCKET}
