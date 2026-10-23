"""Fee rules: which rule sets a checkout's fee, the admin API for them, and upgrading old databases."""
import sqlite3
import unittest
from pathlib import Path

from tests.test_server import TMP, ServerCase  # sets up the throwaway database first

import config  # noqa: E402
import db  # noqa: E402

RULES = [
    {"id": "r1", "match": "sku", "text": "94681785", "retailer": "", "kind": "flat", "amount": 30},
    {"id": "r2", "match": "product", "text": "Prismatic Evolutions", "retailer": "Target", "kind": "per_item", "amount": 20},
    {"id": "r3", "match": "product", "text": "booster box", "retailer": "", "kind": "percent", "amount": 15},
    {"id": "r4", "match": "product", "text": "Prismatic", "retailer": "", "kind": "flat", "amount": 5},
]


class ComputeFee(unittest.TestCase):
    def setUp(self):
        db.init()
        with db.transaction() as conn:
            db.kv_set(conn, "fees", {"Target": 12, "Pokemon Center": 10, "Other": 8})

    def fee(self, retailer, product, sku="", price=0, qty=1):
        with db.transaction() as conn:
            return db.compute_fee(conn, retailer, product, sku, price, qty, rules=RULES)

    def test_rules_in_order(self):
        # SKU rule wins even though the product name matches later rules.
        self.assertEqual(self.fee("Target", "Prismatic Evolutions ETB", sku="94681785"), (30.0, "rule:r1"))
        # Per item: $20 x 2.
        self.assertEqual(self.fee("Target", "prismatic evolutions etb", qty=2), (40.0, "rule:r2"))
        # r2 is Target-only, so Pokemon Center falls through to r4.
        self.assertEqual(self.fee("Pokemon Center", "Prismatic Evolutions Surprise Box", qty=2), (5.0, "rule:r4"))
        # 15% of $159.99.
        self.assertEqual(self.fee("Target", "Surging Sparks Booster Box", price=159.99), (24.0, "rule:r3"))

    def test_falls_back_to_retailer_default(self):
        self.assertEqual(self.fee("Target", "Journey Together ETB"), (12.0, "default"))
        self.assertEqual(self.fee("Walmart", "Journey Together ETB"), (8.0, "default"))
        self.assertEqual(self.fee("Target", "Some item", sku="94681785x"), (12.0, "default"))


class DefaultFeeTypes(unittest.TestCase):
    def setUp(self):
        db.init()

    def tearDown(self):
        with db.transaction() as conn:
            db.kv_set(conn, "fees", {"Target": 12, "Pokemon Center": 10, "Other": 8})
            db.kv_set(conn, "fee_types", {})

    def test_percent_default(self):
        with db.transaction() as conn:
            db.kv_set(conn, "fees", {"Target": 10, "Pokemon Center": 15, "Other": 5})
            db.kv_set(conn, "fee_types", {"Target": "percent", "Other": "percent"})
            # 10% of $159.99; Pokemon Center stays flat; unknown retailers use Other (5%).
            self.assertEqual(db.compute_fee(conn, "Target", "Box", "", 159.99, 1, rules=[]), (16.0, "default"))
            self.assertEqual(db.compute_fee(conn, "Pokemon Center", "Box", "", 159.99, 1, rules=[]), (15.0, "default"))
            self.assertEqual(db.compute_fee(conn, "Walmart", "Box", "", 40, 1, rules=[]), (2.0, "default"))
            # Saved before fee types existed: still flat.
            db.kv_set(conn, "fee_types", {})
            self.assertEqual(db.compute_fee(conn, "Target", "Box", "", 159.99, 1, rules=[]), (10.0, "default"))


