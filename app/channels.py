"""Publicação multi-canal dos anúncios (cross-posting).

Cada canal tem uma função publish_*() que honra Config.DRY_RUN (não toca a
rede, devolve um resultado falso) e levanta PublishError com mensagem clara
quando o canal não está configurado ou a API recusa. Tudo em stdlib (urllib,
smtplib) — sem dependências novas. O Instagram continua em instagram.py (fluxo
próprio de containers da Graph API); aqui ficam os canais simples.

Canais e provedores:
  whatsapp  Whapi.Cloud — número real, admin da Comunidade (mesma opção "C" do
            scripts/post_whatsapp.py; ToS gray area, volume baixo)
  telegram  Bot API oficial (bot admin do canal/grupo)
  mastodon  API do Mastodon: upload das mídias + status
  reddit    API oficial (app "script", grant de senha) — post de texto; as
            imagens entram como links no corpo (upload nativo exige lease S3)
  email     SMTP com as imagens anexadas
  gcal      Google Calendar API v3 — cria o evento na agenda do grupo (a mesma
            que calendario.pedalhidrografi.co exibe). Credencial via ADC
            (google-auth, já presente pelo google-cloud-storage): na Cloud Run
            é a conta de serviço de runtime — compartilhe a agenda com ela.
"""
from __future__ import annotations

import base64
import json
import secrets
import smtplib
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from typing import List, Optional, Sequence, Tuple

from config import Config
from instagram import PublishError, publish_reel

WHAPI_BASE = "https://gate.whapi.cloud"

# Blob de imagem como o main.py entrega: (filename, bytes, content_type).
Blob = Tuple[str, bytes, str]

# Env vars por canal — pras mensagens de erro/UI dizerem o que falta.
REQUIRED_ENV = {
    "reel": "IG_ACCESS_TOKEN",
    "whatsapp": "WHAPI_TOKEN, WHAPI_ANNOUNCE_GROUP",
    "telegram": "TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID",
    "mastodon": "MASTODON_BASE_URL, MASTODON_ACCESS_TOKEN",
    "reddit": "REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD, REDDIT_SUBREDDIT",
    "email": "SMTP_HOST, EMAIL_FROM, EMAIL_TO",
    "gcal": "GCAL_CALENDAR_ID",
}


def publish(channel: str, *, text: str = "", title: str = "",
            image_urls: Sequence[str] = (), blobs: Sequence[Blob] = (),
            to: str = "", subreddit: str = "",
            event_start: str = "", event_end: str = "", location: str = "") -> dict:
    """Despacho único usado pela API. Devolve {"id", "permalink", "dry_run"}."""
    if Config.DRY_RUN:
        return {"id": "DRYRUN", "permalink": "", "dry_run": True}
    fns = {
        # Reel: mesma Graph API do Instagram (instagram.py), só muda a mídia.
        "reel": lambda: publish_reel(image_urls[0] if image_urls else "", text),
        "whatsapp": lambda: publish_whatsapp(text, list(image_urls)),
        "telegram": lambda: publish_telegram(text, list(image_urls)),
        "mastodon": lambda: publish_mastodon(text, list(blobs)),
        "reddit": lambda: publish_reddit(title, text, list(image_urls), subreddit=subreddit),
        "email": lambda: publish_email(title, text, list(blobs), to=to),
        "gcal": lambda: publish_gcal(title, text, event_start=event_start,
                                     event_end=event_end, location=location),
    }
    fn = fns.get(channel)
    if fn is None:
        raise PublishError(f"Canal desconhecido: {channel}")
    return fn()


def delete(channel: str, provider_id: str) -> dict:
    """Apaga um anúncio JÁ PUBLICADO no provedor do canal (o registro no TTL é
    responsabilidade de quem chama). Só os canais com des-envio; a janela de
    24h é aplicada pelo /api/announcements/delete, não aqui."""
    if Config.DRY_RUN:
        return {"deleted": False, "dry_run": True}
    fns = {
        "telegram": delete_telegram,
        "mastodon": delete_mastodon,
        "whatsapp": delete_whatsapp,
        "gcal": delete_gcal,
    }
    fn = fns.get(channel)
    if fn is None:
        raise PublishError(f"O canal {channel} não suporta apagar.")
    if not provider_id:
        raise PublishError("Anúncio sem id do provedor — não dá pra apagar na rede.")
    return fn(provider_id)


