"""
GUARDIAN-Health — Audit Trail
Append-only, cryptographically chained record of every agentic action.
This module must be initialised before any agent or tool is invoked.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import psycopg2
import psycopg2.extras
import structlog

from src.guardian.config import get_settings

log = structlog.get_logger(__name__)


class ActionType(str, Enum):
    LLM_CALL        = "LLM_CALL"
    TOOL_USE        = "TOOL_USE"
    HITL_REQUEST    = "HITL_REQUEST"
    HITL_DECISION   = "HITL_DECISION"
    STATE_TRANSITION = "STATE_TRANSITION"
    SIGNAL_DETECTED = "SIGNAL_DETECTED"
    EVALUATION_COMPLETE = "EVALUATION_COMPLETE"
    NOTIFICATION_SENT   = "NOTIFICATION_SENT"
    ERROR           = "ERROR"


class Module(str, Enum):
    VIGIL   = "VIGIL"
    ASSESS  = "ASSESS"
    RESPOND = "RESPOND"
    SYSTEM  = "SYSTEM"


def _compute_hash(data: dict) -> str:
    """SHA-256 hash of a dict, serialised with sorted keys for determinism."""
    serialised = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(serialised.encode()).hexdigest()


def _get_connection():
    settings = get_settings()
    return psycopg2.connect(settings.database_url)


def _get_last_hash(cursor) -> str:
    """Retrieve the hash of the last record in the chain.
    Returns a fixed genesis hash if the table is empty.
    """
    cursor.execute(
        "SELECT record_hash FROM audit_trail ORDER BY sequence_num DESC LIMIT 1"
    )
    row = cursor.fetchone()
    if row is None:
        return hashlib.sha256(b"GUARDIAN-HEALTH-GENESIS").hexdigest()
    return row[0]


def write_audit_record(
    session_id: str,
    module: Module,
    agent_id: str,
    action_type: ActionType,
    action_detail: dict,
    state_snapshot: dict,
    result: Optional[dict] = None,
) -> str:
    """
    Write a single immutable record to the audit trail.
    Returns the record_hash of the inserted record.
    Raises if audit_trail_enabled is False (skips silently in that case).
    """
    settings = get_settings()
    if not settings.audit_trail_enabled:
        log.info("audit_trail_disabled", session_id=session_id)
        return "disabled"

    record_id = str(uuid.uuid4())
    recorded_at = datetime.now(timezone.utc).isoformat()

    conn = _get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                previous_hash = _get_last_hash(cur)

                record_payload = {
                    "id": record_id,
                    "session_id": session_id,
                    "recorded_at": recorded_at,
                    "module": module.value,
                    "agent_id": agent_id,
                    "action_type": action_type.value,
                    "action_detail": action_detail,
                    "state_snapshot": state_snapshot,
                    "result": result,
                    "previous_hash": previous_hash,
                }
                record_hash = _compute_hash(record_payload)

                cur.execute(
                    """
                    INSERT INTO audit_trail (
                        id, session_id, recorded_at, module, agent_id,
                        action_type, action_detail, state_snapshot, result,
                        previous_hash, record_hash
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s
                    )
                    """,
                    (
                        record_id,
                        session_id,
                        recorded_at,
                        module.value,
                        agent_id,
                        action_type.value,
                        json.dumps(action_detail),
                        json.dumps(state_snapshot),
                        json.dumps(result) if result else None,
                        previous_hash,
                        record_hash,
                    ),
                )

        log.info(
            "audit_record_written",
            session_id=session_id,
            module=module.value,
            action_type=action_type.value,
            record_hash=record_hash[:12] + "...",
        )
        return record_hash

    finally:
        conn.close()


def verify_chain_integrity() -> dict:
    """
    Walk the entire audit trail and verify the cryptographic chain.
    Returns a report: {valid: bool, records_checked: int, broken_at: optional int}
    This is the function governance auditors call.
    """
    conn = _get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT sequence_num, id, session_id, recorded_at, module,
                       agent_id, action_type, action_detail, state_snapshot,
                       result, previous_hash, record_hash
                FROM audit_trail
                ORDER BY sequence_num ASC
                """
            )
            rows = cur.fetchall()

            if not rows:
                return {"valid": True, "records_checked": 0, "broken_at": None}

            genesis_hash = hashlib.sha256(b"GUARDIAN-HEALTH-GENESIS").hexdigest()
            expected_previous = genesis_hash
            broken_at = None

            for row in rows:
                if row["previous_hash"] != expected_previous:
                    broken_at = row["sequence_num"]
                    break

                recomputed_payload = {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "recorded_at": row["recorded_at"].isoformat()
                    if hasattr(row["recorded_at"], "isoformat")
                    else row["recorded_at"],
                    "module": row["module"],
                    "agent_id": row["agent_id"],
                    "action_type": row["action_type"],
                    "action_detail": row["action_detail"],
                    "state_snapshot": row["state_snapshot"],
                    "result": row["result"],
                    "previous_hash": row["previous_hash"],
                }
                recomputed_hash = _compute_hash(recomputed_payload)

                if recomputed_hash != row["record_hash"]:
                    broken_at = row["sequence_num"]
                    break

                expected_previous = row["record_hash"]

            return {
                "valid": broken_at is None,
                "records_checked": len(rows),
                "broken_at": broken_at,
            }
    finally:
        conn.close()
