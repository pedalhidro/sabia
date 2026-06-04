#!/usr/bin/env python3
"""Post a ph:WhatsappMessage to a WhatsApp **Community announcement group** via
the Whapi.Cloud API (option "C": a third-party provider that wraps the
unofficial multi-device protocol — see the project notes).

Why a third party? Meta's official WhatsApp Cloud API does NOT expose Community
announcement groups (or Channels) — it only does 1:1 template/session messages.
Whapi.Cloud drives a *real* WhatsApp number you control; if that number is an
admin of the Community, this script can post to its announcement group.

⚠️  This relies on the unofficial protocol and is a WhatsApp ToS gray area. The
    number can in principle be banned. Fine for low volume (this project: a
    handful of announcements a month); don't run it on a number you can't lose.

What it does: reads a ph:WhatsappMessage from the Turtle (schema:text + optional
single schema:image) and POSTs it — text-only via /messages/text, or image+caption
via /messages/image. The shapes already cap text at 2150 chars and images at 1.

Setup:
    1. Create a channel at https://whapi.cloud and pair your WhatsApp number (QR).
    2. Make that number an admin of the Community.
    3. export WHAPI_TOKEN=...              # required (Bearer token of the channel)
       export WHAPI_ANNOUNCE_GROUP=...     # the announcement group id, e.g.
                                           #   120363297265854901@g.us
       Discover the group id with:  python scripts/post_whatsapp.py --list-announce

Usage:
    pip install rdflib
    # Post a specific message node from the TTL:
    python scripts/post_whatsapp.py --message ph:wa-passeio-cantareira
    # Or post ad-hoc text without touching the TTL:
    python scripts/post_whatsapp.py --text "Bom dia! Passeio domingo 8h." --image https://…/foto.jpg
    # Always check first:
    python scripts/post_whatsapp.py --message ph:wa-… --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from ttl_common import MAX_BODY, PH, SCHEMA, new_graph

try:
    from rdflib import Graph, URIRef
    from rdflib.namespace import RDF
except ImportError:
    raise SystemExit("Missing dependency: pip install rdflib")

WHAPI_BASE = "https://gate.whapi.cloud"


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


def _request(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    url = f"{WHAPI_BASE}/{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("authorization", f"Bearer {token}")
    req.add_header("accept", "application/json")
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            err = json.load(exc)
        except Exception:
            err = {"error": exc.read().decode("utf-8", "replace")[:500]}
        sys.exit(f"Whapi HTTP {exc.code}: {json.dumps(err)[:600]}")


def list_announce_groups(token: str) -> None:
    """Print the announcement group of every Community the number belongs to.

    The announcement group is the subgroup with isCommunityAnnounce == true
    (its id differs from the Community id, despite sharing the name).
    """
    communities = _request("GET", "communities", token).get("communities", [])
    if not communities:
        print("No communities found for this number. Is it a member/admin of one?")
        return
    for c in communities:
        cid = c.get("id")
        subs = _request("GET", f"communities/{cid}/subgroups", token).get("subgroups", [])
        announce = next((s for s in subs if s.get("isCommunityAnnounce")), None)
        name = c.get("name", "?")
        if announce:
            print(f"{announce['id']}\t# announce group of community “{name}”")
        else:
            print(f"(none)\t# community “{name}” ({cid}) — no announce subgroup visible")


# ── Reading a ph:WhatsappMessage out of the Turtle ────────────────────────────

def _expand(ref: str) -> URIRef:
    """Accept either a full IRI or a ph:-prefixed name."""
    if ref.startswith("ph:"):
        return URIRef(PH[ref[3:]])
    return URIRef(ref)


def read_message(graph: Graph, node: URIRef) -> tuple[str, str | None]:
    """Return (text, image_url|None) for a ph:WhatsappMessage node."""
    if (node, RDF.type, PH.WhatsappMessage) not in graph:
        sys.exit(f"{node} is not a ph:WhatsappMessage in the graph.")
    texts = list(graph.objects(node, SCHEMA.text))
    if not texts:
        sys.exit(f"{node} has no schema:text — nothing to post.")
    text = str(texts[0])

    # schema:image points at a single ph:AnnouncementImage (the WA shape caps it
    # at 1, not a list); pull its schema:contentUrl if present.
    image_url = None
    for img in graph.objects(node, SCHEMA.image):
        url = graph.value(img, SCHEMA.contentUrl)
        if url:
            image_url = str(url)
        break
    return text, image_url


# ── Posting ───────────────────────────────────────────────────────────────────

def post(token: str, to: str, text: str, image_url: str | None, dry_run: bool) -> None:
    if len(text) > MAX_BODY:
        sys.exit(f"Text is {len(text)} chars; WhatsApp shape caps it at {MAX_BODY}.")

    if image_url:
        # media as a URL string — Whapi auto-fetches it. If your account needs the
        # object form instead, swap to {"url": image_url}.
        endpoint, payload = "messages/image", {"to": to, "media": image_url, "caption": text}
    else:
        endpoint, payload = "messages/text", {"to": to, "body": text}

    if dry_run:
        print(f"[dry-run] POST /{endpoint}\n{json.dumps(payload, ensure_ascii=False, indent=2)}")
        return

    res = _request("POST", endpoint, token, payload)
    sent = (res.get("sent") is True) or bool(res.get("message"))
    mid = (res.get("message") or {}).get("id", "?")
    print(f"{'Sent' if sent else 'Response'}: id={mid}\n{json.dumps(res, ensure_ascii=False)[:400]}")


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-announce", action="store_true",
                    help="list announcement group ids for the number's communities, then exit")
    ap.add_argument("--message", help="ph:WhatsappMessage IRI or ph:-name to read from --in")
    ap.add_argument("--text", help="post this text directly (instead of --message)")
    ap.add_argument("--image", help="image URL to attach (with --text)")
    ap.add_argument("--in", dest="infile", default="definitions/data.ttl")
    ap.add_argument("--to", default=os.environ.get("WHAPI_ANNOUNCE_GROUP"),
                    help="announcement group id, e.g. 1203…@g.us (default: $WHAPI_ANNOUNCE_GROUP)")
    ap.add_argument("--dry-run", action="store_true", help="print the request, don't send")
    args = ap.parse_args()

    token = os.environ.get("WHAPI_TOKEN")
    if not token:
        sys.exit("Set WHAPI_TOKEN (see the module docstring for setup).")

    if args.list_announce:
        list_announce_groups(token)
        return

    if args.message:
        g = new_graph()
        g.parse(args.infile, format="turtle")
        text, image_url = read_message(g, _expand(args.message))
    elif args.text:
        text, image_url = args.text, args.image
    else:
        sys.exit("Give --message <iri>, or --text (optionally --image), or --list-announce.")

    if not args.to:
        sys.exit("No target group: pass --to or set WHAPI_ANNOUNCE_GROUP "
                 "(find it with --list-announce).")

    post(token, args.to, text, image_url, args.dry_run)


if __name__ == "__main__":
    main()
