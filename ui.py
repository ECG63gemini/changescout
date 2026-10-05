"""Shared presentation helpers for ChangeScout's server-rendered pages.

Everything here returns escaped HTML strings. Business logic (monitoring,
scheduling, email delivery) stays in app.py and its modules; this file only
decides how existing data is described and displayed.
"""

import hashlib
import sqlite3
from collections.abc import Iterable, Sequence
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import unquote, urlparse

from display_time import (
    display_timezone_name,
    format_compact_time,
    format_display_time,
    machine_time,
)
from monitoring import (
    BUSINESS_CATEGORIES,
    StructuredChange,
    deserialize_structured_changes,
)
from scheduling import FREQUENCY_LABELS, next_check_at

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _asset_version(name: str) -> str:
    try:
        return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]
    except OSError:
        return "missing"


CSS_VERSION = _asset_version("app.css")


def h(value: object) -> str:
    return escape(str(value), quote=True)


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    word = singular if count == 1 else (plural_form or f"{singular}s")
    return f"{count} {word}"


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Status:
    label: str
    tone: str = "neutral"
    detail: str = ""


def badge(label: str, tone: str = "neutral") -> str:
    return f'<span class="badge badge--{h(tone)}">{h(label)}</span>'


def status_badge(status: Status) -> str:
    return badge(status.label, status.tone)


ALERT_STATUSES = {
    "sent": Status("Sent", "success"),
    "not_applicable": Status("No alert needed", "neutral"),
    "pending": Status("Pending", "info"),
    "sending": Status("Sending", "info"),
    "failed": Status("Failed", "error"),
    "not_configured": Status("Email unavailable", "warning"),
    "no_recipient": Status("No recipient", "error"),
    "customer_disabled": Status("Not sent: customer paused", "warning"),
    "indeterminate": Status("Delivery unknown", "warning"),
}
ALERT_ATTENTION_WINDOW = timedelta(days=7)


EMAIL_NOT_SENT = Status("Email not sent", "neutral")


def alert_status(value: str | None, *, changed: bool = False) -> Status:
    """Display status for a scan's email alert.

    "No alert needed" is reserved for baseline and unchanged scans; a scan that
    found a change but has no alert attempt on record says so plainly.
    """
    key = value or "not_applicable"
    if key == "not_applicable" and changed:
        return EMAIL_NOT_SENT
    return ALERT_STATUSES.get(key, Status(key.replace("_", " ").capitalize()))


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def unresolved_alert_problem(
    *,
    status: str | None,
    detected_at: str | None,
    has_recipient: bool,
    now: datetime,
) -> bool:
    """Whether the newest change alert still needs an operator's attention.

    A missing recipient stops mattering once one is configured; failed or
    unconfirmed deliveries are only surfaced while they are recent.
    """
    if status == "no_recipient":
        return not has_recipient
    if status in {"failed", "indeterminate"}:
        detected = _parse_utc(detected_at)
        return detected is not None and now - detected <= ALERT_ATTENTION_WINDOW
    return False


_HTTP_STATUS_HINTS = {
    "401": "login required",
    "403": "access denied",
    "404": "page not found",
    "410": "page removed",
    "429": "too many requests",
}
_ADDRESS_NOT_FOUND = (
    "We couldn't find or reach this website address. Check the URL and try again."
)
_PRIVATE_ADDRESS = (
    "This address points to a private or internal network, which ChangeScout "
    "can't monitor. Use the address of a public website."
)
_FRIENDLY_ERRORS = {
    "A check is already running for this target.": (
        "A scan is already running for this page. Refresh in a moment to see the result."
    ),
    "The check did not complete.": "The scan didn't complete. Please try again.",
    "The check failed unexpectedly. No capture was saved; please try again.": (
        "The scan failed unexpectedly. Please try again."
    ),
    "The page was captured, but the result could not be saved. Please try again.": (
        "The page was scanned, but the result couldn't be saved. Please try again."
    ),
    # Address problems, raised both when a URL is entered and during scans.
    "The hostname could not be resolved.": _ADDRESS_NOT_FOUND,
    "The hostname did not resolve to an IP address.": _ADDRESS_NOT_FOUND,
    "The hostname resolved to an invalid IP address.": _ADDRESS_NOT_FOUND,
    "Hostname validation timed out. Please try again.": (
        "We couldn't check this website address in time. Please try again in a few minutes."
    ),
    "Local, private, loopback, link-local, and reserved addresses are not supported.": (
        _PRIVATE_ADDRESS
    ),
    "Internal hostnames are not supported.": _PRIVATE_ADDRESS,
    "Use a full public http/https URL.": "Enter the full web address, including https://.",
    "The page timed out before any usable content was available.": (
        "The page took too long to load. Try again later, or check that the page "
        "opens in your browser."
    ),
}
_HTTP_ERROR_RE = re.compile(
    r"^The website returned HTTP (\d{3}); no capture was saved\.$"
)
_BROWSER_FAILURE_PREFIX = "The browser could not capture this page after two attempts."


