import requests
import os
import re
import uuid
import time
import random
import string
import json
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("travestcapital.auth")

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'travest_capital_prod_secret_key_2026_x99')

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ==============================================================================
# SUPABASE INITIALIZATION (Render + Supabase)
# ==============================================================================
SUPABASE_URL = os.environ.get('SUPABASE_URL', '').strip()
SUPABASE_KEY = (
    os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    or os.environ.get('SUPABASE_KEY')
    or os.environ.get('SUPABASE_ANON_KEY')
    or ''
).strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY) must be set in environment variables.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==============================================================================
# PAYSTACK CONFIGURATION
# ==============================================================================
PAYSTACK_PUBLIC_KEY = os.environ.get('PAYSTACK_PUBLIC_KEY', 'pk_test_your_paystack_public_key_here')
PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY', 'sk_test_your_paystack_secret_key_here')

# Ghana Standard Time (UTC+0 / GMT) for consistent calendar-day midnight resets
GHANA_TZ = timezone.utc


def get_calendar_day_str(ts=None):
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=GHANA_TZ)
    return dt.strftime('%Y-%m-%d')


def seconds_until_midnight(ts=None):
    now_dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=GHANA_TZ)
    next_midnight = (now_dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0, int((next_midnight - now_dt).total_seconds()))


def normalize_and_validate_ghana_phone(raw_phone):
    if raw_phone is None:
        return None
    if isinstance(raw_phone, (dict, list, tuple, set, bool)):
        return None
    try:
        raw_str = str(raw_phone).strip()
    except Exception:
        return None
    if not raw_str:
        return None

    cleaned = re.sub(r'[\s\-\(\)]+', '', raw_str)
    if cleaned.startswith('+233'):
        cleaned = '0' + cleaned[4:]
    elif cleaned.startswith('233') and len(cleaned) == 12:
        cleaned = '0' + cleaned[3:]

    # Valid Ghana MTN prefixes: 024, 054, 055, 059 (plus 025/053 if needed, strictly validating 10-digit MTN & Telecel)
    # Valid Ghana Telecel prefixes: 020, 050
    if not re.fullmatch(r'^(024|054|055|059|020|050)\d{7}$', cleaned):
        return None
    return cleaned


def _parse_json_body():
    """Safely parse JSON or form payloads without raising HTTP 400/500 on malformed bodies."""
    try:
        data = request.get_json(silent=True)
    except Exception:
        data = None
    if isinstance(data, dict):
        return data
    if request.form:
        try:
            return dict(request.form)
        except Exception:
            return {}
    return {}


def _safe_str(val):
    if val is None or isinstance(val, (dict, list, tuple, set, bool)):
        return ''
    try:
        return str(val).strip()
    except Exception:
        return ''


def generate_invite_code():
    return 'TC' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


def get_setting(key, default=''):
    try:
        res = supabase.table('settings').select('value').eq('key', key).limit(1).execute()
        if res.data:
            return res.data[0].get('value', default)
    except Exception:
        pass
    return default


def set_setting(key, value):
    supabase.table('settings').upsert({'key': key, 'value': str(value)}, on_conflict='key').execute()


def init_db():
    defaults = {
        'admin_pin': '236963',
        'min_withdrawal': '500',
        'company_name': 'TravestCapital',
        'paystack_public_key': PAYSTACK_PUBLIC_KEY,
        'paystack_secret_key': PAYSTACK_SECRET_KEY,
        'about_company_text': 'TravestCapital (TC) is an institutional wealth and equity management platform committed to transparent daily dividend yields, verified mobile money settlements, and hardware partner rewards.',
        'about_company_image': '',
        'about_company_video': '',
        'invite_target_count': '10',
        'invite_reward_amount': '2000'
    }
    try:
        existing_res = supabase.table('settings').select('key').execute()
        existing_keys = {row['key'] for row in (existing_res.data or [])}
        for k, v in defaults.items():
            if k not in existing_keys:
                supabase.table('settings').insert({'key': k, 'value': v}).execute()

        tiers_res = supabase.table('vip_tiers').select('id').limit(1).execute()
        if not tiers_res.data:
            default_tiers = [
                {'name': 'VIP 1', 'price': 300.0, 'daily_profit': 50.0, 'duration_days': 15, 'is_vip2_target': 0},
                {'name': 'VIP 2', 'price': 600.0, 'daily_profit': 110.0, 'duration_days': 15, 'is_vip2_target': 1},
                {'name': 'VIP 3', 'price': 1200.0, 'daily_profit': 230.0, 'duration_days': 15, 'is_vip2_target': 0},
                {'name': 'VIP 4', 'price': 2500.0, 'daily_profit': 500.0, 'duration_days': 15, 'is_vip2_target': 0},
            ]
            supabase.table('vip_tiers').insert(default_tiers).execute()

        devs_res = supabase.table('spin_devices').select('id').limit(1).execute()
        if not devs_res.data:
            now = int(time.time())
            default_devices = [
                {'name': 'iPhone XR', 'specs': '128GB Storage • 4GB RAM • Liquid Retina HD', 'image_url': '', 'spin_type': 'free', 'original_price': 0.0, 'discount_options': '[]', 'difficulty': 'random', 'created_at': now},
                {'name': 'Samsung Galaxy S23 Ultra', 'specs': '256GB Storage • 12GB RAM • 200MP Camera', 'image_url': '', 'spin_type': 'sale', 'original_price': 6500.0, 'discount_options': '[2800, 3200, 3500, 3900, 4100, 4500]', 'difficulty': 'random', 'created_at': now},
                {'name': 'Apple MacBook Air M2', 'specs': '512GB SSD • 8GB Unified Memory • Midnight', 'image_url': '', 'spin_type': 'free', 'original_price': 0.0, 'discount_options': '[]', 'difficulty': 'hard', 'created_at': now},
                {'name': 'PlayStation 5 Slim', 'specs': '1TB Ultra-High Speed SSD • DualSense Bundle', 'image_url': '', 'spin_type': 'sale', 'original_price': 5800.0, 'discount_options': '[2200, 2500, 2900, 3100, 3400, 3700]', 'difficulty': 'random', 'created_at': now}
            ]
            supabase.table('spin_devices').insert(default_devices).execute()

        news_res = supabase.table('news').select('id').limit(1).execute()
        if not news_res.data:
            supabase.table('news').insert({
                'title': 'Welcome to TravestCapital (TC) Institutional Yield Platform',
                'body': 'Earn daily dividends on your share packages, invite VIP 2 partners for an instant milestone bonus, and spin for flagship devices!',
                'created_at': int(time.time())
            }).execute()
    except Exception as e:
        print("Supabase init_db warning (ensure tables exist):", e)


