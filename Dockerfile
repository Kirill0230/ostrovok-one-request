FROM node:20.20.0-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12.16-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
ENV OPENSSL_armcap=0
WORKDIR /srv
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt
COPY backend/app backend/app
COPY backend/eval backend/eval
COPY backend/certs certs
COPY data/cities.json data/climate.json data/
COPY data/videos data/videos
COPY --from=web /web/dist frontend/dist
RUN useradd --system --uid 10001 --no-create-home app && chown -R app /srv
USER app
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status != 200)"]
CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
