from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
from pymongo import MongoClient
from bson.objectid import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import os
from datetime import datetime, timedelta
import secrets
import requests as req_lib
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import threading
import time
from collections import OrderedDict

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'abc123')
CORS(app)

# MongoDB Connection
# maxPoolSize=3      — at most 3 simultaneous DB ops per worker/thread
# minPoolSize=0      — release ALL connections when idle (crucial on free tier)
# maxIdleTimeMS=5000 — close a connection after 5s of no use
# waitQueueTimeoutMS — fail fast instead of piling up waiting requests
MONGO_URI = os.environ.get('MONGO_API_KEY')
client = MongoClient(
    MONGO_URI,
    maxPoolSize=3,
    minPoolSize=0,
    maxIdleTimeMS=5000,
    serverSelectionTimeoutMS=5000,
    socketTimeoutMS=10000,
    connectTimeoutMS=5000,
    waitQueueTimeoutMS=3000,
)
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
messages_collection = db['messages']

# Email Configuration
SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp-mail.outlook.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USERNAME = os.environ.get('SMTP_USERNAME')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD')
FROM_EMAIL = os.environ.get('FROM_EMAIL', SMTP_USERNAME)


def cleanup_expired_data():
    """
    Delete expired verification codes (15-minute TTL) and
    unverified accounts older than 1 day.
    Runs every second via APScheduler, independent of traffic.
    """
    now = datetime.utcnow()

    # Delete expired verification codes
    verification_codes_collection.delete_many({'expires_at': {'$lt': now}})

    # Delete unverified accounts older than 1 day
    one_day_ago = now - timedelta(days=1)
    old_unverified = list(
        users_collection.find({
            'email_verified': False,
            'created_at': {
                '$lt': one_day_ago
            }
        }))
    for user in old_unverified:
        user_id = user['_id']
        favorites_collection.delete_many({'user_id': user_id})
        user_pins_collection.delete_many({'user_id': user_id})
        users_collection.delete_one({'_id': user_id})


# Start background scheduler
# 5 minutes is fine — verification codes have a 15-min TTL.
# Running every 1 second was making ~86,400 DB queries/day for no reason.
scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_expired_data, 'interval', minutes=5)
scheduler.start()
atexit.register(lambda: scheduler.shutdown())


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
        'expires_at': {
            '$gt': datetime.utcnow()
        }
    })

    if verification:
        verification_codes_collection.update_one({'_id': verification['_id']},
                                                 {'$set': {
                                                     'used': True
                                                 }})
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
    user_pins = user_pins_collection.find_one(
        {'user_id': ObjectId(session['user_id'])})
    pinned_category_ids = set(user_pins.get('pinned_categories',
                                            [])) if user_pins else set()

    # Mark all categories with is_user_pinned
    for category in all_categories:
        category['is_user_pinned'] = str(
            category['_id']) in pinned_category_ids

    # Separate into pinned and unpinned based on user's pins
    pinned_categories = sorted(
        [c for c in all_categories if c['is_user_pinned']],
        key=lambda x: x['name'].lower())
    unpinned_categories = [
        c for c in all_categories if not c['is_user_pinned']
    ]

    # Within unpinned, separate into paid and free
    paid_categories = sorted(
        [c for c in unpinned_categories if not c.get('is_free', False)],
        key=lambda x: x['name'].lower())
    free_categories = sorted(
        [c for c in unpinned_categories if c.get('is_free', False)],
        key=lambda x: x['name'].lower())

    # Combine: pinned first, then paid, then free
    return pinned_categories + paid_categories + free_categories


# ── Context-processor cache ───────────────────────────────────────────────────
# inject_categories fires on EVERY template render — 2-3 DB queries each time.
# Cache per user for 30s. Busted immediately on pin/category changes.
_ctx_cache: dict = {}
_CTX_TTL = 30
_ctx_lock = threading.Lock()


def _ctx_cache_get(user_id):
    with _ctx_lock:
        entry = _ctx_cache.get(user_id)
        if entry and (time.time() - entry['ts']) < _CTX_TTL:
            return entry['data']
        return None


def _ctx_cache_set(user_id, data):
    with _ctx_lock:
        _ctx_cache[user_id] = {'data': data, 'ts': time.time()}
        cutoff = time.time() - 300
        for k in [k for k, v in _ctx_cache.items() if v['ts'] < cutoff]:
            del _ctx_cache[k]


def invalidate_ctx_cache(user_id=None):
    with _ctx_lock:
        if user_id:
            _ctx_cache.pop(str(user_id), None)
        else:
            _ctx_cache.clear()


# ─────────────────────────────────────────────────────────────────────────────


