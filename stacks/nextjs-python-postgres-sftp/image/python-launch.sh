#!/bin/sh
# Arranque de la API Python (uvicorn + FastAPI).
#
# Corre como usuario `app` bajo supervisord, con el venv de la imagen
# (/opt/venv): ahi estan fastapi/uvicorn y lo que haya instalado el
# requirements.txt del cliente.
#
# El TARGET se deduce de lo que subio el cliente, para no obligarlo a una
# estructura unica:
#   backend/main.py      -> main:app
#   backend/app/main.py  -> app.main:app   (layout de paquete)
# Si no hay ninguno de los dos, corre el stub (el entrypoint siempre deja
# uno escrito, asi que el archivo existe).
set -e

cd /app/backend
PORT="${PY_PORT:-8000}"

if [ -f main.py ]; then
    TARGET="main:app"
elif [ -f app/main.py ]; then
    TARGET="app.main:app"
else
    TARGET="main:app"
fi

echo "[python] uvicorn ${TARGET} en 127.0.0.1:${PORT} (cwd $(pwd))"
# --proxy-headers: nginx esta adelante y manda X-Forwarded-*; sin esto la
# app ve la IP del proxy como cliente. No se confia en X-Forwarded-For de
# afuera: --forwarded-allow-ips queda en su default (127.0.0.1), que es
# justo nginx en este contenedor.
exec /opt/venv/bin/uvicorn "${TARGET}" \
    --host 127.0.0.1 --port "${PORT}" \
    --proxy-headers --log-level info
