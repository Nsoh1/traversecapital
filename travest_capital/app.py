import os
import re
import uuid
import time
import random
import string
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Text, Index, text, func, event
)
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session
from sqlalchemy.pool import NullPool

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'travest_capital_prod_secret_key_2026_x99')

# Secure session cookie settings suitable for Render HTTPS & local dev
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if os.environ.get('RENDER') or os.environ.get('FLASK_ENV') == 'production':
    app.config['SESSION_COOKIE_SECURE'] = True

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ==============================================================================
# TIMEZONE CONFIGURATION (Consistent Daily 00:00 Reset in Africa/Accra GMT+0)
# ==============================================================================
APP_TIMEZONE_NAME = os.environ.get('APP_TIMEZONE', 'Africa/Accra')
try:
    APP_TZ = ZoneInfo(APP_TIMEZONE_NAME)
except Exception:
    APP_TZ = timezone.utc


def get_now_dt():
    return datetime.now(APP_TZ)


def get_today_str():
    return get_now_dt().strftime('%Y-%m-%d')


def ts_to_date_str(ts):
    if not ts or ts <= 0:
        return ''
    return datetime.fromtimestamp(int(ts), APP_TZ).strftime('%Y-%m-%d')


def get_start_of_today_ts():
    now_dt = get_now_dt()
    start_dt = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start_dt.timestamp())


# ==============================================================================
# SUPABASE POSTGRESQL & SQLALCHEMY CONNECTION POOLING CONFIGURATION
# ==============================================================================
PAYSTACK_PUBLIC_KEY = os.environ.get('PAYSTACK_PUBLIC_KEY', 'pk_test_your_paystack_public_key_here')
PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY', 'sk_test_your_paystack_secret_key_here')


def build_database_url():
    raw_url = (
        os.environ.get('DATABASE_URL')
        or os.environ.get('SUPABASE_DB_URL')
        or ''
    ).strip()

    if not raw_url:
        # Local fallback SQLite for offline testing when no Supabase URL is set
        sqlite_path = os.path.join(BASE_DIR, 'travest_capital.db')
        return f'sqlite:///{sqlite_path}', True

    if raw_url.startswith('postgres://'):
        raw_url = 'postgresql://' + raw_url[len('postgres://'):]

    # Ensure SSL mode is required for Supabase PostgreSQL connections
    if 'sslmode=' not in raw_url:
        sep = '&' if '?' in raw_url else '?'
        raw_url = f'{raw_url}{sep}sslmode=require'

    return raw_url, False


DATABASE_URL, IS_SQLITE = build_database_url()

if IS_SQLITE:
    engine = create_engine(
        DATABASE_URL,
        connect_args={'check_same_thread': False, 'timeout': 20},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, 'connect')
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute('PRAGMA journal_mode=WAL;')
        cursor.close()
else:
    # Detect Supabase Transaction Pooler (port 6543) vs Session Pooler (port 5432)
    use_null_pool = (
        ':6543/' in DATABASE_URL
        or os.environ.get('DB_POOL_MODE', '').lower() == 'transaction'
    )
    if use_null_pool:
        engine = create_engine(
            DATABASE_URL,
            poolclass=NullPool,
            pool_pre_ping=True,
            connect_args={'connect_timeout': 10},
        )
    else:
        engine = create_engine(
            DATABASE_URL,
            pool_size=int(os.environ.get('DB_POOL_SIZE', 10)),
            max_overflow=int(os.environ.get('DB_MAX_OVERFLOW', 20)),
            pool_timeout=30,
            pool_recycle=300,
            pool_pre_ping=True,
            connect_args={'connect_timeout': 10},
        )

SessionLocal = scoped_session(sessionmaker(bind=engine, autocommit=False, autoflush=False))
Base = declarative_base()


@app.teardown_appcontext
def remove_db_session(exception=None):
    SessionLocal.remove()


# ==============================================================================
# ORM MODELS WITH HIGH-CONCURRENCY INDEXES
# ==============================================================================
class Setting(Base):
    __tablename__ = 'settings'
    key = Column(String(120), primary_key=True)
    value = Column(Text, nullable=False)


class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(160), nullable=False)
    phone = Column(String(32), unique=True, nullable=False, index=True)
    password_hash = Column(Text, nullable=False)
    invite_code = Column(String(20), unique=True, nullable=False, index=True)
    referred_by_code = Column(String(20), nullable=True, index=True)
    referral_reward_paid = Column(Integer, default=0)
    product_wallet = Column(Float, default=0.0)
    earnings_wallet = Column(Float, default=0.0)
    vip_level_id = Column(Integer, default=0, index=True)
    vip_level_name = Column(String(80), default='Starter')
    vip_expires_at = Column(Integer, default=0)
    last_claim_at = Column(Integer, default=0)
    last_claim_date = Column(String(16), default='')
    vip2_invites_count = Column(Integer, default=0)
    vip2_bonus_claimed = Column(Integer, default=0)
    last_free_spin_at = Column(Integer, default=0)
    last_free_spin_date = Column(String(16), default='')
    is_active = Column(Integer, default=1, index=True)
    created_at = Column(Integer, nullable=False, index=True)


class VipTier(Base):
    __tablename__ = 'vip_tiers'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(80), nullable=False)
    price = Column(Float, nullable=False)
    daily_profit = Column(Float, nullable=False)
    duration_days = Column(Integer, nullable=False, default=15)
    is_vip2_target = Column(Integer, default=0)


class SpinDevice(Base):
    __tablename__ = 'spin_devices'
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(160), nullable=False)
    specs = Column(Text, nullable=False)
    image_url = Column(Text, nullable=False, default='')
    spin_type = Column(String(20), nullable=False)  # 'free' or 'sale'
    original_price = Column(Float, default=0.0)
    discount_options = Column(Text, default='[]')
    difficulty = Column(String(20), default='random')
    created_at = Column(Integer, nullable=False)


class SpinOrder(Base):
    __tablename__ = 'spin_orders'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    user_name = Column(String(160), nullable=False)
    device_id = Column(Integer, nullable=False)
    device_name = Column(String(160), nullable=False)
    spin_type = Column(String(20), nullable=False)
    amount_paid = Column(Float, default=0.0)
    paystack_ref = Column(String(120), nullable=True, index=True)
    recipient_phone = Column(String(32), nullable=False)
    region = Column(String(100), nullable=False)
    address = Column(Text, nullable=False)
    status = Column(String(60), default='Pending Delivery')
    created_at = Column(Integer, nullable=False, index=True)


class Transaction(Base):
    __tablename__ = 'transactions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    user_name = Column(String(160), nullable=False)
    user_phone = Column(String(32), nullable=False)
    tx_type = Column(String(40), nullable=False, index=True)
    amount = Column(Float, nullable=False)
    reference = Column(String(120), nullable=True, index=True)
    status = Column(String(60), default='completed')
    details = Column(Text, nullable=True)
    created_at = Column(Integer, nullable=False, index=True)


Index('ix_transactions_user_created', Transaction.user_id, Transaction.created_at.desc())
Index('ix_transactions_type_created', Transaction.tx_type, Transaction.created_at.desc())


class ChatMessage(Base):
    __tablename__ = 'chat_messages'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, index=True)
    sender = Column(String(20), nullable=False)  # 'user' or 'admin'
    message = Column(Text, nullable=False)
    is_read = Column(Integer, default=0, index=True)
    created_at = Column(Integer, nullable=False, index=True)


Index('ix_chat_user_id_id', ChatMessage.user_id, ChatMessage.id)
Index('ix_chat_user_sender_read', ChatMessage.user_id, ChatMessage.sender, ChatMessage.is_read)


class NewsItem(Base):
    __tablename__ = 'news'
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(Integer, nullable=False, index=True)


# ==============================================================================
# GHANA MTN & TELECEL PHONE VALIDATION & NORMALIZATION
# ==============================================================================
VALID_GH_PREFIXES = ('024', '054', '055', '059', '020', '050')