# Context processor to make categories available in all templates
@app.context_processor
def inject_categories():
    user_id = session.get('user_id')

    if user_id:
        cached = _ctx_cache_get(str(user_id))
        if cached:
            return cached

    content_hidden_doc = db['secrets'].find_one({'key': 'content_hidden'})
    content_hidden = bool(
        content_hidden_doc
        and content_hidden_doc.get('value', 'false').lower() == 'true')

    if user_id:
        cats = get_accessible_categories()
        result = {
            'sidebar_categories': cats,
            'categories': cats,
            'global_content_hidden': content_hidden
        }
        _ctx_cache_set(str(user_id), result)
        return result

    return {
        'sidebar_categories': [],
        'categories': [],
        'global_content_hidden': content_hidden
    }


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
            user = users_collection.find_one(
                {'_id': ObjectId(session['user_id'])})
            # Admin and subscribed users bypass timer
            if user and not user.get('is_subscribed', False) and not user.get(
                    'is_admin', False):
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
        if user and not user.get('is_subscribed', False) and not user.get(
                'is_admin', False):
            user = reset_user_timer_if_needed(user)

    # ── Trending content: top 10 most-favourited content items ──────────
    trending_content = []
    top_category = None
    try:
        # Favorites are stored with string content_id (including synthetic batch IDs like "abc___0")
        fav_pipeline = [{
            '$group': {
                '_id': '$content_id',
                'count': {
                    '$sum': 1
                }
            }
        }, {
            '$sort': {
                'count': -1
            }
        }, {
            '$limit': 10
        }]
        top_fav_docs = list(favorites_collection.aggregate(fav_pipeline))
        if top_fav_docs:
            # content_id may be stored as ObjectId (old) or string (new) — normalise to str
            def _norm_id(v):
                s = str(v)
                # pymongo ObjectId repr looks like "ObjectId('abc123')" — strip it
                if s.startswith('ObjectId('):
                    s = s[10:-2]
                return s

            fav_counts = {_norm_id(d['_id']): d['count'] for d in top_fav_docs}
            # Resolve each favorited ID (real or synthetic batch) to its content
            real_oids = set()
            for cid_str in fav_counts:
                base = cid_str.split('___')[0] if '___' in cid_str else cid_str
                try:
                    real_oids.add(ObjectId(base))
                except Exception:
                    pass
            raw_docs = list(
                content_collection.find({'_id': {
                    '$in': list(real_oids)
                }}))
            all_expanded = expand_content_items(raw_docs)
            expanded_map = {str(item['_id']): item for item in all_expanded}

            content_docs = []
            for cid_str, count in fav_counts.items():
                item = expanded_map.get(cid_str)
                if not item:
                    continue
                item = dict(item)
                item['favorite_count'] = count
                content_docs.append(item)

            # Attach category info
            cat_ids = list({
                str(c.get('category_id', ''))
                for c in content_docs if c.get('category_id')
            })
            cats_map = {}
            for cat in categories_collection.find(
                {'_id': {
                    '$in': [ObjectId(cid) for cid in cat_ids if cid]
                }}):
                cats_map[str(cat['_id'])] = cat
            for doc in content_docs:
                cat = cats_map.get(str(doc.get('category_id', '')), {})
                doc['category_name'] = cat.get('name', '')
                doc['category_accent_color'] = cat.get('accent_color',
                                                       '#317888')

            trending_content = sorted(content_docs,
                                      key=lambda x: x['favorite_count'],
                                      reverse=True)

        # ── Top category: category with most favorites (including folder content) ──
        # Collect all content IDs grouped by category
        all_content = list(
            content_collection.find({}, {
                '_id': 1,
                'category_id': 1,
                'media_type': 1,
                'urls': 1
            }))
        # Build a map: real_doc_id_str -> category_id_str
        doc_to_cat = {}
        for doc in all_content:
            doc_to_cat[str(doc['_id'])] = str(doc.get('category_id', ''))

        # Get all favorites and map them to categories
        all_favs = list(favorites_collection.find({}, {'content_id': 1}))
        cat_fav_counts = {}
        for fav in all_favs:
            cid = str(fav.get('content_id', ''))
            # synthetic batch ID: "realoid___index" -> use realoid
            base_id = cid.split('___')[0] if '___' in cid else cid
            cat_id = doc_to_cat.get(base_id, '')
            if cat_id:
                cat_fav_counts[cat_id] = cat_fav_counts.get(cat_id, 0) + 1

        if cat_fav_counts:
            top_cat_id_str = max(cat_fav_counts,
                                 key=lambda k: cat_fav_counts[k])
            try:
                top_cat = categories_collection.find_one(
                    {'_id': ObjectId(top_cat_id_str)})
            except Exception:
                top_cat = None
            if top_cat:
                top_cat_id = str(top_cat['_id'])
                top_cat['_id'] = top_cat_id
                # Count ALL content in this category (including folder content)
                top_cat['content_count'] = content_collection.count_documents(
                    {'category_id': top_cat_id})
                top_cat['total_favorites'] = cat_fav_counts.get(top_cat_id, 0)

                # Thumbnails — expand batch docs to find real image URLs
                recent_raw = list(
                    content_collection.find(
                        {
                            'category_id': top_cat_id,
                            'media_type': {
                                '$in': ['image', 'batch']
                            }
                        }, {
                            'media_url': 1,
                            'media_type': 1,
                            'urls': 1,
                            'batch_media_type': 1,
                            'created_at': 1
                        }).sort('created_at', -1).limit(20))
                thumbnails = []
                for doc in recent_raw:
                    if doc.get('media_type') == 'batch' and doc.get(
                            'batch_media_type') == 'image':
                        for url in (doc.get('urls') or [])[:4]:
                            thumbnails.append(url)
                            if len(thumbnails) >= 4:
                                break
                    elif doc.get('media_type') == 'image' and doc.get(
                            'media_url'):
                        thumbnails.append(doc['media_url'])
                    if len(thumbnails) >= 4:
                        break
                top_cat['recent_thumbnails'] = thumbnails[:4]
                top_cat['is_premium'] = not top_cat.get('is_free', False)
                top_category = top_cat
    except Exception:
        pass  # Never crash the home page

    return render_template('index.html',
                           page=page_data,
                           trending_content=trending_content,
                           top_category=top_category)


