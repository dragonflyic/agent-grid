"""End-to-end test: scan -> classify -> pick easiest -> spawn Fly Machine -> show logs.

Uses the real engine pipeline:
- Scanner (Phase 1) to find unprocessed issues
- Classifier (Phase 2) to classify via Claude API
- prompt_builder to generate the agent prompt
- FlyMachinesClient to spawn a real ephemeral worker

Reads from your real GitHub repo. Spawns a real Fly Machine.
Does NOT write anything to GitHub -- prompt tells agent not to push/PR.

Usage:
    AGENT_GRID_TARGET_REPO=myorg/myrepo \\
    AGENT_GRID_GITHUB_TOKEN=ghp_... \\
    AGENT_GRID_ANTHROPIC_API_KEY=sk-ant-... \\
    AGENT_GRID_FLY_API_TOKEN=... \\
    AGENT_GRID_FLY_APP_NAME=agent-grid-workers \\
    AGENT_GRID_FLY_WORKER_IMAGE=registry.fly.io/agent-grid-workers:latest \\
    python -m agent_grid.e2e_test

Optional:
    AGENT_GRID_E2E_ISSUE=42   -- skip scanning/classification, test a specific issue
"""

import asyncio
import logging
import os
import subprocess
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agent_grid.e2e_test")

