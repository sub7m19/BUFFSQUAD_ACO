import html
import imaplib
import ipaddress
import json
import re
import secrets
import socket
import subprocess
import time
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import aycd
import config
import db

GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993
ICLOUD_IMAP_HOST = "imap.mail.me.com"
ICLOUD_IMAP_PORT = 993
ICLOUD_DOMAINS = {"icloud.com", "me.com", "mac.com"}

# From AYCD's official Inbox API client (verify_user_api_key).
AYCD_VERIFY_URL = "https://inbox-api.aycd.io/api/v1/user/verify"

DISCORD_API = "https://discord.com/api/v10"


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=config.ROOT, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _version() -> dict:
    """The git commit this copy of the code is at. Admins see it in the sidebar as
    "v<number>", the number going up by one with every update pushed."""
    commit, _, rest = _git("log", "-1", "--format=%h%n%ct%n%s").partition("\n")
    at, _, subject = rest.partition("\n")
    if not commit:
        return {}
    return {"commit": commit, "number": int(_git("rev-list", "--count", "HEAD") or 0),
            "at": int(at or 0) * 1000, "subject": subject}


VERSION_INFO = _version()
# The commit id alone, for /api/health and the deploy log.
VERSION = VERSION_INFO.get("commit", "")
SESSION_COOKIE = "bs_session"
OAUTH_COOKIE = "bs_oauth"

# Only these file types are served, and never from these folders, so the database,
# .env, encryption key and Python source stay private.
PUBLIC_EXT = {".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".webp", ".woff2"}
PRIVATE_DIRS = {"data", "tests", ".venv", ".idea", ".git", ".claude", "__pycache__"}
ADMIN_FILES = {"/admin.html", "/admin.js", "/admin.css"}
# This site's own .css and .js links in a page (not https:// ones), for send_html.
ASSET_LINK = re.compile(r'\b(href|src)="((?![a-z]+:|/)[^"?#]+\.(?:css|js))"')
# Only for signed-in members; anyone else goes straight to Discord sign-in.
MEMBER_PAGES = {"/dashboard_proto_type.html"}


def check_gmail(email: str, password: str) -> tuple[bool, str]:
    """Sign in to Gmail over IMAP and sign straight back out. Nothing is stored."""
    try:
        with imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, GMAIL_IMAP_PORT, timeout=15) as imap:
            imap.login(email, password)
            imap.logout()
        return True, "Connected to Gmail."
    except imaplib.IMAP4.error:
        return False, "Gmail didn't accept that email and app password."
    except (OSError, socket.timeout):
        return False, "Couldn't reach Gmail. Try again in a moment."


def check_icloud(email: str, password: str) -> tuple[bool, str]:
    """Sign in to iCloud Mail over IMAP and straight back out. Nothing is stored.
    Apple's sign-in name is usually just the part before @ for iCloud addresses,
    and sometimes the full address, so both are tried."""
    local, _, domain = email.partition("@")
    names = [email] + ([local] if domain.lower() in ICLOUD_DOMAINS else [])
    for name in names:
        try:
            with imaplib.IMAP4_SSL(ICLOUD_IMAP_HOST, ICLOUD_IMAP_PORT, timeout=15) as imap:
                imap.login(name, password)
                imap.logout()
            return True, "Connected to iCloud Mail."
        except imaplib.IMAP4.error:
            continue
        except (OSError, socket.timeout):
            return False, "Couldn't reach iCloud Mail. Try again in a moment."
    return False, "iCloud didn't accept that email and app-specific password."


def check_aycd(key: str) -> tuple[bool, str]:
    """Ask AYCD whether the Inbox API key works. AYCD answers 401 for a bad key or when Inbox isn't open."""
    req = Request(AYCD_VERIFY_URL, headers={"Authorization": f"Token {key}"}, method="GET")
    try:
        with urlopen(req, timeout=15):
            return True, "Connected to AYCD Inbox."
    except HTTPError as e:
        if e.code == 401:
            return False, "AYCD didn't accept that key. Check the key, and make sure the AYCD Inbox app is open."
        if e.code == 429:
            return False, "AYCD is busy right now. Try again in a minute."
        return False, f"AYCD returned an error ({e.code}). Try again in a moment."
    except (URLError, OSError, socket.timeout):
        return False, "Couldn't reach AYCD. Try again in a moment."


# Discord sign-in

def discord_request(url: str, *, data: dict | None = None, token: str = "", bot: bool = False):
    headers = {"User-Agent": "BUFFSQUAD-ACO (https://github.com/sub7m19/BUFFSQUAD_ACO, 1.0)"}
    body = None
    if data is not None:
        body = urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"{'Bot' if bot else 'Bearer'} {token}"
    with urlopen(Request(url, data=body, headers=headers), timeout=15) as res:
        return json.loads(res.read())


def in_guild(discord_id: str) -> bool:
    """Whether the member is in the BUFFSQUAD server. Skipped when no server is configured."""
    if not (config.DISCORD_GUILD_ID and config.DISCORD_BOT_TOKEN):
        return True
    try:
        discord_request(f"{DISCORD_API}/guilds/{config.DISCORD_GUILD_ID}/members/{discord_id}",
                        token=config.DISCORD_BOT_TOKEN, bot=True)
        return True
    except HTTPError as e:
        if e.code == 404:
            return False
        raise


def safe_next(value: str) -> str:
    """Only same-site paths, so the login can't bounce members to another site."""
    return value if value.startswith("/") and not value.startswith("//") and "\\" not in value else "/dashboard"


def is_owner(user) -> bool:
    """Owners (OWNER_DISCORD_IDS) see the master log; other admins don't."""
    return user["discord_id"] in config.OWNER_DISCORD_IDS


def can_open(admin, member) -> bool:
    """Whether an admin may see, reveal and download a member's profiles, cards,
    accounts and inbox. Another admin's are off limits, except to the owner."""
    return is_owner(admin) or not member["is_admin"] or member["id"] == admin["id"]


LOCKED = "Only Admin Profile owner can see their own profiles."


NEW_TAB = ' target="_blank" rel="noreferrer"'


