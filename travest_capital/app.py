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
from supabase import create_client, Client

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
# SUPABASE INITIALIZATION
# ==============================================================================
SUPABASE_URL = os.environ.get('SUPABASE_URL', '').strip()
SUPABASE_KEY = (
    os.environ.get('SUPABASE_KEY')
    or os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    or os.environ.get('SUPABASE_ANON_KEY')
    or ''
).strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY environment variables must be set.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ==============================================================================
# PAYSTACK CONFIGURATION
# ==============================================================================
PAYSTACK_PUBLIC_KEY = os.environ.get('PAYSTACK_PUBLIC_KEY', 'pk_test_your_paystack_public_key_here')
PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY', 'sk_test_your_paystack_secret_key_here')


def get_next_sequence_id(counter_name):
    res = supabase.table('counters').select('name, val').eq('name', counter_name).limit(1).execute()
    rows = res.data or []
    if not rows:
        val = 1
        supabase.table('counters').insert({'name': counter_name, 'val': val}).execute()
        return val
    val = int(rows[0].get('val', 0)) + 1
    supabase.table('counters').update({'val': val}).eq('name', counter_name).execute()
    return val


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
        existing = supabase.table('settings').select('key').eq('key', k).limit(1).execute().data or []
        if not existing:
            supabase.table('settings').insert({'key': k, 'value': v}).execute()

    tiers_ref = supabase.table('vip_tiers').select('id').limit(1).execute().data or []
    if not tiers_ref:
        default_tiers = [
            (1, 'VIP 1', 300.0, 50.0, 15, 0),
            (2, 'VIP 2', 600.0, 110.0, 15, 1),
            (3, 'VIP 3', 1200.0, 230.0, 15, 0),
            (4, 'VIP 4', 2500.0, 500.0, 15, 0),
        ]
        for tid, name, price, profit, days, vip2 in default_tiers:
            supabase.table('vip_tiers').upsert({
                'id': tid,
                'name': name,
                'price': price,
                'daily_profit': profit,
                'duration_days': days,
                'is_vip2_target': vip2
            }).execute()
        supabase.table('counters').upsert({'name': 'vip_tiers', 'val': 4}).execute()

    devs_ref = supabase.table('spin_devices').select('id').limit(1).execute().data or []
    if not devs_ref:
        now = int(time.time())
        default_devices = [
            (1, 'iPhone XR', '128GB Storage • 4GB RAM • Liquid Retina HD', '', 'free', 0.0, '[]', 'random', now),
            (2, 'Samsung Galaxy S23 Ultra', '256GB Storage • 12GB RAM • 200MP Camera', '', 'sale', 6500.0, '[2800, 3200, 3500, 3900, 4100, 4500]', 'random', now),
            (3, 'Apple MacBook Air M2', '512GB SSD • 8GB Unified Memory • Midnight', '', 'free', 0.0, '[]', 'hard', now),
            (4, 'PlayStation 5 Slim', '1TB Ultra-High Speed SSD • DualSense Bundle', '', 'sale', 5800.0, '[2200, 2500, 2900, 3100, 3400, 3700]', 'random', now)
        ]
        for did, name, specs, img, stype, orig, disc, diff, created in default_devices:
            supabase.table('spin_devices').upsert({
                'id': did,
                'name': name,
                'specs': specs,
                'image_url': img,
                'spin_type': stype,
                'original_price': orig,
                'discount_options': disc,
                'difficulty': diff,
                'created_at': created
            }).execute()
        supabase.table('counters').upsert({'name': 'spin_devices', 'val': 4}).execute()

    news_ref = supabase.table('news').select('id').limit(1).execute().data or []
    if not news_ref:
        nid = 1
        supabase.table('news').upsert({
            'id': nid,
            'title': 'Welcome to TravestCapital (TC) Institutional Yield Platform',
            'body': 'Earn daily dividends on your share packages, invite 10 VIP 2 partners for an instant GHS 2,000 bonus, and spin for flagship devices!',
            'created_at': int(time.time())
        }).execute()
        supabase.table('counters').upsert({'name': 'news', 'val': 1}).execute()


