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
import re

BOTS = ["Refract", "Shikari", "Valor", "Polar", "Stellar", "Hayha", "Sniped"]

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
    """Strips Discord markdown: spoilers, code, bold, links, and <url> wrappers."""
    text = str(value or "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [label](url) -> label
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = re.sub(r"\|\||```|`|\*\*|__|~~", "", text)
    text = re.sub(r"(?<!\S)[*_](\S(?:.*?\S)?)[*_](?!\S)", r"\1", text)  # *italic* / _italic_
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


def retailer_name(site: str) -> str:
    s = (site or "").lower()
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
    return site.strip()


def _price(text: str):
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)", text or "")
    return float(m.group(1).replace(",", "")) if m else None


def _qty(text: str) -> int:
    m = re.search(r"\d+", text or "")
    return max(1, int(m.group())) if m else 1


def _source(*texts: str) -> str:
    joined = " ".join(t for t in texts if t).lower()
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
    if not product and title and not SUCCESS.search(title) and not FAILURE.search(title) and not CHECKOUT_WORD.search(title):
        product = title

    return {
        "status": status,
        "profile": found.get("profile", ""),
        "product": product or "",
        "retailer": retailer_name(found.get("site", "")),
        "price": _price(found.get("price", "")),
        "qty": _qty(found.get("qty", "")),
        "order": found.get("order", "").lstrip("#").strip(),
        "size": found.get("size", ""),
        "sku": found.get("sku", ""),
        "email": found.get("email", ""),
        "source": _source(author, footer, title, content, sender),
    }


def parse_message(message: dict) -> list[dict]:
    """All checkouts and declines in one Discord message (bots may batch several embeds)."""
    results = []
    for embed in message.get("embeds") or []:
        parsed = parse_embed(embed, message.get("content", ""), message.get("author", ""))
        if parsed:
            results.append(parsed)
    return results
