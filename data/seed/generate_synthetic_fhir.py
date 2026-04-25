"""
GUARDIAN-Health — Synthetic FHIR R4 Data Generator
Produces realistic (but entirely fictional) clinical data for development
and testing. No real patient data is used or implied.

Output: data/synthetic/patients.ndjson (FHIR Bundle per patient)

Anomalies pre-seeded for VIGIL module testing:
  - Patient idx 2: potassium critically low (2.8 mEq/L) + digoxin prescription
  - Patient idx 5: INR supratherapeutic (4.8) + warfarin prescription
  - Patient idx 8: creatinine rising trend + NSAID prescription
"""

import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from faker import Faker

fake = Faker("en_GB")
random.seed(42)

OUTPUT_DIR = Path(__file__).parent.parent / "synthetic"
OUTPUT_FILE = OUTPUT_DIR / "patients.ndjson"

# ── Reference data ─────────────────────────────────────────────────────────────

DIAGNOSES = [
    {"code": "I10",  "display": "Essential hypertension",         "snomed": "38341003"},
    {"code": "E11",  "display": "Type 2 diabetes mellitus",       "snomed": "44054006"},
    {"code": "I48",  "display": "Atrial fibrillation",            "snomed": "49436004"},
    {"code": "N18",  "display": "Chronic kidney disease",         "snomed": "709044004"},
    {"code": "J45",  "display": "Asthma",                        "snomed": "195967001"},
    {"code": "M79",  "display": "Soft tissue disorder",           "snomed": "57676002"},
    {"code": "K21",  "display": "Gastro-oesophageal reflux",      "snomed": "235595009"},
    {"code": "F32",  "display": "Depressive episode",             "snomed": "35489007"},
]

MEDICATIONS = [
    {"code": "372665008", "display": "Warfarin sodium 5mg",       "atc": "B01AA03"},
    {"code": "372687004", "display": "Metformin hydrochloride 850mg", "atc": "A10BA02"},
    {"code": "386871004", "display": "Digoxin 125mcg",            "atc": "C01AA05"},
    {"code": "372756006", "display": "Lisinopril 10mg",           "atc": "C09AA03"},
    {"code": "372571008", "display": "Atorvastatin 40mg",         "atc": "C10AA05"},
    {"code": "387207008", "display": "Ibuprofen 400mg",           "atc": "M01AE01"},
    {"code": "372687004", "display": "Amlodipine 5mg",            "atc": "C08CA01"},
    {"code": "386849001", "display": "Omeprazole 20mg",           "atc": "A02BC01"},
    {"code": "372687004", "display": "Salbutamol 100mcg inhaler", "atc": "R03AC02"},
    {"code": "387207008", "display": "Sertraline 50mg",           "atc": "N06AB06"},
]

LAB_TESTS = {
    "potassium":   {"loinc": "2823-3",  "unit": "mEq/L",   "low": 3.5, "high": 5.0,  "normal": (3.8, 4.5)},
    "creatinine":  {"loinc": "2160-0",  "unit": "mg/dL",   "low": 0.6, "high": 1.2,  "normal": (0.7, 1.1)},
    "inr":         {"loinc": "6301-6",  "unit": "ratio",   "low": 0.8, "high": 3.5,  "normal": (0.9, 1.1)},
    "glucose":     {"loinc": "2345-7",  "unit": "mg/dL",   "low": 70,  "high": 140,  "normal": (80, 120)},
    "haemoglobin": {"loinc": "718-7",   "unit": "g/dL",    "low": 11.5,"high": 17.5, "normal": (12.5, 16.0)},
    "sodium":      {"loinc": "2951-2",  "unit": "mEq/L",   "low": 135, "high": 145,  "normal": (137, 143)},
    "alt":         {"loinc": "1742-6",  "unit": "U/L",     "low": 7,   "high": 56,   "normal": (15, 40)},
}

