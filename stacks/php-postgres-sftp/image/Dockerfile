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
      postgresql-dev postgresql-libs icu-libs libzip oniguruma \
      tzdata libintl curl busybox-extras

# pdo_pgsql: el driver de PostgreSQL. pgsql: el nativo. Los dos, porque un
# proyecto Laravel usa PDO y un script legacy usa pg_connect().
#
# SIN -j: compilar en paralelo hace que una extension que falle quede
# tapada por las otras ("cp: can't stat 'modules/*'" y el error real se
# pierde). Una por una, y el error dice QUE extension fallo.
RUN docker-php-ext-install -j1 pdo_pgsql
RUN docker-php-ext-install -j1 pgsql

# opcache: SI viene compilado en la imagen oficial (no tiene .so propio: se
# consulta con el nombre exacto "Zend OPcache"). intl: NO venia. El comentario
# que habia aca afirmaba que la base los traia a los dos y era falso, y por eso
# nadie noto que intl faltaba (medido: extension_loaded('intl') = false).
#
# Estas cinco son las que un cliente pide seguido y la imagen no traia:
#   intl    formateo de fechas/numeros por ICU (Laravel, Symfony, TCPDF)
#   zip     ZipArchive: exports de Excel (Laravel Excel, zipstream)
#   gd      imagenes: TCPDF, PhpSpreadsheet, thumbnails
#   bcmath  decimales exactos: JWK/JWT, barcodes PDF417 de TCPDF
#   soap    clientes SOAP (legacy)
# Un solo RUN con las libs de build en un grupo --virtual que se borra al
# final: en capas separadas los -dev quedan ocupando disco en la imagen.
# Las libs de RUNTIME van afuera del grupo: `apk del` se lleva las del grupo,
# y sin libpng/freetype/libjpeg presentes las extensiones no cargan.
# -j1 una por una: compilar en paralelo tapa el error de la que falla.
# icu-data-full: alpine parte los datos de ICU y de fabrica trae
# icu-data-en (solo ingles). Medido: con eso, intl carga pero
# ResourceBundle::getLocales() devuelve 4 locales y es_AR NO esta,
# asi que IntlDateFormatter('es_AR') cae a 'en' — una app en espanol
# formatearia fechas y monedas en ingles. icu-data-full los trae todos.
RUN apk add --no-cache libpng libjpeg-turbo freetype libxml2 icu-data-full \
 && apk add --no-cache --virtual .ext-build-deps \
      icu-dev libzip-dev libpng-dev libjpeg-turbo-dev freetype-dev libxml2-dev \
 && docker-php-ext-configure gd --with-freetype --with-jpeg \
 && docker-php-ext-install -j1 intl \
 && docker-php-ext-install -j1 zip \
 && docker-php-ext-install -j1 gd \
 && docker-php-ext-install -j1 bcmath \
 && docker-php-ext-install -j1 soap \
 && apk del .ext-build-deps

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
