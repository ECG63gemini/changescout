import difflib
import ipaddress
import json
import re
import socket
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from bs4 import BeautifulSoup

MAX_TEXT_LENGTH = 120_000
MAX_COMPARISON_TOKENS = 12_000
VISUAL_CHANGE_THRESHOLD = 8.0
OTHER_TEXT_CHANGE_THRESHOLD = 5.0
OTHER_CHANGED_TOKEN_THRESHOLD = 3
MAX_STRUCTURED_CHANGES = 10
MAX_CHANGE_VALUE_LENGTH = 240

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
_MONEY_VALUE_RE = re.compile(
    r"(?P<symbol>[$€£¥])\s?"
    r"(?P<symbol_amount>\d+(?:,\d{3})*(?:\.\d+)?)"
    r"|(?P<code>USD|CAD|AUD|EUR|GBP|JPY)\s?"
    r"(?P<code_amount>\d+(?:,\d{3})*(?:\.\d+)?)"
    r"|(?P<trailing_amount>\d+(?:,\d{3})*(?:\.\d+)?)\s?"
    r"(?P<trailing_code>USD|CAD|AUD|EUR|GBP|JPY)\b",
    re.IGNORECASE,
)
_PERCENT_OFF_RE = re.compile(
    r"\b(?P<amount>\d+(?:\.\d+)?)\s*%\s*off\b",
    re.IGNORECASE,
)
_PROMOTION_RE = re.compile(
    r"\b(?:"
    r"\d+(?:\.\d+)?\s*(?:%|percent)\s*off"
    r"|sale"
    r"|discount"
    r"|buy\s+one\s+get\s+one"
    r"|bogo"
    r"|free\s+gift"
    r"|limited\s+time"
    r"|coupon"
    r"|promo(?:tion)?(?:\s+code)?"
    r")\b",
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
    "buy one get one",
    "bogo",
    "free gift",
    "clearance",
    "promo code",
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


@dataclass(frozen=True)
class StructuredChange:
    category: str
    old_value: str
    new_value: str
    description: str
    importance: int


@dataclass(frozen=True)
class _LineRecord:
    index: int
    total: int
    text: str
    key: str
    context: str


@dataclass(frozen=True)
class _MoneyValue:
    raw: str
    currency: str
    amount: Decimal


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


def _line_key(text: str) -> str:
    value = _MONEY_RE.sub(" money ", text.casefold())
    value = re.sub(r"\d+(?:[.,]\d+)*(?:%|\s+percent)?", " number ", value)
    return " ".join(re.findall(r"[^\W_]+", value, re.UNICODE))


def _line_context(lines: list[str], index: int) -> str:
    nearby = (
        lines[max(0, index - 1) : index]
        + lines[index + 1 : min(len(lines), index + 2)]
    )
    return " | ".join(_line_key(line) for line in nearby)


def _line_record(lines: list[str], index: int) -> _LineRecord:
    return _LineRecord(
        index=index,
        total=len(lines),
        text=lines[index][:MAX_CHANGE_VALUE_LENGTH],
        key=_line_key(lines[index]),
        context=_line_context(lines, index),
    )


def _changed_line_records(
    old: str,
    new: str,
) -> tuple[tuple[_LineRecord, ...], tuple[_LineRecord, ...]]:
    old_lines = normalize_text(old).splitlines()
    new_lines = normalize_text(new).splitlines()
    matcher = difflib.SequenceMatcher(
        a=[line.casefold() for line in old_lines],
        b=[line.casefold() for line in new_lines],
        autojunk=True,
    )
    old_indexes: set[int] = set()
    new_indexes: set[int] = set()
    for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if operation == "equal":
            continue
        old_indexes.update(range(old_start, old_end))
        new_indexes.update(range(new_start, new_end))

    return (
        tuple(_line_record(old_lines, index) for index in sorted(old_indexes)),
        tuple(_line_record(new_lines, index) for index in sorted(new_indexes)),
    )


def _text_similarity(old: str, new: str) -> float:
    if not old and not new:
        return 1.0
    return difflib.SequenceMatcher(
        a=old,
        b=new,
        autojunk=False,
    ).ratio()


def _record_pair_score(old: _LineRecord, new: _LineRecord) -> float:
    old_position = old.index / max(1, old.total - 1)
    new_position = new.index / max(1, new.total - 1)
    position_similarity = max(0.0, 1 - abs(old_position - new_position))
    return (
        (_text_similarity(old.key, new.key) * 0.65)
        + (_text_similarity(old.context, new.context) * 0.25)
        + (position_similarity * 0.1)
    )


def _pair_records(
    old_records: Iterable[_LineRecord],
    new_records: Iterable[_LineRecord],
    *,
    minimum_score: float,
) -> tuple[
    tuple[tuple[_LineRecord, _LineRecord], ...],
    tuple[_LineRecord, ...],
    tuple[_LineRecord, ...],
]:
    old_items = tuple(old_records)
    new_items = tuple(new_records)
    candidates = sorted(
        (
            (_record_pair_score(old, new), old.index, new.index, old, new)
            for old in old_items
            for new in new_items
        ),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    used_old: set[int] = set()
    used_new: set[int] = set()
    pairs: list[tuple[_LineRecord, _LineRecord]] = []
    for score, _, _, old, new in candidates:
        if score < minimum_score:
            break
        if old.index in used_old or new.index in used_new:
            continue
        pairs.append((old, new))
        used_old.add(old.index)
        used_new.add(new.index)

    pairs.sort(key=lambda pair: (pair[0].index, pair[1].index))
    return (
        tuple(pairs),
        tuple(item for item in old_items if item.index not in used_old),
        tuple(item for item in new_items if item.index not in used_new),
    )


def _pair_records_by_position(
    old_records: Iterable[_LineRecord],
    new_records: Iterable[_LineRecord],
) -> tuple[
    tuple[tuple[_LineRecord, _LineRecord], ...],
    tuple[_LineRecord, ...],
    tuple[_LineRecord, ...],
]:
    old_items = tuple(old_records)
    remaining_new = list(new_records)
    pairs: list[tuple[_LineRecord, _LineRecord]] = []
    unmatched_old: list[_LineRecord] = []
    for old in old_items:
        if not remaining_new:
            unmatched_old.append(old)
            continue
        old_position = old.index / max(1, old.total - 1)
        new = min(
            remaining_new,
            key=lambda item: (
                abs(
                    old_position
                    - (item.index / max(1, item.total - 1))
                ),
                item.index,
            ),
        )
        remaining_new.remove(new)
        pairs.append((old, new))
    return tuple(pairs), tuple(unmatched_old), tuple(remaining_new)


def _parse_money_values(text: str) -> tuple[_MoneyValue, ...]:
    values: list[_MoneyValue] = []
    for match in _MONEY_VALUE_RE.finditer(text):
        currency = (
            match.group("symbol")
            or match.group("code")
            or match.group("trailing_code")
        )
        amount_text = (
            match.group("symbol_amount")
            or match.group("code_amount")
            or match.group("trailing_amount")
        )
        if not currency or not amount_text:
            continue
        try:
            amount = Decimal(amount_text.replace(",", ""))
        except InvalidOperation:
            continue
        values.append(
            _MoneyValue(
                raw=match.group(0).strip(),
                currency=(
                    currency
                    if currency in {"$", "€", "£", "¥"}
                    else currency.upper()
                ),
                amount=amount,
            )
        )
    return tuple(values)


def _format_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _format_money(currency: str, value: Decimal) -> str:
    amount = _format_decimal(value)
    if currency in {"$", "€", "£", "¥"}:
        return f"{currency}{amount}"
    return f"{currency} {amount}"


def _change_position(
    old: _LineRecord | None,
    new: _LineRecord | None,
) -> float:
    record = new or old
    if record is None:
        return 1.0
    return record.index / max(1, record.total - 1)


def _is_shipping_line(text: str) -> bool:
    lowered = text.casefold()
    return "free" in lowered and _contains_term(lowered, _SHIPPING_TERMS)


def _is_promotion_line(text: str) -> bool:
    return bool(_PROMOTION_RE.search(text))


def _is_cta_line(text: str) -> bool:
    normalized = " ".join(re.findall(r"[^\W_]+", text.casefold(), re.UNICODE))
    return normalized in _CTA_TERMS


def _price_changes(
    old_records: tuple[_LineRecord, ...],
    new_records: tuple[_LineRecord, ...],
) -> list[tuple[float, StructuredChange]]:
    def candidate(record: _LineRecord) -> bool:
        money = _parse_money_values(record.text)
        lowered = record.text.casefold()
        if len(money) != 1 or len(record.text.split()) > 16:
            return False
        if _is_shipping_line(record.text):
            return False
        if _is_promotion_line(record.text) and "price" not in lowered:
            return False
        return True

    pairs, _, _ = _pair_records(
        (record for record in old_records if candidate(record)),
        (record for record in new_records if candidate(record)),
        minimum_score=0.62,
    )
    changes: list[tuple[float, StructuredChange]] = []
    for old, new in pairs:
        old_money = _parse_money_values(old.text)[0]
        new_money = _parse_money_values(new.text)[0]
        if (
            old_money.currency != new_money.currency
            or old_money.amount == new_money.amount
        ):
            continue
        difference = abs(new_money.amount - old_money.amount)
        direction = (
            "decreased"
            if new_money.amount < old_money.amount
            else "increased"
        )
        description = (
            f"Price {direction} by "
            f"{_format_money(old_money.currency, difference)}"
        )
        if old_money.amount:
            percentage = (difference / old_money.amount) * Decimal("100")
            description += f" ({percentage.quantize(Decimal('0.1'))}%)"
        changes.append(
            (
                _change_position(old, new),
                StructuredChange(
                    category="PRICE",
                    old_value=old_money.raw,
                    new_value=new_money.raw,
                    description=f"{description}.",
                    importance=5,
                ),
            )
        )
    return changes


def _shipping_changes(
    old_records: tuple[_LineRecord, ...],
    new_records: tuple[_LineRecord, ...],
) -> list[tuple[float, StructuredChange]]:
    def candidate(record: _LineRecord) -> bool:
        return (
            _is_shipping_line(record.text)
            and len(_parse_money_values(record.text)) == 1
            and len(record.text.split()) <= 20
        )

    pairs, _, _ = _pair_records(
        (record for record in old_records if candidate(record)),
        (record for record in new_records if candidate(record)),
        minimum_score=0.58,
    )
    changes: list[tuple[float, StructuredChange]] = []
    for old, new in pairs:
        old_money = _parse_money_values(old.text)[0]
        new_money = _parse_money_values(new.text)[0]
        if (
            old_money.currency != new_money.currency
            or old_money.amount == new_money.amount
        ):
            continue
        difference = abs(new_money.amount - old_money.amount)
        direction = (
            "decreased"
            if new_money.amount < old_money.amount
            else "increased"
        )
        changes.append(
            (
                _change_position(old, new),
                StructuredChange(
                    category="SHIPPING",
                    old_value=old.text,
                    new_value=new.text,
                    description=(
                        f"Free-shipping threshold {direction} by "
                        f"{_format_money(old_money.currency, difference)}."
                    ),
                    importance=5,
                ),
            )
        )
    return changes


def _cta_changes(
    old_records: tuple[_LineRecord, ...],
    new_records: tuple[_LineRecord, ...],
) -> list[tuple[float, StructuredChange]]:
    pairs, _, _ = _pair_records_by_position(
        (record for record in old_records if _is_cta_line(record.text)),
        (record for record in new_records if _is_cta_line(record.text)),
    )
    return [
        (
            _change_position(old, new),
            StructuredChange(
                category="CTA",
                old_value=old.text,
                new_value=new.text,
                description="Call to action changed.",
                importance=3,
            ),
        )
        for old, new in pairs
        if old.text.casefold() != new.text.casefold()
    ]


def _promotion_description(old: str | None, new: str | None) -> str:
    if old is None:
        return "Promotion added."
    if new is None:
        return "Promotion removed."
    old_percent = _PERCENT_OFF_RE.search(old)
    new_percent = _PERCENT_OFF_RE.search(new)
    if old_percent and new_percent:
        old_value = Decimal(old_percent.group("amount"))
        new_value = Decimal(new_percent.group("amount"))
        if old_value != new_value:
            difference = abs(new_value - old_value)
            direction = "increased" if new_value > old_value else "decreased"
            return (
                f"Discount {direction} by {_format_decimal(difference)} "
                "percentage points."
            )
    return "Promotion changed."


def _promotion_changes(
    old_records: tuple[_LineRecord, ...],
    new_records: tuple[_LineRecord, ...],
) -> list[tuple[float, StructuredChange]]:
    old_promotions = tuple(
        record
        for record in old_records
        if _is_promotion_line(record.text)
        and not _is_shipping_line(record.text)
        and len(record.text.split()) <= 24
    )
    new_promotions = tuple(
        record
        for record in new_records
        if _is_promotion_line(record.text)
        and not _is_shipping_line(record.text)
        and len(record.text.split()) <= 24
    )
    pairs, unmatched_old, unmatched_new = _pair_records(
        old_promotions,
        new_promotions,
        minimum_score=0.38,
    )
    changes: list[tuple[float, StructuredChange]] = []
    for old, new in pairs:
        if old.text.casefold() == new.text.casefold():
            continue
        changes.append(
            (
                _change_position(old, new),
                StructuredChange(
                    category="PROMOTION",
                    old_value=old.text,
                    new_value=new.text,
                    description=_promotion_description(old.text, new.text),
                    importance=5,
                ),
            )
        )
    for old in unmatched_old:
        changes.append(
            (
                _change_position(old, None),
                StructuredChange(
                    category="PROMOTION",
                    old_value=old.text,
                    new_value="No visible promotion",
                    description=_promotion_description(old.text, None),
                    importance=5,
                ),
            )
        )
    for new in unmatched_new:
        changes.append(
            (
                _change_position(None, new),
                StructuredChange(
                    category="PROMOTION",
                    old_value="No visible promotion",
                    new_value=new.text,
                    description=_promotion_description(None, new.text),
                    importance=5,
                ),
            )
        )
    return changes


def _changed_token_count(old: str, new: str) -> int:
    matcher = difflib.SequenceMatcher(
        a=_comparison_tokens(old),
        b=_comparison_tokens(new),
        autojunk=False,
    )
    return sum(
        max(old_end - old_start, new_end - new_start)
        for operation, old_start, old_end, new_start, new_end
        in matcher.get_opcodes()
        if operation != "equal"
    )


def _is_positioning_candidate(record: _LineRecord) -> bool:
    word_count = len(_comparison_tokens(record.text))
    if not 3 <= word_count <= 14 or len(record.text) > 140:
        return False
    if (
        _parse_money_values(record.text)
        or _is_shipping_line(record.text)
        or _is_promotion_line(record.text)
        or _is_cta_line(record.text)
        or _contains_term(record.text.casefold(), _PRODUCT_TERMS)
    ):
        return False
    return record.index < 12 or _contains_term(
        record.text.casefold(),
        _POSITIONING_TERMS,
    )


def _positioning_changes(
    old_records: tuple[_LineRecord, ...],
    new_records: tuple[_LineRecord, ...],
) -> list[tuple[float, StructuredChange]]:
    pairs, _, _ = _pair_records(
        (record for record in old_records if _is_positioning_candidate(record)),
        (record for record in new_records if _is_positioning_candidate(record)),
        minimum_score=0.52,
    )
    changes: list[tuple[float, StructuredChange]] = []
    for old, new in pairs:
        explicit_positioning = _contains_term(
            f"{old.text} {new.text}".casefold(),
            _POSITIONING_TERMS,
        )
        if not explicit_positioning and _changed_token_count(old.text, new.text) < 2:
            continue
        changes.append(
            (
                _change_position(old, new),
                StructuredChange(
                    category="POSITIONING",
                    old_value=old.text,
                    new_value=new.text,
                    description="Marketing message changed.",
                    importance=3,
                ),
            )
        )
        if len(changes) == 2:
            break
    return changes


def _product_changes(
    old_records: tuple[_LineRecord, ...],
    new_records: tuple[_LineRecord, ...],
) -> list[tuple[float, StructuredChange]]:
    pairs, _, _ = _pair_records(
        (
            record
            for record in old_records
            if _contains_term(record.text.casefold(), _PRODUCT_TERMS)
            and len(record.text.split()) <= 20
        ),
        (
            record
            for record in new_records
            if _contains_term(record.text.casefold(), _PRODUCT_TERMS)
            and len(record.text.split()) <= 20
        ),
        minimum_score=0.48,
    )
    return [
        (
            _change_position(old, new),
            StructuredChange(
                category="PRODUCT",
                old_value=old.text,
                new_value=new.text,
                description="Product availability or offering changed.",
                importance=4,
            ),
        )
        for old, new in pairs
        if old.text.casefold() != new.text.casefold()
    ][:2]


def _other_changes(
    old_records: tuple[_LineRecord, ...],
    new_records: tuple[_LineRecord, ...],
) -> list[tuple[float, StructuredChange]]:
    pairs, _, _ = _pair_records(
        (
            record
            for record in old_records
            if 3 <= len(_comparison_tokens(record.text)) <= 16
        ),
        (
            record
            for record in new_records
            if 3 <= len(_comparison_tokens(record.text)) <= 16
        ),
        minimum_score=0.68,
    )
    for old, new in pairs:
        if _changed_token_count(old.text, new.text) < 3:
            continue
        return [
            (
                _change_position(old, new),
                StructuredChange(
                    category="OTHER",
                    old_value=old.text,
                    new_value=new.text,
                    description="Page message changed.",
                    importance=1,
                ),
            )
        ]
    return []


def extract_structured_changes(
    old: str,
    new: str,
    *,
    limit: int = MAX_STRUCTURED_CHANGES,
) -> tuple[StructuredChange, ...]:
    if limit <= 0:
        return ()
    old_records, new_records = _changed_line_records(old, new)
    detected = [
        *_price_changes(old_records, new_records),
        *_shipping_changes(old_records, new_records),
        *_promotion_changes(old_records, new_records),
        *_product_changes(old_records, new_records),
        *_cta_changes(old_records, new_records),
        *_positioning_changes(old_records, new_records),
        *_other_changes(old_records, new_records),
    ]
    category_order = {
        "PRICE": 0,
        "SHIPPING": 1,
        "PROMOTION": 2,
        "PRODUCT": 3,
        "CTA": 4,
        "POSITIONING": 5,
        "OTHER": 6,
    }
    detected.sort(
        key=lambda item: (
            -item[1].importance,
            category_order[item[1].category],
            item[0],
            item[1].old_value.casefold(),
            item[1].new_value.casefold(),
        )
    )
    changes: list[StructuredChange] = []
    seen_pairs: set[tuple[str, str]] = set()
    seen_changes: set[tuple[str, str, str]] = set()
    for _, change in detected:
        pair_key = (change.old_value.casefold(), change.new_value.casefold())
        change_key = (change.category, *pair_key)
        if change_key in seen_changes:
            continue
        if pair_key in seen_pairs and change.category in {"POSITIONING", "OTHER"}:
            continue
        seen_changes.add(change_key)
        seen_pairs.add(pair_key)
        changes.append(change)
        if len(changes) == min(limit, MAX_STRUCTURED_CHANGES):
            break
    return tuple(changes)


def serialize_structured_changes(changes: Iterable[StructuredChange]) -> str:
    return json.dumps(
        [
            {
                "category": change.category,
                "old_value": change.old_value,
                "new_value": change.new_value,
                "description": change.description,
                "importance": change.importance,
            }
            for change in tuple(changes)[:MAX_STRUCTURED_CHANGES]
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def deserialize_structured_changes(value: str | None) -> tuple[StructuredChange, ...]:
    try:
        payload = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("Structured changes contain invalid JSON.") from exc
    if not isinstance(payload, list):
        raise ValueError("Structured changes must be a JSON list.")

    changes: list[StructuredChange] = []
    for item in payload[:MAX_STRUCTURED_CHANGES]:
        if not isinstance(item, dict):
            raise ValueError("Each structured change must be an object.")
        category = item.get("category")
        old_value = item.get("old_value")
        new_value = item.get("new_value")
        description = item.get("description")
        importance = item.get("importance")
        if (
            category not in BUSINESS_CATEGORIES
            or not isinstance(old_value, str)
            or not isinstance(new_value, str)
            or not isinstance(description, str)
            or isinstance(importance, bool)
            or not isinstance(importance, int)
            or not 1 <= importance <= 5
        ):
            raise ValueError("Structured change fields are invalid.")
        changes.append(
            StructuredChange(
                category=category,
                old_value=old_value[:MAX_CHANGE_VALUE_LENGTH],
                new_value=new_value[:MAX_CHANGE_VALUE_LENGTH],
                description=description[:MAX_CHANGE_VALUE_LENGTH],
                importance=importance,
            )
        )
    return tuple(changes)


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
