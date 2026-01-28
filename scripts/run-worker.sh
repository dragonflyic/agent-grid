#!/bin/bash
# Run the local worker with environment from .env.worker

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$SCRIPT_DIR/.."
ENV_FILE="$PROJECT_DIR/.env.worker"

if [ ! -f "$ENV_FILE" ]; then
    echo "Error: $ENV_FILE not found."
    echo "Run ./scripts/setup-local-worker.sh first."
    exit 1
fi

# Export environment variables
set -a
source "$ENV_FILE"
set +a

# Check required variables
if [ -z "$AGENT_GRID_SQS_JOB_QUEUE_URL" ]; then
    echo "Error: AGENT_GRID_SQS_JOB_QUEUE_URL not set in $ENV_FILE"
    exit 1
fi

if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "Warning: ANTHROPIC_API_KEY not set. Agent execution will fail."
fi

echo "Starting Agent Grid Worker..."
echo "Job Queue: $AGENT_GRID_SQS_JOB_QUEUE_URL"
echo ""

cd "$PROJECT_DIR"
poetry run python -m agent_grid.worker
