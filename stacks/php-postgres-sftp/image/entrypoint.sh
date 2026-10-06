#!/bin/sh
set -e

SFTP_USER="${SFTP_USER:?falta SFTP_USER}"
APP_USER="app"
APP_HOME="/home/${APP_USER}"
# ChrootDirectory de sshd: tiene que ser propiedad de ROOT y no
# group-writable, o sshd lo rechaza con "Connection closed". Por eso NO es
# el home del usuario (que es de app:app 775) sino un dir aparte.
SFTP_CHROOT="/srv/sftp"
# uid/gid numericos del usuario SFTP: el chown los usa en vez de un
# nombre para no depender del orden (el usuario se crea mas abajo).
: "${SFTP_UID:=1001}"
: "${SFTP_GID:=1000}"
SFTP_USER_HOME="/home/${APP_USER}/upload"
# el webroot ES el upload del chroot: el cliente sube por SFTP y
# nginx sirve el mismo archivo. Un solo directorio.
WEBROOT="/srv/sftp/upload"

log() { echo "[entrypoint] $*"; }

# --- usuario del proyecto -------------------------------------------------
# El cliente sube por SFTP y php-fpm sirve. Si son usuarios distintos, el
# archivo subido no lo puede leer el web: por eso los dos son app, y el
# directorio de codigo es de app.
if ! id "${APP_USER}" >/dev/null 2>&1; then
    addgroup -g 1000 -S app
    adduser  -u 1000 -S -D -G app -h "${APP_HOME}" -s /sbin/nologin app

# nginx (los workers) tiene que poder LEER lo que sube el cliente por SFTP.
# En Alpine el worker corre como `nginx`; si no esta en el grupo app, un
# archivo que el cliente sube con un modo raro le da 403 y el sitio no carga
# (el healthcheck tampoco: 404 en vez de 200).
for _u in nginx www-data; do
    if id "$_u" >/dev/null 2>&1; then
        addgroup "$_u" app 2>/dev/null || true
        usermod -aG app "$_u" 2>/dev/null || true
    fi
done
    log "usuario app creado (uid 1000)"
fi

# --- home del SFTP --------------------------------------------------------
# ChrootDirectory: el padre DEBE ser root y no group-writable, o sshd lo
# rechaza. /home es root:root 755, asi que el home va en /home/app/upload.
# chroot de sshd: root:root 755 (requisito de OpenSSH). Adentro va un
# symlink al home real del usuario, que es lo que ve el cliente como /.
mkdir -p "${SFTP_CHROOT}"
chown root:root "${SFTP_CHROOT}" 2>/dev/null || true
chmod 755 "${SFTP_CHROOT}" 2>/dev/null || true
mkdir -p "${APP_HOME}/upload" "${WEBROOT}" /run/php /run/nginx \
         /var/log/nginx /var/log/supervisor

chown -R app:app "${APP_HOME}" 2>/dev/null || true
chmod 755 "${APP_HOME}" 2>/dev/null || true
chmod 775 "${APP_HOME}/upload" "${WEBROOT}" 2>/dev/null || true

# el chroot NO puede ser group-writable (sshd lo rechaza). El contenido
# que el cliente ve sale de un bind mount del host, no de un symlink:
# con symlink, el cliente sube a /upload que es ${SFTP_CHROOT}/upload, y
# ese path no existe en el host.
rm -rf "${SFTP_CHROOT}/upload"
mkdir -p "${SFTP_CHROOT}/upload"
# el SFTP user (uid 1001) tiene que poder ESCRIBIR aca: es el unico
# directorio del chroot donde el cliente sube su codigo. app:app 775 no
# alcanza si el usuario no esta en el grupo app.
# el ownership lo hace el grupo, NO el owner: sftp-server exige que el
# directorio de upload sea del usuario (uid 1001), pero el chroot y sus
# padres root:root. Cambiar el owner del chroot a un usuario hace que sshd
# rechace la sesion y el contenedor entre en restart loop.
# Ownership por UID/GID NUMERICOS, no por nombre: el usuario SFTP se crea
# DESPUES de esta seccion (mas abajo en el script), y chown con un nombre
# inexistente falla -> con `set -e` el entrypoint muere -> restart loop.
# Con numeros no depende del orden.
chown "${SFTP_UID}:${SFTP_GID}" "${SFTP_CHROOT}/upload" 2>/dev/null || true
chmod 700 "${SFTP_CHROOT}/upload" 2>/dev/null || true
log "upload: uid=1001 700 (escribible por el usuario SFTP)"

