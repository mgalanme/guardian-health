"""
GUARDIAN-Health — Data Sanitiser
Pseudonymisation layer. No patient-identifiable data may enter
an agent context without passing through this module first.

Two-layer scheme:
  Layer 1: permanent pseudo_id (UUID) per real patient ID
           stored in patient_pseudo_mapping (restricted access)
  Layer 2: session_alias (short token) generated per agentic session
           used in LLM contexts, expires with the session
"""

import hashlib
import re
import uuid
from typing import Any

import psycopg2
import psycopg2.extras
import structlog

from src.guardian.config import get_settings

log = structlog.get_logger(__name__)

# Fields that must never appear in an agent context
_FORBIDDEN_FIELDS = {
    "name", "full_name", "first_name", "last_name", "surname",
    "national_id", "dni", "passport", "social_security",
    "address", "street", "postcode", "zip_code",
    "phone", "mobile", "telephone",
    "email", "email_address",
    "date_of_birth", "dob", "birth_date",
    "ip_address", "device_id",
}

# Regex patterns for direct identifiers in free text
_IDENTIFIER_PATTERNS = [
    re.compile(r'\b\d{8}[A-Z]\b'),           # Spanish DNI
    re.compile(r'\b[A-Z]{1,2}\d{6,8}\b'),    # Passport-like
    re.compile(r'\b\d{3}[-.\s]\d{2}[-.\s]\d{4}\b'),  # SSN-like
    re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b'),       # Email
    re.compile(r'\b(\+34|0034)?[679]\d{8}\b'),         # Spanish phone
]


def _ensure_mapping_table(cursor) -> None:
    """Create the mapping table if it does not exist."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS patient_pseudo_mapping (
            real_id     TEXT PRIMARY KEY,
            pseudo_id   UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def get_or_create_pseudo_id(real_patient_id: str) -> str:
    """
    Return the permanent pseudo_id for a real patient ID.
    Creates a new mapping if one does not exist.
    This function accesses the restricted mapping table.
    """
    settings = get_settings()
    conn = psycopg2.connect(settings.database_url)
    try:
        with conn:
            with conn.cursor() as cur:
                _ensure_mapping_table(cur)
                cur.execute(
                    "SELECT pseudo_id FROM patient_pseudo_mapping WHERE real_id = %s",
                    (real_patient_id,)
                )
                row = cur.fetchone()
                if row:
                    return str(row[0])

                cur.execute(
                    """
                    INSERT INTO patient_pseudo_mapping (real_id)
                    VALUES (%s)
                    RETURNING pseudo_id
                    """,
                    (real_patient_id,)
                )
                pseudo_id = str(cur.fetchone()[0])
                log.info(
                    "pseudo_id_created",
                    pseudo_id=pseudo_id[:8] + "...",
                )
                return pseudo_id
    finally:
        conn.close()


def generate_session_alias(pseudo_id: str, session_id: str) -> str:
    """
    Generate a short session-scoped alias for use in LLM contexts.
    Deterministic per (pseudo_id, session_id) pair but not reversible
    without both inputs.
    """
    combined = f"{pseudo_id}:{session_id}"
    digest = hashlib.sha256(combined.encode()).hexdigest()[:12]
    return f"PAT-{digest.upper()}"


def sanitise_dict(data: dict, session_alias: str) -> dict:
    """
    Remove or replace forbidden fields from a dict.
    Replaces any value in a forbidden field with the session_alias.
    Recursively processes nested dicts.
    """
    sanitised = {}
    for key, value in data.items():
        lower_key = key.lower()
        if lower_key in _FORBIDDEN_FIELDS:
            sanitised[key] = session_alias
            log.warning("field_sanitised", field=key, replaced_with="session_alias")
        elif isinstance(value, dict):
            sanitised[key] = sanitise_dict(value, session_alias)
        elif isinstance(value, str):
            sanitised[key] = sanitise_free_text(value, session_alias)
        else:
            sanitised[key] = value
    return sanitised


def sanitise_free_text(text: str, session_alias: str) -> str:
    """
    Scan free text for direct identifier patterns and redact them.
    """
    result = text
    for pattern in _IDENTIFIER_PATTERNS:
        result = pattern.sub(f"[REDACTED:{session_alias}]", result)
    return result


def build_agent_context(
    pseudo_id: str,
    session_id: str,
    raw_clinical_data: dict,
) -> dict:
    """
    Main entry point for preparing data for agent consumption.
    Returns a sanitised context dict safe to include in an LLM prompt.
    """
    session_alias = generate_session_alias(pseudo_id, session_id)

    context = sanitise_dict(raw_clinical_data, session_alias)
    context["_patient_alias"] = session_alias
    context["_pseudo_id"] = pseudo_id
    context["_session_id"] = session_id
    context["_sanitised"] = True

    log.info(
        "agent_context_built",
        session_alias=session_alias,
        fields_in_context=len(context),
    )
    return context
