"""Auto-renovação do IG_ACCESS_TOKEN (Instagram Login, token "IG…").

O token de longa duração vale 60 dias mas PODE ser renovado indefinidamente
(GET /refresh_access_token) enquanto ainda estiver válido e tiver ≥24h de
idade. Foi a falta disso que deixou o token expirar calado: publicar quebrava
com 502 e a UI seguia dizendo "configurado".

refresh_ig_token() renova e persiste em DOIS lugares:
  1. em memória (Config.IG_ACCESS_TOKEN) — vale já, nesta instância;
  2. numa versão nova do secret ig-access-token (Secret Manager) — vale pras
     próximas instâncias, já que a env var é resolvida no boot do container.
     Local (sem GCS), grava no .env em vez do Secret Manager.

Quem chama é POST /api/token-refresh (main.py), disparado toda semana pelo
Cloud Scheduler (job ig-token-refresh — ver deploy.sh). Renovar toda semana é
inócuo: cada renovação emite um token de 60 dias.

O IG_MANAGE_TOKEN (EAA…, token de página) NÃO passa por aqui: ele não expira,
mas o ACESSO A DADOS dele expira (~90 dias) e só renova com login manual
(scripts/refresh_manage_token.py). manage_token_status() apenas reporta esse
relógio, pra rota dar visibilidade.

Nenhuma função devolve o valor de token — as respostas são só metadados.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from time import time

from config import Config
from instagram import FB_HOST

REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _get_json(url: str, *, method: str = "GET", headers: dict | None = None,
              data: bytes | None = None) -> dict:
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        err = json.load(exc)
        return err.get("error", {}).get("message") or json.dumps(err, ensure_ascii=False)[:200]
    except Exception:
        return exc.read().decode("utf-8", "replace")[:200]


# ── persistência ──────────────────────────────────────────────────────────────
def _project_id() -> str:
    pid = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
    if pid:
        return pid
    # Metadata server (Cloud Run/GCE) — fora dele, defina GOOGLE_CLOUD_PROJECT.
    req = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/project/project-id",
        headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode("utf-8").strip()


def _adc_token() -> str:
    """Token ADC com escopo cloud-platform (na Cloud Run, a SA de runtime)."""
    import google.auth
    import google.auth.transport.requests
    creds, _ = google.auth.default()
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def _add_secret_version(secret_id: str, value: str) -> str:
    """Grava uma versão nova do secret e devolve o nome da versão. A SA de
    runtime precisa de roles/secretmanager.secretVersionAdder (deploy.sh)."""
    project = _project_id()
    res = _get_json(
        f"https://secretmanager.googleapis.com/v1/projects/{project}"
        f"/secrets/{secret_id}:addVersion",
        method="POST",
        headers={"Authorization": f"Bearer {_adc_token()}",
                 "Content-Type": "application/json"},
        data=json.dumps({"payload": {
            "data": base64.b64encode(value.encode()).decode()}}).encode())
    return res.get("name", "")


def _write_env(key: str, value: str) -> None:
    """Substitui (ou acrescenta) a chave no .env, preservando o resto —
    mesma lógica do scripts/refresh_manage_token.py."""
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    ENV_PATH.write_text(text, encoding="utf-8")


# ── renovação ─────────────────────────────────────────────────────────────────
def refresh_ig_token() -> dict:
    """Renova o IG_ACCESS_TOKEN. Devolve metadados (nunca o token)."""
    if Config.DRY_RUN:
        return {"ok": True, "refreshed": False, "dry_run": True}
    if not Config.IG_ACCESS_TOKEN:
        return {"ok": False, "refreshed": False, "error": "IG_ACCESS_TOKEN não definido."}

    url = REFRESH_URL + "?" + urllib.parse.urlencode(
        {"grant_type": "ig_refresh_token", "access_token": Config.IG_ACCESS_TOKEN})
    try:
        res = _get_json(url)
    except urllib.error.HTTPError as exc:
        msg = _http_error_message(exc)
        # Token com menos de 24h não renova — não é falha, é cedo demais.
        if "24 hour" in msg or "24 horas" in msg:
            return {"ok": True, "refreshed": False,
                    "reason": "token com menos de 24h — nada a fazer"}
        hint = (" Token expirado não renova: refaça o login do Instagram, "
                "atualize o .env e rode ./deploy.sh."
                if "expire" in msg.lower() or "session" in msg.lower() else "")
        return {"ok": False, "refreshed": False, "error": f"refresh recusado: {msg}{hint}"}
    except urllib.error.URLError as exc:
        return {"ok": False, "refreshed": False, "error": f"graph.instagram.com inacessível: {exc.reason}"}

    new = res.get("access_token", "")
    if not new:
        return {"ok": False, "refreshed": False,
                "error": f"resposta sem access_token: {json.dumps(res, ensure_ascii=False)[:200]}"}

    Config.IG_ACCESS_TOKEN = new  # vale imediatamente nesta instância
    out = {"ok": True, "refreshed": True,
           "expires_in_days": round(int(res.get("expires_in", 0)) / 86400)}
    try:
        if Config.using_gcs():
            out["secret_version"] = _add_secret_version("ig-access-token", new)
        elif ENV_PATH.exists():
            _write_env("IG_ACCESS_TOKEN", new)
            out["env_file"] = str(ENV_PATH)
    except Exception as exc:
        # Renovou em memória mas não persistiu: a próxima instância volta pro
        # token antigo (ainda válido). Fica visível na resposta pra não passar
        # batido — em geral é a IAM do secretVersionAdder faltando.
        out["persist_error"] = str(exc)[:300]
    return out


def manage_token_status() -> dict:
    """Relógio do IG_MANAGE_TOKEN (só leitura): validade e os dias que restam
    de ACESSO A DADOS — o que expira primeiro e só renova com login manual."""
    token = Config.IG_MANAGE_TOKEN
    if not token:
        return {"configured": False}
    try:
        dbg = _get_json(f"{FB_HOST}/debug_token?" + urllib.parse.urlencode(
            {"input_token": token, "access_token": token}))
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        msg = _http_error_message(exc) if isinstance(exc, urllib.error.HTTPError) else str(exc)
        return {"configured": True, "error": msg[:200]}
    data = dbg.get("data", {})
    out = {"configured": True, "valid": bool(data.get("is_valid"))}
    dae = data.get("data_access_expires_at") or 0
    if dae:
        out["data_access_days_left"] = max(0, int((dae - time()) // 86400))
        out["data_access_expires_on"] = datetime.fromtimestamp(
            dae, timezone.utc).strftime("%Y-%m-%d")
        if out["data_access_days_left"] < 14:
            out["hint"] = "reautorize: python scripts/refresh_manage_token.py"
    return out


_manage_status_cache: tuple | None = None  # (quando, resultado)


def manage_token_status_cached(max_age: float = 6 * 3600) -> dict:
    """manage_token_status() com cache de 6h — o /api/config chama a cada
    carga da página, e sem cache cada visita viraria uma ida ao Graph API."""
    global _manage_status_cache
    if _manage_status_cache and time() - _manage_status_cache[0] < max_age:
        return _manage_status_cache[1]
    res = manage_token_status()
    _manage_status_cache = (time(), res)
    return res
