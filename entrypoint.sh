#!/bin/sh
set -e

# Ajustar permisos de los volúmenes montados para que el usuario no-root
# (appuser) pueda leer/escribir. Esta parte corre como root en el entrypoint.
chown -R appuser:appgroup \
    /app/content \
    /app/static/uploads \
    /app/comments_data \
    /app/config.json \
    /app/stats.csv \
    /app/stats.lock \
    /app/totp \
    2>/dev/null || true

# Degradar a appuser (setpriv viene con util-linux) y ejecutar el comando
exec setpriv --reuid=appuser --regid=appgroup --init-groups "$@"
