"""SQLite storage shared by the web server and the Discord bot.

Members' dashboard settings (profiles, retailer account emails, preferences) are
kept as one JSON document per member, the same shape dashboard.js uses. Anything
secret (card number and CVV, retailer passwords, inbox keys) lives encrypted in
`secrets`, never in that document.
"""
import contextlib
import hashlib
import html
import json
import re
import secrets as tokens
import sqlite3
import time

from cryptography.fernet import Fernet, InvalidToken

import checkout_parser
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
  dm_error TEXT NOT NULL DEFAULT '',
  feed_status TEXT NOT NULL DEFAULT 'none'   -- none | pending | sent | failed: the post in the success feed channel
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
        for column, decl in (("sku", "TEXT NOT NULL DEFAULT ''"), ("fee_source", "TEXT NOT NULL DEFAULT ''"),
                             ("feed_status", "TEXT NOT NULL DEFAULT 'none'")):
            if column not in have:
                conn.execute(f"ALTER TABLE checkouts ADD COLUMN {column} {decl}")
        # After the columns above, so an old database has feed_status before it's indexed.
        conn.execute("CREATE INDEX IF NOT EXISTS checkouts_feed ON checkouts(feed_status)")
        # Checkouts saved before the parser decoded HTML codes and list bullets ("- 2x Pok&#233;mon").
        for row in conn.execute("SELECT id, product FROM checkouts WHERE product LIKE '%&%;%' OR product LIKE '- %'").fetchall():
            fixed = re.sub(r"^\s*[-•]\s+", "", html.unescape(row["product"]))
            if fixed != row["product"]:
                conn.execute("UPDATE checkouts SET product = ? WHERE id = ?", (fixed, row["id"]))
        # ...and before it read Polar's "2x Pokémon ..." as quantity 2. Only rows still at qty 1,
        # so a quantity an admin set by hand stays put.
        for row in conn.execute("SELECT id, product FROM checkouts WHERE qty = 1 AND product GLOB '[0-9]*'").fetchall():
            qty, product = checkout_parser.split_qty(row["product"])
            if qty:
                conn.execute("UPDATE checkouts SET qty = ?, product = ? WHERE id = ?", (int(qty), product, row["id"]))
        rematch_checkouts(conn)
        if not kv_get(conn, "fixed_prices_v2"):
            fix_old_prices(conn)
            kv_set(conn, "fixed_prices_v2", now())
        # The admin flag is saved at sign-in; re-sync it with .env so removed admins lose access on restart.
        # Dev sign-ins (dev-*) pick their own flag and are left alone.
        for row in conn.execute("SELECT id, discord_id, is_admin FROM users WHERE discord_id NOT LIKE 'dev-%'").fetchall():
            is_admin = int(row["discord_id"] in config.ADMIN_DISCORD_IDS)
            if row["is_admin"] != is_admin:
                conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (is_admin, row["id"]))


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
    return {**{r: 0 for r in config.RETAILERS}, "Other": 0, **kv_get(conn, "fees", {})}


def fee_types(conn) -> dict:
    """How each retailer's default fee is charged: 'flat' ($ per checkout) or 'percent' (% of price)."""
    saved = kv_get(conn, "fee_types", {})
    return {r: saved.get(r, "flat") for r in fees(conn)}


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
    table, types = fees(conn), fee_types(conn)
    key = retailer if retailer in table else "Other"
    amount = float(table[key])
    if types.get(key) == "percent":
        return round((price or 0) * amount / 100, 2), "default"
    return amount, "default"


def checkout_channels(conn) -> list[dict]:
    """Discord channels the bot watches for checkout messages, managed on the admin
    panel's Channels tab. The first time, it starts from CHECKOUT_CHANNEL_IDS in .env."""
    channels = kv_get(conn, "checkout_channels")
    if channels is None:
        channels = [{"id": str(c), "label": "", "addedAt": now() * 1000, "addedBy": ".env"} for c in sorted(config.CHECKOUT_CHANNEL_IDS)]
        kv_set(conn, "checkout_channels", channels)
    return channels


def feed_channel(conn) -> dict | None:
    """The success feed channel: the bot reposts each checkout there with just the item,
    store, profile name and a tag, leaving out the order number, proxies and the rest
    of the AIO bot's webhook. Set on the admin panel's Channels tab."""
    return kv_get(conn, "feed_channel")


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
    # Checkouts that came in before these profiles were added.
    rematch_checkouts(conn)


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


