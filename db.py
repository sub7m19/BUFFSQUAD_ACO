"""SQLite storage shared by the web server and the Discord bot.

Members' dashboard settings (profiles, retailer account emails, preferences) are
kept as one JSON document per member, the same shape dashboard.js uses. Anything
secret (card number and CVV, retailer passwords, inbox keys) lives encrypted in
`secrets`, never in that document.
"""
import contextlib
import hashlib
import json
import re
import secrets as tokens
import sqlite3
import time

from cryptography.fernet import Fernet, InvalidToken

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  discord_id TEXT NOT NULL UNIQUE,
  username TEXT NOT NULL DEFAULT '',
  display_name TEXT NOT NULL DEFAULT '',
  avatar TEXT,
  is_admin INTEGER NOT NULL DEFAULT 0,
  disabled INTEGER NOT NULL DEFAULT 0,
  settings TEXT,
  created_at INTEGER NOT NULL,
  last_login INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS secrets (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,          -- card | account | inbox
  ref TEXT NOT NULL,           -- profile id, account id, or 'inbox'
  value BLOB NOT NULL,         -- Fernet-encrypted JSON
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, kind, ref)
);
-- One row per dashboard profile, so a checkout's profile name can be matched to
-- its member. bot_name is the name the profile goes by in the checkout bots and
-- never changes once given, even if the member renames the profile.
CREATE TABLE IF NOT EXISTS profiles (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  ref TEXT NOT NULL,
  label TEXT NOT NULL,
  bot_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  PRIMARY KEY (user_id, ref)
);
CREATE TABLE IF NOT EXISTS checkouts (
  id INTEGER PRIMARY KEY,
  user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
  profile_name TEXT NOT NULL DEFAULT '',
  product TEXT NOT NULL DEFAULT '',
  retailer TEXT NOT NULL DEFAULT '',
  order_no TEXT NOT NULL DEFAULT '',
  size TEXT NOT NULL DEFAULT '',
  sku TEXT NOT NULL DEFAULT '',
  qty INTEGER NOT NULL DEFAULT 1,
  price REAL NOT NULL DEFAULT 0,
  fee REAL NOT NULL DEFAULT 0,
  fee_source TEXT NOT NULL DEFAULT '',       -- rule:<id> | default | manual
  paid INTEGER NOT NULL DEFAULT 0,
  stage TEXT NOT NULL DEFAULT 'confirmed',   -- confirmed | shipped | delivered | canceled
  result TEXT NOT NULL DEFAULT 'hit',        -- hit | miss
  source TEXT NOT NULL DEFAULT '',           -- checkout bot, or 'manual'
  message_key TEXT UNIQUE,                   -- Discord message id:embed index, for dedupe
  channel_id TEXT,
  created_at INTEGER NOT NULL,
  dm_status TEXT NOT NULL DEFAULT 'none',    -- none | pending | sent | failed | off
  dm_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS checkouts_user ON checkouts(user_id, created_at);
CREATE INDEX IF NOT EXISTS checkouts_dm ON checkouts(dm_status);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY,
  at INTEGER NOT NULL,
  user_id INTEGER,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
"""

STAGES = ("confirmed", "shipped", "delivered", "canceled")


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextlib.contextmanager
def transaction():
    """A connection that commits when the block finishes (rolls back on error) and then closes."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init() -> None:
    with transaction() as conn:
        conn.executescript(SCHEMA)
        # Columns added after the first release; CREATE TABLE IF NOT EXISTS skips old tables.
        have = {r["name"] for r in conn.execute("PRAGMA table_info(checkouts)")}
        for column, decl in (("sku", "TEXT NOT NULL DEFAULT ''"), ("fee_source", "TEXT NOT NULL DEFAULT ''")):
            if column not in have:
                conn.execute(f"ALTER TABLE checkouts ADD COLUMN {column} {decl}")


def now() -> int:
    return int(time.time())


# Encryption

_fernet: Fernet | None = None


def fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = config.SECRET_KEY.encode()
        if not key:
            path = config.DATA_DIR / "secret.key"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(Fernet.generate_key())
                print(f"Generated a new encryption key at {path}. Back it up: without it, saved cards and passwords can't be read.")
            key = path.read_bytes().strip()
        _fernet = Fernet(key)
    return _fernet


def encrypt(value) -> bytes:
    return fernet().encrypt(json.dumps(value).encode())


def decrypt(blob: bytes):
    try:
        return json.loads(fernet().decrypt(blob))
    except InvalidToken:
        return None


# Key-value settings

def kv_get(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(conn, key: str, value) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value)))


