"""Checks a draft reply against what trusted code produced this session, before the customer sees it.
Pure functions. No I/O, no LLM. Every amount, date, identifier, and completed-action claim in the
reply must trace to a tool result, an application action, or the session itself. Model text and
customer text are never evidence."""

import re
from dataclasses import dataclass
from datetime import date

from bookly.config import TODAY
from bookly.models import SessionState

MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
MONEY_KEYS = {"price", "refund_amount", "fee"}  # the only JSON numbers that are money
UNTRUSTED_KEYS = {"hint", "debug"}  # tool-result fields that can echo the model's own input

# Completed-action claims only; "will be submitted", "isn't submitted", "once you click" don't match.
RETURN_CLAIM = re.compile(
    r"\b(?:(?:has|have) been|(?:I've|I have|we've|we have)|is now)\s+(?:submitted|processed|issued|refunded)\b"
    r"|\breturn (?:is|was) (?:submitted|processed)\b|\bRMA-\d"
    r"|\b(?:I've|I have|we've|we have) (?:emailed|sent you)\b",
    re.IGNORECASE,
)
# "Passed/sent" only counts when a person is named nearby, so "I've sent you a label" isn't a handoff.
ESCALATION_CLAIM = re.compile(
    r"\b(?:I've|I have|we've|we have|has been|have been)\s+(?:escalated\b|(?:passed|handed|sent|forwarded)\b"
    r"(?=[^.?!]{0,40}\b(?:teammate|team|human|colleague|specialist|person)\b))"
    r"|\b(?:opened|created) (?:a|your) ticket\b|\bticket T-\d",
    re.IGNORECASE,
)
CANCEL_CLAIM = re.compile(
    r"\b(?:I've|I have|we've|we have|has been|have been|is now)\s+(?:cancelled|canceled)\b", re.IGNORECASE
)
VERIFIED_CLAIM = re.compile(
    r"\b(?:I've|I have) verified\b|\byou(?:'re| are) (?:now )?verified\b|\byour account is verified\b",
    re.IGNORECASE,
)

IDS = re.compile(
    r"\b(?:BK|I|Q|PA|T)-\d+\b|\bRMA-\d+(?:-\d+)*\b|\b1Z[0-9A-Z]{16}\b|\b\d{20,22}\b", re.IGNORECASE
)

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
# Capitalized month names only, so "you may 3..." style prose can't match.
NAMED_DATE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b"
    r"(?:,?\s+(\d{4})\b)?"
)
SLASH_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b")
ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

SAFE_FALLBACK = (
    "Sorry, I couldn't double-check every detail in my answer, so I'd rather not guess. "
    "Could you ask me again, or would you like me to pass this to a teammate?"
)


@dataclass(frozen=True)
class Evidence:
    """What trusted code has established this session."""

    cents: frozenset[int]
    dates: frozenset[date]
    ids: frozenset[str]
    return_submitted: bool
    return_cancelled: bool
    escalated: bool
    verified: bool


@dataclass(frozen=True)
class Violation:
    """One unsupported fact or claim in a draft reply."""

    code: str  # UNSUPPORTED_AMOUNT | UNSUPPORTED_DATE | UNSUPPORTED_ID | UNSUPPORTED_ACTION
    text: str  # the exact span from the reply

    def describe(self) -> str:
        return f"{self.code}: {self.text!r}"


def to_cents(raw: str) -> int:
    """'$1,022.5' style digits -> integer cents."""
    return round(float(raw.replace(",", "")) * 100)


def add_cents(cents: set[int], value, key: str | None = None) -> None:
    """Collect every amount in a trusted JSON value: money-keyed numbers, and "$x.xx" inside strings."""
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if key in MONEY_KEYS:
            cents.add(round(value * 100))
    elif isinstance(value, str):
        cents.update(to_cents(m) for m in MONEY.findall(value))
    elif isinstance(value, dict):
        for k, v in value.items():
            add_cents(cents, v, k)
        if "refund_amount" in value and "fee" in value:  # "$27.00 minus the $4.99 fee"
            cents.add(round((value["refund_amount"] + value["fee"]) * 100))
    elif isinstance(value, list):
        for v in value:
            add_cents(cents, v, key)


