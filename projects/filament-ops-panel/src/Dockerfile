# Production-style image: FrankenPHP serving Laravel, SQLite on a volume.
FROM dunglas/frankenphp:1-php8.4
RUN install-php-extensions intl zip bcmath pdo_sqlite opcache
COPY --from=composer:2 /usr/bin/composer /usr/bin/composer
WORKDIR /app
COPY composer.json composer.lock ./
RUN composer install --no-dev --no-scripts --no-autoloader --no-interaction --prefer-dist
COPY . .
RUN composer dump-autoload --optimize --no-dev && php artisan filament:upgrade --no-interaction \
 && rm -f .env database/database.sqlite && mkdir -p /data \
 && chown -R www-data:www-data storage bootstrap/cache /data
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/panel-entrypoint
USER www-data
ENV APP_ENV=production APP_DEBUG=false DB_CONNECTION=sqlite DB_DATABASE=/data/database.sqlite \
    LOG_CHANNEL=stderr SESSION_DRIVER=database CACHE_STORE=database
EXPOSE 8080
HEALTHCHECK CMD php -r 'exit(@file_get_contents("http://127.0.0.1:8080/up") === false ? 1 : 0);'
ENTRYPOINT ["panel-entrypoint"]
