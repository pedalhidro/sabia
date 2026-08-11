#!/usr/bin/env python3
"""Confere a saúde dos tokens do Instagram — só LEITURA, não posta nem apaga.

    python scripts/check_tokens.py

Responde as duas perguntas que importam:

  * `IG_ACCESS_TOKEN` (Instagram Login, `IG…`) consegue PUBLICAR?
  * `IG_MANAGE_TOKEN` (Facebook Login, `EAA…`) consegue APAGAR?

Apagar é o que quebra calado: `DELETE /<IG_MEDIA_ID>` só existe no
graph.facebook.com e exige `instagram_basic` + `instagram_manage_contents`.
O graph.instagram.com responde "Unsupported delete request". Além disso o token
EAA… expira (~60 dias), e aí a app publica normal mas nunca consegue apagar.

Sai com status 1 se apagar estiver impossível. Nenhum token é impresso.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from config import Config  # noqa: E402

IG_HOST = "https://graph.instagram.com/v21.0"
FB_HOST = "https://graph.facebook.com/v21.0"
NEEDED = ("instagram_basic", "instagram_manage_contents")


def get(host: str, node: str, **params) -> dict:
    url = f"{host}/{node}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return {"error": json.load(exc).get("error", {})}
        except Exception:
            return {"error": {"message": exc.read().decode("utf-8", "replace")[:200]}}
    except Exception as exc:  # rede, SSL, …
        return {"error": {"message": str(exc)}}


def fail(msg: str) -> None:
    print(f"  ✗ {msg}")


def warn(msg: str) -> None:
    """Funciona agora, mas vai quebrar — não derruba o status de saída."""
    print(f"  ! {msg}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def check_publish() -> bool:
    print("PUBLICAR — IG_ACCESS_TOKEN (Instagram Login, graph.instagram.com)")
    token = Config.IG_ACCESS_TOKEN
    if not token:
        fail("IG_ACCESS_TOKEN não definido.")
        return False
    if not token.startswith("IG"):
        fail("esperava um token de Instagram Login (IG…).")
        return False
    me = get(IG_HOST, "me", fields="id,username,account_type", access_token=token)
    if "error" in me:
        fail(f"token inválido: {me['error'].get('message')}")
        return False
    ok(f"@{me['username']} (id {me['id']}, {me['account_type']})")
    if me["account_type"] not in ("BUSINESS", "MEDIA_CREATOR"):
        fail(f"publicar exige conta Business ou Creator, não {me['account_type']}.")
        return False
    return True


def check_delete() -> bool:
    print("\nAPAGAR — IG_MANAGE_TOKEN (Facebook Login, graph.facebook.com)")
    token = Config.IG_MANAGE_TOKEN
    if not token:
        fail("IG_MANAGE_TOKEN não definido — sem ele a app publica mas nunca apaga.")
        return False
    if not token.startswith("EAA"):
        fail("esperava um token de Facebook Login (EAA…); graph.instagram.com não faz DELETE.")
        return False

    # debug_token serve pra token de USER e de PAGE (o /me/permissions só existe
    # em token de usuário — um token de página responde "nonexisting field").
    dbg = get(FB_HOST, "debug_token", input_token=token, access_token=token)
    if "error" in dbg:
        err = dbg["error"]
        expirou = err.get("code") == 190
        fail(f"token {'EXPIRADO' if expirou else 'inválido'}: {err.get('message')}")
        if expirou:
            print("     → python scripts/refresh_manage_token.py")
        return False

    data = dbg.get("data", {})
    if not data.get("is_valid"):
        fail(f"token inválido: {data.get('error', {}).get('message', data)}")
        return False

    faltando = [p for p in NEEDED if p not in set(data.get("scopes") or [])]
    if faltando:
        fail(f"faltam permissões: {', '.join(faltando)}")
        return False
    ok(f"token de {data.get('type', '?')}, permissões: {', '.join(NEEDED)}")

    exp = data.get("expires_at") or 0
    if exp == 0:
        ok("não expira (token de página de longa duração)")
    else:
        quando = datetime.fromtimestamp(exp, timezone.utc)
        faltam = quando - datetime.now(timezone.utc)
        horas = faltam.total_seconds() / 3600
        msg = f"expira em {quando:%Y-%m-%d %H:%M} UTC ({faltam.days}d {int(horas % 24)}h)"
        if horas < 48:
            warn(f"{msg} — curta duração! → python scripts/refresh_manage_token.py")
        else:
            ok(msg)

    # Segundo relógio, independente do primeiro: mesmo um token que "não expira"
    # para de DEVOLVER DADOS 90 dias após a última atividade da pessoa. O token
    # segue válido — as chamadas é que voltam vazias/erro até reautorizar.
    dae = data.get("data_access_expires_at") or 0
    if dae:
        quando = datetime.fromtimestamp(dae, timezone.utc)
        dias = (quando - datetime.now(timezone.utc)).days
        msg = f"acesso a dados expira em {quando:%Y-%m-%d} ({dias}d)"
        (warn if dias < 14 else ok)(
            f"{msg}{' — reautorize: python scripts/refresh_manage_token.py' if dias < 14 else ''}")

    # Necessário pro DELETE: o token do Facebook precisa ENXERGAR nossa mídia.
    media = get(IG_HOST, "me/media", fields="id", limit=1, access_token=Config.IG_ACCESS_TOKEN)
    if "error" in media:
        # Erro ≠ conta vazia: sem listar a mídia (IG_ACCESS_TOKEN morto?) o
        # teste de visibilidade fica PENDENTE — não dá pra afirmar nada.
        warn("não deu pra listar a mídia (me/media falhou: "
             f"{media['error'].get('message', media['error'])}) — "
             "visibilidade do DELETE não conferida")
        return True
    latest = (media.get("data") or [{}])[0].get("id")
    if not latest:
        ok("conta sem mídia — nada pra conferir")
        return True
    seen = get(FB_HOST, latest, fields="id,permalink", access_token=token)
    if "error" in seen:
        fail(f"não enxerga a mídia {latest}: {seen['error'].get('message')}")
        return False
    ok(f"enxerga a mídia mais recente ({latest}) — DELETE funciona")
    return True


if __name__ == "__main__":
    print(f"conta configurada: IG_USER_ID={Config.IG_USER_ID}  DRY_RUN={Config.DRY_RUN}\n")
    can_publish = check_publish()
    can_delete = check_delete()
    print(f"\npublicar: {'OK' if can_publish else 'NÃO'}   apagar: {'OK' if can_delete else 'NÃO'}")
    sys.exit(0 if (can_publish and can_delete) else 1)
