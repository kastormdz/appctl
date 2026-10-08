#!/bin/sh
# Express + React: el cliente sube DOS cosas por SFTP a /upload:
#   backend/    el Express (package.json + server.js + lo que use)
#   frontend/   el React (la fuente, o ya buildeado en dist/)
#
# Decision: si el backend ya tiene node_modules y el frontend ya tiene un
# build con index.html, solo arrancamos. Si falta, instalamos y buildeamos
# nosotros: es lo que espera un dev que sube su proyecto y quiere que ande.
# nginx sirve el build del frontend como estatico y proxya /api al backend.
set -e

SFTP_USER="${SFTP_USER:?falta SFTP_USER}"
APP_USER="app"
APP_HOME="/home/${APP_USER}"
SFTP_CHROOT="/srv/sftp"
# Defaults por si el compose no los pasa (y para que el orden de creacion del
# usuario SFTP no importe: se usan UID/GID NUMERICOS, no nombres).
: "${SFTP_UID:=1001}"
: "${SFTP_GID:=1000}"
WEBROOT="/srv/sftp/upload"
APP_SRC="/app"

log() { echo "[entrypoint] $*"; }

# --- usuario del proyecto -------------------------------------------------
# node:22-alpine YA TIENE un grupo con gid 1000. Crear otro con el mismo
# gid falla con "gid '1000' in use" y, con set -e, mata el entrypoint.
# Por eso: si el grupo existe, lo reutilizo en vez de crearlo.
if ! id "${APP_USER}" >/dev/null 2>&1; then
    getent group "${APP_USER}" >/dev/null 2>&1 || addgroup -S "${APP_USER}"
    # Sin `|| true`: si getent falla (grupo recien creado y no visible
    # todavia) el pipeline lo convierte en muerte silenciosa del entrypoint.
    GID_APP=$(getent group "${APP_USER}" | cut -d: -f3 || true)
    GID_APP="${GID_APP:-1000}"
    adduser -S -D -G "${APP_USER}" -h "${APP_HOME}" -s /sbin/nologin "${APP_USER}"
    log "usuario app creado (uid=$(id -u ${APP_USER}), gid=${GID_APP})"
fi

# --- nginx: proxy a node, NO servidor de archivos ------------------------
# La imagen base trae un default.conf que sirve ARCHIVOS ESTATICOS de php
# (root /var/www/html, index index.php, try_files =404). Con ese config node
# nunca recibe nada: /healthz daba 403, el healthcheck fallaba y el
# contenedor queda unhealthy para siempre, mientras el stub (node) estava
# escuchando en 3000 y el log de nginx mostraba un 200 de otra ruta.
#
# O sea: el bug no era node, era nginx sirviendo php.
if [ -f /usr/local/share/appctl/nginx.conf ]; then
    cp /usr/local/share/appctl/nginx.conf /etc/nginx/nginx.conf
    log "nginx.conf instalado (estaticos + /api -> 127.0.0.1:3000)"
else
    log "AVISO: no encontro /usr/local/share/appctl/nginx.conf: el healthcheck puede fallar"
fi
rm -f /etc/nginx/conf.d/default.conf 2>/dev/null || true

mkdir -p "${WEBROOT}" "${APP_SRC}" /app/backend /app/frontend-dist \
         /run/nginx /var/log/nginx /var/log/supervisor

chown root:root "${SFTP_CHROOT}"
chmod 755 "${SFTP_CHROOT}"

# el chroot NO puede ser group-writable (sshd lo rechaza). El contenido que
# el cliente ve sale de un bind mount del host, no de un symlink.
# mkdir -p y NUNCA rm -rf: un rm -rf aca borraba el codigo que el cliente
# habia subido en cada recreate, porque el bind mount hace que el borrado
# pegue en el disco real del host.
mkdir -p "${SFTP_CHROOT}/upload"
# El SFTP user es el UNICO dueno de este directorio: es donde sube su
# codigo. 700 (no 755): nginx NO sirve de aca (sirve el build copiado a
# /app/frontend-dist) — nada mas que el usuario SFTP tiene por que leer
# esto. Se copia a /app, y es /app lo que corre node.
# Ownership por UID/GID NUMERICOS: el usuario SFTP se crea DESPUES de esta
# seccion, y un chown por nombre inexistente falla en silencio.
chown "${SFTP_UID}:${SFTP_GID}" "${SFTP_CHROOT}/upload" 2>/dev/null || true
chmod 700 "${SFTP_CHROOT}/upload" 2>/dev/null || true
log "upload: uid=${SFTP_UID} 700 (escribible por el usuario SFTP)"