def normalize_and_validate_ghana_phone(raw_phone):
    """
    Validates Ghana MTN (024,025,054, 055, 059) and Telecel (020, 050) numbers.
    Accepts 10-digit local format (024XXXXXXX) or international (+23324XXXXXXX / 23324XXXXXXX),
    normalizing to canonical 10-digit local format '0XXXXXXXXX'.
    Rejects fake/repeated sequences like 0240000000 or 0241234567.
    """
    if not raw_phone:
        return None, 'Phone number is required.'

    cleaned = re.sub(r'[\s\-\(\)]+', '', str(raw_phone).strip())

    if cleaned.startswith('+233'):
        local = '0' + cleaned[4:]
    elif cleaned.startswith('233') and len(cleaned) == 12:
        local = '0' + cleaned[3:]
    else:
        local = cleaned

    if not re.fullmatch(r'0\d{9}', local):
        return None, 'Enter a valid 10-digit Ghana MTN or Telecel number (e.g. 024XXXXXXX or 020XXXXXXX).'

    prefix = local[:3]
    if prefix not in VALID_GH_PREFIXES:
        return None, 'Only Ghana MTN (024, 025,054, 055, 059) and Telecel (020, 050) numbers are accepted.'

    subscriber_digits = local[3:]
    # Reject obviously fake/dummy patterns
    if len(set(subscriber_digits)) == 1 or subscriber_digits in ('1234567', '7654321', '0123456'):
        return None, 'Please enter a genuine active MTN or Telecel phone number.'

    return local, None


def generate_invite_code():
    return 'TC' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


def run_schema_migrations(db):
    """Idempotently ensure new columns exist on existing databases (PostgreSQL & SQLite)."""
    alter_statements = [
        "ALTER TABLE users ADD COLUMN last_claim_date VARCHAR(16) DEFAULT ''",
        "ALTER TABLE users ADD COLUMN last_free_spin_date VARCHAR(16) DEFAULT ''",
        "ALTER TABLE spin_devices ADD COLUMN image_url TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE spin_devices ADD COLUMN original_price DOUBLE PRECISION DEFAULT 0.0",
        "ALTER TABLE spin_devices ADD COLUMN discount_options TEXT DEFAULT '[]'",
        "ALTER TABLE spin_devices ADD COLUMN difficulty VARCHAR(20) DEFAULT 'random'",
    ]
    for stmt in alter_statements:
        try:
            db.execute(text(stmt))
            db.commit()
        except Exception:
            db.rollback()


def init_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        run_schema_migrations(db)

        defaults = {
            'admin_pin': '236963',
            'min_withdrawal': '500',
            'company_name': 'TravestCapital',
            'paystack_public_key': PAYSTACK_PUBLIC_KEY,
            'paystack_secret_key': PAYSTACK_SECRET_KEY,
            'referral_target_count': '10',
            'referral_reward_amount': '2000',
            'about_company_text': (
                'TravestCapital (TC) is an institutional equity and digital asset yield portal '
                'committed to transparent daily dividend distributions, secure Paystack-verified '
                'settlements, and long-term partner growth across Ghana.'
            ),
            'about_company_image': '',
            'about_company_video': '',
        }
        for k, v in defaults.items():
            exists = db.query(Setting).filter(Setting.key == k).first()
            if not exists:
                db.add(Setting(key=k, value=v))
        db.commit()

        if db.query(VipTier).count() == 0:
            db.add_all([
                VipTier(name='VIP 1', price=300.0, daily_profit=50.0, duration_days=15, is_vip2_target=0),
                VipTier(name='VIP 2', price=600.0, daily_profit=110.0, duration_days=15, is_vip2_target=1),
                VipTier(name='VIP 3', price=1200.0, daily_profit=230.0, duration_days=15, is_vip2_target=0),
                VipTier(name='VIP 4', price=2500.0, daily_profit=500.0, duration_days=15, is_vip2_target=0),
            ])
            db.commit()

        if db.query(SpinDevice).count() == 0:
            now = int(time.time())
            db.add_all([
                SpinDevice(
                    name='iPhone XR',
                    specs='128GB Storage • 4GB RAM • Liquid Retina HD',
                    image_url='',
                    spin_type='free',
                    original_price=0.0,
                    discount_options='[]',
                    difficulty='random',
                    created_at=now,
                ),
                SpinDevice(
                    name='Samsung Galaxy S23 Ultra',
                    specs='256GB Storage • 12GB RAM • 200MP Camera',
                    image_url='',
                    spin_type='sale',
                    original_price=6500.0,
                    discount_options='[2800, 3200, 3500, 3900, 4100, 4500]',
                    difficulty='random',
                    created_at=now,
                ),
                SpinDevice(
                    name='Apple MacBook Air M2',
                    specs='512GB SSD • 8GB Unified Memory • Midnight',
                    image_url='',
                    spin_type='free',
                    original_price=0.0,
                    discount_options='[]',
                    difficulty='hard',
                    created_at=now,
                ),
                SpinDevice(
                    name='PlayStation 5 Slim',
                    specs='1TB Ultra-High Speed SSD • DualSense Bundle',
                    image_url='',
                    spin_type='sale',
                    original_price=5800.0,
                    discount_options='[2200, 2500, 2900, 3100, 3400, 3700]',
                    difficulty='random',
                    created_at=now,
                ),
            ])
            db.commit()

        if db.query(NewsItem).count() == 0:
            db.add(NewsItem(
                title='Welcome to TravestCapital (TC) Institutional Yield Platform',
                body='Earn daily dividends on your share packages, invite VIP 2 partners for an instant milestone bonus, and spin for flagship devices!',
                created_at=int(time.time()),
            ))
            db.commit()
    finally:
        db.close()
        SessionLocal.remove()


init_db()


def get_settings_map(db, keys=None):
    q = db.query(Setting)
    if keys:
        q = q.filter(Setting.key.in_(keys))
    return {row.key: row.value for row in q.all()}


def get_setting(db, key, default=''):
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row else default


