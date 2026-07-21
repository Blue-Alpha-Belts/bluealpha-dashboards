from flask import Flask, send_from_directory, abort, request, Response, redirect, make_response
import os
import re
import functools
import json
import base64
import threading
import secrets
import requests as req_lib
import jwt as pyjwt

# Per-record lock registry — prevents race condition where two background threads
# both pass the _has_items check before either creates Return Items records.
_return_items_locks: dict = {}
_return_items_locks_mutex = threading.Lock()

def _get_return_items_lock(record_id: str) -> threading.Lock:
    with _return_items_locks_mutex:
        if record_id not in _return_items_locks:
            _return_items_locks[record_id] = threading.Lock()
        return _return_items_locks[record_id]

_BUILD_VERSION = "catalog-live"

AIRTABLE_OPS_TOKEN      = os.environ.get("AIRTABLE_OPS_TOKEN", "")
AIRTABLE_BASE_TOKEN     = os.environ.get("AIRTABLE_BASE_TOKEN", "")
AIRTABLE_WRITE_TOKEN    = os.environ.get("AIRTABLE_WRITE_TOKEN", "")
RETURNS_WRITE_TOKEN     = os.environ.get("AIRTABLE_WRITE_TOKEN_2", AIRTABLE_WRITE_TOKEN)
APPLY_WRITE_TOKEN       = os.environ.get("APPLY_WRITE_TOKEN", "")

def _today_utc():
    """Return today's date in UTC (avoids Railway timezone drift)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date()
FLASK_BASE_URL          = os.environ.get("FLASK_BASE_URL", "https://bluealpha-dashboards-production.up.railway.app")
AIRTABLE_BASE_ID        = "appA13jo4b3TIn4yT"
RETURNS_TABLE_ID        = os.environ.get("RETURNS_TABLE_ID", "tblxwbeaVHBzXcAen")
RETURN_ITEMS_TABLE_ID   = "tblThFm0UA6gLQShV"
PRODUCT_SKUS_TABLE_ID   = "tbljngm75r4Km2XIN"
RM_SNAPSHOTS_TABLE_ID   = os.environ.get("RM_SNAPSHOTS_TABLE_ID", "")
RM_SNAPSHOTS_BASE_ID    = os.environ.get("RM_SNAPSHOTS_BASE_ID", AIRTABLE_BASE_ID)
RAW_MATERIALS_TABLE_ID  = "tblokid4GHQCvdXuQ"
SIZING_EXCHANGE_STORE_ID = 185018

SHIPSTATION_KEY      = os.environ.get("SHIPSTATION_KEY", "")
SHIPSTATION_SECRET   = os.environ.get("SHIPSTATION_SECRET", "")
SENDGRID_API_KEY     = os.environ.get("SENDGRID_API_KEY", "")
SENDGRID_FROM_EMAIL  = os.environ.get("SENDGRID_FROM_EMAIL", "orders@bluealpha.us")
CS_FROM_EMAIL        = "info@bluealpha.us"   # CS portal emails (exchange, returns, warranty, etc.)
TEST_EMAIL_OVERRIDE  = os.environ.get("TEST_EMAIL_OVERRIDE", "")
CS_ADMIN_PASSWORD    = os.environ.get("CS_ADMIN_PASSWORD", "")

QUOTE_ADMIN_PASSWORD     = os.environ.get("QUOTE_ADMIN_PASSWORD", "")
QUOTE_CS_PASSWORD        = os.environ.get("QUOTE_CS_PASSWORD", "")
QUOTE_SECRET_KEY         = os.environ.get("QUOTE_SECRET_KEY", "change-me-ba-portal-2024")

STRIPE_SECRET_KEY        = os.environ.get("STRIPE_SECRET_KEY", "")
INT_EXCHANGE_TABLE_ID    = os.environ.get("INT_EXCHANGE_TABLE_ID", "")
RETURN_ADDRESS_INTL      = "35 Andrew St., Newnan, GA 30263 USA"

MANUAL_ORDERS_TABLE_ID   = "tblOOZ2wVzIsR1DyL"
MO_LINE_ITEMS_TABLE_ID   = "tblNDxbfgyZDMex7n"
CUSTOMERS_TABLE_ID       = "tblO4AdJE84kFDfEe"
EMPLOYEES_TABLE_ID       = "tblUDcItnhNhe2GgO"
# NOTE: These two tables must be created manually in Airtable and their IDs updated here.
# Table 1: "Account Applications" — see task description for required fields
# Table 2: "Portal Users" — see task description for required fields
APP_APPLICATIONS_TABLE_ID = os.environ.get("APP_APPLICATIONS_TABLE_ID", "tbl_REPLACE_APPLICATIONS")
PORTAL_USERS_TABLE_ID     = os.environ.get("PORTAL_USERS_TABLE_ID",     "tbl_REPLACE_PORTAL_USERS")
PARENT_PRODUCTS_TABLE_ID    = "tbl40th76YvjdQExS"
INVENTORY_ADJUSTMENTS_TABLE_ID = "tbl95iUeitvqYwggK"
COLORS_TABLE_ID             = "tblN08IV26TpRYSMf"
SIZES_TABLE_ID              = "tblUGwl1YLaVGCeIJ"
FEATURE_VARIATIONS_TABLE_ID = "tblwbWDNFSjJSV9hh"
ADDONS_TABLE_ID             = "tblW8N35cbaXQDuQv"
QUOTE_BASE_URL           = os.environ.get("QUOTE_BASE_URL", "https://quote.bluealphabelts.com")

app = Flask(__name__, static_folder="static")

@app.before_request
def redirect_http_to_https():
    # Railway sets X-Forwarded-Proto when behind the proxy
    proto = request.headers.get("X-Forwarded-Proto") or request.headers.get("X-Forwarded-Scheme")
    if proto == "http":
        return redirect(request.url.replace("http://", "https://", 1), 301)

@app.after_request
def add_security_headers(response):
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

# In-memory status cache for return submissions (cleared on restart, only needed during ~60s poll window)
_return_status_cache = {}

# In-memory store for pending international exchange checkout sessions (keyed by UUID ref_id)
_intl_pending = {}

DASHBOARDS = {
    "kurt": "kurt.html",
    "jesse": "jesse.html",
    "kelly": "kelly.html",
    "patty": "patty.html",
}

OPS_DASHBOARDS = {
    "production": "production.html",
    "shipments":  "shipments.html",
    "waiting":    "waiting.html",
    "returns":    "returns.html",
}

USERNAME = "bluealpha"
PASSWORD = "bluealpha2026"

def check_auth(username, password):
    return username == USERNAME and password == PASSWORD

def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response(
                "Authentication required.",
                401,
                {"WWW-Authenticate": 'Basic realm="Blue Alpha Dashboards"'}
            )
        return f(*args, **kwargs)
    return decorated

@app.route("/_version")
def version():
    tok = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    return Response(json.dumps({
        "v": _BUILD_VERSION,
        "base_token_set": bool(AIRTABLE_BASE_TOKEN),
        "token_prefix": tok[:12] if tok else "(empty)",
        "write_token_set": bool(RETURNS_WRITE_TOKEN),
        "write_token_prefix": RETURNS_WRITE_TOKEN[:12] if RETURNS_WRITE_TOKEN else "(empty)",
        "ss_key_set": bool(SHIPSTATION_KEY),
        "ss_key_prefix": SHIPSTATION_KEY[:8] if SHIPSTATION_KEY else "(empty)",
        "cs_pw_set": bool(CS_ADMIN_PASSWORD),
    }), mimetype="application/json")

@app.route("/_ss_test")
def ss_test():
    """Quick ShipStation connectivity test — returns HTTP status + response time."""
    import time as _time
    t0 = _time.time()
    try:
        r = req_lib.get("https://ssapi.shipstation.com/orders",
                        params={"orderNumber": "0", "pageSize": 1},
                        headers=ss_headers(), timeout=15)
        elapsed = _time.time() - t0
        try:
            body = r.json()
        except Exception:
            body = r.text[:200]
        return Response(json.dumps({
            "http_status": r.status_code,
            "elapsed_s": round(elapsed, 2),
            "ss_key_prefix": SHIPSTATION_KEY[:8] if SHIPSTATION_KEY else "(empty)",
            "body_preview": str(body)[:200],
        }), mimetype="application/json")
    except Exception as e:
        elapsed = _time.time() - t0
        return Response(json.dumps({"error": str(e), "elapsed_s": round(elapsed, 2)}),
                        status=500, mimetype="application/json")

@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory("static", filename)

@app.route("/")
def index():
    host = request.host.split(".")[0].lower()
    if host == "exchange":
        return redirect("/exchange")
    if host == "return":
        return send_from_directory("static", "returns.html")
    if host in DASHBOARDS:
        return dashboard(host)
    if host == "quote":
        return redirect("/quote")
    return "Blue Alpha Dashboards", 200

@app.route("/cs")
def cs_returns():
    return send_from_directory("static", "cs-returns.html")

@app.route("/<name>")
@require_auth
def dashboard(name):
    if name in DASHBOARDS:
        return send_from_directory("static", DASHBOARDS[name])
    if name in OPS_DASHBOARDS:
        filepath = os.path.join(app.static_folder, OPS_DASHBOARDS[name])
        with open(filepath, "r") as f:
            content = f.read()
        content = content.replace("%%AIRTABLE_OPS_TOKEN%%", AIRTABLE_OPS_TOKEN)
        return Response(content, mimetype="text/html", headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        })
    abort(404)

def ss_headers():
    creds = base64.b64encode(f"{SHIPSTATION_KEY}:{SHIPSTATION_SECRET}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}

def cors():
    return {"Access-Control-Allow-Origin": "*"}

def at_headers(token):
    return {"Authorization": f"Bearer {token}"}

def _next_order_id(read_token):
    """Return the next Manual Order ID string (zero-padded 4 digits) by fetching only the top record."""
    try:
        params = {"pageSize": 1, "fields[]": "Order ID",
                  "sort[0][field]": "Order ID", "sort[0][direction]": "desc"}
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}",
            headers=at_headers(read_token), params=params, timeout=10,
        )
        records = r.json().get("records", [])
        if records:
            oid = records[0].get("fields", {}).get("Order ID", "0")
            try:
                return str(int(str(oid)) + 1).zfill(4)
            except (ValueError, TypeError):
                pass
    except Exception:
        pass
    import time as _t
    return str(int(_t.time()))[-6:]   # fallback: last 6 digits of timestamp

def at_get_all(table_id, token, fields=None, formula=None, base_id=None, view=None):
    """Paginate through all records in an Airtable table."""
    records = []
    offset = None
    bid = base_id or AIRTABLE_BASE_ID
    while True:
        params = {"pageSize": 100}
        if fields:
            for i, f in enumerate(fields):
                params[f"fields[{i}]"] = f
        if formula:
            params["filterByFormula"] = formula
        if view:
            params["view"] = view
        if offset:
            params["offset"] = offset
        r = req_lib.get(
            f"https://api.airtable.com/v0/{bid}/{table_id}",
            headers=at_headers(token),
            params=params,
            timeout=30,
        )
        data = r.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
    return records


# ─────────────────────────────────────────────────────────────────────────────
# Portal Auth Helpers
# ─────────────────────────────────────────────────────────────────────────────

def create_portal_token(user_id, customer_id, is_primary, role=None):
    from datetime import datetime, timezone, timedelta
    payload = {
        "user_id":     user_id,
        "customer_id": customer_id,
        "is_primary":  is_primary,
        "role":        role,   # None = legacy primary (treated as admin)
        "exp": datetime.now(timezone.utc) + timedelta(days=30),
    }
    return pyjwt.encode(payload, QUOTE_SECRET_KEY, algorithm="HS256")


def get_portal_user(req):
    token = req.cookies.get("ba_portal_session")
    if not token:
        return None
    try:
        return pyjwt.decode(token, QUOTE_SECRET_KEY, algorithms=["HS256"])
    except Exception:
        return None


def portal_login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        user = get_portal_user(request)
        if not user:
            # API routes return JSON 403; page routes redirect to /quote
            # (403 not 401 — browsers show a native auth dialog on 401)
            if request.path.startswith("/api/"):
                return Response(json.dumps({"error": "Unauthorized"}), status=403,
                                headers=cors(), mimetype="application/json")
            return redirect("/quote")
        return f(*args, user=user, **kwargs)
    return decorated


def _hash_password(password, salt=None):
    import hashlib, secrets as sec
    if salt is None:
        salt = sec.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"sha256${salt}${h}"

def _check_password(password, stored_hash):
    try:
        _, salt, _ = stored_hash.split("$")
        return _hash_password(password, salt) == stored_hash
    except Exception:
        return False

def _lookup_admin(username, password):
    """Check username+password against Employees table. Returns record dict or None."""
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    formula = f"AND({{Portal Username}}='{username}',{{Quote Portal Admin}}=1)"
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}",
            headers=at_headers(read_token),
            params={"filterByFormula": formula, "fields[]": ["Full Name", "Portal Username", "Password Hash", "Email"]},
            timeout=10,
        )
        records = r.json().get("records", [])
        if not records:
            return None
        rec = records[0]
        stored = rec.get("fields", {}).get("Password Hash", "")
        if not stored or not _check_password(password, stored):
            return None
        return rec
    except Exception:
        return None

def _lookup_portal_user(username, password):
    """Check username+password for EITHER admin OR CS role.
    Returns (record, role) where role is 'admin' or 'cs', or (None, None)."""
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    formula = f"AND({{Portal Username}}='{username}',OR({{Quote Portal Admin}}=1,{{Quote Portal CS}}=1))"
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}",
            headers=at_headers(read_token),
            params={"filterByFormula": formula, "fields[]": ["Full Name", "Portal Username", "Password Hash", "Email", "Quote Portal Admin", "Quote Portal CS"]},
            timeout=10,
        )
        records = r.json().get("records", [])
        if not records:
            return None, None
        rec = records[0]
        stored = rec.get("fields", {}).get("Password Hash", "")
        if not stored or not _check_password(password, stored):
            return None, None
        role = 'admin' if rec.get("fields", {}).get("Quote Portal Admin") else 'cs'
        return rec, role
    except Exception:
        return None, None

def _lookup_portal_customer(username, password):
    """Look up a B2B customer by Portal Username + Password Hash.
    Returns (record, role, customer_id) or (None, None, None).
    role is one of: 'admin', 'full_access', 'quotes_only', 'read_only', or None (legacy = admin).
    customer_id is Parent Company ID if set, else own record ID."""
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    formula = f"{{Portal Username}}='{username}'"
    try:
        records = at_get_all(
            CUSTOMERS_TABLE_ID, read_token,
            fields=["Portal Username", "Portal Hash", "Portal Role",
                    "Parent Company", "Application Status"],
            formula=formula,
        )
        if not records:
            return None, None, None
        rec = records[0]
        f = rec.get("fields", {})
        stored_hash = f.get("Portal Hash", "")
        if not stored_hash or not _check_password(password, stored_hash):
            return None, None, None
        # Only approved customers can log in
        app_status = f.get("Application Status", "")
        if app_status and app_status != "Approved":
            return None, None, None
        role_raw = (f.get("Portal Role") or "").strip()
        role_map = {
            "Admin":       "admin",
            "Full Access": "full_access", "Orders": "orders",
            "Quotes Only": "quotes_only", "Invoices": "invoices",
            "Read Only":   "read_only",
        }
        role = role_map.get(role_raw, None)
        parent_ids = f.get("Parent Company", [])
        customer_id = parent_ids[0] if parent_ids else rec["id"]
        return rec, role, customer_id
    except Exception as e:
        print(f"[_lookup_portal_customer] error: {e}")
        return None, None, None


# Permission hierarchy
# Tabs: New Quote + Our Quotes = create_quote / Our Orders = view_orders / Invoices = view_invoices
_PORTAL_PERMISSIONS = {
    "read_only":   {"view", "view_quotes"},
    "quotes_only": {"view", "view_quotes", "create_quote"},
    "orders":      {"view", "view_quotes", "create_quote", "accept_quote", "view_orders"},
    "invoices":    {"view", "view_invoices"},
    "full_access": {"view", "view_quotes", "create_quote", "accept_quote", "view_orders", "view_invoices"},
    "admin":       {"view", "view_quotes", "create_quote", "accept_quote", "view_orders", "view_invoices", "manage_team"},
}

def portal_can(user, action):
    """Check if a portal user JWT payload can perform an action.
    user is the decoded JWT dict. action is one of:
    'view', 'create_quote', 'accept_quote', 'manage_team'.
    No role set (legacy primary users) → treated as admin."""
    role = user.get("role")
    if role is None:
        return True  # Legacy primary user — full admin
    return action in _PORTAL_PERMISSIONS.get(role, set())


def check_admin_session(req):
    token = req.cookies.get("ba_admin_session")
    if not token:
        return False
    try:
        data = pyjwt.decode(token, QUOTE_SECRET_KEY + "_admin", algorithms=["HS256"])
        return data.get("admin") is True
    except Exception:
        return False

def get_portal_role(req):
    """Returns 'admin', 'cs', or None from the admin session cookie."""
    token = req.cookies.get("ba_admin_session")
    if not token:
        return None
    try:
        data = pyjwt.decode(token, QUOTE_SECRET_KEY + "_admin", algorithms=["HS256"])
        return data.get("role") or ('admin' if data.get("admin") else None)
    except Exception:
        return None

def get_admin_username(req):
    """Return username from admin session, or None."""
    token = req.cookies.get("ba_admin_session")
    if not token:
        return None
    try:
        data = pyjwt.decode(token, QUOTE_SECRET_KEY + "_admin", algorithms=["HS256"])
        return data.get("username") if data.get("admin") else None
    except Exception:
        return None

def get_portal_username(req):
    """Return username from portal session (admin or CS), or None."""
    token = req.cookies.get("ba_admin_session")
    if not token:
        return None
    try:
        data = pyjwt.decode(token, QUOTE_SECRET_KEY + "_admin", algorithms=["HS256"])
        return data.get("username") if (data.get("admin") or data.get("role") in ('admin', 'cs')) else None
    except Exception:
        return None

def get_portal_record_id(req):
    """Return the Employees record_id from the portal session cookie, or None."""
    token = req.cookies.get("ba_admin_session")
    if not token:
        return None
    try:
        data = pyjwt.decode(token, QUOTE_SECRET_KEY + "_admin", algorithms=["HS256"])
        return data.get("record_id")
    except Exception:
        return None


def generate_magic_link(portal_user_record_id, expiry_hours=0.25):
    from datetime import datetime, timezone, timedelta
    token = secrets.token_urlsafe(32)
    expiry = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)
    expiry_iso = expiry.isoformat().replace("+00:00", "Z")
    write_token = APPLY_WRITE_TOKEN or RETURNS_WRITE_TOKEN
    try:
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{portal_user_record_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {"Magic Token": token, "Token Expiry": expiry_iso}},
            timeout=10,
        )
    except Exception as e:
        print(f"[generate_magic_link] Airtable PATCH failed: {e}")
    return f"{QUOTE_BASE_URL}/auth/{token}"


def send_approval_email(to_email, to_name, company, magic_link):
    if not SENDGRID_API_KEY:
        return
    first_name = to_name.split()[0] if to_name else "there"
    actual_to = TEST_EMAIL_OVERRIDE or to_email
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;padding:32px 0;">
    <tr><td align="center">
      <table width="520" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#1B2438;padding:24px 36px;">
          <span style="font-family:Arial;font-size:20px;font-weight:800;color:#fff;letter-spacing:2px;">BLUE ALPHA</span>
        </td></tr>
        <tr><td style="padding:32px 36px;">
          <p style="color:#1a2633;font-size:16px;margin:0 0 8px;">Hi {first_name},</p>
          <p style="color:#6b7a8d;font-size:14px;line-height:1.6;margin:0 0 20px;">
            Great news — your application for <strong>{company}</strong> has been approved!
            You now have access to the Blue Alpha Government Agency Portal.
          </p>
          <p style="color:#6b7a8d;font-size:14px;line-height:1.6;margin:0 0 24px;">
            Click the button below to create your username and password. This link is valid for <strong>7 days</strong>.
          </p>
          <table cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
            <tr><td>
              <a href="{magic_link}" style="display:inline-block;background:#1B2438;color:#fff;font-family:Arial;font-size:14px;font-weight:700;text-decoration:none;padding:13px 32px;border-radius:6px;">Set Up Your Account →</a>
            </td></tr>
          </table>
          <p style="color:#6b7a8d;font-size:12px;line-height:1.5;">
            If you have trouble with the button, copy and paste this link:<br>
            <a href="{magic_link}" style="color:#1B2438;">{magic_link}</a>
          </p>
          <p style="color:#6b7a8d;font-size:12px;margin-top:16px;">
            Questions? Contact us at <a href="mailto:orders@bluealpha.us" style="color:#1B2438;">orders@bluealpha.us</a>
          </p>
        </td></tr>
        <tr><td style="background:#f5f7fa;border-top:1px solid #dde3ea;padding:16px 36px;text-align:center;">
          <p style="color:#6b7a8d;font-size:11px;margin:0;">Blue Alpha &bull; bluealphabelts.com</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
    try:
        req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": actual_to, "name": to_name}]}],
                "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
                "subject": "Your Blue Alpha Portal Access Has Been Approved",
                "content": [{"type": "text/html", "value": html_body}],
            },
            timeout=15,
        )
    except Exception as e:
        print(f"[send_approval_email] failed: {e}")


def send_denial_email(to_email, to_name, company, reason):
    if not SENDGRID_API_KEY:
        return
    first_name = to_name.split()[0] if to_name else "there"
    actual_to = TEST_EMAIL_OVERRIDE or to_email
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;padding:32px 0;">
    <tr><td align="center">
      <table width="520" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#1B2438;padding:24px 36px;">
          <span style="font-family:Arial;font-size:20px;font-weight:800;color:#fff;letter-spacing:2px;">BLUE ALPHA</span>
        </td></tr>
        <tr><td style="padding:32px 36px;">
          <p style="color:#1a2633;font-size:16px;margin:0 0 8px;">Hi {first_name},</p>
          <p style="color:#6b7a8d;font-size:14px;line-height:1.6;margin:0 0 16px;">
            Thank you for your interest in the Blue Alpha Government Agency Quote Portal.
            Unfortunately, we're unable to approve the application for <strong>{company}</strong> at this time.
          </p>
          <p style="color:#6b7a8d;font-size:14px;line-height:1.6;margin:0 0 16px;">
            <strong>Reason:</strong> {reason}
          </p>
          <p style="color:#6b7a8d;font-size:13px;line-height:1.6;">
            If you have questions, please contact us at
            <a href="mailto:orders@bluealpha.us" style="color:#1B2438;">orders@bluealpha.us</a>.
          </p>
        </td></tr>
        <tr><td style="background:#f5f7fa;border-top:1px solid #dde3ea;padding:16px 36px;text-align:center;">
          <p style="color:#6b7a8d;font-size:11px;margin:0;">Blue Alpha &bull; bluealphabelts.com</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
    try:
        req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": actual_to, "name": to_name}]}],
                "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
                "subject": "Blue Alpha Portal Application Update",
                "content": [{"type": "text/html", "value": html_body}],
            },
            timeout=15,
        )
    except Exception as e:
        print(f"[send_denial_email] failed: {e}")


def send_magic_link_email(to_email, magic_link):
    if not SENDGRID_API_KEY:
        return
    actual_to = TEST_EMAIL_OVERRIDE or to_email
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;padding:32px 0;">
    <tr><td align="center">
      <table width="520" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#1B2438;padding:24px 36px;">
          <span style="font-family:Arial;font-size:20px;font-weight:800;color:#fff;letter-spacing:2px;">BLUE ALPHA</span>
        </td></tr>
        <tr><td style="padding:32px 36px;">
          <p style="color:#1a2633;font-size:16px;margin:0 0 16px;">Your portal login link</p>
          <p style="color:#6b7a8d;font-size:14px;line-height:1.6;margin:0 0 24px;">
            Click the button below to log in to the Blue Alpha Government Portal. This link expires in 15 minutes and can only be used once.
          </p>
          <table cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
            <tr><td>
              <a href="{magic_link}" style="display:inline-block;background:#1B2438;color:#fff;font-family:Arial;font-size:14px;font-weight:700;text-decoration:none;padding:13px 32px;border-radius:6px;">Log In to Portal →</a>
            </td></tr>
          </table>
          <p style="color:#6b7a8d;font-size:12px;line-height:1.5;">
            If you didn't request this, you can safely ignore this email.<br><br>
            Trouble with the button? Copy this link:<br>
            <a href="{magic_link}" style="color:#1B2438;word-break:break-all;">{magic_link}</a>
          </p>
        </td></tr>
        <tr><td style="background:#f5f7fa;border-top:1px solid #dde3ea;padding:16px 36px;text-align:center;">
          <p style="color:#6b7a8d;font-size:11px;margin:0;">Blue Alpha &bull; bluealphabelts.com</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
    try:
        req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": actual_to}]}],
                "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
                "subject": "Your Blue Alpha Portal Login Link",
                "content": [{"type": "text/html", "value": html_body}],
            },
            timeout=15,
        )
    except Exception as e:
        print(f"[send_magic_link_email] failed: {e}")


@app.route("/api/raw-material-cost", methods=["GET"])
def raw_material_cost():
    c = cors()
    try:
        records = at_get_all(RAW_MATERIALS_TABLE_ID, AIRTABLE_OPS_TOKEN,
                             fields=["Total Inventory Value"])
        total = sum(r["fields"].get("Total Inventory Value") or 0 for r in records)
        count = len(records)
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500,
                        headers=c, mimetype="application/json")

    snapshots = []
    if RM_SNAPSHOTS_TABLE_ID:
        try:
            snap_records = at_get_all(
                RM_SNAPSHOTS_TABLE_ID, AIRTABLE_OPS_TOKEN,
                fields=["Month", "Total Inventory Value", "Record Count", "Notes"],
                base_id=RM_SNAPSHOTS_BASE_ID,
            )
            snapshots = sorted(
                [
                    {
                        "id": r["id"],
                        "month": r["fields"].get("Month"),
                        "total": r["fields"].get("Total Inventory Value"),
                        "count": r["fields"].get("Record Count"),
                        "notes": r["fields"].get("Notes", ""),
                    }
                    for r in snap_records
                    if r["fields"].get("Month")
                ],
                key=lambda x: x["month"],
            )
        except Exception:
            snapshots = []

    return Response(
        json.dumps({"current": {"total": round(total, 2), "count": count},
                    "snapshots": snapshots}),
        headers=c, mimetype="application/json",
    )


@app.route("/api/raw-material-cost/capture", methods=["POST", "OPTIONS"])
def capture_raw_material_cost():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(),
                                     "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "POST"})
    c = cors()
    if not RM_SNAPSHOTS_TABLE_ID or not AIRTABLE_WRITE_TOKEN:
        return Response(
            json.dumps({"error": "RM_SNAPSHOTS_TABLE_ID or AIRTABLE_WRITE_TOKEN not configured"}),
            status=400, headers=c, mimetype="application/json",
        )

    try:
        records = at_get_all(RAW_MATERIALS_TABLE_ID, AIRTABLE_OPS_TOKEN,
                             fields=["Total Inventory Value"])
        total = sum(r["fields"].get("Total Inventory Value") or 0 for r in records)
        count = len(records)
    except Exception as e:
        return Response(json.dumps({"error": f"Airtable fetch failed: {str(e)}"}),
                        status=500, headers=c, mimetype="application/json")

    from datetime import date as dt_date
    body = request.get_json() or {}
    snap_date = body.get("date", _today_utc().isoformat())
    notes = body.get("notes", "Auto-captured snapshot")

    try:
        r = req_lib.post(
            f"https://api.airtable.com/v0/{RM_SNAPSHOTS_BASE_ID}/{RM_SNAPSHOTS_TABLE_ID}",
            headers={**at_headers(AIRTABLE_WRITE_TOKEN), "Content-Type": "application/json"},
            json={"fields": {
                "Month": snap_date,
                "Total Inventory Value": round(total, 2),
                "Record Count": count,
                "Notes": notes,
            }},
            timeout=10,
        )
        if r.status_code in (200, 201):
            return Response(
                json.dumps({"success": True, "total": round(total, 2),
                            "count": count, "date": snap_date}),
                headers=c, mimetype="application/json",
            )
        else:
            return Response(json.dumps({"error": r.text}),
                            status=500, headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/verify-order", methods=["POST", "OPTIONS"])
def verify_order():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}
    order_number = data.get("orderNumber", "").strip()
    last_name    = data.get("lastName", "").strip().lower()
    email_input  = data.get("email", "").strip().lower()

    if not order_number or (not last_name and not email_input):
        return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

    try:
        from datetime import datetime, timezone, timedelta

        # Look up order in ShipStation
        r = req_lib.get("https://ssapi.shipstation.com/orders",
                         params={"orderNumber": order_number},
                         headers=ss_headers(), timeout=10)
        orders = r.json().get("orders", [])

        if not orders:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        order = orders[0]

        # Verify identity — last name OR email must match
        ship_name   = order.get("shipTo", {}).get("name", "").strip()
        order_last  = ship_name.split()[-1].lower() if ship_name else ""
        order_email = (order.get("customerEmail") or "").strip().lower()
        name_match  = last_name and last_name == order_last
        email_match = email_input and email_input == order_email
        if not name_match and not email_match:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        # Check international — also block military overseas (APO/FPO/DPO)
        # which use state codes AA, AE, AP and are USPS-domestic but overseas
        MILITARY_STATES = {"AA", "AE", "AP"}
        country = order.get("shipTo", {}).get("country", "US")
        state   = order.get("shipTo", {}).get("state", "").upper()
        if country not in ("US", "USA") or state in MILITARY_STATES:
            return Response(json.dumps({"status": "international"}), headers=c, mimetype="application/json")

        # Block returns on unshipped orders
        order_status = order.get("orderStatus", "")
        UNSHIPPED_STATUSES = {"awaiting_shipment", "awaiting_payment", "on_hold"}
        if order_status in UNSHIPPED_STATUSES:
            return Response(json.dumps({"status": "not_shipped"}), headers=c, mimetype="application/json")

        # Get ship date from shipments
        sr = req_lib.get("https://ssapi.shipstation.com/shipments",
                          params={"orderNumber": order_number},
                          headers=ss_headers(), timeout=10)
        shipments = sr.json().get("shipments", [])
        ship_date_str = shipments[0].get("shipDate", "") if shipments else ""

        # Also block if orderStatus isn't clearly shipped and there are no shipments
        if not ship_date_str and order_status != "shipped":
            return Response(json.dumps({"status": "not_shipped"}), headers=c, mimetype="application/json")

        def parse_dt(s):
            """Parse ISO date/datetime string, always return UTC-aware datetime."""
            s = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        if ship_date_str:
            ship_date = parse_dt(ship_date_str)
        else:
            od = order.get("orderDate", "")
            ship_date = parse_dt(od) if od else datetime.now(timezone.utc)

        eligible_until = ship_date + timedelta(days=37)
        if datetime.now(timezone.utc) > eligible_until:
            return Response(json.dumps({"status": "outside_window"}), headers=c, mimetype="application/json")

        # Build items list — exclude empty SKUs and discount/fee line items
        def is_returnable_item(i):
            sku = (i.get("sku") or "").strip()
            if not sku:
                return False
            if "total-discount" in sku.lower():
                return False
            return bool(i.get("name"))
        items = [{"sku": i.get("sku","").strip(), "name": i.get("name",""), "quantity": i.get("quantity",1)}
                 for i in order.get("items", []) if is_returnable_item(i)]

        # Check for existing active returns — build qty-per-SKU of already-requested returns
        # Use AIRTABLE_OPS_TOKEN for reads (write token may not have read scope)
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        already_returned_qtys = {}  # {sku: total_qty_already_requested}
        if RETURNS_TABLE_ID and airtable_read_token:
            try:
                filter_formula = (f"AND({{Order Number}}='{order_number}',"
                                 f"OR({{Status}}='New',{{Status}}='Label Sent',"
                                 f"{{Status}}='Needs Review',"
                                 f"{{Status}}='Items Received',{{Status}}='Partial Received',"
                                 f"{{Status}}='Refunded'))")
                ar = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                    params={"filterByFormula": filter_formula, "fields[]": ["Items to Return", "Status"]},
                    headers={"Authorization": f"Bearer {airtable_read_token}"},
                    timeout=10,
                )
                import re as re_lib
                for rec in ar.json().get("records", []):
                    items_text = rec.get("fields", {}).get("Items to Return", "")
                    for line in items_text.split("\n"):
                        # Format: "1x SKU-123 — Item Name"
                        m = re_lib.match(r'(\d+)x\s+(\S+)\s+[—\-]', line.strip())
                        if m:
                            qty  = int(m.group(1))
                            sku  = m.group(2).strip()
                            already_returned_qtys[sku] = already_returned_qtys.get(sku, 0) + qty
            except Exception:
                pass  # Don't block the flow if this check fails

        ship_to = order.get("shipTo", {})
        phone = (ship_to.get("phone") or
                 (order.get("billTo") or {}).get("phone") or "")
        return Response(json.dumps({
            "status":        "eligible",
            "orderId":       order.get("orderId"),
            "orderKey":      order.get("orderKey", ""),
            "customerName":  ship_to.get("name", ""),
            "shipDate":      ship_date_str,
            "eligibleUntil": eligible_until.isoformat(),
            "email":         order.get("customerEmail", ""),
            "phone":         phone,
            "address": {
                "name":       ship_to.get("name", ""),
                "street1":    ship_to.get("street1", ""),
                "street2":    ship_to.get("street2", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
            },
            "items": items,
            "alreadyReturnedQtys": already_returned_qtys,
        }), headers=c, mimetype="application/json")

    except Exception as e:
        return Response(json.dumps({"status": "error", "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/cs-lookup-order", methods=["POST", "OPTIONS"])
def cs_lookup_order():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    # Validate CS password
    if not CS_ADMIN_PASSWORD or data.get("password", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"status": "unauthorized"}), status=401, headers=c, mimetype="application/json")

    order_number = data.get("orderNumber", "").strip()
    if not order_number:
        return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

    mode = data.get("mode", "").strip()  # 'missing' | 'incorrect' | '' (default)

    try:
        # Fetch order — no eligibility check, just pull the data
        r = req_lib.get("https://ssapi.shipstation.com/orders",
                        params={"orderNumber": order_number},
                        headers=ss_headers(), timeout=10)
        orders = r.json().get("orders", [])
        if not orders:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        order = orders[0]
        ship_to = order.get("shipTo", {})

        def is_returnable_item(i):
            sku = (i.get("sku") or "").strip()
            if not sku:
                return False
            if "total-discount" in sku.lower():
                return False
            return bool(i.get("name"))

        items = [{"sku": i.get("sku", "").strip(), "name": i.get("name", ""), "quantity": i.get("quantity", 1)}
                 for i in order.get("items", []) if is_returnable_item(i)]

        # Remove already-cancelled items/quantities from this form's previous submissions
        # (skipped for missing/incorrect modes)
        if mode not in ("missing", "incorrect"):
            try:
                formula = f"AND({{Order Number}}='{order_number}',{{Type}}='Cancellation')"
                ac_r = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                    params={"filterByFormula": formula, "fields[]": ["Items to Return"]},
                    headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}"},
                    timeout=10,
                )
                already_cancelled = {}
                for rec in ac_r.json().get("records", []):
                    items_text = rec.get("fields", {}).get("Items to Return", "")
                    for line in items_text.split("\n"):
                        m = re.match(r'(\d+)x\s+(\S+)\s+[—\-]', line.strip())
                        if m:
                            already_cancelled[m.group(2).strip()] = \
                                already_cancelled.get(m.group(2).strip(), 0) + int(m.group(1))
                if already_cancelled:
                    adjusted = []
                    for item in items:
                        remaining = item["quantity"] - already_cancelled.get(item["sku"], 0)
                        if remaining > 0:
                            adjusted.append({**item, "quantity": remaining})
                    items = adjusted
            except Exception:
                pass  # Don't block lookup if this check fails

        # Get ship date from shipments
        sr = req_lib.get("https://ssapi.shipstation.com/shipments",
                         params={"orderNumber": order_number},
                         headers=ss_headers(), timeout=10)
        shipments = sr.json().get("shipments", [])
        active_shipments = [s for s in shipments if not s.get("voided", False)]
        ship_date_str    = active_shipments[0].get("shipDate", "") if active_shipments else ""
        tracking_number  = active_shipments[0].get("trackingNumber", "") if active_shipments else ""
        carrier_code     = active_shipments[0].get("carrierCode", "") if active_shipments else ""

        # Missing/incorrect modes: order MUST have shipped
        if mode in ("missing", "incorrect") and not active_shipments:
            return Response(json.dumps({"status": "not_shipped"}), headers=c, mimetype="application/json")

        phone = (ship_to.get("phone") or (order.get("billTo") or {}).get("phone") or "")

        # Build already-returned qty map so CS portal can gray out items
        # (skipped for missing/incorrect modes)
        cs_already_returned = {}
        if mode not in ("missing", "incorrect"):
            _cs_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
            if RETURNS_TABLE_ID and _cs_read_token:
                try:
                    _cs_ar_formula = (f"AND({{Order Number}}='{order_number}'"
                                      f",OR({{Status}}='New',{{Status}}='Label Sent',"
                                      f"{{Status}}='Items Received',{{Status}}='Partial Received',"
                                      f"{{Status}}='Refunded'),{{Type}}='Return')")
                    _cs_ar_resp = req_lib.get(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                        params={"filterByFormula": _cs_ar_formula, "fields[]": ["Items to Return"], "maxRecords": 20},
                        headers={"Authorization": f"Bearer {_cs_read_token}"},
                        timeout=10,
                    )
                    for _rec in _cs_ar_resp.json().get("records", []):
                        for _line in _rec.get("fields", {}).get("Items to Return", "").split("\n"):
                            _m = re.match(r'(\d+)x\s+(\S+)\s+[—\-]', _line.strip())
                            if _m:
                                _sku = _m.group(2).strip()
                                cs_already_returned[_sku] = cs_already_returned.get(_sku, 0) + int(_m.group(1))
                except Exception:
                    pass

        # Check for existing UPS Shipping Refund request for this order
        # (skipped for missing/incorrect modes)
        existing_shipping_refund = False
        if mode not in ("missing", "incorrect"):
            try:
                _sr_r = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                    params={"filterByFormula": f"AND({{Order Number}}='{order_number}',{{Type}}='UPS Shipping Refund')",
                            "maxRecords": 1, "fields[]": ["Order Number"]},
                    headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}"},
                    timeout=10,
                )
                if _sr_r.status_code == 200 and _sr_r.json().get("records"):
                    existing_shipping_refund = True
            except Exception:
                pass

        # Build orderGroups (combo expansion) for missing/incorrect modes
        order_groups = []
        if mode in ("missing", "incorrect"):
            for item in items:
                sku  = item["sku"]
                name = item["name"]
                qty  = item["quantity"]
                expanded = expand_sku_to_leaf_items(sku, qty)
                is_combo = not (len(expanded) == 1 and expanded[0][1] == sku)
                order_groups.append({
                    "originalSku":  sku,
                    "originalName": name,
                    "originalQty":  qty,
                    "isCombo":      is_combo,
                    "components": [
                        {"sku": c[1], "name": c[0], "qty": c[2]}
                        for c in expanded
                    ],
                })

        # Check for existing reshipment orders for this order number
        already_reshipped = {}
        if mode in ("missing", "incorrect"):
            for suffix_base in ("-M", "-I"):
                check_num = order_number + suffix_base
                counter = 2
                while counter <= 8:
                    chk_r = req_lib.get("https://ssapi.shipstation.com/orders",
                                        params={"orderNumber": check_num},
                                        headers=ss_headers(), timeout=10)
                    existing_orders = chk_r.json().get("orders", [])
                    if not existing_orders:
                        break
                    for o in existing_orders:
                        for it in o.get("items", []):
                            sku = (it.get("sku") or "").strip()
                            qty = it.get("quantity", 1)
                            if sku:
                                already_reshipped[sku] = already_reshipped.get(sku, 0) + qty
                    check_num = f"{order_number}{suffix_base}{counter}"
                    counter += 1

        return Response(json.dumps({
            "status":               "found",
            "orderId":              order.get("orderId"),
            "orderKey":             order.get("orderKey", ""),
            "orderStatus":          order.get("orderStatus", ""),
            "customerName":         ship_to.get("name", ""),
            "email":                order.get("customerEmail", ""),
            "phone":                phone,
            "shipDate":             ship_date_str,
            "trackingNumber":       tracking_number,
            "carrierCode":          carrier_code,
            "address": {
                "name":       ship_to.get("name", ""),
                "street1":    ship_to.get("street1", ""),
                "street2":    ship_to.get("street2", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country":    ship_to.get("country", "US"),
            },
            "items":                items,
            "orderGroups":          order_groups,
            "alreadyReturnedQtys":  cs_already_returned,
            "alreadyReshippedSkus": already_reshipped,
            "shippingAmount":       float(order.get("shippingAmount") or 0),
            "existingShippingRefund": existing_shipping_refund,
        }), headers=c, mimetype="application/json")

    except Exception as e:
        print(f"[cs_lookup_order] Exception: {e}")
        return Response(json.dumps({"status": "error", "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


def _cs_reship_submit(mode, data, c):
    """Shared logic for cs-missing-submit and cs-incorrect-submit."""
    from datetime import datetime, timezone
    if not CS_ADMIN_PASSWORD or data.get("password", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"status": "unauthorized"}), status=401, headers=c, mimetype="application/json")

    order_number   = str(data.get("orderNumber", "")).strip()
    order_id       = data.get("orderId")
    customer_email = data.get("customerEmail", "")
    address        = data.get("address", {})
    selected_items = data.get("selectedItems", [])
    notes          = data.get("notes", "").strip()

    if not order_number or not selected_items:
        return Response(json.dumps({"status": "error", "error": "Missing required fields"}),
                        status=400, headers=c, mimetype="application/json")

    suffix_base = "-M" if mode == "missing" else "-I"
    store_name_fragment = "missing items" if mode == "missing" else "incorrect item sent"
    internal_note = (f"Missing items reshipment of order {order_number}"
                     if mode == "missing"
                     else f"Incorrect item reshipment of order {order_number}")
    if notes:
        internal_note = internal_note + "\n" + notes

    try:
        # 1. Find next available order number suffix
        new_order_number = order_number + suffix_base
        suffix_counter   = 2
        while True:
            chk = req_lib.get("https://ssapi.shipstation.com/orders",
                              params={"orderNumber": new_order_number},
                              headers=ss_headers(), timeout=10)
            if not chk.json().get("orders"):
                break
            new_order_number = f"{order_number}{suffix_base}{suffix_counter}"
            suffix_counter += 1
            if suffix_counter > 20:
                break  # safety valve

        # 2. Look up store ID
        stores_r = req_lib.get("https://ssapi.shipstation.com/stores", headers=ss_headers(), timeout=10)
        missing_store_id = None
        for store in stores_r.json():
            if store_name_fragment in (store.get("storeName") or "").lower():
                missing_store_id = store.get("storeId")
                break

        # 3. Create ShipStation order
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        order_payload = {
            "orderNumber":    new_order_number,
            "orderDate":      now_str,
            "paymentDate":    now_str,
            "orderStatus":    "awaiting_shipment",
            "amountPaid":     0,
            "taxAmount":      0,
            "shippingAmount": 0,
            "internalNotes":  internal_note,
            "customerEmail":  customer_email,
            "shipTo": {
                "name":        address.get("name", ""),
                "street1":     address.get("street1", ""),
                "street2":     address.get("street2", ""),
                "city":        address.get("city", ""),
                "state":       address.get("state", ""),
                "postalCode":  address.get("postalCode", ""),
                "country":     "US",
                "residential": True,
            },
            "billTo": {
                "name":       address.get("name", ""),
                "street1":    address.get("street1", ""),
                "city":       address.get("city", ""),
                "state":      address.get("state", ""),
                "postalCode": address.get("postalCode", ""),
                "country":    "US",
            },
            "items": [
                {"sku": i["sku"], "name": i["name"], "quantity": int(i["qty"]), "unitPrice": 0}
                for i in selected_items
            ],
            "carrierCode":  "stamps_com",
            "serviceCode":  "usps_ground_advantage",
            "packageCode":  "package",
            "confirmation": "delivery",
            "weight":       {"value": 8, "units": "ounces"},
            "dimensions":   {"units": "inches", "length": 8, "width": 8, "height": 2},
            "advancedOptions": {"storeId": missing_store_id} if missing_store_id else {},
        }
        create_r = req_lib.post(
            "https://ssapi.shipstation.com/orders/createorder",
            headers={**ss_headers(), "Content-Type": "application/json"},
            json=order_payload,
            timeout=20,
        )
        if create_r.status_code not in (200, 201):
            return Response(json.dumps({"status": "error", "error": f"SS createorder failed: {create_r.status_code} {create_r.text[:200]}"}),
                            status=500, headers=c, mimetype="application/json")
        new_order_id = create_r.json().get("orderId")

        # 4. Apply Expedite tag 49845
        if new_order_id:
            try:
                req_lib.post(
                    "https://ssapi.shipstation.com/orders/addtag",
                    headers={**ss_headers(), "Content-Type": "application/json"},
                    json={"orderId": new_order_id, "tagId": 49845},
                    timeout=10,
                )
            except Exception:
                pass  # tag failure is non-fatal

        return Response(json.dumps({"status": "ok", "newOrderNumber": new_order_number}),
                        headers=c, mimetype="application/json")

    except Exception as e:
        print(f"[cs_reship_submit:{mode}] Exception: {e}")
        return Response(json.dumps({"status": "error", "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/cs-missing-submit", methods=["POST", "OPTIONS"])
def cs_missing_submit():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    return _cs_reship_submit("missing", request.get_json() or {}, cors())


@app.route("/api/cs-incorrect-submit", methods=["POST", "OPTIONS"])
def cs_incorrect_submit():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    return _cs_reship_submit("incorrect", request.get_json() or {}, cors())


def cancel_in_shipstation(order_id, items_to_cancel):
    """Void full order or remove specific items and re-save. Returns (success, note)."""
    try:
        # Fetch current order by ID
        r = req_lib.get(
            f"https://ssapi.shipstation.com/orders/{order_id}",
            headers=ss_headers(), timeout=15,
        )
        if r.status_code != 200:
            return False, f"Could not fetch order from ShipStation (HTTP {r.status_code})"
        order = r.json()

        current_items = order.get("items", [])

        # Build cancel map: sku → qty to remove
        cancel_map = {}
        for item in items_to_cancel:
            sku = (item.get("sku") or "").strip()
            if sku:
                cancel_map[sku] = cancel_map.get(sku, 0) + int(item.get("quantity", 1))

        # Determine remaining items after cancellation
        remaining = []
        for item in current_items:
            sku = (item.get("sku") or "").strip()
            qty = item.get("quantity", 1)
            if sku in cancel_map:
                remaining_qty = qty - cancel_map[sku]
                if remaining_qty > 0:
                    item = dict(item)
                    item["quantity"] = remaining_qty
                    remaining.append(item)
                # else: fully cancelled — omit from remaining
            else:
                remaining.append(item)

        if not remaining:
            # All items cancelled — mark order as cancelled in ShipStation
            internal_note = (order.get("internalNotes") or "").strip()
            internal_note += (" | " if internal_note else "") + "Cancelled via CS portal"
            cancel_payload = {
                "orderId":         order.get("orderId"),
                "orderNumber":     order.get("orderNumber"),
                "orderKey":        order.get("orderKey"),
                "orderDate":       order.get("orderDate"),
                "orderStatus":     "cancelled",
                "customerEmail":   order.get("customerEmail", ""),
                "billTo":          order.get("billTo", {}),
                "shipTo":          order.get("shipTo", {}),
                "items":           current_items,
                "amountPaid":      order.get("amountPaid", 0),
                "taxAmount":       order.get("taxAmount", 0),
                "shippingAmount":  order.get("shippingAmount", 0),
                "internalNotes":   internal_note,
                "advancedOptions": order.get("advancedOptions", {}),
            }
            cancel_r = req_lib.post(
                "https://ssapi.shipstation.com/orders/createorder",
                headers={**ss_headers(), "Content-Type": "application/json"},
                json=cancel_payload, timeout=20,
            )
            if cancel_r.status_code in (200, 201):
                return True, "Order cancelled in ShipStation"
            return False, f"Cancel failed (HTTP {cancel_r.status_code}): {cancel_r.text[:200]}"
        else:
            # Partial: update order with only the remaining items
            internal_note = (order.get("internalNotes") or "").strip()
            internal_note += (" | " if internal_note else "") + "Partial cancellation — some items removed"
            payload = {
                "orderId":         order.get("orderId"),
                "orderNumber":     order.get("orderNumber"),
                "orderKey":        order.get("orderKey"),
                "orderDate":       order.get("orderDate"),
                "orderStatus":     order.get("orderStatus", "awaiting_shipment"),
                "customerEmail":   order.get("customerEmail", ""),
                "billTo":          order.get("billTo", {}),
                "shipTo":          order.get("shipTo", {}),
                "items":           remaining,
                "amountPaid":      order.get("amountPaid", 0),
                "taxAmount":       order.get("taxAmount", 0),
                "shippingAmount":  order.get("shippingAmount", 0),
                "internalNotes":   internal_note,
                "advancedOptions": order.get("advancedOptions", {}),
            }
            upd_r = req_lib.post(
                "https://ssapi.shipstation.com/orders/createorder",
                headers={**ss_headers(), "Content-Type": "application/json"},
                json=payload, timeout=20,
            )
            if upd_r.status_code in (200, 201):
                return True, "Partial cancellation applied in ShipStation"
            return False, f"Order update failed (HTTP {upd_r.status_code}): {upd_r.text[:200]}"

    except Exception as e:
        return False, f"Exception during ShipStation cancellation: {e}"


@app.route("/api/cs-verify-exchange", methods=["POST", "OPTIONS"])
def cs_verify_exchange():
    """CS override: look up order for size exchange, skipping identity check and date window."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    # Validate CS password
    if not CS_ADMIN_PASSWORD or data.get("csPassword", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"status": "unauthorized"}), status=401, headers=c, mimetype="application/json")

    order_number = data.get("orderNumber", "").strip().lstrip("#")
    if not order_number:
        return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

    try:
        r = req_lib.get("https://ssapi.shipstation.com/orders",
                        params={"orderNumber": order_number},
                        headers=ss_headers(), timeout=10)
        orders = r.json().get("orders", [])
        if not orders:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        order = orders[0]
        ship_to = order.get("shipTo", {})

        # Block international and military overseas orders
        MILITARY_STATES = {"AA", "AE", "AP"}
        country = ship_to.get("country", "US")
        state   = ship_to.get("state", "").upper()
        if country not in ("US", "USA") or state in MILITARY_STATES:
            return Response(json.dumps({"status": "international"}), headers=c, mimetype="application/json")

        # Find exchange-eligible items via Airtable (same logic as /api/verify-exchange)
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        all_exchange_options = at_get_all(
            PRODUCT_SKUS_TABLE_ID,
            airtable_read_token,
            fields=["Parent Product"],
            formula="{Can Exchange}=TRUE()",
        )
        eligible_parent_ids = set()
        eligible_parent_ids.update(SAME_SKU_EXCHANGE_PARENT_IDS)  # same-SKU exchange
        for opt in all_exchange_options:
            for pid in opt["fields"].get("Parent Product", []):
                eligible_parent_ids.add(pid)

        eligible_items = []
        for item in order.get("items", []):
            sku = (item.get("sku") or "").strip()
            if not sku:
                continue
            at_r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
                params={"filterByFormula": f'{{SKU ID}}="{sku}"', "maxRecords": 1,
                        "fields[]": ["Name + Variations", "SKU ID", "Parent Product"]},
                headers=at_headers(airtable_read_token),
                timeout=10,
            )
            records = at_r.json().get("records", [])
            if not records:
                continue
            rec = records[0]
            parent_products = rec["fields"].get("Parent Product", [])
            parent_product_id = parent_products[0] if parent_products else ""
            if not parent_product_id or parent_product_id not in eligible_parent_ids:
                continue
            eligible_items.append({
                "name":            item.get("name", ""),
                "sku":             sku,
                "quantity":        int(item.get("quantity", 1)),
                "airtableId":      rec["id"],
                "parentProductId": parent_product_id,
                "sameSkuExchange": parent_product_id in SAME_SKU_EXCHANGE_PARENT_IDS,
            })

        if not eligible_items:
            return Response(json.dumps({"status": "no_eligible_items"}), headers=c, mimetype="application/json")

        # Determine next exchange order suffix
        already_exchanged_skus = set()
        next_suffix = "-E"
        try:
            for n in range(1, 10):
                suffix = "-E" if n == 1 else f"-E{n}"
                ex_r = req_lib.get("https://ssapi.shipstation.com/orders",
                                    params={"orderNumber": f"{order_number}{suffix}"},
                                    headers=ss_headers(), timeout=10)
                ex_orders = ex_r.json().get("orders", [])
                if not ex_orders:
                    next_suffix = suffix
                    break
                for ex_order in ex_orders:
                    orig_sku = ((ex_order.get("advancedOptions") or {}).get("customField3") or "").strip()
                    if not orig_sku:
                        notes_text = ex_order.get("internalNotes") or ""
                        m = re.search(r'Original SKUs?:\s*([^\.\n]+)', notes_text)
                        if m:
                            orig_sku = m.group(1).strip()
                    for s in orig_sku.split(","):
                        s = s.strip()
                        if s:
                            already_exchanged_skus.add(s)
        except Exception as ex_check_err:
            print(f"[cs-verify-exchange] exchange-order check failed (non-fatal): {ex_check_err}")

        # CS override: do NOT filter out already-exchanged items (exception flow)

        return Response(json.dumps({
            "status":        "eligible",
            "orderId":       order.get("orderId"),
            "orderNumber":   order_number,
            "orderKey":      order.get("orderKey", ""),
            "customerName":  ship_to.get("name", ""),
            "customerEmail": order.get("customerEmail", ""),
            "shipTo": {
                "name":       ship_to.get("name", ""),
                "street1":    ship_to.get("street1", ""),
                "street2":    ship_to.get("street2", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country":    ship_to.get("country", "US"),
            },
            "eligibleItems": eligible_items,
            "nextSuffix":    next_suffix,
        }), headers=c, mimetype="application/json")

    except Exception as e:
        import traceback
        print(f"[cs-verify-exchange] ERROR: {e}\n{traceback.format_exc()}")
        return Response(json.dumps({"status": "error", "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/cs-verify-intl-exchange", methods=["POST", "OPTIONS"])
def cs_verify_intl_exchange():
    """CS override: look up order for international exchange exception.
    Identical to cs-verify-exchange but skips the international/military block
    and the date eligibility window (already skipped in cs-verify-exchange).
    """
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    if not CS_ADMIN_PASSWORD or data.get("csPassword", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"status": "unauthorized"}), status=401, headers=c, mimetype="application/json")

    order_number = data.get("orderNumber", "").strip().lstrip("#")
    if not order_number:
        return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

    try:
        r = req_lib.get("https://ssapi.shipstation.com/orders",
                        params={"orderNumber": order_number},
                        headers=ss_headers(), timeout=10)
        orders = r.json().get("orders", [])
        if not orders:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        order = orders[0]
        ship_to = order.get("shipTo", {})

        # NOTE: International block intentionally omitted — this is the CS international exception flow

        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        all_exchange_options = at_get_all(
            PRODUCT_SKUS_TABLE_ID,
            airtable_read_token,
            fields=["Parent Product"],
            formula="{Can Exchange}=TRUE()",
        )
        eligible_parent_ids = set()
        eligible_parent_ids.update(SAME_SKU_EXCHANGE_PARENT_IDS)  # same-SKU exchange
        for opt in all_exchange_options:
            for pid in opt["fields"].get("Parent Product", []):
                eligible_parent_ids.add(pid)

        eligible_items = []
        for item in order.get("items", []):
            sku = (item.get("sku") or "").strip()
            if not sku:
                continue
            at_r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
                params={"filterByFormula": f'{{SKU ID}}="{sku}"', "maxRecords": 1,
                        "fields[]": ["Name + Variations", "SKU ID", "Parent Product"]},
                headers=at_headers(airtable_read_token),
                timeout=10,
            )
            records = at_r.json().get("records", [])
            if not records:
                continue
            rec = records[0]
            parent_products = rec["fields"].get("Parent Product", [])
            parent_product_id = parent_products[0] if parent_products else ""
            if not parent_product_id or parent_product_id not in eligible_parent_ids:
                continue
            eligible_items.append({
                "name":            item.get("name", ""),
                "sku":             sku,
                "quantity":        int(item.get("quantity", 1)),
                "airtableId":      rec["id"],
                "parentProductId": parent_product_id,
                "sameSkuExchange": parent_product_id in SAME_SKU_EXCHANGE_PARENT_IDS,
            })

        if not eligible_items:
            return Response(json.dumps({"status": "no_eligible_items"}), headers=c, mimetype="application/json")

        # Determine next exchange order suffix
        next_suffix = "-E"
        try:
            for n in range(1, 10):
                suffix = "-E" if n == 1 else f"-E{n}"
                ex_r = req_lib.get("https://ssapi.shipstation.com/orders",
                                    params={"orderNumber": f"{order_number}{suffix}"},
                                    headers=ss_headers(), timeout=10)
                ex_orders = ex_r.json().get("orders", [])
                if not ex_orders:
                    next_suffix = suffix
                    break
        except Exception as ex_check_err:
            print(f"[cs-verify-intl-exchange] suffix-check failed (non-fatal): {ex_check_err}")

        return Response(json.dumps({
            "status":        "eligible",
            "orderId":       order.get("orderId"),
            "orderNumber":   order_number,
            "orderKey":      order.get("orderKey", ""),
            "customerName":  ship_to.get("name", ""),
            "customerEmail": order.get("customerEmail", ""),
            "shipTo": {
                "name":       ship_to.get("name", ""),
                "street1":    ship_to.get("street1", ""),
                "street2":    ship_to.get("street2", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country":    ship_to.get("country", "US"),
            },
            "eligibleItems": eligible_items,
            "nextSuffix":    next_suffix,
        }), headers=c, mimetype="application/json")

    except Exception as e:
        import traceback
        print(f"[cs-verify-intl-exchange] ERROR: {e}\n{traceback.format_exc()}")
        return Response(json.dumps({"status": "error", "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/cs-intl-exchange-submit", methods=["POST", "OPTIONS"])
def cs_intl_exchange_submit():
    """CS-initiated international exchange exception.
    Creates Airtable record + Stripe checkout session, returns a checkout URL
    CS can email to the customer. The existing /api/international-success handler
    takes over once the customer pays ($10 fee).
    """
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    if not CS_ADMIN_PASSWORD or data.get("csPassword", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"error": "unauthorized"}), status=401, headers=c, mimetype="application/json")

    if not STRIPE_SECRET_KEY:
        return Response(json.dumps({"error": "Stripe not configured"}),
                        status=500, headers=c, mimetype="application/json")

    import uuid
    ref_id = str(uuid.uuid4())

    _intl_pending[ref_id] = {
        "orderId":         data.get("orderId"),
        "orderNumber":     data.get("orderNumber", ""),
        "customerName":    data.get("customerName", ""),
        "customerEmail":   data.get("customerEmail", ""),
        "items":           data.get("items", []),
        "deliveryAddress": data.get("deliveryAddress", {}),
        "nextSuffix":      data.get("nextSuffix", "-E"),
        "trackingNumber":  data.get("trackingNumber", ""),
        "carrier":         data.get("carrier", ""),
    }

    # Clean up any abandoned pending records for this order
    try:
        order_num_int = int(data.get("orderNumber", ""))
        read_token  = AIRTABLE_OPS_TOKEN or AIRTABLE_BASE_TOKEN or RETURNS_WRITE_TOKEN
        write_token = os.environ.get("AIRTABLE_WRITE_TOKEN_2", RETURNS_WRITE_TOKEN)
        search_resp = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}",
            params={
                "filterByFormula": f"AND({{Order #}}={order_num_int}, NOT({{Payment Confirmed}}))",
                "fields[]": ["Order #", "Payment Confirmed"],
                "maxRecords": 10,
            },
            headers=at_headers(read_token),
            timeout=10,
        )
        if search_resp.status_code == 200:
            stale = search_resp.json().get("records", [])
            print(f"[cs-intl-exchange-submit] Found {len(stale)} stale record(s) for order {order_num_int}")
            for rec in stale:
                del_resp = req_lib.delete(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}/{rec['id']}",
                    headers=at_headers(write_token),
                    timeout=10,
                )
                print(f"[cs-intl-exchange-submit] Delete stale {rec['id']}: {del_resp.status_code}")
        else:
            print(f"[cs-intl-exchange-submit] Stale record search failed: {search_resp.status_code}")
    except Exception as cleanup_err:
        print(f"[cs-intl-exchange-submit] cleanup error (non-fatal): {cleanup_err}")

    # Write to Airtable immediately so success handler survives redeploys
    try:
        tracking_str = (f"{data.get('trackingNumber', '')} ({data.get('carrier', '')})"
                        if data.get("carrier") else data.get("trackingNumber", ""))
        items = data.get("items", [])
        delivery_addr = data.get("deliveryAddress", {})
        items_to_exchange = "\n".join(
            f"{i.get('quantity', 1)}x {i.get('originalSku', '')} — {i.get('originalName', i.get('selectedName', ''))}"
            for i in items
        )
        desired_items = "\n".join(
            f"{i.get('quantity', 1)}x {i.get('selectedSku', '')} — {i.get('selectedName', '')}"
            for i in items
        )
        delivery_str = (
            f"{delivery_addr.get('name', '')}\n"
            f"{delivery_addr.get('street1', '')}"
            + (f"\n{delivery_addr['street2']}" if delivery_addr.get("street2") else "")
            + f"\n{delivery_addr.get('city', '')}, {delivery_addr.get('state', '')} {delivery_addr.get('postalCode', '')}\n"
            f"{delivery_addr.get('country', '')}"
        )
        at_fields = {
            "Customer Name":     data.get("customerName", ""),
            "Customer Email":    data.get("customerEmail", ""),
            "Items to Exchange": items_to_exchange,
            "Desired Items":     desired_items,
            "Delivery Address":  delivery_str,
            "Return Tracking #": tracking_str,
            "Stripe Payment ID": ref_id,
            "Original Order ID": str(data.get("orderId", "")) if data.get("orderId") else "",
            "Next Suffix":       data.get("nextSuffix", "-E"),
            "Payment Confirmed": False,
        }
        try:
            at_fields["Order #"] = int(data.get("orderNumber", ""))
        except (ValueError, TypeError):
            pass
        write_token = os.environ.get("AIRTABLE_WRITE_TOKEN_2", RETURNS_WRITE_TOKEN)
        at_resp = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": at_fields},
            timeout=15,
        )
        if at_resp.status_code in (200, 201):
            _intl_pending[ref_id]["airtableRecordId"] = at_resp.json().get("id", "")
            print(f"[cs-intl-exchange-submit] Airtable record created: {_intl_pending[ref_id]['airtableRecordId']}")
        else:
            print(f"[cs-intl-exchange-submit] Airtable write failed: {at_resp.status_code} {at_resp.text}")
    except Exception as at_err:
        print(f"[cs-intl-exchange-submit] Airtable write error: {at_err}")

    # Detect APO: US address with military state code
    delivery_addr_cs = data.get("deliveryAddress", {})
    is_apo = (
        delivery_addr_cs.get("country", "").upper() in ("US", "USA")
        and delivery_addr_cs.get("state", "").upper() in ("AA", "AE", "AP")
    )

    if is_apo:
        # APO: confirm payment immediately, send confirmation email, no Stripe
        try:
            write_token = os.environ.get("AIRTABLE_WRITE_TOKEN_2", RETURNS_WRITE_TOKEN)
            from datetime import datetime as _dt
            today_str = _dt.utcnow().strftime("%Y-%m-%d")
            apo_rec_id = _intl_pending[ref_id].get("airtableRecordId", "")
            if apo_rec_id:
                req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}/{apo_rec_id}",
                    headers={**at_headers(write_token), "Content-Type": "application/json"},
                    json={"fields": {"Payment Confirmed": True, "Date Submitted": today_str}},
                    timeout=15,
                )
        except Exception as apo_patch_err:
            print(f"[cs-intl-exchange-submit] APO patch error: {apo_patch_err}")

        cs_customer_email = data.get("customerEmail", "")
        cs_customer_name  = data.get("customerName", "")
        cs_order_number   = data.get("orderNumber", "")
        email_sent = False
        if SENDGRID_API_KEY and cs_customer_email:
            try:
                first_name = cs_customer_name.split()[0] if cs_customer_name else "there"
                email_body = (
                    f"Hi {first_name},\n\n"
                    f"Our customer service team has submitted a size exchange for your order #{cs_order_number}.\n\n"
                    f"We'll begin preparing your new belt(s) once we see movement on your shipment.\n\n"
                    f"Please allow 2-4 weeks for delivery.\n\n"
                    f"Questions? Reply to this email.\n\n"
                    f"— Blue Alpha"
                )
                send_resp = req_lib.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "personalizations": [{"to": [{"email": TEST_EMAIL_OVERRIDE or cs_customer_email}]}],
                        "from":    {"email": CS_FROM_EMAIL, "name": "Blue Alpha"},
                        "reply_to": {"email": CS_FROM_EMAIL},
                        "subject": f"Your Blue Alpha Size Exchange — Order #{cs_order_number}",
                        "content": [{"type": "text/plain", "value": email_body}],
                    },
                    timeout=15,
                )
                email_sent = send_resp.status_code in (200, 202)
            except Exception as email_err:
                print(f"[cs-intl-exchange-submit] APO email error: {email_err}")

        _intl_pending.pop(ref_id, None)
        return Response(json.dumps({"checkoutUrl": None, "emailSent": email_sent, "apo": True}),
                        headers=c, mimetype="application/json")

    # Non-APO: existing Stripe flow continues below
    success_url = f"https://exchange.bluealphabelts.com/exchange/international?success=1&ref={ref_id}"
    cancel_url  = "https://exchange.bluealphabelts.com/exchange/international?cancelled=1"

    try:
        stripe_resp = req_lib.post(
            "https://api.stripe.com/v1/checkout/sessions",
            auth=(STRIPE_SECRET_KEY, ""),
            data={
                "line_items[0][price_data][currency]":           "usd",
                "line_items[0][price_data][product_data][name]": "International Belt Exchange Fee",
                "line_items[0][price_data][unit_amount]":        "1000",
                "line_items[0][quantity]":                       "1",
                "mode":                                          "payment",
                "success_url":                                   success_url,
                "cancel_url":                                    cancel_url,
                "client_reference_id":                           ref_id,
            },
            timeout=15,
        )
        if stripe_resp.status_code not in (200, 201):
            raise Exception(f"Stripe error {stripe_resp.status_code}: {stripe_resp.text}")

        session = stripe_resp.json()
        checkout_url = session.get("url")
        if not checkout_url:
            raise Exception("No checkout URL returned from Stripe")

        print(f"[cs-intl-exchange-submit] Stripe session created for order {data.get('orderNumber')}, ref={ref_id}")

        # Auto-email the payment link to the customer
        customer_email = data.get("customerEmail", "")
        customer_name  = data.get("customerName", "")
        order_number   = data.get("orderNumber", "")
        email_sent = False
        if SENDGRID_API_KEY and customer_email:
            try:
                first_name = customer_name.split()[0] if customer_name else "there"
                email_body = (
                    f"Hi {first_name},\n\n"
                    f"Our customer service team has initiated a size exchange for your order #{order_number}.\n\n"
                    f"To complete your exchange, please pay the $10 international shipping fee using the link below:\n\n"
                    f"{checkout_url}\n\n"
                    f"This link expires in 24 hours. Once payment is received, we'll begin preparing your new belt(s) "
                    f"and ship it once we see movement on your return shipment.\n\n"
                    f"Questions? Reply to this email.\n\n"
                    f"— Blue Alpha"
                )
                send_resp = req_lib.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "personalizations": [{"to": [{"email": TEST_EMAIL_OVERRIDE or customer_email}]}],
                        "from":    {"email": CS_FROM_EMAIL, "name": "Blue Alpha"},
                        "reply_to": {"email": CS_FROM_EMAIL},
                        "subject": f"Your Blue Alpha Size Exchange Payment — Order #{order_number}",
                        "content": [{"type": "text/plain", "value": email_body}],
                    },
                    timeout=15,
                )
                email_sent = send_resp.status_code in (200, 202)
                print(f"[cs-intl-exchange-submit] Email {'sent' if email_sent else 'failed'}: {send_resp.status_code}")
            except Exception as email_err:
                print(f"[cs-intl-exchange-submit] Email error: {email_err}")

        return Response(json.dumps({"checkoutUrl": checkout_url, "emailSent": email_sent}), headers=c, mimetype="application/json")

    except Exception as e:
        import traceback
        print(f"[cs-intl-exchange-submit] ERROR: {e}\n{traceback.format_exc()}")
        _intl_pending.pop(ref_id, None)
        return Response(json.dumps({"error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/submit-lost-refund", methods=["POST", "OPTIONS"])
def submit_lost_refund():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    if not CS_ADMIN_PASSWORD or data.get("password", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"status": "unauthorized"}), status=401, headers=c, mimetype="application/json")

    order_number  = data.get("orderNumber", "").strip()
    items         = data.get("items", [])
    customer_name = data.get("customerName", "").strip()

    if not order_number or not items:
        return Response(json.dumps({"status": "error", "message": "Missing required fields"}), headers=c, mimetype="application/json")

    items_text = "\n".join(
        f"{i.get('quantity', 1)}x {i.get('sku', '')} — {i.get('name', '')}" for i in items
    )
    wc_link = f"https://www.bluealphabelts.com/wp-admin/post.php?post={order_number}&action=edit"

    fields = {
        "Order Number":                   order_number,
        "Customer Name from Shipstation": customer_name,
        "Items to Return":                items_text,
        "Reason for Return":              "[LOST ORDER] Refund requested",
        "Submission Date":                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "Status":                         "Refund Lost Order",
        "Type":                           "Lost",
        "WooCommerce Order Link":         wc_link,
    }
    fields = {k: v for k, v in fields.items() if v}

    try:
        r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
            headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
            json={"fields": fields},
            timeout=10,
        )
        if r.status_code not in (200, 201):
            return Response(json.dumps({"status": "error", "message": r.text}), status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"status": "ok"}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"status": "error", "message": str(e)}), status=500, headers=c, mimetype="application/json")

@app.route("/api/submit-shipping-refund", methods=["POST", "OPTIONS"])
def submit_shipping_refund():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    if not CS_ADMIN_PASSWORD or data.get("csPassword", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"success": False, "error": "Unauthorized"}),
                        status=401, headers=c, mimetype="application/json")

    if not RETURNS_TABLE_ID or not RETURNS_WRITE_TOKEN:
        return Response(json.dumps({"success": False, "error": "Airtable not configured"}),
                        status=500, headers=c, mimetype="application/json")

    from datetime import datetime, timezone

    order_number  = data.get("orderNumber", "").strip()
    customer_name = data.get("customerName", "").strip()
    reason        = data.get("reason", "").strip()
    cs_notes      = data.get("csNotes", "").strip()

    if not order_number or not reason:
        return Response(json.dumps({"success": False, "error": "Missing required fields"}),
                        status=400, headers=c, mimetype="application/json")

    reason_str = f"[SHIPPING REFUND] {reason}" + (f" — {cs_notes}" if cs_notes else "")
    wc_link    = f"https://www.bluealphabelts.com/wp-admin/post.php?post={order_number}&action=edit"

    fields = {
        "Order Number":                   order_number,
        "Customer Name from Shipstation": customer_name,
        "Reason for Return":              reason_str,
        "Submission Date":                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "Status":                         "UPS Shipping Needs Refund",
        "Type":                           "UPS Shipping Refund",
        "WooCommerce Order Link":         wc_link,
    }
    fields = {k: v for k, v in fields.items() if v}

    try:
        r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
            headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
            json={"fields": fields},
            timeout=10,
        )
        if r.status_code not in (200, 201):
            return Response(json.dumps({"success": False, "error": r.text}),
                            status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"success": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"success": False, "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/submit-reshipment", methods=["POST", "OPTIONS"])
def submit_reshipment():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    # Validate CS password
    if not CS_ADMIN_PASSWORD or data.get("password", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"status": "unauthorized"}), status=401, headers=c, mimetype="application/json")

    original_order_number = data.get("orderNumber", "").strip()
    items = data.get("items", [])  # [{"sku": ..., "name": ..., "quantity": ...}]
    ship_to = data.get("shipTo", {})  # edited address from CS

    if not original_order_number or not items or not ship_to:
        return Response(json.dumps({"status": "error", "message": "Missing required fields"}), headers=c, mimetype="application/json")

    try:
        from datetime import datetime as _dt
        now_str = _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # Look up original order to get customer email
        customer_email = ""
        try:
            orig_r = req_lib.get("https://ssapi.shipstation.com/orders",
                                 params={"orderNumber": original_order_number},
                                 headers=ss_headers(), timeout=10)
            orig_data = orig_r.json() if orig_r.content else {}
            orig_orders = orig_data.get("orders", [])
            if orig_orders:
                customer_email = orig_orders[0].get("customerEmail", "") or ""
        except Exception:
            pass  # Proceed without email if lookup fails

        # Determine reshipment order number (-L, -L2, -L3...)
        reship_number = None
        for suffix in ["-L"] + [f"-L{i}" for i in range(2, 20)]:
            candidate = original_order_number + suffix
            r = req_lib.get("https://ssapi.shipstation.com/orders",
                           params={"orderNumber": candidate},
                           headers=ss_headers(), timeout=10)
            try:
                r_data = r.json()
            except Exception:
                r_data = {}
            if not r_data.get("orders"):
                reship_number = candidate
                break

        if not reship_number:
            return Response(json.dumps({"status": "error", "message": "Could not determine reshipment order number"}), headers=c, mimetype="application/json")

        # Look up "Lost Item" store ID
        lost_store_id = None
        try:
            stores_r = req_lib.get("https://ssapi.shipstation.com/stores",
                                   headers=ss_headers(), timeout=10)
            stores_data = stores_r.json() if stores_r.content else []
            for store in (stores_data if isinstance(stores_data, list) else []):
                if "lost" in store.get("storeName", "").lower():
                    lost_store_id = store.get("storeId")
                    break
        except Exception:
            pass  # Proceed without store if lookup fails

        # Build ShipStation order payload
        order_payload = {
            "orderNumber": reship_number,
            "orderDate": now_str,
            "paymentDate": now_str,
            "orderStatus": "awaiting_shipment",
            "amountPaid": 0,
            "taxAmount": 0,
            "shippingAmount": 0,
            "internalNotes": f"Reshipment of order {original_order_number}",
            "customerNotes": "",
            "customerEmail": customer_email,
            "shipTo": {
                "name": ship_to.get("name", ""),
                "street1": ship_to.get("street1", ""),
                "street2": ship_to.get("street2", ""),
                "city": ship_to.get("city", ""),
                "state": ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country": ship_to.get("country", "US"),
                "phone": ship_to.get("phone", ""),
                "residential": True,
            },
            "billTo": {
                "name": ship_to.get("name", ""),
                "street1": ship_to.get("street1", ""),
                "city": ship_to.get("city", ""),
                "state": ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country": ship_to.get("country", "US"),
            },
            "items": [
                {
                    "sku": item["sku"],
                    "name": item["name"],
                    "quantity": item["quantity"],
                    "unitPrice": 0,
                }
                for item in items
            ],
            "carrierCode": "stamps_com",
            "serviceCode": "usps_ground_advantage",
            "packageCode": "package",
            "confirmation": "delivery",
            "weight": {"value": 8, "units": "ounces"},
            "dimensions": {"units": "inches", "length": 8, "width": 8, "height": 2},
            "advancedOptions": {"storeId": lost_store_id} if lost_store_id else {},
        }

        create_r = req_lib.post(
            "https://ssapi.shipstation.com/orders/createorder",
            headers={**ss_headers(), "Content-Type": "application/json"},
            json=order_payload,
            timeout=15,
        )
        try:
            result = create_r.json()
        except Exception:
            return Response(json.dumps({"status": "error", "message": f"ShipStation returned unexpected response (HTTP {create_r.status_code}): {create_r.text[:300]}"}), headers=c, mimetype="application/json")
        new_order_id = result.get("orderId")
        if not new_order_id:
            return Response(json.dumps({"status": "error", "message": f"ShipStation error: {result}"}), headers=c, mimetype="application/json")

        return Response(json.dumps({
            "status": "ok",
            "reshipOrderNumber": reship_number,
            "reshipOrderId": new_order_id,
            "ssUrl": f"https://ship11.shipstation.com/orders/all-orders-search-result?quickSearch={reship_number}",
        }), headers=c, mimetype="application/json")

    except Exception as e:
        return Response(json.dumps({"status": "error", "message": str(e)}), headers=c, mimetype="application/json")


@app.route("/api/submit-cancellation", methods=["POST", "OPTIONS"])
def submit_cancellation():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    # Validate CS password
    if not CS_ADMIN_PASSWORD or data.get("csPassword", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"success": False, "error": "Unauthorized"}),
                        status=401, headers=c, mimetype="application/json")

    if not RETURNS_TABLE_ID or not RETURNS_WRITE_TOKEN:
        return Response(json.dumps({"success": False, "error": "Airtable not configured"}),
                        status=500, headers=c, mimetype="application/json")

    from datetime import datetime, timezone

    order_number  = data.get("orderNumber", "")
    order_id      = data.get("orderId")
    customer_name = data.get("customerName", "")
    items         = data.get("items", [])   # [{sku, name, quantity}]
    reason        = data.get("reason", "")
    cs_notes      = data.get("csNotes", "").strip()

    # Block cancellation only if order has shipped OR has a tracking number.
    # If the order is already cancelled in ShipStation, always allow through.
    try:
        ss_r = req_lib.get(
            f"https://ssapi.shipstation.com/orders/{order_id}",
            headers=ss_headers(), timeout=10,
        )
        ss_status = ""
        if ss_r.status_code == 200:
            ss_status = ss_r.json().get("orderStatus", "")

        if ss_status == "cancelled":
            pass  # Always allow — already cancelled in ShipStation, just need Airtable record
        elif ss_status == "shipped":
            return Response(
                json.dumps({
                    "success": False,
                    "error": "Order cannot be cancelled — it has already shipped.",
                }),
                status=400, headers=c, mimetype="application/json",
            )
        else:
            # Check for tracking numbers on any non-voided shipment
            ship_r = req_lib.get(
                "https://ssapi.shipstation.com/shipments",
                params={"orderId": order_id},
                headers=ss_headers(), timeout=10,
            )
            if ship_r.status_code == 200:
                for s in ship_r.json().get("shipments", []):
                    tracking = (s.get("trackingNumber") or "").strip()
                    if tracking and not s.get("voided", False):
                        return Response(
                            json.dumps({
                                "success": False,
                                "error": "Order cannot be cancelled — a tracking number has already been assigned.",
                            }),
                            status=400, headers=c, mimetype="application/json",
                        )
    except Exception as e:
        print(f"[submit_cancellation] Status check failed: {e}")
        # Don't block if the check itself errors — proceed and let ShipStation handle it

    # Validate no items have already been cancelled via this form
    try:
        formula = f"AND({{Order Number}}='{order_number}',{{Type}}='Cancellation')"
        ac_r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
            params={"filterByFormula": formula, "fields[]": ["Items to Return"]},
            headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}"},
            timeout=10,
        )
        already_cancelled = {}
        for rec in ac_r.json().get("records", []):
            items_text_existing = rec.get("fields", {}).get("Items to Return", "")
            for line in items_text_existing.split("\n"):
                m = re.match(r'(\d+)x\s+(\S+)\s+[—\-]', line.strip())
                if m:
                    already_cancelled[m.group(2).strip()] = \
                        already_cancelled.get(m.group(2).strip(), 0) + int(m.group(1))
        if already_cancelled:
            dupes = []
            for item in items:
                sku = item.get("sku", "")
                qty = int(item.get("quantity", 1))
                already = already_cancelled.get(sku, 0)
                if already >= qty:
                    dupes.append(item.get("name") or sku)
            if dupes:
                return Response(
                    json.dumps({
                        "success": False,
                        "error": f"Cancellation already submitted for: {', '.join(dupes)}.",
                    }),
                    status=400, headers=c, mimetype="application/json",
                )
    except Exception as e:
        print(f"[submit_cancellation] Duplicate check failed (non-fatal): {e}")

    items_text = "\n".join(
        f"{i.get('quantity', 1)}x {i.get('sku', '')} — {i.get('name', '')}"
        for i in items
    )

    wc_link = (f"https://www.bluealphabelts.com/wp-admin/post.php"
               f"?post={order_id}&action=edit")

    reason_str = f"[CANCELLATION] {reason}" + (f" — {cs_notes}" if cs_notes else "")

    fields = {
        "Order Number":                   order_number,
        "Customer Name from Shipstation": customer_name,
        "Items to Return":                items_text,
        "Reason for Return":              reason_str,
        "Submission Date":                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "Status":                         "Cancellation Needs Refund",
        "Type":                           "Cancellation",
        "WooCommerce Order Link":         wc_link,
    }
    fields = {k: v for k, v in fields.items() if v}

    # Create Airtable record
    try:
        r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
            headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
            json={"fields": fields},
            timeout=10,
        )
        if r.status_code not in (200, 201):
            return Response(json.dumps({"success": False, "error": r.text}),
                            status=500, headers=c, mimetype="application/json")
        record_id = r.json().get("id", "")
    except Exception as e:
        return Response(json.dumps({"success": False, "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")

    # Handle Return Items creation + ShipStation cancellation in background
    def process_cancellation(record_id, order_id, items):
        # Create Return Items with Received auto-checked (items never shipped)
        for item in items:
            try:
                qty = int(item.get("quantity", 1))
                req_lib.post(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}",
                    headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}",
                             "Content-Type": "application/json"},
                    json={"fields": {
                        "Item Name":     item.get("name") or item.get("sku", ""),
                        "SKU":           item.get("sku", ""),
                        "Qty Submitted": qty,
                        "Qty Received":  qty,
                        "Received":      True,
                        "Return":        [record_id],
                    }},
                    timeout=10,
                )
            except Exception as e:
                print(f"[submit_cancellation] Return Item creation failed: {e}")

        # Cancel / update in ShipStation
        ss_success, ss_note = cancel_in_shipstation(order_id, items)
        status_notes = ss_note if not ss_success else ""

        try:
            patch_fields = {"Status Notes": (ss_note if not ss_success else "Cancelled in ShipStation")}
            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}",
                         "Content-Type": "application/json"},
                json={"fields": patch_fields},
                timeout=10,
            )
        except Exception as e:
            print(f"[submit_cancellation] Airtable status update failed: {e}")

    threading.Thread(target=process_cancellation, args=(record_id, order_id, items), daemon=True).start()

    return Response(json.dumps({"success": True, "recordId": record_id}),
                    headers=c, mimetype="application/json")


LEAF_PRODUCT_TYPES = {"Base Product", "Resell", "Made-to-Order Base Product", "x-Base Product"}

def _expand_record(record_id, qty, visited, depth):
    """Recursively expand a Product SKUs record into leaf (base/resell) items.
    Returns list of (name, sku, qty)."""
    if depth > 6 or record_id in visited:
        return []
    visited.add(record_id)
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}/{record_id}",
            headers={"Authorization": f"Bearer {AIRTABLE_OPS_TOKEN}"},
            timeout=10,
        )
        f = r.json().get("fields", {})
        name        = f.get("Name + Variations", "")
        sku         = f.get("SKU ID", "")
        ptype       = f.get("Product Type", "")
        components  = f.get("Component(s)", [])
        # x-Base Products are leaves but multiply qty by Multi-Component Qty.
        if ptype == "x-Base Product":
            multi_qty = f.get("Multi-Component Qty.", 1) or 1
            return [(name, sku, qty * int(multi_qty))]
        if ptype in LEAF_PRODUCT_TYPES or not components:
            return [(name, sku, qty)]
        result = []
        for comp_id in components:
            result.extend(_expand_record(comp_id, qty, visited, depth + 1))
        return result if result else [(name, sku, qty)]
    except Exception as e:
        print(f"[expand_record] Error on {record_id}: {e}")
        return []

def expand_sku_to_leaf_items(sku, qty):
    """Look up a SKU in Product SKUs and recursively expand combos to base/resell items.
    Returns list of (name, sku, qty). Falls back to [(sku, sku, qty)] if not found."""
    if not AIRTABLE_OPS_TOKEN:
        return [(sku, sku, qty)]
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
            params={"filterByFormula": f"{{SKU ID}}='{sku}'", "maxRecords": 1,
                    "fields[]": ["SKU ID", "Name + Variations", "Product Type", "Component(s)"]},
            headers={"Authorization": f"Bearer {AIRTABLE_OPS_TOKEN}"},
            timeout=10,
        )
        records = r.json().get("records", [])
        if not records:
            return [(sku, sku, qty)]
        rec = records[0]
        f = rec.get("fields", {})
        ptype      = f.get("Product Type", "")
        components = f.get("Component(s)", [])
        name       = f.get("Name + Variations", sku)
        if ptype == "x-Base Product":
            multi_qty = f.get("Multi-Component Qty.", 1) or 1
            return [(name, sku, qty * int(multi_qty))]
        if ptype in LEAF_PRODUCT_TYPES or not components:
            return [(name, sku, qty)]
        # Combo — expand components
        result = []
        for comp_id in components:
            result.extend(_expand_record(comp_id, qty, {rec["id"]}, 1))
        return result if result else [(name, sku, qty)]
    except Exception as e:
        print(f"[expand_sku] Error expanding {sku}: {e}")
        return [(sku, sku, qty)]

def create_return_items(return_record_id, items_to_return_text):
    """Parse submitted items text, expand combos, and create Return Items records in Airtable."""
    import re as re_lib
    if not return_record_id or not items_to_return_text:
        return
    leaf_items = []
    for line in items_to_return_text.strip().split("\n"):
        m = re_lib.match(r'(\d+)x\s+(\S+)\s+[—\-]\s*(.*)', line.strip())
        if m:
            qty  = int(m.group(1))
            sku  = m.group(2).strip()
            name = m.group(3).strip()
            expanded = expand_sku_to_leaf_items(sku, qty)
            # If expansion found nothing useful, fall back to the original item
            leaf_items.extend(expanded if expanded else [(name, sku, qty)])
        else:
            print(f"[create_return_items] Could not parse line: {line!r}")

    # Deduplicate by SKU — combine quantities if same SKU appears via multiple combo paths
    seen = {}
    for (item_name, item_sku, item_qty) in leaf_items:
        if item_sku in seen:
            seen[item_sku] = (item_name, item_sku, seen[item_sku][2] + item_qty)
        else:
            seen[item_sku] = (item_name, item_sku, item_qty)

    for (item_name, item_sku, item_qty) in seen.values():
        try:
            req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}",
                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
                json={"fields": {
                    "Item Name":     item_name or item_sku,
                    "SKU":           item_sku,
                    "Qty Submitted": item_qty,
                    "Return":        [return_record_id],
                }},
                timeout=10,
            )
        except Exception as e:
            print(f"[create_return_items] Failed to create item record for {item_sku}: {e}")


def send_return_label_email(to_email, customer_name, order_number, label_pdf_b64):
    """Send return label PDF to customer via SendGrid. Returns (success, error_message)."""
    if not SENDGRID_API_KEY:
        return False, "SendGrid not configured"
    try:
        first_name = customer_name.split()[0] if customer_name else "there"
        payload = {
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": CS_FROM_EMAIL, "name": "Blue Alpha"},
            "subject": f"Your Blue Alpha Return Label — Order #{order_number}",
            "content": [{"type": "text/plain", "value": (
                f"Hi {first_name},\n\n"
                "Your return label is attached. Print it, attach it to your package, and drop it off at any USPS location.\n\n"
                "Please note: this label will expire 30 days from today. Be sure to ship your return before then.\n\n"
                "Once we receive your return, we'll process it within 3 business days.\n\n"
                "Questions? Reply to this email and our team will help you out.\n\n"
                "— Blue Alpha"
            )}],
            "attachments": [{
                "content": label_pdf_b64,
                "type": "application/pdf",
                "filename": f"return-label-{order_number}.pdf",
                "disposition": "attachment",
            }],
        }
        r = req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        if r.status_code == 202:
            return True, None
        else:
            return False, f"SendGrid {r.status_code}: {r.text}"
    except Exception as e:
        return False, str(e)


def create_return_label(order_id, customer_addr, customer_email="", order_number=""):
    """Create a return label in ShipStation tied to the original order (no new order created).
    Returns (tracking_number, label_pdf_b64) or raises Exception.
    label_pdf_b64 is the base64-encoded PDF from ShipStation (may be empty string if not returned)."""
    from datetime import datetime, timezone

    # Always use USPS Ground Advantage for return labels regardless of original shipment method
    carrier = "stamps_com"
    service = "usps_ground_advantage"
    weight  = {"value": 16, "units": "ounces"}
    # Still fetch original shipment to inherit weight only
    sr = req_lib.get("https://ssapi.shipstation.com/shipments",
                     params={"orderId": order_id},
                     headers=ss_headers(), timeout=10)
    ships = sr.json().get("shipments", [])
    outbound = [s for s in ships if not s.get("isReturnLabel", False)]
    for s in (outbound or ships):
        w = s.get("weight") or {}
        if w.get("value", 0) > 0:
            weight = w
        break

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    r = req_lib.post(
        "https://ssapi.shipstation.com/shipments/createlabel",
        headers={**ss_headers(), "Content-Type": "application/json"},
        json={
            "orderId":      int(order_id) if order_id else None,
            "carrierCode":  carrier,
            "serviceCode":  service,
            "packageCode":  "package",
            "shipDate":     today,
            "weight":       weight,
            "shipFrom": {
                "name":       customer_addr.get("name", ""),
                "street1":    customer_addr.get("street1", ""),
                "street2":    customer_addr.get("street2", ""),
                "city":       customer_addr.get("city", ""),
                "state":      customer_addr.get("state", ""),
                "postalCode": customer_addr.get("postalCode", ""),
                "country":    "US",
                "phone":      customer_addr.get("phone", ""),
            },
            "shipTo": {
                "name":       "Blue Alpha",
                "company":    "Blue Alpha",
                "street1":    "35 Andrew St",
                "city":       "Newnan",
                "state":      "GA",
                "postalCode": "30263",
                "country":    "US",
                "phone":      "6789822442",
            },
            "isReturnLabel": True,
            "testLabel":     False,
        },
        timeout=20,
    )
    result = r.json()
    if r.status_code not in (200, 201):
        raise Exception(f"ShipStation {r.status_code}: {result}")

    tracking   = result.get("trackingNumber", "")
    label_pdf  = result.get("labelData", "")   # base64-encoded PDF
    return tracking, label_pdf


@app.route("/api/submit-return", methods=["POST", "OPTIONS"])
def submit_return():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    # If this is a CS exception submission, validate the CS password
    if data.get("csException"):
        if not CS_ADMIN_PASSWORD or data.get("csPassword", "") != CS_ADMIN_PASSWORD:
            return Response(json.dumps({"success": False, "error": "Unauthorized"}),
                            status=401, headers=c, mimetype="application/json")

    if not RETURNS_TABLE_ID or not RETURNS_WRITE_TOKEN:
        return Response(json.dumps({"success": False, "error": "Airtable not configured"}),
                        status=500, headers=c, mimetype="application/json")

    from datetime import datetime, timezone

    addr = data.get("address", {})
    address_str = ", ".join(filter(None, [
        addr.get("name"), addr.get("street1"), addr.get("street2"),
        addr.get("city"), addr.get("state"), addr.get("postalCode")
    ]))
    wc_link = (f"https://www.bluealphabelts.com/wp-admin/post.php"
               f"?post={data.get('orderId', '')}&action=edit")

    # ── Create Airtable record immediately (Status: "New") ───────────────
    # Build reason string — CS exceptions get a visible prefix
    reason_for_return = data.get("reasonForReturn", "")
    if data.get("csException"):
        cs_notes = data.get("csNotes", "").strip()
        reason_for_return = "[CS Exception] " + reason_for_return + (f" — Notes: {cs_notes}" if cs_notes else "")

    fields = {
        "Order Number":                   data.get("orderNumber", ""),
        "Customer Name from Shipstation": data.get("customerName", ""),
        "Email Address":                  data.get("email", ""),
        "Phone Number":                   data.get("phone", ""),
        "Confirmed Shipping Address":     address_str,
        "Items to Return":                data.get("itemsToReturn", ""),
        "Reason for Return":              reason_for_return,
        "Submission Date":                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "Ship Date from Shipstation":     data.get("shipDate", "")[:10] if data.get("shipDate") else "",
        "Eligible Until":                 data.get("eligibleUntil", "")[:10] if data.get("eligibleUntil") else "",
        "Status":                         "New",
        "Type":                           "Return",
        "WooCommerce Order Link":         wc_link,
    }
    fields = {k: v for k, v in fields.items() if v}

    import re as _re_ret
    read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

    # ── Server-side: filter out items already covered by an active return ─
    # Mirrors the alreadyReturnedQtys check in verify-order so the server
    # enforces the same rule even if the client sends stale/manipulated data.
    # CS exceptions bypass this check (CS may legitimately override).
    if data.get("orderNumber") and not data.get("csException") and RETURNS_TABLE_ID and read_token:
        try:
            _ar_formula = (f"AND({{Order Number}}='{data.get('orderNumber','')}'"
                           f",OR({{Status}}='New',{{Status}}='Label Sent',"
                           f"{{Status}}='Needs Review',"
                           f"{{Status}}='Items Received',{{Status}}='Partial Received',"
                           f"{{Status}}='Refunded'),{{Type}}='Return')")
            _ar_resp = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                params={"filterByFormula": _ar_formula, "fields[]": ["Items to Return"], "maxRecords": 20},
                headers={"Authorization": f"Bearer {read_token}"},
                timeout=10,
            )
            _already = {}
            for _rec in _ar_resp.json().get("records", []):
                for _line in _rec.get("fields", {}).get("Items to Return", "").split("\n"):
                    _m = _re_ret.match(r'(\d+)x\s+(\S+)\s+[—\-]', _line.strip())
                    if _m:
                        _sku = _m.group(2).strip()
                        _already[_sku] = _already.get(_sku, 0) + int(_m.group(1))
            if _already:
                _filtered = []
                for _line in data.get("itemsToReturn", "").strip().split("\n"):
                    _m = _re_ret.match(r'(\d+)x\s+(\S+)\s+[—\-](.*)', _line.strip())
                    if _m:
                        _qty  = int(_m.group(1))
                        _sku  = _m.group(2).strip()
                        _rest = _m.group(3)
                        _remaining = _qty - _already.get(_sku, 0)
                        if _remaining > 0:
                            _filtered.append(f"{_remaining}x {_sku} —{_rest}")
                    else:
                        if _line.strip():
                            _filtered.append(_line)
                if not _filtered:
                    return Response(json.dumps({"success": False,
                        "error": "A return has already been submitted for these items."}),
                        headers=c, mimetype="application/json")
                data = dict(data)
                data["itemsToReturn"] = "\n".join(_filtered)
                fields["Items to Return"] = data["itemsToReturn"]
        except Exception as _ar_err:
            print(f"[submit-return] Could not check already-returned items: {_ar_err}")

    # ── Reuse existing "Needs Review" or in-flight "New" record ──────────
    # Prevents duplicate records when label generation fails and the customer
    # retries, or when two requests race in nearly simultaneously.
    existing_nr_ids = []
    record_id = None
    if data.get("orderNumber"):
        try:
            nr_formula = (f"AND({{Order Number}}='{data.get('orderNumber','')}'"
                          f",OR({{Status}}='Needs Review',{{Status}}='New'),{{Type}}='Return')")
            nr_resp = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                params={"filterByFormula": nr_formula, "fields[]": ["Status"], "maxRecords": 10},
                headers={"Authorization": f"Bearer {read_token}"},
                timeout=10,
            )
            existing_nr_ids = [r["id"] for r in nr_resp.json().get("records", [])]
        except Exception as nr_err:
            print(f"[submit-return] Could not check existing Needs Review records: {nr_err}")

    try:
        if existing_nr_ids:
            # Reuse the first existing record; delete any extras
            record_id = existing_nr_ids[0]
            patch_fields = {**fields, "Status": "New", "Status Notes": ""}
            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
                json={"fields": patch_fields},
                timeout=10,
            )
            for stale_id in existing_nr_ids[1:]:
                try:
                    req_lib.delete(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{stale_id}",
                        headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}"},
                        timeout=10,
                    )
                except Exception:
                    pass
        else:
            r = req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
                json={"fields": fields},
                timeout=10,
            )
            if r.status_code not in (200, 201):
                return Response(json.dumps({"success": False, "error": r.text}),
                                status=500, headers=c, mimetype="application/json")
            record_id = r.json().get("id", "")
    except Exception as e:
        return Response(json.dumps({"success": False, "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")

    # ── Kick off label generation + email in background ──────────────────
    def process_label(record_id, data, addr):
        try:
            # ── Create Return Items (combo-expanded checklist for CS) ─────────
            # Use a per-record lock to prevent the race condition where two retry
            # threads both pass the _has_items check before either creates records.
            with _get_return_items_lock(record_id):
                _has_items = False
                try:
                    _ri_check = req_lib.get(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
                        params={"fields[]": ["Return Items"]},
                        headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}"},
                        timeout=10,
                    )
                    _has_items = len(_ri_check.json().get("fields", {}).get("Return Items", [])) > 0
                except Exception:
                    pass
                if not _has_items:
                    create_return_items(record_id, data.get("itemsToReturn", ""))

            addr_for_label = {
                "name":       addr.get("name", data.get("customerName", "")),
                "street1":    addr.get("street1", ""),
                "street2":    addr.get("street2", ""),
                "city":       addr.get("city", ""),
                "state":      addr.get("state", ""),
                "postalCode": addr.get("postalCode", ""),
                "phone":      data.get("phone", ""),
            }
            tracking_number, label_pdf_b64 = create_return_label(
                data.get("orderId"),
                addr_for_label,
                customer_email=data.get("email", ""),
                order_number=data.get("orderNumber", ""),
            )
            if not label_pdf_b64:
                raise Exception("Label generated but no PDF data returned by ShipStation")

            # Store tracking + PDF, build download URL
            label_download_url = f"{FLASK_BASE_URL}/api/return-label/{record_id}"
            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
                json={"fields": {
                    "Return Tracking #": tracking_number,
                    "Label PDF Data":    label_pdf_b64,
                    "Return Label URL":  label_download_url,
                }},
                timeout=10,
            )

            # Send email
            customer_email = data.get("email", "")
            status_update = {"Status": "Needs Review", "Status Notes": "No customer email on file"}
            if customer_email:
                email_sent, email_error = send_return_label_email(
                    to_email=customer_email,
                    customer_name=data.get("customerName", ""),
                    order_number=data.get("orderNumber", ""),
                    label_pdf_b64=label_pdf_b64,
                )
                if email_sent:
                    status_update = {"Status": "Label Sent"}
                else:
                    status_update = {"Status": "Needs Review",
                                     "Status Notes": f"Label generated but email failed: {email_error}"}
                    print(f"[process_label] Email failed: {email_error}")

            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
                json={"fields": status_update},
                timeout=10,
            )
            # Cache the full response payload so the poll endpoint can include label URL
            cached_payload = {"status": status_update.get("Status", "Needs Review")}
            if status_update.get("Status") == "Needs Review" and label_pdf_b64:
                # Label was generated but email failed — surface download URL
                cached_payload["labelUrl"] = label_download_url
            _return_status_cache[record_id] = cached_payload

            # ── On success, delete any other stale "Needs Review" records for this order ──
            if status_update.get("Status") == "Label Sent":
                try:
                    order_num = data.get("orderNumber", "")
                    stale_formula = (f"AND({{Order Number}}='{order_num}'"
                                     f",{{Status}}='Needs Review',{{Type}}='Return')")
                    stale_resp = req_lib.get(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                        params={"filterByFormula": stale_formula, "fields[]": ["Status"], "maxRecords": 10},
                        headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}"},
                        timeout=10,
                    )
                    for stale in stale_resp.json().get("records", []):
                        if stale["id"] != record_id:
                            req_lib.delete(
                                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{stale['id']}",
                                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}"},
                                timeout=10,
                            )
                except Exception as cleanup_err:
                    print(f"[process_label] Stale record cleanup failed: {cleanup_err}")
        except Exception as e:
            print(f"[process_label] Failed for record {record_id}: {e}")
            _return_status_cache[record_id] = "Needs Review"
            try:
                req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
                    headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
                    json={"fields": {"Status": "Needs Review", "Status Notes": f"Label generation failed: {e}"}},
                    timeout=10,
                )
            except Exception:
                pass

    threading.Thread(target=process_label, args=(record_id, data, addr), daemon=True).start()

    # ── Respond immediately — label + email handled in background ─────────
    return Response(json.dumps({"success": True, "recordId": record_id}), headers=c, mimetype="application/json")


@app.route("/api/return-status/<record_id>")
def return_status(record_id):
    """Poll for the current status of a return record.
    Reads from in-memory cache first; falls back to Airtable so app restarts
    don't cause a permanently stuck 'New' status."""
    cached = _return_status_cache.get(record_id)
    if cached:
        # Cache may be a dict (new format) or a plain status string (legacy/exception path)
        if isinstance(cached, dict):
            return Response(json.dumps(cached), headers=cors(), mimetype="application/json")
        return Response(json.dumps({"status": cached}), headers=cors(), mimetype="application/json")
    # Not in cache — check Airtable directly (handles post-restart scenarios)
    try:
        read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        at_resp = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
            params={"fields[]": ["Status", "Status Notes", "Label PDF Data", "Return Label URL"]},
            headers={"Authorization": f"Bearer {read_token}"},
            timeout=8,
        )
        at_fields = at_resp.json().get("fields", {})
        at_status = at_fields.get("Status", "New")
        # Populate cache so subsequent polls are fast
        _return_status_cache[record_id] = at_status
        payload = {"status": at_status}
        # If label was generated but email failed, surface the download URL to the frontend
        if at_fields.get("Label PDF Data") and at_fields.get("Return Label URL"):
            payload["labelUrl"] = at_fields["Return Label URL"]
        if at_fields.get("Status Notes"):
            payload["statusNotes"] = at_fields["Status Notes"]
        return Response(json.dumps(payload), headers=cors(), mimetype="application/json")
    except Exception:
        pass
    # Airtable unreachable — tell client to keep polling
    return Response(json.dumps({"status": "New"}), headers=cors(), mimetype="application/json")


def _create_inventory_adjustment_for_return(sku_text, qty, read_token, write_token):
    """Look up Product SKU record by SKU ID and create an Inventory Adjustment for a return receipt.
    Returns (success: bool, message: str).
    NOTE: 'Return Received' must exist as an option in the Adjustment Reason field in Airtable."""
    try:
        # Escape \ and " so SKUs like 1182-BLK-1/8-20"-BASE don't break the formula string
        sku_escaped = str(sku_text).replace("\\", "\\\\").replace('"', '\\"')
        # Look up the Product SKU record ID and current Finished Inventory
        sku_recs = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
            params={"filterByFormula": f'{{SKU ID}}="{sku_escaped}"', "maxRecords": 1,
                    "fields[]": ["SKU ID", "Name + Variations", "Finished Inventory"]},
            headers=at_headers(read_token),
            timeout=10,
        ).json().get("records", [])
        if not sku_recs:
            return False, f"SKU '{sku_text}' not found in Product SKUs"
        sku_record_id    = sku_recs[0]["id"]
        sku_name         = sku_recs[0]["fields"].get("Name + Variations", sku_text)
        current_inv      = int(sku_recs[0]["fields"].get("Finished Inventory") or 0)
        new_inv          = current_inv + int(qty)

        # Create the Inventory Adjustment record (audit trail)
        adj_fields = {
            "Adjustment Reason":  "Return Received",
            "Amount":             int(qty),
            "Product SKU":        [sku_record_id],
            "Ready":              True,
            "Adjusted":           True,
        }
        r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INVENTORY_ADJUSTMENTS_TABLE_ID}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": adj_fields},
            timeout=10,
        )
        if r.status_code not in (200, 201):
            return False, f"Airtable error {r.status_code}: {r.text[:200]}"

        # Directly increment Finished Inventory on the Product SKU record
        upd = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}/{sku_record_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {"Finished Inventory": new_inv}},
            timeout=10,
        )
        if upd.status_code not in (200, 201):
            return False, f"Adjustment created but inventory update failed ({upd.status_code}): {upd.text[:200]}"

        return True, f"Adjustment created for {sku_name}: {current_inv} → {new_inv} (+{qty})"
    except Exception as e:
        return False, str(e)


@app.route("/api/mark-all-received/<record_id>")
def mark_all_received(record_id):
    """Mark all Return Items as Received (Qty Received = Qty Submitted), create inventory
    adjustments for each, and update the parent Returns status to 'Items Received'."""
    if not RETURN_ITEMS_TABLE_ID or not AIRTABLE_OPS_TOKEN:
        return Response("<h2>Not configured</h2>", status=500, mimetype="text/html")
    try:
        read_token  = AIRTABLE_OPS_TOKEN
        write_token = RETURNS_WRITE_TOKEN

        # Fetch the return record to get linked Return Items record IDs
        ret_r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
            headers={"Authorization": f"Bearer {read_token}"},
            timeout=10,
        )
        item_ids = ret_r.json().get("fields", {}).get("Return Items", [])
        if not item_ids:
            return Response("<h2 style='font-family:sans-serif'>No items found for this return.</h2>",
                            status=404, mimetype="text/html")

        # Fetch each Return Item
        items = []
        for item_id in item_ids:
            ir = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}/{item_id}",
                headers={"Authorization": f"Bearer {read_token}"},
                timeout=10,
            )
            items.append({"id": item_id, "fields": ir.json().get("fields", {})})

        # Mark each item received
        updated = []
        for item in items:
            item_id       = item["id"]
            fields        = item.get("fields", {})
            qty_submitted = int(fields.get("Qty Submitted", 1) or 1)
            item_name     = fields.get("Item Name", item_id)

            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}/{item_id}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": {"Received": True, "Qty Received": qty_submitted}},
                timeout=10,
            )
            updated.append(item_name)

        html = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>body{{font-family:sans-serif;max-width:500px;margin:60px auto;text-align:center}}
h2{{color:#2d7a2d}}ul{{text-align:left;display:inline-block}}
p{{color:#555;margin-top:20px}}</style></head><body>
<h2>✓ All Items Marked as Received</h2>
<ul>{items}</ul>
<p>You can close this tab and refresh the interface.</p>
</body></html>""".format(items="".join(f"<li>{name}</li>" for name in updated))
        return Response(html, mimetype="text/html")
    except Exception as e:
        return Response(f"<h2>Error: {e}</h2>", status=500, mimetype="text/html")


@app.route("/api/return-received-webhook/<record_id>", methods=["GET", "POST"])
def return_received_webhook(record_id):
    """Called by an Airtable automation when a Returns record status changes to
    'Items Received' or 'Partial Received'. Finds all Return Items where
    Received=True and Qty Received > 0, then creates Inventory Adjustment records."""
    read_token  = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = RETURNS_WRITE_TOKEN
    results = []

    try:
        # Fetch the Return record to get linked Return Items
        ret_r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token),
            timeout=10,
        )
        if ret_r.status_code != 200:
            return Response(json.dumps({"ok": False, "error": "Return record not found"}),
                            status=404, mimetype="application/json")

        ret_fields = ret_r.json().get("fields", {})
        item_ids   = ret_fields.get("Return Items", [])
        status     = ret_fields.get("Status", "")

        if status not in ("Items Received", "Partial Received"):
            return Response(json.dumps({"ok": False,
                "error": f"Status '{status}' does not require inventory adjustment"}),
                mimetype="application/json")

        if not item_ids:
            return Response(json.dumps({"ok": True, "message": "No Return Items linked", "adjustments": []}),
                            mimetype="application/json")

        # Fetch each Return Item, process only those marked Received with a Qty
        for item_id in item_ids:
            ir = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}/{item_id}",
                headers=at_headers(read_token),
                timeout=10,
            )
            if ir.status_code != 200:
                continue
            f        = ir.json().get("fields", {})
            received = f.get("Received", False)
            qty      = int(f.get("Qty Received") or 0)
            sku      = (f.get("SKU") or "").strip()
            name     = f.get("Item Name", sku)

            if not received or qty <= 0 or not sku:
                results.append({"item": name, "ok": False,
                    "reason": "Skipped — not received, zero qty, or no SKU"})
                continue

            ok, msg = _create_inventory_adjustment_for_return(sku, qty, read_token, write_token)
            results.append({"item": name, "sku": sku, "qty": qty, "ok": ok, "message": msg})
            print(f"[return-received-webhook] {name}: {msg}")

        return Response(json.dumps({"ok": True, "record_id": record_id,
                                     "status": status, "adjustments": results}),
                        mimetype="application/json")

    except Exception as e:
        print(f"[return-received-webhook] Error for {record_id}: {e}")
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype="application/json")


@app.route("/api/cron-return-inventory", methods=["GET", "POST"])
def cron_return_inventory():
    """Cron endpoint — runs at 1am daily.
    Finds all Return Items where Received=True AND Inventory Added is not checked,
    creates an Inventory Adjustment for each, then marks Inventory Added=True."""
    read_token  = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = RETURNS_WRITE_TOKEN
    results = []

    try:
        # Fetch all received Return Items that haven't been added to inventory yet
        filter_formula = "AND({Received}=TRUE(), NOT({Inventory Added}=TRUE()), {Qty Received}>0)"
        all_items = []
        offset = None
        while True:
            params = {
                "filterByFormula": filter_formula,
                "fields[]": ["Item Name", "SKU", "Qty Received"],
            }
            if offset:
                params["offset"] = offset
            r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}",
                headers=at_headers(read_token),
                params=params,
                timeout=15,
            )
            data = r.json()
            all_items.extend(data.get("records", []))
            offset = data.get("offset")
            if not offset:
                break

        if not all_items:
            print("[cron-return-inventory] No pending items found.")
            return Response(json.dumps({"ok": True, "message": "No pending items", "adjustments": []}),
                            mimetype="application/json")

        for item in all_items:
            item_id = item["id"]
            f       = item.get("fields", {})
            sku     = (f.get("SKU") or "").strip()
            name    = f.get("Item Name", sku)
            qty     = int(f.get("Qty Received") or 0)

            if not sku or qty <= 0:
                results.append({"item": name, "ok": False, "reason": "No SKU or zero qty"})
                continue

            ok, msg = _create_inventory_adjustment_for_return(sku, qty, read_token, write_token)
            results.append({"item": name, "sku": sku, "qty": qty, "ok": ok, "message": msg})
            print(f"[cron-return-inventory] {name}: {msg}")

            if ok:
                try:
                    req_lib.patch(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}/{item_id}",
                        headers={**at_headers(write_token), "Content-Type": "application/json"},
                        json={"fields": {"Inventory Added": True}},
                        timeout=10,
                    )
                except Exception as mark_err:
                    print(f"[cron-return-inventory] Could not mark Inventory Added for {item_id}: {mark_err}")

        return Response(json.dumps({"ok": True, "processed": len(results), "adjustments": results}),
                        mimetype="application/json")

    except Exception as e:
        print(f"[cron-return-inventory] Error: {e}")
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype="application/json")


@app.route("/api/refunded-webhook/<record_id>", methods=["GET", "POST"])
def refunded_webhook(record_id):
    """Called by an Airtable automation when a Returns record status changes to 'Refunded'.
    Looks up all linked Return Items and creates an Inventory Adjustment (+qty) for each
    item that has a SKU and Qty Submitted > 0.  Skips items already adjusted (Inventory Added = True)
    and marks each processed item with Inventory Added = True to prevent double-counting."""
    read_token  = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = RETURNS_WRITE_TOKEN
    results = []

    try:
        # Fetch the Return record
        ret_r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token),
            timeout=10,
        )
        if ret_r.status_code != 200:
            return Response(json.dumps({"ok": False, "error": "Return record not found"}),
                            status=404, mimetype="application/json")

        ret_fields = ret_r.json().get("fields", {})
        item_ids   = ret_fields.get("Return Items", [])
        status     = ret_fields.get("Status", "")

        if status != "Refunded":
            return Response(json.dumps({"ok": False,
                "error": f"Status is '{status}', not 'Refunded' — skipping"}),
                mimetype="application/json")

        if not item_ids:
            return Response(json.dumps({"ok": True, "message": "No Return Items linked", "adjustments": []}),
                            mimetype="application/json")

        # Process each Return Item
        for item_id in item_ids:
            ir = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}/{item_id}",
                headers=at_headers(read_token),
                timeout=10,
            )
            if ir.status_code != 200:
                continue
            f    = ir.json().get("fields", {})
            sku  = (f.get("SKU") or "").strip()
            name = f.get("Item Name", sku)
            qty  = int(f.get("Qty Submitted") or 0)

            # Skip if already adjusted (idempotency guard)
            if f.get("Inventory Added"):
                results.append({"item": name, "sku": sku, "ok": True,
                    "message": "Skipped — already added to inventory"})
                continue

            if not sku or qty <= 0:
                results.append({"item": name, "ok": False,
                    "reason": "Skipped — no SKU or zero qty"})
                continue

            ok, msg = _create_inventory_adjustment_for_return(sku, qty, read_token, write_token)
            results.append({"item": name, "sku": sku, "qty": qty, "ok": ok, "message": msg})
            print(f"[refunded-webhook] {name}: {msg}")

            # Mark item so we don't double-count if webhook fires again
            if ok:
                try:
                    req_lib.patch(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}/{item_id}",
                        headers={**at_headers(write_token), "Content-Type": "application/json"},
                        json={"fields": {"Inventory Added": True}},
                        timeout=10,
                    )
                except Exception as mark_err:
                    print(f"[refunded-webhook] Could not mark Inventory Added for {item_id}: {mark_err}")

        return Response(json.dumps({"ok": True, "record_id": record_id,
                                     "status": status, "adjustments": results}),
                        mimetype="application/json")

    except Exception as e:
        print(f"[refunded-webhook] Error for {record_id}: {e}")
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype="application/json")


@app.route("/api/return-label/<record_id>")
def return_label_pdf(record_id):
    """Serve a return label PDF from the base64 data stored in Airtable."""
    if not RETURNS_TABLE_ID or not RETURNS_WRITE_TOKEN:
        return Response("Not configured", status=500)
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
            headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}"},
            timeout=10,
        )
        if r.status_code != 200:
            return Response("Label not found", status=404)
        pdf_b64 = r.json().get("fields", {}).get("Label PDF Data", "")
        if not pdf_b64:
            return Response("No label PDF on file — please contact support", status=404)
        pdf_bytes = base64.b64decode(pdf_b64)
        order_num = r.json().get("fields", {}).get("Order Number", record_id)
        filename = f"return-label-{order_num}.pdf"
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        return Response(f"Error: {e}", status=500)


_ONTIME_CACHE = {"ts": 0, "data": None}  # reset on every deploy
_ONTIME_REFRESHING = False
_ONTIME_LAST_ERROR = {"msg": None, "ts": 0}

_ONTIME_CONTRACT = 137893
_ONTIME_EXCLUDE_TAGS = {137893, 49845}   # Contract + Expedite — excluded from on-time calc
_ONTIME_RULES = [
    ({109623, 137358, 105813},                  5,  True),
    ({137359, 68484, 119374, 123911, 102014},  10, False),
    ({55571},                                  14, False),
    ({139291},                                 17, False),
]


def _business_days(start, end):
    from datetime import timedelta
    days = 0
    d = start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def _refresh_ontime_cache():
    """Run in a background thread — fetches ShipStation data and populates _ONTIME_CACHE."""
    global _ONTIME_REFRESHING
    import time as _time
    from datetime import datetime, timedelta
    try:
        if not SHIPSTATION_KEY or not SHIPSTATION_SECRET:
            raise RuntimeError("SHIPSTATION_KEY or SHIPSTATION_SECRET not configured")
        thirty_days_ago   = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        fourteen_days_ago = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%d")
        today_date        = datetime.utcnow().date()

        # ── Shared helpers ──────────────────────────────────────────────────────
        def _get_sla(order):
            """Return (sla_days, use_business_days) or (None, None) to skip."""
            tag_ids = set(order.get("tagIds") or [])
            if tag_ids & _ONTIME_EXCLUDE_TAGS:
                return None, None
            store_id = (order.get("advancedOptions") or {}).get("storeId")
            if store_id == SIZING_EXCHANGE_STORE_ID:
                return 3, True
            sla, biz = None, False
            for rule_tags, days, use_biz in _ONTIME_RULES:
                if tag_ids & rule_tags:
                    if sla is None or days > sla:
                        sla, biz = days, use_biz
            return sla, biz

        def _fetch_ss(status, create_start, max_pages=40):
            results = []
            for pg in range(1, max_pages + 1):
                if pg > 1:
                    _time.sleep(1.5)
                resp = req_lib.get("https://ssapi.shipstation.com/orders",
                    params={"orderStatus": status, "createDateStart": create_start,
                            "pageSize": 500, "page": pg},
                    headers=ss_headers(), timeout=30)
                if resp.status_code == 429:
                    _time.sleep(int(resp.headers.get("Retry-After", 60)))
                    continue
                resp.raise_for_status()
                body = resp.json()
                results.extend(body.get("orders", []))
                print(f"[on-time] {status}/{create_start} page {pg}/{body.get('pages',1)} ({len(results)} so far)")
                if pg >= body.get("pages", 1):
                    break
            return results

        def _score_window(shipped_orders, awaiting_orders):
            """Return (on_time, total, overdue) for a set of shipped + awaiting orders."""
            on_time = total = overdue = 0
            for order in shipped_orders:
                sla, biz = _get_sla(order)
                if sla is None:
                    continue
                create_str = order.get("createDate") or ""
                ship_str   = order.get("shipDate") or ""
                try:
                    create_d = datetime.strptime(create_str[:10], "%Y-%m-%d").date()
                    ship_d   = datetime.strptime(ship_str[:10],   "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    continue
                age = _business_days(create_d, ship_d) if biz else (ship_d - create_d).days
                total += 1
                if age < sla:
                    on_time += 1
            for order in awaiting_orders:
                sla, biz = _get_sla(order)
                if sla is None:
                    continue
                create_str = order.get("createDate") or ""
                try:
                    create_d = datetime.strptime(create_str[:10], "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    continue
                age = _business_days(create_d, today_date) if biz else (today_date - create_d).days
                if age >= sla:          # past deadline → count as late
                    total += 1
                    overdue += 1
                # still within window → exclude (not yet due)
            return on_time, total, overdue

        # ── 30-day fetch ────────────────────────────────────────────────────────
        shipped_30d  = _fetch_ss("shipped",            thirty_days_ago, max_pages=40)
        _time.sleep(1.5)
        awaiting_30d = _fetch_ss("awaiting_shipment",  thirty_days_ago, max_pages=10)

        on_time_30, total_30, overdue_30 = _score_window(shipped_30d, awaiting_30d)
        pct_30 = round(on_time_30 * 100 / total_30) if total_30 else 0
        print(f"[on-time] 30d — {pct_30}% ({on_time_30}/{total_30}), overdue: {overdue_30}")

        # ── 14-day fetch ────────────────────────────────────────────────────────
        # Filter the already-fetched lists rather than hitting ShipStation again
        shipped_14d  = [o for o in shipped_30d
                        if (o.get("createDate") or "")[:10] >= fourteen_days_ago]
        awaiting_14d = [o for o in awaiting_30d
                        if (o.get("createDate") or "")[:10] >= fourteen_days_ago]

        on_time_14, total_14, overdue_14 = _score_window(shipped_14d, awaiting_14d)
        pct_14 = round(on_time_14 * 100 / total_14) if total_14 else 0
        print(f"[on-time] 14d — {pct_14}% ({on_time_14}/{total_14}), overdue: {overdue_14}")

        _ONTIME_CACHE["data"] = {
            "percent":    pct_30,    "onTime":    on_time_30,  "total":    total_30,  "overdue":    overdue_30,
            "percent14d": pct_14,    "onTime14d": on_time_14,  "total14d": total_14,  "overdue14d": overdue_14,
        }
        _ONTIME_CACHE["ts"] = _time.time()
    except Exception as e:
        import time as _time
        _ONTIME_LAST_ERROR["msg"] = str(e)
        _ONTIME_LAST_ERROR["ts"]  = _time.time()
        print(f"[on-time] background refresh failed: {e}")
    finally:
        _ONTIME_REFRESHING = False


@app.route("/api/on-time-debug-sync")
def on_time_debug_sync():
    """Temporary: synchronous mini-fetch to inspect raw shipDate values."""
    from datetime import datetime, timedelta
    import time as _time
    cors_headers = {"Access-Control-Allow-Origin": "*"}
    try:
        seven_days_ago_date = (datetime.utcnow() - timedelta(days=7)).date()
        thirty_days_ago = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        r = req_lib.get(
            "https://ssapi.shipstation.com/orders",
            params={"orderStatus": "shipped", "shipDateStart": thirty_days_ago,
                    "pageSize": 10, "page": 1},
            headers=ss_headers(), timeout=30,
        )
        r.raise_for_status()
        orders = r.json().get("orders", [])
        samples = [{"shipDate": o.get("shipDate"), "createDate": o.get("createDate"),
                    "tagIds": o.get("tagIds")} for o in orders]
        in_7d = sum(1 for o in orders if (o.get("shipDate") or "")[:10] >= str(seven_days_ago_date))
        return Response(json.dumps({
            "seven_days_ago_date": str(seven_days_ago_date),
            "thirty_days_ago": thirty_days_ago,
            "page1_count": len(orders),
            "page1_in_7d": in_7d,
            "samples": samples,
        }), headers=cors_headers, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), headers=cors_headers, mimetype="application/json")


@app.route("/api/on-time-shipments")
def on_time_shipments():
    import time as _time
    import threading
    global _ONTIME_REFRESHING
    cors_headers = {"Access-Control-Allow-Origin": "*"}

    now = _time.time()
    cache_fresh = _ONTIME_CACHE["data"] is not None and now - _ONTIME_CACHE["ts"] < 90000

    if cache_fresh:
        return Response(json.dumps(_ONTIME_CACHE["data"]), headers=cors_headers,
                        mimetype="application/json")

    # If last error was recent (within 2 min), back off — don't hammer ShipStation
    error_recent = now - _ONTIME_LAST_ERROR["ts"] < 120

    # Kick off background refresh if not already running and not in backoff
    if not _ONTIME_REFRESHING and not error_recent:
        _ONTIME_REFRESHING = True
        threading.Thread(target=_refresh_ontime_cache, daemon=True).start()

    # Return stale data while refreshing
    if _ONTIME_CACHE["data"] is not None:
        return Response(json.dumps({**_ONTIME_CACHE["data"], "stale": True}),
                        headers=cors_headers, mimetype="application/json")

    # No data yet — report error or pending
    if error_recent and _ONTIME_LAST_ERROR["msg"]:
        return Response(json.dumps({"error": _ONTIME_LAST_ERROR["msg"]}),
                        headers=cors_headers, mimetype="application/json")
    return Response(json.dumps({"pending": True}),
                    headers=cors_headers, mimetype="application/json")


@app.route("/api/on-time-shipments/debug")
def on_time_debug():
    import time as _t
    cors_headers = {"Access-Control-Allow-Origin": "*"}
    return Response(json.dumps({
        "cache_data":      _ONTIME_CACHE["data"],
        "cache_age_secs":  round(_t.time() - _ONTIME_CACHE["ts"], 1),
        "refreshing":      _ONTIME_REFRESHING,
        "last_error":      _ONTIME_LAST_ERROR["msg"],
        "error_age_secs":  round(_t.time() - _ONTIME_LAST_ERROR["ts"], 1),
        "ss_key_set":      bool(SHIPSTATION_KEY),
        "ss_secret_set":   bool(SHIPSTATION_SECRET),
    }), headers=cors_headers, mimetype="application/json")


_AWAITING_CACHE = {"ts": 0, "data": None}

@app.route("/api/awaiting")
def awaiting_shipment():
    import time as _time
    cors_headers = {"Access-Control-Allow-Origin": "*"}
    if not SHIPSTATION_KEY or not SHIPSTATION_SECRET:
        return Response(json.dumps({"error": "ShipStation not configured"}),
                        status=500, headers=cors_headers, mimetype="application/json")

    from datetime import datetime, timezone, timedelta
    creds = base64.b64encode(f"{SHIPSTATION_KEY}:{SHIPSTATION_SECRET}".encode()).decode()
    ss_auth = {"Authorization": f"Basic {creds}"}

    # Today's date in Eastern time (UTC-4 DST / UTC-5 standard)
    eastern = timezone(timedelta(hours=-4))
    now_eastern = datetime.now(eastern)
    today = now_eastern.strftime("%Y-%m-%d")
    tomorrow = (now_eastern + timedelta(days=1)).strftime("%Y-%m-%d")

    # Fetch awaiting shipment count
    count = None
    try:
        r = req_lib.get(
            "https://ssapi.shipstation.com/orders",
            params={"orderStatus": "awaiting_shipment", "pageSize": 1},
            headers=ss_auth, timeout=10
        )
        count = r.json().get("total", 0)
    except Exception as e:
        print(f"[awaiting] count fetch error: {e}")

    # Fetch orders placed today
    placed_today = 0
    placed_error = None
    try:
        r2 = req_lib.get(
            "https://ssapi.shipstation.com/orders",
            params={"orderDateStart": today, "orderDateEnd": tomorrow, "pageSize": 1},
            headers=ss_auth, timeout=10
        )
        body = r2.json()
        if "total" in body:
            placed_today = body["total"]
        else:
            placed_error = body.get("message", f"HTTP {r2.status_code}: {str(body)[:200]}")
    except Exception as e:
        placed_error = str(e)
        print(f"[awaiting] placedToday fetch error: {e}")

    # If both calls failed, return stale cache rather than zeroes
    if count is None and placed_today == 0 and _AWAITING_CACHE["data"] is not None:
        return Response(json.dumps({**_AWAITING_CACHE["data"], "stale": True}),
                        headers=cors_headers, mimetype="application/json")

    result = {"count": count, "placedToday": placed_today}
    if placed_error:
        result["placedTodayError"] = placed_error

    # Cache only successful responses
    if count is not None:
        _AWAITING_CACHE["data"] = result
        _AWAITING_CACHE["ts"]   = _time.time()

    return Response(json.dumps(result), headers=cors_headers, mimetype="application/json")


@app.route("/budget")
def budget_page():
    return send_from_directory("static", "budget.html")

@app.route("/budget-parent")
def budget_parent_page():
    return send_from_directory("static", "budget-parent.html")

@app.route("/exchange")
def exchange_portal():
    resp = send_from_directory("static", "exchange.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

# ─────────────────────────────────────────────────────────────────────────────
# Budget Cloud Sync API
# ─────────────────────────────────────────────────────────────────────────────

BUDGET_SYNC_DIR = os.path.join(os.path.dirname(__file__), 'budget_sync_data')
os.makedirs(BUDGET_SYNC_DIR, exist_ok=True)

def _safe_sync_code(code):
    """Sanitize sync code: lowercase alphanumeric + hyphens only, max 32 chars."""
    return re.sub(r'[^a-z0-9\-]', '', code.lower())[:32]

@app.after_request
def add_cors_budget(response):
    if request.path.startswith('/api/budget/'):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

@app.route("/api/budget/sync/<code>", methods=["OPTIONS"])
def budget_sync_options(code):
    resp = app.make_default_options_response()
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return resp

@app.route("/api/budget/sync/<code>", methods=["GET"])
def budget_sync_get(code):
    code = _safe_sync_code(code)
    if not code:
        return Response(json.dumps({"error": "invalid code"}), status=400, mimetype="application/json")
    path = os.path.join(BUDGET_SYNC_DIR, f"{code}.json")
    if not os.path.exists(path):
        return Response(json.dumps({"error": "not found"}), status=404, mimetype="application/json")
    with open(path) as f:
        data = json.load(f)
    return Response(json.dumps(data), mimetype="application/json")

@app.route("/api/budget/sync/<code>", methods=["POST"])
def budget_sync_post(code):
    code = _safe_sync_code(code)
    if not code:
        return Response(json.dumps({"error": "invalid code"}), status=400, mimetype="application/json")
    payload = request.get_json(force=True)
    if not payload or "data" not in payload:
        return Response(json.dumps({"error": "missing data"}), status=400, mimetype="application/json")
    path = os.path.join(BUDGET_SYNC_DIR, f"{code}.json")
    from datetime import datetime as _bdt
    with open(path, 'w') as f:
        json.dump({"data": payload["data"], "updated": _bdt.utcnow().isoformat()}, f)
    return Response(json.dumps({"ok": True}), mimetype="application/json")

@app.route("/api/budget/sync/<code>", methods=["DELETE"])
def budget_sync_delete(code):
    code = _safe_sync_code(code)
    path = os.path.join(BUDGET_SYNC_DIR, f"{code}.json")
    if os.path.exists(path):
        os.remove(path)
    return Response(json.dumps({"ok": True}), mimetype="application/json")


@app.route("/api/verify-exchange", methods=["POST", "OPTIONS"])
def verify_exchange():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}
    order_number  = data.get("orderNumber", "").strip().lstrip("#")
    email_input   = data.get("email", "").strip().lower()
    last_name_input = data.get("lastName", "").strip().lower()

    if not order_number or (not email_input and not last_name_input):
        return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

    try:
        from datetime import datetime, timezone, timedelta

        r = req_lib.get("https://ssapi.shipstation.com/orders",
                         params={"orderNumber": order_number},
                         headers=ss_headers(), timeout=10)
        orders = r.json().get("orders", [])

        if not orders:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        order = orders[0]

        # Verify identity — last name OR email must match (same logic as returns)
        ship_name   = order.get("shipTo", {}).get("name", "").strip()
        order_last  = ship_name.split()[-1].lower() if ship_name else ""
        order_email = (order.get("customerEmail") or "").strip().lower()
        name_match  = last_name_input and last_name_input == order_last
        email_match = email_input and email_input == order_email
        if not name_match and not email_match:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        # Block international and military overseas orders
        MILITARY_STATES = {"AA", "AE", "AP"}
        country = order.get("shipTo", {}).get("country", "US")
        state   = order.get("shipTo", {}).get("state", "").upper()
        if country not in ("US", "USA") or state in MILITARY_STATES:
            return Response(json.dumps({"status": "international"}), headers=c, mimetype="application/json")

        # Block exchanges on unshipped orders
        order_status = order.get("orderStatus", "")
        UNSHIPPED_STATUSES = {"awaiting_shipment", "awaiting_payment", "on_hold"}
        if order_status in UNSHIPPED_STATUSES:
            return Response(json.dumps({"status": "not_shipped"}), headers=c, mimetype="application/json")

        # Check ship date within 37 days
        sr = req_lib.get("https://ssapi.shipstation.com/shipments",
                          params={"orderNumber": order_number},
                          headers=ss_headers(), timeout=10)
        shipments = sr.json().get("shipments", [])
        active_shipments = [s for s in shipments if not s.get("voided", False)]
        ship_date_str = active_shipments[0].get("shipDate", "") if active_shipments else ""

        # Also block if no shipments and order status isn't clearly shipped
        if not ship_date_str and order_status != "shipped":
            return Response(json.dumps({"status": "not_shipped"}), headers=c, mimetype="application/json")

        def parse_dt(s):
            s = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        if ship_date_str:
            ship_date = parse_dt(ship_date_str)
        else:
            od = order.get("orderDate", "")
            ship_date = parse_dt(od) if od else datetime.now(timezone.utc)

        eligible_until = ship_date + timedelta(days=37)
        if datetime.now(timezone.utc) > eligible_until:
            return Response(json.dumps({"status": "outside_window"}), headers=c, mimetype="application/json")

        # Find exchange-eligible items via Airtable
        # "Can Exchange = TRUE" marks exchange *targets* (options), not the customer's item.
        # Eligibility = customer's SKU exists in Airtable with a parent that has Can Exchange options.
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        # Fetch all exchange options once (used to check parent eligibility per item)
        all_exchange_options = at_get_all(
            PRODUCT_SKUS_TABLE_ID,
            airtable_read_token,
            fields=["Parent Product"],
            formula="{Can Exchange}=TRUE()",
        )
        eligible_parent_ids = set()
        for opt in all_exchange_options:
            for pid in opt["fields"].get("Parent Product", []):
                eligible_parent_ids.add(pid)
        # Always allow same-SKU exchange parents even if no Can Exchange SKUs set
        eligible_parent_ids.update(SAME_SKU_EXCHANGE_PARENT_IDS)

        print(f"[verify-exchange] eligible_parent_ids ({len(eligible_parent_ids)}): {eligible_parent_ids}")
        eligible_items = []
        for item in order.get("items", []):
            sku = (item.get("sku") or "").strip()
            if not sku:
                continue
            print(f"[verify-exchange] checking item SKU: '{sku}' name: '{item.get('name','')}'")
            # Look up the SKU without Can Exchange filter
            at_r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
                params={"filterByFormula": f'{{SKU ID}}="{sku}"', "maxRecords": 1,
                        "fields[]": ["Name + Variations", "SKU ID", "Parent Product"]},
                headers=at_headers(airtable_read_token),
                timeout=10,
            )
            records = at_r.json().get("records", [])
            print(f"[verify-exchange] Airtable direct lookup for '{sku}': {len(records)} record(s)")
            # Fallback: if outer-only SKU (ends in -O) not found, try base SKU without -O suffix
            lookup_sku = sku
            if not records and re.search(r'-O$', sku, re.IGNORECASE):
                base_sku = re.sub(r'-O$', '', sku, flags=re.IGNORECASE)
                fallback_r = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
                    params={"filterByFormula": f'{{SKU ID}}="{base_sku}"', "maxRecords": 1,
                            "fields[]": ["Name + Variations", "SKU ID", "Parent Product"]},
                    headers=at_headers(airtable_read_token),
                    timeout=10,
                )
                records = fallback_r.json().get("records", [])
                print(f"[verify-exchange] Fallback lookup for '{base_sku}': {len(records)} record(s)")
                if records:
                    lookup_sku = base_sku
            if not records:
                print(f"[verify-exchange] No Airtable record found for SKU '{sku}' — skipping")
                continue
            rec = records[0]
            parent_products = rec["fields"].get("Parent Product", [])
            parent_product_id = parent_products[0] if parent_products else ""
            # Only eligible if parent has exchange options available
            print(f"[verify-exchange] SKU '{sku}' → parent '{parent_product_id}' | in eligible set: {parent_product_id in eligible_parent_ids}")
            if not parent_product_id or parent_product_id not in eligible_parent_ids:
                continue
            eligible_items.append({
                "name":            item.get("name", ""),
                "sku":             sku,  # keep original SKU for duplicate-exchange detection
                "quantity":        int(item.get("quantity", 1)),
                "airtableId":      rec["id"],
                "parentProductId": parent_product_id,
                "sameSkuExchange": parent_product_id in SAME_SKU_EXCHANGE_PARENT_IDS,
            })

        if not eligible_items:
            return Response(json.dumps({"status": "no_eligible_items"}), headers=c, mimetype="application/json")

        # Check for existing exchange orders (-E, -E2, -E3, ...) and collect already-exchanged SKUs
        # Original SKU is stored in customField3 of each exchange order
        already_exchanged_skus = set()
        next_suffix = "-E"
        try:
            for n in range(1, 10):
                suffix = "-E" if n == 1 else f"-E{n}"
                ex_r = req_lib.get("https://ssapi.shipstation.com/orders",
                                    params={"orderNumber": f"{order_number}{suffix}"},
                                    headers=ss_headers(), timeout=10)
                ex_orders = ex_r.json().get("orders", [])
                if not ex_orders:
                    next_suffix = suffix
                    break
                for ex_order in ex_orders:
                    orig_sku = ((ex_order.get("advancedOptions") or {}).get("customField3") or "").strip()
                    if not orig_sku:
                        # Fallback: parse "Original SKUs: ..." from internalNotes (for older orders)
                        notes_text = ex_order.get("internalNotes") or ""
                        m = re.search(r'Original SKUs?:\s*([^\.\n]+)', notes_text)
                        if m:
                            orig_sku = m.group(1).strip()
                    for s in orig_sku.split(","):
                        s = s.strip()
                        if s:
                            already_exchanged_skus.add(s)
        except Exception as ex_check_err:
            print(f"[verify-exchange] exchange-order check failed (non-fatal): {ex_check_err}")

        # Filter out already-exchanged items
        eligible_items = [i for i in eligible_items if i["sku"] not in already_exchanged_skus]

        if not eligible_items:
            return Response(json.dumps({"status": "already_exchanged"}), headers=c, mimetype="application/json")

        ship_to = order.get("shipTo", {})
        return Response(json.dumps({
            "status":        "eligible",
            "orderId":       order.get("orderId"),
            "orderNumber":   order_number,
            "customerName":  ship_to.get("name", ""),
            "customerEmail": order.get("customerEmail", ""),
            "shipTo": {
                "name":       ship_to.get("name", ""),
                "street1":    ship_to.get("street1", ""),
                "street2":    ship_to.get("street2", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country":    ship_to.get("country", "US"),
            },
            "eligibleItems": eligible_items,
            "nextSuffix":    next_suffix,
        }), headers=c, mimetype="application/json")

    except Exception as e:
        import traceback
        print(f"[verify-exchange] ERROR: {e}\n{traceback.format_exc()}")
        return Response(json.dumps({"status": "error", "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/exchange-options", methods=["POST", "OPTIONS"])
def exchange_options():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}
    parent_product_id = data.get("parentProductId", "").strip()

    if not parent_product_id:
        return Response(json.dumps({"options": []}), headers=c, mimetype="application/json")

    try:
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        # Same-SKU exchange: return color/size dropdowns from the parent product's variations
        if parent_product_id in SAME_SKU_EXCHANGE_PARENT_IDS:
            parent_r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PARENT_PRODUCTS_TABLE_ID}/{parent_product_id}",
                headers=at_headers(airtable_read_token), timeout=10,
            )
            pf = parent_r.json().get("fields", {})
            color_options, size_options = [], []
            for cid in pf.get("Color Variations", []):
                cr = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{COLORS_TABLE_ID}/{cid}",
                    headers=at_headers(airtable_read_token), timeout=10,
                )
                name = cr.json().get("fields", {}).get("Name", "").strip()
                if name:
                    color_options.append(name)
            for sid in pf.get("Size Variations", []):
                sr = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{SIZES_TABLE_ID}/{sid}",
                    headers=at_headers(airtable_read_token), timeout=10,
                )
                name = sr.json().get("fields", {}).get("Name", "").strip()
                if name and name.lower() not in ("none", "n/a", "one size"):
                    size_options.append(name)
            return Response(json.dumps({
                "sameSkuOnly":  True,
                "colorOptions": color_options,
                "sizeOptions":  size_options,
            }), headers=c, mimetype="application/json")

        records = at_get_all(
            PRODUCT_SKUS_TABLE_ID,
            airtable_read_token,
            fields=["Name + Variations", "SKU ID", "Parent Product"],
            formula="{Can Exchange}=TRUE()",
        )
        options = []
        for rec in records:
            fields = rec.get("fields", {})
            if parent_product_id not in fields.get("Parent Product", []):
                continue
            raw_name = fields.get("Name + Variations", "")
            # Exclude "Add Size to Order Notes" options — customer can't specify size there
            if "add size to order notes" in raw_name.lower():
                continue
            sku = fields.get("SKU ID", "")
            # Apply explicit name overrides first; otherwise clean the raw name
            if sku in _EXCHANGE_NAME_OVERRIDES:
                clean_name = _EXCHANGE_NAME_OVERRIDES[sku]
            else:
                clean_name = raw_name.replace(" - Base Only (-ONB)", "").replace(" - Base Only", "").replace(" (-ONB)", "").strip()
                clean_name = re.sub(r'\s+Outer\s*-\s*', ' - ', clean_name).strip()
            options.append({
                "id":   rec["id"],
                "name": clean_name,
                "sku":  sku,
            })
        options.sort(key=lambda x: (1 if "other size" in x["name"].lower() else 0, x["name"]))
        return Response(json.dumps({"options": options}), headers=c, mimetype="application/json")

    except Exception as e:
        return Response(json.dumps({"error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/submit-exchange", methods=["POST", "OPTIONS"])
def submit_exchange():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    from datetime import datetime, timezone

    original_order_id     = data.get("originalOrderId")
    original_order_number = data.get("originalOrderNumber", "")
    customer_email        = data.get("customerEmail", "").strip()
    customer_name         = data.get("customerName", "")
    ship_to               = data.get("shipTo", {})
    notes                 = data.get("notes", "")
    next_suffix           = data.get("nextSuffix", "-E")
    items_payload         = data.get("items", [])

    if not items_payload:
        return Response(json.dumps({"success": False, "error": "No items provided"}),
                        status=400, headers=c, mimetype="application/json")

    exchange_order_number = f"{original_order_number}{next_suffix}"

    # Routing: if ANY selected belt is EDC/Low Profile → EDC shipper; otherwise battle/duty
    all_names_lower = " ".join(i.get("selectedName", "") for i in items_payload).lower()
    if any(k in all_names_lower for k in ["edc", "low profile", "inner only", "1.5"]):
        tag_id  = 105813
        user_id = "c2fc99de-a9ec-4dfb-8b74-1d263eab34b8"  # Lisa Barnes
    else:
        tag_id  = 102014
        user_id = "62230ab9-eefe-4dd9-8175-949f097fa363"  # Janna Frei

    today     = datetime.now(timezone.utc)
    today_iso = today.isoformat()

    try:
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        order_items = []

        # Pre-check: if this is an exchange-of-an-exchange (original order number contains "-E"),
        # and the customer is explicitly selecting an inner-only belt, suppress the auto-add —
        # the inner was already included in the previous exchange order, so auto-adding another
        # would create a duplicate. For plain original orders, allow both (customer may have
        # originally ordered an outer + inner separately and wants to exchange both).
        is_exchange_of_exchange = "-E" in original_order_number.upper()
        customer_selecting_inner = any(
            "inner" in (i.get("selectedName", "")).lower()
            for i in items_payload
        )
        suppress_inner_auto_add = is_exchange_of_exchange and customer_selecting_inner

        for item_idx, item_data in enumerate(items_payload):
            original_sku = item_data.get("originalSku", "")
            selected_sku = item_data.get("selectedSku", "")
            selected_name = item_data.get("selectedName", "")
            quantity      = int(item_data.get("quantity", 1))

            order_items.append({
                "lineItemKey":    f"exchange-{item_idx + 1}",
                "name":          selected_name,
                "sku":           selected_sku,
                "quantity":      quantity,
                "unitPrice":     0.00,
                "taxAmount":     0.00,
                "shippingAmount": 0.00,
            })

            # ── LP Inner lookup ───────────────────────────────────────────────
            # Skip entirely if this is an exchange-of-exchange and the customer is
            # already selecting an inner belt item directly
            if suppress_inner_auto_add:
                continue

            selected_is_onb = bool(re.search(r'-ONB$', selected_sku, re.IGNORECASE))
            inner_added = False

            # Shared color map used by all inner lookup paths
            LP_COLOR_MAP = [
                (["mc black", "mc tropic", "woodland", "black"], "Black"),
                (["coyote brown", "coyote", "mc australian", "mc arid"], "Coyote Brown"),
                (["mc classic", "multicam"],                             "Multicam"),
                (["ranger green", "ranger", "od green"],                 "OD Green"),
                (["wolf gray"],                                          "Wolf Gray"),
            ]

            def _lp_inner_item(color, size, key_suffix):
                """Look up LP INNER ONLY Belt by color+size; return order item dict or None."""
                search_str    = f"LP INNER ONLY Belt {color} {size}"
                inner_formula = (
                    f'AND(NOT(SEARCH("WPS",{{Name + Variations}})),'
                    f'SEARCH("{search_str}",{{Name + Variations}}))'
                )
                recs = at_get_all(
                    PRODUCT_SKUS_TABLE_ID, airtable_read_token,
                    fields=["Name + Variations", "SKU ID"],
                    formula=inner_formula,
                )
                if recs:
                    ir = recs[0]["fields"]
                    return {
                        "lineItemKey":    key_suffix,
                        "name":          ir.get("Name + Variations", ""),
                        "sku":           ir.get("SKU ID", ""),
                        "quantity":      quantity,
                        "unitPrice":     0.00,
                        "taxAmount":     0.00,
                        "shippingAmount": 0.00,
                    }
                return None

            def _extract_lp_color_size(name):
                """Extract LP inner color and belt size from a product name."""
                name_lower = name.lower()
                color = None
                for keywords, c in LP_COLOR_MAP:
                    for kw in keywords:
                        if kw in name_lower:
                            color = c
                            break
                    if color:
                        break
                # Find a 2-digit number in belt-size range (24–64) not adjacent to other digits
                size_matches = re.findall(r'(?<!\d)(\d{2})(?!\d)', name)
                size = next((s for s in size_matches if 24 <= int(s) <= 64), None)
                return color, size

            if selected_is_onb:
                # Path 1 — ONB selection: extract color+size from SKU/name
                try:
                    size_m     = re.search(r'-(\d+)-ONB$', selected_sku, re.IGNORECASE)
                    inner_size = size_m.group(1) if size_m else None
                    name_lower = selected_name.lower()
                    inner_color = None
                    for keywords, color in LP_COLOR_MAP:
                        for kw in keywords:
                            if kw in name_lower:
                                inner_color = color
                                break
                        if inner_color:
                            break
                    if inner_color and inner_size:
                        item = _lp_inner_item(inner_color, inner_size,
                                              f"exchange-{item_idx + 1}-inner")
                        if item:
                            order_items.append(item)
                            inner_added = True
                        else:
                            print(f"[submit-exchange] No LP inner found for {inner_color} size {inner_size}")
                except Exception as lp_err:
                    print(f"[submit-exchange] ONB LP inner lookup failed for {selected_sku}: {lp_err}")

            elif not re.search(r'(-O)$', original_sku, re.IGNORECASE):
                # Path 2 — Full-combo selected belt: find inner via Component(s) in Airtable
                try:
                    combo_sku = re.sub(r'(-ONB|-O)$', '', selected_sku, flags=re.IGNORECASE).strip()
                    combo_recs = req_lib.get(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
                        params={"filterByFormula": f'{{SKU ID}}="{combo_sku}"', "maxRecords": 1,
                                "fields[]": ["Component(s)"]},
                        headers=at_headers(airtable_read_token), timeout=10
                    ).json().get("records", [])
                    component_ids = combo_recs[0]["fields"].get("Component(s)", []) if combo_recs else []
                    for comp_id in component_ids:
                        comp_rec    = req_lib.get(
                            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}/{comp_id}",
                            headers=at_headers(airtable_read_token), timeout=10
                        ).json()
                        comp_fields = comp_rec.get("fields", {})
                        comp_name   = comp_fields.get("Name + Variations", "")
                        comp_sku    = comp_fields.get("SKU ID", "")
                        if "inner" not in comp_name.lower():
                            continue
                        order_items.append({
                            "lineItemKey":    f"exchange-{item_idx + 1}-inner",
                            "name":          comp_name,
                            "sku":           comp_sku,
                            "quantity":      quantity,
                            "unitPrice":     0.00,
                            "taxAmount":     0.00,
                            "shippingAmount": 0.00,
                        })
                        inner_added = True
                        break
                except Exception as lp_err:
                    print(f"[submit-exchange] Component LP inner lookup failed for {selected_sku}: {lp_err}")

            # Path 3 — Parent-based LP inner lookup (for products whose exchange options are
            # outer-only base SKUs with no Component(s), e.g. 1.75" Battle Belt, 2" MOLLE Duty Belt)
            # Skip if the original order was outer-only (-O suffix) — customer only ordered the outer belt
            original_is_outer_only = bool(re.search(r'-O$', original_sku, re.IGNORECASE))
            if not inner_added and not original_is_outer_only:
                item_parent_id = item_data.get("parentProductId", "")
                if item_parent_id in _LP_INNER_REQUIRED_PARENT_IDS:
                    try:
                        inner_color, inner_size = _extract_lp_color_size(selected_name)
                        if inner_color and inner_size:
                            item = _lp_inner_item(inner_color, inner_size,
                                                  f"exchange-{item_idx + 1}-inner")
                            if item:
                                order_items.append(item)
                                inner_added = True
                            else:
                                print(f"[submit-exchange] Path3: No LP inner for {inner_color} size {inner_size} (parent {item_parent_id})")
                        else:
                            print(f"[submit-exchange] Path3: Could not extract color/size from '{selected_name}'")
                    except Exception as lp_err:
                        print(f"[submit-exchange] Path3 LP inner lookup failed for {selected_sku}: {lp_err}")

        original_skus_csv = ",".join(i.get("originalSku", "") for i in items_payload)
        selected_names    = ", ".join(i.get("selectedName", "") for i in items_payload)

        # Create ShipStation exchange order
        order_payload = {
            "orderNumber":   exchange_order_number,
            "orderDate":     today_iso,
            "paymentDate":   today_iso,
            "orderStatus":   "awaiting_shipment",
            "customerEmail": customer_email,
            "billTo": {
                "name":       ship_to.get("name", ""),
                "street1":    ship_to.get("street1", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country":    "US",
            },
            "shipTo": ship_to,
            "items": order_items,
            "amountPaid":     0.00,
            "taxAmount":      0.00,
            "shippingAmount": 0.00,
            "carrierCode":    "stamps_com",
            "serviceCode":    "usps_ground_advantage",
            "packageCode":    "package",
            "confirmation":   "delivery",
            "dimensions": {
                "units":  "inches",
                "length": 8,
                "width":  8,
                "height": 2,
            },
            "internalNotes":  f"Exchange for original order #{original_order_number}. Original SKUs: {original_skus_csv}. Include return label. Customer note: {notes}",
            "advancedOptions": {
                "storeId":      SIZING_EXCHANGE_STORE_ID,
                "customField1": f"Exchange for order #{original_order_number}",
                "customField2": notes,
                "customField3": original_skus_csv,
            },
        }

        r = req_lib.post(
            "https://ssapi.shipstation.com/orders/createorder",
            headers={**ss_headers(), "Content-Type": "application/json"},
            json=order_payload,
            timeout=20,
        )
        if r.status_code not in (200, 201):
            raise Exception(f"ShipStation order creation failed: {r.status_code} {r.text}")

        new_order    = r.json()
        new_order_id = new_order.get("orderId")

        # Add routing tag (EDC or Manual Label)
        try:
            req_lib.post(
                "https://ssapi.shipstation.com/orders/addtag",
                headers={**ss_headers(), "Content-Type": "application/json"},
                json={"orderId": new_order_id, "tagId": tag_id},
                timeout=10,
            )
        except Exception as tag_err:
            print(f"[submit-exchange] Routing tag failed: {tag_err}")

        # Add Expedite (2 Days) tag to all exchange orders
        try:
            req_lib.post(
                "https://ssapi.shipstation.com/orders/addtag",
                headers={**ss_headers(), "Content-Type": "application/json"},
                json={"orderId": new_order_id, "tagId": 49845},
                timeout=10,
            )
        except Exception as tag_err:
            print(f"[submit-exchange] Expedite tag failed: {tag_err}")

        # Assign user
        try:
            req_lib.post(
                "https://ssapi.shipstation.com/orders/assignuser",
                headers={**ss_headers(), "Content-Type": "application/json"},
                json={"orderIds": [new_order_id], "userId": user_id},
                timeout=10,
            )
        except Exception as assign_err:
            print(f"[submit-exchange] User assign failed: {assign_err}")

        # Send confirmation email via SendGrid
        if SENDGRID_API_KEY and customer_email:
            first_name = customer_name.split()[0] if customer_name else "there"
            belt_lines = "\n".join(f"  • {i['selectedName']}" for i in items_payload)
            email_body = (
                f"Hi {first_name},\n\n"
                f"Your size exchange request has been received!\n\n"
                f"Original Order: #{original_order_number}\n"
                f"New Belt(s):\n{belt_lines}\n\n"
                f"We'll ship your new belt(s) to the address on your original order. "
                f"Your package will include a prepaid return label — please use it to send back "
                f"your original belt(s) within 30 days.\n\n"
                f"Questions? Reply to this email and our team will help you out.\n\n"
                f"— Blue Alpha"
            )
            try:
                req_lib.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "personalizations": [{"to": [{"email": TEST_EMAIL_OVERRIDE or customer_email}]}],
                        "from":    {"email": CS_FROM_EMAIL, "name": "Blue Alpha"},
                        "subject": f"Your Blue Alpha Size Exchange — Order #{original_order_number}",
                        "content": [{"type": "text/plain", "value": email_body}],
                    },
                    timeout=15,
                )
            except Exception as email_err:
                print(f"[submit-exchange] Email failed: {email_err}")

        return Response(
            json.dumps({"success": True, "exchangeOrderNumber": exchange_order_number}),
            headers=c, mimetype="application/json",
        )

    except Exception as e:
        return Response(
            json.dumps({"success": False, "error": str(e)}),
            status=500, headers=c, mimetype="application/json",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Quote Portal
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/quote-builder")
def quote_builder_page():
    resp = send_from_directory("static", "quote.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/quote")
def quote_gate_page():
    """Gate page — redirect to portal if logged in, else show apply/login page."""
    user = get_portal_user(request)
    if user:
        return redirect("/portal")
    resp = send_from_directory("static", "portal-gate.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp

@app.route("/view-quote/<record_id>")
def view_quote_page(record_id):
    return send_from_directory("static", "view-quote.html")

@app.route("/view-invoice/<record_id>")
def view_invoice_page(record_id):
    return send_from_directory("static", "view-invoice.html")


def _clean_product_name(name):
    """Strip marketing suffixes from product names."""
    for suffix in [" - Base Only (-ONB)", " - Base Only", " (-ONB)"]:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.strip()


def send_quote_email(to_email, to_name, company, quote_number, record_id, expiry_date, quote_data=None):
    """Send quote notification email via SendGrid."""
    if not SENDGRID_API_KEY:
        return
    actual_to = TEST_EMAIL_OVERRIDE or to_email
    quote_link = f"{QUOTE_BASE_URL}/portal?tab=our-quotes&amp;quote={record_id}"
    first_name = to_name.split()[0] if to_name else "there"
    subject = f"Your Blue Alpha Quote \u2013 {quote_number}"
    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;padding:32px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#1B2438;padding:28px 40px;text-align:left;">
          <span style="font-family:Arial,Helvetica,sans-serif;font-size:22px;font-weight:800;color:#ffffff;letter-spacing:2px;">BLUE ALPHA</span>
        </td></tr>
        <tr><td style="padding:36px 40px;">
          <p style="color:#1a2633;font-size:16px;margin:0 0 8px;">Hi {first_name},</p>
          <p style="color:#6b7a8d;font-size:15px;line-height:1.6;margin:0 0 28px;">
            Your quote from Blue Alpha is ready. Click the button below to view your items, make changes, or accept your order.
          </p>
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;border:1px solid #dde3ea;border-radius:8px;margin-bottom:28px;">
            <tr><td style="padding:20px 24px;">
              <table cellpadding="0" cellspacing="0">
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;width:110px;">Quote Number</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;font-weight:700;">{quote_number}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;">Company</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;">{company}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;">Expires</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;">{expiry_date}</td>
                </tr>
              </table>
            </td></tr>
          </table>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr><td align="center">
              <a href="{quote_link}" style="display:inline-block;background:#1B2438;color:#ffffff;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:700;text-decoration:none;padding:14px 36px;border-radius:6px;letter-spacing:0.5px;">View Your Quote &rarr;</a>
            </td></tr>
          </table>
          <p style="color:#6b7a8d;font-size:13px;margin:28px 0 0;line-height:1.6;">
            This link is unique to your quote. Bookmark it or save this email — you can return anytime to edit or accept before the expiry date.<br><br>
            Questions? Contact us at <a href="mailto:orders@bluealpha.us" style="color:#1B2438;">orders@bluealpha.us</a> or 678-961-3304.
          </p>
        </td></tr>
        <tr><td style="background:#f5f7fa;border-top:1px solid #dde3ea;padding:20px 40px;text-align:center;">
          <p style="color:#6b7a8d;font-size:12px;margin:0;">Blue Alpha &bull; bluealphabelts.com &bull; orders@bluealpha.us &bull; 678-961-3304</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    try:
        payload = {
            "personalizations": [{"to": [{"email": actual_to, "name": to_name}]}],
            "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
            "subject": subject,
            "content": [{"type": "text/html", "value": html_body}],
        }
        # Attach PDF if quote data available
        if quote_data:
            try:
                import base64
                pdf_bytes = _build_quote_pdf_bytes(quote_data)
                payload["attachments"] = [{
                    "content":     base64.b64encode(pdf_bytes).decode("utf-8"),
                    "type":        "application/pdf",
                    "filename":    f"{quote_number}.pdf",
                    "disposition": "attachment",
                }]
            except Exception as pdf_err:
                print(f"[send_quote_email] PDF generation failed: {pdf_err}")

        sg_r = req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        print(f"[send_quote_email] to={actual_to} status={sg_r.status_code} body={sg_r.text[:200]}")
    except Exception as e:
        print(f"[send_quote_email] failed: {e}")


def send_quote_accepted_email(to_email, to_name, org_name, qu_number, so_number):
    """Send order confirmation email after quote acceptance."""
    if not SENDGRID_API_KEY:
        return
    actual_to = TEST_EMAIL_OVERRIDE or to_email
    first_name = to_name.split()[0] if to_name else "there"
    subject = f"Blue Alpha Quote {qu_number} \u2013 Order Confirmed"
    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;padding:32px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#1B2438;padding:28px 40px;">
          <span style="font-family:Arial,Helvetica,sans-serif;font-size:22px;font-weight:800;color:#ffffff;letter-spacing:2px;">BLUE ALPHA</span>
        </td></tr>
        <tr><td style="padding:36px 40px;">
          <p style="color:#1a2633;font-size:16px;margin:0 0 8px;">Hi {first_name},</p>
          <p style="color:#6b7a8d;font-size:15px;line-height:1.6;margin:0 0 20px;">
            Great news \u2014 your Blue Alpha order has been confirmed! We've created your sales order and our team will be in touch about shipping and invoicing shortly.
          </p>
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;border:1px solid #dde3ea;border-radius:8px;margin-bottom:28px;">
            <tr><td style="padding:20px 24px;">
              <table cellpadding="0" cellspacing="0">
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;width:110px;">Organization</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;">{org_name}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;">Quote</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;">{qu_number}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;">Sales Order</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;font-weight:700;">{so_number}</td>
                </tr>
              </table>
            </td></tr>
          </table>
          <p style="color:#6b7a8d;font-size:13px;margin:0 0 0;line-height:1.6;">
            Questions? Contact us at <a href="mailto:orders@bluealpha.us" style="color:#1B2438;">orders@bluealpha.us</a> or 678-961-3304.
          </p>
        </td></tr>
        <tr><td style="background:#f5f7fa;border-top:1px solid #dde3ea;padding:20px 40px;text-align:center;">
          <p style="color:#6b7a8d;font-size:12px;margin:0;">Blue Alpha &bull; bluealphabelts.com &bull; orders@bluealpha.us &bull; 678-961-3304</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    try:
        req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": actual_to, "name": to_name}]}],
                "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
                "subject": subject,
                "content": [{"type": "text/html", "value": html_body}],
            },
            timeout=15,
        )
    except Exception as e:
        print(f"[send_quote_accepted_email] failed: {e}")


def _invoice_payment_buttons(cc_url, ach_url):
    """Return HTML payment button block, or empty string if no URLs."""
    if not cc_url and not ach_url:
        return ""
    buttons = ""
    if cc_url:
        buttons += (
            f'<a href="{cc_url}" style="display:inline-block;margin-right:12px;margin-bottom:8px;'
            f'padding:12px 24px;background:#1B2438;color:#ffffff;font-size:14px;font-weight:700;'
            f'text-decoration:none;border-radius:6px;">Pay by Credit Card</a>'
        )
    if ach_url:
        buttons += (
            f'<a href="{ach_url}" style="display:inline-block;margin-bottom:8px;'
            f'padding:12px 24px;background:#ffffff;color:#1B2438;font-size:14px;font-weight:700;'
            f'text-decoration:none;border-radius:6px;border:2px solid #1B2438;">Pay by ACH / Bank Transfer</a>'
        )
    return (
        f'<div style="margin:24px 0 20px;">'
        f'<p style="color:#1a2633;font-size:14px;font-weight:700;margin:0 0 12px;">Pay this invoice:</p>'
        f'{buttons}'
        f'<p style="color:#666666;font-size:12px;margin:8px 0 0;font-style:italic;">'
        f'A 3% credit card processing fee is added to CC payments. Pay by ACH/bank transfer to avoid this fee.</p>'
        f'</div>'
    )


def send_invoice_email(to_email, to_name, org_name, so_number, inv_number, line_items, total, tracking, ship_date=None, pdf_bytes=None, stripe_cc_url=None, stripe_ach_url=None):
    """Send invoice email after SO is converted to invoice."""
    if not SENDGRID_API_KEY:
        return
    # Support semicolon or comma-separated email addresses
    if TEST_EMAIL_OVERRIDE:
        to_list = [{"email": TEST_EMAIL_OVERRIDE, "name": to_name}]
    else:
        raw_emails = [e.strip() for e in to_email.replace(",", ";").split(";") if e.strip()]
        to_list = [{"email": e, "name": to_name} for e in raw_emails] if raw_emails else []
    if not to_list:
        return
    actual_to = to_list[0]["email"]  # kept for legacy references below
    first_name = to_name.split()[0] if to_name else "there"
    subject = f"Blue Alpha {so_number} \u2014 Invoice {inv_number}"
    from datetime import timedelta
    due_date = (_today_utc() + timedelta(days=30)).strftime("%B %d, %Y")
    today_str = _today_utc().strftime("%B %d, %Y")
    ship_date_display = ""
    if ship_date:
        try:
            from datetime import date as _dt_date
            sd = _dt_date.fromisoformat(ship_date[:10])
            ship_date_display = sd.strftime("%B %d, %Y")
        except Exception:
            ship_date_display = ship_date

    # Build line items rows
    li_rows = ""
    for li in line_items:
        name       = li.get("name", "")
        qty        = li.get("qty", 0)
        unit_price = float(li.get("unit_price") or 0)
        line_total = float(li.get("line_total") or li.get("total") or (qty * unit_price))
        li_rows += f"""
            <tr>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#1a2633;font-size:13px;">{name}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#1a2633;font-size:13px;text-align:center;">{qty}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#1a2633;font-size:13px;text-align:right;">${unit_price:,.2f}</td>
              <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#1a2633;font-size:13px;text-align:right;">${line_total:,.2f}</td>
            </tr>"""

    tracking_display = tracking if tracking else "—"

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;padding:32px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#1B2438;padding:28px 40px;">
          <span style="font-family:Arial,Helvetica,sans-serif;font-size:22px;font-weight:800;color:#ffffff;letter-spacing:2px;">BLUE ALPHA</span>
        </td></tr>
        <tr><td style="padding:36px 40px;">
          <p style="color:#1a2633;font-size:16px;margin:0 0 8px;">Hi {first_name},</p>
          <p style="color:#6b7a8d;font-size:15px;line-height:1.6;margin:0 0 24px;">
            Your order has shipped and your invoice is ready. Please review the details below.
          </p>
          <!-- Info box -->
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;border:1px solid #dde3ea;border-radius:8px;margin-bottom:28px;">
            <tr><td style="padding:20px 24px;">
              <table cellpadding="0" cellspacing="0" width="100%">
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;width:130px;">Organization</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;">{org_name}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;">Sales Order</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;font-weight:700;">{so_number}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;">Invoice #</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;font-weight:700;">{inv_number}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;">Tracking #</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;">{tracking_display}</td>
                </tr>
                {'<tr><td style="padding:4px 0;color:#6b7a8d;font-size:13px;">Ship Date</td><td style="padding:4px 0;color:#1a2633;font-size:13px;">' + ship_date_display + '</td></tr>' if ship_date_display else ''}
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;">Invoice Date</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;">{today_str}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;">Due Date</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;font-weight:700;">{due_date}</td>
                </tr>
              </table>
            </td></tr>
          </table>
          <!-- Line items table -->
          <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #dde3ea;border-radius:8px;overflow:hidden;margin-bottom:20px;">
            <thead>
              <tr style="background:#1B2438;">
                <th style="padding:10px 12px;color:#ffffff;font-size:12px;text-align:left;font-weight:700;text-transform:uppercase;letter-spacing:.04em;">Description</th>
                <th style="padding:10px 12px;color:#ffffff;font-size:12px;text-align:center;font-weight:700;text-transform:uppercase;letter-spacing:.04em;">Qty</th>
                <th style="padding:10px 12px;color:#ffffff;font-size:12px;text-align:right;font-weight:700;text-transform:uppercase;letter-spacing:.04em;">Unit Price</th>
                <th style="padding:10px 12px;color:#ffffff;font-size:12px;text-align:right;font-weight:700;text-transform:uppercase;letter-spacing:.04em;">Total</th>
              </tr>
            </thead>
            <tbody>{li_rows}
              <tr style="background:#f5f7fa;">
                <td colspan="3" style="padding:10px 12px;color:#1a2633;font-size:14px;font-weight:700;text-align:right;">Grand Total</td>
                <td style="padding:10px 12px;color:#1B2438;font-size:14px;font-weight:800;text-align:right;">${total:,.2f}</td>
              </tr>
            </tbody>
          </table>
          {_invoice_payment_buttons(stripe_cc_url, stripe_ach_url)}
          <p style="color:#BD3333;font-size:12px;margin:0 0 20px;line-height:1.6;">
            A 1.5% monthly late fee applies to balances unpaid after 30 days.
          </p>
          <p style="color:#6b7a8d;font-size:13px;margin:0 0 0;line-height:1.6;">
            Questions? Contact us at <a href="mailto:orders@bluealpha.us" style="color:#1B2438;">orders@bluealpha.us</a> or 678-961-3304.
          </p>
        </td></tr>
        <tr><td style="background:#f5f7fa;border-top:1px solid #dde3ea;padding:20px 40px;text-align:center;">
          <p style="color:#6b7a8d;font-size:12px;margin:0;">Blue Alpha &bull; bluealphabelts.com &bull; orders@bluealpha.us &bull; 678-961-3304</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    try:
        import base64 as _b64
        payload = {
            "personalizations": [{"to": to_list}],
            "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
            "subject": subject,
            "content": [{"type": "text/html", "value": html_body}],
        }
        if pdf_bytes:
            payload["attachments"] = [{
                "content":     _b64.b64encode(pdf_bytes).decode("utf-8"),
                "type":        "application/pdf",
                "filename":    f"{inv_number}.pdf",
                "disposition": "attachment",
            }]
        req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
    except Exception as e:
        print(f"[send_invoice_email] failed: {e}")


def _fetch_quote_data(record_id):
    """Shared logic: fetch full quote data dict from Airtable. Returns dict or raises."""
    from datetime import date as dt_date
    # Use broadest available read token (write token may not have read scope on all tables)
    token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

    # Fetch MO record
    r = req_lib.get(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
        headers=at_headers(token),
        timeout=15,
    )
    if r.status_code != 200:
        return None
    mo = r.json()
    fields = mo.get("fields", {})
    order_type = fields.get("Order Type", "")
    if order_type not in ("Quote", "Sales Order"):
        return None

    order_id     = fields.get("Order ID", "")
    quote_number = fields.get("Document ID", f"QU-{order_id}")
    date_str     = fields.get("Date", "")
    expiry_str   = fields.get("Expiry Date", "")
    is_accepted  = bool(fields.get("MO Is Approved", False))
    po_number    = fields.get("Purchase Order #", "")
    notes        = fields.get("Notes from Customer", "")

    today = _today_utc()
    is_expired = False
    if expiry_str:
        try:
            exp_d = dt_date.fromisoformat(expiry_str)
            is_expired = exp_d < today
        except Exception:
            pass

    # Snapshot fields stored directly on the MO record (preferred — immune to customer record changes)
    _snap_org     = fields.get("Snapshot Org", "")
    _snap_contact = fields.get("Snapshot Contact", "")
    _snap_email   = fields.get("Snapshot Email", "")
    _snap_phone   = fields.get("Snapshot Phone", "")
    _snap_addr1   = fields.get("Snapshot Addr 1", "")
    _snap_addr2   = fields.get("Snapshot Addr 2", "")

    # Fetch customer record as fallback for older quotes that pre-date snapshot fields
    customer_ids = fields.get("Customer", [])
    customer = {}

    # Fetch line items concurrently with customer fetch
    li_record_ids = fields.get("MO Line Items", [])
    line_items = []

    import concurrent.futures as _cf
    def _fetch_li_full(li_id):
        try:
            r2 = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                headers=at_headers(token), timeout=15,
            )
            if r2.status_code == 200:
                return li_id, r2.json()
        except Exception:
            pass
        return li_id, None

    _fut_customer = None
    with _cf.ThreadPoolExecutor(max_workers=21) as _ex:
        if customer_ids:
            _fut_customer = _ex.submit(
                req_lib.get,
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_ids[0]}",
                headers=at_headers(token),
                timeout=15,
            )
        if li_record_ids:
            _results = list(_ex.map(_fetch_li_full, li_record_ids))
        else:
            _results = []

    if customer_ids:
        cr = _fut_customer.result()
        if cr.status_code == 200:
            cf = cr.json().get("fields", {})
            customer = {
                # Use snapshot if present, fall back to customer record
                "orgName":      _snap_org     or cf.get("Organization Name", ""),
                "shipToOrg":    cf.get("Ship To Name", "") or _snap_org or cf.get("Organization Name", ""),
                "contactName":  _snap_contact or cf.get("Main Contact Name", ""),
                "email":        _snap_email   or cf.get("Main Contact Email", ""),
                "phone":        _snap_phone   or cf.get("Main Contact Phone #", ""),
                "address1":     _snap_addr1   or cf.get("Customer Address (Line 1)", ""),
                "address2":     _snap_addr2   or cf.get("Customer Address (Line 2)", ""),
                "city":         cf.get("Customer City", ""),
                "state":        cf.get("Customer State", ""),
                "zip":          cf.get("Customer Zip Code", ""),
                "_taxRate":     float(cf.get("Tax Rate") or 0),
                # Bill-To always comes from Customer record — never snapshot (snapshot = shipping)
                "billToName":   cf.get("Bill-To Contact Name", "") or cf.get("Main Contact Name", ""),
                "billToEmail":  cf.get("Bill-To Contact Email", "") or cf.get("Main Contact Email", ""),
                "billToOrg":    cf.get("Bill-To Org Name", "") or cf.get("Organization Name", ""),
                "billToAddr1":  cf.get("Bill-To Address (Line 1)", ""),
                "billToAddr2":  cf.get("Bill-To Address (Line 2)", ""),
            }
    elif _snap_org or _snap_contact or _snap_email:
        # No linked customer but snapshot exists (edge case)
        customer = {
            "orgName":    _snap_org,     "contactName": _snap_contact,
            "email":      _snap_email,   "phone":       _snap_phone,
            "address1":   _snap_addr1,   "address2":    _snap_addr2,
            "city": "", "state": "", "zip": "",
            "billToName":  _snap_contact, "billToEmail": _snap_email,
            "billToOrg":   _snap_org,
            "billToAddr1": "", "billToAddr2": "",
        }

    if li_record_ids:
        _results_map = {li_id: data for li_id, data in _results if data}
        for li_id in li_record_ids:
            li = _results_map.get(li_id)
            if not li:
                continue
            lf = li.get("fields", {})
            sku_ids   = lf.get("Product SKU", [])
            sku_names = lf.get("Name + Variations (from Product SKU)", [])
            sku_ids_f = lf.get("SKU ID (from Product SKU)", [])
            line_items.append({
                "lineItemId":  li["id"],
                "skuRecordId": sku_ids[0] if sku_ids else "",
                "skuId":       sku_ids_f[0] if sku_ids_f else "",
                "name":        sku_names[0] if sku_names else "",
                "qty":         lf.get("Qty.", 0),
                "unitPrice":   lf.get("Confirmed Unit Price", 0),
            })

    subtotal   = sum(i["qty"] * i["unitPrice"] for i in line_items)
    _tax_rate  = float(customer.get("_taxRate", 0))
    tax_amount = round(subtotal * _tax_rate, 2) if _tax_rate else 0

    # For Sales Orders: check if an invoice exists for this order_id
    inv_record_id = ""
    inv_number    = ""
    if order_type == "Sales Order" and order_id:
        try:
            inv_formula = f'AND({{Order Type}}="Invoice",{{Order ID}}="{order_id}")'
            inv_r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}",
                headers=at_headers(token),
                params={"filterByFormula": inv_formula, "fields[]": ["Document ID"], "maxRecords": 1},
                timeout=15,
            )
            if inv_r.status_code == 200:
                inv_recs = inv_r.json().get("records", [])
                if inv_recs:
                    inv_record_id = inv_recs[0]["id"]
                    inv_number    = inv_recs[0].get("fields", {}).get("Document ID", f"IN-{order_id}")
        except Exception:
            pass

    return {
        "recordId":      record_id,
        "orderId":       order_id,
        "quoteNumber":   quote_number,
        "date":          date_str,
        "expiryDate":    expiry_str,
        "isExpired":     is_expired,
        "isAccepted":    is_accepted,
        "poNumber":      po_number,
        "notes":         notes,
        "customer":      customer,
        "lineItems":     line_items,
        "subtotal":      round(subtotal, 2),
        "taxAmount":     tax_amount,
        "shipping":      0,
        "total":         round(subtotal + tax_amount, 2),
        "invRecordId":   inv_record_id,
        "invNumber":     inv_number,
    }


_CATALOG_CACHE      = {"data": None, "ts": 0}
_CATALOG_TTL        = 86400  # 24 hours (manual refresh via admin button)
_CATALOG_REFRESHING = False  # prevent duplicate background refreshes


def _build_catalog():
    """Fetch and assemble the quote catalog. Fetches all tables in parallel."""
    import concurrent.futures
    token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        fut_skus    = ex.submit(at_get_all, PRODUCT_SKUS_TABLE_ID, token,
                                fields=["SKU ID", "Name + Variations", "Sale Price",
                                        "Parent Product", "Color", "Size", "Category",
                                        "Feature Variation", "Add-ons"],
                                formula="AND({Sale Price},{Category}!='Contract')")
        fut_parents = ex.submit(at_get_all, PARENT_PRODUCTS_TABLE_ID,    token, fields=["Name"])
        fut_colors  = ex.submit(at_get_all, COLORS_TABLE_ID,             token, fields=["Name"])
        fut_sizes   = ex.submit(at_get_all, SIZES_TABLE_ID,              token, fields=["Name"])
        fut_fvars   = ex.submit(at_get_all, FEATURE_VARIATIONS_TABLE_ID, token, fields=["Name"])
        fut_addons  = ex.submit(at_get_all, ADDONS_TABLE_ID,             token,
                                fields=["Name", "Parent Products"])

        sku_records_all = fut_skus.result()
        parent_records  = fut_parents.result()
        color_records   = fut_colors.result()
        size_records    = fut_sizes.result()
        fvar_records    = fut_fvars.result()
        addon_records   = fut_addons.result()

    sku_records = [
        r for r in sku_records_all
        if r.get("fields", {}).get("Sale Price")
        and r.get("fields", {}).get("Category", "") != "Contract"
    ]

    parent_map = {r["id"]: r["fields"].get("Name", "") for r in parent_records}
    color_map  = {r["id"]: r["fields"].get("Name", "") for r in color_records}
    size_map   = {r["id"]: r["fields"].get("Name", "") for r in size_records}
    fvar_map   = {r["id"]: r["fields"].get("Name", "").strip() for r in fvar_records}

    # Build add-on name map and parent → addons list
    addon_name_map   = {}  # addonId → name
    addon_parent_map = {}  # parentId → [{id, name}]
    for r in addon_records:
        name = r["fields"].get("Name", "").strip()
        if not name:
            continue
        if name.lower() in _EXCLUDED_ADDONS_GLOBAL:
            continue
        addon_name_map[r["id"]] = name
        for pid in r["fields"].get("Parent Products", []):
            parent_name_for_addon = _clean_product_name(parent_map.get(pid, "")).lower()
            excluded_for_parent = _EXCLUDED_ADDONS_BY_PARENT.get(parent_name_for_addon, set())
            if name.lower() in excluded_for_parent:
                continue
            addon_parent_map.setdefault(pid, []).append({"id": r["id"], "name": name})

    # Build add-on SKU map: addonId → colorId → {recordId, price, name, sizeId}
    # Uses pre-filter SKU list so add-on SKUs are found even if their parent is excluded
    addon_sku_map = {}
    for r in sku_records_all:
        f = r["fields"]
        if not f.get("Sale Price"):
            continue
        addon_ids = f.get("Add-ons", [])
        if not addon_ids:
            continue
        color_ids = f.get("Color", [])
        color_id  = color_ids[0] if color_ids else ""
        size_ids  = f.get("Size", [])
        size_id   = size_ids[0] if size_ids else ""
        price     = f.get("Sale Price", 0)
        name      = _clean_product_name(f.get("Name + Variations", ""))
        for aid in addon_ids:
            if aid not in addon_name_map:
                continue  # skip "None" and unlisted add-ons
            addon_sku_map.setdefault(aid, {})
            if color_id not in addon_sku_map[aid]:  # keep first match per color
                addon_sku_map[aid][color_id] = {
                    "recordId": r["id"],
                    "price":    price,
                    "name":     name,
                    "sizeId":   size_id,
                }

    skus = []
    seen_parents = {}
    for r in sku_records:
        f = r["fields"]
        parent_ids = f.get("Parent Product", [])
        if not parent_ids:
            continue
        parent_id   = parent_ids[0]
        parent_name = _clean_product_name(parent_map.get(parent_id, ""))
        if not parent_name:
            continue
        if parent_name.lower() in _EXCLUDED_PARENTS:
            continue

        color_ids  = f.get("Color", [])
        size_ids   = f.get("Size", [])
        color_id   = color_ids[0] if color_ids else ""
        size_id    = size_ids[0]  if size_ids  else ""
        color_name = color_map.get(color_id, "")
        color_name = _COLOR_NAME_OVERRIDES.get(color_name.lower(), color_name)
        size_name  = size_map.get(size_id, "")
        # Treat placeholder sizes as no-size so frontend handles correctly
        if size_name.strip().lower() in ("none", "n/a", "one size"):
            size_id = ""; size_name = ""

        excluded_colors = _EXCLUDED_COLORS_BY_PARENT.get(parent_name.lower(), set())
        _cn_lower = color_name.lower()
        _globally_excluded = (
            _cn_lower in _EXCLUDED_COLORS_GLOBAL
            and parent_name.lower() not in _EXCLUDED_COLORS_GLOBAL_EXCEPT.get(_cn_lower, set())
        )
        if _cn_lower in excluded_colors or _globally_excluded:
            continue

        fvar_ids  = f.get("Feature Variation", [])
        fvar_id   = fvar_ids[0] if fvar_ids else ""
        fvar_name = fvar_map.get(fvar_id, "").strip() if fvar_id else ""
        # Treat internal/non-display feature variation names as no feature variation
        if fvar_name.lower() in ("none", "", "molle mount"):
            fvar_id = ""; fvar_name = ""
        if fvar_name.lower() in _EXCLUDED_FEATURE_VARS_GLOBAL:
            continue

        sku_id = f.get("SKU ID", "")
        if "-onb" in sku_id.lower():
            continue

        addon_ids_sku  = f.get("Add-ons", [])
        addon_id_sku   = addon_ids_sku[0] if addon_ids_sku else ""
        addon_name_sku = addon_name_map.get(addon_id_sku, "") if addon_id_sku else ""
        # If add-on record exists but has no name, treat as base (no add-on)
        if addon_id_sku and not addon_name_sku.strip():
            addon_id_sku = ""; addon_name_sku = ""

        raw_name   = f.get("Name + Variations", "")
        clean_name = _clean_product_name(raw_name)
        category   = f.get("Category", "") or ""

        skus.append({
            "recordId":       r["id"],
            "sku":            f.get("SKU ID", ""),
            "name":           clean_name,
            "price":          f.get("Sale Price", 0),
            "parentId":       parent_id,
            "parentName":     parent_name,
            "colorId":        color_id,
            "colorName":      color_name,
            "sizeId":         size_id,
            "sizeName":       size_name,
            "featureVarId":   fvar_id,
            "featureVarName": fvar_name,
            "addonId":        addon_id_sku,
            "addonName":      addon_name_sku,
            "category":       category,
        })
        if parent_id not in seen_parents:
            display_name = _PARENT_NAME_OVERRIDES.get(parent_name.lower(), parent_name)
            seen_parents[parent_id] = {"name": display_name, "category": category}

    _PRODUCT_SELECT_PARENTS = {"alias accessories", "ohc items"}
    parents = sorted(
        [{"id": k, "name": v["name"], "category": v["category"],
          "addons": addon_parent_map.get(k, []),
          "productSelect": v["name"].lower().strip() in _PRODUCT_SELECT_PARENTS}
         for k, v in seen_parents.items()],
        key=lambda x: (-1 if x["name"].lower() in _SORT_FIRST_PARENTS else 1 if x["name"].lower() in _SORT_LAST_PARENTS else 0, x["name"]),
    )
    return {"parents": parents, "skus": skus, "addonSkus": addon_sku_map}


def _refresh_catalog_bg():
    """Build catalog in background and update cache. Clears refreshing flag when done."""
    global _CATALOG_REFRESHING
    import time as _time
    try:
        result = _build_catalog()
        _CATALOG_CACHE["data"] = result
        _CATALOG_CACHE["ts"]   = _time.time()
        print("[catalog] background refresh complete")
    except Exception as e:
        print(f"[catalog] background refresh failed: {e}")
    finally:
        _CATALOG_REFRESHING = False


# Warm cache on startup so the first visitor never waits
threading.Thread(target=_refresh_catalog_bg, daemon=True).start()
# Parent product Airtable IDs whose exchange selections are outer-only SKUs (no components),
# but still require an LP inner belt to be added to the exchange order.
# These use the same LP INNER ONLY Belt color+size lookup as the ONB path.
# Outer-only SKUs that exchange for the exact same SKU (customer picks color/size via dropdowns)
SAME_SKU_EXCHANGE_SKUS = {"BAG-2946", "BAG-2947", "BAG-2948", "BAG-2949"}
SAME_SKU_EXCHANGE_PARENT_IDS = {
    "recgRB1lD5pz1XyAt",  # 1.75" Standard Belt Outer Only
    "recWdIikdBGidXXJl",  # 1.75" MOLLE Battle Belt Outer Only
}

_LP_INNER_REQUIRED_PARENT_IDS = {
    "recMx2geTxsMGq4H8",  # 1.75" Battle Belt - Aluminum COBRA® Buckle
    "recyoI521Kdbbkz6x",  # 1.75" Battle Belt - D-ring COBRA® Buckle
    "rec8H08vrdCtZ0yrn",  # 2" Duty Belt Lite - MOLLE
    "recobDC5byTEvR81w",  # 2" Duty Belt Lite - Standard
    "rec7KI77AOb7qMe0g",  # 2" MOLLE Duty Belt
    "recPHyho0XoHRQXZp",  # 2" Standard Duty Belt
}

# Display name overrides for exchange options (keyed by SKU ID)
_EXCHANGE_NAME_OVERRIDES = {
    "STA-ALC-OTH-BASE": '2" Standard - Black - Other Size - Aluminum COBRA\u00ae',
    "MOL-ALC-OTH-BASE": '2" MOLLE - Black - Other Size - Aluminum COBRA\u00ae',
}

_EXCLUDED_PARENTS = {
    # WPS / NeoMag products
    "wps lp inner", "wps low profile", "wps 1.75\" cobra", "wps 1.75\" d-ring",
    "belt lanyardwps", "sentry strapwps",
    "sentry strapneomag", "sentry strapneomag ",
    "sentry strap extensionneomag",
    "beltless backer pad - neomag", "beltless backer tegris - neomag",
    "tray insert - neomag",
    # Internal / components
    "dog collar", "dog leash", "gps pouch", "belt resize service",
    "service", "stock sock",
    "buckle", "shock cord", "scrim", "fife loop", "overlap inner",
    "med components", "med pouch handles", "packy sack parts",
    "combo packets", "sandwich bags",
    # Test / placeholder
    "test parent", "test3", "new parent", "second test parent",
    "see manager", "anklemdkt",
    # Excluded from portal
    "adapter",
    # Specific exclusions
    "1.75\" standard belt - both buckles",
    "1.75\" standard belt outer only",
    "misc.",
    "hat", "hoodie", "t-shirt",
    "sentry strap - ba",
    "fanny pack",
    "medical pouch - side pull outer",
    "medical pouch - top/bottom pull outer",
}

# Feature variations to exclude globally (lowercase)
_EXCLUDED_FEATURE_VARS_GLOBAL = {
    "base only (-onb)",
}

# Add-ons to exclude globally (lowercase add-on names)
_EXCLUDED_ADDONS_GLOBAL = {
    "one wrap",
}

# Add-ons to exclude per parent (lowercase parent name → set of lowercase add-on names)
_EXCLUDED_ADDONS_BY_PARENT = {
    "1.75\" battle belt lite": {"outer only"},
}

# Colors to exclude globally (lowercase color names)
_EXCLUDED_COLORS_GLOBAL = {
    "splatter",
    "mc arid",
    "mc tropic",
    "mc australian",
}

# Parents exempt from a global color exclusion (lowercase color → set of lowercase parent names)
_EXCLUDED_COLORS_GLOBAL_EXCEPT = {
    "mc arid":   {"thigh strap", "sandwich bags"},
    "mc tropic": {"thigh strap", "sandwich bags"},
}

# Colors to exclude per parent (lowercase parent name → set of lowercase color names)
_EXCLUDED_COLORS_BY_PARENT = {
    "radio pouch": {"wolf gray"},
}

# Display name overrides (post-clean lowercase → desired display name)
_PARENT_NAME_OVERRIDES = {
    "1.5\" lp inner only belt": "1.5\" Low Profile Inner Only Belt",
    "keepers": "Keepers (set of 4)",
    "1.75\" battle belt - aluminum cobra® buckle": "1.75\" MOLLE Battle Belt - Aluminum COBRA® Buckle",
    "1.75\" battle belt - d-ring cobra® buckle":   "1.75\" MOLLE Battle Belt - D-ring COBRA® Buckle",
}

# Color display name overrides (lowercase color name from Airtable → desired display name)
_COLOR_NAME_OVERRIDES = {
    "mc classic": "Multicam Classic",
    "mc black":   "Multicam Black",
}

# Parents that should sort first within their category (post-clean lowercase)
_SORT_FIRST_PARENTS = {
    "battle belt lite",
}

# Parents that should sort last within their category (post-clean lowercase)
_SORT_LAST_PARENTS = {
    "1.5\" low profile inner only belt",
}

@app.route("/api/admin/retry-label/<return_record_id>", methods=["POST"])
def admin_retry_label(return_record_id):
    """Re-trigger label generation + email for an existing return record."""
    read_token  = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = RETURNS_WRITE_TOKEN
    if not read_token or not write_token or not RETURNS_TABLE_ID:
        return Response(json.dumps({"ok": False, "error": "Not configured"}), status=500, mimetype="application/json")
    try:
        # Fetch the return record
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{return_record_id}",
            headers={"Authorization": f"Bearer {read_token}"}, timeout=10,
        )
        fields = r.json().get("fields", {})
        if not fields:
            return Response(json.dumps({"ok": False, "error": "Record not found or no fields"}), status=404, mimetype="application/json")

        # Address stored as "Name, Street1, City, State, PostalCode" (5-part)
        addr_str = fields.get("Confirmed Shipping Address", "")
        parts = [p.strip() for p in addr_str.split(",")]
        addr = {
            "name":       parts[0] if len(parts) > 0 else "",
            "street1":    parts[1] if len(parts) > 1 else "",
            "street2":    "",
            "city":       parts[2] if len(parts) > 2 else "",
            "state":      parts[3].strip() if len(parts) > 3 else "",
            "postalCode": parts[4].strip() if len(parts) > 4 else "",
        }
        body    = request.get_json() or {}
        # orderId not stored in Airtable — caller must supply it, or we parse from WC link
        wc_link = fields.get("WooCommerce Order Link", "")
        import re as _re_rl
        wc_id_m = _re_rl.search(r'post=(\d+)', wc_link)
        order_id = body.get("orderId") or (wc_id_m.group(1) if wc_id_m else "")
        data = {
            "orderNumber":    fields.get("Order Number", ""),
            "customerName":   fields.get("Customer Name from Shipstation", ""),
            "email":          fields.get("Email Address", ""),
            "phone":          fields.get("Phone Number", ""),
            "itemsToReturn":  fields.get("Items to Return", ""),
            "reasonForReturn":fields.get("Reason for Return", ""),
            "orderId":        order_id,
        }

        # Reset status to New so background thread can update it
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{return_record_id}",
            headers={"Authorization": f"Bearer {write_token}", "Content-Type": "application/json"},
            json={"fields": {"Status": "New", "Status Notes": "Retrying label generation"}},
            timeout=10,
        )

        # Re-run label generation in background
        def _retry(rid, d, a):
            try:
                addr_for_label = {
                    "name": a.get("name", d.get("customerName", "")),
                    "street1": a.get("street1", ""), "street2": a.get("street2", ""),
                    "city": a.get("city", ""), "state": a.get("state", ""),
                    "postalCode": a.get("postalCode", ""), "phone": d.get("phone", ""),
                }
                tracking_number, label_pdf_b64 = create_return_label(
                    d.get("orderId"), addr_for_label,
                    customer_email=d.get("email", ""), order_number=d.get("orderNumber", ""),
                )
                if not label_pdf_b64:
                    raise Exception("No PDF data returned by ShipStation")
                import time as _t
                label_url = f"{FLASK_BASE_URL}/api/return-label/{rid}"
                req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{rid}",
                    headers={"Authorization": f"Bearer {write_token}", "Content-Type": "application/json"},
                    json={"fields": {"Return Tracking #": tracking_number, "Label PDF Data": label_pdf_b64, "Return Label URL": label_url}},
                    timeout=10,
                )
                status_update = {"Status": "Needs Review", "Status Notes": "No customer email on file"}
                if d.get("email"):
                    ok, err = send_return_label_email(d["email"], d.get("customerName", ""), d.get("orderNumber", ""), label_pdf_b64)
                    status_update = {"Status": "Label Sent"} if ok else {"Status": "Needs Review", "Status Notes": f"Email failed: {err}"}
                req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{rid}",
                    headers={"Authorization": f"Bearer {write_token}", "Content-Type": "application/json"},
                    json={"fields": status_update}, timeout=10,
                )
                print(f"[retry-label] {rid} → {status_update.get('Status')}")
            except Exception as ex:
                print(f"[retry-label] failed for {rid}: {ex}")
                req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{rid}",
                    headers={"Authorization": f"Bearer {write_token}", "Content-Type": "application/json"},
                    json={"fields": {"Status": "Needs Review", "Status Notes": f"Retry failed: {ex}"}}, timeout=10,
                )

        threading.Thread(target=_retry, args=(return_record_id, data, addr), daemon=True).start()
        return Response(json.dumps({"ok": True, "message": "Label generation re-triggered"}), mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}), status=500, mimetype="application/json")


@app.route("/api/admin/delete-return-items", methods=["POST"])
def admin_delete_return_items():
    """Delete specific Return Item records by ID list."""
    write_token = RETURNS_WRITE_TOKEN
    if not write_token or not RETURN_ITEMS_TABLE_ID:
        return Response(json.dumps({"ok": False, "error": "Not configured"}), status=500, mimetype="application/json")
    body = request.get_json() or {}
    ids = body.get("ids", [])
    if not ids:
        return Response(json.dumps({"ok": False, "error": "No ids provided"}), status=400, mimetype="application/json")
    try:
        import time as _time
        deleted, failed = [], []
        for rid in ids:
            r = req_lib.delete(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}/{rid}",
                headers={"Authorization": f"Bearer {write_token}"},
                timeout=10,
            )
            (deleted if r.status_code == 200 else failed).append(rid)
            _time.sleep(0.1)
        return Response(json.dumps({"ok": True, "deleted": deleted, "failed": failed}), mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}), status=500, mimetype="application/json")


@app.route("/api/admin/refresh-catalog", methods=["POST"])
def admin_refresh_catalog():
    """Force an immediate synchronous catalog rebuild (internal use)."""
    global _CATALOG_REFRESHING
    try:
        result = _build_catalog()
        import time as _time
        _CATALOG_CACHE["data"] = result
        _CATALOG_CACHE["ts"]   = _time.time()
        _CATALOG_REFRESHING = False
        return Response(json.dumps({"ok": True, "parents": len(result["parents"]), "skus": len(result["skus"]), "ts": _CATALOG_CACHE["ts"]}),
                        mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}), status=500, mimetype="application/json")


def _catalog_response(data, extra_headers=None):
    """Return a gzip-compressed JSON response for the catalog."""
    import gzip as _gzip
    c = cors()
    # Never let the browser cache the catalog — we manage freshness server-side
    c["Cache-Control"] = "no-store"
    body = json.dumps(data).encode("utf-8")
    accept = request.headers.get("Accept-Encoding", "")
    if "gzip" in accept:
        body = _gzip.compress(body, compresslevel=6)
        c["Content-Encoding"] = "gzip"
    if extra_headers:
        c.update(extra_headers)
    return Response(body, headers=c, mimetype="application/json")


@app.route("/api/quote-catalog", methods=["GET", "OPTIONS"])
def quote_catalog():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET"})
    global _CATALOG_REFRESHING
    import time as _time
    now = _time.time()

    # Fresh cache — serve immediately
    if _CATALOG_CACHE["data"] and (now - _CATALOG_CACHE["ts"]) < _CATALOG_TTL:
        return _catalog_response({**_CATALOG_CACHE["data"], "ts": _CATALOG_CACHE["ts"]})

    # Stale cache — serve instantly and kick off a background refresh
    if _CATALOG_CACHE["data"]:
        if not _CATALOG_REFRESHING:
            _CATALOG_REFRESHING = True
            threading.Thread(target=_refresh_catalog_bg, daemon=True).start()
        return _catalog_response({**_CATALOG_CACHE["data"], "ts": _CATALOG_CACHE["ts"]})

    # No cache yet — kick off background build if not already running, return loading state
    if not _CATALOG_REFRESHING:
        _CATALOG_REFRESHING = True
        threading.Thread(target=_refresh_catalog_bg, daemon=True).start()
    return Response(json.dumps({"loading": True}),
                    headers={**cors()}, mimetype="application/json")


@app.route("/api/create-quote", methods=["POST", "OPTIONS"])
def create_quote():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    # If called from a portal session, enforce create_quote permission
    _pu = get_portal_user(request)
    if _pu is not None and not portal_can(_pu, "create_quote"):
        return Response(json.dumps({"error": "Insufficient permissions"}), status=403, headers=c, mimetype="application/json")
    from datetime import date as dt_date, timedelta
    data = request.get_json() or {}
    token = RETURNS_WRITE_TOKEN

    org_name   = (data.get("orgName") or "").strip()
    email      = (data.get("email") or "").strip()
    items      = data.get("items", [])
    # Contract portal orders are Sales Orders; legacy/admin submissions default to Quote
    order_type = (data.get("orderType") or "Quote").strip()
    if order_type not in ("Quote", "Sales Order"):
        order_type = "Quote"

    if not org_name or not email or not items:
        return Response(json.dumps({"error": "orgName, email, and items are required"}),
                        status=400, headers=c, mimetype="application/json")

    contact_name = (data.get("contactName") or "").strip()
    phone        = (data.get("phone") or "").strip()
    address1     = (data.get("address1") or "").strip()
    address2     = (data.get("address2") or "").strip()
    city         = (data.get("city") or "").strip()
    state        = (data.get("state") or "").strip()
    zip_code     = (data.get("zip") or "").strip()
    country      = (data.get("country") or "US").strip()
    po_number    = (data.get("poNumber") or "").strip()
    notes        = (data.get("notes") or "").strip()

    provided_cust_id = (data.get("customerId") or "").strip()

    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        # 1. Use provided customer ID if available, otherwise find/create by email
        # Build address lines (shared by both paths)
        _line1 = address1
        if address2:
            _line1 = f"{address1}, {address2}"
        _line2 = ""
        if city and state and zip_code:
            _line2 = f"{city}, {state} {zip_code}"
        elif city or state or zip_code:
            _line2 = " ".join(filter(None, [city, state, zip_code]))

        if provided_cust_id:
            cust_id = provided_cust_id
            # Do NOT update the customer record from the quote form.
            # Address/phone entered here go to snapshot fields on the MO record only.
            # Customer address should only be updated via the Team tab.
        else:
          existing = at_get_all(
            CUSTOMERS_TABLE_ID, read_token,
            fields=["Main Contact Email", "Organization Name"],
            formula=f"{{Main Contact Email}}='{email}'",
          )
          if existing:
            cust_id = existing[0]["id"]
          else:
            new_cust = {
                "Organization Name":      org_name,
                "Main Contact Name":      contact_name,
                "Main Contact Email":     email,
            }
            if phone:    new_cust["Main Contact Phone #"]        = phone
            if _line1:   new_cust["Customer Address (Line 1)"]   = _line1
            if _line2:   new_cust["Customer Address (Line 2)"]   = _line2

            cr = req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}",
                headers={**at_headers(token), "Content-Type": "application/json"},
                json={"fields": new_cust},
                timeout=15,
            )
            cr.raise_for_status()
            cust_id = cr.json()["id"]

        # 2. Get next order ID — use read token (write token may lack read scope)
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        order_id_str = _next_order_id(read_token)
        doc_prefix   = "SO" if order_type == "Sales Order" else "QU"
        quote_number = f"{doc_prefix}-{order_id_str}"

        today     = _today_utc()
        today_str = today.isoformat()
        # Expiry only applies to Quotes
        expiry_str = ""
        if order_type == "Quote":
            expiry_str = (today + timedelta(days=90)).isoformat()

        # 3. Create Manual Order
        # Collect per-item size notes and append to order notes
        item_size_notes = []
        for it in items:
            sn = (it.get("sizeNote") or "").strip()
            if sn:
                item_name = (it.get("name") or f"Item {items.index(it)+1}").strip()
                item_size_notes.append(f"  - {item_name}: {sn}")
        if item_size_notes:
            size_note_block = "Size/fit specs:\n" + "\n".join(item_size_notes)
            notes = (notes + "\n\n" + size_note_block).strip() if notes else size_note_block

        # Build snapshot addr 2 from city/state/zip
        _snap_addr2 = ""
        if city and state:
            _snap_addr2 = f"{city}, {state} {zip_code}".strip()
        elif city or state or zip_code:
            _snap_addr2 = " ".join(filter(None, [city, state, zip_code]))

        mo_body = {
            "fields": {
                "Order Type":       order_type,
                "Order ID":         order_id_str,
                "Date":             today_str,
                "Customer":         [cust_id],
                # Sales Orders from the portal are pre-approved (vetted contract customers)
                **({"Sales Order Status": "Approved"} if order_type == "Sales Order" else {}),
                # Snapshot billing/shipping — stored directly so customer record changes
                # never alter this quote's displayed info
                "Snapshot Org":     org_name,
                "Snapshot Contact": contact_name,
                "Snapshot Email":   email,
                "Snapshot Phone":   phone,
                "Snapshot Addr 1":  address1,
                "Snapshot Addr 2":  _snap_addr2,
            }
        }
        if po_number:
            mo_body["fields"]["Purchase Order #"] = po_number
        if notes:
            mo_body["fields"]["Notes from Customer"] = notes
        if expiry_str:
            mo_body["fields"]["Expiry Date"] = expiry_str

        mo_r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}",
            headers={**at_headers(token), "Content-Type": "application/json"},
            json=mo_body,
            timeout=15,
        )
        if not mo_r.ok:
            err_detail = ""
            try: err_detail = mo_r.json()
            except Exception: err_detail = mo_r.text
            print(f"[create_quote] MO creation failed {mo_r.status_code}: {err_detail}")
            return Response(json.dumps({"error": f"Order creation failed ({mo_r.status_code}): {err_detail}"}),
                            status=500, headers=c, mimetype="application/json")
        mo_record_id = mo_r.json()["id"]

        # 4. Create line items
        for item in items:
            sku_id = item.get("skuRecordId") or item.get("recordId", "")
            li_r = req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}",
                headers={**at_headers(token), "Content-Type": "application/json"},
                json={"fields": {
                    "Manual Order":               [mo_record_id],
                    "Product SKU":                [sku_id],
                    "Qty.":                       int(item["qty"]),
                    "Confirmed Unit Price":       float(item["unitPrice"]),
                    "Confirmed Adj. Unit Price":  float(item["unitPrice"]),
                }},
                timeout=15,
            )
            if not li_r.ok:
                err_detail = ""
                try: err_detail = li_r.json()
                except Exception: err_detail = li_r.text
                print(f"[create_quote] line item creation failed {li_r.status_code}: {err_detail} | item={item}")
            li_r.raise_for_status()

        # 5. Upload PO document to Airtable if provided
        pdf_b64  = (data.get("pdfBase64") or "").strip()
        pdf_name = (data.get("pdfFilename") or "po-document.pdf").strip()
        if pdf_b64:
            try:
                import base64 as _b64, io as _io
                pdf_bytes = _b64.b64decode(pdf_b64)
                upload_r = req_lib.post(
                    f"https://content.airtable.com/v0/{AIRTABLE_BASE_ID}/{mo_record_id}/fldLJ5x2nJhGVuzKv/uploadAttachment",
                    headers={"Authorization": f"Bearer {token}"},
                    files={"file": (pdf_name, _io.BytesIO(pdf_bytes), "application/pdf")},
                    timeout=30,
                )
                if not upload_r.ok:
                    print(f"[create_quote] PDF upload failed {upload_r.status_code}: {upload_r.text[:200]}")
            except Exception as pdf_err:
                print(f"[create_quote] PDF upload error: {pdf_err}")

        # 6. Send email with PDF attachment
        try:
            quote_data = _fetch_quote_data(mo_record_id)
            send_quote_email(email, contact_name or org_name, org_name,
                             quote_number, mo_record_id, expiry_str, quote_data=quote_data)
        except Exception as email_err:
            print(f"[create_quote] email failed: {email_err}")

        # Bust quotes cache for this customer
        _QUOTES_CACHE.pop(cust_id, None)
        _ORDERS_CACHE.pop(cust_id, None)

        return Response(
            json.dumps({"success": True, "quoteNumber": quote_number, "recordId": mo_record_id}),
            headers=c, mimetype="application/json",
        )
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/get-quote/<record_id>", methods=["GET", "OPTIONS"])
def get_quote(record_id):
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET"})
    c = cors()
    try:
        data = _fetch_quote_data(record_id)
        if not data:
            return Response(json.dumps({"error": "Quote not found"}), status=404, headers=c, mimetype="application/json")
        return Response(json.dumps(data), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/update-quote/<record_id>", methods=["POST", "OPTIONS"])
def update_quote(record_id):
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    # If called from a portal session, enforce create_quote permission
    _pu = get_portal_user(request)
    if _pu is not None and not portal_can(_pu, "create_quote"):
        return Response(json.dumps({"error": "Insufficient permissions"}), status=403, headers=c, mimetype="application/json")
    write_token = RETURNS_WRITE_TOKEN
    # Use broadest available read token for verification (write token may lack read scope)
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    data = request.get_json() or {}
    items = data.get("items", [])
    notes = (data.get("notes") or "").strip()

    try:
        # Verify it's a quote and not accepted
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=15,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Quote not found"}), status=404, headers=c, mimetype="application/json")
        mo_fields = r.json().get("fields", {})
        if mo_fields.get("Order Type") != "Quote":
            return Response(json.dumps({"error": "Not a quote"}), status=400, headers=c, mimetype="application/json")
        if mo_fields.get("MO Is Approved"):
            return Response(json.dumps({"error": "Quote already accepted"}), status=400, headers=c, mimetype="application/json")

        # Delete existing line items in batches of 10 (Airtable limit)
        existing_li_ids = mo_fields.get("MO Line Items", [])
        for i in range(0, len(existing_li_ids), 10):
            batch = existing_li_ids[i:i+10]
            params = "&".join(f"records[]={lid}" for lid in batch)
            req_lib.delete(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}?{params}",
                headers=at_headers(write_token), timeout=15,
            )

        # Create new line items in batches of 10
        for i in range(0, len(items), 10):
            batch = items[i:i+10]
            li_r = req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"records": [{"fields": {
                    "Manual Order":              [record_id],
                    "Product SKU":               [item["skuRecordId"]],
                    "Qty.":                      int(item["qty"]),
                    "Confirmed Unit Price":      float(item["unitPrice"]),
                    "Confirmed Adj. Unit Price": float(item["unitPrice"]),
                }} for item in batch]},
                timeout=15,
            )
            li_r.raise_for_status()


        # Save notes back to the MO record
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {"Notes from Customer": notes}},
            timeout=10,
        )
        return Response(json.dumps({"success": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/accept-quote/<record_id>", methods=["POST", "OPTIONS"])
def accept_quote(record_id):
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    # If called from a portal session, enforce accept_quote permission
    _pu = get_portal_user(request)
    if _pu is not None and not portal_can(_pu, "accept_quote"):
        return Response(json.dumps({"error": "Insufficient permissions"}), status=403, headers=c, mimetype="application/json")
    from datetime import date as dt_date
    write_token = RETURNS_WRITE_TOKEN
    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or write_token
    token       = write_token  # used for all write operations below

    data = request.get_json() or {}
    billing      = data.get("billing") or {}
    shipping_obj = data.get("shipping")  # may be None (same as billing)
    po_override  = (data.get("poNumber") or "").strip()  # optional PO # submitted from modal
    pdf_b64      = (data.get("pdfBase64") or "").strip()
    pdf_name     = (data.get("pdfFilename") or "po-document.pdf").strip()

    try:
        # Fetch MO record
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=15,
        )
        if r.status_code != 200:
            err_body = ""
            try: err_body = r.json()
            except Exception: err_body = r.text[:200]
            return Response(json.dumps({"error": f"Quote not found (AT {r.status_code}: {err_body})"}), status=404, headers=c, mimetype="application/json")
        mo_fields = r.json().get("fields", {})

        if mo_fields.get("Order Type") != "Quote":
            return Response(json.dumps({"error": "Not a quote"}), status=400, headers=c, mimetype="application/json")
        if mo_fields.get("MO Is Approved"):
            return Response(json.dumps({"error": "Already accepted"}), status=400, headers=c, mimetype="application/json")

        expiry_str = mo_fields.get("Expiry Date", "")
        if expiry_str:
            try:
                if dt_date.fromisoformat(expiry_str) < _today_utc():
                    return Response(json.dumps({"error": "Quote has expired"}), status=400, headers=c, mimetype="application/json")
            except Exception:
                pass

        order_id_str = mo_fields.get("Order ID", "")
        quote_number = mo_fields.get("Document ID", f"QU-{order_id_str}")
        so_number    = f"SO-{order_id_str}"
        customer_ids = mo_fields.get("Customer", [])
        customer_id  = customer_ids[0] if customer_ids else None
        po_number    = po_override or mo_fields.get("Purchase Order #", "")
        notes        = mo_fields.get("Notes from Customer", "")
        date_str     = mo_fields.get("Date", _today_utc().isoformat())

        # Create SO record
        # Note: Document ID, MO Is Approved, Ready for ShipStation (SOs), Origin Quote are all
        # formula fields — Airtable computes them automatically. Do NOT write them.
        # Sales Order Status = "Approved" drives both MO Is Approved and Ready for ShipStation.
        # Carry snapshot fields from the quote to the SO — prefer billing form data if provided,
        # otherwise inherit whatever was stored on the quote record
        _b_org     = (billing.get("org")   or "").strip() or mo_fields.get("Snapshot Org", "")
        _b_name    = (billing.get("name")  or "").strip() or mo_fields.get("Snapshot Contact", "")
        _b_email   = (billing.get("email") or "").strip() or mo_fields.get("Snapshot Email", "")
        _b_phone   = (billing.get("phone") or "").strip() or mo_fields.get("Snapshot Phone", "")
        _b_addr1   = (billing.get("addr1") or "").strip()
        if billing.get("addr2"): _b_addr1 = f"{_b_addr1}, {billing['addr2']}".strip(" ,")
        if not _b_addr1: _b_addr1 = mo_fields.get("Snapshot Addr 1", "")
        _b_city    = (billing.get("city",  "") or "").strip()
        _b_state   = (billing.get("state", "") or "").strip()
        _b_zip     = (billing.get("zip",   "") or "").strip()
        if _b_city and _b_state:
            _b_addr2 = f"{_b_city}, {_b_state} {_b_zip}".strip()
        elif _b_city or _b_state or _b_zip:
            _b_addr2 = " ".join(filter(None, [_b_city, _b_state, _b_zip]))
        else:
            _b_addr2 = mo_fields.get("Snapshot Addr 2", "")

        so_body = {
            "fields": {
                "Order Type":         "Sales Order",
                "Order ID":           order_id_str,
                "Date":               date_str,
                "Sales Order Status": "Approved",
                "Snapshot Org":       _b_org,
                "Snapshot Contact":   _b_name,
                "Snapshot Email":     _b_email,
                "Snapshot Phone":     _b_phone,
                "Snapshot Addr 1":    _b_addr1,
                "Snapshot Addr 2":    _b_addr2,
            }
        }
        if customer_ids:
            so_body["fields"]["Customer"] = customer_ids
        if po_number:
            so_body["fields"]["Purchase Order #"] = po_number
        if notes:
            so_body["fields"]["Notes from Customer"] = notes

        so_r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}",
            headers={**at_headers(token), "Content-Type": "application/json"},
            json=so_body,
            timeout=15,
        )
        if not so_r.ok:
            try:
                at_err = so_r.json()
            except Exception:
                at_err = so_r.text[:500]
            return Response(json.dumps({"error": f"SO create failed ({so_r.status_code}): {at_err}"}),
                            status=500, headers=c, mimetype="application/json")
        so_record_id = so_r.json()["id"]

        # PATCH QU to Approved immediately after SO is confirmed created.
        # Doing this early (before line items / email) ensures the quote is
        # marked accepted if-and-only-if the SO exists. Line items and email
        # are non-fatal and can recover, but a missing SO with an accepted QU
        # is the worst failure mode.
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers={**at_headers(token), "Content-Type": "application/json"},
            json={"fields": {"Quote Status": "Approved"}},
            timeout=15,
        )

        # Upload PO document to SO record if provided
        if pdf_b64:
            try:
                import base64 as _b64, io as _io
                pdf_bytes = _b64.b64decode(pdf_b64)
                upload_r = req_lib.post(
                    f"https://content.airtable.com/v0/{AIRTABLE_BASE_ID}/{so_record_id}/fldLJ5x2nJhGVuzKv/uploadAttachment",
                    headers={"Authorization": f"Bearer {token}"},
                    files={"file": (pdf_name, _io.BytesIO(pdf_bytes), "application/pdf")},
                    timeout=30,
                )
                if not upload_r.ok:
                    print(f"[accept_quote] PDF upload failed {upload_r.status_code}: {upload_r.text[:200]}")
            except Exception as pdf_err:
                print(f"[accept_quote] PDF upload error: {pdf_err}")

        # Update customer address only (never org name or contact name — those are shared
        # across all documents and Chrome autofill can corrupt them)
        if billing and customer_id:
            bill_line1 = billing.get("addr1", "")
            if billing.get("addr2"):
                bill_line1 = f"{bill_line1}, {billing['addr2']}"
            bill_line2 = ""
            city_  = billing.get("city", "")
            state_ = billing.get("state", "")
            zip_   = billing.get("zip", "")
            if city_ and state_:
                bill_line2 = f"{city_}, {state_} {zip_}".strip()
            cust_update = {}
            if bill_line1: cust_update["Customer Address (Line 1)"] = bill_line1
            if bill_line2: cust_update["Customer Address (Line 2)"] = bill_line2
            if cust_update:
                try:
                    req_lib.patch(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
                        headers={**at_headers(token), "Content-Type": "application/json"},
                        json={"fields": cust_update},
                        timeout=15,
                    )
                except Exception as cust_err:
                    print(f"[accept_quote] customer update failed: {cust_err}")

        # Add ship-to to SO notes if different from billing
        if shipping_obj:
            ship_note = "Ship To: {org} {name}, {addr1}{addr2}, {city}, {state} {zip}".format(
                org   = shipping_obj.get("org", ""),
                name  = shipping_obj.get("name", ""),
                addr1 = shipping_obj.get("addr1", ""),
                addr2 = f", {shipping_obj['addr2']}" if shipping_obj.get("addr2") else "",
                city  = shipping_obj.get("city", ""),
                state = shipping_obj.get("state", ""),
                zip   = shipping_obj.get("zip", ""),
            ).strip()
            try:
                req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{so_record_id}",
                    headers={**at_headers(token), "Content-Type": "application/json"},
                    json={"fields": {"Notes from Customer": ship_note}},
                    timeout=15,
                )
            except Exception:
                pass

        # Copy line items from QU to SO
        # Use the MO Line Items IDs already on the quote record (formula query doesn't work
        # because ARRAYJOIN on linked records returns display names, not record IDs)
        li_ids = mo_fields.get("MO Line Items", [])
        for li_id in li_ids:
            li_r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                headers=at_headers(read_token), timeout=10,
            )
            if li_r.status_code != 200:
                print(f"[accept_quote] could not fetch line item {li_id}: {li_r.status_code}")
                continue
            lf = li_r.json().get("fields", {})
            # Adj. Unit Price is a formula — use Confirmed Unit Price if set, else Unit Price lookup
            price = lf.get("Confirmed Unit Price") or (lf.get("Unit Price") or [None])[0] or 0
            new_li = req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}",
                headers={**at_headers(token), "Content-Type": "application/json"},
                json={"fields": {
                    "Manual Order":              [so_record_id],
                    "Product SKU":               lf.get("Product SKU", []),
                    "Qty.":                      lf.get("Qty.", 0),
                    "Confirmed Unit Price":      float(price),
                    "Confirmed Adj. Unit Price": float(price),
                }},
                timeout=15,
            )
            if not new_li.ok:
                print(f"[accept_quote] line item copy failed {new_li.status_code}: {new_li.text[:200]}")

        # Send confirmation email
        to_email = ""
        to_name  = ""
        org_name = ""
        if customer_ids:
            try:
                cr = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_ids[0]}",
                    headers=at_headers(token), timeout=10,
                )
                if cr.status_code == 200:
                    cf = cr.json().get("fields", {})
                    to_email = cf.get("Main Contact Email", "")
                    to_name  = cf.get("Main Contact Name", "")
                    org_name = cf.get("Organization Name", "")
            except Exception:
                pass
        if to_email:
            try:
                send_quote_accepted_email(to_email, to_name, org_name, quote_number, so_number)
            except Exception as email_err:
                print(f"[accept_quote] email failed: {email_err}")

        # Bust quotes cache
        if customer_id:
            _QUOTES_CACHE.pop(customer_id, None)
        _ORDERS_CACHE.pop(customer_id, None)

        return Response(json.dumps({"success": True, "soNumber": so_number}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


def _build_quote_pdf_bytes(quote, doc_type="quote"):
    """Generate PDF bytes for a quote dict. Returns bytes."""
    import os, tempfile
    from fpdf import FPDF

    def _fmt_date(iso_str):
        """Convert YYYY-MM-DD to M-D-YYYY."""
        try:
            from datetime import date as _d
            d = _d.fromisoformat(iso_str)
            return f"{d.month}-{d.day}-{d.year}"
        except Exception:
            return iso_str

    cust       = quote.get("customer", {})
    line_items = quote.get("lineItems", [])
    subtotal   = quote.get("subtotal", 0.0)
    shipping   = quote.get("shipping", 0.0)
    total      = quote.get("total",    0.0)
    q_number   = quote.get("quoteNumber", "")
    q_date     = _fmt_date(quote.get("date", ""))
    q_expiry   = _fmt_date(quote.get("expiryDate", ""))
    q_po       = quote.get("poNumber", "")
    q_notes    = quote.get("notes", "")
    q_record   = quote.get("recordId", "")
    portal_link = (f"{QUOTE_BASE_URL}/portal?tab=our-orders&order={q_record}" if doc_type == "order"
                   else f"{QUOTE_BASE_URL}/portal?tab=our-quotes&quote={q_record}") if q_record else ""

    bill_org   = cust.get("billToOrg")  or cust.get("orgName", "")
    bill_name  = cust.get("billToName") or cust.get("contactName", "")
    bill_addr1 = cust.get("billToAddr1", "")
    bill_addr2 = cust.get("billToAddr2", "")
    addr1      = cust.get("address1", "")
    city       = cust.get("city", "")
    state_v    = cust.get("state", "")
    zip_v      = cust.get("zip", "")

    # Subclass for small header on pages 2+
    _static_dir    = os.path.dirname(os.path.abspath(__file__))
    logo_local_ref = None
    for _c in ["ba-logo-white-bg.jpg", "ba-logo-dark.png", "ba-logo.jpg"]:
        _p = os.path.join(_static_dir, "static", _c)
        if os.path.exists(_p):
            logo_local_ref = _p
            break
    _qnum_ref = q_number

    class QuotePDF(FPDF):
        def header(self):
            if self.page_no() <= 1:
                return
            # Small logo + quote # for continuation pages
            self.set_fill_color(255, 255, 255)
            self.rect(19, 6, 28, 10, style="F")
            try:
                self.image(logo_local_ref, x=19, y=6, w=28)
            except Exception:
                self.set_xy(19, 8)
                self.set_font("Helvetica", "B", 9)
                self.set_text_color(27, 36, 56)
                self.cell(28, 5, "BLUE ALPHA", border=0)
            self.set_xy(49, 9)
            self.set_font("Helvetica", "B", 8)
            self.set_text_color(27, 36, 56)
            self.cell(0, 5, _qnum_ref, border=0)
            self.set_draw_color(27, 36, 56)
            self.set_line_width(0.4)
            self.line(19, 18, 19 + 177, 18)
            self.ln(4)

    pdf = QuotePDF(orientation="P", unit="mm", format="letter")
    pdf.set_margins(19, 19, 19)
    pdf.set_auto_page_break(auto=True, margin=25)
    pdf.add_page()

    W     = 177.0   # usable width (215.9mm - 2×19mm margins)
    NAVY  = (27,  36,  56)
    MUTED = (107, 122, 141)
    TEXT  = (26,  38,  51)
    LG    = (245, 247, 250)
    BD    = (221, 227, 234)

    # ── Logo (top-left) ───────────────────────────────────────────────
    LOGO_TOP           = 13.0          # mm from top of page
    LOGO_W             = 40.0          # fixed logo width in mm
    _PIXEL_ASPECT      = 726 / 1600    # image h/w ratio (ba-logo-white-bg.jpg)
    _LOGO_CONTENT_FRAC = 708 / 726     # fraction of image height containing real content
    _logo_h            = LOGO_W * _PIXEL_ASPECT                    # rendered height in mm
    HEADER_BOTTOM      = LOGO_TOP + _logo_h * _LOGO_CONTENT_FRAC   # visual logo bottom — anchor for all 3 elements
    # Prefer transparent PNG; fall back to JPG, then URL download, then text
    _static    = os.path.dirname(os.path.abspath(__file__))
    logo_url   = "https://www.bluealphabelts.com/wp-content/uploads/2024/04/logo-1.png"
    logo_tmp   = None
    # Prefer white-bg jpg (clean navy logo), then fallbacks
    for _candidate in ["ba-logo-white-bg.jpg", "ba-logo-dark.png", "ba-logo.jpg"]:
        _path = os.path.join(_static, "static", _candidate)
        if os.path.exists(_path):
            logo_file = _path
            break
    else:
        logo_file = None
    try:
        if not logo_file:
            import urllib.request
            logo_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            urllib.request.urlretrieve(logo_url, logo_tmp.name)
            logo_file = logo_tmp.name
        pdf.image(logo_file, x=19, y=LOGO_TOP, w=LOGO_W, h=_logo_h)
    except Exception:
        # Fallback: text logo
        pdf.set_xy(19, LOGO_TOP)
        pdf.set_font("Helvetica", "B", 18)
        pdf.set_text_color(*NAVY)
        pdf.cell(LOGO_W, 10, "BLUE ALPHA", border=0)
    finally:
        if logo_tmp:
            try: os.unlink(logo_tmp.name)
            except Exception: pass

    # ── Middle: company address ────────────────────────────────────────
    mid_x = 19 + LOGO_W + 6
    mid_w = W * 0.36
    addr_lines = [
        ("Blue Alpha",       True),
        ("35 Andrew St.",    False),
        ("Newnan, GA 30263", False),
    ]
    addr_line_h = 4.5
    # Position so the last line sits at the logo bottom
    addr_y = HEADER_BOTTOM - len(addr_lines) * addr_line_h  # bottom-align to HEADER_BOTTOM
    for cline, bold in addr_lines:
        pdf.set_xy(mid_x, addr_y)
        pdf.set_font("Helvetica", "B" if bold else "", 8)
        pdf.set_text_color(*TEXT if bold else MUTED)
        pdf.cell(mid_w, addr_line_h, cline, border=0, new_x="LMARGIN", new_y="NEXT")
        addr_y += addr_line_h

    # ── Right: doc type heading + quote/order number ───────────────────
    right_x = 19 + W * 0.62
    right_w = W * 0.38

    _doc_label   = "SALES ORDER" if doc_type == "order" else "QUOTE"
    _num_cell_h  = 5.5
    _quote_cell_h = 12.0
    _quote_gap    = 1.0
    # Pin both cells to HEADER_BOTTOM so they align with logo and address
    _quote_start = HEADER_BOTTOM - _num_cell_h - _quote_gap - _quote_cell_h

    pdf.set_xy(right_x, _quote_start)
    pdf.set_font("Helvetica", "B", 28)
    pdf.set_text_color(*NAVY)
    pdf.cell(right_w, _quote_cell_h, _doc_label, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_xy(right_x, HEADER_BOTTOM - _num_cell_h)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*NAVY)
    pdf.cell(right_w, _num_cell_h, f"#{q_number}", align="R", new_x="LMARGIN", new_y="NEXT")

    # Move cursor below header block — everything bottoms out at 32mm, tight gap before divider
    pdf.set_y(HEADER_BOTTOM + 4)  # tight gap after aligned header

    # Navy divider
    pdf.set_draw_color(*NAVY)
    pdf.set_line_width(0.7)
    pdf.line(19, pdf.get_y(), 19 + W, pdf.get_y())
    pdf.ln(5)

    # ── Bill To | Ship To | Quote Details (3 equal columns) ──────────
    y_info  = pdf.get_y()
    col3_w  = W / 3.0
    bill_x  = 19
    ship_x  = 19 + col3_w
    meta_x  = 19 + col3_w * 2

    # --- Bill To ---
    pdf.set_xy(bill_x, y_info)
    pdf.set_font("Helvetica", "B", 7)
    pdf.set_text_color(*MUTED)
    pdf.cell(col3_w, 5.5, "BILL TO", border=0, new_x="LEFT", new_y="NEXT")
    for ln in [bill_org, bill_name, bill_addr1, bill_addr2]:
        if ln:
            pdf.set_x(bill_x)
            pdf.set_font("Helvetica", "B" if ln == bill_org else "", 8)
            pdf.set_text_color(*TEXT)
            pdf.cell(col3_w, 5, ln, border=0, new_x="LEFT", new_y="NEXT")
    y_after_bill = pdf.get_y()

    # --- Ship To ---
    ship_org_display = cust.get("shipToOrg") or cust.get("orgName", "")
    ship_lines = [
        ship_org_display,
        cust.get("contactName", ""),
        addr1,
        ", ".join(filter(None, [city, f"{state_v} {zip_v}".strip()])),
    ]
    pdf.set_xy(ship_x, y_info)
    pdf.set_font("Helvetica", "B", 7)
    pdf.set_text_color(*MUTED)
    pdf.cell(col3_w, 5.5, "SHIP TO", border=0, new_x="LEFT", new_y="NEXT")
    for ln in ship_lines:
        if ln:
            pdf.set_x(ship_x)
            pdf.set_font("Helvetica", "B" if ln == ship_org_display else "", 8)
            pdf.set_text_color(*TEXT)
            pdf.cell(col3_w, 5, ln, border=0, new_x="LEFT", new_y="NEXT")
    y_after_ship = pdf.get_y()

    # --- Quote / Order Details ---
    def meta_kv(label, value):
        pdf.set_x(meta_x)
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_text_color(*MUTED)
        pdf.cell(col3_w, 5, label.upper(), border=0, new_x="LEFT", new_y="NEXT")
        pdf.set_x(meta_x)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*TEXT)
        pdf.cell(col3_w, 5, str(value), border=0, new_x="LEFT", new_y="NEXT")
        pdf.set_x(meta_x)
        pdf.set_y(pdf.get_y() + 1)

    pdf.set_xy(meta_x, y_info)
    if doc_type == "order":
        meta_kv("Order Date",   q_date)
        if q_po:
            meta_kv("PO #", q_po)
    else:
        meta_kv("Quote Date",   q_date)
        if q_expiry:
            meta_kv("Expiry Date", q_expiry)
        if q_po:
            meta_kv("PO #", q_po)
    y_after_meta = pdf.get_y()

    pdf.set_y(max(y_after_bill, y_after_ship, y_after_meta) + 4)

    # Validity note
    pdf.ln(2)

    # ── Line items table ──────────────────────────────────────────────
    sku_w  = 36
    name_w = W - sku_w - 14 - 25 - 25
    col_widths = [sku_w, name_w, 14, 25, 25]
    headers    = ["SKU", "DESCRIPTION", "QTY", "UNIT PRICE", "TOTAL"]
    aligns     = ["L",   "L",           "R",   "R",          "R"    ]
    row_h      = 7    # minimum row height (single-line items)
    line_h     = 4.5  # tighter line height inside wrapped descriptions

    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 8)
    for w, h, a in zip(col_widths, headers, aligns):
        pdf.cell(w, row_h, h, border=0, align=a, fill=True)
    pdf.ln()

    def _redraw_table_header():
        pdf.set_fill_color(*NAVY)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 8)
        for w, h, a in zip(col_widths, headers, aligns):
            pdf.cell(w, row_h, h, border=0, align=a, fill=True)
        pdf.ln()
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*TEXT)

    pdf.set_font("Helvetica", "", 8)
    for idx, item in enumerate(line_items):
        pdf.set_font("Helvetica", "", 8)   # force-reset each row — prevents state bleed
        pdf.set_text_color(*TEXT)
        line_total = item["qty"] * item["unitPrice"]
        sku_val   = item.get("skuId", "")
        name_val  = item.get("name", "")
        qty_val   = str(item["qty"])
        price_val = f"${item['unitPrice']:.2f}"
        total_val = f"${line_total:.2f}"
        fill_row  = idx % 2 == 0

        row_y = pdf.get_y()

        # ── Step 1: measure actual text height (auto-break off so we don't flip pages) ──
        pdf.set_auto_page_break(auto=False)
        pdf.set_xy(19 + sku_w, row_y)
        pdf.multi_cell(name_w, line_h, name_val, border=0, align="L")
        text_height  = pdf.get_y() - row_y
        pdf.set_auto_page_break(auto=True, margin=25)

        # Row height = num_lines × row_h (keeps original item spacing)
        # Text is vertically centered within the taller row
        num_lines    = max(1, round(text_height / line_h))
        actual_row_h = num_lines * row_h
        actual_end_y = row_y + actual_row_h
        desc_y       = row_y + (actual_row_h - text_height) / 2  # vertical center

        # ── Step 2: page break if this row overruns the trigger ──
        if actual_end_y > pdf.page_break_trigger:
            pdf.add_page()
            _redraw_table_header()
            row_y = pdf.get_y()
            # Re-measure on new page
            pdf.set_auto_page_break(auto=False)
            pdf.set_xy(19 + sku_w, row_y)
            pdf.multi_cell(name_w, line_h, name_val, border=0, align="L")
            text_height  = pdf.get_y() - row_y
            pdf.set_auto_page_break(auto=True, margin=25)
            num_lines    = max(1, round(text_height / line_h))
            actual_row_h = num_lines * row_h
            actual_end_y = row_y + actual_row_h
            desc_y       = row_y + (actual_row_h - text_height) / 2

        # ── Step 3: background rect covers the measurement render for all rows ──
        #    (white for non-fill rows, light-gray for fill rows)
        pdf.set_fill_color(*(LG if fill_row else (255, 255, 255)))
        pdf.rect(19, row_y, W, actual_row_h, style="F")

        # ── Step 4: re-render description at vertically-centered position ──
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*TEXT)
        pdf.set_xy(19 + sku_w, desc_y)
        pdf.multi_cell(name_w, line_h, name_val, border=0, align="L")

        # ── Step 5: SKU + numeric columns (full row height) ──
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*TEXT)
        pdf.set_xy(19, row_y)
        pdf.cell(sku_w, actual_row_h, sku_val, border=0, align="L")

        pdf.set_xy(19 + sku_w + name_w, row_y)
        pdf.cell(col_widths[2], actual_row_h, qty_val,   border=0, align="R")
        pdf.cell(col_widths[3], actual_row_h, price_val, border=0, align="R")
        pdf.cell(col_widths[4], actual_row_h, total_val, border=0, align="R")

        pdf.set_y(actual_end_y)

    pdf.ln(3)

    # ── Totals ────────────────────────────────────────────────────────
    def totals_row(label, value, bold=False):
        pdf.set_font("Helvetica", "B" if bold else "", 9)
        pdf.cell(W - 50, 6, "", border=0)
        pdf.set_text_color(*TEXT if bold else MUTED)
        pdf.cell(25, 6, label, border=0, align="R")
        pdf.set_text_color(*TEXT)
        pdf.cell(25, 6, value, border=0, align="R", new_x="LMARGIN", new_y="NEXT")

    if shipping > 0:
        totals_row("Shipping", f"${shipping:.2f}")
    y_rule = pdf.get_y()
    pdf.set_draw_color(*NAVY)
    pdf.set_line_width(0.4)
    pdf.line(19 + W - 50, y_rule, 19 + W, y_rule)
    pdf.ln(1)
    totals_row("Total", f"${total:.2f}", bold=True)
    pdf.ln(5)

    # ── Notes + Footer pinned to bottom ──────────────────────────────
    clean_notes = (q_notes or "").strip()
    clean_notes = clean_notes if clean_notes.lower() not in ("no notes", "none", "n/a", "") else ""

    # Pin footer to bottom of current page; disable auto page break so it never overflows
    PAGE_H     = 279.4   # letter height in mm
    BOT_MARGIN = 6       # mm from bottom for footer start
    footer_h   = 20 + (10 if clean_notes else 0)
    footer_y   = PAGE_H - BOT_MARGIN - footer_h
    # Only jump down if we're above the footer zone (never jump backward past content)
    if pdf.get_y() < footer_y:
        pdf.set_y(footer_y)
    pdf.set_auto_page_break(auto=False)

    if clean_notes:
        pdf.set_draw_color(*BD)
        pdf.set_line_width(0.3)
        pdf.line(19, pdf.get_y(), 19 + W, pdf.get_y())
        pdf.ln(1)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*MUTED)
        pdf.cell(W, 4, "NOTES", border=0, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*TEXT)
        pdf.multi_cell(W, 4, clean_notes, border=0)
        pdf.ln(1)

    # Footer divider
    pdf.set_draw_color(*BD)
    pdf.set_line_width(0.3)
    pdf.line(19, pdf.get_y(), 19 + W, pdf.get_y())
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*MUTED)

    # Line 1: payment terms / order confirmation
    if doc_type == "order":
        pdf.write(4.5, "Thank you for your order. ")
        if portal_link:
            pdf.write(4.5, "View your order at ")
            pdf.set_text_color(91, 127, 160)
            pdf.set_font("Helvetica", "U", 7.5)
            pdf.write(4.5, "the Blue Alpha Portal", link=portal_link)
            pdf.set_font("Helvetica", "", 7.5)
            pdf.set_text_color(*MUTED)
            pdf.write(4.5, ".")
    else:
        pdf.write(4.5, "Ready to place your order? ")
        pdf.set_text_color(91, 127, 160)   # Steel Blue
        pdf.set_font("Helvetica", "U", 7.5)
        pdf.write(4.5, "Accept this quote in your Blue Alpha Portal", link=portal_link)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*MUTED)
        pdf.write(4.5, "  Payment terms are Net 30 upon acceptance.")
    pdf.ln(5)

    # Line 2: questions
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*MUTED)
    pdf.cell(W, 4.5,
        "Questions? Contact us at orders@bluealpha.us or 678-961-3304.",
        border=0, new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


@app.route("/quote-pdf/<record_id>", methods=["GET"])
def quote_pdf(record_id):
    c = cors()
    try:
        quote = _fetch_quote_data(record_id)
        if not quote:
            return Response(json.dumps({"error": "Quote not found"}), status=404,
                            headers=c, mimetype="application/json")
        pdf_bytes = _build_quote_pdf_bytes(quote)
        q_number  = quote.get("quoteNumber", record_id)
        return Response(
            pdf_bytes,
            headers={
                **cors(),
                "Content-Disposition": f'attachment; filename="{q_number}.pdf"',
                "Content-Type": "application/pdf",
            },
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        return Response(json.dumps({"error": str(e)}), status=500, headers=cors(), mimetype="application/json")


@app.route("/order-pdf/<record_id>", methods=["GET"])
def order_pdf(record_id):
    c = cors()
    try:
        order = _fetch_quote_data(record_id)
        if not order:
            return Response(json.dumps({"error": "Order not found"}), status=404,
                            headers=c, mimetype="application/json")
        pdf_bytes = _build_quote_pdf_bytes(order, doc_type="order")
        so_number = order.get("quoteNumber", record_id)
        return Response(
            pdf_bytes,
            headers={
                **cors(),
                "Content-Disposition": f'attachment; filename="{so_number}.pdf"',
                "Content-Type": "application/pdf",
            },
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        return Response(json.dumps({"error": str(e)}), status=500, headers=cors(), mimetype="application/json")


def _build_invoice_pdf_bytes(inv):
    """Generate PDF bytes for an invoice matching the quote/SO PDF style."""
    import os, tempfile
    from fpdf import FPDF

    def _fmt_date(iso_str):
        try:
            from datetime import date as _d
            d = _d.fromisoformat(iso_str)
            return f"{d.month}-{d.day}-{d.year}"
        except Exception:
            return iso_str or ""

    def _due_date(iso_str):
        try:
            from datetime import date as _d, timedelta
            return _fmt_date((_d.fromisoformat(iso_str) + timedelta(days=30)).isoformat())
        except Exception:
            return ""

    inv_number = inv.get("invNumber", "")
    so_number  = inv.get("soNumber", "")
    date_str   = inv.get("date", "")
    po_number  = inv.get("poNumber", "")
    org_name   = inv.get("orgName", "")
    contact    = inv.get("contact", "")
    addr1      = inv.get("addr1", "")
    addr2      = inv.get("addr2", "")
    ship_org   = inv.get("shipOrg", "")
    ship_name  = inv.get("shipName", "")
    ship_addr1 = inv.get("shipAddr1", "")
    ship_addr2 = inv.get("shipAddr2", "")
    tracking   = inv.get("tracking", "")
    ship_date  = inv.get("shipDate", "")
    line_items = inv.get("lineItems", [])
    subtotal   = inv.get("subtotal", 0.0)

    _static_dir    = os.path.dirname(os.path.abspath(__file__))
    logo_local_ref = None
    for _c in ["ba-logo-white-bg.jpg", "ba-logo-dark.png", "ba-logo.jpg"]:
        _p = os.path.join(_static_dir, "static", _c)
        if os.path.exists(_p):
            logo_local_ref = _p
            break
    _inv_ref = inv_number

    class InvPDF(FPDF):
        def header(self):
            if self.page_no() <= 1:
                return
            self.set_fill_color(255, 255, 255)
            self.rect(19, 6, 28, 10, style="F")
            try:
                self.image(logo_local_ref, x=19, y=6, w=28)
            except Exception:
                self.set_xy(19, 8)
                self.set_font("Helvetica", "B", 9)
                self.set_text_color(27, 36, 56)
                self.cell(28, 5, "BLUE ALPHA", border=0)
            self.set_xy(49, 9)
            self.set_font("Helvetica", "B", 8)
            self.set_text_color(27, 36, 56)
            self.cell(0, 5, _inv_ref, border=0)
            self.set_draw_color(27, 36, 56)
            self.set_line_width(0.4)
            self.line(19, 18, 19 + 177, 18)
            self.ln(4)

    pdf = InvPDF(orientation="P", unit="mm", format="letter")
    pdf.set_margins(19, 19, 19)
    pdf.set_auto_page_break(auto=True, margin=25)
    pdf.add_page()

    W     = 177.0   # usable width (215.9mm - 2×19mm margins)
    NAVY  = (27,  36,  56)
    MUTED = (107, 122, 141)
    TEXT  = (26,  38,  51)
    LG    = (245, 247, 250)
    BD    = (221, 227, 234)

    # ── Logo (top-left) — same constants as quote PDF ─────────────────
    LOGO_TOP           = 13.0          # mm from top of page
    LOGO_W             = 40.0          # fixed logo width in mm
    _PIXEL_ASPECT      = 726 / 1600    # image h/w ratio (ba-logo-white-bg.jpg)
    _LOGO_CONTENT_FRAC = 708 / 726     # fraction of image height containing real content
    _logo_h            = LOGO_W * _PIXEL_ASPECT                    # rendered height in mm
    HEADER_BOTTOM      = LOGO_TOP + _logo_h * _LOGO_CONTENT_FRAC   # visual logo bottom — anchor for all 3 elements

    logo_file = logo_local_ref
    logo_tmp  = None
    try:
        if not logo_file:
            import urllib.request
            logo_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            urllib.request.urlretrieve(
                "https://www.bluealphabelts.com/wp-content/uploads/2024/04/logo-1.png", logo_tmp.name)
            logo_file = logo_tmp.name
        pdf.image(logo_file, x=19, y=LOGO_TOP, w=LOGO_W, h=_logo_h)
    except Exception:
        pdf.set_xy(19, LOGO_TOP)
        pdf.set_font("Helvetica", "B", 18)
        pdf.set_text_color(*NAVY)
        pdf.cell(LOGO_W, 10, "BLUE ALPHA", border=0)
    finally:
        if logo_tmp:
            try: os.unlink(logo_tmp.name)
            except Exception: pass

    # ── Middle: company address (bottom-aligned to HEADER_BOTTOM) ─────
    mid_x = 19 + LOGO_W + 6
    mid_w = W * 0.36
    addr_lines = [
        ("Blue Alpha",       True),
        ("35 Andrew St.",    False),
        ("Newnan, GA 30263", False),
    ]
    addr_line_h = 4.5
    addr_y = HEADER_BOTTOM - len(addr_lines) * addr_line_h
    for cline, bold in addr_lines:
        pdf.set_xy(mid_x, addr_y)
        pdf.set_font("Helvetica", "B" if bold else "", 8)
        pdf.set_text_color(*TEXT if bold else MUTED)
        pdf.cell(mid_w, addr_line_h, cline, border=0, new_x="LMARGIN", new_y="NEXT")
        addr_y += addr_line_h

    # ── Right: "INVOICE" heading + invoice number (pinned to HEADER_BOTTOM) ──
    right_x = 19 + W * 0.62
    right_w = W * 0.38

    _num_cell_h  = 5.5
    _inv_cell_h  = 12.0
    _inv_gap     = 1.0
    _inv_start   = HEADER_BOTTOM - _num_cell_h - _inv_gap - _inv_cell_h

    pdf.set_xy(right_x, _inv_start)
    pdf.set_font("Helvetica", "B", 28)
    pdf.set_text_color(*NAVY)
    pdf.cell(right_w, _inv_cell_h, "INVOICE", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_xy(right_x, HEADER_BOTTOM - _num_cell_h)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(*NAVY)
    pdf.cell(right_w, _num_cell_h, f"#{inv_number}", align="R", new_x="LMARGIN", new_y="NEXT")

    # Move cursor below header block
    pdf.set_y(HEADER_BOTTOM + 4)

    # Navy divider
    pdf.set_draw_color(*NAVY)
    pdf.set_line_width(0.7)
    pdf.line(19, pdf.get_y(), 19 + W, pdf.get_y())
    pdf.ln(5)

    # ── Bill To | Ship To | Invoice Details (3 equal columns) ────────
    y_info = pdf.get_y()
    col3_w = W / 3.0
    bill_x = 19
    ship_x = 19 + col3_w
    meta_x = 19 + col3_w * 2

    # --- Bill To ---
    pdf.set_xy(bill_x, y_info)
    pdf.set_font("Helvetica", "B", 7)
    pdf.set_text_color(*MUTED)
    pdf.cell(col3_w, 5.5, "BILL TO", border=0, new_x="LEFT", new_y="NEXT")
    for ln in [org_name, contact, addr1, addr2]:
        if ln:
            pdf.set_x(bill_x)
            pdf.set_font("Helvetica", "B" if ln == org_name else "", 8)
            pdf.set_text_color(*TEXT)
            pdf.cell(col3_w, 5, ln, border=0, new_x="LEFT", new_y="NEXT")
    y_after_bill = pdf.get_y()

    # --- Ship To ---
    pdf.set_xy(ship_x, y_info)
    pdf.set_font("Helvetica", "B", 7)
    pdf.set_text_color(*MUTED)
    pdf.cell(col3_w, 5.5, "SHIP TO", border=0, new_x="LEFT", new_y="NEXT")
    for ln in [ship_org, ship_name, ship_addr1, ship_addr2]:
        if ln:
            pdf.set_x(ship_x)
            pdf.set_font("Helvetica", "B" if ln == ship_org else "", 8)
            pdf.set_text_color(*TEXT)
            pdf.cell(col3_w, 5, ln, border=0, new_x="LEFT", new_y="NEXT")
    # Ship Date + Tracking directly below Ship To address
    if ship_date:
        pdf.set_x(ship_x)
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_text_color(*MUTED)
        pdf.cell(col3_w, 5.5, "SHIP DATE", border=0, new_x="LEFT", new_y="NEXT")
        pdf.set_x(ship_x)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*TEXT)
        pdf.cell(col3_w, 5, _fmt_date(ship_date), border=0, new_x="LEFT", new_y="NEXT")
    if tracking:
        pdf.set_x(ship_x)
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_text_color(*MUTED)
        pdf.cell(col3_w, 5.5, "TRACKING #", border=0, new_x="LEFT", new_y="NEXT")
        pdf.set_x(ship_x)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*TEXT)
        pdf.cell(col3_w, 5, tracking, border=0, new_x="LEFT", new_y="NEXT")
    y_after_ship = pdf.get_y()

    # --- Invoice Details ---
    def meta_kv(label, value):
        pdf.set_x(meta_x)
        pdf.set_font("Helvetica", "B", 7)
        pdf.set_text_color(*MUTED)
        pdf.cell(col3_w, 5, label.upper(), border=0, new_x="LEFT", new_y="NEXT")
        pdf.set_x(meta_x)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*TEXT)
        pdf.cell(col3_w, 5, str(value), border=0, new_x="LEFT", new_y="NEXT")
        pdf.set_x(meta_x)
        pdf.set_y(pdf.get_y() + 1)

    pdf.set_xy(meta_x, y_info)
    # Invoice # is in the header — skip it here
    if so_number:
        meta_kv("Sales Order #", so_number)
    meta_kv("Invoice Date", _fmt_date(date_str))
    meta_kv("Due Date",     _due_date(date_str))
    if po_number:
        meta_kv("PO #", po_number)
    # Ship Date + Tracking moved to Ship To column — not repeated here
    y_after_meta = pdf.get_y()

    pdf.set_y(max(y_after_bill, y_after_ship, y_after_meta) + 4)
    pdf.ln(2)

    # ── Line items table ──────────────────────────────────────────────
    name_w     = W - 14 - 25 - 25
    col_widths = [name_w, 14, 25, 25]
    headers    = ["DESCRIPTION", "QTY", "UNIT PRICE", "TOTAL"]
    aligns     = ["L",           "C",   "R",          "R"    ]
    row_h      = 7    # minimum row height
    line_h     = 4.5  # tighter line height inside wrapped descriptions

    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 8)
    for w, h, a in zip(col_widths, headers, aligns):
        pdf.cell(w, row_h, h, border=0, align=a, fill=True)
    pdf.ln()

    def _redraw_table_header():
        pdf.set_fill_color(*NAVY)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 8)
        for w, h, a in zip(col_widths, headers, aligns):
            pdf.cell(w, row_h, h, border=0, align=a, fill=True)
        pdf.ln()
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*TEXT)

    pdf.set_font("Helvetica", "", 8)
    for idx, item in enumerate(line_items):
        pdf.set_font("Helvetica", "", 8)   # force-reset each row — prevents state bleed
        pdf.set_text_color(*TEXT)
        name_val  = item.get("name", "")
        qty_val   = str(int(item.get("qty", 0)))
        price_val = f"${float(item.get('unit_price', 0)):.2f}"
        total_val = f"${float(item.get('total', 0)):.2f}"
        fill_row  = idx % 2 == 0

        row_y = pdf.get_y()

        # ── Step 1: measure actual text height (auto-break off so we don't flip pages) ──
        pdf.set_auto_page_break(auto=False)
        pdf.set_xy(19, row_y)
        pdf.multi_cell(name_w, line_h, name_val, border=0, align="L")
        text_height  = pdf.get_y() - row_y
        pdf.set_auto_page_break(auto=True, margin=25)

        num_lines    = max(1, round(text_height / line_h))
        actual_row_h = num_lines * row_h
        actual_end_y = row_y + actual_row_h
        desc_y       = row_y + (actual_row_h - text_height) / 2  # vertical center

        # ── Step 2: page break if this row overruns the trigger ──
        if actual_end_y > pdf.page_break_trigger:
            pdf.add_page()
            _redraw_table_header()
            row_y = pdf.get_y()
            # Re-measure on new page
            pdf.set_auto_page_break(auto=False)
            pdf.set_xy(19, row_y)
            pdf.multi_cell(name_w, line_h, name_val, border=0, align="L")
            text_height  = pdf.get_y() - row_y
            pdf.set_auto_page_break(auto=True, margin=25)
            num_lines    = max(1, round(text_height / line_h))
            actual_row_h = num_lines * row_h
            actual_end_y = row_y + actual_row_h
            desc_y       = row_y + (actual_row_h - text_height) / 2

        # ── Step 3: background rect (white for non-fill rows, light-gray for fill rows) ──
        pdf.set_fill_color(*(LG if fill_row else (255, 255, 255)))
        pdf.rect(19, row_y, W, actual_row_h, style="F")

        # ── Step 4: re-render description at vertically-centered position ──
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*TEXT)
        pdf.set_xy(19, desc_y)
        pdf.multi_cell(name_w, line_h, name_val, border=0, align="L")

        # ── Step 5: numeric columns (full row height) ──
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*TEXT)
        pdf.set_xy(19 + name_w, row_y)
        pdf.cell(col_widths[1], actual_row_h, qty_val,   border=0, align="C")
        pdf.cell(col_widths[2], actual_row_h, price_val, border=0, align="R")
        pdf.cell(col_widths[3], actual_row_h, total_val, border=0, align="R")

        pdf.set_y(actual_end_y)

    pdf.ln(3)

    # ── Totals ────────────────────────────────────────────────────────
    def totals_row(label, value, bold=False):
        pdf.set_font("Helvetica", "B" if bold else "", 9)
        pdf.cell(W - 50, 6, "", border=0)
        pdf.set_text_color(*TEXT if bold else MUTED)
        pdf.cell(25, 6, label, border=0, align="R")
        pdf.set_text_color(*TEXT)
        pdf.cell(25, 6, value, border=0, align="R", new_x="LMARGIN", new_y="NEXT")

    y_rule = pdf.get_y()
    pdf.set_draw_color(*NAVY)
    pdf.set_line_width(0.4)
    pdf.line(19 + W - 50, y_rule, 19 + W, y_rule)
    pdf.ln(1)
    totals_row("Total Due", f"${subtotal:.2f}", bold=True)
    pdf.ln(5)

    # ── Stripe Payment Links ──────────────────────────────────────────
    stripe_cc_url  = inv.get("stripeCcUrl", "")
    stripe_ach_url = inv.get("stripeAchUrl", "")
    if stripe_cc_url or stripe_ach_url:
        pdf.set_draw_color(*BD)
        pdf.set_line_width(0.3)
        pdf.line(19, pdf.get_y(), 19 + W, pdf.get_y())
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 8.5)
        pdf.set_text_color(*NAVY)
        pdf.cell(0, 5, "Pay Online", border=0, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)
        if stripe_cc_url:
            pdf.set_font("Helvetica", "", 8.5)
            pdf.set_text_color(27, 100, 180)
            pdf.cell(0, 6, "Pay by Credit Card >>", border=0, link=stripe_cc_url, new_x="LMARGIN", new_y="NEXT")
        if stripe_ach_url:
            pdf.set_font("Helvetica", "", 8.5)
            pdf.set_text_color(27, 100, 180)
            pdf.cell(0, 6, "Pay by ACH / Bank Transfer >>", border=0, link=stripe_ach_url, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
        pdf.set_font("Helvetica", "I", 7.5)
        pdf.set_text_color(120, 120, 120)
        pdf.cell(0, 5, "A 3% credit card processing fee is added to CC payments. Pay by ACH/bank transfer to avoid this fee.", border=0, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    # ── Footer pinned to bottom of current page ───────────────────────
    PAGE_H     = 279.4   # letter height in mm
    BOT_MARGIN = 6       # mm from bottom for footer start
    footer_h   = 20
    footer_y   = PAGE_H - BOT_MARGIN - footer_h
    if pdf.get_y() < footer_y:
        pdf.set_y(footer_y)
    pdf.set_auto_page_break(auto=False)

    # Footer divider
    pdf.set_draw_color(*BD)
    pdf.set_line_width(0.3)
    pdf.line(19, pdf.get_y(), 19 + W, pdf.get_y())
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*MUTED)

    # Line 1
    pdf.write(4.5, "Payment terms are Net 30 from invoice date. A 1.5% monthly late fee applies after 30 days.")
    pdf.ln(5)

    # Line 2
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*MUTED)
    pdf.cell(W, 4.5,
        "Questions about this invoice? Contact us at orders@bluealpha.us or 678-961-3304.",
        border=0, new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


@app.route("/invoice-pdf/<record_id>", methods=["GET"])
def invoice_pdf(record_id):
    c = cors()
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=15,
        )
        if not r.ok:
            return Response(json.dumps({"error": "Invoice not found"}), status=404, headers=c, mimetype="application/json")
        fields = r.json().get("fields", {})
        if fields.get("Order Type") != "Invoice":
            return Response(json.dumps({"error": "Not an invoice"}), status=400, headers=c, mimetype="application/json")

        def _first(lst):
            return lst[0] if isinstance(lst, list) and lst else (lst or "")

        order_id   = str(fields.get("Order ID", "")).strip()
        inv_number = fields.get("Document ID", f"IN-{order_id}")
        so_number  = f"SO-{order_id}" if order_id else ""

        tracking = ship_date = ""
        tracking_recs = at_get_all(_SO_TRACKING_TABLE, _SO_TRACKING_TOKEN,
                                    fields=["Order #", "Tracking #", "Ship Date"], base_id=_SO_TRACKING_BASE)
        for tr in tracking_recs:
            if tr.get("fields", {}).get("Order #", "").strip() == so_number:
                tracking  = tr["fields"].get("Tracking #", "")
                ship_date = tr["fields"].get("Ship Date", "")
                break
        # Fall back to Tracking field on the Manual Orders record itself (legacy orders)
        if not tracking:
            tracking = fields.get("Tracking", "")

        li_ids = fields.get("MO Line Items", [])
        line_items = []
        for li_id in li_ids:
            lr = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                headers=at_headers(read_token), timeout=10,
            )
            if lr.ok:
                lf = lr.json().get("fields", {})
                pname = _first(lf.get("Product Name (from Product SKU)", [])) or \
                        _first(lf.get("Name + Variations (from Product SKU)", [])) or "Item"
                qty        = lf.get("Qty.", 0)
                unit_price = float(lf.get("Confirmed Adj. Unit Price") or lf.get("Confirmed Unit Price") or 0)
                li_total   = float(lf.get("Confirmed Line Item Total") or (qty * unit_price))
                line_items.append({"name": pname, "qty": qty, "unit_price": unit_price,
                                   "total": li_total})

        ship_city  = _first(fields.get("Customer City (from Customer)", []))
        ship_state = _first(fields.get("Customer State (from Customer)", []))
        ship_zip   = _first(fields.get("Customer Zip Code (from Customer)", []))
        ship_csz   = ", ".join(filter(None, [ship_city, f"{ship_state} {ship_zip}".strip()]))

        inv = {
            "invNumber":    inv_number,
            "soNumber":     so_number,
            "date":         fields.get("Date", ""),
            "poNumber":     fields.get("Purchase Order #", ""),
            "orgName":      _first(fields.get("Bill-To Org Name (from Customer)", [])),
            "contact":      _first(fields.get("Bill-To Contact Name (from Customer)", [])),
            "email":        _first(fields.get("Bill-To Contact Email (from Customer)", [])),
            "addr1":        _first(fields.get("Bill-To Address (Line 1) (from Customer)", [])),
            "addr2":        _first(fields.get("Bill-To Address (Line 2) (from Customer)", [])),
            "shipOrg":      _first(fields.get("Organization Name (from Customer)", [])),
            "shipName":     _first(fields.get("Main Contact Name (from Customer)", [])),
            "shipAddr1":    _first(fields.get("Customer Address (Line 1) (from Customer)", [])),
            "shipAddr2":    _first(fields.get("Customer Address (Line 2) (from Customer)", [])) or ship_csz,
            "tracking":     tracking,
            "shipDate":     ship_date,
            "lineItems":    line_items,
            "subtotal":     round(sum(li["total"] for li in line_items), 2),
            "stripeCcUrl":  fields.get("Stripe Invoice URL (CC)", ""),
            "stripeAchUrl": fields.get("Stripe Invoice URL (ACH)", ""),
        }

        pdf_bytes = _build_invoice_pdf_bytes(inv)
        return Response(
            pdf_bytes,
            headers={
                **cors(),
                "Content-Disposition": f'attachment; filename="{inv_number}.pdf"',
                "Content-Type": "application/pdf",
            },
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


# ─────────────────────────────────────────────────────────────────────────────
# Customer Portal Routes
# ─────────────────────────────────────────────────────────────────────────────

def _send_application_received_email(to_email, to_name, company):
    if not SENDGRID_API_KEY:
        return
    first_name = to_name.split()[0] if to_name else "there"
    actual_to = TEST_EMAIL_OVERRIDE or to_email
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;padding:32px 0;">
    <tr><td align="center">
      <table width="520" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#1B2438;padding:24px 36px;">
          <span style="font-family:Arial;font-size:20px;font-weight:800;color:#fff;letter-spacing:2px;">BLUE ALPHA</span>
        </td></tr>
        <tr><td style="padding:32px 36px;">
          <p style="color:#1a2633;font-size:16px;margin:0 0 8px;">Hi {first_name},</p>
          <p style="color:#6b7a8d;font-size:14px;line-height:1.6;margin:0 0 16px;">
            Thank you for applying for access to the Blue Alpha Government Agency Quote Portal.
            We've received your application for <strong>{company}</strong> and will review it within 2 business days.
          </p>
          <p style="color:#6b7a8d;font-size:14px;line-height:1.6;margin:0 0 24px;">
            You'll receive another email once your application has been reviewed. If you have any questions in the meantime, feel free to reach out to us.
          </p>
          <p style="color:#6b7a8d;font-size:12px;margin-top:16px;">
            Questions? Contact us at <a href="mailto:orders@bluealpha.us" style="color:#1B2438;">orders@bluealpha.us</a>
          </p>
        </td></tr>
        <tr><td style="background:#f5f7fa;border-top:1px solid #dde3ea;padding:16px 36px;text-align:center;">
          <p style="color:#6b7a8d;font-size:11px;margin:0;">Blue Alpha &bull; bluealphabelts.com</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
    try:
        req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": actual_to, "name": to_name}]}],
                "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
                "subject": "We received your Blue Alpha portal application",
                "content": [{"type": "text/html", "value": html_body}],
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[apply] confirmation email error: {e}")


@app.route("/apply", methods=["GET", "POST"])
def apply_page():
    if request.method == "GET":
        return send_from_directory("static", "apply.html")

    from datetime import datetime, timezone
    c = cors()

    # Accept both multipart/form-data (new, supports file upload) and JSON (legacy)
    if request.content_type and request.content_type.startswith("multipart/form-data"):
        def gf(k): return (request.form.get(k) or "").strip()
        cert_file = request.files.get("exemptionCert")
    else:
        data = request.get_json() or {}
        def gf(k): return (data.get(k) or "").strip()
        cert_file = None

    company_name              = gf("companyName")
    ein                       = gf("ein")
    website                   = gf("website")
    tax_exempt_raw            = gf("taxExempt")
    tax_exempt                = tax_exempt_raw.lower() not in ("false", "0", "no")
    tax_exemption_number      = gf("taxExemptionNumber")

    # Shipping fields (always filled)
    shipping_contact_name  = gf("shippingContactName")
    shipping_contact_email = gf("shippingContactEmail")
    shipping_contact_phone = gf("shippingContactPhone")
    shipping_addr1         = gf("shippingAddr1")
    shipping_addr2         = gf("shippingAddr2")
    shipping_city          = gf("shippingCity")
    shipping_state         = gf("shippingState")
    shipping_zip           = gf("shippingZip")

    # Billing fields — fall back to shipping when same-as-shipping is checked
    billing_same_raw = gf("billingSameAsShipping")
    billing_same     = billing_same_raw.lower() not in ("false", "0", "no")
    billing_contact_name   = gf("billingContactName")  if not billing_same else shipping_contact_name
    billing_contact_email  = gf("billingContactEmail") if not billing_same else shipping_contact_email
    billing_contact_phone  = gf("billingContactPhone") if not billing_same else shipping_contact_phone
    billing_addr1          = gf("billingAddr1")        if not billing_same else shipping_addr1
    billing_addr2          = gf("billingAddr2")        if not billing_same else shipping_addr2
    billing_city           = gf("billingCity")         if not billing_same else shipping_city
    billing_state          = gf("billingState")        if not billing_same else shipping_state
    billing_zip            = gf("billingZip")          if not billing_same else shipping_zip

    required_fields = [company_name,
                       shipping_contact_name, shipping_contact_email, shipping_contact_phone,
                       shipping_addr1, shipping_city, shipping_state, shipping_zip]
    if tax_exempt:
        required_fields += [ein, tax_exemption_number]
    if not billing_same:
        required_fields += [billing_contact_name, billing_contact_email,
                            billing_addr1, billing_city, billing_state, billing_zip]
    if not all(required_fields):
        return Response(json.dumps({"error": "Please fill in all required fields."}),
                        status=400, headers=c, mimetype="application/json")

    # Reject duplicate company names — same name may exist for different locations,
    # but those should be set up manually by Blue Alpha staff.
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        dup_check = at_get_all(
            CUSTOMERS_TABLE_ID, read_token,
            fields=["Organization Name"],
            formula=f"LOWER({{Organization Name}})='{company_name.lower().replace(chr(39), chr(39)+chr(39))}'",
        )
        if dup_check:
            return Response(json.dumps({"error": (
                "An account already exists with this organization name. "
                "Please enter a different name."
            )}), status=409, headers=c, mimetype="application/json")
    except Exception as _dup_err:
        print(f"[apply] duplicate check failed: {_dup_err}")  # non-fatal — proceed

    # Customer Address = SHIPPING address (city/state/zip auto-parsed by Airtable formula)
    # Bill-To Address  = BILLING address (plain text)
    def pack_addr2(city, state, zip_code, line2=""):
        csz = f"{city}, {state} {zip_code}"
        return (line2 + "\n" + csz) if line2 else csz

    bill_addr2_full = pack_addr2(billing_city, billing_state, billing_zip, billing_addr2)
    ship_addr2_full = f"{shipping_city}, {shipping_state} {shipping_zip}"
    if shipping_addr2:
        ship_addr2_full = shipping_addr2 + "\n" + ship_addr2_full

    fields = {
        "Organization Name":            company_name,
        "EIN":                          ein,
        # Main Contact = shipping contact (person submitting form; receives goods)
        "Main Contact Name":            shipping_contact_name,
        "Main Contact Email":           shipping_contact_email,
        "Main Contact Phone #":         shipping_contact_phone,
        # Customer Address = shipping address (city/state/zip auto-parsed by Airtable formula)
        "Customer Address (Line 1)":    shipping_addr1,
        "Customer Address (Line 2)":    ship_addr2_full,
        # Bill-To = billing contact & address
        "Bill-To Org Name":             company_name,
        "Bill-To Contact Name":         billing_contact_name,
        "Bill-To Contact Email":        billing_contact_email,
        "Bill-To Phone #":              billing_contact_phone,
        "Bill-To Address (Line 1)":     billing_addr1,
        "Bill-To Address (Line 2)":     bill_addr2_full,
        "State Tax Exemption #":        tax_exemption_number,
        "Tax Exempt":                   tax_exempt,
        "Application Status":           "Pending",
        "Applied Date":                 datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    if website: fields["Website"] = website

    try:
        r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}",
            headers={**at_headers(APPLY_WRITE_TOKEN), "Content-Type": "application/json"},
            json={"fields": fields},
            timeout=15,
        )
        if r.status_code not in (200, 201):
            return Response(json.dumps({"error": "Failed to save application. Please try again."}),
                            status=500, headers=c, mimetype="application/json")

        record_id = r.json().get("id")

        # Upload tax exemption certificate to Airtable if provided (only for tax-exempt applicants)
        # Strategy: upload file to tmpfiles.org → get public URL → PATCH Airtable record
        if tax_exempt and cert_file and cert_file.filename and record_id:
            try:
                file_bytes = cert_file.read()
                filename   = cert_file.filename or "exemption-certificate"
                # 1. Upload to tmpfiles.org to get a public URL
                tmp_resp = req_lib.post(
                    "https://tmpfiles.org/api/v1/upload",
                    files={"file": (filename, file_bytes, cert_file.content_type or "application/octet-stream")},
                    timeout=30,
                )
                if tmp_resp.status_code == 200:
                    tmp_data = tmp_resp.json()
                    tmp_url  = tmp_data.get("data", {}).get("url", "")
                    # tmpfiles.org returns http:// — Airtable needs https://
                    if tmp_url.startswith("http://"):
                        tmp_url = "https://" + tmp_url[7:]
                    if tmp_url:
                        # 2. PATCH Airtable record with the attachment URL
                        req_lib.patch(
                            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
                            headers={**at_headers(APPLY_WRITE_TOKEN), "Content-Type": "application/json"},
                            json={"fields": {"Tax Exemption Certificate": [{"url": tmp_url, "filename": filename}]}},
                            timeout=15,
                        )
            except Exception:
                pass  # Don't fail the whole application if cert upload fails

        # Send confirmation email to applicant
        threading.Thread(
            target=_send_application_received_email,
            args=(shipping_contact_email, shipping_contact_name, company_name),
            daemon=True
        ).start()

        return Response(json.dumps({"success": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        return send_from_directory("static", "login.html")

    c = cors()
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()

    # Always return same message — do the real work silently
    def _try_send_magic_link(email):
        try:
            read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
            records = at_get_all(
                CUSTOMERS_TABLE_ID, read_token,
                fields=["Main Contact Email", "Main Contact Name", "Application Status"],
                formula=f"AND(LOWER({{Main Contact Email}})='{email}',{{Application Status}}='Approved')",
            )
            if not records:
                return
            user_rec = records[0]
            user_id  = user_rec["id"]  # Customer record ID is both user_id and customer_id
            magic_link = generate_magic_link(user_id, expiry_hours=0.25)
            send_magic_link_email(email, magic_link)
        except Exception as e:
            print(f"[login] magic link error: {e}")

    if email:
        threading.Thread(target=_try_send_magic_link, args=(email,), daemon=True).start()

    return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")


@app.route("/setup-account/<token>")
def setup_account_page(token):
    """Serves the account setup page for new sub-users (invited via email)."""
    return send_from_directory("static", "setup-account.html")


import re as _re

# ── Portal credential requirements (enforced on both frontend and backend) ────
_USERNAME_RE = _re.compile(r'^[a-z0-9._-]{3,32}$')

def _validate_username(username: str):
    """Returns error string or None if valid."""
    if not username:
        return "Username is required."
    if not _USERNAME_RE.match(username):
        if len(username) < 3:
            return "Username must be at least 3 characters."
        if len(username) > 32:
            return "Username must be 32 characters or fewer."
        return "Username may only contain lowercase letters, numbers, and . _ -"
    return None

def _validate_password(password: str):
    """Returns error string or None if valid."""
    if not password:
        return "Password is required."
    if len(password) < 8:
        return "Password must be at least 8 characters."
    return None

@app.route("/api/portal/setup-account", methods=["POST"])
def portal_setup_account():
    """Complete account setup: validate invite token, set username + password."""
    from datetime import datetime, timezone
    c = cors()
    data     = request.get_json() or {}
    token    = (data.get("token") or "").strip()
    username = (data.get("username") or "").strip().lower()
    password = (data.get("password") or "").strip()
    if not token:
        return Response(json.dumps({"error": "Invalid or missing invite token."}),
                        status=400, headers=c, mimetype="application/json")
    err = _validate_username(username)
    if err:
        return Response(json.dumps({"error": err}), status=400, headers=c, mimetype="application/json")
    err = _validate_password(password)
    if err:
        return Response(json.dumps({"error": err}), status=400, headers=c, mimetype="application/json")

    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = APPLY_WRITE_TOKEN or RETURNS_WRITE_TOKEN
    try:
        # Find record by invite token
        records = at_get_all(CUSTOMERS_TABLE_ID, read_token,
                             fields=["Magic Token", "Token Expiry", "Portal Hash",
                                     "Portal Role", "Parent Company", "Main Contact Email"],
                             formula=f"{{Magic Token}}='{token}'")
        if not records:
            return Response(json.dumps({"error": "Invalid or expired invite link"}),
                            status=400, headers=c, mimetype="application/json")
        rec = records[0]
        f   = rec["fields"]

        # Check token expiry
        expiry_str = f.get("Token Expiry", "")
        if expiry_str:
            try:
                exp_dt = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > exp_dt:
                    return Response(json.dumps({"error": "This setup link has expired. Please contact orders@bluealpha.us to get a new one."}),
                                    status=400, headers=c, mimetype="application/json")
            except Exception:
                pass

        # Check username not already taken
        existing = at_get_all(CUSTOMERS_TABLE_ID, read_token,
                              fields=["Portal Username"],
                              formula=f"LOWER({{Portal Username}})='{username}'")
        if existing:
            return Response(json.dumps({"error": "Username already taken — please choose another"}),
                            status=400, headers=c, mimetype="application/json")

        # Set username + password, clear invite token
        pw_hash = _hash_password(password)
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{rec['id']}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {
                "Portal Username": username,
                "Portal Hash":   pw_hash,
                "Magic Token":     "",
                "Token Expiry":    None,
                "Last Login":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            }},
            timeout=10,
        )

        # Log them in immediately
        parent_ids  = f.get("Parent Company", [])
        customer_id = parent_ids[0] if parent_ids else rec["id"]
        role_raw    = (f.get("Portal Role") or "").strip()
        _rmap = {"Admin": "admin", "Full Access": "full_access", "Orders": "orders", "Quotes Only": "quotes_only", "Invoices": "invoices", "Read Only": "read_only"}
        role = _rmap.get(role_raw, "admin" if not parent_ids else "read_only")
        jwt_token = create_portal_token(rec["id"], customer_id, not bool(parent_ids), role)
        resp = make_response(Response(json.dumps({"ok": True}), headers=c, mimetype="application/json"))
        resp.set_cookie("ba_portal_session", jwt_token, max_age=30*24*3600,
                        httponly=True, samesite="Lax", secure=True)
        return resp
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/auth/<token>")
def auth_magic_link(token):
    from datetime import datetime, timezone
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        records = at_get_all(
            CUSTOMERS_TABLE_ID, read_token,
            fields=["Magic Token", "Token Expiry", "Application Status", "Portal Role"],
            formula=f"{{Magic Token}}='{token}'",
        )
        if not records:
            return send_from_directory("static", "auth-error.html"), 401

        user_rec = records[0]
        uf = user_rec.get("fields", {})
        expiry_str = uf.get("Token Expiry", "")
        if not expiry_str:
            return send_from_directory("static", "auth-error.html"), 401

        # Check Application Status
        if uf.get("Application Status") != "Approved":
            return send_from_directory("static", "auth-error.html"), 401

        # Check expiry
        try:
            exp_dt = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                from datetime import timezone as tz
                exp_dt = exp_dt.replace(tzinfo=tz.utc)
        except Exception:
            return send_from_directory("static", "auth-error.html"), 401

        if datetime.now(timezone.utc) > exp_dt:
            return send_from_directory("static", "auth-error.html"), 401

        # Customer record ID is both user_id and customer_id for magic-link users
        user_id     = user_rec["id"]
        customer_id = user_rec["id"]
        is_primary  = True

        # Fetch Portal Role from Customer record (legacy users with no role → admin)
        portal_role = None
        try:
            role_raw = uf.get("Portal Role", "")
            _role_map = {"Admin": "admin", "Full Access": "full_access", "Orders": "orders",
                         "Quotes Only": "quotes_only", "Invoices": "invoices", "Read Only": "read_only"}
            if not role_raw:
                portal_role = None  # legacy → admin
            else:
                portal_role = _role_map.get(role_raw.strip(), None)
        except Exception:
            portal_role = None

        # Clear magic token + update last login
        try:
            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{user_id}",
                headers={**at_headers(RETURNS_WRITE_TOKEN), "Content-Type": "application/json"},
                json={"fields": {
                    "Magic Token":  "",
                    "Token Expiry": None,
                    "Last Login":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                }},
                timeout=10,
            )
        except Exception as e:
            print(f"[auth_magic_link] token clear failed: {e}")

        # Create JWT session cookie
        jwt_token = create_portal_token(user_id, customer_id, is_primary, role=portal_role)
        resp = make_response(redirect("/portal"))
        resp.set_cookie(
            "ba_portal_session",
            jwt_token,
            max_age=30 * 24 * 3600,
            httponly=True,
            samesite="Lax",
            secure=True,
        )
        return resp

    except Exception as e:
        print(f"[auth_magic_link] error: {e}")
        return send_from_directory("static", "auth-error.html"), 500


@app.route("/logout")
def portal_logout():
    resp = make_response(redirect("/quote"))
    resp.delete_cookie("ba_portal_session")
    return resp


@app.route("/portal")
@portal_login_required
def portal_page(user):
    # Department/law enforcement accounts go to their own portal
    if user.get("role", "").lower() == "invoices":
        return redirect("/department")
    # If this customer has contract SKUs, redirect them to the contract portal
    customer_id = user.get("customer_id", "")
    if customer_id:
        try:
            token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
            # ARRAYJOIN returns display names not IDs, so filter in Python
            all_contract = at_get_all(PRODUCT_SKUS_TABLE_ID, token,
                                      fields=["SKU ID", "Customers"],
                                      formula='{Category}="Contract"')
            if any(customer_id in r.get("fields", {}).get("Customers", [])
                   for r in all_contract):
                return redirect("/contract")
        except Exception:
            pass
    resp = send_from_directory("static", "portal.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@app.route("/contract")
@portal_login_required
def contract_portal_page(user):
    resp = send_from_directory("static", "contract-portal.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@app.route("/department")
@portal_login_required
def department_portal_page(user):
    """Dedicated portal for law enforcement / department accounts (Newnan PD etc.)."""
    resp = send_from_directory("static", "pd-portal.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@app.route("/api/department/catalog", methods=["GET", "OPTIONS"])
@portal_login_required
def department_catalog(user):
    """Return catalog SKUs that have a Newnan PD Price (department ordering portal)."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET"})
    c = cors()
    try:
        token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        # Fetch all SKUs where Newnan PD Discount > 0 (percent field stored as decimal e.g. 0.1 = 10%)
        import concurrent.futures as _cf_dc
        fut_skus = _cf_dc.ThreadPoolExecutor(max_workers=1).submit(
            at_get_all, PRODUCT_SKUS_TABLE_ID, token,
            ["SKU ID", "Name + Variations", "Sale Price", "Newnan PD Price",
             "Newnan PD Discount", "Parent Product", "Color", "Size",
             "Feature Variation", "Add-ons", "Category"],
            "{Newnan PD Discount}>0",
        )
        with _cf_dc.ThreadPoolExecutor(max_workers=5) as ex:
            fut_parents = ex.submit(at_get_all, PARENT_PRODUCTS_TABLE_ID,    token, fields=["Name"])
            fut_colors  = ex.submit(at_get_all, COLORS_TABLE_ID,             token, fields=["Name"])
            fut_sizes   = ex.submit(at_get_all, SIZES_TABLE_ID,              token, fields=["Name"])
            fut_fvars   = ex.submit(at_get_all, FEATURE_VARIATIONS_TABLE_ID, token, fields=["Name"])
            fut_addons  = ex.submit(at_get_all, ADDONS_TABLE_ID,             token, fields=["Name"])
            parent_recs = fut_parents.result()
            parent_map  = {r["id"]: r["fields"].get("Name", "") for r in parent_recs}
            parent_cat_map = {}  # category comes from SKU records, not parent records
            color_map   = {r["id"]: r["fields"].get("Name", "") for r in fut_colors.result()}
            size_map    = {r["id"]: r["fields"].get("Name", "") for r in fut_sizes.result()}
            fvar_map    = {r["id"]: r["fields"].get("Name", "").strip() for r in fut_fvars.result()}
            addon_map   = {r["id"]: r["fields"].get("Name", "").strip() for r in fut_addons.result()}

        sku_records = fut_skus.result()
        skus = []
        seen_parents = {}
        for r in sku_records:
            f = r.get("fields", {})
            parent_ids = f.get("Parent Product", [])
            if not parent_ids:
                continue
            parent_id   = parent_ids[0]
            parent_name = parent_map.get(parent_id, "")
            if not parent_name:
                continue
            # Use Newnan PD Price as the price shown
            pd_price = f.get("Newnan PD Price") or f.get("Sale Price") or 0
            color_ids = f.get("Color", [])
            size_ids  = f.get("Size", [])
            fvar_ids  = f.get("Feature Variation", [])
            addon_ids = f.get("Add-ons", [])
            color_id   = color_ids[0] if color_ids else ""
            size_id    = size_ids[0]  if size_ids  else ""
            fvar_id    = fvar_ids[0]  if fvar_ids  else ""
            addon_id   = addon_ids[0] if addon_ids else ""
            color_name = color_map.get(color_id, "")
            # Department portal: Black only
            if color_name.lower() != "black":
                continue
            size_name  = size_map.get(size_id, "")
            fvar_name  = fvar_map.get(fvar_id, "").strip() if fvar_id else ""
            addon_name = addon_map.get(addon_id, "").strip() if addon_id else ""
            if size_name.strip().lower() in ("none", "n/a", "one size"):
                size_id = ""; size_name = ""
            if fvar_name.lower() in ("none", ""):
                fvar_id = ""; fvar_name = ""
            # Don't strip "None" addon — it's explicitly set in Airtable and needed for exact SKU matching
            if not addon_id:
                addon_name = ""
            cat = f.get("Category", "") or parent_cat_map.get(parent_id, "")
            skus.append({
                "recordId":       r["id"],
                "sku":            f.get("SKU ID", ""),
                "name":           f.get("Name + Variations", ""),
                "price":          float(pd_price),
                "parentId":       parent_id,
                "parentName":     parent_name,
                "colorId":        color_id,
                "colorName":      color_name,
                "sizeId":         size_id,
                "sizeName":       size_name,
                "featureVarId":   fvar_id,
                "featureVarName": fvar_name,
                "addonId":        addon_id,
                "addonName":      addon_name,
                "category":       cat,
            })
            if parent_id not in seen_parents:
                seen_parents[parent_id] = parent_name

        parents = sorted(
            [{"id": k, "name": v} for k, v in seen_parents.items()],
            key=lambda x: x["name"],
        )
        return Response(json.dumps({"skus": skus, "parents": parents}), headers=c, mimetype="application/json")
    except Exception as e:
        import traceback; traceback.print_exc()
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/department/order/create", methods=["POST", "OPTIONS"])
@portal_login_required
def department_order_create(user):
    """Create a department order (PD portal — officer places order directly)."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "POST"})
    c = cors()
    if user.get("role") == "invoices":
        return Response(json.dumps({"error": "Your account does not have permission to create orders."}),
                        status=403, headers=c, mimetype="application/json")
    data         = request.get_json(force=True) or {}
    officer_name = data.get("officer_name", "").strip()
    badge_num    = data.get("badge_num", "").strip()
    line_items   = data.get("line_items", [])

    if not officer_name or not line_items:
        return Response(json.dumps({"error": "officer_name and line_items are required"}),
                        status=400, headers=c, mimetype="application/json")

    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = RETURNS_WRITE_TOKEN

    # 1. Generate order ID
    try:
        order_id = _next_order_id(read_token)
    except Exception as e:
        return Response(json.dumps({"error": f"Order ID generation failed: {e}"}),
                        status=500, headers=c, mimetype="application/json")

    # 2. Get customer ID from session
    customer_id = user.get("customer_id") or user.get("customerId", "")

    import datetime as _dt_dept
    today = _dt_dept.date.today().isoformat()

    # 3. Create Manual Order record
    try:
        mo_fields = {
            "Order Type":         "Sales Order",
            "Order ID":           order_id,
            "Date":               today,
            "Officer Name":       officer_name,
            "Sales Order Status": "Approved",
        }
        if badge_num:
            mo_fields["Badge Number"] = badge_num
        if customer_id:
            mo_fields["Customer"] = [customer_id]

        mo_resp = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": mo_fields},
            timeout=15,
        )
        if not mo_resp.ok:
            return Response(json.dumps({"error": f"Order create error: {mo_resp.status_code} {mo_resp.text}"}),
                            status=500, headers=c, mimetype="application/json")
        mo_record_id = mo_resp.json()["id"]
    except Exception as e:
        return Response(json.dumps({"error": f"Order create failed: {e}"}),
                        status=500, headers=c, mimetype="application/json")

    # 4. Create MO Line Items
    for li in line_items:
        try:
            req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": {
                    "Manual Order":              [mo_record_id],
                    "Product SKU":               [li["sku_record_id"]],
                    "Qty.":                      int(li["qty"]),
                    "Confirmed Unit Price":       float(li["unit_price"]),
                    "Confirmed Adj. Unit Price":  float(li["unit_price"]),
                }},
                timeout=15,
            )
        except Exception as e:
            print(f"[department_order_create] line item warning: {e}")

    return Response(json.dumps({"ok": True, "record_id": mo_record_id, "order_id": f"SO-{order_id}"}),
                    headers=c, mimetype="application/json")


@app.route("/api/contract-catalog", methods=["GET", "OPTIONS"])
@portal_login_required
def contract_catalog(user):
    """Return contract SKUs visible to the logged-in customer."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET"})
    c = cors()
    customer_id = user.get("customer_id", "")
    if not customer_id:
        return Response(json.dumps({"error": "No customer"}), status=403, headers=c, mimetype="application/json")
    try:
        token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        # Fetch all contract SKUs, then filter in Python (ARRAYJOIN returns display names not IDs)
        all_contract_skus = at_get_all(
            PRODUCT_SKUS_TABLE_ID, token,
            fields=["SKU ID", "Name + Variations", "Sale Price", "Parent Product",
                    "Color", "Size", "Feature Variation", "Add-ons", "Category", "Customers"],
            formula='{Category}="Contract"',
        )
        sku_records = [r for r in all_contract_skus
                       if customer_id in r.get("fields", {}).get("Customers", [])]
        # Fetch parent/color/size/fvar name maps
        import concurrent.futures as _cf_cc
        with _cf_cc.ThreadPoolExecutor(max_workers=4) as ex:
            fut_parents = ex.submit(at_get_all, PARENT_PRODUCTS_TABLE_ID, token, fields=["Name"])
            fut_colors  = ex.submit(at_get_all, COLORS_TABLE_ID,          token, fields=["Name"])
            fut_sizes   = ex.submit(at_get_all, SIZES_TABLE_ID,           token, fields=["Name"])
            fut_fvars   = ex.submit(at_get_all, FEATURE_VARIATIONS_TABLE_ID, token, fields=["Name"])
            parent_map  = {r["id"]: r["fields"].get("Name","") for r in fut_parents.result()}
            color_map   = {r["id"]: r["fields"].get("Name","") for r in fut_colors.result()}
            size_map    = {r["id"]: r["fields"].get("Name","") for r in fut_sizes.result()}
            fvar_map    = {r["id"]: r["fields"].get("Name","").strip() for r in fut_fvars.result()}

        skus = []
        seen_parents = {}
        for r in sku_records:
            f = r.get("fields", {})
            parent_ids = f.get("Parent Product", [])
            if not parent_ids:
                continue
            parent_id   = parent_ids[0]
            parent_name = parent_map.get(parent_id, "")
            if not parent_name:
                continue
            color_ids  = f.get("Color", [])
            size_ids   = f.get("Size", [])
            color_id   = color_ids[0] if color_ids else ""
            size_id    = size_ids[0]  if size_ids  else ""
            color_name = color_map.get(color_id, "")
            size_name  = size_map.get(size_id, "")
            if size_name.strip().lower() in ("none", "n/a", "one size"):
                size_id = ""; size_name = ""
            fvar_ids  = f.get("Feature Variation", [])
            fvar_id   = fvar_ids[0] if fvar_ids else ""
            fvar_name = fvar_map.get(fvar_id, "").strip() if fvar_id else ""
            if fvar_name.lower() in ("none", ""):
                fvar_id = ""; fvar_name = ""
            skus.append({
                "recordId":       r["id"],
                "sku":            f.get("SKU ID", ""),
                "name":           f.get("Name + Variations", ""),
                "price":          f.get("Sale Price", 0),
                "parentId":       parent_id,
                "parentName":     parent_name,
                "colorId":        color_id,
                "colorName":      color_name,
                "sizeId":         size_id,
                "sizeName":       size_name,
                "featureVarId":   fvar_id,
                "featureVarName": fvar_name,
            })
            if parent_id not in seen_parents:
                seen_parents[parent_id] = parent_name

        _PRODUCT_SELECT_PARENTS = {"alias accessories", "ohc items"}
        parents = sorted(
            [{"id": k, "name": v,
              "productSelect": v.lower().strip() in _PRODUCT_SELECT_PARENTS}
             for k, v in seen_parents.items()],
            key=lambda x: x["name"],
        )
        return Response(json.dumps({"parents": parents, "skus": skus}), headers=c, mimetype="application/json")
    except Exception as e:
        import traceback; traceback.print_exc()
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/me")
@portal_login_required
def portal_me(user):
    c = cors()
    import time as _time_mod
    customer_id = user.get("customer_id", "")
    is_primary  = user.get("is_primary", False)
    role = user.get("role")
    can_manage_team  = portal_can(user, "manage_team")
    can_create_quote = portal_can(user, "create_quote")
    can_accept_quote = portal_can(user, "accept_quote")
    can_view_quotes  = portal_can(user, "view_quotes")
    can_view_orders  = portal_can(user, "view_orders")
    can_view_invoices= portal_can(user, "view_invoices")

    # Use cached agency name if fresh
    _me_cached = _ME_CACHE.get(customer_id)
    if _me_cached and (_time_mod.time() - _me_cached["ts"]) < _ME_CACHE_TTL:
        agency_name = _me_cached["data"]
    else:
        agency_name = ""
        if customer_id:
            try:
                read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
                cr = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
                    headers=at_headers(read_token),
                    timeout=10,
                )
                if cr.status_code == 200:
                    agency_name = cr.json().get("fields", {}).get("Organization Name", "")
            except Exception as e:
                print(f"[portal_me] customer fetch failed: {e}")
        _ME_CACHE[customer_id] = {"ts": _time_mod.time(), "data": agency_name}

    # Check if user has credentials set up; also grab email while we're here
    has_credentials = False
    user_email = ""
    user_id = user.get("user_id", "") or customer_id
    if user_id:
        try:
            read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
            ur = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{user_id}",
                headers=at_headers(read_token),
                timeout=10,
            )
            if ur.status_code == 200:
                uf = ur.json().get("fields", {})
                has_credentials = bool(uf.get("Portal Username", "").strip())
                user_email = uf.get("Main Contact Email", "").strip()
        except Exception as e:
            print(f"[portal_me] credentials check failed: {e}")

    return Response(json.dumps({
        "agencyName":      agency_name,
        "orgName":         agency_name,   # alias so submitOrder() works without a separate profile fetch
        "email":           user_email,
        "isPrimary":       is_primary,
        "customerId":      customer_id,
        "role":            role,
        "canManageTeam":   can_manage_team,
        "canCreateQuote":  can_create_quote,
        "canAcceptQuote":  can_accept_quote,
        "canViewQuotes":   can_view_quotes,
        "canViewOrders":   can_view_orders,
        "canViewInvoices": can_view_invoices,
        "hasCredentials":  has_credentials,
    }), headers=c, mimetype="application/json")


@app.route("/api/portal/profile", methods=["GET", "OPTIONS"])
@portal_login_required
def portal_profile(user):
    """Return profile for the logged-in user.
    Personal info (name/email) comes from user_id (their own record).
    Company/address info comes from customer_id (parent account record).
    For primary users these are the same record.
    """
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Methods": "GET"})
    c = cors()
    user_id     = user.get("user_id") or user.get("customer_id", "")
    customer_id = user.get("customer_id", "")
    if not user_id:
        return Response(json.dumps({"profile": {}}), headers=c, mimetype="application/json")
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        # Fetch user's own record for personal info
        ur = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{user_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if ur.status_code != 200:
            return Response(json.dumps({"profile": {}}), headers=c, mimetype="application/json")
        uf = ur.json().get("fields", {})

        # For sub-users, also fetch parent company record for address/org info
        cf = uf  # default: same record (primary users)
        if customer_id and customer_id != user_id:
            cr = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
                headers=at_headers(read_token), timeout=10,
            )
            if cr.status_code == 200:
                cf = cr.json().get("fields", {})

        profile = {
            "customerId":  customer_id,
            "contactName": uf.get("Main Contact Name", ""),   # personal — from user record
            "email":       uf.get("Main Contact Email", ""),  # personal — from user record
            "orgName":     cf.get("Organization Name", ""),   # company  — from parent record
            "phone":       cf.get("Main Contact Phone #", ""),
            "addr1":       cf.get("Customer Address (Line 1)", ""),
            "city":        cf.get("Customer City", ""),
            "state":       cf.get("Customer State", ""),
            "zip":         cf.get("Customer Zip Code", ""),
        }
        return Response(json.dumps({"profile": profile}), headers=c, mimetype="application/json")
    except Exception as e:
        print(f"[portal_profile] error: {e}")
        return Response(json.dumps({"profile": {}}), headers=c, mimetype="application/json")


@app.route("/api/portal/update-profile", methods=["POST", "OPTIONS"])
@portal_login_required
def portal_update_profile(user):
    """Update the logged-in user's own contact info in Airtable (their record, not parent company)."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    user_id = user.get("user_id") or user.get("customer_id", "")
    if not user_id:
        return Response(json.dumps({"error": "Not authenticated"}), status=401, headers=c, mimetype="application/json")
    data = request.get_json() or {}
    def pack(city, state, zip_code):
        parts = [p for p in [city, f"{state} {zip_code}".strip()] if p]
        return ", ".join(parts)
    fields = {}
    # Organization Name is intentionally excluded — cannot be changed via portal
    if data.get("contactName"): fields["Main Contact Name"]            = data["contactName"].strip()
    if data.get("email"):       fields["Main Contact Email"]           = data["email"].strip()
    if data.get("phone"):       fields["Main Contact Phone #"]         = data["phone"].strip()
    if data.get("addr1"):       fields["Customer Address (Line 1)"]    = data["addr1"].strip()
    city  = data.get("city",  "").strip()
    state = data.get("state", "").strip()
    zip_  = data.get("zip",   "").strip()
    if city or state or zip_:
        fields["Customer Address (Line 2)"] = pack(city, state, zip_)
    if not fields:
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    try:
        write_token = APPLY_WRITE_TOKEN or RETURNS_WRITE_TOKEN
        pr = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{user_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": fields},
            timeout=15,
        )
        pr.raise_for_status()
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        print(f"[portal_update_profile] error: {e}")
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


_QUOTES_CACHE: dict = {}   # {customer_id: {"ts": float, "data": list}}
_QUOTES_CACHE_TTL = 60    # seconds
_ORDERS_CACHE: dict = {}  # {customer_id: {"ts": float, "data": list}}
_ORDERS_CACHE_TTL = 60    # seconds
_ME_CACHE: dict = {}      # {customer_id: {"ts": float, "data": dict}}
_ME_CACHE_TTL = 300       # seconds (5 min — agency name rarely changes)

@app.route("/api/portal/quotes")
@portal_login_required
def portal_quotes(user):
    c = cors()
    customer_id = user.get("customer_id", "")
    if not customer_id:
        return Response(json.dumps({"quotes": []}), headers=c, mimetype="application/json")

    # Return cached result if fresh enough
    import time as _time_mod
    _cached = _QUOTES_CACHE.get(customer_id)
    if _cached and (_time_mod.time() - _cached["ts"]) < _QUOTES_CACHE_TTL:
        return Response(json.dumps({"quotes": _cached["data"]}), headers=c, mimetype="application/json")

    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        # Note: ARRAYJOIN({Customer}) in Airtable formulas returns the linked record's
        # primary field (name), not its record ID — so we can't filter by customer_id
        # in the formula. Fetch all open quotes and filter in Python instead.
        formula = '{Order Type}="Quote"'
        records = at_get_all(
            MANUAL_ORDERS_TABLE_ID, read_token,
            fields=["Document ID", "Order ID", "Date", "Expiry Date", "MO Is Approved",
                    "Customer", "Hidden from Customer", "MO Line Items"],
            formula=formula,
        )
        records = [r for r in records
                   if customer_id in r.get("fields", {}).get("Customer", [])
                   and not r.get("fields", {}).get("Hidden from Customer", False)]
        from datetime import date as dt_date
        today = _today_utc()

        # Batch-fetch all line items in one AT query using OR(RECORD_ID()=...)
        all_li_ids = []
        for r in records:
            all_li_ids.extend(r.get("fields", {}).get("MO Line Items", []))
        li_total_map = {}  # li_id -> Confirmed Line Item Total
        if all_li_ids:
            chunks = [all_li_ids[i:i+30] for i in range(0, len(all_li_ids), 30)]
            for chunk in chunks:
                formula = "OR(" + ",".join(f'RECORD_ID()="{lid}"' for lid in chunk) + ")"
                li_recs = at_get_all(MO_LINE_ITEMS_TABLE_ID, read_token,
                                     fields=["Confirmed Line Item Total"],
                                     formula=formula)
                for lr in li_recs:
                    li_total_map[lr["id"]] = float(lr.get("fields", {}).get("Confirmed Line Item Total") or 0)

        quotes = []
        for r in records:
            f = r.get("fields", {})
            quote_number = f.get("Document ID", f"QU-{f.get('Order ID','')}")
            expiry_str   = f.get("Expiry Date", "")
            is_expired   = False
            if expiry_str:
                try:
                    is_expired = dt_date.fromisoformat(expiry_str) < today
                except Exception:
                    pass
            total = sum(li_total_map.get(lid, 0) for lid in f.get("MO Line Items", []))
            quotes.append({
                "record_id":    r["id"],
                "quote_number": quote_number,
                "date":         f.get("Date", ""),
                "expiry_date":  expiry_str,
                "is_expired":   is_expired,
                "is_accepted":  bool(f.get("MO Is Approved")),
                "total":        round(total, 2),
            })
        # Sort most recent first
        quotes.sort(key=lambda x: x.get("date", ""), reverse=True)
        _QUOTES_CACHE[customer_id] = {"ts": _time_mod.time(), "data": quotes}
        return Response(json.dumps({"quotes": quotes}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/orders")
@portal_login_required
def portal_orders(user):
    c = cors()
    customer_id = user.get("customer_id", "")
    if not customer_id:
        return Response(json.dumps({"orders": []}), headers=c, mimetype="application/json")

    import time as _time_mod
    import concurrent.futures as _cf

    # Return cached result if fresh
    _cached = _ORDERS_CACHE.get(customer_id)
    if _cached and (_time_mod.time() - _cached["ts"]) < _ORDERS_CACHE_TTL:
        return Response(json.dumps({"orders": _cached["data"]}), headers=c, mimetype="application/json")

    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        # Fetch all SOs and tracking in parallel
        with _cf.ThreadPoolExecutor(max_workers=2) as _ex_po:
            _fut_records  = _ex_po.submit(at_get_all,
                MANUAL_ORDERS_TABLE_ID, read_token,
                fields=["Document ID", "Order ID", "Date", "MO Line Items", "Customer", "Sales Order Status", "Go-to PDF", "Hidden from Customer", "Tracking", "Tracking #", "Purchase Order #"],
                formula='{Order Type}="Sales Order"',
            )
            _fut_tracking = _ex_po.submit(at_get_all,
                _SO_TRACKING_TABLE, _SO_TRACKING_TOKEN,
                fields=["Order #", "Tracking #", "Base Order #"],
                base_id=_SO_TRACKING_BASE,
            )
        records       = _fut_records.result()
        tracking_recs = _fut_tracking.result()
        tracking_map      = {}  # so_number → main tracking
        split_tracking_map = {}  # so_number → list of {split_num, tracking}
        for r in tracking_recs:
            f2 = r.get("fields", {})
            key  = f2.get("Order #", "").strip()
            trk  = f2.get("Tracking #") or ""
            base = f2.get("Base Order #") or ""
            if not key:
                continue
            if base:
                # This is a split — add to parent's split list
                split_tracking_map.setdefault(base, []).append({"splitNum": key, "tracking": trk})
            else:
                tracking_map[key] = trk
        records = [r for r in records
                   if customer_id in r.get("fields", {}).get("Customer", [])
                   and r.get("fields", {}).get("Sales Order Status") == "Approved"
                   and not r.get("fields", {}).get("Hidden from Customer", False)]

        # Batch-fetch all line items in one AT query
        all_li_ids = []
        for r in records:
            all_li_ids.extend(r.get("fields", {}).get("MO Line Items", []))
        li_total_map = {}
        if all_li_ids:
            chunks = [all_li_ids[i:i+30] for i in range(0, len(all_li_ids), 30)]
            for chunk in chunks:
                formula = "OR(" + ",".join(f'RECORD_ID()="{lid}"' for lid in chunk) + ")"
                li_recs = at_get_all(MO_LINE_ITEMS_TABLE_ID, read_token,
                                     fields=["Confirmed Line Item Total"],
                                     formula=formula)
                for lr in li_recs:
                    li_total_map[lr["id"]] = float(lr.get("fields", {}).get("Confirmed Line Item Total") or 0)

        orders = []
        for r in records:
            f = r.get("fields", {})
            so_number = f.get("Document ID", f'SO-{f.get("Order ID","")}')
            total = sum(li_total_map.get(lid, 0) for lid in f.get("MO Line Items", []))
            go_to_pdf_field = f.get("Go-to PDF") or {}
            go_to_pdf_url   = go_to_pdf_field.get("url", "") if isinstance(go_to_pdf_field, dict) else ""
            orders.append({
                "record_id":      r["id"],
                "so_number":      so_number,
                "date":           f.get("Date", ""),
                "total":          round(total, 2),
                "go_to_pdf":      go_to_pdf_url,
                "tracking":       tracking_map.get(so_number, "") or f.get("Tracking #", "") or f.get("Tracking", ""),
                "splitShipments": split_tracking_map.get(so_number, []),
                "po_number":      f.get("Purchase Order #", ""),
            })
        orders.sort(key=lambda x: x.get("date", ""), reverse=True)
        _ORDERS_CACHE[customer_id] = {"ts": _time_mod.time(), "data": orders}
        return Response(json.dumps({"orders": orders}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/users")
@portal_login_required
def portal_users(user):
    c = cors()
    customer_id = user.get("customer_id", "")
    # One login per agency — return just the current user from the Customer record
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        cr = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
            headers=at_headers(read_token),
            params={"fields[]": ["Main Contact Name", "Main Contact Email"]},
            timeout=10,
        )
        if cr.status_code == 200:
            cf = cr.json().get("fields", {})
            current_user = {
                "id":         customer_id,
                "name":       cf.get("Main Contact Name", ""),
                "email":      cf.get("Main Contact Email", ""),
                "is_primary": True,
            }
        else:
            current_user = {"id": customer_id, "name": "", "email": "", "is_primary": True}
        return Response(json.dumps({"users": [current_user]}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/users/add", methods=["POST"])
@portal_login_required
def portal_users_add(user):
    c = cors()
    # One login per agency — additional users are not supported
    return Response(
        json.dumps({"error": "Additional portal users are not supported. Each agency has one login."}),
        status=400, headers=c, mimetype="application/json",
    )


@app.route("/api/portal/users/remove", methods=["POST"])
@portal_login_required
def portal_users_remove(user):
    c = cors()
    # One login per agency — user removal is not supported
    return Response(
        json.dumps({"error": "User removal is not supported. Each agency has one login."}),
        status=400, headers=c, mimetype="application/json",
    )


@app.route("/api/portal/request-magic-link", methods=["POST"])
def portal_request_magic_link():
    c = cors()
    data  = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()

    def _send(email):
        try:
            read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
            records = at_get_all(
                CUSTOMERS_TABLE_ID, read_token,
                fields=["Main Contact Email", "Application Status"],
                formula=f"AND(LOWER({{Main Contact Email}})='{email}',{{Application Status}}='Approved')",
            )
            if records:
                link = generate_magic_link(records[0]["id"])
                send_magic_link_email(email, link)
        except Exception as ex:
            print(f"[request_magic_link] error: {ex}")

    if email:
        threading.Thread(target=_send, args=(email,), daemon=True).start()
    return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")


# ─────────────────────────────────────────────────────────────────────────────
# Customer Portal — Username/Password Login
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/portal/login", methods=["POST", "OPTIONS"])
def portal_login():
    """Authenticate a B2B customer with username + password. Sets ba_portal_session cookie."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}
    username = (data.get("username") or "").strip().lower()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return Response(json.dumps({"error": "Username and password are required"}),
                        status=400, headers=c, mimetype="application/json")
    rec, role, customer_id = _lookup_portal_customer(username, password)
    if not rec:
        return Response(json.dumps({"error": "Invalid username or password"}),
                        status=401, headers=c, mimetype="application/json")
    user_id    = rec["id"]
    is_primary = not rec.get("fields", {}).get("Parent Company")
    jwt_token  = create_portal_token(user_id, customer_id, is_primary, role=role)
    resp = make_response(Response(json.dumps({"ok": True}), headers=c, mimetype="application/json"))
    resp.set_cookie(
        "ba_portal_session", jwt_token,
        max_age=30 * 24 * 3600,
        httponly=True,
        samesite="Lax",
        secure=True,
    )
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Customer Portal — Set Password (migration path for magic-link-only users)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/portal/set-password", methods=["POST"])
@portal_login_required
def portal_set_password(user):
    """Allows a logged-in customer to set Portal Username and Password Hash for the first time."""
    c = cors()
    data     = request.get_json() or {}
    username = (data.get("username") or "").strip().lower()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return Response(json.dumps({"error": "username and password are required"}),
                        status=400, headers=c, mimetype="application/json")
    if len(password) < 8:
        return Response(json.dumps({"error": "Password must be at least 8 characters"}),
                        status=400, headers=c, mimetype="application/json")
    user_id = user.get("user_id", "")
    if not user_id:
        return Response(json.dumps({"error": "Invalid session"}), status=401,
                        headers=c, mimetype="application/json")
    pw_hash     = _hash_password(password)
    write_token = RETURNS_WRITE_TOKEN
    try:
        r = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{user_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {"Portal Username": username, "Portal Hash": pw_hash}},
            timeout=10,
        )
        r.raise_for_status()
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


# ─────────────────────────────────────────────────────────────────────────────
# Customer Portal — Team Management
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/portal/team", methods=["GET"])
@portal_login_required
def portal_team_list(user):
    """List all team members for this customer account. Requires manage_team permission."""
    c = cors()
    if not portal_can(user, "manage_team"):
        return Response(json.dumps({"error": "Insufficient permissions"}),
                        status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id", "")
    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        # Fetch all customers with Portal Username set
        records = at_get_all(
            CUSTOMERS_TABLE_ID, read_token,
            fields=["Main Contact Name", "Organization Name", "Portal Username",
                    "Portal Role", "Parent Company", "Main Contact Email", "Portal Hash"],
            formula=f"OR({{Portal Username}}!='',{{Main Contact Email}}!='')",
        )
        # Also fetch the primary record (may not have Portal Username)
        primary_r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
            headers=at_headers(read_token), timeout=10,
        )
        _role_map = {"Admin": "admin", "Full Access": "full_access", "Orders": "orders", "Quotes Only": "quotes_only", "Invoices": "invoices", "Read Only": "read_only"}
        team     = []
        seen_ids = set()

        # Primary record first
        if primary_r.status_code == 200:
            pf    = primary_r.json().get("fields", {})
            prole = _role_map.get((pf.get("Portal Role") or "").strip(), "admin")
            team.append({
                "id":       customer_id,
                "name":     pf.get("Main Contact Name") or pf.get("Organization Name", ""),
                "username": pf.get("Portal Username", ""),
                "role":     prole,
                "isOwner":  True,
            })
            seen_ids.add(customer_id)

        # Sub-users
        for r in records:
            if r["id"] in seen_ids:
                continue
            f = r.get("fields", {})
            parent_ids = f.get("Parent Company", [])
            if customer_id not in parent_ids:
                continue
            srole = _role_map.get((f.get("Portal Role") or "").strip(), "read_only")
            team.append({
                "id":           r["id"],
                "name":         f.get("Main Contact Name", ""),
                "username":     f.get("Portal Username", ""),
                "email":        f.get("Main Contact Email", ""),
                "role":         srole,
                "isOwner":      False,
                "pendingSetup": not bool(f.get("Portal Hash")),
            })
            seen_ids.add(r["id"])

        return Response(json.dumps({"team": team}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


def _send_portal_invite_email(to_email, invitee_name, inviter_company, setup_link):
    """Send a portal account setup invitation email."""
    if not SENDGRID_API_KEY:
        return
    try:
        first = invitee_name.split()[0] if invitee_name else "there"
        payload = {
            "personalizations": [{"to": [{"email": to_email, "name": invitee_name}]}],
            "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
            "subject": f"You've been invited to the Blue Alpha Portal",
            "content": [{"type": "text/html", "value": f"""
<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;padding:32px 16px;color:#1a2633;">
  <img src="https://bluealphabelts.com/wp-content/uploads/2021/01/Blue-Alpha-Logo-White-Background.png" alt="Blue Alpha" style="height:40px;margin-bottom:24px;">
  <h2 style="font-size:20px;font-weight:700;margin:0 0 12px;">You've been invited</h2>
  <p style="font-size:15px;margin:0 0 16px;">Hi {first},</p>
  <p style="font-size:15px;margin:0 0 24px;">{inviter_company} has invited you to join their Blue Alpha portal account. Click the button below to set up your username and password.</p>
  <a href="{setup_link}" style="display:inline-block;background:#1B2438;color:#fff;font-family:Arial;font-size:15px;font-weight:700;text-decoration:none;padding:14px 36px;border-radius:6px;">Set Up My Account →</a>
  <p style="font-size:12px;color:#888;margin-top:24px;">This link expires in 48 hours. If you weren't expecting this invitation, you can safely ignore this email.</p>
</div>"""}],
        }
        req_lib.post("https://api.sendgrid.com/v3/mail/send",
                     headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
                     json=payload, timeout=10)
    except Exception as e:
        print(f"[portal_invite_email] failed: {e}")


def _generate_portal_invite(record_id, write_token, expiry_hours=48):
    """Generate an invite token for a sub-user and store it in Airtable. Returns setup URL."""
    import secrets
    from datetime import datetime, timezone, timedelta
    token = secrets.token_urlsafe(32)
    expiry = (datetime.now(timezone.utc) + timedelta(hours=expiry_hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    req_lib.patch(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
        headers={**at_headers(write_token), "Content-Type": "application/json"},
        json={"fields": {"Magic Token": token, "Token Expiry": expiry}},
        timeout=10,
    )
    return f"{QUOTE_BASE_URL}/setup-account/{token}"


@app.route("/api/portal/team/add", methods=["POST"])
@portal_login_required
def portal_team_add(user):
    """Add a new team member (sub-user). Sends invite email — sub-user sets own password."""
    c = cors()
    if not portal_can(user, "manage_team"):
        return Response(json.dumps({"error": "Insufficient permissions"}),
                        status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id", "")
    data        = request.get_json() or {}
    first_name  = (data.get("firstName") or "").strip()
    last_name   = (data.get("lastName") or "").strip()
    email       = (data.get("email") or "").strip().lower()
    role        = (data.get("role") or "").strip()

    if not first_name or not last_name or not email:
        return Response(json.dumps({"error": "Please fill in all fields (first name, last name, and email)."}),
                        status=400, headers=c, mimetype="application/json")
    import re as _re
    if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return Response(json.dumps({"error": "Please enter a valid email address."}),
                        status=400, headers=c, mimetype="application/json")
    if role not in ("Admin", "Full Access", "Orders", "Quotes Only", "Invoices", "Read Only"):
        return Response(json.dumps({"error": "Role must be Admin, Full Access, Orders, Quotes Only, Invoices, or Read Only"}),
                        status=400, headers=c, mimetype="application/json")

    write_token = APPLY_WRITE_TOKEN or RETURNS_WRITE_TOKEN
    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        # Fetch company name for invite email
        company_name = ""
        try:
            pr = req_lib.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
                             headers=at_headers(read_token), timeout=10)
            company_name = pr.json().get("fields", {}).get("Organization Name", "Blue Alpha")
        except Exception:
            company_name = "Blue Alpha"

        # Create sub-user record (no username/password yet — pending setup)
        fields = {
            "Main Contact Name":  f"{first_name} {last_name}",
            "Main Contact Email": email,
            "Portal Role":        role,
            "Parent Company":     [customer_id],
            "Application Status": "Approved",
        }
        r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": fields}, timeout=15,
        )
        if not r.ok:
            print(f"[portal_team_add] Airtable error {r.status_code}: {r.text[:300]}")
            return Response(json.dumps({"error": "Failed to create team member. Please try again."}),
                            status=500, headers=c, mimetype="application/json")
        new_id = r.json().get("id", "")

        # Generate invite token and send email
        setup_link = _generate_portal_invite(new_id, write_token)
        _send_portal_invite_email(email, f"{first_name} {last_name}", company_name, setup_link)

        return Response(json.dumps({"ok": True, "id": new_id}), headers=c, mimetype="application/json")
    except Exception as e:
        print(f"[portal_team_add] exception: {e}")
        return Response(json.dumps({"error": "Something went wrong. Please try again."}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/team/<record_id>/resend-invite", methods=["POST"])
@portal_login_required
def portal_team_resend_invite(user, record_id):
    """Resend invite email to a sub-user who hasn't completed setup (no Password Hash)."""
    c = cors()
    if not portal_can(user, "manage_team"):
        return Response(json.dumps({"error": "Insufficient permissions"}),
                        status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id", "")
    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = APPLY_WRITE_TOKEN or RETURNS_WRITE_TOKEN
    try:
        # Verify record belongs to this account and has no password yet
        r = req_lib.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
                        headers=at_headers(read_token), timeout=10)
        f = r.json().get("fields", {})
        if customer_id not in f.get("Parent Company", []):
            return Response(json.dumps({"error": "Not found"}), status=404, headers=c, mimetype="application/json")
        if f.get("Portal Hash"):
            return Response(json.dumps({"error": "User has already completed setup"}),
                            status=400, headers=c, mimetype="application/json")

        # Fetch company name
        company_name = "Blue Alpha"
        try:
            pr = req_lib.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
                             headers=at_headers(read_token), timeout=10)
            company_name = pr.json().get("fields", {}).get("Organization Name", "Blue Alpha")
        except Exception:
            pass

        setup_link = _generate_portal_invite(record_id, write_token)
        _send_portal_invite_email(f.get("Main Contact Email", ""), f.get("Main Contact Name", ""),
                                  company_name, setup_link)
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/team/<record_id>/role", methods=["PATCH"])
@portal_login_required
def portal_team_change_role(user, record_id):
    """Change a team member's role. Cannot change Admin's role."""
    c = cors()
    if not portal_can(user, "manage_team"):
        return Response(json.dumps({"error": "Insufficient permissions"}),
                        status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id", "")
    data     = request.get_json() or {}
    new_role = (data.get("role") or "").strip()
    if new_role not in ("Admin", "Full Access", "Orders", "Quotes Only", "Invoices", "Read Only"):
        return Response(json.dumps({"error": "Role must be Admin, Full Access, Orders, Quotes Only, Invoices, or Read Only"}),
                        status=400, headers=c, mimetype="application/json")
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "User not found"}), status=404,
                            headers=c, mimetype="application/json")
        f = r.json().get("fields", {})
        parent_ids = f.get("Parent Company", [])
        if customer_id not in parent_ids:
            return Response(json.dumps({"error": "Cannot change role of this user"}),
                            status=403, headers=c, mimetype="application/json")
        if (f.get("Portal Role") or "").strip() == "Admin":
            return Response(json.dumps({"error": "Cannot change Admin's role"}),
                            status=403, headers=c, mimetype="application/json")
        pr = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
            headers={**at_headers(RETURNS_WRITE_TOKEN), "Content-Type": "application/json"},
            json={"fields": {"Portal Role": new_role}},
            timeout=10,
        )
        pr.raise_for_status()
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/team/<record_id>/reset-password", methods=["POST"])
@portal_login_required
def portal_team_reset_password(user, record_id):
    """Reset a team member's password. Admins can reset any sub-user's password."""
    c = cors()
    if not portal_can(user, "manage_team"):
        return Response(json.dumps({"error": "Insufficient permissions"}),
                        status=403, headers=c, mimetype="application/json")
    customer_id  = user.get("customer_id", "")
    data         = request.get_json() or {}
    new_password = (data.get("newPassword") or "").strip()
    if len(new_password) < 8:
        return Response(json.dumps({"error": "Password must be at least 8 characters"}),
                        status=400, headers=c, mimetype="application/json")
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "User not found"}), status=404,
                            headers=c, mimetype="application/json")
        f = r.json().get("fields", {})
        parent_ids = f.get("Parent Company", [])
        # Allow if this is the primary record or a sub-user of this customer
        is_primary_record = (record_id == customer_id)
        is_sub_user = (customer_id in parent_ids)
        if not is_primary_record and not is_sub_user:
            return Response(json.dumps({"error": "Cannot reset password for this user"}),
                            status=403, headers=c, mimetype="application/json")
        new_hash = _hash_password(new_password)
        pr = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
            headers={**at_headers(RETURNS_WRITE_TOKEN), "Content-Type": "application/json"},
            json={"fields": {"Portal Hash": new_hash}},
            timeout=10,
        )
        pr.raise_for_status()
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/team/<record_id>", methods=["DELETE"])
@portal_login_required
def portal_team_delete(user, record_id):
    """Delete a sub-user. Cannot delete the primary account or Admin users."""
    c = cors()
    if not portal_can(user, "manage_team"):
        return Response(json.dumps({"error": "Insufficient permissions"}),
                        status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id", "")
    if record_id == customer_id:
        return Response(json.dumps({"error": "Cannot delete the primary account"}),
                        status=400, headers=c, mimetype="application/json")
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "User not found"}), status=404,
                            headers=c, mimetype="application/json")
        f = r.json().get("fields", {})
        parent_ids = f.get("Parent Company", [])
        if customer_id not in parent_ids:
            return Response(json.dumps({"error": "Cannot delete this user"}),
                            status=403, headers=c, mimetype="application/json")
        if (f.get("Portal Role") or "").strip() == "Admin":
            return Response(json.dumps({"error": "Cannot delete Admin users"}),
                            status=403, headers=c, mimetype="application/json")
        dr = req_lib.delete(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
            headers=at_headers(RETURNS_WRITE_TOKEN), timeout=10,
        )
        if dr.status_code not in (200, 204):
            return Response(json.dumps({"error": "Failed to delete user"}),
                            status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


# ─────────────────────────────────────────────────────────────────────────────
# Customer Portal — Quote Actions (duplicate, hide)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/portal/account-info", methods=["GET", "PATCH"])
@portal_login_required
def portal_account_info(user):
    c = cors()
    customer_id = user.get("customer_id", "")
    user_id     = user.get("user_id", customer_id)   # sub-users have their own record
    is_primary  = user.get("is_primary", True)
    if not customer_id:
        return Response(json.dumps({"error": "No customer"}), status=400, headers=c, mimetype="application/json")

    if request.method == "GET":
        try:
            token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
            # Fetch company record for org/address fields
            r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
                headers=at_headers(token),
                timeout=10,
            )
            f = {}
            if r.status_code == 200:
                f = r.json().get("fields", {})
            else:
                print(f"[account_info] AT fetch failed: id={customer_id} status={r.status_code} body={r.text[:300]}")

            # For sub-users, fetch their own record for their personal name/email
            uf = f
            if not is_primary and user_id != customer_id:
                ur = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{user_id}",
                    headers=at_headers(token), timeout=10,
                )
                if ur.status_code == 200:
                    uf = ur.json().get("fields", {})

            return Response(json.dumps({"info": {
                "shipOrg":   f.get("Ship To Name", "") or f.get("Organization Name", ""),
                "shipName":  uf.get("Main Contact Name", "") or (f.get("Main Contact Name", "") if is_primary else ""),
                "shipEmail": uf.get("Main Contact Email", "") or (f.get("Main Contact Email", "") if is_primary else ""),
                "shipPhone": f.get("Main Contact Phone #", ""),
                "shipAddr1": f.get("Customer Address (Line 1)", ""),
                "shipAddr2": f.get("Customer Address (Line 2)", ""),
                "shipCity":  f.get("Customer City", ""),
                "shipState": f.get("Customer State", ""),
                "shipZip":   f.get("Customer Zip Code", ""),
                "billOrg":   f.get("Bill-To Org Name", ""),
                "billName":  f.get("Bill-To Contact Name", ""),
                "billEmail": f.get("Bill-To Contact Email", ""),
                "billPhone": f.get("Bill-To Phone #", ""),
                "billAddr1": f.get("Bill-To Address (Line 1)", ""),
                "billAddr2": f.get("Bill-To Address (Line 2)", ""),
            }}), headers=c, mimetype="application/json")
        except Exception as e:
            return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")

    # PATCH — each user saves their own name/email to their own record;
    # company-level fields (address, org) only saveable by primary/admin
    data = request.get_json() or {}
    write_token = RETURNS_WRITE_TOKEN

    # Personal fields always save to user's own record
    _PERSONAL = {"shipName": "Main Contact Name", "shipEmail": "Main Contact Email"}
    personal_fields = {at_f: data[k] for k, at_f in _PERSONAL.items() if k in data}
    if personal_fields:
        try:
            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{user_id}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": personal_fields}, timeout=15,
            ).raise_for_status()
        except Exception as e:
            return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")

    # Company-level fields save to company record — admin/primary only
    _COMPANY = {
        "shipOrg":   "Ship To Name",
        "shipPhone": "Main Contact Phone #",
        "shipAddr1": "Customer Address (Line 1)", "shipAddr2": "Customer Address (Line 2)",
        "shipCity":  "Customer City",             "shipState": "Customer State",
        "shipZip":   "Customer Zip Code",
        "billOrg":   "Bill-To Org Name",         "billName":  "Bill-To Contact Name",
        "billEmail": "Bill-To Contact Email",     "billPhone": "Bill-To Phone #",
        "billAddr1": "Bill-To Address (Line 1)",  "billAddr2": "Bill-To Address (Line 2)",
    }
    company_fields = {at_f: data[k] for k, at_f in _COMPANY.items() if k in data}
    if company_fields:
        if not portal_can(user, "manage_team"):
            return Response(json.dumps({"error": "Admin access required to update company details"}), status=403, headers=c, mimetype="application/json")
        try:
            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": company_fields}, timeout=15,
            ).raise_for_status()
        except Exception as e:
            return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")

    return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")


@app.route("/api/portal/update-quote-addresses/<record_id>", methods=["POST"])
@portal_login_required
def portal_update_quote_addresses(user, record_id):
    """Update billing/shipping addresses on a quote's customer record without touching expiry or prices."""
    c = cors()
    if not portal_can(user, "create_quote"):
        return Response(json.dumps({"error": "Insufficient permissions"}), status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id", "")
    data = request.get_json() or {}
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        # Verify quote belongs to this customer
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Quote not found"}), status=404, headers=c, mimetype="application/json")
        mo_fields   = r.json().get("fields", {})
        cust_ids    = mo_fields.get("Customer", [])
        if customer_id not in cust_ids:
            return Response(json.dumps({"error": "Not authorized"}), status=403, headers=c, mimetype="application/json")
        cust_id = cust_ids[0]

        # Build update fields — bill-to and ship-to address only; no expiry/price changes
        fields = {}
        if data.get("billOrg"):   fields["Bill-To Org Name"]            = data["billOrg"]
        if data.get("billName"):  fields["Bill-To Contact Name"]         = data["billName"]
        if "billAddr1" in data:   fields["Bill-To Address (Line 1)"]     = data["billAddr1"]
        if "billAddr2" in data:   fields["Bill-To Address (Line 2)"]     = data["billAddr2"]
        # Organization Name is intentionally excluded — Ship To Name is the editable ship-to org
        if data.get("shipOrg"):   fields["Ship To Name"]                 = data["shipOrg"]
        if data.get("shipName"):  fields["Main Contact Name"]            = data["shipName"]
        if "shipAddr1" in data:   fields["Customer Address (Line 1)"]    = data["shipAddr1"]
        if "shipLine2" in data:   fields["Customer Address (Line 2)"]    = data["shipLine2"]

        if fields:
            wr = req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{cust_id}",
                headers={**at_headers(RETURNS_WRITE_TOKEN), "Content-Type": "application/json"},
                json={"fields": fields},
                timeout=15,
            )
            wr.raise_for_status()

        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/duplicate-quote/<record_id>", methods=["POST"])
@portal_login_required
def portal_duplicate_quote(user, record_id):
    """Duplicate an expired quote with current catalog prices. Requires create_quote permission."""
    c = cors()
    if not portal_can(user, "create_quote"):
        return Response(json.dumps({"error": "Insufficient permissions"}),
                        status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id", "")
    from datetime import date as dt_date, timedelta
    token      = RETURNS_WRITE_TOKEN
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        # Fetch original quote
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=15,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Quote not found"}), status=404,
                            headers=c, mimetype="application/json")
        mo     = r.json()
        fields = mo.get("fields", {})
        if fields.get("Order Type") not in ("Quote", "Sales Order"):
            return Response(json.dumps({"error": "Not a quote or order"}), status=400,
                            headers=c, mimetype="application/json")
        cust_ids = fields.get("Customer", [])
        if customer_id not in cust_ids:
            return Response(json.dumps({"error": "Quote not found"}), status=404,
                            headers=c, mimetype="application/json")

        li_ids = fields.get("MO Line Items", [])

        # Get next order ID
        order_id_str = _next_order_id(read_token)
        quote_number = f"QU-{order_id_str}"

        today      = _today_utc()
        today_str  = today.isoformat()
        expiry_str = (today + timedelta(days=90)).isoformat()

        # Create new Manual Order
        mo_body = {"fields": {
            "Order Type":  "Quote",
            "Order ID":    order_id_str,
            "Date":        today_str,
            "Expiry Date": expiry_str,
            "Customer":    [customer_id],
        }}
        if fields.get("Notes from Customer"):
            mo_body["fields"]["Notes from Customer"] = fields["Notes from Customer"]
        if fields.get("Purchase Order #"):
            mo_body["fields"]["Purchase Order #"] = fields["Purchase Order #"]

        mo_r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}",
            headers={**at_headers(token), "Content-Type": "application/json"},
            json=mo_body, timeout=15,
        )
        mo_r.raise_for_status()
        new_mo_id = mo_r.json()["id"]

        # Fetch all line items and SKU prices in parallel
        import concurrent.futures as _cf
        def _fetch_li_dup(li_id):
            try:
                r2 = req_lib.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                                  headers=at_headers(read_token), timeout=10)
                if r2.status_code == 200:
                    return r2.json().get("fields", {})
            except Exception: pass
            return {}
        with _cf.ThreadPoolExecutor(max_workers=20) as ex:
            li_fields_list = list(ex.map(_fetch_li_dup, li_ids))

        sku_ids_to_fetch = [lf.get("Product SKU", [None])[0] for lf in li_fields_list if lf.get("Product SKU")]
        def _fetch_sku_price(sku_id):
            try:
                r2 = req_lib.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}/{sku_id}",
                                  headers=at_headers(read_token), timeout=10)
                if r2.status_code == 200:
                    return sku_id, r2.json().get("fields", {}).get("Sale Price", 0)
            except Exception: pass
            return sku_id, 0
        sku_prices = {}
        if sku_ids_to_fetch:
            with _cf.ThreadPoolExecutor(max_workers=20) as ex:
                for sid, price in ex.map(_fetch_sku_price, sku_ids_to_fetch):
                    sku_prices[sid] = price

        for lf in li_fields_list:
            sku_ids = lf.get("Product SKU", [])
            if not sku_ids: continue
            sku_id        = sku_ids[0]
            qty           = int(lf.get("Qty.", 1) or 1)
            current_price = float(sku_prices.get(sku_id) or lf.get("Confirmed Unit Price", 0))
            req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}",
                headers={**at_headers(token), "Content-Type": "application/json"},
                json={"fields": {"Manual Order": [new_mo_id], "Product SKU": [sku_id],
                                 "Qty.": qty, "Confirmed Unit Price": current_price,
                                 "Confirmed Adj. Unit Price": current_price}}, timeout=15,
            ).raise_for_status()

        # Bust per-customer quotes cache
        _QUOTES_CACHE.pop(customer_id, None)
        _ORDERS_CACHE.pop(customer_id, None)

        return Response(json.dumps({"success": True, "quoteNumber": quote_number, "recordId": new_mo_id}),
                        headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/hide-quote/<record_id>", methods=["POST"])
@portal_login_required
def portal_hide_quote(user, record_id):
    """Mark a quote as Hidden from Customer (soft delete from customer view)."""
    c = cors()
    if not portal_can(user, "create_quote"):
        return Response(json.dumps({"error": "Insufficient permissions"}), status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id", "")
    token      = RETURNS_WRITE_TOKEN
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Quote not found"}), status=404,
                            headers=c, mimetype="application/json")
        f        = r.json().get("fields", {})
        cust_ids = f.get("Customer", [])
        if customer_id not in cust_ids:
            return Response(json.dumps({"error": "Quote not found"}), status=404,
                            headers=c, mimetype="application/json")
        pr = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers={**at_headers(token), "Content-Type": "application/json"},
            json={"fields": {"Hidden from Customer": True}},
            timeout=10,
        )
        pr.raise_for_status()
        _QUOTES_CACHE.pop(customer_id, None)
        _ORDERS_CACHE.pop(customer_id, None)
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


# ─────────────────────────────────────────────────────────────────────────────
# Admin Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/admin")
def admin_page():
    return send_from_directory("static", "admin.html")

@app.route("/cs-status")
def cs_status_page():
    return send_from_directory("static", "cs-status.html")

@app.route("/api/cs-status/applications", methods=["GET", "POST", "OPTIONS"])
def cs_status_applications():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET, POST"})
    c = cors()
    # Accept session-based auth (admin or CS role)
    if not get_portal_role(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        records = at_get_all(
            CUSTOMERS_TABLE_ID, read_token,
            fields=["Organization Name", "Main Contact Name", "Main Contact Email",
                    "Application Status", "Applied Date"],
        )
        apps = []
        for r in records:
            f = r.get("fields", {})
            apps.append({
                "id":           r["id"],
                "orgName":      f.get("Organization Name", ""),
                "contactName":  f.get("Main Contact Name", ""),
                "contactEmail": f.get("Main Contact Email", ""),
                "status":       f.get("Application Status", "Pending"),
                "appliedDate":  f.get("Applied Date", ""),
            })
        # Sort: Pending first, then Approved, then Denied
        order = {"Pending": 0, "Approved": 1, "Denied": 2}
        apps.sort(key=lambda x: (order.get(x["status"], 9), x["appliedDate"]))
        return Response(json.dumps({"applications": apps}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/users", methods=["GET"])
def admin_list_users():
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        formula = "OR({Quote Portal Admin}=1,{Quote Portal CS}=1)"
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}",
            headers=at_headers(read_token),
            params={"filterByFormula": formula,
                    "fields[]": ["Full Name", "Portal Username", "Email", "Quote Portal Admin", "Quote Portal CS"]},
            timeout=10,
        )
        records = r.json().get("records", [])
        users = []
        for rec in records:
            f = rec.get("fields", {})
            users.append({
                "id":       rec["id"],
                "username": f.get("Portal Username", ""),
                "fullName": f.get("Full Name", ""),
                "email":    f.get("Email", ""),
                "role":     "admin" if f.get("Quote Portal Admin") else "cs",
            })
        return Response(json.dumps({"users": users}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/users/add", methods=["POST"])
def admin_add_user():
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    data = request.get_json() or {}
    username  = (data.get("username") or "").strip().lower()
    password  = (data.get("password") or "").strip()
    role      = (data.get("role") or "cs").strip()
    first_name = (data.get("firstName") or "").strip()
    last_name  = (data.get("lastName") or "").strip()
    if not username or not password:
        return Response(json.dumps({"error": "username and password are required"}), status=400, headers=c, mimetype="application/json")
    if role not in ("admin", "cs"):
        return Response(json.dumps({"error": "role must be 'admin' or 'cs'"}), status=400, headers=c, mimetype="application/json")
    if len(password) < 8:
        return Response(json.dumps({"error": "Password must be at least 8 characters"}), status=400, headers=c, mimetype="application/json")
    pw_hash = _hash_password(password)
    fields = {
        "Portal Username":    username,
        "Portal Hash":      pw_hash,
        "Quote Portal Admin": role == "admin",
        "Quote Portal CS":    role == "cs",
    }
    if first_name:
        fields["First Name"] = first_name
    if last_name:
        fields["Last Name"] = last_name
    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = RETURNS_WRITE_TOKEN
    try:
        # Check if an employee with this username already exists — update instead of creating
        existing = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}",
            headers=at_headers(read_token),
            params={"filterByFormula": f"{{Portal Username}}='{username}'", "maxRecords": 1},
            timeout=10,
        )
        existing_records = existing.json().get("records", []) if existing.status_code == 200 else []
        if existing_records:
            record_id = existing_records[0]["id"]
            r = req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}/{record_id}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": fields},
                timeout=15,
            )
        else:
            r = req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": fields},
                timeout=15,
            )
        if r.status_code not in (200, 201):
            return Response(json.dumps({"error": f"Airtable error: {r.text}"}), status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"ok": True, "id": r.json().get("id")}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/users/reset-password/<record_id>", methods=["POST"])
def admin_reset_user_password(record_id):
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    data = request.get_json() or {}
    new_pw = (data.get("newPassword") or "").strip()
    if len(new_pw) < 8:
        return Response(json.dumps({"error": "Password must be at least 8 characters"}), status=400, headers=c, mimetype="application/json")
    new_hash = _hash_password(new_pw)
    write_token = RETURNS_WRITE_TOKEN
    try:
        r = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}/{record_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {"Password Hash": new_hash}},
            timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Failed to update password"}), status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/users/<record_id>", methods=["DELETE"])
def admin_remove_user(record_id):
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    write_token = RETURNS_WRITE_TOKEN
    try:
        r = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}/{record_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {
                "Portal Username":    "",
                "Portal Hash":      "",
                "Quote Portal Admin": False,
                "Quote Portal CS":    False,
            }},
            timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Failed to remove portal access"}), status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/change-password", methods=["POST"])
def portal_change_password():
    """Change password for B2B customers OR admin/CS users."""
    c = cors()
    data       = request.get_json() or {}
    current_pw = (data.get("currentPassword") or "").strip()
    new_pw     = (data.get("newPassword") or "").strip()
    if not current_pw or not new_pw:
        return Response(json.dumps({"error": "currentPassword and newPassword are required"}), status=400, headers=c, mimetype="application/json")
    err = _validate_password(new_pw)
    if err:
        return Response(json.dumps({"error": err}), status=400, headers=c, mimetype="application/json")

    # ── B2B customer path (ba_portal_session) ──────────────────────────────
    customer_user = get_portal_user(request)
    if customer_user:
        user_id = customer_user.get("user_id", "")
        if not user_id:
            return Response(json.dumps({"error": "Session missing user info — please log in again"}), status=401, headers=c, mimetype="application/json")
        read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        write_token = APPLY_WRITE_TOKEN or RETURNS_WRITE_TOKEN
        try:
            # Fetch current hash from Customers table
            cr = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{user_id}",
                headers=at_headers(read_token), timeout=10,
            )
            if cr.status_code != 200:
                return Response(json.dumps({"error": "Could not verify identity"}), status=500, headers=c, mimetype="application/json")
            stored_hash = cr.json().get("fields", {}).get("Portal Hash", "")
            if not stored_hash or not _check_password(current_pw, stored_hash):
                return Response(json.dumps({"error": "Current password is incorrect"}), status=401, headers=c, mimetype="application/json")
            new_hash = _hash_password(new_pw)
            pr = req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{user_id}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": {"Portal Hash": new_hash}}, timeout=10,
            )
            if pr.status_code != 200:
                return Response(json.dumps({"error": "Failed to update password"}), status=500, headers=c, mimetype="application/json")
            return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
        except Exception as e:
            return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")

    # ── Admin / CS path (ba_admin_session) ────────────────────────────────
    role = get_portal_role(request)
    if not role:
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    record_id = get_portal_record_id(request)
    username  = get_portal_username(request)
    if not record_id or not username:
        return Response(json.dumps({"error": "Session missing record info — please log in again"}), status=401, headers=c, mimetype="application/json")
    rec, _ = _lookup_portal_user(username, current_pw)
    if not rec:
        return Response(json.dumps({"error": "Current password is incorrect"}), status=401, headers=c, mimetype="application/json")
    new_hash = _hash_password(new_pw)
    try:
        r = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}/{record_id}",
            headers={**at_headers(RETURNS_WRITE_TOKEN), "Content-Type": "application/json"},
            json={"fields": {"Password Hash": new_hash}}, timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Failed to update password"}), status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    from datetime import datetime, timezone, timedelta
    c = cors()
    data = request.get_json() or {}
    username = (data.get("username") or "").strip().lower()
    pw       = (data.get("password") or "").strip()
    rec, role = _lookup_portal_user(username, pw)
    if not rec:
        return Response(json.dumps({"error": "Invalid username or password"}), status=401, headers=c, mimetype="application/json")

    payload = {
        "admin":    role == 'admin',  # backward compat: approve/deny checks use this
        "role":     role,             # 'admin' or 'cs'
        "username": username,
        "name":     rec.get("fields", {}).get("Full Name", username),
        "record_id": rec.get("id"),
        "exp":      datetime.now(timezone.utc) + timedelta(hours=12),
    }
    token = pyjwt.encode(payload, QUOTE_SECRET_KEY + "_admin", algorithm="HS256")
    resp = make_response(Response(json.dumps({"ok": True, "name": payload["name"], "role": role}), headers=c, mimetype="application/json"))
    resp.set_cookie("ba_admin_session", token, max_age=12*3600, httponly=True, samesite="Lax", secure=True)
    return resp


@app.route("/api/admin/change-password", methods=["POST"])
def admin_change_password():
    """Change password for the logged-in portal user (admin or CS). Kept for backward compat."""
    return portal_change_password()


@app.route("/api/admin/applications")
def admin_applications():
    c = cors()
    if not get_portal_role(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")

    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        records = at_get_all(
            CUSTOMERS_TABLE_ID, read_token,
            fields=["Organization Name", "EIN", "Main Contact Name", "Main Contact Email",
                    "Main Contact Phone #", "Website",
                    "Customer Address (Line 1)", "Customer City", "Customer State", "Customer Zip Code",
                    "Bill-To Contact Name", "Bill-To Contact Email", "Bill-To Phone #",
                    "Bill-To Address (Line 1)", "Bill-To Address (Line 2)",
                    "State Tax Exemption #", "Tax Exempt", "Tax Exemption Certificate", "Tax Rate",
                    "Application Status", "Denial Reason", "Applied Date",
                    "Portal Hash", "Portal Role"],
            formula="NOT({Application Status}='')",
        )
        apps = []
        for r in records:
            f = r.get("fields", {})
            apps.append({
                "id":                    r["id"],
                "company_name":          f.get("Organization Name", ""),
                "ein":                   f.get("EIN", ""),
                "business_phone":        f.get("Main Contact Phone #", ""),
                "website":               f.get("Website", ""),
                "shipping_contact_name":  f.get("Main Contact Name", ""),
                "shipping_contact_email": f.get("Main Contact Email", ""),
                "shipping_contact_phone": f.get("Main Contact Phone #", ""),
                "shipping_addr1":         f.get("Customer Address (Line 1)", ""),
                "shipping_city":          f.get("Customer City", ""),
                "shipping_state":         f.get("Customer State", ""),
                "shipping_zip":           f.get("Customer Zip Code", ""),
                "billing_contact_name":  f.get("Bill-To Contact Name", ""),
                "billing_contact_email": f.get("Bill-To Contact Email", ""),
                "billing_contact_phone": f.get("Bill-To Phone #", ""),
                "billing_addr1":         f.get("Bill-To Address (Line 1)", ""),
                "billing_addr2":         f.get("Bill-To Address (Line 2)", ""),
                "tax_exemption_number":  f.get("State Tax Exemption #", ""),
                "tax_cert_url":          (f.get("Tax Exemption Certificate") or [{}])[0].get("url", ""),
                "tax_cert_filename":     (f.get("Tax Exemption Certificate") or [{}])[0].get("filename", ""),
                "tax_exempt":            bool(f.get("Tax Exempt", True)),
                "tax_rate":              float(f.get("Tax Rate") or 0),
                "status":                f.get("Application Status", "Pending"),
                "denial_reason":         f.get("Denial Reason", ""),
                "applied_date":          f.get("Applied Date", ""),
                "portal_setup":          bool(f.get("Portal Hash", "")),
                "portal_role":           f.get("Portal Role", ""),
            })
        # Sort most recent first
        apps.sort(key=lambda x: x.get("applied_date", ""), reverse=True)
        return Response(json.dumps({"applications": apps}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/approve/<app_id>", methods=["POST"])
def admin_approve(app_id):
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")

    req_data  = request.get_json() or {}
    tax_rate_pct = req_data.get("taxRate")  # percent value e.g. 7.0 → stored as 0.07

    write_token = RETURNS_WRITE_TOKEN
    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

    try:
        # Fetch application
        # Fetch Customer record — app_id IS the Customer record ID
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{app_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Application not found."}), status=404, headers=c, mimetype="application/json")
        f = r.json().get("fields", {})

        company_name  = f.get("Organization Name", "")
        contact_name  = f.get("Main Contact Name", "")
        contact_email = f.get("Main Contact Email", "")

        # Update Application Status to Approved; save tax rate if provided
        approve_fields = {"Application Status": "Approved"}
        if tax_rate_pct is not None:
            try:
                approve_fields["Tax Rate"] = round(float(tax_rate_pct) / 100, 6)
            except (TypeError, ValueError):
                pass
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{app_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": approve_fields},
            timeout=10,
        )

        # Generate setup-account link (7 days) so user can create username + password
        write_token = APPLY_WRITE_TOKEN or RETURNS_WRITE_TOKEN
        magic_link = _generate_portal_invite(app_id, write_token, expiry_hours=168)

        # Send approval email
        send_approval_email(contact_email, contact_name, company_name, magic_link)

        return Response(json.dumps({"success": True, "customerId": app_id}),
                        headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/deny/<app_id>", methods=["POST"])
def admin_deny(app_id):
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")

    data   = request.get_json() or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return Response(json.dumps({"error": "Denial reason is required."}), status=400, headers=c, mimetype="application/json")

    write_token = RETURNS_WRITE_TOKEN
    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

    try:
        # Fetch Customer record — app_id IS the Customer record ID
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{app_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Application not found."}), status=404, headers=c, mimetype="application/json")
        f = r.json().get("fields", {})

        contact_name  = f.get("Main Contact Name", "")
        contact_email = f.get("Main Contact Email", "")
        company_name  = f.get("Organization Name", "")

        # Update Application Status to Denied on the Customer record
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{app_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {"Application Status": "Denied", "Denial Reason": reason}},
            timeout=10,
        )

        # Send denial email
        if contact_email:
            send_denial_email(contact_email, contact_name, company_name, reason)

        return Response(json.dumps({"success": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/customers/<record_id>/send-invite", methods=["POST"])
def admin_send_portal_invite(record_id):
    """Admin: generate & send a portal setup link to an approved customer who hasn't logged in yet."""
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")

    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = APPLY_WRITE_TOKEN or RETURNS_WRITE_TOKEN

    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if not r.ok:
            return Response(json.dumps({"error": "Customer not found"}), status=404, headers=c, mimetype="application/json")
        f = r.json().get("fields", {})

        to_email     = f.get("Main Contact Email", "")
        to_name      = f.get("Main Contact Name", "")
        company_name = f.get("Organization Name", "Blue Alpha Customer")

        if not to_email:
            return Response(json.dumps({"error": "No email address on record"}), status=400, headers=c, mimetype="application/json")

        setup_link = _generate_portal_invite(record_id, write_token)
        _send_portal_invite_email(to_email, to_name, company_name, setup_link)

        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


# ─────────────────────────────────────────────────────────────────────────────
# International Exchange Portal
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/exchange/international")
def international_exchange_portal():
    return send_from_directory("static", "exchange-international.html")


@app.route("/api/verify-international-exchange", methods=["POST", "OPTIONS"])
def verify_international_exchange():
    """Like verify-exchange but: 45-day window, no international/military block."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}
    order_number    = data.get("orderNumber", "").strip().lstrip("#")
    email_input     = data.get("email", "").strip().lower()
    last_name_input = data.get("lastName", "").strip().lower()

    if not order_number or (not email_input and not last_name_input):
        return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

    try:
        from datetime import datetime, timezone, timedelta

        r = req_lib.get("https://ssapi.shipstation.com/orders",
                        params={"orderNumber": order_number},
                        headers=ss_headers(), timeout=10)
        orders = r.json().get("orders", [])

        if not orders:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        order = orders[0]

        # Verify identity — last name OR email must match
        ship_name   = order.get("shipTo", {}).get("name", "").strip()
        order_last  = ship_name.split()[-1].lower() if ship_name else ""
        order_email = (order.get("customerEmail") or "").strip().lower()
        name_match  = last_name_input and last_name_input == order_last
        email_match = email_input and email_input == order_email
        if not name_match and not email_match:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        # Block US domestic orders — they should use the domestic exchange form
        MILITARY_STATES = {"AA", "AE", "AP"}
        country = order.get("shipTo", {}).get("country", "").strip().upper()
        state   = order.get("shipTo", {}).get("state", "").strip().upper()
        if country in ("US", "USA") and state not in MILITARY_STATES:
            return Response(json.dumps({"status": "domestic"}), headers=c, mimetype="application/json")

        # Check ship date within 45 days
        sr = req_lib.get("https://ssapi.shipstation.com/shipments",
                         params={"orderNumber": order_number},
                         headers=ss_headers(), timeout=10)
        shipments = sr.json().get("shipments", [])
        ship_date_str = shipments[0].get("shipDate", "") if shipments else ""

        def parse_dt(s):
            s = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        if ship_date_str:
            ship_date = parse_dt(ship_date_str)
        else:
            od = order.get("orderDate", "")
            ship_date = parse_dt(od) if od else datetime.now(timezone.utc)

        eligible_until = ship_date + timedelta(days=45)
        if datetime.now(timezone.utc) > eligible_until:
            return Response(json.dumps({"status": "outside_window"}), headers=c, mimetype="application/json")

        # Find exchange-eligible items via Airtable
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        all_exchange_options = at_get_all(
            PRODUCT_SKUS_TABLE_ID,
            airtable_read_token,
            fields=["Parent Product"],
            formula="{Can Exchange}=TRUE()",
        )
        eligible_parent_ids = set()
        for opt in all_exchange_options:
            for pid in opt["fields"].get("Parent Product", []):
                eligible_parent_ids.add(pid)

        eligible_items = []
        for item in order.get("items", []):
            sku = (item.get("sku") or "").strip()
            if not sku:
                continue
            at_r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
                params={"filterByFormula": f'{{SKU ID}}="{sku}"', "maxRecords": 1,
                        "fields[]": ["Name + Variations", "SKU ID", "Parent Product"]},
                headers=at_headers(airtable_read_token),
                timeout=10,
            )
            records = at_r.json().get("records", [])
            if not records:
                continue
            rec = records[0]
            parent_products = rec["fields"].get("Parent Product", [])
            parent_product_id = parent_products[0] if parent_products else ""
            if not parent_product_id or parent_product_id not in eligible_parent_ids:
                continue
            eligible_items.append({
                "name":            item.get("name", ""),
                "sku":             sku,
                "quantity":        int(item.get("quantity", 1)),
                "airtableId":      rec["id"],
                "parentProductId": parent_product_id,
            })

        if not eligible_items:
            return Response(json.dumps({"status": "no_eligible_items"}), headers=c, mimetype="application/json")

        # Check for existing exchange orders
        already_exchanged_skus = set()
        next_suffix = "-E"
        try:
            for n in range(1, 10):
                suffix = "-E" if n == 1 else f"-E{n}"
                ex_r = req_lib.get("https://ssapi.shipstation.com/orders",
                                   params={"orderNumber": f"{order_number}{suffix}"},
                                   headers=ss_headers(), timeout=10)
                ex_orders = ex_r.json().get("orders", [])
                if not ex_orders:
                    next_suffix = suffix
                    break
                for ex_order in ex_orders:
                    orig_sku = ((ex_order.get("advancedOptions") or {}).get("customField3") or "").strip()
                    if not orig_sku:
                        notes_text = ex_order.get("internalNotes") or ""
                        m = re.search(r'Original SKUs?:\s*([^\.\n]+)', notes_text)
                        if m:
                            orig_sku = m.group(1).strip()
                    for s in orig_sku.split(","):
                        s = s.strip()
                        if s:
                            already_exchanged_skus.add(s)
        except Exception as ex_check_err:
            print(f"[verify-international-exchange] exchange-order check failed (non-fatal): {ex_check_err}")

        eligible_items = [i for i in eligible_items if i["sku"] not in already_exchanged_skus]

        if not eligible_items:
            return Response(json.dumps({"status": "already_exchanged"}), headers=c, mimetype="application/json")

        ship_to = order.get("shipTo", {})
        return Response(json.dumps({
            "status":        "eligible",
            "orderId":       order.get("orderId"),
            "orderNumber":   order_number,
            "customerName":  ship_to.get("name", ""),
            "customerEmail": order.get("customerEmail", ""),
            "eligibleUntil": eligible_until.strftime("%B %-d, %Y"),
            "isApo":         (country in ("US", "USA") and state in MILITARY_STATES),
            "shipTo": {
                "name":       ship_to.get("name", ""),
                "street1":    ship_to.get("street1", ""),
                "street2":    ship_to.get("street2", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country":    ship_to.get("country", "US"),
            },
            "eligibleItems": eligible_items,
            "nextSuffix":    next_suffix,
        }), headers=c, mimetype="application/json")

    except Exception as e:
        import traceback
        print(f"[verify-international-exchange] ERROR: {e}\n{traceback.format_exc()}")
        return Response(json.dumps({"status": "error", "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/intl-exchange-options", methods=["POST", "OPTIONS"])
def intl_exchange_options():
    """Same logic as /api/exchange-options — reused for international flow."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}
    parent_product_id = data.get("parentProductId", "").strip()

    if not parent_product_id:
        return Response(json.dumps({"options": []}), headers=c, mimetype="application/json")

    try:
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        records = at_get_all(
            PRODUCT_SKUS_TABLE_ID,
            airtable_read_token,
            fields=["Name + Variations", "SKU ID", "Parent Product"],
            formula="{Can Exchange}=TRUE()",
        )
        options = []
        for rec in records:
            fields = rec.get("fields", {})
            if parent_product_id not in fields.get("Parent Product", []):
                continue
            raw_name = fields.get("Name + Variations", "")
            # Exclude "Add Size to Order Notes" options — customer can't specify size there
            if "add size to order notes" in raw_name.lower():
                continue
            sku = fields.get("SKU ID", "")
            # Apply explicit name overrides first; otherwise clean the raw name
            if sku in _EXCHANGE_NAME_OVERRIDES:
                clean_name = _EXCHANGE_NAME_OVERRIDES[sku]
            else:
                clean_name = raw_name.replace(" - Base Only (-ONB)", "").replace(" - Base Only", "").replace(" (-ONB)", "").strip()
                clean_name = re.sub(r'\s+Outer\s*-\s*', ' - ', clean_name).strip()
            options.append({
                "id":   rec["id"],
                "name": clean_name,
                "sku":  sku,
            })
        options.sort(key=lambda x: (1 if "other size" in x["name"].lower() else 0, x["name"]))
        return Response(json.dumps({"options": options}), headers=c, mimetype="application/json")

    except Exception as e:
        return Response(json.dumps({"error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/create-international-checkout", methods=["POST", "OPTIONS"])
def create_international_checkout():
    """Store pending exchange data and create a Stripe Checkout session."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    if not STRIPE_SECRET_KEY:
        return Response(json.dumps({"error": "Stripe not configured"}),
                        status=500, headers=c, mimetype="application/json")

    import uuid
    ref_id = str(uuid.uuid4())

    # Store all exchange data keyed by ref_id (in-memory cache; Airtable is the source of truth)
    _intl_pending[ref_id] = {
        "orderId":       data.get("orderId"),
        "orderNumber":   data.get("orderNumber", ""),
        "customerName":  data.get("customerName", ""),
        "customerEmail": data.get("customerEmail", ""),
        "items":         data.get("items", []),        # list of {originalSku, selectedSku, selectedName, quantity, parentProductId}
        "deliveryAddress": data.get("deliveryAddress", {}),  # {name, street1, street2, city, state, postalCode, country}
        "nextSuffix":    data.get("nextSuffix", "-E"),
        "trackingNumber": data.get("trackingNumber", ""),
        "carrier":        data.get("carrier", ""),
    }

    # Clean up any abandoned pending records for this order
    try:
        order_num_int = int(data.get("orderNumber", ""))
        read_token = AIRTABLE_OPS_TOKEN or AIRTABLE_BASE_TOKEN or RETURNS_WRITE_TOKEN
        write_token = os.environ.get("AIRTABLE_WRITE_TOKEN_2", RETURNS_WRITE_TOKEN)
        search_resp = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}",
            params={
                "filterByFormula": f'AND({{Order #}}={order_num_int}, NOT({{Payment Confirmed}}))',
                "fields[]": ["Order #", "Payment Confirmed"],
                "maxRecords": 10,
            },
            headers=at_headers(read_token),
            timeout=10,
        )
        if search_resp.status_code == 200:
            stale = search_resp.json().get("records", [])
            print(f"[create-international-checkout] Found {len(stale)} stale record(s) to clean up for order {order_num_int}")
            for rec in stale:
                del_resp = req_lib.delete(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}/{rec['id']}",
                    headers=at_headers(write_token),
                    timeout=10,
                )
                if del_resp.status_code in (200, 204):
                    print(f"[create-international-checkout] Deleted stale record {rec['id']}")
                else:
                    print(f"[create-international-checkout] DELETE failed for {rec['id']}: {del_resp.status_code} {del_resp.text}")
        else:
            print(f"[create-international-checkout] Stale record search failed: {search_resp.status_code} {search_resp.text}")
    except Exception as cleanup_err:
        print(f"[create-international-checkout] cleanup error (non-fatal): {cleanup_err}")

    # Write to Airtable immediately so success handler survives redeploys
    try:
        tracking_str = f"{data.get('trackingNumber', '')} ({data.get('carrier', '')})" if data.get('carrier') else data.get('trackingNumber', '')
        items = data.get('items', [])
        delivery_addr = data.get('deliveryAddress', {})
        items_to_exchange = "\n".join(
            f"{i.get('quantity', 1)}x {i.get('originalSku', '')} — {i.get('originalName', i.get('selectedName', ''))}"
            for i in items
        )
        desired_items = "\n".join(
            f"{i.get('quantity', 1)}x {i.get('selectedSku', '')} — {i.get('selectedName', '')}"
            for i in items
        )
        delivery_str = (
            f"{delivery_addr.get('name', '')}\n"
            f"{delivery_addr.get('street1', '')}"
            + (f"\n{delivery_addr['street2']}" if delivery_addr.get('street2') else "")
            + f"\n{delivery_addr.get('city', '')}, {delivery_addr.get('state', '')} {delivery_addr.get('postalCode', '')}\n"
            f"{delivery_addr.get('country', '')}"
        )
        at_fields = {
            "Customer Name":     data.get("customerName", ""),
            "Customer Email":    data.get("customerEmail", ""),
            "Items to Exchange": items_to_exchange,
            "Desired Items":     desired_items,
            "Delivery Address":  delivery_str,
            "Return Tracking #": tracking_str,
            "Stripe Payment ID": ref_id,
            "Original Order ID": str(data.get("orderId", "")) if data.get("orderId") else "",
            "Next Suffix":       data.get("nextSuffix", "-E"),
            "Payment Confirmed": False,
        }
        try:
            at_fields["Order #"] = int(data.get("orderNumber", ""))
        except (ValueError, TypeError):
            pass
        write_token = os.environ.get("AIRTABLE_WRITE_TOKEN_2", RETURNS_WRITE_TOKEN)
        at_resp = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": at_fields},
            timeout=15,
        )
        if at_resp.status_code in (200, 201):
            _intl_pending[ref_id]["airtableRecordId"] = at_resp.json().get("id", "")
        else:
            print(f"[create-international-checkout] Airtable pre-write failed: {at_resp.status_code} {at_resp.text}")
    except Exception as at_err:
        print(f"[create-international-checkout] Airtable pre-write error: {at_err}")

    success_url = f"https://exchange.bluealphabelts.com/exchange/international?success=1&ref={ref_id}"
    cancel_url  = "https://exchange.bluealphabelts.com/exchange/international?cancelled=1"

    try:
        stripe_resp = req_lib.post(
            "https://api.stripe.com/v1/checkout/sessions",
            auth=(STRIPE_SECRET_KEY, ""),
            data={
                "line_items[0][price_data][currency]":                  "usd",
                "line_items[0][price_data][product_data][name]":        "International Belt Exchange Fee",
                "line_items[0][price_data][unit_amount]":               "1000",
                "line_items[0][quantity]":                              "1",
                "mode":                                                 "payment",
                "success_url":                                          success_url,
                "cancel_url":                                           cancel_url,
                "client_reference_id":                                  ref_id,
            },
            timeout=15,
        )
        if stripe_resp.status_code not in (200, 201):
            raise Exception(f"Stripe error {stripe_resp.status_code}: {stripe_resp.text}")

        session = stripe_resp.json()
        checkout_url = session.get("url")
        if not checkout_url:
            raise Exception("No checkout URL returned from Stripe")

        return Response(json.dumps({"url": checkout_url}), headers=c, mimetype="application/json")

    except Exception as e:
        import traceback
        print(f"[create-international-checkout] ERROR: {e}\n{traceback.format_exc()}")
        # Clean up pending record on failure
        _intl_pending.pop(ref_id, None)
        return Response(json.dumps({"error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/submit-apo-exchange", methods=["POST", "OPTIONS"])
def submit_apo_exchange():
    """APO/military exchange: same as international but no Stripe fee.
    Payment Confirmed is set to True immediately."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    import uuid
    ref_id = str(uuid.uuid4())

    _intl_pending[ref_id] = {
        "orderId":         data.get("orderId"),
        "orderNumber":     data.get("orderNumber", ""),
        "customerName":    data.get("customerName", ""),
        "customerEmail":   data.get("customerEmail", ""),
        "items":           data.get("items", []),
        "deliveryAddress": data.get("deliveryAddress", {}),
        "nextSuffix":      data.get("nextSuffix", "-E"),
        "trackingNumber":  data.get("trackingNumber", ""),
        "carrier":         data.get("carrier", ""),
    }

    # Clean up stale unpaid Airtable records for this order
    try:
        order_num_int = int(data.get("orderNumber", ""))
        read_token  = AIRTABLE_OPS_TOKEN or AIRTABLE_BASE_TOKEN or RETURNS_WRITE_TOKEN
        write_token = os.environ.get("AIRTABLE_WRITE_TOKEN_2", RETURNS_WRITE_TOKEN)
        search_resp = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}",
            params={
                "filterByFormula": f"AND({{Order #}}={order_num_int}, NOT({{Payment Confirmed}}))",
                "fields[]": ["Order #", "Payment Confirmed"],
                "maxRecords": 10,
            },
            headers=at_headers(read_token),
            timeout=10,
        )
        if search_resp.status_code == 200:
            stale = search_resp.json().get("records", [])
            for rec in stale:
                req_lib.delete(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}/{rec['id']}",
                    headers=at_headers(write_token),
                    timeout=10,
                )
    except Exception as cleanup_err:
        print(f"[submit-apo-exchange] cleanup error (non-fatal): {cleanup_err}")

    # Write Airtable record with Payment Confirmed = True immediately (no Stripe)
    airtable_record_id = ""
    try:
        tracking_str = f"{data.get('trackingNumber', '')} ({data.get('carrier', '')})" if data.get('carrier') else data.get('trackingNumber', '')
        items = data.get('items', [])
        delivery_addr = data.get('deliveryAddress', {})
        items_to_exchange = "\n".join(
            f"{i.get('quantity', 1)}x {i.get('originalSku', '')} — {i.get('originalName', i.get('selectedName', ''))}"
            for i in items
        )
        desired_items = "\n".join(
            f"{i.get('quantity', 1)}x {i.get('selectedSku', '')} — {i.get('selectedName', '')}"
            for i in items
        )
        delivery_str = (
            f"{delivery_addr.get('name', '')}\n"
            f"{delivery_addr.get('street1', '')}"
            + (f"\n{delivery_addr['street2']}" if delivery_addr.get('street2') else "")
            + f"\n{delivery_addr.get('city', '')}, {delivery_addr.get('state', '')} {delivery_addr.get('postalCode', '')}\n"
            f"{delivery_addr.get('country', '')}"
        )
        from datetime import datetime as _dt
        today_str = _dt.utcnow().strftime("%Y-%m-%d")
        at_fields = {
            "Customer Name":     data.get("customerName", ""),
            "Customer Email":    data.get("customerEmail", ""),
            "Items to Exchange": items_to_exchange,
            "Desired Items":     desired_items,
            "Delivery Address":  delivery_str,
            "Return Tracking #": tracking_str,
            "Stripe Payment ID": ref_id,
            "Original Order ID": str(data.get("orderId", "")) if data.get("orderId") else "",
            "Next Suffix":       data.get("nextSuffix", "-E"),
            "Payment Confirmed": True,
            "Date Submitted":    today_str,
        }
        try:
            at_fields["Order #"] = int(data.get("orderNumber", ""))
        except (ValueError, TypeError):
            pass
        write_token = os.environ.get("AIRTABLE_WRITE_TOKEN_2", RETURNS_WRITE_TOKEN)
        at_resp = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": at_fields},
            timeout=15,
        )
        if at_resp.status_code in (200, 201):
            airtable_record_id = at_resp.json().get("id", "")
            _intl_pending[ref_id]["airtableRecordId"] = airtable_record_id
            print(f"[submit-apo-exchange] Airtable record created: {airtable_record_id}")
        else:
            print(f"[submit-apo-exchange] Airtable write failed: {at_resp.status_code} {at_resp.text}")
            return Response(json.dumps({"success": False, "error": "Failed to save exchange request"}),
                            status=500, headers=c, mimetype="application/json")
    except Exception as at_err:
        import traceback
        print(f"[submit-apo-exchange] Airtable error: {at_err}\n{traceback.format_exc()}")
        return Response(json.dumps({"success": False, "error": str(at_err)}),
                        status=500, headers=c, mimetype="application/json")

    # Send confirmation email
    try:
        customer_email = data.get("customerEmail", "")
        customer_name  = data.get("customerName", "")
        order_number   = data.get("orderNumber", "")
        if SENDGRID_API_KEY and customer_email:
            first_name = customer_name.split()[0] if customer_name else "there"
            email_body = (
                f"Hi {first_name},\n\n"
                f"We've received your size exchange request for order #{order_number}.\n\n"
                f"We'll begin preparing your new belt(s) once we see movement on your shipment.\n\n"
                f"Please allow 2-4 weeks for delivery.\n\n"
                f"Questions? Reply to this email.\n\n"
                f"— Blue Alpha"
            )
            req_lib.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
                json={
                    "personalizations": [{"to": [{"email": TEST_EMAIL_OVERRIDE or customer_email}]}],
                    "from":    {"email": CS_FROM_EMAIL, "name": "Blue Alpha"},
                    "reply_to": {"email": CS_FROM_EMAIL},
                    "subject": f"Your Blue Alpha Size Exchange — Order #{order_number}",
                    "content": [{"type": "text/plain", "value": email_body}],
                },
                timeout=15,
            )
    except Exception as email_err:
        print(f"[submit-apo-exchange] Email error (non-fatal): {email_err}")

    _intl_pending.pop(ref_id, None)
    return Response(json.dumps({"success": True}), headers=c, mimetype="application/json")


@app.route("/api/international-success", methods=["GET"])
def international_success():
    """
    Stripe redirects here after successful payment.
    Looks up the Airtable record by Stripe Payment ID (ref_id), sets the
    "Payment Confirmed" checkbox, sends a confirmation email, then redirects.
    Falls back to Airtable lookup if _intl_pending was wiped by a redeploy.
    """
    ref_id = request.args.get("ref", "").strip()

    if not ref_id:
        return redirect("https://exchange.bluealphabelts.com/exchange/international?success=1")

    # --- Resolve data from in-memory cache or Airtable ---
    airtable_record_id = None
    order_number   = ""
    customer_name  = ""
    customer_email = ""

    if ref_id in _intl_pending:
        # Fast path: still in memory
        pending = _intl_pending.pop(ref_id)
        airtable_record_id = pending.get("airtableRecordId", "")
        order_number   = pending.get("orderNumber", "")
        customer_name  = pending.get("customerName", "")
        customer_email = pending.get("customerEmail", "")
    else:
        # Slow path: redeploy wiped in-memory state — look up from Airtable
        try:
            import urllib.parse
            read_token = AIRTABLE_OPS_TOKEN or AIRTABLE_BASE_TOKEN or RETURNS_WRITE_TOKEN
            formula = urllib.parse.quote(f'{{Stripe Payment ID}}="{ref_id}"')
            lookup_resp = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}"
                f"?filterByFormula={formula}&maxRecords=1",
                headers=at_headers(read_token),
                timeout=15,
            )
            if lookup_resp.status_code == 200:
                records = lookup_resp.json().get("records", [])
                if not records:
                    print(f"[international-success] No Airtable record found for ref_id={ref_id}")
                    return redirect("https://exchange.bluealphabelts.com/exchange/international?success=1")
                rec = records[0]
                airtable_record_id = rec.get("id", "")
                fields = rec.get("fields", {})
                order_number   = str(fields.get("Order #", ""))
                customer_name  = fields.get("Customer Name", "")
                customer_email = fields.get("Customer Email", "")
            else:
                print(f"[international-success] Airtable lookup failed: {lookup_resp.status_code} {lookup_resp.text}")
                return redirect("https://exchange.bluealphabelts.com/exchange/international?success=1")
        except Exception as lookup_err:
            import traceback
            print(f"[international-success] Airtable lookup error: {lookup_err}\n{traceback.format_exc()}")
            return redirect("https://exchange.bluealphabelts.com/exchange/international?success=1")

    # --- Set "Payment Confirmed" ---
    # Skip GET idempotency check — token only has write access, not read.
    # PATCH is idempotent: setting true when already true is harmless.
    already_processed = False
    if airtable_record_id:
        try:
            write_token = os.environ.get("AIRTABLE_WRITE_TOKEN_2", RETURNS_WRITE_TOKEN)
            from datetime import datetime as _dt
            today_str = _dt.utcnow().strftime("%Y-%m-%d")
            patch_resp = req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}/{airtable_record_id}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": {
                    "Payment Confirmed": True,
                    "Date Submitted": today_str,
                }},
                timeout=15,
            )
            print(f"[international-success] PATCH Payment Confirmed: {patch_resp.status_code} {patch_resp.text[:200]}")
            if patch_resp.status_code not in (200, 201):
                print(f"[international-success] Airtable PATCH failed: {patch_resp.status_code} {patch_resp.text}")
        except Exception as patch_err:
            import traceback
            print(f"[international-success] Airtable PATCH error: {patch_err}\n{traceback.format_exc()}")

    if already_processed:
        # Already handled (e.g. duplicate redirect) — just redirect
        return redirect("https://exchange.bluealphabelts.com/exchange/international?success=1")

    # --- Send confirmation email ---
    try:
        if SENDGRID_API_KEY and customer_email:
            first_name = customer_name.split()[0] if customer_name else "there"
            email_body = (
                f"Hi {first_name},\n\n"
                f"We've received your size exchange request for order #{order_number}.\n\n"
                f"We'll begin preparing your new belt(s) once we see movement on your shipment.\n\n"
                f"Please allow 2-4 weeks for international delivery.\n\n"
                f"Questions? Reply to this email.\n\n"
                f"— Blue Alpha"
            )
            req_lib.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
                json={
                    "personalizations": [{"to": [{"email": TEST_EMAIL_OVERRIDE or customer_email}]}],
                    "from":    {"email": CS_FROM_EMAIL, "name": "Blue Alpha"},
                    "reply_to": {"email": CS_FROM_EMAIL},
                    "subject": f"Your Blue Alpha Size Exchange — Order #{order_number}",
                    "content": [{"type": "text/plain", "value": email_body}],
                },
                timeout=15,
            )
    except Exception as email_err:
        print(f"[international-success] Email failed: {email_err}")

    # If called via fetch() from the frontend, return JSON instead of redirect
    if request.headers.get("X-Requested-With") == "fetch" or "application/json" in request.headers.get("Accept", ""):
        return Response(json.dumps({"ok": True}), headers=cors(), mimetype="application/json")
    return redirect(f"https://exchange.bluealphabelts.com/exchange/international?success=1")


@app.route("/api/submit-international-tracking", methods=["POST", "OPTIONS"])
def submit_international_tracking():
    """Customer submits return tracking number for their international exchange."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}
    order_number    = data.get("orderNumber", "").strip().lstrip("#")
    tracking_number = data.get("trackingNumber", "").strip()
    carrier         = data.get("carrier", "").strip()

    if not order_number or not tracking_number:
        return Response(json.dumps({"success": False, "error": "Missing order number or tracking number"}),
                        status=400, headers=c, mimetype="application/json")

    try:
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        # Find the Airtable record for this order number
        search_resp = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}",
            params={
                "filterByFormula": f'{{Order Number}}="{order_number}"',
                "maxRecords": 1,
            },
            headers=at_headers(airtable_read_token),
            timeout=10,
        )
        records = search_resp.json().get("records", [])
        if not records:
            return Response(json.dumps({"success": False, "error": "No exchange request found for this order number"}),
                            headers=c, mimetype="application/json")

        record = records[0]
        record_id = record["id"]
        current_status = record.get("fields", {}).get("Status", "")

        # Only update if in a valid state
        if current_status not in ("Awaiting Return Shipment", "Tracking Submitted"):
            return Response(json.dumps({
                "success": False,
                "error": f"Cannot submit tracking — current status is '{current_status}'"
            }), headers=c, mimetype="application/json")

        # Update the record
        update_resp = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}/{record_id}",
            headers={**at_headers(RETURNS_WRITE_TOKEN), "Content-Type": "application/json"},
            json={"fields": {
                "Return Tracking #": tracking_number,
                "Return Carrier":    carrier,
                "Status":            "Tracking Submitted",
            }},
            timeout=10,
        )
        if update_resp.status_code not in (200, 201):
            raise Exception(f"Airtable update failed: {update_resp.status_code} {update_resp.text}")

        return Response(json.dumps({"success": True}), headers=c, mimetype="application/json")

    except Exception as e:
        import traceback
        print(f"[submit-international-tracking] ERROR: {e}\n{traceback.format_exc()}")
        return Response(json.dumps({"success": False, "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/confirm-international-movement/<record_id>", methods=["POST", "OPTIONS"])
def confirm_international_movement(record_id):
    """
    CS-triggered endpoint: looks up the Airtable record, creates a ShipStation exchange order,
    and updates the record with the new exchange order number and status 'Order Created'.
    """
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()

    try:
        from datetime import datetime, timezone

        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        # 1. Look up the AT record
        rec_resp = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}/{record_id}",
            headers=at_headers(airtable_read_token),
            timeout=10,
        )
        if rec_resp.status_code == 404:
            return Response(json.dumps({"success": False, "error": "Record not found"}),
                            status=404, headers=c, mimetype="application/json")
        rec_resp.raise_for_status()
        record = rec_resp.json()
        fields = record.get("fields", {})

        order_number    = str(fields.get("Order #", "") or fields.get("Order Number", ""))
        customer_name   = fields.get("Customer Name", "")
        customer_email  = fields.get("Customer Email", "")
        desired_items_raw = fields.get("Desired Items", "")
        delivery_addr_raw = fields.get("Delivery Address", "")
        next_suffix     = fields.get("Next Suffix", "-E")
        original_order_id = fields.get("Original Order ID", "")
        date_submitted  = fields.get("Date Submitted", today_iso) or today_iso

        # Find next available suffix — skip any existing cancelled orders
        def _next_intl_suffix(base_order_num, start_suffix):
            idx = 1
            if start_suffix.startswith("-E") and len(start_suffix) > 2:
                try: idx = int(start_suffix[2:])
                except: idx = 1
            for attempt in range(idx, idx + 20):
                sfx = "-E" if attempt == 1 else f"-E{attempt}"
                candidate = f"{base_order_num}{sfx}"
                try:
                    r = req_lib.get("https://ssapi.shipstation.com/orders",
                                    params={"orderNumber": candidate, "pageSize": 5},
                                    headers=ss_headers(), timeout=10)
                    if r.status_code == 200:
                        orders = r.json().get("orders", [])
                        if not orders:
                            return sfx  # Unused — take it
                        active = [o for o in orders if o.get("orderStatus") != "cancelled"]
                        if not active:
                            return sfx  # Only cancelled orders — reuse this number
                        # Active order exists, try next suffix
                    else:
                        return sfx
                except Exception:
                    return sfx
            return f"-E{idx + 20}"

        next_suffix = _next_intl_suffix(order_number, next_suffix)
        exchange_order_number = f"{order_number}{next_suffix}"

        # 2. Parse desired items  (format: "1x SKU-123 — Item Name\n...")
        items_payload = []
        for line in desired_items_raw.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            # e.g. "2x BA-SKU-XL — Blue Alpha Duty Belt XL Black"
            m = re.match(r'^(\d+)x\s+(\S+)\s+[—\-]+\s+(.+)$', line)
            if m:
                qty          = int(m.group(1))
                selected_sku = m.group(2)
                selected_name = m.group(3)
            else:
                # Fallback: try splitting on whitespace
                parts = line.split(None, 1)
                qty_str = parts[0].rstrip('x') if parts else "1"
                try:
                    qty = int(qty_str)
                except ValueError:
                    qty = 1
                selected_sku  = parts[0] if len(parts) > 0 else ""
                selected_name = parts[1] if len(parts) > 1 else line
            items_payload.append({
                "selectedSku":  selected_sku,
                "selectedName": selected_name,
                "quantity":     qty,
            })

        # 3. Parse delivery address (multi-line text field)
        addr_lines = [l.strip() for l in delivery_addr_raw.strip().splitlines() if l.strip()]
        ship_to = {
            "name":       addr_lines[0] if len(addr_lines) > 0 else customer_name,
            "street1":    addr_lines[1] if len(addr_lines) > 1 else "",
            "street2":    addr_lines[2] if len(addr_lines) > 2 else "",
            "city":       "",
            "state":      "",
            "postalCode": "",
            "country":    "",
        }
        # Try to parse "City, State PostalCode" line
        if len(addr_lines) >= 3:
            city_state_line = addr_lines[-2] if len(addr_lines) >= 4 else (addr_lines[2] if len(addr_lines) == 3 else "")
            country_line    = addr_lines[-1]
            # "city, state postalcode" pattern
            csz_m = re.match(r'^(.+),\s*(\S+)\s+(\S+)$', city_state_line)
            if csz_m:
                ship_to["city"]       = csz_m.group(1).strip()
                ship_to["state"]      = csz_m.group(2).strip()
                ship_to["postalCode"] = csz_m.group(3).strip()
            ship_to["country"] = country_line

        # 4. Build ShipStation order items (same LP inner logic as submit-exchange)
        airtable_rt = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        # Routing tag: EDC/LP vs Battle/Duty — based on item names
        all_names_lower = " ".join(i.get("selectedName", "") for i in items_payload).lower()
        if any(k in all_names_lower for k in ["edc", "low profile", "inner only", "1.5"]):
            tag_id = 105813
        else:
            tag_id = 102014

        today     = datetime.now(timezone.utc)
        today_iso = today.isoformat()

        LP_COLOR_MAP = [
            (["mc black", "mc tropic", "woodland", "black"], "Black"),
            (["coyote brown", "coyote", "mc australian", "mc arid"], "Coyote Brown"),
            (["mc classic", "multicam"],                             "Multicam"),
            (["ranger green", "ranger", "od green"],                 "OD Green"),
            (["wolf gray"],                                          "Wolf Gray"),
        ]

        def _lp_inner_item_intl(color, size, key_suffix):
            search_str    = f"LP INNER ONLY Belt {color} {size}"
            inner_formula = (
                f'AND(NOT(SEARCH("WPS",{{Name + Variations}})),'
                f'SEARCH("{search_str}",{{Name + Variations}}))'
            )
            recs = at_get_all(
                PRODUCT_SKUS_TABLE_ID, airtable_rt,
                fields=["Name + Variations", "SKU ID"],
                formula=inner_formula,
            )
            if recs:
                ir = recs[0]["fields"]
                return {
                    "lineItemKey":    key_suffix,
                    "name":          ir.get("Name + Variations", ""),
                    "sku":           ir.get("SKU ID", ""),
                    "quantity":      1,
                    "unitPrice":     0.00,
                    "taxAmount":     0.00,
                    "shippingAmount": 0.00,
                }
            return None

        def _extract_lp_color_size_intl(name):
            name_lower = name.lower()
            color = None
            for keywords, c_name in LP_COLOR_MAP:
                for kw in keywords:
                    if kw in name_lower:
                        color = c_name
                        break
                if color:
                    break
            size_matches = re.findall(r'(?<!\d)(\d{2})(?!\d)', name)
            size = next((s for s in size_matches if 24 <= int(s) <= 64), None)
            return color, size

        order_items = []
        for item_idx, item_data in enumerate(items_payload):
            selected_sku  = item_data.get("selectedSku", "")
            selected_name = item_data.get("selectedName", "")
            quantity      = int(item_data.get("quantity", 1))

            order_items.append({
                "lineItemKey":    f"exchange-{item_idx + 1}",
                "name":          selected_name,
                "sku":           selected_sku,
                "quantity":      quantity,
                "unitPrice":     0.00,
                "taxAmount":     0.00,
                "shippingAmount": 0.00,
            })

            # LP inner lookup (same three paths as domestic)
            selected_is_onb = bool(re.search(r'-ONB$', selected_sku, re.IGNORECASE))
            inner_added = False

            if selected_is_onb:
                try:
                    size_m     = re.search(r'-(\d+)-ONB$', selected_sku, re.IGNORECASE)
                    inner_size = size_m.group(1) if size_m else None
                    name_lower = selected_name.lower()
                    inner_color = None
                    for keywords, color in LP_COLOR_MAP:
                        for kw in keywords:
                            if kw in name_lower:
                                inner_color = color
                                break
                        if inner_color:
                            break
                    if inner_color and inner_size:
                        item = _lp_inner_item_intl(inner_color, inner_size,
                                                   f"exchange-{item_idx + 1}-inner")
                        if item:
                            item["quantity"] = quantity
                            order_items.append(item)
                            inner_added = True
                except Exception as lp_err:
                    print(f"[confirm-intl-movement] ONB LP inner lookup failed: {lp_err}")

            elif not re.search(r'(-O)$', selected_sku, re.IGNORECASE):
                try:
                    combo_sku = re.sub(r'(-ONB|-O)$', '', selected_sku, flags=re.IGNORECASE).strip()
                    combo_recs = req_lib.get(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
                        params={"filterByFormula": f'{{SKU ID}}="{combo_sku}"', "maxRecords": 1,
                                "fields[]": ["Component(s)"]},
                        headers=at_headers(airtable_rt), timeout=10
                    ).json().get("records", [])
                    component_ids = combo_recs[0]["fields"].get("Component(s)", []) if combo_recs else []
                    for comp_id in component_ids:
                        comp_rec    = req_lib.get(
                            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}/{comp_id}",
                            headers=at_headers(airtable_rt), timeout=10
                        ).json()
                        comp_fields = comp_rec.get("fields", {})
                        comp_name   = comp_fields.get("Name + Variations", "")
                        comp_sku    = comp_fields.get("SKU ID", "")
                        if "inner" not in comp_name.lower():
                            continue
                        order_items.append({
                            "lineItemKey":    f"exchange-{item_idx + 1}-inner",
                            "name":          comp_name,
                            "sku":           comp_sku,
                            "quantity":      quantity,
                            "unitPrice":     0.00,
                            "taxAmount":     0.00,
                            "shippingAmount": 0.00,
                        })
                        inner_added = True
                        break
                except Exception as lp_err:
                    print(f"[confirm-intl-movement] Component LP inner lookup failed: {lp_err}")

            # Path 3: parent-based LP inner lookup
            if not inner_added:
                # We don't have parentProductId stored in Airtable, so look it up via SKU
                try:
                    sku_recs = req_lib.get(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
                        params={"filterByFormula": f'{{SKU ID}}="{selected_sku}"', "maxRecords": 1,
                                "fields[]": ["Parent Product"]},
                        headers=at_headers(airtable_rt), timeout=10
                    ).json().get("records", [])
                    item_parent_id = ""
                    if sku_recs:
                        parents = sku_recs[0]["fields"].get("Parent Product", [])
                        item_parent_id = parents[0] if parents else ""
                    if item_parent_id in _LP_INNER_REQUIRED_PARENT_IDS:
                        inner_color, inner_size = _extract_lp_color_size_intl(selected_name)
                        if inner_color and inner_size:
                            item = _lp_inner_item_intl(inner_color, inner_size,
                                                       f"exchange-{item_idx + 1}-inner")
                            if item:
                                item["quantity"] = quantity
                                order_items.append(item)
                                inner_added = True
                except Exception as lp_err:
                    print(f"[confirm-intl-movement] Path3 LP inner lookup failed: {lp_err}")

        original_skus_csv = fields.get("Items to Exchange", "")

        # 5. Create ShipStation order — GlobalPost Economy International
        order_payload = {
            "orderNumber":   exchange_order_number,
            "orderDate":     date_submitted,
            "paymentDate":   date_submitted,
            "orderStatus":   "awaiting_shipment",
            "customerEmail": customer_email,
            "billTo": {
                "name":       ship_to.get("name", customer_name),
                "street1":    ship_to.get("street1", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country":    ship_to.get("country", ""),
            },
            "shipTo": {
                "name":       ship_to.get("name", customer_name),
                "street1":    ship_to.get("street1", ""),
                "street2":    ship_to.get("street2", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country":    ship_to.get("country", ""),
            },
            "items": order_items,
            "amountPaid":     0.00,
            "taxAmount":      0.00,
            "shippingAmount": 0.00,
            "carrierCode":    "stamps_com",
            "serviceCode":    "globalpost_economy",
            "internalNotes":  f"International exchange for order #{order_number}. Customer ships belt(s) to us first.",
            "advancedOptions": {
                "storeId":      SIZING_EXCHANGE_STORE_ID,
                "customField1": f"Intl exchange for order #{order_number}",
                "customField3": original_skus_csv,
            },
        }

        ss_resp = req_lib.post(
            "https://ssapi.shipstation.com/orders/createorder",
            headers={**ss_headers(), "Content-Type": "application/json"},
            json=order_payload,
            timeout=20,
        )
        if ss_resp.status_code not in (200, 201):
            raise Exception(f"ShipStation order creation failed: {ss_resp.status_code} {ss_resp.text}")

        new_order    = ss_resp.json()
        new_order_id = new_order.get("orderId")

        # Add routing tag
        try:
            req_lib.post(
                "https://ssapi.shipstation.com/orders/addtag",
                headers={**ss_headers(), "Content-Type": "application/json"},
                json={"orderId": new_order_id, "tagId": tag_id},
                timeout=10,
            )
        except Exception as tag_err:
            print(f"[confirm-intl-movement] Routing tag failed: {tag_err}")

        # Add Expedite tag
        try:
            req_lib.post(
                "https://ssapi.shipstation.com/orders/addtag",
                headers={**ss_headers(), "Content-Type": "application/json"},
                json={"orderId": new_order_id, "tagId": 49845},
                timeout=10,
            )
        except Exception as tag_err:
            print(f"[confirm-intl-movement] Expedite tag failed: {tag_err}")

        # 6. Update Airtable record
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}/{record_id}",
            headers={**at_headers(RETURNS_WRITE_TOKEN), "Content-Type": "application/json"},
            json={"fields": {
                "Exchange Order #": exchange_order_number,
                "Review Status":    "Complete",
            }},
            timeout=10,
        )

        return Response(json.dumps({
            "success":             True,
            "exchangeOrderNumber": exchange_order_number,
            "shipStationOrderId":  new_order_id,
        }), headers=c, mimetype="application/json")

    except Exception as e:
        import traceback
        print(f"[confirm-international-movement] ERROR: {e}\n{traceback.format_exc()}")
        return Response(json.dumps({"success": False, "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/cron-intl-exchange", methods=["GET", "POST"])
def cron_intl_exchange():
    """
    Called by Make daily at 11 PM ET.
    Finds international exchange records where Movement Confirmed = true
    and Exchange Order # is blank, then creates ShipStation orders for each.
    """
    read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    results = []
    try:
        formula = 'AND({Movement Confirmed}=TRUE(), {Exchange Order #}="")'
        records = at_get_all(
            INT_EXCHANGE_TABLE_ID, read_token,
            fields=["Exchange Order #", "Customer Name"],
            formula=formula,
        )
        print(f"[cron-intl-exchange] {len(records)} record(s) to process")
        for rec in records:
            record_id = rec["id"]
            try:
                resp = req_lib.post(
                    f"https://exchange.bluealphabelts.com/api/confirm-international-movement/{record_id}",
                    timeout=30,
                )
                result = resp.json()
                results.append({"recordId": record_id, "success": result.get("success"),
                                 "order": result.get("exchangeOrderNumber"), "error": result.get("error")})
                print(f"[cron-intl-exchange] {record_id} → {result.get('exchangeOrderNumber')} ok={result.get('success')}")
            except Exception as rec_err:
                results.append({"recordId": record_id, "success": False, "error": str(rec_err)})
                print(f"[cron-intl-exchange] {record_id} ERROR: {rec_err}")
    except Exception as e:
        import traceback
        print(f"[cron-intl-exchange] OUTER ERROR: {e}\n{traceback.format_exc()}")
        return Response(json.dumps({"success": False, "error": str(e), "results": results}),
                        status=500, mimetype="application/json")
    return Response(json.dumps({"success": True, "processed": len(results), "results": results}),
                    mimetype="application/json")


def _ontime_bg_worker():
    """Refresh on-time cache on startup (if no data), then daily at 9 PM ET."""
    import time as _t
    from zoneinfo import ZoneInfo
    from datetime import timedelta
    _t.sleep(5)  # brief delay so app finishes starting up
    # Refresh on startup if we have no data yet
    if _ONTIME_CACHE["data"] is None:
        try:
            _refresh_ontime_cache()
        except Exception as exc:
            print(f'[ontime-bg] startup refresh error: {exc}')
    while True:
        try:
            et_tz  = ZoneInfo('America/New_York')
            now_et = datetime.now(et_tz)
            next_run = now_et.replace(hour=21, minute=0, second=0, microsecond=0)
            if now_et >= next_run:
                next_run += timedelta(days=1)
            sleep_secs = (next_run - now_et).total_seconds()
            print(f'[ontime-bg] sleeping {sleep_secs/3600:.1f}h until {next_run.strftime("%Y-%m-%d %H:%M ET")}')
            _t.sleep(sleep_secs)
            _refresh_ontime_cache()
        except Exception as exc:
            print(f'[ontime-bg] error: {exc}')
            _t.sleep(300)  # back off 5 min on error

_INVOICES_CACHE: dict = {}  # {customer_id: {"ts": float, "data": list}}
_INVOICES_CACHE_TTL = 60   # seconds


@app.route("/api/portal/admin/shipped-orders")
@portal_login_required
def portal_admin_shipped_orders(user):
    """List shipped (tracking available) approved SOs with invoiced status. Admin only."""
    c = cors()
    if not portal_can(user, "manage_team"):
        return Response(json.dumps({"error": "Insufficient permissions"}), status=403, headers=c, mimetype="application/json")
    try:
        import time as _time_mod
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        # Fetch approved SOs (after 2026-04-30)
        so_records = at_get_all(
            MANUAL_ORDERS_TABLE_ID, read_token,
            fields=["Document ID", "Order ID", "Date", "Bill-To Org Name (from Customer)",
                    "MO Line Items", "Adj. Unit Price (from MO Line Items)"],
            formula='AND({Order Type}="Sales Order",{Sales Order Status}="Approved",IS_AFTER({Date},"2026-04-30"))',
        )

        # Fetch all existing invoices to know which SOs are already invoiced
        inv_records = at_get_all(
            MANUAL_ORDERS_TABLE_ID, read_token,
            fields=["Order ID", "Document ID"],
            formula='{Order Type}="Invoice"',
        )
        # Build dicts: order_id -> inv_number, order_id -> inv_record_id
        invoiced_ids     = {}
        invoiced_rec_ids = {}
        for ir in inv_records:
            f = ir.get("fields", {})
            oid = str(f.get("Order ID", "")).strip()
            doc = f.get("Document ID", "")
            if oid:
                invoiced_ids[oid]     = doc
                invoiced_rec_ids[oid] = ir["id"]

        # Fetch tracking + ship date + base order from SO Tracking Link
        tracking_recs = at_get_all(
            _SO_TRACKING_TABLE, _SO_TRACKING_TOKEN,
            fields=["Order #", "Tracking #", "Ship Date", "Base Order #", "Order Total"],
            base_id=_SO_TRACKING_BASE,
        )
        tracking_map    = {}
        ship_date_map   = {}
        base_order_map  = {}
        order_total_map = {}
        for r in tracking_recs:
            f2 = r.get("fields", {})
            key = f2.get("Order #", "").strip()
            if key:
                tracking_map[key]    = f2.get("Tracking #") or ""
                ship_date_map[key]   = f2.get("Ship Date") or ""
                base_order_map[key]  = f2.get("Base Order #") or ""
                order_total_map[key] = f2.get("Order Total")

        so_by_doc = {rec.get("fields", {}).get("Document ID", ""): rec for rec in so_records}

        orders = []

        # Main orders
        for rec in so_records:
            f = rec.get("fields", {})
            so_number  = f.get("Document ID", "")
            order_id   = str(f.get("Order ID", "")).strip()
            tracking   = tracking_map.get(so_number, "")
            ship_date  = ship_date_map.get(so_number, "")
            # Include pickup orders (shipped but no tracking) if they have a ship date
            if not tracking and not ship_date:
                continue
            org_name_list = f.get("Bill-To Org Name (from Customer)", [])
            org_name = org_name_list[0] if org_name_list else ""
            ss_total = order_total_map.get(so_number)
            total    = round(float(ss_total), 2) if ss_total else 0
            orders.append({
                "record_id":     rec["id"],
                "so_number":     so_number,
                "order_id":      order_id,
                "date":          f.get("Date", ""),
                "ship_date":     ship_date_map.get(so_number, ""),
                "org_name":      org_name,
                "total":         total,
                "tracking":      tracking,
                "invoiced":      order_id in invoiced_ids,
                "inv_number":    invoiced_ids.get(order_id),
                "inv_record_id": invoiced_rec_ids.get(order_id, ""),
                "is_split":      False,
                "split_suffix":  "",
            })

        # Split orders
        for order_num, base_order_num in base_order_map.items():
            if not base_order_num:
                continue
            tracking  = tracking_map.get(order_num, "")
            ship_date = ship_date_map.get(order_num, "")
            if not tracking and not ship_date:
                continue
            parent_rec = so_by_doc.get(base_order_num)
            if not parent_rec:
                continue
            pf = parent_rec.get("fields", {})
            order_id_base = str(pf.get("Order ID", "")).strip()
            split_suffix  = order_num[len(base_order_num):].lstrip("-")
            split_order_id = f"{order_id_base}-{split_suffix}" if split_suffix else order_id_base
            org_name_list  = pf.get("Bill-To Org Name (from Customer)", [])
            org_name = org_name_list[0] if org_name_list else ""
            orders.append({
                "record_id":     parent_rec["id"],
                "so_number":     order_num,
                "order_id":      split_order_id,
                "date":          pf.get("Date", ""),
                "ship_date":     ship_date_map.get(order_num, ""),
                "org_name":      org_name,
                "total":         round(float(order_total_map.get(order_num) or 0), 2),
                "tracking":      tracking,
                "invoiced":      split_order_id in invoiced_ids,
                "inv_number":    invoiced_ids.get(split_order_id),
                "inv_record_id": invoiced_rec_ids.get(split_order_id, ""),
                "is_split":      True,
                "split_suffix":  split_suffix,
            })

        orders.sort(key=lambda x: (x.get("date", ""), x.get("so_number", "")), reverse=True)
        return Response(json.dumps({"orders": orders}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


def _fetch_so_line_items(record_id, split_order_number=None):
    """Shared helper: fetch Airtable line items for an SO record.
    If split_order_number is given (e.g. 'SO-0337-1'), fetches that ShipStation order's
    items, matches by SKU to Airtable line items, and pre-checks those with SS quantities."""
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    r = req_lib.get(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
        headers=at_headers(read_token), timeout=15,
    )
    if r.status_code != 200:
        return None, f"SO not found (AT {r.status_code})"
    so_fields = r.json().get("fields", {})
    li_ids = so_fields.get("MO Line Items", [])

    import concurrent.futures as _cfli
    def _fetch_li(li_id):
        try:
            lr = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                headers=at_headers(read_token), timeout=10,
            )
            if lr.status_code == 200:
                return li_id, lr.json()
        except Exception:
            pass
        return li_id, None
    with _cfli.ThreadPoolExecutor(max_workers=10) as _exli:
        _resli = list(_exli.map(_fetch_li, li_ids))

    line_items = []
    for li_id, li_data in _resli:
        if not li_data:
            continue
        lf = li_data.get("fields", {})
        pname = (lf.get("Product Name (from Product SKU)") or [None])[0] or \
                (lf.get("Name + Variations (from Product SKU)") or [None])[0] or ""
        confirmed_price = lf.get("Confirmed Unit Price")
        adj_list = lf.get("Adj. Unit Price (from MO Line Items)", [])
        price = confirmed_price if confirmed_price is not None else (float(adj_list[0]) if adj_list else 0)
        sku_ids = lf.get("SKU ID (from Product SKU)", [])
        sku_str = sku_ids[0].strip().upper() if sku_ids else ""
        line_items.append({
            "lineItemId":    li_id,
            "name":          pname,
            "qty":           lf.get("Qty.", 0),
            "unit_price":    float(price),
            "sku":           sku_str,
            "defaultChecked": True,   # default: all checked; may be overridden for splits
        })

    def _ss_items_for_order(order_num):
        """Query ShipStation orders API and return {sku: qty} for exact order number match."""
        ss_r = req_lib.get("https://ssapi.shipstation.com/orders",
                            params={"orderNumber": order_num, "pageSize": 5},
                            headers=ss_headers(), timeout=15)
        result = {}
        if ss_r.ok:
            for order in ss_r.json().get("orders", []):
                if order.get("orderNumber", "").strip() != order_num:
                    continue  # exact match only
                for item in order.get("items", []):
                    sku = (item.get("sku") or "").strip().upper()
                    qty = int(item.get("quantity") or 0)
                    if sku:
                        result[sku] = result.get(sku, 0) + qty
        return result

    # If this is a split order, fetch SS items and match by SKU
    if split_order_number:
        ss_items = _ss_items_for_order(split_order_number)
        if ss_items:
            for li in line_items:
                sku = li["sku"]
                if sku in ss_items:
                    li["defaultChecked"] = True
                    li["qty"] = ss_items[sku]
                else:
                    li["defaultChecked"] = False

    else:
        # Main order — check if it has splits in ShipStation and use SS quantities if so
        so_number = so_fields.get("Document ID", "")
        if so_number:
            tracking_recs = at_get_all(_SO_TRACKING_TABLE, _SO_TRACKING_TOKEN,
                                        fields=["Order #", "Base Order #"],
                                        base_id=_SO_TRACKING_BASE)
            has_splits = any(
                r.get("fields", {}).get("Base Order #") == so_number
                for r in tracking_recs
            )
            if has_splits:
                ss_items = _ss_items_for_order(so_number)
                if ss_items:
                    for li in line_items:
                        if li["sku"] in ss_items:
                            li["qty"] = ss_items[li["sku"]]

    return line_items, None


@app.route("/api/portal/admin/so-line-items/<record_id>", methods=["GET", "OPTIONS"])
@portal_login_required
def portal_admin_so_line_items(user, record_id):
    """Fetch line items for a Sales Order (for invoice item selection). Admin only.
    Optional ?split=SO-0337-1 to pre-check/qty from ShipStation split order."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "GET"})
    c = cors()
    if not portal_can(user, "manage_team"):
        return Response(json.dumps({"error": "Insufficient permissions"}), status=403, headers=c, mimetype="application/json")
    try:
        split_order_number = request.args.get("split") or None
        line_items, err = _fetch_so_line_items(record_id, split_order_number)
        if err:
            return Response(json.dumps({"error": err}), status=404, headers=c, mimetype="application/json")
        return Response(json.dumps({"lineItems": line_items}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/admin/convert-to-invoice/<record_id>", methods=["POST", "OPTIONS"])
@portal_login_required
def portal_admin_convert_to_invoice(user, record_id):
    """Convert an approved SO into an Invoice record and send invoice email. Admin only.
    Optional body: {"lineItems": [{"lineItemId": "...", "qty": N}]} to invoice a subset/adjusted qtys.
    If omitted, all line items are copied at their original quantities."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "POST"})
    c = cors()
    if not portal_can(user, "manage_team"):
        return Response(json.dumps({"error": "Insufficient permissions"}), status=403, headers=c, mimetype="application/json")
    try:
        # Parse optional body: selected line items + split suffix
        selected_items = None  # None = use all
        split_suffix   = ""
        try:
            body = request.get_json(silent=True) or {}
            if "lineItems" in body:
                selected_items = {item["lineItemId"]: int(item["qty"]) for item in body["lineItems"] if int(item.get("qty", 0)) > 0}
            split_suffix = str(body.get("splitSuffix") or "").strip()
        except Exception:
            pass

        write_token = RETURNS_WRITE_TOKEN
        read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or write_token

        # Fetch the SO record
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=15,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": f"SO not found (AT {r.status_code})"}),
                            status=404, headers=c, mimetype="application/json")
        so_fields = r.json().get("fields", {})

        if so_fields.get("Order Type") != "Sales Order":
            return Response(json.dumps({"error": "Not a Sales Order"}), status=400, headers=c, mimetype="application/json")

        base_order_id = str(so_fields.get("Order ID", "")).strip()
        # For split invoices use order_id like "0337-1" so Document ID becomes "IN-0337-1"
        order_id_str  = f"{base_order_id}-{split_suffix}" if split_suffix else base_order_id
        so_number     = so_fields.get("Document ID", f"SO-{base_order_id}")
        customer_ids  = so_fields.get("Customer", [])
        po_number     = so_fields.get("Purchase Order #", "")

        # Check not already invoiced (exact order_id match handles splits separately)
        existing_inv = at_get_all(
            MANUAL_ORDERS_TABLE_ID, read_token,
            fields=["Document ID"],
            formula=f'AND({{Order Type}}="Invoice",{{Order ID}}="{order_id_str}")',
        )
        if existing_inv:
            inv_num = existing_inv[0].get("fields", {}).get("Document ID", "")
            return Response(json.dumps({"error": f"Already invoiced as {inv_num}"}),
                            status=400, headers=c, mimetype="application/json")

        # Get tracking and ship date (use split order number for splits)
        tracking_order_num = f"{so_number}-{split_suffix}" if split_suffix else so_number
        tracking_recs = at_get_all(
            _SO_TRACKING_TABLE, _SO_TRACKING_TOKEN,
            fields=["Order #", "Tracking #", "Ship Date"],
            base_id=_SO_TRACKING_BASE,
        )
        tracking  = ""
        ship_date = ""
        for tr in tracking_recs:
            if tr.get("fields", {}).get("Order #", "").strip() == tracking_order_num:
                tracking  = tr["fields"].get("Tracking #") or ""
                ship_date = tr["fields"].get("Ship Date") or ""
                break

        # Fetch line items — use selected subset if provided, else all
        li_ids = so_fields.get("MO Line Items", [])
        if selected_items is not None:
            li_ids = [lid for lid in li_ids if lid in selected_items]

        email_line_items = []
        for li_id in li_ids:
            li_r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                headers=at_headers(read_token), timeout=10,
            )
            if li_r.status_code != 200:
                print(f"[convert-to-invoice] could not fetch line item {li_id}: {li_r.status_code}")
                continue
            lf = li_r.json().get("fields", {})
            product_name_list = lf.get("Product Name (from Product SKU)", [])
            product_name = product_name_list[0] if product_name_list else ""
            # Use caller-specified qty if provided, else original
            qty = selected_items[li_id] if (selected_items is not None and li_id in selected_items) else lf.get("Qty.", 0)
            adj_price       = lf.get("Confirmed Adj. Unit Price")
            confirmed_price = lf.get("Confirmed Unit Price")
            adj_list        = lf.get("Adj. Unit Price (from MO Line Items)", [])
            unit_price      = float(adj_price if adj_price is not None else (confirmed_price if confirmed_price is not None else (float(adj_list[0]) if adj_list else 0)))
            li_total        = float(lf.get("Confirmed Line Item Total") or (qty * unit_price))
            email_line_items.append({"name": product_name, "qty": qty, "unit_price": unit_price,
                                     "line_total": li_total, "_li_fields": lf})

        # Create Invoice record — carry snapshot billing/shipping fields from SO
        inv_fields = {
            "Order Type":      "Invoice",
            "Order ID":        order_id_str,
            "Date":            _today_utc().isoformat(),
            "Invoice Status":  "Approved",
            # Note: "Ready for ShipStation (SOs)", "MO Is Approved" are computed — do not write.
            # Do NOT set "Sales Order Status" on invoices — that's an SO-only field.
            # "Invoice Status" = "Approved" is what drives MO Is Approved for invoice records.
        }
        # Copy snapshot fields so invoice shows correct billing/shipping regardless of
        # future customer record changes
        for _sf in ("Snapshot Org", "Snapshot Contact", "Snapshot Email",
                    "Snapshot Phone", "Snapshot Addr 1", "Snapshot Addr 2"):
            if so_fields.get(_sf):
                inv_fields[_sf] = so_fields[_sf]
        inv_body = {"fields": inv_fields}
        if customer_ids:
            inv_body["fields"]["Customer"] = customer_ids
        if po_number:
            inv_body["fields"]["Purchase Order #"] = po_number

        inv_r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json=inv_body,
            timeout=15,
        )
        if not inv_r.ok:
            try:
                at_err = inv_r.json()
            except Exception:
                at_err = inv_r.text[:500]
            return Response(json.dumps({"error": f"Invoice create failed ({inv_r.status_code}): {at_err}"}),
                            status=500, headers=c, mimetype="application/json")
        inv_record = inv_r.json()
        inv_record_id = inv_record["id"]

        # Copy (selected) line items from SO to Invoice with (possibly adjusted) qty
        for item in email_line_items:
            lf = item.pop("_li_fields")
            confirmed_price = lf.get("Confirmed Unit Price")
            adj_list = lf.get("Adj. Unit Price (from MO Line Items)", [])
            price = confirmed_price if confirmed_price is not None else (float(adj_list[0]) if adj_list else 0)
            new_li = req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": {
                    "Manual Order":              [inv_record_id],
                    "Product SKU":               lf.get("Product SKU", []),
                    "Qty.":                      item["qty"],
                    "Confirmed Unit Price":      float(price),
                    "Confirmed Adj. Unit Price": float(price),
                }},
                timeout=15,
            )
            if not new_li.ok:
                print(f"[convert-to-invoice] line item copy failed {new_li.status_code}: {new_li.text[:200]}")

        # Fetch the created invoice to get its Document ID (formula field)
        inv_fetch = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{inv_record_id}",
            headers=at_headers(read_token), timeout=15,
        )
        inv_number = f"IN-{order_id_str}"
        if inv_fetch.ok:
            inv_number = inv_fetch.json().get("fields", {}).get("Document ID", inv_number)

        # Get billing info from SO fields
        email_list = so_fields.get("Bill-To Contact Email (from Customer)", [])
        name_list  = so_fields.get("Bill-To Contact Name (from Customer)", [])
        org_list   = so_fields.get("Bill-To Org Name (from Customer)", [])
        to_email   = email_list[0] if email_list else ""
        to_name    = name_list[0] if name_list else ""
        org_name   = org_list[0] if org_list else ""

        total = round(sum(li.get("line_total", li["qty"] * float(li.get("unit_price") or 0)) for li in email_line_items), 2)

        if to_email:
            try:
                def _fso(lst):
                    return lst[0] if isinstance(lst, list) and lst else (lst or "")
                ship_city  = _fso(so_fields.get("Customer City (from Customer)", []))
                ship_state = _fso(so_fields.get("Customer State (from Customer)", []))
                ship_zip   = _fso(so_fields.get("Customer Zip Code (from Customer)", []))
                ship_csz   = ", ".join(filter(None, [ship_city, f"{ship_state} {ship_zip}".strip()]))
                inv_dict = {
                    "invNumber":    inv_number,
                    "soNumber":     so_number,
                    "date":         so_fields.get("Date", ""),
                    "poNumber":     so_fields.get("Purchase Order #", ""),
                    "orgName":      org_name,
                    "contact":      to_name,
                    "addr1":        _fso(so_fields.get("Bill-To Address (Line 1) (from Customer)", [])),
                    "addr2":        _fso(so_fields.get("Bill-To Address (Line 2) (from Customer)", [])),
                    "shipOrg":      _fso(so_fields.get("Organization Name (from Customer)", [])),
                    "shipName":     _fso(so_fields.get("Main Contact Name (from Customer)", [])),
                    "shipAddr1":    _fso(so_fields.get("Customer Address (Line 1) (from Customer)", [])),
                    "shipAddr2":    _fso(so_fields.get("Customer Address (Line 2) (from Customer)", [])) or ship_csz,
                    "tracking":     tracking,
                    "shipDate":     ship_date,
                    "lineItems":    email_line_items,
                    "subtotal":     total,
                    "stripeCcUrl":  "",
                    "stripeAchUrl": "",
                }
                inv_pdf_bytes = _build_invoice_pdf_bytes(inv_dict)
                send_invoice_email(to_email, to_name, org_name, so_number, inv_number,
                                   email_line_items, total, tracking, ship_date=ship_date,
                                   pdf_bytes=inv_pdf_bytes)
            except Exception as email_err:
                print(f"[convert-to-invoice] email failed: {email_err}")

        # Create Stripe CC + ACH invoices inline (Airtable automation replaced by this)
        if bill_email:
            try:
                _create_stripe_invoices_for_record(
                    write_token, inv_record_id, bill_email, bill_name, org_name, li_items_for_email
                )
            except Exception as stripe_err:
                print(f"[convert-to-invoice] Stripe invoice creation failed for {inv_number}: {stripe_err}")
                # Non-fatal: invoice record exists, Stripe links can be created manually

        # Clear orders cache
        _ORDERS_CACHE.clear()
        _INVOICES_CACHE.clear()

        return Response(json.dumps({"success": True, "invNumber": inv_number, "invRecordId": inv_record_id}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/invoices")
@portal_login_required
def portal_invoices(user):
    """Return all invoices for the authenticated portal customer."""
    c = cors()
    customer_id = user.get("customer_id", "")
    if not customer_id:
        return Response(json.dumps({"invoices": []}), headers=c, mimetype="application/json")

    import time as _time_mod

    # Return cached result if fresh
    _cached = _INVOICES_CACHE.get(customer_id)
    if _cached and (_time_mod.time() - _cached["ts"]) < _INVOICES_CACHE_TTL:
        return Response(json.dumps({"invoices": _cached["data"]}), headers=c, mimetype="application/json")

    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        # Fetch all invoices and filter in Python (linked field formula trick)
        inv_records = at_get_all(
            MANUAL_ORDERS_TABLE_ID, read_token,
            fields=["Document ID", "Order ID", "Date", "MO Line Items",
                    "Sales Order Status", "Go-to PDF", "Customer",
                    "Stripe Invoice Status (CC)", "Stripe Invoice Status (ACH)",
                    "Invoice Paid", "Hidden from Customer", "Tracking", "Tracking #", "Purchase Order #"],
            formula='{Order Type}="Invoice"',
        )
        # Filter to this customer, excluding hidden records
        inv_records = [r for r in inv_records
                       if customer_id in r.get("fields", {}).get("Customer", [])
                       and not r.get("fields", {}).get("Hidden from Customer", False)]

        # Batch-fetch all line items to calculate totals (Adj. Unit Price is a chained lookup — doesn't work)
        all_li_ids = []
        for rec in inv_records:
            all_li_ids.extend(rec.get("fields", {}).get("MO Line Items", []))
        li_total_map = {}  # line_item_id → Confirmed Line Item Total
        if all_li_ids:
            for i in range(0, len(all_li_ids), 100):
                batch = all_li_ids[i:i+100]
                formula = "OR(" + ",".join(f'RECORD_ID()="{lid}"' for lid in batch) + ")"
                li_recs = at_get_all(MO_LINE_ITEMS_TABLE_ID, read_token,
                                     fields=["Confirmed Line Item Total"],
                                     formula=formula)
                for lr in li_recs:
                    li_total_map[lr["id"]] = float(lr.get("fields", {}).get("Confirmed Line Item Total") or 0)

        # Fetch tracking from SO Tracking Link (keyed by SO-{order_id})
        tracking_recs = at_get_all(
            _SO_TRACKING_TABLE, _SO_TRACKING_TOKEN,
            fields=["Order #", "Tracking #"],
            base_id=_SO_TRACKING_BASE,
        )
        tracking_map = {r["fields"].get("Order #", "").strip(): (r["fields"].get("Tracking #") or "")
                        for r in tracking_recs if r.get("fields", {}).get("Order #")}

        invoices = []
        for rec in inv_records:
            f = rec.get("fields", {})
            order_id   = str(f.get("Order ID", "")).strip()
            so_number  = f"SO-{order_id}" if order_id else ""
            inv_number = f.get("Document ID", f"IN-{order_id}")
            li_ids     = f.get("MO Line Items", [])
            total      = round(sum(li_total_map.get(lid, 0) for lid in li_ids), 2)
            tracking   = tracking_map.get(so_number, "") or f.get("Tracking #", "") or f.get("Tracking", "")
            go_to_pdf_field = f.get("Go-to PDF") or {}
            go_to_pdf_url   = go_to_pdf_field.get("url", "") if isinstance(go_to_pdf_field, dict) else ""
            cc_status  = f.get("Stripe Invoice Status (CC)", "")
            ach_status = f.get("Stripe Invoice Status (ACH)", "")
            is_paid    = bool(f.get("Invoice Paid")) or cc_status == "paid" or ach_status == "paid"
            invoices.append({
                "record_id":  rec["id"],
                "inv_number": inv_number,
                "so_number":  so_number,
                "date":       f.get("Date", ""),
                "total":      total,
                "tracking":   tracking,
                "go_to_pdf":  go_to_pdf_url,
                "is_paid":    is_paid,
                "po_number":  f.get("Purchase Order #", ""),
            })

        invoices.sort(key=lambda x: x.get("date", ""), reverse=True)
        _INVOICES_CACHE[customer_id] = {"ts": _time_mod.time(), "data": invoices}
        return Response(json.dumps({"invoices": invoices}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


threading.Thread(target=_ontime_bg_worker, daemon=True).start()

# ── Invoice detail (admin read-only preview) ──────────────────────────────────

@app.route("/api/invoice-detail/<record_id>", methods=["GET", "OPTIONS"])
def invoice_detail(record_id):
    """Return full invoice details for the admin read-only preview page."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type"})
    c = cors()
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        # Fetch invoice record
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=15,
        )
        if not r.ok:
            return Response(json.dumps({"error": "Invoice not found"}), status=404, headers=c, mimetype="application/json")
        fields = r.json().get("fields", {})
        if fields.get("Order Type") != "Invoice":
            return Response(json.dumps({"error": "Not an invoice"}), status=400, headers=c, mimetype="application/json")

        def _first(lst):
            return lst[0] if isinstance(lst, list) and lst else (lst or "")

        order_id    = str(fields.get("Order ID", "")).strip()
        inv_number  = fields.get("Document ID", f"IN-{order_id}")
        so_number   = f"SO-{order_id}" if order_id else ""
        date_str    = fields.get("Date", "")
        po_number   = fields.get("Purchase Order #", "")
        org_name    = _first(fields.get("Bill-To Org Name (from Customer)", []))
        contact     = _first(fields.get("Bill-To Contact Name (from Customer)", []))
        email       = _first(fields.get("Bill-To Contact Email (from Customer)", []))
        addr1       = _first(fields.get("Bill-To Address (Line 1) (from Customer)", []))
        addr2       = _first(fields.get("Bill-To Address (Line 2) (from Customer)", []))
        ship_city   = _first(fields.get("Customer City (from Customer)", []))
        ship_state  = _first(fields.get("Customer State (from Customer)", []))
        ship_zip    = _first(fields.get("Customer Zip Code (from Customer)", []))
        ship_csz    = ", ".join(filter(None, [ship_city, f"{ship_state} {ship_zip}".strip()]))
        ship_org    = _first(fields.get("Organization Name (from Customer)", []))
        ship_name   = _first(fields.get("Main Contact Name (from Customer)", []))
        ship_addr1  = _first(fields.get("Customer Address (Line 1) (from Customer)", []))
        ship_addr2  = _first(fields.get("Customer Address (Line 2) (from Customer)", [])) or ship_csz

        # Get tracking + ship date and line items in parallel
        tracking  = ""
        ship_date = ""
        li_ids = fields.get("MO Line Items", [])
        import concurrent.futures as _cf_inv
        def _fetch_inv_li(li_id):
            try:
                lr = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                    headers=at_headers(read_token), timeout=10,
                )
                if lr.status_code == 200:
                    return li_id, lr.json()
            except Exception:
                pass
            return li_id, None
        with _cf_inv.ThreadPoolExecutor(max_workers=11) as _ex_inv:
            _fut_tracking_inv = _ex_inv.submit(at_get_all,
                _SO_TRACKING_TABLE, _SO_TRACKING_TOKEN,
                fields=["Order #", "Tracking #", "Ship Date"],
                base_id=_SO_TRACKING_BASE,
            )
            _li_results = list(_ex_inv.map(_fetch_inv_li, li_ids))
        tracking_recs = _fut_tracking_inv.result()
        for tr in tracking_recs:
            if tr.get("fields", {}).get("Order #", "").strip() == so_number:
                tracking  = tr["fields"].get("Tracking #", "")
                ship_date = tr["fields"].get("Ship Date", "")
                break

        line_items = []
        for li_id, li_data in _li_results:
            if not li_data:
                continue
            lf = li_data.get("fields", {})
            pname = _first(lf.get("Product Name (from Product SKU)", [])) or _first(lf.get("Name + Variations (from Product SKU)", [])) or "Item"
            qty        = lf.get("Qty.", 0)
            adj_price  = lf.get("Confirmed Adj. Unit Price")
            conf_price = lf.get("Confirmed Unit Price")
            adj_list   = lf.get("Adj. Unit Price (from MO Line Items)", [])
            unit_price = float(adj_price if adj_price is not None else (conf_price if conf_price is not None else (_first(adj_list) or 0)))
            li_total   = float(lf.get("Confirmed Line Item Total") or (qty * unit_price))
            line_items.append({
                "name":       pname,
                "qty":        qty,
                "unit_price": unit_price,
                "total":      li_total,
            })

        subtotal = round(sum(li["total"] for li in line_items), 2)

        # Fetch tax rate from linked customer record
        tax_amount = 0.0
        try:
            cust_ids = fields.get("Customer", [])
            if cust_ids:
                cr = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{cust_ids[0]}",
                    headers=at_headers(read_token), timeout=10,
                )
                if cr.ok:
                    tax_rate = float(cr.json().get("fields", {}).get("Tax Rate") or 0)
                    tax_amount = round(subtotal * tax_rate, 2) if tax_rate else 0.0
        except Exception:
            pass

        stripe_cc_url  = fields.get("Stripe Invoice URL (CC)", "")
        stripe_ach_url = fields.get("Stripe Invoice URL (ACH)", "")
        stripe_cc_status  = fields.get("Stripe Invoice Status (CC)", "")
        stripe_ach_status = fields.get("Stripe Invoice Status (ACH)", "")

        return Response(json.dumps({
            "invNumber":       inv_number,
            "soNumber":        so_number,
            "date":            date_str,
            "poNumber":        po_number,
            "orgName":         org_name,
            "contact":         contact,
            "email":           email,
            "addr1":           addr1,
            "addr2":           addr2,
            "shipOrg":         ship_org,
            "shipName":        ship_name,
            "shipAddr1":       ship_addr1,
            "shipAddr2":       ship_addr2,
            "tracking":        tracking,
            "shipDate":        ship_date,
            "lineItems":       line_items,
            "subtotal":        subtotal,
            "taxAmount":       tax_amount,
            "total":           round(subtotal + tax_amount, 2),
            "stripeCcUrl":     stripe_cc_url,
            "stripeAchUrl":    stripe_ach_url,
            "stripeCcStatus":  stripe_cc_status,
            "stripeAchStatus": stripe_ach_status,
        }), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


# ── Admin portal: Shipped Orders & Invoice Conversion ────────────────────────

@app.route("/api/admin/shipped-orders", methods=["GET"])
def admin_shipped_orders():
    """Return all shipped (tracked) approved SOs for the admin portal."""
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        records = at_get_all(
            MANUAL_ORDERS_TABLE_ID, read_token,
            fields=["Document ID", "Order ID", "Date", "Bill-To Org Name (from Customer)", "MO Line Items"],
            formula='AND({Order Type}="Sales Order",{Sales Order Status}="Approved",IS_AFTER({Date},"2026-04-30"))',
        )
        # Existing invoices: order_id → inv_number + record_id
        inv_recs = at_get_all(MANUAL_ORDERS_TABLE_ID, read_token,
                               fields=["Order ID", "Document ID"],
                               formula='{Order Type}="Invoice"')
        invoiced         = {r["fields"].get("Order ID", ""): r["fields"].get("Document ID", "")
                            for r in inv_recs if r.get("fields", {}).get("Order ID")}
        invoiced_rec_ids = {r["fields"].get("Order ID", ""): r["id"]
                            for r in inv_recs if r.get("fields", {}).get("Order ID")}
        # Tracking + ship date map: SO number → tracking #, ship date, base order #
        tracking_recs = at_get_all(_SO_TRACKING_TABLE, _SO_TRACKING_TOKEN,
                                    fields=["Order #", "Tracking #", "Ship Date", "Base Order #", "Order Total"],
                                    base_id=_SO_TRACKING_BASE)
        tracking_map    = {}
        ship_date_map   = {}
        base_order_map  = {}  # split order # → parent order #
        order_total_map = {}
        for r in tracking_recs:
            f2 = r.get("fields", {})
            key = f2.get("Order #", "").strip()
            if key:
                tracking_map[key]    = f2.get("Tracking #") or ""
                ship_date_map[key]   = f2.get("Ship Date") or ""
                base_order_map[key]  = f2.get("Base Order #") or ""
                order_total_map[key] = f2.get("Order Total")

        # Build SO record lookup: document_id → record
        so_by_doc = {rec.get("fields", {}).get("Document ID", ""): rec for rec in records}

        # Batch-fetch line item totals
        all_li_ids = []
        for rec in records:
            all_li_ids.extend(rec.get("fields", {}).get("MO Line Items", []))
        li_total_map = {}
        if all_li_ids:
            for chunk in [all_li_ids[i:i+30] for i in range(0, len(all_li_ids), 30)]:
                formula_li = "OR(" + ",".join(f'RECORD_ID()="{lid}"' for lid in chunk) + ")"
                li_recs = at_get_all(MO_LINE_ITEMS_TABLE_ID, read_token,
                                     fields=["Confirmed Line Item Total"], formula=formula_li)
                for lr in li_recs:
                    li_total_map[lr["id"]] = float(lr.get("fields", {}).get("Confirmed Line Item Total") or 0)

        orders = []

        # Main orders
        for rec in records:
            f = rec.get("fields", {})
            so_number = f.get("Document ID", f'SO-{f.get("Order ID","")}')
            tracking  = tracking_map.get(so_number, "")
            ship_date = ship_date_map.get(so_number, "")
            if not tracking and not ship_date:
                continue
            order_id = f.get("Order ID", "")
            org_list = f.get("Bill-To Org Name (from Customer)", [])
            org_name = org_list[0] if isinstance(org_list, list) and org_list else (org_list or "")
            at_total = round(sum(li_total_map.get(lid, 0) for lid in f.get("MO Line Items", [])), 2)
            ss_total = order_total_map.get(so_number)
            total    = round(float(ss_total), 2) if ss_total else at_total
            orders.append({
                "record_id":     rec["id"],
                "so_number":     so_number,
                "order_id":      order_id,
                "date":          f.get("Date", ""),
                "ship_date":     ship_date_map.get(so_number, ""),
                "org_name":      org_name,
                "total":         total,
                "tracking":      tracking,
                "invoiced":      order_id in invoiced,
                "inv_number":    invoiced.get(order_id),
                "inv_record_id": invoiced_rec_ids.get(order_id, ""),
                "is_split":      False,
                "split_suffix":  "",
            })

        # Split orders (Base Order # is set in tracking table)
        for order_num, base_order_num in base_order_map.items():
            if not base_order_num:
                continue
            tracking  = tracking_map.get(order_num, "")
            ship_date = ship_date_map.get(order_num, "")
            if not tracking and not ship_date:
                continue
            parent_rec = so_by_doc.get(base_order_num)
            if not parent_rec:
                continue
            pf = parent_rec.get("fields", {})
            order_id_base = pf.get("Order ID", "")
            # Split suffix: the part after the last "-" in the split order number beyond base
            # e.g. "SO-0337-1" → suffix "1"
            split_suffix = order_num[len(base_order_num):].lstrip("-")
            split_order_id = f"{order_id_base}-{split_suffix}" if split_suffix else order_id_base
            org_list = pf.get("Bill-To Org Name (from Customer)", [])
            org_name = org_list[0] if isinstance(org_list, list) and org_list else (org_list or "")
            orders.append({
                "record_id":     parent_rec["id"],  # parent Airtable record for customer/PO info
                "so_number":     order_num,
                "order_id":      split_order_id,
                "date":          pf.get("Date", ""),
                "ship_date":     ship_date_map.get(order_num, ""),
                "org_name":      org_name,
                "total":         round(float(order_total_map.get(order_num) or 0), 2),
                "tracking":      tracking,
                "invoiced":      split_order_id in invoiced,
                "inv_number":    invoiced.get(split_order_id),
                "inv_record_id": invoiced_rec_ids.get(split_order_id, ""),
                "is_split":      True,
                "split_suffix":  split_suffix,
            })

        orders.sort(key=lambda x: (x.get("date", ""), x.get("so_number", "")), reverse=True)
        return Response(json.dumps({"orders": orders}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/so-line-items/<record_id>", methods=["GET", "OPTIONS"])
def admin_so_line_items(record_id):
    """Fetch line items for a Sales Order to populate the invoice item-selection modal.
    Optional ?split=SO-0337-1 to pre-check/qty from ShipStation split order."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "GET"})
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    try:
        split_order_number = request.args.get("split") or None
        line_items, err = _fetch_so_line_items(record_id, split_order_number)
        if err:
            return Response(json.dumps({"error": err}), status=404, headers=c, mimetype="application/json")
        return Response(json.dumps({"lineItems": line_items}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/convert-to-invoice/<record_id>", methods=["POST", "OPTIONS"])
def admin_convert_to_invoice(record_id):
    """Convert an approved SO to an Invoice (admin portal).
    Optional body: {"lineItems": [{"lineItemId": "...", "qty": N}]} to invoice a subset/adjusted qtys."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "POST"})
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    try:
        # Parse optional selected line items + split suffix
        selected_items = None
        split_suffix   = ""
        shipping_cost  = 0.0
        try:
            body = request.get_json(silent=True) or {}
            if "lineItems" in body:
                selected_items = {item["lineItemId"]: int(item["qty"]) for item in body["lineItems"] if int(item.get("qty", 0)) > 0}
            split_suffix  = str(body.get("splitSuffix") or "").strip()
            shipping_cost = float(body.get("shippingCost") or 0)
        except Exception:
            pass

        read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        write_token = RETURNS_WRITE_TOKEN

        # Fetch SO record
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=15,
        )
        if not r.ok:
            return Response(json.dumps({"error": "SO not found"}), status=404, headers=c, mimetype="application/json")
        so_fields = r.json().get("fields", {})
        if so_fields.get("Order Type") != "Sales Order":
            return Response(json.dumps({"error": "Not a Sales Order"}), status=400, headers=c, mimetype="application/json")

        base_order_id = str(so_fields.get("Order ID", "")).strip()
        order_id_str  = f"{base_order_id}-{split_suffix}" if split_suffix else base_order_id
        so_number     = so_fields.get("Document ID", f"SO-{base_order_id}")
        # For splits, tracking is under the split order number (e.g. SO-0337-1)
        tracking_order_num = f"{so_number}-{split_suffix}" if split_suffix else so_number

        # Check not already invoiced
        existing = at_get_all(MANUAL_ORDERS_TABLE_ID, read_token,
                               fields=["Order ID"],
                               formula=f'AND({{Order Type}}="Invoice",{{Order ID}}="{order_id_str}")')
        if existing:
            inv_doc = existing[0]["fields"].get("Document ID", f"IN-{order_id_str}")
            return Response(json.dumps({"error": f"Already invoiced as {inv_doc}"}), status=400, headers=c, mimetype="application/json")

        # Get tracking and ship date (use split order number for splits)
        tracking_recs = at_get_all(_SO_TRACKING_TABLE, _SO_TRACKING_TOKEN,
                                    fields=["Order #", "Tracking #", "Ship Date"], base_id=_SO_TRACKING_BASE)
        tracking  = ""
        ship_date = ""
        for _tr in tracking_recs:
            if _tr.get("fields", {}).get("Order #", "").strip() == tracking_order_num:
                tracking  = _tr["fields"].get("Tracking #", "")
                ship_date = _tr["fields"].get("Ship Date", "")
                break

        # Get billing info from SO
        def _first(lst):
            return lst[0] if isinstance(lst, list) and lst else (lst or "")
        bill_email = _first(so_fields.get("Bill-To Contact Email (from Customer)", []))
        bill_name  = _first(so_fields.get("Bill-To Contact Name (from Customer)", []))
        org_name   = _first(so_fields.get("Bill-To Org Name (from Customer)", []))
        customer_ids = so_fields.get("Customer", [])
        po_number    = so_fields.get("Purchase Order #", "")

        # Determine which line items to copy
        all_li_ids = so_fields.get("MO Line Items", [])
        li_ids_to_copy = [lid for lid in all_li_ids if selected_items is None or lid in selected_items]

        # Create Invoice record — carry snapshot billing/shipping fields from SO
        inv_fields = {
            "Order Type":     "Invoice",
            "Order ID":       order_id_str,
            "Date":           _today_utc().isoformat(),
            "Invoice Status": "Approved",
            # Note: "Ready for ShipStation (SOs)", "MO Is Approved" are computed — do not write.
            # Do NOT set "Sales Order Status" on invoices — that's an SO-only field.
            # "Invoice Status" = "Approved" is what drives MO Is Approved for invoice records.
        }
        for _sf in ("Snapshot Org", "Snapshot Contact", "Snapshot Email",
                    "Snapshot Phone", "Snapshot Addr 1", "Snapshot Addr 2"):
            if so_fields.get(_sf):
                inv_fields[_sf] = so_fields[_sf]
        inv_body = {"fields": inv_fields}
        if customer_ids:
            inv_body["fields"]["Customer"] = customer_ids
        if po_number:
            inv_body["fields"]["Purchase Order #"] = po_number
        inv_r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json=inv_body, timeout=15,
        )
        if not inv_r.ok:
            return Response(json.dumps({"error": f"Invoice create failed: {inv_r.text[:200]}"}),
                            status=500, headers=c, mimetype="application/json")
        inv_record_id = inv_r.json()["id"]
        # Fetch computed Document ID (formula field)
        inv_fetch = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{inv_record_id}",
            headers=at_headers(read_token), timeout=10,
        )
        inv_number = inv_fetch.json().get("fields", {}).get("Document ID", f"IN-{order_id_str}") if inv_fetch.ok else f"IN-{order_id_str}"

        # Copy (selected) line items with (possibly adjusted) quantities
        li_items_for_email = []
        for li_id in li_ids_to_copy:
            li_r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                headers=at_headers(read_token), timeout=10,
            )
            if not li_r.ok:
                continue
            lf = li_r.json().get("fields", {})
            qty        = selected_items[li_id] if (selected_items is not None and li_id in selected_items) else lf.get("Qty.", 0)
            adj_price  = lf.get("Confirmed Adj. Unit Price")
            conf_price = lf.get("Confirmed Unit Price")
            adj_list_v = lf.get("Adj. Unit Price (from MO Line Items)", [])
            unit_price = float(adj_price if adj_price is not None else (conf_price if conf_price is not None else (float(adj_list_v[0]) if adj_list_v else 0)))
            li_total   = float(lf.get("Confirmed Line Item Total") or (qty * unit_price))
            pname_list = lf.get("Product Name (from Product SKU)", [])
            pname = _first(pname_list) if pname_list else "Item"
            req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": {
                    "Manual Order":              [inv_record_id],
                    "Product SKU":               lf.get("Product SKU", []),
                    "Qty.":                      qty,
                    "Confirmed Unit Price":      unit_price,
                    "Confirmed Adj. Unit Price": unit_price,
                }}, timeout=15,
            )
            li_items_for_email.append({"name": pname, "qty": qty, "unit_price": unit_price,
                                       "line_total": li_total})

        # Add shipping line item if specified (links to "Shipping Fee" SKU record)
        _SHIPPING_SKU_RECORD_ID = "recvxAfBvDgM1HEpw"
        if shipping_cost and shipping_cost > 0:
            req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": {
                    "Manual Order":              [inv_record_id],
                    "Product SKU":               [_SHIPPING_SKU_RECORD_ID],
                    "Qty.":                      1,
                    "Confirmed Unit Price":      round(shipping_cost, 2),
                    "Confirmed Adj. Unit Price": round(shipping_cost, 2),
                }}, timeout=15,
            )
            li_items_for_email.append({"name": "Shipping Fee", "qty": 1, "unit_price": round(shipping_cost, 2)})

        total = round(sum(i["qty"] * i["unit_price"] for i in li_items_for_email), 2)

        # Send invoice email with PDF attachment
        if bill_email:
            try:
                ship_city  = _first(so_fields.get("Customer City (from Customer)", []))
                ship_state = _first(so_fields.get("Customer State (from Customer)", []))
                ship_zip   = _first(so_fields.get("Customer Zip Code (from Customer)", []))
                ship_csz   = ", ".join(filter(None, [ship_city, f"{ship_state} {ship_zip}".strip()]))
                inv_dict = {
                    "invNumber":    inv_number,
                    "soNumber":     so_number,
                    "date":         so_fields.get("Date", ""),
                    "poNumber":     so_fields.get("Purchase Order #", ""),
                    "orgName":      org_name,
                    "contact":      bill_name,
                    "addr1":        _first(so_fields.get("Bill-To Address (Line 1) (from Customer)", [])),
                    "addr2":        _first(so_fields.get("Bill-To Address (Line 2) (from Customer)", [])),
                    "shipOrg":      _first(so_fields.get("Organization Name (from Customer)", [])),
                    "shipName":     _first(so_fields.get("Main Contact Name (from Customer)", [])),
                    "shipAddr1":    _first(so_fields.get("Customer Address (Line 1) (from Customer)", [])),
                    "shipAddr2":    _first(so_fields.get("Customer Address (Line 2) (from Customer)", [])) or ship_csz,
                    "tracking":     tracking,
                    "shipDate":     ship_date,
                    "lineItems":    li_items_for_email,
                    "subtotal":     total,
                    "stripeCcUrl":  "",
                    "stripeAchUrl": "",
                }
                inv_pdf_bytes = _build_invoice_pdf_bytes(inv_dict)
                send_invoice_email(bill_email, bill_name, org_name, so_number, inv_number,
                                   li_items_for_email, total, tracking, ship_date=ship_date,
                                   pdf_bytes=inv_pdf_bytes)
            except Exception as email_err:
                print(f"[convert-to-invoice] email failed: {email_err}")

        # Create Stripe CC + ACH invoices inline (Airtable automation replaced by this)
        if bill_email:
            try:
                _create_stripe_invoices_for_record(
                    write_token, inv_record_id, bill_email, bill_name, org_name, li_items_for_email
                )
            except Exception as stripe_err:
                print(f"[convert-to-invoice] Stripe invoice creation failed for {inv_number}: {stripe_err}")
                # Non-fatal: invoice record exists, Stripe links can be created manually

        _ORDERS_CACHE.clear()
        _INVOICES_CACHE.clear()
        return Response(json.dumps({"success": True, "invNumber": inv_number, "invRecordId": inv_record_id}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


# ── Stripe invoice creation ───────────────────────────────────────────────────

def _create_stripe_invoices_for_record(write_token, inv_record_id, billing_email, billing_name, org_name, li_items):
    """Create CC + ACH Stripe hosted invoices and patch back to the Airtable invoice record."""
    if not STRIPE_SECRET_KEY:
        raise Exception("STRIPE_SECRET_KEY not configured")
    if not billing_email:
        raise Exception("No billing email provided")

    ss_auth = (STRIPE_SECRET_KEY, "")

    # Stripe only accepts a single email — take the first if semicolon/comma-separated
    stripe_email = billing_email.replace(",", ";").split(";")[0].strip()

    # 1. Create Stripe customer
    cust_r = req_lib.post("https://api.stripe.com/v1/customers",
        data={"email": stripe_email, "name": org_name or billing_name or stripe_email,
              "description": billing_name or ""},
        auth=ss_auth, timeout=15)
    if not cust_r.ok:
        raise Exception(f"Stripe customer create failed: {cust_r.status_code} {cust_r.text[:300]}")
    customer_id = cust_r.json()["id"]
    print(f"[stripe] created customer {customer_id} for {billing_email}")

    cc_id = cc_url = ach_id = ach_url = due_date_str = ""

    for method in ["card", "us_bank_account"]:
        # 2. Create draft invoice first
        inv_data = {
            "customer":          customer_id,
            "collection_method": "send_invoice",
            "days_until_due":    "30",
            "payment_settings[payment_method_types][0]": method,
        }
        inv_r = req_lib.post("https://api.stripe.com/v1/invoices",
            data=inv_data, auth=ss_auth, timeout=15)
        if not inv_r.ok:
            raise Exception(f"Stripe invoice create failed ({method}): {inv_r.status_code} {inv_r.text[:300]}")
        stripe_inv_id = inv_r.json()["id"]
        print(f"[stripe] created draft invoice {stripe_inv_id} ({method})")

        # 3. Add line items directly to the draft invoice (avoids pending-item race condition)
        for item in li_items:
            # Prefer Confirmed Line Item Total (exact adjusted total); fall back to qty × unit_price
            if item.get("line_total") is not None:
                item_amount_cents = str(round(float(item["line_total"]) * 100))
                ii_data = {
                    "customer":    customer_id,
                    "invoice":     stripe_inv_id,
                    "amount":      item_amount_cents,
                    "currency":    "usd",
                    "description": item["name"],
                }
            else:
                item_amount_cents = str(round(float(item["unit_price"]) * 100, 4))
                ii_data = {
                    "customer":             customer_id,
                    "invoice":              stripe_inv_id,
                    "unit_amount_decimal":  item_amount_cents,
                    "quantity":             str(int(item["qty"])),
                    "currency":             "usd",
                    "description":          item["name"],
                }
            ii_r = req_lib.post("https://api.stripe.com/v1/invoiceitems",
                data=ii_data,
                auth=ss_auth, timeout=15)
            if not ii_r.ok:
                print(f"[stripe] invoice item warn ({method}): {ii_r.status_code} {ii_r.text[:200]}")

        # 4. Finalize → get hosted_invoice_url
        fin_r = req_lib.post(f"https://api.stripe.com/v1/invoices/{stripe_inv_id}/finalize",
            data={}, auth=ss_auth, timeout=15)
        if not fin_r.ok:
            raise Exception(f"Stripe finalize failed ({method}): {fin_r.status_code} {fin_r.text[:300]}")
        fin_inv    = fin_r.json()
        hosted_url = fin_inv.get("hosted_invoice_url", "")

        if not due_date_str and fin_inv.get("due_date"):
            import datetime as _dt_stripe
            due_date_str = _dt_stripe.datetime.utcfromtimestamp(fin_inv["due_date"]).strftime("%Y-%m-%d")

        if method == "card":
            cc_id, cc_url = stripe_inv_id, hosted_url
        else:
            ach_id, ach_url = stripe_inv_id, hosted_url
        print(f"[stripe] {method} invoice {stripe_inv_id} finalized → {hosted_url[:60]}...")

    # 5. Patch Airtable invoice record with all Stripe data
    patch_fields = {
        "Stripe Customer ID":          customer_id,
        "Stripe Invoice ID (CC)":      cc_id,
        "Stripe Invoice URL (CC)":     cc_url,
        "Stripe Invoice Status (CC)":  "Open",
        "Stripe Invoice ID (ACH)":     ach_id,
        "Stripe Invoice URL (ACH)":    ach_url,
        "Stripe Invoice Status (ACH)": "Open",
    }
    if due_date_str:
        patch_fields["Stripe Invoice Due Date"] = due_date_str

    patch_r = req_lib.patch(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{inv_record_id}",
        headers={**at_headers(write_token), "Content-Type": "application/json"},
        json={"fields": patch_fields}, timeout=15,
    )
    if patch_r.ok:
        print(f"[stripe] patched Airtable record {inv_record_id} with Stripe invoice URLs")
    else:
        raise Exception(f"Airtable patch failed: {patch_r.status_code} {patch_r.text[:300]}")


# ── Nightly tracking sync ─────────────────────────────────────────────────────

_SO_TRACKING_BASE  = "app3xt0dghBWnHxdN"
_SO_TRACKING_TABLE = "tblvgBZZvxwRPZpMx"   # SO Tracking Link (writable)
_SO_TRACKING_TOKEN = os.environ.get("SO_TRACKING_WRITE_TOKEN", "")


def _run_tracking_sync():
    """Fetch all approved SOs, pull tracking from ShipStation, upsert into Sales Order Tracking table."""
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        records = at_get_all(
            MANUAL_ORDERS_TABLE_ID, read_token,
            fields=["Document ID", "Order ID", "Date", "Sales Order Status"],
            formula='AND({Order Type}="Sales Order",{Sales Order Status}="Approved",IS_AFTER({Date},"2026-04-30"))',
        )
        print(f"[tracking-sync] syncing {len(records)} approved SOs")

        # Fetch existing tracking records indexed by Order #
        existing = at_get_all(_SO_TRACKING_TABLE, _SO_TRACKING_TOKEN,
                               fields=["Order #"],
                               base_id=_SO_TRACKING_BASE)
        existing_by_doc = {r["fields"].get("Order #", "").strip(): r["id"]
                           for r in existing if r.get("fields", {}).get("Order #")}

        dest_headers = {**at_headers(_SO_TRACKING_TOKEN), "Content-Type": "application/json"}
        ss_hdrs = ss_headers()
        synced = 0

        for rec in records:
            f = rec.get("fields", {})
            doc_id = (f.get("Document ID") or "").strip()
            if not doc_id:
                continue
            try:
                def _sync_one_order(order_num, so_date, base_order_num=None):
                    """Fetch shipment tracking for one ShipStation order number and upsert into SO Tracking table.
                    base_order_num is set for split orders (e.g. 'SO-0337-1' has base 'SO-0337').
                    Only writes to Airtable if tracking was found — never clears existing data."""
                    r2 = req_lib.get("https://ssapi.shipstation.com/shipments",
                                     params={"orderNumber": order_num, "pageSize": 50, "storeId": 212019},
                                     headers=ss_hdrs, timeout=15)
                    t_parts, sd_str = [], ""
                    order_total = None
                    if r2.ok:
                        for s in r2.json().get("shipments", []):
                            if s.get("voided"):
                                continue
                            # Exact order number match — ShipStation does prefix matching
                            if s.get("orderNumber", "").strip() != order_num:
                                continue
                            tn = (s.get("trackingNumber") or "").strip()
                            if tn and tn not in t_parts:
                                t_parts.append(tn)
                            sd = (s.get("shipDate") or "")[:10]
                            if sd and (not sd_str or sd < sd_str):
                                sd_str = sd
                    t_str = " | ".join(t_parts)

                    # Get order total and check pickup status from ShipStation orders API
                    try:
                        ro = req_lib.get("https://ssapi.shipstation.com/orders",
                                         params={"orderNumber": order_num, "pageSize": 5},
                                         headers=ss_hdrs, timeout=15)
                        if ro.ok:
                            ss_orders = [o for o in ro.json().get("orders", [])
                                         if o.get("orderNumber", "").strip() == order_num]
                            if ss_orders:
                                ot = ss_orders[0].get("orderTotal")
                                if ot is not None and float(ot) > 0:
                                    order_total = float(ot)
                                # Pickup: order is shipped but has no tracking from shipments
                                if not t_str and ss_orders[0].get("orderStatus") == "shipped":
                                    if not sd_str:
                                        sd_str = (ss_orders[0].get("shipDate") or "")[:10]
                                    if sd_str:
                                        print(f"[tracking-sync] {order_num}: no tracking, marked as pickup (ship date {sd_str})")
                    except Exception as _ot_err:
                        print(f"[tracking-sync] order fetch failed for {order_num}: {_ot_err}")

                    # Only write to Airtable if we have tracking OR a ship date (pickup orders)
                    if not t_str and not sd_str:
                        return ""

                    flds = {"Order #": order_num, "Date": so_date, "Tracking #": t_str}
                    if sd_str:
                        flds["Ship Date"] = sd_str
                    if base_order_num:
                        flds["Base Order #"] = base_order_num
                    if order_total is not None:
                        flds["Order Total"] = float(order_total)
                    if order_num in existing_by_doc:
                        req_lib.patch(
                            f"https://api.airtable.com/v0/{_SO_TRACKING_BASE}/{_SO_TRACKING_TABLE}/{existing_by_doc[order_num]}",
                            headers=dest_headers, json={"fields": flds}, timeout=15,
                        )
                    else:
                        new_r = req_lib.post(
                            f"https://api.airtable.com/v0/{_SO_TRACKING_BASE}/{_SO_TRACKING_TABLE}",
                            headers=dest_headers, json={"records": [{"fields": flds}]}, timeout=15,
                        )
                        # Register new record so subsequent splits don't duplicate it
                        if new_r.ok:
                            new_records = new_r.json().get("records", [])
                            if new_records:
                                existing_by_doc[order_num] = new_records[0]["id"]
                    return t_str

                # Sync the main order
                t = _sync_one_order(doc_id, f.get("Date", ""))
                if t:
                    print(f"[tracking-sync] {doc_id}: {t}")
                    synced += 1

                # Check for split orders (-1, -2, -3)
                for split_n in range(1, 4):
                    split_id = f"{doc_id}-{split_n}"
                    t_split = _sync_one_order(split_id, f.get("Date", ""), base_order_num=doc_id)
                    if t_split:
                        print(f"[tracking-sync] {split_id} (split): {t_split}")
                        synced += 1
                    else:
                        # No tracking (or no order) for this suffix — stop checking further
                        break

            except Exception as exc:
                print(f"[tracking-sync] error for {doc_id}: {exc}")

        _ORDERS_CACHE.clear()
        print(f"[tracking-sync] done — {synced} orders with tracking")
    except Exception as exc:
        print(f"[tracking-sync] run failed: {exc}")


def _tracking_sync_worker():
    """Daily at 6 PM ET — sync ShipStation tracking to Airtable for all approved SOs."""
    import time as _t
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt, timedelta
    _t.sleep(10)  # brief startup delay
    # On startup: if it's already past 6 PM ET, run immediately (catches missed deploys)
    try:
        _startup_et = _dt.now(ZoneInfo("America/New_York"))
        if _startup_et.hour >= 18:
            print(f"[tracking-sync] startup after 6 PM ET ({_startup_et.strftime('%H:%M')}), running sync now")
            _run_tracking_sync()
    except Exception as _se:
        print(f"[tracking-sync] startup check error: {_se}")
    while True:
        try:
            et_tz  = ZoneInfo("America/New_York")
            now_et = _dt.now(et_tz)
            next_run = now_et.replace(hour=18, minute=0, second=0, microsecond=0)
            if now_et >= next_run:
                next_run += timedelta(days=1)
            sleep_secs = (next_run - now_et).total_seconds()
            print(f"[tracking-sync] sleeping {sleep_secs/3600:.1f}h until {next_run.strftime('%Y-%m-%d %H:%M ET')}")
            _t.sleep(sleep_secs)
            _run_tracking_sync()
        except Exception as exc:
            print(f"[tracking-sync] worker error: {exc}")
            import time as _t2; _t2.sleep(300)

threading.Thread(target=_tracking_sync_worker, daemon=True).start()


@app.route("/api/admin/trigger-tracking-sync", methods=["POST"])
def admin_trigger_tracking_sync():
    """Trigger tracking sync from the admin portal (session cookie auth)."""
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    threading.Thread(target=_run_tracking_sync, daemon=True).start()
    return Response(json.dumps({"ok": True, "message": "Tracking sync started — ship dates will update in ~30 seconds"}), headers=c, mimetype="application/json")


@app.route("/api/admin/sync-tracking", methods=["POST"])
def admin_sync_tracking():
    """Manually trigger a tracking sync (admin only)."""
    c = cors()
    auth = request.headers.get("Authorization", "")
    admin_pw = os.environ.get("CS_ADMIN_PASSWORD", "")
    if not admin_pw or auth != f"Bearer {admin_pw}":
        return Response(json.dumps({"error": "unauthorized"}), status=401, headers=c, mimetype="application/json")
    threading.Thread(target=_run_tracking_sync, daemon=True).start()
    return Response(json.dumps({"ok": True, "message": "Tracking sync started"}), headers=c, mimetype="application/json")


@app.route("/api/admin/sync-tracking-debug", methods=["POST"])
def admin_sync_tracking_debug():
    """Run tracking sync for first 5 SOs synchronously and return detailed results."""
    c = cors()
    auth = request.headers.get("Authorization", "")
    admin_pw = os.environ.get("CS_ADMIN_PASSWORD", "")
    if not admin_pw or auth != f"Bearer {admin_pw}":
        return Response(json.dumps({"error": "unauthorized"}), status=401, headers=c, mimetype="application/json")
    try:
        write_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        results = {
            "write_token_set": bool(write_token),
            "ss_key_set": bool(SHIPSTATION_KEY),
            "orders": []
        }
        records = at_get_all(
            MANUAL_ORDERS_TABLE_ID, write_token,
            fields=["Document ID", "Order ID", "Sales Order Status"],
            formula='AND({Order Type}="Sales Order",{Sales Order Status}="Approved")',
        )
        for rec in records[:5]:
            f = rec.get("fields", {})
            doc_id = f.get("Document ID", "")
            entry = {"record_id": rec["id"], "doc_id": doc_id}
            # Query ShipStation
            ss_r = req_lib.get("https://ssapi.shipstation.com/shipments",
                                params={"orderNumber": doc_id, "pageSize": 10},
                                headers=ss_headers(), timeout=15)
            entry["ss_status"] = ss_r.status_code
            entry["ss_total"] = ss_r.json().get("total", 0) if ss_r.ok else "error"
            # Try write to Sales Order Tracking table
            dest_hdrs = {**at_headers(_SO_TRACKING_TOKEN), "Content-Type": "application/json"}
            patch_r = req_lib.post(
                f"https://api.airtable.com/v0/{_SO_TRACKING_BASE}/{_SO_TRACKING_TABLE}",
                headers=dest_hdrs,
                json={"records": [{"fields": {"Document ID": doc_id, "Tracking": "TEST"}}]},
                timeout=15,
            )
            entry["at_write_status"] = patch_r.status_code
            if not patch_r.ok:
                entry["at_write_error"] = patch_r.text[:200]
            results["orders"].append(entry)
        return Response(json.dumps(results, indent=2), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")

# ─────────────────────────────────────────────────────────────────────────────
# Admin: Invoices
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/admin/invoices", methods=["GET"])
def admin_invoices():
    """Return all Invoice-type Manual Orders for the admin portal."""
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        inv_records = at_get_all(
            MANUAL_ORDERS_TABLE_ID, read_token,
            fields=["Document ID", "Order ID", "Date", "Snapshot Org", "MO Line Items",
                    "Invoice Paid", "Check #", "Check Date", "Check Payment Amount",
                    "Stripe Invoice Status (CC)", "Stripe Invoice Status (ACH)",
                    "Stripe Invoice URL (CC)", "Stripe Invoice Due Date",
                    "Bill-To Contact Email (from Customer)", "Bill-To Contact Name (from Customer)",
                    "Bill-To Org Name (from Customer)", "Overdue Notice Dates"],
            formula='{Order Type}="Invoice"',
        )

        # Batch-fetch all line items to calculate totals
        all_li_ids = []
        for rec in inv_records:
            all_li_ids.extend(rec.get("fields", {}).get("MO Line Items", []))
        li_total_map = {}
        if all_li_ids:
            for i in range(0, len(all_li_ids), 100):
                batch = all_li_ids[i:i + 100]
                formula = "OR(" + ",".join(f'RECORD_ID()="{lid}"' for lid in batch) + ")"
                li_recs = at_get_all(MO_LINE_ITEMS_TABLE_ID, read_token,
                                     fields=["Confirmed Line Item Total"],
                                     formula=formula)
                for lr in li_recs:
                    li_total_map[lr["id"]] = float(lr.get("fields", {}).get("Confirmed Line Item Total") or 0)

        invoices = []
        # Collect customer IDs that are missing Snapshot Org so we can batch-fetch names
        missing_cust_ids = set()
        for rec in inv_records:
            f = rec.get("fields", {})
            if not f.get("Snapshot Org"):
                for cid in f.get("Customer", []):
                    missing_cust_ids.add(cid)
        # Batch-fetch org names for those customers
        cust_name_map = {}
        for cid in missing_cust_ids:
            try:
                cr = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{cid}",
                    headers=at_headers(read_token),
                    params={"fields[]": ["Organization Name"]},
                    timeout=10,
                )
                if cr.status_code == 200:
                    cust_name_map[cid] = cr.json().get("fields", {}).get("Organization Name", "")
            except Exception:
                pass

        for rec in inv_records:
            f = rec.get("fields", {})
            order_id     = str(f.get("Order ID", "")).strip()
            so_number    = f"SO-{order_id}" if order_id else ""
            inv_number   = f.get("Document ID", f"IN-{order_id}")
            li_ids       = f.get("MO Line Items", [])
            total        = round(sum(li_total_map.get(lid, 0) for lid in li_ids), 2)
            cc_status    = f.get("Stripe Invoice Status (CC)", "")
            ach_status   = f.get("Stripe Invoice Status (ACH)", "")
            is_paid      = bool(f.get("Invoice Paid")) or cc_status == "paid" or ach_status == "paid"
            check_number = f.get("Check #", "") or ""
            check_date   = f.get("Check Date", "") or ""
            check_amt_raw = f.get("Check Payment Amount")
            check_amount  = float(check_amt_raw) if check_amt_raw is not None else None
            # Resolve customer name: prefer Snapshot Org, fall back to linked customer record
            snapshot_org  = f.get("Snapshot Org", "") or ""
            if not snapshot_org:
                linked_custs = f.get("Customer", [])
                snapshot_org = cust_name_map.get(linked_custs[0], "") if linked_custs else ""
            if not snapshot_org:
                bill_to_org_raw = f.get("Bill-To Org Name (from Customer)", [])
                snapshot_org = (bill_to_org_raw[0] if isinstance(bill_to_org_raw, list) and bill_to_org_raw else bill_to_org_raw) or ""
            bill_email_raw = f.get("Bill-To Contact Email (from Customer)", [])
            invoices.append({
                "record_id":     rec["id"],
                "inv_number":    inv_number,
                "so_number":     so_number,
                "customer_name": snapshot_org,
                "date":          f.get("Date", ""),
                "due_date":      f.get("Stripe Invoice Due Date", "") or "",
                "total":         total,
                "is_paid":       is_paid,
                "check_number":  check_number,
                "check_date":    check_date,
                "check_amount":  check_amount,
                "has_stripe":           bool(f.get("Stripe Invoice URL (CC)", "")),
                "billing_email":        bill_email_raw[0] if isinstance(bill_email_raw, list) and bill_email_raw else (bill_email_raw or ""),
                "overdue_notice_dates": (f.get("Overdue Notice Dates") or "").strip(),
            })

        invoices.sort(key=lambda x: x.get("date", ""), reverse=True)
        return Response(json.dumps({"invoices": invoices}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/invoices/<record_id>/create-stripe", methods=["POST", "OPTIONS"])
def admin_create_stripe_invoices(record_id):
    """Create Stripe CC + ACH invoices for an invoice record that is missing them."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "POST"})
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    try:
        read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        write_token = RETURNS_WRITE_TOKEN

        # Fetch invoice record
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=15,
        )
        if not r.ok:
            return Response(json.dumps({"error": "Invoice not found"}), status=404, headers=c, mimetype="application/json")
        fields = r.json().get("fields", {})
        if fields.get("Order Type") != "Invoice":
            return Response(json.dumps({"error": "Not an invoice"}), status=400, headers=c, mimetype="application/json")

        body_json  = request.get_json(silent=True) or {}
        force_recreate = body_json.get("force", False)

        def _first(lst):
            return lst[0] if isinstance(lst, list) and lst else (lst or "")

        # Guard: don't create if Stripe URLs already exist (unless force=true to void+recreate)
        existing_cc_url  = fields.get("Stripe Invoice URL (CC)", "")
        existing_ach_url = fields.get("Stripe Invoice URL (ACH)", "")
        if existing_cc_url and not force_recreate:
            return Response(json.dumps({"error": "Stripe invoices already exist for this record. Use force=true to void and recreate."}),
                            status=400, headers=c, mimetype="application/json")

        billing_email = _first(fields.get("Bill-To Contact Email (from Customer)", []))
        billing_name  = _first(fields.get("Bill-To Contact Name (from Customer)", []))
        org_name      = _first(fields.get("Bill-To Org Name (from Customer)", []))

        if not billing_email:
            return Response(json.dumps({"error": "No billing email on record"}),
                            status=400, headers=c, mimetype="application/json")

        # If force=true, void the existing Stripe invoices first by looking them up via URL
        if force_recreate and (existing_cc_url or existing_ach_url):
            ss_auth = (STRIPE_SECRET_KEY, "")
            # Fetch all open invoices for the billing email and void ones matching stored URLs
            for _url in [existing_cc_url, existing_ach_url]:
                if not _url:
                    continue
                # Try to find the invoice via Stripe list by hosted_invoice_url match
                list_r = req_lib.get("https://api.stripe.com/v1/invoices",
                    params={"limit": 100},
                    auth=ss_auth, timeout=15)
                if list_r.ok:
                    for stripe_inv in list_r.json().get("data", []):
                        if stripe_inv.get("hosted_invoice_url") == _url and stripe_inv.get("status") in ("open", "draft"):
                            req_lib.post(f"https://api.stripe.com/v1/invoices/{stripe_inv['id']}/void",
                                auth=ss_auth, timeout=15)
                            print(f"[stripe] voided {stripe_inv['id']}")
                            break
            # Clear the Airtable URL fields
            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": {"Stripe Invoice URL (CC)": "", "Stripe Invoice URL (ACH)": "",
                                 "Stripe Due Date": ""}},
                timeout=15,
            )

        # Fetch line items using Confirmed Adj. Unit Price + Confirmed Line Item Total
        li_ids = fields.get("MO Line Items", [])
        li_items = []
        for li_id in li_ids:
            lr = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                headers=at_headers(read_token), timeout=10,
            )
            if lr.ok:
                lf = lr.json().get("fields", {})
                pname = _first(lf.get("Product Name (from Product SKU)", [])) or \
                        _first(lf.get("Name + Variations (from Product SKU)", [])) or "Item"
                qty        = lf.get("Qty.", 0)
                unit_price = float(lf.get("Confirmed Adj. Unit Price") or lf.get("Confirmed Unit Price") or 0)
                li_total   = float(lf.get("Confirmed Line Item Total") or (qty * unit_price))
                li_items.append({"name": pname, "qty": qty, "unit_price": unit_price,
                                 "line_total": li_total})

        if not li_items:
            return Response(json.dumps({"error": "No line items found"}),
                            status=400, headers=c, mimetype="application/json")

        # Run synchronously so any Stripe or Airtable errors surface to the caller
        _create_stripe_invoices_for_record(write_token, record_id, billing_email, billing_name, org_name, li_items)

        _INVOICES_CACHE.clear()
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        print(f"[create-stripe] error for {record_id}: {e}")
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/invoices/<record_id>/mark-paid-check", methods=["PATCH"])
def admin_mark_invoice_paid_check(record_id):
    """Mark an invoice as paid by check — writes Check #, Check Date, Check Payment Amount."""
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    body = request.get_json() or {}
    check_number = (body.get("checkNumber") or "").strip()
    check_date   = (body.get("checkDate") or "").strip()
    check_amount = body.get("checkAmount")
    if not check_number or not check_date or check_amount is None:
        return Response(
            json.dumps({"error": "checkNumber, checkDate, and checkAmount are required"}),
            status=400, headers=c, mimetype="application/json",
        )
    try:
        write_token = RETURNS_WRITE_TOKEN
        r = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {
                "Check #":              check_number,
                "Check Date":           check_date,
                "Check Payment Amount": float(check_amount),
            }},
            timeout=15,
        )
        if not r.ok:
            return Response(
                json.dumps({"error": f"Airtable error: {r.text[:200]}"}),
                status=500, headers=c, mimetype="application/json",
            )
        _INVOICES_CACHE.clear()
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/invoices/<record_id>/resend", methods=["POST", "OPTIONS"])
def admin_resend_invoice(record_id):
    """Resend the invoice email (with PDF) for an existing invoice record."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "POST"})
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        # Fetch the invoice record
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=15,
        )
        if not r.ok:
            return Response(json.dumps({"error": "Invoice not found"}), status=404, headers=c, mimetype="application/json")
        fields = r.json().get("fields", {})
        if fields.get("Order Type") != "Invoice":
            return Response(json.dumps({"error": "Not an invoice"}), status=400, headers=c, mimetype="application/json")

        def _first(lst):
            return lst[0] if isinstance(lst, list) and lst else (lst or "")

        order_id   = str(fields.get("Order ID", "")).strip()
        inv_number = fields.get("Document ID", f"IN-{order_id}")
        so_number  = f"SO-{order_id}" if order_id else ""

        # Get tracking + ship date
        tracking = ship_date = ""
        tracking_recs = at_get_all(_SO_TRACKING_TABLE, _SO_TRACKING_TOKEN,
                                    fields=["Order #", "Tracking #", "Ship Date"], base_id=_SO_TRACKING_BASE)
        for tr in tracking_recs:
            if tr.get("fields", {}).get("Order #", "").strip() == so_number:
                tracking  = tr["fields"].get("Tracking #", "")
                ship_date = tr["fields"].get("Ship Date", "")
                break
        if not tracking:
            tracking = fields.get("Tracking #", "") or fields.get("Tracking", "")

        # Fetch line items
        li_ids = fields.get("MO Line Items", [])
        line_items = []
        for li_id in li_ids:
            lr = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                headers=at_headers(read_token), timeout=10,
            )
            if lr.ok:
                lf = lr.json().get("fields", {})
                pname = _first(lf.get("Product Name (from Product SKU)", [])) or \
                        _first(lf.get("Name + Variations (from Product SKU)", [])) or "Item"
                qty        = lf.get("Qty.", 0)
                unit_price = float(lf.get("Confirmed Adj. Unit Price") or lf.get("Confirmed Unit Price") or 0)
                li_total   = float(lf.get("Confirmed Line Item Total") or (qty * unit_price))
                line_items.append({"name": pname, "qty": qty, "unit_price": unit_price,
                                   "total": li_total})

        total      = round(sum(li["total"] for li in line_items), 2)
        to_email   = _first(fields.get("Bill-To Contact Email (from Customer)", []))
        to_name    = _first(fields.get("Bill-To Contact Name (from Customer)", []))
        org_name   = _first(fields.get("Bill-To Org Name (from Customer)", []))

        # Allow caller to override the destination email
        body = request.get_json(silent=True) or {}
        override_email = (body.get("email") or "").strip()
        if override_email:
            to_email = override_email

        if not to_email:
            return Response(json.dumps({"error": "No billing email on record"}), status=400, headers=c, mimetype="application/json")

        ship_city  = _first(fields.get("Customer City (from Customer)", []))
        ship_state = _first(fields.get("Customer State (from Customer)", []))
        ship_zip   = _first(fields.get("Customer Zip Code (from Customer)", []))
        ship_csz   = ", ".join(filter(None, [ship_city, f"{ship_state} {ship_zip}".strip()]))

        inv_dict = {
            "invNumber":    inv_number,
            "soNumber":     so_number,
            "date":         fields.get("Date", ""),
            "poNumber":     fields.get("Purchase Order #", ""),
            "orgName":      org_name,
            "contact":      to_name,
            "addr1":        _first(fields.get("Bill-To Address (Line 1) (from Customer)", [])),
            "addr2":        _first(fields.get("Bill-To Address (Line 2) (from Customer)", [])),
            "shipOrg":      _first(fields.get("Organization Name (from Customer)", [])),
            "shipName":     _first(fields.get("Main Contact Name (from Customer)", [])),
            "shipAddr1":    _first(fields.get("Customer Address (Line 1) (from Customer)", [])),
            "shipAddr2":    _first(fields.get("Customer Address (Line 2) (from Customer)", [])) or ship_csz,
            "tracking":     tracking,
            "shipDate":     ship_date,
            "lineItems":    line_items,
            "subtotal":     total,
            "stripeCcUrl":  fields.get("Stripe Invoice URL (CC)", ""),
            "stripeAchUrl": fields.get("Stripe Invoice URL (ACH)", ""),
        }
        pdf_bytes = _build_invoice_pdf_bytes(inv_dict)
        send_invoice_email(to_email, to_name, org_name, so_number, inv_number,
                           line_items, total, tracking, ship_date=ship_date, pdf_bytes=pdf_bytes,
                           stripe_cc_url=inv_dict.get("stripeCcUrl", ""),
                           stripe_ach_url=inv_dict.get("stripeAchUrl", ""))
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/invoices/<record_id>/overdue-notice", methods=["POST", "OPTIONS"])
def admin_send_overdue_notice(record_id):
    """Send an overdue notice email and log the date to Airtable."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "POST"})
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    try:
        read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        write_token = RETURNS_WRITE_TOKEN

        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=15,
        )
        if not r.ok:
            return Response(json.dumps({"error": "Invoice not found"}), status=404, headers=c, mimetype="application/json")
        fields = r.json().get("fields", {})
        if fields.get("Order Type") != "Invoice":
            return Response(json.dumps({"error": "Not an invoice"}), status=400, headers=c, mimetype="application/json")

        def _first(lst):
            return lst[0] if isinstance(lst, list) and lst else (lst or "")

        order_id   = str(fields.get("Order ID", "")).strip()
        inv_number = fields.get("Document ID", f"IN-{order_id}")
        due_date_str = fields.get("Stripe Invoice Due Date", "") or ""
        cc_url  = fields.get("Stripe Invoice URL (CC)", "")
        ach_url = fields.get("Stripe Invoice URL (ACH)", "")
        po_number = fields.get("Purchase Order #", "") or ""

        # Calculate days overdue
        days_overdue = 0
        if due_date_str:
            try:
                from datetime import date as _date
                due = _date.fromisoformat(due_date_str[:10])
                days_overdue = (_today_utc() - due).days
            except Exception:
                pass

        # Billing info
        to_email  = _first(fields.get("Bill-To Contact Email (from Customer)", []))
        to_name   = _first(fields.get("Bill-To Contact Name (from Customer)", []))
        org_name  = _first(fields.get("Bill-To Org Name (from Customer)", [])) or \
                    _first(fields.get("Snapshot Org", ""))

        # Allow caller to override destination email
        body = request.get_json(silent=True) or {}
        override_email = (body.get("email") or "").strip()
        if override_email:
            to_email = override_email

        if not to_email:
            return Response(json.dumps({"error": "No billing email on record"}), status=400, headers=c, mimetype="application/json")

        # Fetch line items for total
        li_ids = fields.get("MO Line Items", [])
        line_items = []
        for li_id in li_ids:
            lr = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                headers=at_headers(read_token), timeout=10,
            )
            if lr.ok:
                lf = lr.json().get("fields", {})
                price = float(lf.get("Confirmed Unit Price") or 0)
                qty   = lf.get("Qty.", 0)
                pname = _first(lf.get("Product Name (from Product SKU)", [])) or \
                        _first(lf.get("Name + Variations (from Product SKU)", [])) or "Item"
                line_items.append({"name": pname, "qty": qty, "unit_price": price})
        total = round(sum(li["qty"] * li["unit_price"] for li in line_items), 2)

        # Refresh Stripe invoices if due date is more than 45 days old (links go dead)
        if days_overdue >= 45 and cc_url and line_items:
            try:
                old_cc_id  = fields.get("Stripe Invoice ID (CC)", "")
                old_ach_id = fields.get("Stripe Invoice ID (ACH)", "")
                # Void old invoices
                for old_id in [old_cc_id, old_ach_id]:
                    if old_id:
                        req_lib.post(f"https://api.stripe.com/v1/invoices/{old_id}/void",
                                     auth=(STRIPE_KEY, ""), data={}, timeout=15)
                # Create fresh ones (new customer, fresh due date)
                _create_stripe_invoices_for_record(write_token, record_id, to_email, to_name, org_name, line_items)
                # Re-fetch updated URLs from Airtable
                refreshed = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
                    headers=at_headers(read_token), timeout=10,
                )
                if refreshed.ok:
                    rf = refreshed.json().get("fields", {})
                    cc_url  = rf.get("Stripe Invoice URL (CC)", cc_url)
                    ach_url = rf.get("Stripe Invoice URL (ACH)", ach_url)
                print(f"[overdue-notice] refreshed Stripe links for {inv_number} ({days_overdue} days overdue)")
            except Exception as refresh_err:
                print(f"[overdue-notice] Stripe refresh failed for {inv_number}: {refresh_err}")
                # Non-fatal — send notice with existing (possibly stale) links

        # Send overdue notice email
        _send_overdue_notice_email(
            to_email=to_email, to_name=to_name, org_name=org_name,
            inv_number=inv_number, po_number=po_number,
            total=total, days_overdue=days_overdue, due_date_str=due_date_str,
            cc_url=cc_url, ach_url=ach_url,
        )

        # Append today's date to Overdue Notice Dates field
        today_iso = _today_utc().strftime("%Y-%m-%d")
        existing_dates = (fields.get("Overdue Notice Dates") or "").strip()
        new_dates = (existing_dates + "\n" + today_iso).strip() if existing_dates else today_iso
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {"Overdue Notice Dates": new_dates}},
            timeout=15,
        )

        _INVOICES_CACHE.clear()
        return Response(json.dumps({"ok": True, "days_overdue": days_overdue}), headers=c, mimetype="application/json")
    except Exception as e:
        print(f"[overdue-notice] error for {record_id}: {e}")
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


def _send_overdue_notice_email(to_email, to_name, org_name, inv_number, po_number,
                                total, days_overdue, due_date_str, cc_url, ach_url):
    """Send an overdue invoice notice email via SendGrid."""
    if not SENDGRID_API_KEY:
        return
    if TEST_EMAIL_OVERRIDE:
        to_list = [{"email": TEST_EMAIL_OVERRIDE, "name": to_name}]
    else:
        raw_emails = [e.strip() for e in to_email.replace(",", ";").split(";") if e.strip()]
        to_list = [{"email": e, "name": to_name} for e in raw_emails] if raw_emails else []
    if not to_list:
        return

    first_name = to_name.split()[0] if to_name else "there"
    subject = f"OVERDUE: Blue Alpha Invoice {inv_number}"

    # Format due date for display
    due_display = ""
    if due_date_str:
        try:
            from datetime import date as _date
            due_display = _date.fromisoformat(due_date_str[:10]).strftime("%B %d, %Y")
        except Exception:
            due_display = due_date_str

    overdue_phrase = f"{days_overdue} day{'s' if days_overdue != 1 else ''} past due" if days_overdue > 0 else "past due"

    po_row = f'<tr><td style="padding:6px 0;color:#555;font-size:13px;">PO Number</td><td style="padding:6px 0;color:#1a2633;font-size:13px;font-weight:600;">{po_number}</td></tr>' if po_number else ""
    due_row = f'<tr><td style="padding:6px 0;color:#555;font-size:13px;">Due Date</td><td style="padding:6px 0;color:#c0392b;font-size:13px;font-weight:600;">{due_display}</td></tr>' if due_display else ""

    payment_buttons = ""
    if cc_url:
        payment_buttons += f'<a href="{cc_url}" style="display:inline-block;margin:6px 8px 6px 0;padding:12px 28px;background:#1B2438;color:#ffffff;text-decoration:none;border-radius:5px;font-size:14px;font-weight:700;">Pay by Credit Card</a>'
    if ach_url:
        payment_buttons += f'<a href="{ach_url}" style="display:inline-block;margin:6px 0;padding:12px 28px;background:#5B7FA0;color:#ffffff;text-decoration:none;border-radius:5px;font-size:14px;font-weight:700;">Pay by ACH / Bank Transfer</a>'

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;padding:32px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#7B2318;padding:28px 40px;">
          <span style="font-family:Arial,Helvetica,sans-serif;font-size:22px;font-weight:800;color:#ffffff;letter-spacing:2px;">BLUE ALPHA</span>
          <span style="display:block;font-size:12px;color:#f0c0bb;margin-top:4px;letter-spacing:1px;">OVERDUE INVOICE NOTICE</span>
        </td></tr>
        <tr><td style="padding:32px 40px 16px;">
          <p style="margin:0 0 16px;font-size:16px;color:#1a2633;">Hi {first_name},</p>
          <p style="margin:0 0 20px;font-size:15px;color:#333;line-height:1.6;">
            This is a reminder that the following invoice is <strong style="color:#c0392b;">{overdue_phrase}</strong>.
            Please remit payment at your earliest convenience.
          </p>
          <table cellpadding="0" cellspacing="0" style="width:100%;margin-bottom:24px;">
            <tr><td style="padding:6px 0;color:#555;font-size:13px;">Invoice</td><td style="padding:6px 0;color:#1a2633;font-size:13px;font-weight:600;">{inv_number}</td></tr>
            {po_row}
            {due_row}
            <tr><td style="padding:6px 0;color:#555;font-size:13px;">Amount Due</td><td style="padding:6px 0;color:#c0392b;font-size:15px;font-weight:700;">${total:,.2f}</td></tr>
          </table>
          {'<p style="margin:0 0 16px;font-size:14px;color:#333;">View your invoice and pay online using one of the links below:</p>' + '<div style="margin-bottom:24px;">' + payment_buttons + '</div>' if payment_buttons else ''}
          <p style="margin:0 0 20px;font-size:14px;color:#555;line-height:1.6;">
            If you have already submitted payment, please disregard this notice.
            If you have any questions, reply to this email or contact us at
            <a href="mailto:orders@bluealpha.us" style="color:#1B2438;">orders@bluealpha.us</a>.
          </p>
          <p style="margin:0;font-size:14px;color:#555;">Thank you,<br><strong style="color:#1a2633;">Blue Alpha</strong></p>
        </td></tr>
        <tr><td style="background:#f5f7fa;padding:16px 40px;text-align:center;">
          <span style="font-size:11px;color:#999;">Blue Alpha &bull; bluealphabelts.com &bull; orders@bluealpha.us</span>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    plain = f"Hi {first_name},\n\nThis is a reminder that invoice {inv_number} is {overdue_phrase}.\n\nAmount Due: ${total:,.2f}\n" + (f"Due Date: {due_display}\n" if due_display else "") + (f"PO Number: {po_number}\n" if po_number else "") + ("\nPay by Credit Card: " + cc_url if cc_url else "") + ("\nPay by ACH: " + ach_url if ach_url else "") + "\n\nIf you have already submitted payment, please disregard this notice.\n\nThank you,\nBlue Alpha"

    payload = {
        "personalizations": [{"to": to_list}],
        "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": plain},
            {"type": "text/html",  "value": html_body},
        ],
    }
    req_lib.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
        json=payload, timeout=15,
    )


# ─────────────────────────────────────────────────────────────────────────────
# WARRANTY PORTAL
# ─────────────────────────────────────────────────────────────────────────────

WARRANTY_TABLE_ID   = "tbl2CQL5LiRXxCrnK"
WARRANTY_WRITE_TOKEN = os.environ.get("AIRTABLE_WRITE_TOKEN_2", "")

# In-memory lock to prevent duplicate webhook processing (race condition between
# Airtable automation and background scan firing for the same record simultaneously)
_warranty_processing: dict = {}   # key -> completed_at timestamp (0 = still in progress)
_warranty_processing_lock = threading.Lock()
_WARRANTY_DEDUP_WINDOW = 300  # seconds to block duplicate webhook calls after completion
# Disk-based photo store: photos saved to /tmp/warranty_photos/{key}
# More reliable than in-memory — survives within the same container instance.
_WARRANTY_PHOTO_DIR = "/tmp/warranty_photos"
os.makedirs(_WARRANTY_PHOTO_DIR, exist_ok=True)

# ShipStation product IDs and SKUs for warranty repair types
_WARRANTY_REPAIR_MAP = {
    "Rig Repair": {
        "sku":       "WARR-RIG",
        "name":      "Warranty Repair - Rig",
        "productId": 94998152,
    },
    "EDC Repair": {
        "sku":       "WARR-EDC",
        "name":      "Warranty Repair - EDC",
        "productId": 94998156,
    },
}


@app.route("/warranty")
def warranty_page():
    """Serve the warranty request form."""
    return send_from_directory(app.static_folder, "warranty.html")


@app.route("/warranty/photos/<key>")
def warranty_photo(key):
    """Serve a warranty submission photo from disk so Airtable can fetch it."""
    # key format: {uuid}.{ext}  — meta file: {uuid}.{ext}.meta (content_type\nfilename)
    if ".." in key or "/" in key:
        return Response("Forbidden", status=403)
    file_path = os.path.join(_WARRANTY_PHOTO_DIR, key)
    meta_path = file_path + ".meta"
    if not os.path.exists(file_path):
        return Response("Not found", status=404)
    content_type = "image/jpeg"
    filename = key
    if os.path.exists(meta_path):
        with open(meta_path) as mf:
            lines = mf.read().splitlines()
            if lines: content_type = lines[0]
            if len(lines) > 1: filename = lines[1]
    with open(file_path, "rb") as f:
        photo_bytes = f.read()
    return Response(photo_bytes, content_type=content_type,
                    headers={"Content-Disposition": f'inline; filename="{filename}"'})


@app.route("/api/warranty/submit", methods=["POST", "OPTIONS"])
def warranty_submit():
    """Accept multipart form submission: create Airtable record, then upload photos."""
    if request.method == "OPTIONS":
        return Response("", headers={
            **cors(),
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "POST",
        })
    c = cors()

    if not WARRANTY_WRITE_TOKEN:
        return Response(
            json.dumps({"success": False, "error": "Warranty system not configured"}),
            status=500, headers=c, mimetype="application/json",
        )

    try:
        first_name        = request.form.get("firstName", "").strip()
        last_name         = request.form.get("lastName", "").strip()
        email             = request.form.get("email", "").strip()
        phone             = request.form.get("phone", "").strip()
        address           = request.form.get("address", "").strip()
        city              = request.form.get("city", "").strip()
        state             = request.form.get("state", "").strip()
        zip_code          = request.form.get("zip", "").strip()
        repair_description= request.form.get("repairDescription", "").strip()
        original_order_num         = request.form.get("originalOrderNum", "").strip()
        original_purchaser_last_name = request.form.get("originalPurchaserLastName", "").strip()
        approval          = request.form.get("approval", "").strip()
        replacement_item_val = request.form.get("replacementItem", "").strip()
        send_customer_email_val = request.form.get("sendCustomerEmail", "0").strip() == "1"
        photos            = request.files.getlist("photos")

        # Basic validation
        required_fields = [first_name, last_name, email, phone, address, city, state, zip_code, repair_description]
        if not all(required_fields):
            return Response(
                json.dumps({"success": False, "error": "Missing required fields"}),
                status=400, headers=c, mimetype="application/json",
            )
        photos = photos[:5]  # cap at 5 (photos are optional for CS submissions)

        # ── Step 1: Create Airtable record (no photos yet) ──
        from datetime import datetime as _dt_sub, timezone as _tz_sub
        fields = {
            "First Name":          first_name,
            "Last Name":           last_name,
            "Email":               email,
            "Phone":               phone,
            "Address":             address,
            "City":                city,
            "State":               state,
            "Zip":                 zip_code,
            "Repair Description":  repair_description,
            "Request Date":        _dt_sub.now(_tz_sub.utc).strftime("%Y-%m-%d"),
        }
        if original_order_num:
            fields["Original Order #"] = original_order_num
        if original_purchaser_last_name:
            fields["Original Purchaser's Last Name"] = original_purchaser_last_name
        if approval:
            fields["Approval"] = approval
        if replacement_item_val:
            fields["Replacement Item"] = replacement_item_val
        if send_customer_email_val:
            fields["Send Customer Email"] = True

        create_resp = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{WARRANTY_TABLE_ID}",
            headers={
                "Authorization": f"Bearer {WARRANTY_WRITE_TOKEN}",
                "Content-Type":  "application/json",
            },
            json={"fields": fields},
            timeout=15,
        )
        if not create_resp.ok:
            return Response(
                json.dumps({"success": False, "error": f"Airtable error: {create_resp.text[:300]}"}),
                status=500, headers=c, mimetype="application/json",
            )
        record_id = create_resp.json()["id"]

        # ── Step 2: Attach photos via Airtable's content upload API ──
        # Photos used to be stashed on this container's disk and served back to
        # Airtable via public URLs, which Airtable fetched asynchronously — a
        # restart/redeploy in that window silently lost them (all three real
        # requests of 2026-07-22 lost their photos this way). uploadAttachment
        # pushes the bytes into Airtable synchronously: no temp storage, no
        # fetch window, and a hard status code if it fails. PATs with
        # data.records:write can use it (verified against this base 2026-07-25).
        # Files over the endpoint's 5 MB cap fall back to the legacy URL flow.
        import base64 as _b64
        import uuid as _uuid
        import os as _os
        BASE_URL = _os.environ.get("RAILWAY_PUBLIC_DOMAIN", "quote.bluealphabelts.com")
        if not BASE_URL.startswith("http"):
            BASE_URL = f"https://{BASE_URL}"
        _PHOTOS_FIELD_ID = "fld1g7wNcTB9b7SsW"  # Warranty Requests → Photos
        _DIRECT_UPLOAD_MAX = 5 * 1024 * 1024

        legacy_photo_urls = []
        uploaded_attachments = []  # field value from the last successful upload
        for photo in photos:
            photo_bytes = photo.read()
            content_type = photo.content_type or "image/jpeg"
            filename = photo.filename or "photo.jpg"

            uploaded = False
            if len(photo_bytes) <= _DIRECT_UPLOAD_MAX:
                try:
                    up_resp = req_lib.post(
                        f"https://content.airtable.com/v0/{AIRTABLE_BASE_ID}/{record_id}/{_PHOTOS_FIELD_ID}/uploadAttachment",
                        headers={"Authorization": f"Bearer {WARRANTY_WRITE_TOKEN}", "Content-Type": "application/json"},
                        json={"contentType": content_type,
                              "file": _b64.b64encode(photo_bytes).decode("ascii"),
                              "filename": filename},
                        timeout=30,
                    )
                    if up_resp.ok:
                        uploaded = True
                        uploaded_attachments = up_resp.json().get("fields", {}).get(_PHOTOS_FIELD_ID, uploaded_attachments)
                        print(f"[warranty_submit] Photo uploaded directly: {filename} ({len(photo_bytes)} bytes)")
                    else:
                        print(f"[warranty_submit] Direct upload failed {up_resp.status_code} for {filename}: {up_resp.text[:200]}")
                except Exception as up_err:
                    print(f"[warranty_submit] Direct upload error for {filename}: {up_err}")

            if not uploaded:
                # Legacy fallback (oversize file or upload failure): serve from
                # disk and let Airtable fetch the URL. Still has the old loss
                # window, but now only for the rare photo that needs it.
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
                key = f"{_uuid.uuid4().hex}.{ext}"
                file_path = os.path.join(_WARRANTY_PHOTO_DIR, key)
                with open(file_path, "wb") as pf:
                    pf.write(photo_bytes)
                with open(file_path + ".meta", "w") as mf:
                    mf.write(f"{content_type}\n{filename}")
                legacy_photo_urls.append({
                    "url": f"{BASE_URL}/warranty/photos/{key}",
                    "filename": filename,
                })

        if legacy_photo_urls:
            try:
                # PATCH replaces the whole field, so re-include the attachments
                # already uploaded directly (by id) or they would be wiped.
                keep_existing = [{"id": a["id"]} for a in uploaded_attachments if a.get("id")]
                patch_resp = req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{WARRANTY_TABLE_ID}/{record_id}",
                    headers={"Authorization": f"Bearer {WARRANTY_WRITE_TOKEN}", "Content-Type": "application/json"},
                    json={"fields": {"Photos": keep_existing + legacy_photo_urls}},
                    timeout=15,
                )
                if not patch_resp.ok:
                    print(f"[warranty_submit] Photo patch failed {patch_resp.status_code}: {patch_resp.text[:200]}")
            except Exception as photo_err:
                print(f"[warranty_submit] Photo patch error: {photo_err}")

        return Response(
            json.dumps({"success": True}),
            status=200, headers=c, mimetype="application/json",
        )

    except Exception as e:
        print(f"[warranty_submit] Unexpected error: {e}")
        return Response(
            json.dumps({"success": False, "error": str(e)}),
            status=500, headers=c, mimetype="application/json",
        )


def _search_ss_order(order_number):
    """Search ShipStation for an order by number. Returns first match dict or None."""
    try:
        r = req_lib.get(
            "https://ssapi.shipstation.com/orders",
            params={"orderNumber": order_number},
            headers=ss_headers(),
            timeout=15,
        )
        orders = r.json().get("orders", [])
        return orders[0] if orders else None
    except Exception as e:
        print(f"[_search_ss_order] Error: {e}")
        return None


def _send_warranty_approval_email(to_email, first_name, label_pdf_b64):
    """Send approval email with return label attached. FROM: info@bluealpha.us"""
    if not SENDGRID_API_KEY:
        print("[warranty_email] SendGrid not configured")
        return
    actual_to = TEST_EMAIL_OVERRIDE or to_email
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f4ef;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f4ef;padding:32px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#1B2438;border-bottom:3px solid #BD3333;padding:24px 36px;">
          <span style="font-family:Arial;font-size:20px;font-weight:800;color:#ffffff;letter-spacing:2px;">BLUE ALPHA</span>
        </td></tr>
        <tr><td style="padding:32px 36px;">
          <p style="color:#1a2633;font-size:16px;margin:0 0 16px;">Hi {first_name},</p>
          <p style="color:#4a5568;font-size:14px;line-height:1.7;margin:0 0 16px;">
            Great news — your warranty request has been approved for repair! Attached is a prepaid return label.
          </p>
          <p style="color:#4a5568;font-size:14px;line-height:1.7;margin:0 0 16px;">
            Please print the label, attach it to your package, and drop it off at any USPS location at your earliest convenience. Once we receive your item, we'll get it repaired and back to you as quickly as possible.
          </p>
          <p style="color:#4a5568;font-size:14px;line-height:1.7;margin:0;">
            Questions? Reply to this email and we'll be happy to help.
          </p>
          <p style="color:#4a5568;font-size:14px;line-height:1.7;margin:16px 0 0;">
            — Blue Alpha<br>
            <a href="mailto:info@bluealpha.us" style="color:#1B2438;">info@bluealpha.us</a>
          </p>
        </td></tr>
        <tr><td style="background:#DDD8C4;border-top:1px solid #c8c3b0;padding:16px 36px;text-align:center;">
          <p style="color:#6b7a8d;font-size:11px;margin:0;">Blue Alpha &bull; bluealphabelts.com</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""

    payload = {
        "personalizations": [{"to": [{"email": actual_to}]}],
        "from": {"email": "info@bluealpha.us", "name": "Blue Alpha"},
        "subject": "Your Blue Alpha Warranty Return Label",
        "content": [{"type": "text/html", "value": html_body}],
    }
    if label_pdf_b64:
        payload["attachments"] = [{
            "content":     label_pdf_b64,
            "type":        "application/pdf",
            "filename":    "warranty-return-label.pdf",
            "disposition": "attachment",
        }]

    try:
        r = req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        if r.status_code != 202:
            print(f"[warranty_email] SendGrid approval returned {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[warranty_email] Exception sending approval email: {e}")


def _send_warranty_ineligible_email(to_email, first_name, ineligibility_reason, denial_explanation=""):
    """Send not-eligible email. FROM: info@bluealpha.us"""
    if not SENDGRID_API_KEY:
        print("[warranty_email] SendGrid not configured")
        return
    actual_to = TEST_EMAIL_OVERRIDE or to_email
    reason_text = ineligibility_reason or "No reason provided."
    notes_line = (f'<p style="color:#4a5568;font-size:14px;line-height:1.7;margin:0 0 16px;">'
                  f'<strong>Customer Service Notes:</strong> {denial_explanation}</p>'
                  if denial_explanation else "")
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f4ef;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f4ef;padding:32px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#1B2438;border-bottom:3px solid #BD3333;padding:24px 36px;">
          <span style="font-family:Arial;font-size:20px;font-weight:800;color:#ffffff;letter-spacing:2px;">BLUE ALPHA</span>
        </td></tr>
        <tr><td style="padding:32px 36px;">
          <p style="color:#1a2633;font-size:16px;margin:0 0 16px;">Hi {first_name},</p>
          <p style="color:#4a5568;font-size:14px;line-height:1.7;margin:0 0 16px;">
            Thank you for submitting your warranty request. After reviewing your submission, we're unable to move forward with a repair at this time.
          </p>
          <p style="color:#4a5568;font-size:14px;line-height:1.7;margin:0 0 16px;">
            <strong>Reason:</strong> {reason_text}
          </p>
          {notes_line}
          <p style="color:#4a5568;font-size:14px;line-height:1.7;margin:0 0 16px;">
            If you have additional information that may change this decision, please reply to this email and our team will take another look.
          </p>
          <p style="color:#4a5568;font-size:14px;line-height:1.7;margin:0;">
            Thank you for being a Blue Alpha customer.
          </p>
          <p style="color:#4a5568;font-size:14px;line-height:1.7;margin:16px 0 0;">
            — Blue Alpha Customer Service<br>
            <a href="mailto:info@bluealpha.us" style="color:#1B2438;">info@bluealpha.us</a>
          </p>
        </td></tr>
        <tr><td style="background:#DDD8C4;border-top:1px solid #c8c3b0;padding:16px 36px;text-align:center;">
          <p style="color:#6b7a8d;font-size:11px;margin:0;">Blue Alpha &bull; bluealphabelts.com</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""

    try:
        r = req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": actual_to}]}],
                "from": {"email": "info@bluealpha.us", "name": "Blue Alpha"},
                "subject": "Update on Your Blue Alpha Warranty Request",
                "content": [{"type": "text/html", "value": html_body}],
            },
            timeout=15,
        )
        if r.status_code != 202:
            print(f"[warranty_email] SendGrid ineligible returned {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[warranty_email] Exception sending ineligible email: {e}")


def _wa_email_header():
    return """<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f4ef;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f4ef;padding:32px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#1B2438;border-bottom:3px solid #BD3333;padding:24px 36px;">
          <span style="font-family:Arial;font-size:20px;font-weight:800;color:#ffffff;letter-spacing:2px;">BLUE ALPHA</span>
        </td></tr><tr><td style="padding:32px 36px;">"""


def _wa_email_footer():
    return """<p style="color:#4a5568;font-size:14px;line-height:1.7;margin:16px 0 0;">
            — Blue Alpha Customer Service<br>
            <a href="mailto:info@bluealpha.us" style="color:#1B2438;">info@bluealpha.us</a>
          </p>
        </td></tr>
        <tr><td style="background:#DDD8C4;border-top:1px solid #c8c3b0;padding:16px 36px;text-align:center;">
          <p style="color:#6b7a8d;font-size:11px;margin:0;">Blue Alpha &bull; bluealphabelts.com</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def _send_warranty_replace_email(to_email, first_name, replacement_item, label_pdf_b64=None):
    """Replace approval email — includes return label if provided."""
    if not SENDGRID_API_KEY:
        return
    actual_to = TEST_EMAIL_OVERRIDE or to_email
    if label_pdf_b64:
        return_label_line = """<p style="color:#4a5568;font-size:14px;line-height:1.7;margin:0 0 16px;">
            Attached is a prepaid return label. Please print it, attach it to your original item, and drop it off at any USPS location.
          </p>"""
    else:
        return_label_line = ""
    html_body = _wa_email_header() + f"""
          <p style="color:#1a2633;font-size:16px;margin:0 0 16px;">Hi {first_name},</p>
          <p style="color:#4a5568;font-size:14px;line-height:1.7;margin:0 0 16px;">
            Great news — your warranty request has been approved! We'll be sending out a replacement <strong>{replacement_item}</strong> to you shortly.
          </p>
          {return_label_line}
          <p style="color:#4a5568;font-size:14px;line-height:1.7;margin:0;">
            If you have any questions, please reply to this email and we'll be happy to help.
          </p>""" + _wa_email_footer()

    payload = {
        "personalizations": [{"to": [{"email": actual_to}]}],
        "from": {"email": "info@bluealpha.us", "name": "Blue Alpha"},
        "subject": "Your Blue Alpha Warranty Request Has Been Approved",
        "content": [{"type": "text/html", "value": html_body}],
    }
    if label_pdf_b64:
        payload["attachments"] = [{
            "content": label_pdf_b64, "type": "application/pdf",
            "filename": "return-label.pdf", "disposition": "attachment",
        }]
    try:
        r = req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json=payload, timeout=15,
        )
        if r.status_code != 202:
            print(f"[warranty_email] replace email returned {r.status_code}: {r.text}", flush=True)
    except Exception as e:
        print(f"[warranty_email] replace email error: {e}", flush=True)


@app.route("/api/warranty/webhook", methods=["POST"])
def warranty_webhook():
    """
    Handles two triggers:
      - approval_changed: create SS return label + SS order, update Airtable, email customer
      - received_changed: flip SS order to awaiting_shipment
    """
    c = cors()
    try:
        body = request.get_json(force=True) or {}
        record_id = body.get("record_id", "").strip()
        trigger   = body.get("trigger", "").strip()
        print(f"[warranty_webhook] received: record={record_id} trigger={trigger}", flush=True)

        if not record_id or not trigger:
            return Response(
                json.dumps({"success": False, "error": "Missing record_id or trigger"}),
                status=400, headers=c, mimetype="application/json",
            )

        # ── Duplicate-call guard: reject if this record is already being processed
        #    or was processed within the last 5 minutes ──
        processing_key = f"{record_id}:{trigger}"
        import time as _time
        now = _time.time()
        with _warranty_processing_lock:
            # Clean up entries older than the dedup window
            expired = [k for k, t in _warranty_processing.items() if t > 0 and now - t > _WARRANTY_DEDUP_WINDOW]
            for k in expired:
                del _warranty_processing[k]
            if processing_key in _warranty_processing:
                print(f"[warranty_webhook] duplicate call ignored for {processing_key}", flush=True)
                return Response(
                    json.dumps({"success": True, "action": "duplicate_ignored"}),
                    status=200, headers=c, mimetype="application/json",
                )
            _warranty_processing[processing_key] = 0  # 0 = in progress
        try:
            result = _warranty_webhook_inner(record_id, trigger, c)
            with _warranty_processing_lock:
                _warranty_processing[processing_key] = _time.time()  # mark completed
            return result
        except Exception:
            with _warranty_processing_lock:
                _warranty_processing.pop(processing_key, None)  # on error, allow retry
            raise

    except Exception as outer_err:
        print(f"[warranty_webhook] outer error: {outer_err}", flush=True)
        return Response(json.dumps({"success": False, "error": str(outer_err)}),
                        status=500, headers=cors(), mimetype="application/json")


def _warranty_webhook_inner(record_id, trigger, c):
    try:
        # ── Fetch full Airtable record (use read token — write token lacks read scope) ──
        _read_tok = AIRTABLE_OPS_TOKEN or AIRTABLE_BASE_TOKEN or WARRANTY_WRITE_TOKEN
        rec_resp = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{WARRANTY_TABLE_ID}/{record_id}",
            headers={"Authorization": f"Bearer {_read_tok}"},
            timeout=15,
        )
        if not rec_resp.ok:
            return Response(
                json.dumps({"success": False, "error": f"Could not fetch record: {rec_resp.text[:200]}"}),
                status=500, headers=c, mimetype="application/json",
            )
        fields = rec_resp.json().get("fields", {})

        # Helper: format Airtable date (YYYY-MM-DD) as ShipStation paymentDate
        def _ss_payment_date(airtable_date_str):
            if airtable_date_str:
                d = (airtable_date_str or "").strip()[:10]  # take YYYY-MM-DD portion
                if d:
                    return f"{d}T00:00:00.0000000"
            return None

        # ────────────────────────────────────────────────────────────────
        # TRIGGER: approval_changed
        # ────────────────────────────────────────────────────────────────
        if trigger == "approval_changed":
            approval           = (fields.get("Approval") or "").strip()
            first_name         = (fields.get("First Name") or "").strip()
            last_name          = (fields.get("Last Name") or "").strip()
            email              = (fields.get("Email") or "").strip()
            phone              = (fields.get("Phone") or "").strip()
            address            = (fields.get("Address") or "").strip()
            city               = (fields.get("City") or "").strip()
            state              = (fields.get("State") or "").strip()
            zip_code           = (fields.get("Zip") or "").strip()
            original_order_num = (fields.get("Original Order #") or "").strip()
            ineligibility_reason = (fields.get("Ineligibility Reason") or "").strip()

            _DENIAL_OPTIONS = ("Outside warranty window", "Not a Blue Alpha product", "Repair not deemed necessary")

            # ── Idempotency: skip if already processed ──
            if approval in ("Rig Repair", "EDC Repair", "Replace") and (fields.get("Tracking #") or fields.get("Warranty Order #")):
                print(f"[warranty_webhook] approval_changed skipped — Tracking # already set for {record_id}", flush=True)
                return Response(
                    json.dumps({"success": True, "action": "already_processed"}),
                    status=200, headers=c, mimetype="application/json",
                )

            if approval in ("Rig Repair", "EDC Repair"):
                repair_info = _WARRANTY_REPAIR_MAP[approval]
                from datetime import datetime, timezone

                # ── Order reference ──
                if original_order_num:
                    order_ref = f"{original_order_num}-W"
                else:
                    order_ref = f"{last_name}-W"

                # ── Write order ref immediately to block duplicate webhook calls ──
                _lock_resp = req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{WARRANTY_TABLE_ID}/{record_id}",
                    headers={"Authorization": f"Bearer {WARRANTY_WRITE_TOKEN}", "Content-Type": "application/json"},
                    json={"fields": {"Warranty Order #": order_ref}}, timeout=10,
                )
                if not _lock_resp.ok:
                    print(f"[warranty_webhook] ABORT — could not write Warranty Order # lock for {record_id}: {_lock_resp.text[:200]}", flush=True)
                    return Response(json.dumps({"success": False, "error": "Failed to write idempotency lock — aborting to prevent duplicate"}),
                                    status=500, headers=c, mimetype="application/json")

                # ── Look up original SS order if we have an order number ──
                orig_ss_order_id = None
                if original_order_num:
                    orig_order = _search_ss_order(original_order_num)
                    if orig_order:
                        orig_ss_order_id = orig_order.get("orderId")

                # ── Create return label ──
                label_payload = {
                    "carrierCode":  "stamps_com",
                    "serviceCode":  "usps_ground_advantage",
                    "packageCode":  "package",
                    "shipDate":     datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "weight":       {"value": 32, "units": "ounces"},
                    "shipFrom": {
                        "name":       f"{first_name} {last_name}".strip(),
                        "street1":    address,
                        "city":       city,
                        "state":      state,
                        "postalCode": zip_code,
                        "country":    "US",
                        "phone":      phone,
                    },
                    "shipTo": {
                        "name":       "Blue Alpha",
                        "company":    "Blue Alpha",
                        "street1":    "35 Andrew St",
                        "city":       "Newnan",
                        "state":      "GA",
                        "postalCode": "30263",
                        "country":    "US",
                        "phone":      "6789822442",
                    },
                    "isReturnLabel": True,
                    "testLabel":     False,
                }
                if orig_ss_order_id:
                    label_payload["orderId"] = int(orig_ss_order_id)

                label_resp = req_lib.post(
                    "https://ssapi.shipstation.com/shipments/createlabel",
                    headers={**ss_headers(), "Content-Type": "application/json"},
                    json=label_payload,
                    timeout=20,
                )
                if not label_resp.ok:
                    return Response(
                        json.dumps({"success": False, "error": f"SS createlabel failed: {label_resp.text[:300]}"}),
                        status=500, headers=c, mimetype="application/json",
                    )
                label_result     = label_resp.json()
                label_pdf_b64    = label_result.get("labelData", "")
                label_tracking   = label_result.get("trackingNumber", "")

                # ── Update Airtable: order ref + tracking (SS order created on Item Received) ──
                tracking_url = (
                    f"https://tools.usps.com/go/TrackConfirmAction?qtc_tLabels1={label_tracking}"
                    if label_tracking else ""
                )
                at_update_fields = {
                    "Warranty Order #":  order_ref,
                    "USPS Tracking":     tracking_url,
                    "Tracking #":        label_tracking,
                }
                req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{WARRANTY_TABLE_ID}/{record_id}",
                    headers={
                        "Authorization": f"Bearer {WARRANTY_WRITE_TOKEN}",
                        "Content-Type":  "application/json",
                    },
                    json={"fields": at_update_fields},
                    timeout=15,
                )

                # ── Send approval email ──
                print(f"[warranty_webhook] sending approval email to {email}, label_data_len={len(label_pdf_b64)}", flush=True)
                _send_warranty_approval_email(email, first_name, label_pdf_b64)

                return Response(
                    json.dumps({"success": True, "action": "label_sent", "orderRef": order_ref}),
                    status=200, headers=c, mimetype="application/json",
                )

            elif approval in ("Outside warranty window", "Not a Blue Alpha product", "Repair not deemed necessary"):
                denial_explanation = (fields.get("Denial Explanation") or "").strip()
                _send_warranty_ineligible_email(email, first_name, approval, denial_explanation)
                # ── Set Tracking # to "N/A" so the scan's idempotency check won't re-fire ──
                req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{WARRANTY_TABLE_ID}/{record_id}",
                    headers={"Authorization": f"Bearer {WARRANTY_WRITE_TOKEN}", "Content-Type": "application/json"},
                    json={"fields": {"Tracking #": "N/A"}},
                    timeout=10,
                )
                return Response(
                    json.dumps({"success": True, "action": "denial_email_sent", "reason": approval}),
                    status=200, headers=c, mimetype="application/json",
                )

            elif approval.lower() == "replace w/o return":
                # ── No return label; just create SS order and email customer ──
                replacement_item = (fields.get("Replacement Item") or "").strip()
                if not replacement_item:
                    return Response(json.dumps({"success": False, "error": "Replacement Item field is empty"}),
                                    status=400, headers=c, mimetype="application/json")
                if fields.get("Warranty Order #"):
                    return Response(json.dumps({"success": True, "action": "already_processed"}),
                                    status=200, headers=c, mimetype="application/json")
                from datetime import datetime, timezone
                if original_order_num:
                    order_ref = f"{original_order_num}-W"
                else:
                    order_ref = f"{last_name}-W"
                # ── Write order ref immediately to block duplicate webhook calls ──
                _lock_resp = req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{WARRANTY_TABLE_ID}/{record_id}",
                    headers={"Authorization": f"Bearer {WARRANTY_WRITE_TOKEN}", "Content-Type": "application/json"},
                    json={"fields": {"Warranty Order #": order_ref}}, timeout=10,
                )
                if not _lock_resp.ok:
                    print(f"[warranty_webhook] ABORT — could not write Warranty Order # lock for {record_id}: {_lock_resp.text[:200]}", flush=True)
                    return Response(json.dumps({"success": False, "error": "Failed to write idempotency lock — aborting to prevent duplicate"}),
                                    status=500, headers=c, mimetype="application/json")
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000")
                _caddr = {"name": f"{first_name} {last_name}".strip(), "street1": address,
                          "city": city, "state": state, "postalCode": zip_code, "country": "US", "phone": phone}
                _pd = _ss_payment_date(fields.get("Request Date"))
                _order_payload = {"orderNumber": order_ref, "orderKey": order_ref, "orderStatus": "awaiting_shipment",
                          "orderDate": today_str, "billTo": _caddr, "shipTo": _caddr,
                          "items": [{"name": replacement_item, "quantity": 1, "unitPrice": 0}],
                          "advancedOptions": {"storeId": 241180}}
                if _pd:
                    _order_payload["paymentDate"] = _pd
                order_resp = req_lib.post(
                    "https://ssapi.shipstation.com/orders/createorder",
                    headers={**ss_headers(), "Content-Type": "application/json"},
                    json=_order_payload,
                    timeout=20,
                )
                if not order_resp.ok:
                    print(f"[warranty_webhook] replace_wo_return SS order warning: {order_resp.text[:300]}", flush=True)
                req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{WARRANTY_TABLE_ID}/{record_id}",
                    headers={"Authorization": f"Bearer {WARRANTY_WRITE_TOKEN}", "Content-Type": "application/json"},
                    json={"fields": {"Warranty Order #": order_ref}}, timeout=10,
                )
                print(f"[warranty_webhook] sending replace_wo_return email to {email}", flush=True)
                _send_warranty_replace_email(email, first_name, replacement_item)
                return Response(json.dumps({"success": True, "action": "replace_wo_return", "orderRef": order_ref}),
                                status=200, headers=c, mimetype="application/json")

            elif approval == "Replace":
                # ── Create return label + SS replacement order + email with label ──
                replacement_item = (fields.get("Replacement Item") or "").strip()
                if not replacement_item:
                    return Response(json.dumps({"success": False, "error": "Replacement Item field is empty"}),
                                    status=400, headers=c, mimetype="application/json")
                if fields.get("Tracking #") or fields.get("Warranty Order #"):
                    return Response(json.dumps({"success": True, "action": "already_processed"}),
                                    status=200, headers=c, mimetype="application/json")
                from datetime import datetime, timezone
                if original_order_num:
                    order_ref = f"{original_order_num}-W"
                else:
                    order_ref = f"{last_name}-W"
                # ── Write order ref immediately to block duplicate webhook calls ──
                _lock_resp = req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{WARRANTY_TABLE_ID}/{record_id}",
                    headers={"Authorization": f"Bearer {WARRANTY_WRITE_TOKEN}", "Content-Type": "application/json"},
                    json={"fields": {"Warranty Order #": order_ref}}, timeout=10,
                )
                if not _lock_resp.ok:
                    print(f"[warranty_webhook] ABORT — could not write Warranty Order # lock for {record_id}: {_lock_resp.text[:200]}", flush=True)
                    return Response(json.dumps({"success": False, "error": "Failed to write idempotency lock — aborting to prevent duplicate"}),
                                    status=500, headers=c, mimetype="application/json")

                # Create return label
                label_resp = req_lib.post(
                    "https://ssapi.shipstation.com/shipments/createlabel",
                    headers={**ss_headers(), "Content-Type": "application/json"},
                    json={"carrierCode": "stamps_com", "serviceCode": "usps_ground_advantage",
                          "packageCode": "package", "shipDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                          "weight": {"value": 32, "units": "ounces"},
                          "shipFrom": {"name": f"{first_name} {last_name}".strip(), "street1": address,
                                       "city": city, "state": state, "postalCode": zip_code, "country": "US", "phone": phone},
                          "shipTo": {"name": "Blue Alpha", "company": "Blue Alpha", "street1": "35 Andrew St",
                                     "city": "Newnan", "state": "GA", "postalCode": "30263", "country": "US", "phone": "6789822442"},
                          "isReturnLabel": True, "testLabel": False},
                    timeout=20,
                )
                if not label_resp.ok:
                    return Response(json.dumps({"success": False, "error": f"SS createlabel failed: {label_resp.text[:300]}"}),
                                    status=500, headers=c, mimetype="application/json")
                label_result   = label_resp.json()
                label_pdf_b64  = label_result.get("labelData", "")
                label_tracking = label_result.get("trackingNumber", "")

                # Create SS replacement order
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000")
                _caddr = {"name": f"{first_name} {last_name}".strip(), "street1": address,
                          "city": city, "state": state, "postalCode": zip_code, "country": "US", "phone": phone}
                _pd = _ss_payment_date(fields.get("Request Date"))
                _order_payload = {"orderNumber": order_ref, "orderKey": order_ref, "orderStatus": "awaiting_shipment",
                          "orderDate": today_str, "billTo": _caddr, "shipTo": _caddr,
                          "items": [{"name": replacement_item, "quantity": 1, "unitPrice": 0}],
                          "weight": {"value": 4, "units": "ounces"},
                          "dimensions": {"units": "inches", "length": 8, "width": 6, "height": 2},
                          "advancedOptions": {"storeId": 241180}}
                if _pd:
                    _order_payload["paymentDate"] = _pd
                order_resp = req_lib.post(
                    "https://ssapi.shipstation.com/orders/createorder",
                    headers={**ss_headers(), "Content-Type": "application/json"},
                    json=_order_payload,
                    timeout=20,
                )
                if not order_resp.ok:
                    print(f"[warranty_webhook] replace SS order warning: {order_resp.text[:300]}", flush=True)

                # Update Airtable
                tracking_url = (f"https://tools.usps.com/go/TrackConfirmAction?qtc_tLabels1={label_tracking}"
                                if label_tracking else "")
                req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{WARRANTY_TABLE_ID}/{record_id}",
                    headers={"Authorization": f"Bearer {WARRANTY_WRITE_TOKEN}", "Content-Type": "application/json"},
                    json={"fields": {"Warranty Order #": order_ref, "USPS Tracking": tracking_url, "Tracking #": label_tracking}},
                    timeout=10,
                )
                print(f"[warranty_webhook] sending replace email to {email}, label_len={len(label_pdf_b64)}", flush=True)
                _send_warranty_replace_email(email, first_name, replacement_item, label_pdf_b64)
                return Response(json.dumps({"success": True, "action": "replace", "orderRef": order_ref}),
                                status=200, headers=c, mimetype="application/json")

            else:
                # Approval value not actionable yet (e.g. "Pending" or empty)
                return Response(
                    json.dumps({"success": True, "action": "no_op", "approval": approval}),
                    status=200, headers=c, mimetype="application/json",
                )

        # ────────────────────────────────────────────────────────────────
        # TRIGGER: received_changed — create SS order as awaiting_shipment
        # ────────────────────────────────────────────────────────────────
        elif trigger == "received_changed":
            from datetime import datetime, timezone

            warranty_order_num = (fields.get("Warranty Order #") or "").strip()
            approval           = (fields.get("Approval") or "").strip()
            first_name         = (fields.get("First Name") or "").strip()
            last_name          = (fields.get("Last Name") or "").strip()
            phone              = (fields.get("Phone") or "").strip()
            address            = (fields.get("Address") or "").strip()
            city               = (fields.get("City") or "").strip()
            state              = (fields.get("State") or "").strip()
            zip_code           = (fields.get("Zip") or "").strip()

            if not warranty_order_num:
                return Response(
                    json.dumps({"success": False, "error": "No Warranty Order # — was it approved first?"}),
                    status=400, headers=c, mimetype="application/json",
                )
            if approval not in ("Rig Repair", "EDC Repair"):
                return Response(
                    json.dumps({"success": False, "error": f"Unexpected approval: {approval}"}),
                    status=400, headers=c, mimetype="application/json",
                )

            # ── Idempotency: skip if SS order already exists ──
            existing = _search_ss_order(warranty_order_num)
            if existing:
                print(f"[warranty_webhook] received_changed skipped — SS order {warranty_order_num} already exists", flush=True)
                return Response(
                    json.dumps({"success": True, "action": "already_created", "orderNumber": warranty_order_num}),
                    status=200, headers=c, mimetype="application/json",
                )

            repair_info    = _WARRANTY_REPAIR_MAP[approval]
            today_str      = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000")
            _customer_addr = {
                "name":       f"{first_name} {last_name}".strip(),
                "street1":    address,
                "city":       city,
                "state":      state,
                "postalCode": zip_code,
                "country":    "US",
                "phone":      phone,
            }
            _pd = _ss_payment_date(fields.get("Received Date"))
            _repair_payload = {
                "orderNumber": warranty_order_num,
                "orderKey":    warranty_order_num,
                "orderStatus": "awaiting_shipment",
                "orderDate":   today_str,
                "billTo":      _customer_addr,
                "shipTo":      _customer_addr,
                "items": [{
                    "sku":       repair_info["sku"],
                    "name":      repair_info["name"],
                    "productId": repair_info["productId"],
                    "quantity":  1,
                    "unitPrice": 0,
                }],
                "advancedOptions": {"storeId": 241180},
            }
            if _pd:
                _repair_payload["paymentDate"] = _pd
            order_resp = req_lib.post(
                "https://ssapi.shipstation.com/orders/createorder",
                headers={**ss_headers(), "Content-Type": "application/json"},
                json=_repair_payload,
                timeout=20,
            )
            if not order_resp.ok:
                return Response(
                    json.dumps({"success": False, "error": f"SS createorder failed: {order_resp.text[:300]}"}),
                    status=500, headers=c, mimetype="application/json",
                )
            print(f"[warranty_webhook] SS order created on item received: {warranty_order_num}", flush=True)
            # Set Received Date in Airtable
            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{WARRANTY_TABLE_ID}/{record_id}",
                headers={"Authorization": f"Bearer {WARRANTY_WRITE_TOKEN}", "Content-Type": "application/json"},
                json={"fields": {"Received Date": today_str[:10]}},
                timeout=10,
            )
            return Response(
                json.dumps({"success": True, "action": "order_created", "orderNumber": warranty_order_num}),
                status=200, headers=c, mimetype="application/json",
            )

        else:
            return Response(
                json.dumps({"success": False, "error": f"Unknown trigger: {trigger}"}),
                status=400, headers=c, mimetype="application/json",
            )

    except Exception as e:
        print(f"[warranty_webhook] Unexpected error: {e}")
        return Response(
            json.dumps({"success": False, "error": str(e)}),
            status=500, headers=c, mimetype="application/json",
        )


# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# WARRANTY CATCH-UP SCANNER  (runs every 12 hours)
# Finds records that need processing but weren't caught by the real-time trigger
# ─────────────────────────────────────────────────────────────────────────────
_WARRANTY_SCAN_LOCK_FILE = "/tmp/warranty_scan.lock"

def _warranty_scan():
    """Scan Warranty Requests at 10 AM and 2 PM ET daily and process any missed records."""
    import time as _time
    import fcntl as _fcntl
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    import zoneinfo as _zi

    _ET = _zi.ZoneInfo("America/New_York")
    _RUN_HOURS = {10, 14}  # 10 AM and 2 PM ET
    _STARTUP_DELAY = 300   # 5 min — prevents re-firing when app restarts mid-target-hour

    _time.sleep(30)  # brief startup delay
    _process_start = _dt.now(_ET)
    _last_run_hour = None
    _read_tok = AIRTABLE_OPS_TOKEN or AIRTABLE_BASE_TOKEN or WARRANTY_WRITE_TOKEN

    while True:
        now_et = _dt.now(_ET)
        current_hour = now_et.hour
        secs_since_start = (now_et - _process_start).total_seconds()
        # Run at the top of each target hour, once per hour.
        # Also require 5 min since startup so deployments mid-hour don't re-fire.
        if (current_hour in _RUN_HOURS
                and _last_run_hour != (now_et.date(), current_hour)
                and secs_since_start >= _STARTUP_DELAY):
            _last_run_hour = (now_et.date(), current_hour)
            # ── Inter-worker file lock: only ONE gunicorn worker runs the scan ──
            try:
                _lock_fh = open(_WARRANTY_SCAN_LOCK_FILE, "w")
                _fcntl.flock(_lock_fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except BlockingIOError:
                print("[warranty-scan] another worker already running scan — skipping", flush=True)
                _time.sleep(60)
                continue
        else:
            _time.sleep(60)
            continue
        try:
            print("[warranty-scan] running catch-up scan", flush=True)
            # Fetch ALL warranty records (paginate through Airtable's 100-record pages)
            records = []
            offset = None
            while True:
                params = {}
                if offset:
                    params["offset"] = offset
                resp = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{WARRANTY_TABLE_ID}",
                    headers={"Authorization": f"Bearer {_read_tok}"},
                    params=params,
                    timeout=15,
                )
                if not resp.ok:
                    print(f"[warranty-scan] fetch failed: {resp.status_code}", flush=True)
                    break
                page = resp.json()
                records.extend(page.get("records", []))
                offset = page.get("offset")
                if not offset:
                    break
            print(f"[warranty-scan] fetched {len(records)} records", flush=True)
            if records:
                for rec in records:
                    f = rec["fields"]
                    rid = rec["id"]
                    send_email   = f.get("Send Customer Email", False)
                    item_rcvd    = f.get("Item Received", False)
                    tracking_set = bool(f.get("Tracking #", "").strip())
                    order_ref    = (f.get("Warranty Order #") or "").strip()
                    approval     = (f.get("Approval") or "").strip()

                    shipped_date = (f.get("Shipped Date") or "").strip()

                    # Check if SS order has shipped → populate Shipped Date + Outgoing Tracking #
                    if order_ref and not shipped_date:
                        try:
                            ship_resp = req_lib.get(
                                "https://ssapi.shipstation.com/shipments",
                                params={"orderNumber": order_ref, "pageSize": 50},
                                headers={**ss_headers()},
                                timeout=10,
                            )
                            if ship_resp.ok:
                                shipments = [s for s in ship_resp.json().get("shipments", [])
                                             if not s.get("voided", False)]
                                if shipments:
                                    latest = sorted(shipments, key=lambda s: s.get("shipDate",""), reverse=True)[0]
                                    ship_date = (latest.get("shipDate") or "")[:10]
                                    out_tracking = latest.get("trackingNumber", "")
                                    if ship_date:
                                        patch_fields = {"Shipped Date": ship_date}
                                        if out_tracking:
                                            patch_fields["Outgoing Tracking #"] = out_tracking
                                        req_lib.patch(
                                            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{WARRANTY_TABLE_ID}/{rid}",
                                            headers={"Authorization": f"Bearer {WARRANTY_WRITE_TOKEN}", "Content-Type": "application/json"},
                                            json={"fields": patch_fields},
                                            timeout=10,
                                        )
                                        print(f"[warranty-scan] shipped date set for {rid}: {ship_date} tracking={out_tracking}", flush=True)
                        except Exception as se:
                            print(f"[warranty-scan] shipment check error for {rid}: {se}", flush=True)

                    _DENIAL_OPTS = ("Outside warranty window", "Not a Blue Alpha product", "Repair not deemed necessary")

                    warranty_order_set = bool(order_ref)

                    # Needs processing: repairs/replace need tracking; replace w/o return needs order#
                    # Also guard on warranty_order_set: the webhook writes Warranty Order # as a lock BEFORE
                    # creating anything — if it's already set, approval_changed already started for this record.
                    if send_email and approval in ("Rig Repair", "EDC Repair", "Replace") and not tracking_set and not warranty_order_set:
                        print(f"[warranty-scan] queuing approval_changed for {rid}", flush=True)
                        try:
                            req_lib.post(
                                "http://localhost:" + os.environ.get("PORT", "5000") + "/api/warranty/webhook",
                                json={"record_id": rid, "trigger": "approval_changed"},
                                timeout=30,
                            )
                        except Exception as e:
                            print(f"[warranty-scan] approval call error: {e}", flush=True)

                    # Denial email missed (send_email checked, denial approval, no tracking#)
                    elif send_email and not tracking_set and approval in _DENIAL_OPTS:
                        print(f"[warranty-scan] queuing denial email for {rid} ({approval})", flush=True)
                        try:
                            req_lib.post(
                                "http://localhost:" + os.environ.get("PORT", "5000") + "/api/warranty/webhook",
                                json={"record_id": rid, "trigger": "approval_changed"},
                                timeout=30,
                            )
                        except Exception as e:
                            print(f"[warranty-scan] denial call error: {e}", flush=True)

                    # Replace w/o Return: needs order but hasn't been created yet
                    elif send_email and not warranty_order_set and approval.lower() == "replace w/o return":
                        print(f"[warranty-scan] queuing replace_wo_return for {rid}", flush=True)
                        try:
                            req_lib.post(
                                "http://localhost:" + os.environ.get("PORT", "5000") + "/api/warranty/webhook",
                                json={"record_id": rid, "trigger": "approval_changed"},
                                timeout=30,
                            )
                        except Exception as e:
                            print(f"[warranty-scan] replace_wo_return call error: {e}", flush=True)

                    # Needs SS order but order doesn't exist yet (repairs only — replacements handled above)
                    elif item_rcvd and order_ref and approval in ("Rig Repair", "EDC Repair"):
                        existing = _search_ss_order(order_ref)
                        if not existing:
                            print(f"[warranty-scan] queuing received_changed for {rid}", flush=True)
                            try:
                                req_lib.post(
                                    "http://localhost:" + os.environ.get("PORT", "5000") + "/api/warranty/webhook",
                                    json={"record_id": rid, "trigger": "received_changed"},
                                    timeout=30,
                                )
                            except Exception as e:
                                print(f"[warranty-scan] received call error: {e}", flush=True)

        except Exception as e:
            print(f"[warranty-scan] unexpected error: {e}", flush=True)
        finally:
            # Release the inter-worker file lock
            try:
                _fcntl.flock(_lock_fh, _fcntl.LOCK_UN)
                _lock_fh.close()
            except Exception:
                pass

        _time.sleep(60)  # check every minute, run only at target hours

threading.Thread(target=_warranty_scan, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# NEWNAN PD COUNTER TOOL + DEPARTMENT PORTAL
# ─────────────────────────────────────────────────────────────────────────────

_PD_DISCOUNT_25_CATEGORIES = {"EDC", "Rig"}


# ── Page routes ───────────────────────────────────────────────────────────────

@app.route("/pd", methods=["GET"])
def pd_counter_page():
    if not check_admin_session(request):
        return redirect("/quote-builder")
    return app.send_static_file("pd-counter.html")


@app.route("/pd/portal", methods=["GET"])
def pd_portal_page():
    user = get_portal_user(request)
    if not user:
        return redirect("/quote")
    return app.send_static_file("pd-portal.html")


# ── Admin API ─────────────────────────────────────────────────────────────────

@app.route("/api/pd/catalog", methods=["GET", "OPTIONS"])
def pd_catalog():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type"})
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    if not _CATALOG_CACHE["data"]:
        return Response(json.dumps({"error": "Catalog not loaded yet"}), status=503, headers=c, mimetype="application/json")
    import copy
    _PD_EXCLUDED_CATEGORIES = {"Apparel", "Other"}
    data = copy.deepcopy(_CATALOG_CACHE["data"])
    # Filter out Apparel and Other categories
    data["skus"] = [s for s in data.get("skus", []) if s.get("category", "") not in _PD_EXCLUDED_CATEGORIES]
    filtered_parent_ids = {s["parentId"] for s in data["skus"]}
    data["parents"] = [p for p in data.get("parents", []) if p["id"] in filtered_parent_ids]
    for sku in data["skus"]:
        cat = sku.get("category", "")
        price = sku.get("price", 0) or 0
        if cat in _PD_DISCOUNT_25_CATEGORIES:
            sku["price"] = round(price * 0.75, 2)
            sku["pdDiscount"] = 25
        else:
            sku["price"] = round(price * 0.90, 2)
            sku["pdDiscount"] = 10
    for sku in data.get("addonSkus", {}).values():
        for variant in sku.values():
            price = variant.get("price", 0) or 0
            variant["price"] = round(price * 0.90, 2)
            variant["pdDiscount"] = 10
    return Response(json.dumps({**data, "ts": _CATALOG_CACHE["ts"]}), headers=c, mimetype="application/json")


@app.route("/api/pd/departments", methods=["GET", "OPTIONS"])
def pd_departments():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type"})
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        records = at_get_all(
            CUSTOMERS_TABLE_ID, read_token,
            fields=["Organization Name", "Portal Role"],
            formula="{Portal Role}='Invoices'",
        )
        depts = []
        for r in records:
            f = r.get("fields", {})
            name = f.get("Organization Name", "").strip()
            if not name:
                continue
            depts.append({"record_id": r["id"], "name": name})
        depts.sort(key=lambda x: x["name"].lower())
        return Response(json.dumps(depts), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/pd/order/create", methods=["POST", "OPTIONS"])
def pd_order_create():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "POST"})
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    data = request.get_json(force=True) or {}
    department_id          = data.get("department_id", "").strip()
    officer_name           = data.get("officer_name", "").strip()
    badge_num              = data.get("badge_num", "").strip()
    line_items             = data.get("line_items", [])
    officer_portion        = float(data.get("officer_portion", 0))
    department_portion     = float(data.get("department_portion", 0))
    officer_payment_method = data.get("officer_payment_method", "Cash").strip()

    if not department_id or not officer_name or not line_items:
        return Response(json.dumps({"error": "Missing required fields"}), status=400, headers=c, mimetype="application/json")

    import datetime as _dt
    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = RETURNS_WRITE_TOKEN

    # 1. Auto-generate Order ID (shared SO-#### sequence with other manual orders)
    try:
        order_id = _next_order_id(read_token)   # plain number, e.g. "0446"
    except Exception as e:
        return Response(json.dumps({"error": f"Order ID generation failed: {e}"}), status=500, headers=c, mimetype="application/json")

    # 2. Create Manual Order record
    today = _dt.date.today().isoformat()
    try:
        mo_resp = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {
                "Order Type":              "PD Order",
                "Order ID":                order_id,
                "Date":                    today,
                "Officer Name":            officer_name,
                "Badge #":                 badge_num,
                "Officer Portion $":       officer_portion,
                "Department Portion $":    department_portion,
                "Officer Payment Method":  officer_payment_method,
                "Customer":                [department_id],
                "Sales Order Status":      "Approved",
                "Production Status":       "Not Started",
            }},
            timeout=15,
        )
        if not mo_resp.ok:
            return Response(json.dumps({"error": f"Airtable MO create error: {mo_resp.status_code} {mo_resp.text}"}),
                            status=500, headers=c, mimetype="application/json")
        mo_record = mo_resp.json()
        mo_record_id = mo_record["id"]
    except Exception as e:
        return Response(json.dumps({"error": f"MO create failed: {e}"}), status=500, headers=c, mimetype="application/json")

    # 3. Create MO Line Items
    for li in line_items:
        try:
            req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": {
                    "Manual Order":            [mo_record_id],
                    "Product SKU":             [li["sku_record_id"]],
                    "Qty.":                    int(li["qty"]),
                    "Confirmed Unit Price":    float(li["unit_price"]),
                    "Confirmed Adj. Unit Price": float(li["unit_price"]),
                }},
                timeout=15,
            )
        except Exception as e:
            print(f"[pd_order_create] line item create warning: {e}")

    return Response(json.dumps({"ok": True, "record_id": mo_record_id, "order_id": f"SO-{order_id}"}),
                    headers=c, mimetype="application/json")


@app.route("/api/pd/order/<record_id>/stripe/officer", methods=["POST", "OPTIONS"])
def pd_stripe_officer(record_id):
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "POST"})
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    data  = request.get_json(force=True) or {}
    email = data.get("email", "").strip()
    name  = data.get("name", "").strip()
    if not email:
        return Response(json.dumps({"error": "email required"}), status=400, headers=c, mimetype="application/json")

    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = RETURNS_WRITE_TOKEN

    try:
        mo_r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if not mo_r.ok:
            return Response(json.dumps({"error": "Order not found"}), status=404, headers=c, mimetype="application/json")
        mo_fields = mo_r.json().get("fields", {})
        order_id       = mo_fields.get("Order ID", record_id)
        officer_portion = float(mo_fields.get("Officer Portion $") or 0)
        amount_cents    = int(round(officer_portion * 100))
    except Exception as e:
        return Response(json.dumps({"error": f"Fetch order failed: {e}"}), status=500, headers=c, mimetype="application/json")

    stripe_auth = (STRIPE_SECRET_KEY, "")
    try:
        # Find or create customer
        cust_r = req_lib.get(
            "https://api.stripe.com/v1/customers",
            auth=stripe_auth,
            params={"email": email, "limit": 1},
            timeout=10,
        )
        custs = cust_r.json().get("data", [])
        if custs:
            cust_id = custs[0]["id"]
        else:
            cr = req_lib.post(
                "https://api.stripe.com/v1/customers",
                auth=stripe_auth,
                data={"email": email, "name": name},
                timeout=10,
            )
            cust_id = cr.json()["id"]

        # Create invoice
        inv_r = req_lib.post(
            "https://api.stripe.com/v1/invoices",
            auth=stripe_auth,
            data={
                "customer":            cust_id,
                "collection_method":   "send_invoice",
                "days_until_due":      "1",
                "payment_settings[payment_method_types][]": "card",
            },
            timeout=10,
        )
        inv_data = inv_r.json()
        if "error" in inv_data:
            return Response(json.dumps({"error": inv_data["error"].get("message", "Stripe error")}),
                            status=500, headers=c, mimetype="application/json")
        inv_id  = inv_data["id"]

        # Add invoice item
        req_lib.post(
            "https://api.stripe.com/v1/invoiceitems",
            auth=stripe_auth,
            data={
                "customer":             cust_id,
                "invoice":              inv_id,
                "description":          f"Order {order_id} \u2014 Officer Balance",
                "unit_amount_decimal":  str(amount_cents),
                "currency":             "usd",
                "quantity":             "1",
            },
            timeout=10,
        )

        # Finalize invoice
        fin_r = req_lib.post(
            f"https://api.stripe.com/v1/invoices/{inv_id}/finalize",
            auth=stripe_auth, timeout=10,
        )
        fin_data = fin_r.json()
        inv_url  = fin_data.get("hosted_invoice_url", "")

        # Patch Airtable
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {
                "Officer Stripe Invoice ID":     inv_id,
                "Officer Stripe Invoice URL":    inv_url,
                "Officer Stripe Invoice Status": "Open",
            }},
            timeout=10,
        )
        return Response(json.dumps({"ok": True, "url": inv_url}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/pd/order/<record_id>/stripe/department", methods=["POST", "OPTIONS"])
def pd_stripe_department(record_id):
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "POST"})
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")

    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = RETURNS_WRITE_TOKEN

    try:
        mo_r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if not mo_r.ok:
            return Response(json.dumps({"error": "Order not found"}), status=404, headers=c, mimetype="application/json")
        mo_fields = mo_r.json().get("fields", {})
        order_id           = mo_fields.get("Order ID", record_id)
        department_portion = float(mo_fields.get("Department Portion $") or 0)
        amount_cents       = int(round(department_portion * 100))
        customer_ids       = mo_fields.get("Customer", [])
    except Exception as e:
        return Response(json.dumps({"error": f"Fetch order failed: {e}"}), status=500, headers=c, mimetype="application/json")

    # Fetch department email
    dept_email = ""
    if customer_ids:
        try:
            cr = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_ids[0]}",
                headers=at_headers(read_token),
                params={"fields[]": ["Main Contact Email", "Bill-To Contact Email", "Organization Name"]},
                timeout=10,
            )
            cf = cr.json().get("fields", {})
            dept_email = cf.get("Bill-To Contact Email") or cf.get("Main Contact Email", "")
        except Exception:
            pass

    if not dept_email:
        return Response(json.dumps({"error": "No billing email on file for this department"}),
                        status=400, headers=c, mimetype="application/json")

    stripe_auth = (STRIPE_SECRET_KEY, "")
    try:
        # Find or create Stripe customer for department
        cust_r = req_lib.get(
            "https://api.stripe.com/v1/customers",
            auth=stripe_auth,
            params={"email": dept_email, "limit": 1},
            timeout=10,
        )
        custs = cust_r.json().get("data", [])
        if custs:
            cust_id = custs[0]["id"]
        else:
            cr2 = req_lib.post(
                "https://api.stripe.com/v1/customers",
                auth=stripe_auth,
                data={"email": dept_email},
                timeout=10,
            )
            cust_id = cr2.json()["id"]

        item_desc = f"Order {order_id} \u2014 Department Balance"

        def _make_invoice(payment_method_type, days_due):
            inv_r = req_lib.post(
                "https://api.stripe.com/v1/invoices",
                auth=stripe_auth,
                data={
                    "customer":            cust_id,
                    "collection_method":   "send_invoice",
                    "days_until_due":      str(days_due),
                    "payment_settings[payment_method_types][]": payment_method_type,
                },
                timeout=10,
            )
            inv_data = inv_r.json()
            if "error" in inv_data:
                raise Exception(inv_data["error"].get("message", "Stripe error"))
            inv_id = inv_data["id"]
            req_lib.post(
                "https://api.stripe.com/v1/invoiceitems",
                auth=stripe_auth,
                data={
                    "customer":            cust_id,
                    "invoice":             inv_id,
                    "description":         item_desc,
                    "unit_amount_decimal": str(amount_cents),
                    "currency":            "usd",
                    "quantity":            "1",
                },
                timeout=10,
            )
            fin_r = req_lib.post(
                f"https://api.stripe.com/v1/invoices/{inv_id}/finalize",
                auth=stripe_auth, timeout=10,
            )
            fin_data = fin_r.json()
            return inv_id, fin_data.get("hosted_invoice_url", "")

        cc_id,  cc_url  = _make_invoice("card", 30)
        ach_id, ach_url = _make_invoice("us_bank_account", 30)

        # Patch Airtable with CC invoice (primary)
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {
                "Department Stripe Invoice ID":     cc_id,
                "Department Stripe Invoice URL":    cc_url,
                "Department Stripe Invoice Status": "Open",
            }},
            timeout=10,
        )
        return Response(json.dumps({"ok": True, "cc_url": cc_url, "ach_url": ach_url}),
                        headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/pd/order/<record_id>/cash", methods=["PATCH", "OPTIONS"])
def pd_order_cash(record_id):
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "PATCH"})
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    import datetime as _dt
    write_token = RETURNS_WRITE_TOKEN
    today = _dt.date.today().isoformat()
    try:
        r = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {
                "Officer Cash Collected": True,
                "Officer Payment Date":   today,
            }},
            timeout=10,
        )
        if not r.ok:
            return Response(json.dumps({"error": f"Airtable error: {r.status_code}"}),
                            status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/pd/orders", methods=["GET", "OPTIONS"])
def pd_orders_list():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type"})
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        records = at_get_all(
            MANUAL_ORDERS_TABLE_ID, read_token,
            fields=["Order ID", "Date", "Officer Name", "Badge #",
                    "Officer Portion $", "Department Portion $", "Officer Payment Method",
                    "Officer Cash Collected", "Officer Payment Date",
                    "Officer Stripe Invoice URL", "Officer Stripe Invoice Status",
                    "Department Stripe Invoice URL", "Department Stripe Invoice Status",
                    "Customer", "Sales Order Status", "MO Line Items"],
            formula="{Order Type}='PD Order'",
        )
        orders = []
        for r in records:
            f = r.get("fields", {})
            orders.append({
                "record_id":                      r["id"],
                "order_id":                       f.get("Order ID", ""),
                "date":                           f.get("Date", ""),
                "officer_name":                   f.get("Officer Name", ""),
                "badge_num":                      f.get("Badge #", ""),
                "officer_portion":                f.get("Officer Portion $", 0),
                "department_portion":             f.get("Department Portion $", 0),
                "officer_payment_method":         f.get("Officer Payment Method", ""),
                "officer_cash_collected":         f.get("Officer Cash Collected", False),
                "officer_payment_date":           f.get("Officer Payment Date", ""),
                "officer_stripe_invoice_url":     f.get("Officer Stripe Invoice URL", ""),
                "officer_stripe_invoice_status":  f.get("Officer Stripe Invoice Status", ""),
                "department_stripe_invoice_url":  f.get("Department Stripe Invoice URL", ""),
                "department_stripe_invoice_status": f.get("Department Stripe Invoice Status", ""),
                "customer":                       f.get("Customer", []),
                "sales_order_status":             f.get("Sales Order Status", ""),
                "mo_line_items":                  f.get("MO Line Items", []),
            })
        orders.sort(key=lambda x: x["date"] or "", reverse=True)
        return Response(json.dumps(orders), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/pd/order/<record_id>", methods=["GET", "OPTIONS"])
def pd_order_detail(record_id):
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type"})
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        mo_r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if not mo_r.ok:
            return Response(json.dumps({"error": "Order not found"}), status=404, headers=c, mimetype="application/json")
        mo = mo_r.json()
        f  = mo.get("fields", {})
        order = {
            "record_id":                      mo["id"],
            "order_id":                       f.get("Order ID", ""),
            "date":                           f.get("Date", ""),
            "officer_name":                   f.get("Officer Name", ""),
            "badge_num":                      f.get("Badge #", ""),
            "officer_portion":                f.get("Officer Portion $", 0),
            "department_portion":             f.get("Department Portion $", 0),
            "officer_payment_method":         f.get("Officer Payment Method", ""),
            "officer_cash_collected":         f.get("Officer Cash Collected", False),
            "officer_payment_date":           f.get("Officer Payment Date", ""),
            "officer_stripe_invoice_url":     f.get("Officer Stripe Invoice URL", ""),
            "officer_stripe_invoice_status":  f.get("Officer Stripe Invoice Status", ""),
            "department_stripe_invoice_url":  f.get("Department Stripe Invoice URL", ""),
            "department_stripe_invoice_status": f.get("Department Stripe Invoice Status", ""),
            "customer":                       f.get("Customer", []),
            "sales_order_status":             f.get("Sales Order Status", ""),
        }
        li_ids = f.get("MO Line Items", [])
        line_items = []
        for li_id in li_ids:
            try:
                li_r = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                    headers=at_headers(read_token),
                    params={"fields[]": ["Name + Variations (from Product SKU)", "Qty.",
                                         "Confirmed Unit Price", "Picked Up"]},
                    timeout=10,
                )
                if li_r.ok:
                    lf = li_r.json().get("fields", {})
                    name_list = lf.get("Name + Variations (from Product SKU)", [])
                    name = name_list[0] if isinstance(name_list, list) and name_list else (name_list or "")
                    line_items.append({
                        "record_id":  li_id,
                        "name":       name,
                        "qty":        lf.get("Qty.", 1),
                        "unit_price": lf.get("Confirmed Unit Price", 0),
                        "picked_up":  lf.get("Picked Up", False),
                    })
            except Exception:
                pass
        order["line_items"] = line_items
        return Response(json.dumps(order), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/pd/order/<record_id>/pickup", methods=["PATCH", "OPTIONS"])
def pd_order_pickup(record_id):
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "PATCH"})
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    data         = request.get_json(force=True) or {}
    line_item_id = data.get("line_item_id", "").strip()
    picked_up    = bool(data.get("picked_up", False))
    if not line_item_id:
        return Response(json.dumps({"error": "line_item_id required"}), status=400, headers=c, mimetype="application/json")
    write_token = RETURNS_WRITE_TOKEN
    try:
        r = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{line_item_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {"Picked Up": picked_up}},
            timeout=10,
        )
        if not r.ok:
            return Response(json.dumps({"error": f"Airtable error: {r.status_code}"}),
                            status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


# ── Department Portal API ─────────────────────────────────────────────────────

@app.route("/api/pd/portal/orders", methods=["GET", "OPTIONS"])
def pd_portal_orders():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type"})
    c = cors()
    user = get_portal_user(request)
    if not user:
        return Response(json.dumps({"error": "Unauthorized"}), status=403, headers=c, mimetype="application/json")
    if not portal_can(user, "view_invoices"):
        return Response(json.dumps({"error": "Forbidden"}), status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id") or user.get("customerId", "")
    if not customer_id:
        return Response(json.dumps({"error": "No customer context"}), status=403, headers=c, mimetype="application/json")
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        records = at_get_all(
            MANUAL_ORDERS_TABLE_ID, read_token,
            fields=["Order ID", "Date", "Officer Name", "Badge Number",
                    "Department Portion $", "Department Stripe Invoice URL",
                    "Department Stripe Invoice Status", "Picked Up", "Customer"],
            formula=f'AND({{Order Type}}="Sales Order",FIND("{customer_id}",ARRAYJOIN({{Customer}},","))>0,{{Officer Name}}!="")',
        )
        orders = []
        for r in records:
            f = r.get("fields", {})
            orders.append({
                "record_id":                        r["id"],
                "order_id":                         f"SO-{f.get('Order ID', '')}" if f.get("Order ID") else "",
                "date":                             f.get("Date", ""),
                "officer_name":                     f.get("Officer Name", ""),
                "badge_num":                        f.get("Badge Number", ""),
                "department_portion":               f.get("Department Portion $", 0),
                "department_stripe_invoice_url":    f.get("Department Stripe Invoice URL", ""),
                "department_stripe_invoice_status": f.get("Department Stripe Invoice Status", ""),
                "picked_up":                        bool(f.get("Picked Up", False)),
            })
        orders.sort(key=lambda x: x["date"] or "", reverse=True)
        return Response(json.dumps(orders), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/pd/portal/order/<record_id>", methods=["GET", "OPTIONS"])
def pd_portal_order_detail(record_id):
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type"})
    c = cors()
    user = get_portal_user(request)
    if not user:
        return Response(json.dumps({"error": "Unauthorized"}), status=403, headers=c, mimetype="application/json")
    if not portal_can(user, "view_invoices"):
        return Response(json.dumps({"error": "Forbidden"}), status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id") or user.get("customerId", "")
    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        mo_r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if not mo_r.ok:
            return Response(json.dumps({"error": "Order not found"}), status=404, headers=c, mimetype="application/json")
        mo = mo_r.json()
        f  = mo.get("fields", {})
        # Verify customer owns this order
        if customer_id and customer_id not in (f.get("Customer") or []):
            return Response(json.dumps({"error": "Forbidden"}), status=403, headers=c, mimetype="application/json")
        raw_oid = f.get("Order ID", "")
        order = {
            "record_id":                        mo["id"],
            "order_id":                         f"SO-{raw_oid}" if raw_oid and not str(raw_oid).upper().startswith("SO") else raw_oid,
            "date":                             f.get("Date", ""),
            "officer_name":                     f.get("Officer Name", ""),
            "badge_num":                        f.get("Badge Number", ""),
            "department_portion":               f.get("Department Portion $", 0),
            "department_stripe_invoice_url":    f.get("Department Stripe Invoice URL", ""),
            "department_stripe_invoice_status": f.get("Department Stripe Invoice Status", ""),
            "sales_order_status":               f.get("Sales Order Status", ""),
            "picked_up":                        bool(f.get("Picked Up", False)),
        }
        li_ids = f.get("MO Line Items", [])
        line_items = []
        for li_id in li_ids:
            try:
                li_r = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                    headers=at_headers(read_token),
                    params={"fields[]": ["Name + Variations (from Product SKU)", "Qty.",
                                         "Confirmed Unit Price", "Picked Up"]},
                    timeout=10,
                )
                if li_r.ok:
                    lf = li_r.json().get("fields", {})
                    name_list = lf.get("Name + Variations (from Product SKU)", [])
                    name = name_list[0] if isinstance(name_list, list) and name_list else (name_list or "")
                    line_items.append({
                        "record_id":  li_id,
                        "name":       name,
                        "qty":        lf.get("Qty.", 1),
                        "unit_price": lf.get("Confirmed Unit Price", 0),
                        "picked_up":  lf.get("Picked Up", False),
                    })
            except Exception:
                pass
        order["line_items"] = line_items
        return Response(json.dumps(order), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


# ─────────────────────────────────────────────────────────────────────────────
# ANNIVERSARY AWARD PICKER
# Base: Automated Data (app3xt0dghBWnHxdN) — separate from ops base
# Tables created manually — set env vars after Airtable setup:
#   ANNIVERSARY_AWARDS_TABLE_ID        (Anniversary Awards table)
#   ANNIVERSARY_PARTICIPANTS_TABLE_ID  (Anniversary Participants table)
# Token: SO_TRACKING_WRITE_TOKEN (Railway) — used for both reads and writes
#        since patVc6ShozxQ6mrkp does not have access to the Automated Data base
# ─────────────────────────────────────────────────────────────────────────────

ANNIVERSARY_BASE_ID               = "app3xt0dghBWnHxdN"
ANNIVERSARY_AWARDS_TABLE_ID       = os.environ.get("ANNIVERSARY_AWARDS_TABLE_ID", "tbl_REPLACE_AWARDS")
ANNIVERSARY_PARTICIPANTS_TABLE_ID = os.environ.get("ANNIVERSARY_PARTICIPANTS_TABLE_ID", "tbl_REPLACE_PARTICIPANTS")
ANNIVERSARY_WRITE_TOKEN           = os.environ.get("SO_TRACKING_WRITE_TOKEN", "")

@app.route("/anniversary")
def anniversary_page():
    return send_from_directory("static", "anniversary.html")

@app.route("/anniversary/admin")
def anniversary_admin_page():
    return send_from_directory("static", "anniversary-admin.html")

@app.route("/api/anniversary/info")
def anniversary_info():
    c = cors()
    token = request.args.get("t", "").strip()
    if not token:
        return Response(json.dumps({"error": "Missing token"}),
                        status=400, headers=c, mimetype="application/json")

    read_token = ANNIVERSARY_WRITE_TOKEN

    # Look up participant by Token field
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{ANNIVERSARY_BASE_ID}/{ANNIVERSARY_PARTICIPANTS_TABLE_ID}",
            headers=at_headers(read_token),
            params={"filterByFormula": f"{{Token}}='{token}'", "maxRecords": 1},
            timeout=10,
        )
        records = r.json().get("records", [])
    except Exception as e:
        return Response(json.dumps({"error": str(e)}),
                        status=500, headers=c, mimetype="application/json")

    if not records:
        return Response(json.dumps({"error": "Invalid or expired link"}),
                        status=404, headers=c, mimetype="application/json")

    rec   = records[0]
    f     = rec.get("fields", {})
    first_name       = f.get("First Name", "")
    last_name        = f.get("Last Name", "")
    points_budget    = int(f.get("Points") or 0)
    already_submitted = bool(f.get("Submitted"))
    selections = None
    if already_submitted:
        sel_json = f.get("Selections JSON", "")
        if sel_json:
            try:
                selections = json.loads(sel_json)
            except Exception:
                selections = []

    # Fetch all active awards and filter client-side by budget
    try:
        award_records = at_get_all(
            ANNIVERSARY_AWARDS_TABLE_ID, read_token,
            fields=["Name", "Points", "Category", "Active", "Product URL", "Image"],
            formula="{Active}=TRUE()",
            base_id=ANNIVERSARY_BASE_ID,
        )
    except Exception as e:
        return Response(json.dumps({"error": f"Could not load awards: {str(e)}"}),
                        status=500, headers=c, mimetype="application/json")

    awards = []
    for ar in award_records:
        af  = ar.get("fields", {})
        pts = int(af.get("Points") or 0)
        if pts <= points_budget:
            # Extract image URL from Airtable attachment field
            img_url = ""
            img_field = af.get("Image", [])
            if isinstance(img_field, list) and img_field:
                att = img_field[0]
                # Prefer large thumbnail, fall back to full URL
                img_url = (att.get("thumbnails", {}).get("large", {}).get("url")
                           or att.get("url", ""))
            awards.append({
                "record_id":   ar["id"],
                "name":        af.get("Name", ""),
                "points":      pts,
                "category":    af.get("Category", ""),
                "product_url": af.get("Product URL", "") or "",
                "image_url":   img_url,
            })

    awards.sort(key=lambda x: (x["points"], x["name"]))

    resp = {
        "first_name":       first_name,
        "last_name":        last_name,
        "points_budget":    points_budget,
        "already_submitted": already_submitted,
        "awards":           awards,
    }
    if selections is not None:
        resp["selections"] = selections

    return Response(json.dumps(resp), headers=c, mimetype="application/json")


@app.route("/api/anniversary/submit", methods=["POST", "OPTIONS"])
def anniversary_submit():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(),
                        "Access-Control-Allow-Headers": "Content-Type",
                        "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data       = request.get_json() or {}
    token      = data.get("token", "").strip()
    selections = data.get("selections", [])   # [{record_id, name, points, qty}]

    if not token:
        return Response(json.dumps({"error": "Missing token"}),
                        status=400, headers=c, mimetype="application/json")
    if not selections:
        return Response(json.dumps({"error": "No selections provided"}),
                        status=400, headers=c, mimetype="application/json")

    read_token = ANNIVERSARY_WRITE_TOKEN

    # Look up participant
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{ANNIVERSARY_BASE_ID}/{ANNIVERSARY_PARTICIPANTS_TABLE_ID}",
            headers=at_headers(read_token),
            params={"filterByFormula": f"{{Token}}='{token}'", "maxRecords": 1},
            timeout=10,
        )
        records = r.json().get("records", [])
    except Exception as e:
        return Response(json.dumps({"error": str(e)}),
                        status=500, headers=c, mimetype="application/json")

    if not records:
        return Response(json.dumps({"error": "Invalid token"}),
                        status=404, headers=c, mimetype="application/json")

    rec           = records[0]
    f             = rec.get("fields", {})
    record_id     = rec["id"]
    points_budget = int(f.get("Points") or 0)

    if f.get("Submitted"):
        return Response(json.dumps({"error": "Already submitted"}),
                        status=409, headers=c, mimetype="application/json")

    # Validate total points <= budget
    total_pts = sum(int(s.get("points", 0)) * int(s.get("qty", 1)) for s in selections)
    if total_pts > points_budget:
        return Response(
            json.dumps({"error": f"Total ({total_pts} pts) exceeds budget ({points_budget} pts)"}),
            status=400, headers=c, mimetype="application/json")

    from datetime import datetime, timezone
    submitted_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    try:
        patch_r = req_lib.patch(
            f"https://api.airtable.com/v0/{ANNIVERSARY_BASE_ID}/{ANNIVERSARY_PARTICIPANTS_TABLE_ID}/{record_id}",
            headers={**at_headers(ANNIVERSARY_WRITE_TOKEN), "Content-Type": "application/json"},
            json={"fields": {
                "Submitted":      True,
                "Submitted At":   submitted_at,
                "Selections JSON": json.dumps(selections),
            }},
            timeout=10,
        )
        if patch_r.status_code not in (200, 201):
            return Response(json.dumps({"error": patch_r.text}),
                            status=500, headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}),
                        status=500, headers=c, mimetype="application/json")

    return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")


_ANNIVERSARY_SHEET_CSV = "https://docs.google.com/spreadsheets/d/1uQIpMb0PZLdCg7BZweIhsj4pvf9lrzWupTrTck8H-gU/export?format=csv&gid=727731834"

def _fetch_form_responses():
    """Fetch Google Form responses CSV and return list of dicts."""
    import csv, io
    try:
        r = req_lib.get(_ANNIVERSARY_SHEET_CSV, timeout=10)
        if not r.ok:
            return []
        reader = csv.DictReader(io.StringIO(r.text))
        rows = []
        for row in reader:
            # Normalize XXXL → 3XL
            size = row.get("What's your t-shirt size?", "").strip()
            if size.upper() in ("XXXL", "XXX-L"):
                size = "3XL"
            rows.append({
                "raw_name":  row.get("Your Name", "").strip(),
                "referral":  row.get("Who referred you to Blue Alpha?", "").strip(),
                "shirt":     size,
                "rsvp":      row.get("Can you make it to the party?", "").strip(),
                "timestamp": row.get("Timestamp", "").strip(),
            })
        return rows
    except Exception as e:
        print(f"[anniversary] form fetch error: {e}")
        return []

_NAME_ALIASES = {
    "melissa kuel-coe":   "melissa kuehl-coe",
    "michael newton":     "mike newton",
    "rebecca olmstead":   "becca olmstead",
}

def _fuzzy_match_name(raw, participants):
    """Return (participant, score) for the best fuzzy match, or (None, 0)."""
    import difflib
    raw_norm = _NAME_ALIASES.get(raw.lower().strip(), raw.lower().strip())
    best_p, best_score = None, 0.0
    for p in participants:
        full = f"{p['first_name']} {p['last_name']}".lower()
        score = difflib.SequenceMatcher(None, raw_norm, full).ratio()
        # Also try last, first order
        rev   = f"{p['last_name']} {p['first_name']}".lower()
        score = max(score, difflib.SequenceMatcher(None, raw_norm, rev).ratio())
        if score > best_score:
            best_score, best_p = score, p
    return (best_p, best_score)

@app.route("/api/anniversary/admin/data")
def anniversary_admin_data():
    c = cors()
    read_token = ANNIVERSARY_WRITE_TOKEN

    try:
        participant_records = at_get_all(
            ANNIVERSARY_PARTICIPANTS_TABLE_ID, read_token,
            fields=["First Name", "Last Name", "Points",
                    "Submitted", "Submitted At", "Selections JSON"],
            base_id=ANNIVERSARY_BASE_ID,
        )
    except Exception as e:
        return Response(json.dumps({"error": str(e)}),
                        status=500, headers=c, mimetype="application/json")

    participants  = []
    award_totals  = {}

    for rec in participant_records:
        f         = rec.get("fields", {})
        submitted = bool(f.get("Submitted"))
        sels      = []
        if submitted:
            sel_json = f.get("Selections JSON", "")
            if sel_json:
                try:
                    sels = json.loads(sel_json)
                    for s in sels:
                        nm  = s.get("name", "")
                        qty = int(s.get("qty", 1))
                        pts = int(s.get("points", 0))
                        if nm not in award_totals:
                            award_totals[nm] = {"qty": 0, "points": pts}
                        award_totals[nm]["qty"] += qty
                except Exception:
                    pass
        participants.append({
            "record_id":    rec["id"],
            "first_name":   f.get("First Name", ""),
            "last_name":    f.get("Last Name", ""),
            "points":       int(f.get("Points") or 0),
            "submitted":    submitted,
            "submitted_at": f.get("Submitted At", ""),
            "selections":   sels,
            "form":         None,  # filled in below
        })

    participants.sort(key=lambda x: (x["first_name"], x["last_name"]))

    # ── Merge Google Form responses ──────────────────────────────────────────
    form_rows   = _fetch_form_responses()
    matched_ids = set()
    unmatched   = []
    shirt_totals = {}

    for row in form_rows:
        p, score = _fuzzy_match_name(row["raw_name"], participants)
        if p and score >= 0.75:
            p["form"] = {
                "raw_name":  row["raw_name"],
                "shirt":     row["shirt"],
                "rsvp":      row["rsvp"],
                "referral":  row["referral"],
                "timestamp": row["timestamp"],
                "match_score": round(score, 2),
            }
            matched_ids.add(p["record_id"])
        else:
            unmatched.append(row)

        # Tally shirt sizes (all responses, matched or not)
        sz = row["shirt"]
        if sz:
            shirt_totals[sz] = shirt_totals.get(sz, 0) + 1

    # Sort shirt totals in standard size order
    size_order = ["XS","S","M","L","XL","2XL","3XL","4XL"]
    shirt_totals_sorted = {sz: shirt_totals[sz] for sz in size_order if sz in shirt_totals}
    # Any unexpected sizes at the end
    for sz in shirt_totals:
        if sz not in shirt_totals_sorted:
            shirt_totals_sorted[sz] = shirt_totals[sz]

    participants.sort(key=lambda x: (x["first_name"], x["last_name"]))

    # ── Full awards catalog ──────────────────────────────────────────────────
    awards_catalog = []
    try:
        award_records = at_get_all(
            ANNIVERSARY_AWARDS_TABLE_ID, read_token,
            fields=["Name", "Points", "Category", "Product URL", "Image", "Active", "Qty Ordered 6/23", "Qty Ordered 6/24", "Qty Ordered 6/25", "Qty Ordered 6/26"],
            base_id=ANNIVERSARY_BASE_ID,
        )
        for rec in award_records:
            f = rec.get("fields", {})
            if not f.get("Active", True):
                continue
            images = f.get("Image", [])
            img_url = images[0].get("url", "") if images else ""
            name = f.get("Name", "")
            awards_catalog.append({
                "record_id":      rec["id"],
                "name":           name,
                "points":         int(f.get("Points") or 0),
                "category":       f.get("Category", ""),
                "product_url":    f.get("Product URL") or "",
                "image_url":      img_url,
                "qty_needed":     award_totals.get(name, {}).get("qty", 0),
                "qty_ordered":    f.get("Qty Ordered 6/23", None),
                "qty_ordered_624": f.get("Qty Ordered 6/24", None),
                "qty_ordered_625": f.get("Qty Ordered 6/25", None),
                "qty_ordered_626": f.get("Qty Ordered 6/26", None),
            })
    except Exception as e:
        print(f"[anniversary_admin] awards catalog error: {e}")

    return Response(json.dumps({
        "participants":   participants,
        "award_totals":   award_totals,
        "awards_catalog": awards_catalog,
        "shirt_totals":   shirt_totals_sorted,
        "unmatched_form": unmatched,
    }), headers=c, mimetype="application/json")


@app.route("/api/anniversary/admin/qty-ordered", methods=["POST", "OPTIONS"])
def anniversary_admin_set_qty_ordered():
    """Save Qty Ordered or Qty Ordered 6/24 for an award back to Airtable.
    Expects JSON: { record_id, qty, field } where field is 'qty_ordered' or 'qty_ordered_624'.
    """
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data      = request.get_json(force=True) or {}
    record_id = data.get("record_id", "").strip()
    qty       = data.get("qty")
    field_key = data.get("field", "qty_ordered_624")

    if not record_id:
        return Response(json.dumps({"error": "record_id required"}), status=400, headers=c, mimetype="application/json")

    field_map = {
        "qty_ordered":     "Qty Ordered 6/23",
        "qty_ordered_624": "Qty Ordered 6/24",
        "qty_ordered_625": "Qty Ordered 6/25",
        "qty_ordered_626": "Qty Ordered 6/26",
    }
    at_field = field_map.get(field_key)
    if not at_field:
        return Response(json.dumps({"error": "invalid field"}), status=400, headers=c, mimetype="application/json")

    write_token = os.environ.get("SO_TRACKING_WRITE_TOKEN", "")
    patch_val   = int(qty) if qty is not None and qty != "" else None
    try:
        r = req_lib.patch(
            f"https://api.airtable.com/v0/{ANNIVERSARY_BASE_ID}/{ANNIVERSARY_AWARDS_TABLE_ID}/{record_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {at_field: patch_val}},
            timeout=10,
        )
        if not r.ok:
            return Response(json.dumps({"error": f"Airtable error: {r.status_code}"}),
                            status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")
