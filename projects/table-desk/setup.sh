#!/usr/bin/env bash
# Writes .env with fresh random secrets (once).
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then echo ".env already exists, keeping it"; exit 0; fi
{
  for k in SECRET_KEY AGENT_TOOL_TOKEN SMS_AUTH_TOKEN FAKES_ADMIN_TOKEN; do printf '%s=%s\n' "$k" "$(openssl rand -hex 32)"; done
  printf 'SMS_ACCOUNT_SID=AC%s\n' "$(openssl rand -hex 16)"
} > .env
chmod 600 .env
echo "wrote .env"