VITALS = {
    "heart_rate":  {"loinc": "8867-4",  "unit": "beats/min", "normal": (60, 100)},
    "systolic_bp": {"loinc": "8480-6",  "unit": "mmHg",      "normal": (100, 140)},
    "diastolic_bp":{"loinc": "8462-4",  "unit": "mmHg",      "normal": (60, 90)},
    "temperature": {"loinc": "8310-5",  "unit": "Cel",       "normal": (36.1, 37.5)},
    "spo2":        {"loinc": "59408-5", "unit": "%",         "normal": (95, 100)},
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def fhir_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def days_ago(n: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=n)
    return dt.isoformat()


def observation(patient_id: str, encounter_id: str,
                name: str, meta: dict, value: float,
                effective_date: str, status: str = "final") -> dict:
    low = meta.get("low")
    high = meta.get("high")
    if low and high:
        interp_code = "N" if low <= value <= high else ("L" if value < low else "H")
        interp_display = {"N": "Normal", "L": "Low", "H": "High"}[interp_code]
    else:
        interp_code = "N"
        interp_display = "Normal"

    obs = {
        "resourceType": "Observation",
        "id": fhir_id(),
        "status": status,
        "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                   "code": "laboratory", "display": "Laboratory"}]}],
        "code": {"coding": [{"system": "http://loinc.org",
                              "code": meta["loinc"], "display": name}]},
        "subject": {"reference": f"Patient/{patient_id}"},
        "encounter": {"reference": f"Encounter/{encounter_id}"},
        "effectiveDateTime": effective_date,
        "valueQuantity": {"value": round(value, 2), "unit": meta["unit"],
                          "system": "http://unitsofmeasure.org", "code": meta["unit"]},
        "interpretation": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                                         "code": interp_code, "display": interp_display}]}],
        "referenceRange": [{"low": {"value": low, "unit": meta["unit"]},
                             "high": {"value": high, "unit": meta["unit"]}}] if low and high else [],
    }
    return obs


# ── Patient builder ────────────────────────────────────────────────────────────

