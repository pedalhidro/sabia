#!/usr/bin/env python3
"""Pull the last N Instagram posts of a profile into Turtle conforming to the
Pedal Hidrográfico shapes, via instaloader (scraping).

This is the no-API-setup option. For an account you control, prefer the official
scripts/pull_instagram_graph.py (ToS-compliant, no rate-limit bans). Both share
the RDF mapping in scripts/ttl_common.py.

LIMITS:
  * Instagram strips EXIF, so there is no per-image GPS/timestamp — announcement
    images carry only their URL (ph:AnnouncementImage), by design.
  * post.location and stable fetching generally require a logged-in session
    (--login). Anonymous runs are rate-limited.

Usage:
    pip install instaloader rdflib
    python scripts/pull_instagram.py --profile pedalhidrografico --count 5 \
        --out definitions/data.ttl [--login your_ig_username]
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import timezone

try:
    import instaloader
except ImportError:
    sys.exit("Missing dependency: pip install instaloader rdflib")

from ttl_common import Post, add_post, new_graph, write_ttl


def to_post(post) -> Post:
    """Normalise an instaloader Post into the shared Post."""
    sidecar = list(post.get_sidecar_nodes()) if post.typename == "GraphSidecar" else []
    urls = [n.display_url for n in sidecar] if sidecar else [post.url]
    loc = getattr(post, "location", None)
    return Post(
        shortcode=post.shortcode,
        caption=post.caption or "",
        timestamp=post.date_utc.replace(tzinfo=timezone.utc),
        image_urls=urls,
        location_name=loc.name if loc else None,
        location_lat=loc.lat if loc else None,
        location_lng=loc.lng if loc else None,
        tagged=list(getattr(post, "tagged_users", []) or []),
    )


def login(L: "instaloader.Instaloader", username: str) -> None:
    try:
        # 1) reuse a cached session if one exists (no password needed).
        L.load_session_from_file(username)
    except FileNotFoundError:
        # 2) non-interactive: password from the IG_PASSWORD env var;
        #    3) otherwise prompt for it (never echoed, never stored).
        password = os.environ.get("IG_PASSWORD") or getpass.getpass(
            f"Instagram password for {username}: "
        )
        L.login(username, password)  # raises on 2FA — use interactive_login then
        L.save_session_to_file()     # cache it so step 1 works next time


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", default="pedalhidrografico")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--out", default="definitions/data.ttl")
    ap.add_argument("--login", help="IG username to use a logged-in session")
    args = ap.parse_args()

    L = instaloader.Instaloader(
        download_pictures=False, download_videos=False,
        download_comments=False, save_metadata=False, quiet=True,
    )
    if args.login:
        login(L, args.login)

    profile = instaloader.Profile.from_username(L.context, args.profile)
    posts = []
    for post in profile.get_posts():
        posts.append(post)
        if len(posts) >= args.count:
            break

    g = new_graph()
    for post in posts:
        add_post(g, to_post(post))
    write_ttl(g, args.out)
    print(f"Wrote {len(posts)} posts to {args.out}")


if __name__ == "__main__":
    main()
