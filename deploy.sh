#!/usr/bin/env bash
# Deploy the Instagram composer to Cloud Run, with images + data_manual.ttl in GCS.
# Idempotent: safe to re-run. Reads IG tokens from .env into Secret Manager.
#
#   ./deploy.sh
# Override any var:  REGION=us-central1 BUCKET=my-bucket ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

PROJECT="${PROJECT:-pedal-hidrografico}"
REGION="${REGION:-southamerica-east1}"
SERVICE="${SERVICE:-ph-composer}"
BUCKET="${BUCKET:-${PROJECT}-composer}"          # globally-unique GCS bucket
IG_USER_ID="${IG_USER_ID:-me}"
SEED_DATA="${SEED_DATA:-0}"                       # 1 = upload local data_manual.ttl once
APP_PASSWORD="${APP_PASSWORD:-}"                  # empty = OPEN app (no password gate)

echo ">> project=$PROJECT region=$REGION service=$SERVICE bucket=$BUCKET"
gcloud config set project "$PROJECT" >/dev/null

echo ">> enabling APIs..."
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com storage.googleapis.com secretmanager.googleapis.com \
  cloudscheduler.googleapis.com >/dev/null

# Bucket (images must be publicly readable so Instagram can fetch them).
if ! gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1; then
  echo ">> creating bucket gs://$BUCKET ..."
  gcloud storage buckets create "gs://$BUCKET" --location="$REGION" \
    --uniform-bucket-level-access
fi
echo ">> making bucket objects public-read..."
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member=allUsers --role=roles/storage.objectViewer >/dev/null

# Secrets from .env
get_env() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | sed "s/^[\"']//;s/[\"']$//"; }
upsert_secret() {
  local name="$1" val="$2"
  if [ -z "$val" ]; then echo "   - $name: empty, skipping"; return; fi
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    printf '%s' "$val" | gcloud secrets versions add "$name" --data-file=- >/dev/null
  else
    printf '%s' "$val" | gcloud secrets create "$name" --replication-policy=automatic --data-file=- >/dev/null
  fi
  echo "   - $name: updated"
}
# Vars SENSÍVEIS dos canais de cross-posting → Secret Manager (nome do secret =
# var minúscula com dashes). Canal sem var no .env fica "não configurado" na app.
CHANNEL_SECRET_VARS="WHAPI_TOKEN TELEGRAM_BOT_TOKEN MASTODON_ACCESS_TOKEN \
REDDIT_CLIENT_ID REDDIT_CLIENT_SECRET REDDIT_USERNAME REDDIT_PASSWORD \
SMTP_USER SMTP_PASSWORD"
# Vars NÃO-sensíveis dos canais (alvos/endereços) → env vars comuns.
# GCAL_*: a Agenda não tem secret — a credencial é a própria SA de runtime
# (compartilhe a agenda com ela; o final do deploy lembra o e-mail).
CHANNEL_PLAIN_VARS="WHAPI_ANNOUNCE_GROUP TELEGRAM_CHAT_ID MASTODON_BASE_URL \
REDDIT_SUBREDDIT SMTP_HOST SMTP_PORT EMAIL_FROM EMAIL_TO \
GCAL_CALENDAR_ID GCAL_TIMEZONE"
secret_name() { echo "$1" | tr '[:upper:]_' '[:lower:]-'; }

echo ">> syncing secrets from .env..."
upsert_secret ig-access-token "$(get_env IG_ACCESS_TOKEN)"
upsert_secret ig-manage-token "$(get_env IG_MANAGE_TOKEN)"
for v in $CHANNEL_SECRET_VARS; do
  upsert_secret "$(secret_name "$v")" "$(get_env "$v")"
done

# Grant the Cloud Run runtime SA access to the bucket + secrets.
PNUM="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
RUNTIME_SA="${PNUM}-compute@developer.gserviceaccount.com"
echo ">> granting $RUNTIME_SA bucket + secret access..."
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$RUNTIME_SA" --role=roles/storage.objectAdmin >/dev/null
for s in ig-access-token ig-manage-token $(for v in $CHANNEL_SECRET_VARS; do secret_name "$v"; done); do
  if gcloud secrets describe "$s" >/dev/null 2>&1; then
    gcloud secrets add-iam-policy-binding "$s" \
      --member="serviceAccount:$RUNTIME_SA" --role=roles/secretmanager.secretAccessor >/dev/null
  fi
done

