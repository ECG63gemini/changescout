# ChangeScout MVP

A deliberately small competitor-page monitoring product.

## What it does
- Add a competitor URL
- Group competitor monitors by managed-service customer
- Present a public product overview and collect start requests
- Capture the page as a screenshot
- Extract visible text
- Compare the newest capture with the previous one
- Flag meaningful visual/text changes
- Classify text changes as price, promotion, shipping, product, CTA, positioning, or other
- Show high-confidence old → new business changes with deterministic impact explanations
- Automatically check enabled monitors hourly, every 6 hours, or daily
- Send deduplicated email alerts through Resend HTTPS or SMTP for meaningful structured business changes
- Optionally use `browser-use`'s OpenAI client to summarize already-extracted changes

This is designed as a **managed-service MVP**. Do not add teams, complex billing, multi-user auth, Slack, or scheduled jobs until customers pay.

## Monitoring safeguards

- Only public HTTP/HTTPS URLs are accepted. ChangeScout rejects internal hostnames and DNS results in private, loopback, link-local, or reserved IP ranges.
- Browser requests and redirects are revalidated during capture.
- Captures retry once after browser failures, do not require `networkidle`, and cap screenshots at 12,000 pixels high.
- Navigation/footer boilerplate, scripts, styles, hidden content, and repeated text are excluded from text comparisons.
- Failed checks are not stored as successful captures and show a useful message on the monitor's report.

## Local setup

Requires Python 3.11+.

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
playwright install chromium
cp .env.example .env
uvicorn app:app --reload
```

Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` in `.env` before opening the operator
application. Open http://127.0.0.1:8000/ for the public product page or
http://127.0.0.1:8000/app for the protected operator overview.

Run the offline test suite with:

```bash
pytest -q
```

## Automatic monitoring

Each target has three operator-managed settings:

- automatic monitoring enabled or disabled
- alert email
- check frequency: hourly, every 6 hours, or daily

The scheduler runs inside the FastAPI process, checks for due targets every 60
seconds by default, and uses the same secure capture pipeline as manual checks.
Run exactly one application process for this MVP; multiple Uvicorn workers or
multiple app instances could run duplicate checks. This single-process
limitation is intentional until the managed service needs external worker
infrastructure.

Set `SCHEDULER_POLL_SECONDS` to a smaller value such as `5` for local testing.
Targets with no previous attempt are due immediately. A failed attempt is
recorded and does not prevent other due targets from running.

Targets created from a customer page use that customer's email as their alert
recipient. Disabling a customer pauses scheduled checks for all of that
customer's targets without deleting targets or history. Re-enabling the
customer makes enabled targets eligible for the scheduler again. Existing
targets that are not assigned to a customer continue to run normally.

Set `DISPLAY_TIMEZONE=America/New_York` in `.env` to show UI and email times
in that timezone. If unset or invalid, displayed times use UTC. Stored
timestamps and monitoring schedules remain in UTC. The page footer names the
zone in plain language, for example "Times shown in Eastern Time."

## Operator customer onboarding

Open **Customers** from the top navigation, then:

1. Select **Create customer**, enter the customer's name and alert email, and
   select **Create customer**.
2. On the customer page, select **Add competitor** and enter the competitor
   name, the page to monitor, and the scan frequency. The form shows which
   email address will receive alerts; it defaults to the customer's email.
3. Select **Scan now** on the new competitor's report to take the baseline
   snapshot immediately, or let the scheduler take it on its next cycle.
4. Use **Disable customer** (under **Customer controls** at the bottom of the
   customer page) to pause every scheduled monitor for that customer while
   retaining all reports and history.

Monitors that are not assigned to a customer can still be created from
**Monitors → Add a monitor without a customer**, which keeps the explicit
per-monitor alert email behavior.

## Interface

The UI is server-rendered HTML with no frontend framework or build step:

- **Public landing page** (`/`, with `/landing` as an alias): product overview, example report,
  pricing, and the start/contact form.
- **Overview** (`/app`): a snapshot, recent meaningful changes across all
  customers, the top items needing attention (paused customers aren't treated
  as problems), and a compact monitoring overview.
- **Customers** (`/app/customers`, `/app/customer/{id}`): customer list and detail with
  that customer's changes, competitor pages, and controls.
- **Monitors** (`/app/monitors`): every monitor across customers.
- **Report** (`/app/target/{id}`): latest scan result, the most recent business
  changes (kept visible after later no-change scans, and shown above the
  failure notice when the newest scan failed), history, and monitoring
  settings. Difference scores, delivery diagnostics, and original error
  messages are under **Technical details**.

Page rendering lives in `pages.py`, shared components and status vocabulary in
`ui.py`, and the stylesheets in `static/app.css` and `static/landing.css`.
Invalid form input is shown inline on the form, and missing pages render an
HTML error page instead of JSON.

## Public inquiries

The public form at `/` (also available at `/landing`) posts to
`/landing/contact` and redirects to `/?submitted=1#contact`. It does not create
an account or start billing. A valid submission is stored in SQLite before any
optional operator email and redirects back to an on-page confirmation.
Configure `INQUIRY_NOTIFICATION_EMAIL` with either email provider below to notify
an operator. Missing email configuration or a delivery failure does not change
inquiry storage or the visitor confirmation.

