#!/bin/sh
# Corren una vez al arrancar. Alinea permisos de codigo y home.
# El cliente sube por SFTP como su usuario; si el archivo queda 600:root,
# php-fpm (que corre como app) no lo puede leer y el sitio da 403.
chown -R app:app /var/www/html 2>/dev/null || true
find /var/www/html -type d -exec chmod 755 {} + 2>/dev/null || true
find /var/www/html -type f -exec chmod 644 {} + 2>/dev/null || true
chown -R app:app /home/app/upload 2>/dev/null || true
echo "[fixperms] permisos alineados"