def build_patient_bundle(idx: int) -> dict:
    patient_id = fhir_id()
    encounter_id = fhir_id()
    sex = random.choice(["male", "female"])
    age = random.randint(45, 85)
    dob = (datetime.now() - timedelta(days=age * 365)).strftime("%Y-%m-%d")
    admission_date = days_ago(random.randint(1, 7))

    patient = {
        "resourceType": "Patient",
        "id": patient_id,
        "identifier": [{"system": "http://healthbridge.example/his", "value": f"HIS-{idx:05d}"}],
        "name": [{"use": "official",
                  "family": fake.last_name(),
                  "given": [fake.first_name_male() if sex == "male" else fake.first_name_female()]}],
        "gender": sex,
        "birthDate": dob,
        "address": [{"city": fake.city(), "country": "GB"}],
    }

    encounter = {
        "resourceType": "Encounter",
        "id": encounter_id,
        "status": "in-progress",
        "class": {"code": "IMP", "display": "inpatient encounter"},
        "subject": {"reference": f"Patient/{patient_id}"},
        "period": {"start": admission_date},
        "serviceType": {"coding": [{"code": "394802001", "display": "General medicine"}]},
    }

    # Diagnoses: 1 to 3 random
    conditions = []
    for diag in random.sample(DIAGNOSES, random.randint(1, 3)):
        conditions.append({
            "resourceType": "Condition",
            "id": fhir_id(),
            "clinicalStatus": {"coding": [{"code": "active"}]},
            "verificationStatus": {"coding": [{"code": "confirmed"}]},
            "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10",
                                  "code": diag["code"], "display": diag["display"]}]},
            "subject": {"reference": f"Patient/{patient_id}"},
            "onsetDateTime": days_ago(random.randint(30, 365)),
        })

    # Medications: 2 to 4 random
    medications = []
    for med in random.sample(MEDICATIONS, random.randint(2, 4)):
        medications.append({
            "resourceType": "MedicationRequest",
            "id": fhir_id(),
            "status": "active",
            "intent": "order",
            "medicationCodeableConcept": {
                "coding": [{"system": "http://snomed.info/sct",
                             "code": med["code"], "display": med["display"]}],
                "text": med["display"],
            },
            "subject": {"reference": f"Patient/{patient_id}"},
            "authoredOn": admission_date,
            "dosageInstruction": [{"text": f"As directed. ATC: {med['atc']}"}],
        })

    # Lab observations: normal values by default
    observations = []
    for lab_name, meta in LAB_TESTS.items():
        low_n, high_n = meta["normal"]
        value = round(random.uniform(low_n, high_n), 2)
        observations.append(
            observation(patient_id, encounter_id, lab_name, meta, value, days_ago(1))
        )

    # Vital signs
    for vital_name, meta in VITALS.items():
        low_n, high_n = meta["normal"]
        value = round(random.uniform(low_n, high_n), 1)
        obs = {
            "resourceType": "Observation",
            "id": fhir_id(),
            "status": "final",
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                       "code": "vital-signs"}]}],
            "code": {"coding": [{"system": "http://loinc.org",
                                  "code": meta["loinc"], "display": vital_name}]},
            "subject": {"reference": f"Patient/{patient_id}"},
            "encounter": {"reference": f"Encounter/{encounter_id}"},
            "effectiveDateTime": days_ago(0),
            "valueQuantity": {"value": value, "unit": meta["unit"]},
        }
        observations.append(obs)

    # ── Anomaly injection ──────────────────────────────────────────────────────
    anomaly_note = None

    if idx == 2:
        # Critically low potassium + digoxin: high risk of cardiac arrhythmia
        for obs in observations:
            if obs.get("code", {}).get("coding", [{}])[0].get("display") == "potassium":
                obs["valueQuantity"]["value"] = 2.8
                obs["interpretation"] = [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                                                       "code": "LL", "display": "Critical low"}]}]
        medications.append({
            "resourceType": "MedicationRequest",
            "id": fhir_id(),
            "status": "active",
            "intent": "order",
            "medicationCodeableConcept": {
                "coding": [{"system": "http://snomed.info/sct",
                             "code": "372665008", "display": "Digoxin 125mcg"}],
                "text": "Digoxin 125mcg",
            },
            "subject": {"reference": f"Patient/{patient_id}"},
            "authoredOn": admission_date,
            "dosageInstruction": [{"text": "Once daily. ATC: C01AA05"}],
        })
        anomaly_note = "VIGIL_TEST: critically low K+ (2.8) + digoxin — arrhythmia risk"

    elif idx == 5:
        # Supratherapeutic INR + warfarin: bleeding risk
        for obs in observations:
            if obs.get("code", {}).get("coding", [{}])[0].get("display") == "inr":
                obs["valueQuantity"]["value"] = 4.8
                obs["interpretation"] = [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation",
                                                       "code": "HH", "display": "Critical high"}]}]
        medications.append({
            "resourceType": "MedicationRequest",
            "id": fhir_id(),
            "status": "active",
            "intent": "order",
            "medicationCodeableConcept": {
                "coding": [{"system": "http://snomed.info/sct",
                             "code": "372665008", "display": "Warfarin sodium 5mg"}],
                "text": "Warfarin sodium 5mg",
            },
            "subject": {"reference": f"Patient/{patient_id}"},
            "authoredOn": admission_date,
            "dosageInstruction": [{"text": "Once daily. ATC: B01AA03"}],
        })
        anomaly_note = "VIGIL_TEST: supratherapeutic INR (4.8) + warfarin — bleeding risk"

    elif idx == 8:
        # Rising creatinine + NSAID: nephrotoxicity signal
        for i, date_offset in enumerate([3, 2, 1]):
            meta = LAB_TESTS["creatinine"]
            value = round(0.9 + (i * 0.35), 2)  # 0.90 -> 1.25 -> 1.60 (rising)
            obs = observation(
                patient_id, encounter_id, "creatinine", meta,
                value, days_ago(date_offset)
            )
            obs["note"] = [{"text": f"Day {i+1} of rising trend"}]
            observations.append(obs)
        medications.append({
            "resourceType": "MedicationRequest",
            "id": fhir_id(),
            "status": "active",
            "intent": "order",
            "medicationCodeableConcept": {
                "coding": [{"system": "http://snomed.info/sct",
                             "code": "387207008", "display": "Ibuprofen 400mg"}],
                "text": "Ibuprofen 400mg",
            },
            "subject": {"reference": f"Patient/{patient_id}"},
            "authoredOn": admission_date,
            "dosageInstruction": [{"text": "Three times daily. ATC: M01AE01"}],
        })
        anomaly_note = "VIGIL_TEST: rising creatinine (0.90->1.25->1.60) + NSAID — nephrotoxicity risk"

    # ── Assemble bundle ────────────────────────────────────────────────────────
    entries = (
        [patient, encounter]
        + conditions
        + medications
        + observations
    )

    bundle = {
        "resourceType": "Bundle",
        "id": fhir_id(),
        "type": "collection",
        "timestamp": now_iso(),
        "meta": {
            "his_patient_id": f"HIS-{idx:05d}",
            "synthetic": True,
            "generated_at": now_iso(),
            "anomaly": anomaly_note,
        },
        "entry": [{"resource": r} for r in entries],
    }
    return bundle


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    n_patients = 12

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        for idx in range(n_patients):
            bundle = build_patient_bundle(idx)
            f.write(json.dumps(bundle, ensure_ascii=False) + "\n")

    print(f"Generated {n_patients} synthetic FHIR bundles -> {OUTPUT_FILE}")
    print()
    print("Anomalies seeded for VIGIL testing:")
    print("  Patient idx 2 : K+ critically low (2.8) + digoxin")
    print("  Patient idx 5 : INR supratherapeutic (4.8) + warfarin")
    print("  Patient idx 8 : Creatinine rising trend + NSAID")


if __name__ == "__main__":
    main()
