#!/bin/bash
set -e

echo "Running database migrations..."
cd /app
python -m alembic upgrade head

echo "Starting scheduled coordinator cycle..."
exec python -m agent_grid.scheduled_task
