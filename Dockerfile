# Usamos una imagen ligera de Python
FROM python:3.11-slim

# Evita que Python genere archivos .pyc y permite ver logs en tiempo real
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

WORKDIR /app

# Instalamos dependencias
COPY  requeriments.txt .
RUN pip install --no-cache-dir -r requeriments.txt

# Copiamos el resto del código (los secretos quedan fuera vía .dockerignore)
COPY . .

# CAMBIO SEGURIDAD: Usuario no-root (el entrypoint degrada tras ajustar permisos)
RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --ingroup appgroup appuser && \
    mkdir -p /app/content /app/static/uploads && \
    chown -R appuser:appgroup /app
# El control server de gunicorn y sus archivos temporales van a /tmp (tmpfs writable)
ENV HOME=/tmp

# Exponemos el puerto de Flask
EXPOSE 5000

# El entrypoint ajusta permisos de los volúmenes y luego corre como appuser
ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]

# Servidor WSGI de producción (sin el debugger de desarrollo de Flask)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "60", "app:app"]

