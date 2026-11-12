"""The bot's DMs fit Discord's limits without cutting a line in half."""
import asyncio
import unittest
from unittest import mock

import tests.test_server  # noqa: F401  sets up the throwaway database before config loads

import bot  # noqa: E402

KNOCK_OUT = "Pokémon Trading Card Game: 30th Celebration Knock Out Collection"


class FakeChannel:
    def __init__(self, name, position=0, can_invite=True):
        self.name, self.position, self.can_invite, self.made = name, position, can_invite, []

    def permissions_for(self, member):
        return mock.Mock(create_instant_invite=self.can_invite)

    async def create_invite(self, **kw):
        self.made.append(kw)
        return mock.Mock(url=f"https://discord.gg/new{len(self.made)}")


class InviteRenewalTest(unittest.TestCase):
    """Every 6 days the bot saves a new 7-day invite, where new members land."""

    def setUp(self):
        bot.db.init()
        self.welcome, self.general = FakeChannel("welcome", 0, can_invite=False), FakeChannel("general", 1)
        guild = mock.Mock(system_channel=self.welcome, rules_channel=None, text_channels=[self.general, self.welcome])
        patches = [mock.patch.object(bot.config, "DISCORD_GUILD_ID", "1"), mock.patch.object(bot.client, "get_guild", return_value=guild)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.clear()
        self.addCleanup(self.clear)

    @staticmethod
    def clear():
        with bot.db.transaction() as conn:
            conn.execute("DELETE FROM kv WHERE key IN ('discord_invite', 'discord_invite_status')")

    def run_once(self):
        asyncio.run(bot.refresh_invite())
        with bot.db.transaction() as conn:
            return bot.db.discord_invite(conn), bot.db.invite_status(conn)

    def test_renews_every_six_days(self):
        url, status = self.run_once()  # none saved yet: makes one now, skipping #welcome it can't invite to
        self.assertEqual((url, status["by"], status["channel"]), ("https://discord.gg/new1", "bot", "general"))
        self.assertEqual(self.general.made[0]["max_age"], 7 * 86400)
        self.assertEqual(self.run_once()[0], "https://discord.gg/new1")  # still fresh
        with mock.patch.object(bot.db, "now", return_value=bot.db.now() + 6 * 86400):
            self.assertEqual(self.run_once()[0], "https://discord.gg/new2")

    def test_an_admins_invite_waits_six_days(self):
        with bot.db.transaction() as conn:
            bot.db.save_invite(conn, "https://discord.gg/pasted", by="admin")
        self.assertEqual(self.run_once()[0], "https://discord.gg/pasted")

    def test_failure_is_shown_and_retried_hourly(self):
        self.general.can_invite = False
        url, status = self.run_once()
        self.assertIn("can't make invites", status["error"])
        self.general.can_invite = True
        self.run_once()
        self.assertEqual(self.general.made, [])  # waits an hour
        with mock.patch.object(bot.db, "now", return_value=bot.db.now() + 3600):
            self.assertEqual(self.run_once()[1]["error"], "")


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
        self.assertIn(f"**{KNOCK_OUT}** · Quantity 2\n↳ `$63.98 × 5% = $3.20` × 28 checkouts = **$89.60**", text)
        self.assertIn("**Prismatic ETB** · Quantity 2\n↳ `$6.00 × 2 items = $12.00` × 2 checkouts = **$24.00**", text)
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
    JOB = {"title": "Friday drop", "from": 1, "to": 2}

    def test_totals_then_a_section_per_store(self):
        hits = [checkout(KNOCK_OUT, qty=2), checkout("Prismatic ETB", qty=1, size="Box"),
                checkout("151 Booster Bundle", retailer="Pokemon Center", email="")]
        embed = bot.hit_summary_embed(self.JOB, hits)
        self.assertEqual(embed.title, "🎉 Friday drop: you hit!")
        self.assertEqual([(f.name, f.value) for f in embed.fields[:3]], [("Items", "**4**"), ("Checkouts", "**3**"), ("Stores", "**2**")])
        target, pc = embed.fields[3], embed.fields[4]
        self.assertEqual(target.name, "🎯 Target")
        self.assertEqual(target.value, f"**{KNOCK_OUT}**  ·  Quantity 2\n-# jake-main · ||jake+1@gmail.com||\n\n"
                                       "**Prismatic ETB**  ·  Size Box\n-# jake-main · ||jake+1@gmail.com||")
        self.assertEqual((pc.name, pc.value), ("⚡ Pokémon Center", "**151 Booster Bundle**\n-# jake-main"))

    def test_lots_of_hits_stay_within_discords_limits(self):
        embed = bot.hit_summary_embed(self.JOB, [checkout(f"{KNOCK_OUT} {i}") for i in range(100)])
        self.assertLessEqual(len(embed), bot.EMBED_TOTAL)
        value = embed.fields[3].value
        self.assertLessEqual(len(value), bot.FIELD_VALUE)
        self.assertTrue(value.endswith("more on your dashboard"))
        self.assertTrue(value.splitlines()[-3].startswith("-# jake-main"))  # never ends mid-entry


if __name__ == "__main__":
    unittest.main()