class AppError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Handler(SimpleHTTPRequestHandler):
    server_version = "BUFFSQUAD"
    sys_version = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(config.ROOT), **kwargs)

    # Plumbing

    revalidate = False
    private_page = False

    def end_headers(self) -> None:
        if self.private_page:
            # Signed-in pages: never kept by the browser, so Back after signing out shows nothing.
            self.send_header("Cache-Control", "no-store")
        elif self.revalidate:
            self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        super().end_headers()

    @property
    def url(self):
        return urlparse(self.path)

    def cookies(self) -> SimpleCookie:
        return SimpleCookie(self.headers.get("Cookie", ""))

    def cookie(self, name: str) -> str:
        c = self.cookies().get(name)
        return c.value if c else ""

    @staticmethod
    def cookie_header(name: str, value: str, max_age: int) -> tuple[str, str]:
        secure = "; Secure" if config.PUBLIC_URL.startswith("https://") else ""
        return "Set-Cookie", f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}"

    # API and sign-in responses are held until the request's database changes are
    # committed (see dispatch). Sent any earlier, the browser's next request could
    # arrive before the commit: a new session not found yet, a save not visible yet.
    pending = None

    def respond(self, status: int, headers: list[tuple[str, str]], body: bytes = b"") -> None:
        if self.pending is not None:
            self.pending.append((status, headers, body))
            return
        self.send_response(status)
        for name, value in headers + [("Content-Length", str(len(body)))]:
            self.send_header(name, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, data) -> None:
        self.respond(status, [("Content-Type", "application/json"), ("Cache-Control", "no-store")], json.dumps(data).encode())

    def send_page(self, status: int, message: str, buttons: list[tuple[str, str, bool]] | None = None) -> None:
        """A plain page for sign-in problems, which happen in a full page load.
        buttons: (label, link, opens in a new tab); defaults to a link back to the splash page."""
        buttons = buttons or [("Back to BUFFSQUAD", "/index.html", False)]
        base = "display:inline-block;margin:6px;padding:12px 18px;border-radius:8px;text-decoration:none;font-weight:800;"
        looks = ["background:#23ced9;color:#081424", "border:1px solid rgba(255,255,255,.3);color:#fff"]
        links = "".join(
            f'<a href="{html.escape(href)}"{NEW_TAB if new_tab else ""} style="{base}{looks[min(i, 1)]}">{html.escape(label)}</a>'
            for i, (label, href, new_tab) in enumerate(buttons))
        body = (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
                f'<title>BUFFSQUAD ACO</title><body style="margin:0;display:grid;place-items:center;min-height:100vh;'
                f'background:#081424;color:#fff;font:600 16px Inter,Arial,sans-serif;text-align:center;padding:16px">'
                f'<div><p style="max-width:34rem">{html.escape(message)}</p><p>{links}</p></div>').encode()
        self.respond(status, [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")], body)

    def send_file(self, filename: str, text: str, content_type: str) -> None:
        self.respond(200, [("Content-Type", content_type), ("Content-Disposition", f'attachment; filename="{filename}"'),
                           ("Cache-Control", "no-store")], text.encode())

    def redirect(self, location: str, cookies: list[tuple[str, str, int]] = ()) -> None:
        self.respond(302, [("Location", location), ("Cache-Control", "no-store")]
                     + [self.cookie_header(name, value, age) for name, value, age in cookies])

    def body(self, limit: int = 4096):
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= limit:
            raise AppError(413 if length > limit else 400, "Bad request.")
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            raise AppError(400, "Bad request.")

    def from_this_machine(self) -> bool:
        """A request made on this computer. Requests through Cloudflare's tunnel (or any
        proxy) also arrive from 127.0.0.1, so anything carrying proxy headers doesn't count."""
        if any(self.headers.get(h) for h in ("CF-Connecting-IP", "CF-Ray", "X-Forwarded-For", "X-Real-IP")):
            return False
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def log_message(self, format, *args):  # noqa: A002 - matches the base class
        # Keep query strings (OAuth codes) out of the console.
        super().log_message(format, *[re.sub(r"\?\S*", "", a) if isinstance(a, str) else a for a in args])

    # Routing

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("HEAD")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method: str) -> None:
        path = self.url.path
        try:
            if path.startswith(("/api/", "/auth/")):
                if path.startswith("/api/") and method not in ("GET", "HEAD"):
                    self.check_same_origin()
                self.pending = []
                with db.transaction() as conn:
                    if path.startswith("/api/"):
                        self.api(conn, method, path)
                    else:
                        self.auth(conn, path)
                # Committed: now it's safe to answer.
                held, self.pending = self.pending, None
                for status, headers, body in held:
                    self.respond(status, headers, body)
            elif method in ("GET", "HEAD"):
                self.static(method)
            else:
                self.send_error(405)
        except AppError as e:
            self.pending = None  # rolled back; drop anything held
            if path.startswith("/auth/"):
                self.send_page(e.status, e.message)
            else:
                self.send_json(e.status, {"ok": False, "message": e.message})
        except (HTTPError, URLError, OSError) as e:
            self.pending = None
            self.log_error("Upstream error: %s", e)
            self.send_json(502, {"ok": False, "message": "Couldn't reach Discord. Try again in a moment."})

    def check_same_origin(self) -> None:
        """Blocks other sites from making changes with a member's cookie."""
        origin = self.headers.get("Origin")
        allowed = {self.headers.get("Host"), urlparse(config.PUBLIC_URL).netloc}
        if self.headers.get("Sec-Fetch-Site") == "cross-site" or (origin and urlparse(origin).netloc not in allowed):
            raise AppError(403, "Cross-site request blocked.")

    # Files

    def static(self, method: str) -> None:
        path = self.url.path
        if path in ("/dashboard", "/dashboard/"):
            path = self.path = "/dashboard_proto_type.html"
        if path == "/admin":
            path = self.path = "/admin.html"
        parts = [p for p in path.split("/") if p]
        target = config.ROOT.joinpath(*parts) if parts else config.ROOT / "index.html"
        private = any(p.startswith(".") or p in PRIVATE_DIRS for p in parts)
        if path != "/" and (private or target.suffix.lower() not in PUBLIC_EXT):
            self.send_error(404)
            return
        if path in ADMIN_FILES or path in MEMBER_PAGES:
            with db.transaction() as conn:
                user = self.current_user(conn)
            if not user:
                self.redirect("/auth/discord/login?" + urlencode({"next": "/admin" if path in ADMIN_FILES else "/dashboard"}))
                return
            if path in ADMIN_FILES and not user["is_admin"]:
                self.send_error(403)
                return
            self.private_page = True
        # Files change with each deploy: browsers check back (and get a 304 if unchanged).
        self.revalidate = True
        if target.suffix.lower() == ".html" and VERSION and target.is_file():
            self.send_html(target)
            return
        super().do_HEAD() if method == "HEAD" else super().do_GET()

    def send_html(self, target: Path) -> None:
        """A page with its own stylesheets and scripts linked as name.css?v=<commit>.
        Each deploy changes those links, so browsers and Cloudflare fetch the new
        files instead of serving copies they kept from before."""
        text = ASSET_LINK.sub(lambda m: f'{m.group(1)}="{m.group(2)}?v={VERSION}"', target.read_text(encoding="utf-8"))
        self.respond(200, [("Content-Type", "text/html; charset=utf-8")], text.encode())

    def list_directory(self, path):
        self.send_error(404)
        return None

    # Auth

    def current_user(self, conn):
        return db.session_user(conn, self.cookie(SESSION_COOKIE))

    def sign_in(self, conn, discord_id: str, username: str, display_name: str, avatar, next_path: str,
                admin: bool | None = None) -> None:
        is_admin = discord_id in config.ADMIN_DISCORD_IDS if admin is None else admin
        existing = conn.execute("SELECT disabled FROM users WHERE discord_id = ?", (discord_id,)).fetchone()
        if existing and existing["disabled"]:
            raise AppError(403, "This account has been turned off. Ask a BUFFSQUAD admin.")
        user = db.upsert_user(conn, discord_id, username, display_name, avatar, is_admin)
        db.audit(conn, user["id"], "login", username)
        token = db.create_session(conn, user["id"])
        self.redirect(next_path, [(SESSION_COOKIE, token, config.SESSION_DAYS * 86400), (OAUTH_COOKIE, "", 0)])

    def auth(self, conn, path: str) -> None:
        query = parse_qs(self.url.query)
        q = lambda name: (query.get(name) or [""])[0]  # noqa: E731

        if path == "/auth/discord/login":
            if not (config.DISCORD_CLIENT_ID and config.DISCORD_CLIENT_SECRET):
                raise AppError(503, "Discord sign-in isn't set up yet. Add DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET to .env.")
            state = secrets.token_urlsafe(24)
            next_path = safe_next(q("next"))
            params = {
                "client_id": config.DISCORD_CLIENT_ID, "redirect_uri": f"{config.PUBLIC_URL}/auth/discord/callback",
                "response_type": "code", "scope": "identify", "state": state, "prompt": "none",
            }
            self.redirect("https://discord.com/oauth2/authorize?" + urlencode(params),
                          [(OAUTH_COOKIE, f"{state}|{next_path}", 600)])

        elif path == "/auth/discord/callback":
            state, _, next_path = self.cookie(OAUTH_COOKIE).partition("|")
            if not state or not secrets.compare_digest(state, q("state")) or not q("code"):
                self.redirect("/index.html")
                return
            token = discord_request(f"{DISCORD_API}/oauth2/token", data={
                "client_id": config.DISCORD_CLIENT_ID, "client_secret": config.DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code", "code": q("code"),
                "redirect_uri": f"{config.PUBLIC_URL}/auth/discord/callback",
            })
            me = discord_request(f"{DISCORD_API}/users/@me", token=token["access_token"])
            discord_id = str(me["id"])
            if discord_id not in config.ADMIN_DISCORD_IDS and not in_guild(discord_id):
                # Invite opens in a new tab; this page stays open to sign in again after joining.
                self.send_page(403, "You need to be in the BUFFSQUAD Discord to use the dashboard. "
                                    "Join the server, then come back to this tab and sign in.", [
                    ("Join Discord", config.DISCORD_INVITE_URL, True),
                    ("I've joined, sign in", "/auth/discord/login?" + urlencode({"next": safe_next(next_path)}), False),
                ])
                return
            avatar = f"https://cdn.discordapp.com/avatars/{discord_id}/{me['avatar']}.png?size=64" if me.get("avatar") else None
            self.sign_in(conn, discord_id, me.get("username", ""), me.get("global_name") or me.get("username", ""),
                         avatar, safe_next(next_path))

        elif path == "/auth/dev" and config.DEV_LOGIN and self.from_this_machine():
            # Local testing without Discord: /auth/dev?user=alice&admin=1
            name = re.sub(r"[^a-z0-9_]", "", q("user").lower()) or "member"
            self.sign_in(conn, f"dev-{name}", name, name.title(), None, safe_next(q("next") or "/dashboard"),
                         admin=q("admin") == "1")

        elif path == "/auth/logout":
            db.end_session(conn, self.cookie(SESSION_COOKIE))
            self.redirect("/index.html", [(SESSION_COOKIE, "", 0)])

        else:
            self.send_error(404)

    # API

    def require_user(self, conn):
        user = self.current_user(conn)
        if not user:
            raise AppError(401, "Sign in first.")
        return user

    def require_admin(self, conn):
        user = self.require_user(conn)
        if not user["is_admin"]:
            raise AppError(403, "Admins only.")
        return user

    def api(self, conn, method: str, path: str) -> None:
        route = (method if method != "HEAD" else "GET", path)

        if route == ("GET", "/api/health"):
            # For the deploy script and uptime checks: is the site up, which version, is the bot alive.
            bot = db.kv_get(conn, "bot_status")
            error = db.kv_get(conn, "bot_error")
            # The version (a git commit id) is only shown on the server itself, for the deploy script.
            return self.send_json(200, {"ok": True, "version": VERSION if self.from_this_machine() else None, "bot": {
                "configured": bool(config.DISCORD_BOT_TOKEN), "lastSeen": bot["at"] if bot else None,
                "error": error["message"] if error else None}})

        if route == ("GET", "/api/version"):
            # Which update the site is on, for the label admins see on the dashboard and admin panel.
            self.require_admin(conn)
            return self.send_json(200, VERSION_INFO)

        if route == ("GET", "/api/me"):
            return self.send_json(200, member_payload(conn, self.require_user(conn)))

        if route == ("PUT", "/api/me/settings"):
            user = self.require_user(conn)
            try:
                settings = db.clean_settings(self.body(limit=512 * 1024))
            except ValueError:
                raise AppError(400, "Those settings couldn't be saved.")
            db.save_settings(conn, user, settings)
            return self.send_json(200, {"ok": True, "saved": db.saved_secrets(conn, user["id"])})

        if route == ("POST", "/api/me/secret"):
            user = self.require_user(conn)
            body = self.body()
            kind, ref, value = body.get("kind"), str(body.get("ref") or "")[:64], body.get("value")
            if kind not in ("card", "account", "inbox") or not ref:
                raise AppError(400, "Bad request.")
            if kind == "card" and value is not None:
                if not isinstance(value, dict) or not re.fullmatch(r"\d{12,19}", str(value.get("number", ""))):
                    raise AppError(400, "Check the card number.")
                value = {k: str(value.get(k, ""))[:64] for k in ("number", "cvv", "exp", "name")}
            elif value is not None:
                value = str(value)[:512]
            db.set_secret(conn, user["id"], kind, "inbox" if kind == "inbox" else ref, value)
            return self.send_json(200, {"ok": True})

        if route in (("POST", "/api/inbox/gmail/check"), ("POST", "/api/inbox/icloud/check"), ("POST", "/api/inbox/aycd/check")):
            self.require_user(conn)
            body = self.body()
            try:
                if path.endswith("/aycd/check"):
                    key = str(body["key"]).strip()
                    ok, message = check_aycd(key) if key else (False, "Paste your AYCD Inbox key.")
                else:
                    icloud = path.endswith("/icloud/check")
                    email = str(body["email"]).strip()
                    password = str(body["password"]).replace(" ", "")
                    if not (email and password):
                        ok, message = False, f"Enter your {'iCloud' if icloud else 'Gmail'} address and app password."
                    else:
                        ok, message = (check_icloud if icloud else check_gmail)(email, password)
            except (KeyError, TypeError):
                raise AppError(400, "Bad request.")
            return self.send_json(200, {"ok": ok, "message": message})

        if path.startswith("/api/admin/"):
            return admin_api(self, conn, route[0], path, self.require_admin(conn))

        raise AppError(404, "Not found.")


def member_payload(conn, user) -> dict:
    rows = conn.execute("SELECT * FROM checkouts WHERE user_id = ? ORDER BY created_at DESC, id DESC", (user["id"],)).fetchall()
    hits = [r for r in rows if r["result"] == "hit"]
    feed = [{
        "kind": "hit" if r["result"] == "hit" else "miss",
        "text": f"{'Checked out' if r['result'] == 'hit' else 'Missed'} {r['product'] or 'an item'}",
        "meta": f"{r['retailer']} · Qty {r['qty']}", "at": r["created_at"] * 1000,
    } for r in rows[:8]]
    return {
        "user": {"name": user["display_name"] or user["username"], "avatar": user["avatar"], "isAdmin": bool(user["is_admin"]),
                 "isOwner": is_owner(user)},
        "settings": db.user_settings(user),
        "saved": db.saved_secrets(conn, user["id"]),
        "checkouts": [db.checkout_json(r, member_view=True) for r in hits],
        "declined": len(rows) - len(hits),
        "feed": feed,
    }


# Admin API

def member_row(conn, r) -> dict:
    settings = json.loads(r["settings"]) if r["settings"] else {}
    stats = conn.execute(
        """SELECT COUNT(*) AS hits,
             COALESCE(SUM(CASE WHEN paid = 0 AND stage != 'canceled' THEN fee END), 0) AS due,
             COALESCE(SUM(CASE WHEN paid = 1 AND stage != 'canceled' THEN fee END), 0) AS paid
           FROM checkouts WHERE user_id = ? AND result = 'hit'""", (r["id"],)).fetchone()
    return {
        "id": r["id"], "discordId": r["discord_id"], "username": r["username"], "name": r["display_name"] or r["username"],
        "avatar": r["avatar"], "isAdmin": bool(r["is_admin"]), "disabled": bool(r["disabled"]),
        "createdAt": r["created_at"] * 1000, "lastLogin": (r["last_login"] or 0) * 1000,
        "profiles": len(settings.get("profiles", [])),
        "accounts": sum(len(v) for v in settings.get("accounts", {}).values() if isinstance(v, list)),
        "inbox": (settings.get("inbox") or {}).get("method"),
        "hits": stats["hits"], "due": stats["due"], "paid": stats["paid"],
    }


def _int(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        raise AppError(400, "Bad request.")


def checkout_filter(view: str, search: str) -> tuple[str, list]:
    """SQL WHERE clause for an admin checkouts view (hits, unmatched, due, misses, dm_failed) and search."""
    where = {
        "unmatched": "checkouts.user_id IS NULL AND checkouts.result = 'hit'",
        "misses": "checkouts.result = 'miss'",
        "due": "checkouts.result = 'hit' AND checkouts.paid = 0 AND checkouts.stage != 'canceled'",
        "dm_failed": "checkouts.dm_status = 'failed'",
    }.get(view, "checkouts.result = 'hit'")
    args = []
    if search:
        where += (" AND (checkouts.product LIKE ? OR checkouts.order_no LIKE ? OR checkouts.profile_name LIKE ?"
                  " OR users.username LIKE ? OR users.display_name LIKE ?)")
        args = [f"%{search}%"] * 5
    return where, args


CHECKOUT_SELECT = """SELECT checkouts.*, COALESCE(NULLIF(users.display_name, ''), users.username) AS member
                     FROM checkouts LEFT JOIN users ON users.id = checkouts.user_id"""


def channel_id_from(body: dict) -> str:
    """A Discord channel ID from a form. A pasted channel link (discord.com/channels/<server>/<channel>) works too."""
    raw = str(body.get("id") or "").strip().rstrip("/")
    channel_id = raw.rsplit("/", 1)[-1]
    if not re.fullmatch(r"\d{17,20}", channel_id):
        raise AppError(400, "That isn't a channel ID. In Discord, right-click the channel and pick Copy Channel ID.")
    return channel_id


# The owner's master log filters: which audit rows each choice shows.
AUDIT_KINDS = {
    "admin": "audit.action != 'login'",
    "downloads": "(audit.action LIKE 'export.%' OR audit.action LIKE 'reveal.%')",
    "changes": "audit.action != 'login' AND audit.action NOT LIKE 'export.%' AND audit.action NOT LIKE 'reveal.%'",
    "logins": "audit.action = 'login'",
    "all": "1 = 1",
}


def fee_text(kind: str, amount) -> str:
    return f"{amount:g}%" if kind == "percent" else f"${amount:.2f}" + (" per item" if kind == "per_item" else "")


def member_name(conn, user_id) -> str:
    row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone() if user_id else None
    return row["username"] if row else "unmatched"


def checkout_label(conn, checkout_id: int, row=None) -> str:
    """A checkout as the master log names it: #id, item, member, store and fee."""
    c = row or conn.execute("SELECT * FROM checkouts WHERE id = ?", (checkout_id,)).fetchone()
    if not c:
        return f"#{checkout_id}"
    return f"#{c['id']} {c['product'] or 'Unknown item'} ({member_name(conn, c['user_id'])}, {c['retailer'] or 'unknown store'}, fee ${c['fee']:.2f})"


def checkout_changes(conn, old, sets: dict) -> str:
    """What an edit changed, old to new, e.g. "fee $5.00 → $8.00, paid no → yes"."""
    def show(col, value):
        if col in ("fee", "price"):
            return f"${float(value or 0):.2f}"
        if col == "paid":
            return "yes" if value else "no"
        if col == "user_id":
            return member_name(conn, value)
        return str(value) if value not in (None, "") else "blank"

    labels = {"order_no": "order", "user_id": "member"}
    parts = []
    for col, new in sets.items():
        if col in ("fee_source", "dm_error"):
            continue
        if col == "dm_status":
            parts.append("sent the DM again" if new == "pending" else f"DM {new}")
        elif show(col, old[col]) != show(col, new):
            parts.append(f"{labels.get(col, col)} {show(col, old[col])} → {show(col, new)}")
    return ", ".join(parts) or "no changes"


def secret_label(conn, user_id, kind: str, ref: str) -> str:
    """Whose card, password or inbox key was revealed, by profile name or account email."""
    member = conn.execute("SELECT username, settings FROM users WHERE id = ?", (user_id,)).fetchone()
    if not member:
        return f"member {user_id} {ref}"
    settings = json.loads(member["settings"]) if member["settings"] else {}
    if kind == "card":
        p = next((p for p in settings.get("profiles", []) if str(p.get("id")) == ref), {})
        bot = conn.execute("SELECT bot_name FROM profiles WHERE user_id = ? AND ref = ?", (user_id, ref)).fetchone()
        return f"{member['username']}: card on profile {p.get('label') or ref}" + (f" ({bot['bot_name']})" if bot else "")
    if kind == "account":
        for store, accounts in (settings.get("accounts") or {}).items():
            a = next((a for a in accounts or [] if isinstance(a, dict) and str(a.get("id")) == ref), None)
            if a:
                return f"{member['username']}: {store} password for {a.get('email')}"
        return f"{member['username']}: account password {ref}"
    return f"{member['username']}: inbox key"


def admin_api(h: Handler, conn, method: str, path: str, admin) -> None:
    query = parse_qs(h.url.query)
    q = lambda name: (query.get(name) or [""])[0]  # noqa: E731
    m = re.fullmatch(r"/api/admin/(members|checkouts)/(\d+)", path)
    item_id = int(m.group(2)) if m else None

    if (method, path) == ("GET", "/api/admin/overview"):
        totals = conn.execute(
            """SELECT
                 COUNT(CASE WHEN result = 'hit' THEN 1 END) AS hits,
                 COUNT(CASE WHEN result = 'miss' THEN 1 END) AS misses,
                 COUNT(CASE WHEN result = 'hit' AND created_at > ? THEN 1 END) AS today,
                 COUNT(CASE WHEN result = 'hit' AND user_id IS NULL THEN 1 END) AS unmatched,
                 COALESCE(SUM(CASE WHEN result = 'hit' AND paid = 0 AND stage != 'canceled' THEN fee END), 0) AS due,
                 COALESCE(SUM(CASE WHEN result = 'hit' AND paid = 1 AND stage != 'canceled' THEN fee END), 0) AS paid,
                 COALESCE(SUM(CASE WHEN result = 'hit' AND stage != 'canceled' THEN price END), 0) AS value,
                 COUNT(CASE WHEN dm_status = 'failed' THEN 1 END) AS dm_failed
               FROM checkouts""", (db.now() - 86400,)).fetchone()
        members = conn.execute("SELECT COUNT(*) AS n FROM users WHERE disabled = 0").fetchone()["n"]
        recent = conn.execute(CHECKOUT_SELECT + " ORDER BY checkouts.created_at DESC, checkouts.id DESC LIMIT 8").fetchall()
        return h.send_json(200, {
            **dict(totals), "members": members, "bot": db.kv_get(conn, "bot_status"), "botError": db.kv_get(conn, "bot_error"),
            "recent": [db.checkout_json(r) for r in recent],
        })

    if (method, path) == ("GET", "/api/admin/members"):
        rows = conn.execute("SELECT * FROM users ORDER BY last_login DESC").fetchall()
        return h.send_json(200, [member_row(conn, r) for r in rows])

    if path.startswith("/api/admin/members/") and item_id is not None:
        r = conn.execute("SELECT * FROM users WHERE id = ?", (item_id,)).fetchone()
        if not r:
            raise AppError(404, "No such member.")
        if method == "GET":
            # Turning accounts off: never your own, and another admin's only by the owner.
            can_disable = r["id"] != admin["id"] and (not r["is_admin"] or is_owner(admin))
            if not can_open(admin, r):
                # Just who they are and their checkouts: no addresses, cards, accounts or inbox.
                checkouts = conn.execute(CHECKOUT_SELECT + " WHERE checkouts.user_id = ? ORDER BY checkouts.created_at DESC", (r["id"],)).fetchall()
                return h.send_json(200, {
                    **member_row(conn, r), "locked": LOCKED, "canDisable": can_disable, "settings": {},
                    "saved": {"cards": [], "accounts": [], "inbox": False}, "checkouts": [db.checkout_json(c) for c in checkouts],
                })
            settings = db.user_settings(r) or {}
            names = {p["ref"]: p["bot_name"] for p in conn.execute("SELECT ref, bot_name FROM profiles WHERE user_id = ?", (r["id"],))}
            for p in settings.get("profiles", []):
                p["botName"] = names.get(str(p.get("id")), "")
            checkouts = conn.execute(CHECKOUT_SELECT + " WHERE checkouts.user_id = ? ORDER BY checkouts.created_at DESC", (r["id"],)).fetchall()
            return h.send_json(200, {
                **member_row(conn, r), "canDisable": can_disable, "settings": settings, "saved": db.saved_secrets(conn, r["id"]),
                "checkouts": [db.checkout_json(c) for c in checkouts],
            })
        if method == "PATCH":
            body = h.body()
            if "disabled" in body:
                if r["id"] == admin["id"]:
                    raise AppError(400, "You can't turn off your own account.")
                if r["is_admin"] and not is_owner(admin):
                    db.audit(conn, admin["id"], "blocked.disable", f"tried to turn {'off' if body['disabled'] else 'on'} admin {r['username']}")
                    return h.send_json(403, {"ok": False, "message": "Only the owner can turn an admin's account off or on."})
                conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(bool(body["disabled"])), r["id"]))
                if body["disabled"]:
                    conn.execute("DELETE FROM sessions WHERE user_id = ?", (r["id"],))
                db.audit(conn, admin["id"], "member.disable" if body["disabled"] else "member.enable", r["username"])
            return h.send_json(200, {"ok": True})

    if (method, path) == ("POST", "/api/admin/reveal"):
        body = h.body()
        user_id, kind, ref = _int(body.get("userId")), body.get("kind"), str(body.get("ref") or "")
        if kind not in ("card", "account", "inbox"):
            raise AppError(400, "Bad request.")
        member = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if member and not can_open(admin, member):
            db.audit(conn, admin["id"], "blocked.reveal", f"tried to reveal {secret_label(conn, user_id, kind, ref)}")
            return h.send_json(403, {"ok": False, "message": LOCKED})
        value = db.get_secret(conn, user_id, kind, "inbox" if kind == "inbox" else ref)
        if value is None:
            raise AppError(404, "Nothing saved for that.")
        db.audit(conn, admin["id"], f"reveal.{kind}", secret_label(conn, user_id, kind, ref))
        return h.send_json(200, {"value": value})

    if (method, path) == ("GET", "/api/admin/checkouts"):
        where, args = checkout_filter(q("view"), q("q"))
        rows = conn.execute(f"{CHECKOUT_SELECT} WHERE {where} ORDER BY checkouts.created_at DESC, checkouts.id DESC LIMIT 500", args).fetchall()
        return h.send_json(200, [db.checkout_json(r) for r in rows])

    if (method, path) == ("POST", "/api/admin/checkouts/delete"):
        # Either the ticked checkouts (ids), or everything in a view and search (all),
        # which also covers rows past the 500 the list shows. dryRun only counts.
        body = h.body(limit=128 * 1024)
        if body.get("all"):
            where, args = checkout_filter(str(body.get("view") or ""), str(body.get("q") or ""))
            ids = [r["id"] for r in conn.execute(f"SELECT checkouts.id FROM checkouts LEFT JOIN users ON users.id = checkouts.user_id WHERE {where}", args)]
        else:
            try:
                ids = sorted({int(i) for i in body.get("ids") or []})
            except (TypeError, ValueError):
                raise AppError(400, "Bad request.")
            if len(ids) > 10000:
                raise AppError(400, "Too many at once. Use delete all in this view instead.")
            ids = [r["id"] for r in conn.execute(f"SELECT id FROM checkouts WHERE id IN ({','.join('?' * len(ids))})", ids)] if ids else []
        if body.get("dryRun"):
            return h.send_json(200, {"count": len(ids)})
        gone = [checkout_label(conn, i) for i in ids[:25]]
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            conn.execute(f"DELETE FROM checkouts WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        scope = "all in view " + (body.get("view") or "hits") + (f", search '{body.get('q')}'" if body.get("q") else "") if body.get("all") else "selected"
        more = f"; and {len(ids) - len(gone)} more" if len(ids) > len(gone) else ""
        db.audit(conn, admin["id"], "checkout.bulk_delete",
                 f"{len(ids)} checkout{'' if len(ids) == 1 else 's'} ({scope})" + (f": {'; '.join(gone)}{more}" if gone else ""))
        return h.send_json(200, {"ok": True, "deleted": len(ids)})

    if (method, path) == ("POST", "/api/admin/checkouts"):
        body = h.body()
        user_id = _int(body.get("userId"))
        if not body.get("product"):
            raise AppError(400, "Add the product name.")
        parsed = {
            "status": "success", "profile": str(body.get("profileName") or ""), "product": str(body["product"])[:200],
            "retailer": str(body.get("retailer") or "")[:60], "order": str(body.get("order") or "")[:80],
            "qty": body.get("qty") or 1, "price": body.get("price") or 0, "sku": str(body.get("sku") or "")[:80],
            "fee": float(body["fee"]) if body.get("fee") not in (None, "") else None,
        }
        new_id = db.record_checkout(conn, parsed, user_id=user_id, source="manual", notify=bool(body.get("notify")))
        db.audit(conn, admin["id"], "checkout.add", checkout_label(conn, new_id))
        return h.send_json(200, {"ok": True, "id": new_id})

    if path.startswith("/api/admin/checkouts/") and item_id is not None:
        c = conn.execute("SELECT * FROM checkouts WHERE id = ?", (item_id,)).fetchone()
        if not c:
            raise AppError(404, "No such checkout.")
        if method == "DELETE":
            db.audit(conn, admin["id"], "checkout.delete", checkout_label(conn, item_id, c))
            conn.execute("DELETE FROM checkouts WHERE id = ?", (item_id,))
            return h.send_json(200, {"ok": True})
        if method == "PATCH":
            body = h.body()
            sets = {}
            if body.get("stage") in db.STAGES:
                sets["stage"] = body["stage"]
            for key, col, cast in (("fee", "fee", float), ("price", "price", float), ("qty", "qty", int),
                                   ("product", "product", str), ("retailer", "retailer", str), ("order", "order_no", str),
                                   ("sku", "sku", str)):
                if key in body:
                    try:
                        sets[col] = cast(body[key])
                    except (TypeError, ValueError):
                        raise AppError(400, f"Check the {key}.")
            # A fee changed by hand stays put when fee rules are applied again.
            if "fee" in sets and round(sets["fee"], 2) != round(c["fee"], 2):
                sets["fee_source"] = "manual"
            if "paid" in body:
                sets["paid"] = int(bool(body["paid"]))
            if "userId" in body:
                sets["user_id"] = _int(body["userId"])
                # Assigning an unmatched checkout to a member sends their DM now.
                if sets["user_id"] and body.get("notify") and c["result"] == "hit":
                    sets["dm_status"] = "pending" if db.wants_checkout_dm(conn, sets["user_id"]) else "off"
            if body.get("resendDm") and c["user_id"]:
                sets["dm_status"], sets["dm_error"] = "pending", ""
            if sets:
                conn.execute(f"UPDATE checkouts SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", [*sets.values(), item_id])
                db.audit(conn, admin["id"], "checkout.edit", f"{checkout_label(conn, item_id, c)}: {checkout_changes(conn, c, sets)}")
            row = conn.execute(CHECKOUT_SELECT + " WHERE checkouts.id = ?", (item_id,)).fetchone()
            return h.send_json(200, db.checkout_json(row))

    if (method, path) == ("GET", "/api/admin/export/profiles"):
        retailer = q("retailer")
        with_cards = q("cards") == "1"
        # With userId, just that member's profiles (from their details on the Members tab).
        user_id = _int(q("userId"))
        if user_id is None:
            users = conn.execute("SELECT * FROM users WHERE disabled = 0 AND settings IS NOT NULL").fetchall()
        else:
            users = conn.execute("SELECT * FROM users WHERE id = ? AND settings IS NOT NULL", (user_id,)).fetchall()
            if users and not can_open(admin, users[0]):
                db.audit(conn, admin["id"], "blocked.export", f"tried to download admin {users[0]['username']}'s profiles")
                return h.send_json(403, {"ok": False, "message": LOCKED})
        # Downloading everyone's leaves out other admins (the owner gets them all).
        skipped = [u["username"] for u in users if not can_open(admin, u)]
        users = [u for u in users if can_open(admin, u)]
        out, taken = [], {}
        for u in users:
            settings = json.loads(u["settings"])
            names = {p["ref"]: p["bot_name"] for p in conn.execute("SELECT ref, bot_name FROM profiles WHERE user_id = ?", (u["id"],))}
            for p in settings.get("profiles", []):
                if not p.get("active", True) or (retailer and retailer not in p.get("sites", [])):
                    continue
                card = db.get_secret(conn, u["id"], "card", str(p.get("id"))) if with_cards else None
                bot_name = names.get(str(p.get("id")), p.get("label", ""))
                out.append(aycd.to_aycd(p, bot_name, card))
                taken.setdefault(u["username"], []).append(bot_name)
        who = f"{users[0]['username']}'s " if user_id is not None and users else ""
        db.audit(conn, admin["id"], "export.profiles",
                 f"{len(out)} {who}profiles, {retailer or 'all sites'}, cards {'on' if with_cards else 'off'}"
                 + (". " + "; ".join(f"{name}: {', '.join(bots)}" for name, bots in taken.items()) if taken else "")
                 + (f". Left out admins: {', '.join(skipped)}" if skipped else ""))
        stamp = time.strftime("%Y-%m-%d")
        parts = ["buffsquad", re.sub(r"[^a-z0-9]+", "-", users[0]["username"].lower()) if who else "",
                 retailer.lower().replace(" ", "-") if retailer else "", "profiles", stamp]
        return h.send_file("-".join(p for p in parts if p) + ".json", json.dumps(out, indent=2), "application/json")

    if (method, path) == ("GET", "/api/admin/export/accounts"):
        retailer = q("retailer")
        if retailer not in config.RETAILERS:
            raise AppError(400, "Pick a retailer.")
        lines, taken, skipped = [], {}, []
        for u in conn.execute("SELECT * FROM users WHERE disabled = 0 AND settings IS NOT NULL").fetchall():
            if not can_open(admin, u):
                skipped.append(u["username"])
                continue
            for a in json.loads(u["settings"]).get("accounts", {}).get(retailer, []):
                password = db.get_secret(conn, u["id"], "account", str(a.get("id")))
                if password:
                    lines.append(f"{a.get('email')}:{password}")
                    taken.setdefault(u["username"], []).append(str(a.get("email")))
        db.audit(conn, admin["id"], "export.accounts", f"{len(lines)} {retailer} accounts"
                 + (". " + "; ".join(f"{name}: {', '.join(emails)}" for name, emails in taken.items()) if taken else "")
                 + (f". Left out admins: {', '.join(skipped)}" if skipped else ""))
        slug = retailer.lower().replace(" ", "-")
        return h.send_file(f"buffsquad-{slug}-accounts.txt", "\r\n".join(lines) + "\r\n", "text/plain")

    if (method, path) == ("GET", "/api/admin/settings"):
        return h.send_json(200, {"fees": db.fees(conn), "feeTypes": db.fee_types(conn), "rules": db.fee_rules(conn),
                                 "retailers": config.RETAILERS})

    if (method, path) == ("PUT", "/api/admin/fee-rules"):
        body = h.body(limit=64 * 1024)
        raw = body.get("rules")
        if not isinstance(raw, list) or len(raw) > 200:
            raise AppError(400, "Bad request.")
        rules = []
        for i, r in enumerate(raw, 1):
            if not isinstance(r, dict):
                raise AppError(400, "Bad request.")
            text = str(r.get("text") or "").strip()[:100]
            if not text:
                raise AppError(400, f"Rule {i} needs a product name or SKU to match.")
            try:
                amount = round(float(r.get("amount")), 2)
            except (TypeError, ValueError):
                raise AppError(400, f"Rule {i} needs a fee amount.")
            kind = r.get("kind") if r.get("kind") in db.FEE_KINDS else "flat"
            if amount < 0 or (kind == "percent" and amount > 100):
                raise AppError(400, f"Rule {i}: the fee must be between 0 and {'100%' if kind == 'percent' else 'any amount'}.")
            rule_id = str(r.get("id") or "")
            if not re.fullmatch(r"[a-z0-9]{1,16}", rule_id):
                rule_id = secrets.token_hex(4)
            rules.append({"id": rule_id, "match": "sku" if r.get("match") == "sku" else "product", "text": text,
                          "retailer": str(r.get("retailer") or "")[:60], "kind": kind, "amount": amount})
        db.kv_set(conn, "fee_rules", rules)
        summary = "; ".join(f"{r['text']} ({r['retailer'] or 'any store'}): {fee_text(r['kind'], r['amount'])}" for r in rules)
        db.audit(conn, admin["id"], "settings.rules", f"{len(rules)} fee rule{'' if len(rules) == 1 else 's'}" + (f". {summary}" if summary else ""))
        return h.send_json(200, {"ok": True, "rules": rules})

    if (method, path) == ("POST", "/api/admin/fee-rules/apply"):
        # Recalculates unpaid checkouts with the saved rules. Paid, canceled and
        # hand-set fees are left alone. dryRun only counts what would change.
        dry_run = bool(h.body().get("dryRun"))
        rows = conn.execute(
            """SELECT * FROM checkouts WHERE result = 'hit' AND paid = 0 AND stage != 'canceled' AND fee_source != 'manual'""").fetchall()
        changes, relabels = [], []
        for c in rows:
            fee, source = db.compute_fee(conn, c["retailer"], c["product"], c["sku"], c["price"], c["qty"])
            if round(fee, 2) != round(c["fee"], 2):
                changes.append((fee, source, c["id"], c["fee"]))
            elif source != c["fee_source"]:
                relabels.append((fee, source, c["id"]))  # same fee, now credited to a rule
        if not dry_run:
            conn.executemany("UPDATE checkouts SET fee = ?, fee_source = ? WHERE id = ?", [ch[:3] for ch in changes] + relabels)
            db.audit(conn, admin["id"], "checkout.refee", f"{len(changes)} unpaid checkout{'' if len(changes) == 1 else 's'}")
        return h.send_json(200, {"changed": len(changes), "checked": len(rows),
                                 "difference": round(sum(ch[0] - ch[3] for ch in changes), 2)})

    if (method, path) == ("PUT", "/api/admin/settings"):
        body = h.body()
        try:
            fees = {str(k)[:60]: round(float(v), 2) for k, v in (body.get("fees") or {}).items()}
        except (TypeError, ValueError):
            raise AppError(400, "Fees must be numbers.")
        types = {k: "percent" if (body.get("feeTypes") or {}).get(k) == "percent" else "flat" for k in fees}
        for k, v in fees.items():
            if v < 0 or (types[k] == "percent" and v > 100):
                raise AppError(400, f"The {k if k != 'Other' else 'other retailers'} fee must be "
                                    f"{'between 0 and 100%' if types[k] == 'percent' else '$0 or more'}.")
        db.kv_set(conn, "fees", fees)
        db.kv_set(conn, "fee_types", types)
        db.audit(conn, admin["id"], "settings.fees", json.dumps({k: f"{v}%" if types[k] == "percent" else f"${v}" for k, v in fees.items()}))
        return h.send_json(200, {"ok": True, "fees": db.fees(conn), "feeTypes": db.fee_types(conn)})

    if (method, path) == ("GET", "/api/admin/channels"):
        counts = {r["channel_id"]: r["n"] for r in conn.execute(
            "SELECT channel_id, COUNT(*) AS n FROM checkouts WHERE channel_id IS NOT NULL GROUP BY channel_id")}
        channels = [{**c, "checkouts": counts.get(c["id"], 0)} for c in db.checkout_channels(conn)]
        return h.send_json(200, {"channels": channels, "status": db.kv_get(conn, "bot_channels", {}), "bot": db.kv_get(conn, "bot_status"),
                                 "feed": db.feed_channel(conn), "feedStatus": db.kv_get(conn, "bot_feed_channel", {})})

    if (method, path) == ("POST", "/api/admin/channels"):
        body = h.body()
        channel_id = channel_id_from(body)
        channels = db.checkout_channels(conn)
        if any(c["id"] == channel_id for c in channels):
            raise AppError(400, "That channel is already on the list.")
        feed = db.feed_channel(conn)
        if feed and feed["id"] == channel_id:
            raise AppError(400, "That's the success feed channel. Checkout webhooks need a channel of their own.")
        label = str(body.get("label") or "").strip()[:60]
        channels.append({"id": channel_id, "label": label, "addedAt": db.now() * 1000, "addedBy": admin["username"]})
        db.kv_set(conn, "checkout_channels", channels)
        db.audit(conn, admin["id"], "channel.add", f"{channel_id} {label}".strip())
        return h.send_json(200, {"ok": True})

    m_channel = re.fullmatch(r"/api/admin/channels/(\d{17,20})", path)
    if method == "DELETE" and m_channel:
        channels = db.checkout_channels(conn)
        kept = [c for c in channels if c["id"] != m_channel.group(1)]
        if len(kept) == len(channels):
            raise AppError(404, "That channel isn't on the list.")
        db.kv_set(conn, "checkout_channels", kept)
        db.audit(conn, admin["id"], "channel.remove", m_channel.group(1))
        return h.send_json(200, {"ok": True})

    if (method, path) == ("PUT", "/api/admin/feed-channel"):
        channel_id = channel_id_from(h.body())
        if any(c["id"] == channel_id for c in db.checkout_channels(conn)):
            raise AppError(400, "The bot reads checkout webhooks from that channel. Pick a different one for the feed.")
        db.kv_set(conn, "feed_channel", {"id": channel_id, "addedAt": db.now() * 1000, "addedBy": admin["username"]})
        db.audit(conn, admin["id"], "feed.set", channel_id)
        return h.send_json(200, {"ok": True})

    if (method, path) == ("DELETE", "/api/admin/feed-channel"):
        conn.execute("DELETE FROM kv WHERE key IN ('feed_channel', 'bot_feed_channel')")
        db.audit(conn, admin["id"], "feed.clear", "")
        return h.send_json(200, {"ok": True})

    if (method, path) == ("GET", "/api/admin/audit"):
        # The master log: owners only, so admins can't see what's recorded about them.
        if not is_owner(admin):
            raise AppError(403, "Only the owner can see the master log.")
        where, args = AUDIT_KINDS.get(q("kind"), AUDIT_KINDS["admin"]), []
        who = _int(q("who"))
        if who is not None:
            where += " AND audit.user_id = ?"
            args.append(who)
        rows = conn.execute(
            f"""SELECT audit.*, users.username, users.display_name, users.discord_id, users.is_admin
                FROM audit LEFT JOIN users ON users.id = audit.user_id WHERE {where} ORDER BY audit.id DESC LIMIT 500""", args).fetchall()
        # Everyone who's done something besides signing in, for the "who" filter.
        people = conn.execute(
            """SELECT DISTINCT users.id, COALESCE(NULLIF(users.display_name, ''), users.username) AS name
               FROM audit JOIN users ON users.id = audit.user_id WHERE audit.action != 'login' ORDER BY name""").fetchall()

        def role(r):
            if r["discord_id"] is None:
                return None
            return "owner" if r["discord_id"] in config.OWNER_DISCORD_IDS else "admin" if r["is_admin"] else "member"

        return h.send_json(200, {
            "rows": [{"at": r["at"] * 1000, "who": r["display_name"] or r["username"], "username": r["username"],
                      "role": role(r), "action": r["action"], "detail": r["detail"]} for r in rows],
            "people": [dict(p) for p in people]})

    raise AppError(404, "Not found.")


def make_server() -> ThreadingHTTPServer:
    db.init()
    db.fernet()
    server = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    print(f"BUFFSQUAD ACO site running at http://{config.HOST}:{config.PORT} (version {VERSION or 'unknown'})")
    if config.DEV_LOGIN:
        print(f"Dev sign-in is on: http://{config.HOST}:{config.PORT}/auth/dev?user=alice  (add &admin=1 for the admin panel)")
    return server


def main() -> None:
    make_server().serve_forever()


if __name__ == "__main__":
    main()
