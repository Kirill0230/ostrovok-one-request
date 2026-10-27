# Продакшн-образ: собранный фронт + бэкенд в одном контейнере. Запуск — docker-compose.prod.yml.

FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_ENV=production
WORKDIR /srv
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/app backend/app
COPY data/cities.json data/cities.json
COPY --from=web /web/dist frontend/dist
RUN useradd --system --uid 10001 app && mkdir -p data/videos && chown -R app /srv
USER app
EXPOSE 8000
# Порт приложения наружу не публикуется: снаружи доступен только Caddy, поэтому заголовкам прокси доверяем.
CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--workers", "2"]
