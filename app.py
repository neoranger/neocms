import os
import sys
import frontmatter
from flask import Flask, render_template, abort, request, redirect, url_for, session, Response, g, make_response, send_file, flash
from functools import wraps
from markdown import markdown
import re
import html
import hashlib
import ipaddress
import unicodedata
from urllib.parse import quote, urlsplit, urlunsplit
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from datetime import datetime, timedelta
import zipfile
import json
import glob
from dotenv import load_dotenv
import sqlite3
import uuid
import requests
from collections import Counter
import pyotp
import qrcode
from io import StringIO
import base64
import bleach
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Cargar .env PRIMERO, antes de leer cualquier variable de entorno de configuración
# (SECRET_KEY, COOKIE_SECURE, TWO_FA_ENABLED, etc.).
load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TOTP_DIR = os.path.join(BASE_DIR, 'totp')
os.makedirs(TOTP_DIR, exist_ok=True)
TOTP_FILE = os.path.join(TOTP_DIR, '.totp_secret')

# CAMBIO 1: Base de datos SQLite para estadísticas (reemplaza al antiguo CSV)
# Vive en un SUBDIRECTORIO escribible (data/) para que SQLite pueda crear junto al
# archivo sus journals auxiliares (-journal/-wal/-shm) aunque /app sea read-only.
DB_DIR = os.path.join(BASE_DIR, 'data')
DB_FILE = os.path.join(DB_DIR, 'stats.db')


def _init_db():
    """Crea la tabla de visitas si no existe. SQLite maneja el locking por
    transacciones (no se necesita FileLock manual)."""
    # Asegurar que el directorio de la DB exista y sea escribible
    try:
        os.makedirs(DB_DIR, exist_ok=True)
    except Exception as e:
        print(f"ERROR STATS: no se pudo crear {DB_DIR}: {e}")
        return
    # Si el path fue montado como un DIRECTORIO (p. ej. bind de un archivo que no
    # existía al crear el contenedor), SQLite no puede abrirlo. Lo detectamos y
    # logueamos en vez de derribar el arranque.
    if os.path.isdir(DB_FILE):
        print(f"ERROR STATS: {DB_FILE} es un directorio, no un archivo de base de datos.")
        return
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS visits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    action TEXT,
                    detail TEXT,
                    os TEXT,
                    location TEXT,
                    ip TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_detail ON visits(detail)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_timestamp ON visits(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_user ON visits(user_id)")
    except Exception as e:
        print(f"ERROR STATS: no se pudo inicializar la base ({e}).")


# Inicializar la DB al cargar el módulo (gunicorn importa el módulo, no ejecuta __main__)
_init_db()

# CAMBIO: Toggle 2FA. Con '0' el login va directo al admin tras la contraseña;
# con '1' (default) exige el código TOTP. Se controla desde .env sin tocar código.
TWO_FA_ENABLED = os.environ.get('TWO_FA_ENABLED', '1').strip() != '0'

# Directorio donde el microservicio de comentarios guarda los JSON.
# El microservicio usa 'data/comments' montado en ./comments_data:/app/data,
# por lo que en el host los archivos quedan en comments_data/comments/.
COMMENTS_DATA_DIR = 'comments_data/comments/'

app = Flask(__name__)

# CAMBIO SEGURIDAD: No usar secret key por defecto. Si falta, abortar el arranque.
_secret_key = os.environ.get('SECRET_KEY', '').strip()
if not _secret_key:
    sys.exit("ERROR DE SEGURIDAD: La variable de entorno SECRET_KEY es obligatoria. Genera una con: python -c \"import secrets; print(secrets.token_hex(32))\" y colócala en el archivo .env")
app.secret_key = _secret_key

CONTENT_DIR = "content"
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'mp3', 'mp4'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB máx. por request (uploads)

# CAMBIO SEGURIDAD: Cookies de sesión más seguras
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# Secure solo si estamos detrás de TLS
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('COOKIE_SECURE', '').strip() == '1'

# Confiar en headers de proxy (reverse proxy / docker)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# CAMBIO SEGURIDAD: Protección CSRF para los métodos que mutan estado
csrf = CSRFProtect(app)


# Verificación CSRF: si falla, mostrar un mensaje claro en vez de un 400 crudo
@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    return (
        "<html><body style='font-family:sans-serif;text-align:center;padding:40px'>"
        "<h2>Error de validación CSRF</h2>"
        "<p>La solicitud fue rechazada por protección CSRF. "
        "Recargá la página e intentá de nuevo.</p>"
        f"<p><em>{e}</em></p></body></html>",
        400,
    )

# CAMBIO SEGURIDAD: Rate limiting contra fuerza bruta.
# Se aplican límites explícitos a /login y /login/2fa (rutas sensibles).
# No se limita agresivamente el front público para no perjudicar a lectores reales.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    enabled=True,
)

# Niveles/atributos HTML permitidos al sanitizar el contenido markdown y el RSS
ALLOWED_TAGS = (
    'a', 'abbr', 'acronym', 'b', 'blockquote', 'br', 'code', 'dd', 'del', 'div',
    'dl', 'dt', 'em', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'iframe', 'img', 'li',
    'ol', 'p', 'pre', 's', 'span', 'strike', 'strong', 'sub', 'sup', 'table',
    'tbody', 'td', 'th', 'thead', 'tr', 'ul', 'video', 'audio', 'source'
)
ALLOWED_ATTRIBUTES = {
    'a': ['href', 'title', 'target', 'rel'],
    'abbr': ['title'],
    'iframe': ['src', 'width', 'height', 'frameborder', 'scrolling', 'style', 'allow', 'allowfullscreen'],
    'acronym': ['title'],
    'img': ['src', 'alt', 'title', 'width', 'height'],
    'video': ['src', 'controls', 'width', 'height', 'poster'],
    'audio': ['src', 'controls'],
    'source': ['src', 'type'],
    'td': ['align', 'valign'],
    'th': ['align', 'valign'],
    'div': ['align'],
    'span': ['align'],
}
ALLOWED_SCHEMES = ['http', 'https', 'mailto', 'tel', 'data']

# --- FUNCIONES DE SANITIZACIÓN ---

def sanitize_html(html, extra_tags=None, extra_attrs=None):
    """Limpia HTML generado desde markdown o contenido no confiable."""
    allowed_tags = set(ALLOWED_TAGS)
    allowed_attrs = {k: list(v) for k, v in ALLOWED_ATTRIBUTES.items()}
    if extra_tags:
        allowed_tags = allowed_tags.union(extra_tags)
    if extra_attrs:
        for tag, attrs in extra_attrs.items():
            allowed_attrs.setdefault(tag, [])
            allowed_attrs[tag].extend([a for a in attrs if a not in allowed_attrs[tag]])
    return bleach.clean(
        html,
        tags=allowed_tags,
        attributes=allowed_attrs,
        protocols=ALLOWED_SCHEMES,
        strip=True,
        strip_comments=True,
    )

def plain_text_from_html(value, limit=160):
    """Convierte HTML sanitizado en texto plano para descripciones y metadatos."""
    text = re.sub(r'<[^>]+>', ' ', value or '')
    text = html.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    if limit and len(text) > limit:
        cut = text[:limit].rsplit(' ', 1)[0].rstrip(' ,.;:')
        text = (cut or text[:limit]).rstrip(' ,.;:') + '…'
    return text

def build_share_links(post_url, title):
    """Arma los enlaces de compartir para las redes con URL universal.

    Mastodon y Diaspora no se incluyen porque dependen de la instancia
    que elige cada lector; eso se resuelve en el navegador.
    """
    title = str(title or '').strip()
    text = f"{title} {post_url}".strip()
    q = lambda value: quote(str(value), safe='')
    return {
        'bluesky': f"https://bsky.app/intent/compose?text={q(text)}",
        'x': f"https://x.com/intent/tweet?text={q(title)}&url={q(post_url)}",
        'telegram': f"https://t.me/share/url?url={q(post_url)}&text={q(title)}",
        'whatsapp': f"https://wa.me/?text={q(text)}",
        'reddit': f"https://www.reddit.com/submit?url={q(post_url)}&title={q(title)}",
    }

