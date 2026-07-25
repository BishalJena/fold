.PHONY: dev backend ui test lint

dev:
	python3 scripts/dev.py

backend:
	python3 -m uvicorn foldos.app:create_app --factory --host 127.0.0.1 --port 7777

ui:
	scripts/start_agent_ui.sh

test:
	pytest -q

lint:
	ruff check foldos tests scripts
	mypy foldos scripts/e2e_backend.py scripts/e2e_agno_ollama.py scripts/fetch_signoz_dashboard.py scripts/demo_scenario.py
