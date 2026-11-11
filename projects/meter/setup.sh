#!/usr/bin/env bash
# Writes .env with fresh random secrets (once).
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then echo ".env already exists, keeping it"; exit 0; fi
{
  for k in SECRET_KEY FAKES_ADMIN_TOKEN; do printf '%s=%s\n' "$k" "$(openssl rand -hex 32)"; done
  printf 'STRIPE_SECRET_KEY=sk_test_%s\nSTRIPE_WEBHOOK_SECRET=whsec_%s\n' "$(openssl rand -hex 24)" "$(openssl rand -hex 24)"
} > .env
chmod 600 .env
echo "wrote .env"
