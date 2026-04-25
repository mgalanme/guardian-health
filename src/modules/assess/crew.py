"""
GUARDIAN-Health — ASSESS Module
CrewAI crew for structured pharmacovigilance signal evaluation.
Four specialised agents collaborate to produce a structured Evaluation.
Every crew execution is recorded in the audit trail.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog
from crewai import Agent, Crew, Process, Task
from crewai.llm import LLM
from pydantic import BaseModel, Field

from src.guardian.config import get_settings
from src.guardian.state import GuardianState, Evaluation, RawSignal
from src.guardian.governance.audit import write_audit_record, ActionType, Module

log = structlog.get_logger(__name__)


# ── Pydantic output models ─────────────────────────────────────────────────────

class CausalityAssessment(BaseModel):
    plausibility: str = Field(description="biological plausibility: HIGH/MODERATE/LOW")
    mechanism: str = Field(description="proposed pharmacological mechanism")
    supporting_evidence: str = Field(description="evidence supporting the causal link")
    confounding_factors: str = Field(description="alternative explanations considered")
    causality_level: str = Field(description="CERTAIN/PROBABLE/POSSIBLE/UNLIKELY/UNCLASSIFIABLE")


class ClinicalAssessment(BaseModel):
    patient_context: str = Field(description="relevant clinical context summary")
    severity_justification: str = Field(description="why this severity level was assigned")
    severity: str = Field(description="MILD/MODERATE/SERIOUS/POTENTIALLY_SERIOUS")
    reversibility: str = Field(description="REVERSIBLE/IRREVERSIBLE/UNKNOWN")
    immediate_action_needed: bool = Field(description="whether immediate clinical action is required")


class RegulatoryAssessment(BaseModel):
    reportable: bool = Field(description="whether this event is reportable to the NCA")
    reporting_timeline: str = Field(description="15_DAYS/90_DAYS/NOT_REQUIRED")
    regulatory_basis: str = Field(description="regulatory provision requiring notification")
    nca_form_required: str = Field(description="E2B_R3/NATIONAL_FORM/NONE")


class FinalEvaluation(BaseModel):
    evaluation_id: str = Field(description="unique evaluation identifier")
    severity: str = Field(description="MILD/MODERATE/SERIOUS/POTENTIALLY_SERIOUS")
    causality: str = Field(description="CERTAIN/PROBABLE/POSSIBLE/UNLIKELY/UNCLASSIFIABLE")
    reportable: bool = Field(description="whether NCA notification is required")
    confidence_level: float = Field(description="overall confidence 0.0-1.0")
    requires_hitl: bool = Field(description="whether human review is mandatory")
    pharmacologist_reasoning: str = Field(description="causality reasoning summary")
    clinician_reasoning: str = Field(description="clinical context reasoning summary")
    regulatory_reasoning: str = Field(description="regulatory determination reasoning")
    synthesis: str = Field(description="integrated final assessment narrative")
    recommended_action: str = Field(description="specific recommended next action")


# ── LLM factory ───────────────────────────────────────────────────────────────

def _crewai_llm() -> LLM:
    s = get_settings()
    return LLM(
        model="groq/llama-3.3-70b-versatile",
        api_key=s.groq_api_key,
        temperature=0.1,
    )


# ── Agent definitions ──────────────────────────────────────────────────────────

def build_pharmacologist_agent(llm: LLM) -> Agent:
    return Agent(
        role="Clinical Pharmacologist",
        goal="Assess the biological plausibility and causality of the drug-laboratory signal",
        backstory="""You are a senior clinical pharmacologist with 20 years of experience
in pharmacovigilance. You specialise in drug-laboratory interactions and adverse drug
reaction causality assessment using the WHO-UMC scale. You are methodical, evidence-based,
and always consider alternative explanations before attributing causality.""",
        llm=llm,
        verbose=False,
        allow_delegation=False,
    )


def build_clinician_agent(llm: LLM) -> Agent:
    return Agent(
        role="Senior Clinician",
        goal="Evaluate the clinical severity and patient context of the detected signal",
        backstory="""You are a consultant physician specialising in clinical risk assessment.
