"""SQL DDL for SQLite and PostgreSQL backends."""

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_seconds REAL NOT NULL DEFAULT 0.0,
    process_name TEXT NOT NULL DEFAULT '',
    level INTEGER NOT NULL DEFAULT 1,
    system_wide INTEGER NOT NULL DEFAULT 0,
    labels TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS samples (
    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    timestamp REAL NOT NULL,
    cpu INTEGER,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT '%',
    status TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_runs_process_name ON runs(process_name);
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_samples_run_id ON samples(run_id);
CREATE INDEX IF NOT EXISTS idx_samples_metric_name ON samples(metric_name);
CREATE INDEX IF NOT EXISTS idx_samples_timestamp ON samples(timestamp);
"""

POSTGRESQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    duration_seconds DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    process_name TEXT NOT NULL DEFAULT '',
    level INTEGER NOT NULL DEFAULT 1,
    system_wide BOOLEAN NOT NULL DEFAULT FALSE,
    labels JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS samples (
    sample_id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    timestamp DOUBLE PRECISION NOT NULL,
    cpu INTEGER,
    metric_name TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    unit TEXT NOT NULL DEFAULT '%',
    status TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_runs_process_name ON runs(process_name);
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_labels ON runs USING GIN(labels);
CREATE INDEX IF NOT EXISTS idx_samples_run_id ON samples(run_id);
CREATE INDEX IF NOT EXISTS idx_samples_metric_name ON samples(metric_name);
CREATE INDEX IF NOT EXISTS idx_samples_timestamp ON samples(timestamp);
"""
