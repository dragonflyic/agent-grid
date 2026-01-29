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

-- Webhook deduplication queue
CREATE TABLE webhook_events (
    id UUID PRIMARY KEY,
    delivery_id TEXT NOT NULL UNIQUE,  -- X-GitHub-Delivery header (idempotency key)
    event_type TEXT NOT NULL,          -- issues, issue_comment, etc.
    action TEXT,                       -- opened, labeled, created, etc.
    repo TEXT,
    issue_id TEXT,
    payload TEXT,                      -- JSON payload for processing
    processed BOOLEAN NOT NULL DEFAULT FALSE,
    coalesced_into UUID,               -- Reference to primary event if deduplicated
    received_at TIMESTAMPTZ DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX idx_webhook_events_delivery_id ON webhook_events(delivery_id);
CREATE INDEX idx_webhook_events_unprocessed ON webhook_events(processed, received_at) WHERE processed = FALSE;
CREATE INDEX idx_webhook_events_issue ON webhook_events(repo, issue_id, received_at);
