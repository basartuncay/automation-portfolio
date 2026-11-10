#!/usr/bin/env bash
# Writes .env with fresh random secrets (once).
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then echo ".env already exists, keeping it"; exit 0; fi
{
  for k in SECRET_KEY TOKEN_KEY CRM_CLIENT_SECRET SHOP_ACCESS_TOKEN SHOP_WEBHOOK_SECRET FAKES_ADMIN_TOKEN; do
    printf '%s=%s\n' "$k" "$(openssl rand -hex 32)"
  done
} > .env
chmod 600 .env
echo "wrote .env"
