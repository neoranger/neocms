import os
import re
import json
import requests
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from datetime import datetime

app = Flask(__name__)

# CAMBIO SEGURIDAD: CORS restringido solo al dominio del blog (no "*")
ALLOWED_ORIGIN = os.environ.get('ALLOWED_ORIGIN', '').strip()

if ALLOWED_ORIGIN:
    CORS(app, resources={r"/comments/*": {"origins": [ALLOWED_ORIGIN]}})
else:
    # Sin un origen configurado, NO habilitamos CORS (bloquea cross-origin abusivo).
    # Configura ALLOWED_ORIGIN con el dominio de tu blog, p.ej. https://blog.tudominio.com
    CORS(app, resources={r"/comments/*": {"origins": []}})

COMMENTS_DIR = 'data/comments'
os.makedirs(COMMENTS_DIR, exist_ok=True)


def get_post_file(slug):
    # Sanitizar el slug para evitar path traversal
    slug = os.path.basename(str(slug))
    slug = re.sub(r'[^a-zA-Z0-9_-]', '', slug)
    return os.path.join(COMMENTS_DIR, f"{slug}.json")


def send_telegram_notification(author, text, slug):
    """Notifica al administrador por Telegram sobre un comentario nuevo a aprobar."""
    token = os.getenv('TELEGRAM_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": f"📝 *Nuevo comentario a aprobar*\n*De:* {author}\n*Post:* {slug}\n*Dice:* {text}",
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"STATUS TELEGRAM: {r.status_code}")
    except Exception as e:
        print(f"ERROR DE RED (TELEGRAM): {e}")


@app.route('/comments/<slug>', methods=['GET'])
def get_comments(slug):
    file_path = get_post_file(slug)
    if not os.path.exists(file_path):
        return jsonify([])

    with open(file_path, 'r') as f:
        comments = json.load(f)
    # Solo devolver comentarios aprobados en el front
    return jsonify([c for c in comments if c.get('approved', False)])


@app.route('/comments/<slug>', methods=['POST'])
def add_comment(slug):
    data = request.json
    if not data or not data.get('author') or not data.get('text'):
        return jsonify({"error": "Faltan campos"}), 400

    new_comment = {
        "id": datetime.now().timestamp(),
        "author": data['author'],
        "text": data['text'],
        "date": datetime.now().strftime('%Y-%m-%d %H:%M'),
        "approved": False  # Por defecto, requiere moderación
    }

    file_path = get_post_file(slug)
    comments = []
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            comments = json.load(f)

    comments.append(new_comment)

    with open(file_path, 'w') as f:
        json.dump(comments, f, indent=4)

    # Notificar por Telegram (aviso en segundo plano de los nuevos comentarios)
    try:
        send_telegram_notification(data['author'], data['text'], slug)
    except Exception as e:
        print(f"ERROR NOTIFICANDO: {e}")

    return jsonify({"message": "Comentario enviado, pendiente de aprobación"}), 201


if __name__ == '__main__':
    # DEBUG solo si se pide explícitamente
    debug = os.environ.get('FLASK_DEBUG', '').strip() == '1'
    app.run(host='0.0.0.0', port=5001, debug=debug)
