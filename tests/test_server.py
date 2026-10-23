"""End-to-end checks of main.py against a throwaway database, signing in with /auth/dev."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ.update({
    "DB_PATH": str(Path(TMP) / "test.db"), "DEV_LOGIN": "1", "BUFFSQUAD_SECRET_KEY": "",
    "DISCORD_GUILD_ID": "", "ADMIN_DISCORD_IDS": "",
})

import config  # noqa: E402

config.DATA_DIR = Path(TMP)  # generated key goes here, not into the real data/

import db  # noqa: E402
import main  # noqa: E402

PROFILE = {"id": "p1", "label": "Main", "sites": ["Target"], "email": "a@x.com", "name": "Alice A", "line1": "1 Main St",
           "line2": "", "city": "Denver", "state": "CO", "zip": "80202", "phone": "", "cardName": "Alice A",
           "brand": "Visa", "last4": "4242", "exp": "08/28", "active": True}
SETTINGS = {"profiles": [PROFILE], "accounts": {"Target": [{"id": "a1", "email": "a@x.com"}], "Pokemon Center": []},
            "notify": {"checkout": True}, "inbox": {"method": None, "email": ""}, "discord": True}


class Client:
    def __init__(self, port):
        self.port, self.cookie = port, ""

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        h = {"Cookie": self.cookie, **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        conn.request(method, path, data, h)
        res = conn.getresponse()
        raw = res.read()
        for name, value in res.getheaders():
            if name.lower() == "set-cookie" and value.startswith(main.SESSION_COOKIE + "="):
                self.cookie = value.split(";")[0]
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw
        return res.status, payload, dict(res.getheaders())

    def login(self, user, admin=False):
        status, _, headers = self.request("GET", f"/auth/dev?user={user}{'&admin=1' if admin else ''}")
        assert status == 302, status
        return self


class ServerCase(unittest.TestCase):
    """Runs main.py on a free port for the tests in a subclass."""

    @classmethod
    def setUpClass(cls):
        db.init()
        cls.server = main.ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def client(self):
        return Client(self.port)


class ServerTest(ServerCase):
    def test_private_files_are_not_served(self):
        c = self.client()
        for path in ("/main.py", "/db.py", "/.env", "/.env.example", "/data/test.db", "/.git/config", "/assets/", "/tests/test_server.py"):
            with self.subTest(path=path):
                self.assertEqual(c.request("GET", path)[0], 404)
        self.assertEqual(c.request("GET", "/index.html")[0], 200)
        self.assertEqual(c.request("GET", "/dashboard.js")[0], 200)

    def test_admin_page_needs_admin(self):
        status, _, headers = self.client().request("GET", "/admin")
        self.assertEqual(status, 302)
        self.assertIn("/auth/discord/login", headers["Location"])
        self.assertEqual(self.client().login("bob").request("GET", "/admin")[0], 403)
        self.assertEqual(self.client().login("boss", admin=True).request("GET", "/admin")[0], 200)

    def test_member_flow_bot_match_and_admin(self):
        alice = self.client().login("alice")
        status, me, _ = alice.request("GET", "/api/me")
        self.assertEqual((status, me["settings"], me["checkouts"]), (200, None, []))

        # Card number sent inside settings is dropped; secrets go through /secret.
        leaky = json.loads(json.dumps(SETTINGS))
        leaky["profiles"][0]["cardNumber"] = "4242424242424242"
        self.assertEqual(alice.request("PUT", "/api/me/settings", leaky)[0], 200)
        self.assertEqual(alice.request("POST", "/api/me/secret", {"kind": "card", "ref": "p1",
                         "value": {"number": "4242424242424242", "cvv": "123", "exp": "08/28", "name": "Alice A"}})[0], 200)
        self.assertEqual(alice.request("POST", "/api/me/secret", {"kind": "account", "ref": "a1", "value": "hunter2"})[0], 200)
        _, me, _ = alice.request("GET", "/api/me")
        self.assertNotIn("cardNumber", me["settings"]["profiles"][0])
        self.assertEqual((me["saved"]["cards"], me["saved"]["accounts"]), (["p1"], ["a1"]))

        # The bot sees a checkout on Alice's profile, by bot name and by plain label.
        with db.transaction() as conn:
            bot_name = conn.execute("SELECT bot_name FROM profiles JOIN users ON users.id = profiles.user_id WHERE users.username = 'alice'").fetchone()["bot_name"]
            self.assertEqual(bot_name, "alice-main")
            hit = db.record_checkout(conn, {"status": "success", "profile": "ALICE-MAIN", "product": "ETB", "retailer": "Target", "price": 54.99, "qty": 2}, message_key="m1:0")
            self.assertIsNone(db.record_checkout(conn, {"status": "success", "profile": "alice-main"}, message_key="m1:0"))
            db.record_checkout(conn, {"status": "decline", "profile": "Main", "retailer": "Target"}, message_key="m2:0")
            unmatched = db.record_checkout(conn, {"status": "success", "profile": "someone-else", "product": "Box"}, message_key="m3:0")
            row = conn.execute("SELECT user_id, dm_status FROM checkouts WHERE id = ?", (hit,)).fetchone()
            self.assertIsNotNone(row["user_id"])
            self.assertEqual(row["dm_status"], "pending")
        _, me, _ = alice.request("GET", "/api/me")
        self.assertEqual((len(me["checkouts"]), me["declined"], me["checkouts"][0]["qty"]), (1, 1, 2))

        # Members can't use the admin API, and other sites can't post with their cookie.
        self.assertEqual(alice.request("GET", "/api/admin/members")[0], 403)
        self.assertEqual(alice.request("PUT", "/api/me/settings", SETTINGS, {"Origin": "https://evil.example"})[0], 403)

        admin = self.client().login("boss", admin=True)
        _, members, _ = admin.request("GET", "/api/admin/members")
        alice_id = next(m["id"] for m in members if m["username"] == "alice")
        _, detail, _ = admin.request("GET", f"/api/admin/members/{alice_id}")
        self.assertEqual(detail["settings"]["profiles"][0]["botName"], "alice-main")
        _, revealed, _ = admin.request("POST", "/api/admin/reveal", {"userId": alice_id, "kind": "card", "ref": "p1"})
        self.assertEqual(revealed["value"]["number"], "4242424242424242")
        _, audit, _ = admin.request("GET", "/api/admin/audit")
        self.assertIn("reveal.card", [a["action"] for a in audit])

        status, exported, headers = admin.request("GET", "/api/admin/export/profiles?cards=1")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        alice_profile = next(p for p in exported if p["name"] == "alice-main")
        self.assertEqual(alice_profile["paymentDetails"]["cardNumber"], "4242424242424242")
        _, text, _ = admin.request("GET", "/api/admin/export/accounts?retailer=Target")
        self.assertIn(b"a@x.com:hunter2", text)

        # Assign the unmatched checkout to Alice and queue her DM.
        _, unmatched_list, _ = admin.request("GET", "/api/admin/checkouts?view=unmatched")
        self.assertEqual([c["id"] for c in unmatched_list], [unmatched])
        _, updated, _ = admin.request("PATCH", f"/api/admin/checkouts/{unmatched}", {"userId": alice_id, "notify": True, "stage": "shipped", "fee": 15})
        self.assertEqual((updated["userId"], updated["dmStatus"], updated["stage"], updated["fee"]), (alice_id, "pending", "shipped", 15))

        # Deleting the profile on the dashboard deletes its saved card.
        alice.request("PUT", "/api/me/settings", {**SETTINGS, "profiles": []})
        _, me, _ = alice.request("GET", "/api/me")
        self.assertEqual(me["saved"]["cards"], [])

        # A turned-off member is signed out.
        admin.request("PATCH", f"/api/admin/members/{alice_id}", {"disabled": True})
        self.assertEqual(alice.request("GET", "/api/me")[0], 401)

    def test_export_one_members_profiles(self):
        def profile(pid, label, sites, active=True):
            return {**PROFILE, "id": pid, "label": label, "sites": sites, "active": active}

        dana = self.client().login("dana")
        dana.request("PUT", "/api/me/settings", {**SETTINGS, "profiles": [
            profile("d1", "Home", ["Target"]), profile("d2", "PC", ["Pokemon Center"]),
            profile("d3", "Both", ["Target", "Pokemon Center"]), profile("d4", "Off", ["Target"], active=False)]})
        dana.request("POST", "/api/me/secret", {"kind": "card", "ref": "d1", "value": {"number": "4111111111111111", "cvv": "999", "exp": "01/29", "name": "Dana"}})
        self.client().login("eve").request("PUT", "/api/me/settings", {**SETTINGS, "profiles": [profile("e1", "Home", ["Target"])]})

        admin = self.client().login("chief2", admin=True)
        _, members, _ = admin.request("GET", "/api/admin/members")
        dana_id = next(m["id"] for m in members if m["username"] == "dana")

        status, out, headers = admin.request("GET", f"/api/admin/export/profiles?userId={dana_id}&retailer=Target&cards=1")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(p["name"] for p in out), ["dana-both", "dana-home"])
        self.assertEqual(next(p for p in out if p["name"] == "dana-home")["paymentDetails"]["cardNumber"], "4111111111111111")
        self.assertIn('filename="buffsquad-dana-target-profiles-', headers["Content-Disposition"])

        _, out, _ = admin.request("GET", f"/api/admin/export/profiles?userId={dana_id}&retailer=Pokemon%20Center&cards=0")
        self.assertEqual(sorted(p["name"] for p in out), ["dana-both", "dana-pc"])
        self.assertTrue(all(p["paymentDetails"]["cardNumber"] == "" for p in out))

        _, audit, _ = admin.request("GET", "/api/admin/audit")
        self.assertTrue(any(a["action"] == "export.profiles" and "dana's" in a["detail"] for a in audit))

    def test_health_check(self):
        status, health, _ = self.client().request("GET", "/api/health")
        self.assertEqual((status, health["ok"]), (200, True))
        self.assertIn("lastSeen", health["bot"])
        # Through Cloudflare the version (a git commit id) stays hidden.
        _, public, _ = self.client().request("GET", "/api/health", headers={"CF-Connecting-IP": "203.0.113.9"})
        self.assertIsNone(public["version"])

    def test_dev_sign_in_refused_through_a_proxy(self):
        # Cloudflare's tunnel connects from 127.0.0.1 too; its headers give it away.
        for header in ("CF-Connecting-IP", "X-Forwarded-For"):
            with self.subTest(header=header):
                status, _, _ = self.client().request("GET", "/auth/dev?user=mallory&admin=1", headers={header: "203.0.113.9"})
                self.assertEqual(status, 404)

    def test_backup(self):
        import backup

        os.environ["BACKUP_DIR"] = str(Path(TMP) / "backups")
        try:
            first = backup.backup()
            self.assertTrue(first.exists())
            with db.transaction() as conn:
                self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'checkouts'").fetchone())
            import sqlite3
            copy = sqlite3.connect(first)
            self.assertIsNotNone(copy.execute("SELECT 1 FROM sqlite_master WHERE name = 'checkouts'").fetchone())
            copy.close()
        finally:
            del os.environ["BACKUP_DIR"]

    def test_sign_in_before_joining_the_server(self):
        """Someone not in the Discord server gets the invite (new tab) and a way to sign in again."""
        saved = (config.DISCORD_CLIENT_ID, config.DISCORD_CLIENT_SECRET, main.discord_request, main.in_guild)
        config.DISCORD_CLIENT_ID, config.DISCORD_CLIENT_SECRET = "123", "secret"
        main.discord_request = lambda url, **kw: {"access_token": "t"} if "token" in url else {"id": "999", "username": "newbie"}
        main.in_guild = lambda discord_id: False
        try:
            c = self.client()
            status, _, headers = c.request("GET", "/auth/discord/login?next=/dashboard")
            self.assertEqual(status, 302)
            state = next(v for k, v in [x.split("=", 1) for x in headers["Location"].split("?", 1)[1].split("&")] if k == "state")
            oauth = headers["Set-Cookie"].split(";")[0]
            status, page, _ = c.request("GET", f"/auth/discord/callback?code=abc&state={state}", headers={"Cookie": oauth})
            self.assertEqual(status, 403)
            self.assertIn(config.DISCORD_INVITE_URL.encode(), page)
            self.assertIn(b'target="_blank"', page)
            self.assertIn(b"/auth/discord/login?next=%2Fdashboard", page)
            self.assertEqual(c.cookie, "")  # not signed in
        finally:
            config.DISCORD_CLIENT_ID, config.DISCORD_CLIENT_SECRET, main.discord_request, main.in_guild = saved

    def test_reply_waits_for_the_save(self):
        """The server answers only after saving, so the next request always sees the change.
        Every save is slowed down here; answering before the save would fail this every time."""
        import contextlib
        import time

        real = db.transaction

        @contextlib.contextmanager
        def slow_transaction():
            with real() as conn:
                yield conn
                time.sleep(0.3)  # the commit happens after this

        db.transaction = slow_transaction
        try:
            fast = self.client().login("speedy")  # new session saved during sign-in
            status, me, _ = fast.request("GET", "/api/me")
            self.assertEqual(status, 200)
            fast.request("PUT", "/api/me/settings", {**SETTINGS, "profiles": []})
            _, me, _ = fast.request("GET", "/api/me")
            self.assertEqual(me["settings"]["profiles"], [])
        finally:
            db.transaction = real

    def test_dashboard_only_for_signed_in_members(self):
        for path in ("/dashboard", "/dashboard_proto_type.html"):
            with self.subTest(path=path):
                status, _, headers = self.client().request("GET", path)
                self.assertEqual(status, 302)
                self.assertIn("/auth/discord/login?next=%2Fdashboard", headers["Location"])
        status, page, headers = self.client().login("gwen").request("GET", "/dashboard")
        self.assertEqual(status, 200)
        self.assertIn(b"Welcome back", page)
        self.assertEqual(headers["Cache-Control"], "no-store")  # Back button can't show it after signing out

    def test_bulk_delete_checkouts(self):
        admin = self.client().login("bulkboss", admin=True)
        with db.transaction() as conn:
            uid = conn.execute("SELECT id FROM users WHERE username = 'bulkboss'").fetchone()["id"]
            mk = lambda product, status="success": db.record_checkout(
                conn, {"status": status, "product": product, "retailer": "Target"}, user_id=uid, notify=False)
            picked = [mk("ZZBULK pick 1"), mk("ZZBULK pick 2")]
            kept = mk("ZZBULK keep")
            misses = [mk("ZZBULK miss", "decline"), mk("ZZBULK miss", "decline")]
            other = mk("Unrelated item")

        # Ticked ones only.
        status, out, _ = admin.request("POST", "/api/admin/checkouts/delete", {"ids": picked})
        self.assertEqual((status, out["deleted"]), (200, 2))
        # Everything in a view + search: count first, then delete. Only the misses go.
        _, dry, _ = admin.request("POST", "/api/admin/checkouts/delete", {"all": True, "view": "misses", "q": "ZZBULK", "dryRun": True})
        self.assertEqual(dry["count"], 2)
        _, out, _ = admin.request("POST", "/api/admin/checkouts/delete", {"all": True, "view": "misses", "q": "ZZBULK"})
        self.assertEqual(out["deleted"], 2)
        with db.transaction() as conn:
            left = {r["id"] for r in conn.execute("SELECT id FROM checkouts WHERE id IN (?, ?, ?, ?, ?, ?)", [*picked, kept, *misses, other])}
        self.assertEqual(left, {kept, other})

        self.assertEqual(self.client().login("nobody").request("POST", "/api/admin/checkouts/delete", {"ids": [kept]})[0], 403)
        _, audit, _ = admin.request("GET", "/api/admin/audit")
        self.assertTrue(any(a["action"] == "checkout.bulk_delete" and "2 checkouts (selected)" in a["detail"] for a in audit))

    def test_icloud_inbox_check(self):
        """iCloud signs in with the part before @ for iCloud addresses; both forms are tried."""
        import imaplib

        tried = []

        class FakeICloud:
            def __init__(self, host, port, timeout=None):
                assert (host, port) == ("imap.mail.me.com", 993)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def login(self, name, password):
                tried.append(name)
                if (name, password) != ("jake", "abcd-efgh-ijkl-mnop"):
                    raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

            def logout(self):
                pass

        saved = main.imaplib.IMAP4_SSL
        main.imaplib.IMAP4_SSL = FakeICloud
        try:
            c = self.client().login("icloudtester")
            _, ok, _ = c.request("POST", "/api/inbox/icloud/check", {"email": "jake@icloud.com", "password": "abcd-efgh-ijkl-mnop"})
            self.assertEqual((ok["ok"], tried), (True, ["jake@icloud.com", "jake"]))
            _, bad, _ = c.request("POST", "/api/inbox/icloud/check", {"email": "jake@icloud.com", "password": "wrong"})
            self.assertFalse(bad["ok"])
            self.assertIn("app-specific password", bad["message"])
            self.assertEqual(self.client().request("POST", "/api/inbox/icloud/check", {"email": "a@icloud.com", "password": "x"})[0], 401)
        finally:
            main.imaplib.IMAP4_SSL = saved

    def test_checkout_channels(self):
        admin = self.client().login("chief", admin=True)
        status, data, _ = admin.request("GET", "/api/admin/channels")
        self.assertEqual((status, data["channels"]), (200, []))

        self.assertEqual(admin.request("POST", "/api/admin/channels", {"id": "1287654321098765432", "label": "Refract"})[0], 200)
        # A pasted channel link works; duplicates and junk don't.
        self.assertEqual(admin.request("POST", "/api/admin/channels", {"id": "https://discord.com/channels/111111111111111111/1299999999999999999"})[0], 200)
        self.assertEqual(admin.request("POST", "/api/admin/channels", {"id": "1287654321098765432"})[0], 400)
        self.assertEqual(admin.request("POST", "/api/admin/channels", {"id": "checkouts"})[0], 400)

        _, data, _ = admin.request("GET", "/api/admin/channels")
        self.assertEqual([(c["id"], c["label"]) for c in data["channels"]],
                         [("1287654321098765432", "Refract"), ("1299999999999999999", "")])
        with db.transaction() as conn:
            self.assertEqual({c["id"] for c in db.checkout_channels(conn)}, {"1287654321098765432", "1299999999999999999"})

        self.assertEqual(admin.request("DELETE", "/api/admin/channels/1299999999999999999")[0], 200)
        self.assertEqual(admin.request("DELETE", "/api/admin/channels/1299999999999999999")[0], 404)
        _, data, _ = admin.request("GET", "/api/admin/channels")
        self.assertEqual(len(data["channels"]), 1)

        member = self.client().login("carl")
        self.assertEqual(member.request("POST", "/api/admin/channels", {"id": "1287654321098765433"})[0], 403)


if __name__ == "__main__":
    unittest.main()