# --- internal-sftp tiene que existir DENTRO del chroot --------------------
# "subsystem request failed on channel 0": la auth funciona pero internal-sftp
# no esta. En Alpine el binario esta en /usr/lib/ssh/sftp-server, y el
# chroot no lo tiene. Hay que ponerlo ahi y las libs que usa.
SFTP_BIN=$(command -v internal-sftp || true)
if [ -z "${SFTP_BIN}" ] && [ -x /usr/lib/ssh/sftp-server ]; then
    SFTP_BIN=/usr/lib/ssh/sftp-server
fi
if [ -n "${SFTP_BIN}" ]; then
    mkdir -p "${SFTP_CHROOT}/usr/lib/ssh" "${SFTP_CHROOT}/usr/bin"
    cp -a "${SFTP_BIN}" "${SFTP_CHROOT}/usr/lib/ssh/sftp-server"
    chmod 755 "${SFTP_CHROOT}/usr/lib/ssh/sftp-server" 2>/dev/null || true
    # Musl: el loader y las libs van en /lib. Si falta el loader, el binario
    # no arranca y el error del cliente es "subsystem request failed".
    if [ -f /lib/ld-musl-x86_64.so.1 ]; then
        mkdir -p "${SFTP_CHROOT}/lib"
        cp -a /lib/ld-musl-x86_64.so.1 "${SFTP_CHROOT}/lib/" 2>/dev/null || true
    fi
    # cada .so que pide, a SU path exacto (no a /lib generico: con musl el
    # loader los busca donde el ELF dice, no donde nosotros los pongamos)
    ldd "${SFTP_BIN}" 2>/dev/null | grep -oE '/[^ ]+\.so[^ ]*' | sort -u | while read -r lib; do
        [ -f "${lib}" ] || continue
        mkdir -p "${SFTP_CHROOT}$(dirname "${lib}")"
        cp -aL "${lib}" "${SFTP_CHROOT}${lib}" 2>/dev/null || true
    done
    # y el interprete de shells, por si el cliente lo pide
    for b in /bin/sh /bin/ls /bin/cat /bin/mkdir; do
        [ -f "${b}" ] || continue
        mkdir -p "${SFTP_CHROOT}$(dirname "${b}")"
        cp -aL "${b}" "${SFTP_CHROOT}${b}" 2>/dev/null || true
        for lib in $(ldd "${b}" 2>/dev/null | grep -oE '/[^ ]+\.so[^ ]*' | sort -u); do
            [ -f "${lib}" ] || continue
            mkdir -p "${SFTP_CHROOT}$(dirname "${lib}")"
            cp -aL "${lib}" "${SFTP_CHROOT}${lib}" 2>/dev/null || true
        done
    done
    # --- device nodes minimos ------------------------------------------
    # Sin /dev/null, internal-sftp aborta al arrancar con
    # "Couldn't open /dev/null" y el cliente ve
    # "subsystem request failed on channel 0". El chroot tiene que traer
    # los nodes, no solo los archivos.
    mkdir -p "${SFTP_CHROOT}/dev" "${SFTP_CHROOT}/proc" "${SFTP_CHROOT}/tmp"
    chmod 1777 "${SFTP_CHROOT}/tmp" 2>/dev/null || true
    for dev in "null c 1 3 666" "zero c 1 5 666" "random c 1 8 666" "urandom c 1 9 666"; do
        set -- ${dev}
        [ -e "${SFTP_CHROOT}/dev/$1" ] || mknod -m "$5" "${SFTP_CHROOT}/dev/$1" "$2" "$3" "$4" 2>/dev/null || true
    done

    # --- /etc/passwd y /etc/group ---------------------------------------
    # "No user found for uid 0": el chroot no tiene quien sea el uid con el
    # que corre el proceso. sshd corre como root y espera poder resolverlo.
    cp /etc/passwd "${SFTP_CHROOT}/etc_passwd_tmp" 2>/dev/null || true
    mkdir -p "${SFTP_CHROOT}/etc"
    cp /etc/passwd "${SFTP_CHROOT}/etc/passwd" 2>/dev/null || true
    cp /etc/group "${SFTP_CHROOT}/etc/group" 2>/dev/null || true
    cp /etc/shells "${SFTP_CHROOT}/etc/shells" 2>/dev/null || true
    rm -f "${SFTP_CHROOT}/etc_passwd_tmp"
    # solo root y el usuario del proyecto: no el passwd entero del contenedor
    if [ -f "${SFTP_CHROOT}/etc/passwd" ]; then
        grep -E "^(root|${APP_USER}|${SFTP_USER}):" /etc/passwd \
            > "${SFTP_CHROOT}/etc/passwd" 2>/dev/null || true
        grep -E "^(root|app):" /etc/group \
            > "${SFTP_CHROOT}/etc/group" 2>/dev/null || true
    fi

    # prueba de humo: el binario tiene que poder ejecutar DENTRO del chroot
    # Probar el binario sin args: si responde "usage" arranca, si se queja de
    # /dev/null, uid, o libs, no. -h NO es un flag valido de sftp-server.
    # pipefail + este pipe: si el chroot falla, el exit code viaja por el pipe
