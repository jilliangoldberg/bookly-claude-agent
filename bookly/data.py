"""Loads the mock backend (customers, orders, policies) once. Stands in for a real CRM,
order system, and help-center knowledge base."""

import json
import re
from functools import cache

from bookly.config import DATA_DIR


@cache
def customers() -> list[dict]:
    """Return all mock customers."""
    return json.loads((DATA_DIR / "customers.json").read_text())


@cache
def orders() -> list[dict]:
    """Return all mock orders."""
    return json.loads((DATA_DIR / "orders.json").read_text())


@cache
def policies() -> list[dict]:
    """Return policy sections parsed from policies.md headings like '## [ship-02] Where we ship'."""
    sections = re.split(r"^## ", (DATA_DIR / "policies.md").read_text(), flags=re.MULTILINE)[1:]
    parsed = []
    for section in sections:
        heading, _, body = section.partition("\n")
        match = re.match(r"\[(?P<id>[\w-]+)\]\s*(?P<title>.+)", heading)
        parsed.append({"section_id": match["id"], "title": match["title"].strip(), "text": body.strip()})
    return parsed
