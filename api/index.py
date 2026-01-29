from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
from pymongo import MongoClient
from bson.objectid import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import os
from datetime import datetime, timedelta
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

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
folders_collection = db['folders']
verification_codes_collection = db['verification_codes']

# Email Configuration
SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp-mail.outlook.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USERNAME = os.environ.get('SMTP_USERNAME')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD')
FROM_EMAIL = os.environ.get('FROM_EMAIL', SMTP_USERNAME)

def send_email(to_email, subject, body):
    """Send email using SMTP"""
    try:
        msg = MIMEMultipart()
        msg['From'] = FROM_EMAIL
        msg['To'] = to_email
        msg['Subject'] = subject

        msg.attach(MIMEText(body, 'html'))

        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        text = msg.as_string()
        server.sendmail(FROM_EMAIL, to_email, text)
        server.quit()
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False

def generate_verification_code():
    """Generate a 6-digit verification code"""
    return ''.join([str(secrets.randbelow(10)) for _ in range(6)])

def create_verification_code(user_id, email, code_type='email_verification'):
    """Create and store a verification code"""
    code = generate_verification_code()
    expiry = datetime.utcnow() + timedelta(minutes=15)

    verification_codes_collection.insert_one({
        'user_id': user_id,
        'email': email,
        'code': code,
        'type': code_type,
        'expires_at': expiry,
        'used': False
    })

    return code

def verify_code(email, code, code_type='email_verification'):
    """Verify a code and mark it as used"""
    verification = verification_codes_collection.find_one({
        'email': email,
        'code': code,
        'type': code_type,
        'used': False,
        'expires_at': {'$gt': datetime.utcnow()}
    })

    if verification:
        verification_codes_collection.update_one(
            {'_id': verification['_id']},
            {'$set': {'used': True}}
        )
        return True
    return False

