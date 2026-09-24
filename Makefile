# Truffle — see README.md
VENV := .venv
PY   := $(VENV)/bin/python
API_PORT ?= 8000
WEB_PORT ?= 5173

.PHONY: help install dev api web run run20 dry collect backfill symbols test lint clean reset

help:
	@echo "make install   create venv, install python + node deps"
	@echo "make dev       run API (:$(API_PORT)) and dashboard (:$(WEB_PORT)) together"
	@echo "make api       run the API only"
	@echo "make web       run the dashboard only"
	@echo "make run       full 300-coin pipeline run"
	@echo "make run20     20-coin run (quick check)"
	@echo "make dry       projected API calls and credit cost, fetches nothing"
	@echo "make collect   hourly Binance bars: backfills every gap since the last stored bar"
	@echo "make backfill  one-time 90-day hourly history for the tracked symbols"
	@echo "make symbols   rebuild the Binance symbol -> coin_id map only"
	@echo "make test      pytest"
	@echo "make clean     remove the response cache"
	@echo "make reset     delete the database (destructive, asks first)"

install:
	@test -d $(VENV) || python3 -m venv $(VENV)
	@$(PY) -m pip install -q -r requirements.txt
	@cd frontend && npm install
	@test -f .env || cp .env.example .env
	@echo "done. put your API keys in .env, then: make run20 && make dev"

dev:
	@trap 'kill 0' EXIT INT TERM; \
	$(VENV)/bin/uvicorn api.main:app --port $(API_PORT) --reload & \
	(cd frontend && npm run dev -- --port $(WEB_PORT)) & \
	wait

api:
	@$(VENV)/bin/uvicorn api.main:app --port $(API_PORT) --reload

web:
	@cd frontend && npm run dev -- --port $(WEB_PORT)

run:
	@$(PY) run.py $(ARGS)

run20:
	@$(PY) run.py --limit 20 $(ARGS)

dry:
	@$(PY) run.py --dry-run $(ARGS)

collect:
	@$(PY) collect.py $(ARGS)

backfill:
	@$(PY) collect.py --backfill $(ARGS)

symbols:
	@$(PY) collect.py --map $(ARGS)

test:
	@$(PY) -m pytest -q

lint:
	@cd frontend && npm run lint

clean:
	@rm -rf data/cache && echo "cache cleared"

reset:
	@printf "delete data/truffle.db and all run history? [y/N] "; read a; [ "$$a" = y ] && rm -f data/truffle.db && echo deleted || echo cancelled
