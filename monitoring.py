import difflib
import ipaddress
import re
import socket
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlparse

from bs4 import BeautifulSoup

MAX_TEXT_LENGTH = 120_000
MAX_COMPARISON_TOKENS = 12_000
VISUAL_CHANGE_THRESHOLD = 8.0
OTHER_TEXT_CHANGE_THRESHOLD = 5.0
OTHER_CHANGED_TOKEN_THRESHOLD = 3

BUSINESS_CATEGORIES = (
    "PRICE",
    "PROMOTION",
    "SHIPPING",
    "PRODUCT",
    "CTA",
    "POSITIONING",
    "OTHER",
)

_INTERNAL_HOST_SUFFIXES = (
    ".internal",
    ".intranet",
    ".lan",
    ".local",
    ".localdomain",
    ".localhost",
    ".home",
    ".corp",
)
_INTERNAL_HOSTNAMES = {
    "broadcasthost",
    "ip6-localhost",
    "ip6-loopback",
    "localhost",
    "metadata",
    "metadata.google.internal",
}
_REMOVED_TAGS = ("script", "style", "noscript", "svg", "template", "canvas")
_BOILERPLATE_ATTRIBUTE_RE = re.compile(
    r"(?:^|[\s_-])(?:breadcrumb|cookie|consent|site-footer|site-nav)(?:$|[\s_-])",
    re.IGNORECASE,
)
_HIDDEN_STYLE_RE = re.compile(
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden)",
    re.IGNORECASE,
)
_VOLATILE_LINE_PATTERNS = (
    re.compile(r"^(?:copyright\b|©)\s*\d{4}(?:\s*[-–]\s*\d{4})?", re.IGNORECASE),
    re.compile(
        r"^(?:last\s+updated|updated|generated|page\s+generated)\b.*"
        r"(?:\d{1,2}:\d{2}|\d{4}[-/]\d{1,2}[-/]\d{1,2})",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\d+\s+(?:people|customers|shoppers)\s+(?:are\s+)?"
        r"(?:viewing|watching|looking)\b",
        re.IGNORECASE,
    ),
)
_COMPARISON_TOKEN_RE = re.compile(
    r"[$€£¥]\s?\d+(?:[.,]\d+)*"
    r"|\d+(?:[.,]\d+)*(?:%|\s+percent)?"
    r"|[^\W_]+(?:['’][^\W_]+)*",
    re.UNICODE,
)
_MONEY_RE = re.compile(
    r"(?:[$€£¥]\s?\d+(?:[.,]\d+)*)"
    r"|(?:\b(?:usd|cad|aud|eur|gbp|jpy)\s?\d+(?:[.,]\d+)*\b)"
    r"|(?:\b\d+(?:[.,]\d+)*\s?(?:usd|cad|aud|eur|gbp|jpy)\b)",
    re.IGNORECASE,
)

_SHIPPING_TERMS = (
    "shipping",
    "delivery",
    "deliver",
    "dispatch",
    "ships",
    "free returns",
)
_PROMOTION_TERMS = (
    "sale",
    "discount",
    "percent off",
    "% off",
    "save",
    "promo",
    "promotion",
    "coupon",
    "offer",
    "deal",
    "limited time",
    "buy one",
    "bogo",
    "clearance",
)
_CTA_TERMS = (
    "shop now",
    "start shopping",
    "buy now",
    "add to cart",
    "order now",
    "get started",
    "learn more",
    "view collection",
    "explore",
    "subscribe",
    "sign up",
)
_PRODUCT_TERMS = (
    "in stock",
    "out of stock",
    "sold out",
    "new arrival",
    "product",
    "collection",
    "available",
    "inventory",
    "variant",
    "preorder",
    "pre order",
    "discontinued",
)
_POSITIONING_TERMS = (
    "premium",
    "sustainable",
    "trusted",
    "leading",
    "best",
    "fastest",
    "built for",
    "made for",
    "designed for",
    "mission",
    "luxury",
    "affordable",
    "handcrafted",
)

AddressResolver = Callable[[str, int], Iterable[str]]


class URLValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ChangedContext:
    old: str
    new: str
    changed_tokens: int


@dataclass(frozen=True)
class ChangeAssessment:
    text_score: float
    meaningful: bool
    categories: tuple[str, ...]
    changed_tokens: int


def _resolve_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise URLValidationError("The hostname could not be resolved.") from exc

    addresses = tuple(
        sorted({record[4][0].split("%", 1)[0] for record in records if record[4]})
    )
    if not addresses:
        raise URLValidationError("The hostname did not resolve to an IP address.")
    return addresses


