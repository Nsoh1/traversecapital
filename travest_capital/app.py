import requests
import os
import uuid
import time
import random
import string
import json
import re
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import psycopg2
from psycopg2.extras import RealDictCursor

try:
    from zoneinfo import ZoneInfo
    GHANA_TZ = ZoneInfo('Africa/Accra')
except Exception:
    GHANA_TZ = timezone.utc

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'travest_capital_prod_secret_key_2026_x99')

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ==============================================================================
# SUPABASE POSTGRESQL CONNECTION STRING INITIALIZATION
# ==============================================================================
DATABASE_URL = (
    os.environ.get('DATABASE_URL')
    or os.environ.get('SUPABASE_DB_URL')
    or os.environ.get('POSTGRES_URL')
    or ''
).strip()

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable must be set with your Supabase connection string.")

if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)


def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    conn.autocommit = False
    return conn


# ==============================================================================
# PAYSTACK CONFIGURATION
# ==============================================================================
PAYSTACK_PUBLIC_KEY = os.environ.get('PAYSTACK_PUBLIC_KEY', 'pk_test_your_paystack_public_key_here')
PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY', 'sk_test_your_paystack_secret_key_here')


def get_next_sequence_id(counter_name, conn=None):
    own_conn = conn is None
    if own_conn:
        conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO counters (name, val)
                VALUES (%s, 1)
                ON CONFLICT (name)
                DO UPDATE SET val = counters.val + 1
                RETURNING val
                """,
                (counter_name,)
            )
            row = cur.fetchone()
            val = int(row['val']) if row else 1
        if own_conn:
            conn.commit()
        return val
    except Exception:
        if own_conn:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def generate_invite_code():
    return 'TC' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


def normalize_ghana_phone(raw_phone):
    cleaned = re.sub(r'[\s\-\(\)]', '', (raw_phone or '').strip())
    match = re.match(r'^(?:0|\+?233)(24|54|55|59|20|50)(\d{7})$', cleaned)
    if not match:
        return None
    return f"0{match.group(1)}{match.group(2)}"


def get_ghana_day_info(ts=None):
    now_ts = int(time.time()) if ts is None else int(ts)
    dt_ghana = datetime.fromtimestamp(now_ts, tz=GHANA_TZ)
    day_str = dt_ghana.strftime('%Y-%m-%d')
    next_midnight = (dt_ghana + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    seconds_until_midnight = max(0, int((next_midnight - dt_ghana).total_seconds()))
    return day_str, seconds_until_midnight


def init_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS counters (
                    name TEXT PRIMARY KEY,
                    val BIGINT NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS vip_tiers (
                    id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL,
                    price DOUBLE PRECISION NOT NULL DEFAULT 0,
                    daily_profit DOUBLE PRECISION NOT NULL DEFAULT 0,
                    duration_days INTEGER NOT NULL DEFAULT 15,
                    is_vip2_target INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS spin_devices (
                    id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL,
                    specs TEXT NOT NULL,
                    image_url TEXT NOT NULL DEFAULT '',
                    spin_type TEXT NOT NULL DEFAULT 'free',
                    original_price DOUBLE PRECISION NOT NULL DEFAULT 0,
                    discount_options TEXT NOT NULL DEFAULT '[]',
                    difficulty TEXT NOT NULL DEFAULT 'random',
                    created_at BIGINT NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS news (
                    id BIGINT PRIMARY KEY,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at BIGINT NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    invite_code TEXT NOT NULL,
                    referred_by_code TEXT,
                    referral_reward_paid INTEGER NOT NULL DEFAULT 0,
                    product_wallet DOUBLE PRECISION NOT NULL DEFAULT 0,
                    earnings_wallet DOUBLE PRECISION NOT NULL DEFAULT 0,
                    vip_level_id INTEGER NOT NULL DEFAULT 0,
                    vip_level_name TEXT NOT NULL DEFAULT 'Starter',
                    vip_expires_at BIGINT NOT NULL DEFAULT 0,
                    last_claim_at BIGINT NOT NULL DEFAULT 0,
                    last_claim_day TEXT NOT NULL DEFAULT '',
                    vip2_invites_count INTEGER NOT NULL DEFAULT 0,
                    vip2_bonus_claimed INTEGER NOT NULL DEFAULT 0,
                    last_free_spin_at BIGINT NOT NULL DEFAULT 0,
                    last_free_spin_day TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at BIGINT NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    id BIGINT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    user_name TEXT NOT NULL,
                    user_phone TEXT NOT NULL,
                    tx_type TEXT NOT NULL,
                    amount DOUBLE PRECISION NOT NULL DEFAULT 0,
                    reference TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    details TEXT NOT NULL DEFAULT '',
                    created_at BIGINT NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS spin_orders (
                    id BIGINT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    user_name TEXT NOT NULL,
                    device_id BIGINT NOT NULL,
                    device_name TEXT NOT NULL,
                    spin_type TEXT NOT NULL,
                    amount_paid DOUBLE PRECISION NOT NULL DEFAULT 0,
                    paystack_ref TEXT NOT NULL DEFAULT '',
                    recipient_phone TEXT NOT NULL DEFAULT '',
                    region TEXT NOT NULL DEFAULT '',
                    address TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'Processing Delivery',
                    created_at BIGINT NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS chat_messages (
                    id BIGINT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    sender TEXT NOT NULL,
                    message TEXT NOT NULL,
                    is_read INTEGER NOT NULL DEFAULT 0,
                    created_at BIGINT NOT NULL DEFAULT 0
                );
            """)

            defaults = {
                'admin_pin': '236963',
                'min_withdrawal': '500',
                'company_name': 'TravestCapital',
                'about_company_text': 'TravestCapital (TC) is an institutional equity and asset platform focused on transparent share packages, partner recruitment rewards, and device spin opportunities.',
                'about_company_image': '',
                'about_company_video': '',
                'invite_vip2_target': '10',
                'invite_vip2_reward': '2000',
                'paystack_public_key': PAYSTACK_PUBLIC_KEY,
                'paystack_secret_key': PAYSTACK_SECRET_KEY
            }
            for k, v in defaults.items():
                cur.execute(
                    "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                    (k, v)
                )

            cur.execute("SELECT id FROM vip_tiers LIMIT 1")
            if not cur.fetchone():
                default_tiers = [
                    (1, 'VIP 1', 300.0, 50.0, 15, 0),
                    (2, 'VIP 2', 600.0, 110.0, 15, 1),
                    (3, 'VIP 3', 1200.0, 230.0, 15, 0),
                    (4, 'VIP 4', 2500.0, 500.0, 15, 0),
                ]
                for tid, name, price, profit, days, vip2 in default_tiers:
                    cur.execute(
                        """
                        INSERT INTO vip_tiers (id, name, price, daily_profit, duration_days, is_vip2_target)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (tid, name, price, profit, days, vip2)
                    )
                cur.execute(
                    """
                    INSERT INTO counters (name, val) VALUES ('vip_tiers', 4)
                    ON CONFLICT (name) DO UPDATE SET val = GREATEST(counters.val, 4)
                    """
                )

            cur.execute("SELECT id FROM spin_devices LIMIT 1")
            if not cur.fetchone():
                now = int(time.time())
                default_devices = [
                    (1, 'iPhone XR', '128GB Storage • 4GB RAM • Liquid Retina HD', '', 'free', 0.0, '[]', 'random', now),
                    (2, 'Samsung Galaxy S23 Ultra', '256GB Storage • 12GB RAM • 200MP Camera', '', 'sale', 6500.0, '[2800, 3200, 3500, 3900, 4100, 4500]', 'random', now),
                    (3, 'Apple MacBook Air M2', '512GB SSD • 8GB Unified Memory • Midnight', '', 'free', 0.0, '[]', 'hard', now),
                    (4, 'PlayStation 5 Slim', '1TB Ultra-High Speed SSD • DualSense Bundle', '', 'sale', 5800.0, '[2200, 2500, 2900, 3100, 3400, 3700]', 'random', now)
                ]
                for did, name, specs, img, stype, orig, disc, diff, created in default_devices:
                    cur.execute(
                        """
                        INSERT INTO spin_devices (id, name, specs, image_url, spin_type, original_price, discount_options, difficulty, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (did, name, specs, img, stype, orig, disc, diff, created)
                    )
                cur.execute(
                    """
                    INSERT INTO counters (name, val) VALUES ('spin_devices', 4)
                    ON CONFLICT (name) DO UPDATE SET val = GREATEST(counters.val, 4)
                    """
                )

            cur.execute("SELECT id FROM news LIMIT 1")
            if not cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO news (id, title, body, created_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        1,
                        'Welcome to TravestCapital (TC) Institutional Yield Platform',
                        'Earn daily dividends on your share packages, invite 10 VIP 2 partners for an instant GHS 2,000 bonus, and spin for flagship devices!',
                        int(time.time())
                    )
                )
                cur.execute(
                    """
                    INSERT INTO counters (name, val) VALUES ('news', 1)
                    ON CONFLICT (name) DO UPDATE SET val = GREATEST(counters.val, 1)
                    """
                )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