def delete_telegram(message_id: str) -> dict:
    token, chat = Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID
    if not (token and chat):
        raise PublishError(f"Telegram não configurado — defina {REQUIRED_ENV['telegram']}.")
    res, _ = _http_json("POST", f"https://api.telegram.org/bot{token}/deleteMessage",
                        data=urllib.parse.urlencode(
                            {"chat_id": chat, "message_id": message_id}).encode())
    if not res.get("ok"):
        raise PublishError(f"Telegram recusou o delete: {json.dumps(res, ensure_ascii=False)[:300]}")
    return {"deleted": True, "dry_run": False}


def delete_mastodon(status_id: str) -> dict:
    base = (Config.MASTODON_BASE_URL or "").rstrip("/")
    token = Config.MASTODON_ACCESS_TOKEN
    if not (base and token):
        raise PublishError(f"Mastodon não configurado — defina {REQUIRED_ENV['mastodon']}.")
    _http_json("DELETE", f"{base}/api/v1/statuses/{urllib.parse.quote(status_id)}",
               headers={"Authorization": f"Bearer {token}"})
    return {"deleted": True, "dry_run": False}


def delete_whatsapp(message_id: str) -> dict:
    token = Config.WHAPI_TOKEN
    if not token:
        raise PublishError(f"WhatsApp não configurado — defina {REQUIRED_ENV['whatsapp']}.")
    _http_json("DELETE", f"{WHAPI_BASE}/messages/{urllib.parse.quote(message_id)}",
               headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    return {"deleted": True, "dry_run": False}


def delete_gcal(event_id: str) -> dict:
    cal = Config.GCAL_CALENDAR_ID
    if not cal:
        raise PublishError(f"Agenda não configurada — defina {REQUIRED_ENV['gcal']}.")
    _http_json("DELETE",
               f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(cal)}"
               f"/events/{urllib.parse.quote(event_id)}",
               headers={"Authorization": f"Bearer {_google_token()}"})
    return {"deleted": True, "dry_run": False}


# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _http_json(method: str, url: str, *, headers: Optional[dict] = None,
               data: Optional[bytes] = None) -> tuple[dict, int]:
    # UA própria SEMPRE: o default "Python-urllib/3.x" é banido por WAFs
    # (o gate.whapi.cloud responde 403 só por causa dele). Canal que precisa
    # de UA específica (Reddit) passa a dela em headers e sobrescreve.
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": "sabia-ph-composer/1.0",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            # corpo vazio (ex.: 204 do events.delete da Agenda) vira {}
            return (json.loads(raw) if raw.strip() else {}), resp.status
    except urllib.error.HTTPError as exc:
        try:
            err = json.load(exc)
        except Exception:
            err = {"error": exc.read().decode("utf-8", "replace")[:400]}
        host = urllib.parse.urlsplit(url).netloc
        raise PublishError(f"{host} HTTP {exc.code}: {json.dumps(err, ensure_ascii=False)[:400]}")


def _multipart(field: str, filename: str, content_type: str, data: bytes) -> tuple[bytes, str]:
    """Corpo multipart/form-data de um único arquivo (upload de mídia)."""
    boundary = "phb" + secrets.token_hex(12)
    buf = bytearray()
    buf += (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n").encode()
    buf += data + b"\r\n"
    buf += f"--{boundary}--\r\n".encode()
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


def _split_list(raw: str) -> List[str]:
    return [x.strip() for x in (raw or "").replace(";", ",").split(",") if x.strip()]


# ── WhatsApp (Whapi.Cloud) ────────────────────────────────────────────────────
def publish_whatsapp(text: str, image_urls: List[str]) -> dict:
    token, group = Config.WHAPI_TOKEN, Config.WHAPI_ANNOUNCE_GROUP
    if not (token and group):
        raise PublishError(f"WhatsApp não configurado — defina {REQUIRED_ENV['whatsapp']}.")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "Content-Type": "application/json"}
    if image_urls:
        # media como URL — o Whapi baixa sozinho (igual scripts/post_whatsapp.py).
        path, payload = "messages/image", {"to": group, "media": image_urls[0], "caption": text}
    else:
        path, payload = "messages/text", {"to": group, "body": text}
    res, _ = _http_json("POST", f"{WHAPI_BASE}/{path}", headers=headers,
                        data=json.dumps(payload).encode())
    if not (res.get("sent") is True or res.get("message")):
        raise PublishError(f"Whapi não confirmou o envio: {json.dumps(res, ensure_ascii=False)[:300]}")
    mid = (res.get("message") or {}).get("id", "")
    return {"id": mid or "sent", "permalink": "", "dry_run": False}


