#!/bin/sh
# Stack nextjs-python: DOS runtimes adentro del mismo contenedor, detras de
# un solo nginx y un solo puerto.
#
#   /api/  -> 127.0.0.1:8000   uvicorn + FastAPI
#   resto  -> 127.0.0.1:3000   Next.js standalone
#
# El cliente sube CODIGO FUENTE por SFTP al webroot (que es el chroot):
#   /upload/...            -> la app de Next (package.json, app/, etc)
#   /upload/backend/...    -> la API (main.py + requirements.txt)
#
# Decisiones de arranque, iguales que en el stack de Next solo:
#   - si hay node_modules, asumimos que el cliente ya instalo y buildeo
#   - si no hay, buildeamos nosotros
#   - si no hay backend, se escribe un stub de FastAPI para que el proyecto
#     recien creado arranque y se pueda verificar sin subir nada
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
PY_SRC="/app/backend"
VENV="/opt/venv"

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

# --- nginx: reverse proxy a los dos runtimes ------------------------------
# La imagen base trae un default.conf que sirve ARCHIVOS ESTATICOS de php
# (root /var/www/html, index index.php, try_files =404). Con ese config ni
# node ni uvicorn reciben nada: /healthz daba 403, el healthcheck fallaba y
# el contenedor queda unhealthy para siempre, mientras el stub (node)
# escuchaba en 3000 y el log de nginx mostraba un 200 de otra ruta.
#
# O sea: el bug no era node, era nginx sirviendo php.
if [ -f /usr/local/share/appctl/nginx.conf ]; then
    cp /usr/local/share/appctl/nginx.conf /etc/nginx/nginx.conf
    log "nginx.conf instalado (proxy a 127.0.0.1:3000 y /api a :8000)"
else
    log "AVISO: no encontro /usr/local/share/appctl/nginx.conf: el healthcheck puede fallar"
fi
rm -f /etc/nginx/conf.d/default.conf 2>/dev/null || true

# --- directorios ----------------------------------------------------------
mkdir -p "${WEBROOT}" "${APP_SRC}" "${PY_SRC}" \
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
# codigo. 700 (no 755): nginx aca no sirve archivos, es proxy — nada mas
# que el usuario SFTP tiene por que leer esto. Se copia a /app, y ES /app
# lo que leen node y uvicorn.
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
# primer login: cambiar la password fuerza cambio, y esto es un server
# unattended -> la marco como ya usada
chage -E -1 -I -1 -m 0 -M 99999 "${SFTP_USER}" 2>/dev/null || true

# --- sshd_config: sustituir los placeholders ------------------------------
# El baked-in trae AllowUsers __SFTP_USER__ y ChrootDirectory __SFTP_HOME__.
# Con esos literales, sshd rechaza a TODOS: el unico usuario permitido se
# llama "__SFTP_USER__", que no existe.
sed -i.bak \
    -e "s|__SFTP_USER__|${SFTP_USER}|g" \
    -e "s|__SFTP_HOME__|${SFTP_CHROOT}|g" \
    /etc/ssh/sshd_config
# -d /upload: el cliente arranca parado en su codigo, con /private al lado.
sed -i "s|^ForceCommand.*|ForceCommand internal-sftp -d /upload|" /etc/ssh/sshd_config
log "sshd: AllowUsers=${SFTP_USER} ChrootDirectory=${SFTP_CHROOT}"
# Si esto falla, el cliente no puede entrar por SFTP y su error va a ser
# "Permission denied" sin pista. Mejor que falle el arranque, aca.
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
    cp -a "${WEBROOT}/." "${APP_SRC}/" 2>/dev/null || true
fi
chown -R app:app "${APP_SRC}" 2>/dev/null || true
find "${APP_SRC}" -type d -exec chmod 755 {} + 2>/dev/null || true
find "${APP_SRC}" -type f -exec chmod 644 {} + 2>/dev/null || true

# --- stub de Next ---------------------------------------------------------
# Se escribe SOLO si el cliente no subio una app de Next. Antes el stub
# dependia de que el webroot estuviera vacio: un proyecto que usaba
# solamente la API python dejaba a Next sin server.js y el contenedor
# quedaba unhealthy con la API perfecta.
if [ -f "${APP_SRC}/package.json" ] || [ -f "${APP_SRC}/server.js" ] || \
   [ -f "${APP_SRC}/.next/standalone/server.js" ]; then
    log "next: app del cliente detectada"
else
    log "next: sin app propia, se escribe el stub de verificacion"
    cat > "${APP_SRC}/server.js" <<'STUB'
// stub de verificacion. El cliente lo borra (o lo pisa) al subir su app.
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

