.PHONY: install migrate test seed demo ops api

install:
	cd backend && python -m pip install -e ".[dev]"

migrate:
	cd backend && alembic upgrade head

test:
	cd backend && pytest -q

seed:
	cd backend && python -m app.scripts.seed_demo

demo:
	cd backend && python -m app.scripts.run_demo

ops:
	cd backend && python -m app.scripts.ops_check --latest

api:
	cd backend && uvicorn app.main:app --reload

