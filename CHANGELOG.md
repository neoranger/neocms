# Changelog

Todas las cambios relevantes de NeoCMS se documentan en este archivo.

## [v1.2] — 2026-11-10

Esta versión agrega la barra de compartir en las entradas y la publicación automática en el Fediverso desde el panel de administración.

### 🆕 Agregado

- **Barra de compartir en las entradas** con Mastodon, Diaspora, Bluesky, X, Telegram, WhatsApp, Reddit y copia del enlace. Incluye diálogo para elegir la instancia de Mastodon o Diaspora y botón nativo cuando el navegador lo soporta.
- **Metadatos sociales** en las entradas: Open Graph, Twitter Card, `canonical`, etiquetas y tiempo de lectura.
- **Publicación automática en el Fediverso** para instancias Mastodon-compatibles (Mastodon, Pleroma, Akkoma, GoToSocial):
  - Pantalla `/admin/fediverse` para conectar la cuenta con un token manual.
  - Conexión opcional con **usuario y contraseña** mediante `grant_type=password`, con registro automático de la app si faltan las credenciales de cliente.
  - Soporte para verificación en dos pasos (2FA), credenciales inválidas, método no soportado e instancia caída.
  - Verificación de credenciales y consulta del límite de caracteres de la instancia.
  - Opciones de visibilidad, idioma, hashtags y activación de la publicación automática.
  - Publicación al pasar una entrada de borrador a pública, con texto de título, enlace y hashtags.
  - Botón manual para publicar entradas viejas o reintentar fallos.
  - Checkbox por entrada para excluirla del Fediverso.
  - Estado de la publicación en la lista de entradas y enlace al estado desde el editor.

### 🔧 Cambiado

- El editor ahora conserva todos los metadatos existentes del frontmatter al guardar; antes se perdían campos como `image`.
- El panel de administración muestra mensajes flash de éxito y error.
- La barra de compartir usa una grilla responsive de celdas iguales, alineada con el ancho del texto del post.
- Se retiró la opción de compartir por Email.

### 🔒 Seguridad

- El token del Fediverso se guarda en `data/fediverse.json` con permisos `0600`, fuera de Git y del `config.json` versionado.
- El token nunca se envía al HTML: solo se muestra enmascarado y se reemplaza si se escribe uno nuevo.
- La contraseña del método usuario/contraseña se usa solo para obtener el token: no se guarda, no se muestra y no se registra en logs. Si la conexión falla, se conserva la configuración y el token anteriores.
- Se valida la URL de la instancia y se rechazan credenciales embebidas, esquemas inseguros y direcciones privadas.

### 🚀 Notas de despliegue

- Reconstruir la imagen para tomar los templates, CSS y rutas nuevas:

  ```bash
  docker compose up -d --build web
  ```

- Crear el token en la instancia con el permiso `write:statuses`.
- No hay migraciones de base de datos.

## [v1.1] — 2026-11-02

