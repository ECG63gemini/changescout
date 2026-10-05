"""Page-level renderers for ChangeScout.

Routes in app.py gather data; the functions here turn it into complete HTML
documents using the shared helpers in ui.py.
"""

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from display_time import format_display_time
from monitoring import StructuredChange
from ui import (
    AttentionItem,
    FeedItem,
    MonitorView,
    alert_status,
    alert_summary_html,
    attention_list,
    badge,
    banner,
    cadence,
    capture_categories,
    category_list,
    changed_since,
    change_cards,
    change_lines,
    checkbox_field,
    customer_status,
    disclosure,
    empty_state,
    facts,
    feed_list,
    form_error_summary,
    friendly_error,
    frequency_field,
    h,
    link_button,
    monitor_table,
    next_check_text,
    page_header,
    page_link,
    plural,
    post_button,
    render_document,
    safe_changes,
    scan_result,
    score_text,
    section,
    sort_monitors,
    status_badge,
    technical_list,
    text_field,
    time_tag,
)

NO_CATEGORIES = '<span class="muted">None</span>'
OVERVIEW_MONITOR_LIMIT = 6
OVERVIEW_ATTENTION_LIMIT = 3


def _need_attention_text(count: int) -> str:
    return f"{count} {'needs' if count == 1 else 'need'} attention"


def _notice_banner(notice: str | None) -> str:
    return banner(h(notice), tone="success") if notice else ""


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def overview_page(
    *,
    now: datetime,
    customers_total: int,
    customers_active: int,
    monitors: Sequence[MonitorView],
    feed: Sequence[FeedItem],
    attention: Sequence[AttentionItem],
) -> str:
    header = page_header(
        "Overview",
        description="Meaningful competitor changes across all customers.",
        actions=link_button("Add customer", "/customers/new", "primary"),
    )
    if customers_total == 0 and not monitors:
        content = header + empty_state(
            "Start monitoring your first competitor.",
            "Create a customer, add a competitor page, and capture an initial "
            "baseline. ChangeScout will then monitor the page automatically and "
            "email meaningful changes.",
            link_button("Create first customer", "/customers/new", "primary"),
            hero=True,
        )
        return render_document(
            title="Overview",
            content=content,
            active="overview",
            current_href="/",
        )

    active_monitors = sum(1 for monitor in monitors if monitor.active)
    attention_monitors = sum(1 for monitor in monitors if monitor.needs_attention)
    changed_this_week = changed_since(monitors, now - timedelta(days=7))
    stats = f"""<section class="snapshot" aria-labelledby="snapshot-title">
<h2 class="visually-hidden" id="snapshot-title">Snapshot</h2>
<dl class="stats">
<div class="stat"><dt>Active customers</dt><dd><span class="stat__value">{customers_active}</span><span class="stat__note">of {plural(customers_total, 'customer')}</span></dd></div>
<div class="stat"><dt>Active monitors</dt><dd><span class="stat__value">{active_monitors}</span><span class="stat__note">of {plural(len(monitors), 'monitor')}</span></dd></div>
<div class="stat"><dt>Need attention</dt><dd><span class="stat__value">{attention_monitors}</span><span class="stat__note">{'monitor' if attention_monitors == 1 else 'monitors'}</span></dd></div>
<div class="stat"><dt>Competitors with changes</dt><dd><span class="stat__value">{changed_this_week}</span><span class="stat__note">in the last 7 days</span></dd></div>
</dl>
</section>"""

    attention_html = ""
    if attention:
        shown = attention[:OVERVIEW_ATTENTION_LIMIT]
        more = len(attention) - len(shown)
        more_html = (
            f'<p class="section__footer">{more} more '
            f"{'needs' if more == 1 else 'need'} attention. "
            '<a href="/monitors">Review in Monitors</a></p>'
            if more
            else ""
        )
        attention_html = section(
            "Needs attention",
            attention_list(shown) + more_html,
            section_id="attention",
        )

    if feed:
        feed_html = feed_list(feed, now=now)
    else:
        feed_html = empty_state(
            "No meaningful business changes detected yet.",
            "ChangeScout compares every scan with the previous one. Changes to "
            "prices, promotions, shipping, products, calls to action, and "
            "messaging will appear here.",
        )
    changes_html = section(
        "Recent meaningful changes",
        feed_html,
        section_id="recent-changes",
    )

    if monitors:
        ordered = sort_monitors(monitors)
        shown_monitors = ordered[:OVERVIEW_MONITOR_LIMIT]
        monitors_body = monitor_table(
            shown_monitors,
            now=now,
            show_customer=True,
            caption="Monitoring overview",
        )
        aside = link_button(
            f"View all {plural(len(monitors), 'monitor')}",
            "/monitors",
            "tertiary",
        )
    else:
        monitors_body = empty_state(
            "No competitor pages are being monitored yet.",
            "Open a customer and add a competitor page to start monitoring.",
            link_button("View customers", "/customers", "secondary"),
        )
        aside = ""
    monitoring_html = section(
        "Monitoring overview",
        monitors_body,
        section_id="monitoring",
        aside=aside,
    )
    # Business changes first, then problems, then operations.
    return render_document(
        title="Overview",
        content=header + stats + changes_html + attention_html + monitoring_html,
        active="overview",
        current_href="/",
    )


