# Build context is the REPO ROOT (see docker-compose api.build.context: .)
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/app

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Two importable top-level packages from WORKDIR: app (API) and pipeline (jobs)
COPY backend/app ./app
COPY pipeline ./pipeline
# Committed Phase-1 fixture used as the deterministic OSM fallback in pipeline.seed
COPY research/data/osm_columbus.json ./research/data/osm_columbus.json

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