# Helper function to get accessible categories for sidebar
def get_accessible_categories():
    """Get all categories for current user, sorted paid first then free, alphabetically"""
    if 'user_id' not in session:
        return []

    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    if not user:
        return []

    # Get all categories (show all to everyone)
    all_categories = list(categories_collection.find())

    # Separate into paid and free
    paid_categories = sorted(
        [c for c in all_categories if not c.get('is_free', False)],
        key=lambda x: x['name'].lower()
    )
    free_categories = sorted(
        [c for c in all_categories if c.get('is_free', False)],
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
        username_or_email = data.get('username')
        password = data.get('password')

        # Search by username or email
        user = users_collection.find_one({
            '$or': [
                {'username': username_or_email},
                {'email': username_or_email}
            ]
        })

        if user:
            # Check if email is verified
            if not user.get('email_verified', False):
                return jsonify({'success': False, 'message': 'Please verify your email first'}), 401

            if check_password_hash(user['password'], password):
                session['user_id'] = str(user['_id'])
                session['username'] = user['username']
                session['is_admin'] = user.get('is_admin', False)
                session['is_subscribed'] = user.get('is_subscribed', False)
                return jsonify({'success': True, 'is_admin': user.get('is_admin', False)})

        return jsonify({'success': False, 'message': 'Invalid credentials'}), 401

    return render_template('login.html')

@app.route('/verify-beta-key', methods=['POST'])
def verify_beta_key():
    """Verify beta key against stored secret"""
    data = request.json
    provided_key = data.get('beta_key', '').strip().upper()

    # Get beta key from secrets collection
    beta_secret = db['secrets'].find_one({'key': 'beta_key'})

    if not beta_secret:
        return jsonify({'success': False, 'message': 'Beta system not configured'}), 500

    correct_key = beta_secret.get('value', '').strip().upper()

    if provided_key == correct_key:
        session['beta_verified'] = True
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'message': 'Invalid beta key'}), 401

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        data = request.json
        username = data.get('username')
        email = data.get('email')
        password = data.get('password')

        # Check beta mode from secrets
        beta_config = db['secrets'].find_one({'key': 'beta_mode'})
        beta_mode = beta_config and beta_config.get('value', 'false').lower() == 'true'

        if beta_mode and not session.get('beta_verified', False):
            return jsonify({'success': False, 'message': 'Beta key verification required'}), 403

        # Check if username already exists
        if users_collection.find_one({'username': username}):
            return jsonify({'success': False, 'message': 'Username already exists'}), 400

        # Check if email already exists
        if users_collection.find_one({'email': email}):
            return jsonify({'success': False, 'message': 'Email already exists'}), 400

        # Create new user
        user_data = {
            'username': username,
            'email': email,
            'password': generate_password_hash(password),
            'is_admin': False,
            'is_subscribed': False,
            'email_verified': False,
            'created_at': datetime.utcnow()
        }

        result = users_collection.insert_one(user_data)
        user_id = str(result.inserted_id)

        # Generate verification code
        code = create_verification_code(user_id, email, 'email_verification')

        # Send verification email
        subject = "Verify Your Email - Effexor Hub"
        body = f"""
        <html>
            <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
                <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 30px; border-radius: 10px; text-align: center;">
                    <h1 style="color: white; margin: 0;">Welcome to Effexor Hub!</h1>
                </div>
                <div style="padding: 30px; background: #f9fafb; border-radius: 10px; margin-top: 20px;">
                    <h2 style="color: #333;">Verify Your Email</h2>
                    <p style="color: #666; font-size: 16px;">Thank you for registering! Your verification code is:</p>
                    <div style="background: white; padding: 20px; border-radius: 8px; text-align: center; margin: 20px 0;">
                        <h1 style="color: #667eea; font-size: 36px; letter-spacing: 8px; margin: 0;">{code}</h1>
                    </div>
                    <p style="color: #666; font-size: 14px;">This code will expire in 15 minutes.</p>
                    <p style="color: #999; font-size: 12px; margin-top: 30px;">If you didn't create this account, please ignore this email.</p>
                </div>
            </body>
        </html>
        """

        if send_email(email, subject, body):
            return jsonify({'success': True, 'user_id': user_id})
        else:
            # If email fails, delete the user
            users_collection.delete_one({'_id': ObjectId(user_id)})
            return jsonify({'success': False, 'message': 'Failed to send verification email'}), 500

    # GET request - show registration form
    beta_config = db['secrets'].find_one({'key': 'beta_mode'})
    beta_mode = beta_config and beta_config.get('value', 'false').lower() == 'true'
    return render_template('register.html', beta_mode=beta_mode)

@app.route('/verify-email', methods=['POST'])
def verify_email():
    data = request.json
    user_id = data.get('user_id')
    code = data.get('code')

    user = users_collection.find_one({'_id': ObjectId(user_id)})
    if not user:
        return jsonify({'success': False, 'message': 'User not found'}), 404

    if verify_code(user['email'], code, 'email_verification'):
        users_collection.update_one(
            {'_id': ObjectId(user_id)},
            {'$set': {'email_verified': True}}
        )

        # Auto-login after verification
        session['user_id'] = user_id
        session['username'] = user['username']
        session['is_admin'] = user.get('is_admin', False)
        session['is_subscribed'] = user.get('is_subscribed', False)

        return jsonify({'success': True})

    return jsonify({'success': False, 'message': 'Invalid or expired code'}), 400

