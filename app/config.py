"""Environment-driven config. Works locally (filesystem, dry-run) and on
Cloud Run (GCS, live publishing) with no code changes — only env vars differ.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip("'\""))


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Config:
    # Instagram Graph API (Instagram Login → graph.instagram.com, token "IG...")
    IG_ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
    IG_USER_ID = os.environ.get("IG_USER_ID", "me")  # "me" works with IG-login tokens

    # OPTIONAL separate token for DELETE (needs instagram_manage_contents). May be
    # a different type (e.g. Facebook-Login EAA...) than the publish token; the
    # client routes it to the matching host automatically. Falls back to
    # IG_ACCESS_TOKEN when unset.
    IG_MANAGE_TOKEN = os.environ.get("IG_MANAGE_TOKEN", "")

    # Storage: GCS bucket when set (Cloud Run), else local ./uploads (testing).
    GCS_BUCKET = os.environ.get("GCS_BUCKET", "")
    LOCAL_UPLOAD_DIR = Path(os.environ.get("LOCAL_UPLOAD_DIR", REPO_ROOT / "app" / "uploads"))

    # Where the dataset TTL lives. Local path or gs://bucket/path.ttl
    DATA_TTL = os.environ.get("DATA_TTL", str(REPO_ROOT / "definitions" / "data_manual.ttl"))

    # Dry-run skips the real Instagram calls (default ON unless a bucket is set,
    # since Instagram needs public image URLs that localhost can't provide).
    DRY_RUN = _bool("DRY_RUN", default=not bool(GCS_BUCKET))

    PORT = int(os.environ.get("PORT", "8080"))  # Cloud Run injects PORT

    # Shared password (HTTP Basic). When set, every request needs it — this is
    # what guards the public Cloud Run URL. Empty = no gate (local dev).
    APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

    @classmethod
    def using_gcs(cls) -> bool:
        return bool(cls.GCS_BUCKET)
