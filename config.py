"""Settings for the web server and the Discord bot, read from the environment or a .env file."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"


def _load_dotenv() -> None:
    """Fill os.environ from a KEY=value file. Real environment variables win.
    On the server the file lives outside the code folder (BUFFSQUAD_ENV points at
    it) so deploys never touch it; locally it's ROOT/.env."""
    path = Path(os.environ.get("BUFFSQUAD_ENV") or ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_dotenv()


def _ids(name: str) -> set[str]:
    return {v.strip() for v in os.environ.get(name, "").split(",") if v.strip()}


HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
# Where members reach the site, e.g. https://buffsquad.example.com. Used for the
# Discord login redirect and to mark the session cookie Secure on HTTPS.
PUBLIC_URL = os.environ.get("PUBLIC_URL", f"http://{HOST}:{PORT}").rstrip("/")

DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "buffsquad.db"))
# Fernet key that encrypts cards, retailer passwords and inbox keys. If it's not
# set, one is generated into data/secret.key on first run. Lose it and the stored
# secrets can't be read back, so back it up.
SECRET_KEY = os.environ.get("BUFFSQUAD_SECRET_KEY", "")

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
# Only members of this server can sign in (needs the bot token, and the bot in the server).
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
# Invite link for the BUFFSQUAD server, shown to people who try to sign in before joining.
DISCORD_INVITE_URL = os.environ.get("DISCORD_INVITE_URL", "https://discord.gg/7SGtgcEq")
# Discord user IDs that get the admin panel.
ADMIN_DISCORD_IDS = _ids("ADMIN_DISCORD_IDS")
# Channels the bot reads checkout webhooks from.
CHECKOUT_CHANNEL_IDS = {int(v) for v in _ids("CHECKOUT_CHANNEL_IDS")}

# Local testing only: /auth/dev signs in without Discord, from this machine only.
DEV_LOGIN = os.environ.get("DEV_LOGIN") == "1"

SESSION_DAYS = 30
RETAILERS = ["Target", "Pokemon Center"]