def friendly_error(message: str | None) -> str:
    """Rephrase scan and address errors in plain language.

    Unknown messages pass through unchanged; callers keep the original text
    under Technical details where one is shown.
    """
    text = " ".join((message or "").split())
    if text in _FRIENDLY_ERRORS:
        return _FRIENDLY_ERRORS[text]
    match = _HTTP_ERROR_RE.match(text)
    if match:
        code = match.group(1)
        hint = _HTTP_STATUS_HINTS.get(code) or (
            "server error" if code.startswith("5") else ""
        )
        return f"The website returned an error (HTTP {code}{', ' + hint if hint else ''})."
    if text.startswith(_BROWSER_FAILURE_PREFIX):
        return (
            "We couldn't load this page. The website may be down, too slow to "
            "respond, or blocking ChangeScout. Try again later, or check that the "
            "page opens in your browser."
        )
    return text


def customer_status(active: bool) -> Status:
    return Status("Active", "success") if active else Status("Paused", "warning")


CADENCE = {
    "hourly": "Scans hourly",
    "every_6_hours": "Scans every 6 hours",
    "daily": "Scans daily",
}


def cadence(frequency: str) -> str:
    return CADENCE.get(frequency, "Invalid schedule")


CATEGORY_LABELS = {
    "PRICE": "Price",
    "PROMOTION": "Promotion",
    "SHIPPING": "Shipping",
    "PRODUCT": "Product",
    "CTA": "Call to action",
    "POSITIONING": "Messaging",
    "OTHER": "Other",
}


def category_label(code: str) -> str:
    return CATEGORY_LABELS.get(code, code.replace("_", " ").title())


def ordered_categories(codes: Iterable[str]) -> tuple[str, ...]:
    found = {code.strip().upper() for code in codes if code and code.strip()}
    known = [code for code in BUSINESS_CATEGORIES if code in found]
    extra = sorted(found.difference(BUSINESS_CATEGORIES))
    return (*known, *extra)


def capture_categories(
    categories: str | None,
    changes: Sequence[StructuredChange] = (),
) -> tuple[str, ...]:
    return ordered_categories(
        [*(categories or "").split(","), *(change.category for change in changes)]
    )


def category_list(codes: Sequence[str], *, label: str = "Categories") -> str:
    if not codes:
        return ""
    items = "".join(
        f'<li class="category">{h(category_label(code))}</li>' for code in codes
    )
    return f'<ul class="category-list" aria-label="{h(label)}">{items}</ul>'


def safe_changes(value: str | None) -> tuple[StructuredChange, ...]:
    try:
        return deserialize_structured_changes(value)
    except ValueError:
        return ()


def scan_result(
    *,
    scan_error: str | None,
    has_capture: bool,
    is_baseline: bool,
    change_count: int,
    categories: Sequence[str],
) -> Status:
    if scan_error:
        return Status("Scan failed", "error")
    if not has_capture:
        return Status("Not checked yet", "neutral")
    if change_count:
        return Status(f"{plural(change_count, 'change')} detected", "info")
    if categories:
        return Status("Change detected", "info")
    if is_baseline:
        return Status("Monitoring started", "neutral")
    return Status("No meaningful change", "neutral")


def monitor_status(
    *,
    enabled: bool,
    customer_paused: bool,
    has_baseline: bool,
    scan_error: str | None,
    alert_problem: bool,
    frequency: str,
) -> Status:
    if not enabled:
        return Status("Paused", "warning", "Disabled in settings")
    if customer_paused:
        return Status("Paused by customer", "warning", "Customer is paused")
    if scan_error:
        # The row's latest result already says "Scan failed"; no helper line.
        return Status("Needs attention", "error")
    if alert_problem:
        return Status("Needs attention", "error", "Email alert not delivered")
    if not has_baseline:
        return Status("Setup needed", "info", "Waiting for first scan")
    return Status("Watching", "success", cadence(frequency))


STATUS_PRIORITY = {
    "Needs attention": 0,
    "Setup needed": 1,
    "Watching": 2,
    "Paused by customer": 3,
    "Paused": 4,
}


# ---------------------------------------------------------------------------
# Monitor view model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NextCheck:
    kind: str  # scheduled | due | paused | customer_paused | invalid
    at: datetime | None = None


@dataclass(frozen=True)
class MonitorView:
    id: int
    name: str
    url: str
    customer_id: int | None
    customer_name: str | None
    customer_email: str | None
    customer_paused: bool
    enabled: bool
    frequency: str
    alert_email: str | None
    last_check_at: str | None
    scan_error: str | None
    has_baseline: bool
    first_capture_id: int | None
    latest_at: str | None
    latest_categories: tuple[str, ...]
    latest_change_count: int
    report_at: str | None  # newest meaningful change of any kind
    alerted_at: str | None  # newest change with extracted values (alerts)
    alerted_status: str | None
    alert_problem: bool
    status: Status
    result: Status
    next_check: NextCheck

    @property
    def needs_attention(self) -> bool:
        return self.status.label == "Needs attention"

    @property
    def active(self) -> bool:
        return self.enabled and not self.customer_paused

    @property
    def recipient(self) -> str | None:
        return self.alert_email or self.customer_email

    @property
    def recipient_override(self) -> str | None:
        if not self.alert_email or self.customer_id is None:
            return None
        if (self.customer_email or "").casefold() == self.alert_email.casefold():
            return None
        return self.alert_email


