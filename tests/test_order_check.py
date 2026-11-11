"""Order check: AYCD tasks for each checkout, and only emails naming that order count."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from tests.test_server import SETTINGS, ServerCase

import db  # noqa: E402
import order_check  # noqa: E402

# What the fake mailbox holds, by account email: (subject, body) pairs.
MAILBOX = {
    "a@x.com": [
        ("Here's your order #:912000000000001.", "Order #912000000000001\nOrder total $96.39"),
        ("Here's your order #:912000000000002.", "Order #912000000000002\nOrder total $48.20"),
        ("Your order #912000000000002 has been canceled", "Order #912000000000002"),
        ("Your order #912000000000003 has shipped", "Order #912000000000003\nTracking number 1Z999AA10123456784"),
        ("Here's your order #:912000000000003.", "Order #912000000000003\nOrder total $20.00"),
    ],
}


def matches(task, subject, sender):
    for f in task["mailFilters"]:
        text = subject if f["target"] == "subject" else sender
        hit = any(v.lower() in text.lower() for v in [f["value"], *f.get("orValues", [])])
        if f["comparator"] == "excludes":
            hit = not hit
        if not hit and not f.get("optional"):
            return False
    return True


class FakeAycd(BaseHTTPRequestHandler):
    tasks: dict = {}
    keys = {"good-key"}

    def log_message(self, *args):
        pass

    def reply(self, status, data):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def authed(self):
        if self.headers.get("Authorization", "").removeprefix("Token ") not in self.keys:
            self.reply(401, {"message": "bad key"})
            return False
        return True

    def do_GET(self):
        if not self.authed():
            return
        url = urlparse(self.path)
        if url.path == "/api/v1/user/verify":
            return self.reply(200, {})
        group = parse_qs(url.query)["group"][0]
        self.reply(200, [t for t in self.tasks.values() if t["group"] == group])

    def do_POST(self):
        if not self.authed():
            return
        task = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        found = next(((s, b) for s, b in MAILBOX.get(task["email"], []) if matches(task, s, "orders@target.com")), None)
        task_id = f"t{len(self.tasks) + 1}"
        if found:
            subject, body = found
            results = {"subject": subject, "from": "orders@target.com", "orderNumber": body.splitlines()[0],
                       "orderTotal": next((ln for ln in body.splitlines() if "total" in ln), ""),
                       "trackingNumber": next((ln for ln in body.splitlines() if "Tracking" in ln), "")}
            done = {"id": task_id, "status": "success", "results": results}
        else:
            done = {"id": task_id, "status": "timeout"}
        self.tasks[task_id] = {**done, "group": task["group"]}
        self.reply(200, {"id": task_id})


class OrderCheckTest(ServerCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.aycd = ThreadingHTTPServer(("127.0.0.1", 0), FakeAycd)
        threading.Thread(target=cls.aycd.serve_forever, daemon=True).start()
        cls.old = (order_check.BASE_URL, order_check.POLL_SECONDS)
        order_check.BASE_URL = f"http://127.0.0.1:{cls.aycd.server_address[1]}"
        order_check.POLL_SECONDS = 0.05

    @classmethod
    def tearDownClass(cls):
        order_check.BASE_URL, order_check.POLL_SECONDS = cls.old
        cls.aycd.shutdown()
        super().tearDownClass()

    def member(self, name, key):
        c = self.client().login(name)
        # No profiles or accounts, so the other tests' checkouts never match to these members.
        settings = {**json.loads(json.dumps(SETTINGS)), "profiles": [], "accounts": {}, "inbox": {"method": "aycd", "email": ""}}
        self.assertEqual(c.request("PUT", "/api/me/settings", settings)[0], 200)
        self.assertEqual(c.request("POST", "/api/me/secret", {"kind": "inbox", "ref": "inbox", "value": key})[0], 200)
        with db.transaction() as conn:
            return conn.execute("SELECT id FROM users WHERE discord_id = ?", (f"dev-{name}",)).fetchone()["id"]

    def wait(self, admin, user_id):
        for _ in range(200):
            _, body, _ = admin.request("GET", f"/api/admin/order-check/{user_id}")
            if body["run"]["status"] != "running":
                return body["run"]
            time.sleep(0.05)
        self.fail("check never finished")

    def test_check_sorts_orders_by_what_the_emails_say(self):
        user_id = self.member("ocheck", "good-key")
        with db.transaction() as conn:
            for order in ("912000000000001", "912000000000002", "912000000000003", "912000000000009", ""):
                db.record_checkout(conn, {"retailer": "Target", "product": "Box", "order": order, "email": "a@x.com",
                                          "price": 50}, user_id=user_id, notify=False)
        admin = self.owner()
        _, listing, _ = admin.request("GET", "/api/admin/order-check")
        self.assertIn(user_id, [m["id"] for m in listing["members"]])

        status, body, _ = admin.request("POST", f"/api/admin/order-check/{user_id}", {"days": 7})
        self.assertEqual(status, 200, body)
        run = self.wait(admin, user_id)
        self.assertEqual(run["status"], "done", run["message"])
        got = {r["order"]: r for r in run["results"]}
        self.assertEqual(got["912000000000001"]["outcome"], "confirmed")
        self.assertEqual(got["912000000000001"]["emailTotal"], 96.39)
        self.assertEqual(got["912000000000002"]["outcome"], "canceled")
        self.assertEqual(got["912000000000003"]["outcome"], "shipped")
        self.assertEqual(got["912000000000003"]["tracking"], "1Z999AA10123456784")
        self.assertEqual(got["912000000000009"]["outcome"], "not_found")
        self.assertEqual(run["options"]["skipped"], 1)   # the checkout with no order number
        self.assertEqual((run["summary"]["stuck"], run["summary"]["done"]), (2, 4))

        # The finished run is kept, and the check is in the log.
        order_check.JOBS.pop(user_id)
        _, again, _ = admin.request("GET", f"/api/admin/order-check/{user_id}")
        self.assertEqual(again["run"]["summary"]["stuck"], 2)
        _, log, _ = admin.request("GET", "/api/admin/audit")
        self.assertTrue(any(r["action"] == "order_check.run" for r in log["rows"]))
        self.assertTrue(all(isinstance(r["secs"], int) for r in run["results"]))
        # Only the one with nothing found got a second, slower look.
        self.assertEqual(run["secondLook"], 1)
        self.assertEqual(run["tasks"]["total"], 4 * 2 + 1 * 2)
        self.assertGreaterEqual(run["finishedAt"], run["startedAt"])

        # Confirmations only: one search per checkout, so the canceled order just reads as confirmed.
        status, body, _ = admin.request("POST", f"/api/admin/order-check/{user_id}", {"days": 7, "quick": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["run"]["tasks"]["total"], 4)
        run = self.wait(admin, user_id)
        got = {r["order"]: r["outcome"] for r in run["results"]}
        self.assertEqual((got["912000000000001"], got["912000000000002"], got["912000000000009"]),
                         ("confirmed", "confirmed", "not_found"))

    def test_bad_key_fails_the_check(self):
        user_id = self.member("ocheckbad", "wrong-key")
        with db.transaction() as conn:
            db.record_checkout(conn, {"retailer": "Target", "order": "912000000000001", "email": "a@x.com"},
                               user_id=user_id, notify=False)
        admin = self.owner()
        self.assertEqual(admin.request("POST", f"/api/admin/order-check/{user_id}", {"days": 7})[0], 200)
        run = self.wait(admin, user_id)
        self.assertEqual(run["status"], "failed")
        self.assertIn("didn't accept", run["message"])

    def test_members_cant_run_it(self):
        user_id = self.member("ochecknosy", "good-key")
        self.assertEqual(self.client().login("ochecknosy").request("POST", f"/api/admin/order-check/{user_id}", {})[0], 403)

    def test_another_order_on_the_account_is_not_this_one(self):
        c = {"order": "912000000000005", "retailer": "Target"}
        other = {"status": "success", "results": {"subject": "Here's your order #:912000000000001."}}
        self.assertEqual(order_check.outcome(c, other, {"status": "timeout"}, [])["outcome"], "mismatch")

    def test_search_window_starts_before_the_webhook(self):
        # The bot caught up on this checkout an hour late: search from the Discord message's time, not the log time.
        posted = 1794290000
        message_id = (posted * 1000 - 1420070400000) << 22
        row = {"message_key": f"{message_id}:0", "created_at": posted + 3600}
        import main
        self.assertEqual(main.placed_at(row), posted)
        self.assertEqual(main.placed_at({"message_key": None, "created_at": posted}), posted)
        target = {"order": "912000000000001", "retailer": "Target", "email": "a@x.com", "createdAt": posted + 3600, "placedAt": posted}
        task = order_check.build_task(target, "order", "g", "", 60)
        self.assertEqual(task["receivedAt"], posted - order_check.LOOKBACK_BY_ORDER_NUMBER)
        loose = {**target, "retailer": "Pokemon Center"}
        self.assertEqual(order_check.build_task(loose, "order", "g", "", 60)["receivedAt"], posted - order_check.LOOKBACK_LOOSE)

    def test_target_search_has_no_excludes_and_reads_the_subject(self):
        c = {"order": "102003869997105", "retailer": "Target", "email": "a@outlook.com", "createdAt": 0}
        filters = order_check.mail_filters(c, "order")
        self.assertFalse([f for f in filters if f["comparator"] == "excludes"])
        self.assertIn({"target": "subject", "comparator": "includes", "value": "102003869997105"}, filters)
        confirmation = {"status": "success", "results": {
            "subject": "Thanks for shopping with us! Here's your order #:102003869997105."}}
        self.assertEqual(order_check.outcome(c, confirmation, {"status": "timeout"}, [])["outcome"], "confirmed")
        # The order search handed back the cancel email, the follow-up search a shipping one: canceled wins.
        canceled = {"status": "success", "results": {"subject": "Your order #102003869997105 has been canceled"}}
        shipped = {"status": "success", "results": {"subject": "Items from order #102003869997105 have shipped"}}
        self.assertEqual(order_check.outcome(c, canceled, shipped, [])["outcome"], "canceled")
        self.assertEqual(order_check.outcome(c, confirmation, shipped, [])["outcome"], "shipped")

    def test_checkout_with_no_retailer_searches_by_order_number(self):
        # The bot couldn't tell the store: never ask AYCD for mail "from unknown", which can't match.
        for retailer in ("Unknown", ""):
            c = {"order": "102003869997105", "retailer": retailer, "email": "a@outlook.com", "createdAt": 0}
            for kind in ("order", "followup"):
                filters = order_check.mail_filters(c, kind)
                self.assertEqual(filters[0], {"target": "subject", "comparator": "includes", "value": "102003869997105"})
                self.assertFalse([f for f in filters if f["target"] == "from"])
                self.assertFalse([f for f in filters if f.get("optional")])

    def test_aycd_timing_out_means_no_email(self):
        # AYCD reports a search that found nothing as an error saying it timed out.
        c = {"order": "912000000000005", "retailer": "Target"}
        timed = {"status": "error", "results": {"errorMessage": "The task has timed out."}}
        self.assertEqual(order_check.outcome(c, timed, timed, [])["outcome"], "not_found")
        broken = {"status": "error", "results": {"errorMessage": "No mail credentials for that email."}}
        self.assertEqual(order_check.outcome(c, timed, broken, [])["outcome"], "error")


def raw_email(subject, body, sent, to="a@x.com", html_body=None):
    from email.message import EmailMessage
    from email.utils import formatdate
    m = EmailMessage()
    m["Subject"], m["From"], m["To"], m["Date"] = subject, "Target <orders@oe1.target.com>", to, formatdate(sent)
    m.set_content(body)
    if html_body:
        m.add_alternative(html_body, subtype="html")
    return m.as_bytes()


class FakeImap:
    """Stands in for AYCD Inbox's IMAP Server: a mailbox per account, one password."""
    boxes: dict = {}
    password = "imap-pass"
    seen: list = []

    def __init__(self, cfg):
        self.account = None

    def login(self, account, password):
        if password != self.password or account not in self.boxes:
            import imaplib
            raise imaplib.IMAP4.error(b"Invalid credentials")
        self.account = account

    def select(self, box, readonly=False):
        assert readonly
        return "OK", [str(len(self.boxes[self.account])).encode()]

    def logout(self):
        pass

    def uid(self, command, *args):
        import email as email_mod
        from email import policy
        box = self.boxes[self.account]
        if command == "SEARCH":
            FakeImap.seen.append(args)
            pairs = dict(zip(args[::2], args[1::2]))
            hits = []
            for n, raw in enumerate(box, 1):
                m = email_mod.message_from_bytes(raw, policy=policy.default)
                body = "\n".join(p.get_content() for p in m.walk() if p.get_content_maintype() == "text")
                text = {"SUBJECT": m["Subject"], "FROM": m["From"], "TO": m["To"], "BODY": body}
                if all(v.strip('"').lower() in str(text[k]).lower() for k, v in pairs.items() if k != "SINCE"):
                    hits.append(str(n).encode())
            return "OK", [b" ".join(hits)]
        uids, what = args
        data = []
        for u in uids.split(","):
            raw = box[int(u) - 1]
            if "HEADER.FIELDS" in what:
                raw = raw.replace(b"\r\n", b"\n").split(b"\n\n", 1)[0] + b"\n\n"
            data += [(f"{u} (UID {u} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        return "OK", data


class ImapCheckTest(ServerCase):
    def setUp(self):
        self.old_open = order_check.imap_open
        order_check.imap_open = lambda cfg: FakeImap(cfg)
        now = int(time.time())
        FakeImap.seen = []
        FakeImap.boxes = {
            "a@x.com": [
                # Older than the run: Inbox tasks never see these, IMAP does.
                raw_email("Thanks for shopping with us! Here's your order #:912000000000001.",
                          "Order #912000000000001\nOrder total\n$96.39", now - 3000),
                raw_email("Here's your order #:912000000000002.", "Order #912000000000002\nTotal $48.20", now - 3000),
                raw_email("Your order #912000000000002 has been canceled", "Order #912000000000002", now - 2000),
                raw_email("Here's your order #:912000000000003.", "Order #912000000000003", now - 3000),
                raw_email("Items from order #912000000000003 have shipped",
                          "Order #912000000000003\nTracking number 1Z999AA10123456784", now - 1000),
            ],
            "fwd@gmail.com": [
                raw_email("Here's your order #:912000000000007.", "", now - 3000, to="alias@icloud.com",
                          html_body="<p>Order&nbsp;#912000000000007</p><table><tr><td>Order total</td><td>$12.50</td></tr></table>"),
            ],
        }

    def tearDown(self):
        order_check.imap_open = self.old_open

    def member(self, name):
        c = self.client().login(name)
        settings = {**json.loads(json.dumps(SETTINGS)), "profiles": [], "accounts": {}, "inbox": {"method": "aycd", "email": ""}}
        self.assertEqual(c.request("PUT", "/api/me/settings", settings)[0], 200)
        self.assertEqual(c.request("POST", "/api/me/secret", {"kind": "inbox", "ref": "inbox", "value": "good-key"})[0], 200)
        with db.transaction() as conn:
            return conn.execute("SELECT id FROM users WHERE discord_id = ?", (f"dev-{name}",)).fetchone()["id"]

    def wait(self, admin, user_id):
        for _ in range(200):
            _, body, _ = admin.request("GET", f"/api/admin/order-check/{user_id}")
            if body["run"]["status"] != "running":
                return body
            time.sleep(0.05)
        self.fail("check never finished")

    def test_imap_check_reads_mail_already_in_the_inbox(self):
        user_id = self.member("oimap")
        with db.transaction() as conn:
            for order, mail in (("912000000000001", "a@x.com"), ("912000000000002", "a@x.com"), ("912000000000003", "a@x.com"),
                                ("912000000000009", "a@x.com"), ("912000000000007", "alias@icloud.com"),
                                ("912000000000008", "nobody@x.com")):
                db.record_checkout(conn, {"retailer": "Target", "product": "Box", "order": order, "email": mail, "price": 50},
                                   user_id=user_id, notify=False)
        admin = self.owner()
        imap = {"host": "uplink.example", "port": 993, "ssl": True, "password": "imap-pass", "forwardTo": "fwd@gmail.com"}
        status, body, _ = admin.request("POST", f"/api/admin/order-check/{user_id}", {"days": 7, "source": "imap", "imap": imap})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["run"]["tasks"]["total"], 6)
        self.assertNotIn("imap-pass", json.dumps(body))
        got = self.wait(admin, user_id)
        run = got["run"]
        self.assertEqual(run["status"], "done", run["message"])
        out = {r["order"]: r for r in run["results"]}
        self.assertEqual(out["912000000000001"]["outcome"], "confirmed")
        self.assertEqual(out["912000000000001"]["emailTotal"], 96.39)    # label and amount on separate lines
        self.assertEqual(out["912000000000002"]["outcome"], "canceled")
        self.assertEqual(out["912000000000003"]["outcome"], "shipped")
        self.assertEqual(out["912000000000003"]["tracking"], "1Z999AA10123456784")
        self.assertEqual(out["912000000000009"]["outcome"], "not_found")
        # The iCloud alias was found in the mailbox it forwards to, from an HTML-only email.
        self.assertEqual(out["912000000000007"]["outcome"], "confirmed")
        self.assertEqual(out["912000000000007"]["emailTotal"], 12.5)
        # An account not set up in AYCD's IMAP Service is an error that says what to fix.
        self.assertEqual(out["912000000000008"]["outcome"], "error")
        self.assertIn("Configure Accounts", out["912000000000008"]["error"])
        self.assertTrue(all(a[0] == "SINCE" for a in FakeImap.seen))

        # The settings are kept for next time, but the password never comes back.
        self.assertEqual(got["imap"], {"host": "uplink.example", "port": 993, "ssl": True, "forwardTo": "fwd@gmail.com", "saved": True})
        self.assertNotIn("imap-pass", json.dumps(got))
        status, body, _ = admin.request("POST", f"/api/admin/order-check/{user_id}",
                                        {"days": 7, "source": "imap", "imap": {**imap, "password": ""}})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.wait(admin, user_id)["run"]["summary"]["stuck"], 3)   # 001, 003, 007

    def test_imap_check_needs_a_host_and_password(self):
        user_id = self.member("oimapbad")
        with db.transaction() as conn:
            db.record_checkout(conn, {"retailer": "Target", "order": "912000000000001", "email": "a@x.com"},
                               user_id=user_id, notify=False)
        admin = self.owner()
        status, body, _ = admin.request("POST", f"/api/admin/order-check/{user_id}",
                                        {"source": "imap", "imap": {"host": "", "password": "x"}})
        self.assertEqual(status, 400)
        status, body, _ = admin.request("POST", f"/api/admin/order-check/{user_id}", {"source": "imap", "imap": {"host": "h.example"}})
        self.assertEqual(status, 400)
        self.assertIn("IMAP password", body["message"])

        # A host that can't be reached fails the whole check at once.
        def unreachable(cfg):
            raise order_check.AycdError("Couldn't reach AYCD IMAP at h.example:993")
        order_check.imap_open = unreachable
        status, body, _ = admin.request("POST", f"/api/admin/order-check/{user_id}",
                                        {"source": "imap", "imap": {"host": "h.example", "password": "x"}})
        self.assertEqual(status, 200, body)
        run = self.wait(admin, user_id)["run"]
        self.assertEqual(run["status"], "failed")
        self.assertIn("Couldn't reach", run["message"])


if __name__ == "__main__":
    unittest.main()
