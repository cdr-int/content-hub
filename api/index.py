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
favorites_collection = db['favorites']
user_pins_collection = db['user_pins']
system_settings_collection = db['system_settings']

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
    """Get all categories for current user, sorted with user's pinned first, then paid, then free, alphabetically"""
    if 'user_id' not in session:
        return []

    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    if not user:
        return []

    # Get all categories (show all to everyone)
    all_categories = list(categories_collection.find())

    # Get user's pinned categories
    user_pins = user_pins_collection.find_one({'user_id': ObjectId(session['user_id'])})
    pinned_category_ids = set(user_pins.get('pinned_categories', [])) if user_pins else set()

    # Mark all categories with is_user_pinned
    for category in all_categories:
        category['is_user_pinned'] = str(category['_id']) in pinned_category_ids

    # Separate into pinned and unpinned based on user's pins
    pinned_categories = sorted(
        [c for c in all_categories if c['is_user_pinned']],
        key=lambda x: x['name'].lower()
    )
    unpinned_categories = [c for c in all_categories if not c['is_user_pinned']]

    # Within unpinned, separate into paid and free
    paid_categories = sorted(
        [c for c in unpinned_categories if not c.get('is_free', False)],
        key=lambda x: x['name'].lower()
    )
    free_categories = sorted(
        [c for c in unpinned_categories if c.get('is_free', False)],
        key=lambda x: x['name'].lower()
    )

    # Combine: pinned first, then paid, then free
    return pinned_categories + paid_categories + free_categories

# Context processor to make categories available in all templates
@app.context_processor
def inject_categories():
    if 'user_id' in session:
        cats = get_accessible_categories()
        return {'sidebar_categories': cats, 'categories': cats}
    return {'sidebar_categories': [], 'categories': []}

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