# --- directorio privado: SFTP si, web no ----------------------------------
# Hermano de /upload dentro del chroot: el cliente lo ve por SFTP como
# /private. Aca nginx es proxy puro (no hay root), y el nginx.conf igual
# tiene el deny y el disable_symlinks: el dia que alguien agregue un server
# block con root, la barrera ya esta puesta.
# mkdir -p y NUNCA rm -rf (misma razon que arriba: el borrado pegaria en el
# disco real del host a traves del bind mount).
PRIVATEDIR="${SFTP_CHROOT}/private"
mkdir -p "${PRIVATEDIR}"
chown "${SFTP_UID}:${SFTP_GID}" "${PRIVATEDIR}" 2>/dev/null || true
chmod 700 "${PRIVATEDIR}" 2>/dev/null || true
log "private: uid=${SFTP_UID} 700 (solo SFTP, la web no lo ve)"

# nginx corre como `app` (los workers) y el codigo de /app es de app.
for _u in nginx www-data; do
    if id "$_u" >/dev/null 2>&1; then
        addgroup "$_u" app 2>/dev/null || true
        usermod -aG app "$_u" 2>/dev/null || true
    fi
done
chown -R app:app "${APP_SRC}" 2>/dev/null || true
chmod 755 "${APP_HOME}" 2>/dev/null || true

# --- chroot minimalista: solo lo que el cliente debe ver -------------------
# El SFTP corre con ForceCommand internal-sftp, EN el proceso de sshd: no
# necesita NINGUN binario en el chroot. Pero el entrypoint copiaba
# sftp-server + loader + .so + shells + /etc/passwd de la epoca del
# subsystem externo, y el cliente veia bin/, dev/, etc/, lib/, proc/ y
# usr/ al entrar. Lo visible queda: /upload y /private, nada mas. /tmp no
# se crea; si un proyecto viejo lo tiene vacio, se quita solo (abajo).
# Limpieza de proyectos viejos: SOLO esos directorios de andamiaje, lista
# explicita y guardia :? (nunca comodines, nunca las dirs del cliente).
# Eran root:root y el cliente jamas pudo escribir ahi, asi que no hay datos
# que perder.
for _rm in bin usr lib etc proc dev; do
    if [ -e "${SFTP_CHROOT}/${_rm}" ]; then
        rm -rf "${SFTP_CHROOT:?}/${_rm}" 2>/dev/null || true
        log "chroot: ${_rm}/ de andamiaje eliminado"
    fi
done
# sin /dev: medido que no hace falta con internal-sftp en proceso (login +
# listado + subida andan sin /dev/null). Menos ruido para el cliente.
rmdir "${SFTP_CHROOT}/tmp" 2>/dev/null || true
# El subsystem tiene que estar declarado EN PROCESO. Sin la linea, si algun
# dia se saca el ForceCommand el cliente recibe "subsystem request failed
# on channel 0".
if ! grep -q "^Subsystem sftp internal-sftp" /etc/ssh/sshd_config; then
    log "ERROR: sshd_config no trae 'Subsystem sftp internal-sftp'"
    exit 1
fi
log "sftp en proceso (internal-sftp): sin binarios en el chroot"

# --- usuario SFTP ---------------------------------------------------------
if ! id "${SFTP_USER}" >/dev/null 2>&1; then
    adduser -D -u ${SFTP_UID} -G app -h "${APP_HOME}" -s /sbin/nologin "${SFTP_USER}" \
        2>/dev/null || adduser -D -G app -h "${APP_HOME}" -s /sbin/nologin "${SFTP_USER}"
    log "usuario sftp ${SFTP_USER} creado"
fi
echo "${SFTP_USER}:${SFTP_PASSWORD}" | chpasswd
chage -E -1 -I -1 -m 0 -M 99999 "${SFTP_USER}" 2>/dev/null || true

# --- sshd_config ----------------------------------------------------------
sed -i.bak \
    -e "s|__SFTP_USER__|${SFTP_USER}|g" \
    -e "s|__SFTP_HOME__|${SFTP_CHROOT}|g" \
    /etc/ssh/sshd_config 2>/dev/null || true
# -d /upload: el cliente arranca parado en su codigo, con /private al lado.
sed -i "s|^ForceCommand.*|ForceCommand internal-sftp -d /upload|" /etc/ssh/sshd_config
log "sshd: AllowUsers=${SFTP_USER} ChrootDirectory=${SFTP_CHROOT}"
if grep -q '__SFTP_' /etc/ssh/sshd_config 2>/dev/null; then
    log "ERROR: sshd_config quedo con placeholders sin sustituir"
    exit 1