def compute_next_check(
    *,
    enabled: bool,
    customer_paused: bool,
    frequency: str,
    last_check_at: str | None,
) -> NextCheck:
    if not enabled:
        return NextCheck("paused")
    if customer_paused:
        return NextCheck("customer_paused")
    try:
        due_at = next_check_at(last_check_at, frequency)
    except ValueError:
        return NextCheck("invalid")
    if due_at is None:
        return NextCheck("due")
    return NextCheck("scheduled", due_at)


def monitor_view(row: sqlite3.Row, *, now: datetime) -> MonitorView:
    """Build display state from a row produced by app.MONITOR_QUERY."""
    enabled = bool(row["enabled"])
    customer_paused = row["customer_id"] is not None and not bool(row["customer_active"])
    has_baseline = row["latest_id"] is not None
    latest_changes = safe_changes(row["latest_changes"])
    latest_categories = capture_categories(row["latest_categories"], latest_changes)
    scan_error = row["last_check_error"] or None
    alert_problem = unresolved_alert_problem(
        status=row["alerted_status"],
        detected_at=row["alerted_at"],
        has_recipient=bool(row["alert_email"] or row["customer_email"]),
        now=now,
    )
    frequency = row["check_frequency"]
    return MonitorView(
        id=int(row["id"]),
        name=row["name"],
        url=row["url"],
        customer_id=row["customer_id"],
        customer_name=row["customer_name"],
        customer_email=row["customer_email"],
        customer_paused=customer_paused,
        enabled=enabled,
        frequency=frequency,
        alert_email=row["alert_email"],
        last_check_at=row["last_check_at"],
        scan_error=scan_error,
        has_baseline=has_baseline,
        first_capture_id=row["first_capture_id"],
        latest_at=row["latest_at"],
        latest_categories=latest_categories,
        latest_change_count=len(latest_changes),
        report_at=row["report_at"],
        alerted_at=row["alerted_at"],
        alerted_status=row["alerted_status"],
        alert_problem=alert_problem,
        status=monitor_status(
            enabled=enabled,
            customer_paused=customer_paused,
            has_baseline=has_baseline,
            scan_error=scan_error,
            alert_problem=alert_problem,
            frequency=frequency,
        ),
        result=scan_result(
            scan_error=scan_error,
            has_capture=has_baseline,
            is_baseline=(
                has_baseline and row["latest_id"] == row["first_capture_id"]
            ),
            change_count=len(latest_changes),
            categories=latest_categories,
        ),
        next_check=compute_next_check(
            enabled=enabled,
            customer_paused=customer_paused,
            frequency=frequency,
            last_check_at=row["last_check_at"],
        ),
    )


def sort_monitors(monitors: Iterable[MonitorView]) -> list[MonitorView]:
    return sorted(
        monitors,
        key=lambda monitor: (
            STATUS_PRIORITY.get(monitor.status.label, 9),
            (monitor.customer_name or "\uffff").casefold(),
            monitor.name.casefold(),
            monitor.id,
        ),
    )


def changed_since(monitors: Iterable[MonitorView], cutoff: datetime) -> int:
    """Count monitors whose latest meaningful change is at or after cutoff."""
    count = 0
    for monitor in monitors:
        detected = _parse_utc(monitor.report_at)
        if detected is not None and detected >= cutoff:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Time and URL presentation
# ---------------------------------------------------------------------------


def time_tag(
    value: str | datetime | None,
    *,
    full: bool = False,
    now: datetime | None = None,
) -> str:
    if not value:
        return ""
    try:
        full_text = format_display_time(value)
        stamp = machine_time(value)
    except (TypeError, ValueError):
        return h(value)
    if full:
        return f'<time datetime="{stamp}">{h(full_text)}</time>'
    # Keep "Oct 1" and "3:43 PM" together so narrow columns wrap after the comma.
    compact = (
        format_compact_time(value, now=now)
        .replace(" ", "\u00a0")
        .replace(",\u00a0", ", ")
    )
    return f'<time datetime="{stamp}" title="{h(full_text)}">{h(compact)}</time>'


def last_checked_html(value: str | None, *, now: datetime) -> str:
    if not value:
        return '<span class="muted">Not checked yet</span>'
    return time_tag(value, now=now)


def next_check_html(next_check: NextCheck, *, now: datetime) -> str:
    if next_check.kind == "paused":
        return '<span class="muted">Paused</span>'
    if next_check.kind == "customer_paused":
        return '<span class="muted">Paused by customer</span>'
    if next_check.kind == "invalid":
        return badge("Invalid schedule", "error")
    if next_check.kind == "due" or next_check.at is None:
        return badge("Due now", "warning")
    overdue = next_check.at <= now
    return time_tag(next_check.at, now=now) + (
        f" {badge('Due', 'warning')}" if overdue else ""
    )


def next_check_text(next_check: NextCheck) -> str:
    if next_check.kind == "paused":
        return "Paused"
    if next_check.kind == "customer_paused":
        return "Paused by customer"
    if next_check.kind == "invalid":
        return "Invalid schedule"
    if next_check.kind == "due" or next_check.at is None:
        return "Due now"
    return format_display_time(next_check.at)


