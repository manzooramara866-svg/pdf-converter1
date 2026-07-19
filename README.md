# PDF → Spreadsheet Converter (MVP)

A small SaaS tool: upload a PDF (bank statement, invoice, report), get back a clean .xlsx or .csv.

## What's here
- `main.py` — FastAPI backend. Extracts tables with pdfplumber, falls back to raw text lines if no table is detected. Includes a simple in-memory freemium limiter (5 free conversions/day per client, returns HTTP 429 with an "upgrade" message past that).
- `static/index.html` — Single-page frontend: drag-and-drop upload, format toggle (xlsx/csv), usage meter.

## Run it locally
```bash
pip install fastapi uvicorn pdfplumber pandas openpyxl python-multipart
uvicorn main:app --reload --port 8000
```
Then open http://localhost:8000 in your browser.

## What's real vs. stubbed
**Real and working (tested):**
- Table extraction from PDFs — correctly pulled Date/Description/Amount/Balance columns from a sample statement
- XLSX and CSV export
- Usage limiting logic (429 after 5 free conversions/day per client)
- Stripe Checkout session creation, webhook handler for `checkout.session.completed` / `customer.subscription.deleted`, and a Pro tier that bypasses the limiter
- Everything degrades gracefully if Stripe isn't configured (returns a 503 instead of crashing)

**Still stubbed / needs work before you fully trust it with money:**
- Usage + pro-status tracking is in-memory (`usage_log`, `pro_users` dicts) — resets on every server restart and won't work if you scale to more than one server instance. Swap for Redis or a small Postgres table before real launch.
- "client_id" is just a string the frontend makes up and stores nowhere permanent — add real accounts (even just email + magic link, or a persistent browser-stored ID) so a user's Pro status survives clearing cookies or switching devices.
- No file size limit enforced server-side (frontend just hints at 20MB).
- pdfplumber handles typical bank/invoice tables well, but scanned or multi-column PDFs will need OCR (`pytesseract`) as a further fallback.

## Setting up Stripe (once you're ready to charge)
1. Create a Stripe account, then in the Dashboard create a **Product** (e.g. "Pro — Unlimited Conversions") with a recurring **Price** (e.g. $12/mo). Copy its Price ID (`price_...`).
2. Get your **Secret key** from Developers → API keys.
3. Set up a webhook endpoint in Developers → Webhooks pointing at `https://yourdomain.com/billing/webhook`, subscribed to `checkout.session.completed` and `customer.subscription.deleted`. Copy the signing secret (`whsec_...`).
4. Set these environment variables wherever you deploy:
   ```
   STRIPE_SECRET_KEY=sk_live_...
   STRIPE_WEBHOOK_SECRET=whsec_...
   STRIPE_PRICE_ID=price_...
   APP_BASE_URL=https://yourdomain.com
   ```
5. Start in **test mode** first (use `sk_test_...` keys and Stripe's test card `4242 4242 4242 4242`) before flipping to live keys.

## Deploying it (so it's live 24/7, not just on your laptop)
Any of these work well for a small FastAPI app and have free or near-free starter tiers:

- **Railway** (railway.app) — easiest: connect your GitHub repo, it detects `requirements.txt`, set env vars in the dashboard, done. Good default choice if you've never deployed a backend before.
- **Render** (render.com) — similar to Railway, free tier available (spins down when idle, which is fine for early traffic).
- **Fly.io** — a bit more setup (needs a `Dockerfile` or `fly launch`), but cheap and fast once running.

General steps for any of them:
1. Push this folder to a GitHub repo.
2. Connect the repo to the platform, set the start command to `uvicorn main:app --host 0.0.0.0 --port $PORT`.
3. Add the Stripe environment variables from above.
4. Point your domain (or use the free subdomain they give you) — update `APP_BASE_URL` to match.

## Next steps toward "idle" income
1. Get Stripe working end-to-end in test mode, confirm the webhook actually flips a test user to Pro.
2. Deploy it live (Railway is the fastest path).
3. Pick a narrow niche for the landing page copy — "PDF converter for everyone" doesn't convert; "convert your bank statement to Excel in 10 seconds" or "for freelance bookkeepers who are sick of retyping invoices" does.
4. Write one or two blog posts targeting real search terms like "convert bank statement PDF to Excel" — this is a genuinely searched phrase, and a working free tool ranks well over time with very little upkeep.