def check_access_timer(f):
    """Decorator to check if unsubscribed user has time remaining"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' in session:
            user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
            # Admin and subscribed users bypass timer
            if user and not user.get('is_subscribed', False) and not user.get('is_admin', False):
                # Reset timer if needed
                user = reset_user_timer_if_needed(user)
                # Don't redirect - let JavaScript modal handle it
                # Just ensure timer is reset
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

    # If user is logged in and not subscribed, check/reset timer
    if 'user_id' in session:
        user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
        if user and not user.get('is_subscribed', False) and not user.get('is_admin', False):
            user = reset_user_timer_if_needed(user)

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

@app.route('/privacy')
def privacy():
    """Privacy policy page"""
    return render_template('privacy.html')

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
            'created_at': datetime.utcnow(),
            'access_time_remaining': 3600,  # 1 hour in seconds
            'last_reset_date': datetime.utcnow().date().isoformat()
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

@app.route('/premium')
@login_required
def premium_page():
    return render_template('premium.html')

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
@check_access_timer
def categories():
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    is_subscribed = user.get('is_subscribed', False)
    is_admin = user.get('is_admin', False)

    # Fetch all categories for everyone, sorted with user's pinned first, then paid, then free
    all_categories = list(categories_collection.find())

    # Get user's pinned categories
    user_pins = user_pins_collection.find_one({'user_id': ObjectId(session['user_id'])})
    pinned_category_ids = set(user_pins.get('pinned_categories', [])) if user_pins else set()

    # Mark categories as pinned for the template
    for category in all_categories:
        category['is_user_pinned'] = str(category['_id']) in pinned_category_ids

    # Separate pinned categories
    pinned_categories = sorted(
        [c for c in all_categories if c['is_user_pinned']],
        key=lambda x: x['name'].lower()
    )
    unpinned_categories = [c for c in all_categories if not c['is_user_pinned']]

    # Within unpinned, separate paid and free
    paid_categories = sorted(
        [c for c in unpinned_categories if not c.get('is_free', False)],
        key=lambda x: x['name'].lower()
    )
    free_categories = sorted(
        [c for c in unpinned_categories if c.get('is_free', False)],
        key=lambda x: x['name'].lower()
    )

    # Combine: pinned first, then paid, then free
    categories = pinned_categories + paid_categories + free_categories

    return render_template('categories.html', 
                         categories=categories, 
                         is_admin=is_admin,
                         is_subscribed=is_subscribed)

@app.route('/category/<category_id>')
@login_required
@check_access_timer
def category_detail(category_id):
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    is_subscribed = user.get('is_subscribed', False)
    is_admin = user.get('is_admin', False)

    category = categories_collection.find_one({'_id': ObjectId(category_id)})

    if not category:
        return render_template('no_access.html'), 404

    # Check access
    if not is_admin and not is_subscribed and not category.get('is_free', False):
        return render_template('no_access.html'), 403

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
@login_required
def admin_panel():
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    if not user.get('is_admin', False):
        return render_template('admin_access_denied.html'), 403

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
        update_data['needs_refresh'] = True
        update_data['updated_at'] = datetime.utcnow()
    if 'is_admin' in data:
        update_data['is_admin'] = data['is_admin']
        update_data['needs_refresh'] = True
        update_data['updated_at'] = datetime.utcnow()

    users_collection.update_one(
        {'_id': ObjectId(user_id)},
        {'$set': update_data}
    )

    return jsonify({'success': True})

@app.route('/api/users/<user_id>/subscription', methods=['PUT'])
@admin_required
def update_subscription(user_id):
    """Update user subscription status"""
    data = request.json
    is_subscribed = data.get('is_subscribed', False)

    users_collection.update_one(
        {'_id': ObjectId(user_id)},
        {'$set': {
            'is_subscribed': is_subscribed,
            'needs_refresh': True,
            'updated_at': datetime.utcnow()
        }}
    )

    return jsonify({'success': True})

@app.route('/api/users/<user_id>', methods=['DELETE'])
@admin_required
def delete_user(user_id):
    # Also delete user's favorites when deleting user
    favorites_collection.delete_many({'user_id': user_id})
    users_collection.delete_one({'_id': ObjectId(user_id)})
    return jsonify({'success': True})

@app.route('/api/admin/verify-password', methods=['POST'])
@admin_required
def verify_admin_password():
    """Verify admin password for advanced settings"""
    data = request.json
    provided_password = data.get('password', '')

    # Get admin password from secrets collection
    admin_password_doc = db['secrets'].find_one({'key': 'admin_password'})

    if not admin_password_doc:
        # If no admin password is set, create a default one
        default_password = 'Admin123!'
        db['secrets'].insert_one({
            'key': 'admin_password',
            'value': default_password,
            'created_at': datetime.utcnow()
        })
        return jsonify({'success': provided_password == default_password})

    correct_password = admin_password_doc.get('value', '')
    return jsonify({'success': provided_password == correct_password})

@app.route('/api/admin/users/<user_id>/admin-status', methods=['PUT'])
@admin_required
def toggle_admin_status(user_id):
    """Promote or demote user to/from admin"""
    data = request.json
    is_admin = data.get('is_admin', False)

    users_collection.update_one(
        {'_id': ObjectId(user_id)},
        {'$set': {
            'is_admin': is_admin,
            'needs_refresh': True,
            'updated_at': datetime.utcnow()
        }}
    )

    return jsonify({'success': True})

@app.route('/api/admin/users/<user_id>/reset-password', methods=['POST'])
@admin_required
def reset_user_password(user_id):
    """Reset user password to default P@$$w0rd"""
    default_password = 'P@$$w0rd'
    hashed_password = generate_password_hash(default_password)

    users_collection.update_one(
        {'_id': ObjectId(user_id)},
        {'$set': {
            'password': hashed_password,
            'needs_refresh': True,
            'updated_at': datetime.utcnow()
        }}
    )

    return jsonify({'success': True})

@app.route('/api/account/check-update', methods=['GET'])
@login_required
def check_account_update():
    """Check if user account needs to be refreshed"""
    user_id = session.get('user_id')
    user = users_collection.find_one({'_id': ObjectId(user_id)})

    needs_refresh = user.get('needs_refresh', False) if user else False

    return jsonify({'needs_refresh': needs_refresh})

@app.route('/api/account/mark-refreshed', methods=['POST'])
@login_required
def mark_account_refreshed():
    """Mark that user has refreshed their account"""
    user_id = session.get('user_id')

    # Update user record
    users_collection.update_one(
        {'_id': ObjectId(user_id)},
        {'$set': {'needs_refresh': False}}
    )

    # Update session with latest data
    user = users_collection.find_one({'_id': ObjectId(user_id)})
    if user:
        session['is_admin'] = user.get('is_admin', False)
        session['is_subscribed'] = user.get('is_subscribed', False)
        session['username'] = user.get('username')
        session.modified = True  # Ensure session is saved

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
    if 'banner_image' in data:
        update_data['banner_image'] = data['banner_image']

    categories_collection.update_one(
        {'_id': ObjectId(category_id)},
        {'$set': update_data}
    )

    return jsonify({'success': True})

@app.route('/api/categories/<category_id>/pin', methods=['PUT'])
@login_required
def pin_category(category_id):
    data = request.json
    is_pinned = data.get('is_pinned', False)
    user_id = ObjectId(session['user_id'])

    # Get or create user pins document
    user_pins = user_pins_collection.find_one({'user_id': user_id})

    if not user_pins:
        user_pins = {
            'user_id': user_id,
            'pinned_categories': []
        }
        user_pins_collection.insert_one(user_pins)

    # Update pinned categories list
    pinned_categories = user_pins.get('pinned_categories', [])

    if is_pinned:
        # Add to pinned if not already there
        if category_id not in pinned_categories:
            pinned_categories.append(category_id)
    else:
        # Remove from pinned if present
        if category_id in pinned_categories:
            pinned_categories.remove(category_id)

    # Update the database
    user_pins_collection.update_one(
        {'user_id': user_id},
        {'$set': {'pinned_categories': pinned_categories}},
        upsert=True
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

# Favorites Page Route (must come before API routes)
@app.route('/favorites')
@login_required
@check_access_timer
def favorites_page():
    """Display user's favorites page"""
    user_id = session['user_id']
    is_admin = session.get('is_admin', False)
    is_subscribed = session.get('is_subscribed', False)

    # Get all favorited content IDs
    favorite_docs = list(favorites_collection.find({'user_id': ObjectId(user_id)}))
    content_ids = [fav['content_id'] for fav in favorite_docs]

    # Get all content details
    favorited_content = []
    if content_ids:
        contents = list(content_collection.find({'_id': {'$in': content_ids}}))

        # Get category info for each content
        for content in contents:
            # Handle category_id as either string or ObjectId
            category_id = content.get('category_id')
            if category_id:
                if isinstance(category_id, str):
                    category_id = ObjectId(category_id)
                category = categories_collection.find_one({'_id': category_id})
                if category:
                    content['category_name'] = category['name']
                    content['category_accent_color'] = category.get('accent_color', '#4F46E5')
            favorited_content.append(content)

    return render_template('favorites.html',
                         favorited_content=favorited_content,
                         is_admin=is_admin,
                         is_subscribed=is_subscribed)