# ---------------------------------------------------------------------------
# Monitors
# ---------------------------------------------------------------------------


def _legacy_monitor_form(values: Mapping[str, str], errors: Mapping[str, str]) -> str:
    field_ids = {
        "name": "monitor-name",
        "url": "monitor-url",
        "alert_email": "monitor-alert-email",
        "check_frequency": "monitor-frequency",
    }
    return f"""{form_error_summary(dict(errors), field_ids)}
<form class="form" method="post" action="/targets" novalidate>
{text_field(field_id="monitor-name", name="name", label="Competitor name", value=values.get("name", ""), required=True, error=errors.get("name", ""), placeholder="Northstar Outfitters", autocomplete="off")}
{text_field(field_id="monitor-url", name="url", label="Page to monitor", value=values.get("url", ""), input_type="url", required=True, error=errors.get("url", ""), placeholder="https://competitor.com/pricing", hint="Use the full public address, including https://.", autocomplete="url", extra_attrs='spellcheck="false"')}
{text_field(field_id="monitor-alert-email", name="alert_email", label="Alert email", value=values.get("alert_email", ""), input_type="email", error=errors.get("alert_email", ""), placeholder="owner@example.com", hint="Needed to receive email alerts for this page.", autocomplete="email")}
<div class="form-row">
{frequency_field(field_id="monitor-frequency", selected=values.get("check_frequency", "daily"), error=errors.get("check_frequency", ""))}
{checkbox_field(field_id="monitor-enabled", name="enabled", label="Start automatic scans", checked=values.get("enabled", "yes") == "yes")}
</div>
<div class="form-actions"><button class="btn btn--primary" type="submit" data-busy-label="Adding…">Add monitor</button></div>
</form>"""


def monitors_page(
    *,
    now: datetime,
    monitors: Sequence[MonitorView],
    notice: str | None = None,
    form_values: Mapping[str, str] | None = None,
    form_errors: Mapping[str, str] | None = None,
) -> str:
    errors = form_errors or {}
    values = form_values or {}
    counts = [
        plural(len(monitors), "monitor"),
        f"{sum(1 for m in monitors if m.status.label == 'Watching')} watching",
        _need_attention_text(sum(1 for m in monitors if m.needs_attention)),
        f"{sum(1 for m in monitors if m.status.label.startswith('Paused'))} paused",
    ]
    header = page_header(
        "Monitors",
        description=(
            "Every competitor page ChangeScout scans, across all customers. "
            'To add a page for a customer, open it from <a href="/customers">Customers</a>.'
        ),
        meta=facts(h(item) for item in counts) if monitors else "",
    )
    if monitors:
        body = monitor_table(
            sort_monitors(monitors),
            now=now,
            show_customer=True,
            caption="All monitors",
        )
    else:
        body = empty_state(
            "No competitor pages are being monitored yet.",
            "Open a customer and add a competitor page. ChangeScout takes a "
            "baseline snapshot, then scans the page on a schedule.",
            link_button("View customers", "/customers", "primary"),
        )
    legacy = (
        '<section class="section" id="add-monitor" aria-label="Add a monitor without a customer">'
        + disclosure(
            "Add a monitor without a customer",
            '<p class="muted">Most competitor pages should be added from a customer '
            "so alerts go to the right person. Use this only for internal or "
            "unassigned monitoring.</p>" + _legacy_monitor_form(values, errors),
            open_=bool(errors),
            extra_class="disclosure--card",
        )
        + "</section>"
    )
    return render_document(
        title="Monitors",
        content=_notice_banner(notice) + header + body + legacy,
        active="monitors",
        current_href="/monitors",
    )


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------