def set_setting(db, key, value):
    row = db.query(Setting).filter(Setting.key == key).first()
    if row:
        row.value = str(value)
    else:
        db.add(Setting(key=key, value=str(value)))


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = session.get('user_id')
        if not uid:
            return jsonify({'ok': False, 'error': 'Authentication required'}), 401
        db = SessionLocal()
        user = db.query(User).filter(User.id == uid).first()
        if not user:
            session.clear()
            return jsonify({'ok': False, 'error': 'User not found'}), 401
        if user.is_active == 0:
            session.clear()
            return jsonify({'ok': False, 'error': 'Your account has been deactivated by Admin.'}), 403
        return f(db, user, *args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
            return jsonify({'ok': False, 'error': 'Admin authorization required'}), 401
        db = SessionLocal()
        return f(db, *args, **kwargs)
    return decorated


# ==============================================================================
# HTML ROUTES
# ==============================================================================
@app.route('/')
def index_page():
    return render_template('index.html')


@app.route('/admin')
@app.route('/admin.html')
def admin_page():
    return render_template('admin.html')


@app.route('/static/uploads/<path:filename>')
def serve_upload(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ==============================================================================
# AUTHENTICATION API (USER)
# ==============================================================================
@app.route('/api/register', methods=['POST'])
def api_register():
    data = request.get_json(force=True)
    name = (data.get('name') or '').strip()
    raw_phone = (data.get('phone') or '').strip()
    password = (data.get('password') or '').strip()
    referral_code = (data.get('referral_code') or '').strip().upper()

    if not name or not raw_phone or not password:
        return jsonify({'ok': False, 'error': 'Full name, phone number, and password are required.'}), 400
    if len(name) < 2:
        return jsonify({'ok': False, 'error': 'Please enter your full name.'}), 400
    if len(password) < 4:
        return jsonify({'ok': False, 'error': 'Password must be at least 4 characters.'}), 400

    normalized_phone, phone_err = normalize_and_validate_ghana_phone(raw_phone)
    if phone_err:
        return jsonify({'ok': False, 'error': phone_err}), 400

    db = SessionLocal()
    existing = db.query(User.id).filter(User.phone == normalized_phone).first()
    if existing:
        return jsonify({'ok': False, 'error': 'This phone number is already registered.'}), 400

    valid_ref = None
    if referral_code:
        ref_user = db.query(User.invite_code).filter(User.invite_code == referral_code).first()
        if not ref_user:
            return jsonify({'ok': False, 'error': 'Invalid referral code. Leave blank if none.'}), 400
        valid_ref = ref_user.invite_code

    invite_code = generate_invite_code()
    while db.query(User.id).filter(User.invite_code == invite_code).first():
        invite_code = generate_invite_code()

    pw_hash = generate_password_hash(password)
    now = int(time.time())
    new_user = User(
        name=name,
        phone=normalized_phone,
        password_hash=pw_hash,
        invite_code=invite_code,
        referred_by_code=valid_ref,
        created_at=now,
    )
    db.add(new_user)
    db.commit()
    return jsonify({'ok': True, 'message': 'Account created successfully! Please sign in.'})


@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json(force=True)
    raw_phone = (data.get('phone') or '').strip()
    password = (data.get('password') or '').strip()

    if not raw_phone or not password:
        return jsonify({'ok': False, 'error': 'Enter your phone number and password.'}), 400

    # Normalize Ghana phone format on login so +233 / 233 / 024 all resolve seamlessly
    normalized_phone, _ = normalize_and_validate_ghana_phone(raw_phone)
    lookup_phone = normalized_phone or raw_phone

    db = SessionLocal()
    user = db.query(User).filter(User.phone == lookup_phone).first()
    if not user and lookup_phone != raw_phone:
        user = db.query(User).filter(User.phone == raw_phone).first()

    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({'ok': False, 'error': 'Invalid phone number or password.'}), 401

    if user.is_active == 0:
        return jsonify({'ok': False, 'error': 'Account deactivated. Contact Support.'}), 403

    session['user_id'] = user.id
    return jsonify({'ok': True, 'user_name': user.name})


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.pop('user_id', None)
    return jsonify({'ok': True})


# ==============================================================================
# USER STATE, DASHBOARD & WALLET API (00:00 CALENDAR-DAY RESET ENFORCED)
# ==============================================================================
def has_claimed_today(last_date_str, last_ts, today_str):
    if last_date_str:
        return last_date_str == today_str
    if last_ts and last_ts > 0:
        return ts_to_date_str(last_ts) == today_str
    return False


def seconds_until_midnight():
    now_dt = get_now_dt()
    next_midnight = (now_dt.replace(hour=0, minute=0, second=0, microsecond=0)).timestamp() + 86400
    return max(0, int(next_midnight - now_dt.timestamp()))


@app.route('/api/user/state', methods=['GET'])
@login_required
def api_user_state(db, user):
    now = int(time.time())
    today_str = get_today_str()

    # Check if VIP expired
    if user.vip_level_id > 0 and user.vip_expires_at > 0 and now >= user.vip_expires_at:
        user.vip_level_id = 0
        user.vip_level_name = 'Starter (Expired)'
        user.vip_expires_at = 0
        db.commit()

    tiers_rows = db.query(VipTier).order_by(VipTier.price.asc()).all()
    tiers = [
        {
            'id': t.id,
            'name': t.name,
            'price': t.price,
            'daily_profit': t.daily_profit,
            'duration_days': t.duration_days,
            'is_vip2_target': t.is_vip2_target,
        }
        for t in tiers_rows
    ]

    devices = []
    for r in db.query(SpinDevice).order_by(SpinDevice.id.desc()).all():
        try:
            opts = json.loads(r.discount_options or '[]')
        except Exception:
            opts = []
        devices.append({
            'id': r.id,
            'name': r.name,
            'specs': r.specs,
            'image_url': r.image_url,
            'spin_type': r.spin_type,
            'original_price': r.original_price,
            'discount_options': opts,
            'difficulty': r.difficulty,
            'created_at': r.created_at,
        })

    news = [
        {'id': n.id, 'title': n.title, 'body': n.body, 'created_at': n.created_at}
        for n in db.query(NewsItem).order_by(NewsItem.id.desc()).limit(10).all()
    ]

    txs = [
        {
            'id': t.id,
            'tx_type': t.tx_type,
            'amount': t.amount,
            'reference': t.reference,
            'status': t.status,
            'details': t.details,
            'created_at': t.created_at,
        }
        for t in db.query(Transaction)
        .filter(Transaction.user_id == user.id)
        .order_by(Transaction.id.desc())
        .limit(25)
        .all()
    ]

    spin_orders = [
        {
            'id': o.id,
            'device_name': o.device_name,
            'spin_type': o.spin_type,
            'amount_paid': o.amount_paid,
            'region': o.region,
            'address': o.address,
            'status': o.status,
            'created_at': o.created_at,
        }
        for o in db.query(SpinOrder)
        .filter(SpinOrder.user_id == user.id)
        .order_by(SpinOrder.id.desc())
        .all()
    ]

    # Daily Claim Eligibility: Once per calendar day, resets at 00:00 (12:00 AM)
    can_claim_daily = False
    next_claim_in = 0
    active_tier = None
    if user.vip_level_id > 0 and user.vip_expires_at > now:
        for t in tiers:
            if t['id'] == user.vip_level_id:
                active_tier = t
                break
        if active_tier:
            already_claimed = has_claimed_today(user.last_claim_date, user.last_claim_at, today_str)
            if not already_claimed:
                can_claim_daily = True
            else:
                next_claim_in = seconds_until_midnight()

    # Free Spin Eligibility: Once per calendar day, resets at 00:00 (12:00 AM)
    already_spun_today = has_claimed_today(user.last_free_spin_date, user.last_free_spin_at, today_str)
    can_free_spin_today = not already_spun_today
    free_spin_wait = seconds_until_midnight() if already_spun_today else 0

    has_vip2 = bool(active_tier and (active_tier['is_vip2_target'] == 1 or active_tier['price'] >= 600))

    cfg = get_settings_map(db, [
        'min_withdrawal',
        'paystack_public_key',
        'referral_target_count',
        'referral_reward_amount',
        'about_company_text',
        'about_company_image',
        'about_company_video',
    ])

    min_withdrawal = float(cfg.get('min_withdrawal') or '500')
    paystack_pk = cfg.get('paystack_public_key') or PAYSTACK_PUBLIC_KEY
    referral_target_count = max(1, int(float(cfg.get('referral_target_count') or '10')))
    referral_reward_amount = float(cfg.get('referral_reward_amount') or '2000')

    return jsonify({
        'ok': True,
        'server_time': now,
        'timezone': APP_TIMEZONE_NAME,
        'today_date': today_str,
        'paystack_public_key': paystack_pk,
        'min_withdrawal': min_withdrawal,
        'referral_config': {
            'target_count': referral_target_count,
            'reward_amount': referral_reward_amount,
        },
        'about_company': {
            'text': cfg.get('about_company_text', ''),
            'image_url': cfg.get('about_company_image', ''),
            'video_url': cfg.get('about_company_video', ''),
        },
        'user': {
            'id': user.id,
            'name': user.name,
            'phone': user.phone,
            'invite_code': user.invite_code,
            'product_wallet': round(user.product_wallet, 2),
            'earnings_wallet': round(user.earnings_wallet, 2),
            'vip_level_id': user.vip_level_id,
            'vip_level_name': user.vip_level_name,
            'vip_expires_at': user.vip_expires_at,
            'vip2_invites_count': user.vip2_invites_count,
            'vip2_bonus_claimed': user.vip2_bonus_claimed,
            'has_vip2': has_vip2,
        },
        'active_tier': active_tier,
        'can_claim_daily': can_claim_daily,
        'next_claim_in': next_claim_in,
        'can_free_spin_today': can_free_spin_today,
        'free_spin_wait': free_spin_wait,
        'tiers': tiers,
        'devices': devices,
        'news': news,
        'transactions': txs,
        'spin_orders': spin_orders,
    })


@app.route('/api/user/deposit', methods=['POST'])
@login_required
def api_user_deposit(db, user):
    data = request.get_json(force=True)
    amount = float(data.get('amount') or 0)
    reference = (data.get('reference') or '').strip()

    if amount <= 0 or not reference:
        return jsonify({'ok': False, 'error': 'Invalid deposit amount or Paystack reference.'}), 400

    dup = db.query(Transaction.id).filter(Transaction.reference == reference).first()
    if dup:
        return jsonify({'ok': False, 'error': 'Transaction already processed.'}), 400

    now = int(time.time())
    user.product_wallet = round((user.product_wallet or 0.0) + amount, 2)
    db.add(Transaction(
        user_id=user.id,
        user_name=user.name,
        user_phone=user.phone,
        tx_type='deposit',
        amount=amount,
        reference=reference,
        status='completed',
        details='Paystack Product Wallet Deposit',
        created_at=now,
    ))
    db.commit()
    return jsonify({'ok': True, 'message': f'GHS {amount:,.2f} credited to your Product Wallet!'})


def process_share_activation(db, user, tier, pay_source, reference=''):
    now = int(time.time())
    expires_at = now + (tier.duration_days * 86400)

    # Reset last_claim_at & last_claim_date so user can claim today's dividend immediately after buying
    user.vip_level_id = tier.id
    user.vip_level_name = tier.name
    user.vip_expires_at = expires_at
    user.last_claim_at = 0
    user.last_claim_date = ''

    db.add(Transaction(
        user_id=user.id,
        user_name=user.name,
        user_phone=user.phone,
        tx_type='share_buy',
        amount=tier.price,
        reference=reference,
        status='completed',
        details=f'Purchased {tier.name} ({pay_source})',
        created_at=now,
    ))

    # One-time 10% Signup Referral Commission on FIRST share purchase
    if user.referred_by_code and user.referral_reward_paid == 0:
        referrer = db.query(User).filter(User.invite_code == user.referred_by_code).first()
        if referrer:
            commission = round(tier.price * 0.10, 2)
            referrer.earnings_wallet = round((referrer.earnings_wallet or 0.0) + commission, 2)
            user.referral_reward_paid = 1
            db.add(Transaction(
                user_id=referrer.id,
                user_name=referrer.name,
                user_phone=referrer.phone,
                tx_type='commission',
                amount=commission,
                reference=reference,
                status='completed',
                details=f'10% Referral Bonus from {user.name}',
                created_at=now,
            ))


@app.route('/api/user/buy-share', methods=['POST'])
@login_required
def api_buy_share(db, user):
    data = request.get_json(force=True)
    tier_id = int(data.get('tier_id') or 0)
    method = data.get('method')
    reference = (data.get('reference') or '').strip()
    vip2_recruit_code = (data.get('vip2_recruit_code') or '').strip().upper()

    tier = db.query(VipTier).filter(VipTier.id == tier_id).first()
    if not tier:
        return jsonify({'ok': False, 'error': 'Selected VIP tier does not exist.'}), 404

    price = float(tier.price)

    recruiter = None
    if vip2_recruit_code:
        if tier.is_vip2_target != 1 and price < 600:
            return jsonify({'ok': False, 'error': 'Get Hired recruitment codes only apply to VIP 2 shares.'}), 400
        recruiter = db.query(User).filter(User.invite_code == vip2_recruit_code).first()
        if not recruiter:
            return jsonify({'ok': False, 'error': 'Invalid Get Hired unique invitation code.'}), 400
        if recruiter.id == user.id:
            return jsonify({'ok': False, 'error': 'You cannot use your own Get Hired code.'}), 400

    if method == 'wallet':
        if (user.product_wallet or 0.0) < price:
            return jsonify({
                'ok': False,
                'error': f'Insufficient Product Wallet balance (GHS {user.product_wallet:,.2f}). Deposit or pay directly via Paystack.'
            }), 400
        user.product_wallet = round(user.product_wallet - price, 2)
        ref_code = 'WAL-' + uuid.uuid4().hex[:10].upper()
        process_share_activation(db, user, tier, 'Product Wallet', ref_code)
    elif method == 'paystack':
        if not reference:
            return jsonify({'ok': False, 'error': 'Missing Paystack payment reference.'}), 400
        dup = db.query(Transaction.id).filter(Transaction.reference == reference).first()
        if dup:
            return jsonify({'ok': False, 'error': 'Payment reference already used.'}), 400
        process_share_activation(db, user, tier, 'Paystack Direct', reference)
    else:
        return jsonify({'ok': False, 'error': 'Invalid payment method.'}), 400

    if recruiter:
        recruiter.vip2_invites_count = (recruiter.vip2_invites_count or 0) + 1

    db.commit()
    return jsonify({
        'ok': True,
        'message': f'{tier.name} activated! Click "Claim Daily Profit" now to collect your first GHS {tier.daily_profit:,.2f}.'
    })


@app.route('/api/user/claim-daily', methods=['POST'])
@login_required
def api_claim_daily(db, user):
    now = int(time.time())
    today_str = get_today_str()

    if user.vip_level_id <= 0 or user.vip_expires_at <= now:
        return jsonify({'ok': False, 'error': 'No active VIP share package. Please purchase shares first.'}), 400

    tier = db.query(VipTier).filter(VipTier.id == user.vip_level_id).first()
    if not tier:
        return jsonify({'ok': False, 'error': 'VIP tier not found.'}), 404

    # Enforce strict once-per-calendar-day rule resetting at 00:00 (missed past days cannot be reclaimed)
    if has_claimed_today(user.last_claim_date, user.last_claim_at, today_str):
        rem = seconds_until_midnight()
        hrs = rem // 3600
        mins = (rem % 3600) // 60
        return jsonify({
            'ok': False,
            'error': f'Today\'s profit has already been claimed. Next dividend unlocks at 00:00 ({hrs}h {mins}m).'
        }), 400

    profit = float(tier.daily_profit)
    user.earnings_wallet = round((user.earnings_wallet or 0.0) + profit, 2)
    user.last_claim_at = now
    user.last_claim_date = today_str

    db.add(Transaction(
        user_id=user.id,
        user_name=user.name,
        user_phone=user.phone,
        tx_type='daily_claim',
        amount=profit,
        reference='CLM-' + uuid.uuid4().hex[:8].upper(),
        status='completed',
        details=f'{tier.name} Daily Dividend Claim ({today_str})',
        created_at=now,
    ))
    db.commit()
    return jsonify({'ok': True, 'message': f'GHS {profit:,.2f} daily profit added to your Earnings Wallet!'})


@app.route('/api/user/claim-vip2-bonus', methods=['POST'])
@login_required
def api_claim_vip2_bonus(db, user):
    target_count = max(1, int(float(get_setting(db, 'referral_target_count', '10'))))
    reward_amount = float(get_setting(db, 'referral_reward_amount', '2000'))

    if (user.vip2_invites_count or 0) < target_count:
        return jsonify({
            'ok': False,
            'error': f'You need {target_count} VIP 2 recruits (currently {user.vip2_invites_count}/{target_count}).'
        }), 400
    if user.vip2_bonus_claimed == 1:
        return jsonify({
            'ok': False,
            'error': f'You have already claimed your GHS {reward_amount:,.2f} recruitment milestone bonus.'
        }), 400

    now = int(time.time())
    user.earnings_wallet = round((user.earnings_wallet or 0.0) + reward_amount, 2)
    user.vip2_bonus_claimed = 1

    db.add(Transaction(
        user_id=user.id,
        user_name=user.name,
        user_phone=user.phone,
        tx_type='vip2_bonus',
        amount=reward_amount,
        reference='HIRE-' + uuid.uuid4().hex[:8].upper(),
        status='completed',
        details=f'Get Hired {target_count} VIP2 Milestone Reward',
        created_at=now,
    ))
    db.commit()
    return jsonify({'ok': True, 'message': f'Congratulations! GHS {reward_amount:,.2f} added to your Earnings Wallet!'})


@app.route('/api/user/withdraw', methods=['POST'])
@login_required
def api_user_withdraw(db, user):
    data = request.get_json(force=True)
    amount = float(data.get('amount') or 0)
    momo_network = (data.get('network') or 'MTN MoMo').strip()
    momo_number = (data.get('account_number') or user.phone).strip()
    account_name = (data.get('account_name') or user.name).strip()

    if not momo_number or not account_name:
        return jsonify({'ok': False, 'error': 'Please enter your Mobile Money number and registered account name.'}), 400

    min_w = float(get_setting(db, 'min_withdrawal', '500'))
    if amount < min_w:
        return jsonify({'ok': False, 'error': f'Minimum withdrawal amount is GHS {min_w:,.2f}.'}), 400
    if (user.earnings_wallet or 0.0) < amount:
        return jsonify({'ok': False, 'error': 'Insufficient Earnings Wallet balance. Product Wallet cannot be withdrawn directly.'}), 400

    now = int(time.time())
    ref = 'WDR-' + uuid.uuid4().hex[:10].upper()
    user.earnings_wallet = round(user.earnings_wallet - amount, 2)

    db.add(Transaction(
        user_id=user.id,
        user_name=user.name,
        user_phone=user.phone,
        tx_type='withdrawal',
        amount=amount,
        reference=ref,
        status='Pending Payout',
        details=f'{momo_network} • {momo_number} ({account_name})',
        created_at=now,
    ))
    db.commit()
    return jsonify({'ok': True, 'message': f'Withdrawal request of GHS {amount:,.2f} submitted! Your Mobile Money payout is being processed.'})


# ==============================================================================
# SPIN WHEEL API (00:00 CALENDAR-DAY RESET FOR FREE SPIN)
# ==============================================================================
@app.route('/api/user/spin', methods=['POST'])
@login_required
def api_user_spin(db, user):
    data = request.get_json(force=True)
    device_id = int(data.get('device_id') or 0)
    now = int(time.time())
    today_str = get_today_str()

    device = db.query(SpinDevice).filter(SpinDevice.id == device_id).first()
    if not device:
        return jsonify({'ok': False, 'error': 'Device not found.'}), 404

    if device.spin_type == 'free':
        tier = db.query(VipTier).filter(VipTier.id == user.vip_level_id).first()
        is_vip2 = tier and (tier.is_vip2_target == 1 or tier.price >= 600) and user.vip_expires_at > now
        if not is_vip2:
            return jsonify({'ok': False, 'error': 'Free Device Spin is strictly reserved for active VIP 2 shareholders!'}), 403

        if has_claimed_today(user.last_free_spin_date, user.last_free_spin_at, today_str):
            rem = seconds_until_midnight()
            hrs = rem // 3600
            mins = (rem % 3600) // 60
            return jsonify({
                'ok': False,
                'error': f'You have already used today\'s free spin. Resets at 00:00 ({hrs}h {mins}m).'
            }), 400

        if device.difficulty == 'hard':
            is_win = random.random() < 0.05
        else:
            is_win = random.choice([True, False])

        segment_index = random.choice([0, 2, 4]) if is_win else random.choice([1, 3, 5])
        user.last_free_spin_at = now
        user.last_free_spin_date = today_str
        db.commit()

        return jsonify({
            'ok': True,
            'spin_type': 'free',
            'won': is_win,
            'segment_index': segment_index,
            'label': 'WIN' if is_win else 'LOST',
        })
    else:
        try:
            options = json.loads(device.discount_options or '[]')
        except Exception:
            options = [2500, 2800, 3100, 3400, 3700, 4000]
        if not options:
            options = [2500, 2800, 3100, 3400, 3700, 4000]
        segment_index = random.randrange(len(options))
        won_price = float(options[segment_index])
        return jsonify({
            'ok': True,
            'spin_type': 'sale',
            'won': True,
            'segment_index': segment_index,
            'discounted_price': won_price,
            'label': f'GHS {won_price:,.0f}',
        })


@app.route('/api/user/spin-claim', methods=['POST'])
@login_required
def api_user_spin_claim(db, user):
    data = request.get_json(force=True)
    device_id = int(data.get('device_id') or 0)
    spin_type = data.get('spin_type')
    amount_paid = float(data.get('amount_paid') or 0)
    paystack_ref = (data.get('paystack_ref') or '').strip()
    recipient_phone = (data.get('recipient_phone') or '').strip()
    region = (data.get('region') or '').strip()
    address = (data.get('address') or '').strip()

    if not recipient_phone or not region or not address:
        return jsonify({'ok': False, 'error': 'Phone number, region, and delivery address are required.'}), 400

    device = db.query(SpinDevice).filter(SpinDevice.id == device_id).first()
    if not device:
        return jsonify({'ok': False, 'error': 'Device not found.'}), 404

    if spin_type == 'sale':
        if amount_paid <= 0 or not paystack_ref:
            return jsonify({'ok': False, 'error': 'Paystack payment confirmation is required before submitting delivery details.'}), 400
        dup = db.query(SpinOrder.id).filter(SpinOrder.paystack_ref == paystack_ref).first()
        if dup:
            return jsonify({'ok': False, 'error': 'This Paystack payment reference has already been submitted.'}), 400

    now = int(time.time())
    db.add(SpinOrder(
        user_id=user.id,
        user_name=user.name,
        device_id=device.id,
        device_name=device.name,
        spin_type=spin_type,
        amount_paid=amount_paid,
        paystack_ref=paystack_ref,
        recipient_phone=recipient_phone,
        region=region,
        address=address,
        status='Processing Delivery',
        created_at=now,
    ))

    if spin_type == 'sale':
        db.add(Transaction(
            user_id=user.id,
            user_name=user.name,
            user_phone=user.phone,
            tx_type='deposit',
            amount=amount_paid,
            reference=paystack_ref,
            status='completed',
            details=f'Discount Spin Purchase: {device.name}',
            created_at=now,
        ))

    db.commit()
    return jsonify({'ok': True, 'message': 'Delivery details submitted! Admin will dispatch your device shortly.'})


# ==============================================================================
# USER ACCOUNT SETTINGS (MINE TAB)
# ==============================================================================
@app.route('/api/user/reset-password', methods=['POST'])
@login_required
def api_user_reset_password(db, user):
    data = request.get_json(force=True)
    old_pw = (data.get('old_password') or '').strip()
    new_pw = (data.get('new_password') or '').strip()

    if not check_password_hash(user.password_hash, old_pw):
        return jsonify({'ok': False, 'error': 'Current password is incorrect.'}), 400
    if len(new_pw) < 4:
        return jsonify({'ok': False, 'error': 'New password must be at least 4 characters.'}), 400

    user.password_hash = generate_password_hash(new_pw)
    db.commit()
    return jsonify({'ok': True, 'message': 'Password updated successfully!'})


@app.route('/api/user/change-phone', methods=['POST'])
@login_required
def api_user_change_phone(db, user):
    data = request.get_json(force=True)
    raw_new_phone = (data.get('new_phone') or '').strip()
    password = (data.get('password') or '').strip()

    normalized_phone, phone_err = normalize_and_validate_ghana_phone(raw_new_phone)
    if phone_err:
        return jsonify({'ok': False, 'error': phone_err}), 400
    if not check_password_hash(user.password_hash, password):
        return jsonify({'ok': False, 'error': 'Password verification failed.'}), 400

    exists = db.query(User.id).filter(User.phone == normalized_phone, User.id != user.id).first()
    if exists:
        return jsonify({'ok': False, 'error': 'Phone number already in use by another account.'}), 400

    user.phone = normalized_phone
    db.commit()
    return jsonify({'ok': True, 'message': 'Phone number updated successfully!'})


# ==============================================================================
# REAL-TIME LIVE CHAT API (SINGLE DELIVERY GUARANTEE)
# ==============================================================================
@app.route('/api/chat/messages', methods=['GET'])
@login_required
def api_user_chat_messages(db, user):
    after_id = int(request.args.get('after_id') or 0)
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == user.id, ChatMessage.id > after_id)
        .order_by(ChatMessage.id.asc())
        .all()
    )
    if rows:
        db.query(ChatMessage).filter(
            ChatMessage.user_id == user.id,
            ChatMessage.sender == 'admin',
            ChatMessage.is_read == 0,
        ).update({'is_read': 1}, synchronize_session=False)
        db.commit()

    return jsonify({
        'ok': True,
        'messages': [
            {
                'id': r.id,
                'user_id': r.user_id,
                'sender': r.sender,
                'message': r.message,
                'is_read': r.is_read,
                'created_at': r.created_at,
            }
            for r in rows
        ],
    })


