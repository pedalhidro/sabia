"""Shared Turtle mapping for the Instagram pullers.

Both fetchers (instaloader scraping and the official Graph API) normalise their
results into a `Post` and call `add_post`, so the RDF mapping to the Pedal
Hidrográfico shapes lives in exactly one place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

try:
    from rdflib import Graph, Literal, Namespace, URIRef, RDF, BNode
    from rdflib.collection import Collection
    from rdflib.namespace import XSD
except ImportError:
    raise SystemExit("Missing dependency: pip install rdflib")

PH = Namespace("https://pedalhidrografi.co/terms#")
DCTERMS = Namespace("http://purl.org/dc/terms/")
SCHEMA = Namespace("https://schema.org/")

MAX_BODY = 2150  # matches InstagramPostShape sh:maxLength

HEADER = (
    "# Gerado pelos scripts de scripts/ — REVISAR.\n"
    "# Anúncios SEM passeio associado (ph:Tour). Local de encontro, contas\n"
    "# marcadas, colaboradoras e o vínculo com o passeio (ph:announces) NÃO vêm\n"
    "# do Instagram — cure à mão em definitions/data_manual.ttl.\n\n"
)


@dataclass
class Post:
    """A channel-agnostic, normalised Instagram post."""

    shortcode: str
    caption: str
    timestamp: datetime                      # timezone-aware
    image_urls: List[str]                    # ordered
    location_name: Optional[str] = None
    location_lat: Optional[float] = None
    location_lng: Optional[float] = None
    location_url: Optional[str] = None       # e.g. IG location page
    tagged: List[str] = field(default_factory=list)        # IG usernames
    collaborators: List[str] = field(default_factory=list)  # IG usernames (Collab)
    is_posted: bool = True                   # pulled posts are already live


def new_graph() -> Graph:
    g = Graph()
    g.bind("ph", PH)
    g.bind("dcterms", DCTERMS)
    g.bind("schema", SCHEMA)
    g.bind("xsd", XSD)
    return g


def _slug(shortcode: str) -> str:
    return shortcode.replace("-", "_")


def post_iri(shortcode: str) -> URIRef:
    """The ph:InstagramPost IRI for a given shortcode (matches add_post)."""
    return URIRef(PH[f"ig-{_slug(shortcode)}"])


def _geo(g: Graph, lat: float, lng: float) -> BNode:
    n = BNode()
    g.add((n, RDF.type, SCHEMA.GeoCoordinates))
    g.add((n, SCHEMA.latitude, Literal(round(lat, 6), datatype=XSD.decimal)))
    g.add((n, SCHEMA.longitude, Literal(round(lng, 6), datatype=XSD.decimal)))
    return n


def add_post(g: Graph, post: Post) -> None:
    """Add one normalised post as a bare ph:InstagramPost (+ images).

    No ph:Tour is generated: the tour↔announcement link is optional now, and
    associating each post with a tour is a manual/curation step (do it in
    definitions/data_manual.ttl via ph:announces if/when wanted).
    """
    sc = _slug(post.shortcode)
    ann = URIRef(PH[f"ig-{sc}"])

    # ── InstagramPost (announcement) ──────────────────────────────────────
    g.add((ann, RDF.type, PH.InstagramPost))
    g.add((ann, PH.isPosted, Literal(post.is_posted)))  # rdflib → xsd:boolean
    # Publish timestamp of the post itself (not a tour date).
    g.add((ann, DCTERMS.date, Literal(post.timestamp.isoformat(), datatype=XSD.dateTime)))
    g.add((ann, SCHEMA.articleBody, Literal((post.caption or "")[:MAX_BODY])))

    if post.location_name:
        place = BNode()
        g.add((place, RDF.type, SCHEMA.Place))
        g.add((place, SCHEMA.name, Literal(post.location_name)))
        if post.location_url:
            g.add((place, SCHEMA.url, URIRef(post.location_url)))
        if post.location_lat is not None and post.location_lng is not None:
            g.add((place, SCHEMA.geo, _geo(g, post.location_lat, post.location_lng)))
        g.add((ann, PH.gatheringLocation, place))

    def handle_iri(h: str) -> URIRef:
        return URIRef(f"https://www.instagram.com/{h.lstrip('@')}/")

    for handle in sorted(post.tagged):
        g.add((ann, PH.tagAccount, handle_iri(handle)))
    for handle in sorted(post.collaborators):
        g.add((ann, PH.collaborator, handle_iri(handle)))

    # ── Images (ordered list of ph:AnnouncementImage; no GPS needed) ───────
    img_nodes = []
    for i, url in enumerate(post.image_urls[:10], start=1):
        img = URIRef(PH[f"ig-{sc}-img{i}"])
        g.add((img, RDF.type, PH.AnnouncementImage))
        g.add((img, SCHEMA.contentUrl, URIRef(url)))
        img_nodes.append(img)
    head = BNode()
    Collection(g, head, img_nodes)
    g.add((ann, SCHEMA.image, head))


def write_ttl(g: Graph, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(HEADER + g.serialize(format="turtle"))