@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.json
        username_or_email = data.get('username')
        password = data.get('password')

        # Search by username or email
        user = users_collection.find_one({
            '$or': [{
                'username': username_or_email
            }, {
                'email': username_or_email
            }]
        })

        if user:
            # Check if email is verified
            if not user.get('email_verified', False):
                return jsonify({
                    'success': False,
                    'message': 'Please verify your email first'
                }), 401

            if check_password_hash(user['password'], password):
                session['user_id'] = str(user['_id'])
                session['username'] = user['username']
                session['is_admin'] = user.get('is_admin', False)
                session['is_subscribed'] = user.get('is_subscribed', False)
                return jsonify({
                    'success': True,
                    'is_admin': user.get('is_admin', False)
                })

        return jsonify({
            'success': False,
            'message': 'Invalid credentials'
        }), 401

    return render_template(
        'login.html',
        quick_pin_code=os.environ.get('QUICK_PIN_CODE', ''),
        quick_pin_username=os.environ.get('QUICK_PIN_USERNAME', ''),
        quick_pin_password=os.environ.get('QUICK_PIN_PASSWORD', ''),
        quick_pin_sequence=os.environ.get('QUICK_PIN_SEQUENCE', 'login'),
    )


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
        return jsonify({
            'success': False,
            'message': 'Beta system not configured'
        }), 500

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

        # Check if signup is disabled
        signup_disabled_config = db['secrets'].find_one(
            {'key': 'signup_disabled'})
        signup_disabled = signup_disabled_config and signup_disabled_config.get(
            'value', 'false').lower() == 'true'

        if signup_disabled:
            return jsonify({
                'success': False,
                'message': 'Registrations are currently closed'
            }), 403

        # Check beta mode from secrets
        beta_config = db['secrets'].find_one({'key': 'beta_mode'})
        beta_mode = beta_config and beta_config.get('value',
                                                    'false').lower() == 'true'

        if beta_mode and not session.get('beta_verified', False):
            return jsonify({
                'success': False,
                'message': 'Beta key verification required'
            }), 403

        # Check if username already exists
        if users_collection.find_one({'username': username}):
            return jsonify({
                'success': False,
                'message': 'Username already exists'
            }), 400

        # Check if email already exists
        if users_collection.find_one({'email': email}):
            return jsonify({
                'success': False,
                'message': 'Email already exists'
            }), 400

        # Create new user
        user_data = {
            'username': username,
            'email': email,
            'password': generate_password_hash(password),
            'is_admin': False,
            'is_subscribed': False,
            'email_verified': False,
            'created_at': datetime.utcnow(),
            'access_time_remaining':
            get_access_time_limit(),  # use admin-configured limit
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
            return jsonify({
                'success': False,
                'message': 'Failed to send verification email'
            }), 500

    # GET request - show registration form
    beta_config = db['secrets'].find_one({'key': 'beta_mode'})
    beta_mode = beta_config and beta_config.get('value',
                                                'false').lower() == 'true'
    signup_disabled_config = db['secrets'].find_one({'key': 'signup_disabled'})
    signup_disabled = signup_disabled_config and signup_disabled_config.get(
        'value', 'false').lower() == 'true'
    return render_template('register.html',
                           beta_mode=beta_mode,
                           signup_disabled=signup_disabled)


@app.route('/verify-email', methods=['POST'])
def verify_email():
    data = request.json
    user_id = data.get('user_id')
    code = data.get('code')

    user = users_collection.find_one({'_id': ObjectId(user_id)})
    if not user:
        return jsonify({'success': False, 'message': 'User not found'}), 404

    if verify_code(user['email'], code, 'email_verification'):
        users_collection.update_one({'_id': ObjectId(user_id)},
                                    {'$set': {
                                        'email_verified': True
                                    }})

        # Auto-login after verification
        session['user_id'] = user_id
        session['username'] = user['username']
        session['is_admin'] = user.get('is_admin', False)
        session['is_subscribed'] = user.get('is_subscribed', False)

        return jsonify({'success': True})

    return jsonify({
        'success': False,
        'message': 'Invalid or expired code'
    }), 400


@app.route('/resend-verification', methods=['POST'])
def resend_verification():
    data = request.json
    user_id = data.get('user_id')

    user = users_collection.find_one({'_id': ObjectId(user_id)})
    if not user:
        return jsonify({'success': False, 'message': 'User not found'}), 404

    if user.get('email_verified', False):
        return jsonify({
            'success': False,
            'message': 'Email already verified'
        }), 400

    # Generate new code
    code = create_verification_code(user_id, user['email'],
                                    'email_verification')

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
            {'$set': {
                'password': generate_password_hash(new_password)
            }})
        return jsonify({'success': True})

    return jsonify({
        'success': False,
        'message': 'Invalid or expired code'
    }), 400


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
            {'$set': {
                'password': generate_password_hash(new_password)
            }})
        return jsonify({'success': True})

    return jsonify({
        'success': False,
        'message': 'Invalid or expired code'
    }), 400


@app.route('/settings/send-change-password-code', methods=['POST'])
@login_required
def send_change_password_code():
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})

    # Generate code
    code = create_verification_code(session['user_id'], user['email'],
                                    'password_change')

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

    return jsonify({
        'success': False,
        'message': 'Invalid or expired code'
    }), 400


