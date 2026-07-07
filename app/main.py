"""Pedal Hidrográfico — Instagram post composer.

Serves the composer UI, uploads images to storage, publishes to Instagram
(or dry-runs), and records the post in the dataset TTL.

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

import storage
import ttl_store
from config import Config
from instagram import PublishError, publish

log = logging.getLogger("composer")
APP_DIR = Path(__file__).resolve().parent
app = FastAPI(title="PH Instagram Composer")


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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(APP_DIR / "static" / "composer.html"))


@app.get("/api/config")
def get_config() -> dict:
    return {"dry_run": Config.DRY_RUN, "using_gcs": Config.using_gcs(),
            "ig_user": Config.IG_USER_ID}


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
    ttl_store.run_rules(g)  # materialize AppOwned / Deletable types
    grid = []
    for p in posts:
        if not ttl_store.has_type(g, p["iri"], PH.AppOwnedInstagramPost):
            continue  # only posts published THROUGH this app
        grid.append({
            "shortcode": p["shortcode"], "permalink": p["permalink"],
            "thumb": p["thumb"], "caption": p["caption"][:120],
            "likes": p["likes"], "comments": p["comments"], "views": p["views"],
            "deletable": ttl_store.has_type(g, p["iri"], PH.DeletableInstagramPost),
        })
    return grid[:limit]


@app.get("/api/posts")
def list_posts() -> JSONResponse:
    return JSONResponse(_classified_grid(ttl_store.load_dataset()))


@app.post("/api/posts/delete")
def delete_post(shortcode: str = Form(...)) -> JSONResponse:
    g = ttl_store.load_dataset()
    iri = str(ttl_store.post_iri(shortcode))

    # Re-classify with fresh metrics — deletion is gated by the SHACL rules.
    from instagram import get_metrics, delete_media, PublishError
    post = next((p for p in ttl_store.app_posts(g) if p["iri"] == iri), None)
    if post is None:
        return JSONResponse({"ok": False, "error": "Post não encontrado."}, status_code=404)
    m = get_metrics(post["media_id"]) if post["media_id"] else {"likes": 0, "comments": 0, "views": 0}
    ttl_store.set_metrics(g, iri, m["likes"], m["comments"], m["views"])
    ttl_store.run_rules(g)

    if not ttl_store.has_type(g, iri, ttl_store.PH.DeletableInstagramPost):
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
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    ttl_store.remove_post(g, iri)
    ttl_store.save_dataset(g)
    return JSONResponse({"ok": True, "instagram": deleted})


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
    images: List[UploadFile] = File(default=[]),
    caption: str = Form(""),
    collaborators: str = Form(""),
    tagged: str = Form(""),
    location_name: str = Form(""),
    location_id: str = Form(""),
    location_url: str = Form(""),
    is_posted: str = Form("true"),
    confirm: str = Form("false"),
) -> JSONResponse:
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

    posted = is_posted.lower() in ("1", "true", "yes", "on")
    confirmed = confirm.lower() in ("1", "true", "yes", "on")

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
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

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
    )
    check = ttl_store.validate(ttl)

    return JSONResponse({
        "ok": True,
        "instagram": pub,
        "image_urls": image_urls,
        "validation": check,
    })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=Config.PORT, reload=True)
