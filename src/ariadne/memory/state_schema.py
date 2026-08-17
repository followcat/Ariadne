"""SQLite DDL for conversation-state events, projection, and lookup."""

from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state_documents (
    session_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    watermark_turn_id TEXT,
    event_seq INTEGER NOT NULL,
    projection_hash TEXT NOT NULL,
    state_json TEXT NOT NULL,
    migrated_from TEXT
);

CREATE TABLE IF NOT EXISTS state_events (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    source_turn_id TEXT NOT NULL,
    op_index INTEGER NOT NULL,
    op_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    PRIMARY KEY (session_id, seq, op_index)
);

CREATE TABLE IF NOT EXISTS state_versions (
    session_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    parent_version INTEGER NOT NULL,
    watermark_turn_id TEXT,
    source_turn_id TEXT,
    ops_json TEXT NOT NULL,
    operations_json TEXT NOT NULL,
    PRIMARY KEY (session_id, version)
);

CREATE TABLE IF NOT EXISTS state_projection_items (
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source_turn_id TEXT,
    status TEXT,
    PRIMARY KEY (session_id, kind, ref)
);

CREATE TABLE IF NOT EXISTS state_collection_members (
    session_id TEXT NOT NULL,
    collection_ref TEXT NOT NULL,
    position INTEGER NOT NULL,
    member_key TEXT NOT NULL,
    PRIMARY KEY (session_id, collection_ref, member_key)
);

CREATE TABLE IF NOT EXISTS state_idempotency (
    session_id TEXT NOT NULL,
    key TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (session_id, key)
);

CREATE INDEX IF NOT EXISTS idx_state_events_session_seq
    ON state_events (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_state_members_collection
    ON state_collection_members (session_id, collection_ref, position);
CREATE INDEX IF NOT EXISTS idx_state_items_kind
    ON state_projection_items (session_id, kind);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS state_fts USING fts5(
    session_id UNINDEXED,
    kind UNINDEXED,
    ref,
    body,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""
