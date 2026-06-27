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

# Run as a non-root user (defense-in-depth for a public-facing container)
RUN useradd -r -u 10001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health',timeout=4).status==200 else 1)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