@app.route('/api/chat/send', methods=['POST'])
@login_required
def api_user_chat_send(db, user):
    data = request.get_json(force=True)
    msg = (data.get('message') or '').strip()
    if not msg:
        return jsonify({'ok': False, 'error': 'Message cannot be empty.'}), 400

    now = int(time.time())
    # Deduplicate accidental rapid double-submits within 2 seconds
    recent = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.user_id == user.id,
            ChatMessage.sender == 'user',
            ChatMessage.message == msg,
            ChatMessage.created_at >= now - 2,
        )
        .order_by(ChatMessage.id.desc())
        .first()
    )
    if recent:
        return jsonify({
            'ok': True,
            'message': {
                'id': recent.id,
                'user_id': recent.user_id,
                'sender': recent.sender,
                'message': recent.message,
                'is_read': recent.is_read,
                'created_at': recent.created_at,
            },
        })

    chat_msg = ChatMessage(
        user_id=user.id,
        sender='user',
        message=msg,
        is_read=0,
        created_at=now,
    )
    db.add(chat_msg)
    db.commit()
    db.refresh(chat_msg)

    return jsonify({
        'ok': True,
        'message': {
            'id': chat_msg.id,
            'user_id': chat_msg.user_id,
            'sender': chat_msg.sender,
            'message': chat_msg.message,
            'is_read': chat_msg.is_read,
            'created_at': chat_msg.created_at,
        },
    })


