-- Agent Grid Database Schema

-- Tracks all agent executions
CREATE TABLE executions (
    id UUID PRIMARY KEY,
    issue_id TEXT NOT NULL,
    repo_url TEXT NOT NULL,
    status TEXT NOT NULL,  -- pending, running, completed, failed
    prompt TEXT,
    result TEXT,
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

CREATE INDEX idx_executions_status ON executions(status);
CREATE INDEX idx_executions_issue ON executions(issue_id);
CREATE INDEX idx_nudge_queue_pending ON nudge_queue(processed_at) WHERE processed_at IS NULL;
