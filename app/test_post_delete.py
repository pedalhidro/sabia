"""Testa o ciclo publicar → apagar, contra uma Graph API FALSA.

Não toca na conta real: `urlopen` é substituído. Roda sem pytest:

    cd app && python test_post_delete.py

Cobre as propriedades que importam:
  1. publicar (foto única e carrossel) grava um post que conforma o SHACL;
  2. a grade só mostra posts publicados POR ESTA APP (ph:managedByApp);
  3. apagar remove no Instagram (host certo, token certo) e no dataset;
  4. a trava de engajamento bloqueia apagar post popular — inclusive DEPOIS de
     um apagar anterior (os tipos derivados não podem ser persistidos no .ttl);
  5. apagar exige token de Facebook Login; graph.instagram.com não faz DELETE.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
from pathlib import Path

APP = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="sabia-test-"))

os.environ.update(
    GCS_BUCKET="", DRY_RUN="false", APP_PASSWORD="",
    IG_ACCESS_TOKEN="IGfake",          # Instagram Login → graph.instagram.com
    IG_MANAGE_TOKEN="EAAfake",         # Facebook Login  → graph.facebook.com
    IG_USER_ID="17841400000000000",
    DATA_TTL=str(TMP / "data.ttl"), LOCAL_UPLOAD_DIR=str(TMP / "uploads"),
)
sys.path.insert(0, str(APP))

import instagram  # noqa: E402

# ── Graph API falsa ──────────────────────────────────────────────────────────
API = {"media": {}, "metrics": {}, "deleted": [], "containers": 0, "calls": []}


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _json(obj) -> _Resp:
    return _Resp(json.dumps(obj).encode())


def _fail(url, code, msg, api_code=100):
    return urllib.error.HTTPError(
        url, code, msg, {}, io.BytesIO(json.dumps({"error": {"message": msg, "code": api_code}}).encode()))


def fake_urlopen(req, *a, **kw):
    url = req if isinstance(req, str) else req.full_url
    method = "GET" if isinstance(req, str) else req.get_method()
    path, _, qs = url.partition("?")
    query = dict(urllib.parse.parse_qsl(qs))
    body = dict(urllib.parse.parse_qsl(req.data.decode())) if (not isinstance(req, str) and req.data) else {}
    on_fb = path.startswith(instagram.FB_HOST)
    node = path.split("/v21.0/", 1)[1]
    API["calls"].append((method, "FB" if on_fb else "IG", node))

    if method == "POST" and node.endswith("/media"):
        API["containers"] += 1
        return _json({"id": f"CONTAINER{API['containers']}"})
    if method == "GET" and node.startswith("CONTAINER"):
        return _json({"status_code": "FINISHED"})
    if method == "POST" and node.endswith("/media_publish"):
        n = len(API["media"]) + 1
        mid = f"MEDIA{n}"
        API["media"][mid] = f"https://www.instagram.com/p/DFake{n}/"
        API["metrics"][mid] = {"like_count": 0, "comments_count": 0}
        return _json({"id": mid})
    if method == "GET" and node in API["media"]:
        if "permalink" in query.get("fields", ""):
            return _json({"permalink": API["media"][node]})
        return _json({**API["metrics"][node], "media_type": "IMAGE"})
    if method == "DELETE" and node in API["media"]:
        # graph.instagram.com não implementa DELETE — igual à API de verdade.
        if not on_fb:
            raise _fail(path, 400, "Unsupported delete request")
        API["deleted"].append(node)
        del API["media"][node]
        return _json({"success": True})
    raise _fail(path, 400, f"Unknown node {node}", api_code=803)


instagram.urllib.request.urlopen = fake_urlopen

from fastapi.testclient import TestClient  # noqa: E402
import rdflib  # noqa: E402

import main  # noqa: E402
import ttl_store  # noqa: E402
from config import Config  # noqa: E402

client = TestClient(main.app)
PNG = bytes.fromhex("89504e470d0a1a0a0000000d494844520000000100000001080600000"
                    "01f15c4890000000d4944415478da6364f8cf000000030101002718e3"
                    "660000000049454e44ae426082")
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))
    if not ok:
        FAILURES.append(name)


def publish(caption: str, images: int = 1):
    return client.post(
        "/api/publish",
        files=[("images", (f"f{i}.png", PNG, "image/png")) for i in range(images)],
        data={"caption": caption, "is_posted": "true", "confirm": "true"},
    )


def dataset() -> rdflib.Graph:
    g = rdflib.Graph()
    g.parse(str(TMP / "data.ttl"), format="turtle")
    return g


print("1) publicar foto única e carrossel")
a, b = publish("post simples"), publish("post carrossel", images=2)
check("foto única publica", a.status_code == 200 and a.json()["ok"])
check("carrossel publica", b.status_code == 200 and b.json()["ok"])
check("SHACL conforma", b.json()["validation"]["conforms"], str(b.json()["validation"]["violations"]))
check("carrossel guarda o id do álbum, não dos filhos",
      b.json()["instagram"]["id"] in API["media"], b.json()["instagram"]["id"])

print("\n2) a grade lista os dois")
grid = client.get("/api/posts").json()
check("dois posts na grade", len(grid) == 2, f"len={len(grid)}")
check("ambos removíveis (engajamento zero)", all(p["deletable"] for p in grid))
shortcodes = {p["caption"]: p["shortcode"] for p in grid}

print("\n3) apagar o primeiro")
r = client.post("/api/posts/delete", data={"shortcode": shortcodes["post simples"]})
check("apagar responde 200", r.status_code == 200, r.text[:120])
check("apagou no Instagram", r.json()["instagram"]["deleted"] is True)
check("DELETE foi pro graph.facebook.com",
      ("DELETE", "FB", "MEDIA1") in API["calls"], str(API["calls"][-1]))
check("sumiu da grade", len(client.get("/api/posts").json()) == 1)

print("\n4) tipos derivados não vão pro .ttl")
persisted = {str(t) for t in dataset().objects(None, rdflib.RDF.type)}
leaked = persisted & {str(c) for c in ttl_store.INFERRED_CLASSES}
check("nenhum tipo inferido persistido", not leaked, str(leaked))

print("\n5) trava de engajamento (post popular, DEPOIS de um apagar)")
API["metrics"]["MEDIA2"] = {"like_count": 100, "comments_count": 50}
grid = client.get("/api/posts").json()
check("grade marca como não-removível", grid[0]["deletable"] is False)
r = client.post("/api/posts/delete", data={"shortcode": shortcodes["post carrossel"]})
check("apagar é bloqueado com 403", r.status_code == 403, f"got {r.status_code}")
check("continua vivo no Instagram", "MEDIA2" not in API["deleted"])
check("continua no dataset", len(client.get("/api/posts").json()) == 1)

print("\n6) .ttl envenenado por versão antiga se cura ao carregar")
poisoned = dataset()
PH = ttl_store.PH
victim = next(poisoned.subjects(rdflib.RDF.type, PH.InstagramPost))
poisoned.add((victim, rdflib.RDF.type, PH.DeletableInstagramPost))
(TMP / "data.ttl").write_text(poisoned.serialize(format="turtle"), encoding="utf-8")
healed = ttl_store.load_dataset()
check("tipo inferido é descartado na leitura",
      not ttl_store.has_type(healed, str(victim), PH.DeletableInstagramPost))
r = client.post("/api/posts/delete", data={"shortcode": shortcodes["post carrossel"]})
check("post popular segue bloqueado mesmo com .ttl envenenado", r.status_code == 403,
      f"got {r.status_code}: {r.text[:120]}")

print("\n7) apagar exige token de Facebook Login")
API["metrics"]["MEDIA2"] = {"like_count": 0, "comments_count": 0}
Config.IG_MANAGE_TOKEN = ""  # sobra só o token IG… de publicação
r = client.post("/api/posts/delete", data={"shortcode": shortcodes["post carrossel"]})
check("erro claro (422), sem apagar do dataset", r.status_code == 422, f"got {r.status_code}")
check("mensagem cita o IG_MANAGE_TOKEN", "IG_MANAGE_TOKEN" in r.text, r.text[:160])
check("post preservado no dataset (nada foi apagado no IG)",
      len(client.get("/api/posts").json()) == 1)
Config.IG_MANAGE_TOKEN = "EAAfake"

print("\n8) DELETE recusado pela API não apaga o registro")
_real = instagram.urllib.request.urlopen
instagram.urllib.request.urlopen = lambda req, *a, **k: (
    _json({"success": False}) if (not isinstance(req, str) and req.get_method() == "DELETE")
    else _real(req, *a, **k))
r = client.post("/api/posts/delete", data={"shortcode": shortcodes["post carrossel"]})
check('{"success": false} vira erro 422', r.status_code == 422, f"got {r.status_code}")
instagram.urllib.request.urlopen = _real
check("post continua no dataset", len(client.get("/api/posts").json()) == 1)

print("\n9) agora sim: apagar o carrossel de verdade")
r = client.post("/api/posts/delete", data={"shortcode": shortcodes["post carrossel"]})
check("apagar responde 200", r.status_code == 200, r.text[:120])
check("nada sobrou no Instagram", API["media"] == {}, str(API["media"]))
check("nada sobrou no dataset", len(dataset()) == 0, f"{len(dataset())} triplas")

print("\n" + ("TUDO PASSOU" if not FAILURES else f"{len(FAILURES)} FALHAS: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
