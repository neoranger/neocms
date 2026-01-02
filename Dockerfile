# Usamos una imagen ligera de Python
FROM python:3.11-slim

# Evita que Python genere archivos .pyc y permite ver logs en tiempo real
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

WORKDIR /app

# Instalamos dependencias
COPY  requeriments.txt .
RUN pip install --no-cache-dir -r requeriments.txt

# Copiamos el resto del código
COPY . .

# Exponemos el puerto de Flask
EXPOSE 5000

CMD ["python", "app.py"]