init_db()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = session.get('user_id')
        if not uid:
            return jsonify({'ok': False, 'error': 'Authentication required'}), 401
        res = supabase.table('users').select('*').eq('id', uid).limit(1).execute()
        if not res.data:
            session.clear()
            return jsonify({'ok': False, 'error': 'User not found'}), 401
        user = res.data[0]
        if user.get('is_active', 1) == 0:
            session.clear()
            return jsonify({'ok': False, 'error': 'Your account has been deactivated by Admin.'}), 403
        return f(user, *args, **kwargs)
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
    try:
        data = _parse_json_body()
        name = _safe_str(data.get('name'))
        raw_phone = _safe_str(data.get('phone'))
        raw_password = data.get('password')
        password = raw_password.strip() if isinstance(raw_password, str) else ''
        referral_code = (
            _safe_str(data.get('referral_code'))
            or _safe_str(data.get('ref_code'))
            or _safe_str(data.get('invite_code'))
        ).upper()

        if not name or not raw_phone or not password:
            return jsonify({'ok': False, 'error': 'Full name, phone number, and password are required.'}), 400

        phone = normalize_and_validate_ghana_phone(raw_phone)
        if not phone:
            return jsonify({
                'ok': False,
                'error': 'Invalid phone number. Enter a valid 10-digit Ghana MTN (024, 054, 055, 059) or Telecel (020, 050) number.'
            }), 400

        if len(password) < 4:
            return jsonify({'ok': False, 'error': 'Password must be at least 4 characters.'}), 400

        try:
            existing_res = supabase.table('users').select('id').eq('phone', phone).limit(1).execute()
        except Exception as db_err:
            logger.exception("Supabase phone lookup error during registration for %s: %s", phone, db_err)
            return jsonify({'ok': False, 'error': 'Unable to verify phone number right now. Please try again.'}), 500

        if existing_res and existing_res.data:
            return jsonify({'ok': False, 'error': 'This phone number is already registered.'}), 400

        valid_ref = None
        if referral_code:
            try:
                ref_res = supabase.table('users').select('invite_code').eq('invite_code', referral_code).limit(1).execute()
            except Exception as ref_err:
                logger.exception("Supabase referral lookup error during registration for code %s: %s", referral_code, ref_err)
                return jsonify({'ok': False, 'error': 'Unable to verify referral code right now. Please try again.'}), 500

            if not ref_res or not ref_res.data:
                return jsonify({'ok': False, 'error': 'Invalid referral code. Leave blank if none.'}), 400
            valid_ref = _safe_str(ref_res.data[0].get('invite_code')) or referral_code

        invite_code = generate_invite_code()
        for _ in range(15):
            try:
                dup_code = supabase.table('users').select('id').eq('invite_code', invite_code).limit(1).execute()
                if not (dup_code and dup_code.data):
                    break
            except Exception as code_err:
                logger.warning("Invite code uniqueness check warning: %s", code_err)
                break
            invite_code = generate_invite_code()

        try:
            pw_hash = generate_password_hash(password)
        except Exception as hash_err:
            logger.exception("Password hashing error during registration: %s", hash_err)
            return jsonify({'ok': False, 'error': 'Could not process password securely. Please try again.'}), 400

        now = int(time.time())
        user_data = {
            'name': name,
            'phone': phone,
            'password_hash': pw_hash,
            'invite_code': invite_code,
            'referred_by_code': valid_ref,
            'referral_reward_paid': 0,
            'product_wallet': 0.0,
            'earnings_wallet': 0.0,
            'vip_level_id': 0,
            'vip_level_name': 'Starter',
            'vip_expires_at': 0,
            'last_claim_at': 0,
            'last_claim_date': '',
            'vip2_invites_count': 0,
            'vip2_bonus_claimed': 0,
            'last_free_spin_at': 0,
            'last_free_spin_date': '',
            'is_active': 1,
            'created_at': now
        }

        try:
            insert_res = supabase.table('users').insert(user_data).execute()
        except Exception as insert_err:
            logger.exception("Supabase user insert error during registration for %s: %s", phone, insert_err)
            err_msg = str(insert_err).lower()
            if 'duplicate' in err_msg or 'unique' in err_msg:
                return jsonify({'ok': False, 'error': 'This phone number or account already exists.'}), 400
            return jsonify({'ok': False, 'error': 'Failed to create account due to a database error. Please try again.'}), 500

        if insert_res is None or (hasattr(insert_res, 'data') and insert_res.data is None):
            logger.error("Supabase user insert returned empty/null response for %s", phone)
            return jsonify({'ok': False, 'error': 'Account creation could not be confirmed. Please try again.'}), 500

        return jsonify({'ok': True, 'message': 'Account created successfully! Please sign in.'})
    except Exception as exc:
        logger.exception("Unhandled error in /api/register: %s", exc)
        return jsonify({'ok': False, 'error': 'An unexpected error occurred while creating your account.'}), 500


