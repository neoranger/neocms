# NeoCMS 🚀
NeoCMS es un sistema de gestión de contenidos (CMS) ligero, privado y ultra-rápido, diseñado para blogs personales que priorizan la simplicidad y el rendimiento. Construido con Python (Flask), utiliza archivos Markdown como base de datos y Docker para un despliegue sin fricciones.

✨ Características principales
- Markdown-Based: Escribí tus posts en Markdown con soporte para Frontmatter (metadatos como títulos, fechas, tags y categorías).
- Sistema de Borradores (Drafts): Guardá posts en modo borrador; solo serán visibles en el blog cuando decidas publicarlos.
- Analíticas Privadas: Panel de estadísticas integrado que muestra visitas de los últimos 7 días con gráficos auto-escalables.
- Feed RSS 2.0: Generación automática de feed para lectores de noticias, incluyendo el contenido completo de los artículos.
- SEO & Social: Metadatos automáticos para Twitter Cards y Open Graph, incluyendo descripción y tiempo estimado de lectura.
- Interfaz Adaptativa: Diseño limpio con modo oscuro automático, sidebar de categorías/tags y buscador integrado.
- Dockerized: Listo para desplegar en cualquier servidor con un solo comando.

🛠️ Tecnologías utilizadas
- Backend: Python 3.x + Flask
- Frontend: Jinja2 Templates, CSS3 (Custom Variables)
- Datos: Python-Frontmatter (Markdown), JSON (Stats)
- Despliegue: Docker & Docker Compose

### 🚀 Instalación y Despliegue
**Requisitos previos**

**Docker y Docker Compose** instalados.

#### Pasos para el despliegue
Cloná el repositorio:

```
git clone https://github.com/tu-usuario/neocms.git
cd neocms
```

Configurá las credenciales: Editá el archivo **app.py** para cambiar las credenciales de acceso al Panel Admin (si usas autenticación básica).
Levantá el contenedor:

```
docker compose up -d --build
```

El sitio estará disponible en http://localhost:5000.

📁 Estructura del Proyecto
- /posts: Carpeta donde se almacenan los archivos .md. Los archivos que comienzan con draft_ no se muestran al público.
- /static: Archivos CSS, imágenes y recursos del frontend.
- /templates: Plantillas HTML (Index, Post, Admin, Editor).
- app.py: Lógica principal del servidor y gestión de estadísticas.
- stats.json: Registro de visitas (persitente mediante volúmenes de Docker). **Crealo antes de levnatar el contenedor**

📊 Estadísticas y Persistencia
El sistema registra visitas únicas diarias y totales. Gracias al uso de Docker Volumes, los datos de visitas y los posts se mantienen a salvo aunque el contenedor se reinicie o se actualice.

### Sugerencias:
Creá los directorios antes para que tengan los permisos adecuados, así como el archivo stats.json para que docker lo interprete como archivo y no como directorio.