def fees(conn) -> dict:
    """Default ACO fee per retailer, set by admins. 'Other' covers anything else."""
    return {"Other": 0, **{r: 0 for r in config.RETAILERS}, **kv_get(conn, "fees", {})}


# Fee rules: set a checkout's fee by what was bought. Each rule is
#   {"id", "match": "product" | "sku", "text", "retailer": "" (any) | name,
#    "kind": "flat" | "per_item" | "percent", "amount"}
# and they're checked in order; the first that matches wins, otherwise the
# retailer default applies.
FEE_KINDS = ("flat", "per_item", "percent")


def fee_rules(conn) -> list[dict]:
    return kv_get(conn, "fee_rules", [])


def rule_matches(rule: dict, retailer: str, product: str, sku: str) -> bool:
    if rule.get("retailer") and rule["retailer"] != retailer:
        return False
    text = str(rule.get("text") or "").strip().lower()
    if not text:
        return False
    if rule.get("match") == "sku":
        return (sku or "").strip().lower() == text
    return text in (product or "").lower()


def compute_fee(conn, retailer: str, product: str, sku: str, price: float, qty: int, rules=None) -> tuple[float, str]:
    """The fee for a checkout and where it came from ('rule:<id>' or 'default')."""
    for rule in fee_rules(conn) if rules is None else rules:
        if rule_matches(rule, retailer, product, sku):
            amount = float(rule.get("amount") or 0)
            if rule.get("kind") == "percent":
                fee = (price or 0) * amount / 100
            elif rule.get("kind") == "per_item":
                fee = amount * max(1, qty or 1)
            else:
                fee = amount
            return round(fee, 2), f"rule:{rule.get('id')}"
    table = fees(conn)
    return float(table.get(retailer, table["Other"])), "default"


def checkout_channels(conn) -> list[dict]:
    """Discord channels the bot watches for checkout messages, managed on the admin
    panel's Channels tab. The first time, it starts from CHECKOUT_CHANNEL_IDS in .env."""
    channels = kv_get(conn, "checkout_channels")
    if channels is None:
        channels = [{"id": str(c), "label": "", "addedAt": now() * 1000, "addedBy": ".env"} for c in sorted(config.CHECKOUT_CHANNEL_IDS)]
        kv_set(conn, "checkout_channels", channels)
    return channels


def audit(conn, user_id, action: str, detail: str = "") -> None:
    conn.execute("INSERT INTO audit (at, user_id, action, detail) VALUES (?, ?, ?, ?)", (now(), user_id, action, detail))


# Users and sessions

def upsert_user(conn, discord_id: str, username: str, display_name: str, avatar, is_admin: bool) -> sqlite3.Row:
    conn.execute(
        """INSERT INTO users (discord_id, username, display_name, avatar, is_admin, created_at, last_login)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(discord_id) DO UPDATE SET username = excluded.username, display_name = excluded.display_name,
             avatar = excluded.avatar, is_admin = excluded.is_admin, last_login = excluded.last_login""",
        (discord_id, username, display_name, avatar, int(is_admin), now(), now()))
    return conn.execute("SELECT * FROM users WHERE discord_id = ?", (discord_id,)).fetchone()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn, user_id: int) -> str:
    token = tokens.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))
    conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                 (_hash(token), user_id, now() + config.SESSION_DAYS * 86400))
    return token


def session_user(conn, token: str):
    if not token:
        return None
    return conn.execute(
        """SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id
           WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.disabled = 0""",
        (_hash(token), now())).fetchone()