@app.route('/settings/send-delete-account-code', methods=['POST'])
@login_required
def send_delete_account_code():
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})

    # Generate code
    code = create_verification_code(session['user_id'], user['email'],
                                    'account_deletion')

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
    user_pins = user_pins_collection.find_one(
        {'user_id': ObjectId(session['user_id'])})
    pinned_category_ids = set(user_pins.get('pinned_categories',
                                            [])) if user_pins else set()

    # Mark categories as pinned for the template
    for category in all_categories:
        category['is_user_pinned'] = str(
            category['_id']) in pinned_category_ids

    # Separate pinned categories
    pinned_categories = sorted(
        [c for c in all_categories if c['is_user_pinned']],
        key=lambda x: x['name'].lower())
    unpinned_categories = [
        c for c in all_categories if not c['is_user_pinned']
    ]

    # Within unpinned, separate paid and free
    paid_categories = sorted(
        [c for c in unpinned_categories if not c.get('is_free', False)],
        key=lambda x: x['name'].lower())
    free_categories = sorted(
        [c for c in unpinned_categories if c.get('is_free', False)],
        key=lambda x: x['name'].lower())

    # Combine: pinned first, then paid, then free
    categories = pinned_categories + paid_categories + free_categories

    # Check if content is hidden site-wide
    content_hidden_doc = db['secrets'].find_one({'key': 'content_hidden'})
    content_hidden = content_hidden_doc and content_hidden_doc.get(
        'value', 'false').lower() == 'true'

    return render_template('categories.html',
                           categories=categories,
                           is_admin=is_admin,
                           is_subscribed=is_subscribed,
                           content_hidden=content_hidden)


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
    if not is_admin and not is_subscribed and not category.get(
            'is_free', False):
        return render_template('no_access.html'), 403

    # Fetch folders for this category and convert ObjectId to string
    folders = list(folders_collection.find({'category_id': category_id}))
    for folder in folders:
        folder['_id'] = str(folder['_id'])

    # Build folder tree (root folders only - parent_folder_id is None)
    def build_folder_tree(parent_id=None):
        children = []
        for f in folders:
            fp = f.get('parent_folder_id')
            if fp == parent_id:
                node = dict(f)
                node['children'] = build_folder_tree(f['_id'])
                children.append(node)
        return children

    root_folders = [f for f in folders if not f.get('parent_folder_id')]
    folder_tree = build_folder_tree(None)

    # Fetch content for this category (root level only - no folder_id)
    # expand_content_items unpacks any batch documents into individual item dicts
    content_items = expand_content_items(
        list(
            content_collection.find({
                'category_id': category_id,
                'folder_id': None
            })))

    # Check if content is hidden site-wide
    content_hidden_doc = db['secrets'].find_one({'key': 'content_hidden'})
    content_hidden = content_hidden_doc and content_hidden_doc.get(
        'value', 'false').lower() == 'true'

    return render_template('category_detail.html',
                           category=category,
                           content_items=content_items,
                           folders=folders,
                           root_folders=root_folders,
                           folder_tree=folder_tree,
                           is_admin=is_admin,
                           content_hidden=content_hidden)


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
        'mode': beta_mode
        and beta_mode.get('value', 'false').lower() == 'true',
        'key': beta_key.get('value', '') if beta_key else ''
    }

    signup_disabled_doc = db['secrets'].find_one({'key': 'signup_disabled'})
    content_hidden_doc2 = db['secrets'].find_one({'key': 'content_hidden'})
    site_settings = {
        'registration_disabled':
        signup_disabled_doc
        and signup_disabled_doc.get('value', 'false').lower() == 'true',
        'content_hidden':
        content_hidden_doc2
        and content_hidden_doc2.get('value', 'false').lower() == 'true'
    }
    return render_template('admin.html',
                           users=users,
                           categories=categories,
                           beta_settings=beta_settings,
                           site_settings=site_settings)


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

    users_collection.update_one({'_id': ObjectId(user_id)},
                                {'$set': update_data})

    return jsonify({'success': True})


@app.route('/api/users/<user_id>/subscription', methods=['PUT'])
@admin_required
def update_subscription(user_id):
    """Update user subscription status"""
    data = request.json
    is_subscribed = data.get('is_subscribed', False)

    users_collection.update_one({'_id': ObjectId(user_id)}, {
        '$set': {
            'is_subscribed': is_subscribed,
            'needs_refresh': True,
            'updated_at': datetime.utcnow()
        }
    })

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

    users_collection.update_one({'_id': ObjectId(user_id)}, {
        '$set': {
            'is_admin': is_admin,
            'needs_refresh': True,
            'updated_at': datetime.utcnow()
        }
    })

    return jsonify({'success': True})


@app.route('/api/admin/users/<user_id>/reset-password', methods=['POST'])
@admin_required
def reset_user_password(user_id):
    """Reset user password to default P@$$w0rd"""
    default_password = 'P@$$w0rd'
    hashed_password = generate_password_hash(default_password)

    users_collection.update_one({'_id': ObjectId(user_id)}, {
        '$set': {
            'password': hashed_password,
            'needs_refresh': True,
            'updated_at': datetime.utcnow()
        }
    })

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
    users_collection.update_one({'_id': ObjectId(user_id)},
                                {'$set': {
                                    'needs_refresh': False
                                }})

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

    categories_collection.update_one({'_id': ObjectId(category_id)},
                                     {'$set': update_data})
    invalidate_ctx_cache()  # category name/visibility changed for all users
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
        user_pins = {'user_id': user_id, 'pinned_categories': []}
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
        {'$set': {
            'pinned_categories': pinned_categories
        }},
        upsert=True)
    invalidate_ctx_cache(str(
        session['user_id']))  # only this user's sidebar changed
    return jsonify({'success': True})


@app.route('/api/categories/<category_id>', methods=['DELETE'])
@admin_required
def delete_category(category_id):
    categories_collection.delete_one({'_id': ObjectId(category_id)})
    # Delete all folders in this category
    folders_collection.delete_many({'category_id': category_id})
    # Delete all content in this category
    content_collection.delete_many({'category_id': category_id})
    invalidate_ctx_cache()  # category removed for all users
    return jsonify({'success': True})


