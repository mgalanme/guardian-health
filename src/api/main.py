"""
GUARDIAN-Health — FastAPI Service
Exposes the three-module pipeline as a REST API.
All endpoints log to the audit trail. No raw patient data is accepted.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src.guardian.config import get_settings
from src.guardian.governance.audit import verify_chain_integrity, write_audit_record, ActionType, Module

log = structlog.get_logger(__name__)


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info("guardian.api.startup",
             env=settings.guardian_env,
             audit_trail=settings.audit_trail_enabled,
             hitl=settings.hitl_enabled)
    yield
    log.info("guardian.api.shutdown")


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="GUARDIAN-Health API",
    description="""Governance and Accountability by Design in Agentic AI.
    Pharmacovigilance signal detection, evaluation, and response coordination.""",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── Request / Response models ──────────────────────────────────────────────────

class PipelineRequest(BaseModel):
    his_patient_id: str = Field(
        ...,
        description="Hospital Information System patient ID (e.g. HIS-00005)",
        examples=["HIS-00005"],
    )
    modules: list[str] = Field(
        default=["vigil", "assess", "respond"],
        description="Modules to execute in order",
    )


class PipelineResponse(BaseModel):
    session_id: str
    patient_alias: str
    flow_status: str
    signals_detected: int
    evaluation: dict | None
    hitl_decision: dict | None
    notification_preview: str | None
    audit_records_written: int
    duration_seconds: float


class AuditSummaryResponse(BaseModel):
    total_records: int
    total_sessions: int
    chain_valid: bool
    broken_at_sequence: int | None
    records_by_module: dict
    first_record: str | None
    last_record: str | None


class HITLDecisionRequest(BaseModel):
    session_id: str = Field(..., description="Session ID to act upon")
    reviewer_id: str = Field(..., description="ID of the human reviewer")
    decision: str = Field(..., description="approve | reject | modify")
    justification: str = Field(..., description="Clinical justification for the decision")
    modified_action: str | None = Field(None, description="Alternative action if decision is modify")


class HITLDecisionResponse(BaseModel):
    decision_id: str
    session_id: str
    decision: str
    recorded_at: str
    audit_hash: str


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
def health_check():
    """System health check. Returns API status and governance configuration."""
    settings = get_settings()
    return {
        "status": "healthy",
        "environment": settings.guardian_env,
        "audit_trail_enabled": settings.audit_trail_enabled,
        "hitl_enabled": settings.hitl_enabled,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/pipeline/run", response_model=PipelineResponse, tags=["Pipeline"])
def run_pipeline(request: PipelineRequest):
    """
    Execute the GUARDIAN-Health pipeline for a single patient.
    Runs VIGIL (detection), ASSESS (evaluation), and RESPOND (coordination)
    in sequence. All actions are recorded in the audit trail.
    Accepts only HIS patient IDs — never real names or national identifiers.
    """
    import time
    start = time.time()

    from src.modules.vigil.graph import run_vigil
    from src.modules.assess.crew import run_assess
    from src.modules.respond.coordinator import run_respond

    log.info("api.pipeline.start", his_id=request.his_patient_id)

    try:
        state = None
        notification = None

        if "vigil" in request.modules:
            state = run_vigil(request.his_patient_id)

        if state and "assess" in request.modules and state["raw_signals"]:
            state = run_assess(state)

        if state and "respond" in request.modules and state.get("evaluation"):
            state, notification = run_respond(state)

        if not state:
            raise HTTPException(status_code=400, detail="No modules executed")

        duration = round(time.time() - start, 2)
        ev = state.get("evaluation")

        return PipelineResponse(
            session_id=state["session_id"],
            patient_alias=state["session_alias"],
            flow_status=state["flow_status"],
            signals_detected=len(state["raw_signals"]),
            evaluation=dict(ev) if ev else None,
            hitl_decision=dict(state["hitl_decision"]) if state.get("hitl_decision") else None,
            notification_preview=notification[:300] if notification else None,
            audit_records_written=len(state["agent_actions"]),
            duration_seconds=duration,
        )

    except Exception as e:
        log.error("api.pipeline.error", error=str(e), his_id=request.his_patient_id)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/audit/summary", response_model=AuditSummaryResponse, tags=["Audit"])
def get_audit_summary():
    """
    Return a summary of the audit trail with chain integrity verification.
    This is the governance endpoint: auditors and the DPO use this to verify
    that the system has operated correctly and no records have been tampered with.
    """
    import psycopg2
    from src.guardian.config import get_settings

    settings = get_settings()
    conn = psycopg2.connect(settings.database_url)

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT session_id) FROM audit_trail")
            total, sessions = cur.fetchone()

            cur.execute("""
                SELECT module, COUNT(*) FROM audit_trail
                GROUP BY module ORDER BY module
            """)
            by_module = {row[0]: row[1] for row in cur.fetchall()}

            cur.execute("SELECT MIN(recorded_at), MAX(recorded_at) FROM audit_trail")
            first, last = cur.fetchone()

        report = verify_chain_integrity()

        return AuditSummaryResponse(
            total_records=total,
            total_sessions=sessions,
            chain_valid=report["valid"],
            broken_at_sequence=report["broken_at"],
            records_by_module=by_module,
            first_record=str(first)[:19] if first else None,
            last_record=str(last)[:19] if last else None,
        )
    finally:
        conn.close()


@app.get("/audit/session/{session_id}", tags=["Audit"])
def get_session_audit(session_id: str):
    """
    Return the complete audit trail for a specific session.
    Shows every action taken by every agent in chronological order.
    """
    import psycopg2
    import psycopg2.extras
    from src.guardian.config import get_settings

    settings = get_settings()
    conn = psycopg2.connect(settings.database_url)

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT sequence_num, recorded_at, module, agent_id,
                       action_type, action_detail, result, record_hash
                FROM audit_trail
                WHERE session_id = %s
                ORDER BY sequence_num ASC
            """, (session_id,))
            rows = cur.fetchall()

        if not rows:
            raise HTTPException(status_code=404, detail="Session not found")

        return {
            "session_id": session_id,
            "record_count": len(rows),
            "records": [
                {
                    "seq": r["sequence_num"],
                    "timestamp": str(r["recorded_at"])[:19],
                    "module": r["module"],
                    "agent": r["agent_id"],
                    "action": r["action_type"],
                    "detail": r["action_detail"],
                    "result": r["result"],
                    "hash": r["record_hash"][:16] + "...",
                }
                for r in rows
            ],
        }
    finally:
        conn.close()


