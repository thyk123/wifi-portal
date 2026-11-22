"""
Gading Wi-Fi portal — STAFF ONLY (Casdoor OIDC -> MikroTik hotspot login).

The guest hotspot does NOT use this service. It serves the router's own themed
login page (see ../mikrotik-guest-hotspot/).

Routes
  GET /                     health probe
  GET /hotspot              branded staff login page (one SSO button)
  GET /hotspot/sso          start Casdoor OIDC
  GET /callback/staff       Casdoor redirect target -> hotspot login form

Env (see docker-compose.yml)
  SSO_URL, CASDOOR_CLIENT_ID, CASDOOR_CLIENT_SECRET, PORTAL_URL,
  ROUTER_URL, ROUTER_USER, ROUTER_PASS,
  STAFF_PREFIX, STAFF_GW, STAFF_HOTSPOT,
  LOGO_URL, BRAND_NAME, SUPPORT_URL, TERMS_URL
"""

import os
import secrets
import time
import urllib.parse

import requests
from flask import Flask, abort, redirect, render_template, request, session

SSO = os.environ["SSO_URL"].rstrip("/")
CLIENT_ID = os.environ["CASDOOR_CLIENT_ID"]
CLIENT_SECRET = os.environ["CASDOOR_CLIENT_SECRET"]
PORTAL = os.environ["PORTAL_URL"].rstrip("/")
ROUTER = os.environ["ROUTER_URL"].strip().rstrip("/")
# The REST helpers below already prepend "/rest". If ROUTER_URL is given as
# https://router/rest the request becomes /rest/rest/... and RouterOS answers
# 400 {"detail":"no such command"}. Accept either form.
if ROUTER.endswith("/rest"):
    ROUTER = ROUTER[: -len("/rest")]
R_USER = os.environ["ROUTER_USER"]
R_PASS = os.environ["ROUTER_PASS"]

# Fail fast with a readable message instead of a cryptic error on first request
_missing = [k for k in ("SSO_URL", "CASDOOR_CLIENT_ID", "CASDOOR_CLIENT_SECRET",
                        "PORTAL_URL", "ROUTER_URL", "ROUTER_USER", "ROUTER_PASS")
            if not os.environ.get(k)]
if _missing:
    raise RuntimeError("missing required env vars: " + ", ".join(_missing))

STAFF_PREFIX = os.environ.get("STAFF_PREFIX", "10.77.10.")
STAFF_GW = os.environ.get("STAFF_GW", "10.77.10.1")
STAFF_HOTSPOT = os.environ.get("STAFF_HOTSPOT", "hs-staff")

# Where the browser posts the login form. Use https://hotspot.<domain>/login
# once the router has a cert for that name (see plan §2.11).
STAFF_LOGIN_URL = os.environ.get("STAFF_LOGIN_URL", f"http://{STAFF_GW}/login")

BRAND = {
    "name": os.environ.get("BRAND_NAME", "Gading Counseling & Empowerment Center"),
    "logo": os.environ.get("LOGO_URL", ""),
    "support": os.environ.get("SUPPORT_URL", "https://gadingkonseling.org"),
    "terms": os.environ.get("TERMS_URL", "https://gadingkonseling.org/kebijakan-privasi"),
}

app = Flask(__name__)

# The OIDC state must survive across gunicorn workers and restarts, so it is
# kept in a signed cookie instead of process memory. The key is derived from
# the Casdoor client secret, so every worker computes the same value.
app.secret_key = os.environ.get("PORTAL_SECRET") or (CLIENT_SECRET + "-portal-state")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=PORTAL.startswith("https"),
    SESSION_COOKIE_NAME="wifi_portal_state",
)
STATE_TTL = 600     # seconds a pending login stays valid


@app.after_request
def no_store(resp):
    """Login pages and callbacks must never be cached (Cloudflare included)."""
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


# ----------------------------------------------------------------- router API
def router_get(path, **params):
    r = requests.get(
        f"{ROUTER}/rest/{path}",
        params=params,
        auth=(R_USER, R_PASS),
        verify=False,
        timeout=8,
    )
    r.raise_for_status()
    return r.json()


def router_post(path, payload):
    r = requests.post(
        f"{ROUTER}/rest/{path}",
        json=payload,
        auth=(R_USER, R_PASS),
        verify=False,
        timeout=8,
    )
    if r.status_code >= 400:
        safe = {k: (v if k != "password" else "***") for k, v in payload.items()}
        raise RuntimeError(
            f"router POST {r.url} -> {r.status_code}: {r.text[:300]} payload={safe}"
        )
    return r.json() if r.text else {}