# Folder API Routes
@app.route('/api/folders', methods=['POST'])
@admin_required
def create_folder():
    data = request.json
    folder_data = {
        'category_id': data['category_id'],
        'parent_folder_id': data.get('parent_folder_id'),  # None = root folder
        'name': data['name'],
        'description': data.get('description', ''),
        'accent_color': data.get('accent_color', '#4F46E5'),
        'thumbnail_url': data.get('thumbnail_url', ''),
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
    if 'accent_color' in data:
        update_data['accent_color'] = data['accent_color']
    if 'thumbnail_url' in data:
        update_data['thumbnail_url'] = data['thumbnail_url']

    folders_collection.update_one({'_id': ObjectId(folder_id)},
                                  {'$set': update_data})

    return jsonify({'success': True})


@app.route('/api/folders/<folder_id>', methods=['DELETE'])
@admin_required
def delete_folder(folder_id):
    # Recursively delete all subfolders and their content
    def delete_folder_recursive(fid):
        subfolders = list(folders_collection.find({'parent_folder_id': fid}))
        for sf in subfolders:
            delete_folder_recursive(str(sf['_id']))
        folders_collection.delete_one({'_id': ObjectId(fid)})
        content_collection.delete_many({'folder_id': fid})

    delete_folder_recursive(folder_id)
    return jsonify({'success': True})


@app.route('/api/folders/<folder_id>/content', methods=['GET'])
@login_required
def get_folder_content(folder_id):
    # expand_content_items handles batch documents transparently
    content_items = expand_content_items(
        list(content_collection.find({'folder_id': folder_id})))
    return jsonify(content_items)


@app.route('/api/categories/<category_id>/folder-tree', methods=['GET'])
@login_required
def get_folder_tree(category_id):
    """Get all folders for a category as a tree structure"""
    all_folders = list(folders_collection.find({'category_id': category_id}))
    for f in all_folders:
        f['_id'] = str(f['_id'])

    def build_tree(parent_id=None):
        children = []
        for f in all_folders:
            fp = f.get('parent_folder_id')
            if fp == parent_id:
                node = dict(f)
                node['children'] = build_tree(f['_id'])
                children.append(node)
        return children

    tree = build_tree(None)
    return jsonify(tree)


def _serialize_doc(doc):
    """Convert a MongoDB document dict to JSON-safe types."""
    out = {}
    for k, v in doc.items():
        if hasattr(v, 'isoformat'):  # datetime / date
            out[k] = v.isoformat()
        elif hasattr(v, '__str__') and type(v).__name__ == 'ObjectId':
            out[k] = str(v)
        else:
            out[k] = v
    return out


def expand_content_items(raw_items):
    """
    Transparently expand any batch documents (media_type == 'batch') into
    individual item dicts, one per URL.  Regular items pass through unchanged.
    All values are converted to JSON-safe types (datetime → ISO string, etc.)
    so Flask jsonify never raises a serialization error.
    """
    expanded = []
    for item in raw_items:
        if item.get('media_type') == 'batch':
            batch_id = str(item['_id'])
            created_at = item.get('created_at')
            if hasattr(created_at, 'isoformat'):
                created_at = created_at.isoformat()
            for idx, url in enumerate(item.get('urls', [])):
                expanded.append({
                    '_id':
                    f"{batch_id}___{idx}",
                    'category_id':
                    item.get('category_id'),
                    'folder_id':
                    item.get('folder_id'),
                    'title':
                    '',
                    'text':
                    '',
                    'media_url':
                    url,
                    'media_type':
                    item.get('batch_media_type', 'image'),
                    'caption':
                    '',
                    'created_at':
                    created_at,
                    '_batch_id':
                    batch_id,
                    '_batch_index':
                    idx,
                })
        else:
            expanded.append(_serialize_doc(item))
    return expanded


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
    """
    Store all URLs in a single 'batch' document instead of one document per URL.
    This cuts a 200-image upload from 200 DB inserts → 1, and loading that folder
    from 200 DB reads + 200 HTTP requests → 1 read expanded server-side.
    The batch document is transparently unpacked by expand_content_items() before
    being passed to any template or API response.
    """
    data = request.json
    urls = [u.strip() for u in data.get('urls', []) if u.strip()]
    category_id = data['category_id']
    folder_id = data.get('folder_id')
    media_type = data.get('media_type', 'image')

    if not urls:
        return jsonify({
            'success': True,
            'created': 0,
            'failed': 0,
            'failed_urls': []
        })

    batch_doc = {
        'category_id': category_id,
        'folder_id': folder_id,
        'title': '',
        'text': '',
        'media_url': '',  # empty – real URLs live in the urls array
        'media_type':
        'batch',  # sentinel so expand_content_items knows to unpack
        'batch_media_type': media_type,
        'urls': urls,
        'caption': '',
        'created_at': datetime.utcnow()
    }
    content_collection.insert_one(batch_doc)

    return jsonify({
        'success': True,
        'created': len(urls),
        'failed': 0,
        'failed_urls': []
    })


@app.route('/api/content/<content_id>', methods=['PUT'])
@admin_required
def update_content(content_id):
    data = request.json
    update_data = {}

    for field in [
            'title', 'text', 'media_url', 'media_type', 'caption', 'folder_id'
    ]:
        if field in data:
            update_data[field] = data[field]

    content_collection.update_one({'_id': ObjectId(content_id)},
                                  {'$set': update_data})

    return jsonify({'success': True})


@app.route('/api/content/<content_id>', methods=['DELETE'])
@admin_required
def delete_content(content_id):
    """
    Supports both normal content IDs and synthetic batch item IDs
    (format: "batchObjectId___index").
    - Normal ID: deletes the document outright.
    - Batch item: removes just that URL from the urls array; if the array
      becomes empty the whole batch document is removed.
    """
    if '___' in content_id:
        batch_id_str, idx_str = content_id.split('___', 1)
        try:
            batch_oid = ObjectId(batch_id_str)
            idx = int(idx_str)
        except Exception:
            return jsonify({
                'success': False,
                'error': 'Invalid batch content ID'
            }), 400

        batch_doc = content_collection.find_one({'_id': batch_oid})
        if not batch_doc:
            return jsonify({'success': False, 'error': 'Batch not found'}), 404

        urls = batch_doc.get('urls', [])
        if 0 <= idx < len(urls):
            urls.pop(idx)

        if urls:
            content_collection.update_one({'_id': batch_oid},
                                          {'$set': {
                                              'urls': urls
                                          }})
        else:
            content_collection.delete_one({'_id': batch_oid})
    else:
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
    pages_collection.update_one({'page_name': page_name}, {'$set': data},
                                upsert=True)
    return jsonify({'success': True})


# Beta Settings API Routes
@app.route('/api/beta-settings/mode', methods=['PUT'])
@admin_required
def update_beta_mode():
    data = request.json
    enabled = data.get('enabled', False)

    db['secrets'].update_one(
        {'key': 'beta_mode'},
        {'$set': {
            'value': 'true' if enabled else 'false'
        }},
        upsert=True)

    return jsonify({'success': True})


@app.route('/api/beta-settings/key', methods=['PUT'])
@admin_required
def update_beta_key():
    data = request.json
    key = data.get('key', '').strip().upper()

    # Validate key format
    if len(key) != 12 or not key.isalnum():
        return jsonify({
            'success': False,
            'message': 'Invalid key format'
        }), 400

    db['secrets'].update_one({'key': 'beta_key'}, {'$set': {
        'value': key
    }},
                             upsert=True)

    return jsonify({'success': True})


# Site Control Settings
@app.route('/api/settings/signup-disabled', methods=['PUT'])
@admin_required
def update_signup_disabled():
    """Enable or disable new user registrations"""
    data = request.json
    disabled = data.get('disabled', False)

    db['secrets'].update_one(
        {'key': 'signup_disabled'},
        {'$set': {
            'value': 'true' if disabled else 'false'
        }},
        upsert=True)

    return jsonify({'success': True})


@app.route('/api/settings/content-hidden', methods=['PUT'])
@admin_required
def update_content_hidden():
    """Show or hide all media content (images/videos) site-wide"""
    data = request.json
    hidden = data.get('hidden', False)

    db['secrets'].update_one(
        {'key': 'content_hidden'},
        {'$set': {
            'value': 'true' if hidden else 'false'
        }},
        upsert=True)

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

    # Get all favorited content IDs (stored as strings, including synthetic batch IDs like "abc___0")
    favorite_docs = list(
        favorites_collection.find({'user_id': ObjectId(user_id)}))
    content_id_strs = [str(fav['content_id']) for fav in favorite_docs]

    favorited_content = []
    if content_id_strs:
        # Separate real ObjectIds from synthetic batch IDs
        real_ids = set()
        for cid in content_id_strs:
            if '___' in cid:
                real_ids.add(ObjectId(cid.split('___')[0]))
            else:
                try:
                    real_ids.add(ObjectId(cid))
                except Exception:
                    pass

        # Fetch all relevant documents and expand batch items
        raw_docs = list(
            content_collection.find({'_id': {
                '$in': list(real_ids)
            }}))
        all_expanded = expand_content_items(raw_docs)

        # Build lookup from expanded item ID -> item
        expanded_map = {str(item['_id']): item for item in all_expanded}

        # Return favorites in the order they were saved, keeping only matching items
        for cid in content_id_strs:
            item = expanded_map.get(cid)
            if not item:
                continue
            # Attach category info
            category_id = item.get('category_id')
            if category_id:
                if isinstance(category_id, str):
                    try:
                        category_id = ObjectId(category_id)
                    except Exception:
                        category_id = None
                if category_id:
                    category = categories_collection.find_one(
                        {'_id': category_id})
                    if category:
                        item['category_name'] = category['name']
                        item['category_accent_color'] = category.get(
                            'accent_color', '#4F46E5')
            favorited_content.append(item)

    # Check if content is hidden site-wide
    content_hidden_doc = db['secrets'].find_one({'key': 'content_hidden'})

    return render_template(
        'favorites.html',
        favorited_content=favorited_content,
        is_admin=is_admin,
        is_subscribed=is_subscribed,
        content_hidden=content_hidden_doc
        and content_hidden_doc.get('value', 'false').lower() == 'true')


# Favorites API Routes


@app.route('/api/imgproxy')
def image_proxy():
    """Proxy external images to avoid NotSameSite redirect blocks."""
    url = request.args.get('url', '')
    if not url or not url.startswith('https://'):
        return '', 400
    try:
        resp = req_lib.get(url,
                           timeout=10,
                           allow_redirects=True,
                           headers={'User-Agent': 'Mozilla/5.0'})
        content_type = resp.headers.get('Content-Type', 'image/jpeg')
        from flask import Response
        return Response(resp.content, content_type=content_type)
    except Exception as e:
        return str(e), 502


def parse_content_id(content_id):
    """
    Batch items have synthetic IDs like 'objectid___index'.
    For favorites we store/look up using just the real ObjectId part.
    Returns (real_object_id_str, original_content_id_str)
    """
    if '___' in content_id:
        return content_id.split('___')[0], content_id
    return content_id, content_id


@app.route('/api/favorites/<content_id>', methods=['POST'])
def add_favorite(content_id):
    """Add content to user's favorites"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    user_id = session['user_id']
    is_subscribed = session.get('is_subscribed', False)
    is_admin = session.get('is_admin', False)

    real_id, orig_id = parse_content_id(content_id)

    # Check if already favorited
    existing = favorites_collection.find_one({
        'user_id': ObjectId(user_id),
        'content_id': orig_id
    })

    if existing:
        return jsonify({'success': False, 'error': 'Already favorited'}), 400

    # Check favorites limit for non-premium users
    if not is_subscribed and not is_admin:
        favorites_count = favorites_collection.count_documents(
            {'user_id': ObjectId(user_id)})

        if favorites_count >= 50:
            return jsonify({
                'success':
                False,
                'error':
                'Favorites limit reached',
                'limit_reached':
                True,
                'message':
                'You have reached the maximum of 50 favorites. Upgrade to premium for unlimited favorites!'
            }), 403

    # Add to favorites - store original synthetic ID so we can look it up later
    favorites_collection.insert_one({
        'user_id': ObjectId(user_id),
        'content_id': orig_id,
        'created_at': datetime.utcnow()
    })

    return jsonify({'success': True})


@app.route('/api/favorites/<content_id>', methods=['DELETE'])
def remove_favorite(content_id):
    """Remove content from user's favorites"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401

    user_id = session['user_id']

    _, orig_id = parse_content_id(content_id)
    result = favorites_collection.delete_one({
        'user_id': ObjectId(user_id),
        'content_id': orig_id
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

    _, orig_id = parse_content_id(content_id)
    favorite = favorites_collection.find_one({
        'user_id': ObjectId(user_id),
        'content_id': orig_id
    })

    return jsonify({'is_favorited': favorite is not None})


@app.route('/api/favorites/check-bulk', methods=['POST'])
def check_favorites_bulk():
    """
    Check which of a list of content IDs are favorited by the current user.
    Returns { "favorited": ["id1", "id2", ...] } — a single DB query instead of
    one query per item.  The frontend falls back to individual checks if this
    endpoint is not available.
    """
    if 'user_id' not in session:
        return jsonify({'favorited': []})

    data = request.json or {}
    ids = data.get('ids', [])

    if not ids:
        return jsonify({'favorited': []})

    user_id = ObjectId(session['user_id'])

    # Favorites are stored with the original ID string (including synthetic batch IDs)
    str_ids = [str(i) for i in ids]

    favorited_docs = favorites_collection.find(
        {
            'user_id': user_id,
            'content_id': {
                '$in': str_ids
            }
        }, {'content_id': 1})

    result = [str(doc['content_id']) for doc in favorited_docs]
    return jsonify({'favorited': result})


@app.route('/api/favorites/count', methods=['GET'])
def get_favorites_count():
    """Get current user's favorites count"""
    if 'user_id' not in session:
        return jsonify({'count': 0, 'is_subscribed': False})

    user_id = session['user_id']
    is_subscribed = session.get('is_subscribed', False)
    is_admin = session.get('is_admin', False)

    count = favorites_collection.count_documents(
        {'user_id': ObjectId(user_id)})

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

    categories_collection.update_one({'_id': ObjectId(category_id)},
                                     {'$set': {
                                         'banner_image': banner_url
                                     }})

    return jsonify({'success': True})


# =======================
# ACCESS TIMER SYSTEM
# =======================


def get_access_time_limit():
    """Get the access time limit for unsubscribed users (in seconds)"""
    settings = system_settings_collection.find_one(
        {'key': 'access_time_limit'})
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
        users_collection.update_one({'_id': user['_id']}, {
            '$set': {
                'access_time_remaining': access_limit,
                'last_reset_date': today
            }
        })
        user['access_time_remaining'] = access_limit
        user['last_reset_date'] = toda

    else:
        # Field missing entirely (old user with no timer field) — initialise it now
        if user.get('access_time_remaining') is None:
            access_limit = get_access_time_limit()
            users_collection.update_one(
                {'_id': user['_id']},
                {'$set': {
                    'access_time_remaining': access_limit
                }})
            user['access_time_remaining'] = access_limit

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
        'success':
        True,
        'is_subscribed':
        False,
        'time_remaining':
        user.get('access_time_remaining') if user.get('access_time_remaining')
        is not None else get_access_time_limit()
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
        {'$set': {
            'access_time_remaining': time_remaining
        }})

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
    system_settings_collection.update_one({'key': 'access_time_limit'},
                                          {'$set': {
                                              'value': new_limit
                                          }},
                                          upsert=True)

    return jsonify({'success': True, 'access_time_limit': new_limit})


# =======================
# MESSAGING SYSTEM
# =======================


@app.route('/messages')
@login_required
def messages_page():
    """User messages page"""
    return render_template('messages.html')


@app.route('/api/messages', methods=['GET'])
@login_required
def get_user_messages():
    """Get all messages for the current user's conversation with admins"""
    user_id = ObjectId(session['user_id'])

    msgs = list(
        messages_collection.find({'conversation_user_id': user_id},
                                 sort=[('created_at', 1)]))

    for m in msgs:
        m['_id'] = str(m['_id'])
        m['sender_id'] = str(m['sender_id'])
        m['conversation_user_id'] = str(m['conversation_user_id'])
        if isinstance(m.get('created_at'), datetime):
            m['created_at'] = m['created_at'].isoformat()

    return jsonify({'messages': msgs})


@app.route('/api/messages', methods=['POST'])
@login_required
def send_user_message():
    """User sends a message to admins"""
    data = request.json
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'success': False, 'error': 'Empty message'}), 400

    user_id = ObjectId(session['user_id'])
    username = session.get('username', 'Unknown')

    msg = {
        'conversation_user_id': user_id,
        'sender_id': user_id,
        'sender_username': username,
        'sender_role': 'user',
        'content': content,
        'read_by_admin': False,
        'created_at': datetime.utcnow()
    }

    result = messages_collection.insert_one(msg)
    msg['_id'] = str(result.inserted_id)
    msg['sender_id'] = str(msg['sender_id'])
    msg['conversation_user_id'] = str(msg['conversation_user_id'])
    msg['created_at'] = msg['created_at'].isoformat()

    return jsonify({'success': True, 'message': msg})


