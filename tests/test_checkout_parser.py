"""Checkout webhook parsing. The sample embeds follow the layouts these bots use;
paste a real webhook from the channel into SAMPLES to pin down its exact format."""
import unittest

from checkout_parser import clean, parse_message


def embed(title="", description="", author="", footer="", fields=()):
    return {"title": title, "description": description, "author": author, "footer": footer,
            "fields": [{"name": n, "value": v} for n, v in fields]}


SAMPLES = {
    # As Refract posts it: signed "Prism Technologies", the store only in the title, Price for one item.
    "Refract": embed(title="Successful Checkout | Target", footer="Prism Technologies - 1.56.1 (1.0.149)", fields=[
        ("Product", "[Pokémon Trading Card Game: 30th Celebration Elite Trainer Box](https://www.target.com/p/x)"),
        ("Price", "$69.99"), ("Profile", "jake-main"), ("Proxy Details", "||1.2.3.4||"), ("Quantity", "2"),
        ("Order Number", "102003841860229"), ("Fraud Status", "SUCCESS"), ("Harvester", "buffloved 8"), ("Email", "j@x.com")]),
    # As Shikari posts it: the product is a link on its own line under the title, and there's no price.
    "Shikari": embed(title="Successful Checkout!", author="Shikari", footer="@shikaritech",
                     description="[Pokémon Trading Card Game: 30th Celebration Knock Out Collection](https://www.target.com/p/x)",
                     fields=[("Site", "Target"), ("Profile", "jake-main"), ("Order ID", "912003841971019"), ("Quantity", "2"), ("Proxy", "1.2.3.4")]),
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

    def test_sniped_email_under_the_profile_is_left_out(self):
        [c] = parse_message({"embeds": [embed(title="Sniped Checkout!", fields=[
            ("Retailer", "Target"), ("Profile", "||jake-main-558||\n||someone@example.com||")])]})
        self.assertEqual((c["profile"], c["email"]), ("jake-main-558", "someone@example.com"))

    def test_fields(self):
        # Refract's $69.99 is for one item: the order total is 2 x $69.99.
        [c] = parse_message({"embeds": [SAMPLES["Refract"]]})
        self.assertEqual((c["price"], c["qty"], c["order"], c["retailer"], c["source"]), (139.98, 2, "102003841860229", "Target", "Refract"))
        self.assertEqual(c["product"], "Pokémon Trading Card Game: 30th Celebration Elite Trainer Box")
        [c] = parse_message({"embeds": [SAMPLES["Shikari"]]})
        self.assertEqual(c["product"], "Pokémon Trading Card Game: 30th Celebration Knock Out Collection")
        self.assertEqual((c["order"], c["retailer"], c["qty"], c["price"]), ("912003841971019", "Target", 2, None))

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


    def test_polar_html_codes_and_bullet(self):
        # As Polar AIO posts it: the product is a "- " bullet with the é sent as &#233;.
        polar = embed(title="Successful Checkout 🎉", footer="Polar AIO", fields=[
            ("Product", "- 2x [Pok&#233;mon Trading Card Game: 30th Celebration Booster Bundle Box](https://www.target.com/p/x) - Default"),
            ("Price", "$63.98"), ("Size", "Default"), ("Site", "Target"), ("Profile", "jake-main"),
            ("Order Number", "102003841766417"), ("Fraud Status", "REVIEW_HOLD")])
        [c] = parse_message({"embeds": [polar]})
        # Polar has no Quantity field; the "2x" in front of the product is the quantity.
        # Its " - Default" size ending is trimmed, so the same item from every bot reads the same.
        self.assertEqual(c["product"], "Pokémon Trading Card Game: 30th Celebration Booster Bundle Box")
        self.assertEqual((c["qty"], c["source"], c["retailer"], c["price"]), (2, "Polar", "Target", 63.98))

    def test_leading_count_only_without_a_quantity_field(self):
        with_field = embed(title="Successful Checkout", fields=[("Product", "2x Booster Bundle"), ("Quantity", "3"), ("Profile", "jake-main")])
        [c] = parse_message({"embeds": [with_field]})
        self.assertEqual((c["qty"], c["product"]), (3, "2x Booster Bundle"))
        plain = embed(title="Successful Checkout", fields=[("Product", "151 Booster Bundle"), ("Profile", "jake-main")])
        [c] = parse_message({"embeds": [plain]})
        self.assertEqual((c["qty"], c["product"]), (1, "151 Booster Bundle"))


if __name__ == "__main__":
    unittest.main()