def customers_page(*, now: datetime, customers: Sequence[sqlite3.Row]) -> str:
    header = page_header(
        "Customers",
        description="Businesses you monitor competitors for.",
        actions=link_button("Create customer", "/customers/new", "primary"),
    )
    if not customers:
        body = empty_state(
            "No customers yet.",
            "Create your first customer to begin monitoring competitors.",
            link_button("Create customer", "/customers/new", "primary"),
        )
        return render_document(
            title="Customers",
            content=header + body,
            active="customers",
            current_href="/customers",
        )
    rows = []
    for customer in customers:
        activity = (
            time_tag(customer["most_recent_activity"], now=now)
            if customer["most_recent_activity"]
            else '<span class="muted">No scans yet</span>'
        )
        rows.append(
            f"""<tr>
<td class="cell-primary"><a class="cell-title" href="/customer/{customer['id']}">{h(customer['name'])}</a><p class="cell-sub cell-email">{h(customer['email'])}</p></td>
<td data-label="Status">{status_badge(customer_status(bool(customer['active'])))}</td>
<td>{customer['monitor_count']}</td>
<td data-label="Last activity" class="num">{activity}</td>
<td class="cell-action"><a class="btn btn--tertiary btn--sm" href="/customer/{customer['id']}">View customer<span class="visually-hidden">: {h(customer['name'])}</span></a></td>
</tr>"""
        )
    table = f"""<div class="card card--flush">
<table class="data-table data-table--stack customers-table">
<caption class="visually-hidden">Customers</caption>
<thead><tr><th scope="col">Customer</th><th scope="col">Status</th><th scope="col" class="num">Monitors</th><th scope="col">Last activity</th><th scope="col"><span class="visually-hidden">Actions</span></th></tr></thead>
<tbody>{"".join(rows)}</tbody>
</table>
</div>"""
    return render_document(
        title="Customers",
        content=header + table,
        active="customers",
        current_href="/customers",
    )


def customer_form_page(
    *,
    values: Mapping[str, str] | None = None,
    errors: Mapping[str, str] | None = None,
) -> str:
    values = values or {}
    errors = errors or {}
    field_ids = {"name": "customer-name", "email": "customer-email"}
    form = f"""<div class="form-layout">
{form_error_summary(dict(errors), field_ids)}
<div class="card">
<form class="form" method="post" action="/customers" novalidate>
{text_field(field_id="customer-name", name="name", label="Customer name", value=values.get("name", ""), required=True, error=errors.get("name", ""), placeholder="Acme Retail", autocomplete="organization")}
{text_field(field_id="customer-email", name="email", label="Alert email", value=values.get("email", ""), input_type="email", required=True, error=errors.get("email", ""), placeholder="owner@acme.com", hint="Meaningful change alerts for this customer's competitor pages are sent here.", autocomplete="email")}
<div class="form-actions">
<button class="btn btn--primary" type="submit" data-busy-label="Creating…">Create customer</button>
{link_button("Cancel", "/customers", "secondary")}
</div>
</form>
</div>
</div>"""
    header = page_header(
        "Create customer",
        description=(
            "Add a business you monitor competitors for. You'll add their "
            "competitor pages next."
        ),
    )
    return render_document(
        title="Create customer",
        content=header + form,
        active="customers",
        crumbs=(("Customers", "/customers"), ("New customer", None)),
    )