# y el `set -e` corta el entrypoint sin log. Los otros stacks ya tienen
# el `|| true`; este se agrego antes y quedo sin el.
OUT=$(chroot "${SFTP_CHROOT}" /usr/lib/ssh/sftp-server 2>&1 | head -1 || true)
    case "${OUT}" in
        usage:*|sftp-server\ version*)
            log "internal-sftp OK en el chroot" ;;
        "")
            log "internal-sftp arranca sin error" ;;
        *)
            log "ERROR: internal-sftp no arranca: ${OUT}" ;;
    esac

    # el subsystem tiene que estar declarado. Sin la linea "Subsystem sftp",
    # el binario puede estar perfecto y el cliente igual recibe
    # "subsystem request failed on channel 0".
    if ! grep -q "^Subsystem sftp" /etc/ssh/sshd_config; then
        log "ERROR: no hay linea 'Subsystem sftp' en sshd_config"
    fi
else
    log "ERROR: no encontre internal-sftp; el SFTP no va a funcionar"
fi

# --- usuario SFTP ---------------------------------------------------------
if ! id "${SFTP_USER}" >/dev/null 2>&1; then
    adduser -D -u 1001 -G app -h "${APP_HOME}" -s /sbin/nologin "${SFTP_USER}" \
        2>/dev/null || adduser -D -G app -h "${APP_HOME}" -s /sbin/nologin "${SFTP_USER}"
    log "usuario sftp ${SFTP_USER} creado"
fi
echo "${SFTP_USER}:${SFTP_PASSWORD}" | chpasswd
# primer login: cambiar la password fuerza cambio, no quiero eso en un server
# unattended -> la marco como ya usada
chage -E -1 -I -1 -m 0 -M 99999 "${SFTP_USER}" 2>/dev/null || true

# --- sshd_config: sustituir los placeholders ----------------------------
# El baked-in trae AllowUsers __SFTP_USER__ y ChrootDirectory __SFTP_HOME__.
# Con esos literales, sshd rechaza a TODOS: el unico usuario permitido se
# llama "__SFTP_USER__", que no existe. Ademas el chroot de internal-sftp
# tiene que ser una ruta ABSOLUTA y su padre root:root no group-writable.
sed -i.bak \
    -e "s|__SFTP_USER__|${SFTP_USER}|g" \
    -e "s|__SFTP_HOME__|${SFTP_CHROOT}|g" \
    /etc/ssh/sshd_config

# ForceCommand internal-sftp NO sirve con ChrootDirectory: "internal-sftp"
# es un wrapper de sshd, no un binario, y no existe dentro del chroot. El
# cliente pide el subsystem y sshd responde "subsystem request failed".
# Hay que apuntar al binario real, con el path DENTRO del chroot.
sed -i "s|^ForceCommand.*|ForceCommand internal-sftp -d /upload|" /etc/ssh/sshd_config
log "sshd: AllowUsers=${SFTP_USER} ChrootDirectory=${SFTP_CHROOT}"

# Si esto falla, el cliente no puede entrar por SFTP y el error del cliente
# va a ser "Permission denied" sin pista. Mejor que el arranque falle aca.
if grep -q '__SFTP_' /etc/ssh/sshd_config; then
    log "ERROR: sshd_config quedo con placeholders sin sustituir"
    exit 1
fi
# el padre del chroot tiene que ser root y NO group-writable
CHROOT_PARENT=$(dirname "${APP_HOME}")
if [ "$(stat -c '%U' "${CHROOT_PARENT}")" != "root" ]; then
    log "aviso: ${CHROOT_PARENT} no es de root; el chroot puede fallar"
fi

# --- host keys: identicas entre todos los stacks -------------------------
# Si cada contenedor genera las suyas, el cliente ve una huella distinta cada
# vez que se recrea el contenedor. Se montan desde el host.
if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
    log "sin host key montada: se genera una nueva"
    ssh-keygen -A >/dev/null 2>&1 || true
else
    log "host key montada desde el host"
fi

# (los permisos del codigo se alinean al FINAL, despues de crear el
#  usuario SFTP: antes el chown "${SFTP_UID}" falla en silencio y upload queda root:root)