**Comparación:** [`v1.0...main`](https://github.com/neoranger/neocms/compare/v1.0...main)

Esta versión renueva por completo el panel de administración, incorpora páginas estáticas y un sistema de temas, y corrige varios problemas del editor y de la exportación de estadísticas.

### ✨ Destacados

- **Nuevo panel de administración tipo WordPress:** sidebar fija a la izquierda, navegación agrupada, topbar y contenido a la derecha.
- **Diseño responsive:** en pantallas chicas el menú se convierte en un drawer con overlay.
- **Pantalla de Entradas independiente** en `/admin/posts`, con el Escritorio mostrando solo los últimos artículos.
- **Páginas estáticas** con editor Markdown, borradores, descripción SEO y opción de mostrarlas en la navbar.
- **Sistema de temas y apariencia:** temas `default`, `minimal` y `claro`, selección de tema activo, colores principales y CSS personalizado.
- **Soporte para contenido embebido:** iframes de YouTube y NeoCast habilitados de forma controlada.
- **Exportación CSV de estadísticas corregida**, con soporte para acentos en Excel.

### 🆕 Agregado

- Gestión completa de páginas:
  - Crear, editar y eliminar páginas.
  - Soporte para borradores.
  - Slug personalizado.
  - Descripción SEO.
  - Opción “mostrar en navbar”.
  - Ruta pública `/p/<slug>`.
- Panel de Apariencia:
  - Selección de tema.
  - Configuración del título del sitio.
  - Variables de color (`accent-primary`, `bg-main`, `text-primary`).
  - Editor de CSS personalizado.
- Nuevos temas: `default`, `minimal` y `claro`.
- Nueva pantalla `/admin/posts`.
- Layout de administración con:
  - Sidebar con secciones Principal, Comunidad, Diseño, Analítica y Herramientas.
  - Estado activo según la sección.
  - Botón “Nueva entrada”.
  - Acceso directo a Backup, CSV, “Ver sitio” y cierre de sesión.
  - Topbar con título de sección.
- Soporte de `iframe` en el sanitizador de Markdown y en la CSP para NeoCast y YouTube.
- Exportación CSV con BOM UTF-8 y nombre de archivo con fecha (`stats-YYYY-MM-DD.csv`).
- Endpoint `/theme/generated.css` para servir los colores y el CSS personalizado desde `config.json`.

### 🔧 Cambiado

- Todas las pantallas del admin usan ahora un layout propio, sin el header, banner ni footer del sitio público.
- El Escritorio ya no muestra la tabla completa de artículos: muestra los últimos 5 y deriva a `/admin/posts`.
- Al guardar o eliminar un post se redirige a la pantalla de Entradas.
- El editor guarda los archivos Markdown en modo binario para preservar correctamente el frontmatter.
- La CSP permite los estilos de EasyMDE desde `cdn.jsdelivr.net`.
- El enlace de Mastodon se actualizó a `social.neosite.com.ar`.
- `config.json` ahora se carga con valores por defecto y soporta las nuevas claves de tema sin romper configuraciones existentes.
- La personalización de temas ya no escribe archivos dentro de `static/themes`; se sirve dinámicamente desde la configuración.

### 🐛 Corregido

- **Exportación CSV:** se reemplazó `BytesIO` por `StringIO`, que era la causa del error `TypeError: a bytes-like object is required, not 'str'`.
- La conexión SQLite de la exportación ahora se cierra siempre.
- Si la exportación falla de verdad, devuelve `HTTP 500` en lugar de un texto plano con estado `200`.
- El botón “Nueva entrada” ya no se desborda fuera del sidebar (`box-sizing: border-box`).
- Se corrigió la carga de estilos del editor bloqueada por la CSP.
- Se corrigió un punto y coma faltante en la directiva `connect-src` de la CSP.
- El CSS personalizado y las variables de color ahora funcionan con el contenedor `read_only: true` de producción.

### 🚀 Notas de despliegue

- Reconstruir la imagen es obligatorio porque hay templates, CSS y temas nuevos:

  ```bash
  docker compose build --no-cache web
  docker compose up -d
  ```

- No hay migraciones de base de datos. La base SQLite de estadísticas y los datos existentes se mantienen.
- El directorio `content/pages/` se crea automáticamente dentro del volumen `content`, por lo que las páginas nuevas persisten.
- `config.json` debe seguir siendo escribible para guardar el tema activo, los colores y el CSS personalizado.
- Los colores y el CSS personalizado se sirven desde `/theme/generated.css`, por lo que no requieren volúmenes adicionales ni permisos de escritura sobre `static/themes`.

### 📦 Commits incluidos

- `25b652b` — Nuevo panel de administración con sidebar y corrección de exportación CSV.
- `e2e8bf1` — Páginas estáticas y sistema de temas con CSS personalizado.
- `589c7c3` — Corrección de CSP.
- `e374021` — Habilitación de iframes para contenido embebido.
- `fa5ec41` — Corrección del guardado del editor y enlace de Mastodon.
- `95dbc6a` — Corrección de estilos del editor en la CSP.

## [v1.0] — 2026-09-01

Release base anterior a este changelog.
