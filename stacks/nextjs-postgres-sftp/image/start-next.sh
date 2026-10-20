#!/bin/sh
# Arranque de Next.
#
# Orden:
#   1. standalone (lo ideal): /app/server.js emitido por `next build` con
#      output:'standalone' -> el server minimo, la mitad de RAM.
#   2. `next start` si no hay standalone (el entrypoint ya avisa).
#   3. nada: error explicito en el log.
#
# OJO con el stub: un proyecto sin app deja un /app/server.js de verificacion.
# Si el cliente sube su app y el stub sobrevive, este script arrancaria el
# STUB creyendo que es un build standalone. Por eso se mira la marca.
set -e
cd /app
PORT="${PORT:-3000}"

ES_STUB=""
if [ -f server.js ] && head -n5 server.js 2>/dev/null | grep -q "stub de verificacion"; then
    ES_STUB="si"
fi

if [ -f server.js ] && [ -z "$ES_STUB" ]; then
    exec /usr/bin/node server.js
fi
if [ -x node_modules/.bin/next ]; then
    echo "[next] sin standalone: arranco con next start (mas RAM)."
    echo "[next] para el modo liviano: output:'standalone' en next.config"
    exec node_modules/.bin/next start -H 127.0.0.1 -p "$PORT"
fi
if [ -n "$ES_STUB" ]; then
    # Sin next instalado y con el stub: se sirve el stub (proyecto vacio).
    echo "[next] sin app propia: sirvo el stub de verificacion"
    exec /usr/bin/node server.js
fi
echo "[next] no hay server.js ni next instalado: revisa el build (appctl <proyecto> logs)"
exit 1
