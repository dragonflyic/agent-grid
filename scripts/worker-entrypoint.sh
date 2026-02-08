#!/bin/bash
# worker-entrypoint.sh — Runs on each Fly Machine
set -e

echo "=== Agent Grid Worker ==="
echo "Execution: $EXECUTION_ID"
echo "Repo: $REPO_URL"
echo "Issue: $ISSUE_NUMBER"
echo "Mode: $MODE"

# Configure git
git config --global user.name "Agent Grid"
git config --global user.email "agent-grid@noreply.github.com"

# Configure gh CLI auth
echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null || true

# Configure git credentials for private repos
git config --global url."https://${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"

# Clone repo
echo "Cloning $REPO_URL ..."
git clone "$REPO_URL" /workspace/repo
cd /workspace/repo
echo "Clone complete. Branch: $(git branch --show-current)"

# Run Claude Code SDK via Python
python3 -c "
import asyncio, json, os, sys
from claude_code_sdk import query
from claude_code_sdk.types import ClaudeCodeOptions, ResultMessage

async def main():
    prompt = os.environ['PROMPT']
    options = ClaudeCodeOptions(
        cwd='/workspace/repo',
        permission_mode='bypassPermissions',
    )

    result = ''
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage) and message.result:
            result = message.result

    # Print result to stdout so it appears in Fly logs
    print('=== AGENT RESULT ===')
    print(result[:10000])
    print('=== END RESULT ===')

    # Report back to orchestrator
    import httpx
    callback_url = os.environ.get('ORCHESTRATOR_URL', '') + '/api/agent-status'
    payload = {
        'execution_id': os.environ['EXECUTION_ID'],
        'status': 'completed',
        'result': result[:10000],
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(callback_url, json=payload)
            print(f'Reported status: {resp.status_code}')
    except Exception as e:
        print(f'Warning: Failed to report status: {e}')
        # Don't exit(1) — the agent work is done, callback failure is non-fatal

asyncio.run(main())
" 2>&1 || {
    echo "=== Agent failed, capturing git state ==="
    cd /workspace/repo 2>/dev/null && {
        git log --oneline -10 2>/dev/null || true
        git diff --stat 2>/dev/null || true
    }
    # Report failure
    python3 -c "
import asyncio, os
import httpx

async def report_failure():
    callback_url = os.environ.get('ORCHESTRATOR_URL', '') + '/api/agent-status'
    payload = {
        'execution_id': os.environ['EXECUTION_ID'],
        'status': 'failed',
        'result': 'Agent process exited with error',
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(callback_url, json=payload)
    except Exception as e:
        print(f'Warning: Failed to report failure: {e}')

asyncio.run(report_failure())
" || true
}

# Print final git state so it always shows in logs
cd /workspace/repo 2>/dev/null && {
    echo "=== GIT STATE ==="
    git log --oneline -10 2>/dev/null || true
    echo "---"
    git diff --stat HEAD~1..HEAD 2>/dev/null || true
    echo "=== END GIT STATE ==="
} || true

echo "=== Worker complete ==="
