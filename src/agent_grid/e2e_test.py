"""End-to-end test: uses the real engine pipeline, runs agent locally, shows diff.

Uses our actual Scanner, Classifier, prompt_builder, and dry-run wrappers.
Reads from your real GitHub repo. Runs a real Claude Code agent locally.
Does NOT write anything to GitHub — no push, no PRs, no comments, no labels.

Usage:
    AGENT_GRID_TARGET_REPO=myorg/myrepo \
    AGENT_GRID_GITHUB_TOKEN=ghp_... \
    AGENT_GRID_ANTHROPIC_API_KEY=sk-ant-... \
    python -m agent_grid.e2e_test

Optional:
    AGENT_GRID_E2E_ISSUE=42   — skip scanning/classification, test a specific issue
"""

import asyncio
import json
import logging
import os
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
    from .dry_run import install_dry_run_wrappers

    # ── Validate ─────────────────────────────────────────────────────────
    errors = []
    if not settings.target_repo:
        errors.append("AGENT_GRID_TARGET_REPO=owner/repo")
    if not settings.github_token:
        errors.append("AGENT_GRID_GITHUB_TOKEN=ghp_...")
    if not settings.anthropic_api_key:
        errors.append("AGENT_GRID_ANTHROPIC_API_KEY=sk-ant-...")
    if errors:
        print("ERROR: Missing environment variables:")
        for e in errors:
            print(f"  export {e}")
        sys.exit(1)

    repo = settings.target_repo
    print(f"\n{'='*60}")
    print(f"  End-to-end test for: {repo}")
    print(f"{'='*60}\n")

    # ── Install dry-run wrappers (real reads, logged writes) ─────────────
    install_dry_run_wrappers()

    # ── Phase 1 & 2: Use our real Scanner and Classifier ─────────────────
    from .coordinator.scanner import get_scanner
    from .coordinator.classifier import get_classifier
    from .coordinator.prompt_builder import build_prompt
    from .issue_tracker import get_issue_tracker

    tracker = get_issue_tracker()
    specific_issue = os.environ.get("AGENT_GRID_E2E_ISSUE")

    if specific_issue:
        print(f"Using specified issue #{specific_issue}\n")
        issue = await tracker.get_issue(repo, specific_issue)
        classification_info = "(user-specified, skipping classification)"
    else:
        # Phase 1: Scan using our real Scanner
        print("Phase 1: Scanning (using Scanner from coordinator.scanner)...")
        scanner = get_scanner()
        candidates = await scanner.scan(repo)

        if not candidates:
            print("No candidate issues found.")
            await tracker.close()
            return

        print(f"Found {len(candidates)} candidate issues:")
        for i, iss in enumerate(candidates[:15]):
            labels_str = f" [{', '.join(iss.labels)}]" if iss.labels else ""
            print(f"  {i+1}. #{iss.number}: {iss.title}{labels_str}")

        # Phase 2: Classify using our real Classifier
        print(f"\nPhase 2: Classifying (using Classifier from coordinator.classifier)...")
        classifier = get_classifier()
        classified = []
        for iss in candidates:
            classification = await classifier.classify(iss)
            classified.append((iss, classification))
            symbol = {"SIMPLE": "+", "COMPLEX": "*", "BLOCKED": "!", "SKIP": "-"}.get(classification.category, "?")
            print(f"  [{symbol}] #{iss.number}: {classification.category} "
                  f"(complexity={classification.estimated_complexity}) "
                  f"-- {classification.reason}")

        # Pick the easiest SIMPLE issue
        simple_issues = [
            (iss, c) for iss, c in classified
            if c.category == "SIMPLE"
        ]
        if not simple_issues:
            print("\nNo SIMPLE issues found. Picking the least complex one.")
            classified.sort(key=lambda x: x[1].estimated_complexity)
            issue, classification = classified[0]
        else:
            simple_issues.sort(key=lambda x: x[1].estimated_complexity)
            issue, classification = simple_issues[0]

        classification_info = (f"{classification.category} "
                               f"(complexity={classification.estimated_complexity}): "
                               f"{classification.reason}")

    print(f"\n{'~'*60}")
    print(f"  Selected: #{issue.number} -- {issue.title}")
    print(f"  Classification: {classification_info}")
    print(f"{'~'*60}")

    if issue.body:
        preview = issue.body[:400] + ("..." if len(issue.body) > 400 else "")
        print(f"\n  Body:\n  {preview}\n")

    # ── Phase 3: Build prompt using our real prompt_builder ───────────────
    print("Phase 3: Building prompt (using prompt_builder.build_prompt)...")
    raw_prompt = build_prompt(issue, repo, mode="implement")

    # Append local-test override: don't push, don't create PR, don't comment
    local_override = """

## LOCAL TEST MODE OVERRIDE
You are running in a local test. Additional rules:
- The branch is already checked out for you. Do NOT run git checkout -b.
- Implement the changes and commit them locally.
- Do NOT push to any remote.
- Do NOT create a pull request.
- Do NOT run any `gh` commands.
- Do NOT post comments to GitHub issues.
- Just implement, test, and commit locally.
"""
    prompt = raw_prompt + local_override

    print(f"  Prompt length: {len(prompt)} chars")
    print(f"  Mode: implement")

    # ── Phase 4: Clone repo and run agent ────────────────────────────────
    workdir = tempfile.mkdtemp(prefix="agent-grid-e2e-")
    repo_dir = os.path.join(workdir, "repo")

    print(f"\nPhase 4: Cloning {repo}...")
    clone_url = f"https://x-access-token:{settings.github_token}@github.com/{repo}.git"
    result = subprocess.run(
        ["git", "clone", "--depth=50", clone_url, repo_dir],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: git clone failed:\n{result.stderr}")
        await tracker.close()
        return

    # Create the branch the prompt_builder expects
    branch_name = f"agent/{issue.number}"
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=repo_dir, capture_output=True,
    )
    print(f"  Cloned to: {repo_dir}")
    print(f"  Branch: {branch_name}")

    # Detect default branch name for diff later
    default_branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    default_branch = default_branch_result.stdout.strip().replace("origin/", "") or "main"

    # ── Phase 5: Run Claude Code SDK ─────────────────────────────────────
    print(f"\nPhase 5: Running Claude Code agent...")
    print(f"{'~'*60}")
    print("  Agent output:")
    print(f"{'~'*60}\n")

    agent_result = ""
    try:
        from claude_code_sdk import query, ClaudeCodeOptions
        from claude_code_sdk.types import AssistantMessage, ResultMessage, ToolUseMessage, ToolResultMessage

        options = ClaudeCodeOptions(
            cwd=repo_dir,
            permission_mode="bypassPermissions",
        )

        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                text = message.content[:200] if isinstance(message.content, str) else str(message.content)[:200]
                print(f"  [assistant] {text}")
            elif isinstance(message, ResultMessage):
                agent_result = message.result or ""
                print(f"\n  [result] {agent_result[:500]}")
            elif isinstance(message, ToolUseMessage):
                print(f"  [tool] {message.tool_name}: {str(message.tool_input)[:150]}")
            elif isinstance(message, ToolResultMessage):
                output = str(message.content)[:200] if message.content else ""
                print(f"  [tool_result] {output}")

    except ImportError:
        print("  claude-code-sdk not installed.")
        print("  Install with: pip install claude-code-sdk")
        print(f"\n  Prompt that WOULD be sent ({len(prompt)} chars):\n")
        print(f"  {prompt[:800]}")
        agent_result = "(sdk not installed)"

    except Exception as e:
        print(f"\n  Agent error: {e}")
        agent_result = f"Error: {e}"

    # ── Phase 6: Show results ────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("  Results")
    print(f"{'='*60}\n")

    # Show git log
    log_result = subprocess.run(
        ["git", "log", "--oneline", f"{default_branch}..{branch_name}"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    commits = log_result.stdout.strip()
    if commits:
        print("Commits:")
        for line in commits.split("\n"):
            print(f"  {line}")
    else:
        print("No commits were made by the agent.")

    # Show diff stat
    diff_stat = subprocess.run(
        ["git", "diff", "--stat", f"{default_branch}..{branch_name}"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if diff_stat.stdout.strip():
        print(f"\nFiles changed:\n{diff_stat.stdout}")

    # Full diff
    diff_result = subprocess.run(
        ["git", "diff", f"{default_branch}..{branch_name}"],
        cwd=repo_dir, capture_output=True, text=True,
    )

    # Save artifacts
    diff_file = Path(workdir) / "agent_diff.patch"
    diff_file.write_text(diff_result.stdout)

    prompt_file = Path(workdir) / "prompt.txt"
    prompt_file.write_text(prompt)

    summary_file = Path(workdir) / "e2e_summary.json"
    summary_file.write_text(json.dumps({
        "repo": repo,
        "issue_number": issue.number,
        "issue_title": issue.title,
        "classification": classification_info,
        "branch": branch_name,
        "workdir": repo_dir,
        "commits": commits,
        "diff_stat": diff_stat.stdout.strip(),
        "agent_result": agent_result[:2000],
    }, indent=2))

    print(f"Artifacts saved:")
    print(f"  Prompt:  {prompt_file}")
    print(f"  Diff:    {diff_file}")
    print(f"  Summary: {summary_file}")
    print(f"  Repo:    {repo_dir}")
    print(f"\nInspect:")
    print(f"  cd {repo_dir}")
    print(f"  git log --oneline")
    print(f"  git diff {default_branch}..{branch_name}")
    print(f"\nClean up:")
    print(f"  rm -rf {workdir}")

    await tracker.close()
    print(f"\nDone. Nothing was written to GitHub.")


if __name__ == "__main__":
    asyncio.run(main())
