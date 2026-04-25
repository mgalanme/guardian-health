"""
Governance tests for the data sanitiser.
Verify that no PII can reach an agent context.
"""

import uuid
import pytest

from src.guardian.governance.sanitiser import (
    get_or_create_pseudo_id,
    generate_session_alias,
    build_agent_context,
    sanitise_free_text,
)


SESSION = str(uuid.uuid4())
REAL_ID = f"HIS-TEST-{uuid.uuid4().hex[:8].upper()}"


class TestPseudonymisation:

    def test_pseudo_id_is_uuid(self):
        pseudo_id = get_or_create_pseudo_id(REAL_ID)
        assert len(pseudo_id) == 36
        assert pseudo_id.count("-") == 4

    def test_pseudo_id_is_stable(self):
        p1 = get_or_create_pseudo_id(REAL_ID)
        p2 = get_or_create_pseudo_id(REAL_ID)
        assert p1 == p2

    def test_different_patients_get_different_ids(self):
        p1 = get_or_create_pseudo_id(f"HIS-A-{uuid.uuid4().hex[:6]}")
        p2 = get_or_create_pseudo_id(f"HIS-B-{uuid.uuid4().hex[:6]}")
        assert p1 != p2

    def test_session_alias_format(self):
        pseudo_id = get_or_create_pseudo_id(REAL_ID)
        alias = generate_session_alias(pseudo_id, SESSION)
        assert alias.startswith("PAT-")
        assert len(alias) == 16  # PAT- + 12 hex chars

    def test_session_alias_differs_across_sessions(self):
        pseudo_id = get_or_create_pseudo_id(REAL_ID)
        a1 = generate_session_alias(pseudo_id, str(uuid.uuid4()))
        a2 = generate_session_alias(pseudo_id, str(uuid.uuid4()))
        assert a1 != a2


class TestContextSanitisation:

    PII_DATA = {
        "name": "Maria Fernández",
        "date_of_birth": "1980-07-15",
        "national_id": "87654321X",
        "email": "maria@example.com",
        "age_band": "40-49",
        "sex": "F",
        "diagnosis_codes": ["J45", "E11"],
        "active_prescriptions": ["salbutamol 100mcg"],
        "notes": "Call 612999888 for follow-up. Email maria@example.com",
    }

    def test_no_real_name_in_context(self):
        pseudo_id = get_or_create_pseudo_id(REAL_ID)
        ctx = build_agent_context(pseudo_id, SESSION, self.PII_DATA)
        assert "Maria Fernández" not in str(ctx)

    def test_no_national_id_in_context(self):
        pseudo_id = get_or_create_pseudo_id(REAL_ID)
        ctx = build_agent_context(pseudo_id, SESSION, self.PII_DATA)
        assert "87654321X" not in str(ctx)

    def test_no_email_in_context(self):
        pseudo_id = get_or_create_pseudo_id(REAL_ID)
        ctx = build_agent_context(pseudo_id, SESSION, self.PII_DATA)
        assert "maria@example.com" not in str(ctx)

    def test_no_phone_in_context(self):
        pseudo_id = get_or_create_pseudo_id(REAL_ID)
        ctx = build_agent_context(pseudo_id, SESSION, self.PII_DATA)
        assert "612999888" not in str(ctx)

    def test_clinical_data_preserved(self):
        pseudo_id = get_or_create_pseudo_id(REAL_ID)
        ctx = build_agent_context(pseudo_id, SESSION, self.PII_DATA)
        assert ctx["age_band"] == "40-49"
        assert ctx["sex"] == "F"
        assert "J45" in str(ctx["diagnosis_codes"])

    def test_sanitised_flag_present(self):
        pseudo_id = get_or_create_pseudo_id(REAL_ID)
        ctx = build_agent_context(pseudo_id, SESSION, self.PII_DATA)
        assert ctx["_sanitised"] is True