def end_session(conn, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))


# Member settings and secrets

SETTINGS_KEYS = {"accounts", "profiles", "discord", "notify", "inbox"}
# Never stored in the settings document, even if a client sends them.
SECRET_FIELDS = {"number", "cardNumber", "cvv", "password", "key", "secret"}


def _strip_secrets(value):
    if isinstance(value, dict):
        return {k: _strip_secrets(v) for k, v in value.items() if k not in SECRET_FIELDS}
    if isinstance(value, list):
        return [_strip_secrets(v) for v in value]
    return value


def clean_settings(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("settings must be an object")
    settings = _strip_secrets({k: v for k, v in raw.items() if k in SETTINGS_KEYS})
    if not isinstance(settings.get("profiles", []), list) or not isinstance(settings.get("accounts", {}), dict):
        raise ValueError("bad profiles or accounts")
    return settings


def user_settings(row) -> dict | None:
    return json.loads(row["settings"]) if row["settings"] else None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32] or "profile"


def save_settings(conn, user: sqlite3.Row, settings: dict) -> None:
    """Store the member's settings, keep the profiles table in step, and drop
    secrets whose profile or account was deleted."""
    conn.execute("UPDATE users SET settings = ? WHERE id = ?", (json.dumps(settings), user["id"]))

    profiles = [p for p in settings.get("profiles", []) if isinstance(p, dict) and p.get("id")]
    refs = {str(p["id"]) for p in profiles}
    existing = {r["ref"]: r for r in conn.execute("SELECT * FROM profiles WHERE user_id = ?", (user["id"],))}
    for p in profiles:
        ref, label = str(p["id"]), str(p.get("label") or "Profile")
        if ref in existing:
            conn.execute("UPDATE profiles SET label = ? WHERE user_id = ? AND ref = ?", (label, user["id"], ref))
            continue
        base = f"{_slug(user['username'] or user['display_name'])}-{_slug(label)}"
        name, n = base, 2
        while conn.execute("SELECT 1 FROM profiles WHERE bot_name = ?", (name,)).fetchone():
            name, n = f"{base}-{n}", n + 1
        conn.execute("INSERT INTO profiles (user_id, ref, label, bot_name) VALUES (?, ?, ?, ?)", (user["id"], ref, label, name))
    for ref in existing.keys() - refs:
        conn.execute("DELETE FROM profiles WHERE user_id = ? AND ref = ?", (user["id"], ref))

    account_refs = {str(a.get("id")) for accts in settings.get("accounts", {}).values() if isinstance(accts, list)
                    for a in accts if isinstance(a, dict)}
    for row in conn.execute("SELECT kind, ref FROM secrets WHERE user_id = ?", (user["id"],)).fetchall():
        gone = (row["kind"] == "card" and row["ref"] not in refs) or (row["kind"] == "account" and row["ref"] not in account_refs)
        if gone:
            conn.execute("DELETE FROM secrets WHERE user_id = ? AND kind = ? AND ref = ?", (user["id"], row["kind"], row["ref"]))
    if not (settings.get("inbox") or {}).get("method"):
        conn.execute("DELETE FROM secrets WHERE user_id = ? AND kind = 'inbox'", (user["id"],))


