"""Storage abstraction: local filesystem for testing, Google Cloud Storage on
Cloud Run. Both return a PUBLIC https URL for an uploaded image (Instagram's
publishing API requires publicly reachable image URLs) and read/write the
dataset TTL.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

from config import Config


# ── Images ───────────────────────────────────────────────────────────────────
def save_image(data: bytes, filename: str, content_type: str, base_url: str) -> str:
    """Persist an image and return a public URL.

    base_url is the app's own external URL (used only in local mode to build a
    /uploads/<file> link); in GCS mode the bucket's public URL is used.
    """
    if Config.using_gcs():
        return _gcs_save(data, filename, content_type)
    return _local_save(data, filename, base_url)


def _local_save(data: bytes, filename: str, base_url: str) -> str:
    Config.LOCAL_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (Config.LOCAL_UPLOAD_DIR / filename).write_bytes(data)
    return f"{base_url.rstrip('/')}/uploads/{filename}"


def _gcs_save(data: bytes, filename: str, content_type: str) -> str:
    from google.cloud import storage  # lazy import: not needed locally

    client = storage.Client()
    bucket = client.bucket(Config.GCS_BUCKET)
    blob = bucket.blob(f"posts/{filename}")
    blob.upload_from_file(io.BytesIO(data), content_type=content_type)
    # Bucket should be configured for public reads (uniform access + allUsers
    # objectViewer), which is the recommended Cloud setup. This is the public URL.
    return f"https://storage.googleapis.com/{Config.GCS_BUCKET}/posts/{filename}"


# ── Dataset TTL ──────────────────────────────────────────────────────────────
def read_ttl() -> str:
    uri = Config.DATA_TTL
    if uri.startswith("gs://"):
        return _gcs_read_text(uri)
    p = Path(uri)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def write_ttl(text: str) -> None:
    uri = Config.DATA_TTL
    if uri.startswith("gs://"):
        _gcs_write_text(uri, text)
    else:
        p = Path(uri)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _split_gs(uri: str) -> tuple[str, str]:
    rest = uri[len("gs://"):]
    bucket, _, key = rest.partition("/")
    return bucket, key


def _gcs_read_text(uri: str) -> str:
    from google.cloud import storage

    bucket_name, key = _split_gs(uri)
    blob = storage.Client().bucket(bucket_name).blob(key)
    return blob.download_as_text() if blob.exists() else ""


def _gcs_write_text(uri: str, text: str) -> None:
    from google.cloud import storage

    bucket_name, key = _split_gs(uri)
    storage.Client().bucket(bucket_name).blob(key).upload_from_string(
        text, content_type="text/turtle"
    )