# ── Telegram (Bot API) ────────────────────────────────────────────────────────
def publish_telegram(text: str, image_urls: List[str]) -> dict:
    token, chat = Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID
    if not (token and chat):
        raise PublishError(f"Telegram não configurado — defina {REQUIRED_ENV['telegram']}.")
    base = f"https://api.telegram.org/bot{token}"
    if not image_urls:
        method, params = "sendMessage", {"chat_id": chat, "text": text}
    elif len(image_urls) == 1:
        method, params = "sendPhoto", {"chat_id": chat, "photo": image_urls[0], "caption": text}
    else:  # 2–10 fotos → media group (a legenda vai na primeira)
        media = [{"type": "photo", "media": u} for u in image_urls[:10]]
        media[0]["caption"] = text
        method, params = "sendMediaGroup", {"chat_id": chat, "media": json.dumps(media)}
    res, _ = _http_json("POST", f"{base}/{method}",
                        data=urllib.parse.urlencode(params).encode())
    if not res.get("ok"):
        raise PublishError(f"Telegram recusou: {json.dumps(res, ensure_ascii=False)[:300]}")
    result = res.get("result")
    msg = result[0] if isinstance(result, list) else (result or {})
    mid = msg.get("message_id", "")
    # Link público só existe em canal/grupo com @nome.
    permalink = f"https://t.me/{chat.lstrip('@')}/{mid}" if str(chat).startswith("@") and mid else ""
    return {"id": str(mid), "permalink": permalink, "dry_run": False}


# ── Mastodon ──────────────────────────────────────────────────────────────────
def publish_mastodon(text: str, blobs: List[Blob]) -> dict:
    base = (Config.MASTODON_BASE_URL or "").rstrip("/")
    token = Config.MASTODON_ACCESS_TOKEN
    if not (base and token):
        raise PublishError(f"Mastodon não configurado — defina {REQUIRED_ENV['mastodon']}.")
    auth = {"Authorization": f"Bearer {token}"}

    # 1) sobe cada imagem (o Mastodon não baixa URLs — precisa dos bytes)…
    media_ids = []
    for filename, data, ctype in blobs[:4]:
        body, mp_type = _multipart("file", filename, ctype or "image/jpeg", data)
        res, _ = _http_json("POST", f"{base}/api/v2/media",
                            headers={**auth, "Content-Type": mp_type}, data=body)
        media_ids.append(str(res["id"]))
    # 2) …espera o processamento (v2 responde 202 e o status 422 se apressar)…
    for mid in media_ids:
        _wait_mastodon_media(base, auth, mid)
    # 3) …e publica o toot.
    form = [("status", text)] + [("media_ids[]", m) for m in media_ids]
    res, _ = _http_json("POST", f"{base}/api/v1/statuses", headers=auth,
                        data=urllib.parse.urlencode(form).encode())
    return {"id": str(res.get("id", "")), "permalink": res.get("url", ""), "dry_run": False}


def _wait_mastodon_media(base: str, auth: dict, media_id: str,
                         attempts: int = 15, delay: float = 1.0) -> None:
    """GET /api/v1/media/{id} devolve 206 enquanto processa, 200 quando pronto."""
    for _ in range(attempts):
        req = urllib.request.Request(f"{base}/api/v1/media/{media_id}", headers=auth)
        try:
            with urllib.request.urlopen(req) as resp:
                if resp.status == 200:
                    return
        except urllib.error.HTTPError as exc:
            raise PublishError(f"Mastodon: mídia {media_id} falhou (HTTP {exc.code}).")
        time.sleep(delay)
    raise PublishError(f"Mastodon: mídia {media_id} não terminou de processar (~{int(attempts * delay)}s).")


