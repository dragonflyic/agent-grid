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

# Clone repo
git clone "$REPO_URL" /workspace/repo
cd /workspace/repo

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

    # Report back to orchestrator
    import httpx
    callback_url = os.environ.get('ORCHESTRATOR_URL', '') + '/api/agent-status'
    payload = {
        'execution_id': os.environ['EXECUTION_ID'],
        'status': 'completed',
        'result': result[:10000],  # Truncate large results
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(callback_url, json=payload)
            print(f'Reported status: {resp.status_code}')
    except Exception as e:
        print(f'Failed to report status: {e}')
        sys.exit(1)

asyncio.run(main())
" 2>&1 || {
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
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.post(callback_url, json=payload)

asyncio.run(report_failure())
"
}

echo "=== Worker complete ==="
