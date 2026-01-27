from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
from pymongo import MongoClient
from bson.objectid import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import os
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'abc123')
CORS(app)

# MongoDB Connection
MONGO_URI = os.environ.get('MONGO_API_KEY')
client = MongoClient(MONGO_URI)
db = client['contenthub']

# Collections
users_collection = db['users']
categories_collection = db['categories']
content_collection = db['content']
pages_collection = db['pages']

# Helper function to get accessible categories for sidebar
def get_accessible_categories():
    """Get categories accessible to current user, sorted paid first then free, alphabetically"""
    if 'user_id' not in session:
        return []

    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    if not user:
        return []

    is_admin = user.get('is_admin', False)
    is_subscribed = user.get('is_subscribed', False)

    # Get all categories
    all_categories = list(categories_collection.find())

    # Filter based on access
    if is_admin or is_subscribed:
        accessible = all_categories
    else:
        accessible = [c for c in all_categories if c.get('is_free', False)]

    # Separate into paid and free
    paid_categories = sorted(
        [c for c in accessible if not c.get('is_free', False)],
        key=lambda x: x['name'].lower()
    )
    free_categories = sorted(
        [c for c in accessible if c.get('is_free', False)],
        key=lambda x: x['name'].lower()
    )

    # Combine: paid first, then free
    return paid_categories + free_categories

# Context processor to make categories available in all templates
@app.context_processor
def inject_categories():
    if 'user_id' in session:
        return {'sidebar_categories': get_accessible_categories()}
    return {'sidebar_categories': []}

# Authentication decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
        if not user or not user.get('is_admin', False):
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

# Routes
@app.route('/')
def index():
    page_data = pages_collection.find_one({'page_name': 'home'})
    if not page_data:
        # Create default home page
        page_data = {
            'page_name': 'home',
            'accent_color': '#6366f1',
            'title': 'Welcome to ContentHub',
            'description': 'Subscribe to access premium content',
            'preview_image': ''
        }
        pages_collection.insert_one(page_data)

    return render_template('index.html', page=page_data)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.json
        username = data.get('username')
        password = data.get('password')

        user = users_collection.find_one({'username': username})

        if user and check_password_hash(user['password'], password):
            session['user_id'] = str(user['_id'])
            session['username'] = user['username']
            session['is_admin'] = user.get('is_admin', False)
            session['is_subscribed'] = user.get('is_subscribed', False)
            return jsonify({'success': True, 'is_admin': user.get('is_admin', False)})

        return jsonify({'success': False, 'message': 'Invalid credentials'}), 401

    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        data = request.json
        username = data.get('username')
        password = data.get('password')

        # Check if username already exists
        if users_collection.find_one({'username': username}):
            return jsonify({'success': False, 'message': 'Username already exists'}), 400

        # Create new user
        user_data = {
            'username': username,
            'password': generate_password_hash(password),
            'is_admin': False,
            'is_subscribed': False,
            'created_at': datetime.utcnow()
        }

        result = users_collection.insert_one(user_data)
        session['user_id'] = str(result.inserted_id)
        session['username'] = username
        session['is_admin'] = False
        session['is_subscribed'] = False

        return jsonify({'success': True})

    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/categories')
@login_required
def categories():
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    is_subscribed = user.get('is_subscribed', False)
    is_admin = user.get('is_admin', False)

    # Fetch all categories
    if is_admin or is_subscribed:
        # Show all categories, sorted with paid first
        all_categories = list(categories_collection.find())
        paid_categories = sorted(
            [c for c in all_categories if not c.get('is_free', False)],
            key=lambda x: x['name'].lower()
        )
        free_categories = sorted(
            [c for c in all_categories if c.get('is_free', False)],
            key=lambda x: x['name'].lower()
        )
        categories = paid_categories + free_categories
    else:
        # Show only free categories
        categories = sorted(
            list(categories_collection.find({'is_free': True})),
            key=lambda x: x['name'].lower()
        )

    return render_template('categories.html', categories=categories, is_admin=is_admin)

@app.route('/category/<category_id>')
@login_required
def category_detail(category_id):
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    is_subscribed = user.get('is_subscribed', False)
    is_admin = user.get('is_admin', False)

    category = categories_collection.find_one({'_id': ObjectId(category_id)})

    if not category:
        return "Category not found", 404

    # Check access
    if not is_admin and not is_subscribed and not category.get('is_free', False):
        return "Subscription required", 403

    # Fetch content for this category
    content_items = list(content_collection.find({'category_id': category_id}))

    return render_template('category_detail.html', 
                         category=category, 
                         content_items=content_items,
                         is_admin=is_admin)

