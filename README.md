# ChangeScout MVP

A deliberately small competitor-page monitoring product.

## What it does
- Add a competitor URL
- Capture the page as a screenshot
- Extract visible text
- Compare the newest capture with the previous one
- Flag meaningful visual/text changes
- Optionally use `browser-use` + OpenAI to summarize what changed

This is designed as a **managed-service MVP**. Do not add teams, complex billing, multi-user auth, Slack, or scheduled jobs until customers pay.

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