def _validate_public_address(address: str) -> None:
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError as exc:
        raise URLValidationError("The hostname resolved to an invalid IP address.") from exc
    if not parsed.is_global or parsed.is_multicast:
        raise URLValidationError(
            "Local, private, loopback, link-local, and reserved addresses are not supported."
        )


def validate_public_url(
    url: str,
    *,
    resolver: AddressResolver | None = None,
) -> str:
    candidate = url.strip()
    if (
        not candidate
        or "\\" in candidate
        or any(ord(character) < 32 for character in candidate)
    ):
        raise URLValidationError("Use a full public http/https URL.")

    try:
        parsed = urlparse(candidate)
    except ValueError as exc:
        raise URLValidationError("The URL is malformed.") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise URLValidationError("Use a full public http/https URL.")
    if parsed.username is not None or parsed.password is not None:
        raise URLValidationError("URLs containing credentials are not supported.")

    try:
        host = (parsed.hostname or "").rstrip(".").lower()
    except ValueError as exc:
        raise URLValidationError("The URL contains an invalid hostname.") from exc
    if not host:
        raise URLValidationError("The URL must include a public hostname.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise URLValidationError("The URL contains an invalid hostname.") from exc

    try:
        literal_address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal_address = None
        if (
            host in _INTERNAL_HOSTNAMES
            or "." not in host
            or host.endswith(_INTERNAL_HOST_SUFFIXES)
        ):
            raise URLValidationError("Internal hostnames are not supported.")

    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise URLValidationError("The URL contains an invalid port.") from exc
    if not 1 <= port <= 65535:
        raise URLValidationError("The URL contains an invalid port.")

    if literal_address is None:
        resolve = resolver or _resolve_addresses
        addresses = tuple(resolve(host, port))
        if not addresses:
            raise URLValidationError("The hostname did not resolve to an IP address.")
        for address in addresses:
            _validate_public_address(str(address))
    else:
        _validate_public_address(str(literal_address))

    return candidate


def _normalize_line(line: str) -> str:
    normalized = unicodedata.normalize("NFKC", line)
    normalized = normalized.replace("\u200b", "").replace("\ufeff", "")
    return " ".join(normalized.split())


def _is_volatile_line(line: str) -> bool:
    return any(pattern.search(line) for pattern in _VOLATILE_LINE_PATTERNS)


def normalize_text(text: str) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    current_length = 0
    for raw_line in text.splitlines():
        line = _normalize_line(raw_line)
        if not line or _is_volatile_line(line):
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
        current_length += len(line) + 1
        if current_length >= MAX_TEXT_LENGTH:
            break
    return "\n".join(lines)[:MAX_TEXT_LENGTH]


def extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_REMOVED_TAGS):
        tag.decompose()
    for tag in soup.select(
        "nav, footer, [role='navigation'], [role='contentinfo'], "
        "[hidden], [aria-hidden='true'], input[type='hidden']"
    ):
        tag.decompose()
    for tag in list(soup.find_all(style=True)):
        if _HIDDEN_STYLE_RE.search(str(tag.get("style", ""))):
            tag.decompose()
    boilerplate_tags = []
    for tag in soup.find_all(True):
        attributes = " ".join(
            [
                str(tag.get("id", "")),
                " ".join(str(value) for value in tag.get("class", [])),
            ]
        )
        if _BOILERPLATE_ATTRIBUTE_RE.search(attributes):
            boilerplate_tags.append(tag)
    for tag in boilerplate_tags:
        if tag.parent is not None:
            tag.decompose()

    root = soup.body or soup
    return normalize_text(root.get_text("\n", strip=True))


def _comparison_tokens(text: str) -> list[str]:
    normalized = normalize_text(text).casefold()
    return [
        token.replace(" ", "")
        for token in _COMPARISON_TOKEN_RE.findall(normalized)
    ][:MAX_COMPARISON_TOKENS]


def text_change_score(old: str, new: str) -> float:
    old_tokens = _comparison_tokens(old)
    new_tokens = _comparison_tokens(new)
    if not old_tokens and not new_tokens:
        return 0.0
    if not old_tokens or not new_tokens:
        return 100.0

    matcher = difflib.SequenceMatcher(
        a=old_tokens,
        b=new_tokens,
        autojunk=True,
    )
    matches = sum(block.size for block in matcher.get_matching_blocks())
    ratio = (2 * matches) / (len(old_tokens) + len(new_tokens))
    return round((1 - ratio) * 100, 1)


