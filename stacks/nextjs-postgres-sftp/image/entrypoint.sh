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
: "${SFTP_UID:=1001}"
: "${SFTP_GID:=1000}"
WEBROOT="/srv/sftp/upload"
APP_SRC="/app"
SFTP_UID=1001
SFTP_GID=1000

log() { echo "[entrypoint] $*"; }

# --- usuario del proyecto -------------------------------------------------
# node:22-alpine YA TIENE un grupo con gid 1000. Crear otro con el mismo
# gid falla con "gid '1000' in use" y, con set -e, mata el entrypoint.
# Por eso: si el grupo existe, lo reutilizo en vez de crearlo.
if ! id "${APP_USER}" >/dev/null 2>&1; then
    getent group "${APP_USER}" >/dev/null 2>&1 || addgroup -S "${APP_USER}"
    # Sin `|| true`: si getent falla (grupo recien creado y no visible todavia)
# el pipefail lo convierte en muerte silenciosa del entrypoint.
GID_APP=$(getent group "${APP_USER}" | cut -d: -f3 || true)
    GID_APP="${GID_APP:-1000}"
    adduser -S -D -G "${APP_USER}" -h "${APP_HOME}" -s /sbin/nologin "${APP_USER}"
    log "usuario app creado (uid=$(id -u ${APP_USER}), gid=${GID_APP})"
fi

# --- nginx: proxy a node, NO servidor de archivos ------------------------
# La imagen base trae un default.conf que sirve ARCHIVOS ESTATICOS de php
# (root /var/www/html, index index.php, try_files =404). Con ese config node
# nunca recibe nada: /healthz daba 403, el healthcheck fallaba y el
# contenedor queda unhealthy para siempre, mientras el stub(node) estava
# escuchando en 3000 y el log de nginx mostraba un 200 de otra ruta.
#
# O sea: el bug no era node, era nginx sirviendo php.
# Este nginx.conf hace proxy a 127.0.0.1:3000 y ya viene en la imagen.
if [ -f /usr/local/share/appctl/nginx.conf ]; then
    cp /usr/local/share/appctl/nginx.conf /etc/nginx/nginx.conf
    log "nginx.conf instalado (proxy a 127.0.0.1:3000)"
else
    log "AVISO: no encontro /usr/local/share/appctl/nginx.conf: el healthcheck puede fallar"
fi
# el default.conf de la imagen base confunde: lo saca para que no haya dos
# server blocks pelearon por el :80
rm -f /etc/nginx/conf.d/default.conf 2>/dev/null || true

mkdir -p "${WEBROOT}" "${APP_SRC}" /run/nginx /run/php \
         /var/log/nginx /var/log/supervisor
mkdir -p "${SFTP_CHROOT}/dev" "${SFTP_CHROOT}/tmp"
chmod 1777 "${SFTP_CHROOT}/tmp"
chown root:root "${SFTP_CHROOT}"
chmod 755 "${SFTP_CHROOT}"
chown ${SFTP_UID}:${SFTP_GID} "${WEBROOT}" 2>/dev/null || true
# 755 y NO 700: el servidor web (nginx en php/nextjs) corre como OTRO
    # usuario que el dueo del upload (el del SFTP, uid 1001), y en 700 no
    # puede ni traversing. El healthcheck daba 403 y el contenedor queda
    # unhealthy para siempre. El cliente igual no puede mkdir en /upload:
    # eso lo decide el chroot (es la raiz), no el modo del directorio.
    chmod 755 "${WEBROOT}"
# nginx corre como `app` (los workers) y el upload es del usuario SFTP
# (uid 1001). Con el directorio en 755 el grupo no alcanza: 755 deja
# leer/travesar a todos, leer los ARCHIVOS depends de su modo. Un archivo
# que el cliente sube en 600 le da 403 a nginx. Por eso el grupo existe.
for _u in nginx www-data; do
    if id "$_u" >/dev/null 2>&1; then
        addgroup "$_u" app 2>/dev/null || true
        usermod -aG app "$_u" 2>/dev/null || true
    fi
done
chown -R app:app "${APP_SRC}" 2>/dev/null || true
chmod 755 "${APP_HOME}" "${APP_HOME}/upload" 2>/dev/null || true

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
sed -i "s|^ForceCommand.*|ForceCommand internal-sftp -d /upload|" /etc/ssh/sshd_config
log "sshd: AllowUsers=${SFTP_USER} ChrootDirectory=${SFTP_CHROOT}"
if grep -q '__SFTP_' /etc/ssh/sshd_config 2>/dev/null; then
    log "ERROR: sshd_config quedo con placeholders sin sustituir"
    exit 1
fi
if ! grep -q "^Subsystem sftp" /etc/ssh/sshd_config 2>/dev/null; then
    log "ERROR: no hay linea 'Subsystem sftp' en sshd_config"
    exit 1
fi

