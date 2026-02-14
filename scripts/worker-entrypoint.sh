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
import asyncio, json, os, sys, time
from claude_code_sdk import query
from claude_code_sdk.types import (
    AssistantMessage,
    ClaudeCodeOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
)

def log(msg):
    \"\"\"Print with flush so it appears in Fly logs immediately.\"\"\"
    print(msg, flush=True)

async def main():
    prompt = os.environ['PROMPT']
    options = ClaudeCodeOptions(
        cwd='/workspace/repo',
        permission_mode='bypassPermissions',
    )

    result = ''
    turn = 0
    start = time.time()

    async for message in query(prompt=prompt, options=options):
        elapsed = int(time.time() - start)

        if isinstance(message, AssistantMessage):
            turn += 1
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    # Log tool usage (most useful for observability)
                    tool_input_summary = str(block.input)[:200]
                    log(f'[turn {turn}] [{elapsed}s] Tool: {block.name} | {tool_input_summary}')
                elif isinstance(block, TextBlock):
                    # Log first 200 chars of assistant text
                    text_preview = block.text[:200].replace('\n', ' ')
                    if text_preview.strip():
                        log(f'[turn {turn}] [{elapsed}s] Agent: {text_preview}')
                elif isinstance(block, ToolResultBlock):
                    # Log tool result status (not full output — too noisy)
                    log(f'[turn {turn}] [{elapsed}s] Tool result (is_error={block.is_error})')

        elif isinstance(message, SystemMessage):
            log(f'[{elapsed}s] System: {message.subtype}')

        elif isinstance(message, ResultMessage):
            if message.result:
                result = message.result
            cost = f'\${message.total_cost_usd:.4f}' if message.total_cost_usd else 'N/A'
            log(f'[{elapsed}s] Done: turns={message.num_turns}, cost={cost}, error={message.is_error}')

    # Print result to stdout so it appears in Fly logs
    log('=== AGENT RESULT ===')
    log(result[:10000])
    log('=== END RESULT ===')

    # Detect branch and PR number from git/gh state
    import subprocess
    branch = None
    pr_number = None

    try:
        branch = subprocess.check_output(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd='/workspace/repo', text=True
        ).strip()
        if branch in ('main', 'master', 'HEAD'):
            branch = None
    except Exception:
        pass

    if branch:
        try:
            pr_list = subprocess.check_output(
                ['gh', 'pr', 'list', '--head', branch, '--json', 'number', '--limit', '1'],
                cwd='/workspace/repo', text=True
            ).strip()
            prs = json.loads(pr_list) if pr_list else []
            if prs:
                pr_number = prs[0]['number']
        except Exception:
            pass

    # Report back to orchestrator
    import httpx
    orchestrator = os.environ.get('ORCHESTRATOR_URL', '')
    if not orchestrator:
        print('Warning: ORCHESTRATOR_URL not set, skipping callback')
        return
    callback_url = orchestrator + '/api/agent-status'
    payload = {
        'execution_id': os.environ['EXECUTION_ID'],
        'status': 'completed',
        'result': result[:10000],
        'branch': branch,
        'pr_number': pr_number,
        'checkpoint': {
            'mode': os.environ.get('MODE', 'implement'),
            'context_summary': result[:500],
        },
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
    orchestrator = os.environ.get('ORCHESTRATOR_URL', '')
    if not orchestrator:
        print('Warning: ORCHESTRATOR_URL not set, skipping callback')
        return
    callback_url = orchestrator + '/api/agent-status'
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
