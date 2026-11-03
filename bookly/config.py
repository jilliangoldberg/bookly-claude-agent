"""Every setting in one place: model, frozen date, and policy numbers.
A non-engineer should be able to change policy here without touching logic."""

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

# Reads ANTHROPIC_API_KEY etc. from .env if present
load_dotenv()

# --- LLM ---
MODEL = os.getenv("BOOKLY_MODEL", "claude-sonnet-5-5")
# Fast replies; hard rules live in code, not model reasoning
EFFORT = "low"
# Ceiling, not target; thinking counts toward it
MAX_TOKENS = 16000
# LLM calls per customer message before hand-off
MAX_AGENT_STEPS = 8

# --- Time ---
# Frozen so return-window math and the demo give the same answers every run
TODAY = date.fromisoformat(os.getenv("BOOKLY_TODAY", "2026-10-24"))

# --- Policy ---
# Failed email+ZIP tries before verification locks
MAX_VERIFY_ATTEMPTS = 3
# Physical books: returnable through day 30 after delivery
RETURN_WINDOW_DAYS = 30
# Ebooks: refundable within 7 days, only if never downloaded
EBOOK_REFUND_DAYS = 7
# Once per change-of-mind return refunded to original payment
RETURN_LABEL_FEE = 4.99

# Timing text per resolution; Dewey repeats these, never invents one
RESOLUTION_TIMING = {
    "refund_original": "Refund to your original payment 5-7 business days after the return arrives.",
    "store_credit": "Store credit is added to your account as soon as the return arrives.",
    "replacement": "A replacement ships within 1-2 business days; send the original back with the prepaid label.",
}

# --- Paths ---
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
