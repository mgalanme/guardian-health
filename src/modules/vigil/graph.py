"""
GUARDIAN-Health — VIGIL Module
LangGraph stateful graph for continuous clinical signal monitoring.
Every node writes to the audit trail before and after execution.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Literal

import structlog
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END

from src.guardian.config import get_settings
from src.guardian.state import GuardianState, initial_state, RawSignal, AgentAction
from src.guardian.governance.audit import write_audit_record, ActionType, Module
from src.guardian.governance.sanitiser import (
    get_or_create_pseudo_id, generate_session_alias, build_agent_context
)
from src.tools.clinical_data import (
    get_patient_summary, get_lab_results, detect_drug_lab_interactions
)

log = structlog.get_logger(__name__)


def _llm():
    s = get_settings()
    return ChatGroq(
        api_key=s.groq_api_key,
        model="llama-3.3-70b-versatile",
        temperature=0.1,
        max_tokens=1024,
    )


def _record(state: GuardianState, action_type: ActionType,
            detail: dict, result: dict = None) -> str:
    """Write an audit record and return its hash."""
    return write_audit_record(
        session_id=state["session_id"],
        module=Module.VIGIL,
        agent_id="vigil-graph",
        action_type=action_type,
        action_detail=detail,
        state_snapshot={
            "flow_status": state["flow_status"],
            "signals_count": len(state["raw_signals"]),
            "requires_hitl": state["requires_hitl"],
        },
        result=result,
    )


# ── Node: initialise ───────────────────────────────────────────────────────────

def node_initialise(state: GuardianState) -> GuardianState:
    log.info("vigil.initialise", session_id=state["session_id"][:8])
    h = _record(state, ActionType.STATE_TRANSITION,
                {"step": "initialise", "module": "VIGIL"})
    state["current_module"] = "VIGIL"
    state["flow_status"] = "monitoring"
    state["agent_actions"].append(AgentAction(
        action_id=str(uuid.uuid4()),
        agent_id="vigil-graph",
        module="VIGIL",
        action_type="STATE_TRANSITION",
        timestamp=datetime.now(timezone.utc).isoformat(),
        detail={"step": "initialise"},
        result=None,
        audit_record_hash=h,
    ))
    return state


# ── Node: fetch_context ────────────────────────────────────────────────────────

def node_fetch_context(state: GuardianState) -> GuardianState:
    pseudo_id = state["patient_pseudo_id"]
    log.info("vigil.fetch_context", pseudo_id=pseudo_id[:8])

    summary_raw = get_patient_summary.invoke({"pseudo_id": pseudo_id})
    labs_raw = get_lab_results.invoke({"pseudo_id": pseudo_id, "test_name": ""})

    summary = json.loads(summary_raw)
    labs = json.loads(labs_raw)

    h = _record(state, ActionType.TOOL_USE,
                {"tool": "get_patient_summary+get_lab_results", "pseudo_id": pseudo_id},
                {"diagnoses_count": len(summary.get("diagnoses", [])),
                 "abnormal_labs": labs.get("count", 0)})

    state["clinical_context"] = summary
    state["lab_results_recent"] = labs.get("results", [])
    state["agent_actions"].append(AgentAction(
        action_id=str(uuid.uuid4()),
        agent_id="vigil-graph",
        module="VIGIL",
        action_type="TOOL_USE",
        timestamp=datetime.now(timezone.utc).isoformat(),
        detail={"tools": ["get_patient_summary", "get_lab_results"]},
        result={"abnormal_labs": labs.get("count", 0)},
        audit_record_hash=h,
    ))
    return state


# ── Node: sanitise ─────────────────────────────────────────────────────────────

def node_sanitise(state: GuardianState) -> GuardianState:
    log.info("vigil.sanitise", session_alias=state["session_alias"])
    context = build_agent_context(
        pseudo_id=state["patient_pseudo_id"],
        session_id=state["session_id"],
        raw_clinical_data=state["clinical_context"],
    )
    h = _record(state, ActionType.STATE_TRANSITION,
                {"step": "sanitise", "fields_sanitised": len(context)})
    state["clinical_context"] = context
    return state


# ── Node: monitor ──────────────────────────────────────────────────────────────

def node_monitor(state: GuardianState) -> GuardianState:
    pseudo_id = state["patient_pseudo_id"]
    log.info("vigil.monitor", pseudo_id=pseudo_id[:8])

    interactions_raw = detect_drug_lab_interactions.invoke({"pseudo_id": pseudo_id})
    interactions = json.loads(interactions_raw)

    signals = []
    for sig in interactions.get("signals", []):
        signals.append(RawSignal(
            signal_id=str(uuid.uuid4()),
            signal_type=sig["signal_type"],
            source_system="neo4j_clinical_graph",
            detected_at=datetime.now(timezone.utc).isoformat(),
            data_as_of=datetime.now(timezone.utc).isoformat(),
            description=sig["description"],
            confidence=0.90 if sig["risk_level"] in ["CRITICAL", "HIGH"] else 0.65,
            raw_data={
                "pattern": sig["pattern"],
                "risk_level": sig["risk_level"],
                "pseudo_id": pseudo_id,
            },
        ))

    h = _record(state, ActionType.SIGNAL_DETECTED,
                {"tool": "detect_drug_lab_interactions"},
                {"signals_found": len(signals)})

    state["raw_signals"] = signals
    state["agent_actions"].append(AgentAction(
        action_id=str(uuid.uuid4()),
        agent_id="vigil-monitor",
        module="VIGIL",
        action_type="SIGNAL_DETECTED",
        timestamp=datetime.now(timezone.utc).isoformat(),
        detail={"signals_found": len(signals)},
        result={"signal_ids": [s["signal_id"] for s in signals]},
        audit_record_hash=h,
    ))
    return state


# ── Node: correlate ────────────────────────────────────────────────────────────

def node_correlate(state: GuardianState) -> GuardianState:
    """Use the LLM to assess the clinical significance of detected signals."""
    if not state["raw_signals"]:
        log.info("vigil.correlate", result="no_signals")
        state["flow_status"] = "complete_no_signals"
        return state

    log.info("vigil.correlate", signals=len(state["raw_signals"]))
    llm = _llm()

    signals_text = "\n".join([
        f"- [{s['raw_data']['risk_level']}] {s['description']}"
        for s in state["raw_signals"]
    ])

    context = state["clinical_context"]
    system = SystemMessage(content="""You are a clinical pharmacovigilance specialist.
