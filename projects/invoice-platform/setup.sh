#!/usr/bin/env bash
# Writes .env with fresh random secrets (once). Nothing here is reused from anywhere else.
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then echo ".env already exists, keeping it"; exit 0; fi
cat > .env <<ENV
APP_KEY=base64:$(openssl rand -base64 32)
INTAKE_TOKEN=$(openssl rand -hex 32)
EXTRACTOR_API_KEY=$(openssl rand -hex 24)
MAIL_PASSWORD=$(openssl rand -hex 12)
ENV
chmod 600 .env
echo "wrote .env"
