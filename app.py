import os
import frontmatter
from flask import Flask, render_template, abort, request, redirect, url_for, session, Response, g, make_response, send_file
from functools import wraps
from markdown import markdown
import re
import unicodedata
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
import zipfile
import json
import glob
from dotenv import load_dotenv
from filelock import FileLock
import csv
import uuid
import requests
from collections import Counter
import pyotp
import qrcode
from io import BytesIO
import base64

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TOTP_FILE = os.path.join(BASE_DIR, '.totp_secret')

# CAMBIO 1: Archivo CSV en lugar de JSON
STATS_FILE = os.path.join(BASE_DIR, 'stats.csv') 
LOCK_FILE = os.path.join(BASE_DIR, 'stats.lock')

COMMENTS_DATA_DIR = 'comments_data/'
load_dotenv() 

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default-key-for-dev')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin')

CONTENT_DIR = "content"
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'mp3', 'mp4'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

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

def analyze_csv_stats():
    """Lee el CSV y reconstruye los diccionarios para el Admin deduplicando y filtrando bots"""
    stats = {
        'daily': Counter(),
        'posts': Counter(),
        'os': Counter(),
        'location': Counter(),
        'total': 0
    }
    
    if not os.path.exists(STATS_FILE):
        return stats

    seen_visits = set()
    ignored_paths = ['/login', '/login/2fa', '/logout', '/rss.xml', '/favicon.ico']

    try:
        with open(STATS_FILE, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                user_id = row['User_ID']
                detail = row['Detail']
                timestamp_str = row['Timestamp']
                
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
                stats['os'][row.get('OS', 'Otro')] += 1
                stats['location'][row.get('Location', 'Desconocido')] += 1
                
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
    # 1. Guardar Cookie si es nuevo
    if getattr(g, 'is_new_user', False):
        response.set_cookie('user_id', g.user_id, max_age=31536000) # 1 año

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
    
    # 4. Escribir en CSV (Con Lock por seguridad)
    lock = FileLock(LOCK_FILE)
    try:
        with lock:
            file_exists = os.path.exists(STATS_FILE)
            with open(STATS_FILE, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(["Timestamp", "User_ID", "Action", "Detail", "OS", "Location", "IP"])
                
                writer.writerow([
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    g.user_id,
                    "VIEW",
                    path_detail,
                    os_name,
                    location,
                    ip # Guardamos IP por seguridad, pero no la mostramos si no quieres
                ])
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

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Configuración de tema
@app.before_request
def load_theme():
    # identify_user() ya se ejecuta antes, esto es solo para el tema
    pass

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # Validar Contraseña primero
        if request.form.get('password') == os.environ.get('ADMIN_PASSWORD'):
            # NO logueamos todavía. Marcamos "pre-autenticación" en la sesión
            session['pre_auth'] = True
            return redirect(url_for('login_2fa'))
        else:
            return render_template('login.html', error="Contraseña incorrecta")
            
    return render_template('login.html')

@app.route('/login/2fa', methods=['GET', 'POST'])
def login_2fa():
    # Seguridad: Si no puso la contraseña bien antes, afuera.
    if not session.get('pre_auth'):
        return redirect(url_for('login'))

    # Verificamos si ya existe una configuración 2FA guardada
    totp_secret = None
    first_time = False
    
    if os.path.exists(TOTP_FILE):
        with open(TOTP_FILE, 'r') as f:
            totp_secret = f.read().strip()
    else:
        # PRIMERA VEZ: Generamos secreto nuevo
        first_time = True
        if 'temp_secret' not in session:
            session['temp_secret'] = pyotp.random_base32()
        totp_secret = session['temp_secret']

    # Objeto TOTP
    totp = pyotp.TOTP(totp_secret)

    if request.method == 'POST':
        code = request.form.get('code')
        
        # Validar código
        if totp.verify(code):
            # ¡ÉXITO!
            
            # Si era la primera vez, GUARDAMOS el secreto permanentemente ahora
            if first_time:
                with open(TOTP_FILE, 'w') as f:
                    f.write(totp_secret)
                session.pop('temp_secret', None) # Limpieza

            # Limpiamos pre_auth y damos acceso full
            session.pop('pre_auth', None)
            session['logged_in'] = True
            
            resp = make_response(redirect(url_for('admin_list')))
            resp.set_cookie('is_admin', 'true', max_age=30*24*60*60)
            return resp
        else:
            return render_template('login_2fa.html', error="Código incorrecto", qr_data=None)

    # Lógica GET (Mostrar formulario)
    qr_b64 = None
    if first_time:
        # Generar QR para escanear
        uri = totp.provisioning_uri(name='Admin', issuer_name='NeoCMS')
        img = qrcode.make(uri)
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        qr_b64 = base64.b64encode(buffered.getvalue()).decode('utf-8')

    return render_template('login_2fa.html', first_time=first_time, qr_code=qr_b64)

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
    path = os.path.join(CONTENT_DIR, f"{slug}.md")
    if not os.path.exists(path):
        abort(404)

    post = frontmatter.load(path)
    content_html = markdown(post.content, extensions=['tables', 'fenced_code', 'nl2br'])
    
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
    
    # CAMBIO: Usamos el analizador de CSV
    stats_data = analyze_csv_stats()
    
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
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        content = request.form.get('content', '')
        date = request.form.get('date') or datetime.now().strftime('%Y-%m-%d')
        category = request.form.get('category', '').strip() or "Sin Categoría"
        tags = request.form.get('tags', '').strip()
        description = request.form.get('description', '').strip()
        status = request.form.get('status')

        clean_slug = slug.replace('draft_', '') if slug else slugify(title)
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

        with open(new_path, 'wb') as f:
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
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        return {"url": f"/static/uploads/{filename}"}, 200
    return {"error": "File type not allowed"}, 400

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
        # Agregamos también el CSV de stats al backup
        if os.path.exists(STATS_FILE):
             zipf.write(STATS_FILE, 'stats.csv')

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
    rss_xml += '<description>Últimas entradas de NeoSite Blog</description>'
    
    base_url = request.url_root.rstrip('/')
    
    for post in published_posts:
        content = post.get('content', '').replace('src="/static/', 'src="https://blog.neosite.com.ar/static/')
        post_url = f"{base_url}/post/{post['slug']}"
        
        rss_xml += '<item>'
        rss_xml += f'<title>{post["title"]}</title>'
        rss_xml += f'<link>{post_url}</link>'
        rss_xml += f'<guid>{post_url}</guid>'
        rss_xml += f'<pubDate>{post["date"]}</pubDate>'
        rss_xml += f'<description><![CDATA[{content}]]></description>'
        rss_xml += '</item>'
    
    rss_xml += '</channel></rss>'
    return Response(rss_xml, mimetype='application/rss+xml')

@app.route('/admin/export-stats')
@login_required
def export_stats():
    # Devuelve el CSV en bruto
    if os.path.exists(STATS_FILE):
        return send_file(STATS_FILE, as_attachment=True)
    return "No stats yet"
     
@app.route('/admin/stats')
@login_required
def full_stats():
    # CAMBIO: Analizar CSV completo
    stats_data = analyze_csv_stats()
    
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
    file_path = os.path.join(COMMENTS_DATA_DIR, f"{slug}.json")
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            comments = json.load(f)
        
        new_comments = [c for c in comments if c['id'] != comment_id]
        
        with open(file_path, 'w') as f:
            json.dump(new_comments, f, indent=4)
            
    return redirect(url_for('admin_comments'))

@app.route('/admin/settings/toggle-comments')
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
    app.run(host='0.0.0.0', port=5000, debug=True)