def split_url(url: str) -> tuple[str, str] | None:
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not host:
        return None
    host = host.lower()
    if host.startswith("www.") and host.count(".") >= 2:
        host = host[4:]
    if port and port != (443 if scheme == "https" else 80):
        host = f"{host}:{port}"
    path = unquote(parsed.path).rstrip("/")
    if parsed.query:
        path = f"{path}?{parsed.query}"
    if len(path) > 64:
        path = f"{path[:63].rstrip()}…"
    return host, path


EXTERNAL_ICON = (
    '<svg class="icon icon--external" viewBox="0 0 16 16" aria-hidden="true" '
    'focusable="false"><path d="M9 2.75h4.25V7M13.25 2.75 7.5 8.5M11.5 '
    '9.5v3.25a.75.75 0 0 1-.75.75h-7.5a.75.75 0 0 1-.75-.75v-7.5a.75.75 0 0 1 '
    '.75-.75H6.5" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


def _breakable(text: str, *, before: str = "", after: str = "") -> str:
    """Escape text and add <wbr> hints so URLs wrap at natural separators."""
    pieces = []
    for character in text:
        if character in before:
            pieces.append("<wbr>")
        pieces.append(h(character))
        if character in after:
            pieces.append("<wbr>")
    return "".join(pieces)


def page_link(url: str, *, show_path: bool = True) -> str:
    parts = split_url(url)
    if parts is None:
        return f'<span class="page-link page-link--plain">{h(url)}</span>'
    host, path = parts
    path_html = (
        '<span class="page-link__path">'
        f'{_breakable(path, before="?&", after="/")}</span>'
        if show_path and path
        else ""
    )
    return (
        f'<a class="page-link" href="{h(url)}" target="_blank" '
        f'rel="noopener noreferrer" title="{h(url)}">'
        f'<span class="page-link__host">{_breakable(host, before=".")}</span>{path_html}'
        f"{EXTERNAL_ICON}"
        '<span class="visually-hidden"> (opens in a new tab)</span></a>'
    )


# ---------------------------------------------------------------------------
# Layout primitives
# ---------------------------------------------------------------------------

NAV_ITEMS = (
    ("overview", "Overview", "/"),
    ("customers", "Customers", "/customers"),
    ("monitors", "Monitors", "/monitors"),
)

FAVICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
    "viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' "
    "fill='%23175CD3'/%3E%3Cpath d='M8 20.5 13 15l4 3.5 7-8' fill='none' "
    "stroke='white' stroke-width='3' stroke-linecap='round' "
    "stroke-linejoin='round'/%3E%3C/svg%3E"
)

