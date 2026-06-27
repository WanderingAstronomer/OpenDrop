# OpenDrop dev shortcuts. (Works in WSL / Linux / CI; on Windows use Git Bash or run the commands directly.)
.PHONY: up down seed test test-fast lint

up:        ## build + start the full stack
	docker compose up -d --build

down:      ## stop the stack (keeps data; add `-v` to wipe)
	docker compose down

seed:      ## seed the Ohio test region
	bash scripts/seed.sh

test-fast: ## fast dedup-logic tests, no services needed
	python tests/test_dedup_logic.py

test:      ## full suite (spins the db, installs deps in a throwaway container)
	docker compose up -d db
	docker run --rm --network opendrop_default -v "$(CURDIR):/src" -w /src \
	  -e DATABASE_URL=postgresql://opendrop:opendrop@db:5432/opendrop python:3.12-slim \
	  sh -c "pip install -q -r backend/requirements.txt -r backend/requirements-dev.txt && pytest"

lint:      ## ruff lint
	docker run --rm -v "$(CURDIR):/src" -w /src python:3.12-slim \
	  sh -c "pip install -q ruff && ruff check backend/app pipeline"
