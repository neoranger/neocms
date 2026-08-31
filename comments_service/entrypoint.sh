#!/bin/sh
set -e

# Ajustar permisos de los volúmenes montados para que el usuario no-root
# (appuser) pueda leer/escribir. Esta parte corre como root en el entrypoint.
chown -R appuser:appgroup /app/data /app/.totp_secret 2>/dev/null || true

# Degradar a appuser (setpriv viene con util-linux) y ejecutar el comando
exec setpriv --reuid=appuser --regid=appgroup --init-groups "$@"
