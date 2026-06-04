# Cloud Run image for the Instagram composer. Build context = repo root.
#   gcloud run deploy ph-composer --source .
# (locally: docker build -t ph-composer .)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv

COPY app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App code + shared mapping (scripts/) + shapes/ontology (definitions/, amora/).
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY definitions/ ./definitions/
COPY amora/ ./amora/

WORKDIR /srv/app
# Cloud Run injects $PORT (defaults to 8080).
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