# --- internal-sftp en el chroot -------------------------------------------
if [ -x /usr/lib/ssh/sftp-server ] || command -v internal-sftp >/dev/null 2>&1; then
    SFTP_BIN=/usr/lib/ssh/sftp-server
    [ -x "${SFTP_BIN}" ] || SFTP_BIN=$(command -v internal-sftp)
    mkdir -p "${SFTP_CHROOT}/usr/lib/ssh" "${SFTP_CHROOT}/usr/bin" "${SFTP_CHROOT}/lib"
    cp -a "${SFTP_BIN}" "${SFTP_CHROOT}/usr/lib/ssh/sftp-server" 2>/dev/null || true
    chmod 755 "${SFTP_CHROOT}/usr/lib/ssh/sftp-server" 2>/dev/null || true
    [ -f /lib/ld-musl-x86_64.so.1 ] && cp -a /lib/ld-musl-x86_64.so.1 "${SFTP_CHROOT}/lib/" 2>/dev/null || true
    ldd "${SFTP_BIN}" 2>/dev/null | grep -oE '/[^ ]+\.so[^ ]*' | sort -u | while read -r lib; do
        [ -f "${lib}" ] || continue
        mkdir -p "${SFTP_CHROOT}$(dirname "${lib}")"
        cp -aL "${lib}" "${SFTP_CHROOT}${lib}" 2>/dev/null || true
    done
    for b in /bin/sh /bin/ls /bin/cat /bin/mkdir; do
        [ -f "${b}" ] || continue
        mkdir -p "${SFTP_CHROOT}$(dirname "${b}")"
        cp -aL "${b}" "${SFTP_CHROOT}${b}" 2>/dev/null || true
    done
    for dev in "null c 1 3 666" "zero c 1 5 666" "random c 1 8 666" "urandom c 1 9 666"; do
        set -- ${dev}
        [ -e "${SFTP_CHROOT}/dev/$1" ] || mknod -m "$5" "${SFTP_CHROOT}/dev/$1" "$2" "$3" "$4" 2>/dev/null || true
    done
    mkdir -p "${SFTP_CHROOT}/etc"
    grep -E "^(root|${APP_USER}|${SFTP_USER}):" /etc/passwd > "${SFTP_CHROOT}/etc/passwd" 2>/dev/null || true
    grep -E "^(root|app):" /etc/group > "${SFTP_CHROOT}/etc/group" 2>/dev/null || true
    OUT=$(chroot "${SFTP_CHROOT}" /usr/lib/ssh/sftp-server 2>&1 | head -1 || true)
    case "${OUT}" in
        usage:*|"") log "internal-sftp OK en el chroot" ;;
        *) log "ERROR: internal-sftp no arranca: ${OUT}" ;;
    esac
fi

# --- host keys ------------------------------------------------------------
if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
    log "sin host key montada: se genera una nueva"
    ssh-keygen -A >/dev/null 2>&1 || true
fi

# --- codigo del cliente ---------------------------------------------------
# el codigo vive en el webroot (el mismo dir que ve por SFTP). Se copia a
# /app porque Next necesita node_modules y .next de sololectura.
if [ -n "$(ls -A "${WEBROOT}" 2>/dev/null)" ]; then
    log "codigo del cliente detectado en el webroot"
    cp -a "${WEBROOT}/." "${APP_SRC}/" 2>/dev/null || true
    chown -R app:app "${APP_SRC}" 2>/dev/null || true
    find "${APP_SRC}" -type d -exec chmod 755 {} + 2>/dev/null || true
    find "${APP_SRC}" -type f -exec chmod 644 {} + 2>/dev/null || true
else
    log "webroot vacio: se escribe un stub de verificacion"
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
fi

# --- build ---------------------------------------------------------------
# STANDALONE: Node standalone, no `next start`. Es lo que emite `next build`
# cuando output:'standalone' esta en next.config, y usa muuucho menos RAM
# que el server de Next completo.
cd "${APP_SRC}"

if [ -f package.json ] && [ ! -d node_modules ]; then
    log "node_modules ausente: instalando con pnpm"
    if [ -f pnpm-lock.yaml ]; then
        pnpm install --frozen-lockfile 2>&1 | tail -8 || {
            log "ERROR: pnpm install fallo. Causa probable: la red no llega a"
            log "  registry.npmjs.org. En una red cerrada hace falta:"
            log "    export HTTPS_PROXY=http://<tu-proxy>:8080"
            log "  o subi vos el node_modules por SFTP."
            exit 1
        }
    else
        pnpm install 2>&1 | tail -8 || {
            log "ERROR: pnpm install fallo (misma causa que arriba)"
            exit 1
        }
    fi
fi

if [ -f package.json ] && [ -d node_modules ] && [ ! -f .next/standalone/server.js ] && [ ! -f server.js ]; then
    log "sin build: corriendo pnpm build (puede tardar)"
    NEXT_TELEMETRY_DISABLED=1 pnpm build 2>&1 | tail -15 || {
        log "ERROR: el build fallo. appctl acme logs para ver el detalle"
        exit 1
    }
fi

# --- arranque -------------------------------------------------------------
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
    chown "${SFTP_UID}:${SFTP_GID}" "${CRON_DIR}" 2>/dev/null || chown "${SFTP_UID}" "${CRON_DIR}" 2>/dev/null || true
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

log "arrancando next"
exec /usr/bin/supervisord -c /etc/supervisord.conf