# ==============================================================================
# ADMIN SECRET 6-DIGIT LOGIN & FULL CONTROL API
# ==============================================================================
@app.route('/api/admin/login', methods=['POST'])
def api_admin_login():
    data = request.get_json(force=True)
    pin = (data.get('pin') or '').strip()
    db = SessionLocal()
    expected = get_setting(db, 'admin_pin', '236963')
    if pin == expected:
        session['is_admin'] = True
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'Invalid 6-digit access code.'}), 401


@app.route('/api/admin/logout', methods=['POST'])
def api_admin_logout():
    session.pop('is_admin', None)
    return jsonify({'ok': True})


@app.route('/api/admin/overview', methods=['GET'])
@admin_required
def api_admin_overview(db):
    users_rows = db.query(User).order_by(User.id.desc()).all()
    users = [
        {
            'id': u.id,
            'name': u.name,
            'phone': u.phone,
            'invite_code': u.invite_code,
            'referred_by_code': u.referred_by_code,
            'product_wallet': u.product_wallet,
            'earnings_wallet': u.earnings_wallet,
            'vip_level_id': u.vip_level_id,
            'vip_level_name': u.vip_level_name,
            'vip_expires_at': u.vip_expires_at,
            'vip2_invites_count': u.vip2_invites_count,
            'is_active': u.is_active,
            'created_at': u.created_at,
        }
        for u in users_rows
    ]

    tiers = [
        {
            'id': t.id,
            'name': t.name,
            'price': t.price,
            'daily_profit': t.daily_profit,
            'duration_days': t.duration_days,
            'is_vip2_target': t.is_vip2_target,
        }
        for t in db.query(VipTier).order_by(VipTier.price.asc()).all()
    ]

    devices = []
    for r in db.query(SpinDevice).order_by(SpinDevice.id.desc()).all():
        try:
            opts = json.loads(r.discount_options or '[]')
        except Exception:
            opts = []
        devices.append({
            'id': r.id,
            'name': r.name,
            'specs': r.specs,
            'image_url': r.image_url,
            'spin_type': r.spin_type,
            'original_price': r.original_price,
            'discount_options': opts,
            'difficulty': r.difficulty,
            'created_at': r.created_at,
        })

    spin_orders = [
        {
            'id': o.id,
            'user_id': o.user_id,
            'user_name': o.user_name,
            'device_name': o.device_name,
            'spin_type': o.spin_type,
            'amount_paid': o.amount_paid,
            'recipient_phone': o.recipient_phone,
            'region': o.region,
            'address': o.address,
            'status': o.status,
            'created_at': o.created_at,
        }
        for o in db.query(SpinOrder).order_by(SpinOrder.id.desc()).all()
    ]

    news = [
        {'id': n.id, 'title': n.title, 'body': n.body, 'created_at': n.created_at}
        for n in db.query(NewsItem).order_by(NewsItem.id.desc()).all()
    ]

    transactions = [
        {
            'id': t.id,
            'user_id': t.user_id,
            'user_name': t.user_name,
            'user_phone': t.user_phone,
            'tx_type': t.tx_type,
            'amount': t.amount,
            'reference': t.reference,
            'status': t.status,
            'details': t.details,
            'created_at': t.created_at,
        }
        for t in db.query(Transaction).order_by(Transaction.id.desc()).limit(100).all()
    ]

    start_of_today = get_start_of_today_ts()
    paid_today = (
        db.query(func.coalesce(func.sum(Transaction.amount), 0.0))
        .filter(Transaction.tx_type.in_(['deposit', 'share_buy']), Transaction.created_at >= start_of_today)
        .scalar()
        or 0.0
    )

    total_paid = (
        db.query(func.coalesce(func.sum(Transaction.amount), 0.0))
        .filter(Transaction.tx_type.in_(['deposit', 'share_buy']))
        .scalar()
        or 0.0
    )

    total_withdrawn = (
        db.query(func.coalesce(func.sum(Transaction.amount), 0.0))
        .filter(Transaction.tx_type == 'withdrawal')
        .scalar()
        or 0.0
    )

    # Single-pass query for chat threads (avoids N+1 subqueries under load)
    thread_rows = db.execute(text("""
        SELECT
            u.id AS user_id,
            u.name AS name,
            u.phone AS phone,
            latest.message AS last_message,
            latest.created_at AS last_time,
            COALESCE(unread.cnt, 0) AS unread_count
        FROM users u
        JOIN (
            SELECT c1.user_id, c1.message, c1.created_at
            FROM chat_messages c1
            JOIN (
                SELECT user_id, MAX(id) AS max_id
                FROM chat_messages
                GROUP BY user_id
            ) c2 ON c1.id = c2.max_id
        ) latest ON latest.user_id = u.id
        LEFT JOIN (
            SELECT user_id, COUNT(*) AS cnt
            FROM chat_messages
            WHERE sender = 'user' AND is_read = 0
            GROUP BY user_id
        ) unread ON unread.user_id = u.id
        ORDER BY latest.created_at DESC
    """)).mappings().all()

    threads = [
        {
            'user_id': r['user_id'],
            'name': r['name'],
            'phone': r['phone'],
            'last_message': r['last_message'],
            'last_time': r['last_time'],
            'unread_count': int(r['unread_count'] or 0),
        }
        for r in thread_rows
    ]

    cfg = get_settings_map(db, [
        'min_withdrawal',
        'paystack_public_key',
        'paystack_secret_key',
        'referral_target_count',
        'referral_reward_amount',
        'about_company_text',
        'about_company_image',
        'about_company_video',
    ])

    min_withdrawal = float(cfg.get('min_withdrawal') or '500')
    paystack_pk = cfg.get('paystack_public_key') or PAYSTACK_PUBLIC_KEY
    paystack_sk = cfg.get('paystack_secret_key') or PAYSTACK_SECRET_KEY
    referral_target_count = max(1, int(float(cfg.get('referral_target_count') or '10')))
    referral_reward_amount = float(cfg.get('referral_reward_amount') or '2000')

    return jsonify({
        'ok': True,
        'stats': {
            'total_users': len(users),
            'active_users': sum(1 for u in users if u['is_active'] == 1),
            'paid_today': round(float(paid_today), 2),
            'total_paid': round(float(total_paid), 2),
            'total_withdrawn': round(float(total_withdrawn), 2),
            'min_withdrawal': min_withdrawal,
        },
        'settings': {
            'min_withdrawal': min_withdrawal,
            'paystack_public_key': paystack_pk,
            'paystack_secret_key': paystack_sk,
            'referral_target_count': referral_target_count,
            'referral_reward_amount': referral_reward_amount,
            'about_company_text': cfg.get('about_company_text', ''),
            'about_company_image': cfg.get('about_company_image', ''),
            'about_company_video': cfg.get('about_company_video', ''),
        },
        'users': users,
        'tiers': tiers,
        'devices': devices,
        'spin_orders': spin_orders,
        'news': news,
        'transactions': transactions,
        'chat_threads': threads,
    })


