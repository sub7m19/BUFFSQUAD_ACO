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

# From AYCD's official Inbox API client (verify_user_api_key).
AYCD_VERIFY_URL = "https://inbox-api.aycd.io/api/v1/user/verify"

DISCORD_API = "https://discord.com/api/v10"


def _version() -> str:
    """The git commit this copy of the code is at, shown by /api/health."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=config.ROOT, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


VERSION = _version()
SESSION_COOKIE = "bs_session"
OAUTH_COOKIE = "bs_oauth"

# Only these file types are served, and never from these folders, so the database,
# .env, encryption key and Python source stay private.
PUBLIC_EXT = {".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".webp", ".woff2"}
PRIVATE_DIRS = {"data", "tests", ".venv", ".idea", ".git", ".claude", "__pycache__"}
ADMIN_FILES = {"/admin.html", "/admin.js", "/admin.css"}


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

    def end_headers(self) -> None:
        if self.revalidate:
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

    def set_cookie(self, name: str, value: str, max_age: int) -> None:
        secure = "; Secure" if config.PUBLIC_URL.startswith("https://") else ""
        self.send_header("Set-Cookie", f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}")

    def send_json(self, status: int, data) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, filename: str, text: str, content_type: str) -> None:
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str, cookies: list[tuple[str, str, int]] = ()) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        for name, value, age in cookies:
            self.set_cookie(name, value, age)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
            if path.startswith("/api/"):
                if method not in ("GET", "HEAD"):
                    self.check_same_origin()
                with db.transaction() as conn:
                    self.api(conn, method, path)
            elif path.startswith("/auth/"):
                with db.transaction() as conn:
                    self.auth(conn, path)
            elif method in ("GET", "HEAD"):
                self.static(method)
            else:
                self.send_error(405)
        except AppError as e:
            if path.startswith("/auth/"):
                self.send_page(e.status, e.message)
            else:
                self.send_json(e.status, {"ok": False, "message": e.message})
        except (HTTPError, URLError, OSError) as e:
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
        if path in ADMIN_FILES:
            with db.transaction() as conn:
                user = self.current_user(conn)
            if not user:
                self.redirect("/auth/discord/login?" + urlencode({"next": "/admin"}))
                return
            if not user["is_admin"]:
                self.send_error(403)
                return
        # Files change with each deploy: browsers check back (and get a 304 if unchanged).
        self.revalidate = True
        super().do_HEAD() if method == "HEAD" else super().do_GET()

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

        if route in (("POST", "/api/inbox/gmail/check"), ("POST", "/api/inbox/aycd/check")):
            self.require_user(conn)
            body = self.body()
            try:
                if path.endswith("/aycd/check"):
                    key = str(body["key"]).strip()
                    ok, message = check_aycd(key) if key else (False, "Paste your AYCD Inbox key.")
                else:
                    email = str(body["email"]).strip()
                    password = str(body["password"]).replace(" ", "")
                    ok, message = check_gmail(email, password) if email and password else (False, "Enter your Gmail address and app password.")
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
        "user": {"name": user["display_name"] or user["username"], "avatar": user["avatar"], "isAdmin": bool(user["is_admin"])},
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


CHECKOUT_SELECT = """SELECT checkouts.*, COALESCE(NULLIF(users.display_name, ''), users.username) AS member
                     FROM checkouts LEFT JOIN users ON users.id = checkouts.user_id"""


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
            settings = db.user_settings(r) or {}
            names = {p["ref"]: p["bot_name"] for p in conn.execute("SELECT ref, bot_name FROM profiles WHERE user_id = ?", (r["id"],))}
            for p in settings.get("profiles", []):
                p["botName"] = names.get(str(p.get("id")), "")
            checkouts = conn.execute(CHECKOUT_SELECT + " WHERE checkouts.user_id = ? ORDER BY checkouts.created_at DESC", (r["id"],)).fetchall()
            return h.send_json(200, {
                **member_row(conn, r), "settings": settings, "saved": db.saved_secrets(conn, r["id"]),
                "checkouts": [db.checkout_json(c) for c in checkouts],
            })
        if method == "PATCH":
            body = h.body()
            if "disabled" in body:
                if r["id"] == admin["id"]:
                    raise AppError(400, "You can't turn off your own account.")
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
        value = db.get_secret(conn, user_id, kind, "inbox" if kind == "inbox" else ref)
        if value is None:
            raise AppError(404, "Nothing saved for that.")
        member = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        db.audit(conn, admin["id"], f"reveal.{kind}", f"{member['username'] if member else user_id} {ref}")
        return h.send_json(200, {"value": value})

    if (method, path) == ("GET", "/api/admin/checkouts"):
        where, args = ["1 = 1"], []
        view = q("view")
        if view == "unmatched":
            where.append("checkouts.user_id IS NULL AND checkouts.result = 'hit'")
        elif view == "misses":
            where.append("checkouts.result = 'miss'")
        elif view == "due":
            where.append("checkouts.result = 'hit' AND checkouts.paid = 0 AND checkouts.stage != 'canceled'")
        elif view == "dm_failed":
            where.append("checkouts.dm_status = 'failed'")
        else:
            where.append("checkouts.result = 'hit'")
        if q("q"):
            where.append("(checkouts.product LIKE ? OR checkouts.order_no LIKE ? OR checkouts.profile_name LIKE ? OR users.username LIKE ? OR users.display_name LIKE ?)")
            args += [f"%{q('q')}%"] * 5
        rows = conn.execute(f"{CHECKOUT_SELECT} WHERE {' AND '.join(where)} ORDER BY checkouts.created_at DESC, checkouts.id DESC LIMIT 500", args).fetchall()
        return h.send_json(200, [db.checkout_json(r) for r in rows])

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
        db.audit(conn, admin["id"], "checkout.add", f"#{new_id} {parsed['product']}")
        return h.send_json(200, {"ok": True, "id": new_id})

    if path.startswith("/api/admin/checkouts/") and item_id is not None:
        c = conn.execute("SELECT * FROM checkouts WHERE id = ?", (item_id,)).fetchone()
        if not c:
            raise AppError(404, "No such checkout.")
        if method == "DELETE":
            conn.execute("DELETE FROM checkouts WHERE id = ?", (item_id,))
            db.audit(conn, admin["id"], "checkout.delete", f"#{item_id} {c['product']}")
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
                db.audit(conn, admin["id"], "checkout.edit", f"#{item_id} {', '.join(sets)}")
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
        out = []
        for u in users:
            settings = json.loads(u["settings"])
            names = {p["ref"]: p["bot_name"] for p in conn.execute("SELECT ref, bot_name FROM profiles WHERE user_id = ?", (u["id"],))}
            for p in settings.get("profiles", []):
                if not p.get("active", True) or (retailer and retailer not in p.get("sites", [])):
                    continue
                card = db.get_secret(conn, u["id"], "card", str(p.get("id"))) if with_cards else None
                out.append(aycd.to_aycd(p, names.get(str(p.get("id")), p.get("label", "")), card))
        who = f"{users[0]['username']}'s " if user_id is not None and users else ""
        db.audit(conn, admin["id"], "export.profiles", f"{len(out)} {who}profiles, {retailer or 'all sites'}, cards {'on' if with_cards else 'off'}")
        stamp = time.strftime("%Y-%m-%d")
        parts = ["buffsquad", re.sub(r"[^a-z0-9]+", "-", users[0]["username"].lower()) if who else "",
                 retailer.lower().replace(" ", "-") if retailer else "", "profiles", stamp]
        return h.send_file("-".join(p for p in parts if p) + ".json", json.dumps(out, indent=2), "application/json")

    if (method, path) == ("GET", "/api/admin/export/accounts"):
        retailer = q("retailer")
        if retailer not in config.RETAILERS:
            raise AppError(400, "Pick a retailer.")
        lines = []
        for u in conn.execute("SELECT * FROM users WHERE disabled = 0 AND settings IS NOT NULL").fetchall():
            for a in json.loads(u["settings"]).get("accounts", {}).get(retailer, []):
                password = db.get_secret(conn, u["id"], "account", str(a.get("id")))
                if password:
                    lines.append(f"{a.get('email')}:{password}")
        db.audit(conn, admin["id"], "export.accounts", f"{len(lines)} {retailer} accounts")
        slug = retailer.lower().replace(" ", "-")
        return h.send_file(f"buffsquad-{slug}-accounts.txt", "\r\n".join(lines) + "\r\n", "text/plain")

    if (method, path) == ("GET", "/api/admin/settings"):
        return h.send_json(200, {"fees": db.fees(conn), "rules": db.fee_rules(conn), "retailers": config.RETAILERS})

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
        db.audit(conn, admin["id"], "settings.rules", f"{len(rules)} fee rule{'' if len(rules) == 1 else 's'}")
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
        db.kv_set(conn, "fees", fees)
        db.audit(conn, admin["id"], "settings.fees", json.dumps(fees))
        return h.send_json(200, {"ok": True, "fees": db.fees(conn)})

    if (method, path) == ("GET", "/api/admin/channels"):
        counts = {r["channel_id"]: r["n"] for r in conn.execute(
            "SELECT channel_id, COUNT(*) AS n FROM checkouts WHERE channel_id IS NOT NULL GROUP BY channel_id")}
        channels = [{**c, "checkouts": counts.get(c["id"], 0)} for c in db.checkout_channels(conn)]
        return h.send_json(200, {"channels": channels, "status": db.kv_get(conn, "bot_channels", {}), "bot": db.kv_get(conn, "bot_status")})

    if (method, path) == ("POST", "/api/admin/channels"):
        body = h.body()
        # Accept a pasted channel link (discord.com/channels/<server>/<channel>) as well as a bare ID.
        raw = str(body.get("id") or "").strip().rstrip("/")
        channel_id = raw.rsplit("/", 1)[-1]
        if not re.fullmatch(r"\d{17,20}", channel_id):
            raise AppError(400, "That isn't a channel ID. In Discord, right-click the channel and pick Copy Channel ID.")
        channels = db.checkout_channels(conn)
        if any(c["id"] == channel_id for c in channels):
            raise AppError(400, "That channel is already on the list.")
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

    if (method, path) == ("GET", "/api/admin/audit"):
        rows = conn.execute(
            """SELECT audit.*, users.username FROM audit LEFT JOIN users ON users.id = audit.user_id
               ORDER BY audit.id DESC LIMIT 300""").fetchall()
        return h.send_json(200, [{"at": r["at"] * 1000, "who": r["username"], "action": r["action"], "detail": r["detail"]} for r in rows])

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
