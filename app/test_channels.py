"""Testa o cross-posting nos canais ≠ Instagram, contra APIs FALSAS.

Não toca em conta nenhuma: `urlopen` (Whapi/Telegram/Mastodon/Reddit/Twilio) e
`smtplib` são substituídos. Roda sem pytest:

    cd app && python test_channels.py

Cobre as propriedades que importam:
  1. publicar em cada canal grava um anúncio que conforma o SHACL, na FORMA
     do shape do canal (texto no lugar certo; imagem única no WhatsApp, lista
     ordenada no Telegram/Mastodon, conjunto no Reddit/e-mail);
  2. os limites do canal viram 400 (texto longo, imagem demais, formato/tamanho
     fora da espec. de mídia das shapes);
  3. publicar AO VIVO sem confirmação → 409; canal sem config → 400;
  4. rascunho (is_posted=false) não chama a rede e fica ph:isPosted false;
  5. /api/announcements lista o que foi gravado.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path

APP = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="sabia-channels-"))

os.environ.update(
    GCS_BUCKET="", DRY_RUN="false", APP_PASSWORD="",
    IG_ACCESS_TOKEN="IGfake", IG_MANAGE_TOKEN="EAAfake", IG_USER_ID="me",
    DATA_TTL=str(TMP / "data.ttl"), LOCAL_UPLOAD_DIR=str(TMP / "uploads"),
    WHAPI_TOKEN="wh-fake", WHAPI_ANNOUNCE_GROUP="123@g.us",
    TELEGRAM_BOT_TOKEN="tg-fake", TELEGRAM_CHAT_ID="@pedalhidro",
    MASTODON_BASE_URL="https://masto.example", MASTODON_ACCESS_TOKEN="ms-fake",
    REDDIT_CLIENT_ID="rc", REDDIT_CLIENT_SECRET="rs", REDDIT_USERNAME="ru",
    REDDIT_PASSWORD="rp", REDDIT_SUBREDDIT="pedalhidro",
    SMTP_HOST="smtp.example", SMTP_PORT="587", SMTP_USER="mail",
    SMTP_PASSWORD="pw", EMAIL_FROM="ph@example.org", EMAIL_TO="lista@example.org",
    GCAL_CALENDAR_ID="agenda-fake@group.calendar.google.com",
    ANTHROPIC_API_KEY="sk-fake",
)
sys.path.insert(0, str(APP))

import channels  # noqa: E402
import instagram  # noqa: E402

# ── APIs falsas ───────────────────────────────────────────────────────────────
CALLS: list[tuple[str, str]] = []   # (host+path, corpo decodificado)


class _Resp(io.BytesIO):
    status = 200
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _json(obj, status=200) -> _Resp:
    r = _Resp(json.dumps(obj).encode())
    r.status = status
    return r


def fake_urlopen(req, *a, **kw):
    url = req if isinstance(req, str) else req.full_url
    body = (req.data or b"").decode("utf-8", "replace") if not isinstance(req, str) else ""
    method = "GET" if isinstance(req, str) else req.get_method()
    CALLS.append((url, body[:200]))
    # Des-envio (DELETE) por canal — antes dos handlers de publicação.
    if method == "DELETE":
        if "masto.example/api/v1/statuses/" in url:
            return _json({"id": "t1"})
        if "whapi" in url and "/messages/" in url:
            return _json({"success": True})
        if "googleapis.com/calendar" in url and "/events/" in url:
            r = _Resp(b"")          # events.delete responde 204 sem corpo
            r.status = 204
            return r
    if "whapi" in url:
        return _json({"sent": True, "message": {"id": "wa-1"}})
    if "api.telegram.org" in url:
        if "deleteMessage" in url:
            return _json({"ok": True, "result": True})
        if "sendMediaGroup" in url:
            return _json({"ok": True, "result": [{"message_id": 42}]})
        return _json({"ok": True, "result": {"message_id": 42}})
    if "masto.example/api/v2/media" in url:
        return _json({"id": "m1"}, status=202)
    if "masto.example/api/v1/media/" in url:
        return _json({"id": "m1"}, status=200)
    if "masto.example/api/v1/statuses" in url:
        return _json({"id": "t1", "url": "https://masto.example/@ph/t1"})
    if "reddit.com/api/v1/access_token" in url:
        return _json({"access_token": "rt"})
    if "oauth.reddit.com/api/submit" in url:
        return _json({"json": {"errors": [], "data": {"id": "abc",
                      "url": "https://reddit.com/r/pedalhidro/abc"}}})
    if "googleapis.com/calendar/v3/calendars/" in url:
        body_j = json.loads(body)
        assert body_j.get("summary") and body_j["start"]["dateTime"] and body_j["end"]["dateTime"], body
        return _json({"id": "ev1", "htmlLink": "https://www.google.com/calendar/event?eid=ev1"})
    # Renovação do IG_ACCESS_TOKEN + relógio do IG_MANAGE_TOKEN (tokens.py).
    if "refresh_access_token" in url:
        assert "grant_type=ig_refresh_token" in url, url
        return _json({"access_token": "IGrenewed", "token_type": "bearer",
                      "expires_in": 5184000})
    if "debug_token" in url:
        return _json({"data": {"is_valid": True,
                               "data_access_expires_at": 9_000_000_000}})
    # Graph API do Instagram (Reel): container REELS → status → publish → permalink
    if "graph.instagram.com" in url or "graph.facebook.com" in url:
        node = url.split("/v21.0/", 1)[1].partition("?")[0]
        if node.endswith("/media"):
            assert "media_type=REELS" in body and "video_url=" in body, body
            return _json({"id": "REELCONTAINER"})
        if node == "REELCONTAINER":
            return _json({"status_code": "FINISHED"})
        if node.endswith("/media_publish"):
            return _json({"id": "REELMEDIA"})
        if node == "REELMEDIA":
            return _json({"permalink": "https://www.instagram.com/reel/DFakeReel/"})
    raise AssertionError(f"URL inesperada: {url}")


channels.urllib.request.urlopen = fake_urlopen
instagram.urllib.request.urlopen = fake_urlopen
channels._google_token = lambda: "gcal-fake-token"   # sem ADC nos testes

SMTP_SENT: list = []


class FakeSMTP:
    def __init__(self, host, port, timeout=None): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self): pass
    def login(self, u, p): pass
    def send_message(self, msg): SMTP_SENT.append(msg)


channels.smtplib.SMTP = FakeSMTP
channels.smtplib.SMTP_SSL = FakeSMTP

from fastapi.testclient import TestClient  # noqa: E402
import rdflib  # noqa: E402
from rdflib import RDF  # noqa: E402
from rdflib.collection import Collection  # noqa: E402

import main  # noqa: E402
import ttl_store  # noqa: E402

client = TestClient(main.app)
PNG = bytes.fromhex("89504e470d0a1a0a0000000d494844520000000100000001080600000"
                    "01f15c4890000000d4944415478da6364f8cf000000030101002718e3"
                    "660000000049454e44ae426082")
PH, SCHEMA = ttl_store.PH, ttl_store.SCHEMA
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))
    if not ok:
        FAILURES.append(name)


def publish(channel: str, *, images=0, mime="image/png", confirm=True, posted=True, **fields):
    data = {"channel": channel, "is_posted": str(posted).lower(),
            "confirm": str(confirm).lower(), **fields}
    ext = ".mp4" if mime.startswith("video/") else ".png"
    return client.post("/api/publish", data=data,
                       files=[("images", (f"f{i}{ext}", PNG, mime)) for i in range(images)])


def dataset() -> rdflib.Graph:
    g = rdflib.Graph()
    g.parse(str(TMP / "data.ttl"), format="turtle")
    return g


print("1) publicar em cada canal grava na forma do shape e conforma o SHACL")
r = publish("reel", text="Reel do passeio — legenda igual à de post #pedal", images=1, mime="video/mp4")
check("reel publica", r.status_code == 200 and r.json()["ok"], r.text[:120])
check("reel: permalink de reel", "instagram.com/reel" in r.json()["result"]["permalink"])
check("reel: SHACL conforma", r.json()["validation"]["conforms"],
      str(r.json()["validation"]["violations"]))
g = dataset()
reel = next(g.subjects(RDF.type, PH.InstagramReel))
check("reel: legenda em schema:articleBody", g.value(reel, SCHEMA.articleBody) is not None)
vid = g.value(reel, SCHEMA.video)
check("reel: UM vídeo em schema:video → ph:AnnouncementVideo",
      vid is not None and (vid, RDF.type, PH.AnnouncementVideo) in g)
check("reel: vídeo declara formato",
      str(g.value(vid, SCHEMA.encodingFormat)) == "video/mp4")

r = publish("whatsapp", text="Bora pedalar domingo!", images=1)
check("whatsapp publica", r.status_code == 200 and r.json()["ok"], r.text[:120])
check("whatsapp: SHACL conforma", r.json()["validation"]["conforms"],
      str(r.json()["validation"]["violations"]))
g = dataset()
wa = next(g.subjects(RDF.type, PH.WhatsappMessage))
check("whatsapp: texto em schema:text", g.value(wa, SCHEMA.text) is not None)
check("whatsapp: imagem única (nó direto, não lista)",
      (g.value(wa, SCHEMA.image), RDF.type, PH.AnnouncementImage) in g)
check("whatsapp: imagem declara tamanho e formato",
      g.value(g.value(wa, SCHEMA.image), SCHEMA.contentSize) is not None
      and str(g.value(g.value(wa, SCHEMA.image), SCHEMA.encodingFormat)) == "image/png")

r = publish("telegram", text="Passeio 8h na praça", images=3)
check("telegram publica", r.status_code == 200 and r.json()["ok"], r.text[:120])
check("telegram: permalink t.me", "t.me/pedalhidro/42" in r.json()["result"]["permalink"])
g = dataset()
tg = next(g.subjects(RDF.type, PH.TelegramMessage))
check("telegram: imagens em lista RDF ordenada",
      len(list(Collection(g, g.value(tg, SCHEMA.image)))) == 3)

r = publish("mastodon", text="Toot do passeio", images=2,
            alts=["grupo pedala à beira do córrego", "cartaz do passeio com data e local"])
check("mastodon publica", r.status_code == 200 and r.json()["ok"], r.text[:120])
check("mastodon: subiu mídia antes do status",
      any("v2/media" in u for u, _ in CALLS) and any("v1/statuses" in u for u, _ in CALLS))
check("mastodon: alt foi no description do upload",
      any("v2/media" in u and "description" in b and "córrego" in b for u, b in CALLS))
g = dataset()
masto = next(g.subjects(RDF.type, PH.MastodonPost))
first_img = next(iter(Collection(g, g.value(masto, SCHEMA.image))))
check("mastodon: alt gravado em schema:description",
      str(g.value(first_img, SCHEMA.description) or "").startswith("grupo pedala"))

r = publish("reddit", title="Pedal das águas", text="Detalhes do passeio", images=1)
check("reddit publica", r.status_code == 200 and r.json()["ok"], r.text[:120])
g = dataset()
rd = next(g.subjects(RDF.type, PH.RedditPost))
check("reddit: título em dcterms:title", g.value(rd, ttl_store.DCTERMS.title) is not None)
check("reddit: link da foto foi no corpo enviado (não no .ttl)",
      any("oauth.reddit.com" in u and "Foto+1" in b for u, b in CALLS))

r = publish("email", title="Boletim do pedal", text="Olá!", images=1)
check("email publica", r.status_code == 200 and r.json()["ok"], r.text[:120])
check("email: mensagem passou pelo SMTP com anexo",
      len(SMTP_SENT) == 1 and len(list(SMTP_SENT[0].iter_attachments())) == 1)

r = publish("gcal", title="Pedal das nascentes", text="Saída 8h, 25km.",
            event_start="2026-08-16T08:00", event_location="Praça da Nascente")
check("gcal publica", r.status_code == 200 and r.json()["ok"], r.text[:120])
check("gcal: permalink da agenda", "google.com/calendar" in r.json()["result"]["permalink"])
check("gcal: mandou o evento pra API com a agenda certa",
      any("calendars/agenda-fake%40group.calendar.google.com/events" in u for u, _ in CALLS))
g = dataset()
ev = next(g.subjects(RDF.type, PH.CalendarEvent))
check("gcal: título em dcterms:title", g.value(ev, ttl_store.DCTERMS.title) is not None)
check("gcal: descrição em schema:description", g.value(ev, SCHEMA.description) is not None)
check("gcal: início xsd:dateTime", str(g.value(ev, SCHEMA.startDate)) == "2026-08-16T08:00:00")
check("gcal: fim padrão = início + 3h", str(g.value(ev, SCHEMA.endDate)) == "2026-08-16T11:00:00")
check("gcal: local em schema:location", str(g.value(ev, SCHEMA.location)) == "Praça da Nascente")
check("SHACL do dataset inteiro conforma", r.json()["validation"]["conforms"],
      str(r.json()["validation"]["violations"]))

print("\n2) limites do canal viram 400")
r = publish("whatsapp", text="oi", images=2)
check("whatsapp 2 imagens → 400", r.status_code == 400, f"got {r.status_code}")
r = publish("mastodon", text="x" * 501)
check("mastodon >500 chars → 400", r.status_code == 400, f"got {r.status_code}")
r = publish("reddit", text="sem título")
check("reddit sem título → 400", r.status_code == 400, f"got {r.status_code}")
r = publish("telegram", text="oi", images=1, mime="image/webp")
check("formato fora da espec. do shape → 400", r.status_code == 400
      and "webp" in r.text, r.text[:120])
r = publish("reel", text="sem vídeo")
check("reel sem vídeo → 400", r.status_code == 400, f"got {r.status_code}")
r = publish("reel", text="formato errado", images=1, mime="video/webm")
check("reel com formato não aceito → 400", r.status_code == 400
      and "webm" in r.text, r.text[:120])
r = publish("gcal", title="Sem data")
check("gcal sem início → 400", r.status_code == 400 and "início" in r.text, r.text[:120])
r = publish("gcal", title="Data torta", event_start="amanhã cedo")
check("gcal com início inválido → 400", r.status_code == 400, f"got {r.status_code}")
r = publish("gcal", title="Fim antes", event_start="2026-08-16T08:00", event_end="2026-08-16T07:00")
check("gcal com fim antes do início → 400", r.status_code == 400, f"got {r.status_code}")
r = publish("gcal", text="sem título", event_start="2026-08-16T08:00")
check("gcal sem título → 400", r.status_code == 400, f"got {r.status_code}")
r = publish("mastodon", text="toot sem alt", images=1)
check("mastodon com imagem SEM alt → 400", r.status_code == 400
      and "alternativo" in r.text, r.text[:140])
r = publish("whatsapp", text="whatsapp sem alt passa (Warning só)", images=1)
check("whatsapp sem alt → 200 (alt é ideal, não exigência)",
      r.status_code == 200 and r.json()["validation"]["conforms"], r.text[:140])

print("\n3) travas de publicação ao vivo")
r = publish("whatsapp", text="oi", confirm=False)
check("sem confirmação → 409", r.status_code == 409, f"got {r.status_code}")
old = os.environ["WHAPI_TOKEN"]
from config import Config  # noqa: E402
Config.WHAPI_TOKEN = ""
r = publish("whatsapp", text="oi")
check("canal sem config → 400 citando env", r.status_code == 400
      and "WHAPI_TOKEN" in r.text, r.text[:140])
Config.WHAPI_TOKEN = old

print("\n4) rascunho não chama a rede")
n = len(CALLS)
r = publish("telegram", text="rascunho do anúncio", posted=False)
check("rascunho responde ok", r.status_code == 200 and r.json()["ok"], r.text[:120])
check("nenhuma chamada de rede", len(CALLS) == n)
g = dataset()
drafts = [s for s in g.subjects(RDF.type, PH.TelegramMessage)
          if str(g.value(s, PH.isPosted)).lower() == "false"]
check("gravado com ph:isPosted false", len(drafts) == 1)

print("\n5) post universal: escada de blocos, registro e proveniência")
# 5a. os degraus da shape (SHACL estático) batem com a escada derivada da
#     tabela de canais — mudou limite de canal, a shape TEM que acompanhar.
ladder = ttl_store.text_ladder()
check("escada derivada: 5 degraus 500/524/1126/37850/60000",
      [t["budget"] for t in ladder] == [500, 524, 1126, 37850, 60000],
      str([t["budget"] for t in ladder]))
shapes_g = rdflib.Graph()
shapes_g.parse(str(APP.parent / "definitions" / "shapes.ttl"), format="turtle")
SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")
shape_budgets = []
for ps in shapes_g.objects(ttl_store.PH.UniversalPostShape, SH.property):
    path = shapes_g.value(ps, SH.path)
    maxlen = shapes_g.value(ps, SH.maxLength)
    if not isinstance(path, rdflib.BNode) or maxlen is None:
        continue  # caminhos escalares (dcterms:title etc.) ficam de fora
    steps = list(rdflib.collection.Collection(shapes_g, path))
    # caminho de bloco: ( ph:textBlocks rdf:rest×N rdf:first )
    if (len(steps) >= 2 and steps[0] == ttl_store.PH.textBlocks
            and steps[-1] == RDF.first
            and all(s == RDF.rest for s in steps[1:-1])):
        shape_budgets.append((len(steps) - 2, int(maxlen)))  # nº de rdf:rest = posição
shape_budgets = [b for _, b in sorted(shape_budgets)]
check("shape codifica os MESMOS degraus da escada derivada",
      shape_budgets == [t["budget"] for t in ladder], str(shape_budgets))

# 5b. registrar um universal grava blocos como lista RDF e conforma o SHACL.
def publish_universal(blocks, title="", images=0):
    data = {"channel": "universal", "title": title, "blocks": blocks}
    return client.post("/api/publish", data=data,
                       files=[("images", (f"u{i}.png", PNG, "image/png")) for i in range(images)])

r = publish_universal(["Passeio domingo 8h!", "Detalhes: 25km seguindo o Ipiranga."],
                      title="Pedal das nascentes", images=2)
check("universal registra", r.status_code == 200 and r.json()["ok"], r.text[:150])
check("universal: SHACL conforma", r.json()["validation"]["conforms"],
      str(r.json()["validation"]["violations"]))
up_iri = r.json()["iri"]
g = dataset()
up = rdflib.URIRef(up_iri)
blocks_head = g.value(up, PH.textBlocks)
members = list(Collection(g, blocks_head))
check("universal: 2 blocos em lista RDF ordenada",
      len(members) == 2 and str(members[0]).startswith("Passeio"))

# 5c. orçamentos e sequência viram 400.
r = publish_universal(["x" * 501])
check("bloco 1 >500 → 400", r.status_code == 400, f"got {r.status_code}")
r = publish_universal(["oi", "x" * 525])
check("bloco 2 >524 → 400", r.status_code == 400, f"got {r.status_code}")
r = publish_universal(["oi", "   ", "pulei o 2"])
check("bloco pulado → 400", r.status_code == 400, f"got {r.status_code}")
r = publish_universal(["b"] * 6)
check("6 blocos → 400", r.status_code == 400, f"got {r.status_code}")

# 5d. publicar num canal com derived_from grava prov:wasDerivedFrom.
r = publish("whatsapp", text="Passeio domingo 8h!", derived_from=up_iri)
check("derivado publica", r.status_code == 200 and r.json()["ok"], r.text[:120])
g = dataset()
PROV = rdflib.Namespace("http://www.w3.org/ns/prov#")
derived = [s for s in g.subjects(PROV.wasDerivedFrom, up)]
check("anúncio aponta pro universal (prov:wasDerivedFrom)", len(derived) == 1,
      str(derived))
check("SHACL segue conforme com universal + derivado no grafo",
      r.json()["validation"]["conforms"], str(r.json()["validation"]["violations"]))

print("\n6) /api/announcements lista o canal")
r = client.get("/api/announcements", params={"channel": "telegram"})
items = r.json()
check("lista os 2 anúncios de telegram", len(items) == 2, f"len={len(items)}")
check("rascunho vem marcado", any(not i["is_posted"] for i in items))
r = client.get("/api/announcements", params={"channel": "universal"})
check("lista o post universal", len(r.json()) == 1 and r.json()[0]["blocks"] == 2,
      r.text[:120])
r = client.get("/api/announcements", params={"channel": "nope"})
check("canal desconhecido → 400", r.status_code == 400)

print("\n7) auto-renovação do IG_ACCESS_TOKEN (/api/token-refresh)")
import tokens as tokens_mod  # noqa: E402
from config import Config as Cfg  # noqa: E402
tokens_mod.ENV_PATH = TMP / "dotenv"          # NUNCA tocar no .env real
tokens_mod.ENV_PATH.write_text("IG_ACCESS_TOKEN=IGfake\n", encoding="utf-8")
r = client.post("/api/token-refresh")
j = r.json()
check("renova e responde 200", r.status_code == 200 and j.get("refreshed"), r.text[:150])
check("validade reportada em dias", j.get("expires_in_days") == 60, str(j.get("expires_in_days")))
check("token trocado em memória (vale já)", Cfg.IG_ACCESS_TOKEN == "IGrenewed")
check("persistiu no .env (modo local)",
      "IG_ACCESS_TOKEN=IGrenewed" in tokens_mod.ENV_PATH.read_text(encoding="utf-8"))
check("resposta NÃO vaza o token", "IGrenewed" not in r.text and "IGfake" not in r.text, r.text[:200])
check("manage: relógio de acesso a dados presente",
      j.get("manage", {}).get("configured") and j["manage"].get("data_access_days_left", 0) > 0,
      r.text[:200])
cfg = client.get("/api/config").json()
check("/api/config expõe o relógio pro aviso da UI",
      cfg.get("manage_token", {}).get("configured")
      and cfg["manage_token"].get("data_access_days_left", 0) > 0
      and cfg["manage_token"].get("data_access_expires_on", "").count("-") == 2,
      json.dumps(cfg.get("manage_token", {})))

print("\n8) apagar anúncios: janela de 24h, des-envio no provedor, rascunhos")
from datetime import datetime, timezone  # noqa: E402

def delete_ann(iri):
    return client.post("/api/announcements/delete", data={"iri": iri})

# publicado agora → deletable, apaga na rede e some do dataset
r = publish("telegram", text="apaga eu")
items = client.get("/api/announcements", params={"channel": "telegram"}).json()
check("recém-publicado vem deletable", items[0]["deletable"] is True, str(items[0]))
r = delete_ann(items[0]["iri"])
check("telegram: apaga e responde ok", r.status_code == 200 and r.json()["ok"], r.text[:150])
check("telegram: chamou deleteMessage", any("deleteMessage" in u for u, _ in CALLS))
g = dataset()
check("telegram: registro sumiu", (rdflib.URIRef(items[0]["iri"]), None, None) not in g)

# mastodon, whatsapp e gcal: mesmo fluxo, cada um no endpoint do provedor
r = publish("mastodon", text="toot pra apagar")
iri = client.get("/api/announcements", params={"channel": "mastodon"}).json()[0]["iri"]
r = delete_ann(iri)
check("mastodon: DELETE /statuses", r.status_code == 200
      and any("statuses/t1" in u for u, _ in CALLS), r.text[:120])
r = publish("whatsapp", text="zap pra apagar")
iri = client.get("/api/announcements", params={"channel": "whatsapp"}).json()[0]["iri"]
r = delete_ann(iri)
check("whatsapp: DELETE /messages", r.status_code == 200
      and any("/messages/wa-1" in u for u, _ in CALLS), r.text[:120])
r = publish("gcal", title="evento pra apagar", event_start="2026-08-16T08:00")
iri = client.get("/api/announcements", params={"channel": "gcal"}).json()[0]["iri"]
r = delete_ann(iri)
check("gcal: DELETE /events (204 sem corpo)", r.status_code == 200
      and any("/events/ev1" in u for u, _ in CALLS), r.text[:120])

# >24h → 403 e o registro FICA
ttl_store.add_channel_announcement("telegram", "velho", text="anúncio antigo",
                                   is_posted=True, provider_id="99",
                                   when=datetime(2020, 1, 1, tzinfo=timezone.utc))
old_iri = str(ttl_store.PH["tg-velho"])
r = delete_ann(old_iri)
check("publicado há >24h → 403", r.status_code == 403 and r.json().get("blocked"),
      r.text[:120])
check("registro antigo continua no dataset",
      (rdflib.URIRef(old_iri), None, None) in dataset())

# rascunho: apaga sempre, sem tocar a rede
r = publish("telegram", text="rascunho pra apagar", posted=False)
items = client.get("/api/announcements", params={"channel": "telegram"}).json()
draft = next(i for i in items if not i["is_posted"] and "rascunho pra apagar" in i["text"])
check("rascunho vem deletable", draft["deletable"] is True)
n = len(CALLS)
r = delete_ann(draft["iri"])
check("rascunho: apaga sem rede", r.status_code == 200 and len(CALLS) == n, r.text[:120])

check("SHACL do dataset segue conforme após os deletes",
      publish("telegram", text="sanidade final").json()["validation"]["conforms"])

# a matriz universal apaga sempre (é só registro; nada na rede)
items = client.get("/api/announcements", params={"channel": "universal"}).json()
check("universal vem deletable", items and items[0]["deletable"] is True, str(items[:1]))
n = len(CALLS)
r = delete_ann(items[0]["iri"])
check("universal: apaga sem rede", r.status_code == 200 and r.json()["ok"]
      and len(CALLS) == n, r.text[:120])
g = dataset()
check("universal: matriz sumiu; derivado FICA com o prov",
      (rdflib.URIRef(items[0]["iri"]), None, None) not in g
      and any(True for _ in g.subjects(rdflib.Namespace("http://www.w3.org/ns/prov#").wasDerivedFrom,
                                       rdflib.URIRef(items[0]["iri"]))))

print("\n9) sugestão de alt-text por IA (/api/alt-suggest)")
import alt_ai  # noqa: E402
alt_ai_called = {}
def fake_suggest(data, mime="image/jpeg"):
    alt_ai_called["bytes"] = len(data)
    return "grupo de ciclistas atravessa ponte sobre córrego urbano"
import main as main_mod  # noqa: E402
main_mod.alt_ai.suggest_alt = fake_suggest
r = client.post("/api/alt-suggest", files={"image": ("f.png", PNG, "image/png")})
check("sugere alt e responde 200", r.status_code == 200
      and "ciclistas" in r.json().get("alt", ""), r.text[:140])
check("recebeu os bytes da imagem", alt_ai_called.get("bytes", 0) > 0)
from config import Config as _Cfg  # noqa: E402
_old_key = _Cfg.ANTHROPIC_API_KEY
_Cfg.ANTHROPIC_API_KEY = ""
r = client.post("/api/alt-suggest", files={"image": ("f.png", PNG, "image/png")})
check("sem ANTHROPIC_API_KEY → 503", r.status_code == 503, f"got {r.status_code}")
_Cfg.ANTHROPIC_API_KEY = _old_key

print("\n" + ("TUDO PASSOU" if not FAILURES else f"{len(FAILURES)} FALHAS: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