def customer_page(
    *,
    now: datetime,
    customer: sqlite3.Row,
    monitors: Sequence[MonitorView],
    feed: Sequence[FeedItem],
    attention: Sequence[AttentionItem],
    notice: str | None = None,
) -> str:
    customer_id = customer["id"]
    active = bool(customer["active"])
    name = customer["name"]
    add_href = f"/customer/{customer_id}/targets/new"
    status_action = f"/customer/{customer_id}/status"

    paused_banner = ""
    if not active:
        paused_banner = banner(
            "Scheduled scans and email alerts are stopped for "
            f"{plural(len(monitors), 'competitor page')}. Reports and history are kept.",
            tone="warning",
            title="This customer is paused",
        )

    attention_count = sum(1 for monitor in monitors if monitor.needs_attention)
    meta = facts(
        [
            h(plural(len(monitors), "monitored competitor")),
            h(f"{plural(attention_count, 'monitor')} {'needs' if attention_count == 1 else 'need'} attention")
            if attention_count
            else "",
            (
                f"Latest meaningful change: {time_tag(feed[0].detected_at, now=now)}"
                if feed
                else "No meaningful changes yet"
            ),
        ]
    )
    secondary_action = (
        link_button("Disable customer…", "#customer-controls", "secondary")
        if active
        else post_button(status_action, "Re-enable customer", fields={"active": "1"})
    )
    header = page_header(
        name,
        description=f'<span class="cell-email">{h(customer["email"])}</span>',
        title_badges=status_badge(customer_status(active)),
        meta=meta,
        actions=link_button("Add competitor", add_href, "primary") + secondary_action,
    )

    attention_html = (
        section("Needs attention", attention_list(attention), section_id="attention")
        if attention
        else ""
    )

    if feed:
        feed_html = feed_list(feed, now=now, show_customer=False, show_technical=True)
    else:
        feed_html = empty_state(
            "No meaningful changes yet.",
            "When ChangeScout finds a change to prices, promotions, shipping, "
            "products, or messaging on one of this customer's competitor pages, "
            "it appears here.",
        )
    # Until the customer has competitor pages, the add-competitor prompt is the
    # only useful next step, so the empty change feed is omitted.
    changes_html = (
        section("Recent meaningful changes", feed_html, section_id="recent-changes")
        if monitors or feed
        else ""
    )

    if monitors:
        monitors_body = monitor_table(
            sort_monitors(monitors),
            now=now,
            show_customer=False,
            caption=f"Competitor pages for {name}",
        )
    else:
        monitors_body = empty_state(
            "No competitor pages are being monitored for this customer yet.",
            "Add a competitor's pricing, product, or promotion page. ChangeScout "
            "takes a baseline snapshot, then scans the page on a schedule.",
            link_button("Add competitor", add_href, "primary"),
        )
    monitors_html = section(
        "Competitor pages",
        monitors_body,
        section_id="competitors",
    )

    add_html = ""
    if monitors:
        add_html = f"""<section class="section" aria-labelledby="add-competitor-title">
<div class="cta-panel">
<div class="cta-panel__text"><h2 id="add-competitor-title">Track another competitor page</h2>
<p>Alerts go to <span class="cell-email">{h(customer['email'])}</span>. Pricing, product, and promotion pages work best.</p></div>
{link_button("Add competitor", add_href, "secondary")}
</div>
</section>"""

    if active:
        control = f"""<div class="control-row">
<div class="control-row__text"><p class="control-row__title">Customer monitoring: Enabled</p>
<p>Scheduled scans and email alerts run for all of this customer's competitor pages. Disabling pauses both; reports and history are kept.</p></div>
{post_button(status_action, "Disable customer", variant="danger", fields={"active": "0"})}
</div>"""
    else:
        control = f"""<div class="control-row">
<div class="control-row__text"><p class="control-row__title">Customer monitoring: Disabled</p>
<p>Scheduled scans and email alerts are paused for all of this customer's competitor pages. Re-enable the customer to resume them.</p></div>
{post_button(status_action, "Re-enable customer", fields={"active": "1"})}
</div>"""
    controls_html = section(
        "Customer controls",
        f'<div class="card">{control}</div>',
        section_id="customer-controls",
    )

    # Same hierarchy as Overview: business changes, problems, operations, controls.
    return render_document(
        title=name,
        content=(
            _notice_banner(notice)
            + header
            + paused_banner
            + changes_html
            + attention_html
            + monitors_html
            + add_html
            + controls_html
        ),
        active="customers",
        crumbs=(("Customers", "/customers"), (name, None)),
    )


