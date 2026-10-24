"""Support tools: answering policy questions only from the knowledge base, and handing off to a
human. Together they give Dewey an honest answer when it can't help: "not covered" or "a teammate will"."""

import re

from bookly import data
from bookly.models import SessionState, tool_error, tool_ok

MIN_MATCH_SHARE = 0.5  # a section must contain at least half the query's keywords to count as an answer
MAX_RESULTS = 3

# Words that carry no meaning for matching. "book" is here because almost every policy mentions books.
STOPWORDS = {
    "a", "an", "the", "and", "or", "to", "of", "in", "on", "for", "with", "my", "me", "i", "you", "your",
    "we", "us", "our", "do", "does", "can", "could", "is", "are", "it", "this", "that", "how", "what",
    "when", "where", "if", "be", "will", "would", "any", "there", "book", "books", "bookly",
}

SEARCH_POLICIES_SCHEMA = {
    "name": "search_policies",
    "description": (
        "Search Bookly's policy knowledge base (shipping, returns, refunds, ebooks, preorders, payment, "
        "price matching, passwords). Call it for any policy question and answer only from the returned text, "
        "naming the policy. An empty result means the policy doesn't exist: say so, don't guess."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "A few keywords from the customer's question"}},
        "required": ["query"],
        "additionalProperties": False,
    },
}

ESCALATE_SCHEMA = {
    "name": "escalate_to_human",
    "description": (
        "Hand the conversation to a human teammate by creating a support ticket. Use it when the customer asks "
        "for a person, wants an exception to a policy, needs something you have no tool for (cancellations, "
        "address changes, lost packages, topics not in the policies), or verification is locked. "
        "Then share the ticket id and when they'll hear back."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "enum": ["policy_exception", "out_of_scope", "customer_request", "verification_failed", "agent_stuck"],
            },
            "summary": {
                "type": "string",
                "description": "For the teammate: what the customer wants, the order id if any, and what was already tried",
            },
        },
        "required": ["reason", "summary"],
        "additionalProperties": False,
    },
}


def keywords(text: str) -> set[str]:
    """Lowercase meaningful words, with a trailing plural 's' dropped so 'ships' matches 'ship'."""
    words = re.findall(r"[a-z]+", text.lower())
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w not in STOPWORDS}


def search_policies(session: SessionState, query: str) -> dict:
    """Return the best-matching policy sections, or nothing if no section is a real match."""
    wanted = keywords(query)
    if not wanted:
        return tool_ok(results=[], note="no matching policy")

    scored = []
    for section in data.policies():
        overlap = len(wanted & keywords(section["title"] + " " + section["text"]))
        if overlap / len(wanted) >= MIN_MATCH_SHARE:
            title_hits = len(wanted & keywords(section["title"]))  # a title match is the strongest signal
            scored.append((overlap + title_hits, section))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    results = [section for _, section in scored[:MAX_RESULTS]]
    if not results:
        return tool_ok(results=[], note="no matching policy")
    return tool_ok(results=results)


def escalate_to_human(session: SessionState, reason: str, summary: str) -> dict:
    """Create a mock support ticket for a teammate (once per session) and return its id."""
    if session.escalation:  # idempotent: a second handoff in the same chat reuses the first ticket
        return tool_ok(**session.escalation, already_open=True)
    if len(summary.strip()) < 20:
        return tool_error(
            "SUMMARY_TOO_SHORT",
            "I need a little more detail before handing this off.",
            hint="Write a summary saying what the customer wants, the order id if any, and what you tried.",
        )

    session.escalation = {
        "ticket_id": f"T-{4000 + session.turn_index}",
        "reason": reason,
        "summary": summary.strip(),
        "expected_response": "by email within 24 hours",
    }
    return tool_ok(**session.escalation)
