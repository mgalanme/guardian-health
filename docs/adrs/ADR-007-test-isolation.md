# ADR-007: Test Database Isolation for Governance Tests

**Status:** Accepted  
**Date:** April 2026

## Context
The governance test `test_chain_integrity_detects_tampering` intentionally
corrupts an audit record via direct SQL to verify detection. When tests share
the production audit_trail table, this corruption persists and breaks the
chain for subsequent verification runs.

## Decision
Governance tests that mutate the audit_trail must either:
1. Use a separate PostgreSQL schema (`test_guardian`) isolated from `public`, or
2. Wrap mutations in a transaction that is always rolled back via pytest fixtures

The production `audit_trail` table must never be used as a test fixture target.

## Consequences
- Add `conftest.py` with a `db_schema` fixture that switches to `test_guardian`
  schema for the duration of the test session
- Update `postgres-init.sql` to create both `public` and `test_guardian` schemas
- Short-term workaround: `TRUNCATE audit_trail` before production verification runs
