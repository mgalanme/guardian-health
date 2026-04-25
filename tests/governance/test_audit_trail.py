"""
Governance tests for the audit trail.
These tests verify the cryptographic integrity guarantees,
not just functional correctness.
"""

import hashlib
import json
import uuid
import pytest
import psycopg2

from src.guardian.governance.audit import (
    write_audit_record,
    verify_chain_integrity,
    ActionType,
    Module,
)
from src.guardian.config import get_settings


def new_session() -> str:
    return str(uuid.uuid4())


class TestAuditWrite:

    def test_write_returns_hash(self):
        h = write_audit_record(
            session_id=new_session(),
            module=Module.SYSTEM,
            agent_id="test",
            action_type=ActionType.STATE_TRANSITION,
            action_detail={"test": True},
            state_snapshot={"status": "test"},
        )
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex digest

    def test_consecutive_hashes_differ(self):
        session = new_session()
        h1 = write_audit_record(
            session_id=session,
            module=Module.VIGIL,
            agent_id="test",
            action_type=ActionType.SIGNAL_DETECTED,
            action_detail={"signal": "a"},
            state_snapshot={"step": 1},
        )
        h2 = write_audit_record(
            session_id=session,
            module=Module.VIGIL,
            agent_id="test",
            action_type=ActionType.SIGNAL_DETECTED,
            action_detail={"signal": "b"},
            state_snapshot={"step": 2},
        )
        assert h1 != h2

    def test_chain_valid_after_multiple_writes(self):
        session = new_session()
        for i in range(5):
            write_audit_record(
                session_id=session,
                module=Module.ASSESS,
                agent_id=f"agent-{i}",
                action_type=ActionType.LLM_CALL,
                action_detail={"iteration": i},
                state_snapshot={"step": i},
            )
        report = verify_chain_integrity()
        assert report["valid"] is True
        assert report["broken_at"] is None


class TestAuditIntegrity:

    def test_delete_is_forbidden(self):
        """The agent role must not be able to delete audit records."""
        settings = get_settings()
        conn = psycopg2.connect(settings.database_url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM audit_trail LIMIT 1")
                row = cur.fetchone()
                if row is None:
                    pytest.skip("No records in audit_trail to test deletion")
                record_id = row[0]
                with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                    cur.execute(
                        "SET ROLE guardian_agent_role; "
                        "DELETE FROM audit_trail WHERE id = %s",
                        (record_id,)
                    )
        finally:
            conn.rollback()
            conn.close()

    def test_update_is_forbidden(self):
        """The agent role must not be able to modify audit records."""
        settings = get_settings()
        conn = psycopg2.connect(settings.database_url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM audit_trail LIMIT 1")
                row = cur.fetchone()
                if row is None:
                    pytest.skip("No records in audit_trail to test update")
                record_id = row[0]
                with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                    cur.execute(
                        "SET ROLE guardian_agent_role; "
                        "UPDATE audit_trail SET module = 'TAMPERED' WHERE id = %s",
                        (record_id,)
                    )
        finally:
            conn.rollback()
            conn.close()

    def test_chain_integrity_detects_tampering(self):
        """Directly corrupting a record hash must be detected by verify_chain_integrity."""
        session = new_session()
        write_audit_record(
            session_id=session,
            module=Module.SYSTEM,
            agent_id="tamper-test",
            action_type=ActionType.STATE_TRANSITION,
            action_detail={"tamper": "before"},
            state_snapshot={"status": "pre"},
        )
        settings = get_settings()
        conn = psycopg2.connect(settings.database_url)
        try:
            with conn:
                with conn.cursor() as cur:
                    # Bypass the agent role and tamper directly as superuser
                    cur.execute(
                        """
                        UPDATE audit_trail
                        SET record_hash = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
                        WHERE session_id = %s
                        """,
                        (session,)
                    )
        finally:
            conn.close()

        report = verify_chain_integrity()
        assert report["valid"] is False
        assert report["broken_at"] is not None
