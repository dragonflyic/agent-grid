#!/usr/bin/env bash
# Upload Claude subscription credentials to AWS Secrets Manager.
# Run this locally after `claude auth login` to refresh the token
# that Fly Machine workers use for headless Claude Code execution.
#
# Usage: ./scripts/refresh-claude-credentials.sh
#
# Prerequisites:
#   - Claude Code CLI authenticated: `claude auth login`
#   - AWS CLI configured with appropriate permissions

set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
SECRET_NAME="agent-grid/claude-credentials"

echo "Checking Claude auth status..."
AUTH_STATUS=$(claude auth status 2>&1) || true

if ! echo "$AUTH_STATUS" | grep -q '"loggedIn": true'; then
    echo "Error: Claude Code is not authenticated."
    echo "Run: claude auth login"
    exit 1
fi

# Show subscription info
SUBSCRIPTION=$(echo "$AUTH_STATUS" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('subscriptionType','unknown'))" 2>/dev/null || echo "unknown")
EMAIL=$(echo "$AUTH_STATUS" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('email','unknown'))" 2>/dev/null || echo "unknown")
echo "Authenticated as: $EMAIL (subscription: $SUBSCRIPTION)"

CREDS_FILE="$HOME/.claude/.credentials.json"
if [ ! -f "$CREDS_FILE" ]; then
    echo "Error: $CREDS_FILE not found"
    exit 1
fi

# Check token expiry
EXPIRY=$(python3 -c "
import json, datetime
d = json.load(open('$CREDS_FILE'))
exp = d.get('claudeAiOauth', {}).get('expiresAt', 0)
dt = datetime.datetime.fromtimestamp(exp / 1000)
print(dt.isoformat())
" 2>/dev/null || echo "unknown")
echo "Token expires: $EXPIRY"

# Upload to Secrets Manager (update existing or create new)
echo "Uploading to Secrets Manager: $SECRET_NAME ..."
if aws secretsmanager describe-secret --region "$REGION" --secret-id "$SECRET_NAME" &>/dev/null; then
    aws secretsmanager put-secret-value \
        --region "$REGION" \
        --secret-id "$SECRET_NAME" \
        --secret-string "$(cat "$CREDS_FILE")"
    echo "Updated existing secret."
else
    aws secretsmanager create-secret \
        --region "$REGION" \
        --name "$SECRET_NAME" \
        --secret-string "$(cat "$CREDS_FILE")"
    echo "Created new secret."
fi

echo "Done! Workers will use these credentials on next launch."