# A app RENOVA o próprio ig-access-token (POST /api/token-refresh, semanal via
# Cloud Scheduler) — pra isso a SA de runtime grava versões novas do secret.
if gcloud secrets describe ig-access-token >/dev/null 2>&1; then
  gcloud secrets add-iam-policy-binding ig-access-token \
    --member="serviceAccount:$RUNTIME_SA" --role=roles/secretmanager.secretVersionAdder >/dev/null
fi

# Optionally seed the curated data file into the bucket (once).
if [ "$SEED_DATA" = "1" ] && [ -f definitions/data_manual.ttl ]; then
  echo ">> seeding gs://$BUCKET/data_manual.ttl from local file..."
  gcloud storage cp definitions/data_manual.ttl "gs://$BUCKET/data_manual.ttl"
fi

# Deploy (PROTECTED -- not public; the service can post/delete on Instagram).
echo ">> deploying..."
# Monta o secret se ele tem valor no .env OU se JÁ EXISTE (com versão) no
# Secret Manager — ex.: telegram-bot-token é criado/gerido pelo pedalbot e não
# mora no .env daqui; sem isso um ./deploy.sh derrubaria o mount dele.
has_secret_version() { gcloud secrets versions describe latest --secret="$1" >/dev/null 2>&1; }
SECRET_FLAGS="IG_ACCESS_TOKEN=ig-access-token:latest"
if [ -n "$(get_env IG_MANAGE_TOKEN)" ] || has_secret_version ig-manage-token; then
  SECRET_FLAGS="$SECRET_FLAGS,IG_MANAGE_TOKEN=ig-manage-token:latest"
fi
for v in $CHANNEL_SECRET_VARS; do
  if [ -n "$(get_env "$v")" ] || has_secret_version "$(secret_name "$v")"; then
    SECRET_FLAGS="$SECRET_FLAGS,$v=$(secret_name "$v"):latest"
  fi
done
# Public URL. If APP_PASSWORD is set, the app gates itself with HTTP Basic;
# empty = OPEN (anyone can post/delete, subject to the SHACL delete rule).
# --set-env-vars REPLACES the whole env set, so omitting APP_PASSWORD removes it.
ENV_VARS="DRY_RUN=false,GCS_BUCKET=$BUCKET,DATA_TTL=gs://$BUCKET/data_manual.ttl,IG_USER_ID=$IG_USER_ID,LOCAL_UPLOAD_DIR=/tmp/uploads"
if [ -n "$APP_PASSWORD" ]; then ENV_VARS="$ENV_VARS,APP_PASSWORD=$APP_PASSWORD"; fi
for v in $CHANNEL_PLAIN_VARS; do
  val="$(get_env "$v")"
  if [ -n "$val" ]; then ENV_VARS="$ENV_VARS,$v=$val"; fi
done
# --quiet: sem prompts (rodando fora de um TTY o gcloud travava aqui).
gcloud run deploy "$SERVICE" --source . --region "$REGION" --quiet \
  --allow-unauthenticated \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "$SECRET_FLAGS"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"

# Job semanal que renova o IG_ACCESS_TOKEN (segunda 09:00 SP). Idempotente:
# só cria se não existir — mudou URL/senha? apague e rode de novo:
#   gcloud scheduler jobs delete ig-token-refresh --location $REGION
if ! gcloud scheduler jobs describe ig-token-refresh --location "$REGION" >/dev/null 2>&1; then
  echo ">> criando o job semanal de renovação do token (Cloud Scheduler)..."
  if [ -n "$APP_PASSWORD" ]; then
    gcloud scheduler jobs create http ig-token-refresh --location "$REGION" \
      --schedule "0 9 * * 1" --time-zone "America/Sao_Paulo" \
      --uri "$URL/api/token-refresh" --http-method POST \
      --headers "Authorization=Basic $(printf 'sabia:%s' "$APP_PASSWORD" | base64)" >/dev/null
  else
    gcloud scheduler jobs create http ig-token-refresh --location "$REGION" \
      --schedule "0 9 * * 1" --time-zone "America/Sao_Paulo" \
      --uri "$URL/api/token-refresh" --http-method POST >/dev/null
  fi
fi

echo
echo "OK deployed: $URL"
if [ -n "$APP_PASSWORD" ]; then echo "   guarded by HTTP Basic password."; else echo "   OPEN (no password)."; fi
if [ -n "$(get_env GCAL_CALENDAR_ID)" ]; then
  echo "   Agenda: compartilhe a agenda $(get_env GCAL_CALENDAR_ID)"
  echo "   com $RUNTIME_SA (permissão \"Fazer alterações em eventos\"),"
  echo "   senão o canal Agenda responde 403 ao criar eventos."
fi
