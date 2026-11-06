"""The bot's DMs fit Discord's limits without cutting a line in half."""
import unittest
from unittest import mock

import tests.test_server  # noqa: F401  sets up the throwaway database before config loads

import bot  # noqa: E402

KNOCK_OUT = "Pokémon Trading Card Game: 30th Celebration Knock Out Collection"


def checkout(product, qty=1, fee=4.80, price=96.00, fee_source="default", **extra):
    return {"product": product, "qty": qty, "fee": fee, "price": price, "fee_source": fee_source, "retailer": "Target",
            "size": "", "profile_name": "jake-main", "email": "jake+1@gmail.com", **extra}


RULES = [{"id": "r1", "match": "product", "text": "Prismatic", "retailer": "Target", "kind": "per_item", "amount": 6},
         {"id": "r2", "match": "sku", "text": "123", "retailer": "", "kind": "flat", "amount": 10}]


def with_settings(fn, *extra_patches):
    """Runs fn with Target's default fee at 5% of price, 'Other' at $2 a checkout, and the two rules above."""
    patches = [mock.patch.object(bot.db, "fee_rules", return_value=RULES),
               mock.patch.object(bot.db, "fees", return_value={"Target": 5, "Other": 2}),
               mock.patch.object(bot.db, "fee_types", return_value={"Target": "percent", "Other": "flat"}), *extra_patches]
    for p in patches:
        p.start()
    try:
        return fn()
    finally:
        for p in patches:
            p.stop()


def explain(row):
    return with_settings(lambda: bot.db.fee_explainer(None)(row))


def invoice(rows):
    pay = {"cashapp": "$buffsquad", "zelle": "", "paypal": "", "note": "", "remindDays": 0}
    return with_settings(lambda: bot.invoice_embed(None, {"id": 2}, reminder=True),
                         mock.patch.object(bot.db, "invoice_checkouts", return_value=rows),
                         mock.patch.object(bot.db, "pay_settings", return_value=pay))


class FitLinesTest(unittest.TestCase):
    def test_everything_fits(self):
        self.assertEqual(bot.fit_lines("Head", ["a", "b"], "…and {n} more"), "Head\na\nb")

    def test_cuts_on_a_whole_line_and_counts_the_rest(self):
        text = bot.fit_lines("Head", ["x" * 40] * 10, "…and {n} more", limit=150)
        self.assertLessEqual(len(text), 150)
        self.assertTrue(text.endswith("…and 7 more"))
        self.assertEqual(text.count("x" * 40), 3)


class FeeMathTest(unittest.TestCase):
    def test_retailer_default_percent(self):
        self.assertEqual(explain(checkout(KNOCK_OUT, qty=2, price=63.98, fee=3.20)),
                         {"basis": "Target default: 5% of price", "math": "5% of $63.98 = $3.20", "adds": True})

    def test_per_item_and_flat_rules(self):
        e = explain(checkout("Prismatic ETB", qty=2, fee=12.00, fee_source="rule:r1"))
        self.assertEqual((e["basis"], e["math"]), ("“Prismatic” rule at Target: $6.00 per item", "$6.00 × 2 items = $12.00"))
        e = explain(checkout("Thing", fee=10, fee_source="rule:r2"))
        self.assertEqual((e["basis"], e["math"]), ("SKU 123 rule: $10.00 per checkout", "$10.00 per checkout"))

    def test_other_store_manual_and_deleted_rule(self):
        self.assertEqual(explain(checkout("X", fee=2, retailer="Walmart"))["basis"], "Other default: $2.00 per checkout")
        self.assertEqual(explain(checkout("X", fee=7, fee_source="manual"))["basis"], "Set by hand by an admin")
        self.assertTrue(explain(checkout("X", fee=7, fee_source="rule:gone"))["adds"])

    def test_flags_a_fee_that_no_longer_adds_up(self):
        e = explain(checkout(KNOCK_OUT, price=63.98, fee=4.00))
        self.assertFalse(e["adds"])
        self.assertIn("but $4.00 was charged", e["math"])


class InvoiceEmbedTest(unittest.TestCase):
    def test_shows_how_its_charged_and_the_math(self):
        # Like the screenshot: most of it the same box, at 5% of price.
        rows = [checkout(KNOCK_OUT, qty=2, price=63.98, fee=3.20) for _ in range(28)] + \
               [checkout("Prismatic ETB", qty=2, fee=12.00, fee_source="rule:r1")] * 2
        embed = invoice(rows)
        text = embed.description
        self.assertIn("You owe **$113.60** for 30 checkouts.", text)
        self.assertIn("• Target default: 5% of price · 28 checkouts · $89.60", text)
        self.assertIn("• “Prismatic” rule at Target: $6.00 per item · 2 checkouts · $24.00", text)
        self.assertIn(f"{KNOCK_OUT} ×2\n↳ 5% of $63.98 = $3.20 each × 28 = **$89.60**", text)
        self.assertIn("Prismatic ETB ×2\n↳ $6.00 × 2 items = $12.00 each × 2 = **$24.00**", text)
        self.assertEqual(embed.fields[0].value, "$89.60 + $24.00 = **$113.60**")
        self.assertNotIn("more", text)

    def test_huge_invoice_stays_under_the_limit(self):
        rows = [checkout(f"{KNOCK_OUT} variant {i}", fee=1 + i / 100) for i in range(300)]
        text = invoice(rows).description
        self.assertLessEqual(len(text), bot.EMBED_DESCRIPTION)
        shown = sum(1 for line in text.splitlines() if "variant" in line)
        self.assertTrue(text.endswith(f"…and {300 - shown} more on your dashboard's Billing page"))
        self.assertTrue(all(line.endswith(tuple("0123456789")) for line in text.splitlines() if "variant" in line))


class HitSummaryEmbedTest(unittest.TestCase):
    def test_lists_profile_and_email_and_fits(self):
        job = {"title": "Friday drop", "from": 1, "to": 2}
        text = bot.hit_summary_embed(job, [checkout(KNOCK_OUT, qty=2)]).description
        self.assertIn(f"**{KNOCK_OUT}** ×2 · Target\n↳ Profile `jake-main` · jake+1@gmail.com", text)
        text = bot.hit_summary_embed(job, [checkout(f"{KNOCK_OUT} {i}") for i in range(100)]).description
        self.assertLessEqual(len(text), bot.EMBED_DESCRIPTION)
        self.assertTrue(text.endswith("more on your dashboard"))
        self.assertTrue(text.splitlines()[-2].startswith("↳ Profile"))  # never ends mid-entry


if __name__ == "__main__":
    unittest.main()
