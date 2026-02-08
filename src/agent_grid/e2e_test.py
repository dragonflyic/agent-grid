"""End-to-end test: scan → classify → pick easiest → run agent locally → show diff.

Reads from your real GitHub repo. Runs a real Claude Code agent.
Does NOT write anything to GitHub — no push, no PRs, no comments, no labels.

Usage:
    AGENT_GRID_TARGET_REPO=myorg/myrepo \
    AGENT_GRID_GITHUB_TOKEN=ghp_... \
    AGENT_GRID_ANTHROPIC_API_KEY=sk-ant-... \
    python -m agent_grid.e2e_test

Optional:
    AGENT_GRID_E2E_ISSUE=42   — skip classification, test a specific issue number
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agent_grid.e2e_test")


async def main():
    from .config import settings

    # ── Validate settings ────────────────────────────────────────────────
    if not settings.target_repo:
        print("ERROR: Set AGENT_GRID_TARGET_REPO=owner/repo")
        sys.exit(1)
    if not settings.github_token:
        print("ERROR: Set AGENT_GRID_GITHUB_TOKEN=ghp_...")
        sys.exit(1)
    if not settings.anthropic_api_key:
        print("ERROR: Set AGENT_GRID_ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    repo = settings.target_repo
    print(f"\n{'='*60}")
    print(f"  End-to-end test for: {repo}")
    print(f"{'='*60}\n")

    # ── Phase 1: Find the target issue ───────────────────────────────────
    from .issue_tracker.github_client import GitHubClient
    github = GitHubClient(token=settings.github_token)

    specific_issue = os.environ.get("AGENT_GRID_E2E_ISSUE")

    if specific_issue:
        # User specified a specific issue
        print(f"Using specified issue #{specific_issue}")
        issue = await github.get_issue(repo, specific_issue)
        classification_info = "(user-specified, skipping classification)"
    else:
        # Scan and classify
        from .issue_tracker.public_api import IssueStatus
        from .coordinator.scanner import HANDLED_LABELS

        print("Phase 1: Scanning open issues...")
        all_issues = await github.list_issues(repo, status=IssueStatus.OPEN)

        # Filter out already-handled issues and PRs
        candidates = [
            i for i in all_issues
            if not any(l in HANDLED_LABELS for l in i.labels)
        ]

        if not candidates:
            print("No candidate issues found. All issues either have ai-* labels or the repo has no open issues.")
            await github.close()
            return

        print(f"Found {len(candidates)} candidate issues:")
        for i, issue in enumerate(candidates[:10]):
            labels_str = f" [{', '.join(issue.labels)}]" if issue.labels else ""
            print(f"  {i+1}. #{issue.number}: {issue.title}{labels_str}")

        # Phase 2: Classify
        print(f"\nPhase 2: Classifying {len(candidates)} issues...")
        from .coordinator.classifier import Classifier

        classifier = Classifier()
        classified = []
        for issue in candidates:
            classification = await classifier.classify(issue)
            classified.append((issue, classification))
            emoji = {"SIMPLE": "✅", "COMPLEX": "🔧", "BLOCKED": "🚫", "SKIP": "⏭️"}.get(classification.category, "?")
            print(f"  {emoji} #{issue.number}: {classification.category} (complexity={classification.estimated_complexity}) — {classification.reason}")

        # Pick the easiest SIMPLE issue
        simple_issues = [
            (issue, c) for issue, c in classified
            if c.category == "SIMPLE"
        ]

        if not simple_issues:
            print("\nNo SIMPLE issues found. Picking the least complex issue instead.")
            classified.sort(key=lambda x: x[1].estimated_complexity)
            issue, classification = classified[0]
        else:
            simple_issues.sort(key=lambda x: x[1].estimated_complexity)
            issue, classification = simple_issues[0]

        classification_info = f"{classification.category} (complexity={classification.estimated_complexity}): {classification.reason}"

    print(f"\n{'─'*60}")
    print(f"  Selected: #{issue.number} — {issue.title}")
    print(f"  Classification: {classification_info}")
    print(f"{'─'*60}")

    # Show issue body preview
    if issue.body:
        body_preview = issue.body[:500]
        if len(issue.body) > 500:
            body_preview += "..."
        print(f"\n  Body:\n  {body_preview}\n")

    # ── Phase 3: Clone repo and run agent ────────────────────────────────
    workdir = tempfile.mkdtemp(prefix="agent-grid-e2e-")
    repo_dir = os.path.join(workdir, "repo")

    print(f"Phase 3: Cloning {repo} to {repo_dir}...")
    clone_url = f"https://x-access-token:{settings.github_token}@github.com/{repo}.git"
    result = subprocess.run(
        ["git", "clone", "--depth=50", clone_url, repo_dir],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: git clone failed:\n{result.stderr}")
        await github.close()
        return

    # Create a local-only branch (never pushed)
    branch_name = f"agent/{issue.number}-e2e-test"
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=repo_dir, capture_output=True,
    )

    # Build the agent prompt — modified to NOT push or create PRs
    prompt = f"""You are a senior software engineer working on a GitHub issue.

