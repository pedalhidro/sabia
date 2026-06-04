"""Instagram content publishing via the Graph API (Instagram Login host).

Flow (https://developers.facebook.com/docs/instagram-platform/content-publishing):
  single image:  create container → publish
  carousel:      create N item containers → create carousel container → publish

Requires the access token to have the `instagram_business_content_publish`
permission. Honors Config.DRY_RUN (skips network, returns a fake result) so the
whole app is testable locally without touching the real account.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional

from config import Config

IG_HOST = "https://graph.instagram.com/v21.0"   # Instagram-Login tokens (IG...)
FB_HOST = "https://graph.facebook.com/v21.0"    # Facebook-Login tokens (EAA...)


class PublishError(RuntimeError):
    pass


def _base(token: str) -> str:
    """Pick the API host that matches the token type."""
    return IG_HOST if (token or "").startswith("IG") else FB_HOST


def _post(path: str, params: dict) -> dict:
    base = _base(params.get("access_token", ""))
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{base}/{path}", data=data, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        try:
            err = json.load(exc).get("error", {})
        except Exception:
            err = {"message": exc.read().decode("utf-8", "replace")[:500]}
        raise PublishError(
            f"IG API HTTP {exc.code}: {err.get('message')} "
            f"(code={err.get('code')}, subcode={err.get('error_subcode')})"
        )


def _get(path: str, params: dict) -> dict:
    base = _base(params.get("access_token", ""))
    url = f"{base}/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url) as resp:
        return json.load(resp)


def get_metrics(media_id: str) -> dict:
    """Best-effort engagement for a media id: {likes, comments, views}.
    Returns zeros in DRY_RUN / no token / on any error (so drafts count as 0).
    """
    zero = {"likes": 0, "comments": 0, "views": 0}
    if Config.DRY_RUN or not media_id or not Config.IG_ACCESS_TOKEN:
        return zero
    token = Config.IG_ACCESS_TOKEN
    try:
        d = _get(media_id, {
            "fields": "like_count,comments_count,media_type,media_product_type",
            "access_token": token,
        })
    except Exception:
        return zero
    out = {"likes": int(d.get("like_count") or 0),
           "comments": int(d.get("comments_count") or 0), "views": 0}
    # "views" only applies to video/reels — fetch via insights (best-effort;
    # needs instagram_manage_insights). Images stay at 0.
    if d.get("media_type") == "VIDEO" or d.get("media_product_type") == "REELS":
        try:
            ins = _get(f"{media_id}/insights", {"metric": "views", "access_token": token})
            for m in ins.get("data", []):
                if "total_value" in m:
                    out["views"] = int(m["total_value"].get("value") or 0)
                elif m.get("values"):
                    out["views"] = int(m["values"][0].get("value") or 0)
        except Exception:
            pass
    return out


def delete_media(media_id: str) -> dict:
    """Delete a published post via DELETE /{ig-media-id}. Requires the
    instagram_manage_contents permission. No-op in DRY_RUN.
    """
    if Config.DRY_RUN or not media_id:
        return {"deleted": False, "dry_run": True}
    # Use the dedicated management token if provided (it carries
    # instagram_manage_contents and may be a different type than the publish
    # token); route to the host that matches that token.
    token = Config.IG_MANAGE_TOKEN or Config.IG_ACCESS_TOKEN
    url = f"{_base(token)}/{media_id}?" + urllib.parse.urlencode({"access_token": token})
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req) as resp:
            json.load(resp)
        return {"deleted": True, "dry_run": False}
    except urllib.error.HTTPError as exc:
        try:
            err = json.load(exc).get("error", {})
        except Exception:
            err = {"message": exc.read().decode("utf-8", "replace")[:300]}
        raise PublishError(
            f"IG delete HTTP {exc.code}: {err.get('message')} "
            f"(code={err.get('code')}) — precisa da permissão instagram_manage_contents."
        )


def publish(
    image_urls: List[str],
    caption: str,
    *,
    location_id: Optional[str] = None,
    user_tags: Optional[List[str]] = None,      # usernames to tag on the image
    collaborators: Optional[List[str]] = None,  # usernames invited to collab
) -> dict:
    """Publish a post. Returns {"id", "permalink", "dry_run"}."""
    # The Instagram-Login API needs the NUMERIC id or "me" — never a username.
    # IG_USER_ID is often set to the @handle, so fall back to "me".
    user = Config.IG_USER_ID if str(Config.IG_USER_ID).isdigit() else "me"
    token = Config.IG_ACCESS_TOKEN

    if Config.DRY_RUN:
        return {
            "id": "DRYRUN",
            "permalink": "https://www.instagram.com/p/DRYRUN/",
            "dry_run": True,
        }
    if not token:
        raise PublishError("IG_ACCESS_TOKEN not set (and DRY_RUN is off).")
    if not image_urls:
        raise PublishError("At least one image is required.")

    # user_tags: IG wants [{username, x, y}] with x,y in 0..1. Center by default.
    tags_param = (
        json.dumps([{"username": u.lstrip("@"), "x": 0.5, "y": 0.5} for u in user_tags])
        if user_tags else None
    )
    collab_param = (
        json.dumps([c.lstrip("@") for c in collaborators]) if collaborators else None
    )

    if len(image_urls) == 1:
        creation_id = _create_container(
            user, token, image_urls[0], caption=caption,
            location_id=location_id, user_tags=tags_param, collaborators=collab_param,
        )
    else:
        children = []
        for url in image_urls:
            cid = _create_container(user, token, url, is_carousel_item=True)
            _wait_ready(cid, token)  # each child must finish before the carousel
            children.append(cid)
        creation_id = _create_carousel(
            user, token, children, caption=caption,
            location_id=location_id, collaborators=collab_param,
        )

    # Containers are processed asynchronously — publishing too early returns
    # 9007 "Media ID is not available". Wait until the container is FINISHED.
    _wait_ready(creation_id, token)
    result = _post(f"{user}/media_publish", {"creation_id": creation_id, "access_token": token})
    media_id = result["id"]
    info = _get_permalink(user, token, media_id)
    return {"id": media_id, "permalink": info, "dry_run": False}


def _wait_ready(container_id: str, token: str, attempts: int = 20, delay: float = 2.0) -> None:
    """Poll a media container until its status_code is FINISHED."""
    for _ in range(attempts):
        url = f"{_base(token)}/{container_id}?" + urllib.parse.urlencode(
            {"fields": "status_code,status", "access_token": token}
        )
        try:
            with urllib.request.urlopen(url) as resp:
                status = json.load(resp).get("status_code")
        except Exception:
            status = None
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise PublishError("Instagram rejected the media container (status ERROR) — "
                               "usually an unreachable or invalid image URL.")
        time.sleep(delay)
    raise PublishError("Media container not ready after waiting (~40s). Try again.")


def _create_container(user, token, image_url, *, caption=None, is_carousel_item=False,
                      location_id=None, user_tags=None, collaborators=None) -> str:
    params = {"image_url": image_url, "access_token": token}
    if is_carousel_item:
        params["is_carousel_item"] = "true"
    if caption is not None:
        params["caption"] = caption
    if location_id:
        params["location_id"] = location_id
    if user_tags:
        params["user_tags"] = user_tags
    if collaborators:
        params["collaborators"] = collaborators
    return _post(f"{user}/media", params)["id"]


def _create_carousel(user, token, children, *, caption=None,
                     location_id=None, collaborators=None) -> str:
    params = {
        "media_type": "CAROUSEL",
        "children": ",".join(children),
        "access_token": token,
    }
    if caption is not None:
        params["caption"] = caption
    if location_id:
        params["location_id"] = location_id
    if collaborators:
        params["collaborators"] = collaborators
    return _post(f"{user}/media", params)["id"]


def _get_permalink(user, token, media_id) -> str:
    url = f"{_base(token)}/{media_id}?" + urllib.parse.urlencode(
        {"fields": "permalink", "access_token": token}
    )
    try:
        with urllib.request.urlopen(url) as resp:
            return json.load(resp).get("permalink", "")
    except Exception:
        return ""
