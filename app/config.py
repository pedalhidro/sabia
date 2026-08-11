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

    # ── Demais canais de cross-posting (todos opcionais; canal sem config
    # aparece como "não configurado" na UI e recusa publicar AO VIVO) ────────

    # WhatsApp — Whapi.Cloud (número real, admin da Comunidade; ver
    # scripts/post_whatsapp.py pro porquê da opção C e o aviso de ToS).
    WHAPI_TOKEN = os.environ.get("WHAPI_TOKEN", "")
    WHAPI_ANNOUNCE_GROUP = os.environ.get("WHAPI_ANNOUNCE_GROUP", "")

    # Telegram — Bot API oficial; o bot precisa ser admin do canal/grupo.
    # TELEGRAM_CHAT_ID: "@nomedocanal" ou id numérico (-100…).
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

    # Mastodon — token de app com escopo write (Preferências → Desenvolvimento).
    MASTODON_BASE_URL = os.environ.get("MASTODON_BASE_URL", "")  # ex.: https://ciclo.social
    MASTODON_ACCESS_TOKEN = os.environ.get("MASTODON_ACCESS_TOKEN", "")

    # Reddit — app tipo "script" (grant de senha) em reddit.com/prefs/apps.
    REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
    REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
    REDDIT_USERNAME = os.environ.get("REDDIT_USERNAME", "")
    REDDIT_PASSWORD = os.environ.get("REDDIT_PASSWORD", "")
    REDDIT_SUBREDDIT = os.environ.get("REDDIT_SUBREDDIT", "")

    # E-mail — SMTP simples (lista/boletim). EMAIL_TO: vírgulas separam.
    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
    EMAIL_TO = os.environ.get("EMAIL_TO", "")

    # Agenda — Google Calendar do grupo (a MESMA que o site
    # calendario.pedalhidrografi.co exibe; id tipo "…@group.calendar.google.com").
    # Sem token próprio: a credencial vem do ambiente (ADC) — na Cloud Run é a
    # conta de serviço de runtime; compartilhe a agenda com o e-mail dela com
    # permissão "Fazer alterações em eventos" (deploy.sh lembra qual é).
    GCAL_CALENDAR_ID = os.environ.get("GCAL_CALENDAR_ID", "")
    GCAL_TIMEZONE = os.environ.get("GCAL_TIMEZONE", "America/Sao_Paulo")

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

    @classmethod
    def channels_status(cls) -> dict:
        """Por canal: dá pra publicar AO VIVO? (+ o alvo, pra UI mostrar).
        Um canal não configurado ainda salva rascunho e roda em DRY_RUN."""
        return {
            "instagram": {"configured": bool(cls.IG_ACCESS_TOKEN),
                          "target": f"@{cls.IG_USER_ID}" if not str(cls.IG_USER_ID).isdigit() else "conta do Instagram"},
            # Reel usa a mesma conta/token do Instagram — só muda a mídia.
            "reel": {"configured": bool(cls.IG_ACCESS_TOKEN),
                     "target": f"@{cls.IG_USER_ID}" if not str(cls.IG_USER_ID).isdigit() else "conta do Instagram"},
            "whatsapp": {"configured": bool(cls.WHAPI_TOKEN and cls.WHAPI_ANNOUNCE_GROUP),
                         "target": cls.WHAPI_ANNOUNCE_GROUP},
            "telegram": {"configured": bool(cls.TELEGRAM_BOT_TOKEN and cls.TELEGRAM_CHAT_ID),
                         "target": cls.TELEGRAM_CHAT_ID},
            "mastodon": {"configured": bool(cls.MASTODON_BASE_URL and cls.MASTODON_ACCESS_TOKEN),
                         "target": cls.MASTODON_BASE_URL},
            "reddit": {"configured": bool(cls.REDDIT_CLIENT_ID and cls.REDDIT_CLIENT_SECRET
                                          and cls.REDDIT_USERNAME and cls.REDDIT_PASSWORD
                                          and cls.REDDIT_SUBREDDIT),
                       "target": f"r/{cls.REDDIT_SUBREDDIT}" if cls.REDDIT_SUBREDDIT else ""},
            "email": {"configured": bool(cls.SMTP_HOST and cls.EMAIL_FROM and cls.EMAIL_TO),
                      "target": cls.EMAIL_TO},
            "gcal": {"configured": bool(cls.GCAL_CALENDAR_ID),
                     "target": cls.GCAL_CALENDAR_ID},
        }