BRAND_MARK = (
    '<svg class="brand__mark" viewBox="0 0 32 32" aria-hidden="true" '
    'focusable="false"><rect width="32" height="32" rx="8" fill="#175CD3"/>'
    '<path d="M8 20.5 13 15l4 3.5 7-8" fill="none" stroke="#fff" '
    'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

BUSY_SCRIPT = """<script>
(function () {
  document.addEventListener("submit", function (event) {
    var button = event.target.querySelector("button[data-busy-label]");
    if (!button || event.defaultPrevented) { return; }
    button.setAttribute("data-idle-label", button.textContent);
    button.textContent = button.getAttribute("data-busy-label");
    button.disabled = true;
    event.target.setAttribute("aria-busy", "true");
  });
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("button[data-idle-label]").forEach(function (button) {
      button.textContent = button.getAttribute("data-idle-label");
      button.removeAttribute("data-idle-label");
      button.disabled = false;
    });
    document.querySelectorAll("form[aria-busy]").forEach(function (form) {
      form.removeAttribute("aria-busy");
    });
  });
})();
</script>"""


Crumb = tuple[str, str | None]


def breadcrumbs(crumbs: Sequence[Crumb]) -> str:
    if not crumbs:
        return ""
    items = []
    last = len(crumbs) - 1
    for index, (label, href) in enumerate(crumbs):
        if index == last or href is None:
            items.append(
                f'<li><span aria-current="page" title="{h(label)}">{h(label)}</span></li>'
            )
        else:
            items.append(f'<li><a href="{h(href)}" title="{h(label)}">{h(label)}</a></li>')
    return (
        '<nav class="breadcrumbs" aria-label="Breadcrumb">'
        f'<ol>{"".join(items)}</ol></nav>'
    )


def render_document(
    *,
    title: str,
    content: str,
    active: str | None = None,
    crumbs: Sequence[Crumb] = (),
    current_href: str | None = None,
) -> str:
    nav_links = []
    for key, label, href in NAV_ITEMS:
        current = ""
        if key == active:
            current = (
                ' aria-current="page"' if href == current_href else ' aria-current="true"'
            )
        nav_links.append(
            f'<li><a class="app-nav__link" href="{href}"{current}>{label}</a></li>'
        )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{h(title)} · ChangeScout</title>
<link rel="icon" href="{FAVICON}">
<link rel="stylesheet" href="/static/app.css?v={CSS_VERSION}">
</head>
<body>
<a class="skip-link" href="#main">Skip to main content</a>
<header class="app-header">
<div class="container app-header__inner">
<a class="brand" href="/">{BRAND_MARK}<span class="brand__name">ChangeScout</span></a>
<nav class="app-nav" aria-label="Primary"><ul class="app-nav__list">{"".join(nav_links)}</ul></nav>
</div>
</header>
<main id="main" class="container page" tabindex="-1">
{breadcrumbs(crumbs)}
{content}
</main>
<footer class="app-footer"><div class="container">Times shown in {h(display_timezone_name())}.</div></footer>
{BUSY_SCRIPT}
</body>
</html>
"""


def page_header(
    title: str,
    *,
    description: str = "",
    title_badges: str = "",
    meta: str = "",
    actions: str = "",
) -> str:
    description_html = (
        f'<p class="page-header__description">{description}</p>' if description else ""
    )
    badges_html = (
        f'<div class="page-header__badges">{title_badges}</div>' if title_badges else ""
    )
    actions_html = (
        f'<div class="page-header__actions">{actions}</div>' if actions else ""
    )
    return f"""<header class="page-header">
<div class="page-header__main">
<div class="page-header__title-row"><h1>{h(title)}</h1>{badges_html}</div>
{description_html}{meta}
</div>
{actions_html}
</header>"""


def facts(items: Iterable[str], *, lead: bool = False) -> str:
    rendered = "".join(f"<li>{item}</li>" for item in items if item)
    if not rendered:
        return ""
    modifier = " facts--lead" if lead else ""
    return f'<div class="facts-wrap"><ul class="facts{modifier}">{rendered}</ul></div>'


def section(
    title: str,
    body: str,
    *,
    section_id: str,
    description: str = "",
    aside: str = "",
    extra_class: str = "",
) -> str:
    description_html = (
        f'<p class="section__description">{description}</p>' if description else ""
    )
    aside_html = f'<div class="section__aside">{aside}</div>' if aside else ""
    return f"""<section class="section {extra_class}" id="{h(section_id)}" aria-labelledby="{h(section_id)}-title">
<div class="section__header">
<div class="section__heading"><h2 id="{h(section_id)}-title">{h(title)}</h2>{description_html}</div>
{aside_html}
</div>
{body}
</section>"""


def link_button(label: str, href: str, variant: str = "secondary") -> str:
    return f'<a class="btn btn--{variant}" href="{h(href)}">{h(label)}</a>'


def post_button(
    action: str,
    label: str,
    *,
    variant: str = "secondary",
    fields: dict[str, str] | None = None,
    busy_label: str | None = None,
) -> str:
    hidden = "".join(
        f'<input type="hidden" name="{h(name)}" value="{h(value)}">'
        for name, value in (fields or {}).items()
    )
    busy = f' data-busy-label="{h(busy_label)}"' if busy_label else ""
    return (
        f'<form class="inline-form" method="post" action="{h(action)}">{hidden}'
        f'<button class="btn btn--{variant}" type="submit"{busy}>{h(label)}</button>'
        "</form>"
    )


def banner(
    body_html: str,
    *,
    tone: str = "info",
    title: str = "",
    actions: str = "",
) -> str:
    role = "alert" if tone == "error" else "status"
    title_html = f'<p class="banner__title">{h(title)}</p>' if title else ""
    actions_html = f'<div class="banner__actions">{actions}</div>' if actions else ""
    return f"""<div class="banner banner--{h(tone)}" role="{role}">
<div class="banner__content">{title_html}<div class="banner__body">{body_html}</div></div>
{actions_html}
</div>"""


def empty_state(
    title: str,
    body: str,
    actions: str = "",
    *,
    hero: bool = False,
) -> str:
    actions_html = f'<div class="empty-state__actions">{actions}</div>' if actions else ""
    modifier = " empty-state--hero" if hero else ""
    return f"""<div class="empty-state{modifier}">
<p class="empty-state__title">{h(title)}</p>
<p class="empty-state__body">{body}</p>
{actions_html}
</div>"""


def disclosure(summary: str, body: str, *, open_: bool = False, extra_class: str = "") -> str:
    return (
        f'<details class="disclosure {extra_class}"{" open" if open_ else ""}>'
        f'<summary class="disclosure__summary">{h(summary)}</summary>'
        f'<div class="disclosure__body">{body}</div></details>'
    )


# ---------------------------------------------------------------------------
# Forms
# ---------------------------------------------------------------------------

ERROR_ICON = (
    '<svg class="icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">'
    '<circle cx="8" cy="8" r="6.25" fill="none" stroke="currentColor" '
    'stroke-width="1.5"/><path d="M8 4.75v3.5M8 10.9v.1" stroke="currentColor" '
    'stroke-width="1.75" stroke-linecap="round"/></svg>'
)


def _described_by(field_id: str, hint: str, error: str) -> tuple[str, str, str]:
    ids = []
    hint_html = ""
    error_html = ""
    if hint:
        ids.append(f"{field_id}-hint")
        hint_html = f'<p class="field__hint" id="{h(field_id)}-hint">{hint}</p>'
    if error:
        ids.append(f"{field_id}-error")
        error_html = (
            f'<p class="field__error" id="{h(field_id)}-error">'
            f"{ERROR_ICON}<span>{h(error)}</span></p>"
        )
    attrs = f' aria-describedby="{" ".join(ids)}"' if ids else ""
    if error:
        attrs += ' aria-invalid="true"'
    return attrs, hint_html, error_html


def text_field(
    *,
    field_id: str,
    name: str,
    label: str,
    value: str = "",
    input_type: str = "text",
    required: bool = False,
    hint: str = "",
    error: str = "",
    placeholder: str = "",
    autocomplete: str = "",
    extra_attrs: str = "",
) -> str:
    aria, hint_html, error_html = _described_by(field_id, hint, error)
    optional = "" if required else ' <span class="field__optional">(optional)</span>'
    attributes = [
        f'id="{h(field_id)}"',
        f'name="{h(name)}"',
        f'type="{h(input_type)}"',
        f'value="{h(value)}"',
        'class="input"',
    ]
    if required:
        attributes.append("required")
    if placeholder:
        attributes.append(f'placeholder="{h(placeholder)}"')
    if autocomplete:
        attributes.append(f'autocomplete="{h(autocomplete)}"')
    if extra_attrs:
        attributes.append(extra_attrs)
    return f"""<div class="field{' field--invalid' if error else ''}">
<label class="field__label" for="{h(field_id)}">{h(label)}{optional}</label>
<input {" ".join(attributes)}{aria}>
{hint_html}{error_html}
</div>"""


def frequency_field(
    *,
    field_id: str,
    selected: str,
    error: str = "",
    hint: str = "",
) -> str:
    aria, hint_html, error_html = _described_by(field_id, hint, error)
    options = "".join(
        f'<option value="{h(value)}"{" selected" if value == selected else ""}>'
        f"{h(label)}</option>"
        for value, label in FREQUENCY_LABELS.items()
    )
    return f"""<div class="field{' field--invalid' if error else ''}">
<label class="field__label" for="{h(field_id)}">Scan frequency</label>
<select class="select" id="{h(field_id)}" name="check_frequency"{aria}>{options}</select>
{hint_html}{error_html}
</div>"""


def checkbox_field(
    *,
    field_id: str,
    name: str,
    label: str,
    checked: bool,
    hint: str = "",
    value: str = "yes",
) -> str:
    aria, hint_html, _ = _described_by(field_id, hint, "")
    return f"""<div class="checkbox">
<input type="checkbox" id="{h(field_id)}" name="{h(name)}" value="{h(value)}"{" checked" if checked else ""}{aria}>
<div><label for="{h(field_id)}">{h(label)}</label>{hint_html}</div>
</div>"""


def form_error_summary(errors: dict[str, str], field_ids: dict[str, str]) -> str:
    if not errors:
        return ""
    items = "".join(
        f'<li><a href="#{h(field_ids.get(name, name))}">{h(message)}</a></li>'
        for name, message in errors.items()
    )
    return banner(
        f'<ul class="error-list">{items}</ul>',
        tone="error",
        title="Please fix the following before continuing",
    )


# ---------------------------------------------------------------------------
# Business change presentation
# ---------------------------------------------------------------------------


def change_card(change: StructuredChange) -> str:
    return f"""<article class="change-card">
<h3 class="change-card__category"><span class="category">{h(category_label(change.category))}</span></h3>
<div class="change-values">
<div class="change-value"><p class="change-label">Old</p><p class="change-value__text">{h(change.old_value)}</p></div>
<div class="change-arrow" aria-hidden="true">→</div>
<div class="change-value change-value--new"><p class="change-label">New</p><p class="change-value__text">{h(change.new_value)}</p></div>
</div>
<div class="change-card__detail"><p class="change-label">Details</p><p>{h(change.description)}</p></div>
</article>"""


def change_cards(changes: Sequence[StructuredChange]) -> str:
    return (
        '<div class="change-grid">'
        + "".join(change_card(change) for change in changes)
        + "</div>"
    )


def change_lines(changes: Sequence[StructuredChange]) -> str:
    items = "".join(
        f"""<li class="change-line">
<span class="category">{h(category_label(change.category))}</span>
<span class="change-line__values"><span class="change-line__old">{h(change.old_value)}</span>
<span class="change-line__arrow" aria-hidden="true">→</span><span class="visually-hidden"> changed to </span>
<span class="change-line__new">{h(change.new_value)}</span></span>
<span class="change-line__detail">{h(change.description)}</span>
</li>"""
        for change in changes
    )
    return f'<ul class="change-lines">{items}</ul>'


def alert_summary_html(
    *,
    status: str | None,
    recipient: str | None,
    sent_at: str | None,
) -> str:
    """Email outcome for a report that contains business changes."""
    current = alert_status(status, changed=True)
    if status == "sent":
        when = format_display_time(sent_at) if sent_at else "an unknown time"
        destination = f" to {h(recipient)} at {h(when)}" if recipient else f" at {h(when)}"
        return f'<span class="alert-summary">Email alert {badge("Sent", "success")}{destination}</span>'
    if current == EMAIL_NOT_SENT:
        return f'<span class="alert-summary">{status_badge(current)}</span>'
    return f'<span class="alert-summary">Email alert {status_badge(current)}</span>'


def technical_list(items: Sequence[tuple[str, str]]) -> str:
    rows = "".join(
        f"<div><dt>{h(term)}</dt><dd>{value}</dd></div>" for term, value in items
    )
    return f'<dl class="tech-list">{rows}</dl>'


def score_text(value: object) -> str:
    if value is None:
        return "Unavailable"
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "Unavailable"


@dataclass(frozen=True)
class FeedItem:
    capture_id: int
    target_id: int
    competitor: str
    url: str
    customer_id: int | None
    customer_name: str | None
    detected_at: str
    categories: tuple[str, ...]
    changes: tuple[StructuredChange, ...]
    alert_status: str | None
    alert_recipient: str | None
    alert_sent_at: str | None
    alert_error: str | None
    summary: str
    visual_score: float | None
    text_score: float | None


def feed_item(row: sqlite3.Row) -> FeedItem:
    changes = safe_changes(row["structured_changes"])
    return FeedItem(
        capture_id=int(row["id"]),
        target_id=int(row["target_id"]),
        competitor=row["target_name"],
        url=row["target_url"],
        customer_id=row["customer_id"],
        customer_name=row["customer_name"],
        detected_at=row["created_at"],
        categories=capture_categories(row["categories"], changes),
        changes=changes,
        alert_status=row["alert_status"],
        alert_recipient=row["alert_recipient"],
        alert_sent_at=row["alert_sent_at"],
        alert_error=row["alert_error"],
        summary=row["summary"] or "",
        visual_score=row["visual_score"],
        text_score=row["text_score"],
    )


def feed_card(
    item: FeedItem,
    *,
    now: datetime,
    show_customer: bool = True,
    show_technical: bool = False,
) -> str:
    context = ""
    if show_customer:
        if item.customer_id is not None:
            context = (
                f'<p class="feed-item__context">for <a href="/customer/{item.customer_id}">'
                f"{h(item.customer_name or 'Unnamed customer')}</a></p>"
            )
        else:
            context = '<p class="feed-item__context">No customer assigned</p>'
    if item.changes:
        strongest = item.changes[0]
        extra = len(item.changes) - 1
        more = (
            f'<p class="feed-item__more">+{h(plural(extra, "more change"))}</p>'
            if extra
            else ""
        )
        change_html = f"""<p class="feed-item__values"><span class="value-old">{h(strongest.old_value)}</span>
<span class="value-arrow" aria-hidden="true">→</span><span class="visually-hidden"> changed to </span>
<span class="value-new">{h(strongest.new_value)}</span></p>
<p class="feed-item__detail">{h(strongest.description)}</p>{more}"""
    else:
        change_html = (
            '<p class="feed-item__detail">The page changed, but no specific '
            "old-to-new business change was identified. Open the report for "
            "details.</p>"
        )
    # Feed items are always scans that found a change.
    alert = alert_status(item.alert_status, changed=True)
    technical_html = ""
    if show_technical:
        technical_html = '<div class="feed-item__technical">' + disclosure(
            "Technical details",
            technical_list(
                [
                    ("Visual difference", h(score_text(item.visual_score))),
                    ("Text difference", h(score_text(item.text_score))),
                    ("Scan summary", h(item.summary or "None recorded")),
                ]
            ),
            extra_class="disclosure--quiet",
        ) + "</div>"
    return f"""<li class="feed-item">
<div class="feed-item__who">
<h3 class="feed-item__title"><a href="/target/{item.target_id}">{h(item.competitor)}</a></h3>
{context}
<p class="feed-item__time">{time_tag(item.detected_at, now=now)}</p>
</div>
<div class="feed-item__what">
{category_list(item.categories)}
{change_html}
</div>
<div class="feed-item__meta">
<p class="feed-item__alert"><span class="meta-label">Alert</span> {status_badge(alert)}</p>
<a class="btn btn--secondary btn--sm" href="/target/{item.target_id}">View report<span class="visually-hidden"> for {h(item.competitor)}</span></a>
</div>
{technical_html}
</li>"""


def feed_list(
    items: Sequence[FeedItem],
    *,
    now: datetime,
    show_customer: bool = True,
    show_technical: bool = False,
) -> str:
    cards = "".join(
        feed_card(
            item,
            now=now,
            show_customer=show_customer,
            show_technical=show_technical,
        )
        for item in items
    )
    return f'<ul class="feed">{cards}</ul>'


# ---------------------------------------------------------------------------
# Attention items
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttentionItem:
    title: str
    body: str  # already-escaped HTML
    action: str  # already-rendered HTML
    tone: str = "error"
    priority: int = 0


def _attention_meta(*parts: str) -> str:
    content = " · ".join(part for part in parts if part)
    return f'<span class="attention-item__meta">{content}</span>' if content else ""


def monitor_attention(
    monitor: MonitorView,
    *,
    now: datetime,
    include_customer: bool = True,
) -> list[AttentionItem]:
    if not monitor.active:
        return []
    customer = (
        h(monitor.customer_name)
        if include_customer and monitor.customer_name
        else ""
    )
    items: list[AttentionItem] = []
    if monitor.scan_error:
        attempted = (
            f"Last attempt {time_tag(monitor.last_check_at, now=now)}"
            if monitor.last_check_at
            else ""
        )
        if monitor.has_baseline:
            title = f"Scan failed: {monitor.name}"
            lead = "The latest scan didn't complete."
        else:
            title = f"First scan failed: {monitor.name}"
            lead = "There's no baseline snapshot yet, so changes can't be detected."
        items.append(
            AttentionItem(
                title=title,
                body=(
                    f"{lead} "
                    f'<span class="attention-item__reason">{h(friendly_error(monitor.scan_error))}</span>'
                    f"{_attention_meta(attempted, customer)}"
                ),
                action=post_button(
                    f"/check/{monitor.id}",
                    "Retry scan",
                    busy_label="Scanning…",
                ),
                tone="error",
                priority=0,
            )
        )
    elif not monitor.has_baseline:
        items.append(
            AttentionItem(
                title=f"Baseline missing: {monitor.name}",
                body=(
                    "Run the first scan to take a baseline snapshot. Future scans will be "
                    f"compared with it.{_attention_meta(customer)}"
                ),
                action=post_button(
                    f"/check/{monitor.id}",
                    "Scan now",
                    busy_label="Scanning…",
                ),
                tone="info",
                priority=2,
            )
        )

    alert = monitor.alerted_status or ""
    if monitor.alert_problem:
        detected = (
            f" for the change detected {time_tag(monitor.alerted_at, now=now)}"
            if monitor.alerted_at
            else ""
        )
        if alert == "no_recipient":
            title = f"No alert recipient: {monitor.name}"
            body = f"The email alert{detected} wasn't sent because no recipient is set."
            action = link_button("Add recipient", f"/target/{monitor.id}#settings")
        elif alert == "indeterminate":
            title = f"Alert delivery unknown: {monitor.name}"
            body = (
                "ChangeScout restarted while sending the email alert"
                f"{detected}, so delivery couldn't be confirmed."
            )
            action = link_button("View report", f"/target/{monitor.id}")
        else:
            title = f"Email alert failed: {monitor.name}"
            body = f"The email alert{detected} couldn't be delivered."
            action = link_button("View report", f"/target/{monitor.id}")
        items.append(
            AttentionItem(
                title=title,
                body=f"{body}{_attention_meta(customer)}",
                action=action,
                tone="error",
                priority=1,
            )
        )
    return items


def attention_list(items: Sequence[AttentionItem]) -> str:
    rendered = "".join(
        f"""<li class="attention-item attention-item--{h(item.tone)}">
<div class="attention-item__content">
<p class="attention-item__title">{h(item.title)}</p>
<div class="attention-item__body">{item.body}</div>
</div>
{f'<div class="attention-item__action">{item.action}</div>' if item.action else ""}
</li>"""
        for item in items
    )
    return f'<ul class="attention-list">{rendered}</ul>'


# ---------------------------------------------------------------------------
# Monitor tables
# ---------------------------------------------------------------------------


def monitor_rows(
    monitors: Sequence[MonitorView],
    *,
    now: datetime,
    show_customer: bool,
) -> str:
    rows = []
    for monitor in monitors:
        override = (
            f'<p class="cell-sub cell-email">Alerts to {h(monitor.recipient_override)}</p>'
            if not show_customer and monitor.recipient_override
            else ""
        )
        customer_cell = ""
        if show_customer:
            customer_html = (
                f'<a class="cell-customer" href="/customer/{monitor.customer_id}" '
                f'title="{h(monitor.customer_name)}">{h(monitor.customer_name)}</a>'
                if monitor.customer_id is not None
                else '<span class="muted">No customer</span>'
            )
            customer_cell = f'<td data-label="Customer">{customer_html}</td>'
        result_sub = ""
        if monitor.result.label.endswith("detected") and monitor.latest_categories:
            result_sub = (
                '<p class="cell-sub">'
                + h(", ".join(category_label(code) for code in monitor.latest_categories))
                + "</p>"
            )
        result_class = " result--error" if monitor.result.tone == "error" else ""
        status_sub = (
            f'<p class="cell-sub">{h(monitor.status.detail)}</p>'
            if monitor.status.detail
            else ""
        )
        rows.append(
            f"""<tr>
<td class="cell-primary"><a class="cell-title" href="/target/{monitor.id}">{h(monitor.name)}</a>
<p class="cell-sub">{page_link(monitor.url)}</p>{override}</td>
{customer_cell}
<td data-label="Status">{status_badge(monitor.status)}{status_sub}</td>
<td data-label="Latest result"><span class="result{result_class}">{h(monitor.result.label)}</span>{result_sub}</td>
<td data-label="Last checked" class="num">{last_checked_html(monitor.last_check_at, now=now)}</td>
<td data-label="Next check" class="num">{next_check_html(monitor.next_check, now=now)}</td>
<td class="cell-action"><a class="btn btn--tertiary btn--sm" href="/target/{monitor.id}">View report<span class="visually-hidden"> for {h(monitor.name)}</span></a></td>
</tr>"""
        )
    return "".join(rows)


def monitor_table(
    monitors: Sequence[MonitorView],
    *,
    now: datetime,
    show_customer: bool,
    caption: str,
) -> str:
    customer_head = '<th scope="col">Customer</th>' if show_customer else ""
    modifier = " monitor-table--with-customer" if show_customer else ""
    return f"""<div class="card card--flush">
<table class="data-table data-table--stack data-table--stack-md monitor-table{modifier}">
<caption class="visually-hidden">{h(caption)}</caption>
<thead><tr><th scope="col">Competitor</th>{customer_head}<th scope="col">Status</th><th scope="col">Latest result</th><th scope="col">Last checked</th><th scope="col">Next check</th><th scope="col"><span class="visually-hidden">Actions</span></th></tr></thead>
<tbody>{monitor_rows(monitors, now=now, show_customer=show_customer)}</tbody>
</table>
</div>"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
