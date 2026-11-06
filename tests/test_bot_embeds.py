"""The bot's DMs fit Discord's limits without cutting a line in half."""
import unittest
from unittest import mock

import tests.test_server  # noqa: F401  sets up the throwaway database before config loads

import bot  # noqa: E402

KNOCK_OUT = "Pokémon Trading Card Game: 30th Celebration Knock Out Collection"


def checkout(product, qty=1, fee=4.80, **extra):
    return {"product": product, "qty": qty, "fee": fee, "retailer": "Target", "size": "",
            "profile_name": "jake-main", "email": "jake+1@gmail.com", **extra}


def invoice(rows):
    pay = {"cashapp": "$buffsquad", "zelle": "", "paypal": "", "note": "", "remindDays": 0}
    with mock.patch.object(bot.db, "invoice_checkouts", return_value=rows), \
            mock.patch.object(bot.db, "pay_settings", return_value=pay):
        return bot.invoice_embed(None, {"id": 2}, reminder=True)


class FitLinesTest(unittest.TestCase):
    def test_everything_fits(self):
        self.assertEqual(bot.fit_lines("Head", ["a", "b"], "…and {n} more"), "Head\na\nb")

    def test_cuts_on_a_whole_line_and_counts_the_rest(self):
        text = bot.fit_lines("Head", ["x" * 40] * 10, "…and {n} more", limit=150)
        self.assertLessEqual(len(text), 150)
        self.assertTrue(text.endswith("…and 7 more"))
        self.assertEqual(text.count("x" * 40), 3)


class InvoiceEmbedTest(unittest.TestCase):
    def test_same_item_collapses_to_one_line(self):
        # The invoice from the screenshot: 30 checkouts, most of them the same box.
        rows = [checkout(KNOCK_OUT, qty=2) for _ in range(28)] + [checkout("Prismatic ETB", fee=12.00)] * 2
        text = invoice(rows).description
        self.assertIn(f"{KNOCK_OUT} ×2 · $4.80 each, ×28 checkouts · **$134.40**", text)
        self.assertIn("Prismatic ETB · $12.00 each, ×2 checkouts · **$24.00**", text)
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
