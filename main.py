"""
PDF -> Excel/CSV Converter SaaS - MVP backend
Extracts tables (and fallback text) from uploaded PDFs (bank statements, invoices)
and returns a clean Excel or CSV file.

Run: uvicorn main:app --reload --port 8000

Environment variables (for Stripe billing):
  STRIPE_SECRET_KEY      - your Stripe secret key (sk_live_... or sk_test_...)
  STRIPE_WEBHOOK_SECRET  - signing secret for the /billing/webhook endpoint
  STRIPE_PRICE_ID        - the Price ID of your "Pro unlimited" subscription
  APP_BASE_URL           - public URL of this app, e.g. https://yourapp.com
"""

from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import pdfplumber
import pandas as pd
import uuid
import os
import time
import json
from collections import defaultdict

try:
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_ENABLED = bool(stripe.api_key)
except ImportError:
    STRIPE_ENABLED = False

STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")

app = FastAPI(title="PDF Converter API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OUTPUT_DIR = "outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# --- Simple in-memory usage limiter (per-IP) to simulate a freemium tier ---
# NOTE: in-memory storage is fine for a demo/MVP but resets on restart and
# doesn't work across multiple server instances. Swap for Redis or a DB
# table before real launch.
FREE_LIMIT_PER_DAY = 5
usage_log = defaultdict(list)  # client_id -> list of timestamps
pro_users = set()              # client_id's with an active Stripe subscription


def check_and_record_usage(client_id: str):
    if client_id in pro_users:
        return  # unlimited for paying users
    now = time.time()
    one_day_ago = now - 86400
    usage_log[client_id] = [t for t in usage_log[client_id] if t > one_day_ago]
    if len(usage_log[client_id]) >= FREE_LIMIT_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=f"Free limit of {FREE_LIMIT_PER_DAY} conversions/day reached. Upgrade to Pro for unlimited conversions.",
        )
    usage_log[client_id].append(now)


# --- Stripe billing ---

@app.post("/billing/checkout")
def create_checkout_session(client_id: str = Query(...)):
    """Creates a Stripe Checkout session for the Pro subscription and
    returns the URL to redirect the user to."""
    if not STRIPE_ENABLED:
        raise HTTPException(status_code=503, detail="Billing is not configured on this server yet.")
    if not STRIPE_PRICE_ID:
        raise HTTPException(status_code=503, detail="STRIPE_PRICE_ID is not set.")

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=f"{APP_BASE_URL}/?checkout=success",
        cancel_url=f"{APP_BASE_URL}/?checkout=cancelled",
        client_reference_id=client_id,
        metadata={"client_id": client_id},
    )
    return {"checkout_url": session.url}


@app.post("/billing/webhook")
async def stripe_webhook(request: Request):
    """Stripe calls this when subscription events happen. Point your Stripe
    dashboard's webhook endpoint at {APP_BASE_URL}/billing/webhook and
    subscribe to: checkout.session.completed, customer.subscription.deleted."""
    if not STRIPE_ENABLED:
        raise HTTPException(status_code=503, detail="Billing is not configured on this server.")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        client_id = data.get("client_reference_id") or data.get("metadata", {}).get("client_id")
        if client_id:
            pro_users.add(client_id)

    elif event_type == "customer.subscription.deleted":
        client_id = data.get("metadata", {}).get("client_id")
        if client_id:
            pro_users.discard(client_id)

    return JSONResponse({"received": True})


@app.get("/billing/status")
def billing_status(client_id: str = Query(...)):
    return {"client_id": client_id, "is_pro": client_id in pro_users}


def extract_tables_from_pdf(path: str) -> pd.DataFrame:
    """Try structured table extraction first; fall back to raw text lines."""
    all_rows = []
    header = None

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if tables:
                for table in tables:
                    if not table:
                        continue
                    if header is None:
                        header = table[0]
                        all_rows.extend(table[1:])
                    else:
                        # skip repeated header rows on subsequent pages
                        start_idx = 1 if table[0] == header else 0
                        all_rows.extend(table[start_idx:])

    if all_rows and header:
        # normalize row lengths to header length
        norm_rows = [r + [None] * (len(header) - len(r)) for r in all_rows]
        df = pd.DataFrame(norm_rows, columns=header)
        return df

    # Fallback: no tables detected, extract raw text line by line
    lines = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(text.split("\n"))
    lines = [l for l in lines if l.strip()]
    return pd.DataFrame({"line": lines})


@app.get("/api/health")
def health():
    return {"status": "ok", "message": "PDF Converter API is running"}


@app.get("/usage")
def usage(ip: str = Query(default="demo")):
    used = len(usage_log.get(ip, []))
    is_pro = ip in pro_users
    return {
        "used": used,
        "limit": FREE_LIMIT_PER_DAY,
        "remaining": "unlimited" if is_pro else max(0, FREE_LIMIT_PER_DAY - used),
        "is_pro": is_pro,
    }


@app.post("/convert")
async def convert(file: UploadFile = File(...), out_format: str = Query(default="xlsx", enum=["xlsx", "csv"]), client_ip: str = Query(default="demo")):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    check_and_record_usage(client_ip)

    job_id = str(uuid.uuid4())
    input_path = os.path.join(OUTPUT_DIR, f"{job_id}.pdf")
    with open(input_path, "wb") as f:
        f.write(await file.read())

    try:
        df = extract_tables_from_pdf(input_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse PDF: {e}")
    finally:
        os.remove(input_path)

    if df.empty:
        raise HTTPException(status_code=422, detail="No extractable content found in this PDF.")

    out_filename = f"{job_id}.{out_format}"
    out_path = os.path.join(OUTPUT_DIR, out_filename)

    if out_format == "xlsx":
        df.to_excel(out_path, index=False)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        df.to_csv(out_path, index=False)
        media_type = "text/csv"

    return FileResponse(
        out_path,
        media_type=media_type,
        filename=f"converted.{out_format}",
    )


# Serve the simple frontend
if os.path.isdir("static"):
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