@app.route('/api/messages/poll', methods=['GET'])
@login_required
def poll_user_messages():
    """Poll for new messages since a given message ID"""
    after_id = request.args.get('after')
    user_id = ObjectId(session['user_id'])

    query = {'conversation_user_id': user_id}
    if after_id:
        try:
            query['_id'] = {'$gt': ObjectId(after_id)}
        except Exception:
            pass

    msgs = list(messages_collection.find(query, sort=[('created_at', 1)]))
    for m in msgs:
        m['_id'] = str(m['_id'])
        m['sender_id'] = str(m['sender_id'])
        m['conversation_user_id'] = str(m['conversation_user_id'])
        if isinstance(m.get('created_at'), datetime):
            m['created_at'] = m['created_at'].isoformat()

    return jsonify({'messages': msgs})


# Admin messaging endpoints


@app.route('/api/admin/conversations', methods=['GET'])
@admin_required
def get_admin_conversations():
    """Get all unique conversations (one per user) for admin view.
    Automatically purges message threads whose user no longer exists."""
    pipeline = [{
        '$sort': {
            'created_at': -1
        }
    }, {
        '$group': {
            '_id': '$conversation_user_id',
            'last_message': {
                '$first': '$content'
            },
            'last_at': {
                '$first': '$created_at'
            },
            'unread_count': {
                '$sum': {
                    '$cond': [{
                        '$and': [{
                            '$eq': ['$sender_role', 'user']
                        }, {
                            '$eq': ['$read_by_admin', False]
                        }]
                    }, 1, 0]
                }
            }
        }
    }, {
        '$sort': {
            'last_at': -1
        }
    }]

    convs = list(messages_collection.aggregate(pipeline))

    result = []
    for c in convs:
        uid = c['_id']
        user = users_collection.find_one({'_id': uid}, {'username': 1})
        if not user:
            # User no longer exists — delete all their messages
            messages_collection.delete_many({'conversation_user_id': uid})
            continue
        result.append({
            'user_id': str(uid),
            'username': user['username'],
            'last_message': c.get('last_message', ''),
            'unread_count': c.get('unread_count', 0)
        })

    return jsonify({'conversations': result})


