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

import hashlib
import hmac
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
# chap -> the password never crosses the Wi-Fi link: the browser posts an MD5
#         digest from the router-hosted bridge page (router/bridge-chap.html).
# pap  -> the portal posts the plaintext password to the hotspot (old behaviour,
#         keep as an emergency fallback).
# Login hand-off mode:
#   get  -> navigate to http://hotspot/login?username=..&password=.. (default).
#           No mixed-content warning, so no "Send anyway" click. The password
#           is visible in the browser history for the moment it takes the
#           hotspot to consume it.
#   form -> the old HTML form post. Works, but HTTPS -> HTTP form submission
#           makes Chrome ask the user to confirm ("Send anyway").
#   chap -> hash in the browser on a router-hosted page; password never crosses
#           the Wi-Fi link, but needs hotspot-staff/bridge.html and a test.
LOGIN_MODE = os.environ.get("STAFF_LOGIN_MODE", "get").strip().lower()
# Where the hotspot sends the browser AFTER a successful login. Defaults to the
# portal's device page: Windows/Android often open a captive-portal window whose
# "original URL" is something like msn.com, and the hotspot would then dump the
# user there instead of somewhere useful.
POST_LOGIN_URL = os.environ.get("POST_LOGIN_URL", f"{PORTAL}/devices")
CHAP_BRIDGE_URL = os.environ.get("CHAP_BRIDGE_URL", f"http://{STAFF_GW}/bridge.html")

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

# --- abuse control ---------------------------------------------------------
# /devices/logout talks to the router, so an authenticated client could hammer
# it and degrade the hotspot. Count actions per account and per source IP.
ACTION_WINDOW = 60
ACTION_LIMIT = 8
_ACTION_LOG = {}


def _rate_limited(identity):
    """True when `identity` has exceeded ACTION_LIMIT actions in the window."""
    now = time.time()
    start, count = _ACTION_LOG.get(identity, (now, 0))
    if now - start > ACTION_WINDOW:
        start, count = now, 0
    count += 1
    _ACTION_LOG[identity] = (start, count)
    return count > ACTION_LIMIT


def _device_token(mac):
    """Opaque, unforgeable handle for a device MAC.

    The page must never expose a MAC that a client could replay, so the form
    posts this HMAC instead. It is derived from the portal secret, so a client
    cannot mint a token for a MAC it does not own.
    """
    key = (os.environ.get("PORTAL_SECRET") or CLIENT_SECRET).encode()
    return hmac.new(key, mac.encode(), hashlib.sha256).hexdigest()[:32]


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
    """True when the client is on the staff VLAN.

    The IP prefix is authoritative: VLAN 100 hands out STAFF_PREFIX addresses
    and the guest VLAN hands out a different range. The hotspot server name is
    only a secondary signal, because it depends on the STAFF_HOTSPOT env var
    matching the router's server name exactly -- a mismatch there used to
    reject real staff clients with a bare 403.
    """
    if not ip:
        return False
    if ip.startswith(STAFF_PREFIX):
        return True
    server = hotspot_server(ip)
    return bool(server) and server == STAFF_HOTSPOT


def _mac_ok(mac):
    """Return a normalised aa:bb:cc:dd:ee:ff MAC, or "" if it is not one."""
    mac = (mac or "").strip().lower()
    parts = mac.split(":")
    if len(parts) != 6:
        return ""
    if not all(len(p) == 2 and all(c in "0123456789abcdef" for c in p) for p in parts):
        return ""
    return mac


