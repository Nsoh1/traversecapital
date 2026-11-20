import os
import re
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash
from supabase import Client, create_client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(
    __name__,
    static_folder=STATIC_DIR if os.path.isdir(STATIC_DIR) else None,
    static_url_path="/static",
)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-this-secret-in-production")

# MTN Ghana valid 3-digit local prefixes (including 025)
MTN_PREFIXES = ("024", "025", "053", "054", "055", "059")

VIP_TIERS = {
    "VIP1": {"name": "VIP 1", "min_amount": 50.0, "daily_rate": 0.05},
    "VIP2": {"name": "VIP 2", "min_amount": 200.0, "daily_rate": 0.07},
    "VIP3": {"name": "VIP 3", "min_amount": 500.0, "daily_rate": 0.10},
}

_supabase_client: Client | None = None


def get_supabase() -> Client:
    """Lazily initialize and return the Supabase client."""
    global _supabase_client
    if _supabase_client is None:
        url = os.environ.get("SUPABASE_URL", "").strip()
        key = (
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
            or os.environ.get("SUPABASE_KEY")
            or ""
        ).strip()
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY (or SUPABASE_SERVICE_ROLE_KEY) must be set.")
        _supabase_client = create_client(url, key)
    return _supabase_client


def normalize_mtn_phone(phone: str) -> str | None:
    """
    Validate and normalize a Ghanaian MTN mobile number.
    Accepts 024, 025, 053, 054, 055, 059 (10 digits starting with 0, or 233XXXXXXXXX).
    Returns normalized 10-digit local format (e.g. '0251234567') or None if invalid.
    """
    if not phone:
        return None
    digits = re.sub(r"\D", "", str(phone))
    if digits.startswith("233") and len(digits) == 12:
        local = "0" + digits[3:]
    elif len(digits) == 10 and digits.startswith("0"):
        local = digits
    else:
        return None

    if not local.startswith(MTN_PREFIXES):
        return None
    return local


def _find_html_file(filename: str) -> str | None:
    """Locate an HTML file across common project directories."""
    candidate_dirs = [
        BASE_DIR,
        REPO_ROOT,
        os.path.join(BASE_DIR, "templates"),
        os.path.join(REPO_ROOT, "templates"),
        os.path.join(BASE_DIR, "frontend"),
        os.path.join(REPO_ROOT, "frontend"),
    ]
    for folder in candidate_dirs:
        if os.path.isfile(os.path.join(folder, filename)):
            return folder
    return None


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""
        if not token:
            return jsonify({"error": "Unauthorized"}), 401
        try:
            user_resp = get_supabase().auth.get_user(token)
            if not getattr(user_resp, "user", None):
                return jsonify({"error": "Invalid or expired token"}), 401
            request.auth_user = user_resp.user
        except Exception as exc:
            return jsonify({"error": f"Authentication failed: {exc}"}), 401
        return f(*args, **kwargs)

    return decorated


def init_db() -> None:
    """
    Verify Supabase connectivity and seed default platform config if missing.
    Never raises uncaught exceptions so Gunicorn startup is never blocked.
    """
    try:
        sb = get_supabase()
        existing = sb.table("platform_config").select("id").eq("id", "default").execute()
        if not existing.data:
            sb.table("platform_config").insert(
                {
                    "id": "default",
                    "mtn_prefixes": list(MTN_PREFIXES),
                    "maintenance_mode": False,
                }
            ).execute()
    except Exception as exc:
        app.logger.warning("init_db warning (non-fatal on startup): %s", exc)


# Safe startup call: logs warning instead of crashing Gunicorn on import
init_db()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "database": "supabase", "mtn_prefixes": MTN_PREFIXES}), 200


@app.route("/", methods=["GET"])
def serve_index():
    folder = _find_html_file("index.html")
    if folder:
        return send_from_directory(folder, "index.html")
    return jsonify({"message": "TravestCapital API running"}), 200


@app.route("/admin", methods=["GET"])
def serve_admin():
    folder = _find_html_file("admin.html")
    if folder:
        return send_from_directory(folder, "admin.html")
    return jsonify({"error": "admin.html not found"}), 404


