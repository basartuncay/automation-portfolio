FROM php:8.4-cli
RUN apt-get update && apt-get install -y --no-install-recommends libicu-dev libzip-dev unzip git sqlite3 \
 && docker-php-ext-install intl zip bcmath \
 && rm -rf /var/lib/apt/lists/*
COPY --from=composer:2 /usr/bin/composer /usr/bin/composer
WORKDIR /app