def competitor_form_page(
    *,
    customer: sqlite3.Row,
    values: Mapping[str, str] | None = None,
    errors: Mapping[str, str] | None = None,
) -> str:
    values = values or {}
    errors = errors or {}
    customer_id = customer["id"]
    field_ids = {
        "name": "competitor-name",
        "url": "competitor-url",
        "check_frequency": "competitor-frequency",
    }
    paused_note = (
        banner(
            "Automatic scans and email alerts will start when the customer is re-enabled.",
            tone="warning",
            title=f"{customer['name']} is paused",
        )
        if not customer["active"]
        else ""
    )
    form = f"""<div class="form-layout">
{form_error_summary(dict(errors), field_ids)}
{paused_note}
<div class="card">
<form class="form" method="post" action="/customer/{customer_id}/targets" novalidate>
{text_field(field_id="competitor-name", name="name", label="Competitor name", value=values.get("name", ""), required=True, error=errors.get("name", ""), placeholder="Northstar Outfitters", autocomplete="off")}
{text_field(field_id="competitor-url", name="url", label="Page to monitor", value=values.get("url", ""), input_type="url", required=True, error=errors.get("url", ""), placeholder="https://competitor.com/pricing", hint="Use the full public address, including https://. Pricing, product, and promotion pages work best.", autocomplete="url", extra_attrs='spellcheck="false"')}
<div class="form-row">
{frequency_field(field_id="competitor-frequency", selected=values.get("check_frequency", "daily"), error=errors.get("check_frequency", ""))}
{checkbox_field(field_id="competitor-enabled", name="enabled", label="Start automatic scans", checked=values.get("enabled", "yes") == "yes", hint="The first scan saves a baseline snapshot. Later scans are compared with it.")}
</div>
<div class="form-note">
<p class="form-note__title">Alerts will go to <span class="cell-email">{h(customer['email'])}</span></p>
<p>You can send alerts for this page somewhere else later in its monitoring settings.</p>
</div>
<div class="form-actions">
<button class="btn btn--primary" type="submit" data-busy-label="Adding…">Add competitor</button>
{link_button("Cancel", f"/customer/{customer_id}", "secondary")}
</div>
</form>
</div>
</div>"""
    header = page_header(
        "Add competitor page",
        description=(
            "ChangeScout scans the page on a schedule and reports meaningful "
            "changes to prices, promotions, shipping, products, and messaging."
        ),
    )
    return render_document(
        title=f"Add competitor · {customer['name']}",
        content=header + form,
        active="customers",
        crumbs=(
            ("Customers", "/customers"),
            (customer["name"], f"/customer/{customer_id}"),
            ("Add competitor", None),
        ),
    )


# ---------------------------------------------------------------------------
# Monitor report
# ---------------------------------------------------------------------------


def _latest_scan_callout(
    *,
    monitor: MonitorView,
    latest: sqlite3.Row | None,
    first_capture_id: int | None,
    has_report: bool,
    after_report: bool = False,
) -> str:
    """Summarize the newest scan.

    after_report renders a failed scan as a compact warning placed below the
    retained business changes, so useful results stay first on the page.
    """
    notes: list[str] = []
    technical = ""
    if monitor.scan_error:
        tone = "error"
        title = "Latest scan failed."
        attempted = (
            f" Attempted {h(format_display_time(monitor.last_check_at))}."
            if monitor.last_check_at
            else ""
        )
        message = friendly_error(monitor.scan_error)
        body = f"{h(message)}{attempted}"
        if after_report:
            notes.append("The business changes above are from an earlier successful scan.")
        elif latest is not None:
            notes.append("Earlier results are still available below.")
        if message != monitor.scan_error:
            technical = disclosure(
                "Technical details",
                technical_list([("Error", h(monitor.scan_error))]),
                extra_class="disclosure--quiet",
            )
    elif latest is None:
        tone = "neutral"
        title = "Not checked yet."
        body = (
            "Run the first scan to take a baseline snapshot. Future scans "
            "will be compared with it."
        )
    else:
        scanned = h(format_display_time(latest["created_at"]))
        changes = safe_changes(latest["structured_changes"])
        categories = capture_categories(latest["categories"], changes)
        if changes:
            tone = "info"
            title = f"{plural(len(changes), 'meaningful business change')} detected."
            body = f"Scanned {scanned}."
        elif categories:
            tone = "info"
            title = (
                "The page changed, but no specific old-to-new business change "
                "was identified."
            )
            body = f"Scanned {scanned}."
            if has_report:
                notes.append("The most recent business changes are shown below.")
        elif latest["id"] == first_capture_id:
            tone = "info"
            title = "Monitoring started. Future scans will be compared with this snapshot."
            body = f"Snapshot taken {scanned}."
        else:
            tone = "neutral"
            title = "No meaningful business changes detected."
            body = f"Scanned {scanned}."
            if has_report:
                notes.append("The most recent business changes are shown below.")
    if not monitor.enabled:
        notes.append(
            "Automatic scans are off for this page. You can still scan it manually."
        )
    elif monitor.customer_paused:
        notes.append(
            f"{h(monitor.customer_name)} is paused, so scheduled scans and email "
            "alerts are stopped."
        )
    notes_html = "".join(f'<p class="callout__note">{note}</p>' for note in notes)
    compact = " callout--compact" if after_report else ""
    return f"""<section class="section section--tight" aria-labelledby="latest-scan-title">
<div class="callout callout--{tone}{compact}">
<h2 class="callout__eyebrow" id="latest-scan-title">Latest scan</h2>
<p class="callout__title">{h(title)}</p>
<p class="callout__body">{body}</p>
{notes_html}{technical}
</div>
</section>"""