@app.route('/resend-verification', methods=['POST'])
def resend_verification():
    data = request.json
    user_id = data.get('user_id')

    user = users_collection.find_one({'_id': ObjectId(user_id)})
    if not user:
        return jsonify({'success': False, 'message': 'User not found'}), 404

    if user.get('email_verified', False):
        return jsonify({'success': False, 'message': 'Email already verified'}), 400

    # Generate new code
    code = create_verification_code(user_id, user['email'], 'email_verification')

    # Send verification email
    subject = "Verify Your Email - Effexor Hub"
    body = f"""
    <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 30px; border-radius: 10px; text-align: center;">
                <h1 style="color: white; margin: 0;">Effexor Hub</h1>
            </div>
            <div style="padding: 30px; background: #f9fafb; border-radius: 10px; margin-top: 20px;">
                <h2 style="color: #333;">Verify Your Email</h2>
                <p style="color: #666; font-size: 16px;">Your new verification code is:</p>
                <div style="background: white; padding: 20px; border-radius: 8px; text-align: center; margin: 20px 0;">
                    <h1 style="color: #667eea; font-size: 36px; letter-spacing: 8px; margin: 0;">{code}</h1>
                </div>
                <p style="color: #666; font-size: 14px;">This code will expire in 15 minutes.</p>
            </div>
        </body>
    </html>
    """

    if send_email(user['email'], subject, body):
        return jsonify({'success': True})

    return jsonify({'success': False, 'message': 'Failed to send email'}), 500

@app.route('/forgot-password', methods=['POST'])
def forgot_password():
    data = request.json
    email = data.get('email')

    user = users_collection.find_one({'email': email})
    if not user:
        # Don't reveal if email exists
        return jsonify({'success': True})

    # Generate reset code
    code = create_verification_code(str(user['_id']), email, 'password_reset')

    # Send reset email
    subject = "Reset Your Password - Effexor Hub"
    body = f"""
    <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 30px; border-radius: 10px; text-align: center;">
                <h1 style="color: white; margin: 0;">Password Reset</h1>
            </div>
            <div style="padding: 30px; background: #f9fafb; border-radius: 10px; margin-top: 20px;">
                <h2 style="color: #333;">Reset Your Password</h2>
                <p style="color: #666; font-size: 16px;">Your password reset code is:</p>
                <div style="background: white; padding: 20px; border-radius: 8px; text-align: center; margin: 20px 0;">
                    <h1 style="color: #667eea; font-size: 36px; letter-spacing: 8px; margin: 0;">{code}</h1>
                </div>
                <p style="color: #666; font-size: 14px;">This code will expire in 15 minutes.</p>
                <p style="color: #999; font-size: 12px; margin-top: 30px;">If you didn't request this reset, please ignore this email.</p>
            </div>
        </body>
    </html>
    """

    send_email(email, subject, body)
    return jsonify({'success': True})

@app.route('/reset-password', methods=['POST'])
def reset_password():
    data = request.json
    email = data.get('email')
    code = data.get('code')
    new_password = data.get('new_password')

    user = users_collection.find_one({'email': email})
    if not user:
        return jsonify({'success': False, 'message': 'Invalid request'}), 400

    if verify_code(email, code, 'password_reset'):
        users_collection.update_one(
            {'_id': user['_id']},
            {'$set': {'password': generate_password_hash(new_password)}}
        )
        return jsonify({'success': True})

    return jsonify({'success': False, 'message': 'Invalid or expired code'}), 400

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/settings')
@login_required
def settings():
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    return render_template('settings.html', user=user)

@app.route('/settings/change-password', methods=['POST'])
@login_required
def change_password():
    data = request.json
    code = data.get('code')
    new_password = data.get('new_password')

    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})

    if verify_code(user['email'], code, 'password_change'):
        users_collection.update_one(
            {'_id': user['_id']},
            {'$set': {'password': generate_password_hash(new_password)}}
        )
        return jsonify({'success': True})

    return jsonify({'success': False, 'message': 'Invalid or expired code'}), 400

@app.route('/settings/send-change-password-code', methods=['POST'])
@login_required
def send_change_password_code():
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})

    # Generate code
    code = create_verification_code(session['user_id'], user['email'], 'password_change')

    # Send email
    subject = "Password Change Verification - Effexor Hub"
    body = f"""
    <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 30px; border-radius: 10px; text-align: center;">
                <h1 style="color: white; margin: 0;">Password Change</h1>
            </div>
            <div style="padding: 30px; background: #f9fafb; border-radius: 10px; margin-top: 20px;">
                <h2 style="color: #333;">Verify Password Change</h2>
                <p style="color: #666; font-size: 16px;">Your verification code is:</p>
                <div style="background: white; padding: 20px; border-radius: 8px; text-align: center; margin: 20px 0;">
                    <h1 style="color: #667eea; font-size: 36px; letter-spacing: 8px; margin: 0;">{code}</h1>
                </div>
                <p style="color: #666; font-size: 14px;">This code will expire in 15 minutes.</p>
            </div>
        </body>
    </html>
    """

    if send_email(user['email'], subject, body):
        return jsonify({'success': True})

    return jsonify({'success': False, 'message': 'Failed to send email'}), 500

