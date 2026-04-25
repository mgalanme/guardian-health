"""
GUARDIAN-Health — LangGraph State Definition
The state is the single source of truth for the entire agentic flow.
The audit trail fields are first-class citizens, not optional extras.
"""

from typing import Annotated, Any, Optional
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages


# ── Sub-types ─────────────────────────────────────────────────────────────────

class RawSignal(TypedDict):
    signal_id: str
    signal_type: str        # lab_anomaly | prescription_alert | vital_sign | narrative
    source_system: str
    detected_at: str        # ISO 8601
    data_as_of: str         # ISO 8601 — when the underlying data was generated
    description: str
    confidence: float       # 0.0 to 1.0
    raw_data: dict          # sanitised source data


class AgentAction(TypedDict):
    action_id: str
    agent_id: str
    module: str
    action_type: str
    timestamp: str
    detail: dict
    result: Optional[dict]
    audit_record_hash: str  # links to the audit_trail table


class HITLRequest(TypedDict):
    request_id: str
    requested_at: str
    reason: str
    severity: str
    proposed_action: str


class HITLDecision(TypedDict):
    decision_id: str
    reviewer_id: str
    decided_at: str
    decision: str           # approve | reject | modify
    justification: str
    modified_action: Optional[str]


class Evaluation(TypedDict):
    evaluation_id: str
    evaluated_at: str
    severity: str           # MILD | MODERATE | SERIOUS | POTENTIALLY_SERIOUS
    causality: str          # CERTAIN | PROBABLE | POSSIBLE | UNLIKELY | UNCLASSIFIABLE
    reportable: bool
    confidence_level: float
    pharmacologist_reasoning: str
    clinician_reasoning: str
    regulatory_reasoning: str
    synthesis: str
    requires_hitl: bool
    agent_consensus: float  # 0.0 to 1.0


# ── Main State ────────────────────────────────────────────────────────────────

class GuardianState(TypedDict):

    # ── Identity and traceability (required from initialisation) ──────────────
    session_id: str
    patient_pseudo_id: str
    session_alias: str          # PAT-XXXX, used in LLM contexts
    trace_id: str               # LangSmith trace ID

    # ── Clinical context (sanitised, safe for LLM) ────────────────────────────
    clinical_context: dict
    prescriptions_active: list[dict]
    lab_results_recent: list[dict]
    nursing_notes: list[str]    # free text, sanitised

    # ── Signals and evaluations ───────────────────────────────────────────────
    raw_signals: list[RawSignal]
    evaluation: Optional[Evaluation]

    # ── HITL control ──────────────────────────────────────────────────────────
    requires_hitl: bool
    hitl_request: Optional[HITLRequest]
    hitl_decision: Optional[HITLDecision]

    # ── Governance: audit trail (append-only within the session) ──────────────
    agent_actions: list[AgentAction]

    # ── Flow control ──────────────────────────────────────────────────────────
    current_module: str         # VIGIL | ASSESS | RESPOND | SYSTEM
    flow_status: str            # initialised | monitoring | signal_detected |
                                # evaluating | hitl_pending | responding | complete | error
    error_state: Optional[str]

    # ── LangGraph messages (for nodes that use chat-style interaction) ─────────
    messages: Annotated[list, add_messages]


def initial_state(
    patient_pseudo_id: str,
    session_alias: str,
    session_id: str,
    trace_id: str,
) -> GuardianState:
    """
    Factory function for a clean initial state.
    Always use this instead of constructing the dict manually
    to guarantee all governance fields are present from the start.
    """
    return GuardianState(
        session_id=session_id,
        patient_pseudo_id=patient_pseudo_id,
        session_alias=session_alias,
        trace_id=trace_id,
        clinical_context={},
        prescriptions_active=[],
        lab_results_recent=[],
        nursing_notes=[],
        raw_signals=[],
        evaluation=None,
        requires_hitl=False,
        hitl_request=None,
        hitl_decision=None,
        agent_actions=[],
        current_module="SYSTEM",
        flow_status="initialised",
        error_state=None,
        messages=[],
    )