# ── Reddit ────────────────────────────────────────────────────────────────────
def publish_reddit(title: str, body: str, image_urls: List[str], *, subreddit: str = "") -> dict:
    sub = (subreddit or Config.REDDIT_SUBREDDIT).removeprefix("/").removeprefix("r/").strip("/")
    cid, csecret = Config.REDDIT_CLIENT_ID, Config.REDDIT_CLIENT_SECRET
    user, pwd = Config.REDDIT_USERNAME, Config.REDDIT_PASSWORD
    if not (cid and csecret and user and pwd and sub):
        raise PublishError(f"Reddit não configurado — defina {REQUIRED_ENV['reddit']}.")
    ua = {"User-Agent": f"sabia-ph-composer/1.0 (by u/{user})"}

    basic = base64.b64encode(f"{cid}:{csecret}".encode()).decode()
    tok, _ = _http_json("POST", "https://www.reddit.com/api/v1/access_token",
                        headers={**ua, "Authorization": f"Basic {basic}"},
                        data=urllib.parse.urlencode(
                            {"grant_type": "password", "username": user, "password": pwd}).encode())
    token = tok.get("access_token")
    if not token:
        raise PublishError(f"Reddit não deu token: {json.dumps(tok, ensure_ascii=False)[:300]}")

    # Post de texto; imagens viram links no corpo (upload nativo = lease S3, fora
    # do escopo). O corpo ARMAZENADO no .ttl é só o texto — os links são extra.
    text = body or ""
    if image_urls:
        text += ("\n\n" if text else "") + "\n".join(
            f"[Foto {i}]({u})" for i, u in enumerate(image_urls, 1))
    res, _ = _http_json("POST", "https://oauth.reddit.com/api/submit",
                        headers={**ua, "Authorization": f"Bearer {token}"},
                        data=urllib.parse.urlencode(
                            {"sr": sub, "kind": "self", "title": title,
                             "text": text, "api_type": "json"}).encode())
    j = res.get("json", {})
    if j.get("errors"):
        raise PublishError(f"Reddit recusou: {j['errors']}")
    d = j.get("data", {})
    return {"id": str(d.get("id", "")), "permalink": d.get("url", ""), "dry_run": False}


# ── E-mail (SMTP) ─────────────────────────────────────────────────────────────
def publish_email(subject: str, body: str, blobs: List[Blob], *, to: str = "") -> dict:
    host, sender = Config.SMTP_HOST, Config.EMAIL_FROM
    rcpts = _split_list(to or Config.EMAIL_TO)
    if not (host and sender and rcpts):
        raise PublishError(f"E-mail não configurado — defina {REQUIRED_ENV['email']}.")

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(rcpts)
    msg.set_content(body)
    for filename, data, ctype in blobs:
        maintype, _, subtype = (ctype or "image/jpeg").partition("/")
        msg.add_attachment(data, maintype=maintype or "image",
                           subtype=subtype or "jpeg", filename=filename)
    try:
        smtp_cls = smtplib.SMTP_SSL if Config.SMTP_PORT == 465 else smtplib.SMTP
        with smtp_cls(host, Config.SMTP_PORT, timeout=30) as server:
            if Config.SMTP_PORT != 465:
                server.starttls()
            if Config.SMTP_USER:
                server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise PublishError(f"SMTP falhou: {exc}")
    return {"id": "sent", "permalink": "", "dry_run": False}


# ── Agenda (Google Calendar) ──────────────────────────────────────────────────
def _google_token() -> str:
    """Access token via ADC (google-auth vem junto do google-cloud-storage).
    Na Cloud Run resolve pra conta de serviço de runtime; localmente, use
    `gcloud auth application-default login` (ou fique no DRY_RUN)."""
    try:
        import google.auth
        import google.auth.transport.requests
    except ImportError:
        raise PublishError("google-auth indisponível — instale google-cloud-storage.")
    try:
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/calendar.events"])
        creds.refresh(google.auth.transport.requests.Request())
    except Exception as exc:
        raise PublishError(f"Sem credencial Google (ADC) pra Agenda: {exc}")
    return creds.token


def publish_gcal(title: str, description: str, *, event_start: str,
                 event_end: str = "", location: str = "") -> dict:
    """Cria o evento na agenda do grupo. event_start/event_end vêm ISO-8601 sem
    fuso (datetime-local) — o fuso vai à parte em timeZone (GCAL_TIMEZONE)."""
    cal = Config.GCAL_CALENDAR_ID
    if not cal:
        raise PublishError(f"Agenda não configurada — defina {REQUIRED_ENV['gcal']}.")
    if not event_start:
        raise PublishError("Evento precisa de data/hora de início.")
    event = {
        "summary": title,
        "start": {"dateTime": event_start, "timeZone": Config.GCAL_TIMEZONE},
        "end": {"dateTime": event_end, "timeZone": Config.GCAL_TIMEZONE},
    }
    if description:
        event["description"] = description
    if location:
        event["location"] = location
    res, _ = _http_json(
        "POST",
        f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(cal)}/events",
        headers={"Authorization": f"Bearer {_google_token()}",
                 "Content-Type": "application/json"},
        data=json.dumps(event).encode())
    if not res.get("id"):
        raise PublishError(f"Agenda não confirmou o evento: {json.dumps(res, ensure_ascii=False)[:300]}")
    return {"id": res["id"], "permalink": res.get("htmlLink", ""), "dry_run": False}