@app.route('/settings/delete-account', methods=['POST'])
@login_required
def delete_account():
    data = request.json
    code = data.get('code')

    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})

    if verify_code(user['email'], code, 'account_deletion'):
        # Delete user
        users_collection.delete_one({'_id': user['_id']})
        # Clear session
        session.clear()
        return jsonify({'success': True})

    return jsonify({'success': False, 'message': 'Invalid or expired code'}), 400

@app.route('/settings/send-delete-account-code', methods=['POST'])
@login_required
def send_delete_account_code():
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})

    # Generate code
    code = create_verification_code(session['user_id'], user['email'], 'account_deletion')

    # Send email
    subject = "Account Deletion Verification - Effexor Hub"
    body = f"""
    <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); padding: 30px; border-radius: 10px; text-align: center;">
                <h1 style="color: white; margin: 0;">⚠️ Account Deletion</h1>
            </div>
            <div style="padding: 30px; background: #f9fafb; border-radius: 10px; margin-top: 20px;">
                <h2 style="color: #333;">Confirm Account Deletion</h2>
                <p style="color: #666; font-size: 16px;">Your verification code is:</p>
                <div style="background: white; padding: 20px; border-radius: 8px; text-align: center; margin: 20px 0;">
                    <h1 style="color: #ef4444; font-size: 36px; letter-spacing: 8px; margin: 0;">{code}</h1>
                </div>
                <p style="color: #666; font-size: 14px;">This code will expire in 15 minutes.</p>
                <p style="color: #dc2626; font-size: 14px; font-weight: bold; margin-top: 20px;">⚠️ Warning: This action is permanent and cannot be undone!</p>
            </div>
        </body>
    </html>
    """

    if send_email(user['email'], subject, body):
        return jsonify({'success': True})

    return jsonify({'success': False, 'message': 'Failed to send email'}), 500

@app.route('/categories')
@login_required
def categories():
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    is_subscribed = user.get('is_subscribed', False)
    is_admin = user.get('is_admin', False)

    # Fetch all categories for everyone, sorted with paid first
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

    return render_template('categories.html', 
                         categories=categories, 
                         is_admin=is_admin,
                         is_subscribed=is_subscribed)

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

    # Fetch folders for this category and convert ObjectId to string
    folders = list(folders_collection.find({'category_id': category_id}))
    for folder in folders:
        folder['_id'] = str(folder['_id'])

    # Fetch content for this category (root level only - no folder_id)
    content_items = list(content_collection.find({'category_id': category_id, 'folder_id': None}))

    return render_template('category_detail.html', 
                         category=category, 
                         content_items=content_items,
                         folders=folders,
                         is_admin=is_admin)

@app.route('/admin')
@admin_required
def admin_panel():
    users = list(users_collection.find())
    categories = list(categories_collection.find())

    # Get beta settings
    beta_mode = db['secrets'].find_one({'key': 'beta_mode'})
    beta_key = db['secrets'].find_one({'key': 'beta_key'})

    beta_settings = {
        'mode': beta_mode and beta_mode.get('value', 'false').lower() == 'true',
        'key': beta_key.get('value', '') if beta_key else ''
    }

    return render_template('admin.html', users=users, categories=categories, beta_settings=beta_settings)

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
    # Delete all folders in this category
    folders_collection.delete_many({'category_id': category_id})
    # Delete all content in this category
    content_collection.delete_many({'category_id': category_id})
    return jsonify({'success': True})

