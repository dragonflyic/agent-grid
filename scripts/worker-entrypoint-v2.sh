#!/usr/bin/env bash
# Worker entrypoint for Fly Machines running Claude Code CLI.
#
# Environment variables (set by coordinator):
#   EXECUTION_ID          - UUID for this execution (also used as session ID)
#   REPO_URL              - Git repo URL to clone
#   ISSUE_NUMBER          - For logging
#   MODE                  - implement, fix_ci, address_review, rebase, retry
#   PROMPT_S3_KEY         - S3 key for the prompt text
#   RESUME_SESSION_ID     - Previous session UUID to resume (optional)
#   COORDINATOR_URL       - URL to POST callbacks and events
#   GITHUB_TOKEN          - GitHub token for gh CLI
#   ANTHROPIC_API_KEY     - Fallback API key (if subscription auth fails)
#   CLAUDE_CREDENTIALS_SECRET - AWS Secrets Manager key for subscription credentials
#   S3_SESSION_BUCKET     - S3 bucket for session persistence
#   MAX_TURNS             - Max agent turns (default 200)
#   MAX_BUDGET_USD        - Max budget per execution (default 5.0)
#   AWS_REGION            - AWS region (default us-west-2)

set -euo pipefail

AWS_REGION="${AWS_REGION:-us-west-2}"
MAX_TURNS="${MAX_TURNS:-200}"
MAX_BUDGET_USD="${MAX_BUDGET_USD:-5.0}"
WORKSPACE="/workspace/repo"

echo "=== Worker starting ==="
echo "EXECUTION_ID: $EXECUTION_ID"
echo "REPO_URL: $REPO_URL"
echo "ISSUE_NUMBER: ${ISSUE_NUMBER:-unknown}"
echo "MODE: ${MODE:-implement}"

# --- Step 1: Load subscription credentials ---
echo "Loading Claude credentials..."
if [ -n "${CLAUDE_CREDENTIALS_JSON:-}" ]; then
    # Credentials passed directly by coordinator (preferred — no AWS access needed)
    mkdir -p ~/.claude
    echo "$CLAUDE_CREDENTIALS_JSON" > ~/.claude/.credentials.json
    echo "Subscription credentials loaded (from coordinator)."
elif [ -n "${CLAUDE_CREDENTIALS_SECRET:-}" ]; then
    # Fallback: fetch from Secrets Manager (requires AWS access)
    CREDS=$(aws secretsmanager get-secret-value \
        --region "$AWS_REGION" \
        --secret-id "$CLAUDE_CREDENTIALS_SECRET" \
        --query SecretString --output text 2>/dev/null) || true
    if [ -n "${CREDS:-}" ]; then
        mkdir -p ~/.claude
        echo "$CREDS" > ~/.claude/.credentials.json
        echo "Subscription credentials loaded (from Secrets Manager)."
    else
        echo "Warning: Could not load subscription credentials. Will use ANTHROPIC_API_KEY if set."
    fi
else
    echo "No Claude credentials available. Using ANTHROPIC_API_KEY."
fi

# --- Step 2: Configure git and gh ---
echo "Configuring git..."
git config --global user.name "agent-grid[bot]"
git config --global user.email "3031599+agent-grid[bot]@users.noreply.github.com"

if [ -n "${GITHUB_TOKEN:-}" ]; then
    echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null || true
    git config --global url."https://x-access-token:${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"
fi

# --- Step 3: Clone repo ---
echo "Cloning $REPO_URL ..."
git clone "$REPO_URL" "$WORKSPACE"
cd "$WORKSPACE"
echo "On branch: $(git branch --show-current)"

# --- Step 4: Get prompt ---
PROMPT="${PROMPT_TEXT:-}"
if [ -z "$PROMPT" ] && [ -n "${PROMPT_S3_KEY:-}" ] && [ -n "${S3_SESSION_BUCKET:-}" ]; then
    echo "Downloading prompt from S3..."
    PROMPT=$(aws s3 cp "s3://${S3_SESSION_BUCKET}/${PROMPT_S3_KEY}" - --region "${AWS_REGION:-us-west-2}" 2>/dev/null) || true
fi
if [ -z "$PROMPT" ]; then
    echo "Error: No prompt available"
    exit 1
fi

# --- Step 5: Download session from S3 (if resuming) ---
SESSION_DIR="$HOME/.claude/projects/-workspace-repo"
mkdir -p "$SESSION_DIR"

if [ -n "${RESUME_SESSION_ID:-}" ] && [ -n "${S3_SESSION_BUCKET:-}" ]; then
    echo "Downloading session $RESUME_SESSION_ID for resume..."
    aws s3 cp "s3://${S3_SESSION_BUCKET}/sessions/${RESUME_SESSION_ID}/session.jsonl" \
        "${SESSION_DIR}/${RESUME_SESSION_ID}.jsonl" \
        --region "$AWS_REGION" 2>/dev/null || echo "Warning: Could not download session for resume."
fi

# --- Step 6: Run Claude Code CLI ---
echo "=== Running Claude Code CLI ==="
EXIT_CODE=0

# Build command
CLAUDE_CMD="claude -p"
if [ -n "${RESUME_SESSION_ID:-}" ]; then
    CLAUDE_CMD="claude --resume ${RESUME_SESSION_ID} -p"
fi