def _business_changes_section(
    *,
    report: sqlite3.Row | None,
    changes: Sequence[StructuredChange],
    newer_scans: int,
) -> str:
    if report is None or not changes:
        body = empty_state(
            "No specific business changes identified yet.",
            "ChangeScout compares each scan with the previous one and reports "
            "changes to prices, promotions, shipping, products, calls to action, "
            "and messaging.",
        )
        return section(
            "Latest business changes",
            body,
            section_id="business-changes",
        )
    meta = facts(
        [
            f"Detected {h(format_display_time(report['created_at']))}",
            h(plural(len(changes), "change")),
            alert_summary_html(
                status=report["alert_status"],
                recipient=report["alert_recipient"],
                sent_at=report["alert_sent_at"],
            ),
        ]
    )
    note = (
        f'<p class="section__note">Showing the most recent scan with business changes. '
        f"{h(plural(newer_scans, 'newer scan'))} since then.</p>"
        if newer_scans
        else ""
    )
    return section(
        "Latest business changes",
        meta + note + change_cards(changes),
        section_id="business-changes",
    )


def _alert_delivery_text(capture: sqlite3.Row, *, changed: bool) -> str:
    status = capture["alert_status"] or "not_applicable"
    recipient = capture["alert_recipient"] or ""
    if status == "sent":
        when = (
            format_display_time(capture["alert_sent_at"])
            if capture["alert_sent_at"]
            else "an unknown time"
        )
        return h(f"Sent to {recipient} at {when}" if recipient else f"Sent at {when}")
    parts = [alert_status(status, changed=changed).label]
    if recipient:
        parts.append(f"Recipient: {recipient}")
    if capture["alert_error"]:
        parts.append(capture["alert_error"])
    return h(". ".join(parts))


def _history_section(
    *,
    history: Sequence[sqlite3.Row],
    first_capture_id: int | None,
) -> str:
    if not history:
        return section(
            "History",
            empty_state(
                "No completed scans yet.",
                "Completed scans appear here with their results and email alerts.",
            ),
            section_id="history",
        )
    rows = []
    technical_rows = []
    for capture in history:
        changes = safe_changes(capture["structured_changes"])
        categories = capture_categories(capture["categories"], changes)
        changed = bool(changes or categories)
        alert = alert_status(capture["alert_status"], changed=changed)
        result = scan_result(
            scan_error=None,
            has_capture=True,
            is_baseline=capture["id"] == first_capture_id,
            change_count=len(changes),
            categories=categories,
        )
        changes_html = (
            disclosure(
                f"Show {plural(len(changes), 'change')}",
                change_lines(changes),
                extra_class="disclosure--inline",
            )
            if changes
            else ""
        )
        rows.append(
            f"<tr><td>{h(format_display_time(capture['created_at']))}</td>"
            f'<td data-label="Result"><span class="result">{h(result.label)}</span>{changes_html}</td>'
            f'<td data-label="Categories">{category_list(categories) or NO_CATEGORIES}</td>'
            f'<td data-label="Alert">{status_badge(alert)}</td></tr>'
        )
        technical_rows.append(
            f'<tr><td class="cell-primary">{h(format_display_time(capture["created_at"]))}</td>'
            f'<td data-label="Visual difference" class="num">{h(score_text(capture["visual_score"]))}</td>'
            f'<td data-label="Text difference" class="num">{h(score_text(capture["text_score"]))}</td>'
            f'<td data-label="Alert delivery">{_alert_delivery_text(capture, changed=changed)}</td>'
            f'<td data-label="Scan summary">{h(capture["summary"] or "")}</td></tr>'
        )
    table = f"""<div class="card card--flush">
<table class="data-table data-table--stack history-table">
<caption class="visually-hidden">Scan history, newest first</caption>
<thead><tr><th scope="col">Checked</th><th scope="col">Result</th><th scope="col">Categories</th><th scope="col">Alert</th></tr></thead>
<tbody>{"".join(rows)}</tbody>
</table>
</div>"""
    technical = disclosure(
        "Technical details",
        f"""<p class="muted">Difference scores, delivery diagnostics, and the full scan summary for the scans above. Failed scans aren't stored in history; the latest failure is shown in the latest scan summary.</p>
<div class="table-scroll">
<table class="data-table data-table--stack data-table--stack-md tech-table">
<caption class="visually-hidden">Technical scan details</caption>
<thead><tr><th scope="col">Checked</th><th scope="col">Visual difference</th><th scope="col">Text difference</th><th scope="col">Alert delivery</th><th scope="col">Scan summary</th></tr></thead>
<tbody>{"".join(technical_rows)}</tbody>
</table>
</div>""",
        extra_class="disclosure--technical",
    )
    return section(
        "History",
        table + technical,
        section_id="history",
        description=h(f"The {plural(len(history), 'most recent scan')}, newest first."),
    )