Your role is to assess the clinical significance of drug-laboratory signals detected
in hospitalised patients. Be concise, evidence-based, and flag if immediate action
is required. Never include patient names or real identifiers in your response.""")

    human = HumanMessage(content=f"""Patient alias: {state['session_alias']}
Gender: {context.get('gender', 'unknown')}
Diagnoses: {', '.join(context.get('diagnoses', []))}
Medications: {', '.join(context.get('medications', []))}

Detected signals:
{signals_text}

Provide a brief clinical assessment (3-5 sentences) of the combined risk and
whether immediate clinical review is warranted. End with: IMMEDIATE_REVIEW: YES or NO.""")

    response = llm.invoke([system, human])
    assessment = response.content

    requires_hitl = "IMMEDIATE_REVIEW: YES" in assessment.upper()

    h = _record(state, ActionType.LLM_CALL,
                {"model": "llama-3.3-70b-versatile",
                 "signals_assessed": len(state["raw_signals"])},
                {"requires_hitl": requires_hitl,
                 "assessment_length": len(assessment)})

    state["requires_hitl"] = requires_hitl
    state["flow_status"] = "signal_detected"
    state["messages"].append(response)
    state["agent_actions"].append(AgentAction(
        action_id=str(uuid.uuid4()),
        agent_id="vigil-correlator",
        module="VIGIL",
        action_type="LLM_CALL",
        timestamp=datetime.now(timezone.utc).isoformat(),
        detail={"signals_assessed": len(state["raw_signals"])},
        result={"requires_hitl": requires_hitl},
        audit_record_hash=h,
    ))

    log.info("vigil.correlate.complete",
             requires_hitl=requires_hitl,
             signals=len(state["raw_signals"]))
    return state


# ── Node: finalise ─────────────────────────────────────────────────────────────

def node_finalise(state: GuardianState) -> GuardianState:
    log.info("vigil.finalise",
             signals=len(state["raw_signals"]),
             requires_hitl=state["requires_hitl"])
    h = _record(state, ActionType.STATE_TRANSITION,
                {"step": "finalise"},
                {"signals": len(state["raw_signals"]),
                 "requires_hitl": state["requires_hitl"],
                 "actions_recorded": len(state["agent_actions"])})
    state["flow_status"] = "vigil_complete"
    return state


# ── Routing ────────────────────────────────────────────────────────────────────

def route_after_correlate(state: GuardianState) -> Literal["finalise", "finalise"]:
    """Routing placeholder: ASSESS module will be wired here in the next phase."""
    return "finalise"


# ── Graph assembly ─────────────────────────────────────────────────────────────

def build_vigil_graph() -> StateGraph:
    graph = StateGraph(GuardianState)

    graph.add_node("initialise",    node_initialise)
    graph.add_node("fetch_context", node_fetch_context)
    graph.add_node("sanitise",      node_sanitise)
    graph.add_node("monitor",       node_monitor)
    graph.add_node("correlate",     node_correlate)
    graph.add_node("finalise",      node_finalise)

    graph.set_entry_point("initialise")
    graph.add_edge("initialise",    "fetch_context")
    graph.add_edge("fetch_context", "sanitise")
    graph.add_edge("sanitise",      "monitor")
    graph.add_edge("monitor",       "correlate")
    graph.add_edge("correlate",     "finalise")
    graph.add_edge("finalise",      END)

    return graph.compile()


# ── Public entry point ─────────────────────────────────────────────────────────

def run_vigil(his_patient_id: str) -> GuardianState:
    """
    Run the VIGIL module for a single patient.
    his_patient_id: the real HIS identifier (pseudonymised internally).
    """
    session_id = str(uuid.uuid4())
    pseudo_id = get_or_create_pseudo_id(his_patient_id)
    alias = generate_session_alias(pseudo_id, session_id)
    trace_id = f"vigil-{session_id[:8]}"

    state = initial_state(
        patient_pseudo_id=pseudo_id,
        session_alias=alias,
        session_id=session_id,
        trace_id=trace_id,
    )

    graph = build_vigil_graph()
    final_state = graph.invoke(state)

    return final_state