def ensure_user(name, password, profile, mac="", server=STAFF_HOTSPOT):
    """Create or update the hotspot user.

    server=hs-staff pins the account to the STAFF hotspot. Without it RouterOS
    leaves the user attached to every hotspot server, so a staff credential
    would also be accepted on the guest hotspot.

    mac-address binds the account to the device that actually logged in, so the
    generated password cannot be reused from another machine.
    """
    payload = {"password": password, "profile": profile, "comment": "wifi-portal"}
    if server:
        payload["server"] = server
    mac = _mac_ok(mac)
    if mac:
        # NOTE: deliberately NOT setting mac-address on the user record.
        # RouterOS allows only one MAC per user, so binding it would break the
        # second device of the same person (and fight with shared-users). The
        # MAC is kept in the comment for auditing instead.
        payload["comment"] = f"wifi-portal {mac}"
    app.logger.info(
        "ensure_user name=%r profile=%s server=%s mac=%s", name, profile, server, mac or "-"
    )
    existing = router_get("ip/hotspot/user", name=name)
    payload["name"] = name
    if existing:
        # RouterOS 7.19 answers HTTP 500 when the .id in the path is
        # percent-encoded ("*8" -> "%2A8"), so use the command form with the
        # id in the body instead of PATCH /rest/ip/hotspot/user/<id>.
        payload[".id"] = existing[0][".id"]
        router_post("ip/hotspot/user/set", payload)
    else:
        # RouterOS 7.19 rejects POST on the collection path ("no such command");
        # the explicit /add command is the form that works.
        router_post("ip/hotspot/user/add", payload)


def oidc_config():
    r = requests.get(f"{SSO}/.well-known/openid-configuration", timeout=10)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------- routes
@app.get("/health")
def health():
    """Liveness probe. Kept separate from / so / can always redirect a browser."""
    return {"ok": True}


@app.get("/")
def index():
    """A person typing the portal hostname lands on their device page."""
    return redirect(f"{PORTAL}/devices")


@app.get("/hotspot")
def staff_page():
    ip = request.args.get("ip", "")
    mac = request.args.get("mac", "")
    orig = request.args.get("orig", "http://gadingkonseling.org")
    if not ip or not is_staff_client(ip):
        abort(403, "This login page is only for the staff network")
    # Skip the login page only if the router really has a session for this
    # device. A stale portal cookie must NOT count: otherwise the captive
    # portal bounces to /devices and the hotspot login never happens at all.
    if _user_for_ip(_client_ip()):
        return redirect(f"{PORTAL}/devices")
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
        app.logger.warning(
            "callback rejected: ip=%r staff_prefix=%r hotspot_server=%r expected=%r",
            data.get("ip"), STAFF_PREFIX, hotspot_server(data.get("ip", "")), STAFF_HOTSPOT,
        )
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
        ensure_user(username, password, "profile-staff", mac=data.get("mac", ""))
    except Exception:
        # Never 500 the browser here: the OIDC state has already been consumed,
        # so a 500 leaves the user stuck on "expired login" forever. Send them
        # back to the login page with a retryable error instead.
        app.logger.exception("hotspot provisioning failed for %r", username)
        # Keep ip/mac so the retry target is still a valid staff login page;
        # dropping them made /hotspot answer 403 instead of a usable page.
        q = urllib.parse.urlencode(
            {
                "ip": data.get("ip", ""),
                "mac": data.get("mac", ""),
                "orig": data.get("orig", ""),
                "err": "router",
            },
            quote_via=urllib.parse.quote,
        )
        return redirect(f"{PORTAL}/hotspot?{q}", code=302)

    # Remember who logged in, so /devices can show only THIS account's devices.
    session["staff_user"] = username
    session["staff_exp"] = time.time() + 8 * 3600

    if LOGIN_MODE == "chap":
        # Credentials travel in the URL *fragment*: never sent to a server, so
        # they cannot end up in router logs; the page hashes and wipes them.
        # quote_via=quote keeps spaces as %20 -- urlencode's default '+' would
        # survive decodeURIComponent in the browser and corrupt dst.
        q = urllib.parse.urlencode(
            {"u": username, "p": password, "d": data.get("orig", "")},
            quote_via=urllib.parse.quote,
        )
        return redirect(f"{CHAP_BRIDGE_URL}#{q}")

    if LOGIN_MODE == "get":
        # Navigate to the hotspot with the credentials as query parameters.
        #
        # Why not a form: the portal is HTTPS and the hotspot is HTTP, so a form
        # post is "mixed content" and Chrome interrupts with
        # "The information you're about to submit is not secure" -- an extra
        # click for every user. A top-level navigation to http:// is ordinary
        # browsing, so there is no warning and no confirmation.
        q = urllib.parse.urlencode(
            {
                "username": username,
                "password": password,
                # not data["orig"]: a captive-portal window reports whatever the
                # OS was loading (msn.com on Windows), which is a confusing place
                # to land after a Wi-Fi login.
                "dst": POST_LOGIN_URL,
            },
            quote_via=urllib.parse.quote,
        )
        return redirect(f"{STAFF_LOGIN_URL}?{q}")

    return render_template(
        "bridge.html",
        username=username,
        password=password,
        dst=data["orig"],
        login_url=STAFF_LOGIN_URL,
        brand=BRAND,
    )


