from flask import Flask, request, jsonify
from flask_cors import CORS
import json
import os
from datetime import datetime

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}) # Importante para que el CMS pueda consultar la API desde el navegador

COMMENTS_DIR = 'data/comments'
os.makedirs(COMMENTS_DIR, exist_ok=True)

def get_post_file(slug):
    return os.path.join(COMMENTS_DIR, f"{slug}.json")

@app.route('/comments/<slug>', methods=['GET'])
def get_comments(slug):
    file_path = get_post_file(slug)
    if not os.path.exists(file_path):
        return jsonify([])
    
    with open(file_path, 'r') as f:
        return jsonify(json.load(f))

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
        "approved": False # Por defecto, requiere moderación
    }

    file_path = get_post_file(slug)
    comments = []
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            comments = json.load(f)
    
    comments.append(new_comment)
    
    with open(file_path, 'w') as f:
        json.dump(comments, f, indent=4)

    return jsonify({"message": "Comentario enviado, pendiente de aprobación"}), 201

@app.after_request
def add_cors_headers(response):
    # Permitir específicamente tu blog
    response.headers['Access-Control-Allow-Origin'] = 'https://tublog.example.com'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