def _settings_section(
    *,
    monitor: MonitorView,
    values: Mapping[str, str],
    errors: Mapping[str, str],
    email_ready: bool,
    email_error: str | None,
) -> str:
    field_ids = {
        "alert_email": "settings-alert-email",
        "check_frequency": "settings-frequency",
    }
    if monitor.customer_id is not None:
        recipient_hint = (
            "Leave blank to use the customer's email, "
            f'<span class="cell-email">{h(monitor.customer_email)}</span>.'
        )
    else:
        recipient_hint = "Needed to receive email alerts for this page."

    if not email_ready:
        email_state = (
            badge("Email unavailable", "warning"),
            "Email alerts are unavailable. Changes are still recorded in reports, "
            "but emails can't be sent until email delivery is set up.",
        )
    elif monitor.customer_paused:
        email_state = (
            badge("Paused", "warning"),
            f"Email alerts are paused because {h(monitor.customer_name)} is paused.",
        )
    elif not monitor.recipient:
        email_state = (
            badge("No recipient", "error"),
            "No alert recipient is set. Changes are recorded but not emailed.",
        )
    else:
        email_state = (
            badge("On", "success"),
            "Email alerts are on. Meaningful changes are emailed to "
            f'<span class="cell-email">{h(monitor.recipient)}</span>.',
        )
    email_technical = (
        disclosure(
            "Technical details",
            technical_list([("Email configuration", h(email_error or "Incomplete."))]),
            extra_class="disclosure--quiet",
        )
        if not email_ready
        else ""
    )
    customer_note = (
        f'<p class="field-note">{h(monitor.customer_name)} is paused, so automatic '
        "scans won't run until the customer is re-enabled.</p>"
        if monitor.customer_paused
        else ""
    )
    last_scan = (
        time_tag(monitor.last_check_at, full=True)
        if monitor.last_check_at
        else '<span class="muted">Not checked yet</span>'
    )
    form = f"""{form_error_summary(dict(errors), field_ids)}
<div class="card">
<form class="form settings-form" method="post" action="/target/{monitor.id}/settings" novalidate>
<div class="settings-grid">
<fieldset class="fieldset">
<legend class="fieldset__legend">Monitoring</legend>
{frequency_field(field_id="settings-frequency", selected=values.get("check_frequency", monitor.frequency), error=errors.get("check_frequency", ""))}
{checkbox_field(field_id="settings-enabled", name="enabled", label="Scan this page automatically", checked=values.get("enabled", "yes" if monitor.enabled else "") == "yes", hint="When off, the page is scanned only when you choose Scan now.")}
{customer_note}
<dl class="detail-list">
<div><dt>Last scan</dt><dd>{last_scan}</dd></div>
<div><dt>Next scan</dt><dd>{h(next_check_text(monitor.next_check))}</dd></div>
</dl>
</fieldset>
<fieldset class="fieldset">
<legend class="fieldset__legend">Notifications</legend>
{text_field(field_id="settings-alert-email", name="alert_email", label="Alert recipient", value=values.get("alert_email", monitor.alert_email or ""), input_type="email", error=errors.get("alert_email", ""), placeholder="owner@example.com", hint=recipient_hint, autocomplete="email")}
<div class="email-status"><p class="email-status__label">Email status</p><p class="email-status__value">{email_state[0]} <span>{email_state[1]}</span></p>{email_technical}</div>
</fieldset>
</div>
<div class="form-actions"><button class="btn btn--primary" type="submit" data-busy-label="Saving…">Save settings</button></div>
</form>
</div>"""
    return section(
        "Monitoring settings",
        form,
        section_id="settings",
    )