def xml_escape(value):
    """Escapa texto para usarlo de forma segura dentro de XML/RSS."""
    if value is None:
        return ''
    return (str(value)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&apos;'))

# --- NUEVAS FUNCIONES DE ESTADÍSTICAS ---

def get_os_from_ua(user_agent):
    """Detecta el Sistema Operativo básico"""
    ua = user_agent.lower()
    if "android" in ua: return "Android"
    if "iphone" in ua or "ipad" in ua: return "iOS"
    if "windows" in ua: return "Windows"
    if "macintosh" in ua or "mac os" in ua: return "Mac"
    if "linux" in ua: return "Linux"
    return "Otro"

def get_location_by_ip(ip):
    """Geolocalización simple por IP"""
    if ip in ["127.0.0.1", "localhost", "::1"]:
        return "Localhost"
    try:
        # Timeout corto para no frenar la carga de la web
        r = requests.get(f"http://ip-api.com/json/{ip}", timeout=1.5)
        data = r.json()
        if data['status'] == 'success':
            return f"{data['city']}, {data['country']}"
    except:
        pass
    return "Desconocido"

def analyze_stats():
    """Lee la DB SQLite y reconstruye los diccionarios para el Admin deduplicando y filtrando bots"""
    stats = {
        'daily': Counter(),
        'posts': Counter(),
        'os': Counter(),
        'location': Counter(),
        'total': 0
    }
    
    if not os.path.exists(DB_FILE):
        return stats

    seen_visits = set()
    ignored_paths = ['/login', '/login/2fa', '/logout', '/rss.xml', '/favicon.ico']

    try:
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute("SELECT timestamp, user_id, detail, os, location FROM visits").fetchall()
        conn.close()
        for timestamp_str, user_id, detail, os_name, location in rows:
            
            # 1. Ignorar páginas técnicas/administrativas
            if detail in ignored_paths or detail.startswith('/admin'):
                continue
            
            # 2. Heurística de deduplicación (Sesión de 30 minutos)
            try:
                dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                rounded_time = dt.replace(minute=(dt.minute // 30) * 30, second=0, microsecond=0)
                visit_key = (user_id, detail, rounded_time)
            except ValueError:
                visit_key = (user_id, detail, timestamp_str[:13]) # Fallback por hora
            
            if visit_key in seen_visits:
                continue
                
            seen_visits.add(visit_key)

            # Incrementar contadores
            stats['total'] += 1
            
            # Fecha (YYYY-MM-DD) extraída del timestamp
            date_only = timestamp_str.split(' ')[0]
            stats['daily'][date_only] += 1
            
            # Posts (slugs)
            if detail != 'home' and not detail.startswith('/'):
                 stats['posts'][detail] += 1
            elif detail.startswith('/post/'):
                 # Limpiar "/post/slug" a "slug"
                 slug = detail.replace('/post/', '')
                 stats['posts'][slug] += 1
            elif detail == 'home':
                 stats['posts']['home'] += 1

            # OS y Ubicación
            stats['os'][os_name or 'Otro'] += 1
            stats['location'][location or 'Desconocido'] += 1
            
    except Exception as e:
        print(f"Error analizando CSV: {e}")
        
    return stats

# --- MIDDLEWARE (Reemplaza a log_visit) ---

@app.before_request
def identify_user():
    """Asigna un ID único al usuario si no lo tiene"""
    user_id = request.cookies.get('user_id')
    if not user_id:
        user_id = str(uuid.uuid4())[:8]
        g.is_new_user = True
        g.user_id = user_id
    else:
        g.is_new_user = False
        g.user_id = user_id

@app.after_request
def log_request_data(response):
    """Guarda la cookie y registra la visita en CSV"""
    # CAMBIO SEGURIDAD: Cabeceras de seguridad en todas las respuestas
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; img-src 'self' data:; media-src 'self' data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
        "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "connect-src 'self' 'unsafe-inline' https: http:; "
        "frame-src 'self' https://neocast.neosite.com.ar https://www.youtube.com https://youtube.com;"
    )

    # 1. Guardar Cookie si es nuevo
    if getattr(g, 'is_new_user', False):
        response.set_cookie(
            'user_id', g.user_id, max_age=31536000,
            httponly=True, samesite='Lax',
            secure=app.config['SESSION_COOKIE_SECURE']
        )

    # 2. Filtrar qué guardamos
    # No guardar si es admin, ni archivos estáticos, ni 404s
    if request.cookies.get('is_admin'):
        return response
    
    # Filtrar bots y scrapers comunes
    ua = request.user_agent.string.lower()
    bot_keywords = ['bot', 'crawler', 'spider', 'wget', 'curl', 'http', 'scrax', 'headless', 'uptime', 'python-requests']
    if any(keyword in ua for keyword in bot_keywords):
        return response

    # Filtrar rutas técnicas, administrativas y de login
    ignored_paths = ['/login', '/login/2fa', '/logout', '/rss.xml', '/favicon.ico']
    if request.path in ignored_paths or request.path.startswith('/static') or request.path.startswith('/admin'):
        return response

    if response.status_code != 200:
        return response

    # 3. Preparar datos
    path_detail = "home" if request.path == "/" else request.path
    
    # Obtener IP real (teniendo en cuenta proxies/docker)
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ip and ',' in ip: ip = ip.split(',')[0].strip()
    
    os_name = get_os_from_ua(request.user_agent.string)
    location = get_location_by_ip(ip)
    
    # 4. Escribir en la DB SQLite (SQLite maneja el locking por transacción)
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute(
                "INSERT INTO visits (timestamp, user_id, action, detail, os, location, ip) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    g.user_id,
                    "VIEW",
                    path_detail,
                    os_name,
                    location,
                    ip  # Guardamos IP por seguridad, pero no la mostramos si no quieres
                )
            )
    except Exception as e:
        print(f"Error escribiendo stats: {e}")

    return response

# --- FIN NUEVAS FUNCIONES ---

def get_posts():
    posts = []
    if not os.path.exists(CONTENT_DIR): os.makedirs(CONTENT_DIR)
    for filename in os.listdir(CONTENT_DIR):
        if filename.endswith(".md"):
            path = os.path.join(CONTENT_DIR, filename)
            post = frontmatter.load(path)
            post.metadata['slug'] = filename[:-3]
            posts.append(post.metadata)
    return sorted(posts, key=lambda x: x.get('date', ''), reverse=True)

def slugify(text):
    text = text.lower()
    return re.sub(r'[^a-z0-9]+', '-', text).strip('-')

def safe_slug(slug):
    """Limpia el slug para prevenir path traversal. Solo permite [a-z0-9-_]."""
    if not slug:
        return ''
    slug = os.path.basename(str(slug))
    slug = re.sub(r'[^a-zA-Z0-9_-]', '', slug)
    return slug

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in') and request.cookies.get('is_admin') != 'true':
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.context_processor
def inject_theme():
    try: cfg = get_config()
    except: cfg = DEFAULT_CONFIG
    try:
        os.makedirs(PAGES_DIR, exist_ok=True)
        nav = []
        for fn in sorted(os.listdir(PAGES_DIR)):
            if fn.startswith('draft_') or not fn.endswith('.md'): continue
            try:
                m = frontmatter.load(os.path.join(PAGES_DIR, fn)).metadata
                nav.append({'slug': fn[:-3], 'title': m.get('title', fn[:-3])}) if m.get('show_in_nav', True) else None
            except: pass
    except: nav = []
    return {'site_config': cfg, 'active_theme': valid_theme(cfg.get('theme', 'default')), 'nav_pages': nav}

def _check_admin_password(attempt):
    """Valida la contraseña. Soporta hash (werkzeug/bcrypt) y texto claro,
    migrando automáticamente de texto claro a hash en el primer login."""
    stored = os.environ.get('ADMIN_PASSWORD', '')

    # Si no hay contraseña configurada, no permitir login
    if not stored:
        return False

    # El stored es un hash de werkzeug (pbkdf2:sha256, scrypt:, pbkdf2::sha256:...)
    if stored.startswith(('pbkdf2:', 'scrypt:', 'sha256$')):
        return check_password_hash(stored, attempt)

    # Texto claro (migración). Comparación constante.
    return hmac_compare(stored, attempt)


def hmac_compare(a, b):
    """Comparación de strings en tiempo constante para evitar timing attacks."""
    import hmac
    return hmac.compare_digest(a.encode('utf-8'), b.encode('utf-8'))


def _migrate_password_to_hash():
    """Convierte la contraseña en claro de .env a hash bcrypt (una sola vez)."""
    stored = os.environ.get('ADMIN_PASSWORD', '')
    if not stored or stored.startswith(('pbkdf2:', 'scrypt:', 'sha256$')):
        return False

    # El env_file de Docker no es reescribible en caliente en todos los casos;
    # intentamos actualizar .env local si existe.
    env_path = os.path.join(BASE_DIR, '.env')
    if not os.path.exists(env_path):
        return False

    new_hash = generate_password_hash(stored, method='pbkdf2:sha256')
    try:
        lines = []
        with open(env_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        replaced = False
        with open(env_path, 'w', encoding='utf-8') as f:
            for line in lines:
                if line.strip().startswith('ADMIN_PASSWORD='):
                    f.write('ADMIN_PASSWORD=' + new_hash + '\n')
                    replaced = True
                else:
                    f.write(line)
            if not replaced:
                f.write('ADMIN_PASSWORD=' + new_hash + '\n')
        if replaced or True:
            # Actualizar la variable en el proceso para el siguiente check
            os.environ['ADMIN_PASSWORD'] = new_hash
            return True
    except Exception:
        pass
    return False


def _admin_password_is_hash():
    stored = os.environ.get('ADMIN_PASSWORD', '')
    return bool(stored) and stored.startswith(('pbkdf2:', 'scrypt:', 'sha256$'))


@app.route('/login', methods=['GET', 'POST'])
@csrf.exempt
@limiter.limit("10 per minute", methods=['POST'], on_breach=lambda r: None)
def login():
    if request.method == 'POST':
        # Validar Contraseña primero
        if _check_admin_password(request.form.get('password', '')):
            # Migrar a hash si venía en claro (transparente, no rompe nada)
            if not _admin_password_is_hash():
                _migrate_password_to_hash()

            # Login directo al admin (el 2FA quedó eliminado del flujo)
            session.pop('pre_auth', None)
            session['logged_in'] = True
            resp = make_response(redirect(url_for('admin_list')))
            resp.set_cookie(
                'is_admin', 'true', max_age=30*24*60*60,
                httponly=True, samesite='Lax',
                secure=app.config['SESSION_COOKIE_SECURE']
            )
            return resp
        else:
            return render_template('login.html', error="Contraseña incorrecta")

    if request.args.get('expired'):
        return render_template('login.html', error="La sesión expiró. Volvé a ingresar tu contraseña.")
    return render_template('login.html')

@app.route('/login/2fa', methods=['GET', 'POST'])
@limiter.limit("10 per minute", methods=['POST'], on_breach=lambda r: None)
def login_2fa():
    # El 2FA quedó eliminado del flujo de acceso. Si alguien llega a esta ruta
    # directamente, lo redirigimos siempre (nunca se muestra la pantalla de 2FA):
    # al admin si ya está logueado, o al login si no.
    if session.get('logged_in'):
        return redirect(url_for('admin_list'))
    return redirect(url_for('login'))

    # El código de verificación 2FA (TOTP/QR) quedó eliminado del flujo de acceso.
    # Esta función solo redirige. (código muerto eliminado)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    session.pop('pre_auth', None) # Por seguridad
    session.pop('temp_secret', None)
    resp = make_response(redirect(url_for('index')))
    resp.set_cookie('is_admin', '', expires=0)
    return resp

@app.route('/')
def index():
    # log_visit() ELIMINADO (Lo hace el middleware)
    query = request.args.get('q', '').lower()
    tag_filter = request.args.get('tag', '').lower()
    cat_filter = request.args.get('category', '').lower()

    page = request.args.get('page', 1, type=int)
    per_page = 8

    all_posts = []
    categories_count = {}
    tags_set = set()

    if not os.path.exists(CONTENT_DIR): os.makedirs(CONTENT_DIR)

    for filename in os.listdir(CONTENT_DIR):
        if filename.startswith("draft_"): continue
        if filename.endswith(".md"):
            p_file = frontmatter.load(os.path.join(CONTENT_DIR, filename))
            metadata = p_file.metadata
            metadata['slug'] = filename[:-3]

            words = len(p_file.content.split())
            metadata['read_time'] = max(1, round(words / 200))

            cat = metadata.get('category', 'Sin Categoría')
            categories_count[cat] = categories_count.get(cat, 0) + 1

            post_tags = [t.strip().lower() for t in str(metadata.get('tags', '')).split(',')] if metadata.get('tags') else []
            for t in post_tags: tags_set.add(t)
            metadata['tags_list'] = post_tags

            match = True
            if query and query not in metadata.get('title', '').lower() and query not in p_file.content.lower():
                match = False
            if tag_filter and tag_filter not in post_tags:
                match = False
            if cat_filter and cat_filter != cat.lower():
                match = False

            if match:
                all_posts.append(metadata)

    all_posts.sort(key=lambda x: str(x.get('date', '')), reverse=True)

    total_posts = len(all_posts)
    start = (page - 1) * per_page
    end = start + per_page
    paginated_posts = all_posts[start:end]

    has_next = end < total_posts
    has_prev = page > 1

    return render_template('index.html', 
                           posts=paginated_posts,
                           categories=categories_count, 
                           tags=sorted(list(tags_set)),
                           query=query, 
                           current_tag=tag_filter, 
                           current_cat=cat_filter,
                           page=page,
                           has_next=has_next,
                           has_prev=has_prev)

@app.route('/post/<slug>')
def post(slug):
    slug = safe_slug(slug)
    path = os.path.join(CONTENT_DIR, f"{slug}.md")
    if not os.path.exists(path):
        abort(404)

    post = frontmatter.load(path)
    content_html = markdown(post.content, extensions=['tables', 'fenced_code', 'nl2br'])

    # CAMBIO SEGURIDAD: Sanitizar el HTML generado del markdown (previene XSS)
    content_html = sanitize_html(content_html)
    
    # 1. Obtenemos TODOS los posts para buscar coincidencias
    all_posts = get_posts() # Esta función ya la tienes definida arriba, trae los posts ordenados por fecha
    
    # 2. Lógica de Relacionados
    current_cat = post.metadata.get('category', 'Sin Categoría')
    
    # Filtramos: Misma categoría Y que no sea el post actual (slug != slug)
    related_posts = [
        p for p in all_posts 
        if p.get('category') == current_cat and p['slug'] != slug
    ]
    
    # 3. Limitamos a 3 (los más recientes porque all_posts ya viene ordenado)
    related_posts = related_posts[:3]
    
    cfg = get_config()
    comments_enabled = cfg.get('comments_enabled', True)
    
    comments_api_base = os.getenv('COMMENTS_API_URL', 'http://localhost:5005')

    # Datos para compartir y para las vistas previas en redes sociales.
    post_url = url_for('post', slug=slug, _external=True)
    share_title = str(post.metadata.get('title') or '')
    share_links = build_share_links(post_url, share_title)
    og_description = str(post.metadata.get('description') or '').strip()
    if not og_description:
        og_description = plain_text_from_html(content_html, 160)
    og_image = url_for(
        'static',
        filename=f"themes/{valid_theme(cfg.get('theme'))}/banner.png",
        _external=True
    )

    raw_tags = post.metadata.get('tags', '')
    if isinstance(raw_tags, (list, tuple)):
        share_tags = [str(t).strip() for t in raw_tags if str(t).strip()]
    else:
        share_tags = [t.strip() for t in str(raw_tags or '').split(',') if t.strip()]

    # La plantilla del post ya esperaba estos datos, pero solo se calculaban en el index.
    post.metadata['tags_list'] = share_tags
    post.metadata['read_time'] = max(1, round(len(post.content.split()) / 200))
    
    # 4. Agregamos related_posts al return
    return render_template('post.html', 
                           post=post.metadata, 
                           content=content_html, 
                           comments_enabled=comments_enabled, 
                           slug=slug, 
                           comments_api_url=comments_api_base,
                           related_posts=related_posts,
                           post_url=post_url,
                           share_links=share_links,
                           share_tags=share_tags,
                           og_description=og_description,
                           og_image=og_image)

@app.route('/admin')
@login_required
def admin_list():
    posts = get_posts()
    
    # CAMBIO: Usamos el analizador de stats (SQLite)
    stats_data = analyze_stats()
    
    # Procesar datos para el Dashboard
    # Convertimos Counters a diccionarios normales para evitar problemas en Jinja
    daily_stats = dict(stats_data['daily'])
    post_stats = dict(stats_data['posts'])
    
    top_posts = sorted(post_stats.items(), key=lambda item: item[1], reverse=True)[:3]
    sorted_stats = dict(sorted(daily_stats.items(), reverse=True)[:7])
    
    last_7_days = []
    for i in range(6, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        last_7_days.append({'date': d[-2:], 'count': daily_stats.get(d, 0)})
    
    max_visits = max([day['count'] for day in last_7_days] + [1])
    config = get_config()
    comments_on = config.get('comments_enabled', True)
    total_visits = stats_data['total']

    return render_template('admin.html', 
                           posts=posts, 
                           stats=sorted_stats,
                           top_posts=top_posts,
                           stats_days=last_7_days, 
                           max_visits=max_visits,
                           comments_on=comments_on,
                           total_visits=total_visits)

@app.route('/admin/posts')
@login_required
def admin_posts():
    return render_template('admin_posts.html', posts=get_posts(), fediverse=get_fediverse_config())


@app.route('/admin/fediverse', methods=['GET', 'POST'])
@login_required
def admin_fediverse():
    cfg = get_fediverse_config()
    if request.method == 'POST':
        action = request.form.get('action', 'save')

        if action == 'disconnect':
            cfg['access_token'] = ''
            cfg['account'] = ''
            cfg['enabled'] = False
            cfg['connected_at'] = ''
            cfg['auth_method'] = 'token'
            cfg['auth_user'] = ''
            cfg['client_id'] = ''
            cfg['client_secret'] = ''
            save_fediverse_config(cfg)
            flash('Cuenta del fediverso desconectada. El token guardado fue eliminado.', 'success')
            return redirect(url_for('admin_fediverse'))

        if action == 'test':
            try:
                account = fediverse_verify(cfg)
                cfg['account'] = account
                cfg['max_chars'] = fediverse_max_chars(cfg)
                cfg['connected_at'] = datetime.now().isoformat(timespec='seconds')
                save_fediverse_config(cfg)
                flash(f'Conexión correcta con {account}.', 'success')
            except FediverseError as exc:
                flash(f'No se pudo verificar la conexión: {exc}', 'error')
            except Exception as exc:
                print(f'Error verificando el fediverso: {exc}')
                flash('Ocurrió un error inesperado al verificar la conexión.', 'error')
            return redirect(url_for('admin_fediverse'))

        instance_raw = request.form.get('instance', '').strip()
        instance = normalize_fediverse_instance(instance_raw)
        if instance_raw and not instance:
            flash('La URL de la instancia no es válida. Usá por ejemplo https://mastodon.social', 'error')
            return redirect(url_for('admin_fediverse'))

        method = request.form.get('auth_method', 'token')
        if method not in FEDIVERSE_AUTH_METHODS:
            method = 'token'
        token = request.form.get('access_token', '').strip()
        auth_user = request.form.get('auth_user', '').strip()
        password = request.form.get('password', '')
        otp = request.form.get('otp', '').strip()
        client_id = request.form.get('client_id', '').strip()
        client_secret = request.form.get('client_secret', '').strip()

        # Trabajamos sobre una copia: si la conexión con contraseña falla, no se
        # toca el token ni la configuración anterior.
        updated = dict(cfg)
        updated['instance'] = instance
        updated['visibility'] = request.form.get('visibility', 'public')
        if updated['visibility'] not in FEDIVERSE_VISIBILITIES:
            updated['visibility'] = 'public'
        language = request.form.get('language', 'es').strip().lower()
        updated['language'] = language if re.match(r'^[a-z]{2,3}$', language) else 'es'
        updated['include_tags'] = request.form.get('include_tags') == '1'
        updated['enabled'] = request.form.get('enabled') == '1'
        updated['auth_method'] = method

        # Al cambiar de instancia, el token y la app anteriores no sirven.
        if instance != cfg.get('instance'):
            updated['account'] = ''
            updated['connected_at'] = ''
            updated['access_token'] = ''
            updated['client_id'] = ''
            updated['client_secret'] = ''

        if client_id:
            updated['client_id'] = client_id
        if client_secret:
            updated['client_secret'] = client_secret
        if auth_user:
            updated['auth_user'] = auth_user

        reauthenticated = False
        if method == 'token':
            if token:
                updated['access_token'] = token
                updated['account'] = ''
                updated['connected_at'] = ''
        else:
            password_form = url_for('admin_fediverse', method='password', user=auth_user)
            if password and not auth_user:
                flash('Ingresá el usuario de la instancia junto con la contraseña.', 'error')
                return redirect(password_form)
            if auth_user and not password:
                if (
                    not updated.get('access_token')
                    or instance != cfg.get('instance')
                    or auth_user != cfg.get('auth_user')
                ):
                    flash('Escribí la contraseña para conectar con este usuario. La contraseña no se guarda.', 'error')
                    return redirect(password_form)
            if password:
                if not instance:
                    flash('Para conectar con usuario y contraseña primero indicá la instancia.', 'error')
                    return redirect(password_form)
                try:
                    result = fediverse_fetch_password_token(
                        instance,
                        auth_user,
                        password,
                        otp,
                        client_id or updated.get('client_id', ''),
                        client_secret or updated.get('client_secret', ''),
                    )
                    updated.update(result)
                    updated['auth_user'] = auth_user
                    updated['account'] = ''
                    updated['connected_at'] = ''
                    reauthenticated = True
                except FediverseTwoFactorRequired as exc:
                    flash(f'{exc} La configuración anterior se mantiene.', 'error')
                    return redirect(password_form)
                except FediverseError as exc:
                    flash(
                        f'No se pudo conectar con usuario y contraseña: {exc} '
                        'La configuración anterior se mantiene.',
                        'error',
                    )
                    return redirect(password_form)
                except Exception as exc:
                    print(f'Error conectando con usuario y contraseña: {exc.__class__.__name__}')
                    flash('Ocurrió un error inesperado al conectar. La configuración anterior se mantiene.', 'error')
                    return redirect(password_form)
            elif not updated.get('access_token'):
                flash('Ingresá usuario y contraseña, o cambiá al método de token manual.', 'error')
                return redirect(password_form)

        if updated['enabled'] and (not updated['instance'] or not updated['access_token']):
            updated['enabled'] = False
            save_fediverse_config(updated)
            flash('Para activar la publicación automática primero guardá la instancia y el token.', 'error')
            return redirect(url_for('admin_fediverse'))

        if updated['instance'] and updated['access_token'] and request.form.get('verify', '1') == '1':
            try:
                updated['account'] = fediverse_verify(updated)
                updated['max_chars'] = fediverse_max_chars(updated)
                updated['connected_at'] = datetime.now().isoformat(timespec='seconds')
                flash('Configuración guardada y conexión verificada.', 'success')
            except FediverseError as exc:
                if reauthenticated:
                    flash(
                        f'La contraseña fue aceptada, pero la verificación falló: {exc} '
                        'La configuración anterior se mantiene.',
                        'error',
                    )
                    return redirect(url_for(
                        'admin_fediverse', method='password', user=updated.get('auth_user', '')
                    ))
                save_fediverse_config(updated)
                flash(f'Configuración guardada, pero la verificación falló: {exc}', 'error')
                return redirect(url_for('admin_fediverse'))
            except Exception as exc:
                print(f'Error verificando el fediverso: {exc}')
                if reauthenticated:
                    flash(
                        'La contraseña fue aceptada, pero ocurrió un error al verificar la conexión. '
                        'La configuración anterior se mantiene.',
                        'error',
                    )
                    return redirect(url_for(
                        'admin_fediverse', method='password', user=updated.get('auth_user', '')
                    ))
                save_fediverse_config(updated)
                flash('Configuración guardada, pero ocurrió un error al verificar la conexión.', 'error')
                return redirect(url_for('admin_fediverse'))
        else:
            flash('Configuración guardada.', 'success')

        save_fediverse_config(updated)
        return redirect(url_for('admin_fediverse'))

    token = cfg.get('access_token') or ''
    selected_method = request.args.get('method', '')
    if selected_method not in FEDIVERSE_AUTH_METHODS:
        selected_method = cfg.get('auth_method', 'token')
    form_user = request.args.get('user', '').strip() or cfg.get('auth_user', '')
    return render_template(
        'fediverse.html',
        cfg=cfg,
        has_token=bool(token),
        selected_method=selected_method,
        form_user=form_user,
        token_hint=('…' + token[-4:]) if len(token) >= 4 else '',
        visibilities=[
            ('public', 'Pública', 'Visible en el timeline público y en las búsquedas.'),
            ('unlisted', 'No listada', 'Visible para quien tenga el enlace, pero fuera del timeline público.'),
            ('private', 'Solo seguidores', 'Visible únicamente para tus seguidores.'),
        ],
    )

@app.route('/admin/fediverse/publish/<slug>', methods=['POST'])
@login_required
def admin_fediverse_publish(slug):
    """Publica manualmente una entrada ya publicada en el blog."""
    slug = safe_slug(slug)
    path = os.path.join(CONTENT_DIR, f"{slug}.md")
    if not os.path.exists(path):
        abort(404)
    post = frontmatter.load(path)
    if slug.startswith('draft_') or str(post.metadata.get('status', 'published')) == 'draft':
        flash('No se puede publicar un borrador en el fediverso. Publicá la entrada primero.', 'error')
        return redirect(url_for('admin_posts'))
    cfg = get_fediverse_config()
    if not cfg.get('instance') or not cfg.get('access_token'):
        flash('Primero configurá la instancia y el token en la sección Fediverso.', 'error')
        return redirect(url_for('admin_posts'))
    if post.metadata.get('fediverse_url'):
        flash('Esta entrada ya está publicada en el fediverso.', 'error')
        return redirect(url_for('admin_posts'))
    try:
        data = publish_to_fediverse(post.metadata, slug)
        post.metadata['fediverse_url'] = data.get('url', '')
        post.metadata['fediverse_instance'] = cfg.get('instance', '')
        post.metadata['fediverse_id'] = str(data.get('id', ''))
        post.metadata['fediverse_posted_at'] = datetime.now().isoformat(timespec='seconds')
        with open(path, 'wb') as f:
            frontmatter.dump(post, f)
        flash('Entrada publicada en el fediverso.', 'success')
    except FediverseError as exc:
        flash(f'No se pudo publicar en el fediverso: {exc}', 'error')
    except Exception as exc:
        print(f'Error publicando manualmente en el fediverso: {exc}')
        flash('Ocurrió un error inesperado al publicar en el fediverso.', 'error')
    return redirect(url_for('admin_posts'))


@app.route('/admin/edit/<slug>', methods=['GET', 'POST'])
@app.route('/admin/new', methods=['GET', 'POST'], defaults={'slug': None})
@login_required
def edit_post(slug):
    if slug:
        slug = safe_slug(slug)

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        content = request.form.get('content', '')
        date = request.form.get('date') or datetime.now().strftime('%Y-%m-%d')
        category = request.form.get('category', '').strip() or "Sin Categoría"
        tags = request.form.get('tags', '').strip()
        description = request.form.get('description', '').strip()
        status = request.form.get('status', 'published')
        if status not in ('published', 'draft'):
            status = 'draft'
        skip_fediverse = request.form.get('fediverse_skip') == '1'

        # Metadatos previos: hay que conservarlos (imagen, fediverse_url, etc.).
        old_metadata = {}
        old_path = None
        if slug:
            old_path = os.path.join(CONTENT_DIR, f"{slug}.md")
            if os.path.exists(old_path):
                try:
                    old_metadata = dict(frontmatter.load(old_path).metadata)
                except Exception:
                    old_metadata = {}
        was_published = bool(
            slug
            and not slug.startswith('draft_')
            and str(old_metadata.get('status', 'published')) != 'draft'
        )

        clean_slug = safe_slug(slug.replace('draft_', '') if slug else slugify(title))
        if not clean_slug: clean_slug = "post-sin-titulo"

        new_filename = f"draft_{clean_slug}.md" if status == 'draft' else f"{clean_slug}.md"
        new_path = os.path.join(CONTENT_DIR, new_filename)

        post_file = frontmatter.Post(content)
        post_file.metadata.update(old_metadata)
        post_file.metadata.update({
            'title': title,
            'date': date,
            'category': category,
            'tags': tags,
            'description': description,
            'status': status,
        })
        if skip_fediverse:
            post_file.metadata['fediverse_skip'] = True
        else:
            post_file.metadata.pop('fediverse_skip', None)

        # Se escribe primero el archivo: si la instancia falla, la entrada no se pierde.
        with open(new_path, 'wb') as f:
            frontmatter.dump(post_file, f)
        if old_path and old_path != new_path and os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass

        # Solo se publica cuando la entrada pasa a pública por primera vez.
        published_now = (
            status == 'published'
            and not was_published
            and not skip_fediverse
            and not post_file.metadata.get('fediverse_url')
        )
        fediverse_cfg = get_fediverse_config()
        if published_now and fediverse_cfg.get('enabled'):
            try:
                data = publish_to_fediverse(post_file.metadata, clean_slug)
                post_file.metadata['fediverse_url'] = data.get('url', '')
                post_file.metadata['fediverse_instance'] = fediverse_cfg.get('instance', '')
                post_file.metadata['fediverse_id'] = str(data.get('id', ''))
                post_file.metadata['fediverse_posted_at'] = datetime.now().isoformat(timespec='seconds')
                with open(new_path, 'wb') as f:
                    frontmatter.dump(post_file, f)
                flash('Entrada publicada también en el fediverso.', 'success')
            except FediverseError as exc:
                flash(f'La entrada se guardó, pero no se pudo publicar en el fediverso: {exc}', 'error')
            except Exception as exc:
                print(f'Error publicando en el fediverso: {exc}')
                flash('La entrada se guardó, pero ocurrió un error inesperado al publicar en el fediverso.', 'error')

        return redirect(url_for('admin_posts'))

    post_data = {
        "title": "", "content": "", "category": "", 
        "tags": "", "description": "", "date": datetime.now().strftime('%Y-%m-%d'),
        "status": "published", "fediverse_skip": False, "fediverse_url": ""
    }

    if slug:
        path = os.path.join(CONTENT_DIR, f"{slug}.md")
        if os.path.exists(path):
            post = frontmatter.load(path)
            post_data = {
                "title": post.metadata.get('title', ''),
                "content": post.content,
                "date": post.metadata.get('date', ''),
                "category": post.metadata.get('category', ''),
                "tags": post.metadata.get('tags', ''),
                "description": post.metadata.get('description', ''),
                "status": "draft" if slug.startswith('draft_') else "published",
                "fediverse_skip": bool(post.metadata.get('fediverse_skip')),
                "fediverse_url": post.metadata.get('fediverse_url', ''),
            }

    return render_template('edit.html', post=post_data)

@app.route('/admin/save', methods=['POST'])
def save_post():
    # Ruta auxiliar legacy, por si acaso
    return redirect('/admin/posts')

@app.route('/admin/delete/<slug>', methods=['POST'])
@login_required
def delete_post(slug):
    slug = safe_slug(slug)
    path = os.path.join(CONTENT_DIR, f"{slug}.md")
    if os.path.exists(path):
        os.remove(path)
    return redirect(url_for('admin_posts'))

@app.route('/admin/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        return {"error": "No file part"}, 400
    file = request.files['file']
    if file.filename == '':
        return {"error": "No selected file"}, 400

    filename = secure_filename(file.filename)
    if not filename or not allowed_file(filename):
        return {"error": "File type not allowed"}, 400

    # Validar tamaño (además de MAX_CONTENT_LENGTH) y extensión real
    content_type = file.content_type or ''
    if content_type and not content_type.startswith(('image/', 'audio/', 'video/')):
        return {"error": "File type not allowed"}, 400

    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
    return {"url": f"/static/uploads/{filename}"}, 200

@app.route('/admin/backup')
@login_required
def backup():
    backup_filename = f"cms_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    backup_path = os.path.join('/tmp', backup_filename)

    with zipfile.ZipFile(backup_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, _, files in os.walk(CONTENT_DIR):
            for file in files:
                zipf.write(os.path.join(root, file), os.path.relpath(os.path.join(root, file), os.path.join(app.root_path, 'content')))
        for root, _, files in os.walk(UPLOAD_FOLDER):
            for file in files:
                zipf.write(os.path.join(root, file), os.path.relpath(os.path.join(root, file), os.path.join(app.root_path, 'static')))
        # Agregamos también la DB de stats al backup
        if os.path.exists(DB_FILE):
             zipf.write(DB_FILE, 'stats.db')

    return send_file(backup_path, as_attachment=True, download_name=backup_filename)

@app.route('/rss.xml')
def rss():
    posts = get_posts()
    published_posts = [p for p in posts if p.get('published', True)]
    published_posts.sort(key=lambda x: x.get('date', ''), reverse=True)

    rss_xml = '<?xml version="1.0" encoding="UTF-8" ?>'
    rss_xml += '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
    rss_xml += '<channel>'
    rss_xml += '<title>NeoSite Blog</title>'
    rss_xml += '<link>https://blog.neosite.com.ar</link>'
    rss_xml += '<description>Software libre, tecnología y más...</description>'
    
    base_url = request.url_root.rstrip('/')
    
    for post in published_posts:
        content = post.get('content', '').replace('src="/static/', 'src="https://blog.neosite.com.ar/static/')
        post_url = f"{base_url}/post/{post['slug']}"

        # CAMBIO SEGURIDAD: escapar texto dentro de etiquetas XML y sanitizar HTML del contenido
        safe_title = xml_escape(post.get('title', ''))
        safe_content = sanitize_html(content)

        rss_xml += '<item>'
        rss_xml += f'<title>{safe_title}</title>'
        rss_xml += f'<link>{post_url}</link>'
        rss_xml += f'<guid>{post_url}</guid>'
        rss_xml += f'<pubDate>{xml_escape(post.get("date", ""))}</pubDate>'
        rss_xml += f'<description><![CDATA[{safe_content}]]></description>'
        rss_xml += '</item>'
    
    rss_xml += '</channel></rss>'
    return Response(rss_xml, mimetype='application/rss+xml')

@app.route('/admin/export-stats')
@login_required
def export_stats():
    # Genera el CSV de stats bajo demanda a partir de la DB SQLite
    import csv
    if not os.path.exists(DB_FILE):
        return "No stats yet"

    buffer = StringIO(newline='')
    conn = None
    try:
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute(
            "SELECT timestamp, user_id, action, detail, os, location, ip "
            "FROM visits ORDER BY timestamp"
        ).fetchall()
        writer = csv.writer(buffer)
        writer.writerow(["Timestamp", "User_ID", "Action", "Detail", "OS", "Location", "IP"])
        writer.writerows(rows)
    except Exception as e:
        print(f"Error exportando stats: {e}")
        return "Error exportando stats", 500
    finally:
        if conn is not None:
            conn.close()

    # utf-8-sig agrega BOM para que Excel reconozca acentos correctamente.
    contenido = buffer.getvalue().encode('utf-8-sig')
    filename = f"stats-{datetime.now().strftime('%Y-%m-%d')}.csv"
    return Response(
        contenido,
        mimetype='text/csv',
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
     
@app.route('/admin/stats')
@login_required
def full_stats():
    # CAMBIO: Analizar CSV completo
    stats_data = analyze_stats()
    
    # Ordenar historial
    sorted_days = sorted(stats_data['daily'].items(), reverse=True)
    # Ordenar posts
    sorted_posts = sorted(stats_data['posts'].items(), key=lambda x: x[1], reverse=True)[:20]
    
    # NUEVO: Pasar OS y Location al template
    # (Tendrás que actualizar full_stats.html si quieres ver esto)
    top_os = sorted(stats_data['os'].items(), key=lambda x: x[1], reverse=True)[:5]
    top_loc = sorted(stats_data['location'].items(), key=lambda x: x[1], reverse=True)[:10]

    return render_template('full_stats.html', 
                           total=stats_data['total'],
                           history=sorted_days, 
                           top_posts=sorted_posts,
                           os_stats=top_os,         # Pasamos datos nuevos
                           location_stats=top_loc)  # Pasamos datos nuevos

@app.route('/admin/comments')
@login_required
def admin_comments():
    all_comments = []
    search_path = os.path.join(COMMENTS_DATA_DIR, "*.json")
    files = glob.glob(search_path)

    for file_path in files:
        slug = os.path.basename(file_path).replace('.json', '')
        try:
            with open(file_path, 'r') as f:
                post_comments = json.load(f)
                for c in post_comments:
                    c['slug'] = slug 
                    all_comments.append(c)
        except Exception as e:
            print(f"Error leyendo {file_path}: {e}")
    
    all_comments.sort(key=lambda x: x.get('id', 0), reverse=True)
    return render_template('admin_comments.html', comments=all_comments)

@app.route('/admin/comments/approve/<slug>/<float:comment_id>')
@login_required
def approve_comment(slug, comment_id):
    slug = safe_slug(slug)
    file_path = os.path.join(COMMENTS_DATA_DIR, f"{slug}.json")
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            comments = json.load(f)
        
        for c in comments:
            if c['id'] == comment_id:
                c['approved'] = True
                break
        
        with open(file_path, 'w') as f:
            json.dump(comments, f, indent=4)
            
    return redirect(url_for('admin_comments'))

@app.route('/admin/comments/delete/<slug>/<float:comment_id>')
@login_required
def delete_comment(slug, comment_id):
    slug = safe_slug(slug)
    file_path = os.path.join(COMMENTS_DATA_DIR, f"{slug}.json")
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            comments = json.load(f)
        
        new_comments = [c for c in comments if c['id'] != comment_id]
        
        with open(file_path, 'w') as f:
            json.dump(new_comments, f, indent=4)
            
    return redirect(url_for('admin_comments'))

@app.route('/admin/settings/toggle-comments', methods=['POST'])
@login_required
def toggle_comments():
    config_path = 'config.json'
    config = {'comments_enabled': True}

    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                content = f.read().strip()
                if content:
                    config = json.loads(content)
        except:
            config = {'comments_enabled': True}

    current_state = config.get('comments_enabled', True)
    config['comments_enabled'] = not current_state
    
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=4)
        
    return redirect(url_for('admin_list'))

DEFAULT_CONFIG = {'comments_enabled': True, 'site_title': 'NeoSite Blog', 'theme': 'default', 'theme_vars': {}, 'banner': 'banner.png', 'nav_links': [], 'custom_css': ''}
THEMES_DIR = os.path.join(BASE_DIR, 'static', 'themes')
PAGES_DIR = os.path.join(BASE_DIR, 'content', 'pages')

def get_config():
    config_path = os.path.join(BASE_DIR, 'config.json')
    cfg = dict(DEFAULT_CONFIG)
    if not os.path.exists(config_path): return cfg
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content: return cfg
            loaded = json.loads(content)
            if isinstance(loaded, dict): cfg.update(loaded)
            return cfg
    except:
        return cfg

def save_config(cfg):
    config_path = os.path.join(BASE_DIR, 'config.json')
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

# --- Publicación automática en el Fediverso ---
# La configuración vive en data/ (volumen escribible y fuera de Git) porque
# contiene el token de acceso. Nunca se guarda en config.json.
FEDIVERSE_CONFIG_FILE = os.path.join(DB_DIR, 'fediverse.json')
FEDIVERSE_DEFAULT = {
    'enabled': False,
    'instance': '',
    'access_token': '',
    'auth_method': 'token',
    'auth_user': '',
    'client_id': '',
    'client_secret': '',
    'visibility': 'public',
    'include_tags': True,
    'language': 'es',
    'account': '',
    'max_chars': 500,
    'connected_at': '',
}
FEDIVERSE_VISIBILITIES = ('public', 'unlisted', 'private', 'direct')
FEDIVERSE_AUTH_METHODS = ('token', 'password')


class FediverseError(Exception):
    """Error controlado al hablar con la instancia del Fediverso."""


class FediverseTwoFactorRequired(FediverseError):
    """La instancia pide un código de verificación en dos pasos."""


def get_fediverse_config():
    """Devuelve la configuración del Fediverso, con valores por defecto seguros."""
    cfg = dict(FEDIVERSE_DEFAULT)
    try:
        with open(FEDIVERSE_CONFIG_FILE, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            for key in FEDIVERSE_DEFAULT:
                if key in loaded:
                    cfg[key] = loaded[key]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    cfg['enabled'] = bool(cfg.get('enabled'))
    cfg['include_tags'] = bool(cfg.get('include_tags', True))
    cfg['instance'] = str(cfg.get('instance') or '').rstrip('/')
    cfg['access_token'] = str(cfg.get('access_token') or '')
    cfg['account'] = str(cfg.get('account') or '')
    cfg['auth_user'] = str(cfg.get('auth_user') or '')
    cfg['client_id'] = str(cfg.get('client_id') or '')
    cfg['client_secret'] = str(cfg.get('client_secret') or '')
    if cfg.get('auth_method') not in FEDIVERSE_AUTH_METHODS:
        cfg['auth_method'] = 'token'
    if cfg.get('visibility') not in FEDIVERSE_VISIBILITIES:
        cfg['visibility'] = 'public'
    if not re.match(r'^[a-z]{2,3}$', str(cfg.get('language') or '')):
        cfg['language'] = 'es'
    try:
        cfg['max_chars'] = max(100, min(10000, int(cfg.get('max_chars') or 500)))
    except (TypeError, ValueError):
        cfg['max_chars'] = 500
    return cfg


def save_fediverse_config(cfg):
    """Guarda la configuración con permisos 0600 y escritura atómica."""
    data = {key: cfg.get(key, FEDIVERSE_DEFAULT[key]) for key in FEDIVERSE_DEFAULT}
    directory = os.path.dirname(FEDIVERSE_CONFIG_FILE)
    os.makedirs(directory, exist_ok=True)
    tmp_path = FEDIVERSE_CONFIG_FILE + '.tmp'
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, FEDIVERSE_CONFIG_FILE)
    try:
        os.chmod(FEDIVERSE_CONFIG_FILE, 0o600)
    except OSError:
        pass
    return data


def normalize_fediverse_instance(value):
    """Normaliza y valida la URL de la instancia.

    Solo acepta http para localhost (desarrollo); el resto debe ser https.
    Evita credenciales embebidas y direcciones privadas.
    """
    raw = str(value or '').strip()
    if not raw:
        return ''
    if '://' not in raw:
        raw = 'https://' + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return ''
    if parts.scheme not in ('http', 'https'):
        return ''
    if parts.username or parts.password:
        return ''
    host = (parts.hostname or '').lower()
    if not host:
        return ''
    is_loopback = host in ('localhost', '127.0.0.1', '::1')
    if parts.scheme == 'http' and not is_loopback:
        return ''
    try:
        ip = ipaddress.ip_address(host)
        if not (ip.is_global or ip.is_loopback):
            return ''
    except ValueError:
        if not re.match(r'^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$', host):
            return ''
    netloc = host if port is None else f'{host}:{port}'
    if ':' in host:
        netloc = f'[{host}]' + (f':{port}' if port is not None else '')
    path = (parts.path or '').rstrip('/')
    if path and not re.match(r'^/[A-Za-z0-9._~/-]*$', path):
        return ''
    return urlunsplit((parts.scheme, netloc, path, '', ''))


def fediverse_request(cfg, method, path, **kwargs):
    """Ejecuta una petición autenticada contra la instancia configurada."""
    instance = str(cfg.get('instance') or '').rstrip('/')
    token = str(cfg.get('access_token') or '')
    if not instance or not token:
        raise FediverseError('Falta configurar la instancia o el token de acceso.')

    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
        'User-Agent': 'NeoCMS/1.2 (+https://github.com/neoranger/neocms)',
    }
    headers.update(kwargs.pop('headers', {}) or {})
    timeout = kwargs.pop('timeout', 10)
    kwargs.setdefault('allow_redirects', False)
    try:
        return requests.request(method, instance + path, headers=headers, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise FediverseError(f'No se pudo conectar con {instance}: {exc.__class__.__name__}.') from exc


def fediverse_public_request(instance, method, path, **kwargs):
    """Petición sin autenticar, para registrar la app e intercambiar el token."""
    base = str(instance or '').rstrip('/')
    if not base:
        raise FediverseError('Falta configurar la instancia.')

    headers = {
        'Accept': 'application/json',
        'User-Agent': 'NeoCMS/1.2 (+https://github.com/neoranger/neocms)',
    }
    headers.update(kwargs.pop('headers', {}) or {})
    timeout = kwargs.pop('timeout', 10)
    kwargs.setdefault('allow_redirects', False)
    try:
        return requests.request(method, base + path, headers=headers, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise FediverseError(f'No se pudo conectar con {base}: {exc.__class__.__name__}.') from exc


def fediverse_register_app(instance):
    """Registra una app temporal para obtener un token por contraseña.

    Se usa solo cuando el formulario no incluye credenciales de una app ya
    existente. El client_secret resultante se guarda junto al token (nunca la
    contraseña del usuario).
    """
    resp = fediverse_public_request(
        instance,
        'POST',
        '/api/v1/apps',
        data={
            'client_name': 'NeoCMS',
            'redirect_uris': 'urn:ietf:wg:oauth:2.0:oob',
            'scopes': 'read write:statuses',
            'website': 'https://github.com/neoranger/neocms',
        },
    )
    if resp.status_code not in (200, 201):
        raise FediverseError(
            'No se pudo registrar una aplicación en la instancia. '
            'Probá con un token manual o cargá client_id y client_secret.'
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise FediverseError('La instancia devolvió una respuesta inválida al registrar la app.') from exc
    client_id = str(data.get('client_id') or '').strip()
    client_secret = str(data.get('client_secret') or '').strip()
    if not client_id:
        raise FediverseError('La instancia no devolvió client_id. Usá un token manual.')
    return client_id, client_secret


def fediverse_oauth_error_text(resp):
    """Extrae un detalle corto del error de OAuth sin exponer credenciales."""
    try:
        data = resp.json()
    except ValueError:
        return ''
    if not isinstance(data, dict):
        return ''
    parts = []
    for key in ('error', 'error_description', 'message', 'detail'):
        value = data.get(key)
        if value and not isinstance(value, (dict, list)):
            parts.append(str(value))
    for key in ('two_factor_required', 'mfa_required', 'otp_required'):
        if data.get(key):
            parts.append(key)
    return ' '.join(parts).strip()[:240]


def fediverse_fetch_password_token(instance, username, password, otp='', client_id='', client_secret=''):
    """Obtiene un token con grant_type=password.

    Devuelve un dict con access_token, client_id y client_secret. La contraseña
    y el OTP solo viven en esta llamada: no se guardan ni se registran.
    """
    username = str(username or '').strip()
    password = str(password or '')
    otp = str(otp or '').strip()
    if not username or not password:
        raise FediverseError('Ingresá usuario y contraseña para conectar.')
    if not instance:
        raise FediverseError('Falta configurar la instancia.')

    client_id = str(client_id or '').strip()
    client_secret = str(client_secret or '').strip()
    if not client_id:
        client_id, client_secret = fediverse_register_app(instance)

    payload = {
        'grant_type': 'password',
        'client_id': client_id,
        'username': username,
        'password': password,
        'scope': 'read write:statuses',
    }
    if client_secret:
        payload['client_secret'] = client_secret
    headers = {}
    if otp:
        # Mastodon y la mayoría de los servidores compatibles leen el header;
        # algunos (Pleroma/Akkoma/KutSocial) también aceptan el campo del form.
        headers['X-Mastodon-OTP'] = otp
        payload['otp'] = otp

    resp = fediverse_public_request(instance, 'POST', '/oauth/token', data=payload, headers=headers)
    if resp.status_code == 200:
        try:
            data = resp.json()
        except ValueError as exc:
            raise FediverseError('La instancia devolvió una respuesta inválida al pedir el token.') from exc
        token = str(data.get('access_token') or '').strip()
        if not token:
            raise FediverseError('La instancia no devolvió un token de acceso.')
        return {
            'access_token': token,
            'client_id': client_id,
            'client_secret': client_secret,
        }

    detail = fediverse_oauth_error_text(resp)
    combined = detail.lower()
    if resp.status_code in (400, 401, 422):
        if any(term in combined for term in ('two_factor', 'two-factor', 'otp', 'mfa', '2fa')):
            if otp:
                raise FediverseError(
                    'El código 2FA no fue aceptado. Verificá que sea el código actual y reintentá.'
                )
            raise FediverseTwoFactorRequired(
                'La cuenta tiene la verificación en dos pasos activada. '
                'Ingresá el código actual en el campo correspondiente y reintentá.'
            )
        if resp.status_code == 422 or any(
            term in combined for term in ('unsupported_grant_type', 'not supported', 'grant type')
        ):
            raise FediverseError(
                'Esta instancia no admite el acceso con usuario y contraseña. '
                'Usá el método de token manual.'
            )
        if 'invalid_client' in combined:
            raise FediverseError(
                'Las credenciales de la aplicación no son válidas. '
                'Dejá client_id y client_secret vacíos para que NeoCMS registre una app nueva.'
            )
        if resp.status_code == 401 or any(
            term in combined for term in ('invalid_grant', 'invalid username', 'invalid password', 'credentials')
        ):
            if otp:
                raise FediverseError(
                    'Usuario, contraseña o código 2FA incorrectos. '
                    'Revisá los datos y, si el código venció, esperá el siguiente.'
                )
            raise FediverseError(
                'Usuario o contraseña incorrectos, o el acceso por contraseña está '
                'deshabilitado en la instancia.'
            )
        raise FediverseError(
            'La instancia rechazó la conexión'
            + (f': {detail}' if detail else '. Revisá los datos e intentá de nuevo.')
        )
    if resp.status_code == 403:
        raise FediverseError('La instancia bloqueó la solicitud de token. Probá con un token manual.')
    if resp.status_code == 404:
        raise FediverseError(
            'La instancia no expone /oauth/token. Verificá que sea Mastodon, Pleroma, '
            'Akkoma o GoToSocial, o usá un token manual.'
        )
    if resp.status_code == 429:
        retry = resp.headers.get('Retry-After')
        raise FediverseError(
            f'La instancia limitó los intentos. Esperá {retry} s y reintentá.'
            if retry else 'La instancia limitó los intentos. Esperá un momento y reintentá.'
        )
    if resp.status_code >= 500:
        raise FediverseError('La instancia tuvo un error interno al pedir el token. Reintentá más tarde.')
    raise FediverseError(
        f'No se pudo obtener el token (HTTP {resp.status_code})'
        + (f': {detail}' if detail else '.')
    )


def fediverse_error_message(resp):
    """Traduce una respuesta de error de la API a un mensaje entendible."""
    detail = ''
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            detail = payload.get('error') or payload.get('error_description') or ''
    except ValueError:
        pass
    if resp.status_code == 401:
        return 'El token es inválido o fue revocado.'
    if resp.status_code == 403:
        return 'El token no tiene permiso para publicar (necesita el scope write:statuses).'
    if resp.status_code == 404:
        return 'La instancia o el endpoint no existen. Revisá la URL.'
    if resp.status_code == 422:
        return f'La instancia rechazó el estado: {detail or "texto inválido o vacío"}'
    if resp.status_code == 429:
        retry = resp.headers.get('Retry-After')
        return f'Límite de publicaciones alcanzado. Reintentá en {retry} s.' if retry else 'Límite de publicaciones alcanzado.'
    if resp.status_code >= 500:
        return 'La instancia tuvo un error interno. Reintentá más tarde.'
    return f'Error HTTP {resp.status_code}: {detail or "respuesta inesperada"}'


def fediverse_verify(cfg):
    """Verifica el token y devuelve la cuenta en formato @usuario@instancia."""
    resp = fediverse_request(cfg, 'GET', '/api/v1/accounts/verify_credentials')
    if resp.status_code != 200:
        raise FediverseError(fediverse_error_message(resp))
    try:
        data = resp.json()
    except ValueError as exc:
        raise FediverseError('La instancia devolvió una respuesta inválida.') from exc
    acct = str(data.get('acct') or data.get('username') or '').strip()
    if not acct:
        return ''
    host = str(cfg.get('instance') or '').split('://')[-1].split('/')[0]
    return acct if '@' in acct else f'@{acct}@{host}'


def fediverse_max_chars(cfg):
    """Obtiene el límite de caracteres de la instancia, con fallback a 500."""
    try:
        resp = fediverse_request(cfg, 'GET', '/api/v2/instance', timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            value = (((data.get('configuration') or {}).get('statuses') or {}).get('max_characters'))
            if isinstance(value, int) and 100 <= value <= 10000:
                return value
        resp = fediverse_request(cfg, 'GET', '/api/v1/instance', timeout=6)
        if resp.status_code == 200:
            value = resp.json().get('max_toot_chars')
            if isinstance(value, int) and 100 <= value <= 10000:
                return value
    except (FediverseError, ValueError, AttributeError):
        pass
    return 500


def fediverse_hashtag(tag):
    """Convierte una etiqueta del post en un hashtag válido."""
    raw = unicodedata.normalize('NFKC', str(tag or '')).strip().lstrip('#')
    raw = re.sub(r'\s+', '', raw)
    raw = re.sub(r'[^\w]', '', raw, flags=re.UNICODE)
    return raw[:40]


def build_fediverse_text(title, post_url, tags, max_chars=500, include_tags=True):
    """Arma el texto del estado: título, enlace y hashtags."""
    title = re.sub(r'\s+', ' ', str(title or '')).strip()
    post_url = str(post_url or '').strip()
    tag_line = ''
    if include_tags:
        seen = set()
        hashtags = []
        for tag in tags or []:
            hashtag = fediverse_hashtag(tag)
            if not hashtag or hashtag.lower() in seen:
                continue
            seen.add(hashtag.lower())
            hashtags.append('#' + hashtag)
        tag_line = ' '.join(hashtags[:6])

    tail = '\n\n'.join(part for part in (post_url, tag_line) if part)
    if title and tail:
        allowed = max_chars - len(tail) - 2
        if allowed >= 10:
            if len(title) > allowed:
                title = title[:max(0, allowed - 1)].rstrip() + '…'
            text = f'{title}\n\n{tail}'
        else:
            text = tail
    elif title:
        text = title[:max_chars]
    else:
        text = tail
    return text[:max_chars].strip()


def publish_to_fediverse(post_metadata, slug):
    """Publica una entrada como estado en la instancia configurada."""
    cfg = get_fediverse_config()
    if not cfg.get('instance') or not cfg.get('access_token'):
        raise FediverseError('Falta configurar la instancia o el token de acceso.')
    if str(post_metadata.get('fediverse_url') or '').strip():
        raise FediverseError('Esta entrada ya está publicada en el fediverso.')

    tags = post_metadata.get('tags') or []
    if isinstance(tags, str):
        tags = [tag.strip() for tag in tags.split(',') if tag.strip()]

    post_url = url_for('post', slug=slug, _external=True)
    max_chars = fediverse_max_chars(cfg) or cfg.get('max_chars') or 500
    text = build_fediverse_text(
        post_metadata.get('title'),
        post_url,
        tags,
        max_chars=max_chars,
        include_tags=cfg.get('include_tags', True),
    )
    if not text:
        raise FediverseError('El estado quedó vacío; revisá el título y el enlace.')

    idempotency_key = hashlib.sha256(
        f"{cfg['instance']}|{slug}|{post_metadata.get('date', '')}".encode('utf-8')
    ).hexdigest()
    resp = fediverse_request(
        cfg,
        'POST',
        '/api/v1/statuses',
        headers={'Idempotency-Key': idempotency_key},
        json={
            'status': text,
            'visibility': cfg.get('visibility', 'public'),
            'language': cfg.get('language', 'es'),
        },
    )
    if resp.status_code not in (200, 201):
        raise FediverseError(fediverse_error_message(resp))
    try:
        data = resp.json()
    except ValueError as exc:
        raise FediverseError('La instancia devolvió una respuesta inválida.') from exc
    if not data.get('url'):
        raise FediverseError('La instancia no devolvió la URL del estado publicado.')
    return data


def list_themes():
    themes = []
    if not os.path.isdir(THEMES_DIR): return ['default']
    for name in sorted(os.listdir(THEMES_DIR)):
        full = os.path.join(THEMES_DIR, name)
        if os.path.isdir(full) and os.path.exists(os.path.join(full, 'theme.css')):
            meta = {'name': name}
            tj = os.path.join(full, 'theme.json')
            if os.path.exists(tj):
                try: meta.update(json.load(open(tj, encoding='utf-8')))
                except: pass
            themes.append(meta)
    return themes or [{'name': 'default'}]

def valid_theme(name):
    if not name: return 'default'
    name = re.sub(r'[^a-zA-Z0-9_-]', '', str(name))
    if os.path.exists(os.path.join(THEMES_DIR, name, 'theme.css')): return name
    return 'default'

def sanitize_css(css):
    if not css: return ''
    low = css.lower()
    for bad in ['expression(', 'javascript:', '@import', 'behavior:', '-moz-binding']:
        if bad in low: return ''
    if len(css) > 20000: return css[:20000]
    return css

def theme_vars_to_css(vars_dict):
    if not vars_dict: return ''
    lines = [':root{']
    for k, v in vars_dict.items():
        kk = re.sub(r'[^a-z0-9-]', '', str(k).lower())[:30]
        vv = str(v).strip()[:50]
        if not re.match(r'^#[0-9a-fA-F]{3,8}$', vv) and not re.match(r'^[a-z]+$', vv): continue
        lines.append(f'--{kk}:{vv};')
    lines.append('}')
    return '\n'.join(lines)

def get_pages():
    pages = []
    os.makedirs(PAGES_DIR, exist_ok=True)
    for fn in os.listdir(PAGES_DIR):
        if fn.endswith('.md'):
            try:
                post = frontmatter.load(os.path.join(PAGES_DIR, fn))
                m = dict(post.metadata); m['slug'] = fn[:-3]
                pages.append(m)
            except: pass
    return sorted(pages, key=lambda x: str(x.get('title','')))


# --- PÁGINAS (editor markdown, igual que posts) ---
@app.route('/p/<slug>')
def page_view(slug):
    slug = safe_slug(slug)
    path = os.path.join(PAGES_DIR, f"{slug}.md")
    if not os.path.exists(path): abort(404)
    pg = frontmatter.load(path)
    content_html = sanitize_html(markdown(pg.content, extensions=['tables', 'fenced_code', 'nl2br']))
    return render_template('page.html', page=pg.metadata, content=content_html, slug=slug)

@app.route('/admin/pages')
@login_required
def admin_pages():
    return render_template('admin_pages.html', pages=get_pages())

@app.route('/admin/pages/new', methods=['GET', 'POST'], defaults={'slug': None})
@app.route('/admin/pages/edit/<slug>', methods=['GET', 'POST'])
@login_required
def edit_page(slug):
    if slug: slug = safe_slug(slug)
    if request.method == 'POST':
        title = request.form.get('title', '').strip() or 'Sin título'
        content = request.form.get('content', '')
        new_slug = safe_slug(request.form.get('slug') or (slug.replace('draft_', '') if slug else slugify(title))) or 'pagina'
        status = request.form.get('status', 'published')
        description = request.form.get('description', '').strip()[:300]
        show_in_nav = request.form.get('show_in_nav', '1') == '1'
        os.makedirs(PAGES_DIR, exist_ok=True)
        fname = f"draft_{new_slug}.md" if status == 'draft' else f"{new_slug}.md"
        new_path = os.path.join(PAGES_DIR, fname)
        if slug:
            for cand in (f"{slug}.md", f"draft_{slug}.md"):
                old = os.path.join(PAGES_DIR, cand)
                if old != new_path and os.path.exists(old): os.remove(old)
        pf = frontmatter.Post(content)
        pf.metadata.update({'title': title, 'status': status, 'description': description, 'show_in_nav': show_in_nav})
        with open(new_path, 'wb') as f: frontmatter.dump(pf, f)
        return redirect(url_for('admin_pages'))
    data = {'title': '', 'content': '', 'slug': '', 'status': 'published', 'description': '', 'show_in_nav': True}
    if slug:
        for cand in (f"{slug}.md", f"draft_{slug}.md"):
            path = os.path.join(PAGES_DIR, cand)
            if os.path.exists(path):
                pg = frontmatter.load(path)
                data = {'title': pg.metadata.get('title',''), 'content': pg.content,
                        'slug': slug.replace('draft_',''),
                        'status': 'draft' if cand.startswith('draft_') else 'published',
                        'description': pg.metadata.get('description',''),
                        'show_in_nav': pg.metadata.get('show_in_nav', True)}
                break
    return render_template('page_builder.html', page=data)

@app.route('/admin/pages/delete/<slug>', methods=['POST'])
@login_required
def delete_page(slug):
    slug = safe_slug(slug)
    for cand in (f"{slug}.md", f"draft_{slug}.md"):
        path = os.path.join(PAGES_DIR, cand)
        if os.path.exists(path): os.remove(path)
    return redirect(url_for('admin_pages'))

# --- APARIENCIA / TEMAS ---
@app.route('/theme/generated.css')
def theme_generated_css():
    """CSS generado desde config.json (colores y CSS personalizado).

    Se sirve dinámicamente para que funcione también con el contenedor
    read-only de producción, donde static/themes no es escribible.
    """
    cfg = get_config()
    parts = [
        theme_vars_to_css(cfg.get('theme_vars') or {}),
        sanitize_css(cfg.get('custom_css') or '')
    ]
    css = "\n".join(part for part in parts if part)
    return Response(css, mimetype='text/css', headers={'Cache-Control': 'no-store'})

@app.route('/admin/apariencia', methods=['GET', 'POST'])
@login_required
def appearance():
    cfg = get_config()
    if request.method == 'POST':
        action = request.form.get('action', 'theme')
        if action == 'theme':
            cfg['theme'] = valid_theme(request.form.get('theme'))
            cfg['site_title'] = request.form.get('site_title', cfg.get('site_title',''))[:80]
            cfg['banner'] = re.sub(r'[^a-zA-Z0-9._-]', '', request.form.get('banner','banner.png') or 'banner.png')[:100]
            vars_dict = {}
            for k in ('accent-primary', 'bg-main', 'text-primary'):
                v = request.form.get(f'var_{k}', '').strip()[:20]
                if re.match(r'^#[0-9a-fA-F]{3,8}$', v): vars_dict[k] = v
            cfg['theme_vars'] = vars_dict
            try:
                links = json.loads(request.form.get('nav_links_json', '[]'))
                clean_links = []
                for l in links[:10]:
                    if isinstance(l, dict) and l.get('label') and l.get('url'):
                        clean_links.append({'label': str(l['label'])[:30], 'url': str(l['url'])[:200]})
                cfg['nav_links'] = clean_links
            except: pass
        elif action == 'css':
            css = sanitize_css(request.form.get('custom_css', ''))
            if css or not request.form.get('custom_css'):
                cfg['custom_css'] = css
        save_config(cfg)
        return redirect(url_for('appearance'))
    themes = list_themes()
    return render_template('appearance.html', cfg=cfg, themes=themes)

if __name__ == '__main__':
    # DEBUG solo si se pide explícitamente (NUNCA en producción)
    debug = os.environ.get('FLASK_DEBUG', '').strip() == '1'
    app.run(host='0.0.0.0', port=5000, debug=debug)