# Favorites API Routes

@app.route('/api/favorites/<content_id>', methods=['POST'])
def add_favorite(content_id):
    """Add content to user's favorites"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    user_id = session['user_id']
    is_subscribed = session.get('is_subscribed', False)
    is_admin = session.get('is_admin', False)

    # Check if already favorited
    existing = favorites_collection.find_one({
        'user_id': ObjectId(user_id),
        'content_id': ObjectId(content_id)
    })

    if existing:
        return jsonify({'success': False, 'error': 'Already favorited'}), 400

    # Check favorites limit for non-premium users
    if not is_subscribed and not is_admin:
        favorites_count = favorites_collection.count_documents({
            'user_id': ObjectId(user_id)
        })

        if favorites_count >= 50:
            return jsonify({
                'success': False, 
                'error': 'Favorites limit reached',
                'limit_reached': True,
                'message': 'You have reached the maximum of 50 favorites. Upgrade to premium for unlimited favorites!'
            }), 403

    # Add to favorites
    favorites_collection.insert_one({
        'user_id': ObjectId(user_id),
        'content_id': ObjectId(content_id),
        'created_at': datetime.utcnow()
    })

    return jsonify({'success': True})

@app.route('/api/favorites/<content_id>', methods=['DELETE'])
def remove_favorite(content_id):
    """Remove content from user's favorites"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    user_id = session['user_id']

    result = favorites_collection.delete_one({
        'user_id': ObjectId(user_id),
        'content_id': ObjectId(content_id)
    })

    if result.deleted_count > 0:
        return jsonify({'success': True})
    else:
        return jsonify({'success': False, 'error': 'Not in favorites'}), 400

