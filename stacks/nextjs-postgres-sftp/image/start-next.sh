#!/bin/sh
# Arranque de Next.
#
# STANDALONE (lo ideal): el build emitio /app/server.js y se corre ese, que es
# el server minimo (la mitad de RAM). `next build` lo emite solo cuando
# next.config tiene output:'standalone'.
#
# SIN STANDALONE: si el cliente no lo puso, en vez de dejar el contenedor en
# loop de reinicios se arranca con `next start` (mas RAM, pero anda). El
# entrypoint ya avisa en el log que falta la opcion.
set -e
cd /app
PORT="${PORT:-3000}"
if [ -f server.js ]; then
    exec /usr/bin/node server.js
fi
if [ -x node_modules/.bin/next ]; then
    echo "[next] sin standalone: arranco con next start (mas RAM)."
    echo "[next] para el modo liviano: output:'standalone' en next.config"
    exec node_modules/.bin/next start -H 127.0.0.1 -p "$PORT"
fi
echo "[next] no hay server.js ni next instalado: revisa el build (appctl <proyecto> logs)"
exit 1