fi

# --- host keys ------------------------------------------------------------
if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
    log "sin host key montada: se genera una nueva"
    ssh-keygen -A >/dev/null 2>&1 || true
fi

# --- codigo del cliente ---------------------------------------------------
# El cliente sube por SFTP a /upload (el mismo dir que ve en el chroot):
#   /upload/backend/    el Express
#   /upload/frontend/   el React (la fuente, o ya buildeado)
# Se copia a /app porque el chroot es del usuario SFTP y lo que corre es de
# app, y porque el build del frontend necesita escribir ahi.
BACKEND="/app/backend"
FRONTEND="/app/frontend"
FRONTEND_DIST="/app/frontend-dist"
START=""
if [ -n "$(ls -A "${WEBROOT}" 2>/dev/null)" ]; then
    log "codigo del cliente detectado en el webroot"
    cp -a "${WEBROOT}/." "/app/" 2>/dev/null || true
fi
mkdir -p "${BACKEND}" "${FRONTEND_DIST}"
chown -R app:app /app 2>/dev/null || true
find /app -type d -exec chmod 755 {} + 2>/dev/null || true
find /app -type f -exec chmod 644 {} + 2>/dev/null || true

# --- backend (Express) ----------------------------------------------------
if [ -f "${BACKEND}/package.json" ] || [ -f "${BACKEND}/server.js" ] || \
   [ -f "${BACKEND}/index.js" ] || [ -f "${BACKEND}/app.js" ]; then
    log "express: backend del cliente detectado"
    cd "${BACKEND}"
    if [ -f package.json ] && [ ! -d node_modules ]; then
        log "backend sin node_modules: instalando"
        # El rc se mira DE VERDAD: con `npm ci ... | tail -8 || { ... }` el que
        # define el exit code es tail, que sale 0, y una instalacion fallida
        # pasaba por buena.
        if [ -f pnpm-lock.yaml ]; then
            pnpm install --frozen-lockfile --prod > /tmp/npm-install.log 2>&1 || INS_FALLO=1
        elif [ -f package-lock.json ]; then
            npm ci --omit=dev > /tmp/npm-install.log 2>&1 || INS_FALLO=1
        else
            npm install --omit=dev > /tmp/npm-install.log 2>&1 || INS_FALLO=1
        fi
        if [ -n "${INS_FALLO:-}" ]; then
            tail -20 /tmp/npm-install.log
            log "ERROR: no se pudieron instalar las deps del backend."
            log "  Causa probable: la red no llega a registry.npmjs.org. En una"
            log "  red cerrada hace falta HTTPS_PROXY, o subi el node_modules"
            log "  ya instalado por SFTP."
            exit 1
        fi
        tail -3 /tmp/npm-install.log
    fi
    # El comando de arranque lo decide lo que haya: el script `start` del
    # cliente manda (puede traer flags), si no los archivos tipicos. El
    # default es node directo porque `npm start` no reenvia bien SIGTERM al
    # hijo; npm solo cuando el cliente definio un script.
    if [ -f package.json ] && \
       node -e "const p=require('./package.json'); process.exit(p.scripts && p.scripts.start ? 0 : 1)" 2>/dev/null; then
        START="exec npm start"
    elif [ -f server.js ]; then
        START="exec node server.js"
    elif [ -f index.js ]; then
        START="exec node index.js"
    elif [ -f app.js ]; then
        START="exec node app.js"
    fi
fi