def collect_strings(value, out: list[str]) -> None:
    """Every string in a JSON value, for id and date matching."""
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            collect_strings(v, out)
    elif isinstance(value, list):
        for v in value:
            collect_strings(v, out)


def trusted(output: dict) -> dict:
    """A tool result without the fields that can echo model-written input."""
    return {k: v for k, v in output.items() if k not in UNTRUSTED_KEYS}


def collect_evidence(session: SessionState) -> Evidence:
    """Gather amounts, dates, ids, and completed actions from trusted session data only."""
    sources = [trusted(e.output) for e in session.trace if e.kind == "tool"]
    sources += list(session.action_results.values())
    if session.escalation:
        sources.append(session.escalation)

    cents: set[int] = set()
    strings: list[str] = []
    for source in sources:
        add_cents(cents, source)
        collect_strings(source, strings)

    dates = {TODAY}
    ids: set[str] = set()
    for s in strings:
        dates.update(date(int(y), int(m), int(d)) for y, m, d in ISO_DATE.findall(s))
        ids.update(m.upper() for m in IDS.findall(s))

    results = session.action_results.values()
    return Evidence(
        cents=frozenset(cents),
        dates=frozenset(dates),
        ids=frozenset(ids),
        return_submitted=any(r.get("ok") and r.get("status") == "submitted" for r in results),
        return_cancelled=any(r.get("ok") and r.get("status") == "cancelled" for r in results),
        escalated=session.escalation is not None,
        verified=session.verified_customer_id is not None,
    )


def date_supported(month: int, day: int, year: int | None, evidence: Evidence) -> bool:
    """A written date matches a trusted one on month and day, and on year when the reply gives one."""
    return any(d.month == month and d.day == day and (year is None or d.year == year) for d in evidence.dates)


def check_reply(reply: str, evidence: Evidence) -> list[Violation]:
    """Every unsupported amount, date, id, and completed-action claim in a draft reply."""
    found: list[Violation] = []

    for m in MONEY.finditer(reply):
        if to_cents(m.group(1)) not in evidence.cents:
            found.append(Violation("UNSUPPORTED_AMOUNT", m.group(0)))

    for m in NAMED_DATE.finditer(reply):
        year = int(m.group(3)) if m.group(3) else None
        if not date_supported(MONTHS[m.group(1)[:3].lower()], int(m.group(2)), year, evidence):
            found.append(Violation("UNSUPPORTED_DATE", m.group(0)))
    for m in SLASH_DATE.finditer(reply):
        month, day = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and not date_supported(month, day, int(m.group(3)) if m.group(3) else None, evidence):
            found.append(Violation("UNSUPPORTED_DATE", m.group(0)))
    for m in ISO_DATE.finditer(reply):
        y, mo, d = (int(g) for g in m.groups())
        if not date_supported(mo, d, y, evidence):
            found.append(Violation("UNSUPPORTED_DATE", m.group(0)))

    for m in IDS.finditer(reply):
        if m.group(0).upper() not in evidence.ids:
            found.append(Violation("UNSUPPORTED_ID", m.group(0)))

    claims = ((RETURN_CLAIM, evidence.return_submitted), (CANCEL_CLAIM, evidence.return_cancelled),
              (ESCALATION_CLAIM, evidence.escalated), (VERIFIED_CLAIM, evidence.verified))
    for pattern, happened in claims:
        if not happened and (m := pattern.search(reply)):
            found.append(Violation("UNSUPPORTED_ACTION", m.group(0)))

    return list(dict.fromkeys(found))  # one entry per distinct span


def repair_instruction(draft: str, violations: list[Violation]) -> str:
    """The one-time system message asking for a rewrite of a draft that failed the check."""
    problems = "\n".join(f"- {v.describe()}" for v in violations)
    return (
        "Your draft reply below states things no tool result or application action in this conversation "
        f"supports:\n{problems}\n\n"
        "Rewrite the reply for the customer. Use only amounts, dates, ids, and completed actions that appear in "
        "tool results above. Don't repeat an amount the customer named unless a tool returned it. Never say an "
        "action happened unless a result shows it did. If you can't state something exactly, say you'll need to "
        "check, or offer a teammate. Reply with the customer-facing text only.\n\n"
        f"<draft>\n{draft}\n</draft>"
    )