def router_patch(path, payload):
    r = requests.patch(
        f"{ROUTER}/rest/{path}",
        json=payload,
        auth=(R_USER, R_PASS),
        verify=False,
        timeout=8,
    )
    r.raise_for_status()
    return r.json() if r.text else {}


def hotspot_server(ip):
    """Name of the hotspot server that owns this client IP ("" if unknown)."""
    try:
        hosts = router_get("ip/hotspot/host", address=ip)
        if hosts:
            return str(hosts[0].get("server", ""))
    except Exception:  # router down / no host entry yet
        pass
    return ""


def is_staff_client(ip):
    server = hotspot_server(ip)
    if server:
        return server == STAFF_HOTSPOT
    return ip.startswith(STAFF_PREFIX)


def ensure_user(name, password, profile):
    app.logger.info("ensure_user name=%r profile=%s", name, profile)
    existing = router_get("ip/hotspot/user", name=name)
    if existing:
        uid = urllib.parse.quote(existing[0][".id"])
        router_patch(
            f"ip/hotspot/user/{uid}",
            {"password": password, "profile": profile, "comment": "wifi-portal"},
        )
    else:
        # RouterOS 7.19 rejects POST on the collection path ("no such command");
        # the explicit /add command is the form that works.
        router_post(
            "ip/hotspot/user/add",
            {
                "name": name,
                "password": password,
                "profile": profile,
                "comment": "wifi-portal",
            },
        )


def oidc_config():
    r = requests.get(f"{SSO}/.well-known/openid-configuration", timeout=10)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------- routes
@app.get("/")
def health():
    return {"ok": True}


@app.get("/hotspot")
def staff_page():
    ip = request.args.get("ip", "")
    mac = request.args.get("mac", "")
    orig = request.args.get("orig", "http://gadingkonseling.org")
    if not ip or not is_staff_client(ip):
        abort(403, "This login page is only for the staff network")
    return render_template(
        "landing.html",
        ip=ip,
        mac=mac,
        orig=orig,
        brand=BRAND,
    )


@app.get("/hotspot/sso")
def hotspot_sso():
    ip = request.args.get("ip", "")
    mac = request.args.get("mac", "")
    orig = request.args.get("orig", "http://gadingkonseling.org")
    if not ip or not is_staff_client(ip):
        abort(403, "SSO is only available on the staff network")

    state = secrets.token_urlsafe(24)
    session["pending"] = {
        "state": state,
        "ip": ip,
        "mac": mac,
        "orig": orig,
        "exp": time.time() + STATE_TTL,
    }
    cfg = oidc_config()
    query = urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": f"{PORTAL}/callback/staff",
            "scope": "openid profile email",
            "state": state,
        }
    )
    return redirect(f"{cfg['authorization_endpoint']}?{query}")


@app.get("/callback/staff")
def callback():
    state = request.args.get("state", "")
    code = request.args.get("code", "")
    data = session.pop("pending", None)
    if not data or data.get("state") != state or data["exp"] < time.time():
        abort(400, "expired login, please reconnect to the Wi-Fi")
    if not is_staff_client(data["ip"]):
        abort(403, "network mismatch, please reconnect to the staff Wi-Fi")

    cfg = oidc_config()
    token = requests.post(
        cfg["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{PORTAL}/callback/staff",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=10,
    )
    token.raise_for_status()
    access = token.json()["access_token"]

    info = requests.get(
        cfg["userinfo_endpoint"],
        headers={"Authorization": f"Bearer {access}"},
        timeout=10,
    )
    info.raise_for_status()
    user = info.json()
    # Casdoor: preferred_username is the login name; "name" is the display name
    # and may contain spaces, which RouterOS rejects as a hotspot user name.
    username = user.get("preferred_username") or user.get("name") or user.get("sub")
    if username:
        username = str(username).strip().replace(" ", ".")
    if not username:
        abort(400, "Casdoor returned no username")

    password = secrets.token_urlsafe(18)
    try:
        ensure_user(username, password, "profile-staff")
    except Exception:
        # Never 500 the browser here: the OIDC state has already been consumed,
        # so a 500 leaves the user stuck on "expired login" forever. Send them
        # back to the login page with a retryable error instead.
        app.logger.exception("hotspot provisioning failed for %r", username)
        return redirect(f"{PORTAL}/hotspot?err=router", code=302)
    return render_template(
        "bridge.html",
        username=username,
        password=password,
        dst=data["orig"],
        login_url=STAFF_LOGIN_URL,
        brand=BRAND,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8585")))
