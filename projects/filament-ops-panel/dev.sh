#!/usr/bin/env bash
# Run any command inside the PHP 8.4 dev container, as your own user, in ./src. Example: ./dev.sh php artisan test
set -euo pipefail
cd "$(dirname "$0")"
docker image inspect filament-dev >/dev/null 2>&1 || docker build -q -t filament-dev . >/dev/null
mkdir -p src
exec docker run --rm -i -u "$(id -u):$(id -g)" -e COMPOSER_HOME=/tmp/c -e HOME=/tmp \
  ${PORT:+-p "$PORT:$PORT"} -v "$PWD/src:/app" -w /app filament-dev "$@"
