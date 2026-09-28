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
PROMOTION, and CTA changes.

## Fastest launch model
1. You manually add each customer's competitor URLs.
2. Run checks once a day or once a week.
3. Send the generated change report yourself.
4. Charge a setup fee + monthly monitoring.
5. Automate scheduling/email only after someone pays.

## Commercialization note
`browser-use` is MIT licensed as of version 0.13.10. Keep required copyright/license notices for any code you directly redistribute from upstream. This starter does not copy upstream source code; it depends on the package.

## Responsible use
Monitor public pages only. Do not bypass logins, paywalls, CAPTCHAs, access controls, or site restrictions.