def _changed_contexts(old: str, new: str) -> tuple[ChangedContext, ...]:
    old_tokens = _comparison_tokens(old)
    new_tokens = _comparison_tokens(new)
    matcher = difflib.SequenceMatcher(
        a=old_tokens,
        b=new_tokens,
        autojunk=True,
    )
    contexts: list[ChangedContext] = []
    context_size = 4
    for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if operation == "equal":
            continue
        old_context = old_tokens[
            max(0, old_start - context_size) : min(
                len(old_tokens), old_end + context_size
            )
        ]
        new_context = new_tokens[
            max(0, new_start - context_size) : min(
                len(new_tokens), new_end + context_size
            )
        ]
        contexts.append(
            ChangedContext(
                old=" ".join(old_context),
                new=" ".join(new_context),
                changed_tokens=max(old_end - old_start, new_end - new_start),
            )
        )
    return tuple(contexts)


def _changed_line_contexts(old: str, new: str) -> tuple[ChangedContext, ...]:
    old_lines = normalize_text(old).splitlines()
    new_lines = normalize_text(new).splitlines()
    old_keys = [" ".join(_comparison_tokens(line)) for line in old_lines]
    new_keys = [" ".join(_comparison_tokens(line)) for line in new_lines]
    matcher = difflib.SequenceMatcher(
        a=old_keys,
        b=new_keys,
        autojunk=True,
    )
    contexts: list[ChangedContext] = []
    for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if operation == "equal":
            continue
        old_block = old_lines[old_start:old_end]
        new_block = new_lines[new_start:new_end]
        if operation == "replace" and len(old_block) == len(new_block):
            pairs = zip(old_block, new_block, strict=True)
        else:
            pairs = [("\n".join(old_block), "\n".join(new_block))]
        for old_line, new_line in pairs:
            contexts.append(
                ChangedContext(
                    old=old_line.casefold(),
                    new=new_line.casefold(),
                    changed_tokens=max(
                        len(_comparison_tokens(old_line)),
                        len(_comparison_tokens(new_line)),
                    ),
                )
            )
    return tuple(contexts)


def _contains_term(text: str, terms: tuple[str, ...]) -> bool:
    return any(
        re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text)
        for term in terms
    )


def _money_values(text: str) -> set[str]:
    return {
        match.group(0).casefold().replace(" ", "")
        for match in _MONEY_RE.finditer(text)
    }


def _classify_context(context: ChangedContext) -> str:
    combined = f"{context.old} {context.new}"
    if _contains_term(combined, _SHIPPING_TERMS):
        return "SHIPPING"
    if _contains_term(combined, _PROMOTION_TERMS):
        return "PROMOTION"
    if _contains_term(combined, _CTA_TERMS):
        return "CTA"
    if _money_values(context.old) != _money_values(context.new) and (
        _money_values(context.old) or _money_values(context.new)
    ):
        return "PRICE"
    if _contains_term(combined, _PRODUCT_TERMS):
        return "PRODUCT"
    if _contains_term(combined, _POSITIONING_TERMS):
        return "POSITIONING"
    return "OTHER"


def classify_business_changes(old: str, new: str) -> tuple[str, ...]:
    contexts = _changed_contexts(old, new)
    if not contexts:
        return ()

    detected = {
        _classify_context(context)
        for context in (*_changed_line_contexts(old, new), *contexts)
    }
    recognized = tuple(
        category
        for category in BUSINESS_CATEGORIES[:-1]
        if category in detected
    )
    return recognized or ("OTHER",)


def assess_change(
    old: str,
    new: str,
    *,
    visual_score: float | None = None,
) -> ChangeAssessment:
    contexts = _changed_contexts(old, new)
    score = text_change_score(old, new)
    changed_tokens = sum(context.changed_tokens for context in contexts)
    categories = classify_business_changes(old, new)
    has_business_category = any(category != "OTHER" for category in categories)

    meaningful_text = bool(contexts) and (
        has_business_category
        or (
            score >= OTHER_TEXT_CHANGE_THRESHOLD
            and changed_tokens >= OTHER_CHANGED_TOKEN_THRESHOLD
        )
    )
    meaningful_visual = (
        visual_score is not None and visual_score >= VISUAL_CHANGE_THRESHOLD
    )
    meaningful = meaningful_text or meaningful_visual
    if not meaningful:
        categories = ()
    elif not categories:
        categories = ("OTHER",)

    return ChangeAssessment(
        text_score=score,
        meaningful=meaningful,
        categories=categories,
        changed_tokens=changed_tokens,
    )


def is_meaningful_change(
    old: str,
    new: str,
    *,
    visual_score: float | None = None,
) -> bool:
    return assess_change(old, new, visual_score=visual_score).meaningful