@app.route('/api/admin/user/toggle-status', methods=['POST'])
@admin_required
def api_admin_toggle_user(db):
    data = request.get_json(force=True)
    uid = int(data.get('user_id') or 0)
    u = db.query(User).filter(User.id == uid).first()
    if not u:
        return jsonify({'ok': False, 'error': 'User not found.'}), 404
    u.is_active = 0 if u.is_active == 1 else 1
    db.commit()
    return jsonify({'ok': True, 'is_active': u.is_active})


@app.route('/api/admin/user/adjust-wallet', methods=['POST'])
@admin_required
def api_admin_adjust_wallet(db):
    data = request.get_json(force=True)
    uid = int(data.get('user_id') or 0)
    product_wallet = float(data.get('product_wallet') or 0)
    earnings_wallet = float(data.get('earnings_wallet') or 0)
    vip_level_id = int(data.get('vip_level_id') or 0)

    u = db.query(User).filter(User.id == uid).first()
    if not u:
        return jsonify({'ok': False, 'error': 'User not found.'}), 404

    if vip_level_id > 0:
        t = db.query(VipTier).filter(VipTier.id == vip_level_id).first()
        vip_name = t.name if t else 'Starter'
        expires = int(time.time()) + ((t.duration_days if t else 15) * 86400)
    else:
        vip_name = 'Starter'
        expires = 0

    u.product_wallet = product_wallet
    u.earnings_wallet = earnings_wallet
    u.vip_level_id = vip_level_id
    u.vip_level_name = vip_name
    u.vip_expires_at = expires
    db.commit()
    return jsonify({'ok': True, 'message': 'User account balances and VIP status updated.'})


