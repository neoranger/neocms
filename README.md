# NeoCMS 🚀

**NeoCMS** es un motor de blog ligero, rápido y moderno construido en Python con Flask. Diseñado para ser minimalista pero potente, cuenta con gestión de contenido basada en Markdown, un panel de administración seguro y una arquitectura de microservicios para la gestión de comentarios.

![Python](https://img.shields.io/badge/Python-3.11-blue?style=flat&logo=python)
![Docker](https://img.shields.io/badge/Docker-Enabled-blue?style=flat&logo=docker)
![Status](https://img.shields.io/badge/Status-Active-success)

## ✨ Características Principales

* **📝 Redacción en Markdown:** Escribe tus posts en archivos `.md` simples con metadatos YAML (Frontmatter).
* **💬 Sistema de Comentarios (Microservicio):**
    * Arquitectura separada en contenedor propio para mayor rendimiento.
    * Moderación previa (los comentarios requieren aprobación).
    * Diseño moderno tipo "burbuja".
* **🔔 Notificaciones en Tiempo Real:** Integración con **Telegram Bot** para avisar al administrador cuando llega un nuevo comentario.
* **📊 Estadísticas Persistentes:**
    * Registro de visitas totales históricas y diarias.
    * Gráficas de los últimos 7 días en el panel de administración.
    * Datos persistentes ante reinicios de contenedores.
* **🛡️ Panel de Administración:**
    * Editor de posts integrado.
    * Gestión de subida de imágenes.
    * Aprobación/Eliminado de comentarios.
    * Toggle global para activar/desactivar comentarios.

## 🛠️ Arquitectura

El proyecto utiliza **Docker Compose** para orquestar dos servicios principales:

1.  **CMS (Web):** La aplicación principal que sirve el blog y el admin (`app.py`).
2.  **Comments Service:** Microservicio dedicado a recibir y guardar comentarios (`comments_service/comments.py`).

Ambos comparten volúmenes de datos para persistencia y configuración.

## 🚀 Instalación y Despliegue

### Requisitos Previos
* Docker y Docker Compose instalados.
* (Opcional) Nginx Proxy Manager para gestión de SSL/Dominios.

### 1. Clonar el Repositorio
```
git clone [https://github.com/tu-usuario/neocms.git](https://github.com/tu-usuario/neocms.git)
cd neocms
```
### 2. Configuración de Entorno (.env)
Crea un archivo .env en la raíz del proyecto. Este archivo no se sube al repositorio por seguridad.
```
# Configuración General
SECRET_KEY=una_clave_muy_segura_y_larga
ADMIN_PASSWORD=tu_password_para_el_admin
FLASK_ENV=production

# Configuración de Telegram (Notificaciones)
TELEGRAM_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_CHAT_ID=123456789
```

### 3. Inicializar Archivos de Datos
Para evitar errores en el primer arranque, asegúrate de crear el archivo de estadísticas inicial en la raíz:
```
echo '{ "total": 0, "daily": {}, "posts": {} }' > stats.json
```

### 4. Ejecutar con Docker Compose
Construye y levanta los contenedores:
```
docker-compose up --build -d
```

### El sitio estará disponible en:

- Blog: http://localhost:5000 (o el puerto configurado).
- API Comentarios: Internamente en puerto 5001.

### 📂 Estructura del Proyecto

```
neocms/
├── content/              # Tus posts en formato Markdown (.md)
├── static/
│   ├── uploads/          # Imágenes subidas desde el admin
│   └── css/              # Estilos (style.css)
├── templates/            # Plantillas HTML (Jinja2)
├── comments_service/     # Carpeta del microservicio
│   └── comments.py       # Lógica de la API de comentarios
├── comments_data/        # Persistencia de comentarios (JSONs)
├── app.py                # Aplicación principal Flask
├── Dockerfile            # Definición de imagen (compartida)
├── docker-compose.yml    # Orquestación de servicios
├── requirements.txt      # Dependencias Python
└── stats.json            # Base de datos de visitas (Persistente)
```

### 🤖 Uso del Bot de Telegram
- Crea un bot con @BotFather en Telegram para obtener tu TELEGRAM_TOKEN.
- Obtén tu ID de usuario con @userinfobot para el TELEGRAM_CHAT_ID.
- Agrega estas variables al .env.
- ¡Listo! Recibirás un mensaje cada vez que alguien comente en tu blog.

### 🛡️ Copias de Seguridad (Backup)
Los datos importantes residen en carpetas mapeadas como volúmenes. Para hacer un backup completo, guarda:
- Carpeta content/
- Carpeta comments_data/
- Carpeta static/uploads/
- Archivo stats.csv
- Archivo .env

---
Desarrollado con ❤️ usando Flask & Docker.

