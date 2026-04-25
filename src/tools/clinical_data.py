"""
GUARDIAN-Health — Clinical Data Tools
Tools used by the VIGIL module agents to access Silver Layer data.
All outputs are pseudonymised. No tool returns real patient identifiers.
"""

import json
from datetime import datetime, timezone
from typing import Any

from langchain_core.tools import tool
from neo4j import GraphDatabase

from src.guardian.config import get_settings
from src.guardian.governance.audit import write_audit_record, ActionType, Module


def _neo4j_driver():
    s = get_settings()
    return GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_username, s.neo4j_password))


@tool
def get_patient_summary(pseudo_id: str) -> str:
    """
    Retrieve a clinical summary for a pseudonymised patient from the knowledge graph.
    Returns diagnoses, active medications, and latest observations.
    Input: pseudo_id (UUID string, never a real patient identifier).
    """
    driver = _neo4j_driver()
    try:
        with driver.session() as session:
            result = session.run("""
                MATCH (p:Patient {pseudo_id: $pseudo_id})
                OPTIONAL MATCH (p)-[:HAS_DIAGNOSIS]->(d:Diagnosis)
                OPTIONAL MATCH (p)-[:PRESCRIBED]->(m:Medicine)
                RETURN p.gender AS gender,
                       p.birth_year AS birth_year,
                       p.anomaly_flag AS anomaly_flag,
                       collect(DISTINCT d.code + ': ' + d.display) AS diagnoses,
                       collect(DISTINCT m.display + ' [ATC:' + m.atc_code + ']') AS medications
            """, pseudo_id=pseudo_id)
            row = result.single()
            if not row:
                return json.dumps({"error": "Patient not found", "pseudo_id": pseudo_id})

            return json.dumps({
                "pseudo_id": pseudo_id,
                "gender": row["gender"],
                "birth_year": row["birth_year"],
                "diagnoses": row["diagnoses"],
                "medications": row["medications"],
            })
    finally:
        driver.close()


@tool
def get_lab_results(pseudo_id: str, test_name: str = "") -> str:
    """
    Retrieve recent laboratory results for a pseudonymised patient.
    Optionally filter by test name (e.g. 'potassium', 'creatinine', 'inr').
    Flags results with interpretation codes L (low), H (high), LL (critical low), HH (critical high).
    Input: pseudo_id (UUID string). test_name is optional.
    """
    driver = _neo4j_driver()
    try:
        with driver.session() as session:
            if test_name:
                result = session.run("""
                    MATCH (p:Patient {pseudo_id: $pseudo_id})-[:HAS_OBSERVATION]->(o:Observation)
                    WHERE toLower(o.display) CONTAINS toLower($test_name)
                    RETURN o.display AS test, o.value AS value, o.unit AS unit,
                           o.interpretation AS interpretation, o.effective_date AS date
                    ORDER BY o.effective_date DESC
                    LIMIT 10
                """, pseudo_id=pseudo_id, test_name=test_name)
            else:
                result = session.run("""
                    MATCH (p:Patient {pseudo_id: $pseudo_id})-[:HAS_OBSERVATION]->(o:Observation)
                    WHERE o.interpretation IN ['L','H','LL','HH']
                    RETURN o.display AS test, o.value AS value, o.unit AS unit,
                           o.interpretation AS interpretation, o.effective_date AS date
                    ORDER BY o.effective_date DESC
                    LIMIT 20
                """, pseudo_id=pseudo_id)

            rows = [dict(r) for r in result]
            return json.dumps({
                "pseudo_id": pseudo_id,
                "filter": test_name or "abnormal_only",
                "results": rows,
                "count": len(rows),
            })
    finally:
        driver.close()