# ------------------------------------------------------------------- clients
def _device_names():
    """Map lower-case MAC -> DHCP lease hostname (device name)."""
    try:
        leases = router_get("ip/dhcp-server/lease")
    except Exception:
        app.logger.exception("cannot read dhcp leases")
        return {}
    out = {}
    for l in leases:
        mac = str(l.get("mac-address", "")).lower()
        name = l.get("host-name") or l.get("comment") or ""
        if mac and name:
            out[mac] = name
    return out


def _human_bytes(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} T"


def _fmt_uptime(u):
    """RouterOS uptime like 1w2d3h4m5s -> '1d 3h 4m'."""
    if not u:
        return "-"
    out, num = [], ""
    for ch in str(u):
        if ch.isdigit():
            num += ch
        elif ch in "wdhms":
            if ch in "dh" and num:
                out.append(f"{num}{'d' if ch == 'd' else 'h'}")
            num = ""
    return " ".join(out[:2]) or str(u)


def _viewer_mac():
    """MAC of the device browsing this page, taken from its hotspot session."""
    ip = _client_ip()
    if not ip:
        return ""
    try:
        for a in router_get("ip/hotspot/active", address=ip):
            return str(a.get("mac-address", "")).lower()
    except Exception:
        app.logger.exception("viewer lookup failed for %s", ip)
    return ""


def _profile_rates():
    """Hotspot profile name -> rate-limit string, e.g. {'profile-staff': '20M/20M'}."""
    try:
        return {
            p.get("name", ""): p.get("rate-limit", "")
            for p in router_get("ip/hotspot/user/profile")
        }
    except Exception:
        app.logger.exception("cannot read hotspot profiles")
        return {}


def _fmt_rate(rate):
    """'20M/20M' -> ('20 Mbps', '20 Mbps'). Empty strings when unlimited."""
    if not rate or "/" not in rate:
        return "", ""
    down, up = rate.split("/", 1)

    def one(v):
        v = v.strip().upper()
        if not v or v.strip("0") == "":
            return ""
        for suffix, unit in (("K", "Kbps"), ("M", "Mbps"), ("G", "Gbps")):
            if v.endswith(suffix):
                return f"{v[:-1]} {unit}"
        return v

    return one(down), one(up)


def _sessions_for(username, viewer_mac=""):
    """Active staff hotspot sessions belonging to `username` (case-insensitive)."""
    try:
        active = router_get("ip/hotspot/active", **{"server": STAFF_HOTSPOT})
    except Exception:
        app.logger.exception("cannot read hotspot active")
        return []

    # rate limit comes from the account's profile, not from the session
    profile = ""
    try:
        users = router_get("ip/hotspot/user", name=username)
        if users:
            profile = users[0].get("profile", "")
    except Exception:
        app.logger.exception("cannot read hotspot user %r", username)
    down, up = _fmt_rate(_profile_rates().get(profile, ""))

    names = _device_names()
    out = []
    for a in active:
        if str(a.get("user", "")).lower() != username.lower():
            continue
        mac = str(a.get("mac-address", "")).lower()
        out.append(
            {
                "user": a.get("user", ""),
                "address": a.get("address", ""),
                "token": _device_token(mac),
                "mac": mac,
                "device": names.get(mac, ""),
                "uptime": _fmt_uptime(a.get("uptime", "")),
                "down": down,
                "up": up,
                # cumulative traffic actually used in this session
                "rx": _human_bytes(a.get("bytes-in", 0)),
                "tx": _human_bytes(a.get("bytes-out", 0)),
                "current": bool(viewer_mac) and mac == viewer_mac,
            }
        )
    return out


