"""Dashboard profiles to AYCD profile JSON, for loading into the checkout bots.

Mirrors toAycd() in dashboard.js. The one difference: the profile's name is its
bot name (e.g. "jake-main"), which is unique across members, so the name a bot
posts in its success webhook leads straight back to the member.
"""

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AS": "American Samoa", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida",
    "GA": "Georgia", "GU": "Guam", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "MP": "Northern Mariana Islands", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "PR": "Puerto Rico", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UM": "U.S. Minor Outlying Islands", "UT": "Utah",
    "VT": "Vermont", "VI": "U.S. Virgin Islands", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

CARD_TYPES = {"Visa": "Visa", "Mastercard": "MasterCard", "Amex": "AmericanExpress", "Discover": "Discover"}


def to_aycd(p: dict, name: str, card: dict | None) -> dict:
    extra = p.get("aycd") or {}
    shipping = {
        "name": p.get("name", ""), "email": p.get("email", ""), "phone": "".join(c for c in p.get("phone", "") if c.isdigit()),
        "line1": p.get("line1", ""), "line2": p.get("line2", ""), "line3": extra.get("line3", ""),
        "postCode": p.get("zip", ""), "city": p.get("city", ""), "country": "United States",
        "state": US_STATES.get(p.get("state", ""), p.get("state", "")),
    }
    mm, _, yy = (p.get("exp") or "").partition("/")
    return {
        "name": name,
        "size": extra.get("size", ""),
        "profileGroup": extra.get("profileGroup", ""),
        "billingAddress": extra.get("billing") or dict(shipping),
        "shippingAddress": shipping,
        "paymentDetails": {
            "nameOnCard": p.get("cardName") or p.get("name", ""),
            "cardType": CARD_TYPES.get(p.get("brand", ""), p.get("brand", "")),
            "cardNumber": card["number"] if card else "",
            "cardExpMonth": mm,
            "cardExpYear": f"20{yy}" if yy else "",
            "cardCvv": card["cvv"] if card else "",
        },
        "sameBillingAndShippingAddress": not extra.get("billing"),
        "onlyCheckoutOnce": bool(extra.get("onlyCheckoutOnce")),
        "matchNameOnCardAndAddress": (p.get("cardName") or p.get("name")) == p.get("name"),
        "sites": p.get("sites", []),
    }
