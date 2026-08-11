"""Append a published announcement to the dataset TTL, reusing scripts/ttl_common
so the output matches the SHACL shapes exactly. Loads the existing graph, adds
the new announcement node (ph:InstagramPost via ttl_common, demais canais via
CHANNELS/add_channel_announcement), and writes it back (works for local files
and gs:// URIs).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Sequence

import rdflib
from rdflib import RDF, BNode, Literal, URIRef
from rdflib.collection import Collection
from rdflib.namespace import PROV, XSD

# Reuse the shared mapping that the pullers use.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ttl_common  # noqa: E402

import storage  # noqa: E402

PH, SCHEMA, DCTERMS = ttl_common.PH, ttl_common.SCHEMA, ttl_common.DCTERMS
ROOT = Path(__file__).resolve().parent.parent

# Tipos DERIVADOS pelas sh:rule (ver definitions/shapes.ttl). Nunca são fato
# armazenado: valem só dentro de uma requisição, recalculados a partir das
# métricas do momento. Se fossem persistidos, um post que ficou popular
# continuaria carregando ph:DeletableInstagramPost do arquivo e furaria a trava
# de engajamento — as regras só ACRESCENTAM tipos, nunca os retiram.
INFERRED_CLASSES = (PH.AppOwnedInstagramPost, PH.DeletableInstagramPost)


# ── Canais de cross-posting (fora o Instagram, que tem fluxo próprio) ─────────
# Espelha definitions/shapes.ttl — a fonte da verdade dos limites é o shape:
#   cls        classe RDF do anúncio
#   prefix     prefixo do IRI (ph:<prefix>-<slug>)
#   text_prop  onde vai o texto (schema:text p/ mensagens, schema:articleBody
#              p/ posts com "corpo")
#   images     "list" = lista RDF ordenada / "single" = um nó direto /
#              "set" = valores soltos / "video" = UM ph:AnnouncementVideo em
#              schema:video / "none"
#   max_*      limites que o shape valida (repetidos aqui pro 400 amigável)
#   needs_*    campos obrigatórios
CHANNELS = {
    "reel": {"cls": PH.InstagramReel, "prefix": "reel", "text_prop": SCHEMA.articleBody,
             "images": "video", "max_images": 1, "max_text": 2150, "max_title": 0,
             "needs_text": True, "needs_title": False},
    "whatsapp": {"cls": PH.WhatsappMessage, "prefix": "wa", "text_prop": SCHEMA.text,
                 "images": "single", "max_images": 1, "max_text": 2150, "max_title": 0,
                 "needs_text": True, "needs_title": False},
    "telegram": {"cls": PH.TelegramMessage, "prefix": "tg", "text_prop": SCHEMA.text,
                 "images": "list", "max_images": 10, "max_text": 1024, "max_title": 0,
                 "needs_text": True, "needs_title": False},
    "mastodon": {"cls": PH.MastodonPost, "prefix": "masto", "text_prop": SCHEMA.articleBody,
                 "images": "list", "max_images": 4, "max_text": 500, "max_title": 0,
                 "needs_text": True, "needs_title": False},
    "reddit": {"cls": PH.RedditPost, "prefix": "rd", "text_prop": SCHEMA.articleBody,
               "images": "set", "max_images": 20, "max_text": 40000, "max_title": 300,
               "needs_text": False, "needs_title": True},
    "email": {"cls": PH.EmailMessage, "prefix": "mail", "text_prop": SCHEMA.articleBody,
              "images": "set", "max_images": 20, "max_text": 100000, "max_title": 255,
              "needs_text": True, "needs_title": True},
    # Agenda (Google Calendar) — o evento que o calendario.pedalhidrografi.co
    # exibe. Sem imagens; além do texto (descrição), leva início/fim/local
    # (needs_event → o /api/publish exige event_start). max_text=2150 DE
    # PROPÓSITO: igual ao WhatsApp/IG, pra não criar degrau novo na escada
    # universal (test_channels.py trava os degraus).
    "gcal": {"cls": PH.CalendarEvent, "prefix": "cal", "text_prop": SCHEMA.description,
             "images": "none", "max_images": 0, "max_text": 2150, "max_title": 255,
             "needs_text": False, "needs_title": True, "needs_event": True},
}


# Limite de texto do Instagram (post e reel) — o fluxo dele é próprio
# (instagram.py), então não está em CHANNELS, mas entra na escada universal.
INSTAGRAM_MAX_TEXT = 2150

# Canais cujo provedor deixa a app APAGAR um anúncio já publicado (Telegram
# deleteMessage, Mastodon DELETE /statuses, Whapi DELETE /messages, Agenda
# events.delete). Reel/e-mail não têm des-envio; o Instagram tem fluxo próprio.
DELETABLE_CHANNELS = {"telegram", "mastodon", "whatsapp", "gcal"}
# Janela de arrependimento: só dá pra apagar anúncio publicado há menos de 24h
# (desfazer um engano fresco, não reescrever história). Gate simples de tempo,
# em Python mesmo — não é regra derivada como as sh:rule da grade do Instagram.
DELETE_WINDOW_HOURS = 24


def announcement_age_ok(date_literal) -> bool:
    """O anúncio ainda está dentro da janela de 24h? (data ilegível = não)."""
    try:
        d = datetime.fromisoformat(str(date_literal))
    except (TypeError, ValueError):
        return False
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - d <= timedelta(hours=DELETE_WINDOW_HOURS)


def text_ladder() -> List[dict]:
    """A escada de blocos do post universal, DERIVADA dos limites de texto dos
    canais: cada degrau é a diferença entre limites consecutivos, então a soma
    dos blocos 1..N é exatamente o limite do canal do degrau N. A
    ph:UniversalPostShape codifica os mesmos números (o SHACL é estático) —
    test_channels.py confere que os dois batem."""
    all_limits = {k: s["max_text"] for k, s in CHANNELS.items()}
    all_limits["instagram"] = INSTAGRAM_MAX_TEXT
    ladder, prev = [], 0
    for lim in sorted(set(all_limits.values())):
        ladder.append({
            "limit": lim,
            "budget": lim - prev,
            "channels": sorted(k for k, v in all_limits.items() if v >= lim),
        })
        prev = lim
    return ladder


# Shape de cada canal — pra ler as anotações de espec. de mídia.
SHAPE_BY_CHANNEL = {
    "instagram": PH.InstagramPostShape,
    "reel": PH.InstagramReelShape,
    "whatsapp": PH.WhatsappMessageShape,
    "telegram": PH.TelegramMessageShape,
    "mastodon": PH.MastodonPostShape,
    "reddit": PH.RedditPostShape,
    "email": PH.EmailMessageShape,
    "gcal": PH.CalendarEventShape,
}

_media_specs_cache: Optional[dict] = None


def media_specs() -> dict:
    """Espec. de mídia por canal, LIDA DAS SHAPES (definitions/shapes.ttl é a
    fonte da verdade): proporção recomendada/aceita, tamanho máximo (bytes) e
    formatos MIME aceitos — imagem e vídeo. A app usa isso na UI (hints,
    accept do <input>) e nas checagens do /api/publish."""
    global _media_specs_cache
    if _media_specs_cache is not None:
        return _media_specs_cache
    g = rdflib.Graph()
    g.parse(str(ROOT / "definitions" / "shapes.ttl"), format="turtle")

    def num(shape, prop):
        v = g.value(shape, prop)
        return float(v) if v is not None else None

    specs = {}
    for channel, shape in SHAPE_BY_CHANNEL.items():
        specs[channel] = {
            "recommended_ratio": num(shape, PH.recommendedAspectRatio),
            "min_ratio": num(shape, PH.minAspectRatio),
            "max_ratio": num(shape, PH.maxAspectRatio),
            "max_image_bytes": (lambda v: int(v) if v else None)(num(shape, PH.maxImageBytes)),
            "image_formats": sorted(str(f) for f in g.objects(shape, PH.acceptedImageFormat)),
            "max_video_bytes": (lambda v: int(v) if v else None)(num(shape, PH.maxVideoBytes)),
            "video_formats": sorted(str(f) for f in g.objects(shape, PH.acceptedVideoFormat)),
            "min_video_seconds": (lambda v: int(v) if v else None)(num(shape, PH.minVideoSeconds)),
            "max_video_seconds": (lambda v: int(v) if v else None)(num(shape, PH.maxVideoSeconds)),
        }
    _media_specs_cache = specs
    return specs


def post_iri(shortcode: str) -> URIRef:
    return ttl_common.post_iri(shortcode)


def load_dataset() -> rdflib.Graph:
    g = ttl_common.new_graph()
    text = storage.read_ttl()
    if text.strip():
        g.parse(data=text, format="turtle")
    _strip_inferred(g)  # cura arquivos escritos por versões que os persistiam
    return g


def _strip_inferred(g: rdflib.Graph) -> None:
    for cls in INFERRED_CLASSES:
        for t in list(g.triples((None, RDF.type, cls))):
            g.remove(t)


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
    derived_from: Optional[str] = None,  # IRI do ph:UniversalPost de origem
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
    if derived_from:
        g.add((iri, PROV.wasDerivedFrom, URIRef(derived_from)))

    out = g.serialize(format="turtle")
    storage.write_ttl(out)
    return out


# ── Demais canais: gravar e listar anúncios ───────────────────────────────────
def add_channel_announcement(
    channel: str,
    slug: str,
    *,
    text: Optional[str] = None,
    title: Optional[str] = None,
    image_urls: Sequence[str] = (),
    image_meta: Optional[Sequence[tuple]] = None,  # [(bytes, mime)] alinhado c/ urls
    is_posted: bool = True,
    permalink: Optional[str] = None,
    derived_from: Optional[str] = None,  # IRI do ph:UniversalPost de origem
    when: Optional[datetime] = None,
    event_start: Optional[str] = None,   # ISO-8601 (Agenda: schema:startDate)
    event_end: Optional[str] = None,
    event_location: Optional[str] = None,
    provider_id: Optional[str] = None,   # id no provedor (habilita apagar ≤24h)
) -> str:
    """Grava um anúncio de canal ≠ Instagram no dataset, na forma que o shape
    do canal espera (texto/título no lugar certo, imagens como lista ordenada,
    nó único ou conjunto). image_meta declara tamanho/formato de cada imagem
    (habilita os Avisos de espec. de mídia); derived_from liga o anúncio ao
    post universal de origem (prov:wasDerivedFrom); event_* são os campos do
    evento de agenda (gcal). Devolve o Turtle."""
    spec = CHANNELS[channel]
    g = load_dataset()
    iri = URIRef(PH[f"{spec['prefix']}-{slug}"])

    g.add((iri, RDF.type, spec["cls"]))
    g.add((iri, DCTERMS.date, Literal((when or datetime.now(timezone.utc)).isoformat(),
                                      datatype=XSD.dateTime)))
    g.add((iri, PH.isPosted, Literal(bool(is_posted))))
    g.add((iri, PH.managedByApp, Literal(True)))
    if title:
        g.add((iri, DCTERMS.title, Literal(title)))
    if text:
        g.add((iri, spec["text_prop"], Literal(text)))
    if event_start:
        g.add((iri, SCHEMA.startDate, Literal(event_start, datatype=XSD.dateTime)))
    if event_end:
        g.add((iri, SCHEMA.endDate, Literal(event_end, datatype=XSD.dateTime)))
    if event_location:
        g.add((iri, SCHEMA.location, Literal(event_location)))
    if permalink:
        g.add((iri, PH.permalink, Literal(permalink, datatype=XSD.anyURI)))
    if provider_id:
        g.add((iri, PH.providerMessageId, Literal(provider_id)))
    if derived_from:
        g.add((iri, PROV.wasDerivedFrom, URIRef(derived_from)))

    is_video = spec["images"] == "video"
    media_cls = PH.AnnouncementVideo if is_video else PH.AnnouncementImage
    suffix = "vid" if is_video else "img"
    imgs = []
    for i, url in enumerate(list(image_urls)[: spec["max_images"]], start=1):
        img = URIRef(PH[f"{spec['prefix']}-{slug}-{suffix}{i}"])
        g.add((img, RDF.type, media_cls))
        g.add((img, SCHEMA.contentUrl, URIRef(url)))
        if image_meta and i - 1 < len(image_meta):
            size, mime = image_meta[i - 1]
            if size:
                g.add((img, SCHEMA.contentSize, Literal(int(size))))
            if mime:
                g.add((img, SCHEMA.encodingFormat, Literal(mime)))
        imgs.append(img)
    if imgs:
        if is_video:                      # Reel: UM vídeo em schema:video
            g.add((iri, SCHEMA.video, imgs[0]))
        elif spec["images"] == "list":    # ordem importa → lista RDF
            head = BNode()
            Collection(g, head, imgs)
            g.add((iri, SCHEMA.image, head))
        elif spec["images"] == "single":  # WhatsApp: um nó direto
            g.add((iri, SCHEMA.image, imgs[0]))
        else:                             # conjunto sem ordem
            for img in imgs:
                g.add((iri, SCHEMA.image, img))

    out = g.serialize(format="turtle")
    storage.write_ttl(out)
    return out


def add_universal_post(
    slug: str,
    *,
    blocks: Sequence[str],
    title: Optional[str] = None,
    image_urls: Sequence[str] = (),
    image_meta: Optional[Sequence[tuple]] = None,
    when: Optional[datetime] = None,
) -> tuple:
    """Grava a MATRIZ do cross-post (ph:UniversalPost): blocos de texto em
    escada (lista RDF) + imagens originais em ordem. Os anúncios derivados
    apontam de volta com prov:wasDerivedFrom. Devolve (iri, turtle)."""
    g = load_dataset()
    iri = URIRef(PH[f"up-{slug}"])
    g.add((iri, RDF.type, PH.UniversalPost))
    g.add((iri, DCTERMS.date, Literal((when or datetime.now(timezone.utc)).isoformat(),
                                      datatype=XSD.dateTime)))
    if title:
        g.add((iri, DCTERMS.title, Literal(title)))

    head = BNode()
    Collection(g, head, [Literal(b) for b in blocks])
    g.add((iri, PH.textBlocks, head))

    imgs = []
    for i, url in enumerate(list(image_urls)[:20], start=1):
        img = URIRef(PH[f"up-{slug}-img{i}"])
        g.add((img, RDF.type, PH.AnnouncementImage))
        g.add((img, SCHEMA.contentUrl, URIRef(url)))
        if image_meta and i - 1 < len(image_meta):
            size, mime = image_meta[i - 1]
            if size:
                g.add((img, SCHEMA.contentSize, Literal(int(size))))
            if mime:
                g.add((img, SCHEMA.encodingFormat, Literal(mime)))
        imgs.append(img)
    if imgs:
        ihead = BNode()
        Collection(g, ihead, imgs)
        g.add((iri, SCHEMA.image, ihead))

    out = g.serialize(format="turtle")
    storage.write_ttl(out)
    return str(iri), out


def universal_posts(g: rdflib.Graph) -> List[dict]:
    """Posts universais gravados, mais novos primeiro (pra lista da aba)."""
    out = []
    for s in g.subjects(RDF.type, PH.UniversalPost):
        bhead = g.value(s, PH.textBlocks)
        blocks = [str(b) for b in Collection(g, bhead)] if bhead is not None else []
        ihead = g.value(s, SCHEMA.image)
        members = list(Collection(g, ihead)) if ihead is not None else []
        thumb = g.value(members[0], SCHEMA.contentUrl) if members else None
        out.append({
            "iri": str(s),
            "title": str(g.value(s, DCTERMS.title) or ""),
            "text": (blocks[0] if blocks else "")[:200],
            "blocks": len(blocks),
            "images": len(members),
            "date": str(g.value(s, DCTERMS.date) or ""),
            "permalink": "",
            "is_posted": True,   # a matriz não é postada em si — sem pílula de rascunho
            "thumb": str(thumb) if thumb else None,
        })
    out.sort(key=lambda p: p["date"], reverse=True)
    return out


def channel_announcements(g: rdflib.Graph, channel: str) -> List[dict]:
    """Anúncios de um canal, mais novos primeiro (pra lista da aba)."""
    spec = CHANNELS[channel]
    out = []
    for s in g.subjects(RDF.type, spec["cls"]):
        thumb = None
        head = g.value(s, SCHEMA.image)
        if head is not None:
            if spec["images"] == "list":
                members = list(Collection(g, head))
                thumb = g.value(members[0], SCHEMA.contentUrl) if members else None
            else:
                thumb = g.value(head, SCHEMA.contentUrl)
        # vídeo (Reel) não vira thumb — <img> não renderiza MP4; a lista mostra só o texto
        posted = str(g.value(s, PH.isPosted)).lower() == "true"
        pid = g.value(s, PH.providerMessageId)
        date = g.value(s, DCTERMS.date)
        # Rascunho apaga sempre (é só registro); publicado, só nos canais com
        # des-envio, com o id do provedor gravado e dentro da janela de 24h.
        deletable = (not posted) or (channel in DELETABLE_CHANNELS
                                     and pid is not None
                                     and announcement_age_ok(date))
        out.append({
            "iri": str(s),
            "title": str(g.value(s, DCTERMS.title) or ""),
            "text": str(g.value(s, spec["text_prop"]) or "")[:200],
            "date": str(date or ""),
            "permalink": str(g.value(s, PH.permalink) or ""),
            "is_posted": posted,
            "thumb": str(thumb) if thumb else None,
            "deletable": deletable,
        })
    out.sort(key=lambda p: p["date"], reverse=True)
    return out


def find_announcement(g: rdflib.Graph, iri: str):
    """(canal, {posted, provider_id, date}) do anúncio, ou (None, None)."""
    s = URIRef(iri)
    for channel, spec in CHANNELS.items():
        if (s, RDF.type, spec["cls"]) in g:
            pid = g.value(s, PH.providerMessageId)
            return channel, {
                "posted": str(g.value(s, PH.isPosted)).lower() == "true",
                "provider_id": str(pid) if pid is not None else None,
                "date": g.value(s, DCTERMS.date),
            }
    return None, None


def remove_announcement(g: rdflib.Graph, iri: str, channel: str) -> None:
    """Apaga o nó do anúncio e as mídias dele, na forma do canal (lista RDF,
    nó único, conjunto ou vídeo) — o remove_post cobre só o caso lista (IG)."""
    spec = CHANNELS[channel]
    s = URIRef(iri)
    if spec["images"] == "list":
        head = g.value(s, SCHEMA.image)
        media = list(Collection(g, head)) if head is not None else []
    elif spec["images"] == "single":
        media = [m for m in (g.value(s, SCHEMA.image),) if m is not None]
    elif spec["images"] == "video":
        media = [m for m in (g.value(s, SCHEMA.video),) if m is not None]
    else:
        media = list(g.objects(s, SCHEMA.image))
    g -= g.cbd(s)                       # nó do anúncio + células da lista
    for m in media:
        for t in list(g.triples((m, None, None))):
            g.remove(t)


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


def classify(g: rdflib.Graph) -> rdflib.Graph:
    """Executa as sh:rule (SHACL-AF) e devolve uma CÓPIA com os tipos
    ph:AppOwnedInstagramPost / ph:DeletableInstagramPost materializados.

    `g` NÃO é tocado: quem chama pode salvá-lo sem gravar tipos derivados no
    .ttl (ver INFERRED_CLASSES). Sem pyshacl, a cópia volta sem os tipos — e
    nada é considerado removível, que é o padrão seguro.
    """
    inferred = ttl_common.new_graph()
    inferred += g
    try:
        from pyshacl import validate as shacl_validate
    except ImportError:
        return inferred
    shapes = rdflib.Graph()
    for f in ("definitions/ontology.ttl", "definitions/shapes.ttl"):
        shapes.parse(str(ROOT / f), format="turtle")
    shacl_validate(inferred, shacl_graph=shapes, advanced=True, inplace=True)
    return inferred


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
