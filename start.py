"""Runs the website and the Discord bot together in one process, as the server does.

One process means one thing to start, stop and restart: on Windows, stopping a
parent doesn't stop its child processes, so running them separately would leave
an old copy holding the port after a deploy. If either half stops, the whole
process exits, and the deploy script's watchdog starts it again within a minute.

Logs go to the console, and also to LOG_DIR/app.log when LOG_DIR is set.
"""
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

import config
import db
import main


def setup_logging() -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    log_dir = os.environ.get("LOG_DIR")
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(Path(log_dir) / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers, force=True)

    # print() and the web server's request log write to stdout/stderr; send them to the log too.
    class ToLog:
        def __init__(self, level):
            self.level = level

        def write(self, text):
            for line in text.rstrip().splitlines():
                logging.getLogger("app").log(self.level, line)

        def flush(self):
            pass

    # The web server writes every request to stderr, so that's info, not a warning.
    sys.stdout, sys.stderr = ToLog(logging.INFO), ToLog(logging.INFO)


def run_web(server) -> None:
    try:
        server.serve_forever()
    except Exception:
        logging.exception("Website stopped")
    finally:
        os._exit(1)


def run() -> None:
    setup_logging()
    server = main.make_server()
    if not config.DISCORD_BOT_TOKEN:
        logging.warning("DISCORD_BOT_TOKEN isn't set, so only the website is running")
        run_web(server)
        return
    threading.Thread(target=run_web, args=(server,), daemon=True, name="web").start()
    import bot  # after logging is set up, so the bot's log lines land in the same place

    import discord

    try:
        bot.main(own_logging=False)
    except (discord.LoginFailure, discord.PrivilegedIntentsRequired) as e:
        # A settings problem that restarting won't fix: keep the website up and say what to change.
        fix = ("Check DISCORD_BOT_TOKEN in the settings file" if isinstance(e, discord.LoginFailure)
               else "Turn on Message Content Intent (Discord developer portal > your app > Bot)")
        logging.error("The bot couldn't sign in to Discord: %s. %s, then restart. The website keeps running.", e, fix)
        with db.transaction() as conn:
            db.kv_set(conn, "bot_error", {"at": db.now() * 1000, "message": fix})
        threading.Event().wait()  # serve the website until the process is stopped
    except Exception:
        logging.exception("Bot stopped")
    os._exit(1)


if __name__ == "__main__":
    run()