@app.route('/admin')
@admin_required
def admin_panel():
    users = list(users_collection.find())
    categories = list(categories_collection.find())
    return render_template('admin.html', users=users, categories=categories)

# API Routes
@app.route('/api/users', methods=['GET'])
@admin_required
def get_users():
    users = list(users_collection.find())
    for user in users:
        user['_id'] = str(user['_id'])
        user.pop('password', None)
    return jsonify(users)

@app.route('/api/users/<user_id>', methods=['PUT'])
@admin_required
def update_user(user_id):
    data = request.json
    update_data = {}

    if 'is_subscribed' in data:
        update_data['is_subscribed'] = data['is_subscribed']
    if 'is_admin' in data:
        update_data['is_admin'] = data['is_admin']

    users_collection.update_one(
        {'_id': ObjectId(user_id)},
        {'$set': update_data}
    )

    return jsonify({'success': True})

@app.route('/api/users/<user_id>', methods=['DELETE'])
@admin_required
def delete_user(user_id):
    users_collection.delete_one({'_id': ObjectId(user_id)})
    return jsonify({'success': True})

@app.route('/api/categories', methods=['GET'])
@login_required
def get_categories():
    categories = list(categories_collection.find())
    for category in categories:
        category['_id'] = str(category['_id'])
    return jsonify(categories)

@app.route('/api/categories', methods=['POST'])
@admin_required
def create_category():
    data = request.json
    category_data = {
        'name': data['name'],
        'description': data.get('description', ''),
        'is_free': data.get('is_free', False),
        'accent_color': data.get('accent_color', '#6366f1'),
        'created_at': datetime.utcnow()
    }

    result = categories_collection.insert_one(category_data)
    return jsonify({'success': True, 'id': str(result.inserted_id)})

@app.route('/api/categories/<category_id>', methods=['PUT'])
@admin_required
def update_category(category_id):
    data = request.json
    update_data = {}

    if 'name' in data:
        update_data['name'] = data['name']
    if 'description' in data:
        update_data['description'] = data['description']
    if 'is_free' in data:
        update_data['is_free'] = data['is_free']
    if 'accent_color' in data:
        update_data['accent_color'] = data['accent_color']

    categories_collection.update_one(
        {'_id': ObjectId(category_id)},
        {'$set': update_data}
    )

    return jsonify({'success': True})

@app.route('/api/categories/<category_id>', methods=['DELETE'])
@admin_required
def delete_category(category_id):
    categories_collection.delete_one({'_id': ObjectId(category_id)})
    content_collection.delete_many({'category_id': category_id})
    return jsonify({'success': True})

@app.route('/api/content', methods=['POST'])
@admin_required
def create_content():
    data = request.json
    content_data = {
        'category_id': data['category_id'],
        'title': data.get('title', ''),
        'text': data.get('text', ''),
        'media_url': data.get('media_url', ''),
        'media_type': data.get('media_type', 'text'),  # text, image, video
        'caption': data.get('caption', ''),
        'created_at': datetime.utcnow()
    }

    result = content_collection.insert_one(content_data)
    return jsonify({'success': True, 'id': str(result.inserted_id)})

@app.route('/api/content/<content_id>', methods=['PUT'])
@admin_required
def update_content(content_id):
    data = request.json
    update_data = {}

    for field in ['title', 'text', 'media_url', 'media_type', 'caption']:
        if field in data:
            update_data[field] = data[field]

    content_collection.update_one(
        {'_id': ObjectId(content_id)},
        {'$set': update_data}
    )

    return jsonify({'success': True})

@app.route('/api/content/<content_id>', methods=['DELETE'])
@admin_required
def delete_content(content_id):
    content_collection.delete_one({'_id': ObjectId(content_id)})
    return jsonify({'success': True})

@app.route('/api/pages/<page_name>', methods=['GET'])
def get_page(page_name):
    page = pages_collection.find_one({'page_name': page_name})
    if page:
        page['_id'] = str(page['_id'])
    return jsonify(page)

@app.route('/api/pages/<page_name>', methods=['PUT'])
@admin_required
def update_page(page_name):
    data = request.json
    pages_collection.update_one(
        {'page_name': page_name},
        {'$set': data},
        upsert=True
    )
    return jsonify({'success': True})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)