@app.route('/api/favorites/check/<content_id>', methods=['GET'])
def check_favorite(content_id):
    """Check if content is favorited by current user"""
    if 'user_id' not in session:
        return jsonify({'is_favorited': False})

    user_id = session['user_id']

    favorite = favorites_collection.find_one({
        'user_id': ObjectId(user_id),
        'content_id': ObjectId(content_id)
    })

    return jsonify({'is_favorited': favorite is not None})

@app.route('/api/favorites/count', methods=['GET'])
def get_favorites_count():
    """Get current user's favorites count"""
    if 'user_id' not in session:
        return jsonify({'count': 0, 'is_subscribed': False})

    user_id = session['user_id']
    is_subscribed = session.get('is_subscribed', False)
    is_admin = session.get('is_admin', False)

    count = favorites_collection.count_documents({
        'user_id': ObjectId(user_id)
    })

    return jsonify({
        'count': count,
        'is_subscribed': is_subscribed or is_admin,
        'limit': None if (is_subscribed or is_admin) else 50
    })

# =======================
# CATEGORY BANNER IMAGE
# =======================

@app.route('/api/categories/<category_id>/banner', methods=['PUT'])
def update_category_banner(category_id):
    """Update category banner image"""
    if 'user_id' not in session or not session.get('is_admin'):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data = request.json
    banner_url = data.get('banner_url', '')

    categories_collection.update_one(
        {'_id': ObjectId(category_id)},
        {'$set': {'banner_image': banner_url}}
    )

    return jsonify({'success': True})

# =======================
# ACCESS TIMER SYSTEM
# =======================

def get_access_time_limit():
    """Get the access time limit for unsubscribed users (in seconds)"""
    settings = system_settings_collection.find_one({'key': 'access_time_limit'})
    if settings:
        return settings.get('value', 3600)
    # Default to 1 hour if not set
    return 3600

def reset_user_timer_if_needed(user):
    """Reset user's access timer if it's a new day in their local timezone"""
    if user.get('is_subscribed', False):
        return user  # No timer for subscribed users

    today = datetime.utcnow().date().isoformat()
    last_reset = user.get('last_reset_date')

    if last_reset != today:
        # It's a new day, reset the timer
        access_limit = get_access_time_limit()
        users_collection.update_one(
            {'_id': user['_id']},
            {
                '$set': {
                    'access_time_remaining': access_limit,
                    'last_reset_date': today
                }
            }
        )
        user['access_time_remaining'] = access_limit
        user['last_reset_date'] = today

    return user

@app.route('/api/timer/get', methods=['GET'])
@login_required
def get_timer():
    """Get current user's remaining access time"""
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})

    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404

    # Check if subscribed
    if user.get('is_subscribed', False):
        return jsonify({
            'success': True,
            'is_subscribed': True,
            'time_remaining': None
        })

    # Reset timer if needed
    user = reset_user_timer_if_needed(user)

    return jsonify({
        'success': True,
        'is_subscribed': False,
        'time_remaining': user.get('access_time_remaining', 3600)
    })