You evaluate adverse events in the context of the patient's full clinical picture,
considering comorbidities, co-medications, and the reversibility of potential harm.
You apply the Hartmann-Naranjo severity classification criteria.""",
        llm=llm,
        verbose=False,
        allow_delegation=False,
    )


def build_regulatory_agent(llm: LLM) -> Agent:
    return Agent(
        role="Pharmacovigilance Regulatory Specialist",
        goal="Determine the reportability of the event under applicable EU pharmacovigilance legislation",
        backstory="""You are a regulatory affairs specialist with deep expertise in EU
pharmacovigilance law: Regulation (EU) No 1235/2010, Directive 2010/84/EU, and EMA
Good Pharmacovigilance Practices (GVP) Modules. You determine whether events must be
reported to the National Competent Authority and within what timeframe.""",
        llm=llm,
        verbose=False,
        allow_delegation=False,
    )


def build_synthesis_agent(llm: LLM) -> Agent:
    return Agent(
        role="Pharmacovigilance Synthesis Specialist",
        goal="Integrate the assessments from all specialists into a final structured evaluation",
        backstory="""You are the lead pharmacovigilance specialist responsible for
integrating multi-disciplinary assessments into coherent, actionable evaluations.
You synthesise causality, severity, and regulatory determinations into a final
recommendation that a clinician can act upon immediately. You flag any inter-specialist
discrepancies explicitly.""",
        llm=llm,
        verbose=False,
        allow_delegation=False,
    )


# ── Task definitions ───────────────────────────────────────────────────────────

def build_causality_task(agent: Agent, signal: RawSignal,
                          clinical_context: dict) -> Task:
    return Task(
        description=f"""Assess the causal relationship between the detected drug-laboratory
signal and the suspected adverse drug reaction.

Patient alias: {clinical_context.get('_patient_alias', 'UNKNOWN')}
Diagnoses: {', '.join(clinical_context.get('diagnoses', []))}
Medications: {', '.join(clinical_context.get('medications', []))}

Signal detected:
- Type: {signal['signal_type']}
- Pattern: {signal['raw_data'].get('pattern', 'unknown')}
- Risk level: {signal['raw_data'].get('risk_level', 'unknown')}
- Description: {signal['description']}

Apply WHO-UMC causality assessment criteria.
Consider biological plausibility, temporal relationship, and alternative explanations.
Return your assessment as structured JSON matching the CausalityAssessment schema.""",
        expected_output="A structured causality assessment with plausibility, mechanism, evidence, confounders, and causality level",
        agent=agent,
    )


def build_severity_task(agent: Agent, signal: RawSignal,
                         clinical_context: dict) -> Task:
    return Task(
        description=f"""Evaluate the clinical severity of the detected adverse signal
in the context of this patient's clinical profile.

Patient alias: {clinical_context.get('_patient_alias', 'UNKNOWN')}
Diagnoses: {', '.join(clinical_context.get('diagnoses', []))}
Medications: {', '.join(clinical_context.get('medications', []))}

Signal:
- Pattern: {signal['raw_data'].get('pattern', 'unknown')}
- Risk level: {signal['raw_data'].get('risk_level', 'unknown')}
- Description: {signal['description']}

Apply severity criteria: life-threatening, hospitalisation-prolonging,
disabling, congenital anomaly, or death. Classify as MILD/MODERATE/SERIOUS/POTENTIALLY_SERIOUS.
Determine whether immediate clinical action is required.
Return structured JSON matching the ClinicalAssessment schema.""",
        expected_output="A structured severity assessment with severity level, justification, reversibility, and immediate action flag",
        agent=agent,
    )


def build_regulatory_task(agent: Agent, signal: RawSignal) -> Task:
    return Task(
        description=f"""Determine the regulatory reportability of this adverse drug signal
