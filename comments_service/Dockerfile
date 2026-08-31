FROM python:3.11-slim
WORKDIR /app
COPY requeriments.txt .
RUN pip install --no-cache-dir -r requeriments.txt
COPY . .

# CAMBIO SEGURIDAD: Usuario no-root (el entrypoint degrada tras ajustar permisos)
RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --ingroup appgroup appuser && \
    chown -R appuser:appgroup /app
ENV HOME=/tmp

EXPOSE 5001
# El entrypoint ajusta permisos de los volúmenes y luego corre como appuser
ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "1", "--timeout", "60", "comments:app"]