@app.route("/<path:filename>", methods=["GET"])
def serve_root_asset(filename: str):
    """Serve static assets or secondary HTML files located in BASE_DIR or REPO_ROOT."""
    if filename.startswith("api/"):
        return jsonify({"error": "Not found"}), 404
    for folder in (BASE_DIR, REPO_ROOT):
        candidate = os.path.join(folder, filename)
        if os.path.isfile(candidate) and os.path.commonpath([folder, candidate]) == folder:
            return send_from_directory(folder, filename)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    full_name = (data.get("full_name") or "").strip()
    raw_phone = data.get("phone") or ""
    referral_code = (data.get("referral_code") or "").strip()

    if not email or not password:
        return jsonify({"error": "Email and password are required."}), 400

    phone = normalize_mtn_phone(raw_phone)
    if not phone:
        return jsonify(
            {
                "error": "Enter a valid MTN Ghana number using prefix 024, 025, 053, 054, 055, or 059."
            }
        ), 400

    try:
        sb = get_supabase()
        auth_res = sb.auth.sign_up({"email": email, "password": password})
        auth_user = getattr(auth_res, "user", None)
        if not auth_user:
            return jsonify({"error": "Could not create user account."}), 400

        profile_payload = {
            "id": auth_user.id,
            "email": email,
            "full_name": full_name,
            "phone": phone,
            "password_hash": generate_password_hash(password),
            "referral_code": referral_code or None,
            "balance": 0.0,
            "vip_tier": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        sb.table("profiles").upsert(profile_payload).execute()

        return jsonify(
            {
                "message": "Account created successfully.",
                "user": {
                    "id": auth_user.id,
                    "email": email,
                    "full_name": full_name,
                    "phone": phone,
                    "balance": 0.0,
                },
            }
        ), 201
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Email and password are required."}), 400

    try:
        sb = get_supabase()
        res = sb.auth.sign_in_with_password({"email": email, "password": password})
        session = getattr(res, "session", None)
        user = getattr(res, "user", None)
        if not session or not user:
            return jsonify({"error": "Invalid credentials."}), 401

        profile_resp = sb.table("profiles").select("*").eq("id", user.id).limit(1).execute()
        profile = profile_resp.data[0] if profile_resp.data else {}
        if profile.get("password_hash") and not check_password_hash(profile["password_hash"], password):
            return jsonify({"error": "Invalid credentials."}), 401

        return jsonify(
            {
                "access_token": session.access_token,
                "user": {
                    "id": user.id,
                    "email": email,
                    "full_name": profile.get("full_name", ""),
                    "phone": profile.get("phone", ""),
                    "balance": float(profile.get("balance") or 0.0),
                    "vip_tier": profile.get("vip_tier"),
                },
            }
        ), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 401


@app.route("/api/profile", methods=["GET", "PATCH"])
@require_auth
def profile():
    user_id = request.auth_user.id
    sb = get_supabase()

    if request.method == "GET":
        resp = sb.table("profiles").select("*").eq("id", user_id).limit(1).execute()
        if not resp.data:
            return jsonify({"error": "Profile not found."}), 404
        row = dict(resp.data[0])
        row.pop("password_hash", None)
        return jsonify({"profile": row}), 200

    data = request.get_json(silent=True) or {}
    updates = {}
    if "full_name" in data:
        updates["full_name"] = (data["full_name"] or "").strip()
    if "phone" in data:
        phone = normalize_mtn_phone(data["phone"])
        if not phone:
            return jsonify(
                {
                    "error": "Enter a valid MTN Ghana number using prefix 024, 025, 053, 054, 055, or 059."
                }
            ), 400
        updates["phone"] = phone

    if not updates:
        return jsonify({"error": "No valid fields to update."}), 400

    updated = sb.table("profiles").update(updates).eq("id", user_id).execute()
    row = dict(updated.data[0]) if updated.data else {}
    row.pop("password_hash", None)
    return jsonify({"profile": row}), 200


@app.route("/api/transactions", methods=["GET"])
@require_auth
def list_transactions():
    user_id = request.auth_user.id
    resp = (
        get_supabase()
        .table("transactions")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return jsonify({"transactions": resp.data or []}), 200


@app.route("/api/claim-profit", methods=["POST"])
@require_auth
def claim_profit():
    user_id = request.auth_user.id
    sb = get_supabase()

    prof_resp = sb.table("profiles").select("*").eq("id", user_id).limit(1).execute()
    if not prof_resp.data:
        return jsonify({"error": "Profile not found."}), 404

    user_row = prof_resp.data[0]
    tier_key = (user_row.get("vip_tier") or "").upper()
    tier = VIP_TIERS.get(tier_key)
    if not tier:
        return jsonify({"error": "No active VIP tier found."}), 400

    current_balance = float(user_row.get("balance") or 0.0)
    profit = round(current_balance * tier["daily_rate"], 2)
    new_balance = round(current_balance + profit, 2)

    sb.table("profiles").update({"balance": new_balance}).eq("id", user_id).execute()
    tx_resp = (
        sb.table("transactions")
        .insert(
            {
                "user_id": user_id,
                "type": "daily_profit",
                "amount": profit,
                "status": "completed",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .execute()
    )

    return jsonify(
        {
            "profit_claimed": profit,
            "new_balance": new_balance,
            "transaction": tx_resp.data[0] if tx_resp.data else None,
        }
    ), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