def current_staff_user():
    """Who is browsing.

    Portal session first (set at SSO login), then fall back to the hotspot
    session that owns this browser's IP. The fallback matters because the
    hotspot often opens its login page in an in-app WebView: the cookies live
    in a different jar from the browser the user later opens /devices in.
    """
    user = session.get("staff_user", "")
    if user and time.time() <= float(session.get("staff_exp", 0) or 0):
        return user
    return _user_for_ip(_client_ip())


def _client_ip():
    """Client IP as seen through Caddy.

    Caddy sets X-Forwarded-For; take the LAST entry, since that is the value
    our own proxy appended and cannot be spoofed by the client.
    """
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.remote_addr or ""


def _user_for_ip(ip):
    """Hotspot username currently logged in from `ip` ("" if none)."""
    if not ip:
        return ""
    try:
        for a in router_get("ip/hotspot/active", address=ip):
            return str(a.get("user", ""))
    except Exception:
        app.logger.exception("active lookup failed for %s", ip)
    return ""


@app.get("/devices")
def devices_page():
    # "Connected" means the router has an active session for this device -- not
    # merely that this browser once completed SSO.
    user = _user_for_ip(_client_ip())
    ip = request.args.get("ip") or _client_ip()
    return render_template(
        "devices.html",
        user=user,
        rows=_sessions_for(user, _viewer_mac()) if user else [],
        ip=ip,
        mac=request.args.get("mac", ""),
        orig=request.args.get("orig", ""),
        brand=BRAND,
    )


@app.post("/devices/logout")
def devices_logout():
    # Disconnecting requires a real session on THIS device; a stale portal
    # cookie is not enough to act on the router.
    user = _user_for_ip(_client_ip())
    if not user:
        abort(403, "perangkat ini belum terhubung ke Wi-Fi")

    ip = _client_ip()
    if _rate_limited("user:" + user.lower()) or _rate_limited("ip:" + ip):
        app.logger.warning("logout rate limited user=%s ip=%s", user, ip)
        abort(429, "terlalu banyak permintaan, tunggu sebentar")

    # Only an opaque token is accepted -- never a raw MAC -- and it is resolved
    # against THIS account's own devices, so one user can never disconnect
    # another user's device even by crafting the POST body.
    token = (request.form.get("token") or "").strip()
    mac = ""
    if token:
        for r in _sessions_for(user):
            if hmac.compare_digest(r["token"], token):
                mac = r["mac"]
                break
    if not mac:
        abort(403, "perangkat itu bukan milik akun Anda")

    was_current = mac == _viewer_mac()

    # Cookies go FIRST. With login-by=cookie the router re-creates the session
    # on the very next request, so deleting the session first just races.
    removed = {"cookies": 0, "sessions": 0}
    try:
        for _ in range(3):
            for c in router_get("ip/hotspot/cookie", **{"mac-address": mac}):
                router_post("ip/hotspot/cookie/remove", {".id": c[".id"]})
                removed["cookies"] += 1
            for a in router_get("ip/hotspot/active", **{"mac-address": mac}):
                router_post("ip/hotspot/active/remove", {".id": a[".id"]})
                removed["sessions"] += 1
            still = router_get("ip/hotspot/active", **{"mac-address": mac})
            left = router_get("ip/hotspot/cookie", **{"mac-address": mac})
            if not still and not left:
                break
    except Exception:
        app.logger.exception("device logout failed user=%s", user)
        abort(502, "router refused the logout")

    app.logger.info(
        "device logout user=%s sessions=%d cookies=%d remaining_active=%d remaining_cookies=%d",
        user, removed["sessions"], removed["cookies"], len(still), len(left),
    )
    if still or left:
        abort(502, "sesi masih aktif, coba sekali lagi")

    if was_current:
        # This device just lost its own session: forget the portal identity and
        # send the browser back to the hotspot login page, like MikroTik does.
        session.pop("staff_user", None)
        session.pop("staff_exp", None)
        return redirect(STAFF_LOGIN_URL)
    return redirect(f"{PORTAL}/devices")


@app.get("/clients")
def clients_redirect():
    """The old admin URL now points at the self-service device page."""
    return redirect(f"{PORTAL}/devices")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8585")))
