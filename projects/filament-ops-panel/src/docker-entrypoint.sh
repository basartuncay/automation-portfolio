#!/bin/sh
# Container start: prepare the database, optionally seed demo data and register the intake token, then serve.
set -e
: "${APP_KEY:?APP_KEY must be set (php artisan key:generate --show)}"
touch "$DB_DATABASE"
php artisan migrate --force --no-interaction
if [ "${DEMO_SEED:-false}" = "true" ] && [ ! -f /data/.seeded ]; then  # seed once per volume
  php artisan db:seed --force --no-interaction && touch /data/.seeded
fi
if [ -n "$INTAKE_TOKEN" ]; then
  php artisan invoices:token "${INTAKE_USER:-intake@example.com}" --name=intake --token="$INTAKE_TOKEN"
fi
php artisan config:cache && php artisan route:cache && php artisan view:cache
exec frankenphp php-server --root public --listen :8080
