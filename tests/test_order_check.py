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

    def test_aycd_timing_out_means_no_email(self):
        # AYCD reports a search that found nothing as an error saying it timed out.
        c = {"order": "912000000000005", "retailer": "Target"}
        timed = {"status": "error", "results": {"errorMessage": "The task has timed out."}}
        self.assertEqual(order_check.outcome(c, timed, timed, [])["outcome"], "not_found")
        broken = {"status": "error", "results": {"errorMessage": "No mail credentials for that email."}}
        self.assertEqual(order_check.outcome(c, timed, broken, [])["outcome"], "error")


if __name__ == "__main__":
    unittest.main()
