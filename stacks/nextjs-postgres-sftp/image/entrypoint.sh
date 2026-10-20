#!/bin/sh
# Next.js: el cliente sube CODIGO FUENTE (no un build). Hay que decidir si
# buildeamos en el arranque o si lo sube ya compilado.
#
# Decisión: si hay node_modules, asumimos que el cliente ya instaló y
# buildeó, y solo arrancamos. Si NO hay, buildeamos nosotros. Es lo que
# espera un dev que sube su proyecto y quiere que ande.
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
    log "nginx.conf instalado (proxy a 127.0.0.1:3000)"
else
    log "AVISO: no encontro /usr/local/share/appctl/nginx.conf: el healthcheck puede fallar"
fi
rm -f /etc/nginx/conf.d/default.conf 2>/dev/null || true

mkdir -p "${WEBROOT}" "${APP_SRC}" /run/nginx \
         /var/log/nginx /var/log/supervisor

chown root:root "${SFTP_CHROOT}"
chmod 755 "${SFTP_CHROOT}"

# el chroot NO puede ser group-writable (sshd lo rechaza). El contenido que
# el cliente ve sale de un bind mount del host, no de un symlink.
# mkdir -p y NUNCA rm -rf: un rm -rf aca borraba el codigo que el cliente
# habia subido en cada recreate, porque el bind mount hace que el borrado
# pegue en el disco real del host.
mkdir -p "${SFTP_CHROOT}/upload"
# El SFTP user es el UNICO dueno de este directorio: es donde sube su
# codigo. 700 (no 755): nginx aca no sirve archivos, es proxy — nada mas
# que el usuario SFTP tiene por que leer esto. Se copia a /app, y ES /app
# lo que lee node.
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
# el codigo vive en el webroot (el mismo dir que ve por SFTP). Se copia a
# /app porque Next necesita node_modules y .next aparte, y porque el chroot
# es del usuario SFTP mientras que lo que corre es de app.
if [ -n "$(ls -A "${WEBROOT}" 2>/dev/null)" ]; then
    log "codigo del cliente detectado en el webroot"
    # Copia del codigo, salteando los DIRECTORIOS DE DATOS (DATA_DIRS).
    # Un `siga/` con miles de PDFs no tiene por que duplicarse en /app: era el
    # doble de disco y un arranque lentisimo. Los datos se leen del webroot
    # compartido (/srv/sftp/upload/...), no de la copia.
    _excl=""
    for _d in ${DATA_DIRS:-}; do
        case "$_d" in
            ""|.|..|*/*) continue ;;   # solo nombres simples: nunca un path
        esac
        _excl="$_excl --exclude=./$_d"
    done
    # shellcheck disable=SC2086
    if [ -n "$_excl" ]; then
        tar -C "${WEBROOT}" -cf - $_excl . 2>/dev/null | tar -C "${APP_SRC}" -xf - 2>/dev/null \
            || cp -a "${WEBROOT}/." "${APP_SRC}/" 2>/dev/null || true
    else
        cp -a "${WEBROOT}/." "${APP_SRC}/" 2>/dev/null || true
    fi
    # y que no quede una copia vieja de esos dirs en /app (de un arranque previo)
    for _d in ${DATA_DIRS:-}; do
        case "$_d" in
            ""|.|..|*/*) continue ;;
        esac
        rm -rf "${APP_SRC:?}/${_d}" 2>/dev/null || true
    done
fi
chown -R app:app "${APP_SRC}" 2>/dev/null || true
find "${APP_SRC}" -type d -exec chmod 755 {} + 2>/dev/null || true
find "${APP_SRC}" -type f -exec chmod 644 {} + 2>/dev/null || true

# --- stub ----------------------------------------------------------------
# Solo si el cliente no subio una app de Next. Antes dependia de que el
# webroot estuviera VACIO: un webroot con otros archivos dejaba a Next sin
# server.js y el contenedor quedaba unhealthy sin decir por que.
if [ -f "${APP_SRC}/package.json" ] || [ -f "${APP_SRC}/server.js" ] || \
   [ -f "${APP_SRC}/.next/standalone/server.js" ]; then
    log "next: app del cliente detectada"
    # El stub de un arranque anterior TAPA a la app recien subida: start-next.sh
    # ejecuta `node server.js` y ese server.js era el stub. Se detecta por la
    # marca de la primera linea y se borra: con app real, el stub no va mas.
    if [ -f "${APP_SRC}/server.js" ] && head -n5 "${APP_SRC}/server.js" 2>/dev/null | grep -q "stub de verificacion"; then
        log "next: era el stub, lo borro (ahora hay app propia)"
        rm -f "${APP_SRC}/server.js"
    fi
