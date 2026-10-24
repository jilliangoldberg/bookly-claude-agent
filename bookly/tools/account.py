"""Account tools: proving who the customer is. Never reveals whether an email exists."""

from bookly import data
from bookly.config import MAX_VERIFY_ATTEMPTS
from bookly.models import SessionState, tool_error, tool_ok

# One message for every failure, so a caller can't tell a wrong email from a wrong ZIP.
VERIFY_FAILED_MESSAGE = "That email and ZIP code don't match an account."

VERIFY_CUSTOMER_SCHEMA = {
    "name": "verify_customer",
    "description": (
        "Verify the customer's identity with their account email and billing ZIP code. "
        "Call this before discussing any order. Ask the customer for both values first; never guess them. "
        "Do not call it again once the customer is verified."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "email": {"type": "string", "description": "The account email the customer gave"},
            "zip_code": {"type": "string", "description": "The 5-digit ZIP code the customer gave"},
        },
        "required": ["email", "zip_code"],
        "additionalProperties": False,
    },
}


def verify_customer(session: SessionState, email: str, zip_code: str) -> dict:
    """Check email + ZIP against the customer list; on a match, mark the session verified."""
    if session.verify_attempts >= MAX_VERIFY_ATTEMPTS:
        return tool_error("VERIFY_LOCKED", "Too many failed attempts. Verification is locked for this chat.")

    match = next(
        (
            c
            for c in data.customers()
            if c["email"].lower() == email.strip().lower() and c["zip_code"] == zip_code.strip()
        ),
        None,
    )
    if match is None:
        session.verify_attempts += 1
        return tool_error("VERIFY_FAILED", VERIFY_FAILED_MESSAGE)

    session.verified_customer_id = match["customer_id"]
    session.verified_first_name = match["first_name"]
    return tool_ok(customer_id=match["customer_id"], first_name=match["first_name"])
