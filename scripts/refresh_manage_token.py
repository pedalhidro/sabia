#!/usr/bin/env python3
"""Troca um token curto por um IG_MANAGE_TOKEN de PÁGINA que NÃO expira.

    python scripts/refresh_manage_token.py

Por que existe: o token que o Graph API Explorer entrega dura ~1h. Quando ele
morre, a app continua publicando e só o ✕ (apagar) quebra com 502 — falha
silenciosa e confusa. Um token de página derivado de um token de usuário de
LONGA duração não tem expiração (só cai se a senha do admin mudar ou o acesso
for revogado).

A corrente é: token de usuário curto → (app secret) → token de usuário longo →
GET /me/accounts → token de PÁGINA permanente.

Precisa de duas coisas, uma vez só, no .env (o script pode ler de variável de
ambiente também, e NÃO grava nenhuma das duas):

    FB_APP_ID=…        # id do app (o "pedalhidro")
    FB_APP_SECRET=…    # Configurações → Básico, no painel do app

E de um token de USUÁRIO curto (não de página!), gerado no Graph API Explorer
com os escopos `pages_show_list`, `instagram_basic` e `instagram_manage_contents`.
Passe em FB_USER_TOKEN, ou o script pergunta.

Sem app secret dá pra fazer à mão: no Access Token Debugger, botão
"Extend Access Token" — aí passe o token JÁ ESTENDIDO em FB_USER_TOKEN e rode
com --skip-exchange.

O token final é gravado direto no .env. Nada de segredo vai pra stdout.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))
from config import Config  # noqa: E402

FB = "https://graph.facebook.com/v21.0"
ENV = REPO / ".env"
NEEDED = ("instagram_basic", "instagram_manage_contents")


def get(node: str, **params) -> dict:
    url = f"{FB}/{node}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return {"error": json.load(exc).get("error", {})}
        except Exception:
            return {"error": {"message": exc.read().decode("utf-8", "replace")[:200]}}


def die(msg: str) -> None:
    print(f"✗ {msg}")
    sys.exit(1)


def write_env(key: str, value: str) -> None:
    """Substitui (ou acrescenta) a chave no .env, preservando o resto."""
    text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    ENV.write_text(text, encoding="utf-8")


def main() -> None:
    skip_exchange = "--skip-exchange" in sys.argv

    user_token = os.environ.get("FB_USER_TOKEN") or getpass.getpass(
        "token de USUÁRIO do Graph API Explorer (não aparece na tela): ").strip()
    if not user_token.startswith("EAA"):
        die("esperava um token EAA… de Facebook Login.")

    if not skip_exchange:
        app_id = os.environ.get("FB_APP_ID") or getpass.getpass("FB_APP_ID: ").strip()
        secret = os.environ.get("FB_APP_SECRET") or getpass.getpass("FB_APP_SECRET: ").strip()
        if not (app_id and secret):
            die("sem FB_APP_ID/FB_APP_SECRET. Use --skip-exchange com um token já estendido.")
        print("→ trocando por token de usuário de longa duração (60 dias)…")
        r = get("oauth/access_token", grant_type="fb_exchange_token",
                client_id=app_id, client_secret=secret, fb_exchange_token=user_token)
        if "error" in r:
            die(f"troca falhou: {r['error'].get('message')}")
        user_token = r["access_token"]
        print("  ✓ token de usuário longo obtido")

    # Confere que o token de usuário é longo — senão o token de página herda a
    # validade curta e a gente só teria empurrado o problema com a barriga.
    dbg = get("debug_token", input_token=user_token, access_token=user_token)
    if "error" in dbg:
        die(f"debug_token: {dbg['error'].get('message')}")
    data = dbg.get("data", {})
    if data.get("type") != "USER":
        die(f"esperava um token de USUÁRIO, veio {data.get('type')}. "
            "No Graph API Explorer escolha 'User Token', não 'Page Token'.")
    faltando = [p for p in NEEDED if p not in set(data.get("scopes") or [])]
    if faltando:
        die(f"faltam escopos no token de usuário: {', '.join(faltando)}")
    exp = data.get("expires_at") or 0
    if exp and (exp - int(data.get("issued_at") or 0) or 0) < 86400 and not skip_exchange:
        print("  ! aviso: token de usuário ainda parece curto")

    print("→ buscando o token de página…")
    accts = get("me/accounts", fields="id,name,access_token,instagram_business_account{id,username}",
                access_token=user_token)
    if "error" in accts:
        die(f"/me/accounts: {accts['error'].get('message')}")

    want = str(Config.IG_USER_ID)
    page = None
    for p in accts.get("data", []):
        iba = (p.get("instagram_business_account") or {}).get("id")
        if iba == want:
            page = p
            break
    if page is None:
        nomes = [(p.get("name"), (p.get("instagram_business_account") or {}).get("id"))
                 for p in accts.get("data", [])]
        die(f"nenhuma página ligada ao IG_USER_ID={want}. Páginas visíveis: {nomes}")

    page_token = page["access_token"]
    pdbg = get("debug_token", input_token=page_token, access_token=page_token).get("data", {})
    if pdbg.get("expires_at"):
        print(f"  ! aviso: o token de página AINDA expira ({pdbg['expires_at']}). "
              "Provavelmente o token de usuário era curto.")
    else:
        print("  ✓ token de página sem expiração")

    ig = page.get("instagram_business_account", {})
    print(f"  página: {page['name']}  →  @{ig.get('username')} ({ig.get('id')})")

    write_env("IG_MANAGE_TOKEN", page_token)
    print(f"\n✓ IG_MANAGE_TOKEN gravado em {ENV} ({len(page_token)} caracteres)")
    print("  confira:  python scripts/check_tokens.py")
    print("  publique: ./deploy.sh          # sobe o token novo pro Secret Manager")


if __name__ == "__main__":
    main()
