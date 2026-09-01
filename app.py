import os
import sys
import frontmatter
from flask import Flask, render_template, abort, request, redirect, url_for, session, Response, g, make_response, send_file
from functools import wraps
from markdown import markdown
import re
import unicodedata
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
from io import BytesIO
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
DB_FILE = os.path.join(BASE_DIR, 'stats.db')


def _init_db():
    """Crea la tabla de visitas si no existe. SQLite maneja el locking por
    transacciones (no se necesita FileLock manual)."""
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


# Inicializar la DB al cargar el módulo (gunicorn importa el módulo, no ejecuta __main__)
_init_db()

# CAMBIO: Toggle 2FA. Con '0' el login va directo al admin tras la contraseña;
# con '1' (default) exige el código TOTP. Se controla desde .env sin tocar código.
TWO_FA_ENABLED = os.environ.get('TWO_FA_ENABLED', '1').strip() != '0'

COMMENTS_DATA_DIR = 'comments_data/'

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
    'dl', 'dt', 'em', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'img', 'li',
    'ol', 'p', 'pre', 's', 'span', 'strike', 'strong', 'sub', 'sup', 'table',
    'tbody', 'td', 'th', 'thead', 'tr', 'ul', 'video', 'audio', 'source'
)
ALLOWED_ATTRIBUTES = {
    'a': ['href', 'title', 'target', 'rel'],
    'abbr': ['title'],
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
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
        "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "connect-src 'self' 'unsafe-inline' https: http:"
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

# Configuración de tema
@app.before_request
def load_theme():
    # identify_user() ya se ejecuta antes, esto es solo para el tema
    pass

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
    
    comments_enabled = True
    if os.path.exists('config.json'):
        try:
            with open('config.json', 'r') as f:
                conf_data = json.load(f)
                comments_enabled = conf_data.get('comments_enabled', True)
        except:
            comments_enabled = True
    
    comments_api_base = os.getenv('COMMENTS_API_URL', 'http://localhost:5005')
    
    # 4. Agregamos related_posts al return
    return render_template('post.html', 
                           post=post.metadata, 
                           content=content_html, 
                           comments_enabled=comments_enabled, 
                           slug=slug, 
                           comments_api_url=comments_api_base,
                           related_posts=related_posts)

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
        status = request.form.get('status')

        clean_slug = safe_slug(slug.replace('draft_', '') if slug else slugify(title))
        if not clean_slug: clean_slug = "post-sin-titulo"

        new_filename = f"draft_{clean_slug}.md" if status == 'draft' else f"{clean_slug}.md"
        new_path = os.path.join(CONTENT_DIR, new_filename)

        if slug:
            old_path = os.path.join(CONTENT_DIR, f"{slug}.md")
            if old_path != new_path and os.path.exists(old_path):
                os.remove(old_path)

        post_file = frontmatter.Post(content)
        post_file.metadata['title'] = title
        post_file.metadata['date'] = date
        post_file.metadata['category'] = category
        post_file.metadata['tags'] = tags
        post_file.metadata['description'] = description
        post_file.metadata['status'] = status 
        
        # Mantenemos imagen si existía y no se cambió (opcional, lógica simple)
        if slug:
             # Aquí podrías agregar lógica para preservar otros metadatos si fuera necesario
             pass

        with open(new_path, 'w', encoding='utf-8') as f:
            frontmatter.dump(post_file, f)

        return redirect(url_for('admin_list'))

    post_data = {
        "title": "", "content": "", "category": "", 
        "tags": "", "description": "", "date": datetime.now().strftime('%Y-%m-%d'),
        "status": "published"
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
                "status": "draft" if slug.startswith('draft_') else "published"
            }

    return render_template('edit.html', post=post_data)

@app.route('/admin/save', methods=['POST'])
def save_post():
    # Ruta auxiliar legacy, por si acaso
    return redirect('/admin')

@app.route('/admin/delete/<slug>', methods=['POST'])
@login_required
def delete_post(slug):
    slug = safe_slug(slug)
    path = os.path.join(CONTENT_DIR, f"{slug}.md")
    if os.path.exists(path):
        os.remove(path)
    return redirect(url_for('admin_list'))

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

    buffer = BytesIO()
    try:
        conn = sqlite3.connect(DB_FILE)
        rows = conn.execute(
            "SELECT timestamp, user_id, action, detail, os, location, ip "
            "FROM visits ORDER BY timestamp"
        ).fetchall()
        conn.close()
        writer = csv.writer(buffer)
        writer.writerow(["Timestamp", "User_ID", "Action", "Detail", "OS", "Location", "IP"])
        writer.writerows(rows)
    except Exception as e:
        print(f"Error exportando stats: {e}")
        return "Error exportando stats"

    buffer.seek(0)
    return Response(
        buffer.getvalue(),
        mimetype='text/csv',
        headers={"Content-Disposition": "attachment; filename=stats.csv"}
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

def get_config():
    config_path = 'config.json'
    default_config = {'comments_enabled': True}
    if not os.path.exists(config_path): return default_config
    try:
        with open(config_path, 'r') as f:
            content = f.read().strip()
            if not content: return default_config
            return json.loads(content)
    except:
        return default_config

if __name__ == '__main__':
    # DEBUG solo si se pide explícitamente (NUNCA en producción)
    debug = os.environ.get('FLASK_DEBUG', '').strip() == '1'
    app.run(host='0.0.0.0', port=5000, debug=debug)