# --- stubs de configuracion, solo si el cliente no trajo los suyos --------
# Re-escribir el stub SIEMPRE que sea el nuestro. El check anterior solo
# escribia si el archivo no existia: un proyecto recreado de postgres a
# mysql conservaba el index.php viejo y la pagina seguia usando
# pdo_pgsql y "SIN CONEXION" en un stack MariaDB. La marca
# APPCTL_STUB lo distingue del codigo real del cliente: si el cliente
# subio su index.php, no se toca.
if [ ! -f ${WEBROOT}/index.php ] || grep -q APPCTL_STUB ${WEBROOT}/index.php 2>/dev/null; then
    log "escribiendo el stub de verificacion (marcado APPCTL_STUB)"
    cat > ${WEBROOT}/index.php <<'PHP'
<?php // APPCTL_STUB: stub de appctl, no es codigo del cliente
// stub de verificacion: lo reemplaza el cliente al subir su app
//
// /healthz responde 200 sin tocar la DB: si el healthcheck dependiera de la
// base, un fallo de la DB marcaria la app unhealthy cuando el problema es
// otro, y el orquestador podria matar el contenedor.
if (($_SERVER['REQUEST_URI'] ?? '') === '/healthz') {
    header('Content-Type: text/plain');
    echo "ok\n";
    exit(0);
}
header('Content-Type: text/plain; charset=utf-8');

// pg_connect toma 2 args: connection_string y flags. Poner user/pass como
// 3er argumento es ArgumentCountError -> 500 -> healthcheck unhealthy.
$dsn = sprintf(
    'host=%s port=%s dbname=%s user=%s password=%s',
    getenv('DB_HOST'), getenv('DB_PORT') ?: '5432', getenv('DB_NAME'),
    getenv('DB_USER'), getenv('DB_PASSWORD')
);
$ok = @pg_connect($dsn);
$pdo = false;
try {
    $pdo = new PDO('pgsql:' . $dsn);
    $pdo->query('SELECT 1');
} catch (Throwable $e) { $pdo = false; }

echo "app: ok\n";
echo "php: " . PHP_VERSION . "\n";
echo "nginx: " . ($_SERVER['SERVER_SOFTWARE'] ?? '?') . "\n";
echo "pdo_pgsql: " . (extension_loaded('pdo_pgsql') ? "ok" : "NO") . "\n";
echo "pgsql: " . (extension_loaded('pgsql') ? "ok" : "NO") . "\n";
echo "opcache: " . (extension_loaded('Zend OPcache') ? "ok" : "no") . "\n";
echo "intl: " . (extension_loaded('intl') ? "ok" : "no") . "\n";
echo "db_pg: " . ($ok ? "conectado" : "SIN CONEXION") . "\n";
echo "db_pdo: " . ($pdo ? "conectado" : "SIN CONEXION") . "\n";
PHP
    chown "${SFTP_UID}:${SFTP_GID}" "${WEBROOT}/index.php" 2>/dev/null || true
fi

# --- cron ------------------------------------------------------------------
# Cron NO es un stack: no cambia la imagen ni la DB. Es una bandera
# (CRON_ENABLED) y el cliente sube su crontab a /upload/cron/crontab por
# SFTP, que es el mismo chroot del webroot. Sin esto, cron seria otro
# directorio de stack duplicado por cada combinacion runtime x db x sftp.
if [ "${CRON_ENABLED:-0}" = "1" ]; then
    # El directorio se crea con el owner del usuario SFTP porque el cliente
    # sube el archivo DIRECTO adentro (no puede hacer mkdir: /upload es 700,
    # y eso es correcto para que nadie mas escriba).
    CRON_DIR="${WEBROOT}/cron"
    mkdir -p "${CRON_DIR}" /var/spool/cron/crontabs /etc/cron.d
    chmod 755 /var/spool/cron/crontabs
    # El mismo chown que recibe /upload: sin esto el directorio queda
    # root:root 755, el cliente entra por SFTP pero `mkdir /upload/cron`
    # falla con "Failure" y el crontab nunca llega. El upload y el cron
    # tienen que ser del MISMO usuario.
    chown "${SFTP_UID}:${SFTP_GID}" "${CRON_DIR}" 2>/dev/null || true
    chmod 775 "${CRON_DIR}" 2>/dev/null || true
    if [ -f "${CRON_DIR}/crontab" ]; then
        # el crontab del cliente se carga TAL CUAL: si quiere una macro de
        # ejemplo o un wrapper, los escribe el.
        crontab -u app "${CRON_DIR}/crontab" 2>/dev/null \
            || crontab "${CRON_DIR}/crontab" 2>/dev/null \
            || log "AVISO: crontab del cliente invalido; cron queda vacio"
        log "crontab cargado desde ${CRON_DIR}/crontab"
    else
        log "cron activo sin crontab: subi uno a /upload/cron/crontab"
    fi
    # CRON_TZ evita que un job de las 3am corra a otra hora: el contenedor
    # esta en UTC por defecto.
    printf 'CRON_TZ=%s\n' "${TZ:-America/Argentina/Buenos_Aires}" > /etc/cron.d/appctl-tz