init_db()


def get_setting(key, default=''):
    rows = supabase.table('settings').select('value').eq('key', key).limit(1).execute().data or []
    if rows:
        return rows[0].get('value', default)
    return default


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = session.get('user_id')
        if not uid:
            return jsonify({'ok': False, 'error': 'Authentication required'}), 401
        rows = supabase.table('users').select('*').eq('id', int(uid)).limit(1).execute().data or []
        if not rows:
            session.clear()
            return jsonify({'ok': False, 'error': 'User not found'}), 401
        user = rows[0]
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

    existing_docs = supabase.table('users').select('id').eq('phone', phone).limit(1).execute().data or []
    if existing_docs:
        return jsonify({'ok': False, 'error': 'This phone number is already registered.'}), 400

    valid_ref = None
    if referral_code:
        ref_docs = supabase.table('users').select('invite_code').eq('invite_code', referral_code).limit(1).execute().data or []
        if not ref_docs:
            return jsonify({'ok': False, 'error': 'Invalid referral code. Leave blank if none.'}), 400
        valid_ref = ref_docs[0]['invite_code']

    invite_code = generate_invite_code()
    while supabase.table('users').select('id').eq('invite_code', invite_code).limit(1).execute().data:
        invite_code = generate_invite_code()

    uid = get_next_sequence_id('users')
    pw_hash = generate_password_hash(password)
    now = int(time.time())

    user_data = {
        'id': uid,
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
        'last_claim_day': '',
        'vip2_invites_count': 0,
        'vip2_bonus_claimed': 0,
        'last_free_spin_at': 0,
        'last_free_spin_day': '',
        'is_active': 1,
        'created_at': now
    }
    supabase.table('users').insert(user_data).execute()
    return jsonify({'ok': True, 'message': 'Account created successfully! Please sign in.'})