@app.route('/api/admin/messages/<user_id>', methods=['GET'])
@admin_required
def get_admin_user_messages(user_id):
    """Get all messages in a specific user's conversation"""
    try:
        uid = ObjectId(user_id)
    except Exception:
        return jsonify({'error': 'Invalid user ID'}), 400

    msgs = list(
        messages_collection.find({'conversation_user_id': uid},
                                 sort=[('created_at', 1)]))

    for m in msgs:
        m['_id'] = str(m['_id'])
        m['sender_id'] = str(m['sender_id'])
        m['conversation_user_id'] = str(m['conversation_user_id'])
        if isinstance(m.get('created_at'), datetime):
            m['created_at'] = m['created_at'].isoformat()

    return jsonify({'messages': msgs})


@app.route('/api/admin/messages/<user_id>', methods=['POST'])
@admin_required
def admin_send_message(user_id):
    """Admin sends a reply to a user"""
    try:
        uid = ObjectId(user_id)
    except Exception:
        return jsonify({'error': 'Invalid user ID'}), 400

    data = request.json
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'success': False, 'error': 'Empty message'}), 400

    admin_username = session.get('username', 'Admin')
    admin_id = ObjectId(session['user_id'])

    msg = {
        'conversation_user_id': uid,
        'sender_id': admin_id,
        'sender_username': admin_username,
        'sender_role': 'admin',
        'content': content,
        'read_by_admin': True,
        'created_at': datetime.utcnow()
    }

    result = messages_collection.insert_one(msg)
    msg['_id'] = str(result.inserted_id)
    msg['sender_id'] = str(msg['sender_id'])
    msg['conversation_user_id'] = str(msg['conversation_user_id'])
    msg['created_at'] = msg['created_at'].isoformat()

    return jsonify({'success': True, 'message': msg})