## Repository
- Repo: {repo}

## Your Task
Issue #{issue.number}: {issue.title}

{issue.body or '(no description)'}

## Rules
1. Work ONLY on what the issue asks for. Do not refactor unrelated code.
2. Write tests for your changes if appropriate.
3. Run existing tests and make sure they pass.
4. Follow the existing code style in the repo.
5. Make atomic, well-described commits.

## IMPORTANT: LOCAL TEST MODE
You are running in a LOCAL TEST. Do the following:
- Work on branch: {branch_name} (already checked out)
- Implement the changes and commit them
- Do NOT push to any remote
- Do NOT create a PR
- Do NOT run `gh` commands
- Do NOT post comments to GitHub
- Just implement, test, and commit locally
"""

    print(f"\nPhase 4: Running Claude Code agent...")
    print(f"  Branch: {branch_name}")
    print(f"  Workdir: {repo_dir}")
    print(f"  Prompt length: {len(prompt)} chars")
    print(f"\n{'─'*60}")
    print("  Agent output:")
    print(f"{'─'*60}\n")

    # Run Claude Code SDK
    try:
        from claude_code_sdk import query, ClaudeCodeOptions
        from claude_code_sdk.types import AssistantMessage, ResultMessage, ToolUseMessage, ToolResultMessage

        options = ClaudeCodeOptions(
            cwd=repo_dir,
            permission_mode="bypassPermissions",
        )

        agent_result = ""
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                print(f"  [assistant] {message.content[:200]}")
            elif isinstance(message, ResultMessage):
                agent_result = message.result or ""
                print(f"\n  [result] {agent_result[:500]}")
            elif isinstance(message, ToolUseMessage):
                print(f"  [tool] {message.tool_name}: {str(message.tool_input)[:150]}")
            elif isinstance(message, ToolResultMessage):
                output = str(message.content)[:200] if message.content else ""
                print(f"  [tool_result] {output}")

    except ImportError:
        print("\n  claude-code-sdk not installed. Simulating agent run...")
        print("  Install with: pip install claude-code-sdk")
        print("\n  Falling back to showing the prompt that WOULD be sent:\n")
        print(f"  {prompt[:1000]}")
        agent_result = "(sdk not installed — simulation only)"

    except Exception as e:
        print(f"\n  Agent error: {e}")
        agent_result = f"Error: {e}"

    # ── Phase 5: Show results ────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  Results")
    print(f"{'='*60}\n")

    # Show git log
    log_result = subprocess.run(
        ["git", "log", "--oneline", f"main..{branch_name}"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    commits = log_result.stdout.strip()
    if commits:
        print("Commits made:")
        for line in commits.split("\n"):
            print(f"  {line}")
    else:
        print("No commits were made by the agent.")

    # Show diff stat
    diff_stat = subprocess.run(
        ["git", "diff", "--stat", f"main..{branch_name}"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if diff_stat.stdout.strip():
        print(f"\nFiles changed:\n{diff_stat.stdout}")

    # Show full diff
    diff_result = subprocess.run(
        ["git", "diff", f"main..{branch_name}"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    diff_text = diff_result.stdout

    # Save diff to file for review
    diff_file = Path(workdir) / "agent_diff.patch"
    diff_file.write_text(diff_text)

    # Save full log
    summary_file = Path(workdir) / "e2e_summary.json"
    summary = {
        "repo": repo,
        "issue_number": issue.number,
        "issue_title": issue.title,
        "classification": classification_info,
        "branch": branch_name,
        "workdir": repo_dir,
        "commits": commits,
        "diff_stat": diff_stat.stdout.strip(),
        "agent_result": agent_result[:2000],
        "diff_file": str(diff_file),
    }
    summary_file.write_text(json.dumps(summary, indent=2))

    print(f"\nFiles saved:")
    print(f"  Diff:    {diff_file}")
    print(f"  Summary: {summary_file}")
    print(f"  Repo:    {repo_dir}")
    print(f"\nTo inspect the agent's work:")
    print(f"  cd {repo_dir}")
    print(f"  git log --oneline")
    print(f"  git diff main..{branch_name}")
    print(f"\nTo clean up:")
    print(f"  rm -rf {workdir}")

    await github.close()
    print(f"\nDone! Nothing was written to GitHub.")


if __name__ == "__main__":
    asyncio.run(main())