@app.route('/api/login', methods=['POST'])
def api_login():
    try:
        data = _parse_json_body()
        raw_phone = _safe_str(data.get('phone'))
        raw_password = data.get('password')
        password = raw_password.strip() if isinstance(raw_password, str) else ''

        if not raw_phone or not password:
            return jsonify({'ok': False, 'error': 'Enter your phone number and password.'}), 400

        normalized = normalize_and_validate_ghana_phone(raw_phone)
        res = None
        try:
            if normalized:
                res = supabase.table('users').select('*').eq('phone', normalized).limit(1).execute()
            if not (res and res.data) and raw_phone != normalized:
                res = supabase.table('users').select('*').eq('phone', raw_phone).limit(1).execute()
        except Exception as db_err:
            logger.exception("Supabase lookup error during login for %s: %s", raw_phone, db_err)
            return jsonify({'ok': False, 'error': 'Unable to verify credentials right now. Please try again.'}), 500

        rows = res.data if (res and res.data) else []
        if not rows:
            return jsonify({'ok': False, 'error': 'Invalid phone number or password.'}), 401

        user = rows[0] if isinstance(rows[0], dict) else {}
        pw_hash = _safe_str(user.get('password_hash'))
        if not pw_hash:
            logger.error("User record %s missing password_hash", user.get('id'))
            return jsonify({'ok': False, 'error': 'Account credential data is incomplete. Contact Support.'}), 401

        try:
            password_ok = check_password_hash(pw_hash, password)
        except Exception as pw_err:
            logger.exception("Password hash check error for user %s: %s", user.get('id'), pw_err)
            return jsonify({'ok': False, 'error': 'Invalid phone number or password.'}), 401

        if not password_ok:
            return jsonify({'ok': False, 'error': 'Invalid phone number or password.'}), 401

        is_active = user.get('is_active', 1)
        if is_active == 0 or str(is_active).lower() in ('false', '0'):
            return jsonify({'ok': False, 'error': 'Account deactivated. Contact Support.'}), 403

        user_id = user.get('id')
        if user_id is None:
            logger.error("Authenticated user missing id field")
            return jsonify({'ok': False, 'error': 'Account profile is missing an identifier.'}), 500

        session['user_id'] = user_id
        user_name = _safe_str(user.get('name')) or 'Investor'
        return jsonify({'ok': True, 'user_name': user_name})
    except Exception as exc:
        logger.exception("Unhandled error in /api/login: %s", exc)
        return jsonify({'ok': False, 'error': 'An unexpected error occurred during sign in.'}), 500


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
    today_str = get_calendar_day_str(now)
    sec_to_midnight = seconds_until_midnight(now)

    # Check if VIP expired
    vip_level_id = user['vip_level_id']
    vip_expires_at = user['vip_expires_at']
    if vip_level_id > 0 and vip_expires_at > 0 and now >= vip_expires_at:
        supabase.table('users').update({
            'vip_level_id': 0,
            'vip_level_name': 'Starter (Expired)',
            'vip_expires_at': 0
        }).eq('id', user['id']).execute()
        user = supabase.table('users').select('*').eq('id', user['id']).limit(1).execute().data[0]

    tiers = supabase.table('vip_tiers').select('*').order('price').execute().data or []

    devices = supabase.table('spin_devices').select('*').order('created_at', desc=True).execute().data or []
    for d in devices:
        try:
            if isinstance(d.get('discount_options'), str):
                d['discount_options'] = json.loads(d['discount_options'])
        except Exception:
            d['discount_options'] = []

    news = supabase.table('news').select('*').order('created_at', desc=True).limit(10).execute().data or []
    txs = supabase.table('transactions').select('*').eq('user_id', user['id']).order('created_at', desc=True).limit(25).execute().data or []
    spin_orders_docs = supabase.table('spin_orders').select('*').eq('user_id', user['id']).order('created_at', desc=True).execute().data or []

    # Calendar-day reset (00:00 Midnight) for Daily Claim
    can_claim_daily = False
    next_claim_in = 0
    active_tier = None
    if user['vip_level_id'] > 0 and user['vip_expires_at'] > now:
        for t in tiers:
            if t['id'] == user['vip_level_id']:
                active_tier = t
                break
        if active_tier:
            last_claim_day = user.get('last_claim_date') or (
                get_calendar_day_str(user['last_claim_at']) if user.get('last_claim_at') else ''
            )
            if last_claim_day != today_str:
                can_claim_daily = True
            else:
                next_claim_in = sec_to_midnight

    # Calendar-day reset (00:00 Midnight) for Free Spin
    last_spin_day = user.get('last_free_spin_date') or (
        get_calendar_day_str(user['last_free_spin_at']) if user.get('last_free_spin_at') else ''
    )
    can_free_spin_today = (last_spin_day != today_str)
    free_spin_wait = 0 if can_free_spin_today else sec_to_midnight

    has_vip2 = False
    if active_tier and (active_tier.get('is_vip2_target') == 1 or active_tier.get('price', 0) >= 600):
        has_vip2 = True

    min_withdrawal = float(get_setting('min_withdrawal', '500'))
    paystack_pk = get_setting('paystack_public_key', PAYSTACK_PUBLIC_KEY)
    invite_target_count = int(float(get_setting('invite_target_count', '10')))
    invite_reward_amount = float(get_setting('invite_reward_amount', '2000'))

    about_company = {
        'text': get_setting('about_company_text', ''),
        'image_url': get_setting('about_company_image', ''),
        'video_url': get_setting('about_company_video', '')
    }

    return jsonify({
        'ok': True,
        'server_time': now,
        'calendar_day': today_str,
        'seconds_until_midnight': sec_to_midnight,
        'paystack_public_key': paystack_pk,
        'min_withdrawal': min_withdrawal,
        'invite_target_count': invite_target_count,
        'invite_reward_amount': invite_reward_amount,
        'about_company': about_company,
        'user': {
            'id': user['id'],
            'name': user['name'],
            'phone': user['phone'],
            'invite_code': user['invite_code'],
            'product_wallet': round(user.get('product_wallet', 0.0), 2),
            'earnings_wallet': round(user.get('earnings_wallet', 0.0), 2),
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

    dup = supabase.table('transactions').select('id').eq('reference', reference).limit(1).execute()
    if dup.data:
        return jsonify({'ok': False, 'error': 'Transaction already processed.'}), 400

    now = int(time.time())
    user_doc = supabase.table('users').select('*').eq('id', user['id']).limit(1).execute().data[0]
    new_prod_wallet = user_doc.get('product_wallet', 0.0) + amount
    supabase.table('users').update({'product_wallet': new_prod_wallet}).eq('id', user['id']).execute()

    supabase.table('transactions').insert({
        'user_id': user['id'],
        'user_name': user['name'],
        'user_phone': user['phone'],
        'tx_type': 'deposit',
        'amount': amount,
        'reference': reference,
        'status': 'completed',
        'details': 'Paystack Product Wallet Deposit',
        'created_at': now
    }).execute()
    return jsonify({'ok': True, 'message': f'GHS {amount:,.2f} credited to your Product Wallet!'})


def process_share_activation(user_row, tier_row, pay_source, reference=''):
    now = int(time.time())
    expires_at = now + (tier_row['duration_days'] * 86400)

    supabase.table('users').update({
        'vip_level_id': tier_row['id'],
        'vip_level_name': tier_row['name'],
        'vip_expires_at': expires_at,
        'last_claim_at': 0,
        'last_claim_date': ''
    }).eq('id', user_row['id']).execute()

    supabase.table('transactions').insert({
        'user_id': user_row['id'],
        'user_name': user_row['name'],
        'user_phone': user_row['phone'],
        'tx_type': 'share_buy',
        'amount': tier_row['price'],
        'reference': reference,
        'status': 'completed',
        'details': f"Purchased {tier_row['name']} ({pay_source})",
        'created_at': now
    }).execute()

    if user_row.get('referred_by_code') and user_row.get('referral_reward_paid', 0) == 0:
        ref_res = supabase.table('users').select('*').eq('invite_code', user_row['referred_by_code']).limit(1).execute()
        if ref_res.data:
            referrer = ref_res.data[0]
            commission = round(tier_row['price'] * 0.10, 2)
            supabase.table('users').update({
                'earnings_wallet': referrer.get('earnings_wallet', 0.0) + commission
            }).eq('id', referrer['id']).execute()
            supabase.table('users').update({'referral_reward_paid': 1}).eq('id', user_row['id']).execute()

            supabase.table('transactions').insert({
                'user_id': referrer['id'],
                'user_name': referrer['name'],
                'user_phone': referrer['phone'],
                'tx_type': 'commission',
                'amount': commission,
                'reference': reference,
                'status': 'completed',
                'details': f"10% Referral Bonus from {user_row['name']}",
                'created_at': now
            }).execute()


@app.route('/api/user/buy-share', methods=['POST'])
@login_required
def api_buy_share(user):
    data = request.get_json(force=True)
    tier_id = int(data.get('tier_id') or 0)
    method = data.get('method')
    reference = (data.get('reference') or '').strip()
    vip2_recruit_code = (data.get('vip2_recruit_code') or '').strip().upper()

    tier_res = supabase.table('vip_tiers').select('*').eq('id', tier_id).limit(1).execute()
    if not tier_res.data:
        return jsonify({'ok': False, 'error': 'Selected VIP tier does not exist.'}), 404

    tier = tier_res.data[0]
    price = float(tier['price'])

    recruiter = None
    if vip2_recruit_code:
        if tier.get('is_vip2_target') != 1 and price < 600:
            return jsonify({'ok': False, 'error': 'Get Hired recruitment codes only apply to VIP 2 shares.'}), 400
        rec_res = supabase.table('users').select('*').eq('invite_code', vip2_recruit_code).limit(1).execute()
        if not rec_res.data:
            return jsonify({'ok': False, 'error': 'Invalid Get Hired unique invitation code.'}), 400
        recruiter = rec_res.data[0]
        if recruiter['id'] == user['id']:
            return jsonify({'ok': False, 'error': 'You cannot use your own Get Hired code.'}), 400

    current_user = supabase.table('users').select('*').eq('id', user['id']).limit(1).execute().data[0]

    if method == 'wallet':
        if current_user.get('product_wallet', 0.0) < price:
            return jsonify({'ok': False, 'error': f'Insufficient Product Wallet balance (GHS {current_user.get("product_wallet", 0.0):,.2f}). Deposit or pay directly via Paystack.'}), 400
        supabase.table('users').update({'product_wallet': current_user.get('product_wallet', 0.0) - price}).eq('id', user['id']).execute()
        ref_code = 'WAL-' + uuid.uuid4().hex[:10].upper()
        process_share_activation(current_user, tier, 'Product Wallet', ref_code)
    elif method == 'paystack':
        if not reference:
            return jsonify({'ok': False, 'error': 'Missing Paystack payment reference.'}), 400
        dup = supabase.table('transactions').select('id').eq('reference', reference).limit(1).execute()
        if dup.data:
            return jsonify({'ok': False, 'error': 'Payment reference already used.'}), 400
        process_share_activation(current_user, tier, 'Paystack Direct', reference)
    else:
        return jsonify({'ok': False, 'error': 'Invalid payment method.'}), 400

    if recruiter:
        supabase.table('users').update({'vip2_invites_count': recruiter.get('vip2_invites_count', 0) + 1}).eq('id', recruiter['id']).execute()

    return jsonify({
        'ok': True,
        'message': f'{tier["name"]} activated! Click "Claim Daily Profit" now to collect your first GHS {tier["daily_profit"]:,.2f}.'
    })


@app.route('/api/user/claim-daily', methods=['POST'])
@login_required
def api_claim_daily(user):
    now = int(time.time())
    today_str = get_calendar_day_str(now)

    if user['vip_level_id'] <= 0 or user['vip_expires_at'] <= now:
        return jsonify({'ok': False, 'error': 'No active VIP share package. Please purchase shares first.'}), 400

    tier_res = supabase.table('vip_tiers').select('*').eq('id', user['vip_level_id']).limit(1).execute()
    if not tier_res.data:
        return jsonify({'ok': False, 'error': 'VIP tier not found.'}), 404

    tier = tier_res.data[0]
    last_claim_day = user.get('last_claim_date') or (
        get_calendar_day_str(user['last_claim_at']) if user.get('last_claim_at') else ''
    )
    if last_claim_day == today_str:
        rem = seconds_until_midnight(now)
        hrs = rem // 3600
        mins = (rem % 3600) // 60
        return jsonify({'ok': False, 'error': f'Already claimed today. Next claim resets at 00:00 midnight (in {hrs}h {mins}m).'}), 400

    profit = float(tier['daily_profit'])
    supabase.table('users').update({
        'earnings_wallet': user.get('earnings_wallet', 0.0) + profit,
        'last_claim_at': now,
        'last_claim_date': today_str
    }).eq('id', user['id']).execute()

    supabase.table('transactions').insert({
        'user_id': user['id'],
        'user_name': user['name'],
        'user_phone': user['phone'],
        'tx_type': 'daily_claim',
        'amount': profit,
        'reference': 'CLM-' + uuid.uuid4().hex[:8].upper(),
        'status': 'completed',
        'details': f"{tier['name']} Daily Dividend Claim ({today_str})",
        'created_at': now
    }).execute()
    return jsonify({'ok': True, 'message': f'GHS {profit:,.2f} daily profit added to your Earnings Wallet!'})


@app.route('/api/user/claim-vip2-bonus', methods=['POST'])
@login_required
def api_claim_vip2_bonus(user):
    target_count = int(float(get_setting('invite_target_count', '10')))
    reward_amount = float(get_setting('invite_reward_amount', '2000'))

    if user.get('vip2_invites_count', 0) < target_count:
        return jsonify({'ok': False, 'error': f'You need {target_count} VIP 2 recruits (currently {user.get("vip2_invites_count", 0)}/{target_count}).'}), 400
    if user.get('vip2_bonus_claimed', 0) == 1:
        return jsonify({'ok': False, 'error': f'You have already claimed your GHS {reward_amount:,.2f} recruitment milestone bonus.'}), 400

    now = int(time.time())
    supabase.table('users').update({
        'earnings_wallet': user.get('earnings_wallet', 0.0) + reward_amount,
        'vip2_bonus_claimed': 1
    }).eq('id', user['id']).execute()

    supabase.table('transactions').insert({
        'user_id': user['id'],
        'user_name': user['name'],
        'user_phone': user['phone'],
        'tx_type': 'vip2_bonus',
        'amount': reward_amount,
        'reference': 'HIRE-' + uuid.uuid4().hex[:8].upper(),
        'status': 'completed',
        'details': f'Get Hired {target_count} VIP2 Milestone Reward',
        'created_at': now
    }).execute()
    return jsonify({'ok': True, 'message': f'Congratulations! GHS {reward_amount:,.2f} added to your Earnings Wallet!'})


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
    if user.get('earnings_wallet', 0.0) < amount:
        return jsonify({'ok': False, 'error': 'Insufficient Earnings Wallet balance. Product Wallet cannot be withdrawn directly.'}), 400

    now = int(time.time())
    ref = 'WDR-' + uuid.uuid4().hex[:10].upper()

    supabase.table('users').update({'earnings_wallet': user.get('earnings_wallet', 0.0) - amount}).eq('id', user['id']).execute()

    supabase.table('transactions').insert({
        'user_id': user['id'],
        'user_name': user['name'],
        'user_phone': user['phone'],
        'tx_type': 'withdrawal',
        'amount': amount,
        'reference': ref,
        'status': 'Pending Payout',
        'details': f"{momo_network} • {momo_number} ({account_name})",
        'created_at': now
    }).execute()

    return jsonify({'ok': True, 'message': f'Withdrawal request of GHS {amount:,.2f} submitted! Your Mobile Money payout is being processed.'})


# ==============================================================================
# SPIN WHEEL API
# ==============================================================================
@app.route('/api/user/spin', methods=['POST'])
@login_required
def api_user_spin(user):
    data = request.get_json(force=True)
    device_id = int(data.get('device_id') or 0)
    now = int(time.time())
    today_str = get_calendar_day_str(now)

    dev_res = supabase.table('spin_devices').select('*').eq('id', device_id).limit(1).execute()
    if not dev_res.data:
        return jsonify({'ok': False, 'error': 'Device not found.'}), 404

    device = dev_res.data[0]

    if device['spin_type'] == 'free':
        tier_id = user['vip_level_id']
        tier = None
        if tier_id > 0:
            t_res = supabase.table('vip_tiers').select('*').eq('id', tier_id).limit(1).execute()
            if t_res.data:
                tier = t_res.data[0]
        is_vip2 = tier and (tier.get('is_vip2_target') == 1 or tier.get('price', 0) >= 600) and user['vip_expires_at'] > now
        if not is_vip2:
            return jsonify({'ok': False, 'error': 'Free Device Spin is strictly reserved for active VIP 2 shareholders!'}), 403

        last_spin_day = user.get('last_free_spin_date') or (
            get_calendar_day_str(user['last_free_spin_at']) if user.get('last_free_spin_at') else ''
        )
        if last_spin_day == today_str:
            rem = seconds_until_midnight(now)
            hrs = rem // 3600
            mins = (rem % 3600) // 60
            return jsonify({'ok': False, 'error': f'You have already used today\'s free spin. Resets at 00:00 midnight (in {hrs}h {mins}m).'}), 400

        if device.get('difficulty') == 'hard':
            is_win = random.random() < 0.05
        else:
            is_win = random.choice([True, False])

        segment_index = random.choice([0, 2, 4]) if is_win else random.choice([1, 3, 5])
        supabase.table('users').update({
            'last_free_spin_at': now,
            'last_free_spin_date': today_str
        }).eq('id', user['id']).execute()
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

    dev_res = supabase.table('spin_devices').select('*').eq('id', device_id).limit(1).execute()
    if not dev_res.data:
        return jsonify({'ok': False, 'error': 'Device not found.'}), 404

    device = dev_res.data[0]

    if spin_type == 'sale':
        if amount_paid <= 0 or not paystack_ref:
            return jsonify({'ok': False, 'error': 'Paystack payment confirmation is required before submitting delivery details.'}), 400
        dup = supabase.table('spin_orders').select('id').eq('paystack_ref', paystack_ref).limit(1).execute()
        if dup.data:
            return jsonify({'ok': False, 'error': 'This Paystack payment reference has already been submitted.'}), 400

    now = int(time.time())
    supabase.table('spin_orders').insert({
        'user_id': user['id'],
        'user_name': user['name'],
        'device_id': device['id'],
        'device_name': device['name'],
        'spin_type': spin_type,
        'amount_paid': amount_paid,
        'paystack_ref': paystack_ref,
        'recipient_phone': recipient_phone,
        'region': region,
        'address': address,
        'status': 'Processing Delivery',
        'created_at': now
    }).execute()

    if spin_type == 'sale':
        supabase.table('transactions').insert({
            'user_id': user['id'],
            'user_name': user['name'],
            'user_phone': user['phone'],
            'tx_type': 'deposit',
            'amount': amount_paid,
            'reference': paystack_ref,
            'status': 'completed',
            'details': f"Discount Spin Purchase: {device['name']}",
            'created_at': now
        }).execute()

    return jsonify({'ok': True, 'message': 'Delivery details submitted! Admin will dispatch your device shortly.'})


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

    supabase.table('users').update({
        'password_hash': generate_password_hash(new_pw)
    }).eq('id', user['id']).execute()
    return jsonify({'ok': True, 'message': 'Password updated successfully!'})


@app.route('/api/user/change-phone', methods=['POST'])
@login_required
def api_user_change_phone(user):
    try:
        data = _parse_json_body()
        raw_new_phone = _safe_str(data.get('new_phone'))
        raw_password = data.get('password')
        password = raw_password.strip() if isinstance(raw_password, str) else ''

        new_phone = normalize_and_validate_ghana_phone(raw_new_phone)
        if not new_phone:
            return jsonify({
                'ok': False,
                'error': 'Invalid phone number. Enter a valid 10-digit Ghana MTN (024, 054, 055, 059) or Telecel (020, 050) number.'
            }), 400
        if not password or not check_password_hash(_safe_str(user.get('password_hash')), password):
            return jsonify({'ok': False, 'error': 'Password verification failed.'}), 400

        existing_docs = supabase.table('users').select('id').eq('phone', new_phone).execute().data or []
        if any(d.get('id') != user.get('id') for d in existing_docs):
            return jsonify({'ok': False, 'error': 'Phone number already in use by another account.'}), 400

        supabase.table('users').update({'phone': new_phone}).eq('id', user['id']).execute()
        return jsonify({'ok': True, 'message': 'Phone number updated successfully!'})
    except Exception as exc:
        logger.exception("Error in /api/user/change-phone: %s", exc)
        return jsonify({'ok': False, 'error': 'Could not update phone number right now.'}), 500


# ==============================================================================
# REAL-TIME LIVE CHAT API
# ==============================================================================
@app.route('/api/chat/messages', methods=['GET'])
@login_required
def api_user_chat_messages(user):
    after_id = int(request.args.get('after_id') or 0)
    msg_docs = supabase.table('chat_messages').select('*').eq('user_id', user['id']).gt('id', after_id).order('id').execute().data or []

    unread_admin_ids = [m['id'] for m in msg_docs if m.get('sender') == 'admin' and m.get('is_read', 0) == 0]
    if unread_admin_ids:
        supabase.table('chat_messages').update({'is_read': 1}).in_('id', unread_admin_ids).execute()

    return jsonify({'ok': True, 'messages': msg_docs})


@app.route('/api/chat/send', methods=['POST'])
@login_required
def api_user_chat_send(user):
    data = request.get_json(force=True)
    msg = (data.get('message') or '').strip()
    if not msg:
        return jsonify({'ok': False, 'error': 'Message cannot be empty.'}), 400

    now = int(time.time())
    res = supabase.table('chat_messages').insert({
        'user_id': user['id'],
        'sender': 'user',
        'message': msg,
        'is_read': 0,
        'created_at': now
    }).execute()
    msg_obj = res.data[0] if res.data else {}
    return jsonify({'ok': True, 'message': msg_obj})


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
    users = supabase.table('users').select('*').order('id', desc=True).execute().data or []
    tiers = supabase.table('vip_tiers').select('*').order('price').execute().data or []

    devices = supabase.table('spin_devices').select('*').order('created_at', desc=True).execute().data or []
    for d in devices:
        try:
            if isinstance(d.get('discount_options'), str):
                d['discount_options'] = json.loads(d['discount_options'])
        except Exception:
            d['discount_options'] = []

    spin_orders = supabase.table('spin_orders').select('*').order('created_at', desc=True).execute().data or []
    news = supabase.table('news').select('*').order('created_at', desc=True).execute().data or []
    transactions = supabase.table('transactions').select('*').order('created_at', desc=True).execute().data or []
    limited_txs = transactions[:100]

    now_dt = datetime.now(tz=GHANA_TZ)
    start_of_today = int(now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    paid_today = sum(t.get('amount', 0) for t in transactions if t.get('tx_type') in ('deposit', 'share_buy') and t.get('created_at', 0) >= start_of_today)
    total_paid = sum(t.get('amount', 0) for t in transactions if t.get('tx_type') in ('deposit', 'share_buy'))
    total_withdrawn = sum(t.get('amount', 0) for t in transactions if t.get('tx_type') == 'withdrawal')

    all_messages = supabase.table('chat_messages').select('*').order('id').execute().data or []
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
    invite_target_count = int(float(get_setting('invite_target_count', '10')))
    invite_reward_amount = float(get_setting('invite_reward_amount', '2000'))

    about_company = {
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
            'invite_target_count': invite_target_count,
            'invite_reward_amount': invite_reward_amount,
            'about_company_text': about_company['text'],
            'about_company_image': about_company['image_url'],
            'about_company_video': about_company['video_url']
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
    res = supabase.table('users').select('is_active').eq('id', uid).limit(1).execute()
    if not res.data:
        return jsonify({'ok': False, 'error': 'User not found.'}), 404
    u = res.data[0]
    new_status = 0 if u.get('is_active', 1) == 1 else 1
    supabase.table('users').update({'is_active': new_status}).eq('id', uid).execute()
    return jsonify({'ok': True, 'is_active': new_status})


@app.route('/api/admin/user/adjust-wallet', methods=['POST'])
@admin_required
def api_admin_user_adjust_wallet():
    data = request.get_json(force=True)
    uid = int(data.get('user_id') or 0)
    product_wallet = float(data.get('product_wallet') or 0)
    earnings_wallet = float(data.get('earnings_wallet') or 0)
    vip_level_id = int(data.get('vip_level_id') or 0)

    if vip_level_id > 0:
        t_res = supabase.table('vip_tiers').select('*').eq('id', vip_level_id).limit(1).execute()
        t = t_res.data[0] if t_res.data else None
        vip_name = t['name'] if t else 'Starter'
        expires = int(time.time()) + ((t['duration_days'] if t else 15) * 86400)
    else:
        vip_name = 'Starter'
        expires = 0

    supabase.table('users').update({
        'product_wallet': product_wallet,
        'earnings_wallet': earnings_wallet,
        'vip_level_id': vip_level_id,
        'vip_level_name': vip_name,
        'vip_expires_at': expires
    }).eq('id', uid).execute()
    return jsonify({'ok': True, 'message': 'User account balances and VIP status updated.'})


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

    if tid:
        supabase.table('vip_tiers').update({
            'name': name,
            'price': price,
            'daily_profit': daily_profit,
            'duration_days': duration_days,
            'is_vip2_target': is_vip2_target
        }).eq('id', int(tid)).execute()
    else:
        supabase.table('vip_tiers').insert({
            'name': name,
            'price': price,
            'daily_profit': daily_profit,
            'duration_days': duration_days,
            'is_vip2_target': is_vip2_target
        }).execute()
    return jsonify({'ok': True, 'message': 'VIP Share Tier saved successfully.'})


@app.route('/api/admin/vip/delete', methods=['POST'])
@admin_required
def api_admin_vip_delete():
    data = request.get_json(force=True)
    tid = int(data.get('id') or 0)
    supabase.table('vip_tiers').delete().eq('id', tid).execute()
    return jsonify({'ok': True})


@app.route('/api/admin/vip/reset-defaults', methods=['POST'])
@admin_required
def api_admin_vip_reset():
    supabase.table('vip_tiers').delete().neq('id', 0).execute()

    default_tiers = [
        {'name': 'VIP 1', 'price': 300.0, 'daily_profit': 50.0, 'duration_days': 15, 'is_vip2_target': 0},
        {'name': 'VIP 2', 'price': 600.0, 'daily_profit': 110.0, 'duration_days': 15, 'is_vip2_target': 1},
        {'name': 'VIP 3', 'price': 1200.0, 'daily_profit': 230.0, 'duration_days': 15, 'is_vip2_target': 0},
        {'name': 'VIP 4', 'price': 2500.0, 'daily_profit': 500.0, 'duration_days': 15, 'is_vip2_target': 0},
    ]
    supabase.table('vip_tiers').insert(default_tiers).execute()
    return jsonify({'ok': True, 'message': 'VIP tiers reset to default amounts.'})


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

    supabase.table('spin_devices').insert({
        'name': name,
        'specs': specs,
        'image_url': image_url,
        'spin_type': spin_type,
        'original_price': original_price,
        'discount_options': json.dumps(discount_list),
        'difficulty': difficulty,
        'created_at': int(time.time())
    }).execute()
    return jsonify({'ok': True, 'message': f'{name} added to Spin Vault!'})


@app.route('/api/admin/devices/update', methods=['POST'])
@admin_required
def api_admin_device_update():
    data = request.get_json(force=True)
    did = int(data.get('id') or 0)
    spin_type = data.get('spin_type')
    difficulty = data.get('difficulty')
    original_price = float(data.get('original_price') or 0)
    discounts = data.get('discount_options') or []

    supabase.table('spin_devices').update({
        'spin_type': spin_type,
        'difficulty': difficulty,
        'original_price': original_price,
        'discount_options': json.dumps(discounts)
    }).eq('id', did).execute()
    return jsonify({'ok': True, 'message': 'Spin device parameters updated.'})


@app.route('/api/admin/devices/delete', methods=['POST'])
@admin_required
def api_admin_device_delete():
    data = request.get_json(force=True)
    did = int(data.get('id') or 0)
    supabase.table('spin_devices').delete().eq('id', did).execute()
    return jsonify({'ok': True})


@app.route('/api/admin/orders/status', methods=['POST'])
@admin_required
def api_admin_order_status():
    data = request.get_json(force=True)
    oid = int(data.get('id') or 0)
    status = (data.get('status') or 'Delivered').strip()
    supabase.table('spin_orders').update({'status': status}).eq('id', oid).execute()
    return jsonify({'ok': True})


@app.route('/api/admin/withdrawals/update', methods=['POST'])
@admin_required
def api_admin_withdrawal_update():
    data = request.get_json(force=True)
    tx_id = int(data.get('id') or 0)
    action = (data.get('action') or '').strip()

    tx_res = supabase.table('transactions').select('*').eq('id', tx_id).limit(1).execute()
    if not tx_res.data:
        return jsonify({'ok': False, 'error': 'Withdrawal record not found.'}), 404

    tx = tx_res.data[0]
    if tx.get('tx_type') != 'withdrawal':
        return jsonify({'ok': False, 'error': 'Not a withdrawal transaction.'}), 400

    if action == 'paid':
        supabase.table('transactions').update({'status': 'Paid'}).eq('id', tx_id).execute()
        return jsonify({'ok': True, 'message': 'Withdrawal marked as Paid!'})
    elif action == 'refund':
        if tx.get('status') == 'Refunded':
            return jsonify({'ok': False, 'error': 'Already refunded.'}), 400
        u_res = supabase.table('users').select('earnings_wallet').eq('id', tx['user_id']).limit(1).execute()
        if u_res.data:
            supabase.table('users').update({
                'earnings_wallet': u_res.data[0].get('earnings_wallet', 0.0) + tx['amount']
            }).eq('id', tx['user_id']).execute()
        supabase.table('transactions').update({'status': 'Refunded'}).eq('id', tx_id).execute()
        return jsonify({'ok': True, 'message': f'GHS {tx["amount"]:,.2f} refunded back to user Earnings Wallet.'})

    return jsonify({'ok': False, 'error': 'Invalid action.'}), 400


@app.route('/api/admin/news/add', methods=['POST'])
@admin_required
def api_admin_news_add():
    data = request.get_json(force=True)
    title = (data.get('title') or '').strip()
    body = (data.get('body') or '').strip()
    if not title or not body:
        return jsonify({'ok': False, 'error': 'Title and message are required.'}), 400
    supabase.table('news').insert({
        'title': title,
        'body': body,
        'created_at': int(time.time())
    }).execute()
    return jsonify({'ok': True, 'message': 'Announcement published to all users!'})


@app.route('/api/admin/news/delete', methods=['POST'])
@admin_required
def api_admin_news_delete():
    data = request.get_json(force=True)
    nid = int(data.get('id') or 0)
    supabase.table('news').delete().eq('id', nid).execute()
    return jsonify({'ok': True})


@app.route('/api/admin/settings/save', methods=['POST'])
@admin_required
def api_admin_settings_save():
    data = request.get_json(force=True)
    if 'min_withdrawal' in data and str(data['min_withdrawal']).strip():
        set_setting('min_withdrawal', str(float(data['min_withdrawal'])))
    if 'paystack_public_key' in data and str(data['paystack_public_key']).strip():
        set_setting('paystack_public_key', str(data['paystack_public_key']).strip())
    if 'paystack_secret_key' in data and str(data['paystack_secret_key']).strip():
        set_setting('paystack_secret_key', str(data['paystack_secret_key']).strip())
    if 'invite_target_count' in data and str(data['invite_target_count']).strip():
        target_val = int(float(data['invite_target_count']))
        if target_val < 1:
            return jsonify({'ok': False, 'error': 'Required invited users must be at least 1.'}), 400
        set_setting('invite_target_count', str(target_val))
    if 'invite_reward_amount' in data and str(data['invite_reward_amount']).strip():
        reward_val = float(data['invite_reward_amount'])
        if reward_val < 0:
            return jsonify({'ok': False, 'error': 'Reward amount cannot be negative.'}), 400
        set_setting('invite_reward_amount', str(reward_val))
    if 'about_company_text' in data:
        set_setting('about_company_text', str(data['about_company_text'] or '').strip())
    if 'about_company_image' in data:
        set_setting('about_company_image', str(data['about_company_image'] or '').strip())
    if 'about_company_video' in data:
        set_setting('about_company_video', str(data['about_company_video'] or '').strip())
    if 'new_admin_pin' in data and str(data['new_admin_pin']).strip():
        pin = str(data['new_admin_pin']).strip()
        if len(pin) != 6 or not pin.isdigit():
            return jsonify({'ok': False, 'error': 'Admin PIN must be exactly 6 digits.'}), 400
        set_setting('admin_pin', pin)
    return jsonify({'ok': True, 'message': 'Platform configuration saved!'})


@app.route('/api/admin/about/upload-media', methods=['POST'])
@admin_required
def api_admin_about_upload_media():
    if 'media' not in request.files:
        return jsonify({'ok': False, 'error': 'No media file provided.'}), 400
    f = request.files['media']
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': 'Empty file selected.'}), 400
    ext = os.path.splitext(secure_filename(f.filename))[1].lower()
    if ext not in ['.png', '.jpg', '.jpeg', '.webp', '.gif', '.mp4', '.webm', '.mov']:
        return jsonify({'ok': False, 'error': 'Unsupported media format. Use PNG, JPG, WEBP, GIF, MP4, or WEBM.'}), 400
    fname = f"about_{uuid.uuid4().hex[:12]}{ext}"
    f.save(os.path.join(UPLOAD_FOLDER, fname))
    url = f"/static/uploads/{fname}"
    media_kind = 'video' if ext in ['.mp4', '.webm', '.mov'] else 'image'
    return jsonify({'ok': True, 'url': url, 'media_kind': media_kind})


@app.route('/api/admin/chat/messages', methods=['GET'])
@admin_required
def api_admin_chat_messages():
    uid = int(request.args.get('user_id') or 0)
    after_id = int(request.args.get('after_id') or 0)
    msg_docs = supabase.table('chat_messages').select('*').eq('user_id', uid).gt('id', after_id).order('id').execute().data or []

    unread_user_ids = [m['id'] for m in msg_docs if m.get('sender') == 'user' and m.get('is_read', 0) == 0]
    if unread_user_ids:
        supabase.table('chat_messages').update({'is_read': 1}).in_('id', unread_user_ids).execute()

    return jsonify({'ok': True, 'messages': msg_docs})


@app.route('/api/admin/chat/send', methods=['POST'])
@admin_required
def api_admin_chat_send():
    data = request.get_json(force=True)
    uid = int(data.get('user_id') or 0)
    msg = (data.get('message') or '').strip()
    if not uid or not msg:
        return jsonify({'ok': False, 'error': 'Select a user and enter a reply.'}), 400

    now = int(time.time())
    res = supabase.table('chat_messages').insert({
        'user_id': uid,
        'sender': 'admin',
        'message': msg,
        'is_read': 0,
        'created_at': now
    }).execute()
    msg_obj = res.data[0] if res.data else {}
    return jsonify({'ok': True, 'message': msg_obj})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
