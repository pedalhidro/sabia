#!/usr/bin/env python3
"""Pull the last N posts of an Instagram **Professional** account via the
official Instagram Graph API, into Turtle conforming to the Pedal Hidrográfico
shapes.

This is the ToS-compliant alternative to scripts/pull_instagram.py (instaloader).
It only works for an account you control (Business/Creator linked to a Facebook
Page), and needs a Meta app access token.

What the Graph API provides: caption, timestamp, permalink, image URLs
(incl. carousel children). What it does NOT: post location, tagged accounts,
per-image EXIF/GPS — those stay manual (location) or are simply absent.

Setup:
    1. Convert @pedalhidrografico to a Professional account, linked to a FB Page.
    2. Create an app at https://developers.facebook.com and get a (long-lived)
       access token with instagram_basic + pages_show_list.
    3. export IG_ACCESS_TOKEN=...        # required
       export IG_USER_ID=...            # optional; auto-discovered if omitted

Usage:
    pip install rdflib
    python scripts/pull_instagram_graph.py --count 5 --out definitions/data.ttl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from ttl_common import Post, add_post, new_graph, write_ttl

# Two different products / hosts:
#   * Instagram API with Instagram Login → graph.instagram.com, token "IG..."
#   * Facebook Login Graph API           → graph.facebook.com,  token "EAA..."
IG_API = "https://graph.instagram.com/v21.0"
FB_API = "https://graph.facebook.com/v21.0"


def api_base(token: str) -> str:
    """Pick the host that matches the token type."""
    return IG_API if token.startswith("IG") else FB_API


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no dependency). Real env vars take precedence."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip("'\""))


def _get(base: str, path: str, params: dict) -> dict:
    url = f"{base}/{path}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        # The Graph API returns a JSON error body even on 4xx — surface it.
        try:
            err = json.load(exc).get("error", {})
        except Exception:
            err = {"message": exc.read().decode("utf-8", "replace")[:500]}
        sys.exit(
            f"Graph API HTTP {exc.code}: {err.get('message')}\n"
            f"  type={err.get('type')} code={err.get('code')} "
            f"subcode={err.get('error_subcode')} "
            f"fbtrace_id={err.get('fbtrace_id')}"
        )
    if "error" in data:
        sys.exit(f"Graph API error: {data['error'].get('message', data['error'])}")
    return data


def discover_user_id(token: str) -> str:
    """Find the IG Business account id behind the token's first FB Page."""
    pages = _get(FB_API, "me/accounts", {
        "fields": "instagram_business_account",
        "access_token": token,
    }).get("data", [])
    for page in pages:
        iba = page.get("instagram_business_account")
        if iba:
            return iba["id"]
    sys.exit("No instagram_business_account found for this token. "
             "Is the account Professional and linked to a Page?")


def shortcode_from_permalink(permalink: str) -> str:
    # https://www.instagram.com/p/<SHORTCODE>/  (or /reel/<SHORTCODE>/)
    parts = [p for p in urllib.parse.urlparse(permalink).path.split("/") if p]
    return parts[-1] if parts else permalink


def image_urls(media: dict) -> list:
    """Ordered image URLs: carousel children in order, else the single media."""
    if media.get("media_type") == "CAROUSEL_ALBUM":
        return [c["media_url"] for c in media.get("children", {}).get("data", [])
                if c.get("media_url")]
    url = media.get("media_url") or media.get("thumbnail_url")
    return [url] if url else []


def to_post(media: dict) -> Post:
    # Graph API timestamps look like "2024-05-01T12:00:00+0000".
    ts = datetime.strptime(media["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
    return Post(
        shortcode=shortcode_from_permalink(media.get("permalink", media["id"])),
        caption=media.get("caption", ""),
        timestamp=ts,
        image_urls=image_urls(media),
    )


def main() -> None:
    load_dotenv()  # before argparse: --user-id's default reads IG_USER_ID
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--out", default="definitions/data.ttl")
    ap.add_argument("--user-id", default=os.environ.get("IG_USER_ID"))
    args = ap.parse_args()

    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        sys.exit("Set IG_ACCESS_TOKEN (see the module docstring for setup).")

    base = api_base(token)
    if base == IG_API:
        # Instagram Login API: the token implies the account → use "me".
        # A username in IG_USER_ID would be wrong here, so we ignore it.
        target = "me"
    else:
        # Facebook Graph API needs the NUMERIC IG Business account id.
        target = args.user_id or discover_user_id(token)
        if target and not str(target).isdigit():
            sys.exit(f"IG_USER_ID must be the numeric account id, got {target!r}. "
                     "Leave it unset to auto-discover, or use the Instagram-login token.")

    media_list = _get(base, f"{target}/media", {
        "fields": "caption,timestamp,permalink,media_type,media_url,"
                  "thumbnail_url,children{media_url,media_type}",
        "limit": args.count,
        "access_token": token,
    }).get("data", [])[:args.count]

    g = new_graph()
    for media in media_list:
        add_post(g, to_post(media))
    write_ttl(g, args.out)
    print(f"Wrote {len(media_list)} posts to {args.out}")


if __name__ == "__main__":
    main()
