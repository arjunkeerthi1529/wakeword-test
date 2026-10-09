from __future__ import annotations

import re
from dataclasses import dataclass

# Locked for version 1. These IDs are the canonical enum for model schemas,
# API validation, the database, and any UI dropdown. No category
# creation/rename API exists -- adding/removing one means a new build, not a
# runtime config.


@dataclass(frozen=True)
class Category:
    id: str
    display_name: str
    sort_order: int


CATEGORIES: list[Category] = [
    Category("food_dining", "Food & Dining", 1),
    Category("groceries", "Groceries", 2),
    Category("transport", "Transport", 3),
    Category("rent_utilities", "Rent & Utilities", 4),
    Category("shopping", "Shopping", 5),
    Category("health_fitness", "Health & Fitness", 6),
    Category("education", "Education", 7),
    Category("entertainment", "Entertainment", 8),
    Category("subscriptions", "Digital Subscriptions", 9),
    Category("travel", "Travel", 10),
    Category("fees_charges", "Fees & Charges", 11),
    Category("other", "Other", 12),
]

CATEGORY_IDS: frozenset[str] = frozenset(c.id for c in CATEGORIES)
CATEGORY_SEED_VERSION = 1

OTHER_CATEGORY_ID = "other"

# Seeded merchant-key aliases: a narrowly scoped default for well-known
# shorthand. Overridable by any saved user merchant rule, which always wins
# over this. Keys must be normalized via money.normalize_merchant_key before
# lookup.
#
# Deliberately excluded: Amazon, Flipkart, Paytm, UPI, generic bank
# narrations, and anything "EMI"-prefixed -- a generic Amazon line without
# item information should become `other` + needs_review unless the user
# saved a rule, and an EMI line must stay flagged for review rather than
# having its principal/interest split invented. An alias here means
# "confident, no review needed" -- only genuinely unambiguous consumer
# brands belong.
SEEDED_MERCHANT_ALIASES: dict[str, str] = {
    # food_dining -- prepared-food delivery / dine-in chains
    "swiggy": "food_dining",
    "zomato": "food_dining",
    "dominos": "food_dining",
    "pizza hut": "food_dining",
    "mcdonald": "food_dining",
    "kfc": "food_dining",
    "burger king": "food_dining",
    "starbucks": "food_dining",
    "cafe coffee day": "food_dining",
    "barista": "food_dining",
    "faasos": "food_dining",
    "box8": "food_dining",
    # groceries -- quick-commerce / grocery delivery (never prepared food)
    "swiggy instamart": "groceries",
    "instamart": "groceries",
    "zepto": "groceries",
    "blinkit": "groceries",
    "bigbasket": "groceries",
    "big basket": "groceries",
    "dunzo": "groceries",
    "jiomart": "groceries",
    "nature basket": "groceries",
    # transport -- local rides only
    "uber": "transport",
    "ola": "transport",
    "rapido": "transport",
    # travel -- intercity transport/accommodation, distinct from local "transport"
    "irctc": "travel",
    "indian railways": "travel",
    "makemytrip": "travel",
    "goibibo": "travel",
    "yatra": "travel",
    "cleartrip": "travel",
    "spicejet": "travel",
    "indigo": "travel",
    "air india": "travel",
    "vistara": "travel",
    "redbus": "travel",
    # health_fitness
    "apollo pharmacy": "health_fitness",
    "practo": "health_fitness",
    "pharmeasy": "health_fitness",
    "1mg": "health_fitness",
    "netmeds": "health_fitness",
    "cult.fit": "health_fitness",
    "cultfit": "health_fitness",
    # education -- online-learning brands only (not generic institution names)
    "byju": "education",
    "byjus": "education",
    "unacademy": "education",
    "coursera": "education",
    "udemy": "education",
    "vedantu": "education",
    # entertainment -- event/ticketing, not a recurring subscription
    "bookmyshow": "entertainment",
    "book my show": "entertainment",
    "pvr": "entertainment",
    "inox": "entertainment",
    # subscriptions -- recurring streaming/music
    "netflix": "subscriptions",
    "amazon prime": "subscriptions",
    "prime video": "subscriptions",
    "hotstar": "subscriptions",
    "disney+hotstar": "subscriptions",
    "spotify": "subscriptions",
    "wynk": "subscriptions",
    "jiosaavn": "subscriptions",
    "youtube premium": "subscriptions",
    # fees_charges -- a standalone GST line is a tax/fee; never when it's
    # part of an "EMI ..." description, which stays unaliased on purpose.
    "gst": "fees_charges",
}


def is_valid_category(category_id: str) -> bool:
    return category_id in CATEGORY_IDS


def find_alias_category_in_text(normalized_text: str) -> str | None:
    """Longest-keyword-wins, whole-word substring match against
    SEEDED_MERCHANT_ALIASES, for noisy real-world statement descriptions
    (e.g. "raz*zomato online orde,gurgaon") where the merchant name isn't
    isolated on its own. Callers should check is_unresolved_emi_text() first.
    """
    best_category: str | None = None
    best_length = 0
    for alias_key, category_id in SEEDED_MERCHANT_ALIASES.items():
        if len(alias_key) <= best_length:
            continue
        if re.search(rf"\b{re.escape(alias_key)}\b", normalized_text):
            best_category = category_id
            best_length = len(alias_key)
    return best_category


_EMI_PATTERN = re.compile(r"\bemi\b", re.IGNORECASE)


def is_unresolved_emi_text(normalized_text: str) -> bool:
    """An unrecognized EMI must be reviewed -- this build never invents a
    principal/interest split. A real statement's "EMI PRINCIPAL" and "EMI
    INTEREST" lines are the same underlying loan, so categorizing each
    independently via the LLM produces inconsistency (one call returning
    fees_charges, the next returning other, for what's semantically one
    transaction type). Detecting "EMI" deterministically and always routing
    to other+needs_review (never calling the model for it) is both more
    correct and faster than leaving this to the LLM's per-call judgement.
    """
    return bool(_EMI_PATTERN.search(normalized_text))