@app.route('/api/admin/vip/save', methods=['POST'])
@admin_required
def api_admin_vip_save(db):
    data = request.get_json(force=True)
    tid = data.get('id')
    name = (data.get('name') or '').strip()
    price = float(data.get('price') or 0)
    daily_profit = float(data.get('daily_profit') or 0)
    duration_days = int(data.get('duration_days') or 15)
    is_vip2_target = 1 if data.get('is_vip2_target') else 0

    if not name or price <= 0 or daily_profit <= 0:
        return jsonify({'ok': False, 'error': 'Provide valid VIP name, price, and daily profit.'}), 400

    if tid:
        t = db.query(VipTier).filter(VipTier.id == int(tid)).first()
        if t:
            t.name = name
            t.price = price
            t.daily_profit = daily_profit
            t.duration_days = duration_days
            t.is_vip2_target = is_vip2_target
    else:
        db.add(VipTier(
            name=name,
            price=price,
            daily_profit=daily_profit,
            duration_days=duration_days,
            is_vip2_target=is_vip2_target,
        ))
    db.commit()
    return jsonify({'ok': True, 'message': 'VIP Share Tier saved successfully.'})


@app.route('/api/admin/vip/delete', methods=['POST'])
@admin_required
def api_admin_vip_delete(db):
    data = request.get_json(force=True)
    tid = int(data.get('id') or 0)
    db.query(VipTier).filter(VipTier.id == tid).delete()
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/admin/vip/reset-defaults', methods=['POST'])
@admin_required
def api_admin_vip_reset(db):
    db.query(VipTier).delete()
    db.add_all([
        VipTier(name='VIP 1', price=300.0, daily_profit=50.0, duration_days=15, is_vip2_target=0),
        VipTier(name='VIP 2', price=600.0, daily_profit=110.0, duration_days=15, is_vip2_target=1),
        VipTier(name='VIP 3', price=1200.0, daily_profit=230.0, duration_days=15, is_vip2_target=0),
        VipTier(name='VIP 4', price=2500.0, daily_profit=500.0, duration_days=15, is_vip2_target=0),
    ])
    db.commit()
    return jsonify({'ok': True, 'message': 'VIP tiers reset to default amounts.'})


@app.route('/api/admin/devices/add', methods=['POST'])
@admin_required
def api_admin_device_add(db):
    name = (request.form.get('name') or '').strip()
    specs = (request.form.get('specs') or '').strip()
    spin_type = (request.form.get('spin_type') or 'free').strip()
    original_price = float(request.form.get('original_price') or 0)
    difficulty = (request.form.get('difficulty') or 'random').strip()
    discounts_raw = (request.form.get('discount_options') or '').strip()

    if not name or not specs:
        return jsonify({'ok': False, 'error': 'Device name and specifications are required.'}), 400

    discount_list = []
    if spin_type == 'sale':
        try:
            discount_list = [float(x.strip()) for x in discounts_raw.split(',') if x.strip()]
        except Exception:
            return jsonify({'ok': False, 'error': 'Discount amounts must be comma-separated numbers (e.g. 2200,2500,2800,3000,3300,3600).'}), 400
        if len(discount_list) < 2:
            return jsonify({'ok': False, 'error': 'Provide at least 2 discount wheel amounts for a Sale Spin device.'}), 400

    image_url = ''
    if 'image' in request.files:
        f = request.files['image']
        if f and f.filename:
            ext = os.path.splitext(secure_filename(f.filename))[1].lower()
            if ext not in ['.png', '.jpg', '.jpeg', '.webp', '.gif']:
                return jsonify({'ok': False, 'error': 'Unsupported image format. Use PNG, JPG, or WEBP.'}), 400
            fname = f'dev_{uuid.uuid4().hex[:12]}{ext}'
            f.save(os.path.join(UPLOAD_FOLDER, fname))
            image_url = f'/static/uploads/{fname}'

    db.add(SpinDevice(
        name=name,
        specs=specs,
        image_url=image_url,
        spin_type=spin_type,
        original_price=original_price,
        discount_options=json.dumps(discount_list),
        difficulty=difficulty,
        created_at=int(time.time()),
    ))
    db.commit()
    return jsonify({'ok': True, 'message': f'{name} added to Spin Vault!'})


@app.route('/api/admin/devices/update', methods=['POST'])
@admin_required
def api_admin_device_update(db):
    data = request.get_json(force=True)
    did = int(data.get('id') or 0)
    device = db.query(SpinDevice).filter(SpinDevice.id == did).first()
    if not device:
        return jsonify({'ok': False, 'error': 'Spin device not found.'}), 404

    spin_type = data.get('spin_type') or device.spin_type
    difficulty = data.get('difficulty') or device.difficulty
    original_price = float(data.get('original_price') if data.get('original_price') is not None else device.original_price)
    discounts = data.get('discount_options') or []

    if spin_type == 'sale':
        try:
            cleaned_discounts = [float(x) for x in discounts if str(x).strip() != '']
        except Exception:
            return jsonify({'ok': False, 'error': 'Discount amounts must be valid numbers.'}), 400
        if len(cleaned_discounts) < 2:
            return jsonify({'ok': False, 'error': 'Provide at least 2 valid discount amounts.'}), 400
        discounts = cleaned_discounts

    device.spin_type = spin_type
    device.difficulty = difficulty
    device.original_price = original_price
    device.discount_options = json.dumps(discounts)
    db.commit()
    return jsonify({'ok': True, 'message': 'Spin device parameters updated.'})


@app.route('/api/admin/devices/delete', methods=['POST'])
@admin_required
def api_admin_device_delete(db):
    data = request.get_json(force=True)
    did = int(data.get('id') or 0)
    db.query(SpinDevice).filter(SpinDevice.id == did).delete()
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/admin/orders/status', methods=['POST'])
@admin_required
def api_admin_order_status(db):
    data = request.get_json(force=True)
    oid = int(data.get('id') or 0)
    status = (data.get('status') or 'Delivered').strip()
    order = db.query(SpinOrder).filter(SpinOrder.id == oid).first()
    if order:
        order.status = status
        db.commit()
    return jsonify({'ok': True})