Startup creates one additive table when it is missing:

```sql
CREATE TABLE IF NOT EXISTS inquiries(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    business_name TEXT NOT NULL,
    email TEXT NOT NULL,
    website TEXT,
    message TEXT,
    created_at TEXT NOT NULL
);
```

This table has no foreign keys and does not alter or read customer, target, or
capture records. Re-running startup preserves existing inquiries.

## Email alerts

Email delivery is selected once per send:

1. When both `RESEND_API_KEY` and `RESEND_FROM_EMAIL` are nonblank, all change
   alerts, inquiry notifications, and test emails use Resend's HTTPS API at
   `https://api.resend.com/emails`.
2. Otherwise, the existing SMTP configuration and delivery behavior are used.
   Partial or blank Resend configuration does not disable SMTP.

Resend requires no additional Python dependency. Configure it in `.env` with
an API key that can send email and a sender address on a verified Resend domain:

```dotenv
RESEND_API_KEY=re_your_resend_api_key
RESEND_FROM_EMAIL=alerts@your-verified-domain.example
INQUIRY_NOTIFICATION_EMAIL=operator@example.com
APP_BASE_URL=https://changescout.example.com
```

Use a plain sender email address for `RESEND_FROM_EMAIL`, without a display
name. For initial testing, Resend's `onboarding@resend.dev` sender is restricted
to your Resend account's own email address; use a verified domain for production
and customer recipients.

To use vendor-neutral SMTP locally or on a host where SMTP is available, leave
at least one Resend variable unset and configure the existing settings:

```dotenv
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=your-smtp-user
SMTP_PASSWORD=your-smtp-password
SMTP_FROM=alerts@example.com
SMTP_USE_TLS=true
INQUIRY_NOTIFICATION_EMAIL=operator@example.com
APP_BASE_URL=https://changescout.example.com
```

`SMTP_USERNAME` and `SMTP_PASSWORD` may both be blank for an SMTP relay that
does not require authentication. `APP_BASE_URL` is the optional public site
origin, without `/app` (for example `https://changescout.example.com`).
`INQUIRY_NOTIFICATION_EMAIL` is optional and receives new landing-page
inquiries when the selected email provider is configured. Inquiry notification failures are
logged without changing the saved inquiry or public confirmation.
Report links append `/app/target/{id}` and require operator authentication;
these are not public customer report links. Existing emails with old
`/target/{id}` links must be opened using `/app/target/{id}` instead.

A change alert is sent only when a capture contains deterministic structured
business changes. Baselines, no-change captures, and visual-only differences
do not send change alerts. Each capture records whether its alert was sent,
skipped because configuration was missing, or failed. Monitoring and reports
continue when neither email provider is configured. Resend API errors,
timeouts, and invalid responses are reported as delivery failures without
failing the capture or inquiry. A Resend failure does **not** retry through SMTP;
the existing per-capture deduplication and inquiry storage-before-notification
behavior are unchanged. API keys, SMTP passwords, authorization headers, and
raw Resend error bodies are not logged.

Test the selected email provider without creating a page change:

```bash
python send_test_email.py owner@example.com
```

For a local Resend smoke test:

1. Set both Resend variables and `INQUIRY_NOTIFICATION_EMAIL` in `.env`. Keep
   API keys and passwords private and out of source control.
2. Run the test-email command above from your activated virtual environment.
   Check the recipient inbox and the Resend email log. A successful command
   means the API accepted the message; inbox delivery can still be affected by
   bounces or spam filtering.
3. Start or restart ChangeScout and submit the public landing-page contact
   form. Confirm the visitor confirmation and operator notification.
4. Monitor a public test page you control with your email as the alert
   recipient. Select **Scan now** for a baseline, change a visible price
   (for example $99 to $79), then scan again. Confirm one meaningful-change
   alert. A further unchanged scan should not send another alert.
5. To check SMTP fallback, remove either Resend variable from the process
   environment and `.env`, restart, and rerun the test-email command with
   working SMTP settings.

## Railway deployment

Railway builds the service from the included `Dockerfile`. Mount a Railway
persistent volume at `/data` and configure:

```dotenv
DATA_DIR=/data
```

Also add these Railway service variables before public launch:

```dotenv
ADMIN_USERNAME=<your-private-operator-username>
ADMIN_PASSWORD=<your-long-random-operator-password>
APP_BASE_URL=https://<your-public-domain>
INQUIRY_NOTIFICATION_EMAIL=<your-operator-email-address>
```

Outbound SMTP is blocked on the current Railway hosting plan. For production
email, add both of these service variables so delivery uses HTTPS on port 443:

```dotenv
RESEND_API_KEY=<your-resend-api-key-with-email-sending-permission>
RESEND_FROM_EMAIL=alerts@your-verified-domain.example
```