# Local-test override appended to the real prompt
LOCAL_TEST_OVERRIDE = """

## E2E TEST MODE -- CRITICAL OVERRIDES
You are running in a local end-to-end test. These rules OVERRIDE everything above:
- Do NOT push to any remote. Do NOT run `git push`.
- Do NOT create a pull request. Do NOT run `gh pr create`.
- Do NOT post comments to GitHub issues. Do NOT run `gh issue comment`.
- Do NOT run any `gh` commands at all.
- Just implement, test, and commit locally.
- Before you finish, run these commands and include their output in your final message:
  ```
  git log --oneline --all
  git diff --stat HEAD~1..HEAD
  ```
  This lets us see what you did from the logs.
"""


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
    if not settings.fly_api_token:
        errors.append("AGENT_GRID_FLY_API_TOKEN=...")
    if not settings.fly_app_name:
        errors.append("AGENT_GRID_FLY_APP_NAME=agent-grid-workers")
    if not settings.fly_worker_image:
        errors.append("AGENT_GRID_FLY_WORKER_IMAGE=registry.fly.io/...")
    if errors:
        print("ERROR: Missing environment variables:")
        for e in errors:
            print(f"  export {e}")
        sys.exit(1)

    repo = settings.target_repo
    print(f"\n{'='*60}")
    print(f"  E2E Test: {repo}")
    print(f"  Fly app:  {settings.fly_app_name}")
    print(f"  Image:    {settings.fly_worker_image}")
    print(f"{'='*60}\n")

    # -- Install dry-run wrappers (real reads, no writes to GitHub) --------
    install_dry_run_wrappers()

    # -- Phase 1 & 2: Scan and Classify using our real engine --------------
    from .coordinator.scanner import get_scanner
    from .coordinator.classifier import get_classifier
    from .coordinator.prompt_builder import build_prompt
    from .issue_tracker import get_issue_tracker

    tracker = get_issue_tracker()
    specific_issue = os.environ.get("AGENT_GRID_E2E_ISSUE")

    if specific_issue:
        print(f"Using specified issue #{specific_issue}\n")
        issue = await tracker.get_issue(repo, specific_issue)
        classification_info = "(user-specified)"
    else:
        print("Phase 1: Scanning (coordinator.scanner)...")
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

        print(f"\nPhase 2: Classifying (coordinator.classifier)...")
        classifier = get_classifier()
        classified = []
        for iss in candidates:
            c = await classifier.classify(iss)
            classified.append((iss, c))
            sym = {"SIMPLE": "+", "COMPLEX": "*", "BLOCKED": "!", "SKIP": "-"}.get(c.category, "?")
            print(f"  [{sym}] #{iss.number}: {c.category} "
                  f"(complexity={c.estimated_complexity}) -- {c.reason}")

        simple = [(iss, c) for iss, c in classified if c.category == "SIMPLE"]
        if not simple:
            print("\nNo SIMPLE issues. Picking least complex.")
            classified.sort(key=lambda x: x[1].estimated_complexity)
            issue, cl = classified[0]
        else:
            simple.sort(key=lambda x: x[1].estimated_complexity)
            issue, cl = simple[0]

        classification_info = f"{cl.category} (complexity={cl.estimated_complexity}): {cl.reason}"

    print(f"\n{'~'*60}")
    print(f"  Selected: #{issue.number} -- {issue.title}")
    print(f"  Classification: {classification_info}")
    print(f"{'~'*60}")
    if issue.body:
        preview = issue.body[:400] + ("..." if len(issue.body) > 400 else "")
        print(f"\n  {preview}\n")

    # -- Phase 3: Build prompt using our real prompt_builder ---------------
    print("Phase 3: Building prompt (coordinator.prompt_builder)...")
    prompt = build_prompt(issue, repo, mode="implement") + LOCAL_TEST_OVERRIDE
    print(f"  Prompt: {len(prompt)} chars")

    # -- Phase 4: Spawn Fly Machine using our real FlyMachinesClient -------
    print(f"\nPhase 4: Spawning Fly Machine (fly.machines)...")
    from .fly.machines import FlyMachinesClient

    fly = FlyMachinesClient()
    execution_id = f"e2e-test-{issue.number}-{int(time.time())}"

    try:
        machine = await fly.spawn_worker(
            execution_id=execution_id,
            repo_url=f"https://github.com/{repo}.git",
            issue_number=issue.number,
            prompt=prompt,
            mode="implement",
        )
    except Exception as e:
        print(f"\nERROR: Failed to spawn Fly Machine: {e}")
        await fly.close()
        await tracker.close()
        return

    machine_id = machine["id"]
    print(f"  Machine ID: {machine_id}")
    print(f"  Name: {machine.get('name', '?')}")
    print(f"  Region: {machine.get('region', '?')}")
    print(f"  State: {machine.get('state', '?')}")

    # -- Phase 5: Stream logs and poll status ------------------------------
    print(f"\n{'~'*60}")
    print(f"  Fly Machine is running. Polling status...")
    print(f"  To stream logs in another terminal:")
    print(f"    fly logs -a {settings.fly_app_name} -i {machine_id}")
    print(f"{'~'*60}\n")

    # Try to stream logs in background (optional — fly CLI may not be installed)
    log_proc = None
    try:
        log_proc = subprocess.Popen(
            ["fly", "logs", "-a", settings.fly_app_name, "-i", machine_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        print("  (fly CLI not installed — skipping log streaming, polling status only)")

    # Poll machine status
    final_state = "unknown"
    poll_count = 0
    max_polls = 180  # 30 min max (10s intervals)

    try:
        while poll_count < max_polls:
            await asyncio.sleep(10)
            poll_count += 1

            try:
                status = await fly.get_machine_status(machine_id)
                state = status.get("state", "unknown")
            except Exception:
                # Machine might be destroyed already
                state = "destroyed"

            if poll_count % 6 == 0 or state not in ("started", "created"):
                elapsed = poll_count * 10
                print(f"  [{elapsed}s] Machine state: {state}")

            # Print any available log output
            if log_proc and log_proc.stdout:
                while True:
                    line = log_proc.stdout.readline()
                    if not line:
                        break
                    print(f"  [log] {line.rstrip()}")

            if state in ("stopped", "destroyed", "failed"):
                final_state = state
                break

        if poll_count >= max_polls:
            print(f"\n  Timeout after {max_polls * 10}s. Destroying machine...")
            await fly.destroy_machine(machine_id)
            final_state = "timeout"

    except KeyboardInterrupt:
        print("\n  Interrupted. Destroying machine...")
        await fly.destroy_machine(machine_id)
        final_state = "interrupted"

    finally:
        if log_proc:
            log_proc.terminate()
            try:
                remaining, _ = log_proc.communicate(timeout=5)
                if remaining:
                    for line in remaining.strip().split("\n"):
                        if line:
                            print(f"  [log] {line}")
            except Exception:
                pass

    # -- Phase 6: Results --------------------------------------------------
    print(f"\n{'='*60}")
    print(f"  Results")
    print(f"{'='*60}\n")
    print(f"  Issue:    #{issue.number} -- {issue.title}")
    print(f"  Machine:  {machine_id}")
    print(f"  State:    {final_state}")
    print(f"  Duration: ~{poll_count * 10}s")

    if final_state == "stopped":
        print(f"\n  Machine completed successfully.")
        print(f"  The agent's work (commits, diffs) was logged above.")
        print(f"  Since this was a test, nothing was pushed to GitHub.")
    elif final_state == "destroyed":
        print(f"\n  Machine was destroyed (auto_destroy). Check logs above for results.")
    elif final_state == "failed":
        print(f"\n  Machine failed. Check logs above for errors.")

    print(f"\n  Full logs:")
    print(f"    fly logs -a {settings.fly_app_name} -i {machine_id}")

    await fly.close()
    await tracker.close()
    print(f"\nDone. Nothing was written to GitHub.")


if __name__ == "__main__":
    asyncio.run(main())