$CLAUDE_CMD "$PROMPT" \
    --session-id "$EXECUTION_ID" \
    --model "${CLAUDE_MODEL:-claude-opus-4-6}" \
    --output-format stream-json \
    --verbose \
    --dangerously-skip-permissions \
    --max-turns "$MAX_TURNS" \
    --max-budget-usd "$MAX_BUDGET_USD" \
    2>/workspace/stderr.log \
    | tee /workspace/events.jsonl \
    | python3 /scripts/stream-to-coordinator.py \
    || EXIT_CODE=$?

echo "Claude exited with code: $EXIT_CODE"

# Check if subscription auth failed — retry with API key
if [ $EXIT_CODE -ne 0 ] && [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    STDERR=$(cat /workspace/stderr.log 2>/dev/null || true)
    if echo "$STDERR" | grep -qi "rate limit\|credit\|unauthorized\|forbidden"; then
        echo "Subscription auth may have failed. Retrying with ANTHROPIC_API_KEY..."
        # Remove subscription credentials to force API key usage
        rm -f ~/.claude/.credentials.json
        export ANTHROPIC_API_KEY

        $CLAUDE_CMD "$PROMPT" \
            --session-id "$EXECUTION_ID" \
            --output-format stream-json \
            --verbose \
            --dangerously-skip-permissions \
            --max-turns "$MAX_TURNS" \
            --max-budget-usd "$MAX_BUDGET_USD" \
            2>>/workspace/stderr.log \
            | tee -a /workspace/events.jsonl \
            | python3 /scripts/stream-to-coordinator.py \
            || EXIT_CODE=$?
        echo "API key retry exited with code: $EXIT_CODE"
    fi
fi

# --- Step 7: Extract result from events ---
RESULT=$(python3 -c "
import json
result = ''
cost = 0
for line in open('/workspace/events.jsonl'):
    try:
        e = json.loads(line.strip())
        if e.get('type') == 'result':
            result = e.get('result', '')[:10000]
            cost = e.get('total_cost_usd', 0)
    except: pass
print(json.dumps({'result': result, 'cost_usd': cost}))
" 2>/dev/null || echo '{"result":"","cost_usd":0}')

RESULT_TEXT=$(echo "$RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin)['result'])")
COST_USD=$(echo "$RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin)['cost_usd'])")

# --- Step 8: Detect branch and PR ---
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
if [ "$BRANCH" = "main" ] || [ "$BRANCH" = "master" ] || [ "$BRANCH" = "HEAD" ]; then
    BRANCH=""
fi

PR_NUMBER=""
if [ -n "$BRANCH" ]; then
    REPO_SLUG=$(echo "$REPO_URL" | sed 's|https://github.com/||; s|\.git$||')
    PR_NUMBER=$(gh pr list --repo "$REPO_SLUG" --head "$BRANCH" --json number --jq '.[0].number' 2>/dev/null || echo "")
fi

echo "Branch: ${BRANCH:-none}"
echo "PR: ${PR_NUMBER:-none}"
echo "Cost: \$${COST_USD}"

# --- Step 9: Upload session to S3 (BEFORE callback — deploy resilience) ---
if [ -n "${S3_SESSION_BUCKET:-}" ]; then
    echo "Uploading session to S3..."
    SESSION_FILE="${SESSION_DIR}/${EXECUTION_ID}.jsonl"
    if [ -f "$SESSION_FILE" ]; then
        aws s3 cp "$SESSION_FILE" \
            "s3://${S3_SESSION_BUCKET}/sessions/${EXECUTION_ID}/session.jsonl" \
            --region "$AWS_REGION" 2>/dev/null || echo "Warning: Session upload failed."
    fi
    # Also upload full event log
    if [ -f /workspace/events.jsonl ]; then
        aws s3 cp /workspace/events.jsonl \
            "s3://${S3_SESSION_BUCKET}/sessions/${EXECUTION_ID}/events.jsonl" \
            --region "$AWS_REGION" 2>/dev/null || echo "Warning: Events upload failed."
    fi
    echo "Session uploaded."
fi

# --- Step 10: POST callback to coordinator (non-fatal) ---
STATUS="completed"
if [ $EXIT_CODE -ne 0 ]; then
    STATUS="failed"
    if [ -z "$RESULT_TEXT" ]; then
        RESULT_TEXT="Claude exited with code $EXIT_CODE. Stderr: $(head -c 2000 /workspace/stderr.log 2>/dev/null || echo 'none')"
    fi
fi

if [ -n "${COORDINATOR_URL:-}" ]; then
    echo "Posting callback to coordinator..."
    CALLBACK_PAYLOAD=$(python3 -c "
import json
print(json.dumps({
    'execution_id': '$EXECUTION_ID',
    'status': '$STATUS',
    'result': '''$(echo "$RESULT_TEXT" | head -c 10000 | sed "s/'/\\\\'/g")'''[:10000],
    'branch': '${BRANCH}' or None,
    'pr_number': int('${PR_NUMBER}') if '${PR_NUMBER}'.isdigit() else None,
    'cost_usd': float('${COST_USD}') if '${COST_USD}' else None,
    'session_id': '$EXECUTION_ID',
    'session_s3_key': 'sessions/$EXECUTION_ID/',
}))
" 2>/dev/null || echo '{}')

    curl -s -X POST \
        "${COORDINATOR_URL}/api/agent-status" \
        -H "Content-Type: application/json" \
        -d "$CALLBACK_PAYLOAD" \
        --max-time 30 \
        || echo "Warning: Callback failed (coordinator may be restarting)."
fi

echo "=== Worker done (status: $STATUS) ==="
