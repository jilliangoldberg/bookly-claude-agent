"""Loads the mock backend (customers and orders) once. Stands in for a real CRM and order system."""

import json
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
