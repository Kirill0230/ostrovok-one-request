.PHONY: up down logs ps eval seed clean

up:
	@test -f .env || cp .env.example .env
	docker compose up --build -d
	@echo "Готово: http://localhost:8080"

down:
	docker compose down

logs:
	docker compose logs -f app

ps:
	docker compose ps

eval:
	docker compose exec -w /srv/backend -e RATE_LIMIT_ENABLED=0 app python -m eval.run_eval

seed: eval

clean:
	docker compose down -v
