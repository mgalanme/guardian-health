"""
GUARDIAN-Health — FHIR to Neo4j + PostgreSQL Loader
Reads the synthetic NDJSON bundles and populates:
  - Neo4j: clinical knowledge graph (patients, meds, diagnoses, observations)
  - PostgreSQL: pseudo_id mappings via the sanitiser
"""

import json
from pathlib import Path

from neo4j import GraphDatabase
import psycopg2

from src.guardian.config import get_settings
from src.guardian.governance.sanitiser import get_or_create_pseudo_id

NDJSON = Path("data/synthetic/patients.ndjson")


def get_neo4j_driver(settings):
    return GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_username, settings.neo4j_password)
    )


def load_bundle(tx, bundle: dict, pseudo_id: str):
    his_id = bundle["meta"]["his_patient_id"]
    anomaly = bundle["meta"].get("anomaly")

    patient_resource = next(
        e["resource"] for e in bundle["entry"]
        if e["resource"]["resourceType"] == "Patient"
    )
    gender = patient_resource.get("gender", "unknown")
    dob = patient_resource.get("birthDate", "")

    # Patient node
    tx.run("""
        MERGE (p:Patient {pseudo_id: $pseudo_id})
        SET p.his_id = $his_id,
            p.gender = $gender,
            p.birth_year = $birth_year,
            p.anomaly_flag = $anomaly
    """, pseudo_id=pseudo_id, his_id=his_id,
         gender=gender,
         birth_year=dob[:4] if dob else "unknown",
         anomaly=anomaly or "")

    for entry in bundle["entry"]:
        resource = entry["resource"]
        rtype = resource["resourceType"]

        if rtype == "Condition":
            coding = resource.get("code", {}).get("coding", [{}])[0]
            tx.run("""
                MERGE (d:Diagnosis {code: $code})
                SET d.display = $display
                WITH d
                MATCH (p:Patient {pseudo_id: $pseudo_id})
                MERGE (p)-[:HAS_DIAGNOSIS]->(d)
            """, code=coding.get("code", "UNK"),
                 display=coding.get("display", ""),
                 pseudo_id=pseudo_id)

        elif rtype == "MedicationRequest":
            coding = resource.get("medicationCodeableConcept", {}).get("coding", [{}])[0]
            dosage = resource.get("dosageInstruction", [{}])[0].get("text", "")
            atc = ""
            if "ATC:" in dosage:
                atc = dosage.split("ATC:")[-1].strip()
            tx.run("""
                MERGE (m:Medicine {snomed_code: $code})
                SET m.display = $display, m.atc_code = $atc
                WITH m
                MATCH (p:Patient {pseudo_id: $pseudo_id})
                MERGE (p)-[:PRESCRIBED]->(m)
            """, code=coding.get("code", "UNK"),
                 display=coding.get("display", ""),
                 atc=atc,
                 pseudo_id=pseudo_id)

        elif rtype == "Observation":
            coding = resource.get("code", {}).get("coding", [{}])[0]
            value_q = resource.get("valueQuantity", {})
            interp = resource.get("interpretation", [{}])[0]
            interp_code = interp.get("coding", [{}])[0].get("code", "N")
            effective = resource.get("effectiveDateTime", "")
            value = value_q.get("value")
            unit = value_q.get("unit", "")
            loinc = coding.get("code", "")
            display = coding.get("display", "")

            if value is not None:
                tx.run("""
                    MATCH (p:Patient {pseudo_id: $pseudo_id})
                    CREATE (o:Observation {
                        loinc: $loinc,
                        display: $display,
                        value: $value,
                        unit: $unit,
                        interpretation: $interp,
                        effective_date: $effective
                    })
                    MERGE (p)-[:HAS_OBSERVATION]->(o)
                """, pseudo_id=pseudo_id, loinc=loinc,
                     display=display, value=value, unit=unit,
                     interp=interp_code, effective=effective)


def main():
    settings = get_settings()
    driver = get_neo4j_driver(settings)

    print("Loading FHIR bundles into Neo4j and PostgreSQL...")
    print()

    with open(NDJSON, encoding="utf-8") as f:
        bundles = [json.loads(line) for line in f]

    loaded = 0
    with driver.session() as session:
        for bundle in bundles:
            his_id = bundle["meta"]["his_patient_id"]

            # Register pseudonymisation mapping in PostgreSQL
            pseudo_id = get_or_create_pseudo_id(his_id)

            # Load into Neo4j
            session.execute_write(load_bundle, bundle, pseudo_id)
            anomaly = bundle["meta"].get("anomaly") or "none"
            print(f"  {his_id} -> pseudo:{pseudo_id[:8]}... | anomaly: {anomaly[:50]}")
            loaded += 1

    driver.close()

    # Summary query
    print()
    print("Neo4j graph summary:")
    driver = get_neo4j_driver(settings)
    with driver.session() as session:
        for label in ["Patient", "Medicine", "Diagnosis", "Observation"]:
            count = session.run(f"MATCH (n:{label}) RETURN count(n) AS c").single()["c"]
            print(f"  {label:15s}: {count}")
        rels = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        print(f"  {'Relationships':15s}: {rels}")
    driver.close()

    print()
    print(f"Loaded {loaded} patients. Governance: all pseudo_id mappings registered.")


if __name__ == "__main__":
    main()