def _base(text: str) -> str:
    """A profile name without the jig code bots add to the end:
    'Zaky1-@457-&57d' and 'Zaky1-!1de-%01ab' are both 'zaky1'."""
    return _norm(re.split(r"-[!@#$%^&*~]", text or "", maxsplit=1)[0])


def match_profile(conn, profile_name: str):
    """Find the member a checkout's profile name belongs to. Returns the profiles
    row or None. Tries the bot name first, then a profile label, then the label
    without its jig code, the last two only when exactly one active member has it."""
    key = _norm(profile_name)
    if not key:
        return None
    rows = conn.execute(
        "SELECT profiles.* FROM profiles JOIN users ON users.id = profiles.user_id WHERE users.disabled = 0").fetchall()
    for r in rows:
        if _norm(r["bot_name"]) == key:
            return r
    by_label = [r for r in rows if _norm(r["label"]) == key]
    if len(by_label) == 1:
        return by_label[0]
    base = _base(profile_name)
    by_base = [r for r in rows if base and _base(r["label"]) == base]
    return by_base[0] if len({r["user_id"] for r in by_base}) == 1 else None


def rematch_checkouts(conn) -> int:
    """Gives unmatched checkouts to their member where the profile name now matches,
    without DMing them (the feed post went out long ago). Returns how many."""
    count = 0
    for row in conn.execute("SELECT id, profile_name FROM checkouts WHERE user_id IS NULL AND profile_name != ''").fetchall():
        match = match_profile(conn, row["profile_name"])
        if match:
            conn.execute("UPDATE checkouts SET user_id = ? WHERE id = ?", (match["user_id"], row["id"]))
            count += 1
    return count


def wants_checkout_dm(conn, user_id) -> bool:
    row = conn.execute("SELECT settings FROM users WHERE id = ?", (user_id,)).fetchone()
    settings = json.loads(row["settings"]) if row and row["settings"] else {}
    return (settings.get("notify") or {}).get("checkout", True) is not False


def product_key(product: str) -> str:
    """The same item from every bot reads the same: Polar's ' - Default' and case,
    spaces and punctuation don't count."""
    return _norm(checkout_parser.trim_size(product or ""))


def item_price(conn, product: str, sku: str = ""):
    """What one of this item cost the last time a bot sent a price for it (Polar
    sends the order total, so it's the total over the quantity), or None."""
    key = product_key(product)
    if not key and not sku:
        return None
    for row in conn.execute("""SELECT product, sku, price, qty FROM checkouts WHERE result = 'hit' AND price > 0
                               ORDER BY created_at DESC LIMIT 500"""):
        if (sku and row["sku"] == sku) or (key and product_key(row["product"]) == key):
            return row["price"] / max(1, row["qty"])
    return None


def fill_prices(conn) -> int:
    """Prices unpaid checkouts that came in without one (Shikari sends none, Refract
    sometimes $0) from the item's last known price, and redoes their fee. Returns how many."""
    count = 0
    for c in conn.execute("""SELECT * FROM checkouts WHERE result = 'hit' AND paid = 0 AND stage != 'canceled'
                             AND (price IS NULL OR price = 0)""").fetchall():
        each = item_price(conn, c["product"], c["sku"])
        if not each:
            continue
        price = round(each * max(1, c["qty"]), 2)
        fee, fee_source = (c["fee"], c["fee_source"]) if c["fee_source"] == "manual" else             compute_fee(conn, c["retailer"], c["product"], c["sku"], price, c["qty"])
        conn.execute("UPDATE checkouts SET price = ?, fee = ?, fee_source = ? WHERE id = ?", (price, fee, fee_source, c["id"]))
        count += 1
    return count