@app.post("/hitl/decision", response_model=HITLDecisionResponse, tags=["HITL"])
def record_hitl_decision(request: HITLDecisionRequest):
    """
    Record a human-in-the-loop decision for a session requiring clinical review.
    This endpoint is used by clinicians and the medical director to approve,
    reject, or modify agent-proposed actions. Every decision is permanently
    recorded in the audit trail with the reviewer's identity and justification.
    """
    import uuid
    from src.guardian.governance.audit import write_audit_record, ActionType, Module

    if request.decision not in ("approve", "reject", "modify"):
        raise HTTPException(
            status_code=400,
            detail="decision must be one of: approve, reject, modify"
        )

    decision_id = str(uuid.uuid4())
    recorded_at = datetime.now(timezone.utc).isoformat()

    audit_hash = write_audit_record(
        session_id=request.session_id,
        module=Module.RESPOND,
        agent_id=f"human:{request.reviewer_id}",
        action_type=ActionType.HITL_DECISION,
        action_detail={
            "decision_id": decision_id,
            "reviewer_id": request.reviewer_id,
            "decision": request.decision,
            "justification": request.justification,
            "modified_action": request.modified_action,
        },
        state_snapshot={"source": "api_hitl_endpoint"},
        result={"decision": request.decision},
    )

    log.info("api.hitl.decision",
             session_id=request.session_id[:8],
             reviewer=request.reviewer_id,
             decision=request.decision)

    return HITLDecisionResponse(
        decision_id=decision_id,
        session_id=request.session_id,
        decision=request.decision,
        recorded_at=recorded_at,
        audit_hash=audit_hash,
    )


@app.get("/pipeline/patients", tags=["Pipeline"])
def list_monitored_patients():
    """
    Return the list of patients currently in the system with their pseudo IDs.
    Never returns real patient identifiers.
    """
    from neo4j import GraphDatabase
    from src.guardian.config import get_settings

    settings = get_settings()
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_username, settings.neo4j_password)
    )
    try:
        with driver.session() as session:
            result = session.run("""
                MATCH (p:Patient)
                OPTIONAL MATCH (p)-[:EXPERIENCED]->(ae:AdverseEvent)
                RETURN p.pseudo_id AS pseudo_id,
                       p.gender AS gender,
                       p.birth_year AS birth_year,
                       p.anomaly_flag AS anomaly_flag,
                       count(ae) AS adverse_events
                ORDER BY adverse_events DESC
            """)
            patients = [dict(r) for r in result]

        return {
            "total_patients": len(patients),
            "patients": [
                {
                    "pseudo_id": p["pseudo_id"][:8] + "...",
                    "gender": p["gender"],
                    "birth_year": p["birth_year"],
                    "adverse_events_recorded": p["adverse_events"],
                    "anomaly_flag": bool(p["anomaly_flag"]),
                }
                for p in patients
            ],
        }
    finally:
        driver.close()


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "src.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.guardian_env == "development",
        log_level="warning",
    )