@app.route('/api/admin/messages/<user_id>/poll', methods=['GET'])
@admin_required
def admin_poll_messages(user_id):
    """Poll for new messages in a conversation since a given message ID"""
    try:
        uid = ObjectId(user_id)
    except Exception:
        return jsonify({'error': 'Invalid user ID'}), 400

    after_id = request.args.get('after')
    query = {'conversation_user_id': uid}
    if after_id:
        try:
            query['_id'] = {'$gt': ObjectId(after_id)}
        except Exception:
            pass

    msgs = list(messages_collection.find(query, sort=[('created_at', 1)]))
    for m in msgs:
        m['_id'] = str(m['_id'])
        m['sender_id'] = str(m['sender_id'])
        m['conversation_user_id'] = str(m['conversation_user_id'])
        if isinstance(m.get('created_at'), datetime):
            m['created_at'] = m['created_at'].isoformat()

    return jsonify({'messages': msgs})


@app.route('/api/admin/messages/<user_id>/read', methods=['POST'])
@admin_required
def mark_messages_read(user_id):
    """Mark all messages from a user as read by admin"""
    try:
        uid = ObjectId(user_id)
    except Exception:
        return jsonify({'error': 'Invalid user ID'}), 400

    messages_collection.update_many(
        {
            'conversation_user_id': uid,
            'sender_role': 'user',
            'read_by_admin': False
        }, {'$set': {
            'read_by_admin': True
        }})

    return jsonify({'success': True})


@app.route('/api/admin/messages/<user_id>/thread', methods=['DELETE'])
@admin_required
def delete_message_thread(user_id):
    """Delete all messages in a conversation thread for a given user"""
    try:
        uid = ObjectId(user_id)
    except Exception:
        return jsonify({'error': 'Invalid user ID'}), 400

    result = messages_collection.delete_many({'conversation_user_id': uid})
    return jsonify({'success': True, 'deleted': result.deleted_count})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
