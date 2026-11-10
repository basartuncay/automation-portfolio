#!/usr/bin/env bash
# Writes .env with fresh random secrets (once).
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then echo ".env already exists, keeping it"; exit 0; fi
printf 'SECRET_KEY=%s\nMAIL_PASSWORD=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 12)" > .env
chmod 600 .env
echo "wrote .env (add ANTHROPIC_API_KEY=... to draft with Claude instead of the offline engine)"