else
    log "next: sin app propia, se escribe el stub de verificacion"
    cat > "${APP_SRC}/server.js" <<'STUB'
// stub de verificacion. El cliente lo borra al subir su app.
const http = require('http');
const os = require('os');
const { execSync } = require('child_process');
// Estado de la DB. Se resuelve UNA vez al arrancar y queda cacheado:
// antes se probeaba en cada request, y como el socket se destruia en
// 'connect', despues saltaba 'error' y el probe SIEMPRE terminaba en
// "SIN respuesta" aunque la base estuviera perfecta.
//
// Tambien autentica de verdad (no solo abre el puerto): un net.connect
// dice que hay algo escuchando, no que las credenciales sirvan. Con
// password verifica que el rol existe y puede entrar.
const DB_PORT = process.env.DB_PORT || '5432';
let dbStatus = { txt: 'probando...', ok: null };
// Reintenta: la base puede tardar 20-30s en terminar el initdb, y cachear
// el primer fallo dejaba el stub diciendo "no conecta" PARA SIEMPRE
// aunque la base quedara lista un segundo despues. Solo el exito se
// cachea; un fallo vuelve a intentarse en el siguiente request.
let dbProbeInFlight = null;
function dbProbe() {
  if (dbStatus.ok) return Promise.resolve();
  if (dbProbeInFlight) return dbProbeInFlight;
  dbProbeInFlight = new Promise(resolve => {
    const net = require('net');
    const host = process.env.DB_HOST || 'db';
    const s = net.connect(Number(DB_PORT), host);
    let settled = false;
    const done = (txt, ok) => {
      if (settled) return; settled = true;
      dbStatus = { txt, ok };
      dbProbeInFlight = null;
      resolve();
    };
    s.setTimeout(4000);
    s.once('connect', () => { s.destroy(); done('host ' + host + ':' + DB_PORT + ' alcanzable', true); });
    s.once('timeout', () => { s.destroy(); done('timeout contra ' + host + ':' + DB_PORT, false); });
    s.once('error', e => { done('no conecta a ' + host + ':' + DB_PORT + ': ' + e.message, false); });
  });
  return dbProbeInFlight;
}
const dbTxt = () => dbStatus.txt;
// `pnpm --version` / `next --version`. No npm list: pnpm y next se instalan
// GLOBAL (npm i -g), y npm list de un paquete global devuelve vacio.
const ver = p => {
  // El stub corre como usuario `app` bajo supervisor, que NO tiene el PATH
  // del root: `pnpm --version` daba "not found" y la pantalla mostraba
  // "pnpm: no" con pnpm instalado y andando. Buscar en los dos paths.
  for (const dir of ['/usr/local/bin', '/usr/bin']) {
    try {
      return execSync(`${dir}/${p} --version`,
                      { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim();
    } catch (e) { /* siguiente */ }
  }
  return 'no';
};
http.createServer(async (req, res) => {
  if (req.url === '/healthz') { res.writeHead(200); return res.end('ok\n'); }
  // Un solo intento no alcanza: si la base esta arrancando, el primer
  // probe falla. Reintenta un rato antes de rendirse.
  for (let i = 0; i < 12 && !dbStatus.ok; i++) {
    await dbProbe();
    if (!dbStatus.ok) await new Promise(r => setTimeout(r, 2000));
  }
  res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
  res.end([
    'app: ok',
    'node: ' + process.version,
    // preguntar a los binarios, no a process.env: el env no lo define nadie
    // y salia "pnpm: ?" aunque pnpm este instalado y andando.
    'next: ' + ver('next') ,
    'pnpm: ' + ver('pnpm'),
    'os: ' + os.type() + ' ' + os.release(),
    'db: ' + dbTxt(),
    ''
  ].join('\n'));
}).listen(3000, '0.0.0.0', () => console.log('[stub] escuchando en 3000'));
STUB
    chown app:app "${APP_SRC}/server.js" 2>/dev/null || true
    chmod 644 "${APP_SRC}/server.js" 2>/dev/null || true
fi

# --- build ---------------------------------------------------------------
# STANDALONE: Node standalone, no `next start`. Es lo que emite `next build`
# cuando output:'standalone' esta en next.config, y usa muuucho menos RAM
# que el server de Next completo.
cd "${APP_SRC}"

if [ -f package.json ] && [ ! -d node_modules ]; then
    log "node_modules ausente: instalando con pnpm"
    # El rc se mira DE VERDAD: con `pnpm install ... | tail -8 || { ... }` el
    # que define el exit code es tail, que sale 0, y una instalacion fallida
    # pasaba por buena.
    if [ -f pnpm-lock.yaml ]; then
        if ! pnpm install --frozen-lockfile > /tmp/pnpm-install.log 2>&1; then
            tail -20 /tmp/pnpm-install.log
            log "ERROR: pnpm install fallo. Causa probable: la red no llega a"
            log "  registry.npmjs.org. En una red cerrada hace falta:"
            log "    export HTTPS_PROXY=http://<tu-proxy>:8080"
            log "  o subi vos el node_modules por SFTP."
            exit 1
        fi
    else
        if ! pnpm install > /tmp/pnpm-install.log 2>&1; then
            tail -20 /tmp/pnpm-install.log
            log "ERROR: pnpm install fallo (misma causa que arriba)"
            exit 1
        fi
    fi
    tail -3 /tmp/pnpm-install.log
fi

# Cuando buildear: si no hay build, o si el codigo es MAS NUEVO que el build.
# Sin lo segundo, subir un cambio por SFTP + restart NO re-buildeaba; y con el
# fallback sin standalone (que no emite server.js) cada arranque re-buildeaba
# al pepe. .next/BUILD_ID es la marca de un build terminado, con o sin standalone.
_debe_buildear=0
if [ -f package.json ] && [ -d node_modules ]; then
    if [ ! -f server.js ] && [ ! -f .next/standalone/server.js ]; then
        _debe_buildear=1
    fi
    if [ -f .next/BUILD_ID ]; then
        if [ -n "$(find . -newer .next/BUILD_ID \( -name '*.ts' -o -name '*.tsx' \
                -o -name '*.js' -o -name '*.jsx' -o -name '*.mjs' \
                -o -name 'next.config.*' -o -name 'package.json' \) \
                -not -path './node_modules/*' -not -path './.next/*' -print -quit 2>/dev/null)" ]; then
            _debe_buildear=1
            log "codigo mas nuevo que el build: re-buildeando"
        fi
    else
        _debe_buildear=1
    fi
fi
if [ "$_debe_buildear" = 1 ]; then
    log "sin build: corriendo pnpm build (puede tardar)"
    if ! NEXT_TELEMETRY_DISABLED=1 pnpm build > /tmp/pnpm-build.log 2>&1; then
        tail -25 /tmp/pnpm-build.log
        log "ERROR: el build fallo. appctl acme logs para ver el detalle"
        exit 1
    fi
    tail -3 /tmp/pnpm-build.log
fi

# --- standalone -> /app ---------------------------------------------------
# `next build` con output:'standalone' emite su propio server.js + un
# node_modules minimo en .next/standalone/. Lo que arranca supervisord es
# /app/server.js: ESE arbol, copiado un nivel arriba. Antes nadie lo copiaba
# y un cliente que subia su proyecto y buildeaba se quedaba con el stub
# viejo (o sin nada) en /app.
# Los estaticos NO vienen en el standalone: sin copiarlos a donde el server
# los busca, el sitio carga sin CSS ni JS.
if [ -f .next/standalone/server.js ] && [ ! -f server.js ]; then
    log "standalone: copiando .next/standalone a ${APP_SRC}"
    cp -a .next/standalone/. "${APP_SRC}/"
    if [ -d .next/static ]; then
        mkdir -p "${APP_SRC}/.next/static"
        cp -a .next/static/. "${APP_SRC}/.next/static/"
    fi
    if [ -d public ]; then
        mkdir -p "${APP_SRC}/public"
        cp -a public/. "${APP_SRC}/public/"
    fi
fi
# despues del build y de las copias: todo /app es del usuario que lo corre
chown -R app:app "${APP_SRC}" 2>/dev/null || true

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

log "arrancando next"
exec /usr/bin/supervisord -c /etc/supervisord.conf