class FeeRulesApi(ServerCase):
    def test_rules_api_bot_checkouts_and_apply(self):
        admin = self.client().login("feeboss", admin=True)
        bad = [{"match": "product", "text": "", "kind": "flat", "amount": 5}]
        self.assertEqual(admin.request("PUT", "/api/admin/fee-rules", {"rules": bad})[0], 400)
        # Default fees: % over 100 refused; a saved % type comes back from settings.
        self.assertEqual(admin.request("PUT", "/api/admin/settings", {"fees": {"Target": 120}, "feeTypes": {"Target": "percent"}})[0], 400)
        self.assertEqual(admin.request("PUT", "/api/admin/settings", {"fees": {"Target": 12, "Other": 7}, "feeTypes": {"Other": "percent"}})[0], 200)
        _, settings, _ = admin.request("GET", "/api/admin/settings")
        self.assertEqual((settings["feeTypes"]["Target"], settings["feeTypes"]["Other"]), ("flat", "percent"))
        with db.transaction() as conn:
            db.kv_set(conn, "fee_types", {})
        bad = [{"match": "product", "text": "x", "kind": "percent", "amount": 150}]
        self.assertEqual(admin.request("PUT", "/api/admin/fee-rules", {"rules": bad})[0], 400)

        status, saved, _ = admin.request("PUT", "/api/admin/fee-rules", {"rules": [
            {"match": "product", "text": "Zebra Box", "retailer": "", "kind": "flat", "amount": "25"}]})
        self.assertEqual(status, 200)
        rule_id = saved["rules"][0]["id"]
        _, settings, _ = admin.request("GET", "/api/admin/settings")
        self.assertEqual(settings["rules"][0]["amount"], 25)

        with db.transaction() as conn:
            db.kv_set(conn, "fees", {"Target": 12})
            # Give them a member so they don't show up as unmatched in other tests.
            uid = conn.execute("SELECT id FROM users WHERE username = 'feeboss'").fetchone()["id"]
            hit = db.record_checkout(conn, {"status": "success", "product": "Zebra Box Deluxe", "retailer": "Target", "sku": "Z1"}, message_key="fee1", user_id=uid, notify=False)
            plain = db.record_checkout(conn, {"status": "success", "product": "Zebra Plush", "retailer": "Target"}, message_key="fee2", user_id=uid, notify=False)
            paid = db.record_checkout(conn, {"status": "success", "product": "Lion Box", "retailer": "Target"}, message_key="fee3", user_id=uid, notify=False)
            hand = db.record_checkout(conn, {"status": "success", "product": "Lion Box", "retailer": "Target"}, message_key="fee4", user_id=uid, notify=False)
            row = conn.execute("SELECT fee, fee_source, sku FROM checkouts WHERE id = ?", (hit,)).fetchone()
            self.assertEqual((row["fee"], row["fee_source"], row["sku"]), (25.0, f"rule:{rule_id}", "Z1"))
            self.assertEqual(conn.execute("SELECT fee_source FROM checkouts WHERE id = ?", (plain,)).fetchone()[0], "default")

        # Mark one paid, and change another's fee by hand.
        admin.request("PATCH", f"/api/admin/checkouts/{paid}", {"paid": True})
        _, edited, _ = admin.request("PATCH", f"/api/admin/checkouts/{hand}", {"fee": 3})
        self.assertEqual(edited["feeSource"], "manual")
        # Saving the form with the same fee doesn't count as a hand edit.
        _, same, _ = admin.request("PATCH", f"/api/admin/checkouts/{plain}", {"fee": 12, "stage": "shipped"})
        self.assertEqual(same["feeSource"], "default")

        # A new rule for "Lion Box" and "Plush": apply should only touch the unpaid, non-manual ones.
        admin.request("PUT", "/api/admin/fee-rules", {"rules": saved["rules"] + [
            {"match": "product", "text": "lion box", "kind": "flat", "amount": 40},
            {"match": "product", "text": "plush", "kind": "flat", "amount": 2}]})
        _, dry, _ = admin.request("POST", "/api/admin/fee-rules/apply", {"dryRun": True})
        changed_ids = {plain}  # Zebra Plush 12 -> 2; Lion Box paid and hand-set stay
        self.assertEqual(dry["changed"], len(changed_ids))
        self.assertEqual(dry["difference"], -10.0)
        with db.transaction() as conn:
            self.assertEqual(conn.execute("SELECT fee FROM checkouts WHERE id = ?", (plain,)).fetchone()[0], 12.0)

        _, done, _ = admin.request("POST", "/api/admin/fee-rules/apply", {"dryRun": False})
        self.assertEqual(done["changed"], 1)
        with db.transaction() as conn:
            fees = {r["id"]: r["fee"] for r in conn.execute("SELECT id, fee FROM checkouts WHERE id IN (?, ?, ?, ?)", (hit, plain, paid, hand))}
        self.assertEqual(fees, {hit: 25.0, plain: 2.0, paid: 12.0, hand: 3.0})


class UpgradeOldDatabase(unittest.TestCase):
    def test_adds_new_columns(self):
        path = Path(TMP) / "old.db"
        # The schema as first released, before sku and fee_source.
        first_release = "\n".join(line for line in db.SCHEMA.splitlines()
                                  if not line.lstrip().startswith(("sku ", "fee_source ")))
        self.assertNotIn("fee_source", first_release)
        old = sqlite3.connect(path)
        old.executescript(first_release)
        old.execute("INSERT INTO checkouts (product, fee, created_at) VALUES ('ETB', 15, 1)")
        old.commit()
        old.close()
        saved, config.DB_PATH = config.DB_PATH, path
        try:
            db.init()
            with db.transaction() as conn:
                row = conn.execute("SELECT sku, fee_source, fee FROM checkouts").fetchone()
            self.assertEqual((row["sku"], row["fee_source"], row["fee"]), ("", "", 15))
        finally:
            config.DB_PATH = saved


if __name__ == "__main__":
    unittest.main()
