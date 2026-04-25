-- GUARDIAN-Health: PostgreSQL initialisation
-- Creates the audit trail table and the agent role with restricted permissions

-- Audit trail table (append-only by policy)
CREATE TABLE IF NOT EXISTS audit_trail (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sequence_num    BIGSERIAL NOT NULL,
    session_id      UUID NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    module          VARCHAR(20) NOT NULL,
    agent_id        VARCHAR(100) NOT NULL,
    action_type     VARCHAR(50) NOT NULL,
    action_detail   JSONB NOT NULL,
    state_snapshot  JSONB NOT NULL,
    result          JSONB,
    previous_hash   VARCHAR(64) NOT NULL,
    record_hash     VARCHAR(64) NOT NULL,
    CONSTRAINT audit_trail_sequence UNIQUE (sequence_num)
);

-- LangGraph checkpointer tables
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id       TEXT NOT NULL,
    checkpoint_ns   TEXT NOT NULL DEFAULT '',
    checkpoint_id   TEXT NOT NULL,
    parent_id       TEXT,
    type            TEXT,
    checkpoint      JSONB NOT NULL,
    metadata        JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id       TEXT NOT NULL,
    checkpoint_ns   TEXT NOT NULL DEFAULT '',
    checkpoint_id   TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    idx             INT NOT NULL,
    channel         TEXT NOT NULL,
    type            TEXT,
    value           JSONB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

-- Agent role with restricted permissions (no DELETE or UPDATE on audit_trail)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'guardian_agent_role') THEN
        CREATE ROLE guardian_agent_role;
    END IF;
END$$;

GRANT SELECT, INSERT ON audit_trail TO guardian_agent_role;
GRANT ALL ON checkpoints TO guardian_agent_role;
GRANT ALL ON checkpoint_writes TO guardian_agent_role;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO guardian_agent_role;

-- Index for audit queries
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_trail(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_module ON audit_trail(module, recorded_at);
CREATE INDEX IF NOT EXISTS idx_audit_sequence ON audit_trail(sequence_num);
