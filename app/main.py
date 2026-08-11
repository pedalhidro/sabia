"""Pedal Hidrográfico — composer multi-canal de anúncios (sabiá).

Serves the composer UI (uma aba por canal), uploads images to storage,
cross-posts to Instagram / WhatsApp / Telegram / Mastodon / Reddit / e-mail /
Agenda (or dry-runs), and records each announcement in the dataset TTL in the
shape its channel expects.

Run locally:   uvicorn main:app --reload --port 8080   (from the app/ dir)
Then open:     http://localhost:8080
"""
from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import unicodedata
import uuid
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import channels
import storage
import tokens
import ttl_store
from config import Config
from instagram import PublishError, publish

log = logging.getLogger("composer")
APP_DIR = Path(__file__).resolve().parent
app = FastAPI(title="PH Composer — anúncios multi-canal")


@app.middleware("http")
async def password_gate(request: Request, call_next):
    """HTTP Basic password gate (guards the public URL). Any username, the
    password must match Config.APP_PASSWORD. No-op when APP_PASSWORD is empty.
    """
    pw = Config.APP_PASSWORD
    if pw:
        ok = False
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                supplied = base64.b64decode(auth[6:]).decode("utf-8").partition(":")[2]
                ok = secrets.compare_digest(supplied, pw)
            except Exception:
                ok = False
        if not ok:
            return Response("Senha necessária.", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="ph-composer"'})
    return await call_next(request)

# Serve uploaded images locally (in GCS mode they're served by the bucket).
Config.LOCAL_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(Config.LOCAL_UPLOAD_DIR)), name="uploads")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


# O Worker da Cloudflare em sabia.pedalhidrografi.co reescreve "/" →
# "/index.html" (convenção dos subdomínios estáticos), então as DUAS rotas
# precisam existir — sem o alias, a home pública responde 404.
@app.get("/")
@app.get("/index.html")
def index() -> FileResponse:
    return FileResponse(str(APP_DIR / "static" / "composer.html"))


@app.get("/api/config")
def get_config() -> dict:
    return {"dry_run": Config.DRY_RUN, "using_gcs": Config.using_gcs(),
            "ig_user": Config.IG_USER_ID,
            "channels": Config.channels_status(),
            # espec. de mídia por canal, lida das shapes (fonte da verdade)
            "media": ttl_store.media_specs(),
            # relógio do token de APAGAR (cacheado 6h) — a UI avisa quando o
            # acesso a dados estiver perto de expirar (renovação é manual).
            "manage_token": tokens.manage_token_status_cached()}


@app.post("/api/token-refresh")
def token_refresh() -> JSONResponse:
    """Renova o IG_ACCESS_TOKEN (60 dias, renovável enquanto válido) e reporta
    o relógio do IG_MANAGE_TOKEN. O Cloud Scheduler chama toda semana (job
    ig-token-refresh, ver deploy.sh) — assim o token nunca chega perto de
    expirar. A resposta traz só metadados, nunca valores de token."""
    result = tokens.refresh_ig_token()
    result["manage"] = tokens.manage_token_status()
    return JSONResponse(result, status_code=200 if result.get("ok") else 502)


@app.get("/api/announcements")
def list_announcements(channel: str) -> JSONResponse:
    """Anúncios já gravados de um canal ≠ Instagram (a grade do IG tem rota
    própria, /api/posts, com métricas e classificação SHACL)."""
    if channel == "universal":
        return JSONResponse(ttl_store.universal_posts(ttl_store.load_dataset()))
    if channel not in ttl_store.CHANNELS:
        return JSONResponse({"error": f"Canal desconhecido: {channel}"}, status_code=400)
    return JSONResponse(ttl_store.channel_announcements(ttl_store.load_dataset(), channel))


