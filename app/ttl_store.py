"""Append a published post to the dataset TTL, reusing scripts/ttl_common so the
output matches the SHACL shapes exactly. Loads the existing graph, adds the new
ph:InstagramPost, and writes it back (works for local files and gs:// URIs).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import rdflib
from rdflib import RDF, Literal, URIRef
from rdflib.collection import Collection
from rdflib.namespace import XSD

# Reuse the shared mapping that the pullers use.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ttl_common  # noqa: E402

import storage  # noqa: E402

PH, SCHEMA, DCTERMS = ttl_common.PH, ttl_common.SCHEMA, ttl_common.DCTERMS
ROOT = Path(__file__).resolve().parent.parent


def post_iri(shortcode: str) -> URIRef:
    return ttl_common.post_iri(shortcode)


def load_dataset() -> rdflib.Graph:
    g = ttl_common.new_graph()
    text = storage.read_ttl()
    if text.strip():
        g.parse(data=text, format="turtle")
    return g


def save_dataset(g: rdflib.Graph) -> None:
    storage.write_ttl(g.serialize(format="turtle"))


def add_published_post(
    *,
    shortcode: str,
    caption: str,
    image_urls: List[str],
    tagged: Optional[List[str]] = None,
    collaborators: Optional[List[str]] = None,
    location_name: Optional[str] = None,
    location_url: Optional[str] = None,
    is_posted: bool = True,
    media_id: Optional[str] = None,
    permalink: Optional[str] = None,
    when: Optional[datetime] = None,
) -> str:
    """Add the post to the dataset and return the serialized Turtle."""
    g = load_dataset()
    post = ttl_common.Post(
        shortcode=shortcode,
        caption=caption,
        timestamp=when or datetime.now(timezone.utc),
        image_urls=image_urls,
        location_name=location_name,
        location_url=location_url,
        tagged=tagged or [],
        collaborators=collaborators or [],
        is_posted=is_posted,
    )
    ttl_common.add_post(g, post)

    iri = ttl_common.post_iri(shortcode)
    g.add((iri, PH.managedByApp, Literal(True)))  # ownership: published via this app
    if media_id:
        g.add((iri, PH.instagramMediaId, Literal(media_id)))
    if permalink:
        g.add((iri, PH.permalink, Literal(permalink, datatype=XSD.anyURI)))

    out = g.serialize(format="turtle")
    storage.write_ttl(out)
    return out


# ── Grid: list app posts, classify via SHACL rules, remove ───────────────────
def app_posts(g: rdflib.Graph) -> List[dict]:
    """Full ph:InstagramPost nodes (have an image list), newest first."""
    posts = []
    for s in g.subjects(RDF.type, PH.InstagramPost):
        head = g.value(s, SCHEMA.image)
        if head is None:
            continue
        members = list(Collection(g, head))
        thumb = g.value(members[0], SCHEMA.contentUrl) if members else None
        posts.append({
            "iri": str(s),
            "shortcode": str(s).rsplit("ig-", 1)[-1],
            "permalink": str(g.value(s, PH.permalink) or ""),
            "media_id": str(g.value(s, PH.instagramMediaId) or ""),
            "caption": str(g.value(s, SCHEMA.articleBody) or ""),
            "is_posted": str(g.value(s, PH.isPosted)).lower() == "true",
            "thumb": str(thumb) if thumb else None,
            "date": str(g.value(s, DCTERMS.date) or ""),
        })
    posts.sort(key=lambda p: p["date"], reverse=True)
    return posts


def set_metrics(g: rdflib.Graph, iri: str, likes: int, comments: int, views: int) -> None:
    s = URIRef(iri)
    for p in (PH.likeCount, PH.commentCount, PH.viewCount):
        for t in list(g.triples((s, p, None))):
            g.remove(t)
    g.add((s, PH.likeCount, Literal(int(likes))))
    g.add((s, PH.commentCount, Literal(int(comments))))
    g.add((s, PH.viewCount, Literal(int(views))))


def run_rules(g: rdflib.Graph) -> None:
    """Materialize ph:VisibleInstagramPost / ph:DeletableInstagramPost in-place
    by executing the SHACL rules (SHACL-AF). No-op if pyshacl is missing.
    """
    try:
        from pyshacl import validate as shacl_validate
    except ImportError:
        return
    shapes = rdflib.Graph()
    for f in ("definitions/ontology.ttl", "definitions/shapes.ttl"):
        shapes.parse(str(ROOT / f), format="turtle")
    shacl_validate(g, shacl_graph=shapes, advanced=True, inplace=True)


def has_type(g: rdflib.Graph, iri: str, cls) -> bool:
    return (URIRef(iri), RDF.type, cls) in g


def remove_post(g: rdflib.Graph, iri: str) -> None:
    """Delete the post node, its image-list cells and image nodes."""
    s = URIRef(iri)
    head = g.value(s, SCHEMA.image)
    members = list(Collection(g, head)) if head is not None else []
    g -= g.cbd(s)                       # post triples + blank list cells + place/geo
    for m in members:                   # the image IRIs themselves
        for t in list(g.triples((m, None, None))):
            g.remove(t)


def validate(ttl_text: str) -> dict:
    """Best-effort SHACL check (only if pyshacl is installed). Returns
    {"ran": bool, "conforms": bool, "violations": [messages]}.
    """
    try:
        from pyshacl import validate as shacl_validate
    except ImportError:
        return {"ran": False, "conforms": True, "violations": []}

    root = Path(__file__).resolve().parent.parent
    shapes = rdflib.Graph()
    for f in ("amora/shapes.ttl", "definitions/ontology.ttl", "definitions/shapes.ttl"):
        shapes.parse(str(root / f), format="turtle")
    onto = str(root / "definitions" / "ontology.ttl")

    data = rdflib.Graph()
    data.parse(data=ttl_text, format="turtle")
    _, res, _ = shacl_validate(data, shacl_graph=shapes, ont_graph=onto, advanced=True)
    SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")
    viols = [
        str(res.value(r, SH.resultMessage))
        for r in res.subjects(rdflib.RDF.type, SH.ValidationResult)
        if res.value(r, SH.resultSeverity) == SH.Violation
    ]
    return {"ran": True, "conforms": not viols, "violations": sorted(set(viols))}
