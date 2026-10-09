"""Deterministic scam-request rules — the fast path that must fire before money moves.

Rules match request semantics (verb + object), not bare keywords, and skip
refusals / safety advice such as "I won't share my OTP" or "never share your PIN".
"""
import re
from dataclasses import dataclass
from typing import Optional

_W = r"(?:\W+\w+){0,6}?\W+"  # up to 6 filler words between verb and object

_SECRET = (
    r"(?:o\.?\s?t\.?\s?p|one[\s-]time password|verification code|security code|"
    r"(?:the|that|this|six[\s-]digit|4[\s-]digit|6[\s-]digit) (?:code|number)|code (?:we|i) (?:have )?sent|"
    r"(?:upi |atm |m-?)?pin|password|passcode|cvv|cvc|card number|card details|"
    r"expiry date|net ?banking (?:password|id|login|details))"
)
_ASK = (
    r"(?:tell|read|share|send|give|say|provide|forward|confirm|repeat|spell|type|enter|note down|"
    r"need|needs|require|requires|want|wants|ask|asks|asking for|asked for|verify)"
)

_SECRET_REQUEST = re.compile(
    rf"\b{_ASK}\b{_W}{_SECRET}\b"
    rf"|\bwhat(?:'s| is| was)\s+(?:the|your|that)\s+{_SECRET}\b"
    rf"|\b{_SECRET}\b{_W}(?:tell|read|share|send|give)\s+(?:it|that|them)?\s*(?:to\s+)?(?:me|us)\b",
)

_REMOTE_APP = r"(?:any\s?desk|team\s?viewer|quick\s?support|rust\s?desk|air\s?droid|screen\s?share|remote (?:access|app|support app))"
_REMOTE_REQUEST = re.compile(
    rf"\b(?:install|download|open|use|start|enable)\b{_W}{_REMOTE_APP}\b"
    rf"|\bshare\s+(?:your|the)\s+(?:phone\s+)?screen\b"
    rf"|\bgive\s+(?:me|us)\s+(?:remote\s+)?(?:access|control)\b",
)

_PAY_VERB = r"(?:transfer|pay|send|deposit|move)"
_PAY_OBJ = r"(?:money|amount|rupees|rs\.?|inr|payment|fee|fees|charges|balance|funds|\d[\d,]*)"
_PAY_REQUEST = re.compile(rf"\b{_PAY_VERB}\b{_W}{_PAY_OBJ}\b|\b{_PAY_VERB}\s+(?:it|that)\s+(?:now|immediately)\b")
_PAY_PRETEXT = re.compile(
    r"\b(?:now|immediately|right away|urgent(?:ly)?|today|within \w+ (?:minutes|hours)|"
    r"refund|verif(?:y|ication)|kyc|safe account|secure account|penalty|fine|arrest|customs|parcel|"
    r"blocked|suspended|frozen|deactivated|unblock)\b"
)

_QR_REQUEST = re.compile(
    r"\bscan\b(?:\W+\w+){0,4}?\W+(?:qr|q r|code)\b"
    r"|\b(?:approve|accept)\b(?:\W+\w+){0,4}?\W+(?:collect )?request\b"
    r"|\benter\b(?:\W+\w+){0,4}?\W+pin\b(?:\W+\w+){0,4}?\W+(?:receive|get|refund)\b"
)
_GIFTCARD_REQUEST = re.compile(r"\b(?:buy|purchase|get)\b(?:\W+\w+){0,4}?\W+gift ?cards?\b")

