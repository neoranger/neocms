import os
import frontmatter
from flask import Flask, render_template, abort, request, redirect, url_for, session, Response
from flask import send_file
from flask import make_response
from functools import wraps
from markdown import markdown
import re
import unicodedata
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
import zipfile
import json

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
STATS_FILE = os.path.join(BASE_DIR, 'stats.json')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default-key-for-dev')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin')

CONTENT_DIR = "content"

UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'mp3', 'mp4'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

def get_posts():
    posts = []
    for filename in os.listdir(CONTENT_DIR):
        if filename.endswith(".md"):
            path = os.path.join(CONTENT_DIR, filename)
            post = frontmatter.load(path)
            # Guardamos el slug (nombre del archivo sin .md) para la URL
            post.metadata['slug'] = filename[:-3]
            posts.append(post.metadata)
    return sorted(posts, key=lambda x: x.get('date', ''), reverse=True)

def slugify(text):
    text = text.lower()
    return re.sub(r'[^a-z0-9]+', '-', text).strip('-')

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Decorador para proteger rutas
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def log_visit(path='home'):
    if request.cookies.get('is_admin'):
        return

    stats = {'daily': {}, 'posts': {}}
    
    # Verificamos que exista Y que sea un archivo
    if os.path.exists(STATS_FILE) and os.path.isfile(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f:
                content = f.read()
                if content: # Si el archivo no está vacío
                    stats = json.loads(content)
        except (json.JSONDecodeError, Exception):
            # Si el archivo está corrupto, empezamos de cero
            stats = {'daily': {}, 'posts': {}}
    
    today = datetime.now().strftime('%Y-%m-%d')
    
    # Inicializar estructuras si no existen en el JSON cargado
    if 'daily' not in stats: stats['daily'] = {}
    if 'posts' not in stats: stats['posts'] = {}

    stats['daily'][today] = stats['daily'].get(today, 0) + 1
    if path != 'home':
        stats['posts'][path] = stats['posts'].get(path, 0) + 1
    
    with open(STATS_FILE, 'w') as f:
        json.dump(stats, f, indent=4) # indent=4 para que sea legible



# Asegúrate de que Flask sepa qué tema cargar al inicio
@app.before_request
def load_theme():
    if 'theme' in session:
        # Esto es solo para que Jinja2 pueda usar 'session.get('theme')'
        # el CSS ya maneja la clase 'dark-mode'
        pass

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password') == os.environ.get('ADMIN_PASSWORD'):
            session['logged_in'] = True
            # 1. Creamos la respuesta primero
            resp = make_response(redirect(url_for('admin_list')))

            # 2. Le asignamos la cookie en una línea separada
            resp.set_cookie('is_admin', 'true', max_age=30*24*60*60)

            # 3. Retornamos el objeto de respuesta completo
            return resp

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('index'))