@app.route('/api/timer/update', methods=['POST'])
@login_required
def update_timer():
    """Update user's remaining access time (called when user is actively on the site)"""
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})

    if not user:
        return jsonify({'success': False, 'error': 'User not found'}), 404

    # Don't update timer for subscribed users
    if user.get('is_subscribed', False):
        return jsonify({'success': True, 'is_subscribed': True})

    # Reset timer if needed
    user = reset_user_timer_if_needed(user)

    data = request.json
    time_remaining = data.get('time_remaining', 0)

    # Make sure time doesn't go negative
    time_remaining = max(0, time_remaining)

    users_collection.update_one(
        {'_id': user['_id']},
        {'$set': {'access_time_remaining': time_remaining}}
    )

    return jsonify({
        'success': True,
        'time_remaining': time_remaining,
        'expired': time_remaining <= 0
    })

@app.route('/api/settings/access-time', methods=['GET'])
@admin_required
def get_access_time_setting():
    """Get the access time limit setting"""
    limit = get_access_time_limit()
    return jsonify({'success': True, 'access_time_limit': limit})

@app.route('/api/settings/access-time', methods=['PUT'])
@admin_required
def update_access_time_setting():
    """Update the access time limit for all unsubscribed users"""
    data = request.json
    new_limit = data.get('access_time_limit', 3600)

    # Validate that it's a positive number
    if not isinstance(new_limit, (int, float)) or new_limit < 0:
        return jsonify({'success': False, 'error': 'Invalid time limit'}), 400

    # Update or create the setting
    system_settings_collection.update_one(
        {'key': 'access_time_limit'},
        {'$set': {'value': new_limit}},
        upsert=True
    )

    # Update all unsubscribed users who haven't been reset today
    # This will apply to their next reset

    return jsonify({'success': True, 'access_time_limit': new_limit})