_PRESSURE = re.compile(
    r"\b(?:digital arrest|arrest warrant|police|cbi|cyber cell|customs|narcotics|trai|"
    r"(?:sim|number|account|card) (?:will be|has been|is) (?:blocked|suspended|frozen|deactivated|closed)|"
    r"kyc (?:update|expired|pending)|lottery|prize|you (?:have )?won|"
    r"electricity (?:will be )?(?:cut|disconnected)|"
    r"don'?t (?:tell|call|talk to|inform|contact) (?:anyone|anybody|any one|your family)|"
    r"keep (?:this|it) (?:secret|confidential)|"
    r"criminal (?:case|charge|complaint|place|activity)|"
    r"linked to (?:a |an )?(?:criminal|crime|fraud|money laundering)|"
    r"(?:under|during) (?:the )?investigation|"
    r"calling from (?:the )?(?:\w+ )?(?:bank(?:ing)?|head office|rbi|income tax)|"
    r"(?:police|polish|cyber ?crime|crime branch|cbi|income tax|customs|rbi|bank(?:ing)?|telecom|trai) "
    r"(?:department|station|officer|branch|office)|"
    r"(?:we|i) (?:am|are|'m|'re) (?:calling )?from (?:the )?"
    r"(?:police|polish|bank(?:ing)?|rbi|income tax|customs|cbi|cyber|telecom|trai)|"
    r"(?:need|have|must|want) to verify your \w+|"
    r"verify your (?:account|identity|kyc|details|card|information|number))\b"
)

_NEGATION = re.compile(
    r"\b(?:never|don'?t|do not|won'?t|will not|not going to|shouldn'?t|should not|"
    r"must not|no one|nobody|refuse|can'?t|cannot|not)\b"
    r"|\bwill never ask\b"
)

MESSAGES = {
    "secret": (
        "A request to share a verification code, PIN or password was heard. "
        "Don't share it; verify through the official app or a number you looked up yourself."
    ),
    "remote": (
        "A request to install remote-access software or share your screen was heard. "
        "Don't install it; end the call and contact the organisation yourself."
    ),
    "payment": (
        "An urgent payment or transfer request was heard. "
        "Don't send money; verify through the official app or a number you looked up yourself."
    ),
    "qr": (
        "A request to scan a QR code or approve a payment request was heard. "
        "Scanning or entering your PIN sends money — it never receives it."
    ),
    "giftcard": (
        "A request to buy gift cards was heard. Real organisations never ask for payment in gift cards."
    ),
}


@dataclass
class RuleResult:
    level: str                 # "warn" | "watch" | "none"
    label: str = ""
    evidence: str = ""
    segment_id: Optional[str] = None

    @property
    def message(self) -> str:
        return MESSAGES.get(self.label, "")


def _negated(text: str, match: re.Match) -> bool:
    """True if the clause containing the match is a refusal or safety advice."""
    clause_start = max(text.rfind(".", 0, match.start()), text.rfind("?", 0, match.start()),
                       text.rfind("!", 0, match.start()), text.rfind(",", 0, match.start()))
    window = text[clause_start + 1: match.start()]     # only what precedes the match
    return bool(_NEGATION.search(window))


def _scan(text: str) -> RuleResult:
    t = text.lower()

    for label, pattern in (("secret", _SECRET_REQUEST), ("remote", _REMOTE_REQUEST),
                           ("qr", _QR_REQUEST), ("giftcard", _GIFTCARD_REQUEST)):
        for m in pattern.finditer(t):
            if not _negated(t, m):
                return RuleResult("warn", label, text[m.start():m.end()])

    for m in _PAY_REQUEST.finditer(t):
        if _negated(t, m):
            continue
        if _PAY_PRETEXT.search(t):
            return RuleResult("warn", "payment", text[m.start():m.end()])
        return RuleResult("watch", "payment", text[m.start():m.end()])

    m = _PRESSURE.search(t)
    if m and not _negated(t, m):
        return RuleResult("watch", "pressure", text[m.start():m.end()])

    return RuleResult("none")


def evaluate(text: str, segment_id: str, prev_text: str = "") -> RuleResult:
    """Check the new segment; if nothing fires, check it joined with the previous
    one so a request split across a chunk boundary is still caught."""
    result = _scan(text)
    if result.level == "none" and prev_text:
        joined = _scan(f"{prev_text} {text}")
        # Only accept if the joined match isn't fully inside the previous segment.
        if joined.level != "none" and joined.evidence not in prev_text:
            result = joined
            result.evidence = text
    result.segment_id = segment_id if result.level != "none" else None
    return result