@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json(force=True)
    raw_phone = (data.get('phone') or '').strip()
    phone = normalize_ghana_phone(raw_phone) or raw_phone
    password = (data.get('password') or '').strip()

    if not phone or not password:
        return jsonify({'ok': False, 'error': 'Enter your phone number and password.'}), 400

    docs = supabase.table('users').select('*').eq('phone', phone).limit(1).execute().data or []
    if not docs and raw_phone != phone:
        docs = supabase.table('users').select('*').eq('phone', raw_phone).limit(1).execute().data or []
    if not docs:
        return jsonify({'ok': False, 'error': 'Invalid phone number or password.'}), 401

    user = docs[0]
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

    # Check if VIP expired
    vip_level_id = user['vip_level_id']
    vip_expires_at = user['vip_expires_at']
    if vip_level_id > 0 and vip_expires_at > 0 and now >= vip_expires_at:
        supabase.table('users').update({
            'vip_level_id': 0,
            'vip_level_name': 'Starter (Expired)',
            'vip_expires_at': 0
        }).eq('id', user['id']).execute()
        refreshed = supabase.table('users').select('*').eq('id', user['id']).limit(1).execute().data or []
        if refreshed:
            user = refreshed[0]

    tiers = supabase.table('vip_tiers').select('*').execute().data or []
    tiers.sort(key=lambda x: x.get('price', 0))

    devices = supabase.table('spin_devices').select('*').execute().data or []
    for d in devices:
        try:
            if isinstance(d.get('discount_options'), str):
                d['discount_options'] = json.loads(d['discount_options'])
        except Exception:
            d['discount_options'] = []
    devices.sort(key=lambda x: x.get('created_at', 0), reverse=True)

    news_docs = supabase.table('news').select('*').execute().data or []
    news_docs.sort(key=lambda x: x.get('created_at', 0), reverse=True)
    news = news_docs[:10]

    tx_docs = supabase.table('transactions').select('*').eq('user_id', user['id']).execute().data or []
    tx_docs.sort(key=lambda x: x.get('created_at', 0), reverse=True)
    txs = tx_docs[:25]

    spin_orders_docs = supabase.table('spin_orders').select('*').eq('user_id', user['id']).execute().data or []
    spin_orders_docs.sort(key=lambda x: x.get('created_at', 0), reverse=True)

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
    if active_tier and (active_tier.get('is_vip2_target') == 1 or active_tier.get('price', 0) >= 600):
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

    dup = supabase.table('transactions').select('id').eq('reference', reference).limit(1).execute().data or []
    if dup:
        return jsonify({'ok': False, 'error': 'Transaction already processed.'}), 400

    now = int(time.time())
    user_rows = supabase.table('users').select('product_wallet').eq('id', user['id']).limit(1).execute().data or []
    current_prod = float(user_rows[0].get('product_wallet', 0.0)) if user_rows else float(user.get('product_wallet', 0.0))
    new_prod_wallet = current_prod + amount
    supabase.table('users').update({'product_wallet': new_prod_wallet}).eq('id', user['id']).execute()

    tx_id = get_next_sequence_id('transactions')
    supabase.table('transactions').insert({
        'id': tx_id,
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
        'last_claim_day': ''
    }).eq('id', user_row['id']).execute()

    tx_id = get_next_sequence_id('transactions')
    supabase.table('transactions').insert({
        'id': tx_id,
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
        ref_docs = supabase.table('users').select('*').eq('invite_code', user_row['referred_by_code']).limit(1).execute().data or []
        if ref_docs:
            referrer = ref_docs[0]
            commission = round(tier_row['price'] * 0.10, 2)
            supabase.table('users').update({
                'earnings_wallet': float(referrer.get('earnings_wallet', 0.0)) + commission
            }).eq('id', referrer['id']).execute()
            supabase.table('users').update({'referral_reward_paid': 1}).eq('id', user_row['id']).execute()

            comm_tx_id = get_next_sequence_id('transactions')
            supabase.table('transactions').insert({
                'id': comm_tx_id,
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

    tier_rows = supabase.table('vip_tiers').select('*').eq('id', tier_id).limit(1).execute().data or []
    if not tier_rows:
        return jsonify({'ok': False, 'error': 'Selected VIP tier does not exist.'}), 404

    tier = tier_rows[0]
    price = float(tier['price'])

    recruiter = None
    if vip2_recruit_code:
        if tier.get('is_vip2_target') != 1 and price < 600:
            return jsonify({'ok': False, 'error': 'Get Hired recruitment codes only apply to VIP 2 shares.'}), 400
        rec_docs = supabase.table('users').select('*').eq('invite_code', vip2_recruit_code).limit(1).execute().data or []
        if not rec_docs:
            return jsonify({'ok': False, 'error': 'Invalid Get Hired unique invitation code.'}), 400
        recruiter = rec_docs[0]
        if recruiter['id'] == user['id']:
            return jsonify({'ok': False, 'error': 'You cannot use your own Get Hired code.'}), 400

    curr_rows = supabase.table('users').select('*').eq('id', user['id']).limit(1).execute().data or []
    current_user = curr_rows[0] if curr_rows else user

    if method == 'wallet':
        if float(current_user.get('product_wallet', 0.0)) < price:
            return jsonify({'ok': False, 'error': f'Insufficient Product Wallet balance (GHS {float(current_user.get("product_wallet", 0.0)):,.2f}). Deposit or pay directly via Paystack.'}), 400
        supabase.table('users').update({
            'product_wallet': float(current_user.get('product_wallet', 0.0)) - price
        }).eq('id', user['id']).execute()
        ref_code = 'WAL-' + uuid.uuid4().hex[:10].upper()
        process_share_activation(current_user, tier, 'Product Wallet', ref_code)
    elif method == 'paystack':
        if not reference:
            return jsonify({'ok': False, 'error': 'Missing Paystack payment reference.'}), 400
        dup = supabase.table('transactions').select('id').eq('reference', reference).limit(1).execute().data or []
        if dup:
            return jsonify({'ok': False, 'error': 'Payment reference already used.'}), 400
        process_share_activation(current_user, tier, 'Paystack Direct', reference)
    else:
        return jsonify({'ok': False, 'error': 'Invalid payment method.'}), 400

    if recruiter:
        supabase.table('users').update({
            'vip2_invites_count': int(recruiter.get('vip2_invites_count', 0)) + 1
        }).eq('id', recruiter['id']).execute()

    return jsonify({
        'ok': True,
        'message': f'{tier["name"]} activated! Click "Claim Daily Profit" now to collect your first GHS {tier["daily_profit"]:,.2f}.'
    })


@app.route('/api/user/claim-daily', methods=['POST'])
@login_required
def api_claim_daily(user):
    now = int(time.time())
    today_str, seconds_to_midnight = get_ghana_day_info(now)

    if user['vip_level_id'] <= 0 or user['vip_expires_at'] <= now:
        return jsonify({'ok': False, 'error': 'No active VIP share package. Please purchase shares first.'}), 400

    tier_rows = supabase.table('vip_tiers').select('*').eq('id', user['vip_level_id']).limit(1).execute().data or []
    if not tier_rows:
        return jsonify({'ok': False, 'error': 'VIP tier not found.'}), 404

    tier = tier_rows[0]
    last_claim_day = user.get('last_claim_day') or ''
    if not last_claim_day and (user.get('last_claim_at') or 0) > 0:
        last_claim_day, _ = get_ghana_day_info(user.get('last_claim_at'))

    if last_claim_day == today_str:
        hrs = seconds_to_midnight // 3600
        mins = (seconds_to_midnight % 3600) // 60
        return jsonify({'ok': False, 'error': f'Daily profit already claimed today. Resets at 00:00 ({hrs}h {mins}m left).'}), 400

    profit = float(tier['daily_profit'])
    supabase.table('users').update({
        'earnings_wallet': float(user.get('earnings_wallet', 0.0)) + profit,
        'last_claim_at': now,
        'last_claim_day': today_str
    }).eq('id', user['id']).execute()

    tx_id = get_next_sequence_id('transactions')
    supabase.table('transactions').insert({
        'id': tx_id,
        'user_id': user['id'],
        'user_name': user['name'],
        'user_phone': user['phone'],
        'tx_type': 'daily_claim',
        'amount': profit,
        'reference': 'CLM-' + uuid.uuid4().hex[:8].upper(),
        'status': 'completed',
        'details': f"{tier['name']} Daily Dividend Claim",
        'created_at': now
    }).execute()
    return jsonify({'ok': True, 'message': f'GHS {profit:,.2f} daily profit added to your Earnings Wallet!'})


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

    now = int(time.time())
    supabase.table('users').update({
        'earnings_wallet': float(user.get('earnings_wallet', 0.0)) + reward,
        'vip2_bonus_claimed': 1
    }).eq('id', user['id']).execute()

    tx_id = get_next_sequence_id('transactions')
    supabase.table('transactions').insert({
        'id': tx_id,
        'user_id': user['id'],
        'user_name': user['name'],
        'user_phone': user['phone'],
        'tx_type': 'vip2_bonus',
        'amount': reward,
        'reference': 'HIRE-' + uuid.uuid4().hex[:8].upper(),
        'status': 'completed',
        'details': f'Get Hired {target} VIP2 Milestone Reward',
        'created_at': now
    }).execute()
    return jsonify({'ok': True, 'message': f'Congratulations! GHS {reward:,.2f} added to your Earnings Wallet!'})


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

    now = int(time.time())
    ref = 'WDR-' + uuid.uuid4().hex[:10].upper()

    supabase.table('users').update({
        'earnings_wallet': float(user.get('earnings_wallet', 0.0)) - amount
    }).eq('id', user['id']).execute()

    tx_id = get_next_sequence_id('transactions')
    supabase.table('transactions').insert({
        'id': tx_id,
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
    today_str, seconds_to_midnight = get_ghana_day_info(now)

    device_rows = supabase.table('spin_devices').select('*').eq('id', device_id).limit(1).execute().data or []
    if not device_rows:
        return jsonify({'ok': False, 'error': 'Device not found.'}), 404

    device = device_rows[0]

    if device['spin_type'] == 'free':
        tier_id = user['vip_level_id']
        tier_rows = supabase.table('vip_tiers').select('*').eq('id', tier_id).limit(1).execute().data if tier_id > 0 else []
        tier = tier_rows[0] if tier_rows else None
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
        supabase.table('users').update({
            'last_free_spin_at': now,
            'last_free_spin_day': today_str
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

    device_rows = supabase.table('spin_devices').select('*').eq('id', device_id).limit(1).execute().data or []
    if not device_rows:
        return jsonify({'ok': False, 'error': 'Device not found.'}), 404

    device = device_rows[0]

    if spin_type == 'sale':
        if amount_paid <= 0 or not paystack_ref:
            return jsonify({'ok': False, 'error': 'Paystack payment confirmation is required before submitting delivery details.'}), 400
        dup = supabase.table('spin_orders').select('id').eq('paystack_ref', paystack_ref).limit(1).execute().data or []
        if dup:
            return jsonify({'ok': False, 'error': 'This Paystack payment reference has already been submitted.'}), 400

    now = int(time.time())
    order_id = get_next_sequence_id('spin_orders')
    supabase.table('spin_orders').insert({
        'id': order_id,
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
        tx_id = get_next_sequence_id('transactions')
        supabase.table('transactions').insert({
            'id': tx_id,
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
    data = request.get_json(force=True)
    raw_phone = (data.get('new_phone') or '').strip()
    new_phone = normalize_ghana_phone(raw_phone)
    password = (data.get('password') or '').strip()

    if not raw_phone or not new_phone:
        return jsonify({'ok': False, 'error': 'Enter a valid Ghana MTN (024, 054, 055, 059) or Telecel (020, 050) phone number.'}), 400
    if not check_password_hash(user['password_hash'], password):
        return jsonify({'ok': False, 'error': 'Password verification failed.'}), 400

    existing_docs = supabase.table('users').select('id').eq('phone', new_phone).execute().data or []
    if any(d['id'] != user['id'] for d in existing_docs):
        return jsonify({'ok': False, 'error': 'Phone number already in use by another account.'}), 400

    supabase.table('users').update({'phone': new_phone}).eq('id', user['id']).execute()
    return jsonify({'ok': True, 'message': 'Phone number updated successfully!'})


# ==============================================================================
# REAL-TIME LIVE CHAT API
# ==============================================================================
@app.route('/api/chat/messages', methods=['GET'])
@login_required
def api_user_chat_messages(user):
    after_id = int(request.args.get('after_id') or 0)
    msg_docs = supabase.table('chat_messages').select('*').eq('user_id', user['id']).execute().data or []
    msg_docs.sort(key=lambda x: x.get('id', 0))

    filtered = []
    for m in msg_docs:
        if m['id'] > after_id:
            filtered.append(m)
        if m.get('sender') == 'admin' and m.get('is_read', 0) == 0:
            supabase.table('chat_messages').update({'is_read': 1}).eq('id', m['id']).execute()

    return jsonify({'ok': True, 'messages': filtered})


@app.route('/api/chat/send', methods=['POST'])
@login_required
def api_user_chat_send(user):
    data = request.get_json(force=True)
    msg = (data.get('message') or '').strip()
    if not msg:
        return jsonify({'ok': False, 'error': 'Message cannot be empty.'}), 400

    now = int(time.time())
    mid = get_next_sequence_id('chat_messages')
    msg_obj = {
        'id': mid,
        'user_id': user['id'],
        'sender': 'user',
        'message': msg,
        'is_read': 0,
        'created_at': now
    }
    supabase.table('chat_messages').insert(msg_obj).execute()
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
    users = supabase.table('users').select('*').execute().data or []
    users.sort(key=lambda x: x.get('id', 0), reverse=True)

    tiers = supabase.table('vip_tiers').select('*').execute().data or []
    tiers.sort(key=lambda x: x.get('price', 0))

    devices = supabase.table('spin_devices').select('*').execute().data or []
    for d in devices:
        try:
            if isinstance(d.get('discount_options'), str):
                d['discount_options'] = json.loads(d['discount_options'])
        except Exception:
            d['discount_options'] = []
    devices.sort(key=lambda x: x.get('created_at', 0), reverse=True)

    spin_orders = supabase.table('spin_orders').select('*').execute().data or []
    spin_orders.sort(key=lambda x: x.get('created_at', 0), reverse=True)

    news = supabase.table('news').select('*').execute().data or []
    news.sort(key=lambda x: x.get('created_at', 0), reverse=True)

    transactions = supabase.table('transactions').select('*').execute().data or []
    transactions.sort(key=lambda x: x.get('created_at', 0), reverse=True)
    limited_txs = transactions[:100]

    start_of_today = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    paid_today = sum(float(t.get('amount', 0)) for t in transactions if t.get('tx_type') in ('deposit', 'share_buy') and t.get('created_at', 0) >= start_of_today)
    total_paid = sum(float(t.get('amount', 0)) for t in transactions if t.get('tx_type') in ('deposit', 'share_buy'))
    total_withdrawn = sum(float(t.get('amount', 0)) for t in transactions if t.get('tx_type') == 'withdrawal')

    all_messages = supabase.table('chat_messages').select('*').execute().data or []
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
    rows = supabase.table('users').select('*').eq('id', uid).limit(1).execute().data or []
    if not rows:
        return jsonify({'ok': False, 'error': 'User not found.'}), 404
    u = rows[0]
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
        t_rows = supabase.table('vip_tiers').select('*').eq('id', vip_level_id).limit(1).execute().data or []
        t = t_rows[0] if t_rows else None
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
        new_id = get_next_sequence_id('vip_tiers')
        supabase.table('vip_tiers').insert({
            'id': new_id,
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
        (1, 'VIP 1', 300.0, 50.0, 15, 0),
        (2, 'VIP 2', 600.0, 110.0, 15, 1),
        (3, 'VIP 3', 1200.0, 230.0, 15, 0),
        (4, 'VIP 4', 2500.0, 500.0, 15, 0),
    ]
    for tid, name, price, profit, days, vip2 in default_tiers:
        supabase.table('vip_tiers').upsert({
            'id': tid,
            'name': name,
            'price': price,
            'daily_profit': profit,
            'duration_days': days,
            'is_vip2_target': vip2
        }).execute()
    supabase.table('counters').upsert({'name': 'vip_tiers', 'val': 4}).execute()
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

    did = get_next_sequence_id('spin_devices')
    supabase.table('spin_devices').insert({
        'id': did,
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

    tx_rows = supabase.table('transactions').select('*').eq('id', tx_id).limit(1).execute().data or []
    if not tx_rows:
        return jsonify({'ok': False, 'error': 'Withdrawal record not found.'}), 404

    tx = tx_rows[0]
    if tx.get('tx_type') != 'withdrawal':
        return jsonify({'ok': False, 'error': 'Not a withdrawal transaction.'}), 400

    if action == 'paid':
        supabase.table('transactions').update({'status': 'Paid'}).eq('id', tx_id).execute()
        return jsonify({'ok': True, 'message': 'Withdrawal marked as Paid!'})
    elif action == 'refund':
        if tx.get('status') == 'Refunded':
            return jsonify({'ok': False, 'error': 'Already refunded.'}), 400
        u_rows = supabase.table('users').select('earnings_wallet').eq('id', tx['user_id']).limit(1).execute().data or []
        if u_rows:
            new_earn = float(u_rows[0].get('earnings_wallet', 0.0)) + float(tx['amount'])
            supabase.table('users').update({'earnings_wallet': new_earn}).eq('id', tx['user_id']).execute()
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
    nid = get_next_sequence_id('news')
    supabase.table('news').insert({
        'id': nid,
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
        supabase.table('settings').upsert({'key': 'min_withdrawal', 'value': str(float(data['min_withdrawal']))}).execute()
    if 'paystack_public_key' in data and str(data['paystack_public_key']).strip():
        supabase.table('settings').upsert({'key': 'paystack_public_key', 'value': str(data['paystack_public_key']).strip()}).execute()
    if 'paystack_secret_key' in data and str(data['paystack_secret_key']).strip():
        supabase.table('settings').upsert({'key': 'paystack_secret_key', 'value': str(data['paystack_secret_key']).strip()}).execute()
    if 'invite_vip2_target' in data and str(data['invite_vip2_target']).strip():
        t_val = int(float(data['invite_vip2_target']))
        if t_val < 1:
            return jsonify({'ok': False, 'error': 'Required invite count must be at least 1.'}), 400
        supabase.table('settings').upsert({'key': 'invite_vip2_target', 'value': str(t_val)}).execute()
    if 'invite_vip2_reward' in data and str(data['invite_vip2_reward']).strip():
        r_val = float(data['invite_vip2_reward'])
        if r_val < 0:
            return jsonify({'ok': False, 'error': 'Invite reward amount cannot be negative.'}), 400
        supabase.table('settings').upsert({'key': 'invite_vip2_reward', 'value': str(r_val)}).execute()
    if 'about_company_text' in data:
        supabase.table('settings').upsert({'key': 'about_company_text', 'value': str(data.get('about_company_text') or '').strip()}).execute()
    if 'about_company_image' in data:
        supabase.table('settings').upsert({'key': 'about_company_image', 'value': str(data.get('about_company_image') or '').strip()}).execute()
    if 'about_company_video' in data:
        supabase.table('settings').upsert({'key': 'about_company_video', 'value': str(data.get('about_company_video') or '').strip()}).execute()
    if 'new_admin_pin' in data and str(data['new_admin_pin']).strip():
        pin = str(data['new_admin_pin']).strip()
        if len(pin) != 6 or not pin.isdigit():
            return jsonify({'ok': False, 'error': 'Admin PIN must be exactly 6 digits.'}), 400
        supabase.table('settings').upsert({'key': 'admin_pin', 'value': pin}).execute()
    return jsonify({'ok': True, 'message': 'Platform configuration saved!'})


@app.route('/api/admin/chat/messages', methods=['GET'])
@admin_required
def api_admin_chat_messages():
    uid = int(request.args.get('user_id') or 0)
    after_id = int(request.args.get('after_id') or 0)
    msg_docs = supabase.table('chat_messages').select('*').eq('user_id', uid).execute().data or []
    msg_docs.sort(key=lambda x: x.get('id', 0))

    filtered = []
    for m in msg_docs:
        if m['id'] > after_id:
            filtered.append(m)
        if m.get('sender') == 'user' and m.get('is_read', 0) == 0:
            supabase.table('chat_messages').update({'is_read': 1}).eq('id', m['id']).execute()

    return jsonify({'ok': True, 'messages': filtered})


@app.route('/api/admin/chat/send', methods=['POST'])
@admin_required
def api_admin_chat_send():
    data = request.get_json(force=True)
    uid = int(data.get('user_id') or 0)
    msg = (data.get('message') or '').strip()
    if not uid or not msg:
        return jsonify({'ok': False, 'error': 'Select a user and enter a reply.'}), 400

    now = int(time.time())
    mid = get_next_sequence_id('chat_messages')
    msg_obj = {
        'id': mid,
        'user_id': uid,
        'sender': 'admin',
        'message': msg,
        'is_read': 0,
        'created_at': now
    }
    supabase.table('chat_messages').insert(msg_obj).execute()
    return jsonify({'ok': True, 'message': msg_obj})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
