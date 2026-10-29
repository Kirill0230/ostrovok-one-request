# О!дин запрос (трек Островка «О!дин запрос»)

AI-агент собирает поездку по одному сообщению: направление, отель, перелёт и трансфер. Он проверяет стыковки и бюджет, собирает корзину и имитирует оплату. Варианты — карточки со свайпом: дизлайк с причиной («дорого», «далеко лететь») меняет ровно этот параметр, и план пересобирается.

- Видео-скринкаст: https://drive.google.com/file/d/1UZLocPrbVr9W_Cqz3VTDmFAxOAyahbdG/view?usp=sharing
- Подробности (архитектура, API, база, безопасность, GigaChat): [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

> Моки: API Островка нет, поэтому отели, рейсы и цены синтетические, оплата имитируется. Климат — реальные данные Open-Meteo, видео — Wikimedia Commons (авторы подписаны на карточках).

## Быстрый старт

```bash
git clone https://github.com/ostrovok-hackathon-2026/office.git
cd office
cp .env.example .env
docker compose up --build
# открыть http://localhost:8080
```

Первая сборка занимает 3–5 минут. Остановить: `docker compose down`.

## Зависимости и переменные окружения

Рекомендованные ресурсы:
- 2 CPU cores
- 2 GB RAM

Сервисы:
- `app` — FastAPI + React, порт `8080`
- `db` — Postgres 16
- `redis` — Redis 7 (лимиты запросов, опционально: без него сервис работает)

Основные переменные (`.env.example`):
- `APP_PORT` — порт на хосте (по умолчанию `8080`)
- `APP_ENV` — `development` (по умолчанию) или `production`
- `DATABASE_URL` — задан в compose: `postgresql://odin:odin@db:5432/odin`
- `REDIS_URL` — задан в compose: `redis://redis:6379/0`
- `AGENT` — `llm` (GigaChat + правила, по умолчанию) или `rules`
- `GIGACHAT_CREDENTIALS` — ключ GigaChat; пусто — агент работает на правилах

Полный список — в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#все-переменные-окружения).

## Сидирование

Не требуется. Миграции применяются при старте контейнера, каталог направлений, климат и видео вшиты в образ.

Проверка на 30 тестовых запросах:

```bash
docker compose exec -w /srv/backend app python -m eval.run_eval
```

## Маршруты/доступ

- `/` — UI
- `/health` — 200 OK, JSON `{ "status": "ok" }`
- `/api/health` — состояние агента и данных
- `/docs` — Swagger API (в режиме `development`)
- Тестовый пользователь не нужен: вход анонимный, автоматически.

Что попробовать: «к тёплому морю в ноябре, недорого, с ребёнком» → лайкайте карточки → «Оформить». Затем «на 2 дня позже» или «едем с собакой».

## Если не запускается

- Порт 8080 занят: `APP_PORT=8081 docker compose up --build`.
- Docker Hub не отдаёт образы: добавьте в Docker Desktop → Settings → Docker Engine `"registry-mirrors": ["https://mirror.gcr.io"]`.
- Логи: `docker compose logs app`.
