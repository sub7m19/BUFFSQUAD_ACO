import imaplib
import json
import socket
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993

# From AYCD's official Inbox API client (verify_user_api_key).
AYCD_VERIFY_URL = "https://inbox-api.aycd.io/api/v1/user/verify"


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


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path not in ("/api/inbox/gmail/check", "/api/inbox/aycd/check"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= 4096:
            self.reply(400, False, "Bad request.")
            return
        try:
            body = json.loads(self.rfile.read(length))
            if self.path == "/api/inbox/aycd/check":
                key = str(body["key"]).strip()
                ok, message = check_aycd(key) if key else (False, "Paste your AYCD Inbox key.")
            else:
                email = str(body["email"]).strip()
                password = str(body["password"]).replace(" ", "")
                if email and password:
                    ok, message = check_gmail(email, password)
                else:
                    ok, message = False, "Enter your Gmail address and app password."
        except (ValueError, KeyError, TypeError):
            self.reply(400, False, "Bad request.")
            return
        self.reply(200, ok, message)

    def reply(self, status: int, ok: bool, message: str) -> None:
        data = json.dumps({"ok": ok, "message": message}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    host = "127.0.0.1"
    port = 8000
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"BUFFSQUAD ACO site running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