fi

# --- permisos del codigo (AL FINAL: el usuario SFTP ya existe) -------------
# El upload es del usuario SFTP (uid 1001), NO de app: con chown -R app el
# cliente deja de poder escribir. Y va ACA y no antes de crear el usuario,
# porque `chown 1001` con un uid sin passwd entry falla en silencio (por el
# `|| true`) y upload queda root:root -> el cliente entra pero no escribe.
find "${WEBROOT}" -mindepth 1 -type d -exec chmod 755 {} + 2>/dev/null || true
find "${WEBROOT}" -type f -exec chmod 644 {} + 2>/dev/null || true
# Por VARIABLE, como en nextjs y tomcat: no depende de que el usuario SFTP ya
# este en /etc/passwd. Con el uid fijo (1001) el chown corria antes de crearlo,
# fallaba en silencio por el `|| true` y upload quedaba root:root.
chown -R "${SFTP_UID}:${SFTP_GID}" "${WEBROOT}" 2>/dev/null || true
# el upload MISMO en 700: es del usuario SFTP y php-fpm lo lee como app, que
# esta en el grupo (SFTP_GID).
chown "${SFTP_UID}:${SFTP_GID}" "${WEBROOT}" 2>/dev/null || true
# 755 y NO 700: nginx corre como www-data y tiene que poder ATRAVESAR
# /upload para servir index.php. Con 700 el healthcheck daba 404
# (stat() failed: Permission denied) y el contenedor queda unhealthy. El
# cliente igual no puede mkdir en /upload: eso lo decide el chroot (es la
# raiz), no el modo del directorio.
chmod 755 "${WEBROOT}" 2>/dev/null || true
log "listo: app=app sftp=${SFTP_USER} webroot=${WEBROOT}"
# --- php.ini en runtime ---------------------------------------------------
# Los defaults de endurecimiento van horneados en la imagen
# (/usr/local/etc/php/conf.d/zz-appctl.ini). Aca se escribe SOLO un override,
# para el proyecto que necesita algo distinto del default. Existe porque hay
# un default que no aplica siempre:
#
#   APPCTL_SESSION_COOKIE_SECURE=0
#       La app se usa por HTTP plano (sin TLS adelante). Con cookie_secure=1
#       el navegador no manda la cookie de sesion y el login no queda
#       logueado: se ve como "la app no guarda la sesion".
#   APPCTL_ALLOW_URL_FOPEN=On
#       La app hace file_get_contents() de una URL y no se puede pasar a curl.
#
# El archivo se ordena DESPUES de zz-appctl.ini (`r` > `a`), que es lo que
# hace que el override gane.
RUNTIME_INI=/usr/local/etc/php/conf.d/zz-runtime.ini
rm -f "${RUNTIME_INI}"
if [ -n "${APPCTL_SESSION_COOKIE_SECURE:-}" ]; then
    case "${APPCTL_SESSION_COOKIE_SECURE}" in
        0|[Oo]ff|false) printf 'session.cookie_secure = 0\n' >> "${RUNTIME_INI}" ;;
        *)              printf 'session.cookie_secure = 1\n' >> "${RUNTIME_INI}" ;;
    esac
fi
if [ -n "${APPCTL_ALLOW_URL_FOPEN:-}" ]; then
    case "${APPCTL_ALLOW_URL_FOPEN}" in
        [Oo]n|1|true) printf 'allow_url_fopen = On\n' >> "${RUNTIME_INI}" ;;
        *)            printf 'allow_url_fopen = Off\n' >> "${RUNTIME_INI}" ;;
    esac
fi
if [ -s "${RUNTIME_INI}" ]; then
    log "php.ini runtime: $(tr '\n' ' ' < "${RUNTIME_INI}")"
else
    rm -f "${RUNTIME_INI}"
fi

# si algo de lo anterior fallo, supervisor no debe arrancar: es mejor un
# exit 1 con el log claro que un restart loop sin diagnostico.
exec /usr/bin/supervisord -c /etc/supervisord.conf
