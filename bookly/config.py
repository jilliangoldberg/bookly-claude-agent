"""Every setting in one place: model, frozen date, and policy numbers.
A non-engineer should be able to change policy here without touching logic."""

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # read ANTHROPIC_API_KEY etc. from .env if present

# --- LLM ---
MODEL = os.getenv("BOOKLY_MODEL", "claude-sonnet-5-5")
EFFORT = "low"  # chat needs fast replies; the hard rules live in code, not in model reasoning
MAX_TOKENS = 16000  # a ceiling, not a target; thinking tokens count toward it, so don't lowball
MAX_AGENT_STEPS = 8  # LLM calls per customer message before we give up and hand off

# --- Time ---
# Frozen so return-window math and the demo give the same answers every run.
TODAY = date.fromisoformat(os.getenv("BOOKLY_TODAY", "2026-10-24"))

# --- Policy ---
MAX_VERIFY_ATTEMPTS = 3  # failed email+ZIP tries before verification locks for the session
RETURN_WINDOW_DAYS = 30  # physical books: returnable through day 30 after delivery, not day 31
EBOOK_REFUND_DAYS = 7  # ebooks: refundable within 7 days of purchase, only if never downloaded
RETURN_LABEL_FEE = 4.99  # charged once per change-of-mind return refunded to the original payment

# What the customer is told about timing, by resolution. Dewey repeats these; it never invents one.
RESOLUTION_TIMING = {
    "refund_original": "Refund to your original payment 5-7 business days after the return arrives.",
    "store_credit": "Store credit is added to your account as soon as the return arrives.",
    "replacement": "A replacement ships within 1-2 business days; send the original back with the prepaid label.",
}

# --- Paths ---
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