Set `INQUIRY_NOTIFICATION_EMAIL` to receive landing-page inquiry notifications;
change-alert recipients still come from the existing customer/monitor settings,
and the test-email command takes its recipient as an argument. SMTP variables
are not required on Railway when both Resend variables are set. Existing SMTP
values may remain configured; Resend takes precedence. Do not rely on SMTP
fallback on this Railway plan.

Keep the existing `DATA_DIR`, scheduler, volume, replica, and start-command
settings unchanged. `APP_BASE_URL` is optional if you do not want email report
links; otherwise set it to the site origin, not the `/app` URL.

After applying the variables and restarting the service when ready, open a
shell **inside the running Railway service** and run:

```bash
python send_test_email.py owner@example.com
```

Confirm acceptance in Resend and receipt in the inbox. Then submit the public
contact form and repeat the controlled-page baseline/price-change/unchanged
scan checks described above against the Railway site. Running the command
locally with Railway variables alone does not verify connectivity from the
Railway service. Delivery failures should leave captures and inquiries saved
and produce credential-free diagnostics.

The image starts exactly one application process with Railway's `PORT`:

```bash
uvicorn app:app --host 0.0.0.0 --port "$PORT"
```

ChangeScout will create the directory structure and store its SQLite database
and screenshots under that volume. When `DATA_DIR` is unset or blank, local
behavior is unchanged and runtime data stays in the repository's `data/`
directory. Do not override the start command with multiple workers, and keep
the Railway service at one replica because the scheduler is single-process.
On the first deployment of customer management, startup creates the
`customers` table and adds a nullable `customer_id` column to `targets`.
The public landing page change also creates the standalone `inquiries` table
described above. Existing monitors remain unassigned and continue working.

## Operator authentication

All `/app` paths and methods, including form submissions, use HTTP Basic
authentication. Credentials come only from `ADMIN_USERNAME` and `ADMIN_PASSWORD`
and are compared using constant-time digest comparisons; they are not stored
in SQLite or logged. Use ASCII credentials, a username without a colon, and a
long randomly generated password (at least 32 characters recommended). Never
put credentials in URLs or source control. Production access **must use HTTPS**:
Basic authentication encodes credentials but does not encrypt them.

If either variable is missing or blank, `/app` fails closed with HTTP 503.
Missing, malformed, or incorrect credentials return HTTP 401 with a browser
authentication prompt. Operator responses are marked `Cache-Control: no-store`.
Cross-site browser POST actions are rejected. The landing page, contact form,
and static assets remain public; monitoring and the scheduler do not require
HTTP authentication and keep running even if credentials are unset. API docs
and the OpenAPI endpoint are disabled.

Every previous operator URL now has an `/app` prefix, including
`/app/customers/new`, `/app/customer/{id}/targets/new`, and POST actions
`/app/customers`, `/app/targets`, `/app/customer/{id}/targets`,
`/app/customer/{id}/status`, `/app/target/{id}/settings`, and `/app/check/{id}`.
Old unprefixed operator paths return 404 rather than expose data or controls.
Update saved bookmarks; `/` is now marketing, not the dashboard.

Manual launch checks:

1. In a fresh private browser window, visit `/` and submit the contact form.
   Neither should prompt for credentials.
2. Visit `/app`, `/app/customers`, and an existing `/app/target/{id}`.
   Each must require authentication before showing operator data.
3. Enter incorrect credentials and verify access is denied. Then enter the
   configured credentials and verify the overview, customer pages, reports,
   and settings work.
4. Without authentication, send a POST to `/app/check/{id}`; it must return
   401 and must not run a scan. The same applies to customer and settings POSTs.
5. Follow a newly generated email report link; it should open the protected
   `/app/target/{id}` page. Check that scheduled scans continue as before.

Browsers cache Basic credentials and there is no application logout button.
Close all private windows or use a separate fresh browser session to re-test
unauthenticated access. Rotate credentials through environment variables when
needed; do not share operator credentials with customers.

## Controlled report demo

The included fixtures demonstrate a $99 → $79 price decrease, a lower
free-shipping threshold, a CTA change, and a newly added 20% promotion.
They do not use the internet or relax URL security.

With ChangeScout running, seed the baseline:

```bash
python seed_demo.py before
```

Open the report URL printed by the command. In a second terminal, apply the
controlled after state:

```bash
python seed_demo.py after
```

Refresh the same report page to show the price, shipping, promotion, and
call-to-action changes. Demo monitors are created with automatic monitoring
disabled (shown as **Paused**) so the scheduler does not replace the
deterministic fixture.

## Fastest launch model
1. Create the customer once from the customer management page.
2. Add the customer's competitor URLs with their monitoring frequencies.
3. Keep one ChangeScout process running to perform due checks and send alerts.
4. Charge a setup fee + monthly monitoring.
5. Add external worker infrastructure only after customer demand justifies it.

## Commercialization note
`browser-use` is MIT licensed as of version 0.13.10. Keep required copyright/license notices for any code you directly redistribute from upstream. This starter does not copy upstream source code; it depends on the package.

## Responsible use
Monitor public pages only. Do not bypass logins, paywalls, CAPTCHAs, access controls, or site restrictions.