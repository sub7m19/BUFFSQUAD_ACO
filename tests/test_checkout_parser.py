"""Checkout webhook parsing. The sample embeds follow the layouts these bots use;
paste a real webhook from the channel into SAMPLES to pin down its exact format."""
import unittest

from checkout_parser import clean, parse_message


def embed(title="", description="", author="", footer="", fields=()):
    return {"title": title, "description": description, "author": author, "footer": footer,
            "fields": [{"name": n, "value": v} for n, v in fields]}


SAMPLES = {
    "Refract": embed(title="Successful Checkout", footer="Refract", fields=[
        ("Site", "Target"), ("Product", "Prismatic Evolutions Elite Trainer Box"), ("Price", "$54.99"),
        ("Quantity", "2"), ("Profile", "||jake-main||"), ("Order Number", "||#912004417||")]),
    "Shikari": embed(title="Checkout Success :tada:", author="Shikari", fields=[
        ("Store", "Pokemon Center"), ("Item", "[Pokemon 151 Booster Bundle](https://www.pokemoncenter.com/x)"),
        ("Total", "$29.99"), ("Profile Name", "`jake-main`"), ("Order #", "P001234567")]),
    "Valor": embed(title="Surging Sparks Booster Box", author="Valor | Successful Checkout!", footer="Valor AIO", fields=[
        ("Site", "Target US"), ("Price", "159.99"), ("Mode", "Fast"), ("Profile", "||Jake-Main||"), ("Qty", "1")]),
    "Polar": embed(title="Polar | Successfully Checked Out", fields=[
        ("Module", "PokemonCenter"), ("Product", "Paldean Fates ETB"), ("Price", "$49.99"), ("Profile", "jake-main")]),
    "Stellar": embed(title="Successfully checked out!", footer="Stellar AIO", fields=[
        ("Store", "target"), ("Product", "**Destined Rivals Booster Bundle**"), ("Price", "$26.94"),
        ("Profile", "||jake-main||"), ("Order", "||912999||"), ("Email", "||j***@gmail.com||")]),
    "Hayha": embed(title="Hayha Checkout: Successful", description=(
        "**Site:** Target\n**Product:** Journey Together ETB\n**Price:** $49.99\n**Profile:** jake-main")),
    "Sniped": embed(title="Sniped Checkout!", fields=[
        ("Retailer", "Pokemon Center"), ("Product", "Prismatic Evolutions Surprise Box"), ("Profile", "jake-main")]),
}


class ParseCheckouts(unittest.TestCase):
    def test_every_bot_parses_as_success_with_profile(self):
        for bot, sample in SAMPLES.items():
            with self.subTest(bot=bot):
                [c] = parse_message({"embeds": [sample]})
                self.assertEqual(c["status"], "success")
                self.assertEqual(c["profile"].lower(), "jake-main")
                self.assertIn(c["retailer"], ("Target", "Pokemon Center"))
                self.assertTrue(c["product"])
                self.assertEqual(c["source"], bot)

    def test_fields(self):
        [c] = parse_message({"embeds": [SAMPLES["Refract"]]})
        self.assertEqual((c["price"], c["qty"], c["order"]), (54.99, 2, "912004417"))
        [c] = parse_message({"embeds": [SAMPLES["Shikari"]]})
        self.assertEqual(c["product"], "Pokemon 151 Booster Bundle")
        self.assertEqual(c["order"], "P001234567")

    def test_product_from_title_when_status_is_in_author(self):
        [c] = parse_message({"embeds": [SAMPLES["Valor"]]})
        self.assertEqual(c["product"], "Surging Sparks Booster Box")
        self.assertEqual(c["retailer"], "Target")

    def test_declines(self):
        for title in ("Checkout Failed", "Card Declined", "Unsuccessful Checkout", "Payment Declined :x:"):
            with self.subTest(title=title):
                [c] = parse_message({"embeds": [embed(title=title, fields=[("Profile", "jake-main"), ("Site", "Target")])]})
                self.assertEqual(c["status"], "decline")

    def test_ignores_other_messages(self):
        self.assertEqual(parse_message({"embeds": [embed(title="Restock alert", fields=[("Site", "Target")])]}), [])
        self.assertEqual(parse_message({"content": "gm", "embeds": []}), [])

    def test_several_embeds_in_one_message(self):
        result = parse_message({"embeds": [SAMPLES["Refract"], SAMPLES["Polar"]]})
        self.assertEqual(len(result), 2)

    def test_clean(self):
        self.assertEqual(clean("||`jake-main`||"), "jake-main")
        self.assertEqual(clean("[Item](https://x.y)"), "Item")
        self.assertEqual(clean("**Bold** *it*"), "Bold it")
        self.assertEqual(clean("N/A"), "")


if __name__ == "__main__":
    unittest.main()
