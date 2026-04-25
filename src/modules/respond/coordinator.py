"""
GUARDIAN-Health — RESPOND Module
Coordinates the response actions following a completed ASSESS evaluation.
Handles: clinical notification draft, knowledge graph update, HITL simulation,
and session closure with full audit trail entry.
"""

import json
import uuid
from datetime import datetime, timezone

import structlog
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage
from neo4j import GraphDatabase

from src.guardian.config import get_settings
from src.guardian.state import GuardianState, HITLRequest, HITLDecision
from src.guardian.governance.audit import write_audit_record, ActionType, Module

log = structlog.get_logger(__name__)


def _llm():
    s = get_settings()
    return ChatGroq(
        api_key=s.groq_api_key,
        model="llama-3.3-70b-versatile",
        temperature=0.1,
        max_tokens=512,
    )


def _neo4j_driver():
    s = get_settings()
    return GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_username, s.neo4j_password))


# ── Action 1: Clinical notification draft ─────────────────────────────────────

def generate_clinical_notification(state: GuardianState) -> str:
    """Generate a plain-language clinical alert for the responsible clinician."""
    ev = state["evaluation"]
    signal = state["raw_signals"][0]
    llm = _llm()

    system = SystemMessage(content="""You are a pharmacovigilance specialist writing
a clinical alert for a hospital physician. Be concise (max 5 sentences), clinically
precise, and always include: what was detected, why it is significant, and what
action is recommended. Never include real patient names or identifiers.""")

    human = HumanMessage(content=f"""Draft a clinical alert for the following
pharmacovigilance signal:

Patient alias: {state['session_alias']}
Signal: {signal['description']}
Severity: {ev['severity']}
Causality: {ev['causality']}
Confidence: {ev['confidence_level']}
Recommended action from evaluation: {ev.get('synthesis', '')}

Write the alert in plain English suitable for a busy clinician.""")

    response = llm.invoke([system, human])

    write_audit_record(
        session_id=state["session_id"],
        module=Module.RESPOND,
        agent_id="respond-notifier",
        action_type=ActionType.NOTIFICATION_SENT,
        action_detail={"notification_type": "clinical_alert",
                        "severity": ev["severity"],
                        "session_alias": state["session_alias"]},
        state_snapshot={"flow_status": state["flow_status"]},
        result={"notification_length": len(response.content)},
    )

    return response.content


# ── Action 2: Knowledge graph update ──────────────────────────────────────────

def update_knowledge_graph(state: GuardianState) -> dict:
    """Record the adverse event and its evaluation in Neo4j."""
    ev = state["evaluation"]
    signal = state["raw_signals"][0]
    pseudo_id = state["patient_pseudo_id"]
    driver = _neo4j_driver()

    try:
        with driver.session() as session:
            session.run("""
                MATCH (p:Patient {pseudo_id: $pseudo_id})
                CREATE (ae:AdverseEvent {
                    event_id:       $event_id,
                    evaluation_id:  $eval_id,
                    detected_at:    $detected_at,
                    signal_type:    $signal_type,
                    pattern:        $pattern,
                    severity:       $severity,
                    causality:      $causality,
                    reportable:     $reportable,
                    confidence:     $confidence,
                    requires_hitl:  $requires_hitl,
                    session_id:     $session_id
                })
                MERGE (p)-[:EXPERIENCED]->(ae)
            """,
            pseudo_id=pseudo_id,
            event_id=str(uuid.uuid4()),
            eval_id=ev["evaluation_id"],
            detected_at=signal["detected_at"],
            signal_type=signal["signal_type"],
            pattern=signal["raw_data"].get("pattern", ""),
            severity=ev["severity"],
            causality=ev["causality"],
            reportable=ev["reportable"],
            confidence=ev["confidence_level"],
            requires_hitl=ev["requires_hitl"],
            session_id=state["session_id"],
            )

        write_audit_record(
            session_id=state["session_id"],
            module=Module.RESPOND,
            agent_id="respond-graph-updater",
            action_type=ActionType.STATE_TRANSITION,
            action_detail={"step": "knowledge_graph_update",
                            "pattern": signal["raw_data"].get("pattern"),
                            "severity": ev["severity"]},
            state_snapshot={"flow_status": state["flow_status"]},
            result={"node_type": "AdverseEvent", "pseudo_id": pseudo_id[:8]},
        )

        return {"status": "updated", "pseudo_id": pseudo_id[:8]}
    finally:
        driver.close()


