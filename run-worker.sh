#!/bin/bash
# Agent Grid Worker Runner
# This script sets up the environment and runs the worker.

set -e

cd "$(dirname "$0")"

echo "==================================="
echo "Agent Grid Local Worker"
echo "==================================="

# Load worker environment
source .env.worker

# Check required environment variables
if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "ERROR: ANTHROPIC_API_KEY is not set"
    echo "Please set it in your shell or in .env.worker"
    exit 1
fi

# GitHub token is optional but recommended for pushing changes
if [ -z "$AGENT_GRID_GITHUB_TOKEN" ] && [ -z "$GITHUB_TOKEN" ]; then
    echo "WARNING: No GitHub token set - worker won't be able to push changes"
    echo "Set AGENT_GRID_GITHUB_TOKEN or GITHUB_TOKEN if you want to push to repos"
fi

echo ""
echo "Configuration:"
echo "  AWS Profile: $AWS_PROFILE"
echo "  Region: $AGENT_GRID_AWS_REGION"
echo "  Job Queue: $AGENT_GRID_SQS_JOB_QUEUE_URL"
echo "  Result Queue: $AGENT_GRID_SQS_RESULT_QUEUE_URL"
echo "  Repo Path: $AGENT_GRID_REPO_BASE_PATH"
echo ""

# Run the worker
exec poetry run python -m agent_grid.worker
