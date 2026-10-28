"""Reads checkout webhooks posted by AIO bots (Refract, Shikari, Valor, Polar,
Stellar, Hayha, Sniped and others) into one shape.

Every bot formats its embed a little differently, so this reads by meaning
rather than by bot: the headline says whether it's a success or a decline, and
the fields (or "Key: Value" lines in the description) carry the profile,
product, site, price and so on under a handful of common names.

parse_message() takes a plain dict so it can be tested without Discord:
    {"content": str, "author": str,
     "embeds": [{"title", "description", "author", "footer", "fields": [{"name", "value"}]}]}
"""
import html
import re

BOTS = ["Refract", "Shikari", "Valor", "Polar", "Stellar", "Hayha", "Sniped"]
# Bots that sign their webhooks with another name: Refract's footer says "Prism Technologies".
BOT_ALIASES = {"prism technologies": "Refract"}
# Bots whose Price is for one item rather than the whole order.
PRICE_EACH = {"Refract"}

# Checked before SUCCESS, so "Unsuccessful" or "Checkout failed" count as declines.
FAILURE = re.compile(r"declin|fail|unsuccess|error|denied|rejected|payment (issue|problem)|card (issue|problem)", re.I)
SUCCESS = re.compile(r"success|checked out|checkout (complete|confirmed)|order (placed|confirmed|submitted)|copped|cooked", re.I)
CHECKOUT_WORD = re.compile(r"check\s?out", re.I)

# Field names, compared lower-case with anything but letters and digits removed.
FIELDS = {
    "profile": ["profile", "profilename", "profiles", "billingprofile", "checkoutprofile"],
    "product": ["product", "productname", "item", "itemname", "products", "title"],
    "site": ["site", "store", "retailer", "website", "sitename", "storename", "shop", "module"],
    "price": ["price", "total", "ordertotal", "cost", "subtotal", "amount"],
    "qty": ["quantity", "qty"],
    "order": ["order", "ordernumber", "orderid", "orderno", "ordernum", "confirmation", "confirmationnumber"],
    "size": ["size", "variant"],
    "sku": ["sku", "pid", "tcin", "productid", "itemid"],
    "email": ["email", "account", "accountemail", "login"],
    "mode": ["mode", "taskmode"],
}
# Prefix matches for names like "Profile Name (Hidden)" or "Order # 123".
PREFIXES = {"profile": "profile", "order": "order", "product": "product", "quantity": "qty"}