# ── Action 3: HITL simulation ─────────────────────────────────────────────────

def process_hitl(state: GuardianState) -> GuardianState:
    """
    Simulate the HITL review process.
    In production, this would pause execution and await a real human decision
    via the FastAPI HITL endpoint. Here we simulate a senior clinician approval.
    """
    ev = state["evaluation"]
    request_id = str(uuid.uuid4())

    hitl_request = HITLRequest(
        request_id=request_id,
        requested_at=datetime.now(timezone.utc).isoformat(),
        reason=f"Severity {ev['severity']} requires mandatory clinical review",
        severity=ev["severity"],
        proposed_action="Review signal, confirm evaluation, approve notification",
    )

    write_audit_record(
        session_id=state["session_id"],
        module=Module.RESPOND,
        agent_id="respond-hitl",
        action_type=ActionType.HITL_REQUEST,
        action_detail={"request_id": request_id,
                        "severity": ev["severity"],
                        "reason": hitl_request["reason"]},
        state_snapshot={"flow_status": "hitl_pending"},
    )

    # Simulated human decision (in production: await FastAPI endpoint)
    hitl_decision = HITLDecision(
        decision_id=str(uuid.uuid4()),
        reviewer_id="SIMULATED-SENIOR-CLINICIAN-001",
        decided_at=datetime.now(timezone.utc).isoformat(),
        decision="approve",
        justification=(f"Signal clinically consistent with {ev['causality'].lower()} "
                       f"causality. {ev['severity']} severity confirmed. "
                       f"Notification to NCA warranted."),
        modified_action=None,
    )

    write_audit_record(
        session_id=state["session_id"],
        module=Module.RESPOND,
        agent_id="respond-hitl",
        action_type=ActionType.HITL_DECISION,
        action_detail={"decision": hitl_decision["decision"],
                        "reviewer_id": hitl_decision["reviewer_id"],
                        "justification": hitl_decision["justification"]},
        state_snapshot={"flow_status": "hitl_complete"},
        result={"decision": hitl_decision["decision"]},
    )

    state["hitl_request"] = hitl_request
    state["hitl_decision"] = hitl_decision
    log.info("respond.hitl.complete",
             decision=hitl_decision["decision"],
             reviewer=hitl_decision["reviewer_id"])
    return state


# ── Main entry point ───────────────────────────────────────────────────────────

def run_respond(assessed_state: GuardianState) -> GuardianState:
    """
    Run the RESPOND module on a fully assessed state.
    Orchestrates: HITL (if required), notification, graph update, closure.
    """
    if not assessed_state.get("evaluation"):
        log.warning("respond.skip", reason="no_evaluation")
        return assessed_state

    session_id = assessed_state["session_id"]
    ev = assessed_state["evaluation"]
    log.info("respond.start",
             session_id=session_id[:8],
             severity=ev["severity"],
             requires_hitl=ev["requires_hitl"])

    write_audit_record(
        session_id=session_id,
        module=Module.RESPOND,
        agent_id="respond-coordinator",
        action_type=ActionType.STATE_TRANSITION,
        action_detail={"step": "respond_start",
                        "severity": ev["severity"],
                        "requires_hitl": ev["requires_hitl"]},
        state_snapshot={"flow_status": assessed_state["flow_status"]},
    )

    # HITL if required
    if ev["requires_hitl"]:
        assessed_state = process_hitl(assessed_state)

    # Clinical notification
    notification = generate_clinical_notification(assessed_state)
    assessed_state["flow_status"] = "responding"

    # Knowledge graph update
    graph_result = update_knowledge_graph(assessed_state)

    # Session closure
    write_audit_record(
        session_id=session_id,
        module=Module.RESPOND,
        agent_id="respond-coordinator",
        action_type=ActionType.STATE_TRANSITION,
        action_detail={"step": "session_complete",
                        "hitl_decision": assessed_state.get(
                            "hitl_decision", {}).get("decision", "not_required"),
                        "graph_updated": graph_result["status"]},
        state_snapshot={"flow_status": "complete"},
        result={"total_actions": len(assessed_state["agent_actions"])},
    )

    assessed_state["flow_status"] = "complete"
    assessed_state["current_module"] = "RESPOND"

    log.info("respond.complete",
             session_id=session_id[:8],
             hitl=bool(assessed_state.get("hitl_decision")),
             flow_status="complete")

    return assessed_state, notification