@app.route('/api/admin/seed-speed-insights', methods=['POST'])
@admin_required
def seed_speed_insights():
    """Seed Speed Insights documentation into the database"""
    try:
        # Check if category already exists
        category_name = "Speed Insights"
        existing_category = categories_collection.find_one({'name': category_name})
        
        if existing_category:
            category_id = existing_category['_id']
        else:
            # Create Speed Insights category
            category_data = {
                'name': category_name,
                'description': 'Learn how to use Vercel Speed Insights to monitor and improve your application performance',
                'accent_color': '#0070f3',  # Vercel blue
                'is_free': True,  # Make it accessible to all users
                'created_at': datetime.utcnow()
            }
            result = categories_collection.insert_one(category_data)
            category_id = result.inserted_id
        
        # Check if content already exists
        existing_content = content_collection.find_one({
            'category_id': str(category_id),
            'title': 'Getting started with Speed Insights'
        })
        
        if existing_content:
            return jsonify({
                'success': True, 
                'message': 'Speed Insights documentation already exists',
                'category_id': str(category_id),
                'content_id': str(existing_content['_id'])
            })
        
        # Create the getting started guide content
        content_text = """# Getting started with Speed Insights

This guide will help you get started with using Vercel Speed Insights on your project, showing you how to enable it, add the package to your project, deploy your app to Vercel, and view your data in the dashboard.

To view instructions on using the Vercel Speed Insights in your project for your framework, use the **Choose a framework** dropdown on the right (at the bottom in mobile view).

## Prerequisites

- A Vercel account. If you don't have one, you can [sign up for free](https://vercel.com/signup).
- A Vercel project. If you don't have one, you can [create a new project](https://vercel.com/new).
- The Vercel CLI installed. If you don't have it, you can install it using the following command:

### Installation commands

**pnpm:**
```bash
pnpm i vercel
```

**yarn:**
```bash
yarn i vercel
```

**npm:**
```bash
npm i vercel
```

**bun:**
```bash
bun i vercel
```

## Setup Steps

### 1. Enable Speed Insights in Vercel

On the [Vercel dashboard](/dashboard), select your Project followed by the **Speed Insights** tab. You can also select the button below to be taken there. Then, select **Enable** from the dialog.

> **💡 Note:** Enabling Speed Insights will add new routes (scoped at `/_vercel/speed-insights/*`) after your next deployment.

### 2. Add `@vercel/speed-insights` to your project

Using the package manager of your choice, add the `@vercel/speed-insights` package to your project:

**pnpm:**
```bash
pnpm i @vercel/speed-insights
```

**yarn:**
```bash
yarn i @vercel/speed-insights
```

**npm:**
```bash
npm i @vercel/speed-insights
```

**bun:**
```bash
bun i @vercel/speed-insights
```

> **💡 Note:** When using the HTML implementation, there is no need to install the `@vercel/speed-insights` package.

## Framework-Specific Implementation

### Next.js (Pages Router)

The `SpeedInsights` component is a wrapper around the tracking script, offering more seamless integration with Next.js.

Add the following component to your main app file:

**TypeScript (pages/_app.tsx):**
```typescript
import type { AppProps } from 'next/app';
import { SpeedInsights } from '@vercel/speed-insights/next';

function MyApp({ Component, pageProps }: AppProps) {
  return (
    <>
      <Component {...pageProps} />
      <SpeedInsights />
    </>
  );
}

export default MyApp;
```

**JavaScript (pages/_app.jsx):**
```javascript
import { SpeedInsights } from "@vercel/speed-insights/next";

function MyApp({ Component, pageProps }) {
  return (
    <>
      <Component {...pageProps} />
      <SpeedInsights />
    </>
  );
}

export default MyApp;
```

**For Next.js versions older than 13.5:**

Import the `<SpeedInsights>` component from `@vercel/speed-insights/react` and pass it the pathname of the route:

```typescript
import { SpeedInsights } from "@vercel/speed-insights/react";
import { useRouter } from "next/router";

export default function Layout() {
  const router = useRouter();
  return <SpeedInsights route={router.pathname} />;
}
```

### Next.js (App Router)

Add the following component to the root layout:

**TypeScript (app/layout.tsx):**
```typescript
import { SpeedInsights } from "@vercel/speed-insights/next";

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <head>
        <title>Next.js</title>
      </head>
      <body>
        {children}
        <SpeedInsights />
      </body>
    </html>
  );
}
```

**JavaScript (app/layout.jsx):**
```javascript
import { SpeedInsights } from "@vercel/speed-insights/next";

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <head>
        <title>Next.js</title>
      </head>
      <body>
        {children}
        <SpeedInsights />
      </body>
    </html>
  );
}
```

**For Next.js versions older than 13.5:**

Create a dedicated component to avoid opting out from SSR on the layout:

**app/insights.tsx:**
```typescript
"use client";

import { SpeedInsights } from "@vercel/speed-insights/react";
import { usePathname } from "next/navigation";

export function Insights() {
  const pathname = usePathname();
  return <SpeedInsights route={pathname} />;
}
```

Then, import the `Insights` component in your layout:

```typescript
import type { ReactNode } from "react";
import { Insights } from "./insights";

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <head>
        <title>Next.js</title>
      </head>
      <body>
        {children}
        <Insights />
      </body>
    </html>
  );
}
```

### React (Create React App)

The `SpeedInsights` component is a wrapper around the tracking script, offering more seamless integration with React.

**App.tsx:**
```typescript
import { SpeedInsights } from '@vercel/speed-insights/react';

export default function App() {
  return (
    <div>
      {/* ... */}
      <SpeedInsights />
    </div>
  );
}
```

### Remix

The `SpeedInsights` component is a wrapper around the tracking script, offering a seamless integration with Remix.

**app/root.tsx:**
```typescript
import { SpeedInsights } from '@vercel/speed-insights/remix';

export default function App() {
  return (
    <html lang="en">
      <body>
        {/* ... */}
        <SpeedInsights />
      </body>
    </html>
  );
}
```

### SvelteKit

Add the following to your root file:

**src/routes/+layout.ts:**
```typescript
import { injectSpeedInsights } from "@vercel/speed-insights/sveltekit";

injectSpeedInsights();
```

### HTML

Add the following scripts before the closing tag of the `<body>`:

```html
<script>
  window.si = window.si || function () { (window.siq = window.siq || []).push(arguments); };
</script>
<script defer src="/_vercel/speed-insights/script.js"></script>
```

### Vue

The `SpeedInsights` component is a wrapper around the tracking script, offering more seamless integration with Vue.

**src/App.vue:**
```vue
<script setup lang="ts">
import { SpeedInsights } from '@vercel/speed-insights/vue';
</script>

<template>
  <SpeedInsights />
</template>
```

### Nuxt

The `SpeedInsights` component is a wrapper around the tracking script, offering more seamless integration with Nuxt.

**layouts/default.vue:**
```vue
<script setup lang="ts">
import { SpeedInsights } from '@vercel/speed-insights/vue';
</script>

<template>
  <SpeedInsights />
</template>
```

### Astro

Speed Insights is available for both static and SSR Astro apps.

To enable this feature, declare the `<SpeedInsights />` component from `@vercel/speed-insights/astro` near the bottom of one of your layout components, such as `BaseHead.astro`:

**BaseHead.astro:**
```astro
---
import SpeedInsights from '@vercel/speed-insights/astro';
const { title, description } = Astro.props;
---
<title>{title}</title>
<meta name="title" content={title} />
<meta name="description" content={description} />

<SpeedInsights />
```

**Optional: Remove sensitive information from URLs**

You can add a `speedInsightsBeforeSend` function to the global `window` object:

```astro
---
import SpeedInsights from '@vercel/speed-insights/astro';
const { title, description } = Astro.props;
---
<title>{title}</title>
<meta name="title" content={title} />
<meta name="description" content={description} />

<script is:inline>
  function speedInsightsBeforeSend(data){
    console.log("Speed Insights before send", data)
    return data;
  }
</script>
<SpeedInsights />
```

[Learn more about `beforeSend`](/docs/speed-insights/package#beforesend).

### Other Frameworks

Import the `injectSpeedInsights` function from the package, which will add the tracking script to your app. **This should only be called once in your app, and must run in the client**.

**main.ts:**
```typescript
import { injectSpeedInsights } from "@vercel/speed-insights";

injectSpeedInsights();
```

## Deployment

### Deploy your app to Vercel

You can deploy your app to Vercel's global CDN by running the following command from your terminal:

```bash
vercel deploy
```

Alternatively, you can [connect your project's git repository](/docs/git#deploying-a-git-repository), which will enable Vercel to deploy your latest pushes and merges to main.

Once your app is deployed, it's ready to begin tracking performance metrics.

> **💡 Note:** If everything is set up correctly, you should be able to find the `/_vercel/speed-insights/script.js` script inside the body tag of your page.

### View your data in the dashboard

Once your app is deployed, and users have visited your site, you can view the data in the dashboard.

To do so, go to your [dashboard](/dashboard), select your project, and click the **Speed Insights** tab.

After a few days of visitors, you'll be able to start exploring your metrics. For more information on how to use Speed Insights, see [Using Speed Insights](/docs/speed-insights/using-speed-insights).

## Privacy and Compliance

Learn more about how Vercel supports [privacy and data compliance standards](/docs/speed-insights/privacy-policy) with Vercel Speed Insights.

## Next steps

Now that you have Vercel Speed Insights set up, you can explore the following topics to learn more:

- [Learn how to use the `@vercel/speed-insights` package](/docs/speed-insights/package)
- [Learn about metrics](/docs/speed-insights/metrics)
- [Read about privacy and compliance](/docs/speed-insights/privacy-policy)
- [Explore pricing](/docs/speed-insights/limits-and-pricing)
- [Troubleshooting](/docs/speed-insights/troubleshooting)
"""
        
        content_data = {
            'category_id': str(category_id),
            'folder_id': None,
            'title': 'Getting started with Speed Insights',
            'text': content_text,
            'media_url': '',
            'media_type': 'text',
            'caption': 'A comprehensive guide to getting started with Vercel Speed Insights',
            'created_at': datetime.utcnow()
        }
        
        result = content_collection.insert_one(content_data)
        
        return jsonify({
            'success': True,
            'message': 'Speed Insights documentation created successfully',
            'category_id': str(category_id),
            'content_id': str(result.inserted_id)
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error seeding Speed Insights documentation: {str(e)}'
        }), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)