under EU pharmacovigilance legislation.

Signal pattern: {signal['raw_data'].get('pattern', 'unknown')}
Risk level: {signal['raw_data'].get('risk_level', 'unknown')}
Signal description: {signal['description']}

Apply: Regulation (EU) No 1235/2010, Directive 2010/84/EU, EMA GVP Module VI.
Determine: is this reportable? What is the applicable timeline (15 or 90 days)?
Which regulatory form is required (E2B R3)?
Return structured JSON matching the RegulatoryAssessment schema.""",
        expected_output="A structured regulatory assessment with reportability, timeline, legal basis, and form type",
        agent=agent,
    )


def build_synthesis_task(agent: Agent, signal: RawSignal) -> Task:
    return Task(
        description=f"""You have received assessments from the Clinical Pharmacologist,
Senior Clinician, and Regulatory Specialist regarding this signal:

Pattern: {signal['raw_data'].get('pattern', 'unknown')}
Description: {signal['description']}

Integrate all three assessments into a final evaluation. Identify any discrepancies
between the specialists and resolve them with explicit justification.
Assign an overall confidence level (0.0-1.0) based on the degree of specialist consensus.
Determine whether mandatory human review (HITL) is required: it is ALWAYS required
for SERIOUS or POTENTIALLY_SERIOUS events, and for any reportable event.
Return structured JSON matching the FinalEvaluation schema with all fields populated.
The evaluation_id must be a new UUID.""",
        expected_output="A complete FinalEvaluation JSON with all fields including evaluation_id, severity, causality, reportable, confidence_level, requires_hitl, and synthesis narrative",
        agent=agent,
    )


# ── Crew runner ────────────────────────────────────────────────────────────────

def run_assess(vigil_state: GuardianState) -> GuardianState:
    """
    Run the ASSESS crew on the first signal from VIGIL state.
    Updates the state with the structured Evaluation and returns it.
    """
    if not vigil_state["raw_signals"]:
        log.info("assess.skip", reason="no_signals")
        vigil_state["flow_status"] = "complete_no_signals"
        return vigil_state

    signal = vigil_state["raw_signals"][0]
    context = vigil_state["clinical_context"]
    session_id = vigil_state["session_id"]

    log.info("assess.start",
             session_id=session_id[:8],
             pattern=signal["raw_data"].get("pattern"))

    write_audit_record(
        session_id=session_id,
        module=Module.ASSESS,
        agent_id="assess-crew",
        action_type=ActionType.STATE_TRANSITION,
        action_detail={"step": "assess_start",
                        "signal_pattern": signal["raw_data"].get("pattern"),
                        "signal_risk": signal["raw_data"].get("risk_level")},
        state_snapshot={"flow_status": vigil_state["flow_status"],
                         "signals": len(vigil_state["raw_signals"])},
    )

    llm = _crewai_llm()

    pharmacologist = build_pharmacologist_agent(llm)
    clinician = build_clinician_agent(llm)
    regulatory = build_regulatory_agent(llm)
    synthesis = build_synthesis_agent(llm)

    crew = Crew(
        agents=[pharmacologist, clinician, regulatory, synthesis],
        tasks=[
            build_causality_task(pharmacologist, signal, context),
            build_severity_task(clinician, signal, context),
            build_regulatory_task(regulatory, signal),
            build_synthesis_task(synthesis, signal),
        ],
        process=Process.sequential,
        verbose=False,
    )

    result = crew.kickoff()
    raw_output = result.raw if hasattr(result, "raw") else str(result)

    # Parse the FinalEvaluation from the synthesis agent output
    evaluation = _parse_evaluation(raw_output, session_id)

    write_audit_record(
        session_id=session_id,
        module=Module.ASSESS,
        agent_id="assess-crew",
        action_type=ActionType.EVALUATION_COMPLETE,
        action_detail={"pattern": signal["raw_data"].get("pattern"),
                        "severity": evaluation["severity"],
                        "causality": evaluation["causality"],
                        "reportable": evaluation["reportable"]},
        state_snapshot={"flow_status": "evaluated"},
        result={"confidence": evaluation["confidence_level"],
                "requires_hitl": evaluation["requires_hitl"]},
    )

    vigil_state["evaluation"] = evaluation
    vigil_state["requires_hitl"] = evaluation["requires_hitl"]
    vigil_state["flow_status"] = "evaluated"
    vigil_state["current_module"] = "ASSESS"

    log.info("assess.complete",
             severity=evaluation["severity"],
             causality=evaluation["causality"],
             requires_hitl=evaluation["requires_hitl"],
             confidence=evaluation["confidence_level"])

    return vigil_state


def _parse_evaluation(raw_output: str, session_id: str) -> Evaluation:
    """
    Extract a FinalEvaluation from the synthesis agent raw output.
    Falls back to a safe default if parsing fails, logging the failure.
    """
    try:
        start = raw_output.find("{")
        end = raw_output.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(raw_output[start:end])

            # Normalise reportable: accept bool or any string containing REPORT
            reportable_raw = data.get("reportable", True)
            if isinstance(reportable_raw, str):
                reportable = "REPORT" in reportable_raw.upper()
            else:
                reportable = bool(reportable_raw)

            # Normalise evaluation_id: reject placeholder values
            eval_id = data.get("evaluation_id", "")
            if not eval_id or "UUID-" in eval_id or len(eval_id) < 30:
                eval_id = str(uuid.uuid4())

            # Normalise synthesis: build narrative from available fields
            synthesis = data.get("synthesis", "")
            if not synthesis or synthesis.strip().startswith("{") or synthesis.strip().startswith("`"):
                recommended = data.get("recommended_action", "")
                pharm = data.get("pharmacologist_reasoning", "")
                clin = data.get("clinician_reasoning", "")
                reg = data.get("regulatory_reasoning", "")
                parts = [p for p in [pharm, clin, reg, recommended] if p and not p.startswith("{")]
                if parts:
                    synthesis = " | ".join(parts[:3])
                else:
                    synthesis = (f"Severity: {data.get('severity','?')} | "
                                 f"Causality: {data.get('causality','?')} | "
                                 f"Reportable: {reportable} | "
                                 f"Confidence: {data.get('confidence_level','?')}")

            return Evaluation(
                evaluation_id=eval_id,
                evaluated_at=datetime.now(timezone.utc).isoformat(),
                severity=data.get("severity", "SERIOUS"),
                causality=data.get("causality", "POSSIBLE"),
                reportable=reportable,
                confidence_level=float(data.get("confidence_level", 0.7)),
                pharmacologist_reasoning=data.get("pharmacologist_reasoning", ""),
                clinician_reasoning=data.get("clinician_reasoning", ""),
                regulatory_reasoning=data.get("regulatory_reasoning", ""),
                synthesis=synthesis,
                requires_hitl=bool(data.get("requires_hitl", True)),
                agent_consensus=float(data.get("confidence_level", 0.7)),
            )
    except Exception as e:
        log.warning("assess.parse_failed", error=str(e), session_id=session_id[:8])

    # Safe default: treat as serious requiring HITL
    return Evaluation(
        evaluation_id=str(uuid.uuid4()),
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        severity="SERIOUS",
        causality="POSSIBLE",
        reportable=True,
        confidence_level=0.5,
        pharmacologist_reasoning="Parse error — defaulting to safe values",
        clinician_reasoning="Parse error — defaulting to safe values",
        regulatory_reasoning="Reportable by default when parsing fails",
        synthesis=raw_output[:500],
        requires_hitl=True,
        agent_consensus=0.5,
    )
