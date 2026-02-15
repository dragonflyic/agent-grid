-- Agent Grid Database Schema
-- NOTE: Authoritative schema is managed by Alembic migrations.
-- This file is a reference snapshot. Run `alembic upgrade head` to set up.

-- Tracks all agent executions
CREATE TABLE executions (
    id UUID PRIMARY KEY,
    issue_id TEXT NOT NULL,
    repo_url TEXT NOT NULL,
    status TEXT NOT NULL,  -- pending, running, completed, failed
    prompt TEXT,
    result TEXT,
    mode TEXT DEFAULT 'implement',
    pr_number INT,
    branch TEXT,
    checkpoint JSONB,
    external_run_id TEXT,  -- Oz run ID or other backend-specific run identifier
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Pending nudge requests
CREATE TABLE nudge_queue (
    id UUID PRIMARY KEY,
    issue_id TEXT NOT NULL,
    source_execution_id UUID REFERENCES executions(id),
    priority INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

-- Budget tracking
CREATE TABLE budget_usage (
    id UUID PRIMARY KEY,
    execution_id UUID REFERENCES executions(id),
    tokens_used INT,
    duration_seconds INT,
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);

-- Issue state tracking
CREATE TABLE issue_state (
    issue_number INT NOT NULL,
    repo TEXT NOT NULL,
    classification TEXT,
    parent_issue INT,
    sub_issues INT[],
    last_checked_at TIMESTAMPTZ,
    retry_count INT NOT NULL DEFAULT 0,
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (issue_number, repo)
);

-- Cron state (timestamps, cursors, etc.)
CREATE TABLE cron_state (
    key TEXT PRIMARY KEY,
    value JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_executions_status ON executions(status);
CREATE INDEX idx_executions_issue ON executions(issue_id);
CREATE UNIQUE INDEX idx_executions_active_issue ON executions(issue_id) WHERE status IN ('pending', 'running');
CREATE INDEX idx_executions_created_at ON executions(created_at);
CREATE INDEX idx_nudge_queue_pending ON nudge_queue(processed_at) WHERE processed_at IS NULL;
CREATE INDEX idx_budget_usage_recorded_at ON budget_usage(recorded_at);
CREATE INDEX idx_executions_external_run_id ON executions(external_run_id);
CREATE INDEX idx_issue_state_classification ON issue_state(classification);
CREATE INDEX idx_issue_state_repo ON issue_state(repo);