# --- frontend (React) -----------------------------------------------------
# Dos caminos: el cliente sube el build hecho (dist/, build/ u out/) o sube la
# fuente y lo buildeamos. Los dos terminan en /app/frontend-dist, que es el
# root de nginx: UNA ruta para servir, sin importar como se llame el
# directorio de salida del bundler.
FRONT_BUILT=0
if [ -d "${FRONTEND}" ]; then
    for _d in dist build out; do
        if [ -f "${FRONTEND}/${_d}/index.html" ]; then
            log "frontend: build ya presente (${_d}/), no se buildea"
            cp -a "${FRONTEND}/${_d}/." "${FRONTEND_DIST}/"
            FRONT_BUILT=1
            break
        fi
    done
    if [ "${FRONT_BUILT}" = "0" ] && [ -f "${FRONTEND}/package.json" ]; then
        cd "${FRONTEND}"
        if [ ! -d node_modules ]; then
            log "frontend sin node_modules: instalando"
            if [ -f package-lock.json ]; then
                npm ci > /tmp/fe-install.log 2>&1 || FEINS_FALLO=1
            else
                npm install > /tmp/fe-install.log 2>&1 || FEINS_FALLO=1
            fi
            if [ -n "${FEINS_FALLO:-}" ]; then
                tail -20 /tmp/fe-install.log
                log "ERROR: no se pudieron instalar las deps del frontend (misma"
                log "  causa que el backend: red, HTTPS_PROXY, o subilo instalado)"
                exit 1
            fi
            tail -3 /tmp/fe-install.log
        fi
        log "frontend: corriendo el build (puede tardar)"
        if ! npm run build > /tmp/fe-build.log 2>&1; then
            tail -25 /tmp/fe-build.log
            log "ERROR: el build del frontend fallo. Ver: appctl <proyecto> logs"
            exit 1
        fi
        tail -3 /tmp/fe-build.log
        for _d in dist build out; do
            if [ -f "${FRONTEND}/${_d}/index.html" ]; then
                cp -a "${FRONTEND}/${_d}/." "${FRONTEND_DIST}/"
                FRONT_BUILT=1
                break
            fi
        done
        if [ "${FRONT_BUILT}" = "0" ]; then
            log "AVISO: el build no dejo dist/, build/ ni out/ con index.html."
            log "  El 'build' de tu package.json tiene que emitir a una de esas."
        fi
    fi
fi

# --- stub: sin app propia, algo verificable -------------------------------
# El backend stub ES Express (viene instalado en la imagen, en
# /opt/appctl/stub): un proyecto recien creado verifica el stack de verdad
# —Express arriba, la DB alcanzable, el proxy de /api andando— y no un
# placeholder que solo devuelve texto.
if [ -z "${START}" ]; then
    log "express: sin backend propio, se usa el stub (Express de la imagen)"
    START="exec node /opt/appctl/stub/server.js"
fi
if [ ! -f "${FRONTEND_DIST}/index.html" ]; then
    log "frontend: sin build, se escribe el stub de verificacion"
    cat > "${FRONTEND_DIST}/index.html" <<'STUB'
<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>app: ok</title></head>
<body>
<h1>app: ok</h1>
<p>Stack listo: nginx sirviendo este React y proxeando /api a Express.</p>
<p>API de verificacion: <a href="/api/healthz">/api/healthz</a></p>
<p>Subi tu <code>backend/</code> y tu <code>frontend/</code> a /upload por SFTP
y reinicia el contenedor: <code>appctl &lt;proyecto&gt; restart</code>.</p>
</body></html>
STUB
fi

# --- el comando de arranque ------------------------------------------------
# El supervisor corre /app/start.sh: asi el stack no obliga a una convencion
# de arranque (server.js, index.js o el script start del cliente).
printf '#!/bin/sh\ncd %s\nexport PORT="${PORT:-3000}"\n%s\n' \
       "${BACKEND}" "${START}" > /app/start.sh
chmod 755 /app/start.sh
chown app:app /app/start.sh 2>/dev/null || true
log "start.sh: ${START}"
chown -R app:app /app 2>/dev/null || true

# --- cron ------------------------------------------------------------------
# Cron NO es un stack: no cambia la imagen ni la DB. Es una bandera
# (CRON_ENABLED) y el cliente sube su crontab a /upload/cron/crontab por
# SFTP, que es el mismo chroot del webroot.
if [ "${CRON_ENABLED:-0}" = "1" ]; then
    # El directorio se crea con el owner del usuario SFTP porque el cliente
    # sube el archivo DIRECTO adentro (no puede hacer mkdir: /upload es 700,
    # y eso es correcto para que nadie mas escriba).
    CRON_DIR="${WEBROOT}/cron"
    mkdir -p "${CRON_DIR}" /var/spool/cron/crontabs /etc/cron.d
    chmod 755 /var/spool/cron/crontabs
    # El mismo chown que recibe /upload: sin esto el directorio queda
    # root:root 755, el cliente entra por SFTP pero `mkdir /upload/cron`
    # falla con "Failure" y el crontab nunca llega.
    chown "${SFTP_UID}:${SFTP_GID}" "${CRON_DIR}" 2>/dev/null || chown "${SFTP_UID}" "${CRON_DIR}" 2>/dev/null || true
    chmod 775 "${CRON_DIR}" 2>/dev/null || true
    if [ -f "${CRON_DIR}/crontab" ]; then
        # el crontab del cliente se carga TAL CUAL
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

log "arrancando express + nginx"
exec /usr/bin/supervisord -c /etc/supervisord.conf