# --- API Python -----------------------------------------------------------
# El backend vive en /app/backend (el cliente lo sube a /upload/backend).
# Las deps van al venv de la imagen, NO al python del sistema: Alpine marca
# el suyo como "externally managed" (PEP 668) y el pip a secas falla con
# "error: externally-managed-environment".
mkdir -p "${PY_SRC}"
if [ -f "${PY_SRC}/requirements.txt" ]; then
    log "python: instalando backend/requirements.txt (puede tardar)"
    # El rc se mira DE VERDAD: con `pip ... | tail -8 || { ... }` el que
    # define el exit code es tail, que sale 0, y un pip fallido pasaba por
    # bueno. El log va a un archivo y se muestra al final.
    if ! "${VENV}/bin/pip" install --no-cache-dir --disable-pip-version-check \
            -r "${PY_SRC}/requirements.txt" > /tmp/pip-install.log 2>&1; then
        tail -20 /tmp/pip-install.log
        log "ERROR: 'pip install -r requirements.txt' fallo."
        log "  Causa mas comun: la red no llega a pypi.org. En una red"
        log "  cerrada hace falta HTTPS_PROXY, o subir las deps ya"
        log "  instaladas (wheelhouse) junto al codigo."
        exit 1
    fi
    tail -3 /tmp/pip-install.log
else
    log "python: sin backend/requirements.txt (usa lo que trae la imagen)"
fi
# Stub de FastAPI: mismo criterio que el de Next. Responde /healthz (lo usa
# el healthcheck del contenedor a traves de /api/healthz) y / con el estado
# de la DB, para que un proyecto recien creado se pueda verificar sin que
# el cliente suba nada.
if [ ! -f "${PY_SRC}/main.py" ] && [ ! -f "${PY_SRC}/app/main.py" ]; then
    log "python: sin API propia, se escribe el stub de verificacion"
    cat > "${PY_SRC}/main.py" <<'STUBPY'
# stub de verificacion de la API. El cliente lo pisa (o lo borra) al subir
# su backend: appctl <proyecto> logs --service app
#
# Convencion de arranque: uvicorn busca `main:app` (este archivo) o
# `app.main:app` (un paquete app/). El launcher (python-launch.sh) prueba
# las dos.
import os
import platform
import socket
import sys

import fastapi
import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

DB_HOST = os.environ.get("DB_HOST", "db")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))


def db_txt() -> str:
    """Hay algo escuchando en el puerto de la DB del proyecto.

    Es un connect TCP, igual que el stub de Next: prueba la RED interna del
    stack, no las credenciales (para eso estan los usuarios del resumen).
    """
    s = socket.socket()
    s.settimeout(4)
    try:
        s.connect((DB_HOST, DB_PORT))
        return f"host {DB_HOST}:{DB_PORT} alcanzable"
    except OSError as e:
        return f"no conecta a {DB_HOST}:{DB_PORT}: {e}"
    finally:
        s.close()


# docs_url/redoc_url en None: un stub de verificacion no tiene por que
# publicar una consola de documentacion de la API.
app = FastAPI(title="appctl stub", docs_url=None, redoc_url=None)


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok\n"


@app.get("/", response_class=PlainTextResponse)
def raiz() -> str:
    return "\n".join([
        "app: python ok",
        f"python: {sys.version.split()[0]}",
        f"fastapi: {fastapi.__version__}",
        f"uvicorn: {uvicorn.__version__}",
        f"os: {platform.system()} {platform.release()}",
        f"db: {db_txt()}",
        "",
    ])
STUBPY
fi
chown -R app:app "${PY_SRC}" 2>/dev/null || true
find "${PY_SRC}" -type f -exec chmod 644 {} + 2>/dev/null || true

# --- build de Next --------------------------------------------------------
# STANDALONE: Node standalone, no `next start`. Es lo que emite `next build`
# cuando output:'standalone' esta en next.config, y usa muuucho menos RAM
# que el server de Next completo.
cd "${APP_SRC}"

if [ -f package.json ] && [ ! -d node_modules ]; then
    log "node_modules ausente: instalando con pnpm"
    if [ -f pnpm-lock.yaml ]; then
        if ! pnpm install --frozen-lockfile > /tmp/pnpm-install.log 2>&1; then
            tail -20 /tmp/pnpm-install.log
            log "ERROR: pnpm install fallo. Causa probable: la red no llega"
            log "  a registry.npmjs.org. En una red cerrada hace falta:"
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

if [ -f package.json ] && [ -d node_modules ] && [ ! -f server.js ] && \
   [ ! -f .next/standalone/server.js ]; then
    log "sin build: corriendo pnpm build (puede tardar)"
    if ! NEXT_TELEMETRY_DISABLED=1 pnpm build > /tmp/pnpm-build.log 2>&1; then
        tail -25 /tmp/pnpm-build.log
        log "ERROR: el build fallo. appctl <proyecto> logs para el detalle"
        exit 1
    fi
    tail -3 /tmp/pnpm-build.log
fi

# --- standalone -> /app ---------------------------------------------------
# `next build` con output:'standalone' emite su propio server.js + un
# node_modules minimo en .next/standalone/. Lo que arranca supervisord es
# /app/server.js: ESE arbol, copiado un nivel arriba.
# Los estaticos NO vienen en el standalone (los sirve nginx en un deploy
# normal con `output: standalone`): sin copiarlos a donde el server los
# busca, el sitio carga sin CSS ni JS. Se copian .next/static y public.
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

# --- cron -----------------------------------------------------------------
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

log "arrancando nginx + next + uvicorn + sshd"
exec /usr/bin/supervisord -c /etc/supervisord.conf
