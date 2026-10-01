# ChangeScout MVP

A deliberately small competitor-page monitoring product.

## What it does
- Add a competitor URL
- Capture the page as a screenshot
- Extract visible text
- Compare the newest capture with the previous one
- Flag meaningful visual/text changes
- Classify text changes as price, promotion, shipping, product, CTA, positioning, or other
- Show high-confidence old → new business changes with deterministic impact explanations
- Automatically check enabled monitors hourly, every 6 hours, or daily
- Send deduplicated SMTP alerts for meaningful structured business changes
- Optionally use `browser-use`'s OpenAI client to summarize already-extracted changes

This is designed as a **managed-service MVP**. Do not add teams, complex billing, multi-user auth, Slack, or scheduled jobs until customers pay.

## Monitoring safeguards

- Only public HTTP/HTTPS URLs are accepted. ChangeScout rejects internal hostnames and DNS results in private, loopback, link-local, or reserved IP ranges.
- Browser requests and redirects are revalidated during capture.
- Captures retry once after browser failures, do not require `networkidle`, and cap screenshots at 12,000 pixels high.
- Navigation/footer boilerplate, scripts, styles, hidden content, and repeated text are excluded from text comparisons.
- Failed checks are not stored as successful captures and show a useful message on the target report.

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

Open http://127.0.0.1:8000

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

Set `DISPLAY_TIMEZONE=America/New_York` in `.env` to show UI and email times
in that timezone. If unset or invalid, displayed times use UTC. Stored
timestamps and monitoring schedules remain in UTC.

## Email alerts

Configure vendor-neutral SMTP settings in `.env`:

```dotenv
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=your-smtp-user
SMTP_PASSWORD=your-smtp-password
SMTP_FROM=alerts@example.com
SMTP_USE_TLS=true
APP_BASE_URL=https://changescout.example.com
```

`SMTP_USERNAME` and `SMTP_PASSWORD` may both be blank for an SMTP relay that
does not require authentication. `APP_BASE_URL` is optional and adds a report
link to alert emails.

Email is sent only when a capture contains deterministic structured business
changes. Baselines, no-change captures, and visual-only differences do not
send email. Each capture records whether its alert was sent, skipped because
configuration was missing, or failed. Monitoring and reports continue when
SMTP is not configured.

Test SMTP configuration without creating a page change:

```bash
python send_test_email.py owner@example.com
```

## Railway deployment

Railway builds the service from the included `Dockerfile`. Mount a Railway
persistent volume at `/data` and configure:

```dotenv
DATA_DIR=/data
```

The image starts exactly one application process with Railway's `PORT`:

```bash
uvicorn app:app --host 0.0.0.0 --port "$PORT"
```

ChangeScout will create the directory structure and store its SQLite database
and screenshots under that volume. When `DATA_DIR` is unset or blank, local
behavior is unchanged and runtime data stays in the repository's `data/`
directory. Do not override the start command with multiple workers, and keep
the Railway service at one replica because the scheduler is single-process.

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

Refresh the same report page to show the structured PRICE, SHIPPING,
PROMOTION, and CTA changes. Demo targets are created with automatic monitoring
disabled so the scheduler does not replace the deterministic fixture.

## Fastest launch model
1. You manually add each customer's competitor URLs.
2. Configure the customer's alert recipient and monitoring frequency.
3. Keep one ChangeScout process running to perform due checks and send alerts.
4. Charge a setup fee + monthly monitoring.
5. Add external worker infrastructure only after customer demand justifies it.

## Commercialization note
`browser-use` is MIT licensed as of version 0.13.10. Keep required copyright/license notices for any code you directly redistribute from upstream. This starter does not copy upstream source code; it depends on the package.

## Responsible use
Monitor public pages only. Do not bypass logins, paywalls, CAPTCHAs, access controls, or site restrictions.