@app.route('/api/admin/withdrawals/update', methods=['POST'])
@admin_required
def api_admin_withdrawal_update(db):
    data = request.get_json(force=True)
    tx_id = int(data.get('id') or 0)
    action = (data.get('action') or '').strip()

    tx = db.query(Transaction).filter(Transaction.id == tx_id, Transaction.tx_type == 'withdrawal').first()
    if not tx:
        return jsonify({'ok': False, 'error': 'Withdrawal record not found.'}), 404

    if action == 'paid':
        tx.status = 'Paid'
        db.commit()
        return jsonify({'ok': True, 'message': 'Withdrawal marked as Paid!'})
    elif action == 'refund':
        if tx.status == 'Refunded':
            return jsonify({'ok': False, 'error': 'Already refunded.'}), 400
        u = db.query(User).filter(User.id == tx.user_id).first()
        if u:
            u.earnings_wallet = round((u.earnings_wallet or 0.0) + tx.amount, 2)
        tx.status = 'Refunded'
        db.commit()
        return jsonify({'ok': True, 'message': f'GHS {tx.amount:,.2f} refunded back to user Earnings Wallet.'})

    return jsonify({'ok': False, 'error': 'Invalid action.'}), 400


@app.route('/api/admin/news/add', methods=['POST'])
@admin_required
def api_admin_news_add(db):
    data = request.get_json(force=True)
    title = (data.get('title') or '').strip()
    body = (data.get('body') or '').strip()
    if not title or not body:
        return jsonify({'ok': False, 'error': 'Title and message are required.'}), 400
    db.add(NewsItem(title=title, body=body, created_at=int(time.time())))
    db.commit()
    return jsonify({'ok': True, 'message': 'Announcement published to all users!'})


@app.route('/api/admin/news/delete', methods=['POST'])
@admin_required
def api_admin_news_delete(db):
    data = request.get_json(force=True)
    nid = int(data.get('id') or 0)
    db.query(NewsItem).filter(NewsItem.id == nid).delete()
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/admin/about/save', methods=['POST'])
@admin_required
def api_admin_about_save(db):
    # Supports both multipart/form-data (with optional file upload) and JSON
    if request.content_type and 'multipart/form-data' in request.content_type:
        about_text = (request.form.get('text') or '').strip()
        image_url = (request.form.get('image_url') or '').strip()
        video_url = (request.form.get('video_url') or '').strip()

        if 'image_file' in request.files:
            img_f = request.files['image_file']
            if img_f and img_f.filename:
                ext = os.path.splitext(secure_filename(img_f.filename))[1].lower()
                if ext not in ['.png', '.jpg', '.jpeg', '.webp', '.gif']:
                    return jsonify({'ok': False, 'error': 'Unsupported image format. Use PNG, JPG, WEBP, or GIF.'}), 400
                fname = f'about_img_{uuid.uuid4().hex[:12]}{ext}'
                img_f.save(os.path.join(UPLOAD_FOLDER, fname))
                image_url = f'/static/uploads/{fname}'

        if 'video_file' in request.files:
            vid_f = request.files['video_file']
            if vid_f and vid_f.filename:
                ext = os.path.splitext(secure_filename(vid_f.filename))[1].lower()
                if ext not in ['.mp4', '.webm', '.ogg', '.mov']:
                    return jsonify({'ok': False, 'error': 'Unsupported video format. Use MP4, WEBM, or OGG.'}), 400
                fname = f'about_vid_{uuid.uuid4().hex[:12]}{ext}'
                vid_f.save(os.path.join(UPLOAD_FOLDER, fname))
                video_url = f'/static/uploads/{fname}'
    else:
        data = request.get_json(force=True)
        about_text = (data.get('text') or '').strip()
        image_url = (data.get('image_url') or '').strip()
        video_url = (data.get('video_url') or '').strip()

    if not about_text:
        return jsonify({'ok': False, 'error': 'Company description text is required.'}), 400

    set_setting(db, 'about_company_text', about_text)
    set_setting(db, 'about_company_image', image_url)
    set_setting(db, 'about_company_video', video_url)
    db.commit()
    return jsonify({
        'ok': True,
        'message': 'About Company profile saved and live for all users!',
        'about_company': {
            'text': about_text,
            'image_url': image_url,
            'video_url': video_url,
        },
    })


@app.route('/api/admin/settings/save', methods=['POST'])
@admin_required
def api_admin_settings_save(db):
    data = request.get_json(force=True)
    if 'min_withdrawal' in data and str(data['min_withdrawal']).strip():
        set_setting(db, 'min_withdrawal', str(float(data['min_withdrawal'])))
    if 'paystack_public_key' in data and str(data['paystack_public_key']).strip():
        set_setting(db, 'paystack_public_key', str(data['paystack_public_key']).strip())
    if 'paystack_secret_key' in data and str(data['paystack_secret_key']).strip():
        set_setting(db, 'paystack_secret_key', str(data['paystack_secret_key']).strip())
    if 'referral_target_count' in data and str(data['referral_target_count']).strip():
        target_cnt = int(float(data['referral_target_count']))
        if target_cnt < 1:
            return jsonify({'ok': False, 'error': 'Invited users milestone target must be at least 1.'}), 400
        set_setting(db, 'referral_target_count', str(target_cnt))
    if 'referral_reward_amount' in data and str(data['referral_reward_amount']).strip():
        reward_amt = float(data['referral_reward_amount'])
        if reward_amt <= 0:
            return jsonify({'ok': False, 'error': 'Referral reward amount must be greater than 0.'}), 400
        set_setting(db, 'referral_reward_amount', str(reward_amt))
    if 'new_admin_pin' in data and str(data['new_admin_pin']).strip():
        pin = str(data['new_admin_pin']).strip()
        if len(pin) != 6 or not pin.isdigit():
            return jsonify({'ok': False, 'error': 'Admin PIN must be exactly 6 digits.'}), 400
        set_setting(db, 'admin_pin', pin)
    db.commit()
    return jsonify({'ok': True, 'message': 'Platform configuration saved!'})


@app.route('/api/admin/chat/messages', methods=['GET'])
@admin_required
def api_admin_chat_messages(db):
    uid = int(request.args.get('user_id') or 0)
    after_id = int(request.args.get('after_id') or 0)
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == uid, ChatMessage.id > after_id)
        .order_by(ChatMessage.id.asc())
        .all()
    )
    if rows:
        db.query(ChatMessage).filter(
            ChatMessage.user_id == uid,
            ChatMessage.sender == 'user',
            ChatMessage.is_read == 0,
        ).update({'is_read': 1}, synchronize_session=False)
        db.commit()

    return jsonify({
        'ok': True,
        'messages': [
            {
                'id': r.id,
                'user_id': r.user_id,
                'sender': r.sender,
                'message': r.message,
                'is_read': r.is_read,
                'created_at': r.created_at,
            }
            for r in rows
        ],
    })


@app.route('/api/admin/chat/send', methods=['POST'])
@admin_required
def api_admin_chat_send(db):
    data = request.get_json(force=True)
    uid = int(data.get('user_id') or 0)
    msg = (data.get('message') or '').strip()
    if not uid or not msg:
        return jsonify({'ok': False, 'error': 'Select a user and enter a reply.'}), 400

    now = int(time.time())
    # Deduplicate accidental rapid double-submits within 2 seconds
    recent = (
        db.query(ChatMessage)
        .filter(
            ChatMessage.user_id == uid,
            ChatMessage.sender == 'admin',
            ChatMessage.message == msg,
            ChatMessage.created_at >= now - 2,
        )
        .order_by(ChatMessage.id.desc())
        .first()
    )
    if recent:
        return jsonify({
            'ok': True,
            'message': {
                'id': recent.id,
                'user_id': recent.user_id,
                'sender': recent.sender,
                'message': recent.message,
                'is_read': recent.is_read,
                'created_at': recent.created_at,
            },
        })

    chat_msg = ChatMessage(
        user_id=uid,
        sender='admin',
        message=msg,
        is_read=0,
        created_at=now,
    )
    db.add(chat_msg)
    db.commit()
    db.refresh(chat_msg)
    return jsonify({
        'ok': True,
        'message': {
            'id': chat_msg.id,
            'user_id': chat_msg.user_id,
            'sender': chat_msg.sender,
            'message': chat_msg.message,
            'is_read': chat_msg.is_read,
            'created_at': chat_msg.created_at,
        },
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
