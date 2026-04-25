.PHONY: help install up down restart logs status \
        db-check neo4j-check qdrant-check \
        test test-unit test-integration test-governance \
        lint format clean reset

# ── Config ────────────────────────────────────────────────────────────────────
VENV        := .venv-guardian
PYTHON      := $(VENV)/bin/python
PIP         := uv pip
COMPOSE     := docker compose
SRC         := src
TESTS       := tests

# ── Help ──────────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "GUARDIAN-Health — Available commands"
	@echo "──────────────────────────────────────────────────────────"
	@echo "  make install          Create venv and install dependencies"
	@echo "  make up               Start all Docker containers"
	@echo "  make down             Stop all Docker containers"
	@echo "  make restart          Restart all containers"
	@echo "  make logs             Tail logs from all containers"
	@echo "  make logs s=postgres  Tail logs from a specific service"
	@echo "  make status           Show container health status"
	@echo ""
	@echo "  make db-check         Verify PostgreSQL connection and schema"
	@echo "  make neo4j-check      Verify Neo4j connection"
	@echo "  make qdrant-check     Verify Qdrant connection"
	@echo "  make infra-check      Run all infrastructure checks"
	@echo ""
	@echo "  make test             Run all tests"
	@echo "  make test-unit        Run unit tests only"
	@echo "  make test-integration Run integration tests only"
	@echo "  make test-governance  Run governance tests (audit trail, HITL)"
	@echo ""
	@echo "  make lint             Run ruff linter"
	@echo "  make format           Run ruff formatter"
	@echo "  make clean            Remove __pycache__ and .pyc files"
	@echo "  make reset            Stop containers and remove all volumes (DESTRUCTIVE)"
	@echo ""

# ── Environment ───────────────────────────────────────────────────────────────
install:
	uv venv $(VENV) --python 3.11
	$(PIP) install -r requirements.txt
	@echo "Virtual environment ready. Activate with: source $(VENV)/bin/activate"

# ── Docker ────────────────────────────────────────────────────────────────────
up:
	$(COMPOSE) up -d
	@echo "Waiting for services to be healthy..."
	@sleep 5
	@$(MAKE) status

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart

logs:
ifdef s
	$(COMPOSE) logs -f $(s)
else
	$(COMPOSE) logs -f
endif

status:
	@echo ""
	@echo "Container status:"
	@$(COMPOSE) ps
	@echo ""

# ── Infrastructure checks ─────────────────────────────────────────────────────
db-check:
	@echo "Checking PostgreSQL..."
	@$(PYTHON) -c "\
import psycopg2, os; from dotenv import load_dotenv; load_dotenv(); \
conn = psycopg2.connect(os.getenv('DATABASE_URL')); \
cur = conn.cursor(); \
cur.execute(\"SELECT COUNT(*) FROM information_schema.tables WHERE table_name IN ('audit_trail','checkpoints','checkpoint_writes')\"); \
count = cur.fetchone()[0]; \
print(f'  PostgreSQL OK — {count}/3 governance tables present'); \
conn.close()"

neo4j-check:
	@echo "Checking Neo4j..."
	@$(PYTHON) -c "\
from neo4j import GraphDatabase; import os; from dotenv import load_dotenv; load_dotenv(); \
driver = GraphDatabase.driver(os.getenv('NEO4J_URI'), auth=(os.getenv('NEO4J_USERNAME'), os.getenv('NEO4J_PASSWORD'))); \
driver.verify_connectivity(); \
print('  Neo4j OK — connection verified'); \
driver.close()"

qdrant-check:
	@echo "Checking Qdrant..."
	@$(PYTHON) -c "\
from qdrant_client import QdrantClient; import os; from dotenv import load_dotenv; load_dotenv(); \
client = QdrantClient(host=os.getenv('QDRANT_HOST','localhost'), port=int(os.getenv('QDRANT_PORT',6333))); \
info = client.get_collections(); \
print(f'  Qdrant OK — {len(info.collections)} collections present')"

infra-check: db-check neo4j-check qdrant-check
	@echo ""
	@echo "All infrastructure checks passed."

# ── Tests ─────────────────────────────────────────────────────────────────────
test:
	$(PYTHON) -m pytest $(TESTS)/ -v

test-unit:
	$(PYTHON) -m pytest $(TESTS)/unit/ -v

test-integration:
	$(PYTHON) -m pytest $(TESTS)/integration/ -v

test-governance:
	$(PYTHON) -m pytest $(TESTS)/governance/ -v --tb=short
	@echo "Governance tests complete. Check audit trail integrity above."

# ── Code quality ──────────────────────────────────────────────────────────────
lint:
	$(PYTHON) -m ruff check $(SRC)/ $(TESTS)/

format:
	$(PYTHON) -m ruff format $(SRC)/ $(TESTS)/

# ── Housekeeping ──────────────────────────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type f -name "*.log" -delete 2>/dev/null || true
	@echo "Clean done."

reset:
	@echo "WARNING: This will destroy all container volumes (database data)."
	@read -p "Are you sure? [y/N] " confirm && [ "$$confirm" = "y" ] || exit 1
	$(COMPOSE) down -v
	@echo "All volumes removed."

seed:
	PYTHONPATH=. $(PYTHON) data/seed/generate_synthetic_fhir.py
	PYTHONPATH=. $(PYTHON) data/seed/load_fhir_to_graph.py