@tool
def detect_drug_lab_interactions(pseudo_id: str) -> str:
    """
    Detect clinically significant drug-laboratory value interactions for a patient.
    Checks known high-risk combinations: digoxin+low_K, warfarin+high_INR, NSAID+rising_creatinine.
    Returns a list of detected signals with risk level.
    Input: pseudo_id (UUID string).
    """
    driver = _neo4j_driver()
    signals = []

    try:
        with driver.session() as session:

            # Pattern 1: Digoxin + critical/low potassium
            r1 = session.run("""
                MATCH (p:Patient {pseudo_id: $pseudo_id})-[:PRESCRIBED]->(m:Medicine)
                WHERE toLower(m.display) CONTAINS 'digoxin'
                MATCH (p)-[:HAS_OBSERVATION]->(o:Observation)
                WHERE toLower(o.display) CONTAINS 'potassium'
                  AND o.interpretation IN ['L', 'LL']
                RETURN m.display AS medicine, o.value AS value,
                       o.unit AS unit, o.interpretation AS interp
            """, pseudo_id=pseudo_id)
            for row in r1:
                signals.append({
                    "signal_type": "DRUG_LAB_INTERACTION",
                    "risk_level": "HIGH" if row["interp"] == "LL" else "MODERATE",
                    "pattern": "digoxin_low_potassium",
                    "description": f"Digoxin prescribed with low K+ ({row['value']} {row['unit']}). Risk of cardiac arrhythmia.",
                    "medicine": row["medicine"],
                    "lab_value": row["value"],
                    "lab_unit": row["unit"],
                    "interpretation": row["interp"],
                })

            # Pattern 2: Warfarin + supratherapeutic INR
            r2 = session.run("""
                MATCH (p:Patient {pseudo_id: $pseudo_id})-[:PRESCRIBED]->(m:Medicine)
                WHERE toLower(m.display) CONTAINS 'warfarin'
                MATCH (p)-[:HAS_OBSERVATION]->(o:Observation)
                WHERE toLower(o.display) CONTAINS 'inr'
                  AND o.value > 3.5
                RETURN m.display AS medicine, o.value AS value, o.unit AS unit
            """, pseudo_id=pseudo_id)
            for row in r2:
                risk = "CRITICAL" if row["value"] > 4.5 else "HIGH"
                signals.append({
                    "signal_type": "DRUG_LAB_INTERACTION",
                    "risk_level": risk,
                    "pattern": "warfarin_supratherapeutic_inr",
                    "description": f"Warfarin prescribed with supratherapeutic INR ({row['value']}). Bleeding risk.",
                    "medicine": row["medicine"],
                    "lab_value": row["value"],
                    "lab_unit": row["unit"],
                })

            # Pattern 3: NSAID + rising creatinine (multiple observations)
            r3 = session.run("""
                MATCH (p:Patient {pseudo_id: $pseudo_id})-[:PRESCRIBED]->(m:Medicine)
                WHERE (m.atc_code STARTS WITH 'M01' OR toLower(m.display) CONTAINS 'ibuprofen' OR toLower(m.display) CONTAINS 'naproxen' OR toLower(m.display) CONTAINS 'diclofenac')
                MATCH (p)-[:HAS_OBSERVATION]->(o:Observation)
                WHERE toLower(o.display) CONTAINS 'creatinine'
                RETURN m.display AS medicine, o.value AS value,
                       o.unit AS unit, o.effective_date AS date
                ORDER BY o.effective_date ASC
            """, pseudo_id=pseudo_id)
            creatinine_rows = [dict(r) for r in r3]
            if len(creatinine_rows) >= 2:
                values = [r["value"] for r in creatinine_rows]
                # Use max vs min for robustness: avoids ordering ambiguity
                # when multiple observations share the same effective_date
                if max(values) > min(values) * 1.2:
                    signals.append({
                        "signal_type": "DRUG_LAB_TREND",
                        "risk_level": "HIGH",
                        "pattern": "nsaid_rising_creatinine",
                        "description": f"NSAID prescribed with rising creatinine ({values[0]} -> {values[-1]} {creatinine_rows[0]['unit']}). Nephrotoxicity risk.",
                        "medicine": creatinine_rows[0]["medicine"],
                        "trend": values,
                    })

        return json.dumps({
            "pseudo_id": pseudo_id,
            "signals_detected": len(signals),
            "signals": signals,
        })
    finally:
        driver.close()