def report_page(
    *,
    now: datetime,
    monitor: MonitorView,
    history: Sequence[sqlite3.Row],
    first_capture_id: int | None,
    report: sqlite3.Row | None,
    report_changes: Sequence[StructuredChange],
    newer_scans: int,
    email_ready: bool,
    email_error: str | None,
    error: str | None = None,
    notice: str | None = None,
    settings_values: Mapping[str, str] | None = None,
    settings_errors: Mapping[str, str] | None = None,
) -> str:
    assigned = monitor.customer_id is not None
    if assigned:
        crumbs = (
            ("Customers", "/customers"),
            (monitor.customer_name or "Customer", f"/customer/{monitor.customer_id}"),
            (monitor.name, None),
        )
        active = "customers"
    else:
        crumbs = (("Monitors", "/monitors"), (monitor.name, None))
        active = "monitors"

    banners = _notice_banner(notice)
    if error and error != monitor.scan_error:
        banners += banner(h(friendly_error(error)), tone="error", title="Scan didn't run")
    settings_errors = settings_errors or {}
    if settings_errors:
        banners += banner(
            'Your changes weren\'t saved. <a href="#settings">Review monitoring settings</a>.',
            tone="error",
            title="Settings not saved",
        )

    subtitle_parts = []
    if assigned:
        subtitle_parts.append(
            f'for <a href="/customer/{monitor.customer_id}">{h(monitor.customer_name)}</a>'
        )
    else:
        subtitle_parts.append('<span class="muted">No customer assigned</span>')
    subtitle_parts.append(page_link(monitor.url))
    subtitle = facts(subtitle_parts, lead=True)

    last_checked = (
        format_display_time(monitor.last_check_at)
        if monitor.last_check_at
        else "Not checked yet"
    )
    overdue = (
        monitor.next_check.kind == "scheduled"
        and monitor.next_check.at is not None
        and monitor.next_check.at <= now
    )
    meta = facts(
        [
            h(cadence(monitor.frequency) if monitor.enabled else "Automatic scans off"),
            h(f"Last checked: {last_checked}"),
            h(f"Next check: {next_check_text(monitor.next_check)}")
            + (f" {badge('Due', 'warning')}" if overdue else ""),
        ]
    )
    header = page_header(
        monitor.name,
        title_badges=status_badge(monitor.status),
        meta=subtitle + meta,
        actions=post_button(
            f"/check/{monitor.id}",
            "Scan now",
            variant="primary",
            busy_label="Scanning…",
        ),
    )
    latest = history[0] if history else None
    values = dict(settings_values or {})
    has_report = report is not None and bool(report_changes)
    # A newer failed scan must not push retained business changes down the page.
    failure_after_report = bool(monitor.scan_error) and has_report
    latest_html = _latest_scan_callout(
        monitor=monitor,
        latest=latest,
        first_capture_id=first_capture_id,
        has_report=has_report,
        after_report=failure_after_report,
    )
    business_html = _business_changes_section(
        report=report,
        changes=report_changes,
        newer_scans=newer_scans,
    )
    content = (
        banners
        + header
        + (
            business_html + latest_html
            if failure_after_report
            else latest_html + business_html
        )
        + _history_section(history=history, first_capture_id=first_capture_id)
        + _settings_section(
            monitor=monitor,
            values=values,
            errors=settings_errors,
            email_ready=email_ready,
            email_error=email_error,
        )
    )
    return render_document(
        title=monitor.name,
        content=content,
        active=active,
        crumbs=crumbs,
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def error_page(*, title: str, message: str) -> str:
    content = page_header(title, description=h(message)) + (
        '<div class="button-row">'
        + link_button("Go to Overview", "/", "primary")
        + link_button("View customers", "/customers", "secondary")
        + "</div>"
    )
    return render_document(title=title, content=content)
