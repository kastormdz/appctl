# El proxy se pasa como build arg: no va hardcodeado en el repo (el repo
# se sincroniza a otras maquinas que no usan proxy).
# Sin esto, apk no alcanza dl-cdn.alpinelinux.org en la red interna.
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG NO_PROXY="localhost,127.0.0.1"

# La version la elige el CLI: appctl build --php 8.4
# (con FROM fijo, --php etiquetaba 8.4 una imagen que era 8.5)
ARG PHP_VERSION="8.5-fpm-alpine"
FROM php:${PHP_VERSION}

# nginx, supervisor, sshd y las libs que necesita pdo_pgsql.
# apk update primero: la imagen no trae indice, sin esto da
# "supervisor (no such package)" que no dice nada del proxy.
RUN apk update && apk add --no-cache \
      nginx supervisor openssh-server openssh-sftp-server \
      icu-libs libzip oniguruma \
      tzdata libintl curl busybox-extras

# pdo_mysql: el driver de MySQL/MariaDB. mysqli: el API nativo, que es lo
# que usa el extension mysql de PHP (mysqli_connect) y algunos scripts.
# Los dos, porque un proyecto Laravel usa PDO y uno legacy usa mysqli.
#
# SIN -j: compilar en paralelo hace que una extension que falle quede
# tapada por las otras. Una por una, y el error dice QUE fallo.
RUN docker-php-ext-install -j1 pdo_mysql
RUN docker-php-ext-install -j1 mysqli

# opcache e intl: los trae la imagen base de php en modo compilado, pero
# instalarlos asi deja el .ini en el lugar que php-cgi espera.

# opcache e intl: NO se compilan. La imagen oficial php:8.5-fpm-alpine ya
# los trae compilados (php -m muestra "Zend OPcache" e "intl").
# docker-php-ext-install opcache falla con "install-modules Error 1" porque
# intenta recompilar contra los headers de zend que ya estan puestos.
#
# zip: lo dejo fuera. En alpine necesita libzip-dev y la version de
# php-zip a veces no compila; se agrega solo si un cliente lo pide
# (Laravel lo usa para excel exports). Para habilitarlo:
#   apk add libzip-dev && docker-php-ext-install -j1 zip

COPY php.ini /usr/local/etc/php/conf.d/zz-appctl.ini
COPY php-fpm.conf /usr/local/etc/php-fpm.d/zz-appctl.conf
COPY nginx.conf /etc/nginx/nginx.conf
COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY fixperms.sh /usr/local/bin/fixperms.sh
COPY supervisord.conf /etc/supervisord.conf

RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/fixperms.sh \
 && mkdir -p /var/www/html /run/nginx /run/php /var/log/nginx \
 && rm -f /etc/nginx/conf.d/default.conf

EXPOSE 80 22
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=6 \
  CMD curl -fsS http://127.0.0.1/healthz || exit 1

CMD ["/usr/local/bin/entrypoint.sh"]