EMPTY = {"", "n/a", "na", "none", "null", "unknown", "-", "—", "hidden"}


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def clean(value) -> str:
    """Strips Discord markdown (spoilers, code, bold, links, <url> wrappers, list
    bullets) and decodes HTML codes some bots send, like Polar's Pok&#233;mon."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [label](url) -> label
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = re.sub(r"\|\||```|`|\*\*|__|~~", "", text)
    text = re.sub(r"(?<!\S)[*_](\S(?:.*?\S)?)[*_](?!\S)", r"\1", text)  # *italic* / _italic_
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)  # "- item" list bullets
    text = text.strip()
    return "" if text.lower() in EMPTY else text


def _field_for(name: str):
    k = _key(name)
    for field, names in FIELDS.items():
        if k in names:
            return field
    for prefix, field in PREFIXES.items():
        if k.startswith(prefix):
            return field
    return None


def _description_pairs(text: str):
    """'Key: Value' lines, as some bots put details in the description instead of fields."""
    for line in (text or "").splitlines():
        m = re.match(r"^\s*[*_>]*\s*([A-Za-z][A-Za-z #/()]{1,30}?)\s*[*_]*\s*[:：]\s*(.+)$", line)
        if m:
            yield m.group(1), m.group(2)


def known_retailer(text: str) -> str:
    """One of our stores if the text names it, or ''."""
    s = (text or "").lower()
    if "target" in s:
        return "Target"
    if "pokemon" in s or "pokémon" in s:
        return "Pokemon Center"
    if "walmart" in s:
        return "Walmart"
    if "amazon" in s:
        return "Amazon"
    if "topps" in s:
        return "Topps"
    return ""


def retailer_name(site: str) -> str:
    return known_retailer(site) or site.strip()


def _price(text: str):
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)", text or "")
    return float(m.group(1).replace(",", "")) if m else None


def _qty(text: str) -> int:
    m = re.search(r"\d+", text or "")
    return max(1, int(m.group())) if m else 1


LEADING_QTY = re.compile(r"^(\d{1,3})\s*[x×]\s+(\S.*)$", re.I)


def split_qty(product: str) -> tuple[str, str]:
    """'2x Pokémon Booster Bundle' -> ('2', 'Pokémon Booster Bundle'). No count in front: ('', product)."""
    m = LEADING_QTY.match(product or "")
    return (m.group(1), m.group(2)) if m and int(m.group(1)) > 0 else ("", product)


def is_status(text: str) -> bool:
    """Whether a line is a headline like "Successful Checkout!" rather than a product."""
    return bool(SUCCESS.search(text) or FAILURE.search(text) or CHECKOUT_WORD.search(text))


def trim_size(product: str, size: str = "") -> str:
    """'Elite Trainer Box - Default' -> 'Elite Trainer Box'."""
    for suffix in {"default", (size or "").strip().lower()} - {""}:
        if product.lower().endswith(f" - {suffix}"):
            return product[:-len(suffix) - 3].rstrip()
    return product


def _source(*texts: str) -> str:
    joined = " ".join(t for t in texts if t).lower()
    for alias, bot in BOT_ALIASES.items():
        if alias in joined:
            return bot
    for bot in BOTS:
        if bot.lower() in joined:
            return bot
    return ""


def parse_embed(embed: dict, content: str = "", sender: str = ""):
    """One embed -> a checkout dict, or None if it isn't a checkout or decline."""
    title = clean(embed.get("title"))
    author = clean(embed.get("author"))
    footer = clean(embed.get("footer"))
    description = embed.get("description") or ""
    first_line = clean(description.splitlines()[0]) if description.strip() else ""
    headline = " ".join(t for t in (title, author, first_line, clean(content)) if t)

    found = {}
    pairs = [(f.get("name", ""), f.get("value", "")) for f in embed.get("fields") or []]
    pairs += list(_description_pairs(description))
    for name, value in pairs:
        field = _field_for(clean(name))
        value = clean(value)
        if field and value and field not in found:
            found[field] = value

    if FAILURE.search(headline):
        status = "decline"
    elif SUCCESS.search(headline) or (CHECKOUT_WORD.search(headline) and "profile" in found):
        status = "success"
    else:
        return None

    product = found.get("product")
    # Some bots put the product in the title and the status in the author line.
    if not product and title and not is_status(title):
        product = title
    # Shikari puts it on its own line under the title, in the description.
    if not product:
        product = next((clean(line) for line in description.splitlines()
                        if clean(line) and not is_status(clean(line)) and not list(_description_pairs(line))), "")
    qty = found.get("qty", "")
    # Bots without a Quantity field (Polar) lead the product with it instead: "2x Pokémon ...".
    if not qty and product:
        qty, product = split_qty(product)
    # Polar adds the size to the product: "... Elite Trainer Box - Default".
    product = trim_size(product or "", found.get("size", ""))
    source = _source(author, footer, title, content, sender)
    price = _price(found.get("price", ""))
    if price and source in PRICE_EACH:
        price = round(price * _qty(qty), 2)

    return {
        "status": status,
        "profile": found.get("profile", ""),
        "product": product or "",
        # Refract has no Site field; its title ends with the store: "Successful Checkout | Target".
        "retailer": retailer_name(found["site"]) if found.get("site") else known_retailer(title.rpartition("|")[2] if "|" in title else ""),
        "price": price,
        "qty": _qty(qty),
        "order": found.get("order", "").lstrip("#").strip(),
        "size": found.get("size", ""),
        "sku": found.get("sku", ""),
        "email": found.get("email", ""),
        "source": source,
    }


def parse_message(message: dict) -> list[dict]:
    """All checkouts and declines in one Discord message (bots may batch several embeds)."""
    results = []
    for embed in message.get("embeds") or []:
        parsed = parse_embed(embed, message.get("content", ""), message.get("author", ""))
        if parsed:
            results.append(parsed)
    return results