@app.post("/api/announcements/delete")
def delete_announcement(iri: str = Form(...)) -> JSONResponse:
    """Remove um anúncio de canal ≠ Instagram. Rascunho: só o registro, sempre.
    Publicado: apaga PRIMEIRO no provedor (Telegram/Mastodon/Whapi/Agenda) e só
    então tira o registro — e apenas dentro de 24h da publicação (janela de
    arrependimento; ver ttl_store.DELETE_WINDOW_HOURS). O Instagram tem fluxo
    próprio com regras de engajamento (/api/posts/delete)."""
    g = ttl_store.load_dataset()
    channel, info = ttl_store.find_announcement(g, iri)
    if channel is None:
        return JSONResponse({"ok": False, "error": "Anúncio não encontrado."}, status_code=404)

    provider = {"deleted": False, "dry_run": Config.DRY_RUN}
    if info["posted"]:
        if channel not in ttl_store.DELETABLE_CHANNELS:
            return JSONResponse(
                {"ok": False, "error": f"O canal {channel} não suporta apagar por aqui."},
                status_code=400)
        if not ttl_store.announcement_age_ok(info["date"]):
            return JSONResponse(
                {"ok": False, "blocked": True,
                 "error": f"Bloqueado: só dá pra apagar anúncio publicado há menos de "
                          f"{ttl_store.DELETE_WINDOW_HOURS}h."},
                status_code=403)
        try:
            provider = channels.delete(channel, info["provider_id"] or "")
        except PublishError as exc:
            log.warning("delete %s falhou: %s", channel, exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

    ttl_store.remove_announcement(g, iri, channel)
    ttl_store.save_dataset(g)
    return JSONResponse({"ok": True, "channel": channel, "provider": provider})


def _classified_grid(g, limit: int = 10) -> list:
    """Fetch live metrics, run the SHACL rules, and return app-owned posts
    (newest first) flagged with appOwned/deletable per the SHACL classification.
    """
    from instagram import get_metrics
    PH = ttl_store.PH
    posts = ttl_store.app_posts(g)
    for p in posts:
        m = get_metrics(p["media_id"]) if p["media_id"] else {"likes": 0, "comments": 0, "views": 0}
        p.update(m)
        ttl_store.set_metrics(g, p["iri"], m["likes"], m["comments"], m["views"])
    inferred = ttl_store.classify(g)  # AppOwned / Deletable, on a copy
    grid = []
    for p in posts:
        if not ttl_store.has_type(inferred, p["iri"], PH.AppOwnedInstagramPost):
            continue  # only posts published THROUGH this app
        grid.append({
            "shortcode": p["shortcode"], "permalink": p["permalink"],
            "thumb": p["thumb"], "caption": p["caption"][:120],
            "likes": p["likes"], "comments": p["comments"], "views": p["views"],
            "deletable": ttl_store.has_type(inferred, p["iri"], PH.DeletableInstagramPost),
        })
    return grid[:limit]


@app.get("/api/posts")
def list_posts() -> JSONResponse:
    return JSONResponse(_classified_grid(ttl_store.load_dataset()))


@app.post("/api/posts/delete")
def delete_post(shortcode: str = Form(...)) -> JSONResponse:
    # Todo erro sai como JSON 4xx. Um 5xx daqui é reescrito pelo gateway numa página HTML, e o
    # cliente (bot) perde a mensagem real — vira um "502 indisponível" opaco (foi o que escondeu
    # o token do IG expirado). Com 4xx o erro de verdade chega no Telegram.
    try:
        g = ttl_store.load_dataset()
        iri = str(ttl_store.post_iri(shortcode))

        # Re-classify with fresh metrics — deletion is gated by the SHACL rules.
        from instagram import get_metrics, delete_media, PublishError
        post = next((p for p in ttl_store.app_posts(g) if p["iri"] == iri), None)
        if post is None:
            return JSONResponse({"ok": False, "error": "Post não encontrado."}, status_code=404)
        m = get_metrics(post["media_id"]) if post["media_id"] else {"likes": 0, "comments": 0, "views": 0}
        ttl_store.set_metrics(g, iri, m["likes"], m["comments"], m["views"])
        inferred = ttl_store.classify(g)  # cópia com os tipos derivados (nada persiste)

        if not ttl_store.has_type(inferred, iri, ttl_store.PH.DeletableInstagramPost):
            return JSONResponse(
                {"ok": False, "blocked": True, "metrics": m,
                 "error": f"Bloqueado: engajamento alto demais (curtidas {m['likes']}, "
                          f"comentários {m['comments']}, views {m['views']}). "
                          "Só remove com <5 curtidas, <2 comentários e <300 views."},
                status_code=403,
            )

        # 1) delete on Instagram (real posts), 2) remove from dataset.
        deleted = {"deleted": False, "dry_run": Config.DRY_RUN}
        if post["media_id"]:
            try:
                deleted = delete_media(post["media_id"])
            except PublishError as exc:
                log.error("Instagram delete failed for media %s: %s", post["media_id"], exc)
                # 422 (não 502) pra mensagem atravessar a Cloudflare até a UI/bot.
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        ttl_store.remove_post(g, iri)
        ttl_store.save_dataset(g)
        return JSONResponse({"ok": True, "instagram": deleted})
    except Exception as exc:  # noqa: BLE001 — nunca estoure num 5xx opaco; devolve o motivo real
        log.exception("delete_post falhou para %s", shortcode)
        return JSONResponse({"ok": False, "error": f"Erro interno ao excluir: {exc}"}, status_code=400)


def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "post"


def _parse_handles(raw: str) -> List[str]:
    """Accept comma/space/newline separated @handles."""
    return [h.lstrip("@") for h in re.split(r"[\s,]+", raw or "") if h.strip()]


@app.post("/api/publish")
async def api_publish(
    request: Request,
    channel: str = Form("instagram"),
    images: List[UploadFile] = File(default=[]),
    caption: str = Form(""),
    text: str = Form(""),        # texto/corpo dos demais canais (caption = alias)
    title: str = Form(""),       # título (Reddit) / assunto (e-mail) / do universal
    blocks: List[str] = Form(default=[]),  # escada de blocos (só channel=universal)
    to: str = Form(""),          # destinatários (e-mail) — default do env
    subreddit: str = Form(""),   # Reddit — default do env
    event_start: str = Form(""),  # Agenda — ISO-8601 local (datetime-local)
    event_end: str = Form(""),    # Agenda — opcional (padrão: início + 3h)
    event_location: str = Form(""),  # Agenda — local do evento (texto livre)
    derived_from: str = Form(""),  # IRI do ph:UniversalPost de origem (prov)
    collaborators: str = Form(""),
    tagged: str = Form(""),
    location_name: str = Form(""),
    location_id: str = Form(""),
    location_url: str = Form(""),
    is_posted: str = Form("true"),
    confirm: str = Form("false"),
) -> JSONResponse:
    posted = is_posted.lower() in ("1", "true", "yes", "on")
    confirmed = confirm.lower() in ("1", "true", "yes", "on")
    if channel == "universal":
        return await _record_universal(request, images, blocks, title)
    if channel != "instagram":
        return await _publish_channel(request, channel, images, text or caption,
                                      title, to, subreddit, posted, confirmed,
                                      derived_from, event_start, event_end,
                                      event_location)

    if not images:
        return JSONResponse({"ok": False, "error": "No images provided."}, status_code=400)

    base_url = str(request.base_url)
    slug = _slugify(caption.splitlines()[0] if caption else "post")
    uid = uuid.uuid4().hex[:8]   # unique per post: keeps filenames from colliding
    image_urls: List[str] = []
    for i, up in enumerate(images, start=1):
        data = await up.read()
        ext = (Path(up.filename or "").suffix or ".jpg").lower()
        name = f"{slug}-{uid}-{i}{ext}"
        image_urls.append(storage.save_image(data, name, up.content_type or "image/jpeg", base_url))

    collab = _parse_handles(collaborators)
    tags = _parse_handles(tagged)
    overlap = set(collab) & set(tags)
    if overlap:
        return JSONResponse(
            {"ok": False, "error": f"Accounts can't be both tagged and collaborators: {sorted(overlap)}"},
            status_code=400,
        )

    # Safety guard: a real LIVE publish (posted, not dry-run) must be confirmed,
    # so a stray click can't post to Instagram.
    live = posted and not Config.DRY_RUN
    if live and not confirmed:
        return JSONResponse(
            {"ok": False, "needs_confirm": True,
             "error": "Confirmação necessária para publicar AO VIVO no Instagram."},
            status_code=409,
        )

    # 1) Publish to Instagram (skipped & faked if DRY_RUN, or if saving a draft).
    pub = {"id": None, "permalink": "", "dry_run": Config.DRY_RUN}
    if posted:
        try:
            pub = publish(
                image_urls, caption,
                location_id=location_id or None,
                user_tags=tags or None,
                collaborators=collab or None,
            )
        except PublishError as exc:
            # 422 (não 502): a Cloudflare troca 502 da origem pela página HTML
            # dela e a UI perderia a mensagem real. Logado pra diagnóstico.
            log.warning("publish instagram falhou: %s", exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

    # 2) Record in the dataset TTL.
    # Real posts get their unique IG permalink shortcode. Dry-runs and drafts
    # have no real permalink, so mint a UNIQUE id — otherwise every dry-run
    # would reuse "DRYRUN" and pile onto one node (duplicate articleBody/image
    # lists → SHACL violations).
    real = bool(pub.get("id")) and not pub.get("dry_run")
    if real:
        shortcode = pub["permalink"].rstrip("/").split("/")[-1] or f"{slug}-{uid}"
    else:
        shortcode = f"{slug}-{uid}"
    ttl = ttl_store.add_published_post(
        shortcode=shortcode,
        caption=caption,
        image_urls=image_urls,
        tagged=tags,
        collaborators=collab,
        location_name=location_name or None,
        location_url=location_url or None,
        is_posted=posted,
        media_id=pub.get("id") if real else None,
        permalink=pub.get("permalink") if real else None,
        derived_from=derived_from or None,
    )
    check = ttl_store.validate(ttl)

    return JSONResponse({
        "ok": True,
        "instagram": pub,
        "image_urls": image_urls,
        "validation": check,
    })


async def _record_universal(request: Request, images: List[UploadFile],
                            blocks: List[str], title: str) -> JSONResponse:
    """Grava a matriz do cross-post (ph:UniversalPost). NUNCA publica em rede:
    a derivação por canal acontece nas abas (a app corta texto na escada e
    recorta as imagens), e cada publicação leva prov:wasDerivedFrom pra cá."""
    def bad(msg: str) -> JSONResponse:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)

    # blocos: sem vazio no meio (a escada é posicional), 1º obrigatório.
    blocks = [b.rstrip("\r") for b in blocks]
    while blocks and not blocks[-1].strip():
        blocks.pop()
    if not blocks or not blocks[0].strip():
        return bad("Bloco 1 é obrigatório (é o que entra em TODOS os canais).")
    if any(not b.strip() for b in blocks):
        return bad("Preencha os blocos em sequência, sem pular (a escada é posicional).")
    ladder = ttl_store.text_ladder()
    if len(blocks) > len(ladder):
        return bad(f"No máximo {len(ladder)} blocos — nenhum canal comporta mais.")
    for i, (b, tier) in enumerate(zip(blocks, ladder), start=1):
        if len(b) > tier["budget"]:
            return bad(f"Bloco {i} com {len(b)} caracteres — o degrau comporta {tier['budget']} "
                       f"(soma até aqui ≤ {tier['limit']}).")
    title = title.strip()
    if len(title) > 255:
        return bad(f"Título com {len(title)} caracteres — máximo é 255 (assunto de e-mail).")
    if len(images) > 20:
        return bad("No máximo 20 imagens (o teto do Reddit/e-mail).")

    base_url = str(request.base_url)
    slug = _slugify(title or blocks[0].splitlines()[0])
    uid = uuid.uuid4().hex[:8]
    blobs, image_urls = [], []
    for i, up in enumerate(images, start=1):
        data = await up.read()
        mime = up.content_type or "image/jpeg"
        ext = (Path(up.filename or "").suffix or ".jpg").lower()
        name = f"{slug}-{uid}-{i}{ext}"
        blobs.append((name, data, mime))
        image_urls.append(storage.save_image(data, name, mime, base_url))

    iri, ttl = ttl_store.add_universal_post(
        f"{slug}-{uid}",
        blocks=blocks,
        title=title or None,
        image_urls=image_urls,
        image_meta=[(len(d), m) for _, d, m in blobs],
    )
    check = ttl_store.validate(ttl)
    return JSONResponse({
        "ok": True,
        "channel": "universal",
        "iri": iri,
        "image_urls": image_urls,
        "ladder": ladder,
        "validation": check,
    })


async def _publish_channel(request: Request, channel: str, images: List[UploadFile],
                           text: str, title: str, to: str, subreddit: str,
                           posted: bool, confirmed: bool,
                           derived_from: str = "", event_start: str = "",
                           event_end: str = "", event_location: str = "") -> JSONResponse:
    """Publica num canal ≠ Instagram e grava o anúncio na forma do shape dele."""
    spec = ttl_store.CHANNELS.get(channel)
    if spec is None:
        return JSONResponse({"ok": False, "error": f"Canal desconhecido: {channel}"},
                            status_code=400)

    def bad(msg: str) -> JSONResponse:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)

    text, title = text.strip(), title.strip()
    if spec["needs_title"] and not title:
        return bad("Falta o título/assunto (obrigatório neste canal).")
    if spec["needs_text"] and not text:
        return bad("Falta o texto da mensagem.")
    if spec.get("needs_event"):
        # Normaliza pra ISO com segundos (o datetime-local manda "…THH:MM" e a
        # Calendar API exige RFC3339); sem fim, o evento dura 3h (pedal típico).
        from datetime import datetime, timedelta
        if not event_start:
            return bad("Falta a data/hora de início do evento.")
        try:
            start_dt = datetime.fromisoformat(event_start)
        except ValueError:
            return bad(f"Início inválido: {event_start!r} (use AAAA-MM-DDTHH:MM).")
        end_dt = start_dt + timedelta(hours=3)
        if event_end:
            try:
                end_dt = datetime.fromisoformat(event_end)
            except ValueError:
                return bad(f"Fim inválido: {event_end!r} (use AAAA-MM-DDTHH:MM).")
            if end_dt <= start_dt:
                return bad("O fim do evento precisa ser depois do início.")
        event_start, event_end = start_dt.isoformat(), end_dt.isoformat()
    if len(text) > spec["max_text"]:
        return bad(f"Texto com {len(text)} caracteres — máximo do canal é {spec['max_text']}.")
    if title and len(title) > spec["max_title"]:
        return bad(f"Título com {len(title)} caracteres — máximo do canal é {spec['max_title']}.")
    is_video = spec["images"] == "video"
    if len(images) > spec["max_images"]:
        kind = "vídeo(s)" if is_video else "imagem(ns)"
        limite = f"até {spec['max_images']} {kind}" if spec["max_images"] else f"nenhum(a) {kind}"
        return bad(f"Este canal aceita {limite}.")
    if is_video and not images:
        return bad("Falta o vídeo do Reel.")

    # Espec. de mídia do shape: formato aceito e tamanho máximo por arquivo.
    media = ttl_store.media_specs().get(channel, {})
    accepted = media.get("video_formats" if is_video else "image_formats") or []
    max_bytes = media.get("max_video_bytes" if is_video else "max_image_bytes")
    blobs, image_urls = [], []
    base_url = str(request.base_url)
    slug = _slugify(title or (text.splitlines()[0] if text else channel))
    uid = uuid.uuid4().hex[:8]
    for i, up in enumerate(images, start=1):
        data = await up.read()
        mime = up.content_type or ("video/mp4" if is_video else "image/jpeg")
        if accepted and mime not in accepted:
            return bad(f"Formato {mime} não aceito — o canal aceita: {', '.join(accepted)}.")
        if max_bytes and len(data) > max_bytes:
            return bad(f"Arquivo {i} tem {len(data) / 1048576:.1f} MB — "
                       f"máximo do canal é {max_bytes / 1048576:.0f} MB.")
        ext = (Path(up.filename or "").suffix or (".mp4" if is_video else ".jpg")).lower()
        name = f"{slug}-{uid}-{i}{ext}"
        blobs.append((name, data, mime))
        image_urls.append(storage.save_image(data, name, mime, base_url))

    # Mesma trava do Instagram: publicar AO VIVO exige confirmação explícita.
    live = posted and not Config.DRY_RUN
    if live and not confirmed:
        return JSONResponse(
            {"ok": False, "needs_confirm": True,
             "error": f"Confirmação necessária para publicar AO VIVO ({channel})."},
            status_code=409,
        )
    if live and not Config.channels_status().get(channel, {}).get("configured"):
        return bad(f"Canal {channel} não configurado — defina {channels.REQUIRED_ENV[channel]} "
                   "(ou salve como rascunho / rode em DRY_RUN).")

    pub = {"id": None, "permalink": "", "dry_run": Config.DRY_RUN}
    if posted:
        try:
            pub = channels.publish(channel, text=text, title=title,
                                   image_urls=image_urls, blobs=blobs,
                                   to=to, subreddit=subreddit,
                                   event_start=event_start, event_end=event_end,
                                   location=event_location)
        except PublishError as exc:
            # 422 (não 502): ver o comentário no fluxo do Instagram.
            log.warning("publish %s falhou: %s", channel, exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

    real = bool(pub.get("id")) and not pub.get("dry_run")
    ttl = ttl_store.add_channel_announcement(
        channel, f"{slug}-{uid}",
        text=text or None,
        title=title or None,
        image_urls=image_urls,
        image_meta=[(len(d), m) for _, d, m in blobs],
        is_posted=posted,
        permalink=(pub.get("permalink") or None) if real else None,
        provider_id=(str(pub.get("id")) or None) if real else None,
        derived_from=derived_from or None,
        event_start=event_start or None,
        event_end=event_end or None,
        event_location=event_location or None,
    )
    check = ttl_store.validate(ttl)
    return JSONResponse({
        "ok": True,
        "channel": channel,
        "result": pub,
        "image_urls": image_urls,
        "validation": check,
    })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=Config.PORT, reload=True)