# Folder API Routes
@app.route('/api/folders', methods=['POST'])
@admin_required
def create_folder():
    data = request.json
    folder_data = {
        'category_id': data['category_id'],
        'name': data['name'],
        'description': data.get('description', ''),
        'created_at': datetime.utcnow()
    }

    result = folders_collection.insert_one(folder_data)
    return jsonify({'success': True, 'id': str(result.inserted_id)})

@app.route('/api/folders/<folder_id>', methods=['PUT'])
@admin_required
def update_folder(folder_id):
    data = request.json
    update_data = {}

    if 'name' in data:
        update_data['name'] = data['name']
    if 'description' in data:
        update_data['description'] = data['description']

    folders_collection.update_one(
        {'_id': ObjectId(folder_id)},
        {'$set': update_data}
    )

    return jsonify({'success': True})

@app.route('/api/folders/<folder_id>', methods=['DELETE'])
@admin_required
def delete_folder(folder_id):
    folders_collection.delete_one({'_id': ObjectId(folder_id)})
    # Delete all content in this folder
    content_collection.delete_many({'folder_id': folder_id})
    return jsonify({'success': True})

@app.route('/api/folders/<folder_id>/content', methods=['GET'])
@login_required
def get_folder_content(folder_id):
    content_items = list(content_collection.find({'folder_id': folder_id}))
    for item in content_items:
        item['_id'] = str(item['_id'])
    return jsonify(content_items)

# Content API Routes
@app.route('/api/content', methods=['POST'])
@admin_required
def create_content():
    data = request.json
    content_data = {
        'category_id': data['category_id'],
        'folder_id': data.get('folder_id'),  # Can be None for root content
        'title': data.get('title', ''),
        'text': data.get('text', ''),
        'media_url': data.get('media_url', ''),
        'media_type': data.get('media_type', 'text'),  # text, image, video
        'caption': data.get('caption', ''),
        'created_at': datetime.utcnow()
    }

    result = content_collection.insert_one(content_data)
    return jsonify({'success': True, 'id': str(result.inserted_id)})

@app.route('/api/content/bulk', methods=['POST'])
@admin_required
def bulk_create_content():
    data = request.json
    urls = data.get('urls', [])
    category_id = data['category_id']
    folder_id = data.get('folder_id')  # Can be None for root
    media_type = data.get('media_type', 'image')

    created_count = 0
    failed_urls = []

    for url in urls:
        url = url.strip()
        if not url:
            continue

        try:
            content_data = {
                'category_id': category_id,
                'folder_id': folder_id,
                'title': '',
                'text': '',
                'media_url': url,
                'media_type': media_type,
                'caption': '',
                'created_at': datetime.utcnow()
            }
            content_collection.insert_one(content_data)
            created_count += 1
        except Exception as e:
            failed_urls.append(url)

    return jsonify({
        'success': True, 
        'created': created_count,
        'failed': len(failed_urls),
        'failed_urls': failed_urls
    })

@app.route('/api/content/<content_id>', methods=['PUT'])
@admin_required
def update_content(content_id):
    data = request.json
    update_data = {}

    for field in ['title', 'text', 'media_url', 'media_type', 'caption', 'folder_id']:
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

# Beta Settings API Routes
@app.route('/api/beta-settings/mode', methods=['PUT'])
@admin_required
def update_beta_mode():
    data = request.json
    enabled = data.get('enabled', False)

    db['secrets'].update_one(
        {'key': 'beta_mode'},
        {'$set': {'value': 'true' if enabled else 'false'}},
        upsert=True
    )

    return jsonify({'success': True})

@app.route('/api/beta-settings/key', methods=['PUT'])
@admin_required
def update_beta_key():
    data = request.json
    key = data.get('key', '').strip().upper()

    # Validate key format
    if len(key) != 12 or not key.isalnum():
        return jsonify({'success': False, 'message': 'Invalid key format'}), 400

    db['secrets'].update_one(
        {'key': 'beta_key'},
        {'$set': {'value': key}},
        upsert=True
    )

    return jsonify({'success': True})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)