@app.route('/')
def index():
    log_visit() # Registramos la visita al cargar el home
    query = request.args.get('q', '').lower()
    tag_filter = request.args.get('tag', '').lower()
    cat_filter = request.args.get('category', '').lower()

    # Parámetro de paginación
    page = request.args.get('page', 1, type=int)
    per_page = 8

    all_posts = []
    categories_count = {}
    tags_set = set()

    if not os.path.exists(CONTENT_DIR): os.makedirs(CONTENT_DIR)

    for filename in os.listdir(CONTENT_DIR):
        if filename.startswith("draft_"):
            continue
        if filename.endswith(".md"):
            p_file = frontmatter.load(os.path.join(CONTENT_DIR, filename))
            metadata = p_file.metadata
            metadata['slug'] = filename[:-3]

            # Cálculo de tiempo de lectura (200 palabras por minuto)
            words = len(p_file.content.split())
            metadata['read_time'] = max(1, round(words / 200))

            # Procesar Categoría y Tags para Sidebar
            cat = metadata.get('category', 'Sin Categoría')
            categories_count[cat] = categories_count.get(cat, 0) + 1

            post_tags = [t.strip().lower() for t in str(metadata.get('tags', '')).split(',')] if metadata.get('tags') else []
            for t in post_tags: tags_set.add(t)
            metadata['tags_list'] = post_tags

            # Lógica de Filtrado
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

    # Lógica de paginación
    total_posts = len(all_posts)
    start = (page - 1) * per_page
    end = start + per_page
    paginated_posts = all_posts[start:end]

    # Calcular si hay página siguiente o anterior
    has_next = end < total_posts
    has_prev = page > 1

    return render_template('index.html', 
                           posts=paginated_posts, # Enviamos solo los 8 de la página
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
    return render_template('post.html', post=post.metadata, content=content_html)

@app.route('/admin')
@login_required
def admin_list():
    posts = get_posts()
    stats = {'daily': {}, 'posts': {}} # Valor por defecto
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f:
                content = f.read().strip()
                if content: # Solo intentamos cargar si hay texto
                    stats = json.loads(content)
        except json.JSONDecodeError:
            stats = {'daily': {}, 'posts': {}} # Si el JSON está mal, usamos el vacío
    # Obtener el top 3 de posts más leídos
    post_stats = stats.get('posts', {})
    top_posts = sorted(post_stats.items(), key=lambda item: item[1], reverse=True)[:3]
    daily_stats = stats.get('daily', {})
    sorted_daily = dict(sorted(daily_stats.items(), reverse=True)[:7])
    sorted_stats = dict(sorted(daily_stats.items(), reverse=True)[:7])
    daily_visits = stats.get('daily', {})
    # Obtenemos las visitas de los últimos 7 días
    last_7_days = []
    for i in range(6, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d')
        last_7_days.append({'date': d[-2:], 'count': daily_visits.get(d, 0)})
    
    # Calculamos el máximo para la escala (mínimo 1 para evitar división por cero)
    max_visits = max([day['count'] for day in last_7_days] + [1])
    return render_template('admin.html', posts=posts, stats=sorted_stats,top_posts=top_posts,stats_days=last_7_days, max_visits=max_visits)

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
        status = request.form.get('status') # 'draft' o 'published'

        # --- LÓGICA DE NOMBRES ---
        # 1. Limpiamos el slug base (quitamos 'draft_' si ya lo tiene para procesar el nombre limpio)
        clean_slug = slug.replace('draft_', '') if slug else slugify(title)
        if not clean_slug: clean_slug = "post-sin-titulo"

        # 2. Definimos el nuevo nombre según el estado elegido en el formulario
        new_filename = f"draft_{clean_slug}.md" if status == 'draft' else f"{clean_slug}.md"
        new_path = os.path.join(CONTENT_DIR, new_filename)

        # 3. Si estamos editando y el nombre cambió (ej: pasó de borrador a público), borramos el viejo
        if slug:
            old_path = os.path.join(CONTENT_DIR, f"{slug}.md")
            if old_path != new_path and os.path.exists(old_path):
                os.remove(old_path)

        # 4. Crear objeto frontmatter y asignar metadatos
        post_file = frontmatter.Post(content)
        post_file.metadata['title'] = title
        post_file.metadata['date'] = date
        post_file.metadata['category'] = category
        post_file.metadata['tags'] = tags
        post_file.metadata['description'] = description
        # Guardamos el estado también en el metadata por si lo necesitas luego
        post_file.metadata['status'] = status 

        # 5. Guardar físicamente
        with open(new_path, 'wb') as f:
            frontmatter.dump(post_file, f)

        return redirect(url_for('admin_list'))

    # --- LÓGICA GET ---
    post_data = {
        "title": "", "content": "", "category": "", 
        "tags": "", "description": "", "date": datetime.now().strftime('%Y-%m-%d'),
        "status": "published" # Por defecto
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
    title = request.form.get('title')
    content = request.form.get('content')
    status = request.form.get('status') # 'published' o 'draft'
    
    filename = secure_filename(title) + ".md"
    
    if status == 'draft':
        path = os.path.join('posts', 'drafts', filename)
    else:
        path = os.path.join('posts', filename)
        
    with open(path, 'w') as f:
        f.write(content)
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
        # Devolvemos la URL para que el JS la use
        return {"url": f"/static/uploads/{filename}"}, 200
    return {"error": "File type not allowed"}, 400

# Modificamos get_posts para que devuelva el contenido completo
def get_all_posts_with_content():
    posts = []
    for filename in os.listdir(CONTENT_DIR):
        if filename.endswith(".md"):
            path = os.path.join(CONTENT_DIR, filename)
            post = frontmatter.load(path)
            post.metadata['slug'] = filename[:-3]
            # Incluir el contenido para buscar
            post.metadata['full_content'] = post.content 
            posts.append(post)
    return sorted(posts, key=lambda x: x.metadata.get('date', ''), reverse=True)

@app.route('/admin/backup')
@login_required
def backup():
    backup_filename = f"cms_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    backup_path = os.path.join('/tmp', backup_filename) # Guardamos temporalmente

    with zipfile.ZipFile(backup_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        # Añadir posts
        for root, _, files in os.walk(CONTENT_DIR):
            for file in files:
                zipf.write(os.path.join(root, file), os.path.relpath(os.path.join(root, file), os.path.join(app.root_path, 'content')))

        # Añadir uploads
        for root, _, files in os.walk(UPLOAD_FOLDER):
            for file in files:
                zipf.write(os.path.join(root, file), os.path.relpath(os.path.join(root, file), os.path.join(app.root_path, 'static')))

    return send_file(backup_path, as_attachment=True, download_name=backup_filename)

@app.route('/rss.xml')
def rss():
    posts = get_posts()
    # Filtramos para que solo aparezcan los publicados
    published_posts = [p for p in posts if p.get('published', True)]
    
    # Ordenar por fecha (asumiendo formato YYYY-MM-DD)
    published_posts.sort(key=lambda x: x['date'], reverse=True)

    rss_xml = '<?xml version="1.0" encoding="UTF-8" ?>'
    rss_xml += '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
    rss_xml += '<channel>'
    rss_xml += '<title>Blog Title</title>'
    rss_xml += '<link>https://example.com</link>'
    rss_xml += '<description>Últimas entradas de Blog</description>'
    
    base_url = request.url_root.rstrip('/')
    
    for post in published_posts:
        # Limpiamos un poco el contenido si es necesario o lo enviamos tal cual
        content = post.get('content', '').replace('src="/static/', 'src="https://example.com/static/')
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
    return send_file(STATS_FILE, as_attachment=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