init_db()


def get_setting(key, default=''):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = %s LIMIT 1", (key,))
            row = cur.fetchone()
            if row is not None:
                return row.get('value', default)
            return default
    finally:
        conn.close()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = session.get('user_id')
        if not uid:
            return jsonify({'ok': False, 'error': 'Authentication required'}), 401
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE id = %s LIMIT 1", (int(uid),))
                user = cur.fetchone()
        finally:
            conn.close()
        if not user:
            session.clear()
            return jsonify({'ok': False, 'error': 'User not found'}), 401
        if user.get('is_active', 1) == 0:
            session.clear()
            return jsonify({'ok': False, 'error': 'Your account has been deactivated by Admin.'}), 403
        return f(dict(user), *args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
            return jsonify({'ok': False, 'error': 'Admin authorization required'}), 401
        return f(*args, **kwargs)
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
    phone = normalize_ghana_phone(raw_phone)
    password = (data.get('password') or '').strip()
    referral_code = (data.get('referral_code') or '').strip().upper()

    if not name or not raw_phone or not password:
        return jsonify({'ok': False, 'error': 'Full name, phone number, and password are required.'}), 400
    if not phone:
        return jsonify({'ok': False, 'error': 'Enter a valid Ghana MTN (024, 054, 055, 059, 025) or Telecel (020, 050) phone number.'}), 400
    if len(password) < 4:
        return jsonify({'ok': False, 'error': 'Password must be at least 4 characters.'}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE phone = %s LIMIT 1", (phone,))
            if cur.fetchone():
                return jsonify({'ok': False, 'error': 'This phone number is already registered.'}), 400

            valid_ref = None
            if referral_code:
                cur.execute("SELECT invite_code FROM users WHERE invite_code = %s LIMIT 1", (referral_code,))
                ref_row = cur.fetchone()
                if not ref_row:
                    return jsonify({'ok': False, 'error': 'Invalid referral code. Leave blank if none.'}), 400
                valid_ref = ref_row['invite_code']

            invite_code = generate_invite_code()
            while True:
                cur.execute("SELECT id FROM users WHERE invite_code = %s LIMIT 1", (invite_code,))
                if not cur.fetchone():
                    break
                invite_code = generate_invite_code()

            uid = get_next_sequence_id('users', conn=conn)
            pw_hash = generate_password_hash(password)
            now = int(time.time())

            cur.execute(
                """
                INSERT INTO users (
                    id, name, phone, password_hash, invite_code, referred_by_code,
                    referral_reward_paid, product_wallet, earnings_wallet, vip_level_id,
                    vip_level_name, vip_expires_at, last_claim_at, last_claim_day,
                    vip2_invites_count, vip2_bonus_claimed, last_free_spin_at,
                    last_free_spin_day, is_active, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    0, 0.0, 0.0, 0,
                    'Starter', 0, 0, '',
                    0, 0, 0,
                    '', 1, %s
                )
                """,
                (uid, name, phone, pw_hash, invite_code, valid_ref, now)
            )
        conn.commit()
        return jsonify({'ok': True, 'message': 'Account created successfully! Please sign in.'})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json(force=True)
    raw_phone = (data.get('phone') or '').strip()
    phone = normalize_ghana_phone(raw_phone) or raw_phone
    password = (data.get('password') or '').strip()

    if not phone or not password:
        return jsonify({'ok': False, 'error': 'Enter your phone number and password.'}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE phone = %s LIMIT 1", (phone,))
            user = cur.fetchone()
            if not user and raw_phone != phone:
                cur.execute("SELECT * FROM users WHERE phone = %s LIMIT 1", (raw_phone,))
                user = cur.fetchone()
    finally:
        conn.close()

    if not user:
        return jsonify({'ok': False, 'error': 'Invalid phone number or password.'}), 401

    if not check_password_hash(user['password_hash'], password):
        return jsonify({'ok': False, 'error': 'Invalid phone number or password.'}), 401

    if user.get('is_active', 1) == 0:
        return jsonify({'ok': False, 'error': 'Account deactivated. Contact Support.'}), 403

    session['user_id'] = user['id']
    return jsonify({'ok': True, 'user_name': user['name']})


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.pop('user_id', None)
    return jsonify({'ok': True})


# ==============================================================================
# USER STATE, DASHBOARD & WALLET API
# ==============================================================================
@app.route('/api/user/state', methods=['GET'])
@login_required
def api_user_state(user):
    now = int(time.time())
    today_str, seconds_to_midnight = get_ghana_day_info(now)

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            vip_level_id = user['vip_level_id']
            vip_expires_at = user['vip_expires_at']
            if vip_level_id > 0 and vip_expires_at > 0 and now >= vip_expires_at:
                cur.execute(
                    """
                    UPDATE users
                    SET vip_level_id = 0, vip_level_name = 'Starter (Expired)', vip_expires_at = 0
                    WHERE id = %s
                    """,
                    (user['id'],)
                )
                cur.execute("SELECT * FROM users WHERE id = %s LIMIT 1", (user['id'],))
                refreshed = cur.fetchone()
                if refreshed:
                    user = dict(refreshed)

            cur.execute("SELECT * FROM vip_tiers ORDER BY price ASC")
            tiers = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT * FROM spin_devices ORDER BY created_at DESC")
            devices = [dict(r) for r in cur.fetchall()]
            for d in devices:
                try:
                    if isinstance(d.get('discount_options'), str):
                        d['discount_options'] = json.loads(d['discount_options'])
                except Exception:
                    d['discount_options'] = []

            cur.execute("SELECT * FROM news ORDER BY created_at DESC LIMIT 10")
            news = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT * FROM transactions WHERE user_id = %s ORDER BY created_at DESC LIMIT 25", (user['id'],))
            txs = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT * FROM spin_orders WHERE user_id = %s ORDER BY created_at DESC", (user['id'],))
            spin_orders_docs = [dict(r) for r in cur.fetchall()]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    can_claim_daily = False
    next_claim_in = 0
    active_tier = None
    last_claim_day = user.get('last_claim_day') or ''
    if not last_claim_day and (user.get('last_claim_at') or 0) > 0:
        last_claim_day, _ = get_ghana_day_info(user.get('last_claim_at'))

    if user['vip_level_id'] > 0 and user['vip_expires_at'] > now:
        for t in tiers:
            if t['id'] == user['vip_level_id']:
                active_tier = t
                break
        if active_tier:
            if last_claim_day != today_str:
                can_claim_daily = True
            else:
                next_claim_in = seconds_to_midnight

    last_free_spin_day = user.get('last_free_spin_day') or ''
    if not last_free_spin_day and (user.get('last_free_spin_at') or 0) > 0:
        last_free_spin_day, _ = get_ghana_day_info(user.get('last_free_spin_at'))

    can_free_spin_today = last_free_spin_day != today_str
    free_spin_wait = 0 if can_free_spin_today else seconds_to_midnight

    has_vip2 = False
    if active_tier and (active_tier.get('is_vip2_target') == 1 or float(active_tier.get('price', 0)) >= 600):
        has_vip2 = True

    min_withdrawal = float(get_setting('min_withdrawal', '500'))
    paystack_pk = get_setting('paystack_public_key', PAYSTACK_PUBLIC_KEY)
    invite_target = int(float(get_setting('invite_vip2_target', '10')))
    invite_reward = float(get_setting('invite_vip2_reward', '2000'))
    about_company = {
        'company_name': get_setting('company_name', 'TravestCapital'),
        'text': get_setting('about_company_text', ''),
        'image_url': get_setting('about_company_image', ''),
        'video_url': get_setting('about_company_video', '')
    }

    return jsonify({
        'ok': True,
        'server_time': now,
        'paystack_public_key': paystack_pk,
        'min_withdrawal': min_withdrawal,
        'invite_vip2_target': invite_target,
        'invite_vip2_reward': invite_reward,
        'about_company': about_company,
        'user': {
            'id': user['id'],
            'name': user['name'],
            'phone': user['phone'],
            'invite_code': user['invite_code'],
            'product_wallet': round(float(user.get('product_wallet', 0.0)), 2),
            'earnings_wallet': round(float(user.get('earnings_wallet', 0.0)), 2),
            'vip_level_id': user['vip_level_id'],
            'vip_level_name': user['vip_level_name'],
            'vip_expires_at': user['vip_expires_at'],
            'vip2_invites_count': user.get('vip2_invites_count', 0),
            'vip2_bonus_claimed': user.get('vip2_bonus_claimed', 0),
            'has_vip2': has_vip2
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
        'spin_orders': spin_orders_docs
    })


@app.route('/api/user/deposit', methods=['POST'])
@login_required
def api_user_deposit(user):
    data = request.get_json(force=True)
    amount = float(data.get('amount') or 0)
    reference = (data.get('reference') or '').strip()

    if amount <= 0 or not reference:
        return jsonify({'ok': False, 'error': 'Invalid deposit amount or Paystack reference.'}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM transactions WHERE reference = %s LIMIT 1", (reference,))
            if cur.fetchone():
                return jsonify({'ok': False, 'error': 'Transaction already processed.'}), 400

            now = int(time.time())
            cur.execute(
                "UPDATE users SET product_wallet = product_wallet + %s WHERE id = %s",
                (amount, user['id'])
            )
            tx_id = get_next_sequence_id('transactions', conn=conn)
            cur.execute(
                """
                INSERT INTO transactions (
                    id, user_id, user_name, user_phone, tx_type, amount, reference, status, details, created_at
                ) VALUES (%s, %s, %s, %s, 'deposit', %s, %s, 'completed', 'Paystack Product Wallet Deposit', %s)
                """,
                (tx_id, user['id'], user['name'], user['phone'], amount, reference, now)
            )
        conn.commit()
        return jsonify({'ok': True, 'message': f'GHS {amount:,.2f} credited to your Product Wallet!'})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def process_share_activation(user_row, tier_row, pay_source, reference='', conn=None):
    own_conn = conn is None
    if own_conn:
        conn = get_db_connection()
    try:
        now = int(time.time())
        expires_at = now + (int(tier_row['duration_days']) * 86400)

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET vip_level_id = %s, vip_level_name = %s, vip_expires_at = %s, last_claim_at = 0, last_claim_day = ''
                WHERE id = %s
                """,
                (tier_row['id'], tier_row['name'], expires_at, user_row['id'])
            )

            tx_id = get_next_sequence_id('transactions', conn=conn)
            cur.execute(
                """
                INSERT INTO transactions (
                    id, user_id, user_name, user_phone, tx_type, amount, reference, status, details, created_at
                ) VALUES (%s, %s, %s, %s, 'share_buy', %s, %s, 'completed', %s, %s)
                """,
                (
                    tx_id,
                    user_row['id'],
                    user_row['name'],
                    user_row['phone'],
                    float(tier_row['price']),
                    reference,
                    f"Purchased {tier_row['name']} ({pay_source})",
                    now
                )
            )

            if user_row.get('referred_by_code') and user_row.get('referral_reward_paid', 0) == 0:
                cur.execute(
                    "SELECT * FROM users WHERE invite_code = %s LIMIT 1",
                    (user_row['referred_by_code'],)
                )
                referrer = cur.fetchone()
                if referrer:
                    commission = round(float(tier_row['price']) * 0.10, 2)
                    cur.execute(
                        "UPDATE users SET earnings_wallet = earnings_wallet + %s WHERE id = %s",
                        (commission, referrer['id'])
                    )
                    cur.execute(
                        "UPDATE users SET referral_reward_paid = 1 WHERE id = %s",
                        (user_row['id'],)
                    )
                    comm_tx_id = get_next_sequence_id('transactions', conn=conn)
                    cur.execute(
                        """
                        INSERT INTO transactions (
                            id, user_id, user_name, user_phone, tx_type, amount, reference, status, details, created_at
                        ) VALUES (%s, %s, %s, %s, 'commission', %s, %s, 'completed', %s, %s)
                        """,
                        (
                            comm_tx_id,
                            referrer['id'],
                            referrer['name'],
                            referrer['phone'],
                            commission,
                            reference,
                            f"10% Referral Bonus from {user_row['name']}",
                            now
                        )
                    )
        if own_conn:
            conn.commit()
    except Exception:
        if own_conn:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


@app.route('/api/user/buy-share', methods=['POST'])
@login_required
def api_buy_share(user):
    data = request.get_json(force=True)
    tier_id = int(data.get('tier_id') or 0)
    method = data.get('method')
    reference = (data.get('reference') or '').strip()
    vip2_recruit_code = (data.get('vip2_recruit_code') or '').strip().upper()

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM vip_tiers WHERE id = %s LIMIT 1", (tier_id,))
            tier = cur.fetchone()
            if not tier:
                return jsonify({'ok': False, 'error': 'Selected VIP tier does not exist.'}), 404
            tier = dict(tier)
            price = float(tier['price'])

            recruiter = None
            if vip2_recruit_code:
                if tier.get('is_vip2_target') != 1 and price < 600:
                    return jsonify({'ok': False, 'error': 'Get Hired recruitment codes only apply to VIP 2 shares.'}), 400
                cur.execute("SELECT * FROM users WHERE invite_code = %s LIMIT 1", (vip2_recruit_code,))
                rec_row = cur.fetchone()
                if not rec_row:
                    return jsonify({'ok': False, 'error': 'Invalid Get Hired unique invitation code.'}), 400
                recruiter = dict(rec_row)
                if recruiter['id'] == user['id']:
                    return jsonify({'ok': False, 'error': 'You cannot use your own Get Hired code.'}), 400

            cur.execute("SELECT * FROM users WHERE id = %s LIMIT 1", (user['id'],))
            curr_row = cur.fetchone()
            current_user = dict(curr_row) if curr_row else user

            if method == 'wallet':
                if float(current_user.get('product_wallet', 0.0)) < price:
                    return jsonify({'ok': False, 'error': f'Insufficient Product Wallet balance (GHS {float(current_user.get("product_wallet", 0.0)):,.2f}). Deposit or pay directly via Paystack.'}), 400
                cur.execute(
                    "UPDATE users SET product_wallet = product_wallet - %s WHERE id = %s",
                    (price, user['id'])
                )
                ref_code = 'WAL-' + uuid.uuid4().hex[:10].upper()
                process_share_activation(current_user, tier, 'Product Wallet', ref_code, conn=conn)
            elif method == 'paystack':
                if not reference:
                    return jsonify({'ok': False, 'error': 'Missing Paystack payment reference.'}), 400
                cur.execute("SELECT id FROM transactions WHERE reference = %s LIMIT 1", (reference,))
                if cur.fetchone():
                    return jsonify({'ok': False, 'error': 'Payment reference already used.'}), 400
                process_share_activation(current_user, tier, 'Paystack Direct', reference, conn=conn)
            else:
                return jsonify({'ok': False, 'error': 'Invalid payment method.'}), 400

            if recruiter:
                cur.execute(
                    "UPDATE users SET vip2_invites_count = vip2_invites_count + 1 WHERE id = %s",
                    (recruiter['id'],)
                )
        conn.commit()
        return jsonify({
            'ok': True,
            'message': f'{tier["name"]} activated! Click "Claim Daily Profit" now to collect your first GHS {tier["daily_profit"]:,.2f}.'
        })
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/user/claim-daily', methods=['POST'])
@login_required
def api_claim_daily(user):
    now = int(time.time())
    today_str, seconds_to_midnight = get_ghana_day_info(now)

    if user['vip_level_id'] <= 0 or user['vip_expires_at'] <= now:
        return jsonify({'ok': False, 'error': 'No active VIP share package. Please purchase shares first.'}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM vip_tiers WHERE id = %s LIMIT 1", (user['vip_level_id'],))
            tier = cur.fetchone()
            if not tier:
                return jsonify({'ok': False, 'error': 'VIP tier not found.'}), 404

            last_claim_day = user.get('last_claim_day') or ''
            if not last_claim_day and (user.get('last_claim_at') or 0) > 0:
                last_claim_day, _ = get_ghana_day_info(user.get('last_claim_at'))

            if last_claim_day == today_str:
                hrs = seconds_to_midnight // 3600
                mins = (seconds_to_midnight % 3600) // 60
                return jsonify({'ok': False, 'error': f'Daily profit already claimed today. Resets at 00:00 ({hrs}h {mins}m left).'}), 400

            profit = float(tier['daily_profit'])
            cur.execute(
                """
                UPDATE users
                SET earnings_wallet = earnings_wallet + %s, last_claim_at = %s, last_claim_day = %s
                WHERE id = %s
                """,
                (profit, now, today_str, user['id'])
            )

            tx_id = get_next_sequence_id('transactions', conn=conn)
            cur.execute(
                """
                INSERT INTO transactions (
                    id, user_id, user_name, user_phone, tx_type, amount, reference, status, details, created_at
                ) VALUES (%s, %s, %s, %s, 'daily_claim', %s, %s, 'completed', %s, %s)
                """,
                (
                    tx_id,
                    user['id'],
                    user['name'],
                    user['phone'],
                    profit,
                    'CLM-' + uuid.uuid4().hex[:8].upper(),
                    f"{tier['name']} Daily Dividend Claim",
                    now
                )
            )
        conn.commit()
        return jsonify({'ok': True, 'message': f'GHS {profit:,.2f} daily profit added to your Earnings Wallet!'})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/user/claim-vip2-bonus', methods=['POST'])
@login_required
def api_claim_vip2_bonus(user):
    target = int(float(get_setting('invite_vip2_target', '10')))
    reward = float(get_setting('invite_vip2_reward', '2000'))
    current_invites = int(user.get('vip2_invites_count', 0))

    if current_invites < target:
        return jsonify({'ok': False, 'error': f'You need {target} VIP 2 recruits (currently {current_invites}/{target}).'}), 400
    if user.get('vip2_bonus_claimed', 0) == 1:
        return jsonify({'ok': False, 'error': f'You have already claimed your GHS {reward:,.2f} recruitment milestone bonus.'}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            now = int(time.time())
            cur.execute(
                "UPDATE users SET earnings_wallet = earnings_wallet + %s, vip2_bonus_claimed = 1 WHERE id = %s",
                (reward, user['id'])
            )
            tx_id = get_next_sequence_id('transactions', conn=conn)
            cur.execute(
                """
                INSERT INTO transactions (
                    id, user_id, user_name, user_phone, tx_type, amount, reference, status, details, created_at
                ) VALUES (%s, %s, %s, %s, 'vip2_bonus', %s, %s, 'completed', %s, %s)
                """,
                (
                    tx_id,
                    user['id'],
                    user['name'],
                    user['phone'],
                    reward,
                    'HIRE-' + uuid.uuid4().hex[:8].upper(),
                    f'Get Hired {target} VIP2 Milestone Reward',
                    now
                )
            )
        conn.commit()
        return jsonify({'ok': True, 'message': f'Congratulations! GHS {reward:,.2f} added to your Earnings Wallet!'})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/user/withdraw', methods=['POST'])
@login_required
def api_user_withdraw(user):
    data = request.get_json(force=True)
    amount = float(data.get('amount') or 0)
    momo_network = (data.get('network') or 'MTN MoMo').strip()
    momo_number = (data.get('account_number') or user['phone']).strip()
    account_name = (data.get('account_name') or user['name']).strip()

    if not momo_number or not account_name:
        return jsonify({'ok': False, 'error': 'Please enter your Mobile Money number and registered account name.'}), 400

    min_w = float(get_setting('min_withdrawal', '500'))
    if amount < min_w:
        return jsonify({'ok': False, 'error': f'Minimum withdrawal amount is GHS {min_w:,.2f}.'}), 400
    if float(user.get('earnings_wallet', 0.0)) < amount:
        return jsonify({'ok': False, 'error': 'Insufficient Earnings Wallet balance. Product Wallet cannot be withdrawn directly.'}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            now = int(time.time())
            ref = 'WDR-' + uuid.uuid4().hex[:10].upper()

            cur.execute(
                "UPDATE users SET earnings_wallet = earnings_wallet - %s WHERE id = %s",
                (amount, user['id'])
            )
            tx_id = get_next_sequence_id('transactions', conn=conn)
            cur.execute(
                """
                INSERT INTO transactions (
                    id, user_id, user_name, user_phone, tx_type, amount, reference, status, details, created_at
                ) VALUES (%s, %s, %s, %s, 'withdrawal', %s, %s, 'Pending Payout', %s, %s)
                """,
                (
                    tx_id,
                    user['id'],
                    user['name'],
                    user['phone'],
                    amount,
                    ref,
                    f"{momo_network} • {momo_number} ({account_name})",
                    now
                )
            )
        conn.commit()
        return jsonify({'ok': True, 'message': f'Withdrawal request of GHS {amount:,.2f} submitted! Your Mobile Money payout is being processed.'})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ==============================================================================
# SPIN WHEEL API
# ==============================================================================
@app.route('/api/user/spin', methods=['POST'])
@login_required
def api_user_spin(user):
    data = request.get_json(force=True)
    device_id = int(data.get('device_id') or 0)
    now = int(time.time())
    today_str, seconds_to_midnight = get_ghana_day_info(now)

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM spin_devices WHERE id = %s LIMIT 1", (device_id,))
            device = cur.fetchone()
            if not device:
                return jsonify({'ok': False, 'error': 'Device not found.'}), 404
            device = dict(device)

            if device['spin_type'] == 'free':
                tier_id = user['vip_level_id']
                tier = None
                if tier_id > 0:
                    cur.execute("SELECT * FROM vip_tiers WHERE id = %s LIMIT 1", (tier_id,))
                    tier_row = cur.fetchone()
                    tier = dict(tier_row) if tier_row else None

                is_vip2 = tier and (tier.get('is_vip2_target') == 1 or float(tier.get('price', 0)) >= 600) and user['vip_expires_at'] > now
                if not is_vip2:
                    return jsonify({'ok': False, 'error': 'Free Device Spin is strictly reserved for active VIP 2 shareholders!'}), 403

                last_free_spin_day = user.get('last_free_spin_day') or ''
                if not last_free_spin_day and (user.get('last_free_spin_at') or 0) > 0:
                    last_free_spin_day, _ = get_ghana_day_info(user.get('last_free_spin_at'))

                if last_free_spin_day == today_str:
                    hrs = seconds_to_midnight // 3600
                    mins = (seconds_to_midnight % 3600) // 60
                    return jsonify({'ok': False, 'error': f'You have already used your free spin today. Resets at 00:00 ({hrs}h {mins}m left).'}), 400

                if device.get('difficulty') == 'hard':
                    is_win = random.random() < 0.05
                else:
                    is_win = random.choice([True, False])

                segment_index = random.choice([0, 2, 4]) if is_win else random.choice([1, 3, 5])
                cur.execute(
                    "UPDATE users SET last_free_spin_at = %s, last_free_spin_day = %s WHERE id = %s",
                    (now, today_str, user['id'])
                )
                conn.commit()
                return jsonify({
                    'ok': True,
                    'spin_type': 'free',
                    'won': is_win,
                    'segment_index': segment_index,
                    'label': 'WIN' if is_win else 'LOST'
                })
            else:
                try:
                    options = json.loads(device.get('discount_options', '[]'))
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
                    'label': f'GHS {won_price:,.0f}'
                })
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/user/spin-claim', methods=['POST'])
@login_required
def api_user_spin_claim(user):
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

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM spin_devices WHERE id = %s LIMIT 1", (device_id,))
            device = cur.fetchone()
            if not device:
                return jsonify({'ok': False, 'error': 'Device not found.'}), 404
            device = dict(device)

            if spin_type == 'sale':
                if amount_paid <= 0 or not paystack_ref:
                    return jsonify({'ok': False, 'error': 'Paystack payment confirmation is required before submitting delivery details.'}), 400
                cur.execute("SELECT id FROM spin_orders WHERE paystack_ref = %s LIMIT 1", (paystack_ref,))
                if cur.fetchone():
                    return jsonify({'ok': False, 'error': 'This Paystack payment reference has already been submitted.'}), 400

            now = int(time.time())
            order_id = get_next_sequence_id('spin_orders', conn=conn)
            cur.execute(
                """
                INSERT INTO spin_orders (
                    id, user_id, user_name, device_id, device_name, spin_type,
                    amount_paid, paystack_ref, recipient_phone, region, address, status, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Processing Delivery', %s)
                """,
                (
                    order_id,
                    user['id'],
                    user['name'],
                    device['id'],
                    device['name'],
                    spin_type,
                    amount_paid,
                    paystack_ref,
                    recipient_phone,
                    region,
                    address,
                    now
                )
            )

            if spin_type == 'sale':
                tx_id = get_next_sequence_id('transactions', conn=conn)
                cur.execute(
                    """
                    INSERT INTO transactions (
                        id, user_id, user_name, user_phone, tx_type, amount, reference, status, details, created_at
                    ) VALUES (%s, %s, %s, %s, 'deposit', %s, %s, 'completed', %s, %s)
                    """,
                    (
                        tx_id,
                        user['id'],
                        user['name'],
                        user['phone'],
                        amount_paid,
                        paystack_ref,
                        f"Discount Spin Purchase: {device['name']}",
                        now
                    )
                )
        conn.commit()
        return jsonify({'ok': True, 'message': 'Delivery details submitted! Admin will dispatch your device shortly.'})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ==============================================================================
# USER ACCOUNT SETTINGS (MINE TAB)
# ==============================================================================
@app.route('/api/user/reset-password', methods=['POST'])
@login_required
def api_user_reset_password(user):
    data = request.get_json(force=True)
    old_pw = (data.get('old_password') or '').strip()
    new_pw = (data.get('new_password') or '').strip()

    if not check_password_hash(user['password_hash'], old_pw):
        return jsonify({'ok': False, 'error': 'Current password is incorrect.'}), 400
    if len(new_pw) < 4:
        return jsonify({'ok': False, 'error': 'New password must be at least 4 characters.'}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (generate_password_hash(new_pw), user['id'])
            )
        conn.commit()
        return jsonify({'ok': True, 'message': 'Password updated successfully!'})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/user/change-phone', methods=['POST'])
@login_required
def api_user_change_phone(user):
    data = request.get_json(force=True)
    raw_phone = (data.get('new_phone') or '').strip()
    new_phone = normalize_ghana_phone(raw_phone)
    password = (data.get('password') or '').strip()

    if not raw_phone or not new_phone:
        return jsonify({'ok': False, 'error': 'Enter a valid Ghana MTN (024, 054, 055, 059) or Telecel (020, 050) phone number.'}), 400
    if not check_password_hash(user['password_hash'], password):
        return jsonify({'ok': False, 'error': 'Password verification failed.'}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE phone = %s", (new_phone,))
            existing_docs = cur.fetchall() or []
            if any(d['id'] != user['id'] for d in existing_docs):
                return jsonify({'ok': False, 'error': 'Phone number already in use by another account.'}), 400

            cur.execute("UPDATE users SET phone = %s WHERE id = %s", (new_phone, user['id']))
        conn.commit()
        return jsonify({'ok': True, 'message': 'Phone number updated successfully!'})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ==============================================================================
# REAL-TIME LIVE CHAT API
# ==============================================================================
@app.route('/api/chat/messages', methods=['GET'])
@login_required
def api_user_chat_messages(user):
    after_id = int(request.args.get('after_id') or 0)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM chat_messages WHERE user_id = %s ORDER BY id ASC", (user['id'],))
            msg_docs = [dict(r) for r in cur.fetchall()]

            filtered = []
            for m in msg_docs:
                if m['id'] > after_id:
                    filtered.append(m)
                if m.get('sender') == 'admin' and m.get('is_read', 0) == 0:
                    cur.execute("UPDATE chat_messages SET is_read = 1 WHERE id = %s", (m['id'],))
        conn.commit()
        return jsonify({'ok': True, 'messages': filtered})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/chat/send', methods=['POST'])
@login_required
def api_user_chat_send(user):
    data = request.get_json(force=True)
    msg = (data.get('message') or '').strip()
    if not msg:
        return jsonify({'ok': False, 'error': 'Message cannot be empty.'}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            now = int(time.time())
            mid = get_next_sequence_id('chat_messages', conn=conn)
            msg_obj = {
                'id': mid,
                'user_id': user['id'],
                'sender': 'user',
                'message': msg,
                'is_read': 0,
                'created_at': now
            }
            cur.execute(
                """
                INSERT INTO chat_messages (id, user_id, sender, message, is_read, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (mid, user['id'], 'user', msg, 0, now)
            )
        conn.commit()
        return jsonify({'ok': True, 'message': msg_obj})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ==============================================================================
# ADMIN SECRET 6-DIGIT LOGIN & FULL CONTROL API
# ==============================================================================
@app.route('/api/admin/login', methods=['POST'])
def api_admin_login():
    data = request.get_json(force=True)
    pin = (data.get('pin') or '').strip()
    expected = get_setting('admin_pin', '236963')
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
def api_admin_overview():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users ORDER BY id DESC")
            users = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT * FROM vip_tiers ORDER BY price ASC")
            tiers = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT * FROM spin_devices ORDER BY created_at DESC")
            devices = [dict(r) for r in cur.fetchall()]
            for d in devices:
                try:
                    if isinstance(d.get('discount_options'), str):
                        d['discount_options'] = json.loads(d['discount_options'])
                except Exception:
                    d['discount_options'] = []

            cur.execute("SELECT * FROM spin_orders ORDER BY created_at DESC")
            spin_orders = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT * FROM news ORDER BY created_at DESC")
            news = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT * FROM transactions ORDER BY created_at DESC")
            transactions = [dict(r) for r in cur.fetchall()]

            cur.execute("SELECT * FROM chat_messages ORDER BY id ASC")
            all_messages = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    limited_txs = transactions[:100]

    start_of_today = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    paid_today = sum(float(t.get('amount', 0)) for t in transactions if t.get('tx_type') in ('deposit', 'share_buy') and t.get('created_at', 0) >= start_of_today)
    total_paid = sum(float(t.get('amount', 0)) for t in transactions if t.get('tx_type') in ('deposit', 'share_buy'))
    total_withdrawn = sum(float(t.get('amount', 0)) for t in transactions if t.get('tx_type') == 'withdrawal')

    user_chat_map = {}
    for m in all_messages:
        uid = m['user_id']
        if uid not in user_chat_map:
            user_chat_map[uid] = []
        user_chat_map[uid].append(m)

    threads = []
    for u in users:
        uid = u['id']
        msgs = user_chat_map.get(uid, [])
        if msgs:
            msgs.sort(key=lambda x: x.get('id', 0))
            last_m = msgs[-1]
            unread_cnt = sum(1 for m in msgs if m.get('sender') == 'user' and m.get('is_read', 0) == 0)
            threads.append({
                'user_id': uid,
                'name': u['name'],
                'phone': u['phone'],
                'last_message': last_m.get('message', ''),
                'last_time': last_m.get('created_at', 0),
                'unread_count': unread_cnt
            })
    threads.sort(key=lambda x: x.get('last_time', 0), reverse=True)

    min_withdrawal = float(get_setting('min_withdrawal', '500'))
    paystack_pk = get_setting('paystack_public_key', PAYSTACK_PUBLIC_KEY)
    paystack_sk = get_setting('paystack_secret_key', PAYSTACK_SECRET_KEY)
    invite_target = int(float(get_setting('invite_vip2_target', '10')))
    invite_reward = float(get_setting('invite_vip2_reward', '2000'))
    about_company = {
        'company_name': get_setting('company_name', 'TravestCapital'),
        'text': get_setting('about_company_text', ''),
        'image_url': get_setting('about_company_image', ''),
        'video_url': get_setting('about_company_video', '')
    }

    return jsonify({
        'ok': True,
        'stats': {
            'total_users': len(users),
            'active_users': sum(1 for u in users if u.get('is_active', 1) == 1),
            'paid_today': round(paid_today, 2),
            'total_paid': round(total_paid, 2),
            'total_withdrawn': round(total_withdrawn, 2),
            'min_withdrawal': min_withdrawal
        },
        'settings': {
            'min_withdrawal': min_withdrawal,
            'paystack_public_key': paystack_pk,
            'paystack_secret_key': paystack_sk,
            'invite_vip2_target': invite_target,
            'invite_vip2_reward': invite_reward,
            'about_company': about_company
        },
        'users': users,
        'tiers': tiers,
        'devices': devices,
        'spin_orders': spin_orders,
        'news': news,
        'transactions': limited_txs,
        'chat_threads': threads
    })


@app.route('/api/admin/user/toggle-status', methods=['POST'])
@admin_required
def api_admin_toggle_user():
    data = request.get_json(force=True)
    uid = int(data.get('user_id') or 0)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = %s LIMIT 1", (uid,))
            u = cur.fetchone()
            if not u:
                return jsonify({'ok': False, 'error': 'User not found.'}), 404
            new_status = 0 if u.get('is_active', 1) == 1 else 1
            cur.execute("UPDATE users SET is_active = %s WHERE id = %s", (new_status, uid))
        conn.commit()
        return jsonify({'ok': True, 'is_active': new_status})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/admin/user/adjust-wallet', methods=['POST'])
@admin_required
def api_admin_user_adjust_wallet():
    data = request.get_json(force=True)
    uid = int(data.get('user_id') or 0)
    product_wallet = float(data.get('product_wallet') or 0)
    earnings_wallet = float(data.get('earnings_wallet') or 0)
    vip_level_id = int(data.get('vip_level_id') or 0)

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if vip_level_id > 0:
                cur.execute("SELECT * FROM vip_tiers WHERE id = %s LIMIT 1", (vip_level_id,))
                t = cur.fetchone()
                vip_name = t['name'] if t else 'Starter'
                expires = int(time.time()) + ((int(t['duration_days']) if t else 15) * 86400)
            else:
                vip_name = 'Starter'
                expires = 0

            cur.execute(
                """
                UPDATE users
                SET product_wallet = %s, earnings_wallet = %s, vip_level_id = %s, vip_level_name = %s, vip_expires_at = %s
                WHERE id = %s
                """,
                (product_wallet, earnings_wallet, vip_level_id, vip_name, expires, uid)
            )
        conn.commit()
        return jsonify({'ok': True, 'message': 'User account balances and VIP status updated.'})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/admin/vip/save', methods=['POST'])
@admin_required
def api_admin_vip_save():
    data = request.get_json(force=True)
    tid = data.get('id')
    name = (data.get('name') or '').strip()
    price = float(data.get('price') or 0)
    daily_profit = float(data.get('daily_profit') or 0)
    duration_days = int(data.get('duration_days') or 15)
    is_vip2_target = 1 if data.get('is_vip2_target') else 0

    if not name or price <= 0 or daily_profit <= 0:
        return jsonify({'ok': False, 'error': 'Provide valid VIP name, price, and daily profit.'}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            if tid:
                cur.execute(
                    """
                    UPDATE vip_tiers
                    SET name = %s, price = %s, daily_profit = %s, duration_days = %s, is_vip2_target = %s
                    WHERE id = %s
                    """,
                    (name, price, daily_profit, duration_days, is_vip2_target, int(tid))
                )
            else:
                new_id = get_next_sequence_id('vip_tiers', conn=conn)
                cur.execute(
                    """
                    INSERT INTO vip_tiers (id, name, price, daily_profit, duration_days, is_vip2_target)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (new_id, name, price, daily_profit, duration_days, is_vip2_target)
                )
        conn.commit()
        return jsonify({'ok': True, 'message': 'VIP Share Tier saved successfully.'})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/admin/vip/delete', methods=['POST'])
@admin_required
def api_admin_vip_delete():
    data = request.get_json(force=True)
    tid = int(data.get('id') or 0)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM vip_tiers WHERE id = %s", (tid,))
        conn.commit()
        return jsonify({'ok': True})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/admin/vip/reset-defaults', methods=['POST'])
@admin_required
def api_admin_vip_reset():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM vip_tiers")
            default_tiers = [
                (1, 'VIP 1', 300.0, 50.0, 15, 0),
                (2, 'VIP 2', 600.0, 110.0, 15, 1),
                (3, 'VIP 3', 1200.0, 230.0, 15, 0),
                (4, 'VIP 4', 2500.0, 500.0, 15, 0),
            ]
            for tid, name, price, profit, days, vip2 in default_tiers:
                cur.execute(
                    """
                    INSERT INTO vip_tiers (id, name, price, daily_profit, duration_days, is_vip2_target)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (tid, name, price, profit, days, vip2)
                )
            cur.execute(
                """
                INSERT INTO counters (name, val) VALUES ('vip_tiers', 4)
                ON CONFLICT (name) DO UPDATE SET val = 4
                """
            )
        conn.commit()
        return jsonify({'ok': True, 'message': 'VIP tiers reset to default amounts.'})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/admin/devices/add', methods=['POST'])
@admin_required
def api_admin_device_add():
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
            return jsonify({'ok': False, 'error': 'Discount amounts must be comma-separated numbers.'}), 400
        if len(discount_list) < 2:
            return jsonify({'ok': False, 'error': 'Provide at least 2 discount wheel amounts for a Sale Spin device.'}), 400

    image_url = ''
    if 'image' in request.files:
        f = request.files['image']
        if f and f.filename:
            ext = os.path.splitext(secure_filename(f.filename))[1].lower()
            if ext not in ['.png', '.jpg', '.jpeg', '.webp', '.gif']:
                return jsonify({'ok': False, 'error': 'Unsupported image format.'}), 400
            fname = f"dev_{uuid.uuid4().hex[:12]}{ext}"
            f.save(os.path.join(UPLOAD_FOLDER, fname))
            image_url = f"/static/uploads/{fname}"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            did = get_next_sequence_id('spin_devices', conn=conn)
            cur.execute(
                """
                INSERT INTO spin_devices (
                    id, name, specs, image_url, spin_type, original_price, discount_options, difficulty, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (did, name, specs, image_url, spin_type, original_price, json.dumps(discount_list), difficulty, int(time.time()))
            )
        conn.commit()
        return jsonify({'ok': True, 'message': f'{name} added to Spin Vault!'})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/admin/devices/update', methods=['POST'])
@admin_required
def api_admin_device_update():
    data = request.get_json(force=True)
    did = int(data.get('id') or 0)
    spin_type = data.get('spin_type')
    difficulty = data.get('difficulty')
    original_price = float(data.get('original_price') or 0)
    discounts = data.get('discount_options') or []

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE spin_devices
                SET spin_type = %s, difficulty = %s, original_price = %s, discount_options = %s
                WHERE id = %s
                """,
                (spin_type, difficulty, original_price, json.dumps(discounts), did)
            )
        conn.commit()
        return jsonify({'ok': True, 'message': 'Spin device parameters updated.'})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/admin/devices/delete', methods=['POST'])
@admin_required
def api_admin_device_delete():
    data = request.get_json(force=True)
    did = int(data.get('id') or 0)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM spin_devices WHERE id = %s", (did,))
        conn.commit()
        return jsonify({'ok': True})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/admin/orders/status', methods=['POST'])
@admin_required
def api_admin_order_status():
    data = request.get_json(force=True)
    oid = int(data.get('id') or 0)
    status = (data.get('status') or 'Delivered').strip()
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE spin_orders SET status = %s WHERE id = %s", (status, oid))
        conn.commit()
        return jsonify({'ok': True})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/admin/withdrawals/update', methods=['POST'])
@admin_required
def api_admin_withdrawal_update():
    data = request.get_json(force=True)
    tx_id = int(data.get('id') or 0)
    action = (data.get('action') or '').strip()

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM transactions WHERE id = %s LIMIT 1", (tx_id,))
            tx = cur.fetchone()
            if not tx:
                return jsonify({'ok': False, 'error': 'Withdrawal record not found.'}), 404
            if tx.get('tx_type') != 'withdrawal':
                return jsonify({'ok': False, 'error': 'Not a withdrawal transaction.'}), 400

            if action == 'paid':
                cur.execute("UPDATE transactions SET status = 'Paid' WHERE id = %s", (tx_id,))
                conn.commit()
                return jsonify({'ok': True, 'message': 'Withdrawal marked as Paid!'})
            elif action == 'refund':
                if tx.get('status') == 'Refunded':
                    return jsonify({'ok': False, 'error': 'Already refunded.'}), 400
                cur.execute(
                    "UPDATE users SET earnings_wallet = earnings_wallet + %s WHERE id = %s",
                    (float(tx['amount']), tx['user_id'])
                )
                cur.execute("UPDATE transactions SET status = 'Refunded' WHERE id = %s", (tx_id,))
                conn.commit()
                return jsonify({'ok': True, 'message': f'GHS {float(tx["amount"]):,.2f} refunded back to user Earnings Wallet.'})

        return jsonify({'ok': False, 'error': 'Invalid action.'}), 400
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/admin/news/add', methods=['POST'])
@admin_required
def api_admin_news_add():
    data = request.get_json(force=True)
    title = (data.get('title') or '').strip()
    body = (data.get('body') or '').strip()
    if not title or not body:
        return jsonify({'ok': False, 'error': 'Title and message are required.'}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            nid = get_next_sequence_id('news', conn=conn)
            cur.execute(
                "INSERT INTO news (id, title, body, created_at) VALUES (%s, %s, %s, %s)",
                (nid, title, body, int(time.time()))
            )
        conn.commit()
        return jsonify({'ok': True, 'message': 'Announcement published to all users!'})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/admin/news/delete', methods=['POST'])
@admin_required
def api_admin_news_delete():
    data = request.get_json(force=True)
    nid = int(data.get('id') or 0)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM news WHERE id = %s", (nid,))
        conn.commit()
        return jsonify({'ok': True})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/admin/settings/save', methods=['POST'])
@admin_required
def api_admin_settings_save():
    data = request.get_json(force=True)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            def upsert_setting(k, val):
                cur.execute(
                    """
                    INSERT INTO settings (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    (k, val)
                )

            if 'min_withdrawal' in data and str(data['min_withdrawal']).strip():
                upsert_setting('min_withdrawal', str(float(data['min_withdrawal'])))
            if 'paystack_public_key' in data and str(data['paystack_public_key']).strip():
                upsert_setting('paystack_public_key', str(data['paystack_public_key']).strip())
            if 'paystack_secret_key' in data and str(data['paystack_secret_key']).strip():
                upsert_setting('paystack_secret_key', str(data['paystack_secret_key']).strip())
            if 'invite_vip2_target' in data and str(data['invite_vip2_target']).strip():
                t_val = int(float(data['invite_vip2_target']))
                if t_val < 1:
                    conn.rollback()
                    return jsonify({'ok': False, 'error': 'Required invite count must be at least 1.'}), 400
                upsert_setting('invite_vip2_target', str(t_val))
            if 'invite_vip2_reward' in data and str(data['invite_vip2_reward']).strip():
                r_val = float(data['invite_vip2_reward'])
                if r_val < 0:
                    conn.rollback()
                    return jsonify({'ok': False, 'error': 'Invite reward amount cannot be negative.'}), 400
                upsert_setting('invite_vip2_reward', str(r_val))
            if 'about_company_text' in data:
                upsert_setting('about_company_text', str(data.get('about_company_text') or '').strip())
            if 'about_company_image' in data:
                upsert_setting('about_company_image', str(data.get('about_company_image') or '').strip())
            if 'about_company_video' in data:
                upsert_setting('about_company_video', str(data.get('about_company_video') or '').strip())
            if 'new_admin_pin' in data and str(data['new_admin_pin']).strip():
                pin = str(data['new_admin_pin']).strip()
                if len(pin) != 6 or not pin.isdigit():
                    conn.rollback()
                    return jsonify({'ok': False, 'error': 'Admin PIN must be exactly 6 digits.'}), 400
                upsert_setting('admin_pin', pin)
        conn.commit()
        return jsonify({'ok': True, 'message': 'Platform configuration saved!'})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/admin/chat/messages', methods=['GET'])
@admin_required
def api_admin_chat_messages():
    uid = int(request.args.get('user_id') or 0)
    after_id = int(request.args.get('after_id') or 0)
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM chat_messages WHERE user_id = %s ORDER BY id ASC", (uid,))
            msg_docs = [dict(r) for r in cur.fetchall()]

            filtered = []
            for m in msg_docs:
                if m['id'] > after_id:
                    filtered.append(m)
                if m.get('sender') == 'user' and m.get('is_read', 0) == 0:
                    cur.execute("UPDATE chat_messages SET is_read = 1 WHERE id = %s", (m['id'],))
        conn.commit()
        return jsonify({'ok': True, 'messages': filtered})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route('/api/admin/chat/send', methods=['POST'])
@admin_required
def api_admin_chat_send():
    data = request.get_json(force=True)
    uid = int(data.get('user_id') or 0)
    msg = (data.get('message') or '').strip()
    if not uid or not msg:
        return jsonify({'ok': False, 'error': 'Select a user and enter a reply.'}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            now = int(time.time())
            mid = get_next_sequence_id('chat_messages', conn=conn)
            msg_obj = {
                'id': mid,
                'user_id': uid,
                'sender': 'admin',
                'message': msg,
                'is_read': 0,
                'created_at': now
            }
            cur.execute(
                """
                INSERT INTO chat_messages (id, user_id, sender, message, is_read, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (mid, uid, 'admin', msg, 0, now)
            )
        conn.commit()
        return jsonify({'ok': True, 'message': msg_obj})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