def fix_old_prices(conn) -> None:
    """One time: checkouts logged before the bot read Refract's and Polar's formats.
    Refract's (no bot name or store, as it signs itself "Prism Technologies" and only
    names the store in its title) get Target, as its title said, and their per-item
    price times the quantity. Polar's lose the ' - Default' ending. Then missing prices
    are filled in. Fees are redone only on the unpaid checkouts this changes."""
    for c in conn.execute("SELECT id, product FROM checkouts WHERE product LIKE '% - Default'").fetchall():
        conn.execute("UPDATE checkouts SET product = ? WHERE id = ?", (checkout_parser.trim_size(c["product"]), c["id"]))
    # A missing store is saved blank (the panel shows "Unknown").
    for c in conn.execute("""SELECT * FROM checkouts WHERE source = '' AND retailer IN ('', 'Unknown')
                             AND message_key IS NOT NULL AND order_no GLOB '[0-9]*'""").fetchall():
        price = c["price"] or 0
        # The first run of this fix gave $0 ones the item's whole-order price already; only per-item ones need the quantity.
        each = next((r["price"] / max(1, r["qty"]) for r in conn.execute(
            "SELECT product, price, qty FROM checkouts WHERE source != '' AND price > 0 ORDER BY created_at DESC")
            if product_key(r["product"]) == product_key(c["product"])), None)
        if not (each and abs(each * c["qty"] - price) < 0.01):
            price = round(price * max(1, c["qty"]), 2)
        fee, fee_source = (c["fee"], c["fee_source"])
        if not c["paid"] and c["stage"] != "canceled" and c["fee_source"] != "manual" and c["result"] == "hit":
            fee, fee_source = compute_fee(conn, "Target", c["product"], c["sku"], price, c["qty"])
        conn.execute("UPDATE checkouts SET source = 'Refract', retailer = 'Target', price = ?, fee = ?, fee_source = ? WHERE id = ?",
                     (price, fee, fee_source, c["id"]))
    fill_prices(conn)


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
    if hit and not price:
        # No price from the bot: use what the same item cost on another bot's checkout.
        each = item_price(conn, product, sku)
        price = round(each * qty, 2) if each else 0
    fee = parsed.get("fee")
    if not hit:
        fee, fee_source = 0, ""
    elif fee is None:
        fee, fee_source = compute_fee(conn, retailer, product, sku, price, qty)
    else:
        fee_source = "manual"
    # Only checkouts the bot saw in a checkout channel go to the feed, not ones added by hand.
    feed = "pending" if hit and message_key and feed_channel(conn) else "none"
    dm = "none"
    if hit and user_id and notify:
        dm = "pending" if wants_checkout_dm(conn, user_id) else "off"
    cur = conn.execute(
        """INSERT INTO checkouts (user_id, profile_name, product, retailer, order_no, size, sku, qty, price, fee,
             fee_source, result, source, message_key, channel_id, created_at, dm_status, feed_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, profile_name, product, retailer, parsed.get("order") or "", parsed.get("size") or "", sku,
         qty, price, float(fee), fee_source, "hit" if hit else "miss", source or parsed.get("source") or "",
         message_key, channel_id, now(), dm, feed))
    if hit and price:
        # Earlier checkouts of this item that came in without a price get this one's.
        fill_prices(conn)
    return cur.lastrowid


def drop_recap(conn, start: int, end: int, top: int = 10) -> dict:
    """A drop's stats between two times (seconds): successful checkouts, items, and
    the members who checked out the most items. Canceled orders don't count."""
    rows = conn.execute(
        """SELECT checkouts.user_id, users.discord_id, COALESCE(NULLIF(users.display_name, ''), users.username) AS name,
                  COUNT(*) AS checkouts, SUM(checkouts.qty) AS items
           FROM checkouts LEFT JOIN users ON users.id = checkouts.user_id
           WHERE checkouts.result = 'hit' AND checkouts.stage != 'canceled' AND checkouts.created_at >= ? AND checkouts.created_at < ?
           GROUP BY checkouts.user_id ORDER BY items DESC, checkouts DESC""", (start, end)).fetchall()
    members = [{"name": r["name"], "discordId": r["discord_id"] if (r["discord_id"] or "").isdigit() else "",
                "checkouts": r["checkouts"], "items": r["items"]} for r in rows if r["user_id"]]
    unmatched = next(({"checkouts": r["checkouts"], "items": r["items"]} for r in rows if not r["user_id"]), None)
    return {"from": start * 1000, "to": end * 1000, "checkouts": sum(r["checkouts"] for r in rows),
            "items": sum(r["items"] for r in rows), "members": len(members), "top": members[:top], "unmatched": unmatched}


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
