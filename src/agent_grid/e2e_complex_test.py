"""End-to-end test for COMPLEX issue flow: classify -> plan -> decompose.

Picks a complex issue (or user-specified one), runs it through:
1. Classifier — confirms it's COMPLEX
2. Planner — generates implementation plan via Claude
3. Sub-issue creation — creates sub-issues (dry-run: logged to file)
4. Prompt generation — shows what each sub-issue's agent prompt would look like

All write operations are intercepted by dry-run mode and logged to JSONL.

Usage:
    AGENT_GRID_TARGET_REPO=myorg/myrepo \\
    AGENT_GRID_GITHUB_TOKEN=ghp_... \\
    AGENT_GRID_ANTHROPIC_API_KEY=sk-ant-... \\
    python -m agent_grid.e2e_complex_test

Optional:
    AGENT_GRID_E2E_ISSUE=42   -- skip scanning, use a specific issue
"""

import asyncio
import json
import logging
import os
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agent_grid.e2e_complex")


async def main():
    from .config import settings
    from .dry_run import install_dry_run_wrappers

    # -- Validate ----------------------------------------------------------
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
    print(f"  Complex Issue E2E Test: {repo}")
    print(f"{'='*60}\n")

    # -- Install dry-run wrappers ------------------------------------------
    install_dry_run_wrappers()

    from .coordinator.scanner import get_scanner
    from .coordinator.classifier import get_classifier, Classification
    from .coordinator.planner import get_planner
    from .coordinator.prompt_builder import build_prompt
    from .issue_tracker import get_issue_tracker

    tracker = get_issue_tracker()
    specific_issue = os.environ.get("AGENT_GRID_E2E_ISSUE")

    # -- Phase 1: Find a complex issue ------------------------------------
    if specific_issue:
        print(f"Using specified issue #{specific_issue}\n")
        issue = await tracker.get_issue(repo, specific_issue)
    else:
        print("Phase 1: Scanning for issues...")
        scanner = get_scanner()
        candidates = await scanner.scan(repo)

        if not candidates:
            print("No candidate issues found.")
            await tracker.close()
            return

        print(f"Found {len(candidates)} candidates. Classifying to find COMPLEX ones...\n")

        print("Phase 2: Classifying all issues...")
        classifier = get_classifier()
        classified = []
        for iss in candidates:
            c = await classifier.classify(iss)
            classified.append((iss, c))
            sym = {"SIMPLE": "+", "COMPLEX": "*", "BLOCKED": "!", "SKIP": "-"}.get(c.category, "?")
            print(f"  [{sym}] #{iss.number}: {c.category} "
                  f"(complexity={c.estimated_complexity}) -- {c.reason}")

        # Pick the most complex COMPLEX issue
        complex_issues = [(iss, c) for iss, c in classified if c.category == "COMPLEX"]
        if not complex_issues:
            print("\nNo COMPLEX issues found. Picking highest complexity issue.")
            classified.sort(key=lambda x: x[1].estimated_complexity, reverse=True)
            issue, cl = classified[0]
        else:
            complex_issues.sort(key=lambda x: x[1].estimated_complexity, reverse=True)
            issue, cl = complex_issues[0]

        print(f"\n  Selected most complex: #{issue.number} "
              f"(complexity={cl.estimated_complexity})")

    # -- Show issue details ------------------------------------------------
    print(f"\n{'~'*60}")
    print(f"  Issue #{issue.number}: {issue.title}")
    print(f"{'~'*60}")
    if issue.body:
        preview = issue.body[:600] + ("..." if len(issue.body) > 600 else "")
        print(f"\n{preview}\n")

    # -- Phase 3: Classify (if user-specified, we classify it too) ---------
    if specific_issue:
        print("Phase 2: Classifying issue...")
        classifier = get_classifier()
        classification = await classifier.classify(issue)
        print(f"  Category: {classification.category}")
        print(f"  Complexity: {classification.estimated_complexity}")
        print(f"  Reason: {classification.reason}")
        if classification.category != "COMPLEX":
            print(f"\n  Note: Issue classified as {classification.category}, "
                  f"not COMPLEX. Running planner anyway for testing.\n")

    # -- Phase 4: Run planner to decompose --------------------------------
    print(f"\n{'~'*60}")
    print(f"  Phase 3: Running Planner (decomposing into sub-tasks)...")
    print(f"{'~'*60}\n")

    planner = get_planner()
    created_issues = await planner.decompose(
        repo=repo,
        issue_number=issue.number,
        title=issue.title,
        body=issue.body or "",
    )

    # -- Phase 5: Show what prompts would be generated --------------------
    if created_issues:
        print(f"\n{'~'*60}")
        print(f"  Phase 4: Generated prompts for each sub-task")
        print(f"{'~'*60}\n")

        for ci in created_issues:
            # Build a fake IssueInfo for the sub-issue to generate its prompt
            from .issue_tracker.public_api import IssueInfo, IssueStatus
            sub_issue = IssueInfo(
                id=str(ci["number"]),
                number=ci["number"],
                title=ci["title"],
                body=f"Sub-task of #{issue.number}: {ci['title']}",
                status=IssueStatus.OPEN,
                labels=["sub-issue"],
                repo_url=f"https://github.com/{repo}",
                html_url=f"https://github.com/{repo}/issues/{ci['number']}",
            )
            prompt = build_prompt(sub_issue, repo, mode="implement")
            print(f"  Sub-issue #{ci['number']}: {ci['title']}")
            print(f"    Prompt length: {len(prompt)} chars")

    # -- Phase 6: Show dry-run log -----------------------------------------
    print(f"\n{'='*60}")
    print(f"  Results")
    print(f"{'='*60}\n")

    print(f"  Parent issue: #{issue.number} -- {issue.title}")
    print(f"  Sub-tasks created: {len(created_issues)}")
    for ci in created_issues:
        print(f"    - #{ci['number']}: {ci['title']}")

    # Read and display dry-run log
    from pathlib import Path
    dry_run_path = Path(settings.dry_run_output_file).resolve()
    print(f"\n  Dry-run log: {dry_run_path}")

    if dry_run_path.exists():
        print(f"\n{'~'*60}")
        print(f"  All intercepted write operations:")
        print(f"{'~'*60}\n")
        for line in dry_run_path.read_text().strip().split("\n"):
            if line:
                entry = json.loads(line)
                action = entry.get("action", "?")
                # Format nicely based on action type
                if action == "create_subissue":
                    print(f"  [CREATE] Sub-issue #{entry.get('fake_number')}: {entry.get('title')}")
                    print(f"           Parent: #{entry.get('parent_id')}")
                    print(f"           Labels: {entry.get('labels')}")
                    body = entry.get("body", "")
                    if body:
                        print(f"           Body preview: {body[:200]}...")
                    print()
                elif action == "add_comment":
                    print(f"  [COMMENT] Issue #{entry.get('issue_id')}")
                    body = entry.get("body", "")
                    print(f"            {body[:300]}...")
                    print()
                elif action == "label_transition":
                    print(f"  [LABEL] Issue #{entry.get('issue_id')} -> {entry.get('new_label')}")
                elif action == "add_label":
                    print(f"  [LABEL+] Issue #{entry.get('issue_id')} + {entry.get('label')}")
                elif action == "remove_label":
                    print(f"  [LABEL-] Issue #{entry.get('issue_id')} - {entry.get('label')}")
                elif action == "launch_agent":
                    print(f"  [AGENT] Launch for issue #{entry.get('issue_number')} "
                          f"(mode={entry.get('mode')})")
                    print(f"          Prompt: {entry.get('prompt_preview', '')[:200]}...")
                    print()
                else:
                    print(f"  [{action}] {json.dumps(entry, default=str)[:200]}")

    await tracker.close()
    print(f"\nDone. Nothing was written to GitHub.")
    print(f"All operations logged to: {dry_run_path}")


if __name__ == "__main__":
    asyncio.run(main())
