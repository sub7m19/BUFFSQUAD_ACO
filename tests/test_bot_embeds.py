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
    def test_retailer_default_percent_is_an_equation(self):
        self.assertEqual(explain(checkout(KNOCK_OUT, qty=2, price=63.98, fee=3.20)), {
            "basis": "Target default: 5% of price", "result": "$3.20", "math": "$63.98 × 5% = $3.20", "adds": True, "expected": 3.2,
            "terms": [{"value": "$63.98", "label": "price"}, {"value": "5%", "label": "Target fee"}]})

    def test_per_item_and_flat_rules(self):
        e = explain(checkout("Prismatic ETB", qty=2, fee=12.00, fee_source="rule:r1"))
        self.assertEqual((e["basis"], e["math"]), ("“Prismatic” rule at Target: $6.00 per item", "$6.00 × 2 items = $12.00"))
        self.assertEqual(e["terms"], [{"value": "$6.00", "label": "fee"}, {"value": "2", "label": "items"}])
        e = explain(checkout("Thing", fee=10, fee_source="rule:r2"))
        self.assertEqual((e["basis"], e["math"], e["terms"]),
                         ("SKU 123 rule: $10.00 per checkout", "$10.00 per checkout", [{"value": "$10.00", "label": "per checkout"}]))

    def test_other_store_manual_and_deleted_rule(self):
        e = explain(checkout("X", fee=2, retailer="Walmart"))
        self.assertEqual((e["basis"], e["terms"][0]["label"]), ("Other default: $2.00 per checkout", "per checkout"))
        e = explain(checkout("X", fee=7, fee_source="manual"))
        self.assertEqual((e["basis"], e["terms"], e["math"]), ("Set by hand by an admin", [], "$7.00"))
        self.assertEqual(explain(checkout("X", fee=7, fee_source="rule:gone"))["terms"], [])

    def test_a_fee_that_no_longer_adds_up_shows_just_its_amount(self):
        e = explain(checkout(KNOCK_OUT, price=63.98, fee=4.00))
        self.assertEqual((e["adds"], e["math"], e["expected"]), (False, "$4.00", 3.2))


class InvoiceEmbedTest(unittest.TestCase):
    def test_shows_how_its_charged_and_the_math(self):
        # Like the screenshot: most of it the same box, at 5% of price.
        rows = [checkout(KNOCK_OUT, qty=2, price=63.98, fee=3.20) for _ in range(28)] + \
               [checkout("Prismatic ETB", qty=2, fee=12.00, fee_source="rule:r1")] * 2
        embed = invoice(rows)
        text = embed.description
        self.assertIn("You owe **$113.60** for 30 checkouts.", text)
        self.assertIn("• Target: 5% of price · 28 checkouts · $89.60", text)
        self.assertIn("• “Prismatic” rule at Target: $6.00 per item · 2 checkouts · $24.00", text)
        self.assertIn(f"**{KNOCK_OUT}** ×2\n↳ `$63.98 × 5% = $3.20` × 28 checkouts = **$89.60**", text)
        self.assertIn("**Prismatic ETB** ×2\n↳ `$6.00 × 2 items = $12.00` × 2 checkouts = **$24.00**", text)
        self.assertEqual(embed.fields[0].value, "$89.60 + $24.00 = **$113.60**")
        self.assertNotIn("more", text)

    def test_members_never_see_how_admins_set_a_fee(self):
        # A hand-set fee and one today's settings no longer give: just the amount, under "Other fees".
        text = invoice([checkout("Hand set", fee=7, fee_source="manual"), checkout(KNOCK_OUT, price=63.98, fee=4.00)]).description
        self.assertIn("**Hand set**\n↳ **$7.00** fee", text)
        self.assertIn(f"**{KNOCK_OUT}**\n↳ **$4.00** fee", text)
        self.assertIn("• Other fees (amount on each line) · 2 checkouts · $11.00", text)
        self.assertNotIn("admin", text)
        self.assertNotIn("Target", text.split("**Checkouts**")[0].split("How the fee")[1])

    def test_huge_invoice_stays_under_the_limit(self):
        rows = [checkout(f"{KNOCK_OUT} variant {i}", fee=1 + i / 100) for i in range(300)]
        text = invoice(rows).description
        self.assertLessEqual(len(text), bot.EMBED_DESCRIPTION)
        shown = sum(1 for line in text.splitlines() if "variant" in line)
        self.assertTrue(text.endswith(f"…and {300 - shown} more on your dashboard's Billing page"))
        # Every item shown is whole: its name, then its fee line.
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "variant" in line:
                self.assertTrue(line.endswith("**") and lines[i + 1].startswith("↳ ") and lines[i + 1].endswith(" fee"), line)


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
