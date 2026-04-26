# GUARDIAN-Health

**Governance and Accountability by Design in Agentic AI — Healthcare Case Study**

A production-adjacent agentic AI system for active pharmacovigilance and patient safety monitoring in multi-centre hospital networks. Built as the third case study of a self-directed Agentic AI Bootcamp.

Repository: [github.com/mgalanme/guardian-health](https://github.com/mgalanme/guardian-health)

Architecture documents: [`docs/`](./docs/)

---

## What This Project Demonstrates

GUARDIAN-Health is not a demo. It is an executable proof that governance, traceability, and accountability in agentic AI systems must be designed in from day zero — not retrofitted after the fact.

The system monitors clinical signals for inpatients, evaluates adverse drug reaction signals through a multi-agent crew, coordinates clinical responses with mandatory human oversight, and records every agent decision in a cryptographically chained audit trail. All of this runs across three decoupled modules connected by an event-driven bus (Solace Agent Mesh), with governance enforced at the bus level, not in any individual agent.

The project applies three architectural frameworks simultaneously:

- **Enterprise Architecture:** TOGAF 10 — governance principles, capability mapping, architecture decisions
- **Data Architecture:** DAMA DMBOK2 — data domains, pseudonymisation, lineage, quality
- **Agentic AI Architecture:** LangGraph, CrewAI, AutoGen — agent patterns, HITL, stateful orchestration

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    VIGIL MODULE (LangGraph)                     │
│  initialise → fetch_context → sanitise → monitor → correlate   │
│  Publishes raw signals to Solace bus                            │
└────────────────────────┬────────────────────────────────────────┘
                         │ guardian/v1/signals/raw/>
                         ▼ Solace Agent Mesh
┌─────────────────────────────────────────────────────────────────┐
│                   ASSESS MODULE (CrewAI)                        │
│  Pharmacologist · Clinician · Regulatory · Synthesis agents     │
│  Publishes evaluations to Solace bus                            │
└────────────────────────┬────────────────────────────────────────┘
                         │ guardian/v1/signals/evaluated/>
                         ▼ Solace Agent Mesh
┌─────────────────────────────────────────────────────────────────┐
│                  RESPOND MODULE (Coordinator)                   │
│  HITL · Clinical notification · Knowledge graph update          │
│  Publishes to guardian/v1/notifications/> and audit/>           │
└─────────────────────────────────────────────────────────────────┘
                         │ guardian/v1/audit/>
                         ▼ Cross-framework audit consumer
┌─────────────────────────────────────────────────────────────────┐
│              CRYPTOGRAPHIC AUDIT TRAIL (PostgreSQL)             │
│  Immutable · Append-only · SHA-256 chained · HITL recorded      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Technology Stack

| Layer | Technology | Purpose |
|---|---|---|
| Agentic orchestration | LangGraph | VIGIL stateful flow, HITL interrupts |
| Multi-agent crews | CrewAI | ASSESS evaluation crew (4 agents) |
| Adversarial evaluation | AutoGen | Ambiguous signal debate protocol |
| LLM abstraction | LangChain | Tool integration, prompt management |
| LLM inference | Groq (llama-3.3-70b-versatile) | Ultra-low latency inference |
| Event bus | Solace Agent Mesh | Cross-framework governance messaging |
| Graph database | Neo4j + APOC | Clinical knowledge graph |
| Relational database | PostgreSQL | Audit trail, checkpointing, master data |
| Vector store (dev) | Qdrant | Local semantic search |
| Vector store (prod) | Pinecone | Managed semantic search |
| Embeddings | HuggingFace nomic-embed-text-v1 | Clinical document indexing |
| API layer | FastAPI | REST service, HITL endpoint |
| Observability | LangSmith | Agentic trace capture |
| Containerisation | Docker + Compose | Full stack isolation |

---

## Governance Design

### The Central Principle

Retroactive governance does not exist in agentic AI systems. Decision traces that were not recorded cannot be recreated. Data lineage that was not captured cannot be reconstructed. Regulatory conformity cannot be demonstrated if the system was not designed to produce the required evidence.

Every design decision in this project is motivated by a governance requirement.

### Audit Trail

The audit trail is a first-class field in the LangGraph graph state, not a secondary logging system. Every node writes to it before and after execution. Records are cryptographically chained: each entry contains the SHA-256 hash of the previous entry. The agent database role has DELETE and UPDATE revoked at the PostgreSQL level.

```sql
REVOKE DELETE, UPDATE ON audit_trail FROM guardian_agent_role;
```

### Data Pseudonymisation

Patient data is special category PII under GDPR Article 9. No identifiable patient data ever reaches the LLM. A two-layer pseudonymisation scheme assigns permanent UUIDs to real patient IDs (stored in a restricted mapping table) and generates session-scoped aliases (PAT-XXXX) for each agentic session. LLMs receive only session aliases.

### HITL by Design

Human oversight is not optional for high-risk decisions. The HITL matrix is defined at design time:

| Severity | HITL Level |
|---|---|
| MILD | Informational notification |
| MODERATE | 24-hour confirmation |
| SERIOUS | Prior approval required |
| POTENTIALLY_SERIOUS | Prior approval required |
| Regulatory notification | Mandatory approval and signature |

### Cross-Framework Governance via Solace

The governance policy is encoded in the topic hierarchy, not in any individual agent or framework. A single audit consumer subscribed to `guardian/v1/audit/>` captures events from LangGraph, CrewAI, and the coordinator without knowing which framework published them.

```
guardian/v1/signals/raw/{centre_id}/{patient_pseudo_id}
guardian/v1/signals/evaluated/{severity}/{patient_pseudo_id}
guardian/v1/hitl/required/{case_id}
guardian/v1/hitl/decision/{case_id}
guardian/v1/notifications/clinician/{session_id}
guardian/v1/audit/{module}/{event_type}   ← cross-framework capture
```

---

## Project Structure

```
guardian-health/
├── docker-compose.yml          # Full stack: PostgreSQL, Neo4j, Qdrant, Solace
├── Makefile                    # up, down, infra-check, test-governance, seed
├── pyproject.toml              # pythonpath config, ruff settings
├── requirements.txt            # Python dependencies
├── .env.example                # Environment variable template
├── docs/
│   ├── GUARDIAN_Health_CaseStudy_ES.md   # Architecture document (Spanish)
│   ├── GUARDIAN_Health_CaseStudy_EN.md   # Architecture document (English)
│   └── adrs/                             # Architecture Decision Records
│       ├── ADR-001 through ADR-007
├── data/
│   ├── seed/
│   │   ├── generate_synthetic_fhir.py    # 12 FHIR R4 patient bundles
│   │   └── load_fhir_to_graph.py         # Neo4j + PostgreSQL loader
│   └── synthetic/
│       └── patients.ndjson               # Generated synthetic data
├── src/
│   ├── guardian/
│   │   ├── config.py                     # Pydantic-settings singleton
│   │   ├── state.py                      # LangGraph GuardianState TypedDict
│   │   ├── messaging.py                  # Solace client + Topics registry
│   │   ├── bus_orchestrator.py           # Decoupled pipeline coordinator
│   │   └── governance/
│   │       ├── audit.py                  # Cryptographic audit trail
│   │       ├── sanitiser.py              # Two-layer pseudonymisation
│   │       └── tracer.py                 # LangSmith configuration
│   ├── modules/
│   │   ├── vigil/
│   │   │   └── graph.py                  # LangGraph monitoring graph
│   │   ├── assess/
│   │   │   └── crew.py                   # CrewAI evaluation crew
│   │   └── respond/
│   │       └── coordinator.py            # Response coordination + HITL
│   ├── tools/
│   │   └── clinical_data.py              # LangChain tools for Neo4j
│   └── api/
│       └── main.py                       # FastAPI service (6 endpoints)
└── tests/
    ├── governance/
    │   ├── test_audit_trail.py           # Chain integrity + tamper detection
    │   └── test_sanitiser.py             # PII elimination verification
    ├── unit/
    └── integration/
```

---

## Quickstart

### Prerequisites

- Docker and Docker Compose
- Python 3.11
- `uv` package manager
- API keys: Groq, LangSmith, Pinecone, HuggingFace (see `.env.example`)

### Setup

```bash
# Clone the repository
git clone https://github.com/mgalanme/guardian-health.git
cd guardian-health

# Create virtual environment and install dependencies
uv venv .venv-guardian --python 3.11
source .venv-guardian/bin/activate
uv pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your API keys and passwords

# Start all services
make up

# Verify infrastructure
make infra-check

# Load synthetic clinical data
make seed

# Run governance test suite
make test-governance
```

### Run the Pipeline

```bash
# Synchronous pipeline (VIGIL → ASSESS → RESPOND in one process)
PYTHONPATH=. python -c "
from src.modules.vigil.graph import run_vigil
from src.modules.assess.crew import run_assess
from src.modules.respond.coordinator import run_respond

state = run_vigil('HIS-00005')
state = run_assess(state)
state, notification = run_respond(state)
print(notification)
"

# Decoupled pipeline via Solace Agent Mesh
PYTHONPATH=. python -c "
from src.guardian.bus_orchestrator import run_decoupled_pipeline
result = run_decoupled_pipeline('HIS-00005', centre_id='centre-a')
print(result)
"

# REST API
PYTHONPATH=. python src/api/main.py &
curl -X POST http://localhost:8000/pipeline/run \
  -H 'Content-Type: application/json' \
  -d '{"his_patient_id": "HIS-00005"}'
```

### Makefile Commands

```bash
make up              # Start all Docker containers
make down            # Stop all containers
make infra-check     # Verify PostgreSQL, Neo4j, Qdrant connectivity
make seed            # Generate synthetic data and load into graph
make test-governance # Run governance test suite (17 tests)
make test            # Run all tests
make logs            # Tail all container logs
make logs s=solace   # Tail a specific service
make reset           # Destroy all volumes (destructive)
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | System status and governance configuration |
| POST | `/pipeline/run` | Execute full pipeline for a patient |
| GET | `/pipeline/patients` | List monitored patients (pseudo IDs only) |
| GET | `/audit/summary` | Audit trail summary with chain integrity check |
| GET | `/audit/session/{id}` | Complete audit trail for a session |
| POST | `/hitl/decision` | Record a human HITL decision |

---

## Synthetic Data and Test Anomalies

The seed script generates 12 FHIR R4 patient bundles with three pre-seeded anomalies for VIGIL testing:

| Patient | Anomaly | Risk Level |
|---|---|---|
| HIS-00002 | Critically low potassium (2.8 mEq/L) + digoxin | HIGH — arrhythmia risk |
| HIS-00005 | Supratherapeutic INR (4.8) + warfarin | CRITICAL — bleeding risk |
| HIS-00008 | Rising creatinine (0.90→1.25→1.60 mg/dL) + NSAID | HIGH — nephrotoxicity risk |

---

## Governance Test Suite

The governance tests verify that system integrity guarantees hold under adversarial conditions, not just normal operation.

```bash
make test-governance
```

Key tests:

- `test_write_returns_hash` — every audit record produces a 64-character SHA-256 hash
- `test_chain_valid_after_multiple_writes` — chain integrity maintained across sessions
- `test_delete_is_forbidden` — agent role cannot delete audit records (raises InsufficientPrivilege)
- `test_update_is_forbidden` — agent role cannot modify audit records
- `test_chain_integrity_detects_tampering` — direct hash corruption is detected by `verify_chain_integrity()`
- `test_no_real_name_in_context` — patient names never appear in agent context
- `test_no_phone_in_context` — phone numbers in free text are redacted
- `test_clinical_data_preserved` — non-identifying clinical data (ICD-10, ATC codes) passes through

---

## Regulatory Context

This system is designed as if it were a high-risk AI system under EU AI Act Annex III, Category 5(a) (clinical decision support). Design decisions reflect:

- **GDPR Article 9** — special category health data, pseudonymisation, records of processing
- **EU AI Act** — technical documentation, traceability logging (Art. 12), human oversight (Art. 14)
- **Regulation (EU) No 1235/2010** — pharmacovigilance legislation, E2B R3 notification format
- **WHO-UMC causality criteria** — assessment methodology in the ASSESS module

---

## Architecture Decision Records

| ADR | Decision | Status |
|---|---|---|
| ADR-001 | LangGraph as agentic flow orchestrator | Accepted |
| ADR-002 | PostgreSQL for audit trail over time-series databases | Accepted |
| ADR-003 | Pseudonymisation rather than full anonymisation | Accepted |
| ADR-004 | Groq as primary LLM provider | Accepted |
| ADR-005 | Solace Agent Mesh for inter-module messaging | Accepted |
| ADR-006 | Separate repository for GUARDIAN-Health | Accepted |
| ADR-007 | Test database isolation for governance tests | Accepted |

Full ADR text: [`docs/adrs/`](./docs/adrs/)

---

## Part of the Agentic AI Bootcamp

GUARDIAN-Health is the third of three case studies:

1. **ARGOS-FCC** — Financial Crime Control (AML, Fraud, KYC/KYB)
2. **Spain Recommender** — Tourism recommendation with GraphRAG
3. **GUARDIAN-Health** — Pharmacovigilance and patient safety (this project)

Full bootcamp repository: [github.com/mgalanme/agentic-ai-bootcamp](https://github.com/mgalanme/agentic-ai-bootcamp)

---

*Author: Martín Galán — April 2026*