def set_secret(conn, user_id: int, kind: str, ref: str, value) -> None:
    if value is None:
        conn.execute("DELETE FROM secrets WHERE user_id = ? AND kind = ? AND ref = ?", (user_id, kind, ref))
        return
    conn.execute(
        """INSERT INTO secrets (user_id, kind, ref, value, updated_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id, kind, ref) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (user_id, kind, ref, encrypt(value), now()))


def get_secret(conn, user_id: int, kind: str, ref: str):
    row = conn.execute("SELECT value FROM secrets WHERE user_id = ? AND kind = ? AND ref = ?", (user_id, kind, ref)).fetchone()
    return decrypt(row["value"]) if row else None


def saved_secrets(conn, user_id: int) -> dict:
    """Which secrets a member has on file (ids only, never the values)."""
    rows = conn.execute("SELECT kind, ref FROM secrets WHERE user_id = ?", (user_id,)).fetchall()
    return {
        "cards": [r["ref"] for r in rows if r["kind"] == "card"],
        "accounts": [r["ref"] for r in rows if r["kind"] == "account"],
        "inbox": any(r["kind"] == "inbox" for r in rows),
    }


# Checkouts

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def match_profile(conn, profile_name: str):
    """Find the member a checkout's profile name belongs to. Returns the profiles
    row or None. Tries the bot name first, then a profile label, but only when
    exactly one active member uses that label."""
    key = _norm(profile_name)
    if not key:
        return None
    rows = conn.execute(
        "SELECT profiles.* FROM profiles JOIN users ON users.id = profiles.user_id WHERE users.disabled = 0").fetchall()
    for r in rows:
        if _norm(r["bot_name"]) == key:
            return r
    by_label = [r for r in rows if _norm(r["label"]) == key]
    return by_label[0] if len(by_label) == 1 else None


def wants_checkout_dm(conn, user_id) -> bool:
    row = conn.execute("SELECT settings FROM users WHERE id = ?", (user_id,)).fetchone()
    settings = json.loads(row["settings"]) if row and row["settings"] else {}
    return (settings.get("notify") or {}).get("checkout", True) is not False


def record_checkout(conn, parsed: dict, message_key: str | None = None, channel_id: str | None = None,
                    user_id=None, source: str = "", notify: bool = True):
    """Save a checkout (or a miss) from the bot or an admin. Matches the profile name
    to a member unless user_id is given, sets the fee from the fee rules (or the
    retailer default) unless one is given, and queues the
    member's DM. Returns the new checkout id, or None if this message was seen before."""
    if message_key and conn.execute("SELECT 1 FROM checkouts WHERE message_key = ?", (message_key,)).fetchone():
        return None
    profile_name = parsed.get("profile") or ""
    if user_id is None and profile_name:
        match = match_profile(conn, profile_name)
        user_id = match["user_id"] if match else None
    retailer = parsed.get("retailer") or ""
    hit = parsed.get("status", "success") == "success"
    product, sku = parsed.get("product") or "", parsed.get("sku") or ""
    qty, price = int(parsed.get("qty") or 1), float(parsed.get("price") or 0)
    fee = parsed.get("fee")
    if not hit:
        fee, fee_source = 0, ""
    elif fee is None:
        fee, fee_source = compute_fee(conn, retailer, product, sku, price, qty)
    else:
        fee_source = "manual"
    dm = "none"
    if hit and user_id and notify:
        dm = "pending" if wants_checkout_dm(conn, user_id) else "off"
    cur = conn.execute(
        """INSERT INTO checkouts (user_id, profile_name, product, retailer, order_no, size, sku, qty, price, fee,
             fee_source, result, source, message_key, channel_id, created_at, dm_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, profile_name, product, retailer, parsed.get("order") or "", parsed.get("size") or "", sku,
         qty, price, float(fee), fee_source, "hit" if hit else "miss", source or parsed.get("source") or "",
         message_key, channel_id, now(), dm))
    return cur.lastrowid


def checkout_json(row, member_view: bool = False) -> dict:
    """A checkout as the dashboards use it."""
    data = {
        "id": row["id"], "product": row["product"] or "Unknown item", "retailer": row["retailer"] or "Unknown",
        "order": row["order_no"], "date": time.strftime("%b %d", time.localtime(row["created_at"])).replace(" 0", " "),
        "qty": row["qty"], "price": row["price"], "fee": row["fee"], "paid": bool(row["paid"]), "stage": row["stage"],
        "at": row["created_at"] * 1000,
    }
    if not member_view:
        keys = row.keys()
        data.update({
            "userId": row["user_id"], "profileName": row["profile_name"], "size": row["size"], "result": row["result"],
            "sku": row["sku"], "feeSource": row["fee_source"],
            "source": row["source"], "dmStatus": row["dm_status"], "dmError": row["dm_error"],
            "member": row["member"] if "member" in keys else None,
        })
    return data
